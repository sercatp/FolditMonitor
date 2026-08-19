import ctypes
import hashlib
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Sequence


SCRIPT_LOG_RE = re.compile(r"^scriptlog\.(?P<track>.*)\.xml$", re.IGNORECASE)


def normalize_path(path: str) -> str:
    """Return a stable comparison key for an existing or future path."""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def track_from_log_path(path: str) -> Optional[str]:
    match = SCRIPT_LOG_RE.match(os.path.basename(str(path)))
    return match.group("track") if match else None


def is_script_log_path(path: str) -> bool:
    return track_from_log_path(path) is not None


@dataclass(frozen=True)
class LogFallback:
    log_path: str
    name: str = ""
    title_suffix: str = ""
    puzzle_id: str = ""

    @property
    def data_root(self) -> str:
        return os.path.dirname(self.log_path)

    @property
    def track(self) -> str:
        return track_from_log_path(self.log_path) or ""


@dataclass
class MonitoredClient:
    client_id: str
    client_name: str
    data_root: str
    log_path: str
    track: str
    binding_status: str
    binding_source: str
    binding_detail: str = ""
    candidate_log_paths: tuple[str, ...] = ()
    puzzle_id_override: str = ""
    pid: Optional[int] = None
    process: Any = None
    process_start_time: float = 0.0
    executable_path: str = ""
    installation_path: str = ""
    window_info: Any = None
    window_title: str = ""
    is_window_visible: bool = False
    is_window_focused: bool = False

    @property
    def folder(self) -> str:
        """Compatibility alias for code that still calls the Foldit data root a folder."""
        return self.data_root

    @property
    def row_id(self) -> str:
        if self.pid is not None:
            return str(self.pid)
        digest = hashlib.sha1(self.client_id.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"configured-{digest}"


def parse_log_fallbacks(settings: Optional[dict], report: Callable[[str], None] = print) -> list[LogFallback]:
    monitoring = settings.get("monitoring", {}) if isinstance(settings, dict) else {}
    raw_entries = monitoring.get("log_fallbacks", []) if isinstance(monitoring, dict) else []
    if raw_entries is None:
        return []
    if not isinstance(raw_entries, list):
        report("Invalid monitoring.log_fallbacks: expected a JSON array")
        return []

    fallbacks: list[LogFallback] = []
    seen_paths: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            report(f"Invalid monitoring.log_fallbacks[{index}]: expected an object")
            continue
        raw_path = str(raw.get("log_path", "")).strip()
        if not raw_path or not os.path.isabs(os.path.expanduser(raw_path)):
            report(f"Invalid monitoring.log_fallbacks[{index}].log_path: an absolute path is required")
            continue
        log_path = normalize_path(os.path.expanduser(raw_path))
        if not is_script_log_path(log_path):
            report(
                f"Invalid monitoring.log_fallbacks[{index}].log_path: "
                "the file name must match scriptlog.<track>.xml"
            )
            continue
        if log_path in seen_paths:
            report(f"Duplicate monitoring log fallback ignored: {raw_path}")
            continue
        seen_paths.add(log_path)
        fallbacks.append(
            LogFallback(
                log_path=log_path,
                name=str(raw.get("name", "")).strip(),
                title_suffix=str(raw.get("title_suffix", "")).strip(),
                puzzle_id=str(raw.get("puzzle_id", "")).strip(),
            )
        )
    return fallbacks


class DirectoryLogCache:
    """Cache the small set of script logs without rescanning a large directory each tick."""

    def __init__(self, rescan_seconds: float = 60.0):
        self.rescan_seconds = float(rescan_seconds)
        self._entries: Dict[str, tuple[int, float, tuple[str, ...]]] = {}

    @staticmethod
    def _directory_mtime_ns(path: str) -> int:
        try:
            return int(os.stat(path).st_mtime_ns)
        except OSError:
            return -1

    def candidates(self, data_root: str, now: Optional[float] = None, force: bool = False) -> list[str]:
        current_time = time.monotonic() if now is None else float(now)
        root_key = normalize_path(data_root)
        directory_mtime = self._directory_mtime_ns(root_key)
        cached = self._entries.get(root_key)
        if (
            not force
            and cached is not None
            and cached[0] == directory_mtime
            and current_time - cached[1] < self.rescan_seconds
        ):
            return list(cached[2])

        matches: list[str] = []
        try:
            with os.scandir(root_key) as entries:
                for entry in entries:
                    if SCRIPT_LOG_RE.match(entry.name) and entry.is_file(follow_symlinks=False):
                        matches.append(normalize_path(entry.path))
        except OSError:
            pass
        matches.sort(key=str.casefold)
        self._entries[root_key] = (directory_mtime, current_time, tuple(matches))
        return matches


class OpenLogBackend:
    def __init__(self, system: Optional[str] = None, directory_cache: Optional[DirectoryLogCache] = None):
        self.system = system or platform.system()
        self.directory_cache = directory_cache or DirectoryLogCache()
        self.diagnostics: Dict[int, str] = {}
        self._windows_owner_cache: Dict[
            str,
            tuple[tuple[str, ...], float, Dict[int, tuple[str, ...]]],
        ] = {}

    def diagnostic(self, pid: int) -> str:
        return str(self.diagnostics.get(int(pid), ""))

    def open_logs(self, process: Any, data_root: str) -> list[str]:
        if self.system == "Windows":
            return self._windows_open_logs(int(process.pid), data_root)
        if self.system == "Darwin":
            return self._macos_open_logs(int(process.pid), data_root)
        return self._psutil_open_logs(process, data_root)

    @staticmethod
    def _filter_paths(paths: Iterable[str], data_root: str) -> list[str]:
        root_key = normalize_path(data_root)
        prefix = root_key + os.sep
        found = {
            normalize_path(path)
            for path in paths
            if is_script_log_path(path)
            and (normalize_path(path) == root_key or normalize_path(path).startswith(prefix))
        }
        return sorted(found, key=str.casefold)

    def _psutil_open_logs(self, process: Any, data_root: str) -> list[str]:
        try:
            paths = [opened.path for opened in process.open_files()]
            self.diagnostics.pop(int(process.pid), None)
        except Exception as error:
            self.diagnostics[int(process.pid)] = f"Open-file inspection failed: {error}"
            return []
        return self._filter_paths(paths, data_root)

    def _macos_open_logs(self, pid: int, data_root: str) -> list[str]:
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", str(int(pid)), "-Fn"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.diagnostics[int(pid)] = f"lsof failed: {error}"
            return []
        paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
        filtered = self._filter_paths(paths, data_root)
        returncode = int(getattr(result, "returncode", 0) or 0)
        stderr = str(getattr(result, "stderr", "") or "").strip()
        if not filtered and (stderr or returncode not in (0, 1)):
            detail = stderr or f"exit code {returncode}"
            self.diagnostics[int(pid)] = f"lsof could not inspect the process: {detail}"
        else:
            self.diagnostics.pop(int(pid), None)
        return filtered

    def _windows_open_logs(self, pid: int, data_root: str) -> list[str]:
        candidates = self.directory_cache.candidates(data_root)
        root_key = normalize_path(data_root)
        candidate_key = tuple(candidates)
        now = time.monotonic()
        cached = self._windows_owner_cache.get(root_key)
        if cached and cached[0] == candidate_key and now - cached[1] < 2.0:
            return list(cached[2].get(int(pid), ()))

        paths_by_pid: Dict[int, list[str]] = {}
        for path in candidates:
            for owner_pid in self._restart_manager_users(path):
                paths_by_pid.setdefault(int(owner_pid), []).append(path)
        frozen_map = {
            owner_pid: tuple(paths)
            for owner_pid, paths in paths_by_pid.items()
        }
        self._windows_owner_cache[root_key] = (candidate_key, now, frozen_map)
        return list(frozen_map.get(int(pid), ()))

    @staticmethod
    def _restart_manager_users(path: str) -> set[int]:
        """Return PIDs using one file through the public Windows Restart Manager API."""
        if platform.system() != "Windows":
            return set()

        from ctypes import wintypes

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [
                ("Process", RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * 256),
                ("strServiceShortName", wintypes.WCHAR * 64),
                ("ApplicationType", wintypes.DWORD),
                ("AppStatus", wintypes.ULONG),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL),
            ]

        try:
            restart_manager = ctypes.WinDLL("Rstrtmgr")
            session = wintypes.DWORD()
            session_key = ctypes.create_unicode_buffer(33)
            if restart_manager.RmStartSession(ctypes.byref(session), 0, session_key) != 0:
                return set()
            try:
                resources = (wintypes.LPCWSTR * 1)(str(path))
                if restart_manager.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
                    return set()
                needed = wintypes.UINT(0)
                count = wintypes.UINT(0)
                reasons = wintypes.DWORD(0)
                status = restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    None,
                    ctypes.byref(reasons),
                )
                if status == 0 and needed.value == 0:
                    return set()
                if status not in (0, 234):
                    return set()
                count = wintypes.UINT(max(1, needed.value))
                records = (RM_PROCESS_INFO * count.value)()
                status = restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    records,
                    ctypes.byref(reasons),
                )
                if status != 0:
                    return set()
                return {int(records[index].Process.dwProcessId) for index in range(count.value)}
            finally:
                restart_manager.RmEndSession(session)
        except (AttributeError, OSError, ValueError):
            return set()


