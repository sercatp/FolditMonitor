import ast
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from logger import FolditLogHandler, LogFileHandler
from stats_domain import format_score_history, format_score_latest, format_score_line
from stats_editor import StatsEditorSession
from stats_module import StatsManager, parse_numeric_score


DRW_OPEN = """<?xml version="1.0" encoding="UTF-8"?>
<Foldit:Script xmlns:Foldit="http://fold.it/scriptlog">
<Foldit:Head>
<Foldit:ScriptName>-Serca DRW 2.1.112</Foldit:ScriptName>
<Foldit:ScriptDesc></Foldit:ScriptDesc>
<Foldit:MacroID>109186</Foldit:MacroID>
<Foldit:ParentID>0</Foldit:ParentID>
</Foldit:Head>
<Foldit:ScriptOutput>
Serca DRW 2.1.112
+++Starting score 4299.941 saved to slot 1
"""

DRW_CLOSED = DRW_OPEN + """Canceled
</Foldit:ScriptOutput>
</Foldit:Script>
"""

GAB_OPEN = """<?xml version="1.0" encoding="UTF-8"?>
<Foldit:Script xmlns:Foldit="http://fold.it/scriptlog">
<Foldit:Head>
<Foldit:ScriptName>Bridge the GAB v2.1.3</Foldit:ScriptName>
<Foldit:ScriptDesc>Searches for the best set of bands to create the starting solution. Try to use as low number of bands as possible to prevent rigidity.  </Foldit:ScriptDesc>
<Foldit:MacroID>109364</Foldit:MacroID>
<Foldit:ParentID>0</Foldit:ParentID>
</Foldit:Head>
<Foldit:ScriptOutput>
2.1.3
GA bands started. Segments=70
Start score: 4299.942
Cys setup: detected=7 | free=7 | free pairings=105 | existing Cys-Cys bands=0 | rotamer-excluded=0 | disulfide_bonus=false
"""

APPEND_SCORE = "score bump 4300.500\n"


def write_windows_text(path: str, text: str):
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(text)


def append_windows_text(path: str, text: str):
    with open(path, "a", encoding="utf-8", newline="\r\n") as f:
        f.write(text)


def make_logger_settings():
    return {
        "MAX_LINES": 400,
        "tooltip_lines": 15,
        "EXCLUSION_CRITERIA": [],
        "SCRIPT_TYPE_MAPPING": {
            "drw": {"name": "DRW", "column_number": 0},
            "gab": {"name": "GAB", "column_number": 0},
        },
        "SCRIPT_TYPE_FALLBACK_MAX_LENGTH": 10,
        "CHECK_INTERVAL": 2,
        "managed_log_exports": True,
        "SCORE_PATTERNS": [
            re.compile(r"\b\d{4,6}\.\d+\b"),
            re.compile(r"\b\d{4,6}\b"),
        ],
    }


def make_stats_manager(temp_dir: str, script_type_mapping=None) -> StatsManager:
    return StatsManager(
        temp_dir,
        {
            "logging": {
                "logs_folder": "logs",
                "stats_save_interval_minutes": 30,
                "stats_score_decimals": 0,
            },
            "script_type_mapping": dict(script_type_mapping or {}),
        },
    )


MONITOR_SOURCE_PATH = Path(__file__).resolve().parents[1] / "Foldit Monitor.pyw"


def build_check_client_changes_function():
    source = MONITOR_SOURCE_PATH.read_text(encoding="utf-8-sig")
    module = ast.parse(source, filename=str(MONITOR_SOURCE_PATH))
    check_node = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "check_client_changes"
    )
    namespace = {
        "__builtins__": __builtins__,
        "os": os,
        "parse_numeric_score": parse_numeric_score,
    }
    exec(
        compile(ast.Module(body=[check_node], type_ignores=[]), str(MONITOR_SOURCE_PATH), "exec"),
        namespace,
    )
    return namespace


def build_get_puzzle_number_function():
    source = MONITOR_SOURCE_PATH.read_text(encoding="utf-8-sig")
    module = ast.parse(source, filename=str(MONITOR_SOURCE_PATH))
    regex_node = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PUZZLE_ID_RE" for target in node.targets)
    )
    function_node = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "get_puzzle_number"
    )
    namespace = {
        "__builtins__": __builtins__,
        "re": re,
    }
    exec(
        compile(ast.Module(body=[regex_node, function_node], type_ignores=[]), str(MONITOR_SOURCE_PATH), "exec"),
        namespace,
    )
    return namespace["get_puzzle_number"]


def build_get_post_copy_shortcut_function():
    source = MONITOR_SOURCE_PATH.read_text(encoding="utf-8-sig")
    module = ast.parse(source, filename=str(MONITOR_SOURCE_PATH))
    function_node = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "get_post_copy_shortcut"
    )
    namespace = {
        "__builtins__": __builtins__,
    }
    exec(
        compile(ast.Module(body=[function_node], type_ignores=[]), str(MONITOR_SOURCE_PATH), "exec"),
        namespace,
    )
    return namespace["get_post_copy_shortcut"]


class FakeProcess:
    def __init__(self, pid: int, exe_path: str):
        self.pid = pid
        self._exe_path = exe_path

    def exe(self):
        return self._exe_path


class FakeProcessTree:
    def __init__(self):
        self.rows = {}

    def exists(self, item_id: str) -> bool:
        return item_id in self.rows

    def item(self, item_id: str, key: str):
        return self.rows[item_id][key]


class PollingLogHandler:
    def __init__(self, settings):
        self.settings = settings
        self.current_handlers = {}
        self.export_calls = []

    def start_monitoring(self, file_path: str):
        handler = self.current_handlers.get(file_path)
        if handler is None:
            handler = LogFileHandler(self.settings, file_path)
            self.current_handlers[file_path] = handler
        handler._update_data()
        return handler

    def stop_monitoring(self, file_path: str):
        handler = self.current_handlers.pop(file_path, None)
        if handler is not None:
            handler.stop()

    def export_log(self, folder_path: str, open_file: bool = True, puzzle_id=None):
        self.export_calls.append(
            {
                "folder_path": folder_path,
                "open_file": open_file,
                "puzzle_id": puzzle_id,
            }
        )


