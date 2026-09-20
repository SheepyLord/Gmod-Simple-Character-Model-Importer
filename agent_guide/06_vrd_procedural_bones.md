# 06 — VRD procedural bones: selecting and validating skirt helpers (Step 11)

Jigglebones react to motion but not to pose: when a character crouches or walks, a skirt driven
only by jiggles clips straight through the thighs. Source's `$proceduralbones` (a `.vrd` file of
`quatinterp` helpers) fix this by rotating skirt bones **as a function of the thigh angle**. Step 11
infers which bones should be helpers, which thigh drives each, and samples the driver poses.

## What the tool does

`core.analyze_vrd(<9_export_proportion_trick/2_proportion_export>)` → `11_sort_vrd/vrd_analysis.json`,
`vrd_plan.json`, `vrd_preview.json`; `core.apply_vrd(final_dir, plan)` → `vrd.vrd` (compiled into
the QC as `$proceduralbones "vrd.vrd"`), `blender_sort_vrd_report.json`.

`vrd_plan.json`:

```json
{"intensity_multipliers": {"10": 0.75, "20": 0.62, "30": 0.26},
 "rows": [
  {"uid": "vrd_003_skirt_0_1_l", "procedural_bone": "Skirt_0_1", "driver_bone": "ValveBiped.Bip01_L_Thigh",
   "essential_parent": "ValveBiped.Bip01_Pelvis", "direct_parent": "Skirt_0_0",
   "side": "left", "rear_panel": false, "angle": 90.0, "front_offset": 4.13,
   "confidence": 0.925, "enabled": true,
   "auto_frame_weights": {"10": 0.99, "20": 0.99, "30": 0.0}, "frame_weight_overrides": {"10": null, "20": null, "30": null},
   "centroid": [0.37, -3.34, 47.19], "bounds": {"mins": [...], "maxs": [...]},
   "source_objects": ["Skirt"], "material_uids": ["skirt__mat_061"], "weighted_vertices": 75,
   "warnings": ["Medium-confidence VRD candidate; review before enabling."]}
 ]}
```

- Driver frames `10`, `20`, `30` are the thigh swung forward by increasing angles (the tool poses
  the armature at those frames in the workspace blend); `intensity_multipliers` scale how far the
  helper follows at each frame (SFM auto-port uses the gentler `0.6 / 0.3 / 0.15`).
- `confidence` combines: name hints (skirt/dress/cloth/hem), height band (lower garment between
  pelvis and knee), geometry (the bone's vertices sit in front/side of the thighs), hierarchy
  (parented under pelvis/spine, not head), size. Candidates below 0.50 are not listed; rows are
  **enabled by default only when `confidence >= 0.78` and `rear_panel` is false**.
- `side` = `left` / `right` / `center`; a `center` bone gets one row per thigh (both drive it).
- `rear_panel` marks bones behind the pelvis: they should usually stay **disabled** (a rear skirt
  panel rotating with the front thigh swing looks wrong; rear panels are handled by jiggles).

## Selecting the right bones

Enable a row when **all** hold:

1. The bone is a skirt/dress/coat-tail/cloth bone that would otherwise clip through the leg —
   front and side panels of skirts, long coat fronts, apron ties, loincloths. Not hair, not
   sleeves, not tails, not breasts (those are jiggle-only).
2. It is the **root** or second link of its column. Deeper links inherit the rotation through
   the hierarchy; adding helpers on every link multiplies triggers and produces double rotation.
3. `driver_bone` is the thigh on the same side (`side == left` → `L_Thigh`); center bones are
   fine with both rows enabled.
4. `weighted_vertices` is meaningful (a few dozen or more) and the `bounds` are below the pelvis
   (`maxs[2]` under the pelvis height ≈ 39 units in Source scale) — the tool warns "outside the
   preferred lower garment height band" otherwise.

Disable rows with warnings you cannot explain, `rear_panel: true`, bones on hair/accessories,
and any bone you merged in Step 4 (it will not exist; the apply step reports missing bones).

Budget — measured with the GMod studiomdl (comments at the top of `tools/blender_sort_vrd.py`):
a single helper with more than **64 triggers** crashes studiomdl, and the whole model fails
(`procedural bone ""`) above roughly **400 triggers total**. The exporter densifies each helper's
grid (swing × twist), so keep enabled rows to about **8–12 helpers**; the report prints the trigger
count per helper — stay under 30 each.

## Validation

- `vrd_preview.json` gives, per enabled row and per frame, the interpolated rotation; anything
  above ~90° at frame 30 means the multiplier is too high for that bone — set
  `frame_weight_overrides` (0–1 per frame) on the row instead of changing the global multipliers.
- Open `11_sort_vrd/*.blend` headlessly, set `scene.frame_set(0/10/20/30)` and dump the world
  matrix of each helper bone: the skirt bone must rotate **away** from the thigh (forward/outward),
  never into the leg, and frame 0 must equal the rest pose.
- After Step 14, grep `compile_main.log` for `procedural` — a trigger-budget failure surfaces here;
  reduce helpers or their grid.
- In game: walk, sprint, crouch (`ctrl`) and sit in a vehicle. The front panels should lift over the
  thighs; if a panel snaps or flickers it has two helpers fighting (a link and its parent both
  enabled) — keep only the root.
- Runtime convention note: since the L4D2 arm/leg fixes the compiled bind pose is the tool's fitted
  skeleton while runtime rotations follow the stock animations; the VRD is authored on the Step 9
  skeleton and the measured mismatch on thighs is ~3°, which is within the trigger tolerance. No
  per-game adjustment is needed.
