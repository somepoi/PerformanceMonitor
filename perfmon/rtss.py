"""FPS из RivaTuner Statistics Server.

RTSS перехватывает вызовы представления кадра в самой игре и публикует
результат в общей памяти ``RTSSSharedMemoryV2``. Это тот же источник, из
которого рисует свой оверлей MSI Afterburner, так что цифры сходятся с
привычными один в один.

Формат: заголовок из девяти DWORD, затем массив записей приложений. Размер
записи берётся из заголовка (``dwAppEntrySize``), поэтому читать по фиксированным
смещениям можно только начало структуры -- зато оно неизменно во всех версиях
2.x, и всё нужное лежит именно там.
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque
from dataclasses import dataclass

from .shmem import SharedMemory, SharedMemoryMissing

_user32 = ctypes.WinDLL("user32.dll")

SHARED_MEMORY_NAME = "RTSSSharedMemoryV2"
# Многосимвольная константа 'RTSS'. Порядок байт зависит от того, как её
# записала конкретная сборка, поэтому принимаем оба варианта.
SIGNATURES = (0x52545353, 0x53535452)

# --- заголовок RTSS_SHARED_MEMORY ---
HDR_SIGNATURE = 0x00
HDR_VERSION = 0x04
HDR_APP_ENTRY_SIZE = 0x08
HDR_APP_ARR_OFFSET = 0x0C
HDR_APP_ARR_SIZE = 0x10

# --- начало RTSS_SHARED_MEMORY_APP_ENTRY (стабильно во всех 2.x) ---
APP_PROCESS_ID = 0x000
APP_NAME = 0x004        # char[MAX_PATH]
APP_NAME_SIZE = 260
APP_FLAGS = 0x108
APP_TIME0 = 0x10C       # начало окна усреднения, мс
APP_TIME1 = 0x110       # конец окна усреднения, мс
APP_FRAMES = 0x114      # кадров за окно
APP_FRAME_TIME = 0x118  # время последнего кадра, мкс

WINDOW_SECONDS = 1.0
BUCKET_SECONDS = 0.1
SAMPLE_SECONDS = 0.01     # опрос времени кадра -- 100 Гц
RESOLVE_SECONDS = 0.5     # как часто пересматривать, чьи кадры считаем
RECONNECT_SECONDS = 5.0   # пауза перед повторной попыткой открыть память
STALE_SECONDS = 15.0      # столько тишины -- и переоткрываем отображение


@dataclass
class FpsStats:
    current: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    @property
    def valid(self) -> bool:
        return self.current is not None


def foreground_pid() -> int:
    """PID процесса, которому принадлежит активное окно."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = ctypes.c_ulong(0)
    _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


@dataclass
class AppEntry:
    """Одно приложение, за которым следит RTSS."""
    index: int
    offset: int
    process_id: int
    name: str
    time0: int
    time1: int
    frames: int

    @property
    def average_fps(self) -> float | None:
        span = self.time1 - self.time0
        return (self.frames * 1000.0 / span) if span > 0 else None