class MonitorIntegrationHarness:
    def __init__(self, temp_dir: str):
        self.temp_dir = temp_dir
        self.settings_manager = SimpleNamespace(FILENAME="Foldit.exe")
        self.stats_manager = make_stats_manager(temp_dir)
        self.foldit_log_handler = PollingLogHandler(make_logger_settings())
        self.process_tree = FakeProcessTree()
        self.monitored_processes = {}
        self.file_processes = []
        self.refresh_count = 0

        self.namespace = build_check_client_changes_function()
        self.namespace.update(
            {
                "settings_manager": self.settings_manager,
                "foldit_log_handler": self.foldit_log_handler,
                "stats_manager": self.stats_manager,
                "monitored_processes": self.monitored_processes,
                "process_tree": self.process_tree,
                "get_file_processes": self.get_file_processes,
                "refresh_stats_puzzle_menu": self.refresh_stats_puzzle_menu,
            }
        )
        self.check_client_changes = self.namespace["check_client_changes"]

    def get_file_processes(self, _filename):
        return list(self.file_processes)

    def refresh_stats_puzzle_menu(self):
        self.refresh_count += 1

    def set_processes(self, processes):
        self.file_processes = list(processes)

    def restart_log_handler(self):
        self.foldit_log_handler = PollingLogHandler(make_logger_settings())
        self.namespace["foldit_log_handler"] = self.foldit_log_handler

    def build_clients(self):
        clients = []
        for process in self.file_processes:
            try:
                exe_path = process.exe()
            except Exception:
                continue
            folder = os.path.dirname(exe_path)
            if not folder:
                continue
            clients.append(
                SimpleNamespace(
                    pid=process.pid,
                    folder=folder,
                    client_name=os.path.basename(folder),
                )
            )
        return clients

    def run(self):
        self.check_client_changes(self.build_clients())


class PuzzleTitleParsingCases(unittest.TestCase):
    def setUp(self):
        self.get_puzzle_number = build_get_puzzle_number_function()

    def test_parses_alpha_suffix_before_title_separator(self):
        self.assertEqual(
            self.get_puzzle_number("Foldit - 2764b: electron density 169"),
            "2764b",
        )

    def test_parses_plain_numeric_id_before_title_separator(self):
        self.assertEqual(
            self.get_puzzle_number("Foldit - 2764: electron density 169"),
            "2764",
        )

    def test_uses_first_matching_token(self):
        self.assertEqual(
            self.get_puzzle_number("Foldit 2026 - 2764b: electron density 169"),
            "2026",
        )

    def test_empty_title_has_no_puzzle_id(self):
        self.assertIsNone(self.get_puzzle_number(""))
        self.assertIsNone(self.get_puzzle_number(None))

    def test_short_number_does_not_block_later_valid_id(self):
        self.assertEqual(
            self.get_puzzle_number("Foldit - 12: puzzle 2764b"),
            "2764b",
        )


class PostCopyShortcutCases(unittest.TestCase):
    def setUp(self):
        self.get_post_copy_shortcut = build_get_post_copy_shortcut_function()

    def test_same_puzzle_keeps_load_shortcut(self):
        self.assertEqual(self.get_post_copy_shortcut("1234", "1234"), "ctrl+o")

    def test_different_puzzles_use_puzzle_picker_shortcut(self):
        self.assertEqual(self.get_post_copy_shortcut("1234", "5678"), "ctrl+p")

    def test_missing_target_puzzle_falls_back_to_load_shortcut(self):
        self.assertEqual(self.get_post_copy_shortcut("1234", None), "ctrl+o")


class LoggerRewriteCases(unittest.TestCase):
    def test_closed_drw_then_gab_rewrite_switches_script_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_OPEN)
            self.assertEqual(os.path.getsize(path), 393)

            handler = LogFileHandler(make_logger_settings(), path)
            handler._update_data()
            handler.consume_stats_events()

            data = handler.get_data()
            self.assertEqual(data["script_name"], "-Serca DRW 2.1.112")
            self.assertEqual(data["script_type"], "DRW")
            self.assertTrue(data["run_open"])
            self.assertAlmostEqual(data["highest_score"], 4299.941, places=3)
            self.assertAlmostEqual(data["script_highest_score"], 4299.941, places=3)

            write_windows_text(path, DRW_CLOSED)
            self.assertEqual(os.path.getsize(path), 445)
            handler._update_data()
            handler.consume_stats_events()

            data = handler.get_data()
            self.assertEqual(data["script_type"], "DRW")
            self.assertFalse(data["run_open"])
            self.assertAlmostEqual(data["highest_score"], 4299.941, places=3)
            self.assertAlmostEqual(data["script_highest_score"], 4299.941, places=3)

            write_windows_text(path, GAB_OPEN)
            self.assertEqual(os.path.getsize(path), 654)
            handler._update_data()

            data = handler.get_data()
            self.assertEqual(data["script_name"], "Bridge the GAB v2.1.3")
            self.assertEqual(data["script_type"], "GAB")
            self.assertTrue(data["run_open"])
            self.assertAlmostEqual(data["highest_score"], 4299.942, places=3)
            self.assertAlmostEqual(data["script_highest_score"], 4299.942, places=3)
            self.assertEqual(
                handler.consume_stats_events(),
                [
                    {
                        "kind": "script",
                        "script": "GAB",
                        "score": 4299.942,
                        "continue_tail": False,
                    }
                ],
            )

    def test_direct_gab_rewrite_without_close_also_switches_script_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_OPEN)
            self.assertEqual(os.path.getsize(path), 393)

            handler = LogFileHandler(make_logger_settings(), path)
            handler._update_data()
            handler.consume_stats_events()

            write_windows_text(path, GAB_OPEN)
            self.assertEqual(os.path.getsize(path), 654)
            handler._update_data()

            data = handler.get_data()
            self.assertEqual(data["script_name"], "Bridge the GAB v2.1.3")
            self.assertEqual(data["script_type"], "GAB")
            self.assertTrue(data["run_open"])
            self.assertAlmostEqual(data["highest_score"], 4299.942, places=3)
            self.assertEqual(
                handler.consume_stats_events(),
                [
                    {
                        "kind": "script",
                        "script": "GAB",
                        "score": 4299.942,
                        "continue_tail": False,
                    }
                ],
            )

    def test_same_script_append_keeps_drw_run_and_updates_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_OPEN)
            handler = LogFileHandler(make_logger_settings(), path)
            handler._update_data()
            handler.consume_stats_events()

            append_windows_text(path, APPEND_SCORE)
            self.assertGreater(os.path.getsize(path), 393)
            handler._update_data()

            data = handler.get_data()
            self.assertEqual(data["script_type"], "DRW")
            self.assertEqual(data["script_name"], "-Serca DRW 2.1.112")
            self.assertTrue(data["run_open"])
            self.assertAlmostEqual(data["highest_score"], 4300.5, places=3)
            self.assertAlmostEqual(data["script_highest_score"], 4300.5, places=3)
            self.assertEqual(data["script_change_token"], 1)
            self.assertEqual(
                handler.consume_stats_events(),
                [
                    {
                        "kind": "script",
                        "script": "DRW",
                        "score": 4300.5,
                        "continue_tail": True,
                    }
                ],
            )


