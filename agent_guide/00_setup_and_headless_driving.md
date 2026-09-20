# 00 — Setup and driving the pipeline headlessly

## Environment

| Need | Windows (primary) | Linux |
| --- | --- | --- |
| Python | 3.12 x64 (3.11+ works) | 3.11+ |
| Runtime deps | `python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt` | `bash run_linux.sh` once (creates `.venv`, installs, launches GUI; close it) |
| Blender 4.5.10 + add-ons | managed automatically under `%LOCALAPPDATA%\MMDCharacterImporter\software\blender\` | `~/.MMDCharacterImporter/software/blender/` (Linux tarball) |
| StudioMDL / gmad / VTFCmd | from the game install (`GarrysMod\bin\studiomdl.exe`, `gmad.exe`); VTFCmd bundled in `external_tools/` | same files, run through Wine automatically (`Z:\` path translation); install the game's **Windows** depot via Proton |
| Game for testing | Garry's Mod (and L4D2 / SFM for those targets) | same |

Provision Blender before the first run (one-off, ~380 MB download):

```bash
.venv\Scripts\python tools\mmd_character_importer_core.py setup
```

Game tooling is auto-detected from Steam libraries; override with the `STUDIOMDL` environment
variable (path to `studiomdl.exe`) or pass `studiomdl_path=` to `analyze_qc`. Step 14 validates the
paths and names the game it looked for when they are blank.

## The workspace

Each model gets `<workspaces>/<ModelName>_<hash>/` with one folder per step:

```
0_source_mmd_assets/        copy of the PMX/VRM folder + textures
1_import_mmd_model/          *_import.blend, pmx_preflight_report.json, blender_import_report.json
2_fix_model_source_skeleton/ *_fixed.blend (CATS fix + ValveBiped rename)
3_fix_spine_bones/           spine_analysis.json, spine_fix_plan.json, *_spine_fixed.blend
4_sort_bones/                bone_merge_analysis.json, bone_merge_plan.json, *_bones_sorted.blend
5_sort_materials/            material_scan.json, material_plan.json, materials.json, *_materials_sorted.blend
6_sort_bodygroups/           bodygroup_analysis.json, bodygroup_plan.json, *_bodygroups_sorted.blend
7_sort_flexes/               flex_analysis.json, flex_plan.json, flexes.json, *_flexes_sorted.blend
8_sort_collision/            collision_analysis.json, collision_plan.json, physics_settings.json, Physics.smd, *_collision_sorted.blend
9_export_proportion_trick/   0_pre_proportion_raw_export/, 1_proportion_workspace/, 2_proportion_export/ (SMD/VTA + anims/)
10_sort_c_arms/              first-person arm meshes + anims/
11_sort_vrd/                 vrd_analysis.json, vrd_plan.json, vrd_preview.json, vrd.vrd
12_param_texture_render_materials/ textures_plan.json, png/, normal/, textures_manifest.json
13_sort_icons_and_arts/      icons_plan.json, release_icon.png, SPIC.png, E/F.png|jpg|vtf|vmt
14_sort_qc_compile/          qc_plan.json, 0_qc_source/ (SMDs + compile*.qc), compile logs, qc_report.json, <addon folder>, .gma
15_sort_release_description/ description translations
```

Every step folder also has `step_complete.json` (inputs/outputs/report path/validation) and a
`blender_*.log` or `*.log` with the full Blender/studiomdl console.

## The working loop

Each step has an **analyze** half that writes an `*_analysis.json` (everything the tool measured)
and a `*_plan.json` (the decisions it proposes), and an **apply/sort** half that executes a plan.
Your leverage as an agent is between the two:

```
analyze  ->  read analysis + plan  ->  edit plan JSON (or dict)  ->  apply  ->  read report  ->  loop or continue
```

Apply functions accept either the plan dict or the path to a plan file. Analyze never modifies the
input blend; apply writes a new blend in the step folder, so any step can be redone from its
predecessor's output as often as needed.

## Complete driver (Steps 1–15, GMod, mirrors the GUI's one-click port)

```python
import sys
from pathlib import Path

sys.path.insert(0, r"<repo>\tools")
import mmd_character_importer_core as core

