# 05 — Accurate physics: the 18-body ragdoll, extra cloth bodies, and template fitting (Step 8)

A Source ragdoll is a set of convex hulls (`Physics.smd`, one hull per physics bone) joined by
constrained joints (`$collisionjoints` in the QC). Get the hulls wrong and the ragdoll jitters,
limbs stretch, or the model falls through the floor; get the joints wrong and it folds like paper.
The corpus standard — used by every released port and built into the tool's defaults — is an
**18-body ragdoll** plus optional extra bodies for hair/skirt/tails.

## The 18 bodies

```
ValveBiped.Bip01_Pelvis        ValveBiped.Bip01_Head1
ValveBiped.Bip01_Spine1        ValveBiped.Bip01_Spine4
ValveBiped.Bip01_L_Clavicle    ValveBiped.Bip01_R_Clavicle
ValveBiped.Bip01_L_UpperArm    ValveBiped.Bip01_R_UpperArm
ValveBiped.Bip01_L_Forearm     ValveBiped.Bip01_R_Forearm
ValveBiped.Bip01_L_Hand        ValveBiped.Bip01_R_Hand
ValveBiped.Bip01_L_Thigh       ValveBiped.Bip01_R_Thigh
ValveBiped.Bip01_L_Calf        ValveBiped.Bip01_R_Calf
ValveBiped.Bip01_L_Foot        ValveBiped.Bip01_R_Foot
```

(`TARGET_BONES` in `tools/blender_sort_collision.py`, `target_bones` in `8_sort_collision/physics_settings.json`.)
`Spine`/`Spine2`/`Neck1`/toes/fingers get **no** body: their mesh is covered by the neighbours, and
more bodies only add joint instability. The ragdoll part budget is **32** (`MAX_TOTAL_COLLISION_PARTS`),
so up to 14 extra bodies remain for cloth/hair.

