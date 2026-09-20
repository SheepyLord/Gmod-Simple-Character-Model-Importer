# 01 — Naming, categories and metadata

Naming decides where the model shows up in Garry's Mod (spawn-menu category, NPC list, playermodel
list), what its files are called on disk (the `$modelname` path, materials folder, vgui icons), and
how the Workshop entry reads. Get it right **before** Step 14, because everything downstream
(compiled paths, lua registration, addon.json, icons) derives from these strings.

The rules below are distilled from ~390 released ports in the maintainer's corpus.

## The five identifiers

| Identifier | Plan key (Step 14 `qc_plan.json`) | Rule | Examples |
| --- | --- | --- | --- |
| Author slug | `author` | lowercase, `[a-z0-9_]`, no spaces; the porter's handle | `sheepylord` |
| Category slug | `character_category` | lowercase snake_case of the **game/franchise** title | `genshin_impact`, `honkai_star_rail`, `wuthering_waves`, `zenless_zone_zero`, `arknights_endfield`, `blue_archive`, `honkai_impact`, `vocaloid`, `touhou`, `azur_lane`, `virtual_youtuber` |
| Category (readable) | `category_readable` | the official English title with its punctuation | `Genshin Impact`, `Honkai: Star Rail`, `Wuthering Waves`, `Zenless Zone Zero`, `Arknights Endfield`, `Blue Archive`, `Neverness to Everness` |
| Model slug | `model_name` | lowercase snake_case of the character's romanised name; `[a-z0-9_]` only | `hu_tao`, `kamisato_ayaka`, `bronya_rand`, `chen_qianyu`, `camellya` |
| Display name | `display_name` | full readable name, Title Case, as the game spells it | `Kuchiba Chisa`, `Raiden Bosenmori Mei Acheron` |

Derived paths (do not invent others — the lua, VMTs and icons all assume them):

```
models/<author>/<category>/<model>.mdl          NPC / ragdoll model
models/<author>/<category>/<model>_pm.mdl       playermodel variant (GMod only)
models/<author>/<category>/<model>_arms.mdl     first-person c_arms (GMod only)
materials/models/<author>/<model>/<material>    every material VTF/VMT
materials/models/<author>/shared/               lightwarp / phong_exp / normal shared textures
materials/vgui/entities/<model>_<author>_F      Friendly NPC spawn icon
materials/vgui/entities/<model>_<author>_E      Enemy NPC spawn icon
lua/autorun/<model>_<author>.lua                registration (playermodel, hands, 2 NPCs)
```

Spawn-menu placement (written into the lua by Step 14): `Category = "<category_readable>"`; NPC
entries `"<Display Name> (Friendly)"` (npc_citizen, citizentype 4, weapon_smg1) and
`"<Display Name> (Enemy)"` (npc_combine_s, weapon_ar2), list keys `<model>_<author>_F` / `_E`.
Playermodel key `player_manager.AddValidModel("<Display Name>", ...)` with `AddValidHands` pointing at
the `_arms` model.

## Disambiguation rules

- **Alternate outfits of an already-released character:** model slug gets `_alt`, `_alt2`, … and the
  readable category becomes `"<Game> Outfit Alternatives"` (slug unchanged). Examples from the
  corpus: `carlotta_alt`, `castorice_alt2`, `cyrene_alt`; categories `Genshin Impact Outfit
  Alternatives`, `Neverness To Everness Outfit Alternatives`.
- **Generic or colliding names** (`cipher`, `pearl`, `alice`): prefix the game (`honkai_star_rail_cipher`)
  or suffix the author (`arona_sheepylord`). A slug must be unique across every addon the author
  has released, because the compiled `.mdl` path is global in the game.
- **Characters whose real name differs from the alias** keep the alias players search for as the
  display name and put the full name in the description (`trbm_acheron` displays as
  `Raiden Bosenmori Mei Acheron`; players know "Acheron").
- **L4D2 survivors** ignore all of this for the model path: the game requires exactly
  `survivors/survivor_<slot>.mdl` (slot = producer/coach/gambler/mechanic/namvet/teenangst/biker/manager)
  and the tool writes it; `character_category`/`model_name` still name the materials folder and the
  VPK. Pick the slot by body type/voice you want replaced, not by name.

## Workshop metadata (Step 15 + upload)

- Workshop title convention: `"<Category readable> - <Display Name> (PM & NPCs)"`; localised
  copies prepend the localised name and keep the English in parentheses.
- `addon.json` written by Step 14: `{"title": "<model>_public_version", "type": "model", "tags":
  ["cartoon", "fun"]}` — edit the title to the Workshop title above before `gmad`/`gmpublish` if you
  are publishing, keep the tags (GMod requires one of its fixed tag set; `cartoon` + `fun` is the
  corpus standard for anime characters).
- Description skeleton (Step 15 generates all 12 languages from `tools/sort_release_description.py`):
  one-paragraph character description → optional quote → features list → credits (model creator,
  porter) → generated-content disclosure. Keep the feature bullets truthful: only claim
  "Jigglebones", "Ragdoll physics for hair/clothes/skirt", "First person view model (c_arms)",
  "Adjustable Bodygroups", "Faceposing (includes eyes)" when the port actually has them.
- Credits must name the original model creator (from the PMX readme) — see
  [`resources/examples/`](resources/examples/) for the corpus lua and QC that show every path in place.

## Where to set these in a headless run

`core.analyze_qc(input_path=<step9 export dir>, author=..., character_category=..., model_name=...,
gmod_root=..., studiomdl_path=..., gender="female"|"male", game="gmod"|"l4d2"|"sfm", survivor=...)`
writes `14_sort_qc_compile/qc_plan.json`; `display_name` and `category_readable` are plan keys you
can set directly in that JSON before compiling. Validate with `python -c "import re; ..."`-style checks:
slugs must match `^[a-z0-9_]+$`, readable names must be non-empty, and `model_name` must not
collide with a previously released slug by the same author.
