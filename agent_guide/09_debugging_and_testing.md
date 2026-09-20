# 09 — Debugging and testing a port

## Where the evidence is

| Step | Log | Report / plan |
| --- | --- | --- |
| 1 | `1_import_mmd_model/blender_import.log` | `pmx_preflight_report.json` (vertex count, morph warnings, missing textures), `blender_import_report.json` |
| 2 | `2_fix_model_source_skeleton/blender_fix_model.log` | `blender_fix_model_report.json` |
| 3–8 | `<step>/blender_<step>.log` | `<step>_analysis.json`, `<step>_plan.json`, `blender_<step>_report.json` |
| 9 | `9_export_proportion_trick/blender_export_proportion_trick.log` | `proportion_export_report.json` (`armature_preparation`, `zero_weight_bone_cleanup`, coincident-vertex fixes, bone-limit merges) |
| 10–13 | `<step>/*.log` | `*_report.json`, `*_files.json` |
| 14 | `14_sort_qc_compile/compile_definebones.log`, `compile_hbox_probe.log`, `compile_main.log`, `compile_pm.log`, `compile_carms.log`, `gmad_create.log` | `qc_report.json`, `qc_files.json`, `missing_definebones_repair.json` |

Every report has `validation.ok`, `errors`, `warnings`. Treat `ok == false` as a stop. Warnings
that mention a specific material/bone/bodygroup name are worth fixing before release.

## Error catalogue (symptom → cause → fix)

| Symptom | Cause | Fix |
| --- | --- | --- |
| Step 1 fails on a path with CJK characters / very long path | Windows MAX_PATH (260) in Blender/Source Tools | keep the workspace root short (`workspace_root=Path("C:/w")`); the tool already stages ASCII names for VTFCmd/gmad |
| Step 1 preflight warns "too many vertices" (>128k) | model over the practical limit | decimate in Blender before importing, or accept the RTX/vertex-limit split in Step 6 |
| Step 6: "over the vertex limit" for one material | single material > 65535 (32767 RTX) verts | split by material/UV island in a `manual_edit_blend`, or decimate that part |
| Step 7: most facial morphs untranslated (`morph_###`) | morph names not in `flex_name_dictionary.json` (new naming scheme / CJK) | add mappings to the dictionary, re-run analyze |
| Step 8: `validation.ok == false`, "hull spans", coverage < 0.5 | wrong `source_bodygroups` (clothes fed the hull) or a bone without mesh | choose body-only sources; use the template body for empty bones ([05](05_physics_ragdoll.md)) |
| Step 9: report lists many "coincident vertices … separated" | duplicated verts in the base mesh with different flex motion (mouth interiors) | normal; the tool separates them by ≥3e-3 units so studiomdl won't weld them. If lips still tear in game, the model has genuinely overlapping shells — merge doubles in a manual edit |
| Step 12: `placeholder_materials` non-empty | base texture missing/unreadable (wrong path, unsupported format) | point `materials.json` `base_color_path` at the real file (DDS/PNG/TGA/BMP/JPG are fine), re-run |
| Step 13: `VTFCmd.exe was not found` | bundled tool missing/blocked | ensure `external_tools/vtfcmd/VTFCmd.exe` exists; set `VTFCMD` env var |
| Step 14: `No studiomdl.exe is set for <game>` / `PermissionError WinError 5` | blank/invalid game paths (tool not detected) | set the game folder or `STUDIOMDL`; on Linux install the game's Windows depot |
| Step 14: `too many bones` / `bone count exceeds` | Step 4 plan over the limit | disable more merges' `enabled:false` → fewer kept bones |
| Step 14: `too many verts` (> 65535) | a bodygroup SMD over the limit | Step 6 auto-split (`always_auto_split=True`) or reduce |
| Step 14: `procedural bone ""` / studiomdl crash during VRD | VRD trigger budget (>64 per helper / ~400 total) | disable VRD rows ([06](06_vrd_procedural_bones.md)) |
| Step 14: `missing parent definebone` warnings | a kept bone whose parent was merged | automatic repair inserts the parent; if `unrecoverable_missing_parents` is non-empty, fix Step 4 |
| Step 14: `Texture processing completed with N blocking error(s)` | Step 12 errors carried into compile | fix Step 12 first |
| In game: model floats / sinks | pelvis hull wrong or skirt in physics sources | Step 8 sources; hitbox/physics in HLMV |
| In game: arms cross / drift (L4D2) | proportion delta carried rotations (fixed in 0.10.21) | update the tool; re-run Step 14 |
| In game: legs bent (L4D2) / melee weapon on the floor | `$origin` sink / missing TLS `L_weapon_bone` (fixed) | re-run Step 14 with a current tool |
| In game: blocky "cubes" on dark glossy materials | alpha path on opaque materials (chromium x64 branch) | current tool drops `$alphatest` for opaque bases; re-run Steps 12+14 |
| In game: hair stiff / jiggle explodes | chain thinned in the middle / jiggle on a vertex-less bone | Step 4 alternation; `Not Jiggle` for the bone |
| In game: skirt clips through thighs when crouching | no VRD helpers on front panels | enable VRD rows for the front/side skirt roots |
| In game: NPC icon black / model missing in spawn menu | `icon_basename` ≠ `model_name`, or lua paths mismatch | keep names consistent; check `lua/autorun/*.lua` paths |
| Previewer window blank / GLError 1282 | GPU driver / software GL | the app falls back to CPU preview; not a port problem |
| Linux: `gameinfo.txt is missing` with `Z:\…\0_qc_source\home\…` | old tool passing POSIX paths to Wine | current tool translates paths; install `wine` |

