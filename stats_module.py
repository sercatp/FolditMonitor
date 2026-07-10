import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from stats_domain import (
    FIN_FIXED_COLUMNS,
    FinalizationDomain,
    append_score_history_if_changed,
    append_text_history_if_changed,
    format_score,
    format_score_history,
    format_score_line,
    merge_score_line,
    normalize_history_value,
    normalize_score_value,
    normalize_state_value,
    normalize_target_value,
    parse_numeric_score,
    replace_latest_score_history_if_changed,
)


def natural_sort_key(value: str) -> List[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def resolve_settings_dict(settings_source: Any) -> Dict[str, Any]:
    if hasattr(settings_source, "settings"):
        settings = getattr(settings_source, "settings")
        if isinstance(settings, dict):
            return settings
    if isinstance(settings_source, dict):
        return settings_source
    return {}


@dataclass
class PuzzleData:
    client_entries: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    fin_rows: List[Dict[str, Any]] = field(default_factory=list)
    fin_columns: List[Dict[str, Any]] = field(default_factory=list)
    active_targets: Dict[str, str] = field(default_factory=dict)
    active_fin_columns: Dict[str, str] = field(default_factory=dict)
    loaded: bool = False
    dirty: bool = False
    last_save_ts: float = 0.0


@dataclass
class PuzzleLogInfo:
    puzzle_id: str
    last_modified: float = 0.0
    is_active: bool = False
    has_fin: bool = False


@dataclass
class StatsUiBridge:
    sync_from_ui: Callable[[str], None]
    push_to_ui: Callable[[str], None]


class StatsManager:
    FIN_META_MARKER = "__fin_meta__"
    MAIN_STATE_SCRIPT = "_state"

    def __init__(self, base_path: str, settings: Dict[str, Any]):
        self.base_path = base_path
        self.settings = settings

        logging_settings = settings.get("logging", {})
        self.logs_folder = os.path.join(base_path, logging_settings.get("logs_folder", "puzzle_logs"))
        os.makedirs(self.logs_folder, exist_ok=True)

        self.save_interval_minutes = int(logging_settings.get("stats_save_interval_minutes", 30))
        self.save_interval_seconds = max(60, self.save_interval_minutes * 60)
        self.score_decimals = int(logging_settings.get("stats_score_decimals", 0))

        self.puzzles: Dict[str, PuzzleData] = {}
        self.active_clients: Dict[str, str] = {}  # client_name -> puzzle_id
        self.client_runtime: Dict[str, Dict[str, Any]] = {}  # client_name -> runtime status
        self._last_autosave_check = 0.0
        self.table_domain = FinalizationDomain(settings, self.score_decimals)
        self._ui_bridge: Optional[StatsUiBridge] = None

    def set_score_decimals(self, decimals: int):
        self.score_decimals = max(0, int(decimals))
        self.table_domain.set_score_decimals(self.score_decimals)

    def get_csv_path(self, puzzle_id: str) -> str:
        return os.path.join(self.logs_folder, f"{puzzle_id}.csv")

    def get_fin_csv_path(self, puzzle_id: str) -> str:
        return os.path.join(self.logs_folder, f"{puzzle_id}_fin.csv")

    def get_notes_path(self, puzzle_id: str) -> str:
        return os.path.join(self.logs_folder, f"{puzzle_id}.txt")

    def has_notes_file(self, puzzle_id: str) -> bool:
        clean_puzzle_id = str(puzzle_id).strip()
        return bool(clean_puzzle_id) and os.path.exists(self.get_notes_path(clean_puzzle_id))

    def ensure_notes_file(self, puzzle_id: str) -> str:
        clean_puzzle_id = str(puzzle_id).strip()
        if not clean_puzzle_id:
            return ""

        file_path = self.get_notes_path(clean_puzzle_id)
        if not os.path.exists(file_path):
            with open(file_path, "a", encoding="utf-8"):
                pass
        return file_path

    def has_puzzle_log(self, puzzle_id: str) -> bool:
        clean_puzzle_id = str(puzzle_id).strip()
        if not clean_puzzle_id:
            return False
        if clean_puzzle_id in self.puzzles:
            return True
        return os.path.exists(self.get_csv_path(clean_puzzle_id)) or os.path.exists(
            self.get_fin_csv_path(clean_puzzle_id)
        )

    def get_logged_puzzles(self) -> List[PuzzleLogInfo]:
        infos: List[PuzzleLogInfo] = []
        active_puzzles = {str(puzzle_id).strip() for puzzle_id in self.active_clients.values() if str(puzzle_id).strip()}

        try:
            entries = list(os.scandir(self.logs_folder))
        except OSError:
            return infos

        for entry in entries:
            if not entry.is_file():
                continue

            file_name = entry.name
            file_name_lower = file_name.lower()
            if not file_name_lower.endswith(".csv") or file_name_lower.endswith("_fin.csv"):
                continue

            puzzle_id = file_name[:-4].strip()
            if not puzzle_id:
                continue

            try:
                last_modified = entry.stat().st_mtime
            except OSError:
                last_modified = 0.0

            infos.append(
                PuzzleLogInfo(
                    puzzle_id=puzzle_id,
                    last_modified=last_modified,
                    is_active=puzzle_id in active_puzzles,
                    has_fin=os.path.exists(self.get_fin_csv_path(puzzle_id)),
                )
            )

        return sorted(
            infos,
            key=lambda info: (
                0 if info.is_active else 1,
                -info.last_modified,
                natural_sort_key(info.puzzle_id),
            ),
        )

    def _get_or_create_puzzle(self, puzzle_id: str) -> PuzzleData:
        if puzzle_id not in self.puzzles:
            self.puzzles[puzzle_id] = PuzzleData()
        return self.puzzles[puzzle_id]

    @classmethod
    def is_state_script_name(cls, script_name: Any) -> bool:
        return str(script_name).strip().casefold() == cls.MAIN_STATE_SCRIPT.casefold()

    @classmethod
    def normalize_main_entry(cls, entry: Dict[str, Any]) -> Dict[str, Any]:
        script = str(entry.get("script", "")).strip()
        if cls.is_state_script_name(script):
            entry_copy = {
                "script": cls.MAIN_STATE_SCRIPT,
                "score": normalize_state_value(entry.get("score", "")),
            }
        else:
            entry_copy = {
                "script": script,
                "score": normalize_score_value(entry.get("score", "")),
            }
        if str(entry.get("kind", "")).strip():
            entry_copy["kind"] = str(entry.get("kind", "")).strip()
        return entry_copy

    def _ensure_puzzle_loaded(self, puzzle_id: str):
        puzzle = self._get_or_create_puzzle(puzzle_id)
        if puzzle.loaded:
            return

        file_path = self.get_csv_path(puzzle_id)
        if os.path.exists(file_path):
            self._load_puzzle_csv(puzzle_id, file_path)
            puzzle.last_save_ts = max(puzzle.last_save_ts, os.path.getmtime(file_path))

        fin_file_path = self.get_fin_csv_path(puzzle_id)
        if os.path.exists(fin_file_path):
            self._load_puzzle_fin_csv(puzzle_id, fin_file_path)
            puzzle.last_save_ts = max(puzzle.last_save_ts, os.path.getmtime(fin_file_path))

        puzzle.loaded = True

    def _load_puzzle_csv(self, puzzle_id: str, file_path: str):
        puzzle = self._get_or_create_puzzle(puzzle_id)
        puzzle.client_entries.clear()

        with open(file_path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        if not rows:
            return

        headers = rows[0]
        clients: List[str] = []
        for idx in range(0, len(headers), 2):
            client_name = headers[idx].strip() if idx < len(headers) else ""
            if not client_name:
                continue
            if client_name not in clients:
                clients.append(client_name)
                puzzle.client_entries.setdefault(client_name, [])

        for row in rows[1:]:
            for col_idx, client_name in enumerate(clients):
                script_idx = col_idx * 2
                score_idx = script_idx + 1
                script = row[script_idx].strip() if script_idx < len(row) else ""
                score_text = row[score_idx].strip() if score_idx < len(row) else ""
                entry = self.normalize_main_entry({"script": script, "score": score_text})
                if not entry["script"] and not str(entry["score"]).strip():
                    continue
                puzzle.client_entries[client_name].append(entry)

    @staticmethod
    def _normalize_fin_header(value: Any) -> str:
        normalized = str(value).strip().lower()
        if normalized == "from":
            return "start_from"
        if normalized == "state":
            return "state"
        if normalized == "score":
            return "start_score"
        return normalized

    def _get_fin_header_layout(self, header_row: List[str]) -> tuple[Dict[str, int], int]:
        fixed_indexes: Dict[str, int] = {}
        if header_row:
            fixed_indexes["client"] = 0

        known_fixed = set(FIN_FIXED_COLUMNS)
        dynamic_start = len(header_row)
        for idx in range(1, len(header_row)):
            header_name = self._normalize_fin_header(header_row[idx])
            if header_name in known_fixed:
                fixed_indexes[header_name] = idx
                continue
            dynamic_start = idx
            break

        return fixed_indexes, dynamic_start

    @staticmethod
    def _serialize_active_targets(active_targets: Dict[str, str]) -> str:
        normalized: Dict[str, str] = {}
        for client_name, target in active_targets.items():
            clean_name = str(client_name).strip()
            if clean_name:
                normalized[clean_name] = normalize_target_value(target)
        if not normalized:
            return ""
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _deserialize_active_targets(raw_value: Any) -> Dict[str, str]:
        text = str(raw_value).strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except Exception:
            return {}
        if not isinstance(loaded, dict):
            return {}

        normalized: Dict[str, str] = {}
        for client_name, target in loaded.items():
            clean_name = str(client_name).strip()
            if clean_name:
                normalized[clean_name] = normalize_target_value(target)
        return normalized

    def _load_puzzle_fin_csv(self, puzzle_id: str, file_path: str):
        puzzle = self._get_or_create_puzzle(puzzle_id)
        puzzle.fin_rows.clear()
        puzzle.fin_columns.clear()
        puzzle.active_targets.clear()
        puzzle.active_fin_columns.clear()

        with open(file_path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        if not rows:
            return

        if rows[0] and rows[0][0] == self.FIN_META_MARKER:
            meta_row = rows[0]
            if len(meta_row) > 1:
                puzzle.active_targets.update(self._deserialize_active_targets(meta_row[1]))
            header_row = rows[1] if len(rows) > 1 else []
            fixed_indexes, dynamic_start = self._get_fin_header_layout(header_row)
            meta_dynamic_start = dynamic_start
            while meta_dynamic_start < len(meta_row) and not str(meta_row[meta_dynamic_start]).strip():
                meta_dynamic_start += 1
            column_keys = meta_row[meta_dynamic_start:]
            labels = header_row[dynamic_start:] if len(header_row) > dynamic_start else []

            for idx, key in enumerate(column_keys):
                parsed = self.table_domain.parse_fin_column(key, labels[idx] if idx < len(labels) else "")
                if parsed is not None:
                    puzzle.fin_columns.append(parsed)

            self._read_fin_rows(puzzle, rows[2:], fixed_indexes, dynamic_start, column_keys)
        else:
            header_row = rows[0]
            fixed_indexes, dynamic_start = self._get_fin_header_layout(header_row)
            column_keys: List[str] = []
            for label in header_row[dynamic_start:]:
                clean_label = str(label).strip()
                if clean_label:
                    key = f"d:{clean_label.casefold()}"
                else:
                    key = f"blank:{len(column_keys) + 1}"
                column_keys.append(key)
                parsed = self.table_domain.parse_fin_column(key, clean_label)
                if parsed is not None:
                    puzzle.fin_columns.append(parsed)

            self._read_fin_rows(puzzle, rows[1:], fixed_indexes, dynamic_start, column_keys)

        self.table_domain.prune_fin_data(puzzle)

    def _read_fin_rows(
        self,
        puzzle: PuzzleData,
        data_rows: List[List[str]],
        fixed_indexes: Dict[str, int],
        dynamic_start: int,
        column_keys: List[str],
    ):
        client_idx = fixed_indexes.get("client", 0)
        state_idx = fixed_indexes.get("state")
        notes_idx = fixed_indexes.get("notes")
        start_from_idx = fixed_indexes.get("start_from")
        start_score_idx = fixed_indexes.get("start_score")

        for row in data_rows:
            client_name = row[client_idx].strip() if client_idx < len(row) else ""
            state = row[state_idx].strip() if state_idx is not None and state_idx < len(row) else ""
            notes = row[notes_idx].strip() if notes_idx is not None and notes_idx < len(row) else ""
            start_from = row[start_from_idx].strip() if start_from_idx is not None and start_from_idx < len(row) else ""
            start_score = normalize_score_value(row[start_score_idx] if start_score_idx is not None and start_score_idx < len(row) else "")
            cells: Dict[str, Any] = {}
            for idx, key in enumerate(column_keys):
                score_idx = idx + dynamic_start
                if score_idx >= len(row):
                    continue
                score_value = normalize_score_value(row[score_idx])
                if str(score_value).strip():
                    cells[key] = score_value
            fin_row = self.table_domain.new_fin_row(
                client_name,
                state=state,
                notes=notes,
                start_from=start_from,
                start_score=start_score,
            )
            fin_row["cells"] = cells
            puzzle.fin_rows.append(fin_row)

    @staticmethod
    def _is_copy_entry(entry: Dict[str, Any]) -> bool:
        if str(entry.get("kind", "")).strip().lower() == "copy":
            return True
        script_text = str(entry.get("script", "")).strip().lower()
        return script_text.startswith("from ")

    @staticmethod
    def _client_has_fin_rows(puzzle: PuzzleData, client_name: str) -> bool:
        clean_client_name = str(client_name).strip()
        if not clean_client_name:
            return False
        return any(
            str(row.get("client", "")).strip() == clean_client_name
            for row in puzzle.fin_rows
        )

    def _ensure_client_slot(self, puzzle: PuzzleData, client_name: str):
        clean_client_name = str(client_name).strip()
        if not clean_client_name:
            return

        puzzle.client_entries.setdefault(clean_client_name, [])
        if clean_client_name not in puzzle.active_targets:
            puzzle.active_targets[clean_client_name] = (
                "horizontal" if self._client_has_fin_rows(puzzle, clean_client_name) else "vertical"
            )

    def _append_vertical_copy_entry(self, puzzle: PuzzleData, target_name: str, source_name: str, source_score: Any):
        puzzle.client_entries.setdefault(target_name, []).append(
            {
                "script": f"from {source_name}",
                "score": normalize_score_value(source_score),
                "kind": "copy",
            }
        )

    def _build_state_text(self, script_name: str, score_value: Optional[float]) -> str:
        clean_script = normalize_state_value(script_name)
        formatted_score = format_score(score_value, self.score_decimals)
        if not clean_script:
            return formatted_score
        if not formatted_score:
            return clean_script
        return f"{clean_script} | {formatted_score}"

    def _set_fin_state(self, row: Dict[str, Any], script_name: str, score_value: Optional[float]) -> bool:
        state_text = self._build_state_text(script_name, score_value)
        if not state_text:
            return False

        row["state"], changed = append_text_history_if_changed(row.get("state", ""), state_text)
        return changed

    @classmethod
    def _is_state_entry(cls, entry: Dict[str, Any]) -> bool:
        script_text = str(entry.get("script", "")).strip()
        return cls.is_state_script_name(script_text)

    @staticmethod
    def _is_blank_main_entry(entry: Dict[str, Any]) -> bool:
        return not str(entry.get("script", "")).strip() and not str(entry.get("score", "")).strip()

    @staticmethod
    def _scripts_match(left: Any, right: Any) -> bool:
        return str(left).strip().casefold() == str(right).strip().casefold()

    def _find_tail_main_anchor_index(self, entries: List[Dict[str, Any]]) -> Optional[int]:
        if not entries:
            return None

        last_idx = len(entries) - 1
        # A trailing blank row is a deliberate tail boundary; blank spacer rows before it are ignored.
        if self._is_blank_main_entry(entries[last_idx]):
            return last_idx

        idx = last_idx
        while idx >= 0 and self._is_state_entry(entries[idx]):
            idx -= 1

        while idx >= 0 and self._is_blank_main_entry(entries[idx]):
            idx -= 1

        return idx if idx >= 0 else None

    @classmethod
    def _find_tail_main_state_index(cls, entries: List[Dict[str, Any]], anchor_idx: Optional[int]) -> Optional[int]:
        if anchor_idx is None:
            return None
        state_idx = anchor_idx + 1
        while state_idx < len(entries):
            if cls._is_blank_main_entry(entries[state_idx]):
                state_idx += 1
                continue
            if cls._is_state_entry(entries[state_idx]):
                return state_idx
            break
        return None

    def _get_or_create_live_fin_row(self, puzzle: PuzzleData, client_name: str) -> Dict[str, Any]:
        row_idx = self.table_domain.find_last_fin_row_index(puzzle, client_name)
        if row_idx is not None:
            return puzzle.fin_rows[row_idx]

        row = self.table_domain.new_fin_row(client_name)
        puzzle.fin_rows.append(row)
        return row

    def _write_fin_score_to_script_column(
        self,
        puzzle: PuzzleData,
        client_name: str,
        row: Dict[str, Any],
        script_name: Any,
        score_value: Optional[float],
    ) -> tuple[Dict[str, Any], bool]:
        if score_value is None:
            return row, False

        script_clean = str(script_name).strip() if script_name is not None else ""
        cells = row.setdefault("cells", {})
        previous_run_key = str(puzzle.active_fin_columns.get(client_name, "")).strip()
        column_key = self.table_domain.ensure_script_column(puzzle, row, script_clean)
        update_history = (
            replace_latest_score_history_if_changed
            if previous_run_key == column_key
            else append_score_history_if_changed
        )
        updated_value, changed = update_history(
            cells.get(column_key, ""),
            score_value,
            self.score_decimals,
        )
        if changed:
            cells[column_key] = updated_value
        puzzle.active_fin_columns[client_name] = column_key

        return row, changed

    def _update_tail_main_anchor(self, entries: List[Dict[str, Any]], anchor_idx: int, script_name: str, score_value: float) -> bool:
        script_clean = str(script_name).strip()
        if anchor_idx < 0 or anchor_idx >= len(entries) or not script_clean:
            return False

        changed = False
        script_entry = entries[anchor_idx]
        if str(script_entry.get("script", "")).strip() != script_clean:
            script_entry["script"] = script_clean
            changed = True
        # Keep where this script run started; the cell shows the end, tooltip the pair.
        new_score = merge_score_line(script_entry.get("score"), score_value, self.score_decimals)
        if format_score_line(script_entry.get("score"), self.score_decimals) != format_score_line(new_score, self.score_decimals):
            script_entry["score"] = new_score
            changed = True
        return changed

    def _update_tail_main_script(
        self,
        entries: List[Dict[str, Any]],
        script_name: str,
        score_value: Optional[float],
        continue_tail: bool,
    ) -> bool:
        script_clean = str(script_name).strip()
        if not script_clean or score_value is None:
            return False

        anchor_idx = self._find_tail_main_anchor_index(entries)
        if (
            continue_tail
            and anchor_idx is not None
            and self._scripts_match(entries[anchor_idx].get("script", ""), script_clean)
        ):
            return self._update_tail_main_anchor(entries, anchor_idx, script_clean, score_value)

        entries.append(self.normalize_main_entry({"script": script_clean, "score": score_value}))
        return True

    def _upsert_tail_main_state(self, entries: List[Dict[str, Any]], script_name: str, score_value: Optional[float]) -> bool:
        state_text = self._build_state_text(script_name, score_value)
        if not state_text:
            return False

        anchor_idx = self._find_tail_main_anchor_index(entries)
        if anchor_idx is None:
            return False

        anchor_entry = entries[anchor_idx]
        anchor_script = str(anchor_entry.get("script", "")).strip()
        if not self._is_copy_entry(anchor_entry) and not anchor_script:
            return False

        state_idx = self._find_tail_main_state_index(entries, anchor_idx)
        if state_idx is None:
            entries.insert(anchor_idx + 1, {"script": self.MAIN_STATE_SCRIPT, "score": state_text})
            return True

        if normalize_state_value(entries[state_idx].get("score", "")) == state_text:
            return False
        entries[state_idx]["score"] = state_text
        return True

    def register_ui_bridge(self, bridge: StatsUiBridge):
        self._ui_bridge = bridge

    def unregister_ui_bridge(self, bridge: Optional[StatsUiBridge] = None):
        if bridge is None or self._ui_bridge is bridge:
            self._ui_bridge = None

    def _sync_from_open_window(self, puzzle_id: str):
        bridge = self._ui_bridge
        if bridge is None:
            return

        try:
            bridge.sync_from_ui(str(puzzle_id).strip())
        except Exception:
            pass

    def _push_update_to_open_window(self, puzzle_id: str):
        bridge = self._ui_bridge
        if bridge is None:
            return

        try:
            bridge.push_to_ui(str(puzzle_id).strip())
        except Exception:
            pass

    def touch_client(self, client_name: str, puzzle_id: str):
        if not client_name or not puzzle_id:
            return

        client_name = str(client_name).strip()
        puzzle_id = str(puzzle_id).strip()
        if not client_name or not puzzle_id:
            return

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        self._ensure_client_slot(puzzle, client_name)
        self.active_clients[client_name] = puzzle_id

        file_path = self.get_csv_path(puzzle_id)
        if not os.path.exists(file_path):
            self.save_puzzle(puzzle_id, force=True)

    def sync_active_clients(self, active_client_names: set):
        active = {str(name).strip() for name in active_client_names if str(name).strip()}
        self.active_clients = {
            client: puzzle_id
            for client, puzzle_id in self.active_clients.items()
            if client in active
        }

    @staticmethod
    def _runtime_ui_state(runtime: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "puzzle_id": str(runtime.get("puzzle_id", "")).strip(),
            "cpu_percent": float(runtime.get("cpu_percent", 0.0) or 0.0),
            "is_idle": bool(runtime.get("is_idle", False)),
        }

    def sync_client_runtime(self, runtime_by_client: Dict[str, Dict[str, Any]]):
        normalized: Dict[str, Dict[str, Any]] = {}
        changed_puzzles = set()

        for client_name, runtime in runtime_by_client.items():
            clean_client = str(client_name).strip()
            clean_puzzle = str(runtime.get("puzzle_id", "")).strip()
            if not clean_client or not clean_puzzle:
                continue

            try:
                cpu_percent = float(runtime.get("cpu_percent", 0.0) or 0.0)
            except (TypeError, ValueError):
                cpu_percent = 0.0
            try:
                score_stale_ticks = max(0, int(runtime.get("score_stale_ticks", 0) or 0))
            except (TypeError, ValueError):
                score_stale_ticks = 0

            normalized_runtime = {
                "puzzle_id": clean_puzzle,
                "cpu_percent": cpu_percent,
                "is_idle": bool(runtime.get("is_idle", False)),
                "score_stale_ticks": score_stale_ticks,
            }
            normalized[clean_client] = normalized_runtime
            if self._runtime_ui_state(self.client_runtime.get(clean_client, {})) != self._runtime_ui_state(normalized_runtime):
                changed_puzzles.add(clean_puzzle)

        for client_name, runtime in self.client_runtime.items():
            if client_name in normalized:
                continue
            clean_puzzle = str(runtime.get("puzzle_id", "")).strip()
            if clean_puzzle:
                changed_puzzles.add(clean_puzzle)

        self.client_runtime = normalized

        for puzzle_id in sorted(changed_puzzles, key=natural_sort_key):
            self._push_update_to_open_window(puzzle_id)

    def reload_puzzle(self, puzzle_id: str):
        clean_puzzle_id = str(puzzle_id).strip()
        if not clean_puzzle_id:
            return

        puzzle = self._get_or_create_puzzle(clean_puzzle_id)
        puzzle.client_entries.clear()
        puzzle.fin_rows.clear()
        puzzle.fin_columns.clear()
        puzzle.active_targets.clear()
        puzzle.active_fin_columns.clear()
        puzzle.loaded = False
        puzzle.dirty = False
        puzzle.last_save_ts = 0.0

        self._ensure_puzzle_loaded(clean_puzzle_id)
        puzzle = self._get_or_create_puzzle(clean_puzzle_id)
        for client_name, active_puzzle in self.active_clients.items():
            if active_puzzle == clean_puzzle_id:
                self._ensure_client_slot(puzzle, client_name)
        puzzle.dirty = False

    def get_active_puzzles(self) -> List[str]:
        puzzles = {puzzle_id for puzzle_id in self.active_clients.values() if puzzle_id}
        return sorted(puzzles, key=natural_sort_key)

    def get_client_order(self, puzzle_id: str) -> List[str]:
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return []

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)

        clients = set(puzzle.client_entries.keys())
        clients.update(
            str(row.get("client", "")).strip()
            for row in puzzle.fin_rows
            if str(row.get("client", "")).strip()
        )
        clients.update(
            client_name
            for client_name, active_puzzle in self.active_clients.items()
            if active_puzzle == puzzle_id
        )
        return sorted(clients, key=natural_sort_key)

    def get_entries_by_client(
        self,
        puzzle_id: str,
        clients: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return {}

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        if clients is None:
            clients = self.get_client_order(puzzle_id)

        copied: Dict[str, List[Dict[str, Any]]] = {}
        for client_name in clients:
            copied[client_name] = [self.normalize_main_entry(entry) for entry in puzzle.client_entries.get(client_name, [])]
        return copied

    def get_fin_rows(self, puzzle_id: str) -> List[Dict[str, Any]]:
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return []

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        return self.table_domain.copy_fin_rows(puzzle.fin_rows)

    def get_fin_columns(self, puzzle_id: str) -> List[Dict[str, Any]]:
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return []

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        return self.table_domain.copy_fin_columns(puzzle.fin_columns)

    def get_active_targets(self, puzzle_id: str) -> Dict[str, str]:
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return {}

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        return dict(puzzle.active_targets)

    def get_client_runtime(self, puzzle_id: str) -> Dict[str, Dict[str, Any]]:
        clean_puzzle_id = str(puzzle_id).strip()
        if not clean_puzzle_id:
            return {}

        runtime_by_client: Dict[str, Dict[str, Any]] = {}
        for client_name, runtime in self.client_runtime.items():
            runtime_puzzle = str(runtime.get("puzzle_id", "")).strip()
            active_puzzle = str(self.active_clients.get(client_name, "")).strip()
            if runtime_puzzle != clean_puzzle_id or active_puzzle != clean_puzzle_id:
                continue
            runtime_by_client[client_name] = {
                "cpu_percent": float(runtime.get("cpu_percent", 0.0) or 0.0),
                "is_idle": bool(runtime.get("is_idle", False)),
                "score_stale_ticks": max(0, int(runtime.get("score_stale_ticks", 0) or 0)),
            }
        return runtime_by_client

    def set_puzzle_entries(self, puzzle_id: str, entries_by_client: Dict[str, List[Dict[str, Any]]]):
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)

        normalized: Dict[str, List[Dict[str, Any]]] = {}
        for client_name, entries in entries_by_client.items():
            clean_name = str(client_name).strip()
            if not clean_name:
                continue

            normalized_entries: List[Dict[str, Any]] = []
            for entry in entries:
                normalized_entries.append(self.normalize_main_entry(entry))
            normalized[clean_name] = normalized_entries

        for client_name, active_puzzle in self.active_clients.items():
            if active_puzzle == puzzle_id:
                normalized.setdefault(client_name, [])

        puzzle.client_entries = normalized
        puzzle.dirty = True

    def set_fin_state(
        self,
        puzzle_id: str,
        fin_rows: List[Dict[str, Any]],
        fin_columns: List[Dict[str, Any]],
        active_targets: Optional[Dict[str, str]] = None,
    ):
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        puzzle.fin_rows = self.table_domain.copy_fin_rows(fin_rows)
        puzzle.fin_columns = []
        for column in fin_columns:
            parsed = self.table_domain.parse_fin_column(column.get("key", ""), column.get("label", ""))
            if parsed is not None:
                puzzle.fin_columns.append(parsed)
        self.table_domain.prune_fin_data(puzzle)

        if active_targets is not None:
            for client_name, target in active_targets.items():
                clean_name = str(client_name).strip()
                if clean_name:
                    puzzle.active_targets[clean_name] = normalize_target_value(target)

        puzzle.dirty = True

    def set_client_target(self, puzzle_id: str, client_name: str, target: str) -> bool:
        """Move a client between the Main (vertical) and Finalization (horizontal)
        tables. This is the lightweight routing flip the stats window's To Main /
        To Finalization buttons perform: it only changes where the client's future
        scores are recorded (ensuring a Finalization row exists when switching to
        horizontal); existing entries/rows are left in place. Returns True when the
        target actually changed."""
        clean_puzzle_id = str(puzzle_id).strip()
        clean_client_name = str(client_name).strip()
        if not clean_puzzle_id or not clean_client_name:
            return False

        normalized_target = normalize_target_value(target)
        self._ensure_puzzle_loaded(clean_puzzle_id)
        self._sync_from_open_window(clean_puzzle_id)
        puzzle = self._get_or_create_puzzle(clean_puzzle_id)
        self._ensure_client_slot(puzzle, clean_client_name)

        if puzzle.active_targets.get(clean_client_name, "vertical") == normalized_target:
            return False

        if normalized_target == "horizontal":
            self._get_or_create_live_fin_row(puzzle, clean_client_name)

        puzzle.active_targets[clean_client_name] = normalized_target
        self.table_domain.prune_fin_data(puzzle, recompute_gaps=True)
        puzzle.dirty = True
        self._push_update_to_open_window(clean_puzzle_id)
        return True

    def handle_monitor_update(
        self,
        client_name: str,
        puzzle_id: str,
        script_name: Any,
        score: Any,
        continue_tail: bool = True,
    ):
        if not client_name or not puzzle_id:
            return

        clean_client_name = str(client_name).strip()
        clean_puzzle_id = str(puzzle_id).strip()
        is_new_session = self.active_clients.get(clean_client_name) != clean_puzzle_id

        self._ensure_puzzle_loaded(clean_puzzle_id)
        self._sync_from_open_window(clean_puzzle_id)
        puzzle = self._get_or_create_puzzle(clean_puzzle_id)
        has_saved_target = clean_client_name in puzzle.active_targets
        self.touch_client(clean_client_name, clean_puzzle_id)
        score_value = parse_numeric_score(score)
        if score_value is None:
            return

        puzzle = self._get_or_create_puzzle(clean_puzzle_id)
        entries = puzzle.client_entries.setdefault(clean_client_name, [])
        script_clean = str(script_name).strip() if script_name is not None else ""
        if is_new_session and not has_saved_target:
            puzzle.active_targets[clean_client_name] = self.table_domain.resolve_startup_target(
                puzzle,
                clean_client_name,
                script_clean,
                score_value,
            )

        target_mode = puzzle.active_targets.get(clean_client_name, "vertical")

        if target_mode == "horizontal":
            row = self._get_or_create_live_fin_row(
                puzzle,
                clean_client_name,
            )
            _, changed = self._write_fin_score_to_script_column(
                puzzle,
                clean_client_name,
                row,
                script_clean,
                score_value,
            )

            if changed:
                self.table_domain.prune_fin_data(puzzle, recompute_gaps=True)
                puzzle.dirty = True
                self._push_update_to_open_window(clean_puzzle_id)
            return

        changed = self._update_tail_main_script(
            entries,
            script_clean,
            score_value,
            continue_tail=bool(continue_tail),
        )
        if changed:
            puzzle.dirty = True
            self._push_update_to_open_window(clean_puzzle_id)

    def handle_script_state_snapshot(
        self,
        client_name: str,
        puzzle_id: str,
        script_name: Any,
        score: Any,
    ):
        clean_client_name = str(client_name).strip()
        clean_puzzle_id = str(puzzle_id).strip()
        script_clean = str(script_name).strip() if script_name is not None else ""
        score_value = parse_numeric_score(score)
        if not clean_client_name or not clean_puzzle_id or not script_clean:
            return

        self._ensure_puzzle_loaded(clean_puzzle_id)
        self._sync_from_open_window(clean_puzzle_id)
        puzzle = self._get_or_create_puzzle(clean_puzzle_id)
        self._ensure_client_slot(puzzle, clean_client_name)

        if puzzle.active_targets.get(clean_client_name, "vertical") == "horizontal":
            # In the fin table the `state` cell is copy-provenance: it records which
            # source snapshot was copied in (set by handle_copy_saves_event) and stays
            # frozen. A running script only updates its score columns (via
            # handle_monitor_update); its live per-event state snapshots are ignored.
            return

        entries = puzzle.client_entries.setdefault(clean_client_name, [])
        changed = self._upsert_tail_main_state(
            entries,
            script_clean,
            score_value,
        )
        if not changed:
            return

        puzzle.dirty = True
        self._push_update_to_open_window(clean_puzzle_id)

    def handle_copy_saves_event(
        self,
        source_client: str,
        target_client: str,
        puzzle_id: str,
        source_score: Any = None,
        source_script_type: Any = None,
        source_state_script: Any = None,
        source_state_score: Any = None,
    ):
        source_name = str(source_client).strip()
        target_name = str(target_client).strip()
        puzzle_id = str(puzzle_id).strip()
        if not source_name or not target_name or not puzzle_id:
            return

        source_script_clean = str(source_script_type).strip() if source_script_type is not None else ""
        source_score_value = parse_numeric_score(source_score)
        state_script = str(source_state_script).strip() if source_state_script is not None else ""
        state_score_value = parse_numeric_score(source_state_score)
        self._ensure_puzzle_loaded(puzzle_id)
        self._sync_from_open_window(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        self._ensure_client_slot(puzzle, source_name)
        self._ensure_client_slot(puzzle, target_name)
        if puzzle.active_targets.get(target_name, "vertical") == "horizontal":
            use_source_script_column = (
                puzzle.active_targets.get(source_name, "vertical") == "horizontal"
                and bool(source_script_clean)
            )
            row = self.table_domain.new_fin_row(
                target_name,
                start_from=source_name,
                start_score="" if use_source_script_column else source_score,
            )
            if use_source_script_column:
                row, _ = self._write_fin_score_to_script_column(
                    puzzle,
                    target_name,
                    row,
                    source_script_clean,
                    source_score_value,
                )
            self._set_fin_state(row, state_script, state_score_value)
            puzzle.fin_rows.append(row)
            self.table_domain.prune_fin_data(puzzle)
            puzzle.dirty = True
            self._push_update_to_open_window(puzzle_id)
            return

        self._append_vertical_copy_entry(puzzle, target_name, source_name, source_score)
        if state_script:
            self._upsert_tail_main_state(
                puzzle.client_entries.setdefault(target_name, []),
                state_script,
                state_score_value,
            )
        puzzle.dirty = True
        self._push_update_to_open_window(puzzle_id)

    def _build_rows_for_save(self, puzzle_id: str, clients: List[str]) -> List[List[str]]:
        puzzle = self._get_or_create_puzzle(puzzle_id)
        max_rows = 0
        for client_name in clients:
            max_rows = max(max_rows, len(puzzle.client_entries.get(client_name, [])))

        rows: List[List[str]] = []
        for row_idx in range(max_rows):
            row_values: List[str] = []
            for client_name in clients:
                entries = puzzle.client_entries.get(client_name, [])
                if row_idx < len(entries):
                    entry = self.normalize_main_entry(entries[row_idx])
                    row_values.extend(
                        [
                            str(entry.get("script", "")).strip(),
                            format_score_line(entry.get("score", ""), self.score_decimals),
                        ]
                    )
                else:
                    row_values.extend(["", ""])
            rows.append(row_values)
        return rows

    def _save_fin_csv(self, puzzle_id: str):
        puzzle = self._get_or_create_puzzle(puzzle_id)
        self.table_domain.prune_fin_data(puzzle)
        fin_file_path = self.get_fin_csv_path(puzzle_id)

        with open(fin_file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    self.FIN_META_MARKER,
                    self._serialize_active_targets(puzzle.active_targets),
                    "",
                    "",
                    "",
                    *[column["key"] for column in puzzle.fin_columns],
                ]
            )
            writer.writerow(
                [
                    *FIN_FIXED_COLUMNS.values(),
                    *[column.get("label", "") for column in puzzle.fin_columns],
                ]
            )
            for row in puzzle.fin_rows:
                cells = row.get("cells", {})
                row_values = [
                    str(row.get("client", "")).strip(),
                    str(row.get("start_from", "")).strip(),
                    normalize_history_value(row.get("state", "")),
                    str(row.get("notes", "")).strip(),
                    format_score(row.get("start_score", ""), self.score_decimals),
                ]
                for column in puzzle.fin_columns:
                    row_values.append(format_score_history(cells.get(column["key"], ""), self.score_decimals))
                writer.writerow(row_values)

    def save_puzzle(self, puzzle_id: str, force: bool = False):
        puzzle_id = str(puzzle_id).strip()
        if not puzzle_id:
            return

        self._ensure_puzzle_loaded(puzzle_id)
        puzzle = self._get_or_create_puzzle(puzzle_id)
        if not force and not puzzle.dirty:
            return

        clients = self.get_client_order(puzzle_id)
        file_path = self.get_csv_path(puzzle_id)

        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            if clients:
                header: List[str] = []
                for client_name in clients:
                    header.extend([client_name, "score"])
                writer.writerow(header)
                for row in self._build_rows_for_save(puzzle_id, clients):
                    writer.writerow(row)

        self._save_fin_csv(puzzle_id)

        puzzle.dirty = False
        puzzle.last_save_ts = time.time()

    def maybe_autosave(self):
        now = time.time()
        if now - self._last_autosave_check < 30:
            return
        self._last_autosave_check = now

        for puzzle_id, puzzle in self.puzzles.items():
            if not puzzle.dirty:
                continue
            if now - puzzle.last_save_ts >= self.save_interval_seconds:
                self.save_puzzle(puzzle_id, force=True)

    def flush_all(self):
        for puzzle_id in list(self.puzzles.keys()):
            self.save_puzzle(puzzle_id, force=True)
