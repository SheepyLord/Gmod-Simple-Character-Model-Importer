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

## Corpus-standard Workshop portrait (`SPIC.png`) rendered with Cycles

The Step 13 renderer makes serviceable spawn icons, but the corpus `SPIC.png`/`Release.jpg` are
proper Cycles portraits. Every human port carries the same scene in `2_Blends/arts.blend`
(a reusable template - the port's copy contains only the camera): **Cycles on the GPU, 2048-4096
samples, denoised, AgX view transform, 2000x2000 with `film_transparent`, no lamps at all - the
world background is pure white at strength 1 and does the lighting**, a 50 mm perspective camera
roughly 0.8-0.9 m in front of the face at about head height, tilted slightly down (corpus cameras
sit at (0, -0.83, 1.47..1.53) with rotation X 65-76 deg), the model imported with MMD Tools (its
`MMDShaderDev` toon materials are what give the corpus look) and posed on an expressive frame of a
dance VMD. The human then composites the transparent render onto branded art in Photoshop
(`SPIC.psd`, `WSIC.psd`); the plain render is what an agent ships.

[`resources/render_spic_cycles.py`](resources/render_spic_cycles.py) reproduces this headlessly
(it reuses the Step 13 renderer's MMD Tools import helpers):

```bash
# 1. contact sheet: 18 candidate frames at 400 px / 32 samples (a few seconds each on a GPU)
"<managed blender>" --background --factory-startup --python-exit-code 1   --python agent_guide/resources/render_spic_cycles.py -- --pmx <ws>/0_source_mmd_assets/<model>.pmx   --vmd reference/ref_motion/bad_bad_water.vmd --frames 60,130,210,300,334,372,528,585,900,1250,1560,1800,2000,2450,2680,2975,3470,3780   --out-dir <scratch>/prev --res 400 --samples 32 --report <scratch>/prev.json
# 2. tile <scratch>/prev/preview_*.png with Pillow, look at the sheet, pick the frame
# 3. final 2000 px / 1024 samples straight into the port folder
"<managed blender>" --background --factory-startup --python-exit-code 1   --python agent_guide/resources/render_spic_cycles.py -- --pmx ... --vmd ... --frames 372   --res 2000 --samples 1024 --dist 0.92 --height 0.10 --target-drop 0.12 --out-dir <scratch>/final   --final <port>/9_art/SPIC.png --report <scratch>/final.json
```

What the script does and why (keep these when adapting it):

- **Camera from the posed head bone**: target = head bone position minus `--target-drop` (0.12 m),
  camera = target + (0, -`--dist`, +`--height`) (0.92 m in front, 0.10 m up), aimed with a
  track-to quaternion; 50 mm on a 36 mm sensor frames head-to-chest like the corpus renders.
  `--yaw` orbits the camera if the pose turns the face. The model faces -Y after an MMD Tools import.
- **Overlay shells are deleted before rendering.** MMD models ship `+` materials (`MI_UP+`,
  `bodyDown+`, `Face+`): duplicates of the clothes/skin faces carrying a mostly transparent
  shading texture. In Cycles the coplanar twin z-fights and punches white holes through the chest.
  The script pairs materials whose face centroids coincide (>= 90 %) and deletes the twin named
  `...+` (else the one with the more transparent texture) with bmesh; shape keys survive.
- **Expressions come from the VMD when the model's morph names are Japanese** (MMD Tools keys
  shape keys by the PMX Japanese morph name; the reference dance keys `笑い`, `ウィンク`, `にこり`,
  `なごみ`, `あ`...). Otherwise, or to override, pass `--morph <shape key name>=<0..1>` (Japanese
  names such as `右口角上げ2`, `じと目`, `なごみ`; the report JSON lists every shape key so you can
  check what exists). The frame list above is where the reference dance holds a smile/wink with the
  head near the camera (mined from the VMD morph track, excluding blinks).
- Cycles GPU is picked automatically (OptiX > CUDA > HIP, CPU fallback); 1024 samples with the
  denoiser is indistinguishable from the corpus 2048-4096 at 2000 px and renders in under a minute
  on a modern GPU.
- Output is RGBA on transparent film, exactly like the corpus `SPIC.png`; flatten it on white with
  Pillow for `Release.jpg` and the 512 px `gmpublish` icon.

Quality bar (compare with any corpus `9_art/SPICR.png`): eyes open or a deliberate wink, face
turned within ~20 deg of the camera, hands not covering the face, hair ornament inside the frame,
no holes in the clothes, no lamp hot-spots (there are no lamps), soft toon shading with visible
texture detail.

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

The renderer's output size is the constant pair `scene.render.resolution_x/y = 1024` in
`tools/blender_render_icon.py`; for the corpus 2000×2000 `Release.jpg` run a patched copy of the
script with the same arguments (`--pmx --vmd --frame --output-png --spawn-output-png --report-json`)
under the managed Blender and flatten the RGBA result onto white.

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
