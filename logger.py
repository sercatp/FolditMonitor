import datetime
import os
import re
import shutil
import threading
from collections import deque
from typing import Any, Dict, List, Optional

from window_manager import open_file as open_exported_file

APPEND_PROBE_BYTES = 64


class FolditLogHandler:
    def __init__(self, settings):
        self.settings = settings
        self.current_handlers: Dict[str, "LogFileHandler"] = {}
        self.managed_exports: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def start_monitoring(self, file_path: str):
        """Start monitoring for a specific file or return the existing handler."""
        with self.lock:
            handler = self.current_handlers.get(file_path)
            if handler is None:
                handler = LogFileHandler(self.settings, file_path)
                self.current_handlers[file_path] = handler
                handler.start()
        return handler

    def stop_monitoring(self, file_path: str):
        """Stop monitoring for a specific file."""
        with self.lock:
            handler = self.current_handlers.pop(file_path, None)
        if handler is not None:
            handler.stop()

    def stop_all_monitoring(self):
        """Stop all monitoring."""
        with self.lock:
            handlers = list(self.current_handlers.values())
            self.current_handlers.clear()
        for handler in handlers:
            handler.stop()

    def get_data(self, file_path: str) -> Optional[dict]:
        """Get cached data for a specific file."""
        with self.lock:
            handler = self.current_handlers.get(file_path)
        return handler.get_data() if handler else None

    def consume_stats_events(self, file_path: str) -> List[dict]:
        with self.lock:
            handler = self.current_handlers.get(file_path)
        return handler.consume_stats_events() if handler else []

    def get_fresh_data(self, file_path: str) -> Optional[dict]:
        """Read the current file contents immediately and return a fresh snapshot."""
        with self.lock:
            handler = self.current_handlers.get(file_path)

        if handler is not None:
            return handler.refresh_now()

        if not os.path.exists(file_path):
            return None

        temp_handler = LogFileHandler(self.settings, file_path)
        return temp_handler.refresh_now()

    @staticmethod
    def _extract_stats_snapshot_from_data(data: Optional[dict]) -> Optional[dict]:
        if not isinstance(data, dict):
            return None

        snapshot = data.get("stats_snapshot")
        if not isinstance(snapshot, dict):
            return None

        script_value = str(snapshot.get("script", "")).strip()
        score_value = snapshot.get("score")
        if not script_value:
            return None

        return {
            "script": script_value,
            "score": score_value,
            "rule": str(snapshot.get("rule", "")).strip(),
        }

    def get_stats_snapshot(self, file_path: str, fresh: bool = False) -> Optional[dict]:
        data = self.get_fresh_data(file_path) if fresh else self.get_data(file_path)
        return self._extract_stats_snapshot_from_data(data)

    def _managed_exports_enabled(self) -> bool:
        value = self.settings.get("managed_log_exports", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    @staticmethod
    def _get_log_source_identity(script_path: str, data: dict) -> Optional[tuple[int, int, int]]:
        try:
            stat = os.stat(script_path)
        except FileNotFoundError:
            return None

        try:
            run_token = max(0, int(data.get("script_change_token", 0) or 0))
        except (TypeError, ValueError):
            run_token = 0

        return run_token, stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _safe_remove_managed_partial(path: Optional[str]):
        if not path:
            return
        if not os.path.basename(path).endswith(".part.txt"):
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"Error removing partial log export {path}: {exc}")

    @staticmethod
    def _get_unique_export_path(folder_path: str, stem: str, status: str) -> str:
        candidate = os.path.join(folder_path, f"{stem}.{status}.txt")
        if not os.path.exists(candidate):
            return candidate

        for index in range(2, 1000):
            candidate = os.path.join(folder_path, f"{stem}.{index}.{status}.txt")
            if not os.path.exists(candidate):
                return candidate

        raise RuntimeError("Cannot allocate log export filename")

    @staticmethod
    def _build_export_stem(folder_path: str, data: dict, puzzle_id: Optional[str], include_seconds: bool = False) -> str:
        timestamp_format = "%Y%m%d.%H%M%S" if include_seconds else "%Y%m%d.%H%M"
        clpbrd = datetime.datetime.now().strftime(timestamp_format)
        highest_score = int(data["highest_score"]) if data["highest_score"] else None
        script_type = data["script_type"]

        folder_name = os.path.basename(folder_path)
        folder_name_short = folder_name.replace("oldit", "")
        puzzle_prefix = f".{str(puzzle_id).strip()} " if str(puzzle_id).strip() else "."
        return f"{folder_name_short}{puzzle_prefix}{script_type}.{highest_score}.{clpbrd}"

    def _export_log_legacy(
        self,
        folder_path: str,
        script_path: str,
        data: dict,
        open_file: bool,
        puzzle_id: Optional[str],
    ) -> Optional[str]:
        stem = self._build_export_stem(folder_path, data, puzzle_id)
        clpbrd_path = os.path.join(folder_path, f"{stem}.txt")

        try:
            shutil.copy(script_path, clpbrd_path)
        except FileNotFoundError:
            print("Script log file not found")
            return None

        if open_file:
            open_exported_file(clpbrd_path, reveal_end=True)
        return clpbrd_path

    def _export_log_managed(
        self,
        folder_path: str,
        script_path: str,
        data: dict,
        open_file: bool,
        puzzle_id: Optional[str],
    ) -> Optional[str]:
        source_identity = self._get_log_source_identity(script_path, data)
        if source_identity is None:
            print("Script log file not found")
            return None

        run_token, source_size, source_mtime_ns = source_identity
        run_open = bool(data.get("run_open", False))

        with self.lock:
            remembered = self.managed_exports.get(script_path)
            if remembered and remembered.get("run_token") != run_token:
                remembered = None

            if remembered:
                remembered_path = remembered.get("export_path")
                unchanged = (
                    remembered.get("source_size") == source_size
                    and remembered.get("source_mtime_ns") == source_mtime_ns
                )
                remembered_exists = bool(remembered_path and os.path.exists(remembered_path))
                remembered_final = bool(remembered.get("final", False))
                if unchanged and remembered_exists and (run_open or remembered_final):
                    if open_file:
                        open_exported_file(remembered_path, reveal_end=True)
                    return remembered_path

            old_partial_path = None
            if remembered and not bool(remembered.get("final", False)):
                old_partial_path = remembered.get("export_path")

        status = "part" if run_open else "fin"
        stem = self._build_export_stem(folder_path, data, puzzle_id, include_seconds=True)
        try:
            export_path = self._get_unique_export_path(folder_path, stem, status)
            shutil.copy(script_path, export_path)
        except FileNotFoundError:
            print("Script log file not found")
            return None
        except OSError as exc:
            print(f"Error exporting log file: {exc}")
            return None

        if not run_open:
            self._safe_remove_managed_partial(old_partial_path)
        elif old_partial_path and old_partial_path != export_path:
            self._safe_remove_managed_partial(old_partial_path)

        with self.lock:
            self.managed_exports[script_path] = {
                "run_token": run_token,
                "source_size": source_size,
                "source_mtime_ns": source_mtime_ns,
                "export_path": export_path,
                "final": not run_open,
            }

        if open_file:
            open_exported_file(export_path, reveal_end=True)
        return export_path

    def export_log(self, folder_path: str, open_file: bool = True, puzzle_id: Optional[str] = None):
        """Export log file with formatted name."""
        script_path = os.path.join(folder_path, "scriptlog.default.xml")
        data = self.get_data(script_path)
        if not data:
            data = self.get_fresh_data(script_path)

        if not data:
            print("No log data available")
            return None

        if not self._managed_exports_enabled():
            return self._export_log_legacy(folder_path, script_path, data, open_file, puzzle_id)
        return self._export_log_managed(folder_path, script_path, data, open_file, puzzle_id)


