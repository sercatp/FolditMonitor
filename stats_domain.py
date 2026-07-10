import math
from typing import Any, Dict, List, Optional


# A score value (one history line) may record where a script started and where it
# is now as "start→end". The grid shows the end (current) value; the tooltip and
# raw edit show the whole "start→end". "->" is accepted as an input alias for "→".
SCORE_ARROW = "→"

# Fixed Finalization columns (key -> header label) in their table/CSV order.
FIN_FIXED_COLUMNS = {
    "client": "client",
    "start_from": "from",
    "state": "state",
    "notes": "Notes",
    "start_score": "score",
}


def split_score_line(value: Any) -> tuple[str, str]:
    """Split a single score line into (start_text, end_text). start_text is "" when
    the line has no recorded start. Accepts both "→" and "->" as the separator."""
    if value is None:
        return "", ""
    if isinstance(value, (int, float)):
        return "", str(value)
    text = str(value).strip()
    if not text:
        return "", ""
    text = text.replace("->", SCORE_ARROW)
    if SCORE_ARROW in text:
        start, _, end = text.partition(SCORE_ARROW)
        return start.strip(), end.strip()
    return "", text


def score_current_text(value: Any) -> str:
    """The current (end) part of a score line, ignoring any recorded start."""
    return split_score_line(value)[1]


