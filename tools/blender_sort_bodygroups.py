#!/usr/bin/env python3
"""Blender-side step 6 bodygroup sorting helper."""

from __future__ import annotations

import argparse
import contextlib
import colorsys
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import bpy
from mathutils import Vector


DEFAULT_SCALE_FACTOR = 40.457
DEFAULT_SOURCE_VERTEX_LIMIT = 65535
RTX_REMIX_VERTEX_LIMIT = 32767
SOURCE_VERTEX_LIMIT = DEFAULT_SOURCE_VERTEX_LIMIT
# Noise-level threshold in post-scale (Source) units. Pruning runs after the
# ~40x character scale, so this must be tiny enough that subtle real morphs
# (eyelids, pupils) are never treated as "unused".
UNUSED_SHAPEKEY_DELTA_EPSILON = 1e-4
FACE_MERGE_NECK_BONE = "ValveBiped.Bip01_Neck1"
FACE_MERGE_NECK_Z_TOLERANCE = 1e-4
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
TRACKING_PREFIXES = ("mci_mat_", "mci_final_")
HELPER_MESH_NAMES = {"smd_bone_vis"}
HEAD_HINTS = ("head", "face", "eye", "iris", "pupil", "mouth", "teeth", "tooth", "tongue", "nose", "brow", "lash", "eyelid", "blush")
FACE_SCALE_HINTS = HEAD_HINTS + ("skin", "顔", "脸", "臉", "面", "目", "眼", "瞳", "口", "眉", "睫")
FACIAL_MERGE_TEXT_HINTS = tuple(
    dict.fromkeys(
        HEAD_HINTS
        + (
            "surface",
            "surfacel",
            "surfacer",
            "eyebrow",
            "eyelash",
            "eyelashes",
            "tear",
            "tears",
            "expression",
            "expressions",
            "bloodline",
            "tooth",
            "teeth",
            "顔",
            "脸",
            "臉",
            "面",
            "目",
            "眼",
            "瞳",
            "口",
            "眉",
            "睫",
            "涙",
            "泪",
        )
    )
)
FACIAL_SHAPEKEY_TEXT_HINTS = (
    "blink",
    "wink",
    "eye",
    "eyes",
    "pupil",
    "stare",
    "calm",
    "hachu",
    "horror",
    "close",
    "mouth",
    "jaw",
    "brow",
    "eyebrow",
    "smile",
    "grin",
    "surprised",
    "blush",
    "tear",
    "tears",
    "serious",
    "sad",
    "sadness",
    "cheerful",
    "anger",
    "angry",
    "tongue",
    "lick",
    "upper",
    "lower",
)


def is_rtx_remix_vertex_limit(vertex_limit: int) -> bool:
    return int(vertex_limit or DEFAULT_SOURCE_VERTEX_LIMIT) <= RTX_REMIX_VERTEX_LIMIT


def rtx_facial_over_limit_warning(name: str, vertex_count: int, vertex_limit: int) -> str:
    return (
        f"{name}: {int(vertex_count):,} vertices exceeds the {int(vertex_limit):,} RTX Remix bodygroup limit. "
        "It was kept merged to avoid duplicated facial flex controllers. "
        "The model can still be used in normal Garry's Mod, but this bodygroup is not RTX Remix compatible."
    )


FACIAL_SHAPEKEY_EXACT_NAMES = {
    "a",
    "i",
    "u",
    "e",
    "o",
    "ah",
    "ch",
    "oh",
    "aa",
    "ii",
    "uu",
    "ee",
    "oo",
}
HAIR_HINTS = ("hair", "bang", "fringe", "liu_hai", "ponytail", "plait", "braid", "发", "髪")
CLOTHES_HINTS = ("cloth", "dress", "skirt", "sleeve", "coat", "shirt", "jacket", "belt", "glove", "stocking", "shoe", "boot", "sock", "cape")
BODY_HINTS = ("body", "skin", "surface", "torso", "arm", "leg", "hand", "foot", "neck")
ACCESSORY_HINTS = ("acc", "ribbon", "bow", "hat", "horn", "ear", "tail", "pendant", "weapon", "ornament", "flower")
AUTO_SPLIT_HIGH_CONFIDENCE = 0.80
AUTO_SPLIT_MEDIUM_CONFIDENCE = 0.55
ADVANCED_AUTO_ENABLE_CONFIDENCE = 0.965
ADVANCED_DISPLAY_MIN_CONFIDENCE = 0.78
SCALE_PRESET_FACE_TOPS = {
    "tall": 69.611259,
    "normal": 63.07148,
    "short": 50.685642,
}


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("analyze", "apply", "validate-manual"), required=True)
    parser.add_argument("--input-blend", type=Path, required=True)
    parser.add_argument("--analysis-json", type=Path)
    parser.add_argument("--plan-json", type=Path)
    parser.add_argument("--output-blend", type=Path)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--manual-edit-blend", type=Path)
    parser.add_argument("--scale-factor", type=float, default=DEFAULT_SCALE_FACTOR)
    parser.add_argument("--scale-preset", choices=("factor", "tall", "normal", "short"), default="factor")
    parser.add_argument("--scale-reference-smd", type=Path)
    parser.add_argument("--vertex-limit", type=int, default=DEFAULT_SOURCE_VERTEX_LIMIT)
    parser.add_argument(
        "--always-auto-split",
        action="store_true",
        help="Run advanced split clustering even when all material-split meshes are within the Source vertex limit.",
    )
    return parser.parse_args(argv)


def write_json(path: Path, data: dict[str, object]) -> None:
    # Compact separators: the analysis JSON can embed hundreds of thousands of
    # preview triangles, and indent=2 multiplies its size several-fold.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def ensure_object_mode() -> None:
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")


def mesh_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.type == "MESH"]


def blender_base_name(name: str) -> str:
    return re.sub(r"\.\d{3}$", "", str(name or ""))


def is_helper_mesh_object(obj: bpy.types.Object) -> bool:
    object_base = blender_base_name(obj.name)
    data_base = blender_base_name(getattr(getattr(obj, "data", None), "name", ""))
    return (
        object_base in HELPER_MESH_NAMES
        or data_base in HELPER_MESH_NAMES
        or obj.name.startswith("VTA vertices")
        or data_base.startswith("VTA vertices")
    )


def armature_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]


def sklearn_status() -> dict[str, object]:
    try:
        import sklearn  # noqa: F401
        from sklearn.cluster import DBSCAN, OPTICS  # noqa: F401

        return {"available": True, "version": getattr(sklearn, "__version__", ""), "method": "sklearn_optics"}
    except Exception as exc:
        return {"available": False, "version": "", "method": "fallback", "warning": str(exc)}


def natural_key(value: object) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value))]


def stripped_safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "", str(name or "").replace(" ", "_"))


def unique_name(base: str, used: set[str], fallback: str) -> str:
    candidate = stripped_safe_name(base) or fallback
    if not SAFE_NAME_RE.fullmatch(candidate):
        candidate = fallback
    root = candidate
    index = 2
    while candidate in used:
        candidate = f"{root}_{index:02d}"
        index += 1
    used.add(candidate)
    return candidate


def strip_generated_material_prefix(name: str) -> str:
    return re.sub(r"^mci_mat_\d+_", "", blender_base_name(str(name or "").strip()), flags=re.IGNORECASE)


def material_bodygroup_name(name: str) -> str:
    cleaned = stripped_safe_name(strip_generated_material_prefix(name))
    if not cleaned or not SAFE_NAME_RE.fullmatch(cleaned):
        return ""
    return capitalized_bodygroup_name(cleaned)


def capitalized_bodygroup_name(name: str) -> str:
    safe = stripped_safe_name(name)
    if safe.lower() == "head":
        return "Face"
    parts = [part for part in safe.split("_") if part]
    if not parts:
        return "Bodygroup"
    out: list[str] = []
    for part in parts:
        if part.upper() in {"L", "R"}:
            out.append(part.upper())
        elif part.isdigit():
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:])
    return "_".join(out)


def default_bodygroup_name(category: str, fallback: str = "Bodygroup") -> str:
    mapping = {
        "body": "Body",
        "head": "Face",
        "hair": "Hair",
        "clothes": "Clothes",
        "accessory": capitalized_bodygroup_name(fallback),
    }
    return mapping.get(str(category or "").lower(), capitalized_bodygroup_name(fallback))


def v3(value: Iterable[float]) -> list[float]:
    vector = list(value)
    return [round(float(vector[0]), 6), round(float(vector[1]), 6), round(float(vector[2]), 6)]


def v4(value: Iterable[float]) -> list[float]:
    vector = list(value)
    out = [float(vector[index]) if index < len(vector) else 1.0 for index in range(4)]
    return [round(max(0.0, min(1.0, item)), 6) for item in out]


def active_armature() -> bpy.types.Object | None:
    arms = armature_objects()
    if not arms:
        return None
    arms.sort(key=lambda obj: len(obj.data.bones), reverse=True)
    return arms[0]


def armature_bone_head_world_z(armature: bpy.types.Object | None, bone_name: str) -> float | None:
    if armature is None or getattr(armature, "data", None) is None:
        return None
    bone = armature.data.bones.get(bone_name)
    if bone is None:
        return None
    try:
        return float((armature.matrix_world @ bone.head_local).z)
    except Exception:
        return None


def associated_meshes(armature: bpy.types.Object | None = None) -> list[bpy.types.Object]:
    meshes = mesh_objects()
    if armature is None:
        return meshes
    associated: list[bpy.types.Object] = []
    for obj in meshes:
        for modifier in obj.modifiers:
            if isinstance(modifier, bpy.types.ArmatureModifier) and modifier.object == armature:
                associated.append(obj)
                break
    return associated or meshes


