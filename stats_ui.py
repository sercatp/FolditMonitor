import importlib
import time
import tkinter as tk
from tkinter import font, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from stats_editor import StatsEditorSession, StatsWindowControllerMixin, display_fin_client_name
from stats_module import (
    PuzzleLogInfo,
    StatsManager,
    StatsUiBridge,
    format_score,
    natural_sort_key,
    resolve_settings_dict,
)
from window_manager import open_path


_qt_backend_module = None
_qt_backend_import_error: Optional[Exception] = None


class PuzzlePickerDialog:
    def __init__(self, parent: tk.Misc, puzzles: List[PuzzleLogInfo], current_puzzle_id: str):
        self.current_puzzle_id = str(current_puzzle_id).strip()
        self.puzzles = list(puzzles)
        self.visible_puzzles: Dict[str, PuzzleLogInfo] = {}
        self.selected_puzzle_id: Optional[str] = None

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Open Puzzle")
        self.dialog.transient(parent)
        self.dialog.protocol("WM_DELETE_WINDOW", self._cancel)
        self.dialog.minsize(520, 320)

        self.filter_var = tk.StringVar()

        container = ttk.Frame(self.dialog, padding=8)
        container.pack(fill=tk.BOTH, expand=True)

        filter_row = ttk.Frame(container)
        filter_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(filter_row, text="Filter:").pack(side=tk.LEFT)
        self.filter_entry = ttk.Entry(filter_row, textvariable=self.filter_var)
        self.filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        list_frame = ttk.Frame(container)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            list_frame,
            columns=("puzzle", "modified", "state"),
            show="headings",
            selectmode="browse",
            height=min(18, max(8, len(self.puzzles))),
        )
        self.tree.heading("puzzle", text="Puzzle")
        self.tree.heading("modified", text="Modified")
        self.tree.heading("state", text="State")
        self.tree.column("puzzle", width=180, stretch=True, anchor=tk.W)
        self.tree.column("modified", width=135, stretch=False, anchor=tk.W)
        self.tree.column("state", width=120, stretch=False, anchor=tk.W)

        tree_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="")
        ttk.Label(container, textvariable=self.status_var).pack(fill=tk.X, pady=(6, 0))

        button_row = ttk.Frame(container)
        button_row.pack(fill=tk.X, pady=(8, 0))
        self.open_button = ttk.Button(button_row, text="Open", width=10, command=self._open_selected)
        self.open_button.pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(button_row, text="Cancel", width=10, command=self._cancel).pack(side=tk.RIGHT)

        self.filter_var.trace_add("write", self._refresh_list)
        self.tree.bind("<Double-1>", self._open_selected)
        self.tree.bind("<Return>", self._open_selected)
        self.dialog.bind("<Escape>", self._cancel)
        self.dialog.bind("<Return>", self._open_selected)

        self._refresh_list()
        self._position_dialog(parent)
        self.dialog.grab_set()
        self.dialog.after_idle(self.filter_entry.focus_set)

    @staticmethod
    def _format_modified(last_modified: float) -> str:
        if last_modified <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(last_modified))

    @staticmethod
    def _state_text(info: PuzzleLogInfo, current_puzzle_id: str) -> str:
        parts: List[str] = []
        if info.puzzle_id == current_puzzle_id:
            parts.append("current")
        if info.is_active:
            parts.append("active")
        if info.has_fin:
            parts.append("fin")
        return ", ".join(parts) if parts else "saved"

    def _position_dialog(self, parent: tk.Misc):
        self.dialog.update_idletasks()
        x = parent.winfo_rootx() + 40
        y = parent.winfo_rooty() + 40
        self.dialog.geometry(f"+{x}+{y}")

    def _refresh_list(self, *_args):
        query = self.filter_var.get().strip().lower()
        current_selection = self.tree.selection()
        selected_puzzle_id = None
        if current_selection:
            selected_info = self.visible_puzzles.get(current_selection[0])
            if selected_info is not None:
                selected_puzzle_id = selected_info.puzzle_id

        self.visible_puzzles.clear()
        self.tree.delete(*self.tree.get_children())

        first_item_id = None
        current_item_id = None
        selected_item_id = None

        for idx, info in enumerate(self.puzzles):
            if query and query not in info.puzzle_id.lower():
                continue

            item_id = f"puzzle_{idx}"
            self.visible_puzzles[item_id] = info
            self.tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    info.puzzle_id,
                    self._format_modified(info.last_modified),
                    self._state_text(info, self.current_puzzle_id),
                ),
            )
            if first_item_id is None:
                first_item_id = item_id
            if info.puzzle_id == self.current_puzzle_id:
                current_item_id = item_id
            if selected_puzzle_id and info.puzzle_id == selected_puzzle_id:
                selected_item_id = item_id

        item_to_select = selected_item_id or current_item_id or first_item_id
        if item_to_select is None:
            self.status_var.set("No puzzles matched the filter.")
            self.open_button.state(["disabled"])
            return

        self.tree.selection_set(item_to_select)
        self.tree.focus(item_to_select)
        self.tree.see(item_to_select)
        visible_count = len(self.visible_puzzles)
        total_count = len(self.puzzles)
        self.status_var.set(f"Showing {visible_count} of {total_count} puzzle logs.")
        self.open_button.state(["!disabled"])

    def _open_selected(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return "break"

        info = self.visible_puzzles.get(selection[0])
        if info is None:
            return "break"

        self.selected_puzzle_id = info.puzzle_id
        self.dialog.destroy()
        return "break"

    def _cancel(self, _event=None):
        self.selected_puzzle_id = None
        self.dialog.destroy()
        return "break"

    def show(self) -> Optional[str]:
        self.dialog.wait_window()
        return self.selected_puzzle_id


class StatsWindowV2(StatsWindowControllerMixin):
    _instance = None

    @classmethod
    def get_open_instance(cls):
        instance = cls._instance
        if instance is None:
            return None
        if instance.window.winfo_exists():
            return instance
        cls._instance = None
        return None

    @classmethod
    def get_open_puzzle_id(cls) -> Optional[str]:
        instance = cls.get_open_instance()
        if instance is None:
            return None
        return str(instance.puzzle_id).strip()

    @classmethod
    def is_open_for_puzzle(cls, puzzle_id: str) -> bool:
        clean_puzzle_id = str(puzzle_id).strip()
        return bool(clean_puzzle_id) and cls.get_open_puzzle_id() == clean_puzzle_id

    @classmethod
    def focus_if_open(cls, puzzle_id: Optional[str] = None) -> bool:
        instance = cls.get_open_instance()
        if instance is None:
            return False
        if puzzle_id is not None and str(instance.puzzle_id).strip() != str(puzzle_id).strip():
            return False
        instance.focus_window()
        return True

    @classmethod
    def is_user_interacting(cls) -> bool:
        instance = cls.get_open_instance()
        return bool(instance is not None and instance._is_window_interacting)

    @classmethod
    def close_if_exists(cls, puzzle_id: Optional[str] = None):
        instance = cls.get_open_instance()
        if instance is not None:
            if puzzle_id is not None and str(instance.puzzle_id).strip() != str(puzzle_id).strip():
                return False
            return instance.request_close()
        return False

    def __init__(
        self,
        parent: tk.Tk,
        manager: StatsManager,
        puzzle_id: str,
        settings_source: Any,
        log_lookup_handler: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        if StatsWindowV2.get_open_instance():
            if not StatsWindowV2.close_if_exists():
                return

        self.manager = manager
        self.session = StatsEditorSession(manager, puzzle_id)
        self.settings_source = settings_source
        self.settings = resolve_settings_dict(settings_source)
        self.log_lookup_handler = log_lookup_handler

        self.clients: List[str] = []

        self.selected_client: Optional[str] = None
        self.selected_row_index: Optional[int] = None
        self.selected_row_end_index: Optional[int] = None
        self.selected_vertical_field: Optional[str] = None
        self.selected_fin_row_index: Optional[int] = None
        self.selected_fin_column_key: Optional[str] = None

        self._editor: Optional[tk.Entry] = None
        self._editor_commit = None
        self._pending_manager_reload = False
        self._window_size_initialized = False
        self._window_configure_job: Optional[str] = None
        self._is_window_interacting = False
        self._main_scroll_job: Optional[str] = None
        self._is_main_scroll_interacting = False

        self.vertical_column_specs: List[Dict[str, Any]] = []
        self.vertical_column_specs_by_name: Dict[str, Dict[str, Any]] = {}
        self.vertical_client_columns: Dict[str, Dict[str, str]] = {}
        self.fin_column_specs: List[Dict[str, Any]] = []
        self.fin_column_specs_by_name: Dict[str, Dict[str, Any]] = {}
        self._main_selection_border: List[tk.Frame] = []
        self._main_status_overlays: Dict[str, Dict[str, tk.Frame]] = {}
        self._main_separator_overlays: List[tk.Frame] = []
        self._ui_bridge: Optional[StatsUiBridge] = None

        self.window = tk.Toplevel(parent)
        self.window.title(f"Stats - {self.puzzle_id}")
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.request_close)
        self.window.bind("<Configure>", self._on_window_configure, add="+")
        self._apply_initial_position(parent)

        font_settings = self.settings.get("display", {}).get("fonts", {})
        self.stats_font = font.Font(
            family=font_settings.get("family", "DejaVu Sans Mono"),
            size=font_settings.get("stats_size", 8),
        )

        style = ttk.Style()
        row_height = self.stats_font.metrics("linespace") + 6
        style.configure("StatsV2.Treeview", font=self.stats_font, rowheight=row_height)
        style.configure("StatsV2.Treeview.Heading", font=self.stats_font)

        self.main_frame = ttk.Frame(self.window, padding=6)
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        self.top_toolbar = ttk.Frame(self.main_frame)
        self.top_toolbar.pack(fill=tk.X, pady=(0, 6))

        self.notes_button = ttk.Button(self.top_toolbar, text="Notes", command=self.open_notes_file)
        self.notes_button.pack(side=tk.RIGHT)
        ttk.Button(self.top_toolbar, text="Open Puzzle", command=self.open_puzzle_dialog).pack(side=tk.RIGHT, padx=(0, 4))
        ttk.Label(self.top_toolbar, text="Decimals:").pack(side=tk.LEFT)
        self.decimals_var = tk.StringVar(value=str(self.manager.score_decimals))
        self.decimals_spinbox = tk.Spinbox(
            self.top_toolbar,
            from_=0,
            to=6,
            width=4,
            textvariable=self.decimals_var,
        )
        self.decimals_spinbox.pack(side=tk.LEFT, padx=(4, 10))

        self.vertical_selection_var = tk.StringVar(value="Main: none")
        ttk.Label(self.top_toolbar, textvariable=self.vertical_selection_var).pack(side=tk.LEFT, padx=(0, 12))

        self.fin_selection_var = tk.StringVar(value="Finalization: none")
        ttk.Label(self.top_toolbar, textvariable=self.fin_selection_var).pack(side=tk.LEFT)

        self.vertical_section = ttk.LabelFrame(self.main_frame, text="Main")
        self.vertical_section.pack(fill=tk.BOTH, expand=True)

        self.vertical_toolbar = ttk.Frame(self.vertical_section)
        self.vertical_toolbar.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Button(self.vertical_toolbar, text="Add Row", width=10, command=self.add_vertical_row).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.vertical_toolbar, text="Delete Row", width=10, command=self.delete_vertical_row).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.vertical_toolbar, text="To Finalization", width=14, command=self.move_vertical_to_fin).pack(side=tk.LEFT)

        self.vertical_tree_frame = ttk.Frame(self.vertical_section)
        self.vertical_tree_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self.vertical_vsb = ttk.Scrollbar(self.vertical_tree_frame, orient=tk.VERTICAL)
        self.vertical_hsb = ttk.Scrollbar(self.vertical_tree_frame, orient=tk.HORIZONTAL)
        self.vertical_tree = ttk.Treeview(
            self.vertical_tree_frame,
            show="headings",
            selectmode="none",
            yscrollcommand=self._on_main_yscroll,
            xscrollcommand=self._on_main_xscroll,
            style="StatsV2.Treeview",
        )
        self.vertical_vsb.config(command=self._on_main_yview)
        self.vertical_hsb.config(command=self._on_main_xview)
        self.vertical_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.vertical_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.vertical_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vertical_tree.bind("<Button-1>", self._on_vertical_click)
        self.vertical_tree.bind("<Double-1>", self._begin_vertical_edit)
        self.vertical_tree.bind("<Configure>", lambda _e: self._update_main_selection_border())
        self.vertical_tree.bind("<MouseWheel>", self._on_main_scroll_input)
        self.vertical_tree.bind("<Button-4>", self._on_main_scroll_input)
        self.vertical_tree.bind("<Button-5>", self._on_main_scroll_input)

        for _ in range(4):
            line = tk.Frame(self.vertical_tree, bg="#2F77D4", bd=0, highlightthickness=0)
            line.place_forget()
            self._main_selection_border.append(line)

        self.fin_section = ttk.LabelFrame(self.main_frame, text="Finalization")

        self.fin_toolbar = ttk.Frame(self.fin_section)
        self.fin_toolbar.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Button(self.fin_toolbar, text="To Main", width=10, command=self.move_fin_cell_to_vertical).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.fin_toolbar, text="Row To Main", width=12, command=self.move_fin_row_to_vertical).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.fin_toolbar, text="New Row", width=9, command=self.add_fin_row).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.fin_toolbar, text="Delete Cell", width=10, command=self.delete_fin_cell).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.fin_toolbar, text="Delete Row", width=10, command=self.delete_fin_row).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.fin_toolbar, text="Move Up", width=9, command=lambda: self.move_fin_row(-1)).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.fin_toolbar, text="Move Down", width=10, command=lambda: self.move_fin_row(1)).pack(side=tk.LEFT)

        self.fin_tree_frame = ttk.Frame(self.fin_section)
        self.fin_tree_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self.fin_vsb = ttk.Scrollbar(self.fin_tree_frame, orient=tk.VERTICAL)
        self.fin_hsb = ttk.Scrollbar(self.fin_tree_frame, orient=tk.HORIZONTAL)
        self.fin_tree = ttk.Treeview(
            self.fin_tree_frame,
            show="headings",
            selectmode="browse",
            yscrollcommand=self.fin_vsb.set,
            xscrollcommand=self.fin_hsb.set,
            style="StatsV2.Treeview",
        )
        self.fin_vsb.config(command=self.fin_tree.yview)
        self.fin_hsb.config(command=self.fin_tree.xview)
        self.fin_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.fin_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.fin_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.fin_tree.bind("<Button-1>", self._on_fin_click)
        self.fin_tree.bind("<Double-1>", self._begin_fin_edit)
        self._bind_copy_shortcuts()
        self.fin_tree.tag_configure("active_fin_row", background="#E7F4EA")
        self.fin_tree.tag_configure("active_fin_row_idle", background="#E7F4EA", foreground="#6F7782")

        self.bottom_frame = ttk.Frame(self.main_frame)
        self.bottom_frame.pack(fill=tk.X, pady=(6, 0))
        self.action_frame = ttk.Frame(self.bottom_frame)
        self.action_frame.pack(side=tk.RIGHT)
        ttk.Button(self.action_frame, text="Save", width=8, command=self.save).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.action_frame, text="Close", width=8, command=self.request_close).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(self.action_frame, text="Cancel", width=8, command=self.cancel).pack(side=tk.LEFT)

        self._load_working_data()
        self._refresh_all(preserve_selection=False)

        StatsWindowV2._instance = self
        self._ui_bridge = StatsUiBridge(
            sync_from_ui=self._sync_manager_state_if_clean,
            push_to_ui=self._reload_from_manager_if_clean,
        )
        self.manager.register_ui_bridge(self._ui_bridge)
        self._refresh_notes_button()
        self.window.after_idle(self.focus_window)

    def set_log_lookup_handler(self, log_lookup_handler: Optional[Callable[[Dict[str, Any]], Any]] = None):
        self.log_lookup_handler = log_lookup_handler

    def _bind_copy_shortcuts(self):
        shortcuts = ("<Control-c>", "<Control-C>", "<Control-Insert>", "<Command-c>", "<Command-C>")
        for sequence in shortcuts:
            self.window.bind(sequence, self.copy_selection, add="+")
            self.vertical_tree.bind(sequence, self.copy_selection, add="+")
            self.fin_tree.bind(sequence, self.copy_selection, add="+")

    def _copy_to_clipboard(self, text: str):
        self.window.clipboard_clear()
        self.window.clipboard_append(text)

    def copy_selection(self, _event=None):
        text = ""

        if self._editor is not None:
            try:
                text = self._editor.selection_get()
            except tk.TclError:
                text = self._editor.get().strip()
        else:
            focus = self.window.focus_get()
            if focus == self.fin_tree:
                if self.selected_fin_row_index is not None and self.selected_fin_column_key is not None:
                    text = self._fin_value(self.selected_fin_row_index, self.selected_fin_column_key)
            else:
                selected_range = self._selected_main_range()
                if self.selected_client is not None and selected_range is not None:
                    start_idx, end_idx = selected_range
                    lines: List[str] = []
                    for row_idx in range(start_idx, end_idx + 1):
                        script = self._vertical_value(self.selected_client, row_idx, "script")
                        score = self._vertical_value(self.selected_client, row_idx, "score")
                        lines.append(f"{script}\t{score}")
                    text = "\n".join(lines)

        if text:
            self._copy_to_clipboard(text)
        return "break"

    def _get_saved_position(self) -> Optional[tuple]:
        display = self.settings.get("display", {})
        stats_pos = display.get("stats_window_position", {})
        x = stats_pos.get("x", -1)
        y = stats_pos.get("y", -1)
        if isinstance(x, int) and isinstance(y, int) and x >= 0 and y >= 0:
            return x, y
        return None

    def _apply_initial_position(self, parent: tk.Misc):
        saved = self._get_saved_position()
        if saved:
            x, y = saved
        else:
            x = parent.winfo_rootx() + 30
            y = max(0, parent.winfo_rooty() - 40)
        self.window.geometry(f"+{x}+{y}")

    def _save_window_position(self):
        x = self.window.winfo_x()
        y = self.window.winfo_y()
        if hasattr(self.settings_source, "save_stats_window_position"):
            try:
                self.settings_source.save_stats_window_position(int(x), int(y))
            except Exception:
                pass
        else:
            display = self.settings.setdefault("display", {})
            display["stats_window_position"] = {"x": int(x), "y": int(y)}

    def _on_window_configure(self, event):
        if event.widget != self.window:
            return

        self._is_window_interacting = True
        if self._window_configure_job is not None:
            try:
                self.window.after_cancel(self._window_configure_job)
            except tk.TclError:
                pass
        self._window_configure_job = self.window.after(180, self._finish_window_interaction)

    def _finish_window_interaction(self):
        self._window_configure_job = None
        self._is_window_interacting = False
        self._apply_pending_manager_reload()
        self._update_main_selection_border()

    def _begin_main_scroll_interaction(self):
        self._is_main_scroll_interacting = True
        self._hide_main_selection_border()
        self._hide_main_status_overlays()
        self._hide_main_separator_overlays()
        if self._main_scroll_job is not None:
            try:
                self.window.after_cancel(self._main_scroll_job)
            except tk.TclError:
                pass
        self._main_scroll_job = self.window.after(120, self._finish_main_scroll_interaction)

    def _finish_main_scroll_interaction(self):
        self._main_scroll_job = None
        self._is_main_scroll_interacting = False
        self._update_main_selection_border()

    def _on_main_scroll_input(self, _event=None):
        self._begin_main_scroll_interaction()

    def _restore_not_topmost(self):
        if self.window.winfo_exists():
            try:
                self.window.attributes("-topmost", False)
            except tk.TclError:
                pass

    def focus_window(self):
        if not self.window.winfo_exists():
            return
        self.window.deiconify()
        self.window.lift()
        try:
            self.window.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.window.focus_force()
        self.window.after_idle(self._restore_not_topmost)

    def _show_info(self, title: str, text: str):
        messagebox.showinfo(title, text)

    def _show_error(self, title: str, text: str):
        messagebox.showerror(title, text)

    def _ask_yes_no(self, title: str, text: str) -> bool:
        return bool(messagebox.askyesno(title, text))

    def _ask_yes_no_cancel(self, title: str, text: str) -> Optional[bool]:
        return messagebox.askyesnocancel(title, text)

    def _flush_active_editor(self) -> bool:
        return self._commit_active_editor()

    def _refresh_stats_view(
        self,
        preserve_selection: bool = True,
        main_view_mode: str = "selection",
        fin_view_mode: str = "selection",
    ):
        self._refresh_all(preserve_selection=preserve_selection)
        if fin_view_mode == "bottom" and self.fin_rows:
            self.fin_tree.see(f"f{len(self.fin_rows) - 1}")

    def _parse_decimals_value(self) -> int:
        return int(self.decimals_var.get())

    def _get_decimals_text(self) -> str:
        return self.decimals_var.get()

    def _set_decimals_value(self, value: int):
        self.decimals_var.set(str(int(value)))

    def _set_window_puzzle(self, puzzle_id: str):
        self.session.puzzle_id = str(puzzle_id).strip()
        self.window.title(f"Stats - {self.puzzle_id}")
        self._refresh_notes_button()

    def _refresh_notes_button(self):
        if not hasattr(self, "notes_button"):
            return
        label = "Notes*" if self.manager.has_notes_file(self.puzzle_id) else "Notes"
        self.notes_button.configure(text=label)

    def open_notes_file(self):
        note_path = self.manager.ensure_notes_file(self.puzzle_id)
        if not note_path:
            messagebox.showerror("Notes", "Failed to resolve notes file path.")
            return

        self._refresh_notes_button()
        try:
            open_path(note_path)
        except Exception as exc:
            messagebox.showerror("Notes", f"Failed to open notes file:\n{exc}")

    def _close_window(self):
        if self._editor:
            self._editor.destroy()
            self._editor = None
        self._editor_commit = None
        if self._window_configure_job is not None:
            try:
                self.window.after_cancel(self._window_configure_job)
            except tk.TclError:
                pass
            self._window_configure_job = None
        if self._main_scroll_job is not None:
            try:
                self.window.after_cancel(self._main_scroll_job)
            except tk.TclError:
                pass
            self._main_scroll_job = None
        self._save_window_position()
        self.manager.unregister_ui_bridge(self._ui_bridge)
        self._ui_bridge = None
        StatsWindowV2._instance = None
        self.window.destroy()

    def _sync_manager_state_if_clean(self, puzzle_id: str):
        if str(self.puzzle_id).strip() != str(puzzle_id).strip():
            return

        if self._editor is not None or self._is_window_interacting or self.session.ui_dirty:
            return

        self._merge_pending_manager_reload()

    def _reload_from_manager_if_clean(self, puzzle_id: str):
        if str(self.puzzle_id).strip() != str(puzzle_id).strip():
            return

        if self._editor is not None or self.session.ui_dirty:
            self._pending_manager_reload = True
            return

        if self._is_window_interacting:
            self._pending_manager_reload = True
            return

        self.reload_from_manager(preserve_dirty=True)
        self._refresh_all()
        self._pending_manager_reload = False

    def _merge_pending_manager_reload(self):
        if not self._pending_manager_reload:
            return

        if self.session.ui_dirty:
            return

        self.reload_from_manager(preserve_dirty=True)
        self._pending_manager_reload = False

    def _apply_pending_manager_reload(self):
        if self._editor is not None or not self._pending_manager_reload or self._is_window_interacting or self.session.ui_dirty:
            return

        self.reload_from_manager(preserve_dirty=True)
        self._pending_manager_reload = False
        self._refresh_all()

    def _load_working_data(self):
        self.session.reload_from_manager(preserve_dirty=False)

    def _discard_unsaved_changes(self, reload_ui: bool = True):
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
        self._editor_commit = None
        self._pending_manager_reload = False
        self.session.discard_unsaved_changes()
        self.decimals_var.set(str(self.manager.score_decimals))
        if reload_ui:
            self._refresh_all(preserve_selection=False)

    def reload_from_manager(self, preserve_dirty: bool = True):
        self.session.reload_from_manager(preserve_dirty=preserve_dirty)

    def _fin_row_tags(self, row_idx: int) -> tuple[str, ...]:
        if not self._is_active_fin_row(row_idx):
            return ()
        client_name = str(self.fin_rows[row_idx].get("client", "")).strip()
        if self._client_is_idle(client_name):
            return ("active_fin_row_idle",)
        return ("active_fin_row",)

    def _refresh_all(self, preserve_selection: bool = True):
        if not preserve_selection:
            self.selected_client = None
            self._clear_main_selection()
            self._clear_fin_selection()

        clients = set(self.working_entries.keys())
        clients.update(str(row.get("client", "")).strip() for row in self.fin_rows if str(row.get("client", "")).strip())
        clients.update(self.active_targets.keys())
        for client_name in clients:
            if not client_name:
                continue
            self.working_entries.setdefault(client_name, [])
            self.active_targets.setdefault(client_name, "vertical")
        self.clients = sorted(
            (
                client_name
                for client_name in clients
                if client_name and self.working_entries.get(client_name)
            ),
            key=natural_sort_key,
        )

        if self.selected_client not in self.clients:
            self.selected_client = None
            self._clear_main_selection()
        elif self.selected_row_index is not None:
            client_entries = self.working_entries.get(self.selected_client, [])
            if client_entries:
                max_idx = len(client_entries) - 1
                self.selected_row_index = min(self.selected_row_index, max_idx)
                end_idx = self.selected_row_end_index if self.selected_row_end_index is not None else self.selected_row_index
                self.selected_row_end_index = min(end_idx, max_idx)
            else:
                self._clear_main_selection()

        if self.selected_fin_row_index is not None and self.selected_fin_row_index >= len(self.fin_rows):
            self._clear_fin_selection()

        self._rebuild_vertical_tree()
        self._rebuild_fin_tree()
        self._update_selection_labels()
        self._update_main_selection_border()
        self._adjust_window_size()

    def _update_selection_labels(self):
        if self.selected_client is None or self.selected_row_index is None:
            self.vertical_selection_var.set("Main: none")
        else:
            field = self.selected_vertical_field or "script"
            end_idx = self.selected_row_end_index if self.selected_row_end_index is not None else self.selected_row_index
            start_idx = min(self.selected_row_index, end_idx)
            end_idx = max(self.selected_row_index, end_idx)
            if start_idx == end_idx:
                text = f"Main: {self.selected_client} row {start_idx + 1} {field}"
            else:
                text = f"Main: {self.selected_client} rows {start_idx + 1}-{end_idx + 1}"
            self.vertical_selection_var.set(text)

        if self.selected_fin_row_index is None or self.selected_fin_row_index >= len(self.fin_rows):
            self.fin_selection_var.set("Finalization: none")
        else:
            row = self.fin_rows[self.selected_fin_row_index]
            label = self._fin_column_label(self.selected_fin_column_key)
            self.fin_selection_var.set(
                f"Finalization: row {self.selected_fin_row_index + 1} {row.get('client', '')} {label}"
            )

    def _row_count(self) -> int:
        if not self.clients:
            return 1
        max_rows = 0
        for client_name in self.clients:
            max_rows = max(max_rows, len(self.working_entries.get(client_name, [])))
        return max(1, max_rows)

    def _build_vertical_layout(self):
        self.vertical_column_specs = []
        self.vertical_column_specs_by_name = {}
        self.vertical_client_columns = {}
        for idx, client_name in enumerate(self.clients):
            script_col = f"script_{idx}"
            score_col = f"score_{idx}"
            self.vertical_column_specs.append({"name": script_col, "type": "script", "client": client_name})
            self.vertical_column_specs.append({"name": score_col, "type": "score", "client": client_name})
            self.vertical_column_specs_by_name[script_col] = self.vertical_column_specs[-2]
            self.vertical_column_specs_by_name[score_col] = self.vertical_column_specs[-1]
            self.vertical_client_columns[client_name] = {"script": script_col, "score": score_col}

    def _build_fin_layout(self):
        self.fin_column_specs = [
            {"name": "client", "type": "client"},
            {"name": "start_from", "type": "start_from"},
            {"name": "state", "type": "state"},
            {"name": "notes", "type": "notes"},
            {"name": "start_score", "type": "start_score"},
        ]
        for column in self.fin_columns:
            spec = {
                "name": str(column.get("key", "")),
                "type": "score",
                "label": str(column.get("label", "")).strip(),
            }
            self.fin_column_specs.append(spec)
        self.fin_column_specs_by_name = {spec["name"]: spec for spec in self.fin_column_specs}

    def _vertical_value(self, client_name: str, row_idx: int, value_type: str) -> str:
        entries = self.working_entries.get(client_name, [])
        if row_idx >= len(entries):
            return ""
        entry = entries[row_idx]
        if value_type == "script":
            return str(entry.get("script", "")).strip()
        return format_score(entry.get("score", ""), self.manager.score_decimals)

    def _fin_value(self, row_idx: int, column_name: str) -> str:
        if row_idx >= len(self.fin_rows):
            return ""
        row = self.fin_rows[row_idx]
        if column_name == "client":
            return display_fin_client_name(row.get("client", ""))
        if column_name == "state":
            return str(row.get("state", "")).strip()
        if column_name == "notes":
            return str(row.get("notes", "")).strip()
        if column_name == "start_from":
            start_from = str(row.get("start_from", "")).strip()
            return display_fin_client_name(start_from)
        if column_name == "start_score":
            return format_score(row.get("start_score", ""), self.manager.score_decimals)
        return format_score(row.get("cells", {}).get(column_name, ""), self.manager.score_decimals)

    def _adjust_vertical_columns(self):
        for spec in self.vertical_column_specs:
            col_name = spec["name"]
            col_type = spec["type"]

            values = [self.vertical_tree.heading(col_name).get("text", "")]
            col_index = list(self.vertical_tree["columns"]).index(col_name)
            for item_id in self.vertical_tree.get_children():
                item_values = list(self.vertical_tree.item(item_id, "values"))
                if col_index < len(item_values):
                    values.append(str(item_values[col_index]))

            width = max(self.stats_font.measure(text) for text in values) + 18
            if col_type == "script":
                self.vertical_tree.column(col_name, width=min(max(width, 100), 260), minwidth=90, stretch=False, anchor="w")
            else:
                self.vertical_tree.column(col_name, width=min(max(width, 70), 120), minwidth=60, stretch=False, anchor="w")

    def _adjust_fin_columns(self):
        for spec in self.fin_column_specs:
            col_name = spec["name"]
            values = [self.fin_tree.heading(col_name).get("text", "")]
            col_index = list(self.fin_tree["columns"]).index(col_name)
            for item_id in self.fin_tree.get_children():
                item_values = list(self.fin_tree.item(item_id, "values"))
                if col_index < len(item_values):
                    values.append(str(item_values[col_index]))

            width = max(self.stats_font.measure(text) for text in values) + 18
            if col_name == "client":
                self.fin_tree.column(col_name, width=min(max(width, 90), 140), minwidth=80, stretch=False, anchor="center")
            elif col_name == "state":
                self.fin_tree.column(col_name, width=min(max(width, 90), 180), minwidth=80, stretch=False, anchor="center")
            elif col_name == "notes":
                self.fin_tree.column(col_name, width=min(max(width, 120), 260), minwidth=100, stretch=False, anchor="w")
            elif col_name == "start_from":
                self.fin_tree.column(col_name, width=min(max(width, 70), 180), minwidth=70, stretch=False, anchor="center")
            elif col_name == "start_score":
                self.fin_tree.column(col_name, width=min(max(width, 72), 110), minwidth=64, stretch=False, anchor="e")
            else:
                self.fin_tree.column(col_name, width=min(max(width, 72), 110), minwidth=64, stretch=False, anchor="e")

    @staticmethod
    def _tree_required_width(tree: ttk.Treeview, vsb: ttk.Scrollbar) -> int:
        width = 24 + vsb.winfo_reqwidth()
        for column in tree["columns"]:
            width += int(tree.column(column, "width"))
        return width

    def _adjust_window_size(self):
        self.window.minsize(760, 420)

        current_width = max(1, int(self.window.winfo_width()))
        current_height = max(1, int(self.window.winfo_height()))
        if self._window_size_initialized and current_width > 1 and current_height > 1:
            return

        self.window.update_idletasks()

        req_width = max(
            self.top_toolbar.winfo_reqwidth(),
            self.bottom_frame.winfo_reqwidth(),
            self.vertical_toolbar.winfo_reqwidth(),
            self._tree_required_width(self.vertical_tree, self.vertical_vsb),
        ) + 24
        if self.fin_rows:
            req_width = max(
                req_width,
                self.fin_toolbar.winfo_reqwidth() + 24,
                self._tree_required_width(self.fin_tree, self.fin_vsb) + 24,
            )

        req_height = self.main_frame.winfo_reqheight() + 24
        max_width = int(self.window.winfo_screenwidth() * 0.9)
        max_height = int(self.window.winfo_screenheight() * 0.9)
        width = min(max(req_width, 760), max_width)
        height = min(max(req_height, 420), max_height)
        x = self.window.winfo_x()
        y = self.window.winfo_y()
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self._window_size_initialized = True

    def _rebuild_vertical_tree(self):
        self._build_vertical_layout()
        columns = [spec["name"] for spec in self.vertical_column_specs]
        self.vertical_tree.configure(columns=columns, height=min(max(5, self._row_count()), 22))
        for spec in self.vertical_column_specs:
            heading = ""
            if spec["type"] == "script":
                heading = str(spec["client"])
            elif spec["type"] == "score":
                heading = "score"
            self.vertical_tree.heading(spec["name"], text=heading)
            self.vertical_tree.column(spec["name"], width=100, minwidth=50, stretch=False)

        for item_id in self.vertical_tree.get_children():
            self.vertical_tree.delete(item_id)

        for row_idx in range(self._row_count()):
            values: List[str] = []
            for spec in self.vertical_column_specs:
                values.append(self._vertical_value(spec["client"], row_idx, spec["type"]))
            self.vertical_tree.insert("", tk.END, iid=f"v{row_idx}", values=values)

        self._adjust_vertical_columns()

    def _rebuild_fin_tree(self):
        visible = bool(self.fin_rows)
        if visible and not self.fin_section.winfo_ismapped():
            self.fin_section.pack(fill=tk.BOTH, expand=True, pady=(6, 0), before=self.bottom_frame)
        elif not visible and self.fin_section.winfo_ismapped():
            self.fin_section.pack_forget()

        self._build_fin_layout()
        columns = [spec["name"] for spec in self.fin_column_specs]
        self.fin_tree.configure(columns=columns, height=min(max(3, len(self.fin_rows) or 1), 22))
        for spec in self.fin_column_specs:
            heading = spec.get("label", spec["name"])
            if spec["name"] == "client":
                heading = "client"
            elif spec["name"] == "start_from":
                heading = "from"
            elif spec["name"] == "state":
                heading = "state"
            elif spec["name"] == "notes":
                heading = "Notes"
            elif spec["name"] == "start_score":
                heading = "score"
            self.fin_tree.heading(spec["name"], text=heading)
            self.fin_tree.column(spec["name"], width=100, minwidth=50, stretch=False)

        for item_id in self.fin_tree.get_children():
            self.fin_tree.delete(item_id)

        for row_idx in range(len(self.fin_rows)):
            values = [self._fin_value(row_idx, spec["name"]) for spec in self.fin_column_specs]
            self.fin_tree.insert("", tk.END, iid=f"f{row_idx}", values=values, tags=self._fin_row_tags(row_idx))

        if self.fin_rows:
            self._adjust_fin_columns()
            if self.selected_fin_row_index is not None and self.selected_fin_row_index < len(self.fin_rows):
                self.fin_tree.selection_set(f"f{self.selected_fin_row_index}")
            self.fin_tree.see(f"f{len(self.fin_rows) - 1}")

    @staticmethod
    def _spec_from_column(
        tree: ttk.Treeview,
        specs_by_name: Dict[str, Dict[str, Any]],
        column_id: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            idx = int(column_id[1:]) - 1
        except ValueError:
            return None
        columns = list(tree["columns"])
        if idx < 0 or idx >= len(columns):
            return None
        return specs_by_name.get(columns[idx])

    def _vertical_spec_from_column(self, column_id: str) -> Optional[Dict[str, Any]]:
        return self._spec_from_column(self.vertical_tree, self.vertical_column_specs_by_name, column_id)

    def _fin_spec_from_column(self, column_id: str) -> Optional[Dict[str, Any]]:
        return self._spec_from_column(self.fin_tree, self.fin_column_specs_by_name, column_id)

    @staticmethod
    def _row_index_from_iid(item_id: str) -> Optional[int]:
        try:
            return int(item_id[1:])
        except (TypeError, ValueError):
            return None

    def _fin_column_label(self, key: Optional[str]) -> str:
        if not key:
            return ""
        if key == "client":
            return "client"
        if key == "start_from":
            return "from"
        if key == "state":
            return "state"
        if key == "notes":
            return "Notes"
        if key == "start_score":
            return "score"
        for column in self.fin_columns:
            if column.get("key") == key:
                return str(column.get("label", "")).strip() or key
        return str(key)

    def _hide_main_selection_border(self):
        for line in self._main_selection_border:
            line.place_forget()

    def _ensure_main_status_overlay(self, client_name: str) -> Dict[str, tk.Frame]:
        overlays = self._main_status_overlays.get(client_name)
        if overlays is None:
            overlays = {
                "target": tk.Frame(self.vertical_tree, bg="#BFDCC8", bd=0, highlightthickness=0),
                "idle": tk.Frame(self.vertical_tree, bg="#D9DDE2", bd=0, highlightthickness=0),
            }
            for frame in overlays.values():
                frame.place_forget()
            self._main_status_overlays[client_name] = overlays
        return overlays

    def _hide_main_status_overlays(self):
        for overlays in self._main_status_overlays.values():
            for frame in overlays.values():
                frame.place_forget()

    def _hide_main_separator_overlays(self):
        for frame in self._main_separator_overlays:
            frame.place_forget()

    @staticmethod
    def _place_overlay(frame: tk.Frame, x: int, y: int, width: int, height: int):
        frame.place_configure(x=x, y=y, width=width, height=height)
        frame.lift()

    def _main_client_overlay_bbox(self, client_name: str) -> Optional[tuple[int, int, int, int]]:
        columns = self.vertical_client_columns.get(client_name)
        if columns is None:
            return None

        script_col = columns["script"]
        score_col = columns["score"]
        top = None
        bottom = None
        left = None
        right = None

        for item_id in self.vertical_tree.get_children():
            script_bbox = self.vertical_tree.bbox(item_id, script_col)
            score_bbox = self.vertical_tree.bbox(item_id, score_col)
            if not script_bbox or not score_bbox:
                continue
            row_left = min(script_bbox[0], score_bbox[0])
            row_top = min(script_bbox[1], score_bbox[1])
            row_right = max(script_bbox[0] + script_bbox[2], score_bbox[0] + score_bbox[2])
            row_bottom = max(script_bbox[1] + script_bbox[3], score_bbox[1] + score_bbox[3])
            if top is None:
                top = row_top
                left = row_left
                right = row_right
            bottom = row_bottom

        if top is None or bottom is None or left is None or right is None:
            return None
        return left, top, right - left, bottom - top

    def _ensure_main_separator_overlays(self, count: int):
        while len(self._main_separator_overlays) < count:
            frame = tk.Frame(self.vertical_tree, bg="#5F6872", bd=0, highlightthickness=0)
            frame.place_forget()
            self._main_separator_overlays.append(frame)
        while len(self._main_separator_overlays) > count:
            frame = self._main_separator_overlays.pop()
            frame.destroy()

    def _update_main_separator_overlays(self):
        separator_count = max(0, len(self.clients) - 1)
        self._ensure_main_separator_overlays(separator_count)
        if separator_count == 0:
            return

        thickness = 1
        for idx in range(separator_count):
            left_client = self.clients[idx]
            right_client = self.clients[idx + 1]
            left_bbox = self._main_client_overlay_bbox(left_client)
            right_bbox = self._main_client_overlay_bbox(right_client)
            frame = self._main_separator_overlays[idx]

            if left_bbox is None or right_bbox is None:
                frame.place_forget()
                continue

            left_x, left_y, left_width, left_height = left_bbox
            right_x, right_y, right_width, right_height = right_bbox
            x = max(left_x + left_width - thickness, right_x - thickness)
            y = 0
            bottom = max(left_y + left_height, right_y + right_height)
            height = bottom - y
            if height <= 0:
                frame.place_forget()
                continue

            self._place_overlay(frame, x=x, y=y, width=thickness, height=height)

    def _update_main_status_overlays(self):
        thickness = 1
        visible_pairs = set()

        for client_name in self.clients:
            bbox = self._main_client_overlay_bbox(client_name)
            if bbox is None:
                continue

            x, y, width, height = bbox
            if width <= 0 or height <= 0:
                continue

            overlays = self._ensure_main_status_overlay(client_name)
            if self._main_client_is_target(client_name):
                self._place_overlay(overlays["target"], x=x, y=y, width=width, height=thickness)
                visible_pairs.add((client_name, "target"))
            else:
                overlays["target"].place_forget()
            if self._client_is_idle(client_name):
                self._place_overlay(overlays["idle"], x=x, y=y + height - thickness, width=width, height=thickness)
                visible_pairs.add((client_name, "idle"))
            else:
                overlays["idle"].place_forget()

        for client_name, overlays in self._main_status_overlays.items():
            for overlay_name, frame in overlays.items():
                if (client_name, overlay_name) not in visible_pairs:
                    frame.place_forget()

    def _update_main_selection_border(self):
        if self._is_window_interacting or self._is_main_scroll_interacting:
            self._hide_main_selection_border()
            self._hide_main_status_overlays()
            self._hide_main_separator_overlays()
            return

        self._update_main_separator_overlays()
        self._update_main_status_overlays()
        selected_range = self._selected_main_range()
        if selected_range is None or self.selected_client not in self.vertical_client_columns:
            self._hide_main_selection_border()
            return

        start_idx, end_idx = selected_range
        start_row_id = f"v{start_idx}"
        end_row_id = f"v{end_idx}"
        if start_row_id not in self.vertical_tree.get_children() or end_row_id not in self.vertical_tree.get_children():
            self._hide_main_selection_border()
            return

        script_col = self.vertical_client_columns[self.selected_client]["script"]
        score_col = self.vertical_client_columns[self.selected_client]["score"]
        top_script_bbox = self.vertical_tree.bbox(start_row_id, script_col)
        top_score_bbox = self.vertical_tree.bbox(start_row_id, score_col)
        bottom_script_bbox = self.vertical_tree.bbox(end_row_id, script_col)
        bottom_score_bbox = self.vertical_tree.bbox(end_row_id, score_col)
        if not top_script_bbox or not top_score_bbox or not bottom_script_bbox or not bottom_score_bbox:
            self._hide_main_selection_border()
            return

        x = min(top_script_bbox[0], top_score_bbox[0])
        y = min(top_script_bbox[1], top_score_bbox[1])
        width = max(top_script_bbox[0] + top_script_bbox[2], top_score_bbox[0] + top_score_bbox[2]) - x
        bottom = max(bottom_script_bbox[1] + bottom_script_bbox[3], bottom_score_bbox[1] + bottom_score_bbox[3])
        height = bottom - y
        if width <= 0 or height <= 0:
            self._hide_main_selection_border()
            return

        thickness = 1
        top, bottom_line, left, right = self._main_selection_border
        top.place(x=x, y=y, width=width, height=thickness)
        bottom_line.place(x=x, y=y + height - thickness, width=width, height=thickness)
        left.place(x=x, y=y, width=thickness, height=height)
        right.place(x=x + width - thickness, y=y, width=thickness, height=height)

    def _set_main_scrollbar(self, scrollbar: ttk.Scrollbar, *args):
        scrollbar.set(*args)
        self._begin_main_scroll_interaction()

    def _main_tree_view(self, axis: str, *args):
        self._begin_main_scroll_interaction()
        getattr(self.vertical_tree, axis)(*args)

    def _on_main_yscroll(self, *args):
        self._set_main_scrollbar(self.vertical_vsb, *args)

    def _on_main_xscroll(self, *args):
        self._set_main_scrollbar(self.vertical_hsb, *args)

    def _on_main_yview(self, *args):
        self._main_tree_view("yview", *args)

    def _on_main_xview(self, *args):
        self._main_tree_view("xview", *args)

    def _commit_active_editor(self) -> bool:
        if self._editor_commit is None:
            return True
        return self._editor_commit()

    def _start_editor(self, tree: ttk.Treeview, row_id: str, column_id: str, initial_text: str, commit_callback):
        if self._editor is not None:
            self._commit_active_editor()

        bbox = tree.bbox(row_id, column_id)
        if not bbox:
            return "break"

        x, y, width, height = bbox
        editor = tk.Entry(tree, font=self.stats_font)
        editor.insert(0, initial_text)
        editor.select_range(0, tk.END)
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        self._editor = editor

        done = {"value": False}

        def commit(_event=None):
            if done["value"]:
                return True
            done["value"] = True
            new_text = editor.get().strip()
            editor.destroy()
            self._editor = None
            self._editor_commit = None
            commit_callback(new_text)
            return True

        def cancel(_event=None):
            if done["value"]:
                return
            done["value"] = True
            editor.destroy()
            self._editor = None
            self._editor_commit = None
            self._apply_pending_manager_reload()

        editor.bind("<Return>", commit)
        editor.bind("<Escape>", cancel)
        editor.bind("<FocusOut>", commit)
        self._editor_commit = commit
        return "break"

    def _on_vertical_click(self, event):
        if self._editor is not None:
            self._commit_active_editor()
        self.vertical_tree.focus_set()
        row_id = self.vertical_tree.identify_row(event.y)
        column_id = self.vertical_tree.identify_column(event.x)
        spec = self._vertical_spec_from_column(column_id)
        row_idx = self._row_index_from_iid(row_id) if row_id else None
        if row_idx is None or spec is None or spec["type"] == "sep":
            return "break"
        shift_pressed = bool(event.state & 0x0001)
        if shift_pressed and self.selected_client == spec["client"] and self.selected_row_index is not None:
            self.selected_row_end_index = row_idx
        else:
            self.selected_client = spec["client"]
            self.selected_row_index = row_idx
            self.selected_row_end_index = row_idx
        self.selected_vertical_field = spec["type"]
        self._update_selection_labels()
        self._update_main_selection_border()
        return "break"

    def _on_fin_click(self, event):
        if self._editor is not None:
            self._commit_active_editor()
        self.fin_tree.focus_set()
        row_id = self.fin_tree.identify_row(event.y)
        column_id = self.fin_tree.identify_column(event.x)
        spec = self._fin_spec_from_column(column_id)
        row_idx = self._row_index_from_iid(row_id) if row_id else None
        if row_id:
            self.fin_tree.selection_set(row_id)
        if row_idx is None or spec is None:
            return "break"
        self.selected_fin_row_index = row_idx
        self.selected_fin_column_key = spec["name"]
        self._update_selection_labels()
        return "break"

    def _begin_vertical_edit(self, event):
        row_id = self.vertical_tree.identify_row(event.y)
        column_id = self.vertical_tree.identify_column(event.x)
        spec = self._vertical_spec_from_column(column_id)
        row_idx = self._row_index_from_iid(row_id) if row_id else None
        if row_idx is None or spec is None or spec["type"] == "sep":
            return "break"
        self.selected_client = spec["client"]
        self.selected_row_index = row_idx
        self.selected_row_end_index = row_idx
        self.selected_vertical_field = spec["type"]
        self._update_selection_labels()
        self._update_main_selection_border()

        current_text = self._vertical_value(spec["client"], row_idx, spec["type"])

        def commit_edit(new_text: str):
            self._handle_main_edit_value(spec["client"], row_idx, spec["type"], new_text)

        return self._start_editor(self.vertical_tree, row_id, column_id, current_text, commit_edit)

    def _begin_fin_edit(self, event):
        row_id = self.fin_tree.identify_row(event.y)
        column_id = self.fin_tree.identify_column(event.x)
        spec = self._fin_spec_from_column(column_id)
        row_idx = self._row_index_from_iid(row_id) if row_id else None
        if row_idx is None or spec is None:
            return "break"
        if spec["name"] == "client":
            return "break"

        self.selected_fin_row_index = row_idx
        self.selected_fin_column_key = spec["name"]
        self._update_selection_labels()

        if spec["name"] == "start_from":
            current_text = str(self.fin_rows[row_idx].get("start_from", "")).strip()
        else:
            current_text = self._fin_value(row_idx, spec["name"])

        def commit_edit(new_text: str):
            self._handle_fin_edit_value(row_idx, spec["name"], new_text)

        return self._start_editor(self.fin_tree, row_id, column_id, current_text, commit_edit)

    def _has_unsaved_changes(self) -> bool:
        return self.ui_dirty or self.decimals_var.get().strip() != str(self.manager.score_decimals)

    def open_puzzle_dialog(self):
        if not self._commit_active_editor():
            return

        puzzles = self.manager.get_logged_puzzles()
        if not puzzles:
            messagebox.showinfo("Open Puzzle", f"No puzzle CSV files found in:\n{self.manager.logs_folder}")
            return

        dialog = PuzzlePickerDialog(self.window, puzzles, self.puzzle_id)
        selected_puzzle_id = dialog.show()
        if selected_puzzle_id:
            self.open_puzzle(selected_puzzle_id)

    def request_close(self):
        if not self._confirm_close(save_prompt=True):
            return False
        self._close_window()
        return True

    def cancel(self):
        if not self._confirm_close(save_prompt=False):
            return False
        self._close_window()
        return True


def show_stats(
    parent: tk.Tk,
    stats_manager: StatsManager,
    settings_source: Any,
    puzzle_id: Optional[str] = None,
    log_lookup_handler: Optional[Callable[[Dict[str, Any]], Any]] = None,
):
    backend_class = _get_selected_stats_window_class(settings_source, show_error=True)
    selected_puzzle_id = str(puzzle_id or "").strip()
    puzzles = stats_manager.get_active_puzzles()
    if not puzzles and not selected_puzzle_id:
        messagebox.showinfo("Stats", "No active puzzles found.")
        return

    if not selected_puzzle_id:
        if len(puzzles) == 1:
            selected_puzzle_id = puzzles[0]
        else:
            messagebox.showinfo("Stats", "Select a puzzle from the Stats list in the main window.")
            return

    if not stats_manager.has_puzzle_log(selected_puzzle_id):
        messagebox.showinfo("Stats", f"Puzzle {selected_puzzle_id} was not found in puzzle logs.")
        return

    open_instance = get_open_stats_window()
    if open_instance is not None and not isinstance(open_instance, backend_class):
        if not open_instance.request_close():
            return
        open_instance = None

    if backend_class.focus_if_open(selected_puzzle_id):
        open_instance = backend_class.get_open_instance()
        if open_instance is not None and hasattr(open_instance, "set_log_lookup_handler"):
            open_instance.set_log_lookup_handler(log_lookup_handler)
        return

    open_instance = backend_class.get_open_instance()
    if open_instance is not None:
        open_instance.open_puzzle(selected_puzzle_id)
        if hasattr(open_instance, "set_log_lookup_handler"):
            open_instance.set_log_lookup_handler(log_lookup_handler)
        return

    backend_class(parent, stats_manager, selected_puzzle_id, settings_source, log_lookup_handler=log_lookup_handler)


def _normalize_stats_ui_backend_name(raw_value: Any) -> str:
    backend_name = str(raw_value).strip().lower()
    if backend_name in {"qt", "pyside", "pyside6"}:
        return "pyside6"
    return "tk"


def _resolve_stats_ui_backend(settings_source: Any) -> str:
    settings = resolve_settings_dict(settings_source)
    display_settings = settings.get("display", {})
    return _normalize_stats_ui_backend_name(display_settings.get("stats_ui_backend", "tk"))


def _load_qt_backend(show_error: bool = False):
    global _qt_backend_module, _qt_backend_import_error
    if _qt_backend_module is not None:
        return _qt_backend_module
    if _qt_backend_import_error is not None:
        if show_error:
            messagebox.showerror(
                "Stats UI",
                f"PySide6 stats UI is unavailable and Tk will be used instead.\n\n{_qt_backend_import_error}",
            )
        return None

    try:
        _qt_backend_module = importlib.import_module("stats_ui_qt")
        return _qt_backend_module
    except Exception as exc:
        _qt_backend_import_error = exc
        if show_error:
            messagebox.showerror(
                "Stats UI",
                f"PySide6 stats UI failed to load and Tk will be used instead.\n\n{exc}",
            )
        return None


def _get_selected_stats_window_class(settings_source: Any, show_error: bool = False):
    if _resolve_stats_ui_backend(settings_source) != "pyside6":
        return StatsWindowV2

    qt_backend = _load_qt_backend(show_error=show_error)
    if qt_backend is None:
        return StatsWindowV2
    return qt_backend.StatsWindowQt


def _iter_stats_window_classes() -> List[Any]:
    classes: List[Any] = [StatsWindowV2]
    qt_backend = _load_qt_backend(show_error=False)
    if qt_backend is not None:
        classes.insert(0, qt_backend.StatsWindowQt)
    return classes


def get_open_stats_window():
    for window_class in _iter_stats_window_classes():
        instance = window_class.get_open_instance()
        if instance is not None:
            return instance
    return None


def is_stats_window_user_interacting() -> bool:
    return any(window_class.is_user_interacting() for window_class in _iter_stats_window_classes())


def is_stats_window_open_for_puzzle(puzzle_id: str) -> bool:
    return any(window_class.is_open_for_puzzle(puzzle_id) for window_class in _iter_stats_window_classes())


def close_stats_window_if_exists(puzzle_id: Optional[str] = None):
    for window_class in _iter_stats_window_classes():
        if window_class.close_if_exists(puzzle_id):
            return True
    return False
