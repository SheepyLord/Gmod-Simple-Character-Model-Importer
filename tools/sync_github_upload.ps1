# =========================================================================
# WARNING - CONTENT DRIFT RISK
#
# This script regenerates several files in the upload folder from here-string
# copies embedded BELOW, rather than copying the tracked repo files. Those
# embedded copies are NOT kept in sync automatically and some have already
# drifted from the live repo:
#
#   - .gitignore   : the embedded copy ($GitIgnore below) intentionally
#                    differs from the live repo .gitignore (it omits /9_arts,
#                    /Github Upload, the reference/* rules, /smoke_tests and
#                    the *.spec names), but it is not reviewed against the
#                    live file when the live file changes.
#   - README.md    : the embedded upload README ("MMD Character Importer
#                    Build Repo") is maintained separately from the live repo
#                    README.md and does not receive its updates.
#   - requirements-build.txt, build_assets_manifest.json,
#     scripts/download_build_assets.ps1
#                  : embedded mirrors of tracked root files. Whenever the
#                    tracked file changes, the matching here-string below
#                    MUST be updated by hand, or the upload repo ships a
#                    stale copy.
#
# Treat the tracked root files as the source of truth and re-check every
# here-string below before running a sync.
# =========================================================================
param(
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $UploadRoot = Join-Path $ProjectRoot "Github Upload"
}
else {
    $UploadRoot = $OutputDir
}

$ProjectRootFull = [System.IO.Path]::GetFullPath($ProjectRoot)
$UploadRootFull = [System.IO.Path]::GetFullPath($UploadRoot)

function Assert-SafeUploadRoot {
    $DirSeparators = [char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $NormalizedUploadRoot = $UploadRootFull.TrimEnd($DirSeparators)
    $NormalizedProjectRoot = $ProjectRootFull.TrimEnd($DirSeparators)
    if ([System.IO.Path]::GetFileName($NormalizedUploadRoot) -ne "Github Upload") {
        throw "Refusing to sync folder not named 'Github Upload': $UploadRootFull"
    }
    if ($NormalizedProjectRoot.Equals($NormalizedUploadRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $NormalizedProjectRoot.StartsWith($NormalizedUploadRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to sync into a folder that contains the project root: $UploadRootFull"
    }
}

function New-CleanUploadRoot {
    Assert-SafeUploadRoot
    if (Test-Path -LiteralPath $UploadRootFull) {
        Remove-Item -LiteralPath $UploadRootFull -Recurse -Force
    }
    New-Item -ItemType Directory -Path $UploadRootFull | Out-Null
}

function Test-ExcludedUploadPath([string]$RelativePath, [bool]$IsDirectory) {
    $Parts = $RelativePath -split '[\\/]+' | Where-Object { $_ -ne "" }
    $ExcludedDirs = @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache")
    foreach ($Part in $Parts) {
        if ($ExcludedDirs -contains $Part) {
            return $true
        }
    }
    if (-not $IsDirectory) {
        $Name = [System.IO.Path]::GetFileName($RelativePath)
        if ($Name -eq "sync_github_upload.ps1") {
            return $true
        }
        $Extension = [System.IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
        if ($Extension -in @(".pyc", ".pyo")) {
            return $true
        }
    }
    return $false
}

function Get-RelativePathCompat([string]$BasePath, [string]$ChildPath) {
    $BaseFull = [System.IO.Path]::GetFullPath($BasePath).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $ChildFull = [System.IO.Path]::GetFullPath($ChildPath)
    $Prefix = $BaseFull + [System.IO.Path]::DirectorySeparatorChar
    if ($ChildFull.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $ChildFull.Substring($Prefix.Length)
    }
    if ($ChildFull -eq $BaseFull) {
        return ""
    }
    throw "Path is not inside base path: $ChildFull"
}

function Copy-FilteredDirectory([string]$SourceRelative, [string]$DestRelative) {
    $Source = Join-Path $ProjectRoot $SourceRelative
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Required source directory was not found: $SourceRelative"
    }
    $Dest = Join-Path $UploadRootFull $DestRelative
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    $SourceFull = [System.IO.Path]::GetFullPath($Source)
    Get-ChildItem -LiteralPath $SourceFull -Recurse -Force | ForEach-Object {
        $Relative = Get-RelativePathCompat $SourceFull $_.FullName
        if (-not (Test-ExcludedUploadPath $Relative $_.PSIsContainer)) {
            $Target = Join-Path $Dest $Relative
            if ($_.PSIsContainer) {
                New-Item -ItemType Directory -Path $Target -Force | Out-Null
            }
            else {
                $Parent = Split-Path -Parent $Target
                if ($Parent -and -not (Test-Path -LiteralPath $Parent)) {
                    New-Item -ItemType Directory -Path $Parent -Force | Out-Null
                }
                Copy-Item -LiteralPath $_.FullName -Destination $Target -Force
            }
        }
    }
}

function Copy-RequiredFile([string]$SourceRelative, [string]$DestRelative) {
    $Source = Join-Path $ProjectRoot $SourceRelative
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required source file was not found: $SourceRelative"
    }
    $Dest = Join-Path $UploadRootFull $DestRelative
    $Parent = Split-Path -Parent $Dest
    if ($Parent -and -not (Test-Path -LiteralPath $Parent)) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $Source -Destination $Dest -Force
}

function Write-TextFile([string]$RelativePath, [string]$Content) {
    $Path = Join-Path $UploadRootFull $RelativePath
    $Parent = Split-Path -Parent $Path
    if ($Parent -and -not (Test-Path -LiteralPath $Parent)) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}

function Write-JsonFile([string]$RelativePath, [object]$Value) {
    $Path = Join-Path $UploadRootFull $RelativePath
    $Parent = Split-Path -Parent $Path
    if ($Parent -and -not (Test-Path -LiteralPath $Parent)) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 8), $Utf8NoBom)
}

