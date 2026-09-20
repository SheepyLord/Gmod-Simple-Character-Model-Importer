# 07 — In-game icons with a custom animation, and the Workshop icon (Step 13)

A port needs three pictures: the two **spawn-menu icons** (`<model>_<author>_F.vtf` for the
Friendly NPC, `_E` for the Enemy NPC; 512×512, shown in the NPC tab) and the **Workshop icon /
preview** (the image players see on Steam; the corpus uses a 2000×2000 JPEG, `gmpublish` wants a
512×512 JPEG). All three come from one render of the character in a chosen pose and expression.

## What the tool does

`core.run_icons(workspace_root_or_step1_blend, custom_source_image=None, icon_basename="<model>",
body_vmd_path=None, face_vmd_paths=None, frame=None)`:

1. `analyze_icons` writes `13_sort_icons_and_arts/icons_plan.json` (paths + the VMD/frame it will use).
2. `tools/blender_render_icon.py` (headless Blender) imports the **original PMX** with mmd_tools,
   applies the body VMD (default `reference/ref_motion/bad_bad_water.vmd`, frame **334**) plus any
   face VMDs, then frames an **orthographic front camera** on the upper body: it measures the
   evaluated mesh bounds, aims slightly along the eye bones' forward direction (never behind the
   head), adds a white backdrop and a neutral three-point light, and renders
   `release_icon.png` (1024², also copied as `SPIC.png`) and `spawn_source.png`.
3. `tools/sort_icons_and_arts.py` fits the spawn source onto a 512² canvas, stamps the labels
   **"Friendly"** (blue, `(0,170,255)`) and **"Enemy"** (red) → `F.jpg/F.png`, `E.jpg/E.png`,
   converts them to VTF with the bundled VTFCmd and writes `F.vmt`/`E.vmt` (`UnlitGeneric`,
   `$vertexalpha 1`, `$vertexcolor 1`). Step 14 copies them to
   `materials/vgui/entities/<model>_<author>_F|E.vtf|vmt`.

Report: `icons_report.json` + `render_report.json` (camera location, bounds, which eye-direction
mode was used, frame actually rendered).

## Choosing the animation and frame (custom VMD)

The default motion is a dance file whose frame 334 gives a relaxed 3/4 upper-body pose for most
models. Override when the default clips (weapons, wings, huge sleeves), when the face is covered,
or when a character has a signature pose:

```python
core.run_icons(ws.root, icon_basename=MODEL,
               body_vmd_path=Path("poses/idle_hand_on_hip.vmd"),
               face_vmd_paths=[Path("poses/smile.vmd")],
               frame=0)
```

Pick the frame with these criteria (check `spawn_source.png` after the render, re-run with another
frame if any fail):

- Eyes open and looking roughly at the camera; mouth closed or a slight smile (use a face VMD
  with `blink`/smile morphs if the body VMD alone leaves a neutral stare).
- Hands and props away from the face; shoulders visible; no limb crossing the chest.
- Head not tilted more than ~15° (the ortho crop is head-and-shoulders; a strong tilt cuts hair).
- Hair/skirt physics at rest — pick a frame where the VMD has been holding a pose for 20+ frames,
  not mid-swing (MMD physics is not simulated in the headless render, but bones keyed by the VMD
  still move).
- Any MMD-format pose works: a single-frame `.vmd` exported from MMD/PMXEditor, or a frame from a
  dance. Keep the VMD next to the model in the workspace so the plan stays reproducible.

## Custom source image (hand-made icon or workshop art)

`custom_source_image=Path("my_render.png")` skips the render and uses your image for **both** the
release icon and the spawn icons. Use it when you rendered the character with better lighting in
Blender/MMD, or when the Workshop icon needs branding. Requirements: square, ≥1024 px, PNG/JPG,
subject centred with headroom (the spawn icons are a centre crop with the label along the bottom).

Corpus conventions for release art (see any `9_art/` folder in a released port):

| File | Size | Purpose |
| --- | --- | --- |
| `Release.jpg` | 2000×2000 | Workshop preview image (also used as the first screenshot) |
| `SPIC.png` / `WSIC.png` (+ `.psd`) | 2000×2000 | Steam preview and Workshop icon composites: the render on a branded background with the character/game name; the PSD keeps the layers |
| `SPICR.png` / `WSICR.png` | 512 / 2000 | the rendered character cut-out used in those composites |
| `E.jpg`, `F.jpg` | 512×512 | spawn icons (what the tool generates) |
| `PS1.jpg` … `PS4.jpg` | 3840×2160 | in-game screenshots for the Workshop gallery (flatgrass, posed with the physgun/ragdoll, one close-up of the face, one showing bodygroups) |

`gmpublish` requires the addon icon to be a **512×512 baseline JPEG**; the tool's
`release_icon.png` is 1024² — downscale/convert it (`Pillow`: `Image.open(...).convert("RGB").resize((512,512)).save("icon.jpg", quality=90)`)
before `gmpublish create -addon <gma> -icon icon.jpg`. The 2000² `Release.jpg` is uploaded as the
preview image on the Workshop page afterwards.

## Quality checklist

- `F.png`/`E.png` show the face at least 40 % of the canvas height, the label readable, no white
  backdrop halo around hair (if there is, re-render with a face VMD that closes gaps or use a
  custom image with a transparent background).
- VTFs generated (`friendly_vtf`/`enemy_vtf` in the report, non-empty). If VTFCmd was not found the
  report lists it under `validation_errors`; the compile still succeeds with fallback icons from
  `reference/li_zhiyan_npc/a_pack/materials/vgui/entities`, which is **not** acceptable for release.
- In game the icons appear under **NPCs → `<Category readable>`**; a black square means the VMT
  base texture path does not match `<model>_<author>_F` — check `icon_basename` equals the Step 14
  `model_name`.
