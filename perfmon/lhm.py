"""Доступ к сенсорам LibreHardwareMonitor (температуры ЦП/ГП, частоты, VRAM).

Работает двумя путями, по убыванию качества:

1. **Встроенная библиотека** -- ``LibreHardwareMonitorLib.dll`` загружается прямо в
   процесс через pythonnet. Требует ``pip install pythonnet`` и запуска от
   администратора (иначе драйвер мониторинга не поднимется).
2. **WMI** -- если у пользователя уже запущен LibreHardwareMonitor или
   OpenHardwareMonitor с включённым «Remote/WMI», читаем пространства имён
   ``root\\LibreHardwareMonitor`` / ``root\\OpenHardwareMonitor``.

Оба пути опциональны: при их отсутствии температуры просто не показываются.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Стандартные места установки LibreHardwareMonitor
_DLL_CANDIDATES = (
    r"lib\LibreHardwareMonitorLib.dll",
    r"LibreHardwareMonitorLib.dll",
    r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitorLib.dll",
    r"C:\Program Files (x86)\LibreHardwareMonitor\LibreHardwareMonitorLib.dll",
)


@dataclass
class LhmReading:
    """Сводка того, что удалось вытащить из LibreHardwareMonitor."""
    cpu_temp: float | None = None
    cpu_core_clocks: list[float] = field(default_factory=list)  # МГц
    gpu_name: str | None = None
    gpu_load: float | None = None      # %
    gpu_temp: float | None = None      # °C
    gpu_vram_used: float | None = None   # МБ
    gpu_vram_total: float | None = None  # МБ


def _find_dll(explicit: str | None = None) -> str | None:
    paths = []
    if explicit:
        paths.append(explicit)
    env = os.environ.get("LHM_DLL")
    if env:
        paths.append(env)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths.extend(rel if os.path.isabs(rel) else os.path.join(here, rel)
                 for rel in _DLL_CANDIDATES)
    for path in paths:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


class LhmLibrarySource:
    """Прямая загрузка LibreHardwareMonitorLib.dll через pythonnet."""

    def __init__(self, dll_path: str | None = None) -> None:
        path = _find_dll(dll_path)
        if path is None:
            raise RuntimeError("LibreHardwareMonitorLib.dll не найдена")

        import sys as _sys

        import clr
        directory = os.path.dirname(path)
        if directory not in _sys.path:
            _sys.path.append(directory)
        clr.AddReference(path[:-4] if path.lower().endswith(".dll") else path)

        from LibreHardwareMonitor.Hardware import Computer  # type: ignore

        self._computer = Computer()
        self._computer.IsCpuEnabled = True
        self._computer.IsGpuEnabled = True
        self._computer.IsMemoryEnabled = True
        self._computer.IsMotherboardEnabled = True
        self._computer.Open()
        self.dll_path = path

    def read(self) -> LhmReading:
        out = LhmReading()
        cpu_package: float | None = None
        cpu_core_temps: list[float] = []

        for hardware in self._computer.Hardware:
            hardware.Update()
            for sub in hardware.SubHardware:
                sub.Update()
            kind = str(hardware.HardwareType)

            if kind == "Cpu":
                for sensor in hardware.Sensors:
                    stype = str(sensor.SensorType)
                    name = str(sensor.Name)
                    value = sensor.Value
                    if value is None:
                        continue
                    if stype == "Temperature":
                        if "Package" in name or "Tctl" in name or "Tdie" in name:
                            cpu_package = float(value)
                        elif name.startswith("CPU Core"):
                            cpu_core_temps.append(float(value))
                    elif stype == "Clock" and name.startswith("CPU Core"):
                        out.cpu_core_clocks.append(float(value))

            elif kind.startswith("Gpu"):
                out.gpu_name = str(hardware.Name)
                for sensor in hardware.Sensors:
                    stype = str(sensor.SensorType)
                    name = str(sensor.Name)
                    value = sensor.Value
                    if value is None:
                        continue
                    if stype == "Temperature" and "Core" in name:
                        out.gpu_temp = float(value)
                    elif stype == "Load" and name in ("GPU Core", "D3D 3D"):
                        if out.gpu_load is None or name == "GPU Core":
                            out.gpu_load = float(value)
                    elif stype == "SmallData":
                        if name in ("GPU Memory Used", "D3D Dedicated Memory Used"):
                            if out.gpu_vram_used is None or name == "GPU Memory Used":
                                out.gpu_vram_used = float(value)
                        elif name == "GPU Memory Total":
                            out.gpu_vram_total = float(value)

        out.cpu_temp = cpu_package if cpu_package is not None else (
            max(cpu_core_temps) if cpu_core_temps else None)
        return out

    def close(self) -> None:
        try:
            self._computer.Close()
        except Exception:
            pass


class LhmWmiSource:
    """Чтение сенсоров уже запущенного LHM/OHM через WMI."""

    NAMESPACES = (r"root\LibreHardwareMonitor", r"root\OpenHardwareMonitor")

    def __init__(self) -> None:
        import wmi  # type: ignore

        self._conn = None
        last_error: Exception | None = None
        for namespace in self.NAMESPACES:
            try:
                conn = wmi.WMI(namespace=namespace)
                conn.Sensor()  # проверяем, что класс действительно есть
                self._conn = conn
                self.namespace = namespace
                break
            except Exception as exc:
                last_error = exc
        if self._conn is None:
            raise RuntimeError(f"WMI-пространство LHM/OHM недоступно: {last_error}")

    def read(self) -> LhmReading:
        out = LhmReading()
        cpu_package: float | None = None
        cpu_core_temps: list[float] = []

        for sensor in self._conn.Sensor():
            name = sensor.Name or ""
            stype = sensor.SensorType or ""
            ident = (sensor.Identifier or "").lower()
            value = sensor.Value
            if value is None:
                continue
            value = float(value)

            is_gpu = "/gpu" in ident or "amdgpu" in ident or "nvidiagpu" in ident
            if stype == "Temperature":
                if is_gpu and "Core" in name:
                    out.gpu_temp = value
                elif "/cpu" in ident:
                    if "Package" in name or "Tctl" in name or "Tdie" in name:
                        cpu_package = value
                    elif name.startswith("CPU Core"):
                        cpu_core_temps.append(value)
            elif stype == "Clock" and "/cpu" in ident and name.startswith("CPU Core"):
                out.cpu_core_clocks.append(value)
            elif stype == "Load" and is_gpu and name == "GPU Core":
                out.gpu_load = value
            elif stype == "SmallData" and is_gpu:
                if name == "GPU Memory Used":
                    out.gpu_vram_used = value
                elif name == "GPU Memory Total":
                    out.gpu_vram_total = value

        out.cpu_temp = cpu_package if cpu_package is not None else (
            max(cpu_core_temps) if cpu_core_temps else None)
        return out

    def close(self) -> None:
        self._conn = None


def _try_open(factory):
    try:
        return factory()
    except Exception:
        return None


def open_lhm(dll_path: str | None = None):
    """Вернуть первый доступный источник LHM или ``None``.

    Сначала библиотека в процессе (точнее и быстрее), затем WMI уже
    запущенного LibreHardwareMonitor.
    """
    return _try_open(lambda: LhmLibrarySource(dll_path)) or _try_open(LhmWmiSource)
