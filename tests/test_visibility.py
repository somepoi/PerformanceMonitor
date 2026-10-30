import os
import unittest
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
        state.refresh(in_game=False, osd_ready=True)
        self.assertTrue(self.overlay.isVisible())
        state.action.trigger()
        state.refresh(in_game=False, osd_ready=True)
        self.assertFalse(self.overlay.isVisible())
        self.assertEqual(state.action.text(), "Показать оверлей")
        state.action.trigger()
        self.assertTrue(self.overlay.isVisible())
        self.assertEqual(state.action.text(), "Скрыть оверлей")

    def test_auto_falls_back_to_window_when_osd_cannot_publish(self):
        state = OverlayVisibility(self.overlay, "auto")
        state.refresh(in_game=True, osd_ready=True)
        self.assertFalse(self.overlay.isVisible())
        state.refresh(in_game=True, osd_ready=False)
        self.assertTrue(self.overlay.isVisible())
        state.refresh(in_game=False, osd_ready=True)
        self.assertTrue(self.overlay.isVisible())

    def test_background_game_does_not_hide_desktop_overlay(self):
        source = object.__new__(RtssFpsSource)
        source._current_offset = 100
        source._current_pid = 42
        with patch("perfmon.rtss.foreground_pid", return_value=99):
            self.assertFalse(source.in_game)
        with patch("perfmon.rtss.foreground_pid", return_value=42):
            self.assertTrue(source.in_game)

    def test_display_modes_and_manual_disable(self):
        for mode, visible in (("window", True), ("both", True), ("rtss", False)):
            with self.subTest(mode=mode):
                state = OverlayVisibility(self.overlay, mode)
                state.refresh(in_game=True, osd_ready=True)
                self.assertEqual(self.overlay.isVisible(), visible)
                state.action.trigger()
                state.refresh(in_game=False, osd_ready=False)
                self.assertFalse(self.overlay.isVisible())
                self.assertFalse(state.enabled)


if __name__ == "__main__":
    unittest.main()
