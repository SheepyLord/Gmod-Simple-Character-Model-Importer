# Resources

Reference assets and real decision records from the maintainer's release corpus. They are data for
the guides, not inputs the tool reads automatically.

| File | What it is | Used by |
| --- | --- | --- |
| `reference_physics_18body.smd` | A proven ragdoll physics mesh: exactly the 18 ValveBiped bodies (2 628 vertices), each hull skinned 100 % to its bone, Source scale (~5'5" female; from the released *Durin* port). Import into a Step 8 blend and fit per bone when generated hulls are unusable. | [05 Physics](../05_physics_ragdoll.md) |
| `reference_collision_block.qc` | The `$collisionjoints … $collisiontext` block of a human-compiled port: the 18-body joint limits, mass/damping globals. Identical to the tool's `physics_settings.json` defaults. | [05](../05_physics_ragdoll.md), [08](../08_compile_bodygroups_jiggles.md) |
| `examples/reference_compile_durin.qc` | A complete human-made GMod QC (bodygroups, flexes, definebones, hitboxes, 127 jigglebones, IK, proportion delta, includemodels, collision). The structure Step 14 reproduces. | [08](../08_compile_bodygroups_jiggles.md), [09](../09_debugging_and_testing.md) |
| `examples/bone_list_acheron.txt` | Bones a human porter kept on a heavy-skirt character (skirt columns thinned by alternation, full hair chains, breasts). | [02 Bones](../02_bone_merging.md) |
| `examples/bone_list_ignore_acheron.txt` | The kept bones demoted to light "omni" jiggles (decorations, sleeves). | [02](../02_bone_merging.md), [08](../08_compile_bodygroups_jiggles.md) |
| `examples/phys_rec_acheron.txt`, `examples/phys_rec_camellya.txt` | `Class:Bone` records of the extra ragdoll bodies (front/back/side skirt, back hair, free hair) — the human form of the tool's `additional_bone_groups` / `rotation_type`. | [05](../05_physics_ragdoll.md) |

Scale of the physics template: Source units (the model was already scaled ×40.457 from MMD units
when the mesh was made). If your character is much taller/shorter, scale the template uniformly by
the ratio of pelvis heights first, then fit per bone.

Not included on purpose: character art (PSD/JPEG renders and screenshots are copyrighted game
assets) and the human notebook (it embeds API keys). The equivalent logic lives in
`tools/sort_release_description.py` (Step 15) and `tools/sort_qc_compile.py` (Step 14).
