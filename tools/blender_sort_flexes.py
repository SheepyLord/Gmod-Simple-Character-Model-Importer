#!/usr/bin/env python3
"""Blender-side step 7 facial/body flex sorting helper."""

from __future__ import annotations

import argparse
import ast
import colorsys
import json
import math
import re
import sys
import time
from pathlib import Path

import bpy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_FLEX_SCRIPT = ROOT / "reference" / "li_zhiyan_npc" / "3_Flexes" / "Blender_p3.py"
# Corpus-aggregated morph->flex dictionary (majority vote across released models;
# entries present in >=2 models, category-consistent). This is the primary,
# authoritative exact-match source. Kept under tools/ so it is both tracked and
# bundled with the rest of the pipeline (the reference/ tree is gitignored).
REFERENCE_FLEX_DICTIONARY = Path(__file__).resolve().parent / "flex_name_dictionary.json"
L4D2_REFERENCE_FLEX_DICTIONARY = Path(__file__).resolve().parent / "flex_name_dictionary_l4d2.json"
# The 52 FACS flex controllers an L4D2 survivor exposes (verified identical across
# the official coach/gambler/mechanic/producer survivors). In L4D2 mode a morph is
# kept only if it maps to one of these; everything else is dropped as redundant.
KNOWN_L4D2_FLEX_CONTROLLERS = frozenset({
    "right_lid_raiser", "left_lid_raiser", "right_lid_tightener", "left_lid_tightener",
    "right_lid_droop", "left_lid_droop", "right_lid_closer", "left_lid_closer",
    "half_closed", "blink", "right_lid_squinter", "left_lid_squinter",
    "right_inner_raiser", "left_inner_raiser", "right_outer_raiser", "left_outer_raiser",
    "right_lowerer", "left_lowerer", "right_cheek_raiser", "left_cheek_raiser",
    "right_wrinkler", "left_wrinkler", "dilator", "right_upper_raiser", "left_upper_raiser",
    "right_corner_puller", "left_corner_puller", "right_corner_depressor", "left_corner_depressor",
    "chin_raiser", "right_part", "left_part", "right_puckerer", "left_puckerer",
    "right_funneler", "left_funneler", "right_stretcher", "left_stretcher",
    "bite", "presser", "tightener", "jaw_clencher", "jaw_drop",
    "right_mouth_drop", "left_mouth_drop", "right_cheek_puffer", "left_cheek_puffer",
    "mouth_sideways", "jaw_sideways", "lower_lip", "eyes_updown", "eyes_rightleft",
})
GAME_CHOICES = ("gmod", "l4d2")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
FLEX_NAME_RE = re.compile(r"^[a-z0-9_]+$")
EPSILON = 1e-7
MAX_SOURCE_FLEXES_EXCLUSIVE = 95
TARGET_SOURCE_FLEXES = MAX_SOURCE_FLEXES_EXCLUSIVE - 1
# A flex counts as "matched" (confidently renamed to a recognized Source flex
# name) at or above this confidence; below it the original name was kept or a
# generic fallback was generated. Matches the low-confidence cutoff used when
# flagging names in collect_flexes().
MATCHED_CONFIDENCE_THRESHOLD = 0.60


FALLBACK_FLEX_MAPPING = {
    "Blink": "blink",
    "Blink Happy": "eye_blink_happy",
    "Wink": "eye_blink_happy_left",
    "Wink Right": "eye_blink_happy_right",
    "Wink 2": "eye_blink_left",
    "Wink 2 Right": "eye_blink_right",
    "Ah": "mouth_a",
    "Aha": "mouth_a",
    "Ch": "mouth_i",
    "Chi": "mouth_i",
    "U": "mouth_u",
    "E": "mouth_e",
    "Oh": "mouth_o",
    "Smile": "mouth_smile",
    "Grin": "mouth_grin",
    "Anger": "brows_angry",
    "Sad": "brows_sad",
    "Serious": "brows_serious",
    "Surprised": "eyes_surprised",
    "Calm": "eyes_calm",
    "Blush": "misc_blush",
}


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
    parser.add_argument("--flexes-json", type=Path)
    parser.add_argument("--game", choices=GAME_CHOICES, default="gmod")
    return parser.parse_args(argv)


def write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def natural_key(value: object) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value))]


def stripped_safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "", str(name or "").replace(" ", "_"))


def flex_output_name(name: str) -> str:
    return stripped_safe_name(name).lower()


def normalized_name(name: str) -> str:
    """Loose match key for a morph name (case/space/punctuation insensitive).

    CJK code points are PRESERVED. Stripping them (the old ``[^a-z0-9]``) made
    every Japanese-only morph normalize to the empty string, so an untranslated
    model collapsed all of its expressions onto whichever symbol-only dictionary
    key ("!", "?", "……", "▲", …) also normalized to "" — every flex came out as
    the same wrong name with a _NN counter. Keeping the kana/kanji lets Japanese
    dictionary entries match directly when CATS translation is unavailable.
    """
    text = str(name or "").lower()
    text = text.replace("左", "left").replace("右", "right")
    text = text.replace("ω", "omega")
    # Keep ASCII alphanumerics plus hiragana, katakana, CJK ideographs and the
    # CJK/fullwidth digit ranges MMD morph names use.
    return re.sub(r"[^a-z0-9぀-ゟ゠-ヿ㐀-䶿一-鿿０-９]+", "", text)


def unique_name(base: str, used: set[str], fallback: str) -> str:
    candidate = flex_output_name(base) or fallback
    fallback = flex_output_name(fallback) or "flex"
    if not FLEX_NAME_RE.fullmatch(candidate):
        candidate = fallback
    root = candidate
    index = 2
    while candidate in used:
        candidate = f"{root}_{index:02d}"
        index += 1
    used.add(candidate)
    return candidate


def load_l4d2_mapping() -> tuple[dict[str, str], list[str]]:
    """L4D2 morph->FACS-controller mapping. Only the dedicated L4D2 dictionary is
    used (the GMod fallback/corpus carry GMod-style names, not FACS controllers)."""
    warnings: list[str] = []
    try:
        data = json.loads(L4D2_REFERENCE_FLEX_DICTIONARY.read_text(encoding="utf-8"))
        mapping = data.get("mapping") if isinstance(data, dict) else None
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("missing or empty 'mapping' object")
        clean = {str(k): str(v) for k, v in mapping.items()}
        print(f"Loaded {len(clean)} L4D2 flex controller mappings.")
        return clean, warnings
    except Exception as exc:
        message = f"L4D2 flex dictionary unavailable ({L4D2_REFERENCE_FLEX_DICTIONARY}): {exc}; no morphs will map to L4D2 controllers."
        print(f"Warning: {message}")
        warnings.append(message)
        return {}, warnings


