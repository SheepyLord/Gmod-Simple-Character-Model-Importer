#!/usr/bin/env python3
"""Step 14 QC generation, StudioMDL compile, and addon folder composition."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STEP_DIR_NAME = "14_sort_qc_compile"
MAX_PREVIEW_TRIANGLES = 500000
MAX_STUDIOMDL_MODEL_VERTS = 65536
SMD_SPLIT_RAW_VERTEX_BUDGET = 55000
STUDIOMDL_FAILURE_EXCERPT_LINES = 60
DEFAULT_DIRECTIONAL_JIGGLE_ANGLE = 25.0
LEGACY_DIRECTIONAL_JIGGLE_ANGLE = 45.0
COMPILED_EXTENSIONS = (".mdl", ".vvd", ".phy", ".dx80.vtx", ".dx90.vtx", ".sw.vtx")
OPTIONAL_CANONICAL_MODEL_SMDS = ("Body.smd", "Face.smd")
OPTIONAL_CANONICAL_SMD_WARNING_PREFIX = "Optional canonical SMD missing:"
SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")
INTERNAL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DISPLAY_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_ ]+$")
CATEGORY_DISPLAY_RE = re.compile(r"^[\x20-\x7E]+$")
KNOWN_CATEGORY_READABLE_OVERRIDES = {
    "honkai_star_rail": "Honkai: Star Rail",
}
MISSING_PARENT_RE = re.compile(
    r"Imported bone\s+(?P<child>.+?)\s+tried to access parent bone\s+(?P<parent>.+?)\s+and failed!"
)
DEFINEBONE_RE = re.compile(
    r'^\s*\$definebone\s+"(?P<name>[^"]+)"\s+"(?P<parent>[^"]*)"\s+(?P<rest>.*?)\s*$'
)
BAD_OPEN_BRACE_RE = re.compile(r"\((?P<line>\d+)\):\s*-\s*bad command\s+\{", re.IGNORECASE)

ESSENTIAL_EXACT = {"ZArmTwist_L", "ZArmTwist_R", "ZHandTwist_L", "ZHandTwist_R", "Eye_L", "Eye_R"}
GENDER_CHOICES = ("female", "male")
GAME_CHOICES = ("gmod", "l4d2", "sfm")
# Human-readable target names, used in messages the user has to act on
# (e.g. "no studiomdl.exe is set for Left 4 Dead 2").
GAME_DISPLAY_NAMES = {
    "gmod": "Garry's Mod",
    "l4d2": "Left 4 Dead 2",
    "sfm": "Source Filmmaker",
}
GENDER_ANIMATION_INCLUDES = {
    "female": {
        # Playermodel QC needs ONLY the standard GMod player animation packs.
        # Pulling in the humans/* NPC sets and alyx_* packs (issue #83) breaks
        # player animations such as jump transitions.
        "player": [
            'f_anm.mdl',
            'f_gst.mdl',
            'f_pst.mdl',
            'f_shd.mdl',
            'f_ss.mdl',
        ],
        "npc": [
            'humans/female_shared.mdl',
            'humans/female_ss.mdl',
            'humans/female_gestures.mdl',
            'humans/female_postures.mdl',
            'alyx_animations.mdl',
            'alyx_postures.mdl',
        ],
        "reference_smd": "reference_female",
    },
    "male": {
        "player": [
            'm_anm.mdl',
            'm_gst.mdl',
            'm_pst.mdl',
            'm_shd.mdl',
            'm_ss.mdl',
        ],
        "npc": [
            'humans/male_shared.mdl',
            'humans/male_ss.mdl',
            'humans/male_gestures.mdl',
            'humans/male_postures.mdl',
        ],
        "reference_smd": "reference_male",
    },
}
# Back-compat aliases (female remains the default animation set).
PLAYER_ANIMATION_INCLUDES = GENDER_ANIMATION_INCLUDES["female"]["player"]
NPC_ANIMATION_INCLUDES = GENDER_ANIMATION_INCLUDES["female"]["npc"]


def normalize_gender(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in GENDER_CHOICES else "female"


def normalize_game(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in GAME_CHOICES else "gmod"


# The 8 L4D2 survivor slots an MMD model can be ported onto (the 4 L4D2 survivors
# plus the 4 returning L4D1 survivors). The model name/path MUST be the exact
# survivor slot (custom names are not allowed in L4D2): the character is
# survivors/survivor_<slot>.mdl and the first-person arms are weapons/arms/v_arms_<slot>_new.mdl
# for the 4 L4D2 survivors, but the 4 returning L4D1 survivors keep their original arms
# (v_arms_bill/zoey/francis/louis, no "_new") -- see L4D2_SURVIVOR_ARMS_RELPATH.
# anim/gesture includes and the vgui survivor panels also use the
# exact slot. Internal slot -> in-game name: producer=Rochelle, coach=Coach,
# gambler=Nick, mechanic=Ellis, namvet=Bill, teenangst=Zoey, biker=Francis,
# manager=Louis.
L4D2_SURVIVOR_SLOTS = (
    "producer",
    "coach",
    "gambler",
    "mechanic",
    "namvet",
    "teenangst",
    "biker",
    "manager",
)
DEFAULT_L4D2_SURVIVOR = "producer"
# The four L4D1 survivors. Campaigns that show them as NPCs (The Passing, The
# Sacrifice) load a separate survivors/survivor_<slot>_light.mdl model, so a
# replacement addon must also ship that variant or the cameo keeps the stock
# look (issue #135).
L4D1_SURVIVOR_SLOTS = ("namvet", "teenangst", "biker", "manager")


def normalize_survivor(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in L4D2_SURVIVOR_SLOTS else DEFAULT_L4D2_SURVIVOR


# L4D2 survivor $attachment set (from the base-game survivor_producer decompile). These tell
# the game where to place weapons and items: without them the held weapon world-model has no
# bone to bone-merge to and is dropped at the model origin. Offsets are bone-relative and shared
# across survivors (the body is a custom MMD model regardless); each line is emitted only if the
# referenced bone exists in the selected survivor's skeleton (the L4D1 survivors lack
# L_weapon_bone, etc.), so the compile never references a missing bone.
L4D2_SURVIVOR_ATTACHMENTS = [
    '$attachment "eyes" "ValveBiped.Bip01_Head1" 3.42 -2.36 0.05 rotate 0 -89.37 -90',
    '$attachment "mouth" "ValveBiped.Bip01_Head1" 0.71 -5.15 -0.13 rotate 0 -80 -90',
    '$attachment "survivor_light" "ValveBiped.Bip01_Spine2" 5.33 21.31 0 rotate 0 0 0',
    '$attachment "forward" "ValveBiped.forward" 0 0 0 rotate 0 0 0',
    '$attachment "pistol" "ValveBiped.Bip01_R_Thigh" -2.95 1.84 -4.61 rotate -3.66 -0.47 91.7',
    '$attachment "L_weapon_bone" "ValveBiped.L_weapon_bone" 0 0 0 rotate 0 0 0',
    '$attachment "weapon_bone" "ValveBiped.weapon_bone" 0 0 0 rotate 0 0 0',
    '$attachment "medkit" "ValveBiped.Bip01_Spine4" -0.65 -2.83 -1.16 rotate 5.03 77.16 0',
    '$attachment "primary" "ValveBiped.Bip01_Spine4" 2.71 -4.36 -2.33 rotate -13.7 170.19 174.29',
    '$attachment "attach_R_shoulderBladeAim" "ValveBiped.Bip01_Spine4" -8.88 0.88 -4.51 rotate -90 -102.85 0',
    '$attachment "attach_L_shoulderBladeAim" "ValveBiped.Bip01_Spine4" -8.88 0.88 3.12 rotate -90 -102.85 0',
    '$attachment "melee" "ValveBiped.Bip01_Spine4" 2.64 -3.12 4.45 rotate 24.08 175.37 97.14',
    '$attachment "molotov" "ValveBiped.Bip01_Spine" -3.19 -2.44 7.01 rotate -63.44 -74.67 -101.41',
    '$attachment "grenade" "ValveBiped.Bip01_Spine" -0.68 1.17 6.97 rotate -90 -175.23 0',
    '$attachment "pills" "ValveBiped.Bip01_Spine" -2.63 0.63 -7.56 rotate -41.18 -88.48 -87.05',
    '$attachment "lfoot" "ValveBiped.Bip01_L_Foot" 0 4.44 0 rotate 0 0 0',
    '$attachment "rfoot" "ValveBiped.Bip01_R_Foot" 0 4.44 0 rotate 0 0 0',
    '$attachment "muzzle_flash" "ValveBiped.Bip01_L_Hand" 0 0 0 rotate 0 0 0',
    '$attachment "survivor_neck" "ValveBiped.Bip01_Neck1" 0 0 0 rotate 0 0 0',
    '$attachment "bleedout" "ValveBiped.Bip01_Pelvis" 8.44 8.88 4.44 rotate 0 0 0',
    '$attachment "legL_B" "ValveBiped.attachment_bandage_legL" 0 0 0 rotate -90 -90 0',
    '$attachment "armL_B" "ValveBiped.attachment_bandage_armL" 0 0 0 rotate -90 -90 0',
    '$attachment "armL_T" "ValveBiped.attachment_armL_T" 0 0 0 rotate -90 -90 0',
    '$attachment "armR_T" "ValveBiped.attachment_armR_T" 0 0 0 rotate -90 -90 0',
    '$attachment "armL" "ValveBiped.Bip01_L_Forearm" 0 0 0 rotate 0 0 0',
    '$attachment "legL" "ValveBiped.Bip01_L_Calf" 0 0 0 rotate 0 0 0',
    '$attachment "thighL" "ValveBiped.Bip01_L_Thigh" 0 0 0 rotate 0 0 0',
    '$attachment "spine" "ValveBiped.Bip01_Spine" 0 0 0 rotate -90 -90 0',
]


def l4d2_survivor_skeleton_bones(slot: str) -> set[str]:
    """Bone names present in the bundled survivor skeleton (the proportion base armature).

    The compiled L4D2 model carries exactly these bones (core biped + the survivor's functional
    weapon/attachment bones), so this is the authoritative set for deciding which attachments and
    IK chains can be emitted without referencing a bone that does not exist.
    """

    smd = Path(__file__).resolve().parent / "l4d2_reference_skeletons" / f"{normalize_survivor(slot)}.smd"
    names: set[str] = set()
    if not smd.exists():
        return names
    for line in smd.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip() == "skeleton":
            break
        match = re.match(r'\s*\d+\s+"([^"]+)"', line)
        if match:
            names.add(match.group(1))
    return names


def _attachment_bone(line: str) -> str:
    parts = line.split('"')
    return parts[3] if len(parts) >= 4 else ""


def l4d2_attachment_lines(model_bones: set[str]) -> list[str]:
    return [f"{line} \n" for line in L4D2_SURVIVOR_ATTACHMENTS if _attachment_bone(line) in model_bones]


def l4d2_ik_lines(model_bones: set[str]) -> list[str]:
    """L4D2 survivor IK chains + autoplay locks (matches the base-game survivor QC)."""
    lines = [
        '$ikchain "rhand" "ValveBiped.Bip01_R_Hand" knee 0.707 0.707 0 \n',
        '$ikchain "lhand" "ValveBiped.Bip01_L_Hand" knee 0.707 0.707 0 \n',
        '$ikchain "rfoot" "ValveBiped.Bip01_R_Foot" knee 0.707 -0.707 0 \n',
        '$ikchain "lfoot" "ValveBiped.Bip01_L_Foot" knee 0.707 -0.707 0 \n',
    ]
    if "ValveBiped.weapon_bone_Clip" in model_bones:
        lines.append('$ikchain "ikclip" "ValveBiped.weapon_bone_Clip" knee 0.707 -0.707 0 \n')
    lines.append("\n")
    lines.append('$ikautoplaylock "rfoot" 1 0.1 \n')
    lines.append('$ikautoplaylock "lfoot" 1 0.1 \n\n')
    return lines


def l4d2_character_modelname(slot: str) -> str:
    return f"survivors/survivor_{normalize_survivor(slot)}.mdl"


# First-person arms model path per survivor slot (relative to models/, no extension). The 4 L4D2
# survivors use the "_new" arms named by their internal anim slot (producer/coach/gambler/mechanic),
# but the 4 returning L4D1 survivors keep their ORIGINAL L4D1 arms, named by the in-game character
# WITHOUT the "_new" suffix (Bill/Zoey/Francis/Louis) -- verified against the base-game v_arms_*.mdl
# headers in L4D2_Support/default_c_arm_l4d1. A custom survivor only shows up in first person if it
# overrides the EXACT arms path the game loads for that character; the L4D1 slots cannot use the
# v_arms_<slot>_new pattern (the game never loads v_arms_namvet_new.mdl, so those arms stayed the
# default -- issue #124).
L4D2_SURVIVOR_ARMS_RELPATH = {
    "producer": "weapons/arms/v_arms_producer_new",
    "coach": "weapons/arms/v_arms_coach_new",
    "gambler": "weapons/arms/v_arms_gambler_new",
    "mechanic": "weapons/arms/v_arms_mechanic_new",
    "namvet": "weapons/arms/v_arms_bill",
    "teenangst": "weapons/arms/v_arms_zoey",
    "biker": "weapons/arms/v_arms_francis",
    "manager": "weapons/arms/v_arms_louis",
}


def l4d2_arms_relpath(slot: str) -> str:
    """Arms model path relative to models/ (no extension) for the survivor the game actually loads."""
    survivor = normalize_survivor(slot)
    return L4D2_SURVIVOR_ARMS_RELPATH.get(survivor, f"weapons/arms/v_arms_{survivor}_new")


def l4d2_arms_modelname(slot: str) -> str:
    return f"{l4d2_arms_relpath(slot)}.mdl"


# The exact $includemodel set each survivor's base-game model uses, verbatim from TwentyCat's
# l4d2_survivors_converter same-slot wrapper QCs. The 4 main survivors are just anim_<slot> +
# gestures_<slot>, but the L4D1 survivors pull extra/aliased anim models (Zoey also loads producer
# anims; Francis/Louis use the *_animloader + Biker anim set), so a naive "anim_<slot>" would
# reference models that don't exist and the $declaresequence names would not resolve. Strings kept
# verbatim, including the few without a "survivors/" prefix.
L4D2_SURVIVOR_INCLUDEMODELS = {
    "producer": ["survivors/anim_producer.mdl", "survivors/gestures_producer.mdl"],
    "coach": ["survivors/anim_coach.mdl", "survivors/gestures_coach.mdl"],
    "gambler": ["survivors/anim_gambler.mdl", "survivors/gestures_gambler.mdl"],
    "mechanic": ["survivors/anim_mechanic.mdl", "survivors/gestures_mechanic.mdl"],
    "namvet": ["survivors/anim_NamVet.mdl", "survivors/gestures_namvet.mdl"],
    "teenangst": [
        "survivors/anim_teenangst.mdl",
        "survivors/gestures_TeenAngst.mdl",
        "survivors/anim_producer.mdl",
        "survivors/anim_gestures.mdl",
    ],
    "biker": [
        "survivors/anim_biker_animloader.mdl",
        "anim_biker_own_anims.mdl",
        "survivors/anim_Biker.mdl",
        "survivors/gestures_biker.mdl",
    ],
    "manager": [
        "survivors/anim_louis_animloader.mdl",
        "anim_louis_own_anims.mdl",
        "survivors/anim_Biker.mdl",
        "survivors/gestures_biker.mdl",
    ],
}


def l4d2_includemodel_lines(slot: str) -> list[str]:
    survivor = normalize_survivor(slot)
    includes = L4D2_SURVIVOR_INCLUDEMODELS.get(survivor) or [
        f"survivors/anim_{survivor}.mdl",
        f"survivors/gestures_{survivor}.mdl",
    ]
    return [f'$includemodel "{include}" \n' for include in includes]


def l4d2_reference_sequences_path(slot: str) -> Path:
    return Path(__file__).resolve().parent / "l4d2_reference_sequences" / f"{normalize_survivor(slot)}.txt"


def l4d2_declaresequence_lines(slot: str) -> list[str]:
    """The selected survivor's full ordered base-game sequence-name list, emitted as $declaresequence.

    This is the fix for the multiplayer survivor T-pose (issue #105). A survivor REPLACEMENT prepends
    its own local sequences (reference/ragdoll/proportions) before $includemodel, which shifts the
    index of every included anim/gesture sequence. L4D2 transmits animation across the network by
    integer sequence INDEX, so the server addresses the wrong sequence and remote clients render the
    bind pose (= T-pose); single-player resolves locally by name and looks fine. Forward-declaring
    every included sequence here, in canonical order, re-pins them to the indices the server expects.
    Lists are bundled verbatim from TwentyCat's l4d2_survivors_converter wrapper QCs (the names are
    Valve base-game sequence names)."""
    path = l4d2_reference_sequences_path(slot)
    if not path.exists():
        return []
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [f'$declaresequence "{name}" \n' for name in names]


def gendered_reference_name(plan: dict, anims_dir: Path, warnings: list | None = None) -> str:
    """Pick the reference SMD for the plan's gender, falling back to female.

    Step 9 exports both reference_female.smd and reference_male.smd, but older
    workspaces may predate the male export; never emit a QC that cannot compile.
    """

    gender = normalize_gender(plan.get("gender"))
    reference = str(GENDER_ANIMATION_INCLUDES[gender]["reference_smd"])
    if gender != "female" and not (anims_dir / f"{reference}.smd").exists():
        if warnings is not None:
            warnings.append(
                f"anims/{reference}.smd was not found in the Step 9 export; the female reference pose is used instead. "
                "Re-run Step 9 to regenerate both reference animations."
            )
        return "reference_female"
    return reference


# Stock GMod c_arms standard skeleton (decompiled weapons/c_arms.mdl). The first-person c_arms
# bonemerge onto the weapon viewmodel, whose arm bones use the STOCK c_arms proportions
# (UpperArm 6.028 / Forearm 11.693 / Hand 11.481) -- which match the MALE player reference but NOT
# the female one. So the proportion-trick subtract MUST go against the stock c_arms arm proportions,
# not the gendered player reference; otherwise non-male characters' first-person arms are displaced
# (issue #121). build_carms_proportion_reference() builds a c_arms-specific reference for the GMod
# proportion-driven c_arms.
CARMS_STOCK_REFERENCE_SMD = Path(__file__).resolve().parent / "assets" / "std_c_arms_skeleton" / "c_arms_stock_reference.smd"
# Override only the arm chain; the spine/neck/head roots keep the character pose (zero delta).
CARMS_REFERENCE_ROOT_BONES = {"ValveBiped.Bip01_Spine4", "ValveBiped.Bip01_Neck1", "ValveBiped.Bip01_Head1"}


def _read_smd_frame0_poses(smd_path: Path) -> dict[str, str]:
    """Return {bone_name: 'px py pz rx ry rz'} from an SMD's skeleton frame 0."""
    text = smd_path.read_text(encoding="utf-8", errors="replace").splitlines()
    nodes_start = next(i for i, ln in enumerate(text) if ln.strip() == "nodes")
    id_to_name: dict[int, str] = {}
    i = nodes_start + 1
    while i < len(text) and text[i].strip() != "end":
        m = re.match(r'\s*(\d+)\s+"([^"]+)"', text[i])
        if m:
            id_to_name[int(m.group(1))] = m.group(2)
        i += 1
    skel_start = next(j for j in range(i, len(text)) if text[j].strip() == "skeleton")
    j = skel_start + 1
    while j < len(text) and not text[j].strip().startswith("time"):
        j += 1
    j += 1
    poses: dict[str, str] = {}
    while j < len(text) and text[j].strip() != "end":
        parts = text[j].split()
        if parts and parts[0].lstrip("-").isdigit() and len(parts) >= 7 and int(parts[0]) in id_to_name:
            poses[id_to_name[int(parts[0])]] = " ".join(parts[1:7])
        j += 1
    return poses


def build_carms_proportion_reference(carms_work: Path) -> str | None:
    """Write anims/c_arms_reference.smd = the character's proportions.smd with the ARM-CHAIN bones'
    frame-0 poses overridden to the stock c_arms standard, so the proportion-trick subtract delta is
    (character - c_arms_standard) for the arms (the value the runtime viewmodel bonemerge expects --
    this is what makes non-male first-person arms land correctly, issue #121) and ZERO for every
    other bone (they are not rendered or merged). Returns "c_arms_reference", or None when
    proportions.smd / the stock reference is unavailable so the caller can fall back to the gendered
    reference (preserving the old behaviour rather than failing the compile)."""
    proportions = carms_work / "anims" / "proportions.smd"
    if not proportions.exists() or not CARMS_STOCK_REFERENCE_SMD.exists():
        return None
    try:
        stock_poses = _read_smd_frame0_poses(CARMS_STOCK_REFERENCE_SMD)
    except Exception as exc:
        emit(f"c_arms reference: could not read the stock c_arms poses ({exc}); using the gendered reference.")
        return None
    override = {name: pose for name, pose in stock_poses.items() if name not in CARMS_REFERENCE_ROOT_BONES}
    if not override:
        return None
    text = proportions.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        nodes_start = next(i for i, ln in enumerate(text) if ln.strip() == "nodes")
    except StopIteration:
        return None
    id_to_name: dict[int, str] = {}
    i = nodes_start + 1
    while i < len(text) and text[i].strip() != "end":
        m = re.match(r'\s*(\d+)\s+"([^"]+)"', text[i])
        if m:
            id_to_name[int(m.group(1))] = m.group(2)
        i += 1
    out = list(text)
    j = next((k for k in range(i, len(out)) if out[k].strip() == "skeleton"), None)
    if j is None:
        return None
    j += 1
    while j < len(out) and not out[j].strip().startswith("time"):
        j += 1
    j += 1
    replaced = 0
    while j < len(out) and out[j].strip() != "end":
        parts = out[j].split()
        if parts and parts[0].lstrip("-").isdigit() and len(parts) >= 7:
            name = id_to_name.get(int(parts[0]))
            if name in override:
                lead = re.match(r"(\s*)", out[j]).group(1)
                out[j] = f"{lead}{parts[0]} {override[name]}"
                replaced += 1
        j += 1
    if replaced == 0:
        return None
    (carms_work / "anims" / "c_arms_reference.smd").write_text("\n".join(out) + "\n", encoding="utf-8")
    emit(f"c_arms proportion reference: overrode {replaced} arm-chain bone(s) onto the stock c_arms standard.")
    return "c_arms_reference"


HBOX_GROUPS = {
    "ValveBiped.Bip01_Pelvis": 3,
    "ValveBiped.Bip01_L_Thigh": 6,
    "ValveBiped.Bip01_L_Calf": 6,
    "ValveBiped.Bip01_L_Foot": 6,
    "ValveBiped.Bip01_R_Thigh": 7,
    "ValveBiped.Bip01_R_Calf": 7,
    "ValveBiped.Bip01_R_Foot": 7,
    "ValveBiped.Bip01_Spine": 3,
    "ValveBiped.Bip01_Spine1": 2,
    "ValveBiped.Bip01_Spine2": 2,
    "ValveBiped.Bip01_Spine4": 2,
    "ValveBiped.Bip01_R_Clavicle": 5,
    "ValveBiped.Bip01_R_UpperArm": 5,
    "ValveBiped.Bip01_R_Forearm": 5,
    "ValveBiped.Bip01_R_Hand": 5,
    "ValveBiped.Bip01_L_Clavicle": 4,
    "ValveBiped.Bip01_L_UpperArm": 4,
    "ValveBiped.Bip01_L_Forearm": 4,
    "ValveBiped.Bip01_L_Hand": 4,
    "ValveBiped.Bip01_Neck1": 8,
    "ValveBiped.Bip01_Head1": 1,
}


def emit(message: str) -> None:
    print(f"[Step14 QC] {message}", flush=True)


def write_json(path: Path, data: dict[str, Any], compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")


def compact_command_output_excerpt(text: str, max_lines: int = STUDIOMDL_FAILURE_EXCERPT_LINES) -> str:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    interesting = [
        line
        for line in lines
        if any(token in line.upper() for token in ("ERROR", "WARNING", "FATAL", "ABORT", "FAILED", "TOO MANY", "MAXSTUDIOVERTS"))
    ]
    selected: list[str] = []
    for line in interesting[-max_lines:]:
        if line not in selected:
            selected.append(line)
    tail_budget = max(0, max_lines - len(selected))
    for line in lines[-tail_budget:]:
        if line not in selected:
            selected.append(line)
    if not selected:
        selected = lines[-max_lines:]
    return "\n".join(selected[-max_lines:])


class StudioMDLCompileError(RuntimeError):
    def __init__(self, qc_path: Path, log_path: Path, output: str, exit_code: int | None = None) -> None:
        self.qc_path = qc_path
        self.log_path = log_path
        self.output = output
        self.exit_code = exit_code
        self.output_excerpt = compact_command_output_excerpt(output)
        message = f"StudioMDL failed for {qc_path.name}"
        if exit_code is not None:
            message += f" with exit code {exit_code}"
        message += f"; see {log_path}"
        if self.output_excerpt:
            message += "\n\nStudioMDL output excerpt:\n" + self.output_excerpt
        super().__init__(message)

    def to_report(self) -> dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "message": str(self),
            "exit_code": self.exit_code,
            "qc_path": str(self.qc_path),
            "log_path": str(self.log_path),
            "output_excerpt": self.output_excerpt,
        }


def hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def path_is_ascii(path: Path | str) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def external_tool_root() -> Path:
    # VTFCmd/gmad use ANSI file APIs, so the staging root itself must be ASCII.
    # %LOCALAPPDATA%/%TEMP%/home all live under C:\Users\<username> and are
    # non-ASCII for non-ASCII Windows usernames; fall back to a guaranteed
    # ASCII location in that case.
    for raw in (os.environ.get("LOCALAPPDATA"), os.environ.get("TEMP"), str(Path.home())):
        if raw and path_is_ascii(raw):
            return Path(raw) / "MMDCharacterImporter" / "external_tool_staging"
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
        candidate = system_root / "Temp"
        if path_is_ascii(candidate):
            return candidate / "MMDCharacterImporter" / "external_tool_staging"
        return Path(system_root.anchor or "C:\\") / "MMDCharacterImporter" / "external_tool_staging"
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or Path.home())
    return base / "MMDCharacterImporter" / "external_tool_staging"


def staging_key(path: Path | str) -> str:
    text = str(path)
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return digest


def external_safe_dir_for(path: Path | str, prefix: str) -> Path:
    return external_tool_root() / f"{prefix}_{staging_key(path)}"


def external_safe_qc_source_dir(qc_dir: Path) -> Path:
    return external_safe_dir_for(qc_dir, "qc") / "0_qc_source"


def ensure_external_safe_qc_source(plan: dict[str, Any], warnings: list[str]) -> None:
    qc_dir = Path(str(plan.get("qc_dir") or ""))
    raw_source_dir = str(plan.get("source_dir") or "").strip()
    if not raw_source_dir or not path_is_ascii(raw_source_dir):
        safe_source_dir = external_safe_qc_source_dir(qc_dir)
        plan["source_dir"] = str(safe_source_dir)
        warnings.append(
            f"QC source was staged in an ASCII-only path for StudioMDL compatibility: {safe_source_dir}"
        )


def safe_name(value: str, fallback: str = "model") -> str:
    text = SAFE_RE.sub("_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:80] or fallback


def safe_lower(value: str, fallback: str = "model") -> str:
    return safe_name(value, fallback).lower()


def safe_internal_identifier(value: str, fallback: str = "model") -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    if text and text[0].isdigit():
        text = "_" + text
    return text[:80] or fallback


def strip_model_hash_suffix(value: str) -> str:
    """Drop the trailing short-hash suffix that auto-derived internal names carry."""
    raw = str(value or "")
    cleaned = re.sub(r"_[0-9a-f]{6,12}$", "", raw, flags=re.IGNORECASE)
    return cleaned if cleaned.strip("_ ") else raw


def safe_display_identifier(value: str, fallback: str = "Model") -> str:
    text = re.sub(r"[^A-Za-z0-9_ ]+", "", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or fallback


def safe_category_display_identifier(value: str, fallback: str = "Sheepy Lord") -> str:
    text = re.sub(r"[^\x20-\x7E]+", "", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or fallback


def display_from_identifier(value: str, fallback: str = "Model") -> str:
    words = [word for word in str(value or "").replace("_", " ").split() if word]
    return safe_display_identifier(" ".join(word[:1].upper() + word[1:] for word in words), fallback)


def category_display_from_identifier(value: str, fallback: str = "Sheepy Lord") -> str:
    key = str(value or "").casefold()
    if key in KNOWN_CATEGORY_READABLE_OVERRIDES:
        return KNOWN_CATEGORY_READABLE_OVERRIDES[key]
    words = [word for word in str(value or "").replace("_", " ").split() if word]
    display = " ".join(word[:1].upper() + word[1:] for word in words)
    return safe_category_display_identifier(display or fallback, fallback)


def lua_string(value: object) -> str:
    text = str(value or "")
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "")
    return f'"{text}"'


def natural_key(value: object) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value))]


def file_row(path: Path, stage: str, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": path.name,
        "type": path.suffix.lower().lstrip("."),
        "path": str(path),
        "size": path.stat().st_size if path.exists() else 0,
        "stage": stage,
        "warnings": warnings or [],
        "exists": path.exists(),
    }


def folder_row(path: Path, stage: str, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": path.name,
        "type": "folder",
        "path": str(path),
        "size": 0,
        "stage": stage,
        "warnings": warnings or [],
        "exists": path.exists(),
    }


def resolve_workspace_root(input_path: Path) -> Path:
    path = input_path.resolve()
    if path.name == "2_proportion_export" and path.parent.name == "9_export_proportion_trick":
        return path.parent.parent
    if path.name == "9_export_proportion_trick":
        return path.parent
    if path.is_file():
        path = path.parent
    for parent in [path] + list(path.parents):
        if (parent / "9_export_proportion_trick" / "2_proportion_export").exists():
            return parent
        if parent.name.startswith("workspaces"):
            break
    return path.parent if path.name == "2_proportion_export" else path


def resolve_step9_dir(input_path: Path) -> tuple[Path, Path]:
    path = input_path.resolve()
    workspace = resolve_workspace_root(path)
    if path.name == "2_proportion_export":
        final_dir = path
    elif path.name == "9_export_proportion_trick":
        final_dir = path / "2_proportion_export"
    elif (path / "9_export_proportion_trick" / "2_proportion_export").exists():
        final_dir = path / "9_export_proportion_trick" / "2_proportion_export"
        workspace = path
    else:
        final_dir = path
    return workspace, final_dir


