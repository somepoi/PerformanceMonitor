"""Тонкая обёртка над Windows PDH (Performance Data Helper) поверх ctypes.

PDH даёт то, чего нет в psutil:
  * ``% Processor Utility``     -- «полезная» загрузка ядра (с поправкой на реальную частоту);
  * ``% Processor Performance`` -- отношение текущей частоты к базовой (реальные ГГц, с турбо);
  * ``GPU Engine`` / ``GPU Adapter Memory`` -- загрузка ГП и VRAM без прав админа.

Счётчики добавляются через ``PdhAddEnglishCounterW``, поэтому имена не зависят
от языка системы (на русской Windows счётчики локализованы).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_pdh = ctypes.WinDLL("pdh.dll")

# --- константы PDH ---------------------------------------------------------
PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000

ERROR_SUCCESS = 0
PDH_MORE_DATA = 0x800007D2
PDH_NO_DATA = 0x800007D5
PDH_INVALID_DATA = 0xC0000BC6
PDH_CALC_NEGATIVE_DENOMINATOR = 0x800007D6

_VALID_STATUS = (0x00000000, 0x00000001)  # VALID_DATA, NEW_DATA


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    # union выровнен по 8 байт -> ctypes сам добавит padding после DWORD
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", PDH_FMT_COUNTERVALUE)]


HQUERY = wintypes.HANDLE
HCOUNTER = wintypes.HANDLE

_pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(HQUERY)]
_pdh.PdhOpenQueryW.restype = ctypes.c_long

_pdh.PdhAddEnglishCounterW.argtypes = [HQUERY, wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(HCOUNTER)]
_pdh.PdhAddEnglishCounterW.restype = ctypes.c_long

_pdh.PdhCollectQueryData.argtypes = [HQUERY]
_pdh.PdhCollectQueryData.restype = ctypes.c_long

_pdh.PdhCloseQuery.argtypes = [HQUERY]
_pdh.PdhCloseQuery.restype = ctypes.c_long

_pdh.PdhGetFormattedCounterValue.argtypes = [HCOUNTER, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD),
                                             ctypes.POINTER(PDH_FMT_COUNTERVALUE)]
_pdh.PdhGetFormattedCounterValue.restype = ctypes.c_long

_pdh.PdhGetFormattedCounterArrayW.argtypes = [HCOUNTER, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(wintypes.DWORD),
                                              ctypes.c_void_p]
_pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long


def _u32(status: int) -> int:
    """PDH возвращает статус как знаковый long -- приводим к беззнаковому виду."""
    return status & 0xFFFFFFFF


class PdhQuery:
    """Одна PDH-сессия с набором именованных счётчиков."""

    def __init__(self) -> None:
        self._query = HQUERY()
        status = _u32(_pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)))
        if status != ERROR_SUCCESS:
            raise OSError(f"PdhOpenQueryW failed: 0x{status:08X}")
        self._counters: dict[str, HCOUNTER] = {}
        # Буферы под wildcard-выборки переиспользуются между тиками: PDH-запрос
        # выполняется раз в секунду, и незачем каждый раз выделять пару килобайт.
        self._buffers: dict[str, ctypes.Array] = {}
        self._closed = False

    # -- построение ---------------------------------------------------------
    def add(self, name: str, path: str) -> bool:
        """Добавить счётчик под ключом *name*. Возвращает False, если его нет в системе."""
        handle = HCOUNTER()
        status = _u32(_pdh.PdhAddEnglishCounterW(self._query, path, None,
                                                 ctypes.byref(handle)))
        if status != ERROR_SUCCESS:
            return False
        self._counters[name] = handle
        return True

    def has(self, name: str) -> bool:
        return name in self._counters

    # -- опрос --------------------------------------------------------------
    def collect(self) -> bool:
        """Снять сырые значения. Первый вызов только заполняет базу для дельт."""
        return _u32(_pdh.PdhCollectQueryData(self._query)) == ERROR_SUCCESS

    def value(self, name: str, nocap100: bool = False) -> float | None:
        """Значение одиночного (не wildcard) счётчика."""
        handle = self._counters.get(name)
        if handle is None:
            return None
        fmt = PDH_FMT_DOUBLE | (PDH_FMT_NOCAP100 if nocap100 else 0)
        out = PDH_FMT_COUNTERVALUE()
        status = _u32(_pdh.PdhGetFormattedCounterValue(handle, fmt, None, ctypes.byref(out)))
        if status != ERROR_SUCCESS or _u32(out.CStatus) not in _VALID_STATUS:
            return None
        return float(out.doubleValue)

    @staticmethod
    def _unpack(buf: ctypes.Array, count: int) -> dict[str, float]:
        items = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        result: dict[str, float] = {}
        for i in range(count):
            item = items[i]
            if _u32(item.FmtValue.CStatus) not in _VALID_STATUS:
                continue
            if item.szName:
                result[item.szName] = float(item.FmtValue.doubleValue)
        return result

    def values(self, name: str, nocap100: bool = False) -> dict[str, float]:
        """Значения wildcard-счётчика: {имя_экземпляра: значение}."""
        handle = self._counters.get(name)
        if handle is None:
            return {}
        fmt = PDH_FMT_DOUBLE | (PDH_FMT_NOCAP100 if nocap100 else 0)
        count = wintypes.DWORD(0)

        # Сначала пробуем уже выделенный буфер -- в устоявшемся режиме набор
        # экземпляров не меняется, так что это единственный вызов PDH за тик.
        buf = self._buffers.get(name)
        if buf is not None:
            size = wintypes.DWORD(ctypes.sizeof(buf))
            status = _u32(_pdh.PdhGetFormattedCounterArrayW(
                handle, fmt, ctypes.byref(size), ctypes.byref(count),
                ctypes.cast(buf, ctypes.c_void_p)))
            if status == ERROR_SUCCESS:
                return self._unpack(buf, count.value)
            if status != PDH_MORE_DATA:
                return {}

        # Буфера нет или он мал -- узнаём нужный размер и выделяем заново
        size = wintypes.DWORD(0)
        status = _u32(_pdh.PdhGetFormattedCounterArrayW(
            handle, fmt, ctypes.byref(size), ctypes.byref(count), None))
        if status != PDH_MORE_DATA or size.value == 0:
            return {}

        buf = ctypes.create_string_buffer(size.value)
        status = _u32(_pdh.PdhGetFormattedCounterArrayW(
            handle, fmt, ctypes.byref(size), ctypes.byref(count),
            ctypes.cast(buf, ctypes.c_void_p)))
        if status != ERROR_SUCCESS:
            return {}
        self._buffers[name] = buf
        return self._unpack(buf, count.value)

    # -- жизненный цикл -----------------------------------------------------
    def close(self) -> None:
        if not self._closed and self._query:
            _pdh.PdhCloseQuery(self._query)
            self._closed = True

    def __del__(self) -> None:  # pragma: no cover - подстраховка
        try:
            self.close()
        except Exception:
            pass


def sort_cpu_instances(names: list[str]) -> list[str]:
    """Отсортировать имена экземпляров вида ``"0,5"`` (группа, ядро) по-человечески."""
    def key(name: str):
        try:
            return tuple(int(part) for part in name.split(","))
        except ValueError:
            return (1 << 30,)
    return sorted(names, key=key)
