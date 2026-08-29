# Source Engine Character Importer from MMD and VMD to Gmod, L4D2 and SFM
<img width="3840" height="2160" alt="PS1c" src="https://github.com/user-attachments/assets/a7eb4ccd-4369-429c-b620-2450fd1cb335" />

To developers: This repo can be directly ran from the source without downloading the .exe binary in the release. Please refer to the following instruction:

Gmod / SFM Steam Workshop Addon: https://steamcommunity.com/sharedfiles/filedetails/?id=3738916298
L4D2 Steam Addon: https://steamcommunity.com/sharedfiles/filedetails/?id=3748993892

## Requirements

- Windows 10/11, 64-bit (for running from source on Linux, see "Run on Linux" below).
- Python 3.12, 64-bit.
- PowerShell.
- Garry's Mod, L4D2 or SFM installed through Steam for final StudioMDL/gmad compile and
  package steps.

The app manages its own portable Blender 4.5 setup under:

```text
%LOCALAPPDATA%\MMDCharacterImporter
```

VTFCmd and the older VC runtime DLLs needed by VTFCmd/PyOpenGL are included in
`external_tools`.

## One-Time Source Setup

Open a terminal in this repo folder. If your prompt looks like `C:\path>`, you
are using Command Prompt. If it starts with `PS`, you are using PowerShell.

Create the virtual environment first, then activate it as a separate command.
Do not append the activation script path to `python -m venv`.

Command Prompt:

```cmd
python -m venv .venv
.\.venv\Scripts\activate.bat
```

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in that same PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

If your prompt shows both `(.venv)` and `(base)`, deactivate conda before
building to avoid conda/venv detection warnings:

```powershell
conda deactivate
.\.venv\Scripts\Activate.ps1
```

Install runtime/build dependencies after activation:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
```

Download and verify the excluded heavyweight asset:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\download_build_assets.ps1
```

This writes `blender-4.5.10-windows-x64.zip` at repo root. The file is ignored
by git because it is larger than GitHub's normal file-size limit.

To verify an already downloaded asset without downloading again:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\download_build_assets.ps1 -VerifyOnly
```

## Run Without Building

After the one-time source setup, launch the GUI directly from Python:

```powershell
python .\tools\mmd_character_importer_gui.py
```

Optional: verify Blender/add-on setup before launching the GUI:

```powershell
python .\tools\mmd_character_importer_core.py setup
```

The main screen can auto-detect Garry's Mod in common Steam locations. If it
does not, browse to the Garry's Mod install folder or to:

```text
...\GarrysMod\bin\studiomdl.exe
```

## Run on Linux

There is no Linux binary release; on Linux you run the program from source.
Clone the repo and run the launch script:

```bash
git clone https://github.com/SheepyLord/Gmod-Simple-Character-Model-Importer.git
cd Gmod-Simple-Character-Model-Importer
bash run_linux.sh
```

The script creates a `.venv` virtual environment, installs the runtime
dependencies from `requirements.txt`, and starts the GUI. It also checks for
Wine/Proton (used by the final compile/packaging steps, see the limitations
below) and prints install instructions when neither is found. On first run the
app downloads the official Blender 4.5.10 Linux build (~360 MB) from
blender.org and manages it under:

```text
~/.MMDCharacterImporter
```

Requirements: a 64-bit distro with Python 3.11+ (3.12 recommended,
Debian/Ubuntu: `sudo apt install python3 python3-venv`) and the Qt 6 system
libraries (Debian/Ubuntu: `sudo apt install libxcb-cursor0 libxkbcommon-x11-0
libegl1 libgl1`).

Known limitations on Linux:

- The Blender-side steps (import through proportion export) run natively.
- StudioMDL, gmad and VTFCmd are Windows programs, and the Linux builds of
  Garry's Mod / L4D2 do not ship StudioMDL. With Wine installed the app runs
  those tools automatically (launching them through `wine` and translating
  their path arguments to Wine's `Z:` drive). Install the game's WINDOWS build
  through Steam (force it via Proton compatibility) so `bin/studiomdl.exe`
  exists; the app auto-detects Steam installs under `~/.steam`,
  `~/.local/share/Steam` and flatpak Steam, and the `STUDIOMDL` environment
  variable still overrides the location manually.
- Running the Windows .exe release under Wine is not supported; use
  `run_linux.sh` instead.

## Build The Program

Build the default one-file executable:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_mmd_character_importer_exe.ps1 -Python .\.venv\Scripts\python.exe
```

The output is written to `release\GmodSimpleMMDCharacterImporter.exe`.

Build a portable folder instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_mmd_character_importer_exe.ps1 -Python .\.venv\Scripts\python.exe -OneDir
```

Portable output:

```text
release\GmodSimpleMMDCharacterImporter_portable\GmodSimpleMMDCharacterImporter.exe
release\GmodSimpleMMDCharacterImporter_portable\_internal
release\GmodSimpleMMDCharacterImporter_portable\dependency_manifest.json
release\GmodSimpleMMDCharacterImporter_portable\RUN_ME.txt
```

Useful build options:

```powershell
# Change executable name
powershell -ExecutionPolicy Bypass -File .\tools\build_mmd_character_importer_exe.ps1 -Python .\.venv\Scripts\python.exe -Name MyImporter

# Keep console window for debugging
powershell -ExecutionPolicy Bypass -File .\tools\build_mmd_character_importer_exe.ps1 -Python .\.venv\Scripts\python.exe -Console

# Use UPX if installed
powershell -ExecutionPolicy Bypass -File .\tools\build_mmd_character_importer_exe.ps1 -Python .\.venv\Scripts\python.exe -UseUPX
```

## Run A Built Release

After building, launch:

```powershell
.\release\GmodSimpleMMDCharacterImporter.exe
```

For a portable-folder build, keep `_internal` beside the executable and launch:

```powershell
.\release\GmodSimpleMMDCharacterImporter_portable\GmodSimpleMMDCharacterImporter.exe
```