class LoggerBootstrapCases(unittest.TestCase):
    def test_new_handler_bootstraps_open_run_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_OPEN)

            first_handler = LogFileHandler(make_logger_settings(), path)
            first_handler._update_data()
            first_handler.consume_stats_events()

            second_handler = LogFileHandler(make_logger_settings(), path)
            second_handler._update_data()

            self.assertEqual(
                second_handler.consume_stats_events(),
                [
                    {
                        "kind": "script",
                        "script": "DRW",
                        "score": 4299.941,
                        "continue_tail": True,
                    }
                ],
            )
            self.assertEqual(second_handler.get_data()["script_type"], "DRW")

            second_handler._update_data()
            self.assertEqual(second_handler.consume_stats_events(), [])

    def test_truncated_log_then_same_script_starts_new_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_OPEN)

            handler = LogFileHandler(make_logger_settings(), path)
            handler._update_data()
            handler.consume_stats_events()

            write_windows_text(path, "")
            handler._update_data()
            self.assertFalse(handler.get_data()["run_open"])
            self.assertEqual(handler.consume_stats_events(), [])

            write_windows_text(path, DRW_OPEN)
            handler._update_data()

            self.assertEqual(
                handler.consume_stats_events(),
                [
                    {
                        "kind": "script",
                        "script": "DRW",
                        "score": 4299.941,
                        "continue_tail": False,
                    }
                ],
            )
            self.assertTrue(handler.get_data()["run_open"])
            self.assertEqual(handler.get_data()["script_change_token"], 1)

    def test_finish_event_emitted_when_open_run_closes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_OPEN)

            handler = LogFileHandler(make_logger_settings(), path)
            handler._update_data()
            handler.consume_stats_events()

            write_windows_text(path, DRW_CLOSED)
            handler._update_data()

            self.assertFalse(handler.get_data()["run_open"])
            self.assertIn({"kind": "finish"}, handler.consume_stats_events())

    def test_finish_event_not_emitted_for_bootstrap_closed_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_CLOSED)

            handler = LogFileHandler(make_logger_settings(), path)
            handler._update_data()

            self.assertFalse(handler.get_data()["run_open"])
            self.assertNotIn({"kind": "finish"}, handler.consume_stats_events())


class ManagedLogExportCases(unittest.TestCase):
    @staticmethod
    def _build_handler(temp_dir: str, settings=None):
        settings = settings or make_logger_settings()
        script_path = os.path.join(temp_dir, "scriptlog.default.xml")
        handler = LogFileHandler(settings, script_path)
        handler._update_data()
        log_handler = FolditLogHandler(settings)
        log_handler.current_handlers[script_path] = handler
        return log_handler, handler, script_path

    def test_managed_export_reuses_unchanged_partial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            write_windows_text(os.path.join(temp_dir, "scriptlog.default.xml"), DRW_OPEN)
            log_handler, _handler, _script_path = self._build_handler(temp_dir)

            first_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")
            second_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")

            self.assertEqual(first_path, second_path)
            self.assertTrue(os.path.exists(first_path))
            self.assertTrue(first_path.endswith(".part.txt"))

    def test_managed_export_replaces_changed_partial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(script_path, DRW_OPEN)
            log_handler, handler, _script_path = self._build_handler(temp_dir)

            first_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")
            append_windows_text(script_path, APPEND_SCORE)
            handler._update_data()
            second_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")

            self.assertNotEqual(first_path, second_path)
            self.assertFalse(os.path.exists(first_path))
            self.assertTrue(os.path.exists(second_path))
            self.assertTrue(second_path.endswith(".part.txt"))

    def test_managed_final_export_removes_remembered_partial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(script_path, DRW_OPEN)
            log_handler, handler, _script_path = self._build_handler(temp_dir)

            partial_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")
            write_windows_text(script_path, DRW_CLOSED)
            handler._update_data()
            final_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")

            self.assertFalse(os.path.exists(partial_path))
            self.assertTrue(os.path.exists(final_path))
            self.assertTrue(final_path.endswith(".fin.txt"))

    def test_managed_export_reuses_final_after_finish(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(script_path, DRW_CLOSED)
            log_handler, _handler, _script_path = self._build_handler(temp_dir)

            first_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")
            second_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")

            self.assertEqual(first_path, second_path)
            self.assertTrue(first_path.endswith(".fin.txt"))

    def test_disabled_managed_export_uses_legacy_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = make_logger_settings()
            settings["managed_log_exports"] = False
            write_windows_text(os.path.join(temp_dir, "scriptlog.default.xml"), DRW_OPEN)
            log_handler, _handler, _script_path = self._build_handler(temp_dir, settings=settings)

            export_path = log_handler.export_log(temp_dir, open_file=False, puzzle_id="1234")

            self.assertTrue(export_path.endswith(".txt"))
            self.assertFalse(export_path.endswith(".part.txt"))
            self.assertFalse(export_path.endswith(".fin.txt"))


class PuzzleSwitchCases(unittest.TestCase):
    def test_switching_puzzle_does_not_reuse_cached_script_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "scriptlog.default.xml")
            write_windows_text(path, DRW_OPEN)

            handler = LogFileHandler(make_logger_settings(), path)
            handler._update_data()

            manager = make_stats_manager(temp_dir)
            manager.touch_client("client1", "1234")

            for event in handler.consume_stats_events():
                if str(event.get("kind", "")).strip().lower() == "script":
                    manager.handle_monitor_update(
                        "client1",
                        "1234",
                        event.get("script"),
                        event.get("score"),
                        continue_tail=bool(event.get("continue_tail", True)),
                    )

            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )

            manager.touch_client("client1", "5678")
            handler._update_data()

            # The cached snapshot still exists, but the event queue stays empty, so the new puzzle
            # should not inherit the old script until the file produces a new log event.
            self.assertEqual(handler.consume_stats_events(), [])
            self.assertEqual(manager.get_entries_by_client("5678")["client1"], [])

            write_windows_text(path, GAB_OPEN)
            handler._update_data()

            for event in handler.consume_stats_events():
                if str(event.get("kind", "")).strip().lower() == "script":
                    manager.handle_monitor_update(
                        "client1",
                        "5678",
                        event.get("script"),
                        event.get("score"),
                        continue_tail=bool(event.get("continue_tail", True)),
                    )
                elif str(event.get("kind", "")).strip().lower() == "state":
                    manager.handle_script_state_snapshot(
                        "client1",
                        "5678",
                        event.get("script"),
                        event.get("score"),
                    )

            self.assertEqual(
                manager.get_entries_by_client("5678")["client1"][0]["script"],
                "GAB",
            )
            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )


