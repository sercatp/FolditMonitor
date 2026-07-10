#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
foldit_api.py

Standalone helper API for Foldit `.ir_solution` files.

This file is self-contained. You can copy it into another project without also
copying `foldit_extract_save.py`.

Import examples:

    from foldit_api import (
        get_basic_info,
        get_bonus_score,
        get_disulfide_info,
        get_player_name,
        get_save_name,
        get_foldit_score,
        export_pdb,
    )

    save_path = "C:/Foldit/foldit_scripts/puzzle_2014286_time_1773822868.ir_solution"

    info = get_basic_info(save_path)
    print(info.player_name)
    print(info.save_name)
    print(info.foldit_score)

    player_name = get_player_name(save_path)
    save_name = get_save_name(save_path)
    foldit_score = get_foldit_score(save_path)
    bonus_score = get_bonus_score(save_path)
    disulfide_info = get_disulfide_info(save_path)

    pdb_path = export_pdb(save_path)
    pdb_path = export_pdb(save_path, "C:/temp/my_model.pdb")

Available functions:
    get_basic_info(save_path) -> FolditBasicInfo
    get_bonus_score(save_path) -> float
    get_disulfide_info(save_path) -> FolditDisulfideInfo
    get_player_name(save_path) -> str
    get_save_name(save_path) -> str
    get_foldit_score(save_path) -> float
    export_pdb(save_path, output_path=None) -> str

Behavior:
    - `get_basic_info()` reads the save once and returns all 3 metadata fields.
    - The metadata functions do not export a PDB.
    - `export_pdb()` only exports the PDB and returns the written file path.
    - On parse problems, the module raises `FolditApiError`.
