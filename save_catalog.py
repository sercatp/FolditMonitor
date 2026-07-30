import csv
import datetime
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from collections import Counter, defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from savefile_api import FolditSaveSummary, get_save_summary


INDEX_SCHEMA_VERSION = 3
ACTIVE_PUZZLE_RE = re.compile(rb"Loading puzzle\s+(\d+)", re.IGNORECASE)
PUZZLE_MAP_FIELDS = ("public_id", "internal_id", "source", "first_seen", "last_verified")


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass(frozen=True)
class ClientLocation:
    name: str
    path: str
    running: bool = False
    active_puzzle_id: str = ""


@dataclass
class SaveRecord:
    path: str
    client_name: str
    client_path: str
    puzzle_id: str
    internal_puzzle_id: str
    file_name: str
    size: int
    mtime_ns: int
    modified: float
    save_name: str = ""
    player_name: str = ""
    base_score: Optional[float] = None
    bonus_score: Optional[float] = None
    total_score: Optional[float] = None
    metadata_loaded: bool = False
    error: str = ""


@dataclass(frozen=True)
class CopyItemResult:
    client_name: str
    target_path: str
    status: str
    error: str = ""


@dataclass
class CopyReport:
    items: List[CopyItemResult] = field(default_factory=list)

    @property
    def copied(self) -> int:
        return sum(item.status == "copied" for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)


@dataclass(frozen=True)
class PuzzleMapping:
    public_id: str
    internal_id: str
    source: str
    first_seen: str
    last_verified: str


@dataclass(frozen=True)
class PuzzleResolution:
    internal_ids: Tuple[str, ...]
    source: str = ""
    warning: str = ""


