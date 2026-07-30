import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import savefile_api
import window_manager
from save_catalog import ClientLocation, PuzzleMappingStore, SaveCatalog, SaveIndex
from savefile_api import FolditSaveSummary


class SaveSummaryApiCases(unittest.TestCase):
    def test_summary_returns_base_bonus_total_and_puzzle_from_one_read(self):
        meta = SimpleNamespace(payload="Best save")
        energy = SimpleNamespace(total_energy=125.5)
        player = SimpleNamespace(puzzle_id=2014362, player_name="Serca")

        with (
            patch.object(savefile_api, "_read_save_bytes", return_value=(Path("sample.ir_solution"), b"data")) as read,
            patch.object(savefile_api, "_find_meta", return_value=meta),
            patch.object(savefile_api, "_find_energy", return_value=energy),
            patch.object(savefile_api, "_find_player", return_value=player),
            patch.object(savefile_api, "_calculate_bonus_score", return_value=500.0),
        ):
            summary = savefile_api.get_save_summary("sample.ir_solution")

        self.assertEqual(read.call_count, 1)
        self.assertEqual(summary.puzzle_id, 2014362)
        self.assertEqual(summary.player_name, "Serca")
        self.assertEqual(summary.save_name, "Best save")
        self.assertAlmostEqual(summary.base_score, 6745.0)
        self.assertAlmostEqual(summary.bonus_score, 500.0)
        self.assertAlmostEqual(summary.total_score, 7245.0)

    def test_basic_info_remains_total_score_compatible(self):
        meta = SimpleNamespace(payload="Compatible")
        energy = SimpleNamespace(total_energy=100.0)
        player = SimpleNamespace(puzzle_id=123456, player_name="Player")
        with (
            patch.object(savefile_api, "_read_save_bytes", return_value=(Path("sample.ir_solution"), b"data")),
            patch.object(savefile_api, "_find_meta", return_value=meta),
            patch.object(savefile_api, "_find_energy", return_value=energy),
            patch.object(savefile_api, "_find_player", return_value=player),
            patch.object(savefile_api, "_calculate_bonus_score", return_value=250.0),
        ):
            info = savefile_api.get_basic_info("sample.ir_solution")
        self.assertEqual(info.player_name, "Player")
        self.assertEqual(info.save_name, "Compatible")
        self.assertAlmostEqual(info.foldit_score, 7250.0)


