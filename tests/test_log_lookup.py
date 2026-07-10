import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from log_lookup import filename_matches_log_query, find_matching_log_file


def write_file(path, text, mtime_ns):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.utime(path, ns=(mtime_ns, mtime_ns))


class LogLookupCases(unittest.TestCase):
    def test_filename_matches_client_puzzle_script_and_int_score(self):
        query = {
            "client_name": "foldit1",
            "puzzle_id": "1234",
            "script_type": "DRW",
            "score": "4300.9",
        }

        self.assertTrue(filename_matches_log_query("f1.1234 DRW.4300.20260704.120000.part.txt", query))
        self.assertFalse(filename_matches_log_query("f1.1235 DRW.4300.20260704.120000.part.txt", query))
        self.assertFalse(filename_matches_log_query("f1.1234 GAB.4300.20260704.120000.part.txt", query))
        self.assertFalse(filename_matches_log_query("f1.1234 DRW.4301.20260704.120000.part.txt", query))

    def test_find_matching_log_file_picks_newest_txt_regardless_of_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = time.time_ns()
            old_part = os.path.join(temp_dir, "f1.1234 DRW.4300.20260704.120000.part.txt")
            new_plain = os.path.join(temp_dir, "f1.1234 DRW.4300.20260704.120010.txt")
            older_fin = os.path.join(temp_dir, "f1.1234 DRW.4300.20260704.120005.fin.txt")
            write_file(old_part, "old", base)
            write_file(older_fin, "older final", base + 1_000_000_000)
            write_file(new_plain, "new", base + 2_000_000_000)

            match = find_matching_log_file(
                {
                    "client_name": "foldit1",
                    "puzzle_id": "1234",
                    "script_type": "DRW",
                    "score": "4300.1",
                },
                [temp_dir],
            )

            self.assertEqual(match, new_plain)

    def test_find_matching_log_file_uses_size_as_tiebreaker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mtime_ns = time.time_ns()
            small = os.path.join(temp_dir, "f1.1234 DRW.4300.20260704.120000.part.txt")
            large = os.path.join(temp_dir, "f1.1234 DRW.4300.20260704.120001.fin.txt")
            write_file(small, "x", mtime_ns)
            write_file(large, "larger", mtime_ns)

            match = find_matching_log_file(
                {
                    "client_name": "foldit1",
                    "puzzle_id": "1234",
                    "script_type": "DRW",
                    "score": 4300,
                },
                [temp_dir],
            )

            self.assertEqual(match, large)

    def test_find_matching_log_file_does_not_scan_recursively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            nested = os.path.join(temp_dir, "nested")
            os.mkdir(nested)
            write_file(
                os.path.join(nested, "f1.1234 DRW.4300.20260704.120000.fin.txt"),
                "nested",
                time.time_ns(),
            )

            match = find_matching_log_file(
                {
                    "client_name": "foldit1",
                    "puzzle_id": "1234",
                    "script_type": "DRW",
                    "score": "4300.9",
                },
                [temp_dir],
            )

            self.assertIsNone(match)


try:
    from stats_ui_qt import StatsWindowQt
except Exception:
    StatsWindowQt = None


@unittest.skipIf(StatsWindowQt is None, "PySide6 stats UI is unavailable")
class QtStatsLogQueryCases(unittest.TestCase):
    def test_main_query_uses_row_script_and_score(self):
        owner = SimpleNamespace(
            vertical_column_specs=[{"client": "foldit1", "type": "script"}],
            working_entries={"foldit1": [{"script": "DRW", "score": 4300.9}]},
            manager=SimpleNamespace(score_decimals=1),
            puzzle_id="1234",
            _valid_log_query=StatsWindowQt._valid_log_query,
        )

        query = StatsWindowQt.build_main_log_query(owner, 0, 0)

        self.assertEqual(query["client_name"], "foldit1")
        self.assertEqual(query["puzzle_id"], "1234")
        self.assertEqual(query["script_type"], "DRW")
        self.assertEqual(query["score"], "4300.9")

    def test_fin_query_uses_script_column_label(self):
        owner = SimpleNamespace(
            fin_column_specs=[
                {"name": "client", "label": "client"},
                {"name": "h:1", "label": "DRW", "kind": "numbered"},
            ],
            fin_rows=[{"client": "foldit1", "cells": {"h:1": "4300.9"}}],
            puzzle_id="1234",
            _valid_log_query=StatsWindowQt._valid_log_query,
            _fin_value=lambda row_idx, column_name: "4300.9",
        )

        query = StatsWindowQt.build_fin_log_query(owner, 0, 1)

        self.assertEqual(query["client_name"], "foldit1")
        self.assertEqual(query["script_type"], "DRW")
        self.assertEqual(query["score"], "4300.9")

    def test_fin_fixed_column_is_ignored(self):
        owner = SimpleNamespace(
            fin_column_specs=[{"name": "client", "label": "client"}],
            fin_rows=[{"client": "foldit1"}],
        )

        self.assertIsNone(StatsWindowQt.build_fin_log_query(owner, 0, 0))

    def test_open_matching_log_reports_not_found_in_qt_window(self):
        messages = []
        owner = SimpleNamespace(
            log_lookup_handler=lambda query: {"status": "not_found"},
            _show_info=lambda title, text: messages.append(("info", title, text)),
            _show_error=lambda title, text: messages.append(("error", title, text)),
        )
        owner._handle_log_lookup_result = lambda result: StatsWindowQt._handle_log_lookup_result(owner, result)

        StatsWindowQt.open_matching_log(owner, {"client_name": "foldit1"})

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0][0], "error")
        self.assertIn("No matching log", messages[0][2])

    def test_open_matching_log_reports_remote_request_in_qt_window(self):
        messages = []
        owner = SimpleNamespace(
            log_lookup_handler=lambda query: {"status": "remote_requested", "count": 2},
            _show_info=lambda title, text: messages.append(("info", title, text)),
            _show_error=lambda title, text: messages.append(("error", title, text)),
        )
        owner._handle_log_lookup_result = lambda result: StatsWindowQt._handle_log_lookup_result(owner, result)

        StatsWindowQt.open_matching_log(owner, {"client_name": "foldit1"})

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0][0], "info")
        self.assertIn("remote lookup", messages[0][2])

    def test_open_matching_log_ignores_non_query_cells(self):
        messages = []
        owner = SimpleNamespace(
            log_lookup_handler=lambda query: messages.append(("handler", query)),
            _show_info=lambda title, text: messages.append(("info", title, text)),
            _show_error=lambda title, text: messages.append(("error", title, text)),
        )
        owner._handle_log_lookup_result = lambda result: StatsWindowQt._handle_log_lookup_result(owner, result)

        StatsWindowQt.open_matching_log(owner, None)

        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
