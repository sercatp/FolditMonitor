import os
import json
import re
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from client_resolver import (
    ClientResolver,
    DirectoryLogCache,
    OpenLogBackend,
    normalize_path,
    parse_log_fallbacks,
    track_from_log_path,
)
from logger import FolditLogHandler
from settings import Settings
from window_manager import WindowManager


class FakeBackend:
    def __init__(self, mapping=None, system="Windows"):
        self.mapping = mapping or {}
        self.system = system

    def open_logs(self, process, _data_root):
        return list(self.mapping.get(int(process.pid), ()))


def fake_client(pid, data_root, title="", name="Foldit", start=None):
    process = SimpleNamespace(pid=pid)
    return SimpleNamespace(
        pid=pid,
        process=process,
        process_start_time=float(start if start is not None else 1000 + pid),
        data_root=str(data_root),
        folder=str(data_root),
        client_name=name,
        executable_path=str(Path(data_root) / "Foldit.exe"),
        installation_path=str(data_root),
        window_info=None,
        window_title=title,
        is_window_visible=False,
        is_window_focused=False,
    )


class ClientResolverCases(unittest.TestCase):
    def test_track_parser_accepts_default_named_and_empty_track(self):
        self.assertEqual(track_from_log_path("scriptlog.default.xml"), "default")
        self.assertEqual(track_from_log_path("scriptlog.2802-drw.xml"), "2802-drw")
        self.assertEqual(track_from_log_path("scriptlog..xml"), "")
        self.assertIsNone(track_from_log_path("log.txt"))

    def test_one_executable_two_tracks_become_independent_clients(self):
        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "scriptlog.drw.xml"
            second = Path(root) / "scriptlog.c-w.xml"
            first.write_text("", encoding="utf-8")
            second.write_text("", encoding="utf-8")
            backend = FakeBackend({1: [str(first)], 2: [str(second)]})
            resolver = ClientResolver(backend=backend, start_worker=False)

            result = resolver.resolve_now([
                fake_client(1, root),
                fake_client(2, root),
            ])

            self.assertEqual([item.binding_status for item in result], ["resolved", "resolved"])
            self.assertEqual({item.track for item in result}, {"drw", "c-w"})
            self.assertEqual({item.client_name for item in result}, {"Foldit [drw]", "Foldit [c-w]"})
            self.assertEqual(len({item.client_id for item in result}), 2)

    def test_same_open_log_for_two_processes_is_collision(self):
        with tempfile.TemporaryDirectory() as root:
            shared = Path(root) / "scriptlog.default.xml"
            shared.write_text("", encoding="utf-8")
            resolver = ClientResolver(
                backend=FakeBackend({1: [str(shared)], 2: [str(shared)]}),
                start_worker=False,
            )

            result = resolver.resolve_now([fake_client(1, root), fake_client(2, root)])

            self.assertEqual({item.binding_status for item in result}, {"collision"})
            self.assertEqual(len({item.client_id for item in result}), 2)

    def test_default_filename_variants_do_not_share_a_stats_name_when_both_are_live(self):
        with tempfile.TemporaryDirectory() as root:
            named_default = Path(root) / "scriptlog.default.xml"
            empty_default = Path(root) / "scriptlog..xml"
            named_default.write_text("", encoding="utf-8")
            empty_default.write_text("", encoding="utf-8")
            resolver = ClientResolver(
                backend=FakeBackend({1: [str(named_default)], 2: [str(empty_default)]}),
                start_worker=False,
            )

            result = resolver.resolve_now([fake_client(1, root), fake_client(2, root)])

            self.assertEqual(len({item.client_name for item in result}), 2)

    def test_process_moves_from_waiting_to_resolved_when_log_opens(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "scriptlog.track.xml"
            log_path.write_text("", encoding="utf-8")
            backend = FakeBackend({1: []})
            resolver = ClientResolver(backend=backend, start_worker=False)
            clients = [fake_client(1, root), fake_client(2, root)]

            waiting = resolver.resolve_now(clients)
            backend.mapping[1] = [str(log_path)]
            resolved = resolver.resolve_now(clients)

            self.assertEqual(next(item for item in waiting if item.pid == 1).binding_status, "waiting")
            bound = next(item for item in resolved if item.pid == 1)
            self.assertEqual(bound.binding_status, "resolved")
            self.assertEqual(bound.track, "track")

    def test_last_proven_track_survives_a_temporary_closed_log(self):
        with tempfile.TemporaryDirectory() as root:
            first = Path(root) / "scriptlog.first.xml"
            second = Path(root) / "scriptlog.second.xml"
            first.write_text("", encoding="utf-8")
            second.write_text("", encoding="utf-8")
            backend = FakeBackend({1: [str(first)]})
            resolver = ClientResolver(backend=backend, start_worker=False)
            clients = [fake_client(1, root), fake_client(2, root)]

            initial = resolver.resolve_now(clients)
            backend.mapping[1] = []
            closed = resolver.resolve_now(clients)
            backend.mapping[1] = [str(second)]
            switched = resolver.resolve_now(clients)

            self.assertEqual(next(item for item in initial if item.pid == 1).track, "first")
            self.assertEqual(next(item for item in closed if item.pid == 1).track, "first")
            self.assertEqual(next(item for item in switched if item.pid == 1).track, "second")

    def test_more_than_one_open_log_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / "scriptlog.a.xml", Path(root) / "scriptlog.b.xml"]
            for path in paths:
                path.write_text("", encoding="utf-8")
            resolver = ClientResolver(
                backend=FakeBackend({1: [str(path) for path in paths]}),
                start_worker=False,
            )

            result = resolver.resolve_now([fake_client(1, root)])

            self.assertEqual(result[0].binding_status, "ambiguous")
            self.assertEqual(result[0].log_path, "")

    def test_json_fallback_is_logical_client_without_pid(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "scriptlog.greg.xml"
            log_path.write_text("", encoding="utf-8")
            settings = {
                "monitoring": {
                    "log_fallbacks": [
                        {"log_path": str(log_path), "name": "Greg", "puzzle_id": "2802"}
                    ]
                }
            }
            resolver = ClientResolver(settings, backend=FakeBackend(), start_worker=False)

            result = resolver.resolve_now([])

            self.assertEqual(len(result), 1)
            self.assertIsNone(result[0].pid)
            self.assertEqual(result[0].client_name, "Greg")
            self.assertEqual(result[0].binding_source, "json")
            self.assertEqual(result[0].puzzle_id_override, "2802")

    def test_automatic_owner_merges_matching_json_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "scriptlog.greg.xml"
            log_path.write_text("", encoding="utf-8")
            settings = {"monitoring": {"log_fallbacks": [{"log_path": str(log_path), "name": "Greg"}]}}
            resolver = ClientResolver(
                settings,
                backend=FakeBackend({1: [str(log_path)]}),
                start_worker=False,
            )

            result = resolver.resolve_now([fake_client(1, root)])

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].pid, 1)
            self.assertEqual(result[0].client_name, "Greg")
            self.assertEqual(result[0].binding_source, "os")

    def test_exact_track_suffix_can_bind_json_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            log_path = Path(root) / "scriptlog.branch.xml"
            log_path.write_text("", encoding="utf-8")
            settings = {"monitoring": {"log_fallbacks": [{"log_path": str(log_path)}]}}
            resolver = ClientResolver(settings, backend=FakeBackend(), start_worker=False)

            result = resolver.resolve_now([
                fake_client(1, root, title="Foldit - 2802: Puzzle - branch"),
                fake_client(2, root, title="Foldit - 2802: Puzzle - other"),
            ])

            bound = next(item for item in result if item.pid == 1)
            self.assertEqual(bound.binding_source, "json")
            self.assertEqual(bound.track, "branch")

    def test_duplicate_json_path_is_reported_once_and_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "scriptlog.a.xml")
            errors = []
            entries = parse_log_fallbacks(
                {"monitoring": {"log_fallbacks": [{"log_path": path}, {"log_path": path}]}},
                report=errors.append,
            )
            self.assertEqual(len(entries), 1)
            self.assertEqual(len(errors), 1)

    def test_absent_monitoring_section_is_not_written_to_user_settings(self):
        with tempfile.TemporaryDirectory() as root:
            Settings(root)
            saved = json.loads((Path(root) / "Foldit Monitor.json").read_text(encoding="utf-8"))
            self.assertNotIn("monitoring", saved)


