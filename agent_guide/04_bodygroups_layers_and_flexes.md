# 04 — Bodygroups, multi-layered models and flexes (Steps 6 and 7)

A bodygroup is a mesh part players can toggle in the spawn menu (`$bodygroup "Coat" { studio
"Coat.smd" blank }`). Step 6 splits the single MMD mesh into bodygroup meshes, scales the model to
Source units and enforces the per-mesh vertex limit; Step 7 decides which shape keys become flexes
and on which bodygroup they live.

## Step 6 — bodygroups

`core.analyze_bodygroups_blend(materials_blend, scale_factor=40.457, scale_preset="factor",
vertex_limit=65535)` → `6_sort_bodygroups/bodygroup_analysis.json` + `bodygroup_plan.json`;
`core.sort_bodygroups_blend(materials_blend, plan, scale_factor=..., vertex_limit=...)` →
`..._bodygroups_sorted.blend`, report.

Plan (`version` 3) essentials:

```json
{"scale_factor": 40.457, "vertex_limit": 65535,
 "bodygroups": [
   {"uid": "bg_001_face", "enabled": true, "proposed_name": "Face",
    "source_uids": ["bgsrc_006_body005", "..."], "source_objects": ["Body.005", "..."],
    "material_names": ["face", "EYE", "EYEhi", "MI_Bangs"],
    "material_vertex_counts": {"face": 2899, "EYE": 384},
    "dominant_material_name": "face", "auto_name_source": "category",
    "material_alphas": {"face": 1.0}, "zero_alpha_materials": []}
 ],
 "facial_merge": {"enabled": true, "target_bodygroup": "Face", "neck_filter_bone": "ValveBiped.Bip01_Neck1", "...": "..."},
 "auto_split": {"vertex_limit": 65535, "always": false, "required": false, "over_limit_sources": []},
 "splits": [], "removed_zero_vertex_bodygroups": [], "manual_edit_blend": "", "manual_edit_renames": {}}
```

- Every `bgsrc_*` source (an object+material chunk from Step 5) must appear in exactly one
  bodygroup's `source_uids`. Move sources between bodygroups to regroup; rename with
  `proposed_name` (`[A-Za-z0-9_]`, PascalCase).
- `facial_merge` pulls every chunk that carries facial shape keys and sits above the neck into the
  `Face` bodygroup (so all face flexes live on one mesh, under the 65k/32k cap). Chunks with facial
  keys that extend below `Neck1` (a body mesh whose morph list includes "Upper Transfer"-style
  keys) are excluded and listed in `neck_filtered_sources` — decide whether they need the keys
  (usually not) or should be split.
- `auto_split` handles meshes above the vertex limit (65535 normally, **32767 for RTX-Remix**
  builds) by splitting by material; if a single material exceeds the limit you must reduce it in
  Blender (decimate or split by UV island) before continuing.
- `scale_factor` 40.457 is the corpus MMD→Source scale for an average adult female
  (`scale_preset="factor"`, the default). For a child or a very tall character use
  `scale_preset="tall"|"normal"|"short"` (baked reference face-top heights from released ports;
  the scale is then solved so the face lands at that height) or pass your own reference
  `scale_reference_smd=` (a `Face.smd` from a port whose height you want to match). Check the
  eye height lands near 64 units after scaling (report bounds); the playermodel hull assumes it.

### Naming and hideability conventions

Corpus bodygroup names (use them; the QC essential list and icon text depend on some):
`Face`, `Body`, `Hair`, `Arms`, `Legs`, `Clothes`, `Coat`, `Jacket`, `Skirt`, `Pants`, `Shoes` /
`LeftShoes` / `RightShoes`, `Socks` / `LeftSock` / `RightSock`, `Pantyhose`, `Bra`, `Kamikazari`
(hair ornament), `Earring`, `Rings`, `LegRing`, `Necklace`, `Wings`, `Tail`, `Claws`, `Tatoo`,
`Patch`. `Physics` is reserved.