class SaveCatalogCases(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.client = self.root / "Foldit1"
        self.client.mkdir()
        self.index_path = self.root / "logs" / "_save_index.sqlite3"
        self.reader_calls = []

        def reader(path):
            self.reader_calls.append(Path(path).name)
            return FolditSaveSummary(
                puzzle_id=2014362,
                player_name="Serca",
                save_name=f"Name {Path(path).stem}",
                base_score=10000.125,
                bonus_score=250.0,
                total_score=10250.125,
            )

        self.reader = reader
        self.location = ClientLocation("Foldit1", str(self.client), True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _catalog(self):
        return SaveCatalog(SaveIndex(str(self.index_path)), self.reader)

    def test_scan_is_exact_non_recursive_and_uses_unchanged_cache(self):
        expected = self.client / "puzzle_2014362_time_100.ir_solution"
        expected.write_bytes(b"one")
        (self.client / "puzzle_20143620_time_100.ir_solution").write_bytes(b"wrong puzzle")
        (self.client / "manual.ir_solution").write_bytes(b"manual")
        nested = self.client / "nested"
        nested.mkdir()
        (nested / "puzzle_2014362_time_200.ir_solution").write_bytes(b"nested")

        catalog = self._catalog()
        records = catalog.scan_client("2014362", self.location)
        self.assertEqual([record.file_name for record in records], [expected.name])
        catalog.load_metadata(records[0])
        self.assertEqual(self.reader_calls, [expected.name])

        cached_records = self._catalog().scan_client("2014362", self.location)
        self.assertTrue(cached_records[0].metadata_loaded)
        self._catalog().load_metadata(cached_records[0])
        self.assertEqual(self.reader_calls, [expected.name])

        expected.write_bytes(b"changed-size")
        changed = self._catalog().scan_client("2014362", self.location)[0]
        self.assertFalse(changed.metadata_loaded)
        self._catalog().load_metadata(changed)
        self.assertEqual(self.reader_calls, [expected.name, expected.name])

    def test_deleted_files_are_pruned_from_index(self):
        save_path = self.client / "puzzle_2014362_time_100.ir_solution"
        save_path.write_bytes(b"one")
        catalog = self._catalog()
        record = catalog.scan_client("2014362", self.location)[0]
        catalog.load_metadata(record)
        save_path.unlink()
        self.assertEqual(catalog.scan_client("2014362", self.location), [])

        with closing(sqlite3.connect(self.index_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM save_index").fetchone()[0]
        self.assertEqual(count, 0)

    def test_parse_error_is_cached_and_retried_explicitly(self):
        save_path = self.client / "puzzle_2014362_time_100.ir_solution"
        save_path.write_bytes(b"one")
        outcomes = [
            FolditSaveSummary(999, "P", "Wrong", 1.0, 0.0, 1.0),
            FolditSaveSummary(2014362, "P", "Right", 2.0, 0.0, 2.0),
        ]
        catalog = SaveCatalog(SaveIndex(str(self.index_path)), lambda _path: outcomes.pop(0))
        record = catalog.scan_client("2014362", self.location)[0]
        catalog.load_metadata(record)
        self.assertIn("Puzzle mismatch", record.error)
        catalog.load_metadata(record)
        self.assertEqual(len(outcomes), 1)
        catalog.load_metadata(record, force_error_retry=True)
        self.assertEqual(record.error, "")
        self.assertEqual(record.save_name, "Right")

    def test_active_public_puzzle_maps_to_internal_id_from_log(self):
        (self.client / "log.txt").write_text(
            "game.application.GameApplication: Loading puzzle 2014359\n"
            "game.application.GameApplication: Loading puzzle 2014362\n",
            encoding="utf-8",
        )
        active_client = ClientLocation("Foldit1", str(self.client), True, "2790")
        catalog = self._catalog()
        self.assertEqual(catalog.resolve_internal_puzzle_id("2790", [active_client]), "2014362")
        self.assertEqual(
            [row.internal_id for row in catalog.mapping_store.get("2790")],
            ["2014362"],
        )

    def test_csv_mapping_is_used_before_log_fallback(self):
        store = PuzzleMappingStore(str(self.root / "logs" / "puzzle_map.csv"))
        self.assertTrue(store.add("2790", "2014362", "manual"))
        catalog = SaveCatalog(SaveIndex(str(self.index_path)), self.reader, store)
        active_client = ClientLocation("Foldit1", str(self.client), True, "2790")
        with patch.object(catalog, "read_active_internal_puzzle_id") as read_log:
            resolution = catalog.resolve_internal_puzzle_ids("2790", [active_client])
        self.assertEqual(resolution.internal_ids, ("2014362",))
        read_log.assert_not_called()

    def test_conflicting_active_clients_are_saved_and_scanned_together(self):
        second_client = self.root / "Foldit2"
        second_client.mkdir()
        (self.client / "log.txt").write_text("Loading puzzle 2014359\n", encoding="utf-8")
        (second_client / "log.txt").write_text("Loading puzzle 2014362\n", encoding="utf-8")
        clients = [
            ClientLocation("Foldit1", str(self.client), True, "2791"),
            ClientLocation("Foldit2", str(second_client), True, "2791"),
        ]
        catalog = self._catalog()
        resolution = catalog.resolve_internal_puzzle_ids("2791", clients)
        self.assertEqual(set(resolution.internal_ids), {"2014359", "2014362"})
        self.assertIn("showing all", resolution.warning)
        self.assertEqual(
            {row.internal_id for row in catalog.mapping_store.get("2791")},
            {"2014359", "2014362"},
        )

    def test_scan_reads_nested_slots_for_mapped_internal_puzzle(self):
        slot = self.client / "puzzles" / "0002014362" / "0000918066" / "default"
        slot.mkdir(parents=True)
        quicksave = slot / "quicksave10.ir_solution"
        quicksave.write_bytes(b"nested-save")

        catalog = self._catalog()
        self.assertEqual(catalog.scan_client("2790", self.location, "2014362"), [])
        records = catalog.scan_client(
            "2790", self.location, "2014362", include_quick_auto=True
        )
        self.assertEqual([record.path for record in records], [str(quicksave)])
        self.assertEqual(records[0].puzzle_id, "2790")
        self.assertEqual(records[0].internal_puzzle_id, "2014362")

    def test_historical_mapping_uses_managed_log_and_save_timestamps(self):
        log_file = self.client / "F1.2787 DRW.9000.20260710.120000.fin.txt"
        log_file.write_text("log", encoding="utf-8")
        reference_time = 1_750_000_000.0
        os.utime(log_file, (reference_time, reference_time))

        close_slot = self.client / "puzzles" / "0002014356" / "1" / "default"
        far_slot = self.client / "puzzles" / "0002014351" / "1" / "default"
        close_slot.mkdir(parents=True)
        far_slot.mkdir(parents=True)
        close_save = close_slot / "quicksave.ir_solution"
        far_save = far_slot / "quicksave.ir_solution"
        close_save.write_bytes(b"close")
        far_save.write_bytes(b"far")
        os.utime(close_save, (reference_time + 5, reference_time + 5))
        os.utime(far_save, (reference_time + 1200, reference_time + 1200))

        catalog = self._catalog()
        self.assertEqual(catalog.resolve_internal_puzzle_id("2787", [self.location]), "2014356")
        self.assertEqual(
            [row.internal_id for row in catalog.mapping_store.get("2787")],
            ["2014356"],
        )

    def test_index_failure_degrades_to_uncached_empty_scan(self):
        invalid_database = self.root / "database-is-a-folder"
        invalid_database.mkdir()
        index = SaveIndex(str(invalid_database))
        catalog = SaveCatalog(index, self.reader)
        self.assertEqual(catalog.scan_client("2790", self.location, "2014362"), [])
        self.assertTrue(index.disabled_reason)

    def test_cache_row_limit_is_applied_lazily(self):
        for number in range(3):
            path = self.client / f"puzzle_2014362_time_{number}.ir_solution"
            path.write_bytes(str(number).encode("ascii"))
        first_catalog = SaveCatalog(SaveIndex(str(self.index_path), max_rows=2), self.reader)
        for record in first_catalog.scan_client("2790", self.location, "2014362"):
            first_catalog.load_metadata(record)

        second_catalog = SaveCatalog(SaveIndex(str(self.index_path), max_rows=2), self.reader)
        second_catalog.scan_client("2790", self.location, "2014362")
        with closing(sqlite3.connect(self.index_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM save_index").fetchone()[0]
        self.assertLessEqual(count, 2)

    def test_name_and_inclusive_score_filters(self):
        paths = [
            self.client / "puzzle_2014362_time_100.ir_solution",
            self.client / "puzzle_2014362_time_200.ir_solution",
        ]
        for path in paths:
            path.write_bytes(path.name.encode("ascii"))
        records = self._catalog().scan_client("2014362", self.location)
        records[0].metadata_loaded = True
        records[0].save_name = "Alpha Best"
        records[0].total_score = 12000.0
        records[0].base_score = 11750.0
        records[1].metadata_loaded = True
        records[1].save_name = "Beta"
        records[1].total_score = 11000.0
        records[1].base_score = 11000.0

        self.assertEqual(SaveCatalog.filter_records(records, "alpha"), [records[0]])
        self.assertEqual(
            SaveCatalog.filter_records(records, score_field="total", minimum=12000.0, maximum=12000.0),
            [records[0]],
        )
        self.assertEqual(
            SaveCatalog.filter_records(records, score_field="base", minimum=11750.0, maximum=11750.0),
            [records[0]],
        )

    def test_copy_selected_file_skips_existing_and_continues_after_failure(self):
        source = self.client / "puzzle_2014362_time_100.ir_solution"
        source.write_bytes(b"selected-save")
        record = self._catalog().scan_client("2014362", self.location)[0]

        copied_dir = self.root / "Foldit2"
        skipped_dir = self.root / "Foldit3"
        missing_dir = self.root / "Foldit4"
        copied_dir.mkdir()
        skipped_dir.mkdir()
        (skipped_dir / source.name).write_bytes(b"existing")
        targets = [
            ClientLocation("Foldit2", str(copied_dir), True),
            ClientLocation("Foldit3", str(skipped_dir), True),
            ClientLocation("Foldit4", str(missing_dir), False),
        ]

        report = SaveCatalog.copy_record(record, targets)
        self.assertEqual((copied_dir / source.name).read_bytes(), b"selected-save")
        self.assertEqual((skipped_dir / source.name).read_bytes(), b"existing")
        self.assertEqual((report.copied, report.skipped, report.failed), (1, 1, 1))


class SaveManagerIntegrationSourceCases(unittest.TestCase):
    def test_open_containing_folder_uses_default_folder_handler(self):
        save_path = os.path.join("client", "puzzle_1_time_1.ir_solution")
        with patch.object(window_manager, "open_folder") as open_folder:
            window_manager.open_containing_folder(save_path)
        open_folder.assert_called_once_with(os.path.dirname(os.path.abspath(save_path)))

    def test_main_and_stats_entry_points_are_wired(self):
        root = Path(__file__).resolve().parents[1]
        monitor_source = (root / "Foldit Monitor.pyw").read_text(encoding="utf-8-sig")
        tk_stats_source = (root / "stats_ui.py").read_text(encoding="utf-8")
        qt_stats_source = (root / "stats_ui_qt.py").read_text(encoding="utf-8")
        save_manager_source = (root / "save_manager_qt.py").read_text(encoding="utf-8")
        self.assertIn('MANAGE_SAVES_LABEL = "Manage Saves"', monitor_source)
        self.assertIn('initial_scope="client"', monitor_source)
        self.assertIn('text="Saves"', tk_stats_source)
        self.assertIn('QPushButton("Saves"', qt_stats_source)
        self.assertIn('self.save_manager_handler(self.puzzle_id, None, "running")', tk_stats_source)
        self.assertIn('self.save_manager_handler(self.puzzle_id, None, "running")', qt_stats_source)
        callback_source = monitor_source.split("def open_save_manager_window", 1)[1].split(
            "def refresh_stats_puzzle_menu", 1
        )[0]
        self.assertIn("from save_manager_qt import show_save_manager", callback_source)
        self.assertNotIn("after_idle", callback_source)
        self.assertNotIn("subprocess", callback_source)
        self.assertIn('QCheckBox("Include quick/auto saves"', save_manager_source)
        self.assertIn('QPushButton("Open folder"', save_manager_source)
        self.assertNotIn('QPushButton("Show in Explorer"', save_manager_source)
        self.assertIn("QPlainTextEdit(dialog)", save_manager_source)
        self.assertIn("details.setReadOnly(True)", save_manager_source)
        self.assertIn('getattr(client, "active_puzzle_id"', save_manager_source)


if __name__ == "__main__":
    unittest.main()
