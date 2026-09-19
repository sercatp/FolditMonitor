"""Tk settings editor for the local Foldit Monitor installation."""

from copy import deepcopy
import re
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from settings import RESET_TO_DEFAULT
from settings_validation import SettingsValidationError


def _get(data, path, fallback=None):
    value = data
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return fallback
        value = value[part]
    return value


def _set(data, path, value):
    node = data
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = deepcopy(value)


def ask_fields(parent, title, fields, initial=None):
    """Small modal form. fields are (label, key) pairs."""
    initial = initial or {}
    previous_grab = parent.grab_current()
    top = tk.Toplevel(parent)
    top.title(title)
    top.transient(parent)
    top.resizable(False, False)
    body = ttk.Frame(top, padding=12)
    body.pack(fill="both", expand=True)
    variables = {}
    for row, (label, key) in enumerate(fields):
        ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(top, value=str(initial.get(key, "")))
        ttk.Entry(body, textvariable=var, width=48).grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=3)
        variables[key] = var
    result = []

    def accept():
        result.append({key: var.get() for key, var in variables.items()})
        top.destroy()

    buttons = ttk.Frame(body)
    buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(10, 0))
    ttk.Button(buttons, text="Cancel", command=top.destroy).pack(side="right")
    ttk.Button(buttons, text="OK", command=accept).pack(side="right", padx=6)
    top.bind("<Return>", lambda _event: accept())
    top.bind("<Escape>", lambda _event: top.destroy())
    top.grab_set()
    top.wait_window()
    if previous_grab is not None and previous_grab.winfo_exists():
        previous_grab.grab_set()
    return result[0] if result else None


class ScrollTab(ttk.Frame):
    def __init__(self, notebook):
        super().__init__(notebook)
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(canvas, padding=12)
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))