log = lambda m: print(m, flush=True)
PMX = Path(r"<model>.pmx"); SRC = PMX.parent
GAME, GENDER = "gmod", "female"        # "l4d2" / "sfm" ; gender picks the GMod animation set
AUTHOR, CATEGORY, MODEL = "yourname", "genshin_impact", "hu_tao"
CATEGORY_READABLE, DISPLAY = "Genshin Impact", "Hu Tao"

# 1 import (PMX or VRM). Keep the workspace root short on Windows (MAX_PATH).
imp = core.import_pmx_to_blender(PMX, SRC, progress=log, workspace_root=None)
ws = imp.workspace                                  # ws.root, ws.blend_path, ws.import_report_path

# 2 fix (CATS fix model + ValveBiped skeleton). clear_custom_normals=False keeps game-rip normals.
fix = core.fix_imported_blend(ws.blend_path, clear_custom_normals=False, progress=log)

# 3 spine
sp_an = core.analyze_spine_blend(fix.output_blend, progress=log)
sp = core.fix_spine_blend(fix.output_blend, sp_an.plan, progress=log)

# 4 bones  -> see 02_bone_merging.md ; edit sb_an.plan["operations"][i]["enabled"] before applying
sb_an = core.analyze_sort_bones_blend(sp.output_blend, progress=log, game=GAME)
sb = core.sort_bones_blend(sp.output_blend, sb_an.plan, progress=log, game=GAME)

# 5 materials -> 03_materials_and_textures.md ; edit keep / combine_target_uid / proposed_name
ms = core.scan_materials_blend(sb.output_blend, progress=log)
ma = core.apply_materials_initial_blend(sb.output_blend, ms.plan, progress=log)
mat_blend = ma.output_blend
if len(ma.report.get("after", {}).get("materials", [])) > 32:
    mm = core.merge_materials_blend(ma.output_blend, ma.merge_plan, progress=log); mat_blend = mm.output_blend

# 6 bodygroups -> 04_bodygroups_layers_and_flexes.md ; regroup source_uids / rename before applying
bg_an = core.analyze_bodygroups_blend(mat_blend, scale_factor=core.DEFAULT_BODYGROUP_SCALE_FACTOR,
                                      scale_preset="factor", progress=log, vertex_limit=core.DEFAULT_BODYGROUP_VERTEX_LIMIT)
bg = core.sort_bodygroups_blend(mat_blend, bg_an.plan, scale_factor=core.DEFAULT_BODYGROUP_SCALE_FACTOR,
                                scale_preset="factor", progress=log, vertex_limit=core.DEFAULT_BODYGROUP_VERTEX_LIMIT)

# 7 flexes
fl_an = core.analyze_flexes_blend(bg.output_blend, progress=log, game=GAME)
fl = core.sort_flexes_blend(bg.output_blend, fl_an.plan, progress=log)

# 8 collision -> 05_physics_ragdoll.md ; source_bodygroups / additional_bone_groups / quality_preset
co_an = core.analyze_collision_blend(fl.output_blend, source_bodygroups=None, additional_bone_groups=None,
                                     quality_preset="fast_preview", progress=log)
co = core.sort_collision_blend(fl.output_blend, co_an.plan, progress=log)   # SFM skips this step
step9_input = co.output_blend

# 9 raw export + proportion trick (SMD/VTA/anims). L4D2 needs survivor=<slot>.
prop = core.run_proportion_export(step9_input, progress=log, game=GAME, survivor=core.DEFAULT_L4D2_SURVIVOR)
final_dir = prop.final_dir                          # 9_export_proportion_trick/2_proportion_export

# 10 c_arms (GMod/L4D2 first-person arms)
carms = core.run_carms_sort(final_dir, progress=log, game=GAME)

# 11 VRD -> 06_vrd_procedural_bones.md ; edit rows[i]["enabled"], intensity_multipliers
vrd_an = core.analyze_vrd(final_dir, progress=log)
vrd = core.apply_vrd(final_dir, vrd_an.plan, progress=log)                  # optional; SFM: off by default

