import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import foldit_speed_boost as speed_boost_module
from foldit_speed_boost_integration import FolditSpeedBoostIntegration
from settings import (
    DEFAULT_SPEED_BOOST_PROFILE,
    SPEED_BOOST_PROFILES,
    Settings,
)
from foldit_speed_boost import (
    GAME_LIBRARY_SLEEP_OFFSETS,
    TARGET_SLEEP_MS,
    FolditSpeedBoostManager,
    SpeedBoostSession,
    SpeedBoostTiming,
    _script_source,
)


FAST_PROFILE = SPEED_BOOST_PROFILES[DEFAULT_SPEED_BOOST_PROFILE]
FAST_TIMING = SpeedBoostTiming(
    replacement_sleep_ms=FAST_PROFILE["replacement_sleep_ms"],
    timer_resolution_ms=FAST_PROFILE["timer_resolution_ms"],
)
MONITOR_SOURCE_PATH = Path(__file__).resolve().parents[1] / "Foldit Monitor.pyw"


class _Exports:
    def getstats(self):
        return {
            "enabled": True,
            "patched": 7,
            "matchedByOffset": {"0xdb729b": 7},
        }


class _Script:
    exports_sync = _Exports()


class _TimingExports:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True}

    def settiming(self, replacement_sleep_ms, timer_resolution_ms):
        self.calls.append((replacement_sleep_ms, timer_resolution_ms))
        return self.result


class _TimingScript:
    def __init__(self, result=None):
        self.exports_sync = _TimingExports(result=result)


class _CleanupExports:
    def __init__(self):
        self.cleanup_calls = 0

    def cleanup(self):
        self.cleanup_calls += 1
        return {"enabled": False, "timerPeriodActive": False}


class _CleanupScript:
    def __init__(self):
        self.exports_sync = _CleanupExports()


class _Session:
    def __init__(self):
        self.detach_calls = 0

    def detach(self):
        self.detach_calls += 1


