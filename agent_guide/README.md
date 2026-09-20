# Agent guide — porting MMD/VRM characters to Source with SCMI

This folder teaches an AI agent (or a new human contributor) how to produce **release-quality**
Garry's Mod / L4D2 / SFM ports with this repository, the way the maintainer's several hundred
released ports were made. The tool automates the pipeline; the quality comes from the decisions
taken between its analyze and apply halves. Each guide below documents one decision area: what the
tool proposes, the plan-file schema you edit, the rules a human porter applies, and how to verify.

Agents: clone the repository and work from source (see [`../AGENTS.md`](../AGENTS.md)). Do not
automate the packaged executable.

## Reading order

| # | Guide | Covers |
| --- | --- | --- |
| 00 | [Setup and headless driving](00_setup_and_headless_driving.md) | environment, workspace layout, the analyze → edit plan → apply loop, a complete Steps 1–15 driver, Blender probes |
| 01 | [Naming and metadata](01_naming_and_metadata.md) | author/category/model slugs, display names, derived paths, alt outfits, Workshop title/description |
| 02 | [Bone merging](02_bone_merging.md) | which bones to keep for physics/jiggle, which to merge, chain thinning, 254/126 budgets |
| 03 | [Materials and textures](03_materials_and_textures.md) | keep/drop/combine rules, 32-material limit, texture schemes, VMT knobs |
| 04 | [Bodygroups, layers and flexes](04_bodygroups_layers_and_flexes.md) | naming, hideability, detachable clothes over incomplete bodies, accessories, facial merge, flex naming |
| 05 | [Physics ragdoll](05_physics_ragdoll.md) | the 18-body model and joint table, CoACD hull knobs, hair/skirt bodies (phys_rec classes), fitting the reference template |
| 06 | [VRD procedural bones](06_vrd_procedural_bones.md) | selecting skirt helpers, confidence/side/rear rules, trigger budget, validation |
| 07 | [Icons and Workshop art](07_icons_and_workshop_art.md) | render pipeline, choosing a VMD/frame, custom images, spawn icons, Workshop icon sizes |
| 08 | [Compile: bodygroups, jiggles, packaging](08_compile_bodygroups_jiggles.md) | qc_plan schema, jiggle types and direction rules, hitbox groups, compile passes, game notes |
| 09 | [Debugging and testing](09_debugging_and_testing.md) | logs/reports, error catalogue, HLMV/Crowbar checks, in-game checklist |
| — | [resources/](resources/README.md) | reference 18-body physics mesh, human QC/collision blocks, real decision records |

## The quality bar

A finished port (what every released corpus addon advertises, and what the in-game checklist in
[09](09_debugging_and_testing.md) verifies):

- Faceposing including eyes; fingerposing; correct hitboxes with HL2 hit groups.
- Jigglebones on hair, clothes, skirt; ragdoll physics bodies for the same where it matters;
  realistic ragdoll joint limits (the 18-body model).
- Adjustable bodygroups for every removable part; no holes when a part is hidden.
- First-person c_arms; Friendly and Enemy NPCs under the game's category; playermodel.
- Clean textures (no placeholders, correct alpha), a rendered Workshop icon and spawn icons,
  a multilingual description crediting the model creator.

## The core loop, once more

```
for each step:
    analyze -> inspect *_analysis.json / *_plan.json
    apply the step guide's rules -> edit the plan
    apply -> read *_report.json (validation.ok / errors / warnings)
    redo from the previous step's blend if needed
finally: compile, then test in HLMV and in game before publishing
```

Reference data in this folder was collected from the maintainer's release corpus (human-made and
tool-assisted ports, 2025–2026). Where a guide quotes numbers (joint limits, jiggle parameters,
scale factor, limits), the same numbers are the tool's defaults in `tools/`.
