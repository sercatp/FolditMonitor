import importlib
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

INSTALL_COMMAND = "python -m pip install frida==17.15.4"
frida = None
FRIDA_IMPORT_ERROR = None
_FRIDA_IMPORT_ATTEMPTED = False
_FRIDA_IMPORT_LOCK = threading.Lock()

# Concrete game_library.dll return-address offsets are runtime configuration.
# Keep this engine identical for public and private installations.
TARGET_SLEEP_MS = 100
RPC_TIMEOUT_SECONDS = 5.0
SHUTDOWN_RPC_TIMEOUT_SECONDS = 0.5
SHUTDOWN_TOTAL_TIMEOUT_SECONDS = 1.0


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
    enabled: false,
    timerPeriodActive: false,
    timerPeriodBeginResult: null,
    timerPeriodEndResult: null,
    replacementSleepMs: replaceMs,
    timerResolutionMs: timerResolutionMs,
    activeTimerResolutionMs: null,
};
const gameModule = Process.getModuleByName("game_library.dll");
const targetReturnAddresses = gameOffsets.map(offset => gameModule.base.add(offset));
let hookListeners = [];
let hookedApis = [];

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

function isTargetCaller(returnAddress) {
    for (let index = 0; index !== targetReturnAddresses.length; index++) {
        if (returnAddress.equals(targetReturnAddresses[index])) {
            return true;
        }
    }
    return false;
}

function attachSleep(moduleName, seenAddresses) {
    const address = findExport(moduleName, "Sleep");
    if (address === null) {
        return false;
    }
    const addressKey = address.toString();
    if (seenAddresses[addressKey] !== undefined) {
        return false;
    }
    seenAddresses[addressKey] = true;
    const listener = Interceptor.attach(address, {
        onEnter(args) {
            if (!stats.enabled) {
                return;
            }
            const ms = args[0].toUInt32();
            if (ms !== targetMs || !isTargetCaller(this.returnAddress)) {
                return;
            }
            args[0] = ptr(replaceMs);
        }
    });
    hookListeners.push(listener);
    hookedApis.push({ api: moduleName + "!Sleep", address: addressKey });
    return true;
}

function installHooks() {
    if (hookListeners.length !== 0) {
        return true;
    }

    const seenAddresses = {};
    try {
        attachSleep("KERNELBASE.dll", seenAddresses);
        attachSleep("KERNEL32.dll", seenAddresses);
        Interceptor.flush();
        return hookListeners.length !== 0;
    } catch (error) {
        removeHooks();
        throw error;
    }
}

function removeHooks() {
    const listeners = hookListeners;
    hookListeners = [];
    hookedApis = [];
    listeners.forEach(listener => {
        try {
            listener.detach();
        } catch (_) {
        }
    });
    Interceptor.flush();
}

function currentStats() {
    return {
        enabled: stats.enabled,
        hooksActive: hookListeners.length !== 0,
        hookedApis: hookedApis.slice(),
        timerPeriodActive: stats.timerPeriodActive,
        timerPeriodBeginResult: stats.timerPeriodBeginResult,
        timerPeriodEndResult: stats.timerPeriodEndResult,
        replacementSleepMs: replaceMs,
        timerResolutionMs: timerResolutionMs,
        activeTimerResolutionMs: stats.activeTimerResolutionMs,
    };
}

