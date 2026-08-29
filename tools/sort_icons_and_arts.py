#!/usr/bin/env python3
"""Step 13 icon/art processor.

This script handles the pure-Python side of Step 13: locating the Step 1 PMX
workspace, normalizing a rendered/custom release icon, generating Friendly /
Enemy spawn icon images, writing VMT files, and optionally converting JPGs to
VTF through VTFCmd.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:  # pragma: no cover - dependency guard
    raise SystemExit("Pillow is required for Step 13 icon generation.") from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VMD = ROOT / "reference" / "ref_motion" / "bad_bad_water.vmd"
ICON_DIR_NAME = "13_sort_icons_and_arts"
RELEASE_SIZE = 1024
SPAWN_SIZE = 512


def configure_text_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["analyze", "process"], required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--analysis-json", type=Path)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--files-json", type=Path)
    parser.add_argument("--release-source", type=Path)
    parser.add_argument("--spawn-source", type=Path)
    parser.add_argument("--icon-basename", default="")
    parser.add_argument("--body-vmd", type=Path)
    parser.add_argument("--face-vmd", action="append", default=[], type=Path)
    parser.add_argument("--frame", type=int)
    return parser.parse_args()


def safe_name(value: str, fallback: str = "model") -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:80] or fallback


def path_is_ascii(path: Path | str) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def external_tool_root() -> Path:
    # VTFCmd uses ANSI file APIs, so the staging root itself must be ASCII.
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


def find_workspace_root(input_path: Path) -> Path:
    path = input_path.resolve()
    if path.is_file():
        parents = [path.parent] + list(path.parents)
    else:
        parents = [path] + list(path.parents)
    for parent in parents:
        if parent.name == "0_source_mmd_assets":
            return parent.parent
        if (parent / "0_source_mmd_assets").exists():
            return parent
        if parent.name == "1_import_mmd_model" and parent.parent.exists():
            return parent.parent
    if path.is_file():
        return path.parent
    return path


MODEL_SUFFIXES = (".pmx", ".vrm")


def pmx_candidates(input_path: Path, workspace_root: Path) -> list[Path]:
    path = input_path.resolve()
    candidates: list[Path] = []
    if path.is_file() and path.suffix.lower() in MODEL_SUFFIXES:
        candidates.append(path)
    source_assets = workspace_root / "0_source_mmd_assets"
    search_dirs = []
    if path.is_dir():
        search_dirs.append(path)
    if source_assets.exists():
        search_dirs.insert(0, source_assets)
    if path.is_file() and path.suffix.lower() == ".blend":
        search_dirs.append(path.parent)
    for folder in search_dirs:
        for pattern in ("*.pmx", "*.vrm"):
            try:
                candidates.extend(folder.rglob(pattern))
            except Exception:
                pass
    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate.absolute()
        if resolved not in seen and resolved.exists():
            seen.add(resolved)
            out.append(resolved)
    return sorted(out, key=lambda item: (-item.stat().st_size, str(item).lower()))


def step1_imported_model_path(workspace_root: Path) -> Path | None:
    """The model Step 1 actually imported, from its report (authoritative).

    A source folder can contain several PMX/VRM files; scanning alone can pick
    a sibling model that was never imported.
    """
    report_path = workspace_root / "1_import_mmd_model" / "blender_import_report.json"
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw = str(report.get("pmx_path") or report.get("vrm_path") or "")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.exists() and candidate.suffix.lower() in MODEL_SUFFIXES:
        return candidate
    return None


def resolve_input(input_path: Path) -> tuple[Path, Path, Path]:
    workspace_root = find_workspace_root(input_path)
    path = input_path.resolve()
    if path.is_file() and path.suffix.lower() in MODEL_SUFFIXES:
        model_path = path
    else:
        model_path = step1_imported_model_path(workspace_root)
    if model_path is None:
        candidates = pmx_candidates(input_path, workspace_root)
        if not candidates:
            raise FileNotFoundError(f"No PMX or VRM model found from Step 1 input: {input_path}")
        model_path = candidates[0]
    output_dir = workspace_root / ICON_DIR_NAME
    return workspace_root, model_path, output_dir


def write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_plan(
    input_path: Path,
    icon_basename: str = "",
    body_vmd: Path | None = None,
    face_vmds: list[Path] | None = None,
    frame: int | None = None,
) -> dict[str, object]:
    workspace_root, pmx_path, output_dir = resolve_input(input_path)
    basename = safe_name(icon_basename or pmx_path.stem, fallback=safe_name(workspace_root.name, "model"))
    body_vmd = body_vmd or DEFAULT_VMD
    face_vmds = face_vmds or []
    selected_frame = 334 if frame is None else int(frame)
    return {
        "step": 13,
        "input_path": str(input_path.resolve()),
        "workspace_root": str(workspace_root),
        "pmx_path": str(pmx_path),
        "vmd_path": str(body_vmd),
        "body_vmd_path": str(body_vmd),
        "face_vmd_paths": [str(path) for path in face_vmds],
        "frame": selected_frame,
        "icon_basename": basename,
        "output_dir": str(output_dir),
        "release_icon": str(output_dir / "release_icon.png"),
        "compat_release_icon": str(output_dir / "SPIC.png"),
        "spawn_source": str(output_dir / "spawn_source.png"),
        "friendly_jpg": str(output_dir / "F.jpg"),
        "enemy_jpg": str(output_dir / "E.jpg"),
        "friendly_png": str(output_dir / "F.png"),
        "enemy_png": str(output_dir / "E.png"),
        "friendly_vtf": str(output_dir / "F.vtf"),
        "enemy_vtf": str(output_dir / "E.vtf"),
        "friendly_vmt": str(output_dir / "F.vmt"),
        "enemy_vmt": str(output_dir / "E.vmt"),
        "warnings": [],
    }


def normalize_release_icon(source_path: Path, output_path: Path) -> dict[str, object]:
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    with Image.open(source_path) as image:
        original_size = image.size
        image = image.convert("RGBA")
        scale = min(RELEASE_SIZE / max(1, image.width), RELEASE_SIZE / max(1, image.height))
        new_size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
        resized = image.resize(new_size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (RELEASE_SIZE, RELEASE_SIZE), (255, 255, 255, 255))
        offset = ((RELEASE_SIZE - new_size[0]) // 2, (RELEASE_SIZE - new_size[1]) // 2)
        canvas.alpha_composite(resized, offset)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path, "PNG")
    return {
        "source": str(source_path),
        "output": str(output_path),
        "original_size": list(original_size),
        "output_size": [RELEASE_SIZE, RELEASE_SIZE],
        "resized": list(new_size),
    }


def font_candidates() -> list[Path]:
    return [
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/Arialbd.ttf"),
        Path("C:/Windows/Fonts/calibrib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
    ]


def find_font(size: int):
    for path in font_candidates():
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def text_bbox(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int, int, int]:
    if hasattr(draw, "textbbox"):
        return draw.textbbox((0, 0), text, font=font, stroke_width=0)
    width, height = draw.textsize(text, font=font)
    return (0, 0, width, height)


def fit_image_on_canvas(source_path: Path, canvas_size: int = SPAWN_SIZE) -> tuple[Image.Image, dict[str, object]]:
    with Image.open(source_path) as source:
        original_size = source.size
        source = source.convert("RGBA")
        scale = min(canvas_size / max(1, source.width), canvas_size / max(1, source.height))
        new_size = (max(1, int(round(source.width * scale))), max(1, int(round(source.height * scale))))
        resized = source.resize(new_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 255))
    offset = ((canvas_size - new_size[0]) // 2, max(0, (canvas_size - new_size[1]) // 2 - 8))
    canvas.alpha_composite(resized, offset)
    return canvas, {"source_size": list(original_size), "resized": list(new_size), "offset": list(offset)}


def draw_labeled_text(image: Image.Image, label: str, fill: tuple[int, int, int], canvas_size: int = SPAWN_SIZE) -> Image.Image:
    draw = ImageDraw.Draw(image)
    font_size = 88
    font = find_font(font_size)
    max_width = int(canvas_size * 0.88)
    while font_size > 20:
        font = find_font(font_size)
        bbox = draw.textbbox((0, 0), label, font=font, stroke_width=10) if hasattr(draw, "textbbox") else text_bbox(draw, label, font)
        if bbox[2] - bbox[0] <= max_width:
            break
        font_size -= 2
    bbox = draw.textbbox((0, 0), label, font=font, stroke_width=10) if hasattr(draw, "textbbox") else text_bbox(draw, label, font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (canvas_size - text_width) // 2 - bbox[0]
    y = canvas_size - text_height - 28 - bbox[1]
    shadow_offset = 3
    draw.text((x + shadow_offset, y + shadow_offset), label, font=font, fill=(160, 160, 160, 255), stroke_width=12, stroke_fill=(220, 220, 220, 255))
    draw.text((x, y), label, font=font, fill=fill + (255,), stroke_width=8, stroke_fill=(255, 255, 255, 255))
    return image


def draw_spawn_icon(source_path: Path, label: str, fill: tuple[int, int, int], output_jpg: Path, output_png: Path) -> dict[str, object]:
    image, fit_info = fit_image_on_canvas(source_path, SPAWN_SIZE)
    image = draw_labeled_text(image, label, fill, SPAWN_SIZE)
    output_jpg.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output_jpg, "JPEG", quality=95, subsampling=0)
    image.save(output_png, "PNG")
    return {"jpg": str(output_jpg), "png": str(output_png), "label": label, "size": [SPAWN_SIZE, SPAWN_SIZE], "source": str(source_path), "fit": fit_info}


def find_vtfcmd() -> Path | None:
    env_path = os.environ.get("VTFCMD")
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return candidate
    for candidate in bundled_vtfcmd_candidates():
        if candidate.exists():
            return candidate
    path_hit = shutil.which("VTFCmd.exe") or shutil.which("VTFCmd")
    if path_hit:
        return Path(path_hit)
    for raw in (
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


# Issue #167: on Linux VTFCmd.exe runs under Wine, which cannot consume POSIX argument
# paths (they get resolved relative to the working directory). Translate absolute POSIX
# path arguments to Wine's default Z:\ drive mapping and launch through `wine` when it
# is on PATH (otherwise rely on the distro's binfmt_misc handler). No-op on Windows.
# Mirrors adapt_windows_tool_command in sort_qc_compile.py (the step tools stay
# standalone scripts, like hidden_subprocess_kwargs).
def adapt_windows_tool_command(command: list[str]) -> list[str]:
    if os.name == "nt" or not command:
        return command
    executable = str(command[0])
    if not executable.lower().endswith(".exe"):
        return command
    adapted = [executable]
    for part in command[1:]:
        text = str(part)
        adapted.append("Z:" + text.replace("/", "\\") if text.startswith("/") else text)
    wine = shutil.which("wine") or shutil.which("wine64")
    if wine:
        adapted.insert(0, wine)
    return adapted


def convert_to_vtf(vtfcmd: Path, image_path: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected = output_dir / f"{image_path.stem}.vtf"
    actual_image = image_path
    actual_output_dir = output_dir
    staged_expected: Path | None = None
    staging_root: Path | None = None
    if not path_is_ascii(image_path) or not path_is_ascii(output_dir):
        staging_root = external_tool_root()
        staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"step13_vtf_{safe_name(image_path.stem, 'icon')}_", dir=str(staging_root) if staging_root else None) as scratch_raw:
        scratch = Path(scratch_raw)
        if staging_root:
            scratch_input = scratch / "input"
            scratch_output = scratch / "output"
            scratch_input.mkdir(parents=True, exist_ok=True)
            scratch_output.mkdir(parents=True, exist_ok=True)
            actual_image = scratch_input / f"{safe_name(image_path.stem, 'icon')}{image_path.suffix.lower()}"
            shutil.copyfile(image_path, actual_image)
            actual_output_dir = scratch_output
            staged_expected = actual_output_dir / f"{actual_image.stem}.vtf"
        command = adapt_windows_tool_command(
            [str(vtfcmd), "-file", str(actual_image), "-output", str(actual_output_dir), "-silent"]
        )
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
                raise RuntimeError(f"VTFCmd finished but {staged_expected.name} was not found in ASCII staging output")
            shutil.copyfile(staged_expected, expected)
        if not expected.exists():
            raise RuntimeError(f"VTFCmd finished but {expected.name} was not found in {output_dir}")
    return expected


def write_vmt(path: Path, base_texture: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '"UnlitGeneric"\n'
        "{\n"
        f'    "$basetexture" "{base_texture}"\n'
        '    "$translucent" "1"\n'
        '    "$vertexcolor" "1"\n'
        '    "$vertexalpha" "1"\n'
        "}\n",
        encoding="utf-8",
    )


def file_row(path: Path, stage: str, warnings: list[str] | None = None) -> dict[str, object]:
    return {
        "name": path.name,
        "type": path.suffix.lower().lstrip("."),
        "path": str(path),
        "size": path.stat().st_size if path.exists() else 0,
        "stage": stage,
        "warnings": warnings or [],
        "exists": path.exists(),
    }


def process_icons(
    plan: dict[str, object],
    release_source: Path,
    spawn_source: Path | None = None,
    icon_basename: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    output_dir = Path(str(plan.get("output_dir") or ""))
    output_dir.mkdir(parents=True, exist_ok=True)
    if icon_basename:
        plan["icon_basename"] = safe_name(icon_basename)
    basename = safe_name(str(plan.get("icon_basename") or ""), fallback=safe_name(Path(str(plan.get("workspace_root") or "")).name, "model"))
    plan["icon_basename"] = basename
    validation_errors: list[str] = []
    warnings: list[str] = []
    generated: list[dict[str, object]] = []

    release_icon = output_dir / "release_icon.png"
    spic = output_dir / "SPIC.png"
    spawn_source_output = output_dir / "spawn_source.png"
    release_info = normalize_release_icon(release_source, release_icon)
    shutil.copyfile(release_icon, spic)
    generated.append(file_row(release_icon, "release_icon"))
    generated.append(file_row(spic, "compat_release_icon"))
    if spawn_source and spawn_source.exists():
        if spawn_source.resolve() != spawn_source_output.resolve():
            normalize_release_icon(spawn_source, spawn_source_output)
        generated.append(file_row(spawn_source_output, "spawn_source"))
    else:
        spawn_source_output = spic
        warnings.append("No separate spawn source was provided; Friendly/Enemy icons used the release icon crop.")

    friendly_info = draw_spawn_icon(spawn_source_output, "Friendly", (0, 170, 255), output_dir / "F.jpg", output_dir / "F.png")
    enemy_info = draw_spawn_icon(spawn_source_output, "Enemy", (255, 20, 20), output_dir / "E.jpg", output_dir / "E.png")
    for path in (Path(friendly_info["jpg"]), Path(friendly_info["png"]), Path(enemy_info["jpg"]), Path(enemy_info["png"])):
        generated.append(file_row(path, "spawn_icon"))

    write_vmt(output_dir / "F.vmt", f"vgui/entities/{basename}_F")
    write_vmt(output_dir / "E.vmt", f"vgui/entities/{basename}_E")
    generated.append(file_row(output_dir / "F.vmt", "vmt"))
    generated.append(file_row(output_dir / "E.vmt", "vmt"))

    vtfcmd = find_vtfcmd()
    vtf_conversion: dict[str, object] = {"vtfcmd": str(vtfcmd) if vtfcmd else "", "converted": []}
    if not vtfcmd:
        validation_errors.append("VTFCmd.exe was not found; F.vtf and E.vtf were not generated.")
        warnings.append("Bundled VTFCmd was not found; set VTFCMD or install VTFEdit/VTFCmd to complete VTF conversion.")
    else:
        for jpg in (output_dir / "F.jpg", output_dir / "E.jpg"):
            try:
                vtf = convert_to_vtf(vtfcmd, jpg, output_dir)
                vtf_conversion["converted"].append(str(vtf))
                generated.append(file_row(vtf, "vtf"))
                # F.vmt/E.vmt reference vgui/entities/{basename}_F/_E, so also write a
                # matching-named VTF copy beside them; the VMT+VTF pair then resolves
                # when manually copied into materials/vgui/entities.
                named_vtf = output_dir / f"{basename}_{jpg.stem}.vtf"
                shutil.copyfile(vtf, named_vtf)
                generated.append(file_row(named_vtf, "vtf"))
            except Exception as exc:
                validation_errors.append(str(exc))
                generated.append(file_row(output_dir / f"{jpg.stem}.vtf", "vtf", [str(exc)]))

    for required in (release_icon, spic, output_dir / "F.jpg", output_dir / "E.jpg", output_dir / "F.vmt", output_dir / "E.vmt"):
        if not required.exists():
            validation_errors.append(f"Required icon output was not written: {required}")

    report = {
        "step": 13,
        "status": "complete" if not validation_errors else "incomplete",
        "output_dir": str(output_dir),
        "pmx_path": str(plan.get("pmx_path") or ""),
        "vmd_path": str(plan.get("vmd_path") or ""),
        "body_vmd_path": str(plan.get("body_vmd_path") or plan.get("vmd_path") or ""),
        "face_vmd_paths": list(plan.get("face_vmd_paths") or []),
        "frame": plan.get("frame", 334),
        "icon_basename": basename,
        "release_source": str(release_source),
        "spawn_source": str(spawn_source_output),
        "release_info": release_info,
        "friendly_info": friendly_info,
        "enemy_info": enemy_info,
        "vtf_conversion": vtf_conversion,
        "validation_errors": validation_errors,
        "warnings": warnings,
    }
    files = {"output_dir": str(output_dir), "files": generated, "validation_errors": validation_errors, "warnings": warnings}
    return report, files


def main() -> int:
    configure_text_streams()
    args = parse_args()
    if args.mode == "analyze":
        plan = build_plan(args.input, args.icon_basename, args.body_vmd, args.face_vmd, args.frame)
        analysis = {
            "step": 13,
            "input_path": str(args.input.resolve()),
            "workspace_root": plan["workspace_root"],
            "pmx_path": plan["pmx_path"],
            "vmd_path": plan["vmd_path"],
            "body_vmd_path": plan["body_vmd_path"],
            "face_vmd_paths": plan["face_vmd_paths"],
            "frame": plan["frame"],
            "vmd_exists": Path(str(plan["body_vmd_path"])).exists(),
            "output_dir": plan["output_dir"],
            "icon_basename": plan["icon_basename"],
            "warnings": [] if Path(str(plan["body_vmd_path"])).exists() else ["Body/reference VMD file is missing."],
        }
        if args.analysis_json:
            write_json(args.analysis_json, analysis)
        write_json(args.plan_json, plan)
        print(f"[Step13 Icons] Analysis wrote plan: {args.plan_json}", flush=True)
        return 0

    if not args.report_json or not args.files_json:
        raise RuntimeError("--report-json and --files-json are required in process mode")
    plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
    release_source = args.release_source or Path(str(plan.get("release_icon") or ""))
    spawn_source = args.spawn_source
    if spawn_source is None and plan.get("spawn_source"):
        spawn_source = Path(str(plan.get("spawn_source") or ""))
    report, files = process_icons(plan, release_source, spawn_source, args.icon_basename)
    write_json(args.report_json, report)
    write_json(args.files_json, files)
    write_json(args.plan_json, plan)
    print(f"[Step13 Icons] Wrote report: {args.report_json}", flush=True)
    if report.get("validation_errors"):
        print("[Step13 Icons] Incomplete: " + "; ".join(str(item) for item in report["validation_errors"]), flush=True)
    else:
        print("[Step13 Icons] Icon generation complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
