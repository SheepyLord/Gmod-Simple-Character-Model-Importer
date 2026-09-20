# 02 — Selecting bones to merge (Step 4)

Source models have a hard bone budget (**254** for GMod/SFM, **126** for L4D2 survivors —
`GAME_BONE_LIMITS` in `tools/blender_sort_bones.py`). MMD models routinely arrive with 400–900 bones
(fingers, twist helpers, IK targets, hair strand chains, skirt grids, face rigs). Step 4 decides
which bones survive. The decision matters twice: a kept non-essential bone becomes a **jigglebone**
and/or **ragdoll physics body** later (Steps 8/14), and a merged bone folds its vertex weights into a
parent, freezing that part of the mesh to the parent.

## What the tool does

`core.analyze_sort_bones_blend(spine_fixed_blend, limit=None, game="gmod")` writes
`4_sort_bones/bone_merge_analysis.json` and `bone_merge_plan.json`, then
`core.sort_bones_blend(spine_fixed_blend, plan, game=...)` applies it.

`bone_merge_plan.json` (fields you edit):

```json
{
  "armature": "Armature",
  "bone_limit": 254,
  "initial_bone_count": 838,
  "estimated_final_bone_count": 254,
  "base_protected_bones": ["Eye_L", "Eye_R", "ZArmTwist_L", "ZArmTwist_R", "ZHandTwist_L", "ZHandTwist_R"],
  "operations": [
    {"order": 0, "round": 0, "enabled": true,
     "source": "Tongue5", "target": "ValveBiped.Bip01_Head1",
     "branch": "Head face detail", "depth": 12,
     "reason": "always merge flex/face detail into Head1", "warnings": []},
    {"order": 7, "round": 1, "enabled": true,
     "source": "Skirt2_0_3", "target": "Skirt2_0_2",
     "branch": "Skirt2_0_0", "depth": 12,
     "reason": "alternating ...", "warnings": []}
  ]
}
```

- `operations[]` — one merge each. `source` is deleted and its weights move to `target`.
  Set `"enabled": false` to keep a bone; add a new operation to merge one the tool kept.
- `round` — 0 = mandatory merges (face detail, helpers), higher rounds are budget-driven thinning,
  applied in order until `estimated_final_bone_count <= bone_limit`.
- `base_protected_bones` — never merged (eyes, arm/hand twist helpers the VRD/c_arms need).
- All `ValveBiped.*` bones are essential and are never merge sources.

The plan must still land under the limit after your edits; the apply half reports the final count
in `blender_sort_bones_report.json` and Step 14 will fail with studiomdl's "too many bones" if not.

## Decision rules (what a human porter keeps)

Think of every non-ValveBiped bone as one of three classes and decide per **chain** (root→tip),
never per isolated bone:

1. **Keep as a physics/jiggle chain** — anything that should move on its own in game:
   hair strands (front bangs, side locks, back hair, pigtails/twintails, ahoge), skirt/dress
   columns, capes, coat tails, ribbons, scarves, tails, ears, wings, sleeves that hang, breast
   bones, accessories that dangle (earrings, charms, chains). Keep the chain **connected from the
   ValveBiped parent to the tip**; a chain with a hole in the middle produces a bone whose parent
   moved and a jigglebone that pops.
2. **Merge into the ValveBiped parent** — helpers that only exist to deform the base mesh:
   tongue/teeth/eye-detail rigs (→ `Head1`; the tool's round-0 "always merge flex/face detail into
   Head1"), knee/elbow/hip correctives (`KneeD+`, `LegD+`, `KneeRotation`, `Elbow*`, `Shoulder*`
   → thigh/calf/upperarm), IK targets and controllers, finger sub-segments beyond the three
   ValveBiped segments (`*Finger*3`, nail bones), foot "toe finger" bones (`LegThumb`,
   `LegIndexFinger…` → `Toe0`/`Foot`), decorative bones with no vertex weight (the Step 9 export
   removes zero-weight non-essential bones anyway).
