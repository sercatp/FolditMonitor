import sys
import tkinter as tk
import weakref
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSpinBox,
    QStyledItemDelegate,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stats_editor import (
    StatsEditorSession,
    StatsViewLayout,
    StatsWindowControllerMixin,
    display_fin_client_name,
)
from stats_domain import (
    FIN_FIXED_COLUMNS,
    encode_history_edit_value,
    format_score_history,
    format_score_latest,
    format_score_line,
    latest_history_value,
    normalize_history_value,
)
from stats_module import (
    PuzzleLogInfo,
    StatsManager,
    StatsUiBridge,
    format_score,
    natural_sort_key,
    resolve_settings_dict,
)
from window_manager import open_path


class QtEventPump:
    INTERVAL_MS = 15
    _app: Optional[QApplication] = None
    _root: Any = None
    _after_id: Any = None
    _started: bool = False
    _windows: "weakref.WeakSet[QWidget]" = weakref.WeakSet()

    @classmethod
    def ensure_app(cls) -> QApplication:
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv[:1])
        app.setQuitOnLastWindowClosed(False)
        cls._app = app
        return app

    @classmethod
    def ensure_started(cls, root: Any) -> QApplication:
        app = cls.ensure_app()
        if root is not None:
            cls._root = root
        cls._started = True
        cls._ensure_scheduled()
        return app

    @classmethod
    def register_window(cls, window: QWidget):
        cls._windows.add(window)
        cls._ensure_scheduled()

    @classmethod
    def unregister_window(cls, window: QWidget):
        cls._windows.discard(window)

    @classmethod
    def _ensure_scheduled(cls):
        root = cls._root
        if root is None or cls._after_id is not None or not cls._started:
            return
        try:
            cls._after_id = root.after(cls.INTERVAL_MS, cls._pump_once)
        except (AttributeError, RuntimeError, tk.TclError):
            cls._after_id = None

    @classmethod
    def _stop(cls):
        root = cls._root
        after_id = cls._after_id
        cls._after_id = None
        if root is None or after_id is None:
            return
        try:
            root.after_cancel(after_id)
        except (AttributeError, RuntimeError, tk.TclError):
            pass

    @classmethod
    def _pump_once(cls):
        cls._after_id = None
        root = cls._root
        if root is None or not cls._started:
            return

        try:
            if not root.winfo_exists():
                return
        except (AttributeError, RuntimeError, tk.TclError):
            return

        try:
            cls.ensure_app().processEvents()
        except RuntimeError:
            pass

        cls._ensure_scheduled()


class TrackingItemDelegate(QStyledItemDelegate):
    def __init__(self, owner: "StatsWindowQt", parent: QWidget):
        super().__init__(parent)
        self._owner = owner

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            self._owner._set_editing_active(True)
        return editor

    def destroyEditor(self, editor, index):
        self._owner._set_editing_active(False)
        super().destroyEditor(editor, index)


