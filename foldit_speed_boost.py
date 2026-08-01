import importlib
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

INSTALL_COMMAND = "python -m pip install frida==17.15.4"
frida = None
FRIDA_IMPORT_ERROR = None
_FRIDA_IMPORT_ATTEMPTED = False
_FRIDA_IMPORT_LOCK = threading.Lock()

# Concrete game_library.dll return-address offsets are runtime configuration.
# Keep this engine identical for public and private installations.
TARGET_SLEEP_MS = 100


@dataclass(frozen=True)
class SpeedBoostTiming:
    replacement_sleep_ms: int
    timer_resolution_ms: int

    def __post_init__(self):
        for field_name, value in (
            ("replacement_sleep_ms", self.replacement_sleep_ms),
            ("timer_resolution_ms", self.timer_resolution_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


SPEED_BOOST_JS = r"""
const targetMs = TARGET_MS_PLACEHOLDER;
let replaceMs = REPLACEMENT_MS_PLACEHOLDER;
let timerResolutionMs = TIMER_RESOLUTION_MS_PLACEHOLDER;
const gameOffsets = [GAME_OFFSETS_PLACEHOLDER];
const stats = {
    patched: 0,
    passed: 0,
    skippedByCaller: 0,
    matchedByOffset: {},
    patchedByOffset: {},
    enabled: false,
    timerPeriodActive: false,
    timerPeriodBeginResult: null,
    timerPeriodEndResult: null,
    replacementSleepMs: replaceMs,
    timerResolutionMs: timerResolutionMs,
    activeTimerResolutionMs: null,
};
const hookedAddresses = {};

function findExport(moduleName, exportName) {
    let module = Process.findModuleByName(moduleName);
    if (module === null) {
        module = Module.load(moduleName);
    }
    return module.findExportByName(exportName);
}

function beginTimerPeriod() {
    if (stats.timerPeriodActive || timerResolutionMs <= 0) {
        return stats.timerPeriodActive;
    }
    const address = findExport("winmm.dll", "timeBeginPeriod");
    if (address === null) {
        return false;
    }
    const timeBeginPeriod = new NativeFunction(address, "uint", ["uint"]);
    stats.timerPeriodBeginResult = timeBeginPeriod(timerResolutionMs);
    stats.timerPeriodActive = stats.timerPeriodBeginResult === 0;
    stats.activeTimerResolutionMs = stats.timerPeriodActive ? timerResolutionMs : null;
    return stats.timerPeriodActive;
}

function endTimerPeriod() {
    const activeResolutionMs = stats.activeTimerResolutionMs;
    if (!stats.timerPeriodActive || activeResolutionMs === null) {
        return true;
    }
    const address = findExport("winmm.dll", "timeEndPeriod");
    if (address === null) {
        return false;
    }
    const timeEndPeriod = new NativeFunction(address, "uint", ["uint"]);
    stats.timerPeriodEndResult = timeEndPeriod(activeResolutionMs);
    if (stats.timerPeriodEndResult === 0) {
        stats.timerPeriodActive = false;
        stats.activeTimerResolutionMs = null;
    }
    return !stats.timerPeriodActive;
}

function setTiming(nextReplaceMs, nextTimerResolutionMs) {
    if (!Number.isInteger(nextReplaceMs) || nextReplaceMs <= 0) {
        return { ok: false, error: "replacement sleep must be a positive integer" };
    }
    if (!Number.isInteger(nextTimerResolutionMs) || nextTimerResolutionMs <= 0) {
        return { ok: false, error: "timer resolution must be a positive integer" };
    }

    const previousReplaceMs = replaceMs;
    const previousTimerResolutionMs = timerResolutionMs;
    const wasTimerActive = stats.timerPeriodActive;
    const shouldTimerBeActive = stats.enabled;
    if (wasTimerActive && !endTimerPeriod()) {
        return { ok: false, error: "timeEndPeriod failed for the previous resolution" };
    }

    replaceMs = nextReplaceMs;
    timerResolutionMs = nextTimerResolutionMs;
    stats.replacementSleepMs = replaceMs;
    stats.timerResolutionMs = timerResolutionMs;

    if (shouldTimerBeActive && !beginTimerPeriod()) {
        replaceMs = previousReplaceMs;
        timerResolutionMs = previousTimerResolutionMs;
        stats.replacementSleepMs = replaceMs;
        stats.timerResolutionMs = timerResolutionMs;
        const rollbackOk = shouldTimerBeActive ? beginTimerPeriod() : true;
        return {
            ok: false,
            error: "timeBeginPeriod failed for the new resolution",
            rollbackOk: rollbackOk,
        };
    }

    return {
        ok: true,
        replacementSleepMs: replaceMs,
        timerResolutionMs: timerResolutionMs,
        timerPeriodActive: stats.timerPeriodActive,
    };
}

function matchingCallerOffset(returnAddress) {
    const module = Process.findModuleByAddress(returnAddress);
    if (module === null || module.name.toLowerCase() !== "game_library.dll") {
        return null;
    }
    const offset = returnAddress.sub(module.base).toUInt32();
    return gameOffsets.indexOf(offset) !== -1 ? offset : null;
}

function incrementByOffset(target, offset) {
    const key = "0x" + offset.toString(16);
    target[key] = (target[key] || 0) + 1;
}

function attachSleep(moduleName) {
    const address = findExport(moduleName, "Sleep");
    if (address === null) {
        return false;
    }
    const addressKey = address.toString();
    if (hookedAddresses[addressKey] !== undefined) {
        return false;
    }
    hookedAddresses[addressKey] = moduleName + "!Sleep";
    Interceptor.attach(address, {
        onEnter(args) {
            const ms = args[0].toUInt32();
            const offset = ms === targetMs ? matchingCallerOffset(this.returnAddress) : null;
            if (offset !== null) {
                incrementByOffset(stats.matchedByOffset, offset);
                if (stats.enabled) {
                    args[0] = ptr(replaceMs);
                    stats.patched += 1;
                    incrementByOffset(stats.patchedByOffset, offset);
                } else {
                    stats.passed += 1;
                }
            } else if (ms === targetMs) {
                stats.skippedByCaller += 1;
            } else {
                stats.passed += 1;
            }
        }
    });
    send({ type: "hook", api: moduleName + "!Sleep", address: addressKey });
    return true;
}

attachSleep("KERNELBASE.dll");
attachSleep("KERNEL32.dll");

rpc.exports = {
    setenabled(value) {
        const nextEnabled = !!value;
        if (nextEnabled) {
            beginTimerPeriod();
        } else {
            endTimerPeriod();
        }
        stats.enabled = nextEnabled;
        return stats.enabled;
    },
    getstats() {
        return stats;
    },
    settiming(replacementSleepMs, requestedTimerResolutionMs) {
        return setTiming(replacementSleepMs, requestedTimerResolutionMs);
    },
    cleanup() {
        stats.enabled = false;
        endTimerPeriod();
        return stats;
    }
};
"""


class SpeedBoostUnavailable(RuntimeError):
    pass


@dataclass
class SpeedBoostSession:
    pid: int
    client_name: str
    session: object
    script: object
    enabled: bool
    started_at: float
    timing: Optional[SpeedBoostTiming] = None
    last_error: str = ""


def _load_frida():
    """Import Frida on the first explicit Speed Boost action."""
    global frida, FRIDA_IMPORT_ERROR, _FRIDA_IMPORT_ATTEMPTED

    if frida is not None:
        return frida

    with _FRIDA_IMPORT_LOCK:
        if frida is not None:
            return frida
        if _FRIDA_IMPORT_ATTEMPTED:
            return None

        _FRIDA_IMPORT_ATTEMPTED = True
        try:
            frida = importlib.import_module("frida")
        except Exception as exc:  # pragma: no cover - depends on local install
            FRIDA_IMPORT_ERROR = exc
            return None

        FRIDA_IMPORT_ERROR = None
        return frida


def is_available() -> bool:
    return _load_frida() is not None


def unavailable_message() -> str:
    detail = f"\n\nImport error: {FRIDA_IMPORT_ERROR}" if FRIDA_IMPORT_ERROR else ""
    return (
        "Speed boost requires the Python package 'frida'.\n\n"
        f"Install it with:\n{INSTALL_COMMAND}"
        f"{detail}"
    )


def _normalize_offsets(offsets) -> tuple[int, ...]:
    if isinstance(offsets, (str, bytes)):
        raise ValueError("offsets must be a non-empty sequence of integers")
    try:
        normalized = tuple(offsets)
    except TypeError as error:
        raise ValueError("offsets must be a non-empty sequence of integers") from error
    if not normalized:
        raise ValueError("offsets must not be empty")
    for offset in normalized:
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError("offsets must contain only integers")
        if offset < 0 or offset > 0xFFFFFFFF:
            raise ValueError("offsets must fit in the 32-bit module range")
    return normalized


def _script_source(timing: SpeedBoostTiming, offsets) -> str:
    if not isinstance(timing, SpeedBoostTiming):
        raise TypeError("timing must be a SpeedBoostTiming instance")
    normalized_offsets = _normalize_offsets(offsets)
    return (
        SPEED_BOOST_JS.replace("TARGET_MS_PLACEHOLDER", str(TARGET_SLEEP_MS))
        .replace("REPLACEMENT_MS_PLACEHOLDER", str(timing.replacement_sleep_ms))
        .replace("TIMER_RESOLUTION_MS_PLACEHOLDER", str(timing.timer_resolution_ms))
        .replace("GAME_OFFSETS_PLACEHOLDER", ", ".join(str(offset) for offset in normalized_offsets))
    )


class FolditSpeedBoostManager:
    def __init__(self, timing: SpeedBoostTiming, offsets, log_callback=None):
        if not isinstance(timing, SpeedBoostTiming):
            raise TypeError("timing must be a SpeedBoostTiming instance")
        self.sessions: Dict[int, SpeedBoostSession] = {}
        self.log_callback = log_callback
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._timing = timing
        self._offsets = _normalize_offsets(offsets)

    def log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)
        else:
            print(message)

    def is_supported(self) -> bool:
        return is_available()

    def is_managed(self, pid: int) -> bool:
        with self._lock:
            return int(pid) in self.sessions

    def is_enabled(self, pid: int) -> bool:
        with self._lock:
            session = self.sessions.get(int(pid))
        return bool(session and session.enabled)

    def snapshot(self) -> Dict[int, bool]:
        with self._lock:
            return {pid: session.enabled for pid, session in self.sessions.items()}

    def get_timing(self) -> SpeedBoostTiming:
        with self._lock:
            return self._timing

    def _apply_timing_to_session(
        self,
        managed: SpeedBoostSession,
        timing: SpeedBoostTiming,
    ) -> bool:
        try:
            result = managed.script.exports_sync.settiming(
                timing.replacement_sleep_ms,
                timing.timer_resolution_ms,
            )
            applied = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
            if not applied:
                detail = result.get("error", "unknown error") if isinstance(result, dict) else str(result)
                raise RuntimeError(detail)
            managed.timing = timing
            managed.last_error = ""
            return True
        except Exception as exc:
            managed.last_error = str(exc)
            self.log(f"Speed boost pid={managed.pid}: set_timing failed: {exc}")
            return False

    def set_timing(self, timing: SpeedBoostTiming) -> Dict[int, bool]:
        """Apply a global timing profile to current and future sessions."""
        if not isinstance(timing, SpeedBoostTiming):
            raise TypeError("timing must be a SpeedBoostTiming instance")
        with self._operation_lock:
            with self._lock:
                self._timing = timing
                managed_sessions = list(self.sessions.values())
            return {
                managed.pid: self._apply_timing_to_session(managed, timing)
                for managed in managed_sessions
            }

    def get_stats(self, pid: int) -> Optional[dict]:
        """Return live hook counters for diagnostics and UI status checks."""
        pid = int(pid)
        with self._lock:
            managed = self.sessions.get(pid)
        if managed is None:
            return None
        try:
            return dict(managed.script.exports_sync.getstats())
        except Exception as exc:
            managed.last_error = str(exc)
            self.log(f"Speed boost pid={pid}: get_stats failed: {exc}")
            return None

    def start(self, pid: int, client_name: str = "", enabled: bool = False) -> bool:
        pid = int(pid)
        frida_module = _load_frida()
        if frida_module is None:
            raise SpeedBoostUnavailable(unavailable_message())
        with self._operation_lock:
            with self._lock:
                already_managed = pid in self.sessions
                timing = self._timing
            if already_managed:
                self.set_enabled(pid, enabled)
                return True

            session = frida_module.attach(pid)
            script = session.create_script(_script_source(timing, self._offsets))

            def on_message(message, data):
                payload = message.get("payload") if isinstance(message, dict) else None
                if payload:
                    self.log(f"Speed boost pid={pid}: {payload}")
                else:
                    self.log(f"Speed boost pid={pid}: {message}")

            script.on("message", on_message)
            script.load()
            managed = SpeedBoostSession(
                pid=pid,
                client_name=client_name or str(pid),
                session=session,
                script=script,
                enabled=False,
                started_at=time.time(),
                timing=timing,
            )
            with self._lock:
                self.sessions[pid] = managed
            self.set_enabled(pid, enabled)
            return True

    def set_enabled(self, pid: int, enabled: bool) -> bool:
        pid = int(pid)
        with self._lock:
            managed = self.sessions.get(pid)
        if managed is None:
            return False
        if managed.enabled == bool(enabled):
            return managed.enabled
        try:
            result = bool(managed.script.exports_sync.setenabled(bool(enabled)))
            with self._lock:
                if self.sessions.get(pid) is managed:
                    managed.enabled = result
            return result
        except Exception as exc:
            managed.last_error = str(exc)
            self.log(f"Speed boost pid={pid}: set_enabled failed: {exc}")
            return False

    def disable(self, pid: int) -> bool:
        return self.set_enabled(pid, False)

    def forget(self, pid: int) -> None:
        with self._lock:
            self.sessions.pop(int(pid), None)

    def detach(self, pid: int, fast: bool = True) -> None:
        pid = int(pid)
        with self._lock:
            managed = self.sessions.pop(pid, None)
        if managed is None:
            return
        try:
            stats = managed.script.exports_sync.cleanup()
            if not fast:
                self.log(f"Speed boost pid={pid}: cleanup={stats}")
        except Exception as exc:
            if not fast:
                self.log(f"Speed boost pid={pid}: cleanup failed: {exc}")
        try:
            managed.session.detach()
        except Exception:
            pass

    def stop(self, pid: int, fast: bool = True) -> None:
        self.detach(pid, fast=fast)

    def prune(self, live_pids) -> None:
        live = {int(pid) for pid in live_pids}
        for pid in list(self.sessions):
            if pid not in live:
                self.forget(pid)

    def stop_all(self) -> None:
        for pid in list(self.sessions):
            self.detach(pid, fast=True)

    def abandon_all(self) -> None:
        """Release timer-period requests and forget sessions during shutdown.

        Explicit Frida detach can take seconds per busy Foldit process. When the
        monitor process exits, Frida sessions are torn down by process shutdown.
        Use this only when the Python app is closing immediately.
        """
        with self._lock:
            managed_sessions = list(self.sessions.values())
            self.sessions.clear()
        for managed in managed_sessions:
            try:
                managed.script.exports_sync.cleanup()
            except Exception:
                pass
