import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from stats_domain import (
    FIN_FIXED_COLUMNS,
    append_score_history_if_changed,
    append_text_history_if_changed,
    decode_history_edit_value,
    format_score,
    format_score_latest,
    format_score_line,
    latest_history_value,
    normalize_score_history_edit_value,
    normalize_score_value,
    normalize_state_value,
    replace_latest_score_history_if_changed,
)
from stats_module import PuzzleData, StatsManager, natural_sort_key


class StatsEditorError(Exception):
    pass


@dataclass(frozen=True)
class StatsViewLayout:
    clients: List[str]
    vertical_column_specs: List[Dict[str, Any]]
    vertical_column_specs_by_name: Dict[str, Dict[str, Any]]
    vertical_client_columns: Dict[str, Dict[str, str]]
    fin_column_specs: List[Dict[str, Any]]
    fin_column_specs_by_name: Dict[str, Dict[str, Any]]
    layout_signature: tuple[Any, ...]

    def row_count(self, working_entries: Dict[str, List[Dict[str, Any]]]) -> int:
        if not self.clients:
            return 1
        max_rows = 0
        for client_name in self.clients:
            max_rows = max(max_rows, len(working_entries.get(client_name, [])))
        return max(1, max_rows)

    def fin_column_label(self, key: Optional[str]) -> str:
        if not key:
            return ""
        clean_key = str(key)
        spec = self.fin_column_specs_by_name.get(clean_key)
        if spec is None:
            return clean_key
        fixed_label = FIN_FIXED_COLUMNS.get(clean_key)
        if fixed_label is not None:
            return fixed_label
        return str(spec.get("label", "")).strip() or clean_key


def display_fin_client_name(client_name: str) -> str:
    clean_name = str(client_name).strip()
    if not clean_name:
        return ""
    return re.sub(r"foldit", "f", clean_name, flags=re.IGNORECASE)


def selected_main_range(
    selected_client: Optional[str],
    selected_row_index: Optional[int],
    selected_row_end_index: Optional[int],
) -> Optional[tuple[int, int]]:
    if selected_client is None or selected_row_index is None:
        return None
    end_idx = selected_row_end_index if selected_row_end_index is not None else selected_row_index
    return min(selected_row_index, end_idx), max(selected_row_index, end_idx)


def main_selection_text(
    selected_client: Optional[str],
    selected_row_index: Optional[int],
    selected_row_end_index: Optional[int],
    selected_vertical_field: Optional[str],
) -> str:
    selected_range = selected_main_range(selected_client, selected_row_index, selected_row_end_index)
    if selected_client is None or selected_range is None:
        return "Main: none"
    start_idx, end_idx = selected_range
    if start_idx == end_idx:
        return f"Main: {selected_client} row {start_idx + 1} {selected_vertical_field or 'script'}"
    return f"Main: {selected_client} rows {start_idx + 1}-{end_idx + 1}"


def fin_selection_text(
    fin_rows: List[Dict[str, Any]],
    selected_fin_row_index: Optional[int],
    selected_fin_column_key: Optional[str],
    layout: Optional[StatsViewLayout],
) -> str:
    if selected_fin_row_index is None or selected_fin_row_index >= len(fin_rows):
        return "Finalization: none"
    row = fin_rows[selected_fin_row_index]
    label = layout.fin_column_label(selected_fin_column_key) if layout is not None else str(selected_fin_column_key or "")
    return f"Finalization: row {selected_fin_row_index + 1} {row.get('client', '')} {label}"


def normalize_clipboard_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def parse_main_clipboard_rows(value: Any) -> Optional[List[Dict[str, str]]]:
    text = normalize_clipboard_text(value)
    if "\n" not in text and "\t" not in text:
        return None

    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()

    rows: List[Dict[str, str]] = []
    for line in lines:
        parts = line.split("\t")
        script = parts[0] if parts else ""
        score = parts[1] if len(parts) > 1 else ""
        rows.append({"script": script, "score": score})
    return rows


def first_clipboard_cell_text(value: Any) -> str:
    text = normalize_clipboard_text(value)
    first_line = text.split("\n", 1)[0]
    return first_line.split("\t", 1)[0]