class PuzzleMappingStore:
    """Small human-editable public/internal puzzle mapping stored as CSV.

    Multiple internal ids for one public id are deliberately allowed. A conflict
    therefore expands the scan instead of making Save Manager unavailable.
    """

    def __init__(self, csv_path: str, legacy_db_path: Optional[str] = None):
        self.csv_path = os.path.abspath(csv_path)
        self.legacy_db_path = os.path.abspath(legacy_db_path) if legacy_db_path else None
        self._lock = threading.RLock()
        self.last_error = ""

    @staticmethod
    def _timestamp() -> str:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _source_rank(source: str) -> int:
        return {
            "manual": 4,
            "active-log": 3,
            "legacy-sqlite": 2,
            "timestamp-inference": 1,
        }.get(str(source).strip().casefold(), 0)

    def _legacy_rows(self) -> List[PuzzleMapping]:
        if not self.legacy_db_path or not os.path.isfile(self.legacy_db_path):
            return []
        try:
            with closing(sqlite3.connect(self.legacy_db_path, timeout=0.25)) as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version >= INDEX_SCHEMA_VERSION:
                    return []
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='puzzle_map'"
                ).fetchone()
                if table is None:
                    return []
                rows = connection.execute(
                    "SELECT public_puzzle_id, internal_puzzle_id, source, learned_at FROM puzzle_map"
                ).fetchall()
        except Exception:
            return []

        result: List[PuzzleMapping] = []
        for public_id, internal_id, _source, learned_at in rows:
            try:
                observed = datetime.datetime.fromtimestamp(
                    float(learned_at), datetime.timezone.utc
                ).replace(microsecond=0).isoformat()
            except (TypeError, ValueError, OSError):
                observed = self._timestamp()
            if str(public_id).strip() and str(internal_id).strip():
                result.append(
                    PuzzleMapping(
                        str(public_id).strip(),
                        str(internal_id).strip(),
                        "legacy-sqlite",
                        observed,
                        observed,
                    )
                )
        return result

    def _read_unlocked(self) -> List[PuzzleMapping]:
        rows: List[PuzzleMapping] = []
        csv_exists = os.path.exists(self.csv_path)
        if csv_exists:
            try:
                with open(self.csv_path, "r", encoding="utf-8-sig", newline="") as handle:
                    for raw in csv.DictReader(handle):
                        public_id = str(raw.get("public_id", "")).strip()
                        internal_id = str(raw.get("internal_id", "")).strip()
                        if not public_id or not internal_id:
                            continue
                        first_seen = str(raw.get("first_seen", "")).strip() or self._timestamp()
                        last_verified = str(raw.get("last_verified", "")).strip() or first_seen
                        rows.append(
                            PuzzleMapping(
                                public_id,
                                internal_id,
                                str(raw.get("source", "")).strip() or "manual",
                                first_seen,
                                last_verified,
                            )
                        )
                self.last_error = ""
            except Exception as exc:
                self.last_error = f"Puzzle map could not be read: {exc}"
                return []
        else:
            rows = self._legacy_rows()
            if rows:
                try:
                    self._write_unlocked(rows)
                except Exception as exc:
                    self.last_error = f"Legacy puzzle map could not be migrated: {exc}"

        deduplicated: Dict[Tuple[str, str], PuzzleMapping] = {}
        for row in rows:
            deduplicated[(row.public_id, row.internal_id)] = row
        return list(deduplicated.values())

    def _write_unlocked(self, rows: Sequence[PuzzleMapping]):
        folder = os.path.dirname(self.csv_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8-sig",
                newline="",
                dir=folder or None,
                prefix=".puzzle_map_",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                writer = csv.DictWriter(handle, fieldnames=PUZZLE_MAP_FIELDS, lineterminator="\n")
                writer.writeheader()
                for row in sorted(rows, key=lambda item: (item.public_id, item.internal_id)):
                    writer.writerow(
                        {
                            "public_id": row.public_id,
                            "internal_id": row.internal_id,
                            "source": row.source,
                            "first_seen": row.first_seen,
                            "last_verified": row.last_verified,
                        }
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.csv_path)
            self.last_error = ""
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def get(self, public_puzzle_id: str) -> List[PuzzleMapping]:
        clean_public_id = str(public_puzzle_id).strip()
        with self._lock:
            matches = [row for row in self._read_unlocked() if row.public_id == clean_public_id]
        matches.sort(
            key=lambda row: (self._source_rank(row.source), row.last_verified, row.internal_id),
            reverse=True,
        )
        return matches

    def add(self, public_puzzle_id: str, internal_puzzle_id: str, source: str) -> bool:
        public_id = str(public_puzzle_id).strip()
        internal_id = str(internal_puzzle_id).strip()
        clean_source = str(source).strip() or "manual"
        if not public_id or not internal_id:
            return False
        now = self._timestamp()
        with self._lock:
            rows = self._read_unlocked()
            updated: List[PuzzleMapping] = []
            found = False
            for row in rows:
                if row.public_id == public_id and row.internal_id == internal_id:
                    stronger_source = (
                        clean_source
                        if self._source_rank(clean_source) >= self._source_rank(row.source)
                        else row.source
                    )
                    updated.append(
                        PuzzleMapping(public_id, internal_id, stronger_source, row.first_seen, now)
                    )
                    found = True
                else:
                    updated.append(row)
            if not found:
                updated.append(PuzzleMapping(public_id, internal_id, clean_source, now, now))
            try:
                self._write_unlocked(updated)
                return True
            except Exception as exc:
                self.last_error = f"Puzzle map could not be written: {exc}"
                return False


class SaveIndex:
    """Disposable, size-limited SQLite cache which always fails open."""

    def __init__(self, db_path: str, max_rows: int = 250_000, max_age_days: int = 365):
        self.db_path = os.path.abspath(db_path)
        self.max_rows = max(0, int(max_rows))
        self.max_age_days = max(0, int(max_age_days))
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._maintenance_done = False
        self.disabled_reason = ""

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=1.0)
        connection.row_factory = sqlite3.Row
        return connection

    @property
    def available(self) -> bool:
        return not bool(self.disabled_reason)

    def _disable(self, exc: Exception):
        if not self.disabled_reason:
            self.disabled_reason = f"Save cache disabled: {exc}"

    def _ensure_schema(self) -> bool:
        if self.disabled_reason:
            return False
        with self._schema_lock:
            if self._schema_ready:
                return True
            try:
                db_folder = os.path.dirname(self.db_path)
                if db_folder:
                    os.makedirs(db_folder, exist_ok=True)
                with closing(self._connect()) as connection, connection:
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version not in (0, INDEX_SCHEMA_VERSION):
                        connection.execute("DROP TABLE IF EXISTS save_index")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS save_index (
                            path_key TEXT PRIMARY KEY,
                            path TEXT NOT NULL,
                            client_name TEXT NOT NULL,
                            client_path_key TEXT NOT NULL,
                            puzzle_id TEXT NOT NULL,
                            internal_puzzle_id TEXT NOT NULL,
                            file_name TEXT NOT NULL,
                            size INTEGER NOT NULL,
                            mtime_ns INTEGER NOT NULL,
                            modified REAL NOT NULL,
                            save_name TEXT NOT NULL,
                            player_name TEXT NOT NULL,
                            base_score REAL,
                            bonus_score REAL,
                            total_score REAL,
                            error TEXT NOT NULL DEFAULT '',
                            last_seen_at REAL NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS save_index_puzzle_client "
                        "ON save_index (puzzle_id, client_path_key)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS save_index_last_seen ON save_index (last_seen_at)"
                    )
                    connection.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
                self._schema_ready = True
            except Exception as exc:
                self._disable(exc)
                return False
        self._maintain_once()
        return True

    def _maintain_once(self):
        if self._maintenance_done or not self._schema_ready or self.disabled_reason:
            return
        self._maintenance_done = True
        try:
            with closing(self._connect()) as connection, connection:
                if self.max_age_days:
                    cutoff = time.time() - self.max_age_days * 86400
                    connection.execute("DELETE FROM save_index WHERE last_seen_at < ?", (cutoff,))
                if self.max_rows:
                    count = int(connection.execute("SELECT COUNT(*) FROM save_index").fetchone()[0])
                    excess = count - self.max_rows
                    if excess > 0:
                        connection.execute(
                            "DELETE FROM save_index WHERE path_key IN "
                            "(SELECT path_key FROM save_index ORDER BY last_seen_at ASC LIMIT ?)",
                            (excess,),
                        )
        except Exception as exc:
            self._disable(exc)

    def lookup(self, record: SaveRecord) -> Optional[SaveRecord]:
        if not self._ensure_schema():
            return None
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT * FROM save_index WHERE path_key = ? AND size = ? AND mtime_ns = ?",
                    (normalize_path(record.path), record.size, record.mtime_ns),
                ).fetchone()
        except Exception as exc:
            self._disable(exc)
            return None
        if row is None:
            return None
        record.save_name = str(row["save_name"] or "")
        record.player_name = str(row["player_name"] or "")
        record.base_score = row["base_score"]
        record.bonus_score = row["bonus_score"]
        record.total_score = row["total_score"]
        record.error = str(row["error"] or "")
        record.metadata_loaded = True
        return record

    def upsert(self, record: SaveRecord):
        if not self._ensure_schema():
            return
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO save_index (
                        path_key, path, client_name, client_path_key, puzzle_id, internal_puzzle_id,
                        file_name, size, mtime_ns, modified, save_name, player_name,
                        base_score, bonus_score, total_score, error, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path_key) DO UPDATE SET
                        path=excluded.path,
                        client_name=excluded.client_name,
                        client_path_key=excluded.client_path_key,
                        puzzle_id=excluded.puzzle_id,
                        internal_puzzle_id=excluded.internal_puzzle_id,
                        file_name=excluded.file_name,
                        size=excluded.size,
                        mtime_ns=excluded.mtime_ns,
                        modified=excluded.modified,
                        save_name=excluded.save_name,
                        player_name=excluded.player_name,
                        base_score=excluded.base_score,
                        bonus_score=excluded.bonus_score,
                        total_score=excluded.total_score,
                        error=excluded.error,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        normalize_path(record.path),
                        record.path,
                        record.client_name,
                        normalize_path(record.client_path),
                        record.puzzle_id,
                        record.internal_puzzle_id,
                        record.file_name,
                        record.size,
                        record.mtime_ns,
                        record.modified,
                        record.save_name,
                        record.player_name,
                        record.base_score,
                        record.bonus_score,
                        record.total_score,
                        record.error,
                        time.time(),
                    ),
                )
        except Exception as exc:
            self._disable(exc)

    def prune_client_puzzle(self, client_path: str, puzzle_id: str, live_paths: Iterable[str]):
        live_keys = {normalize_path(path) for path in live_paths}
        client_key = normalize_path(client_path)
        if not self._ensure_schema():
            return
        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    "SELECT path_key FROM save_index WHERE puzzle_id = ? AND client_path_key = ?",
                    (str(puzzle_id), client_key),
                ).fetchall()
                stale = [(row["path_key"],) for row in rows if row["path_key"] not in live_keys]
                if stale:
                    connection.executemany("DELETE FROM save_index WHERE path_key = ?", stale)
                if live_keys:
                    now = time.time()
                    connection.executemany(
                        "UPDATE save_index SET last_seen_at = ? WHERE path_key = ?",
                        ((now, path_key) for path_key in live_keys),
                    )
        except Exception as exc:
            self._disable(exc)


