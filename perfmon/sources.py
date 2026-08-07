"""Сбор метрик ЦП / ГП / ОЗУ.

Источники выбираются автоматически, по убыванию точности:

ЦП   -- PDH ``% Processor Utility`` + ``% Processor Performance`` (реальная
        загрузка и реальные частоты с турбо), с откатом на psutil.
Темп. ЦП -- LibreHardwareMonitor.
ГП   -- NVML (NVIDIA) -> LibreHardwareMonitor -> PDH ``GPU Engine``.
ОЗУ  -- ``GlobalMemoryStatusEx`` напрямую (psutil здесь в 700 раз дороже).
"""

from __future__ import annotations

import ctypes
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field

import psutil

from .winpdh import PdhQuery, sort_cpu_instances

MB = 1024.0 * 1024.0
GB = 1024.0 * 1024.0 * 1024.0


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------
@dataclass
class CpuStats:
    load_avg: float = 0.0                      # %, среднее по всем ядрам
    load_max: float = 0.0                      # %, самое загруженное ядро
    clock_avg: float = 0.0                     # ГГц, среднее по всем ядрам
    clock_max: float = 0.0                     # ГГц, у самого загруженного ядра
    per_core: list[float] = field(default_factory=list)  # % по каждому ядру
    temp: float | None = None               # °C


@dataclass
class GpuStats:
    load: float | None = None       # %
    temp: float | None = None       # °C
    vram_used: float | None = None  # ГБ
    vram_total: float | None = None # ГБ
    name: str | None = None


@dataclass
class RamStats:
    used: float = 0.0   # ГБ
    total: float = 0.0  # ГБ


@dataclass
class Snapshot:
    cpu: CpuStats = field(default_factory=CpuStats)
    gpu: GpuStats = field(default_factory=GpuStats)
    ram: RamStats = field(default_factory=RamStats)


# ---------------------------------------------------------------------------
# Вспомогательное
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
    """Занятая и полная физическая память.

    ``psutil.virtual_memory()`` на Windows дополнительно дёргает
    ``GetPerformanceInfo`` и обходит список процессов — это 1.4 мс на тик,
    больше, чем все остальные метрики вместе взятые. Нам нужны только два поля
    из ``GlobalMemoryStatusEx``, а это уже единицы микросекунд.
    Значение совпадает с psutil: там ``used`` на Windows тоже
    ``total - available``, где ``available == ullAvailPhys``.
    """
    if _kernel32.GlobalMemoryStatusEx(ctypes.byref(_memory_status)):
        total = float(_memory_status.ullTotalPhys)
        return RamStats(used=(total - float(_memory_status.ullAvailPhys)) / GB,
                        total=total / GB)
    memory = psutil.virtual_memory()  # маловероятный откат
    return RamStats(used=(memory.total - memory.available) / GB,
                    total=memory.total / GB)


