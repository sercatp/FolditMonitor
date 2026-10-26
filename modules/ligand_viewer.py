"""Offline ligand-space maps from Foldit .ir_solution saves.

The Tk integration opens a save, runs cancellable analysis in a worker, and
opens a self-contained English HTML report. No live client access is required.
Saved coordinates are preserved; reports include the source display filename.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import csv
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import shutil
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid
import webbrowser

VERSION = "1.5.2"


def _load_numerics():
    global np, generate_binary_structure, label, cKDTree, cdist
    import numpy as np
    from scipy.ndimage import generate_binary_structure, label
    from scipy.spatial import cKDTree
    from scipy.spatial.distance import cdist


RESTYPE = b"\x0d\x00\x00\x00RESTYPE"
ELEMENTS = ("X H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn "
            "Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd "
            "Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th "
            "Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og").split()
NUMBERS = {symbol: index for index, symbol in enumerate(ELEMENTS)}


class SaveFormatError(ValueError):
    pass


class NoLigandError(SaveFormatError):
    pass


class Reader:
    def __init__(self, data, offset=0):
        self.data, self.offset = data, offset

    def take(self, length):
        if length < 0 or self.offset + length > len(self.data):
            raise SaveFormatError("The save is truncated or uses an unsupported format.")
        start = self.offset
        self.offset += length
        return self.data[start:self.offset]

    def integer(self, maximum=100000):
        value = struct.unpack("<I", self.take(4))[0]
        if value > maximum:
            raise SaveFormatError("Invalid count in the saved atom data.")
        return value

    def text(self):
        try:
            return self.take(self.integer(512)).decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise SaveFormatError("Invalid string in the saved atom data.") from error

    def xyz(self):
        result = list(struct.unpack("<3d", self.take(24)))
        if not all(math.isfinite(value) and abs(value) < 100000 for value in result):
            raise SaveFormatError("Invalid saved atom coordinates.")
        return result


def read_embedded_type(reader):
    """Read chemistry; skip ideal geometry, which is not the saved pose's XYZ."""
    if reader.take(len(RESTYPE)) != RESTYPE:
        raise SaveFormatError("The embedded ligand definition is missing.")
    name, io_name = reader.text(), reader.text()
    reader.take(1)  # one-letter code
    reader.text(); reader.text(); reader.text()
    count = reader.integer(5000)
    if count == 0:
        raise SaveFormatError("An embedded ligand has no atoms.")
    atoms = []
    for _ in range(count):
        atom_name, atom_type = reader.text(), reader.text()
        reader.text()  # MM type
        reader.take(12)  # charge and flags
        reader.xyz()  # ideal coordinates; deliberately discarded
        atoms.append(dict(name=atom_name, rosetta_type=atom_type))
    indices = {atom["name"]: index for index, atom in enumerate(atoms, 1)}
    if len(indices) != count:
        raise SaveFormatError("Duplicate names in an embedded ligand.")
    bonds = []
    for _ in range(reader.integer(count * 8)):
        a, b, kind = reader.text(), reader.text(), reader.integer(5)
        if a not in indices or b not in indices or a == b or kind < 1:
            raise SaveFormatError("Invalid embedded ligand bond.")
        bonds.append(dict(a=indices[a], b=indices[b], bond_type_enum=kind))
    # The remaining RESTYPE fields contain ideal internal geometry and type
    # properties. ENDRESTYPE is the native delimiter for this block.
    end = reader.data.find(b"ENDRESTYPE", reader.offset)
    if end < 0 or end - reader.offset > 4_000_000:
        raise SaveFormatError("The embedded ligand definition is incomplete.")
    reader.offset = end + len(b"ENDRESTYPE")
    return dict(name=name, io_name=io_name, atoms=atoms, bonds=bonds)


def find_database(*folders):
    """Use an active component when several database versions are installed."""
    for folder in dict.fromkeys(Path(value) for value in folders if value):
        for root in (folder, folder / "Contents" / "Resources"):
            if (root / "chemical/atom_type_sets/fa_standard/atom_properties.txt").is_file():
                return root
            try:
                version = json.loads((root / "versions.json").read_text(encoding="utf-8-sig"))["components"]["database"]["version"]
                # A component version is one directory component, never a path.
                if not isinstance(version, str) or Path(version).name != version or "/" in version or "\\" in version:
                    continue
                candidates = [root / f"cmp-database-{version}" / "database"]
            except (OSError, ValueError, KeyError, TypeError):
                candidates = [root / "database", *sorted(root.glob("cmp-database-*/database"))]
            candidates = [p for p in candidates if (p / "chemical/atom_type_sets/fa_standard/atom_properties.txt").is_file()]
            if len(candidates) == 1:
                return candidates[0]
    raise SaveFormatError("Foldit's atom database was not found. Select the Foldit client folder when prompted.")


def read_atom_properties(database):
    properties = {}
    path = Path(database) / "chemical/atom_type_sets/fa_standard/atom_properties.txt"
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 3 or fields[0] == "NAME":
            continue
        try:
            radius = float(fields[2])
        except ValueError:
            continue
        element = fields[1].capitalize()
        if element == "Z":
            element = "X"  # Foldit pseudo-atoms have no physical element.
        if element not in NUMBERS or not math.isfinite(radius) or radius < 0:
            raise SaveFormatError("Unsupported element or radius in Foldit's atom database.")
        properties[fields[0]] = dict(element=element, atomic_number=NUMBERS[element], radius=radius,
                                    donor="DONOR" in fields, acceptor="ACCEPTOR" in fields)
    if not properties:
        raise SaveFormatError("Foldit's atom-radius table is empty.")
    return properties


class ResidueDatabase:
    """Resolve standard protein heavy atoms, including the native terminal order.

    Protein hydrogens do not participate in this map. Their saved coordinates are
    consumed but not assigned guessed names. Ligand hydrogens use RESTYPE order.
    Unrecognized variants fail rather than silently producing a misleading map.
    """
    def __init__(self, database, properties):
        self.root = Path(database) / "chemical/residue_type_sets/fa_standard"
        self.properties = properties
        self.cache = {}

    def protein(self, full_name, atom_count):
        key = full_name, atom_count
        if key in self.cache:
            return self.cache[key]
        base, *patches = full_name.split(":")
        if not base or not all(character.isalnum() or character == "_" for character in base):
            raise SaveFormatError("Invalid protein residue name in the save.")
        path = self.root / "residue_types/l-caa" / f"{base}.params"
        if not path.is_file():
            raise SaveFormatError(f"Unsupported residue type: {full_name}. No atom mapping was guessed.")
        atoms, bond_names = [], []
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split("#", 1)[0].split()
            if fields and fields[0] == "ATOM":
                atoms.append(dict(name=fields[1], rosetta_type=fields[2]))
            elif fields and fields[0] in ("BOND", "BOND_TYPE"):
                bond_names.append((fields[1], fields[2]))
        expected = len(atoms)
        by_name = {a["name"]: a for a in atoms}
        for patch in patches:
            if patch == "NtermProteinFull":
                by_name["N"]["rosetta_type"] = "Nlys"
                expected += 2 if base != "PRO" else 3
                if base == "PRO":
                    atoms.append(dict(name="CAV", rosetta_type="VIRT"))
            elif patch == "CtermProteinFull":
                by_name["C"]["rosetta_type"] = "COO"
                by_name["O"]["rosetta_type"] = "OOC"
                atoms.insert(4, dict(name="OXT", rosetta_type="OOC"))
                bond_names.append(("C", "OXT"))
                expected += 1
            elif patch == "disulfide" and base == "CYS":
                atoms = [a for a in atoms if a["name"] != "HG"]
                expected -= 1
            elif patch in ("NtermTruncation", "CtermTruncation"):
                pass  # These patches change connections, not the atom array.
            else:
                raise SaveFormatError(f"Unsupported protein variant: {full_name}. No atom mapping was guessed.")
        if expected != atom_count:
            raise SaveFormatError(f"Atom count mismatch for {full_name}: save {atom_count}, database {expected}.")
        heavy = [a for a in atoms if self.properties[a["rosetta_type"]]["element"] != "H"]
        index = {a["name"]: i for i, a in enumerate(heavy, 1)}
        bonds = [dict(a=index[a], b=index[b], bond_type_enum=1) for a, b in bond_names if a in index and b in index]
        self.cache[key] = heavy, bonds
        return heavy, bonds


def read_puzzle_id(data, residue_offset):
    """Read the duplicated puzzle identifier in the header; discard player text."""
    end = min(residue_offset, 1_048_576)
    for offset in range(8, max(8, end - 15)):
        first, second, _, length = struct.unpack_from("<4I", data, offset)
        if first != second or not 1 <= first <= 10_000_000 or not 1 <= length <= 512:
            continue
        if offset + 16 + length > end:
            continue
        try:
            text = data[offset + 16:offset + 16 + length].removesuffix(b"\0").decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text and all(ord(character) >= 32 for character in text):
            return str(first)
    return None