class MonitorIntegrationCases(unittest.TestCase):
    def test_check_client_changes_skips_cached_log_when_puzzle_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client_dir = os.path.join(temp_dir, "client1")
            os.makedirs(client_dir, exist_ok=True)
            script_path = os.path.join(client_dir, "scriptlog.default.xml")
            write_windows_text(script_path, DRW_OPEN)

            harness = MonitorIntegrationHarness(temp_dir)
            harness.set_processes([FakeProcess(1001, os.path.join(client_dir, "Foldit.exe"))])
            harness.monitored_processes[1001] = {"puzzle_number": 1234, "score_stale_ticks": 0}

            harness.run()
            self.assertEqual(
                harness.stats_manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )

            harness.monitored_processes[1001]["puzzle_number"] = 5678
            harness.run()

            self.assertEqual(harness.stats_manager.get_entries_by_client("5678")["client1"], [])
            self.assertEqual(
                harness.stats_manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )

    def test_check_client_changes_bootstraps_open_run_once_after_monitor_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client_dir = os.path.join(temp_dir, "client1")
            os.makedirs(client_dir, exist_ok=True)
            script_path = os.path.join(client_dir, "scriptlog.default.xml")
            write_windows_text(script_path, DRW_OPEN)

            harness = MonitorIntegrationHarness(temp_dir)
            harness.set_processes([FakeProcess(1001, os.path.join(client_dir, "Foldit.exe"))])
            harness.monitored_processes[1001] = {"puzzle_number": 1234, "score_stale_ticks": 0}

            harness.run()
            self.assertEqual(
                harness.stats_manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )

            harness.restart_log_handler()
            harness.run()

            self.assertEqual(
                harness.stats_manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )

    def test_check_client_changes_restarts_run_after_truncate_and_reopen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client_dir = os.path.join(temp_dir, "client1")
            os.makedirs(client_dir, exist_ok=True)
            script_path = os.path.join(client_dir, "scriptlog.default.xml")
            write_windows_text(script_path, DRW_OPEN)

            harness = MonitorIntegrationHarness(temp_dir)
            harness.set_processes([FakeProcess(1001, os.path.join(client_dir, "Foldit.exe"))])
            harness.monitored_processes[1001] = {"puzzle_number": 1234, "score_stale_ticks": 0}

            harness.run()
            self.assertEqual(
                harness.stats_manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )

            write_windows_text(script_path, "")
            harness.run()
            self.assertEqual(
                harness.stats_manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 4299.941}],
            )

            write_windows_text(script_path, DRW_OPEN)
            harness.run()

            self.assertEqual(
                harness.stats_manager.get_entries_by_client("1234")["client1"],
                [
                    {"script": "DRW", "score": 4299.941},
                    {"script": "DRW", "score": 4299.941},
                ],
            )

    def test_check_client_changes_exports_final_log_on_finish_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client_dir = os.path.join(temp_dir, "client1")
            os.makedirs(client_dir, exist_ok=True)
            script_path = os.path.join(client_dir, "scriptlog.default.xml")
            write_windows_text(script_path, DRW_OPEN)

            harness = MonitorIntegrationHarness(temp_dir)
            harness.set_processes([FakeProcess(1001, os.path.join(client_dir, "Foldit.exe"))])
            harness.monitored_processes[1001] = {"puzzle_number": 1234, "score_stale_ticks": 0}

            harness.run()
            self.assertEqual(harness.foldit_log_handler.export_calls, [])

            write_windows_text(script_path, DRW_CLOSED)
            harness.run()

            self.assertEqual(
                harness.foldit_log_handler.export_calls,
                [
                    {
                        "folder_path": client_dir,
                        "open_file": False,
                        "puzzle_id": "1234",
                    }
                ],
            )