def _base_clock_mhz() -> float:
    """Базовая частота ЦП в МГц (нужна, чтобы развернуть % Processor Performance)."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            value, _ = winreg.QueryValueEx(key, "~MHz")
            if value:
                return float(value)
    except OSError:
        pass
    try:
        freq = psutil.cpu_freq()
        if freq and freq.max:
            return float(freq.max)
        if freq and freq.current:
            return float(freq.current)
    except Exception:
        pass
    return 0.0


def _registry_vram_total_bytes() -> float | None:
    """Объём видеопамяти из реестра (класс видеоадаптеров) -- запасной вариант."""
    class_key = (r"SYSTEM\CurrentControlSet\Control\Class"
                 r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
    best: float | None = None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, class_key) as root:
            for index in range(32):
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as adapter:
                        raw, _ = winreg.QueryValueEx(adapter,
                                                     "HardwareInformation.qwMemorySize")
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
                if size > 0 and (best is None or size > best):
                    best = float(size)
    except OSError:
        pass
    return best


# ---------------------------------------------------------------------------
# ЦП
# ---------------------------------------------------------------------------
class CpuCollector:
    """Загрузка ядер и их реальные частоты."""

    def __init__(self) -> None:
        self.base_mhz = _base_clock_mhz()
        self._core_order: list[str] = []
        self._pdh: PdhQuery | None = None
        self._has_utility = False
        self._has_perf = False

        try:
            query = PdhQuery()
            self._has_utility = query.add(
                "utility", r"\Processor Information(*)\% Processor Utility")
            if not self._has_utility:
                # На старых сборках Windows счётчика Utility нет
                self._has_utility = query.add(
                    "utility", r"\Processor Information(*)\% Processor Time")
            self._has_perf = query.add(
                "perf", r"\Processor Information(*)\% Processor Performance")
            if self._has_utility or self._has_perf:
                query.collect()
                self._pdh = query
            else:
                query.close()
        except OSError:
            self._pdh = None

        # Прогреваем psutil, чтобы первый замер не был нулевым
        psutil.cpu_percent(percpu=True)

    @staticmethod
    def _real_cores(raw: dict) -> dict:
        return {name: value for name, value in raw.items() if "_Total" not in name}

    def read(self) -> CpuStats:
        stats = CpuStats()
        loads: list[float] = []
        clocks: list[float] = []

        if self._pdh is not None and self._pdh.collect():
            util = self._real_cores(self._pdh.values("utility")) if self._has_utility else {}
            perf = self._real_cores(
                self._pdh.values("perf", nocap100=True)) if self._has_perf else {}

            names = util or perf
            if names:
                if not self._core_order or set(self._core_order) != set(names):
                    self._core_order = sort_cpu_instances(list(names.keys()))
                loads = [min(100.0, max(0.0, util.get(n, 0.0))) for n in self._core_order]
                if perf and self.base_mhz:
                    clocks = [self.base_mhz * perf.get(n, 0.0) / 100.0
                              for n in self._core_order]

        if not loads:  # откат на psutil
            loads = list(psutil.cpu_percent(percpu=True))

        if not clocks:
            try:
                freq = psutil.cpu_freq()
                current = float(freq.current) if freq and freq.current else self.base_mhz
            except Exception:
                current = self.base_mhz
            clocks = [current] * len(loads)

        if not loads:
            return stats

        stats.per_core = loads
        stats.load_avg = sum(loads) / len(loads)
        busiest = max(range(len(loads)), key=loads.__getitem__)
        stats.load_max = loads[busiest]

        if clocks:
            stats.clock_avg = (sum(clocks) / len(clocks)) / 1000.0
            index = busiest if busiest < len(clocks) else 0
            stats.clock_max = max(clocks[index], max(clocks)) / 1000.0
        return stats

    def close(self) -> None:
        if self._pdh is not None:
            self._pdh.close()
            self._pdh = None


# ---------------------------------------------------------------------------
# ГП
# ---------------------------------------------------------------------------
class NvmlGpu:
    """NVIDIA через NVML -- самый точный источник для GeForce/Quadro."""

    def __init__(self) -> None:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        self._nvml = pynvml
        count = pynvml.nvmlDeviceGetCount()
        if count == 0:
            pynvml.nvmlShutdown()
            raise RuntimeError("NVML: устройств не найдено")
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(self._handle)
        self.name = name.decode() if isinstance(name, bytes) else str(name)

    def read(self) -> GpuStats:
        nvml = self._nvml
        stats = GpuStats(name=self.name)
        try:
            stats.load = float(nvml.nvmlDeviceGetUtilizationRates(self._handle).gpu)
        except Exception:
            pass
        try:
            stats.temp = float(nvml.nvmlDeviceGetTemperature(
                self._handle, nvml.NVML_TEMPERATURE_GPU))
        except Exception:
            pass
        try:
            mem = nvml.nvmlDeviceGetMemoryInfo(self._handle)
            stats.vram_used = float(mem.used) / GB
            stats.vram_total = float(mem.total) / GB
        except Exception:
            pass
        return stats

    def close(self) -> None:
        try:
            self._nvml.nvmlShutdown()
        except Exception:
            pass


class PdhGpu:
    """Универсальный откат: счётчики ``GPU Engine`` / ``GPU Adapter Memory``.

    Работает для любых видеокарт и без прав администратора, но не даёт температуру.
    """

    def __init__(self) -> None:
        self._query = PdhQuery()
        ok_engine = self._query.add("engine", r"\GPU Engine(*)\Utilization Percentage")
        ok_memory = self._query.add("memory", r"\GPU Adapter Memory(*)\Dedicated Usage")
        if not (ok_engine or ok_memory):
            self._query.close()
            raise RuntimeError("Счётчики GPU Engine недоступны")
        self._has_engine = ok_engine
        self._has_memory = ok_memory
        self._query.collect()
        total = _registry_vram_total_bytes()
        self._vram_total = total / GB if total else None
        self.name = "GPU"

    def read(self) -> GpuStats:
        stats = GpuStats(name=self.name, vram_total=self._vram_total)
        if not self._query.collect():
            return stats

        if self._has_engine:
            engines = self._query.values("engine")
            # Суммируем только 3D-движки: это и есть «загрузка видеокарты»
            total = sum(value for name, value in engines.items()
                        if "engtype_3D" in name)
            if total == 0.0 and engines:
                total = sum(value for name, value in engines.items()
                            if "_Total" not in name)
            stats.load = min(100.0, total)

        if self._has_memory:
            adapters = self._query.values("memory")
            usable = [value for name, value in adapters.items() if "_Total" not in name]
            if usable:
                stats.vram_used = max(usable) / GB
        return stats

    def close(self) -> None:
        self._query.close()


# ---------------------------------------------------------------------------
# Общий сборщик
# ---------------------------------------------------------------------------
class MetricsCollector:
    """Собирает полный снимок метрик. Все обращения -- из одного рабочего потока."""

    def __init__(self, lhm_dll: str | None = None, use_lhm: bool = True) -> None:
        self.cpu = CpuCollector()
        self.gpu_source = None
        self.lhm = None
        self.notes: list[str] = []

        # ГП: NVML -> PDH
        for factory, label in ((NvmlGpu, "NVML"), (PdhGpu, "PDH GPU Engine")):
            try:
                self.gpu_source = factory()
                self.notes.append(f"ГП: {label}")
                break
            except Exception:
                continue
        if self.gpu_source is None:
            self.notes.append("ГП: источник не найден")

        # Температуры (и запасные значения для ГП) -- LibreHardwareMonitor
        if use_lhm:
            from .lhm import open_lhm
            self.lhm = open_lhm(lhm_dll)
            self.notes.append("Температуры: LibreHardwareMonitor"
                              if self.lhm else "Температуры: недоступны (нужен LHM)")

    def read(self) -> Snapshot:
        snapshot = Snapshot()
        snapshot.cpu = self.cpu.read()

        if self.gpu_source is not None:
            try:
                snapshot.gpu = self.gpu_source.read()
            except Exception:
                snapshot.gpu = GpuStats()

        if self.lhm is not None:
            try:
                reading = self.lhm.read()
            except Exception:
                reading = None
            if reading is not None:
                snapshot.cpu.temp = reading.cpu_temp
                gpu = snapshot.gpu
                if gpu.temp is None:
                    gpu.temp = reading.gpu_temp
                if gpu.load is None and reading.gpu_load is not None:
                    gpu.load = reading.gpu_load
                if gpu.vram_used is None and reading.gpu_vram_used is not None:
                    gpu.vram_used = reading.gpu_vram_used * MB / GB
                if gpu.vram_total is None and reading.gpu_vram_total is not None:
                    gpu.vram_total = reading.gpu_vram_total * MB / GB
                if gpu.name is None:
                    gpu.name = reading.gpu_name

        snapshot.ram = read_memory()
        return snapshot

    def close(self) -> None:
        self.cpu.close()
        for obj in (self.gpu_source, self.lhm):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
