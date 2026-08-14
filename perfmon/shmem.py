"""Чтение именованных объектов общей памяти Windows.

И RTSS, и MSI Afterburner публикуют свои показания через file mapping: они
пишут, все остальные читают. Отсюда два приятных следствия -- опрос стоит
буквально наносекунды (это обычное чтение из памяти, без syscall'ов) и не нужны
права администратора: оба объекта создаются с доступом на чтение для всех.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_MAP_READ = 0x0004
ERROR_FILE_NOT_FOUND = 2

_kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenFileMappingW.restype = wintypes.HANDLE
_kernel32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_size_t]
_kernel32.MapViewOfFile.restype = ctypes.c_void_p
_kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_kernel32.UnmapViewOfFile.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class SharedMemoryMissing(OSError):
    """Объект общей памяти не найден -- программа-издатель не запущена."""


class SharedMemory:
    """Отображение именованной общей памяти только на чтение."""

    def __init__(self, name: str) -> None:
        handle = _kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not handle:
            code = ctypes.get_last_error()
            if code == ERROR_FILE_NOT_FOUND:
                raise SharedMemoryMissing(f"объект «{name}» не найден")
            raise OSError(f"OpenFileMappingW({name}) failed: {code}")

        address = _kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
        if not address:
            code = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise OSError(f"MapViewOfFile({name}) failed: {code}")

        self.name = name
        self._handle = handle
        self._address = int(address)

    # -- чтение полей -------------------------------------------------------
    def u32(self, offset: int) -> int:
        return ctypes.c_uint32.from_address(self._address + offset).value

    def f32(self, offset: int) -> float:
        return ctypes.c_float.from_address(self._address + offset).value

    def raw(self, offset: int, size: int) -> bytes:
        return ctypes.string_at(self._address + offset, size)

    def text(self, offset: int, size: int) -> str:
        """Строка фиксированной длины с завершающим нулём (ASCII)."""
        return self.raw(offset, size).split(b"\x00", 1)[0].decode("latin-1")

    # -- жизненный цикл -----------------------------------------------------
    def close(self) -> None:
        if self._address:
            _kernel32.UnmapViewOfFile(ctypes.c_void_p(self._address))
            self._address = 0
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __del__(self) -> None:  # pragma: no cover - подстраховка
        try:
            self.close()
        except Exception:
            pass