def load_reference_mapping(game: str = "gmod") -> tuple[dict[str, str], list[str]]:
    if game == "l4d2":
        return load_l4d2_mapping()
    mapping = dict(FALLBACK_FLEX_MAPPING)
    warnings: list[str] = []
    try:
        tree = ast.parse(REFERENCE_FLEX_SCRIPT.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "special_replacement_dict" for target in node.targets):
                continue
            value = ast.literal_eval(node.value)
            if isinstance(value, dict):
                mapping.update({str(key): str(val) for key, val in value.items()})
            break
    except Exception as exc:
        message = f"Reference flex mapping unavailable ({REFERENCE_FLEX_SCRIPT}): {exc}; relying on the bundled corpus dictionary and built-in fallback."
        print(f"Warning: {message}")
        warnings.append(message)
    # Authoritative corpus dictionary, applied last so its majority-vote names win.
    try:
        data = json.loads(REFERENCE_FLEX_DICTIONARY.read_text(encoding="utf-8"))
        corpus = data.get("mapping") if isinstance(data, dict) else None
        if isinstance(corpus, dict) and corpus:
            mapping.update({str(key): str(val) for key, val in corpus.items()})
            print(f"Loaded {len(corpus)} flex name mappings from the bundled corpus dictionary.")
        else:
            raise ValueError("missing or empty 'mapping' object")
    except Exception as exc:
        message = f"Bundled flex dictionary unavailable ({REFERENCE_FLEX_DICTIONARY}): {exc}; automatic flex names use {len(mapping)} fallback/reference entries."
        print(f"Warning: {message}")
        warnings.append(message)
    return mapping, warnings


def category_for_flex(name: str, bodygroup: str = "") -> str:
    lower = str(name or "").lower()
    if lower.startswith("brows_") or lower.startswith("brow"):
        return "brows"
    if lower.startswith("eye_") or lower.startswith("eyes_") or "blink" in lower:
        return "eyes"
    if lower.startswith("mouth_") or lower.startswith("jaw_"):
        return "mouth"
    if lower.startswith("nose_"):
        return "nose"
    if lower.startswith("misc_") or lower.startswith("emote_") or lower.startswith("face_"):
        return "misc"
    if "face" not in bodygroup.lower():
        return "body"
    return "face"


def infer_side_suffix(original: str) -> str:
    lower = str(original or "").lower()
    if re.search(r"(^|[_\-\s])l($|[_\-\s])", lower) or " left" in lower or lower.endswith("left") or "左" in original:
        return "_left"
    if re.search(r"(^|[_\-\s])r($|[_\-\s])", lower) or " right" in lower or lower.endswith("right") or "右" in original:
        return "_right"
    return ""


def infer_flex_name(original: str, bodygroup: str, mapping: dict[str, str], normalized_mapping: dict[str, str]) -> tuple[str, str, float, list[str]]:
    warnings: list[str] = []
    if original in mapping:
        name = mapping[original]
        return name, category_for_flex(name, bodygroup), 1.0, warnings
    normalized = normalized_name(original)
    if normalized and normalized in normalized_mapping:
        name = normalized_mapping[normalized]
        return name, category_for_flex(name, bodygroup), 0.92, warnings

    side = infer_side_suffix(original)
    lower = normalized
    # Detect the facial region the modeler named explicitly. This prefix is
    # DECISIVE for which Source flex family the expression belongs to: e.g.
    # "Mouth_Sad" is a mouth flex even though a bare "Sad" defaults to brows, and
    # "Mouth_Surprise" is a mouth flex even though a bare "Surprise" is eyes
    # (GitHub issue: Mouth_Sad01 was wrongly renamed brows_sad). Order matters so
    # "eyebrow" resolves to brows, not eyes.
    category_hint = ""
    if "brow" in lower or "mayu" in lower:
        category_hint = "brows"
    elif any(token in lower for token in ("mouth", "jaw", "lip", "tooth", "teeth", "tongue", "fang")):
        category_hint = "mouth"
    elif any(token in lower for token in ("eye", "blink", "wink", "pupil")):
        category_hint = "eyes"
    elif "nose" in lower:
        category_hint = "nose"
    if not category_hint:
        # Legacy first-letter fallback, preserved so models without an explicit
        # region token keep their previous (working) categorisation.
        if lower.startswith("b") and any(hint in lower for hint in ("angry", "anger", "sad", "flat", "happy", "cheer", "serious", "lower", "upper")):
            category_hint = "brows"
        elif lower.startswith("e") and any(hint in lower for hint in ("blink", "close", "sad", "anger", "surpris", "stare", "calm")):
            category_hint = "eyes"
        elif lower.startswith("m"):
            category_hint = "mouth"

    vowel_map = {
        "a": "mouth_a",
        "ah": "mouth_a",
        "aha": "mouth_a",
        "i": "mouth_i",
        "ch": "mouth_i",
        "u": "mouth_u",
        "e": "mouth_e",
        "o": "mouth_o",
        "oh": "mouth_o",
    }
    if lower in vowel_map:
        return vowel_map[lower], "mouth", 0.85, warnings
    if "blinkhappy" in lower:
        return f"eye_blink_happy{side}", "eyes", 0.82, warnings
    if "blink" in lower or "eyeclose" in lower:
        return f"eye_blink{side}", "eyes", 0.82, warnings
    if "wink" in lower:
        return f"eye_blink_happy{side}", "eyes", 0.80, warnings
    if "smil" in lower:  # smile / smiley / smily
        cat = category_hint or "mouth"
        return f"{cat}_smile{side}", cat, 0.78, warnings
    if "grin" in lower or "laugh" in lower:
        cat = category_hint if category_hint in ("mouth", "eyes") else "mouth"
        return f"{cat}_grin{side}", cat, 0.78, warnings
    if "angry" in lower or "anger" in lower:
        if category_hint == "mouth":
            return f"mouth_anger{side}", "mouth", 0.74, warnings
        if category_hint == "eyes":
            return f"eyes_anger{side}", "eyes", 0.74, warnings
        return f"brows_angry{side}", "brows", 0.74, warnings
    if "sad" in lower:
        if category_hint == "mouth":
            return f"mouth_sad{side}", "mouth", 0.74, warnings
        if category_hint == "eyes":
            return f"eyes_sad{side}", "eyes", 0.74, warnings
        return f"brows_sad{side}", "brows", 0.74, warnings
    if "surpris" in lower:
        if category_hint == "mouth":
            return f"mouth_surprise{side}", "mouth", 0.74, warnings
        if category_hint == "brows":
            return f"brows_surprise{side}", "brows", 0.74, warnings
        return f"eyes_surprised{side}", "eyes", 0.74, warnings
    if "blush" in lower or "face_red" in lower:
        return "misc_blush", "misc", 0.74, warnings

    cleaned = stripped_safe_name(original)
    if cleaned and SAFE_NAME_RE.fullmatch(cleaned):
        warnings.append("Low-confidence flex name; kept safe original spelling.")
        return cleaned, category_for_flex(cleaned, bodygroup), 0.48, warnings
    warnings.append("Low-confidence flex name; generated fallback name.")
    fallback_prefix = "face_flex" if "face" in bodygroup.lower() else "body_flex"
    return fallback_prefix, "face" if "face" in bodygroup.lower() else "body", 0.30, warnings