## Testing outside the game (fast loop)

- **HLMV** (`GarrysMod/bin/hlmv.exe`, open the compiled `.mdl` from `garrysmod/models/…`): check
  sequences play (idle, walk, run), flexes exist in the Flex tab, **Hitboxes** (each box hugs its
  limb, head box contains the head), **Physics Model** (18 hulls + extras), bone count, and the
  **Bodygroups** dropdown lists every group with a `blank` option where expected.
- **Crowbar** decompile of the compiled model vs. `0_qc_source/compile.qc`: bone list, jiggle
  blocks and `$collisionjoints` should match; compare with
  [`resources/examples/reference_compile_durin.qc`](resources/examples/reference_compile_durin.qc)
  for structure.
- **Blender probes** on the Step 9 export: import `2_proportion_export/anims/proportions.smd` with
  Source Tools and check the pose is the model's natural stance (feet on the ground plane, arms
  hanging); import `Physics.smd` and check hulls sit inside limbs.
- **Numeric sanity** from the reports: `definebone_count` = kept bones ≤ limit; `hbox_count` = 20;
  `jiggle_count` = number of non-essential bones you kept (minus `Not Jiggle`); VTF count = kept
  materials (+ `_n`/`_exp` when enabled).

## In-game checklist (required before release)

Launch Garry's Mod on `gm_flatgrass` with the addon installed (`copy_to_gmod_addons`) or the `.gma`
mounted. Verify, in this order:

1. **Spawn menu** → NPCs → `<Category readable>`: both `<Name> (Friendly)` and `(Enemy)` appear
   with icons. Spawn each; they play idle/walk, shoot, and die into a ragdoll.
2. **Ragdoll** (physgun): limbs bend only within joint limits, nothing stretches or vibrates, the
   body settles on the ground; hair/skirt bodies drape.
3. **Playermodel**: select in the C-menu, check third person (`thirdperson` in console) for
   proportions, and first person for c_arms (hands/sleeves match the body textures). Walk, sprint,
   crouch, jump, sit in a jeep — no leg/arm folding.
4. **Face poser**: every flex moves the intended region, eyes track with `eyes_updown/rightleft`,
   `blink` closes both eyes.
5. **Bodygroups**: toggle each in the context menu; hidden clothes reveal a complete body (or the
   group is non-hideable by design).
6. **Jigglebones**: turn with the mouse; hair/skirt follows with a delay and settles; nothing
   pierces the body at rest.
7. **Hitboxes**: `sv_showhitboxes 1` (or shoot the head for a headshot damage multiplier).
8. **Materials**: no purple/black checkerboards, no fully white parts (missing lightwarp),
   eyelashes/hair edges clean (alpha), decals blocked where `nodecal`.
9. **Icons** in the Q menu and on the Workshop preview; `addon.json` title readable.

For L4D2 also load a campaign map as the survivor (walk, crouch, dual pistols, two-handed melee)
and, for L4D1 slots, The Passing/Sacrifice cameos (`_light` models). For SFM open the model in the
Animation Set Editor and drag bones.

## When a fix belongs in the tool

Only when the same defect reproduces on unrelated models and the corpus shows the intended
behaviour. Then: reproduce headlessly with a probe, write the fix gated so default output stays
byte-identical for unaffected games/paths, verify with a before/after diff of the generated QC or
SMD (the repo's issue history — #131, #139, #168, #169 — shows this pattern), and note it in the
step guide.
