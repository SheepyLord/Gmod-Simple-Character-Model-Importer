# AGENTS.md — how an AI agent should use this repository

This repository is **Gmod Simple Character Model Importer (SCMI)**: a Windows PySide6 app plus
headless-Blender step scripts that port MMD (PMX) / VRM characters to Source engine models for
Garry's Mod, Left 4 Dead 2 and Source Filmmaker.

**If you are an AI agent asked to port a model with this project: clone the repository and drive
the pipeline from source. Do not try to automate the packaged `.exe`.** The executable is a GUI
wrapper around the same `tools/` scripts; from source you get a Python API for every step, editable
plan files between the analyze and apply halves of each step, JSON reports you can validate, and
the ability to run Blender inspection scripts on every intermediate `.blend`.

## Start here

1. Read [`agent_guide/README.md`](agent_guide/README.md) — the index and the core working loop.
2. Set up and learn the headless API: [`agent_guide/00_setup_and_headless_driving.md`](agent_guide/00_setup_and_headless_driving.md).
3. Before each step, read that step's guide (naming, bone merging, materials, bodygroups, physics,
   VRD, icons, compile). They encode the decisions a human porter makes and the conventions of a
   corpus of several hundred released, human-reviewed ports.
4. Test and debug with [`agent_guide/09_debugging_and_testing.md`](agent_guide/09_debugging_and_testing.md).

## Quickstart

```bash
git clone https://github.com/SheepyLord/Gmod-Simple-Character-Model-Importer.git
cd Gmod-Simple-Character-Model-Importer
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt     # Windows
python tools/mmd_character_importer_core.py setup           # downloads/verifies the managed Blender 4.5.10 + add-ons
```

On Linux use `bash run_linux.sh` once (it creates the venv and installs everything), then import
`tools/mmd_character_importer_core.py` from that venv exactly as on Windows. StudioMDL/gmad/VTFCmd
are Windows programs; on Linux they run through Wine automatically (see the README).

Every pipeline step is a function in `tools/mmd_character_importer_core.py`
(`import_pmx_to_blender`, `fix_imported_blend`, `analyze_*` / `apply_*` / `sort_*` pairs, …). A complete
driver script that runs Steps 1–15 exactly like the GUI's one-click port is in
[`agent_guide/00_setup_and_headless_driving.md`](agent_guide/00_setup_and_headless_driving.md).

## Rules of engagement

- **Fix the port, not the tool.** Quality problems are solved by editing the step's plan JSON
  (bone merge operations, material keep/combine, bodygroup grouping, physics groups, VRD rows,
  jiggle rows, names) and re-running the apply half. Only change code in `tools/` when the
  behaviour is a genuine bug that affects every model — and then keep the default behaviour
  byte-identical (new behaviour must be gated and default to the current behaviour).
- **Read the report after every step.** Each step writes `<step>_report.json` (plus
  `step_complete.json`). A `validation.ok == false`, an `errors` list, or new `warnings` means the
  step needs attention before you continue. Step 14 additionally leaves the full studiomdl logs.
- **Never guess a schema.** The plan/report file formats are documented in the step guides and,
  authoritatively, in the JSON files the analyze half writes. Open them before editing.
- **Use the workspace.** All intermediate data lives under
  `%LOCALAPPDATA%\MMDCharacterImporter\workspaces\<Model>_<hash>\` (Linux: `~/.MMDCharacterImporter/`).
  Keep it; every step can be re-run from its predecessor's output blend.
- **No secrets, no external services.** Step 15 can call translation/LLM APIs if keys are supplied;
  never hard-code keys, never commit them, and produce the description offline when in doubt.
- **Respect the licences of the source models.** Only port models whose readme permits it, keep
  the model creator credited (the release description does this), and do not ship PSD/source art
  from the corpus.
