"""Offline binary, geometry, cancellation and UI regressions for Ligand viewer."""
import ast
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import modules.ligand_viewer as viewer


def integer(value):
    return struct.pack("<I", value)


def text(value):
    encoded = value.encode("ascii")
    return integer(len(encoded)) + encoded


def residue(name, io, positions, kind="AA", definition=b""):
    return (text(name) + text(io) + text("NOT_USED") + integer(0) + integer(1)
            + integer(len(positions)) + text(kind)
            + b"".join(struct.pack("<3d", *xyz) for xyz in positions) + definition)


def embedded_ligand():
    # Ideal coordinates deliberately differ from the current saved pose.
    block = viewer.RESTYPE + text("LIG") + text("LIG") + b"Z" + text("LIG") + text("UNK") + text("UNK") + integer(2)
    for name, atom_type in (("C1", "CH3"), ("H1", "Hapo")):
        block += text(name) + text(atom_type) + text("VIRT") + bytes(12) + struct.pack("<3d", 90, 90, 90)
    return block + integer(1) + text("C1") + text("H1") + integer(1) + b"ENDRESTYPE"


def fixture(root, ligand=True):
    db = root / "database"
    props = db / "chemical/atom_type_sets/fa_standard/atom_properties.txt"
    props.parent.mkdir(parents=True)
    props.write_text("NAME ATOM LJ_RADIUS\nNbb N 1.5 DONOR\nCAbb C 1.5\nCObb C 1.5\nOCbb O 1.4 ACCEPTOR\nCH3 C 1.5\nHapo H 1.0\nVIRT X 0\nCl CL 2.07\n", encoding="utf-8")
    params = db / "chemical/residue_type_sets/fa_standard/residue_types/l-caa/ALA.params"
    params.parent.mkdir(parents=True)
    params.write_text("\n".join(["ATOM N Nbb", "ATOM CA CAbb", "ATOM C CObb", "ATOM O OCbb", "ATOM CB CH3", "ATOM H Hapo", "BOND N CA", "BOND CA C", "BOND C O", "BOND CA CB"]), encoding="utf-8")
    protein = residue("ALA", "ALA", [(7, 0, 0), (8.4, 0, 0), (9.9, 0, 0), (11.1, 0, 0), (8.4, 1.5, 0), (7, -1, 0)])
    lig = residue("LIG", "LIG", [(0, 0, 0), (1, 0, 0)], "LIGAND", embedded_ligand()) if ligand else b""
    virtual = residue("VRT", "XXX", [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    save = root / "private-save-title.ir_solution"
    save.write_bytes(b"\x0b\0\0\0SOLN" + b"PRIVATE_PLAYER_RESI_AND_NOTES" + b"RESI" + integer(3 if ligand else 2) + protein + lig + virtual + b"CON2")
    return save, db


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_current_coordinates_and_embedded_hydrogens_are_exact(self):
        save, database = fixture(self.root)
        before = hashlib.sha256(save.read_bytes()).hexdigest()
        snapshot, properties = viewer.load_save(save, database)
        self.assertEqual(snapshot["ligand_segments"], [2])
        self.assertEqual(len(snapshot["residues"]), 2)
        self.assertEqual(snapshot["residues"][1]["atoms"][0]["xyz"], [0, 0, 0])
        self.assertEqual(snapshot["residues"][1]["atoms"][1]["atomic_number"], 1)
        self.assertEqual(properties["Cl"]["element"], "Cl")
        self.assertNotIn("PRIVATE_PLAYER", json.dumps(snapshot))
        self.assertEqual(hashlib.sha256(save.read_bytes()).hexdigest(), before)

    def test_missing_ligand_and_truncation_fail_clearly(self):
        save, database = fixture(self.root, ligand=False)
        with self.assertRaisesRegex(viewer.SaveFormatError, "No physical ligand"):
            viewer.load_save(save, database)
        save.write_bytes(save.read_bytes().replace(text("ALA"), text("UNK")))
        with self.assertRaises(viewer.NoLigandError):
            viewer.load_save(save, database)
        save.write_bytes(save.read_bytes()[:80])
        with self.assertRaises(viewer.SaveFormatError):
            viewer.load_save(save, database)

    def test_puzzle_number_comes_from_save_header_even_after_file_is_renamed(self):
        save, database = fixture(self.root)
        data = save.read_bytes()
        save.write_bytes(data[:8] + integer(2014419) * 2 + integer(1) + text("PRIVATE_HEADER_PLAYER") + data[8:])
        save = save.rename(self.root / "puzzle_9999999_time_123.ir_solution")
        snapshot, _ = viewer.load_save(save, database)
        self.assertEqual(snapshot["puzzle_id"], "2014419")
        self.assertEqual(snapshot["save_name"], save.name)
        self.assertNotIn("PRIVATE_HEADER_PLAYER", json.dumps(snapshot))

    def test_unknown_variants_and_wrong_atom_counts_are_not_guessed(self):
        _, database = fixture(self.root)
        reader = viewer.ResidueDatabase(database, viewer.read_atom_properties(database))
        with self.assertRaisesRegex(viewer.SaveFormatError, "Unsupported protein variant"):
            reader.protein("ALA:Unknown", 6)
        with self.assertRaisesRegex(viewer.SaveFormatError, "Atom count mismatch"):
            reader.protein("ALA", 7)

    def test_active_database_wins_over_old_components(self):
        _, database = fixture(self.root)
        root = self.root / "client"
        current = root / "cmp-database-current/database"
        current.parent.mkdir(parents=True)
        database.rename(current)
        old = root / "cmp-database-old/database/chemical/atom_type_sets/fa_standard"
        old.mkdir(parents=True)
        (old / "atom_properties.txt").write_text("C C 1.5")
        (root / "versions.json").write_text(json.dumps({"components": {"database": {"version": "current"}}}))
        self.assertEqual(viewer.find_database(root), current)


class GenerationTests(unittest.TestCase):
    def test_report_is_offline_and_contains_only_molecular_data(self):
        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.remote = []
            def handle_starttag(self, tag, attrs):
                if tag == "script" and dict(attrs).get("src"):
                    self.remote.append(dict(attrs)["src"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save, database = fixture(root)
            save = save.rename(root / "puzzle_2014406_time_12345.ir_solution")
            result = viewer.generate_from_save(save, database, root / "reports", settings=viewer.MapSettings("Fast", 4))
            html = Path(result["reports"][0]["html"]).read_text(encoding="utf-8")
            parser = Scripts(); parser.feed(html)
            self.assertEqual(parser.remote, [])
            self.assertIn('id="clip-foreground"', html)
            self.assertIn('id="protein-opacity"', html)
            self.assertIn(f"Ligand viewer · {save.name}", html)
            self.assertRegex(Path(result["folder"]).name, r"^\d{8}-\d{6}\.2014406$")
            self.assertTrue(all(path.is_file() for path in Path(result["folder"]).iterdir()))
            self.assertEqual(Path(result["reports"][0]["html"]).parent, Path(result["folder"]))
            controls = html.split('id="free-space-controls">', 1)[1].split('<div class="layer-group"><h3>View', 1)[0]
            self.assertEqual(controls.count('type="checkbox"'), 3)
            self.assertEqual(controls.count('type="range"'), 3)
            self.assertNotIn('<select', controls)
            self.assertNotIn('<button', controls)
            for label in ('Free space points', 'Clip foreground', 'Lock clipping plane', 'Free-space point size', 'Opacity', 'Explore free radius', 'Minimum free radius'):
                self.assertIn(label, controls)
            self.assertNotIn('keep-ligand-clear', html)
            self.assertNotIn("Click to pin details", html)
            self.assertNotIn("Open to the measurement limit", html)
            self.assertIn("Space for ligand growth", html)
            spec_start = html.index('<script>(function(){const spec=') + len('<script>(function(){const spec=')
            figure, _ = json.JSONDecoder().raw_decode(html[spec_start:])
            self.assertEqual(len(figure["data"]), 10)
            self.assertEqual(figure["data"][0]["marker"]["size"], 5)
            self.assertIn('id="point-size" type="range" min="1" max="16" step="0.2" value="5"', html)
            self.assertFalse(figure["data"][0]["visible"])
            corridors = next(trace for trace in figure["data"] if trace['name'] == 'Free X–H corridors')
            self.assertNotEqual(corridors.get("hoverinfo"), "skip")
            self.assertIn("hovertemplate", corridors)
            self.assertNotIn("Geometry hint; chemical suitability is not evaluated", html)
            self.assertIn('id="protein-size" type="range" min="1" max="12" step="0.5" value="8"', html)
            self.assertIn('id="protein-opacity" type="range" min="0" max="1" step="0.01" value="0.25"', html)
            for index in (1, 2, 7, 8):
                self.assertEqual(figure["data"][index]["opacity"], .25)
            self.assertNotIn("ray_highlight", html)
            self.assertIn('spec.layout.width=viewport.clientWidth', html)
            self.assertIn('"plotGlPixelRatio": 1', html)
            self.assertNotIn("gd.on('plotly_unhover'", html)
            self.assertIn("gd.on('plotly_relayouting',refreshForCamera)", html)
            self.assertNotIn('Ligand cutting plane', html)
            self.assertLess(html.index('id="ligand-layers"'), html.index('id="labels"'))
            self.assertLess(html.index('id="growth-layers"'), html.index('id="corridor-labels"'))
            self.assertLess(html.index('id="protein-layers"'), html.index('id="protein-size"'))
            self.assertLess(html.index('id="layer-space"'), html.index('id="clip-foreground"'))
            for path in Path(result["folder"]).rglob("*"):
                if path.suffix in (".html", ".json", ".md", ".csv"):
                    content = path.read_text(encoding="utf-8-sig")
                    for private in ("PRIVATE_PLAYER_RESI_AND_NOTES", "private-save-title", str(root)):
                        self.assertNotIn(private, content)
            self.assertEqual(result["reports"][0]["ligand_atoms"], 1)
            self.assertEqual(result["reports"][0]["environment_atoms"], 5)

    def test_multiple_ligands_share_a_flat_folder_and_same_second_replaces_only_that_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save, database = fixture(root)
            snapshot, properties = viewer.load_save(save, database)
            extra = json.loads(json.dumps(snapshot["residues"][1]))
            extra["segment"] = 3
            for atom in extra["atoms"]:
                atom["xyz"][1] += 3
            snapshot["residues"].append(extra)
            snapshot["ligand_segments"].append(3)
            snapshot["puzzle_id"] = "2014406"
            output = root / "reports"
            with mock.patch.object(viewer, "load_save", return_value=(snapshot, properties)), \
                    mock.patch.object(viewer, "datetime", wraps=viewer.datetime) as clock:
                clock.now.return_value = viewer.datetime(2026, 10, 26, 10, 40, 2)
                result = viewer.generate_from_save(save, database, output, settings=viewer.MapSettings("Fast", 4))
                folder = Path(result["folder"])
                self.assertEqual(folder.name, "20261026-104002.2014406")
                self.assertEqual([Path(row["html"]).name for row in result["reports"]],
                                 ["ligand_viewer_2.html", "ligand_viewer_3.html"])
                self.assertTrue(all(path.is_file() for path in folder.iterdir()))
                for segment in (2, 3):
                    self.assertTrue((folder / f"space_{segment}.json").is_file())
                    self.assertTrue((folder / f"space_grid_{segment}.npz").is_file())
                    self.assertTrue((folder / f"atoms_{segment}.csv").is_file())
                original = (folder / "report.json").read_bytes()
                with mock.patch.object(viewer, "write_html", side_effect=RuntimeError("write failed")):
                    with self.assertRaisesRegex(RuntimeError, "write failed"):
                        viewer.generate_from_save(save, database, output, settings=viewer.MapSettings("Fast", 4))
                self.assertEqual((folder / "report.json").read_bytes(), original)
                (folder / "obsolete.txt").write_text("old report")
                sentinel = output / "keep.txt"
                sentinel.write_text("unrelated")
                replacement = viewer.generate_from_save(save, database, output, settings=viewer.MapSettings("Fast", 4))
                self.assertEqual(replacement["folder"], result["folder"])
                self.assertFalse((folder / "obsolete.txt").exists())
                self.assertEqual(sentinel.read_text(), "unrelated")
                self.assertEqual(sorted(path.name for path in output.iterdir()), [folder.name, "keep.txt"])

    def test_protein_parts_keep_peptide_and_branch_bonds_and_separate_other_ligands(self):
        viewer._load_numerics()
        np = viewer.np
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save, database = fixture(root)
            snapshot, properties = viewer.load_save(save, database)
            protein, target = snapshot["residues"]
            for atom, xyz in zip(protein["atoms"], ((2.8, 0, 0), (4.2, 0, 0), (5.7, 0, 0),
                                                     (6.9, 0, 0), (4.2, 1.5, 0), (2.8, -1, 0))):
                atom["xyz"] = list(xyz)
            second = json.loads(json.dumps(protein))
            second["segment"] = 2
            second["atoms"][0]["xyz"] = [6.8, .3, 0]
            terminal = dict(second["atoms"][3], name="OXT", xyz=[5, -1, 0])
            second["atoms"].append(terminal)
            second["bonds"].append(dict(a=3, b=len(second["atoms"])))
            other = dict(segment=3, is_ligand=True, residue_name="COF", bonds=[],
                         atoms=[dict(protein["atoms"][0], xyz=[3, 0, 1])])
            target["segment"] = 4
            snapshot["residues"] = [protein, second, other, target]
            radii = {key: {field: spec[field] for field in ("radius", "donor", "acceptor")}
                     for key, spec in properties.items()}
            ligand, environment, _ = viewer.read_atoms(snapshot, radii, 4)
            report = dict(segment=4, ligand_name="LIG", parameters=dict(probe_radius=0),
                          ligand_bonds=target["bonds"], atoms=[])
            grid = dict(environment_surface_clearance=np.ones((1, 1, 1)),
                        ligand_surface_clearance=np.ones((1, 1, 1)), points=np.array([[1., 1., 1.]]))
            path = root / "viewer.html"
            viewer.write_html(path, report, grid, ligand, environment, snapshot)
            html = path.read_text(encoding="utf-8")
            figure, _ = json.JSONDecoder().raw_decode(html.split('<script>(function(){const spec=', 1)[1])
            traces = {trace["name"]: trace for trace in figure["data"]}
            backbone = traces["Protein backbone atoms"]
            sidechains = traces["Protein side chain atoms"]
            self.assertEqual(len(backbone["x"]), 9)
            self.assertEqual(len(sidechains["x"]), 2)
            self.assertEqual(len(traces["Other environment atoms"]["x"]), 1)
            self.assertTrue(any("OXT" in item for item in backbone["hovertext"]))
            self.assertTrue(all("CB" in item for item in sidechains["hovertext"]))
            self.assertEqual(backbone["marker"]["size"], 8 * 1.15)
            backbone_bonds = traces["Protein backbone bonds"]
            sidechain_bonds = traces["Protein side chain bonds"]
            self.assertGreater(backbone_bonds["line"]["width"], sidechain_bonds["line"]["width"])
            self.assertEqual(len(backbone_bonds["x"]) // 3, 8)  # Six intra-residue bonds, OXT and one peptide bond.
            self.assertEqual(len(sidechain_bonds["x"]) // 3, 2)  # CA–CB stays with its side chain.
            data, _ = json.JSONDecoder().raw_decode(html.split('const DATA=', 1)[1])
            self.assertEqual(data["layers"]["backbone"], [7, 8])
            self.assertEqual(data["layers"]["sidechains"], [1, 2])
            self.assertEqual(data["layers"]["environment"], [9, 10])
            self.assertEqual(data["layers"]["protein"], [2, 8, 10])

    def test_cancel_during_measurement_removes_only_the_new_partial_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save, database = fixture(root)
            cancel = threading.Event()
            reports = root / "reports"
            reports.mkdir()
            sentinel = reports / "existing-report.txt"
            sentinel.write_text("keep")
            def progress(_fraction, message):
                if "Measuring the empty-space grid" in message:
                    cancel.set()
            with self.assertRaises(viewer.GenerationCancelled):
                viewer.generate_from_save(save, database, reports, cancel=cancel, progress=progress,
                                          settings=viewer.MapSettings("Fast", 4))
            self.assertEqual(list(reports.iterdir()), [sentinel])
            self.assertTrue(save.exists())

    def test_failure_while_writing_does_not_publish_a_partial_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save, database = fixture(root)
            with mock.patch.object(viewer, "write_html", side_effect=RuntimeError("write failed")):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    viewer.generate_from_save(save, database, root / "reports", settings=viewer.MapSettings("Fast", 4))
            self.assertEqual(list((root / "reports").iterdir()), [])

    def test_exact_ray_intersection_and_cancellation_inside_grid_chunks(self):
        viewer._load_numerics()
        np = viewer.np
        hit, atom = viewer.ray_first_hit(np.zeros(3), [[1, 0, 0], [-1, 0, 0]], np.array([[5., 0, 0]]), np.array([1.]), 2, 8)
        np.testing.assert_allclose(hit, [4, 8])
        np.testing.assert_array_equal(atom, [0, -1])
        with self.assertRaises(viewer.GenerationCancelled):
            viewer.surface_clearance(np.zeros((4, 3)), np.zeros((1, 3)), np.ones(1), check=mock.Mock(side_effect=viewer.GenerationCancelled))


class UiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for the HTML control smoke test")
    def test_html_dynamic_foreground_cut_and_protein_controls(self):
        # Exercise the controls and camera events without browser automation.
        stub = r'''
const assert=require('node:assert/strict');
class Element {
 constructor(id=''){this.id=id;this.value='';this.checked=true;this.children=[];this.listeners={};this.style={setProperty(){}};this.clientWidth=1200;this.clientHeight=900;}
 append(...values){this.children.push(...values);values.forEach(v=>{if(v.id)elements[v.id]=v;});}
 replaceChildren(){this.children=[];}
 addEventListener(name,fn){this.listeners[name]=fn;}
 setAttribute(name,value){this[name]=value;}
 getBoundingClientRect(){return {top:0,height:200};}
 on(name,fn){this.listeners[name]=fn;}
}
const elements={}, frames=[], completions=[], calls=[], layouts=[], observers=[], classes=new Set();
let active=0,maxActive=0,feedback=false;
const document={body:{classList:{toggle(name){if(classes.has(name)){classes.delete(name);return false;}classes.add(name);return true;}}},getElementById(id){return elements[id]??=new Element(id);},createElement(){return new Element();},createTextNode(text){return {textContent:text};},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}};
const window={ligandViewerReady:true,listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}};
const copy=value=>JSON.parse(JSON.stringify(value));
let liveCamera={eye:{x:0,y:0,z:2},center:{x:0,y:0,z:0},up:{x:0,y:1,z:0}};
const plotElement=document.getElementById('scene');plotElement.layout={scene:{camera:copy(liveCamera)}};
plotElement._fullLayout={scene:{_scene:{getCamera(){return copy(liveCamera);},saveLayout(layout){layout.scene.camera=copy(liveCamera);}}}};
function rendered(gd){active++;maxActive=Math.max(maxActive,active);if(feedback)gd.listeners.plotly_hover?.({points:[{curveNumber:6,customdata:0}]});return new Promise(resolve=>completions.push(()=>{active--;resolve();}));}
const Plotly={restyle(gd,update,index){liveCamera=copy(gd.layout.scene.camera);calls.push({update,index});return rendered(gd);},relayout(gd,update){liveCamera=copy(update['scene.camera']??gd.layout.scene.camera);gd.layout.scene.camera=copy(liveCamera);layouts.push(update);return rendered(gd);},downloadImage(){}};
class ResizeObserver{constructor(callback){this.callback=callback;observers.push(this);}observe(){}}
function requestAnimationFrame(callback){frames.push(callback);return frames.length;}
async function flush(){for(let count=0;count<100;count++){while(frames.length)frames.shift()();while(completions.length)completions.shift()();await Promise.resolve();await Promise.resolve();if(!frames.length&&!completions.length){assert.equal(active,0);return;}}throw new Error('Rendering did not settle');}
for(const [id,value] of Object.entries({'minimum':'0','point-size':'5','cloud-opacity':'.22','protein-size':'8','protein-opacity':'.25'}))document.getElementById(id).value=value;
document.getElementById('layer-space').checked=false;
document.getElementById('lock-clip-plane').checked=false;
'''
        data = dict(layers=dict(space=[0], protein_bonds=[1, 7], protein=[2, 8], ligand_bonds=[3],
                                ligand=[4], sites=[5], rays=[6], backbone=[7, 8], sidechains=[1, 2], environment=[], highlight=[9]),
                    x=[10, 10, 12, 8], y=[-4, -4, -4, -4], z=[7, 8, 8, 9], gaps=[.2, 1, 2.5, .6],
                    total_points=4, center=[10, -4, 8], atoms=[],
                    corridors=[dict(id=0, atom_index=1, atom_name="C1", hydrogen="H1", length=2.5,
                                    length_label="At least 2.50 Å", end_label="No obstacle found within the checked distance; space may continue farther.")],
                    bounds=[[8, -5, 7], [12, -3, 9]])
        stub += 'const fixtureData=' + json.dumps(data) + ';\n'
        script = viewer.TEMPLATE.split("<script>", 1)[1].split("</script>", 1)[0].replace("__DATA__", "fixtureData")
        checks = r'''
assert.equal(calls.length,0,'Initialization must not redraw the completed scene');
assert.equal(layouts.length,0);
assert.deepEqual(document.getElementById('ligand-layers').children.map(row=>row.children[0].id),['layer-ligand','layer-ligand_bonds']);
assert.deepEqual(document.getElementById('protein-layers').children.map(row=>row.children[0].id),['layer-backbone','layer-sidechains']);
assert.equal(document.getElementById('radius-count').textContent,'Free-space points hidden');
document.getElementById('layer-space').checked=true;document.getElementById('layer-space').onchange();await flush();
assert.deepEqual(calls.at(-1).update.z[0],[7,8,8],'Only the camera-facing half must be cut');
assert.match(document.getElementById('radius-count').textContent,/3 visible points.*1 cut away/);
document.getElementById('clip-foreground').checked=false;document.getElementById('clip-foreground').onchange();await flush();
assert.deepEqual(calls.at(-1).update.z[0],[7,8,8,9],'Disabling clipping restores all points');
document.getElementById('clip-foreground').checked=true;document.getElementById('clip-foreground').onchange();await flush();
const beforeRotation=calls.length;
plotElement.listeners.pointerdown();liveCamera.eye={x:0,y:0,z:-2};
for(let i=0;i<200;i++)plotElement.listeners.plotly_relayouting({'scene.camera':liveCamera});await flush();
assert.equal(calls.length,beforeRotation+1,'Camera event bursts must coalesce');
assert.deepEqual(calls.at(-1).update.z[0],[8,8,9],'The cut must rotate before the mouse is released');
assert.deepEqual(liveCamera.eye,{x:0,y:0,z:-2},'Redrawing must retain the live camera');
plotElement.listeners.pointerup();await flush();
assert.equal(calls.length,beforeRotation+1,'An unchanged camera must not redraw');

liveCamera.eye={x:2,y:0,z:0};plotElement.listeners.plotly_relayout({'scene.camera':liveCamera});await flush();
assert.deepEqual(calls.at(-1).update.x[0],[10,10,8],'Rotation around another axis must change the cut');
assert.deepEqual(calls.at(-1).update['marker.color'][0],[.2,1,.6],'Point colors must follow the clipped coordinates');

plotElement.listeners.pointerdown();
document.getElementById('protein-opacity').value='.9';document.getElementById('protein-opacity').listeners.input();
liveCamera.eye={x:-2,y:0,z:0};plotElement.listeners.plotly_relayouting({'scene.camera':liveCamera});await flush();
assert.deepEqual(calls.at(-1).index,[0,9],'The dynamic cut must update while other controls wait for the drag');
assert.deepEqual(calls.at(-1).update.x[0],[10,10,12]);
const rotated=copy(liveCamera);window.listeners.pointerup();await flush();
assert.deepEqual(calls.at(-1),{index:[2,8,1,7],update:{opacity:.9}});
assert.deepEqual(liveCamera,rotated,'Deferred controls must retain the camera');

document.getElementById('layer-space').checked=false;document.getElementById('layer-space').onchange();await flush();
assert.equal(calls.at(-1).update.visible,false);
const hiddenCount=calls.length;liveCamera.eye={x:0,y:0,z:2};plotElement.listeners.plotly_relayouting({});await flush();
assert.equal(calls.length,hiddenCount,'Hidden points must not redraw while rotating');
document.getElementById('layer-space').checked=true;document.getElementById('layer-space').onchange();await flush();
assert.deepEqual(calls.at(-1).update.z[0],[7,8,8]);


// Radius exploration, pinning and minimum-radius filtering share the dynamic cut.
assert.deepEqual(calls.at(-1).update['marker.size'],[5,12.5]);
document.getElementById('radius-bar').listeners.pointermove({clientY:100});await flush();
assert.deepEqual(calls.at(-1).update['marker.color'][1],[1]);
assert.equal(calls.at(-1).update.opacity[0],.08);
assert.match(document.getElementById('radius-count').textContent,/1 highlighted points/);
document.getElementById('radius-bar').listeners.click({clientY:0});await flush();
assert.deepEqual(calls.at(-1).update['marker.color'][1],[2.5],'The 2+ range includes larger radii');
assert.match(document.getElementById('selection-state').textContent,/pinned/);
const pinnedCount=calls.length;document.getElementById('radius-bar').listeners.pointerleave();await flush();
assert.equal(calls.length,pinnedCount,'Pinned ranges survive pointer exit');
liveCamera.eye={x:2,y:0,z:0};plotElement.listeners.plotly_relayouting({});await flush();
assert.deepEqual(calls.at(-1).update.x[1],[],'Highlighted points must obey the moving cut');
liveCamera.eye={x:0,y:0,z:2};plotElement.listeners.plotly_relayout({});await flush();
assert.deepEqual(calls.at(-1).update.x[1],[12]);
document.getElementById('minimum').value='1.1';document.getElementById('minimum').listeners.input();await flush();
assert.deepEqual(calls.at(-1).update.x[0],[12],'Minimum radius filters the complete cloud');
assert.deepEqual(calls.at(-1).update.x[1],[12]);
assert.equal(document.getElementById('minimum-out').textContent,'1.10 Å');
document.getElementById('minimum').value='0';document.getElementById('minimum').listeners.input();await flush();
const key=key=>({key,preventDefault(){}});
document.getElementById('radius-bar').listeners.keydown(key('ArrowDown'));await flush();
assert.equal(document.getElementById('radius-bar')['aria-valuenow'],'1.95');
document.getElementById('radius-bar').listeners.keydown(key('Enter'));await flush();
assert.equal(document.getElementById('selection-state').textContent,'');
document.getElementById('radius-bar').listeners.pointerleave();await flush();
assert.equal(document.getElementById('radius-value').textContent,'All radii');
assert.equal(document.getElementById('radius-band').style.display,'none');
document.getElementById('radius-bar').listeners.keydown(key('ArrowUp'));await flush();
assert.equal(document.getElementById('radius-bar')['aria-valuenow'],'1.05');
assert.deepEqual(calls.at(-1).update['marker.color'][1],[1]);
document.getElementById('radius-bar').listeners.keydown(key(' '));await flush();
assert.equal(document.getElementById('selection-state').textContent,'');
document.getElementById('radius-bar').listeners.pointerleave();await flush();
document.getElementById('radius-bar').listeners.keydown(key('Enter'));await flush();
assert.match(document.getElementById('selection-state').textContent,/pinned/);
document.listeners.keydown(key('Escape'));await flush();
assert.equal(document.getElementById('radius-value').textContent,'All radii');
assert.equal(document.getElementById('selection-state').textContent,'');
const beforeRadiusBurst=calls.length;
for(let i=0;i<200;i++)document.getElementById('radius-bar').listeners.pointermove({clientY:100});await flush();
assert.equal(calls.length,beforeRadiusBurst+1,'Radius-hover bursts must coalesce');
document.getElementById('radius-bar').listeners.pointerleave();await flush();
assert.equal(calls.at(-1).update.x[1].length,0);
assert.equal(calls.at(-1).update.opacity[0],.22,'Leaving the bar restores the original cloud opacity');

document.getElementById('lock-clip-plane').checked=true;document.getElementById('lock-clip-plane').onchange();await flush();
const beforeLockedRotation=calls.length;
liveCamera.eye={x:-2,y:0,z:0};plotElement.listeners.plotly_relayouting({});await flush();
assert.equal(calls.length,beforeLockedRotation,'Locked planes must not follow camera rotation');
document.getElementById('point-size').value='16';document.getElementById('point-size').listeners.input();await flush();
assert.deepEqual(calls.at(-1).update.z[0],[7,8,8],'Style changes must retain the frozen cut');
document.getElementById('clip-foreground').checked=false;document.getElementById('clip-foreground').onchange();await flush();
assert.deepEqual(calls.at(-1).update.z[0],[7,8,8,9]);
document.getElementById('clip-foreground').checked=true;document.getElementById('clip-foreground').onchange();await flush();
assert.deepEqual(calls.at(-1).update.z[0],[7,8,8],'Re-enabling clipping must retain the lock');
document.getElementById('lock-clip-plane').checked=false;document.getElementById('lock-clip-plane').onchange();await flush();
assert.deepEqual(calls.at(-1).update.x[0],[10,10,12],'Unlocking must use the current camera immediately');

const scene=plotElement._fullLayout.scene;
scene.xaxis={range:[0,100]};scene.yaxis={range:[0,10]};scene.zaxis={range:[0,10]};scene.aspectratio={x:1,y:2,z:3};
fixtureData.x=[12,8,10];fixtureData.y=[-4,-4,-4];fixtureData.z=[7,9,8];fixtureData.gaps=[.2,1,2.5];
liveCamera.eye={x:1,y:1,z:1};plotElement.listeners.plotly_relayout({});await flush();
assert.deepEqual(calls.at(-1).update.x[0],[12,10],'Clipping must account for unequal axis ranges and aspect ratios');
liveCamera.center={x:1,y:1,z:2};plotElement.listeners.plotly_relayout({});await flush();
assert.deepEqual(calls.at(-1).update.z[0],[9,8],'Camera target translation must affect the view direction');

document.getElementById('point-size').value='16';document.getElementById('point-size').listeners.input();await flush();
assert.deepEqual(calls.at(-1).update['marker.size'],[16,40]);
document.getElementById('cloud-opacity').value='0';document.getElementById('cloud-opacity').listeners.input();await flush();
assert.equal(calls.at(-1).update.opacity[0],0);
const burstCount=calls.length;
for(let i=0;i<200;i++){document.getElementById('cloud-opacity').value=String(i/199);document.getElementById('cloud-opacity').listeners.input();}
await flush();assert.equal(calls.length,burstCount+1);assert.equal(calls.at(-1).update.opacity[0],1);

document.getElementById('protein-size').value='5';document.getElementById('protein-size').listeners.input();await flush();
assert.deepEqual(calls.at(-1),{index:[2,8,1,7],update:{'marker.size':[5,5*1.15,5,5*1.15],'line.width':[3.5,6.25,3.5,6.25]}});
document.getElementById('layer-backbone').checked=false;document.getElementById('layer-backbone').onchange();await flush();
assert.deepEqual(calls.at(-1),{index:[7,8],update:{visible:false}});
document.getElementById('viewport').clientWidth=1500;observers[0].callback();await flush();
assert.deepEqual(layouts.at(-1),{autosize:false,width:1500,height:900});
const layoutCount=layouts.length;for(let i=0;i<100;i++)observers[0].callback();await flush();assert.equal(layouts.length,layoutCount);
document.getElementById('toggle-controls').onclick();await flush();assert(classes.has('controls-hidden'));

feedback=true;const hoverCount=calls.length;
for(let i=0;i<200;i++)plotElement.listeners.plotly_hover({points:[{curveNumber:6,customdata:0}]});await flush();
assert.equal(calls.length,hoverCount,'Hover must not redraw and dismiss the tooltip');
plotElement.listeners.plotly_click({points:[{curveNumber:6,customdata:0}]});await flush();
assert.equal(document.getElementById('corridor-select').value,'0');
plotElement.listeners.pointerleave();document.getElementById('clear').onclick();await flush();
assert.equal(document.getElementById('corridor-select').value,'');
assert.equal(calls.length,hoverCount);
assert.equal(maxActive,1,'Only one Plotly mutation may run at a time');
console.log('Dynamic clipping, radius exploration and controls passed');
'''
        program = stub + "\n(async()=>{\n" + script + checks + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        result = subprocess.run([shutil.which("node"), "-"], input=program, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_latest_save_uses_client_folder_and_missing_save_opens_picker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "old.ir_solution"; old.touch()
            new = root / "latest.ir_solution"; new.touch()
            os.utime(old, (10, 10)); os.utime(new, (20, 20))
            nested = root / "puzzles"; nested.mkdir()
            (nested / "unrelated.ir_solution").touch()
            feature = viewer.LigandViewer(mock.Mock(), ".", ".")
            with mock.patch.object(feature, "_start_save") as start, mock.patch.object(feature, "open_save") as picker:
                feature.open_latest(root)
                self.assertEqual(start.call_args.args[0], new)
                picker.assert_not_called()
                start.call_args.kwargs["on_no_ligand"]()
                self.assertEqual(picker.call_args.args, (root, None))
            with mock.patch.object(feature, "open_save") as picker:
                feature.open_latest(nested / "missing")
                picker.assert_called_once()

    def test_file_picker_cancel_does_not_start_work(self):
        feature = viewer.LigandViewer(mock.Mock(), ".", ".")
        with mock.patch.object(viewer, "_native_dialog", return_value=""), mock.patch.object(viewer, "ProgressWindow") as worker:
            feature.open_save("current-client")
            worker.assert_not_called()

    def test_map_settings_are_read_fresh_for_each_job(self):
        config = {"quality": "Fine", "reach": 9., "probe_radius": .5}
        feature = viewer.LigandViewer(mock.Mock(), ".", ".", settings_provider=lambda: dict(config))
        with mock.patch.object(viewer, "find_database", return_value="database"), mock.patch.object(viewer, "ProgressWindow") as worker:
            feature._start_save("pose.ir_solution", ".", None)
            self.assertEqual(worker.call_args.args[3], viewer.MapSettings("Fine", 9., .5))
            config["quality"] = "Fast"
            feature._start_save("pose.ir_solution", ".", None)
            self.assertEqual(worker.call_args.args[3].quality, "Fast")

    def test_monitor_context_passes_the_row_folder_to_the_viewer(self):
        source = (Path(__file__).resolve().parents[1] / "modules" / "app.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "set_ligand_viewer_context")
        menu, callback = mock.Mock(), mock.Mock()
        namespace = {"context_menu": menu, "open_ligand_viewer": callback}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "menu", "exec"), namespace)
        namespace["set_ligand_viewer_context"]("clicked-client")
        menu.entryconfigure.call_args.kwargs["command"]()
        callback.assert_called_once_with("clicked-client")
        self.assertIn('add_command(label="LigandViewer"', source)
        self.assertNotIn('label="Map settings…"', source)

    def test_no_ligand_worker_event_runs_fallback_on_the_ui_thread(self):
        job = object.__new__(viewer.ProgressWindow)
        job.owner = mock.Mock(closed=False)
        job.events = __import__("queue").Queue()
        job.cancel = threading.Event()
        job.window = mock.Mock()
        job.on_no_ligand = mock.Mock()
        job.events.put(("no_ligand", "No physical ligand"))
        job.poll()
        job.on_no_ligand.assert_called_once_with()
        self.assertIsNone(job.owner.job)
        job.window.destroy.assert_called_once_with()


class ReportOpeningTests(unittest.TestCase):
    def test_reports_open_in_browser_without_a_ready_dialog(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / f"ligand_viewer_{segment}.html" for segment in (2, 3)]
            for path in paths:
                path.write_text("<html></html>", encoding="utf-8")
            result = dict(folder=folder, reports=[dict(html=str(path)) for path in paths])
            with mock.patch.object(viewer.webbrowser, "open", return_value=True) as browser, \
                    mock.patch.object(viewer, "_native_dialog") as dialog, \
                    mock.patch.object(viewer.tk, "Toplevel") as window:
                viewer.open_reports(mock.Mock(), result)
            self.assertEqual(browser.call_args_list, [mock.call(path.as_uri(), new=2) for path in paths])
            dialog.assert_not_called()
            window.assert_not_called()

    def test_open_failures_show_one_error_and_other_reports_are_still_attempted(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / f"ligand_viewer_{segment}.html" for segment in range(4)]
            for path in paths[1:]:
                path.write_text("<html></html>", encoding="utf-8")
            result = dict(folder=folder, reports=[dict(html=str(path)) for path in paths])
            parent = mock.Mock()
            with mock.patch.object(viewer.webbrowser, "open", side_effect=[False, RuntimeError("Browser failed"), True]) as browser, \
                    mock.patch.object(viewer, "_native_dialog") as dialog:
                viewer.open_reports(parent, result)
            self.assertEqual(browser.call_count, 3)
            dialog.assert_called_once()
            self.assertEqual(dialog.call_args.args, (viewer.messagebox.showerror, parent))
            message = dialog.call_args.kwargs["message"]
            self.assertIn(str(paths[0]), message)
            self.assertIn(str(paths[1]), message)
            self.assertIn("Browser failed", message)
            self.assertIn(folder, message)

    @staticmethod
    def job(event):
        job = object.__new__(viewer.ProgressWindow)
        job.owner = mock.Mock(closed=False)
        job.events = __import__("queue").Queue()
        job.cancel = threading.Event()
        job.window = mock.Mock()
        job.events.put(event)
        return job

    def test_completed_worker_closes_progress_and_opens_report_automatically(self):
        result = dict(folder="reports", reports=[dict(html="report.html")])
        job = self.job(("ready", result))
        with mock.patch.object(viewer, "open_reports") as open_reports:
            job.poll()
        job.window.destroy.assert_called_once_with()
        self.assertIsNone(job.owner.job)
        open_reports.assert_called_once_with(job.owner.root, result)

    def test_generation_failure_shows_error(self):
        job = self.job(("error", "Generation failed"))
        with mock.patch.object(viewer, "open_reports") as open_reports, \
                mock.patch.object(viewer, "_native_dialog") as dialog:
            job.poll()
        open_reports.assert_not_called()
        dialog.assert_called_once_with(viewer.messagebox.showerror, job.owner.root,
                                       title="Ligand viewer", message="Generation failed")

    def test_cancelled_generation_does_not_open_report_or_show_error(self):
        job = self.job(("ready", dict(folder="reports", reports=[])))
        job.cancel.set()
        with mock.patch.object(viewer, "open_reports") as open_reports, \
                mock.patch.object(viewer, "_native_dialog") as dialog:
            job.poll()
        open_reports.assert_not_called()
        dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
