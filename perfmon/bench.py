"""Замер стоимости горячих участков: ``pdm run bench``.

Оверлей делит процессор с игрой, поэтому важно знать, сколько он на самом деле
стоит. Скрипт печатает медиану и 95-й процентиль по каждому участку, так что
любое изменение можно проверить, а не додумать.
"""

from __future__ import annotations

import csv
import io
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
    import psutil

    from .sources import MetricsCollector, read_memory

    print("Сбор метрик")
    started = time.perf_counter()
    collector = MetricsCollector()
    elapsed = (time.perf_counter() - started) * 1000.0
    print(f"  {'MetricsCollector() -- старт':<40s} {elapsed:7.1f} мс")
    for note in collector.notes:
        print(f"    {note}")

    try:
        _measure("collector.read() -- весь снимок", collector.read, 30)
        _measure("  ЦП: PDH", collector.cpu.read, 30)
        if collector.gpu_source is not None:
            _measure("  ГП", collector.gpu_source.read, 30)
        _measure("  ОЗУ: GlobalMemoryStatusEx", read_memory, 500)
        _measure("  ОЗУ: psutil.virtual_memory (для сравнения)",
                 psutil.virtual_memory, 100)
    finally:
        collector.close()


def _bench_overlay() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    from .fps import FpsStats
    from .overlay import OverlayWidget
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


def _bench_fps_parsing() -> None:
    from .fps import BUCKET_SECONDS

    header = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,"
              "PresentFlags,AllowsTearing,PresentMode,FrameType,CPUStartTime,FrameTime,"
              "CPUBusy,CPUWait,GPULatency,GPUTime,GPUBusy,VideoBusy,GPUWait,"
              "DisplayLatency,DisplayedTime\n")
    rows = "".join(
        f"game.exe,4242,0x1,DXGI,1,0,1,Hardware,Application,{index * 0.004:.6f},"
        f"4.0,2.0,1.0,3.0,3.5,3.0,0.0,0.5,8.0,4.1\n"
        for index in range(20000))
    blob = header + rows
    target = "4242"

    def parse() -> int:
        stream = io.StringIO(blob)
        reader = csv.reader(stream)
        next(reader)
        out = []
        append = out.append
        for row in reader:
            if row[1] != target:
                continue
            append(float(row[9]))
        return len(out)

    count = parse()
    print("\nРазбор потока PresentMon")
    _measure(f"csv.reader: {count} строк", parse, 20)
    print(f"  корзина минимума/максимума: {BUCKET_SECONDS * 1000:.0f} мс")


def main() -> int:
    if sys.platform != "win32":
        print("Бенчмарк рассчитан на Windows.", file=sys.stderr)
        return 2
    _bench_sources()
    _bench_overlay()
    _bench_fps_parsing()
    print("\nБюджет на тик = сумма «collector.read()» и «update_data()»;"
          " при обновлении раз в секунду это доля процента одного ядра.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
