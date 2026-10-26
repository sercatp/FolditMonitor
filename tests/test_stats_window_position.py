import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QRect

from modules.stats_ui_qt import StatsWindowQt, _stats_window_position


class StatsWindowPositionTests(unittest.TestCase):
    def setUp(self):
        self.primary = QRect(0, 0, 2603, 1440)

    def test_default_and_legacy_physical_coordinates_center_on_logical_screen(self):
        for saved in (None, (3543, 1204), (100, 5000)):
            self.assertEqual(_stats_window_position(saved, (1200, 850), [self.primary]), (701, 295))

    def test_partially_visible_window_is_kept_wholly_in_work_area(self):
        self.assertEqual(_stats_window_position((2300, 1200), (900, 600), [self.primary]), (1703, 840))

    def test_valid_negative_coordinates_on_secondary_display_are_preserved(self):
        secondary = QRect(-1920, 0, 1920, 1080)
        self.assertEqual(_stats_window_position((-1800, 100), (1000, 700), [self.primary, secondary]), (-1800, 100))

    def test_taskbar_and_oversized_window_leave_title_bar_visible(self):
        work = QRect(0, 40, 1024, 728)
        self.assertEqual(_stats_window_position(None, (680, 420), [work]), (172, 194))
        self.assertEqual(_stats_window_position((100, 100), (1400, 900), [work]), (0, 40))

    def test_default_position_does_not_use_tk_parent_pixel_coordinates(self):
        parent = SimpleNamespace(winfo_rootx=Mock(side_effect=AssertionError("Tk pixels must not enter Qt placement")))
        owner = SimpleNamespace(_adjust_window_size=Mock(), _get_saved_position=lambda: None, _place_window=Mock())
        StatsWindowQt._apply_initial_position(owner, parent)
        owner._place_window.assert_called_once_with(None)
        parent.winfo_rootx.assert_not_called()

    def test_actual_placement_recovers_saved_offscreen_window(self):
        screen = SimpleNamespace(availableGeometry=lambda: self.primary)
        owner = SimpleNamespace(frameGeometry=lambda: QRect(3543, 1204, 1200, 850), move=Mock())
        with patch("modules.stats_ui_qt.QApplication") as application:
            application.primaryScreen.return_value = screen
            application.screens.return_value = [screen]
            StatsWindowQt._place_window(owner, (3543, 1204))
        owner.move.assert_called_once_with(701, 295)


if __name__ == "__main__":
    unittest.main()