class SpeedBoostCases(unittest.TestCase):
    def test_frida_is_imported_only_when_support_is_first_checked(self):
        original_state = (
            speed_boost_module.frida,
            speed_boost_module.FRIDA_IMPORT_ERROR,
            speed_boost_module._FRIDA_IMPORT_ATTEMPTED,
        )
        fake_frida = object()
        try:
            speed_boost_module.frida = None
            speed_boost_module.FRIDA_IMPORT_ERROR = None
            speed_boost_module._FRIDA_IMPORT_ATTEMPTED = False
            with patch.object(
                speed_boost_module.importlib,
                "import_module",
                return_value=fake_frida,
            ) as import_module:
                self.assertTrue(speed_boost_module.is_available())
                self.assertTrue(speed_boost_module.is_available())

            import_module.assert_called_once_with("frida")
            self.assertIs(speed_boost_module.frida, fake_frida)
        finally:
            (
                speed_boost_module.frida,
                speed_boost_module.FRIDA_IMPORT_ERROR,
                speed_boost_module._FRIDA_IMPORT_ATTEMPTED,
            ) = original_state

    def test_unchanged_client_snapshot_does_not_start_another_sync_thread(self):
        integration = object.__new__(FolditSpeedBoostIntegration)
        integration.armed_pids = set()
        integration.script_running_pids = set()
        integration.busy_pids = set()
        integration.managed_pids = set()
        integration.enabled_pids = set()
        integration.client_names = {}
        integration._lock = threading.RLock()
        integration._closed = False
        integration._pending_sync = None
        integration._active_sync = None
        integration._last_completed_sync = None
        integration._sync_running = False

        class SyncManager:
            def __init__(self):
                self.snapshot_calls = 0

            def snapshot(self):
                self.snapshot_calls += 1
                return {}

            def log(self, _message):
                pass

        integration.manager = SyncManager()
        launched = []

        def run_immediately(target, *args):
            launched.append(target.__name__)
            target(*args)

        integration._run_thread = run_immediately
        client = {
            "pid": 123,
            "is_window_visible": True,
            "client_name": "Foldit1",
            "script_running": False,
        }

        integration.on_clients_refreshed([client])
        integration.on_clients_refreshed([client])

        self.assertEqual(launched, ["_worker_sync_clients"])
        self.assertEqual(integration.manager.snapshot_calls, 1)

        changed_client = dict(client, script_running=True)
        integration.on_clients_refreshed([changed_client])

        self.assertEqual(
            launched,
            ["_worker_sync_clients", "_worker_sync_clients"],
        )
        self.assertEqual(integration.manager.snapshot_calls, 2)

    def test_user_selectable_profiles_have_expected_timings(self):
        self.assertEqual(DEFAULT_SPEED_BOOST_PROFILE, "fast")
        self.assertEqual(
            {
                name: (
                    profile["replacement_sleep_ms"],
                    profile["timer_resolution_ms"],
                )
                for name, profile in SPEED_BOOST_PROFILES.items()
            },
            {
                "fastest": (1, 1),
                "fast": (2, 2),
                "medium": (5, 3),
                "slower": (15, 5),
            },
        )

    def test_selected_profile_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(temp_dir)
            self.assertTrue(settings.SPEED_BOOST_ENABLED)
            self.assertEqual(settings.SPEED_BOOST_PROFILE, "fast")

            settings.save_speed_boost_profile("medium")
            reloaded = Settings(temp_dir)

            self.assertEqual(reloaded.SPEED_BOOST_PROFILE, "medium")
            self.assertEqual(reloaded.settings["speed_boost"]["profile"], "medium")

    def test_invalid_selected_profile_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(temp_dir)
            settings.save_speed_boost_profile("not-a-profile")

            self.assertEqual(settings.SPEED_BOOST_PROFILE, "fast")

    def test_feature_flag_is_opt_in_and_malformed_section_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "Foldit Monitor.json"
            settings_path.write_text(
                '{"speed_boost": {"enabled": true}}',
                encoding="utf-8",
            )
            self.assertTrue(Settings(temp_dir).SPEED_BOOST_ENABLED)

            settings_path.write_text('{"speed_boost": []}', encoding="utf-8")
            self.assertFalse(Settings(temp_dir).SPEED_BOOST_ENABLED)

    def test_verified_wiggle_offsets_are_internal_to_speed_boost(self):
        self.assertEqual(
            GAME_LIBRARY_SLEEP_OFFSETS,
            (0xDB729B, 0xE729B9),
        )
        self.assertEqual(TARGET_SLEEP_MS, 100)

    def test_selected_timing_is_embedded_in_new_script(self):
        timing = SpeedBoostTiming(replacement_sleep_ms=1, timer_resolution_ms=1)
        source = _script_source(timing)
        for offset in GAME_LIBRARY_SLEEP_OFFSETS:
            self.assertIn(str(offset), source)
        self.assertIn("let replaceMs = 1;", source)
        self.assertIn("let timerResolutionMs = 1;", source)
        self.assertIn('findExport("winmm.dll", "timeBeginPeriod")', source)
        self.assertIn('findExport("winmm.dll", "timeEndPeriod")', source)
        self.assertIn("timeEndPeriod(activeResolutionMs)", source)
        self.assertIn("settiming(replacementSleepMs, requestedTimerResolutionMs)", source)
        self.assertNotIn("TIMER_RESOLUTION_MS_PLACEHOLDER", source)

    def test_monitor_exposes_one_speed_boost_cascade(self):
        source = MONITOR_SOURCE_PATH.read_text(encoding="utf-8-sig")

        self.assertEqual(source.count('add_cascade(label="Speed boost"'), 1)
        self.assertNotIn('add_cascade(label="All clients"', source)
        self.assertNotIn(
            "from foldit_speed_boost_integration import FolditSpeedBoostIntegration",
            source,
        )
        self.assertIn('import_module("foldit_speed_boost_integration")', source)

    def test_timing_values_must_be_positive_integers(self):
        with self.assertRaises(ValueError):
            SpeedBoostTiming(replacement_sleep_ms=0, timer_resolution_ms=1)
        with self.assertRaises(ValueError):
            SpeedBoostTiming(replacement_sleep_ms=1, timer_resolution_ms=True)

    def test_manager_applies_timing_to_current_and_future_sessions(self):
        manager = FolditSpeedBoostManager(timing=FAST_TIMING)
        script = _TimingScript()
        managed = SpeedBoostSession(
            pid=123,
            client_name="Foldit9",
            session=object(),
            script=script,
            enabled=True,
            started_at=0.0,
        )
        manager.sessions[123] = managed
        timing = SpeedBoostTiming(replacement_sleep_ms=5, timer_resolution_ms=3)

        result = manager.set_timing(timing)

        self.assertEqual(result, {123: True})
        self.assertEqual(script.exports_sync.calls, [(5, 3)])
        self.assertEqual(managed.timing, timing)
        self.assertEqual(manager.get_timing(), timing)

    def test_manager_reports_session_timing_failure_but_keeps_global_choice(self):
        manager = FolditSpeedBoostManager(
            timing=FAST_TIMING,
            log_callback=lambda _message: None,
        )
        script = _TimingScript(result={"ok": False, "error": "unsupported resolution"})
        managed = SpeedBoostSession(
            pid=123,
            client_name="Foldit9",
            session=object(),
            script=script,
            enabled=True,
            started_at=0.0,
        )
        manager.sessions[123] = managed
        timing = SpeedBoostTiming(replacement_sleep_ms=15, timer_resolution_ms=5)

        result = manager.set_timing(timing)

        self.assertEqual(result, {123: False})
        self.assertIsNone(managed.timing)
        self.assertEqual(manager.get_timing(), timing)
        self.assertIn("unsupported resolution", managed.last_error)

    def test_get_stats_returns_live_hook_counters(self):
        manager = FolditSpeedBoostManager(timing=FAST_TIMING)
        manager.sessions[123] = SpeedBoostSession(
            pid=123,
            client_name="Foldit9",
            session=object(),
            script=_Script(),
            enabled=True,
            started_at=0.0,
        )

        stats = manager.get_stats(123)

        self.assertEqual(stats["patched"], 7)
        self.assertEqual(stats["matchedByOffset"], {"0xdb729b": 7})
        self.assertIsNone(manager.get_stats(999))

    def test_fast_detach_releases_timer_period_before_detaching(self):
        manager = FolditSpeedBoostManager(timing=FAST_TIMING)
        script = _CleanupScript()
        session = _Session()
        manager.sessions[123] = SpeedBoostSession(
            pid=123,
            client_name="Foldit9",
            session=session,
            script=script,
            enabled=True,
            started_at=0.0,
        )

        manager.detach(123, fast=True)

        self.assertEqual(script.exports_sync.cleanup_calls, 1)
        self.assertEqual(session.detach_calls, 1)
        self.assertFalse(manager.is_managed(123))

    def test_abandon_all_releases_timer_period_without_slow_detach(self):
        manager = FolditSpeedBoostManager(timing=FAST_TIMING)
        script = _CleanupScript()
        session = _Session()
        manager.sessions[123] = SpeedBoostSession(
            pid=123,
            client_name="Foldit9",
            session=session,
            script=script,
            enabled=True,
            started_at=0.0,
        )

        manager.abandon_all()

        self.assertEqual(script.exports_sync.cleanup_calls, 1)
        self.assertEqual(session.detach_calls, 0)
        self.assertEqual(manager.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
