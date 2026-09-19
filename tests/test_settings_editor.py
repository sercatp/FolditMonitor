import json
import tempfile
import unittest
from pathlib import Path

from settings import RESET_TO_DEFAULT, Settings
from settings_validation import SettingsValidationError, validate_settings


class SettingsEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Foldit Monitor.json"
        self.manager = Settings(self.temp.name)

    def test_profile_contains_new_editable_defaults(self):
        defaults, _, effective = self.manager.get_editor_snapshot()
        self.assertEqual(defaults["check_interval"], 2)
        self.assertEqual(defaults["logging"]["score_patterns"][0], r"\b\d{4,6}\.\d+\b")
        self.assertEqual(effective["speed_boost"]["profiles"]["medium"]["replacement_sleep_ms"], 5)
        validate_settings(effective)

    def test_changed_paths_preserve_unknown_and_concurrent_unrelated_edits(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        disk["unknown_future_option"] = {"keep": 42}
        disk["display"]["window_position"]["x"] = 931
        self.path.write_text(json.dumps(disk), encoding="utf-8")

        effective = self.manager.save_editor_changes({("sound", "volume"): 0.6}, baseline)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["unknown_future_option"], {"keep": 42})
        self.assertEqual(saved["display"]["window_position"]["x"], 931)
        self.assertEqual(effective["sound"]["volume"], 0.6)
        self.assertEqual(json.loads(Path(str(self.path) + ".pre-settings-gui.bak").read_text(encoding="utf-8"))["unknown_future_option"], {"keep": 42})

    def test_conflicting_edit_is_rejected_without_changing_file(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        disk["sound"]["volume"] = 0.4
        self.path.write_text(json.dumps(disk), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(SettingsValidationError, "changed on disk"):
            self.manager.save_editor_changes({("sound", "volume"): 0.6}, baseline)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reset_removes_override_and_keeps_effective_default(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        effective = self.manager.save_editor_changes({("sound", "volume"): RESET_TO_DEFAULT}, baseline)
        self.assertEqual(effective["sound"]["volume"], 1)
        self.assertNotIn("volume", json.loads(self.path.read_text(encoding="utf-8"))["sound"])

    def test_invalid_regex_and_mapping_are_not_written(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        before = self.path.read_bytes()
        with self.assertRaises(SettingsValidationError):
            self.manager.save_editor_changes({("logging", "score_patterns"): ["["]}, baseline)
        self.assertEqual(self.path.read_bytes(), before)
        mapping = dict(baseline["script_type_mapping"])
        mapping["drw"] = {"name": "DRW", "column_number": 0, "state_snapshot_rules": [{"detector": ["X"], "extractors": []}]}
        with self.assertRaises(SettingsValidationError):
            self.manager.save_editor_changes({("script_type_mapping",): mapping}, baseline)
        self.assertEqual(self.path.read_bytes(), before)

    def test_mapping_order_and_nested_rules_survive_reload(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        mapping = dict(baseline["script_type_mapping"])
        first = mapping.pop("drw")
        mapping = {"drw": first, **mapping}
        self.manager.save_editor_changes({("script_type_mapping",): mapping}, baseline)
        reloaded = Settings(self.temp.name)
        self.assertEqual(next(iter(reloaded.settings["script_type_mapping"])), "drw")
        self.assertEqual(reloaded.settings["script_type_mapping"]["drw"]["state_snapshot_rules"][0]["stats_mapping"]["score"], "f_value")

    def test_editor_position_reset_survives_automatic_close_save(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        self.manager.save_editor_changes({
            ("display", "window_position", "x"): RESET_TO_DEFAULT,
            ("display", "window_position", "y"): RESET_TO_DEFAULT,
        }, baseline)
        before = self.path.read_bytes()
        self.manager.save_window_position(777, 888)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(Settings(self.temp.name).settings["display"]["window_position"], {"x": 1, "y": 1})

    def test_custom_palette_applies_live_without_reloading_other_settings(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        effective = self.manager.save_editor_changes({
            ("display", "active_palette"): "custom",
            ("display", "row_appearance"): {"normal": {"foreground": "#123456"}},
        }, baseline)
        changed = self.manager.apply_live_editor_settings(effective, {
            ("display", "active_palette"), ("display", "row_appearance")
        })
        self.assertEqual(len(changed), 2)
        self.assertEqual(self.manager.ROW_APPEARANCE["normal"]["foreground"], "#123456")
        self.assertIn("tree", self.manager.ROW_APPEARANCE)

    def test_restart_fields_do_not_change_active_runtime(self):
        _, baseline, _ = self.manager.get_editor_snapshot()
        old_port = self.manager.DEFAULT_PORT
        old_profiles = dict(self.manager.SPEED_BOOST_PROFILES)
        profiles = dict(baseline["speed_boost"]["profiles"])
        profiles["medium"] = {**profiles["medium"], "replacement_sleep_ms": 8}
        effective = self.manager.save_editor_changes({
            ("network", "default_port"): 9001,
            ("speed_boost", "profiles"): profiles,
        }, baseline)
        self.manager.apply_live_editor_settings(effective, {("network", "default_port"), ("speed_boost", "profiles")})
        self.assertEqual(self.manager.DEFAULT_PORT, old_port)
        self.assertEqual(self.manager.SPEED_BOOST_PROFILES, old_profiles)
        self.manager.save_active_display_palette("dark")
        self.assertEqual(self.manager.DEFAULT_PORT, old_port)
        self.assertEqual(self.manager.SPEED_BOOST_PROFILES, old_profiles)
        reloaded = Settings(self.temp.name)
        self.assertEqual(reloaded.DEFAULT_PORT, 9001)
        self.assertEqual(reloaded.SPEED_BOOST_PROFILES["medium"]["replacement_sleep_ms"], 8)

    def test_malformed_user_file_is_not_overwritten(self):
        self.path.write_text('{"sound":', encoding="utf-8")
        before = self.path.read_bytes()
        reloaded = Settings(self.temp.name)
        self.assertTrue(reloaded.load_error)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(SettingsValidationError):
            reloaded.get_editor_snapshot()

    def test_gui_smoke_on_isolated_config(self):
        try:
            import tkinter as tk
            from settings_ui import SettingsDialog
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk is unavailable")
        self.addCleanup(root.destroy)
        root.withdraw()
        dialog = SettingsDialog(root, self.manager, lambda _paths, _effective: None)
        self.addCleanup(lambda: dialog.window.destroy() if dialog.window.winfo_exists() else None)
        root.update_idletasks()
        self.assertEqual(dialog._collect_changes(), {})
        dialog.controls[("sound", "volume")][0].set("0.75")
        dialog.save()
        self.assertEqual(self.manager.get_editor_snapshot()[2]["sound"]["volume"], 0.75)
        self.assertIn("All changes applied", dialog.status.get())
        dialog.controls[("network", "default_port")][0].set("9001")
        dialog.save()
        self.assertIn("Takes effect after restart", dialog.status.get())

    def test_speed_boost_tab_is_available_only_when_enabled_in_json(self):
        try:
            import tkinter as tk
            from settings_ui import SettingsDialog
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk is unavailable")
        self.addCleanup(root.destroy)
        root.withdraw()

        disabled = SettingsDialog(root, self.manager, lambda _paths, _effective: None)
        self.assertNotIn("Speed Boost", disabled.tabs)
        self.assertNotIn(("speed_boost", "enabled"), disabled.controls)
        disabled.window.destroy()

        disk = json.loads(self.path.read_text(encoding="utf-8"))
        disk["speed_boost"]["enabled"] = True
        self.path.write_text(json.dumps(disk), encoding="utf-8")
        enabled_manager = Settings(self.temp.name)
        enabled = SettingsDialog(root, enabled_manager, lambda _paths, _effective: None)
        self.addCleanup(lambda: enabled.window.destroy() if enabled.window.winfo_exists() else None)
        self.assertIn("Speed Boost", enabled.tabs)
        self.assertNotIn(("speed_boost", "enabled"), enabled.controls)
        self.assertEqual(enabled._collect_changes(), {})

    def test_gui_can_reselect_stored_custom_palette(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk is unavailable")
        self.addCleanup(root.destroy)
        root.withdraw()
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        disk["display"]["active_palette"] = "contrast"
        disk["display"]["row_appearance"] = {"normal": {"foreground": "#123456"}}
        self.path.write_text(json.dumps(disk), encoding="utf-8")
        dialog = __import__("settings_ui").SettingsDialog(root, self.manager, lambda _paths, _effective: None)
        self.addCleanup(lambda: dialog.window.destroy() if dialog.window.winfo_exists() else None)
        dialog.palette_var.set("custom")
        self.assertEqual(dialog.pending["display"]["row_appearance"]["normal"]["foreground"], "#123456")


if __name__ == "__main__":
    unittest.main()