class SaveCatalog:
    def __init__(
        self,
        index: SaveIndex,
        summary_reader: Callable[[str], FolditSaveSummary] = get_save_summary,
        mapping_store: Optional[PuzzleMappingStore] = None,
    ):
        self.index = index
        self.summary_reader = summary_reader
        self.mapping_store = mapping_store or PuzzleMappingStore(
            os.path.join(os.path.dirname(index.db_path), "puzzle_map.csv"),
            legacy_db_path=index.db_path,
        )

    @staticmethod
    def read_active_internal_puzzle_id(client_path: str) -> Optional[str]:
        log_path = os.path.join(client_path, "log.txt")
        try:
            with open(log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                position = size
                data = b""
                while position > 0 and len(data) < 2 * 1024 * 1024:
                    chunk_size = min(64 * 1024, position, 2 * 1024 * 1024 - len(data))
                    position -= chunk_size
                    handle.seek(position)
                    data = handle.read(chunk_size) + data
                    matches = list(ACTIVE_PUZZLE_RE.finditer(data))
                    if matches:
                        return matches[-1].group(1).decode("ascii")
        except OSError:
            return None
        return None

    @staticmethod
    def _managed_log_times(client_path: str, public_puzzle_id: str) -> List[float]:
        pattern = re.compile(rf"^[^.]+\.{re.escape(str(public_puzzle_id))}(?:\s|\.)", re.IGNORECASE)
        times: List[float] = []
        try:
            entries = os.scandir(client_path)
        except OSError:
            return times
        with entries:
            for entry in entries:
                if not pattern.match(entry.name) or not entry.name.casefold().endswith(".txt"):
                    continue
                try:
                    if entry.is_file():
                        times.append(float(entry.stat().st_mtime))
                except OSError:
                    continue
        return times

    @staticmethod
    def _internal_puzzle_dirs(client_path: str) -> List[os.DirEntry]:
        puzzle_root = os.path.join(client_path, "puzzles")
        try:
            with os.scandir(puzzle_root) as iterator:
                entries = [entry for entry in iterator if entry.is_dir() and entry.name.isdigit()]
        except OSError:
            return []
        try:
            entries.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
        except OSError:
            pass
        return entries

    @classmethod
    def _infer_client_mapping(
        cls,
        client: ClientLocation,
        public_puzzle_id: str,
    ) -> Optional[Tuple[str, float, float]]:
        log_times = cls._managed_log_times(client.path, public_puzzle_id)
        if not log_times:
            return None
        scored: List[Tuple[float, str]] = []
        for puzzle_dir in cls._internal_puzzle_dirs(client.path):
            best_distance: Optional[float] = None
            for folder, _dirs, files in os.walk(puzzle_dir.path):
                for file_name in files:
                    if not file_name.casefold().endswith(".ir_solution"):
                        continue
                    try:
                        modified = os.path.getmtime(os.path.join(folder, file_name))
                    except OSError:
                        continue
                    distance = min(abs(modified - log_time) for log_time in log_times)
                    if best_distance is None or distance < best_distance:
                        best_distance = distance
            if best_distance is not None:
                scored.append((best_distance, puzzle_dir.name.lstrip("0") or "0"))
        if not scored:
            return None
        scored.sort()
        second_distance = scored[1][0] if len(scored) > 1 else float("inf")
        return scored[0][1], scored[0][0], second_distance

    def resolve_internal_puzzle_ids(
        self,
        public_puzzle_id: str,
        clients: Sequence[ClientLocation],
    ) -> PuzzleResolution:
        clean_public_id = str(public_puzzle_id).strip()

        # CSV is the primary source. log.txt is touched only for an unknown pair.
        mapped = self.mapping_store.get(clean_public_id)
        if mapped:
            internal_ids = tuple(dict.fromkeys(row.internal_id for row in mapped))
            warning = ""
            if len(internal_ids) > 1:
                warning = f"Multiple mappings in puzzle_map.csv: {', '.join(internal_ids)}"
            return PuzzleResolution(internal_ids, "csv", warning)

        active_candidates: List[str] = []
        for client in clients:
            if not client.running or str(client.active_puzzle_id).strip() != clean_public_id:
                continue
            internal_id = self.read_active_internal_puzzle_id(client.path)
            if internal_id:
                active_candidates.append(internal_id)
        if active_candidates:
            counts = Counter(active_candidates)
            internal_ids = tuple(
                internal_id
                for internal_id, _count in sorted(
                    counts.items(), key=lambda item: (-item[1], item[0])
                )
            )
            for internal_id in internal_ids:
                self.mapping_store.add(clean_public_id, internal_id, "active-log")
            warning = ""
            if len(internal_ids) > 1:
                warning = f"Clients reported multiple internal ids: {', '.join(internal_ids)}; showing all."
            return PuzzleResolution(internal_ids, "active-log", warning)

        # Historical fallback: managed log filenames carry the public id. Match
        # their timestamps to save activity under internal puzzle directories.
        votes: List[Tuple[str, float, float]] = []
        ordered_clients = sorted(clients, key=lambda client: (not client.running, client.name.casefold()))
        for client in ordered_clients:
            evidence = self._infer_client_mapping(client, clean_public_id)
            if evidence is None:
                continue
            votes.append(evidence)
            if len(votes) >= 5:
                break
        if not votes:
            return PuzzleResolution(())

        grouped_distance: Dict[str, float] = defaultdict(float)
        counts = Counter()
        for internal_id, best_distance, _second_distance in votes:
            counts[internal_id] += 1
            grouped_distance[internal_id] += best_distance
        internal_ids = tuple(
            sorted(counts, key=lambda internal_id: (-counts[internal_id], grouped_distance[internal_id], internal_id))
        )
        for internal_id in internal_ids:
            self.mapping_store.add(clean_public_id, internal_id, "timestamp-inference")
        warning = ""
        if len(internal_ids) > 1:
            warning = f"Historical evidence found multiple internal ids: {', '.join(internal_ids)}; showing all."
        return PuzzleResolution(internal_ids, "timestamp-inference", warning)

    def resolve_internal_puzzle_id(
        self,
        public_puzzle_id: str,
        clients: Sequence[ClientLocation],
    ) -> Optional[str]:
        """Compatibility helper returning the first candidate."""
        resolution = self.resolve_internal_puzzle_ids(public_puzzle_id, clients)
        return resolution.internal_ids[0] if resolution.internal_ids else None

    @staticmethod
    def _find_internal_puzzle_dir(client_path: str, internal_puzzle_id: str) -> Optional[str]:
        puzzle_root = os.path.join(client_path, "puzzles")
        try:
            padded_name = f"{int(internal_puzzle_id):010d}"
        except (TypeError, ValueError):
            padded_name = str(internal_puzzle_id).strip()
        direct_path = os.path.join(puzzle_root, padded_name)
        if os.path.isdir(direct_path):
            return direct_path
        try:
            for entry in os.scandir(puzzle_root):
                if entry.is_dir() and (entry.name.lstrip("0") or "0") == str(internal_puzzle_id).lstrip("0"):
                    return entry.path
        except OSError:
            pass
        return None

    def scan_client(
        self,
        puzzle_id: str,
        client: ClientLocation,
        internal_puzzle_id: Optional[Union[str, Sequence[str]]] = None,
        include_quick_auto: bool = False,
    ) -> List[SaveRecord]:
        clean_puzzle_id = str(puzzle_id).strip()
        raw_internal_ids: Sequence[str]
        if internal_puzzle_id is None:
            raw_internal_ids = (clean_puzzle_id,)
        elif isinstance(internal_puzzle_id, str):
            raw_internal_ids = (internal_puzzle_id,)
        else:
            raw_internal_ids = internal_puzzle_id
        clean_internal_ids = tuple(
            dict.fromkeys(str(value).strip() for value in raw_internal_ids if str(value).strip())
        )
        suffix = ".ir_solution"
        records: List[SaveRecord] = []

        candidate_paths: Dict[str, Tuple[str, str]] = {}
        try:
            entries = os.scandir(client.path)
        except OSError:
            entries = None
        if entries is not None:
            with entries:
                for entry in entries:
                    file_name_lower = entry.name.casefold()
                    if not file_name_lower.endswith(suffix):
                        continue
                    matched_internal_id = next(
                        (
                            internal_id
                            for internal_id in clean_internal_ids
                            if file_name_lower.startswith(f"puzzle_{internal_id}_time_".casefold())
                        ),
                        None,
                    )
                    if matched_internal_id is None:
                        # A root-level unnamed save cannot be assigned to a puzzle reliably.
                        continue
                    try:
                        if entry.is_file():
                            candidate_paths[normalize_path(entry.path)] = (entry.path, matched_internal_id)
                    except OSError:
                        continue

        for clean_internal_id in clean_internal_ids:
            internal_dir = self._find_internal_puzzle_dir(client.path, clean_internal_id)
            if not internal_dir:
                continue
            for folder, _dirs, files in os.walk(internal_dir):
                for file_name in files:
                    file_name_lower = file_name.casefold()
                    if not file_name_lower.endswith(suffix):
                        continue
                    if not include_quick_auto and not any(
                        puzzle_number.casefold() in file_name_lower
                        for puzzle_number in (clean_puzzle_id, clean_internal_id)
                    ):
                        continue
                    path = os.path.join(folder, file_name)
                    candidate_paths[normalize_path(path)] = (path, clean_internal_id)

        for path, clean_internal_id in candidate_paths.values():
            try:
                stat = os.stat(path)
            except OSError:
                continue
            file_name = os.path.basename(path)
            record = SaveRecord(
                path=path,
                client_name=client.name,
                client_path=client.path,
                puzzle_id=clean_puzzle_id,
                internal_puzzle_id=clean_internal_id,
                file_name=file_name,
                size=int(stat.st_size),
                mtime_ns=int(stat.st_mtime_ns),
                modified=float(stat.st_mtime),
            )
            self.index.lookup(record)
            records.append(record)

        self.index.prune_client_puzzle(client.path, clean_puzzle_id, (record.path for record in records))
        records.sort(key=lambda record: (record.modified, record.file_name.casefold()), reverse=True)
        return records

    def load_metadata(self, record: SaveRecord, force_error_retry: bool = False) -> SaveRecord:
        if record.metadata_loaded and not (force_error_retry and record.error):
            return record
        try:
            summary = self.summary_reader(record.path)
            if str(summary.puzzle_id).strip() != record.internal_puzzle_id:
                raise ValueError(
                    f"Puzzle mismatch: expected {record.internal_puzzle_id}, save contains {summary.puzzle_id}"
                )
            record.save_name = " ".join(str(summary.save_name).split())
            record.player_name = " ".join(str(summary.player_name).split())
            record.base_score = float(summary.base_score)
            record.bonus_score = float(summary.bonus_score)
            record.total_score = float(summary.total_score)
            record.error = ""
        except Exception as exc:
            record.save_name = ""
            record.player_name = ""
            record.base_score = None
            record.bonus_score = None
            record.total_score = None
            record.error = str(exc)
        record.metadata_loaded = True
        self.index.upsert(record)
        return record

    @staticmethod
    def filter_records(
        records: Iterable[SaveRecord],
        name_query: str = "",
        score_field: str = "total",
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> List[SaveRecord]:
        query = str(name_query or "").strip().casefold()
        use_base = str(score_field).strip().casefold() == "base"
        result: List[SaveRecord] = []
        for record in records:
            if query and query not in record.save_name.casefold() and query not in record.file_name.casefold():
                continue
            score = record.base_score if use_base else record.total_score
            if minimum is not None and (score is None or score < minimum):
                continue
            if maximum is not None and (score is None or score > maximum):
                continue
            result.append(record)
        return result

    @staticmethod
    def copy_record(record: SaveRecord, targets: Sequence[ClientLocation]) -> CopyReport:
        report = CopyReport()
        source_key = normalize_path(record.client_path)
        for target in targets:
            target_path = os.path.join(target.path, record.file_name)
            if normalize_path(target.path) == source_key or os.path.exists(target_path):
                report.items.append(CopyItemResult(target.name, target_path, "skipped"))
                continue
            try:
                if not os.path.isdir(target.path):
                    raise OSError(f"Target folder not found: {target.path}")
                shutil.copy2(record.path, target_path)
                report.items.append(CopyItemResult(target.name, target_path, "copied"))
            except Exception as exc:
                report.items.append(CopyItemResult(target.name, target_path, "failed", str(exc)))
        return report


__all__ = [
    "ClientLocation",
    "CopyItemResult",
    "CopyReport",
    "PuzzleMapping",
    "PuzzleMappingStore",
    "PuzzleResolution",
    "SaveCatalog",
    "SaveIndex",
    "SaveRecord",
    "normalize_path",
]
