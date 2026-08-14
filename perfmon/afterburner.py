"""Показания железа из MSI Afterburner.

Afterburner публикует все свои датчики в общей памяти ``MAHMSharedMemory``:
заголовок, затем массив записей «источник данных -> значение», затем массив
описаний видеокарт. Имена источников (``szSrcName``) всегда английские, даже
если интерфейс переведён, поэтому по ним можно искать напрямую.

Набор датчиков не меняется, пока Afterburner работает, поэтому смещения нужных
записей вычисляются один раз при подключении, а каждый тик читает только сами
числа -- несколько чтений float из уже отображённой памяти.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .shmem import SharedMemory, SharedMemoryMissing

SHARED_MEMORY_NAME = "MAHMSharedMemory"
# В заголовке лежит многосимвольная константа 'MAHM'. Разные сборки записывают
# её в разном порядке байт, а нам важен только факт совпадения -- принимаем оба.
SIGNATURES = (0x4D41484D, 0x4D48414D)

# --- MAHM_SHARED_MEMORY_HEADER ---
HDR_SIGNATURE = 0x00
HDR_VERSION = 0x04
HDR_HEADER_SIZE = 0x08
HDR_NUM_ENTRIES = 0x0C
HDR_ENTRY_SIZE = 0x10
HDR_TIME = 0x14
HDR_NUM_GPU_ENTRIES = 0x18
HDR_GPU_ENTRY_SIZE = 0x1C

# --- MAHM_SHARED_MEMORY_ENTRY ---
STR_SIZE = 260
ENTRY_SRC_NAME = 0x000
ENTRY_SRC_UNITS = 0x104   # 260
ENTRY_DATA = 0x514        # 5 * 260
ENTRY_MIN_LIMIT = 0x518
ENTRY_MAX_LIMIT = 0x51C
ENTRY_FLAGS = 0x520
ENTRY_GPU = 0x524
ENTRY_SRC_ID = 0x528

# --- MAHM_SHARED_MEMORY_GPU_ENTRY ---
GPU_DEVICE = 0x208        # szDevice, третья строка подряд
# dwMemAmount (0x514) не используется: Afterburner оставляет его нулевым

RECONNECT_SECONDS = 5.0
STALE_SECONDS = 10.0

_CORE_USAGE = re.compile(r"^CPU(\d+)\s+usage$", re.IGNORECASE)
_CORE_CLOCK = re.compile(r"^CPU(\d+)\s+clock$", re.IGNORECASE)

# Занятая видеопамять называется по-разному в разных сборках Afterburner
VRAM_NAMES = ("memory usage", "gpu memory usage", "memory used")

# Что должно быть включено в Afterburner, чтобы строка заполнилась целиком.
# Пары «имена источников -> что покажет строка».
REQUIRED_SOURCES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cpu usage",), "CPU usage -- общая загрузка ЦП"),
    (("cpu clock",), "CPU clock -- частота ЦП"),
    (("cpu temperature",), "CPU temperature -- температура ЦП"),
    (("gpu usage",), "GPU usage -- загрузка ГП"),
    (("gpu temperature",), "GPU temperature -- температура ГП"),
    (VRAM_NAMES, "Memory usage -- занятая видеопамять"),
)


@dataclass
class HardwareReading:
    cpu_load: float | None = None
    cpu_load_per_core: list[float] = field(default_factory=list)
    cpu_clock: float | None = None                 # МГц
    cpu_clock_per_core: list[float] = field(default_factory=list)
    cpu_temp: float | None = None                  # °C
    gpu_name: str | None = None
    gpu_load: float | None = None                  # %
    gpu_temp: float | None = None                  # °C
    vram_used: float | None = None                 # МБ


class AfterburnerSource:
    """Датчики ЦП, ГП и ОЗУ из общей памяти MSI Afterburner."""

    def __init__(self) -> None:
        self._shm: SharedMemory | None = None
        self.version = 0
        self.sensor_count = 0
        self._next_attempt = 0.0
        self._last_time = 0
        self._last_change = time.perf_counter()

        # Раскладка: имя источника -> список (смещение data, индекс видеокарты).
        # Именно список: при двух видеоадаптерах Afterburner заводит по записи
        # «GPU temperature» и «Memory usage» на каждый, и запоминать только
        # первую нельзя -- у неработающего адаптера там ноль.
        self._sources: dict[str, list[tuple[int, int]]] = {}
        self._core_usage: list[int] = []
        self._core_clock: list[int] = []
        self._gpu_names: list[str] = []

        self._connect()

    # -- подключение --------------------------------------------------------
    def _connect(self) -> bool:
        now = time.perf_counter()
        if now < self._next_attempt:
            return False
        self._next_attempt = now + RECONNECT_SECONDS
        try:
            shm = SharedMemory(SHARED_MEMORY_NAME)
        except OSError:
            return False
        if shm.u32(HDR_SIGNATURE) not in SIGNATURES:
            shm.close()
            return False

        self._shm = shm
        self.version = shm.u32(HDR_VERSION)
        self._map_layout()
        self._last_time = shm.u32(HDR_TIME)
        self._last_change = now
        return True

    def _disconnect(self) -> None:
        if self._shm is not None:
            self._shm.close()
            self._shm = None
        self._sources.clear()
        self._core_usage.clear()
        self._core_clock.clear()
        self._gpu_names.clear()
        self.sensor_count = 0

    @property
    def connected(self) -> bool:
        return self._shm is not None

    @property
    def version_text(self) -> str:
        return f"{self.version >> 16}.{self.version & 0xFFFF}"

    @property
    def core_count(self) -> int:
        """Сколько ядер Afterburner отдаёт по отдельности."""
        return len(self._core_usage)

    def missing_sources(self) -> list[str]:
        """Чего не хватает, чтобы строка заполнилась целиком.

        Afterburner отдаёт только те датчики, что включены у него в
        *Settings → Monitoring*. Если чего-то нет, соответствующее поле молча
        показывало бы ``--``, поэтому список печатается при запуске.
        """
        if not self.connected:
            return ["MSI Afterburner не запущен -- показаний железа не будет"]

        gaps = [text for names, text in REQUIRED_SOURCES
                if not any(name in self._sources for name in names)]
        if not self._core_usage:
            gaps.append("CPU1..N usage -- загрузка ядер (график ядер будет пуст)")
        if not self._core_clock:
            gaps.append("CPU1..N clock -- частоты ядер "
                        "(обе частоты покажут одно общее значение)")
        return gaps

    # -- разбор раскладки (однократно) --------------------------------------
    def _map_layout(self) -> None:
        shm = self._shm
        if shm is None:
            return
        header_size = shm.u32(HDR_HEADER_SIZE)
        entry_count = shm.u32(HDR_NUM_ENTRIES)
        entry_size = shm.u32(HDR_ENTRY_SIZE)
        self.sensor_count = entry_count

        usage: list[tuple[int, int]] = []
        clock: list[tuple[int, int]] = []
        for index in range(entry_count):
            base = header_size + index * entry_size
            name = shm.text(base + ENTRY_SRC_NAME, STR_SIZE).strip()
            if not name:
                continue
            data_offset = base + ENTRY_DATA

            match = _CORE_USAGE.match(name)
            if match:
                usage.append((int(match.group(1)), data_offset))
                continue
            match = _CORE_CLOCK.match(name)
            if match:
                clock.append((int(match.group(1)), data_offset))
                continue

            gpu_index = shm.u32(base + ENTRY_GPU)
            self._sources.setdefault(name.lower(), []).append((data_offset, gpu_index))

        self._core_usage = [offset for _, offset in sorted(usage)]
        self._core_clock = [offset for _, offset in sorted(clock)]

        # Названия видеокарт -- из массива описаний за массивом датчиков
        gpu_count = shm.u32(HDR_NUM_GPU_ENTRIES)
        gpu_size = shm.u32(HDR_GPU_ENTRY_SIZE)
        base = header_size + entry_count * entry_size
        for index in range(gpu_count):
            entry = base + index * gpu_size
            self._gpu_names.append(shm.text(entry + GPU_DEVICE, STR_SIZE).strip())

    def list_sensors(self) -> list[tuple[str, float, str, int]]:
        """Все датчики Afterburner: (имя, значение, единицы, индекс видеокарты).

        Нужно, когда на чужой машине чего-то не хватает: одним взглядом видно,
        как там названы источники и какие из них живые.
        """
        shm = self._shm
        if shm is None:
            return []
        header_size = shm.u32(HDR_HEADER_SIZE)
        entry_count = shm.u32(HDR_NUM_ENTRIES)
        entry_size = shm.u32(HDR_ENTRY_SIZE)
        out = []
        for index in range(entry_count):
            base = header_size + index * entry_size
            name = shm.text(base + ENTRY_SRC_NAME, STR_SIZE).strip()
            if not name:
                continue
            out.append((name, float(shm.f32(base + ENTRY_DATA)),
                        shm.text(base + ENTRY_SRC_UNITS, STR_SIZE).strip(),
                        shm.u32(base + ENTRY_GPU)))
        return out

    # -- чтение -------------------------------------------------------------
    def _pick(self, name: str, positive: bool = True) -> tuple[float | None, int]:
        """Значение датчика и индекс видеокарты, которой оно принадлежит.

        При нескольких адаптерах побеждает первый ненулевой: у простаивающего
        встроенного видеоядра датчики отдают ноль, и брать первый попавшийся
        нельзя -- именно из-за этого пропадала загрузка видеопамяти.
        """
        candidates = self._sources.get(name)
        if not candidates or self._shm is None:
            return None, 0
        fallback = None
        for offset, gpu_index in candidates:
            value = float(self._shm.f32(offset))
            if value > 0.0:
                return value, gpu_index
            if fallback is None:
                fallback = (value, gpu_index)
        # Датчик, которого нет на этом железе, Afterburner отдаёт нулём: ни
        # температура, ни частота, ни объём памяти нулём быть не могут.
        if positive or fallback is None:
            return None, 0
        return fallback

    def _value(self, name: str, positive: bool = True) -> float | None:
        return self._pick(name, positive)[0]

    def read(self) -> HardwareReading:
        out = HardwareReading()
        shm = self._shm
        if shm is None:
            self._connect()
            return out

        # Afterburner двигает отметку времени на каждом своём опросе. Если она
        # застыла -- он закрыт, а мы читаем осиротевшую копию: переподключаемся.
        stamp = shm.u32(HDR_TIME)
        now = time.perf_counter()
        if stamp != self._last_time:
            self._last_time = stamp
            self._last_change = now
        elif now - self._last_change > STALE_SECONDS:
            self._disconnect()
            return out

        out.cpu_load = self._value("cpu usage", positive=False)
        out.cpu_clock = self._value("cpu clock")
        out.cpu_temp = self._value("cpu temperature")

        out.gpu_load, gpu_index = self._pick("gpu usage", positive=False)
        out.gpu_temp, temp_gpu = self._pick("gpu temperature")
        # «Memory usage» -- название Afterburner для занятой видеопамяти. На
        # некоторых сборках датчик зовётся иначе, поэтому проверяем синонимы.
        for name in VRAM_NAMES:
            out.vram_used, vram_gpu = self._pick(name)
            if out.vram_used is not None:
                gpu_index = vram_gpu
                break
        if out.gpu_temp is not None and out.gpu_load in (None, 0.0):
            gpu_index = temp_gpu
        if gpu_index < len(self._gpu_names):
            out.gpu_name = self._gpu_names[gpu_index] or None

        out.cpu_load_per_core = [max(0.0, shm.f32(offset)) for offset in self._core_usage]
        # Нули не выбрасываем: списки загрузок и частот должны совпадать по
        # индексам, иначе не узнать частоту самого нагруженного ядра. Ноль здесь
        # означает «ядро спит и частоту не сообщило».
        out.cpu_clock_per_core = [shm.f32(offset) for offset in self._core_clock]
        return out

    def close(self) -> None:
        self._disconnect()


def open_afterburner() -> tuple[AfterburnerSource, str]:
    """Создать источник железа и описание для лога."""
    source = AfterburnerSource()
    if source.connected:
        return source, (f"Железо: MSI Afterburner {source.version_text}, "
                        f"датчиков {source.sensor_count}")
    return source, "Железо: MSI Afterburner не запущен, ждём подключения"


__all__ = ["AfterburnerSource", "HardwareReading", "SharedMemoryMissing",
           "open_afterburner"]
