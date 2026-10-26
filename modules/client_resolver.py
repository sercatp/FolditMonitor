import ctypes
import hashlib
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence


SCRIPT_LOG_RE = re.compile(r"^scriptlog\.(?P<track>.*)\.xml$", re.IGNORECASE)


@lru_cache(maxsize=8192)
def _normalize_path_cached(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def normalize_path(path: str) -> str:
    """Return a cached stable comparison key for an existing or future path."""
    return _normalize_path_cached(str(path))


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
    window_title_available: bool = False
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


@dataclass(frozen=True)
class DirectoryObservation:
    candidates: tuple[str, ...]
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class _DirectoryState:
    directory_mtime_ns: int
    candidates: tuple[str, ...]
    stamps: tuple[tuple[str, int, int], ...]


class ScriptLogIndex:
    """Track the few script logs in a root and report file activity cheaply.

    A directory is enumerated only on first use or when its directory mtime
    changes. Ordinary refreshes stat only the already known script-log files.
    """

    def __init__(self):
        self._states: Dict[str, _DirectoryState] = {}

    @staticmethod
    def _directory_mtime_ns(path: str) -> int:
        try:
            return int(os.stat(path).st_mtime_ns)
        except OSError:
            return -1

    @staticmethod
    def _scan(data_root: str) -> tuple[str, ...]:
        matches: list[str] = []
        try:
            with os.scandir(data_root) as entries:
                for entry in entries:
                    if SCRIPT_LOG_RE.match(entry.name) and entry.is_file(follow_symlinks=False):
                        matches.append(normalize_path(entry.path))
        except OSError:
            pass
        return tuple(sorted(set(matches), key=str.casefold))

    @staticmethod
    def _stamps(paths: Sequence[str]) -> tuple[tuple[str, int, int], ...]:
        stamps: list[tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = os.stat(path)
                stamps.append((path, int(stat.st_mtime_ns), int(stat.st_size)))
            except OSError:
                stamps.append((path, -1, -1))
        return tuple(stamps)

    def observe(self, data_roots: Iterable[str], force_rescan: bool = False) -> Dict[str, DirectoryObservation]:
        roots = {normalize_path(root) for root in data_roots if str(root).strip()}
        for stale_root in set(self._states) - roots:
            self._states.pop(stale_root, None)

        observations: Dict[str, DirectoryObservation] = {}
        for root in roots:
            previous = self._states.get(root)
            directory_mtime = self._directory_mtime_ns(root)
            if force_rescan or previous is None or previous.directory_mtime_ns != directory_mtime:
                candidates = self._scan(root)
            else:
                candidates = previous.candidates
            stamps = self._stamps(candidates)

            changed_paths: set[str] = set()
            if previous is not None:
                old_stamps = {path: (mtime, size) for path, mtime, size in previous.stamps}
                new_stamps = {path: (mtime, size) for path, mtime, size in stamps}
                changed_paths.update(set(old_stamps) ^ set(new_stamps))
                changed_paths.update(
                    path
                    for path in set(old_stamps) & set(new_stamps)
                    if old_stamps[path] != new_stamps[path]
                )

            self._states[root] = _DirectoryState(directory_mtime, candidates, stamps)
            observations[root] = DirectoryObservation(
                candidates=candidates,
                changed_paths=tuple(sorted(changed_paths, key=str.casefold)),
            )
        return observations


@dataclass(frozen=True)
class ProbeResult:
    paths: tuple[str, ...] = ()
    error: str = ""


class OpenLogBackend:
    """Stateless platform-specific inspection of files opened by Foldit."""

    def __init__(self, system: Optional[str] = None):
        self.system = system or platform.system()

    @staticmethod
    def _process_key(client: Any) -> tuple[int, float]:
        return int(client.pid), float(getattr(client, "process_start_time", 0.0) or 0.0)

    @staticmethod
    def _filter_paths(paths: Iterable[str], data_root: str) -> tuple[str, ...]:
        root_key = normalize_path(data_root)
        prefix = root_key + os.sep
        found = {
            normalize_path(path)
            for path in paths
            if is_script_log_path(path)
            and (normalize_path(path) == root_key or normalize_path(path).startswith(prefix))
        }
        return tuple(sorted(found, key=str.casefold))

    def probe(
        self,
        clients: Sequence[Any],
        candidates_by_root: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> Dict[tuple[int, float], ProbeResult]:
        process_list = list(clients)
        if self.system == "Windows":
            return self._probe_windows(process_list, candidates_by_root or {})
        if self.system == "Darwin":
            return self._probe_macos(process_list)
        return self._probe_psutil(process_list)

    def _probe_psutil(self, clients: Sequence[Any]) -> Dict[tuple[int, float], ProbeResult]:
        results: Dict[tuple[int, float], ProbeResult] = {}
        for client in clients:
            key = self._process_key(client)
            try:
                paths = [opened.path for opened in client.process.open_files()]
                results[key] = ProbeResult(self._filter_paths(paths, client.data_root))
            except Exception as error:
                results[key] = ProbeResult(error=f"Open-file inspection failed: {error}")
        return results

    def _probe_macos(self, clients: Sequence[Any]) -> Dict[tuple[int, float], ProbeResult]:
        if not clients:
            return {}
        clients_by_pid = {int(client.pid): client for client in clients}
        pid_list = ",".join(str(pid) for pid in sorted(clients_by_pid))
        try:
            completed = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", pid_list, "-Fpn"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {
                self._process_key(client): ProbeResult(error=f"lsof failed: {error}")
                for client in clients
            }

        paths_by_pid: Dict[int, list[str]] = {}
        current_pid: Optional[int] = None
        for line in str(getattr(completed, "stdout", "") or "").splitlines():
            if line.startswith("p"):
                try:
                    current_pid = int(line[1:])
                except ValueError:
                    current_pid = None
            elif line.startswith("n") and current_pid in clients_by_pid:
                paths_by_pid.setdefault(current_pid, []).append(line[1:])

        returncode = int(getattr(completed, "returncode", 0) or 0)
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        shared_error = ""
        if stderr or returncode not in (0, 1):
            shared_error = f"lsof could not inspect the processes: {stderr or f'exit code {returncode}'}"

        results: Dict[tuple[int, float], ProbeResult] = {}
        for pid, client in clients_by_pid.items():
            results[self._process_key(client)] = ProbeResult(
                self._filter_paths(paths_by_pid.get(pid, ()), client.data_root),
                shared_error,
            )
        return results

    def _probe_windows(
        self,
        clients: Sequence[Any],
        candidates_by_root: Mapping[str, Sequence[str]],
    ) -> Dict[tuple[int, float], ProbeResult]:
        clients_by_root: Dict[str, list[Any]] = {}
        for client in clients:
            clients_by_root.setdefault(normalize_path(client.data_root), []).append(client)

        results: Dict[tuple[int, float], ProbeResult] = {}
        for root, root_clients in clients_by_root.items():
            candidates = (
                tuple(candidates_by_root[root])
                if root in candidates_by_root
                else ScriptLogIndex._scan(root)
            )
            paths_by_pid: Dict[int, list[str]] = {}
            errors: list[str] = []
            for path in candidates:
                try:
                    owners = self._restart_manager_users(path)
                except OSError as error:
                    errors.append(f"{os.path.basename(path)}: {error}")
                    continue
                for owner_pid in owners:
                    paths_by_pid.setdefault(int(owner_pid), []).append(path)
            detail = f"Restart Manager failed: {'; '.join(errors[:3])}" if errors else ""
            for client in root_clients:
                key = self._process_key(client)
                results[key] = ProbeResult(
                    tuple(sorted(set(paths_by_pid.get(int(client.pid), ())), key=str.casefold)),
                    detail,
                )
        return results

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
            status = restart_manager.RmStartSession(ctypes.byref(session), 0, session_key)
            if status != 0:
                raise OSError(status, "RmStartSession")
            try:
                resources = (wintypes.LPCWSTR * 1)(str(path))
                status = restart_manager.RmRegisterResources(session, 1, resources, 0, None, 0, None)
                if status != 0:
                    raise OSError(status, "RmRegisterResources")
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
                    raise OSError(status, "RmGetList(size)")
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
                    raise OSError(status, "RmGetList(data)")
                return {int(records[index].Process.dwProcessId) for index in range(count.value)}
            finally:
                restart_manager.RmEndSession(session)
        except OSError:
            raise
        except (AttributeError, ValueError) as error:
            raise OSError(str(error)) from error


@dataclass
class _OwnershipState:
    paths: tuple[str, ...] = ()
    checked_at: float = 0.0
    window_title: str = ""
    has_verified_title: bool = False
    error: str = ""


class ClientResolver:
    """Main-loop-owned PID-to-log binding controller.

    The class has no polling loop. ``submit`` observes cheap state and starts a
    short-lived background probe only after a meaningful event. On macOS and
    Linux, where a window title cannot be trusted as a complete signal, submit
    schedules a single batched probe at ``portable_probe_interval``.
    """

    def __init__(
        self,
        settings: Optional[dict] = None,
        backend: Optional[OpenLogBackend] = None,
        portable_probe_interval: float = 5.0,
        report: Callable[[str], None] = print,
    ):
        self.backend = backend or OpenLogBackend()
        self.portable_probe_interval = float(portable_probe_interval)
        self.report = report
        self.fallbacks = parse_log_fallbacks(settings, report=report)
        self._states: Dict[tuple[int, float], _OwnershipState] = {}
        self._log_index = ScriptLogIndex()
        self._result_lock = threading.Lock()
        self._completed_result: Optional[Dict[tuple[int, float], ProbeResult]] = None
        self._probe_thread: Optional[threading.Thread] = None
        self._pending_keys: set[tuple[int, float]] = set()
        self._stopped = False
        self._reported_conflicts: set[tuple[int, str, str]] = set()

    @staticmethod
    def _process_key(client: Any) -> tuple[int, float]:
        return int(client.pid), float(getattr(client, "process_start_time", 0.0) or 0.0)

    @staticmethod
    def _title_available(client: Any) -> bool:
        explicit = getattr(client, "window_title_available", None)
        if explicit is not None:
            return bool(explicit)
        return bool(str(getattr(client, "window_title", "") or ""))

    def submit(self, processes: Sequence[Any]) -> list[MonitoredClient]:
        process_list = list(processes)
        now = time.monotonic()
        self._consume_completed_result(now)
        live_by_key = {self._process_key(client): client for client in process_list}
        self._remove_stale_states(set(live_by_key))

        roots = {normalize_path(client.data_root) for client in process_list}
        observations = self._log_index.observe(roots) if self.backend.system == "Windows" else {}

        due_keys: set[tuple[int, float]] = set()
        event_keys: set[tuple[int, float]] = set()
        clients_by_root: Dict[str, list[Any]] = {}
        for client in process_list:
            key = self._process_key(client)
            root = normalize_path(client.data_root)
            clients_by_root.setdefault(root, []).append(client)
            state = self._states.get(key)
            if state is None:
                state = _OwnershipState()
                self._states[key] = state
                due_keys.add(key)
                event_keys.add(key)

            title = str(getattr(client, "window_title", "") or "")
            if self._title_available(client):
                if state.has_verified_title and state.window_title != title:
                    due_keys.add(key)
                    event_keys.add(key)
                state.window_title = title
                state.has_verified_title = True

            if state.paths and not all(os.path.exists(path) for path in state.paths):
                due_keys.add(key)
                event_keys.add(key)

            if self.backend.system != "Windows":
                if state.checked_at <= 0 or now - state.checked_at >= self.portable_probe_interval:
                    due_keys.add(key)

        snapshot = self._build_snapshot(process_list)

        if self.backend.system == "Windows":
            bound_by_root: Dict[str, set[str]] = {}
            for monitored in snapshot:
                if monitored.pid is not None and monitored.binding_status == "resolved" and monitored.log_path:
                    bound_by_root.setdefault(normalize_path(monitored.data_root), set()).add(
                        normalize_path(monitored.log_path)
                    )
            for root, observation in observations.items():
                bound_paths = bound_by_root.get(root, set())
                if any(path not in bound_paths for path in observation.changed_paths):
                    for client in clients_by_root.get(root, ()):
                        key = self._process_key(client)
                        due_keys.add(key)
                        event_keys.add(key)

        pending_live = self._pending_keys & set(live_by_key)
        due_keys.update(pending_live)
        event_keys.update(pending_live)
        self._pending_keys.clear()
        candidates_by_root = {root: observation.candidates for root, observation in observations.items()}
        self._request_probe(due_keys, event_keys, live_by_key, candidates_by_root)
        return snapshot

    def stop(self, timeout: float = 1.0):
        self._stopped = True
        thread = self._probe_thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))

    def _remove_stale_states(self, live_keys: set[tuple[int, float]]):
        for key in set(self._states) - live_keys:
            self._states.pop(key, None)
        self._pending_keys.intersection_update(live_keys)

    def _request_probe(
        self,
        due_keys: set[tuple[int, float]],
        event_keys: set[tuple[int, float]],
        live_by_key: Mapping[tuple[int, float], Any],
        candidates_by_root: Mapping[str, Sequence[str]],
    ):
        if self._stopped or not due_keys:
            return
        if self._probe_thread is not None and self._probe_thread.is_alive():
            self._pending_keys.update(event_keys)
            return

        request_clients = [live_by_key[key] for key in due_keys if key in live_by_key]
        if not request_clients:
            return
        request_candidates = {
            root: tuple(paths)
            for root, paths in candidates_by_root.items()
            if any(normalize_path(client.data_root) == root for client in request_clients)
        }
        def run_probe():
            try:
                results = self.backend.probe(request_clients, request_candidates)
            except Exception as error:
                results = {
                    self._process_key(client): ProbeResult(error=f"Open-log probe failed: {error}")
                    for client in request_clients
                }
            with self._result_lock:
                self._completed_result = results

        self._probe_thread = threading.Thread(
            target=run_probe,
            name="FolditLogProbe",
            daemon=True,
        )
        self._probe_thread.start()

    def _consume_completed_result(self, now: float):
        with self._result_lock:
            results = self._completed_result
            self._completed_result = None
        if results is None:
            return
        self._probe_thread = None
        self._apply_results(results, now)

    def _apply_results(self, results: Mapping[tuple[int, float], ProbeResult], now: float):
        for key, result in results.items():
            state = self._states.get(key)
            if state is None:
                continue
            state.checked_at = now
            if result.error:
                state.error = result.error
                if not result.paths:
                    continue
            else:
                state.error = ""
            discovered = tuple(dict.fromkeys(normalize_path(path) for path in result.paths))
            if not discovered and state.paths and all(os.path.exists(path) for path in state.paths):
                continue
            state.paths = discovered

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
            state = self._states.get(key, _OwnershipState())
            data_root = normalize_path(client.data_root)
            paths = list(state.paths)
            log_path = ""
            track = ""
            status = "waiting"
            source = ""
            detail = state.error or "Waiting for Foldit to open a script log"
            matched_fallback: Optional[LogFallback] = None

            if len(paths) == 1:
                log_path = paths[0]
                track = track_from_log_path(log_path) or ""
                status = "resolved"
                source = "os"
                detail = state.error
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
                        detail = state.error
                    elif len(existing_defaults) > 1:
                        status = "ambiguous"
                        detail = "Both default script-log naming variants exist and ownership is unknown"

            base_name = self._installation_name(client, data_root)
            client_name = self._display_name(base_name, track, matched_fallback)
            client_id = f"log:{normalize_path(log_path)}" if log_path else f"process:{client.pid}:{key[1]:.6f}"
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
                    window_title_available=self._title_available(client),
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
    "normalize_path",
]