class ResolverBackendCases(unittest.TestCase):
    def test_directory_cache_does_not_rescan_unchanged_root(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "scriptlog.default.xml").write_text("", encoding="utf-8")
            cache = DirectoryLogCache(rescan_seconds=60)
            real_scandir = os.scandir
            calls = []

            def counting_scandir(path):
                calls.append(path)
                return real_scandir(path)

            with patch("client_resolver.os.scandir", side_effect=counting_scandir):
                first = cache.candidates(root, now=1)
                second = cache.candidates(root, now=2)

            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)

    def test_lsof_machine_output_is_filtered_to_the_data_root(self):
        with tempfile.TemporaryDirectory() as root:
            inside = str(Path(root) / "scriptlog.track with space.xml")
            outside = str(Path(root).parent / "scriptlog.other.xml")
            completed = SimpleNamespace(stdout=f"p123\nfcwd\nn{root}\nftxt\nn{inside}\nftxt\nn{outside}\n")
            backend = OpenLogBackend(system="Darwin")
            with patch("client_resolver.subprocess.run", return_value=completed) as run:
                result = backend._macos_open_logs(123, root)

            self.assertEqual(result, [normalize_path(inside)])
            self.assertEqual(run.call_args.args[0][:5], ["/usr/sbin/lsof", "-a", "-p", "123", "-Fn"])


