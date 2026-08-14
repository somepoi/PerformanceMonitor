"""Оверлей: одна строка статистики в верхнем левом углу экрана, как в Steam."""

from __future__ import annotations

import ctypes
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .rtss import FpsStats
from .sources import Snapshot

# --- палитра ---------------------------------------------------------------
BG = QColor(0, 0, 0, 165)
C_FPS = QColor(255, 200, 61)     # янтарный
C_CPU = QColor(98, 194, 255)     # голубой
C_GPU = QColor(124, 227, 139)    # зелёный
C_RAM = QColor(212, 142, 255)    # сиреневый
C_VALUE = QColor(240, 240, 240)
C_UNIT = QColor(165, 165, 165)
C_MARK = QColor(150, 150, 150)   # стрелочки ↑ ↓
C_SEP = QColor(110, 110, 110)
C_GRAPH_BG = QColor(255, 255, 255, 22)
C_GRAPH_LINE = QColor(255, 82, 82)     # красная -- текущий FPS
C_GRAPH_AVG = QColor(76, 200, 90)      # зелёная -- среднее за 90 с
C_CORE = QColor(76, 200, 90)
C_CORE_IDLE = QColor(76, 200, 90, 120)   # основание простаивающего ядра
C_CORE_BG = QColor(255, 255, 255, 18)

DASH = "--"
ARROW_UP = "↑"
ARROW_DOWN = "↓"
SEPARATOR = "│"

FPS_HISTORY = 100   # точек в графике (= 100 с при обновлении раз в секунду)
FPS_AVG_WINDOW = 90  # окно зелёной линии, с


@dataclass
class _Cursor:
    """Курсор отрисовки: painter=None -> режим обмера ширины."""
    x: float
    painter: QPainter | None = None