class RtssFpsSource:
    """FPS активной игры по данным RTSS."""

    def __init__(self, target_pid: int | None = None) -> None:
        self._target_pid = target_pid
        self._shm: SharedMemory | None = None
        self._entry_size = 0
        self._arr_offset = 0
        self._arr_size = 0
        self.version = 0

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._current_offset: int | None = None
        self._current_pid = 0
        self._current_name = ""
        self._average: float | None = None
        # (момент опроса, время кадра в мкс)
        self._samples: deque[tuple[float, int]] = deque(maxlen=4096)
        self._last_alive = time.perf_counter()
        self._next_attempt = 0.0

        self._connect()          # первая попытка -- сразу, чтобы отчитаться в лог
        self._worker = threading.Thread(target=self._run, daemon=True, name="rtss")
        self._worker.start()

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
        self._entry_size = shm.u32(HDR_APP_ENTRY_SIZE)
        self._arr_offset = shm.u32(HDR_APP_ARR_OFFSET)
        self._arr_size = shm.u32(HDR_APP_ARR_SIZE)
        self._last_alive = now
        return True

    def _disconnect(self) -> None:
        if self._shm is not None:
            self._shm.close()
            self._shm = None
        with self._lock:
            self._current_offset = None
            self._samples.clear()
            self._average = None

    @property
    def connected(self) -> bool:
        return self._shm is not None

    @property
    def version_text(self) -> str:
        return f"{self.version >> 16}.{self.version & 0xFFFF}"

    # -- разбор записей -----------------------------------------------------
    def entries(self) -> list[AppEntry]:
        shm = self._shm
        if shm is None or not self._entry_size:
            return []
        found = []
        for index in range(self._arr_size):
            offset = self._arr_offset + index * self._entry_size
            process_id = shm.u32(offset + APP_PROCESS_ID)
            if not process_id:
                continue
            found.append(AppEntry(
                index=index,
                offset=offset,
                process_id=process_id,
                name=shm.text(offset + APP_NAME, APP_NAME_SIZE),
                time0=shm.u32(offset + APP_TIME0),
                time1=shm.u32(offset + APP_TIME1),
                frames=shm.u32(offset + APP_FRAMES),
            ))
        return found

    def _select(self) -> AppEntry | None:
        """Чьи кадры показываем.

        Сначала процесс активного окна -- так строка отражает то, во что играют.
        Если активное окно RTSS не отслеживает (свернулись в браузер), берём
        последнее обновлявшееся приложение: показания игры остаются на экране,
        а не обнуляются при alt-tab.
        """
        found = self.entries()
        if not found:
            return None
        if self._target_pid is not None:
            for entry in found:
                if entry.process_id == self._target_pid:
                    return entry
            return None

        active = foreground_pid()
        for entry in found:
            if entry.process_id == active:
                return entry
        live = [entry for entry in found if entry.frames]
        return max(live, key=lambda e: e.time1) if live else None

    # -- фоновый опрос ------------------------------------------------------
    def _run(self) -> None:
        next_resolve = 0.0
        while not self._stop.is_set():
            now = time.perf_counter()

            if self._shm is None:
                if not self._connect():
                    self._stop.wait(RECONNECT_SECONDS)
                    continue
                next_resolve = 0.0

            if now >= next_resolve:
                next_resolve = now + RESOLVE_SECONDS
                entry = self._select()
                with self._lock:
                    if entry is None:
                        self._current_offset = None
                        self._current_pid = 0
                        self._current_name = ""
                        self._average = None
                    else:
                        if entry.offset != self._current_offset:
                            self._samples.clear()
                        self._current_offset = entry.offset
                        self._current_pid = entry.process_id
                        self._current_name = entry.name
                        self._average = entry.average_fps
                        if entry.frames:
                            self._last_alive = now

                # Долгая тишина может означать и «никто не играет», и то, что RTSS
                # перезапустился, а мы держим отображение старой, уже мёртвой
                # области. Переоткрыть дёшево, поэтому просто переоткрываем.
                if now - self._last_alive > STALE_SECONDS:
                    self._disconnect()
                    self._last_alive = now
                    continue

            offset = self._current_offset
            if offset is not None and self._shm is not None:
                frame_time = self._shm.u32(offset + APP_FRAME_TIME)
                if frame_time:
                    with self._lock:
                        self._samples.append((now, frame_time))
                        horizon = now - 2.0
                        while self._samples and self._samples[0][0] < horizon:
                            self._samples.popleft()

            self._stop.wait(SAMPLE_SECONDS)

    # -- показания ----------------------------------------------------------
    def stats(self) -> FpsStats:
        with self._lock:
            average = self._average
            if self._current_offset is None:
                return FpsStats()
            now = time.perf_counter()
            start = now - WINDOW_SECONDS
            window = [(t, ft) for t, ft in self._samples if t >= start]

        if not window:
            return FpsStats(current=0.0, minimum=0.0, maximum=0.0)

        # Текущее значение -- собственное окно усреднения RTSS: ровно то, что
        # показывает оверлей Afterburner. Если оно ещё не заполнено, считаем сами.
        if average is None:
            average = 1_000_000.0 / (sum(ft for _, ft in window) / len(window))

        # Минимум и максимум -- по корзинам в 100 мс. По отдельным кадрам они
        # скачут так, что читать невозможно, а по целой секунде теряют просадки.
        bucket_count = round(WINDOW_SECONDS / BUCKET_SECONDS)
        buckets: list[list[int]] = [[] for _ in range(bucket_count)]
        for moment, frame_time in window:
            index = int((moment - start) / BUCKET_SECONDS)
            if 0 <= index < bucket_count:
                buckets[index].append(frame_time)
        rates = [1_000_000.0 * len(values) / sum(values)
                 for values in buckets if values]
        if not rates:
            return FpsStats(current=average, minimum=average, maximum=average)
        return FpsStats(current=average, minimum=min(rates), maximum=max(rates))

    @property
    def application(self) -> str:
        with self._lock:
            return self._current_name

    def close(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._disconnect()


class DemoFpsSource:
    """Правдоподобные псевдослучайные значения -- для проверки внешнего вида."""

    def __init__(self) -> None:
        import random
        self._random = random
        self._value = 120.0

    def stats(self) -> FpsStats:
        drift = self._random.uniform(-6.0, 6.0)
        if self._random.random() < 0.05:
            drift -= self._random.uniform(20.0, 45.0)  # редкая просадка
        self._value = max(20.0, min(300.0, self._value + drift))
        spread = max(2.0, self._value * 0.08)
        return FpsStats(current=self._value,
                        minimum=max(1.0, self._value - spread * self._random.random() * 2),
                        maximum=self._value + spread * self._random.random() * 2)

    @property
    def application(self) -> str:
        return "demo"

    def close(self) -> None:
        pass


class NullFpsSource:
    """Источник-заглушка: FPS не показывается."""

    def stats(self) -> FpsStats:
        return FpsStats()

    @property
    def application(self) -> str:
        return ""

    def close(self) -> None:
        pass


def open_fps_source(mode: str = "auto",
                    target_pid: int | None = None) -> tuple[object, str]:
    """Создать источник FPS. Возвращает (источник, описание для лога)."""
    if mode == "demo":
        return DemoFpsSource(), "FPS: демо-режим"
    if mode == "off":
        return NullFpsSource(), "FPS: выключен"

    source = RtssFpsSource(target_pid)
    if source.connected:
        return source, f"FPS: RTSS {source.version_text}"
    if mode == "rtss":
        source.close()
        raise SharedMemoryMissing(
            "RTSS не запущен: общей памяти RTSSSharedMemoryV2 нет")
    # В режиме auto источник остаётся жить: он сам подключится, когда RTSS
    # запустят -- перезапускать монитор ради этого не придётся.
    return source, "FPS: RTSS не запущен, ждём подключения"