New-CleanUploadRoot

Copy-FilteredDirectory "tools" "tools"
Copy-FilteredDirectory "plugins_software" "plugins_software"
Copy-FilteredDirectory "external_tools" "external_tools"
Copy-FilteredDirectory "reference\ref_motion" "reference\ref_motion"
Copy-FilteredDirectory "reference\proportion_trick_script-main_new\Proportion_Trick\scripts\4.5.10" "reference\proportion_trick_script-main_new\Proportion_Trick\scripts\4.5.10"
Copy-FilteredDirectory "reference\li_zhiyan_npc\a_pack" "reference\li_zhiyan_npc\a_pack"
Copy-FilteredDirectory "reference\!enhanced_animation_importer_arc\tools" "reference\!enhanced_animation_importer_arc\tools"
Copy-FilteredDirectory "reference\dynamic_model_importer" "reference\dynamic_model_importer"

Copy-RequiredFile "steps.txt" "steps.txt"
Copy-RequiredFile "Translation Templates Write.txt" "Translation Templates Write.txt"
Copy-RequiredFile "README.md" "docs\MMDCharacterImporter_README.md"
Copy-RequiredFile ".gitattributes" ".gitattributes"
Copy-RequiredFile "run_linux.sh" "run_linux.sh"
Copy-RequiredFile "requirements.txt" "requirements.txt"
Copy-RequiredFile "reference\proportion_trick_script-main_new\README.md" "reference\proportion_trick_script-main_new\README.md"
Copy-RequiredFile "reference\proportion_trick_script-main_new\operator_proportion_trick.py" "reference\proportion_trick_script-main_new\operator_proportion_trick.py"
Copy-RequiredFile "reference\proportion_trick_script-main_new\Proportion_Trick\README.md" "reference\proportion_trick_script-main_new\Proportion_Trick\README.md"
Copy-RequiredFile "reference\proportion_trick_script-main_new\Proportion_Trick\proportion_trick_4.5.10.blend" "reference\proportion_trick_script-main_new\Proportion_Trick\proportion_trick_4.5.10.blend"
Copy-RequiredFile "reference\li_zhiyan_npc\3_Flexes\Blender_p3.py" "reference\li_zhiyan_npc\3_Flexes\Blender_p3.py"

$Requirements = @"
pyinstaller==6.20.0
PySide6==6.11.0
numpy==2.4.4
Pillow==12.1.1
requests==2.32.4
PyOpenGL==3.1.10
"@
Write-TextFile "requirements-build.txt" $Requirements

$GitIgnore = @"
build/
dist/
release/
.venv/
venv/
env/
__pycache__/
*.py[cod]
*.pyo
*.log
.pytest_cache/
.mypy_cache/
.ruff_cache/
.cache/
blender-4.5.10-windows-x64.zip
blender-4.5.10-linux-x64.tar.xz
"@
Write-TextFile ".gitignore" $GitIgnore

$GitAttributes = @"
* text=auto

*.sh text eol=lf

*.dll binary
*.exe binary
*.zip binary
*.blend binary
*.vmd binary
*.tar.xz binary
"@
Write-TextFile ".gitattributes" $GitAttributes

$AssetManifest = [ordered]@{
    schema_version = 1
    assets = @(
        [ordered]@{
            file_name = "blender-4.5.10-windows-x64.zip"
            url = "https://download.blender.org/release/Blender4.5/blender-4.5.10-windows-x64.zip"
            sha256 = "EF6D846B8015F47ADE6DF3F9322CE17419080A5D922FA562B6C966064FE30DCE"
            size_bytes = 398911842
            reason = "Optional offline-fallback portable Blender zip. When present at the repo root, tools/build_mmd_character_importer_exe.ps1 verifies it against this manifest and bundles it into the exe; when absent, the build still succeeds and the app auto-downloads the official Blender 4.5 zip at first run."
        }
    )
}
Write-JsonFile "build_assets_manifest.json" $AssetManifest