class StatsCopyFinalizationCases(unittest.TestCase):
    @staticmethod
    def _seed_horizontal_copy_targets(manager: StatsManager, source_target: str = "horizontal"):
        manager.set_fin_state(
            "1234",
            [
                {"client": "source"},
                {"client": "target"},
            ],
            [],
            active_targets={
                "source": source_target,
                "target": "horizontal",
            },
        )

    def test_copy_between_horizontal_clients_writes_score_to_source_script_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(
                temp_dir,
                script_type_mapping={"drw": {"name": "DRW", "column_number": 0}},
            )
            self._seed_horizontal_copy_targets(manager)

            manager.handle_copy_saves_event(
                source_client="source",
                target_client="target",
                puzzle_id="1234",
                source_score=9711,
                source_script_type="DRW",
                source_state_script="99",
                source_state_score=4,
            )

            target_rows = [
                row
                for row in manager.get_fin_rows("1234")
                if str(row.get("client", "")).strip() == "target"
            ]
            self.assertEqual(len(target_rows), 2)
            self.assertEqual(target_rows[-1]["start_from"], "source")
            self.assertEqual(target_rows[-1]["start_score"], "")
            self.assertEqual(target_rows[-1]["cells"], {"d:drw": 9711.0})

    def test_copy_between_horizontal_clients_falls_back_to_start_score_without_source_script_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            self._seed_horizontal_copy_targets(manager)

            manager.handle_copy_saves_event(
                source_client="source",
                target_client="target",
                puzzle_id="1234",
                source_score=9711,
                source_state_script="99",
                source_state_score=4,
            )

            target_rows = [
                row
                for row in manager.get_fin_rows("1234")
                if str(row.get("client", "")).strip() == "target"
            ]
            self.assertEqual(len(target_rows), 2)
            self.assertEqual(target_rows[-1]["start_from"], "source")
            self.assertEqual(target_rows[-1]["start_score"], 9711.0)
            self.assertEqual(target_rows[-1]["cells"], {})

    def test_copy_to_horizontal_target_keeps_old_behavior_when_source_is_not_horizontal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(
                temp_dir,
                script_type_mapping={"drw": {"name": "DRW", "column_number": 0}},
            )
            self._seed_horizontal_copy_targets(manager, source_target="vertical")

            manager.handle_copy_saves_event(
                source_client="source",
                target_client="target",
                puzzle_id="1234",
                source_score=9711,
                source_script_type="DRW",
                source_state_script="99",
                source_state_score=4,
            )

            target_rows = [
                row
                for row in manager.get_fin_rows("1234")
                if str(row.get("client", "")).strip() == "target"
            ]
            self.assertEqual(len(target_rows), 2)
            self.assertEqual(target_rows[-1]["start_score"], 9711.0)
            self.assertEqual(target_rows[-1]["cells"], {})

    def test_copy_between_horizontal_clients_preserves_state_on_new_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(
                temp_dir,
                script_type_mapping={"drw": {"name": "DRW", "column_number": 0}},
            )
            self._seed_horizontal_copy_targets(manager)

            manager.handle_copy_saves_event(
                source_client="source",
                target_client="target",
                puzzle_id="1234",
                source_score=9711,
                source_script_type="DRW",
                source_state_script="99",
                source_state_score=4,
            )

            target_rows = [
                row
                for row in manager.get_fin_rows("1234")
                if str(row.get("client", "")).strip() == "target"
            ]
            self.assertEqual(target_rows[-1]["state"], "99 | 4")

    def test_running_script_on_fin_row_keeps_copied_state(self):
        # `state` records the copied source snapshot. Running DRW on the fin row
        # streams per-event state snapshots and score updates; the snapshots must not
        # touch `state` (only the score column updates), and the copied value must
        # survive a save/reload.
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(
                temp_dir,
                script_type_mapping={"drw": {"name": "DRW", "column_number": 0}},
            )
            self._seed_horizontal_copy_targets(manager)

            manager.handle_copy_saves_event(
                source_client="source",
                target_client="target",
                puzzle_id="1234",
                source_score=9711,
                source_script_type="DRW",
                source_state_script="845",
                source_state_score=17,
            )

            manager.handle_script_state_snapshot("target", "1234", "1", 1)
            manager.handle_monitor_update("target", "1234", "DRW", 9712)
            manager.handle_script_state_snapshot("target", "1234", "2", 1)
            manager.save_puzzle("1234", force=True)

            reloaded = make_stats_manager(
                temp_dir,
                script_type_mapping={"drw": {"name": "DRW", "column_number": 0}},
            )
            target_rows = [
                row
                for row in reloaded.get_fin_rows("1234")
                if str(row.get("client", "")).strip() == "target"
            ]
            self.assertEqual(target_rows[-1]["state"], "845 | 17")

    def test_copy_to_source_puzzle_keeps_target_active_in_its_own_puzzle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.touch_client("source", "1234")
            manager.touch_client("target", "5678")

            manager.handle_copy_saves_event(
                source_client="source",
                target_client="target",
                puzzle_id="1234",
                source_score=9711,
            )

            self.assertEqual(manager.active_clients.get("source"), "1234")
            self.assertEqual(manager.active_clients.get("target"), "5678")
            self.assertEqual(
                manager.get_entries_by_client("1234")["target"],
                [{"script": "from source", "score": 9711.0, "kind": "copy"}],
            )
            self.assertEqual(manager.get_entries_by_client("5678")["target"], [])


class StatsActiveTargetPersistenceCases(unittest.TestCase):
    @staticmethod
    def _seed_single_horizontal_fin_row(manager: StatsManager):
        manager.set_fin_state(
            "1234",
            [{"client": "client1"}],
            [],
            active_targets={"client1": "horizontal"},
        )

    def test_delete_last_fin_row_keeps_horizontal_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            self._seed_single_horizontal_fin_row(manager)

            session = StatsEditorSession(manager, "1234")
            session.delete_fin_row(0)

            self.assertEqual(session.fin_rows, [])
            self.assertEqual(session.active_targets.get("client1"), "horizontal")

    def test_save_and_reload_preserve_horizontal_target_without_fin_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.set_fin_state(
                "1234",
                [],
                [],
                active_targets={"client1": "horizontal"},
            )
            manager.save_puzzle("1234", force=True)

            fin_path = manager.get_fin_csv_path("1234")
            self.assertTrue(os.path.exists(fin_path))

            reloaded = make_stats_manager(temp_dir)
            self.assertEqual(reloaded.get_fin_rows("1234"), [])
            self.assertEqual(reloaded.get_active_targets("1234").get("client1"), "horizontal")

    def test_startup_live_update_trusts_saved_horizontal_target_without_fin_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.set_fin_state(
                "1234",
                [],
                [],
                active_targets={"client1": "horizontal"},
            )
            manager.save_puzzle("1234", force=True)

            reloaded = make_stats_manager(temp_dir)
            reloaded.handle_monitor_update("client1", "1234", "DRW", 9652)

            self.assertEqual(reloaded.get_active_targets("1234").get("client1"), "horizontal")
            self.assertEqual(
                reloaded.get_fin_rows("1234"),
                [{"client": "client1", "state": "", "notes": "", "start_from": "", "start_score": "", "cells": {"d:drw": 9652.0}}],
            )

    def test_empty_fin_file_is_preserved_as_meta_only_when_no_horizontal_targets_remain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            self._seed_single_horizontal_fin_row(manager)
            manager.save_puzzle("1234", force=True)

            fin_path = manager.get_fin_csv_path("1234")
            self.assertTrue(os.path.exists(fin_path))

            manager.set_fin_state(
                "1234",
                [],
                [],
                active_targets={"client1": "vertical"},
            )
            manager.save_puzzle("1234", force=True)

            self.assertTrue(os.path.exists(fin_path))
            reloaded = make_stats_manager(temp_dir)
            self.assertEqual(reloaded.get_fin_rows("1234"), [])
            self.assertEqual(reloaded.get_active_targets("1234").get("client1"), "vertical")

    def test_old_fin_file_without_meta_still_uses_startup_inference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            logs_dir = os.path.join(temp_dir, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            fin_path = manager.get_fin_csv_path("1234")

            with open(fin_path, "w", encoding="utf-8", newline="") as handle:
                handle.write("client,from,state,Notes,score,DRW\n")
                handle.write("client1,,,,,9652\n")

            reloaded = make_stats_manager(temp_dir)
            reloaded.handle_monitor_update("client1", "1234", "DRW", 9700)

            self.assertEqual(reloaded.get_active_targets("1234").get("client1"), "vertical")
            self.assertEqual(
                reloaded.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 9700.0}],
            )

    def test_set_client_target_to_finalization_routes_future_scores_to_fin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(
                temp_dir,
                script_type_mapping={"drw": {"name": "DRW", "column_number": 0}},
            )
            manager.handle_monitor_update("client1", "1234", "DRW", 9000)
            self.assertEqual(manager.get_active_targets("1234").get("client1"), "vertical")

            self.assertTrue(manager.set_client_target("1234", "client1", "horizontal"))
            self.assertEqual(manager.get_active_targets("1234").get("client1"), "horizontal")
            # A Finalization row exists immediately so the client shows in the fin table.
            self.assertEqual([row["client"] for row in manager.get_fin_rows("1234")], ["client1"])

            # Future scores now land in Finalization, not Main.
            manager.handle_monitor_update("client1", "1234", "DRW", 9100)
            self.assertEqual(manager.get_fin_rows("1234")[-1]["cells"], {"d:drw": 9100.0})

    def test_set_client_target_to_main_routes_future_scores_to_main(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            self._seed_single_horizontal_fin_row(manager)
            manager.touch_client("client1", "1234")
            self.assertEqual(manager.get_active_targets("1234").get("client1"), "horizontal")

            self.assertTrue(manager.set_client_target("1234", "client1", "vertical"))
            self.assertEqual(manager.get_active_targets("1234").get("client1"), "vertical")

            manager.handle_monitor_update("client1", "1234", "DRW", 9700)
            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 9700.0}],
            )

    def test_set_client_target_is_idempotent_and_reports_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.touch_client("client1", "1234")  # defaults to vertical
            self.assertFalse(manager.set_client_target("1234", "client1", "vertical"))
            self.assertTrue(manager.set_client_target("1234", "client1", "horizontal"))
            self.assertFalse(manager.set_client_target("1234", "client1", "horizontal"))


