#!/usr/bin/env python3
"""Step 12 texture and normal-map processing for MMD Character Importer."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

try:
    from PIL import Image, ImageOps
except Exception as exc:  # pragma: no cover - dependency guard
    raise SystemExit("Pillow is required for Step 12 texture processing. Install with: python -m pip install Pillow") from exc


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds"}
IMAGE_EXTENSION_PRIORITY = (".png", ".tga", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".dds")
NORMAL_HINTS = ("_n", "_hn", "_normal", "_norm", "_nrm", "_bump", "normal", "norm", "bump")
BASE_SUFFIXES = (
    "_d",
    "_diffuse",
    "_basecolor",
    "_base_color",
    "_albedo",
    "_color",
    "_col",
    "_tex",
)
NORMAL_SUFFIXES = ("_n", "_normal", "_norm", "_nrm", "_bump", "_hn", "normal", "norm", "nrm", "bump")
MAX_TEXTURE_EDGE = 4096
# L4D2's engine crashes on very large textures, so every map (base/normal/phong-exp/selfillum)
# is clamped to 2048px on its longest edge for L4D2. GMod keeps the 4096 cap. The effective cap
# is set per-run from the selected game; resize_to_limit and the analyze preview read it.
L4D2_MAX_TEXTURE_EDGE = 2048
_ACTIVE_MAX_TEXTURE_EDGE = MAX_TEXTURE_EDGE


def max_texture_edge_for_game(game: str | None) -> int:
    return L4D2_MAX_TEXTURE_EDGE if str(game or "").strip().lower() == "l4d2" else MAX_TEXTURE_EDGE


# User-selectable texture-size caps exposed in Step 12 (longest edge, px). 4096 is the GMod default.
ALLOWED_TEXTURE_EDGES = (4096, 2048, 1024)


def resolve_max_texture_edge(game: str | None, user_edge: object) -> int:
    """Effective longest-edge cap: the user's Step-12 choice, but never above the game's hard cap
    (L4D2 crashes above 2048). 0/blank/invalid -> the game default (keeps the old output byte-identical)."""
    game_cap = max_texture_edge_for_game(game)
    try:
        chosen = int(user_edge or 0)
    except (TypeError, ValueError):
        chosen = 0
    if chosen > 0:
        return min(chosen, game_cap)
    return game_cap


GENERATE_NORMAL_MIN_EDGE = 1024
SMARTNORMAL_BIAS = 85
NORMAL_REFERENCE_INTENSITY = 85.0
NORMAL_REFERENCE_STRENGTH = 3.0

# --- PBR scheme support (manual Step 12 only; auto-porting always uses
# "legacy", which leaves every code path below byte-identical to before). ---
PBR_SCHEME_LEGACY = "legacy"
# Roles that the GUI exposes as per-material enable/disable toggles. Order is
# the display order. "normal" reuses the existing normal pipeline.
PBR_MAP_ROLES = ("normal", "roughness", "metallic", "ao", "emission")
PBR_SCHEMES: dict[str, dict[str, Any]] = {
    "unreal_wuwa": {
        "label": "Unreal (Wuthering Waves-like)",
        "base_suffixes": ("_d",),
        # role -> sibling filename suffixes (the base "_D"/"_D1" suffix is replaced).
        # Hair uses "_hn" for the normal; body/cloth carry an "_FTM" packed mask.
        "sibling_suffixes": {
            "normal": ("_n", "_hn"),
            "mask": ("_ftm",),
        },
        # role -> (sibling role that holds it, channel, invert). Channel mapping
        # verified against the HoyoToon Wuthering Waves shader + the live texture
        # set (see scmi memory): the _N alpha is the roughness/spec scalar (bright
        # = rough, no invert because the baker emits gloss = 1 - roughness); the
        # _FTM mask is R = ambient occlusion, A = a sparse emission/glow mask
        # (B ~0.5 filler and G are ignored). Wuthering Waves does NOT author a
        # metallic texture (it is shader/region-driven), so no metallic is mapped.
        # "_RGID" is a region/material-ID map (near-black, discrete) — never PBR.
        "packed_channels": {
            "roughness": ("normal", "a", False),
            "ao": ("mask", "r", False),
            "emission": ("mask", "a", False),
        },
    },
    "unity_endfield": {
        "label": "Unity (Arknights Endfield-like)",
        "base_suffixes": ("_d",),
        "sibling_suffixes": {
            "normal": ("_n", "_nro", "_hn"),
            "mask": ("_p",),
            "emission": ("_e",),
        },
        # Unity HDRP-style mask map (_P): R=metallic, G=AO, A=smoothness.
        "packed_channels": {
            "roughness": ("mask", "a", True),  # smoothness -> roughness
            "metallic": ("mask", "r", False),
            "ao": ("mask", "g", False),
        },
    },
    "moongaze_pbr": {
        "label": "MoonGaze-like PBR (packed _M / _OD maps)",
        "base_suffixes": ("_d", "_od"),
        # MoonGaze/HoyoToon exports keep the PBR maps in a SEPARATE sibling folder
        # (e.g. "extra texture") with a model-code prefix and names unrelated to the
        # PMX color texture, so the exact stem-root matcher used by the schemes above
        # cannot find them. Use fuzzy cross-folder matching keyed off the material
        # name and base-color filename instead (see fuzzy_pbr_siblings).
        "resolver": "fuzzy",
        # role -> the trailing filename token that identifies each map.
        "fuzzy_roles": {
            "mask": ("m", "mask"),
            "normal": ("n", "nrm", "norm", "normal"),
            "emission": ("e", "emissive", "emission", "glow"),
        },
        # Packed spec/material mask (_M): red = specular/phong intensity -> gloss
        # (bright = shiny = sharper highlight). No metallic/AO because the toon _M
        # channel packing varies per model and guessing wrong looks worse than
        # leaving it to the phong shader; a real _N normal (fuzzy) and _E emission
        # are picked up through the normal pipeline and the emission slot.
        "packed_channels": {
            "roughness": ("mask", "r", True),
        },
    },
}

# Fuzzy PBR matching (MoonGaze scheme): a candidate map is accepted when its
# similarity to the material name or base-color filename reaches this ratio.
# Calibrated on the Lailai model so clear matches (body1_M, UAV1_M, hair_N) land
# while unrelated masks (beibao_M for a "Package" material) are left out.
FUZZY_PBR_MATCH_THRESHOLD = 0.62
# Filename tokens that never carry material identity (toon-shader variant/junk
# tokens); they lower a candidate's priority but do not disqualify it.
FUZZY_PBR_SECONDARY_TOKENS = frozenset(
    {"leishe", "specular", "reflection", "sdf", "complexio", "zz", "ramp", "outlinemask", "od", "original"}
)


def _fuzzy_segments(stem: str) -> list[str]:
    return [s for s in re.split(r"[_\s]+", str(stem or "").strip()) if s]


def _fuzzy_trailing_role(stem: str, fuzzy_roles: dict[str, tuple[str, ...]]) -> str | None:
    segs = _fuzzy_segments(stem)
    if not segs:
        return None
    last = re.sub(r"[^a-z0-9]+", "", segs[-1].lower())
    for role, tokens in fuzzy_roles.items():
        if last in tokens:
            return role
    return None


def _fuzzy_model_prefix(files: list[Path], fuzzy_roles: dict[str, tuple[str, ...]]) -> str:
    """Dominant leading underscore-segment across the role-tagged files, so a
    shared model code ('LailaiR21M11') does not pollute name matching."""
    from collections import Counter

    leads: Counter[str] = Counter()
    tagged = [path for path in files if _fuzzy_trailing_role(path.stem, fuzzy_roles)]
    for path in tagged:
        segs = _fuzzy_segments(path.stem)
        if len(segs) >= 2:
            leads[segs[0].lower()] += 1
    if not leads:
        return ""
    token, count = leads.most_common(1)[0]
    return token if count >= max(2, int(0.5 * len(tagged))) else ""


def _fuzzy_content_segments(stem: str, model_prefix: str, fuzzy_roles: dict[str, tuple[str, ...]]) -> list[str]:
    segs = _fuzzy_segments(stem)
    if segs and model_prefix and segs[0].lower() == model_prefix:
        segs = segs[1:]
    if segs and _fuzzy_trailing_role(stem, fuzzy_roles):
        segs = segs[:-1]
    return [re.sub(r"[^a-z0-9-￿]+", "", s.lower()) for s in segs if s.strip()]


def _fuzzy_trailing_digits(value: str) -> str:
    match = re.search(r"(\d+)$", value)
    return match.group(1) if match else ""


def _fuzzy_token_score(target: str, token: str) -> float:
    from difflib import SequenceMatcher

    if not target or not token:
        return 0.0
    if target == token:
        return 1.0
    score = SequenceMatcher(None, target, token).ratio()
    if len(target) >= 3 and len(token) >= 3 and (target in token or token in target):
        score = max(score, 0.86)
    # Digit-variant guard: "body1" and "body2" are different materials even though
    # they are ~0.8 similar; a differing trailing number means a different material.
    td, kd = _fuzzy_trailing_digits(target), _fuzzy_trailing_digits(token)
    if td and kd and td != kd:
        score = min(score, 0.45)
    return score


def _fuzzy_match_score(material_name: str, base_stem: str, cand_content: list[str]) -> float:
    targets = [
        re.sub(r"[^a-z0-9-￿]+", "", str(material_name or "").lower()),
        re.sub(r"[^a-z0-9-￿]+", "", str(base_stem or "").lower()),
    ]
    identity = [tok for tok in cand_content if tok not in FUZZY_PBR_SECONDARY_TOKENS] or cand_content
    best = 0.0
    joined = "".join(identity)
    for target in targets:
        if not target:
            continue
        for token in identity:
            best = max(best, _fuzzy_token_score(target, token))
        best = max(best, _fuzzy_token_score(target, joined))
    return best


def fuzzy_pbr_siblings(
    spec: dict[str, Any],
    base_path: Path | None,
    workspace_root: Path,
    material_name: str,
    base_color_file: str,
) -> dict[str, Path]:
    """Fuzzy cross-folder PBR map resolution for the MoonGaze scheme.

    Reuses the recursive source-asset walk (so maps in a sibling 'extra texture'
    folder are reachable) but matches each role's trailing-suffix files
    (``*_M`` / ``*_N`` / ``*_E``) to the material by name similarity rather than
    an exact shared stem root.
    """
    fuzzy_roles = {str(role): tuple(tokens) for role, tokens in spec.get("fuzzy_roles", {}).items()}
    if not fuzzy_roles:
        return {}
    search_dirs = collect_search_dirs(base_path, workspace_root) if base_path else []
    search_dirs = search_dirs + source_texture_search_dirs(base_path, workspace_root)
    files = collect_image_files(search_dirs)
    if not files:
        return {}
    model_prefix = _fuzzy_model_prefix(files, fuzzy_roles)
    base_stem = base_path.stem if base_path else Path(str(base_color_file or "")).stem
    base_resolved = base_path.resolve() if base_path else None

    out: dict[str, Path] = {}
    for role in fuzzy_roles:
        scored: list[tuple[float, int, Path]] = []
        for path in files:
            if _fuzzy_trailing_role(path.stem, fuzzy_roles) != role:
                continue
            if base_resolved is not None and path.resolve() == base_resolved:
                continue
            content = _fuzzy_content_segments(path.stem, model_prefix, fuzzy_roles)
            if not content:
                continue
            score = _fuzzy_match_score(material_name, base_stem, content)
            extra = len(content) - 1 + sum(1 for tok in content if tok in FUZZY_PBR_SECONDARY_TOKENS)
            scored.append((score, -extra, path))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if scored and scored[0][0] >= FUZZY_PBR_MATCH_THRESHOLD:
            out[role] = scored[0][2]
    return out


def normalize_scheme(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in PBR_SCHEMES else PBR_SCHEME_LEGACY


def channel_index(channel: str) -> int:
    return {"r": 0, "g": 1, "b": 2, "a": 3}.get(str(channel or "").lower(), 0)


def load_rgba_array(path: Path) -> np.ndarray:
    image = open_image(path).convert("RGBA")
    image, _ = resize_to_limit(image)
    return np.asarray(image).astype(np.float32) / 255.0


def resize_channel_to(channel: np.ndarray, height: int, width: int) -> np.ndarray:
    if channel.shape[:2] == (height, width):
        return channel
    img = Image.fromarray(np.clip(channel * 255.0, 0, 255).astype(np.uint8), mode="L")
    img = img.resize((width, height), Image.Resampling.LANCZOS)
    return np.asarray(img).astype(np.float32) / 255.0


def base_stem_root(stem: str, base_suffixes: tuple[str, ...]) -> str:
    low = stem.lower()
    for suffix in base_suffixes:
        # Match the base suffix optionally followed by a numeric variant, e.g.
        # "_D", "_D1", "_D2". Wuthering Waves ships both "_D" and "_D1" diffuse
        # variants and the model usually samples "_D1"; sibling maps (_N, _FTM,
        # …) drop the digit, so the shared stem root must strip it too.
        match = re.search(re.escape(suffix) + r"\d*$", low)
        if match:
            return stem[: match.start()]
    return stem


def find_sibling_texture(
    base_path: Path,
    base_suffixes: tuple[str, ...],
    target_suffixes: tuple[str, ...],
    workspace_root: Path,
) -> Path | None:
    """Find a sibling texture sharing the base stem but with a role suffix.

    e.g. base ``T_..Cloth_D`` + target ``_n`` -> ``T_..Cloth_N`` in the same
    asset folders. Exact stem match only, so ``Toe_Finger``-style coincidences
    cannot leak across materials.
    """
    root = base_stem_root(base_path.stem, base_suffixes).lower()
    if not root:
        return None
    search_dirs = collect_search_dirs(base_path, workspace_root) + source_texture_search_dirs(base_path, workspace_root)
    files = collect_image_files(search_dirs)
    by_stem: dict[str, Path] = {}
    for path in files:
        by_stem.setdefault(path.stem.lower(), path)
    for suffix in target_suffixes:
        candidate = by_stem.get(root + suffix)
        if candidate is not None and candidate.resolve() != base_path.resolve():
            return candidate
    return None


def detect_pbr_maps(
    base_path: Path | None,
    workspace_root: Path,
    scheme: str,
    material_name: str = "",
    base_color_file: str = "",
) -> dict[str, Any]:
    """Detect scheme-specific PBR sibling/packed maps for one material.

    Returns ``{}`` for the legacy scheme or when no base texture exists, so the
    auto-port path never grows a ``pbr`` block. Every detected map defaults to
    ``enabled: False`` to match current auto-port output exactly.
    """
    scheme = normalize_scheme(scheme)
    spec = PBR_SCHEMES.get(scheme)
    if not spec or base_path is None:
        return {}
    base_suffixes = tuple(spec.get("base_suffixes", ("_d",)))
    if str(spec.get("resolver") or "exact") == "fuzzy":
        # MoonGaze: maps live in a separate folder with unrelated names, matched
        # by fuzzy name similarity to the material rather than a shared stem root.
        sibling_paths = fuzzy_pbr_siblings(spec, base_path, workspace_root, material_name, base_color_file)
    else:
        sibling_paths = {}
        for role, suffixes in spec.get("sibling_suffixes", {}).items():
            found = find_sibling_texture(base_path, base_suffixes, tuple(suffixes), workspace_root)
            if found is not None:
                sibling_paths[role] = found

    maps: dict[str, dict[str, Any]] = {}
    if "normal" in sibling_paths:
        npath = sibling_paths["normal"]
        try:
            ntype, _stats, _w = classify_normal_map(npath)
        except Exception:
            ntype = "ambiguous"
        kind = "convert_ue_rg" if ntype == "ue_rg" else ("copy_blue" if ntype == "blue" else "ambiguous_copy")
        maps["normal"] = {"available": True, "source": str(npath), "kind": kind, "enabled": False}

    for role, packed in spec.get("packed_channels", {}).items():
        try:
            src_role, channel, invert = packed
        except Exception:
            continue
        src = sibling_paths.get(src_role)
        if src is not None and src.exists():
            maps[role] = {
                "available": True,
                "source": str(src),
                "channel": channel,
                "invert": bool(invert),
                "enabled": False,
            }

    if "emission" in sibling_paths:
        maps["emission"] = {"available": True, "source": str(sibling_paths["emission"]), "enabled": False}

    return {"scheme": scheme, "maps": maps}


def build_phong_exponent_png(
    output_path: Path,
    roughness_source: Path | None,
    roughness_channel: str,
    roughness_invert: bool,
    metallic_source: Path | None,
    metallic_channel: str,
) -> dict[str, Any]:
    """Write a Source $phongexponenttexture from PBR inputs.

    Red channel = per-texel gloss (specular exponent map) derived from
    roughness/smoothness; alpha channel = metallic, which drives
    $phongalbedotint so metallic areas tint specular toward the albedo. This is
    the phong-only metallic approximation (no $envmap).
    """
    gloss: np.ndarray | None = None
    if roughness_source is not None and roughness_source.exists():
        arr = load_rgba_array(roughness_source)
        channel = arr[..., channel_index(roughness_channel)]
        gloss = channel if roughness_invert else (1.0 - channel)
    metallic: np.ndarray | None = None
    if metallic_source is not None and metallic_source.exists():
        arr = load_rgba_array(metallic_source)
        metallic = arr[..., channel_index(metallic_channel)]
    if gloss is None and metallic is None:
        return {}
    if gloss is None:
        gloss = np.full(metallic.shape[:2], 0.5, dtype=np.float32)
    if metallic is None:
        metallic = np.zeros(gloss.shape[:2], dtype=np.float32)
    metallic = resize_channel_to(metallic, gloss.shape[0], gloss.shape[1])
    red = np.clip(gloss, 0.0, 1.0)
    alpha = np.clip(metallic, 0.0, 1.0)
    out = np.stack([red, red, red, alpha], axis=-1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(out * 255.0, 0, 255).astype(np.uint8), mode="RGBA").save(output_path)
    return {"gloss_mean": round(float(gloss.mean()), 4), "metallic_mean": round(float(metallic.mean()), 4)}


def bake_ao_into_base(base_output_path: Path, ao_source: Path, ao_channel: str) -> float:
    """Multiply an ambient-occlusion channel into an already-written base PNG."""
    base_image = open_image(base_output_path).convert("RGBA")
    base = np.asarray(base_image).astype(np.float32) / 255.0
    ao_arr = load_rgba_array(ao_source)
    ao = resize_channel_to(ao_arr[..., channel_index(ao_channel)], base.shape[0], base.shape[1])
    base[..., 0] *= ao
    base[..., 1] *= ao
    base[..., 2] *= ao
    Image.fromarray(np.clip(base * 255.0, 0, 255).astype(np.uint8), mode="RGBA").save(base_output_path)
    return round(float(ao.mean()), 4)


def build_selfillum_png(emission_source: Path, output_path: Path) -> tuple[float, bool]:
    """Write a self-illumination mask PNG from an emission texture.

    Returns (mean_luminance, is_emissive). Black emission maps produce
    is_emissive=False so callers can skip writing a pointless VTF/VMT flag.
    """
    arr = load_rgba_array(emission_source)
    rgb = arr[..., :3]
    luminance = rgb.max(axis=-1)
    mean_luminance = float(luminance.mean())
    out = np.stack([rgb[..., 0], rgb[..., 1], rgb[..., 2], luminance], axis=-1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(out * 255.0, 0, 255).astype(np.uint8), mode="RGBA").save(output_path)
    return round(mean_luminance, 4), mean_luminance >= 0.02


def build_selfillum_mask_from_channel(source: Path, channel: str, output_path: Path) -> tuple[float, bool]:
    """Write a $selfillummask from one packed channel (e.g. the Wuthering Waves
    _FTM alpha glow mask). White = emissive; the glow colour comes from the base
    texture in-game. Returns (mean, is_emissive); a near-black mask reports
    is_emissive=False so callers skip a pointless self-illum flag."""
    arr = load_rgba_array(source)
    mask = arr[..., channel_index(channel)]
    mean_value = float(mask.mean())
    out = np.stack([mask, mask, mask, mask], axis=-1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(out * 255.0, 0, 255).astype(np.uint8), mode="RGBA").save(output_path)
    return round(mean_value, 4), mean_value >= 0.004


@dataclass
class MaterialTexture:
    uid: str
    material_name: str
    output_name: str
    base_source_path: str
    base_color_file: str = ""
    vertex_count: int = 0
    face_count: int = 0
    warnings: list[str] = field(default_factory=list)


def emit(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write((message + "\n").encode(encoding, errors="replace"))
        sys.stdout.flush()


def as_path(value: str | os.PathLike[str] | None) -> Path | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("//"):
        text = text[2:]
    return Path(text).expanduser()


def decode_literal_unicode_escapes(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(1)
        try:
            return chr(int(raw, 16))
        except Exception:
            return match.group(0)

    return re.sub(r"\\u([0-9a-fA-F]{4})", repl, text)


def safe_basename(name: str, fallback: str, used: set[str]) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "", str(name or ""))
    if not cleaned or cleaned in used:
        cleaned = fallback
    base = cleaned
    suffix = 2
    while cleaned in used:
        cleaned = f"{base}_{suffix:02d}"
        suffix += 1
    used.add(cleaned)
    return cleaned


def resolve_material_mapping(input_path: Path) -> tuple[Path | None, Path | None, Path, Path]:
    """Return (materials_npy, materials_json, material_dir, workspace_root)."""
    path = input_path.resolve()
    candidates: list[Path] = []
    if path.is_file():
        if path.name.lower() == "materials.npy":
            candidates.append(path)
        elif path.name.lower() == "materials.json":
            sibling = path.with_name("materials.npy")
            if sibling.exists():
                candidates.append(sibling)
    elif path.is_dir():
        direct = path / "materials.npy"
        step_dir = path / "5_sort_materials" / "materials.npy"
        if direct.exists():
            candidates.append(direct)
        if step_dir.exists():
            candidates.append(step_dir)
        if not candidates:
            found = list(path.rglob("materials.npy"))
            found = [item for item in found if item.parent.name == "5_sort_materials"]
            candidates.extend(sorted(found, key=lambda item: item.stat().st_mtime, reverse=True))
    if not candidates:
        # Step 5's .npy write is best-effort, so accept a materials.json found at
        # the same locations when the sibling .npy is absent.
        json_candidates: list[Path] = []
        if path.is_file() and path.name.lower() == "materials.json":
            json_candidates.append(path)
        elif path.is_dir():
            direct_json = path / "materials.json"
            step_json = path / "5_sort_materials" / "materials.json"
            if direct_json.exists():
                json_candidates.append(direct_json)
            if step_json.exists():
                json_candidates.append(step_json)
            if not json_candidates:
                found_json = [item for item in path.rglob("materials.json") if item.parent.name == "5_sort_materials"]
                json_candidates.extend(sorted(found_json, key=lambda item: item.stat().st_mtime, reverse=True))
        if json_candidates:
            json_path = json_candidates[0].resolve()
            material_dir = json_path.parent
            workspace_root = material_dir.parent if material_dir.name == "5_sort_materials" else material_dir
            return None, json_path, material_dir, workspace_root
        raise FileNotFoundError(f"Could not locate Step 5 materials.npy or materials.json from {input_path}")
    npy_path = candidates[0].resolve()
    json_path = npy_path.with_name("materials.json")
    material_dir = npy_path.parent
    workspace_root = material_dir.parent if material_dir.name == "5_sort_materials" else material_dir
    return npy_path, (json_path if json_path.exists() else None), material_dir, workspace_root


def read_materials_json(json_path: Path, used_names: set[str]) -> list[MaterialTexture]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    rows = data.get("final_materials") or data.get("materials") or data.get("source_materials") or []
    materials: list[MaterialTexture] = []
    for index, entry in enumerate(rows, start=1):
        if not isinstance(entry, dict):
            continue
        if entry.get("keep") is False:
            continue
        base_path = str(entry.get("base_color_path") or "").strip()
        if not base_path:
            continue
        material_name = str(entry.get("proposed_name") or entry.get("material_name") or entry.get("final_name") or f"material_{index:03d}")
        output_name = safe_basename(material_name, f"mat_{index:03d}", used_names)
        warnings = [str(item) for item in entry.get("warnings", []) if item] if isinstance(entry.get("warnings"), list) else []
        materials.append(
            MaterialTexture(
                uid=str(entry.get("uid") or f"material_{index:03d}"),
                material_name=material_name,
                output_name=output_name,
                base_source_path=base_path,
                base_color_file=str(entry.get("base_color_file") or Path(base_path).name),
                vertex_count=int(entry.get("vertex_count", 0) or 0),
                face_count=int(entry.get("face_count", 0) or 0),
                warnings=warnings,
            )
        )
    return materials


def read_materials_npy(npy_path: Path, used_names: set[str]) -> list[MaterialTexture]:
    data = np.load(npy_path, allow_pickle=True)
    materials: list[MaterialTexture] = []
    for index, row in enumerate(data.tolist(), start=1):
        try:
            raw_name = str(row[0] or "").strip()
            raw_path = str(row[1] or "").strip()
        except Exception:
            continue
        if not raw_path:
            continue
        material_name = raw_name or Path(raw_path).stem
        material_name = re.sub(r"(_D|_Diffuse|_BaseColor)$", "", material_name, flags=re.IGNORECASE)
        output_name = safe_basename(material_name, f"mat_{index:03d}", used_names)
        materials.append(
            MaterialTexture(
                uid=f"material_{index:03d}",
                material_name=material_name,
                output_name=output_name,
                base_source_path=raw_path,
                base_color_file=Path(raw_path).name,
            )
        )
    return materials


def load_material_entries(input_path: Path) -> tuple[list[MaterialTexture], Path, Path, Path | None]:
    npy_path, json_path, material_dir, workspace_root = resolve_material_mapping(input_path)
    used_names: set[str] = set()
    materials = read_materials_json(json_path, used_names) if json_path else []
    if not materials and npy_path:
        materials = read_materials_npy(npy_path, used_names)
    materials.extend(load_imported_texture_entries(workspace_root, used_names))
    return materials, workspace_root, material_dir, json_path


def load_imported_texture_entries(workspace_root: Path, used_names: set[str]) -> list[MaterialTexture]:
    """Imported skin textures defined in the Step-5 texture-group editor.

    They live in `<workspace>/texture_groups/texture_groups.json` under `imports`
    and are processed here as ordinary materials so each gets a base PNG (and a
    VMT/VTF in Step 14), letting `$texturegroup skinfamilies` reference them."""
    path = workspace_root / "texture_groups" / "texture_groups.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    imports = data.get("imports") if isinstance(data, dict) else None
    if not isinstance(imports, list):
        return []
    entries: list[MaterialTexture] = []
    for index, entry in enumerate(imports, start=1):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        stored = str(entry.get("path") or "").strip()
        if not name or not stored:
            continue
        stored_path = Path(stored)
        if not stored_path.is_absolute():
            stored_path = workspace_root / stored_path
        warnings = [] if stored_path.exists() else [f"Imported skin texture is missing: {stored_path}"]
        entries.append(
            MaterialTexture(
                uid=f"skin_import_{index:03d}",
                material_name=name,
                output_name=safe_basename(name, f"skin_{index:03d}", used_names),
                base_source_path=str(stored_path),
                base_color_file=stored_path.name,
                warnings=warnings,
            )
        )
    return entries


def open_image(path: Path) -> Image.Image:
    image = Image.open(path)
    image.load()
    return ImageOps.exif_transpose(image)


def image_size(path: Path) -> tuple[int, int]:
    with open_image(path) as image:
        return int(image.width), int(image.height)


def resize_to_limit(image: Image.Image, max_edge: int | None = None) -> tuple[Image.Image, bool]:
    if max_edge is None:
        max_edge = _ACTIVE_MAX_TEXTURE_EDGE
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image, False
    scale = max_edge / float(longest)
    size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    return image.resize(size, Image.Resampling.LANCZOS), True


def cap_pngs_to_limit(dirs: list[Path], max_edge: int) -> list[dict[str, Any]]:
    """Downscale any already-written PNG whose longest edge exceeds max_edge.

    Catches the derived maps (phong-exponent, selfillum, generated/converted normals) that are
    written at their source resolution rather than through resize_to_limit. Base/normal PNGs are
    already clamped at write time, so they are skipped here.
    """
    capped: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for directory in dirs:
        if not directory or not directory.exists():
            continue
        for path in sorted(directory.glob("*.png")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                with Image.open(path) as probe:
                    width, height = probe.size
                    if max(width, height) <= max_edge:
                        continue
                    converted = probe.convert("RGBA")
                resized, _ = resize_to_limit(converted, max_edge)
                resized.save(path)
                capped.append({"path": str(path), "from": [width, height], "to": list(resized.size)})
            except Exception as exc:
                emit(f"[Step12 Textures] WARNING: could not clamp {path.name} to {max_edge}px: {exc}")
    return capped


def normalized_stem(stem: str, suffixes: tuple[str, ...]) -> str:
    value = stem.lower()
    value = re.sub(r"[\s\-]+", "_", value)
    for suffix in suffixes:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return re.sub(r"[^a-z0-9]+", "", value)


def has_normal_hint(path: Path) -> bool:
    stem = f"_{path.stem.lower()}_"
    return any(hint in stem for hint in NORMAL_HINTS)


def normal_candidate_score(base_path: Path, candidate: Path) -> float:
    if candidate.resolve() == base_path.resolve():
        return -1.0
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        return -1.0
    if not has_normal_hint(candidate):
        return -1.0
    base_root = normalized_stem(base_path.stem, BASE_SUFFIXES)
    cand_root = normalized_stem(candidate.stem, NORMAL_SUFFIXES)
    score = 0.0
    if base_root == cand_root:
        score += 100.0
    elif base_root and cand_root and (base_root in cand_root or cand_root in base_root):
        score += 55.0
    else:
        return -1.0
    lower = candidate.stem.lower()
    if lower.endswith("_n"):
        score += 20.0
    if lower.endswith("_normal") or lower.endswith("_norm"):
        score += 16.0
    if "_hn" in lower:
        score += 10.0
    distance = 0 if candidate.parent == base_path.parent else len(candidate.parts)
    score -= min(20.0, distance * 0.25)
    return score


def collect_search_dirs(base_path: Path, workspace_root: Path) -> list[Path]:
    dirs: list[Path] = []
    for item in (
        base_path.parent,
        base_path.parent / "textures",
        base_path.parent.parent / "textures" if base_path.parent.parent else None,
        workspace_root / "0_source_mmd_assets",
        workspace_root / "0_source_mmd_assets" / "textures",
        workspace_root / "0_source_mmd_assets" / "other tex",
    ):
        if item and item.exists() and item.is_dir() and item not in dirs:
            dirs.append(item)
    return dirs


# Per-run caches: the source asset tree can hold thousands of files and the
# material loops would otherwise re-walk it once per material.
_IMAGE_FILE_CACHE: dict[tuple[str, bool], list[Path]] = {}
_TEXTURE_FILE_KEY_CACHE: dict[str, set[str]] = {}


def iter_image_files(folder: Path, recursive: bool) -> list[Path]:
    cache_key = (str(folder.resolve()).lower(), recursive)
    cached = _IMAGE_FILE_CACHE.get(cache_key)
    if cached is None:
        iterator = folder.rglob("*") if recursive else folder.glob("*")
        cached = [
            path.resolve()
            for path in iterator
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        _IMAGE_FILE_CACHE[cache_key] = cached
    return cached


def collect_image_files(search_dirs: list[Path]) -> list[Path]:
    seen: set[str] = set()
    files: list[Path] = []
    for folder in search_dirs:
        for path in iter_image_files(folder, recursive=folder.name == "0_source_mmd_assets"):
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return files


def texture_name_keys(path_or_name: str | Path) -> set[str]:
    text = decode_literal_unicode_escapes(str(path_or_name or "")).strip()
    if not text:
        return set()
    name = Path(text).name
    stem = Path(name).stem
    values = {name, stem}
    if "\ufffd" in name:
        values.add(name.replace("\ufffd", ""))
        values.add(stem.replace("\ufffd", ""))
    keys: set[str] = set()
    for value in values:
        folded = value.casefold()
        keys.add(folded)
        keys.add(re.sub(r"\.[^.]+$", "", folded))
        keys.add(re.sub(r"[^a-z0-9\u0080-\uffff]+", "", folded))
    return {key for key in keys if key}


def texture_keys_for_file(path: Path) -> set[str]:
    cache_key = str(path)
    cached = _TEXTURE_FILE_KEY_CACHE.get(cache_key)
    if cached is None:
        cached = texture_name_keys(path.name)
        _TEXTURE_FILE_KEY_CACHE[cache_key] = cached
    return cached


def source_texture_search_dirs(base_path: Path | None, workspace_root: Path) -> list[Path]:
    dirs: list[Path] = []
    for item in (
        base_path.parent if base_path else None,
        workspace_root / "0_source_mmd_assets" / "tex",
        workspace_root / "0_source_mmd_assets" / "textures",
        workspace_root / "0_source_mmd_assets" / "other tex",
        workspace_root / "0_source_mmd_assets",
    ):
        if item and item.exists() and item.is_dir() and item not in dirs:
            dirs.append(item)
    return dirs


def resolve_base_texture_path(
    raw_path: str | Path | None,
    workspace_root: Path,
    base_color_file: str = "",
) -> tuple[Path | None, list[str]]:
    warnings: list[str] = []
    base_path = as_path(raw_path)
    if base_path and base_path.exists():
        return base_path.resolve(), warnings
    decoded_path = as_path(decode_literal_unicode_escapes(str(raw_path or "")))
    if decoded_path and decoded_path.exists():
        warnings.append(f"Resolved escaped texture path to {decoded_path.name}.")
        return decoded_path.resolve(), warnings

    lookup_keys = set()
    if base_path:
        lookup_keys.update(texture_name_keys(base_path.name))
    if base_color_file:
        lookup_keys.update(texture_name_keys(base_color_file))
    if decoded_path:
        lookup_keys.update(texture_name_keys(decoded_path.name))
    if not lookup_keys:
        return base_path, warnings

    candidates: list[Path] = []
    for folder in source_texture_search_dirs(base_path or decoded_path, workspace_root):
        for path in iter_image_files(folder, recursive=folder.name == "0_source_mmd_assets"):
            keys = texture_keys_for_file(path)
            if lookup_keys.intersection(keys):
                candidates.append(path)
    if not candidates:
        return base_path or decoded_path, warnings

    def score(candidate: Path) -> tuple[int, int, str]:
        suffix_rank = IMAGE_EXTENSION_PRIORITY.index(candidate.suffix.lower()) if candidate.suffix.lower() in IMAGE_EXTENSION_PRIORITY else 99
        same_parent = 0 if base_path and candidate.parent == base_path.parent else 1
        return same_parent, suffix_rank, str(candidate).casefold()

    chosen: Path | None = None
    for candidate in sorted(set(candidates), key=score):
        if candidate.suffix.lower() == ".dds":
            # Pillow handles common DXT1/3/5 DDS but not every exotic sub-format.
            try:
                with Image.open(candidate) as probe:
                    probe.load()
            except Exception:
                warnings.append(f"Skipped unreadable DDS candidate {candidate.name}.")
                continue
        chosen = candidate
        break
    if chosen is None:
        return base_path or decoded_path, warnings
    missing_name = Path(str(raw_path or "")).name
    warnings.append(f"Texture path {missing_name or raw_path} was missing; resolved to {chosen.name}.")
    return chosen, warnings


def find_normal_candidate(base_path: Path, workspace_root: Path) -> tuple[Path | None, float, list[str]]:
    warnings: list[str] = []
    files = collect_image_files(collect_search_dirs(base_path, workspace_root))
    scored = [(normal_candidate_score(base_path, candidate), candidate) for candidate in files]
    scored = [(score, candidate) for score, candidate in scored if score >= 20.0]
    scored.sort(key=lambda item: (-item[0], str(item[1]).lower()))
    if not scored:
        return None, 0.0, warnings
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 5.0:
        warnings.append(f"Ambiguous normal candidates; selected {scored[0][1].name}.")
    return scored[0][1], scored[0][0], warnings


def classify_normal_map(path: Path) -> tuple[str, dict[str, float], list[str]]:
    warnings: list[str] = []
    with open_image(path).convert("RGBA") as image:
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        arr = np.asarray(image).astype(np.float32) / 255.0
    rgb = arr[..., :3]
    means = rgb.reshape(-1, 3).mean(axis=0)
    stds = rgb.reshape(-1, 3).std(axis=0)
    r, g, b = [float(v) for v in means]
    sr, sg, sb = [float(v) for v in stds]
    stats = {
        "mean_r": r,
        "mean_g": g,
        "mean_b": b,
        "std_r": sr,
        "std_g": sg,
        "std_b": sb,
    }
    if b > 0.60 and b > r + 0.08 and b > g + 0.08:
        return "blue", stats, warnings
    if r > 0.42 and g > 0.42 and b < 0.62 and (sr > 0.025 or sg > 0.025):
        return "ue_rg", stats, warnings
    if b < 0.55 and r > b + 0.05 and g > b + 0.05:
        return "ue_rg", stats, warnings
    warnings.append("Normal color statistics were ambiguous.")
    return "ambiguous", stats, warnings


def normal_tone_report(path: Path) -> tuple[bool, dict[str, float], list[str]]:
    warnings: list[str] = []
    with open_image(path).convert("RGBA") as image:
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        arr = np.asarray(image).astype(np.float32) / 255.0
    rgb = arr[..., :3]
    means = rgb.reshape(-1, 3).mean(axis=0)
    stds = rgb.reshape(-1, 3).std(axis=0)
    r, g, b = [float(v) for v in means]
    sr, sg, sb = [float(v) for v in stds]
    luminance = float(rgb.mean())
    chroma_spread = max(r, g, b) - min(r, g, b)
    stats = {
        "mean_r": r,
        "mean_g": g,
        "mean_b": b,
        "std_r": sr,
        "std_g": sg,
        "std_b": sb,
        "mean_luminance": luminance,
        "chroma_spread": chroma_spread,
    }
    if b < 0.58:
        warnings.append("Normal preview is not blue/purple enough; blue channel is too low.")
    if b < max(r, g) + 0.045:
        warnings.append("Normal preview is not blue/purple enough; red/green dominate the blue channel.")
    if luminance < 0.28:
        warnings.append("Normal preview is too dark.")
    if r > 0.82 and g > 0.82 and b > 0.82 and max(sr, sg, sb) < 0.08:
        warnings.append("Normal preview is too white/flat.")
    if r > b + 0.04 and g > b - 0.02:
        warnings.append("Normal preview is brown/yellow rather than blue/purple.")
    return not warnings, stats, warnings


def validate_or_regenerate_normal(output_path: Path, base_source: Path, bias: int, row_report: dict[str, Any], context: str) -> None:
    ok, stats, tone_warnings = normal_tone_report(output_path)
    row_report["normal_tone_stats"] = stats
    row_report["normal_tone_ok"] = ok
    if ok:
        return
    row_report["warnings"].append(
        f"{context} normal did not pass blue/purple tone validation; regenerated from base texture."
    )
    row_report["warnings"].extend(tone_warnings)
    generate_normal_from_base(base_source, output_path, bias)
    ok_after, stats_after, warnings_after = normal_tone_report(output_path)
    row_report["normal_tone_stats"] = stats_after
    row_report["normal_tone_ok"] = ok_after
    row_report["normal_regenerated_for_tone"] = True
    row_report["generate_normal"] = True
    if not ok_after:
        row_report["warnings"].append("Generated normal still did not pass blue/purple tone validation.")
        row_report["warnings"].extend(warnings_after)
        raise RuntimeError("Generated normal failed blue/purple tone validation.")


def convert_ue_rg_normal_to_rgb(input_path: Path, output_path: Path) -> None:
    img = open_image(input_path).convert("RGBA")
    img, _ = resize_to_limit(img)
    arr = np.asarray(img).astype(np.float32) / 255.0
    r = arr[..., 0]
    g = arr[..., 1]
    a = arr[..., 3]
    nx = r * 2.0 - 1.0
    ny = g * 2.0 - 1.0
    nz_sq = np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0)
    nz = np.sqrt(nz_sq)
    out = np.stack([nx * 0.5 + 0.5, ny * 0.5 + 0.5, nz * 0.5 + 0.5, a], axis=-1)
    Image.fromarray(np.clip(out * 255.0, 0, 255).astype(np.uint8), mode="RGBA").save(output_path)


def normal_generation_strength(intensity: int | float) -> float:
    try:
        value = float(intensity)
    except Exception:
        value = float(SMARTNORMAL_BIAS)
    value = max(0.0, min(255.0, value))
    return value * (NORMAL_REFERENCE_STRENGTH / NORMAL_REFERENCE_INTENSITY)


def generate_normal_from_base(input_path: Path, output_path: Path, bias: int = SMARTNORMAL_BIAS) -> float:
    image = open_image(input_path).convert("RGBA")
    image, _ = resize_to_limit(image)
    arr = np.asarray(image).astype(np.float32) / 255.0
    luminance = arr[..., 0] * 0.2126 + arr[..., 1] * 0.7152 + arr[..., 2] * 0.0722
    padded = np.pad(luminance, 1, mode="edge")
    dx = padded[1:-1, 2:] - padded[1:-1, :-2]
    dy = padded[2:, 1:-1] - padded[:-2, 1:-1]
    strength = normal_generation_strength(bias)
    nx = -dx * strength
    ny = dy * strength
    nz = np.ones_like(nx)
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    nx /= length
    ny /= length
    nz /= length
    out = np.stack([nx * 0.5 + 0.5, ny * 0.5 + 0.5, nz * 0.5 + 0.5, arr[..., 3]], axis=-1)
    Image.fromarray(np.clip(out * 255.0, 0, 255).astype(np.uint8), mode="RGBA").save(output_path)
    return strength


def save_base_png(input_path: Path, output_path: Path) -> tuple[tuple[int, int], tuple[int, int], bool]:
    image = open_image(input_path).convert("RGBA")
    original_size = image.size
    image, resized = resize_to_limit(image)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return original_size, image.size, resized


def save_normal_png(input_path: Path, output_path: Path) -> tuple[tuple[int, int], tuple[int, int], bool]:
    image = open_image(input_path).convert("RGBA")
    original_size = image.size
    image, resized = resize_to_limit(image)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return original_size, image.size, resized


# Neutral placeholder for a genuinely missing/unreadable base texture. Flat mid
# grey (opaque) so the material renders as a plain surface instead of the in-game
# missing-texture checkerboard, keeping the model usable while the report flags
# the material for the user to fix. Kept small since it carries no real detail.
PLACEHOLDER_BASE_EDGE = 64
PLACEHOLDER_BASE_RGBA = (128, 128, 128, 255)


def write_placeholder_base_png(output_path: Path, size: tuple[int, int] | None = None) -> tuple[int, int]:
    width = height = PLACEHOLDER_BASE_EDGE
    if size and size[0] > 0 and size[1] > 0:
        longest = max(size)
        cap = min(256, _ACTIVE_MAX_TEXTURE_EDGE)
        scale = min(1.0, cap / float(longest)) if longest > 0 else 1.0
        width = max(4, int(round(size[0] * scale)))
        height = max(4, int(round(size[1] * scale)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (width, height), PLACEHOLDER_BASE_RGBA).save(output_path)
    return width, height


def analyze_textures(input_path: Path, analysis_json: Path, plan_json: Path, scheme: str = PBR_SCHEME_LEGACY, game: str = "gmod", max_texture_edge: int = 0) -> dict[str, Any]:
    global _ACTIVE_MAX_TEXTURE_EDGE
    _ACTIVE_MAX_TEXTURE_EDGE = resolve_max_texture_edge(game, max_texture_edge)
    scheme = normalize_scheme(scheme)
    materials, workspace_root, material_dir, materials_json = load_material_entries(input_path)
    output_dir = workspace_root / "12_param_texture_render_materials"
    png_dir = output_dir / "png"
    normal_dir = output_dir / "png_normals"
    phongexp_dir = output_dir / "png_phongexp"
    selfillum_dir = output_dir / "png_selfillum"
    rows: list[dict[str, Any]] = []
    emit(f"[Step12 Textures] Loaded {len(materials)} material mappings (scheme={scheme}).")
    for index, material in enumerate(materials, start=1):
        warnings = list(material.warnings)
        base_path, resolved_warnings = resolve_base_texture_path(material.base_source_path, workspace_root, material.base_color_file)
        warnings.extend(resolved_warnings)
        base_exists = bool(base_path and base_path.exists())
        base_size = [0, 0]
        output_size = [0, 0]
        base_action = "missing"
        if base_exists and base_path is not None:
            try:
                width, height = image_size(base_path)
                base_size = [width, height]
                longest = max(width, height)
                if longest > _ACTIVE_MAX_TEXTURE_EDGE:
                    scale = _ACTIVE_MAX_TEXTURE_EDGE / float(longest)
                    output_size = [max(1, int(round(width * scale))), max(1, int(round(height * scale)))]
                    base_action = "convert_downscale"
                    warnings.append(f"Base texture will be clamped to {_ACTIVE_MAX_TEXTURE_EDGE}px max edge.")
                else:
                    output_size = [width, height]
                    base_action = "copy_png" if base_path.suffix.lower() == ".png" else "convert_png"
            except Exception as exc:
                warnings.append(f"Could not inspect base texture: {exc}")
        else:
            warnings.append("Base texture source file is missing.")

        normal_source: Path | None = None
        normal_score = 0.0
        normal_type = "missing"
        normal_action = "skip_missing"
        normal_stats: dict[str, float] = {}
        if base_exists and base_path is not None:
            normal_source, normal_score, normal_warnings = find_normal_candidate(base_path, workspace_root)
            warnings.extend(normal_warnings)
            if normal_source:
                try:
                    normal_type, normal_stats, normal_warnings = classify_normal_map(normal_source)
                    warnings.extend(normal_warnings)
                    if normal_type == "ue_rg":
                        normal_action = "convert_ue_rg"
                    elif normal_type == "blue":
                        normal_action = "copy_blue"
                    else:
                        normal_action = "ambiguous_copy"
                        warnings.append("Normal candidate is ambiguous; direct PNG conversion is available if enabled.")
                    warnings.append("Normal candidate found but disabled by default to reduce addon size.")
                except Exception as exc:
                    normal_type = "unreadable"
                    normal_action = "skip_missing"
                    warnings.append(f"Could not inspect normal candidate: {exc}")
            elif max(base_size or [0, 0]) >= GENERATE_NORMAL_MIN_EDGE:
                normal_type = "generatable"
                normal_action = "skip_generate"
                warnings.append(
                    "No normal map found; normal generation is available but disabled by default to reduce addon size."
                )
            else:
                normal_type = "missing"
                normal_action = "skip_small"
                warnings.append("No normal map found; base texture is below the normal generation threshold.")

        base_output = png_dir / f"{material.output_name}.png"
        normal_output = normal_dir / f"{material.output_name}_n.png"
        row = {
            "uid": material.uid,
            "material_name": material.material_name,
            "output_name": material.output_name,
            "enabled": True,
            "base_source_path": str(base_path) if base_path else "",
            "base_source_path_original": material.base_source_path,
            "base_source_exists": base_exists,
            "base_color_file": material.base_color_file,
            "base_output_path": str(base_output),
            "base_action": base_action,
            "base_size": base_size,
            "output_size": output_size,
            "normal_source_path": str(normal_source) if normal_source else "",
            "normal_output_path": str(normal_output),
            "normal_action": "disabled",
            "normal_action_default": normal_action,
            "normal_type": normal_type,
            "normal_candidate_score": round(float(normal_score), 3),
            "normal_stats": normal_stats,
            "use_normal": False,
            "generate_normal": False,
            "force_generate_normal": False,
            "normal_intensity": SMARTNORMAL_BIAS,
            "normal_bias": SMARTNORMAL_BIAS,
            "vertex_count": material.vertex_count,
            "face_count": material.face_count,
            "warnings": warnings,
        }
        if scheme != PBR_SCHEME_LEGACY:
            pbr = detect_pbr_maps(
                base_path if base_exists else None,
                workspace_root,
                scheme,
                material_name=material.material_name,
                base_color_file=material.base_color_file,
            )
            if pbr:
                row["pbr"] = pbr
                # Fuzzy schemes (MoonGaze) can resolve a normal in a separate
                # folder that the exact-stem normal finder missed. Back-fill the
                # row's normal source so the standard normal pipeline (and the
                # Step-12 normal toggle) can apply it; stays disabled by default.
                normal_map = pbr.get("maps", {}).get("normal") if isinstance(pbr.get("maps"), dict) else None
                if (
                    isinstance(normal_map, dict)
                    and normal_map.get("available")
                    and normal_map.get("source")
                    and not row.get("normal_source_path")
                ):
                    kind_to_action = {
                        "convert_ue_rg": ("ue_rg", "convert_ue_rg"),
                        "copy_blue": ("blue", "copy_blue"),
                        "ambiguous_copy": ("ambiguous", "ambiguous_copy"),
                    }
                    ntype, naction = kind_to_action.get(str(normal_map.get("kind") or ""), ("ambiguous", "ambiguous_copy"))
                    row["normal_source_path"] = str(normal_map["source"])
                    row["normal_type"] = ntype
                    row["normal_action_default"] = naction
                    row["warnings"].append(
                        f"Normal map {Path(str(normal_map['source'])).name} matched from a separate folder "
                        "(disabled by default; enable the normal to bake it)."
                    )
                detected = sorted(role for role, info in pbr.get("maps", {}).items() if info.get("available"))
                if detected:
                    emit(f"[Step12 Textures]   PBR maps detected for {material.output_name}: {', '.join(detected)} (disabled by default).")
        rows.append(row)
        emit(
            f"[Step12 Textures] [{index}/{len(materials)}] {material.output_name}: "
            f"base={base_action}, normal={normal_action} available, disabled by default."
        )

    analysis = {
        "version": 1,
        "input": str(input_path.resolve()),
        "workspace_root": str(workspace_root),
        "material_dir": str(material_dir),
        "materials_json": str(materials_json) if materials_json else "",
        "scheme": scheme,
        "output_dir": str(output_dir),
        "png_dir": str(png_dir),
        "normal_dir": str(normal_dir),
        "phongexp_dir": str(phongexp_dir),
        "selfillum_dir": str(selfillum_dir),
        "material_count": len(rows),
        "rows": rows,
    }
    plan = {
        "version": 1,
        "input": str(input_path.resolve()),
        "workspace_root": str(workspace_root),
        "scheme": scheme,
        "output_dir": str(output_dir),
        "png_dir": str(png_dir),
        "normal_dir": str(normal_dir),
        "phongexp_dir": str(phongexp_dir),
        "selfillum_dir": str(selfillum_dir),
        # User-selected longest-edge cap (0 = game default); process mode reuses this if not overridden.
        "max_texture_edge": int(max_texture_edge or 0),
        "rows": rows,
    }
    analysis_json.parent.mkdir(parents=True, exist_ok=True)
    analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    plan_json.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(f"[Step12 Textures] Wrote analysis: {analysis_json}")
    emit(f"[Step12 Textures] Wrote plan: {plan_json}")
    return analysis


def process_textures(input_path: Path, plan_json: Path, report_json: Path, manifest_json: Path, scheme: str | None = None, game: str | None = None, max_texture_edge: int = 0) -> dict[str, Any]:
    global _ACTIVE_MAX_TEXTURE_EDGE
    plan = json.loads(plan_json.read_text(encoding="utf-8"))
    # L4D2 clamps every texture to 2048px (the game crashes on larger maps); GMod default is 4096, and
    # the Step-12 "max texture size" setting can cap lower. Prefer the passed value, else the one the
    # plan was analyzed with, then clamp to the game's hard cap.
    effective_game = game if game is not None else plan.get("game")
    user_edge = int(max_texture_edge or 0) or int(plan.get("max_texture_edge") or 0)
    _ACTIVE_MAX_TEXTURE_EDGE = resolve_max_texture_edge(effective_game, user_edge)
    rows = [row for row in plan.get("rows", []) if isinstance(row, dict)]
    output_dir = Path(str(plan.get("output_dir") or report_json.parent))
    png_dir = Path(str(plan.get("png_dir") or output_dir / "png"))
    normal_dir = Path(str(plan.get("normal_dir") or output_dir / "png_normals"))
    phongexp_dir = Path(str(plan.get("phongexp_dir") or output_dir / "png_phongexp"))
    selfillum_dir = Path(str(plan.get("selfillum_dir") or output_dir / "png_selfillum"))
    active_scheme = normalize_scheme(scheme if scheme is not None else plan.get("scheme"))
    png_dir.mkdir(parents=True, exist_ok=True)
    normal_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    placeholder_materials: list[str] = []
    started = time.monotonic()
    emit(f"[Step12 Textures] Processing {len(rows)} texture rows.")
    for index, row in enumerate(rows, start=1):
        if not row.get("enabled", True):
            continue
        material_name = str(row.get("material_name") or row.get("output_name") or f"material_{index:03d}")
        base_source, base_resolve_warnings = resolve_base_texture_path(
            str(row.get("base_source_path") or ""),
            output_dir.parent if output_dir.name == "12_param_texture_render_materials" else output_dir,
            str(row.get("base_color_file") or Path(str(row.get("base_source_path") or "")).name),
        )
        base_output = Path(str(row.get("base_output_path") or png_dir / f"{row.get('output_name', material_name)}.png"))
        normal_source = as_path(str(row.get("normal_source_path") or ""))
        normal_output = Path(str(row.get("normal_output_path") or normal_dir / f"{row.get('output_name', material_name)}_n.png"))
        row_report = {
            "uid": row.get("uid"),
            "material_name": material_name,
            "output_name": row.get("output_name"),
            "base_source_path": str(base_source) if base_source else "",
            "base_output_path": str(base_output),
            "normal_source_path": str(normal_source) if normal_source else "",
            "normal_output_path": "",
            "normal_status": row.get("normal_action", "disabled"),
            "scheme": active_scheme,
            "phongexp_output_path": "",
            "selfillum_output_path": "",
            "ao_baked": False,
            "pbr_enabled": [],
            "warnings": list(row.get("warnings", [])) if isinstance(row.get("warnings"), list) else [],
        }
        if base_source and base_source.exists() and base_resolve_warnings:
            row_report["warnings"] = [
                warning for warning in row_report["warnings"] if warning != "Base texture source file is missing."
            ]
        row_report["warnings"].extend(base_resolve_warnings)
        pbr_block = row.get("pbr") if isinstance(row.get("pbr"), dict) else {}
        pbr_maps = pbr_block.get("maps", {}) if isinstance(pbr_block.get("maps"), dict) else {}
        enabled_pbr = {
            role: info
            for role, info in pbr_maps.items()
            if isinstance(info, dict) and info.get("available") and info.get("enabled")
        }
        try:
            if not base_source or not base_source.exists():
                raise FileNotFoundError(f"Base texture not found for {material_name}: {base_source}")
            original_size, final_size, resized = save_base_png(base_source, base_output)
            row_report["base_original_size"] = list(original_size)
            row_report["base_output_size"] = list(final_size)
            row_report["base_resized"] = resized
            if "ao" in enabled_pbr:
                ao_source = as_path(enabled_pbr["ao"].get("source"))
                if ao_source and ao_source.exists():
                    try:
                        row_report["ao_mean"] = bake_ao_into_base(base_output, ao_source, str(enabled_pbr["ao"].get("channel") or "g"))
                        row_report["ao_baked"] = True
                        row_report["pbr_enabled"].append("ao")
                    except Exception as ao_exc:
                        row_report["warnings"].append(f"AO bake failed: {ao_exc}")
        except Exception as exc:
            # A missing or unreadable base texture must NOT abort the whole step:
            # the model's other materials are fine, and one broken material would
            # otherwise fail the entire import. Write a neutral placeholder so the
            # material still compiles to a plain surface (not the in-game missing-
            # texture checkerboard) and record a prominent warning instead of a
            # blocking error. Only a failure to even write the placeholder is fatal.
            try:
                placeholder_size = write_placeholder_base_png(
                    base_output, tuple(row.get("base_size") or ()) or None
                )
                row_report["base_output_size"] = list(placeholder_size)
                row_report["base_is_placeholder"] = True
                row_report["base_placeholder_reason"] = str(exc)
                row_report["warnings"].append(
                    f"Base texture missing or unreadable ({exc}); wrote a neutral placeholder. "
                    "Supply the correct texture (or point the material at an existing one) and re-run Step 12."
                )
                placeholder_materials.append(material_name)
                emit(f"[Step12 Textures] [{index}/{len(rows)}] {material_name}: WARNING placeholder base ({exc}).")
            except Exception as placeholder_exc:
                errors.append(f"{material_name}: {exc}; placeholder also failed: {placeholder_exc}")
                row_report["error"] = str(exc)
                manifest_rows.append(row_report)
                emit(f"[Step12 Textures] [{index}/{len(rows)}] {material_name}: ERROR {exc}")
                continue
            # A placeholder base carries no detail, so skip normal/PBR derivation.
            manifest_rows.append(row_report)
            continue

        normal_action = str(row.get("normal_action") or "disabled")
        default_normal_action = str(row.get("normal_action_default") or normal_action or "skip_missing")
        use_normal = bool(row.get("use_normal", row.get("generate_normal", False)))
        force_generate = bool(row.get("force_generate_normal", False))
        if force_generate:
            use_normal = True
        normal_intensity = int(row.get("normal_intensity") or row.get("normal_bias") or SMARTNORMAL_BIAS)
        normal_applied_strength = normal_generation_strength(normal_intensity)
        row_report["use_normal"] = use_normal
        row_report["generate_normal"] = force_generate or (use_normal and default_normal_action in {"generate", "skip_generate"})
        row_report["force_generate_normal"] = force_generate
        row_report["normal_intensity"] = normal_intensity
        row_report["normal_applied_strength"] = round(float(normal_applied_strength), 6)
        try:
            if not use_normal:
                row_report["normal_status"] = "disabled"
            elif force_generate:
                normal_output.parent.mkdir(parents=True, exist_ok=True)
                generate_normal_from_base(base_source, normal_output, normal_intensity)
                validate_or_regenerate_normal(normal_output, base_source, normal_intensity, row_report, "Generated")
                row_report["normal_output_path"] = str(normal_output)
                row_report["normal_status"] = "generated_override"
            elif normal_source and normal_source.exists() and default_normal_action in {"copy_blue", "ambiguous_copy"}:
                _orig, _final, resized = save_normal_png(normal_source, normal_output)
                validate_or_regenerate_normal(normal_output, base_source, normal_intensity, row_report, "Copied")
                row_report["normal_output_path"] = str(normal_output)
                row_report["normal_resized"] = resized
                row_report["normal_status"] = "regenerated_for_tone" if row_report.get("normal_regenerated_for_tone") else default_normal_action
            elif normal_source and normal_source.exists() and default_normal_action == "convert_ue_rg":
                normal_output.parent.mkdir(parents=True, exist_ok=True)
                convert_ue_rg_normal_to_rgb(normal_source, normal_output)
                validate_or_regenerate_normal(normal_output, base_source, normal_intensity, row_report, "Converted")
                row_report["normal_output_path"] = str(normal_output)
                row_report["normal_status"] = "regenerated_for_tone" if row_report.get("normal_regenerated_for_tone") else default_normal_action
            elif default_normal_action in {"generate", "skip_generate"}:
                normal_output.parent.mkdir(parents=True, exist_ok=True)
                generate_normal_from_base(base_source, normal_output, normal_intensity)
                validate_or_regenerate_normal(normal_output, base_source, normal_intensity, row_report, "Generated")
                row_report["normal_output_path"] = str(normal_output)
                row_report["normal_status"] = "generated"
            elif default_normal_action in {"copy_blue", "ambiguous_copy", "convert_ue_rg"}:
                # Normal use is enabled, but the recorded source no longer exists.
                raise FileNotFoundError(f"Normal source file is missing: {normal_source or row.get('normal_source_path') or '(unknown)'}")
            else:
                row_report["normal_status"] = default_normal_action
        except Exception as exc:
            row_report["warnings"].append(f"Normal processing failed: {exc}")
            row_report["normal_status"] = "failed"
        if "normal" in enabled_pbr and row_report.get("normal_output_path"):
            row_report["pbr_enabled"].append("normal")
        if active_scheme != PBR_SCHEME_LEGACY and enabled_pbr:
            roughness_info = enabled_pbr.get("roughness")
            metallic_info = enabled_pbr.get("metallic")
            if roughness_info or metallic_info:
                try:
                    exp_output = phongexp_dir / f"{row.get('output_name', material_name)}_exp.png"
                    stats = build_phong_exponent_png(
                        exp_output,
                        as_path(roughness_info.get("source")) if roughness_info else None,
                        str(roughness_info.get("channel") or "a") if roughness_info else "a",
                        bool(roughness_info.get("invert")) if roughness_info else False,
                        as_path(metallic_info.get("source")) if metallic_info else None,
                        str(metallic_info.get("channel") or "r") if metallic_info else "r",
                    )
                    if stats:
                        row_report["phongexp_output_path"] = str(exp_output)
                        row_report["phongexp_stats"] = stats
                        if roughness_info:
                            row_report["pbr_enabled"].append("roughness")
                        if metallic_info:
                            row_report["pbr_enabled"].append("metallic")
                except Exception as exc:
                    row_report["warnings"].append(f"Phong exponent build failed: {exc}")
            emission_info = enabled_pbr.get("emission")
            if emission_info:
                emission_source = as_path(emission_info.get("source"))
                if emission_source and emission_source.exists():
                    try:
                        selfillum_output = selfillum_dir / f"{row.get('output_name', material_name)}_selfillum.png"
                        emission_channel = str(emission_info.get("channel") or "").strip()
                        if emission_channel:
                            # Packed glow mask (e.g. Wuthering Waves _FTM alpha): the
                            # mask is one channel; the emissive colour is the base texture.
                            mean_luminance, is_emissive = build_selfillum_mask_from_channel(
                                emission_source, emission_channel, selfillum_output
                            )
                        else:
                            mean_luminance, is_emissive = build_selfillum_png(emission_source, selfillum_output)
                        row_report["selfillum_mean"] = mean_luminance
                        if is_emissive:
                            row_report["selfillum_output_path"] = str(selfillum_output)
                            row_report["pbr_enabled"].append("emission")
                        else:
                            row_report["warnings"].append("Emission map is effectively black; self-illumination was skipped.")
                    except Exception as exc:
                        row_report["warnings"].append(f"Self-illumination build failed: {exc}")
        manifest_rows.append(row_report)
        emit(
            f"[Step12 Textures] [{index}/{len(rows)}] {material_name}: "
            f"base -> {base_output.name}, normal={row_report['normal_status']}"
            + (f", pbr={'+'.join(row_report['pbr_enabled'])}" if row_report["pbr_enabled"] else "")
            + "."
        )

    # L4D2: clamp every written texture (base/normal are already clamped at write time; this
    # catches the derived phong-exponent / selfillum / generated-normal maps that are saved at
    # source resolution). GMod keeps the 4096 cap so this is a no-op and the output is unchanged.
    clamped_textures: list[dict[str, Any]] = []
    if _ACTIVE_MAX_TEXTURE_EDGE < MAX_TEXTURE_EDGE:
        clamped_textures = cap_pngs_to_limit(
            [png_dir, normal_dir, phongexp_dir, selfillum_dir], _ACTIVE_MAX_TEXTURE_EDGE
        )
        if clamped_textures:
            emit(
                f"[Step12 Textures] Clamped {len(clamped_textures)} texture(s) to "
                f"{_ACTIVE_MAX_TEXTURE_EDGE}px for L4D2."
            )

    manifest = {
        "version": 1,
        "input": str(input_path.resolve()),
        "output_dir": str(output_dir),
        "png_dir": str(png_dir),
        "normal_dir": str(normal_dir),
        "max_texture_edge": _ACTIVE_MAX_TEXTURE_EDGE,
        "clamped_textures": clamped_textures,
        "textures": manifest_rows,
    }
    if placeholder_materials:
        emit(
            f"[Step12 Textures] Wrote neutral placeholder base textures for {len(placeholder_materials)} "
            f"material(s) with a missing source: {', '.join(placeholder_materials)}."
        )
    report = {
        "version": 1,
        "input": str(input_path.resolve()),
        "output_dir": str(output_dir),
        "processed_count": len([row for row in manifest_rows if not row.get("error")]),
        "error_count": len(errors),
        "errors": errors,
        "placeholder_count": len(placeholder_materials),
        "placeholder_materials": placeholder_materials,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "manifest_path": str(manifest_json),
    }
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(f"[Step12 Textures] Wrote report: {report_json}")
    emit(f"[Step12 Textures] Wrote manifest: {manifest_json}")
    if errors:
        raise RuntimeError(f"Texture processing completed with {len(errors)} blocking error(s).")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("analyze", "process"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--analysis-json", type=Path)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--manifest-json", type=Path)
    parser.add_argument("--scheme", default=None, help="PBR texture scheme (legacy, unreal_wuwa, unity_endfield, moongaze_pbr). Process mode falls back to the plan's stored scheme when omitted.")
    parser.add_argument("--game", default="gmod", help="Target game; 'l4d2' clamps every texture to 2048px (the game crashes on larger maps). Default 'gmod' keeps the 4096 cap.")
    parser.add_argument("--max-texture-edge", type=int, default=0, help="Longest-edge cap in px (4096/2048/1024). 0 = game default (4096 GMod / 2048 L4D2). Never exceeds the game's hard cap.")
    args = parser.parse_args(argv)
    if args.mode == "analyze":
        if not args.analysis_json:
            parser.error("--analysis-json is required for analyze mode")
        analyze_textures(args.input, args.analysis_json, args.plan_json, scheme=args.scheme or PBR_SCHEME_LEGACY, game=args.game, max_texture_edge=args.max_texture_edge)
        return 0
    if not args.report_json or not args.manifest_json:
        parser.error("--report-json and --manifest-json are required for process mode")
    process_textures(args.input, args.plan_json, args.report_json, args.manifest_json, scheme=args.scheme, game=args.game, max_texture_edge=args.max_texture_edge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
