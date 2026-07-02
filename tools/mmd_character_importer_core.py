#!/usr/bin/env python3
"""Core helpers for the MMD Character Importer step workflow."""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import html.parser
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


def bundled_resource_root() -> Path:
    """Return the project resource root in source and PyInstaller builds."""

    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
    return Path(__file__).resolve().parents[1]


ROOT = bundled_resource_root()
TOOLS_DIR = ROOT / "tools"
PLUGIN_DIR = ROOT / "plugins_software"
BUNDLED_BLENDER_ZIP = ROOT / "blender-4.5.10-windows-x64.zip"
CATS_ADDON_ZIP = PLUGIN_DIR / "Cats-Blender-Plugin-Unofficial4.5.3.2.zip"
SOURCE_TOOLS_ZIP = PLUGIN_DIR / "blender_source_tools_3.4.3.zip"
BONES_MERGER_ZIP = PLUGIN_DIR / "blender_bones_merger.zip"
MATERIAL_COMBINER_ZIP = PLUGIN_DIR / "material-combiner-addon-master.zip"
COACD_ADDON_ZIP = PLUGIN_DIR / "coacd_blender_addon_1_0_45.zip"
L4D2_TOOLS_ZIP = PLUGIN_DIR / "Blender_L4D2_Character_Tools-main.zip"
BLENDER_SETUP_SCRIPT = TOOLS_DIR / "blender_setup_addons.py"
SETUP_REQUIREMENTS_VERSION = 8
BLENDER_IMPORT_SCRIPT = TOOLS_DIR / "blender_import_mmd_model.py"
BLENDER_VRM_IMPORT_SCRIPT = TOOLS_DIR / "blender_import_vrm_model.py"
BLENDER_FIX_SCRIPT = TOOLS_DIR / "blender_fix_mmd_model.py"
BLENDER_SPINE_SCRIPT = TOOLS_DIR / "blender_fix_spine_bones.py"
BLENDER_SORT_BONES_SCRIPT = TOOLS_DIR / "blender_sort_bones.py"
BLENDER_SORT_MATERIALS_SCRIPT = TOOLS_DIR / "blender_sort_materials.py"
BLENDER_SORT_BODYGROUPS_SCRIPT = TOOLS_DIR / "blender_sort_bodygroups.py"
BLENDER_SORT_FLEXES_SCRIPT = TOOLS_DIR / "blender_sort_flexes.py"
BLENDER_SORT_COLLISION_SCRIPT = TOOLS_DIR / "blender_sort_collision.py"
BLENDER_PROPORTION_SCRIPT = TOOLS_DIR / "blender_export_proportion_trick.py"
BLENDER_DECIMATE_L4D2_SCRIPT = TOOLS_DIR / "blender_decimate_l4d2.py"
BLENDER_CARMS_SCRIPT = TOOLS_DIR / "blender_sort_carms.py"
BLENDER_VRD_SCRIPT = TOOLS_DIR / "blender_sort_vrd.py"
TEXTURE_PROCESSOR_SCRIPT = TOOLS_DIR / "sort_param_textures.py"
BLENDER_ICON_SCRIPT = TOOLS_DIR / "blender_render_icon.py"
ICON_PROCESSOR_SCRIPT = TOOLS_DIR / "sort_icons_and_arts.py"
QC_PROCESSOR_SCRIPT = TOOLS_DIR / "sort_qc_compile.py"
RELEASE_PROCESSOR_SCRIPT = TOOLS_DIR / "sort_release_description.py"
# Per-survivor proportion reference skeletons (one .smd per survivor). Each is the
# survivor's bind pose expressed in the proportion-trick TEMPLATE convention (the same
# convention as proportions.smd) with ONLY the essential ValveBiped bones — generated
# offline by tools/build_l4d2_reference_skeletons.py from the base-game survivor .mdls.
# In L4D2 mode Step 9 swaps the selected survivor's .smd in as the proportion-trick
# reference so the subtract delta aligns the MMD body to that survivor without the
# convention mismatch that distorted the earlier raw-.mdl reference.
L4D2_REFERENCE_SKELETON_DIR = TOOLS_DIR / "l4d2_reference_skeletons"
# Survivor code (compiled .mdl stem) -> in-game character display name. The 4 L4D2
# survivors plus the 4 returning L4D1 survivors.
L4D2_SURVIVORS = {
    "producer": "Rochelle",
    "coach": "Coach",
    "gambler": "Nick",
    "mechanic": "Ellis",
    "namvet": "Bill",
    "teenangst": "Zoey",
    "biker": "Francis",
    "manager": "Louis",
}
DEFAULT_L4D2_SURVIVOR = "producer"
DEFAULT_ICON_VMD = ROOT / "reference" / "ref_motion" / "bad_bad_water.vmd"
WARNING_KEYWORDS_PATH = ROOT / "reference" / "keywords" / "Warning_Keyword.txt"
BLENDER_LTS_INDEX_URL = "https://download.blender.org/release/Blender4.5/"
# Pinned official Blender 4.5.10 zip used by the "without Blender" build's first-run download
# prompt. sha256/size mirror build_assets_manifest.json (the same file the build script verifies the
# bundled zip against), so a downloaded or browsed zip can be checksum-validated at runtime.
# NOTE: use the direct download.blender.org mirror, NOT www.blender.org/download/... -- the latter
# serves an HTML landing page (text/html), not the zip, so it would fail the checksum.
BLENDER_DOWNLOAD_URL = "https://download.blender.org/release/Blender4.5/blender-4.5.10-windows-x64.zip"
EXPECTED_BLENDER_ZIP_NAME = "blender-4.5.10-windows-x64.zip"
EXPECTED_BLENDER_SHA256 = "ef6d846b8015f47ade6df3f9322ce17419080a5d922fa562b6c966064fe30dce"
EXPECTED_BLENDER_SIZE_BYTES = 398911842
APP_DIR_NAME = "MMDCharacterImporter"
ProgressCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]
DEFAULT_BODYGROUP_SCALE_FACTOR = 40.457
DEFAULT_BODYGROUP_VERTEX_LIMIT = 65535
RTX_BODYGROUP_VERTEX_LIMIT = 32767
MAX_SUPPORTED_PMX_VERTEX_COUNT = 300_000
HIGH_PMX_VERTEX_WARNING_THRESHOLD = 100_000
CONTENT_WARNING_KEYWORD_THRESHOLD = 5
BODYGROUP_SCALE_PRESETS: dict[str, Path] = {
    "tall": Path("E:/G/Upload/acheron/5_propo/Face.smd"),
    "normal": Path("E:/G/Upload/firefly/5_propo/Face.smd"),
    "short": Path("E:/G/Upload/yaoyao_alt/5_propo/Face.smd"),
}
BODYGROUP_SCALE_PRESET_TOPS: dict[str, float] = {
    "tall": 69.611259,
    "normal": 63.07148,
    "short": 50.685642,
}
STEP_COMPLETE_MARKER = "step_complete.json"
STEP_MARKER_SCHEMA_VERSION = 1
SYSTEM_BLENDER_WARNING_CHECKED = False
WINDOWS_STATUS_DLL_INIT_FAILED_CODES = {0xC0000142, -1073741502}
MISSING_OUTPUT_RE = re.compile(r"Blender(?: render)? completed but did not write (?P<path>[^\r\n]+)")


def resolve_icon_vmd_path(raw: object) -> Path:
    """Resolve a body-VMD path, healing stale bundled-resource locations.

    PyInstaller onefile builds expose DEFAULT_ICON_VMD under a per-process
    %TEMP%\\_MEIxxxxxx folder, and icon plans/settings written by an earlier
    run may have captured that absolute path. When the stored path no longer
    exists but clearly pointed at the bundled reference motion, fall back to
    the current default instead of failing.
    """
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_ICON_VMD
    path = Path(text)
    if path.exists():
        return path
    lower = text.lower()
    if path.name.lower() == DEFAULT_ICON_VMD.name.lower() or "\\_mei" in lower or "/_mei" in lower:
        return DEFAULT_ICON_VMD
    return path


TEXTURE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".dds", ".spa", ".sph"}

HUMANOID_SKELETON_GROUPS: dict[str, tuple[str, ...]] = {
    "center/root": (
        "\u30bb\u30f3\u30bf\u30fc",
        "center",
        "\u5168\u3066\u306e\u89aa",
        "\u5168\u3066\u306e\u89aa\u9aa8",
        "motherbone",
        "mother",
    ),
    "hips/lower body": (
        "\u4e0b\u534a\u8eab",
        "lower body",
        "lowerbody",
        "hips",
        "pelvis",
    ),
    "upper body": (
        "\u4e0a\u534a\u8eab",
        "\u4e0a\u534a\u8eab1",
        "upper body",
        "upperbody",
        "spine",
    ),
    "neck/head": (
        "\u9996",
        "\u982d",
        "\u5934",
        "neck",
        "head",
    ),
    "arms": (
        "\u5de6\u8155",
        "\u53f3\u8155",
        "\u5de6\u3072\u3058",
        "\u53f3\u3072\u3058",
        "\u5de6\u624b\u9996",
        "\u53f3\u624b\u9996",
        "left arm",
        "right arm",
        "left elbow",
        "right elbow",
        "left wrist",
        "right wrist",
    ),
    "legs": (
        "\u5de6\u8db3",
        "\u53f3\u8db3",
        "\u5de6\u3072\u3056",
        "\u53f3\u3072\u3056",
        "\u5de6\u8db3\u9996",
        "\u53f3\u8db3\u9996",
        "left leg",
        "right leg",
        "left knee",
        "right knee",
        "left ankle",
        "right ankle",
    ),
}

REQUIRED_MMD_SKELETON_BONES: tuple[tuple[str, str], ...] = (
    ("\u5168\u3066\u306e\u89aa", "mother"),
    ("\u30b0\u30eb\u30fc\u30d6", "groove"),
    ("\u30bb\u30f3\u30bf\u30fc", "center"),
    ("\u4e0a\u534a\u8eab", "upper body"),
    ("\u9996", "neck"),
    ("\u982d", "head"),
    ("\u4e0b\u534a\u8eab", "lower body"),
    ("\u5de6\u80a9", "shoulder L"),
    ("\u5de6\u8155", "arm L"),
    ("\u5de6\u3072\u3058", "elbow L"),
    ("\u5de6\u624b\u9996", "wrist L"),
    ("\u5de6\u3072\u3056", "kneeL"),
    ("\u5de6\u8db3\u9996", "ankleL"),
    ("\u53f3\u80a9", "shoulderR"),
    ("\u53f3\u8155", "armR"),
    ("\u53f3\u3072\u3058", "elbowR"),
    ("\u53f3\u624b\u9996", "wristR"),
    ("\u53f3\u8db3", "legR"),
    ("\u53f3\u3072\u3056", "kneeR"),
    ("\u53f3\u8db3\u9996", "ankleR"),
)


VRM_REQUIRED_HUMANOID_BONES: tuple[str, ...] = (
    "hips",
    "spine",
    "head",
    "leftUpperArm",
    "leftLowerArm",
    "leftHand",
    "leftUpperLeg",
    "leftLowerLeg",
    "leftFoot",
    "rightUpperArm",
    "rightLowerArm",
    "rightHand",
    "rightUpperLeg",
    "rightLowerLeg",
    "rightFoot",
)


@dataclass
class PmxAnalysis:
    pmx_path: str
    source_dir: str
    model_name: str = ""
    model_name_english: str = ""
    version: float = 0.0
    encoding: str = ""
    vertex_count: int = 0
    face_count: int = 0
    texture_ref_count: int = 0
    material_count: int = 0
    bone_count: int = 0
    koikatsu_bone_hint_count: int = 0
    morph_count: int = 0
    morph_names: list[str] = field(default_factory=list)
    texture_file_count: int = 0
    resolved_texture_count: int = 0
    missing_texture_refs: list[str] = field(default_factory=list)
    missing_skeleton_groups: list[str] = field(default_factory=list)
    missing_required_skeleton_bones: list[str] = field(default_factory=list)
    parenting_warning_bones: list[dict[str, object]] = field(default_factory=list)
    content_warning_scan: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


@dataclass
class Workspace:
    root: Path
    source_assets_dir: Path
    import_dir: Path
    copied_pmx: Path
    blend_path: Path
    import_log_path: Path
    import_report_path: Path
    preflight_report_path: Path
    fix_dir: Path
    fixed_blend_path: Path
    fix_log_path: Path
    fix_report_path: Path


@dataclass
class SetupResult:
    blender_exe: Path
    version: str
    reused: bool
    state_path: Path


@dataclass
class ImportResult:
    workspace: Workspace
    setup: SetupResult
    command: list[str]


@dataclass
class FixResult:
    input_blend: Path
    output_blend: Path
    fix_dir: Path
    fix_log_path: Path
    fix_report_path: Path
    setup: SetupResult
    command: list[str]


@dataclass
class SpineAnalysisResult:
    input_blend: Path
    spine_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class SpineFixResult:
    input_blend: Path
    output_blend: Path
    spine_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    report_path: Path
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class SortBonesAnalysisResult:
    input_blend: Path
    sort_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class SortBonesResult:
    input_blend: Path
    output_blend: Path
    sort_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    report_path: Path
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class MaterialScanResult:
    input_blend: Path
    material_dir: Path
    scan_path: Path
    plan_path: Path
    log_path: Path
    scan: dict[str, object]
    plan: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class MaterialApplyResult:
    input_blend: Path
    output_blend: Path
    material_dir: Path
    scan_path: Path
    plan_path: Path
    merge_plan_path: Path
    log_path: Path
    report_path: Path
    materials_json_path: Path
    materials_npy_path: Path
    report: dict[str, object]
    merge_plan: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class MaterialMergeResult:
    input_blend: Path
    output_blend: Path
    material_dir: Path
    merge_plan_path: Path
    log_path: Path
    report_path: Path
    materials_json_path: Path
    materials_npy_path: Path
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class BodygroupAnalysisResult:
    input_blend: Path
    bodygroup_dir: Path
    manual_edit_blend: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class BodygroupResult:
    input_blend: Path
    output_blend: Path
    bodygroup_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    report_path: Path
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class FlexAnalysisResult:
    input_blend: Path
    flex_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class FlexResult:
    input_blend: Path
    output_blend: Path
    flex_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    report_path: Path
    flexes_json_path: Path
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class CollisionSourcesResult:
    input_blend: Path
    collision_dir: Path
    sources_path: Path
    log_path: Path
    sources: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class CollisionBonesResult:
    input_blend: Path
    collision_dir: Path
    bones_path: Path
    log_path: Path
    bones: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class CollisionAnalysisResult:
    input_blend: Path
    collision_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    physics_settings_path: Path
    physics_smd_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class CollisionResult:
    input_blend: Path
    output_blend: Path
    collision_dir: Path
    analysis_path: Path
    plan_path: Path
    log_path: Path
    report_path: Path
    physics_settings_path: Path
    physics_smd_path: Path
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class ProportionResult:
    input_blend: Path
    proportion_dir: Path
    raw_dir: Path
    workspace_dir: Path
    final_dir: Path
    pre_blend_path: Path
    processed_blend_path: Path
    report_path: Path
    files_path: Path
    log_path: Path
    report: dict[str, object]
    files: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class CArmsResult:
    input_dir: Path
    carms_dir: Path
    workspace_blend_path: Path
    report_path: Path
    files_path: Path
    log_path: Path
    report: dict[str, object]
    files: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class VrdAnalysisResult:
    input_dir: Path
    vrd_dir: Path
    workspace_blend_path: Path
    analysis_path: Path
    plan_path: Path
    preview_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    preview: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class VrdResult:
    input_dir: Path
    vrd_dir: Path
    workspace_blend_path: Path
    analysis_path: Path
    plan_path: Path
    preview_path: Path
    report_path: Path
    vrd_path: Path
    log_path: Path
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class VrdPreviewResult:
    input_dir: Path
    vrd_dir: Path
    workspace_blend_path: Path
    analysis_path: Path
    plan_path: Path
    preview_path: Path
    report_path: Path
    log_path: Path
    plan: dict[str, object]
    preview: dict[str, object]
    report: dict[str, object]
    setup: SetupResult
    command: list[str]


@dataclass
class TextureAnalysisResult:
    input_path: Path
    texture_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    manifest_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    command: list[str]


@dataclass
class TextureProcessResult:
    input_path: Path
    texture_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    manifest_path: Path
    log_path: Path
    report: dict[str, object]
    manifest: dict[str, object]
    command: list[str]


@dataclass
class IconAnalysisResult:
    input_path: Path
    icon_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    files_path: Path
    render_report_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    command: list[str]


@dataclass
class IconRunResult:
    input_path: Path
    icon_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    files_path: Path
    render_report_path: Path
    log_path: Path
    report: dict[str, object]
    files: dict[str, object]
    setup: SetupResult | None
    render_command: list[str] | None
    process_command: list[str]


@dataclass
class QcAnalysisResult:
    input_path: Path
    qc_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    files_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    command: list[str]


@dataclass
class QcCompileResult:
    input_path: Path
    qc_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    files_path: Path
    log_path: Path
    report: dict[str, object]
    files: dict[str, object]
    command: list[str]


@dataclass
class ReleaseAnalysisResult:
    input_path: Path
    release_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    files_path: Path
    translations_path: Path
    template_path: Path
    log_path: Path
    analysis: dict[str, object]
    plan: dict[str, object]
    command: list[str]


@dataclass
class ReleaseGenerateResult:
    input_path: Path
    release_dir: Path
    analysis_path: Path
    plan_path: Path
    report_path: Path
    files_path: Path
    translations_path: Path
    template_path: Path
    log_path: Path
    report: dict[str, object]
    files: dict[str, object]
    plan: dict[str, object]
    command: list[str]


class PmxReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = path.open("rb")

    def close(self) -> None:
        self.handle.close()

    def read_exact(self, size: int) -> bytes:
        data = self.handle.read(size)
        if len(data) != size:
            raise EOFError(f"unexpected end of PMX while reading {size} bytes")
        return data

    def read_u8(self) -> int:
        return self.read_exact(1)[0]

    def read_i8(self) -> int:
        return struct.unpack("<b", self.read_exact(1))[0]

    def read_u16(self) -> int:
        return struct.unpack("<H", self.read_exact(2))[0]

    def read_i16(self) -> int:
        return struct.unpack("<h", self.read_exact(2))[0]

    def read_u32(self) -> int:
        return struct.unpack("<I", self.read_exact(4))[0]

    def read_i32(self) -> int:
        return struct.unpack("<i", self.read_exact(4))[0]

    def read_f32(self) -> float:
        return struct.unpack("<f", self.read_exact(4))[0]

    def read_vector3(self) -> tuple[float, float, float]:
        return (self.read_f32(), self.read_f32(), self.read_f32())

    def read_vector_bytes(self, count: int) -> None:
        self.read_exact(4 * count)

    def read_string(self, encoding: str) -> str:
        size = self.read_i32()
        if size < 0:
            raise ValueError(f"invalid PMX string byte length: {size}")
        return self.read_exact(size).decode(encoding, errors="replace")

    def read_index(self, size: int, signed: bool = True) -> int:
        table: dict[tuple[int, bool], Callable[[], int]] = {
            (1, True): self.read_i8,
            (2, True): self.read_i16,
            (4, True): self.read_i32,
            (1, False): self.read_u8,
            (2, False): self.read_u16,
            (4, False): self.read_u32,
        }
        try:
            return table[(size, signed)]()
        except KeyError as exc:
            raise ValueError(f"invalid PMX index size: {size}") from exc


def emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)


def app_local_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME}"


def setup_dir() -> Path:
    return app_local_dir() / "setup"


def setup_state_path() -> Path:
    return setup_dir() / "setup_state.json"


def software_blender_root() -> Path:
    return app_local_dir() / "software" / "blender"


def delete_managed_blender_cache() -> dict[str, object]:
    """Delete only the importer-managed local Blender install and setup state."""

    root = software_blender_root()
    app_root = app_local_dir().resolve()
    try:
        resolved_root = root.resolve()
    except Exception:
        resolved_root = root.absolute()
    if resolved_root != app_root and app_root not in resolved_root.parents:
        raise RuntimeError(f"Refusing to delete path outside importer local data: {root}")

    size_bytes = 0
    file_count = 0
    if root.exists() and root.is_dir() and not root.is_symlink():
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            else:
                                size_bytes += entry.stat(follow_symlinks=False).st_size
                                file_count += 1
                        except OSError:
                            continue
            except OSError:
                continue

    removed_blender = False
    if root.exists():
        if root.is_dir() and not root.is_symlink():
            def on_rmtree_error(func, path, _exc_info) -> None:
                import stat

                os.chmod(path, stat.S_IWUSR)
                func(path)

            shutil.rmtree(root, onerror=on_rmtree_error)
        else:
            root.unlink()
        removed_blender = True

    state_path = setup_state_path()
    removed_state = False
    if state_path.exists():
        state_path.unlink()
        removed_state = True

    return {
        "blender_root": str(root),
        "setup_state": str(state_path),
        "removed_blender": removed_blender,
        "removed_state": removed_state,
        "size_bytes": size_bytes,
        "file_count": file_count,
    }


def workspaces_root() -> Path:
    return app_local_dir() / "workspaces"


def step_complete_marker_path(step_dir: Path) -> Path:
    return Path(step_dir) / STEP_COMPLETE_MARKER


def read_step_complete_marker(step_dir: Path) -> dict[str, object] | None:
    marker_path = step_complete_marker_path(step_dir)
    if not marker_path.exists():
        return None
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def is_step_complete(step_dir: Path, step_number: int | None = None) -> bool:
    data = read_step_complete_marker(step_dir)
    if not data or data.get("status") != "complete":
        return False
    if step_number is not None and int(data.get("step_number", -1) or -1) != int(step_number):
        return False
    return True


def write_step_complete_marker(
    step_dir: Path,
    step_number: int,
    step_title: str,
    inputs: dict[str, object] | None = None,
    outputs: dict[str, object] | None = None,
    report_path: Path | str | None = None,
    validation: dict[str, object] | None = None,
) -> Path:
    step_dir = Path(step_dir)
    step_dir.mkdir(parents=True, exist_ok=True)
    marker_path = step_complete_marker_path(step_dir)
    payload = {
        "schema_version": STEP_MARKER_SCHEMA_VERSION,
        "status": "complete",
        "step_number": int(step_number),
        "step_title": str(step_title),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs or {},
        "outputs": outputs or {},
        "report_path": str(report_path or ""),
        "validation": validation or {},
    }
    marker_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return marker_path


def backfill_step_complete_marker(
    step_dir: Path,
    step_number: int,
    step_title: str,
    expected_outputs: list[Path] | tuple[Path, ...],
    report_path: Path | str | None = None,
    validation: dict[str, object] | None = None,
) -> Path | None:
    """Write a completion marker for trusted existing outputs.

    This helper only accepts a backfill when every expected output path exists.
    Report content validation stays with the caller because each workflow step
    reports errors differently.
    """
    step_dir = Path(step_dir)
    if is_step_complete(step_dir, step_number):
        return step_complete_marker_path(step_dir)
    outputs = [Path(path) for path in expected_outputs if path]
    if not outputs or any(not path.exists() for path in outputs):
        return None
    return write_step_complete_marker(
        step_dir,
        step_number,
        step_title,
        outputs={path.name: str(path) for path in outputs},
        report_path=report_path,
        validation=validation or {"ok": True, "backfilled": True},
    )