def combined_bounds(objects: list[bpy.types.Object]) -> tuple[list[float], list[float]]:
    points: list[object] = []
    for obj in objects:
        for corner in obj.bound_box:
            points.append(obj.matrix_world @ Vector(corner))
    if not points:
        return [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
    mins = [min(float(point[index]) for point in points) for index in range(3)]
    maxs = [max(float(point[index]) for point in points) for index in range(3)]
    return mins, maxs


def character_height(meshes: list[bpy.types.Object]) -> float:
    mins, maxs = combined_bounds(meshes)
    return max(0.0, float(maxs[2] - mins[2]))


def character_top_z(meshes: list[bpy.types.Object]) -> float:
    _mins, maxs = combined_bounds(meshes)
    return float(maxs[2])


def material_has_face_scale_hint(mat: bpy.types.Material | None) -> bool:
    if mat is None:
        return False
    haystack = [mat.name]
    texture_path = material_texture_path(mat)
    if texture_path:
        haystack.extend([Path(texture_path).name, Path(texture_path).stem])
    text = " ".join(haystack).lower()
    return any(str(hint).lower() in text for hint in FACE_SCALE_HINTS)


def current_face_top_z(meshes: list[bpy.types.Object]) -> tuple[float, str, int]:
    """Measure the model top using face/head-like materials when available."""

    top: float | None = None
    vertex_hits = 0
    hint_cache: dict[str, bool] = {}
    for obj in meshes:
        world = obj.matrix_world
        # Evaluate the node-tree based face hint once per material slot instead
        # of once per polygon.
        slot_hints: list[bool] = []
        for mat in obj.data.materials:
            key = mat.name if mat is not None else ""
            if key not in hint_cache:
                hint_cache[key] = material_has_face_scale_hint(mat)
            slot_hints.append(hint_cache[key])
        for poly in obj.data.polygons:
            if not (0 <= int(poly.material_index) < len(slot_hints)) or not slot_hints[int(poly.material_index)]:
                continue
            for vertex_index in poly.vertices:
                z_value = float((world @ obj.data.vertices[int(vertex_index)].co).z)
                top = z_value if top is None else max(top, z_value)
                vertex_hits += 1
    if top is not None and vertex_hits:
        return top, "face_material_top", vertex_hits
    return character_top_z(meshes), "character_top", 0


def parse_smd_highest_vertex_z(path: Path) -> float:
    state = ""
    highest: float | None = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in {"nodes", "skeleton", "triangles"}:
            state = line
            continue
        if line == "end":
            state = ""
            continue
        if state != "triangles":
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            float(parts[0])
            z_value = float(parts[3])
        except Exception:
            continue
        highest = z_value if highest is None else max(highest, z_value)
    if highest is None:
        raise RuntimeError(f"No triangle vertices were found in reference SMD: {path}")
    return float(highest)


def resolve_scale_reference(scale_preset: str, scale_reference_smd: Path | None) -> tuple[str, Path | None]:
    preset = str(scale_preset or "factor").lower()
    if scale_reference_smd:
        return preset if preset != "factor" else "custom", scale_reference_smd
    if preset in SCALE_PRESET_FACE_TOPS:
        # Presets use the baked face-top constants; an explicit
        # --scale-reference-smd is the only file-based override.
        return preset, None
    return "factor", None


def apply_uniform_character_scale(scale_factor: float) -> dict[str, object]:
    armature = active_armature()
    targets: list[bpy.types.Object] = []
    if armature is not None:
        targets.append(armature)
    targets.extend(obj for obj in mesh_objects() if obj not in targets)
    report = {
        "requested_scale": float(scale_factor),
        "scaled_objects": [obj.name for obj in targets],
        "physics_scaled": any(obj.name == "Physics" for obj in targets),
        "warnings": [],
    }
    if not targets:
        report["warnings"].append("No armature or mesh objects were available for scaling.")
        return report
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in targets:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = armature or targets[0]
    bpy.ops.transform.resize(value=(scale_factor, scale_factor, scale_factor))
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return report


def maybe_scale_character(
    scale_factor: float,
    scale_preset: str = "factor",
    scale_reference_smd: Path | None = None,
) -> dict[str, object]:
    ensure_object_mode()
    armature = active_armature()
    meshes = mesh_objects() or associated_meshes(armature)
    height_before = character_height(meshes)
    top_before, measurement_method, measurement_vertices = current_face_top_z(meshes)
    preset_name, reference_smd = resolve_scale_reference(scale_preset, scale_reference_smd)
    report = {
        "height_before": round(height_before, 6),
        "scale_factor": float(scale_factor),
        "scale_mode": preset_name,
        "scale_preset": preset_name,
        "reference_smd": str(reference_smd or ""),
        "target_face_top": 0.0,
        "measured_top_before": round(top_before, 6),
        "measurement_method": measurement_method,
        "measurement_vertices": measurement_vertices,
        "actual_scale": 1.0,
        "scaled": False,
        "height_after": round(height_before, 6),
        "measured_top_after": round(top_before, 6),
        "scaled_objects": [],
        "physics_scaled": False,
        "warnings": [],
    }
    if height_before <= 0:
        report["warnings"].append("Could not measure character height.")
        return report
    if preset_name != "factor":
        if reference_smd is not None and reference_smd.exists():
            try:
                target_top = parse_smd_highest_vertex_z(reference_smd)
            except Exception as exc:
                report["warnings"].append(f"Could not read scale preset {preset_name!r}: {exc}")
                return report
        elif preset_name in SCALE_PRESET_FACE_TOPS:
            target_top = SCALE_PRESET_FACE_TOPS[preset_name]
        else:
            report["warnings"].append(f"Scale preset {preset_name!r} is unavailable because its Face.smd was not found: {reference_smd}")
            return report
        report["target_face_top"] = round(target_top, 6)
        if abs(top_before) <= 1e-8:
            report["warnings"].append("Could not compute preset scale because the current face/model top is at zero.")
            return report
        actual_scale = target_top / top_before
        report["actual_scale"] = round(actual_scale, 8)
        if actual_scale <= 0:
            report["warnings"].append(f"Computed non-positive preset scale {actual_scale:.6f}; model was not scaled.")
            return report
        if abs(actual_scale - 1.0) <= 1e-5:
            report["warnings"].append("Model was not scaled because the preset scale is already approximately 1.0.")
            return report
    else:
        if height_before >= 10.0:
            report["warnings"].append("Model was not scaled because its height is already above the threshold.")
            return report
        actual_scale = float(scale_factor)
        report["actual_scale"] = round(actual_scale, 8)
        if actual_scale <= 0:
            report["warnings"].append(f"Requested non-positive scale factor {actual_scale:.6f}; model was not scaled.")
            return report
        if abs(actual_scale - 1.0) <= 1e-5:
            report["warnings"].append("Model was not scaled because the requested factor is approximately 1.0.")
            return report
    transform_report = apply_uniform_character_scale(actual_scale)
    report["scaled_objects"] = transform_report.get("scaled_objects", [])
    report["physics_scaled"] = bool(transform_report.get("physics_scaled", False))
    report["warnings"].extend(transform_report.get("warnings", []))
    meshes_after = mesh_objects() or associated_meshes(active_armature())
    height_after = character_height(meshes_after)
    top_after, method_after, vertices_after = current_face_top_z(meshes_after)
    report["scaled"] = True
    report["height_after"] = round(height_after, 6)
    report["measured_top_after"] = round(top_after, 6)
    report["measurement_method_after"] = method_after
    report["measurement_vertices_after"] = vertices_after
    return report


def select_meshes(meshes: list[bpy.types.Object]) -> None:
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    if meshes:
        bpy.context.view_layer.objects.active = meshes[0]


@contextlib.contextmanager
def suppress_blender_output(enabled: bool):
    if not enabled:
        yield
        return
    null_fd = None
    saved_fds: list[tuple[int, int]] = []
    try:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        null_fd = os.open(os.devnull, os.O_WRONLY)
        for fd in (1, 2):
            try:
                saved_fds.append((fd, os.dup(fd)))
                os.dup2(null_fd, fd)
            except Exception:
                pass
    except Exception:
        saved_fds = []
        null_fd = None
    try:
        yield
    finally:
        for fd, saved_fd in reversed(saved_fds):
            try:
                os.dup2(saved_fd, fd)
            except Exception:
                pass
            try:
                os.close(saved_fd)
            except Exception:
                pass
        if null_fd is not None:
            try:
                os.close(null_fd)
            except Exception:
                pass


def separate_by_materials() -> dict[str, object]:
    ensure_object_mode()
    meshes = mesh_objects()
    before = sorted(obj.name for obj in meshes)
    report = {
        "method": "blender_mesh_separate_material",
        "before_count": len(before),
        "after_count": len(before),
        "warnings": [],
        "notes": ["Using Blender native material separation for deterministic background processing."],
        "suppressed_bmesh_shape_key_warning_objects": [],
    }
    if not meshes:
        report["warnings"].append("No mesh objects found for bodygroup separation.")
        return report
    for obj in list(mesh_objects()):
        if len(obj.data.materials) <= 1 or len(obj.data.polygons) <= 0:
            continue
        has_shape_keys = obj.data.shape_keys is not None and len(obj.data.shape_keys.key_blocks) > 1
        if has_shape_keys:
            report["suppressed_bmesh_shape_key_warning_objects"].append(obj.name)
        ensure_object_mode()
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        with suppress_blender_output(has_shape_keys):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            try:
                bpy.ops.mesh.separate(type="MATERIAL")
            finally:
                bpy.ops.object.mode_set(mode="OBJECT")
    report["after_count"] = len(mesh_objects())
    suppressed_count = len(report["suppressed_bmesh_shape_key_warning_objects"])
    if suppressed_count:
        report["notes"].append(
            f"Suppressed Blender bmesh shape-key conversion warnings during {suppressed_count} material separation operation(s)."
        )
    return report


def shapekey_relative_key(obj: bpy.types.Object, key: bpy.types.ShapeKey) -> bpy.types.ShapeKey | None:
    keys = obj.data.shape_keys
    if keys is None:
        return None
    relative = getattr(key, "relative_key", None)
    if relative is not None:
        return relative
    return keys.key_blocks.get("Basis") or (keys.key_blocks[0] if keys.key_blocks else None)


def shapekey_max_delta(obj: bpy.types.Object, key: bpy.types.ShapeKey, epsilon: float = UNUSED_SHAPEKEY_DELTA_EPSILON) -> float:
    relative = shapekey_relative_key(obj, key)
    if relative is None:
        return 0.0
    count = min(len(key.data), len(relative.data))
    if count <= 0:
        return 0.0
    try:
        import numpy as np

        key_co = np.empty(len(key.data) * 3, dtype=np.float64)
        key.data.foreach_get("co", key_co)
        relative_co = np.empty(len(relative.data) * 3, dtype=np.float64)
        relative.data.foreach_get("co", relative_co)
        deltas = key_co[: count * 3].reshape(-1, 3) - relative_co[: count * 3].reshape(-1, 3)
        return float(np.sqrt((deltas * deltas).sum(axis=1)).max())
    except ImportError:
        pass
    max_delta = 0.0
    for index, item in enumerate(key.data):
        if index >= len(relative.data):
            break
        delta = (item.co - relative.data[index].co).length
        if delta > max_delta:
            max_delta = float(delta)
        if max_delta > epsilon:
            break
    return max_delta


def fallback_prune_shapekeys(obj: bpy.types.Object, epsilon: float = UNUSED_SHAPEKEY_DELTA_EPSILON) -> dict[str, object]:
    keys = obj.data.shape_keys
    if keys is None or len(keys.key_blocks) <= 1:
        return {"removed": [], "method": "fallback", "warnings": [], "epsilon": epsilon, "movement_threshold_units": epsilon}
    removed: list[str] = []
    kept: list[dict[str, object]] = []
    warnings: list[str] = []
    for key in list(keys.key_blocks)[1:]:
        try:
            max_delta = shapekey_max_delta(obj, key, epsilon)
            if max_delta <= epsilon:
                removed.append(key.name)
                obj.shape_key_remove(key)
            else:
                kept.append({"name": key.name, "max_delta": round(float(max_delta), 8)})
        except Exception as exc:
            warnings.append(f"{obj.name}:{key.name}: {exc}")
    return {
        "removed": removed,
        "kept": kept,
        "method": "fallback",
        "warnings": warnings,
        "epsilon": epsilon,
        "movement_threshold_units": epsilon,
    }


def prune_shapekeys(use_cats: bool = False, stage: str = "") -> dict[str, object]:
    report = {
        "objects": [],
        "warnings": [],
        "method": "per_bodygroup_unused_delta_prune",
        "movement_threshold_units": UNUSED_SHAPEKEY_DELTA_EPSILON,
    }
    if stage:
        report["stage"] = stage
    for obj in mesh_objects():
        object_report: dict[str, object] = {
            "object": obj.name,
            "method": "",
            "removed": [],
            "warnings": [],
            "movement_threshold_units": UNUSED_SHAPEKEY_DELTA_EPSILON,
        }
        if obj.data.shape_keys is None or len(obj.data.shape_keys.key_blocks) <= 1:
            object_report["method"] = "none"
            report["objects"].append(object_report)
            continue
        if use_cats:
            try:
                ensure_object_mode()
                bpy.ops.object.select_all(action="DESELECT")
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                result = bpy.ops.cats_shapekey.shape_key_prune()
                object_report["method"] = "cats_shapekey.shape_key_prune"
                object_report["result"] = list(result)
            except Exception as exc:
                object_report["method"] = "cats_shapekey.shape_key_prune_failed"
                object_report["warnings"] = list(object_report.get("warnings", [])) + [f"CATS shapekey prune unavailable: {exc}"]
        else:
            object_report["method"] = "fallback_unused_delta_prune"
        fallback = fallback_prune_shapekeys(obj)
        fallback_removed = list(fallback.get("removed", [])) if isinstance(fallback.get("removed"), list) else []
        fallback_warnings = list(fallback.get("warnings", [])) if isinstance(fallback.get("warnings"), list) else []
        fallback_kept = list(fallback.get("kept", [])) if isinstance(fallback.get("kept"), list) else []
        existing_removed = list(object_report.get("removed", [])) if isinstance(object_report.get("removed"), list) else []
        object_report["removed"] = existing_removed + [name for name in fallback_removed if name not in existing_removed]
        object_report["fallback_removed"] = fallback_removed
        object_report["fallback_kept"] = fallback_kept
        object_report["fallback_method"] = fallback.get("method", "fallback")
        object_report["unused_delta_epsilon"] = fallback.get("epsilon", UNUSED_SHAPEKEY_DELTA_EPSILON)
        object_report["movement_threshold_units"] = fallback.get("movement_threshold_units", UNUSED_SHAPEKEY_DELTA_EPSILON)
        if use_cats and object_report.get("method") == "cats_shapekey.shape_key_prune":
            object_report["method"] = "cats_shapekey.shape_key_prune+fallback_unused_delta_prune"
        elif use_cats and object_report.get("method") == "cats_shapekey.shape_key_prune_failed":
            object_report["method"] = "fallback_unused_delta_prune"
        object_report["warnings"] = list(object_report.get("warnings", [])) + fallback_warnings
        report["objects"].append(object_report)
    return report


def combined_shapekey_prune_report(pre_report: dict[str, object], post_report: dict[str, object]) -> dict[str, object]:
    warnings: list[str] = []
    for report in (pre_report, post_report):
        values = report.get("warnings", []) if isinstance(report, dict) else []
        if isinstance(values, list):
            warnings.extend(str(value) for value in values if value)
    return {
        "method": "pre_and_post_material_separation",
        "movement_threshold_units": UNUSED_SHAPEKEY_DELTA_EPSILON,
        "pre_separation": pre_report,
        "post_separation": post_report,
        "objects": post_report.get("objects", []) if isinstance(post_report.get("objects"), list) else [],
        "warnings": warnings,
    }


def material_texture_path(mat: bpy.types.Material | None) -> str:
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return ""
    for node in mat.node_tree.nodes:
        if getattr(node, "type", "") == "TEX_IMAGE" and getattr(node, "image", None):
            image = node.image
            if image.packed_file:
                return f"packed:{image.name}"
            raw = bpy.path.abspath(image.filepath or "")
            if raw:
                return raw
    return ""


def material_alpha(mat: bpy.types.Material | None) -> float:
    if mat is None:
        return 1.0
    values: list[float] = []
    try:
        values.append(float(mat.diffuse_color[3]))
    except Exception:
        pass
    if mat.node_tree:
        for node in mat.node_tree.nodes:
            node_name = f"{node.name} {getattr(node, 'label', '')} {getattr(node, 'bl_idname', '')}".lower()
            if "mmd_shader" in node_name and len(node.inputs) > 12:
                try:
                    values.append(float(node.inputs[12].default_value))
                except Exception:
                    pass
            if "principled" in node_name or "bsdf" in node_name:
                socket = node.inputs.get("Alpha") if hasattr(node.inputs, "get") else None
                if socket is not None:
                    try:
                        values.append(float(socket.default_value))
                    except Exception:
                        pass
    if not values:
        return 1.0
    return round(max(0.0, min(1.0, min(values))), 6)


def used_materials(obj: bpy.types.Object) -> list[bpy.types.Material]:
    materials: dict[str, bpy.types.Material] = {}
    for poly in obj.data.polygons:
        mat = material_for_polygon(obj, poly)
        if mat is not None:
            materials[mat.name] = mat
    if not materials:
        for mat in obj.data.materials:
            if mat is not None:
                materials[mat.name] = mat
    return [materials[name] for name in sorted(materials, key=natural_key)]


def material_vertex_counts(obj: bpy.types.Object) -> dict[str, int]:
    material_vertices: dict[str, set[int]] = defaultdict(set)
    for poly in obj.data.polygons:
        mat = material_for_polygon(obj, poly)
        if mat is None or not mat.name:
            continue
        material_vertices[str(mat.name)].update(int(index) for index in poly.vertices)
    return {
        name: len(vertices)
        for name, vertices in sorted(material_vertices.items(), key=lambda item: natural_key(item[0]))
    }


def dominant_material_from_counts(counts: dict[str, int]) -> tuple[str, int]:
    valid = [(str(name), int(count or 0)) for name, count in counts.items() if str(name) and int(count or 0) > 0]
    if not valid:
        return "", 0
    valid.sort(key=lambda item: (-item[1], natural_key(item[0])))
    return valid[0]


def aggregate_material_vertex_counts(items: Iterable[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        raw_counts = item.get("material_vertex_counts", {}) if isinstance(item, dict) else {}
        if not isinstance(raw_counts, dict):
            continue
        for name, count in raw_counts.items():
            try:
                counts[str(name)] += int(count)
            except Exception:
                pass
    return {
        name: count
        for name, count in sorted(counts.items(), key=lambda item: natural_key(item[0]))
        if name and count > 0
    }


def material_based_bodygroup_base(
    sources: Iterable[dict[str, object]],
    category: str,
    fallback_name: str,
) -> tuple[str, str, str, int]:
    fallback = capitalized_bodygroup_name(default_bodygroup_name(category, fallback_name))
    counts = aggregate_material_vertex_counts(sources)
    dominant_name, dominant_count = dominant_material_from_counts(counts)
    material_name = material_bodygroup_name(dominant_name)
    if material_name:
        return material_name, "material", dominant_name, dominant_count
    return fallback, "category", dominant_name, dominant_count


def generic_auto_bodygroup_name(name: str) -> bool:
    safe = stripped_safe_name(name).lower()
    if not safe:
        return True
    return bool(re.fullmatch(r"(body|face|clothes|hair|accessory)(_\d+)?", safe)) or safe.startswith(
        ("accessory_", "hair_tie_", "headphone_")
    )


def material_for_polygon(obj: bpy.types.Object, poly: bpy.types.MeshPolygon) -> bpy.types.Material | None:
    materials = obj.data.materials
    if 0 <= int(poly.material_index) < len(materials):
        mat = materials[int(poly.material_index)]
        if mat is not None:
            return mat
    return obj.active_material or (materials[0] if materials else None)


def material_color(mat: bpy.types.Material | None, fallback: list[float]) -> list[float]:
    if mat is None:
        return fallback
    return v4(getattr(mat, "diffuse_color", fallback))


def preview_color(uid: str, index: int, obj: bpy.types.Object) -> list[float]:
    mat = obj.active_material or (obj.data.materials[0] if obj.data.materials else None)
    if mat is not None:
        diffuse = v4(getattr(mat, "diffuse_color", (0.8, 0.8, 0.8, 1.0)))
        if max(diffuse[:3]) - min(diffuse[:3]) > 0.12:
            return diffuse
    seed = uid + obj.name
    value = sum((offset + 1) * ord(char) for offset, char in enumerate(seed))
    red, green, blue = colorsys.hsv_to_rgb((value % 360) / 360.0, 0.58, 0.88)
    return [round(red, 6), round(green, 6), round(blue, 6), 1.0]


def tracking_vertex_groups(obj: bpy.types.Object) -> list[dict[str, object]]:
    tracking_by_index = {
        group.index: group.name
        for group in obj.vertex_groups
        if group.name.startswith(TRACKING_PREFIXES)
    }
    if not tracking_by_index:
        return []
    counts: dict[int, int] = defaultdict(int)
    for vertex in obj.data.vertices:
        for membership in vertex.groups:
            if int(membership.group) in tracking_by_index and membership.weight > 0.0001:
                counts[int(membership.group)] += 1
    out = [
        {"name": tracking_by_index[group_index], "vertex_count": count}
        for group_index, count in counts.items()
        if count
    ]
    out.sort(key=lambda entry: natural_key(entry["name"]))
    return out


def shape_key_names(obj: bpy.types.Object) -> list[str]:
    keys = obj.data.shape_keys
    if keys is None:
        return []
    names: list[str] = []
    for key in keys.key_blocks:
        base = blender_base_name(str(key.name or "")).strip()
        if not base or base.lower() == "basis":
            continue
        try:
            if shapekey_max_delta(obj, key, UNUSED_SHAPEKEY_DELTA_EPSILON) <= UNUSED_SHAPEKEY_DELTA_EPSILON:
                continue
        except Exception:
            pass
        names.append(str(key.name))
    return names


def shape_key_max_deltas(obj: bpy.types.Object) -> dict[str, float]:
    """Active shape keys with their max vertex delta (post-scale units), in
    key-block order. Same active-key filter as shape_key_names()."""
    keys = obj.data.shape_keys
    if keys is None:
        return {}
    deltas: dict[str, float] = {}
    for key in keys.key_blocks:
        base = blender_base_name(str(key.name or "")).strip()
        if not base or base.lower() == "basis":
            continue
        try:
            delta = float(shapekey_max_delta(obj, key, UNUSED_SHAPEKEY_DELTA_EPSILON))
        except Exception:
            # Mirror shape_key_names(): an unreadable key still counts as active.
            delta = float("inf")
        if delta <= UNUSED_SHAPEKEY_DELTA_EPSILON:
            continue
        deltas[str(key.name)] = delta
    return deltas


def normalized_flex_name(name: str) -> str:
    return re.sub(r"[\s_.\-]+", "", blender_base_name(str(name or "")).lower())


def is_facial_shapekey_name(name: str) -> bool:
    lowered = str(name or "").lower()
    compact = normalized_flex_name(name)
    if compact in FACIAL_SHAPEKEY_EXACT_NAMES:
        return True
    return any(hint in lowered or hint in compact for hint in FACIAL_SHAPEKEY_TEXT_HINTS)


# --- Step 7 flex dictionary as a facial signal (issue #131) ----------------
# Step 7 (blender_sort_flexes.py) maps morph names to Source flex names through
# the bundled corpus dictionary; any shape key that dictionary knows will end up
# as a facial flex slider, so Step 6 treats it as facial evidence even when the
# English keyword hints above miss the (often machine-translated) name.

FLEX_NAME_DICTIONARY_PATH = Path(__file__).resolve().parent / "flex_name_dictionary.json"
_FLEX_DICTIONARY_NAMES: tuple[set[str], set[str]] | None = None


def normalized_flex_dictionary_key(name: str) -> str:
    """Mirror blender_sort_flexes.normalized_name so both steps agree on which
    morph names the dictionary covers."""
    text = str(name or "").lower()
    text = text.replace("左", "left").replace("右", "right")
    text = text.replace("ω", "omega")
    return re.sub(r"[^a-z0-9]+", "", text)


def flex_dictionary_names() -> tuple[set[str], set[str]]:
    """(exact, normalized) morph names known to the Step 7 corpus dictionary.
    Cached; empty when the dictionary is missing (facial detection then relies
    on the keyword hints alone, as before)."""
    global _FLEX_DICTIONARY_NAMES
    if _FLEX_DICTIONARY_NAMES is None:
        exact: set[str] = set()
        normalized: set[str] = set()
        try:
            data = json.loads(FLEX_NAME_DICTIONARY_PATH.read_text(encoding="utf-8"))
            mapping = data.get("mapping") if isinstance(data, dict) else None
            if isinstance(mapping, dict):
                for key in mapping:
                    name = str(key)
                    exact.add(name)
                    compact = normalized_flex_dictionary_key(name)
                    if compact:
                        normalized.add(compact)
        except Exception:
            pass
        _FLEX_DICTIONARY_NAMES = (exact, normalized)
    return _FLEX_DICTIONARY_NAMES


def is_dictionary_flex_shapekey(name: str) -> bool:
    exact, normalized = flex_dictionary_names()
    base = blender_base_name(str(name or "")).strip()
    if not base:
        return False
    if base in exact:
        return True
    compact = normalized_flex_dictionary_key(base)
    return bool(compact) and compact in normalized


def is_facialish_shapekey_name(name: str) -> bool:
    return is_facial_shapekey_name(name) or is_dictionary_flex_shapekey(name)


def facial_shapekey_names(obj: bpy.types.Object) -> list[str]:
    return [name for name in shape_key_names(obj) if is_facial_shapekey_name(name)]


def has_facial_merge_text_hint(
    obj: bpy.types.Object,
    materials: list[str],
    tracking: list[dict[str, object]],
) -> bool:
    text = " ".join(
        [
            obj.name,
            *materials,
            *(str(entry.get("name") or "") for entry in tracking if isinstance(entry, dict)),
        ]
    ).lower()
    return any(str(hint).lower() in text for hint in FACIAL_MERGE_TEXT_HINTS)


def unique_sorted(values: Iterable[object]) -> list[str]:
    return sorted({str(value) for value in values if str(value)}, key=natural_key)


def connected_mesh_components(obj: bpy.types.Object, include_vertices: bool = False) -> list[dict[str, object]]:
    parent = list(range(len(obj.data.vertices)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for poly in obj.data.polygons:
        verts = list(poly.vertices)
        for offset in range(1, len(verts)):
            union(verts[0], verts[offset])

    vertex_roots: dict[int, list[int]] = defaultdict(list)
    for vertex in obj.data.vertices:
        vertex_roots[find(int(vertex.index))].append(int(vertex.index))
    face_roots: dict[int, list[bpy.types.MeshPolygon]] = defaultdict(list)
    for poly in obj.data.polygons:
        if not poly.vertices:
            continue
        face_roots[find(int(poly.vertices[0]))].append(poly)

    matrix = obj.matrix_world
    group_names_by_index = {
        group.index: group.name
        for group in obj.vertex_groups
        if group.name.startswith(TRACKING_PREFIXES)
    }
    components: list[dict[str, object]] = []
    for root, vertex_indices in vertex_roots.items():
        if not vertex_indices:
            continue
        points = [matrix @ obj.data.vertices[index].co for index in vertex_indices]
        mins = [min(float(point[axis]) for point in points) for axis in range(3)]
        maxs = [max(float(point[axis]) for point in points) for axis in range(3)]
        centroid = [sum(float(point[axis]) for point in points) / len(points) for axis in range(3)]
        faces = face_roots.get(root, [])
        material_names = sorted(
            {
                str(obj.data.materials[int(poly.material_index)].name)
                for poly in faces
                if 0 <= int(poly.material_index) < len(obj.data.materials) and obj.data.materials[int(poly.material_index)] is not None
            },
            key=natural_key,
        )
        component_material_vertices: dict[str, set[int]] = defaultdict(set)
        for poly in faces:
            mat = material_for_polygon(obj, poly)
            if mat is not None and mat.name:
                component_material_vertices[str(mat.name)].update(int(index) for index in poly.vertices)
        component_material_counts = {
            name: len(vertices)
            for name, vertices in sorted(component_material_vertices.items(), key=lambda item: natural_key(item[0]))
        }
        dominant_component_material, dominant_component_material_count = dominant_material_from_counts(component_material_counts)
        tracking_counts: dict[str, int] = defaultdict(int)
        for vertex_index in vertex_indices:
            vertex = obj.data.vertices[vertex_index]
            for membership in vertex.groups:
                group_name = group_names_by_index.get(int(membership.group))
                if group_name and membership.weight > 0.0001:
                    tracking_counts[group_name] += 1
        component: dict[str, object] = {
            "root": int(root),
            "min_vertex_index": min(vertex_indices),
            "polygon_indices": [int(poly.index) for poly in faces],
            "vertex_count": len(vertex_indices),
            "face_count": len(faces),
            "bounds_min": v3(mins),
            "bounds_max": v3(maxs),
            "centroid": v3(centroid),
            "extent": v3([maxs[axis] - mins[axis] for axis in range(3)]),
            "material_names": material_names,
            "material_vertex_counts": component_material_counts,
            "dominant_material_name": dominant_component_material,
            "dominant_material_vertex_count": dominant_component_material_count,
            "tracking_vertex_groups": [
                {"name": name, "vertex_count": count}
                for name, count in sorted(tracking_counts.items(), key=lambda item: natural_key(item[0]))
            ],
        }
        if include_vertices:
            component["vertex_indices"] = vertex_indices
        components.append(component)
    components.sort(
        key=lambda item: (
            float(item["centroid"][2]),
            float(item["centroid"][1]),
            float(item["centroid"][0]),
            int(item["min_vertex_index"]),
        )
    )
    for index, component in enumerate(components, start=1):
        component["id"] = index
    return components


def component_summaries(obj: bpy.types.Object, include_indices: bool = False) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for component in connected_mesh_components(obj, include_vertices=include_indices):
        item = dict(component)
        item.pop("root", None)
        item.pop("min_vertex_index", None)
        if not include_indices:
            item.pop("polygon_indices", None)
        summaries.append(item)
    return summaries


def mesh_component_count(obj: bpy.types.Object) -> int:
    return len(connected_mesh_components(obj, include_vertices=False))


def classify_source(obj: bpy.types.Object, materials: list[str], tracking: list[dict[str, object]]) -> tuple[str, float, list[str]]:
    text = " ".join([obj.name, *materials, *(str(entry["name"]) for entry in tracking)]).lower()
    warnings: list[str] = []
    if any(hint in text for hint in HEAD_HINTS):
        return "head", 0.86, warnings
    if any(hint in text for hint in HAIR_HINTS):
        return "hair", 0.82, warnings
    if any(hint in text for hint in CLOTHES_HINTS):
        return "clothes", 0.74, warnings
    if any(hint in text for hint in BODY_HINTS):
        return "body", 0.72, warnings
    if any(hint in text for hint in ACCESSORY_HINTS):
        return "accessory", 0.70, warnings
    warnings.append("Low-confidence bodygroup classification.")
    return "accessory", 0.42, warnings


def source_uid(index: int, obj: bpy.types.Object) -> str:
    slug = stripped_safe_name(obj.name).lower() or "bodygroup"
    return f"bgsrc_{index:03d}_{slug[:36]}"


def collect_sources(vertex_limit: int = DEFAULT_SOURCE_VERTEX_LIMIT) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = []
    used_names: set[str] = set()
    neck_base_z = armature_bone_head_world_z(active_armature(), FACE_MERGE_NECK_BONE)
    for index, obj in enumerate(sorted(mesh_objects(), key=lambda item: natural_key(item.name)), start=1):
        if len(obj.data.vertices) <= 0 or len(obj.data.polygons) <= 0:
            continue
        uid = source_uid(index, obj)
        object_materials = used_materials(obj)
        materials = [mat.name for mat in object_materials]
        material_counts = material_vertex_counts(obj)
        dominant_material_name, dominant_material_count = dominant_material_from_counts(material_counts)
        material_alphas = {mat.name: material_alpha(mat) for mat in object_materials}
        zero_alpha_materials = [name for name, alpha in material_alphas.items() if alpha <= 0.001]
        default_enabled = not zero_alpha_materials
        tracking = tracking_vertex_groups(obj)
        shapekey_deltas = shape_key_max_deltas(obj)
        shapekey_names = list(shapekey_deltas.keys())
        facial_names = [name for name in shapekey_names if is_facial_shapekey_name(name)]
        facial_text_hint = has_facial_merge_text_hint(obj, materials, tracking)
        facial_merge_candidate = bool(default_enabled and facial_names)
        facial_merge_reasons: list[str] = []
        if facial_names:
            facial_merge_reasons.append("facial shapekeys")
        if facial_text_hint:
            if facial_names:
                facial_merge_reasons.append("face/head material or tracking hint")
            else:
                facial_merge_reasons.append("face/head material or tracking hint without active facial shapekeys")
        category, confidence, warnings = classify_source(obj, materials, tracking)
        components = component_summaries(obj, include_indices=True)
        component_count = len(components)
        if components:
            source_mins = [min(component_bounds(component)[0][axis] for component in components) for axis in range(3)]
            source_maxs = [max(component_bounds(component)[1][axis] for component in components) for axis in range(3)]
        else:
            source_mins, source_maxs = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        face_merge_neck_filter_passed = True
        if facial_merge_candidate and neck_base_z is not None and source_mins[2] < neck_base_z - FACE_MERGE_NECK_Z_TOLERANCE:
            face_merge_neck_filter_passed = False
            facial_merge_candidate = False
            facial_merge_reasons.append(
                f"excluded from Face merge because vertices extend below {FACE_MERGE_NECK_BONE} base"
            )
            warnings.append(
                f"Not merged into Face because at least one vertex is below {FACE_MERGE_NECK_BONE} base Z "
                f"({source_mins[2]:.6f} < {neck_base_z:.6f})."
            )
        if zero_alpha_materials:
            warnings.append(
                "Disabled by default because material alpha is 0: "
                + ", ".join(zero_alpha_materials[:8])
            )
        if component_count > 1 and tracking:
            warnings.append(f"Object has {component_count} disconnected components with tracking groups.")
        if default_enabled and len(obj.data.vertices) > vertex_limit:
            if facial_merge_candidate:
                if is_rtx_remix_vertex_limit(vertex_limit):
                    warnings.append(
                        f"Face merge candidate exceeds the {vertex_limit:,} RTX Remix bodygroup limit; "
                        "it may be kept merged to preserve facial flexes and only warned as not RTX Remix compatible."
                    )
                else:
                    warnings.append(
                        f"Face merge candidate exceeds Source's {vertex_limit:,} vertex limit; merged Face will fail validation if it remains over limit."
                    )
            else:
                warnings.append(f"Object exceeds Source's {vertex_limit:,} vertex limit and needs additional splitting.")
        if default_enabled and facial_text_hint and not facial_names:
            warnings.append(
                "Not merged into Face because no active facial shapekeys remained after unused shapekey pruning."
            )
        mat = object_materials[0] if object_materials else (obj.active_material or (obj.data.materials[0] if obj.data.materials else None))
        texture_path = material_texture_path(mat)
        proposed = unique_name(default_bodygroup_name(category, obj.name), used_names, f"Bodygroup_{index:03d}")
        sources.append(
            {
                "uid": uid,
                "object_names": [obj.name],
                "material_names": materials,
                "material_vertex_counts": material_counts,
                "dominant_material_name": dominant_material_name,
                "dominant_material_vertex_count": dominant_material_count,
                "material_alphas": material_alphas,
                "zero_alpha_materials": zero_alpha_materials,
                "default_enabled": default_enabled,
                "proposed_name": proposed,
                "category": category,
                "confidence": confidence,
                "vertex_count": len(obj.data.vertices),
                "face_count": len(obj.data.polygons),
                "bounds_min": v3(source_mins),
                "bounds_max": v3(source_maxs),
                "component_count": component_count,
                "components": components,
                "related_vertex_groups": tracking,
                "shapekey_names": shapekey_names,
                "shapekey_max_deltas": {
                    name: (round(delta, 4) if math.isfinite(delta) else 999999.0)
                    for name, delta in shapekey_deltas.items()
                },
                "facial_shapekey_names": facial_names,
                "facial_merge_candidate": facial_merge_candidate,
                "facial_merge_text_hint": facial_text_hint,
                "facial_merge_neck_filter_passed": face_merge_neck_filter_passed,
                "facial_merge_neck_base_z": round(float(neck_base_z), 6) if neck_base_z is not None else None,
                "facial_merge_reasons": facial_merge_reasons,
                "base_color_path": texture_path if not texture_path.startswith("packed:") else "",
                "base_color_file": Path(texture_path).name if texture_path and not texture_path.startswith("packed:") else texture_path,
                "preview_color": preview_color(uid, index, obj),
                "warnings": warnings,
            }
        )
    expand_facial_merge_candidates(sources, vertex_limit)
    return sources


# Merge evidence must be a REAL facial morph: facial-named (keyword hints or
# the Step 7 flex dictionary) AND moving the source by at least this many
# post-scale (Source) units. Real facial morphs move millimetres-to-centimetres
# (>= ~0.1 units); sloppy PMX morphs leave micrometre residues on unrelated
# meshes, and whole-body morphs (transformations, breast sliders) are real but
# not facial-named - both must NOT glue the model into one Face bodygroup.
FACIAL_MERGE_MIN_KEY_DELTA = 0.05
# A below-neck source may bootstrap the Face merge only when it clearly IS the
# face (real faces carry dozens of facial morphs; body parts with a stray
# facial-named morph carry one or two).
FACIAL_BOOTSTRAP_MIN_KEYS = 5
# Overriding the below-neck exclusion needs more than a single shared key:
# face-skin-in-body materials share whole expression sets (dozens of keys),
# while hair/body parts brushed by one facial adjuster morph share just one -
# merging those would cost a real bodygroup to unify a rarely-used slider.
FACIAL_MERGE_NECK_OVERRIDE_MIN_SHARED = 2


def strong_facial_shapekeys(source: dict[str, object]) -> set[str]:
    """Facial-named shape keys that meaningfully move this source."""
    deltas = source.get("shapekey_max_deltas")
    if not isinstance(deltas, dict):
        return set()
    out: set[str] = set()
    for name, delta in deltas.items():
        try:
            if float(delta) < FACIAL_MERGE_MIN_KEY_DELTA:
                continue
        except (TypeError, ValueError):
            continue
        if is_facialish_shapekey_name(str(name)):
            out.add(str(name))
    return out


def expand_facial_merge_candidates(sources: list[dict[str, object]], vertex_limit: int) -> None:
    """Widen the Face merge so one facial morph never spans multiple bodygroups
    (issue #131: each side of the split compiles into its own flex slider and
    the halves move out of sync).

    The per-source pass marks facial_merge_candidate only for sources with
    facial-NAMED shape keys that stay above the neck base. Custom models break
    both assumptions: morph names the keyword hints miss, and face materials
    whose skin extends below the neck (the neck filter then excludes them, e.g.
    Frostnova's 'face' material carrying 41 facial morphs shared with the eyes
    and eyelashes).

    Three additional signals, all based on STRONG facial keys (facial-named
    with a real delta, see strong_facial_shapekeys) and applied greedily while
    the merged Face stays within ``vertex_limit``:
    1. shared-key expansion - a source sharing a strong facial key with the
       current Face set joins it even below the neck, because keeping it
       separate is exactly what splits that flex slider (transitive);
    2. dictionary seed - an above-neck source whose strong keys the Step 7
       flex dictionary knows, even when the keyword hints miss the name;
    3. bootstrap - when nothing seeded the merge at all, a source with many
       strong facial keys seeds it even below the neck
       (face-skin-inside-a-body-material models).

    Non-facial shared morphs (whole-body transformations, breast sliders) are
    deliberately ignored: merging their sources would fuse half the model into
    Face on perfectly healthy ports.
    """
    vertex_limit = max(1, int(vertex_limit or DEFAULT_SOURCE_VERTEX_LIMIT))
    candidates = [
        source
        for source in sources
        if source.get("default_enabled", True) and source.get("facial_merge_candidate", False)
    ]
    face_total = sum(int(source.get("vertex_count", 0) or 0) for source in candidates)
    face_keys: set[str] = set()
    for source in candidates:
        face_keys.update(strong_facial_shapekeys(source))
    pending = [
        source
        for source in sources
        if source.get("default_enabled", True)
        and not source.get("facial_merge_candidate", False)
        and source.get("shapekey_names")
    ]

    def neck_exclusion_ok(source: dict[str, object]) -> bool:
        return source.get("facial_merge_neck_filter_passed", True) is not False

    while pending:
        ranked: list[tuple[tuple, dict[str, object], list[str], list[str]]] = []
        for source in pending:
            strong = strong_facial_shapekeys(source)
            shared = sorted(strong & face_keys, key=natural_key)
            dictionary_named = sorted(
                (name for name in strong if is_dictionary_flex_shapekey(name)), key=natural_key
            )
            neck_ok = neck_exclusion_ok(source)
            shared_needed = 1 if neck_ok else FACIAL_MERGE_NECK_OVERRIDE_MIN_SHARED
            eligible = len(shared) >= shared_needed or (neck_ok and dictionary_named) or (
                not candidates and len(strong) >= FACIAL_BOOTSTRAP_MIN_KEYS
            )
            if not eligible:
                continue
            rank = (
                0 if neck_ok else 1,
                0 if shared else 1,
                -len(shared),
                -len(strong),
                int(source.get("vertex_count", 0) or 0),
                natural_key(str(source.get("uid") or "")),
            )
            ranked.append((rank, source, shared, dictionary_named))
        if not ranked:
            break
        ranked.sort(key=lambda item: item[0])
        _rank, source, shared, dictionary_named = ranked[0]
        pending.remove(source)
        vertex_count = int(source.get("vertex_count", 0) or 0)
        reasons = source.setdefault("facial_merge_reasons", [])
        warnings = source.setdefault("warnings", [])
        if candidates and face_total + vertex_count > vertex_limit:
            reasons.append("skipped by the Face merge vertex-limit cap")
            warnings.append(
                f"Left out of the Face merge to keep it within the {vertex_limit:,} vertex limit"
                + (
                    f"; shared facial shape keys ({', '.join(shared[:6])}) will compile as separate flex sliders."
                    if shared
                    else "."
                )
            )
            continue
        if shared:
            reasons.append(
                f"merged into Face because it shares {len(shared)} facial shape key(s) with the Face merge "
                f"(e.g. {', '.join(shared[:6])}); keeping it separate would split those flex sliders"
            )
        elif dictionary_named:
            reasons.append(
                "merged into Face because its shape keys are facial morphs known to the flex dictionary "
                f"(e.g. {', '.join(dictionary_named[:6])})"
            )
        else:
            reasons.append("seeded the Face merge as the clearest facial morph source")
        if not neck_exclusion_ok(source):
            reasons.append(
                "the below-neck exclusion was overridden by shared facial shape keys"
                if shared
                else "the below-neck exclusion was overridden as the clearest facial morph source"
            )
            # Drop the stale exclusion warning added by the per-source pass.
            source["warnings"] = [
                warning
                for warning in warnings
                if "Not merged into Face because at least one vertex is below" not in str(warning)
            ]
        source["facial_merge_candidate"] = True
        source["facial_merge_expanded"] = True
        candidates.append(source)
        face_total += vertex_count
        face_keys.update(strong_facial_shapekeys(source))

    # Surface the near-misses: a source still sharing facial keys with the
    # merged Face keeps those sliders split, so say so in the plan table.
    for source in pending:
        shared = sorted(strong_facial_shapekeys(source) & face_keys, key=natural_key)
        if shared:
            source.setdefault("warnings", []).append(
                f"Shares {len(shared)} facial shape key(s) with the Face merge (e.g. {', '.join(shared[:4])}) "
                "but was kept separate; those flex sliders will compile once per bodygroup."
            )


def collect_bodygroup_preview(sources: list[dict[str, object]], max_triangles: int = 500000) -> dict[str, object]:
    uid_by_object: dict[str, str] = {}
    entry_by_uid: dict[str, dict[str, object]] = {}
    for entry in sources:
        uid = str(entry["uid"])
        entry_by_uid[uid] = entry
        for object_name in entry.get("object_names", []):
            uid_by_object[str(object_name)] = uid
    total_triangles = sum(max(0, len(poly.vertices) - 2) for obj in mesh_objects() for poly in obj.data.polygons)
    stride = max(1, math.ceil(total_triangles / max(1, max_triangles)))
    triangle_index = 0
    triangles: list[dict[str, object]] = []
    points: list[list[float]] = []
    for obj in sorted(mesh_objects(), key=lambda item: natural_key(item.name)):
        uid = uid_by_object.get(obj.name, "")
        entry = entry_by_uid.get(uid, {})
        component_by_vertex: dict[int, int] = {}
        # Reuse the connected components already computed by collect_sources
        # instead of re-running union-find over every vertex of every object.
        entry_components = entry.get("components", []) if isinstance(entry.get("components"), list) else []
        if entry_components:
            for component in entry_components:
                if not isinstance(component, dict):
                    continue
                component_id = int(component.get("id", 0) or 0)
                for vertex_index in component.get("vertex_indices", []):
                    component_by_vertex[int(vertex_index)] = component_id
        else:
            for component in connected_mesh_components(obj, include_vertices=True):
                component_id = int(component.get("id", 0) or 0)
                for vertex_index in component.get("vertex_indices", []):
                    component_by_vertex[int(vertex_index)] = component_id
        uv_layer = obj.data.uv_layers.active
        matrix = obj.matrix_world
        # Material node-tree walks are expensive; resolve texture/color once
        # per material slot, and only for triangles that pass the stride check.
        slot_info: dict[int, tuple[str, list[float]]] = {}
        for poly in obj.data.polygons:
            verts = list(poly.vertices)
            loops = list(poly.loop_indices)
            if len(verts) < 3:
                continue
            for offset in range(1, len(verts) - 1):
                if triangle_index % stride == 0:
                    slot_index = int(poly.material_index)
                    cached = slot_info.get(slot_index)
                    if cached is None:
                        mat = material_for_polygon(obj, poly)
                        cached = (
                            material_texture_path(mat),
                            material_color(mat, entry.get("preview_color") or [0.8, 0.8, 0.8, 1.0]),
                        )
                        slot_info[slot_index] = cached
                    texture_path, color = cached
                    loop_indices = [loops[0], loops[offset], loops[offset + 1]]
                    uvs = []
                    for loop_index in loop_indices:
                        if uv_layer is not None and 0 <= loop_index < len(uv_layer.data):
                            uv = uv_layer.data[loop_index].uv
                            uvs.append([round(float(uv.x), 6), round(float(uv.y), 6)])
                        else:
                            uvs.append([0.0, 0.0])
                    coords = [
                        v3(matrix @ obj.data.vertices[verts[0]].co),
                        v3(matrix @ obj.data.vertices[verts[offset]].co),
                        v3(matrix @ obj.data.vertices[verts[offset + 1]].co),
                    ]
                    points.extend(coords)
                    triangles.append(
                        {
                            "points": coords,
                            "uvs": uvs,
                            "material_uid": uid,
                            "component_id": component_by_vertex.get(int(verts[0]), 0),
                            "polygon_index": int(poly.index),
                            "object_name": obj.name,
                            "color": color,
                            "texture_path": texture_path if texture_path and not texture_path.startswith("packed:") else entry.get("base_color_path", ""),
                            "alpha": 1.0,
                        }
                    )
                triangle_index += 1
    mins = [min(point[index] for point in points) for index in range(3)] if points else [0.0, 0.0, 0.0]
    maxs = [max(point[index] for point in points) for index in range(3)] if points else [1.0, 1.0, 1.0]
    return {
        "triangles": triangles,
        "source_triangle_count": total_triangles,
        "sampled_triangle_count": len(triangles),
        "sample_stride": stride,
        "mins": mins,
        "maxs": maxs,
    }


def build_bodygroups(sources: list[dict[str, object]]) -> list[dict[str, object]]:
    """Use the material-separated mesh objects as the default bodygroup rows."""
    used_names: set[str] = set()
    groups: list[dict[str, object]] = []
    sorted_sources = sorted(sources, key=lambda item: natural_key(item.get("proposed_name", item.get("uid", ""))))
    face_sources = [
        source
        for source in sorted_sources
        if source.get("default_enabled", True) and source.get("facial_merge_candidate", False)
    ]
    merged_source_uids = {str(source.get("uid") or "") for source in face_sources}
    if face_sources:
        face_name = unique_name("Face", used_names, "Face")
        material_alphas: dict[str, float] = {}
        face_material_counts = aggregate_material_vertex_counts(face_sources)
        face_dominant_material, face_dominant_material_count = dominant_material_from_counts(face_material_counts)
        warnings: set[str] = set()
        for source in face_sources:
            if isinstance(source.get("material_alphas"), dict):
                material_alphas.update({str(key): float(value) for key, value in source["material_alphas"].items()})
            warnings.update(str(warning) for warning in source.get("warnings", []) if warning)
        if len(face_sources) > 1:
            warnings.add(
                f"Merged {len(face_sources)} face-related material sources into one Face bodygroup to keep facial flexes unified."
            )
        groups.append(
            {
                "uid": f"bg_{len(groups) + 1:03d}_{face_name.lower()[:36]}",
                "enabled": True,
                "proposed_name": face_name,
                "source_uids": [str(source.get("uid") or "") for source in face_sources],
                "source_objects": [
                    str(object_name)
                    for source in face_sources
                    for object_name in source.get("object_names", [])
                ],
                "material_names": unique_sorted(
                    material_name
                    for source in face_sources
                    for material_name in source.get("material_names", [])
                ),
                "material_vertex_counts": face_material_counts,
                "dominant_material_name": face_dominant_material,
                "dominant_material_vertex_count": face_dominant_material_count,
                "auto_name_source": "category",
                "material_alphas": material_alphas,
                "zero_alpha_materials": unique_sorted(
                    material_name
                    for source in face_sources
                    for material_name in source.get("zero_alpha_materials", [])
                ),
                "related_vertex_groups": unique_sorted(
                    group["name"]
                    for source in face_sources
                    for group in source.get("related_vertex_groups", [])
                    if isinstance(group, dict) and group.get("name")
                ),
                "category": "head",
                "confidence": round(max(float(source.get("confidence", 0.0) or 0.0) for source in face_sources), 3),
                "vertex_count": sum(int(source.get("vertex_count", 0) or 0) for source in face_sources),
                "face_count": sum(int(source.get("face_count", 0) or 0) for source in face_sources),
                "preview_color": face_sources[0].get("preview_color", [0.8, 0.8, 0.8, 1.0]),
                "base_color_path": next((str(source.get("base_color_path") or "") for source in face_sources if source.get("base_color_path")), ""),
                "base_color_file": next((str(source.get("base_color_file") or "") for source in face_sources if source.get("base_color_file")), ""),
                "warnings": sorted(warnings, key=natural_key),
                "facial_merge": True,
                "merge_role": "face_flex",
                "facial_merge_source_count": len(face_sources),
                "facial_shapekey_names": unique_sorted(
                    name
                    for source in face_sources
                    for name in source.get("facial_shapekey_names", [])
                ),
                "shapekey_names": unique_sorted(
                    name
                    for source in face_sources
                    for name in source.get("shapekey_names", [])
                ),
                "source_categories": unique_sorted(source.get("category", "") for source in face_sources),
                "facial_merge_sources": [
                    {
                        "uid": source.get("uid", ""),
                        "object_names": list(source.get("object_names", [])),
                        "material_names": list(source.get("material_names", [])),
                        "facial_shapekey_names": list(source.get("facial_shapekey_names", [])),
                        "bounds_min": list(source.get("bounds_min", [])),
                        "bounds_max": list(source.get("bounds_max", [])),
                        "facial_merge_neck_filter_passed": bool(source.get("facial_merge_neck_filter_passed", True)),
                        "facial_merge_neck_base_z": source.get("facial_merge_neck_base_z"),
                        "reasons": list(source.get("facial_merge_reasons", [])),
                    }
                    for source in face_sources
                ],
            }
        )
    for source in sorted_sources:
        if str(source.get("uid") or "") in merged_source_uids:
            continue
        category = str(source.get("category") or "accessory")
        index = len(groups) + 1
        source_object_names = list(source.get("object_names", []))
        fallback_name = str(source_object_names[0]) if source_object_names else f"Bodygroup_{index:03d}"
        base_name, auto_name_source, dominant_material_name, dominant_material_count = material_based_bodygroup_base([source], category, fallback_name)
        name = unique_name(capitalized_bodygroup_name(base_name), used_names, f"Bodygroup_{index:03d}")
        enabled = bool(source.get("default_enabled", True))
        groups.append(
            {
                "uid": f"bg_{index:03d}_{name.lower()[:36]}",
                "enabled": enabled,
                "proposed_name": name,
                "source_uids": [str(source.get("uid") or "")],
                "source_objects": list(source.get("object_names", [])),
                "material_names": list(source.get("material_names", [])),
                "material_vertex_counts": dict(source.get("material_vertex_counts", {})) if isinstance(source.get("material_vertex_counts"), dict) else {},
                "dominant_material_name": dominant_material_name,
                "dominant_material_vertex_count": dominant_material_count,
                "auto_name_source": auto_name_source,
                "material_alphas": dict(source.get("material_alphas", {})) if isinstance(source.get("material_alphas"), dict) else {},
                "zero_alpha_materials": list(source.get("zero_alpha_materials", [])) if isinstance(source.get("zero_alpha_materials"), list) else [],
                "related_vertex_groups": [
                    str(group["name"])
                    for group in source.get("related_vertex_groups", [])
                    if isinstance(group, dict) and group.get("name")
                ],
                "category": category,
                "confidence": round(float(source.get("confidence", 0.0) or 0.0), 3),
                "vertex_count": int(source.get("vertex_count", 0) or 0),
                "face_count": int(source.get("face_count", 0) or 0),
                "preview_color": source.get("preview_color", [0.8, 0.8, 0.8, 1.0]),
                "base_color_path": source.get("base_color_path", ""),
                "base_color_file": source.get("base_color_file", ""),
                "shapekey_names": list(source.get("shapekey_names", [])),
                "facial_shapekey_names": list(source.get("facial_shapekey_names", [])),
                "facial_merge_candidate": bool(source.get("facial_merge_candidate", False)),
                "facial_merge_neck_filter_passed": bool(source.get("facial_merge_neck_filter_passed", True)),
                "facial_merge_neck_base_z": source.get("facial_merge_neck_base_z"),
                "warnings": sorted({str(warning) for warning in source.get("warnings", []) if warning}, key=natural_key),
            }
        )
    return groups


def build_facial_merge_report(
    sources: list[dict[str, object]],
    bodygroups: list[dict[str, object]],
    vertex_limit: int,
) -> dict[str, object]:
    candidates = [
        source
        for source in sources
        if source.get("default_enabled", True) and source.get("facial_merge_candidate", False)
    ]
    neck_filtered_sources = [
        {
            "uid": source.get("uid", ""),
            "object_names": list(source.get("object_names", [])),
            "material_names": list(source.get("material_names", [])),
            "vertex_count": int(source.get("vertex_count", 0) or 0),
            "bounds_min": list(source.get("bounds_min", [])),
            "neck_base_z": source.get("facial_merge_neck_base_z"),
            "facial_shapekey_names": list(source.get("facial_shapekey_names", [])),
            "reasons": list(source.get("facial_merge_reasons", [])),
        }
        for source in sources
        if source.get("default_enabled", True)
        and source.get("facial_merge_neck_filter_passed") is False
        # A source re-included by the shared-shape-key expansion (issue #131) is
        # merged despite the neck filter, so it is no longer "excluded".
        and not source.get("facial_merge_candidate", False)
    ]
    merged_group = next(
        (
            group
            for group in bodygroups
            if isinstance(group, dict) and group.get("enabled", True) and group.get("facial_merge")
        ),
        {},
    )
    warnings: list[str] = []
    if not candidates:
        warnings.append("No face-related material-separated sources were detected for merging.")
    if neck_filtered_sources:
        warnings.append(
            f"Excluded {len(neck_filtered_sources)} facial-looking source(s) from Face merge because they extend below "
            f"{FACE_MERGE_NECK_BONE} base."
        )
    protected_over_limit = [
        {
            "uid": source.get("uid", ""),
            "object_names": list(source.get("object_names", [])),
            "vertex_count": int(source.get("vertex_count", 0) or 0),
        }
        for source in candidates
        if int(source.get("vertex_count", 0) or 0) > vertex_limit
    ]
    if protected_over_limit:
        if is_rtx_remix_vertex_limit(vertex_limit):
            warnings.append(
                "One or more face merge sources exceed the RTX Remix bodygroup limit and were protected from automatic split; "
                "normal Garry's Mod can still use the merged bodygroup."
            )
        else:
            warnings.append("One or more face merge sources exceed the vertex limit and were protected from automatic split.")
    merged_vertex_count = int(merged_group.get("vertex_count", 0) or 0) if isinstance(merged_group, dict) else 0
    if merged_vertex_count > vertex_limit:
        if is_rtx_remix_vertex_limit(vertex_limit):
            warnings.append(
                rtx_facial_over_limit_warning(
                    str(merged_group.get("proposed_name") or merged_group.get("name") or "Face"),
                    merged_vertex_count,
                    vertex_limit,
                )
            )
        else:
            warnings.append("Merged Face exceeds the vertex limit and will fail validation if applied unchanged.")
    return {
        "enabled": bool(candidates),
        "merged": len(candidates) > 1,
        "candidate_count": len(candidates),
        "target_bodygroup": merged_group.get("proposed_name", "Face") if candidates else "",
        "merged_vertex_count": merged_vertex_count,
        "merged_over_vertex_limit": bool(merged_vertex_count > vertex_limit),
        "neck_filter_bone": FACE_MERGE_NECK_BONE,
        "neck_filtered_source_count": len(neck_filtered_sources),
        "neck_filtered_sources": neck_filtered_sources,
        "source_uids": [str(source.get("uid") or "") for source in candidates],
        "source_objects": [
            str(object_name)
            for source in candidates
            for object_name in source.get("object_names", [])
        ],
        "materials": unique_sorted(
            material_name
            for source in candidates
            for material_name in source.get("material_names", [])
        ),
        "facial_shapekey_names": unique_sorted(
            name
            for source in candidates
            for name in source.get("facial_shapekey_names", [])
        ),
        "protected_over_limit_sources": protected_over_limit,
        "sources": [
            {
                "uid": source.get("uid", ""),
                "object_names": list(source.get("object_names", [])),
                "material_names": list(source.get("material_names", [])),
                "category": source.get("category", ""),
                "vertex_count": int(source.get("vertex_count", 0) or 0),
                "facial_shapekey_names": list(source.get("facial_shapekey_names", [])),
                "reasons": list(source.get("facial_merge_reasons", [])),
            }
            for source in candidates
        ],
        "warnings": warnings,
    }


def auto_split_policy(sources: list[dict[str, object]], always_auto_split: bool, vertex_limit: int = DEFAULT_SOURCE_VERTEX_LIMIT) -> dict[str, object]:
    over_limit = [
        {
            "uid": str(source.get("uid") or ""),
            "name": str(source.get("proposed_name") or source.get("uid") or ""),
            "object_names": list(source.get("object_names", [])),
            "vertex_count": int(source.get("vertex_count", 0) or 0),
        }
        for source in sources
        if source.get("default_enabled", True) and int(source.get("vertex_count", 0) or 0) > vertex_limit
        and not source.get("facial_merge_candidate", False)
    ]
    protected_face_over_limit = [
        {
            "uid": str(source.get("uid") or ""),
            "name": str(source.get("proposed_name") or source.get("uid") or ""),
            "object_names": list(source.get("object_names", [])),
            "vertex_count": int(source.get("vertex_count", 0) or 0),
        }
        for source in sources
        if source.get("default_enabled", True) and int(source.get("vertex_count", 0) or 0) > vertex_limit
        and source.get("facial_merge_candidate", False)
    ]
    required = bool(always_auto_split or over_limit)
    if always_auto_split:
        reason = "always"
    elif over_limit:
        reason = "source_vertex_limit"
    else:
        reason = "not_required"
    return {
        "vertex_limit": vertex_limit,
        "always": bool(always_auto_split),
        "required": required,
        "reason": reason,
        "over_limit_sources": over_limit,
        "protected_face_over_limit_sources": protected_face_over_limit,
    }


def auto_split_sources(sources: list[dict[str, object]], policy: dict[str, object]) -> list[dict[str, object]]:
    enabled_sources = [
        source
        for source in sources
        if source.get("default_enabled", True) and not source.get("facial_merge_candidate", False)
    ]
    if policy.get("always"):
        return enabled_sources
    over_limit_uids = {str(entry.get("uid") or "") for entry in policy.get("over_limit_sources", []) if isinstance(entry, dict)}
    return [source for source in enabled_sources if str(source.get("uid") or "") in over_limit_uids]


def scene_metrics() -> dict[str, object]:
    mins, maxs = combined_bounds(mesh_objects())
    extent = [maxs[index] - mins[index] for index in range(3)]
    return {
        "bounds_min": mins,
        "bounds_max": maxs,
        "extent": extent,
        "center": [(mins[index] + maxs[index]) * 0.5 for index in range(3)],
        "height": max(0.001, extent[2]),
    }


def component_center(component: dict[str, object]) -> list[float]:
    raw = component.get("centroid", [0.0, 0.0, 0.0])
    return [float(raw[index]) if isinstance(raw, list) and index < len(raw) else 0.0 for index in range(3)]


def component_bounds(component: dict[str, object]) -> tuple[list[float], list[float]]:
    raw_min = component.get("bounds_min", [0.0, 0.0, 0.0])
    raw_max = component.get("bounds_max", [0.0, 0.0, 0.0])
    mins = [float(raw_min[index]) if isinstance(raw_min, list) and index < len(raw_min) else 0.0 for index in range(3)]
    maxs = [float(raw_max[index]) if isinstance(raw_max, list) and index < len(raw_max) else mins[index] for index in range(3)]
    return mins, maxs


def component_bbox_gap(a: dict[str, object], b: dict[str, object]) -> float:
    amin, amax = component_bounds(a)
    bmin, bmax = component_bounds(b)
    squared = 0.0
    for axis in range(3):
        if amax[axis] < bmin[axis]:
            gap = bmin[axis] - amax[axis]
        elif bmax[axis] < amin[axis]:
            gap = amin[axis] - bmax[axis]
        else:
            gap = 0.0
        squared += gap * gap
    return math.sqrt(squared)


def cluster_components(components: list[dict[str, object]], radius: float, center_x: float) -> list[list[dict[str, object]]]:
    if not components:
        return []
    parent = list(range(len(components)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for left in range(len(components)):
        left_center = component_center(components[left])
        left_side = left_center[0] >= center_x
        for right in range(left + 1, len(components)):
            right_center = component_center(components[right])
            if (right_center[0] >= center_x) != left_side:
                continue
            center_distance = math.sqrt(sum((left_center[axis] - right_center[axis]) ** 2 for axis in range(3)))
            if center_distance <= radius or component_bbox_gap(components[left], components[right]) <= radius * 0.45:
                union(left, right)
    buckets: dict[int, list[dict[str, object]]] = defaultdict(list)
    for index, component in enumerate(components):
        buckets[find(index)].append(component)
    clusters = list(buckets.values())
    clusters.sort(key=lambda items: natural_key([component.get("id") for component in items]))
    return clusters


def combined_component_bounds(components: list[dict[str, object]]) -> tuple[list[float], list[float], list[float]]:
    mins = [min(component_bounds(component)[0][axis] for component in components) for axis in range(3)]
    maxs = [max(component_bounds(component)[1][axis] for component in components) for axis in range(3)]
    total_vertices = max(1, sum(int(component.get("vertex_count", 0) or 0) for component in components))
    centroid = [0.0, 0.0, 0.0]
    for component in components:
        weight = int(component.get("vertex_count", 0) or 0) / total_vertices
        center = component_center(component)
        for axis in range(3):
            centroid[axis] += center[axis] * weight
    return mins, maxs, centroid


def cluster_tracking_groups(components: list[dict[str, object]]) -> list[str]:
    names: set[str] = set()
    for component in components:
        for group in component.get("tracking_vertex_groups", []):
            if isinstance(group, dict) and group.get("name"):
                names.add(str(group["name"]))
    return sorted(names, key=natural_key)


def cluster_polygon_indices(components: list[dict[str, object]]) -> list[int]:
    values: set[int] = set()
    for component in components:
        for index in component.get("polygon_indices", []):
            try:
                values.add(int(index))
            except Exception:
                pass
    return sorted(values)


def cluster_vertex_indices(components: list[dict[str, object]]) -> list[int]:
    values: set[int] = set()
    for component in components:
        for index in component.get("vertex_indices", []):
            try:
                values.add(int(index))
            except Exception:
                pass
    return sorted(values)


def cluster_material_vertex_counts(components: list[dict[str, object]]) -> dict[str, int]:
    return aggregate_material_vertex_counts(components)


def material_named_split_base(default_name: str, components: list[dict[str, object]]) -> tuple[str, str, str, int]:
    counts = cluster_material_vertex_counts(components)
    dominant_name, dominant_count = dominant_material_from_counts(counts)
    material_name = material_bodygroup_name(dominant_name)
    if material_name and generic_auto_bodygroup_name(default_name):
        return material_name, "material", dominant_name, dominant_count
    return default_name, "category", dominant_name, dominant_count


def stripped_source_components(sources: list[dict[str, object]]) -> list[dict[str, object]]:
    public_sources: list[dict[str, object]] = []
    for source in sources:
        copied = dict(source)
        components: list[dict[str, object]] = []
        for component in source.get("components", []):
            if not isinstance(component, dict):
                continue
            item = dict(component)
            item.pop("vertex_indices", None)
            item.pop("polygon_indices", None)
            components.append(item)
        copied["components"] = components
        public_sources.append(copied)
    return public_sources


def split_candidate_name(source: dict[str, object], centroid: list[float], extent: list[float], metrics: dict[str, object]) -> str:
    center = metrics.get("center", [0.0, 0.0, 0.0])
    height = float(metrics.get("height", 1.0) or 1.0)
    side = "L" if centroid[0] >= float(center[0]) else "R"
    category = str(source.get("category") or "")
    text = " ".join(
        [
            category,
            *(str(name) for name in source.get("material_names", []) if name),
            *(str(group) for group in source.get("related_vertex_groups", []) if group),
        ]
    ).lower()
    horizontal = max(extent[0], extent[1], 0.001)
    if "ribbon" in text or "bow" in text:
        return f"ribbon_{side}"
    if category == "hair" or (extent[2] > horizontal * 1.35 and extent[2] > height * 0.035):
        return f"hair_tie_{side}"
    if centroid[2] > float(metrics["bounds_min"][2]) + height * 0.62:
        return f"headphone_{side}"
    return f"accessory_{side}"


def detect_heuristic_split_candidates(sources: list[dict[str, object]], bodygroups: list[dict[str, object]], metrics: dict[str, object]) -> list[dict[str, object]]:
    source_to_group: dict[str, dict[str, object]] = {}
    for group in bodygroups:
        for source_uid in group.get("source_uids", []):
            source_to_group[str(source_uid)] = group
    candidates: list[dict[str, object]] = []
    center = metrics.get("center", [0.0, 0.0, 0.0])
    height = float(metrics.get("height", 1.0) or 1.0)
    z_min = float(metrics["bounds_min"][2])
    for source in sources:
        category = str(source.get("category") or "")
        if category not in {"clothes", "hair", "head"}:
            continue
        components = [component for component in source.get("components", []) if isinstance(component, dict)]
        if len(components) < 2:
            continue
        source_vertices = max(1, int(source.get("vertex_count", 0) or 1))
        selectable: list[dict[str, object]] = []
        for component in components:
            vertex_count = int(component.get("vertex_count", 0) or 0)
            face_count = int(component.get("face_count", 0) or 0)
            centroid = component_center(component)
            mins, maxs = component_bounds(component)
            extent = [maxs[axis] - mins[axis] for axis in range(3)]
            ratio = vertex_count / source_vertices
            high_region = centroid[2] >= z_min + height * (0.58 if category == "clothes" else 0.55)
            small_enough = max(extent) <= height * (0.24 if category == "clothes" else 0.20)
            if vertex_count >= 12 and face_count >= 4 and ratio <= 0.18 and high_region and small_enough:
                selectable.append(component)
        clusters = cluster_components(selectable, max(0.18, height * 0.045), float(center[0]))
        for cluster in clusters:
            vertex_count = sum(int(component.get("vertex_count", 0) or 0) for component in cluster)
            face_count = sum(int(component.get("face_count", 0) or 0) for component in cluster)
            if vertex_count < 24 or face_count < 8:
                continue
            mins, maxs, centroid = combined_component_bounds(cluster)
            extent = [maxs[axis] - mins[axis] for axis in range(3)]
            ratio = vertex_count / source_vertices
            if ratio > 0.22 or max(extent) > height * 0.26:
                continue
            confidence = 0.48
            if category == "clothes":
                confidence += 0.18
            elif category == "hair":
                confidence += 0.10
            if centroid[2] >= z_min + height * 0.66:
                confidence += 0.14
            elif centroid[2] >= z_min + height * 0.58:
                confidence += 0.08
            if 0.001 <= ratio <= 0.10:
                confidence += 0.10
            if abs(centroid[0] - float(center[0])) >= height * 0.018:
                confidence += 0.05
            if max(extent) <= height * 0.16:
                confidence += 0.05
            if len(cluster) >= 2:
                confidence += 0.03
            confidence = round(min(0.96, confidence), 3)
            base_name = capitalized_bodygroup_name(split_candidate_name(source, centroid, extent, metrics))
            material_counts = cluster_material_vertex_counts(cluster)
            base_name, auto_name_source, dominant_material_name, dominant_material_count = material_named_split_base(base_name, cluster)
            if category == "head" or base_name.lower().startswith("accessory_"):
                confidence = min(confidence, 0.74)
            source_uid_value = str(source.get("uid") or "")
            parent_group = source_to_group.get(source_uid_value, {})
            candidates.append(
                {
                    "uid": "",
                    "type": "component_cluster",
                    "source_uid": source_uid_value,
                    "source_bodygroup_uid": parent_group.get("uid", ""),
                    "source_bodygroup_name": parent_group.get("proposed_name", ""),
                    "source_objects": list(source.get("object_names", [])),
                    "component_ids": [int(component.get("id", 0) or 0) for component in cluster],
                    "polygon_indices": cluster_polygon_indices(cluster),
                    "vertex_indices": cluster_vertex_indices(cluster),
                    "component_count": len(cluster),
                    "base_name": base_name,
                    "proposed_name": base_name,
                    "category": "accessory",
                    "confidence": confidence,
                    "enabled": confidence >= AUTO_SPLIT_HIGH_CONFIDENCE,
                    "vertex_count": vertex_count,
                    "face_count": face_count,
                    "bounds_min": v3(mins),
                    "bounds_max": v3(maxs),
                    "centroid": v3(centroid),
                    "extent": v3(extent),
                    "material_names": sorted({name for component in cluster for name in component.get("material_names", [])}, key=natural_key),
                    "material_vertex_counts": material_counts,
                    "dominant_material_name": dominant_material_name,
                    "dominant_material_vertex_count": dominant_material_count,
                    "auto_name_source": auto_name_source,
                    "related_vertex_groups": cluster_tracking_groups(cluster),
                    "warnings": [] if confidence >= AUTO_SPLIT_HIGH_CONFIDENCE else ["Medium-confidence split candidate; review before enabling."],
                }
            )
    candidates.sort(key=lambda item: (-float(item.get("confidence", 0.0) or 0.0), natural_key(item.get("proposed_name", ""))))
    used_names = {str(group.get("proposed_name") or "") for group in bodygroups}
    for index, candidate in enumerate(candidates, start=1):
        proposed_name = unique_name(capitalized_bodygroup_name(str(candidate.get("base_name") or "Accessory")), used_names, f"Accessory_{index:03d}")
        candidate["proposed_name"] = proposed_name
        candidate["uid"] = f"bg_split_auto_{index:03d}_{proposed_name.lower()[:28]}"
    return candidates


def source_category_bounds(sources: list[dict[str, object]], categories: set[str]) -> tuple[list[float], list[float]] | None:
    mins: list[float] | None = None
    maxs: list[float] | None = None
    for source in sources:
        if str(source.get("category") or "") not in categories:
            continue
        raw_min = source.get("bounds_min")
        raw_max = source.get("bounds_max")
        if not isinstance(raw_min, list) or not isinstance(raw_max, list) or len(raw_min) < 3 or len(raw_max) < 3:
            continue
        smin = [float(raw_min[index]) for index in range(3)]
        smax = [float(raw_max[index]) for index in range(3)]
        mins = smin if mins is None else [min(mins[index], smin[index]) for index in range(3)]
        maxs = smax if maxs is None else [max(maxs[index], smax[index]) for index in range(3)]
    if mins is None or maxs is None:
        return None
    return mins, maxs


def split_landmarks(sources: list[dict[str, object]], metrics: dict[str, object]) -> dict[str, object]:
    height = float(metrics.get("height", 1.0) or 1.0)
    z_min = float(metrics.get("bounds_min", [0.0, 0.0, 0.0])[2])
    z_max = float(metrics.get("bounds_max", [0.0, 0.0, 1.0])[2])
    default_head_min = z_min + height * 0.76
    default_head_max = z_max
    head_bounds = source_category_bounds(sources, {"head"})
    hair_bounds = source_category_bounds(sources, {"hair"})
    if head_bounds:
        head_min, head_max = head_bounds
    else:
        head_min = [float(metrics.get("center", [0.0, 0.0, 0.0])[0]), float(metrics.get("center", [0.0, 0.0, 0.0])[1]), default_head_min]
        head_max = [head_min[0], head_min[1], default_head_max]
    if hair_bounds:
        head_max = [max(head_max[index], hair_bounds[1][index]) for index in range(3)]
    head_floor = max(z_min + height * 0.74, float(head_min[2]) - height * 0.07)
    head_ceiling = min(z_max + height * 0.02, max(float(head_max[2]), default_head_min) + height * 0.10)
    return {
        "head_bounds_min": v3(head_min),
        "head_bounds_max": v3(head_max),
        "head_floor": head_floor,
        "head_ceiling": head_ceiling,
        "head_center": [
            (float(head_min[index]) + float(head_max[index])) * 0.5
            for index in range(3)
        ],
    }


def advanced_component_candidates(source: dict[str, object], metrics: dict[str, object], landmarks: dict[str, object]) -> list[dict[str, object]]:
    category = str(source.get("category") or "")
    if category not in {"clothes", "hair", "head"}:
        return []
    height = float(metrics.get("height", 1.0) or 1.0)
    center = metrics.get("center", [0.0, 0.0, 0.0])
    center_x = float(center[0]) if isinstance(center, list) and center else 0.0
    source_vertices = max(1, int(source.get("vertex_count", 0) or 1))
    head_floor = float(landmarks.get("head_floor", 0.0) or 0.0)
    head_ceiling = float(landmarks.get("head_ceiling", 0.0) or 0.0)
    selected: list[dict[str, object]] = []
    for component in source.get("components", []):
        if not isinstance(component, dict):
            continue
        vertex_count = int(component.get("vertex_count", 0) or 0)
        face_count = int(component.get("face_count", 0) or 0)
        if vertex_count < 8 or face_count < 6:
            continue
        centroid = component_center(component)
        mins, maxs = component_bounds(component)
        extent = [maxs[axis] - mins[axis] for axis in range(3)]
        max_extent = max(extent)
        horizontal_extent = max(extent[0], extent[1])
        ratio = vertex_count / source_vertices
        if centroid[2] < head_floor or centroid[2] > head_ceiling:
            continue
        if ratio > 0.16:
            continue
        max_allowed_extent = height * (0.18 if category == "hair" else 0.16)
        if max_extent > max_allowed_extent:
            continue
        central_width = abs(centroid[0] - center_x) < height * 0.012
        if category == "clothes" and central_width and horizontal_extent > height * 0.10:
            continue
        selected.append(component)
    return selected


def component_feature(component: dict[str, object], metrics: dict[str, object], landmarks: dict[str, object]) -> list[float]:
    height = float(metrics.get("height", 1.0) or 1.0)
    center = metrics.get("center", [0.0, 0.0, 0.0])
    center = center if isinstance(center, list) and len(center) >= 3 else [0.0, 0.0, 0.0]
    head_center = landmarks.get("head_center", center)
    head_center = head_center if isinstance(head_center, list) and len(head_center) >= 3 else center
    centroid = component_center(component)
    mins, maxs = component_bounds(component)
    extent = [maxs[axis] - mins[axis] for axis in range(3)]
    vertex_count = max(1, int(component.get("vertex_count", 1) or 1))
    face_count = max(1, int(component.get("face_count", 1) or 1))
    return [
        (centroid[0] - float(center[0])) / height,
        (centroid[1] - float(center[1])) / height,
        (centroid[2] - float(center[2])) / height * 1.25,
        (centroid[0] - float(head_center[0])) / height * 0.55,
        (centroid[1] - float(head_center[1])) / height * 0.55,
        extent[0] / height * 0.50,
        extent[1] / height * 0.50,
        extent[2] / height * 0.50,
        math.log1p(vertex_count) / 8.0,
        math.log1p(face_count) / 8.0,
    ]


def adaptive_eps(features: list[list[float]], min_samples: int) -> float:
    if len(features) < 2:
        return 0.08
    kth: list[float] = []
    for left, feature in enumerate(features):
        distances: list[float] = []
        for right, other in enumerate(features):
            if left == right:
                continue
            distances.append(math.sqrt(sum((feature[index] - other[index]) ** 2 for index in range(len(feature)))))
        distances.sort()
        if distances:
            kth.append(distances[min(max(0, min_samples - 1), len(distances) - 1)])
    if not kth:
        return 0.08
    kth.sort()
    value = kth[min(len(kth) - 1, max(0, int(len(kth) * 0.70)))]
    return max(0.035, min(0.18, value * 1.35))


def sklearn_cluster_components(components: list[dict[str, object]], metrics: dict[str, object], landmarks: dict[str, object]) -> tuple[list[list[dict[str, object]]], str]:
    if not components:
        return [], "none"
    if len(components) == 1:
        return [components], "singleton"
    from sklearn.cluster import DBSCAN, OPTICS

    height = float(metrics.get("height", 1.0) or 1.0)
    center_x = float(metrics.get("center", [0.0, 0.0, 0.0])[0])
    side_cutoff = height * 0.010
    buckets: dict[str, list[dict[str, object]]] = defaultdict(list)
    for component in components:
        x = component_center(component)[0]
        if x < center_x - side_cutoff:
            bucket = "right"
        elif x > center_x + side_cutoff:
            bucket = "left"
        else:
            bucket = "center"
        buckets[bucket].append(component)

    clusters: list[list[dict[str, object]]] = []
    method = "sklearn_optics"
    for bucket_components in buckets.values():
        if len(bucket_components) == 1:
            clusters.append(bucket_components)
            continue
        features = [component_feature(component, metrics, landmarks) for component in bucket_components]
        labels: list[int]
        try:
            min_samples = 2 if len(features) < 8 else 3
            optics = OPTICS(min_samples=min_samples, min_cluster_size=2, max_eps=0.20, xi=0.08)
            labels = [int(label) for label in optics.fit_predict(features)]
        except Exception:
            labels = []
        grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
        for component, label in zip(bucket_components, labels):
            if label >= 0:
                grouped[label].append(component)
        if not grouped:
            method = "sklearn_dbscan"
            min_samples = 2
            eps = adaptive_eps(features, min_samples)
            labels = [int(label) for label in DBSCAN(eps=eps, min_samples=min_samples).fit_predict(features)]
            for component, label in zip(bucket_components, labels):
                if label >= 0:
                    grouped[label].append(component)
        clustered_ids = {int(component.get("id", 0) or 0) for items in grouped.values() for component in items}
        clusters.extend(grouped.values())
        for component in bucket_components:
            if int(component.get("id", 0) or 0) not in clustered_ids and int(component.get("vertex_count", 0) or 0) >= 48:
                clusters.append([component])
    return clusters, method


def advanced_split_candidate_name(source: dict[str, object], centroid: list[float], extent: list[float], metrics: dict[str, object], landmarks: dict[str, object]) -> str:
    center = metrics.get("center", [0.0, 0.0, 0.0])
    center_x = float(center[0]) if isinstance(center, list) and center else 0.0
    side = "L" if centroid[0] >= center_x else "R"
    category = str(source.get("category") or "")
    text = " ".join(
        [
            category,
            *(str(name) for name in source.get("material_names", []) if name),
            *(str(group) for group in source.get("related_vertex_groups", []) if group),
        ]
    ).lower()
    horizontal = max(extent[0], extent[1], 0.001)
    head_max_z = float(landmarks.get("head_bounds_max", [0.0, 0.0, centroid[2]])[2])
    height = float(metrics.get("height", 1.0) or 1.0)
    if "ribbon" in text or "bow" in text:
        return f"Ribbon_{side}"
    if category == "hair" or extent[2] > horizontal * 1.35 or centroid[2] > head_max_z - height * 0.025:
        return f"Hair_Tie_{side}"
    if abs(centroid[0] - center_x) > height * 0.018:
        return f"Headphone_{side}"
    return f"Accessory_{side}"


def advanced_candidate_confidence(source: dict[str, object], cluster: list[dict[str, object]], centroid: list[float], extent: list[float], metrics: dict[str, object], landmarks: dict[str, object]) -> float:
    height = float(metrics.get("height", 1.0) or 1.0)
    center = metrics.get("center", [0.0, 0.0, 0.0])
    center_x = float(center[0]) if isinstance(center, list) and center else 0.0
    category = str(source.get("category") or "")
    head_min_z = float(landmarks.get("head_bounds_min", [0.0, 0.0, centroid[2]])[2])
    head_floor = float(landmarks.get("head_floor", centroid[2]) or centroid[2])
    max_extent = max(extent)
    confidence = 0.58
    if category == "clothes":
        confidence += 0.10
    elif category == "hair":
        confidence -= 0.04
    elif category == "head":
        confidence -= 0.04
    if centroid[2] >= head_min_z:
        confidence += 0.12
    elif centroid[2] >= head_floor + height * 0.04:
        confidence += 0.04
    else:
        confidence -= 0.12
    if len(cluster) >= 2:
        confidence += 0.08
    if len(cluster) >= 6:
        confidence += 0.03
    if max_extent <= height * 0.12:
        confidence += 0.06
    if abs(centroid[0] - center_x) >= height * 0.018:
        confidence += 0.05
    if max_extent > height * 0.18:
        confidence -= 0.12
    cap = 0.94 if category == "hair" else 0.97
    return round(max(0.45, min(cap, confidence)), 3)


def detect_advanced_split_candidates(
    sources: list[dict[str, object]],
    bodygroups: list[dict[str, object]],
    metrics: dict[str, object],
) -> list[dict[str, object]]:
    source_to_group: dict[str, dict[str, object]] = {}
    for group in bodygroups:
        for source_uid in group.get("source_uids", []):
            source_to_group[str(source_uid)] = group
    landmarks = split_landmarks(sources, metrics)
    candidates: list[dict[str, object]] = []
    for source in sources:
        components = advanced_component_candidates(source, metrics, landmarks)
        if not components:
            continue
        clusters, method = sklearn_cluster_components(components, metrics, landmarks)
        source_vertices = max(1, int(source.get("vertex_count", 0) or 1))
        for cluster in clusters:
            vertex_count = sum(int(component.get("vertex_count", 0) or 0) for component in cluster)
            face_count = sum(int(component.get("face_count", 0) or 0) for component in cluster)
            if vertex_count < 24 or face_count < 12:
                continue
            mins, maxs, centroid = combined_component_bounds(cluster)
            extent = [maxs[axis] - mins[axis] for axis in range(3)]
            ratio = vertex_count / source_vertices
            height = float(metrics.get("height", 1.0) or 1.0)
            if ratio > 0.22 or max(extent) > height * 0.24:
                continue
            confidence = advanced_candidate_confidence(source, cluster, centroid, extent, metrics, landmarks)
            base_name = capitalized_bodygroup_name(advanced_split_candidate_name(source, centroid, extent, metrics, landmarks))
            material_counts = cluster_material_vertex_counts(cluster)
            base_name, auto_name_source, dominant_material_name, dominant_material_count = material_named_split_base(base_name, cluster)
            source_uid_value = str(source.get("uid") or "")
            parent_group = source_to_group.get(source_uid_value, {})
            warnings: list[str] = []
            if confidence < ADVANCED_AUTO_ENABLE_CONFIDENCE:
                warnings.append("Advanced medium-confidence split candidate; review before enabling.")
            candidates.append(
                {
                    "uid": "",
                    "type": "region_cluster",
                    "method": method,
                    "source_uid": source_uid_value,
                    "source_bodygroup_uid": parent_group.get("uid", ""),
                    "source_bodygroup_name": parent_group.get("proposed_name", ""),
                    "source_objects": list(source.get("object_names", [])),
                    "component_ids": [int(component.get("id", 0) or 0) for component in cluster],
                    "polygon_indices": cluster_polygon_indices(cluster),
                    "vertex_indices": cluster_vertex_indices(cluster),
                    "component_count": len(cluster),
                    "base_name": base_name,
                    "proposed_name": base_name,
                    "category": "accessory",
                    "confidence": confidence,
                    "enabled": confidence >= ADVANCED_AUTO_ENABLE_CONFIDENCE,
                    "vertex_count": vertex_count,
                    "face_count": face_count,
                    "bounds_min": v3(mins),
                    "bounds_max": v3(maxs),
                    "centroid": v3(centroid),
                    "extent": v3(extent),
                    "material_names": sorted({name for component in cluster for name in component.get("material_names", [])}, key=natural_key),
                    "material_vertex_counts": material_counts,
                    "dominant_material_name": dominant_material_name,
                    "dominant_material_vertex_count": dominant_material_count,
                    "auto_name_source": auto_name_source,
                    "related_vertex_groups": cluster_tracking_groups(cluster),
                    "warnings": warnings,
                }
            )
    candidates.sort(key=lambda item: (-float(item.get("confidence", 0.0) or 0.0), natural_key(item.get("proposed_name", ""))))
    used_names = {str(group.get("proposed_name") or "") for group in bodygroups}
    for index, candidate in enumerate(candidates, start=1):
        proposed_name = unique_name(capitalized_bodygroup_name(str(candidate.get("base_name") or "Accessory")), used_names, f"Accessory_{index:03d}")
        candidate["proposed_name"] = proposed_name
        candidate["uid"] = f"bg_split_auto_{index:03d}_{proposed_name.lower()[:28]}"
    return candidates


def display_split_candidates(candidates: list[dict[str, object]], bodygroups: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    primary: list[dict[str, object]] = []
    review: list[dict[str, object]] = []
    for candidate in candidates:
        confidence = float(candidate.get("confidence", 0.0) or 0.0)
        source_name = str(candidate.get("source_bodygroup_name") or "").lower()
        if confidence < ADVANCED_DISPLAY_MIN_CONFIDENCE:
            continue
        if "clothes" in source_name or "face" in source_name:
            primary.append(candidate)
        elif confidence >= 0.86:
            review.append(candidate)
    selected = primary[:40] + review[:20]
    # Seed with the existing bodygroup names so the re-run rename pass cannot
    # undo the earlier de-duplication and collide with an existing bodygroup.
    used_names: set[str] = {str(group.get("proposed_name") or "") for group in (bodygroups or []) if isinstance(group, dict)}
    used_names.discard("")
    for index, candidate in enumerate(selected, start=1):
        proposed_name = unique_name(capitalized_bodygroup_name(str(candidate.get("base_name") or candidate.get("proposed_name") or "Accessory")), used_names, f"Accessory_{index:03d}")
        candidate["proposed_name"] = proposed_name
        candidate["uid"] = f"bg_split_auto_{index:03d}_{proposed_name.lower()[:28]}"
    return selected


def detect_split_candidates(
    sources: list[dict[str, object]],
    bodygroups: list[dict[str, object]],
    metrics: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    status = sklearn_status()
    if status.get("available"):
        try:
            advanced = detect_advanced_split_candidates(sources, bodygroups, metrics)
            displayed = display_split_candidates(advanced, bodygroups)
            status["raw_candidate_count"] = len(advanced)
            status["candidate_count"] = len(displayed)
            if displayed:
                return displayed, status
            status["warning"] = "Advanced clustering found no split candidates; fallback heuristic candidates are shown unchecked."
        except Exception as exc:
            status = {
                "available": False,
                "method": "fallback",
                "warning": f"Advanced clustering failed; fallback heuristic candidates are shown unchecked: {exc}",
            }
    fallback = detect_heuristic_split_candidates(sources, bodygroups, metrics)
    for candidate in fallback:
        candidate["enabled"] = False
        candidate["confidence"] = min(float(candidate.get("confidence", 0.0) or 0.0), 0.74)
        warnings = list(candidate.get("warnings", []))
        warning = "Fallback heuristic split candidate; review before enabling."
        if warning not in warnings:
            warnings.append(warning)
        candidate["warnings"] = warnings
    status["candidate_count"] = len(fallback)
    status["used_fallback"] = True
    return fallback, status


def append_split_candidates_to_plan(bodygroups: list[dict[str, object]], candidates: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    output_groups = [dict(group) for group in bodygroups]
    plan_splits: list[dict[str, object]] = []
    for candidate in candidates:
        confidence = float(candidate.get("confidence", 0.0) or 0.0)
        if confidence < AUTO_SPLIT_MEDIUM_CONFIDENCE:
            continue
        new_uid = str(candidate.get("uid") or "")
        source_uid_value = str(candidate.get("source_uid") or "")
        output_groups.append(
            {
                "uid": new_uid,
                "enabled": bool(candidate.get("enabled", False)),
                "proposed_name": candidate.get("proposed_name", new_uid),
                "source_uids": [new_uid],
                "preview_source_uids": [new_uid],
                "source_objects": list(candidate.get("source_objects", [])),
                "material_names": list(candidate.get("material_names", [])),
                "material_vertex_counts": dict(candidate.get("material_vertex_counts", {})) if isinstance(candidate.get("material_vertex_counts"), dict) else {},
                "dominant_material_name": candidate.get("dominant_material_name", ""),
                "dominant_material_vertex_count": int(candidate.get("dominant_material_vertex_count", 0) or 0),
                "auto_name_source": candidate.get("auto_name_source", "category"),
                "related_vertex_groups": list(candidate.get("related_vertex_groups", [])),
                "category": "accessory_split",
                "confidence": confidence,
                "source_bodygroup": candidate.get("source_bodygroup_name", ""),
                "source_bodygroup_uid": candidate.get("source_bodygroup_uid", ""),
                "split_candidate": True,
                "split_type": candidate.get("type", "component_cluster"),
                "split_method": candidate.get("method", ""),
                "vertex_count": int(candidate.get("vertex_count", 0) or 0),
                "face_count": int(candidate.get("face_count", 0) or 0),
                "preview_color": [1.0, 0.56, 0.05, 1.0],
                "base_color_path": "",
                "base_color_file": "",
                "warnings": list(candidate.get("warnings", [])),
            }
        )
        plan_splits.append(
            {
                "type": candidate.get("type", "component_cluster"),
                "method": candidate.get("method", ""),
                "source_bodygroup_uid": candidate.get("source_bodygroup_uid", ""),
                "source_uid": source_uid_value,
                "component_ids": list(candidate.get("component_ids", [])),
                "polygon_indices": list(candidate.get("polygon_indices", [])),
                "vertex_indices": list(candidate.get("vertex_indices", [])),
                "new_uid": new_uid,
                "proposed_name": candidate.get("proposed_name", new_uid),
                "confidence": confidence,
            }
        )
        parent_uid = str(candidate.get("source_bodygroup_uid") or "")
        for group in output_groups:
            if str(group.get("uid") or "") == parent_uid:
                warnings = list(group.get("warnings", [])) if isinstance(group.get("warnings"), list) else []
                warning = "Accessory split candidates were detected under this bodygroup."
                if warning not in warnings:
                    warnings.append(warning)
                group["warnings"] = warnings
                break
    return output_groups, plan_splits


def analyze_scene(
    input_blend: Path,
    scale_factor: float,
    scale_preset: str = "factor",
    scale_reference_smd: Path | None = None,
    always_auto_split: bool = False,
    vertex_limit: int = DEFAULT_SOURCE_VERTEX_LIMIT,
    include_preview: bool = True,
    detect_splits: bool = True,
) -> tuple[dict[str, object], dict[str, object]]:
    vertex_limit = max(1, int(vertex_limit or DEFAULT_SOURCE_VERTEX_LIMIT))
    print("Scaling character", flush=True)
    scale_report = maybe_scale_character(scale_factor, scale_preset=scale_preset, scale_reference_smd=scale_reference_smd)
    print("Pruning unused shapekeys (pre-separation)", flush=True)
    pre_separation_shapekey_report = prune_shapekeys(stage="auto_pre_material_separation")
    print(f"Separating by material ({len(mesh_objects())} meshes)", flush=True)
    separate_report = separate_by_materials()
    print("Pruning unused shapekeys (post-separation)", flush=True)
    post_separation_shapekey_report = prune_shapekeys(stage="auto_post_material_separation")
    shapekey_report = combined_shapekey_prune_report(pre_separation_shapekey_report, post_separation_shapekey_report)
    print(f"Collecting bodygroup sources ({len(mesh_objects())} meshes)", flush=True)
    sources = collect_sources(vertex_limit)
    metrics = scene_metrics()
    if include_preview:
        print("Sampling preview triangles", flush=True)
        preview = collect_bodygroup_preview(sources)
    else:
        preview = {
            "triangles": [],
            "source_triangle_count": 0,
            "sampled_triangle_count": 0,
            "sample_stride": 1,
            "mins": [0.0, 0.0, 0.0],
            "maxs": [1.0, 1.0, 1.0],
            "skipped": True,
        }
    base_bodygroups = build_bodygroups(sources)
    facial_merge = build_facial_merge_report(sources, base_bodygroups, vertex_limit)
    split_policy = auto_split_policy(sources, always_auto_split, vertex_limit)
    if detect_splits and split_policy.get("required"):
        print("Clustering split candidates", flush=True)
        split_candidates, clustering_status = detect_split_candidates(auto_split_sources(sources, split_policy), base_bodygroups, metrics)
    elif split_policy.get("required"):
        split_candidates = []
        clustering_status = {
            "available": None,
            "method": "skipped",
            "skipped": True,
            "candidate_count": 0,
            "reason": "Apply mode reuses the analyze-time plan; split clustering was skipped.",
        }
    else:
        split_candidates = []
        clustering_status = {
            "available": None,
            "method": "skipped",
            "skipped": True,
            "candidate_count": 0,
            "reason": f"All material-split meshes are within the {vertex_limit:,} vertex limit; advanced auto-split is disabled.",
        }
    bodygroups, plan_splits = append_split_candidates_to_plan(base_bodygroups, split_candidates)
    public_sources = stripped_source_components(sources)
    warnings = list(scale_report.get("warnings", [])) + list(separate_report.get("warnings", []))
    for source in split_policy.get("over_limit_sources", []):
        if isinstance(source, dict):
            warnings.append(
                f"{source.get('name') or source.get('uid')} exceeds Source's {vertex_limit:,} vertex limit "
                f"({int(source.get('vertex_count', 0) or 0):,} vertices)."
            )
    clustering_warning = str(clustering_status.get("warning") or "")
    if clustering_warning:
        warnings.append(clustering_warning)
    if facial_merge.get("protected_over_limit_sources") or facial_merge.get("merged_over_vertex_limit"):
        if is_rtx_remix_vertex_limit(vertex_limit):
            warnings.append(
                "Merged Face exceeds or contains sources over the RTX Remix bodygroup limit; "
                "Step 6 will keep facial flexes merged and warn that the model is not RTX Remix compatible."
            )
        else:
            warnings.append(
                "Merged Face exceeds or contains sources over the Source vertex limit; Step 6 apply will fail validation instead of splitting facial flexes."
            )
    analysis = {
        "version": 3,
        "kind": "sort_bodygroups",
        "input_blend": str(input_blend),
        "scale": scale_report,
        "separation": separate_report,
        "shapekey_prune": shapekey_report,
        "auto_split": split_policy,
        "facial_merge": facial_merge,
        "vertex_limit": vertex_limit,
        "source_count": len(sources),
        "bodygroup_count": len(bodygroups),
        "sources": public_sources,
        "advanced_clustering": clustering_status,
        "split_candidates": split_candidates,
        "split_regions": split_candidates,
        "bodygroups": bodygroups,
        "materials": bodygroups,
        "model_preview": preview,
        "warnings": warnings,
    }
    plan = {
        "version": 3,
        "kind": "sort_bodygroups_plan",
        "input_blend": str(input_blend),
        "scale_factor": float(scale_factor),
        "scale_preset": str(scale_report.get("scale_preset") or scale_preset or "factor"),
        "scale_reference_smd": str(scale_report.get("reference_smd") or ""),
        "scale_actual_factor": float(scale_report.get("actual_scale", 1.0) or 1.0),
        "vertex_limit": vertex_limit,
        "auto_split": split_policy,
        "facial_merge": facial_merge,
        "bodygroups": bodygroups,
        "splits": plan_splits,
        "warnings": [],
    }
    name_repairs = repair_plan_bodygroup_names(plan)
    if name_repairs:
        repair_by_uid = {str(item.get("uid") or ""): str(item.get("new_name") or "") for item in name_repairs}
        for candidate in split_candidates:
            if isinstance(candidate, dict):
                uid = str(candidate.get("uid") or "")
                if uid in repair_by_uid and repair_by_uid[uid]:
                    candidate["proposed_name"] = repair_by_uid[uid]
        warning = f"Adjusted {len(name_repairs)} duplicate or unsafe bodygroup name(s)."
        warnings.append(warning)
        plan["warnings"] = [warning]
        plan["name_repairs"] = name_repairs
        analysis["warnings"] = warnings
        analysis["name_repairs"] = name_repairs
    return analysis, plan


def source_objects_by_uid(sources: list[dict[str, object]]) -> dict[str, list[bpy.types.Object]]:
    out: dict[str, list[bpy.types.Object]] = {}
    for entry in sources:
        objects = []
        for name in entry.get("object_names", []):
            obj = bpy.data.objects.get(str(name))
            if obj is not None and obj.type == "MESH":
                objects.append(obj)
        out[str(entry.get("uid") or "")] = objects
    return out


def live_mesh_object_name(obj: bpy.types.Object | None) -> str:
    try:
        if obj is None or obj.type != "MESH":
            return ""
        name = str(obj.name)
        if name not in bpy.data.objects:
            return ""
        return name
    except ReferenceError:
        return ""


def live_mesh_objects(objects: Iterable[bpy.types.Object]) -> list[bpy.types.Object]:
    return [obj for obj in objects if live_mesh_object_name(obj)]


def live_mesh_object_names(objects: Iterable[bpy.types.Object]) -> list[str]:
    return [name for obj in objects if (name := live_mesh_object_name(obj))]


def vertex_indices_for_group(obj: bpy.types.Object, group_name: str) -> set[int]:
    group = obj.vertex_groups.get(group_name)
    if group is None:
        return set()
    indices: set[int] = set()
    for vertex in obj.data.vertices:
        for membership in vertex.groups:
            if membership.group == group.index and membership.weight > 0.0001:
                indices.add(int(vertex.index))
                break
    return indices


def remove_empty_meshes() -> None:
    for obj in list(mesh_objects()):
        if len(obj.data.vertices) == 0 or len(obj.data.polygons) == 0:
            bpy.data.objects.remove(obj, do_unlink=True)


def remove_zero_vertex_bodygroups() -> list[str]:
    removed: list[str] = []
    for obj in list(mesh_objects()):
        if len(obj.data.vertices) == 0:
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    return sorted(removed, key=natural_key)


def rename_bodygroup_objects_by_material() -> list[dict[str, str]]:
    """QoL for the "Proceed Manually" flow: give every material-separated
    bodygroup object a recognizable name based on its dominant material before
    the user opens the manual-edit blend.

    Blender's material separation leaves the objects auto-suffixed (Body, Body.001,
    …) which is useless when hand-editing bodygroups. This mirrors the automatic
    naming (`material_based_bodygroup_base`): a safe, capitalized material name when
    the dominant material has one, otherwise the category fallback (Body/Face/Hair/
    …). Only ever called on the manual-edit blend, so the analysis/plan JSON and the
    automatic apply path (which re-separates the original Step-5 blend) are
    untouched. Names are resolved up-front, then applied in two passes (temp names
    first) so reusing a name another object currently holds cannot auto-suffix it.
    """
    ensure_object_mode()
    targets = sorted(
        [
            obj
            for obj in mesh_objects()
            if not is_helper_mesh_object(obj) and len(obj.data.vertices) > 0 and len(obj.data.polygons) > 0
        ],
        key=lambda item: natural_key(item.name),
    )
    used: set[str] = set()
    planned: list[tuple[bpy.types.Object, str, str]] = []
    for index, obj in enumerate(targets, start=1):
        dominant_name, _count = dominant_material_from_counts(material_vertex_counts(obj))
        base = material_bodygroup_name(dominant_name)
        if not base:
            materials = [mat.name for mat in used_materials(obj)]
            category, _confidence, _warnings = classify_source(obj, materials, tracking_vertex_groups(obj))
            base = capitalized_bodygroup_name(default_bodygroup_name(category, obj.name))
        final_name = unique_name(base, used, f"Bodygroup_{index:03d}")
        planned.append((obj, obj.name, final_name))
    for index, (obj, _old, _final) in enumerate(planned):
        temp_name = f"__mci_bg_rename_{index:03d}"
        obj.name = temp_name
        if obj.data is not None:
            obj.data.name = temp_name
    renames: list[dict[str, str]] = []
    for obj, old_name, final_name in planned:
        obj.name = final_name
        if obj.data is not None:
            obj.data.name = final_name
        if old_name != final_name:
            renames.append({"old": old_name, "new": final_name})
    return renames


def split_object_by_vertex_group(obj: bpy.types.Object, group_name: str, new_name: str) -> list[bpy.types.Object]:
    selected = vertex_indices_for_group(obj, group_name)
    if not selected:
        return []
    if len(selected) >= len(obj.data.vertices):
        obj.name = new_name
        obj.data.name = new_name
        return [obj]
    before = set(bpy.data.objects)
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    for vertex in obj.data.vertices:
        vertex.select = int(vertex.index) in selected
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_mode(type="VERT")
    bpy.ops.mesh.separate(type="SELECTED")
    bpy.ops.object.mode_set(mode="OBJECT")
    created = [candidate for candidate in bpy.data.objects if candidate not in before and candidate.type == "MESH"]
    for index, new_obj in enumerate(created, start=1):
        suffix = "" if index == 1 else f"_{index:02d}"
        new_obj.name = new_name + suffix
        new_obj.data.name = new_obj.name
    remove_empty_meshes()
    return created


def add_temp_component_vertex_group(obj: bpy.types.Object, component_ids: set[int], group_name: str) -> int:
    components = connected_mesh_components(obj, include_vertices=True)
    selected: set[int] = set()
    for component in components:
        if int(component.get("id", 0) or 0) in component_ids:
            selected.update(int(index) for index in component.get("vertex_indices", []))
    if not selected:
        return 0
    group = obj.vertex_groups.get(group_name) or obj.vertex_groups.new(name=group_name)
    group.add(sorted(selected), 1.0, "REPLACE")
    return len(selected)


def add_temp_region_vertex_group(obj: bpy.types.Object, polygon_indices: set[int], component_ids: set[int], vertex_indices: set[int], group_name: str) -> int:
    selected: set[int] = set()
    if polygon_indices:
        for poly in obj.data.polygons:
            if int(poly.index) in polygon_indices:
                selected.update(int(index) for index in poly.vertices)
    if not selected and vertex_indices:
        selected.update(index for index in vertex_indices if 0 <= index < len(obj.data.vertices))
    if not selected and component_ids:
        components = connected_mesh_components(obj, include_vertices=True)
        for component in components:
            if int(component.get("id", 0) or 0) in component_ids:
                selected.update(int(index) for index in component.get("vertex_indices", []))
    if not selected:
        return 0
    group = obj.vertex_groups.get(group_name) or obj.vertex_groups.new(name=group_name)
    group.add(sorted(selected), 1.0, "REPLACE")
    return len(selected)


def remove_temp_split_groups() -> None:
    for obj in mesh_objects():
        for group in list(obj.vertex_groups):
            if group.name.startswith("__mci_component_split_") or group.name.startswith("__mci_region_split_"):
                obj.vertex_groups.remove(group)


def enabled_plan_source_uids(plan: dict[str, object]) -> set[str]:
    enabled: set[str] = set()
    for group in plan.get("bodygroups", []):
        if not isinstance(group, dict) or not group.get("enabled", True):
            continue
        for source_uid in group.get("source_uids", []):
            enabled.add(str(source_uid))
    return enabled


def apply_component_splits(plan: dict[str, object], source_map: dict[str, list[bpy.types.Object]], enabled_source_uids: set[str]) -> tuple[dict[str, list[bpy.types.Object]], list[dict[str, object]]]:
    specs_by_source: dict[str, list[dict[str, object]]] = defaultdict(list)
    for split in plan.get("splits", []):
        if not isinstance(split, dict) or split.get("type") not in {"component_cluster", "region_cluster"}:
            continue
        new_uid = str(split.get("new_uid") or "")
        if new_uid not in enabled_source_uids:
            continue
        source_uid_value = str(split.get("source_uid") or "")
        if source_uid_value:
            specs_by_source[source_uid_value].append(split)

    created: dict[str, list[bpy.types.Object]] = {}
    reports: list[dict[str, object]] = []
    temp_groups: list[tuple[bpy.types.Object, str, dict[str, object]]] = []
    try:
        for source_uid_value, specs in specs_by_source.items():
            objects = live_mesh_objects(source_map.get(source_uid_value, []))
            for obj in objects:
                for index, spec in enumerate(specs, start=1):
                    component_ids = {int(value) for value in spec.get("component_ids", []) if str(value).isdigit()}
                    polygon_indices = {int(value) for value in spec.get("polygon_indices", []) if str(value).isdigit()}
                    vertex_indices = {int(value) for value in spec.get("vertex_indices", []) if str(value).isdigit()}
                    is_region = spec.get("type") == "region_cluster"
                    prefix = "__mci_region_split_" if is_region else "__mci_component_split_"
                    temp_name = f"{prefix}{index:02d}_{str(spec.get('new_uid') or '')[:36]}"
                    selected_count = add_temp_region_vertex_group(obj, polygon_indices, component_ids, vertex_indices, temp_name)
                    reports.append(
                        {
                            "type": spec.get("type", "component_cluster"),
                            "method": spec.get("method", ""),
                            "source_uid": source_uid_value,
                            "new_uid": spec.get("new_uid", ""),
                            "proposed_name": spec.get("proposed_name", ""),
                            "confidence": spec.get("confidence", 0.0),
                            "component_ids": sorted(component_ids),
                            "polygon_count": len(polygon_indices),
                            "selected_vertices": selected_count,
                            "created_objects": [],
                        }
                    )
                    if selected_count:
                        temp_groups.append((obj, temp_name, spec))
        for obj, temp_name, spec in temp_groups:
            if not live_mesh_object_name(obj):
                continue
            new_uid = str(spec.get("new_uid") or "")
            new_name = stripped_safe_name(str(spec.get("proposed_name") or new_uid)) or new_uid
            split_objects = split_object_by_vertex_group(obj, temp_name, new_name)
            if split_objects:
                split_objects = live_mesh_objects(split_objects)
                created.setdefault(new_uid, []).extend(split_objects)
                split_names = set(live_mesh_object_names(split_objects))
                source_uid_value = str(spec.get("source_uid") or "")
                source_map[source_uid_value] = [
                    item
                    for item in live_mesh_objects(source_map.get(source_uid_value, []))
                    if live_mesh_object_name(item) not in split_names
                ]
            for report in reports:
                if report.get("new_uid") == new_uid:
                    report["created_objects"] = live_mesh_object_names(split_objects)
                    break
    finally:
        remove_temp_split_groups()
    return created, reports


def apply_splits(plan: dict[str, object], groups: list[dict[str, object]], source_map: dict[str, list[bpy.types.Object]]) -> dict[str, object]:
    group_by_uid = {str(group.get("uid") or ""): group for group in plan.get("bodygroups", []) if isinstance(group, dict)}
    enabled_source_uids = enabled_plan_source_uids(plan)
    created, component_reports = apply_component_splits(plan, source_map, enabled_source_uids)
    reports: list[dict[str, object]] = []
    for split in plan.get("splits", []):
        if not isinstance(split, dict):
            continue
        if split.get("type") in {"component_cluster", "region_cluster"}:
            continue
        source_group_uid = str(split.get("source_bodygroup_uid") or "")
        vertex_group = str(split.get("vertex_group") or "")
        new_uid = str(split.get("new_uid") or "")
        if new_uid not in enabled_source_uids:
            continue
        name = stripped_safe_name(str(split.get("proposed_name") or new_uid)) or new_uid
        source_group = group_by_uid.get(source_group_uid, {})
        objects = [
            obj
            for source_uid in source_group.get("source_uids", [])
            for obj in live_mesh_objects(source_map.get(str(source_uid), []))
        ]
        split_objects: list[bpy.types.Object] = []
        for obj in list(objects):
            if not live_mesh_object_name(obj):
                continue
            split_objects.extend(split_object_by_vertex_group(obj, vertex_group, name))
        split_objects = live_mesh_objects(split_objects)
        if split_objects:
            created[new_uid] = split_objects
            split_object_ids = set(live_mesh_object_names(split_objects))
            for source_uid in source_group.get("source_uids", []):
                source_uid = str(source_uid)
                source_map[source_uid] = [
                    obj
                    for obj in live_mesh_objects(source_map.get(source_uid, []))
                    if live_mesh_object_name(obj) not in split_object_ids
                ]
        reports.append(
            {
                "source_bodygroup_uid": source_group_uid,
                "vertex_group": vertex_group,
                "new_uid": new_uid,
                "created_objects": live_mesh_object_names(split_objects),
            }
        )
    source_map.update({uid: live_mesh_objects(objects) for uid, objects in created.items()})
    return {"splits": component_reports + reports, "created_uid_count": len(created)}


def validate_plan(plan: dict[str, object]) -> list[str]:
    errors: list[str] = []
    names: set[str] = set()
    enabled_count = 0
    for group in plan.get("bodygroups", []):
        if not isinstance(group, dict) or not group.get("enabled", True):
            continue
        enabled_count += 1
        name = str(group.get("proposed_name") or "").strip()
        if not SAFE_NAME_RE.fullmatch(name):
            errors.append(f"{group.get('uid')}: unsafe bodygroup name {name!r}")
        if name in names:
            errors.append(f"{group.get('uid')}: duplicate bodygroup name {name!r}")
        names.add(name)
        if not group.get("source_uids"):
            errors.append(f"{group.get('uid')}: no source objects")
    if enabled_count <= 0:
        errors.append("No enabled bodygroups.")
    return errors


def repair_plan_bodygroup_names(plan: dict[str, object]) -> list[dict[str, object]]:
    """Make enabled bodygroup names safe and unique before validation/apply.

    Auto split candidates may be generated after the initial auto bodygroup
    names are made unique.  This final pass also protects manually edited or
    stale plans loaded from disk.
    """
    repairs: list[dict[str, object]] = []
    used: set[str] = set()
    uid_to_name: dict[str, str] = {}
    groups = plan.get("bodygroups", [])
    if not isinstance(groups, list):
        return repairs
    for index, group in enumerate(groups, start=1):
        if not isinstance(group, dict) or not group.get("enabled", True):
            continue
        original = str(group.get("proposed_name") or group.get("uid") or "").strip()
        fallback = f"Bodygroup_{index:03d}"
        candidate = stripped_safe_name(original) or fallback
        if not SAFE_NAME_RE.fullmatch(candidate):
            candidate = fallback
        root = candidate
        suffix_index = 2
        while candidate in used:
            candidate = f"{root}_{suffix_index:02d}"
            suffix_index += 1
        used.add(candidate)
        uid = str(group.get("uid") or "")
        if uid:
            uid_to_name[uid] = candidate
        if candidate != original:
            group["proposed_name"] = candidate
            warnings = list(group.get("warnings", [])) if isinstance(group.get("warnings"), list) else []
            warning = f"Bodygroup name was changed from {original!r} to {candidate!r} to avoid duplicate or unsafe names."
            if warning not in warnings:
                warnings.append(warning)
            group["warnings"] = warnings
            repairs.append(
                {
                    "uid": uid,
                    "old_name": original,
                    "new_name": candidate,
                    "reason": "duplicate_or_unsafe",
                }
            )
    if uid_to_name:
        for key in ("splits", "split_regions", "split_candidates"):
            entries = plan.get(key, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                uid = str(entry.get("new_uid") or entry.get("uid") or "")
                if uid in uid_to_name:
                    entry["proposed_name"] = uid_to_name[uid]
    if repairs:
        existing = plan.get("name_repairs", [])
        if not isinstance(existing, list):
            existing = []
        plan["name_repairs"] = existing + repairs
    return repairs


def join_objects(objects: list[bpy.types.Object], name: str) -> bpy.types.Object | None:
    objects = [obj for obj in objects if obj.name in bpy.data.objects and obj.type == "MESH"]
    if not objects:
        return None
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    if len(objects) > 1:
        bpy.ops.object.join()
    result = bpy.context.view_layer.objects.active
    result.name = name
    result.data.name = name
    return result


def assign_temporary_mesh_names() -> None:
    for index, obj in enumerate(mesh_objects(), start=1):
        temp_name = f"__mci_bodygroup_work_{index:03d}"
        obj.name = temp_name
        obj.data.name = temp_name


def polygon_axis_centroid(obj: bpy.types.Object, poly: bpy.types.MeshPolygon, axis: int) -> float:
    if not poly.vertices:
        return 0.0
    return sum(float(obj.data.vertices[int(index)].co[axis]) for index in poly.vertices) / max(1, len(poly.vertices))


def split_object_by_face_indices(obj: bpy.types.Object, face_indices: set[int], new_name: str) -> list[bpy.types.Object]:
    if not face_indices or len(face_indices) >= len(obj.data.polygons):
        obj.name = new_name
        obj.data.name = new_name
        return [obj]
    before = set(bpy.data.objects)
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    for poly in obj.data.polygons:
        poly.select = int(poly.index) in face_indices
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_mode(type="FACE")
    bpy.ops.mesh.separate(type="SELECTED")
    bpy.ops.object.mode_set(mode="OBJECT")
    created = [candidate for candidate in bpy.data.objects if candidate not in before and candidate.type == "MESH"]
    for index, new_obj in enumerate(created, start=1):
        suffix = "" if index == 1 else f"_{index:02d}"
        new_obj.name = new_name + suffix
        new_obj.data.name = new_obj.name
    remove_empty_meshes()
    return created


def split_oversized_bodygroup(obj: bpy.types.Object, base_name: str, vertex_limit: int) -> list[bpy.types.Object]:
    vertex_limit = max(1, int(vertex_limit or DEFAULT_SOURCE_VERTEX_LIMIT))
    pending = [obj]
    complete: list[bpy.types.Object] = []
    safety = 0
    while pending and safety < 64:
        safety += 1
        current = pending.pop(0)
        if current.name not in bpy.data.objects or current.type != "MESH":
            continue
        if len(current.data.vertices) <= vertex_limit:
            complete.append(current)
            continue
        if len(current.data.polygons) < 2:
            complete.append(current)
            continue
        mins, maxs = combined_bounds([current])
        extents = [maxs[index] - mins[index] for index in range(3)]
        axis = max(range(3), key=lambda item: extents[item])
        faces = sorted(current.data.polygons, key=lambda poly: polygon_axis_centroid(current, poly, axis))
        split_index = max(1, len(faces) // 2)
        chunk_faces = faces[split_index:]
        created_this_round = split_object_by_face_indices(current, {int(poly.index) for poly in chunk_faces}, f"{base_name}_split")
        current.name = base_name
        current.data.name = base_name
        pending.extend(created_this_round)
        if len(current.data.vertices) <= vertex_limit or not created_this_round:
            complete.append(current)
        else:
            pending.append(current)
    complete.extend(obj for obj in pending if obj.name in bpy.data.objects and obj.type == "MESH")
    complete = [obj for obj in complete if obj.name in bpy.data.objects and obj.type == "MESH"]
    complete.sort(key=lambda item: natural_key(item.name))
    for index, part in enumerate(complete, start=1):
        name = base_name if index == 1 else f"{base_name}_{index:02d}"
        part.name = name
        part.data.name = name
    return complete


def apply_plan(
    input_blend: Path,
    plan: dict[str, object],
    output_blend: Path,
    report_json: Path,
    scale_factor: float,
    scale_preset: str = "factor",
    scale_reference_smd: Path | None = None,
    always_auto_split: bool = False,
    vertex_limit: int = DEFAULT_SOURCE_VERTEX_LIMIT,
) -> None:
    started = time.monotonic()
    vertex_limit = max(1, int(vertex_limit or plan.get("vertex_limit") or DEFAULT_SOURCE_VERTEX_LIMIT))
    repair_plan_bodygroup_names(plan)
    name_repairs = list(plan.get("name_repairs", [])) if isinstance(plan.get("name_repairs"), list) else []
    errors = validate_plan(plan)
    if errors:
        # Write a minimal failure report before raising so the GUI can surface
        # the structured validation errors instead of a generic missing-output
        # message.
        write_json(
            report_json,
            {
                "version": 3,
                "kind": "sort_bodygroups_report",
                "input_blend": str(input_blend),
                "output_blend": str(output_blend),
                "name_repairs": name_repairs,
                "vertex_limit": vertex_limit,
                "bodygroups": [],
                "bodygroup_count": 0,
                "warnings": [],
                "validation": {"ok": False, "errors": errors, "warnings": []},
                "elapsed_seconds": round(time.monotonic() - started, 3),
            },
        )
        raise RuntimeError("Bodygroup plan validation failed:\n" + "\n".join(errors))
    plan_auto_split = plan.get("auto_split", {}) if isinstance(plan.get("auto_split", {}), dict) else {}
    effective_always_auto_split = bool(always_auto_split or plan_auto_split.get("always"))
    plan_scale_preset = str(plan.get("scale_preset") or scale_preset or "factor")
    plan_reference = Path(str(plan.get("scale_reference_smd"))) if plan.get("scale_reference_smd") else scale_reference_smd
    # Apply mode only needs the scale/separation/prune side effects and the
    # source list; the preview sampling and split clustering results would be
    # discarded, so skip them.
    analysis, _default_plan = analyze_scene(
        input_blend,
        scale_factor,
        scale_preset=plan_scale_preset,
        scale_reference_smd=plan_reference,
        always_auto_split=effective_always_auto_split,
        vertex_limit=vertex_limit,
        include_preview=False,
        detect_splits=False,
    )
    sources = [entry for entry in analysis.get("sources", []) if isinstance(entry, dict)]
    source_map = source_objects_by_uid(sources)
    print("Applying bodygroup splits", flush=True)
    split_report = apply_splits(plan, sources, source_map)
    assign_temporary_mesh_names()
    used_objects: set[bpy.types.Object] = set()
    reserved_plan_names = {
        str(group.get("proposed_name") or group.get("uid") or "")
        for group in plan.get("bodygroups", [])
        if isinstance(group, dict) and group.get("enabled", True)
    }
    output_names: set[str] = set()
    output_groups: list[dict[str, object]] = []
    print("Joining bodygroup meshes", flush=True)
    for group in plan.get("bodygroups", []):
        if not isinstance(group, dict) or not group.get("enabled", True):
            continue
        name = str(group.get("proposed_name") or group.get("uid") or "bodygroup")
        objects: list[bpy.types.Object] = []
        for source_uid in group.get("source_uids", []):
            objects.extend(source_map.get(str(source_uid), []))
        objects = [obj for obj in live_mesh_objects(objects) if obj not in used_objects]
        joined = join_objects(objects, name)
        if joined is None:
            continue
        protected_face_merge = bool(group.get("facial_merge")) or str(group.get("merge_role") or "") == "face_flex"
        split_parts = [joined] if protected_face_merge else split_oversized_bodygroup(joined, name, vertex_limit)
        for part_index, part in enumerate(split_parts, start=1):
            desired_name = name if part_index == 1 else f"{name}_{part_index:02d}"
            unavailable = output_names | (reserved_plan_names - {name})
            final_name = desired_name
            suffix_index = 2
            while final_name in unavailable:
                final_name = f"{desired_name}_{suffix_index:02d}"
                suffix_index += 1
            part.name = final_name
            part.data.name = final_name
            output_names.add(final_name)
            used_objects.add(part)
            output_groups.append(
                {
                    "uid": group.get("uid") if part_index == 1 else f"{group.get('uid')}_{part_index:02d}",
                    "name": part.name,
                    "source_uids": group.get("source_uids", []),
                    "vertex_count": len(part.data.vertices),
                    "face_count": len(part.data.polygons),
                    "materials": [mat.name for mat in part.data.materials if mat is not None],
                    "material_vertex_counts": dict(group.get("material_vertex_counts", {})) if isinstance(group.get("material_vertex_counts"), dict) else {},
                    "dominant_material_name": group.get("dominant_material_name", ""),
                    "dominant_material_vertex_count": int(group.get("dominant_material_vertex_count", 0) or 0),
                    "auto_name_source": group.get("auto_name_source", ""),
                    "vertex_group_count": len(part.vertex_groups),
                    "armature_modifiers": [
                        modifier.name
                        for modifier in part.modifiers
                        if isinstance(modifier, bpy.types.ArmatureModifier)
                    ],
                    "auto_split_from": name if len(split_parts) > 1 else "",
                    "auto_split_part": part_index if len(split_parts) > 1 else 0,
                    "facial_merge": protected_face_merge and part_index == 1,
                    "merge_role": group.get("merge_role", "") if protected_face_merge and part_index == 1 else "",
                }
            )
    for obj in list(mesh_objects()):
        if obj not in used_objects:
            bpy.data.objects.remove(obj, do_unlink=True)
    removed_zero_vertex_bodygroups = remove_zero_vertex_bodygroups()
    if removed_zero_vertex_bodygroups:
        removed_names = set(removed_zero_vertex_bodygroups)
        output_groups = [group for group in output_groups if str(group.get("name") or "") not in removed_names]
    output_blend.parent.mkdir(parents=True, exist_ok=True)
    print(f"Saving bodygroup blend: {output_blend}", flush=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_blend))
    validation_errors: list[str] = []
    analysis_warnings = analysis.get("warnings", []) if isinstance(analysis.get("warnings"), list) else []
    warnings: list[str] = [str(item) for item in analysis_warnings if item]

    def add_warning(message: str) -> None:
        if message and message not in warnings:
            warnings.append(message)

    if not output_groups:
        validation_errors.append("No output bodygroups were created.")
    seen_group_names: set[str] = set()
    for group in output_groups:
        group_name = str(group.get("name") or "")
        if group_name in seen_group_names:
            validation_errors.append(f"Duplicate output bodygroup name {group_name!r}.")
        seen_group_names.add(group_name)
        if int(group.get("vertex_count", 0) or 0) > vertex_limit:
            if group.get("facial_merge"):
                if is_rtx_remix_vertex_limit(vertex_limit):
                    add_warning(
                        rtx_facial_over_limit_warning(
                            str(group.get("name") or "Face"),
                            int(group.get("vertex_count", 0) or 0),
                            vertex_limit,
                        )
                    )
                    continue
                validation_errors.append(
                    f"{group.get('name')}: merged Face has {int(group.get('vertex_count', 0) or 0):,} vertices, "
                    f"which exceeds Source's {vertex_limit:,} vertex limit. It was not split because that would duplicate facial flex controllers."
                )
                continue
            validation_errors.append(
                f"{group.get('name')}: {int(group.get('vertex_count', 0) or 0):,} vertices exceeds Source's {vertex_limit:,} vertex limit."
            )
    report = {
        "version": 3,
        "kind": "sort_bodygroups_report",
        "input_blend": str(input_blend),
        "output_blend": str(output_blend),
        "scale": analysis.get("scale", {}),
        "separation": analysis.get("separation", {}),
        "shapekey_prune": analysis.get("shapekey_prune", {}),
        "name_repairs": name_repairs,
        "auto_split": analysis.get("auto_split", {}),
        "facial_merge": plan.get("facial_merge", analysis.get("facial_merge", {})),
        "vertex_limit": vertex_limit,
        "advanced_clustering": analysis.get("advanced_clustering", {}),
        "split_report": split_report,
        "removed_zero_vertex_bodygroups": removed_zero_vertex_bodygroups,
        "bodygroups": output_groups,
        "bodygroup_count": len(output_groups),
        "warnings": warnings,
        "validation": {"ok": not validation_errors, "errors": validation_errors, "warnings": warnings},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    write_json(report_json, report)


def validate_manual_bodygroups(
    input_blend: Path,
    output_blend: Path,
    report_json: Path,
    vertex_limit: int = DEFAULT_SOURCE_VERTEX_LIMIT,
) -> bool:
    started = time.monotonic()
    vertex_limit = max(1, int(vertex_limit or DEFAULT_SOURCE_VERTEX_LIMIT))
    ensure_object_mode()
    armature = active_armature()
    errors: list[str] = []
    warnings: list[str] = []
    if armature is None:
        errors.append("Missing armature.")
    helper_meshes = [obj for obj in mesh_objects() if is_helper_mesh_object(obj)]
    ignored_helper_meshes = [obj.name for obj in helper_meshes]
    for obj in helper_meshes:
        warnings.append(f"{obj.name}: ignored Source/VTA helper mesh; not treated as a bodygroup.")
        bpy.data.objects.remove(obj, do_unlink=True)
    removed_zero_vertex_bodygroups = remove_zero_vertex_bodygroups()
    shapekey_report = prune_shapekeys(stage="manual_bodygroup_validation")
    meshes = sorted([obj for obj in mesh_objects() if not is_helper_mesh_object(obj)], key=lambda obj: natural_key(obj.name))
    if not meshes:
        errors.append("No mesh bodygroups.")
    canonical_names = {"Face", "Body"}
    mesh_names = {obj.name for obj in meshes}
    missing_canonical_names = sorted(canonical_names - mesh_names)
    for name in missing_canonical_names:
        warnings.append(
            f"Optional canonical bodygroup mesh is missing: {name}. "
            "This is allowed; later steps will use the available bodygroup mesh names."
        )

    seen_names: set[str] = set()
    output_groups: list[dict[str, object]] = []
    facial_bodygroups: list[dict[str, object]] = []
    for obj in meshes:
        name = obj.name.strip()
        object_errors: list[str] = []
        object_warnings: list[str] = []
        object_shapekeys = shape_key_names(obj)
        object_facial_shapekeys = [name for name in object_shapekeys if is_facial_shapekey_name(name)]
        if not SAFE_NAME_RE.fullmatch(name):
            object_errors.append(f"{name}: unsafe bodygroup object name.")
        if name in seen_names:
            object_errors.append(f"{name}: duplicate bodygroup object name.")
        seen_names.add(name)
        if name and name != capitalized_bodygroup_name(name):
            object_warnings.append(f"{name}: bodygroup name is safe but not capitalized.")
        if name.startswith("__mci_") or name.lower().startswith("mci_"):
            object_warnings.append(f"{name}: manual edit blend still contains a temporary/helper-looking mesh.")
        vertex_count = len(obj.data.vertices)
        face_count = len(obj.data.polygons)
        if vertex_count <= 0 or face_count <= 0:
            object_errors.append(f"{name}: bodygroup has no vertices or faces.")
        if vertex_count > vertex_limit:
            if is_rtx_remix_vertex_limit(vertex_limit) and (name == "Face" or object_facial_shapekeys):
                object_warnings.append(rtx_facial_over_limit_warning(name or "Face", vertex_count, vertex_limit))
            else:
                object_errors.append(f"{name}: {vertex_count:,} vertices exceeds Source's {vertex_limit:,} vertex limit.")
        if not [mat for mat in obj.data.materials if mat is not None]:
            object_warnings.append(f"{name}: mesh has no material slots.")
        if not any(group.name.startswith(TRACKING_PREFIXES) for group in obj.vertex_groups):
            object_warnings.append(f"{name}: mesh has no MCI tracking vertex groups.")
        if object_facial_shapekeys:
            facial_bodygroups.append({"name": name, "facial_shapekey_names": object_facial_shapekeys})
            if name != "Face":
                object_warnings.append(
                    f"{name}: contains facial shapekeys and should usually be merged into Face to avoid duplicated flex controllers."
                )
        if armature is not None:
            has_armature_modifier = any(
                isinstance(modifier, bpy.types.ArmatureModifier) and modifier.object == armature
                for modifier in obj.modifiers
            )
            if not has_armature_modifier:
                object_errors.append(f"{name}: bodygroup lacks an Armature modifier targeting the main armature.")
        errors.extend(object_errors)
        warnings.extend(object_warnings)
        output_groups.append(
            {
                "name": name,
                "vertex_count": vertex_count,
                "face_count": face_count,
                "materials": [mat.name for mat in obj.data.materials if mat is not None],
                "shapekey_names": object_shapekeys,
                "facial_shapekey_names": object_facial_shapekeys,
                "vertex_group_count": len(obj.vertex_groups),
                "tracking_vertex_groups": [group.name for group in obj.vertex_groups if group.name.startswith(TRACKING_PREFIXES)],
                "armature_modifiers": [
                    modifier.name
                    for modifier in obj.modifiers
                    if isinstance(modifier, bpy.types.ArmatureModifier)
                ],
                "errors": object_errors,
                "warnings": object_warnings,
            }
        )
    if len(facial_bodygroups) > 1 or any(str(item.get("name") or "") != "Face" for item in facial_bodygroups):
        names = ", ".join(str(item.get("name") or "") for item in facial_bodygroups[:12])
        warnings.append(
            f"Multiple/manual bodygroups contain facial shapekeys ({names}); merge them into Face to avoid duplicated flex controllers."
        )

    report = {
        "version": 3,
        "kind": "sort_bodygroups_manual_report",
        "manual": True,
        "input_blend": str(input_blend),
        "output_blend": str(output_blend),
        "shapekey_prune": shapekey_report,
        "ignored_helper_meshes": ignored_helper_meshes,
        "removed_zero_vertex_bodygroups": removed_zero_vertex_bodygroups,
        "missing_canonical_bodygroups": missing_canonical_names,
        "bodygroups": output_groups,
        "bodygroup_count": len(output_groups),
        "facial_bodygroups": facial_bodygroups,
        "vertex_limit": vertex_limit,
        "validation": {"ok": not errors, "errors": errors, "warnings": warnings},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    write_json(report_json, report)
    if errors:
        print("Manual bodygroup validation failed:")
        for error in errors:
            print(f"Error: {error}")
        return False
    output_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_blend))
    return True


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    vertex_limit = max(1, int(args.vertex_limit or DEFAULT_SOURCE_VERTEX_LIMIT))
    with suppress_blender_output(True):
        bpy.ops.wm.open_mainfile(filepath=str(args.input_blend))
    if args.mode == "analyze":
        if not args.analysis_json or not args.plan_json:
            raise RuntimeError("--analysis-json and --plan-json are required in analyze mode")
        print("Analyzing bodygroups")
        analysis, plan = analyze_scene(
            args.input_blend,
            args.scale_factor,
            scale_preset=args.scale_preset,
            scale_reference_smd=args.scale_reference_smd,
            always_auto_split=args.always_auto_split,
            vertex_limit=vertex_limit,
        )
        analysis["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if args.manual_edit_blend:
            removed_zero_vertex_bodygroups = remove_zero_vertex_bodygroups()
            manual_edit_renames = rename_bodygroup_objects_by_material()
            args.manual_edit_blend.parent.mkdir(parents=True, exist_ok=True)
            analysis["removed_zero_vertex_bodygroups"] = removed_zero_vertex_bodygroups
            analysis["manual_edit_blend"] = str(args.manual_edit_blend)
            analysis["manual_edit_renames"] = manual_edit_renames
            plan["removed_zero_vertex_bodygroups"] = removed_zero_vertex_bodygroups
            plan["manual_edit_blend"] = str(args.manual_edit_blend)
            plan["manual_edit_renames"] = manual_edit_renames
            bpy.ops.wm.save_as_mainfile(filepath=str(args.manual_edit_blend))
            print(f"Wrote manual bodygroup edit blend: {args.manual_edit_blend} ({len(manual_edit_renames)} object(s) renamed by material)")
        write_json(args.analysis_json, analysis)
        write_json(args.plan_json, plan)
        print(f"Wrote bodygroup analysis: {args.analysis_json}")
        print(f"Wrote bodygroup plan: {args.plan_json}")
    elif args.mode == "apply":
        if not args.output_blend or not args.report_json or not args.plan_json:
            raise RuntimeError("--plan-json, --output-blend, and --report-json are required in apply mode")
        plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
        print("Applying bodygroup plan")
        apply_plan(
            args.input_blend,
            plan,
            args.output_blend,
            args.report_json,
            args.scale_factor,
            scale_preset=args.scale_preset,
            scale_reference_smd=args.scale_reference_smd,
            always_auto_split=args.always_auto_split,
            vertex_limit=vertex_limit,
        )
        print(f"Wrote bodygroup blend: {args.output_blend}")
        print(f"Wrote bodygroup report: {args.report_json}")
    else:
        if not args.output_blend or not args.report_json:
            raise RuntimeError("--output-blend and --report-json are required in validate-manual mode")
        print("Validating manually edited bodygroups")
        manual_ok = validate_manual_bodygroups(args.input_blend, args.output_blend, args.report_json, vertex_limit=vertex_limit)
        if manual_ok:
            print(f"Wrote bodygroup blend: {args.output_blend}")
        print(f"Wrote bodygroup report: {args.report_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
