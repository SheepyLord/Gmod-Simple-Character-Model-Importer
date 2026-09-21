---
name: scmi-port
description: Port an MMD (PMX) or VRM character to Garry's Mod, Left 4 Dead 2 or Source Filmmaker with this repository's pipeline, at release quality. Use when asked to port/convert a model, to fix a port's bones, materials, bodygroups, physics/ragdoll, VRD skirt helpers, icons, jigglebones or naming, or to debug a compile. Drives the tools headlessly from source (never the .exe) following agent_guide/.
---

# SCMI port skill

You are working in a clone of Gmod-Simple-Character-Model-Importer. Drive the pipeline from source
with the Python API in `tools/mmd_character_importer_core.py`; do not automate the packaged
executable.

## Procedure

1. Read `AGENTS.md`, then `agent_guide/README.md` and `agent_guide/00_setup_and_headless_driving.md`.
   Provision Blender once (`python tools/mmd_character_importer_core.py setup`) and confirm the
   game tooling (`studiomdl.exe`) is detected or set via `STUDIOMDL`.
2. Fix the identifiers first (`agent_guide/01_naming_and_metadata.md`): author, category slug and
   readable name, model slug, display name. Check the slug is unique for the author.
3. Run Steps 1–3 from the driver in guide 00. Then for each of Steps 4–8 and 11–14:
   run the analyze half, open the plan JSON, apply the rules of the matching guide
   (02 bones, 03 materials, 04 bodygroups/flexes, 05 physics, 06 VRD, 07 icons, 08 compile),
   edit the plan, run the apply half, and read the report. Stop on `validation.ok == false`.
4. Render the Workshop portrait the corpus way (`agent_guide/07_icons_and_workshop_art.md`,
   `agent_guide/resources/render_spic_cycles.py`): contact sheet of dance frames, pick one, final
   2000 px Cycles render to `9_art/SPIC.png`; the Step 13 spawn icons alone are not release art.
5. After Step 14, verify with `agent_guide/09_debugging_and_testing.md`: reports, HLMV, then the
   in-game checklist. Report what you could and could not verify.
6. Only modify code in `tools/` for a defect that reproduces across models; keep default output
   byte-identical and gate new behaviour.

## Hard rules

- Never commit or print API keys; Step 15 works offline without them.
- Keep workspace paths short on Windows; never delete a workspace you may need to redo a step from.
- Every kept non-essential bone must end up as a jiggle (or physics body) with a chain-consistent
  type; every hideable bodygroup must leave a complete surface underneath.
- The 18-body ragdoll parameters and the corpus jiggle parameters are not tuning targets — only
  hull shapes and jiggle types/directions change per model.
