import datetime
import os
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from save_catalog import ClientLocation, CopyReport, SaveCatalog, SaveIndex, SaveRecord, normalize_path
from savefile_api import export_pdb
from stats_ui_qt import QtEventPump
from window_manager import open_containing_folder


ClientProvider = Callable[[], Sequence[ClientLocation]]
SCOPE_ROLE = Qt.ItemDataRole.UserRole


def _natural_key(value: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def _format_size(size: int) -> str:
    value = float(max(0, int(size)))
    for suffix in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or suffix == "GB":
            return f"{value:.0f} {suffix}" if suffix == "B" else f"{value:.1f} {suffix}"
        value /= 1024.0
    return f"{value:.1f} GB"


class CopyTargetsDialog(QDialog):
    def __init__(self, parent: QWidget, clients: Sequence[ClientLocation], source_path: str):
        super().__init__(parent)
        self.clients = [client for client in clients if normalize_path(client.path) != normalize_path(source_path)]
        self.setWindowTitle("Copy save to clients")
        self.resize(520, 340)
        self.setMinimumSize(440, 280)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select one or more destination clients:", self))
        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for index, client in enumerate(self.clients):
            marker = "● " if client.running else "  "
            item = QListWidgetItem(f"{marker}{client.name} — {client.path}", self.list_widget)
            item.setData(SCOPE_ROLE, index)
        layout.addWidget(self.list_widget, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Copy")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_clients(self) -> List[ClientLocation]:
        if self.exec() != QDialog.DialogCode.Accepted:
            return []
        return [self.clients[int(item.data(SCOPE_ROLE))] for item in self.list_widget.selectedItems()]


class SaveManagerWindowQt(QMainWindow):
    _instance: Optional["SaveManagerWindowQt"] = None

    @classmethod
    def get_open_instance(cls) -> Optional["SaveManagerWindowQt"]:
        instance = cls._instance
        if instance is None or instance._closed:
            cls._instance = None
            return None
        return instance

    def __init__(
        self,
        pump_root,
        puzzle_id: str,
        clients_provider: ClientProvider,
        index_path: str,
        logs_folder: str,
        initial_client_path: Optional[str] = None,
        initial_scope: str = "client",
    ):
        QtEventPump.ensure_started(pump_root)
        super().__init__(None)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.pump_root = pump_root
        self.clients_provider = clients_provider
        self.index_path = os.path.abspath(index_path)
        self.logs_folder = os.path.abspath(logs_folder)
        self.catalog = SaveCatalog(SaveIndex(self.index_path))
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="save-metadata")

        self.puzzle_id = ""
        self.internal_puzzle_id = ""
        self.internal_puzzle_ids: tuple[str, ...] = ()
        self.clients: List[ClientLocation] = []
        self.clients_by_path: Dict[str, ClientLocation] = {}
        self.records_by_client: Dict[str, List[SaveRecord]] = {}
        self.client_items: Dict[str, QTreeWidgetItem] = {}
        self.visible_records: List[SaveRecord] = []
        self.pending_metadata: set[tuple[int, str]] = set()
        self.generation = 0
        self.scanned_clients = 0
        self.scan_finished = False
        self.scan_errors: List[str] = []
        self.mapping_error = ""
        self.mapping_warning = ""
        self.copying = False
        self.filter_active = False
        self.filter_valid = True
        self.last_normal_scope = "all"
        self.selected_scope = "all"
        self.sort_column = "total"
        self.sort_descending = True
        self._force_error_retry = False
        self._closed = False

        self.setMinimumSize(860, 430)
        self.resize(1360, 760)
        self._build_ui()
        SaveManagerWindowQt._instance = self
        QtEventPump.register_window(self)

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(50)
        self.event_timer.timeout.connect(self._safe_drain_events)
        self.event_timer.start()
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(180)
        self.filter_timer.timeout.connect(self._apply_filters)

        self.open_context(puzzle_id, initial_client_path, initial_scope)

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        compact_font = central.font()
        if compact_font.pointSizeF() > 8:
            compact_font.setPointSizeF(8)
            central.setFont(compact_font)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        toolbar.addWidget(QLabel("Name:", central))
        self.name_filter = QLineEdit(central)
        self.name_filter.setFixedWidth(160)
        toolbar.addWidget(self.name_filter)
        toolbar.addWidget(QLabel("Score:", central))
        self.score_field = QComboBox(central)
        self.score_field.addItems(("Total", "Base"))
        self.score_field.setFixedWidth(65)
        toolbar.addWidget(self.score_field)
        toolbar.addWidget(QLabel("Min:", central))
        self.min_score = QLineEdit(central)
        self.min_score.setFixedWidth(75)
        toolbar.addWidget(self.min_score)
        toolbar.addWidget(QLabel("Max:", central))
        self.max_score = QLineEdit(central)
        self.max_score.setFixedWidth(75)
        toolbar.addWidget(self.max_score)
        self.clear_button = QPushButton("Clear", central)
        self.clear_button.clicked.connect(self._clear_filters)
        toolbar.addWidget(self.clear_button)
        self.include_quick_auto = QCheckBox("Include quick/auto saves", central)
        self.include_quick_auto.setChecked(False)
        self.include_quick_auto.toggled.connect(lambda _checked: self.refresh(False))
        toolbar.addWidget(self.include_quick_auto)
        toolbar.addStretch(1)
        self.refresh_button = QPushButton("Refresh", central)
        self.refresh_button.clicked.connect(lambda: self.refresh(True))
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.name_filter.textChanged.connect(self._schedule_filter_update)
        self.min_score.textChanged.connect(self._schedule_filter_update)
        self.max_score.textChanged.connect(self._schedule_filter_update)
        self.score_field.currentTextChanged.connect(self._schedule_filter_update)

        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        splitter.setChildrenCollapsible(False)
        self.client_tree = QTreeWidget(splitter)
        self.client_tree.setColumnCount(2)
        self.client_tree.setHeaderLabels(("Client", "Saves"))
        self.client_tree.setMinimumWidth(180)
        self.client_tree.setColumnWidth(0, 150)
        self.client_tree.setColumnWidth(1, 50)
        self.client_tree.setUniformRowHeights(True)
        self.client_tree.header().setFixedHeight(22)
        self.client_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.client_tree.currentItemChanged.connect(self._on_client_selected)

        self.save_table = QTableWidget(splitter)
        self.table_columns = ("client", "name", "total", "base", "modified", "size", "file")
        self.save_table.setColumnCount(len(self.table_columns))
        self.save_table.setHorizontalHeaderLabels(("Client", "Save name", "Total", "Base", "Modified", "Size", "File"))
        self.save_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.save_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.save_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.save_table.setAlternatingRowColors(True)
        self.save_table.setWordWrap(False)
        self.save_table.verticalHeader().setVisible(False)
        self.save_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.save_table.verticalHeader().setMinimumSectionSize(18)
        self.save_table.verticalHeader().setDefaultSectionSize(20)
        header = self.save_table.horizontalHeader()
        header.setFixedHeight(22)
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._sort_by_index)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        widths = (90, 145, 80, 80, 135, 70, 240)
        for column, width in enumerate(widths):
            self.save_table.setColumnWidth(column, width)
        self.save_table.itemSelectionChanged.connect(self._update_action_states)
        self.save_table.cellDoubleClicked.connect(lambda _row, _column: self._show_record_details())
        splitter.addWidget(self.client_tree)
        splitter.addWidget(self.save_table)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes((210, 850))
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copy to…", central)
        self.share_button = QPushButton("Share to all", central)
        self.export_button = QPushButton("Export PDB", central)
        self.folder_button = QPushButton("Open folder", central)
        self.copy_button.clicked.connect(self._copy_to)
        self.share_button.clicked.connect(self._share_to_all)
        self.export_button.clicked.connect(self._export_pdb)
        self.folder_button.clicked.connect(self._open_folder)
        for button in (self.copy_button, self.share_button, self.export_button, self.folder_button):
            actions.addWidget(button)
        actions.addStretch(1)
        close_button = QPushButton("Close", central)
        close_button.clicked.connect(self.close)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self.status_label = QLabel("Ready", central)
        self.status_label.setWordWrap(False)
        layout.addWidget(self.status_label)
        self._update_action_states()

    def open_context(self, puzzle_id: str, initial_client_path: Optional[str], initial_scope: str):
        self.puzzle_id = str(puzzle_id).strip()
        self.setWindowTitle(f"Save Manager — Puzzle {self.puzzle_id}")
        if initial_scope == "running":
            self.selected_scope = "running"
        elif initial_scope == "all":
            self.selected_scope = "all"
        elif initial_client_path:
            self.selected_scope = normalize_path(initial_client_path)
        else:
            self.selected_scope = "all"
        self.last_normal_scope = self.selected_scope
        self._clear_filters(refresh=False)
        self.refresh(False)
        self.focus_window()

    def focus_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def refresh(self, force_error_retry: bool = False):
        if self._closed:
            return
        try:
            provided = list(self.clients_provider())
        except Exception as exc:
            QMessageBox.critical(self, "Save Manager", f"Failed to discover Foldit clients:\n{exc}")
            return

        unique: Dict[str, ClientLocation] = {}
        for client in provided:
            client_path = os.path.abspath(str(client.path))
            key = normalize_path(client_path)
            existing = unique.get(key)
            if existing is None or client.running:
                unique[key] = ClientLocation(
                    str(client.name),
                    client_path,
                    bool(client.running),
                    str(getattr(client, "active_puzzle_id", "") or "").strip(),
                )
        self.clients = sorted(unique.values(), key=lambda client: _natural_key(client.name))
        self.clients_by_path = {normalize_path(client.path): client for client in self.clients}
        self.records_by_client = {normalize_path(client.path): [] for client in self.clients}
        self.generation += 1
        generation = self.generation
        self.pending_metadata.clear()
        self.scanned_clients = 0
        self.scan_finished = False
        self.internal_puzzle_id = ""
        self.internal_puzzle_ids = ()
        self.scan_errors = []
        self.mapping_error = ""
        self.mapping_warning = ""
        self._force_error_retry = force_error_retry
        self._populate_client_tree()
        self._refresh_table()
        self.refresh_button.setEnabled(False)
        self._update_status()
        threading.Thread(
            target=self._scan_worker,
            args=(generation, self.puzzle_id, tuple(self.clients), self.include_quick_auto.isChecked()),
            daemon=True,
            name="save-file-scan",
        ).start()

    def _scan_worker(self, generation: int, puzzle_id: str, clients: Sequence[ClientLocation], include_quick_auto: bool):
        try:
            resolution = self.catalog.resolve_internal_puzzle_ids(puzzle_id, clients)
        except Exception as exc:
            self.events.put(("mapping_error", generation, str(exc)))
            self.events.put(("scan_done", generation))
            return
        if not resolution.internal_ids:
            self.events.put(("mapping_error", generation, f"Could not map public puzzle {puzzle_id} to an internal id."))
            self.events.put(("scan_done", generation))
            return
        self.events.put(("mapping_resolved", generation, resolution.internal_ids, resolution.warning))
        for client in clients:
            try:
                records = self.catalog.scan_client(
                    puzzle_id,
                    client,
                    resolution.internal_ids,
                    include_quick_auto=include_quick_auto,
                )
                self.events.put(("client_scanned", generation, normalize_path(client.path), records))
            except Exception as exc:
                self.events.put(("client_scan_error", generation, normalize_path(client.path), str(exc)))
        self.events.put(("scan_done", generation))

    def _populate_client_tree(self):
        self.client_tree.blockSignals(True)
        self.client_tree.clear()
        self.client_items.clear()
        running_item = QTreeWidgetItem(("All running", "0"))
        running_item.setData(0, SCOPE_ROLE, "running")
        all_item = QTreeWidgetItem(("All installations", "0"))
        all_item.setData(0, SCOPE_ROLE, "all")
        self.client_tree.addTopLevelItems((running_item, all_item))
        selected_item = all_item
        for client in self.clients:
            key = normalize_path(client.path)
            marker = "● " if client.running else "  "
            item = QTreeWidgetItem((f"{marker}{client.name}", "0"))
            item.setData(0, SCOPE_ROLE, key)
            self.client_tree.addTopLevelItem(item)
            self.client_items[key] = item
            if self.selected_scope == key:
                selected_item = item
        if self.selected_scope == "running":
            selected_item = running_item
        elif self.selected_scope == "all":
            selected_item = all_item
        elif self.selected_scope not in self.clients_by_path:
            self.selected_scope = "all"
            self.last_normal_scope = "all"
            selected_item = all_item
        self.client_tree.setCurrentItem(selected_item)
        self.client_tree.blockSignals(False)

    def _scope_item(self, scope: str) -> Optional[QTreeWidgetItem]:
        for index in range(self.client_tree.topLevelItemCount()):
            item = self.client_tree.topLevelItem(index)
            if item.data(0, SCOPE_ROLE) == scope:
                return item
        return None

    def _update_client_counts(self):
        running_total = 0
        all_total = 0
        for key, records in self.records_by_client.items():
            count = len(records)
            all_total += count
            client = self.clients_by_path.get(key)
            if client and client.running:
                running_total += count
            item = self.client_items.get(key)
            if item is not None:
                item.setText(1, str(count))
        for scope, count in (("running", running_total), ("all", all_total)):
            item = self._scope_item(scope)
            if item is not None:
                item.setText(1, str(count))
        search = self._scope_item("search")
        if search is not None:
            search.setText(1, str(len(self._filtered_records())))

    def _on_client_selected(self, current: Optional[QTreeWidgetItem], _previous: Optional[QTreeWidgetItem]):
        if current is None:
            return
        scope = current.data(0, SCOPE_ROLE)
        if not scope:
            return
        if self.filter_active and scope != "search":
            search = self._scope_item("search")
            if search is not None:
                self.client_tree.setCurrentItem(search)
            return
        self.selected_scope = str(scope)
        if scope != "search":
            self.last_normal_scope = str(scope)
        self._ensure_metadata_for_current_view()
        self._refresh_table()

    def _records_for_scope(self, scope: Optional[str] = None) -> List[SaveRecord]:
        selected = scope or self.selected_scope
        if selected in ("all", "search"):
            return [record for records in self.records_by_client.values() for record in records]
        if selected == "running":
            return [
                record
                for key, records in self.records_by_client.items()
                if self.clients_by_path.get(key) and self.clients_by_path[key].running
                for record in records
            ]
        return list(self.records_by_client.get(selected, ()))

    def _ensure_metadata_for_current_view(self):
        for record in self._records_for_scope("all" if self.filter_active else None):
            self._schedule_metadata(record)

    def _schedule_metadata(self, record: SaveRecord):
        if record.metadata_loaded and not (self._force_error_retry and record.error):
            return
        key = (self.generation, normalize_path(record.path))
        if key in self.pending_metadata:
            return
        self.pending_metadata.add(key)
        generation = self.generation
        try:
            future = self.executor.submit(self.catalog.load_metadata, record, self._force_error_retry)
        except RuntimeError:
            self.pending_metadata.discard(key)
            return

        def completed(done_future):
            try:
                self.events.put(("metadata", generation, key, done_future.result()))
            except Exception as exc:
                self.events.put(("metadata_error", generation, key, str(exc)))

        future.add_done_callback(completed)

    def _safe_drain_events(self):
        if self._closed:
            return
        try:
            self._drain_events()
        except Exception as exc:
            self.status_label.setText(f"Save Manager UI error: {exc}")

    def _drain_events(self):
        changed = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            event_type, generation, *payload = event
            if event_type == "copy_done":
                copy_puzzle_id, report = payload
                self.copying = False
                self._show_copy_report(report)
                if copy_puzzle_id == self.puzzle_id:
                    self.refresh(False)
                else:
                    self._update_action_states()
                    self._update_status()
                continue
            if generation != self.generation:
                continue
            if event_type == "mapping_resolved":
                self.internal_puzzle_ids = tuple(str(value) for value in payload[0])
                self.internal_puzzle_id = ", ".join(self.internal_puzzle_ids)
                self.mapping_warning = str(payload[1] or "")
                changed = True
            elif event_type == "mapping_error":
                self.mapping_error = str(payload[0])
                changed = True
            elif event_type == "client_scanned":
                client_key, records = payload
                self.records_by_client[client_key] = records
                self.scanned_clients += 1
                changed = True
                if self.filter_active or self.selected_scope in ("all", client_key) or (
                    self.selected_scope == "running"
                    and self.clients_by_path.get(client_key)
                    and self.clients_by_path[client_key].running
                ):
                    for record in records:
                        self._schedule_metadata(record)
            elif event_type == "client_scan_error":
                self.scanned_clients += 1
                client_key, error = payload
                client = self.clients_by_path.get(client_key)
                self.scan_errors.append(f"{client.name if client else client_key}: {error}")
                changed = True
            elif event_type == "scan_done":
                self.scan_finished = True
                self.refresh_button.setEnabled(True)
                changed = True
            elif event_type in ("metadata", "metadata_error"):
                key = payload[0]
                self.pending_metadata.discard(key)
                changed = True
        if changed:
            self._update_client_counts()
            self._refresh_table()
            self._update_status()

    def _schedule_filter_update(self, *_args):
        if hasattr(self, "filter_timer"):
            self.filter_timer.start()

    @staticmethod
    def _parse_optional_float(value: str) -> Optional[float]:
        text = str(value).strip().replace(",", ".")
        return None if not text else float(text)

    def _filter_values(self):
        try:
            minimum = self._parse_optional_float(self.min_score.text())
            maximum = self._parse_optional_float(self.max_score.text())
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError("Min must not exceed Max")
            self.filter_valid = True
            return self.name_filter.text(), self.score_field.currentText().casefold(), minimum, maximum
        except ValueError as exc:
            self.filter_valid = False
            self.status_label.setText(f"Invalid score filter: {exc}")
            return self.name_filter.text(), self.score_field.currentText().casefold(), None, None

    def _apply_filters(self):
        name_query, _score_field, _minimum, _maximum = self._filter_values()
        active = bool(name_query.strip() or self.min_score.text().strip() or self.max_score.text().strip())
        if active and not self.filter_active:
            self.last_normal_scope = self.selected_scope if self.selected_scope != "search" else self.last_normal_scope
            self.filter_active = True
            search = QTreeWidgetItem(("Search results", "0"))
            search.setData(0, SCOPE_ROLE, "search")
            self.client_tree.insertTopLevelItem(0, search)
            self.selected_scope = "search"
            self.client_tree.setCurrentItem(search)
        elif not active and self.filter_active:
            self.filter_active = False
            search = self._scope_item("search")
            if search is not None:
                index = self.client_tree.indexOfTopLevelItem(search)
                self.client_tree.takeTopLevelItem(index)
            self.selected_scope = self.last_normal_scope
            item = self._scope_item(self.selected_scope) or self._scope_item("all")
            if item is not None:
                self.client_tree.setCurrentItem(item)
        if active and self.filter_valid:
            self._ensure_metadata_for_current_view()
        self._update_client_counts()
        self._refresh_table()
        self._update_status()

    def _clear_filters(self, _checked: bool = False, refresh: bool = True):
        for widget in (self.name_filter, self.min_score, self.max_score):
            widget.blockSignals(True)
            widget.clear()
            widget.blockSignals(False)
        if refresh:
            self._apply_filters()
            return
        self.filter_active = False
        self.filter_valid = True
        search = self._scope_item("search")
        if search is not None:
            index = self.client_tree.indexOfTopLevelItem(search)
            self.client_tree.takeTopLevelItem(index)

    def _filtered_records(self) -> List[SaveRecord]:
        records = self._records_for_scope("all" if self.filter_active else None)
        if not self.filter_active:
            return records
        name_query, score_field, minimum, maximum = self._filter_values()
        if not self.filter_valid:
            return []
        return self.catalog.filter_records(records, name_query, score_field, minimum, maximum)

    def _sort_by_index(self, index: int):
        column = self.table_columns[index]
        if self.sort_column == column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column
            self.sort_descending = column in ("total", "base", "modified", "size")
        self._refresh_table()

    def _record_sort_value(self, record: SaveRecord):
        return {
            "client": record.client_name.casefold(),
            "name": record.save_name.casefold(),
            "total": record.total_score,
            "base": record.base_score,
            "modified": record.modified,
            "size": record.size,
            "file": record.file_name.casefold(),
        }.get(self.sort_column)

    def _sorted_records(self, records: Iterable[SaveRecord]) -> List[SaveRecord]:
        present, missing = [], []
        for record in records:
            (missing if self._record_sort_value(record) is None else present).append(record)
        present.sort(key=self._record_sort_value, reverse=self.sort_descending)
        missing.sort(key=lambda record: (record.modified, record.file_name.casefold()), reverse=True)
        return present + missing

    def _refresh_table(self):
        selected = self._selected_record()
        selected_path = normalize_path(selected.path) if selected else None
        self.visible_records = self._sorted_records(self._filtered_records())
        self.save_table.blockSignals(True)
        self.save_table.setRowCount(len(self.visible_records))
        selected_row = -1
        for row, record in enumerate(self.visible_records):
            if record.error:
                save_name = "⚠ Parse error"
            elif record.metadata_loaded:
                save_name = record.save_name
            else:
                save_name = "…"
            values = (
                record.client_name,
                save_name,
                "" if record.total_score is None else f"{record.total_score:.3f}",
                "" if record.base_score is None else f"{record.base_score:.3f}",
                datetime.datetime.fromtimestamp(record.modified).strftime("%Y-%m-%d %H:%M:%S"),
                _format_size(record.size),
                record.file_name,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (2, 3, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(record.error or record.path)
                self.save_table.setItem(row, column, item)
            if selected_path and normalize_path(record.path) == selected_path:
                selected_row = row
        if selected_row >= 0:
            self.save_table.selectRow(selected_row)
        self.save_table.blockSignals(False)
        self._update_action_states()

    def _selected_record(self) -> Optional[SaveRecord]:
        row = self.save_table.currentRow()
        return self.visible_records[row] if 0 <= row < len(self.visible_records) else None

    def _update_action_states(self):
        enabled = self._selected_record() is not None and not self.copying
        for button in (self.copy_button, self.share_button, self.export_button, self.folder_button):
            button.setEnabled(enabled)

    def _copy_to(self):
        record = self._selected_record()
        if record is None:
            return
        targets = CopyTargetsDialog(self, self.clients, record.client_path).selected_clients()
        if targets:
            self._start_copy(record, targets)

    def _share_to_all(self):
        record = self._selected_record()
        if record is None:
            return
        targets = [
            client
            for client in self.clients
            if client.running and normalize_path(client.path) != normalize_path(record.client_path)
        ]
        if not targets:
            QMessageBox.information(self, "Save Manager", "No other running clients found.")
            return
        self._start_copy(record, targets)

    def _start_copy(self, record: SaveRecord, targets: Sequence[ClientLocation]):
        if self.copying:
            return
        self.copying = True
        self._update_action_states()
        self.status_label.setText(f"Copying {record.file_name} to {len(targets)} client(s)…")
        generation = self.generation
        copy_puzzle_id = self.puzzle_id

        def worker():
            self.events.put(("copy_done", generation, copy_puzzle_id, self.catalog.copy_record(record, targets)))

        threading.Thread(target=worker, daemon=True, name="save-copy").start()

    def _show_copy_report(self, report: CopyReport):
        lines = [f"Copied: {report.copied}", f"Skipped: {report.skipped}", f"Failed: {report.failed}"]
        failures = [item for item in report.items if item.status == "failed"]
        if failures:
            lines.extend(("", *(f"{item.client_name}: {item.error}" for item in failures[:5])))
            QMessageBox.warning(self, "Save copy complete", "\n".join(lines))
        else:
            QMessageBox.information(self, "Save copy complete", "\n".join(lines))

    def _export_pdb(self):
        record = self._selected_record()
        if record is None:
            return
        try:
            save_name = record.save_name or Path(record.file_name).stem
            pdb_name = re.sub(r'[<>:"/\\|?*]+', "_", save_name).strip(". ") or Path(record.file_name).stem
            if self.puzzle_id:
                pdb_name = f"{self.puzzle_id} {pdb_name}"
            pdb_path = os.path.join(self.logs_folder, f"{pdb_name}.pdb")
            export_pdb(record.path, pdb_path)
            QMessageBox.information(self, "Success", f"PDB exported to:\n{pdb_path}")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Error exporting PDB: {exc}")

    def _open_folder(self):
        record = self._selected_record()
        if record is not None:
            open_containing_folder(record.path)

    def _show_record_details(self):
        record = self._selected_record()
        if record is None:
            return
        lines = [
            f"Client: {record.client_name}",
            f"Save: {record.save_name or '—'}",
            f"Player: {record.player_name or '—'}",
            f"Total: {'—' if record.total_score is None else f'{record.total_score:.3f}'}",
            f"Base: {'—' if record.base_score is None else f'{record.base_score:.3f}'}",
            f"File: {record.path}",
        ]
        if record.error:
            lines.extend(("", f"Parse error: {record.error}"))

        dialog = QDialog(self)
        dialog.setWindowTitle("Save details")
        dialog.resize(620, 260)
        layout = QVBoxLayout(dialog)

        details = QPlainTextEdit(dialog)
        details.setReadOnly(True)
        details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        details.setPlainText("\n".join(lines))
        layout.addWidget(details, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _update_status(self):
        if not self.filter_valid:
            return
        if self.mapping_error:
            self.status_label.setText(self.mapping_error)
            return
        all_records = self._records_for_scope("all")
        loaded = sum(record.metadata_loaded for record in all_records)
        errors = sum(bool(record.error) for record in all_records)
        scan_state = "Scan complete" if self.scan_finished else f"Scanning clients {self.scanned_clients}/{len(self.clients)}"
        notices = []
        if self.mapping_warning:
            notices.append(self.mapping_warning)
        if self.catalog.mapping_store.last_error:
            notices.append(self.catalog.mapping_store.last_error)
        if self.catalog.index.disabled_reason:
            notices.append(self.catalog.index.disabled_reason)
        status = (
            f"{scan_state}  |  internal {self.internal_puzzle_id or '…'}"
            f"  |  metadata {loaded}/{len(all_records)}  |  shown {len(self._filtered_records())}"
            + (f"  |  errors {errors + len(self.scan_errors)}" if errors or self.scan_errors else "")
        )
        if notices:
            status += "  |  " + "; ".join(notices)
        self.status_label.setText(status)

    def closeEvent(self, event):
        if self._closed:
            event.accept()
            return
        self._closed = True
        self.generation += 1
        self.event_timer.stop()
        self.filter_timer.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        QtEventPump.unregister_window(self)
        if SaveManagerWindowQt._instance is self:
            SaveManagerWindowQt._instance = None
        event.accept()


def show_save_manager(
    pump_root,
    puzzle_id: str,
    clients_provider: ClientProvider,
    index_path: str,
    logs_folder: str,
    initial_client_path: Optional[str] = None,
    initial_scope: str = "client",
) -> SaveManagerWindowQt:
    instance = SaveManagerWindowQt.get_open_instance()
    if instance is not None and normalize_path(instance.index_path) == normalize_path(index_path):
        instance.clients_provider = clients_provider
        instance.logs_folder = os.path.abspath(logs_folder)
        instance.open_context(puzzle_id, initial_client_path, initial_scope)
        return instance
    if instance is not None:
        instance.close()
    return SaveManagerWindowQt(
        pump_root,
        puzzle_id,
        clients_provider,
        index_path,
        logs_folder,
        initial_client_path=initial_client_path,
        initial_scope=initial_scope,
    )


__all__ = ["CopyTargetsDialog", "SaveManagerWindowQt", "show_save_manager"]