class SettingsDialog:
    LIVE_PATHS = {
        ("display", "active_palette"),
        ("display", "row_appearance"),
        ("display", "always_on_top"),
        ("sound", "volume"),
        ("speed_boost", "profile"),
    }

    def __init__(self, parent, manager, on_applied):
        self.manager = manager
        self.on_applied = on_applied
        self.defaults, self.baseline_user, self.initial_effective = manager.get_editor_snapshot()
        self.pending = deepcopy(self.initial_effective)
        self.reset_paths = set()
        self.controls = {}
        self.complex_paths = set()
        self.list_widgets = {}
        self.table_widgets = {}

        self.window = tk.Toplevel(parent)
        self.window.title("Foldit Monitor Settings")
        self.window.transient(parent)
        width = min(900, max(640, self.window.winfo_screenwidth() - 60))
        height = min(670, max(440, self.window.winfo_screenheight() - 100))
        left = max(0, (self.window.winfo_screenwidth() - width) // 2)
        top = max(0, (self.window.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{left}+{top}")
        self.window.minsize(min(730, width), min(510, height))
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

        outer = ttk.Frame(self.window, padding=10)
        outer.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Changes are saved only when you click Save.")
        ttk.Label(outer, textvariable=self.status, wraplength=840).pack(fill="x", pady=(0, 6))
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self.tabs = {}
        tab_names = ["Display", "Sound", "Network", "Logs", "Monitoring"]
        if _get(self.pending, ("speed_boost", "enabled")) is True:
            tab_names.append("Speed Boost")
        tab_names.extend(("Scripts", "State"))
        for name in tab_names:
            tab = ScrollTab(notebook)
            notebook.add(tab, text=name)
            self.tabs[name] = tab.body
        self._build_display()
        self._build_sound()
        self._build_network()
        self._build_logging()
        self._build_monitoring()
        if "Speed Boost" in self.tabs:
            self._build_speed()
        self._build_mapping()
        self._build_state()

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Button(footer, text="Close", command=self.window.destroy).pack(side="right")
        ttk.Button(footer, text="Save", command=self.save).pack(side="right", padx=8)
        self.window.focus_set()

    def _section(self, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.pack(fill="x", pady=(0, 10))
        frame.columnconfigure(1, weight=1)
        return frame

    def _scalar(self, frame, row, label, path, kind="str", *, choices=None, secret=False, browse=False):
        value = _get(self.pending, path)
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.BooleanVar(self.window, value=bool(value)) if kind == "bool" else tk.StringVar(self.window, value=str(value))
        if kind == "bool":
            widget = ttk.Checkbutton(frame, variable=var)
        elif choices:
            widget = ttk.Combobox(frame, textvariable=var, values=choices, state="readonly")
        else:
            widget = ttk.Entry(frame, textvariable=var, show="*" if secret else "")
        widget.grid(row=row, column=1, sticky="ew", padx=7, pady=3)
        if browse:
            ttk.Button(frame, text="Browse…", command=lambda: self._browse(var, browse)).grid(row=row, column=2, padx=3)
            reset_column = 3
        else:
            reset_column = 2
        ttk.Button(frame, text="Reset to Default", command=lambda: self._reset_scalar(path)).grid(row=row, column=reset_column, padx=3)
        self.controls[path] = (var, kind)

    def _browse(self, var, mode):
        if mode == "file":
            path = filedialog.askopenfilename(parent=self.window)
        else:
            path = filedialog.askdirectory(parent=self.window)
        if path:
            var.set(path)

    def _default(self, path, fallback=None):
        return deepcopy(_get(self.defaults, path, fallback))

    def _reset_scalar(self, path):
        var, kind = self.controls[path]
        value = self._default(path)
        var.set(bool(value) if kind == "bool" else str(value))
        self.reset_paths.add(path)

    def _value_from_control(self, path):
        var, kind = self.controls[path]
        raw = var.get()
        if kind == "bool":
            return bool(raw)
        if kind == "int":
            return int(raw)
        if kind == "float":
            return float(raw)
        return raw

    def _build_display(self):
        frame = self._section(self.tabs["Display"], "Main window appearance")
        fields = [
            ("Tooltip lines", ("display", "tooltip_lines"), "int"),
            ("Always on top", ("display", "always_on_top"), "bool"),
            ("Show puzzle column", ("display", "show_puzzle_column"), "bool"),
            ("Stale row threshold", ("display", "stale_tick_limit"), "int"),
            ("Unknown script type length", ("display", "script_type_fallback_max_length"), "int"),
        ]
        for row, (label, path, kind) in enumerate(fields):
            self._scalar(frame, row, label, path, kind)
        fonts = self._section(self.tabs["Display"], "Fonts and statistics window")
        for row, (label, path, kind) in enumerate([
            ("Font family", ("display", "fonts", "family"), "str"),
            ("Main font size", ("display", "fonts", "normal_size"), "int"),
            ("Tooltip font size", ("display", "fonts", "tooltip_size"), "int"),
            ("Statistics font size", ("display", "fonts", "stats_size"), "int"),
            ("Statistics UI backend", ("display", "stats_ui_backend"), "str"),
        ]):
            self._scalar(fonts, row, label, path, kind, choices=["pyside6", "tk"] if path[-1] == "stats_ui_backend" else None)
        self._build_palette(self.tabs["Display"])

    def _build_sound(self):
        frame = self._section(self.tabs["Sound"], "Alert")
        self._scalar(frame, 0, "Alert file", ("sound", "alert_file"), browse="file")
        self._scalar(frame, 1, "Volume (0–1)", ("sound", "volume"), "float")

    def _build_network(self):
        frame = self._section(self.tabs["Network"], "Connections and server")
        fields = [
            ("Default address", ("network", "default_address"), "str"),
            ("Port", ("network", "default_port"), "int"),
            ("Server timeout (s)", ("network", "server_timeout"), "int"),
            ("Password", ("network", "password"), "str"),
            ("Auto reconnect", ("network", "auto_reconnect"), "bool"),
            ("Maximum artifact size (bytes)", ("network", "max_artifact_bytes"), "int"),
            ("Transfer chunk size (bytes)", ("network", "artifact_chunk_bytes"), "int"),
        ]
        for row, (label, path, kind) in enumerate(fields):
            self._scalar(frame, row, label, path, kind, secret=path[-1] == "password")
        self._table_section(
            self.tabs["Network"], "Connect on startup", ("network", "startup_connections"),
            ("Address", "Port"), (("Address", "address"), ("Port", "port")),
            lambda row: {"address": row[0], "port": row[1]},
            lambda values: [values["address"], int(values["port"])],
        )

    def _build_logging(self):
        frame = self._section(self.tabs["Logs"], "Logs and statistics")
        fields = [
            ("Maximum lines", ("logging", "max_lines"), "int"),
            ("Line length", ("logging", "max_line_length"), "int"),
            ("Logs folder", ("logging", "logs_folder"), "str"),
            ("Statistics save interval (min)", ("logging", "stats_save_interval_minutes"), "int"),
            ("Decimal places", ("logging", "stats_score_decimals"), "int"),
            ("Managed log exports", ("logging", "managed_log_exports"), "bool"),
        ]
        for row, (label, path, kind) in enumerate(fields):
            self._scalar(frame, row, label, path, kind, browse="directory" if path[-1] == "logs_folder" else False)
        self._list_section(self.tabs["Logs"], "Strings excluded from score detection", ("logging", "exclude_score_strings"))
        self._list_section(self.tabs["Logs"], "Score regex patterns (in order)", ("logging", "score_patterns"), regex_test=True)

    def _build_monitoring(self):
        frame = self._section(self.tabs["Monitoring"], "Client polling and CPU")
        for row, (label, path) in enumerate([
            ("Poll interval (s)", ("check_interval",)),
            ("CPU history duration (s)", ("monitor_duration",)),
            ("High CPU threshold (%)", ("high_cpu_threshold",)),
            ("Low CPU threshold (%)", ("low_cpu_threshold",)),
        ]):
            self._scalar(frame, row, label, path, "float" if path[-1].endswith("cpu_threshold") else "int")
        self._table_section(
            self.tabs["Monitoring"], "Fallback Foldit log paths", ("monitoring", "log_fallbacks"),
            ("Path", "Name", "Suffix", "Puzzle"),
            (("Absolute path", "log_path"), ("Name", "name"), ("Title suffix", "title_suffix"), ("Puzzle ID", "puzzle_id")),
            lambda row: row,
            lambda values: values,
        )

    def _build_speed(self):
        frame = self._section(self.tabs["Speed Boost"], "General settings")
        self._scalar(frame, 0, "Active profile", ("speed_boost", "profile"), choices=list(self.manager.SPEED_BOOST_PROFILES))
        self._list_section(self.tabs["Speed Boost"], "game_library.dll offsets (hex)", ("speed_boost", "offsets"))
        self._build_profiles(self.tabs["Speed Boost"])

    def _build_state(self):
        frame = self._section(self.tabs["State"], "Recent values")
        self._scalar(frame, 0, "Last Foldit parent folder", ("launch", "last_seen_foldit_parent"), browse="directory")
        self._scalar(frame, 1, "Last puzzle", ("display", "stats_last_puzzle"))
        for row, prefix in ((2, ("display", "window_position")), (4, ("display", "stats_window_position"))):
            name = "Main window" if row == 2 else "Statistics window"
            self._scalar(frame, row, f"{name}: X", prefix + ("x",), "int")
            self._scalar(frame, row + 1, f"{name}: Y", prefix + ("y",), "int")
        ttk.Label(frame, text="The monitor normally updates window positions. Use Reset to Default to clear them.").grid(row=6, column=0, columnspan=4, sticky="w", pady=8)

    def _list_section(self, parent, title, path, *, regex_test=False):
        frame = self._section(parent, title)
        box = tk.Listbox(frame, height=5, exportselection=False)
        box.grid(row=0, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.list_widgets[path] = box
        self.complex_paths.add(path)

        def selected():
            indices = box.curselection()
            return indices[0] if indices else None

        def update():
            self.reset_paths.discard(path)
            self._refresh_list(path)

        def add():
            result = ask_fields(self.window, "Add", (("Value", "value"),))
            if result is not None:
                _get(self.pending, path).append(result["value"])
                update()

        def edit():
            index = selected()
            if index is None:
                return
            data = _get(self.pending, path)
            result = ask_fields(self.window, "Edit", (("Value", "value"),), {"value": data[index]})
            if result is not None:
                data[index] = result["value"]
                update()
                box.selection_set(index)

        def delete():
            index = selected()
            if index is not None:
                del _get(self.pending, path)[index]
                update()

        def move(delta):
            index = selected()
            data = _get(self.pending, path)
            if index is None or not 0 <= index + delta < len(data):
                return
            data[index], data[index + delta] = data[index + delta], data[index]
            update()
            box.selection_set(index + delta)

        for label, command in (("Add", add), ("Edit", edit), ("Delete", delete), ("Up", lambda: move(-1)), ("Down", lambda: move(1)), ("Reset to Default", lambda: self._reset_complex(path))):
            ttk.Button(buttons, text=label, command=command).pack(side="left", padx=(0, 4))
        if regex_test:
            sample = tk.StringVar(self.window)
            ttk.Entry(frame, textvariable=sample).grid(row=2, column=0, sticky="ew", pady=(7, 0))

            def test_pattern():
                index = selected()
                if index is None:
                    messagebox.showinfo("Pattern test", "Select a regex pattern.", parent=self.window)
                    return
                try:
                    match = re.search(_get(self.pending, path)[index], sample.get())
                except re.error as error:
                    messagebox.showerror("Invalid pattern", str(error), parent=self.window)
                    return
                messagebox.showinfo("Pattern test", f"Match: {match.group(0)}" if match else "No match", parent=self.window)

            ttk.Button(frame, text="Test on sample line", command=test_pattern).grid(row=2, column=1, padx=5, pady=(7, 0))
        self._refresh_list(path)

    def _refresh_list(self, path):
        box = self.list_widgets[path]
        box.delete(0, "end")
        for value in _get(self.pending, path, []):
            box.insert("end", str(value))

    def _table_section(self, parent, title, path, columns, fields, to_fields, from_fields):
        if _get(self.pending, path) is None:
            _set(self.pending, path, [])
        frame = self._section(parent, title)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=5, selectmode="browse")
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=140, stretch=True)
        tree.grid(row=0, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.table_widgets[path] = (tree, columns)
        self.complex_paths.add(path)

        def selected():
            ids = tree.selection()
            return int(ids[0]) if ids else None

        def add():
            result = ask_fields(self.window, "Add", fields)
            if result is None:
                return
            try:
                row = from_fields(result)
            except ValueError as error:
                messagebox.showerror("Invalid value", str(error), parent=self.window)
                return
            _get(self.pending, path).append(row)
            self.reset_paths.discard(path)
            self._refresh_table(path)

        def edit():
            index = selected()
            if index is None:
                return
            rows = _get(self.pending, path)
            result = ask_fields(self.window, "Edit", fields, to_fields(rows[index]))
            if result is None:
                return
            try:
                updated = from_fields(result)
            except ValueError as error:
                messagebox.showerror("Invalid value", str(error), parent=self.window)
                return
            if isinstance(rows[index], dict) and isinstance(updated, dict):
                preserved = deepcopy(rows[index])
                preserved.update(updated)
                updated = preserved
            rows[index] = updated
            self.reset_paths.discard(path)
            self._refresh_table(path)
            tree.selection_set(str(index))

        def delete():
            index = selected()
            if index is not None:
                del _get(self.pending, path)[index]
                self.reset_paths.discard(path)
                self._refresh_table(path)

        def move(delta):
            index = selected()
            rows = _get(self.pending, path)
            if index is None or not 0 <= index + delta < len(rows):
                return
            rows[index], rows[index + delta] = rows[index + delta], rows[index]
            self.reset_paths.discard(path)
            self._refresh_table(path)
            tree.selection_set(str(index + delta))

        for label, command in (("Add", add), ("Edit", edit), ("Delete", delete), ("Up", lambda: move(-1)), ("Down", lambda: move(1)), ("Reset to Default", lambda: self._reset_complex(path))):
            ttk.Button(buttons, text=label, command=command).pack(side="left", padx=(0, 4))
        tree.bind("<Double-1>", lambda _event: edit())
        self._refresh_table(path)

    def _refresh_table(self, path):
        tree, columns = self.table_widgets[path]
        tree.delete(*tree.get_children())
        for index, row in enumerate(_get(self.pending, path, [])):
            values = row if isinstance(row, (list, tuple)) else [row.get(key, "") for key in ("log_path", "name", "title_suffix", "puzzle_id")]
            tree.insert("", "end", iid=str(index), values=values[:len(columns)])

    def _reset_complex(self, path):
        fallback = [] if path == ("monitoring", "log_fallbacks") else None
        _set(self.pending, path, self._default(path, fallback))
        self.reset_paths.add(path)
        if path in self.list_widgets:
            self._refresh_list(path)
        if path in self.table_widgets:
            self._refresh_table(path)
        if path == ("script_type_mapping",):
            self._refresh_mapping()
        if path == ("speed_boost", "profiles"):
            self._refresh_profiles()

    def _build_profiles(self, parent):
        path = ("speed_boost", "profiles")
        frame = self._section(parent, "Profile timings (ms)")
        tree = ttk.Treeview(frame, columns=("name", "label", "sleep", "resolution"), show="headings", height=4, selectmode="browse")
        for key, label, width in (("name", "ID", 100), ("label", "Label", 150), ("sleep", "Sleep", 100), ("resolution", "Timer resolution", 160)):
            tree.heading(key, text=label)
            tree.column(key, width=width)
        tree.grid(row=0, column=0, sticky="ew")
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="w", pady=5)
        self.profiles_tree = tree
        self.complex_paths.add(path)

        def edit():
            selection = tree.selection()
            if not selection:
                return
            key = selection[0]
            item = _get(self.pending, path)[key]
            result = ask_fields(self.window, f"Profile {key}", (("Label", "label"), ("Sleep (ms)", "replacement_sleep_ms"), ("Timer resolution (ms)", "timer_resolution_ms")), item)
            if result is None:
                return
            try:
                updated = dict(item)
                updated.update(label=result["label"], replacement_sleep_ms=int(result["replacement_sleep_ms"]), timer_resolution_ms=int(result["timer_resolution_ms"]))
            except ValueError as error:
                messagebox.showerror("Invalid value", str(error), parent=self.window)
                return
            _get(self.pending, path)[key] = updated
            self.reset_paths.discard(path)
            self._refresh_profiles()
            tree.selection_set(key)

        ttk.Button(buttons, text="Edit", command=edit).pack(side="left", padx=(0, 5))
        ttk.Button(buttons, text="Reset to Default", command=lambda: self._reset_complex(path)).pack(side="left")
        tree.bind("<Double-1>", lambda _event: edit())
        self._refresh_profiles()

    def _refresh_profiles(self):
        tree = self.profiles_tree
        tree.delete(*tree.get_children())
        for key, profile in _get(self.pending, ("speed_boost", "profiles"), {}).items():
            tree.insert("", "end", iid=key, values=(key, profile.get("label", ""), profile.get("replacement_sleep_ms", ""), profile.get("timer_resolution_ms", "")))

    def _build_palette(self, parent):
        frame = self._section(parent, "Palette and row appearance")
        current = _get(self.pending, ("display", "active_palette"))
        self.palette_var = tk.StringVar(self.window, value=current)
        ttk.Label(frame, text="Palette").grid(row=0, column=0, sticky="w")
        names = list(self.manager.DISPLAY_PALETTES) + ["custom"]
        ttk.Combobox(frame, textvariable=self.palette_var, values=names, state="readonly").grid(row=0, column=1, sticky="ew", padx=7)
        ttk.Button(frame, text="Reset to Default", command=self._reset_palette).grid(row=0, column=2)
        ttk.Button(frame, text="Create custom from selected", command=self._create_custom_palette).grid(row=1, column=0, columnspan=3, sticky="w", pady=6)
        tree = ttk.Treeview(frame, columns=("state", "attribute", "value"), show="headings", height=8, selectmode="browse")
        for key, label in (("state", "State"), ("attribute", "Property"), ("value", "Value")):
            tree.heading(key, text=label)
            tree.column(key, width=180)
        tree.grid(row=2, column=0, columnspan=3, sticky="nsew")
        self.palette_tree = tree
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Button(buttons, text="Edit color/style", command=self._edit_palette_value).pack(side="left", padx=(0, 5))
        ttk.Button(buttons, text="Add property", command=self._add_palette_value).pack(side="left", padx=(0, 5))
        ttk.Button(buttons, text="Remove property", command=self._delete_palette_value).pack(side="left")
        self.palette_var.trace_add("write", lambda *_: self._palette_selected())
        self.complex_paths.add(("display", "active_palette"))
        self.complex_paths.add(("display", "row_appearance"))
        self._refresh_palette()

    def _palette_selected(self):
        name = self.palette_var.get()
        previous = _get(self.pending, ("display", "active_palette"))
        if name == "custom" and previous != "custom":
            current_appearance = _get(self.pending, ("display", "row_appearance"))
            if current_appearance == self.manager.DISPLAY_PALETTES.get(previous):
                stored = _get(self.baseline_user, ("display", "row_appearance"))
                if isinstance(stored, dict):
                    _set(self.pending, ("display", "row_appearance"), stored)
        _set(self.pending, ("display", "active_palette"), name)
        self.reset_paths.discard(("display", "active_palette"))
        self._refresh_palette()

    def _reset_palette(self):
        name = self._default(("display", "active_palette"))
        self.palette_var.set(name)
        self.reset_paths.add(("display", "active_palette"))
        self.reset_paths.add(("display", "row_appearance"))

    def _create_custom_palette(self):
        name = self.palette_var.get()
        source = self.manager.DISPLAY_PALETTES.get(name)
        if source is None:
            source = _get(self.pending, ("display", "row_appearance"), self.manager.DISPLAY_PALETTES[self.manager.DEFAULT_DISPLAY_PALETTE])
        _set(self.pending, ("display", "row_appearance"), source)
        _set(self.pending, ("display", "active_palette"), "custom")
        self.palette_var.set("custom")
        self.reset_paths.discard(("display", "row_appearance"))
        self._refresh_palette()

    def _palette_data(self):
        name = self.palette_var.get()
        return _get(self.pending, ("display", "row_appearance"), {}) if name == "custom" else self.manager.DISPLAY_PALETTES.get(name, {})

    def _refresh_palette(self):
        tree = self.palette_tree
        tree.delete(*tree.get_children())
        for state, properties in self._palette_data().items():
            for attribute, value in properties.items():
                tree.insert("", "end", iid=f"{state}|{attribute}", values=(state, attribute, value))

    def _selected_palette_property(self):
        selection = self.palette_tree.selection()
        if not selection:
            return None
        return tuple(selection[0].split("|", 1))

    def _edit_palette_value(self):
        item = self._selected_palette_property()
        if item is None:
            return
        if self.palette_var.get() != "custom":
            self._create_custom_palette()
        state, attribute = item
        data = _get(self.pending, ("display", "row_appearance"))
        value = data[state][attribute]
        if isinstance(value, bool):
            data[state][attribute] = not value
        else:
            result = colorchooser.askcolor(color=value, parent=self.window, title=f"{state}: {attribute}")
            if result[1] is None:
                return
            data[state][attribute] = result[1]
        self.reset_paths.discard(("display", "row_appearance"))
        self._refresh_palette()
        self.palette_tree.selection_set(f"{state}|{attribute}")

    def _add_palette_value(self):
        if self.palette_var.get() != "custom":
            self._create_custom_palette()
        result = ask_fields(self.window, "New palette property", (("State", "state"), ("Property", "attribute"), ("Value (#RRGGBB or true/false)", "value")))
        if result is None:
            return
        state, attribute, raw = (result[key].strip() for key in ("state", "attribute", "value"))
        if not state or not attribute:
            messagebox.showerror("Error", "Enter a state and property.", parent=self.window)
            return
        states = {"tree", "normal", "idle", "stale", "fin", "visible", "focused", "copy_source"}
        allowed = {"background", "heading_background", "heading_foreground"} if state == "tree" else {"foreground", "background", "stale_to_foreground", "fade_to_foreground", "bold", "italic"}
        if state not in states or attribute not in allowed:
            messagebox.showerror("Error", "Unknown row state or appearance property.", parent=self.window)
            return
        value = raw.lower() == "true" if raw.lower() in ("true", "false") else raw
        data = _get(self.pending, ("display", "row_appearance"))
        data.setdefault(state, {})[attribute] = value
        self.reset_paths.discard(("display", "row_appearance"))
        self._refresh_palette()

    def _delete_palette_value(self):
        item = self._selected_palette_property()
        if item is None:
            return
        if self.palette_var.get() != "custom":
            self._create_custom_palette()
        state, attribute = item
        _get(self.pending, ("display", "row_appearance"))[state].pop(attribute, None)
        self.reset_paths.discard(("display", "row_appearance"))
        self._refresh_palette()

    def _build_mapping(self):
        path = ("script_type_mapping",)
        frame = self._section(self.tabs["Scripts"], "Script type mapping (first match takes priority)")
        tree = ttk.Treeview(frame, columns=("key", "name", "column", "rules"), show="headings", height=17, selectmode="browse")
        for key, label, width in (("key", "Name fragment", 240), ("name", "Label", 190), ("column", "Column", 90), ("rules", "State rules", 120)):
            tree.heading(key, text=label)
            tree.column(key, width=width)
        tree.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        controls = ttk.Frame(frame)
        controls.grid(row=1, column=0, sticky="w", pady=6)
        self.mapping_tree = tree
        self.complex_paths.add(path)

        def selected_index():
            ids = tree.selection()
            return int(ids[0]) if ids else None

        def edit(add=False):
            items = list(_get(self.pending, path).items())
            index = selected_index()
            if not add and index is None:
                return
            old_key, old_item = ("", {"name": "", "column_number": 0}) if add else items[index]
            result = self._mapping_form(old_key, old_item)
            if result is None:
                return
            new_key, new_item = result
            if not new_key.strip():
                messagebox.showerror("Error", "Name fragment cannot be empty.", parent=self.window)
                return
            if any(key == new_key for pos, (key, _) in enumerate(items) if add or pos != index):
                messagebox.showerror("Error", "This name fragment is already in the table.", parent=self.window)
                return
            if add:
                items.append((new_key, new_item))
                index = len(items) - 1
            else:
                items[index] = (new_key, new_item)
            _set(self.pending, path, dict(items))
            self.reset_paths.discard(path)
            self._refresh_mapping()
            tree.selection_set(str(index))

        def delete():
            index = selected_index()
            if index is None:
                return
            items = list(_get(self.pending, path).items())
            del items[index]
            _set(self.pending, path, dict(items))
            self.reset_paths.discard(path)
            self._refresh_mapping()

        def move(delta):
            index = selected_index()
            items = list(_get(self.pending, path).items())
            if index is None or not 0 <= index + delta < len(items):
                return
            items[index], items[index + delta] = items[index + delta], items[index]
            _set(self.pending, path, dict(items))
            self.reset_paths.discard(path)
            self._refresh_mapping()
            tree.selection_set(str(index + delta))

        for label, command in (("Add", lambda: edit(True)), ("Edit", edit), ("Delete", delete), ("Up", lambda: move(-1)), ("Down", lambda: move(1)), ("Reset to Default", lambda: self._reset_complex(path))):
            ttk.Button(controls, text=label, command=command).pack(side="left", padx=(0, 5))
        tree.bind("<Double-1>", lambda _event: edit())
        self._refresh_mapping()

    def _refresh_mapping(self):
        tree = self.mapping_tree
        tree.delete(*tree.get_children())
        for index, (key, item) in enumerate(_get(self.pending, ("script_type_mapping",), {}).items()):
            rules = item.get("state_snapshot_rules", [])
            tree.insert("", "end", iid=str(index), values=(key, item.get("name", ""), item.get("column_number", 0), len(rules)))

    def _mapping_form(self, key, item):
        previous_grab = self.window.grab_current()
        top = tk.Toplevel(self.window)
        top.title("Script type")
        top.transient(self.window)
        top.geometry("680x470")
        body = ttk.Frame(top, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)
        vars_ = {}
        for row, (label, field, value) in enumerate((("Name fragment (spaces are preserved)", "key", key), ("Display name", "name", item.get("name", "")), ("Column number (0 = automatic)", "column", item.get("column_number", 0)))):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(top, value=str(value))
            ttk.Entry(body, textvariable=var).grid(row=row, column=1, sticky="ew", padx=7, pady=3)
            vars_[field] = var
        rules = deepcopy(item.get("state_snapshot_rules", []))
        if not rules and isinstance(item.get("state_snapshot_rule"), dict):
            rules = [deepcopy(item["state_snapshot_rule"])]
        rule_frame = ttk.LabelFrame(body, text="State detection rules", padding=6)
        rule_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=8)
        rule_frame.rowconfigure(0, weight=1)
        rule_frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(rule_frame, columns=("name", "detectors", "extractors"), show="headings", height=7, selectmode="browse")
        for column, label in (("name", "Name"), ("detectors", "Conditions"), ("extractors", "Extractors")):
            tree.heading(column, text=label)
            tree.column(column, width=180)
        tree.grid(row=0, column=0, sticky="nsew")

        def refresh_rules():
            tree.delete(*tree.get_children())
            for index, rule in enumerate(rules):
                tree.insert("", "end", iid=str(index), values=(rule.get("name", ""), ", ".join(rule.get("detector", [])), len(rule.get("extractors", []))))

        def selected():
            ids = tree.selection()
            return int(ids[0]) if ids else None

        def edit_rule(add=False):
            index = selected()
            if not add and index is None:
                return
            source = {} if add else rules[index]
            updated = self._rule_form(top, source)
            if updated is None:
                return
            if add:
                rules.append(updated)
            else:
                rules[index] = updated
            refresh_rules()

        def delete_rule():
            index = selected()
            if index is not None:
                del rules[index]
                refresh_rules()

        def move_rule(delta):
            index = selected()
            if index is None or not 0 <= index + delta < len(rules):
                return
            rules[index], rules[index + delta] = rules[index + delta], rules[index]
            refresh_rules()
            tree.selection_set(str(index + delta))

        rule_buttons = ttk.Frame(rule_frame)
        rule_buttons.grid(row=1, column=0, sticky="w", pady=5)
        for label, command in (("Add", lambda: edit_rule(True)), ("Edit", edit_rule), ("Delete", delete_rule), ("Up", lambda: move_rule(-1)), ("Down", lambda: move_rule(1))):
            ttk.Button(rule_buttons, text=label, command=command).pack(side="left", padx=(0, 4))
        tree.bind("<Double-1>", lambda _event: edit_rule())
        refresh_rules()
        result = []

        def accept():
            try:
                number = int(vars_["column"].get())
            except ValueError:
                messagebox.showerror("Error", "Column number must be an integer.", parent=top)
                return
            updated = deepcopy(item)
            updated["name"] = vars_["name"].get()
            updated["column_number"] = number
            if rules:
                updated["state_snapshot_rules"] = rules
                updated.pop("state_snapshot_rule", None)
            else:
                updated.pop("state_snapshot_rules", None)
                updated.pop("state_snapshot_rule", None)
            result.append((vars_["key"].get(), updated))
            top.destroy()

        footer = ttk.Frame(body)
        footer.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(footer, text="Cancel", command=top.destroy).pack(side="right")
        ttk.Button(footer, text="OK", command=accept).pack(side="right", padx=5)
        top.grab_set()
        top.wait_window()
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
        return result[0] if result else None

    def _rule_form(self, parent, original):
        previous_grab = parent.grab_current()
        top = tk.Toplevel(parent)
        top.title("State rule")
        top.transient(parent)
        top.geometry("700x450")
        body = ttk.Frame(top, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)
        name = tk.StringVar(top, value=original.get("name", ""))
        script_field = tk.StringVar(top, value=original.get("stats_mapping", {}).get("script", ""))
        score_field = tk.StringVar(top, value=original.get("stats_mapping", {}).get("score", ""))
        for row, (label, var) in enumerate((("Rule name", name), ("Script field for statistics", script_field), ("Score field for statistics", score_field))):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(body, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6, pady=3)
        conditions = ttk.LabelFrame(body, text="Conditions (one fragment per line)", padding=5)
        conditions.grid(row=3, column=0, sticky="nsew", pady=6)
        text = tk.Text(conditions, height=7, width=25)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(original.get("detector", [])))
        extractors = deepcopy(original.get("extractors", []))
        extractor_frame = ttk.LabelFrame(body, text="Extracted fields", padding=5)
        extractor_frame.grid(row=3, column=1, sticky="nsew", pady=6)
        extractor_frame.columnconfigure(0, weight=1)
        extractor_frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(extractor_frame, columns=("name", "after", "before"), show="headings", height=7, selectmode="browse")
        for column, label in (("name", "Name"), ("after", "After"), ("before", "Before")):
            tree.heading(column, text=label)
            tree.column(column, width=115)
        tree.grid(row=0, column=0, sticky="nsew")

        def refresh():
            tree.delete(*tree.get_children())
            for index, extractor in enumerate(extractors):
                tree.insert("", "end", iid=str(index), values=(extractor.get("name", ""), extractor.get("find_after", ""), extractor.get("find_before", "")))

        def selected():
            ids = tree.selection()
            return int(ids[0]) if ids else None

        def edit_extractor(add=False):
            index = selected()
            if not add and index is None:
                return
            initial = {} if add else extractors[index]
            initial = {"name": initial.get("name", ""), "find_after": initial.get("find_after", ""), "find_before": initial.get("find_before", "")}
            values = ask_fields(top, "Extracted field", (("Name", "name"), ("After text", "find_after"), ("Before text", "find_before")), initial)
            if values is None:
                return
            if add:
                extractors.append(values)
            else:
                updated = deepcopy(extractors[index])
                updated.update(values)
                extractors[index] = updated
            refresh()

        def delete_extractor():
            index = selected()
            if index is not None:
                del extractors[index]
                refresh()

        controls = ttk.Frame(extractor_frame)
        controls.grid(row=1, column=0, sticky="w", pady=4)
        for label, command in (("Add", lambda: edit_extractor(True)), ("Edit", edit_extractor), ("Delete", delete_extractor)):
            ttk.Button(controls, text=label, command=command).pack(side="left", padx=(0, 4))
        refresh()
        result = []

        def accept():
            updated = deepcopy(original)
            updated["name"] = name.get()
            updated["detector"] = [line for line in text.get("1.0", "end-1c").splitlines() if line]
            updated["extractors"] = extractors
            updated["stats_mapping"] = {key: value for key, value in (("script", script_field.get().strip()), ("score", score_field.get().strip())) if value}
            result.append(updated)
            top.destroy()

        footer = ttk.Frame(body)
        footer.grid(row=4, column=0, columnspan=2, sticky="e", pady=6)
        ttk.Button(footer, text="Cancel", command=top.destroy).pack(side="right")
        ttk.Button(footer, text="OK", command=accept).pack(side="right", padx=5)
        top.grab_set()
        top.wait_window()
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
        return result[0] if result else None

    def _collect_changes(self):
        for path in self.controls:
            try:
                _set(self.pending, path, self._value_from_control(path))
            except ValueError as error:
                raise SettingsValidationError(f"{'.'.join(path)}: {error}") from error
        paths = set(self.controls) | self.complex_paths | self.reset_paths
        changes = {}
        missing = object()
        for path in paths:
            if path in self.reset_paths:
                if _get(self.baseline_user, path, missing) is not missing:
                    changes[path] = RESET_TO_DEFAULT
            else:
                current = _get(self.pending, path, missing)
                initial = _get(self.initial_effective, path, missing)
                if initial is missing and current == self._default(path, []):
                    continue
                if current != initial:
                    changes[path] = deepcopy(current)
        return changes

    def save(self):
        try:
            changes = self._collect_changes()
            if not changes:
                self.status.set("No changes.")
                return
            effective = self.manager.save_editor_changes(changes, self.baseline_user)
        except (SettingsValidationError, OSError, KeyError, TypeError, ValueError) as error:
            messagebox.showerror("Settings not saved", str(error), parent=self.window)
            return
        changed_paths = set(changes)
        live = self.manager.apply_live_editor_settings(effective, changed_paths)
        try:
            self.on_applied(live, effective)
        except Exception as error:
            messagebox.showwarning("Settings saved", f"Some changes could not be applied immediately: {error}", parent=self.window)
        restart = changed_paths - self.LIVE_PATHS
        self.status.set("Saved. " + ("Takes effect after restart: " + ", ".join(".".join(path) for path in sorted(restart)) if restart else "All changes applied."))
        self.defaults, self.baseline_user, self.initial_effective = self.manager.get_editor_snapshot()
        self.pending = deepcopy(self.initial_effective)
        self.reset_paths.clear()
        for path, (var, kind) in self.controls.items():
            value = _get(self.pending, path)
            var.set(bool(value) if kind == "bool" else str(value))
        self.palette_var.set(_get(self.pending, ("display", "active_palette")))
        for path in self.list_widgets:
            self._refresh_list(path)
        for path in self.table_widgets:
            self._refresh_table(path)
        self._refresh_mapping()
        if "Speed Boost" in self.tabs:
            self._refresh_profiles()
        self._refresh_palette()
