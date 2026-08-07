"""Измерение FPS активного приложения.

Основной источник -- **PresentMon** (Intel): консольная утилита, которая через ETW
слушает вызовы Present() всех процессов. Это тот же механизм, на котором построены
оверлеи Steam, RTSS и Windows Game Bar, и единственный способ узнать FPS чужой игры
без внедрения в её процесс.

PresentMon скачивается отдельно: https://github.com/GameTechDev/PresentMon/releases
Положите ``PresentMon.exe`` рядом с проектом (или в ``tools\\``) и запустите
монитор от администратора -- ETW-сессия требует прав.

Если PresentMon недоступен, FPS показывается как ``--``; остальные метрики работают.
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32.dll")
_shell32 = ctypes.WinDLL("shell32.dll")

CREATE_NO_WINDOW = 0x08000000

# Окно усреднения для «текущего» FPS и корзина для поиска минимума/максимума
WINDOW_SECONDS = 1.0
BUCKET_SECONDS = 0.1


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


def is_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_presentmon(explicit: str | None = None) -> str | None:
    """Найти PresentMon.exe: аргумент -> переменная окружения -> проект -> PATH."""
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)

    env = os.environ.get("PRESENTMON")
    if env and os.path.isfile(env):
        return os.path.abspath(env)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for directory in (root, os.path.join(root, "tools"), os.getcwd()):
        if not os.path.isdir(directory):
            continue
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in sorted(entries):
            low = entry.lower()
            if low.startswith("presentmon") and low.endswith(".exe"):
                return os.path.join(directory, entry)

    found = shutil.which("PresentMon")
    return os.path.abspath(found) if found else None


class PresentMonFpsSource:
    """Запускает PresentMon и считает FPS по потоку событий Present().

    Нам нужны только моменты вызовов Present(), поэтому отслеживание доведения
    кадра до экрана, ввода и работы ГП отключается: это заметно уменьшает поток
    ETW-событий, а значит и процессорное время, которое PresentMon отнимает
    у самой игры. Плюс отдельное имя сессии, чтобы не конфликтовать с оверлеями
    Steam / Game Bar, которые тоже держат ETW-сессию.
    """

    # Наборы аргументов от самого экономного к самому совместимому:
    # сначала PresentMon 2.x, затем 1.x (там однодефисные ключи).
    ARG_SETS: Sequence[list[str]] = (
        ["--output_stdout", "--stop_existing_session", "--no_console_stats",
         "--session_name", "PerfMonOverlay",
         "--no_track_display", "--no_track_input", "--no_track_gpu"],
        ["--output_stdout", "--stop_existing_session", "--no_console_stats"],
        ["--output_stdout", "--stop_existing_session"],
        ["-output_stdout", "-stop_existing_session", "-no_top",
         "-no_track_display", "-no_track_input"],
        ["-output_stdout", "-stop_existing_session", "-no_top"],
        ["-output_stdout", "-stop_existing_session"],
    )

    def __init__(self, exe_path: str | None = None,
                 target_pid: int | None = None) -> None:
        path = find_presentmon(exe_path)
        if path is None:
            raise RuntimeError("PresentMon.exe не найден")
        self.exe_path = path
        self._target_pid = target_pid       # None -> следим за активным окном
        self._pid = target_pid or 0
        # Сравниваем PID как строку: так на каждую строку CSV не тратится int()
        self._pid_text = str(self._pid)
        # Только метки времени и только текущего процесса: при смене активного
        # окна очередь сбрасывается, так что фильтр по PID делается на входе.
        self._samples: deque[float] = deque(maxlen=4096)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._latest_t = 0.0                # последняя метка времени из CSV
        self._latest_arrival = time.perf_counter()
        # Последние строки stderr PresentMon: именно там он объясняет, почему
        # не смог стартовать («access denied» и т. п.)
        self._stderr_tail: deque[str] = deque(maxlen=12)
        self._strip_nulls = False
        self.error: str | None = None

        self._proc = self._spawn()
        if self._proc is None:
            raise RuntimeError(self._failure_reason())

        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="presentmon-reader")
        self._reader.start()
        if self._target_pid is None:
            self._watcher = threading.Thread(target=self._watch_foreground, daemon=True,
                                             name="fps-foreground")
            self._watcher.start()

    # -- запуск -------------------------------------------------------------
    def _failure_reason(self) -> str:
        """Внятное сообщение о том, почему PresentMon не поднялся."""
        if self.error:
            return f"PresentMon не запустился: {self.error}"
        detail = " ".join(line for line in self._stderr_tail
                          if line.lower().startswith("error")) or " ".join(self._stderr_tail)
        if detail:
            return f"PresentMon не запустился: {detail.strip()}"
        if not is_admin():
            return ("PresentMon не запустился: нужен запуск от администратора "
                    "(ETW-сессия) или членство в группе «Performance Log Users»")
        return "PresentMon не запустился и ничего не сообщил"

    def _drain_stderr(self, pipe) -> None:
        for raw in iter(pipe.readline, b""):
            text = raw.decode("utf-8", "replace").strip()
            if text:
                self._stderr_tail.append(text)
        pipe.close()

    def _spawn(self) -> subprocess.Popen | None:
        for args in self.ARG_SETS:
            if self._target_pid is not None:
                # Пусть PresentMon сам отфильтрует процесс -- дешевле, чем
                # прогонять через Python строки всех остальных приложений
                prefix = "--" if args[0].startswith("--") else "-"
                args = [*args, f"{prefix}process_id", str(self._target_pid)]
            try:
                proc = subprocess.Popen(
                    [self.exe_path, *args],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            except OSError as exc:
                self.error = str(exc)
                return None

            # Постоянно вычитываем stderr: иначе он рано или поздно переполнится
            # и заблокирует PresentMon, а так заодно видно причину отказа.
            threading.Thread(target=self._drain_stderr, args=(proc.stderr,),
                             daemon=True, name="presentmon-stderr").start()

            stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
            header = self._await_header(proc, stream, timeout=8.0)
            if header is not None:
                self._stream = stream
                self._configure_columns(header)
                return proc

            self._kill(proc)
        return None

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except Exception:
            pass

    def _await_header(self, proc: subprocess.Popen, stream: io.TextIOWrapper,
                      timeout: float) -> list[str] | None:
        """Дождаться строки-заголовка CSV; None -- если процесс умер или молчит.

        ``readline()`` на трубе блокируется до конца строки, и проверка дедлайна
        между вызовами тут не спасает: PresentMon может запуститься и молчать
        (например, когда ETW-сессия зависла). Поэтому процесс убивается по
        сторожевому таймеру -- труба закрывается, и readline возвращает пустую
        строку.
        """
        watchdog = threading.Timer(timeout, self._kill, args=(proc,))
        watchdog.daemon = True
        watchdog.start()
        try:
            while True:
                line = stream.readline()
                if not line:
                    return None
                if "ProcessID" in line:
                    return next(csv.reader([line.strip()]))
                # При перенаправлении в файл PresentMon переключается в широкий
                # режим CRT и пишет UTF-16LE. В трубу он этого не делает, но
                # если вдруг сделает -- выкидываем нулевые байты и работаем дальше.
                if "\x00" in line:
                    stripped = line.replace("\x00", "")
                    if "ProcessID" in stripped:
                        self._strip_nulls = True
                        return next(csv.reader([stripped.strip()]))
        finally:
            watchdog.cancel()

    # Колонка с меткой времени называлась по-разному и мерялась в разных
    # единицах: 1.x -- TimeInSeconds, ранние 2.x -- CPUStartTime, 2.5 -- TimeInMs
    # и CPUStartTimeInMs. Порядок здесь -- порядок предпочтения.
    TIME_COLUMNS = (
        ("TimeInMs", 0.001),
        ("CPUStartTimeInMs", 0.001),
        ("TimeInSeconds", 1.0),
        ("CPUStartTime", 1.0),
    )

    def _configure_columns(self, header: list[str]) -> None:
        index = {name.strip(): position for position, name in enumerate(header)}
        self._col_pid = index.get("ProcessID")
        self._col_time = None
        self._time_scale = 1.0
        for name, scale in self.TIME_COLUMNS:
            if name in index:
                self._col_time = index[name]
                self._time_scale = scale
                break
        self._col_dropped = index.get("Dropped")

    # -- фоновые потоки -----------------------------------------------------
    def _watch_foreground(self) -> None:
        while not self._stop.wait(0.5):
            pid = foreground_pid()
            if pid and pid != self._pid:
                with self._lock:
                    self._samples.clear()
                    self._pid = pid
                    self._pid_text = str(pid)

    def _read_loop(self) -> None:
        # Горячий цикл: на 240 FPS это сотни строк в секунду, поэтому всё, что
        # можно, вынесено в локальные переменные, а строки чужих процессов
        # отсеиваются до разбора чисел.
        col_pid = self._col_pid
        col_time = self._col_time
        col_dropped = self._col_dropped
        scale = self._time_scale
        if col_pid is None:
            return
        min_columns = max(index for index in (col_pid, col_time, col_dropped)
                          if index is not None) + 1

        samples = self._samples
        append = samples.append
        lock = self._lock
        clock = time.perf_counter
        stopped = self._stop.is_set

        source = self._stream
        if self._strip_nulls:
            source = (line.replace("\x00", "") for line in self._stream)

        for row in csv.reader(source):
            if len(row) < min_columns:
                continue
            if row[col_pid] != self._pid_text:
                continue
            if col_dropped is not None and row[col_dropped][:1] in ("1", "T"):
                continue

            now = clock()
            if col_time is not None:
                try:
                    timestamp = float(row[col_time]) * scale
                except ValueError:
                    continue  # «NA» -- шкалы смешивать нельзя, строку пропускаем
            else:
                # Формат незнакомый: берём время получения строки. Само по себе
                # это согласовано, просто чуть менее точно.
                timestamp = now

            with lock:
                if timestamp >= self._latest_t:
                    self._latest_t = timestamp
                    self._latest_arrival = now
                append(timestamp)
                # Чистим прямо здесь, чтобы stats() оставался линейным и коротким
                horizon = timestamp - 4.0
                while samples and samples[0] < horizon:
                    samples.popleft()

            if stopped():
                break

    # -- расчёт -------------------------------------------------------------
    def stats(self) -> FpsStats:
        pid = self._pid
        if not pid:
            return FpsStats()

        with self._lock:
            # «Сейчас» в шкале PresentMon: последняя метка + время с её получения.
            # Так показания не застывают, когда игра перестала выводить кадры.
            now = self._latest_t + (time.perf_counter() - self._latest_arrival)
            start = now - WINDOW_SECONDS
            times = [value for value in self._samples if value >= start]

        if not times:
            return FpsStats(current=0.0, minimum=0.0, maximum=0.0)

        current = len(times) / WINDOW_SECONDS

        # Минимум/максимум за секунду -- по корзинам в 100 мс: заметно устойчивее,
        # чем 1/кадровое_время, но всё ещё ловит просадки.
        bucket_count = round(WINDOW_SECONDS / BUCKET_SECONDS)
        buckets = [0] * bucket_count
        for value in times:
            index = int((value - start) / BUCKET_SECONDS)
            if 0 <= index < bucket_count:
                buckets[index] += 1
        rates = [count / BUCKET_SECONDS for count in buckets]
        # Крайние корзины могут быть неполными -- отбрасываем последнюю
        if len(rates) > 2:
            rates = rates[:-1]
        return FpsStats(current=current, minimum=min(rates), maximum=max(rates))

    def close(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None


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

    def close(self) -> None:
        pass


class NullFpsSource:
    """Источник-заглушка: FPS недоступен."""

    def stats(self) -> FpsStats:
        return FpsStats()

    def close(self) -> None:
        pass


def open_fps_source(mode: str = "auto", exe_path: str | None = None,
                    target_pid: int | None = None) -> tuple[object, str]:
    """Создать источник FPS. Возвращает (источник, описание для лога)."""
    if mode == "demo":
        return DemoFpsSource(), "FPS: демо-режим"
    if mode == "off":
        return NullFpsSource(), "FPS: выключен"
    try:
        source = PresentMonFpsSource(exe_path, target_pid)
        return source, f"FPS: PresentMon ({source.exe_path})"
    except Exception as exc:
        if mode == "presentmon":
            raise
        return NullFpsSource(), f"FPS: недоступен -- {exc}"