def slugify(value: str, fallback: str = "model") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._ ")
    return text[:80] or fallback


def file_sha1(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_file_metadata(path: Path) -> str:
    # Fingerprint by file name + size only. PyInstaller onefile builds extract
    # bundled resources to a new %TEMP%\_MEIxxxxxx directory (fresh path and
    # mtime) on every launch, so including those would invalidate the setup
    # cache each session and force a full add-on re-verification.
    digest = hashlib.sha1()
    stat = path.stat()
    digest.update(path.name.encode("utf-8", errors="replace"))
    digest.update(str(stat.st_size).encode("ascii"))
    return digest.hexdigest()


def parse_version_from_blender_zip_name(name: str) -> str:
    match = re.search(r"blender-(4\.5\.\d+)-windows-x64\.zip", name, re.IGNORECASE)
    if not match:
        return "4.5"
    return match.group(1)


def infer_blender_version_from_path(path: Path | str) -> str | None:
    text = str(path)
    for pattern in (
        r"blender[-_/\\](4\.5\.\d+)",
        r"blender-(4\.5\.\d+)",
        r"[\\/](4\.5\.\d+)[\\/]",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    if re.search(r"blender[-_/\\]4\.5(?:[\\/]|$)|[\\/]4\.5[\\/]", text, re.IGNORECASE):
        return "4.5"
    return None


def parse_version_tuple(version: str) -> tuple[int, int, int]:
    parts = [int(part) for part in re.findall(r"\d+", version)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def read_setup_state() -> dict[str, object]:
    path = setup_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_setup_state(state: dict[str, object]) -> None:
    path = setup_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def plugin_fingerprints() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in (CATS_ADDON_ZIP, SOURCE_TOOLS_ZIP, BONES_MERGER_ZIP, MATERIAL_COMBINER_ZIP, COACD_ADDON_ZIP, L4D2_TOOLS_ZIP):
        if path.exists():
            out[path.name] = hash_file_metadata(path)
    return out


def hidden_subprocess_kwargs() -> dict[str, object]:
    """Hide console windows for background helper processes on Windows."""

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def blender_version_details(blender_exe: Path) -> tuple[str, str, str]:
    output = ""
    try:
        completed = subprocess.run(
            [str(blender_exe), "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            **hidden_subprocess_kwargs(),
        )
        output = completed.stdout or ""
    except Exception as exc:
        output = str(exc)
    match = re.search(r"Blender\s+(\d+\.\d+(?:\.\d+)?)", output)
    if not match:
        inferred = infer_blender_version_from_path(blender_exe)
        if inferred:
            return inferred, "path", output
        raise RuntimeError(f"could not determine Blender version from {blender_exe}\n{output}")
    return match.group(1), "command", output


def blender_version(blender_exe: Path) -> str:
    return blender_version_details(blender_exe)[0]


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def is_managed_blender_path(blender_exe: Path) -> bool:
    return path_is_under(blender_exe, software_blender_root())


def setup_state_is_current(state: dict[str, object]) -> Path | None:
    blender_raw = str(state.get("blender_exe") or "")
    if not blender_raw:
        return None
    blender = Path(blender_raw)
    if not blender.exists():
        return None
    if not is_managed_blender_path(blender):
        return None
    state_version = str(state.get("blender_version") or "")
    if parse_version_tuple(state_version)[:2] != (4, 5):
        inferred_version = infer_blender_version_from_path(blender)
        if parse_version_tuple(inferred_version or "")[:2] != (4, 5):
            return None
        state["blender_version"] = inferred_version
    if parse_version_tuple(str(state.get("blender_version") or ""))[:2] != (4, 5):
        return None
    if state.get("plugin_fingerprints") != plugin_fingerprints():
        return None
    if int(state.get("setup_requirements_version", 0) or 0) < SETUP_REQUIREMENTS_VERSION:
        return None
    if not bool(state.get("addons_verified")):
        return None
    deps = state.get("python_dependencies")
    if isinstance(deps, dict) and deps.get("mmd_tools") is False:
        return None
    return blender


class BlenderIndexParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def latest_official_blender_zip_url(progress: ProgressCallback | None = None) -> tuple[str, str]:
    emit(progress, f"Checking Blender 4.5 LTS downloads: {BLENDER_LTS_INDEX_URL}")
    request = urllib.request.Request(
        BLENDER_LTS_INDEX_URL,
        headers={"User-Agent": "MMDCharacterImporter/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")

    parser = BlenderIndexParser()
    parser.feed(body)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for link in parser.links:
        name = Path(urllib.parse.urlparse(link).path).name
        if re.fullmatch(r"blender-4\.5\.\d+-windows-x64\.zip", name, flags=re.IGNORECASE):
            candidates.append((parse_version_tuple(parse_version_from_blender_zip_name(name)), name))
    if not candidates:
        raise RuntimeError("no Blender 4.5 Windows x64 zip was found in the official download listing")
    _version_tuple, filename = sorted(candidates)[-1]
    return urllib.parse.urljoin(BLENDER_LTS_INDEX_URL, filename), filename


def download_file(
    url: str,
    target: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_name(target.name + ".part")
    if temp_target.exists():
        temp_target.unlink()
    request = urllib.request.Request(url, headers={"User-Agent": "MMDCharacterImporter/1.0"})
    emit(progress, f"Downloading {url}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temp_target.open("wb") as handle:
            length_header = response.headers.get("Content-Length")
            total = int(length_header) if length_header and length_header.isdigit() else 0
            received = 0
            last_report = 0.0
            while True:
                if cancel_check and cancel_check():
                    raise RuntimeError("operation was cancelled")
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_report > 2.0:
                    if total:
                        emit(progress, f"Downloaded {received / 1024 / 1024:.1f} MB / {total / 1024 / 1024:.1f} MB")
                    else:
                        emit(progress, f"Downloaded {received / 1024 / 1024:.1f} MB")
                    last_report = now
    except BaseException:
        with contextlib.suppress(OSError):
            temp_target.unlink()
        raise
    temp_target.replace(target)


def valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except Exception:
        return False


def blender_is_bundled() -> bool:
    """Lightweight check for whether THIS build ships the portable Blender zip (the "with Blender"
    variant). Existence is enough for the first-run decision; blender_zip_path() still validates the
    archive before extracting."""
    try:
        return BUNDLED_BLENDER_ZIP.exists() and BUNDLED_BLENDER_ZIP.stat().st_size > 0
    except OSError:
        return False


def provisioned_blender_zip_path() -> Path:
    """Where a user-downloaded/-browsed Blender zip is stored so the normal setup flow extracts it
    (the "without Blender" build provisions Blender here on first run)."""
    return app_local_dir() / "downloads" / EXPECTED_BLENDER_ZIP_NAME


def managed_blender_is_installed() -> bool:
    """True when a managed portable Blender 4.5.x is already extracted + verified from a prior run."""
    try:
        return setup_state_is_current(read_setup_state()) is not None
    except Exception:
        return False


def blender_needs_provisioning() -> bool:
    """True only for the "without Blender" build before Blender has been supplied: not bundled, not
    already installed, and no provisioned zip waiting to extract. The GUI uses this to decide whether
    to show the first-run download/browse prompt. All checks are cheap (no full-archive CRC)."""
    if blender_is_bundled():
        return False
    if managed_blender_is_installed():
        return False
    try:
        provisioned = provisioned_blender_zip_path()
        return not (provisioned.exists() and provisioned.stat().st_size > 0)
    except OSError:
        return True


def hash_file_sha256(
    path: Path, progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None
) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size if path.exists() else 0
    read = 0
    last = 0.0
    with path.open("rb") as handle:
        while True:
            if cancel_check and cancel_check():
                raise RuntimeError("operation was cancelled")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            read += len(chunk)
            now = time.monotonic()
            if now - last > 1.5:
                if total:
                    emit(progress, f"Verifying checksum: {read / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MB")
                last = now
    return digest.hexdigest()


def verify_blender_zip_checksum(
    path: Path, progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None
) -> tuple[bool, str]:
    """Return (matches_expected_sha256, actual_sha256_lowercase) for a Blender zip."""
    actual = hash_file_sha256(path, progress, cancel_check=cancel_check).lower()
    return (actual == EXPECTED_BLENDER_SHA256.lower(), actual)


def download_blender_zip(
    target: Path | None = None, progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None
) -> Path:
    """Download the pinned official Blender 4.5.10 zip to the provisioned location."""
    target = target or provisioned_blender_zip_path()
    download_file(BLENDER_DOWNLOAD_URL, target, progress, cancel_check=cancel_check)
    return target


def install_provisioned_blender_zip(
    source_zip: Path, progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None
) -> Path:
    """Place a user-supplied (browsed) Blender zip at the provisioned location so the normal setup
    flow extracts it. Copies only when the source is not already the provisioned file."""
    source_zip = source_zip.resolve()
    target = provisioned_blender_zip_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_zip == target.resolve():
        return target
    emit(progress, f"Copying Blender zip into place ({source_zip.name})...")
    tmp = target.with_name(target.name + ".part")
    if tmp.exists():
        tmp.unlink()
    try:
        shutil.copyfile(source_zip, tmp)
        if cancel_check and cancel_check():
            raise RuntimeError("operation was cancelled")
        tmp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def blender_zip_path(progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None) -> Path:
    if BUNDLED_BLENDER_ZIP.exists() and valid_zip(BUNDLED_BLENDER_ZIP):
        emit(progress, f"Using bundled Blender zip: {BUNDLED_BLENDER_ZIP}")
        return BUNDLED_BLENDER_ZIP
    if BUNDLED_BLENDER_ZIP.exists():
        emit(progress, f"WARNING: Bundled Blender zip is invalid and will be ignored: {BUNDLED_BLENDER_ZIP}")

    # A zip the user downloaded/browsed via the first-run prompt (the "without Blender" build). It is
    # preferred over the auto-download fallback so the user's chosen/verified zip is what gets used.
    provisioned = provisioned_blender_zip_path()
    if provisioned.exists() and valid_zip(provisioned):
        emit(progress, f"Using provisioned Blender zip: {provisioned}")
        return provisioned
    if provisioned.exists():
        emit(progress, f"WARNING: Provisioned Blender zip is invalid and will be ignored: {provisioned}")

    downloads = app_local_dir() / "downloads"
    try:
        url, filename = latest_official_blender_zip_url(progress)
        target = downloads / filename
        if not target.exists() or target.stat().st_size <= 0 or not valid_zip(target):
            download_file(url, target, progress, cancel_check=cancel_check)
        return target
    except Exception as exc:
        if "operation was cancelled" in str(exc):
            raise
        raise RuntimeError(f"could not download Blender and no valid bundled fallback exists: {exc}") from exc


def find_blender_exe_in_dir(root: Path) -> Path | None:
    direct = root / "blender.exe"
    if direct.exists():
        return direct
    for candidate in root.rglob("blender.exe"):
        return candidate
    return None


def extract_blender(
    zip_path: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> Path:
    version = parse_version_from_blender_zip_name(zip_path.name)
    target_root = software_blender_root() / version
    existing = find_blender_exe_in_dir(target_root) if target_root.exists() else None
    # The "portable" folder is created only after a fully successful extraction,
    # so it doubles as a completion marker: blender.exe without it means a
    # previous extraction was interrupted and the install is likely broken.
    if existing and (existing.parent / "portable").exists():
        emit(progress, f"Using existing portable Blender: {existing}")
        return existing
    if target_root.exists():
        emit(progress, f"Removing incomplete portable Blender extraction: {target_root}")
        shutil.rmtree(target_root, ignore_errors=True)

    target_root.parent.mkdir(parents=True, exist_ok=True)
    temp_root = target_root.with_name(target_root.name + ".extracting")
    if temp_root.exists():
        shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    emit(progress, f"Extracting Blender {version} to {target_root}")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            total = len(members)
            last_report = time.monotonic()
            for index, member in enumerate(members, start=1):
                if cancel_check and cancel_check():
                    raise RuntimeError("operation was cancelled")
                archive.extract(member, temp_root)
                now = time.monotonic()
                if now - last_report > 2.0:
                    emit(progress, f"Extracting Blender: {index:,} / {total:,} files ({index * 100 // max(total, 1)}%)")
                    last_report = now
    except BaseException:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    os.replace(temp_root, target_root)
    blender = find_blender_exe_in_dir(target_root)
    if not blender:
        raise RuntimeError(f"Blender extraction completed but blender.exe was not found under {target_root}")
    (blender.parent / "portable").mkdir(parents=True, exist_ok=True)
    return blender


def native_process_failure_diagnostic(return_code: int, command: list[str]) -> str:
    normalized_code = return_code if return_code >= 0 else return_code + (1 << 32)
    if return_code not in WINDOWS_STATUS_DLL_INIT_FAILED_CODES and normalized_code not in WINDOWS_STATUS_DLL_INIT_FAILED_CODES:
        return ""

    executable = Path(command[0]).name if command else "The child process"
    subject = "Blender" if "blender" in executable.lower() else executable
    return (
        "Windows error 0xC0000142 (STATUS_DLL_INIT_FAILED): "
        f"{subject} or one of its DLL/application components failed during initialization.\n"
        "This is usually a Windows/runtime environment issue, not a model import error.\n"
        "Suggested fixes:\n"
        "1. Install/reinstall the Microsoft Visual C++ Redistributable 2015-2022 x64.\n"
        "2. Whitelist MMD Character Importer and Blender in antivirus/security software.\n"
        "3. Update your GPU driver from the vendor site.\n"
        "4. Make sure the system has at least 1 GiB free storage and 2.5 GiB spare memory."
    )


def is_suppressed_process_output_line(line: str) -> bool:
    return (
        line.startswith("WARN (bmesh.mesh.convert):")
        and "bm_to_mesh_shape: Found shape-key but no CD_SHAPEKEY layers" in line
    )


def kill_process_tree(process: subprocess.Popen) -> None:
    """Terminate a child process and all of its descendants.

    Plain terminate()/kill() on Windows only kills the direct child, which
    orphans grandchildren such as studiomdl.exe/VTFCmd.exe/gmad.exe spawned by
    helper scripts; taskkill /T walks the whole tree.
    """

    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                **hidden_subprocess_kwargs(),
            )
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)


def run_process_streamed(
    command: list[str],
    progress: ProgressCallback | None = None,
    log_path: Path | None = None,
    cancel_check: CancelCheck | None = None,
    env: dict[str, str] | None = None,
) -> str:
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    output_lines: list[str] = []
    with contextlib.ExitStack() as stack:
        log_handle = stack.enter_context(log_path.open("w", encoding="utf-8", errors="replace")) if log_path else None
        suppressed_bmesh_shape_warnings = 0
        process = subprocess.Popen(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            env=env,
            **hidden_subprocess_kwargs(),
        )
        assert process.stdout is not None
        # Drain stdout on a dedicated thread so the main loop can poll
        # cancel_check every ~100 ms even while the subprocess is silent;
        # a blocking readline() here would make Cancel unresponsive during
        # long quiet stretches (CoACD, Cycles renders, studiomdl compiles).
        line_queue: queue.Queue[str | None] = queue.Queue()

        def drain_stdout() -> None:
            try:
                for raw_line in process.stdout:
                    line_queue.put(raw_line)
            except Exception:
                pass
            finally:
                line_queue.put(None)

        reader_thread = threading.Thread(target=drain_stdout, daemon=True)
        reader_thread.start()
        cancelled = False
        stream_finished = False
        while True:
            if cancel_check and cancel_check():
                kill_process_tree(process)
                cancelled = True
                break

            try:
                line = line_queue.get(timeout=0.1)
            except queue.Empty:
                if stream_finished and process.poll() is not None:
                    break
                continue

            if line is None:
                stream_finished = True
                if process.poll() is not None:
                    break
                continue

            clean = line.rstrip("\r\n")
            if is_suppressed_process_output_line(clean):
                suppressed_bmesh_shape_warnings += 1
                continue
            output_lines.append(clean)
            if log_handle:
                log_handle.write(clean + "\n")
                log_handle.flush()
            if clean:
                emit(progress, clean)

        return_code = process.wait()
        if suppressed_bmesh_shape_warnings:
            summary = (
                "Suppressed "
                f"{suppressed_bmesh_shape_warnings:,} repeated Blender bmesh shape-key conversion warning(s)."
            )
            output_lines.append(summary)
            if log_handle:
                log_handle.write(summary + "\n")
                log_handle.flush()
            emit(progress, summary)
    output = "\n".join(output_lines)
    if cancelled:
        raise RuntimeError("operation was cancelled")
    if return_code != 0:
        diagnostic = native_process_failure_diagnostic(return_code, command)
        diagnostic_block = f"\n\n{diagnostic}" if diagnostic else ""
        raise RuntimeError(f"process failed with exit code {return_code}{diagnostic_block}\n{' '.join(command)}\n{output}")
    return output


def compact_log_excerpt(lines: list[str], max_lines: int = 80) -> str:
    if not lines:
        return ""
    markers = (
        "error",
        "exception",
        "traceback",
        "failed",
        "runtimeerror",
        "warning",
        "could not",
        "not found",
        "permission",
        "access",
        "cancel",
    )
    interesting: list[str] = []
    for line in lines:
        lower = line.lower()
        if any(marker in lower for marker in markers):
            interesting.append(line)
    selected: list[str] = []
    for line in interesting[-max(10, max_lines // 2) :]:
        if line not in selected:
            selected.append(line)
    tail_budget = max(0, max_lines - len(selected))
    for line in lines[-tail_budget:]:
        if line not in selected:
            selected.append(line)
    if not selected:
        selected = lines[-max_lines:]
    return "\n".join(selected[-max_lines:])


def missing_output_log_candidates(expected_path: Path, log_path: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if log_path is not None:
        candidates.append(log_path)
    folder = expected_path.parent
    if folder.exists():
        candidates.extend(sorted(folder.glob("*.log"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True))
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).lower()
        except Exception:
            key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def missing_output_diagnostic(
    expected_path: Path,
    log_path: Path | None = None,
    command: list[str] | None = None,
    max_log_lines: int = 80,
) -> str:
    expected_path = Path(expected_path)
    lines: list[str] = [
        "Missing-output diagnostics:",
        f"- Expected output: {expected_path}",
        f"- Output folder: {expected_path.parent}",
    ]
    if command:
        lines.append("- Blender command: " + " ".join(str(part) for part in command))

    if expected_path.parent.exists():
        try:
            siblings = sorted(
                [path for path in expected_path.parent.iterdir() if path.is_file()],
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if siblings:
                preview = ", ".join(path.name for path in siblings[:12])
                suffix = "" if len(siblings) <= 12 else f", ... {len(siblings) - 12} more"
                lines.append(f"- Files currently in output folder: {preview}{suffix}")
            else:
                lines.append("- Output folder exists but contains no files.")
        except Exception as exc:
            lines.append(f"- Could not list output folder: {exc}")
    else:
        lines.append("- Output folder does not exist.")

    log_candidates = [path for path in missing_output_log_candidates(expected_path, log_path) if path.exists()]
    if not log_candidates:
        lines.append("- No Blender log file was found next to the expected output.")
    else:
        for candidate in log_candidates[:3]:
            try:
                raw_lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                excerpt = compact_log_excerpt(raw_lines, max_log_lines)
            except Exception as exc:
                lines.append(f"- Could not read Blender log {candidate}: {exc}")
                continue
            lines.append(f"\nBlender log excerpt from {candidate}:")
            lines.append(excerpt if excerpt else "(log file is empty)")

    lines.append(
        "\nIf the log excerpt has no Python traceback, Blender likely exited before the script reached the save/write step "
        "or was interrupted by an external Blender/add-on/Windows environment issue."
    )
    return "\n".join(lines)


def raise_missing_output(
    expected_path: Path,
    log_path: Path | None = None,
    command: list[str] | None = None,
    label: str = "Blender",
) -> None:
    diagnostic = missing_output_diagnostic(expected_path, log_path=log_path, command=command)
    raise RuntimeError(f"{label} completed but did not write {expected_path}\n\n{diagnostic}")


def enrich_missing_output_message(message: str) -> str:
    text = str(message or "")
    if "Missing-output diagnostics:" in text:
        return text
    match = MISSING_OUTPUT_RE.search(text)
    if not match:
        return text
    raw_path = match.group("path").strip().strip("\"'")
    if not raw_path:
        return text
    try:
        expected_path = Path(raw_path)
    except Exception:
        return text
    return text + "\n\n" + missing_output_diagnostic(expected_path)


def verify_and_install_addons(
    blender_exe: Path,
    progress: ProgressCallback | None = None,
    check_only: bool = False,
    cancel_check: CancelCheck | None = None,
) -> str:
    if not BLENDER_SETUP_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SETUP_SCRIPT)
    if not CATS_ADDON_ZIP.exists():
        raise FileNotFoundError(CATS_ADDON_ZIP)
    if not SOURCE_TOOLS_ZIP.exists():
        raise FileNotFoundError(SOURCE_TOOLS_ZIP)
    if not BONES_MERGER_ZIP.exists():
        raise FileNotFoundError(BONES_MERGER_ZIP)
    if not MATERIAL_COMBINER_ZIP.exists():
        raise FileNotFoundError(MATERIAL_COMBINER_ZIP)
    if not COACD_ADDON_ZIP.exists():
        raise FileNotFoundError(COACD_ADDON_ZIP)
    if not L4D2_TOOLS_ZIP.exists():
        raise FileNotFoundError(L4D2_TOOLS_ZIP)

    command = [
        str(blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SETUP_SCRIPT),
        "--",
        "--cats-zip",
        str(CATS_ADDON_ZIP),
        "--source-tools-zip",
        str(SOURCE_TOOLS_ZIP),
        "--bones-merger-zip",
        str(BONES_MERGER_ZIP),
        "--material-combiner-zip",
        str(MATERIAL_COMBINER_ZIP),
        "--coacd-addon-zip",
        str(COACD_ADDON_ZIP),
        "--l4d2-tools-zip",
        str(L4D2_TOOLS_ZIP),
    ]
    if check_only:
        command.append("--check-only")
    emit(progress, "Verifying bundled Blender add-ons...")
    output = run_process_streamed(command, progress=progress, cancel_check=cancel_check)
    if "Blender add-on setup verified." not in output:
        raise RuntimeError("Blender add-on setup did not report successful verification.")
    return output


def log_tail(path: Path, max_lines: int = 80) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def _find_steam_dir() -> Path | None:
    """Detect the Steam installation directory on Windows."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
            if steam_path and Path(steam_path).exists():
                return Path(steam_path)
    except Exception:
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam") as key:
            steam_path, _ = winreg.QueryValueEx(key, "InstallPath")
            if steam_path and Path(steam_path).exists():
                return Path(steam_path)
    except Exception:
        pass
    for candidate in (
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
    ):
        if candidate.exists():
            return candidate
    return None


def _find_steam_library_folders(steam_dir: Path) -> list[Path]:
    """Return all Steam library folders from libraryfolders.vdf."""
    libraries: list[Path] = [steam_dir]
    vdf = steam_dir / "steamapps" / "libraryfolders.vdf"
    if not vdf.exists():
        return libraries
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            lib = Path(match.group(1).replace("\\\\", "\\"))
            if lib.exists():
                libraries.append(lib)
    except Exception:
        pass
    return libraries


def _system_blender_candidates() -> list[Path]:
    candidates: list[Path] = []

    for exe_name in ("blender.exe", "blender"):
        path_exe = shutil.which(exe_name)
        if path_exe:
            candidates.append(Path(path_exe))

    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    search_roots: list[Path] = []
    for base in (program_files, program_files_x86, local_appdata):
        if base:
            search_roots.append(Path(base) / "Blender Foundation")
    # Steam Blender: probe only the fixed install location instead of walking
    # every Steam library with rglob (which can scan 100k+ game files).
    steam_path = _find_steam_dir()
    if steam_path:
        for lib in _find_steam_library_folders(steam_path):
            steam_blender = lib / "steamapps" / "common" / "Blender" / "blender.exe"
            if steam_blender.exists():
                candidates.append(steam_blender)

    for root in search_roots:
        if not root.exists():
            continue
        for candidate in root.rglob("blender.exe"):
            candidates.append(candidate)

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            key = candidate.resolve()
        except Exception:
            key = candidate.absolute()
        if key in seen or is_managed_blender_path(key):
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _find_system_blender(progress: ProgressCallback | None = None) -> Path | None:
    """Search for system Blender and warn when it is not Blender 4.5.x.

    This is intentionally warning-only. The importer uses its managed portable
    Blender by default so user Blender installs are not modified or selected
    automatically.
    """

    compatible: Path | None = None
    incompatible: list[tuple[Path, str]] = []
    for candidate in _system_blender_candidates():
        try:
            ver = blender_version(candidate)
        except Exception:
            continue
        if parse_version_tuple(ver)[:2] == (4, 5):
            if compatible is None:
                compatible = candidate
        else:
            incompatible.append((candidate, ver))

    for candidate, ver in incompatible[:4]:
        emit(
            progress,
            "WARNING: System Blender "
            f"{ver} was found at {candidate}. MMD Character Importer expects Blender 4.5.x; "
            "using the bundled/managed Blender instead to avoid add-on conflicts.",
        )
    if len(incompatible) > 4:
        emit(progress, f"WARNING: {len(incompatible) - 4} additional non-4.5 system Blender install(s) were found.")
    if compatible:
        emit(progress, f"Detected system Blender 4.5.x at {compatible}; using bundled/managed Blender by default.")
    return compatible


def warn_about_system_blender(progress: ProgressCallback | None = None) -> None:
    global SYSTEM_BLENDER_WARNING_CHECKED
    if SYSTEM_BLENDER_WARNING_CHECKED:
        return
    SYSTEM_BLENDER_WARNING_CHECKED = True
    _find_system_blender(progress)


def find_managed_blender(progress: ProgressCallback | None = None, cancel_check: CancelCheck | None = None) -> Path:
    zip_path = blender_zip_path(progress, cancel_check=cancel_check)
    return extract_blender(zip_path, progress, cancel_check=cancel_check)


def reusable_managed_blender_from_state(state: dict[str, object], progress: ProgressCallback | None = None) -> Path | None:
    state_blender_raw = str(state.get("blender_exe") or "")
    if not state_blender_raw:
        return None
    candidate = Path(state_blender_raw)
    if not candidate.exists():
        return None
    if not is_managed_blender_path(candidate):
        emit(progress, f"Ignoring saved system Blender path; using bundled/managed Blender by default: {candidate}")
        return None
    try:
        candidate_version, version_source, _version_output = blender_version_details(candidate)
        if parse_version_tuple(candidate_version)[:2] == (4, 5):
            if version_source != "command":
                emit(
                    progress,
                    "WARNING: Could not read managed Blender version from --version output; "
                    f"using version inferred from folder name: {candidate_version}.",
                )
            emit(progress, f"Reusing existing managed Blender for setup repair: {candidate}")
            return candidate
    except Exception as exc:
        inferred_version = infer_blender_version_from_path(candidate)
        if parse_version_tuple(inferred_version or "")[:2] == (4, 5):
            emit(
                progress,
                "WARNING: Could not read managed Blender version from --version output; "
                f"using version inferred from folder name: {inferred_version}. Details: {exc}",
            )
            return candidate
    return None


def ensure_portable_blender(
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> SetupResult:
    warn_about_system_blender(progress)
    state = read_setup_state()
    state_blender = setup_state_is_current(state)
    if state_blender:
        return SetupResult(
            blender_exe=state_blender,
            version=str(state.get("blender_version") or "4.5"),
            reused=True,
            state_path=setup_state_path(),
        )

    blender = reusable_managed_blender_from_state(state, progress)
    if blender is None:
        blender = find_managed_blender(progress, cancel_check=cancel_check)
    try:
        version, version_source, _version_output = blender_version_details(blender)
    except Exception as exc:
        inferred_version = infer_blender_version_from_path(blender)
        if is_managed_blender_path(blender) and parse_version_tuple(inferred_version or "")[:2] == (4, 5):
            version = inferred_version or "4.5"
            version_source = "path"
            emit(
                progress,
                "WARNING: Could not read managed Blender version from --version output; "
                f"using version inferred from folder name: {version}. Details: {exc}",
            )
        elif is_managed_blender_path(blender):
            version = "4.5"
            version_source = "managed-default"
            emit(
                progress,
                "WARNING: Could not determine managed Blender version; assuming bundled Blender 4.5.x "
                f"from managed install path and continuing. Details: {exc}",
            )
        else:
            raise
    if parse_version_tuple(version)[:2] != (4, 5):
        raise RuntimeError(f"Blender {version} is not supported; expected Blender 4.5.x for the bundled CATS add-on")
    if version_source != "command" and version_source != "managed-default":
        emit(
            progress,
            "WARNING: Could not read managed Blender version from --version output; "
            f"using version inferred from folder name: {version}.",
        )
    setup_output = verify_and_install_addons(blender, progress=progress, check_only=False, cancel_check=cancel_check)
    write_setup_state(
        {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "setup_requirements_version": SETUP_REQUIREMENTS_VERSION,
            "blender_exe": str(blender),
            "blender_version": version,
            "plugin_fingerprints": plugin_fingerprints(),
            "addons_verified": True,
            "python_dependencies": {
                "scikit_learn": "Scikit-learn is available" in setup_output,
                "coacd": "CoACD collision operator is available" in setup_output,
                "l4d2_vrd": "L4D2 Character Tools VRD operators are available" in setup_output,
                "mmd_tools": "MMD Tools PMX/VMD import operators are available" in setup_output,
            },
        }
    )
    return SetupResult(blender_exe=blender, version=version, reused=False, state_path=setup_state_path())


def skip_vertex(reader: PmxReader, bone_index_size: int, additional_uvs: int) -> tuple[float, float, float]:
    position = reader.read_vector3()
    reader.read_vector_bytes(3)
    reader.read_vector_bytes(2)
    for _ in range(additional_uvs):
        reader.read_vector_bytes(4)
    weight_type = reader.read_u8()
    if weight_type == 0:
        reader.read_index(bone_index_size)
    elif weight_type == 1:
        reader.read_index(bone_index_size)
        reader.read_index(bone_index_size)
        reader.read_f32()
    elif weight_type in (2, 4):
        for _ in range(4):
            reader.read_index(bone_index_size)
        reader.read_vector_bytes(4)
    elif weight_type == 3:
        reader.read_index(bone_index_size)
        reader.read_index(bone_index_size)
        reader.read_f32()
        reader.read_vector_bytes(3)
        reader.read_vector_bytes(3)
        reader.read_vector_bytes(3)
    else:
        raise ValueError(f"unsupported PMX vertex weight type: {weight_type}")
    reader.read_f32()
    return position


def skip_material(reader: PmxReader, encoding: str, texture_index_size: int) -> None:
    reader.read_string(encoding)
    reader.read_string(encoding)
    reader.read_vector_bytes(4)
    reader.read_vector_bytes(3)
    reader.read_f32()
    reader.read_vector_bytes(3)
    reader.read_u8()
    reader.read_vector_bytes(4)
    reader.read_f32()
    reader.read_index(texture_index_size)
    reader.read_index(texture_index_size)
    reader.read_i8()
    shared_toon = reader.read_i8()
    if shared_toon == 1:
        reader.read_i8()
    else:
        reader.read_index(texture_index_size)
    reader.read_string(encoding)
    reader.read_i32()


def read_bone_info(reader: PmxReader, encoding: str, bone_index_size: int) -> dict[str, object]:
    name = reader.read_string(encoding)
    english_name = reader.read_string(encoding)
    position = reader.read_vector3()
    parent_index = reader.read_index(bone_index_size)
    reader.read_i32()
    flags = reader.read_i16()
    if flags & 0x0001:
        reader.read_index(bone_index_size)
    else:
        reader.read_vector_bytes(3)
    if flags & 0x0300:
        reader.read_index(bone_index_size)
        reader.read_f32()
    if flags & 0x0400:
        reader.read_vector_bytes(3)
    if flags & 0x0800:
        reader.read_vector_bytes(3)
        reader.read_vector_bytes(3)
    if flags & 0x2000:
        reader.read_i32()
    if flags & 0x0020:
        reader.read_index(bone_index_size)
        reader.read_i32()
        reader.read_f32()
        link_count = reader.read_i32()
        for _ in range(link_count):
            reader.read_index(bone_index_size)
            limited = reader.read_u8()
            if limited:
                reader.read_vector_bytes(3)
                reader.read_vector_bytes(3)
    return {
        "name": name,
        "english_name": english_name,
        "position": position,
        "parent_index": parent_index,
    }


def skip_morph(
    reader: PmxReader,
    encoding: str,
    vertex_index_size: int,
    bone_index_size: int,
    morph_index_size: int,
    material_index_size: int,
    rigid_index_size: int,
) -> tuple[str, str]:
    name = reader.read_string(encoding)
    english_name = reader.read_string(encoding)
    reader.read_u8()
    morph_type = reader.read_u8()
    offset_count = reader.read_i32()
    for _ in range(offset_count):
        if morph_type == 0:
            reader.read_index(morph_index_size)
            reader.read_f32()
        elif morph_type == 1:
            reader.read_index(vertex_index_size, signed=False)
            reader.read_vector_bytes(3)
        elif morph_type == 2:
            reader.read_index(bone_index_size)
            reader.read_vector_bytes(3)
            reader.read_vector_bytes(4)
        elif morph_type in (3, 4, 5, 6, 7):
            reader.read_index(vertex_index_size, signed=False)
            reader.read_vector_bytes(4)
        elif morph_type == 8:
            reader.read_index(material_index_size)
            reader.read_u8()
            reader.read_vector_bytes(4)
            reader.read_vector_bytes(3)
            reader.read_f32()
            reader.read_vector_bytes(3)
            reader.read_vector_bytes(4)
            reader.read_f32()
            reader.read_vector_bytes(4)
            reader.read_vector_bytes(4)
            reader.read_vector_bytes(4)
        elif morph_type == 9:
            reader.read_index(morph_index_size)
            reader.read_f32()
        elif morph_type == 10:
            reader.read_index(rigid_index_size)
            reader.read_u8()
            reader.read_vector_bytes(3)
            reader.read_vector_bytes(3)
        else:
            raise ValueError(f"unsupported PMX morph type: {morph_type}")
    return name, english_name


def distance3(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((float(a[index]) - float(b[index])) ** 2 for index in range(3)))


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = int(max(0, min(len(sorted_values) - 1, round((len(sorted_values) - 1) * float(fraction)))))
    return float(sorted_values[index])


def pmx_bone_display_name(bone: dict[str, object]) -> str:
    name = str(bone.get("name") or "").strip()
    english = str(bone.get("english_name") or "").strip()
    return name or english or f"bone_{int(bone.get('index', 0) or 0)}"


def combined_bone_name_text(bone: dict[str, object]) -> str:
    return f"{bone.get('name') or ''} {bone.get('english_name') or ''}".strip()


def explicit_bone_side(name: str) -> str:
    lowered = str(name or "").lower()
    left = bool(re.search(r"(^|[_\-. ])l($|[_\-. ]|\d)|left|左", lowered))
    right = bool(re.search(r"(^|[_\-. ])r($|[_\-. ]|\d)|right|右", lowered))
    if left and not right:
        return "L"
    if right and not left:
        return "R"
    return ""


def is_control_or_root_bone_name(name: str) -> bool:
    lowered = str(name or "").lower()
    compact = re.sub(r"[\s_.\-]+", "", lowered)
    if not compact:
        return True
    control_tokens = (
        "ik",
        "target",
        "dummy",
        "root",
        "mother",
        "center",
        "groove",
        "全ての親",
        "センター",
        "グルーブ",
        "操作",
        "表示",
    )
    if any(token in lowered for token in control_tokens):
        return True
    return "先" in lowered and ("ik" in lowered or "センター" in lowered or "center" in lowered)


def detect_parenting_warning_bones(
    bones: list[dict[str, object]],
    model_height: float,
) -> list[dict[str, object]]:
    if not bones or model_height <= 1.0e-6:
        return []
    distances: list[float] = []
    pairs: list[tuple[dict[str, object], dict[str, object], float]] = []
    for bone in bones:
        try:
            parent_index = int(bone.get("parent_index", -1) or -1)
        except Exception:
            parent_index = -1
        if parent_index < 0 or parent_index >= len(bones):
            continue
        parent = bones[parent_index]
        child_pos = bone.get("position")
        parent_pos = parent.get("position")
        if not isinstance(child_pos, tuple) or not isinstance(parent_pos, tuple):
            continue
        dist = distance3(child_pos, parent_pos)
        distances.append(dist)
        pairs.append((bone, parent, dist))
    if not pairs:
        return []
    p95 = percentile(sorted(distances), 0.95)
    extreme_threshold = max(float(model_height) * 0.9, p95 * 5.0)
    side_threshold = float(model_height) * 0.15
    warnings: list[dict[str, object]] = []
    seen_children: set[int] = set()
    for child, parent, dist in sorted(pairs, key=lambda item: item[2], reverse=True):
        child_index = int(child.get("index", -1) or -1)
        if child_index in seen_children:
            continue
        child_text = combined_bone_name_text(child)
        if is_control_or_root_bone_name(child_text):
            continue
        parent_text = combined_bone_name_text(parent)
        child_side = explicit_bone_side(child_text)
        parent_side = explicit_bone_side(parent_text)
        reason = ""
        threshold = 0.0
        if dist > extreme_threshold:
            reason = "extreme_distance"
            threshold = extreme_threshold
        elif child_side and parent_side and child_side != parent_side and dist > side_threshold:
            reason = "opposite_side_parent"
            threshold = side_threshold
        if not reason:
            continue
        seen_children.add(child_index)
        warnings.append(
            {
                "child_index": child_index,
                "child_name": pmx_bone_display_name(child),
                "parent_index": int(parent.get("index", -1) or -1),
                "parent_name": pmx_bone_display_name(parent),
                "distance": round(float(dist), 6),
                "model_height": round(float(model_height), 6),
                "distance_ratio": round(float(dist) / max(float(model_height), 1.0e-9), 6),
                "threshold": round(float(threshold), 6),
                "reason": reason,
                "child_side": child_side,
                "parent_side": parent_side,
            }
        )
    return warnings


def load_warning_keywords(path: Path = WARNING_KEYWORDS_PATH) -> list[str]:
    if not path.exists():
        return []
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        keyword = raw.strip()
        if not keyword or keyword.startswith("#"):
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)
    return keywords


def scan_morph_content_warning(
    morph_names: list[str],
    keywords: list[str] | None = None,
    threshold: int = CONTENT_WARNING_KEYWORD_THRESHOLD,
) -> dict[str, object]:
    keywords = keywords if keywords is not None else load_warning_keywords()
    keyword_pairs = [(keyword, keyword.casefold()) for keyword in keywords if keyword.strip()]
    matches: list[dict[str, object]] = []
    for index, morph_name in enumerate(morph_names):
        haystack = morph_name.casefold()
        matched_for_morph: set[str] = set()
        for keyword, folded_keyword in keyword_pairs:
            if folded_keyword and folded_keyword in haystack and folded_keyword not in matched_for_morph:
                matched_for_morph.add(folded_keyword)
                matches.append({"index": index, "morph_name": morph_name, "keyword": keyword})
    matched_keywords = sorted({str(match["keyword"]) for match in matches}, key=str.casefold)
    return {
        "keyword_file": str(WARNING_KEYWORDS_PATH),
        "threshold": threshold,
        "keyword_count": len(keywords),
        "match_count": len(matches),
        "matched_morph_count": len({int(match["index"]) for match in matches}),
        "triggered": len(matches) > threshold,
        "matched_keywords": matched_keywords,
        "matches": matches[:100],
        "truncated": len(matches) > 100,
    }


def resolve_texture_ref(pmx_path: Path, texture_ref: str) -> Path:
    normalized = texture_ref.replace("\\", os.sep).replace("/", os.sep)
    texture_path = Path(normalized)
    if texture_path.is_absolute():
        return texture_path
    return (pmx_path.parent / texture_path).resolve()


def native_texture_files(source_dir: Path) -> list[Path]:
    return [
        path
        for path in source_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXTURE_EXTENSIONS
    ]


def analyze_pmx(pmx_path: Path, source_dir: Path | None = None) -> PmxAnalysis:
    pmx_path = pmx_path.resolve()
    source_dir = (source_dir or pmx_path.parent).resolve()
    if not pmx_path.exists():
        raise FileNotFoundError(pmx_path)
    if not pmx_path.is_file():
        raise FileNotFoundError(f"PMX path is not a file: {pmx_path}")

    analysis = PmxAnalysis(pmx_path=str(pmx_path), source_dir=str(source_dir))
    reader = PmxReader(pmx_path)
    try:
        signature = reader.read_exact(4)
        if signature != b"PMX ":
            raise ValueError(f"not a PMX file: invalid signature {signature!r}")
        analysis.version = reader.read_f32()
        header_size = reader.read_u8()
        if header_size < 8:
            raise ValueError(f"unsupported PMX header size: {header_size}")
        encoding_flag = reader.read_u8()
        analysis.encoding = "utf-16-le" if encoding_flag == 0 else "utf-8"
        additional_uvs = reader.read_u8()
        vertex_index_size = reader.read_u8()
        texture_index_size = reader.read_u8()
        material_index_size = reader.read_u8()
        bone_index_size = reader.read_u8()
        morph_index_size = reader.read_u8()
        rigid_index_size = reader.read_u8()
        for _ in range(max(0, header_size - 8)):
            reader.read_u8()

        analysis.model_name = reader.read_string(analysis.encoding)
        analysis.model_name_english = reader.read_string(analysis.encoding)
        reader.read_string(analysis.encoding)
        reader.read_string(analysis.encoding)

        analysis.vertex_count = reader.read_i32()
        if analysis.vertex_count > MAX_SUPPORTED_PMX_VERTEX_COUNT:
            raise RuntimeError(
                f"PMX vertex count {analysis.vertex_count:,} exceeds the supported limit of "
                f"{MAX_SUPPORTED_PMX_VERTEX_COUNT:,}. This model is too large for the importer."
            )
        vertex_bounds_min = [float("inf"), float("inf"), float("inf")]
        vertex_bounds_max = [float("-inf"), float("-inf"), float("-inf")]
        for _ in range(analysis.vertex_count):
            position = skip_vertex(reader, bone_index_size, additional_uvs)
            for axis in range(3):
                value = float(position[axis])
                vertex_bounds_min[axis] = min(vertex_bounds_min[axis], value)
                vertex_bounds_max[axis] = max(vertex_bounds_max[axis], value)
        # PMX coordinates are Y-up (mmd_tools converts to Blender's Z-up later),
        # so the model height is the Y extent (axis index 1).
        model_height = (
            vertex_bounds_max[1] - vertex_bounds_min[1]
            if analysis.vertex_count > 0 and all(math.isfinite(value) for value in (vertex_bounds_min[1], vertex_bounds_max[1]))
            else 0.0
        )

        face_index_count = reader.read_i32()
        analysis.face_count = face_index_count // 3
        reader.read_exact(face_index_count * vertex_index_size)

        analysis.texture_ref_count = reader.read_i32()
        texture_refs = [reader.read_string(analysis.encoding) for _ in range(analysis.texture_ref_count)]

        analysis.material_count = reader.read_i32()
        for _ in range(analysis.material_count):
            skip_material(reader, analysis.encoding, texture_index_size)

        analysis.bone_count = reader.read_i32()
        bone_names: set[str] = set()
        exact_bone_names: set[str] = set()
        koikatsu_bone_hint_count = 0
        bone_infos: list[dict[str, object]] = []
        for bone_index in range(analysis.bone_count):
            bone_info = read_bone_info(reader, analysis.encoding, bone_index_size)
            bone_info["index"] = bone_index
            bone_infos.append(bone_info)
            name = str(bone_info.get("name") or "")
            english_name = str(bone_info.get("english_name") or "")
            stripped_name = name.strip()
            stripped_english_name = english_name.strip()
            lower_bone_names = [value.lower() for value in (stripped_name, stripped_english_name) if value]
            if any("cf_" in value or "k_f_" in value for value in lower_bone_names):
                koikatsu_bone_hint_count += 1
            if stripped_name:
                exact_bone_names.add(stripped_name)
                bone_names.add(stripped_name.lower())
            if stripped_english_name:
                exact_bone_names.add(stripped_english_name)
                bone_names.add(stripped_english_name.lower())
        analysis.koikatsu_bone_hint_count = koikatsu_bone_hint_count
        analysis.parenting_warning_bones = detect_parenting_warning_bones(bone_infos, model_height)

        analysis.morph_count = reader.read_i32()
        for _ in range(analysis.morph_count):
            morph_name, morph_english_name = skip_morph(
                reader,
                analysis.encoding,
                vertex_index_size,
                bone_index_size,
                morph_index_size,
                material_index_size,
                rigid_index_size,
            )
            display_name = morph_name.strip()
            english_display_name = morph_english_name.strip()
            if english_display_name and english_display_name != display_name:
                display_name = f"{display_name} / {english_display_name}" if display_name else english_display_name
            if display_name:
                analysis.morph_names.append(display_name)
    finally:
        reader.close()

    textures = native_texture_files(source_dir)
    analysis.texture_file_count = len(textures)
    missing_refs: list[str] = []
    resolved_count = 0
    for texture_ref in texture_refs:
        if resolve_texture_ref(pmx_path, texture_ref).exists():
            resolved_count += 1
        else:
            missing_refs.append(texture_ref)
    analysis.resolved_texture_count = resolved_count
    analysis.missing_texture_refs = missing_refs

    missing_groups: list[str] = []
    for group, aliases in HUMANOID_SKELETON_GROUPS.items():
        if not any(alias.lower() in bone_names for alias in aliases):
            missing_groups.append(group)
    analysis.missing_skeleton_groups = missing_groups

    missing_required_bones = [
        f"{japanese_name} / {english_name}"
        for japanese_name, english_name in REQUIRED_MMD_SKELETON_BONES
        if japanese_name not in exact_bone_names and english_name not in exact_bone_names
    ]
    analysis.missing_required_skeleton_bones = missing_required_bones

    warnings: list[str] = []
    if missing_required_bones:
        preview = ", ".join(missing_required_bones[:10])
        suffix = "" if len(missing_required_bones) <= 10 else f" and {len(missing_required_bones) - 10} more"
        warnings.append(
            "MMD skeleton compatibility check failed: missing exact required bones: "
            f"{preview}{suffix}. Import is expected to fail because the model does not use the required MMD humanoid bone names."
        )
    if missing_groups:
        warnings.append("Missing expected MMD humanoid skeleton groups: " + ", ".join(missing_groups))
    if analysis.texture_file_count <= 0:
        warnings.append("No native texture files were found in the selected model folder.")
    if missing_refs:
        preview = ", ".join(missing_refs[:8])
        suffix = "" if len(missing_refs) <= 8 else f" and {len(missing_refs) - 8} more"
        warnings.append(f"{len(missing_refs)} PMX texture references could not be resolved: {preview}{suffix}")
    if analysis.vertex_count > HIGH_PMX_VERTEX_WARNING_THRESHOLD:
        warnings.append(
            f"High vertex count: {analysis.vertex_count:,} vertices; "
            "importing may take a long time and produce unexpected results."
        )
    if analysis.bone_count > 600:
        warnings.append(f"High bone count: {analysis.bone_count:,} bones; importing may be slow.")
    if analysis.koikatsu_bone_hint_count > 100:
        warnings.append(
            f"Koikatsu-style bone names detected: {analysis.koikatsu_bone_hint_count:,} bones contain cf_ or k_f_. "
            "Your model looks like it is from Koikatsu, and the import is likely to fail if your model is not "
            "simplified to Very Simple level in KKBP Blender plugin. You can ignore this warning only if you are "
            "sure that your model is optimized for MikuMikuDance."
        )
    if analysis.parenting_warning_bones:
        preview_pairs = [
            f"{entry.get('child_name')} -> {entry.get('parent_name')}"
            for entry in analysis.parenting_warning_bones[:8]
        ]
        preview = "; ".join(preview_pairs)
        if len(analysis.parenting_warning_bones) > 8:
            preview += f"; and {len(analysis.parenting_warning_bones) - 8} more"
        warnings.append(
            f"Potential bone parenting issues detected: {len(analysis.parenting_warning_bones):,} bone(s) may be parented far away or to the opposite side: {preview}. "
            "This may produce unexpected results in game. Contact the model author or fix the parenting in Blender before using this tool. "
            "You can safely ignore this warning if you think this is a false positive."
        )
    if analysis.morph_count > 96:
        warnings.append(f"High morph/shapekey count: {analysis.morph_count:,}; later flex work may be slow.")
    analysis.content_warning_scan = scan_morph_content_warning(analysis.morph_names)
    if analysis.content_warning_scan.get("triggered"):
        match_count = int(analysis.content_warning_scan.get("match_count") or 0)
        matched_morph_count = int(analysis.content_warning_scan.get("matched_morph_count") or 0)
        warnings.append(
            "Potential NSFW shapekey names detected: "
            f"{match_count:,} keyword matches across {matched_morph_count:,} morphs. "
            "Auto porting is not optimized for this kind of model."
        )
    if analysis.texture_ref_count > 32:
        warnings.append(f"High native PMX texture count: {analysis.texture_ref_count:,}; material work may be slow.")
    analysis.warnings = warnings
    return analysis


def read_glb_json(path: Path) -> dict:
    """Read the JSON chunk from a GLB container (used by .vrm files)."""

    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"glTF":
            raise ValueError(f"not a GLB/VRM file: invalid magic in {path}")
        while True:
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_length, chunk_type = struct.unpack("<I4s", chunk_header)
            chunk_data = handle.read(chunk_length)
            if len(chunk_data) < chunk_length:
                raise ValueError(f"truncated GLB chunk in {path}")
            if chunk_type == b"JSON":
                return json.loads(chunk_data.decode("utf-8"))
    raise ValueError(f"no JSON chunk found in GLB/VRM file: {path}")


def vrm_extension_info(gltf: dict) -> tuple[str, dict, dict[str, int]]:
    """Return (vrm spec version, vrm extension dict, humanoid slot -> node index map)."""

    extensions = gltf.get("extensions") or {}
    vrm1 = extensions.get("VRMC_vrm")
    if isinstance(vrm1, dict):
        bone_map: dict[str, int] = {}
        human_bones = (vrm1.get("humanoid") or {}).get("humanBones") or {}
        if isinstance(human_bones, dict):
            for slot, info in human_bones.items():
                node = info.get("node") if isinstance(info, dict) else None
                if isinstance(node, int):
                    bone_map[str(slot)] = node
        return str(vrm1.get("specVersion") or "1.0"), vrm1, bone_map
    vrm0 = extensions.get("VRM")
    if isinstance(vrm0, dict):
        bone_map = {}
        entries = (vrm0.get("humanoid") or {}).get("humanBones") or []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("node"), int) and entry.get("bone"):
                    bone_map.setdefault(str(entry["bone"]), entry["node"])
        return str(vrm0.get("specVersion") or "0.0"), vrm0, bone_map
    return "", {}, {}


def vrm_morph_groups(vrm_version: str, vrm_extension: dict, gltf: dict) -> list[str]:
    """Return VRM blendshape/expression group names (or per-mesh target names as fallback)."""

    names: list[str] = []
    if vrm_version.startswith("1"):
        expressions = vrm_extension.get("expressions") or {}
        for section in ("preset", "custom"):
            section_data = expressions.get(section)
            if isinstance(section_data, dict):
                names.extend(str(key) for key in section_data.keys())
        if names:
            return names
    else:
        groups = (vrm_extension.get("blendShapeMaster") or {}).get("blendShapeGroups") or []
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict):
                    label = str(group.get("name") or group.get("presetName") or "").strip()
                    if label:
                        names.append(label)
        if names:
            return names
    for mesh in gltf.get("meshes") or []:
        target_names = (mesh.get("extras") or {}).get("targetNames")
        if isinstance(target_names, list):
            names.extend(str(name) for name in target_names if str(name).strip())
        else:
            target_count = max(
                (len(primitive.get("targets") or []) for primitive in mesh.get("primitives") or []),
                default=0,
            )
            names.extend(f"{mesh.get('name') or 'mesh'}_morph_{index}" for index in range(target_count))
    return names


def analyze_vrm(vrm_path: Path, source_dir: Path | None = None) -> PmxAnalysis:
    vrm_path = vrm_path.resolve()
    source_dir = (source_dir or vrm_path.parent).resolve()
    if not vrm_path.exists():
        raise FileNotFoundError(vrm_path)
    if not vrm_path.is_file():
        raise FileNotFoundError(f"VRM path is not a file: {vrm_path}")

    analysis = PmxAnalysis(pmx_path=str(vrm_path), source_dir=str(source_dir))
    gltf = read_glb_json(vrm_path)
    vrm_version, vrm_extension, bone_map = vrm_extension_info(gltf)
    meta = vrm_extension.get("meta") or {}
    model_name = str(meta.get("name") or meta.get("title") or "").strip() or vrm_path.stem
    analysis.model_name = model_name
    analysis.model_name_english = model_name
    analysis.version = vrm_version or "0.0"
    analysis.encoding = "glb"

    accessors = gltf.get("accessors") or []
    bounds_min = [float("inf"), float("inf"), float("inf")]
    bounds_max = [float("-inf"), float("-inf"), float("-inf")]
    for mesh in gltf.get("meshes") or []:
        for primitive in mesh.get("primitives") or []:
            position_index = (primitive.get("attributes") or {}).get("POSITION")
            if isinstance(position_index, int) and 0 <= position_index < len(accessors):
                accessor = accessors[position_index]
                analysis.vertex_count += int(accessor.get("count") or 0)
                accessor_min = accessor.get("min")
                accessor_max = accessor.get("max")
                if isinstance(accessor_min, list) and isinstance(accessor_max, list) and len(accessor_min) >= 3 and len(accessor_max) >= 3:
                    for axis in range(3):
                        bounds_min[axis] = min(bounds_min[axis], float(accessor_min[axis]))
                        bounds_max[axis] = max(bounds_max[axis], float(accessor_max[axis]))
            indices_index = primitive.get("indices")
            if isinstance(indices_index, int) and 0 <= indices_index < len(accessors):
                analysis.face_count += int(accessors[indices_index].get("count") or 0) // 3
    if analysis.vertex_count > MAX_SUPPORTED_PMX_VERTEX_COUNT:
        raise RuntimeError(
            f"VRM vertex count {analysis.vertex_count:,} exceeds the supported limit of "
            f"{MAX_SUPPORTED_PMX_VERTEX_COUNT:,}. This model is too large for the importer."
        )
    # glTF is Y-up like PMX, so the model height is the Y extent (axis index 1).
    model_height = (
        bounds_max[1] - bounds_min[1]
        if all(math.isfinite(value) for value in (bounds_min[1], bounds_max[1]))
        else 0.0
    )

    joint_nodes: set[int] = set()
    for skin in gltf.get("skins") or []:
        joint_nodes.update(index for index in skin.get("joints") or [] if isinstance(index, int))
    analysis.bone_count = len(joint_nodes)

    morph_names = vrm_morph_groups(vrm_version, vrm_extension, gltf)
    analysis.morph_count = len(morph_names)
    analysis.morph_names = morph_names

    analysis.material_count = len(gltf.get("materials") or [])
    image_count = len(gltf.get("images") or [])
    analysis.texture_ref_count = image_count
    analysis.texture_file_count = image_count
    analysis.resolved_texture_count = image_count

    warnings: list[str] = []
    if not bone_map:
        analysis.missing_required_skeleton_bones = list(VRM_REQUIRED_HUMANOID_BONES)
        warnings.append(
            "VRM humanoid extension is missing: no humanoid bone mapping was found. "
            "Import is expected to fail because the skeleton cannot be converted to a humanoid rig."
        )
    else:
        missing_slots = [slot for slot in VRM_REQUIRED_HUMANOID_BONES if slot not in bone_map]
        analysis.missing_required_skeleton_bones = missing_slots
        if missing_slots:
            warnings.append(
                "VRM humanoid bone mapping is incomplete: missing required humanoid bones: "
                + ", ".join(missing_slots)
            )
    if analysis.vertex_count <= 0:
        warnings.append("No mesh vertices were found in the VRM file.")
    if model_height <= 0.0:
        warnings.append("Could not determine the model height from the VRM position accessors.")
    if analysis.vertex_count > HIGH_PMX_VERTEX_WARNING_THRESHOLD:
        warnings.append(
            f"High vertex count: {analysis.vertex_count:,} vertices; "
            "importing may take a long time and produce unexpected results."
        )
    if analysis.bone_count > 600:
        warnings.append(f"High bone count: {analysis.bone_count:,} bones; importing may be slow.")
    if analysis.morph_count > 96:
        warnings.append(f"High morph/shapekey count: {analysis.morph_count:,}; later flex work may be slow.")
    analysis.warnings = warnings
    return analysis


def analyze_model_file(path: Path, source_dir: Path | None = None) -> PmxAnalysis:
    """Analyze a supported model file (.pmx or .vrm) by extension."""

    if path.suffix.lower() == ".vrm":
        return analyze_vrm(path, source_dir)
    return analyze_pmx(path, source_dir)


def build_workspace(pmx_path: Path, source_dir: Path, analysis: PmxAnalysis | None = None, workspace_root: Path | None = None) -> Workspace:
    source_dir = source_dir.resolve()
    pmx_path = pmx_path.resolve()
    if analysis is None:
        analysis = analyze_model_file(pmx_path, source_dir)
    model_label = analysis.model_name_english or analysis.model_name or pmx_path.stem
    slug = slugify(model_label)
    digest = file_sha1(pmx_path)[:10]
    workspace_name = f"{slug}_{digest}"
    workspace_base = workspace_root or workspaces_root()
    root = workspace_base if workspace_base.name.lower() == workspace_name.lower() else workspace_base / workspace_name
    source_assets_dir = root / "0_source_mmd_assets"
    import_dir = root / "1_import_mmd_model"
    fix_dir = root / "2_fix_model_source_skeleton"
    try:
        rel_pmx = pmx_path.relative_to(source_dir)
    except ValueError as exc:
        raise RuntimeError(
            f"The PMX file {pmx_path} is not inside the selected model folder {source_dir}. "
            "Select the folder that contains the PMX file as the source directory."
        ) from exc
    copied_pmx = source_assets_dir / rel_pmx
    blend_path = import_dir / f"{slugify(pmx_path.stem)}_import.blend"
    fixed_blend_path = fix_dir / f"{slugify(pmx_path.stem)}_fixed.blend"
    return Workspace(
        root=root,
        source_assets_dir=source_assets_dir,
        import_dir=import_dir,
        copied_pmx=copied_pmx,
        blend_path=blend_path,
        import_log_path=import_dir / "blender_import.log",
        import_report_path=import_dir / "blender_import_report.json",
        preflight_report_path=import_dir / "pmx_preflight_report.json",
        fix_dir=fix_dir,
        fixed_blend_path=fixed_blend_path,
        fix_log_path=fix_dir / "blender_fix_model.log",
        fix_report_path=fix_dir / "blender_fix_model_report.json",
    )


def fix_paths_for_import_blend(input_blend: Path) -> tuple[Path, Path, Path]:
    input_blend = input_blend.resolve()
    workspace_root = input_blend.parent.parent if input_blend.parent.name == "1_import_mmd_model" else input_blend.parent
    fix_dir = workspace_root / "2_fix_model_source_skeleton"
    stem = input_blend.stem
    if stem.endswith("_import"):
        stem = stem[: -len("_import")]
    output_blend = fix_dir / f"{slugify(stem)}_fixed.blend"
    return fix_dir, output_blend, fix_dir / "blender_fix_model_report.json"


def spine_paths_for_fixed_blend(input_blend: Path) -> tuple[Path, Path, Path, Path, Path]:
    input_blend = input_blend.resolve()
    workspace_root = input_blend.parent.parent if input_blend.parent.name == "2_fix_model_source_skeleton" else input_blend.parent
    spine_dir = workspace_root / "3_fix_spine_bones"
    stem = input_blend.stem
    if stem.endswith("_fixed"):
        stem = stem[: -len("_fixed")]
    output_blend = spine_dir / f"{slugify(stem)}_spine_fixed.blend"
    analysis_json = spine_dir / "spine_analysis.json"
    plan_json = spine_dir / "spine_fix_plan.json"
    report_json = spine_dir / "blender_fix_spine_bones_report.json"
    return spine_dir, output_blend, analysis_json, plan_json, report_json


def sort_bones_paths_for_spine_blend(input_blend: Path) -> tuple[Path, Path, Path, Path, Path]:
    input_blend = input_blend.resolve()
    workspace_root = input_blend.parent.parent if input_blend.parent.name in {"3_fix_spine_bones", "4_sort_bones"} else input_blend.parent
    sort_dir = workspace_root / "4_sort_bones"
    stem = input_blend.stem
    if stem.endswith("_spine_fixed"):
        stem = stem[: -len("_spine_fixed")]
    if stem.endswith("_bones_manual_merge"):
        stem = stem[: -len("_bones_manual_merge")]
    if stem.endswith("_bones_sorted"):
        stem = stem[: -len("_bones_sorted")]
    output_blend = sort_dir / f"{slugify(stem)}_bones_sorted.blend"
    analysis_json = sort_dir / "bone_merge_analysis.json"
    plan_json = sort_dir / "bone_merge_plan.json"
    report_json = sort_dir / "blender_sort_bones_report.json"
    return sort_dir, output_blend, analysis_json, plan_json, report_json


def sort_bones_manual_merge_path_for_spine_blend(input_blend: Path) -> Path:
    input_blend = input_blend.resolve()
    if input_blend.parent.name == "4_sort_bones" and input_blend.stem.endswith("_bones_manual_merge"):
        return input_blend
    sort_dir, _output_blend, _analysis_json, _plan_json, _report_json = sort_bones_paths_for_spine_blend(input_blend)
    stem = input_blend.stem
    if stem.endswith("_spine_fixed"):
        stem = stem[: -len("_spine_fixed")]
    if stem.endswith("_bones_sorted"):
        stem = stem[: -len("_bones_sorted")]
    if stem.endswith("_bones_manual_merge"):
        stem = stem[: -len("_bones_manual_merge")]
    return sort_dir / f"{slugify(stem)}_bones_manual_merge.blend"


def material_paths_for_sort_bones_blend(input_blend: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path, Path]:
    input_blend = input_blend.resolve()
    workspace_root = input_blend.parent.parent if input_blend.parent.name == "4_sort_bones" else input_blend.parent
    material_dir = workspace_root / "5_sort_materials"
    stem = input_blend.stem
    if stem.endswith("_bones_sorted"):
        stem = stem[: -len("_bones_sorted")]
    output_blend = material_dir / f"{slugify(stem)}_materials_sorted.blend"
    final_blend = material_dir / f"{slugify(stem)}_materials_merged.blend"
    scan_json = material_dir / "material_scan.json"
    plan_json = material_dir / "material_plan.json"
    merge_plan_json = material_dir / "material_merge_plan.json"
    report_json = material_dir / "material_initial_report.json"
    merge_report_json = material_dir / "material_merge_report.json"
    return material_dir, output_blend, final_blend, scan_json, plan_json, merge_plan_json, report_json, merge_report_json


def bodygroup_paths_for_material_blend(input_blend: Path) -> tuple[Path, Path, Path, Path, Path]:
    input_blend = input_blend.resolve()
    workspace_root = input_blend.parent.parent if input_blend.parent.name == "5_sort_materials" else input_blend.parent
    bodygroup_dir = workspace_root / "6_sort_bodygroups"
    stem = input_blend.stem
    for suffix in ("_materials_merged", "_materials_sorted"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    output_blend = bodygroup_dir / f"{slugify(stem)}_bodygroups_sorted.blend"
    analysis_json = bodygroup_dir / "bodygroup_analysis.json"
    plan_json = bodygroup_dir / "bodygroup_plan.json"
    report_json = bodygroup_dir / "blender_sort_bodygroups_report.json"
    return bodygroup_dir, output_blend, analysis_json, plan_json, report_json


def bodygroup_manual_edit_path_for_material_blend(input_blend: Path) -> Path:
    bodygroup_dir, output_blend, _analysis_json, _plan_json, _report_json = bodygroup_paths_for_material_blend(input_blend)
    stem = output_blend.stem
    if stem.endswith("_bodygroups_sorted"):
        stem = stem[: -len("_bodygroups_sorted")]
    return bodygroup_dir / f"{stem}_bodygroups_manual_edit.blend"


def bodygroup_paths_for_manual_edit_blend(manual_edit_blend: Path) -> tuple[Path, Path, Path, Path, Path]:
    manual_edit_blend = manual_edit_blend.resolve()
    bodygroup_dir = manual_edit_blend.parent
    stem = manual_edit_blend.stem
    if stem.endswith("_bodygroups_manual_edit"):
        stem = stem[: -len("_bodygroups_manual_edit")]
    output_blend = bodygroup_dir / f"{stem}_bodygroups_sorted.blend"
    analysis_json = bodygroup_dir / "bodygroup_analysis.json"
    plan_json = bodygroup_dir / "bodygroup_plan.json"
    report_json = bodygroup_dir / "blender_sort_bodygroups_report.json"
    return bodygroup_dir, output_blend, analysis_json, plan_json, report_json


def flex_paths_for_bodygroup_blend(input_blend: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    input_blend = input_blend.resolve()
    workspace_root = input_blend.parent.parent if input_blend.parent.name == "6_sort_bodygroups" else input_blend.parent
    flex_dir = workspace_root / "7_sort_flexes"
    stem = input_blend.stem
    if stem.endswith("_bodygroups_sorted"):
        stem = stem[: -len("_bodygroups_sorted")]
    output_blend = flex_dir / f"{slugify(stem)}_flexes_sorted.blend"
    analysis_json = flex_dir / "flex_analysis.json"
    plan_json = flex_dir / "flex_plan.json"
    report_json = flex_dir / "blender_sort_flexes_report.json"
    flexes_json = flex_dir / "flexes.json"
    return flex_dir, output_blend, analysis_json, plan_json, report_json, flexes_json


def collision_paths_for_flex_blend(input_blend: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    input_blend = input_blend.resolve()
    workspace_root = input_blend.parent.parent if input_blend.parent.name == "7_sort_flexes" else input_blend.parent
    collision_dir = workspace_root / "8_sort_collision"
    stem = input_blend.stem
    if stem.endswith("_flexes_sorted"):
        stem = stem[: -len("_flexes_sorted")]
    output_blend = collision_dir / f"{slugify(stem)}_collision_sorted.blend"
    analysis_json = collision_dir / "collision_analysis.json"
    plan_json = collision_dir / "collision_plan.json"
    report_json = collision_dir / "blender_sort_collision_report.json"
    physics_settings_json = collision_dir / "physics_settings.json"
    physics_smd = collision_dir / "Physics.smd"
    return collision_dir, output_blend, analysis_json, plan_json, report_json, physics_settings_json, physics_smd


def collision_sources_path_for_flex_blend(input_blend: Path) -> Path:
    collision_dir, *_rest = collision_paths_for_flex_blend(input_blend)
    return collision_dir / "collision_sources.json"


def collision_bones_path_for_flex_blend(input_blend: Path) -> Path:
    collision_dir, *_rest = collision_paths_for_flex_blend(input_blend)
    return collision_dir / "collision_bones.json"


def collision_bone_selection_path_for_flex_blend(input_blend: Path) -> Path:
    collision_dir, *_rest = collision_paths_for_flex_blend(input_blend)
    return collision_dir / "collision_bone_selection.json"


def proportion_paths_for_collision_blend(input_blend: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path, Path, Path]:
    input_blend = input_blend.resolve()
    # Step 9 normally runs on the Step 8 collision blend (in 8_sort_collision). SFM skips Step 8,
    # so it runs on the Step 7 flex blend (in 7_sort_flexes). In both cases the export folder must
    # land at the WORKSPACE ROOT, not nested inside the step dir.
    workspace_root = (
        input_blend.parent.parent
        if input_blend.parent.name in ("8_sort_collision", "7_sort_flexes")
        else input_blend.parent
    )
    proportion_dir = workspace_root / "9_export_proportion_trick"
    raw_dir = proportion_dir / "0_pre_proportion_raw_export"
    workspace_dir = proportion_dir / "1_proportion_workspace"
    final_dir = proportion_dir / "2_proportion_export"
    stem = input_blend.stem
    for suffix in ("_collision_sorted", "_flexes_sorted"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = slugify(stem)
    pre_blend = workspace_dir / f"{stem}_pre_proportion.blend"
    processed_blend = workspace_dir / f"{stem}_proportion_processed.blend"
    report_json = proportion_dir / "proportion_export_report.json"
    files_json = proportion_dir / "proportion_export_files.json"
    log_path = proportion_dir / "blender_export_proportion_trick.log"
    return proportion_dir, raw_dir, workspace_dir, final_dir, pre_blend, processed_blend, report_json, files_json, log_path


def carms_paths_for_proportion_export(input_dir: Path) -> tuple[Path, Path, Path, Path, Path]:
    input_dir = input_dir.resolve()
    if input_dir.name == "9_export_proportion_trick":
        final_dir = input_dir / "2_proportion_export"
        workspace_root = input_dir.parent
    elif input_dir.name == "2_proportion_export" and input_dir.parent.name == "9_export_proportion_trick":
        final_dir = input_dir
        workspace_root = input_dir.parent.parent
    else:
        final_dir = input_dir
        workspace_root = input_dir.parent
    carms_dir = workspace_root / "10_sort_c_arms"
    model_stem = slugify(workspace_root.name)
    proportion_workspace = workspace_root / "9_export_proportion_trick" / "1_proportion_workspace"
    try:
        processed = sorted(proportion_workspace.glob("*_proportion_processed.blend"), key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        processed = []
    if processed:
        model_stem = processed[0].stem
        if model_stem.endswith("_proportion_processed"):
            model_stem = model_stem[: -len("_proportion_processed")]
        model_stem = slugify(model_stem)
    workspace_blend = carms_dir / f"{model_stem}_c_arms.blend"
    report_json = carms_dir / "c_arms_report.json"
    files_json = carms_dir / "c_arms_files.json"
    return final_dir, carms_dir, workspace_blend, report_json, files_json


def vrd_paths_for_proportion_export(input_dir: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    input_dir = input_dir.resolve()
    if input_dir.name == "9_export_proportion_trick":
        final_dir = input_dir / "2_proportion_export"
        workspace_root = input_dir.parent
    elif input_dir.name == "2_proportion_export" and input_dir.parent.name == "9_export_proportion_trick":
        final_dir = input_dir
        workspace_root = input_dir.parent.parent
    else:
        final_dir = input_dir
        workspace_root = input_dir.parent
    vrd_dir = workspace_root / "11_sort_vrd"
    model_stem = slugify(workspace_root.name)
    proportion_workspace = workspace_root / "9_export_proportion_trick" / "1_proportion_workspace"
    try:
        processed = sorted(proportion_workspace.glob("*_proportion_processed.blend"), key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        processed = []
    if processed:
        model_stem = processed[0].stem
        if model_stem.endswith("_proportion_processed"):
            model_stem = model_stem[: -len("_proportion_processed")]
        model_stem = slugify(model_stem)
    workspace_blend = vrd_dir / f"{model_stem}_vrd.blend"
    analysis_json = vrd_dir / "vrd_analysis.json"
    plan_json = vrd_dir / "vrd_plan.json"
    preview_json = vrd_dir / "vrd_preview.json"
    report_json = vrd_dir / "blender_sort_vrd_report.json"
    vrd_path = vrd_dir / "vrd.vrd"
    return final_dir, vrd_dir, workspace_blend, analysis_json, plan_json, preview_json, report_json, vrd_path


def texture_paths_for_material_input(input_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    input_path = input_path.resolve()
    if input_path.is_file():
        material_dir = input_path.parent
        workspace_root = material_dir.parent if material_dir.name == "5_sort_materials" else material_dir
    elif input_path.name == "5_sort_materials":
        material_dir = input_path
        workspace_root = input_path.parent
    elif (input_path / "5_sort_materials").exists():
        workspace_root = input_path
        material_dir = input_path / "5_sort_materials"
    else:
        workspace_root = input_path
        material_dir = input_path
    texture_dir = workspace_root / "12_param_texture_render_materials"
    analysis_json = texture_dir / "textures_analysis.json"
    plan_json = texture_dir / "textures_plan.json"
    report_json = texture_dir / "textures_report.json"
    manifest_json = texture_dir / "textures_manifest.json"
    log_path = texture_dir / "textures.log"
    return texture_dir, analysis_json, plan_json, report_json, manifest_json, log_path


def workspace_root_for_step1_input(input_path: Path) -> Path:
    input_path = input_path.resolve()
    if input_path.is_file():
        candidates = [input_path.parent] + list(input_path.parents)
    else:
        candidates = [input_path] + list(input_path.parents)
    for candidate in candidates:
        if candidate.name == "0_source_mmd_assets":
            return candidate.parent
        if (candidate / "0_source_mmd_assets").exists():
            return candidate
        if candidate.name == "1_import_mmd_model":
            return candidate.parent
    return input_path.parent if input_path.is_file() else input_path


def workspace_model_format(path: Path) -> str:
    """Read the step-1 import report's "format" ("pmx"/"vrm") for the workspace containing ``path``."""
    try:
        path = path.resolve()
        for ancestor in [path] + list(path.parents):
            report_path = ancestor / "1_import_mmd_model" / "blender_import_report.json"
            if report_path.exists():
                report = json.loads(report_path.read_text(encoding="utf-8"))
                return str(report.get("format") or "pmx").strip().lower() or "pmx"
    except Exception:
        pass
    return "pmx"


def icon_paths_for_step1_input(input_path: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    workspace_root = workspace_root_for_step1_input(input_path)
    icon_dir = workspace_root / "13_sort_icons_and_arts"
    analysis_json = icon_dir / "icons_analysis.json"
    plan_json = icon_dir / "icons_plan.json"
    report_json = icon_dir / "icons_report.json"
    files_json = icon_dir / "icons_files.json"
    render_report_json = icon_dir / "blender_render_icon_report.json"
    log_path = icon_dir / "blender_render_icon.log"
    return icon_dir, analysis_json, plan_json, report_json, files_json, render_report_json, log_path


def workspace_root_for_step9_input(input_path: Path) -> tuple[Path, Path]:
    input_path = input_path.resolve()
    if input_path.name == "2_proportion_export" and input_path.parent.name == "9_export_proportion_trick":
        return input_path.parent.parent, input_path
    if input_path.name == "9_export_proportion_trick":
        return input_path.parent, input_path / "2_proportion_export"
    if (input_path / "9_export_proportion_trick" / "2_proportion_export").exists():
        return input_path, input_path / "9_export_proportion_trick" / "2_proportion_export"
    if input_path.is_file():
        return input_path.parent, input_path.parent
    return input_path.parent, input_path


def qc_paths_for_step9_input(input_path: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    workspace_root, final_dir = workspace_root_for_step9_input(input_path)
    qc_dir = workspace_root / "14_sort_qc_compile"
    analysis_json = qc_dir / "qc_analysis.json"
    plan_json = qc_dir / "qc_plan.json"
    report_json = qc_dir / "qc_report.json"
    files_json = qc_dir / "qc_files.json"
    log_path = qc_dir / "qc_compile.log"
    return final_dir, qc_dir, analysis_json, plan_json, report_json, files_json, log_path


def workspace_root_for_step14_input(input_path: Path) -> Path:
    path = input_path.resolve()
    if path.is_file():
        path = path.parent
    if path.name == "15_sort_release_description":
        return path.parent
    if path.name == "14_sort_qc_compile":
        return path.parent
    if (path / "14_sort_qc_compile").exists():
        return path
    for candidate in [path] + list(path.parents):
        if (candidate / "14_sort_qc_compile").exists():
            return candidate
    return path


def release_paths_for_step14_input(input_path: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path, Path]:
    workspace_root = workspace_root_for_step14_input(input_path)
    release_dir = workspace_root / "15_sort_release_description"
    analysis_json = release_dir / "release_description_analysis.json"
    plan_json = release_dir / "release_description_plan.json"
    report_json = release_dir / "release_description_report.json"
    files_json = release_dir / "release_description_files.json"
    translations_json = release_dir / "translations.json"
    template_path = release_dir / "Translation Templates Write.txt"
    log_path = release_dir / "release_description.log"
    return release_dir, analysis_json, plan_json, report_json, files_json, translations_json, template_path, log_path


def copy_asset_tree(source_dir: Path, target_dir: Path, progress: ProgressCallback | None = None) -> None:
    source_dir = source_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    files = [path for path in source_dir.rglob("*") if path.is_file()]
    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
    if total_bytes > 2 * 1024 ** 3:
        emit(
            progress,
            f"WARNING: The selected model folder contains {len(files):,} files "
            f"({total_bytes / 1024 ** 3:.1f} GB) and the whole folder is copied into the workspace. "
            "Select the specific model's own folder to avoid duplicating unrelated files.",
        )
    copied = 0
    last_report = time.monotonic()
    for index, path in enumerate(files, start=1):
        rel = path.relative_to(source_dir)
        target = target_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        skip = False
        if target.exists():
            try:
                source_stat = path.stat()
                target_stat = target.stat()
                if source_stat.st_size == target_stat.st_size and int(source_stat.st_mtime) == int(target_stat.st_mtime):
                    skip = True
            except OSError:
                pass
        if not skip:
            shutil.copy2(path, target)
            copied += 1
        now = time.monotonic()
        if now - last_report > 2.0:
            emit(progress, f"Copying source assets: {index:,} / {len(files):,} files")
            last_report = now
    emit(progress, f"Copied/updated {copied} source asset files into {target_dir}")


def import_pmx_to_blender(
    pmx_path: Path,
    source_dir: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    workspace_root: Path | None = None,
    analysis: PmxAnalysis | None = None,
) -> ImportResult:
    pmx_path = pmx_path.resolve()
    source_dir = source_dir.resolve()
    if not path_is_under(pmx_path, source_dir):
        raise RuntimeError(
            f"The PMX file {pmx_path} is not inside the selected model folder {source_dir}. "
            "Select the folder that contains the PMX file as the source directory."
        )
    if analysis is None:
        analysis = analyze_model_file(pmx_path, source_dir)
    workspace = build_workspace(pmx_path, source_dir, analysis, workspace_root=workspace_root)
    workspace.import_dir.mkdir(parents=True, exist_ok=True)
    workspace.preflight_report_path.write_text(analysis.to_json(), encoding="utf-8")

    copy_asset_tree(source_dir, workspace.source_assets_dir, progress)
    if not workspace.copied_pmx.exists():
        raise FileNotFoundError(f"copied PMX was not found: {workspace.copied_pmx}")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    is_vrm = pmx_path.suffix.lower() == ".vrm"
    import_script = BLENDER_VRM_IMPORT_SCRIPT if is_vrm else BLENDER_IMPORT_SCRIPT
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(import_script),
        "--",
        "--vrm" if is_vrm else "--pmx",
        str(workspace.copied_pmx),
        "--output-blend",
        str(workspace.blend_path),
        "--report-json",
        str(workspace.import_report_path),
    ]
    if is_vrm:
        command += ["--textures-dir", str(workspace.source_assets_dir / "vrm_textures")]
    emit(progress, f"Starting Blender import: {workspace.copied_pmx}")
    started = time.monotonic()
    run_process_streamed(
        command,
        progress=progress,
        log_path=workspace.import_log_path,
        cancel_check=cancel_check,
    )
    if not workspace.blend_path.exists():
        raise RuntimeError(f"Blender completed but did not write {workspace.blend_path}")
    if not workspace.import_report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {workspace.import_report_path}")
    emit(progress, f"Blender import finished in {time.monotonic() - started:.1f}s")
    return ImportResult(workspace=workspace, setup=setup, command=command)


def fix_imported_blend(
    input_blend: Path,
    output_blend: Path | None = None,
    clear_custom_normals: bool = False,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> FixResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_FIX_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_FIX_SCRIPT)

    fix_dir, default_output_blend, default_report = fix_paths_for_import_blend(input_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    fix_dir = output_blend.parent
    fix_log_path = fix_dir / "blender_fix_model.log"
    fix_report_path = default_report if output_blend == default_output_blend else fix_dir / "blender_fix_model_report.json"
    fix_dir.mkdir(parents=True, exist_ok=True)

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_FIX_SCRIPT),
        "--",
        "--input-blend",
        str(input_blend),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(fix_report_path),
    ]
    command.append("--clear-custom-normals" if clear_custom_normals else "--keep-custom-normals")
    if workspace_model_format(input_blend) == "vrm":
        command.append("--vrm-spine-merge")
        emit(progress, "VRM model detected: Spine2 will be merged into Spine1 after the ValveBiped conversion.")
    emit(progress, f"Starting Blender fix step: {input_blend}")
    started = time.monotonic()
    run_process_streamed(
        command,
        progress=progress,
        log_path=fix_log_path,
        cancel_check=cancel_check,
    )
    if not output_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {output_blend}")
    if not fix_report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {fix_report_path}")
    emit(progress, f"Blender fix step finished in {time.monotonic() - started:.1f}s")
    return FixResult(
        input_blend=input_blend,
        output_blend=output_blend,
        fix_dir=fix_dir,
        fix_log_path=fix_log_path,
        fix_report_path=fix_report_path,
        setup=setup,
        command=command,
    )


def analyze_spine_blend(
    input_blend: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> SpineAnalysisResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SPINE_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SPINE_SCRIPT)

    spine_dir, _output_blend, analysis_path, plan_path, _report_path = spine_paths_for_fixed_blend(input_blend)
    log_path = spine_dir / "blender_fix_spine_bones.log"
    spine_dir.mkdir(parents=True, exist_ok=True)

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SPINE_SCRIPT),
        "--",
        "--mode",
        "analyze",
        "--input-blend",
        str(input_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
    ]
    emit(progress, f"Starting Blender spine analysis: {input_blend}")
    started = time.monotonic()
    run_process_streamed(
        command,
        progress=progress,
        log_path=log_path,
        cancel_check=cancel_check,
    )
    if not analysis_path.exists():
        raise RuntimeError(f"Blender completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender spine analysis finished in {time.monotonic() - started:.1f}s")
    return SpineAnalysisResult(
        input_blend=input_blend,
        spine_dir=spine_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        setup=setup,
        command=command,
    )


def fix_spine_blend(
    input_blend: Path,
    plan: dict[str, object] | Path,
    output_blend: Path | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> SpineFixResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SPINE_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SPINE_SCRIPT)

    spine_dir, default_output_blend, analysis_path, default_plan_path, report_path = spine_paths_for_fixed_blend(input_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    spine_dir = output_blend.parent
    log_path = spine_dir / "blender_fix_spine_bones.log"
    plan_path = default_plan_path if output_blend == default_output_blend else spine_dir / "spine_fix_plan.json"
    report_path = report_path if output_blend == default_output_blend else spine_dir / "blender_fix_spine_bones_report.json"
    spine_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SPINE_SCRIPT),
        "--",
        "--mode",
        "apply",
        "--input-blend",
        str(input_blend),
        "--plan-json",
        str(plan_path),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
    ]
    emit(progress, f"Starting Blender spine fix step: {input_blend}")
    started = time.monotonic()
    run_process_streamed(
        command,
        progress=progress,
        log_path=log_path,
        cancel_check=cancel_check,
    )
    if not output_blend.exists():
        raise_missing_output(output_blend, log_path=log_path, command=command)
    if not report_path.exists():
        raise_missing_output(report_path, log_path=log_path, command=command)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender spine fix step finished in {time.monotonic() - started:.1f}s")
    return SpineFixResult(
        input_blend=input_blend,
        output_blend=output_blend,
        spine_dir=spine_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        report_path=report_path,
        report=report,
        setup=setup,
        command=command,
    )


# Per-game bone budget mirror of blender_sort_bones.GAME_BONE_LIMITS. Used to
# resolve the limit for callers (e.g. the full auto-port) that do not pass one,
# so L4D2 mode merges down to 126 while GMod stays at 254.
SORT_BONES_GAME_LIMITS = {"gmod": 254, "l4d2": 126}


def _resolve_sort_bones_limit(limit: int | None, game: str) -> int:
    if limit is not None:
        return int(limit)
    return SORT_BONES_GAME_LIMITS.get(game, 254)


def analyze_sort_bones_blend(
    input_blend: Path,
    limit: int | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    game: str = "gmod",
) -> SortBonesAnalysisResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_BONES_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_BONES_SCRIPT)

    sort_dir, _output_blend, analysis_path, plan_path, _report_path = sort_bones_paths_for_spine_blend(input_blend)
    log_path = sort_dir / "blender_sort_bones.log"
    sort_dir.mkdir(parents=True, exist_ok=True)

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_BONES_SCRIPT),
        "--",
        "--mode",
        "analyze",
        "--input-blend",
        str(input_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
        "--limit",
        str(_resolve_sort_bones_limit(limit, game)),
    ]
    # Only L4D2 adds --game so the GMod command line stays byte-identical.
    if game == "l4d2":
        command += ["--game", game]
    emit(progress, f"Starting Blender sort bones analysis: {input_blend}")
    started = time.monotonic()
    run_process_streamed(
        command,
        progress=progress,
        log_path=log_path,
        cancel_check=cancel_check,
    )
    if not analysis_path.exists():
        raise RuntimeError(f"Blender completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender sort bones analysis finished in {time.monotonic() - started:.1f}s")
    return SortBonesAnalysisResult(
        input_blend=input_blend,
        sort_dir=sort_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        setup=setup,
        command=command,
    )


def sort_bones_blend(
    input_blend: Path,
    plan: dict[str, object] | Path,
    output_blend: Path | None = None,
    limit: int | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    game: str = "gmod",
) -> SortBonesResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_BONES_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_BONES_SCRIPT)

    sort_dir, default_output_blend, analysis_path, default_plan_path, report_path = sort_bones_paths_for_spine_blend(input_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    sort_dir = output_blend.parent
    log_path = sort_dir / "blender_sort_bones.log"
    plan_path = default_plan_path if output_blend == default_output_blend else sort_dir / "bone_merge_plan.json"
    report_path = report_path if output_blend == default_output_blend else sort_dir / "blender_sort_bones_report.json"
    sort_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_BONES_SCRIPT),
        "--",
        "--mode",
        "apply",
        "--input-blend",
        str(input_blend),
        "--plan-json",
        str(plan_path),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
        "--limit",
        str(_resolve_sort_bones_limit(limit, game)),
    ]
    # Only L4D2 adds --game so the GMod command line stays byte-identical.
    if game == "l4d2":
        command += ["--game", game]
    emit(progress, f"Starting Blender sort bones step: {input_blend}")
    started = time.monotonic()
    run_process_streamed(
        command,
        progress=progress,
        log_path=log_path,
        cancel_check=cancel_check,
    )
    if not output_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {output_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender sort bones step finished in {time.monotonic() - started:.1f}s")
    return SortBonesResult(
        input_blend=input_blend,
        output_blend=output_blend,
        sort_dir=sort_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        report_path=report_path,
        report=report,
        setup=setup,
        command=command,
    )


def scan_materials_blend(
    input_blend: Path,
    limit: int = 32,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> MaterialScanResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_MATERIALS_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_MATERIALS_SCRIPT)

    material_dir, _output_blend, _final_blend, scan_path, plan_path, _merge_plan_path, _report_path, _merge_report_path = material_paths_for_sort_bones_blend(input_blend)
    log_path = material_dir / "blender_sort_materials.log"
    material_dir.mkdir(parents=True, exist_ok=True)

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_MATERIALS_SCRIPT),
        "--",
        "--mode",
        "scan",
        "--input-blend",
        str(input_blend),
        "--scan-json",
        str(scan_path),
        "--plan-json",
        str(plan_path),
        "--limit",
        str(int(limit)),
    ]
    emit(progress, f"Starting Blender material scan: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not scan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {scan_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender material scan finished in {time.monotonic() - started:.1f}s")
    return MaterialScanResult(
        input_blend=input_blend,
        material_dir=material_dir,
        scan_path=scan_path,
        plan_path=plan_path,
        log_path=log_path,
        scan=scan,
        plan=plan,
        setup=setup,
        command=command,
    )


def apply_materials_initial_blend(
    input_blend: Path,
    plan: dict[str, object] | Path,
    output_blend: Path | None = None,
    limit: int = 32,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> MaterialApplyResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_MATERIALS_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_MATERIALS_SCRIPT)

    material_dir, default_output_blend, _final_blend, scan_path, default_plan_path, merge_plan_path, report_path, _merge_report_path = material_paths_for_sort_bones_blend(input_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    material_dir = output_blend.parent
    log_path = material_dir / "blender_sort_materials.log"
    plan_path = default_plan_path if output_blend == default_output_blend else material_dir / "material_plan.json"
    report_path = report_path if output_blend == default_output_blend else material_dir / "material_initial_report.json"
    merge_plan_path = merge_plan_path if output_blend == default_output_blend else material_dir / "material_merge_plan.json"
    materials_json_path = material_dir / "materials.json"
    materials_npy_path = material_dir / "materials.npy"
    material_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_MATERIALS_SCRIPT),
        "--",
        "--mode",
        "apply-initial",
        "--input-blend",
        str(input_blend),
        "--plan-json",
        str(plan_path),
        "--merge-plan-json",
        str(merge_plan_path),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
        "--materials-json",
        str(materials_json_path),
        "--materials-npy",
        str(materials_npy_path),
        "--limit",
        str(int(limit)),
    ]
    emit(progress, f"Starting Blender material initial apply: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not output_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {output_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    if not merge_plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {merge_plan_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    merge_plan = json.loads(merge_plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender material initial apply finished in {time.monotonic() - started:.1f}s")
    return MaterialApplyResult(
        input_blend=input_blend,
        output_blend=output_blend,
        material_dir=material_dir,
        scan_path=scan_path,
        plan_path=plan_path,
        merge_plan_path=merge_plan_path,
        log_path=log_path,
        report_path=report_path,
        materials_json_path=materials_json_path,
        materials_npy_path=materials_npy_path,
        report=report,
        merge_plan=merge_plan,
        setup=setup,
        command=command,
    )


def merge_materials_blend(
    input_blend: Path,
    merge_plan: dict[str, object] | Path,
    output_blend: Path | None = None,
    limit: int = 32,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> MaterialMergeResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_MATERIALS_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_MATERIALS_SCRIPT)

    material_dir, _default_initial_blend, default_output_blend, _scan_path, _plan_path, default_merge_plan_path, _report_path, merge_report_path = material_paths_for_sort_bones_blend(input_blend)
    if input_blend.parent.name == "5_sort_materials":
        material_dir = input_blend.parent
        stem = input_blend.stem
        if stem.endswith("_materials_sorted"):
            stem = stem[: -len("_materials_sorted")]
        default_output_blend = material_dir / f"{slugify(stem)}_materials_merged.blend"
        default_merge_plan_path = material_dir / "material_merge_plan.json"
        merge_report_path = material_dir / "material_merge_report.json"
    output_blend = (output_blend or default_output_blend).resolve()
    material_dir = output_blend.parent
    log_path = material_dir / "blender_sort_materials.log"
    merge_plan_path = default_merge_plan_path if output_blend == default_output_blend else material_dir / "material_merge_plan.json"
    report_path = merge_report_path if output_blend == default_output_blend else material_dir / "material_merge_report.json"
    materials_json_path = material_dir / "materials.json"
    materials_npy_path = material_dir / "materials.npy"
    material_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(merge_plan, Path):
        merge_plan_path = merge_plan.resolve()
        if not merge_plan_path.exists():
            raise FileNotFoundError(merge_plan_path)
    else:
        merge_plan_path.write_text(json.dumps(merge_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_MATERIALS_SCRIPT),
        "--",
        "--mode",
        "merge",
        "--input-blend",
        str(input_blend),
        "--plan-json",
        str(merge_plan_path),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
        "--materials-json",
        str(materials_json_path),
        "--materials-npy",
        str(materials_npy_path),
        "--limit",
        str(int(limit)),
    ]
    emit(progress, f"Starting Blender material merge: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not output_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {output_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender material merge finished in {time.monotonic() - started:.1f}s")
    return MaterialMergeResult(
        input_blend=input_blend,
        output_blend=output_blend,
        material_dir=material_dir,
        merge_plan_path=merge_plan_path,
        log_path=log_path,
        report_path=report_path,
        materials_json_path=materials_json_path,
        materials_npy_path=materials_npy_path,
        report=report,
        setup=setup,
        command=command,
    )


def analyze_bodygroups_blend(
    input_blend: Path,
    scale_factor: float = DEFAULT_BODYGROUP_SCALE_FACTOR,
    scale_preset: str = "factor",
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    always_auto_split: bool = False,
    vertex_limit: int = DEFAULT_BODYGROUP_VERTEX_LIMIT,
) -> BodygroupAnalysisResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_BODYGROUPS_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_BODYGROUPS_SCRIPT)

    bodygroup_dir, _output_blend, analysis_path, plan_path, _report_path = bodygroup_paths_for_material_blend(input_blend)
    manual_edit_blend = bodygroup_manual_edit_path_for_material_blend(input_blend)
    log_path = bodygroup_dir / "blender_sort_bodygroups.log"
    bodygroup_dir.mkdir(parents=True, exist_ok=True)

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_BODYGROUPS_SCRIPT),
        "--",
        "--mode",
        "analyze",
        "--input-blend",
        str(input_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
        "--manual-edit-blend",
        str(manual_edit_blend),
        "--scale-factor",
        str(float(scale_factor)),
        "--scale-preset",
        str(scale_preset or "factor"),
        "--vertex-limit",
        str(int(vertex_limit)),
    ]
    if always_auto_split:
        command.append("--always-auto-split")
    emit(progress, f"Starting Blender bodygroup analysis: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not analysis_path.exists():
        raise RuntimeError(f"Blender completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    if not manual_edit_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {manual_edit_blend}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender bodygroup analysis finished in {time.monotonic() - started:.1f}s")
    return BodygroupAnalysisResult(
        input_blend=input_blend,
        bodygroup_dir=bodygroup_dir,
        manual_edit_blend=manual_edit_blend,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        setup=setup,
        command=command,
    )


def sort_bodygroups_blend(
    input_blend: Path,
    plan: dict[str, object] | Path,
    output_blend: Path | None = None,
    scale_factor: float = DEFAULT_BODYGROUP_SCALE_FACTOR,
    scale_preset: str = "factor",
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    always_auto_split: bool = False,
    vertex_limit: int = DEFAULT_BODYGROUP_VERTEX_LIMIT,
) -> BodygroupResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_BODYGROUPS_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_BODYGROUPS_SCRIPT)

    bodygroup_dir, default_output_blend, analysis_path, default_plan_path, report_path = bodygroup_paths_for_material_blend(input_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    bodygroup_dir = output_blend.parent
    log_path = bodygroup_dir / "blender_sort_bodygroups.log"
    plan_path = default_plan_path if output_blend == default_output_blend else bodygroup_dir / "bodygroup_plan.json"
    report_path = report_path if output_blend == default_output_blend else bodygroup_dir / "blender_sort_bodygroups_report.json"
    bodygroup_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_BODYGROUPS_SCRIPT),
        "--",
        "--mode",
        "apply",
        "--input-blend",
        str(input_blend),
        "--plan-json",
        str(plan_path),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
        "--scale-factor",
        str(float(scale_factor)),
        "--scale-preset",
        str(scale_preset or "factor"),
        "--vertex-limit",
        str(int(vertex_limit)),
    ]
    if always_auto_split:
        command.append("--always-auto-split")
    emit(progress, f"Starting Blender bodygroup sort: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not output_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {output_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender bodygroup sort finished in {time.monotonic() - started:.1f}s")
    return BodygroupResult(
        input_blend=input_blend,
        output_blend=output_blend,
        bodygroup_dir=bodygroup_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        report_path=report_path,
        report=report,
        setup=setup,
        command=command,
    )


