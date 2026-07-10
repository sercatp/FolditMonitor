import os
import re
from typing import Any, Iterable, Optional

from stats_domain import parse_numeric_score, score_current_text


def score_int(value: Any) -> Optional[int]:
    numeric = parse_numeric_score(value)
    if numeric is None:
        numeric = parse_numeric_score(score_current_text(value))
    if numeric is None:
        return None
    return int(numeric)


def _casefold_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _compact_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z]+", "", _casefold_text(value))


def _token_present(filename: str, token: str) -> bool:
    clean_token = str(token or "").strip()
    if not clean_token:
        return False
    return re.search(
        rf"(?<![0-9A-Za-z]){re.escape(clean_token)}(?![0-9A-Za-z])",
        filename,
        flags=re.IGNORECASE,
    ) is not None


def _client_aliases(client_name: Any) -> set[str]:
    clean = str(client_name or "").strip()
    aliases = {clean}
    aliases.add(re.sub(r"foldit", "f", clean, flags=re.IGNORECASE))
    aliases.add(clean.replace("oldit", ""))
    aliases.add(clean.replace("Oldit", ""))
    return {_casefold_text(alias) for alias in aliases if str(alias or "").strip()}


def _client_matches(filename: str, client_name: Any) -> bool:
    folded = _casefold_text(filename)
    compact = _compact_text(filename)
    return any(alias in folded or _compact_text(alias) in compact for alias in _client_aliases(client_name))


def _script_matches(filename: str, script_type: Any) -> bool:
    clean_script = str(script_type or "").strip()
    if not clean_script:
        return False
    folded_filename = _casefold_text(filename)
    folded_script = _casefold_text(clean_script)
    return folded_script in folded_filename or _compact_text(clean_script) in _compact_text(filename)


def filename_matches_log_query(filename: str, query: dict[str, Any]) -> bool:
    if not str(filename or "").casefold().endswith(".txt"):
        return False

    puzzle_id = str(query.get("puzzle_id") or "").strip()
    script_type = str(query.get("script_type") or "").strip()
    client_name = str(query.get("client_name") or "").strip()
    query_score = score_int(query.get("score"))

    if not client_name or not puzzle_id or not script_type or query_score is None:
        return False
    if not _client_matches(filename, client_name):
        return False
    if not _token_present(filename, puzzle_id):
        return False
    if not _script_matches(filename, script_type):
        return False
    return _token_present(filename, str(query_score))


def values_match_log_query(
    query: dict[str, Any],
    *,
    client_name: Any,
    puzzle_id: Any,
    script_type: Any,
    score: Any,
) -> bool:
    query_client = str(query.get("client_name") or "").strip()
    query_puzzle = str(query.get("puzzle_id") or "").strip()
    query_script = str(query.get("script_type") or "").strip()
    query_score = score_int(query.get("score"))
    value_score = score_int(score)

    if not query_client or not query_puzzle or not query_script:
        return False
    if query_score is None or value_score is None or query_score != value_score:
        return False
    if str(query_puzzle).strip() != str(puzzle_id or "").strip():
        return False
    if _compact_text(query_client) != _compact_text(client_name):
        return False
    return _compact_text(query_script) == _compact_text(script_type)


def find_matching_log_file(query: dict[str, Any], folders: Iterable[str]) -> Optional[str]:
    best: Optional[tuple[int, int, str]] = None

    for folder in folders:
        clean_folder = str(folder or "").strip()
        if not clean_folder or not os.path.isdir(clean_folder):
            continue
        try:
            names = os.listdir(clean_folder)
        except OSError:
            continue
        for name in names:
            if not filename_matches_log_query(name, query):
                continue
            path = os.path.join(clean_folder, name)
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            key = (int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))), int(stat.st_size), path)
            if best is None or key > best:
                best = key

    return best[2] if best else None