class StatsFinalizationHistoryCases(unittest.TestCase):
    SCRIPT_MAPPING = {
        "h3": {"name": "H3", "column_number": 3},
        "h4": {"name": "H4", "column_number": 4},
        "h10": {"name": "H10", "column_number": 10},
    }

    @staticmethod
    def _column_keys(manager: StatsManager, puzzle_id: str = "1234"):
        return [column["key"] for column in manager.get_fin_columns(puzzle_id)]

    def test_highlight_updates_use_primary_slots_without_bridge_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            manager.set_fin_state(
                "1234",
                [{"client": "client1", "cells": {"h:3": 100}}],
                manager.table_domain.default_numbered_columns,
                active_targets={"client1": "horizontal"},
            )

            manager.handle_monitor_update("client1", "1234", "H10", 110)
            manager.handle_monitor_update("client1", "1234", "H4", 104)

            row = manager.get_fin_rows("1234")[0]
            self.assertEqual(row["cells"]["h:3"], 100.0)
            self.assertEqual(row["cells"]["h:4"], 104.0)
            self.assertEqual(row["cells"]["h:10"], 110.0)
            keys = self._column_keys(manager)
            self.assertNotIn("h:4#2", keys)
            self.assertFalse(any(key.startswith("bridge:") for key in keys))

    def test_repeated_updates_for_current_highlight_replace_latest_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            manager.set_fin_state(
                "1234",
                [{"client": "client1"}],
                [],
                active_targets={"client1": "horizontal"},
            )

            manager.handle_monitor_update("client1", "1234", "H4", 104)
            manager.handle_monitor_update("client1", "1234", "H4", 104.4)
            manager.handle_monitor_update("client1", "1234", "H4", 105)

            row = manager.get_fin_rows("1234")[0]
            # The run keeps where it started; the grid shows the end (105).
            self.assertEqual(row["cells"]["h:4"], "104→105")

    def test_returning_to_previous_highlight_appends_once_then_replaces_latest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            manager.set_fin_state(
                "1234",
                [{"client": "client1", "cells": {"h:4": 100, "h:10": 110}}],
                manager.table_domain.default_numbered_columns,
                active_targets={"client1": "horizontal"},
            )

            manager.handle_monitor_update("client1", "1234", "H4", 104)
            manager.handle_monitor_update("client1", "1234", "H4", 105)

            row = manager.get_fin_rows("1234")[0]
            # Returning to h:4 appends a new run line; that run keeps its start (104).
            self.assertEqual(row["cells"]["h:4"], "100\n104→105")
            self.assertEqual(row["cells"]["h:10"], 110.0)

    def test_copy_then_running_next_highlight_replaces_latest_in_target_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            manager.set_fin_state(
                "1234",
                [{"client": "source"}, {"client": "target"}],
                [],
                active_targets={"source": "horizontal", "target": "horizontal"},
            )

            manager.handle_copy_saves_event(
                source_client="source",
                target_client="target",
                puzzle_id="1234",
                source_score=43863,
                source_script_type="H3",
            )
            manager.handle_monitor_update("target", "1234", "H4", 43864)
            manager.handle_monitor_update("target", "1234", "H4", 43866)
            manager.handle_monitor_update("target", "1234", "H4", 43870)

            target_rows = [
                row
                for row in manager.get_fin_rows("1234")
                if str(row.get("client", "")).strip() == "target"
            ]
            self.assertEqual(target_rows[-1]["cells"]["h:3"], 43863.0)
            self.assertEqual(target_rows[-1]["cells"]["h:4"], "43864→43870")

    def test_dynamic_column_in_empty_fin_row_is_first_for_live_and_to_finalization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            live_manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            live_manager.set_fin_state(
                "live",
                [{"client": "client1"}],
                [],
                active_targets={"client1": "horizontal"},
            )
            live_manager.handle_monitor_update("client1", "live", "NEW0", 101)

            edit_manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            edit_manager.set_puzzle_entries(
                "edit",
                {"client1": [{"script": "NEW0", "score": 101}]},
            )
            session = StatsEditorSession(edit_manager, "edit")
            session.move_vertical_to_fin("client1", 0, 0)

            self.assertEqual(self._column_keys(live_manager, "live")[0], "d:new0")
            self.assertEqual([column["key"] for column in session.fin_columns][0], "d:new0")
            self.assertEqual(live_manager.get_fin_rows("live")[0]["cells"], {"d:new0": 101.0})
            self.assertEqual(session.fin_rows[0]["cells"], {"d:new0": 101.0})

    def test_csv_roundtrip_preserves_score_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            manager.set_fin_state(
                "1234",
                [{"client": "client1"}],
                [],
                active_targets={"client1": "horizontal"},
            )

            manager.handle_monitor_update("client1", "1234", "H4", 104)
            manager.handle_monitor_update("client1", "1234", "H10", 110)
            manager.handle_monitor_update("client1", "1234", "H4", 105)
            # A running script's live state snapshots must not touch the fin `state`
            # cell, so they are intentionally not exercised here (see
            # StatsCopyFinalizationCases.test_running_script_on_fin_row_keeps_copied_state).
            manager.save_puzzle("1234", force=True)

            reloaded = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            row = reloaded.get_fin_rows("1234")[0]
            self.assertEqual(row["cells"]["h:4"], "104\n105")

    def test_gap_column_records_next_script_start_and_survives_reruns(self):
        gap_mapping = {
            "alpha": {"name": "Alpha", "column_number": 1},
            "gap": {"name": "", "column_number": 2},
            "beta": {"name": "Beta", "column_number": 3},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, script_type_mapping=gap_mapping)
            manager.set_fin_state(
                "1234",
                [{"client": "client1"}],
                [],
                active_targets={"client1": "horizontal"},
            )

            manager.handle_monitor_update("client1", "1234", "Alpha", 90)
            manager.handle_monitor_update("client1", "1234", "Alpha", 100)
            # An unlogged manual bump to 110; Beta then picks up at 110, so the gap
            # column (h:2) exposes the 100 -> 110 jump.
            manager.handle_monitor_update("client1", "1234", "Beta", 110)
            manager.handle_monitor_update("client1", "1234", "Beta", 130)

            row = manager.get_fin_rows("1234")[0]
            self.assertEqual(row["cells"]["h:1"], "90→100")
            self.assertEqual(row["cells"]["h:2"], 110.0)
            self.assertEqual(row["cells"]["h:3"], "110→130")

            # The gap keeps its first crossing: re-running Beta from a new start does
            # not overwrite it, and it survives a save/reload.
            manager.handle_monitor_update("client1", "1234", "Alpha", 100)
            manager.handle_monitor_update("client1", "1234", "Beta", 140)
            manager.save_puzzle("1234", force=True)

            reloaded = make_stats_manager(temp_dir, script_type_mapping=gap_mapping)
            row = reloaded.get_fin_rows("1234")[0]
            self.assertEqual(row["cells"]["h:2"], 110.0)

    def test_reordering_dynamic_column_after_gap_makes_gap_capture_it(self):
        # DRW is a Main script (column_number 0), so in Finalization it lands in a dynamic
        # d: column. The gap fill is positional: dragging DRW to sit right after the gap
        # makes the gap expose DRW's start, which the numeric capture could never do.
        mapping = {
            "cc": {"name": "CC", "column_number": 10},
            "gap": {"name": "", "column_number": 20},
            "mid": {"name": "MID", "column_number": 30},
            "drw": {"name": "DRW", "column_number": 0},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, script_type_mapping=mapping)
            manager.set_fin_state(
                "1234",
                [{"client": "client1"}],
                [],
                active_targets={"client1": "horizontal"},
            )
            manager.handle_monitor_update("client1", "1234", "CC", 100)
            manager.handle_monitor_update("client1", "1234", "DRW", 150)

            # Live insertion puts d:drw left of the gap (right after CC), so the gap has no
            # column with data to its right yet and stays empty.
            self.assertEqual(manager.get_fin_rows("1234")[0]["cells"].get("h:20", ""), "")

            session = StatsEditorSession(manager, "1234")
            order = [column["key"] for column in session.fin_columns if column["key"] != "d:drw"]
            order.insert(order.index("h:20") + 1, "d:drw")
            session.reorder_fin_columns(order)

            result_keys = [column["key"] for column in session.fin_columns]
            self.assertLess(result_keys.index("h:20"), result_keys.index("d:drw"))
            self.assertEqual(session.fin_rows[0]["cells"]["h:20"], 150.0)

    def test_reordered_fin_columns_survive_save_and_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            manager.set_fin_state(
                "1234",
                [{"client": "client1", "cells": {"h:3": 30, "h:4": 40, "h:10": 100}}],
                manager.table_domain.default_numbered_columns,
                active_targets={"client1": "horizontal"},
            )
            session = StatsEditorSession(manager, "1234")
            session.reorder_fin_columns(["h:10", "h:3", "h:4"])
            session.save(0)

            reloaded = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            self.assertEqual(
                [column["key"] for column in reloaded.get_fin_columns("1234")],
                ["h:10", "h:3", "h:4"],
            )
            # Cells follow their stable keys, not column positions.
            row = reloaded.get_fin_rows("1234")[0]
            self.assertEqual(row["cells"]["h:10"], 100.0)
            self.assertEqual(row["cells"]["h:3"], 30.0)
            self.assertEqual(row["cells"]["h:4"], 40.0)

    def test_legacy_duplicate_and_bridge_columns_are_preserved_but_not_extended(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.SCRIPT_MAPPING)
            manager.set_fin_state(
                "1234",
                [
                    {
                        "client": "client1",
                        "cells": {
                            "h:4#2": 103,
                            "bridge:h:4": 103.5,
                        },
                    }
                ],
                [
                    {"key": "h:4#2", "label": "H4", "kind": "numbered", "column_number": 4},
                    {"key": "bridge:h:4", "label": "", "kind": "bridge", "column_number": 0},
                ],
                active_targets={"client1": "horizontal"},
            )

            manager.handle_monitor_update("client1", "1234", "H4", 104)

            keys = self._column_keys(manager)
            self.assertIn("h:4#2", keys)
            self.assertIn("bridge:h:4", keys)
            self.assertEqual(keys.count("bridge:h:4"), 1)
            row = manager.get_fin_rows("1234")[0]
            self.assertEqual(row["cells"]["h:4#2"], 103.0)
            self.assertEqual(row["cells"]["bridge:h:4"], 103.5)
            self.assertEqual(row["cells"]["h:4"], 104.0)


