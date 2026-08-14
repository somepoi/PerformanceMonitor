"""Сборка снимка метрик ЦП / ГП / ОЗУ из показаний MSI Afterburner.

Единственный источник железа -- общая память Afterburner. Он уже собрал всё, что
нужно строке: загрузку и температуру ЦП, загрузку и частоты по ядрам, загрузку,
температуру и видеопамять ГП. Ровно тот же набор, который Afterburner рисует в
своём OSD.

Исключение одно -- оперативная память. Датчик «RAM usage» у Afterburner считает
не то же самое, что «Используется» в диспетчере задач (расходится на единицы
гигабайт), а полного объёма он не сообщает вовсе. Поэтому ОЗУ берётся из
``GlobalMemoryStatusEx``: это ровно та цифра, с которой пользователь сравнивает,
вызов стоит единицы микросекунд и доступен всегда.
"""

from __future__ import annotations

import ctypes
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field

from .afterburner import AfterburnerSource, open_afterburner

MB = 1024.0 * 1024.0
GB = 1024.0 * 1024.0 * 1024.0


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------
@dataclass
class CpuStats:
    # None означает «датчика нет» и рисуется как «--». Ноль в этих полях был бы
    # ложью: он неотличим от настоящей нулевой загрузки.
    load_avg: float | None = None              # %, среднее по всем ядрам
    load_max: float | None = None              # %, самое загруженное ядро
    clock_avg: float | None = None             # ГГц, среднее по всем ядрам
    clock_max: float | None = None             # ГГц, у самого загруженного ядра
    per_core: list[float] = field(default_factory=list)  # % по каждому ядру
    temp: float | None = None                  # °C


@dataclass
class GpuStats:
    load: float | None = None       # %
    temp: float | None = None       # °C
    vram_used: float | None = None  # ГБ
    vram_total: float | None = None # ГБ
    name: str | None = None


@dataclass
class RamStats:
    used: float | None = None   # ГБ
    total: float | None = None  # ГБ


@dataclass
class Snapshot:
    cpu: CpuStats = field(default_factory=CpuStats)
    gpu: GpuStats = field(default_factory=GpuStats)
    ram: RamStats = field(default_factory=RamStats)


# ---------------------------------------------------------------------------
# Полный объём ОЗУ -- запасной источник
# ---------------------------------------------------------------------------
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


_kernel32 = ctypes.WinDLL("kernel32.dll")
_kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
_kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

# Структура переиспользуется: опрос идёт из одного потока раз в секунду
_memory_status = MEMORYSTATUSEX()
_memory_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)


def read_memory() -> RamStats:
    """Занятая и полная физическая память по данным Windows."""
    if _kernel32.GlobalMemoryStatusEx(ctypes.byref(_memory_status)):
        total = float(_memory_status.ullTotalPhys)
        return RamStats(used=(total - float(_memory_status.ullAvailPhys)) / GB,
                        total=total / GB)
    return RamStats()


# Класс видеоадаптеров в реестре
_DISPLAY_CLASS = (r"SYSTEM\CurrentControlSet\Control\Class"
                  r"\{4d36e968-e325-11ce-bfc1-08002be10318}")


def vram_total_gb(device_name: str | None = None) -> float | None:
    """Полный объём видеопамяти, ГБ.

    Afterburner его не сообщает: ``dwMemAmount`` в описании видеокарты остаётся
    нулевым, а ``maxLimit`` датчика «Memory usage» -- это предел шкалы графика
    (8192 МБ хоть для карты на 16 ГБ). Настоящее значение лежит в реестре, в
    свойствах адаптера.

    В системе обычно два адаптера -- встроенный и дискретный, -- поэтому нужный
    ищется по имени из Afterburner; если совпадения нет, берётся самый большой.
    """
    best: float | None = None
    exact: float | None = None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as root:
            for index in range(32):
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as adapter:
                        raw, _ = winreg.QueryValueEx(
                            adapter, "HardwareInformation.qwMemorySize")
                        desc, _ = winreg.QueryValueEx(adapter, "DriverDesc")
                except OSError:
                    continue

                if isinstance(raw, bytes):
                    if len(raw) < 8:
                        continue
                    size = int.from_bytes(raw[:8], "little")
                elif isinstance(raw, int):
                    size = raw
                else:
                    continue
                if size <= 0:
                    continue

                gigabytes = size / GB
                if device_name and str(desc).strip() == device_name.strip():
                    exact = gigabytes
                if best is None or gigabytes > best:
                    best = gigabytes
    except OSError:
        return None
    return exact if exact is not None else best


# ---------------------------------------------------------------------------
# Сборщик
# ---------------------------------------------------------------------------
class MetricsCollector:
    """Снимок метрик железа. Все обращения -- из одного рабочего потока."""

    def __init__(self, hardware: AfterburnerSource | None = None) -> None:
        if hardware is None:
            hardware, note = open_afterburner()
            self.notes = [note]
        else:
            self.notes = []
        self.hardware = hardware
        # Объём видеопамяти постоянен -- читаем реестр один раз, а не каждый тик
        self._vram_total: float | None = None
        self._vram_probed_for: str | None = None

    @property
    def connected(self) -> bool:
        return self.hardware.connected

    def read(self) -> Snapshot:
        snapshot = Snapshot()
        reading = self.hardware.read()

        # --- ЦП ---
        cpu = snapshot.cpu
        cores = reading.cpu_load_per_core
        cpu.per_core = cores
        if cores:
            cpu.load_avg = sum(cores) / len(cores)
            cpu.load_max = max(cores)
        elif reading.cpu_load is not None:
            # Afterburner не показывает ядра по отдельности -- строка остаётся
            # осмысленной, просто оба значения совпадают, а график ядер пуст
            cpu.load_avg = cpu.load_max = reading.cpu_load
        if reading.cpu_load is not None and cores:
            cpu.load_avg = reading.cpu_load    # общая загрузка точнее среднего

        clocks = reading.cpu_clock_per_core
        live = [value for value in clocks if value > 0]
        if live:
            cpu.clock_avg = sum(live) / len(live) / 1000.0
            # Стрелочка вверх помечает самое нагруженное ядро -- значит и частоту
            # берём его же, а не просто наибольшую по системе
            busiest_clock = 0.0
            if cores and len(clocks) == len(cores):
                busiest = max(range(len(cores)), key=cores.__getitem__)
                busiest_clock = clocks[busiest]
            cpu.clock_max = (busiest_clock or max(live)) / 1000.0
        elif reading.cpu_clock is not None:
            cpu.clock_avg = cpu.clock_max = reading.cpu_clock / 1000.0
        cpu.temp = reading.cpu_temp

        # --- ГП ---
        gpu = snapshot.gpu
        gpu.load = reading.gpu_load
        gpu.temp = reading.gpu_temp
        gpu.name = reading.gpu_name
        if reading.vram_used is not None:
            gpu.vram_used = reading.vram_used * MB / GB
        if reading.vram_total is not None:
            gpu.vram_total = reading.vram_total * MB / GB
        elif reading.gpu_name is not None:
            if self._vram_probed_for != reading.gpu_name:
                self._vram_probed_for = reading.gpu_name
                self._vram_total = vram_total_gb(reading.gpu_name)
            gpu.vram_total = self._vram_total

        # --- ОЗУ ---
        snapshot.ram = read_memory()
        return snapshot

    def close(self) -> None:
        self.hardware.close()