rpc.exports = {
    setenabled(value) {
        const nextEnabled = !!value;
        if (nextEnabled) {
            if (stats.enabled && hookListeners.length !== 0) {
                return true;
            }
            stats.enabled = true;
            try {
                if (!installHooks()) {
                    stats.enabled = false;
                    endTimerPeriod();
                    return false;
                }
                beginTimerPeriod();
            } catch (error) {
                stats.enabled = false;
                removeHooks();
                endTimerPeriod();
                throw error;
            }
        } else {
            // Stop patching before detach/cleanup work begins.
            stats.enabled = false;
            removeHooks();
            endTimerPeriod();
        }
        return stats.enabled;
    },
    getstats() {
        return currentStats();
    },
    settiming(replacementSleepMs, requestedTimerResolutionMs) {
        return setTiming(replacementSleepMs, requestedTimerResolutionMs);
    },
    cleanup() {
        stats.enabled = false;
        removeHooks();
        endTimerPeriod();
        return currentStats();
    }
};
"""


class SpeedBoostUnavailable(RuntimeError):
    pass


class SpeedBoostTimeout(TimeoutError):
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
    rpc_lock: object = field(default_factory=threading.RLock, repr=False)


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
    def __init__(
        self,
        timing: SpeedBoostTiming,
        offsets,
        log_callback=None,
        rpc_timeout_seconds: float = RPC_TIMEOUT_SECONDS,
    ):
        if not isinstance(timing, SpeedBoostTiming):
            raise TypeError("timing must be a SpeedBoostTiming instance")
        if isinstance(rpc_timeout_seconds, bool) or rpc_timeout_seconds <= 0:
            raise ValueError("rpc_timeout_seconds must be positive")
        self.sessions: Dict[int, SpeedBoostSession] = {}
        self.log_callback = log_callback
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._timing = timing
        self._offsets = _normalize_offsets(offsets)
        self._rpc_timeout_seconds = float(rpc_timeout_seconds)

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

    def _call_with_timeout(self, operation_name: str, callback, timeout=None):
        """Run one Frida call with a bounded wait on the calling thread."""
        cancellable_class = getattr(frida, "Cancellable", None)
        if cancellable_class is None:
            # Unit-test fakes do not need to implement Frida's cancellation API.
            return callback()

        timeout_seconds = (
            self._rpc_timeout_seconds if timeout is None else float(timeout)
        )
        cancellable = cancellable_class()
        timed_out = threading.Event()

        def cancel_operation():
            timed_out.set()
            cancellable.cancel()

        timer = threading.Timer(timeout_seconds, cancel_operation)
        timer.daemon = True
        timer.start()
        try:
            with cancellable:
                return callback()
        except Exception as exc:
            if timed_out.is_set():
                raise SpeedBoostTimeout(
                    f"{operation_name} timed out after {timeout_seconds:g}s"
                ) from exc
            raise
        finally:
            timer.cancel()

    def _apply_timing_to_session(
        self,
        managed: SpeedBoostSession,
        timing: SpeedBoostTiming,
    ) -> bool:
        try:
            with managed.rpc_lock:
                result = self._call_with_timeout(
                    f"set_timing pid={managed.pid}",
                    lambda: managed.script.exports_sync.settiming(
                        timing.replacement_sleep_ms,
                        timing.timer_resolution_ms,
                    ),
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
        """Return lightweight hook state for diagnostics and UI checks."""
        pid = int(pid)
        with self._lock:
            managed = self.sessions.get(pid)
        if managed is None:
            return None
        try:
            with managed.rpc_lock:
                result = self._call_with_timeout(
                    f"get_stats pid={pid}",
                    lambda: managed.script.exports_sync.getstats(),
                )
            managed.last_error = ""
            return dict(result)
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

            session = None
            script = None
            try:
                session = self._call_with_timeout(
                    f"attach pid={pid}",
                    lambda: frida_module.attach(pid),
                )
                script = self._call_with_timeout(
                    f"create_script pid={pid}",
                    lambda: session.create_script(_script_source(timing, self._offsets)),
                )

                def on_message(message, data):
                    payload = message.get("payload") if isinstance(message, dict) else None
                    if payload:
                        self.log(f"Speed boost pid={pid}: {payload}")
                    else:
                        self.log(f"Speed boost pid={pid}: {message}")

                script.on("message", on_message)
                self._call_with_timeout(f"load_script pid={pid}", script.load)
                self._call_with_timeout(
                    f"verify_script pid={pid}",
                    lambda: script.exports_sync.getstats(),
                )
            except Exception:
                if script is not None:
                    try:
                        self._call_with_timeout(
                            f"unload_failed_script pid={pid}",
                            script.unload,
                            timeout=SHUTDOWN_RPC_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        pass
                if session is not None:
                    try:
                        self._call_with_timeout(
                            f"detach_failed_session pid={pid}",
                            session.detach,
                            timeout=SHUTDOWN_RPC_TIMEOUT_SECONDS,
                        )
                    except Exception:
                        pass
                raise

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
        if managed.enabled == bool(enabled) and not managed.last_error:
            return managed.enabled
        try:
            with managed.rpc_lock:
                result = bool(
                    self._call_with_timeout(
                        f"set_enabled pid={pid}",
                        lambda: managed.script.exports_sync.setenabled(bool(enabled)),
                    )
                )
            with self._lock:
                if self.sessions.get(pid) is managed:
                    managed.enabled = result
                    managed.last_error = ""
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
        with managed.rpc_lock:
            try:
                stats = self._call_with_timeout(
                    f"cleanup pid={pid}",
                    lambda: managed.script.exports_sync.cleanup(),
                )
                if not fast:
                    self.log(f"Speed boost pid={pid}: cleanup={stats}")
            except Exception as exc:
                if not fast:
                    self.log(f"Speed boost pid={pid}: cleanup failed: {exc}")
            try:
                self._call_with_timeout(
                    f"detach pid={pid}",
                    managed.session.detach,
                )
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
        """Best-effort parallel cleanup with one bounded shutdown deadline."""
        with self._lock:
            managed_sessions = list(self.sessions.values())
            self.sessions.clear()

        def cleanup_session(managed):
            acquired = managed.rpc_lock.acquire(
                timeout=SHUTDOWN_RPC_TIMEOUT_SECONDS
            )
            if not acquired:
                return
            try:
                try:
                    self._call_with_timeout(
                        f"shutdown_cleanup pid={managed.pid}",
                        lambda: managed.script.exports_sync.cleanup(),
                        timeout=SHUTDOWN_RPC_TIMEOUT_SECONDS,
                    )
                except Exception:
                    pass
                try:
                    self._call_with_timeout(
                        f"shutdown_detach pid={managed.pid}",
                        managed.session.detach,
                        timeout=SHUTDOWN_RPC_TIMEOUT_SECONDS,
                    )
                except Exception:
                    pass
            finally:
                managed.rpc_lock.release()

        workers = []
        for managed in managed_sessions:
            worker = threading.Thread(
                target=cleanup_session,
                args=(managed,),
                daemon=True,
            )
            worker.start()
            workers.append(worker)

        deadline = time.monotonic() + SHUTDOWN_TOTAL_TIMEOUT_SECONDS
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