def parse_numeric_score(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def format_score(value: Any, decimals: int) -> str:
    """Format the current (end) value of a score. A "start→end" line collapses to
    its end here; use format_score_line/format_score_history to keep the start."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return _format_number(float(value), decimals)

    text = str(value).strip()
    if not text:
        return ""
    end = score_current_text(text)
    parsed = parse_numeric_score(end)
    if parsed is None:
        return end
    return _format_number(parsed, decimals)


def _format_number(value: float, decimals: int) -> str:
    places = max(0, decimals)
    factor = 10 ** places
    truncated = math.trunc(float(value) * factor) / factor
    return f"{truncated:.{places}f}"


def format_score_line(value: Any, decimals: int) -> str:
    """Format a single line, preserving a "start→end" pair (collapsing to the end
    when there is no start or start equals end)."""
    start, end = split_score_line(value)
    end_fmt = format_score(end, decimals)
    if not start or not end_fmt:
        return end_fmt
    start_fmt = format_score(start, decimals)
    if not start_fmt or start_fmt == end_fmt:
        return end_fmt
    return f"{start_fmt}{SCORE_ARROW}{end_fmt}"


def merge_score_line(existing: Any, new_value: Any, decimals: int) -> Any:
    """Update a single score line's end to `new_value`, keeping its start. When
    `new_value` itself carries a start, that start wins (transfer case)."""
    new_start, new_end = split_score_line(new_value)
    if new_start:
        start_text = new_start
    else:
        ex_start, ex_end = split_score_line(existing)
        start_text = ex_start if ex_start else ex_end
    line = f"{start_text}{SCORE_ARROW}{new_end}" if start_text else new_end
    return normalize_score_value(format_score_line(line, decimals))


def normalize_score_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return ""

    start, end = split_score_line(text)
    if start:
        # Keep a "start→end" pair as a canonical string instead of a bare number.
        return f"{start}{SCORE_ARROW}{end}" if end else start

    parsed = parse_numeric_score(text)
    return parsed if parsed is not None else text


def normalize_state_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_target_value(value: Any) -> str:
    """A client's stats target has two valid values; anything unknown means "vertical"."""
    return "horizontal" if str(value).strip().lower() == "horizontal" else "vertical"


def split_history_lines(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    else:
        text = str(value).strip()
    if not text:
        return []
    return [line.strip() for line in text.split("\n") if line.strip()]


def normalize_history_value(value: Any) -> str:
    return "\n".join(split_history_lines(value))


def latest_history_value(value: Any) -> str:
    lines = split_history_lines(value)
    return lines[-1] if lines else ""


def decode_history_edit_value(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    return normalize_history_value(text)


def encode_history_edit_value(value: Any, decimals: Optional[int] = None) -> str:
    if decimals is None:
        text = normalize_history_value(value)
    else:
        text = format_score_history(value, decimals)
    return text.replace("\n", "\\n")


def format_score_history(value: Any, decimals: int) -> str:
    lines = split_history_lines(value)
    if not lines:
        return ""
    return "\n".join(format_score_line(line, decimals) for line in lines)


def format_score_latest(value: Any, decimals: int) -> str:
    return format_score(latest_history_value(value), decimals)


def normalize_score_history_edit_value(value: Any, decimals: int) -> Any:
    lines = split_history_lines(decode_history_edit_value(value))
    if not lines:
        return ""
    formatted_lines = [format_score_line(line, decimals) for line in lines]
    if len(formatted_lines) == 1:
        return normalize_score_value(formatted_lines[0])
    return "\n".join(formatted_lines)


def append_score_history_if_changed(current_value: Any, new_value: Any, decimals: int) -> tuple[Any, bool]:
    """Append `new_value` as a new history line (a new script run). The end value is
    used for dedup; a "start→end" new value is preserved as-is."""
    new_line = format_score_line(new_value, decimals)
    if not new_line:
        return current_value, False

    lines = split_history_lines(current_value)
    if not lines:
        return normalize_score_value(new_line), True

    if format_score(lines[-1], decimals) == format_score(new_value, decimals):
        return current_value, False

    formatted_lines = [format_score_line(line, decimals) for line in lines]
    formatted_lines.append(new_line)
    return "\n".join(formatted_lines), True


def replace_latest_score_history_if_changed(current_value: Any, new_value: Any, decimals: int) -> tuple[Any, bool]:
    """Update the end of the latest history line (the running script), keeping its
    recorded start. Materializes "start→end" once the end moves off the start."""
    if not format_score(new_value, decimals):
        return current_value, False

    lines = split_history_lines(current_value)
    if not lines:
        return normalize_score_value(format_score_line(new_value, decimals)), True

    new_line = merge_score_line(lines[-1], new_value, decimals)
    new_line_text = format_score_line(new_line, decimals)
    if format_score_line(lines[-1], decimals) == new_line_text:
        return current_value, False

    formatted_lines = [format_score_line(line, decimals) for line in lines[:-1]]
    formatted_lines.append(new_line_text)
    if len(formatted_lines) == 1:
        return normalize_score_value(new_line_text), True
    return "\n".join(formatted_lines), True


def append_text_history_if_changed(current_value: Any, new_value: Any) -> tuple[str, bool]:
    new_text = normalize_state_value(new_value)
    if not new_text:
        return normalize_history_value(current_value), False

    lines = split_history_lines(current_value)
    if lines and lines[-1] == new_text:
        return normalize_history_value(current_value), False

    lines.append(new_text)
    return "\n".join(lines), True


class FinalizationDomain:
    def __init__(self, settings: Dict[str, Any], score_decimals: int):
        self.score_decimals = max(0, int(score_decimals))
        self.script_column_numbers = self._build_script_column_numbers(settings)
        self.default_numbered_columns = self._build_default_numbered_columns(settings)
        self.gap_column_numbers = self._build_gap_column_numbers(settings)

    def set_score_decimals(self, decimals: int):
        self.score_decimals = max(0, int(decimals))

    @staticmethod
    def _build_script_column_numbers(settings: Dict[str, Any]) -> Dict[str, int]:
        column_numbers: Dict[str, int] = {}
        mapping = settings.get("script_type_mapping", {})
        for value in mapping.values():
            if not isinstance(value, dict):
                continue
            name = str(value.get("name", "")).strip()
            if not name:
                continue
            try:
                column_number = int(value.get("column_number", 0))
            except (TypeError, ValueError):
                column_number = 0
            column_numbers.setdefault(name.casefold(), column_number)
        return column_numbers

    @staticmethod
    def _build_gap_column_numbers(settings: Dict[str, Any]) -> set:
        """Highlights of "empty script" mappings (name == "") with a positive number.
        No real script routes to these, so they become inert, unnamed columns used to
        surface an unlogged manual score jump (the next script's start score)."""
        gaps: set = set()
        mapping = settings.get("script_type_mapping", {})
        for value in mapping.values():
            if not isinstance(value, dict):
                continue
            if str(value.get("name", "")).strip():
                continue
            try:
                column_number = int(value.get("column_number", 0))
            except (TypeError, ValueError):
                column_number = 0
            if column_number > 0:
                gaps.add(column_number)
        return gaps

    def _build_default_numbered_columns(self, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
        columns: List[Dict[str, Any]] = []
        seen_column_numbers = set()
        mapping = settings.get("script_type_mapping", {})
        for value in mapping.values():
            if not isinstance(value, dict):
                continue
            try:
                column_number = int(value.get("column_number", 0))
            except (TypeError, ValueError):
                column_number = 0
            if column_number <= 0 or column_number in seen_column_numbers:
                continue
            seen_column_numbers.add(column_number)
            columns.append(
                {
                    "key": f"h:{column_number}",
                    "label": str(value.get("name", "")).strip(),
                    "kind": "numbered",
                    "column_number": column_number,
                }
            )
        return self.sort_fin_columns(columns)

    def resolve_script_column_number(self, script_name: str) -> int:
        clean_name = str(script_name).strip()
        if not clean_name:
            return 0
        return int(self.script_column_numbers.get(clean_name.casefold(), 0))

    def _is_gap_column(self, column: Dict[str, Any]) -> bool:
        """A gap column is an (unnamed) numbered column whose number was registered as a
        gap in script_type_mapping. No real script routes to it; it exposes an unlogged
        manual score jump into the following phase."""
        return (
            str(column.get("kind", "")) == "numbered"
            and int(column.get("column_number", 0) or 0) in self.gap_column_numbers
        )

    @staticmethod
    def _column_first_start_value(cell_value: Any) -> str:
        """Start score of a column's first run for one row (the score the phase began
        at). Falls back to the end when no start was recorded."""
        lines = split_history_lines(cell_value)
        if not lines:
            return ""
        start, end = split_score_line(lines[0])
        return start or end

    def recompute_gap_cells(self, puzzle: Any):
        """Fill every still-empty gap cell with the start score of the next column to its
        right (in display order) that holds data. Positional successor to the old numeric
        capture: the gap surfaces the unlogged jump into the following phase whether that
        phase landed on a numbered or a dynamic column, and follows manual column
        reordering. "Only if empty" keeps the first crossing and never disturbs a manual
        edit or a later rerun."""
        columns = puzzle.fin_columns
        if not columns:
            return
        gap_flags = [self._is_gap_column(column) for column in columns]
        if not any(gap_flags):
            return
        keys = [str(column.get("key", "")).strip() for column in columns]
        for row in puzzle.fin_rows:
            cells = row.get("cells", {})
            if not cells:
                continue
            for idx, is_gap in enumerate(gap_flags):
                if not is_gap or str(cells.get(keys[idx], "")).strip():
                    continue
                for next_idx in range(idx + 1, len(columns)):
                    if gap_flags[next_idx]:
                        continue
                    next_value = cells.get(keys[next_idx], "")
                    if not str(next_value).strip():
                        continue
                    start_value = self._column_first_start_value(next_value)
                    if str(start_value).strip():
                        cells[keys[idx]] = normalize_score_value(start_value)
                    break

    @staticmethod
    def parse_fin_column(key: str, label: str) -> Optional[Dict[str, Any]]:
        clean_key = str(key).strip()
        clean_label = str(label).strip()
        if not clean_key:
            return None

        if clean_key.startswith("h:"):
            parsed_column_number = FinalizationDomain.parse_numbered_column_key(clean_key)
            column_number = parsed_column_number[0] if parsed_column_number is not None else 0
            return {
                "key": clean_key,
                "label": clean_label,
                "kind": "numbered",
                "column_number": column_number,
            }

        if clean_key.startswith("blank:"):
            return {
                "key": clean_key,
                "label": "",
                "kind": "blank",
                "column_number": 0,
            }

        if clean_key.startswith("bridge:"):
            return {
                "key": clean_key,
                "label": "",
                "kind": "bridge",
                "column_number": 0,
            }

        if clean_key.startswith("d:"):
            if not clean_label:
                clean_label = clean_key.split(":", 1)[1]
            return {
                "key": clean_key,
                "label": clean_label,
                "kind": "dynamic",
                "column_number": 0,
            }

        return {
            "key": clean_key,
            "label": clean_label,
            "kind": "dynamic",
            "column_number": 0,
        }

    @staticmethod
    def new_fin_row(
        client_name: str,
        state: str = "",
        notes: str = "",
        start_from: str = "",
        start_score: Any = "",
    ) -> Dict[str, Any]:
        return {
            "client": str(client_name).strip(),
            "state": normalize_state_value(state),
            "notes": str(notes).strip(),
            "start_from": str(start_from).strip(),
            "start_score": normalize_score_value(start_score),
            "cells": {},
        }

    @staticmethod
    def sort_fin_columns(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        numbered_columns = [col for col in columns if col.get("kind") == "numbered"]
        other_columns = [col for col in columns if col.get("kind") != "numbered"]
        numbered_columns.sort(key=lambda col: int(col.get("column_number", 0)))
        return numbered_columns + other_columns

    @staticmethod
    def parse_numbered_column_key(key: str) -> Optional[tuple[int, int]]:
        clean_key = str(key).strip()
        if not clean_key.startswith("h:"):
            return None

        suffix = clean_key[2:]
        sequence = 1
        if "#" in suffix:
            suffix, sequence_text = suffix.split("#", 1)
            try:
                sequence = int(sequence_text)
            except (TypeError, ValueError):
                sequence = 1

        try:
            column_number = int(suffix)
        except (TypeError, ValueError):
            return None

        return column_number, max(1, sequence)

    @staticmethod
    def numbered_column_key(column_number: int, sequence: int = 1) -> str:
        clean_column_number = int(column_number)
        clean_sequence = max(1, int(sequence))
        if clean_sequence == 1:
            return f"h:{clean_column_number}"
        return f"h:{clean_column_number}#{clean_sequence}"

    @classmethod
    def is_primary_numbered_key(cls, key: str) -> bool:
        parsed = cls.parse_numbered_column_key(key)
        return bool(parsed is not None and parsed[1] == 1)

    @classmethod
    def next_numbered_column_key(cls, columns: List[Dict[str, Any]], column_number: int) -> str:
        clean_column_number = int(column_number)
        next_sequence = 1
        for column in columns:
            parsed = cls.parse_numbered_column_key(column.get("key", ""))
            if parsed is None or parsed[0] != clean_column_number:
                continue
            next_sequence = max(next_sequence, parsed[1] + 1)
        return cls.numbered_column_key(clean_column_number, next_sequence)

    @staticmethod
    def insert_column_after_key(columns: List[Dict[str, Any]], new_column: Dict[str, Any], after_key: Optional[str]) -> None:
        clean_after_key = str(after_key).strip() if after_key else ""
        if clean_after_key:
            for idx, column in enumerate(columns):
                if str(column.get("key", "")).strip() == clean_after_key:
                    columns.insert(idx + 1, new_column)
                    return
        columns.append(new_column)

    @staticmethod
    def insert_numbered_column_in_order(columns: List[Dict[str, Any]], new_column: Dict[str, Any]) -> None:
        new_column_number = int(new_column.get("column_number", 0) or 0)
        for idx, column in enumerate(columns):
            if column.get("kind") != "numbered":
                continue
            existing_column_number = int(column.get("column_number", 0) or 0)
            if existing_column_number > new_column_number:
                columns.insert(idx, new_column)
                return
        columns.append(new_column)

    def prune_fin_data(self, puzzle: Any, recompute_gaps: bool = False):
        existing_columns: List[Dict[str, Any]] = []
        seen_keys = set()
        for column in puzzle.fin_columns:
            parsed = self.parse_fin_column(column.get("key", ""), column.get("label", ""))
            if parsed is None or parsed["key"] in seen_keys:
                continue
            seen_keys.add(parsed["key"])
            existing_columns.append(parsed)

        ordered_keys = [column["key"] for column in existing_columns]
        used_keys = set()
        rows: List[Dict[str, Any]] = []

        for raw_row in puzzle.fin_rows:
            client_name = str(raw_row.get("client", "")).strip()
            state = normalize_state_value(raw_row.get("state", ""))
            notes = str(raw_row.get("notes", "")).strip()
            start_from = str(raw_row.get("start_from", "")).strip()
            start_score = normalize_score_value(raw_row.get("start_score", ""))

            cells: Dict[str, Any] = {}
            for key, value in dict(raw_row.get("cells", {})).items():
                clean_key = str(key).strip()
                if not clean_key:
                    continue
                score_value = normalize_score_value(value)
                if not str(score_value).strip():
                    continue
                cells[clean_key] = score_value
                used_keys.add(clean_key)
                if clean_key not in ordered_keys:
                    ordered_keys.append(clean_key)

            if (
                not client_name
                and not state
                and not notes
                and not start_from
                and not str(start_score).strip()
                and not cells
            ):
                continue
            if not client_name:
                continue

            rows.append(
                {
                    "client": client_name,
                    "state": state,
                    "notes": notes,
                    "start_from": start_from,
                    "start_score": start_score,
                    "cells": cells,
                }
            )

        column_map = {column["key"]: column for column in existing_columns}
        columns: List[Dict[str, Any]] = []
        included_keys = set()

        for column in existing_columns:
            key = column["key"]
            if column.get("kind") == "numbered":
                if self.is_primary_numbered_key(key) or key in used_keys:
                    columns.append(column)
                    included_keys.add(key)
                continue
            if key in used_keys:
                columns.append(column)
                included_keys.add(key)

        for default_column in self.default_numbered_columns:
            key = default_column["key"]
            if key in included_keys:
                continue
            column_copy = {
                "key": key,
                "label": default_column.get("label", ""),
                "kind": "numbered",
                "column_number": int(default_column.get("column_number", 0) or 0),
            }
            self.insert_numbered_column_in_order(columns, column_copy)
            included_keys.add(key)

        for key in ordered_keys:
            if key in included_keys or key not in used_keys:
                continue
            column = column_map.get(key)
            if column is None:
                column = self.parse_fin_column(key, "")
            if column is not None:
                columns.append(column)
                included_keys.add(key)

        puzzle.fin_rows = rows
        puzzle.fin_columns = columns

        if recompute_gaps:
            self.recompute_gap_cells(puzzle)

    @staticmethod
    def copy_fin_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        copied: List[Dict[str, Any]] = []
        for row in rows:
            copied.append(
                {
                    "client": str(row.get("client", "")).strip(),
                    "state": normalize_state_value(row.get("state", "")),
                    "notes": str(row.get("notes", "")).strip(),
                    "start_from": str(row.get("start_from", "")).strip(),
                    "start_score": row.get("start_score", ""),
                    "cells": dict(row.get("cells", {})),
                }
            )
        return copied

    @staticmethod
    def copy_fin_columns(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        copied: List[Dict[str, Any]] = []
        for column in columns:
            copied.append(
                {
                    "key": str(column.get("key", "")).strip(),
                    "label": str(column.get("label", "")).strip(),
                    "kind": str(column.get("kind", "dynamic")).strip(),
                    "column_number": int(column.get("column_number", 0) or 0),
                }
            )
        return copied

    @staticmethod
    def find_last_fin_row_index(puzzle: Any, client_name: str) -> Optional[int]:
        for idx in range(len(puzzle.fin_rows) - 1, -1, -1):
            if str(puzzle.fin_rows[idx].get("client", "")).strip() == client_name:
                return idx
        return None

    @staticmethod
    def find_last_fin_row_with_cells_index(puzzle: Any, client_name: str) -> Optional[int]:
        for idx in range(len(puzzle.fin_rows) - 1, -1, -1):
            row = puzzle.fin_rows[idx]
            if str(row.get("client", "")).strip() != client_name:
                continue
            if dict(row.get("cells", {})):
                return idx
        return None

    @staticmethod
    def find_fin_column(puzzle: Any, key: str) -> Optional[Dict[str, Any]]:
        for column in puzzle.fin_columns:
            if column.get("key") == key:
                return column
        return None

    @staticmethod
    def dynamic_column_key(script_name: str) -> str:
        return f"d:{script_name.casefold()}"

    def _update_numbered_column_label(self, puzzle: Any, column: Dict[str, Any], label: str):
        clean_label = str(label).strip()
        if not clean_label:
            return

        key = str(column.get("key", "")).strip()
        has_data = any(
            key in row.get("cells", {}) and str(row.get("cells", {}).get(key, "")).strip()
            for row in puzzle.fin_rows
        )
        if not str(column.get("label", "")).strip() or not has_data:
            column["label"] = clean_label

    def ensure_numbered_column(
        self,
        puzzle: Any,
        column_number: int,
        label: str,
        after_key: Optional[str] = None,
        row: Optional[Dict[str, Any]] = None,
    ) -> str:
        clean_column_number = int(column_number)
        base_key = self.numbered_column_key(clean_column_number)
        base_column = self.find_fin_column(puzzle, base_key)
        if base_column is None:
            base_column = {
                "key": base_key,
                "label": str(label).strip(),
                "kind": "numbered",
                "column_number": clean_column_number,
            }
            self.insert_numbered_column_in_order(puzzle.fin_columns, base_column)
        else:
            self._update_numbered_column_label(puzzle, base_column, label)

        for column in puzzle.fin_columns:
            parsed = self.parse_numbered_column_key(column.get("key", ""))
            if parsed is None or parsed[0] != clean_column_number:
                continue
            self._update_numbered_column_label(puzzle, column, label)

        return base_key

    def ensure_dynamic_column(self, puzzle: Any, script_name: str, after_key: Optional[str] = None) -> str:
        clean_name = str(script_name).strip()
        key = self.dynamic_column_key(clean_name)
        if self.find_fin_column(puzzle, key) is None:
            new_column = {
                "key": key,
                "label": clean_name,
                "kind": "dynamic",
                "column_number": 0,
            }
            if after_key:
                self.insert_column_after_key(puzzle.fin_columns, new_column, after_key)
            else:
                puzzle.fin_columns.insert(0, new_column)
        return key

    def ensure_script_column(self, puzzle: Any, row: Dict[str, Any], script_name: str) -> str:
        clean_name = str(script_name).strip()
        after_key = self.find_last_fin_cell_key(puzzle, row)
        column_number = self.resolve_script_column_number(clean_name)
        if clean_name and column_number > 0:
            return self.ensure_numbered_column(
                puzzle,
                column_number,
                clean_name,
                after_key=after_key,
                row=row,
            )
        if clean_name:
            return self.ensure_dynamic_column(puzzle, clean_name, after_key=after_key)
        if after_key:
            return after_key
        return self.ensure_blank_column(puzzle, row, after_key=after_key)

    @staticmethod
    def bridge_column_key(after_key: str) -> str:
        return f"bridge:{str(after_key).strip()}"

    def ensure_bridge_column(self, puzzle: Any, after_key: str) -> str:
        clean_after_key = str(after_key).strip()
        key = self.bridge_column_key(clean_after_key)
        existing = self.find_fin_column(puzzle, key)
        if existing is None:
            self.insert_column_after_key(
                puzzle.fin_columns,
                {
                    "key": key,
                    "label": "",
                    "kind": "bridge",
                    "column_number": 0,
                },
                clean_after_key,
            )
        return key

    def ensure_blank_column(self, puzzle: Any, row: Dict[str, Any], after_key: Optional[str] = None) -> str:
        for column in puzzle.fin_columns:
            if column.get("kind") != "blank":
                continue
            if column.get("key") not in row.get("cells", {}):
                return str(column.get("key"))

        for column in puzzle.fin_columns:
            key = str(column.get("key", "")).strip()
            kind = str(column.get("kind", "")).strip()
            label = str(column.get("label", "")).strip()
            if kind != "bridge" and not key.startswith("bridge:"):
                continue
            if label:
                continue
            if key not in row.get("cells", {}):
                return key

        clean_after_key = str(after_key).strip() if after_key else ""
        if clean_after_key:
            return self.ensure_bridge_column(puzzle, clean_after_key)

        idx = 1
        existing_keys = {column.get("key") for column in puzzle.fin_columns}
        key = f"blank:{idx}"
        while key in existing_keys:
            idx += 1
            key = f"blank:{idx}"

        self.insert_column_after_key(
            puzzle.fin_columns,
            {
                "key": key,
                "label": "",
                "kind": "blank",
                "column_number": 0,
            },
            after_key,
        )
        return key

    def get_or_create_fin_row(self, puzzle: Any, client_name: str) -> Dict[str, Any]:
        last_row_idx = self.find_last_fin_row_index(puzzle, client_name)
        if last_row_idx is None:
            row = self.new_fin_row(client_name)
            puzzle.fin_rows.append(row)
            return row
        return puzzle.fin_rows[last_row_idx]

    @staticmethod
    def find_last_fin_cell_key(puzzle: Any, row: Dict[str, Any]) -> Optional[str]:
        cells = row.get("cells", {})
        for column in reversed(puzzle.fin_columns):
            key = str(column.get("key", "")).strip()
            if key and key in cells and str(cells.get(key, "")).strip():
                return key
        return None

    def target_column_key_for_script(self, script_name: str) -> Optional[str]:
        clean_name = str(script_name).strip()
        if not clean_name:
            return None
        column_number = self.resolve_script_column_number(clean_name)
        if column_number > 0:
            return self.numbered_column_key(column_number)
        return self.dynamic_column_key(clean_name)

    def column_matches_script(self, key: str, script_name: str) -> bool:
        clean_key = str(key).strip()
        clean_name = str(script_name).strip()
        if not clean_key or not clean_name:
            return False

        column_number = self.resolve_script_column_number(clean_name)
        if column_number > 0:
            parsed = self.parse_numbered_column_key(clean_key)
            return bool(parsed is not None and parsed[0] == column_number)

        return clean_key == self.dynamic_column_key(clean_name)

    def scores_match_for_startup(self, saved_score: Any, current_score: float) -> bool:
        return format_score_latest(saved_score, self.score_decimals) == format_score(current_score, self.score_decimals)

    def resolve_startup_target(
        self,
        puzzle: Any,
        client_name: str,
        script_name: str,
        score_value: float,
    ) -> str:
        row_idx = self.find_last_fin_row_with_cells_index(puzzle, client_name)
        if row_idx is None:
            return "vertical"

        row = puzzle.fin_rows[row_idx]
        last_key = self.find_last_fin_cell_key(puzzle, row)
        if not last_key:
            return "vertical"

        if not self.column_matches_script(last_key, script_name):
            return "vertical"

        saved_score = row.get("cells", {}).get(last_key, "")
        if not self.scores_match_for_startup(saved_score, score_value):
            return "vertical"

        return "horizontal"