class MainStatsTableView(QTableView):
    def __init__(self, owner: "StatsWindowQt"):
        super().__init__(owner)
        self._owner = owner

    def paintEvent(self, event):
        super().paintEvent(event)
        self._owner._paint_main_block_separators(self)

    def mousePressEvent(self, event):
        self._owner.note_user_interaction()
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._owner.select_main_index(index.row(), index.column(), extend=extend)
            self.setCurrentIndex(index)
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton and index.isValid():
            self._owner.select_main_index(index.row(), index.column())
            self.setCurrentIndex(index)
            self._owner.open_matching_log(self._owner.build_main_log_query(index.row(), index.column()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._owner.note_user_interaction()
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._owner.select_main_index(index.row(), index.column(), extend=extend)
            self.setCurrentIndex(index)
            self.edit(index)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FinStatsTableView(QTableView):
    def __init__(self, owner: "StatsWindowQt"):
        super().__init__(owner)
        self._owner = owner

    def mousePressEvent(self, event):
        self._owner.note_user_interaction()
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton and index.isValid():
            self._owner.select_fin_index(index.row(), index.column())
            self.setCurrentIndex(index)
            self._owner.open_matching_log(self._owner.build_fin_log_query(index.row(), index.column()))
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        self._owner.note_user_interaction()
        key = event.key()
        edit_keys = {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2}
        movement_keys = {
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
            Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown,
        }

        if key in edit_keys and self.currentIndex().isValid():
            self.edit(self.currentIndex())
            return

        super().keyPressEvent(event)

        if key in movement_keys and self.currentIndex().isValid():
            extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._owner.select_main_index(
                self.currentIndex().row(),
                self.currentIndex().column(),
                extend=extend,
            )


class PuzzlePickerDialogQt(QDialog):
    def __init__(self, parent: QWidget, puzzles: List[PuzzleLogInfo], current_puzzle_id: str):
        super().__init__(parent)
        self.current_puzzle_id = str(current_puzzle_id).strip()
        self.puzzles = list(puzzles)
        self.visible_puzzles: Dict[int, PuzzleLogInfo] = {}
        self.selected_puzzle_id: Optional[str] = None

        self.setWindowTitle("Open Puzzle")
        self.setMinimumSize(560, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:", self))
        self.filter_edit = QLineEdit(self)
        filter_row.addWidget(self.filter_edit, 1)
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Puzzle", "Modified", "State"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("", self)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Open")
        layout.addWidget(buttons)

        self.filter_edit.textChanged.connect(self._refresh_list)
        self.table.itemDoubleClicked.connect(lambda _item: self.accept())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        self._refresh_list()
        self.filter_edit.setFocus()

    @staticmethod
    def _format_modified(last_modified: float) -> str:
        if last_modified <= 0:
            return ""
        import time

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

    def _refresh_list(self):
        query = self.filter_edit.text().strip().lower()
        selected_puzzle_id = self.selected_puzzle_id
        current_row = self.table.currentRow()
        if selected_puzzle_id is None and current_row >= 0:
            info = self.visible_puzzles.get(current_row)
            if info is not None:
                selected_puzzle_id = info.puzzle_id

        self.visible_puzzles.clear()
        self.table.setRowCount(0)

        selected_row = None
        current_row_idx = None

        for info in self.puzzles:
            if query and query not in info.puzzle_id.lower():
                continue

            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                info.puzzle_id,
                self._format_modified(info.last_modified),
                self._state_text(info, self.current_puzzle_id),
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(row, col, item)
            self.visible_puzzles[row] = info
            if selected_puzzle_id and info.puzzle_id == selected_puzzle_id:
                selected_row = row
            if info.puzzle_id == self.current_puzzle_id:
                current_row_idx = row

        row_to_select = selected_row
        if row_to_select is None:
            row_to_select = current_row_idx
        if row_to_select is None and self.table.rowCount() > 0:
            row_to_select = 0

        if row_to_select is not None:
            self.table.selectRow(row_to_select)
            self.table.scrollToItem(self.table.item(row_to_select, 0))
            self.status_label.setText(
                f"Showing {len(self.visible_puzzles)} of {len(self.puzzles)} puzzle logs."
            )
            return

        self.status_label.setText("No puzzles matched the filter.")

    def accept(self):
        current_row = self.table.currentRow()
        info = self.visible_puzzles.get(current_row)
        if info is None:
            return
        self.selected_puzzle_id = info.puzzle_id
        super().accept()

    def show_dialog(self) -> Optional[str]:
        return self.selected_puzzle_id if self.exec() == QDialog.DialogCode.Accepted else None


class MainTableModel(QAbstractTableModel):
    TARGET_HEADER_COLOR = "#BFDCC8"
    TARGET_CELL_COLOR = "#F3FAF5"
    FIN_TARGET_HEADER_COLOR = "#EEDDB8"
    FIN_TARGET_CELL_COLOR = "#FCF8EE"

    def __init__(self, owner: "StatsWindowQt"):
        super().__init__(owner)
        self._owner = owner

    def refresh(self):
        self.beginResetModel()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return self._owner._row_count()

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._owner.vertical_column_specs)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEditable
        )

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        spec = self._owner.vertical_column_specs[index.column()]
        client_name = spec["client"]
        value_type = spec["type"]
        row_idx = index.row()
        display_text = self._owner._vertical_value(client_name, row_idx, value_type)

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if role == Qt.ItemDataRole.EditRole and value_type == "score":
                return self._owner._vertical_tooltip_value(client_name, row_idx, value_type)
            return display_text

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if value_type == "score":
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.BackgroundRole:
            if self._owner._is_main_cell_selected(client_name, row_idx):
                return QBrush(QColor(self._owner.selected_main_background_color))
            if self._owner._client_is_idle(client_name):
                return QBrush(QColor(self._owner.idle_main_background_color))
            if self._owner._main_client_is_fin_target(client_name):
                return QBrush(QColor(self.FIN_TARGET_CELL_COLOR))
            if self._owner._main_client_is_target(client_name):
                return QBrush(QColor(self.TARGET_CELL_COLOR))

        if role == Qt.ItemDataRole.ForegroundRole and self._owner._client_is_idle(client_name):
            return QBrush(QColor(self._owner.idle_main_foreground_color))

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._owner._vertical_tooltip_value(client_name, row_idx, value_type)

        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if section < 0 or section >= len(self._owner.vertical_column_specs):
                return None

            spec = self._owner.vertical_column_specs[section]
            client_name = spec["client"]

            if role == Qt.ItemDataRole.DisplayRole:
                return client_name if spec["type"] == "script" else "score"

            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

            if role == Qt.ItemDataRole.BackgroundRole:
                if self._owner._main_client_is_target(client_name):
                    return QBrush(QColor(self.TARGET_HEADER_COLOR))
                if self._owner._main_client_is_fin_target(client_name):
                    return QBrush(QColor(self.FIN_TARGET_HEADER_COLOR))
                if self._owner._client_is_idle(client_name):
                    return QBrush(QColor(self._owner.idle_header_background_color))

            if role == Qt.ItemDataRole.ForegroundRole and self._owner._client_is_idle(client_name):
                return QBrush(QColor(self._owner.idle_main_foreground_color))

            if role == Qt.ItemDataRole.FontRole and self._owner._main_client_is_target(client_name):
                font = QFont(self._owner.stats_font)
                font.setBold(True)
                return font

            if role == Qt.ItemDataRole.ToolTipRole:
                hints = [client_name]
                if self._owner._main_client_is_target(client_name):
                    hints.append("active target: Main")
                elif self._owner._main_client_is_fin_target(client_name):
                    hints.append("active target: Finalization")
                if self._owner._client_is_idle(client_name):
                    hints.append("idle")
                return "\n".join(hints)

            return None

        if orientation == Qt.Orientation.Vertical and role == Qt.ItemDataRole.DisplayRole:
            return str(section + 1)

        return None

    def setData(self, index: QModelIndex, value: Any, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        spec = self._owner.vertical_column_specs[index.column()]
        return self._owner._handle_main_edit(index.row(), spec, value)


class FinTableModel(QAbstractTableModel):
    ACTIVE_ROW_COLOR = "#E7F4EA"
    ACTIVE_ROW_IDLE_FOREGROUND = "#6F7782"

    def __init__(self, owner: "StatsWindowQt"):
        super().__init__(owner)
        self._owner = owner

    def refresh(self):
        self.beginResetModel()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._owner.fin_rows)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._owner.fin_column_specs)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        spec = self._owner.fin_column_specs[index.column()]
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if spec["name"] != "client":
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        spec = self._owner.fin_column_specs[index.column()]
        row_idx = index.row()
        column_name = spec["name"]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._owner._fin_value(row_idx, column_name)

        if role == Qt.ItemDataRole.EditRole:
            return self._owner._fin_value(row_idx, column_name, edit_mode=True)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column_name in {"client", "start_from", "state"}:
                return Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            if column_name == "notes":
                return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.BackgroundRole:
            if self._owner._is_selected_fin_row(row_idx):
                return QBrush(QColor(self._owner.selected_main_background_color))
            if self._owner._is_active_fin_row(row_idx):
                return QBrush(QColor(self.ACTIVE_ROW_COLOR))

        if (
            role == Qt.ItemDataRole.ForegroundRole
            and self._owner._is_active_fin_row(row_idx)
            and self._owner._client_is_idle(str(self._owner.fin_rows[row_idx].get("client", "")).strip())
        ):
            return QBrush(QColor(self.ACTIVE_ROW_IDLE_FOREGROUND))

        if role == Qt.ItemDataRole.ToolTipRole:
            if column_name == "client":
                return str(self._owner.fin_rows[row_idx].get("client", "")).strip()
            if column_name == "start_from":
                return str(self._owner.fin_rows[row_idx].get("start_from", "")).strip()
            return self._owner._fin_tooltip_value(row_idx, column_name)

        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if section < 0 or section >= len(self._owner.fin_column_specs):
                return None
            spec = self._owner.fin_column_specs[section]
            column_name = spec["name"]
            if role == Qt.ItemDataRole.DisplayRole:
                return FIN_FIXED_COLUMNS.get(column_name) or spec.get("label", column_name)
            return None

        if orientation == Qt.Orientation.Vertical and role == Qt.ItemDataRole.DisplayRole:
            return str(section + 1)

        return None

    def setData(self, index: QModelIndex, value: Any, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        spec = self._owner.fin_column_specs[index.column()]
        return self._owner._handle_fin_edit(index.row(), spec["name"], value)


class StatsWindowQt(StatsWindowControllerMixin, QMainWindow):
    _instance: Optional["StatsWindowQt"] = None

    @classmethod
    def get_open_instance(cls):
        instance = cls._instance
        if instance is None or instance._closed:
            cls._instance = None
            return None
        return instance

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
        if instance is None:
            return False
        if puzzle_id is not None and str(instance.puzzle_id).strip() != str(puzzle_id).strip():
            return False
        return instance.request_close()

    def __init__(
        self,
        pump_root: Any,
        manager: StatsManager,
        puzzle_id: str,
        settings_source: Any,
        log_lookup_handler: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        QtEventPump.ensure_started(pump_root)
        super().__init__(None)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        self.manager = manager
        self.session = StatsEditorSession(manager, puzzle_id)
        self.settings_source = settings_source
        self.settings = resolve_settings_dict(settings_source)
        self.log_lookup_handler = log_lookup_handler

        display_settings = self.settings.get("display", {})
        row_appearance = display_settings.get("row_appearance", {})
        if not isinstance(row_appearance, dict):
            row_appearance = {}
        copy_source_appearance = row_appearance.get("copy_source", {})
        idle_appearance = row_appearance.get("idle", {})
        if not isinstance(copy_source_appearance, dict):
            copy_source_appearance = {}
        if not isinstance(idle_appearance, dict):
            idle_appearance = {}
        self.selected_main_background_color = str(
            copy_source_appearance.get("background", "#dbeafe")
        )
        self.idle_main_background_color = str(idle_appearance.get("background", "#f3f4f6"))
        self.idle_header_background_color = str(idle_appearance.get("background", "#f3f4f6"))
        self.idle_main_foreground_color = str(idle_appearance.get("foreground", "#6b7280"))

        self.clients: List[str] = []
        self.selected_client: Optional[str] = None
        self.selected_row_index: Optional[int] = None
        self.selected_row_end_index: Optional[int] = None
        self.selected_vertical_field: Optional[str] = None
        self.selected_fin_row_index: Optional[int] = None
        self.selected_fin_column_key: Optional[str] = None

        self._pending_manager_reload = False
        self._editing_active = False
        self._is_window_interacting = False
        self._restoring_current_indexes = False
        self._reordering_fin_columns = False
        self._cleanup_done = False
        self._closing_via_request = False
        self._closed = False
        self._ui_bridge: Optional[StatsUiBridge] = None
        self._window_size_initialized = False
        self._splitter_sizes_initialized = False
        self._deferred_window_adjust_pending = False
        self._deferred_window_adjust_reset_splitter = False

        self.vertical_column_specs: List[Dict[str, Any]] = []
        self.vertical_client_columns: Dict[str, Dict[str, str]] = {}
        self.fin_column_specs: List[Dict[str, Any]] = []
        self._layout_state: Optional[StatsViewLayout] = None
        self._layout_signature: Optional[tuple[Any, ...]] = None

        font_settings = display_settings.get("fonts", {})
        self.stats_font = QFont(
            str(font_settings.get("family", "DejaVu Sans Mono")),
            int(font_settings.get("stats_size", 8)),
        )

        self._interaction_timer = QTimer(self)
        self._interaction_timer.setSingleShot(True)
        self._interaction_timer.setInterval(180)
        self._interaction_timer.timeout.connect(self._finish_window_interaction)

        self.setWindowTitle(f"Stats - {self.puzzle_id}")
        self.setMinimumSize(680, 420)
        self._build_ui()
        QtEventPump.register_window(self)

        self._load_working_data()
        self._refresh_all(preserve_selection=False, fin_view_mode="bottom")
        self._apply_initial_position(pump_root)

        StatsWindowQt._instance = self
        self._ui_bridge = StatsUiBridge(
            sync_from_ui=self._sync_manager_state_if_clean,
            push_to_ui=self._reload_from_manager_if_clean,
        )
        self.manager.register_ui_bridge(self._ui_bridge)
        self._refresh_notes_button()
        self._update_puzzle_buttons()
        self._remember_last_puzzle()

        self._puzzle_buttons_timer = QTimer(self)
        self._puzzle_buttons_timer.setInterval(2000)
        self._puzzle_buttons_timer.timeout.connect(self._update_puzzle_buttons)
        self._puzzle_buttons_timer.start()

        self.show()
        self.focus_window()

    def _build_ui(self):
        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(6)
        self.setCentralWidget(central)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        top_row.addWidget(QLabel("Decimals:", central))

        self.decimals_spin = QSpinBox(central)
        self.decimals_spin.setRange(0, 6)
        self.decimals_spin.setValue(int(self.manager.score_decimals))
        self.decimals_spin.setFixedWidth(56)
        top_row.addWidget(self.decimals_spin)

        self.client_assoc_label = QLabel("", central)
        top_row.addWidget(self.client_assoc_label)

        top_row.addStretch(1)

        self.puzzle_buttons_layout = QHBoxLayout()
        self.puzzle_buttons_layout.setSpacing(2)
        top_row.addLayout(self.puzzle_buttons_layout)
        self._puzzle_button_ids: List[str] = []

        top_row.addStretch(1)

        self.open_puzzle_button = QPushButton("Open Puzzle", central)
        self.open_puzzle_button.clicked.connect(self.open_puzzle_dialog)
        top_row.addWidget(self.open_puzzle_button)

        self.notes_button = QPushButton("Notes", central)
        self.notes_button.clicked.connect(self.open_notes_file)
        top_row.addWidget(self.notes_button)

        root_layout.addLayout(top_row)

        self.content_splitter = QSplitter(Qt.Orientation.Vertical, central)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.splitterMoved.connect(lambda *_args: self.note_user_interaction())

        self.main_group = QGroupBox("Main", central)
        main_layout = QVBoxLayout(self.main_group)
        main_layout.setContentsMargins(3, 4, 3, 3)
        main_layout.setSpacing(3)

        main_toolbar = QHBoxLayout()
        main_toolbar.setSpacing(3)
        add_row_button = QPushButton("Add Row", self.main_group)
        add_row_button.clicked.connect(self.add_vertical_row)
        main_toolbar.addWidget(add_row_button)

        delete_row_button = QPushButton("Delete Row", self.main_group)
        delete_row_button.clicked.connect(self.delete_vertical_row)
        main_toolbar.addWidget(delete_row_button)

        main_toolbar.addStretch(1)

        to_fin_button = QPushButton("To Finalization", self.main_group)
        to_fin_button.clicked.connect(self.move_vertical_to_fin)
        main_toolbar.addWidget(to_fin_button)
        main_layout.addLayout(main_toolbar)

        self.main_table = MainStatsTableView(self)
        self.main_table.setModel(MainTableModel(self))
        self.main_table.setFont(self.stats_font)
        self.main_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.main_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.main_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.main_table.setAlternatingRowColors(False)
        self.main_table.setWordWrap(False)
        self.main_table.setCornerButtonEnabled(False)
        self.main_table.setShowGrid(True)
        self.main_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.main_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.main_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.main_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.main_table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.main_table.horizontalHeader().setFont(self.stats_font)
        self.main_table.verticalHeader().setFont(self.stats_font)
        self.main_table.horizontalHeader().setStretchLastSection(False)
        self._main_delegate = TrackingItemDelegate(self, self.main_table)
        self.main_table.setItemDelegate(self._main_delegate)
        main_layout.addWidget(self.main_table, 1)

        self.content_splitter.addWidget(self.main_group)

        self.fin_group = QGroupBox("Finalization", central)
        fin_layout = QVBoxLayout(self.fin_group)
        fin_layout.setContentsMargins(3, 4, 3, 3)
        fin_layout.setSpacing(3)

        fin_toolbar = QHBoxLayout()
        fin_toolbar.setSpacing(3)
        to_main_button = QPushButton("To Main", self.fin_group)
        to_main_button.clicked.connect(self.move_fin_cell_to_vertical)
        fin_toolbar.addWidget(to_main_button)

        row_to_main_button = QPushButton("Row To Main", self.fin_group)
        row_to_main_button.clicked.connect(self.move_fin_row_to_vertical)
        fin_toolbar.addWidget(row_to_main_button)

        new_row_button = QPushButton("New Row", self.fin_group)
        new_row_button.clicked.connect(self.add_fin_row)
        fin_toolbar.addWidget(new_row_button)

        move_up_button = QPushButton("Move Up", self.fin_group)
        move_up_button.clicked.connect(lambda: self.move_fin_row(-1))
        fin_toolbar.addWidget(move_up_button)

        move_down_button = QPushButton("Move Down", self.fin_group)
        move_down_button.clicked.connect(lambda: self.move_fin_row(1))
        fin_toolbar.addWidget(move_down_button)

        delete_fin_row_button = QPushButton("Delete Row", self.fin_group)
        delete_fin_row_button.clicked.connect(self.delete_fin_row)
        fin_toolbar.addWidget(delete_fin_row_button)

        delete_cell_button = QPushButton("Delete Cell", self.fin_group)
        delete_cell_button.clicked.connect(self.delete_fin_cell)
        fin_toolbar.addWidget(delete_cell_button)
        fin_toolbar.addStretch(1)
        fin_layout.addLayout(fin_toolbar)

        self.fin_table = FinStatsTableView(self)
        self.fin_table.setModel(FinTableModel(self))
        self.fin_table.setFont(self.stats_font)
        self.fin_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.fin_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.fin_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.fin_table.setAlternatingRowColors(False)
        self.fin_table.setWordWrap(False)
        self.fin_table.setCornerButtonEnabled(False)
        self.fin_table.setShowGrid(True)
        self.fin_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.fin_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.fin_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.fin_table.horizontalHeader().setSectionsMovable(True)
        self.fin_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.fin_table.horizontalHeader().setFont(self.stats_font)
        self.fin_table.verticalHeader().setFont(self.stats_font)
        self.fin_table.horizontalHeader().setStretchLastSection(False)
        self._fin_delegate = TrackingItemDelegate(self, self.fin_table)
        self.fin_table.setItemDelegate(self._fin_delegate)
        self.fin_table.clicked.connect(self._on_fin_clicked)
        fin_layout.addWidget(self.fin_table, 1)

        self.content_splitter.addWidget(self.fin_group)
        root_layout.addWidget(self.content_splitter, 1)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)

        save_button = QPushButton("Save", central)
        save_button.clicked.connect(self.save)
        bottom_row.addWidget(save_button)

        close_button = QPushButton("Close", central)
        close_button.clicked.connect(self.request_close)
        bottom_row.addWidget(close_button)

        cancel_button = QPushButton("Cancel", central)
        cancel_button.clicked.connect(self.cancel)
        bottom_row.addWidget(cancel_button)
        root_layout.addLayout(bottom_row)

        compact_header_style = "QHeaderView::section { padding: 0px 2px; }"
        compact_item_style = "QTableView::item { padding: 0px 1px; }"
        self.main_table.setStyleSheet(
            compact_header_style
            + compact_item_style
            + "QTableView { gridline-color: #f4f7fa; }"
        )
        self.fin_table.setStyleSheet(
            compact_header_style
            + compact_item_style
            + "QTableView { gridline-color: #f4f7fa; }"
        )

        self._bind_shortcuts()
        self.main_table.selectionModel().currentChanged.connect(self._on_main_current_changed)
        self.fin_table.selectionModel().currentChanged.connect(self._on_fin_current_changed)
        self.fin_table.horizontalHeader().sectionMoved.connect(self._on_fin_section_moved)

    def _bind_shortcuts(self):
        self._copy_shortcut = QShortcut(QKeySequence("Ctrl+C"), self)
        self._copy_shortcut.activated.connect(self.copy_selection)
        self._insert_shortcut = QShortcut(QKeySequence("Ctrl+Insert"), self)
        self._insert_shortcut.activated.connect(self.copy_selection)
        self._paste_shortcut = QShortcut(QKeySequence("Ctrl+V"), self)
        self._paste_shortcut.activated.connect(self.paste_selection)
        self._cut_shortcut = QShortcut(QKeySequence("Ctrl+X"), self)
        self._cut_shortcut.activated.connect(self.cut_selection)
        self._save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self._save_shortcut.activated.connect(self.save)
        self._close_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._close_shortcut.activated.connect(self.cancel)

    def _show_info(self, title: str, text: str):
        QMessageBox.information(self, title, text)

    def _show_error(self, title: str, text: str):
        QMessageBox.critical(self, title, text)

    def _ask_yes_no(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _ask_yes_no_cancel(self, title: str, text: str) -> Optional[bool]:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        save_button = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == save_button:
            return True
        if clicked == discard_button:
            return False
        if clicked == cancel_button:
            return None
        return None

    def _refresh_stats_view(
        self,
        preserve_selection: bool = True,
        main_view_mode: str = "selection",
        fin_view_mode: str = "selection",
    ):
        self._refresh_all(
            preserve_selection=preserve_selection,
            main_view_mode=main_view_mode,
            fin_view_mode=fin_view_mode,
        )

    def _parse_decimals_value(self) -> int:
        return int(self.decimals_spin.value())

    def _get_decimals_text(self) -> str:
        return str(self.decimals_spin.value())

    def _set_decimals_value(self, value: int):
        self.decimals_spin.setValue(int(value))

    def note_user_interaction(self):
        self._is_window_interacting = True
        self._interaction_timer.start()

    def _finish_window_interaction(self):
        self._is_window_interacting = False
        self._apply_pending_manager_reload()

    def _set_editing_active(self, active: bool):
        self._editing_active = bool(active)
        if active:
            self.note_user_interaction()
            return
        QTimer.singleShot(0, self._apply_pending_manager_reload)

    def moveEvent(self, event):
        super().moveEvent(event)
        self.note_user_interaction()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.note_user_interaction()

    def closeEvent(self, event):
        if not self._closing_via_request:
            if not self._confirm_close(save_prompt=True):
                event.ignore()
                return
            self._closing_via_request = True
        self._cleanup_window()
        event.accept()

    def focus_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _get_saved_position(self) -> Optional[tuple[int, int]]:
        display = self.settings.get("display", {})
        stats_pos = display.get("stats_window_position", {})
        x = stats_pos.get("x", -1)
        y = stats_pos.get("y", -1)
        if isinstance(x, int) and isinstance(y, int) and x >= 0 and y >= 0:
            return x, y
        return None

    def _apply_initial_position(self, parent: Any):
        saved = self._get_saved_position()
        if saved:
            x, y = saved
        else:
            parent_x = 60
            parent_y = 60
            if parent is not None:
                try:
                    parent_x = int(parent.winfo_rootx()) + 30
                    parent_y = max(0, int(parent.winfo_rooty()) - 40)
                except (AttributeError, RuntimeError, TypeError, ValueError, tk.TclError):
                    pass
            x, y = parent_x, parent_y

        self._adjust_window_size()
        self.move(x, y)

    def _save_window_position(self):
        pos = self.frameGeometry().topLeft()
        x = int(pos.x())
        y = int(pos.y())
        if hasattr(self.settings_source, "save_stats_window_position"):
            try:
                self.settings_source.save_stats_window_position(x, y)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                pass
        else:
            display = self.settings.setdefault("display", {})
            display["stats_window_position"] = {"x": x, "y": y}

    def _cleanup_window(self):
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self._closed = True
        self._puzzle_buttons_timer.stop()
        QtEventPump.unregister_window(self)
        self._save_window_position()
        self.manager.unregister_ui_bridge(self._ui_bridge)
        self._ui_bridge = None
        if StatsWindowQt._instance is self:
            StatsWindowQt._instance = None

    def _sync_manager_state_if_clean(self, puzzle_id: str):
        if str(self.puzzle_id).strip() != str(puzzle_id).strip():
            return
        if self._editing_active or self._is_window_interacting or self.session.ui_dirty:
            return
        self._merge_pending_manager_reload()

    def _reload_from_manager_if_clean(self, puzzle_id: str):
        if str(self.puzzle_id).strip() != str(puzzle_id).strip():
            return
        if self._editing_active or self._is_window_interacting or self.session.ui_dirty:
            self._pending_manager_reload = True
            return
        self.reload_from_manager(preserve_dirty=True)
        self._refresh_all(main_view_mode="preserve", fin_view_mode="preserve")
        self._pending_manager_reload = False

    def _merge_pending_manager_reload(self):
        if not self._pending_manager_reload:
            return
        if self.session.ui_dirty:
            return
        self.reload_from_manager(preserve_dirty=True)
        self._pending_manager_reload = False

    def _apply_pending_manager_reload(self):
        if self._editing_active or not self._pending_manager_reload or self._is_window_interacting or self.session.ui_dirty:
            return
        self.reload_from_manager(preserve_dirty=True)
        self._pending_manager_reload = False
        self._refresh_all(main_view_mode="preserve", fin_view_mode="preserve")

    def _load_working_data(self):
        self.session.reload_from_manager(preserve_dirty=False)

    def _discard_unsaved_changes(self, reload_ui: bool = True):
        self._pending_manager_reload = False
        self.session.discard_unsaved_changes()
        self._set_decimals_value(int(self.manager.score_decimals))
        if reload_ui:
            self._refresh_all(preserve_selection=False)

    def reload_from_manager(self, preserve_dirty: bool = True):
        self.session.reload_from_manager(preserve_dirty=preserve_dirty)

    def _main_client_is_fin_target(self, client_name: str) -> bool:
        return self.active_targets.get(str(client_name).strip(), "vertical") == "horizontal"

    def _rebuild_layout_state(self) -> bool:
        layout = self.session.build_view_layout()
        layout_changed = layout.layout_signature != self._layout_signature
        self._layout_state = layout
        self._layout_signature = layout.layout_signature
        self.clients = layout.clients
        self.vertical_column_specs = layout.vertical_column_specs
        self.vertical_client_columns = layout.vertical_client_columns
        self.fin_column_specs = layout.fin_column_specs
        return layout_changed

    def _refresh_all(
        self,
        preserve_selection: bool = True,
        main_view_mode: str = "selection",
        fin_view_mode: str = "selection",
    ):
        if main_view_mode not in {"selection", "preserve"}:
            main_view_mode = "selection"
        if fin_view_mode not in {"selection", "preserve", "bottom"}:
            fin_view_mode = "selection"
        main_view_state = self._capture_table_view_state(self.main_table) if main_view_mode == "preserve" else None
        fin_view_state = self._capture_table_view_state(self.fin_table) if fin_view_mode == "preserve" else None

        if not preserve_selection:
            self.selected_client = None
            self._clear_main_selection()
            self._clear_fin_selection()

        layout_changed = self._rebuild_layout_state()
        if layout_changed:
            self._window_size_initialized = False
            self._splitter_sizes_initialized = False

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

        self.main_table.model().refresh()
        self.fin_table.model().refresh()
        fin_visible = bool(self.fin_rows)
        fin_visibility_changed = self.fin_group.isVisible() != fin_visible
        self.fin_group.setVisible(fin_visible)
        self._update_selection_labels()
        self._apply_table_layout()
        if fin_visibility_changed:
            self._queue_deferred_window_size_adjust(reset_splitter=fin_visible)
        else:
            self._adjust_window_size()
        self._restore_current_indexes(
            scroll_main_to_selection=main_view_mode == "selection",
            scroll_fin_to_selection=fin_view_mode == "selection",
        )
        if main_view_state is not None:
            self._restore_table_view_state(self.main_table, main_view_state)
        if fin_view_state is not None:
            self._restore_table_view_state(self.fin_table, fin_view_state)
        elif fin_view_mode == "bottom":
            self._scroll_fin_to_bottom()
            QTimer.singleShot(0, self._scroll_fin_to_bottom)

    def _update_selection_labels(self):
        self.client_assoc_label.setText(self.client_table_summary())

    def _row_count(self) -> int:
        if self._layout_state is None:
            self._rebuild_layout_state()
        return self._layout_state.row_count(self.working_entries) if self._layout_state is not None else 1

    def _vertical_value(self, client_name: str, row_idx: int, value_type: str) -> str:
        entries = self.working_entries.get(client_name, [])
        if row_idx >= len(entries):
            return ""
        entry = entries[row_idx]
        if value_type == "script":
            return str(entry.get("script", "")).strip()
        return format_score(entry.get("score", ""), self.manager.score_decimals)

    def _vertical_tooltip_value(self, client_name: str, row_idx: int, value_type: str) -> str:
        entries = self.working_entries.get(client_name, [])
        if row_idx >= len(entries):
            return ""
        entry = entries[row_idx]
        if value_type == "script":
            return str(entry.get("script", "")).strip()
        return format_score_line(entry.get("score", ""), self.manager.score_decimals)

    def _fin_value(self, row_idx: int, column_name: str, edit_mode: bool = False) -> str:
        if row_idx >= len(self.fin_rows):
            return ""
        row = self.fin_rows[row_idx]
        if column_name == "client":
            raw_value = str(row.get("client", "")).strip()
            return raw_value if edit_mode else display_fin_client_name(raw_value)
        if column_name == "state":
            raw_value = row.get("state", "")
            return encode_history_edit_value(raw_value) if edit_mode else latest_history_value(raw_value)
        if column_name == "notes":
            return str(row.get("notes", "")).strip()
        if column_name == "start_from":
            raw_value = str(row.get("start_from", "")).strip()
            return raw_value if edit_mode else display_fin_client_name(raw_value)
        if column_name == "start_score":
            return format_score(row.get("start_score", ""), self.manager.score_decimals)
        raw_value = row.get("cells", {}).get(column_name, "")
        if edit_mode:
            return encode_history_edit_value(raw_value, self.manager.score_decimals)
        return format_score_latest(raw_value, self.manager.score_decimals)

    def _fin_tooltip_value(self, row_idx: int, column_name: str) -> str:
        if row_idx >= len(self.fin_rows):
            return ""
        row = self.fin_rows[row_idx]
        if column_name == "state":
            return normalize_history_value(row.get("state", ""))
        if column_name == "notes":
            return str(row.get("notes", "")).strip()
        if column_name == "start_score":
            return format_score(row.get("start_score", ""), self.manager.score_decimals)
        if column_name in {"client", "start_from"}:
            return self._fin_value(row_idx, column_name, edit_mode=True)
        return format_score_history(row.get("cells", {}).get(column_name, ""), self.manager.score_decimals)

    def set_log_lookup_handler(self, log_lookup_handler: Optional[Callable[[Dict[str, Any]], Any]] = None):
        self.log_lookup_handler = log_lookup_handler

    @staticmethod
    def _valid_log_query(query: Dict[str, Any]) -> bool:
        return all(str(query.get(key) or "").strip() for key in ("client_name", "puzzle_id", "script_type", "score"))

    def build_main_log_query(self, row_idx: int, column_idx: int) -> Optional[Dict[str, Any]]:
        if column_idx < 0 or column_idx >= len(self.vertical_column_specs):
            return None
        spec = self.vertical_column_specs[column_idx]
        if spec.get("type") not in {"script", "score"}:
            return None

        client_name = str(spec.get("client", "")).strip()
        entries = self.working_entries.get(client_name, [])
        if row_idx < 0 or row_idx >= len(entries):
            return None

        entry = entries[row_idx]
        query = {
            "client_name": client_name,
            "puzzle_id": str(self.puzzle_id).strip(),
            "script_type": str(entry.get("script", "")).strip(),
            "score": format_score(entry.get("score", ""), self.manager.score_decimals),
        }
        return query if self._valid_log_query(query) else None

    def build_fin_log_query(self, row_idx: int, column_idx: int) -> Optional[Dict[str, Any]]:
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            return None
        if column_idx < 0 or column_idx >= len(self.fin_column_specs):
            return None

        spec = self.fin_column_specs[column_idx]
        column_name = str(spec.get("name", "")).strip()
        if column_name in FIN_FIXED_COLUMNS:
            return None
        if str(spec.get("kind", "")).strip() in {"blank", "bridge"}:
            return None

        script_type = str(spec.get("label") or column_name).strip()
        score = self._fin_value(row_idx, column_name)
        row = self.fin_rows[row_idx]
        query = {
            "client_name": str(row.get("client", "")).strip(),
            "puzzle_id": str(self.puzzle_id).strip(),
            "script_type": script_type,
            "score": score,
        }
        return query if self._valid_log_query(query) else None

    def open_matching_log(self, query: Optional[Dict[str, Any]]):
        if not query:
            return
        if self.log_lookup_handler is None:
            self._show_info("Logs", "Log lookup is not available.")
            return
        try:
            result = self.log_lookup_handler(dict(query))
        except Exception as exc:
            self._show_error("Logs", f"Failed to open matching log:\n{exc}")
            return

        self._handle_log_lookup_result(result)

    def _handle_log_lookup_result(self, result: Any):
        if isinstance(result, dict):
            status = str(result.get("status") or "").strip()
            if status == "opened":
                return
            if status == "remote_requested":
                count = int(result.get("count") or 0)
                suffix = "peer" if count == 1 else "peers"
                self._show_info("Logs", f"No local matching log found. Requested remote lookup from {count} {suffix}.")
                return
            if status == "not_found":
                self._show_error("Logs", "No matching log found.")
                return
            if status == "error":
                self._show_error("Logs", str(result.get("message") or "Failed to open matching log."))
                return

        if result:
            return

        self._show_error("Logs", "No matching log found.")

    def _measure_text_width(self, values: List[str]) -> int:
        metrics = QFontMetrics(self.stats_font)
        texts = [str(value) for value in values if value is not None]
        if not texts:
            return 0
        return max(metrics.horizontalAdvance(text) for text in texts) + 6

    def _preferred_column_width(self, table: QTableView, column_idx: int, header_text: str) -> int:
        content_width = max(0, int(table.sizeHintForColumn(column_idx)))
        header_width = self._measure_text_width([header_text])
        return max(content_width, header_width)

    def _apply_main_column_widths(self):
        for idx, spec in enumerate(self.vertical_column_specs):
            header_text = spec["client"] if spec["type"] == "script" else "score"
            width = self._preferred_column_width(self.main_table, idx, header_text)
            if spec["type"] == "script":
                self.main_table.setColumnWidth(idx, min(max(width, 72), 200))
            else:
                self.main_table.setColumnWidth(idx, min(max(width, 46), 120))

    def _apply_fin_column_widths(self):
        for idx, spec in enumerate(self.fin_column_specs):
            name = spec["name"]
            header_text = FIN_FIXED_COLUMNS.get(name) or str(spec.get("label", name))
            width = self._preferred_column_width(self.fin_table, idx, header_text)
            if name == "client":
                self.fin_table.setColumnWidth(idx, min(max(width, 48), 78))
            elif name == "state":
                self.fin_table.setColumnWidth(idx, min(max(width, 72), 150))
            elif name == "notes":
                self.fin_table.setColumnWidth(idx, min(max(width, 72), 180))
            elif name == "start_from":
                self.fin_table.setColumnWidth(idx, min(max(width, 48), 78))
            else:
                self.fin_table.setColumnWidth(idx, min(max(width, 46), 120))

    def _apply_table_layout(self):
        text_height = QFontMetrics(self.stats_font).height()
        row_height = max(13, text_height + 1)
        self.main_table.verticalHeader().setMinimumSectionSize(row_height)
        self.fin_table.verticalHeader().setMinimumSectionSize(row_height)
        self.main_table.verticalHeader().setDefaultSectionSize(row_height)
        self.fin_table.verticalHeader().setDefaultSectionSize(row_height)
        self.main_table.horizontalHeader().setFixedHeight(row_height)
        self.fin_table.horizontalHeader().setFixedHeight(row_height)

        self._apply_main_column_widths()
        self._apply_fin_column_widths()

        if self.fin_rows and not self._splitter_sizes_initialized:
            main_rows = min(max(6, self._row_count()), 14)
            fin_rows = min(max(6, len(self.fin_rows)), 18)
            self.content_splitter.setSizes(
                [
                    self.main_table.horizontalHeader().height() + row_height * main_rows + 90,
                    self.fin_table.horizontalHeader().height() + row_height * fin_rows + 90,
                ]
            )
            self._splitter_sizes_initialized = True

    def _activate_root_layout(self):
        central = self.centralWidget()
        layout = central.layout() if central is not None else None
        if layout is None:
            return
        layout.invalidate()
        self.content_splitter.updateGeometry()
        self.main_group.updateGeometry()
        self.fin_group.updateGeometry()
        layout.activate()

    def _queue_deferred_window_size_adjust(self, reset_splitter: bool = False):
        self._window_size_initialized = False
        self._deferred_window_adjust_reset_splitter = (
            self._deferred_window_adjust_reset_splitter or bool(reset_splitter)
        )
        if self._deferred_window_adjust_pending:
            return

        self._deferred_window_adjust_pending = True
        QTimer.singleShot(0, self._run_deferred_window_size_adjust)

    def _run_deferred_window_size_adjust(self):
        reset_splitter = self._deferred_window_adjust_reset_splitter
        self._deferred_window_adjust_pending = False
        self._deferred_window_adjust_reset_splitter = False
        if self._closed:
            return

        self._window_size_initialized = False
        if reset_splitter:
            self._splitter_sizes_initialized = False
        self._activate_root_layout()
        self._apply_table_layout()
        self._activate_root_layout()
        self._adjust_window_size()

    def _adjust_window_size(self):
        if self._window_size_initialized:
            return

        screen = self.screen()
        if screen is None:
            return

        available = screen.availableGeometry()
        self._activate_root_layout()
        self.main_table.updateGeometry()
        self.fin_table.updateGeometry()

        main_width = self.main_table.verticalHeader().width()
        for idx in range(self.main_table.model().columnCount()):
            main_width += self.main_table.columnWidth(idx)
        main_width += self.main_table.verticalScrollBar().sizeHint().width() + 24

        fin_width = self.fin_table.verticalHeader().width()
        for idx in range(self.fin_table.model().columnCount()):
            fin_width += self.fin_table.columnWidth(idx)
        fin_width += self.fin_table.verticalScrollBar().sizeHint().width() + 24

        target_width = max(
            680,
            self.top_row_min_width(),
            main_width + 12,
            fin_width + 12 if self.fin_rows else 0,
        )
        target_height = max(420, min(int(available.height() * 0.88), self.sizeHint().height() + 60))
        width = min(target_width, int(available.width() * 0.95))
        height = min(target_height, int(available.height() * 0.92))
        self.resize(width, height)
        self._window_size_initialized = True

    def top_row_min_width(self) -> int:
        root_layout = self.centralWidget().layout()
        margins = root_layout.contentsMargins()
        spacing = root_layout.spacing()
        buttons_width = 0
        for idx in range(self.puzzle_buttons_layout.count()):
            widget = self.puzzle_buttons_layout.itemAt(idx).widget()
            if widget is not None:
                buttons_width += widget.minimumWidth() + self.puzzle_buttons_layout.spacing()
        return (
            margins.left()
            + margins.right()
            + self.decimals_spin.sizeHint().width()
            + self.client_assoc_label.sizeHint().width()
            + buttons_width
            + self.open_puzzle_button.sizeHint().width()
            + self.notes_button.sizeHint().width()
            + spacing * 5
            + 56
        )

    @staticmethod
    def _capture_table_view_state(table: QTableView) -> Dict[str, int]:
        return {
            "horizontal": table.horizontalScrollBar().value(),
            "vertical": table.verticalScrollBar().value(),
        }

    @staticmethod
    def _restore_table_view_state(table: QTableView, state: Dict[str, int]):
        table.horizontalScrollBar().setValue(int(state.get("horizontal", 0)))
        table.verticalScrollBar().setValue(int(state.get("vertical", 0)))

    def _scroll_fin_to_bottom(self):
        if not self.fin_rows:
            return
        scroll_bar = self.fin_table.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _restore_current_indexes(self, scroll_main_to_selection: bool = True, scroll_fin_to_selection: bool = True):
        self._restoring_current_indexes = True
        try:
            if self.selected_client in self.vertical_client_columns and self.selected_row_index is not None:
                field_name = "score" if self.selected_vertical_field == "score" else "script"
                column_name = self.vertical_client_columns[self.selected_client][field_name]
                column_index = next(
                    (
                        idx
                        for idx, spec in enumerate(self.vertical_column_specs)
                        if spec["name"] == column_name
                    ),
                    0,
                )
                row_index = min(self.selected_row_index, max(0, self._row_count() - 1))
                current_index = self.main_table.model().index(row_index, column_index)
                if current_index.isValid():
                    self.main_table.setCurrentIndex(current_index)
                    if scroll_main_to_selection:
                        self.main_table.scrollTo(current_index, QAbstractItemView.ScrollHint.PositionAtCenter)
            else:
                self.main_table.setCurrentIndex(QModelIndex())

            if self.selected_fin_row_index is not None and self.selected_fin_row_index < len(self.fin_rows):
                column_name = self.selected_fin_column_key or "notes"
                column_index = next(
                    (
                        idx
                        for idx, spec in enumerate(self.fin_column_specs)
                        if spec["name"] == column_name
                    ),
                    0,
                )
                current_index = self.fin_table.model().index(self.selected_fin_row_index, column_index)
                if current_index.isValid():
                    self.fin_table.setCurrentIndex(current_index)
                    if scroll_fin_to_selection:
                        self.fin_table.scrollTo(current_index, QAbstractItemView.ScrollHint.PositionAtCenter)
            else:
                self.fin_table.setCurrentIndex(QModelIndex())
        finally:
            self._restoring_current_indexes = False

    def _paint_main_block_separators(self, table: QTableView):
        if not self.vertical_column_specs:
            return

        painter = QPainter(table.viewport())
        painter.save()
        painter.setPen(QPen(QColor("#c7d3de"), 1))
        bottom = max(0, table.viewport().height() - 1)
        for column_idx, spec in enumerate(self.vertical_column_specs):
            if spec["type"] != "score" or table.isColumnHidden(column_idx):
                continue
            x = table.columnViewportPosition(column_idx) + table.columnWidth(column_idx) - 1
            painter.drawLine(x, 0, x, bottom)
        painter.restore()
        painter.end()

    def _is_main_cell_selected(self, client_name: str, row_idx: int) -> bool:
        selected_range = self._selected_main_range()
        if selected_range is None or self.selected_client != client_name:
            return False
        start_idx, end_idx = selected_range
        return start_idx <= row_idx <= end_idx

    def _is_selected_fin_row(self, row_idx: int) -> bool:
        return self.selected_fin_row_index == row_idx and 0 <= row_idx < len(self.fin_rows)

    def select_main_index(self, row_idx: int, column_idx: int, extend: bool = False):
        if column_idx < 0 or column_idx >= len(self.vertical_column_specs):
            return
        spec = self.vertical_column_specs[column_idx]
        self._set_main_selection(spec["client"], row_idx, spec["type"], extend=extend)
        self._update_selection_labels()
        self.main_table.viewport().update()

    def select_fin_index(self, row_idx: int, column_idx: int):
        if row_idx < 0 or row_idx >= len(self.fin_rows):
            return
        if column_idx < 0 or column_idx >= len(self.fin_column_specs):
            return
        self._set_fin_selection(row_idx, self.fin_column_specs[column_idx]["name"])
        self._update_selection_labels()
        self.fin_table.viewport().update()

    def _on_main_current_changed(self, current: QModelIndex, _previous: QModelIndex):
        if self._restoring_current_indexes:
            return
        if not current.isValid():
            return
        extend = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.select_main_index(current.row(), current.column(), extend=extend)

    def _on_fin_clicked(self, index: QModelIndex):
        if not index.isValid() or index.column() >= len(self.fin_column_specs):
            return
        self.note_user_interaction()
        self.select_fin_index(index.row(), index.column())

    def _on_fin_current_changed(self, current: QModelIndex, _previous: QModelIndex):
        if self._restoring_current_indexes:
            return
        if not current.isValid() or current.column() >= len(self.fin_column_specs):
            return
        self.select_fin_index(current.row(), current.column())

    def _on_fin_section_moved(self, _logical_index: int, old_visual: int, new_visual: int):
        if self._reordering_fin_columns:
            return
        self.note_user_interaction()
        header = self.fin_table.horizontalHeader()
        column_count = len(self.fin_column_specs)
        if column_count == 0:
            return
        fixed_count = len(FIN_FIXED_COLUMNS)
        visual_to_logical = [header.logicalIndex(visual) for visual in range(column_count)]
        # The structural columns (client/from/state/notes/score) are pinned at the front:
        # a move is legal only when it permuted the dynamic columns among themselves and
        # left the structural block in place. Anything else (moving a structural column, or
        # dragging a score column into the structural block) is bounced — the CSV schema
        # keeps those columns contiguous up front.
        legal = all(visual_to_logical[visual] == visual for visual in range(fixed_count))
        ordered_keys = None
        if legal:
            ordered_keys = [
                self.fin_column_specs[visual_to_logical[visual]]["name"]
                for visual in range(fixed_count, column_count)
            ]
        # Always undo the visual move so the header stays at identity; the column list is
        # the single source of truth and the refresh re-renders it in the new order.
        self._reordering_fin_columns = True
        try:
            header.moveSection(new_visual, old_visual)
        finally:
            self._reordering_fin_columns = False
        if ordered_keys is not None:
            self.reorder_fin_columns(ordered_keys)

    def _flush_active_editor(self):
        focus_widget = QApplication.focusWidget()
        if focus_widget is not None and focus_widget is not self:
            focus_widget.clearFocus()
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        if self._editing_active:
            return False
        self._merge_pending_manager_reload()
        return True

    def _handle_main_edit(self, row_idx: int, spec: Dict[str, Any], value: Any) -> bool:
        return self._handle_main_edit_value(spec["client"], row_idx, spec["type"], value)

    def _handle_fin_edit(self, row_idx: int, column_name: str, value: Any) -> bool:
        return self._handle_fin_edit_value(row_idx, column_name, value)

    def _copy_text_from_active_editor(self) -> Optional[str]:
        focus_widget = QApplication.focusWidget()
        if focus_widget is None or not self._editing_active:
            return None
        if not hasattr(focus_widget, "text"):
            return None

        try:
            text = focus_widget.selectedText() if hasattr(focus_widget, "selectedText") else ""
        except (AttributeError, RuntimeError, TypeError):
            text = ""
        if text:
            return text
        try:
            return focus_widget.text().strip()
        except (AttributeError, RuntimeError, TypeError):
            return ""

    def _is_fin_focus_widget(self, focus_widget: Optional[QWidget]) -> bool:
        return bool(
            focus_widget is not None
            and (focus_widget is self.fin_table or self.fin_table.isAncestorOf(focus_widget))
        )

    def copy_selection(self):
        focus_widget = QApplication.focusWidget()
        text = self._copy_text_from_active_editor()
        if text is None:
            if self._is_fin_focus_widget(focus_widget):
                if self.selected_fin_row_index is None or self.selected_fin_column_key is None:
                    return
                text = self._fin_cell_clipboard_text(self.selected_fin_row_index, self.selected_fin_column_key)
            else:
                text = self._main_selection_clipboard_text()

        if text is not None:
            QApplication.clipboard().setText(text)

    def paste_selection(self):
        focus_widget = QApplication.focusWidget()
        if self._editing_active and focus_widget is not None and hasattr(focus_widget, "paste"):
            try:
                focus_widget.paste()
            except (AttributeError, RuntimeError, TypeError):
                pass
            return

        text = QApplication.clipboard().text()
        if self._is_fin_focus_widget(focus_widget):
            self.paste_fin_text(text)
            return
        self.paste_main_text(text)

    def cut_selection(self):
        focus_widget = QApplication.focusWidget()
        if self._editing_active and focus_widget is not None and hasattr(focus_widget, "cut"):
            try:
                focus_widget.cut()
            except (AttributeError, RuntimeError, TypeError):
                pass
            return

        if self._is_fin_focus_widget(focus_widget):
            text = self.cut_fin_selection_text()
        else:
            text = self.cut_main_selection_text()

        if text is not None:
            QApplication.clipboard().setText(text)

    def _set_window_puzzle(self, puzzle_id: str):
        self.session.puzzle_id = str(puzzle_id).strip()
        self.setWindowTitle(f"Stats - {self.puzzle_id}")
        self._window_size_initialized = False
        self._splitter_sizes_initialized = False
        self._refresh_notes_button()
        self._remember_last_puzzle()
        self._update_puzzle_buttons()

    def _remember_last_puzzle(self):
        if hasattr(self.settings_source, "save_stats_last_puzzle"):
            try:
                self.settings_source.save_stats_last_puzzle(self.puzzle_id)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                pass

    def _update_puzzle_buttons(self):
        if self._closed:
            return
        puzzle_ids = [str(puzzle_id).strip() for puzzle_id in self.manager.get_active_puzzles()]
        current_id = str(self.puzzle_id).strip()
        if current_id and current_id not in puzzle_ids:
            puzzle_ids = sorted([*puzzle_ids, current_id], key=natural_sort_key)
        if puzzle_ids != self._puzzle_button_ids:
            while self.puzzle_buttons_layout.count():
                item = self.puzzle_buttons_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            for puzzle_id in puzzle_ids:
                button = QPushButton(puzzle_id, self.centralWidget())
                button.setCheckable(True)
                button.setFixedWidth(button.fontMetrics().horizontalAdvance(puzzle_id) + 22)
                button.clicked.connect(lambda _checked=False, pid=puzzle_id: self._on_puzzle_button_clicked(pid))
                self.puzzle_buttons_layout.addWidget(button)
            self._puzzle_button_ids = puzzle_ids
        for idx in range(self.puzzle_buttons_layout.count()):
            button = self.puzzle_buttons_layout.itemAt(idx).widget()
            if button is not None:
                button.setChecked(button.text() == current_id)

    def _on_puzzle_button_clicked(self, puzzle_id: str):
        if str(puzzle_id).strip() != str(self.puzzle_id).strip():
            self.open_puzzle(puzzle_id)
        # Re-sync checked states: reverts the click if the switch was cancelled.
        self._update_puzzle_buttons()

    def _refresh_notes_button(self):
        label = "Notes*" if self.manager.has_notes_file(self.puzzle_id) else "Notes"
        self.notes_button.setText(label)

    def open_notes_file(self):
        note_path = self.manager.ensure_notes_file(self.puzzle_id)
        if not note_path:
            self._show_error("Notes", "Failed to resolve notes file path.")
            return

        self._refresh_notes_button()
        try:
            open_path(note_path)
        except Exception as exc:
            self._show_error("Notes", f"Failed to open notes file:\n{exc}")

    def open_puzzle_dialog(self):
        if not self._flush_active_editor():
            return

        puzzles = self.manager.get_logged_puzzles()
        if not puzzles:
            self._show_info("Open Puzzle", f"No puzzle CSV files found in:\n{self.manager.logs_folder}")
            return

        dialog = PuzzlePickerDialogQt(self, puzzles, self.puzzle_id)
        selected_puzzle_id = dialog.show_dialog()
        if selected_puzzle_id:
            self.open_puzzle(selected_puzzle_id)

    def _close_window(self):
        if self._closed or self._cleanup_done:
            return
        self._closing_via_request = True
        self.hide()
        # Defer the actual close until the button click event has unwound.
        QTimer.singleShot(0, self.close)

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