class StatsTailCases(unittest.TestCase):
    def _seed_loaded_csv(self, manager: StatsManager):
        manager.touch_client("client1", "1234")
        manager.set_puzzle_entries(
            "1234",
            {
                "client1": [
                    {"script": "DRW", "score": 9652},
                    {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "98 | 4"},
                ]
            },
        )
        manager.save_puzzle("1234", force=True)
        manager.reload_puzzle("1234")

    def test_continue_tail_true_after_reload_keeps_same_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            self._seed_loaded_csv(manager)

            manager.handle_monitor_update("client1", "1234", "DRW", 9653, continue_tail=True)
            manager.handle_script_state_snapshot("client1", "1234", "99", 4)

            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [
                    {"script": "DRW", "score": "9652→9653"},
                    {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "99 | 4"},
                ],
            )

    def test_continue_tail_false_after_reload_starts_new_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            self._seed_loaded_csv(manager)

            manager.handle_monitor_update("client1", "1234", "DRW", 9653, continue_tail=False)
            manager.handle_script_state_snapshot("client1", "1234", "99", 4)

            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [
                    {"script": "DRW", "score": 9652.0},
                    {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "98 | 4"},
                    {"script": "DRW", "score": 9653.0},
                    {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "99 | 4"},
                ],
            )