class StatsEditorSession:
    def __init__(self, manager: StatsManager, puzzle_id: str):
        self.manager = manager
        self.puzzle_id = str(puzzle_id).strip()

        self.working_entries: Dict[str, List[Dict[str, Any]]] = {}
        self.fin_rows: List[Dict[str, Any]] = []
        self.fin_columns: List[Dict[str, Any]] = []
        self.active_targets: Dict[str, str] = {}
        self.client_runtime: Dict[str, Dict[str, Any]] = {}
        self.ui_dirty = False

        self.reload_from_manager(preserve_dirty=False)

    def open_puzzle(self, puzzle_id: str):
        self.puzzle_id = str(puzzle_id).strip()
        self.reload_from_manager(preserve_dirty=False)

    def reload_from_manager(self, preserve_dirty: bool = True):
        was_dirty = self.ui_dirty
        all_clients = self.manager.get_client_order(self.puzzle_id)
        self.working_entries = self.manager.get_entries_by_client(self.puzzle_id, all_clients)
        self.fin_rows = self.manager.get_fin_rows(self.puzzle_id)
        self.fin_columns = self.manager.get_fin_columns(self.puzzle_id)
        self.active_targets = self.manager.get_active_targets(self.puzzle_id)
        self.client_runtime = self.manager.get_client_runtime(self.puzzle_id)
        self._ensure_client_state(extra_clients=all_clients)
        self.ui_dirty = was_dirty if preserve_dirty else False

    def discard_unsaved_changes(self):
        self.reload_from_manager(preserve_dirty=False)

    def sync_to_manager(self):
        self.manager.set_puzzle_entries(self.puzzle_id, self.working_entries)
        self.manager.set_fin_state(self.puzzle_id, self.fin_rows, self.fin_columns, self.active_targets)

    def save(self, decimals: int):
        self.manager.set_score_decimals(decimals)
        self.sync_to_manager()
        self.manager.save_puzzle(self.puzzle_id, force=True)
        self.reload_from_manager(preserve_dirty=False)

    def client_is_idle(self, client_name: str) -> bool:
        runtime = self.client_runtime.get(str(client_name).strip())
        return bool(runtime and runtime.get("is_idle", False))

    def is_main_target(self, client_name: str) -> bool:
        return self.active_targets.get(str(client_name).strip(), "vertical") == "vertical"

    def is_active_fin_row(self, row_idx: int) -> bool:
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            return False
        client_name = str(self.fin_rows[row_idx].get("client", "")).strip()
        if not client_name or self.active_targets.get(client_name, "vertical") != "horizontal":
            return False
        return self.last_fin_row_index(client_name) == row_idx

    def update_main_cell(self, client_name: str, row_idx: int, field_name: str, new_text: str) -> Optional[int]:
        clean_client = str(client_name).strip()
        if not clean_client:
            raise StatsEditorError("Select a main client cell first.")

        entries = self.working_entries.setdefault(clean_client, [])
        editing_existing_row = row_idx < len(entries)
        target_idx = min(row_idx, len(entries)) if editing_existing_row else len(entries)
        if target_idx == len(entries):
            entries.append({"script": "", "score": ""})
        if field_name == "script":
            entries[target_idx]["script"] = new_text
        else:
            entries[target_idx]["score"] = new_text
        entries[target_idx] = StatsManager.normalize_main_entry(entries[target_idx])
        self._finish_mutation()
        if not entries:
            return None
        return min(target_idx, len(entries) - 1)

    def update_fin_cell(self, row_idx: int, column_name: str, new_text: str):
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            raise StatsEditorError("Selected Finalization row does not exist.")

        row = self.fin_rows[row_idx]
        if column_name == "client":
            raise StatsEditorError("Client cell cannot be edited.")
        if column_name == "state":
            row["state"] = decode_history_edit_value(new_text)
        elif column_name == "notes":
            row["notes"] = new_text
        elif column_name == "start_from":
            row["start_from"] = new_text
        elif column_name == "start_score":
            row["start_score"] = normalize_score_value(new_text)
        else:
            score_value = normalize_score_history_edit_value(new_text, self.manager.score_decimals)
            if str(score_value).strip():
                row.setdefault("cells", {})[column_name] = score_value
            else:
                row.setdefault("cells", {}).pop(column_name, None)
        self._finish_mutation(normalize_fin=True)

    def add_vertical_row(self, client_name: str, end_idx: int) -> int:
        clean_client = str(client_name).strip()
        entries = self.working_entries.setdefault(clean_client, [])
        insert_idx = min(end_idx + 1, len(entries))
        entries.insert(insert_idx, {"script": "", "score": ""})
        self._finish_mutation()
        return insert_idx

    def paste_vertical_rows(self, client_name: str, start_idx: int, rows: List[Dict[str, Any]]) -> tuple[int, int]:
        clean_client = str(client_name).strip()
        if not clean_client:
            raise StatsEditorError("Select a main client cell first.")

        entries = self.working_entries.setdefault(clean_client, [])
        insert_idx = max(0, int(start_idx))
        while len(entries) < insert_idx:
            entries.append({"script": "", "score": ""})

        for offset, raw_row in enumerate(rows):
            target_idx = insert_idx + offset
            normalized_row = StatsManager.normalize_main_entry(raw_row)
            if target_idx < len(entries):
                entries[target_idx] = normalized_row
            else:
                entries.append(normalized_row)

        self._finish_mutation()
        return insert_idx, insert_idx + max(0, len(rows) - 1)

    def delete_vertical_rows(self, client_name: str, start_idx: int, end_idx: int) -> Optional[int]:
        clean_client = str(client_name).strip()
        entries = self.working_entries.setdefault(clean_client, [])
        del entries[start_idx : end_idx + 1]
        self._finish_mutation()
        if not entries:
            return None
        return min(start_idx, len(entries) - 1)

    def move_vertical_to_fin(self, client_name: str, start_idx: int, end_idx: int) -> Optional[int]:
        clean_client = str(client_name).strip()
        entries = self.working_entries.setdefault(clean_client, [])
        moved_entries = entries[start_idx : end_idx + 1]
        del entries[start_idx : end_idx + 1]

        current_fin_row: Optional[Dict[str, Any]] = None
        current_fin_active_key: Optional[str] = None

        for entry in moved_entries:
            script = str(entry.get("script", "")).strip()
            score = entry.get("score", "")
            if self.is_state_script(script):
                state_value = normalize_state_value(score)
                if not state_value:
                    continue

                if current_fin_row is not None:
                    current_fin_row["state"], _changed = append_text_history_if_changed(
                        current_fin_row.get("state", ""),
                        state_value,
                    )
            elif self.is_copy_script(script):
                source_name = script[5:].strip()
                score_value = normalize_score_value(score)

                if self._fin_row_has_payload(current_fin_row):
                    current_fin_row = None
                    current_fin_active_key = None

                if current_fin_row is None:
                    current_fin_row = self.manager.table_domain.new_fin_row(clean_client)
                    self.fin_rows.append(current_fin_row)
                    current_fin_active_key = None

                if str(current_fin_row.get("start_from", "")).strip() or str(current_fin_row.get("start_score", "")).strip():
                    current_fin_row = self.manager.table_domain.new_fin_row(clean_client)
                    self.fin_rows.append(current_fin_row)
                    current_fin_active_key = None

                current_fin_row["start_from"] = source_name
                current_fin_row["start_score"] = score_value
            else:
                score_value = normalize_score_value(score)
                if not script and not str(score_value).strip():
                    continue

                if current_fin_row is None:
                    current_fin_row = self.manager.table_domain.new_fin_row(clean_client)
                    self.fin_rows.append(current_fin_row)
                    current_fin_active_key = None

                key = self.ensure_working_fin_column(current_fin_row, script)
                cells = current_fin_row.setdefault("cells", {})
                update_history = (
                    replace_latest_score_history_if_changed
                    if key == current_fin_active_key
                    else append_score_history_if_changed
                )
                updated_value, _changed = update_history(
                    cells.get(key, ""),
                    score_value,
                    self.manager.score_decimals,
                )
                if str(updated_value).strip():
                    cells[key] = updated_value
                current_fin_active_key = key

        if current_fin_row is None:
            self.ensure_fin_target_row(clean_client)

        self.active_targets[clean_client] = "horizontal"
        self._finish_mutation(normalize_fin=True)
        if not entries:
            return None
        return min(start_idx, len(entries) - 1)

    def ensure_fin_target_row(self, client_name: str) -> int:
        clean_client = str(client_name).strip()
        if not clean_client:
            raise StatsEditorError("Select a main client cell first.")

        row_idx = self.last_fin_row_index(clean_client)
        if row_idx is not None:
            return row_idx

        self.fin_rows.append(self.manager.table_domain.new_fin_row(clean_client))
        return len(self.fin_rows) - 1

    def activate_fin_target(self, client_name: str) -> int:
        clean_client = str(client_name).strip()
        row_idx = self.ensure_fin_target_row(clean_client)
        self.active_targets[clean_client] = "horizontal"
        self._finish_mutation(normalize_fin=True)
        resolved_idx = self.last_fin_row_index(clean_client)
        return row_idx if resolved_idx is None else resolved_idx

    def activate_main_target(self, client_name: str):
        clean_client = str(client_name).strip()
        if not clean_client:
            raise StatsEditorError("Select a Finalization row first.")
        self.active_targets[clean_client] = "vertical"
        self._finish_mutation(normalize_fin=True)

    def add_fin_row(self, client_name: str) -> int:
        clean_client = str(client_name).strip()
        if not clean_client:
            raise StatsEditorError("Select a client in Main or Finalization first.")

        self.fin_rows.append(self.manager.table_domain.new_fin_row(clean_client))
        self.active_targets[clean_client] = "horizontal"
        self._finish_mutation(normalize_fin=True)
        return len(self.fin_rows) - 1

    def move_fin_cell_to_vertical(self, row_idx: int, column_key: str):
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            raise StatsEditorError("Selected Finalization row does not exist.")

        row = self.fin_rows[row_idx]
        client_name = str(row.get("client", "")).strip()
        if column_key in ("client", "state", "notes", "start_from", "start_score"):
            fixed_value = row.get(column_key, "")
            if column_key == "client":
                raise StatsEditorError("Use 'Row To Main' for fixed Finalization columns or an entire row.")
            if not str(fixed_value).strip():
                self.activate_main_target(client_name)
                return
            raise StatsEditorError("Use 'Row To Main' for fixed Finalization columns or an entire row.")

        cells = row.setdefault("cells", {})
        if column_key not in cells or not str(cells.get(column_key, "")).strip():
            self.activate_main_target(client_name)
            return

        is_last_row = self.last_fin_row_index(client_name) == row_idx
        is_last_cell = self.last_fin_cell_key(row) == column_key
        score = cells.pop(column_key)
        self.append_vertical_entry(
            client_name,
            self.fin_script_from_column(column_key),
            format_score_line(latest_history_value(score), self.manager.score_decimals),
        )
        if is_last_row and is_last_cell:
            self.active_targets[client_name] = "vertical"
        self._finish_mutation(normalize_fin=True)

    def move_fin_row_to_vertical(self, row_idx: int):
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            raise StatsEditorError("Selected Finalization row does not exist.")

        row = self.fin_rows.pop(row_idx)
        client_name = str(row.get("client", "")).strip()
        if str(row.get("start_from", "")).strip():
            self.append_vertical_entry(client_name, f"from {row.get('start_from', '')}", row.get("start_score", ""))
        elif str(row.get("start_score", "")).strip():
            self.append_vertical_entry(client_name, "", row.get("start_score", ""))
        if str(row.get("state", "")).strip():
            self.append_vertical_entry(client_name, StatsManager.MAIN_STATE_SCRIPT, latest_history_value(row.get("state", "")))
        for column in self.fin_columns:
            key = str(column.get("key", ""))
            if key in row.get("cells", {}):
                self.append_vertical_entry(
                    client_name,
                    self.fin_script_from_column(key),
                    format_score_line(latest_history_value(row["cells"][key]), self.manager.score_decimals),
                )

        self.active_targets[client_name] = "vertical"
        self._finish_mutation(normalize_fin=True)

    def delete_fin_cell(self, row_idx: int, column_key: str):
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            raise StatsEditorError("Selected Finalization row does not exist.")
        if column_key == "client":
            raise StatsEditorError("Client cell cannot be cleared.")

        row = self.fin_rows[row_idx]
        if column_key == "state":
            row["state"] = ""
        elif column_key == "notes":
            row["notes"] = ""
        elif column_key == "start_from":
            row["start_from"] = ""
        elif column_key == "start_score":
            row["start_score"] = ""
        else:
            row.setdefault("cells", {}).pop(column_key, None)
        self._finish_mutation(normalize_fin=True)

    def delete_fin_row(self, row_idx: int):
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            raise StatsEditorError("Selected Finalization row does not exist.")

        del self.fin_rows[row_idx]
        self._finish_mutation(normalize_fin=True)

    def move_fin_row(self, row_idx: int, direction: int) -> int:
        target_idx = row_idx + direction
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            raise StatsEditorError("Selected Finalization row does not exist.")
        if target_idx < 0 or target_idx >= len(self.fin_rows):
            raise StatsEditorError("No adjacent Finalization row.")

        self.fin_rows[row_idx], self.fin_rows[target_idx] = (
            self.fin_rows[target_idx],
            self.fin_rows[row_idx],
        )
        self._finish_mutation()
        return target_idx

    def reorder_fin_columns(self, ordered_keys: List[str]) -> bool:
        """Reorder the dynamic Finalization columns to match `ordered_keys` (the dynamic
        column keys in their new left-to-right order). Keys are stable identities, so the
        cells follow automatically and only the display order changes; the gap recompute
        in normalize then refills empty gaps from whatever now sits to their right.
        Returns True when the order actually changed."""
        by_key = {str(column.get("key", "")).strip(): column for column in self.fin_columns}
        new_order = [by_key[key] for key in ordered_keys if key in by_key]
        listed = set(ordered_keys)
        for column in self.fin_columns:
            if str(column.get("key", "")).strip() not in listed:
                new_order.append(column)
        if [column.get("key") for column in new_order] == [column.get("key") for column in self.fin_columns]:
            return False
        self.fin_columns = new_order
        self._finish_mutation(normalize_fin=True)
        return True

    def normalize_working_fin(self):
        temp = PuzzleData(fin_rows=self.fin_rows, fin_columns=self.fin_columns)
        self.manager.table_domain.prune_fin_data(temp, recompute_gaps=True)
        self.fin_rows = self.manager.table_domain.copy_fin_rows(temp.fin_rows)
        self.fin_columns = self.manager.table_domain.copy_fin_columns(temp.fin_columns)

    def ensure_working_fin_column(self, row: Dict[str, Any], script_name: str) -> str:
        temp = PuzzleData(fin_rows=self.fin_rows, fin_columns=self.fin_columns)
        key = self.manager.table_domain.ensure_script_column(temp, row, script_name)
        self.fin_columns = self.manager.table_domain.copy_fin_columns(temp.fin_columns)
        return key

    def append_vertical_entry(self, client_name: str, script: str, score: Any):
        self.working_entries.setdefault(client_name, []).append(
            StatsManager.normalize_main_entry(
                {
                    "script": str(script).strip(),
                    "score": score,
                }
            )
        )

    @staticmethod
    def is_copy_script(script_text: str) -> bool:
        return str(script_text).strip().lower().startswith("from ")

    @staticmethod
    def is_state_script(script_text: str) -> bool:
        return StatsManager.is_state_script_name(script_text)

    def last_fin_row_index(self, client_name: str) -> Optional[int]:
        for idx in range(len(self.fin_rows) - 1, -1, -1):
            if str(self.fin_rows[idx].get("client", "")).strip() == client_name:
                return idx
        return None

    def last_fin_cell_key(self, row: Dict[str, Any]) -> Optional[str]:
        temp = PuzzleData(fin_rows=self.fin_rows, fin_columns=self.fin_columns)
        return self.manager.table_domain.find_last_fin_cell_key(temp, row)

    def fin_script_from_column(self, key: str) -> str:
        for column in self.fin_columns:
            if column.get("key") == key:
                return str(column.get("label", "")).strip()
        return ""

    def build_view_layout(self) -> StatsViewLayout:
        clients = self._visible_clients()

        vertical_column_specs: List[Dict[str, Any]] = []
        vertical_column_specs_by_name: Dict[str, Dict[str, Any]] = {}
        vertical_client_columns: Dict[str, Dict[str, str]] = {}
        for idx, client_name in enumerate(clients):
            script_spec = {"name": f"script_{idx}", "type": "script", "client": client_name}
            score_spec = {"name": f"score_{idx}", "type": "score", "client": client_name}
            vertical_column_specs.append(script_spec)
            vertical_column_specs.append(score_spec)
            vertical_column_specs_by_name[script_spec["name"]] = script_spec
            vertical_column_specs_by_name[score_spec["name"]] = score_spec
            vertical_client_columns[client_name] = {
                "script": script_spec["name"],
                "score": score_spec["name"],
            }

        fin_column_specs = [
            {"name": "client", "type": "client"},
            {"name": "start_from", "type": "start_from"},
            {"name": "state", "type": "state"},
            {"name": "notes", "type": "notes"},
            {"name": "start_score", "type": "start_score"},
        ]
        for column in self.fin_columns:
            fin_column_specs.append(
                {
                    "name": str(column.get("key", "")),
                    "type": "score",
                    "label": str(column.get("label", "")).strip(),
                }
            )
        fin_column_specs_by_name = {spec["name"]: spec for spec in fin_column_specs}
        layout_signature = (
            tuple((spec["name"], spec.get("type", ""), spec.get("client", "")) for spec in vertical_column_specs),
            tuple((spec["name"], spec.get("label", "")) for spec in fin_column_specs),
            bool(self.fin_rows),
        )
        return StatsViewLayout(
            clients=clients,
            vertical_column_specs=vertical_column_specs,
            vertical_column_specs_by_name=vertical_column_specs_by_name,
            vertical_client_columns=vertical_client_columns,
            fin_column_specs=fin_column_specs,
            fin_column_specs_by_name=fin_column_specs_by_name,
            layout_signature=layout_signature,
        )

    def _finish_mutation(self, normalize_fin: bool = False):
        if normalize_fin:
            self.normalize_working_fin()
        self._ensure_client_state()
        self.ui_dirty = True

    @staticmethod
    def _fin_row_has_payload(row: Optional[Dict[str, Any]]) -> bool:
        if row is None:
            return False
        if str(row.get("start_from", "")).strip():
            return True
        if str(row.get("state", "")).strip():
            return True
        if str(row.get("start_score", "")).strip():
            return True
        return bool(row.get("cells", {}))

    def _all_client_names(self, extra_clients: Optional[List[str]] = None) -> List[str]:
        clients = set(self.working_entries.keys())
        clients.update(
            str(row.get("client", "")).strip()
            for row in self.fin_rows
            if str(row.get("client", "")).strip()
        )
        clients.update(str(client_name).strip() for client_name in self.active_targets.keys() if str(client_name).strip())
        if extra_clients:
            clients.update(str(client_name).strip() for client_name in extra_clients if str(client_name).strip())
        return [client_name for client_name in clients if client_name]

    def _ensure_client_state(self, extra_clients: Optional[List[str]] = None):
        for client_name in self._all_client_names(extra_clients=extra_clients):
            self.working_entries.setdefault(client_name, [])
            self.active_targets.setdefault(client_name, "vertical")

    def _visible_clients(self) -> List[str]:
        self._ensure_client_state()
        clients = self._all_client_names()
        return sorted(
            (
                client_name
                for client_name in clients
                if client_name and self.working_entries.get(client_name)
            ),
            key=natural_sort_key,
        )


