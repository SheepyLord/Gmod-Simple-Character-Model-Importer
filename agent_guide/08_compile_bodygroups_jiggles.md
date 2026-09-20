# 08 — Compile: bodygroups, jigglebones, hitboxes, physics and packaging (Step 14)

Step 14 turns the Step 9 export into the shipped addon: it writes the QC, runs studiomdl (several
passes), composes the addon folder (models, materials, lua, vgui icons, addon.json), packs the
`.gma` and optionally installs it into the game. Everything is driven by
`14_sort_qc_compile/qc_plan.json`.

`core.analyze_qc(final_dir, author=, character_category=, model_name=, gmod_root=, studiomdl_path=,
gender=, game=, survivor=)` → `qc_analysis.json` + `qc_plan.json`;
`core.compile_and_compose_qc(final_dir, plan)` → `qc_report.json`, `qc_files.json`, addon folder + `.gma`.

## The plan

Top-level keys you set (see [01](01_naming_and_metadata.md) for the naming ones): `author`,
`character_category`, `category_readable`, `model_name`, `display_name`, `gender`
(`female`/`male` → which GMod animation set is included), `game`, `survivor` (L4D2 slot),
`nodecal` (block decals; default on for L4D2), `merge_identical_textures` (dedupe VTFs by
content), `copy_to_gmod_addons`, `distribution_output_dir`, `gmod_experimental_arms`,
`sfm_static_bones`, `invert_jiggle_direction`, `texturegroups` (skins), and the three row tables below.

### `bodygroups` rows

```json
{"uid": "bg_004", "name": "Clothes", "group": "Clothes", "smd": "Clothes.smd", "has_flex": false, "can_hide": true}
```

One row per exported SMD (from Step 6). `can_hide: true` emits the `blank` option so players can
toggle the part; it is forced **false** for `Body`/`Hair`/`Face`/`Legs`/`Arms` and for any group with
`has_flex` (a blanked flex mesh breaks the face poser). `bodygroups_default` keeps the tool's
proposal for comparison. Order rows the way you want them listed in the spawn menu (Face, Body,
Hair, then clothing top-to-bottom, then accessories).

### `rows` — jigglebones

One row per bone in the skeleton:

```json
{"uid": "bone_057", "bone": "SweetChild_0_4", "parent": "ValveBiped.Bip01_Pelvis",
 "essential": false, "nearest_essential": "ValveBiped.Bip01_Pelvis", "nearest_essential_distance": 6.1,
 "region": "lower_left", "confidence": 0.9, "reason": "skirt chain root",
 "jiggle_type": "Directional Jiggle",
 "jiggle_params": {"length": 10.0, "tip_mass": 100.0, "pitch_stiffness": 25.0, "pitch_damping": 2.0,
                   "yaw_stiffness": 25.0, "yaw_damping": 2.0, "along_stiffness": 50.0, "along_damping": 1.0,
                   "angle_constraint": 90.0},
 "pitch_constraint": [-2, 45], "yaw_constraint": [-2, 2],
 "weighted_vertices": 812, "direct_children": 1, "warnings": []}
```

`jiggle_type` is one of:

| Type | Use for | Behaviour (QC block) |
| --- | --- | --- |
| `Not Jiggle` | every ValveBiped bone, twist helpers, eyes, facial-flex bones, bones with no vertices | no `$jigglebone` |
| `Directional Jiggle` | hair strands, skirt/dress columns, capes, tails, ribbons | `is_flexible` with `pitch_constraint`/`yaw_constraint` opened toward the direction the part hangs away from the body (≈45° open side, ±2° closed side), `angle_constraint 90`, `tip_mass 100`, stiffness 25, damping 2 |
| `Spring Jiggle` | breasts, buttocks (`chest`/`butt`/`breast` in the name) | `is_flexible` + `has_base_spring` (base mass 15, stiffness 50, damping 6, small left/up/forward constraints); `tip_mass 30`, `angle_constraint` ~9–14 |
| `Omni Jiggle` | small decorations: ear rings, clothes-on-breast bones, sleeves, hair ornaments, epaulettes | light `is_flexible` with `tip_mass 0`, stiffness ~10, `angle_constraint` 30, symmetric constraints (±25 pitch / ±10 yaw) |

Exact default numbers live in `default_jiggle_params()` in `tools/sort_qc_compile.py`; the human
corpus values are the same (see the `$jigglebone` blocks in
[`resources/examples/reference_compile_durin.qc`](resources/examples/reference_compile_durin.qc)).

