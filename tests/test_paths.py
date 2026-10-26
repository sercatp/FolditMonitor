import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.paths import AppPaths, get_app_paths
from modules.settings import Settings
from modules.stats_module import StatsManager


class LocalDataPathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = AppPaths(self.root, get_app_paths().resources)

    def test_existing_settings_reports_logs_and_save_cache_move_together(self):
        profile = '{"unknown_user_option": "keep", "logging": {"logs_folder": "puzzle_logs"}}'
        (self.root / "Foldit Monitor.json").write_text(profile, encoding="utf-8")
        (self.root / "Foldit Monitor.json.pre-settings-gui.bak").write_bytes(b"backup")
        expected = {
            "puzzle_logs/2819.csv": b"score,client\n100,Foldit1\n",
            "puzzle_logs/_runtime/pose.ir_solution": b"saved pose",
            "puzzle_logs/_save_index.sqlite3": b"local index",
            "ligand_viewer_reports/example/report.html": b"report",
            "logs/diagnostic.log": b"trace",
        }
        for name, content in expected.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.paths.prepare_data()
        self.paths.prepare_data()  # Repeated launches do not create another data/.
        for name, content in expected.items():
            self.assertEqual((self.paths.data / name).read_bytes(), content)
            self.assertFalse((self.root / name).exists())
        self.assertEqual((self.paths.data / "Foldit Monitor.json").read_text(encoding="utf-8"), profile)
        self.assertEqual((self.paths.data / "Foldit Monitor.json.pre-settings-gui.bak").read_bytes(), b"backup")

    def test_conflicting_data_is_never_overwritten(self):
        legacy = self.root / "puzzle_logs" / "2819.csv"
        current = self.paths.puzzle_logs / "2819.csv"
        for path in (legacy, current):
            path.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"old statistics")
        current.write_bytes(b"new statistics")
        with self.assertRaises(FileExistsError):
            self.paths.prepare_data()
        self.assertEqual(legacy.read_bytes(), b"old statistics")
        self.assertEqual(current.read_bytes(), b"new statistics")

    def test_partial_migration_merges_distinct_files(self):
        (self.root / "puzzle_logs").mkdir()
        self.paths.puzzle_logs.mkdir(parents=True)
        (self.root / "puzzle_logs" / "old.csv").write_bytes(b"old")
        (self.paths.puzzle_logs / "new.csv").write_bytes(b"new")
        self.paths.prepare_data()
        self.assertEqual({p.name for p in self.paths.puzzle_logs.iterdir()}, {"old.csv", "new.csv"})
        self.assertFalse((self.root / "puzzle_logs").exists())

    def test_legacy_custom_relative_and_external_paths_keep_their_location(self):
        external = self.root.parent / "custom-audio.wav"
        profile = {"logging": {"logs_folder": "custom_stats"}, "sound": {"alert_file": str(external)}}
        (self.root / "Foldit Monitor.json").write_text(json.dumps(profile), encoding="utf-8")
        self.paths.prepare_data()
        profile = json.loads((self.paths.data / "Foldit Monitor.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["logging"]["logs_folder"], str(self.root / "custom_stats"))
        self.assertEqual(profile["sound"]["alert_file"], str(external))

    def test_legacy_absolute_standard_folder_follows_the_move(self):
        profile = {"logging": {"logs_folder": str(self.root / "puzzle_logs")}}
        (self.root / "Foldit Monitor.json").write_text(json.dumps(profile), encoding="utf-8")
        self.paths.prepare_data()
        profile = json.loads((self.paths.data / "Foldit Monitor.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["logging"]["logs_folder"], "puzzle_logs")

    def test_custom_subfolder_and_audio_inside_moved_data_follow_the_move(self):
        profile = {"logging": {"logs_folder": "puzzle_logs/custom"},
                   "sound": {"alert_file": str(self.root / "logs" / "custom.wav")}}
        (self.root / "Foldit Monitor.json").write_text(json.dumps(profile), encoding="utf-8")
        self.paths.prepare_data()
        profile = json.loads((self.paths.data / "Foldit Monitor.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(profile["logging"]["logs_folder"]), Path("puzzle_logs/custom"))
        self.assertEqual(profile["sound"]["alert_file"], str(self.paths.logs / "custom.wav"))

    def test_new_relative_paths_are_not_reinterpreted_on_later_launches(self):
        self.paths.prepare_data()
        profile = {"logging": {"logs_folder": "custom_new_stats"}}
        (self.paths.data / "Foldit Monitor.json").write_text(json.dumps(profile), encoding="utf-8")
        self.paths.prepare_data()
        self.assertEqual(json.loads((self.paths.data / "Foldit Monitor.json").read_text(encoding="utf-8")), profile)

    def test_new_data_directory_works_from_an_unrelated_working_directory(self):
        self.paths.prepare_data()
        original = Path.cwd()
        try:
            os.chdir(self.root.parent)
            with patch("modules.settings.get_app_paths", return_value=self.paths):
                settings = Settings(str(self.paths.data))
            stats = StatsManager(str(self.paths.data), settings.settings)
        finally:
            os.chdir(original)
        self.assertEqual(Path(settings.settings_file), self.paths.data / "Foldit Monitor.json")
        self.assertEqual(Path(stats.logs_folder), self.paths.puzzle_logs)
        self.assertEqual(Path(settings.sound_file), self.paths.resources / "alert.wav")
        self.assertTrue(Path(settings.sound_file).is_file())

    def test_frozen_application_writes_beside_exe_and_reads_bundle_resources(self):
        executable = self.root / "install" / "FolditMonitor.exe"
        bundle = self.root / "temporary_bundle"
        with patch("modules.paths.sys.frozen", True, create=True), patch("modules.paths.sys.executable", str(executable)), patch("modules.paths.sys._MEIPASS", str(bundle), create=True):
            paths = get_app_paths()
        self.assertEqual(paths.data, executable.parent / "data")
        self.assertEqual(paths.resources, bundle / "modules" / "resources")

    def test_invalid_settings_are_preserved_for_settings_error_handling(self):
        (self.root / "Foldit Monitor.json").write_bytes(b"{broken JSON")
        self.paths.prepare_data()
        self.assertEqual((self.paths.data / "Foldit Monitor.json").read_bytes(), b"{broken JSON")


if __name__ == "__main__":
    unittest.main()