def ensure_object_mode() -> None:
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")


def mesh_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if obj.type == "MESH"]


AVERAGE_WORLD_Z_CACHE: dict[str, float] = {}


def object_world_matrix(obj: bpy.types.Object) -> np.ndarray:
    return np.array(obj.matrix_world, dtype=np.float64)


def object_vertex_coords(obj: bpy.types.Object) -> np.ndarray:
    count = len(obj.data.vertices)
    buffer = np.empty(count * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", buffer)
    return buffer.reshape((count, 3))


def object_average_world_z(obj: bpy.types.Object) -> float:
    cached = AVERAGE_WORLD_Z_CACHE.get(obj.name)
    if cached is not None:
        return cached
    if not obj.data.vertices:
        AVERAGE_WORLD_Z_CACHE[obj.name] = float("-inf")
        return float("-inf")
    coords = object_vertex_coords(obj).astype(np.float64)
    matrix = object_world_matrix(obj)
    average = float((coords @ matrix[2, :3]).mean() + matrix[2, 3])
    AVERAGE_WORLD_Z_CACHE[obj.name] = average
    return average


def bodygroup_processing_key(obj: bpy.types.Object) -> tuple[float, list[object]]:
    return (-object_average_world_z(obj), natural_key(obj.name))


def fallback_prune_shapekeys(obj: bpy.types.Object, epsilon: float = EPSILON) -> dict[str, object]:
    keys = obj.data.shape_keys
    if keys is None or len(keys.key_blocks) <= 1:
        return {"removed": [], "method": "none", "warnings": []}
    basis = keys.key_blocks[0]
    removed: list[str] = []
    warnings: list[str] = []
    for key in list(keys.key_blocks)[1:]:
        try:
            if shape_key_max_delta(obj, key.name, basis=basis) <= epsilon:
                removed.append(key.name)
                obj.shape_key_remove(key)
        except Exception as exc:
            warnings.append(f"{obj.name}:{key.name}: {exc}")
    return {"removed": removed, "method": "zero_delta_prune", "warnings": warnings}


def prune_shapekeys() -> dict[str, object]:
    report = {"objects": [], "warnings": []}
    ensure_object_mode()
    for obj in mesh_objects():
        object_report: dict[str, object] = {"object": obj.name, "method": "", "removed": [], "warnings": []}
        if obj.data.shape_keys is None or len(obj.data.shape_keys.key_blocks) <= 1:
            object_report["method"] = "none"
            report["objects"].append(object_report)
            continue
        object_report.update(fallback_prune_shapekeys(obj))
        report["objects"].append(object_report)
    return report


def shape_key_coords(key: bpy.types.ShapeKey) -> np.ndarray:
    count = len(key.data)
    buffer = np.empty(count * 3, dtype=np.float32)
    key.data.foreach_get("co", buffer)
    return buffer.reshape((count, 3))


def shape_key_max_delta(obj: bpy.types.Object, key_name: str, basis: bpy.types.ShapeKey | None = None) -> float:
    keys = obj.data.shape_keys
    if keys is None or key_name not in keys.key_blocks:
        return 0.0
    key = keys.key_blocks[key_name]
    basis = basis or keys.key_blocks[0]
    if not key.data:
        return 0.0
    deltas = shape_key_coords(key) - shape_key_coords(basis)
    return float(math.sqrt(float((deltas * deltas).sum(axis=1).max())))


def set_shape_key_delta_scale(obj: bpy.types.Object, key_name: str, scale: float) -> None:
    keys = obj.data.shape_keys
    if keys is None or key_name not in keys.key_blocks:
        return
    key = keys.key_blocks[key_name]
    basis = keys.key_blocks[0]
    for index, item in enumerate(key.data):
        item.co = basis.data[index].co + (item.co - basis.data[index].co) * float(scale)


def flex_delta_scale(rest_value: float, max_amplitude: float) -> float:
    return float(max_amplitude) - float(rest_value)


def as_float(value: object, default: float) -> float:
    if value is None or value == "":
        return float(default)
    return float(value)


def entry_enabled(entry: dict[str, object]) -> bool:
    return bool(entry.get("enabled", True)) and str(entry.get("action") or "keep") != "remove"


def sync_merge_sources(flexes: list[dict[str, object]]) -> None:
    by_uid = {str(entry.get("uid") or ""): entry for entry in flexes if entry.get("uid")}
    for entry in flexes:
        if str(entry.get("action") or "keep") != "merge":
            continue
        sources = entry.get("source_flexes", [])
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict) or not source.get("uid"):
                continue
            source_entry = by_uid.get(str(source.get("uid") or ""))
            if source_entry is None:
                continue
            source_rest = as_float(source_entry.get("rest_value", 0.0), 0.0)
            source_max = as_float(source_entry.get("max_amplitude", 1.0), 1.0)
            source["bodygroup"] = source_entry.get("bodygroup")
            source["original_name"] = source_entry.get("original_name")
            source["final_name"] = source_entry.get("final_name")
            source["rest_value"] = source_rest
            source["max_amplitude"] = source_max
            source["weight"] = flex_delta_scale(source_rest, source_max)


def source_weight(source: dict[str, object]) -> float:
    if source.get("weight") not in (None, ""):
        return as_float(source.get("weight"), 1.0)
    return flex_delta_scale(
        as_float(source.get("rest_value", 0.0), 0.0),
        as_float(source.get("max_amplitude", 1.0), 1.0),
    )


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


def material_for_polygon(obj: bpy.types.Object, poly: bpy.types.MeshPolygon) -> bpy.types.Material | None:
    if 0 <= int(poly.material_index) < len(obj.data.materials):
        return obj.data.materials[int(poly.material_index)]
    return None


def preview_color(uid: str, index: int) -> list[float]:
    seed = sum((offset + 1) * ord(char) for offset, char in enumerate(uid)) + index * 29
    red, green, blue = colorsys.hsv_to_rgb((seed % 360) / 360.0, 0.55, 0.86)
    return [round(red, 4), round(green, 4), round(blue, 4), 1.0]