class OverlayWidget(QWidget):
    def __init__(self, scale: float = 1.0, click_through: bool = True) -> None:
        super().__init__(None)
        self.scale_factor = scale

        s = scale
        self.H = round(30 * s)
        self.PAD = round(9 * s)
        self.GAP = round(7 * s)
        self.GAP_S = round(4 * s)
        self.GRAPH_W = round(100 * s)
        self.CORE_STEP = max(2, round(3 * s))

        # Вертикальные полосы: крупные значения по центру, единицы -- у верхнего края
        self.BAND_VALUE = (round(7 * s), self.H - round(9 * s))
        self.BAND_UNIT = (round(1 * s), round(11 * s))
        self.BAND_GRAPH = (round(5 * s), self.H - round(9 * s))
        self.BAND_TOP = (round(2 * s), round(11 * s))
        self.BAND_BOTTOM = (self.H - round(13 * s), round(11 * s))

        self.font_value = QFont("Consolas")
        self.font_value.setPixelSize(max(9, round(14 * s)))
        self.font_value.setBold(True)
        self.font_value.setStyleHint(QFont.StyleHint.Monospace)

        self.font_small = QFont("Consolas")
        self.font_small.setPixelSize(max(7, round(9 * s)))
        self.font_small.setBold(True)
        self.font_small.setStyleHint(QFont.StyleHint.Monospace)

        self.fm_value = QFontMetricsF(self.font_value)
        self.fm_small = QFontMetricsF(self.font_small)
        self.cell_value = self.fm_value.horizontalAdvance("0")
        self.cell_small = self.fm_small.horizontalAdvance("0")

        self._fps = FpsStats()
        self._snapshot = Snapshot()
        self._history: deque[float] = deque(maxlen=FPS_HISTORY)

        # Перья и контур фона создаются один раз: конструирование QPen/QPainterPath
        # на каждый кадр -- самая частая причина «дорогого» paintEvent в Qt.
        self._pen_avg = QPen(C_GRAPH_AVG, max(1.0, 1.0 * s))
        self._pen_line = QPen(C_GRAPH_LINE, max(1.0, 1.2 * s))
        self._radius = 4.0 * s
        self._bg_path: QPainterPath | None = None
        # Ширина строки зависит только от числа ядер: остальные поля занимают
        # фиксированное число знакомест. Пересчитываем, лишь когда оно меняется.
        self._shape_key: int | None = None

        flags = (Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool
                 | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        if click_through:
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if click_through:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setWindowTitle("Performance Monitor Overlay")
        self.setFixedHeight(self.H)
        self.resize(round(900 * s), self.H)

    # -- обновление данных --------------------------------------------------
    def update_data(self, snapshot: Snapshot, fps: FpsStats) -> None:
        self._snapshot = snapshot
        self._fps = fps
        if fps.current is not None:
            self._history.append(float(fps.current))

        shape = len(snapshot.cpu.per_core)
        if shape != self._shape_key:
            self._shape_key = shape
            width = round(self._render(None))
            if width != self.width():
                self.resize(width, self.H)
        self.update()

    # -- служебные примитивы отрисовки --------------------------------------
    def _band(self, band, x: float, width: float) -> QRectF:
        return QRectF(x, band[0], width, band[1])

    def _draw_text(self, cursor: _Cursor, text: str, font: QFont, metrics: QFontMetricsF,
                   color: QColor, band, cells: int | None = None,
                   align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignRight) -> None:
        cell = self.cell_value if font is self.font_value else self.cell_small
        width = cell * cells if cells is not None else metrics.horizontalAdvance(text)
        if cursor.painter is not None:
            cursor.painter.setFont(font)
            cursor.painter.setPen(color)
            cursor.painter.drawText(self._band(band, cursor.x, width),
                                    align | Qt.AlignmentFlag.AlignVCenter, text)
        cursor.x += width

    def _value(self, cursor: _Cursor, text: str, cells: int,
               color: QColor = C_VALUE) -> None:
        self._draw_text(cursor, text, self.font_value, self.fm_value, color,
                        self.BAND_VALUE, cells)

    def _label(self, cursor: _Cursor, text: str, color: QColor) -> None:
        self._draw_text(cursor, text, self.font_value, self.fm_value, color,
                        self.BAND_VALUE, None, Qt.AlignmentFlag.AlignLeft)

    def _unit(self, cursor: _Cursor, text: str) -> None:
        """Единица измерения: мелко и прижата к верхнему краю строки."""
        self._draw_text(cursor, text, self.font_small, self.fm_small, C_UNIT,
                        self.BAND_UNIT, None, Qt.AlignmentFlag.AlignLeft)

    def _marked(self, cursor: _Cursor, mark: str, text: str, cells: int) -> None:
        """Стрелочка вплотную к своему значению.

        Числовые поля занимают фиксированное число знакомест, иначе строка
        дёргалась бы при смене разрядности. Но если прижимать вправо только
        число, свободное знакоместо оказывается между стрелкой и числом:
        ``46 ↑ 66`` против ``46 ↑221``. Поэтому вправо прижимается пара
        целиком, а запас уходит влево, где и так стоит отступ.
        """
        mark_width = self.fm_small.horizontalAdvance(mark)
        total = self.cell_value * cells + mark_width
        painter = cursor.painter
        if painter is not None:
            right = cursor.x + total
            number_width = self.cell_value * len(text)
            painter.setFont(self.font_value)
            painter.setPen(C_VALUE)
            painter.drawText(self._band(self.BAND_VALUE, right - number_width,
                                        number_width),
                             Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter, text)
            painter.setFont(self.font_small)
            painter.setPen(C_MARK)
            painter.drawText(
                self._band(self.BAND_VALUE, right - number_width - mark_width,
                           mark_width),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, mark)
        cursor.x += total

    def _separator(self, cursor: _Cursor) -> None:
        cursor.x += self.GAP
        self._draw_text(cursor, SEPARATOR, self.font_value, self.fm_value, C_SEP,
                        self.BAND_VALUE, None, Qt.AlignmentFlag.AlignHCenter)
        cursor.x += self.GAP

    # -- графики ------------------------------------------------------------
    def _draw_fps_graph(self, cursor: _Cursor) -> None:
        width = self.GRAPH_W
        top, height = self.BAND_GRAPH
        rect = QRectF(cursor.x, top, width, height)
        history = list(self._history)

        low = high = None
        if history:
            low, high = min(history), max(history)
            span = high - low
            if span < 10.0:                    # не даём шуму растянуться на весь график
                extra = (10.0 - span) / 2.0
                low -= extra
                high += extra
            else:
                pad = span * 0.08
                low -= pad
                high += pad

        painter = cursor.painter
        if painter is not None:
            painter.fillRect(rect, C_GRAPH_BG)
            if history and high is not None and high > low:
                def y_of(value: float) -> float:
                    ratio = (value - low) / (high - low)
                    return rect.bottom() - ratio * rect.height()

                step = width / max(1, FPS_HISTORY - 1)
                offset = len(history) - 1
                points = [QPointF(rect.right() - (offset - i) * step, y_of(v))
                          for i, v in enumerate(history)]

                # Зелёная линия -- среднее за последние 90 с
                window = history[-FPS_AVG_WINDOW:]
                average = sum(window) / len(window)
                painter.setPen(self._pen_avg)
                y_avg = y_of(average)
                painter.drawLine(QPointF(rect.left(), y_avg),
                                 QPointF(rect.right(), y_avg))

                # Красная линия -- текущие значения
                if len(points) >= 2:
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setPen(self._pen_line)
                    painter.drawPolyline(QPolygonF(points))
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        cursor.x += width

        # Минимум и максимум графика -- правее него, столбиком
        cursor.x += self.GAP_S
        text_max = f"{max(history):.0f}" if history else DASH
        text_min = f"{min(history):.0f}" if history else DASH
        label_width = self.cell_small * 4
        if painter is not None:
            painter.setFont(self.font_small)
            painter.setPen(C_UNIT)
            align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            painter.drawText(self._band(self.BAND_TOP, cursor.x, label_width),
                             align, text_max)
            painter.drawText(self._band(self.BAND_BOTTOM, cursor.x, label_width),
                             align, text_min)
        cursor.x += label_width

    def _draw_core_graph(self, cursor: _Cursor) -> None:
        cores: list[float] = self._snapshot.cpu.per_core or []
        count = max(1, len(cores))
        width = count * self.CORE_STEP
        top, height = self.BAND_GRAPH
        rect = QRectF(cursor.x, top, width, height)

        painter = cursor.painter
        if painter is not None:
            painter.fillRect(rect, C_CORE_BG)
            bar_width = max(1.0, self.CORE_STEP - 1.0)
            # Столбик простаивающего ядра короче пикселя и не рисуется вовсе,
            # из-за чего график читается как «ядер всего четыре». Поэтому у
            # каждого ядра есть основание в один пиксель: количество ядер видно
            # всегда, а разница между простоем и нагрузкой -- по высоте.
            floor_height = max(1.0, round(self.scale_factor))
            for index, load in enumerate(cores):
                ratio = max(0.0, min(1.0, load / 100.0))
                bar_height = max(floor_height, ratio * rect.height())
                bar = QRectF(rect.left() + index * self.CORE_STEP,
                             rect.bottom() - bar_height, bar_width, bar_height)
                painter.fillRect(bar, C_CORE_IDLE if ratio * rect.height()
                                 < floor_height else C_CORE)
        cursor.x += width

    # -- сборка строки ------------------------------------------------------
    def _render(self, painter: QPainter | None) -> float:
        cursor = _Cursor(x=float(self.PAD), painter=painter)
        cpu = self._snapshot.cpu
        gpu = self._snapshot.gpu
        ram = self._snapshot.ram
        fps = self._fps

        def number(value: float | None, digits: int = 0) -> str:
            return DASH if value is None else f"{value:.{digits}f}"

        # Внутри одной величины -- узкий отступ, между разными величинами --
        # широкий: так глаз сразу видит, что «46 ↑66 %» это одно, а «4.89 ↑4.90
        # ГГц» -- другое.
        tight, wide = self.GAP_S, self.GAP

        # --- FPS ---
        self._label(cursor, "FPS", C_FPS)
        cursor.x += tight
        self._value(cursor, number(fps.current), 3)
        cursor.x += tight
        self._marked(cursor, ARROW_DOWN, number(fps.minimum), 3)
        cursor.x += tight
        self._marked(cursor, ARROW_UP, number(fps.maximum), 3)

        self._separator(cursor)
        self._draw_fps_graph(cursor)
        self._separator(cursor)

        # --- ЦП ---
        self._label(cursor, "CPU", C_CPU)
        cursor.x += tight
        self._value(cursor, number(cpu.load_avg), 3)
        cursor.x += tight
        self._marked(cursor, ARROW_UP, number(cpu.load_max), 3)
        self._unit(cursor, "%")
        cursor.x += wide
        self._value(cursor, number(cpu.clock_avg, 2), 4)
        cursor.x += tight
        self._marked(cursor, ARROW_UP, number(cpu.clock_max, 2), 4)
        self._unit(cursor, "ГГц")  # ГГц
        cursor.x += wide

        self._draw_core_graph(cursor)
        cursor.x += wide

        self._value(cursor, number(cpu.temp), 3)
        self._unit(cursor, "°C")

        self._separator(cursor)

        # --- ГП ---
        self._label(cursor, "GPU", C_GPU)
        cursor.x += tight
        self._value(cursor, number(gpu.load), 3)
        self._unit(cursor, "%")
        cursor.x += wide
        self._value(cursor, number(gpu.temp), 3)
        self._unit(cursor, "°C")
        cursor.x += wide
        self._value(cursor, number(gpu.vram_used, 1), 4)
        self._draw_text(cursor, "/", self.font_value, self.fm_value, C_UNIT,
                        self.BAND_VALUE, None, Qt.AlignmentFlag.AlignHCenter)
        self._value(cursor, number(gpu.vram_total, 1), 4)
        self._unit(cursor, "ГБ")  # ГБ

        self._separator(cursor)

        # --- ОЗУ ---
        self._label(cursor, "RAM", C_RAM)
        cursor.x += tight
        self._value(cursor, number(ram.used, 1), 4)
        self._draw_text(cursor, "/", self.font_value, self.fm_value, C_UNIT,
                        self.BAND_VALUE, None, Qt.AlignmentFlag.AlignHCenter)
        self._value(cursor, number(ram.total, 1), 4)
        self._unit(cursor, "ГБ")

        return cursor.x + self.PAD

    # -- Qt -----------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        self._bg_path = None
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if self._bg_path is None:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, self.width(), self.height()),
                                self._radius, self._radius)
            self._bg_path = path
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillPath(self._bg_path, BG)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        self._render(painter)
        painter.end()

    def apply_native_flags(self) -> None:
        """Дополнительные стили окна Windows: не активируется, не в панели задач."""
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        GWL_EXSTYLE = -20
        user32 = ctypes.WinDLL("user32.dll")
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_ssize_t]
        hwnd = ctypes.c_void_p(int(self.winId()))
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    def raise_topmost(self) -> None:
        """Вернуть окно поверх всех: полноэкранные игры любят перекрывать оверлеи."""
        HWND_TOPMOST = -1
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
        user32 = ctypes.WinDLL("user32.dll")
        user32.SetWindowPos(ctypes.c_void_p(int(self.winId())),
                            ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