"""

import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


PathLike = Union[str, Path]
MAX_RESIDUES = 10000
COORD_ABS_MAX = 500.0


ATOM_MAP = {
    "ALA": ["N", "CA", "C", "O", "CB"],
    "ARG": ["N", "CA", "C", "O", "CB", "CG", "CD", "NE", "CZ", "NH1", "NH2"],
    "ASN": ["N", "CA", "C", "O", "CB", "CG", "OD1", "ND2"],
    "ASP": ["N", "CA", "C", "O", "CB", "CG", "OD1", "OD2"],
    "CYS": ["N", "CA", "C", "O", "CB", "SG"],
    "GLN": ["N", "CA", "C", "O", "CB", "CG", "CD", "OE1", "NE2"],
    "GLU": ["N", "CA", "C", "O", "CB", "CG", "CD", "OE1", "OE2"],
    "GLY": ["N", "CA", "C", "O"],
    "HIS": ["N", "CA", "C", "O", "CB", "CG", "ND1", "CD2", "CE1", "NE2"],
    "ILE": ["N", "CA", "C", "O", "CB", "CG1", "CG2", "CD1"],
    "LEU": ["N", "CA", "C", "O", "CB", "CG", "CD1", "CD2"],
    "LYS": ["N", "CA", "C", "O", "CB", "CG", "CD", "CE", "NZ"],
    "MET": ["N", "CA", "C", "O", "CB", "CG", "SD", "CE"],
    "PHE": ["N", "CA", "C", "O", "CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ"],
    "PRO": ["N", "CA", "C", "O", "CB", "CG", "CD"],
    "SER": ["N", "CA", "C", "O", "CB", "OG"],
    "THR": ["N", "CA", "C", "O", "CB", "OG1", "CG2"],
    "TRP": ["N", "CA", "C", "O", "CB", "CG", "CD1", "CD2", "NE1", "CE2", "CE3", "CZ2", "CZ3", "CH2"],
    "TYR": ["N", "CA", "C", "O", "CB", "CG", "CD1", "CD2", "CE1", "CE2", "CZ", "OH"],
    "VAL": ["N", "CA", "C", "O", "CB", "CG1", "CG2"],
}


ATOM_PARENTS = {
    "ALA": [None, "N", "CA", "C", "CA"],
    "ARG": [None, "N", "CA", "C", "CA", "CB", "CG", "CD", "NE", "CZ", "CZ"],
    "ASN": [None, "N", "CA", "C", "CA", "CB", "CG", "CG"],
    "ASP": [None, "N", "CA", "C", "CA", "CB", "CG", "CG"],
    "CYS": [None, "N", "CA", "C", "CA", "CB"],
    "GLN": [None, "N", "CA", "C", "CA", "CB", "CG", "CD", "CD"],
    "GLU": [None, "N", "CA", "C", "CA", "CB", "CG", "CD", "CD"],
    "GLY": [None, "N", "CA", "C"],
    "HIS": [None, "N", "CA", "C", "CA", "CB", "CG", "CG", "ND1", "CD2"],
    "ILE": [None, "N", "CA", "C", "CA", "CB", "CB", "CG1"],
    "LEU": [None, "N", "CA", "C", "CA", "CB", "CG", "CG"],
    "LYS": [None, "N", "CA", "C", "CA", "CB", "CG", "CD", "CE"],
    "MET": [None, "N", "CA", "C", "CA", "CB", "CG", "SD"],
    "PHE": [None, "N", "CA", "C", "CA", "CB", "CG", "CG", "CD1", "CD2", "CE1"],
    "PRO": [None, "N", "CA", "C", "CA", "CB", "CG"],
    "SER": [None, "N", "CA", "C", "CA", "CB"],
    "THR": [None, "N", "CA", "C", "CA", "CB", "CB"],
    "TRP": [None, "N", "CA", "C", "CA", "CB", "CG", "CG", "CD1", "CD2", "CD2", "CE2", "CE3", "CZ2"],
    "TYR": [None, "N", "CA", "C", "CA", "CB", "CG", "CG", "CD1", "CD2", "CE1", "CZ"],
    "VAL": [None, "N", "CA", "C", "CA", "CB", "CB"],
}


class FolditApiError(RuntimeError):
    """Raised when the helper cannot extract the requested data."""


@dataclass(frozen=True)
class FolditBasicInfo:
    player_name: str
    save_name: str
    foldit_score: float


@dataclass(frozen=True)
class FolditDisulfideInfo:
    endpoint_count: int
    bond_count: int
    residue_offsets: Tuple[int, ...]
    has_unpaired_endpoint: bool


@dataclass
class TaggedString:
    tag: str
    tag_off: int
    strlen: int
    payload_off: int
    payload: str


@dataclass
class ResiTag:
    tag_off: int
    count: int


@dataclass
class EnergyBlock:
    off: int
    energy_off: int
    total_energy: float
    n_off: int
    n: int
    arr_off: int
    per_residue: List[float]
    end_off: int
    variant: str


@dataclass
class PuzzlePlayerBlock:
    off: int
    puzzle_id: int
    maybe_one: int
    name_len: int
    player_name: str
    next_u32s: List[int]
    end_off: int


def format_pdb_atom_line(
    serial: int,
    atom_name: str,
    res_name: str,
    chain_id: str,
    res_seq: int,
    x: float,
    y: float,
    z: float,
    occupancy: float = 1.00,
    temp_factor: float = 0.00,
    element: Optional[str] = None,
) -> str:
    if element is None:
        element = atom_name[:1].strip().upper()
    return (
        f"ATOM  {serial:5d} {atom_name:>4s} {res_name:>3s} {chain_id:1s}"
        f"{res_seq:4d}    {x:8.3f}{y:8.3f}{z:8.3f}"
        f"{occupancy:6.2f}{temp_factor:6.2f}          {element:>2s}"
    )


def u32(data: bytes, off: int) -> Optional[int]:
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]


def f64(data: bytes, off: int) -> Optional[float]:
    if off < 0 or off + 8 > len(data):
        return None
    return struct.unpack_from("<d", data, off)[0]


def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def is_plausible_residue_count(n: int) -> bool:
    return 2 <= n <= MAX_RESIDUES


def find_tagged_string(
    data: bytes,
    tag: bytes,
    prefer_after: Optional[int] = None,
) -> Optional[TaggedString]:
    positions = []
    start = 0
    while True:
        pos = data.find(tag, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    if not positions:
        return None

    chosen = None
    if prefer_after is not None:
        for pos in positions:
            if pos >= prefer_after:
                chosen = pos
                break
    if chosen is None:
        chosen = positions[0]

    length = u32(data, chosen + 4)
    if length is None:
        return None
    if length > 2_000_000 or chosen + 8 + length > len(data):
        return None

    payload_bytes = data[chosen + 8 : chosen + 8 + length]
    try:
        payload = payload_bytes.decode("ascii")
    except UnicodeDecodeError:
        payload = payload_bytes.decode("utf-8", errors="replace")

    return TaggedString(
        tag=tag.decode("ascii", errors="replace"),
        tag_off=chosen,
        strlen=length,
        payload_off=chosen + 8,
        payload=payload,
    )


def find_resi_tag(data: bytes, prefer_after: Optional[int] = None) -> Optional[ResiTag]:
    positions = []
    start = 0
    while True:
        pos = data.find(b"RESI", start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + 1
    if not positions:
        return None

    chosen = None
    if prefer_after is not None:
        for pos in positions:
            if pos >= prefer_after:
                chosen = pos
                break
    if chosen is None:
        chosen = positions[0]

    count = u32(data, chosen + 4)
    if count is None or not is_plausible_residue_count(count):
        return None

    return ResiTag(tag_off=chosen, count=count)


def try_energy_variant_a(
    data: bytes,
    off: int,
    n_targets: Optional[set[int]] = None,
) -> Optional[EnergyBlock]:
    energy = f64(data, off + 4)
    count = u32(data, off + 12)
    if energy is None or count is None:
        return None
    if not math.isfinite(energy):
        return None
    if not is_plausible_residue_count(count):
        return None
    if n_targets is not None and count not in n_targets:
        return None

    arr_off = off + 16
    end = arr_off + 8 * count
    if end > len(data):
        return None

    values = [f64(data, arr_off + 8 * i) for i in range(count)]
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    per_residue = [float(v) for v in values if v is not None]
    if abs(sum(per_residue) - energy) > 1e-6 or abs(energy) < 1.0:
        return None

    return EnergyBlock(
        off=off,
        energy_off=off + 4,
        total_energy=energy,
        n_off=off + 12,
        n=count,
        arr_off=arr_off,
        per_residue=per_residue,
        end_off=end,
        variant="A",
    )


def try_energy_variant_b(
    data: bytes,
    off: int,
    n_targets: Optional[set[int]] = None,
) -> Optional[EnergyBlock]:
    blob_len = u32(data, off)
    if blob_len is None or blob_len > 64:
        return None

    energy_off = off + 4 + blob_len
    energy = f64(data, energy_off)
    count = u32(data, energy_off + 8)
    if energy is None or count is None:
        return None
    if not math.isfinite(energy):
        return None
    if not is_plausible_residue_count(count):
        return None
    if n_targets is not None and count not in n_targets:
        return None

    arr_off = energy_off + 12
    end = arr_off + 8 * count
    if end > len(data):
        return None

    values = [f64(data, arr_off + 8 * i) for i in range(count)]
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    per_residue = [float(v) for v in values if v is not None]
    if abs(sum(per_residue) - energy) > 1e-6 or abs(energy) < 1.0:
        return None

    return EnergyBlock(
        off=off,
        energy_off=energy_off,
        total_energy=energy,
        n_off=energy_off + 8,
        n=count,
        arr_off=arr_off,
        per_residue=per_residue,
        end_off=end,
        variant="B",
    )


def try_energy_variant_c(
    data: bytes,
    off: int,
    n_targets: Optional[set[int]] = None,
) -> Optional[EnergyBlock]:
    energy = f64(data, off)
    count = u32(data, off + 8)
    if energy is None or count is None:
        return None
    if not math.isfinite(energy):
        return None
    if not is_plausible_residue_count(count):
        return None
    if n_targets is not None and count not in n_targets:
        return None

    arr_off = off + 12
    end = arr_off + 8 * count
    if end > len(data):
        return None

    values = [f64(data, arr_off + 8 * i) for i in range(count)]
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    per_residue = [float(v) for v in values if v is not None]
    if abs(sum(per_residue) - energy) > 1e-6 or abs(energy) < 1.0:
        return None

    return EnergyBlock(
        off=off,
        energy_off=off,
        total_energy=energy,
        n_off=off + 8,
        n=count,
        arr_off=arr_off,
        per_residue=per_residue,
        end_off=end,
        variant="C",
    )


def try_energy_variant_d(
    data: bytes,
    off: int,
    n_targets: Optional[set[int]] = None,
) -> Optional[EnergyBlock]:
    count = u32(data, off)
    if count is None:
        return None
    if not is_plausible_residue_count(count):
        return None
    if n_targets is not None and count not in n_targets:
        return None

    arr_off = off + 4
    end = arr_off + 8 * count
    if end > len(data):
        return None

    values = [f64(data, arr_off + 8 * i) for i in range(count)]
    if any(v is None or not math.isfinite(v) for v in values):
        return None

    per_residue = [float(v) for v in values if v is not None]
    energy = sum(per_residue)
    if not math.isfinite(energy) or abs(energy) < 1.0:
        return None

    return EnergyBlock(
        off=off,
        energy_off=arr_off,
        total_energy=energy,
        n_off=off,
        n=count,
        arr_off=arr_off,
        per_residue=per_residue,
        end_off=end,
        variant="D",
    )


def find_energy_block(
    data: bytes,
    start: int,
    window: int = 16384,
    n_targets: Optional[Tuple[int, ...]] = None,
) -> Optional[EnergyBlock]:
    begin = clamp(start, 0, len(data))
    end = clamp(start + window, 0, len(data))

    targets = None
    if n_targets:
        targets = {n for n in n_targets if is_plausible_residue_count(n)}
        if not targets:
            targets = None

    for off in range(begin, end):
        block = try_energy_variant_b(data, off, targets)
        if block:
            return block
    for off in range(begin, end):
        block = try_energy_variant_a(data, off, targets)
        if block:
            return block
    for off in range(begin, end):
        block = try_energy_variant_c(data, off, targets)
        if block:
            return block
    for off in range(begin, end):
        block = try_energy_variant_d(data, off, targets)
        if block:
            return block

    for off in range(0, len(data) - 16):
        block = (
            try_energy_variant_b(data, off, targets)
            or try_energy_variant_a(data, off, targets)
            or try_energy_variant_c(data, off, targets)
            or try_energy_variant_d(data, off, targets)
        )
        if block:
            return block
    return None


def find_puzzle_player_block(
    data: bytes,
    start: int,
    window: int = 2048,
) -> Optional[PuzzlePlayerBlock]:
    begin = clamp(start, 0, len(data))
    end = clamp(start + window, 0, len(data))

    for off in range(begin, end):
        pid1 = u32(data, off)
        pid2 = u32(data, off + 4)
        if pid1 is None or pid2 is None or pid1 != pid2:
            continue
        if not (1 <= pid1 <= 10_000_000):
            continue

        maybe_one = u32(data, off + 8)
        name_len = u32(data, off + 12)
        if maybe_one is None or name_len is None:
            continue
        if name_len == 0 or name_len > 512:
            continue
        if off + 16 + name_len > len(data):
            continue

        name_bytes = data[off + 16 : off + 16 + name_len]
        if name_bytes.endswith(b"\x00"):
            name_bytes = name_bytes[:-1]
        player_name = name_bytes.decode("utf-8", errors="replace")
        if not player_name or any(ord(ch) < 32 for ch in player_name):
            continue

        cursor = off + 16 + name_len
        next_u32s = []
        for _ in range(10):
            value = u32(data, cursor)
            if value is None:
                break
            next_u32s.append(value)
            cursor += 4

        return PuzzlePlayerBlock(
            off=off,
            puzzle_id=pid1,
            maybe_one=maybe_one,
            name_len=name_len,
            player_name=player_name,
            next_u32s=next_u32s,
            end_off=cursor,
        )
    return None


def find_residue_coord_candidate(
    block: bytes,
    res_name: str,
    prefer_offset: Optional[int] = None,
) -> Optional[Dict[str, object]]:
    def distance(p1: Tuple[float, float, float], p2: Tuple[float, float, float]) -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))

    best: Optional[Dict[str, object]] = None

    def score_candidate(points: List[Tuple[float, float, float]]) -> Optional[Dict[str, float]]:
        if len(points) < 4:
            return None
        d_n_ca = distance(points[0], points[1])
        d_ca_c = distance(points[1], points[2])
        d_c_o = distance(points[2], points[3])
        if not ((1.35 < d_n_ca < 1.60) and (1.40 < d_ca_c < 1.65) and (1.10 < d_c_o < 1.35)):
            return None

        score = 3.0
        d_ca_cb = None
        if res_name != "GLY" and len(points) >= 5:
            d_ca_cb = distance(points[1], points[4])
            if 1.35 < d_ca_cb < 1.70:
                score += 1.0

        return {
            "score": score,
            "d_n_ca": d_n_ca,
            "d_ca_c": d_ca_c,
            "d_c_o": d_c_o,
            "d_ca_cb": d_ca_cb,
        }

    def consider_run(run_start: int, run_vals: List[float], align: int) -> None:
        nonlocal best
        if len(run_vals) < 12:
            return

        for triple_offset in range(3):
            points = []
            for idx in range(triple_offset, len(run_vals) - 2, 3):
                x, y, z = run_vals[idx : idx + 3]
                if all(math.isfinite(v) and abs(v) < COORD_ABS_MAX for v in (x, y, z)):
                    points.append((x, y, z))
                else:
                    break

            metrics = score_candidate(points)
            if not metrics:
                continue

            score = float(metrics["score"])
            if prefer_offset is not None and abs(run_start - prefer_offset) <= 64:
                score += 0.5
            score += min(len(points) / 40.0, 1.0)

            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "align": align,
                    "run_start": run_start,
                    "run_len": len(run_vals),
                    "triple_offset": triple_offset,
                    "points_len": len(points),
                    "metrics": metrics,
                }

    for align in range(8):
        run_vals: List[float] = []
        run_start = -1
        for off in range(align, len(block) - 8, 8):
            value = struct.unpack_from("<d", block, off)[0]
            if math.isfinite(value) and abs(value) < COORD_ABS_MAX:
                if not run_vals:
                    run_start = off
                run_vals.append(value)
            else:
                if run_vals:
                    consider_run(run_start, run_vals, align)
                run_vals = []
                run_start = -1
        if run_vals:
            consider_run(run_start, run_vals, align)

    return best


def extract_and_build_pdb_sidechains(
    data: bytes,
    resi_offset: int,
    count: Optional[int] = None,
    filename: str = "output_re.pdb",
) -> bool:
    start_payload = resi_offset + 8
    if start_payload < 0 or start_payload >= len(data):
        return False

    def distance(p1: Tuple[float, float, float], p2: Tuple[float, float, float]) -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))

    def bond_range(atom_name: str, parent_name: str) -> Tuple[float, float]:
        if atom_name == "O" and parent_name == "C":
            return 1.10, 1.35
        if atom_name.startswith("O") and parent_name == "C":
            return 1.10, 1.40
        if atom_name == "SG" or parent_name == "SG":
            return 1.55, 2.20
        if atom_name == "SD" or parent_name == "SD":
            return 1.55, 2.30
        return 1.20, 1.85

    def find_next_by_distance(
        points: List[Tuple[float, float, float]],
        start_idx: int,
        parent_coord: Tuple[float, float, float],
        lo: float,
        hi: float,
        max_skip: int,
    ) -> Tuple[Optional[Tuple[float, float, float]], int]:
        idx = start_idx
        skipped = 0
        while idx < len(points):
            candidate = points[idx]
            idx += 1
            dist = distance(parent_coord, candidate)
            if lo <= dist <= hi:
                return candidate, idx
            skipped += 1
            if skipped > max_skip:
                break
        return None, idx

    search_area = data[start_payload:]
    pattern_bytes = b"|".join(name.encode("ascii") for name in ATOM_MAP.keys())

    markers = []
    for match in re.finditer(pattern_bytes, search_area):
        if markers and match.start() - markers[-1]["pos"] < 50:
            continue
        markers.append({"pos": match.start(), "name": match.group().decode("ascii")})
        if count is not None and len(markers) >= count:
            break

    if not markers:
        return False

    pdb_lines: List[str] = []
    atom_serial = 1

    for idx, current in enumerate(markers):
        res_name = current["name"]
        res_seq = idx + 1
        start_byte = current["pos"]
        end_byte = markers[idx + 1]["pos"] if idx < len(markers) - 1 else start_byte + 1000

        block_data = search_area[start_byte:end_byte]
        candidate = find_residue_coord_candidate(block_data, res_name, prefer_offset=44)
        if not candidate:
            continue

        run_start = int(candidate["run_start"])
        run_len = int(candidate["run_len"])
        triple_offset = int(candidate["triple_offset"])

        values = []
        for value_idx in range(run_len):
            off = run_start + value_idx * 8
            if off + 8 > len(block_data):
                break
            values.append(struct.unpack_from("<d", block_data, off)[0])

        points: List[Tuple[float, float, float]] = []
        for point_idx in range(triple_offset, len(values) - 2, 3):
            x, y, z = values[point_idx : point_idx + 3]
            if all(math.isfinite(v) and abs(v) < COORD_ABS_MAX for v in (x, y, z)):
                points.append((x, y, z))
            else:
                break

        if len(points) < 4:
            continue

        atom_names = ATOM_MAP.get(res_name, ["N", "CA", "C", "O"])
        parent_names = ATOM_PARENTS.get(res_name, [None] + atom_names[:-1])
        name_to_index: Dict[str, int] = {}
        coords: List[Tuple[float, float, float]] = []
        point_cursor = 0

        for atom_idx, atom_name in enumerate(atom_names):
            parent_name = (
                parent_names[atom_idx]
                if atom_idx < len(parent_names)
                else atom_names[atom_idx - 1] if atom_idx > 0 else None
            )
            if parent_name is None:
                if point_cursor >= len(points):
                    break
                coords.append(points[point_cursor])
                name_to_index[atom_name] = len(coords) - 1
                point_cursor += 1
                continue

            parent_idx = name_to_index.get(parent_name)
            if parent_idx is None:
                break

            lo, hi = bond_range(atom_name, parent_name)
            candidate_point, point_cursor = find_next_by_distance(
                points,
                point_cursor,
                coords[parent_idx],
                lo,
                hi,
                max_skip=12,
            )
            if candidate_point is None:
                break

            coords.append(candidate_point)
            name_to_index[atom_name] = len(coords) - 1

        for atom_idx, atom_name in enumerate(atom_names):
            if atom_idx >= len(coords):
                break
            x, y, z = coords[atom_idx]
            pdb_lines.append(
                format_pdb_atom_line(
                    serial=atom_serial,
                    atom_name=atom_name,
                    res_name=res_name,
                    chain_id="A",
                    res_seq=res_seq,
                    x=x,
                    y=y,
                    z=z,
                )
            )
            atom_serial += 1

    if not pdb_lines:
        return False

    with open(filename, "w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(pdb_lines))
    return True


def extract_and_build_pdb(
    data: bytes,
    resi_offset: int,
    count: Optional[int] = None,
    filename: str = "output_re.pdb",
) -> bool:
    start_payload = resi_offset + 8
    if start_payload < 0 or start_payload >= len(data):
        return False

    search_area = data[start_payload:]
    aa_names = [
        "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
        "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    ]
    pattern_bytes = b"|".join(name.encode("ascii") for name in aa_names)

    markers = []
    for match in re.finditer(pattern_bytes, search_area):
        if markers and match.start() - markers[-1]["pos"] < 50:
            continue
        markers.append({"pos": match.start(), "name": match.group().decode("ascii")})
        if count is not None and len(markers) >= count:
            break

    pdb_lines = []
    for idx, current in enumerate(markers):
        start_byte = current["pos"]
        end_byte = markers[idx + 1]["pos"] if idx < len(markers) - 1 else start_byte + 1000
        block_data = search_area[start_byte:end_byte]

        best_run: List[float] = []
        for alignment in range(8):
            current_run: List[float] = []
            chunks = [block_data[i : i + 8] for i in range(alignment, len(block_data), 8)]
            for chunk in chunks:
                if len(chunk) < 8:
                    continue
                try:
                    value = struct.unpack("<d", chunk)[0]
                except struct.error:
                    continue

                if -500.0 < value < 500.0 and abs(value) > 0.0001:
                    current_run.append(value)
                else:
                    if len(current_run) > len(best_run):
                        best_run = list(current_run)
                    current_run = []
            if len(current_run) > len(best_run):
                best_run = list(current_run)

        if len(best_run) < 3:
            continue

        atom_name = "CA"
        if len(best_run) >= 6:
            x, y, z = best_run[3], best_run[4], best_run[5]
        else:
            x, y, z = best_run[0], best_run[1], best_run[2]
            atom_name = "UNK"

        pdb_lines.append(
            format_pdb_atom_line(
                serial=idx + 1,
                atom_name=atom_name,
                res_name=current["name"],
                chain_id="A",
                res_seq=idx + 1,
                x=x,
                y=y,
                z=z,
            )
        )

    if not pdb_lines:
        return False

    with open(filename, "w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(pdb_lines))
    return True


def _normalize_save_path(save_path: PathLike) -> Path:
    path = Path(save_path).expanduser()
    if not path.is_file():
        raise FolditApiError(f"Save file not found: {path}")
    return path


def _read_save_bytes(save_path: PathLike) -> tuple[Path, bytes]:
    path = _normalize_save_path(save_path)
    return path, path.read_bytes()


def _find_meta(data: bytes) -> TaggedString:
    soln_off = data.find(b"SOLN")
    meta = find_tagged_string(
        data,
        b"META",
        prefer_after=soln_off if soln_off != -1 else None,
    )
    if not meta:
        raise FolditApiError("META tag not found; cannot read save_name.")
    return meta


def _infer_energy_targets(data: bytes, meta: Optional[TaggedString] = None) -> Optional[Tuple[int, ...]]:
    meta = meta or _find_meta(data)
    prefer_after = meta.payload_off + meta.strlen

    targets: set[int] = set()

    resi_tag = find_resi_tag(data, prefer_after=prefer_after)
    if resi_tag:
        targets.add(resi_tag.count)

    for tag in (b"SSTR", b"ALGN"):
        tagged = find_tagged_string(data, tag, prefer_after=prefer_after)
        if tagged and is_plausible_residue_count(tagged.strlen):
            targets.add(tagged.strlen)

    if not targets:
        return None
    return tuple(sorted(targets))


def _extract_disulfide_info(data: bytes) -> FolditDisulfideInfo:
    resi_tag = find_resi_tag(data)
    if not resi_tag:
        return FolditDisulfideInfo(0, 0, (), False)

    search_start = resi_tag.tag_off + 8
    search_end = len(data)
    for tag in (b"SSTR", b"PHAS", b"ALGN", b"DICT"):
        tagged = find_tagged_string(data, tag, prefer_after=search_start)
        if tagged:
            search_end = min(search_end, tagged.tag_off)

    pattern = re.compile(rb"CYS:(?:[A-Za-z]+:)*disulfide")
    residue_offsets = tuple(
        search_start + match.start()
        for match in pattern.finditer(data[search_start:search_end])
    )
    endpoint_count = len(residue_offsets)
    bond_count = endpoint_count // 2
    return FolditDisulfideInfo(
        endpoint_count=endpoint_count,
        bond_count=bond_count,
        residue_offsets=residue_offsets,
        has_unpaired_endpoint=(endpoint_count % 2) != 0,
    )


def _calculate_base_score(energy_block: EnergyBlock) -> float:
    return 8000.0 - 10.0 * energy_block.total_energy


def _calculate_bonus_score(data: bytes) -> float:
    # Current bonus detection is based on disulfide links encoded in RESI.
    return 250.0 * _extract_disulfide_info(data).bond_count


def _find_energy(data: bytes, meta: Optional[TaggedString] = None) -> EnergyBlock:
    meta = meta or _find_meta(data)
    energy_block = find_energy_block(
        data,
        start=meta.payload_off + meta.strlen,
        n_targets=_infer_energy_targets(data, meta),
    )
    if not energy_block:
        raise FolditApiError("Energy block not found; cannot calculate Foldit score.")
    return energy_block


def _find_player(data: bytes, energy_block: Optional[EnergyBlock] = None) -> PuzzlePlayerBlock:
    starts: List[int] = []

    if energy_block is not None:
        starts.append(energy_block.end_off)
    else:
        try:
            starts.append(_find_energy(data).end_off)
        except FolditApiError:
            pass

    try:
        meta = _find_meta(data)
        starts.append(meta.payload_off + meta.strlen)
    except FolditApiError:
        pass

    starts.append(0)

    seen: set[int] = set()
    for start in starts:
        if start in seen:
            continue
        seen.add(start)

        window = len(data) if start == 0 else 4096
        player_block = find_puzzle_player_block(data, start=start, window=window)
        if player_block:
            return player_block

    raise FolditApiError("Player block not found; cannot read player_name.")


def _extract_basic_info(data: bytes) -> FolditBasicInfo:
    meta = _find_meta(data)
    energy_block = _find_energy(data, meta)
    player_block = _find_player(data, energy_block)
    return FolditBasicInfo(
        player_name=player_block.player_name,
        save_name=meta.payload,
        foldit_score=_calculate_base_score(energy_block) + _calculate_bonus_score(data),
    )


def get_basic_info(save_path: PathLike) -> FolditBasicInfo:
    """Return player_name, save_name, and foldit_score using one file read."""
    _, data = _read_save_bytes(save_path)
    return _extract_basic_info(data)


def get_player_name(save_path: PathLike) -> str:
    """Return the player name from a Foldit save."""
    _, data = _read_save_bytes(save_path)
    return _find_player(data).player_name


def get_save_name(save_path: PathLike) -> str:
    """Return the save name from a Foldit save."""
    _, data = _read_save_bytes(save_path)
    return _find_meta(data).payload


def get_foldit_score(save_path: PathLike) -> float:
    """Return the overall Foldit score including detected bonus points."""
    _, data = _read_save_bytes(save_path)
    return _calculate_base_score(_find_energy(data)) + _calculate_bonus_score(data)


def get_bonus_score(save_path: PathLike) -> float:
    """Return detected bonus points encoded in the save."""
    _, data = _read_save_bytes(save_path)
    return _calculate_bonus_score(data)


def get_disulfide_info(save_path: PathLike) -> FolditDisulfideInfo:
    """Return disulfide endpoint/bond markers encoded in the RESI section."""
    _, data = _read_save_bytes(save_path)
    return _extract_disulfide_info(data)


def _resolve_output_path(save_path: Path, output_path: Optional[PathLike]) -> Path:
    target = save_path.with_suffix(".pdb") if output_path is None else Path(output_path).expanduser()
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def export_pdb(save_path: PathLike, output_path: Optional[PathLike] = None) -> str:
    """
    Export the PDB for a Foldit save and return the written path.

    If `output_path` is omitted, the PDB is written next to the save file with
    the same base name and the `.pdb` extension.
    """

    save_file, data = _read_save_bytes(save_path)
    target = _resolve_output_path(save_file, output_path)

    resi_tag = find_resi_tag(data)
    if not resi_tag:
        raise FolditApiError("RESI tag not found; cannot export PDB.")

    ok = extract_and_build_pdb_sidechains(
        data,
        resi_tag.tag_off,
        resi_tag.count,
        filename=str(target),
    )
    if not ok:
        ok = extract_and_build_pdb(
            data,
            resi_tag.tag_off,
            resi_tag.count,
            filename=str(target),
        )

    if not ok or not target.is_file():
        raise FolditApiError("PDB export failed.")

    return str(target.resolve())


__all__ = [
    "FolditApiError",
    "FolditBasicInfo",
    "FolditDisulfideInfo",
    "export_pdb",
    "get_basic_info",
    "get_bonus_score",
    "get_disulfide_info",
    "get_foldit_score",
    "get_player_name",
    "get_save_name",
]
