"""Stable data columns and configurable presentation for monitor trees."""

from copy import deepcopy


COLUMN_IDS = ("Score", "CPU", "Folder", "Type", "Puzzle")
DEFAULT_COLUMN_ORDER = ("Score", "CPU", "Puzzle", "Folder", "Type")
COLUMN_LABELS = {
    "Score": "Score",
    "CPU": "CPU",
    "Folder": "Client (Cl)",
    "Type": "Script",
    "Puzzle": "Puzzle",
}


def default_monitor_columns(puzzle_visible=True):
    return [
        {"id": column_id, "visible": column_id != "Puzzle" or bool(puzzle_visible)}
        for column_id in DEFAULT_COLUMN_ORDER
    ]


def validate_monitor_columns(columns):
    if not isinstance(columns, list) or len(columns) != len(COLUMN_IDS):
        raise ValueError("expected exactly five columns")
    ids = []
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"id", "visible"}:
            raise ValueError("each column must contain id and visible")
        if not isinstance(column["id"], str) or not isinstance(column["visible"], bool):
            raise ValueError("column id must be text and visible must be true or false")
        ids.append(column["id"])
    if len(set(ids)) != len(ids) or set(ids) != set(COLUMN_IDS):
        raise ValueError("column ids must contain each monitor column exactly once")
    if not any(column["visible"] for column in columns):
        raise ValueError("at least one column must be visible")


def effective_monitor_columns(raw_display, effective_display):
    """Use the old Puzzle toggle for profiles without a saved column layout."""
    configured = raw_display.get("monitor_columns") if isinstance(raw_display, dict) else None
    if configured is not None:
        try:
            validate_monitor_columns(configured)
            return deepcopy(configured)
        except ValueError:
            pass
    return default_monitor_columns(effective_display.get("show_puzzle_column", False))


def visible_column_ids(columns):
    return tuple(column["id"] for column in columns if column["visible"])


def column_at_x(tree, x):
    """Resolve Tk's visual #n column to the stable data-column ID."""
    visual = tree.identify_column(x)
    if not visual.startswith("#") or not visual[1:].isdigit():
        return None
    index = int(visual[1:]) - 1
    displayed = tree["displaycolumns"]
    if displayed == "#all":
        displayed = tree["columns"]
    if isinstance(displayed, str):
        displayed = tree.tk.splitlist(displayed)
    return displayed[index] if 0 <= index < len(displayed) else None
