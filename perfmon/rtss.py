"""FPS из RivaTuner Statistics Server.

RTSS перехватывает вызовы представления кадра внутри самой игры и публикует
результат в общей памяти ``RTSSSharedMemoryV2``. Это тот же источник, из
которого рисует свой оверлей MSI Afterburner, так что цифры сходятся с
привычными один в один.

Формат: заголовок из девяти DWORD, затем массив записей приложений. Размер
записи берётся из заголовка (``dwAppEntrySize``), поэтому читать по фиксированным
смещениям можно только начало структуры -- зато оно неизменно во всех версиях
2.x, и всё нужное лежит именно там.

Считается FPS по окну усреднения самого RTSS: ``dwFrames`` кадров за
``dwTime1 - dwTime0`` миллисекунд. Это единственные данные, на которые можно
опираться:

* ``dwFrameTime`` -- НЕ интервал между кадрами. Замер на Cyberpunk 2077 при
  эталонных 228 FPS (то есть 4380 мкс на кадр) даёт 270-900 мкс: это время
  внутри вызова Present, а не время кадра. По нему FPS завышается в разы;
* ``dwStatFramerateMin/Avg/Max`` и буфер ``dwStatFrameTimeBuf`` заполняются,
  только когда в RTSS запущен сбор статистики. По умолчанию ``dwStatFlags``
  равен нулю, а буфер пуст.
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

POLL_SECONDS = 0.05       # как часто проверять, не сменилось ли окно RTSS
RESOLVE_SECONDS = 0.5     # как часто пересматривать, чьи кадры считаем
HISTORY_SECONDS = 120.0   # сколько показаний храним
MIN_WINDOW = 1.0          # минимальное окно для минимума/максимума, с
WINDOW_PERIODS = 3        # или столько периодов обновления RTSS -- что больше
MAX_WINDOW = 6.0
RECONNECT_SECONDS = 5.0   # пауза перед повторной попыткой открыть память
STALE_SECONDS = 30.0      # столько тишины -- и переоткрываем отображение


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
        return (self.frames * 1000.0 / span) if span > 0 and self.frames else None


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
        self._current_pid: int | None = None
        self._current_name = ""
        self._last_window: tuple[int, int] | None = None
        # (момент получения, FPS за окно RTSS)
        self._samples: deque[tuple[float, float]] = deque()
        self._period = MIN_WINDOW          # наблюдаемый период обновления RTSS
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
            self._last_window = None
            self._samples.clear()

    @property
    def connected(self) -> bool:
        return self._shm is not None

    @property
    def version_text(self) -> str:
        return f"{self.version >> 16}.{self.version & 0xFFFF}"

    @property
    def update_period(self) -> float:
        """Наблюдаемый период обновления статистики RTSS, с."""
        return self._period

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

        RTSS держит записи и для обычных программ -- мессенджеров, оверлея
        Steam, -- но кадров в них нет. Поэтому среди приложений активного окна
        нас интересуют только те, что действительно выводят кадры; иначе
        показания игры пропадали бы, стоит навести фокус на любое окно рядом.
        """
        live = [entry for entry in self.entries() if entry.average_fps]
        if not live:
            return None
        if self._target_pid is not None:
            return next((e for e in live if e.process_id == self._target_pid), None)

        active = foreground_pid()
        for entry in live:
            if entry.process_id == active:
                return entry
        # Активное окно кадров не выводит -- держим последнюю живую игру, чтобы
        # значения не обнулялись при alt-tab
        return max(live, key=lambda e: e.time1)

    # -- фоновый опрос ------------------------------------------------------
    def _record(self, entry: AppEntry, now: float) -> None:
        """Запомнить показание, если RTSS закрыл новое окно усреднения."""
        window = (entry.time0, entry.time1)
        if window == self._last_window:
            return
        fps = entry.average_fps
        if fps is None:
            return

        with self._lock:
            if self._last_window is not None and self._samples:
                # Период обновления RTSS настраивается пользователем (по
                # умолчанию 1000 мс). Замеряем его сами, чтобы знать, за сколько
                # секунд вообще можно посчитать минимум и максимум.
                gap = now - self._samples[-1][0]
                if 0.02 <= gap <= 5.0:
                    self._period = self._period * 0.7 + gap * 0.3
            self._last_window = window
            self._samples.append((now, fps))
            horizon = now - HISTORY_SECONDS
            while self._samples and self._samples[0][0] < horizon:
                self._samples.popleft()
        self._last_alive = now

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
                if entry is None:
                    with self._lock:
                        self._current_offset = None
                        self._current_pid = None
                        self._current_name = ""
                elif entry.offset != self._current_offset:
                    with self._lock:
                        self._current_offset = entry.offset
                        self._current_pid = entry.process_id
                        self._current_name = entry.name
                        self._last_window = None
                        self._samples.clear()

                # Долгая тишина может означать и «никто не играет», и то, что
                # RTSS перезапустился, а мы держим отображение старой, уже
                # мёртвой области. Переоткрыть дёшево, поэтому переоткрываем.
                if now - self._last_alive > STALE_SECONDS:
                    self._disconnect()
                    self._last_alive = now
                    continue

            offset = self._current_offset
            shm = self._shm
            if offset is not None and shm is not None:
                self._record(AppEntry(
                    index=-1, offset=offset, process_id=0, name="",
                    time0=shm.u32(offset + APP_TIME0),
                    time1=shm.u32(offset + APP_TIME1),
                    frames=shm.u32(offset + APP_FRAMES)), now)

            self._stop.wait(POLL_SECONDS)

    # -- показания ----------------------------------------------------------
    def stats(self) -> FpsStats:
        with self._lock:
            if self._current_offset is None or not self._samples:
                return FpsStats()
            now = time.perf_counter()
            current = self._samples[-1][1]
            # Минимум и максимум -- за последнюю секунду. Но если RTSS обновляет
            # статистику раз в секунду (значение по умолчанию), в это окно
            # попадёт единственное показание. Тогда окно расширяется до трёх
            # периодов обновления -- иначе стрелочки просто дублировали бы
            # текущее значение и ничего не сообщали.
            span = min(MAX_WINDOW, max(MIN_WINDOW, self._period * WINDOW_PERIODS))
            start = now - span
            window = [fps for moment, fps in self._samples if moment >= start]

        if not window:
            return FpsStats(current=current, minimum=current, maximum=current)
        return FpsStats(current=current, minimum=min(window), maximum=max(window))

    @property
    def application(self) -> str:
        with self._lock:
            return self._current_name

    @property
    def in_game(self) -> bool:
        """Игра с показаниями FPS находится на переднем плане."""
        return (self._current_offset is not None
                and self._current_pid == foreground_pid())

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
