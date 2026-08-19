#!/usr/bin/env python3
"""Blender-side step 3 Source spine repair helper."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import bpy
import numpy as np
from mathutils import Vector


PELVIS = "ValveBiped.Bip01_Pelvis"
SPINE = "ValveBiped.Bip01_Spine"
SPINE1 = "ValveBiped.Bip01_Spine1"
SPINE2 = "ValveBiped.Bip01_Spine2"
SPINE4 = "ValveBiped.Bip01_Spine4"
NECK = "ValveBiped.Bip01_Neck1"
R_CLAVICLE = "ValveBiped.Bip01_R_Clavicle"
L_CLAVICLE = "ValveBiped.Bip01_L_Clavicle"
R_UPPER_ARM = "ValveBiped.Bip01_R_UpperArm"
L_UPPER_ARM = "ValveBiped.Bip01_L_UpperArm"
R_THIGH = "ValveBiped.Bip01_R_Thigh"
L_THIGH = "ValveBiped.Bip01_L_Thigh"

CHAIN_TARGETS = [PELVIS, SPINE, SPINE1, SPINE2, SPINE4]
ATTACHMENT_TARGETS = [NECK, R_CLAVICLE, L_CLAVICLE]
TARGETS = CHAIN_TARGETS + ATTACHMENT_TARGETS
TARGET_PARENTS = {
    PELVIS: None,
    SPINE: PELVIS,
    SPINE1: SPINE,
    SPINE2: SPINE1,
    SPINE4: SPINE2,
    NECK: SPINE4,
    R_CLAVICLE: SPINE4,
    L_CLAVICLE: SPINE4,
}
ADDABLE_TARGETS = {SPINE, SPINE2}
SIDE_LANDMARKS = [R_CLAVICLE, L_CLAVICLE, R_UPPER_ARM, L_UPPER_ARM, R_THIGH, L_THIGH]
SAFE_BONE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
SOURCE_BONE_NAME_PATTERN = re.compile(r"^ValveBiped\.Bip01_[A-Za-z0-9_]+$")
TORSO_REJECT_TOKENS = (
    "pai",
    "breast",
    "boob",
    "chestaccessory",
    "mune",
    "乳",
    "胸",
)


def parse_args() -> argparse.Namespace:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("analyze", "apply"), required=True)
    parser.add_argument("--input-blend", type=Path, required=True)
    parser.add_argument("--analysis-json", type=Path)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--output-blend", type=Path)
    parser.add_argument("--report-json", type=Path)
    return parser.parse_args(argv)


def ensure_object_mode() -> None:
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")


def armatures() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]


def mesh_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.type == "MESH"]


def active_armature() -> bpy.types.Object:
    candidates = armatures()
    if not candidates:
        raise RuntimeError("No armature found in the fixed blend file.")
    candidates.sort(key=lambda obj: len(obj.data.bones), reverse=True)
    armature = candidates[0]
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    return armature


def v3(value: Iterable[float]) -> list[float]:
    vector = list(value)
    return [round(float(vector[0]), 6), round(float(vector[1]), 6), round(float(vector[2]), 6)]


def vector_from(value: object) -> Vector:
    if isinstance(value, Vector):
        return value.copy()
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return Vector((float(value[0]), float(value[1]), float(value[2])))
    raise ValueError(f"Expected a 3D vector, got {value!r}")


def bone_lookup(armature: bpy.types.Object) -> dict[str, bpy.types.Bone]:
    return {bone.name: bone for bone in armature.data.bones}


def parent_map(armature: bpy.types.Object) -> dict[str, str | None]:
    return {bone.name: bone.parent.name if bone.parent else None for bone in armature.data.bones}


def weighted_bone_names() -> set[str]:
    weighted: set[str] = set()
    for obj in mesh_objects():
        index_to_name = {group.index: group.name for group in obj.vertex_groups}
        for vertex in obj.data.vertices:
            for group in vertex.groups:
                if group.weight > 0.000001:
                    name = index_to_name.get(group.group)
                    if name:
                        weighted.add(name)
    return weighted


def is_safe_bone_name(name: str) -> bool:
    return bool(SAFE_BONE_NAME_PATTERN.fullmatch(name) or SOURCE_BONE_NAME_PATTERN.fullmatch(name))


def collect_bones(armature: bpy.types.Object, weighted: set[str]) -> list[dict[str, object]]:
    bones: list[dict[str, object]] = []
    for index, bone in enumerate(armature.data.bones):
        bones.append(
            {
                "index": index,
                "name": bone.name,
                "parent": bone.parent.name if bone.parent else None,
                "children": [child.name for child in bone.children],
                "head": v3(bone.head_local),
                "tail": v3(bone.tail_local),
                "use_connect": bool(bone.use_connect),
                "use_deform": bool(bone.use_deform),
                "has_weights": bone.name in weighted,
            }
        )
    return bones


def collect_model_preview(armature: bpy.types.Object, max_vertices: int = 500000, max_edges: int = 500000) -> dict[str, object]:
    vertices: list[list[float]] = []
    edges: list[tuple[int, int]] = []
    to_armature = armature.matrix_world.inverted()
    for obj in mesh_objects():
        offset = len(vertices)
        transform = to_armature @ obj.matrix_world
        vertex_count = len(obj.data.vertices)
        if vertex_count:
            coords = np.empty(vertex_count * 3, dtype=np.float64)
            obj.data.vertices.foreach_get("co", coords)
            coords = coords.reshape(-1, 3)
            matrix = np.array([[float(transform[row][col]) for col in range(4)] for row in range(4)], dtype=np.float64)
            transformed = coords @ matrix[:3, :3].T + matrix[:3, 3]
            vertices.extend(np.round(transformed, 6).tolist())
        edge_count = len(obj.data.edges)
        if edge_count:
            edge_indices = np.empty(edge_count * 2, dtype=np.int64)
            obj.data.edges.foreach_get("vertices", edge_indices)
            edge_indices = edge_indices.reshape(-1, 2) + offset
            edges.extend((int(a), int(b)) for a, b in edge_indices.tolist())

    if not vertices:
        return {"vertices": [], "edges": [], "source_vertex_count": 0, "source_edge_count": 0, "sample_stride": 1}

    sampled_vertices: list[list[float]] = []
    index_map: dict[int, int] = {}

    def add_vertex(index: int) -> int | None:
        if index in index_map:
            return index_map[index]
        if len(sampled_vertices) >= max_vertices:
            return None
        index_map[index] = len(sampled_vertices)
        sampled_vertices.append(vertices[index])
        return index_map[index]

    edge_stride = max(1, math.ceil(len(edges) / max(1, max_edges)))
    sampled_edges: list[list[int]] = []
    for a, b in edges[::edge_stride]:
        mapped_a = add_vertex(a)
        mapped_b = add_vertex(b)
        if mapped_a is None or mapped_b is None or mapped_a == mapped_b:
            continue
        sampled_edges.append([mapped_a, mapped_b])
        if len(sampled_edges) >= max_edges:
            break

    point_stride = max(1, math.ceil(len(vertices) / max(1, max_vertices)))
    for index in range(0, len(vertices), point_stride):
        if len(sampled_vertices) >= max_vertices:
            break
        add_vertex(index)

    return {
        "vertices": sampled_vertices,
        "edges": sampled_edges,
        "source_vertex_count": len(vertices),
        "source_edge_count": len(edges),
        "sample_stride": point_stride,
        "edge_sample_stride": edge_stride,
    }


def ancestors(name: str | None, parents: dict[str, str | None], include_self: bool = True) -> list[str]:
    out: list[str] = []
    current = name if include_self else parents.get(name or "")
    seen: set[str] = set()
    while current and current not in seen:
        out.append(current)
        seen.add(current)
        current = parents.get(current)
    return out


def nearest_common_ancestor(names: list[str], parents: dict[str, str | None]) -> str | None:
    names = [name for name in names if name in parents]
    if not names:
        return None
    first = ancestors(names[0], parents, include_self=True)
    other_sets = [set(ancestors(name, parents, include_self=True)) for name in names[1:]]
    for candidate in first:
        if all(candidate in other for other in other_sets):
            return candidate
    return None


def normalized_name(name: str) -> str:
    return name.lower().replace(" ", "").replace("_", "").replace(".", "")


def xy_distance(a: Vector, b: Vector) -> float:
    return math.hypot(float(a.x - b.x), float(a.y - b.y))


def point_segment_distance_xy(point: Vector, start: Vector, end: Vector) -> float:
    ab_x = float(end.x - start.x)
    ab_y = float(end.y - start.y)
    ap_x = float(point.x - start.x)
    ap_y = float(point.y - start.y)
    denom = ab_x * ab_x + ab_y * ab_y
    if denom <= 0.00000001:
        return math.hypot(ap_x, ap_y)
    t = max(0.0, min(1.0, (ap_x * ab_x + ap_y * ab_y) / denom))
    closest_x = float(start.x) + ab_x * t
    closest_y = float(start.y) + ab_y * t
    return math.hypot(float(point.x) - closest_x, float(point.y) - closest_y)


def torso_line_limit(pelvis: Vector, chest: Vector, multiplier: float = 0.26) -> float:
    height = abs(float(chest.z - pelvis.z))
    return max(0.055, height * multiplier)


def near_torso_line(
    point: Vector,
    pelvis_source: str | None,
    chest_source: str | None,
    bones: dict[str, bpy.types.Bone],
    multiplier: float = 0.26,
) -> bool:
    if pelvis_source not in bones or chest_source not in bones:
        return True
    pelvis = bones[pelvis_source].head_local
    chest = bones[chest_source].head_local
    if not (pelvis.z - 0.03 <= point.z <= chest.z + 0.03):
        return False
    return point_segment_distance_xy(point, pelvis, chest) <= torso_line_limit(pelvis, chest, multiplier)


def exact_or_named_candidate(names: set[str], target: str, hints: tuple[str, ...]) -> str | None:
    if target in names:
        return target
    scored: list[tuple[float, str]] = []
    for name in names:
        low = normalized_name(name)
        score = 0.0
        for hint in hints:
            if normalized_name(hint) in low:
                score += 1.0
        if score:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return scored[0][1] if scored else None


def attachment_side(target: str) -> str | None:
    if "_R_" in target:
        return "R"
    if "_L_" in target:
        return "L"
    return None


def matches_attachment_side(name: str, side: str, bones: dict[str, bpy.types.Bone]) -> bool:
    bone = bones.get(name)
    if bone is None:
        return True
    x = float(bone.head_local.x)
    if abs(x) <= 0.0005:
        return True
    return x < 0.0 if side == "R" else x > 0.0


def score_spine1_candidate(
    name: str,
    bones: dict[str, bpy.types.Bone],
    parents: dict[str, str | None],
    pelvis_source: str | None,
    spine_source: str | None,
    chest_source: str | None,
    used: set[str],
) -> float:
    if name in used or name not in bones:
        return -9999.0
    bone = bones[name]
    low = normalized_name(name)
    if any(token in low for token in TORSO_REJECT_TOKENS):
        return -9999.0
    if not near_torso_line(bone.head_local, pelvis_source, chest_source, bones):
        return -9999.0
    score = 0.0
    if name == SPINE1:
        score += 45.0
    for token, value in (
        ("upperbody1", 55.0),
        ("upperbody", 42.0),
        ("上半身1", 55.0),
        ("上半身", 42.0),
        ("spine1", 36.0),
        ("spine", 18.0),
        ("body", 8.0),
    ):
        if normalized_name(token) in low:
            score += value
    if spine_source and parents.get(name) == spine_source:
        score += 35.0
    if spine_source and spine_source in ancestors(name, parents, include_self=False):
        score += 16.0
    if chest_source and chest_source in parents:
        chest_head = bones[chest_source].head_local
        lower = bones[spine_source].head_local if spine_source in bones else Vector((0, 0, -9999))
        if lower.z - 0.02 <= bone.head_local.z <= chest_head.z + 0.02:
            score += 22.0
        if bone.head_local.z > chest_head.z + 0.02:
            score -= 60.0
    if pelvis_source in bones and chest_source in bones:
        pelvis = bones[pelvis_source].head_local
        chest = bones[chest_source].head_local
        drift = point_segment_distance_xy(bone.head_local, pelvis, chest)
        score += max(0.0, 16.0 - drift * 180.0)
    else:
        score += max(0.0, 12.0 - abs(float(bone.head_local.x)) * 80.0)
    return score


def added_spine_position(target: str, selected: dict[str, str | None], bones: dict[str, bpy.types.Bone]) -> dict[str, object]:
    if target == SPINE2:
        lower_name = selected.get(SPINE1)
        upper_name = selected.get(SPINE4)
    else:
        lower_name = selected.get(PELVIS)
        upper_name = selected.get(SPINE1)
    lower = bones.get(lower_name or "")
    upper = bones.get(upper_name or "")
    if lower and upper:
        if target == SPINE:
            head = lower.head_local.lerp(upper.head_local, 0.45)
            tail = lower.head_local.lerp(upper.head_local, 0.82)
        else:
            head = lower.tail_local.copy()
            tail = upper.head_local.copy()
            if (tail - head).length < 0.004 or head.z > tail.z:
                head = lower.head_local.lerp(upper.head_local, 0.55)
                tail = lower.head_local.lerp(upper.head_local, 0.82)
        if (tail - head).length < 0.004:
            tail = head + Vector((0.0, 0.0, 0.035))
    elif upper:
        tail = upper.head_local.copy()
        head = tail - Vector((0.0, 0.0, 0.035))
    elif lower:
        head = lower.tail_local.copy()
        tail = head + Vector((0.0, 0.0, 0.035))
    else:
        head = Vector((0.0, 0.0, 0.0))
        tail = Vector((0.0, 0.0, 0.035))
    return {"head": v3(head), "tail": v3(tail)}


def make_entry(
    target: str,
    source: str | None,
    action: str,
    parent: str | None,
    confidence: float,
    warnings: list[str] | None = None,
    position: dict[str, object] | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "target": target,
        "source": source,
        "action": action,
        "parent": parent,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "warnings": warnings or [],
    }
    if position:
        entry["position"] = position
    return entry


def detect_collapsed_source_spine(
    bones: dict[str, bpy.types.Bone],
    parents: dict[str, str | None],
    selected: dict[str, str | None],
    chest_source: str | None,
) -> bool:
    pelvis_source = selected.get(PELVIS)
    if not pelvis_source or pelvis_source not in bones:
        return False
    if SPINE not in bones or SPINE1 not in bones:
        return False
    if SPINE2 in bones or SPINE4 in bones:
        return False
    if chest_source != SPINE1:
        return False
    if parents.get(SPINE) != pelvis_source or parents.get(SPINE1) != SPINE:
        return False
    pelvis_head = bones[pelvis_source].head_local
    lower_head = bones[SPINE].head_local
    chest_head = bones[SPINE1].head_local
    if not (pelvis_head.z <= lower_head.z <= chest_head.z):
        return False
    if not near_torso_line(lower_head, pelvis_source, chest_source, bones, multiplier=0.35):
        return False
    for candidate_name, candidate_bone in bones.items():
        if candidate_name in {PELVIS, SPINE, SPINE1, SPINE2, SPINE4}:
            continue
        low = normalized_name(candidate_name)
        if not any(token in low for token in ("upperbody", "上半身")):
            continue
        if any(token in low for token in TORSO_REJECT_TOKENS):
            continue
        if not (lower_head.z - 0.02 <= candidate_bone.head_local.z <= chest_head.z + 0.01):
            continue
        if not near_torso_line(candidate_bone.head_local, pelvis_source, chest_source, bones):
            continue
        if parents.get(candidate_name) == SPINE or SPINE in ancestors(candidate_name, parents, include_self=False):
            return False
    return True


def trace_source_spine_to_pelvis(
    start: str | None,
    pelvis_source: str | None,
    bones: dict[str, bpy.types.Bone],
    parents: dict[str, str | None],
) -> list[dict[str, object]]:
    trace: list[dict[str, object]] = []
    for name in ancestors(start, parents, include_self=True):
        bone = bones.get(name)
        if not bone:
            continue
        trace.append(
            {
                "name": name,
                "parent": parents.get(name),
                "head": v3(bone.head_local),
                "tail": v3(bone.tail_local),
                "head_z": round(float(bone.head_local.z), 6),
            }
        )
        if name == pelvis_source:
            break
    return trace


def detect_inverted_source_spine_merge(
    bones: dict[str, bpy.types.Bone],
    parents: dict[str, str | None],
    selected: dict[str, str | None],
    chest_source: str | None,
) -> dict[str, object] | None:
    pelvis_source = selected.get(PELVIS)
    if not pelvis_source or pelvis_source not in bones:
        return None
    if not all(name in bones for name in (SPINE, SPINE1, SPINE2)):
        return None
    if SPINE4 in bones:
        return None
    if parents.get(SPINE) != pelvis_source or parents.get(SPINE1) != SPINE or parents.get(SPINE2) != SPINE1:
        return None
    if chest_source != SPINE2 and parents.get(NECK) != SPINE2:
        return None
    spine1_head = bones[SPINE1].head_local
    spine2_head = bones[SPINE2].head_local
    if spine2_head.z >= spine1_head.z - 0.002:
        return None
    trace_start = NECK if NECK in bones else SPINE2
    trace = trace_source_spine_to_pelvis(trace_start, pelvis_source, bones, parents)
    trace_names = [str(entry.get("name") or "") for entry in trace]
    expected_order = [SPINE2, SPINE1, SPINE, pelvis_source]
    indexes = [trace_names.index(name) if name in trace_names else -1 for name in expected_order]
    if any(index < 0 for index in indexes) or indexes != sorted(indexes):
        return None
    return {
        "source": SPINE1,
        "target": SPINE2,
        "reparent_children_to": SPINE2,
        "trace": trace,
        "reason": (
            f"{SPINE2} is a child of {SPINE1} but its head is lower in Z; "
            f"merge {SPINE1} into {SPINE2} before canonical spine repair."
        ),
    }


def proposal_for_armature(armature: bpy.types.Object, weighted: set[str]) -> dict[str, object]:
    bones = bone_lookup(armature)
    names = set(bones)
    parents = parent_map(armature)
    selected: dict[str, str | None] = {}
    targets: dict[str, dict[str, object]] = {}
    proposal_warnings: list[str] = []
    pre_bone_merges: list[dict[str, object]] = []

    selected[PELVIS] = exact_or_named_candidate(names, PELVIS, ("pelvis", "hips", "lowerbody", "下半身"))
    targets[PELVIS] = make_entry(
        PELVIS,
        selected[PELVIS],
        "keep" if selected[PELVIS] == PELVIS else ("rename" if selected[PELVIS] else "missing"),
        None,
        1.0 if selected[PELVIS] == PELVIS else 0.45,
        [] if selected[PELVIS] else ["Pelvis must map to an existing bone."],
    )

    landmark_parents = [parents.get(name) for name in (NECK, R_CLAVICLE, L_CLAVICLE) if name in parents and parents.get(name)]
    chest_source = nearest_common_ancestor([name for name in landmark_parents if name], parents)
    if not chest_source and SPINE4 in names:
        chest_source = SPINE4
    if not chest_source and SPINE1 in names:
        chest_source = SPINE1

    inverted_merge = detect_inverted_source_spine_merge(bones, parents, selected, chest_source)
    if inverted_merge:
        pre_bone_merges.append(inverted_merge)
        selected[SPINE] = None
        selected[SPINE1] = SPINE
        selected[SPINE2] = None
        selected[SPINE4] = SPINE2
        targets[SPINE] = make_entry(
            SPINE,
            None,
            "add",
            PELVIS,
            0.9,
            ["Inverted Source spine merge detected; a new lower Spine will be added below the existing Spine source."],
            added_spine_position(SPINE, selected, bones),
        )
        targets[SPINE1] = make_entry(
            SPINE1,
            SPINE,
            "rename",
            SPINE,
            0.94,
            ["Existing ValveBiped.Bip01_Spine is the centered lower torso source and will become Spine1."],
        )
        targets[SPINE2] = make_entry(
            SPINE2,
            None,
            "add",
            SPINE1,
            0.86,
            ["Inverted Source spine merge detected; Spine2 will be added between Spine1 and Spine4."],
            added_spine_position(SPINE2, selected, bones),
        )
        targets[SPINE4] = make_entry(
            SPINE4,
            SPINE2,
            "rename",
            SPINE2,
            0.96,
            ["Existing ValveBiped.Bip01_Spine2 is the upper chest source and will become Spine4 after merging old Spine1 into it."],
        )
    elif detect_collapsed_source_spine(bones, parents, selected, chest_source):
        selected[SPINE] = None
        selected[SPINE1] = SPINE
        selected[SPINE2] = None
        selected[SPINE4] = SPINE1
        targets[SPINE] = make_entry(
            SPINE,
            None,
            "add",
            PELVIS,
            0.88,
            ["Collapsed Source spine detected; a new lower Spine will be added below the existing Spine source."],
            added_spine_position(SPINE, selected, bones),
        )
        targets[SPINE1] = make_entry(
            SPINE1,
            SPINE,
            "rename",
            SPINE,
            0.94,
            ["Existing ValveBiped.Bip01_Spine is the centered lower torso source and will become Spine1."],
        )
        targets[SPINE2] = make_entry(
            SPINE2,
            None,
            "add",
            SPINE1,
            0.86,
            ["Collapsed Source spine detected; Spine2 will be added between Spine1 and Spine4."],
            added_spine_position(SPINE2, selected, bones),
        )
        targets[SPINE4] = make_entry(
            SPINE4,
            SPINE1,
            "rename",
            SPINE2,
            0.96,
            ["Existing ValveBiped.Bip01_Spine1 is the upper chest source and will become Spine4."],
        )
    else:
        selected[SPINE] = SPINE if SPINE in names else None
        if selected[SPINE]:
            targets[SPINE] = make_entry(SPINE, selected[SPINE], "keep", PELVIS, 1.0)
        else:
            targets[SPINE] = make_entry(
                SPINE,
                None,
                "add",
                PELVIS,
                0.3,
                ["Spine is missing and will be added without weights."],
                added_spine_position(SPINE, selected, bones),
            )

        selected[SPINE4] = chest_source
        if chest_source:
            targets[SPINE4] = make_entry(
                SPINE4,
                chest_source,
                "keep" if chest_source == SPINE4 else "rename",
                SPINE2,
                1.0 if chest_source == SPINE4 else 0.86,
            )
        else:
            targets[SPINE4] = make_entry(SPINE4, None, "missing", SPINE2, 0.0, ["Spine4 must map to an existing upper chest bone."])

        used = {source for source in selected.values() if source}
        if (
            SPINE1 in names
            and SPINE1 not in used
            and near_torso_line(bones[SPINE1].head_local, selected.get(PELVIS), selected.get(SPINE4), bones)
        ):
            selected[SPINE1] = SPINE1
            targets[SPINE1] = make_entry(SPINE1, SPINE1, "keep", SPINE, 1.0)
        else:
            candidates = sorted(
                (
                    (
                        score_spine1_candidate(
                            name,
                            bones,
                            parents,
                            selected.get(PELVIS),
                            selected.get(SPINE),
                            selected.get(SPINE4),
                            used,
                        ),
                        name,
                    )
                    for name in names
                ),
                key=lambda item: (-item[0], item[1].lower()),
            )
            best_score, best_name = candidates[0] if candidates else (-9999.0, "")
            if best_score > 20.0:
                selected[SPINE1] = best_name
                targets[SPINE1] = make_entry(SPINE1, best_name, "keep" if best_name == SPINE1 else "rename", SPINE, min(0.95, best_score / 100.0))
                used.add(best_name)
            else:
                selected[SPINE1] = None
                targets[SPINE1] = make_entry(SPINE1, None, "missing", SPINE, 0.0, ["Spine1 must map to an existing centered upper-body bone."])

        if SPINE2 in names and SPINE2 not in used:
            selected[SPINE2] = SPINE2
            targets[SPINE2] = make_entry(SPINE2, SPINE2, "keep", SPINE1, 1.0)
        else:
            selected[SPINE2] = None
            position = added_spine_position(SPINE2, selected, bones)
            targets[SPINE2] = make_entry(SPINE2, None, "add", SPINE1, 0.8, ["Spine2 is missing and will be added without weights."], position)

    if SPINE2 not in targets:
        targets[SPINE2] = make_entry(
            SPINE2,
            None,
            "add",
            SPINE1,
            0.8,
            ["Spine2 is missing and will be added without weights."],
            added_spine_position(SPINE2, selected, bones),
        )

    used_attachment_sources = {source for source in selected.values() if source}
    for target in ATTACHMENT_TARGETS:
        side = attachment_side(target)
        available = {
            name
            for name in names
            if name == target
            or (name not in used_attachment_sources and (side is None or matches_attachment_side(name, side, bones)))
        }
        selected[target] = exact_or_named_candidate(available, target, (target.rsplit("_", 1)[-1], target))
        if selected[target]:
            used_attachment_sources.add(selected[target])
            targets[target] = make_entry(target, selected[target], "keep" if selected[target] == target else "rename", SPINE4, 1.0 if selected[target] == target else 0.5)
        else:
            targets[target] = make_entry(target, None, "missing", SPINE4, 0.0, [f"{target} must map to an existing bone."])

    duplicate_sources: dict[str, list[str]] = {}
    for target, entry in targets.items():
        source = entry.get("source")
        if source:
            duplicate_sources.setdefault(str(source), []).append(target)
    for source, mapped_targets in duplicate_sources.items():
        if len(mapped_targets) > 1:
            proposal_warnings.append(f"Source bone {source} is mapped to multiple targets: {', '.join(mapped_targets)}")

    return {
        "version": 1,
        "armature": armature.name,
        "targets": targets,
        "chain": CHAIN_TARGETS,
        "attachments": ATTACHMENT_TARGETS,
        "addable_targets": sorted(ADDABLE_TARGETS),
        "pre_bone_merges": pre_bone_merges,
        "warnings": proposal_warnings,
    }


def validation_for_armature(armature: bpy.types.Object, added_bones: set[str] | None = None) -> dict[str, object]:
    added_bones = added_bones or set()
    bones = bone_lookup(armature)
    names = [bone.name for bone in armature.data.bones]
    indexes = {name: index for index, name in enumerate(names)}
    errors: list[str] = []
    warnings: list[str] = []

    for target in TARGETS:
        if target not in bones:
            errors.append(f"Missing required bone: {target}")
    for target, parent in TARGET_PARENTS.items():
        if target not in bones or parent is None:
            continue
        actual_parent = bones[target].parent.name if bones[target].parent else None
        if actual_parent != parent:
            errors.append(f"{target} must be parented to {parent}; current parent is {actual_parent or '<none>'}.")
        if target in indexes and parent in indexes and indexes[parent] > indexes[target]:
            errors.append(f"{parent} appears after child {target} in armature.data.bones order.")

    if all(target in bones for target in CHAIN_TARGETS):
        z_values = [float(bones[target].head_local.z) for target in CHAIN_TARGETS]
        for lower, upper, low_z, high_z in zip(CHAIN_TARGETS, CHAIN_TARGETS[1:], z_values, z_values[1:]):
            if high_z < low_z - 0.002:
                errors.append(f"{upper} head is below {lower} head; expected {lower} <= {upper} in Z.")
        pelvis_head = bones[PELVIS].head_local
        chest_head = bones[SPINE4].head_local
        max_drift = torso_line_limit(pelvis_head, chest_head, multiplier=0.38)
        for target in CHAIN_TARGETS:
            drift = point_segment_distance_xy(bones[target].head_local, pelvis_head, chest_head)
            if drift > max_drift:
                errors.append(
                    f"{target} is too far from the pelvis-to-Spine4 torso line "
                    f"({drift:.4f} > {max_drift:.4f}); spine assignment is likely wrong."
                )

    def side_check(right: str, left: str, label: str, *, use_midpoint: bool = False) -> None:
        if right not in bones or left not in bones:
            warnings.append(f"Could not validate {label} left/right side because one landmark is missing.")
            return
        right_bone = bones[right]
        left_bone = bones[left]
        if use_midpoint:
            # Clavicles can share a chest-center head, so their segment
            # midpoints carry the side information without relying on direction.
            right_x = (float(right_bone.head_local.x) + float(right_bone.tail_local.x)) * 0.5
            left_x = (float(left_bone.head_local.x) + float(left_bone.tail_local.x)) * 0.5
        else:
            right_x = float(right_bone.head_local.x)
            left_x = float(left_bone.head_local.x)
        if not (right_x < left_x):
            errors.append(f"{label} side appears swapped: expected {right} on negative X and {left} on positive X.")

    side_check(R_CLAVICLE, L_CLAVICLE, "clavicle", use_midpoint=True)
    side_check(R_THIGH, L_THIGH, "leg")
    for target in added_bones:
        warnings.append(f"{target} was added as an unweighted deform bone.")
    unsafe_names = [name for name in names if not is_safe_bone_name(name)]
    if unsafe_names:
        errors.append("Unsafe bone names remain after cleanup: " + ", ".join(unsafe_names[:12]))

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def source_mapping_from_plan(plan: dict[str, object]) -> dict[str, str]:
    targets = plan.get("targets", {})
    if not isinstance(targets, dict):
        raise RuntimeError("Invalid spine plan: targets must be a dictionary.")
    mapping: dict[str, str] = {}
    for target, raw_entry in targets.items():
        if not isinstance(raw_entry, dict):
            continue
        action = str(raw_entry.get("action") or "")
        source = raw_entry.get("source")
        if action in {"keep", "rename"} and source and str(source) != str(target):
            source_name = str(source)
            if source_name in mapping:
                raise RuntimeError(
                    f"Invalid spine plan: source bone {source_name} is mapped to multiple targets "
                    f"({mapping[source_name]} and {target})."
                )
            mapping[source_name] = str(target)
    return mapping


def rename_or_merge_vertex_group(obj: bpy.types.Object, old_name: str, new_name: str) -> None:
    if old_name == new_name or old_name not in obj.vertex_groups:
        return
    old_group = obj.vertex_groups[old_name]
    if new_name not in obj.vertex_groups:
        old_group.name = new_name
        return
    new_group = obj.vertex_groups[new_name]
    for vertex in obj.data.vertices:
        for group in vertex.groups:
            if group.group == old_group.index and group.weight > 0.0:
                new_group.add([vertex.index], group.weight, "ADD")
                break
    obj.vertex_groups.remove(old_group)


def pre_bone_merges_from_plan(plan: dict[str, object]) -> list[dict[str, str]]:
    raw_merges = plan.get("pre_bone_merges", [])
    if not isinstance(raw_merges, list):
        return []
    merges: list[dict[str, str]] = []
    for raw in raw_merges:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "")
        target = str(raw.get("target") or "")
        if source and target and source != target:
            merges.append({"source": source, "target": target})
    return merges


def apply_pre_bone_merges(armature: bpy.types.Object, plan: dict[str, object]) -> list[dict[str, object]]:
    merges = pre_bone_merges_from_plan(plan)
    if not merges:
        return []
    results: list[dict[str, object]] = []
    ensure_object_mode()

    for merge in merges:
        source = merge["source"]
        target = merge["target"]
        existing = {bone.name for bone in armature.data.bones}
        if source not in existing:
            results.append({"source": source, "target": target, "skipped": True, "reason": "source bone missing"})
            continue
        if target not in existing:
            raise RuntimeError(f"Cannot merge {source} into {target}; target bone is missing.")

        for obj in mesh_objects():
            rename_or_merge_vertex_group(obj, source, target)

        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            edit_bones = armature.data.edit_bones
            if source not in edit_bones:
                results.append({"source": source, "target": target, "skipped": True, "reason": "source edit bone missing"})
                continue
            if target not in edit_bones:
                raise RuntimeError(f"Cannot merge {source} into {target}; target edit bone is missing.")
            source_bone = edit_bones[source]
            target_bone = edit_bones[target]
            original_parent = source_bone.parent.name if source_bone.parent else None
            reparented_children: list[str] = []
            for child in list(source_bone.children):
                if child.name == target:
                    continue
                child.parent = target_bone
                child.use_connect = False
                reparented_children.append(child.name)
            if target_bone.parent == source_bone:
                target_bone.parent = edit_bones[original_parent] if original_parent and original_parent in edit_bones else None
                target_bone.use_connect = False
            edit_bones.remove(source_bone)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")

        print(f"Merged inverted source spine bone {source} into {target}.")
        results.append(
            {
                "source": source,
                "target": target,
                "reparented_children": reparented_children,
                "target_parent": original_parent,
            }
        )
    return results


def unique_vertex_group_temp_name(obj: bpy.types.Object, index: int) -> str:
    base = f"__mci_spine_vg_tmp_{index:03d}__"
    if base not in obj.vertex_groups:
        return base
    suffix = 1
    while f"{base}{suffix:02d}" in obj.vertex_groups:
        suffix += 1
    return f"{base}{suffix:02d}"


def ordered_mapping_sources(mapping: dict[str, str]) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(source: str) -> None:
        if source in visited:
            return
        if source in visiting:
            ordered.append(source)
            visited.add(source)
            return
        visiting.add(source)
        target = mapping[source]
        if target in mapping:
            visit(target)
        visiting.remove(source)
        if source not in visited:
            ordered.append(source)
            visited.add(source)

    for source in mapping:
        visit(source)
    return ordered


def apply_renames(armature: bpy.types.Object, plan: dict[str, object]) -> list[str]:
    mapping = source_mapping_from_plan(plan)
    if not mapping:
        return []
    if len(set(mapping.values())) != len(mapping.values()):
        raise RuntimeError("Invalid spine plan: multiple source bones map to the same target.")
    existing = set(bone.name for bone in armature.data.bones)
    missing_sources = [source for source in mapping if source not in existing]
    if missing_sources:
        raise RuntimeError("Invalid spine plan: missing source bone(s): " + ", ".join(missing_sources))
    protected_targets = [target for source, target in mapping.items() if target in existing and target not in mapping and target != source]
    if protected_targets:
        raise RuntimeError("Invalid spine plan: target name(s) already exist and are not being renamed away: " + ", ".join(protected_targets))

    temp_by_source = {source: f"__mci_spine_tmp_{index:03d}__" for index, source in enumerate(mapping)}
    weighted_before = weighted_bone_names()
    source_had_weights = {source: source in weighted_before for source in mapping}
    vg_temp_by_object: dict[str, dict[str, str]] = {}
    print("Renaming mapped spine bones through temporary names.")
    ensure_object_mode()
    for obj in mesh_objects():
        per_object: dict[str, str] = {}
        for index, source in enumerate(mapping):
            if source not in obj.vertex_groups:
                continue
            temp = unique_vertex_group_temp_name(obj, index)
            obj.vertex_groups[source].name = temp
            per_object[source] = temp
        if per_object:
            vg_temp_by_object[obj.name] = per_object

    for source, temp in temp_by_source.items():
        armature.data.bones[source].name = temp
    for source, target in mapping.items():
        armature.data.bones[temp_by_source[source]].name = target

    for obj in mesh_objects():
        object_vg_temps = vg_temp_by_object.get(obj.name, {})
        for source in ordered_mapping_sources(mapping):
            temp = object_vg_temps.get(source)
            if temp:
                rename_or_merge_vertex_group(obj, temp, mapping[source])
    final_weighted = weighted_bone_names()
    lost_weight_targets = [
        f"{source} -> {target}"
        for source, target in mapping.items()
        if source_had_weights.get(source) and target not in final_weighted
    ]
    if lost_weight_targets:
        raise RuntimeError(
            "Spine rename lost weighted vertex groups for mapped bone(s): "
            + ", ".join(lost_weight_targets)
        )
    return [f"{source} -> {target}" for source, target in mapping.items()]


def plan_entry(plan: dict[str, object], target: str) -> dict[str, object]:
    targets = plan.get("targets", {})
    if not isinstance(targets, dict) or target not in targets or not isinstance(targets[target], dict):
        raise RuntimeError(f"Invalid spine plan: missing target entry {target}")
    return targets[target]  # type: ignore[return-value]


def add_missing_bones(armature: bpy.types.Object, plan: dict[str, object]) -> set[str]:
    added: set[str] = set()
    ensure_object_mode()
    current_bones = {bone.name: bone for bone in armature.data.bones}
    selected = {name: name for name in CHAIN_TARGETS if name in current_bones}
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for target in CHAIN_TARGETS:
            entry = plan_entry(plan, target)
            if str(entry.get("action")) != "add":
                continue
            if target not in ADDABLE_TARGETS:
                raise RuntimeError(f"{target} is not allowed to be added automatically.")
            if target in armature.data.edit_bones:
                continue
            position = entry.get("position") if isinstance(entry.get("position"), dict) else {}
            if not position:
                position = added_spine_position(target, selected, current_bones)
            head = vector_from(position.get("head")) if isinstance(position, dict) and "head" in position else Vector((0.0, 0.0, 0.0))
            tail = vector_from(position.get("tail")) if isinstance(position, dict) and "tail" in position else head + Vector((0.0, 0.0, 0.035))
            if (tail - head).length < 0.004:
                tail = head + Vector((0.0, 0.0, 0.035))
            bone = armature.data.edit_bones.new(target)
            bone.head = head
            bone.tail = tail
            bone.use_deform = True
            added.add(target)
            print(f"Added missing spine bone: {target}")
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    return added


def align_pelvis_if_far_from_spine1(armature: bpy.types.Object) -> dict[str, object]:
    ensure_object_mode()
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = armature.data.edit_bones
        if PELVIS not in edit_bones or SPINE1 not in edit_bones:
            return {"moved": False, "reason": "Pelvis or Spine1 missing."}
        pelvis = edit_bones[PELVIS]
        spine1 = edit_bones[SPINE1]
        before_head = pelvis.head.copy()
        before_tail = pelvis.tail.copy()
        distance = xy_distance(pelvis.head, spine1.head)
        threshold = max(0.06, abs(float(spine1.head.z - pelvis.head.z)) * 0.65)
        if distance <= threshold:
            return {"moved": False, "distance": round(distance, 6), "threshold": round(threshold, 6)}
        delta = Vector((float(spine1.head.x - pelvis.head.x), float(spine1.head.y - pelvis.head.y), 0.0))
        pelvis.head += delta
        pelvis.tail += delta
        if SPINE in edit_bones and pelvis.head.z > edit_bones[SPINE].head.z:
            z_delta = float(edit_bones[SPINE].head.z - pelvis.head.z - 0.002)
            pelvis.head.z += z_delta
            pelvis.tail.z += z_delta
        print(f"Moved {PELVIS} in XY to align with proposed {SPINE1}.")
        return {
            "moved": True,
            "distance": round(distance, 6),
            "threshold": round(threshold, 6),
            "before_head": v3(before_head),
            "before_tail": v3(before_tail),
            "after_head": v3(pelvis.head),
            "after_tail": v3(pelvis.tail),
        }
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def place_bone_between(edit_bone: bpy.types.EditBone, lower: Vector, upper: Vector, head_factor: float, tail_factor: float) -> None:
    head = lower.lerp(upper, head_factor)
    tail = lower.lerp(upper, tail_factor)
    if tail.z < head.z:
        tail.z = head.z + 0.004
    if (tail - head).length < 0.004:
        tail = head + Vector((0.0, 0.0, 0.035))
    edit_bone.head = head
    edit_bone.tail = tail


def normalize_added_spine_positions(armature: bpy.types.Object, added: set[str]) -> list[dict[str, object]]:
    if not added:
        return []
    changes: list[dict[str, object]] = []
    ensure_object_mode()
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = armature.data.edit_bones
        if SPINE in added and PELVIS in edit_bones and SPINE1 in edit_bones and SPINE in edit_bones:
            before = {"head": v3(edit_bones[SPINE].head), "tail": v3(edit_bones[SPINE].tail)}
            place_bone_between(edit_bones[SPINE], edit_bones[PELVIS].head, edit_bones[SPINE1].head, 0.45, 0.82)
            changes.append({"bone": SPINE, "before": before, "after": {"head": v3(edit_bones[SPINE].head), "tail": v3(edit_bones[SPINE].tail)}})
        if SPINE2 in added and SPINE1 in edit_bones and SPINE4 in edit_bones and SPINE2 in edit_bones:
            before = {"head": v3(edit_bones[SPINE2].head), "tail": v3(edit_bones[SPINE2].tail)}
            place_bone_between(edit_bones[SPINE2], edit_bones[SPINE1].head, edit_bones[SPINE4].head, 0.55, 0.82)
            changes.append({"bone": SPINE2, "before": before, "after": {"head": v3(edit_bones[SPINE2].head), "tail": v3(edit_bones[SPINE2].tail)}})
        if changes:
            print("Normalized added spine bone positions from the final canonical chain.")
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    return changes


def set_spine_parents(armature: bpy.types.Object) -> None:
    ensure_object_mode()
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = armature.data.edit_bones
        for target in TARGETS:
            if target not in edit_bones:
                raise RuntimeError(f"Cannot set spine parent; missing bone {target}")
        for target, parent in TARGET_PARENTS.items():
            if parent is None:
                continue
            edit_bones[target].parent = edit_bones[parent]
            edit_bones[target].use_connect = False
            edit_bones[target].use_deform = True
        print("Applied canonical spine, neck, and clavicle parent hierarchy.")
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


def copy_source_tools_settings(old_data: bpy.types.Armature, new_data: bpy.types.Armature) -> None:
    for attr in ("display_type", "show_names", "show_axes", "show_bone_custom_shapes", "show_group_colors", "show_in_front"):
        if hasattr(old_data, attr) and hasattr(new_data, attr):
            try:
                setattr(new_data, attr, getattr(old_data, attr))
            except Exception:
                pass
    if hasattr(old_data, "vs") and hasattr(new_data, "vs"):
        try:
            for prop in old_data.vs.bl_rna.properties:
                if prop.identifier == "rna_type" or prop.is_readonly:
                    continue
                try:
                    setattr(new_data.vs, prop.identifier, getattr(old_data.vs, prop.identifier))
                except Exception:
                    pass
        except Exception:
            pass


def topological_bone_order(armature: bpy.types.Object) -> list[str]:
    bones = list(armature.data.bones)
    original_index = {bone.name: index for index, bone in enumerate(bones)}
    priority = {name: index for index, name in enumerate(CHAIN_TARGETS + ATTACHMENT_TARGETS)}
    children: dict[str | None, list[str]] = {}
    for bone in bones:
        parent = bone.parent.name if bone.parent else None
        children.setdefault(parent, []).append(bone.name)

    def sort_key(name: str) -> tuple[int, int, str]:
        return (priority.get(name, 10_000), original_index.get(name, 10_000), name.lower())

    ordered: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        ordered.append(name)
        for child in sorted(children.get(name, []), key=sort_key):
            visit(child)

    for root in sorted(children.get(None, []), key=sort_key):
        visit(root)
    for bone in sorted((bone.name for bone in bones if bone.name not in seen), key=sort_key):
        visit(bone)
    return ordered


def idprop_to_python(value: object) -> object:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    to_list = getattr(value, "to_list", None)
    if callable(to_list):
        return to_list()
    return value


def rebuild_armature_data(armature: bpy.types.Object) -> None:
    ensure_object_mode()
    old_data = armature.data
    order = topological_bone_order(armature)
    object_bone_state = {
        bone.name: {
            "hide": bool(bone.hide),
            "hide_select": bool(bone.hide_select),
            "custom_props": {key: idprop_to_python(bone[key]) for key in bone.keys()},
        }
        for bone in old_data.bones
    }
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = old_data.edit_bones
        bone_data = {
            name: {
                "head": edit_bones[name].head.copy(),
                "tail": edit_bones[name].tail.copy(),
                "roll": float(edit_bones[name].roll),
                "parent": edit_bones[name].parent.name if edit_bones[name].parent else None,
                "use_connect": bool(edit_bones[name].use_connect),
                "use_deform": bool(edit_bones[name].use_deform),
                "hide": bool(getattr(edit_bones[name], "hide", False)),
                "hide_select": bool(getattr(edit_bones[name], "hide_select", False)),
            }
            for name in order
        }
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    old_name = old_data.name
    new_data = bpy.data.armatures.new(old_name + "_mci_reordered")
    copy_source_tools_settings(old_data, new_data)
    armature.data = new_data
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        created: dict[str, bpy.types.EditBone] = {}
        for name in order:
            info = bone_data[name]
            bone = new_data.edit_bones.new(name)
            bone.head = info["head"]
            bone.tail = info["tail"]
            bone.roll = info["roll"]
            bone.use_deform = info["use_deform"]
            bone.hide = bool(info["hide"])
            bone.hide_select = bool(info["hide_select"])
            created[name] = bone
        for name in order:
            parent = bone_data[name]["parent"]
            if parent and parent in created:
                created[name].parent = created[parent]
                created[name].use_connect = bool(bone_data[name]["use_connect"])
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    for name, state in object_bone_state.items():
        bone = new_data.bones.get(name)
        if bone is None:
            continue
        bone.hide = state["hide"]
        bone.hide_select = state["hide_select"]
        for key, value in state["custom_props"].items():
            try:
                bone[key] = value
            except Exception:
                pass
    new_data.name = old_name
    if old_data.users == 0:
        bpy.data.armatures.remove(old_data)
    print("Rebuilt armature data in parent-before-child order.")


def placeholder_region_name(bone: bpy.types.Bone) -> str:
    head = bone.head_local
    if float(head.y) > 0.06:
        depth = "front"
    elif float(head.y) < -0.06:
        depth = "back"
    else:
        depth = "middle"
    if float(head.x) < -0.025:
        side = "right"
    elif float(head.x) > 0.025:
        side = "left"
    else:
        side = "middle"
    return f"{depth}_{side}"


def unique_placeholder_name(base: str, used: set[str], counters: dict[str, int]) -> str:
    index = counters.get(base, 0) + 1
    while True:
        candidate = f"{base}_{index:02d}"
        if candidate not in used:
            counters[base] = index
            used.add(candidate)
            return candidate
        index += 1


def stripped_safe_bone_name(name: str) -> str:
    return "".join(char for char in name if char.isascii() and (char.isalnum() or char == "_"))


def rename_unsafe_bones(armature: bpy.types.Object) -> list[dict[str, object]]:
    ensure_object_mode()
    used = {bone.name for bone in armature.data.bones}
    counters: dict[str, int] = {}
    mapping: dict[str, str] = {}
    methods: dict[str, str] = {}
    for bone in armature.data.bones:
        if is_safe_bone_name(bone.name):
            continue
        stripped = stripped_safe_bone_name(bone.name)
        if stripped and stripped not in used:
            new_name = stripped
            used.add(new_name)
            methods[bone.name] = "stripped_unsafe_characters"
        else:
            base = placeholder_region_name(bone)
            new_name = unique_placeholder_name(base, used, counters)
            methods[bone.name] = "positional_placeholder"
        mapping[bone.name] = new_name
    if not mapping:
        print("Bone name cleanup found no unsafe names.")
        return []

    print(f"Renaming {len(mapping)} unsafe bone name(s) to stripped safe names or placeholders.")
    temp_by_source = {source: f"__mci_name_tmp_{index:03d}__" for index, source in enumerate(mapping)}
    for source, temp in temp_by_source.items():
        armature.data.bones[source].name = temp
    for source, target in mapping.items():
        armature.data.bones[temp_by_source[source]].name = target

    for obj in mesh_objects():
        for source, temp in temp_by_source.items():
            rename_or_merge_vertex_group(obj, source, temp)
        for source, target in mapping.items():
            rename_or_merge_vertex_group(obj, temp_by_source[source], target)

    return [
        {
            "old_name": source,
            "new_name": target,
            "method": methods.get(source, "unknown"),
            "reason": "Name contained characters outside A-Z, a-z, 0-9, underscore, or the required ValveBiped.Bip01_ prefix pattern.",
        }
        for source, target in sorted(mapping.items(), key=lambda item: item[1])
    ]


def apply_plan(armature: bpy.types.Object, plan: dict[str, object]) -> dict[str, object]:
    pre_bone_merges = apply_pre_bone_merges(armature, plan)
    changes = apply_renames(armature, plan)
    added = add_missing_bones(armature, plan)
    pelvis_alignment = align_pelvis_if_far_from_spine1(armature)
    added_position_changes = normalize_added_spine_positions(armature, added)
    set_spine_parents(armature)
    rebuild_armature_data(armature)
    renamed_unsafe = rename_unsafe_bones(armature)
    validation = validation_for_armature(armature, added)
    return {
        "pre_bone_merges": pre_bone_merges,
        "renamed": changes,
        "added": sorted(added),
        "pelvis_alignment": pelvis_alignment,
        "added_position_changes": added_position_changes,
        "renamed_unsafe_bones": renamed_unsafe,
        "validation": validation,
    }


def load_plan(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def analyze_current_file(input_blend: Path, include_preview: bool = True) -> tuple[dict[str, object], dict[str, object]]:
    armature = active_armature()
    weighted = weighted_bone_names()
    bones = collect_bones(armature, weighted)
    proposal = proposal_for_armature(armature, weighted)
    current_validation = validation_for_armature(armature)
    analysis = {
        "version": 1,
        "input_blend": str(input_blend),
        "armature": armature.name,
        "object_count": len(bpy.data.objects),
        "mesh_object_count": len(mesh_objects()),
        "bone_count": len(armature.data.bones),
        "weighted_bone_count": len(weighted),
        "unsafe_bone_names": [bone.name for bone in armature.data.bones if not is_safe_bone_name(bone.name)],
        "safe_name_policy": "Bone names must use A-Z, a-z, 0-9, and underscore. Required ValveBiped.Bip01_* Source bones are preserved as exceptions.",
        "bones": bones,
        "model_preview": collect_model_preview(armature) if include_preview else {},
        "target_chain": CHAIN_TARGETS,
        "attachments": ATTACHMENT_TARGETS,
        "side_landmarks": SIDE_LANDMARKS,
        "current_validation": current_validation,
        "proposal": proposal,
    }
    proposal["input_blend"] = str(input_blend)
    return analysis, proposal


def main() -> int:
    args = parse_args()
    if not args.input_blend.exists():
        raise FileNotFoundError(args.input_blend)
    started = time.monotonic()
    print("Starting MMD Character Importer Blender step 3.")
    print(f"Opening fixed blend: {args.input_blend}")
    bpy.ops.wm.open_mainfile(filepath=str(args.input_blend))

    if args.mode == "analyze":
        if not args.analysis_json:
            raise RuntimeError("--analysis-json is required for analyze mode")
        analysis, proposal = analyze_current_file(args.input_blend)
        write_json(args.analysis_json, analysis)
        write_json(args.plan_json, proposal)
        print(f"Wrote spine analysis: {args.analysis_json}")
        print(f"Wrote proposed spine plan: {args.plan_json}")
        print(f"Blender spine analysis finished in {time.monotonic() - started:.1f}s")
        return 0

    if not args.output_blend:
        raise RuntimeError("--output-blend is required for apply mode")
    if not args.report_json:
        raise RuntimeError("--report-json is required for apply mode")
    plan = load_plan(args.plan_json)
    before_analysis, _proposal = analyze_current_file(args.input_blend, include_preview=False)
    armature = active_armature()
    result = apply_plan(armature, plan)
    if not result["validation"]["ok"]:
        print("Validation failed after spine repair.")
        for error in result["validation"]["errors"]:
            print(f"Error: {error}")
        # Write a structured failure report so the GUI can surface the
        # validation errors instead of a generic missing-output message.
        failure_report = {
            "version": 1,
            "input_blend": str(args.input_blend),
            "output_blend": str(args.output_blend),
            "plan_json": str(args.plan_json),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "before": before_analysis,
            "applied": result,
            "validation": result["validation"],
        }
        write_json(args.report_json, failure_report)
        print(f"Wrote spine fix failure report: {args.report_json}")
        raise RuntimeError("Spine validation failed after apply.")
    args.output_blend.parent.mkdir(parents=True, exist_ok=True)
    print(f"Saving spine-fixed blend file: {args.output_blend}")
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output_blend))
    after_analysis, _after_proposal = analyze_current_file(args.output_blend)
    report = {
        "version": 1,
        "input_blend": str(args.input_blend),
        "output_blend": str(args.output_blend),
        "plan_json": str(args.plan_json),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "before": before_analysis,
        "after": after_analysis,
        "applied": result,
    }
    write_json(args.report_json, report)
    print(f"Wrote spine fix report: {args.report_json}")
    print(f"Blender spine fix step finished in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