class LogFileHandler:
    def __init__(self, settings, file_path):
        self.settings = settings
        self.file_path = file_path
        self.tail_capacity = max(
            int(self.settings["MAX_LINES"]),
            int(self.settings["tooltip_lines"]),
        )
        self.data = {
            "script_name": "",
            "script_type": "",
            "highest_score": None,
            "script_column_number": 0,
            "script_change_token": 0,
            "run_open": False,
            "last_log_lines": [],
            "script_state_snapshot": None,
            "stats_snapshot": None,
        }
        self.running = False
        self.timer = None
        self.last_mtime_ns = None
        self.last_size = None
        self.last_read_position = 0
        self._line_count = 0
        self._tail_lines = deque(maxlen=self.tail_capacity)
        self._highest_score = None
        self._highest_pattern_scores = self._new_pattern_score_state()
        self._script_name = ""
        self._script_type = ""
        self._script_column_number = 0
        self._script_change_token = 0
        self._script_state_snapshot = None
        self._current_state_rules = []
        self._run_open = False
        self._tail_mode = "continue"
        self._stats_events = deque()
        self._last_emitted_script_payload = None
        self._last_emitted_state_payload = None
        self._append_probe = b""
        self._append_probe_size = 0
        self.lock = threading.Lock()

    def start(self):
        if self.running:
            return
        self.running = True
        self._update_data()

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.cancel()

    def _schedule_update(self):
        if self.running:
            self.timer = threading.Timer(
                self.settings["CHECK_INTERVAL"],
                self._update_data,
            )
            self.timer.start()

    def _reset_cached_log_state(self):
        self._tail_lines.clear()
        self._line_count = 0
        self.last_read_position = 0
        self._highest_score = None
        self._highest_pattern_scores = self._new_pattern_score_state()
        self._script_name = ""
        self._script_type = ""
        self._script_column_number = 0
        self._script_change_token = 0
        self._script_state_snapshot = None
        self._current_state_rules = []
        self._run_open = False
        self._tail_mode = "continue"
        self._last_emitted_script_payload = None
        self._last_emitted_state_payload = None
        self._append_probe = b""
        self._append_probe_size = 0

    def _build_stats_snapshot(self):
        snapshot = self._script_state_snapshot
        if not isinstance(snapshot, dict):
            return None

        script_value = str(snapshot.get("script_value", "")).strip()
        score_value = str(snapshot.get("score_value", "")).strip()
        if not script_value:
            return None

        numeric_score = None
        if score_value:
            try:
                numeric_score = float(score_value)
            except (TypeError, ValueError):
                numeric_score = None

        return {
            "script": script_value,
            "score": numeric_score,
            "rule": str(snapshot.get("rule", "")).strip(),
        }

    def _publish_cached_data(self):
        self.data.update(
            {
                "script_name": self._script_name,
                "script_type": self._script_type,
                "highest_score": self._highest_score,
                "script_column_number": self._script_column_number,
                "script_change_token": self._script_change_token,
                "run_open": self._run_open,
                "last_log_lines": self._get_last_log_lines(),
                "script_state_snapshot": self._script_state_snapshot,
                "stats_snapshot": self._build_stats_snapshot(),
            }
        )

    def _enqueue_stats_event(self, event: Dict[str, Any]):
        self._stats_events.append(dict(event))

    def _sync_stats_event_baseline(self):
        if not self._run_open:
            self._tail_mode = "continue"
            self._last_emitted_script_payload = None
            self._last_emitted_state_payload = None
            return

        script_payload = None
        if self._script_type and self._highest_score is not None:
            script_payload = (
                str(self._script_type).strip(),
                float(self._highest_score),
            )
        self._last_emitted_script_payload = script_payload

        state_payload = None
        snapshot = self._build_stats_snapshot()
        if isinstance(snapshot, dict):
            script_value = str(snapshot.get("script", "")).strip()
            if script_value:
                state_payload = (
                    script_value,
                    snapshot.get("score"),
                )
        self._last_emitted_state_payload = state_payload
        self._tail_mode = "continue"

    def _queue_stats_events(self):
        if not self._run_open:
            self._sync_stats_event_baseline()
            return

        run_started = self._tail_mode != "continue"

        script_payload = None
        if self._script_type and self._highest_score is not None:
            script_payload = (
                str(self._script_type).strip(),
                float(self._highest_score),
            )
            if run_started or script_payload != self._last_emitted_script_payload:
                self._enqueue_stats_event(
                    {
                        "kind": "script",
                        "script": script_payload[0],
                        "score": script_payload[1],
                        "continue_tail": not run_started,
                    }
                )
            self._last_emitted_script_payload = script_payload

        state_payload = None
        snapshot = self._build_stats_snapshot()
        if isinstance(snapshot, dict):
            script_value = str(snapshot.get("script", "")).strip()
            if script_value:
                state_payload = (
                    script_value,
                    snapshot.get("score"),
                )
                if run_started or state_payload != self._last_emitted_state_payload:
                    self._enqueue_stats_event(
                        {
                            "kind": "state",
                            "script": state_payload[0],
                            "score": state_payload[1],
                            "continue_tail": not run_started,
                        }
                    )
        self._last_emitted_state_payload = state_payload
        self._tail_mode = "continue"

    def _get_append_probe_bytes(self) -> int:
        try:
            probe_bytes = int(self.settings.get("append_probe_bytes", APPEND_PROBE_BYTES))
        except (TypeError, ValueError, AttributeError):
            probe_bytes = APPEND_PROBE_BYTES
        return max(1, probe_bytes)

    def _update_append_probe(self, file_size: Optional[int] = None):
        if file_size is None:
            try:
                file_size = os.path.getsize(self.file_path)
            except OSError:
                file_size = 0

        try:
            normalized_size = max(0, int(file_size or 0))
        except (TypeError, ValueError):
            normalized_size = 0

        probe = b""
        probe_len = min(self._get_append_probe_bytes(), normalized_size)
        if probe_len > 0:
            try:
                with open(self.file_path, "rb") as f:
                    f.seek(normalized_size - probe_len)
                    probe = f.read(probe_len)
            except OSError:
                probe = b""
                normalized_size = 0

        with self.lock:
            self._append_probe = probe
            self._append_probe_size = normalized_size

    def _looks_like_append(self, current_size: int, last_size: int) -> bool:
        if last_size <= 0:
            return True

        with self.lock:
            previous_probe = self._append_probe
            previous_probe_size = self._append_probe_size

        if previous_probe_size != last_size or not previous_probe:
            return False

        probe_len = min(len(previous_probe), last_size, current_size)
        if probe_len <= 0:
            return False

        try:
            with open(self.file_path, "rb") as f:
                f.seek(last_size - probe_len)
                current_probe = f.read(probe_len)
        except OSError:
            return False

        return len(current_probe) == probe_len and current_probe == previous_probe[-probe_len:]

    def _update_data(self):
        try:
            if not os.path.exists(self.file_path):
                return

            try:
                stat = os.stat(self.file_path)
            except FileNotFoundError:
                return

            current_mtime_ns = stat.st_mtime_ns
            current_size = stat.st_size

            with self.lock:
                if self.last_mtime_ns == current_mtime_ns and self.last_size == current_size:
                    return
                last_size = self.last_size

            full_reload = (
                last_size is None
                or current_size < last_size
                or current_size == last_size
            )
            if not full_reload and not self._looks_like_append(current_size, last_size):
                full_reload = True

            if full_reload:
                self._reload_from_start(bootstrap_attach=last_size is None)
            else:
                self._read_appended_tail()

            self._update_append_probe(current_size)

            with self.lock:
                self._publish_cached_data()
                self._queue_stats_events()
                self.last_mtime_ns = current_mtime_ns
                self.last_size = current_size

        except Exception as e:
            print(f"Error updating {self.file_path}: {e}")
        finally:
            self._schedule_update()

    def _reload_from_start(self, bootstrap_attach: bool = False):
        with self.lock:
            self._reset_cached_log_state()

        with open(self.file_path, "rb") as f:
            self._consume_stream(f, bootstrap_attach=bootstrap_attach)

    def _read_appended_tail(self):
        with self.lock:
            read_position = self.last_read_position

        with open(self.file_path, "rb") as f:
            f.seek(read_position)
            self._consume_stream(f, mark_appended=True)

    def refresh_now(self):
        if not os.path.exists(self.file_path):
            return None

        with self.lock:
            self._reset_cached_log_state()

        with open(self.file_path, "rb") as f:
            self._consume_stream(f)

        try:
            stat = os.stat(self.file_path)
        except FileNotFoundError:
            stat = None

        if stat is not None:
            self._update_append_probe(stat.st_size)

        with self.lock:
            self._publish_cached_data()
            self._stats_events.clear()
            self._sync_stats_event_baseline()
            if stat is not None:
                self.last_mtime_ns = stat.st_mtime_ns
                self.last_size = stat.st_size
            return self.data.copy()

    def _consume_stream(self, file_obj, bootstrap_attach: bool = False, mark_appended: bool = False):
        # Binary I/O with manual offset tracking: TextIOWrapper.tell() is pathologically
        # slow in a readline loop (it re-snapshots the incremental decoder on every call),
        # which dominated startup. pos += len(raw) is free; we decode each line ourselves.
        pos = file_obj.tell()
        while True:
            raw = file_obj.readline()
            if not raw:
                with self.lock:
                    self.last_read_position = pos
                return

            # Do not commit an unfinished trailing line; re-read it after the next append.
            if not raw.endswith((b"\n", b"\r")):
                with self.lock:
                    self.last_read_position = pos
                return

            next_pos = pos + len(raw)
            line = raw.decode("utf-8", "replace")
            # Match text-mode universal newlines so downstream display is unchanged.
            if line.endswith("\r\n"):
                line = line[:-2] + "\n"
            elif line.endswith("\r"):
                line = line[:-1] + "\n"

            with self.lock:
                self._line_count += 1
                self._tail_lines.append((self._line_count, line))
                self._update_script_metadata(line, bootstrap_attach=bootstrap_attach)
                if self._run_open:
                    self._update_highest_pattern_scores(line)
                    self._update_script_state_snapshot(line)
                    self._highest_score = self._resolve_highest_score(
                        self._highest_pattern_scores
                    )
                self.last_read_position = next_pos
            pos = next_pos

    def _update_script_metadata(self, line: str, bootstrap_attach: bool = False):
        if self._is_script_close_line(line):
            self._close_script_run(boot_attached=bootstrap_attach)
            return

        if "Foldit:ScriptName" not in line:
            return

        script_name = self._extract_script_name(line)
        self._begin_script_run(script_name, boot_attached=bootstrap_attach)

    def _begin_script_run(self, script_name: str, boot_attached: bool = False):
        self._script_change_token += 1
        self._run_open = True
        self._tail_mode = "continue" if boot_attached else "new"
        self._highest_pattern_scores = self._new_pattern_score_state()
        self._highest_score = None
        self._script_state_snapshot = None
        self._script_name = script_name
        self._script_type, self._script_column_number = self._build_script_type(script_name)
        self._last_emitted_script_payload = None
        self._last_emitted_state_payload = None

    def _close_script_run(self, boot_attached: bool = False):
        if not self._run_open:
            return
        self._run_open = False
        self._tail_mode = "continue"
        if not boot_attached:
            self._enqueue_stats_event({"kind": "finish"})
        self._last_emitted_script_payload = None
        self._last_emitted_state_payload = None

    @staticmethod
    def _is_script_close_line(line: str) -> bool:
        return (
            re.search(r"<\s*/\s*Foldit:Script\s*>", line, flags=re.IGNORECASE)
            is not None
        )

    @staticmethod
    def _normalize_script_state_rule(raw_rule):
        if not isinstance(raw_rule, dict):
            return None

        detectors = []
        for raw_detector in raw_rule.get("detector", []):
            detector_text = str(raw_detector)
            if detector_text:
                detectors.append(detector_text)

        extractors = []
        for raw_extractor in raw_rule.get("extractors", []):
            if not isinstance(raw_extractor, dict):
                continue
            extractor_name = str(raw_extractor.get("name", "")).strip()
            if not extractor_name:
                continue
            extractors.append(
                {
                    "name": extractor_name,
                    "find_after": str(raw_extractor.get("find_after", "")),
                    "find_before": str(raw_extractor.get("find_before", "")),
                }
            )

        if not detectors or not extractors:
            return None

        stats_mapping = raw_rule.get("stats_mapping", {})
        if not isinstance(stats_mapping, dict):
            stats_mapping = {}

        default_script_field = extractors[0]["name"] if extractors else ""
        default_score_field = extractors[1]["name"] if len(extractors) > 1 else ""

        return {
            "name": str(raw_rule.get("name", "")).strip(),
            "detector": detectors,
            "extractors": extractors,
            "stats_script_field": str(
                stats_mapping.get("script", default_script_field)
            ).strip(),
            "stats_score_field": str(
                stats_mapping.get("score", default_score_field)
            ).strip(),
        }

    @classmethod
    def _normalize_script_state_rules(cls, raw_rules):
        if isinstance(raw_rules, dict):
            normalized_rule = cls._normalize_script_state_rule(raw_rules)
            return [normalized_rule] if normalized_rule else []

        if not isinstance(raw_rules, list):
            return []

        normalized_rules = []
        for raw_rule in raw_rules:
            normalized_rule = cls._normalize_script_state_rule(raw_rule)
            if normalized_rule:
                normalized_rules.append(normalized_rule)
        return normalized_rules

    @staticmethod
    def _extract_state_value(line: str, find_after: str, find_before: str) -> str:
        start = 0 if not find_after else line.find(find_after)
        if start == -1:
            return ""
        start += len(find_after)

        end = len(line) if not find_before else line.find(find_before, start)
        if end == -1:
            return ""
        return line[start:end].strip()

    def _update_script_state_snapshot(self, line: str):
        if not self._run_open:
            return

        for rule in self._current_state_rules:
            if not all(detector in line for detector in rule["detector"]):
                continue

            values = {}
            for extractor in rule["extractors"]:
                values[extractor["name"]] = self._extract_state_value(
                    line,
                    extractor["find_after"],
                    extractor["find_before"],
                )

            script_value = ""
            score_value = ""
            stats_script_field = rule.get("stats_script_field", "")
            stats_score_field = rule.get("stats_score_field", "")
            if stats_script_field:
                script_value = str(values.get(stats_script_field, "")).strip()
            if stats_score_field:
                score_value = str(values.get(stats_score_field, "")).strip()

            self._script_state_snapshot = {
                "rule": rule.get("name", ""),
                "script_name": self._script_name,
                "script_type": self._script_type,
                "line": line.strip(),
                "values": values,
                "script_value": script_value,
                "score_value": score_value,
            }
            return

    def _build_script_type(self, script_name: str) -> tuple[str, int]:
        script_name_lower = script_name.lower()
        script_column_number = 0
        script_type = self._build_fallback_script_type(
            script_name,
            self.settings.get("SCRIPT_TYPE_FALLBACK_MAX_LENGTH", 10),
        )
        self._current_state_rules = []

        for key, value in self.settings["SCRIPT_TYPE_MAPPING"].items():
            if str(key).lower() in script_name_lower:
                script_type = value["name"]
                script_column_number = value["column_number"]
                raw_state_rules = value.get("state_snapshot_rules")
                if raw_state_rules is None:
                    raw_state_rules = value.get("state_snapshot_rule")
                self._current_state_rules = self._normalize_script_state_rules(
                    raw_state_rules
                )
                break

        return script_type, script_column_number

    @staticmethod
    def _extract_script_name(line: str) -> str:
        """Extract raw script name from Foldit:ScriptName XML-like line."""
        line = line.strip()
        match = re.search(
            r"<\s*Foldit:ScriptName\s*>(.*?)<\s*/\s*Foldit:ScriptName\s*>",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()

        cleaned = line.replace("Foldit:ScriptName", "").strip()
        return re.sub(r"^[<>\s]+|[<>\s]+$", "", cleaned)

    @staticmethod
    def _build_fallback_script_type(script_name: str, max_display_len: int = 10) -> str:
        """Build display name when no mapping key matches."""
        if not script_name:
            return ""

        candidate = script_name.strip().strip('"').strip("'")
        is_path_like = (
            "\\" in candidate
            or "/" in candidate
            or re.match(r"^[A-Za-z]:", candidate) is not None
            or candidate.lower().endswith(".lua")
        )

        if is_path_like:
            parts = re.split(r"[\\/]+", candidate)
            candidate = parts[-1] if parts else candidate

        if candidate.lower().endswith(".lua"):
            candidate = candidate[:-4]

        try:
            max_display_len = max(1, int(max_display_len))
        except (TypeError, ValueError):
            max_display_len = 10
        return candidate[:max_display_len]

    def _new_pattern_score_state(self):
        return [None] * len(self.settings["SCORE_PATTERNS"])

    def _update_max_score_for_pattern(self, current_score: Optional[float], line: str, pattern) -> Optional[float]:
        exclusion_criteria = self.settings["EXCLUSION_CRITERIA"]
        if any(excl in line for excl in exclusion_criteria):
            return current_score

        matches = pattern.findall(line)
        for match in matches:
            match_start = line.find(match)
            if match_start > 0 and line[match_start - 1] == "-":
                continue

            score = float(match)
            if 1000 < score < 100000:
                if current_score is None or score > current_score:
                    current_score = score
        return current_score

    def _update_highest_pattern_scores(self, line: str):
        for idx, pattern in enumerate(self.settings["SCORE_PATTERNS"]):
            self._highest_pattern_scores[idx] = self._update_max_score_for_pattern(
                self._highest_pattern_scores[idx],
                line,
                pattern,
            )

    @staticmethod
    def _resolve_highest_score(pattern_high_scores) -> Optional[float]:
        for score in pattern_high_scores:
            if score is not None:
                return score
        return None

    def _get_last_log_lines(self):
        tooltip_lines = int(self.settings["tooltip_lines"])
        return list(self._tail_lines)[-tooltip_lines:]

    def get_data(self):
        with self.lock:
            return self.data.copy()

    def consume_stats_events(self) -> List[dict]:
        with self.lock:
            events = list(self._stats_events)
            self._stats_events.clear()
        return events