@dataclass
class _OwnershipState:
    paths: tuple[str, ...] = ()
    checked_at: float = 0.0
    window_title: str = ""


class ClientResolver:
    """Resolve Foldit processes into independently monitored log sources."""

    def __init__(
        self,
        settings: Optional[dict] = None,
        backend: Optional[OpenLogBackend] = None,
        waiting_interval: float = 5.0,
        resolved_interval: float = 30.0,
        start_worker: bool = True,
        report: Callable[[str], None] = print,
    ):
        self.backend = backend or OpenLogBackend()
        self.waiting_interval = float(waiting_interval)
        self.resolved_interval = float(resolved_interval)
        self.report = report
        self.fallbacks = parse_log_fallbacks(settings, report=report)
        self._lock = threading.RLock()
        self._states: Dict[tuple[int, float], _OwnershipState] = {}
        self._latest_processes: list[Any] = []
        self._snapshot: list[MonitoredClient] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reported_conflicts: set[tuple[int, str, str]] = set()
        self._root_mtimes: Dict[str, int] = {}
        if start_worker:
            self._thread = threading.Thread(target=self._worker, name="FolditLogResolver", daemon=True)
            self._thread.start()

    @staticmethod
    def _process_key(client: Any) -> tuple[int, float]:
        return int(client.pid), float(getattr(client, "process_start_time", 0.0) or 0.0)

    def submit(self, processes: Sequence[Any]) -> list[MonitoredClient]:
        with self._lock:
            self._latest_processes = list(processes)
            snapshot = self._build_snapshot(self._latest_processes)
            self._snapshot = snapshot
        self._wake.set()
        return list(snapshot)

    def get_snapshot(self) -> list[MonitoredClient]:
        with self._lock:
            return list(self._snapshot)

    def resolve_now(self, processes: Sequence[Any], force: bool = True) -> list[MonitoredClient]:
        process_list = list(processes)
        now = time.monotonic()
        self._refresh_states(process_list, now, force=force)
        with self._lock:
            self._latest_processes = process_list
            self._snapshot = self._build_snapshot(process_list)
            return list(self._snapshot)

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(0.0, float(timeout)))

    def _worker(self):
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            with self._lock:
                processes = list(self._latest_processes)
            if not processes and not self.fallbacks:
                continue
            self._refresh_states(processes, time.monotonic(), force=False)
            with self._lock:
                self._snapshot = self._build_snapshot(processes)

    def _refresh_states(self, processes: Sequence[Any], now: float, force: bool):
        live_keys = {self._process_key(client) for client in processes}
        with self._lock:
            stale_keys = set(self._states) - live_keys
            for key in stale_keys:
                self._states.pop(key, None)

        changed_roots: set[str] = set()
        current_root_mtimes: Dict[str, int] = {}
        for client in processes:
            root_key = normalize_path(client.data_root)
            if root_key in current_root_mtimes:
                continue
            try:
                current_mtime = int(os.stat(root_key).st_mtime_ns)
            except OSError:
                current_mtime = -1
            previous_mtime = self._root_mtimes.get(root_key)
            if previous_mtime is not None and previous_mtime != current_mtime:
                changed_roots.add(root_key)
            current_root_mtimes[root_key] = current_mtime
        self._root_mtimes = current_root_mtimes

        for client in processes:
            key = self._process_key(client)
            title = str(getattr(client, "window_title", "") or "")
            with self._lock:
                state = self._states.get(key, _OwnershipState())
            interval = self.resolved_interval if len(state.paths) == 1 else self.waiting_interval
            path_missing = bool(state.paths and not all(os.path.exists(path) for path in state.paths))
            due = (
                force
                or state.checked_at <= 0
                or now - state.checked_at >= interval
                or state.window_title != title
                or path_missing
                or normalize_path(client.data_root) in changed_roots
            )
            if not due:
                continue
            try:
                discovered_paths = tuple(
                    normalize_path(path)
                    for path in self.backend.open_logs(client.process, client.data_root)
                    if is_script_log_path(path)
                )
                discovered_paths = tuple(dict.fromkeys(discovered_paths))
                if (
                    not discovered_paths
                    and state.paths
                    and all(os.path.exists(path) for path in state.paths)
                ):
                    # Foldit may close its log between scripts. Keep the last
                    # proven binding until a new open log supersedes it.
                    paths = state.paths
                else:
                    paths = discovered_paths
            except Exception as error:
                self.report(f"Could not resolve Foldit log for PID {client.pid}: {error}")
                paths = state.paths
            with self._lock:
                self._states[key] = _OwnershipState(paths=paths, checked_at=now, window_title=title)

    @staticmethod
    def _fallback_title_matches(fallback: LogFallback, client: Any) -> bool:
        title = str(getattr(client, "window_title", "") or "")
        if not title:
            return False
        suffix = fallback.title_suffix
        if not suffix and fallback.track and fallback.track.casefold() != "default":
            suffix = f" - {fallback.track}"
        return bool(suffix and title.endswith(suffix))

    @staticmethod
    def _installation_name(client: Any, data_root: str) -> str:
        name = str(getattr(client, "client_name", "") or "").strip()
        if name:
            return name
        installation = str(getattr(client, "installation_path", "") or "").rstrip(os.sep)
        if installation:
            base = os.path.basename(installation)
            return os.path.splitext(base)[0] if base.casefold().endswith(".app") else base
        return os.path.basename(data_root.rstrip(os.sep)) or "Foldit"

    @staticmethod
    def _display_name(base: str, track: str, fallback: Optional[LogFallback]) -> str:
        if fallback and fallback.name:
            return fallback.name
        if track and track.casefold() != "default":
            return f"{base} [{track}]"
        return base

    def _build_snapshot(self, processes: Sequence[Any]) -> list[MonitoredClient]:
        roots: Dict[str, list[Any]] = {}
        for client in processes:
            roots.setdefault(normalize_path(client.data_root), []).append(client)

        fallback_by_path = {normalize_path(item.log_path): item for item in self.fallbacks}
        used_fallback_paths: set[str] = set()
        resolved: list[MonitoredClient] = []

        for client in processes:
            key = self._process_key(client)
            with self._lock:
                state = self._states.get(key, _OwnershipState())
            data_root = normalize_path(client.data_root)
            paths = list(state.paths)
            log_path = ""
            track = ""
            status = "waiting"
            source = ""
            detail = "Waiting for Foldit to open a script log"
            matched_fallback: Optional[LogFallback] = None

            if len(paths) == 1:
                log_path = paths[0]
                track = track_from_log_path(log_path) or ""
                status = "resolved"
                source = "os"
                detail = ""
                matched_fallback = fallback_by_path.get(log_path)
                if matched_fallback:
                    used_fallback_paths.add(log_path)
                for fallback in self.fallbacks:
                    fallback_path = normalize_path(fallback.log_path)
                    if (
                        fallback_path != log_path
                        and normalize_path(fallback.data_root) == data_root
                        and self._fallback_title_matches(fallback, client)
                    ):
                        conflict_key = (int(client.pid), log_path, fallback_path)
                        if conflict_key not in self._reported_conflicts:
                            self._reported_conflicts.add(conflict_key)
                            self.report(
                                f"Ignoring conflicting JSON log binding for PID {client.pid}: "
                                f"the process owns {log_path}, configured {fallback_path}"
                            )
            elif len(paths) > 1:
                status = "ambiguous"
                detail = "Process has more than one open script log"
            else:
                title_matches = [
                    fallback
                    for fallback in self.fallbacks
                    if normalize_path(fallback.data_root) == data_root
                    and self._fallback_title_matches(fallback, client)
                ]
                if len(title_matches) == 1:
                    matched_fallback = title_matches[0]
                    log_path = normalize_path(matched_fallback.log_path)
                    track = matched_fallback.track
                    status = "resolved" if os.path.exists(log_path) else "waiting"
                    source = "json"
                    detail = "" if status == "resolved" else "Configured script log does not exist yet"
                    used_fallback_paths.add(log_path)
                elif len(title_matches) > 1:
                    status = "ambiguous"
                    detail = "More than one JSON fallback matches the window title"
                elif len(roots.get(data_root, ())) == 1:
                    default_names = (
                        ("scriptlog..xml", "scriptlog.default.xml")
                        if self.backend.system == "Darwin"
                        else ("scriptlog.default.xml", "scriptlog..xml")
                    )
                    existing_defaults = [
                        normalize_path(os.path.join(data_root, name))
                        for name in default_names
                        if os.path.isfile(os.path.join(data_root, name))
                    ]
                    if len(existing_defaults) == 1:
                        log_path = existing_defaults[0]
                        track = track_from_log_path(log_path) or ""
                        status = "resolved"
                        source = "single_default"
                        detail = ""
                    elif len(existing_defaults) > 1:
                        status = "ambiguous"
                        detail = "Both default script-log naming variants exist and ownership is unknown"

                if status == "waiting" and not source:
                    diagnostic = getattr(self.backend, "diagnostic", lambda _pid: "")(client.pid)
                    if diagnostic:
                        detail = diagnostic

            base_name = self._installation_name(client, data_root)
            client_name = self._display_name(base_name, track, matched_fallback)
            if log_path:
                client_id = f"log:{normalize_path(log_path)}"
            else:
                client_id = f"process:{client.pid}:{key[1]:.6f}"
            resolved.append(
                MonitoredClient(
                    client_id=client_id,
                    client_name=client_name,
                    data_root=data_root,
                    log_path=log_path,
                    track=track,
                    binding_status=status,
                    binding_source=source,
                    binding_detail=detail,
                    candidate_log_paths=tuple(paths),
                    puzzle_id_override=matched_fallback.puzzle_id if matched_fallback else "",
                    pid=int(client.pid),
                    process=client.process,
                    process_start_time=key[1],
                    executable_path=str(getattr(client, "executable_path", "") or ""),
                    installation_path=str(getattr(client, "installation_path", "") or ""),
                    window_info=getattr(client, "window_info", None),
                    window_title=str(getattr(client, "window_title", "") or ""),
                    is_window_visible=bool(getattr(client, "is_window_visible", False)),
                    is_window_focused=bool(getattr(client, "is_window_focused", False)),
                )
            )

        by_path: Dict[str, list[MonitoredClient]] = {}
        for client in resolved:
            if client.pid is not None and client.log_path:
                by_path.setdefault(normalize_path(client.log_path), []).append(client)
        for path, owners in by_path.items():
            if len(owners) <= 1:
                continue
            for client in owners:
                client.binding_status = "collision"
                client.binding_detail = "The same script log is open in more than one Foldit process"
                client.client_id = f"collision:{path}:{client.pid}:{client.process_start_time:.6f}"

        existing_paths = {normalize_path(client.log_path) for client in resolved if client.log_path}
        for fallback in self.fallbacks:
            fallback_path = normalize_path(fallback.log_path)
            if fallback_path in used_fallback_paths or fallback_path in existing_paths:
                continue
            data_root = normalize_path(fallback.data_root)
            base_name = os.path.basename(data_root.rstrip(os.sep)) or "Foldit"
            if base_name.casefold() == "resources":
                app_path = data_root
                while app_path and not app_path.casefold().endswith(".app"):
                    parent = os.path.dirname(app_path)
                    if parent == app_path:
                        break
                    app_path = parent
                if app_path.casefold().endswith(".app"):
                    base_name = os.path.splitext(os.path.basename(app_path))[0]
            status = "resolved" if os.path.isfile(fallback_path) else "waiting"
            resolved.append(
                MonitoredClient(
                    client_id=f"log:{fallback_path}",
                    client_name=self._display_name(base_name, fallback.track, fallback),
                    data_root=data_root,
                    log_path=fallback_path,
                    track=fallback.track,
                    binding_status=status,
                    binding_source="json",
                    binding_detail="" if status == "resolved" else "Configured script log does not exist yet",
                    candidate_log_paths=(fallback_path,),
                    puzzle_id_override=fallback.puzzle_id,
                )
            )

        names_by_root: Dict[tuple[str, str], list[MonitoredClient]] = {}
        for client in resolved:
            names_by_root.setdefault(
                (normalize_path(client.data_root), client.client_name.casefold()),
                [],
            ).append(client)
        for same_name_clients in names_by_root.values():
            distinct_paths = {
                normalize_path(client.log_path)
                for client in same_name_clients
                if client.log_path
            }
            if len(distinct_paths) <= 1:
                continue
            for client in same_name_clients:
                track_label = client.track or "unnamed"
                client.client_name = f"{client.client_name} [{track_label}]"

        resolved.sort(key=lambda item: (item.pid is None, item.client_name.casefold(), item.row_id))
        return resolved


__all__ = [
    "ClientResolver",
    "DirectoryLogCache",
    "LogFallback",
    "MonitoredClient",
    "OpenLogBackend",
    "is_script_log_path",
    "normalize_path",
    "parse_log_fallbacks",
    "track_from_log_path",
]
