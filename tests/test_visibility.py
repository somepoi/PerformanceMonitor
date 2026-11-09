import os
import threading
import unittest
from ctypes.wintypes import RECT
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from perfmon.app import OverlayVisibility
from perfmon.overlay import OverlayWidget
from perfmon.rtss import RtssFpsSource


class VisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.overlay = OverlayWidget()
        self.addCleanup(self.overlay.close)
        self.native = patch.object(self.overlay, "apply_native_flags").start()
        self.topmost = patch.object(self.overlay, "raise_topmost").start()
        self.addCleanup(patch.stopall)

    def test_manual_hide_survives_samples_and_show_restores_window(self):
        state = OverlayVisibility(self.overlay, "auto")
        state.refresh(fullscreen=False, osd_ready=True)
        self.assertTrue(self.overlay.isVisible())
        state.action.trigger()
        state.refresh(fullscreen=False, osd_ready=True)
        self.assertFalse(self.overlay.isVisible())
        self.assertEqual(state.action.text(), "Показать оверлей")
        state.action.trigger()
        self.assertTrue(self.overlay.isVisible())
        self.assertEqual(state.action.text(), "Скрыть оверлей")

    def test_auto_falls_back_to_window_when_osd_cannot_publish(self):
        state = OverlayVisibility(self.overlay, "auto")
        state.refresh(fullscreen=True, osd_ready=True)
        self.assertFalse(self.overlay.isVisible())
        state.refresh(fullscreen=True, osd_ready=False)
        self.assertTrue(self.overlay.isVisible())
        state.refresh(fullscreen=False, osd_ready=True)
        self.assertTrue(self.overlay.isVisible())

    def test_auto_switches_only_for_fullscreen_game_on_overlay_monitor(self):
        source = object.__new__(RtssFpsSource)
        source._lock = threading.Lock()
        source._current_offset = 100
        source._current_pid = 42
        state = OverlayVisibility(self.overlay, "auto")
        monitor_bounds = (-1920, 0, 0, 1080)

        def monitor_info(_monitor, info):
            info._obj.rcMonitor = RECT(*monitor_bounds)
            return True

        def window_rect(_hwnd, rect):
            rect._obj.left, rect._obj.top, rect._obj.right, rect._obj.bottom = bounds
            return True

        framed = 0x00C00000  # WS_CAPTION
        popup = 0x80000000   # WS_POPUP
        cases = (
            ("maximized", 42, framed, monitor_bounds, 1, True),
            ("windowed", 42, popup, (-1800, 100, -100, 1000), 1, True),
            ("fullscreen", 42, popup, monitor_bounds, 1, False),
            ("other monitor", 42, popup, monitor_bounds, 2, True),
            ("background", 99, popup, monitor_bounds, 1, True),
        )
        with patch("perfmon.rtss._window_pid") as window_pid, patch(
            "perfmon.rtss._user32"
        ) as user32:
            user32.GetForegroundWindow.return_value = 10
            user32.GetMonitorInfoW.side_effect = monitor_info
            user32.GetWindowRect.side_effect = window_rect
            for name, pid, style, bounds, overlay_monitor, visible in cases:
                with self.subTest(name=name):
                    window_pid.return_value = pid
                    user32.GetWindowLongW.return_value = style
                    user32.MonitorFromWindow.side_effect = [1, overlay_monitor]
                    state.refresh(fullscreen=source.is_fullscreen_on(20), osd_ready=True)
                    self.assertEqual(self.overlay.isVisible(), visible)

    def test_display_modes_and_manual_disable(self):
        for mode, visible in (("window", True), ("both", True), ("rtss", False)):
            with self.subTest(mode=mode):
                state = OverlayVisibility(self.overlay, mode)
                state.refresh(fullscreen=True, osd_ready=True)
                self.assertEqual(self.overlay.isVisible(), visible)
                state.action.trigger()
                state.refresh(fullscreen=False, osd_ready=False)
                self.assertFalse(self.overlay.isVisible())
                self.assertFalse(state.enabled)


if __name__ == "__main__":
    unittest.main()