def object_report(obj: bpy.types.Object) -> dict[str, object]:
    keys = obj.data.shape_keys
    shapekeys = [] if keys is None else [key.name for key in keys.key_blocks[1:]]
    average_height = object_average_world_z(obj)
    warnings: list[str] = []
    if not shapekeys:
        warnings.append("Bodygroup has no shapekeys.")
    elif "face" not in obj.name.lower():
        warnings.append("Body flex exists outside Face bodygroup.")
    return {
        "name": obj.name,
        "vertex_count": len(obj.data.vertices),
        "face_count": len(obj.data.polygons),
        "average_height": round(average_height, 6) if math.isfinite(average_height) else None,
        "material_count": len([mat for mat in obj.data.materials if mat is not None]),
        "vertex_group_count": len(obj.vertex_groups),
        "armature_modifiers": [modifier.name for modifier in obj.modifiers if isinstance(modifier, bpy.types.ArmatureModifier)],
        "shapekey_count": len(shapekeys),
        "shapekeys": shapekeys,
        "warnings": warnings,
    }


def flex_match_sort_key(entry: dict[str, object]) -> tuple[int, list[object]]:
    """Output ordering: matched (confidently named) flexes first, then unmatched,
    each group alphabetical (natural) by the final flex name."""
    try:
        confidence = float(entry.get("confidence", 0.0) or 0.0)
    except Exception:
        confidence = 0.0
    matched_rank = 0 if confidence >= MATCHED_CONFIDENCE_THRESHOLD else 1
    name = str(entry.get("final_name") or entry.get("original_name") or "")
    return (matched_rank, natural_key(name))


def sort_and_renumber_flexes(flexes: list[dict[str, object]]) -> None:
    """Reorder flexes (matched-alphabetical first, then unmatched-alphabetical)
    and reassign sequential uids so the plan, table, and exported flex order all
    follow the same order. Safe at analyze time: uids are not yet cross-referenced
    (merge source_flexes are populated later from the plan)."""
    flexes.sort(key=flex_match_sort_key)
    for new_index, entry in enumerate(flexes, start=1):
        entry["uid"] = f"flex_{new_index:03d}"


def reorder_shape_keys_to_plan(flexes: list[dict[str, object]]) -> list[str]:
    """Reorder each mesh's shape keys to follow the (sorted) plan order so the
    exported model's flex list matches step 7's output. Basis stays first."""
    warnings: list[str] = []
    rank: dict[str, int] = {}
    for index, entry in enumerate(flexes):
        final_name = str(entry.get("final_name") or "").strip()
        if final_name and final_name not in rank:
            rank[final_name] = index
    fallback_rank = len(flexes) + 1
    for obj in mesh_objects():
        keys = obj.data.shape_keys
        if keys is None or len(keys.key_blocks) <= 1:
            continue
        desired = sorted(
            (key.name for key in keys.key_blocks[1:]),
            key=lambda name: (rank.get(name, fallback_rank), natural_key(name)),
        )
        try:
            ensure_object_mode()
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
            blocks = obj.data.shape_keys.key_blocks
            for name in desired:
                idx = blocks.find(name)
                if idx <= 0:  # skip missing (-1) and basis (0)
                    continue
                obj.active_shape_key_index = idx
                bpy.ops.object.shape_key_move(type="BOTTOM")
        except Exception as exc:
            warnings.append(f"Could not reorder shape keys on {obj.name}: {exc}")
        finally:
            ensure_object_mode()
    return warnings


def l4d2_category_for(controller: str) -> str:
    if "lid" in controller or "blink" in controller or controller.startswith("eyes_") or "squinter" in controller:
        return "eyes"
    if "inner_raiser" in controller or "outer_raiser" in controller or "lowerer" in controller:
        return "brows"
    return "mouth"


def resolve_l4d2_controller(original: str, mapping: dict[str, str], normalized_mapping: dict[str, str]) -> tuple[str, float]:
    """Map an MMD morph to its L4D2 FACS controller via exact then normalized match."""
    if original in mapping:
        return mapping[original], 1.0
    norm = normalized_name(original)
    if norm and norm in normalized_mapping:
        return normalized_mapping[norm], 0.92
    return "", 0.0


def collect_flexes(game: str = "gmod") -> tuple[list[dict[str, object]], list[str]]:
    mapping, mapping_warnings = load_reference_mapping(game)
    # Skip keys with an empty normalized form (symbol-only names like "!", "?",
    # "……", "▲"). They would otherwise all share the "" bucket and match every
    # morph whose name normalizes to nothing.
    normalized_mapping = {
        normalized: value
        for normalized, value in ((normalized_name(key), value) for key, value in mapping.items())
        if normalized
    }
    used_names: set[str] = set()
    used_l4d2_controllers: set[str] = set()
    flexes: list[dict[str, object]] = []
    index = 1
    for obj in sorted(mesh_objects(), key=bodygroup_processing_key):
        keys = obj.data.shape_keys
        if keys is None or len(keys.key_blocks) <= 1:
            continue
        average_height = object_average_world_z(obj)
        for key in keys.key_blocks[1:]:
            max_delta = shape_key_max_delta(obj, key.name)
            if max_delta <= EPSILON:
                continue
            fallback = "face_flex_%03d" % index if "face" in obj.name.lower() else "body_flex_%03d" % index
            if game == "l4d2":
                # L4D2 keeps only morphs that map to a free FACS controller; every
                # other morph (and duplicates of a taken controller) is dropped.
                controller, confidence = resolve_l4d2_controller(key.name, mapping, normalized_mapping)
                warnings = []
                if controller in KNOWN_L4D2_FLEX_CONTROLLERS and controller not in used_l4d2_controllers:
                    used_l4d2_controllers.add(controller)
                    final_name = controller
                    category = l4d2_category_for(controller)
                    enabled = True
                    action = "keep"
                else:
                    enabled = False
                    action = "remove"
                    final_name = flex_output_name(key.name) or ("removed_flex_%03d" % index)
                    category = "face" if "face" in obj.name.lower() else "body"
                    confidence = 0.0
                    if controller in used_l4d2_controllers:
                        warnings.append(f"Duplicate of L4D2 flex controller '{controller}'; removed as redundant.")
                    else:
                        warnings.append("No L4D2 facial flex controller maps to this morph; removed as redundant.")
            else:
                base_name, category, confidence, warnings = infer_flex_name(key.name, obj.name, mapping, normalized_mapping)
                final_name = unique_name(base_name, used_names, fallback)
                enabled = True
                action = "keep"
                if confidence < 0.60:
                    warnings.append("Low-confidence automatic name mapping.")
            if obj.name.lower() != "face" and "face" not in obj.name.lower():
                warnings.append("Body flex exists outside Face bodygroup.")
            flexes.append(
                {
                    "uid": f"flex_{index:03d}",
                    "enabled": enabled,
                    "action": action,
                    "final_name": final_name,
                    "original_name": key.name,
                    "bodygroup": obj.name,
                    "bodygroup_average_height": round(average_height, 6) if math.isfinite(average_height) else None,
                    "category": category,
                    "confidence": round(confidence, 3),
                    "rest_value": round(float(key.value), 4),
                    "max_amplitude": 1.0,
                    "source_flexes": [],
                    "max_delta": round(max_delta, 6),
                    "warnings": warnings,
                }
            )
            index += 1
    return flexes, mapping_warnings