class StatsDraftCases(unittest.TestCase):
    def test_discard_returns_current_manager_state_after_live_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.touch_client("client1", "1234")
            manager.set_puzzle_entries(
                "1234",
                {"client1": [{"script": "DRW", "score": 9652}]},
            )

            session = StatsEditorSession(manager, "1234")
            session.update_main_cell("client1", 0, "score", "9700")

            manager.handle_monitor_update("client1", "1234", "DRW", 9710)

            session.discard_unsaved_changes()

            self.assertFalse(session.ui_dirty)
            self.assertEqual(
                session.working_entries["client1"],
                [{"script": "DRW", "score": "9652→9710"}],
            )

    def test_save_overwrites_live_manager_state_with_dirty_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.touch_client("client1", "1234")
            manager.set_puzzle_entries(
                "1234",
                {"client1": [{"script": "DRW", "score": 9652}]},
            )

            session = StatsEditorSession(manager, "1234")
            session.update_main_cell("client1", 0, "score", "9700")

            manager.handle_monitor_update("client1", "1234", "DRW", 9710)

            session.save(0)

            self.assertFalse(session.ui_dirty)
            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [{"script": "DRW", "score": 9700.0}],
            )

    def test_blank_row_inserted_before_state_row_does_not_spawn_new_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.touch_client("client1", "1234")
            manager.set_puzzle_entries(
                "1234",
                {
                    "client1": [
                        {"script": "DRW", "score": 9652},
                        {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "98 | 4"},
                    ]
                },
            )

            session = StatsEditorSession(manager, "1234")
            session.add_vertical_row("client1", 0)
            session.save(0)

            manager.handle_monitor_update("client1", "1234", "DRW", 9653)
            manager.handle_script_state_snapshot("client1", "1234", "99", 4)

            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [
                    {"script": "DRW", "score": "9652→9653"},
                    {"script": "", "score": ""},
                    {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "99 | 4"},
                ],
            )

    def test_blank_row_at_tail_starts_next_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir)
            manager.touch_client("client1", "1234")
            manager.set_puzzle_entries(
                "1234",
                {
                    "client1": [
                        {"script": "DRW", "score": 9652},
                        {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "98 | 4"},
                    ]
                },
            )

            session = StatsEditorSession(manager, "1234")
            session.add_vertical_row("client1", 1)
            session.save(0)

            manager.handle_monitor_update("client1", "1234", "DRW", 9653)
            manager.handle_script_state_snapshot("client1", "1234", "99", 4)

            self.assertEqual(
                manager.get_entries_by_client("1234")["client1"],
                [
                    {"script": "DRW", "score": 9652.0},
                    {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "98 | 4"},
                    {"script": "", "score": ""},
                    {"script": "DRW", "score": 9653.0},
                    {"script": StatsManager.MAIN_STATE_SCRIPT, "score": "99 | 4"},
                ],
            )


class StatsStartEndScoreCases(unittest.TestCase):
    MAP = {"h4": {"name": "H4", "column_number": 4}}

    def test_fin_run_records_start_and_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.MAP)
            manager.set_fin_state("1", [{"client": "c"}], [], active_targets={"c": "horizontal"})

            manager.handle_monitor_update("c", "1", "H4", 9700)
            manager.handle_monitor_update("c", "1", "H4", 9710)
            manager.handle_monitor_update("c", "1", "H4", 9715)

            cell = manager.get_fin_rows("1")[0]["cells"]["h:4"]
            self.assertEqual(cell, "9700→9715")
            self.assertEqual(format_score_latest(cell, 0), "9715")        # grid shows end
            self.assertEqual(format_score_history(cell, 0), "9700→9715")  # tooltip shows pair

    def test_fin_single_tick_run_stays_plain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.MAP)
            manager.set_fin_state("1", [{"client": "c"}], [], active_targets={"c": "horizontal"})

            manager.handle_monitor_update("c", "1", "H4", 9700)

            self.assertEqual(manager.get_fin_rows("1")[0]["cells"]["h:4"], 9700.0)

    def test_fin_csv_roundtrip_preserves_start_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.MAP)
            manager.set_fin_state("1", [{"client": "c"}], [], active_targets={"c": "horizontal"})
            manager.handle_monitor_update("c", "1", "H4", 9700)
            manager.handle_monitor_update("c", "1", "H4", 9715)
            manager.save_puzzle("1", force=True)
            manager.reload_puzzle("1")

            self.assertEqual(manager.get_fin_rows("1")[0]["cells"]["h:4"], "9700→9715")

    def test_main_run_records_start_and_end_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.MAP)
            manager.touch_client("c", "1")
            manager.set_puzzle_entries("1", {"c": [{"script": "DRW", "score": 9650}]})

            manager.handle_monitor_update("c", "1", "DRW", 9660, continue_tail=True)
            self.assertEqual(
                manager.get_entries_by_client("1")["c"],
                [{"script": "DRW", "score": "9650→9660"}],
            )

            manager.save_puzzle("1", force=True)
            manager.reload_puzzle("1")
            self.assertEqual(
                manager.get_entries_by_client("1")["c"],
                [{"script": "DRW", "score": "9650→9660"}],
            )

    def test_fin_to_main_moves_full_start_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.MAP)
            manager.set_fin_state(
                "1",
                [{"client": "c", "cells": {"h:4": "9700→9715"}}],
                manager.table_domain.default_numbered_columns,
                active_targets={"c": "horizontal"},
            )

            session = StatsEditorSession(manager, "1")
            session.move_fin_cell_to_vertical(0, "h:4")

            moved = session.working_entries["c"][-1]
            self.assertEqual(moved["score"], "9700→9715")

    def test_main_to_fin_preserves_start_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.MAP)
            manager.touch_client("c", "1")
            manager.set_puzzle_entries("1", {"c": [{"script": "H4", "score": "9700→9715"}]})

            session = StatsEditorSession(manager, "1")
            session.move_vertical_to_fin("c", 0, 0)

            fin_row = next(row for row in session.fin_rows if row.get("client") == "c")
            self.assertEqual(fin_row["cells"]["h:4"], "9700→9715")

    def test_fin_raw_edit_accepts_arrow_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_stats_manager(temp_dir, self.MAP)
            manager.set_fin_state(
                "1",
                [{"client": "c", "cells": {"h:4": 50}}],
                manager.table_domain.default_numbered_columns,
                active_targets={"c": "horizontal"},
            )

            session = StatsEditorSession(manager, "1")
            session.update_fin_cell(0, "h:4", "100->120")
            self.assertEqual(session.fin_rows[0]["cells"]["h:4"], "100→120")


if __name__ == "__main__":
    unittest.main()