def qc_paths_for_input(input_path: Path) -> dict[str, Path]:
    workspace, step9_dir = resolve_step9_dir(input_path)
    qc_dir = workspace / STEP_DIR_NAME
    return {
        "workspace_root": workspace,
        "step9_dir": step9_dir,
        "qc_dir": qc_dir,
        "analysis": qc_dir / "qc_analysis.json",
        "plan": qc_dir / "qc_plan.json",
        "report": qc_dir / "qc_report.json",
        "files": qc_dir / "qc_files.json",
        "log": qc_dir / "qc_compile.log",
        "source_dir": external_safe_qc_source_dir(qc_dir),
        "compiled_dir": qc_dir / "1_compiled_from_gmod",
    }


def steam_library_roots() -> list[Path]:
    roots: list[Path] = []
    steam = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"
    candidates = [steam / "steamapps" / "libraryfolders.vdf"]
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        candidates.append(Path(f"{drive}:/SteamLibrary/steamapps/libraryfolders.vdf"))
    for vdf in candidates:
        if not vdf.exists():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            root = Path(raw.replace("\\\\", "\\"))
            if root.exists():
                roots.append(root)
        base = vdf.parent.parent
        if base.exists():
            roots.append(base)
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        key = str(root.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(root.resolve())
    return out


def validate_gmod_root(root: Path) -> dict[str, str] | None:
    if root.name.lower() == "garrysmod" and (root / "gameinfo.txt").exists():
        install_root = root.parent
        game_dir = root
    else:
        install_root = root
        game_dir = root / "garrysmod"
    studiomdl = install_root / "bin" / "studiomdl.exe"
    if studiomdl.exists() and (game_dir / "gameinfo.txt").exists():
        return {"install_root": str(install_root), "game_dir": str(game_dir), "studiomdl_path": str(studiomdl)}
    return None


def detect_gmod(explicit_root: str = "", explicit_studiomdl: str = "") -> dict[str, Any]:
    candidates: list[Path] = []
    if explicit_studiomdl:
        exe = Path(explicit_studiomdl)
        if exe.exists():
            root = exe.parent.parent
            hit = validate_gmod_root(root)
            if hit:
                hit["source"] = "explicit_studiomdl"
                return hit
    for raw in [explicit_root, os.environ.get("GMOD_PATH", ""), os.environ.get("GARRYSMOD_PATH", "")]:
        if raw:
            candidates.append(Path(raw))
    env_studiomdl = os.environ.get("STUDIOMDL", "")
    if env_studiomdl:
        exe = Path(env_studiomdl)
        if exe.exists():
            candidates.append(exe.parent.parent)
    for library in steam_library_roots():
        candidates.append(library / "steamapps" / "common" / "GarrysMod")
        candidates.append(library / "steamapps" / "common" / "GarrysMod_RTX")
        candidates.append(library / "steamapps" / "common" / "GarrysMod_RTX_c")
    candidates.extend(
        [
            Path(r"H:\SteamLibrary\steamapps\common\GarrysMod_RTX"),
            Path(r"H:\SteamLibrary\steamapps\common\GarrysMod_RTX_c"),
            Path(r"H:\SteamLibrary\steamapps\common\GarrysMod"),
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\GarrysMod"),
        ]
    )
    seen: set[str] = set()
    checked: list[str] = []
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except Exception:
            pass
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        checked.append(str(candidate))
        hit = validate_gmod_root(candidate)
        if hit:
            hit["source"] = "detected"
            hit["checked"] = checked
            return hit
    return {"install_root": "", "game_dir": "", "studiomdl_path": "", "source": "not_found", "checked": checked}


def validate_l4d2_root(root: Path) -> dict[str, str] | None:
    # The install root is the "Left 4 Dead 2" folder (holds bin/studiomdl.exe);
    # the game dir is its "left4dead2" subfolder (holds gameinfo.txt).
    if root.name.lower() == "left4dead2" and (root / "gameinfo.txt").exists():
        install_root = root.parent
        game_dir = root
    else:
        install_root = root
        game_dir = root / "left4dead2"
    studiomdl = install_root / "bin" / "studiomdl.exe"
    if studiomdl.exists() and (game_dir / "gameinfo.txt").exists():
        return {"install_root": str(install_root), "game_dir": str(game_dir), "studiomdl_path": str(studiomdl)}
    return None


def detect_l4d2(explicit_root: str = "", explicit_studiomdl: str = "") -> dict[str, Any]:
    candidates: list[Path] = []
    if explicit_studiomdl:
        exe = Path(explicit_studiomdl)
        if exe.exists():
            hit = validate_l4d2_root(exe.parent.parent)
            if hit:
                hit["source"] = "explicit_studiomdl"
                return hit
    for raw in [explicit_root, os.environ.get("L4D2_PATH", ""), os.environ.get("LEFT4DEAD2_PATH", "")]:
        if raw:
            candidates.append(Path(raw))
    env_studiomdl = os.environ.get("STUDIOMDL", "")
    if env_studiomdl:
        exe = Path(env_studiomdl)
        if exe.exists():
            candidates.append(exe.parent.parent)
    for library in steam_library_roots():
        candidates.append(library / "steamapps" / "common" / "Left 4 Dead 2")
    seen: set[str] = set()
    checked: list[str] = []
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except Exception:
            pass
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        checked.append(str(candidate))
        hit = validate_l4d2_root(candidate)
        if hit:
            hit["source"] = "detected"
            hit["checked"] = checked
            return hit
    return {"install_root": "", "game_dir": "", "studiomdl_path": "", "source": "not_found", "checked": checked}


def validate_sfm_root(root: Path) -> dict[str, str] | None:
    # Source Filmmaker layout: <SourceFilmmaker>/game/bin/studiomdl.exe (+ vpk.exe)
    # and <SourceFilmmaker>/game/usermod/gameinfo.txt (the loose-file content mod dir).
    # Accept the SourceFilmmaker root, its "game" folder, or the "usermod" folder.
    name = root.name.lower()
    if name == "usermod" and (root / "gameinfo.txt").exists():
        install_root = root.parent
        game_dir = root
    elif name == "game" and (root / "bin" / "studiomdl.exe").exists():
        install_root = root
        game_dir = root / "usermod"
    elif (root / "game" / "bin" / "studiomdl.exe").exists():
        install_root = root / "game"
        game_dir = install_root / "usermod"
    else:
        install_root = root
        game_dir = root / "usermod"
    studiomdl = install_root / "bin" / "studiomdl.exe"
    if studiomdl.exists() and (game_dir / "gameinfo.txt").exists():
        return {"install_root": str(install_root), "game_dir": str(game_dir), "studiomdl_path": str(studiomdl)}
    return None


def detect_sfm(explicit_root: str = "", explicit_studiomdl: str = "") -> dict[str, Any]:
    candidates: list[Path] = []
    if explicit_studiomdl:
        exe = Path(explicit_studiomdl)
        if exe.exists():
            # studiomdl lives in <game>/bin, so its grandparent is the <game> install root.
            hit = validate_sfm_root(exe.parent.parent)
            if hit:
                hit["source"] = "explicit_studiomdl"
                return hit
    for raw in [explicit_root, os.environ.get("SFM_PATH", ""), os.environ.get("SOURCEFILMMAKER_PATH", "")]:
        if raw:
            candidates.append(Path(raw))
    env_studiomdl = os.environ.get("STUDIOMDL", "")
    if env_studiomdl:
        exe = Path(env_studiomdl)
        if exe.exists():
            candidates.append(exe.parent.parent)
    for library in steam_library_roots():
        candidates.append(library / "steamapps" / "common" / "SourceFilmmaker")
    seen: set[str] = set()
    checked: list[str] = []
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except Exception:
            pass
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        checked.append(str(candidate))
        hit = validate_sfm_root(candidate)
        if hit:
            hit["source"] = "detected"
            hit["checked"] = checked
            return hit
    return {"install_root": "", "game_dir": "", "studiomdl_path": "", "source": "not_found", "checked": checked}


def detect_game(game: str, explicit_root: str = "", explicit_studiomdl: str = "") -> dict[str, Any]:
    """Detect the Source install (studiomdl + game dir) for the target game."""
    target = normalize_game(game)
    if target == "l4d2":
        return detect_l4d2(explicit_root, explicit_studiomdl)
    if target == "sfm":
        return detect_sfm(explicit_root, explicit_studiomdl)
    return detect_gmod(explicit_root, explicit_studiomdl)


def find_vtfcmd() -> Path | None:
    env_path = os.environ.get("VTFCMD")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    for candidate in bundled_vtfcmd_candidates():
        if candidate.exists():
            return candidate
    hit = shutil.which("VTFCmd.exe") or shutil.which("VTFCmd")
    if hit:
        return Path(hit)
    for raw in (
        r"C:\Users\1peng\Modding\Plugins\vtflib132-bin\bin\x64\VTFCmd.exe",
        r"C:\Program Files\VTFEdit\VTFCmd.exe",
        r"C:\Program Files (x86)\VTFEdit\VTFCmd.exe",
        r"C:\Program Files\Nem's Tools\VTFEdit\VTFCmd.exe",
        r"C:\Program Files (x86)\Nem's Tools\VTFEdit\VTFCmd.exe",
    ):
        candidate = Path(raw)
        if candidate.exists():
            return candidate
    return None


def bundled_vtfcmd_candidates() -> list[Path]:
    roots = [ROOT]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.extend([Path(sys.executable).resolve().parent, Path(sys.executable).resolve().parent / "_internal"])

    env_path = os.environ.get("MCI_BUNDLED_VTFCMD")
    candidates: list[Path] = [Path(env_path)] if env_path else []
    for root in roots:
        candidates.append(root / "external_tools" / "vtfcmd" / "VTFCmd.exe")

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


@dataclass
class SmdNode:
    index: int
    name: str
    parent: int
    local_pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    local_rot: tuple[float, float, float] = (0.0, 0.0, 0.0)
    global_pos: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class SmdData:
    path: Path
    nodes: dict[int, SmdNode] = field(default_factory=dict)
    triangles: list[dict[str, Any]] = field(default_factory=list)
    material_names: set[str] = field(default_factory=set)
    triangle_count: int = 0


@dataclass(frozen=True)
class DefineBone:
    name: str
    parent: str
    line_index: int
    values: tuple[float, ...] = ()
    value_tokens: tuple[str, ...] = ()
    raw_line: str = ""


@dataclass(frozen=True)
class SmdBonePose:
    name: str
    parent: str
    local_pos: tuple[float, float, float]
    local_rot: tuple[float, float, float]
    source_smd: str


def rotation_matrix_xyz(rx: float, ry: float, rz: float) -> list[list[float]]:
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rxm = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    rym = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rzm = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    return matmul(rzm, matmul(rym, rxm))


def matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def matvec(a: list[list[float]], v: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[0][0] * v[0] + a[0][1] * v[1] + a[0][2] * v[2],
        a[1][0] * v[0] + a[1][1] * v[1] + a[1][2] * v[2],
        a[2][0] * v[0] + a[2][1] * v[1] + a[2][2] * v[2],
    )


def add3(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def compute_global_bones(nodes: dict[int, SmdNode]) -> None:
    global_rot: dict[int, list[list[float]]] = {}
    pending = set(nodes)
    while pending:
        progressed = False
        for index in list(pending):
            node = nodes[index]
            if node.parent >= 0 and node.parent in pending:
                continue
            local_rot = rotation_matrix_xyz(*node.local_rot)
            if node.parent >= 0 and node.parent in nodes:
                parent = nodes[node.parent]
                parent_rot = global_rot.get(node.parent, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
                node.global_pos = add3(parent.global_pos, matvec(parent_rot, node.local_pos))
                global_rot[index] = matmul(parent_rot, local_rot)
            else:
                node.global_pos = node.local_pos
                global_rot[index] = local_rot
            pending.remove(index)
            progressed = True
        if not progressed:
            for index in list(pending):
                node = nodes[index]
                node.global_pos = node.local_pos
                global_rot[index] = rotation_matrix_xyz(*node.local_rot)
                pending.remove(index)


def parse_vertex(line: str) -> dict[str, Any] | None:
    parts = line.split()
    if len(parts) < 9:
        return None
    try:
        parent = int(parts[0])
        pos = [float(parts[1]), float(parts[2]), float(parts[3])]
        normal = [float(parts[4]), float(parts[5]), float(parts[6])]
        uv = [float(parts[7]), float(parts[8])]
        weights: list[tuple[int, float]] = []
        if len(parts) >= 10:
            link_count = int(float(parts[9]))
            offset = 10
            for index in range(link_count):
                if offset + index * 2 + 1 >= len(parts):
                    break
                bone = int(float(parts[offset + index * 2]))
                weight = float(parts[offset + index * 2 + 1])
                weights.append((bone, weight))
        if not weights:
            weights.append((parent, 1.0))
        return {"pos": pos, "normal": normal, "uv": uv, "weights": weights}
    except Exception:
        return None


def parse_smd(path: Path, include_triangles: bool = True) -> SmdData:
    data = SmdData(path=path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    state = ""
    skeleton_time_zero = False
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if line in {"nodes", "skeleton", "triangles"}:
            state = line
            continue
        if line == "end":
            state = ""
            continue
        if state == "nodes":
            match = re.match(r'(-?\d+)\s+"(.*)"\s+(-?\d+)', line)
            if match:
                bone_id = int(match.group(1))
                data.nodes[bone_id] = SmdNode(bone_id, match.group(2), int(match.group(3)))
            continue
        if state == "skeleton":
            if line.startswith("time"):
                skeleton_time_zero = line == "time 0"
                continue
            if not skeleton_time_zero:
                continue
            parts = line.split()
            if len(parts) >= 7:
                try:
                    bone_id = int(parts[0])
                    if bone_id in data.nodes:
                        data.nodes[bone_id].local_pos = (float(parts[1]), float(parts[2]), float(parts[3]))
                        data.nodes[bone_id].local_rot = (float(parts[4]), float(parts[5]), float(parts[6]))
                except Exception:
                    pass
            continue
        if state == "triangles" and include_triangles:
            material = line
            if material == "end":
                state = ""
                continue
            verts = []
            for _ in range(3):
                if index >= len(lines):
                    break
                vertex = parse_vertex(lines[index].strip())
                index += 1
                if vertex:
                    verts.append(vertex)
            if len(verts) == 3:
                data.triangle_count += 1
                data.material_names.add(material)
                data.triangles.append({"material": material, "vertices": verts, "object": path.stem})
    compute_global_bones(data.nodes)
    return data


def is_essential_bone(name: str) -> bool:
    return name.startswith("ValveBiped.") or name in ESSENTIAL_EXACT


def lower_name(name: str) -> str:
    return name.lower().replace(" ", "_")


def nearest_essential_distance(pos: tuple[float, float, float], essentials: list[tuple[str, tuple[float, float, float]]]) -> tuple[str, float]:
    best_name = ""
    best_dist = float("inf")
    for name, epos in essentials:
        dist = math.sqrt(sum((pos[i] - epos[i]) ** 2 for i in range(3)))
        if dist < best_dist:
            best_dist = dist
            best_name = name
    return best_name, best_dist


def essential_ancestor(node: SmdNode, nodes: dict[int, SmdNode]) -> str:
    parent = node.parent
    while parent >= 0 and parent in nodes:
        pname = nodes[parent].name
        if is_essential_bone(pname):
            return pname
        parent = nodes[parent].parent
    return ""


# Flex-driven facial-detail bone name tokens. Mirrors FACE_NAME_HINTS in
# blender_sort_bones.py (keep in sync): the face animates via flexes, not bones,
# so these are never jigglebones. The "lip" token also appears inside "clip"
# (a real jiggle accessory) — the caller only applies this after excluding
# jiggle-hinted names, which resolves that overlap.
FACIAL_FLEX_TOKENS = (
    "eye", "eyebrow", "eyelid", "brow", "nose", "tongue", "tooth", "teeth",
    "mouth", "lip", "jaw", "cheek", "beak", "angulusoris", "face",
)


def is_facial_flex_bone(name: str, node: SmdNode, nodes: dict[int, SmdNode]) -> bool:
    """A flex-driven facial detail bone living under Head1 (never a jigglebone).

    Step 4 normally merges these into Head1; this catches any that survive
    (manual/legacy runs, or non-merged inputs) so auto-select never turns an
    eye/brow/nose/tongue/teeth/lip/eyelid/mouth bone into a jigglebone.
    """
    lname = lower_name(name)
    if not any(token in lname for token in FACIAL_FLEX_TOKENS):
        return False
    return essential_ancestor(node, nodes) == "ValveBiped.Bip01_Head1"


def is_frontal_head_flex_bone(
    node: SmdNode,
    nodes: dict[int, SmdNode],
    head_pos: tuple[float, float, float] | None,
    direct_children: int,
    front_margin: float,
) -> bool:
    """A childless bone sitting clearly in front of the head pivot (issue #103).

    Structural counterpart to the name-based is_facial_flex_bone(): flex/
    expression detail bones (eyes, mouth, teeth, tongue, cheeks and any unnamed
    or localized facial helpers) live on the face -- in front of the Head1 pivot
    -- and are leaf bones, because the source drives the face with flexes, not
    bone chains. They must never become jigglebones, even when their names are
    unrecognized. This catches the cases the token list misses.

    "Front" is the -Y side of the head pivot in the standardized skeleton frame
    (matching the $attachment "eyes"/"mouth" offsets and orientation_code()).
    The caller scales front_margin to ~0.45x the neck->head length so genuine
    facial bones (~1 head-radius forward) qualify while near-pivot side
    accessories (earrings, short floating ribbons) do not. The caller also only
    applies this to non-hinted bones, so named hair/ribbon/cloth (bangs included)
    stay jiggle-eligible.
    """
    if direct_children != 0:
        return False
    if head_pos is None:
        return False
    if essential_ancestor(node, nodes) != "ValveBiped.Bip01_Head1":
        return False
    return node.global_pos[1] < head_pos[1] - front_margin


def weighted_stats_from_smds(smds: list[SmdData], nodes: dict[int, SmdNode]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {
        node.name: {"weighted_vertices": 0, "weight_sum": 0.0, "pos_sum": [0.0, 0.0, 0.0]}
        for node in nodes.values()
    }
    for smd in smds:
        if smd.path.name.lower() == "physics.smd":
            continue
        for triangle in smd.triangles:
            for vertex in triangle["vertices"]:
                pos = vertex["pos"]
                for bone_id, weight in vertex["weights"]:
                    if weight <= 0.001 or bone_id not in nodes:
                        continue
                    name = nodes[bone_id].name
                    entry = stats.setdefault(name, {"weighted_vertices": 0, "weight_sum": 0.0, "pos_sum": [0.0, 0.0, 0.0]})
                    entry["weighted_vertices"] += 1
                    entry["weight_sum"] += float(weight)
                    entry["pos_sum"][0] += pos[0] * float(weight)
                    entry["pos_sum"][1] += pos[1] * float(weight)
                    entry["pos_sum"][2] += pos[2] * float(weight)
    for name, entry in stats.items():
        total = float(entry.get("weight_sum") or 0.0)
        if total > 1e-8:
            entry["centroid"] = [round(float(v) / total, 6) for v in entry["pos_sum"]]
        elif name in [node.name for node in nodes.values()]:
            node = next((node for node in nodes.values() if node.name == name), None)
            entry["centroid"] = [round(v, 6) for v in (node.global_pos if node else (0.0, 0.0, 0.0))]
        entry.pop("pos_sum", None)
    return stats


def classify_region(pos: tuple[float, float, float], landmarks: dict[str, tuple[float, float, float]]) -> str:
    pelvis = landmarks.get("ValveBiped.Bip01_Pelvis", (0.0, 0.0, 0.0))
    spine4 = landmarks.get("ValveBiped.Bip01_Spine4", pelvis)
    neck = landmarks.get("ValveBiped.Bip01_Neck1", spine4)
    side = "center"
    if pos[0] > pelvis[0] + 0.4:
        side = "left"
    elif pos[0] < pelvis[0] - 0.4:
        side = "right"
    if pos[2] >= neck[2]:
        base = "head"
    elif pos[2] >= spine4[2]:
        base = "upper"
    elif pos[2] >= pelvis[2]:
        base = "torso"
    else:
        base = "lower"
    return f"{base}_{side}"


def orientation_code(pos: tuple[float, float, float], landmarks: dict[str, tuple[float, float, float]]) -> tuple[float, float, float, float]:
    pelvis = landmarks.get("ValveBiped.Bip01_Pelvis", (0.0, 0.0, 0.0))
    spine4 = landmarks.get("ValveBiped.Bip01_Spine4", pelvis)
    neck = landmarks.get("ValveBiped.Bip01_Neck1", spine4)
    angle = DEFAULT_DIRECTIONAL_JIGGLE_ANGLE
    x, y, z = pos
    if z < spine4[2]:
        ratio_yx = (y - pelvis[1]) / (x - pelvis[0]) if abs(x - pelvis[0]) > 1e-6 else float("inf")
        if y < pelvis[1] and abs(ratio_yx) > 5:
            return (-2, angle, -2, -2)
        if y > pelvis[1] and abs(ratio_yx) > 5:
            return (-angle, 2, -2, -2)
        if x > pelvis[0] and abs(ratio_yx) < 1:
            return (-2, -2, -2, angle)
        if x < pelvis[0] and abs(ratio_yx) < 1:
            return (-2, -2, -angle, 2)
    ratio_yx = (y - neck[1]) / (x - neck[0]) if abs(x - neck[0]) > 1e-6 else float("inf")
    if y < neck[1] and abs(ratio_yx) > 2:
        return (-2, angle, -2, -2)
    if y < neck[1] and abs(ratio_yx) < 2 and x > neck[0]:
        return (-2, angle, -2, angle)
    if y < neck[1] and abs(ratio_yx) < 2 and x < neck[0]:
        return (-2, angle, -angle, 2)
    if y > neck[1] and abs(ratio_yx) > 2:
        return (-angle, 2, -2, -2)
    if y > neck[1] and abs(ratio_yx) < 2 and x > neck[0]:
        return (-angle, 2, -2, angle)
    if y > neck[1] and abs(ratio_yx) < 2 and x < neck[0]:
        return (-angle, 2, -angle, 2)
    if x > neck[0] and abs(ratio_yx) < 0.66:
        return (-2, -2, -2, angle)
    if x < neck[0] and abs(ratio_yx) < 0.66:
        return (-2, -2, -angle, 2)
    return (-10, 10, -10, 10)


def default_jiggle_params(jiggle_type: str) -> dict[str, float]:
    kind = str(jiggle_type or "Directional Jiggle")
    if kind == "Spring Jiggle":
        return {
            "length": 15.0,
            "tip_mass": 30.0,
            "pitch_stiffness": 40.0,
            "pitch_damping": 5.0,
            "yaw_stiffness": 40.0,
            "yaw_damping": 3.0,
            "along_stiffness": 100.0,
            "along_damping": 0.0,
            "angle_constraint": 14.0,
            "base_mass": 15.0,
            "base_stiffness": 50.0,
            "base_damping": 6.0,
            "left_min": -0.15,
            "left_max": 0.15,
            "left_friction": 0.01,
            "up_min": -0.15,
            "up_max": 0.15,
            "up_friction": 0.01,
            "forward_min": -0.01,
            "forward_max": 0.01,
            "forward_friction": 0.05,
        }
    if kind == "Omni Jiggle":
        return {
            "length": 15.0,
            "tip_mass": 0.0,
            "pitch_stiffness": 10.0,
            "pitch_damping": 2.0,
            "yaw_stiffness": 10.0,
            "yaw_damping": 2.0,
            "along_stiffness": 100.0,
            "along_damping": 0.0,
            "angle_constraint": 30.0,
        }
    return {
        "length": 10.0,
        "tip_mass": 100.0,
        "pitch_stiffness": 25.0,
        "pitch_damping": 2.0,
        "yaw_stiffness": 25.0,
        "yaw_damping": 2.0,
        "along_stiffness": 50.0,
        "along_damping": 1.0,
        "angle_constraint": 90.0,
    }


def classify_jigglebones(nodes: dict[int, SmdNode], stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    essentials = [(node.name, node.global_pos) for node in nodes.values() if is_essential_bone(node.name)]
    landmarks = {node.name: node.global_pos for node in nodes.values()}
    positions = [node.global_pos for node in nodes.values()]
    if positions:
        mins = [min(pos[i] for pos in positions) for i in range(3)]
        maxs = [max(pos[i] for pos in positions) for i in range(3)]
        extent = max(maxs[i] - mins[i] for i in range(3))
    else:
        extent = 1.0
    near_threshold = max(0.18, extent * 0.012)
    # Issue #103: stricter head jigglebone selection (both GMod and L4D2). A
    # non-hinted childless bone must sit at least this far in front (toward the
    # face, -Y) of the head pivot to be treated as a flex/expression bone. The
    # margin scales to ~0.45x the neck->head length so genuine facial bones
    # (eyes/mouth/teeth ~1 head-radius forward) qualify while near-pivot side
    # accessories (earrings, short floating ribbons) are spared.
    head_pos = landmarks.get("ValveBiped.Bip01_Head1")
    neck_pos = landmarks.get("ValveBiped.Bip01_Neck1")
    if head_pos is not None and neck_pos is not None:
        head_scale = math.sqrt(sum((head_pos[i] - neck_pos[i]) ** 2 for i in range(3)))
    else:
        head_scale = extent * 0.05
    front_margin = max(0.5, head_scale * 0.45)
    jiggle_hints = (
        "hair", "cape", "ribbon", "plait", "ear", "tail", "skirt", "sleeve", "cloth", "clothes",
        "coat", "robe", "dress", "collar", "belt", "chain", "boot", "breast", "butt", "chest",
        "zip", "phone", "clip", "hat", "tie", "knot", "strap", "髪", "发", "髮", "裙", "袖",
        "衣", "带", "帯", "リボン", "耳", "飘", "布",
    )
    omni_hints = (
        "boot", "collar", "phone", "clip", "chain", "belt", "zip", "accessory", "ornament",
        "device", "strap", "sleevee", "capef", "capee", "knief", "knife", "weapon",
    )
    spring_hints = ("breast", "chest", "butt", "胸", "乳")
    rows: list[dict[str, Any]] = []
    pelvis_x = float(landmarks.get("ValveBiped.Bip01_Pelvis", (0.0, 0.0, 0.0))[0])
    direct_child_counts: dict[int, int] = {}
    for node in nodes.values():
        if node.parent in nodes and node.parent != node.index:
            direct_child_counts[node.parent] = direct_child_counts.get(node.parent, 0) + 1
    for node in sorted(nodes.values(), key=lambda item: item.index):
        name = node.name
        stat = stats.get(name, {})
        weighted = int(stat.get("weighted_vertices", 0) or 0)
        centroid = stat.get("centroid")
        if isinstance(centroid, list) and len(centroid) >= 3:
            ref_pos = (float(centroid[0]), float(centroid[1]), float(centroid[2]))
        else:
            ref_pos = node.global_pos
        nearest_name, nearest_dist = nearest_essential_distance(node.global_pos, essentials)
        lname = lower_name(name)
        parent_name = nodes[node.parent].name if node.parent in nodes else ""
        ancestor = essential_ancestor(node, nodes)
        warnings: list[str] = []
        confidence = 0.0
        jiggle_type = "Not Jiggle"
        reason = "no jiggle evidence"
        hint_hit = any(hint in lname or hint in name for hint in jiggle_hints)
        if is_essential_bone(name):
            reason = "essential Source/twist/eye bone"
            confidence = 1.0
        else:
            near_support = nearest_dist <= near_threshold and weighted < 25 and not hint_hit
            if near_support:
                reason = f"support/helper near {nearest_name}"
                confidence = 0.86
            elif any(hint in lname or hint in name for hint in spring_hints):
                jiggle_type = "Spring Jiggle"
                reason = "spring-like body part name"
                confidence = 0.82
            elif any(hint in lname or hint in name for hint in omni_hints):
                jiggle_type = "Omni Jiggle"
                reason = "loose accessory/clothes helper"
                confidence = 0.78
            elif hint_hit:
                jiggle_type = "Directional Jiggle"
                reason = "hair/clothes/skirt/cape name"
                confidence = 0.74
            elif weighted >= 40 and ancestor in {
                "ValveBiped.Bip01_Pelvis",
                "ValveBiped.Bip01_Spine",
                "ValveBiped.Bip01_Spine1",
                "ValveBiped.Bip01_Spine2",
                "ValveBiped.Bip01_Spine4",
                "ValveBiped.Bip01_Head1",
                "ValveBiped.Bip01_Neck1",
            }:
                jiggle_type = "Directional Jiggle"
                reason = "weighted nonessential torso/head branch"
                confidence = 0.55
                warnings.append("Low-confidence jiggle classification; verify manually.")
            else:
                confidence = 0.7
        direct_children = direct_child_counts.get(node.index, 0)
        # Guard rail: flex-driven facial detail bones under Head1 (eyes/brows/
        # nose/tongue/teeth/lips/eyelids/mouth) never jiggle. Excluding
        # jiggle-hinted names (hint_hit) keeps real accessories such as hairclips
        # ("clip" contains "lip") jiggle-eligible. Step 4 usually merges these
        # into Head1; this protects manual/legacy runs where they survive.
        if jiggle_type != "Not Jiggle" and not hint_hit and is_facial_flex_bone(name, node, nodes):
            jiggle_type = "Not Jiggle"
            reason = "flex-driven facial detail bone under Head1; never a jigglebone"
            confidence = 0.95
        # Guard rail (issue #103): structural flex-bone filter. A non-hinted
        # childless bone clearly in front of the head pivot is a flex/expression
        # helper even when its name is unrecognized or localized -- catches the
        # cases the FACIAL_FLEX_TOKENS list above misses. Hinted bones (hair/
        # bangs/ribbon/cloth) also sit in front but stay jiggle-eligible.
        if (
            jiggle_type != "Not Jiggle"
            and not hint_hit
            and is_frontal_head_flex_bone(node, nodes, head_pos, direct_children, front_margin)
        ):
            jiggle_type = "Not Jiggle"
            reason = "flex-driven facial bone in front of head with no children; never a jigglebone"
            confidence = 0.93
        if jiggle_type != "Not Jiggle" and direct_children > 4:
            jiggle_type = "Not Jiggle"
            reason = f"hub bone with {direct_children} direct children (more than 4); defaulted to no jiggle"
            confidence = 0.9
        region = classify_region(ref_pos, landmarks)
        pitch_min, pitch_max, yaw_min, yaw_max = orientation_code(ref_pos, landmarks)
        rows.append(
            {
                "uid": f"bone_{node.index:03d}",
                "bone": name,
                "parent": parent_name,
                "essential": is_essential_bone(name),
                "direct_children": direct_children,
                "jiggle_type": jiggle_type,
                "region": region,
                "confidence": round(confidence, 3),
                "weighted_vertices": weighted,
                "bone_position": [round(value, 6) for value in node.global_pos],
                "centroid": [round(value, 6) for value in ref_pos],
                "nearest_essential": nearest_name,
                "nearest_essential_distance": round(nearest_dist, 6) if nearest_dist < float("inf") else None,
                "essential_ancestor": ancestor,
                "pitch_constraint": [pitch_min, pitch_max],
                "yaw_constraint": [yaw_min, yaw_max],
                "jiggle_params": default_jiggle_params(jiggle_type),
                "reason": reason,
                "warnings": warnings,
            }
        )
    enforce_spring_jiggle_default_limit(rows, pelvis_x)
    return rows


def spring_jiggle_side(row: dict[str, Any], pelvis_x: float = 0.0) -> str:
    for key in ("bone_position", "centroid"):
        position = row.get(key)
        if not isinstance(position, list) or len(position) < 1:
            continue
        try:
            x = float(position[0])
            if abs(x - pelvis_x) > 1e-5:
                return "left" if x > pelvis_x else "right"
        except Exception:
            pass
    return "unknown"


def enforce_spring_jiggle_default_limit(rows: list[dict[str, Any]], pelvis_x: float = 0.0) -> bool:
    spring_rows = [row for row in rows if str(row.get("jiggle_type") or "") == "Spring Jiggle"]
    if len(spring_rows) <= 4:
        return False

    rows_by_side: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}
    unknown_rows: list[dict[str, Any]] = []
    for row in spring_rows:
        side = spring_jiggle_side(row, pelvis_x)
        row["spring_side"] = side
        if side in rows_by_side:
            rows_by_side[side].append(row)
        else:
            unknown_rows.append(row)

    def spring_priority(row: dict[str, Any]) -> tuple[int, list[object]]:
        name = str(row.get("bone") or "")
        return (len(name), natural_key(name))

    kept: set[int] = set()
    for side in ("left", "right"):
        for row in sorted(rows_by_side[side], key=spring_priority)[:2]:
            kept.add(id(row))

    disabled_rows = [row for row in spring_rows if id(row) not in kept]
    if not disabled_rows:
        return False

    kept_names = ", ".join(str(row.get("bone") or "") for row in sorted(spring_rows, key=spring_priority) if id(row) in kept)
    disabled_names = ", ".join(str(row.get("bone") or "") for row in sorted(disabled_rows, key=spring_priority) if row.get("bone"))
    unknown_names = ", ".join(str(row.get("bone") or "") for row in sorted(unknown_rows, key=spring_priority) if row.get("bone"))
    warning = (
        "More than four Spring Jiggle candidates were detected. Automatic Step 14 keeps at most two left-side and "
        f"two right-side spring bones by bone location, choosing the shortest bone names. Kept: {kept_names or 'none'}. "
        f"Set to Not Jiggle: {disabled_names or 'none'}."
    )
    if unknown_names:
        warning += f" Location side could not be determined for: {unknown_names}."
    for row in disabled_rows:
        row["jiggle_type"] = "Not Jiggle"
        row["reason"] = "spring jiggle disabled by four-bone automatic safety limit"
        row["confidence"] = min(float(row.get("confidence", 0.0) or 0.0), 0.5)
        row_warnings = row.setdefault("warnings", [])
        if isinstance(row_warnings, list):
            row_warnings.append(warning)
        else:
            row["warnings"] = [warning]
    return True


def parse_vta_flexes(path: Path) -> list[str]:
    if not path.exists():
        return []
    flexes: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("vertexanimation"):
            break
        if line.startswith("time"):
            parts = line.split("#", 1)
            if len(parts) == 2:
                try:
                    frame = int(line.split()[1])
                except Exception:
                    frame = 0
                if frame != 0:
                    flexes.append(parts[-1].strip().replace(" ", ""))
    return flexes


def material_uid(name: str) -> str:
    return f"mat_{safe_lower(name, 'material')}"


def build_texture_map(manifest_path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not manifest_path.exists():
        return out
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return out
    for row in manifest.get("textures", []):
        if not isinstance(row, dict):
            continue
        names = {
            str(row.get("material_name") or ""),
            str(row.get("output_name") or ""),
            Path(str(row.get("base_output_path") or "")).stem,
        }
        for name in names:
            key = safe_lower(name, "")
            if key:
                out[key] = row
    return out


def build_preview(smds: list[SmdData], texture_map: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    material_entries: dict[str, dict[str, Any]] = {}
    all_triangles: list[dict[str, Any]] = []
    source_count = 0
    for smd in smds:
        if smd.path.name.lower() == "physics.smd":
            continue
        for triangle in smd.triangles:
            source_count += 1
            mat_name = str(triangle["material"])
            uid = material_uid(mat_name)
            texture_row = texture_map.get(safe_lower(mat_name, ""))
            texture_path = str(texture_row.get("base_output_path") or "") if texture_row else ""
            if uid not in material_entries:
                material_entries[uid] = {
                    "uid": uid,
                    "material_name": mat_name,
                    "proposed_name": safe_name(mat_name, uid),
                    "bodygroup": triangle.get("object", ""),
                    "keep": True,
                    "alpha": 1.0,
                    "base_color_path": texture_path,
                    "preview_color": color_from_string(uid),
                }
            if len(all_triangles) < MAX_PREVIEW_TRIANGLES:
                # texture_path is intentionally omitted per triangle; the preview
                # resolves it from the material entry's base_color_path via material_uid.
                all_triangles.append(
                    {
                        "material_uid": uid,
                        "points": [vertex["pos"] for vertex in triangle["vertices"]],
                        "uvs": [vertex["uv"] for vertex in triangle["vertices"]],
                    }
                )
    return list(material_entries.values()), {"source_triangle_count": source_count, "triangles": all_triangles}


def color_from_string(value: str) -> list[float]:
    seed = sum((index + 1) * ord(char) for index, char in enumerate(value))
    hue = (seed % 360) / 360.0
    chroma = 0.55
    x = chroma * (1 - abs((hue * 6) % 2 - 1))
    if hue < 1 / 6:
        rgb = (chroma, x, 0.2)
    elif hue < 2 / 6:
        rgb = (x, chroma, 0.2)
    elif hue < 3 / 6:
        rgb = (0.2, chroma, x)
    elif hue < 4 / 6:
        rgb = (0.2, x, chroma)
    elif hue < 5 / 6:
        rgb = (x, 0.2, chroma)
    else:
        rgb = (chroma, 0.2, x)
    return [min(1.0, channel + 0.25) for channel in rgb] + [1.0]


def bone_preview(nodes: dict[int, SmdNode]) -> dict[str, Any]:
    bones = []
    for node in sorted(nodes.values(), key=lambda item: item.index):
        parent = nodes.get(node.parent)
        bones.append(
            {
                "uid": node.name,
                "name": node.name,
                "parent": parent.name if parent else "",
                "head": [round(value, 6) for value in node.global_pos],
                "tail": [round(value, 6) for value in (node.global_pos if not parent else node.global_pos)],
            }
        )
    return {"bones": bones}


def discover_inputs(input_path: Path) -> dict[str, Any]:
    paths = qc_paths_for_input(input_path)
    workspace = paths["workspace_root"]
    step9_dir = paths["step9_dir"]
    smd_files = sorted(step9_dir.glob("*.smd"), key=lambda path: natural_key(path.name))
    vta_files = sorted(step9_dir.glob("*.vta"), key=lambda path: natural_key(path.name))
    return {
        "workspace_root": str(workspace),
        "step9_dir": str(step9_dir),
        "smd_files": [str(path) for path in smd_files],
        "vta_files": [str(path) for path in vta_files],
        "anims_dir": str(step9_dir / "anims") if (step9_dir / "anims").exists() else "",
        "step10_dir": str(workspace / "10_sort_c_arms"),
        "step11_vrd": str(workspace / "11_sort_vrd" / "vrd.vrd"),
        "step12_manifest": str(workspace / "12_param_texture_render_materials" / "textures_manifest.json"),
        "step13_dir": str(workspace / "13_sort_icons_and_arts"),
        "step8_physics_settings": str(workspace / "8_sort_collision" / "physics_settings.json"),
    }


def is_auto_port_plan(plan: dict[str, Any]) -> bool:
    return bool(plan.get("auto_porting") or plan.get("auto_port"))


def missing_optional_canonical_smds(step9_dir: Path) -> list[str]:
    return [name for name in OPTIONAL_CANONICAL_MODEL_SMDS if not (step9_dir / name).exists()]


def optional_canonical_smd_warning(missing: list[str]) -> str:
    return (
        f"{OPTIONAL_CANONICAL_SMD_WARNING_PREFIX} {', '.join(missing)}. "
        "This is allowed for models without matching Face/Body bodygroups or flex keys; "
        "Step 14 will compile the available SMD bodygroups."
    )


def without_optional_canonical_smd_warnings(warnings: list[Any]) -> list[str]:
    return [
        str(warning)
        for warning in warnings
        if not str(warning).startswith(OPTIONAL_CANONICAL_SMD_WARNING_PREFIX)
    ]


def add_optional_canonical_smd_warnings(warnings: list[str], step9_dir: Path) -> None:
    missing = missing_optional_canonical_smds(step9_dir)
    if not missing:
        return
    warning = optional_canonical_smd_warning(missing)
    if warning not in warnings:
        warnings.append(warning)


INPUT_AVAILABILITY_WARNINGS = (
    ("step11_vrd", "Step 11 VRD file was not found; QC will omit $proceduralbones."),
    ("step12_manifest", "Step 12 texture manifest was not found; material conversion will be incomplete."),
    ("step13_dir", "Step 13 icons were not found; fallback entity icons will be used if available."),
)


def refresh_input_availability_warnings(plan: dict[str, Any], warnings: list[str]) -> None:
    """Re-evaluate optional-input warnings against the current filesystem.

    The plan snapshots these warnings at analyze time, but steps 11-13 are
    often run after Generate QC. Compile actually consumes whatever exists at
    compile time, so the report must not parrot stale "not found" messages
    (and must warn when an input vanished after the analyze).
    """
    inputs = plan.get("inputs", {}) if isinstance(plan.get("inputs"), dict) else {}
    # SFM ships loose models + materials only (no GMod spawn-menu icons), so a missing Step 13 icon
    # dir is expected and must not warn. VRD ($proceduralbones) is also opt-in / off by default for
    # SFM, so a missing Step 11 VRD is expected and must not warn either.
    sfm = normalize_game(plan.get("game")) == "sfm"
    skip_keys = {"step13_dir", "step11_vrd"} if sfm else set()
    messages = {message for _key, message in INPUT_AVAILABILITY_WARNINGS}
    warnings[:] = [warning for warning in warnings if str(warning) not in messages]
    for key, message in INPUT_AVAILABILITY_WARNINGS:
        if key in skip_keys:
            continue
        path_text = str(inputs.get(key) or "")
        if not path_text or not Path(path_text).exists():
            warnings.append(message)


def load_smds(smd_files: list[str]) -> tuple[list[SmdData], dict[int, SmdNode]]:
    smds = [parse_smd(Path(path), include_triangles=True) for path in smd_files]
    nodes: dict[int, SmdNode] = {}
    for smd in smds:
        if smd.nodes:
            nodes = smd.nodes
            break
    return smds, nodes


def analyze(input_path: Path, author: str = "", category: str = "", model_name: str = "", gmod_root: str = "", studiomdl_path: str = "", gender: str = "female", game: str = "gmod", survivor: str = DEFAULT_L4D2_SURVIVOR) -> dict[str, Any]:
    paths = qc_paths_for_input(input_path)
    qc_dir = paths["qc_dir"]
    qc_dir.mkdir(parents=True, exist_ok=True)
    discovered = discover_inputs(input_path)
    step9_dir = Path(discovered["step9_dir"])
    warnings: list[str] = []
    errors: list[str] = []
    if not step9_dir.exists():
        errors.append(f"Step 9 export folder was not found: {step9_dir}")
    smd_files = [path for path in discovered["smd_files"] if Path(path).exists()]
    if not smd_files:
        errors.append(f"No SMD files found in Step 9 export folder: {step9_dir}")
    add_optional_canonical_smd_warnings(warnings, step9_dir)
    if not (step9_dir / "Physics.smd").exists() and Path(discovered["step8_physics_settings"]).exists():
        errors.append("Physics.smd is missing but Step 8 collision settings exist.")
    if step9_dir.exists():
        for anim_name in ("reference_female.smd", "proportions.smd"):
            anim_path = step9_dir / "anims" / anim_name
            if not anim_path.exists():
                errors.append(
                    f"Required animation SMD is missing: {anim_path}. "
                    "The generated QC references anims/reference_female and anims/proportions and cannot compile without them."
                )
        if normalize_gender(gender) == "male" and not (step9_dir / "anims" / "reference_male.smd").exists():
            warnings.append(
                "anims/reference_male.smd was not found in the Step 9 export; the compile will fall back to the "
                "female reference pose. Re-run Step 9 to regenerate both reference animations."
            )
    texture_manifest = Path(discovered["step12_manifest"])
    # SFM: VRD ($proceduralbones) is opt-in and off by default, so a missing Step 11 VRD is
    # expected and must not warn.
    vrd_warning_skip = {"step11_vrd"} if normalize_game(game) == "sfm" else set()
    for key, message in INPUT_AVAILABILITY_WARNINGS:
        if key in vrd_warning_skip:
            continue
        if not Path(str(discovered.get(key) or "")).exists():
            warnings.append(message)

    smds, nodes = load_smds(smd_files) if smd_files else ([], {})
    stats = weighted_stats_from_smds(smds, nodes) if nodes else {}
    jiggle_rows = classify_jigglebones(nodes, stats) if nodes else []
    for row in jiggle_rows:
        for warning in row.get("warnings", []) if isinstance(row.get("warnings"), list) else []:
            text = str(warning)
            if "Spring Jiggle candidates were detected" in text and text not in warnings:
                warnings.append(text)
    texture_map = build_texture_map(texture_manifest)
    materials, model_preview = build_preview(smds, texture_map)
    bones_preview = bone_preview(nodes)
    game = normalize_game(game)
    gmod = detect_game(game, gmod_root, studiomdl_path)
    if not gmod.get("studiomdl_path"):
        game_label = "Left 4 Dead 2" if game == "l4d2" else "Garry's Mod"
        warnings.append(f"{game_label} StudioMDL was not detected; compile will require manual selection.")
    physics_globals, physics_rows, physics_collision_text_lines = physics_plan_from_qc_lines(collision_qc_block({"inputs": discovered}))

    workspace_name = Path(discovered["workspace_root"]).name
    workspace_stem = re.sub(r"_[0-9a-f]{8,}$", "", workspace_name, flags=re.IGNORECASE)
    default_model = safe_internal_identifier(model_name or workspace_stem, "mmd_model")
    if not model_name:
        # Auto-derived defaults collide between models ("mmd_model" is common);
        # reuse the workspace's PMX-hash suffix to keep them unique.
        hash_match = re.search(r"_([0-9a-f]{8,})$", workspace_name, flags=re.IGNORECASE)
        if hash_match and not re.search(r"_[0-9a-f]{6,12}$", default_model, flags=re.IGNORECASE):
            default_model = safe_internal_identifier(f"{default_model}_{hash_match.group(1)[:6].lower()}", default_model)
    # The author namespace is fixed; validate_plan() requires exactly "sheepylord".
    safe_author = "sheepylord"
    if author and safe_internal_identifier(author, safe_author).lower() != safe_author:
        warnings.append(
            f'The --author value "{author}" is ignored; the author namespace is fixed to "{safe_author}".'
        )
    safe_category = safe_internal_identifier(category or "SheepyLord", "SheepyLord")
    material_rows = material_plan_rows(texture_manifest)
    texturegroups = load_texture_groups_config(discovered.get("workspace_root"))
    plan = {
        "version": 1,
        "kind": "sort_qc_compile",
        "step": 14,
        "input_path": str(input_path.resolve()),
        "workspace_root": discovered["workspace_root"],
        "step9_dir": discovered["step9_dir"],
        "qc_dir": str(qc_dir),
        "source_dir": str(paths["source_dir"]),
        "compiled_dir": str(paths["compiled_dir"]),
        "addon_dir": str(qc_dir / default_model),
        "author": safe_author,
        "character_category": safe_category,
        "model_name": default_model,
        "gender": normalize_gender(gender),
        "game": game,
        "survivor": normalize_survivor(survivor),
        "display_name": display_from_identifier(strip_model_hash_suffix(default_model), "Mmd Model"),
        "category_readable": category_display_from_identifier(safe_category, "Sheepy Lord"),
        "gmod": gmod,
        "inputs": discovered,
        "rows": jiggle_rows,
        "bodygroups": default_bodygroup_rows(step9_dir) if step9_dir.exists() else [],
        "physics_globals": physics_globals,
        "physics_rows": physics_rows,
        "physics_collision_text_lines": physics_collision_text_lines,
        "invert_jiggle_direction": False,
        "include_mci_metadata_json": True,
        "nodecal": normalize_game(game) == "l4d2",
        "material_rows": material_rows,
        "texturegroups": texturegroups,
        "warnings": warnings,
        "validation_errors": errors,
    }
    warnings.extend(validate_texturegroups(plan))
    analysis = {
        "version": 1,
        "kind": "sort_qc_compile_analysis",
        "input_path": str(input_path.resolve()),
        "workspace_root": discovered["workspace_root"],
        "step9_dir": discovered["step9_dir"],
        "smd_count": len(smd_files),
        "vta_count": len(discovered["vta_files"]),
        "bone_count": len(nodes),
        "jiggle_count": sum(1 for row in jiggle_rows if row.get("jiggle_type") != "Not Jiggle"),
        "omni_jiggle_count": sum(1 for row in jiggle_rows if row.get("jiggle_type") == "Omni Jiggle"),
        "spring_jiggle_count": sum(1 for row in jiggle_rows if row.get("jiggle_type") == "Spring Jiggle"),
        "materials": materials,
        "material_count": len(materials),
        "model_preview": model_preview,
        "bone_preview": bones_preview,
        "gmod": gmod,
        "warnings": warnings,
        "validation_errors": errors,
    }
    write_json(paths["analysis"], analysis, compact=True)
    write_json(paths["plan"], plan)
    emit(f"Wrote QC analysis: {paths['analysis']}")
    emit(f"Wrote QC plan: {paths['plan']}")
    return {"analysis": analysis, "plan": plan, "paths": {key: str(value) for key, value in paths.items()}}


def material_plan_rows(manifest_path: Path) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(manifest.get("textures", []), start=1):
        if not isinstance(row, dict):
            continue
        output_name = safe_name(str(row.get("output_name") or row.get("material_name") or f"mat_{index:03d}"), f"mat_{index:03d}")
        rows.append(
            {
                "uid": f"mat_{index:03d}",
                "material_name": str(row.get("material_name") or output_name),
                "output_name": output_name,
                "base_png": str(row.get("base_output_path") or ""),
                "normal_png": str(row.get("normal_output_path") or ""),
                "normal_status": str(row.get("normal_status") or row.get("normal_action") or ""),
                # Optional Step 12 PBR outputs (manual scheme only; empty for
                # the legacy/auto-port path, which keeps the VMT unchanged).
                "phongexp_png": str(row.get("phongexp_output_path") or ""),
                "selfillum_png": str(row.get("selfillum_output_path") or ""),
                "pbr_enabled": row.get("pbr_enabled", []) if isinstance(row.get("pbr_enabled"), list) else [],
                "warnings": row.get("warnings", []) if isinstance(row.get("warnings"), list) else [],
            }
        )
    return rows


def copytree_clean(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def workspace_compile_source_copy_dir(qc_dir: Path) -> Path:
    return qc_dir / "0_qc_source"


# Only these QC files are kept in the distributed qc_compile_source folder; the
# others (compile_initial.qc / compile_hbox_probe.qc) are intermediate passes
# used only during Step 14's multi-stage compile and aren't needed to re-compile
# the shipped model or player model.
COMPILE_SOURCE_KEEP_QC = ("compile.qc", "compile_pm.qc")


def prune_extra_compile_source_qc(folder: Path) -> None:
    """Drop every top-level ``*.qc`` in ``folder`` except compile.qc / compile_pm.qc."""
    keep = {name.casefold() for name in COMPILE_SOURCE_KEEP_QC}
    try:
        qc_files = list(folder.glob("*.qc"))
    except OSError:
        return
    for qc in qc_files:
        if qc.name.casefold() not in keep:
            try:
                qc.unlink()
            except OSError:
                pass


def distribution_compile_source_copy_dir(distribution_output_dir: Path, model: str, author: str) -> Path:
    return distribution_output_dir / f"{model}_{author}_qc_compile_source"


# Issue #139: L4D2 ported survivors' arms drift left / cross in game. Step 9's proportion
# trick fits the survivor skeleton onto the MMD model, which arrives posed in the GMOD
# ValveBiped reference convention (arms ~40 deg below horizontal), while the survivor bind
# pose holds the arms ~61 deg down. The a_proportions delta (proportions - reference)
# therefore bakes a ~20 deg rotation into every arm-chain bone, and the autoplay predelta
# applies that rotation on top of every included survivor animation (worst under the
# weapon-hold/IK poses, where the hand IK chases the displaced weapon_bone and crosses the
# arms). The WORKING GMod pipeline's delta is measurably position-only -- the fitted
# skeleton and the GMod reference carry identical bone rotations -- so L4D2 is fixed by
# giving its delta the same shape: every bone shared with the subtract reference keeps its
# fitted POSITION (bone lengths / proportions) but takes the reference's ROTATION verbatim,
# leaving a rotation-free delta. Bones only present in the fitted skeleton (MMD hair /
# skirt / sleeve helpers) are untouched: stock animations never drive them, so they follow
# their parent bone at the exported local offset either way.
def neutralize_proportion_rotation_delta(anims_dir: Path, reference_name: str, label: str = "survivor model") -> dict[str, Any]:
    report: dict[str, Any] = {"applied": False, "label": label, "reference": reference_name}
    proportions_path = anims_dir / "proportions.smd"
    reference_path = anims_dir / f"{reference_name}.smd"
    if not proportions_path.exists() or not reference_path.exists():
        report["reason"] = "proportions.smd or reference SMD missing"
        emit(f"L4D2 proportion delta ({label}): anims/proportions.smd or anims/{reference_name}.smd is missing; rotation neutralization skipped.")
        return report
    try:
        reference_poses = _read_smd_frame0_poses(reference_path)
    except Exception as exc:
        report["reason"] = f"unreadable reference SMD: {exc}"
        emit(f"L4D2 proportion delta ({label}): could not read anims/{reference_name}.smd ({exc}); rotation neutralization skipped.")
        return report
    reference_rot_tokens = {
        name: pose.split()[3:6] for name, pose in reference_poses.items() if len(pose.split()) >= 6
    }
    text = proportions_path.read_text(encoding="utf-8", errors="replace")
    had_trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    id_to_name: dict[int, str] = {}
    state = ""
    rewritten = 0
    untouched_extra_bones: set[str] = set()
    max_delta_deg = 0.0
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line in {"nodes", "skeleton", "triangles"}:
            state = line
            continue
        if line == "end":
            state = ""
            continue
        if state == "nodes":
            match = re.match(r'(-?\d+)\s+"(.*)"\s+(-?\d+)', line)
            if match:
                id_to_name[int(match.group(1))] = match.group(2)
            continue
        if state != "skeleton" or not line or line.startswith("time"):
            continue
        parts = line.split()
        if len(parts) < 7 or not parts[0].lstrip("-").isdigit():
            continue
        name = id_to_name.get(int(parts[0]))
        if name is None:
            continue
        rot_tokens = reference_rot_tokens.get(name)
        if rot_tokens is None:
            untouched_extra_bones.add(name)
            continue
        try:
            old_matrix = rotation_matrix_xyz(*(float(value) for value in parts[4:7]))
            new_matrix = rotation_matrix_xyz(*(float(value) for value in rot_tokens))
            relative = matmul(old_matrix, [[new_matrix[j][i] for j in range(3)] for i in range(3)])
            trace = max(-1.0, min(1.0, (relative[0][0] + relative[1][1] + relative[2][2] - 1.0) / 2.0))
            max_delta_deg = max(max_delta_deg, math.degrees(math.acos(trace)))
        except ValueError:
            pass
        prefix = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        lines[index] = prefix + " ".join(parts[:4] + list(rot_tokens) + parts[7:])
        rewritten += 1
    if not rewritten:
        report["reason"] = "no shared bones found between proportions.smd and the reference SMD"
        emit(f"L4D2 proportion delta ({label}): no shared bones between anims/proportions.smd and anims/{reference_name}.smd; rotation neutralization skipped.")
        return report
    proportions_path.write_text("\n".join(lines) + ("\n" if had_trailing_newline else ""), encoding="utf-8")
    report.update(
        {
            "applied": True,
            "rewritten_pose_lines": rewritten,
            "untouched_extra_bones": len(untouched_extra_bones),
            "max_rotation_delta_deg": round(max_delta_deg, 2),
        }
    )
    emit(
        f"L4D2 proportion delta ({label}): made position-only against anims/{reference_name}.smd "
        f"({rewritten} pose line(s) rewritten; removed up to {max_delta_deg:.1f} deg of baked rotation; "
        f"{len(untouched_extra_bones)} custom bone(s) untouched)."
    )
    return report


def prepare_qc_source(plan: dict[str, Any]) -> Path:
    raw_source_dir = str(plan.get("source_dir") or "").strip()
    if not raw_source_dir:
        raise RuntimeError("QC plan source_dir is empty; refusing to prepare the QC source folder.")
    source_dir = Path(raw_source_dir)
    resolved_source_dir = source_dir.resolve()
    if resolved_source_dir == Path.cwd().resolve() or resolved_source_dir.parent == resolved_source_dir:
        raise RuntimeError(
            f"QC plan source_dir resolves to an unsafe location ({resolved_source_dir}); "
            "refusing to delete and recreate it."
        )
    if source_dir.exists():
        shutil.rmtree(source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)
    step9_dir = Path(str(plan["step9_dir"]))
    for path in sorted(step9_dir.glob("*.smd"), key=lambda item: natural_key(item.name)):
        shutil.copyfile(path, source_dir / path.name)
    for path in sorted(step9_dir.glob("*.vta"), key=lambda item: natural_key(item.name)):
        shutil.copyfile(path, source_dir / path.name)
    copytree_clean(step9_dir / "anims", source_dir / "anims")
    vrd_path = Path(str(plan.get("inputs", {}).get("step11_vrd") or ""))
    if vrd_path.exists():
        shutil.copyfile(vrd_path, source_dir / "vrd.vrd")
    return source_dir


def qc_model_header(plan: dict[str, Any], pm: bool = False, arms: bool = False) -> list[str]:
    # L4D2 requires exact, non-custom model paths: the character is survivors/survivor_<slot>.mdl
    # and the arms come from l4d2_arms_modelname (v_arms_<slot>_new for L4D2 survivors,
    # v_arms_bill/zoey/francis/louis for the L4D1 survivors).
    if normalize_game(plan.get("game")) == "l4d2":
        slot = normalize_survivor(plan.get("survivor"))
        name = l4d2_arms_modelname(slot) if arms else l4d2_character_modelname(slot)
        return [f'$modelname "{name}" \n\n']
    author = str(plan["author"])
    category = str(plan["character_category"])
    model = str(plan["model_name"])
    stem = f"{model}_arms" if arms else f"{model}_pm" if pm else model
    return [f'$modelname "{author}/{category}/{stem}.mdl" \n\n']


def flex_model_block(name: str, smd: str, vta: Path) -> list[str]:
    flexes = parse_vta_flexes(vta)
    lines = [f'$model "{name}" "{smd}" {{\n\n']
    if flexes:
        lines.append(f'\tflexfile "{vta.name}"\n\t{{\n')
        lines.append('\t\tdefaultflex frame 0\n')
        for index, flex in enumerate(flexes, start=1):
            lines.append(f'\t\tflex "{flex}" frame {index}\n')
        lines.append('\t}\n\n')
        for flex in flexes:
            lines.append(f'\tflexcontroller phoneme range 0 1 "{flex}"\n')
        lines.append('\n')
        for flex in flexes:
            lines.append(f'\t%{flex}  = {flex}\n')
    lines.append('}\n\n')
    return lines


def bodygroup_group_block(name: str, studios: list[str], can_hide: bool = True) -> list[str]:
    """A `$bodygroup` with one or more switchable `studio` options and an optional
    hide ("blank") option. Grouping multiple SMDs lets the user switch parts
    in-game; omitting `blank` makes the bodygroup non-hideable."""
    lines = [f'$bodygroup "{name}"\n', "{\n"]
    for smd in studios:
        lines.append(f'\tstudio "{smd}"\n')
    if can_hide:
        lines.append("\tblank\n")
    lines.append("}\n\n")
    return lines


def bodygroup_block(name: str, smd: str, optional_blank: bool = True) -> list[str]:
    return bodygroup_group_block(name, [smd], optional_blank)


# Core body parts that are never hideable by default: removing them in game
# leaves the character visibly broken (a floating head, a bald scalp, missing
# limbs) rather than showing an intentional outfit variation. Their $bodygroup
# omits `blank`, so the part cannot be switched off. Matched case-insensitively
# on the exact bodygroup name; the user can still tick "can hide" per part in
# the Step-14 table to override this.
ESSENTIAL_BODYGROUP_NAMES = frozenset({"hair", "body", "face", "legs", "arms"})


def is_essential_bodygroup(name: str) -> bool:
    return str(name or "").strip().casefold() in ESSENTIAL_BODYGROUP_NAMES


def default_bodygroup_rows(step9_dir: Path) -> list[dict[str, Any]]:
    """Default bodygroup control rows: one per body-part SMD (Physics excluded),
    Face/Body first then the rest alphabetical. Each part is its own group and
    hideable, except flex parts (a sibling .vta) which are locked to their own
    group and non-hideable (they must stay $model blocks for the flexes), and
    the core body parts in ESSENTIAL_BODYGROUP_NAMES."""
    rows: list[dict[str, Any]] = []
    ordered: list[Path] = []
    for special in ("Face.smd", "Body.smd"):
        if (step9_dir / special).exists():
            ordered.append(step9_dir / special)
    for smd in sorted(step9_dir.glob("*.smd"), key=lambda item: natural_key(item.name)):
        if smd.name in {"Physics.smd", "Face.smd", "Body.smd"}:
            continue
        ordered.append(smd)
    for index, smd in enumerate(ordered, start=1):
        name = smd.stem
        has_flex = (step9_dir / f"{name}.vta").exists()
        rows.append(
            {
                "uid": f"bg_{index:03d}",
                "smd": smd.name,
                "name": name,
                "has_flex": has_flex,
                "group": name,
                "can_hide": not has_flex and not is_essential_bodygroup(name),
            }
        )
    return rows


def _bodygroup_qc_blocks_scan(source_dir: Path, include_flexes: bool) -> list[str]:
    """Back-compat default: every SMD is its own bodygroup, hideable except the
    core body parts (ESSENTIAL_BODYGROUP_NAMES); flex SMDs (with a .vta) become
    $model blocks. Used when the plan has no bodygroup config (legacy plans)."""
    lines: list[str] = []
    consumed = {"Physics.smd"}
    if (source_dir / "Face.smd").exists():
        consumed.add("Face.smd")
        face_vta = source_dir / "Face.vta"
        lines.extend(
            flex_model_block("Face", "Face.smd", face_vta)
            if include_flexes and face_vta.exists()
            else bodygroup_block("Face", "Face.smd", not is_essential_bodygroup("Face"))
        )
    if (source_dir / "Body.smd").exists():
        consumed.add("Body.smd")
        body_vta = source_dir / "Body.vta"
        lines.extend(
            flex_model_block("Body", "Body.smd", body_vta)
            if include_flexes and body_vta.exists()
            else bodygroup_block("Body", "Body.smd", not is_essential_bodygroup("Body"))
        )
    for smd in sorted(source_dir.glob("*.smd"), key=lambda item: natural_key(item.name)):
        if smd.name in consumed:
            continue
        name = smd.stem
        vta = source_dir / f"{name}.vta"
        if include_flexes and vta.exists():
            lines.extend(flex_model_block(name, smd.name, vta))
            continue
        lines.extend(bodygroup_block(name, smd.name, not is_essential_bodygroup(name)))
    return lines


def _bodygroup_qc_blocks_from_config(source_dir: Path, include_flexes: bool, bodygroups: list[dict[str, Any]]) -> list[str]:
    """Emit bodygroups from the step-14 control plan: SMDs sharing a group become
    one switchable $bodygroup; non-hideable groups omit `blank`. Flex SMDs are
    always their own $model (never grouped or hidden)."""
    lines: list[str] = []
    valid = [row for row in bodygroups if isinstance(row, dict) and str(row.get("smd") or "")]
    flex_names = {
        str(row.get("name") or Path(str(row.get("smd"))).stem)
        for row in valid
        if bool(row.get("has_flex")) and (source_dir / f"{Path(str(row.get('smd'))).stem}.vta").exists()
    }
    order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in valid:
        smd = str(row.get("smd"))
        if not (source_dir / smd).exists():
            continue
        name = str(row.get("name") or Path(smd).stem)
        has_flex = bool(row.get("has_flex")) and (source_dir / f"{Path(smd).stem}.vta").exists()
        if has_flex:
            group = name  # flex parts can never be grouped
        else:
            group = str(row.get("group") or name).strip() or name
            if group in flex_names:  # never merge a part into a flex part's group
                group = name
        member = {"smd": smd, "name": name, "has_flex": has_flex, "can_hide": bool(row.get("can_hide", True))}
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append(member)
    covered = {member["smd"] for members in groups.values() for member in members}
    for group in order:
        members = groups[group]
        for member in (m for m in members if m["has_flex"]):
            stem = Path(member["smd"]).stem
            vta = source_dir / f"{stem}.vta"
            if include_flexes and vta.exists():
                lines.extend(flex_model_block(stem, member["smd"], vta))
            else:
                lines.extend(bodygroup_group_block(member["name"], [member["smd"]], False))
        nonflex = [m for m in members if not m["has_flex"]]
        if nonflex:
            # Show the part whose name matches the group first (the default option),
            # then the rest in their listed order.
            anchor = [m for m in nonflex if m["name"] == group]
            rest = [m for m in nonflex if m["name"] != group]
            studios = [m["smd"] for m in anchor + rest]
            can_hide = all(bool(m["can_hide"]) for m in nonflex)
            lines.extend(bodygroup_group_block(group, studios, can_hide))
    # Any SMD present on disk but absent from the config (e.g. re-exported with a
    # new part) falls back to its own bodygroup so nothing is dropped, hideable
    # unless it is a core body part.
    for smd in sorted(source_dir.glob("*.smd"), key=lambda item: natural_key(item.name)):
        if smd.name in covered or smd.name == "Physics.smd":
            continue
        stem = smd.stem
        vta = source_dir / f"{stem}.vta"
        if include_flexes and vta.exists():
            lines.extend(flex_model_block(stem, smd.name, vta))
        else:
            lines.extend(bodygroup_block(stem, smd.name, not is_essential_bodygroup(stem)))
    return lines


def bodygroup_qc_blocks(source_dir: Path, include_flexes: bool = True, bodygroups: list[dict[str, Any]] | None = None) -> list[str]:
    if bodygroups:
        return _bodygroup_qc_blocks_from_config(source_dir, include_flexes, bodygroups)
    return _bodygroup_qc_blocks_scan(source_dir, include_flexes)


def texture_groups_config_path(workspace_root: Path) -> Path:
    """Cross-step sidecar that holds the Step-5 `$texturegroup` (skin) config.
    Step 5 writes it, Step 12 reads it to inject imported skin textures, and
    Step 14 reads it to emit the `$texturegroup skinfamilies` QC block."""
    return workspace_root / "texture_groups" / "texture_groups.json"


def load_texture_groups_config(workspace_root: Path | None) -> dict[str, Any]:
    if workspace_root is None:
        return {}
    path = texture_groups_config_path(Path(workspace_root))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _texturegroup_skin_rows(config: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    """Return (slots, rows) where `slots` are the default (skin-0) material names
    and `rows` is the full skin table including the default row, each row padded to
    the slot count (an empty cell inherits the default slot for that column)."""
    if not isinstance(config, dict):
        return [], []
    slots = [str(s).strip() for s in config.get("slots", []) if str(s).strip()]
    skins_raw = config.get("skins", [])
    if not slots or not isinstance(skins_raw, list):
        return slots, []
    rows: list[list[str]] = [list(slots)]
    for skin in skins_raw:
        if isinstance(skin, dict):
            cells = skin.get("cells", [])
        elif isinstance(skin, list):
            cells = skin
        else:
            continue
        cells = [str(c).strip() for c in cells] if isinstance(cells, list) else []
        normalized = [(cells[i] if i < len(cells) and cells[i] else slots[i]) for i in range(len(slots))]
        rows.append(normalized)
    # Only meaningful with at least one alternate skin beyond the default.
    return slots, (rows if len(rows) >= 2 else [])


def _texturegroup_name_to_vmt(plan: dict[str, Any]) -> dict[str, str]:
    """Map every material display/output name to its final VMT base name so the
    skin table references match the compiled .vmt files exactly."""
    mapping: dict[str, str] = {}
    for row in plan.get("material_rows", []):
        if not isinstance(row, dict):
            continue
        vmt = safe_name(str(row.get("output_name") or row.get("material_name") or ""), "")
        if not vmt:
            continue
        for key in (row.get("material_name"), row.get("output_name"), vmt):
            key_str = str(key or "").strip()
            if key_str:
                mapping.setdefault(key_str, vmt)
    return mapping


def texturegroup_qc_lines(plan: dict[str, Any]) -> list[str]:
    slots, rows = _texturegroup_skin_rows(plan.get("texturegroups") or {})
    if not slots or not rows:
        return []
    mapping = _texturegroup_name_to_vmt(plan)

    def resolve(name: str) -> str:
        name = str(name).strip()
        return mapping.get(name) or safe_name(name, name)

    lines = ["$texturegroup skinfamilies\n", "{\n"]
    for row in rows:
        cells = " ".join(f'"{resolve(cell)}"' for cell in row)
        lines.append(f"\t{{ {cells} }}\n")
    lines.append("}\n\n")
    return lines


def validate_texturegroups(plan: dict[str, Any]) -> list[str]:
    """Warn (do not fail) when a skin references a material with no matching .vmt,
    so the user learns the skin will not work in-game."""
    slots, rows = _texturegroup_skin_rows(plan.get("texturegroups") or {})
    if not slots or not rows:
        return []
    mapping = _texturegroup_name_to_vmt(plan)
    known = set(mapping)
    missing: list[str] = []
    for row in rows:
        for cell in row:
            cell = str(cell).strip()
            if cell and cell not in known and cell not in missing:
                missing.append(cell)
    warnings: list[str] = []
    if missing:
        warnings.append(
            "$texturegroup references material(s) with no matching .vmt: "
            + ", ".join(missing)
            + ". The skin will fall back to a missing texture in-game unless the name matches a model material or an imported texture."
        )
    return warnings


def sfm_idle_sequence(source_dir: Path) -> tuple[str, bool]:
    """Pick the SFM static-idle source; returns (path_without_or_with_name, is_anim_smd).

    SFM uses a single static idle (no proportion subtract/autoplay). The model's
    bind pose (captured `$definebone`) is the MMD-proportioned pose, so the idle
    just needs to hold that pose. Prefer the skeleton-only proportioned animation
    `anims/proportions.smd`: it carries the proportioned pose (idempotent with the
    bind) AND studiomdl reliably loads it as a 1-frame idle even alongside
    `$definebone`. A mesh bodygroup SMD (which has `triangles`) can be read as
    geometry under `$definebone`, making studiomdl report "no animations found",
    so it is only a last-resort fallback.
    """
    if (source_dir / "anims" / "proportions.smd").exists():
        return ("anims/proportions", True)
    if (source_dir / "Body.smd").exists():
        return ("Body.smd", False)
    for smd in sorted(source_dir.glob("*.smd"), key=lambda p: natural_key(p.name)):
        if smd.name.lower() != "physics.smd":
            return (smd.name, False)
    return ("anims/proportions", True)


def base_qc_lines(
    plan: dict[str, Any],
    source_dir: Path,
    pm: bool = False,
    include_definebones: list[str] | None = None,
    include_jiggles: list[str] | None = None,
    include_hboxes: list[str] | None = None,
    include_collision: bool = False,
    include_flexes: bool = True,
    for_definebone_capture: bool = False,
) -> list[str]:
    target = normalize_game(plan.get("game"))
    l4d2 = target == "l4d2"
    sfm = target == "sfm"
    lines: list[str] = []
    lines.append('// Created by MMD Character Importer Step 14\n\n')
    lines.extend(qc_model_header(plan, pm=pm))
    if sfm:
        # SFM's older studiomdl needs $maxverts raised to accept the new model
        # format (the "single number" the SFM expert described).
        lines.append("$maxverts 65530 \n\n")
    bodygroups = plan.get("bodygroups") if isinstance(plan.get("bodygroups"), list) else None
    lines.extend(bodygroup_qc_blocks(source_dir, include_flexes=include_flexes, bodygroups=bodygroups))
    lines.append('$surfaceprop "flesh" \n\n')
    lines.append('$contents "solid" \n\n')
    if l4d2:
        # L4D2 survivor head/eye conventions.
        lines.append("$illumposition 0 -0.861 33.401 \n\n")
        lines.append("$eyeposition 0 0 60 \n")
        lines.append("$maxeyedeflection 30 \n")
        lines.append("$forcephonemecrossfade \n\n")
    else:
        lines.append("$illumposition -0.637 0 35.954 \n\n")
    lines.append("$ambientboost \n\n")
    lines.append("$mostlyopaque \n\n")
    lines.append(f'$cdmaterials "models/{plan["author"]}/{plan["model_name"]}/" \n\n')
    lines.extend(texturegroup_qc_lines(plan))
    lines.append('$cbox 0 0 0 0 0 0 \n\n')
    lines.append('$bbox -13 -13 0 13 13 72 \n\n')
    if include_definebones:
        lines.extend(include_definebones)
        if lines and not lines[-1].endswith("\n\n"):
            lines.append("\n")
    if include_hboxes:
        if l4d2:
            # L4D2 survivors use a named "L4D" hitbox set.
            lines.append('$hboxset "L4D" \n')
        lines.extend(include_hboxes)
        if lines and not lines[-1].endswith("\n\n"):
            lines.append("\n")
    if include_jiggles:
        lines.extend(include_jiggles)
        lines.append("\n")
    if l4d2:
        lines.append('$poseparameter "body_pitch" -90 90 loop 360 \n')
        lines.append('$poseparameter "body_yaw" -90 90 loop 360 \n')
        lines.append('$poseparameter "head_pitch" -90 90 loop 360 \n')
        lines.append('$poseparameter "head_yaw" -90 90 loop 360 \n\n')
    if (source_dir / "vrd.vrd").exists():
        lines.append('$proceduralbones "vrd.vrd"\n\n')
    if l4d2:
        # L4D2 survivor weapon/item placement + IK. Without these $attachments the game has
        # nowhere to place the held weapon and drops it at the model origin; the survivor IK set
        # (incl. the "ikclip" weapon chain and ikautoplaylock 1) matches the base-game survivor.
        survivor_bones = l4d2_survivor_skeleton_bones(plan.get("survivor"))
        if include_definebones:
            # Issue #168: the compile's real bone set is the $definebone list, which can carry
            # bones the bundled skeleton lacks (the injected TLS weapon bones for the L4D1
            # slots). Emit attachments for those too. The -definebones capture pass passes no
            # definebones and keeps the original bundle-only gate.
            survivor_bones = survivor_bones | {bone.name for bone in parse_definebone_lines(include_definebones)}
        attachment_lines = l4d2_attachment_lines(survivor_bones)
        if attachment_lines:
            lines.extend(attachment_lines)
            lines.append("\n")
        # IMPORTANT: omit $ikchain/$ikautoplaylock from the `-definebones` capture pass for L4D2.
        # Our L4D2 base armature is extracted from the base-game survivor's *compiled* .mdl, whose
        # bind pose is already post-IK-realignment. Declaring $ikchain here makes studiomdl realign
        # the left leg / hands a SECOND time, snapping the left calf into a 90deg quadrant and
        # garbling the left foot + right hand. We capture the definebones WITHOUT ik (clean bind),
        # then re-add the ik chains in the real compile (definebones lock the bind, so the real
        # compile stays clean). The base-game/gold ports avoid this by using a clean canonical
        # reference where the realignment is idempotent.
        if not for_definebone_capture:
            lines.extend(l4d2_ik_lines(survivor_bones))
    elif sfm:
        # SFM models are posed in the SFM timeline; no IK chains/locks.
        pass
    else:
        lines.append('$ikchain "rhand" "ValveBiped.Bip01_R_Hand" knee 0.707 0.707 0 \n')
        lines.append('$ikchain "lhand" "ValveBiped.Bip01_L_Hand" knee 0.707 0.707 0 \n')
        lines.append('$ikchain "rfoot" "ValveBiped.Bip01_R_Foot" knee 0.707 -0.707 0 \n')
        lines.append('$ikchain "lfoot" "ValveBiped.Bip01_L_Foot" knee 0.707 -0.707 0 \n\n')
        lines.append('$ikautoplaylock "rfoot" 0.7 0.1 \n')
        lines.append('$ikautoplaylock "lfoot" 0.7 0.1 \n\n')
    if sfm:
        # SFM: a single static idle holding the proportioned pose. No proportion
        # subtract/autoplay, no ragdoll, no $includemodel, no collision -- per the
        # expert's example QC. The idle source is the skeleton-only proportioned
        # animation (studiomdl-safe under $definebone); see sfm_idle_sequence().
        idle_src, is_anim = sfm_idle_sequence(source_dir)
        if is_anim:
            lines.append(f'$sequence "idle" "{idle_src}" fps 1 \n\n')
        else:
            lines.append('$sequence "idle" \n{\n')
            lines.append(f'\t"{idle_src}"\n')
            lines.append('}\n\n')
        return lines
    reference_name = gendered_reference_name(plan, source_dir / "anims")
    lines.append(f'$sequence reference "anims/{reference_name}" fps 1 \n')
    if l4d2:
        # Issue #168 (legs bent/twisted): NO root drop for L4D2. The -2.50 below is the GMod
        # player-model convention; it lands in the proportion delta (anims/proportions loads
        # after $origin, the reference sequence loads before it), sinking every animation pose
        # 2.5 units. Measured on a real export that put the toes ~1.2 units UNDER the floor,
        # and the survivor foot IK (ground rules + $ikautoplaylock, active every frame) then
        # bends the knees ~4-6 units off-line to fish the feet back up. The stock survivor
        # plays its animations with no shift at all, so the delta must reproduce the Step 9
        # stance exactly.
        lines.append('\n')
    else:
        lines.append('$origin 0 0 -2.50 \n\n')
    lines.append('$animation a_proportions "anims/proportions" subtract reference 0 \n\n')
    if l4d2:
        # L4D2 survivor MULTIPLAYER sequence-index alignment (issue #105). A survivor REPLACEMENT must
        # reproduce the base-game sequence INDEX layout: the local idle ('reference', index 0) and
        # 'ragdoll' (1), then EVERY included anim/gesture sequence forward-declared in canonical order
        # ($declaresequence, indices 2..N) so $includemodel fills exactly the indices the MP server
        # addresses by number, and finally the proportions autoplay delta tacked on at the END (hidden)
        # where it can't shift the included indices. Without this the server plays the wrong index ->
        # remote clients see the bind/T-pose (single-player resolves by name, so it looks fine there).
        # SCMI previously emitted 'proportions' at index 1, shifting every included sequence. The
        # $declaresequence list + the per-slot $includemodel set are the base-game survivor's, bundled
        # verbatim from TwentyCat's l4d2_survivors_converter (the proven fix for #105).
        lines.append('$Sequence "ragdoll" {\n')
        lines.append('\t"anims/proportions"\n')
        lines.append('\tactivity "ACT_DIERAGDOLL" 1\n')
        lines.append('\tfadein 0.2\n')
        lines.append('\tfadeout 0.2\n')
        lines.append('\tfps 60\n')
        lines.append('}\n\n')
        declare_lines = l4d2_declaresequence_lines(plan.get("survivor"))
        if declare_lines:
            lines.extend(declare_lines)
            lines.append("\n")
        lines.append('$sequence proportions a_proportions predelta autoplay hidden \n\n')
        lines.extend(l4d2_includemodel_lines(plan.get("survivor")))
    else:
        lines.append('$sequence proportions a_proportions predelta autoplay \n\n')
        lines.append('$Sequence "ragdoll" {\n')
        lines.append('\t"anims/proportions"\n')
        lines.append('\tactivity "ACT_DIERAGDOLL" 1\n')
        lines.append('\tfadein 0.2\n')
        lines.append('\tfadeout 0.2\n')
        lines.append('\tfps 60\n')
        lines.append('}\n\n')
        gender_includes = GENDER_ANIMATION_INCLUDES[normalize_gender(plan.get("gender"))]
        for include in (gender_includes["player"] if pm else gender_includes["npc"]):
            lines.append(f'$includemodel "{include}" \n')
    lines.append("\n")
    if include_collision and (source_dir / "Physics.smd").exists():
        lines.extend(collision_qc_block(plan))
    return lines


def jiggle_params_for_row(row: dict[str, Any], jiggle_type: str) -> dict[str, float]:
    params = default_jiggle_params(jiggle_type)
    raw_params = row.get("jiggle_params")
    if isinstance(raw_params, dict):
        for key, value in raw_params.items():
            if key in params:
                params[key] = qc_float(value, params[key])
    return params


def spring_jiggle_block(row: dict[str, Any]) -> list[str]:
    name = str(row.get("bone") or "")
    params = jiggle_params_for_row(row, "Spring Jiggle")
    return [
        f'$jigglebone "{name}"\n',
        "{\n",
        " is_flexible\n",
        " {\n",
        f"     length {fmt_qc_float(params['length'], 15.0)}\n",
        f"     tip_mass {fmt_qc_float(params['tip_mass'], 30.0)}\n",
        f"     pitch_stiffness {fmt_qc_float(params['pitch_stiffness'], 40.0)}\n",
        f"     pitch_damping {fmt_qc_float(params['pitch_damping'], 5.0)}\n",
        f"     yaw_stiffness {fmt_qc_float(params['yaw_stiffness'], 40.0)}\n",
        f"     yaw_damping {fmt_qc_float(params['yaw_damping'], 3.0)}\n",
        f"     along_stiffness {fmt_qc_float(params['along_stiffness'], 100.0)}\n",
        f"     along_damping {fmt_qc_float(params['along_damping'], 0.0)}\n",
        f"     angle_constraint {fmt_qc_float(params['angle_constraint'], 14.0)}\n",
        " }\n",
        " has_base_spring\n",
        " {\n",
        f"     base_mass {fmt_qc_float(params['base_mass'], 15.0)}\n",
        f"     stiffness {fmt_qc_float(params['base_stiffness'], 50.0)}\n",
        f"     damping {fmt_qc_float(params['base_damping'], 6.0)}\n",
        f"     left_constraint {fmt_qc_float(params['left_min'], -0.15)} {fmt_qc_float(params['left_max'], 0.15)}\n",
        f"     left_friction {fmt_qc_float(params['left_friction'], 0.01)}\n",
        f"     up_constraint {fmt_qc_float(params['up_min'], -0.15)} {fmt_qc_float(params['up_max'], 0.15)}\n",
        f"     up_friction {fmt_qc_float(params['up_friction'], 0.01)}\n",
        f"     forward_constraint {fmt_qc_float(params['forward_min'], -0.01)} {fmt_qc_float(params['forward_max'], 0.01)}\n",
        f"     forward_friction {fmt_qc_float(params['forward_friction'], 0.05)}\n",
        " }\n",
        "}\n",
    ]


def normalize_default_jiggle_angle(value: float) -> float:
    if math.isclose(abs(value), LEGACY_DIRECTIONAL_JIGGLE_ANGLE, abs_tol=1e-6):
        return math.copysign(DEFAULT_DIRECTIONAL_JIGGLE_ANGLE, value)
    return value


def normalized_constraint_pair(values: tuple[float, float]) -> tuple[float, float]:
    return normalize_default_jiggle_angle(float(values[0])), normalize_default_jiggle_angle(float(values[1]))


def inverted_constraint(values: object, fallback: tuple[float, float]) -> tuple[float, float]:
    if isinstance(values, list) and len(values) >= 2:
        try:
            low = normalize_default_jiggle_angle(float(values[0]))
            high = normalize_default_jiggle_angle(float(values[1]))
            return -high, -low
        except Exception:
            pass
    fallback_low, fallback_high = normalized_constraint_pair(fallback)
    return -fallback_high, -fallback_low


def normalized_constraint(values: object, fallback: tuple[float, float], invert: bool = False) -> tuple[float, float]:
    if invert:
        return inverted_constraint(values, fallback)
    if isinstance(values, list) and len(values) >= 2:
        try:
            return normalize_default_jiggle_angle(float(values[0])), normalize_default_jiggle_angle(float(values[1]))
        except Exception:
            pass
    return normalized_constraint_pair(fallback)


def omni_jiggle_block(row: dict[str, Any], invert_direction: bool = False) -> list[str]:
    name = str(row.get("bone") or "")
    params = jiggle_params_for_row(row, "Omni Jiggle")
    if bool(row.get("jiggle_constraint_overrides")):
        pitch = normalized_constraint(row.get("pitch_constraint"), (-25.0, 25.0), invert_direction)
        yaw = normalized_constraint(row.get("yaw_constraint"), (-10.0, 10.0), invert_direction)
    else:
        pitch = normalized_constraint([-25, 25], (-25.0, 25.0), invert_direction)
        yaw = normalized_constraint([-10, 10], (-10.0, 10.0), invert_direction)
    return [
        f'$jigglebone "{name}"\n',
        "{\n",
        " is_flexible\n",
        " {\n",
        f"     length {fmt_qc_float(params['length'], 15.0)}\n",
        f"     tip_mass {fmt_qc_float(params['tip_mass'], 0.0)}\n",
        f"     pitch_constraint {pitch[0]:g} {pitch[1]:g}\n",
        f"     pitch_stiffness {fmt_qc_float(params['pitch_stiffness'], 10.0)}\n",
        f"     pitch_damping {fmt_qc_float(params['pitch_damping'], 2.0)}\n",
        f"     yaw_constraint {yaw[0]:g} {yaw[1]:g}\n",
        f"     yaw_stiffness {fmt_qc_float(params['yaw_stiffness'], 10.0)}\n",
        f"     yaw_damping {fmt_qc_float(params['yaw_damping'], 2.0)}\n",
        f"     along_stiffness {fmt_qc_float(params['along_stiffness'], 100.0)}\n",
        f"     along_damping {fmt_qc_float(params['along_damping'], 0.0)}\n",
        f"     angle_constraint {fmt_qc_float(params['angle_constraint'], 30.0)}\n",
        " }\n",
        "}\n",
    ]


def directional_jiggle_block(row: dict[str, Any], invert_direction: bool = False) -> list[str]:
    name = str(row.get("bone") or "")
    params = jiggle_params_for_row(row, "Directional Jiggle")
    pitch = normalized_constraint(row.get("pitch_constraint"), (-10.0, 10.0), invert_direction)
    yaw = normalized_constraint(row.get("yaw_constraint"), (-10.0, 10.0), invert_direction)
    return [
        f'$jigglebone "{name}"\n',
        "{\n",
        " is_flexible\n",
        " {\n",
        f"     length {fmt_qc_float(params['length'], 10.0)}\n",
        f"     tip_mass {fmt_qc_float(params['tip_mass'], 100.0)}\n",
        f"     pitch_constraint {pitch[0]:g} {pitch[1]:g}\n",
        f"     pitch_stiffness {fmt_qc_float(params['pitch_stiffness'], 25.0)}\n",
        f"     pitch_damping {fmt_qc_float(params['pitch_damping'], 2.0)}\n",
        f"     yaw_constraint {yaw[0]:g} {yaw[1]:g}\n",
        f"     yaw_stiffness {fmt_qc_float(params['yaw_stiffness'], 25.0)}\n",
        f"     yaw_damping {fmt_qc_float(params['yaw_damping'], 2.0)}\n",
        f"     along_stiffness {fmt_qc_float(params['along_stiffness'], 50.0)}\n",
        f"     along_damping {fmt_qc_float(params['along_damping'], 1.0)}\n",
        f"     angle_constraint {fmt_qc_float(params['angle_constraint'], 90.0)}\n",
        " }\n",
        "}\n",
    ]


def jiggle_qc_blocks(plan: dict[str, Any], excluded_bones: set[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    excluded_bones = excluded_bones or set()
    # SFM static-bones option (issue #141): jigglebone simulation bakes
    # wind-like motion into SFM sessions and fights manual animation, so the
    # toggle drops every $jigglebone block (bones stay ordinary, poseable).
    if bool(plan.get("sfm_static_bones")) and normalize_game(plan.get("game")) == "sfm":
        return [], [], []
    rows = [row for row in plan.get("rows", []) if isinstance(row, dict)]
    invert_direction = bool(plan.get("invert_jiggle_direction", False))
    jiggles: list[str] = []
    ignore: list[str] = []
    lines: list[str] = []
    for row in rows:
        name = str(row.get("bone") or "")
        jiggle_type = str(row.get("jiggle_type") or "Not Jiggle")
        if not name or name in excluded_bones or jiggle_type == "Not Jiggle" or is_essential_bone(name):
            continue
        jiggles.append(name)
        if jiggle_type == "Spring Jiggle":
            lines.extend(spring_jiggle_block(row))
        elif jiggle_type == "Omni Jiggle":
            ignore.append(name)
            lines.extend(omni_jiggle_block(row, invert_direction))
        else:
            lines.extend(directional_jiggle_block(row, invert_direction))
    return lines, jiggles, ignore


STANDARD_PHYSICS_QC_LINES = [
    '$collisionjoints "Physics.smd" \n',
    "{\n",
    "\t$mass 48 \n",
    "\t$inertia 12 \n",
    "\t$damping 0.8 \n",
    "\t$rotdamping 4 \n",
    '\t$rootbone "ValveBiped.Bip01_Pelvis" \n\n',
    '\t$jointrotdamping "ValveBiped.Bip01_Pelvis" 3 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_Spine1" 8 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_Spine1" 5 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Spine1" x limit -10 10 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Spine1" y limit -16 16 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Spine1" z limit -19 19 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_Spine4" 9 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_Spine4" 5 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Spine4" x limit -10 10 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Spine4" y limit -10 10 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Spine4" z limit -20 20 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_R_Clavicle" 4 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_R_Clavicle" 6 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Clavicle" x limit -10 10 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Clavicle" y limit -5 5 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Clavicle" z limit 0 15 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_R_UpperArm" 5 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_UpperArm" x limit -15 20 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_UpperArm" y limit -40 32 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_UpperArm" z limit -80 25 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_L_Clavicle" 4 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_L_Clavicle" 6 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Clavicle" x limit -10 10 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Clavicle" y limit -5 5 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Clavicle" z limit 0 15 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_L_UpperArm" 5 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_UpperArm" x limit -15 20 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_UpperArm" y limit -40 32 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_UpperArm" z limit -80 25 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_L_Forearm" 4 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_L_Forearm" 4 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Forearm" x limit -40 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Forearm" y limit 0 0 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Forearm" z limit -120 10 0 \n\n',
    '\t$jointrotdamping "ValveBiped.Bip01_L_Hand" 1 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Hand" x limit -25 25 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Hand" y limit -35 35 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Hand" z limit -50 50 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_R_Forearm" 4 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_R_Forearm" 4 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Forearm" x limit -40 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Forearm" y limit 0 0 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Forearm" z limit -120 10 0 \n\n',
    '\t$jointrotdamping "ValveBiped.Bip01_R_Hand" 1 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Hand" x limit -25 25 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Hand" y limit -35 35 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Hand" z limit -50 50 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_Head1" 4 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_Head1" 3 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Head1" x limit -50 50 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Head1" y limit -20 20 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_Head1" z limit -26 30 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_R_Thigh" 7 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_R_Thigh" 7 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Thigh" x limit -30 30 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Thigh" y limit -60 30 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Thigh" z limit -100 30 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_R_Calf" 4 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_R_Calf" 5 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Calf" x limit -15 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Calf" y limit -5 5 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Calf" z limit -10 125 0 \n\n',
    '\t$jointrotdamping "ValveBiped.Bip01_R_Foot" 9 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Foot" x limit -15 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Foot" y limit -15 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_R_Foot" z limit -18 25 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_L_Thigh" 7 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_L_Thigh" 7 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Thigh" x limit -30 30 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Thigh" y limit -30 60 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Thigh" z limit -100 30 0 \n\n',
    '\t$jointmassbias "ValveBiped.Bip01_L_Calf" 4 \n',
    '\t$jointrotdamping "ValveBiped.Bip01_L_Calf" 5 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Calf" x limit -15 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Calf" y limit -5 5 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Calf" z limit -10 125 0 \n\n',
    '\t$jointrotdamping "ValveBiped.Bip01_L_Foot" 9 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Foot" x limit -15 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Foot" y limit -15 15 0 \n',
    '\t$jointconstrain "ValveBiped.Bip01_L_Foot" z limit -18 25 0 \n\n',
    "}\n\n",
    "$collisiontext\n",
    "{\n",
    "\tanimatedfriction\n\t{\n",
    '\t\tanimfrictionmin\t\t"80.000000"\n',
    '\t\tanimfrictionmax\t\t"600.000000"\n',
    '\t\tanimfrictiontimein\t\t"0.150000"\n',
    '\t\tanimfrictiontimeout\t\t"0.250000"\n',
    '\t\tanimfrictiontimehold\t\t"1.750000"\n',
    "\t}\n",
    "\teditparams\n\t{\n",
    '\t\trootname\t\t"valvebiped.bip01_pelvis"\n',
    '\t\ttotalmass\t\t"50.000000"\n',
    "\t}\n",
    "}\n",
]


QC_BLOCK_COMMANDS_WITH_SEPARATE_OPEN_BRACE = (
    "$collisionjoints",
    "$collisionmodel",
)


def split_inline_qc_block_opening_braces(lines: list[str]) -> list[str]:
    normalized: list[str] = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        uses_collision_block = any(
            lower.startswith(command) for command in QC_BLOCK_COMMANDS_WITH_SEPARATE_OPEN_BRACE
        )
        if uses_collision_block and stripped.endswith("{"):
            normalized.append(line.rstrip("\r\n").rstrip()[:-1].rstrip() + "\n")
            normalized.append("{\n")
            continue
        normalized.append(line)
    return normalized


def normalize_qc_line_list(raw_lines: object) -> list[str]:
    if not isinstance(raw_lines, list):
        return []
    lines: list[str] = []
    for raw in raw_lines:
        text = str(raw)
        if not text:
            continue
        lines.append(text if text.endswith("\n") else text + "\n")
    return split_inline_qc_block_opening_braces(lines)


def qc_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except Exception:
        pass
    return float(default)


def fmt_qc_float(value: object, default: float = 0.0) -> str:
    number = qc_float(value, default)
    if math.isclose(number, round(number), abs_tol=1e-6):
        return str(int(round(number)))
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def default_physics_constraint() -> dict[str, float]:
    return {"min": 0.0, "max": 0.0, "friction": 0.0}


def physics_plan_from_qc_lines(lines: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    globals_out: dict[str, Any] = {
        "mass": 48.0,
        "inertia": 12.0,
        "damping": 0.8,
        "rotdamping": 4.0,
        "rootbone": "ValveBiped.Bip01_Pelvis",
    }
    rows_by_bone: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    collision_text_lines: list[str] = []
    in_collision_text = False

    def row_for_bone(bone: str) -> dict[str, Any]:
        if bone not in rows_by_bone:
            rows_by_bone[bone] = {
                "bone": bone,
                "enabled": True,
                "constraints": {axis: default_physics_constraint() for axis in ("x", "y", "z")},
            }
            order.append(bone)
        return rows_by_bone[bone]

    for raw_line in lines:
        line = str(raw_line)
        stripped = line.strip()
        lower = stripped.lower()
        if lower == "$collisiontext":
            in_collision_text = True
        if in_collision_text:
            collision_text_lines.append(line if line.endswith("\n") else line + "\n")
            continue
        match = re.match(r'^\$(mass|inertia|damping|rotdamping)\s+(-?\d+(?:\.\d+)?)', stripped, re.IGNORECASE)
        if match:
            globals_out[match.group(1).lower()] = qc_float(match.group(2))
            continue
        match = re.match(r'^\$rootbone\s+"([^"]+)"', stripped, re.IGNORECASE)
        if match:
            globals_out["rootbone"] = match.group(1)
            continue
        match = re.match(r'^\$jointmassbias\s+"([^"]+)"\s+(-?\d+(?:\.\d+)?)', stripped, re.IGNORECASE)
        if match:
            row = row_for_bone(match.group(1))
            row["mass_bias"] = qc_float(match.group(2))
            row["has_mass_bias"] = True
            continue
        match = re.match(r'^\$jointrotdamping\s+"([^"]+)"\s+(-?\d+(?:\.\d+)?)', stripped, re.IGNORECASE)
        if match:
            row = row_for_bone(match.group(1))
            row["rot_damping"] = qc_float(match.group(2))
            row["has_rot_damping"] = True
            continue
        match = re.match(
            r'^\$jointconstrain\s+"([^"]+)"\s+([xyz])\s+limit\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)',
            stripped,
            re.IGNORECASE,
        )
        if match:
            row = row_for_bone(match.group(1))
            constraints = row.setdefault("constraints", {axis: default_physics_constraint() for axis in ("x", "y", "z")})
            axis = match.group(2).lower()
            if isinstance(constraints, dict):
                constraints[axis] = {
                    "min": qc_float(match.group(3)),
                    "max": qc_float(match.group(4)),
                    "friction": qc_float(match.group(5)),
                }
            continue
    if not collision_text_lines:
        standard_text_start = next((index for index, line in enumerate(STANDARD_PHYSICS_QC_LINES) if line.strip().lower() == "$collisiontext"), -1)
        if standard_text_start >= 0:
            collision_text_lines = list(STANDARD_PHYSICS_QC_LINES[standard_text_start:])
    return globals_out, [rows_by_bone[bone] for bone in order], collision_text_lines


def physics_constraint_for_axis(row: dict[str, Any], axis: str) -> dict[str, float]:
    constraints = row.get("constraints")
    if isinstance(constraints, dict):
        value = constraints.get(axis)
        if isinstance(value, dict):
            return {
                "min": qc_float(value.get("min")),
                "max": qc_float(value.get("max")),
                "friction": qc_float(value.get("friction")),
            }
    return default_physics_constraint()


def build_physics_qc_lines(plan: dict[str, Any]) -> list[str]:
    rows = [row for row in plan.get("physics_rows", []) if isinstance(row, dict) and row.get("enabled", True)]
    globals_in = plan.get("physics_globals") if isinstance(plan.get("physics_globals"), dict) else {}
    rootbone = str(globals_in.get("rootbone") or "ValveBiped.Bip01_Pelvis")
    lines = [
        '$collisionjoints "Physics.smd" \n',
        "{\n",
        f"\t$mass {fmt_qc_float(globals_in.get('mass'), 48.0)} \n",
        f"\t$inertia {fmt_qc_float(globals_in.get('inertia'), 12.0)} \n",
        f"\t$damping {fmt_qc_float(globals_in.get('damping'), 0.8)} \n",
        f"\t$rotdamping {fmt_qc_float(globals_in.get('rotdamping'), 4.0)} \n",
        f'\t$rootbone "{rootbone}" \n\n',
    ]
    for row in rows:
        bone = str(row.get("bone") or "").strip()
        if not bone:
            continue
        wrote_any = False
        if bool(row.get("has_mass_bias")) or "mass_bias" in row:
            lines.append(f'\t$jointmassbias "{bone}" {fmt_qc_float(row.get("mass_bias"), 0.0)} \n')
            wrote_any = True
        if bool(row.get("has_rot_damping")) or "rot_damping" in row:
            lines.append(f'\t$jointrotdamping "{bone}" {fmt_qc_float(row.get("rot_damping"), 0.0)} \n')
            wrote_any = True
        for axis in ("x", "y", "z"):
            constraint = physics_constraint_for_axis(row, axis)
            lines.append(
                f'\t$jointconstrain "{bone}" {axis} limit '
                f'{fmt_qc_float(constraint["min"])} {fmt_qc_float(constraint["max"])} {fmt_qc_float(constraint["friction"])} \n'
            )
            wrote_any = True
        if wrote_any:
            lines.append("\n")
    lines.append("}\n\n")
    collision_text_lines = normalize_qc_line_list(plan.get("physics_collision_text_lines"))
    if not collision_text_lines:
        standard_text_start = next((index for index, line in enumerate(STANDARD_PHYSICS_QC_LINES) if line.strip().lower() == "$collisiontext"), -1)
        collision_text_lines = list(STANDARD_PHYSICS_QC_LINES[standard_text_start:]) if standard_text_start >= 0 else []
    lines.extend(collision_text_lines)
    return lines


def collision_qc_block(plan: dict[str, Any]) -> list[str]:
    # Step 8 still records whether the preview collision was produced from
    # concave-style pieces, but the compile QC intentionally omits
    # $concaveperjoint because StudioMDL collapses these thin Physics meshes
    # into a bad single convex hull when that option is present.
    if isinstance(plan.get("physics_rows"), list) and plan.get("physics_rows"):
        return build_physics_qc_lines(plan)
    inputs = plan.get("inputs", {}) if isinstance(plan.get("inputs"), dict) else {}
    physics_settings_value = str(inputs.get("step8_physics_settings") or "").strip()
    if physics_settings_value:
        physics_settings_path = Path(physics_settings_value)
    else:
        physics_settings_path = None
    if physics_settings_path and physics_settings_path.exists():
        try:
            settings = json.loads(physics_settings_path.read_text(encoding="utf-8"))
            dynamic_lines = normalize_qc_line_list(settings.get("collision_qc_lines") if isinstance(settings, dict) else [])
            if dynamic_lines and any("$collisionjoints" in line for line in dynamic_lines[:3]):
                return dynamic_lines
        except Exception as exc:
            emit(f"WARNING: Could not read Step 8 dynamic collision QC settings: {exc}")
    return list(STANDARD_PHYSICS_QC_LINES)


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def run_command(command: list[str], log_path: Path, cwd: Path | None = None) -> tuple[int, str]:
    emit("Running: " + " ".join(f'"{part}"' if " " in str(part) else str(part) for part in command))
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **hidden_subprocess_kwargs(),
    )
    captured: list[str] = []
    if process.stdout is not None:
        for raw_line in process.stdout:
            captured.append(raw_line)
            line = raw_line.rstrip("\r\n")
            if "$definebone" in line or "$hbox" in line or "ERROR" in line.upper() or "WARNING" in line.upper():
                emit(line)
    returncode = process.wait()
    output = "".join(captured)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return returncode, output


def extract_definebones(output: str) -> list[str]:
    return [line.strip() + " \n" for line in output.splitlines() if line.strip().startswith("$definebone")]


def parse_missing_parent_warnings(log_text: str) -> tuple[dict[str, set[str]], list[str]]:
    parent_to_children: dict[str, set[str]] = {}
    warnings: list[str] = []
    for line in str(log_text or "").splitlines():
        match = MISSING_PARENT_RE.search(line)
        if not match:
            continue
        child = match.group("child").strip()
        parent = match.group("parent").strip()
        parent_to_children.setdefault(parent, set()).add(child)
        warnings.append(line.strip())
    return parent_to_children, warnings


def parse_definebone_lines(lines: list[str]) -> list[DefineBone]:
    definitions: list[DefineBone] = []
    for index, line in enumerate(lines):
        match = DEFINEBONE_RE.match(line.strip())
        if match:
            tokens = tuple(match.group("rest").split())
            values: tuple[float, ...] = ()
            try:
                values = tuple(float(part) for part in tokens)
            except Exception:
                values = ()
            definitions.append(DefineBone(match.group("name"), match.group("parent"), index, values, tokens, line.strip()))
    return definitions


def scan_missing_definebone_parents(definitions: list[DefineBone]) -> dict[str, set[str]]:
    defined = {definition.name for definition in definitions}
    missing: dict[str, set[str]] = {}
    for definition in definitions:
        if definition.parent and definition.parent not in defined:
            missing.setdefault(definition.parent, set()).add(definition.name)
    return missing


def collect_smd_bone_poses(source_dir: Path) -> dict[str, SmdBonePose]:
    smd_paths = sorted(source_dir.glob("*.smd"), key=lambda item: natural_key(item.name))
    anims_dir = source_dir / "anims"
    if anims_dir.exists():
        smd_paths.extend(sorted(anims_dir.glob("*.smd"), key=lambda item: natural_key(item.name)))
    poses: dict[str, SmdBonePose] = {}
    for smd_path in smd_paths:
        try:
            smd = parse_smd(smd_path, include_triangles=False)
        except Exception as exc:
            emit(f"Definebone repair skipped unreadable SMD {smd_path.name}: {exc}")
            continue
        for node in smd.nodes.values():
            parent_name = smd.nodes[node.parent].name if node.parent in smd.nodes else ""
            poses.setdefault(
                node.name,
                SmdBonePose(
                    name=node.name,
                    parent=parent_name,
                    local_pos=node.local_pos,
                    local_rot=node.local_rot,
                    source_smd=str(smd_path.relative_to(source_dir)),
                ),
            )
    return poses


def format_definebone_float(value: float) -> str:
    if abs(value) < 0.00005:
        value = 0.0
    return f"{value:.6f}"


def make_definebone_from_smd_pose(bone: SmdBonePose, align_to_child: DefineBone | None = None) -> str:
    """$definebone line in studiomdl's parse convention: the rotation fields are the
    per-component DEGREES of the SMD/mdl RadianEuler in field order (ry, rz, rx) --
    verified against the Crowbar-decompiled base-game L4D2 survivors (coach R_UpperArm,
    R_Clavicle and L_Hand match to 5 decimals, compound rotations included)."""
    rx, ry, rz = bone.local_rot
    values = [
        f"{bone.local_pos[0]:.6f}",
        f"{bone.local_pos[1]:.6f}",
        f"{bone.local_pos[2]:.6f}",
        format_definebone_float(math.degrees(ry)),
        format_definebone_float(math.degrees(rz)),
        format_definebone_float(math.degrees(rx)),
        "0.000000",
        "0.000000",
        "0.000000",
        "0.000000",
        "0.000000",
        "0.000000",
    ]
    if align_to_child is not None and len(align_to_child.values) >= 6:
        child_values = list(align_to_child.value_tokens) if len(align_to_child.value_tokens) >= 6 else [format_definebone_float(value) for value in align_to_child.values]
        values = values[:3] + child_values[3:12]
    return f'$definebone "{bone.name}" "{bone.parent}" ' + " ".join(values) + " \n"


# Issue #168: the TLS ("The Last Stand") two-handed-melee weapon bones. All four L4D2
# survivors share this exact convention (identical local pos/RadianEuler across the
# producer/coach/gambler/mechanic reference skeletons, values below copied verbatim from
# producer), but the L4D1 survivor models never got L_weapon_bone (teenangst/biker/manager),
# so a replacement compiled on those skeletons gives two-handed melees no left-hand anchor
# and the game drops the weapon at the model origin. The included TLS animations drive these
# bones by name, so declaring them via $definebone (+ the matching $attachment) is enough --
# the community-verified fix.
L4D2_TLS_WEAPON_BONES: tuple[SmdBonePose, ...] = (
    SmdBonePose(
        name="ValveBiped.weapon_bone",
        parent="ValveBiped.Bip01_R_Hand",
        local_pos=(2.856897, -1.536200, -0.973800),
        local_rot=(-1.501147, -0.272558, -1.578261),
        source_smd="l4d2_reference_skeletons/producer.smd",
    ),
    SmdBonePose(
        name="ValveBiped.L_weapon_bone",
        parent="ValveBiped.Bip01_L_Hand",
        local_pos=(3.348999, -1.371000, 0.725000),
        local_rot=(1.977842, -0.088017, 1.557794),
        source_smd="l4d2_reference_skeletons/producer.smd",
    ),
)


def inject_l4d2_tls_weapon_bones(definebones: list[str]) -> tuple[list[str], list[str]]:
    """Ensure the TLS weapon bones exist in an L4D2 compile's $definebone set (issue #168).

    Each missing bone is inserted directly after its parent hand's $definebone line so
    parents always precede children. Bones whose parent is absent are skipped (never emit
    an orphan). Returns (definebones, injected_bone_names); no-op when everything exists."""
    definitions = parse_definebone_lines(definebones)
    existing = {definition.name for definition in definitions}
    out = list(definebones)
    injected: list[str] = []
    for bone in L4D2_TLS_WEAPON_BONES:
        if bone.name in existing or bone.parent not in existing:
            continue
        parent_index = next(
            (index for index, line in enumerate(out) if parse_definebone_lines([line]) and parse_definebone_lines([line])[0].name == bone.parent),
            None,
        )
        line = make_definebone_from_smd_pose(bone)
        if parent_index is None:
            out.append(line)
        else:
            out.insert(parent_index + 1, line)
        existing.add(bone.name)
        injected.append(bone.name)
    return out, injected


def order_smd_definebone_names(smd_poses: dict[str, SmdBonePose]) -> list[str]:
    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            emit(f"WARNING: Cycle while ordering SMD definebones at {name!r}; keeping remaining order.")
            return
        bone = smd_poses.get(name)
        if bone is None:
            return
        visiting.add(name)
        if bone.parent and bone.parent in smd_poses:
            visit(bone.parent)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in sorted(smd_poses, key=natural_key):
        visit(name)
    return ordered


def synthesize_definebones_from_smd(
    source_dir: Path,
    reason: str,
    log_text: str = "",
    pass_name: str = "smd_definebone_fallback",
) -> tuple[list[str], dict[str, Any]]:
    smd_poses = collect_smd_bone_poses(source_dir)
    ordered = order_smd_definebone_names(smd_poses)
    definebones = [make_definebone_from_smd_pose(smd_poses[name]) for name in ordered]
    _missing, warning_lines = parse_missing_parent_warnings(log_text)
    report: dict[str, Any] = {
        "pass": pass_name,
        "fallback": "smd_skeleton",
        "reason": reason,
        "warning_count": len(warning_lines),
        "warning_lines": warning_lines,
        "direct_missing_parents": {},
        "inserted_bones": [],
        "inserted_lines": [],
        "source_smds": {name: smd_poses[name].source_smd for name in ordered if name in smd_poses},
        "unrecoverable_missing_parents": [],
        "synthesized_bone_count": len(definebones),
        "synthesized_bones": ordered,
    }
    return definebones, report


def expand_missing_definebones(initial_missing: set[str], defined: set[str], smd_poses: dict[str, SmdBonePose]) -> set[str]:
    expanded = set(initial_missing)
    queue = list(initial_missing)
    while queue:
        name = queue.pop(0)
        bone = smd_poses.get(name)
        if bone is None:
            continue
        parent = bone.parent
        if parent and parent not in defined and parent not in expanded:
            expanded.add(parent)
            queue.append(parent)
    return expanded


def order_missing_definebones(
    initial_missing: set[str],
    expanded_missing: set[str],
    defined: set[str],
    definitions: list[DefineBone],
    parent_to_children: dict[str, set[str]],
    smd_poses: dict[str, SmdBonePose],
) -> list[str]:
    line_by_child = {definition.name: definition.line_index for definition in definitions}

    def first_child_line(parent: str) -> int:
        child_lines = [line_by_child[child] for child in parent_to_children.get(parent, set()) if child in line_by_child]
        return min(child_lines) if child_lines else 10**9

    seeds = sorted(initial_missing, key=lambda name: (first_child_line(name), name))
    remaining = sorted(expanded_missing - initial_missing, key=natural_key)
    result: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visited or name in defined:
            return
        if name in visiting:
            raise RuntimeError(f"Cycle while sorting missing definebones at {name!r}")
        visiting.add(name)
        bone = smd_poses.get(name)
        if bone and bone.parent and bone.parent not in defined and bone.parent in expanded_missing:
            visit(bone.parent)
        visiting.remove(name)
        visited.add(name)
        if name in expanded_missing:
            result.append(name)

    for seed in seeds:
        visit(seed)
    for extra in remaining:
        visit(extra)
    return result


def choose_definebone_insertion_index(
    definitions: list[DefineBone],
    parent_to_children: dict[str, set[str]],
    ordered_missing: list[str],
) -> int:
    child_names = {child for parent in ordered_missing for child in parent_to_children.get(parent, set())}
    candidate_indices = [definition.line_index for definition in definitions if definition.name in child_names]
    if candidate_indices:
        return min(candidate_indices)
    if definitions:
        return max(definition.line_index for definition in definitions) + 1
    return 0


def repair_definebones(
    definebones: list[str],
    source_dir: Path,
    log_text: str = "",
    pass_name: str = "initial",
) -> tuple[list[str], dict[str, Any]]:
    definitions = parse_definebone_lines(definebones)
    defined = {definition.name for definition in definitions}
    log_missing, warning_lines = parse_missing_parent_warnings(log_text)
    parent_to_children = {parent: set(children) for parent, children in log_missing.items()}
    for parent, children in scan_missing_definebone_parents(definitions).items():
        parent_to_children.setdefault(parent, set()).update(children)

    initial_missing = {parent for parent in parent_to_children if parent and parent not in defined}
    report: dict[str, Any] = {
        "pass": pass_name,
        "warning_count": len(warning_lines),
        "warning_lines": warning_lines,
        "direct_missing_parents": {parent: sorted(children, key=natural_key) for parent, children in sorted(parent_to_children.items(), key=lambda item: natural_key(item[0]))},
        "inserted_bones": [],
        "inserted_lines": [],
        "source_smds": {},
        "unrecoverable_missing_parents": [],
    }
    if not initial_missing:
        return definebones, report

    smd_poses = collect_smd_bone_poses(source_dir)
    expanded_missing = expand_missing_definebones(initial_missing, defined, smd_poses)
    patchable = {name for name in expanded_missing if name in smd_poses}
    unrecoverable = sorted(expanded_missing - patchable, key=natural_key)
    report["expanded_missing_parents"] = sorted(expanded_missing, key=natural_key)
    report["unrecoverable_missing_parents"] = unrecoverable
    if not patchable:
        return definebones, report

    ordered = order_missing_definebones(initial_missing, patchable, defined, definitions, parent_to_children, smd_poses)
    insertion_index = choose_definebone_insertion_index(definitions, parent_to_children, ordered)
    definition_by_name = {definition.name: definition for definition in definitions}
    alignments: dict[str, str] = {}
    new_lines: list[str] = []
    for name in ordered:
        children = sorted(parent_to_children.get(name, set()), key=lambda child: definition_by_name.get(child, DefineBone(child, "", 10**9)).line_index)
        align_child = next((definition_by_name[child] for child in children if child in definition_by_name), None)
        if align_child is not None:
            alignments[name] = align_child.name
        new_lines.append(make_definebone_from_smd_pose(smd_poses[name], align_child))
    repaired = definebones[:insertion_index] + new_lines + definebones[insertion_index:]
    report["inserted_bones"] = ordered
    report["support_bones_excluded_from_jiggle"] = ordered
    report["aligned_to_child_bones"] = alignments
    report["inserted_lines"] = [line.strip() for line in new_lines]
    report["source_smds"] = {name: smd_poses[name].source_smd for name in ordered}
    report["insertion_index"] = insertion_index
    return repaired, report


def write_definebone_repair_report(qc_dir: Path, reports: list[dict[str, Any]]) -> None:
    inserted: list[str] = []
    unrecoverable: list[str] = []
    warning_count = 0
    for report in reports:
        inserted.extend(str(name) for name in report.get("inserted_bones", []) if str(name))
        unrecoverable.extend(str(name) for name in report.get("unrecoverable_missing_parents", []) if str(name))
        warning_count += int(report.get("warning_count", 0) or 0)
    write_json(
        qc_dir / "missing_definebones_repair.json",
        {
            "version": 1,
            "kind": "missing_definebones_repair",
            "passes": reports,
            "inserted_bones": sorted(set(inserted), key=natural_key),
            "unrecoverable_missing_parents": sorted(set(unrecoverable), key=natural_key),
            "warning_count": warning_count,
        },
    )


def extract_hboxes(output: str) -> list[str]:
    lines: list[str] = []
    for line in output.splitlines():
        text = line.strip()
        if not text.startswith("$hbox"):
            continue
        parts = re.findall(r'"([^"]+)"', text)
        if parts:
            bone = parts[0]
            group = HBOX_GROUPS.get(bone)
            if group is not None:
                text = re.sub(r"^\$hbox\s+\d+", f"$hbox {group}", text)
                lines.append(text + " \n")
    return lines


def validate_plan(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("author", "character_category", "model_name"):
        value = str(plan.get(field) or "")
        if not value or not INTERNAL_IDENTIFIER_RE.fullmatch(value):
            errors.append(f"{field} must contain only English letters, digits, and underscores, and must not start with a digit.")
    category_readable = str(plan.get("category_readable") or "")
    if not category_readable or not CATEGORY_DISPLAY_RE.fullmatch(category_readable):
        errors.append("category_readable must contain only printable ASCII characters.")
    raw_gender = plan.get("gender")
    if raw_gender is not None and str(raw_gender).strip().lower() not in GENDER_CHOICES:
        errors.append('gender must be "female" or "male".')
    display_name = str(plan.get("display_name") or "")
    if not display_name or not DISPLAY_IDENTIFIER_RE.fullmatch(display_name):
        errors.append("display_name must contain only English letters, digits, spaces, and underscores.")
    if str(plan.get("author") or "") != "sheepylord":
        errors.append('author must be exactly "sheepylord".')
    gmod = plan.get("gmod") if isinstance(plan.get("gmod"), dict) else {}
    # NOTE: check the raw string BEFORE building a Path, and require the right
    # kind of entry. Path("") collapses to Path("."), and "." exists, so a blank
    # studiomdl_path used to pass this check; the compile then ran
    # Popen(["."]) -> "PermissionError: [WinError 5] Access is denied" (issue
    # #162, hit by users whose L4D2/SFM install was not auto-detected while
    # their GMod one was).
    game_label = GAME_DISPLAY_NAMES.get(normalize_game(plan.get("game")), "Garry's Mod")
    studiomdl_raw = str(gmod.get("studiomdl_path") or "").strip()
    if not studiomdl_raw:
        errors.append(
            f"No studiomdl.exe is set for {game_label}. Set the {game_label} install folder or "
            "studiomdl.exe in Step 1 (manual) or on the main interface (auto-port)."
        )
    elif not Path(studiomdl_raw).is_file():
        errors.append(f"studiomdl.exe for {game_label} was not found at: {studiomdl_raw}")
    game_dir_raw = str(gmod.get("game_dir") or "").strip()
    if not game_dir_raw:
        errors.append(
            f"No {game_label} game directory is set. Set the {game_label} install folder in Step 1 "
            "(manual) or on the main interface (auto-port)."
        )
    elif not Path(game_dir_raw).is_dir():
        errors.append(f"The {game_label} game directory was not found at: {game_dir_raw}")
    jiggles = {str(row.get("bone") or "") for row in plan.get("rows", []) if isinstance(row, dict) and str(row.get("jiggle_type") or "") != "Not Jiggle"}
    ignores = {str(row.get("bone") or "") for row in plan.get("rows", []) if isinstance(row, dict) and str(row.get("jiggle_type") or "") == "Omni Jiggle"}
    for bone in sorted(ignores - jiggles):
        errors.append(f"bone_list_ignore contains non-jiggle bone: {bone}")
    for bone in sorted(jiggles):
        if is_essential_bone(bone):
            errors.append(f"Essential bone cannot be jigglebone: {bone}")
    for row in [entry for entry in plan.get("rows", []) if isinstance(entry, dict)]:
        bone = str(row.get("bone") or "")
        for key in ("pitch_constraint", "yaw_constraint"):
            values = row.get(key)
            if isinstance(values, list) and len(values) >= 2:
                low = qc_float(values[0])
                high = qc_float(values[1])
                if low > high:
                    errors.append(f"{bone} {key} minimum is greater than maximum.")
    for row in [entry for entry in plan.get("physics_rows", []) if isinstance(entry, dict)]:
        bone = str(row.get("bone") or "")
        constraints = row.get("constraints")
        if not isinstance(constraints, dict):
            continue
        for axis in ("x", "y", "z"):
            value = constraints.get(axis)
            if not isinstance(value, dict):
                continue
            low = qc_float(value.get("min"))
            high = qc_float(value.get("max"))
            if low > high:
                errors.append(f"{bone} physics {axis.upper()} constraint minimum is greater than maximum.")
    return errors


def is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def next_power_of_two(value: int, maximum: int = 4096) -> int:
    if value <= 1:
        return 1
    return min(1 << (int(value) - 1).bit_length(), maximum)


def texture_needs_vtf_normalization(image_path: Path) -> tuple[bool, tuple[int, int], int]:
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # pragma: no cover - only hit on broken local installs
        raise RuntimeError(
            f"Pillow is required to inspect texture dimensions before VTFCmd conversion: {exc}"
        ) from exc
    with Image.open(image_path) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
    target_side = next_power_of_two(max(width, height), 4096)
    needs_resize = (
        not is_power_of_two(width)
        or not is_power_of_two(height)
        or width > 4096
        or height > 4096
    )
    return needs_resize, (width, height), target_side


def write_vtf_safe_texture_copy(source: Path, destination: Path, target_side: int) -> tuple[tuple[int, int], tuple[int, int]]:
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # pragma: no cover - only hit on broken local installs
        raise RuntimeError(
            f"Pillow is required to normalize non-power-of-two textures before VTFCmd conversion: {exc}"
        ) from exc
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        original_size = image.size
        mode = "RGBA" if ("A" in image.getbands() or image.mode in {"P", "LA"}) else "RGB"
        image = image.convert(mode)
        # VTFCmd is strict about power-of-two dimensions.  Stretch to a square
        # power-of-two target so normalized UVs preserve the in-game mapping.
        image = image.resize((target_side, target_side), Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        return original_size, image.size


def convert_one_vtf(vtfcmd: Path, image_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected = output_dir / f"{image_path.stem}.vtf"
    actual_image = image_path
    actual_output_dir = output_dir
    staged_expected: Path | None = None
    scratch: Path | None = None
    normalize_for_vtf, original_size, target_side = texture_needs_vtf_normalization(image_path)
    needs_staging = normalize_for_vtf or not path_is_ascii(image_path) or not path_is_ascii(output_dir)
    try:
        if needs_staging:
            scratch_key = output_dir / f"{image_path.name}_{'pot' if normalize_for_vtf else 'ascii'}"
            scratch = external_safe_dir_for(scratch_key, "vtf")
            if scratch.exists():
                shutil.rmtree(scratch)
            scratch_input = scratch / "input"
            scratch_output = scratch / "output"
            scratch_input.mkdir(parents=True, exist_ok=True)
            scratch_output.mkdir(parents=True, exist_ok=True)
            safe_stem = safe_name(image_path.stem, "texture")
            if normalize_for_vtf:
                actual_image = scratch_input / f"{safe_stem}.png"
                _before, after_size = write_vtf_safe_texture_copy(image_path, actual_image, target_side)
                emit(
                    "Normalized texture for VTFCmd power-of-two conversion: "
                    f"{image_path.name} {original_size[0]}x{original_size[1]} -> {after_size[0]}x{after_size[1]}"
                )
            else:
                actual_image = scratch_input / f"{safe_stem}{image_path.suffix.lower()}"
                shutil.copyfile(image_path, actual_image)
            actual_output_dir = scratch_output
            staged_expected = actual_output_dir / f"{actual_image.stem}.vtf"
            if not path_is_ascii(image_path) or not path_is_ascii(output_dir):
                emit(f"Staging VTF conversion through ASCII path: {actual_image}")
        command = [str(vtfcmd), "-file", str(actual_image), "-output", str(actual_output_dir), "-silent"]
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(vtfcmd.parent),
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"VTFCmd failed for {image_path.name}: {completed.stdout}")
        if staged_expected is not None:
            if not staged_expected.exists():
                raise RuntimeError(f"VTFCmd finished but did not create {staged_expected.name}")
            shutil.copyfile(staged_expected, expected)
        if not expected.exists():
            raise RuntimeError(f"VTFCmd finished but did not create {expected.name}")
        return expected
    finally:
        if scratch is not None and scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)


def l4d2_shared_material_root(author: str, model_name: str, game: str) -> str:
    """Path (under materials/) of the generic shared textures (normal/phong_exp/lightwarp).

    GMod uses an author-level folder (models/<author>/shared). For L4D2 that collides across EVERY
    addon by the same author -- two enabled addons shipping the same file path are flagged as a
    conflict in the L4D2 add-ons menu even when they replace different survivors -- so L4D2
    namespaces it PER-MODEL (models/<author>/<model>/shared) to keep each VPK self-contained."""
    if normalize_game(game) == "l4d2":
        return f"models/{author}/{model_name}/shared"
    return f"models/{author}/shared"


# Material-name tokens that keep alpha-test transparency in SFM. The SFM expert
# removes transparency from almost every material (it disables the in-editor
# outline/"Lines" feature, turning the model fully transparent) EXCEPT materials
# used for eyes, effects, or those with a background texture (black background) --
# where the alpha is actually load-bearing. We keep $alphatest for materials whose
# name matches one of these, and drop it for the rest.
SFM_ALPHATEST_KEEP_HINTS = (
    "eye", "effect", "fx", "glass", "tear", "cornea", "highlight",
    "halo", "glow", "star", "transparent", "alpha", "decal", "screen",
)


def sfm_material_keeps_alphatest(material_name: str) -> bool:
    lname = str(material_name or "").strip().lower()
    return any(token in lname for token in SFM_ALPHATEST_KEEP_HINTS)


def write_vmt(
    path: Path,
    author: str,
    model_name: str,
    material_name: str,
    has_normal: bool,
    phongexp_texture: str | None = None,
    selfillum_mask: str | None = None,
    game: str = "gmod",
    nodecal: bool = False,
    base_name: str | None = None,
    normal_name: str | None = None,
) -> None:
    target = normalize_game(game)
    sfm = target == "sfm"
    shared = l4d2_shared_material_root(author, model_name, game)
    # base_name / normal_name let a de-duplicated material point its $basetexture / $bumpmap at
    # another material's shared VTF (merge-identical-textures). Default to this material's own name,
    # which keeps the VMT byte-identical to before.
    base_name = base_name or material_name
    normal_name = normal_name or material_name
    bump = f"models/{author}/{model_name}/{normal_name}_n" if has_normal else f"{shared}/normal"
    # A per-material phong-exponent map (Step 12 PBR scheme) carries gloss in
    # red and metallic in alpha, replacing the shared static exponent. When
    # absent (legacy/auto-port), fall back to the shared texture so the VMT is
    # byte-identical to before.
    phong = phongexp_texture or f"{shared}/phong_exp"
    phong_fresnel = "[0.0 1.5 2]" if has_normal else "[0.0 0.5 1]"
    # SFM keeps alpha-test only for eye/effect/transparent materials; GMod/L4D2
    # keep it on every material (unchanged, byte-identical).
    include_alphatest = (not sfm) or sfm_material_keeps_alphatest(material_name)
    body = (
        "VertexLitGeneric\n"
        "{\n"
        f'\t$basetexture "models/{author}/{model_name}/{base_name}"\n'
        f'\t$bumpmap "{bump}"\n'
        '\t$nocull "1"\n'
    )
    # $nodecal stops decals (bullet/blood marks) from sticking to the material. Default off keeps
    # GMod/SFM output byte-identical; the Step 12 / main-interface toggle turns it on (default on
    # for L4D2 survivors, where decals on a custom anime mesh look wrong).
    if nodecal:
        body += '\t$nodecal "1"\n'
    if include_alphatest:
        body += (
            '\t$alphatest "1"\n'
            "\t$alphatestreference 0.5\n"
        )
    body += (
        '\t$allowalphatocoverage "1"\n'
        f'\t$lightwarptexture "{shared}/lightwarptexture"\n'
        '\t$phong "1"\n'
        '\t$phongboost "1"\n'
        '\t$phongalbedotint "1"\n'
        f'\t$phongexponenttexture "{phong}"\n'
        f'\t$phongfresnelranges "{phong_fresnel}"\n'
    )
    # SFM drops rim light ("re-lighting" / raylight): unnecessary for SFM (it is
    # not a game), per the expert. GMod/L4D2 keep it (unchanged).
    if not sfm:
        body += (
            '\t$rimlight "1"\n'
            '\t$rimlightexponent "2"\n'
            '\t$rimlightboost "2"\n'
        )
    if selfillum_mask:
        body += (
            '\t$selfillum "1"\n'
            f'\t$selfillummask "{selfillum_mask}"\n'
        )
    body += "}\n"
    path.write_text(body, encoding="utf-8")


def parse_used_materials(compile_outputs: list[str]) -> set[str]:
    """Material names studiomdl actually processed, from its 'Processing LOD for material: X' lines,
    normalized (safe_name + lower) to match the VMT filenames. Union across every compiled model
    (main / player / c_arms) so a material used by ANY of them is never treated as unused."""
    used: set[str] = set()
    for output in compile_outputs:
        for match in re.finditer(r"Processing LOD for material:\s*(.+)", str(output or "")):
            name = match.group(1).strip()
            if name:
                used.add(safe_name(name, "material").lower())
    return used


def _texture_md5(path: Path) -> str | None:
    """Content checksum of a texture PNG (used to merge byte-identical textures). None on error."""
    try:
        digest = hashlib.md5()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def compose_materials(
    plan: dict[str, Any],
    addon_dir: Path,
    used_materials: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    author = str(plan["author"])
    model = str(plan["model_name"])
    game = normalize_game(plan.get("game"))
    # $nodecal toggle (set by the GUI); falls back to the game default (on for L4D2) when the plan
    # predates the key, so old/external plans still get the expected behavior.
    nodecal = bool(plan.get("nodecal", game == "l4d2"))
    # Merge-identical-textures toggle (Step 12 / main interface, default on): dedupe byte-identical
    # textures to a single VTF (the VMT references the shared one) AND drop VTFs for materials the
    # compiled model never uses. Off -> byte-identical to the old per-material output.
    merge = bool(plan.get("merge_identical_textures", True))
    out_dir = addon_dir / "materials" / "models" / author / model
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    errors: list[str] = []
    files: list[dict[str, Any]] = []
    vtfcmd = find_vtfcmd()
    if vtfcmd is None:
        warnings.append("VTFCmd.exe was not found; material VTF files were not generated.")

    def row_name(row: dict[str, Any]) -> str:
        return safe_name(str(row.get("output_name") or row.get("material_name") or "material"), "material")

    rows = [row for row in plan.get("material_rows", []) if isinstance(row, dict)]

    # Drop materials the compiled model(s) don't reference (e.g. a bodygroup the user removed while
    # editing). Safe: only when merging is on AND we actually parsed a non-empty used set (otherwise
    # keep everything). Done BEFORE building the dedupe map so a kept material never references a
    # dropped material's texture.
    if merge and used_materials:
        kept_rows: list[dict[str, Any]] = []
        dropped_names: list[str] = []
        for row in rows:
            if row_name(row).lower() in used_materials:
                kept_rows.append(row)
            else:
                dropped_names.append(row_name(row))
        rows = kept_rows
        if dropped_names:
            emit(f"Merge textures: skipping {len(dropped_names)} unused material(s): {', '.join(sorted(dropped_names))}")

    # Per-slot dedupe map: content-checksum -> the FIRST (canonical) material carrying that texture.
    SLOT_PNG = {"base": "base_png", "normal": "normal_png", "phongexp": "phongexp_png", "selfillum": "selfillum_png"}

    def slot_digest(row: dict[str, Any], slot: str) -> str | None:
        raw = str(row.get(SLOT_PNG[slot]) or "").strip()
        if not raw:
            return None
        path = Path(raw)
        if not (path.is_file() and path.stat().st_size > 0):
            return None
        return _texture_md5(path)

    row_digests = [{slot: slot_digest(row, slot) for slot in SLOT_PNG} for row in rows]
    canonical: dict[str, dict[str, str]] = {slot: {} for slot in SLOT_PNG}
    if merge:
        for row, digests in zip(rows, row_digests):
            name = row_name(row)
            for slot, digest in digests.items():
                if digest:
                    canonical[slot].setdefault(digest, name)
    dedup_saved = 0

    for row, digests in zip(rows, row_digests):
        material_name = row_name(row)

        def canon(slot: str) -> str:
            digest = digests[slot]
            return canonical[slot].get(digest, material_name) if (merge and digest) else material_name

        base_canon, normal_canon = canon("base"), canon("normal")
        phongexp_canon, selfillum_canon = canon("phongexp"), canon("selfillum")
        base_png_raw = str(row.get("base_png") or "").strip()
        normal_png_raw = str(row.get("normal_png") or "").strip()
        phongexp_png_raw = str(row.get("phongexp_png") or "").strip()
        selfillum_png_raw = str(row.get("selfillum_png") or "").strip()
        base_png, normal_png = Path(base_png_raw), Path(normal_png_raw)
        phongexp_png, selfillum_png = Path(phongexp_png_raw), Path(selfillum_png_raw)
        base_vtf = out_dir / f"{material_name}.vtf"
        normal_vtf = out_dir / f"{material_name}_n.vtf"
        phongexp_vtf = out_dir / f"{material_name}_exp.vtf"
        selfillum_vtf = out_dir / f"{material_name}_selfillum.vtf"
        has_normal = False
        phongexp_texture: str | None = None
        selfillum_mask: str | None = None
        # BASE: only the canonical material writes its own VTF; a duplicate emits nothing and its
        # VMT points $basetexture at the canonical below.
        try:
            if base_canon != material_name:
                dedup_saved += 1
            elif vtfcmd and base_png_raw and base_png.is_file():
                converted = convert_one_vtf(vtfcmd, base_png, out_dir)
                if converted != base_vtf and converted.exists():
                    converted.replace(base_vtf)
                files.append(file_row(base_vtf, "material_vtf"))
            elif not base_png_raw or not base_png.is_file():
                warnings.append(f"Missing base PNG for material {material_name}: {base_png}")
        except Exception as exc:
            warnings.append(str(exc))
        try:
            if normal_png_raw and normal_png.is_file() and normal_png.stat().st_size > 0:
                has_normal = True
                if normal_canon != material_name:
                    dedup_saved += 1
                elif vtfcmd:
                    converted = convert_one_vtf(vtfcmd, normal_png, out_dir)
                    if converted != normal_vtf and converted.exists():
                        converted.replace(normal_vtf)
                    has_normal = normal_vtf.exists()
                    files.append(file_row(normal_vtf, "normal_vtf"))
        except Exception as exc:
            warnings.append(str(exc))
        try:
            if phongexp_png_raw and phongexp_png.is_file() and phongexp_png.stat().st_size > 0:
                phongexp_texture = f"models/{author}/{model}/{phongexp_canon}_exp"
                if phongexp_canon != material_name:
                    dedup_saved += 1
                elif vtfcmd:
                    converted = convert_one_vtf(vtfcmd, phongexp_png, out_dir)
                    if converted != phongexp_vtf and converted.exists():
                        converted.replace(phongexp_vtf)
                    if phongexp_vtf.exists():
                        files.append(file_row(phongexp_vtf, "phongexp_vtf"))
                    else:
                        phongexp_texture = None
        except Exception as exc:
            warnings.append(str(exc))
        try:
            if selfillum_png_raw and selfillum_png.is_file() and selfillum_png.stat().st_size > 0:
                selfillum_mask = f"models/{author}/{model}/{selfillum_canon}_selfillum"
                if selfillum_canon != material_name:
                    dedup_saved += 1
                elif vtfcmd:
                    converted = convert_one_vtf(vtfcmd, selfillum_png, out_dir)
                    if converted != selfillum_vtf and converted.exists():
                        converted.replace(selfillum_vtf)
                    if selfillum_vtf.exists():
                        files.append(file_row(selfillum_vtf, "selfillum_vtf"))
                    else:
                        selfillum_mask = None
        except Exception as exc:
            warnings.append(str(exc))
        vmt = out_dir / f"{material_name}.vmt"
        write_vmt(
            vmt, author, model, material_name, has_normal, phongexp_texture, selfillum_mask,
            game=game, nodecal=nodecal, base_name=base_canon, normal_name=normal_canon,
        )
        files.append(file_row(vmt, "material_vmt"))
    if merge and dedup_saved:
        emit(f"Merge textures: {dedup_saved} duplicate texture(s) share an existing VTF (no extra file written).")
    shared_src = ROOT / "reference" / "li_zhiyan_npc" / "a_pack" / "materials" / "models" / "sheepylord" / "shared"
    # Mirror l4d2_shared_material_root(): per-model for L4D2 (self-contained, no cross-addon
    # conflict), author-level for GMod (byte-identical).
    if game == "l4d2":
        shared_dst = addon_dir / "materials" / "models" / author / model / "shared"
    else:
        shared_dst = addon_dir / "materials" / "models" / author / "shared"
    shared_dst.mkdir(parents=True, exist_ok=True)
    for name in ("lightwarptexture.vtf", "normal.vtf", "phong_exp.vtf"):
        src = shared_src / name
        dst = shared_dst / name
        if src.exists():
            shutil.copyfile(src, dst)
            files.append(file_row(dst, "shared_material"))
        else:
            warnings.append(f"Shared material fallback missing: {src}")
    return files, warnings, errors


def compose_icons(plan: dict[str, Any], addon_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    files: list[dict[str, Any]] = []
    author = str(plan["author"])
    model = str(plan["model_name"])
    out_dir = addon_dir / "materials" / "vgui" / "entities"
    out_dir.mkdir(parents=True, exist_ok=True)
    step13 = Path(str(plan.get("inputs", {}).get("step13_dir") or ""))
    fallback = ROOT / "reference" / "li_zhiyan_npc" / "a_pack" / "materials" / "vgui" / "entities"
    for suffix, fallback_name in (("F", "Yumemizuki_F.vtf"), ("E", "Yumemizuki_E.vtf")):
        src = step13 / f"{suffix}.vtf"
        if not src.exists():
            src = fallback / fallback_name
            warnings.append(f"Step 13 {suffix}.vtf missing; using fallback icon." if src.exists() else f"Icon VTF missing: {suffix}.vtf")
        dst = out_dir / f"{model}_{author}_{suffix}.vtf"
        if src.exists():
            shutil.copyfile(src, dst)
            files.append(file_row(dst, "vgui_icon"))
        vmt = out_dir / f"{model}_{author}_{suffix}.vmt"
        vmt.write_text(
            "UnlitGeneric\n"
            "{\n"
            f'\t$basetexture "vgui/entities/{model}_{author}_{suffix}"\n'
            "\t$vertexalpha 1\n"
            "\t$vertexcolor 1\n"
            "}\n",
            encoding="utf-8",
        )
        files.append(file_row(vmt, "vgui_vmt"))
    return files, warnings


def write_lua(plan: dict[str, Any], addon_dir: Path, has_carms: bool) -> Path:
    author = str(plan["author"])
    category = str(plan["character_category"])
    model = str(plan["model_name"])
    display = safe_display_identifier(str(plan.get("display_name") or model.replace("_", " ").title()), model.replace("_", " ").title())
    category_readable = safe_category_display_identifier(str(plan.get("category_readable") or category_display_from_identifier(category)))
    lua_dir = addon_dir / "lua" / "autorun"
    lua_dir.mkdir(parents=True, exist_ok=True)
    path = lua_dir / f"{model}_{author}.lua"
    lines = [
        f'player_manager.AddValidModel({lua_string(display)}, "models/{author}/{category}/{model}_pm.mdl");\n',
    ]
    if has_carms:
        lines.append(f'player_manager.AddValidHands({lua_string(display)}, "models/{author}/{category}/{model}_arms.mdl" , 0, "000000")\n\n')
    lines.extend(
        [
            f"local Category = {lua_string(category_readable)}\n\n",
            "local NPC = {\n",
            f"    Name = {lua_string(display + ' (Friendly)')},\n",
            '    Class = "npc_citizen",\n',
            f'    Model = "models/{author}/{category}/{model}.mdl",\n',
            '    Health = "100",\n',
            '    KeyValues = { citizentype = 4 },\n',
            '    Weapons = { "weapon_smg1" },\n',
            "    Category = Category\n",
            "}\n\n",
            f'list.Set("NPC", "{model}_{author}_F", NPC)\n\n',
            "local NPC = {\n",
            f"    Name = {lua_string(display + ' (Enemy)')},\n",
            '    Class = "npc_combine_s",\n',
            f'    Model = "models/{author}/{category}/{model}.mdl",\n',
            '    Health = "100",\n',
            '    Numgrenades = "4",\n',
            '    Weapons = { "weapon_ar2" },\n',
            "    Category = Category\n",
            "}\n\n",
            f'list.Set("NPC", "{model}_{author}_E", NPC)\n',
        ]
    )
    path.write_text("".join(lines), encoding="utf-8")
    return path


def write_simple_vrd_immunity(plan: dict[str, Any], addon_dir: Path, has_carms: bool) -> Path:
    author = str(plan["author"])
    category = str(plan["character_category"])
    model = str(plan["model_name"])
    model_paths = [
        f"models/{author}/{category}/{model}.mdl",
        f"models/{author}/{category}/{model}_pm.mdl",
    ]
    if has_carms:
        model_paths.append(f"models/{author}/{category}/{model}_arms.mdl")
    payload = {
        "version": 1,
        "kind": "simple_vrd_static_immunity",
        "generated_by": "MMD Character Importer Step 14",
        "immune": True,
        "reason": "This model already ships generated VRD/procedural skirt handling or was authored by the MMD Character Importer workflow.",
        "model_paths": model_paths,
        "model_prefixes": [],
        "author": author,
        "character_category": category,
        "model_name": model,
    }
    static_dir = addon_dir / "data_static" / "skirt_vrd_driver" / "immune_models"
    static_dir.mkdir(parents=True, exist_ok=True)
    path = static_dir / f"{model}_{author}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_dynamic_model_importer_manifest(plan: dict[str, Any], addon_dir: Path, has_carms: bool) -> Path:
    author = str(plan["author"])
    category = str(plan["character_category"])
    model = str(plan["model_name"])
    display = safe_display_identifier(str(plan.get("display_name") or model.replace("_", " ").title()), model.replace("_", " ").title())
    category_readable = safe_category_display_identifier(str(plan.get("category_readable") or category_display_from_identifier(category)))
    manifest_id = safe_lower(f"{model}_{author}", "model")
    model_path = f"models/{author}/{category}/{model}.mdl"
    player_model_path = f"models/{author}/{category}/{model}_pm.mdl"
    arms_model_path = f"models/{author}/{category}/{model}_arms.mdl" if has_carms else ""
    payload = {
        "version": 1,
        "kind": "dynamic_model_importer_manifest",
        "generated_by": "MMD Character Importer Step 14",
        "manifest_id": manifest_id,
        "addon_id": manifest_id,
        "author": author,
        "character_category": category,
        "category_readable": category_readable,
        "model_name": model,
        "display_name": display,
        "paths": {
            "model": model_path,
            "player_model": player_model_path,
            "arms_model": arms_model_path,
            "friendly_icon": f"materials/vgui/entities/{model}_{author}_F.vtf",
            "enemy_icon": f"materials/vgui/entities/{model}_{author}_E.vtf",
        },
        "npc_defaults": {
            "relation": "friendly",
            "health": 100,
            "weapon": "weapon_smg1",
            "friendly": {
                "class": "npc_citizen",
                "weapon": "weapon_smg1",
                "keyvalues": {"citizentype": "4"},
            },
            "hostile": {
                "class": "npc_combine_s",
                "weapon": "weapon_ar2",
                "keyvalues": {"Numgrenades": "4"},
            },
            "neutral": {
                "class": "npc_citizen",
                "weapon": "weapon_smg1",
                "keyvalues": {"citizentype": "4"},
            },
        },
    }
    static_dir = addon_dir / "data_static" / "dynamic_model_importer" / "models"
    static_dir.mkdir(parents=True, exist_ok=True)
    path = static_dir / f"{manifest_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def compile_one(studiomdl: Path, game_dir: Path, qc_path: Path, log_path: Path) -> str:
    code, output = run_command([str(studiomdl), "-game", str(game_dir), "-nop4", "-verbose", str(qc_path)], log_path, cwd=qc_path.parent)
    if code != 0:
        raise StudioMDLCompileError(qc_path, log_path, output, code)
    return output


def is_collision_block_parser_failure(error: StudioMDLCompileError) -> bool:
    try:
        qc_lines = error.qc_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        qc_lines = []
    for match in BAD_OPEN_BRACE_RE.finditer(error.output or ""):
        try:
            line_number = int(match.group("line"))
        except ValueError:
            continue
        candidates = []
        if 1 <= line_number <= len(qc_lines):
            candidates.append(qc_lines[line_number - 1].strip().lower())
        if 1 < line_number <= len(qc_lines):
            candidates.append(qc_lines[line_number - 2].strip().lower())
        for candidate in candidates:
            if any(candidate.startswith(command) for command in QC_BLOCK_COMMANDS_WITH_SEPARATE_OPEN_BRACE):
                return True
    return False


def source_has_vta_files(source_dir: Path) -> bool:
    return any(source_dir.glob("*.vta"))


def is_vta_model_compile_failure(error: StudioMDLCompileError, source_dir: Path) -> bool:
    if not source_has_vta_files(source_dir):
        return False
    text = error.output or ""
    if not text:
        try:
            text = error.log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
    upper = text.upper()
    return "VTA MODEL" in upper


def is_too_many_verts_compile_failure(error: StudioMDLCompileError) -> bool:
    upper = (error.output or "").upper()
    if "TOO MANY VERTS IN MODEL" in upper or "MAXSTUDIOVERTS" in upper:
        return True
    try:
        log_text = error.log_path.read_text(encoding="utf-8", errors="replace").upper()
    except Exception:
        log_text = ""
    return "TOO MANY VERTS IN MODEL" in log_text or "MAXSTUDIOVERTS" in log_text


def find_studiomdl_compile_error(exc: BaseException) -> StudioMDLCompileError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, StudioMDLCompileError):
            return current
        current = current.__cause__ or current.__context__
    return None


def failure_report_from_exception(plan: dict[str, Any], exc: BaseException, traceback_text: str) -> dict[str, Any]:
    warnings = list(plan.get("warnings", [])) if isinstance(plan.get("warnings"), list) else []
    compile_error = find_studiomdl_compile_error(exc)
    failure: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "traceback": traceback_text,
    }
    if compile_error is not None:
        failure["studiomdl"] = compile_error.to_report()
    return {
        "version": 1,
        "kind": "sort_qc_compile_report",
        "status": "failed",
        "addon_dir": str(plan.get("addon_dir") or ""),
        "source_dir": str(plan.get("source_dir") or ""),
        "validation": {"ok": False, "errors": [str(exc)], "warnings": warnings},
        "validation_errors": [str(exc)],
        "warnings": warnings,
        "failure": failure,
        "studiomdl_failure": failure.get("studiomdl", {}),
    }


def write_failure_outputs(plan_path: Path, exc: BaseException) -> tuple[Path, Path]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        plan = {}
    qc_dir = Path(str(plan.get("qc_dir") or plan_path.parent))
    qc_dir.mkdir(parents=True, exist_ok=True)
    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    (qc_dir / "qc_failure_traceback.log").write_text(traceback_text, encoding="utf-8")
    report = failure_report_from_exception(plan, exc, traceback_text)
    report_path = qc_dir / "qc_report.json"
    files_path = qc_dir / "qc_files.json"
    write_json(report_path, report)
    files_payload = {
        "addon_dir": str(plan.get("addon_dir") or ""),
        "source_dir": str(plan.get("source_dir") or ""),
        "files": [],
        "validation": report["validation"],
        "validation_errors": report["validation_errors"],
        "warnings": report["warnings"],
        "failure": report["failure"],
        "studiomdl_failure": report["studiomdl_failure"],
    }
    write_json(files_path, files_payload)
    return report_path, files_path


def read_raw_smd_triangle_sections(path: Path) -> tuple[list[str], list[list[str]]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "triangles":
            continue
        prefix = lines[: index + 1]
        triangles: list[list[str]] = []
        cursor = index + 1
        while cursor < len(lines):
            if lines[cursor].strip() == "end":
                break
            if cursor + 3 >= len(lines):
                break
            triangles.append(lines[cursor : cursor + 4])
            cursor += 4
        return prefix, triangles
    return lines, []


def write_raw_smd_triangle_chunk(path: Path, prefix: list[str], triangles: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in prefix:
            handle.write(line.rstrip("\r\n") + "\n")
        for triangle in triangles:
            for line in triangle:
                handle.write(line.rstrip("\r\n") + "\n")
        handle.write("end\n")


def split_smd_chunk_stems(stem: str, chunk_count: int, reserved: set[str]) -> list[str]:
    stems: list[str] = []
    for index in range(1, chunk_count + 1):
        base = f"{stem}_{index:02d}"
        candidate = base
        suffix = 2
        while candidate.lower() in reserved:
            candidate = f"{base}_part{suffix:02d}"
            suffix += 1
        reserved.add(candidate.lower())
        stems.append(candidate)
    return stems


def split_oversized_smds_for_compile(source_dir: Path, vertex_budget: int = SMD_SPLIT_RAW_VERTEX_BUDGET) -> dict[str, Any]:
    smds = [
        path
        for path in sorted(source_dir.glob("*.smd"), key=lambda item: natural_key(item.name))
        if path.name.lower() != "physics.smd"
    ]
    reserved = {path.stem.lower() for path in smds}
    backup_dir = source_dir / "_oversized_smd_originals"
    report: dict[str, Any] = {
        "version": 1,
        "kind": "oversized_smd_split_report",
        "raw_vertex_budget": vertex_budget,
        "max_studiomdl_model_verts": MAX_STUDIOMDL_MODEL_VERTS,
        "splits": [],
    }
    for smd in smds:
        prefix, triangles = read_raw_smd_triangle_sections(smd)
        raw_vertex_count = len(triangles) * 3
        if not triangles or raw_vertex_count <= vertex_budget:
            continue
        chunks: list[list[list[str]]] = []
        current: list[list[str]] = []
        current_vertices = 0
        for triangle in triangles:
            if current and current_vertices + 3 > vertex_budget:
                chunks.append(current)
                current = []
                current_vertices = 0
            current.append(triangle)
            current_vertices += 3
        if current:
            chunks.append(current)
        if len(chunks) <= 1:
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / smd.name
        shutil.copyfile(smd, backup_path)
        chunk_stems = split_smd_chunk_stems(smd.stem, len(chunks), reserved)
        chunk_rows: list[dict[str, Any]] = []
        for chunk_stem, chunk in zip(chunk_stems, chunks):
            chunk_path = source_dir / f"{chunk_stem}.smd"
            write_raw_smd_triangle_chunk(chunk_path, prefix, chunk)
            chunk_rows.append(
                {
                    "name": chunk_path.name,
                    "triangle_count": len(chunk),
                    "raw_vertex_count": len(chunk) * 3,
                }
            )
        smd.unlink()
        report["splits"].append(
            {
                "source": smd.name,
                "backup": str(backup_path),
                "removed_original": str(smd),
                "original_triangle_count": len(triangles),
                "original_raw_vertex_count": raw_vertex_count,
                "chunk_count": len(chunks),
                "chunks": chunk_rows,
            }
        )
    return report


def fallback_gmod_compile_candidates(gmod: dict[str, Any]) -> list[dict[str, str]]:
    current_install = Path(str(gmod.get("install_root") or ""))
    current_studiomdl = Path(str(gmod.get("studiomdl_path") or ""))
    roots: list[Path] = []
    if current_install:
        roots.append(current_install.parent / "GarrysMod")
    for library in steam_library_roots():
        roots.append(library / "steamapps" / "common" / "GarrysMod")
    roots.append(Path(r"C:\Program Files (x86)\Steam\steamapps\common\GarrysMod"))

    seen: set[str] = set()
    candidates: list[dict[str, str]] = []
    for root in roots:
        try:
            resolved_root = root.resolve()
        except Exception:
            resolved_root = root
        key = str(resolved_root).lower()
        if key in seen:
            continue
        seen.add(key)
        hit = validate_gmod_root(resolved_root)
        if not hit:
            continue
        candidate_studiomdl = Path(str(hit.get("studiomdl_path") or ""))
        if current_studiomdl and same_resolved_path(candidate_studiomdl, current_studiomdl):
            continue
        hit["source"] = "collision_compile_fallback"
        candidates.append(hit)
    return candidates


def clean_expected_compiled(game_dir: Path, author: str, category: str, stems: list[str]) -> None:
    out_dir = game_dir / "models" / author / category
    for stem in stems:
        for ext in COMPILED_EXTENSIONS:
            path = out_dir / f"{stem}{ext}"
            if path.exists():
                path.unlink()


def copy_compiled_outputs(game_dir: Path, addon_dir: Path, author: str, category: str, stems: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    source_dir = game_dir / "models" / author / category
    target_dir = addon_dir / "models" / author / category
    target_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    for stem in stems:
        found = False
        for ext in COMPILED_EXTENSIONS:
            src = source_dir / f"{stem}{ext}"
            if src.exists():
                dst = target_dir / src.name
                shutil.copyfile(src, dst)
                files.append(file_row(dst, "compiled_model"))
                found = True
        if not found:
            errors.append(f"Compiled model files not found for {stem} in {source_dir}")
    return files, errors


def same_resolved_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except Exception:
        return str(a.absolute()).lower() == str(b.absolute()).lower()


def path_is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def find_gmad_executable(gmod: dict[str, Any]) -> Path:
    candidates: list[Path] = []
    install_root = Path(str(gmod.get("install_root") or ""))
    game_dir = Path(str(gmod.get("game_dir") or ""))
    if install_root:
        candidates.append(install_root / "bin" / "gmad.exe")
    if game_dir:
        if game_dir.name.lower() == "garrysmod":
            candidates.append(game_dir.parent / "bin" / "gmad.exe")
        candidates.append(game_dir / "bin" / "gmad.exe")
    found = shutil.which("gmad.exe") or shutil.which("gmad")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates if str(candidate))
    raise FileNotFoundError(f"gmad.exe was not found. Searched: {searched or 'PATH'}")


def addon_tree_has_long_paths(addon_dir: Path, limit: int = 250) -> bool:
    """True if any file under addon_dir has a full path at/over `limit` chars.

    vpk.exe and gmad.exe are not long-path aware: past Windows MAX_PATH (260) they
    silently fail (exit 1, no output, no archive). Such trees must be packaged through
    a short staging directory. `limit` is below 260 to leave margin for the staged
    name. Mirrors the non-ASCII staging trigger.
    """
    try:
        for item in addon_dir.rglob("*"):
            if item.is_file() and len(str(item)) >= limit:
                return True
    except OSError:
        return True
    return False


def package_addon_gma(addon_dir: Path, output_gma: Path, gmod: dict[str, Any], log_path: Path) -> Path:
    gmad = find_gmad_executable(gmod)
    output_gma = output_gma.with_suffix(".gma")
    output_gma.parent.mkdir(parents=True, exist_ok=True)
    if output_gma.exists():
        output_gma.unlink()
    actual_addon_dir = addon_dir
    actual_output_gma = output_gma
    staged_output: Path | None = None
    scratch: Path | None = None
    try:
        if not path_is_ascii(addon_dir) or not path_is_ascii(output_gma) or addon_tree_has_long_paths(addon_dir):
            scratch = external_safe_dir_for(output_gma, "gmad")
            if scratch.exists():
                shutil.rmtree(scratch)
            scratch.mkdir(parents=True, exist_ok=True)
            actual_addon_dir = scratch / safe_name(addon_dir.name, "addon")
            copytree_clean(addon_dir, actual_addon_dir)
            actual_output_gma = scratch / f"{safe_name(output_gma.stem, 'addon')}.gma"
            staged_output = actual_output_gma
            emit(f"Staging GMA packaging through ASCII path: {actual_addon_dir}")
        code, output = run_command(
            [str(gmad), "create", "-folder", str(actual_addon_dir), "-out", str(actual_output_gma)],
            log_path,
            cwd=actual_addon_dir.parent,
        )
        if code != 0:
            raise RuntimeError(f"gmad failed with exit code {code}; see {log_path}")
        if staged_output is not None:
            if not staged_output.exists():
                raise RuntimeError(f"gmad completed but did not write staged output {staged_output}. Output:\n{output}")
            shutil.copyfile(staged_output, output_gma)
        if not output_gma.exists():
            raise RuntimeError(f"gmad completed but did not write {output_gma}. Output:\n{output}")
        return output_gma
    finally:
        if scratch is not None and scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# L4D2 packaging helpers (VPK + addoninfo). The character/arms compiled outputs
# live at the exact survivor paths (models/survivors, models/weapons/arms), not
# the author namespace, so L4D2 uses dedicated clean/copy helpers.
# ---------------------------------------------------------------------------

def l4d2_compiled_relpaths(slot: str, has_arms: bool) -> list[str]:
    """Compiled output relative paths (under models/) for the L4D2 survivor."""
    slot = normalize_survivor(slot)
    rels = [f"survivors/survivor_{slot}"]
    if has_arms:
        rels.append(l4d2_arms_relpath(slot))
    return rels


def clean_expected_compiled_l4d2(game_dir: Path, slot: str, has_arms: bool) -> None:
    for rel in l4d2_compiled_relpaths(slot, has_arms):
        for ext in COMPILED_EXTENSIONS:
            path = game_dir / "models" / f"{rel}{ext}"
            if path.exists():
                path.unlink()


def copy_compiled_outputs_l4d2(game_dir: Path, addon_dir: Path, slot: str, has_arms: bool) -> tuple[list[dict[str, Any]], list[str]]:
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    for rel in l4d2_compiled_relpaths(slot, has_arms):
        src_base = game_dir / "models" / rel
        dst_base = addon_dir / "models" / rel
        dst_base.parent.mkdir(parents=True, exist_ok=True)
        found = False
        for ext in COMPILED_EXTENSIONS:
            src = src_base.with_name(src_base.name + ext)
            if src.exists():
                dst = dst_base.with_name(dst_base.name + ext)
                shutil.copyfile(src, dst)
                files.append(file_row(dst, "compiled_model"))
                found = True
        if not found:
            errors.append(f"Compiled L4D2 model files not found for models/{rel} in {game_dir}")
    return files, errors


def patch_mdl_internal_name(mdl_path: Path, new_name: str) -> None:
    """Rewrite the studiohdr_t name[64] field (offset 12) of a compiled .mdl.

    The engine loads models by file path, but several systems (model cache,
    $includemodel resolution) read the internal name, so a renamed copy should
    carry its own path. The header checksum is untouched: it ties the .mdl to
    its .vvd/.vtx companions, which are copied unmodified."""
    encoded = new_name.encode("ascii")
    if len(encoded) > 63:
        raise ValueError(f"Internal model name is longer than 63 bytes: {new_name}")
    with open(mdl_path, "r+b") as handle:
        magic = handle.read(4)
        if magic != b"IDST":
            raise ValueError(f"Not a studiomdl file (magic {magic!r}): {mdl_path}")
        handle.seek(12)
        handle.write(encoded.ljust(64, b"\x00"))


def write_l4d2_light_model_copies(addon_dir: Path, slot: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Duplicate the compiled L4D1 survivor as survivor_<slot>_light (issue #135).

    The Passing / The Sacrifice render the L4D1 survivors as NPCs through the
    dedicated survivors/survivor_<slot>_light.mdl model, so without this copy a
    replacement only applies in playable contexts. The full-detail model is
    shipped under the _light name (replacement addons do not need the stock
    lower-LOD geometry) with its internal name patched to match."""
    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    slot = normalize_survivor(slot)
    if slot not in L4D1_SURVIVOR_SLOTS:
        return files, warnings
    base = addon_dir / "models" / "survivors" / f"survivor_{slot}"
    light = addon_dir / "models" / "survivors" / f"survivor_{slot}_light"
    copied = 0
    for ext in COMPILED_EXTENSIONS:
        src = base.with_name(base.name + ext)
        if not src.exists():
            continue
        dst = light.with_name(light.name + ext)
        try:
            shutil.copyfile(src, dst)
            if ext == ".mdl":
                patch_mdl_internal_name(dst, f"survivors/survivor_{slot}_light.mdl")
            files.append(file_row(dst, "compiled_model_light"))
            copied += 1
        except Exception as exc:
            warnings.append(f"Could not create The Passing _light variant {dst.name}: {exc}")
    if copied:
        emit(f"Added survivors/survivor_{slot}_light model copies for The Passing/The Sacrifice NPC cameo ({copied} file(s)).")
    else:
        warnings.append(
            f"No compiled files were found to create survivors/survivor_{slot}_light; "
            "The Passing/The Sacrifice NPC cameo will keep the stock model."
        )
    return files, warnings


def write_l4d2_addonimage(plan: dict[str, Any], addon_dir: Path) -> Path | None:
    """Generate the L4D2 add-on thumbnail (addonimage.jpg) from the Step 13 release icon.

    L4D2 shows ``addonimage.jpg`` (a 512x512 JPG, matching the workshop convention) as the add-on's
    thumbnail in the Add-ons menu; without it the menu falls back to the generic red '4' placeholder.
    Reuses the Step 13 icon output the user pointed at. Returns None (with the caller warning) if no
    source icon or PIL is available -- the addon still works, just without a custom thumbnail."""
    step13 = Path(str(plan.get("inputs", {}).get("step13_dir") or ""))
    source = next(
        (step13 / name for name in ("release_icon.png", "SPIC.png", "E.png", "E.jpg") if (step13 / name).exists()),
        None,
    )
    if source is None:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    target = addon_dir / "addonimage.jpg"
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            if image.size != (512, 512):
                image = image.resize((512, 512), Image.LANCZOS)
            image.save(target, "JPEG", quality=90)
    except Exception:
        return None
    return target


def write_l4d2_addoninfo(plan: dict[str, Any], addon_dir: Path) -> Path:
    """Write the L4D2 addoninfo.txt (the VPK addon manifest)."""
    slot = normalize_survivor(plan.get("survivor"))
    survivor_display = {"producer": "Rochelle", "coach": "Coach", "gambler": "Nick", "mechanic": "Ellis"}.get(slot, slot)
    title = str(plan.get("display_name") or plan.get("model_name") or "MMD Survivor")
    author = str(plan.get("author") or "MMD Character Importer")
    path = addon_dir / "addoninfo.txt"
    lines = [
        '"AddonInfo"',
        "{",
        "addonSteamAppID         550",
        f'addontitle "{title}"',
        "addonversion 1.0",
        f'addontagline "survivor,{survivor_display}"',
        f'addonauthor "{author}"',
        f'addonDescription "Replaces {survivor_display}"',
        "addonContent_Survivor 1",
        "}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def find_vpk_executable(gmod: dict[str, Any]) -> Path:
    candidates: list[Path] = []
    install_root = Path(str(gmod.get("install_root") or ""))
    game_dir = Path(str(gmod.get("game_dir") or ""))
    if install_root:
        candidates.append(install_root / "bin" / "vpk.exe")
    if game_dir:
        candidates.append(game_dir.parent / "bin" / "vpk.exe")
        candidates.append(game_dir / "bin" / "vpk.exe")
    found = shutil.which("vpk.exe") or shutil.which("vpk")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(c) for c in candidates if str(c))
    raise FileNotFoundError(f"vpk.exe was not found. Searched: {searched or 'PATH'}")


def package_addon_vpk(addon_dir: Path, output_vpk: Path, gmod: dict[str, Any], log_path: Path) -> Path:
    """Pack the composed addon folder into a single .vpk using L4D2's vpk.exe.

    `vpk.exe <folder>` produces `<folder>.vpk` next to the folder; we then move it
    to the requested output path.

    vpk.exe converts its path argument to the system ANSI codepage internally and fails
    (prints its usage banner and exits 1) when the path contains characters that don't
    round-trip in that codepage — e.g. a workspace named with an em dash / fullwidth
    parentheses. So when the addon folder or the output path is non-ASCII, stage the
    packaging through an ASCII scratch directory, exactly like package_addon_gma.
    """
    vpk = find_vpk_executable(gmod)
    output_vpk = output_vpk.with_suffix(".vpk")
    output_vpk.parent.mkdir(parents=True, exist_ok=True)
    if output_vpk.exists():
        output_vpk.unlink()
    actual_addon_dir = addon_dir
    scratch: Path | None = None
    try:
        if not path_is_ascii(addon_dir) or not path_is_ascii(output_vpk) or addon_tree_has_long_paths(addon_dir):
            scratch = external_safe_dir_for(output_vpk, "vpk")
            if scratch.exists():
                shutil.rmtree(scratch)
            scratch.mkdir(parents=True, exist_ok=True)
            actual_addon_dir = scratch / safe_name(addon_dir.name, "addon")
            copytree_clean(addon_dir, actual_addon_dir)
            emit(f"Staging VPK packaging through ASCII path: {actual_addon_dir}")
        returncode, output = run_command([str(vpk), str(actual_addon_dir)], log_path, cwd=actual_addon_dir.parent)
        produced = actual_addon_dir.with_name(actual_addon_dir.name + ".vpk")
        if returncode != 0 or not produced.exists():
            raise RuntimeError(f"vpk.exe failed (exit {returncode}) or did not write {produced}. Output:\n{output}")
        shutil.move(str(produced), str(output_vpk))
        return output_vpk
    finally:
        if scratch is not None and scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)


def matrix_to_xyz_radians(m: list[list[float]]) -> tuple[float, float, float]:
    """Inverse of rotation_matrix_xyz (which builds Rz·Ry·Rx): recover (rx, ry, rz) radians.
    Used to re-express a bone's accumulated GLOBAL rotation as a root local rotation."""
    cy = math.sqrt(m[0][0] * m[0][0] + m[1][0] * m[1][0])
    if cy > 1.0e-8:
        rx = math.atan2(m[2][1], m[2][2])
        ry = math.atan2(-m[2][0], cy)
        rz = math.atan2(m[1][0], m[0][0])
    else:  # gimbal lock (cos(ry) ~ 0)
        rx = math.atan2(-m[1][2], m[1][1])
        ry = math.atan2(-m[2][0], cy)
        rz = 0.0
    return (rx, ry, rz)


def compose_global_rotation(name: str, poses: dict[str, SmdBonePose]) -> list[list[float]]:
    """Accumulate root->name local rotations into name's global rotation matrix."""
    chain: list[str] = []
    current = name
    guard: set[str] = set()
    while current and current in poses and current not in guard:
        guard.add(current)
        chain.append(current)
        current = poses[current].parent
    chain.reverse()  # root first
    rot = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for bone_name in chain:
        rot = matmul(rot, rotation_matrix_xyz(*poses[bone_name].local_rot))
    return rot


# The GMod c_arms root: the arms hang off Spine4 (matching the bones a weapon viewmodel
# carries and what models/weapons/c_arms_animations.mdl is authored against).
CARMS_ROOT_BONE = "ValveBiped.Bip01_Spine4"


def carms_minimal_definebones(carms_work: Path) -> list[str]:
    """GMod c_arms $definebone block: a MINIMAL arms-only skeleton -- Spine4 root plus only the
    bones the cut mesh is weighted to, closed over parents up to Spine4 -- with the character's
    FINAL proportions BAKED into the bone rest positions. This mirrors a hand-made GMod c_arms
    (issue #121): no full-body skeleton and no runtime proportion-trick delta, so nothing
    re-drives the bones on top of the weapon-viewmodel bonemerge (which displaced the arms on
    non-Valve-standard custom weapons).

    Positions come from the proportioned SMD node blocks already present in carms_work: the
    Step-10 cut meshes and the copied anims/proportions.smd both carry the FULL proportioned
    skeleton (Blender Source Tools writes every node), even though the pruned mesh itself only
    weights to forearm-and-below. No mesh re-cut and no new definebone capture are required."""
    poses = collect_smd_bone_poses(carms_work)
    if not poses:
        return []
    # Bones the pruned arm mesh is actually skinned to (forearm/hand/fingers + any arm-deform
    # helper the mesh uses). UpperArm/Clavicle/Spine4 are pulled in by the parent closure below.
    used: set[str] = set()
    for smd_path in sorted(carms_work.glob("*.smd"), key=lambda item: natural_key(item.name)):
        try:
            smd = parse_smd(smd_path, include_triangles=True)
        except Exception as exc:
            emit(f"c_arms minimal skeleton: skipped unreadable SMD {smd_path.name}: {exc}")
            continue
        id_to_name = {bone_id: node.name for bone_id, node in smd.nodes.items()}
        for triangle in smd.triangles:
            for vertex in triangle["vertices"]:
                for bone_id, weight in vertex["weights"]:
                    if weight > 0.0 and bone_id in id_to_name:
                        used.add(id_to_name[bone_id])
    # Close over parents up to (and including) the Spine4 root.
    keep: set[str] = set()
    for name in used:
        current = name
        guard: set[str] = set()
        while current and current in poses and current not in guard:
            guard.add(current)
            keep.add(current)
            if current == CARMS_ROOT_BONE:
                break
            current = poses[current].parent
    if CARMS_ROOT_BONE in poses:
        keep.add(CARMS_ROOT_BONE)
    subset = {name: poses[name] for name in keep if name in poses}
    if not subset:
        return []
    # Re-root Spine4: zero its position and bake its GLOBAL rotation, so the arm chain keeps the
    # correct orientation once the spine above it is dropped (as the hand-made c_arms does).
    if CARMS_ROOT_BONE in subset:
        root_global = compose_global_rotation(CARMS_ROOT_BONE, poses)
        subset[CARMS_ROOT_BONE] = SmdBonePose(
            name=CARMS_ROOT_BONE,
            parent="",
            local_pos=(0.0, 0.0, 0.0),
            local_rot=matrix_to_xyz_radians(root_global),
            source_smd=subset[CARMS_ROOT_BONE].source_smd,
        )
    # Any other kept bone whose parent fell outside the minimal set becomes a root too (rare:
    # an arm-deform helper whose ancestor chain does not pass through Spine4).
    for name, pose in list(subset.items()):
        if name != CARMS_ROOT_BONE and pose.parent and pose.parent not in subset:
            subset[name] = SmdBonePose(
                name=name, parent="", local_pos=pose.local_pos, local_rot=pose.local_rot, source_smd=pose.source_smd
            )
    ordered = order_smd_definebone_names(subset)
    return [make_definebone_from_smd_pose(subset[name]) for name in ordered]


def build_carms_qc(plan: dict[str, Any], source_dir: Path, definebones: list[str]) -> Path | None:
    carms_dir = Path(str(plan.get("inputs", {}).get("step10_dir") or ""))
    if not carms_dir.exists():
        return None
    smds = [path for path in sorted(carms_dir.glob("*.smd"), key=lambda item: natural_key(item.name)) if path.name.lower() != "c_arms_citizen.smd"]
    if not smds:
        return None
    carms_work = source_dir / "c_arms"
    carms_work.mkdir(parents=True, exist_ok=True)
    for smd in smds:
        shutil.copyfile(smd, carms_work / smd.name)
    copytree_clean(carms_dir / "anims", carms_work / "anims")
    # Issue #139: the L4D2 first-person arms use the same proportion-subtract structure as the
    # survivor model, so their delta gets the same position-only treatment (GMod is untouched:
    # its delta is already rotation-free, and its c_arms subtract base is the issue-#121
    # stock-arms reference built below).
    if normalize_game(plan.get("game")) == "l4d2" and not bool(plan.get("l4d2_keep_proportion_rotation_delta")):
        neutralize_proportion_rotation_delta(
            carms_work / "anims",
            gendered_reference_name(plan, carms_work / "anims"),
            label="first-person arms",
        )
    lines = qc_model_header(plan, arms=True)
    for smd in smds:
        lines.extend(bodygroup_block(smd.stem, smd.name))
    lines.append('$surfaceprop "flesh" \n\n')
    lines.append('$contents "solid" \n\n')
    lines.append("$illumposition -0.637 0 35.954 \n\n")
    lines.append("$ambientboost \n\n")
    lines.append("$mostlyopaque \n\n")
    lines.append(f'$cdmaterials "models/{plan["author"]}/{plan["model_name"]}/" \n\n')
    lines.append('$cbox 0 0 0 0 0 0 \n\n')
    lines.append('$bbox -13 -13 0 13 13 72 \n\n')
    game = normalize_game(plan.get("game"))
    # "Use experimental arms" (GMod only, default OFF): when ON, Step 10 conformed the arm mesh onto
    # the standard c_arms skeleton, so we emit the minimal/conform c_arms below. When OFF (default,
    # and always for L4D2), the c_arms keeps the full-body skeleton + proportion trick (the original
    # method). Auto-porting never sets this key, so it gets the proportion-driven arms.
    experimental_arms = bool(plan.get("gmod_experimental_arms", False))
    if game == "l4d2" or (game == "gmod" and not experimental_arms):
        # Proportion-driven first-person arms: the full-body skeleton + proportion trick + ragdoll +
        # foot IK + a static idle. This is L4D2's v_arms (kept byte-identical so its sensitive
        # sequence layout is not disturbed) AND the default/original GMod c_arms. The arms bonemerge
        # onto the weapon viewmodel at runtime.
        lines.extend(definebones)
        lines.append('$ikchain "rhand" "ValveBiped.Bip01_R_Hand" knee 0.707 0.707 0 \n')
        lines.append('$ikchain "lhand" "ValveBiped.Bip01_L_Hand" knee 0.707 0.707 0 \n')
        lines.append('$ikchain "rfoot" "ValveBiped.Bip01_R_Foot" knee 0.707 -0.707 0 \n')
        lines.append('$ikchain "lfoot" "ValveBiped.Bip01_L_Foot" knee 0.707 -0.707 0 \n\n')
        lines.append('$ikautoplaylock "rfoot" 0.7 0.1 \n')
        lines.append('$ikautoplaylock "lfoot" 0.7 0.1 \n\n')
        # GMod: subtract against the STOCK c_arms arm proportions (issue #121 -- the c_arms bonemerge
        # uses the stock/male arm lengths, so the female player reference displaces the arms). L4D2
        # keeps its survivor-specific gendered reference unchanged.
        carms_reference = None
        if game == "gmod":
            carms_reference = build_carms_proportion_reference(carms_work)
        if not carms_reference:
            carms_reference = gendered_reference_name(plan, carms_work / "anims")
        lines.append(f'$sequence reference "anims/{carms_reference}" fps 1 \n')
        lines.append('$origin 0 0 -2.40 \n\n')
        lines.append('$animation a_proportions "anims/proportions" subtract reference 0 \n\n')
        lines.append('$sequence proportions a_proportions predelta autoplay \n\n')
        lines.append('$Sequence "ragdoll" {\n\t"anims/proportions"\n\tactivity "ACT_DIERAGDOLL" 1\n\tfadein 0.2\n\tfadeout 0.2\n\tfps 60\n}\n\n')
        lines.append('$sequence "idle" {\n\t"anims/proportions"\n\tfadein 0.2\n\tfadeout 0.2\n\tfps 30\n}\n')
    else:
        # GMod EXPERIMENTAL c_arms (issue #121, "use experimental arms" ON): the arm MESH is
        # rest-pose-conformed onto the STANDARD c_arms skeleton in Step 10
        # (blender_sort_carms.py conform_meshes_to_standard), so the cut
        # SMDs now carry standard ValveBiped arm proportions. carms_minimal_definebones therefore
        # reads STANDARD bone positions straight from the conformed SMD -- no proportion trick and no
        # MMD-proportion bake -- which is what bonemerges cleanly onto weapon viewmodels. Like the
        # stock weapons/c_arms.mdl, no local $sequence is emitted; the included animation model
        # supplies the first-person hand/finger poses (a local idle from the MMD-proportion
        # anims/proportions would fight the now-standard skeleton).
        minimal_definebones = carms_minimal_definebones(carms_work)
        if not minimal_definebones or not any('_Hand"' in line for line in minimal_definebones):
            # Safety net: if the cut SMDs could not be read / under-collected, fall back to the
            # captured definebones so the c_arms still compiles.
            emit("c_arms minimal skeleton under-collected; falling back to captured definebones.")
            minimal_definebones = definebones
        lines.extend(minimal_definebones)
        # Forearm-twist Ulna procedural bones (matches the good human port) so the lower forearm
        # twists with the hand. Emit ONLY when the Ulna bones are actually defined (Step 10 weighted
        # them), so the VRD never references an undeclared bone.
        if any('_Ulna"' in line for line in minimal_definebones):
            vrd_src = Path(__file__).resolve().parent / "assets" / "std_c_arms_skeleton" / "c_arms_ulna.vrd"
            if vrd_src.exists():
                shutil.copyfile(vrd_src, carms_work / "c_arms_ulna.vrd")
                lines.append('$proceduralbones "c_arms_ulna.vrd" \n')
        lines.append('$includemodel "weapons/c_arms_animations.mdl" \n')
    qc = carms_work / "pm_carms.qc"
    write_lines(qc, lines)
    return qc


def compose(plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    errors = validate_plan(plan)
    warnings = list(plan.get("warnings", [])) if isinstance(plan.get("warnings"), list) else []
    if is_auto_port_plan(plan):
        warnings = without_optional_canonical_smd_warnings(warnings)
    else:
        add_optional_canonical_smd_warnings(warnings, Path(str(plan.get("step9_dir") or "")))
    refresh_input_availability_warnings(plan, warnings)
    ensure_external_safe_qc_source(plan, warnings)
    plan["warnings"] = warnings
    write_json(plan_path, plan)
    if errors:
        validation = {"ok": False, "errors": errors, "warnings": warnings}
        report = {
            "version": 1,
            "kind": "sort_qc_compile_report",
            "status": "failed",
            "validation": validation,
            "validation_errors": errors,
            "warnings": warnings,
        }
        write_json(Path(str(plan.get("qc_dir") or plan_path.parent)) / "qc_report.json", report)
        raise RuntimeError("QC plan validation failed: " + "; ".join(errors))
    qc_dir = Path(str(plan["qc_dir"]))
    emit("Preparing QC source folder.")
    source_dir = prepare_qc_source(plan)
    addon_dir = Path(str(plan["addon_dir"]))
    if addon_dir.exists():
        shutil.rmtree(addon_dir)
    addon_dir.mkdir(parents=True, exist_ok=True)
    emit("Prepared QC source folder.")

    gmod = dict(plan["gmod"]) if isinstance(plan.get("gmod"), dict) else {}
    # Re-resolve the game tooling (studiomdl + game dir) for the plan's TARGET game so the compile
    # -game, gmad.exe search, and addons install are ALWAYS consistent with the selected game
    # (manual = Step 1, auto-port = main interface -> plan["game"]). plan["gmod"] can go stale: on a
    # manual compile update_qc_plan_from_gui() refreshes studiomdl_path but NOT game_dir/install_root,
    # so switching target game after Analyze left a GMod studiomdl paired with a leftover L4D2 game_dir
    # (gmad searched in Left 4 Dead 2\bin, addon installed to the L4D2 addons folder). detect_game
    # prioritises the explicit studiomdl, so the user's set studiomdl is preserved and the matching
    # game dir is derived from it.
    resolved_game = detect_game(
        normalize_game(plan.get("game")),
        str(gmod.get("install_root") or ""),
        str(gmod.get("studiomdl_path") or ""),
    )
    if resolved_game.get("studiomdl_path") and resolved_game.get("game_dir"):
        if str(resolved_game.get("game_dir")) != str(gmod.get("game_dir") or ""):
            emit(
                f"Re-resolved {normalize_game(plan.get('game'))} tooling for compile: "
                f"game dir {resolved_game.get('game_dir')} (was {gmod.get('game_dir') or 'unset'})."
            )
        gmod.update({k: resolved_game[k] for k in ("install_root", "game_dir", "studiomdl_path") if resolved_game.get(k)})
        plan["gmod"] = gmod
        write_json(plan_path, plan)
    studiomdl = Path(str(gmod["studiomdl_path"]))
    game_dir = Path(str(gmod["game_dir"]))
    compile_studiomdl = studiomdl
    compile_game_dir = game_dir
    author = str(plan["author"])
    category = str(plan["character_category"])
    model = str(plan["model_name"])
    l4d2 = normalize_game(plan.get("game")) == "l4d2"
    sfm = normalize_game(plan.get("game")) == "sfm"
    single_model = l4d2 or sfm  # no GMod-style "_pm" playermodel variant
    survivor_slot = normalize_survivor(plan.get("survivor"))

    # Issue #139 (L4D2 arms drift/cross): strip the baked rotations out of the proportion
    # delta so it only carries the MMD bone positions/lengths, matching the shape of the
    # working GMod delta. Plan key l4d2_keep_proportion_rotation_delta restores the old
    # (broken) behaviour if ever needed.
    proportion_delta_report: dict[str, Any] | None = None
    if l4d2 and not bool(plan.get("l4d2_keep_proportion_rotation_delta")):
        proportion_delta_report = neutralize_proportion_rotation_delta(
            source_dir / "anims",
            gendered_reference_name(plan, source_dir / "anims", warnings),
            label="survivor model",
        )

    initial_qc = source_dir / "compile_initial.qc"
    write_lines(initial_qc, base_qc_lines(plan, source_dir, pm=False, include_flexes=False, for_definebone_capture=True))
    define_log = qc_dir / "compile_definebones.log"
    emit("Running studiomdl -definebones.")
    code, define_output = run_command([str(studiomdl), "-game", str(game_dir), "-definebones", "-nop4", "-verbose", str(initial_qc)], define_log, cwd=source_dir)
    definebone_repair_reports: list[dict[str, Any]] = []
    if code != 0:
        reason = f"StudioMDL -definebones failed with exit code {code}; see {define_log}"
        emit("WARNING: " + reason)
        definebones, fallback_report = synthesize_definebones_from_smd(source_dir, reason, define_output)
        definebone_repair_reports.append(fallback_report)
        if not definebones:
            excerpt = compact_command_output_excerpt(define_output)
            detail = f"{reason}; SMD skeleton fallback did not find any bones."
            if excerpt:
                detail += "\n\nStudioMDL output excerpt:\n" + excerpt
            raise RuntimeError(detail)
        warnings.append(
            "StudioMDL -definebones failed, so Step 14 generated definebones from the exported SMD skeleton instead. "
            f"See {define_log}"
        )
        emit(f"Generated {len(definebones)} definebone lines from exported SMD skeleton.")
    else:
        definebones = extract_definebones(define_output)
        if not definebones:
            reason = f"StudioMDL -definebones completed but produced no $definebone lines; see {define_log}"
            emit("WARNING: " + reason)
            definebones, fallback_report = synthesize_definebones_from_smd(source_dir, reason, define_output)
            definebone_repair_reports.append(fallback_report)
            if not definebones:
                excerpt = compact_command_output_excerpt(define_output)
                detail = f"{reason}; SMD skeleton fallback did not find any bones."
                if excerpt:
                    detail += "\n\nStudioMDL output excerpt:\n" + excerpt
                raise RuntimeError(detail)
            warnings.append(
                "StudioMDL -definebones produced no definebone lines, so Step 14 generated definebones from the exported SMD skeleton instead. "
                f"See {define_log}"
            )
            emit(f"Generated {len(definebones)} definebone lines from exported SMD skeleton.")
    emit(f"Captured {len(definebones)} definebone lines.")
    definebones, repair_report = repair_definebones(definebones, source_dir, define_output, "initial_definebones")
    definebone_repair_reports.append(repair_report)
    definebone_support_bones = {str(name) for name in repair_report.get("support_bones_excluded_from_jiggle", []) if str(name)}
    inserted = repair_report.get("inserted_bones", [])
    if inserted:
        emit("Inserted missing parent definebones: " + ", ".join(str(name) for name in inserted))
    write_definebone_repair_report(qc_dir, definebone_repair_reports)

    # Issue #168: L4D1-slot survivors (teenangst/biker/manager) lack the TLS L_weapon_bone,
    # leaving two-handed melees with no left-hand anchor (weapon drops to the model origin).
    # Declare any missing TLS weapon bone via $definebone; the attachment gate in
    # base_qc_lines emits the matching $attachment once the bone is in the definebone set.
    tls_injected_bones: list[str] = []
    if l4d2:
        definebones, tls_injected_bones = inject_l4d2_tls_weapon_bones(definebones)
        if tls_injected_bones:
            emit(
                "Added missing TLS weapon bone definebone(s) for two-handed melee support: "
                + ", ".join(tls_injected_bones)
            )

    jiggle_lines: list[str] = []
    jiggles: list[str] = []
    ignores: list[str] = []

    def refresh_jiggle_outputs() -> None:
        nonlocal jiggle_lines, jiggles, ignores
        jiggle_lines, jiggles, ignores = jiggle_qc_blocks(plan, excluded_bones=definebone_support_bones)
        (qc_dir / "bone_list.txt").write_text("\n".join(jiggles) + ("\n" if jiggles else ""), encoding="utf-8")
        (qc_dir / "bone_list_ignore.txt").write_text("\n".join(ignores) + ("\n" if ignores else ""), encoding="utf-8")
        invert_label = "inverted yaw/pitch" if bool(plan.get("invert_jiggle_direction", False)) else "normal yaw/pitch"
        emit(
            f"Wrote bone_list.txt ({len(jiggles)} bones) and bone_list_ignore.txt ({len(ignores)} bones); "
            f"excluded {len(definebone_support_bones)} repaired support bone(s); {invert_label}."
        )

    refresh_jiggle_outputs()

    hbox_probe_qc = source_dir / "compile_hbox_probe.qc"
    write_lines(
        hbox_probe_qc,
        base_qc_lines(
            plan,
            source_dir,
            pm=False,
            include_definebones=definebones,
            include_jiggles=jiggle_lines,
            include_flexes=False,
        ),
    )
    hbox_log = qc_dir / "compile_hbox.log"
    emit("Running studiomdl -h for hitbox capture.")
    hbox_code, hbox_output = run_command([str(studiomdl), "-game", str(game_dir), "-nop4", "-verbose", "-h", str(hbox_probe_qc)], hbox_log, cwd=source_dir)
    hboxes = extract_hboxes(hbox_output)
    emit(f"Captured {len(hboxes)} hitbox lines.")
    if hbox_code != 0 or not hboxes:
        hbox_warning = (
            f"Hitbox probe {'failed with exit code ' + str(hbox_code) if hbox_code != 0 else 'completed'}"
            " but produced no usable $hbox lines; the compiled model will use auto-generated hitboxes. "
            f"See {hbox_log}"
        ) if not hboxes else (
            f"Hitbox probe exited with code {hbox_code}; captured $hbox lines may be incomplete. See {hbox_log}"
        )
        warnings.append(hbox_warning)
        emit("WARNING: " + hbox_warning)

    main_qc = source_dir / "compile.qc"
    pm_qc = source_dir / "compile_pm.qc"
    flex_compile_enabled = True
    flex_compile_disabled_reason = ""

    def refresh_compile_qcs() -> Path | None:
        write_lines(
            main_qc,
            base_qc_lines(
                plan,
                source_dir,
                pm=False,
                include_definebones=definebones,
                include_jiggles=jiggle_lines,
                include_hboxes=hboxes,
                include_collision=True,
                include_flexes=flex_compile_enabled,
            ),
        )
        write_lines(
            pm_qc,
            base_qc_lines(
                plan,
                source_dir,
                pm=True,
                include_definebones=definebones,
                include_jiggles=jiggle_lines,
                include_hboxes=hboxes,
                include_collision=True,
                include_flexes=flex_compile_enabled,
            ),
        )
        return build_carms_qc(plan, source_dir, definebones)

    carms_qc = refresh_compile_qcs()

    def compile_with_definebone_repair(label: str, qc_path: Path, log_path: Path) -> tuple[str, bool]:
        nonlocal definebones, carms_qc, definebone_support_bones
        output = compile_one(compile_studiomdl, compile_game_dir, qc_path, log_path)
        missing, warning_lines = parse_missing_parent_warnings(output)
        if not missing:
            return output, False
        emit(f"{qc_path.name} emitted {len(warning_lines)} missing parent definebone warning(s); repairing and retrying once.")
        repaired_definebones, retry_report = repair_definebones(definebones, source_dir, output, f"{label}_compile_retry")
        definebone_repair_reports.append(retry_report)
        write_definebone_repair_report(qc_dir, definebone_repair_reports)
        inserted_retry = retry_report.get("inserted_bones", [])
        if not inserted_retry:
            missing_text = ", ".join(sorted(missing, key=natural_key))
            unrecoverable = {str(name) for name in retry_report.get("unrecoverable_missing_parents", []) if str(name)}
            if unrecoverable and set(missing) <= unrecoverable:
                # StudioMDL compiled successfully (compile_one raised otherwise) and the
                # missing parents exist in no source SMD, so demote to a warning instead
                # of failing a usable compile.
                warning = (
                    f"{qc_path.name} compiled with missing parent definebone warning(s) that cannot be repaired "
                    f"(parent bones exist in no source SMD): {missing_text}. See {log_path}"
                )
                warnings.append(warning)
                emit("WARNING: " + warning)
                return output, False
            raise RuntimeError(f"Could not repair missing parent definebones for {qc_path.name}: {missing_text}")
        emit("Inserted retry definebones: " + ", ".join(str(name) for name in inserted_retry))
        definebones = repaired_definebones
        definebone_support_bones.update(str(name) for name in retry_report.get("support_bones_excluded_from_jiggle", []) if str(name))
        refresh_jiggle_outputs()
        carms_qc = refresh_compile_qcs()
        retry_log = log_path.with_name(f"{log_path.stem}_retry{log_path.suffix}")
        retry_output = compile_one(compile_studiomdl, compile_game_dir, qc_path, retry_log)
        retry_missing, retry_warnings = parse_missing_parent_warnings(retry_output)
        if retry_missing:
            missing_text = ", ".join(sorted(retry_missing, key=natural_key))
            unrecoverable = {
                str(name)
                for repair in definebone_repair_reports
                for name in repair.get("unrecoverable_missing_parents", [])
                if str(name)
            }
            if unrecoverable and set(retry_missing) <= unrecoverable:
                warning = (
                    f"{qc_path.name} compiled with missing parent definebone warning(s) that cannot be repaired "
                    f"(parent bones exist in no source SMD): {missing_text}. See {retry_log}"
                )
                warnings.append(warning)
                emit("WARNING: " + warning)
                return retry_output, True
            raise RuntimeError(
                f"StudioMDL still reports missing parent definebones after retry for {qc_path.name}: {missing_text}. "
                f"See {retry_log}"
            )
        if retry_warnings:
            raise RuntimeError(f"StudioMDL missing parent definebone retry did not clear warnings for {qc_path.name}. See {retry_log}")
        return retry_output, True

    # L4D2 + SFM compile a single model (no GMod-style "_pm" playermodel).
    stems = [model] if single_model else [model, f"{model}_pm"]
    if carms_qc:
        stems.append(f"{model}_arms")

    def compile_log_path(stem: str, suffix: str = "") -> Path:
        suffix_text = f"_{suffix}" if suffix else ""
        return qc_dir / f"{stem}{suffix_text}.log"

    # StudioMDL output of the successful compile pass, used to learn which materials the model
    # actually references ('Processing LOD for material: X') so compose_materials can drop unused ones.
    compile_material_outputs: list[str] = []

    def compile_all_models(log_suffix: str = "") -> None:
        compile_material_outputs.clear()
        emit("Compiling main model.")
        _main_output, main_repaired = compile_with_definebone_repair(
            f"main{('_' + log_suffix) if log_suffix else ''}",
            main_qc,
            compile_log_path("compile_main", log_suffix),
        )
        compile_material_outputs.append(_main_output)
        pm_repaired = False
        if not single_model:
            emit("Compiling player model.")
            _pm_output, pm_repaired = compile_with_definebone_repair(
                f"player{('_' + log_suffix) if log_suffix else ''}",
                pm_qc,
                compile_log_path("compile_pm", log_suffix),
            )
            compile_material_outputs.append(_pm_output)
        if pm_repaired:
            emit("Recompiling main model after player-model definebone repair.")
            _main_output, _main_repaired_after_pm = compile_with_definebone_repair(
                f"main_after_player_repair{('_' + log_suffix) if log_suffix else ''}",
                main_qc,
                compile_log_path("compile_main_after_definebone_repair", log_suffix),
            )
            compile_material_outputs.append(_main_output)
        if carms_qc:
            emit("Compiling c_arms model.")
            _carms_output, carms_repaired = compile_with_definebone_repair(
                f"carms{('_' + log_suffix) if log_suffix else ''}",
                carms_qc,
                compile_log_path("compile_carms", log_suffix),
            )
            compile_material_outputs.append(_carms_output)
            if carms_repaired:
                emit("Recompiling main model after c_arms definebone repair.")
                _main_output, _ = compile_with_definebone_repair(
                    f"main_after_carms_repair{('_' + log_suffix) if log_suffix else ''}",
                    main_qc,
                    compile_log_path("compile_main_after_carms_definebone_repair", log_suffix),
                )
                compile_material_outputs.append(_main_output)
                if not single_model:
                    _pm_output, _ = compile_with_definebone_repair(
                        f"player_after_carms_repair{('_' + log_suffix) if log_suffix else ''}",
                        pm_qc,
                        compile_log_path("compile_pm_after_carms_definebone_repair", log_suffix),
                    )
                    compile_material_outputs.append(_pm_output)

    if not carms_qc and not sfm:
        # SFM does not use c_arms (Step 10 is skipped), so its absence is expected -- no warning.
        warnings.append("Step 10 c_arms output was not found; c_arms QC and Lua hands registration were skipped.")
    fallback_compile_used = False
    oversized_smd_split_applied = False
    oversized_smd_split_report: dict[str, Any] = {}
    successful_compile_log_suffix = ""
    while True:
        suffix_parts: list[str] = []
        if fallback_compile_used:
            suffix_parts.append("fallback")
        if not flex_compile_enabled:
            suffix_parts.append("no_flex")
        log_suffix = "_".join(suffix_parts)
        if l4d2:
            clean_expected_compiled_l4d2(compile_game_dir, survivor_slot, carms_qc is not None)
        else:
            clean_expected_compiled(compile_game_dir, author, category, stems)
        try:
            compile_all_models(log_suffix)
            successful_compile_log_suffix = log_suffix
            break
        except StudioMDLCompileError as exc:
            if flex_compile_enabled and is_vta_model_compile_failure(exc, source_dir):
                flex_compile_enabled = False
                flex_compile_disabled_reason = f"StudioMDL failed while loading VTA flex data; see {exc.log_path}"
                warning = (
                    "StudioMDL failed while compiling VTA flex data, so Step 14 is retrying without flex controllers. "
                    "The model will compile, but facial/body flex controls from VTA files will be unavailable. "
                    f"See {exc.log_path}"
                )
                warnings.append(warning)
                emit("WARNING: " + warning)
                carms_qc = refresh_compile_qcs()
                continue
            if not oversized_smd_split_applied and is_too_many_verts_compile_failure(exc):
                split_report = split_oversized_smds_for_compile(source_dir)
                split_rows = split_report.get("splits", [])
                if split_rows:
                    oversized_smd_split_applied = True
                    oversized_smd_split_report = split_report
                    if flex_compile_enabled and source_has_vta_files(source_dir):
                        flex_compile_enabled = False
                        flex_compile_disabled_reason = (
                            f"StudioMDL rejected an oversized SMD while VTA files were present; "
                            f"Step 14 disabled flex controllers before retrying split bodygroups. See {exc.log_path}"
                        )
                    write_json(qc_dir / "oversized_smd_split_report.json", split_report)
                    split_summary = ", ".join(
                        f"{row.get('source')} -> {row.get('chunk_count')} chunks"
                        for row in split_rows
                        if isinstance(row, dict)
                    )
                    flex_note = " Flex controllers are disabled for this compile." if not flex_compile_enabled else ""
                    warning = (
                        "StudioMDL rejected an oversized SMD, so Step 14 split the oversized "
                        f"mesh bodygroup(s) and retried: {split_summary}.{flex_note}"
                    )
                    warnings.append(warning)
                    emit("WARNING: " + warning)
                    carms_qc = refresh_compile_qcs()
                    continue
            if l4d2 and is_collision_block_parser_failure(exc):
                # No GMod-studiomdl fallback for L4D2 (it would compile against the
                # wrong game dir); surface the failure directly.
                raise RuntimeError(
                    f"L4D2 StudioMDL rejected the physics collision block. See {exc.log_path}"
                ) from exc
            if not fallback_compile_used and is_collision_block_parser_failure(exc):
                fallback_candidates = fallback_gmod_compile_candidates(gmod)
                if not fallback_candidates:
                    raise RuntimeError(
                        "The selected StudioMDL rejected the physics collision block, and no standard Garry's Mod "
                        f"StudioMDL fallback was found. See {exc.log_path}"
                    ) from exc
                fallback = fallback_candidates[0]
                compile_studiomdl = Path(str(fallback["studiomdl_path"]))
                compile_game_dir = Path(str(fallback["game_dir"]))
                fallback_compile_used = True
                warning = (
                    "Selected StudioMDL rejected the physics collision block; retrying Step 14 compile with standard "
                    f"Garry's Mod StudioMDL: {compile_studiomdl}"
                )
                warnings.append(warning)
                emit("WARNING: " + warning)
                continue
            raise
    write_definebone_repair_report(qc_dir, definebone_repair_reports)
    studiomdl_logs = {
        "definebones": str(define_log),
        "hitbox_probe": str(hbox_log),
        "main": str(compile_log_path("compile_main", successful_compile_log_suffix)),
        "player": str(compile_log_path("compile_pm", successful_compile_log_suffix)),
    }
    if carms_qc:
        studiomdl_logs["c_arms"] = str(compile_log_path("compile_carms", successful_compile_log_suffix))

    emit("Composing final addon folder.")
    generated_files: list[dict[str, Any]] = []
    compile_source_copy_dir = workspace_compile_source_copy_dir(qc_dir)
    try:
        if same_resolved_path(source_dir, compile_source_copy_dir):
            generated_files.append(folder_row(compile_source_copy_dir, "qc_compile_source", ["Already the active StudioMDL compile source folder."]))
        else:
            copytree_clean(source_dir, compile_source_copy_dir)
            generated_files.append(folder_row(compile_source_copy_dir, "qc_compile_source"))
        # Keep only the re-compilable QCs in the distributed source copy. The
        # distribution copy below is made from this folder, so it inherits the prune.
        prune_extra_compile_source_qc(compile_source_copy_dir)
    except Exception as exc:
        errors.append(f"Failed to copy QC compile source folder: {exc}")
    if l4d2:
        compiled_files, compiled_errors = copy_compiled_outputs_l4d2(compile_game_dir, addon_dir, survivor_slot, carms_qc is not None)
        if not compiled_errors:
            light_files, light_warnings = write_l4d2_light_model_copies(addon_dir, survivor_slot)
            compiled_files = compiled_files + light_files
            warnings.extend(light_warnings)
    else:
        compiled_files, compiled_errors = copy_compiled_outputs(compile_game_dir, addon_dir, author, category, stems)
    generated_files.extend(compiled_files)
    errors.extend(compiled_errors)
    used_materials = parse_used_materials(compile_material_outputs)
    material_files, material_warnings, material_errors = compose_materials(plan, addon_dir, used_materials)
    generated_files.extend(material_files)
    warnings.extend(material_warnings)
    errors.extend(material_errors)
    include_mci_metadata_json = bool(plan.get("include_mci_metadata_json", True))
    simple_vrd_immunity_path: Path | None = None
    dynamic_model_manifest_path: Path | None = None
    if l4d2:
        # L4D2 uses a VPK addoninfo.txt manifest; GMod-only spawn icons / lua /
        # MCI metadata / addon.json are skipped.
        addoninfo_path = write_l4d2_addoninfo(plan, addon_dir)
        generated_files.append(file_row(addoninfo_path, "addoninfo"))
        addonimage_path = write_l4d2_addonimage(plan, addon_dir)
        if addonimage_path is not None:
            generated_files.append(file_row(addonimage_path, "addonimage"))
        else:
            warnings.append("Step 13 release icon not found; addon ships without addonimage.jpg (L4D2 shows the default thumbnail).")
    elif sfm:
        # SFM ships loose models + materials only: no GMod lua/spawn-icons/MCI
        # metadata/addon.json and no L4D2 addoninfo.
        emit("SFM: composing loose model + materials only (no GMod lua/spawn-icons/metadata).")
    else:
        icon_files, icon_warnings = compose_icons(plan, addon_dir)
        generated_files.extend(icon_files)
        warnings.extend(icon_warnings)
        lua_path = write_lua(plan, addon_dir, carms_qc is not None)
        generated_files.append(file_row(lua_path, "lua"))
        if include_mci_metadata_json:
            simple_vrd_immunity_path = write_simple_vrd_immunity(plan, addon_dir, carms_qc is not None)
            generated_files.append(file_row(simple_vrd_immunity_path, "simple_vrd_immunity"))
            dynamic_model_manifest_path = write_dynamic_model_importer_manifest(plan, addon_dir, carms_qc is not None)
            generated_files.append(file_row(dynamic_model_manifest_path, "dynamic_model_importer_manifest"))
        else:
            emit("Skipping MMD Character Importer metadata JSON files by user request.")
        addon_json = addon_dir / "addon.json"
        addon_json.write_text(json.dumps({"title": f"{model}_public_version", "type": "model", "tags": ["cartoon", "fun"]}, indent=4), encoding="utf-8")
        generated_files.append(file_row(addon_json, "addon_json"))

    if l4d2:
        for rel in l4d2_compiled_relpaths(survivor_slot, carms_qc is not None):
            if not (addon_dir / "models" / f"{rel}.mdl").exists():
                errors.append(f"L4D2 compiled .mdl missing from addon folder: models/{rel}.mdl")
    elif sfm:
        model_dir = addon_dir / "models" / author / category
        if not (model_dir / f"{model}.mdl").exists():
            errors.append("Main .mdl output is missing from the final SFM model folder.")
    else:
        required_lua = addon_dir / "lua" / "autorun" / f"{model}_{author}.lua"
        if not required_lua.exists():
            errors.append("Lua autorun file was not written.")
        model_dir = addon_dir / "models" / author / category
        if not (model_dir / f"{model}.mdl").exists() or not (model_dir / f"{model}_pm.mdl").exists():
            errors.append("Main or player-model .mdl output is missing from the final addon folder.")
    mat_dir = addon_dir / "materials" / "models" / author / model
    if not any(mat_dir.glob("*.vmt")):
        warnings.append("No material VMT files were written.")

    # L4D2 ships as a single .vpk; the unpacked addon folder is not needed in-game. SFM always
    # also gets a .vpk (alongside the loose distribution folder, per the user's choice). Package
    # the VPK once here and deliver it below. GMod keeps copying the unpacked addon folder + a .gma.
    packaged_vpk_path: Path | None = None
    if l4d2:
        try:
            emit("Packaging final addon VPK.")
            packaged_vpk_path = package_addon_vpk(
                addon_dir, qc_dir / f"survivor_{survivor_slot}_{model}.vpk", gmod, qc_dir / "vpk_create.log"
            )
            generated_files.append(file_row(packaged_vpk_path, "vpk_package"))
            emit(f"Wrote VPK package: {packaged_vpk_path}")
        except Exception as exc:
            errors.append(f"Failed to package addon VPK: {exc}")
    elif sfm:
        try:
            emit("Packaging SFM addon VPK.")
            packaged_vpk_path = package_addon_vpk(
                addon_dir, qc_dir / f"{model}_{author}.vpk", gmod, qc_dir / "vpk_create.log"
            )
            generated_files.append(file_row(packaged_vpk_path, "vpk_package"))
            emit(f"Wrote VPK package: {packaged_vpk_path}")
        except Exception as exc:
            errors.append(f"Failed to package SFM addon VPK: {exc}")

    distribution_output_dir = Path(str(plan.get("distribution_output_dir") or "")).expanduser()
    distribution_addon_dir = Path("")
    distribution_gma = Path("")
    distribution_compile_source_dir = Path("")
    gmod_addons_dir = Path("")
    gmod_addon_dir = Path("")

    if str(distribution_output_dir).strip() and str(distribution_output_dir) != ".":
        emit(f"Copying final addon package to user-selected folder: {distribution_output_dir}")
        try:
            if path_is_inside(distribution_output_dir, addon_dir):
                raise RuntimeError("The selected distribution folder cannot be inside the composed addon folder.")
            if compile_source_copy_dir.exists() and path_is_inside(distribution_output_dir, compile_source_copy_dir):
                raise RuntimeError("The selected distribution folder cannot be inside the QC compile source folder.")
            distribution_output_dir.mkdir(parents=True, exist_ok=True)
            if not l4d2:
                # L4D2 delivers only the .vpk (below); the unpacked addon folder is not needed.
                # GMod and SFM both copy the unpacked folder (SFM: loose model + materials).
                distribution_addon_dir = distribution_output_dir / addon_dir.name
                if same_resolved_path(distribution_addon_dir, addon_dir):
                    generated_files.append(folder_row(distribution_addon_dir, "distribution_addon_folder", ["Already composed in this folder."]))
                else:
                    copytree_clean(addon_dir, distribution_addon_dir)
                    generated_files.append(folder_row(distribution_addon_dir, "distribution_addon_folder"))
            if compile_source_copy_dir.exists():
                distribution_compile_source_dir = distribution_compile_source_copy_dir(distribution_output_dir, model, author)
                if same_resolved_path(distribution_compile_source_dir, compile_source_copy_dir):
                    generated_files.append(folder_row(distribution_compile_source_dir, "distribution_qc_compile_source", ["Already copied in this folder."]))
                else:
                    copytree_clean(compile_source_copy_dir, distribution_compile_source_dir)
                    generated_files.append(folder_row(distribution_compile_source_dir, "distribution_qc_compile_source"))
            if l4d2 or sfm:
                # Deliver the packaged .vpk. L4D2 ships only the vpk; SFM also keeps the
                # loose folder copied above.
                if packaged_vpk_path and packaged_vpk_path.exists():
                    distribution_gma = distribution_output_dir / packaged_vpk_path.name
                    if not same_resolved_path(distribution_gma, packaged_vpk_path):
                        shutil.copyfile(packaged_vpk_path, distribution_gma)
                    generated_files.append(file_row(distribution_gma, "vpk_package"))
                    emit(f"Copied VPK package to distribution folder: {distribution_gma}")
            else:
                emit("Packaging final addon GMA.")
                distribution_gma = distribution_output_dir / f"{model}_{author}.gma"
                distribution_gma = package_addon_gma(addon_dir, distribution_gma, gmod, qc_dir / "gmad_create.log")
                generated_files.append(file_row(distribution_gma, "gma_package"))
                emit(f"Wrote GMA package: {distribution_gma}")
        except Exception as exc:
            errors.append(f"Failed to copy/package addon to selected output folder: {exc}")

    # SFM uses its own "put into usermod" toggle (default on); GMod/L4D2 use copy_to_gmod_addons.
    install_to_game = bool(plan.get("copy_to_sfm_usermod", True)) if sfm else bool(plan.get("copy_to_gmod_addons", False))
    if install_to_game:
        try:
            target_game_dir = Path(str(gmod.get("game_dir") or ""))
            if not target_game_dir.exists():
                raise RuntimeError(f"Game directory was not found: {target_game_dir}")
            if sfm:
                # SFM uses loose files in the usermod content dir (game_dir = usermod):
                # overlay models/ + materials/ without disturbing other SFM content.
                emit("Installing loose model + materials into SFM usermod.")
                installed_any = False
                for sub in ("models", "materials"):
                    src = addon_dir / sub
                    if src.exists():
                        dst = target_game_dir / sub
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                        generated_files.append(folder_row(dst, "sfm_usermod_folder"))
                        installed_any = True
                gmod_addons_dir = target_game_dir
                gmod_addon_dir = target_game_dir  # report/open points at the usermod folder
                if installed_any:
                    emit(f"Installed loose SFM content into: {target_game_dir}")
                else:
                    errors.append("SFM composed folder had no models/ or materials/ to install into usermod.")
            else:
                gmod_addons_dir = target_game_dir / "addons"
                gmod_addons_dir.mkdir(parents=True, exist_ok=True)
                if l4d2:
                    # L4D2 installs the single .vpk into addons/; the unpacked folder is not needed.
                    emit("Installing addon VPK to detected L4D2 addons folder.")
                    if packaged_vpk_path and packaged_vpk_path.exists():
                        installed_vpk = gmod_addons_dir / packaged_vpk_path.name
                        if not same_resolved_path(installed_vpk, packaged_vpk_path):
                            shutil.copyfile(packaged_vpk_path, installed_vpk)
                        generated_files.append(file_row(installed_vpk, "gmod_addons_vpk"))
                        gmod_addon_dir = gmod_addons_dir  # report/open points at the addons folder
                        emit(f"Installed addon VPK: {installed_vpk}")
                    else:
                        errors.append("L4D2 addon VPK was not packaged, so nothing was installed to the addons folder.")
                else:
                    emit("Installing composed addon folder to detected GMod addons folder.")
                    gmod_addon_dir = gmod_addons_dir / addon_dir.name
                    if same_resolved_path(gmod_addon_dir, addon_dir):
                        generated_files.append(folder_row(gmod_addon_dir, "gmod_addons_folder", ["Already composed in this folder."]))
                    else:
                        copytree_clean(addon_dir, gmod_addon_dir)
                        generated_files.append(folder_row(gmod_addon_dir, "gmod_addons_folder"))
                    emit(f"Installed composed addon folder: {gmod_addon_dir}")
        except Exception as exc:
            label = "SFM usermod" if sfm else ("L4D2" if l4d2 else "GMod")
            errors.append(f"Failed to copy addon to {label}: {exc}")

    files_json = qc_dir / "qc_files.json"
    report_json = qc_dir / "qc_report.json"
    validation = {"ok": not errors, "errors": errors, "warnings": warnings}
    files_payload = {
        "addon_dir": str(addon_dir),
        "distribution_output_dir": str(distribution_output_dir) if str(distribution_output_dir) != "." else "",
        "distribution_addon_dir": str(distribution_addon_dir) if str(distribution_addon_dir) != "." else "",
        "distribution_gma": str(distribution_gma) if str(distribution_gma) != "." else "",
        "compile_source_copy_dir": str(compile_source_copy_dir),
        "distribution_compile_source_dir": str(distribution_compile_source_dir) if str(distribution_compile_source_dir) != "." else "",
        "include_mci_metadata_json": include_mci_metadata_json,
        "studiomdl_logs": studiomdl_logs,
        "flex_compile_enabled": flex_compile_enabled,
        "flex_compile_disabled_reason": flex_compile_disabled_reason,
        "oversized_smd_split_report": str(qc_dir / "oversized_smd_split_report.json") if oversized_smd_split_report else "",
        "gmod_addons_dir": str(gmod_addons_dir) if str(gmod_addons_dir) != "." else "",
        "gmod_addon_dir": str(gmod_addon_dir) if str(gmod_addon_dir) != "." else "",
        "files": generated_files,
        "validation": validation,
        "validation_errors": errors,
        "warnings": warnings,
    }
    report = {
        "version": 1,
        "kind": "sort_qc_compile_report",
        "status": "complete" if not errors else "incomplete",
        "addon_dir": str(addon_dir),
        "distribution_output_dir": str(distribution_output_dir) if str(distribution_output_dir) != "." else "",
        "distribution_addon_dir": str(distribution_addon_dir) if str(distribution_addon_dir) != "." else "",
        "distribution_gma": str(distribution_gma) if str(distribution_gma) != "." else "",
        "compile_source_copy_dir": str(compile_source_copy_dir),
        "distribution_compile_source_dir": str(distribution_compile_source_dir) if str(distribution_compile_source_dir) != "." else "",
        "copy_to_gmod_addons": bool(plan.get("copy_to_gmod_addons", False)),
        "copy_to_sfm_usermod": bool(plan.get("copy_to_sfm_usermod", True)),
        "include_mci_metadata_json": include_mci_metadata_json,
        "studiomdl_logs": studiomdl_logs,
        "flex_compile_enabled": flex_compile_enabled,
        "flex_compile_disabled_reason": flex_compile_disabled_reason,
        "oversized_smd_split_report": str(qc_dir / "oversized_smd_split_report.json") if oversized_smd_split_report else "",
        "gmod_addons_dir": str(gmod_addons_dir) if str(gmod_addons_dir) != "." else "",
        "gmod_addon_dir": str(gmod_addon_dir) if str(gmod_addon_dir) != "." else "",
        "source_dir": str(source_dir),
        "main_qc": str(main_qc),
        "pm_qc": str(pm_qc),
        "carms_qc": str(carms_qc) if carms_qc else "",
        "simple_vrd_immunity": str(simple_vrd_immunity_path) if simple_vrd_immunity_path else "",
        "dynamic_model_importer_manifest": str(dynamic_model_manifest_path) if dynamic_model_manifest_path else "",
        "definebone_count": len(definebones),
        "l4d2_proportion_rotation_delta": proportion_delta_report,
        "l4d2_tls_weapon_bones_injected": tls_injected_bones,
        "definebone_repair": {
            "report": str(qc_dir / "missing_definebones_repair.json"),
            "inserted_bones": sorted(
                {
                    str(name)
                    for repair in definebone_repair_reports
                    for name in repair.get("inserted_bones", [])
                    if str(name)
                },
                key=natural_key,
            ),
            "unrecoverable_missing_parents": sorted(
                {
                    str(name)
                    for repair in definebone_repair_reports
                    for name in repair.get("unrecoverable_missing_parents", [])
                    if str(name)
                },
                key=natural_key,
            ),
        },
        "hbox_count": len(hboxes),
        "jiggle_count": len(jiggles),
        "omni_jiggle_count": len(ignores),
        "invert_jiggle_direction": bool(plan.get("invert_jiggle_direction", False)),
        "compiled_stems": stems,
        "validation": validation,
        "validation_errors": errors,
        "warnings": warnings,
    }
    emit(f"Wrote QC report: {report_json}")
    write_json(report_json, report)
    write_json(files_json, files_payload)
    emit(f"Wrote final addon folder: {addon_dir}")
    if errors:
        emit("Step 14 completed with validation errors: " + "; ".join(errors))
    elif warnings:
        emit(f"Step 14 complete with warnings: {len(warnings)} warning(s).")
    else:
        emit("Step 14 complete.")
    return {"report": report, "files": files_payload}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["analyze", "compile"], required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--analysis-json", type=Path)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--files-json", type=Path)
    parser.add_argument("--author", default="")
    parser.add_argument("--character-category", default="")
    parser.add_argument("--model-name", default="")
    parser.add_argument("--gender", choices=GENDER_CHOICES, default="female")
    parser.add_argument("--game", choices=GAME_CHOICES, default="gmod")
    parser.add_argument("--survivor", choices=L4D2_SURVIVOR_SLOTS, default=DEFAULT_L4D2_SURVIVOR)
    parser.add_argument("--gmod-root", default="")
    parser.add_argument("--studiomdl", default="")
    return parser.parse_args()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = parse_args()
    if args.mode == "analyze":
        result = analyze(args.input, args.author, args.character_category, args.model_name, args.gmod_root, args.studiomdl, args.gender, args.game, args.survivor)
        if args.analysis_json and Path(result["paths"]["analysis"]) != args.analysis_json:
            shutil.copyfile(result["paths"]["analysis"], args.analysis_json)
        if Path(result["paths"]["plan"]) != args.plan_json:
            shutil.copyfile(result["paths"]["plan"], args.plan_json)
        return 0
    try:
        result = compose(args.plan_json)
    except Exception as exc:
        report_path, files_path = write_failure_outputs(args.plan_json, exc)
        if args.report_json and not same_resolved_path(report_path, args.report_json):
            shutil.copyfile(report_path, args.report_json)
        if args.files_json and not same_resolved_path(files_path, args.files_json):
            shutil.copyfile(files_path, args.files_json)
        raise
    if args.report_json:
        write_json(args.report_json, result["report"])
    if args.files_json:
        write_json(args.files_json, result["files"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