3. **Keep, but only as a light "omni" jiggle later** — small decorations that should wobble
   without direction constraints: clothes-on-breast bones, ear rings, hair ornaments, epaulettes.
   These stay in Step 4 and are classified in Step 14 (see [08](08_compile_bodygroups_jiggles.md)).

Rules of thumb when the budget forces thinning:

- **Thin chains by alternation, not truncation.** A 12-link skirt column keeps links 0/3/6/9 (or
  0/2/4/…), never just the first two — a truncated chain freezes the hem. The tool's
  "alternating" reason implements exactly this; when you thin manually, keep the root and the tip.
- **Keep left/right pairs symmetric** — drop the same link index on both sides.
- **Prefer merging fingers/face helpers over hair/skirt links**: players notice frozen hair before
  they notice a missing knuckle bend.
- **Twist helpers** (`ZArmTwist_*`, `ZHandTwist_*`, `*Twist*`, `Ulna`) stay protected: c_arms and
  the VRD use them; merging them breaks first-person forearm twist.
- **Physics-capable skirt columns count double**: each kept skirt bone may become a VRD helper
  (Step 11) and/or a ragdoll body (Step 8, max 32 total collision parts) — for a heavy dress keep
  4–6 columns × 2–3 links rather than 8 columns × 4 links.
- **L4D2 (126 bones)**: keep at most ~40 non-essential bones; hair front/back/sides 1–2 links each,
  one skirt ring, breasts; merge everything else.

The corpus records the human decisions as `bone_list.txt` (bones kept) and
`bone_list_ignore.txt` (kept bones that only get the light jiggle). Real examples:
[`resources/examples/bone_list_acheron.txt`](resources/examples/bone_list_acheron.txt) keeps four
`SweetChild_{0,3,6,9}_{4,7,8,11}` skirt links per column (alternation of 12-link columns), full
`SideLongHare1..8_R` hair chains, `Breast1/3_L/R`, and
[`bone_list_ignore_acheron.txt`](resources/examples/bone_list_ignore_acheron.txt) demotes
`BreastClothes*`, `EarsRing`, `HareDecoration`, `Sleeve*`, `UpperSleeve*` to light jiggles.

## How to evaluate a plan without guessing

- The analysis JSON lists every bone with `weighted_vertices`, position, depth and branch. A bone
  with `weighted_vertices == 0` is free to merge; a chain whose links each carry hundreds of
  vertices is real cloth/hair.
- Open the Step 3 blend in the managed Blender headlessly and dump the armature to inspect
  chains you are unsure about:

  ```bash
  "<managed blender>/blender.exe" --background <3_fix_spine_bones/*.blend> --python probe.py -- out.json
  ```

  where `probe.py` walks `bpy.data.objects[...].data.bones` and prints `name, parent, head, length,
  len(children)` plus vertex-group weight sums from the mesh. (`core.setup_state_is_current(core.read_setup_state())`
  returns the managed Blender path.)
- After apply, `blender_sort_bones_report.json` lists `merged`, `kept`, `final_bone_count`, and any
  bones the apply could not merge (`reason: "source is protected"`, `"target missing"`). A merge
  whose target was itself merged earlier is re-targeted automatically; a cycle aborts the step.
- Re-run Step 4 from the Step 3 blend as many times as needed; it never modifies its input.

## Typical failure modes

| Symptom later in the pipeline | Cause in Step 4 | Fix |
| --- | --- | --- |
| Hair/skirt stiff as a board in game | chain merged (thinned to 1 link, or a middle link merged) | re-enable the links; thin by alternation |
| Jigglebone "explodes" or spins | a kept bone has no vertices / is a leaf far from mesh | merge it, or mark it `Not Jiggle` in Step 14 |
| studiomdl: `too many bones` / `bone count exceeds 254` | plan edited above the limit | check `estimated_final_bone_count`; disable more low-value bones |
| Fingers don't bend in poser | merged a ValveBiped finger segment's *source* helper that carried the weights | keep the helper, or move its weights (Step 4 does this automatically for direct parents only) |
| VRD step finds no candidates | skirt bones merged away | keep skirt columns in Step 4 |