Joint parameters (identical in the tool's `physics_settings.json` defaults and the human QCs):

```
$collisionjoints "Physics.smd" {
    $mass 48   $inertia 12   $damping 0.8   $rotdamping 4   $rootbone "ValveBiped.Bip01_Pelvis"
    $jointrotdamping "…Pelvis" 3
    $jointmassbias "…Spine1" 8    $jointrotdamping 5   x -10 10 | y -16 16 | z -19 19
    $jointmassbias "…Spine4" 9    $jointrotdamping 5   x -10 10 | y -10 10 | z -20 20
    $jointmassbias "…Clavicle" 4  $jointrotdamping 6   x -10 10 | y -5 5   | z 0 15
    $jointmassbias "…UpperArm" 5                       x -15 20 | y -40 32 | z -80 25
    $jointmassbias "…Forearm" 4   $jointrotdamping 4   x -40 15 | y 0 0    | z -120 10
    $jointrotdamping "…Hand" 1                         x -25 25 | y -35 35 | z -50 50
    $jointmassbias "…Head1" 4     $jointrotdamping 3   x -50 50 | y -20 20 | z -26 30
    $jointmassbias "…Thigh" 7     $jointrotdamping 7   x -30 30 | y -60 30 (R) / -30 60 (L) | z -100 30
    $jointmassbias "…Calf" 4      $jointrotdamping 5   x -15 15 | y -5 5   | z -10 125
    $jointrotdamping "…Foot" 9                         x -15 15 | y -15 15 | z -18 25
}
$collisiontext { animatedfriction { min 80 max 600 timein 0.15 timeout 0.25 timehold 1.75 }
                 editparams { rootname "valvebiped.bip01_pelvis" totalmass 50 } }
```

The complete block as compiled by a human port is in
[`resources/reference_collision_block.qc`](resources/reference_collision_block.qc). Do not
"improve" these numbers per model; they are tuned for the ValveBiped animation set and only the
hull **shapes** change between characters.

## How Step 8 builds the hulls

`core.analyze_collision_blend(flexes_blend, source_bodygroups=None, additional_bone_groups=None,
quality_preset="fast_preview")` → `collision_analysis.json`, `collision_plan.json`, `physics_settings.json`;
`core.sort_collision_blend(flexes_blend, plan)` → `*_collision_sorted.blend` with a `Physics` mesh
object, `Physics.smd` (validated by importing it back), report.

For each target bone the tool collects the vertices weighted to it (from the selected source
bodygroups), runs CoACD (convex decomposition) or a direct convex hull, and stores one part:

```json
{"bone": "ValveBiped.Bip01_Pelvis", "enabled": true, "base_shrink": 0.86,
 "coverage_score": 0.7603, "cluster_count": 1, "face_count": 92, "center": [0.0, 0.58, 38.95],
 "coacd": {"method": "convex_hull", "returned_hulls": 1, "input_vertices": 7490, "quality_preset": "fast_preview"},
 "faces": [[7, 4, 26], ...], "collision_group": null}
```

Knobs that matter:

- `source_bodygroups` (list of bodygroup names): **which meshes feed the hulls**. Use only the
  body-shaped groups (`Body`, `Face`/`Hair` for the head, `Arms`, `Legs`, tight clothing). Exclude
  coats, skirts, capes, wings and dangling accessories — otherwise the pelvis/thigh hulls balloon
  to the skirt hem and the ragdoll floats.
- `quality_preset`: `fast_preview` (default, seconds) is usually fine; use the higher presets for
  release if a hull looks jagged. Presets differ in CoACD resolution/threshold and per-bone face
  caps (`COACD_QUALITY_PRESETS`).
- Per part: `enabled` (drop a body — never drop one of the 18), `base_shrink` (0.8–0.9 shrinks the
  hull off the skin so limbs don't self-collide; the tool sets it per bone).
- `additional_bone_groups` — extra bodies, see below.

The report's `validation` re-imports `Physics.smd` and checks hull counts; `coverage_score`
below ~0.5 on a limb usually means the wrong source bodygroup (a sleeve mesh swallowed the arm).

## Extra bodies for hair, skirts, tails (the corpus "phys_rec" classes)

Human ports give physics to a few hair/skirt/tail bones so the ragdoll's cloth drapes instead of
freezing. The porter records each extra body as `Class:Bone` in `phys_rec.txt`; the tool models the
same thing as `additional_bone_groups` with a `rotation_type` chosen from `ROTATION_PRESETS`:

| Class | Meaning (constraint preset) | Typical bones |
| --- | --- | --- |
| `FS` | Front skirt or clothes — swings forward/back only (x −80..2) | front skirt column root |
| `BS` | Back skirt — swings back (x −2..80) | back skirt column root |
| `SL` / `SR` | Side skirt left/right (y ±80 one-sided) | side skirt roots |
| `FL` / `FR` | Front-left / front-right hair (x −60..10, y one-sided 60) | front side locks |
| `BH` | Back hair or tail (x −30..105, y ±30, z ±15) | ponytail/tail root |
| `BL` / `BR` | Back hair, spin left/right (y biased ±60) | twin tails |
| `GF` | Free hair (x ±100, y ±45, z ±30) | short bouncy strands |
| `GS` | Limited all-direction (±10 with friction 3, rotdamping 12) | stiff accessories, wings |

Real records: [`resources/examples/phys_rec_acheron.txt`](resources/examples/phys_rec_acheron.txt)
(`S:L:SweetChild_0_4`, `B:S:SweetChild_0_7`, `B:H:MiddleBackHare1`, `G:F:BackHare1_L`, …) and
[`phys_rec_camellya.txt`](resources/examples/phys_rec_camellya.txt). The notebook syntax splits the
class into two letters (`S:L` = side-left, `B:H` = back-hair); the tool takes the joined code.

Rules:

- Give physics to the **root link** of a chain (or root + one mid link for long hair), never to
  every link — each body costs a part and joint stability. 2–8 extra bodies is typical; 14 is the
  cap.
- Group syntax: `{"group": 1, "bones": ["Skirt_0_0", "Skirt_0_1"], "rotation_type": "FS"}` — the
  **first** bone owns the hull; later bones' weight regions merge into it (use this to make one
  body from a short chain). Bones must be connected, non-ValveBiped, and not in another group.
- Skirt classes by **position around the pelvis** (front/back/left/right), hair classes by position
  around the neck; when a bone sits on a diagonal, prefer the side class for skirts and the
  `FL`/`FR`/`BL`/`BR` diagonals for hair.
- Bones that get a physics body still get a jigglebone in Step 14 (jiggles drive the animated
  model; the ragdoll takes over when the model dies/is physgunned).

## Fitting the reference 18-body template (when generated hulls are bad)

Sometimes CoACD hulls are unusable — very thin anime limbs, arms baked into a coat, a model whose
body under the clothes is missing. The corpus method is to reuse a proven physics mesh and fit it.
[`resources/reference_physics_18body.smd`](resources/reference_physics_18body.smd) is such a mesh:
exactly the 18 bodies, 2 628 vertices, each hull skinned 100 % to its bone, Source scale (a
~5'5" female; it is the physics of the released *Durin* port).

Procedure (Blender, on the Step 8 output blend so the character is already at Source scale):

1. Import the template with Blender Source Tools (`bpy.ops.import_scene.smd(filepath=…, append="APPEND")`
   into the character's armature) so its vertex groups bind to the same ValveBiped bones.
2. For every body, fit the hull to the character: scale along the bone axis so the hull length
   equals the character's bone length (`bone.length` in Blender, or the distance to the child head),
   and scale laterally so the hull's radius matches the mesh — the studiomdl hitbox pass gives
   these extents for free (`$hbox` lines in `14_sort_qc_compile/compile_hbox_probe.log`; each is
   min/max in bone space). A body-space transform per bone is the whole fit: translate the hull's
   centre to the bone head, scale (length, width, thickness), keep 100 % weight on the bone.
3. Delete/replace the tool's `Physics` object with the fitted mesh (same object name `Physics`,
   one vertex group per body, armature modifier) and re-run Step 9. Step 9 exports the `Physics`
   object as `Physics.smd` and — because the hulls are skinned — the proportion trick moves each
   hull with its bone automatically; only sizes needed your fit.
4. Add cloth bodies on top with `additional_bone_groups` if wanted (they are generated from the
   mesh as usual), or hand-model a small box per extra bone and weight it 100 % to that bone.

This is also the fallback when a body has no source vertices at all (a bone with mesh only in a
hidden layer): a template body sized from the hitbox keeps the ragdoll complete.

## Validating physics before and after compiling

- `blender_sort_collision_report.json`: `validation.ok`, hull count (must be ≤ 32), per-part
  `coverage_score`, warnings such as "hull spans more than one bone".
- `compile_main.log`: studiomdl prints one line per collision body; an `ERROR: … convex piece`
  / `too many collision pieces` means a hull is degenerate or over the 32 limit.
- In HLMV (`GarrysMod\bin\hlmv.exe`, open the compiled `.mdl`): enable **Physics Model** and
  check every hull sits inside its limb, no hull spans a joint, the head hull contains the skull.
- In game: spawn the ragdoll, drag it with the physgun and drop it. Joints must not fold backwards
  (constraints), limbs must not vibrate (overlapping hulls → raise `base_shrink`), the model must
  not sink into the floor (pelvis hull too small) or hover (skirt hull in the source set).
