import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

try:
    import frida
except Exception as exc:  # pragma: no cover - depends on local install
    frida = None
    FRIDA_IMPORT_ERROR = exc
else:
    FRIDA_IMPORT_ERROR = None


INSTALL_COMMAND = "python -m pip install frida==17.15.4"
GAME_LIBRARY_SLEEP_OFFSET = 0xAECD90
GAME_LIBRARY_OFFSET_TOLERANCE = 32


SPEED_BOOST_JS = r"""
const targetMs = 2;
const replaceMs = 0;
const gameOffset = GAME_OFFSET_PLACEHOLDER;
const offsetTolerance = OFFSET_TOLERANCE_PLACEHOLDER;
const stats = {
    patched: 0,
    passed: 0,
    skippedByCaller: 0,
    enabled: false,
};
const hookedAddresses = {};

function findExport(moduleName, exportName) {
    let module = Process.findModuleByName(moduleName);
    if (module === null) {
        module = Module.load(moduleName);
    }
    return module.findExportByName(exportName);
}

function callerMatches(returnAddress) {
    const module = Process.findModuleByAddress(returnAddress);
    if (module === null || module.name.toLowerCase() !== "game_library.dll") {
        return false;
    }
    const offset = returnAddress.sub(module.base).toUInt32();
    return Math.abs(offset - gameOffset) <= offsetTolerance;
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
            if (ms === targetMs && callerMatches(this.returnAddress)) {
                if (stats.enabled) {
                    args[0] = ptr(replaceMs);
                    stats.patched += 1;
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
        stats.enabled = !!value;
        return stats.enabled;
    },
    getstats() {
        return stats;
    },
    cleanup() {
        stats.enabled = false;
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
    last_error: str = ""


def is_available() -> bool:
    return frida is not None


def unavailable_message() -> str:
    detail = f"\n\nImport error: {FRIDA_IMPORT_ERROR}" if FRIDA_IMPORT_ERROR else ""
    return (
        "Speed boost requires the Python package 'frida'.\n\n"
        f"Install it with:\n{INSTALL_COMMAND}"
        f"{detail}"
    )


def _script_source() -> str:
    return (
        SPEED_BOOST_JS.replace("GAME_OFFSET_PLACEHOLDER", str(GAME_LIBRARY_SLEEP_OFFSET))
        .replace("OFFSET_TOLERANCE_PLACEHOLDER", str(GAME_LIBRARY_OFFSET_TOLERANCE))
    )


class FolditSpeedBoostManager:
    def __init__(self, log_callback=None):
        self.sessions: Dict[int, SpeedBoostSession] = {}
        self.log_callback = log_callback
        self._lock = threading.RLock()

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

    def start(self, pid: int, client_name: str = "", enabled: bool = False) -> bool:
        pid = int(pid)
        if frida is None:
            raise SpeedBoostUnavailable(unavailable_message())
        with self._lock:
            already_managed = pid in self.sessions
        if already_managed:
            self.set_enabled(pid, enabled)
            return True

        session = frida.attach(pid)
        script = session.create_script(_script_source())

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
        if not fast:
            try:
                stats = managed.script.exports_sync.cleanup()
                self.log(f"Speed boost pid={pid}: cleanup={stats}")
            except Exception as exc:
                self.log(f"Speed boost pid={pid}: cleanup failed: {exc}")
        try:
            managed.session.detach()
        except Exception:
            pass

    def stop(self, pid: int, fast: bool = True) -> None:
        self.detach(pid, fast=fast)

    def sync_window_state(self, pid: int, is_window_visible: bool) -> None:
        self.set_enabled(pid, not bool(is_window_visible))

    def prune(self, live_pids) -> None:
        live = {int(pid) for pid in live_pids}
        for pid in list(self.sessions):
            if pid not in live:
                self.forget(pid)

    def stop_all(self) -> None:
        for pid in list(self.sessions):
            self.detach(pid, fast=True)

    def abandon_all(self) -> None:
        """Forget sessions during app shutdown.

        Explicit Frida detach can take seconds per busy Foldit process. When the
        monitor process exits, Frida sessions are torn down by process shutdown.
        Use this only when the Python app is closing immediately.
        """
        with self._lock:
            self.sessions.clear()
