"""Замер стоимости горячих участков: ``pdm run bench``.

Оверлей делит процессор с игрой, поэтому важно знать, сколько он на самом деле
стоит. Скрипт печатает медиану и 95-й процентиль по каждому участку, так что
любое изменение можно проверить, а не додумать.
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable


def _measure(label: str, fn: Callable[[], object], repeats: int) -> None:
    fn()  # прогрев: первый вызов почти всегда аномальный
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    median = statistics.median(samples)
    p95 = samples[max(0, int(repeats * 0.95) - 1)]
    print(f"  {label:<40s} медиана {median:7.3f} мс   p95 {p95:7.3f} мс")


def _bench_sources() -> None:
    from .afterburner import open_afterburner
    from .sources import MetricsCollector, read_memory

    print("Сбор метрик")
    started = time.perf_counter()
    hardware, note = open_afterburner()
    elapsed = (time.perf_counter() - started) * 1000.0
    print(f"  {'подключение к Afterburner':<40s} {elapsed:7.3f} мс")
    print(f"    {note}")
    if not hardware.connected:
        print("    Afterburner не запущен -- замеры пойдут по пустым данным")

    collector = MetricsCollector(hardware)
    try:
        _measure("collector.read() -- весь снимок", collector.read, 300)
        _measure("  afterburner.read() -- сырые датчики", hardware.read, 300)
        _measure("  GlobalMemoryStatusEx (запасной ОЗУ)", read_memory, 500)
    finally:
        collector.close()


def _bench_fps() -> None:
    from .rtss import RtssFpsSource

    print("\nFPS")
    started = time.perf_counter()
    source = RtssFpsSource()
    elapsed = (time.perf_counter() - started) * 1000.0
    print(f"  {'подключение к RTSS':<40s} {elapsed:7.3f} мс")
    if not source.connected:
        print("    RTSS не запущен -- измеряется только холостой путь")
    else:
        print(f"    RTSS {source.version_text}, "
              f"приложений в списке: {len(source.entries())}")
    try:
        _measure("stats() -- то, что зовётся каждый тик", source.stats, 300)
        _measure("entries() -- полный разбор списка приложений",
                 source.entries, 300)
    finally:
        source.close()


def _bench_overlay() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    from .overlay import OverlayWidget
    from .rtss import FpsStats
    from .sources import CpuStats, GpuStats, RamStats, Snapshot

    widget = OverlayWidget()
    snapshot = Snapshot(
        cpu=CpuStats(46.3, 97.0, 4.76, 5.12, [50.0] * 16, 68.0),
        gpu=GpuStats(87.0, 64.0, 9.4, 15.9),
        ram=RamStats(18.3, 31.6))
    fps = FpsStats(143.0, 118.0, 152.0)
    for _ in range(100):  # наполняем историю графика
        widget.update_data(snapshot, fps)

    print("\nОтрисовка")
    _measure("update_data() -- то, что делается каждый тик",
             lambda: widget.update_data(snapshot, fps), 300)
    _measure("_render(None) -- обмер строки", lambda: widget._render(None), 300)
    _measure("grab() -- полная перерисовка", widget.grab, 300)
    del app


def main() -> int:
    if sys.platform != "win32":
        print("Бенчмарк рассчитан на Windows.", file=sys.stderr)
        return 2
    _bench_sources()
    _bench_fps()
    _bench_overlay()
    print("\nБюджет на тик = сумма «collector.read()», «stats()» и «update_data()»;"
          " при обновлении раз в секунду это доля процента одного ядра.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