class MacWindowBackendCases(unittest.TestCase):
    def test_macos_layout_uses_app_resources(self):
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "Foldit.app" / "Contents" / "MacOS" / "Foldit"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            manager = WindowManager()
            manager.system = "Darwin"

            installation, data_root, name = manager.resolve_client_layout(str(executable))

            self.assertEqual(installation, os.path.realpath(str(Path(root) / "Foldit.app")))
            self.assertEqual(data_root, os.path.realpath(str(Path(root) / "Foldit.app" / "Contents" / "Resources")))
            self.assertEqual(name, "Foldit")

    def test_quartz_keeps_offscreen_window_with_empty_title(self):
        class Quartz:
            kCGWindowListOptionAll = 0
            kCGNullWindowID = 0
            kCGWindowOwnerPID = "pid"
            kCGWindowName = "title"
            kCGWindowOwnerName = "owner"
            kCGWindowNumber = "number"
            kCGWindowLayer = "layer"
            kCGWindowIsOnscreen = "onscreen"
            kCGWindowBounds = "bounds"

            @staticmethod
            def CGWindowListCopyWindowInfo(_options, _window):
                return [{
                    "pid": 42,
                    "title": "",
                    "owner": "Foldit",
                    "number": 7,
                    "layer": 0,
                    "onscreen": False,
                    "bounds": {"Width": 1200, "Height": 800},
                }]

        manager = WindowManager()
        manager.system = "Darwin"
        manager.AppKit = object()
        manager.Quartz = Quartz
        manager._refresh_macos_window_cache()

        windows = manager._get_macos_process_windows(42)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][1], "")
        self.assertFalse(manager.is_window_visible(windows[0]))

    def test_activation_targets_the_requested_process(self):
        calls = []

        class Application:
            def activateWithOptions_(self, options):
                calls.append(("activate", options))
                return True

        class RunningApplication:
            @staticmethod
            def runningApplicationWithProcessIdentifier_(pid):
                calls.append(("pid", pid))
                return Application()

        manager = WindowManager()
        manager.system = "Darwin"
        manager.AppKit = SimpleNamespace(
            NSRunningApplication=RunningApplication,
            NSApplicationActivateAllWindows=17,
        )

        self.assertTrue(manager.activate_client(42))
        self.assertEqual(calls, [("pid", 42), ("activate", 17)])

    def test_macos_install_discovery_and_launch_use_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            app = Path(root) / "Foldit.app"
            executable = app / "Contents" / "MacOS" / "Foldit"
            resources = app / "Contents" / "Resources"
            executable.parent.mkdir(parents=True)
            resources.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            manager = WindowManager()
            manager.system = "Darwin"

            found = manager.find_installation_folders([root])
            with patch("window_manager.subprocess.Popen") as popen:
                launched = manager.launch_client(str(resources))

            self.assertEqual(found, [os.path.realpath(str(resources))])
            self.assertEqual(launched, os.path.realpath(str(app)))
            popen.assert_called_once_with(["/usr/bin/open", "-n", os.path.realpath(str(app))])


class PathFirstLoggerCases(unittest.TestCase):
    @staticmethod
    def logger_settings():
        return {
            "MAX_LINES": 50,
            "tooltip_lines": 10,
            "EXCLUSION_CRITERIA": [],
            "SCRIPT_TYPE_MAPPING": {"drw": {"name": "DRW", "column_number": 0}},
            "SCRIPT_TYPE_FALLBACK_MAX_LENGTH": 10,
            "CHECK_INTERVAL": 2,
            "managed_log_exports": True,
            "SCORE_PATTERNS": [re.compile(r"\b\d{4,6}\.\d+\b")],
        }

    @staticmethod
    def closed_log():
        return """<?xml version="1.0" encoding="UTF-8"?>
<Foldit:Script xmlns:Foldit="http://fold.it/scriptlog">
<Foldit:Head><Foldit:ScriptName>Serca DRW</Foldit:ScriptName></Foldit:Head>
<Foldit:ScriptOutput>Starting score 4299.9\nCanceled\n</Foldit:ScriptOutput>
</Foldit:Script>
"""

    def test_named_track_is_part_of_export_client_stem(self):
        stem = FolditLogHandler._source_client_name(
            os.path.join("tmp", "Foldit"),
            os.path.join("tmp", "Foldit", "scriptlog.drw.xml"),
        )
        self.assertEqual(stem, "Foldit[drw]")

    def test_export_log_file_uses_the_exact_named_track_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "scriptlog.drw.xml"
            path.write_text(self.closed_log(), encoding="utf-8")
            handler = FolditLogHandler(self.logger_settings())

            exported = handler.export_log_file(str(path), open_file=False, puzzle_id="2802")

            self.assertTrue(exported and os.path.isfile(exported))
            self.assertIn("[drw]", os.path.basename(exported))

    def test_recover_interrupted_log_file_uses_named_track_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "scriptlog.drw.xml"
            open_log = self.closed_log().replace(
                "Canceled\n</Foldit:ScriptOutput>\n</Foldit:Script>",
                "",
            )
            path.write_text(open_log, encoding="utf-8")
            old_time = time.time() - 60
            os.utime(path, (old_time, old_time))
            handler = FolditLogHandler(self.logger_settings())

            recovered = handler.recover_interrupted_log_file(
                str(path),
                process_create_time=time.time(),
                puzzle_id="2802",
            )

            self.assertTrue(recovered and os.path.isfile(recovered))
            self.assertIn("[drw]", os.path.basename(recovered))


if __name__ == "__main__":
    unittest.main()
