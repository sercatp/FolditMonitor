import ast
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import savefile_api as api


def save_data(*, name=True, energy=True, player=True):
    data = b"SOLN"
    if name:
        data += b"META" + struct.pack("<I", 5) + b"B2828"
    if energy:
        data += struct.pack("<IdI2d", 0, 3.0, 2, 1.0, 2.0)
    if player:
        author = b"Bruno Kestemont\x00"
        data += b"\x01" + struct.pack("<4I", 2014419, 2014419, 1, len(author)) + author
        data += b"\x00" * 13 + b"POSE"
    return data + b"RESI" + struct.pack("<I", 2) + b"residue payload"


class PartialSaveApiCases(unittest.TestCase):
    def summary(self, data, **kwargs):
        with patch.object(api, "_read_save_bytes", return_value=(Path("save.ir_solution"), data)) as read:
            result = api.get_save_summary("save.ir_solution", **kwargs)
            read.assert_called_once()
            return result

    def test_missing_energy_keeps_name_player_puzzle_and_known_bonus(self):
        result = self.summary(save_data(energy=False))
        self.assertEqual(result.save_name, "B2828")
        self.assertEqual(result.player_name, "Bruno Kestemont")
        self.assertEqual(result.puzzle_id, 2014419)
        self.assertIsNone(result.total_score)
        self.assertIsNone(result.base_score)
        self.assertEqual(result.bonus_score, 0.0)
        self.assertEqual(set(result.errors), {"total_score"})

    def test_missing_player_keeps_name_and_score(self):
        result = self.summary(save_data(player=False))
        self.assertEqual(result.save_name, "B2828")
        self.assertEqual(result.total_score, 7970.0)
        self.assertIsNone(result.player_name)
        self.assertIsNone(result.puzzle_id)
        self.assertEqual(set(result.errors), {"player_name", "puzzle_id"})

    def test_missing_meta_keeps_score_player_and_puzzle(self):
        result = self.summary(save_data(name=False))
        self.assertIsNone(result.save_name)
        self.assertEqual(result.total_score, 7970.0)
        self.assertEqual(result.player_name, "Bruno Kestemont")
        self.assertEqual(result.puzzle_id, 2014419)
        self.assertEqual(set(result.errors), {"save_name"})

    def test_only_name_can_be_read(self):
        data = b"SOLNMETA" + struct.pack("<I", 5) + b"B2828"
        with patch.object(api, "_read_save_bytes", return_value=(Path("save.ir_solution"), data)):
            result = api.get_basic_info("save.ir_solution")
            self.assertEqual(api.get_save_name("save.ir_solution"), "B2828")
            with self.assertRaises(api.FolditApiError):
                api.get_foldit_score("save.ir_solution")
        self.assertEqual(result.save_name, "B2828")
        self.assertIsNone(result.player_name)
        self.assertIsNone(result.foldit_score)
        self.assertEqual(set(result.errors), {"player_name", "foldit_score"})

    def test_single_field_queries_do_not_require_unrelated_fields(self):
        for field, data, expected in (
            ("get_foldit_score", save_data(name=False, player=False), 7970.0),
            ("get_player_name", save_data(name=False, energy=False), "Bruno Kestemont"),
        ):
            with self.subTest(field=field), patch.object(
                api, "_read_save_bytes", return_value=(Path("save.ir_solution"), data)
            ):
                self.assertEqual(getattr(api, field)("save.ir_solution"), expected)

    def test_missing_score_does_not_hide_mode(self):
        build = b"20250926-ed7de9856f-win_x64"
        mode = b"PDLT" + struct.pack("<5I", 3, 2, 447652, 988042, len(build)) + build
        result = self.summary(save_data(energy=False) + mode)
        self.assertEqual(result.solution_mode, "evolver")
        self.assertEqual(result.mode_player_id, 447652)
        self.assertEqual(result.save_name, "B2828")
        self.assertIsNone(result.total_score)

    def test_bonus_markers_remain_available_without_score_or_identity(self):
        data = b"RESI" + struct.pack("<I", 2) + b"CYS:disulfide\x00CYS:disulfide"
        result = self.summary(data)
        self.assertEqual(result.bonus_score, 250.0)
        self.assertIsNone(result.base_score)
        self.assertIsNone(result.total_score)

    def test_unreadable_data_returns_missing_fields_and_errors(self):
        for data in (b"", b"not a save", b"\x00" * 128):
            with self.subTest(data=data):
                result = self.summary(data)
                for field in ("save_name", "player_name", "puzzle_id", "base_score", "bonus_score", "total_score"):
                    self.assertIsNone(getattr(result, field))
                self.assertEqual(set(result.errors), {"save_name", "player_name", "puzzle_id", "total_score"})
                with patch.object(api, "_read_save_bytes", return_value=(Path("save.ir_solution"), data)):
                    self.assertIsNone(api.get_bonus_score("save.ir_solution"))

    def test_strict_mode_requires_core_fields_but_not_optional_mode(self):
        self.assertFalse(self.summary(save_data(), strict=True).errors)
        for data in (save_data(energy=False), save_data(name=False), save_data(player=False)):
            with self.subTest(data=data):
                for query in (api.get_basic_info, api.get_save_summary):
                    with patch.object(api, "_read_save_bytes", return_value=(Path("save.ir_solution"), data)):
                        with self.assertRaises(api.FolditApiError):
                            query("save.ir_solution", strict=True)

    def test_file_access_errors_still_raise_in_partial_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "save.ir_solution"
            for query in (api.get_basic_info, api.get_save_summary):
                with self.assertRaises(api.FolditApiError):
                    query(path)
            path.write_bytes(save_data())
            with patch.object(Path, "read_bytes", side_effect=PermissionError("locked")):
                with self.assertRaisesRegex(api.FolditApiError, "Cannot read save file"):
                    api.get_basic_info(path)


class PartialShareMenuCases(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "modules" / "app.py"
        module = ast.parse(path.read_text(encoding="utf-8-sig"))
        functions = [node for node in module.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("_read_share_info", "get_share_info", "get_share_label")]
        self.namespace = {"os": os, "threading": threading, "get_basic_info": api.get_basic_info,
                          "share_info_cache": {}}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), self.namespace)

    def test_share_displays_available_fields_and_caches_partial_data(self):
        for name, energy, player, expected in (
            (True, True, True, "Share B2828 7970.0"),
            (True, False, True, "Share B2828"),
            (True, True, False, "Share B2828 7970.0"),
            (False, True, True, "Share shared.ir_solution 7970.0"),
            (False, False, False, "Share shared.ir_solution"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "shared.ir_solution").write_bytes(save_data(name=name, energy=energy, player=player))
                for _ in range(2):
                    info = self.namespace["get_share_info"](temp)
                    self.assertTrue(info["done"])
                    self.assertEqual(self.namespace["get_share_label"](info), expected)

    def test_zero_score_is_displayed(self):
        self.assertEqual(self.namespace["get_share_label"]({"name": "Zero", "score": 0.0}), "Share Zero 0.0")

    def test_slow_read_becomes_available_on_next_menu_open(self):
        ready = threading.Event()
        def reader(_path):
            ready.wait(5)
            return api.FolditBasicInfo(None, "B2828", 23691.2071)

        self.namespace["get_basic_info"] = reader
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "shared.ir_solution").write_bytes(b"save")
            info = self.namespace["get_share_info"](temp, wait=0)
            try:
                self.assertFalse(info["done"])
            finally:
                ready.set()
                info["thread"].join(5)
            info = self.namespace["get_share_info"](temp)
            self.assertEqual(self.namespace["get_share_label"](info), "Share B2828 23691.2")
