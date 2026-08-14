"""Точка сборки: рабочий поток сбора метрик, оверлей и значок в трее."""

from __future__ import annotations

import argparse
import ctypes
import signal
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .overlay import OverlayWidget
from .rtss import FpsStats, open_fps_source
from .sources import MetricsCollector, Snapshot


class MetricsWorker(QObject):
    """Опрашивает датчики в фоновом потоке и отдаёт снимки в UI-поток."""

    sample = Signal(object, object)  # Snapshot, FpsStats

    def __init__(self, collector: MetricsCollector, fps_source,
                 interval: float) -> None:
        super().__init__()
        self._collector = collector
        self._fps_source = fps_source
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="metrics-worker")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        next_tick = time.perf_counter()
        while not self._stop.is_set():
            try:
                snapshot = self._collector.read()
            except Exception:
                snapshot = Snapshot()
            try:
                fps = self._fps_source.stats()
            except Exception:
                fps = FpsStats()
            # Сигнал уходит в UI-поток через очередь событий Qt
            self.sample.emit(snapshot, fps)

            next_tick += self._interval
            delay = next_tick - time.perf_counter()
            if delay < 0:                       # отстали -- не копим долг
                next_tick = time.perf_counter()
                delay = 0.0
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


def _tray_icon() -> QIcon:
    """Мини-иконка: три столбика, как на графике загрузки."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.fillRect(0, 0, 32, 32, QColor(20, 20, 20, 200))
    painter.fillRect(6, 18, 5, 9, QColor(98, 194, 255))
    painter.fillRect(14, 10, 5, 17, QColor(76, 200, 90))
    painter.fillRect(22, 5, 5, 22, QColor(255, 200, 61))
    painter.end()
    return QIcon(pixmap)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="performance-monitor",
        description="Оверлей мониторинга производительности в стиле Steam. "
                    "FPS берётся из RivaTuner Statistics Server, показания "
                    "железа -- из общей памяти MSI Afterburner.")
    parser.add_argument("--fps", default="auto",
                        choices=("auto", "rtss", "demo", "off"),
                        help="источник FPS (по умолчанию auto: RTSS, если запущен)")
    parser.add_argument("--fps-pid", type=int, metavar="PID",
                        help="считать FPS только этого процесса вместо активного окна")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="масштаб оверлея (по умолчанию 1.0)")
    parser.add_argument("--x", type=int, default=8, help="отступ слева, px")
    parser.add_argument("--y", type=int, default=8, help="отступ сверху, px")
    parser.add_argument("--monitor", type=int, default=0,
                        help="номер монитора (0 -- основной)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="период обновления в секундах (по умолчанию 1.0)")
    parser.add_argument("--no-click-through", action="store_true",
                        help="не делать окно прозрачным для мыши")
    parser.add_argument("--no-tray", action="store_true",
                        help="не создавать значок в трее")
    parser.add_argument("--priority", default="normal",
                        choices=("normal", "below", "idle"),
                        help="приоритет процесса: below/idle уступают ЦП игре")
    parser.add_argument("--list-sensors", action="store_true",
                        help="напечатать все датчики Afterburner и выйти "
                             "(для разбора, чего не хватает на конкретной машине)")
    return parser


def _list_sensors() -> int:
    """Выгрузка всех источников Afterburner -- диагностика чужой машины."""
    from .afterburner import open_afterburner
    from .sources import logical_processor_count

    hardware, note = open_afterburner()
    print(note)
    if not hardware.connected:
        return 1
    print(f"логических процессоров в системе: {logical_processor_count()}")
    print(f"ядер отдаёт Afterburner: {hardware.core_count}")
    print(f"{'источник':<28} {'значение':>12}  {'ед.':<6} ГП")
    for name, value, units, gpu in hardware.list_sensors():
        print(f"{name:<28} {value:12.2f}  {units:<6} {gpu}")
    hardware.close()
    return 0


def _force_utf8(stream) -> None:
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _apply_priority(level: str) -> None:
    """Понизить класс приоритета процесса, чтобы уступать ЦП игре."""
    classes = {"below": 0x00004000,   # BELOW_NORMAL_PRIORITY_CLASS
               "idle": 0x00000040}    # IDLE_PRIORITY_CLASS
    value = classes.get(level)
    if value is None:
        return
    kernel32 = ctypes.WinDLL("kernel32.dll")
    if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), value):
        print("Не удалось понизить приоритет процесса.", file=sys.stderr)


def run(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)

    if sys.platform != "win32":
        print("Монитор рассчитан на Windows: RTSS и MSI Afterburner публикуют "
              "данные через общую память Windows.", file=sys.stderr)
        return 2

    # Консоль или файл могут быть в cp866 -- там нет ни «°», ни «│»
    _force_utf8(sys.stdout)
    _force_utf8(sys.stderr)

    if args.list_sensors:
        return _list_sensors()

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)

    try:
        fps_source, fps_note = open_fps_source(args.fps, args.fps_pid)
    except Exception as exc:
        print(f"Источник FPS недоступен: {exc}", file=sys.stderr)
        print("Подсказка: запустите RivaTuner Statistics Server (он ставится "
              "вместе с MSI Afterburner) либо используйте --fps demo / --fps off.",
              file=sys.stderr)
        return 1

    collector = MetricsCollector()
    _apply_priority(args.priority)

    print("Источники данных:")
    for note in (fps_note, *collector.notes):
        print(f"  * {note}")

    gaps = collector.hardware.missing_sources()
    if not getattr(fps_source, "connected", True):
        gaps.insert(0, "RivaTuner Statistics Server не запущен -- FPS не будет")
    if gaps:
        print("\nНе хватает данных -- эти поля покажут «--»:", file=sys.stderr)
        for gap in gaps:
            print(f"  ! {gap}", file=sys.stderr)
        print("Включаются в MSI Afterburner: Settings -> Monitoring, "
              "галочка у нужного графика.\n", file=sys.stderr)

    overlay = OverlayWidget(scale=args.scale,
                            click_through=not args.no_click_through)
    screens = app.screens()
    screen = screens[args.monitor] if 0 <= args.monitor < len(screens) else app.primaryScreen()
    geometry = screen.geometry()
    overlay.show()
    overlay.move(geometry.x() + args.x, geometry.y() + args.y)
    overlay.apply_native_flags()
    overlay.raise_topmost()

    worker = MetricsWorker(collector, fps_source, max(0.05, args.interval))
    worker.sample.connect(overlay.update_data, Qt.ConnectionType.QueuedConnection)
    worker.start()

    # Полноэкранные приложения периодически «выталкивают» оверлей вниз по Z-порядку
    topmost_timer = QTimer()
    topmost_timer.timeout.connect(overlay.raise_topmost)
    topmost_timer.start(2000)

    tray: QSystemTrayIcon | None = None
    if not args.no_tray and QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(_tray_icon())
        tray.setToolTip("Performance Monitor")
        menu = QMenu()

        toggle = QAction("Скрыть оверлей")

        def on_toggle() -> None:
            visible = overlay.isVisible()
            overlay.setVisible(not visible)
            toggle.setText("Скрыть оверлей" if not visible else "Показать оверлей")
            if not visible:
                overlay.apply_native_flags()
                overlay.raise_topmost()

        toggle.triggered.connect(on_toggle)
        quit_action = QAction("Выход")
        quit_action.triggered.connect(app.quit)
        menu.addAction(toggle)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.show()

    # Ctrl+C в консоли: даём интерпретатору шанс обработать сигнал
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(200)

    try:
        code = app.exec()
    finally:
        worker.stop()
        try:
            fps_source.close()
        except Exception:
            pass
        collector.close()
        if tray is not None:
            tray.hide()
    return code
