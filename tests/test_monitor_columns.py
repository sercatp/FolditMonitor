import ast
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.monitor_columns import (
    COLUMN_IDS,
    column_at_x,
    default_monitor_columns,
    validate_monitor_columns,
    visible_column_ids,
)
from modules.network import NetworkManager
from modules.tooltip import TooltipWindow


class FakeTree:
    def __init__(self, values):
        self.values = values
        self.displayed = ("Puzzle", "Type", "Score", "Folder")

    def __getitem__(self, key):
        return self.displayed if key == "displaycolumns" else COLUMN_IDS

    def identify_column(self, x):
        return f"#{x}"

    def identify(self, kind, _x, _y):
        return "row-1" if kind == "item" else ""

    def get_children(self):
        return ("row-1",)

    def item(self, _item, field=None):
        data = {"values": self.values, "tags": ("client-1",)}
        return data[field] if field else data

    def configure(self, **options):
        if "displaycolumns" in options:
            self.displayed = options["displaycolumns"]


class MonitorColumnTests(unittest.TestCase):
    def test_default_order_shows_all_columns(self):
        columns = default_monitor_columns()
        self.assertEqual(visible_column_ids(columns), ("Score", "CPU", "Puzzle", "Folder", "Type"))

    def test_validation_rejects_missing_duplicate_and_fully_hidden_columns(self):
        valid = default_monitor_columns()
        validate_monitor_columns(valid)
        for bad in (
            valid[:-1],
            [{**item, "id": "CPU"} if item["id"] == "Puzzle" else item for item in valid],
            [{**item, "visible": False} for item in valid],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_monitor_columns(bad)

    def test_visual_column_maps_to_stable_id(self):
        tree = FakeTree(("9000", "12", "Cl1", "DRW", "1234"))
        self.assertEqual(column_at_x(tree, 1), "Puzzle")
        self.assertEqual(column_at_x(tree, 2), "Type")
        self.assertEqual(column_at_x(tree, 3), "Score")
        self.assertEqual(column_at_x(tree, 0), None)
        self.assertEqual(column_at_x(tree, 5), None)

    def test_network_payload_keeps_canonical_values_after_visual_reorder(self):
        values = ["9000", "12", "Cl1", "DRW", "1234"]
        manager = NetworkManager.__new__(NetworkManager)
        manager.process_tree = FakeTree(values)
        manager.get_row_payload = lambda _item: None
        manager.monitored_processes = {}
        payload = manager._get_tree_data()
        self.assertEqual(payload[0]["values"], values)
        self.assertEqual(payload[0]["values"][1], "12")
        self.assertEqual(manager._normalize_tree_items({"items": payload})[0]["values"], values)
        legacy = [{**payload[0], "values": values[:4]}]
        self.assertEqual(len(manager._normalize_tree_items({"items": legacy})[0]["values"]), 4)

    def test_hidden_columns_are_omitted_only_from_display(self):
        columns = default_monitor_columns(True)
        columns[1]["visible"] = False
        columns.insert(0, columns.pop(2))
        self.assertEqual(visible_column_ids(columns), ("Puzzle", "Score", "Folder", "Type"))

    def test_tooltip_follows_score_after_reorder(self):
        tree = FakeTree(("9000", "12", "Cl1", "DRW", "1234"))
        tree.displayed = ("Puzzle", "Type", "Score", "Folder")
        tree.winfo_pointerx = lambda: 3
        tree.winfo_rootx = lambda: 0
        tree.winfo_pointery = lambda: 1
        tree.winfo_rooty = lambda: 0
        shown = []
        tooltip = SimpleNamespace(current_tree=tree, hover_item="row-1", hover_column="Score",
                                  show=lambda *args: shown.append(args))
        TooltipWindow.check_and_show(tooltip, "log", 0, 0, "row-1")
        self.assertEqual(len(shown), 1)
        tree.winfo_pointerx = lambda: 2
        TooltipWindow.check_and_show(tooltip, "log", 0, 0, "row-1")
        self.assertEqual(len(shown), 1)

    def test_double_click_actions_follow_column_ids(self):
        source = Path(__file__).resolve().parents[1] / "modules" / "app.py"
        module = ast.parse(source.read_text(encoding="utf-8-sig"))
        method = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                      and node.name == "on_treeview_click")
        tree = FakeTree(("9000", "12", "Cl1", "DRW", "1234"))
        tree.displayed = ("Puzzle", "Type", "Score", "Folder", "CPU")
        actions = []
        namespace = {
            "time": time,
            "process_tree": tree,
            "column_at_x": column_at_x,
            "get_pid_tag": lambda _tags: 42,
            "get_folder_tag": lambda _tags: "folder-path",
            "get_cached_artifact_row": lambda _item: {
                "log_path": "log.txt", "binding_status": "resolved", "puzzle_id": "1234"
            },
            "foldit_log_handler": SimpleNamespace(export_log_file=lambda *args, **kwargs: actions.append("log")),
            "window_manager": SimpleNamespace(activate_client=lambda _pid: actions.append("activate")),
            "speed_boost": None,
            "open_folder": lambda _path: actions.append("folder"),
            "open_stats_for_puzzle": lambda _puzzle: actions.append("stats"),
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        for visual in range(1, 6):
            namespace["on_treeview_click"](SimpleNamespace(x=visual, y=1))
        self.assertEqual(actions, ["stats", "log", "log", "folder", "activate"])

    def test_live_apply_changes_both_views_without_replacing_rows_or_size_lock(self):
        source = Path(__file__).resolve().parents[1] / "modules" / "app.py"
        module = ast.parse(source.read_text(encoding="utf-8-sig"))
        method = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                      and node.name == "apply_live_settings_from_dialog")
        values = ("9000", "12", "Cl1", "DRW", "1234")
        local = FakeTree(values)
        remote = FakeTree(values[:4])
        columns = default_monitor_columns(True)
        columns.insert(0, columns.pop(2))
        widths = []
        resized = []
        root = SimpleNamespace(size_locked=False, remote_trees={"peer": SimpleNamespace(tree=remote)})
        namespace = {
            "settings_manager": SimpleNamespace(settings={"display": {"monitor_columns": columns}}),
            "root": root,
            "process_tree": local,
            "visible_column_ids": visible_column_ids,
            "adjust_column_widths": lambda tree: widths.append(tree),
            "adjust_window_size": lambda: resized.append(True),
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        changed = {("display", "monitor_columns")}
        namespace["apply_live_settings_from_dialog"](changed, {})
        self.assertEqual(local.displayed, ("Puzzle", "Score", "CPU", "Folder", "Type"))
        self.assertEqual(remote.displayed, local.displayed)
        self.assertEqual(widths, [local, remote])
        self.assertEqual(len(resized), 1)
        self.assertEqual(local.values, values)
        self.assertEqual(remote.values, values[:4])
        root.size_locked = True
        namespace["apply_live_settings_from_dialog"](changed, {})
        self.assertEqual(len(resized), 1)


if __name__ == "__main__":
    unittest.main()
