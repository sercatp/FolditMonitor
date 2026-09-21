import math
import os
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import savefile_api
import window_manager
from save_catalog import ClientLocation, PuzzleMappingStore, SaveCatalog, SaveIndex, SaveRecord, normalize_path
from savefile_api import FolditSaveSummary


class SaveSummaryApiCases(unittest.TestCase):
    @staticmethod
    def _mode_block(raw_mode, player_id=415886):
        build = b"20250926-ed7de9856f-win_x64"
        reference_energy = -2010.5697409246077
        return (
            b"PDLT"
            + struct.pack("<5I", 3, raw_mode, player_id, 988042, len(build))
            + build
            + struct.pack("<dBdd", reference_energy, 0xFF, reference_energy, reference_energy)
        )

    def test_summary_returns_base_bonus_total_and_puzzle_from_one_read(self):
        meta = SimpleNamespace(payload="Best save")
        energy = SimpleNamespace(total_energy=125.5)
        player = SimpleNamespace(puzzle_id=2014362, player_name="Serca")

        with (
            patch.object(savefile_api, "_read_save_bytes", return_value=(Path("sample.ir_solution"), b"data" + self._mode_block(2))) as read,
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
        self.assertEqual(summary.solution_mode, "evolver")
        self.assertEqual(summary.mode_player_id, 415886)
        self.assertAlmostEqual(summary.mode_reference_base_score, 28105.697409246077)

    def test_mode_block_distinguishes_solo_evolver_and_unidentified_values(self):
        for code, expected in ((1, "solo"), (2, "evolver"), (3, None)):
            with self.subTest(code=code):
                data = b"shared solution without energy" + self._mode_block(code)
                with patch.object(savefile_api, "_read_save_bytes", return_value=(Path("sample.ir_solution"), data)):
                    info = savefile_api.get_solution_mode_info("sample.ir_solution")
                self.assertEqual(info.mode, expected)
                self.assertEqual(info.raw_mode, code)
                self.assertEqual(info.player_id, 415886)
                self.assertAlmostEqual(info.reference_base_score, 28105.697409246077)

    def test_mode_block_rejects_truncated_or_invalid_data(self):
        for data in (b"no PDLT", self._mode_block(2)[:20], self._mode_block(2).replace(b"PDLT", b"XXXX")):
            with self.subTest(data=data), patch.object(
                savefile_api, "_read_save_bytes", return_value=(Path("sample.ir_solution"), data)
            ):
                with self.assertRaises(savefile_api.FolditApiError):
                    savefile_api.get_solution_mode_info("sample.ir_solution")

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


class EnergyBlockDetectionCases(unittest.TestCase):
    @staticmethod
    def _variant_b(values, blob=b""):
        values = [float(value) for value in values]
        energy = math.fsum(values)
        return (
            struct.pack("<I", len(blob))
            + blob
            + struct.pack("<dI", energy, len(values))
            + struct.pack(f"<{len(values)}d", *values)
        )

    def test_trimmed_energy_count_does_not_have_to_match_resi_count(self):
        # 131 active amino acids plus one VRT energy entry, while RESI still
        # describes the complete 569-amino-acid pose plus VRT.
        values = [-4.5] * 131 + [-475.0]
        encoded = self._variant_b(values)
        data = encoded + b"RESI" + struct.pack("<I", 570) + b"payload"

        block = savefile_api.find_energy_block(
            data,
            start=0,
            window=len(data),
            n_targets=(570,),
        )

        self.assertIsNotNone(block)
        self.assertEqual(block.variant, "B")
        self.assertEqual(block.n, 132)
        self.assertAlmostEqual(block.total_energy, math.fsum(values))

    def test_structured_energy_can_be_small_when_its_sum_is_valid(self):
        encoded = self._variant_b([0.25, -0.125])
        block = savefile_api.find_energy_block(encoded, start=0, window=len(encoded))
        self.assertIsNotNone(block)
        self.assertEqual(block.n, 2)
        self.assertAlmostEqual(block.total_energy, 0.125)

    def test_tiny_mismatched_binary_values_do_not_pass_absolute_tolerance(self):
        encoded = (
            struct.pack("<I", 0)
            + struct.pack("<dI", 0.0, 1)
            + struct.pack("<d", 5e-312)
        )
        self.assertIsNone(savefile_api.try_energy_variant_b(encoded, 0))

    def test_single_entry_structured_energy_is_supported(self):
        encoded = self._variant_b([-2.5])
        block = savefile_api.find_energy_block(encoded, start=0, window=len(encoded))
        self.assertIsNotNone(block)
        self.assertEqual(block.n, 1)
        self.assertAlmostEqual(block.total_energy, -2.5)

    def test_resi_count_is_not_accepted_as_a_variant_d_energy_block(self):
        data = (
            b"unrelated-prefix-data"
            + b"RESI"
            + struct.pack("<I", 570)
            + (b"\xff" * (570 * 8))
        )
        block = savefile_api.find_energy_block(
            data,
            start=0,
            window=len(data),
            n_targets=(570,),
        )
        self.assertIsNone(block)

    def test_plausible_variant_d_remains_available_before_resi(self):
        values = [-1.25, 2.5, -3.0]
        data = b"prefix!" + struct.pack("<I3d", len(values), *values) + b"tail"
        block = savefile_api.find_energy_block(
            data,
            start=0,
            window=len(data),
            n_targets=(len(values),),
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.variant, "D")
        self.assertEqual(block.n, len(values))
        self.assertAlmostEqual(block.total_energy, math.fsum(values))

    def test_malformed_data_returns_no_candidate_instead_of_leaking_nan(self):
        self.assertIsNone(
            savefile_api.find_energy_block(
                b"\x00" * 128,
                start=0,
                window=128,
                n_targets=(2,),
            )
        )
        with self.assertRaises(savefile_api.FolditApiError):
            savefile_api._calculate_base_score(SimpleNamespace(total_energy=math.inf))

    def test_invalid_early_tags_do_not_hide_later_valid_sections(self):
        bad_length = struct.pack("<I", 0xFFFFFFFF)
        tagged_data = b"META" + bad_length + b"junk" + b"META" + struct.pack("<I", 2) + b"ok"
        tagged = savefile_api.find_tagged_string(tagged_data, b"META")
        self.assertIsNotNone(tagged)
        self.assertEqual(tagged.payload, "ok")

        resi_data = b"RESI" + struct.pack("<I", 0) + b"junk" + b"RESI" + struct.pack("<I", 7)
        resi = savefile_api.find_resi_tag(resi_data)
        self.assertIsNotNone(resi)
        self.assertEqual(resi.count, 7)


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

    def test_mode_fields_survive_metadata_cache(self):
        save_path = self.client / "puzzle_2014362_time_100.ir_solution"
        save_path.write_bytes(b"save")
        self.reader = lambda _path: FolditSaveSummary(
            2014362, "Serca", "Evolver save", 28121.231, 0.0, 28121.231,
            "evolver", 415886, 28105.697,
        )
        catalog = self._catalog()
        catalog.load_metadata(catalog.scan_client("2014362", self.location)[0])
        cached = self._catalog().scan_client("2014362", self.location)[0]
        self.assertTrue(cached.metadata_loaded)
        self.assertEqual(cached.solution_mode, "evolver")
        self.assertEqual(cached.mode_player_id, 415886)
        self.assertAlmostEqual(cached.mode_reference_base_score, 28105.697)

    def test_older_parser_cache_schema_is_rebuilt(self):
        save_path = self.client / "puzzle_2014362_time_100.ir_solution"
        save_path.write_bytes(b"one")

        first_catalog = self._catalog()
        first_record = first_catalog.scan_client("2014362", self.location)[0]
        first_catalog.load_metadata(first_record)
        self.assertEqual(self.reader_calls, [save_path.name])

        with closing(sqlite3.connect(self.index_path)) as connection, connection:
            connection.execute("PRAGMA user_version = 4")

        second_catalog = self._catalog()
        second_record = second_catalog.scan_client("2014362", self.location)[0]
        self.assertFalse(second_record.metadata_loaded)
        second_catalog.load_metadata(second_record)
        self.assertEqual(self.reader_calls, [save_path.name, save_path.name])

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

    def test_stale_cached_mapping_is_replaced_using_puzzle_titles(self):
        wrong_id = "2014390"
        right_id = "2014388"
        for internal_id, title in (
            (wrong_id, "2814: Revisiting Puzzle 115"),
            (right_id, "2815: Electron Density Reconstruction"),
        ):
            (self.client / f"{int(internal_id):010d}.ir_puzzle").write_text(
                f'version: 1\n{{\n "id" : "{internal_id}"\n "title" : "{title}"\n}}\n',
                encoding="utf-8",
            )
        (self.client / "log.txt").write_text(
            f"Loading puzzle {wrong_id}\n", encoding="utf-8"
        )
        store = PuzzleMappingStore(str(self.root / "logs" / "puzzle_map.csv"))
        self.assertTrue(store.add("2815", wrong_id, "active-log"))
        catalog = SaveCatalog(SaveIndex(str(self.index_path)), self.reader, store)
        client = ClientLocation("Foldit1", str(self.client), True, "2815")

        resolution = catalog.resolve_internal_puzzle_ids("2815", [client])

        self.assertEqual(resolution.internal_ids, (right_id,))
        self.assertEqual([row.internal_id for row in store.get("2815")], [right_id])
        self.assertEqual(store.get("2815")[0].source, "puzzle-file")

    def test_puzzle_title_prevents_stale_active_log_from_creating_mapping(self):
        wrong_id = "2014390"
        right_id = "2014388"
        for internal_id, title in ((wrong_id, "2814: Other"), (right_id, "2815: Current")):
            (self.client / f"{int(internal_id):010d}.ir_puzzle").write_text(
                f'"id" : "{internal_id}"\n"title" : "{title}"\n', encoding="utf-8"
            )
        (self.client / "log.txt").write_text(
            f"Loading puzzle {wrong_id}\n", encoding="utf-8"
        )
        catalog = self._catalog()
        client = ClientLocation("Foldit1", str(self.client), True, "2815")

        self.assertEqual(
            catalog.resolve_internal_puzzle_ids("2815", [client]).internal_ids,
            (right_id,),
        )
        self.assertEqual(
            [row.internal_id for row in catalog.mapping_store.get("2815")], [right_id]
        )

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

    def test_batch_copy_to_external_folder_preserves_name_collisions(self):
        other_client = self.root / "Foldit2"
        target_client = self.root / "Foldit3"
        external = self.root / "archive"
        for folder in (other_client, target_client, external):
            folder.mkdir()
        file_name = "puzzle_2014362_time_100.ir_solution"
        first = self.client / file_name
        second = other_client / file_name
        first.write_bytes(b"first version")
        second.write_bytes(b"second version")
        (external / file_name).write_bytes(b"existing archive")

        records = []
        for path, client_name, client_path in (
            (first, "Foldit1", self.client), (second, "Foldit2", other_client)
        ):
            stat = path.stat()
            records.append(SaveRecord(
                str(path), client_name, str(client_path), "2014362", "2014362",
                path.name, stat.st_size, stat.st_mtime_ns, stat.st_mtime,
            ))
        report = SaveCatalog.copy_records(
            records, [ClientLocation("Foldit3", str(target_client), True)], str(external)
        )
        self.assertEqual((report.copied, report.skipped, report.failed), (3, 1, 0))
        self.assertEqual((external / file_name).read_bytes(), b"existing archive")
        self.assertEqual((external / f"Foldit1 {file_name}").read_bytes(), b"first version")
        self.assertEqual((external / f"Foldit2 {file_name}").read_bytes(), b"second version")
        self.assertEqual((target_client / file_name).read_bytes(), b"first version")

    def test_batch_share_skips_each_source_client(self):
        other_client = self.root / "Foldit2"
        other_client.mkdir()
        records = []
        for client_name, client_path, number in (
            ("Foldit1", self.client, 100), ("Foldit2", other_client, 200)
        ):
            path = client_path / f"puzzle_2014362_time_{number}.ir_solution"
            path.write_bytes(client_name.encode())
            stat = path.stat()
            records.append(SaveRecord(
                str(path), client_name, str(client_path), "2014362", "2014362",
                path.name, stat.st_size, stat.st_mtime_ns, stat.st_mtime,
            ))
        clients = [self.location, ClientLocation("Foldit2", str(other_client), True)]
        report = SaveCatalog.copy_records(records, clients, skip_own_client=True)
        self.assertEqual((report.copied, report.skipped, report.failed), (2, 0, 0))
        self.assertTrue((self.client / records[1].file_name).exists())
        self.assertTrue((other_client / records[0].file_name).exists())

    def test_delete_records_removes_only_unchanged_client_save_files(self):
        first = self.client / "puzzle_2014362_time_100.ir_solution"
        changed = self.client / "puzzle_2014362_time_200.ir_solution"
        first.write_bytes(b"first")
        changed.write_bytes(b"second")
        catalog = self._catalog()
        records = catalog.scan_client("2014362", self.location)
        changed.write_bytes(b"changed since scan")
        outside = self.root / "outside.ir_solution"
        outside.write_bytes(b"outside")
        outside_stat = outside.stat()
        records.append(SaveRecord(
            str(outside), "Foldit1", str(self.client), "2014362", "2014362",
            outside.name, outside_stat.st_size, outside_stat.st_mtime_ns, outside_stat.st_mtime,
        ))
        with patch("save_catalog.delete_file", wraps=window_manager.delete_file) as delete_file:
            report = catalog.delete_records(records)
        self.assertEqual((report.deleted, report.skipped, report.failed), (1, 2, 0))
        delete_file.assert_called_once_with(str(first))
        self.assertFalse(first.exists())
        self.assertTrue(changed.exists())
        self.assertTrue(outside.exists())


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


class SaveManagerPuzzleSelectorCases(unittest.TestCase):
    def test_switches_between_running_puzzles_and_scans_all_clients(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from save_manager_qt import QtEventPump, SaveManagerWindowQt

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            clients = [
                ClientLocation("Foldit1", str(Path(folder) / "Foldit1"), True, "2817"),
                ClientLocation("Foldit10", str(Path(folder) / "Foldit10"), True, "2818b"),
                ClientLocation("Foldit14", str(Path(folder) / "Foldit14"), False, "old"),
            ]
            with (
                patch.object(QtEventPump, "ensure_started"),
                patch.object(QtEventPump, "register_window"),
                patch.object(QtEventPump, "unregister_window"),
                patch.object(SaveManagerWindowQt, "focus_window"),
                patch.object(SaveManagerWindowQt, "_scan_worker"),
            ):
                window = SaveManagerWindowQt(
                    None, "2817", lambda: clients, str(Path(folder) / "index.sqlite3"), folder,
                    initial_client_path=clients[0].path,
                )
                try:
                    choices = [window.puzzle_selector.itemText(i) for i in range(window.puzzle_selector.count())]
                    self.assertEqual(choices, ["2817", "2818b"])
                    previous_generation = window.generation
                    window.puzzle_selector.activated.emit(window.puzzle_selector.findText("2818b"))
                    self.assertEqual(window.puzzle_id, "2818b")
                    self.assertEqual(window.selected_scope, "all")
                    self.assertEqual(window.generation, previous_generation + 1)
                    self.assertEqual(window.puzzle_selector.currentText(), "2818b")
                    clients[1] = ClientLocation("Foldit10", clients[1].path, True, "2819")
                    window.refresh()
                    refreshed = [window.puzzle_selector.itemText(i) for i in range(window.puzzle_selector.count())]
                    self.assertEqual(refreshed, ["2817", "2818b", "2819"])
                finally:
                    window.close()
        self.assertIsNotNone(app)


class SaveManagerSelectionAndDeleteCases(unittest.TestCase):
    def test_copy_dialog_accepts_external_folder_and_rejects_missing_destination(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
        from save_manager_qt import CopyTargetsDialog

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            dialog = CopyTargetsDialog(None, [ClientLocation("Foldit9", folder, True)])
            with patch.object(QMessageBox, "warning") as warning:
                dialog.accept()
            warning.assert_called_once()
            with patch.object(QFileDialog, "getExistingDirectory", return_value=folder):
                dialog._browse_external_folder()
            self.assertEqual(dialog.external_path_edit.text(), folder)
            dialog.accept()
            self.assertEqual(dialog.external_folder, folder)
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
            dialog.close()
        self.assertIsNotNone(app)

    def test_batch_export_keeps_existing_pdb_and_makes_distinct_names(self):
        from save_manager_qt import export_records_to_pdb

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            existing = root / "2815 Foldit9 Same name.pdb"
            existing.write_text("existing", encoding="utf-8")
            records = []
            for number in (1, 2):
                path = root / f"puzzle_2014388_time_{number}.ir_solution"
                path.write_bytes(str(number).encode())
                stat = path.stat()
                records.append(SaveRecord(
                    str(path), "Foldit9", str(root), "2815", "2014388",
                    path.name, stat.st_size, stat.st_mtime_ns, stat.st_mtime,
                    save_name="Same name", metadata_loaded=True,
                ))

            def fake_export(source, destination):
                Path(destination).write_bytes(Path(source).read_bytes())
                return destination

            report = export_records_to_pdb(records, "2815", folder, fake_export)
            self.assertEqual((report.exported, report.failed), (2, 0))
            self.assertEqual(existing.read_text(encoding="utf-8"), "existing")
            self.assertEqual((root / "2815 Foldit9 Same name (2).pdb").read_bytes(), b"1")
            self.assertEqual((root / "2815 Foldit9 Same name (3).pdb").read_bytes(), b"2")

    def test_select_all_targets_filtered_rows_and_delete_requires_confirmation(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox
        from save_manager_qt import QtEventPump, SaveManagerWindowQt

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            client_dir = Path(folder) / "Foldit9"
            client_dir.mkdir()
            client = ClientLocation("Foldit9", str(client_dir), True, "2815")
            other_dir = Path(folder) / "Foldit10"
            other_dir.mkdir()
            other = ClientLocation("Foldit10", str(other_dir), True, "2815")
            records = []
            for index, save_name in enumerate(("Keep one", "Keep two", "Other"), 1):
                path = client_dir / f"puzzle_2014388_time_{index}.ir_solution"
                path.write_bytes(save_name.encode())
                stat = path.stat()
                records.append(SaveRecord(
                    str(path), "Foldit9", str(client_dir), "2815", "2014388",
                    path.name, stat.st_size, stat.st_mtime_ns, stat.st_mtime,
                    save_name=save_name, base_score=28121.0, total_score=28121.0,
                    solution_mode="evolver", mode_player_id=415886,
                    mode_reference_base_score=28105.0, metadata_loaded=True,
                ))
            with (
                patch.object(QtEventPump, "ensure_started"),
                patch.object(QtEventPump, "register_window"),
                patch.object(QtEventPump, "unregister_window"),
                patch.object(SaveManagerWindowQt, "_scan_worker"),
            ):
                window = SaveManagerWindowQt(
                    None, "2815", lambda: [client, other], str(Path(folder) / "index.sqlite3"), folder,
                )
                try:
                    window.records_by_client[normalize_path(client.path)] = records
                    window.name_filter.setText("Keep")
                    window._apply_filters()
                    self.assertEqual(len(window.visible_records), 2)
                    window.select_all_button.click()
                    self.assertEqual(len(window._selected_records()), 2)
                    window._refresh_table()
                    self.assertEqual(len(window._selected_records()), 2)
                    self.assertTrue(window.copy_button.isEnabled())
                    self.assertTrue(window.share_button.isEnabled())
                    self.assertTrue(window.export_button.isEnabled())
                    self.assertFalse(window.folder_button.isEnabled())
                    self.assertEqual(window.save_table.item(0, 4).text(), "Evolver")
                    self.assertEqual(window.save_table.item(0, 5).text(), "415886")
                    self.assertEqual(window.save_table.item(0, 6).text(), "28105.000")
                    self.assertEqual(window.save_table.item(0, 7).text(), "+16.000")

                    from save_manager_qt import CopyTargetsDialog
                    with (
                        patch.object(CopyTargetsDialog, "selected_destinations", return_value=([], folder)),
                        patch.object(window, "_start_copy") as start_copy,
                    ):
                        window.copy_button.click()
                    self.assertEqual(len(start_copy.call_args.args[0]), 2)
                    self.assertEqual(start_copy.call_args.args[2], folder)
                    with patch.object(window, "_start_copy") as start_share:
                        window.share_button.click()
                    self.assertEqual(len(start_share.call_args.args[0]), 2)
                    self.assertTrue(start_share.call_args.kwargs["skip_own_client"])

                    from save_manager_qt import PdbExportItemResult, PdbExportReport
                    export_report = PdbExportReport([
                        PdbExportItemResult(records[0].path, str(Path(folder) / "one.pdb")),
                        PdbExportItemResult(records[1].path, str(Path(folder) / "two.pdb")),
                    ])
                    with (
                        patch("save_manager_qt.threading.Thread") as export_thread,
                        patch("save_manager_qt.export_records_to_pdb", return_value=export_report) as exporter,
                        patch.object(QMessageBox, "information") as information,
                    ):
                        window.export_button.click()
                        export_thread.call_args.kwargs["target"]()
                        window._drain_events()
                    self.assertEqual(len(exporter.call_args.args[0]), 2)
                    self.assertIn("Exported: 2", information.call_args.args[2])
                    self.assertTrue(window.export_button.isEnabled())

                    with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Cancel):
                        window.delete_button.click()
                    self.assertTrue(all(Path(record.path).exists() for record in records))

                    with (
                        patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes),
                        patch.object(QMessageBox, "information") as info,
                        patch("save_manager_qt.threading.Thread") as thread_class,
                    ):
                        window.delete_button.click()
                        thread_class.call_args.kwargs["target"]()
                        window._drain_events()
                    self.assertIn("Deleted: 2", info.call_args.args[2])
                    self.assertFalse(Path(records[0].path).exists())
                    self.assertFalse(Path(records[1].path).exists())
                    self.assertTrue(Path(records[2].path).exists())
                finally:
                    window.close()
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