Direction rule for `Directional Jiggle` (what the tool's `region` classification and the human
notebook's `bone_orientation_determiner` both do): decide by the bone's position relative to the
**pelvis** (bones below `Spine4` = skirt/lower body) or the **neck** (bones above = hair):

| Position | pitch (min, max) | yaw (min, max) |
| --- | --- | --- |
| front | (−2, 45) | (−2, 2) |
| back | (−45, 2) | (−2, 2) |
| left side | (−2, 2) | (−45, 2) |
| right side | (−2, 2) | (−2, 45) |
| front-left / front-right (hair) | (−2, 45) | (−2, 45) / (−45, 2) |
| back-left / back-right (hair) | (−45, 2) | (−2, 45) / (−45, 2) |
| unclear | (−10, 10) | (−10, 10) |

The tool's `region` field records where it placed each bone: `head_`, `upper_`, `torso_` or
`lower_` (height band) × `left`, `right`, `center` — front/back is resolved from the bone's
position when the constraints are computed, so check `pitch_constraint`/`yaw_constraint` rather
than the region name when reviewing a row.

If the whole skeleton's jiggles swing the wrong way in game (hair flies forward when walking), the
source model faced +Y instead of −Y — set `"invert_jiggle_direction": true` rather than editing
every row.

Review rules for the table:

- Every kept non-essential bone from Step 4 should be a jiggle of some type, **and every link of a
  chain must be the same type** (a `Not Jiggle` link in the middle of a hair chain freezes the tip).
- Bones that also received a **physics body** in Step 8 stay jiggles (jiggles animate the live
  model; the ragdoll takes over on death).
- Roots that attach to a moving parent (skirt roots on the pelvis) are fine as `Directional`;
  do **not** set `Spring` on anything but breast/butt bones (the base spring lets the bone
  translate, which looks wrong on cloth).
- Facial bones (tongue, eyelids, jaw helpers) must be `Not Jiggle`; the tool forces this when it
  detects flex bones (`is_facial_flex_bone`), but check imported rigs with unusual names.
- `length` is in Source units from the bone head to the simulated tip; long hair strands can use
  15–20, skirt links 8–10, small charms 5.

### `physics_rows` + `physics_globals`

Mirror `8_sort_collision/physics_settings.json` (see [05](05_physics_ragdoll.md)); rows have
`bone`, `enabled`, `has_rot_damping`, `rot_damping`, `constraints{x,y,z:{min,max,friction}}` and
mass bias. Leave the 18 standard rows alone; extra rows appear for additional collision groups
with their preset constraints. `physics_collision_text_lines` is the `$collisiontext` block.

## What the compile does (and where to look)

Inside `compile_and_compose_qc` (all under `14_sort_qc_compile/`):

1. `0_qc_source/` — copies of the Step 9 SMD/VTA/anims plus the generated `compile*.qc`. For
   L4D2 the proportion delta is made position-only here.
2. `compile_definebones.log` — `studiomdl -definebones` pass captures the bind pose; missing
   parent bones are repaired automatically (`missing_definebones_repair.json`). L4D1-slot survivors
   get the TLS `L_weapon_bone` injected.
3. `compile_hbox_probe.log` — `studiomdl -h` pass; the tool keeps the 20 ValveBiped hitboxes
   and assigns HL2 hit groups: 1 head, 2 chest (`Spine1`, `Spine4`), 3 stomach (`Pelvis`, `Spine`),
   4 left arm, 5 right arm, 6 left leg, 7 right leg, 8 neck.
4. `compile_main.log` (+ `compile_pm.log` for the GMod playermodel, `compile_carms.log` for the
   first-person arms) — the real compiles. Read these first when anything is off.
5. Addon composition: `models/…`, `materials/models/<author>/<model>/*.vtf|vmt`, shared
   textures, `materials/vgui/entities/*`, `lua/autorun/<model>_<author>.lua`, `addon.json`,
   `<model>_<author>.gma`; L4D2 gets a VPK + `addoninfo.txt` (+ `_light` model copies for L4D1
   slots); SFM gets loose models/materials only.
6. `qc_report.json` — `status`, `validation{ok, errors, warnings}`, `studiomdl_logs`,
   `definebone_count`, `hbox_count`, `jiggle_count`, `compiled_stems`, `addon_dir`,
   `distribution_gma`, and game-specific keys (`l4d2_proportion_rotation_delta`,
   `l4d2_tls_weapon_bones_injected`, `l4d2_light_models`).

The human-made QC structure the tool reproduces — `$modelname`, `$model "Face"` with
`flexfile`/`flexcontroller`/`%name = name`, `$bodygroup` blocks (blank or not), `$surfaceprop flesh`,
`$definebone`s, `$hbox`es, `$jigglebone`s, `$ikchain`s + `$ikautoplaylock`, `reference` + proportion
delta sequences, `$includemodel` animation set, `$collisionjoints` — is in
[`resources/examples/reference_compile_durin.qc`](resources/examples/reference_compile_durin.qc).
Compare a generated `0_qc_source/compile.qc` against it when something looks structurally wrong.

## Game-specific notes

- **GMod**: two models are built — `<model>.mdl` (NPC/ragdoll, `$includemodel` female/male
  shared + ss + gestures + postures) and `<model>_pm.mdl` (playermodel with the player animation
  set) — plus `<model>_arms.mdl` c_arms. `gender` selects the animation set; use `male` for male
  characters or the walk cycles look wrong.
- **L4D2**: 126-bone budget, 2048 textures, 52 FACS flexes, survivor slot chooses the animation
  model and file names; the compile emits survivor attachments/IK chains and `$declaresequence`
  ordering so multiplayer animation indices match; no `$origin` drop; TLS weapon bones injected
  for L4D1 slots. Test both the survivor and the `_light` NPC version (The Passing) for those.
- **SFM**: one static idle, no physics/collision, `$maxverts 65530`; `sfm_static_bones` disables
  jigglebones and keeps natural bone orientations when the model is meant to be posed by hand.

## Packaging and publishing

- `copy_to_gmod_addons: true` installs the addon folder into `garrysmod/addons/` for testing.
- `distribution_output_dir` receives the `.gma` + `qc_compile_source/` (the reproducible QC source
  the corpus keeps next to every release).
- Before `gmpublish`: fix `addon.json` title, prepare the 512² JPEG icon
  ([07](07_icons_and_workshop_art.md)), and run the in-game checklist in
  [09](09_debugging_and_testing.md).