def analyze_flexes_blend(
    input_blend: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    game: str = "gmod",
) -> FlexAnalysisResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_FLEXES_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_FLEXES_SCRIPT)

    flex_dir, _output_blend, analysis_path, plan_path, _report_path, _flexes_json = flex_paths_for_bodygroup_blend(input_blend)
    log_path = flex_dir / "blender_sort_flexes.log"
    flex_dir.mkdir(parents=True, exist_ok=True)
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_FLEXES_SCRIPT),
        "--",
        "--mode",
        "analyze",
        "--input-blend",
        str(input_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
    ]
    # Only L4D2 adds --game so the GMod flex-analyze command stays byte-identical.
    if game == "l4d2":
        command += ["--game", game]
    emit(progress, f"Starting Blender flex analysis: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not analysis_path.exists():
        raise RuntimeError(f"Blender completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender flex analysis finished in {time.monotonic() - started:.1f}s")
    return FlexAnalysisResult(
        input_blend=input_blend,
        flex_dir=flex_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        setup=setup,
        command=command,
    )


def sort_flexes_blend(
    input_blend: Path,
    plan: dict[str, object] | Path,
    output_blend: Path | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> FlexResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_FLEXES_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_FLEXES_SCRIPT)

    flex_dir, default_output_blend, analysis_path, default_plan_path, report_path, flexes_json_path = flex_paths_for_bodygroup_blend(input_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    flex_dir = output_blend.parent
    log_path = flex_dir / "blender_sort_flexes.log"
    plan_path = default_plan_path if output_blend == default_output_blend else flex_dir / "flex_plan.json"
    report_path = report_path if output_blend == default_output_blend else flex_dir / "blender_sort_flexes_report.json"
    flexes_json_path = flexes_json_path if output_blend == default_output_blend else flex_dir / "flexes.json"
    flex_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_FLEXES_SCRIPT),
        "--",
        "--mode",
        "apply",
        "--input-blend",
        str(input_blend),
        "--plan-json",
        str(plan_path),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
        "--flexes-json",
        str(flexes_json_path),
    ]
    emit(progress, f"Starting Blender flex sort: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not output_blend.exists():
        details = log_tail(log_path)
        suffix = f"\n\nBlender log tail:\n{details}" if details else ""
        raise RuntimeError(f"Blender completed but did not write {output_blend}{suffix}")
    if not report_path.exists():
        details = log_tail(log_path)
        suffix = f"\n\nBlender log tail:\n{details}" if details else ""
        raise RuntimeError(f"Blender completed but did not write {report_path}{suffix}")
    if not flexes_json_path.exists():
        details = log_tail(log_path)
        suffix = f"\n\nBlender log tail:\n{details}" if details else ""
        raise RuntimeError(f"Blender completed but did not write {flexes_json_path}{suffix}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender flex sort finished in {time.monotonic() - started:.1f}s")
    return FlexResult(
        input_blend=input_blend,
        output_blend=output_blend,
        flex_dir=flex_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        report_path=report_path,
        flexes_json_path=flexes_json_path,
        report=report,
        setup=setup,
        command=command,
    )


def scan_collision_source_bodygroups(
    input_blend: Path,
    additional_bone_groups: list[dict[str, object]] | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> CollisionSourcesResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_COLLISION_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_COLLISION_SCRIPT)

    collision_dir, *_rest = collision_paths_for_flex_blend(input_blend)
    sources_path = collision_sources_path_for_flex_blend(input_blend)
    bone_selection_path = collision_bone_selection_path_for_flex_blend(input_blend)
    log_path = collision_dir / "blender_sort_collision_sources.log"
    collision_dir.mkdir(parents=True, exist_ok=True)
    additional_selection_path: Path | None = None
    if additional_bone_groups is not None:
        additional_selection_path = bone_selection_path
        additional_selection_path.write_text(
            json.dumps({"additional_groups": additional_bone_groups}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_COLLISION_SCRIPT),
        "--",
        "--mode",
        "scan-sources",
        "--input-blend",
        str(input_blend),
        "--sources-json",
        str(sources_path),
    ]
    if additional_selection_path is not None:
        command.extend(["--additional-bones-json", str(additional_selection_path)])
    emit(progress, f"Starting Blender collision source scan: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not sources_path.exists():
        raise RuntimeError(f"Blender completed but did not write {sources_path}")
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender collision source scan finished in {time.monotonic() - started:.1f}s")
    return CollisionSourcesResult(
        input_blend=input_blend,
        collision_dir=collision_dir,
        sources_path=sources_path,
        log_path=log_path,
        sources=sources,
        setup=setup,
        command=command,
    )


def scan_collision_bones(
    input_blend: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> CollisionBonesResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_COLLISION_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_COLLISION_SCRIPT)

    collision_dir, *_rest = collision_paths_for_flex_blend(input_blend)
    bones_path = collision_bones_path_for_flex_blend(input_blend)
    log_path = collision_dir / "blender_sort_collision_bones.log"
    collision_dir.mkdir(parents=True, exist_ok=True)
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_COLLISION_SCRIPT),
        "--",
        "--mode",
        "scan-bones",
        "--input-blend",
        str(input_blend),
        "--bones-json",
        str(bones_path),
    ]
    emit(progress, f"Starting Blender collision bone scan: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not bones_path.exists():
        raise RuntimeError(f"Blender completed but did not write {bones_path}")
    bones = json.loads(bones_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender collision bone scan finished in {time.monotonic() - started:.1f}s")
    return CollisionBonesResult(
        input_blend=input_blend,
        collision_dir=collision_dir,
        bones_path=bones_path,
        log_path=log_path,
        bones=bones,
        setup=setup,
        command=command,
    )


def analyze_collision_blend(
    input_blend: Path,
    source_bodygroups: list[str] | None = None,
    additional_bone_groups: list[dict[str, object]] | None = None,
    quality_preset: str = "fast_preview",
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> CollisionAnalysisResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_COLLISION_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_COLLISION_SCRIPT)

    collision_dir, _output_blend, analysis_path, plan_path, _report_path, physics_settings_path, physics_smd_path = collision_paths_for_flex_blend(input_blend)
    log_path = collision_dir / "blender_sort_collision.log"
    collision_dir.mkdir(parents=True, exist_ok=True)
    source_selection_path: Path | None = None
    bone_selection_path: Path | None = None
    if source_bodygroups is not None:
        source_selection_path = collision_dir / "collision_source_selection.json"
        source_selection_path.write_text(
            json.dumps({"enabled_bodygroups": [str(name) for name in source_bodygroups]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if additional_bone_groups is not None:
        bone_selection_path = collision_bone_selection_path_for_flex_blend(input_blend)
        bone_selection_path.write_text(
            json.dumps({"additional_groups": additional_bone_groups}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_COLLISION_SCRIPT),
        "--",
        "--mode",
        "analyze",
        "--input-blend",
        str(input_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
        "--quality-preset",
        str(quality_preset or "fast_preview"),
        "--coacd-cache-dir",
        str(collision_dir / "coacd_cache"),
    ]
    if source_selection_path is not None:
        command.extend(["--source-bodygroups-json", str(source_selection_path)])
    if bone_selection_path is not None:
        command.extend(["--additional-bones-json", str(bone_selection_path)])
    emit(progress, f"Starting Blender collision analysis: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not analysis_path.exists():
        raise RuntimeError(f"Blender completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender collision analysis finished in {time.monotonic() - started:.1f}s")
    return CollisionAnalysisResult(
        input_blend=input_blend,
        collision_dir=collision_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        physics_settings_path=physics_settings_path,
        physics_smd_path=physics_smd_path,
        analysis=analysis,
        plan=plan,
        setup=setup,
        command=command,
    )


def sort_collision_blend(
    input_blend: Path,
    plan: dict[str, object] | Path,
    output_blend: Path | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> CollisionResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_SORT_COLLISION_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_COLLISION_SCRIPT)

    collision_dir, default_output_blend, analysis_path, default_plan_path, report_path, physics_settings_path, physics_smd_path = collision_paths_for_flex_blend(input_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    collision_dir = output_blend.parent
    log_path = collision_dir / "blender_sort_collision.log"
    plan_path = default_plan_path if output_blend == default_output_blend else collision_dir / "collision_plan.json"
    report_path = report_path if output_blend == default_output_blend else collision_dir / "blender_sort_collision_report.json"
    physics_settings_path = physics_settings_path if output_blend == default_output_blend else collision_dir / "physics_settings.json"
    physics_smd_path = physics_smd_path if output_blend == default_output_blend else collision_dir / "Physics.smd"
    collision_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_COLLISION_SCRIPT),
        "--",
        "--mode",
        "apply",
        "--input-blend",
        str(input_blend),
        "--plan-json",
        str(plan_path),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
        "--physics-settings-json",
        str(physics_settings_path),
        "--physics-smd",
        str(physics_smd_path),
    ]
    emit(progress, f"Starting Blender collision generation: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not output_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {output_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    if not physics_settings_path.exists():
        raise RuntimeError(f"Blender completed but did not write {physics_settings_path}")
    if not physics_smd_path.exists():
        raise RuntimeError(f"Blender completed but did not write {physics_smd_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender collision generation finished in {time.monotonic() - started:.1f}s")
    return CollisionResult(
        input_blend=input_blend,
        output_blend=output_blend,
        collision_dir=collision_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        report_path=report_path,
        physics_settings_path=physics_settings_path,
        physics_smd_path=physics_smd_path,
        report=report,
        setup=setup,
        command=command,
    )


def validate_manual_collision_blend(
    edited_blend: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, object]:
    """Validate a hand-edited collision-sorted blend and re-export Physics.smd.

    Used by the optional Step 8 substep where the user reshapes the Physics
    mesh in Blender; the validation report carries structured errors (isolated
    vertices, unweighted geometry, missing Physics object).
    """

    edited_blend = edited_blend.resolve()
    if not edited_blend.exists():
        raise FileNotFoundError(edited_blend)
    if not BLENDER_SORT_COLLISION_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_COLLISION_SCRIPT)
    collision_dir = edited_blend.parent
    report_path = collision_dir / "collision_manual_validation_report.json"
    physics_smd_path = collision_dir / "Physics.smd"
    log_path = collision_dir / "blender_collision_manual_validation.log"
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_COLLISION_SCRIPT),
        "--",
        "--mode",
        "validate-manual",
        "--input-blend",
        str(edited_blend),
        "--report-json",
        str(report_path),
        "--physics-smd",
        str(physics_smd_path),
    ]
    emit(progress, f"Validating manually edited collision shape: {edited_blend}")
    started = time.monotonic()
    report_path.unlink(missing_ok=True)
    process_error: Exception | None = None
    try:
        run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    except Exception as exc:
        if "cancelled" in str(exc).lower():
            raise
        process_error = exc
    report: dict[str, object] | None = None
    if report_path.exists():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
            report = loaded if isinstance(loaded, dict) else None
        except Exception:
            report = None
    if report is not None:
        validation = report.get("validation") if isinstance(report.get("validation"), dict) else {}
        errors = [str(item) for item in validation.get("errors", []) if item] if isinstance(validation.get("errors"), list) else []
        if errors:
            raise RuntimeError("Manual collision validation failed:\n- " + "\n- ".join(errors))
    if process_error is not None:
        raise process_error
    if report is None:
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    report["report_path"] = str(report_path)
    report["physics_smd_path"] = str(physics_smd_path)
    report["log_path"] = str(log_path)
    emit(progress, f"Manual collision validation finished in {time.monotonic() - started:.1f}s")
    return report


def validate_manual_bodygroups_blend(
    manual_edit_blend: Path,
    output_blend: Path | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    vertex_limit: int = DEFAULT_BODYGROUP_VERTEX_LIMIT,
) -> BodygroupResult:
    manual_edit_blend = manual_edit_blend.resolve()
    if not manual_edit_blend.exists():
        raise FileNotFoundError(manual_edit_blend)
    if not BLENDER_SORT_BODYGROUPS_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_SORT_BODYGROUPS_SCRIPT)

    bodygroup_dir, default_output_blend, analysis_path, plan_path, report_path = bodygroup_paths_for_manual_edit_blend(manual_edit_blend)
    output_blend = (output_blend or default_output_blend).resolve()
    bodygroup_dir = output_blend.parent
    log_path = bodygroup_dir / "blender_sort_bodygroups.log"
    report_path = bodygroup_dir / "blender_sort_bodygroups_report.json"
    bodygroup_dir.mkdir(parents=True, exist_ok=True)

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_SORT_BODYGROUPS_SCRIPT),
        "--",
        "--mode",
        "validate-manual",
        "--input-blend",
        str(manual_edit_blend),
        "--output-blend",
        str(output_blend),
        "--report-json",
        str(report_path),
        "--vertex-limit",
        str(int(vertex_limit)),
    ]
    emit(progress, f"Starting Blender manual bodygroup validation: {manual_edit_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validation = report.get("validation") if isinstance(report.get("validation"), dict) else {}
    if not validation.get("ok", False):
        errors = validation.get("errors", []) if isinstance(validation.get("errors"), list) else []
        message = "Manual bodygroup validation failed."
        if errors:
            message += "\n" + "\n".join(str(error) for error in errors)
        raise RuntimeError(message)
    if not output_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {output_blend}")
    emit(progress, f"Blender manual bodygroup validation finished in {time.monotonic() - started:.1f}s")
    return BodygroupResult(
        input_blend=manual_edit_blend,
        output_blend=output_blend,
        bodygroup_dir=bodygroup_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        log_path=log_path,
        report_path=report_path,
        report=report,
        setup=setup,
        command=command,
    )


def l4d2_reference_skeleton_path(survivor: str) -> Path:
    survivor = survivor if survivor in L4D2_SURVIVORS else DEFAULT_L4D2_SURVIVOR
    return L4D2_REFERENCE_SKELETON_DIR / f"{survivor}.smd"


# L4D2 polygon budget (triangles). Derived from working vs broken survivor ports: a single
# bodygroup mesh above ~30k tris renders as an exploding spray in game, and the whole model is
# unstable above the base-game envelope. Working community ports keep every mesh well under
# ~27.7k tris and the whole model under ~76k tris; the broken model had a 32.7k-tri mesh and
# ~100k tris total. These caps (with margin) bring an over-budget L4D2 model back inside the
# proven-safe envelope. GMod is never decimated (the budget only runs for L4D2).
L4D2_PER_MESH_TRIS_BUDGET = 25000
L4D2_TOTAL_TRIS_BUDGET = 70000


def run_l4d2_polygon_budget(
    final_dir: Path,
    blender_exe: Path,
    per_mesh_tris: int = L4D2_PER_MESH_TRIS_BUDGET,
    total_tris: int = L4D2_TOTAL_TRIS_BUDGET,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict:
    """L4D2 only: decimate the Step 9 export's bodygroup SMDs to the triangle budget.

    Runs AFTER the proportion export is finished, on the exported SMD files. Meshes that have a
    paired ``.vta`` (vertex animation / flexes) and the Physics collision SMD are left untouched
    (the script handles that); decimation re-imposes the <=3-bones-per-vertex limit afterwards.
    """
    if not BLENDER_DECIMATE_L4D2_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_DECIMATE_L4D2_SCRIPT)
    report_path = final_dir.parent / "l4d2_decimation_report.json"
    log_path = final_dir.parent / "blender_decimate_l4d2.log"
    command = [
        str(blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_DECIMATE_L4D2_SCRIPT),
        "--",
        "--export-dir",
        str(final_dir),
        "--per-mesh-tris",
        str(int(per_mesh_tris)),
        "--total-tris",
        str(int(total_tris)),
        "--report-json",
        str(report_path),
    ]
    emit(progress, f"L4D2 polygon budget: decimating bodygroups to <= {per_mesh_tris} tris/mesh, {total_tris} tris total.")
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if report_path.exists():
        try:
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            emit(progress, f"L4D2 polygon budget: total tris {rep.get('total_tris_before')} -> {rep.get('total_tris_after')} (budget {total_tris}).")
            return rep
        except Exception:
            return {}
    return {}


def run_proportion_export(
    input_blend: Path,
    remove_zero_weight_bones: bool = True,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    game: str = "gmod",
    survivor: str = DEFAULT_L4D2_SURVIVOR,
) -> ProportionResult:
    input_blend = input_blend.resolve()
    if not input_blend.exists():
        raise FileNotFoundError(input_blend)
    if not BLENDER_PROPORTION_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_PROPORTION_SCRIPT)

    (
        proportion_dir,
        raw_dir,
        workspace_dir,
        final_dir,
        pre_blend,
        processed_blend,
        report_path,
        files_path,
        log_path,
    ) = proportion_paths_for_collision_blend(input_blend)
    proportion_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_PROPORTION_SCRIPT),
        "--",
        "--input-blend",
        str(input_blend),
        "--raw-dir",
        str(raw_dir),
        "--workspace-dir",
        str(workspace_dir),
        "--final-dir",
        str(final_dir),
        "--pre-blend",
        str(pre_blend),
        "--processed-blend",
        str(processed_blend),
        "--report-json",
        str(report_path),
        "--files-json",
        str(files_path),
    ]
    if remove_zero_weight_bones:
        command.append("--remove-zero-weight-bones")
    if str(game or "").strip().lower() == "l4d2":
        # L4D2: process the model against the selected survivor's skeleton instead of the
        # built-in GMod ValveBiped armature, so the compiled model lives in the survivor's
        # bone convention (matching the included survivor animations). Step 9 swaps the
        # bundled survivor skeleton in for the template proportions/reference armatures.
        reference_smd = l4d2_reference_skeleton_path(survivor)
        if reference_smd.exists():
            command.extend(["--game", "l4d2", "--survivor-reference-smd", str(reference_smd)])
            emit(progress, f"L4D2 proportion base = survivor '{survivor}' skeleton ({reference_smd.name}).")
        else:
            emit(
                progress,
                f"NOTE: no bundled L4D2 skeleton for survivor '{survivor}'; falling back to the "
                "built-in GMod proportion armature (convention-correct, not survivor-specific).",
            )
    emit(progress, f"Starting Blender proportion export: {input_blend}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not pre_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {pre_blend}")
    if not processed_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {processed_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    if not files_path.exists():
        raise RuntimeError(f"Blender completed but did not write {files_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = json.loads(files_path.read_text(encoding="utf-8"))
    # For L4D2 the survivor skeleton is swapped in as the proportion base armature INSIDE the
    # Blender step (see --survivor-reference-smd above), so proportions.smd AND the reference
    # SMDs are already exported in the survivor's bone convention. No post-export reference
    # overwrite is needed (and doing one would reintroduce a convention mismatch).
    emit(progress, f"Blender proportion export finished in {time.monotonic() - started:.1f}s")
    # L4D2 only: now that the export is finished, decimate over-budget bodygroup SMDs to L4D2's
    # safe polygon envelope (a single mesh above ~30k tris / the model above ~76k tris explodes in
    # game). VTA-flexed meshes and the Physics SMD are left untouched. GMod is never decimated.
    if str(game or "").strip().lower() == "l4d2":
        try:
            decimation = run_l4d2_polygon_budget(
                final_dir, setup.blender_exe, progress=progress, cancel_check=cancel_check
            )
            report["l4d2_polygon_budget"] = decimation
        except Exception as exc:
            emit(progress, f"WARNING: L4D2 polygon-budget decimation did not complete ({exc}); some bodygroups may ship at full resolution and could be unstable in game.")
    return ProportionResult(
        input_blend=input_blend,
        proportion_dir=proportion_dir,
        raw_dir=raw_dir,
        workspace_dir=workspace_dir,
        final_dir=final_dir,
        pre_blend_path=pre_blend,
        processed_blend_path=processed_blend,
        report_path=report_path,
        files_path=files_path,
        log_path=log_path,
        report=report,
        files=files,
        setup=setup,
        command=command,
    )


def run_carms_sort(
    input_dir: Path,
    weight_threshold: float = 0.12,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    game: str = "gmod",
    experimental_arms: bool = False,
) -> CArmsResult:
    input_dir = input_dir.resolve()
    final_dir, carms_dir, workspace_blend, report_path, files_path = carms_paths_for_proportion_export(input_dir)
    if not final_dir.exists():
        raise FileNotFoundError(final_dir)
    if not BLENDER_CARMS_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_CARMS_SCRIPT)
    carms_dir.mkdir(parents=True, exist_ok=True)
    log_path = carms_dir / "blender_sort_carms.log"
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_CARMS_SCRIPT),
        "--",
        "--input-dir",
        str(final_dir),
        "--output-dir",
        str(carms_dir),
        "--workspace-blend",
        str(workspace_blend),
        "--report-json",
        str(report_path),
        "--files-json",
        str(files_path),
        "--weight-threshold",
        str(float(weight_threshold)),
        "--game",
        (str(game or "gmod").strip().lower() or "gmod"),
        "--experimental-arms",
        ("1" if experimental_arms else "0"),
    ]
    emit(progress, f"Starting Blender c_arms sorting: {final_dir}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not workspace_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {workspace_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    if not files_path.exists():
        raise RuntimeError(f"Blender completed but did not write {files_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = json.loads(files_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender c_arms sorting finished in {time.monotonic() - started:.1f}s")
    return CArmsResult(
        input_dir=final_dir,
        carms_dir=carms_dir,
        workspace_blend_path=workspace_blend,
        report_path=report_path,
        files_path=files_path,
        log_path=log_path,
        report=report,
        files=files,
        setup=setup,
        command=command,
    )


def analyze_vrd(
    input_dir: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> VrdAnalysisResult:
    input_dir = input_dir.resolve()
    final_dir, vrd_dir, workspace_blend, analysis_path, plan_path, preview_path, report_path, vrd_path = vrd_paths_for_proportion_export(input_dir)
    if not final_dir.exists():
        raise FileNotFoundError(final_dir)
    if not BLENDER_VRD_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_VRD_SCRIPT)
    vrd_dir.mkdir(parents=True, exist_ok=True)
    log_path = vrd_dir / "blender_sort_vrd.log"
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_VRD_SCRIPT),
        "--",
        "--mode",
        "analyze",
        "--input-dir",
        str(final_dir),
        "--output-dir",
        str(vrd_dir),
        "--workspace-blend",
        str(workspace_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
        "--preview-json",
        str(preview_path),
        "--report-json",
        str(report_path),
        "--vrd-path",
        str(vrd_path),
    ]
    emit(progress, f"Starting Blender VRD analysis: {final_dir}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not workspace_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {workspace_blend}")
    if not analysis_path.exists():
        raise RuntimeError(f"Blender completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    if not preview_path.exists():
        raise RuntimeError(f"Blender completed but did not write {preview_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender VRD analysis finished in {time.monotonic() - started:.1f}s")
    return VrdAnalysisResult(
        input_dir=final_dir,
        vrd_dir=vrd_dir,
        workspace_blend_path=workspace_blend,
        analysis_path=analysis_path,
        plan_path=plan_path,
        preview_path=preview_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        preview=preview,
        setup=setup,
        command=command,
    )


def apply_vrd(
    input_dir: Path,
    plan: dict[str, object] | Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> VrdResult:
    input_dir = input_dir.resolve()
    final_dir, vrd_dir, workspace_blend, analysis_path, default_plan_path, preview_path, report_path, vrd_path = vrd_paths_for_proportion_export(input_dir)
    if not final_dir.exists():
        raise FileNotFoundError(final_dir)
    if not BLENDER_VRD_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_VRD_SCRIPT)
    vrd_dir.mkdir(parents=True, exist_ok=True)
    log_path = vrd_dir / "blender_sort_vrd.log"
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path = default_plan_path
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_VRD_SCRIPT),
        "--",
        "--mode",
        "apply",
        "--input-dir",
        str(final_dir),
        "--output-dir",
        str(vrd_dir),
        "--workspace-blend",
        str(workspace_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
        "--preview-json",
        str(preview_path),
        "--report-json",
        str(report_path),
        "--vrd-path",
        str(vrd_path),
    ]
    emit(progress, f"Starting Blender VRD export: {final_dir}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not workspace_blend.exists():
        raise RuntimeError(f"Blender completed but did not write {workspace_blend}")
    if not report_path.exists():
        raise RuntimeError(f"Blender completed but did not write {report_path}")
    if not vrd_path.exists():
        raise RuntimeError(f"Blender completed but did not write {vrd_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit(progress, f"Blender VRD export finished in {time.monotonic() - started:.1f}s")
    return VrdResult(
        input_dir=final_dir,
        vrd_dir=vrd_dir,
        workspace_blend_path=workspace_blend,
        analysis_path=analysis_path,
        plan_path=plan_path,
        preview_path=preview_path,
        report_path=report_path,
        vrd_path=vrd_path,
        log_path=log_path,
        report=report,
        setup=setup,
        command=command,
    )


def preview_vrd(
    input_dir: Path,
    plan: dict[str, object] | Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> VrdPreviewResult:
    input_dir = input_dir.resolve()
    final_dir, vrd_dir, workspace_blend, analysis_path, default_plan_path, preview_path, _report_path, vrd_path = vrd_paths_for_proportion_export(input_dir)
    report_path = vrd_dir / "blender_sort_vrd_preview_report.json"
    if not final_dir.exists():
        raise FileNotFoundError(final_dir)
    if not BLENDER_VRD_SCRIPT.exists():
        raise FileNotFoundError(BLENDER_VRD_SCRIPT)
    vrd_dir.mkdir(parents=True, exist_ok=True)
    log_path = vrd_dir / "blender_sort_vrd.log"
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path = default_plan_path
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    setup = ensure_portable_blender(progress, cancel_check=cancel_check)
    command = [
        str(setup.blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(BLENDER_VRD_SCRIPT),
        "--",
        "--mode",
        "preview",
        "--input-dir",
        str(final_dir),
        "--output-dir",
        str(vrd_dir),
        "--workspace-blend",
        str(workspace_blend),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
        "--preview-json",
        str(preview_path),
        "--report-json",
        str(report_path),
        "--vrd-path",
        str(vrd_path),
    ]
    emit(progress, f"Starting Blender VRD preview: {final_dir}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not preview_path.exists():
        raise RuntimeError(f"Blender completed but did not write {preview_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Blender completed but did not write {plan_path}")
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    updated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    emit(progress, f"Blender VRD preview finished in {time.monotonic() - started:.1f}s")
    return VrdPreviewResult(
        input_dir=final_dir,
        vrd_dir=vrd_dir,
        workspace_blend_path=workspace_blend,
        analysis_path=analysis_path,
        plan_path=plan_path,
        preview_path=preview_path,
        report_path=report_path,
        log_path=log_path,
        plan=updated_plan,
        preview=preview,
        report=report,
        setup=setup,
        command=command,
    )


def analyze_textures(
    input_path: Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    scheme: str = "legacy",
    game: str = "gmod",
    max_texture_edge: int = 0,
) -> TextureAnalysisResult:
    input_path = input_path.resolve()
    texture_dir, analysis_path, plan_path, report_path, manifest_path, log_path = texture_paths_for_material_input(input_path)
    if not TEXTURE_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(TEXTURE_PROCESSOR_SCRIPT)
    texture_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(TEXTURE_PROCESSOR_SCRIPT),
        "--mode",
        "analyze",
        "--input",
        str(input_path),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
    ]
    # Only the manual Step 12 UI passes a non-legacy scheme; auto-porting omits
    # it entirely so the subprocess command stays byte-identical to before.
    if str(scheme or "legacy").strip().lower() != "legacy":
        command += ["--scheme", str(scheme).strip().lower()]
    # Only L4D2 passes --game (clamps textures to 2048px); GMod stays byte-identical.
    if str(game or "gmod").strip().lower() == "l4d2":
        command += ["--game", "l4d2"]
    # Only a below-default "max texture size" choice is passed, so the default 4096 stays byte-identical.
    if int(max_texture_edge or 0) and int(max_texture_edge) < 4096:
        command += ["--max-texture-edge", str(int(max_texture_edge))]
    emit(progress, f"Starting Step 12 texture analysis: {input_path}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not analysis_path.exists():
        raise RuntimeError(f"Texture analysis completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Texture analysis completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Step 12 texture analysis finished in {time.monotonic() - started:.1f}s")
    return TextureAnalysisResult(
        input_path=input_path,
        texture_dir=texture_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        manifest_path=manifest_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        command=command,
    )


def process_textures(
    input_path: Path,
    plan: dict[str, object] | Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    game: str = "gmod",
    max_texture_edge: int = 0,
) -> TextureProcessResult:
    input_path = input_path.resolve()
    texture_dir, analysis_path, default_plan_path, report_path, manifest_path, log_path = texture_paths_for_material_input(input_path)
    if not TEXTURE_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(TEXTURE_PROCESSOR_SCRIPT)
    texture_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path = default_plan_path
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        sys.executable,
        str(TEXTURE_PROCESSOR_SCRIPT),
        "--mode",
        "process",
        "--input",
        str(input_path),
        "--plan-json",
        str(plan_path),
        "--report-json",
        str(report_path),
        "--manifest-json",
        str(manifest_path),
    ]
    # Only L4D2 passes --game (clamps textures to 2048px); GMod stays byte-identical.
    if str(game or "gmod").strip().lower() == "l4d2":
        command += ["--game", "l4d2"]
    # Only a below-default "max texture size" choice is passed, so the default 4096 stays byte-identical.
    if int(max_texture_edge or 0) and int(max_texture_edge) < 4096:
        command += ["--max-texture-edge", str(int(max_texture_edge))]
    emit(progress, f"Starting Step 12 texture processing: {input_path}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not report_path.exists():
        raise RuntimeError(f"Texture processing completed but did not write {report_path}")
    if not manifest_path.exists():
        raise RuntimeError(f"Texture processing completed but did not write {manifest_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    emit(progress, f"Step 12 texture processing finished in {time.monotonic() - started:.1f}s")
    return TextureProcessResult(
        input_path=input_path,
        texture_dir=texture_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        manifest_path=manifest_path,
        log_path=log_path,
        report=report,
        manifest=manifest,
        command=command,
    )


def analyze_icons(
    input_path: Path,
    body_vmd_path: Path | None = None,
    face_vmd_paths: list[Path] | None = None,
    frame: int | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> IconAnalysisResult:
    input_path = input_path.resolve()
    icon_dir, analysis_path, plan_path, report_path, files_path, render_report_path, log_path = icon_paths_for_step1_input(input_path)
    # Each icon phase gets its own log file so the render log does not
    # overwrite the analyze output (run_process_streamed opens logs with "w").
    log_path = icon_dir / "icons_analyze.log"
    if not ICON_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(ICON_PROCESSOR_SCRIPT)
    icon_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ICON_PROCESSOR_SCRIPT),
        "--mode",
        "analyze",
        "--input",
        str(input_path),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
    ]
    if body_vmd_path:
        command.extend(["--body-vmd", str(body_vmd_path)])
    for face_vmd_path in face_vmd_paths or []:
        command.extend(["--face-vmd", str(face_vmd_path)])
    if frame is not None:
        command.extend(["--frame", str(int(frame))])
    emit(progress, f"Starting Step 13 icon analysis: {input_path}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not analysis_path.exists():
        raise RuntimeError(f"Icon analysis completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Icon analysis completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Step 13 icon analysis finished in {time.monotonic() - started:.1f}s")
    return IconAnalysisResult(
        input_path=input_path,
        icon_dir=icon_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        files_path=files_path,
        render_report_path=render_report_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        command=command,
    )


def run_icons(
    input_path: Path,
    custom_source_image: Path | None = None,
    icon_basename: str = "",
    body_vmd_path: Path | None = None,
    face_vmd_paths: list[Path] | None = None,
    frame: int | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> IconRunResult:
    input_path = input_path.resolve()
    icon_dir, analysis_path, plan_path, report_path, files_path, render_report_path, log_path = icon_paths_for_step1_input(input_path)
    if not ICON_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(ICON_PROCESSOR_SCRIPT)
    if not plan_path.exists() or body_vmd_path or face_vmd_paths or frame is not None:
        analyze_icons(
            input_path,
            body_vmd_path=body_vmd_path,
            face_vmd_paths=face_vmd_paths,
            frame=frame,
            progress=progress,
            cancel_check=cancel_check,
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    icon_dir.mkdir(parents=True, exist_ok=True)

    setup: SetupResult | None = None
    render_command: list[str] | None = None
    release_source: Path
    spawn_source: Path | None = None
    if custom_source_image:
        release_source = custom_source_image.resolve()
        spawn_source = release_source
        if not release_source.exists():
            raise FileNotFoundError(release_source)
        emit(progress, f"Using custom release icon image: {release_source}")
    else:
        if not BLENDER_ICON_SCRIPT.exists():
            raise FileNotFoundError(BLENDER_ICON_SCRIPT)
        setup = ensure_portable_blender(progress, cancel_check=cancel_check)
        pmx_path = Path(str(plan.get("pmx_path") or ""))
        vmd_path = resolve_icon_vmd_path(plan.get("body_vmd_path") or plan.get("vmd_path"))
        face_vmds = [Path(str(path)) for path in plan.get("face_vmd_paths", []) if str(path).strip()] if isinstance(plan.get("face_vmd_paths"), list) else []
        selected_frame = int(plan.get("frame", 334) if frame is None else frame)
        if not pmx_path.exists():
            raise FileNotFoundError(pmx_path)
        if not vmd_path.exists():
            raise FileNotFoundError(vmd_path)
        for face_vmd in face_vmds:
            if not face_vmd.exists():
                raise FileNotFoundError(face_vmd)
        release_source = icon_dir / "release_icon.png"
        spawn_source = icon_dir / "spawn_source.png"
        render_command = [
            str(setup.blender_exe),
            "--background",
            "--factory-startup",
            "--python-exit-code",
            "1",
            "--python",
            str(BLENDER_ICON_SCRIPT),
            "--",
            "--pmx",
            str(pmx_path),
            "--vmd",
            str(vmd_path),
            "--frame",
            str(selected_frame),
            "--output-png",
            str(release_source),
            "--spawn-output-png",
            str(spawn_source),
            "--report-json",
            str(render_report_path),
        ]
        for face_vmd in face_vmds:
            render_command.extend(["--face-vmd", str(face_vmd)])
        emit(progress, f"Starting Blender release icon render: {pmx_path}")
        started = time.monotonic()
        run_process_streamed(render_command, progress=progress, log_path=log_path, cancel_check=cancel_check)
        if not release_source.exists():
            raise RuntimeError(f"Blender render completed but did not write {release_source}")
        if not render_report_path.exists():
            raise RuntimeError(f"Blender render completed but did not write {render_report_path}")
        if not spawn_source.exists():
            raise RuntimeError(f"Blender render completed but did not write {spawn_source}")
        emit(progress, f"Blender icon render finished in {time.monotonic() - started:.1f}s")

    process_command = [
        sys.executable,
        str(ICON_PROCESSOR_SCRIPT),
        "--mode",
        "process",
        "--input",
        str(input_path),
        "--plan-json",
        str(plan_path),
        "--report-json",
        str(report_path),
        "--files-json",
        str(files_path),
        "--release-source",
        str(release_source),
    ]
    if spawn_source:
        process_command.extend(["--spawn-source", str(spawn_source)])
    if icon_basename:
        process_command.extend(["--icon-basename", icon_basename])
    emit(progress, "Generating Step 13 spawn icon assets...")
    started = time.monotonic()
    # The icon processor logs to its own file so it never overwrites (and is
    # never discarded because of) the Blender render log written above.
    processor_log = log_path if custom_source_image else icon_dir / "icons_process.log"
    run_process_streamed(process_command, progress=progress, log_path=processor_log, cancel_check=cancel_check)
    if not report_path.exists():
        raise RuntimeError(f"Icon processing completed but did not write {report_path}")
    if not files_path.exists():
        raise RuntimeError(f"Icon processing completed but did not write {files_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = json.loads(files_path.read_text(encoding="utf-8"))
    status = str(report.get("status") or "complete")
    emit(progress, f"Step 13 icon generation finished in {time.monotonic() - started:.1f}s ({status})")
    return IconRunResult(
        input_path=input_path,
        icon_dir=icon_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        files_path=files_path,
        render_report_path=render_report_path,
        log_path=log_path,
        report=report,
        files=files,
        setup=setup,
        render_command=render_command,
        process_command=process_command,
    )


def analyze_qc(
    input_path: Path,
    author: str = "",
    character_category: str = "",
    model_name: str = "",
    gmod_root: str = "",
    studiomdl_path: str = "",
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    gender: str = "female",
    game: str = "gmod",
    survivor: str = DEFAULT_L4D2_SURVIVOR,
) -> QcAnalysisResult:
    input_path = input_path.resolve()
    final_dir, qc_dir, analysis_path, plan_path, report_path, files_path, log_path = qc_paths_for_step9_input(input_path)
    if not QC_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(QC_PROCESSOR_SCRIPT)
    qc_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(QC_PROCESSOR_SCRIPT),
        "--mode",
        "analyze",
        "--input",
        str(input_path),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
    ]
    if author:
        command.extend(["--author", author])
    if character_category:
        command.extend(["--character-category", character_category])
    if model_name:
        command.extend(["--model-name", model_name])
    if gmod_root:
        command.extend(["--gmod-root", gmod_root])
    if studiomdl_path:
        command.extend(["--studiomdl", studiomdl_path])
    if str(gender or "").strip().lower() == "male":
        command.extend(["--gender", "male"])
    # Only L4D2/SFM add --game so the GMod analyze command stays byte-identical;
    # L4D2 also adds --survivor (SFM has no survivor slot).
    target_game = str(game or "").strip().lower()
    if target_game == "l4d2":
        command.extend(["--game", "l4d2", "--survivor", str(survivor or DEFAULT_L4D2_SURVIVOR)])
    elif target_game == "sfm":
        command.extend(["--game", "sfm"])
    emit(progress, f"Starting Step 14 QC analysis: {final_dir}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not analysis_path.exists():
        raise RuntimeError(f"QC analysis completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"QC analysis completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, f"Step 14 QC analysis finished in {time.monotonic() - started:.1f}s")
    return QcAnalysisResult(
        input_path=input_path,
        qc_dir=qc_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        files_path=files_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        command=command,
    )


def compile_and_compose_qc(
    input_path: Path,
    plan: dict[str, object] | Path,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> QcCompileResult:
    input_path = input_path.resolve()
    final_dir, qc_dir, analysis_path, default_plan_path, report_path, files_path, log_path = qc_paths_for_step9_input(input_path)
    if not QC_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(QC_PROCESSOR_SCRIPT)
    qc_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path = default_plan_path
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        sys.executable,
        str(QC_PROCESSOR_SCRIPT),
        "--mode",
        "compile",
        "--input",
        str(input_path),
        "--plan-json",
        str(plan_path),
        "--report-json",
        str(report_path),
        "--files-json",
        str(files_path),
    ]
    emit(progress, f"Starting Step 14 QC compile and compose: {final_dir}")
    started = time.monotonic()
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not report_path.exists():
        raise RuntimeError(f"QC compile completed but did not write {report_path}")
    if not files_path.exists():
        raise RuntimeError(f"QC compile completed but did not write {files_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = json.loads(files_path.read_text(encoding="utf-8"))
    emit(progress, f"Step 14 QC compile and compose finished in {time.monotonic() - started:.1f}s")
    return QcCompileResult(
        input_path=input_path,
        qc_dir=qc_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        files_path=files_path,
        log_path=log_path,
        report=report,
        files=files,
        command=command,
    )


def analyze_release_description(
    input_path: Path,
    character_name: str = "",
    work_title: str = "",
    author: str = "",
    model_creator: str = "",
    quote_text: str = "",
    quote_original_text: str = "",
    quote_language: str = "",
    quote_author: str = "",
    image_url: str = "",
    rtx_link: str = "",
    openai_model: str = "",
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> ReleaseAnalysisResult:
    input_path = input_path.resolve()
    release_dir, analysis_path, plan_path, report_path, files_path, translations_path, template_path, log_path = release_paths_for_step14_input(input_path)
    if not RELEASE_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(RELEASE_PROCESSOR_SCRIPT)
    release_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(RELEASE_PROCESSOR_SCRIPT),
        "--mode",
        "analyze",
        "--input",
        str(input_path),
        "--analysis-json",
        str(analysis_path),
        "--plan-json",
        str(plan_path),
    ]
    optional_args = {
        "--character-name": character_name,
        "--work-title": work_title,
        "--author": author,
        "--model-creator": model_creator,
        "--quote-text": quote_text,
        "--quote-original-text": quote_original_text,
        "--quote-language": quote_language,
        "--quote-author": quote_author,
        "--image-url": image_url,
        "--rtx-link": rtx_link,
        "--openai-model": openai_model,
    }
    for key, value in optional_args.items():
        if value:
            command.extend([key, str(value)])
    emit(progress, f"Starting Step 15 release description analysis: {release_dir}")
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check)
    if not analysis_path.exists():
        raise RuntimeError(f"Release description analysis completed but did not write {analysis_path}")
    if not plan_path.exists():
        raise RuntimeError(f"Release description analysis completed but did not write {plan_path}")
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, "Step 15 release description analysis finished.")
    return ReleaseAnalysisResult(
        input_path=input_path,
        release_dir=release_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        files_path=files_path,
        translations_path=translations_path,
        template_path=template_path,
        log_path=log_path,
        analysis=analysis,
        plan=plan,
        command=command,
    )


def generate_release_description(
    input_path: Path,
    plan: dict[str, object] | Path,
    openai_api_key: str = "",
    deepl_api_key: str = "",
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> ReleaseGenerateResult:
    input_path = input_path.resolve()
    release_dir, analysis_path, plan_path, report_path, files_path, translations_path, template_path, log_path = release_paths_for_step14_input(input_path)
    if not RELEASE_PROCESSOR_SCRIPT.exists():
        raise FileNotFoundError(RELEASE_PROCESSOR_SCRIPT)
    release_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(plan, Path):
        plan_path = plan.resolve()
        if not plan_path.exists():
            raise FileNotFoundError(plan_path)
    else:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        sys.executable,
        str(RELEASE_PROCESSOR_SCRIPT),
        "--mode",
        "generate",
        "--input",
        str(input_path),
        "--plan-json",
        str(plan_path),
        "--report-json",
        str(report_path),
        "--files-json",
        str(files_path),
    ]
    env = os.environ.copy()
    if openai_api_key:
        env["MCI_SESSION_OPENAI_API_KEY"] = openai_api_key
    if deepl_api_key:
        env["MCI_SESSION_DEEPL_API_KEY"] = deepl_api_key
    emit(progress, f"Starting Step 15 release description generation: {release_dir}")
    run_process_streamed(command, progress=progress, log_path=log_path, cancel_check=cancel_check, env=env)
    if not report_path.exists():
        raise RuntimeError(f"Release description generation completed but did not write {report_path}")
    if not files_path.exists():
        raise RuntimeError(f"Release description generation completed but did not write {files_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = json.loads(files_path.read_text(encoding="utf-8"))
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    emit(progress, "Step 15 release description generation finished.")
    return ReleaseGenerateResult(
        input_path=input_path,
        release_dir=release_dir,
        analysis_path=analysis_path,
        plan_path=plan_path,
        report_path=report_path,
        files_path=files_path,
        translations_path=translations_path,
        template_path=template_path,
        log_path=log_path,
        report=report,
        files=files,
        plan=plan_data,
        command=command,
    )


def find_model_files(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    matches = list(folder.rglob("*.pmx")) + list(folder.rglob("*.vrm"))
    return sorted(matches, key=lambda path: (-path.stat().st_size, str(path).lower()))


def find_pmx_files(folder: Path) -> list[Path]:
    return find_model_files(folder)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="analyze a PMX or VRM model file")
    analyze_parser.add_argument("pmx", type=Path)
    analyze_parser.add_argument("--source-dir", type=Path)

    setup_parser = subparsers.add_parser("setup", help="install/verify portable Blender and add-ons")
    setup_parser.add_argument("--force-check", action="store_true")

    import_parser = subparsers.add_parser("import", help="copy assets and import a PMX or VRM model into Blender")
    import_parser.add_argument("pmx", type=Path)
    import_parser.add_argument("--source-dir", type=Path, required=True)

    fix_parser = subparsers.add_parser("fix", help="make single-user, optionally clear custom normals, fix CATS armature, and convert to Valve bones")
    fix_parser.add_argument("input_blend", type=Path)
    fix_parser.add_argument("--output-blend", type=Path)
    fix_parser.add_argument("--clear-custom-normals", dest="clear_custom_normals", action="store_true", default=False)
    fix_parser.add_argument("--keep-custom-normals", dest="clear_custom_normals", action="store_false")

    spine_analyze_parser = subparsers.add_parser("spine-analyze", help="analyze and propose Source spine bone repair")
    spine_analyze_parser.add_argument("fixed_blend", type=Path)

    spine_fix_parser = subparsers.add_parser("spine-fix", help="apply a proposed Source spine bone repair")
    spine_fix_parser.add_argument("fixed_blend", type=Path)
    spine_fix_parser.add_argument("--plan-json", type=Path, required=True)
    spine_fix_parser.add_argument("--output-blend", type=Path)

    sort_analyze_parser = subparsers.add_parser("sort-bones-analyze", help="analyze and propose bone merges for the Source bone limit")
    sort_analyze_parser.add_argument("spine_fixed_blend", type=Path)
    sort_analyze_parser.add_argument("--limit", type=int, default=None)
    sort_analyze_parser.add_argument("--game", choices=("gmod", "l4d2", "sfm"), default="gmod")

    sort_parser = subparsers.add_parser("sort-bones", help="apply a proposed bone merge plan")
    sort_parser.add_argument("spine_fixed_blend", type=Path)
    sort_parser.add_argument("--plan-json", type=Path, required=True)
    sort_parser.add_argument("--output-blend", type=Path)
    sort_parser.add_argument("--limit", type=int, default=None)
    sort_parser.add_argument("--game", choices=("gmod", "l4d2", "sfm"), default="gmod")

    material_scan_parser = subparsers.add_parser("materials-scan", help="scan materials and propose cleanup/combine plan")
    material_scan_parser.add_argument("bones_sorted_blend", type=Path)
    material_scan_parser.add_argument("--limit", type=int, default=32)

    material_apply_parser = subparsers.add_parser("materials-apply", help="apply initial material cleanup and base-texture combining")
    material_apply_parser.add_argument("bones_sorted_blend", type=Path)
    material_apply_parser.add_argument("--plan-json", type=Path, required=True)
    material_apply_parser.add_argument("--output-blend", type=Path)
    material_apply_parser.add_argument("--limit", type=int, default=32)

    material_merge_parser = subparsers.add_parser("materials-merge", help="merge grouped materials and validate final material count")
    material_merge_parser.add_argument("materials_sorted_blend", type=Path)
    material_merge_parser.add_argument("--merge-plan-json", type=Path, required=True)
    material_merge_parser.add_argument("--output-blend", type=Path)
    material_merge_parser.add_argument("--limit", type=int, default=32)

    bodygroup_analyze_parser = subparsers.add_parser("bodygroups-analyze", help="analyze and propose bodygroup sorting")
    bodygroup_analyze_parser.add_argument("step5_blend", type=Path)
    bodygroup_analyze_parser.add_argument("--scale-factor", type=float, default=DEFAULT_BODYGROUP_SCALE_FACTOR)
    bodygroup_analyze_parser.add_argument("--scale-preset", choices=("factor", "tall", "normal", "short"), default="factor")
    bodygroup_analyze_parser.add_argument("--vertex-limit", type=int, default=DEFAULT_BODYGROUP_VERTEX_LIMIT)
    bodygroup_analyze_parser.add_argument("--always-auto-split", action="store_true")

    bodygroup_apply_parser = subparsers.add_parser("bodygroups-apply", help="apply a bodygroup sorting plan")
    bodygroup_apply_parser.add_argument("step5_blend", type=Path)
    bodygroup_apply_parser.add_argument("--plan-json", type=Path, required=True)
    bodygroup_apply_parser.add_argument("--output-blend", type=Path)
    bodygroup_apply_parser.add_argument("--scale-factor", type=float, default=DEFAULT_BODYGROUP_SCALE_FACTOR)
    bodygroup_apply_parser.add_argument("--scale-preset", choices=("factor", "tall", "normal", "short"), default="factor")
    bodygroup_apply_parser.add_argument("--vertex-limit", type=int, default=DEFAULT_BODYGROUP_VERTEX_LIMIT)
    bodygroup_apply_parser.add_argument("--always-auto-split", action="store_true")

    bodygroup_manual_parser = subparsers.add_parser("bodygroups-manual-validate", help="validate a manually edited Step 6 bodygroup blend")
    bodygroup_manual_parser.add_argument("manual_edit_blend", type=Path)
    bodygroup_manual_parser.add_argument("--output-blend", type=Path)
    bodygroup_manual_parser.add_argument("--vertex-limit", type=int, default=DEFAULT_BODYGROUP_VERTEX_LIMIT)

    flex_analyze_parser = subparsers.add_parser("flexes-analyze", help="analyze and propose facial/body flex sorting")
    flex_analyze_parser.add_argument("bodygroups_sorted_blend", type=Path)
    flex_analyze_parser.add_argument("--game", choices=("gmod", "l4d2", "sfm"), default="gmod")

    flex_apply_parser = subparsers.add_parser("flexes-apply", help="apply a flex sorting plan")
    flex_apply_parser.add_argument("bodygroups_sorted_blend", type=Path)
    flex_apply_parser.add_argument("--plan-json", type=Path, required=True)
    flex_apply_parser.add_argument("--output-blend", type=Path)

    collision_analyze_parser = subparsers.add_parser("collision-analyze", help="analyze and propose Source collision mesh generation")
    collision_analyze_parser.add_argument("flexes_sorted_blend", type=Path)
    collision_analyze_parser.add_argument("--source-bodygroups-json", type=Path)
    collision_analyze_parser.add_argument("--additional-bones-json", type=Path)
    collision_analyze_parser.add_argument("--quality-preset", choices=("fast_preview", "balanced", "high_quality"), default="fast_preview")

    collision_bones_parser = subparsers.add_parser("collision-bones", help="scan Step 8 optional CoACD bones")
    collision_bones_parser.add_argument("flexes_sorted_blend", type=Path)

    collision_sources_parser = subparsers.add_parser("collision-sources", help="scan Step 8 CoACD source bodygroups")
    collision_sources_parser.add_argument("flexes_sorted_blend", type=Path)
    collision_sources_parser.add_argument("--additional-bones-json", type=Path)

    collision_apply_parser = subparsers.add_parser("collision-apply", help="generate and validate a Physics collision mesh")
    collision_apply_parser.add_argument("flexes_sorted_blend", type=Path)
    collision_apply_parser.add_argument("--plan-json", type=Path, required=True)
    collision_apply_parser.add_argument("--output-blend", type=Path)

    proportion_parser = subparsers.add_parser("proportion-run", help="export raw Source files and run the proportion trick")
    proportion_parser.add_argument("collision_sorted_blend", type=Path)
    proportion_parser.add_argument("--remove-zero-weight-bones", dest="remove_zero_weight_bones", action="store_true", default=True)
    proportion_parser.add_argument("--keep-zero-weight-bones", dest="remove_zero_weight_bones", action="store_false")
    proportion_parser.add_argument("--game", choices=("gmod", "l4d2", "sfm"), default="gmod")
    proportion_parser.add_argument("--survivor", choices=tuple(L4D2_SURVIVORS), default=DEFAULT_L4D2_SURVIVOR)

    carms_parser = subparsers.add_parser("carms-run", help="create c_arms SMD files from the proportion export")
    carms_parser.add_argument("proportion_export_dir", type=Path)
    carms_parser.add_argument("--weight-threshold", type=float, default=0.12)
    carms_parser.add_argument("--game", default="gmod")

    vrd_analyze_parser = subparsers.add_parser("vrd-analyze", help="analyze Step 9 SMD files and propose VRD skirt helpers")
    vrd_analyze_parser.add_argument("proportion_export_dir", type=Path)

    vrd_apply_parser = subparsers.add_parser("vrd-apply", help="apply a VRD helper plan and export vrd.vrd")
    vrd_apply_parser.add_argument("proportion_export_dir", type=Path)
    vrd_apply_parser.add_argument("--plan-json", type=Path, required=True)

    vrd_preview_parser = subparsers.add_parser("vrd-preview", help="regenerate Step 11 VRD preview from a helper plan without exporting")
    vrd_preview_parser.add_argument("proportion_export_dir", type=Path)
    vrd_preview_parser.add_argument("--plan-json", type=Path, required=True)

    textures_analyze_parser = subparsers.add_parser("textures-analyze", help="analyze Step 5 material textures and normal maps")
    textures_analyze_parser.add_argument("material_mapping", type=Path)

    textures_process_parser = subparsers.add_parser("textures-process", help="copy/convert base textures and normals to PNG")
    textures_process_parser.add_argument("material_mapping", type=Path)
    textures_process_parser.add_argument("--plan-json", type=Path, required=True)

    icons_analyze_parser = subparsers.add_parser("icons-analyze", help="analyze Step 1 PMX workspace for Step 13 icon generation")
    icons_analyze_parser.add_argument("step1_input", type=Path)
    icons_analyze_parser.add_argument("--body-vmd", type=Path)
    icons_analyze_parser.add_argument("--face-vmd", action="append", default=[], type=Path)
    icons_analyze_parser.add_argument("--frame", type=int)

    icons_run_parser = subparsers.add_parser("icons-run", help="render or process Step 13 release/spawn icons")
    icons_run_parser.add_argument("step1_input", type=Path)
    icons_run_parser.add_argument("--custom-source-image", type=Path)
    icons_run_parser.add_argument("--icon-basename", default="")
    icons_run_parser.add_argument("--body-vmd", type=Path)
    icons_run_parser.add_argument("--face-vmd", action="append", default=[], type=Path)
    icons_run_parser.add_argument("--frame", type=int)

    qc_analyze_parser = subparsers.add_parser("qc-analyze", help="analyze Step 9 outputs and propose QC/jigglebone plan")
    qc_analyze_parser.add_argument("step9_export_dir", type=Path)
    qc_analyze_parser.add_argument("--author", default="")
    qc_analyze_parser.add_argument("--character-category", default="")
    qc_analyze_parser.add_argument("--model-name", default="")
    qc_analyze_parser.add_argument("--gender", choices=("female", "male"), default="female")
    qc_analyze_parser.add_argument("--game", choices=("gmod", "l4d2", "sfm"), default="gmod")
    qc_analyze_parser.add_argument("--survivor", choices=tuple(L4D2_SURVIVORS), default=DEFAULT_L4D2_SURVIVOR)
    qc_analyze_parser.add_argument("--gmod-root", default="")
    qc_analyze_parser.add_argument("--studiomdl", default="")

    qc_compile_parser = subparsers.add_parser("qc-compile-compose", help="generate QC, compile with StudioMDL, and compose final addon folder")
    qc_compile_parser.add_argument("step9_export_dir", type=Path)
    qc_compile_parser.add_argument("--plan-json", type=Path, required=True)

    release_analyze_parser = subparsers.add_parser("release-analyze", help="analyze Step 14 output and prepare release description metadata")
    release_analyze_parser.add_argument("workspace_or_step14_dir", type=Path)
    release_analyze_parser.add_argument("--character-name", default="")
    release_analyze_parser.add_argument("--work-title", default="")
    release_analyze_parser.add_argument("--author", default="")
    release_analyze_parser.add_argument("--model-creator", default="")
    release_analyze_parser.add_argument("--quote-text", default="")
    release_analyze_parser.add_argument("--quote-original-text", default="")
    release_analyze_parser.add_argument("--quote-language", default="")
    release_analyze_parser.add_argument("--quote-author", default="")
    release_analyze_parser.add_argument("--image-url", default="")
    release_analyze_parser.add_argument("--rtx-link", default="")
    release_analyze_parser.add_argument("--openai-model", default="")

    release_generate_parser = subparsers.add_parser("release-generate", help="generate Step 15 release description template")
    release_generate_parser.add_argument("workspace_or_step14_dir", type=Path)
    release_generate_parser.add_argument("--plan-json", type=Path, required=True)

    args = parser.parse_args(argv)

    def print_progress(message: str) -> None:
        print(message, flush=True)

    if args.command == "analyze":
        result = analyze_model_file(args.pmx, args.source_dir)
        print(result.to_json())
        return 0
    if args.command == "setup":
        setup = ensure_portable_blender(print_progress)
        if args.force_check:
            verify_and_install_addons(setup.blender_exe, progress=print_progress, check_only=True)
        print(json.dumps({"blender_exe": str(setup.blender_exe), "version": setup.version, "reused": setup.reused}, indent=2))
        return 0
    if args.command == "import":
        result = import_pmx_to_blender(args.pmx, args.source_dir, progress=print_progress)
        print(json.dumps({"blend": str(result.workspace.blend_path), "workspace": str(result.workspace.root)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "fix":
        result = fix_imported_blend(
            args.input_blend,
            args.output_blend,
            clear_custom_normals=args.clear_custom_normals,
            progress=print_progress,
        )
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "fix_dir": str(result.fix_dir),
                    "report": str(result.fix_report_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "spine-analyze":
        result = analyze_spine_blend(args.fixed_blend, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "spine_dir": str(result.spine_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "spine-fix":
        result = fix_spine_blend(args.fixed_blend, args.plan_json, args.output_blend, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "spine_dir": str(result.spine_dir),
                    "report": str(result.report_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sort-bones-analyze":
        result = analyze_sort_bones_blend(args.spine_fixed_blend, args.limit, progress=print_progress, game=args.game)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "sort_dir": str(result.sort_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sort-bones":
        result = sort_bones_blend(args.spine_fixed_blend, args.plan_json, args.output_blend, args.limit, progress=print_progress, game=args.game)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "sort_dir": str(result.sort_dir),
                    "report": str(result.report_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "materials-scan":
        result = scan_materials_blend(args.bones_sorted_blend, args.limit, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "material_dir": str(result.material_dir),
                    "scan": str(result.scan_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "materials-apply":
        result = apply_materials_initial_blend(args.bones_sorted_blend, args.plan_json, args.output_blend, args.limit, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "material_dir": str(result.material_dir),
                    "report": str(result.report_path),
                    "merge_plan": str(result.merge_plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "materials-merge":
        result = merge_materials_blend(args.materials_sorted_blend, args.merge_plan_json, args.output_blend, args.limit, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "material_dir": str(result.material_dir),
                    "report": str(result.report_path),
                    "materials": str(result.materials_json_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "bodygroups-analyze":
        result = analyze_bodygroups_blend(
            args.step5_blend,
            args.scale_factor,
            scale_preset=args.scale_preset,
            always_auto_split=args.always_auto_split,
            vertex_limit=args.vertex_limit,
            progress=print_progress,
        )
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "bodygroup_dir": str(result.bodygroup_dir),
                    "manual_edit_blend": str(result.manual_edit_blend),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "bodygroups-apply":
        result = sort_bodygroups_blend(
            args.step5_blend,
            args.plan_json,
            args.output_blend,
            args.scale_factor,
            scale_preset=args.scale_preset,
            always_auto_split=args.always_auto_split,
            vertex_limit=args.vertex_limit,
            progress=print_progress,
        )
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "bodygroup_dir": str(result.bodygroup_dir),
                    "report": str(result.report_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "bodygroups-manual-validate":
        result = validate_manual_bodygroups_blend(args.manual_edit_blend, args.output_blend, progress=print_progress, vertex_limit=args.vertex_limit)
        print(
            json.dumps(
                {
                    "manual_edit_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "bodygroup_dir": str(result.bodygroup_dir),
                    "report": str(result.report_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "flexes-analyze":
        result = analyze_flexes_blend(args.bodygroups_sorted_blend, progress=print_progress, game=args.game)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "flex_dir": str(result.flex_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "flexes-apply":
        result = sort_flexes_blend(args.bodygroups_sorted_blend, args.plan_json, args.output_blend, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "flex_dir": str(result.flex_dir),
                    "report": str(result.report_path),
                    "flexes_json": str(result.flexes_json_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "collision-bones":
        result = scan_collision_bones(args.flexes_sorted_blend, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "collision_dir": str(result.collision_dir),
                    "bones": str(result.bones_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "collision-sources":
        additional_groups = None
        if getattr(args, "additional_bones_json", None):
            data = json.loads(args.additional_bones_json.read_text(encoding="utf-8"))
            raw = data.get("additional_groups", []) if isinstance(data, dict) else []
            additional_groups = [dict(item) for item in raw if isinstance(item, dict)]
        result = scan_collision_source_bodygroups(args.flexes_sorted_blend, additional_bone_groups=additional_groups, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "collision_dir": str(result.collision_dir),
                    "sources": str(result.sources_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "collision-analyze":
        source_bodygroups = None
        if getattr(args, "source_bodygroups_json", None):
            data = json.loads(args.source_bodygroups_json.read_text(encoding="utf-8"))
            raw = data.get("enabled_bodygroups", []) if isinstance(data, dict) else []
            source_bodygroups = [str(name) for name in raw if str(name)]
        additional_groups = None
        if getattr(args, "additional_bones_json", None):
            data = json.loads(args.additional_bones_json.read_text(encoding="utf-8"))
            raw = data.get("additional_groups", []) if isinstance(data, dict) else []
            additional_groups = [dict(item) for item in raw if isinstance(item, dict)]
        result = analyze_collision_blend(
            args.flexes_sorted_blend,
            source_bodygroups=source_bodygroups,
            additional_bone_groups=additional_groups,
            quality_preset=args.quality_preset,
            progress=print_progress,
        )
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "collision_dir": str(result.collision_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "physics_smd": str(result.physics_smd_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "collision-apply":
        result = sort_collision_blend(args.flexes_sorted_blend, args.plan_json, args.output_blend, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "output_blend": str(result.output_blend),
                    "collision_dir": str(result.collision_dir),
                    "report": str(result.report_path),
                    "physics_settings": str(result.physics_settings_path),
                    "physics_smd": str(result.physics_smd_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "proportion-run":
        result = run_proportion_export(args.collision_sorted_blend, remove_zero_weight_bones=args.remove_zero_weight_bones, progress=print_progress, game=args.game, survivor=args.survivor)
        print(
            json.dumps(
                {
                    "input_blend": str(result.input_blend),
                    "proportion_dir": str(result.proportion_dir),
                    "raw_dir": str(result.raw_dir),
                    "workspace_dir": str(result.workspace_dir),
                    "final_dir": str(result.final_dir),
                    "pre_blend": str(result.pre_blend_path),
                    "processed_blend": str(result.processed_blend_path),
                    "report": str(result.report_path),
                    "files": str(result.files_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "carms-run":
        result = run_carms_sort(args.proportion_export_dir, args.weight_threshold, progress=print_progress, game=args.game)
        print(
            json.dumps(
                {
                    "input_dir": str(result.input_dir),
                    "carms_dir": str(result.carms_dir),
                    "workspace_blend": str(result.workspace_blend_path),
                    "report": str(result.report_path),
                    "files": str(result.files_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "vrd-analyze":
        result = analyze_vrd(args.proportion_export_dir, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_dir": str(result.input_dir),
                    "vrd_dir": str(result.vrd_dir),
                    "workspace_blend": str(result.workspace_blend_path),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "preview": str(result.preview_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "vrd-apply":
        result = apply_vrd(args.proportion_export_dir, args.plan_json, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_dir": str(result.input_dir),
                    "vrd_dir": str(result.vrd_dir),
                    "workspace_blend": str(result.workspace_blend_path),
                    "report": str(result.report_path),
                    "preview": str(result.preview_path),
                    "vrd": str(result.vrd_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "vrd-preview":
        result = preview_vrd(args.proportion_export_dir, args.plan_json, progress=print_progress)
        print(
            json.dumps(
                {
                    "input_dir": str(result.input_dir),
                    "vrd_dir": str(result.vrd_dir),
                    "workspace_blend": str(result.workspace_blend_path),
                    "plan": str(result.plan_path),
                    "preview": str(result.preview_path),
                    "report": str(result.report_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "textures-analyze":
        result = analyze_textures(args.material_mapping, progress=print_progress)
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "texture_dir": str(result.texture_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "textures-process":
        result = process_textures(args.material_mapping, args.plan_json, progress=print_progress)
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "texture_dir": str(result.texture_dir),
                    "report": str(result.report_path),
                    "manifest": str(result.manifest_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "icons-analyze":
        result = analyze_icons(
            args.step1_input,
            body_vmd_path=args.body_vmd,
            face_vmd_paths=args.face_vmd,
            frame=args.frame,
            progress=print_progress,
        )
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "icon_dir": str(result.icon_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "icons-run":
        result = run_icons(
            args.step1_input,
            custom_source_image=args.custom_source_image,
            icon_basename=args.icon_basename,
            body_vmd_path=args.body_vmd,
            face_vmd_paths=args.face_vmd,
            frame=args.frame,
            progress=print_progress,
        )
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "icon_dir": str(result.icon_dir),
                    "report": str(result.report_path),
                    "files": str(result.files_path),
                    "render_report": str(result.render_report_path),
                    "log": str(result.log_path),
                    "status": result.report.get("status"),
                    "validation_errors": result.report.get("validation_errors", []),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "qc-analyze":
        result = analyze_qc(
            args.step9_export_dir,
            author=args.author,
            character_category=args.character_category,
            model_name=args.model_name,
            gmod_root=args.gmod_root,
            studiomdl_path=args.studiomdl,
            progress=print_progress,
            gender=args.gender,
            game=args.game,
            survivor=args.survivor,
        )
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "qc_dir": str(result.qc_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                    "validation_errors": result.analysis.get("validation_errors", []),
                    "warnings": result.analysis.get("warnings", []),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "qc-compile-compose":
        result = compile_and_compose_qc(args.step9_export_dir, args.plan_json, progress=print_progress)
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "qc_dir": str(result.qc_dir),
                    "report": str(result.report_path),
                    "files": str(result.files_path),
                    "log": str(result.log_path),
                    "status": result.report.get("status"),
                    "addon_dir": result.report.get("addon_dir"),
                    "validation_errors": result.report.get("validation_errors", []),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "release-analyze":
        result = analyze_release_description(
            args.workspace_or_step14_dir,
            character_name=args.character_name,
            work_title=args.work_title,
            author=args.author,
            model_creator=args.model_creator,
            quote_text=args.quote_text,
            quote_original_text=args.quote_original_text,
            quote_language=args.quote_language,
            quote_author=args.quote_author,
            image_url=args.image_url,
            rtx_link=args.rtx_link,
            openai_model=args.openai_model,
            progress=print_progress,
        )
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "release_dir": str(result.release_dir),
                    "analysis": str(result.analysis_path),
                    "plan": str(result.plan_path),
                    "log": str(result.log_path),
                    "validation_errors": result.analysis.get("validation_errors", []),
                    "warnings": result.analysis.get("warnings", []),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "release-generate":
        result = generate_release_description(args.workspace_or_step14_dir, args.plan_json, progress=print_progress)
        print(
            json.dumps(
                {
                    "input": str(result.input_path),
                    "release_dir": str(result.release_dir),
                    "report": str(result.report_path),
                    "files": str(result.files_path),
                    "template": str(result.template_path),
                    "log": str(result.log_path),
                    "validation_errors": result.report.get("validation_errors", []),
                    "warnings": result.report.get("warnings", []),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