def load_save(save_path, database, *, check=lambda: None):
    """Read molecular data and the display filename, omitting player metadata."""
    path = Path(save_path)
    if path.stat().st_size > 128 * 1024 * 1024:
        raise SaveFormatError("This save exceeds the 128 MB parsing limit.")
    data = path.read_bytes()
    marker = -1
    search = 8
    while True:
        candidate = data.find(b"RESI", search)
        if candidate < 0:
            break
        probe = Reader(data, candidate + 4)
        try:
            plausible_count = probe.integer(10000)
            names = [probe.text() for _ in range(3)]
            if plausible_count > 0 and all(names):
                marker = candidate
                break
        except SaveFormatError:
            pass
        search = candidate + 4
    if not data.startswith(b"\x0b\x00\x00\x00SOLN") or marker < 0:
        raise SaveFormatError("This is not a supported Foldit .ir_solution save.")
    reader = Reader(data, marker + 4)
    count = reader.integer(10000)
    if not count:
        raise SaveFormatError("This save has no residues.")
    raw_rows, definitions = [], {}
    total = 0
    for segment in range(1, count + 1):
        check()
        full_name, io_name = reader.text(), reader.text()
        reader.text()  # unused residue field
        for _ in range(reader.integer(100)):
            reader.text()  # recorded variants; full_name identifies the atom map
        reader.integer(10000)  # native serialization/type reference
        atom_count = reader.integer(5000)
        kind = reader.text()
        total += atom_count
        if not atom_count or total > 100000:
            raise SaveFormatError("The saved pose has an invalid atom count.")
        xyz = [reader.xyz() for _ in range(atom_count)]
        if data.startswith(RESTYPE, reader.offset):
            definition = read_embedded_type(reader)
            if definition["name"] != full_name or definition["io_name"] != io_name:
                raise SaveFormatError("The saved ligand and its definition do not match.")
            previous = definitions.setdefault(full_name, definition)
            if previous != definition:
                raise SaveFormatError("Conflicting embedded residue definitions.")
        if full_name == "VRT":
            continue
        raw_rows.append((segment, full_name, io_name, atom_count, kind, xyz))
    if data[reader.offset:reader.offset + 4] != b"CON2":
        raise SaveFormatError("Unsupported data after the saved residue array.")
    if not any(row[4] == "LIGAND" for row in raw_rows):
        raise NoLigandError("No physical ligand was found in this save.")
    properties = read_atom_properties(database)
    residue_database = ResidueDatabase(database, properties)
    residues, ligands = [], []
    for segment, full_name, io_name, atom_count, kind, xyz in raw_rows:
        check()
        is_ligand = kind == "LIGAND"
        if full_name in definitions:
            definition = definitions[full_name]
            mapped, bonds = definition["atoms"], definition["bonds"]
            if len(mapped) != atom_count:
                raise SaveFormatError("The ligand coordinate and atom counts differ.")
        else:
            if is_ligand:
                raise SaveFormatError(f"Ligand {full_name} has no embedded atom definition in this save.")
            mapped, bonds = residue_database.protein(full_name, atom_count)
        atoms = []
        for atom, position in zip(mapped, xyz):
            spec = properties.get(atom["rosetta_type"])
            if spec is None:
                raise SaveFormatError(f"Unknown Foldit atom type: {atom['rosetta_type']}.")
            atoms.append(dict(**atom, xyz=position, element=spec["element"], atomic_number=spec["atomic_number"]))
        # Preserve strained saved geometry as-is. Bond lengths are not a reason
        # to reject a Foldit pose, and are never used to guess atom identities.
        residue = dict(segment=segment, residue_name=io_name, full_type_name=full_name,
                       atoms=atoms, bonds=bonds, is_ligand=is_ligand)
        residues.append(residue)
        if is_ligand and any(a["atomic_number"] > 1 for a in atoms):
            ligands.append(segment)
    if not ligands:
        raise NoLigandError("No physical ligand was found in this save.")
    digest = hashlib.sha256(json.dumps(residues, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    puzzle = re.match(r"puzzle_([0-9]+)(?:_|$)", path.stem, re.IGNORECASE)
    puzzle_id = read_puzzle_id(data, marker) or (puzzle[1] if puzzle else None)
    return dict(version=1, residues=residues, ligand_segments=ligands, geometry_sha256=digest,
                save_name=path.name, puzzle_id=puzzle_id), properties


@dataclass
class Atoms:
    xyz: np.ndarray
    radii: np.ndarray
    records: list


def read_atoms(snapshot, properties, segment):
    residues = snapshot.get('residues', snapshot.get('snapshot', {}).get('residues'))
    if not residues:
        raise ValueError('Snapshot has no complete residue list')
    groups = [[], []]
    target = None
    seen = set()
    for residue in residues:
        ri = residue['segment']
        if ri in seen:
            raise ValueError('Duplicate residue segment')
        seen.add(ri)
        if ri == segment:
            target = residue
        for index, atom in enumerate(residue['atoms'], 1):
            xyz = np.asarray(atom['xyz'], dtype=float)
            if xyz.shape != (3,) or not np.isfinite(xyz).all() or np.max(np.abs(xyz)) >= 100000:
                raise ValueError('Invalid atom coordinates')
            if atom['atomic_number'] <= 1:
                continue
            spec = properties.get(atom['rosetta_type'])
            if spec is None or spec['radius'] <= 0:
                raise ValueError(f"Missing positive Foldit radius for {atom['rosetta_type']}")
            groups[ri != segment].append(dict(segment=ri, atom_index=index, name=atom['name'],
                element=atom['element'], rosetta_type=atom['rosetta_type'], xyz=xyz.tolist(),
                residue_name=residue.get('residue_name', ''), **spec))
    if target is None or not groups[0] or not groups[1]:
        raise ValueError('Need target heavy atoms and surrounding heavy atoms')
    packed = [Atoms(np.array([r['xyz'] for r in rows]), np.array([r['radius'] for r in rows]), rows)
              for rows in groups]
    return packed[0], packed[1], target


def fibonacci_directions(n):
    if n < 6:
        raise ValueError('At least six directions are required')
    i = np.arange(n)
    z = 1 - 2 * (i + .5) / n
    angle = i * math.pi * (3 - math.sqrt(5))
    r = np.sqrt(1 - z * z)
    return np.column_stack((r * np.cos(angle), r * np.sin(angle), z))


def surface_clearance(points, xyz, radii, batch=1024, check=lambda: None):
    """Exact min_j(|point-atom_j|-radius_j), including negative overlap."""
    points = np.asarray(points, dtype=float).reshape(-1, 3)
    if not len(xyz):
        return np.full(len(points), np.inf)
    batch = max(1, min(batch, 2_000_000 // len(xyz)))
    result = np.empty(len(points))
    for start in range(0, len(points), batch):
        check()
        distances = cdist(points[start:start + batch], xyz)
        distances -= radii
        result[start:start + batch] = distances.min(axis=1)
    return result


def ray_first_hit(origin, directions, xyz, radii, starts, limit, probe_radius=0):
    """First intersection of a ray after starts with the union of expanded atom spheres.

    A ray already inside a sphere at its starting distance is blocked immediately.
    A hit exactly at limit is recorded as a hit, separately from a censored ray.
    """
    directions = np.asarray(directions, dtype=float)
    if not np.allclose(np.linalg.norm(directions, axis=1), 1, atol=1e-8):
        raise ValueError('Ray directions must be unit vectors')
    starts = np.broadcast_to(np.asarray(starts, dtype=float), (len(directions),))
    if len(xyz) == 0:
        return np.full(len(directions), limit), np.full(len(directions), -1, dtype=int)
    delta = xyz - origin
    projection = directions @ delta.T
    perpendicular2 = np.maximum(0, np.sum(delta * delta, axis=1)[None, :] - projection * projection)
    radius2 = (radii + probe_radius) ** 2
    valid = perpendicular2 <= radius2[None, :]
    half_chord = np.sqrt(np.maximum(0, radius2[None, :] - perpendicular2))
    entry, exit_ = projection - half_chord, projection + half_chord
    valid &= (exit_ >= starts[:, None]) & (entry <= limit)
    hit = np.where(valid, np.maximum(entry, starts[:, None]), np.inf)
    which = hit.argmin(axis=1)
    times = hit[np.arange(len(directions)), which]
    which[~np.isfinite(times)] = -1
    return np.minimum(times, limit), which


def grid_fields(ligand, environment, reach, step, probe_radius, check=lambda: None):
    lo = np.floor((ligand.xyz.min(axis=0) - reach) / step) * step
    hi = np.ceil((ligand.xyz.max(axis=0) + reach) / step) * step
    shape = np.rint((hi - lo) / step).astype(int) + 1
    if np.prod(shape) > 2_000_000:
        raise ValueError('Grid exceeds two million voxels; increase --grid-step')
    axes = [lo[k] + np.arange(shape[k]) * step for k in range(3)]
    points = np.column_stack([a.ravel() for a in np.meshgrid(*axes, indexing='ij')])
    receptor = surface_clearance(points, environment.xyz, environment.radii, check=check).reshape(shape)
    ligand_gap = surface_clearance(points, ligand.xyz, ligand.radii, check=check).reshape(shape)
    gap = np.minimum(receptor, ligand_gap) - probe_radius
    free = gap >= 0
    components, count = label(free, structure=generate_binary_structure(3, 1))
    boundary_labels = np.unique(np.concatenate([components[0].ravel(), components[-1].ravel(),
        components[:, 0].ravel(), components[:, -1].ravel(),
        components[:, :, 0].ravel(), components[:, :, -1].ravel()]))
    boundary_labels = boundary_labels[boundary_labels != 0]
    open_to_boundary = np.isin(components, boundary_labels) & free
    return dict(origin=lo, step=step, shape=shape, points=points,
        environment_surface_clearance=receptor, ligand_surface_clearance=ligand_gap,
        probe_clearance=gap, free=free, boundary_connected=open_to_boundary,
        free_components=count)


def measure(snapshot, properties, segment, *, reach=8., step=.5, directions=256, probe_radius=0.,
            check=lambda: None, progress=lambda value, message: None):
    if not (reach > 0 and step > 0 and probe_radius >= 0 and math.isfinite(reach + step + probe_radius)):
        raise ValueError('Invalid measurement dimensions')
    check()
    progress(0.05, "Reading atom radii")
    ligand, environment, target = read_atoms(snapshot, properties, segment)
    unit = fibonacci_directions(directions)
    enclosure_unit = fibonacci_directions(96)
    # All environment atoms remain available to the exact distance calculations.
    progress(0.1, "Measuring the empty-space grid")
    grid = grid_fields(ligand, environment, reach, step, probe_radius, check=check)
    raw_clearance = np.minimum(grid['environment_surface_clearance'], grid['ligand_surface_clearance'])
    point_free = raw_clearance >= 0
    free_points = grid['points'][point_free.ravel()]
    free_tree = cKDTree(free_points) if len(free_points) else None
    point_clearance = raw_clearance[point_free]
    boundary_points = grid['boundary_connected'][point_free]
    probe_sizes = sorted(set((0., 1., 1.4, 1.7, probe_radius)))
    atoms, best_rays = [], []
    bond_map = {i: [] for i in range(1, len(target['atoms']) + 1)}
    for bond in target['bonds']:
        bond_map[bond['a']].append(bond['b'])
        bond_map[bond['b']].append(bond['a'])
    for li, atom in enumerate(ligand.records):
        check()
        progress(0.4 + 0.55 * li / len(ligand.records), f"Measuring ligand atom {li + 1} of {len(ligand.records)}")
        origin = ligand.xyz[li]
        source_index = atom['atom_index']
        hydrogen_vectors = []
        for index in bond_map[source_index]:
            other = target['atoms'][index - 1]
            if other['atomic_number'] == 1:
                v = np.asarray(other['xyz']) - origin
                if np.linalg.norm(v) > 1e-8:
                    hydrogen_vectors.append(v / np.linalg.norm(v))
        h_dirs = np.array(hydrogen_vectors).reshape(-1, 3)
        sampling = np.vstack((unit, h_dirs))
        other_ligand = np.arange(len(ligand.xyz)) != li
        # Ray measurement begins outside the source atom's exclusion sphere.
        # It measures free corridors beyond that surface, not a valid covalent bond.
        start = atom['radius'] + probe_radius + 1e-6
        direction_rows = []
        if start < reach:
            env_hit, env_index = ray_first_hit(origin, sampling, environment.xyz,
                environment.radii, start, reach, probe_radius)
            lig_hit, lig_index = ray_first_hit(origin, sampling, ligand.xyz[other_ligand],
                ligand.radii[other_ligand], start, reach, probe_radius)
            first_hit = np.minimum(env_hit, lig_hit)
            for di, vector in enumerate(sampling):
                env_wins = env_hit[di] < lig_hit[di] or (
                    env_hit[di] == lig_hit[di] and env_index[di] >= 0)
                ei = env_index[di] if env_wins else lig_index[di]
                records = environment.records if env_wins else [r for j, r in enumerate(ligand.records) if j != li]
                blocker = records[ei] if ei >= 0 else None
                gap_length = max(0., float(first_hit[di] - start))
                midpoint = origin + vector * (start + gap_length / 2)
                enclosure = None
                if gap_length >= step:
                    _, walls = ray_first_hit(midpoint, enclosure_unit, environment.xyz,
                        environment.radii, 0., reach, probe_radius)
                    enclosure = float(np.mean(walls >= 0))
                row = dict(direction_index=di, vector=vector.tolist(), origin=origin.tolist(),
                    start_distance=float(start), first_obstacle_distance=float(first_hit[di]),
                    free_length=gap_length, reaches_measurement_limit=blocker is None,
                    midpoint=midpoint.tolist(), environment_enclosure_fraction=enclosure,
                    follows_explicit_hydrogen=di >= len(unit),
                    nearest_hydrogen_angle_deg=float(np.degrees(np.arccos(np.clip(np.max(h_dirs @ vector), -1, 1))))
                        if len(h_dirs) else None,
                    blocker={k: blocker[k] for k in ('segment', 'atom_index', 'name', 'element')}
                        if blocker else None,
                    blocker_kind=('environment' if env_wins else 'ligand') if blocker else None)
                direction_rows.append(row)
        distances = np.linalg.norm(environment.xyz - origin, axis=1)
        shell_gaps = distances - environment.radii - atom['radius']
        nearest = int(np.argmin(shell_gaps))
        nearby_environment = [dict(**{k: environment.records[j][k] for k in
            ('segment', 'atom_index', 'name', 'element', 'donor', 'acceptor')},
            center_distance=float(distances[j]), surface_gap=float(shell_gaps[j]))
            for j in np.argsort(shell_gaps)[:12]]
        ligand_distances = np.linalg.norm(ligand.xyz - origin, axis=1)
        nearby_ligand = [dict(**{k: ligand.records[j][k] for k in ('atom_index', 'name', 'element')},
            bonded=ligand.records[j]['atom_index'] in bond_map[source_index],
            center_distance=float(ligand_distances[j]),
            surface_gap=float(ligand_distances[j] - ligand.radii[j] - atom['radius']))
            for j in np.argsort(ligand_distances) if j != li][:8]
        spaces = {}
        for radius in (4., 6., reach):
            neighbors = free_tree.query_ball_point(origin, radius) if free_tree else []
            selected_probe = point_clearance[neighbors] >= probe_radius
            spaces[str(radius)] = dict(free_volume=int(np.sum(selected_probe)) * step ** 3,
                boundary_connected_volume=int(np.sum(boundary_points[neighbors] & selected_probe)) * step ** 3,
                probe_volume_profile={str(size): int(np.sum(point_clearance[neighbors] >= size)) * step ** 3
                                      for size in probe_sizes})
        ranked = sorted(direction_rows, key=lambda r: (
            r['environment_enclosure_fraction'] or 0, r['free_length']), reverse=True)
        selected = []
        for ray in ranked:
            if ray['free_length'] < step:
                continue
            if all(np.dot(ray['vector'], old['vector']) < math.cos(math.radians(35)) for old in selected):
                selected.append(ray)
            if len(selected) == 3:
                break
        for ray in selected:
            best_rays.append(dict(atom_index=source_index, atom_name=atom['name'], **ray))
        atoms.append(dict(**atom, explicit_hydrogens=len(hydrogen_vectors),
            hydrogen_directions=h_dirs.tolist(), nearby_environment=nearby_environment,
            nearby_ligand=nearby_ligand, nearest_environment=dict(
                **{k: environment.records[nearest][k] for k in ('segment', 'atom_index', 'name', 'element')},
                center_distance=float(distances[nearest]), surface_gap=float(shell_gaps[nearest])),
            free_corridor_fraction=float(np.mean([r['free_length'] >= step for r in direction_rows[:len(unit)]]))
                if direction_rows else 0., local_space=spaces, directions=direction_rows,
            representative_directions=selected))
    report = dict(version=VERSION, captured_utc=snapshot.get('captured_utc'), client=snapshot.get('client'),
        puzzle_id=snapshot.get('puzzle_id'), save_name=snapshot.get('save_name'),
        segment=segment, ligand_name=target.get('residue_name', target.get('type')),
        geometry_sha256=snapshot.get('geometry_sha256'),
        parameters=dict(reach=reach, grid_step=step, probe_radius=probe_radius,
            directions=directions, radius_model='unscaled Foldit atom-type LJ_RADIUS; heavy atoms only'),
        semantics=dict(clearance='distance from probe center to nearest atom surface minus probe radius',
            local_volume='Free voxel volume inside an atom-centered ball; balls overlap; not additive.',
            boundary='Connectivity to measurement box boundary on a six-neighbor grid; not a binding-pocket label.',
            ray='Continuous straight corridor beyond source exclusion sphere; not covalent attachment feasibility.',
            enclosure='Fraction of 96 rays hitting environment within reach at corridor midpoint; not an energy.',
            hydrogen='Present H directions are exact pose vectors, not a complete chemical growth eligibility test.'),
        heavy_atoms=dict(ligand=len(ligand.records), environment=len(environment.records)),
        ligand_bonds=target['bonds'],
        grid=dict(shape=grid['shape'].tolist(), voxels=int(np.prod(grid['shape'])),
            free_volume=float(np.sum(grid['free']) * step ** 3),
            probe_volume_profile={str(size): int(np.sum(raw_clearance >= size)) * step ** 3
                                  for size in probe_sizes},
            free_components=int(grid['free_components'])), atoms=atoms,
        representative_directions=best_rays)
    return report, grid, ligand, environment


def save_outputs(out, report, grid, ligand, environment, suffix=""):
    (out / f'space{suffix}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    np.savez_compressed(out / f'space_grid{suffix}.npz', origin=grid['origin'], step=grid['step'],
        environment_surface_clearance=grid['environment_surface_clearance'].astype(np.float32),
        ligand_surface_clearance=grid['ligand_surface_clearance'].astype(np.float32),
        probe_clearance=grid['probe_clearance'].astype(np.float32),
        free=grid['free'], boundary_connected=grid['boundary_connected'])
    with (out / f'atoms{suffix}.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        fields = ['atom_index', 'name', 'element', 'explicit_hydrogens', 'nearest_environment_segment',
            'nearest_environment_atom', 'nearest_environment_center_distance', 'nearest_environment_surface_gap',
            'free_corridor_fraction', 'free_volume_4A', 'best_corridor_length', 'best_enclosure']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for atom in report['atoms']:
            rays = atom['representative_directions']
            writer.writerow(dict(**{k: atom[k] for k in ('atom_index', 'name', 'element', 'explicit_hydrogens',
                'free_corridor_fraction')}, nearest_environment_segment=atom['nearest_environment']['segment'],
                nearest_environment_atom=atom['nearest_environment']['name'],
                nearest_environment_center_distance=atom['nearest_environment']['center_distance'],
                nearest_environment_surface_gap=atom['nearest_environment']['surface_gap'],
                free_volume_4A=atom['local_space']['4.0']['free_volume'],
                best_corridor_length=max((r['free_length'] for r in rays), default=0),
                best_enclosure=max((r['environment_enclosure_fraction'] or 0 for r in rays), default=0)))


COLORS = {"C": "#475569", "N": "#3977ec", "O": "#ec5252", "S": "#dab332", "P": "#e69239",
          "F": "#4fbf79", "Cl": "#4fbf79", "Br": "#b07238", "I": "#9f7aea"}
BACKBONE_ATOMS = frozenset(("N", "CA", "C", "O", "OXT"))


def _lines(pairs):
    axes = [[], [], []]
    for a, b in pairs:
        for k in range(3):
            axes[k].extend((float(a[k]), float(b[k]), None))
    return dict(zip(("x", "y", "z"), axes))


def write_html(path, report, grid, ligand, environment, snapshot):
    import plotly.graph_objects as go

    field = np.minimum(grid["environment_surface_clearance"], grid["ligand_surface_clearance"]).ravel()
    visible = ((field >= report["parameters"]["probe_radius"])
               & (grid["ligand_surface_clearance"].ravel() <= 4.)
               & (grid["environment_surface_clearance"].ravel() <= 5.))
    selected = np.flatnonzero(visible)
    total_points = len(selected)
    if len(selected) > 18000:
        selected = np.sort(np.random.default_rng(12345).choice(selected, 18000, replace=False))
    points, gaps = grid["points"][selected], field[selected]
    nearby = cdist(environment.xyz, ligand.xyz).min(axis=1) <= 7.
    env = [a for a, include in zip(environment.records, nearby) if include]
    env_by_id = {(a["segment"], a["atom_index"]): a for a in env}
    env_by_name = {(a["segment"], a["name"]): a for a in env}
    protein_segments = {r["segment"] for r in snapshot["residues"] if not r.get("is_ligand")}

    def environment_part(atom):
        if atom["segment"] not in protein_segments:
            return "environment"
        return "backbone" if atom["name"] in BACKBONE_ATOMS else "sidechains"

    env_atoms = {key: [] for key in ("backbone", "sidechains", "environment")}
    env_pairs = {key: [] for key in env_atoms}
    for atom in env:
        env_atoms[environment_part(atom)].append(atom)
    for residue in snapshot["residues"]:
        ri = residue["segment"]
        if ri == report["segment"]:
            continue
        for bond in residue["bonds"]:
            a, b = env_by_id.get((ri, bond["a"])), env_by_id.get((ri, bond["b"]))
            if a and b:
                part = "sidechains" if "sidechains" in (environment_part(a), environment_part(b)) else environment_part(a)
                env_pairs[part].append((a["xyz"], b["xyz"]))
        a, b = env_by_name.get((ri, "C")), env_by_name.get((ri + 1, "N"))
        if ri in protein_segments and ri + 1 in protein_segments and a and b and math.dist(a["xyz"], b["xyz"]) < 2.5:
            env_pairs["backbone"].append((a["xyz"], b["xyz"]))
    lig_by_id = {a["atom_index"]: a for a in ligand.records}
    lig_pairs = [(lig_by_id[b["a"]]["xyz"], lig_by_id[b["b"]]["xyz"])
                 for b in report["ligand_bonds"] if b["a"] in lig_by_id and b["b"] in lig_by_id]
    fig = go.Figure()
    layer_indices = {}

    def add(key, **kwargs):
        layer_indices.setdefault(key, []).append(len(fig.data))
        fig.add_trace(go.Scatter3d(**kwargs))

    def add_environment(part, bonds_key, atoms_key, title, color):
        atoms = env_atoms[part]
        xyz = np.array([a["xyz"] for a in atoms]).reshape(-1, 3)
        is_backbone = part == "backbone"
        add(bonds_key, **_lines(env_pairs[part]), mode="lines", name=title + " bonds",
            line=dict(color=color, width=8 * (1.25 if is_backbone else .7)), opacity=.25, hoverinfo="skip")
        add(atoms_key, x=xyz[:, 0].tolist(), y=xyz[:, 1].tolist(), z=xyz[:, 2].tolist(), mode="markers",
            name=title + " atoms", opacity=.25,
            marker=dict(size=8 * (1.15 if is_backbone else 1),
                        color=[color if a["element"] == "C" else COLORS.get(a["element"], "#bc8bd7") for a in atoms]),
            hovertext=[f"{escape(a.get('residue_name', ''))} {a['segment']} · {escape(a['name'])} ({escape(a['element'])})"
                       f"<br>{title}<br>{escape(a['rosetta_type'])} · radius {a['radius']:.2f} Å"
                       f"<br>Donor: {'yes' if a['donor'] else 'no'} · Acceptor: {'yes' if a['acceptor'] else 'no'}" for a in atoms],
            hovertemplate="%{hovertext}<extra></extra>")
        layer_indices[part] = layer_indices[bonds_key] + layer_indices[atoms_key]

    add("space", x=points[:, 0].tolist(), y=points[:, 1].tolist(), z=points[:, 2].tolist(), mode="markers",
        name="Free space", visible=False, opacity=.22, marker=dict(size=5, color=gaps.tolist(), colorscale="Viridis",
        cmin=0, cmax=2., showscale=False), hovertemplate="Empty-sphere radius: %{marker.color:.2f} Å<extra></extra>")
    add_environment("sidechains", "protein_bonds", "protein", "Protein side chain", "#94a3b8")
    add("ligand_bonds", **_lines(lig_pairs), mode="lines", name="Ligand bonds",
        line=dict(color="#334155", width=7), hoverinfo="skip")
    add("ligand", x=ligand.xyz[:, 0].tolist(), y=ligand.xyz[:, 1].tolist(), z=ligand.xyz[:, 2].tolist(),
        mode="markers+text", name="Ligand atoms", text=[a["name"] for a in ligand.records],
        customdata=[a["atom_index"] for a in ligand.records], textposition="top center", textfont=dict(size=12, color="#172e48"),
        marker=dict(size=7, color=[COLORS.get(a["element"], "#9f7aea") for a in ligand.records]),
        hovertext=[f"{escape(a['name'])} · Foldit atom {a['atom_index']}<br>{escape(a['rosetta_type'])} · radius {a['radius']:.2f} Å"
                   for a in ligand.records], hovertemplate="%{hovertext}<extra></extra>")
    sites = [a for a in report["atoms"] if a["explicit_hydrogens"] and a["local_space"]["4.0"]["probe_volume_profile"]["1.0"] > 0]
    site_xyz = np.array([a["xyz"] for a in sites]).reshape(-1, 3)
    add("sites", x=site_xyz[:, 0].tolist(), y=site_xyz[:, 1].tolist(), z=site_xyz[:, 2].tolist(), mode="markers",
        name="Atoms with H and nearby space", customdata=[a["atom_index"] for a in sites],
        marker=dict(size=11, color="#ea9b32", symbol="diamond", opacity=.6),
        hovertext=[f"{escape(a['name'])} · {a['explicit_hydrogens']} explicit H<br>"
                   f"1 Å probe volume within 4 Å: {a['local_space']['4.0']['probe_volume_profile']['1.0']:.1f} Å³"
                   for a in sites],
        hovertemplate="%{hovertext}<extra></extra>")
    corridors = []
    ray_axes = [[], [], []]
    ray_text, ray_hover, ray_ids, ray_sizes = [], [], [], []
    target = next(r for r in snapshot["residues"] if r["segment"] == report["segment"])
    for atom in report["atoms"]:
        hydrogens = []
        for bond in target["bonds"]:
            if atom["atom_index"] in (bond["a"], bond["b"]):
                other = target["atoms"][(bond["b"] if bond["a"] == atom["atom_index"] else bond["a"]) - 1]
                if other["atomic_number"] == 1:
                    vector = np.asarray(other["xyz"]) - atom["xyz"]
                    if np.linalg.norm(vector) > 1e-8:
                        hydrogens.append((float(np.linalg.norm(vector)), vector / np.linalg.norm(vector), other["name"]))
        for ray in atom["directions"]:
            if ray["follows_explicit_hydrogen"] and ray["free_length"] >= report["parameters"]["grid_step"]:
                origin, vector = np.array(ray["origin"]), np.array(ray["vector"])
                start = origin + vector * ray["start_distance"]
                end = origin + vector * ray["first_obstacle_distance"]
                xyz = np.vstack((start, (start + end) / 2, end))
                hydrogen = max(hydrogens, key=lambda item: float(item[1] @ vector))[2] if hydrogens else "H"
                blocker = ray["blocker"]
                if blocker:
                    obstacle = "Protein / environment atom" if ray["blocker_kind"] == "environment" else "Another ligand atom"
                    end_label = f"{obstacle} {blocker['name']} (residue {blocker['segment']}) limits this space."
                else:
                    end_label = "No obstacle found within the checked distance; space may continue farther."
                length_label = ("At least " if not blocker else "") + f"{ray['free_length']:.2f} Å"
                corridor_id = len(corridors)
                corridor = dict(id=corridor_id, atom_index=atom["atom_index"], atom_name=atom["name"],
                                hydrogen=hydrogen, length=ray["free_length"], length_label=length_label, end_label=end_label)
                corridors.append(corridor)
                for k in range(3):
                    ray_axes[k].extend([*xyz[:, k].tolist(), None])
                ray_text.extend(["", "", f"{atom['name']} · {ray['free_length']:.1f} Å", ""])
                hover = (f"<b>Space for ligand growth</b><br>From {escape(atom['name'])}, along its bond to {escape(hydrogen)}"
                         f"<br>Room beyond the atom: {escape(length_label)}<br>{escape(end_label)}")
                ray_hover.extend([hover, hover, hover, ""])
                ray_ids.extend([corridor_id, corridor_id, corridor_id, None])
                ray_sizes.extend([2, 3.5, 5, 0])
    add("rays", **dict(zip(("x", "y", "z"), ray_axes)), mode="lines+markers+text",
        name="Free X–H corridors", text=ray_text, textposition="top right",
        textfont=dict(size=11, color="#b65f05"), line=dict(color="#df841b", width=5.5),
        marker=dict(size=ray_sizes, color="#df841b"), opacity=.95, customdata=ray_ids,
        hovertext=ray_hover, hovertemplate="%{hovertext}<extra></extra>",
        hoverlabel=dict(bgcolor="#fff8eb", bordercolor="#df841b", font_color="#71430e"))
    add_environment("backbone", "backbone_bonds", "backbone_atoms", "Protein backbone", "#365b7d")
    layer_indices["environment"] = []
    if env_atoms["environment"]:
        add_environment("environment", "environment_bonds", "environment_atoms", "Other environment", "#9c82af")
    for part in ("backbone", "environment"):
        layer_indices["protein"].extend(layer_indices.get(part + "_atoms", []))
        layer_indices["protein_bonds"].extend(layer_indices.get(part + "_bonds", []))
    add("highlight", x=[], y=[], z=[], mode="markers", name="Selected radius", visible=False, opacity=.95,
        marker=dict(size=12.5, color=[], colorscale="Viridis", cmin=0, cmax=2., showscale=False),
        hovertemplate="Selected free radius: %{marker.color:.2f} Å<extra></extra>")
    fig.update_layout(autosize=False, showlegend=False, paper_bgcolor="#f8fafc", plot_bgcolor="#f8fafc", margin=dict(l=0, r=0, t=0, b=0),
                      font=dict(family="Arial, sans-serif", color="#172e48"), uirevision="ligand-viewer",
                      scene=dict(aspectmode="data", dragmode="orbit", bgcolor="#f8fafc",
                                 xaxis=dict(title="X, Å", visible=False), yaxis=dict(title="Y, Å", visible=False),
                                 zaxis=dict(title="Z, Å", visible=False), camera=dict(eye=dict(x=1.15, y=1.15, z=.95))))
    for k, axis in enumerate(("xaxis", "yaxis", "zaxis")):
        fig.update_layout(scene={axis: dict(range=[float(ligand.xyz[:, k].min()) - 3,
                                                   float(ligand.xyz[:, k].max()) + 3], autorange=False)})
    from plotly.offline import get_plotlyjs
    figure_json = fig.to_json().replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    # Draw once at the final viewport size. A pixel ratio of one avoids the
    # default fourfold WebGL buffer cost on large displays.
    config = json.dumps(dict(displaylogo=False, scrollZoom=True, responsive=False, showLink=False,
                             plotGlPixelRatio=1, modeBarButtonsToRemove=["toImage"], doubleClick="reset"))
    plot = ('<div id="scene" style="height:100%;width:100%"></div><script>' + get_plotlyjs() + '</script>'
            '<script>(function(){const spec=' + figure_json + ';const viewport=document.getElementById("viewport");'
            'spec.layout.width=viewport.clientWidth;spec.layout.height=viewport.clientHeight;'
            'Plotly.newPlot("scene",spec.data,spec.layout,' + config + ').then(function(){'
            'window.ligandViewerReady=true;window.dispatchEvent(new Event("ligand-viewer-ready"));});})();</script>')
    data = dict(x=points[:, 0].tolist(), y=points[:, 1].tolist(), z=points[:, 2].tolist(), gaps=gaps.tolist(),
                layers=layer_indices, corridors=corridors, total_points=total_points,
                atoms=[{key: a[key] for key in ("name", "atom_index", "explicit_hydrogens", "xyz", "local_space",
                                                "nearest_environment", "representative_directions")} for a in report["atoms"]],
                center=ligand.xyz.mean(axis=0).tolist(), bounds=[ligand.xyz.min(axis=0).tolist(), ligand.xyz.max(axis=0).tolist()])
    # User-controlled molecular strings cannot terminate the script element.
    encoded = json.dumps(data, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    save_name = report.get("save_name") or snapshot.get("save_name")
    title = "Ligand viewer" + (f" · {escape(save_name)}" if save_name else "")
    html = TEMPLATE.replace("__TITLE__", title).replace("__PLOT__", plot).replace("__DATA__", encoded)
    path.write_text(html, encoding="utf-8")


TEMPLATE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
*{box-sizing:border-box}html,body{width:100%;height:100%;overflow:hidden}body{margin:0;background:#f8fafc;color:#172e48;font:14px/1.5 system-ui,Arial,sans-serif}
header{height:76px;padding:13px 23px;border-bottom:1px solid #dbe4ef;display:flex;align-items:center;justify-content:space-between;gap:16px;background:#fff}
h1{font-size:18px;line-height:1.35;margin:0}header p{margin:2px 0 0;color:#64748b;font-size:12px}.badge{border:1px solid #d5e5df;color:#24765b;border-radius:20px;padding:4px 10px;font-size:12px;white-space:nowrap}
main{display:grid;grid-template-columns:minmax(0,1fr) 310px;grid-template-rows:minmax(0,1fr);height:calc(100vh - 76px);min-height:0;overflow:hidden}#viewport{position:relative;min-width:0;min-height:0;overflow:hidden}.plot-host{position:absolute;inset:0}.plot-host,.plot-host>div{height:100%;width:100%}#scene{height:100%!important;width:100%!important}
aside{background:#fff;border-left:1px solid #dbe4ef;overflow:auto;min-height:0;padding:18px}h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;margin:0 0 12px;color:#64748b}.section{border-bottom:1px solid #e2e8f0;padding-bottom:16px;margin-bottom:16px}
body.controls-hidden main{grid-template-columns:minmax(0,1fr)}body.controls-hidden aside{display:none}
button,select{font:inherit;border:1px solid #cbd5e1;border-radius:7px;background:#fff;color:#172e48;padding:6px 10px;cursor:pointer}button:hover{background:#eff6ff}button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}
.tools{position:absolute;z-index:3;left:16px;top:14px;display:flex;gap:6px;flex-wrap:wrap}.help{position:absolute;left:16px;bottom:15px;max-width:440px;background:#ffffffdf;border:1px solid #dbe4ef;padding:7px 11px;border-radius:8px;color:#64748b;font-size:12px;pointer-events:none}
.scale-row{display:flex;gap:16px;align-items:stretch}.bar-wrap{position:relative;width:61px;height:208px;flex:none}.bar{height:100%;width:32px;background:linear-gradient(to top,#440154,#3b528b,#21918c,#5ec962,#fde725);border-radius:7px;cursor:crosshair;touch-action:none;position:relative}
.band{position:absolute;left:-3px;right:-3px;height:13px;border:2px solid #fff;box-shadow:0 0 0 1px #334155;border-radius:4px;pointer-events:none;display:none}.ticks{display:flex;flex-direction:column;justify-content:space-between;position:absolute;left:36px;top:-5px;bottom:-5px;color:#64748b;font-size:11px;white-space:nowrap;pointer-events:none}.scale-info{font-size:12px;color:#64748b}#radius-value{display:block;font-size:21px;color:#172e48;line-height:1.3;margin-bottom:8px}.selected-note{font-size:11px;color:#946315}#selection-state{min-height:18px}
.hint{font-size:12px;color:#64748b;margin:8px 0}label.row{display:flex;align-items:center;gap:7px;margin:7px 0;font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:var(--dot);display:inline-block;flex:none}input[type=checkbox]{accent-color:#3477cc}input[type=range]{width:100%;accent-color:#3477cc;cursor:pointer}label.slider{display:block;margin:12px 0 0;font-size:12px}output{float:right;color:#64748b}
.layer-group+.layer-group{border-top:1px solid #e2e8f0;margin-top:16px;padding-top:16px}h3{font-size:13px;margin:0 0 9px}button:disabled,input:disabled{cursor:default;opacity:.5}
details summary{cursor:pointer;font-size:13px;font-weight:600}details p{font-size:12px;color:#64748b}#atom-info,#corridor-info{font-size:12px;margin-top:10px;min-height:58px}#atom-select,#corridor-select{width:100%}.metric{display:flex;justify-content:space-between;margin:6px 0;gap:8px}.metric span{color:#64748b}.primary{background:#edf5ff}.corridor-key{width:18px;height:3px;position:relative;background:#df841b;flex:none}.corridor-key:after{content:'';position:absolute;right:0;top:-2px;width:7px;height:7px;border-radius:50%;background:#df841b}
@media(max-width:760px){html,body{height:auto;overflow:auto}main{grid-template-columns:1fr;grid-template-rows:auto;height:auto;overflow:visible}#viewport{height:70vh;min-height:380px}aside{border-left:0;border-top:1px solid #dbe4ef}header{height:auto;min-height:76px}.badge{display:none}.help{max-width:75%}}
</style></head><body><header><div><h1>__TITLE__</h1><p>Explore space around the saved ligand. Coordinates are preserved.</p></div><span class="badge">Offline report</span></header>
<main><div id="viewport"><div class="tools"><button id="reset">Reset view</button><button id="focus">Focus ligand</button><button id="clear">Clear selection</button><button id="image">Save image</button><button id="toggle-controls" aria-expanded="true">Hide controls</button></div><div class="plot-host">__PLOT__</div><div class="help">Drag to rotate · Scroll to zoom · Right-drag to pan · Click a ligand atom for details</div></div>
<aside><section class="section"><h2>Layers</h2>
<div class="layer-group"><h3>Ligand</h3><div id="ligand-layers"></div><label class="row"><input id="labels" type="checkbox" checked>Atom labels</label><div id="growth-layers"></div><label class="row"><input id="corridor-labels" type="checkbox" checked>Corridor labels</label></div>
<div class="layer-group"><h3>Protein / environment</h3><div id="protein-layers"></div>
<label class="slider">Protein size <output id="protein-size-out">8</output><input id="protein-size" type="range" min="1" max="12" step="0.5" value="8"></label>
<label class="slider">Protein opacity <output id="protein-opacity-out">25%</output><input id="protein-opacity" type="range" min="0" max="1" step="0.01" value="0.25"></label><p class="hint">Backbone has darker, thicker bonds; side chains have lighter, thinner bonds. Size and opacity apply to atoms and bonds together.</p></div>
<div class="layer-group" id="free-space-controls"><h3>Free space</h3><label class="row"><input id="layer-space" type="checkbox">Free space points</label>
<label class="row"><input id="clip-foreground" type="checkbox" checked>Clip foreground</label>
<label class="row"><input id="lock-clip-plane" type="checkbox">Lock clipping plane</label>
<label class="slider">Free-space point size <output id="point-size-out">5</output><input id="point-size" type="range" min="1" max="16" step="0.2" value="5"></label>
<label class="slider">Opacity <output id="cloud-opacity-out">22%</output><input id="cloud-opacity" type="range" min="0" max="1" step="0.01" value="0.22"></label>
<h3 style="margin-top:18px">Explore free radius</h3><div class="scale-row"><div class="bar-wrap"><div id="radius-bar" class="bar" tabindex="0" role="slider" aria-label="Highlight empty-sphere radius" aria-valuemin="0" aria-valuemax="2" aria-valuenow="1"><div id="radius-band" class="band"></div></div><div class="ticks"><span>2+</span><span>1</span><span>0 Å</span></div></div><div class="scale-info"><strong id="radius-value">All radii</strong><div id="radius-count"></div><p>Move over the bar to enlarge points of that color. Click to pin the range.</p><div id="selection-state" class="selected-note"></div><span>Arrow keys: adjust · Escape: clear</span></div></div>
<label class="slider">Minimum free radius <output id="minimum-out">0.00 Å</output><input id="minimum" type="range" min="0" max="2" step="0.05" value="0"></label></div>
<div class="layer-group"><h3>View</h3><label class="row"><input id="axes" type="checkbox">Coordinate axes</label></div></section>
<section class="section"><h2>Free X–H corridors</h2><p class="hint">Orange lines show room to extend the ligand along its existing X–H bonds. Hover over a line to see the available space.</p><select id="corridor-select" aria-label="Select free X–H corridor"><option value="">Choose a direction…</option></select><div id="corridor-info"></div></section>
<section class="section"><h2>Ligand atom details</h2><select id="atom-select" aria-label="Select ligand atom"><option value="">Choose an atom…</option></select><div id="atom-info">Click an atom or choose one above to inspect its local free space.</div></section>
<details><summary>How to read this map</summary><p>The cloud shows possible centers of empty spheres. Color is distance to the nearest heavy-atom LJ surface; yellow includes radii of 2 Å or more. Marker sizes are visual controls, not atomic radii.</p><p>Orange diamonds mark atoms with explicit H and nearby free volume. Orange lines show potential growth directions along existing X–H bonds, starting beyond the source atom's surface. An unblocked line ends where the check stops; free space may continue farther.</p><p>Geometry alone does not evaluate valence, chemical suitability, binding energy or Foldit score. Local volumes overlap and must not be added together. Protein hydrogens are excluded from the steric model.</p><p id="sampling-note"></p><p>The source filename is shown in the title. Player metadata, notes, user IDs and local file paths are not exported. The report runs without a network connection.</p></details></aside></main>
<script>
"use strict";
function initializeViewer(){
const DATA=__DATA__, gd=document.getElementById('scene'), $=id=>document.getElementById(id);
const idx=key=>DATA.layers[key], cloud=idx('space'),high=idx('highlight');
let pinnedCorridor=null,cloudStamp='',radius=null,pinned=false,lockedPlaneNormal=null;
// Keep one WebGL update in flight; slider/resize bursts retain the latest state.
let rendering=false, renderFrame=null, interacting=false;
const renderJobs=new Map(), styleJobs=new Map();let layoutJob={};
function nextRender(){return [...renderJobs.entries()].find(([key])=>!interacting||key==='cloud');}
function requestRender(){if(!rendering&&renderFrame===null&&nextRender())renderFrame=requestAnimationFrame(runRender);}
function scheduleRender(key,operation){renderJobs.set(key,operation);requestRender();}
function runRender(){
 renderFrame=null;const next=nextRender();if(rendering||!next)return;
 const [key,operation]=next;renderJobs.delete(key);rendering=true;
 let result;try{result=operation();}catch(error){finishRender(error);return;}
 Promise.resolve(result).then(()=>finishRender(),finishRender);
}
function finishRender(error){rendering=false;if(error){cloudStamp='';console.error('Ligand viewer update failed',error);}requestRender();}
function preserveView(){const scene=gd._fullLayout?.scene?._scene;if(gd.layout&&typeof scene?.saveLayout==='function')scene.saveLayout(gd.layout);}
function finishInteraction(){if(!interacting)return;interacting=false;preserveView();refreshForCamera();requestRender();}
gd.addEventListener('pointerdown',()=>{interacting=true;});
gd.addEventListener('pointerup',finishInteraction);gd.addEventListener('pointercancel',finishInteraction);
window.addEventListener('pointerup',finishInteraction);window.addEventListener('pointercancel',finishInteraction);
function styleUpdate(key,update,indices){
 const job=styleJobs.get(key)||{update:{},indices};Object.assign(job.update,update);styleJobs.set(key,job);
 scheduleRender('style:'+key,()=>{const next=styleJobs.get(key);styleJobs.delete(key);preserveView();return Plotly.restyle(gd,next.update,next.indices);});
}
function layoutUpdate(update){Object.assign(layoutJob,update);scheduleRender('layout',()=>{const next=layoutJob;layoutJob={};preserveView();return Plotly.relayout(gd,next);});}
const layerGroups=[['ligand-layers',[['ligand','Ligand atoms','#3977ec'],['ligand_bonds','Ligand bonds','#334155']]],['growth-layers',[['sites','Atoms with H and nearby space','#ea9b32'],['rays','Free X–H corridors','#e38b25']]],['protein-layers',[['backbone','Protein backbone','#365b7d'],['sidechains','Protein side chains','#94a3b8'],['environment','Other environment','#9c82af']]]];
layerGroups.forEach(([group,rows])=>rows.filter(([key])=>idx(key)?.length).forEach(([key,label,color])=>{const line=document.createElement('label');line.className='row';const box=document.createElement('input');box.type='checkbox';box.checked=true;box.id='layer-'+key;box.onchange=()=>styleUpdate('layer-'+key,{visible:box.checked},idx(key));const dot=document.createElement('span');dot.className=key==='rays'?'corridor-key':'dot';dot.style.setProperty('--dot',color);line.append(box,dot,document.createTextNode(label));$(group).append(line);}));
function foregroundNormal(){
 const scene=gd._fullLayout?.scene??gd.layout?.scene??{};
 const camera=scene._scene?.getCamera?.()??gd.layout?.scene?.camera??{};
 const eye=camera.eye??{x:1.15,y:1.15,z:.95},target=camera.center??{x:0,y:0,z:0};
 // Camera coordinates live in Plotly's scaled scene; transform its plane
 // normal back to data coordinates, including unequal axis ranges/aspects.
 const normal=['x','y','z'].map((key,k)=>{const range=scene[key+'axis']?.range??[DATA.bounds[0][k]-3,DATA.bounds[1][k]+3];return (eye[key]-target[key])*(scene.aspectratio?.[key]??1)/Math.max(1e-8,Math.abs(range[1]-range[0]));});
 const length=Math.hypot(...normal);return length>1e-12?normal.map(v=>v/length):[0,0,1];
}
function drawCloud(updatePlot){
 const visible=$('layer-space').checked,size=Number($('point-size').value),opacity=Number($('cloud-opacity').value);
 const minimum=Number($('minimum').value);
 const normal=visible&&$('clip-foreground').checked?(lockedPlaneNormal??foregroundNormal()):null;
 const stamp=[visible,size,opacity,minimum,radius,...(normal??[])].join('/');if(updatePlot&&stamp===cloudStamp)return;
 const all=[[],[],[],[]],chosen=[[],[],[],[]],center=DATA.center;let hidden=0;
 for(let i=0;i<DATA.gaps.length;i++){
  const gap=DATA.gaps[i];if(gap<minimum)continue;
  if(normal&&normal[0]*(DATA.x[i]-center[0])+normal[1]*(DATA.y[i]-center[1])+normal[2]*(DATA.z[i]-center[2])>1e-8){hidden++;continue;}
  const values=[DATA.x[i],DATA.y[i],DATA.z[i],gap];values.forEach((v,k)=>all[k].push(v));
  if(radius!==null&&(radius>=1.9?gap>=1.88:Math.abs(gap-radius)<.12))values.forEach((v,k)=>chosen[k].push(v));
 }
 $('radius-count').textContent=visible?(radius===null?all[0].length.toLocaleString()+' visible points':chosen[0].length.toLocaleString()+' highlighted points')+(hidden?' · '+hidden.toLocaleString()+' cut away':''):'Free-space points hidden';
 if(updatePlot){preserveView();cloudStamp=stamp;return Plotly.restyle(gd,{x:[all[0],chosen[0]],y:[all[1],chosen[1]],z:[all[2],chosen[2]],'marker.color':[all[3],chosen[3]],'marker.size':[size,Math.max(7,size*2.5)],opacity:[radius===null?opacity:Math.min(opacity,.08),.95],visible},[...cloud,...high]);}
}
function refresh(updatePlot=true){
 $('minimum-out').textContent=Number($('minimum').value).toFixed(2)+' Å';
 $('radius-value').textContent=radius===null?'All radii':radius>=1.9?'≥ 1.88 Å':Math.max(0,radius-.12).toFixed(2)+'–'+(radius+.12).toFixed(2)+' Å';
 $('radius-band').style.display=radius===null?'none':'block';
 if(radius!==null){$('radius-band').style.top='calc('+(100*(1-radius/2))+'% - 6px)';$('radius-bar').setAttribute('aria-valuenow',radius.toFixed(2));}
 $('radius-bar').setAttribute('aria-valuetext',$('radius-value').textContent);
 $('selection-state').textContent=pinned?'Range pinned · click bar to release':'';
 if(updatePlot)scheduleRender('cloud',()=>drawCloud(true));else drawCloud(false);
}
function refreshForCamera(){if($('layer-space').checked&&$('clip-foreground').checked&&!lockedPlaneNormal)refresh();}
// Recompute during rotation, not only on mouse release. The render queue
// coalesces camera events and reads the latest camera when the job runs.
gd.on('plotly_relayouting',refreshForCamera);gd.on('plotly_relayout',refreshForCamera);
$('layer-space').onchange=()=>refresh();$('clip-foreground').onchange=()=>refresh();
$('lock-clip-plane').onchange=()=>{lockedPlaneNormal=$('lock-clip-plane').checked?foregroundNormal():null;refresh();};
function pickRadius(event){const bar=$('radius-bar').getBoundingClientRect();return Math.max(0,Math.min(2,2*(1-(event.clientY-bar.top)/bar.height)));}
$('radius-bar').addEventListener('pointermove',event=>{if(!pinned){radius=pickRadius(event);refresh();}});
$('radius-bar').addEventListener('pointerleave',()=>{if(!pinned){radius=null;refresh();}});
$('radius-bar').addEventListener('click',event=>{pinned=!pinned;radius=pickRadius(event);refresh();});
$('radius-bar').addEventListener('keydown',event=>{
 if(event.key==='ArrowUp'||event.key==='ArrowDown'){event.preventDefault();pinned=true;radius=Math.max(0,Math.min(2,(radius??1)+(event.key==='ArrowUp'?.05:-.05)));refresh();}
 if(event.key==='Enter'||event.key===' '){event.preventDefault();pinned=!pinned;radius=radius??1;refresh();}
});
function clear(){pinned=false;radius=null;pinnedCorridor=null;showCorridor(null);refresh();}$('clear').onclick=clear;document.addEventListener('keydown',event=>{if(event.key==='Escape')clear();});
['minimum','point-size','cloud-opacity'].forEach(id=>$(id).addEventListener('input',()=>{if(id!=='minimum')$(id+'-out').textContent=id==='cloud-opacity'?Math.round(Number($(id).value)*100)+'%':$(id).value;refresh();}));
const proteinTargets=[...idx('protein'),...idx('protein_bonds')];
['protein-size','protein-opacity'].forEach(id=>$(id).addEventListener('input',()=>{const v=Number($(id).value);$(id+'-out').textContent=id==='protein-opacity'?Math.round(v*100)+'%':v.toFixed(1);styleUpdate('protein',id==='protein-size'?{'marker.size':proteinTargets.map(i=>v*(idx('backbone').includes(i)?1.15:1)),'line.width':proteinTargets.map(i=>Math.max(.5,v*(idx('backbone').includes(i)?1.25:.7)))}:{opacity:v},proteinTargets);}));
$('labels').onchange=()=>styleUpdate('labels',{mode:$('labels').checked?'markers+text':'markers'},idx('ligand'));
$('corridor-labels').onchange=()=>styleUpdate('corridor-labels',{mode:$('corridor-labels').checked?'lines+markers+text':'lines+markers'},idx('rays'));
$('axes').onchange=()=>layoutUpdate({'scene.xaxis.visible':$('axes').checked,'scene.yaxis.visible':$('axes').checked,'scene.zaxis.visible':$('axes').checked});
function focusLigand(){let update={'scene.camera':{eye:{x:1.15,y:1.15,z:.95},center:{x:0,y:0,z:0},up:{x:0,y:0,z:1}}};['x','y','z'].forEach((axis,k)=>{update['scene.'+axis+'axis.range']=[DATA.bounds[0][k]-3,DATA.bounds[1][k]+3];update['scene.'+axis+'axis.autorange']=false;});layoutUpdate(update);}
$('focus').onclick=focusLigand;$('reset').onclick=()=>layoutUpdate({'scene.xaxis.autorange':true,'scene.yaxis.autorange':true,'scene.zaxis.autorange':true,'scene.camera':{eye:{x:1.35,y:1.35,z:1.1},center:{x:0,y:0,z:0},up:{x:0,y:0,z:1}}});
let lastWidth=$('viewport').clientWidth,lastHeight=$('viewport').clientHeight;
function resizeScene(){const viewport=$('viewport'),width=viewport.clientWidth,height=viewport.clientHeight;if(width>0&&height>0&&(Math.abs(width-lastWidth)>1||Math.abs(height-lastHeight)>1)){lastWidth=width;lastHeight=height;layoutUpdate({autosize:false,width,height});}}
$('toggle-controls').onclick=()=>{const hidden=document.body.classList.toggle('controls-hidden');$('toggle-controls').textContent=hidden?'Show controls':'Hide controls';$('toggle-controls').setAttribute('aria-expanded',String(!hidden));resizeScene();};
$('image').onclick=()=>Plotly.downloadImage(gd,{format:'png',filename:'ligand-viewer',width:1600,height:1100,scale:1});
DATA.atoms.forEach(a=>{const o=document.createElement('option');o.value=a.atom_index;o.textContent=a.name+' · atom '+a.atom_index;$('atom-select').append(o);});
function showAtom(id){const a=DATA.atoms.find(a=>a.atom_index===Number(id));$('atom-select').value=a?String(a.atom_index):'';const panel=$('atom-info');panel.replaceChildren();if(!a){panel.textContent='Choose a ligand atom to inspect its local free space.';return;}const metrics=[['Explicit hydrogens',a.explicit_hydrogens],['Free volume within 4 Å',a.local_space['4.0'].free_volume.toFixed(1)+' Å³'],['1 Å probe volume within 4 Å',a.local_space['4.0'].probe_volume_profile['1.0'].toFixed(1)+' Å³'],['Nearest environment surface',a.nearest_environment.surface_gap.toFixed(2)+' Å'],['Longest representative corridor',Math.max(0,...a.representative_directions.map(r=>r.free_length)).toFixed(2)+' Å']];metrics.forEach(([label,value])=>{const row=document.createElement('div');row.className='metric';const name=document.createElement('span'),val=document.createElement('strong');name.textContent=label;val.textContent=value;row.append(name,val);panel.append(row);});}
let corridorPanelKey='';
function showCorridor(c){
 const next=c?c.id:null;
 const panelKey=String(next)+'/'+String(pinnedCorridor);if(panelKey===corridorPanelKey)return;corridorPanelKey=panelKey;
 const panel=$('corridor-info');panel.replaceChildren();$('corridor-select').value=c?String(c.id):'';
 if(!c){panel.textContent=DATA.corridors.length?DATA.corridors.length+' measured directions. Select an orange handle or choose one above.':'No free X–H corridor was found at this resolution.';return;}
 const title=document.createElement('strong');title.textContent=c.atom_name+' → '+c.hydrogen;panel.append(title);
 const row=document.createElement('div');row.className='metric';const name=document.createElement('span'),val=document.createElement('strong');name.textContent='Room beyond the atom';val.textContent=c.length_label;row.append(name,val);panel.append(row);
 const note=document.createElement('p');note.className='hint';note.textContent=c.end_label;panel.append(note);
}
function corridorAt(point){if(!point||!idx('rays').includes(point.curveNumber))return null;const id=point.customdata??Math.floor(point.pointNumber/4);return DATA.corridors.find(c=>c.id===id)??null;}
DATA.corridors.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.atom_name+' → '+c.hydrogen+' · '+c.length.toFixed(2)+' Å';$('corridor-select').append(o);});
$('corridor-select').onchange=()=>{const c=DATA.corridors.find(c=>String(c.id)===$('corridor-select').value);pinnedCorridor=c?c.id:null;showCorridor(c??null);if(c)showAtom(c.atom_index);};
$('atom-select').onchange=()=>showAtom($('atom-select').value);
gd.on('plotly_hover',event=>{if(rendering)return;const c=corridorAt(event.points[0]);showCorridor(c??DATA.corridors.find(c=>c.id===pinnedCorridor)??null);});
// Plotly can emit unhover while redrawing; only an actual pointer exit clears it.
gd.addEventListener('pointerleave',()=>showCorridor(DATA.corridors.find(c=>c.id===pinnedCorridor)??null));
gd.on('plotly_click',event=>{const p=event.points[0];if(!p)return;const c=corridorAt(p);if(c){pinnedCorridor=c.id;showCorridor(c);showAtom(c.atom_index);}else if(idx('ligand').includes(p.curveNumber)||idx('sites').includes(p.curveNumber))showAtom(p.customdata);});
$('sampling-note').textContent='Display: '+DATA.gaps.length.toLocaleString()+' of '+DATA.total_points.toLocaleString()+' nearby free voxels; the full grid and per-atom measurements are saved beside this HTML.';
showCorridor(null);refresh(false);
if(typeof ResizeObserver!=='undefined')new ResizeObserver(resizeScene).observe($('viewport'));
window.addEventListener('resize',resizeScene);
}
if(window.ligandViewerReady)initializeViewer();else window.addEventListener('ligand-viewer-ready',initializeViewer,{once:true});
</script></body></html>'''


class GenerationCancelled(Exception):
    """Raised at cooperative cancellation points; no partial report is published."""


@dataclass(frozen=True)
class MapSettings:
    quality: str = "Standard"
    reach: float = 8.0
    probe_radius: float = 0.0

    def parameters(self):
        choices = {"Fast": (.75, 96), "Standard": (.5, 256), "Fine": (.35, 384)}
        if self.quality not in choices or not 4 <= self.reach <= 12 or not 0 <= self.probe_radius <= 2:
            raise ValueError("Choose a quality preset, a reach from 4 to 12 Å and a probe radius from 0 to 2 Å.")
        step, directions = choices[self.quality]
        return dict(reach=self.reach, step=step, directions=directions, probe_radius=self.probe_radius)


def generate_from_save(save, database, output_root, *, settings=None, cancel=None, progress=lambda fraction, message: None):
    """Publish a complete report directory only after every ligand succeeds."""
    def check():
        if cancel is not None and cancel.is_set():
            raise GenerationCancelled()

    check()
    progress(.01, "Loading the offline analysis tools")
    try:
        _load_numerics()
        import plotly.graph_objects
    except ImportError as error:
        raise RuntimeError("Ligand viewer needs NumPy, SciPy and Plotly. Install the packages from requirements.txt and restart the monitor.") from error
    parameters = (settings or MapSettings()).parameters()
    progress(.03, "Reading the saved coordinates and ligand definitions")
    snapshot, properties = load_save(save, database, check=check)
    measure_properties = {key: {field: spec[field] for field in ("radius", "donor", "acceptor")}
                          for key, spec in properties.items()}
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    staging = root / f".ligand-viewer-{token}"
    puzzle_id = str(snapshot.get("puzzle_id") or "unknown")
    if not re.fullmatch(r"[0-9]+|unknown", puzzle_id):
        puzzle_id = "unknown"
    final = root / f"{datetime.now():%Y%m%d-%H%M%S}.{puzzle_id}"
    staging.mkdir()
    results = []
    try:
        check()
        (staging / "geometry.json").write_text(json.dumps(snapshot, ensure_ascii=True, indent=2), encoding="utf-8")
        used = {a["rosetta_type"] for r in snapshot["residues"] for a in r["atoms"]}
        (staging / "atom_properties.json").write_text(json.dumps({key: properties[key] for key in sorted(used)}, indent=2), encoding="utf-8")
        for i, segment in enumerate(snapshot["ligand_segments"]):
            check()
            suffix = f"_{segment}" if len(snapshot["ligand_segments"]) > 1 else ""

            def measured(fraction, message):
                progress(.08 + .86 * (i + fraction) / len(snapshot["ligand_segments"]),
                         f"Ligand {i + 1} of {len(snapshot['ligand_segments'])} · {message}")

            report, grid, ligand, environment = measure(snapshot, measure_properties, segment, **parameters,
                                                       check=check, progress=measured)
            check()
            measured(.96, "Writing the interactive HTML report")
            save_outputs(staging, report, grid, ligand, environment, suffix)
            write_html(staging / f"ligand_viewer{suffix}.html", report, grid, ligand, environment, snapshot)
            results.append(dict(segment=segment, ligand=report["ligand_name"], ligand_atoms=len(ligand.records),
                                environment_atoms=len(environment.records), relative_html=f"ligand_viewer{suffix}.html",
                                relative_space=f"space{suffix}.json", relative_grid=f"space_grid{suffix}.npz"))
        check()
        manifest = dict(version=VERSION, parameters=parameters, ligands=results, save_name=snapshot.get("save_name"),
                        puzzle_id=snapshot.get("puzzle_id"),
                        geometry_sha256=snapshot["geometry_sha256"])
        (staging / "report.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (staging / "README.md").write_text(
            "# Ligand viewer\n\nOpen ligand_viewer.html in a browser. For multiple ligands, choose a ligand_viewer_<segment>.html file. "
            "No network connection is required.\n\n"
            "Use the grouped layer controls to inspect the ligand and surrounding protein. "
            "Free space points toggles the cloud. Clip foreground hides cloud points between the camera "
            "and the ligand center and follows the camera while rotating. Free-space point size and Opacity adjust the cloud. "
            "Lock clipping plane freezes the current cut until unchecked. "
            "Move over the vertical Explore free radius bar to highlight matching points; click to pin the range. "
            "Use arrow keys to adjust the selection, Escape to clear it, and Minimum free radius to filter smaller gaps.\n\n"
            "All calculations use the saved XYZ coordinates and Foldit's unscaled heavy-atom LJ radii. "
            "Geometry is not optimized. The map measures geometric space, not chemical suitability or binding energy.\n\n"
            "The source filename is shown in the viewer. Player metadata, notes, user IDs and source paths are not exported. "
            "All files share this folder: geometry.json, atom_properties.json, space.json, atoms.csv and space_grid.npz. "
            "For multiple ligands, measurement filenames include the residue segment number.\n",
            encoding="utf-8")
        check()
        # Keep a previous same-second report intact until the replacement is ready.
        previous = root / f".ligand-viewer-replaced-{token}"
        if previous.resolve().parent != root or previous.exists():
            raise RuntimeError("The temporary report destination is already in use.")
        if final.exists():
            if final.resolve().parent != root or final.is_symlink() or not final.is_dir():
                raise RuntimeError("The report destination is not a regular folder.")
            final.rename(previous)
        try:
            staging.rename(final)
        except BaseException:
            if previous.exists():
                previous.rename(final)
            raise
        if previous.exists() and previous.resolve().parent == root:
            shutil.rmtree(previous, ignore_errors=True)
    except BaseException:
        # This is the fresh temporary directory created above, verified inside
        # the intended reports root before any recursive deletion on Windows.
        target = staging.resolve()
        if target.parent == root and target.name == f".ligand-viewer-{token}" and target.exists():
            shutil.rmtree(target)
        raise
    progress(1., "The HTML report is ready")
    return dict(folder=str(final), reports=[dict(row, html=str(final / row["relative_html"])) for row in results])


def initial_save_folder(client_folder, fallback):
    candidate = Path(client_folder) if client_folder else Path(fallback)
    return str(candidate if candidate.is_dir() else Path(fallback))


def latest_save(client_folder):
    """Newest root-level save by modification time, including overwritten saves."""
    if not client_folder:
        return None
    candidates = []
    try:
        for path in Path(client_folder).iterdir():
            if path.suffix.casefold() == ".ir_solution" and path.is_file():
                try:
                    candidates.append((path.stat().st_mtime_ns, path.name.casefold(), path))
                except OSError:
                    continue
    except OSError:
        return None
    return max(candidates)[2] if candidates else None


def center_window(window):
    window.update_idletasks()
    width = min(window.winfo_reqwidth(), max(200, window.winfo_screenwidth() - 40))
    height = min(window.winfo_reqheight(), max(100, window.winfo_screenheight() - 80))
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    y = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.deiconify()


def _native_dialog(dialog, parent, **options):
    """A centered invisible owner also centers native file/error dialogs."""
    anchor = tk.Toplevel(parent)
    anchor.withdraw()
    anchor.transient(parent)
    x, y = parent.winfo_screenwidth() // 2, parent.winfo_screenheight() // 2
    anchor.geometry(f"1x1+{x}+{y}")
    try:
        anchor.attributes("-alpha", 0.)
        anchor.deiconify()
        anchor.update_idletasks()
        return dialog(parent=anchor, **options)
    finally:
        anchor.destroy()


class LigandViewer:
    def __init__(self, root, output_root, fallback_folder, *, settings_provider=None):
        self.root = root
        self.output_root = Path(output_root)
        self.fallback_folder = Path(fallback_folder)
        self.settings_provider = settings_provider or (lambda: {})
        self.job = None
        self.closed = False

    def _show_existing_job(self):
        if self.job is not None:
            self.job.window.lift()
            self.job.window.focus_set()
            return True
        return False

    def open_latest(self, client_folder=None, installation_folder=None):
        if self._show_existing_job():
            return
        selected = latest_save(client_folder)
        if selected is None:
            self.open_save(client_folder, installation_folder)
            return
        self._start_save(selected, client_folder, installation_folder,
                         on_no_ligand=lambda: self.open_save(client_folder, installation_folder,
                             title="No ligand in the latest save — Open another save"))

    def open_save(self, client_folder=None, installation_folder=None, *, title="Ligand viewer — Open save"):
        if self._show_existing_job():
            return
        initial = initial_save_folder(client_folder, self.fallback_folder)
        selected = _native_dialog(filedialog.askopenfilename, self.root, title=title, initialdir=initial,
                                  filetypes=[("Foldit saves", "*.ir_solution"), ("All files", "*")])
        if selected:
            self._start_save(selected, client_folder, installation_folder)

    def _start_save(self, selected, client_folder, installation_folder, *, on_no_ligand=None):
        initial = initial_save_folder(client_folder, self.fallback_folder)
        try:
            database = find_database(Path(selected).parent, installation_folder, client_folder)
        except SaveFormatError:
            folder = _native_dialog(filedialog.askdirectory, self.root,
                                    title="Select the Foldit client folder (atom database)", initialdir=initial)
            if not folder:
                return
            try:
                database = find_database(folder)
            except SaveFormatError as error:
                _native_dialog(messagebox.showerror, self.root, title="Ligand viewer", message=str(error))
                return
        settings = MapSettings(**self.settings_provider())
        settings.parameters()
        self.job = ProgressWindow(self, selected, database, settings, on_no_ligand=on_no_ligand)

    def shutdown(self):
        self.closed = True
        if self.job is not None:
            self.job.cancel.set()


class ProgressWindow:
    def __init__(self, owner, save, database, settings, *, on_no_ligand=None):
        self.owner = owner
        self.on_no_ligand = on_no_ligand
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.window = tk.Toplevel(owner.root)
        self.window.withdraw()
        self.window.title("Ligand viewer — Generating map")
        self.window.transient(owner.root)
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.request_cancel)
        self.window.bind("<Escape>", lambda _event: self.request_cancel())
        body = ttk.Frame(self.window, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Building the ligand-space report", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        self.message = tk.StringVar(self.window, "Preparing the saved geometry…")
        ttk.Label(body, textvariable=self.message, width=62, wraplength=440).pack(anchor="w", pady=(10, 12))
        self.bar = ttk.Progressbar(body, mode="determinate", maximum=100, length=440)
        self.bar.pack(fill="x")
        ttk.Label(body, text="The monitor keeps running while the map is generated.", foreground="#64748b").pack(anchor="w", pady=(8, 12))
        self.cancel_button = ttk.Button(body, text="Cancel", command=self.request_cancel)
        self.cancel_button.pack(anchor="e")
        center_window(self.window)
        self.thread = threading.Thread(target=self.run, args=(save, database, settings), name="LigandViewer", daemon=True)
        self.thread.start()
        self.window.after(75, self.poll)

    def request_cancel(self):
        self.cancel.set()
        self.cancel_button.configure(state="disabled")
        self.message.set("Cancelling…")

    def run(self, save, database, settings):
        try:
            result = generate_from_save(save, database, self.owner.output_root, settings=settings, cancel=self.cancel,
                                        progress=lambda value, message: self.events.put(("progress", value, message)))
            self.events.put(("ready", result))
        except GenerationCancelled:
            self.events.put(("cancelled",))
        except NoLigandError as error:
            self.events.put(("no_ligand", str(error)))
        except Exception as error:
            self.events.put(("error", str(error)))

    def poll(self):
        if self.owner.closed:
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    if not self.cancel.is_set():
                        self.bar["value"] = event[1] * 100
                        self.message.set(event[2])
                    continue
                self.owner.job = None
                self.window.destroy()
                if event[0] == "ready" and not self.cancel.is_set():
                    open_reports(self.owner.root, event[1])
                elif event[0] == "no_ligand" and not self.cancel.is_set() and self.on_no_ligand:
                    self.on_no_ligand()
                elif event[0] in ("error", "no_ligand") and not self.cancel.is_set():
                    _native_dialog(messagebox.showerror, self.owner.root, title="Ligand viewer", message=event[1])
                return
        except queue.Empty:
            self.window.after(75, self.poll)


def open_reports(parent, result):
    """Open completed reports immediately; notify only when opening fails."""
    reports = result.get("reports", [])
    errors = [] if reports else ["No HTML reports were generated."]
    for report in reports:
        try:
            path = Path(report["html"]).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"HTML report not found: {path}")
            if not webbrowser.open(path.as_uri(), new=2):
                raise RuntimeError(f"The default browser could not open: {path}")
        except Exception as error:
            errors.append(str(error))
    if errors:
        _native_dialog(messagebox.showerror, parent, title="Ligand viewer",
                       message="Could not open the report:\n" + "\n".join(errors)
                       + f"\n\nSaved reports:\n{result.get('folder', '')}")
