# 03 — Selecting materials and textures to keep (Steps 5 and 12)

Source models allow **32 materials** per model (`DEFAULT_LIMIT` in `tools/blender_sort_materials.py`),
every material needs a real base texture, and the material list is also what Step 6 groups into
bodygroups. MMD models often carry 30–80 materials (per-part duplicates, hidden layers, sphere/toon
helpers, "shadow" materials with no texture). Step 5 cleans this up; Step 12 turns the survivors into
VTF/VMT.

## Step 5 — materials

`core.scan_materials_blend(bones_sorted_blend)` → `5_sort_materials/material_scan.json` + `material_plan.json`;
`core.apply_materials_initial_blend(bones_sorted_blend, plan)` → `..._materials_sorted.blend`, `materials.json`
(the texture manifest Step 12 reads) and `material_initial_report.json`. If more than 32 materials
remain, the GUI/worker runs `core.merge_materials_blend(blend, material_merge_plan)`.

`material_plan.json` rows (`materials[]`):

```json
{"uid": "mci_mat_001_body", "material_name": "Body.001", "proposed_name": "Body",
 "keep": true, "combine_target_uid": "mci_mat_001_body",
 "alpha": 1.0, "diffuse": [1,1,1,1], "has_base_texture": true,
 "base_color_path": ".../0_source_mmd_assets/.../tex/Body.png", "base_color_file": "Body.png",
 "base_alpha_zero_ratio": 0.0, "vertex_count": 33979, "face_count": 63676,
 "object_names": ["Body"], "slot_refs": [{"object": "Body", "slot_index": 0}],
 "warnings": []}
```

- `keep` — false deletes the material's faces. The scan defaults `keep=false` for materials whose
  MMD alpha is **below 0.5** (invisible layers that MMD hides through material alpha — undergarment
  "off" variants, hidden expressions, alternate outfits) and for stacked duplicate layers it can
  prove are covered (`STACKED_FACE_*` detection). Review these: a material with alpha 0 that is a
  *toggleable* outfit piece should instead be **kept and put in its own bodygroup** (Step 6) if
  the underlying body is complete.
- `combine_target_uid` — materials that share the same base texture are combined into one Source
  material (same VTF, one VMT). This is the primary way under the 32 limit: MMD models that give
  every part its own material name but one atlas texture collapse to a handful. Never combine two
  materials with different textures (the second texture is lost).
- `proposed_name` — the Source material name (`[A-Za-z0-9_]`, unique). Use the corpus vocabulary:
  `Body`, `Face`, `Hair`, `Eye`, `EyeHi`, `Clothes`, `Coat`, `Skirt`, `Shoes`, `Socks`, `Accessory`.
  Eye/face/mouth/teeth stay **separate** materials even when they share an atlas with the body if
  they need different VMT treatment (eyes usually want no alpha test, faces want `$nodecal`).
- Materials with `has_base_texture == false` (toon/sphere-only materials, "Shoe_metal" style matcap
  parts) get a generated flat-colour texture unless you point `manual_base_texture` at a real
  image; usually merge them into the neighbouring material with the same colour instead.
- `SHADOW_NO_BASE_HINTS` (`shadow`, `eyeshadow`) materials without a texture are dropped by default —
  they are MMD shading tricks with no Source equivalent.

Decision rules:

1. Drop what MMD hides: alpha < 0.5, zero-vertex materials, `shadow*` helpers, duplicate stacked
   shells (`base_alpha_zero_ratio` ≈ 1 or the stacked-face warning).
2. Combine by texture, not by name. Check `base_color_path` equality; the scan already proposes
   `combine_target_uid` — confirm it did not pair two different atlases with the same file name in
   different folders (`base_color_key` is the resolved lowercase path used for the match).
3. Keep future bodygroup boundaries: if two parts share a texture but one must be hideable
   (a removable coat), keep them as **two materials** — Step 6 groups by object+material.
4. Aim for 8–20 materials on a typical character; hard cap 32. When forced to merge distinct
   textures, merge the smallest-area accessory into a neighbour with a similar colour, never the
   face into the body (face flexes and `$nodecal` differ).
5. Names must be stable before Step 12: later steps reference `proposed_name` in plans, VMTs and
   the QC.

## Step 12 — textures and VMT knobs

`core.analyze_textures(materials_json, game=..., max_texture_edge=0, scheme="legacy")` →
`12_param_texture_render_materials/textures_plan.json`; `core.process_textures(materials_json, plan, ...)` →
`png/<Material>.png` (+ `normal/<Material>_n.png` when enabled), `textures_manifest.json`.

- **Legacy scheme (auto-port default)**: base texture only, converted to RGBA PNG, capped at
  4096 px (GMod/SFM) or 2048 px (L4D2, which crashes on larger maps). Normal-map generation is
  available but **disabled by default** (`normal_action`) to keep addons small; enable it per
  material for cloth/leather where a subtle bump helps, never for faces/eyes (it adds noise).
- **PBR schemes** (`unreal_wuwa`, `unity_endfield`, `moongaze_pbr`) for game rips that ship
  metallic/roughness/AO/emissive maps: they look for sibling files (fuzzy matched, possibly in a
  different sub-folder such as `extra texture/`), bake AO into the base, emit a phong-exponent map
  (`<mat>_exp`) and a self-illumination mask. Use them only when those maps exist; otherwise legacy.
- `base_has_transparency` (recorded per material): fully opaque bases get **no `$alphatest` /
  `$allowalphatocoverage`** in the VMT; textures with real alpha keep them. If a material has a
  bogus alpha channel (bake artefacts), flatten its alpha to 255 in the PNG before Step 14.
- Missing/unreadable base textures do not stop the step: a neutral grey placeholder is written and
  the material is listed in `placeholder_materials` in the report. Fix the source path in
  `materials.json` (or `manual_base_texture`) and re-run rather than shipping grey parts.

The VMT written by Step 14 (`write_vmt` in `tools/sort_qc_compile.py`) is the corpus standard:
`VertexLitGeneric`, `$bumpmap` (shared flat normal unless generated), `$nocull 1`, optional
`$nodecal 1`, alpha test only for translucent bases, `$lightwarptexture` (toon ramp), `$phong 1`,
`$phongboost 1`, `$phongalbedotint 1`, shared `$phongexponenttexture`, `$phongfresnelranges`
(`[0 0.5 1]` without a normal, `[0 1.5 2]` with one), `$rimlight 1 / exponent 2 / boost 2`
(GMod/L4D2). Human ports sometimes raise `$phongboost` (the corpus notebook used 24 with
`$phongfresnelranges "[0 0 1]"`) for glossier stylised skin — treat `$phongboost` as the one knob
to tune per model; leave the rest.

## Texture QA before compiling

- Every kept material must have a non-placeholder `png/<Material>.png`; sizes are powers of two
  after VTFCmd (the tool pads/normalises when needed).
- Alpha: hair/eyelashes/transparent accessories keep alpha; body/face/clothes should be opaque
  (check `base_has_transparency` in `textures_manifest.json`).
- Total VTF size: a release addon should stay under ~150 MB; 4096² bases are rarely worth it —
  set `max_texture_edge=2048` for accessories and keep 4096 only for the body/face atlas.
- The corpus compressed textures with VTFEdit by hand; the tool uses the bundled VTFCmd with
  default DXT1/DXT5 selection by alpha, which matches.