$DownloadScript = @'
param(
    [switch]$VerifyOnly,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ManifestPath = Join-Path $RepoRoot "build_assets_manifest.json"
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "build_assets_manifest.json was not found: $ManifestPath"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
# Allow TLS 1.2 and, when the runtime supports it, TLS 1.3 (older .NET
# Framework builds do not define the Tls13 enum value).
$SecurityProtocols = [Net.SecurityProtocolType]::Tls12
try {
    $SecurityProtocols = $SecurityProtocols -bor [Net.SecurityProtocolType]::Tls13
}
catch {
}
[Net.ServicePointManager]::SecurityProtocol = $SecurityProtocols

function Test-AssetHash([string]$Path, [string]$ExpectedHash) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $ActualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    return $ActualHash -eq $ExpectedHash.ToUpperInvariant()
}

foreach ($Asset in $Manifest.assets) {
    $Target = Join-Path $RepoRoot $Asset.file_name
    $ExpectedHash = [string]$Asset.sha256
    $ExpectedSize = [int64]$Asset.size_bytes
    $Url = [string]$Asset.url

    if ((Test-Path -LiteralPath $Target -PathType Leaf) -and -not $Force) {
        $Item = Get-Item -LiteralPath $Target
        if ($Item.Length -eq $ExpectedSize -and (Test-AssetHash $Target $ExpectedHash)) {
            Write-Host "Verified existing asset: $($Asset.file_name)"
            continue
        }
        if ($VerifyOnly) {
            throw "Asset exists but failed size/hash validation: $($Asset.file_name)"
        }
        Write-Warning "Existing asset failed validation and will be replaced: $($Asset.file_name)"
    }
    elseif ($VerifyOnly) {
        throw "Missing required build asset: $($Asset.file_name)"
    }

    $TempTarget = "$Target.download"
    if (Test-Path -LiteralPath $TempTarget) {
        Remove-Item -LiteralPath $TempTarget -Force
    }
    Write-Host "Downloading $Url"
    # Suppress the per-chunk progress bar: under Windows PowerShell 5.1 it
    # slows large downloads by an order of magnitude. -UseBasicParsing keeps
    # 5.1 working without the IE engine (it is a harmless no-op on PowerShell 7).
    $PreviousProgressPreference = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $TempTarget -UseBasicParsing
    }
    finally {
        $ProgressPreference = $PreviousProgressPreference
    }
    $Item = Get-Item -LiteralPath $TempTarget
    if ($Item.Length -ne $ExpectedSize) {
        Remove-Item -LiteralPath $TempTarget -Force
        throw "Downloaded asset size mismatch for $($Asset.file_name): $($Item.Length) != $ExpectedSize"
    }
    if (-not (Test-AssetHash $TempTarget $ExpectedHash)) {
        Remove-Item -LiteralPath $TempTarget -Force
        throw "Downloaded asset hash mismatch for $($Asset.file_name)"
    }
    Move-Item -LiteralPath $TempTarget -Destination $Target -Force
    Write-Host "Downloaded and verified: $($Asset.file_name)"
}
'@
Write-TextFile "scripts\download_build_assets.ps1" $DownloadScript

$UploadReadme = @'
# MMD Character Importer Build Repo

This repository is the GitHub-uploadable source/build package for MMD Character
Importer. It contains the source files, small vendored tools/plugins, required
reference subsets, and scripts needed to run from source or build the Windows
executable.

Large generated outputs and the large Blender fallback zip are intentionally
excluded from git. Download the heavyweight build asset before running or
building.

## Requirements

- Windows 10/11, 64-bit.
- Python 3.12, 64-bit.
- PowerShell.
- Garry's Mod installed through Steam for final StudioMDL/gmad compile and
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
blender.org and manages it under `~/.MMDCharacterImporter`.

Requirements: a 64-bit distro with Python 3.11+ (3.12 recommended,
Debian/Ubuntu: `sudo apt install python3 python3-venv`) and the Qt 6 system
libraries (Debian/Ubuntu: `sudo apt install libxcb-cursor0 libxkbcommon-x11-0
libegl1 libgl1`).

Known limitations on Linux:

- The Blender-side steps (import through proportion export) run natively.
- StudioMDL, gmad and VTFCmd are Windows programs, and the Linux builds of
  Garry's Mod / L4D2 do not ship StudioMDL. The final texture-conversion and
  compile/packaging steps therefore need those tools from a Windows install of
  the game (for example via Proton/Wine, using the `STUDIOMDL` and `VTFCMD`
  environment variables to point at them), or finish those steps on Windows.
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

## Repository Maintenance

- `blender-4.5.10-windows-x64.zip` is required by the build script but is excluded from git because it is larger than GitHub's normal file limit.
- `external_tools\vtfcmd` and the required VC runtime DLLs are included directly because they are needed for icon and VTF generation.
- Garry's Mod is still required on the machine that runs the importer because StudioMDL and gmad are distributed with Garry's Mod.
- The source project updates this folder by running `tools\sync_github_upload.ps1`; do not manually copy files when refreshing this repo.
- Generated `build`, `dist`, and `release` folders are ignored by git.

The original project README is copied to `docs\MMDCharacterImporter_README.md`.
'@
Write-TextFile "README.md" $UploadReadme

Write-Host "Github Upload sync complete: $UploadRootFull"