class StatsWindowControllerMixin:
    manager: StatsManager
    session: StatsEditorSession
    settings_source: Any

    selected_client: Optional[str]
    selected_row_index: Optional[int]
    selected_row_end_index: Optional[int]
    selected_vertical_field: Optional[str]
    selected_fin_row_index: Optional[int]
    selected_fin_column_key: Optional[str]
    _pending_manager_reload: bool

    def _show_info(self, title: str, text: str):
        raise NotImplementedError

    def _show_error(self, title: str, text: str):
        raise NotImplementedError

    def _ask_yes_no(self, title: str, text: str) -> bool:
        raise NotImplementedError

    def _ask_yes_no_cancel(self, title: str, text: str) -> Optional[bool]:
        raise NotImplementedError

    def _flush_active_editor(self) -> bool:
        raise NotImplementedError

    def _refresh_stats_view(
        self,
        preserve_selection: bool = True,
        main_view_mode: str = "selection",
        fin_view_mode: str = "selection",
    ):
        raise NotImplementedError

    def _parse_decimals_value(self) -> int:
        raise NotImplementedError

    def _get_decimals_text(self) -> str:
        raise NotImplementedError

    def _set_decimals_value(self, value: int):
        raise NotImplementedError

    def _set_window_puzzle(self, puzzle_id: str):
        raise NotImplementedError

    def _load_working_data(self):
        raise NotImplementedError

    def _discard_unsaved_changes(self, reload_ui: bool = True):
        raise NotImplementedError

    def focus_window(self):
        raise NotImplementedError

    def _merge_pending_manager_reload(self):
        raise NotImplementedError

    @property
    def puzzle_id(self) -> str:
        return self.session.puzzle_id

    @property
    def working_entries(self) -> Dict[str, List[Dict[str, Any]]]:
        return self.session.working_entries

    @working_entries.setter
    def working_entries(self, value: Dict[str, List[Dict[str, Any]]]):
        self.session.working_entries = value

    @property
    def fin_rows(self) -> List[Dict[str, Any]]:
        return self.session.fin_rows

    @fin_rows.setter
    def fin_rows(self, value: List[Dict[str, Any]]):
        self.session.fin_rows = value

    @property
    def fin_columns(self) -> List[Dict[str, Any]]:
        return self.session.fin_columns

    @fin_columns.setter
    def fin_columns(self, value: List[Dict[str, Any]]):
        self.session.fin_columns = value

    @property
    def active_targets(self) -> Dict[str, str]:
        return self.session.active_targets

    @active_targets.setter
    def active_targets(self, value: Dict[str, str]):
        self.session.active_targets = value

    @property
    def client_runtime(self) -> Dict[str, Dict[str, Any]]:
        return self.session.client_runtime

    @client_runtime.setter
    def client_runtime(self, value: Dict[str, Dict[str, Any]]):
        self.session.client_runtime = value

    @property
    def ui_dirty(self) -> bool:
        return self.session.ui_dirty

    @ui_dirty.setter
    def ui_dirty(self, value: bool):
        self.session.ui_dirty = bool(value)

    def _notify_user_interaction(self):
        callback = getattr(self, "note_user_interaction", None)
        if callable(callback):
            callback()

    def _selected_main_range(self) -> Optional[tuple[int, int]]:
        return selected_main_range(
            self.selected_client,
            self.selected_row_index,
            self.selected_row_end_index,
        )

    def _clear_main_selection(self):
        self.selected_row_index = None
        self.selected_row_end_index = None
        self.selected_vertical_field = None

    def _clear_fin_selection(self):
        self.selected_fin_row_index = None
        self.selected_fin_column_key = None

    def _set_main_selection(self, client_name: str, row_idx: int, field_name: str, extend: bool = False):
        clean_client = str(client_name).strip()
        if extend and self.selected_client == clean_client and self.selected_row_index is not None:
            self.selected_row_end_index = row_idx
        else:
            self.selected_client = clean_client
            self.selected_row_index = row_idx
            self.selected_row_end_index = row_idx
        self.selected_vertical_field = field_name

    def _set_fin_selection(self, row_idx: int, column_key: str):
        self.selected_fin_row_index = row_idx
        self.selected_fin_column_key = str(column_key).strip()

    def _require_main_rows(
        self,
        empty_selection_message: str,
        missing_row_message: Optional[str] = None,
    ) -> Optional[tuple[List[Dict[str, Any]], int, int]]:
        selected_range = self._selected_main_range()
        if self.selected_client is None or selected_range is None:
            self._show_info("Stats", empty_selection_message)
            return None

        entries = self.working_entries.setdefault(self.selected_client, [])
        start_idx, end_idx = selected_range
        if missing_row_message is not None and start_idx >= len(entries):
            self._show_info("Stats", missing_row_message)
            return None

        return entries, start_idx, end_idx

    def _require_fin_row(
        self,
        empty_selection_message: str = "Select a Finalization row first.",
        missing_row_message: str = "Selected Finalization row does not exist.",
    ) -> Optional[int]:
        if self.selected_fin_row_index is None:
            self._show_info("Stats", empty_selection_message)
            return None
        if self.selected_fin_row_index >= len(self.fin_rows):
            self._show_info("Stats", missing_row_message)
            return None
        return self.selected_fin_row_index

    def _require_fin_cell(self) -> Optional[tuple[int, str, Dict[str, Any]]]:
        row_idx = self._require_fin_row(empty_selection_message="Select a Finalization cell first.")
        if row_idx is None:
            return None
        if self.selected_fin_column_key is None:
            self._show_info("Stats", "Select a Finalization cell first.")
            return None
        return row_idx, self.selected_fin_column_key, self.fin_rows[row_idx]

    def _main_selection_clipboard_text(self) -> Optional[str]:
        selected_range = self._selected_main_range()
        if self.selected_client is None or selected_range is None:
            return None

        entries = self.working_entries.setdefault(self.selected_client, [])
        start_idx, end_idx = selected_range
        lines: List[str] = []
        for row_idx in range(start_idx, end_idx + 1):
            if row_idx < len(entries):
                entry = entries[row_idx]
                script = str(entry.get("script", "")).strip()
                score = format_score(entry.get("score", ""), self.manager.score_decimals)
            else:
                script = ""
                score = ""
            lines.append(f"{script}\t{score}")
        return "\n".join(lines)

    def _fin_cell_clipboard_text(self, row_idx: int, column_key: str) -> str:
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            return ""

        row = self.fin_rows[row_idx]
        if column_key == "client":
            return str(row.get("client", "")).strip()
        if column_key == "state":
            return latest_history_value(row.get("state", ""))
        if column_key == "notes":
            return str(row.get("notes", "")).strip()
        if column_key == "start_from":
            return str(row.get("start_from", "")).strip()
        if column_key == "start_score":
            return format_score(row.get("start_score", ""), self.manager.score_decimals)
        return format_score_latest(row.get("cells", {}).get(column_key, ""), self.manager.score_decimals)

    def _selected_fin_cell_clipboard_text(self) -> Optional[str]:
        selected_cell = self._require_fin_cell()
        if selected_cell is None:
            return None
        row_idx, column_key, _row = selected_cell
        return self._fin_cell_clipboard_text(row_idx, column_key)

    def paste_main_text(self, text: Any) -> bool:
        if not self._flush_active_editor():
            return False

        selected_range = self._selected_main_range()
        if self.selected_client is None or selected_range is None:
            self._show_info("Stats", "Select a main client cell first.")
            return False

        self._notify_user_interaction()
        self._merge_pending_manager_reload()

        start_idx, _end_idx = selected_range
        rows = parse_main_clipboard_rows(text)
        try:
            if rows is None:
                target_idx = self.session.update_main_cell(
                    self.selected_client,
                    start_idx,
                    self.selected_vertical_field or "script",
                    first_clipboard_cell_text(text).strip(),
                )
                if target_idx is None:
                    self._clear_main_selection()
                else:
                    self.selected_row_index = target_idx
                    self.selected_row_end_index = target_idx
            else:
                insert_start, insert_end = self.session.paste_vertical_rows(self.selected_client, start_idx, rows)
                self.selected_row_index = insert_start
                self.selected_row_end_index = insert_end
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return False

        self._refresh_stats_view(main_view_mode="preserve", fin_view_mode="preserve")
        return True

    def cut_main_selection_text(self) -> Optional[str]:
        if not self._flush_active_editor():
            return None

        selected_rows = self._require_main_rows(
            "Select main row(s) first.",
            missing_row_message="Selected main row does not exist.",
        )
        if selected_rows is None:
            return None

        text = self._main_selection_clipboard_text()
        _entries, start_idx, end_idx = selected_rows
        next_idx = self.session.delete_vertical_rows(self.selected_client, start_idx, end_idx)
        if next_idx is not None:
            self.selected_row_index = next_idx
            self.selected_row_end_index = next_idx
        else:
            self._clear_main_selection()
        self._refresh_stats_view()
        return text

    def paste_fin_text(self, text: Any) -> bool:
        if not self._flush_active_editor():
            return False

        selected_cell = self._require_fin_cell()
        if selected_cell is None:
            return False

        row_idx, column_key, _row = selected_cell
        if column_key == "client":
            self._show_info("Stats", "Client cell cannot be edited.")
            return False

        return self._handle_fin_edit_value(row_idx, column_key, first_clipboard_cell_text(text).strip())

    def cut_fin_selection_text(self) -> Optional[str]:
        if not self._flush_active_editor():
            return None

        selected_cell = self._require_fin_cell()
        if selected_cell is None:
            return None

        row_idx, column_key, _row = selected_cell
        if column_key == "client":
            self._show_info("Stats", "Client cell cannot be cut.")
            return None

        text = self._fin_cell_clipboard_text(row_idx, column_key)
        try:
            self.session.delete_fin_cell(row_idx, column_key)
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return None

        self._set_fin_selection(row_idx, column_key)
        self._refresh_stats_view()
        return text

    def _handle_main_edit_value(self, client_name: str, row_idx: int, field_name: str, value: Any) -> bool:
        self._notify_user_interaction()
        self._merge_pending_manager_reload()
        try:
            selected_idx = self.session.update_main_cell(
                client_name,
                row_idx,
                field_name,
                str(value).strip(),
            )
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return False

        self.selected_client = str(client_name).strip()
        self.selected_vertical_field = field_name
        if selected_idx is None:
            self._clear_main_selection()
        else:
            self.selected_row_index = selected_idx
            self.selected_row_end_index = selected_idx
        self._refresh_stats_view(main_view_mode="preserve", fin_view_mode="preserve")
        return True

    def _handle_fin_edit_value(self, row_idx: int, column_name: str, value: Any) -> bool:
        self._notify_user_interaction()
        self._merge_pending_manager_reload()
        try:
            self.session.update_fin_cell(row_idx, column_name, str(value).strip())
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return False
        self._set_fin_selection(row_idx, column_name)
        self._refresh_stats_view(main_view_mode="preserve", fin_view_mode="preserve")
        return True

    def add_vertical_row(self):
        if not self._flush_active_editor():
            return
        selected_rows = self._require_main_rows("Select a main client cell first.")
        if selected_rows is None:
            return

        _entries, _start_idx, end_idx = selected_rows
        insert_idx = self.session.add_vertical_row(self.selected_client, end_idx)
        self.selected_row_index = insert_idx
        self.selected_row_end_index = insert_idx
        self._refresh_stats_view()

    def delete_vertical_row(self):
        if not self._flush_active_editor():
            return
        selected_rows = self._require_main_rows(
            "Select main row(s) first.",
            missing_row_message="Selected main row does not exist.",
        )
        if selected_rows is None:
            return

        _entries, start_idx, end_idx = selected_rows
        next_idx = self.session.delete_vertical_rows(self.selected_client, start_idx, end_idx)
        if next_idx is not None:
            self.selected_row_index = next_idx
            self.selected_row_end_index = next_idx
        else:
            self._clear_main_selection()
        self._refresh_stats_view()

    def move_vertical_to_fin(self):
        if not self._flush_active_editor():
            return
        selected_range = self._selected_main_range()
        if self.selected_client is None or selected_range is None:
            self._show_info("Stats", "Select main row(s) first.")
            return

        entries = self.working_entries.setdefault(self.selected_client, [])
        start_idx, end_idx = selected_range
        if start_idx >= len(entries):
            try:
                fin_row_idx = self.session.activate_fin_target(self.selected_client)
            except StatsEditorError as exc:
                self._show_info("Stats", str(exc))
                return

            self._set_fin_selection(fin_row_idx, "notes")
            self._refresh_stats_view()
            return

        next_idx = self.session.move_vertical_to_fin(
            self.selected_client,
            start_idx,
            min(end_idx, len(entries) - 1),
        )
        if next_idx is not None:
            self.selected_row_index = next_idx
            self.selected_row_end_index = next_idx
        else:
            self.selected_row_index = None
            self.selected_row_end_index = None
        self._refresh_stats_view()

    def add_fin_row(self):
        if not self._flush_active_editor():
            return

        client_name = ""
        if self.selected_fin_row_index is not None and self.selected_fin_row_index < len(self.fin_rows):
            client_name = str(self.fin_rows[self.selected_fin_row_index].get("client", "")).strip()
        elif self.selected_client:
            client_name = str(self.selected_client).strip()

        try:
            new_row_idx = self.session.add_fin_row(client_name)
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return

        self._set_fin_selection(new_row_idx, "notes")
        self._refresh_stats_view()

    def move_fin_cell_to_vertical(self):
        if not self._flush_active_editor():
            return
        selected_cell = self._require_fin_cell()
        if selected_cell is None:
            return

        row_idx, column_key, _row = selected_cell
        try:
            self.session.move_fin_cell_to_vertical(row_idx, column_key)
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return

        self._refresh_stats_view()

    def move_fin_row_to_vertical(self):
        if not self._flush_active_editor():
            return
        row_idx = self._require_fin_row()
        if row_idx is None:
            return

        self.session.move_fin_row_to_vertical(row_idx)
        self._clear_fin_selection()
        self._refresh_stats_view()

    def delete_fin_cell(self):
        if not self._flush_active_editor():
            return
        selected_cell = self._require_fin_cell()
        if selected_cell is None:
            return

        row_idx, column_key, _row = selected_cell
        try:
            self.session.delete_fin_cell(row_idx, column_key)
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return

        self._refresh_stats_view()

    def delete_fin_row(self):
        if not self._flush_active_editor():
            return
        row_idx = self._require_fin_row()
        if row_idx is None:
            return

        self.session.delete_fin_row(row_idx)
        self._clear_fin_selection()
        self._refresh_stats_view()

    def move_fin_row(self, direction: int):
        if not self._flush_active_editor():
            return
        row_idx = self._require_fin_row()
        if row_idx is None:
            return

        try:
            new_row_idx = self.session.move_fin_row(row_idx, direction)
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return

        self.selected_fin_row_index = new_row_idx
        self._refresh_stats_view()

    def reorder_fin_columns(self, ordered_keys: List[str]):
        if not self._flush_active_editor():
            return
        try:
            changed = self.session.reorder_fin_columns(ordered_keys)
        except StatsEditorError as exc:
            self._show_info("Stats", str(exc))
            return
        if changed:
            self._refresh_stats_view(fin_view_mode="preserve")

    def _has_unsaved_changes(self) -> bool:
        return self.ui_dirty or self._get_decimals_text().strip() != str(self.manager.score_decimals)

    def client_table_summary(self) -> str:
        """One-line overview of which table each client feeds; stopped clients in parentheses."""
        targets = self.active_targets or {}
        runtime = self.client_runtime or {}  # holds only clients live on this puzzle
        main_clients: List[str] = []
        fin_clients: List[str] = []
        for client_name in sorted(targets, key=natural_sort_key):
            label = display_fin_client_name(client_name)
            if client_name not in runtime:
                label = f"({label})"
            if targets.get(client_name) == "horizontal":
                fin_clients.append(label)
            else:
                main_clients.append(label)
        parts = []
        if main_clients:
            parts.append("Main: " + " ".join(main_clients))
        if fin_clients:
            parts.append("Fin: " + " ".join(fin_clients))
        return "   |   ".join(parts) if parts else "No clients"

    def open_puzzle(self, puzzle_id: str) -> bool:
        clean_puzzle_id = str(puzzle_id).strip()
        if not clean_puzzle_id:
            return False
        if clean_puzzle_id == str(self.puzzle_id).strip():
            self.focus_window()
            return True
        if not self.manager.has_puzzle_log(clean_puzzle_id):
            self._show_info("Open Puzzle", f"Puzzle {clean_puzzle_id} was not found in puzzle logs.")
            return False
        if not self._flush_active_editor():
            return False
        if self._has_unsaved_changes():
            answer = self._ask_yes_no_cancel(
                "Unsaved changes",
                f"Save changes to puzzle {self.puzzle_id} before opening {clean_puzzle_id}?",
            )
            if answer is None:
                return False
            if answer:
                if not self.save():
                    return False
            else:
                self._discard_unsaved_changes(reload_ui=False)

        self._pending_manager_reload = False
        self._set_window_puzzle(clean_puzzle_id)
        self._set_decimals_value(int(self.manager.score_decimals))
        self._load_working_data()
        self._refresh_stats_view(preserve_selection=False, fin_view_mode="bottom")
        self.focus_window()
        return True

    def save(self) -> bool:
        if not self._flush_active_editor():
            return False
        try:
            decimals = self._parse_decimals_value()
        except ValueError:
            self._show_error("Invalid value", "Decimals must be an integer.")
            return False

        try:
            self.session.save(decimals)
        except Exception as exc:
            self._show_error("Save", f"Failed to save puzzle {self.puzzle_id}:\n{exc}")
            return False

        # Persist every other live puzzle to disk too, so one Save click gives the
        # same on-disk guarantee as the on-exit flush. The open puzzle was just
        # written by session.save above; flush_all force-writes the rest.
        try:
            self.manager.flush_all()
        except Exception as exc:
            self._show_error("Save", f"Puzzle {self.puzzle_id} saved, but flushing other puzzles failed:\n{exc}")

        self._pending_manager_reload = False

        if hasattr(self.settings_source, "save_stats_score_decimals"):
            try:
                self.settings_source.save_stats_score_decimals(decimals)
            except Exception as exc:
                self._show_error("Save", f"Puzzle saved, but failed to save decimals setting:\n{exc}")

        self._set_decimals_value(int(self.manager.score_decimals))
        self._refresh_stats_view(main_view_mode="preserve", fin_view_mode="preserve")
        return True

    def _confirm_close(self, save_prompt: bool) -> bool:
        if not self._flush_active_editor():
            return False
        if not self._has_unsaved_changes():
            return True

        if save_prompt:
            answer = self._ask_yes_no_cancel("Unsaved changes", "Save changes before closing?")
            if answer is None:
                return False
            if answer and not self.save():
                return False
            if answer is False:
                self._discard_unsaved_changes(reload_ui=False)
            return True

        discard = self._ask_yes_no("Discard changes", "Close without saving changes?")
        if not discard:
            return False
        self._discard_unsaved_changes(reload_ui=False)
        return True

    def _client_is_idle(self, client_name: str) -> bool:
        return self.session.client_is_idle(client_name)

    def _main_client_is_target(self, client_name: str) -> bool:
        return self.session.is_main_target(client_name)

    def _is_active_fin_row(self, row_idx: int) -> bool:
        return self.session.is_active_fin_row(row_idx)