Step 14 makes a bodygroup **non-hideable** (no `blank` line) when its name is `Body`, `Hair`,
`Face`, `Legs` or `Arms` (`ESSENTIAL_BODYGROUP_NAMES`) or when it carries flexes (`has_flex`) — a
flexed bodygroup cannot be blanked or the face poser breaks. Everything else defaults to
hideable (`can_hide: true` in `qc_plan.json`'s `bodygroups` rows) and you can flip it per row.

### Multi-layered models (detachable clothes, body underneath, accessories)

The core question for every removable layer: **is the surface underneath complete?**

1. Body under clothes is fully modelled (many game rips, most "nude base" MMD models) → clothes
   go in their own bodygroups (`Clothes`, `Coat`, `Skirt`, `Pants`, `Shoes`, `Socks`) and stay
   hideable. Keep the body's materials separate from the clothes' so nothing gets combined across
   the layer.
2. Body under clothes is **missing** (MMD models routinely delete hidden skin) → either put the
   clothing in a bodygroup that is **not** hideable (set `can_hide: false` in Step 14, or name the
   merged chunk `Body`), or merge those chunks into the `Body` bodygroup. A hideable coat over a
   hollow torso shows the inside of the mesh (with `$nocull` the backfaces render dark).
3. Layered pieces that must toggle together (jacket + its collar + its buttons) belong in **one**
   bodygroup: move their `source_uids` into it. Pieces that should toggle independently
   (glasses, hat, hair ornament, weapon) each get their own group — one bodygroup per accessory
   is the corpus norm (`Kamikazari`, `Earring`, `Rings`, `LegRing`, `Patch`).
4. Alternate hairstyles/outfit variants inside one PMX (materials with alpha 0 in MMD): keep the
   variant materials in Step 5, give each variant its own bodygroup, and make sure exactly one
   variant is the default (the first `studio` entry) — Step 14 emits only one mesh per group, so
   true "either/or" variants need two bodygroups both hideable, or a manual QC edit adding a second
   `studio` line.
5. Hair is usually one group (`Hair`, non-hideable) but split a detachable ponytail/wig piece into
   `Ponytail` if the model has physics for it and players will want it toggled.
6. Symmetric single items (`LeftShoes`/`RightShoes`) only when the source has them as separate
   objects and toggling one side is meaningful; otherwise `Shoes`.

Multi-layer clothes also interact with **physics**: Step 8 builds hulls from the body meshes you
select as `source_bodygroups`; exclude coats/skirts so the ragdoll hulls hug the body, and give the
skirt its own collision group (see [05](05_physics_ragdoll.md)).

Splitting one atlas material into accessory bodygroups (game rips with 5-6 atlases where
"Body" also holds the hairpin, earring, chains, tassels): a bodygroup source is an
*object + material* chunk, so split the Step 5 output mesh into named objects **before** Step 6
and re-run `analyze_bodygroups_blend` on the new blend. Headlessly: union-find the faces into
connected islands (same material only), compute each island's dominant deform bone
(`vertex_groups` weight sum), classify by bone name (`Piao048_L` -> Earring, `Piao_L*`/`Piao_R*`
-> Chains, `Skirt*` or a `zmax` threshold -> Skirt, ...), select those faces in edit mode and
`bpy.ops.mesh.separate(type="SELECTED")`, rename the new object to the bodygroup name (shape
keys and the armature modifier survive the separation). The Step 5/6 blends are still in
**metres** (Source units only appear inside the Step 6 analysis JSON), so divide any Source
height threshold by the scale factor (47.5 units -> 1.174 m). Then regroup `source_uids` in the
plan: `Face` gets every facial chunk plus eye bases, `Clothes` gets the top plus small attached
decorations, each accessory its own group. The recorded Yinlin port used exactly this
(`agent_guide` session 2026-09-20).

Verification: the report lists `vertex_count` per bodygroup and `removed_zero_vertex_bodygroups`;
open `..._bodygroups_sorted.blend` headlessly and check that every object has an armature modifier
and that no object exceeds the vertex limit. Use `manual_edit_blend` when a chunk must be edited by
hand (delete stray triangles, fix a piece assigned to the wrong material): the plan re-imports the
manually edited blend and `manual_edit_renames` maps renamed objects back.

## Step 7 — flexes

`core.analyze_flexes_blend(bodygroups_blend, game=...)` → `7_sort_flexes/flex_analysis.json` +
`flex_plan.json`; `core.sort_flexes_blend(bodygroups_blend, plan)` → `..._flexes_sorted.blend`,
`flexes.json`, report.

`flex_plan.json`:

```json
{"flexes": [
  {"uid": "flex_001", "original_name": "Blink", "final_name": "blink", "category": "eyes",
   "action": "keep", "enabled": true, "bodygroup": "Face", "confidence": 1.0,
   "max_delta": 0.90164, "max_amplitude": 1.0, "rest_value": 0.0, "source_flexes": [], "warnings": []}
 ],
 "auto_removed_for_source_limit": [], "enabled_flex_count": 61}
```

- `final_name` comes from `tools/flex_name_dictionary.json` (corpus majority vote, 470+ MMD/Japanese
  morph names → Source names: `blink`, `eye_blink_left`, `brows_angry`, `brows_sad`, `eyes_smug`,
  `mouth_smile`, `jaw_drop`, `ah`, `oh`, `wink`, …; suffixes `_left/_right`, `_02` for duplicates).
  A `confidence` below ~0.7 means a fuzzy match — read `original_name` and rename by hand.
- `category` (`eyes`, `brows`, `mouth`, `cheeks`, `body`, `other`) drives ordering in the face poser.
- `enabled: false` drops a morph. Drop: morphs with `max_delta` ≈ 0 (no-ops), morphs that move
  the whole body or hide parts (MMD "outfit off" morphs — those are bodygroups, not flexes),
  duplicate variants (`_02`, `_03` of the same pose) beyond 2–3, and anything the Source limit
  rejects: the enabled count must stay **below 95** (`MAX_SOURCE_FLEXES_EXCLUSIVE`; the tool
  targets 94 and lists what it dropped in `auto_removed_for_source_limit`). Keep the expressive
  ~40–70.
- `bodygroup` — the mesh the flex lives on; must be `Face` for facial morphs (see facial merge) —
  a flex on a hideable bodygroup makes that group non-hideable (the Step 7 UI shows this notice).
- L4D2 keeps only morphs mapping to the survivors' 52 FACS controllers (`KNOWN_L4D2_FLEX_CONTROLLERS`);
  everything else is dropped automatically.

Verification: after apply, `flexes.json` is what Step 9 exports as VTA frames; `blender_sort_flexes_report.json`
lists untranslated names — if many facial morphs stayed untranslated (e.g. CJK names not in the
dictionary), add mappings to the dictionary JSON (and consider contributing them upstream) rather
than shipping `morph_123` names.