def auto_mark_excess_flexes_for_removal(flexes: list[dict[str, object]]) -> int:
    enabled_entries = [
        entry
        for entry in flexes
        if bool(entry.get("enabled", True)) and str(entry.get("action") or "keep") != "remove"
    ]
    if len(enabled_entries) <= TARGET_SOURCE_FLEXES:
        return 0

    def keep_priority(entry: dict[str, object], index: int) -> tuple[float, int]:
        category = str(entry.get("category") or "").lower()
        bodygroup = str(entry.get("bodygroup") or "").lower()
        name = str(entry.get("final_name") or entry.get("original_name") or "").lower()
        action = str(entry.get("action") or "keep")
        try:
            confidence = float(entry.get("confidence", 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        priority = confidence * 20.0
        if action == "merge":
            priority += 100.0
        if bodygroup == "face":
            priority += 15.0
        if category in {"mouth", "eye", "eyes", "brow", "brows"}:
            priority += 40.0
        if any(token in name for token in ("blink", "smile", "mouth", "eye", "brow", "angry", "sad", "happy")):
            priority += 15.0
        if category in {"body", "misc", "unknown"}:
            priority -= 12.0
        if str(entry.get("final_name") or "").startswith(("face_flex_", "body_flex_")):
            priority -= 8.0
        return (priority, -index)

    indexed = [
        (entry, index)
        for index, entry in enumerate(flexes)
        if bool(entry.get("enabled", True)) and str(entry.get("action") or "keep") != "remove"
    ]
    indexed.sort(key=lambda item: keep_priority(item[0], item[1]))
    remove_count = max(0, len(enabled_entries) - TARGET_SOURCE_FLEXES)
    note = "Auto-marked for removal because Source flex count must stay below 95."
    for entry, _index in indexed[:remove_count]:
        entry["enabled"] = False
        if str(entry.get("action") or "keep") != "merge":
            entry["action"] = "remove"
        warnings = entry.setdefault("warnings", [])
        if isinstance(warnings, list) and note not in warnings:
            warnings.append(note)
    return remove_count


def flex_uid_lookup(flexes: list[dict[str, object]]) -> dict[tuple[str, str], str]:
    return {
        (str(entry.get("bodygroup") or ""), str(entry.get("original_name") or "")): str(entry.get("uid") or "")
        for entry in flexes
        if entry.get("uid")
    }


def collect_flex_preview(flexes: list[dict[str, object]], max_triangles: int = 500000) -> dict[str, object]:
    lookup = flex_uid_lookup(flexes)
    total_triangles = sum(max(0, len(poly.vertices) - 2) for obj in mesh_objects() for poly in obj.data.polygons)
    stride = max(1, math.ceil(total_triangles / max_triangles)) if total_triangles else 1
    triangles: list[dict[str, object]] = []
    materials_by_uid: dict[str, dict[str, object]] = {}
    overall_mins: np.ndarray | None = None
    overall_maxs: np.ndarray | None = None
    triangle_index = 0
    for object_index, obj in enumerate(sorted(mesh_objects(), key=bodygroup_processing_key), start=1):
        bodygroup_uid = f"bodygroup_{object_index:03d}_{stripped_safe_name(obj.name).lower()[:32]}"
        uv_layer = obj.data.uv_layers.active
        keys = obj.data.shape_keys
        flex_keys = [] if keys is None else [key for key in keys.key_blocks[1:] if (obj.name, key.name) in lookup]
        matrix = object_world_matrix(obj)
        world_coords = np.round(object_vertex_coords(obj).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3], 6)
        uv_coords: np.ndarray | None = None
        if uv_layer is not None and len(uv_layer.data):
            uv_buffer = np.empty(len(uv_layer.data) * 2, dtype=np.float32)
            uv_layer.data.foreach_get("uv", uv_buffer)
            uv_coords = np.round(uv_buffer.astype(np.float64).reshape((-1, 2)), 6)
        key_world_deltas: dict[str, np.ndarray] = {}
        if keys is not None and flex_keys:
            world3 = np.array(obj.matrix_world.to_3x3(), dtype=np.float64)
            basis_coords = shape_key_coords(keys.key_blocks[0]).astype(np.float64)
            for key in flex_keys:
                local_deltas = shape_key_coords(key).astype(np.float64) - basis_coords
                key_world_deltas[key.name] = np.round(local_deltas @ world3.T, 6)
        uid_by_material_index: dict[int, str] = {}
        if len(world_coords) and len(obj.data.polygons):
            object_mins = world_coords.min(axis=0)
            object_maxs = world_coords.max(axis=0)
            overall_mins = object_mins if overall_mins is None else np.minimum(overall_mins, object_mins)
            overall_maxs = object_maxs if overall_maxs is None else np.maximum(overall_maxs, object_maxs)
        for poly in obj.data.polygons:
            material_index = int(poly.material_index)
            material_uid = uid_by_material_index.get(material_index)
            if material_uid is None:
                mat = material_for_polygon(obj, poly)
                mat_name = mat.name if mat is not None else "No_Material"
                material_uid = f"{bodygroup_uid}__mat_{material_index:03d}_{stripped_safe_name(mat_name).lower()[:32]}"
                uid_by_material_index[material_index] = material_uid
                if material_uid not in materials_by_uid:
                    texture_path = material_texture_path(mat)
                    color = preview_color(material_uid, object_index + material_index + 1)
                    materials_by_uid[material_uid] = {
                        "uid": material_uid,
                        "material_name": f"{obj.name} / {mat_name}",
                        "proposed_name": mat_name,
                        "bodygroup": obj.name,
                        "keep": True,
                        "preview_color": color,
                        "base_color_path": texture_path if texture_path and not texture_path.startswith("packed:") else "",
                        "base_color_file": Path(texture_path).name if texture_path and not texture_path.startswith("packed:") else texture_path,
                        "alpha": 1.0,
                    }
            verts = list(poly.vertices)
            loops = list(poly.loop_indices)
            if len(verts) < 3:
                continue
            for offset in range(1, len(verts) - 1):
                if triangle_index % stride == 0:
                    loop_indices = [loops[0], loops[offset], loops[offset + 1]]
                    vertex_indices = [verts[0], verts[offset], verts[offset + 1]]
                    coords = [world_coords[index].tolist() for index in vertex_indices]
                    uvs: list[list[float]] = []
                    for loop_index in loop_indices:
                        if uv_coords is not None and 0 <= loop_index < len(uv_coords):
                            uvs.append(uv_coords[loop_index].tolist())
                        else:
                            uvs.append([0.0, 0.0])
                    flex_deltas: dict[str, list[list[float]]] = {}
                    for key_name, world_deltas in key_world_deltas.items():
                        tri_deltas = world_deltas[vertex_indices]
                        if (np.abs(tri_deltas) > EPSILON).any():
                            flex_deltas[lookup[(obj.name, key_name)]] = tri_deltas.tolist()
                    triangles.append(
                        {
                            "points": coords,
                            "uvs": uvs,
                            "material_uid": material_uid,
                            "polygon_index": int(poly.index),
                            "flex_deltas": flex_deltas,
                        }
                    )
                triangle_index += 1
    mins = overall_mins.tolist() if overall_mins is not None else [0.0, 0.0, 0.0]
    maxs = overall_maxs.tolist() if overall_maxs is not None else [1.0, 1.0, 1.0]
    materials = sorted(materials_by_uid.values(), key=lambda item: natural_key(item.get("uid", "")))
    return {
        "materials": materials,
        "material_count": len(materials),
        "model_preview": {
            "triangles": triangles,
            "source_triangle_count": total_triangles,
            "sampled_triangle_count": len(triangles),
            "sample_stride": stride,
            "mins": mins,
            "maxs": maxs,
        },
    }


def analyze_scene(input_blend: Path, game: str = "gmod") -> tuple[dict[str, object], dict[str, object]]:
    prune_report = prune_shapekeys()
    bodygroups = [object_report(obj) for obj in sorted(mesh_objects(), key=bodygroup_processing_key)]
    flexes, mapping_warnings = collect_flexes(game)
    sort_and_renumber_flexes(flexes)
    preview = collect_flex_preview(flexes)
    auto_removed = auto_mark_excess_flexes_for_removal(flexes)
    warnings: list[str] = list(mapping_warnings)
    for group in bodygroups:
        warnings.extend(str(warning) for warning in group.get("warnings", []) if warning)
    for flex in flexes:
        warnings.extend(str(warning) for warning in flex.get("warnings", []) if warning)
    if auto_removed:
        warnings.append(f"Auto-marked {auto_removed} low-priority flexes for removal to stay below the Source flex limit.")
    analysis = {
        "version": 1,
        "kind": "sort_flexes",
        "input_blend": str(input_blend),
        "prune": prune_report,
        "bodygroups": bodygroups,
        "bodygroup_count": len(bodygroups),
        "flexes": flexes,
        "flex_count": len(flexes),
        "enabled_flex_count": sum(1 for entry in flexes if entry_enabled(entry)),
        "auto_removed_for_source_limit": auto_removed,
        "model_preview": preview.get("model_preview", {}),
        "materials": preview.get("materials", []),
        "material_count": preview.get("material_count", 0),
        "warnings": sorted(set(warnings), key=natural_key),
    }
    plan = {
        "version": 1,
        "kind": "sort_flexes_plan",
        "input_blend": str(input_blend),
        "flexes": flexes,
        "enabled_flex_count": sum(1 for entry in flexes if entry_enabled(entry)),
        "auto_removed_for_source_limit": auto_removed,
        "warnings": [f"Auto-marked {auto_removed} low-priority flexes for removal to stay below the Source flex limit."] if auto_removed else [],
    }
    return analysis, plan


def validate_plan(plan: dict[str, object]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    flexes = [entry for entry in plan.get("flexes", []) if isinstance(entry, dict)]
    sync_merge_sources(flexes)
    keep_count = sum(1 for entry in flexes if entry_enabled(entry))
    if keep_count >= MAX_SOURCE_FLEXES_EXCLUSIVE:
        errors.append(f"Source flex count must be smaller than {MAX_SOURCE_FLEXES_EXCLUSIVE}; {keep_count} flexes are enabled.")
    for entry in flexes:
        if not entry_enabled(entry):
            continue
        name = str(entry.get("final_name") or "").strip()
        if not FLEX_NAME_RE.fullmatch(name):
            errors.append(f"{entry.get('uid')}: flex name must use lowercase letters, numbers, and underscores: {name!r}")
        if name in seen:
            errors.append(f"{entry.get('uid')}: duplicate flex name {name!r}")
        seen.add(name)
        action = str(entry.get("action") or "keep")
        if action == "merge" and not entry.get("source_flexes"):
            errors.append(f"{entry.get('uid')}: additive merge has no source flexes")
        try:
            max_amplitude = as_float(entry.get("max_amplitude", 1.0), 1.0)
        except Exception:
            errors.append(f"{entry.get('uid')}: invalid max amplitude")
            max_amplitude = 1.0
        try:
            rest_value = as_float(entry.get("rest_value", 0.0), 0.0)
        except Exception:
            errors.append(f"{entry.get('uid')}: invalid rest value")
            rest_value = 0.0
        if abs(flex_delta_scale(rest_value, max_amplitude)) <= EPSILON:
            errors.append(f"{entry.get('uid')}: max amplitude must be different from rest value")
    return errors


def object_by_name(name: str) -> bpy.types.Object | None:
    obj = bpy.data.objects.get(name)
    return obj if obj is not None and obj.type == "MESH" else None


def apply_plan(input_blend: Path, plan: dict[str, object], output_blend: Path, report_json: Path, flexes_json: Path) -> None:
    started = time.monotonic()
    errors = validate_plan(plan)
    if errors:
        raise RuntimeError("Flex plan validation failed:\n" + "\n".join(errors))
    prune_report = prune_shapekeys()
    before_groups = {obj.name: object_report(obj) for obj in mesh_objects()}
    flexes = [entry for entry in plan.get("flexes", []) if isinstance(entry, dict)]
    sync_merge_sources(flexes)
    created_merges: list[dict[str, object]] = []
    removed: list[dict[str, object]] = []
    renamed: list[dict[str, object]] = []
    warnings: list[str] = []

    needed_names_by_object: dict[str, set[str]] = {}
    for entry in flexes:
        if str(entry.get("action") or "keep") == "merge":
            for source in entry.get("source_flexes", []):
                if not isinstance(source, dict):
                    continue
                source_bodygroup = str(source.get("bodygroup") or "")
                source_name = str(source.get("original_name") or source.get("source_name") or "")
                if source_bodygroup and source_name:
                    needed_names_by_object.setdefault(source_bodygroup, set()).add(source_name)
            continue
        bodygroup = str(entry.get("bodygroup") or "")
        original = str(entry.get("original_name") or "")
        if bodygroup and original:
            needed_names_by_object.setdefault(bodygroup, set()).add(original)

    basis_by_object: dict[str, np.ndarray] = {}
    raw_by_object_uid: dict[str, dict[str, np.ndarray]] = {}
    raw_by_object_name: dict[str, dict[str, np.ndarray]] = {}
    for obj in mesh_objects():
        keys = obj.data.shape_keys
        if keys is None or not keys.key_blocks:
            continue
        basis = keys.key_blocks[0]
        basis_coords = shape_key_coords(basis)
        basis_by_object[obj.name] = basis_coords
        wanted_names = needed_names_by_object.get(obj.name, set())
        name_deltas: dict[str, np.ndarray] = {}
        for key in keys.key_blocks[1:]:
            if key.name in wanted_names:
                name_deltas[key.name] = shape_key_coords(key) - basis_coords
        raw_by_object_name[obj.name] = name_deltas
        raw_by_object_uid[obj.name] = {}

    for entry in flexes:
        if str(entry.get("action") or "keep") == "merge":
            continue
        bodygroup = str(entry.get("bodygroup") or "")
        original = str(entry.get("original_name") or "")
        uid = str(entry.get("uid") or "")
        delta = raw_by_object_name.get(bodygroup, {}).get(original)
        if uid and delta is not None:
            raw_by_object_uid.setdefault(bodygroup, {})[uid] = delta

    merge_targets: dict[tuple[str, str], np.ndarray] = {}
    for entry in flexes:
        if not entry_enabled(entry) or str(entry.get("action") or "keep") != "merge":
            continue
        uid = str(entry.get("uid") or "")
        grouped: dict[str, list[dict[str, object]]] = {}
        for source in entry.get("source_flexes", []):
            if isinstance(source, dict):
                grouped.setdefault(str(source.get("bodygroup") or ""), []).append(source)
        for bodygroup, specs in grouped.items():
            basis_coords = basis_by_object.get(bodygroup)
            if basis_coords is None or not len(basis_coords):
                warnings.append(f"{entry.get('uid')}: missing merge bodygroup {bodygroup}")
                continue
            total_delta = np.zeros_like(basis_coords)
            used_sources = 0
            for source in specs:
                source_uid = str(source.get("uid") or "")
                source_name = str(source.get("original_name") or source.get("source_name") or "")
                source_delta = raw_by_object_uid.get(bodygroup, {}).get(source_uid)
                if source_delta is None:
                    source_delta = raw_by_object_name.get(bodygroup, {}).get(source_name)
                if source_delta is None:
                    warnings.append(f"{entry.get('uid')}: missing merge source {bodygroup}:{source_name or source_uid}")
                    continue
                weight = source_weight(source)
                total_delta += source_delta * float(weight)
                used_sources += 1
            if used_sources:
                raw_by_object_uid.setdefault(bodygroup, {})[uid] = total_delta
                merge_targets[(uid, bodygroup)] = total_delta

    temp_key_names: dict[tuple[str, str], str] = {}
    for entry in flexes:
        if str(entry.get("action") or "keep") == "merge":
            continue
        bodygroup = str(entry.get("bodygroup") or "")
        original = str(entry.get("original_name") or "")
        obj = object_by_name(bodygroup)
        if obj is None or obj.data.shape_keys is None or original not in obj.data.shape_keys.key_blocks:
            continue
        temp_name = f"__mci_flex_tmp_{str(entry.get('uid') or '')}"
        index = 2
        while temp_name in obj.data.shape_keys.key_blocks:
            temp_name = f"__mci_flex_tmp_{str(entry.get('uid') or '')}_{index:02d}"
            index += 1
        obj.data.shape_keys.key_blocks[original].name = temp_name
        temp_key_names[(bodygroup, original)] = temp_name

    rest_offsets: dict[str, np.ndarray] = {
        bodygroup: np.zeros_like(basis_coords)
        for bodygroup, basis_coords in basis_by_object.items()
    }
    for entry in flexes:
        if not entry_enabled(entry):
            continue
        uid = str(entry.get("uid") or "")
        rest_value = as_float(entry.get("rest_value", 0.0), 0.0)
        if abs(rest_value) <= EPSILON:
            continue
        if str(entry.get("action") or "keep") == "merge":
            bodygroups = [bodygroup for merge_uid, bodygroup in merge_targets if merge_uid == uid]
        else:
            bodygroups = [str(entry.get("bodygroup") or "")]
        for bodygroup in bodygroups:
            delta = raw_by_object_uid.get(bodygroup, {}).get(uid)
            offset = rest_offsets.get(bodygroup)
            if delta is None or offset is None:
                continue
            offset += delta * float(rest_value)

    normalized_basis: dict[str, np.ndarray] = {}
    for bodygroup, basis_coords in basis_by_object.items():
        obj = object_by_name(bodygroup)
        if obj is None or obj.data.shape_keys is None:
            continue
        offset = rest_offsets.get(bodygroup)
        new_basis = basis_coords + offset if offset is not None else basis_coords.copy()
        basis = obj.data.shape_keys.key_blocks[0]
        basis.data.foreach_set("co", np.ascontiguousarray(new_basis, dtype=np.float32).ravel())
        normalized_basis[bodygroup] = new_basis

    for entry in flexes:
        action = str(entry.get("action") or "keep")
        if action == "merge":
            continue
        bodygroup = str(entry.get("bodygroup") or "")
        original = str(entry.get("original_name") or "")
        obj = object_by_name(bodygroup)
        temp_name = temp_key_names.get((bodygroup, original), original)
        if obj is None or obj.data.shape_keys is None or temp_name not in obj.data.shape_keys.key_blocks:
            if entry.get("enabled", True):
                warnings.append(f"{entry.get('uid')}: missing source flex {bodygroup}:{original}")
            continue
        key = obj.data.shape_keys.key_blocks[temp_name]
        if not entry.get("enabled", True) or action == "remove":
            obj.shape_key_remove(key)
            removed.append({"uid": entry.get("uid"), "bodygroup": bodygroup, "name": original})
            continue
        final_name = str(entry.get("final_name") or original)
        amplitude = as_float(entry.get("max_amplitude", 1.0), 1.0)
        rest_value = as_float(entry.get("rest_value", 0.0), 0.0)
        delta_scale = flex_delta_scale(rest_value, amplitude)
        raw_delta = raw_by_object_uid.get(bodygroup, {}).get(str(entry.get("uid") or ""))
        new_basis = normalized_basis.get(bodygroup)
        if raw_delta is None or new_basis is None:
            warnings.append(f"{entry.get('uid')}: missing raw flex delta for {bodygroup}:{original}")
            continue
        key.data.foreach_set("co", np.ascontiguousarray(new_basis + raw_delta * float(delta_scale), dtype=np.float32).ravel())
        key.name = final_name
        key.value = 0.0
        key.slider_min = 0.0
        key.slider_max = 1.0
        renamed.append(
            {
                "uid": entry.get("uid"),
                "bodygroup": bodygroup,
                "original": original,
                "final": final_name,
                "max_amplitude": amplitude,
                "rest_value": rest_value,
                "delta_scale": delta_scale,
            }
        )

    for entry in flexes:
        if not entry_enabled(entry) or str(entry.get("action") or "keep") != "merge":
            continue
        uid = str(entry.get("uid") or "")
        final_name = str(entry.get("final_name") or "").strip()
        rest_value = as_float(entry.get("rest_value", 0.0), 0.0)
        amplitude = as_float(entry.get("max_amplitude", 1.0), 1.0)
        delta_scale = flex_delta_scale(rest_value, amplitude)
        for (merge_uid, bodygroup), raw_delta in sorted(merge_targets.items(), key=lambda item: natural_key(item[0][1])):
            if merge_uid != uid:
                continue
            obj = object_by_name(bodygroup)
            new_basis = normalized_basis.get(bodygroup)
            if obj is None or new_basis is None:
                warnings.append(f"{entry.get('uid')}: missing merge target bodygroup {bodygroup}")
                continue
            keys = obj.data.shape_keys
            if keys is None:
                obj.shape_key_add(name="Basis", from_mix=False)
                keys = obj.data.shape_keys
            if keys is None:
                warnings.append(f"{entry.get('uid')}: failed to create shapekeys for {bodygroup}")
                continue
            key = keys.key_blocks.get(final_name) if final_name in keys.key_blocks else obj.shape_key_add(name=final_name, from_mix=False)
            key.data.foreach_set("co", np.ascontiguousarray(new_basis + raw_delta * float(delta_scale), dtype=np.float32).ravel())
            key.value = 0.0
            key.slider_min = 0.0
            key.slider_max = 1.0
            created_merges.append(
                {
                    "uid": entry.get("uid"),
                    "bodygroup": bodygroup,
                    "name": key.name,
                    "source_count": len([source for source in entry.get("source_flexes", []) if isinstance(source, dict) and str(source.get("bodygroup") or "") == bodygroup]),
                    "max_amplitude": amplitude,
                    "rest_value": rest_value,
                    "delta_scale": delta_scale,
                }
            )

    warnings.extend(reorder_shape_keys_to_plan(flexes))

    after_groups = {obj.name: object_report(obj) for obj in mesh_objects()}
    validation_errors: list[str] = []
    for entry in flexes:
        if not entry.get("enabled", True) or str(entry.get("action") or "keep") == "remove":
            continue
        if str(entry.get("action") or "keep") == "merge":
            final_name = str(entry.get("final_name") or "")
            bodygroups = {
                str(source.get("bodygroup") or "")
                for source in entry.get("source_flexes", [])
                if isinstance(source, dict) and source.get("bodygroup")
            }
            for bodygroup in sorted(bodygroups, key=natural_key):
                obj = object_by_name(bodygroup)
                if obj is None or obj.data.shape_keys is None or final_name not in obj.data.shape_keys.key_blocks:
                    validation_errors.append(f"{entry.get('uid')}: merged flex {final_name!r} missing on {bodygroup}")
            continue
        obj = object_by_name(str(entry.get("bodygroup") or ""))
        final_name = str(entry.get("final_name") or "")
        if obj is None or obj.data.shape_keys is None or final_name not in obj.data.shape_keys.key_blocks:
            validation_errors.append(f"{entry.get('uid')}: final flex {final_name!r} missing on {entry.get('bodygroup')}")
    for obj in mesh_objects():
        keys = obj.data.shape_keys
        if keys is None:
            continue
        for key in keys.key_blocks[1:]:
            if shape_key_max_delta(obj, key.name) <= EPSILON:
                validation_errors.append(f"{obj.name}:{key.name}: unused zero-delta shapekey remains")
    for name, before in before_groups.items():
        after = after_groups.get(name)
        if after is None:
            continue
        if int(after.get("material_count", 0) or 0) < int(before.get("material_count", 0) or 0):
            validation_errors.append(f"{name}: material slots were lost")
        if int(after.get("vertex_group_count", 0) or 0) < int(before.get("vertex_group_count", 0) or 0):
            validation_errors.append(f"{name}: vertex groups were lost")
        if len(after.get("armature_modifiers", [])) < len(before.get("armature_modifiers", [])):
            validation_errors.append(f"{name}: armature modifiers were lost")

    output_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_blend))
    flexes_json_data = {
        "version": 1,
        "kind": "flexes",
        "input_blend": str(input_blend),
        "output_blend": str(output_blend),
        "flexes": flexes,
    }
    write_json(flexes_json, flexes_json_data)
    report = {
        "version": 1,
        "kind": "sort_flexes_report",
        "input_blend": str(input_blend),
        "output_blend": str(output_blend),
        "flexes_json": str(flexes_json),
        "prune": prune_report,
        "created_merges": created_merges,
        "renamed": renamed,
        "removed": removed,
        "bodygroups_before": list(before_groups.values()),
        "bodygroups_after": list(after_groups.values()),
        "validation": {"ok": not validation_errors, "errors": validation_errors, "warnings": warnings},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    write_json(report_json, report)


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    bpy.ops.wm.open_mainfile(filepath=str(args.input_blend))
    if args.mode == "analyze":
        if not args.analysis_json:
            raise RuntimeError("--analysis-json is required in analyze mode")
        print("Analyzing flexes")
        analysis, plan = analyze_scene(args.input_blend, args.game)
        analysis["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(args.analysis_json, analysis)
        write_json(args.plan_json, plan)
        print(f"Wrote flex analysis: {args.analysis_json}")
        print(f"Wrote flex plan: {args.plan_json}")
    else:
        if not args.output_blend or not args.report_json or not args.flexes_json:
            raise RuntimeError("--output-blend, --report-json, and --flexes-json are required in apply mode")
        plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
        print("Applying flex plan")
        apply_plan(args.input_blend, plan, args.output_blend, args.report_json, args.flexes_json)
        print(f"Wrote flex blend: {args.output_blend}")
        print(f"Wrote flex report: {args.report_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