# 12 textures
tx_an = core.analyze_textures(ma.materials_json_path, progress=log, game=GAME, max_texture_edge=0)
tx = core.process_textures(ma.materials_json_path, tx_an.plan, progress=log, game=GAME, max_texture_edge=0)

# 13 icons -> 07_icons_and_workshop_art.md ; custom VMD/frame/source image are keyword args
icons = core.run_icons(ws.root, icon_basename=MODEL, progress=log)

# 14 QC + compile + addon -> 08_compile_bodygroups_jiggles.md
qc_an = core.analyze_qc(final_dir, author=AUTHOR, character_category=CATEGORY, model_name=MODEL,
                        gmod_root="", studiomdl_path="", progress=log, gender=GENDER, game=GAME)
plan = qc_an.plan
plan.update({"author": AUTHOR, "gender": GENDER, "game": GAME, "auto_porting": True,
             "character_category": CATEGORY, "model_name": MODEL,
             "category_readable": CATEGORY_READABLE, "display_name": DISPLAY,
             "copy_to_gmod_addons": True, "nodecal": False, "merge_identical_textures": True})
# edit plan["bodygroups"][i]["can_hide"], plan["rows"][i]["jiggle_type"] etc. here
qc = core.compile_and_compose_qc(final_dir, plan, progress=log)
assert qc.report.get("validation", {}).get("ok") is not False, qc.report.get("validation")

# 15 release description (offline unless you pass API keys)
rel_an = core.analyze_release_description(qc.qc_dir, character_name=DISPLAY, work_title=CATEGORY_READABLE,
                                          author=AUTHOR, model_creator="<from the PMX readme>")
rel = core.generate_release_description(qc.qc_dir, rel_an.plan, progress=log)
```

Timing on a typical workstation: Steps 1–2 take 3–8 minutes (CATS fix), 3–8 a minute or two each,
Step 9 5–10 minutes, Steps 12–14 a few minutes. Run long steps in the background and poll the log.

Result objects expose `.report` (dict), `.report_path`, the output blend/dir, and step-specific
extras (`ma.materials_json_path`, `prop.final_dir`, `qc.qc_dir`, `qc.files_path`). Always assert on
`report["validation"]["ok"]` / `report["errors"]` before moving on; warnings are advisory but read
them.

## Inspecting any intermediate blend

Run the managed Blender headlessly with a probe script; this is how you verify a decision without
the GUI (bone chains, vertex counts, UV/alpha stats, which object owns a material, shape keys):

```bash
"%LOCALAPPDATA%\MMDCharacterImporter\software\blender\4.5.10\blender-4.5.10-windows-x64\blender.exe" --background <step>.blend --python probe.py -- out.json
```

```python
# probe.py — everything after "--" is yours
import bpy, json, sys
out = sys.argv[sys.argv.index("--") + 1]
data = {o.name: {"type": o.type, "verts": len(o.data.vertices) if o.type == "MESH" else None,
                 "materials": [s.material.name for s in o.material_slots if s.material] if o.type == "MESH" else None,
                 "shape_keys": len(o.data.shape_keys.key_blocks) if o.type == "MESH" and o.data.shape_keys else 0}
        for o in bpy.data.objects}
json.dump(data, open(out, "w"), indent=1)
```

`core.setup_state_is_current(core.read_setup_state())` returns the managed Blender path
programmatically. Blender Source Tools, mmd_tools and CATS are installed in that Blender, so
`bpy.ops.import_scene.smd` / `mmd_tools` operators are available in probes.

## GUI parity notes

- The one-click **Auto-port** in the GUI is exactly the driver above (`FullImportWorker` in
  `tools/mmd_character_importer_gui.py`); every checkbox on the main screen is a plan key or a
  keyword argument shown in the driver (`nodecal`, `merge_identical_textures`, `sfm_static_bones`,
  `natural_bone_orientation`, `generate_vrd`, `clear_custom_normals`, RTX vertex limit 32767 via
  `vertex_limit`).
- The manual tabs are the same analyze/apply pairs with a table editor over the plan JSON — when a
  guide says "edit the plan", the GUI user would be editing that table.
- Auto-port never enables the Step 12 PBR schemes or normal generation; those are manual-run
  options (`scheme=`, `normal_action` in the plan).
