#!/usr/bin/env python3
"""Static PMX/VRM OpenGL preview for the MMD Character Importer."""

from __future__ import annotations

import base64
import ctypes
import colorsys
import hashlib
import io
import json
import math
import os
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import numpy as np

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except Exception as exc:  # pragma: no cover - GUI dependency guard
    raise RuntimeError("PySide6 is required for model preview") from exc

try:
    from OpenGL import GL
except Exception:  # pragma: no cover - widget reports unavailable backend
    GL = None

try:
    import mmd_character_importer_core as core
except ModuleNotFoundError:
    from . import mmd_character_importer_core as core  # type: ignore[no-redef]


BONE_LINE_WIDTH = 2.2
BONE_POINT_RADIUS = 3.2
BONE_OVERLAY_LINE_WIDTH = 2.6
BONE_OVERLAY_POINT_SIZE = 5.0
BONE_HIGHLIGHT_LINE_WIDTH = 6.0
BONE_HIGHLIGHT_POINT_RADIUS = 8.0
BONE_HIGHLIGHT_POINT_SIZE = 10.0

# --- Height indicator (issue #79) ---------------------------------------
# Calibrated from the firefly_alt reference model (full bounding box = 1.65 m
# in game). Main preview shows raw model space; Step 5 shows Blender space.
HEIGHT_REFERENCE_METERS = 1.65
# Raw preview-space units per meter at each format's DEFAULT scale multiplier.
# PMX import space (CATS, ~0.076 m/unit): firefly_alt Y-extent 20.669 -> 1.65 m.
# VRM/glTF is authored in real meters, so ~1 unit per meter.
MAIN_PREVIEW_UNITS_PER_METER = {"pmx": 12.527, "vrm": 1.0}
# Blender (Step 6 pre-scale) Z-unit -> meters at scale multiplier 1.0:
# firefly_alt Blender height 1.5748 -> 1.65 m  (= 40.457 / 38.61).
BLENDER_PREVIEW_METERS_PER_UNIT = 1.04776


def _nice_tick_interval_meters(pixels_per_meter: float) -> float:
    """Pick a tick interval that keeps labels readable and gets finer on zoom-in.

    Defaults to 0.25 m at normal zoom and shrinks (0.1, 0.05, ...) as the model
    fills more pixels per meter, or grows (0.5, 1.0, ...) when zoomed out.
    """
    ladder = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)
    if not math.isfinite(pixels_per_meter) or pixels_per_meter <= 0.0:
        return 0.25
    for interval in ladder:
        if interval * pixels_per_meter >= 30.0:  # min vertical gap between ticks
            return interval
    return ladder[-1]


def draw_meter_height_bar(
    painter: "QtGui.QPainter",
    project_points,
    scene_mins,
    scene_maxs,
    up_axis: int,
    meters_per_unit: float,
    reference_m: float = HEIGHT_REFERENCE_METERS,
) -> None:
    """Draw a screen-space meter ruler beside the model with a highlighted
    reference height.

    ``project_points`` maps an (N,3) world-space array to a list of QPointF
    screen positions using the widget's current camera, so the ruler stays
    accurate under zoom/pan/rotation. ``meters_per_unit`` already includes the
    active scale multiplier, so the readout reflects the scaled in-game height.
    The ruler sits just left of the model and uses adaptive 0.25 m ticks that
    get finer as the user zooms in.
    """
    if meters_per_unit <= 0.0 or scene_mins is None or scene_maxs is None:
        return
    try:
        ground = float(scene_mins[up_axis])
        top = float(scene_maxs[up_axis])
    except Exception:
        return
    span_units = top - ground
    if not math.isfinite(span_units) or span_units <= 1e-6:
        return
    units_per_meter = 1.0 / meters_per_unit
    model_height_m = span_units * meters_per_unit
    other_axes = [a for a in range(3) if a != up_axis]
    c0 = float((scene_mins[other_axes[0]] + scene_maxs[other_axes[0]]) * 0.5)
    c1 = float((scene_mins[other_axes[1]] + scene_maxs[other_axes[1]]) * 0.5)

    def world_point(meters: float) -> list[float]:
        p = [0.0, 0.0, 0.0]
        p[up_axis] = ground + meters * units_per_meter
        p[other_axes[0]] = c0
        p[other_axes[1]] = c1
        return p

    # Vertical pixels-per-meter from a 1 m probe, for adaptive tick spacing.
    probe = project_points(np.array([world_point(0.0), world_point(1.0)], dtype=np.float64))
    pixels_per_meter = abs(probe[1].y() - probe[0].y()) if len(probe) >= 2 else 0.0
    interval = _nice_tick_interval_meters(pixels_per_meter)
    decimals = 0 if interval >= 1.0 else 1 if interval >= 0.5 else 2 if interval >= 0.1 else 3

    axis_top_m = max(model_height_m, reference_m) + interval
    tick_meters: list[float] = []
    m = 0.0
    while m <= axis_top_m + 1e-6:
        tick_meters.append(round(m, 4))
        m += interval
    if reference_m not in tick_meters:
        tick_meters.append(reference_m)
    tick_meters = sorted(set(tick_meters))

    world = np.array([world_point(v) for v in tick_meters], dtype=np.float64)
    screen = project_points(world)
    if not screen or len(screen) != len(tick_meters):
        return

    # Place the ruler just left of the model's projected silhouette instead of
    # at the window edge. Project the bounding-box corners to find its left edge.
    corners = np.array(
        [
            [scene_mins[0], scene_mins[1], scene_mins[2]],
            [scene_mins[0], scene_mins[1], scene_maxs[2]],
            [scene_mins[0], scene_maxs[1], scene_mins[2]],
            [scene_mins[0], scene_maxs[1], scene_maxs[2]],
            [scene_maxs[0], scene_mins[1], scene_mins[2]],
            [scene_maxs[0], scene_mins[1], scene_maxs[2]],
            [scene_maxs[0], scene_maxs[1], scene_mins[2]],
            [scene_maxs[0], scene_maxs[1], scene_maxs[2]],
        ],
        dtype=np.float64,
    )
    corner_pts = project_points(corners)
    try:
        view_width = float(painter.device().width())
    except Exception:
        view_width = 800.0
    label_room = 58.0
    model_left = min((pt.x() for pt in corner_pts), default=label_room + 16.0)
    spine_x = max(label_room, min(model_left - 16.0, view_width - 40.0))

    ys = [pt.y() for pt in screen]
    top_y, bottom_y = min(ys), max(ys)
    painter.setPen(QtGui.QPen(QtGui.QColor(200, 205, 215, 200), 1.6))
    painter.drawLine(QtCore.QPointF(spine_x, top_y), QtCore.QPointF(spine_x, bottom_y))

    base_font = painter.font()
    tick_font = QtGui.QFont(base_font)
    tick_font.setPointSize(max(7, base_font.pointSize() - 1))
    painter.setFont(tick_font)

    def draw_label(y: float, text: str, color: QtGui.QColor) -> None:
        painter.setPen(color)
        painter.drawText(
            QtCore.QRectF(0.0, y - 9.0, spine_x - 12.0, 18.0),
            int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter),
            text,
        )

    for meters, pt in zip(tick_meters, screen):
        y = pt.y()
        if abs(meters - reference_m) < 1e-6:
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 196, 0, 235), 2.0))
            painter.drawLine(QtCore.QPointF(spine_x - 10.0, y), QtCore.QPointF(spine_x + 10.0, y))
            draw_label(y, f"{reference_m:.2f} m (avg)", QtGui.QColor(255, 210, 90))
        else:
            painter.setPen(QtGui.QPen(QtGui.QColor(170, 178, 190, 200), 1.4))
            painter.drawLine(QtCore.QPointF(spine_x - 7.0, y), QtCore.QPointF(spine_x, y))
            draw_label(y, f"{meters:.{decimals}f} m", QtGui.QColor(205, 210, 220))
    # Model total-height callout just above the model's top.
    painter.setPen(QtGui.QColor(120, 220, 140))
    painter.drawText(
        QtCore.QRectF(0.0, max(2.0, top_y - 19.0), spine_x - 12.0, 16.0),
        int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter),
        f"≈ {model_height_m:.2f} m",
    )
    painter.setFont(base_font)


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _preview_debug_log_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "MMDCharacterImporter" / "logs" / "material_preview_debug.log"
    return Path.home() / ".MMDCharacterImporter" / "logs" / "material_preview_debug.log"


# --- Preview renderer selection + GL runtime probe (issue #129) -----------
# On some machines Qt silently creates its OpenGL contexts through a module
# PyOpenGL cannot see (e.g. the opengl32sw.dll software rasterizer picked by
# Qt's GPU heuristics, or an otherwise rejected ICD). Qt's own painting works,
# but every PyOpenGL call then fails with GLError 1282 ("invalid operation",
# first hit at glViewport) and the mesh never renders. The probe below detects
# that mismatch once at startup so the widgets can switch to a pure-QPainter
# CPU renderer instead of erroring every frame, and the renderer mode lets the
# user force a specific backend when the automatic pick is wrong.

PREVIEW_RENDERER_CHOICES = ("auto", "desktop", "software", "cpu")


def preview_renderer_mode() -> str:
    """Requested preview renderer: env MCI_PREVIEW_RENDERER overrides the
    persisted 'preview_renderer' setting; anything unknown means 'auto'."""
    value = str(os.environ.get("MCI_PREVIEW_RENDERER", "") or "").strip().lower()
    if value not in PREVIEW_RENDERER_CHOICES:
        try:
            value = str(
                QtCore.QSettings("MMDCharacterImporter", "PortingTool").value("preview_renderer", "auto") or "auto"
            ).strip().lower()
        except Exception:
            value = "auto"
    return value if value in PREVIEW_RENDERER_CHOICES else "auto"


_RENDERER_MODE = preview_renderer_mode()
_FORCE_CPU_PREVIEW = _RENDERER_MODE == "cpu"
# In forced-CPU mode the preview widgets never touch OpenGL, so they use plain
# QWidget: a QOpenGLWidget would still create a (potentially broken) GL
# context just by existing, which is exactly what "cpu" must avoid.
_PreviewWidgetBase = QtWidgets.QWidget if _FORCE_CPU_PREVIEW else QOpenGLWidget


@dataclass
class GLRuntimeProbe:
    ok: bool
    mode: str
    detail: str
    renderer: str = ""
    vendor: str = ""
    version: str = ""
    software_dll_loaded: bool = False

    def summary(self) -> str:
        state = "ok" if self.ok else "UNAVAILABLE"
        parts = [f"OpenGL preview backend: {state} (renderer mode: {self.mode})"]
        if self.renderer or self.vendor or self.version:
            parts.append(f"GL_RENDERER={self.renderer!r} GL_VENDOR={self.vendor!r} GL_VERSION={self.version!r}")
        if self.software_dll_loaded:
            parts.append("Qt is using the opengl32sw.dll software rasterizer")
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


_GL_RUNTIME_PROBE: GLRuntimeProbe | None = None


def _software_gl_dll_loaded() -> bool:
    """True when Qt loaded its software OpenGL rasterizer (opengl32sw.dll);
    PyOpenGL binds the system opengl32.dll, so the two cannot interoperate."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.kernel32.GetModuleHandleW("opengl32sw.dll"))
    except Exception:
        return False


def probe_gl_runtime() -> GLRuntimeProbe:
    """Create an offscreen Qt OpenGL context and verify PyOpenGL can drive it.

    Must run on the GUI thread after QApplication exists and before the first
    preview paints. The result gates gl_runtime_usable(): a failed probe makes
    every preview widget render through the QPainter CPU fallback instead of
    raising GLError once per frame (issue #129).
    """
    global _GL_RUNTIME_PROBE
    if _FORCE_CPU_PREVIEW:
        _GL_RUNTIME_PROBE = GLRuntimeProbe(
            ok=False, mode=_RENDERER_MODE, detail="CPU renderer forced; OpenGL disabled for previews."
        )
        return _GL_RUNTIME_PROBE
    if GL is None:
        _GL_RUNTIME_PROBE = GLRuntimeProbe(
            ok=False, mode=_RENDERER_MODE, detail="PyOpenGL is not installed.",
            software_dll_loaded=_software_gl_dll_loaded(),
        )
        return _GL_RUNTIME_PROBE
    renderer = vendor = version = ""
    try:
        surface = QtGui.QOffscreenSurface()
        surface.create()
        if not surface.isValid():
            raise RuntimeError("Qt could not create an offscreen surface")
        context = QtGui.QOpenGLContext()
        if not context.create():
            raise RuntimeError("Qt could not create an OpenGL context")
        if not context.makeCurrent(surface):
            raise RuntimeError("Qt could not make the OpenGL context current")
        try:
            # Exercise the exact call that fails on mismatched backends
            # (glViewport was the first reported error in issue #129) and read
            # the identity strings for the diagnostics log.
            def _decode(value: object) -> str:
                if isinstance(value, bytes):
                    return value.decode("utf-8", "replace")
                return str(value) if value else ""

            renderer = _decode(GL.glGetString(GL.GL_RENDERER))
            vendor = _decode(GL.glGetString(GL.GL_VENDOR))
            version = _decode(GL.glGetString(GL.GL_VERSION))
            GL.glViewport(0, 0, 4, 4)
            GL.glClearColor(0.0, 0.0, 0.0, 1.0)
            error = int(GL.glGetError())
            if not renderer:
                raise RuntimeError("PyOpenGL sees no current context (GL_RENDERER is empty)")
            if error:
                raise RuntimeError(f"OpenGL reported error 0x{error:04x} during the self-test")
        finally:
            context.doneCurrent()
    except Exception as exc:
        _GL_RUNTIME_PROBE = GLRuntimeProbe(
            ok=False,
            mode=_RENDERER_MODE,
            detail=f"PyOpenGL cannot drive Qt's OpenGL context: {exc}. Previews use the CPU renderer.",
            renderer=renderer,
            vendor=vendor,
            version=version,
            software_dll_loaded=_software_gl_dll_loaded(),
        )
        return _GL_RUNTIME_PROBE
    _GL_RUNTIME_PROBE = GLRuntimeProbe(
        ok=True,
        mode=_RENDERER_MODE,
        detail="",
        renderer=renderer,
        vendor=vendor,
        version=version,
        software_dll_loaded=_software_gl_dll_loaded(),
    )
    return _GL_RUNTIME_PROBE


def gl_runtime_usable() -> bool:
    """Whether the widgets may issue PyOpenGL calls. True when the probe has
    not run (standalone/tests import this module without the GUI startup)."""
    if _FORCE_CPU_PREVIEW or GL is None:
        return False
    return _GL_RUNTIME_PROBE is None or _GL_RUNTIME_PROBE.ok


def _cpu_preview_budget() -> int:
    try:
        value = int(os.environ.get("MCI_CPU_PREVIEW_BUDGET", "") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 20000


@dataclass
class PreviewMaterial:
    name: str
    diffuse: tuple[float, float, float, float]
    texture_path: Path | None
    index_start: int
    index_count: int
    texture_ref: str = ""
    missing_texture: bool = False


@dataclass
class PreviewBone:
    name: str
    english_name: str
    parent: int
    position: tuple[float, float, float]


@dataclass
class StaticPreviewModel:
    path: Path
    name: str
    english_name: str
    positions: np.ndarray
    normals: np.ndarray
    uvs: np.ndarray
    indices: np.ndarray
    materials: list[PreviewMaterial]
    bones: list[PreviewBone]
    morph_count: int
    texture_count: int
    warnings: list[str]

    @property
    def vertex_count(self) -> int:
        return int(len(self.positions))

    @property
    def triangle_count(self) -> int:
        return int(len(self.indices) // 3)


class _BytesPmxReader(core.PmxReader):
    """PmxReader over an in-memory buffer so bulk sections can be parsed with numpy."""

    def __init__(self, path: Path, data: bytes) -> None:  # noqa: D401 - mirrors PmxReader
        self.path = path
        self.data = data
        self.handle = io.BytesIO(data)


def _parse_vertices_bulk(
    data: bytes, offset: int, vertex_count: int, bone_index_size: int, additional_uvs: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Parse PMX vertex records from a byte buffer.

    Records are variable-size (skinning type varies per vertex), so a single pass
    collects record offsets cheaply, then numpy gathers the fixed 32-byte prefix
    (position, normal, uv) of every record at once.
    """
    prefix_size = 32 + 16 * additional_uvs
    # weight byte + skinning payload + edge-scale float, per weight type
    record_sizes = {
        0: prefix_size + 1 + bone_index_size + 4,
        1: prefix_size + 1 + bone_index_size * 2 + 4 + 4,
        2: prefix_size + 1 + bone_index_size * 4 + 16 + 4,
        3: prefix_size + 1 + bone_index_size * 2 + 4 + 36 + 4,
        4: prefix_size + 1 + bone_index_size * 4 + 16 + 4,
    }
    offsets: list[int] = []
    append = offsets.append
    pos = offset
    try:
        for _ in range(vertex_count):
            append(pos)
            weight_type = data[pos + prefix_size]
            try:
                pos += record_sizes[weight_type]
            except KeyError:
                raise ValueError(f"unsupported PMX vertex weight type: {weight_type}") from None
    except IndexError:
        raise EOFError("unexpected end of PMX while reading vertices") from None
    if pos > len(data):
        raise EOFError("unexpected end of PMX while reading vertices")
    if not vertex_count:
        empty3 = np.zeros((0, 3), dtype=np.float32)
        return empty3, empty3.copy(), np.zeros((0, 2), dtype=np.float32), pos
    byte_view = np.frombuffer(data, dtype=np.uint8)
    gather = np.asarray(offsets, dtype=np.int64)[:, None] + np.arange(32, dtype=np.int64)
    floats = np.ascontiguousarray(byte_view[gather]).view("<f4").reshape(vertex_count, 8)
    positions = np.ascontiguousarray(floats[:, 0:3])
    normals = np.ascontiguousarray(floats[:, 3:6])
    uvs = np.ascontiguousarray(floats[:, 6:8])
    return positions, normals, uvs, pos


def _parse_indices_bulk(data: bytes, offset: int, index_count: int, vertex_index_size: int) -> tuple[np.ndarray, int]:
    dtypes = {1: np.uint8, 2: np.uint16, 4: np.uint32}
    if vertex_index_size not in dtypes:
        raise ValueError(f"invalid PMX index size: {vertex_index_size}")
    end = offset + index_count * vertex_index_size
    if index_count < 0 or end > len(data):
        raise EOFError("unexpected end of PMX while reading indices")
    indices = np.frombuffer(data, dtype=dtypes[vertex_index_size], count=index_count, offset=offset)
    return indices.astype(np.uint32), end


def _texture_files(model_path: Path, max_files: int = 4000) -> list[Path]:
    # Non-recursive scan of the model folder plus one level of subfolders
    # (textures/, tex/, ...), bounded so a PMX sitting in a huge directory
    # does not stall the load with thousands of stat calls.
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".dds"}
    out: list[Path] = []
    subdirs: list[Path] = []
    try:
        for path in model_path.parent.iterdir():
            if path.suffix.lower() in exts and path.is_file():
                out.append(path)
            elif path.is_dir():
                subdirs.append(path)
    except OSError:
        return []
    for base in subdirs:
        if len(out) >= max_files:
            break
        try:
            for path in base.iterdir():
                if path.suffix.lower() in exts and path.is_file():
                    out.append(path)
                    if len(out) >= max_files:
                        break
        except OSError:
            continue
    return sorted(set(out))


def _texture_ref(texture_names: list[str], texture_index: int) -> str:
    if texture_index < 0 or texture_index >= len(texture_names):
        return ""
    return texture_names[texture_index].replace("\\", "/").strip()


def _resolve_texture_ref(model_path: Path, raw: str, lookup: dict[str, Path]) -> Path | None:
    if not raw or raw.startswith("*"):
        return None
    relative = Path(raw)
    for candidate in (model_path.parent / relative, model_path.parent / "textures" / relative.name):
        if candidate.exists():
            return candidate
    return lookup.get(relative.name.lower())


def _resolve_texture_path(model_path: Path, texture_names: list[str], texture_index: int, lookup: dict[str, Path]) -> Path | None:
    return _resolve_texture_ref(model_path, _texture_ref(texture_names, texture_index), lookup)


def _read_materials(reader: core.PmxReader, encoding: str, texture_index_size: int, model_path: Path, texture_names: list[str]) -> list[PreviewMaterial]:
    texture_lookup = {path.name.lower(): path for path in _texture_files(model_path)}
    materials: list[PreviewMaterial] = []
    index_start = 0
    for _ in range(reader.read_i32()):
        name = reader.read_string(encoding)
        reader.read_string(encoding)
        diffuse = (reader.read_f32(), reader.read_f32(), reader.read_f32(), reader.read_f32())
        reader.read_vector_bytes(3)
        reader.read_f32()
        reader.read_vector_bytes(3)
        reader.read_u8()
        reader.read_vector_bytes(4)
        reader.read_f32()
        texture_index = reader.read_index(texture_index_size)
        texture_ref = _texture_ref(texture_names, texture_index)
        texture_path = _resolve_texture_ref(model_path, texture_ref, texture_lookup)
        reader.read_index(texture_index_size)
        reader.read_i8()
        shared_toon = reader.read_i8()
        if shared_toon == 1:
            reader.read_i8()
        else:
            reader.read_index(texture_index_size)
        reader.read_string(encoding)
        index_count = reader.read_i32()
        materials.append(
            PreviewMaterial(
                name=name,
                diffuse=diffuse,
                texture_path=texture_path,
                index_start=index_start,
                index_count=index_count,
                texture_ref=texture_ref,
                missing_texture=bool(texture_ref and not texture_ref.startswith("*") and texture_path is None),
            )
        )
        index_start += index_count
    return materials


def _read_bone(reader: core.PmxReader, encoding: str, bone_index_size: int) -> PreviewBone:
    name = reader.read_string(encoding)
    english_name = reader.read_string(encoding)
    position = (reader.read_f32(), reader.read_f32(), reader.read_f32())
    parent = reader.read_index(bone_index_size)
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
        for _ in range(reader.read_i32()):
            reader.read_index(bone_index_size)
            if reader.read_u8():
                reader.read_vector_bytes(3)
                reader.read_vector_bytes(3)
    return PreviewBone(name, english_name, parent, position)


def load_static_preview_model(model_path: Path) -> StaticPreviewModel:
    """Load a static preview model, dispatching on the file suffix (.pmx/.vrm)."""
    model_path = model_path.resolve()
    if model_path.suffix.lower() == ".vrm":
        return _load_vrm_preview_model(model_path)
    return _load_pmx_preview_model(model_path)


def _load_pmx_preview_model(model_path: Path) -> StaticPreviewModel:
    model_path = model_path.resolve()
    warnings: list[str] = []
    # Read the whole file once: bulk sections (vertices, indices) are parsed with
    # numpy and the remaining sections reuse the buffered PmxReader API without
    # millions of small file reads.
    data = model_path.read_bytes()
    reader = _BytesPmxReader(model_path, data)
    try:
        signature = reader.read_exact(4)
        if signature != b"PMX ":
            raise ValueError(f"not a PMX file: invalid signature {signature!r}")
        reader.read_f32()
        header_size = reader.read_u8()
        encoding = "utf-16-le" if reader.read_u8() == 0 else "utf-8"
        additional_uvs = reader.read_u8()
        vertex_index_size = reader.read_u8()
        texture_index_size = reader.read_u8()
        material_index_size = reader.read_u8()
        bone_index_size = reader.read_u8()
        morph_index_size = reader.read_u8()
        rigid_index_size = reader.read_u8()
        for _ in range(max(0, header_size - 8)):
            reader.read_u8()

        name = reader.read_string(encoding)
        english_name = reader.read_string(encoding)
        reader.read_string(encoding)
        reader.read_string(encoding)

        vertex_count = reader.read_i32()
        if vertex_count < 0:
            raise ValueError(f"invalid PMX vertex count: {vertex_count}")
        if vertex_count > core.MAX_SUPPORTED_PMX_VERTEX_COUNT:
            raise RuntimeError(
                f"PMX vertex count {vertex_count:,} exceeds the supported limit of "
                f"{core.MAX_SUPPORTED_PMX_VERTEX_COUNT:,}. This model is too large for the importer."
            )
        positions, normals, uvs, next_offset = _parse_vertices_bulk(
            data, reader.handle.tell(), vertex_count, bone_index_size, additional_uvs
        )
        reader.handle.seek(next_offset)

        index_count = reader.read_i32()
        indices, next_offset = _parse_indices_bulk(data, reader.handle.tell(), index_count, vertex_index_size)
        reader.handle.seek(next_offset)

        texture_names = [reader.read_string(encoding) for _ in range(reader.read_i32())]
        materials = _read_materials(reader, encoding, texture_index_size, model_path, texture_names)

        bones = [_read_bone(reader, encoding, bone_index_size) for _ in range(reader.read_i32())]
        morph_count = reader.read_i32()
        for _ in range(morph_count):
            core.skip_morph(
                reader,
                encoding,
                vertex_index_size,
                bone_index_size,
                morph_index_size,
                material_index_size,
                rigid_index_size,
            )
    finally:
        reader.close()

    unresolved = sum(1 for material in materials if material.missing_texture)
    if unresolved:
        warnings.append(f"{unresolved} material(s) have no resolved preview texture.")
    return StaticPreviewModel(
        path=model_path,
        name=name,
        english_name=english_name,
        positions=positions,
        normals=normals,
        uvs=uvs,
        indices=indices,
        materials=materials,
        bones=bones,
        morph_count=morph_count,
        texture_count=len(texture_names),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# VRM (glTF 2.0 GLB container) preview loader
# ---------------------------------------------------------------------------

_GLTF_COMPONENT_DTYPES = {
    5120: np.dtype("<i1"),
    5121: np.dtype("<u1"),
    5122: np.dtype("<i2"),
    5123: np.dtype("<u2"),
    5125: np.dtype("<u4"),
    5126: np.dtype("<f4"),
}
_GLTF_TYPE_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_GLTF_NORMALIZED_SCALE = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}
_GLTF_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}


def _parse_glb(data: bytes) -> tuple[dict, bytes]:
    """Split a GLB container into its parsed JSON chunk and binary chunk."""
    if len(data) < 12 or data[:4] != b"glTF":
        raise ValueError(f"not a VRM/GLB file: invalid signature {data[:4]!r}")
    _version, total_length = struct.unpack_from("<II", data, 4)
    end = min(len(data), total_length) if total_length else len(data)
    json_chunk: bytes | None = None
    bin_chunk = b""
    offset = 12
    while offset + 8 <= end:
        chunk_length, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        chunk = data[offset : offset + chunk_length]
        if len(chunk) < chunk_length:
            raise EOFError("unexpected end of VRM/GLB while reading chunks")
        if chunk_type == b"JSON" and json_chunk is None:
            json_chunk = chunk
        elif chunk_type == b"BIN\x00" and not bin_chunk:
            bin_chunk = chunk
        offset += chunk_length
    if json_chunk is None:
        raise ValueError("VRM/GLB file has no JSON chunk")
    return json.loads(json_chunk.decode("utf-8")), bin_chunk


def _read_gltf_accessor(gltf: dict, bin_chunk: bytes, accessor_index: int) -> np.ndarray:
    """Read one accessor as a (count, components) array, honoring byteStride."""
    accessor = gltf["accessors"][int(accessor_index)]
    component_type = int(accessor["componentType"])
    dtype = _GLTF_COMPONENT_DTYPES[component_type]
    components = _GLTF_TYPE_COMPONENTS[str(accessor["type"])]
    count = int(accessor["count"])
    if count <= 0:
        return np.zeros((0, components), dtype=np.float32)
    view_index = accessor.get("bufferView")
    if view_index is None:
        array = np.zeros((count, components), dtype=dtype)
    else:
        view = gltf["bufferViews"][int(view_index)]
        start = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
        tight = dtype.itemsize * components
        stride = int(view.get("byteStride") or tight)
        if stride == tight:
            array = np.frombuffer(bin_chunk, dtype=dtype, count=count * components, offset=start).reshape(count, components)
        else:
            span = stride * (count - 1) + tight
            raw = np.frombuffer(bin_chunk, dtype=np.uint8, count=span, offset=start)
            strided = np.lib.stride_tricks.as_strided(raw, shape=(count, tight), strides=(stride, 1))
            array = np.ascontiguousarray(strided).view(dtype).reshape(count, components)
    if accessor.get("normalized") and component_type in _GLTF_NORMALIZED_SCALE:
        array = np.maximum(array.astype(np.float32) / _GLTF_NORMALIZED_SCALE[component_type], -1.0)
    return array


def _gltf_quaternion_matrix(quaternion: list) -> np.ndarray:
    x, y, z, w = (float(quaternion[0]), float(quaternion[1]), float(quaternion[2]), float(quaternion[3]))
    norm = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    matrix = np.eye(4, dtype=np.float64)
    matrix[0, 0] = 1.0 - 2.0 * (y * y + z * z)
    matrix[0, 1] = 2.0 * (x * y - z * w)
    matrix[0, 2] = 2.0 * (x * z + y * w)
    matrix[1, 0] = 2.0 * (x * y + z * w)
    matrix[1, 1] = 1.0 - 2.0 * (x * x + z * z)
    matrix[1, 2] = 2.0 * (y * z - x * w)
    matrix[2, 0] = 2.0 * (x * z - y * w)
    matrix[2, 1] = 2.0 * (y * z + x * w)
    matrix[2, 2] = 1.0 - 2.0 * (x * x + y * y)
    return matrix


def _gltf_local_matrix(node: dict) -> np.ndarray:
    raw = node.get("matrix")
    if isinstance(raw, list) and len(raw) == 16:
        # glTF matrices are column-major.
        return np.array(raw, dtype=np.float64).reshape(4, 4).T
    matrix = np.eye(4, dtype=np.float64)
    scale = node.get("scale")
    if isinstance(scale, list) and len(scale) == 3:
        matrix = np.diag([float(scale[0]), float(scale[1]), float(scale[2]), 1.0])
    rotation = node.get("rotation")
    if isinstance(rotation, list) and len(rotation) == 4:
        matrix = _gltf_quaternion_matrix(rotation) @ matrix
    translation = node.get("translation")
    if isinstance(translation, list) and len(translation) == 3:
        matrix[:3, 3] += np.array(translation[:3], dtype=np.float64)
    return matrix


def _gltf_node_parents(nodes: list) -> dict[int, int]:
    parents: dict[int, int] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        for child in node.get("children") or []:
            if isinstance(child, int) and 0 <= child < len(nodes):
                parents.setdefault(child, index)
    return parents


def _gltf_global_matrices(nodes: list, parents: dict[int, int]) -> list[np.ndarray]:
    globals_: list[np.ndarray | None] = [None] * len(nodes)

    def resolve(index: int, visiting: frozenset[int]) -> np.ndarray:
        cached = globals_[index]
        if cached is not None:
            return cached
        local = _gltf_local_matrix(nodes[index]) if isinstance(nodes[index], dict) else np.eye(4, dtype=np.float64)
        parent = parents.get(index)
        if parent is None or parent in visiting:
            matrix = local
        else:
            matrix = resolve(parent, visiting | {index}) @ local
        globals_[index] = matrix
        return matrix

    return [resolve(index, frozenset({index})) for index in range(len(nodes))]


def _vrm_preview_cache_dir(model_path: Path) -> Path:
    try:
        stat = model_path.stat()
        token = f"{model_path}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        token = str(model_path)
    digest = hashlib.sha1(token.encode("utf-8", "replace")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"mci_vrm_preview_{digest}"


def _extract_vrm_images(gltf: dict, bin_chunk: bytes, model_path: Path, warnings: list[str]) -> dict[int, Path]:
    """Write embedded glTF images to a per-model temp cache and return file paths.

    The existing texture decode/upload path works on plain files, so embedded
    PNG/JPEG payloads are materialized once per (path, mtime, size) and reused.
    """
    images = gltf.get("images") or []
    if not images:
        return {}
    cache_dir = _vrm_preview_cache_dir(model_path)
    resolved: dict[int, Path] = {}
    failures = 0
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        suffix = _GLTF_IMAGE_EXTENSIONS.get(str(image.get("mimeType") or "").lower(), ".png")
        blob: bytes | None = None
        uri = image.get("uri")
        if isinstance(uri, str) and uri:
            if uri.startswith("data:"):
                header, _, payload = uri.partition(",")
                try:
                    blob = base64.b64decode(payload)
                except Exception:
                    blob = None
                mime = header[5:].split(";", 1)[0].lower()
                suffix = _GLTF_IMAGE_EXTENSIONS.get(mime, suffix)
            else:
                candidate = model_path.parent / unquote(uri.replace("\\", "/"))
                if candidate.exists():
                    resolved[index] = candidate
                continue
        if blob is None:
            view_index = image.get("bufferView")
            if view_index is None:
                continue
            try:
                view = gltf["bufferViews"][int(view_index)]
                start = int(view.get("byteOffset", 0))
                blob = bytes(bin_chunk[start : start + int(view.get("byteLength", 0))])
            except Exception:
                blob = None
        if not blob:
            continue
        # Trust the payload over a missing/incorrect mimeType.
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            suffix = ".png"
        elif blob[:3] == b"\xff\xd8\xff":
            suffix = ".jpg"
        target = cache_dir / f"image_{index:03d}{suffix}"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.stat().st_size != len(blob):
                target.write_bytes(blob)
            resolved[index] = target
        except OSError:
            failures += 1
    if failures:
        warnings.append(f"{failures} embedded texture(s) could not be written to the preview cache.")
    return resolved


def _vrm_material_image_index(gltf: dict, material_index: int) -> int:
    """Resolve a glTF material to its base color source image index (or -1)."""
    materials = gltf.get("materials") or []
    material = materials[material_index] if 0 <= material_index < len(materials) else {}
    if not isinstance(material, dict):
        material = {}
    base = (material.get("pbrMetallicRoughness") or {}).get("baseColorTexture") or {}
    texture_index = base.get("index")
    if texture_index is None:
        # VRM 0.x MToon materials carry the main texture in materialProperties,
        # aligned with the materials array.
        properties = ((gltf.get("extensions") or {}).get("VRM") or {}).get("materialProperties") or []
        if 0 <= material_index < len(properties) and isinstance(properties[material_index], dict):
            texture_index = (properties[material_index].get("textureProperties") or {}).get("_MainTex")
    if texture_index is None:
        return -1
    textures = gltf.get("textures") or []
    if not (0 <= int(texture_index) < len(textures)):
        return -1
    source = textures[int(texture_index)].get("source") if isinstance(textures[int(texture_index)], dict) else None
    return int(source) if source is not None else -1


def _vrm_material_color(material: dict) -> tuple[float, float, float, float]:
    factor = (material.get("pbrMetallicRoughness") or {}).get("baseColorFactor")
    if isinstance(factor, list) and len(factor) >= 4:
        try:
            return (float(factor[0]), float(factor[1]), float(factor[2]), float(factor[3]))
        except (TypeError, ValueError):
            pass
    return (1.0, 1.0, 1.0, 1.0)


def _vrm_morph_count(gltf: dict) -> int:
    extensions = gltf.get("extensions") or {}
    groups = ((extensions.get("VRM") or {}).get("blendShapeMaster") or {}).get("blendShapeGroups")
    if isinstance(groups, list) and groups:
        return len(groups)
    expressions = (extensions.get("VRMC_vrm") or {}).get("expressions")
    if isinstance(expressions, dict):
        total = sum(len(value) for value in (expressions.get("preset"), expressions.get("custom")) if isinstance(value, dict))
        if total:
            return total
    total = 0
    for mesh in gltf.get("meshes") or []:
        if not isinstance(mesh, dict):
            continue
        counts = [len(primitive.get("targets") or []) for primitive in mesh.get("primitives") or [] if isinstance(primitive, dict)]
        if counts:
            total += max(counts)
    return total


def _computed_vertex_normals(positions: np.ndarray, indices: np.ndarray) -> np.ndarray:
    normals = np.zeros_like(positions, dtype=np.float32)
    if len(indices) >= 3 and len(positions):
        triangles = indices[: (len(indices) // 3) * 3].reshape(-1, 3).astype(np.int64)
        edge1 = positions[triangles[:, 1]] - positions[triangles[:, 0]]
        edge2 = positions[triangles[:, 2]] - positions[triangles[:, 0]]
        face_normals = np.cross(edge1, edge2)
        for column in range(3):
            np.add.at(normals, triangles[:, column], face_normals)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = (normals / np.maximum(lengths, 1e-8)).astype(np.float32)
    return normals


def _load_vrm_preview_model(model_path: Path) -> StaticPreviewModel:
    """Parse a VRM (glTF 2.0 GLB) into the same structure as the PMX loader."""
    warnings: list[str] = []
    gltf, bin_chunk = _parse_glb(model_path.read_bytes())
    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []
    parents = _gltf_node_parents(nodes)
    global_matrices = _gltf_global_matrices(nodes, parents)
    image_paths = _extract_vrm_images(gltf, bin_chunk, model_path, warnings)

    positions_parts: list[np.ndarray] = []
    normals_parts: list[np.ndarray] = []
    uvs_parts: list[np.ndarray] = []
    material_buckets: dict[int, list[np.ndarray]] = {}
    vertex_offset = 0
    identity = np.eye(4, dtype=np.float64)
    for node_index, node in enumerate(nodes):
        mesh_index = node.get("mesh") if isinstance(node, dict) else None
        if mesh_index is None or not (0 <= int(mesh_index) < len(meshes)):
            continue
        mesh = meshes[int(mesh_index)]
        if not isinstance(mesh, dict):
            continue
        # Skinned vertices are stored in bind/model space; per the glTF spec the
        # node transform must be ignored for skinned meshes.
        matrix = identity if node.get("skin") is not None else global_matrices[node_index]
        apply_transform = not np.allclose(matrix, identity)
        for primitive in mesh.get("primitives") or []:
            if not isinstance(primitive, dict) or int(primitive.get("mode", 4)) != 4:
                continue
            attributes = primitive.get("attributes") or {}
            if "POSITION" not in attributes:
                continue
            positions = _read_gltf_accessor(gltf, bin_chunk, attributes["POSITION"]).astype(np.float32)
            if positions.shape[1] < 3:
                continue
            positions = np.ascontiguousarray(positions[:, :3])
            count = len(positions)
            normals = None
            if "NORMAL" in attributes:
                raw_normals = _read_gltf_accessor(gltf, bin_chunk, attributes["NORMAL"]).astype(np.float32)
                if raw_normals.shape[1] >= 3 and len(raw_normals) == count:
                    normals = np.ascontiguousarray(raw_normals[:, :3])
            if "TEXCOORD_0" in attributes:
                raw_uvs = _read_gltf_accessor(gltf, bin_chunk, attributes["TEXCOORD_0"]).astype(np.float32)
                uvs = np.ascontiguousarray(raw_uvs[:, :2]) if raw_uvs.shape[1] >= 2 and len(raw_uvs) == count else np.zeros((count, 2), dtype=np.float32)
            else:
                uvs = np.zeros((count, 2), dtype=np.float32)
            indices_accessor = primitive.get("indices")
            if indices_accessor is None:
                indices = np.arange(count, dtype=np.uint32)
            else:
                indices = _read_gltf_accessor(gltf, bin_chunk, indices_accessor).reshape(-1).astype(np.uint32)
            if apply_transform:
                linear = matrix[:3, :3].T.astype(np.float32)
                positions = positions @ linear + matrix[:3, 3].astype(np.float32)
                if normals is not None:
                    normals = normals @ linear
            if normals is None:
                normals = _computed_vertex_normals(positions, indices)
            material_index = primitive.get("material")
            bucket = int(material_index) if isinstance(material_index, int) else -1
            material_buckets.setdefault(bucket, []).append(indices + np.uint32(vertex_offset))
            positions_parts.append(positions)
            normals_parts.append(normals)
            uvs_parts.append(uvs)
            vertex_offset += count

    if positions_parts:
        all_positions = np.ascontiguousarray(np.concatenate(positions_parts, axis=0), dtype=np.float32)
        all_normals = np.ascontiguousarray(np.concatenate(normals_parts, axis=0), dtype=np.float32)
        all_uvs = np.ascontiguousarray(np.concatenate(uvs_parts, axis=0), dtype=np.float32)
    else:
        all_positions = np.zeros((0, 3), dtype=np.float32)
        all_normals = np.zeros((0, 3), dtype=np.float32)
        all_uvs = np.zeros((0, 2), dtype=np.float32)
    if len(all_positions) > core.MAX_SUPPORTED_PMX_VERTEX_COUNT:
        raise RuntimeError(
            f"VRM vertex count {len(all_positions):,} exceeds the supported limit of "
            f"{core.MAX_SUPPORTED_PMX_VERTEX_COUNT:,}. This model is too large for the importer."
        )
    # The PMX preview convention is left-handed Y-up, facing -Z, model's left
    # on +X. VRM 0.x assets face -Z with the left side on -X (mirror X to
    # match); VRM 1.0 / plain glTF assets face +Z with the left side on +X
    # (mirror Z to match). Either way the default Front view shows the face.
    mirror_axis = 0 if "VRM" in (gltf.get("extensions") or {}) else 2
    all_positions[:, mirror_axis] *= -1.0
    all_normals[:, mirror_axis] *= -1.0

    # One contiguous index range per glTF material (mirrors PMX material spans).
    gltf_materials = gltf.get("materials") or []
    gltf_images = gltf.get("images") or []
    materials: list[PreviewMaterial] = []
    indices_parts: list[np.ndarray] = []
    index_start = 0
    for bucket in sorted(material_buckets, key=lambda value: (value < 0, value)):
        chunks = material_buckets[bucket]
        merged = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
        material = gltf_materials[bucket] if 0 <= bucket < len(gltf_materials) and isinstance(gltf_materials[bucket], dict) else {}
        name = str(material.get("name") or (f"material_{bucket}" if bucket >= 0 else "material"))
        image_index = _vrm_material_image_index(gltf, bucket) if bucket >= 0 else -1
        texture_path = image_paths.get(image_index) if image_index >= 0 else None
        texture_ref = ""
        if image_index >= 0 and 0 <= image_index < len(gltf_images) and isinstance(gltf_images[image_index], dict):
            texture_ref = str(gltf_images[image_index].get("name") or gltf_images[image_index].get("uri") or f"image_{image_index}")
        materials.append(
            PreviewMaterial(
                name=name,
                diffuse=_vrm_material_color(material),
                texture_path=texture_path,
                index_start=index_start,
                index_count=int(len(merged)),
                texture_ref=texture_ref,
                missing_texture=bool(image_index >= 0 and texture_path is None),
            )
        )
        indices_parts.append(merged)
        index_start += int(len(merged))
    all_indices = np.concatenate(indices_parts) if indices_parts else np.zeros(0, dtype=np.uint32)

    # Skeleton overlay: skin joints in encounter order, parented within joints.
    joints: list[int] = []
    seen: set[int] = set()
    for skin in gltf.get("skins") or []:
        if not isinstance(skin, dict):
            continue
        for joint in skin.get("joints") or []:
            if isinstance(joint, int) and 0 <= joint < len(nodes) and joint not in seen:
                seen.add(joint)
                joints.append(joint)
    node_to_bone = {node_index: bone_index for bone_index, node_index in enumerate(joints)}
    bones: list[PreviewBone] = []
    for node_index in joints:
        matrix = global_matrices[node_index]
        head = [float(matrix[0, 3]), float(matrix[1, 3]), float(matrix[2, 3])]
        head[mirror_axis] = -head[mirror_axis]
        position = (head[0], head[1], head[2])
        parent = parents.get(node_index)
        while parent is not None and parent not in node_to_bone:
            parent = parents.get(parent)
        node = nodes[node_index] if isinstance(nodes[node_index], dict) else {}
        name = str(node.get("name") or f"node_{node_index}")
        bones.append(PreviewBone(name=name, english_name=name, parent=node_to_bone[parent] if parent is not None else -1, position=position))

    extensions = gltf.get("extensions") or {}
    meta0 = (extensions.get("VRM") or {}).get("meta") or {}
    meta1 = (extensions.get("VRMC_vrm") or {}).get("meta") or {}
    model_name = str(meta0.get("title") or meta1.get("name") or model_path.stem)

    unresolved = sum(1 for material in materials if material.missing_texture)
    if unresolved:
        warnings.append(f"{unresolved} material(s) have no resolved preview texture.")
    return StaticPreviewModel(
        path=model_path,
        name=model_name,
        english_name=model_name,
        positions=all_positions,
        normals=all_normals,
        uvs=all_uvs,
        indices=all_indices,
        materials=materials,
        bones=bones,
        morph_count=_vrm_morph_count(gltf),
        texture_count=len(gltf_images),
        warnings=warnings,
    )


def _translation_matrix(v: np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, 3] = v[:3]
    return matrix


def _rotation_x_matrix(angle: float) -> np.ndarray:
    c, s = float(math.cos(angle)), float(math.sin(angle))
    matrix = np.eye(4, dtype=np.float64)
    matrix[1, 1] = c
    matrix[1, 2] = -s
    matrix[2, 1] = s
    matrix[2, 2] = c
    return matrix


def _rotation_z_matrix(angle: float) -> np.ndarray:
    c, s = float(math.cos(angle)), float(math.sin(angle))
    matrix = np.eye(4, dtype=np.float64)
    matrix[0, 0] = c
    matrix[0, 1] = -s
    matrix[1, 0] = s
    matrix[1, 1] = c
    return matrix


def _upright_orbit_view_matrix(scene_center: np.ndarray, azimuth: float, elevation: float) -> np.ndarray:
    # MMD/Source characters are upright on Z. Rotate azimuth around Z so left/right
    # drag orbits around the head-to-feet axis instead of tilting around Y.
    return _rotation_x_matrix(-math.pi * 0.5 + elevation) @ _rotation_z_matrix(azimuth) @ _translation_matrix(-scene_center)


def _scalar_float(value: object, default: float = 0.0) -> float:
    try:
        array = np.asarray(value, dtype=np.float64)
        if array.size:
            return float(array.reshape(-1)[0])
    except Exception:
        pass
    try:
        return float(value)  # type: ignore[arg-type]
    except Exception:
        return float(default)


class StaticModelPreviewWidget(_PreviewWidgetBase):
    statsChanged = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.model: StaticPreviewModel | None = None
        self._scene_center = np.zeros(3, dtype=np.float64)
        self._scene_extent = 1.0
        self._scene_mins = np.zeros(3, dtype=np.float64)
        self._scene_maxs = np.ones(3, dtype=np.float64)
        self._view_yaw = math.pi
        self._view_pitch = 0.0
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self._last_mouse_pos: QtCore.QPoint | None = None
        self._show_bones = True
        self._show_bone_names = False
        self._show_wireframe = False
        self._hide_missing_texture_materials = False
        self._texture_ids: dict[Path, int] = {}
        self._texture_images: dict[Path, QtGui.QImage] = {}
        self._gl_ready = False
        self._gl_error = ""
        self._overlay_error = ""
        self._last_stats_text: str | None = None
        self._vertex_data: np.ndarray | None = None
        # CPU (QPainter) fallback renderer state (issue #129).
        self._gl_disabled = False
        self._gl_failures = 0
        self._cpu_face_cache: dict[str, object] | None = None
        self._cpu_tint_cache: dict[Path, tuple[float, float, float]] = {}
        self._drag_active = False
        self._last_wheel_time = 0.0
        self._height_meters_per_unit = 0.0
        self._height_up_axis = 1
        self._height_reference_m = HEIGHT_REFERENCE_METERS
        self.setMinimumSize(520, 340)
        self.setMouseTracking(True)

    def set_height_reference(self, meters_per_unit: float, up_axis: int = 1, reference_m: float = HEIGHT_REFERENCE_METERS) -> None:
        self._height_meters_per_unit = max(0.0, float(meters_per_unit))
        self._height_up_axis = int(up_axis)
        self._height_reference_m = float(reference_m)
        self.update()

    def load_model(self, model_path: Path) -> StaticPreviewModel:
        self.model = load_static_preview_model(model_path)
        self._prepare_scene()
        self.reset_front_view()
        self._release_textures()
        self._decode_texture_images()
        self._gl_ready = False
        self._gl_error = ""
        self._overlay_error = ""
        self._cpu_face_cache = None
        self._cpu_tint_cache = {}
        self.update()
        self._emit_stats()
        return self.model

    def clear_model(self) -> None:
        self.model = None
        self._release_textures()
        self._texture_images = {}
        self._gl_ready = False
        self._gl_error = ""
        self._overlay_error = ""
        self._cpu_face_cache = None
        self._cpu_tint_cache = {}
        self.update()
        self._emit_stats()

    def _decode_texture_images(self) -> None:
        # Decode texture images at load time so the first paintGL only does the
        # (cheap) GL upload instead of decoding every image inside the paint path.
        self._texture_images = {}
        if not self.model:
            return
        for material in self.model.materials:
            path = material.texture_path
            if not path or path in self._texture_images or not path.exists():
                continue
            image = QtGui.QImage(str(path))
            if not image.isNull():
                self._texture_images[path] = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)

    def _delete_gl_textures(self) -> None:
        """Delete GL texture objects; the GL context must be current."""
        ids = [texture_id for texture_id in self._texture_ids.values() if texture_id]
        self._texture_ids = {}
        if ids and GL:
            try:
                GL.glDeleteTextures(ids)
            except Exception:
                pass

    def _release_textures(self) -> None:
        if not self._texture_ids:
            return
        context = self.context()
        if GL and context is not None and context.isValid():
            try:
                self.makeCurrent()
                self._delete_gl_textures()
            finally:
                self.doneCurrent()
        else:
            self._texture_ids = {}

    def set_bones_visible(self, visible: bool) -> None:
        self._show_bones = bool(visible)
        self.update()

    def set_bone_names_visible(self, visible: bool) -> None:
        self._show_bone_names = bool(visible)
        self.update()

    def set_wireframe_visible(self, visible: bool) -> None:
        self._show_wireframe = bool(visible)
        self.update()

    def set_hide_missing_texture_materials(self, hidden: bool) -> None:
        next_value = bool(hidden)
        if next_value == self._hide_missing_texture_materials:
            return
        self._hide_missing_texture_materials = next_value
        self.update()
        self._emit_stats()

    def reset_front_view(self, *_args: object) -> None:
        self._view_yaw = math.pi
        self._view_pitch = 0.0
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self.update()

    def _prepare_scene(self) -> None:
        if not self.model or len(self.model.positions) == 0:
            return
        positions = self.model.positions.astype(np.float64)
        self._scene_mins = positions.min(axis=0)
        self._scene_maxs = positions.max(axis=0)
        self._scene_center = (self._scene_mins + self._scene_maxs) * 0.5
        self._scene_extent = max(1.0, float(np.max(self._scene_maxs - self._scene_mins)))
        self._vertex_data = np.zeros((len(self.model.positions), 8), dtype=np.float32)
        self._vertex_data[:, 0:3] = self.model.positions
        self._vertex_data[:, 3:6] = self.model.normals
        self._vertex_data[:, 6:8] = self.model.uvs

    def initializeGL(self) -> None:
        context = self.context()
        if context is not None:
            # Free GPU textures when the context goes away (widget teardown or
            # context recreation while docking/reparenting).
            context.aboutToBeDestroyed.connect(self._on_context_about_to_be_destroyed, QtCore.Qt.ConnectionType.DirectConnection)
        # A fresh context invalidates previously created texture names.
        self._texture_ids = {}
        self._gl_ready = False
        if GL and self._gl_backend_active():
            # Guard these initial GL calls. initializeGL is normally invoked with
            # the context already current, but under some drivers / on context
            # recreation during docking/reparenting the context is not yet current
            # here, and an unguarded GL call raises GLError 1282 (invalid
            # operation) which crashes the whole app. Ensure the context is
            # current and swallow transient failures, matching how paintGL and
            # the texture-delete paths already protect their GL calls.
            try:
                self.makeCurrent()
            except Exception:
                pass
            try:
                GL.glClearColor(0.08, 0.09, 0.10, 1.0)
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glDisable(GL.GL_CULL_FACE)
            except Exception as exc:
                # Defer real initialization to the first paintGL, which runs with
                # a guaranteed-current context and is already exception-guarded.
                self._gl_error = str(exc)

    def _on_context_about_to_be_destroyed(self) -> None:
        try:
            self.makeCurrent()
            self._delete_gl_textures()
        except Exception:
            self._texture_ids = {}
        finally:
            try:
                self.doneCurrent()
            except Exception:
                pass
        self._gl_ready = False

    def _gl_backend_active(self) -> bool:
        """True while the widget may issue PyOpenGL calls: PyOpenGL imported,
        the startup probe passed, no local failure latch, and the widget is
        actually GL-backed (forced-CPU mode uses a plain QWidget base)."""
        return GL is not None and gl_runtime_usable() and not self._gl_disabled and isinstance(self, QOpenGLWidget)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        if isinstance(self, QOpenGLWidget):
            super().paintEvent(event)  # drives paintGL
            return
        self._render_frame()

    def paintGL(self) -> None:
        self._render_frame()

    def _render_frame(self) -> None:
        gl_active = self._gl_backend_active()
        if gl_active:
            try:
                GL.glViewport(0, 0, *self._gl_viewport_size())
                GL.glClearColor(0.08, 0.09, 0.10, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
                if self.model:
                    self._draw_mesh_gl()
                # Do not latch a transient failure forever: a successful frame
                # clears any previous draw error.
                self._gl_error = ""
                self._gl_failures = 0
            except Exception as exc:
                self._gl_error = str(exc)
                self._gl_failures += 1
                if self._gl_failures >= 2:
                    # PyOpenGL cannot drive this Qt context (issue #129): stop
                    # issuing GL calls and render on the CPU from now on.
                    self._gl_disabled = True

        painter = QtGui.QPainter(self)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            if not self.model:
                painter.fillRect(self.rect(), QtGui.QColor(22, 24, 27))
                painter.setPen(QtGui.QColor(230, 230, 230))
                painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Select a PMX/VRM model to preview.")
            elif not gl_active or self._gl_error:
                # CPU fallback: flat-shaded QPainter mesh so the preview stays
                # usable when PyOpenGL cannot render (issue #129).
                painter.fillRect(self.rect(), QtGui.QColor(22, 24, 27))
                try:
                    self._draw_mesh_painter(painter)
                except Exception:
                    painter.setPen(QtGui.QColor(255, 210, 120))
                    painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "OpenGL preview is unavailable in this context.")
            try:
                if self.model and (self._show_bones or self._show_bone_names):
                    self._draw_skeleton(painter)
                self._draw_overlay(painter)
                self._overlay_error = ""
            except Exception as exc:
                # Keep QPainter overlay failures separate from GL backend errors
                # so a one-frame overlay glitch does not blank the rendered mesh.
                self._overlay_error = str(exc)
                painter.setPen(QtGui.QColor(255, 210, 120))
                painter.drawText(12, max(64, self.height() - 18), "Preview overlay error; see status text.")
        finally:
            painter.end()
        self._emit_stats()

    def _gl_viewport_size(self) -> tuple[int, int]:
        dpr = max(1.0, float(self.devicePixelRatioF()))
        return max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr)))

    # --- CPU (QPainter) fallback renderer (issue #129) --------------------

    def _cpu_interactive(self) -> bool:
        return self._drag_active or (time.monotonic() - self._last_wheel_time) < 0.25

    def _cpu_triangle_budget(self) -> int:
        budget = _cpu_preview_budget()
        # Rotating/zooming redraws every event; drop detail to stay responsive
        # and repaint at full quality once the interaction settles.
        return max(1500, budget // 4) if self._cpu_interactive() else budget

    def _cpu_material_tint(self, material: PreviewMaterial) -> tuple[float, float, float]:
        """Average texture color, so the CPU preview keeps hair/cloth hues even
        though it cannot texture-map. Cached per texture path."""
        path = material.texture_path
        if not path:
            return (1.0, 1.0, 1.0)
        cached = self._cpu_tint_cache.get(path)
        if cached is not None:
            return cached
        tint = (1.0, 1.0, 1.0)
        image = self._texture_images.get(path)
        if image is None and path.exists():
            image = QtGui.QImage(str(path))
            image = None if image.isNull() else image
        if image is not None and not image.isNull():
            try:
                mean = image.scaled(
                    1,
                    1,
                    QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                ).pixelColor(0, 0)
                tint = (mean.redF(), mean.greenF(), mean.blueF())
            except Exception:
                tint = (1.0, 1.0, 1.0)
        self._cpu_tint_cache[path] = tint
        return tint

    def _painter_face_arrays(self) -> dict[str, object]:
        """Per-model face arrays for the CPU renderer: (F,3) vertex indices,
        (F,4) flat colors (material diffuse x mean texture color), (F,3) unit
        face normals in model space. Rebuilt when the model or the
        hide-missing-texture filter changes."""
        key = (id(self.model), self._hide_missing_texture_materials)
        cached = self._cpu_face_cache
        if cached is not None and cached.get("key") == key:
            return cached
        tri_blocks: list[np.ndarray] = []
        color_blocks: list[np.ndarray] = []
        model = self.model
        if model is not None and len(model.indices) >= 3:
            indices = model.indices
            for material in model.materials:
                if self._hide_missing_texture_materials and material.missing_texture:
                    continue
                start = int(material.index_start)
                count = int(material.index_count)
                if count <= 0 or start >= len(indices):
                    continue
                stop = min(len(indices), start + count)
                block = np.asarray(indices[start:stop], dtype=np.int64)
                block = block[: (len(block) // 3) * 3].reshape(-1, 3)
                if not len(block):
                    continue
                tint = self._cpu_material_tint(material)
                diffuse = material.diffuse
                color = np.array(
                    [
                        max(0.0, min(1.0, float(diffuse[0]) * tint[0])),
                        max(0.0, min(1.0, float(diffuse[1]) * tint[1])),
                        max(0.0, min(1.0, float(diffuse[2]) * tint[2])),
                        max(0.25, min(1.0, float(diffuse[3]))),
                    ],
                    dtype=np.float32,
                )
                tri_blocks.append(block)
                color_blocks.append(np.repeat(color[None, :], len(block), axis=0))
        if tri_blocks:
            tri = np.vstack(tri_blocks)
            colors = np.vstack(color_blocks)
            positions = model.positions.astype(np.float64)
            edge1 = positions[tri[:, 1]] - positions[tri[:, 0]]
            edge2 = positions[tri[:, 2]] - positions[tri[:, 0]]
            normals = np.cross(edge1, edge2)
            lengths = np.linalg.norm(normals, axis=1)
            normals /= np.maximum(lengths, 1e-12)[:, None]
        else:
            tri = np.zeros((0, 3), dtype=np.int64)
            colors = np.zeros((0, 4), dtype=np.float32)
            normals = np.zeros((0, 3), dtype=np.float64)
        cache = {"key": key, "tri": tri, "colors": colors, "normals": normals}
        self._cpu_face_cache = cache
        return cache

    def _draw_mesh_painter(self, painter: QtGui.QPainter) -> None:
        model = self.model
        if model is None or len(model.positions) == 0:
            return
        faces = self._painter_face_arrays()
        tri = faces["tri"]
        if not len(tri):
            return
        projection, view = self._projection_view_matrices()
        positions = model.positions.astype(np.float64)
        homogeneous = np.concatenate([positions, np.ones((len(positions), 1), dtype=np.float64)], axis=1)
        clip = ((projection @ view) @ homogeneous.T).T
        safe_w = np.where(np.abs(clip[:, 3]) <= 1e-8, 1.0, clip[:, 3])
        ndc = clip[:, :3] / safe_w[:, None]
        sx = self.width() * (0.5 + ndc[:, 0] * 0.5)
        sy = self.height() * (0.5 - ndc[:, 1] * 0.5)
        depth = ndc[:, 2]

        face_depth = (depth[tri[:, 0]] + depth[tri[:, 1]] + depth[tri[:, 2]]) / 3.0
        x0, y0 = sx[tri[:, 0]], sy[tri[:, 0]]
        x1, y1 = sx[tri[:, 1]], sy[tri[:, 1]]
        x2, y2 = sx[tri[:, 2]], sy[tri[:, 2]]
        area = np.abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))
        budget = self._cpu_triangle_budget()
        if len(tri) > budget:
            # Keep the largest on-screen triangles: the silhouette and big
            # surfaces survive, sub-pixel detail is dropped.
            keep = np.argpartition(area, len(area) - budget)[-budget:]
        else:
            keep = np.arange(len(tri))
        order = keep[np.argsort(face_depth[keep])[::-1]]  # far-to-near

        # Headlight shading like the GL path: 0.38 ambient + two-sided diffuse.
        rotation = view[:3, :3]
        view_normals = faces["normals"] @ rotation.T
        shade = np.clip(0.38 + 0.85 * np.abs(view_normals[:, 2]), 0.0, 1.0)
        colors = faces["colors"]
        shade_q = np.round(shade * 15.0) / 15.0  # quantize to reuse brushes
        rgb = np.clip(colors[:, :3] * shade_q[:, None] * 255.0, 0.0, 255.0).astype(np.int32)
        alpha = np.clip(colors[:, 3] * 255.0, 0.0, 255.0).astype(np.int32)

        xs = sx.tolist()
        ys = sy.tolist()
        tri_list = tri.tolist()
        point = QtCore.QPointF
        polygon_cls = QtGui.QPolygonF
        painter.save()
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            wireframe = self._show_wireframe
            if wireframe:
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            else:
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
            last_key: tuple[int, int, int, int] | None = None
            for face_index in order.tolist():
                ia, ib, ic = tri_list[face_index]
                color_key = (rgb[face_index, 0], rgb[face_index, 1], rgb[face_index, 2], alpha[face_index])
                if color_key != last_key:
                    last_key = color_key
                    color = QtGui.QColor(int(color_key[0]), int(color_key[1]), int(color_key[2]), int(color_key[3]))
                    if wireframe:
                        painter.setPen(QtGui.QPen(color, 1.0))
                    else:
                        painter.setBrush(color)
                painter.drawConvexPolygon(
                    polygon_cls([point(xs[ia], ys[ia]), point(xs[ib], ys[ib]), point(xs[ic], ys[ic])])
                )
        finally:
            painter.restore()
        if self._cpu_interactive():
            # Repaint at full quality shortly after the interaction settles.
            QtCore.QTimer.singleShot(300, self.update)

    def _ensure_textures(self) -> None:
        if self._gl_ready or not GL or not self.model:
            return
        # The GL context is current here (called from paintGL), so stale texture
        # objects can be deleted directly before new ones are generated.
        self._delete_gl_textures()
        for material in self.model.materials:
            if material.texture_path and material.texture_path not in self._texture_ids:
                texture_id = self._create_texture(material.texture_path)
                if texture_id:
                    self._texture_ids[material.texture_path] = texture_id
        # Uploaded to the GPU; drop the CPU-side decoded copies.
        self._texture_images = {}
        self._gl_ready = True

    def _create_texture(self, path: Path) -> int:
        image = self._texture_images.get(path)
        if image is None:
            # Fallback for textures not decoded at load time (e.g. context rebuilt).
            image = QtGui.QImage(str(path))
            if not image.isNull():
                image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
                self._texture_images[path] = image
        if image is None or image.isNull() or not GL:
            return 0
        try:
            texture_id = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
            bits = image.constBits()
            data = bits.tobytes() if hasattr(bits, "tobytes") else bytes(bits)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, image.width(), image.height(), 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            return int(texture_id)
        except Exception:
            return 0

    def _draw_mesh_gl(self) -> None:
        if not GL or not self.model or self._vertex_data is None:
            return
        self._ensure_textures()
        projection, view = self._projection_view_matrices()
        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glLoadMatrixf(np.ascontiguousarray(projection.T, dtype=np.float32))
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glLoadIdentity()
        # Headlight: directional light fixed in eye space (set while the
        # modelview matrix is identity, before the view matrix is loaded).
        GL.glLightfv(GL.GL_LIGHT0, GL.GL_POSITION, (0.0, 0.0, 1.0, 0.0))
        GL.glLightfv(GL.GL_LIGHT0, GL.GL_DIFFUSE, (0.85, 0.85, 0.85, 1.0))
        GL.glLightfv(GL.GL_LIGHT0, GL.GL_AMBIENT, (0.0, 0.0, 0.0, 1.0))
        GL.glLoadMatrixf(np.ascontiguousarray(view.T, dtype=np.float32))

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        if not self._show_wireframe:
            # Simple fixed-function shading using the parsed PMX normals.
            GL.glEnable(GL.GL_LIGHTING)
            GL.glEnable(GL.GL_LIGHT0)
            GL.glEnable(GL.GL_COLOR_MATERIAL)
            GL.glColorMaterial(GL.GL_FRONT_AND_BACK, GL.GL_AMBIENT_AND_DIFFUSE)
            GL.glEnable(GL.GL_NORMALIZE)
            GL.glLightModeli(GL.GL_LIGHT_MODEL_TWO_SIDE, 1)
            GL.glLightModelfv(GL.GL_LIGHT_MODEL_AMBIENT, (0.38, 0.38, 0.38, 1.0))
        GL.glEnable(GL.GL_TEXTURE_2D)
        GL.glTexEnvi(GL.GL_TEXTURE_ENV, GL.GL_TEXTURE_ENV_MODE, GL.GL_MODULATE)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE if self._show_wireframe else GL.GL_FILL)

        stride = 8 * 4
        base_ptr = int(self._vertex_data.ctypes.data)
        GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
        GL.glVertexPointer(3, GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr))
        GL.glEnableClientState(GL.GL_NORMAL_ARRAY)
        GL.glNormalPointer(GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr + 12))
        GL.glEnableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glTexCoordPointer(2, GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr + 24))

        for material in self.model.materials:
            if self._hide_missing_texture_materials and material.missing_texture:
                continue
            if material.index_count <= 0 or material.index_start >= len(self.model.indices):
                continue
            color = material.diffuse
            GL.glColor4f(float(color[0]), float(color[1]), float(color[2]), max(0.25, float(color[3])))
            texture_id = self._texture_ids.get(material.texture_path) if material.texture_path else None
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id or 0)
            start = int(material.index_start)
            stop = min(len(self.model.indices), start + int(material.index_count))
            GL.glDrawElements(GL.GL_TRIANGLES, stop - start, GL.GL_UNSIGNED_INT, self.model.indices[start:stop])

        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
        GL.glDisable(GL.GL_LIGHTING)
        GL.glDisable(GL.GL_COLOR_MATERIAL)
        GL.glDisable(GL.GL_NORMALIZE)
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisableClientState(GL.GL_NORMAL_ARRAY)
        GL.glDisableClientState(GL.GL_VERTEX_ARRAY)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    def _projection_view_matrices(self) -> tuple[np.ndarray, np.ndarray]:
        width = max(1, self.width())
        height = max(1, self.height())
        aspect = width / height
        scene_extent = max(1.0, _scalar_float(self._scene_extent, 1.0))
        view_zoom = max(0.001, _scalar_float(self._view_zoom, 1.0))
        view_yaw = _scalar_float(self._view_yaw, math.pi)
        view_pitch = _scalar_float(self._view_pitch, 0.0)
        scene_center = np.asarray(self._scene_center, dtype=np.float64).reshape(-1)
        if scene_center.size < 3:
            scene_center = np.zeros(3, dtype=np.float64)
        else:
            scene_center = scene_center[:3]

        view_height = scene_extent / max(0.001, view_zoom * 0.78)
        view_width = view_height * aspect
        pixel_scale = min(width, height) * 0.78 * view_zoom / max(0.001, scene_extent)
        pan_x = -float(self._view_pan.x()) / max(0.001, pixel_scale)
        pan_y = float(self._view_pan.y()) / max(0.001, pixel_scale)
        left = -view_width * 0.5 + pan_x
        right = view_width * 0.5 + pan_x
        bottom = -view_height * 0.5 + pan_y
        top = view_height * 0.5 + pan_y
        near = -scene_extent * 4.0
        far = scene_extent * 4.0
        projection = np.array(
            [
                [2.0 / (right - left), 0.0, 0.0, -(right + left) / (right - left)],
                [0.0, 2.0 / (top - bottom), 0.0, -(top + bottom) / (top - bottom)],
                [0.0, 0.0, -2.0 / (far - near), -(far + near) / (far - near)],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        cy, sy = float(math.cos(view_yaw)), float(math.sin(view_yaw))
        cp, sp = float(math.cos(view_pitch)), float(math.sin(view_pitch))
        yaw = np.eye(4, dtype=np.float64)
        yaw[0, 0] = cy
        yaw[0, 2] = sy
        yaw[2, 0] = -sy
        yaw[2, 2] = cy
        pitch = np.eye(4, dtype=np.float64)
        pitch[1, 1] = cp
        pitch[1, 2] = -sp
        pitch[2, 1] = sp
        pitch[2, 2] = cp
        view = pitch @ yaw @ _translation_matrix(-scene_center)
        return projection, view

    def _project_np(self, positions: np.ndarray) -> list[QtCore.QPointF]:
        if len(positions) == 0:
            return []
        projection, view = self._projection_view_matrices()
        homogeneous = np.concatenate([positions[:, :3], np.ones((len(positions), 1), dtype=np.float64)], axis=1)
        clip = ((projection @ view) @ homogeneous.T).T
        safe_w = np.where(np.abs(clip[:, 3]) <= 1e-8, 1.0, clip[:, 3])
        ndc = clip[:, :3] / safe_w[:, None]
        x = self.width() * (0.5 + ndc[:, 0] * 0.5)
        y = self.height() * (0.5 - ndc[:, 1] * 0.5)
        return [QtCore.QPointF(float(px), float(py)) for px, py in zip(x, y)]

    def _draw_skeleton(self, painter: QtGui.QPainter) -> None:
        if not self.model or not self.model.bones:
            return
        positions = np.array([bone.position for bone in self.model.bones], dtype=np.float64)
        points = self._project_np(positions)
        if self._show_bones:
            painter.setPen(QtGui.QPen(QtGui.QColor(80, 170, 255, 220), BONE_LINE_WIDTH))
            for index, bone in enumerate(self.model.bones):
                if 0 <= bone.parent < len(points):
                    painter.drawLine(points[bone.parent], points[index])
            painter.setBrush(QtGui.QColor(245, 245, 245, 180))
            painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20, 180), 1.4))
            stride = max(1, len(points) // 90)
            for point in points[::stride]:
                painter.drawEllipse(point, BONE_POINT_RADIUS, BONE_POINT_RADIUS)
        if self._show_bone_names:
            old_font = painter.font()
            font = QtGui.QFont(old_font)
            font.setPointSize(max(7, old_font.pointSize() - 1))
            painter.setFont(font)
            painter.setPen(QtGui.QColor(235, 235, 235, 220))
            visible_rect = self.rect().adjusted(-80, -20, 160, 40)
            stride = max(1, len(points) // 140)
            for index in range(0, len(points), stride):
                point = points[index]
                if visible_rect.contains(point.toPoint()):
                    bone = self.model.bones[index]
                    painter.drawText(QtCore.QPointF(point.x() + 5.0, point.y() - 3.0), bone.name or bone.english_name or str(index))
            painter.setFont(old_font)

    def _draw_overlay(self, painter: QtGui.QPainter) -> None:
        if not self.model:
            return
        backend = "OpenGL" if self._gl_backend_active() and not self._gl_error else "CPU preview"
        textured = sum(1 for material in self.model.materials if material.texture_path)
        text = (
            f"{self.model.name or self.model.path.stem} | "
            f"{self.model.vertex_count:,} verts | {self.model.triangle_count:,} tris | "
            f"{len(self.model.bones):,} bones | {self.model.morph_count:,} morphs | "
            f"{len(self.model.materials):,} materials | {textured}/{self.model.texture_count} textures | {backend}"
        )
        painter.setPen(QtGui.QColor(235, 235, 235))
        painter.drawText(12, 22, text)
        if self.model.warnings:
            painter.setPen(QtGui.QColor(255, 210, 120))
            painter.drawText(12, 44, self.model.warnings[0])
        if self._height_meters_per_unit > 0.0:
            draw_meter_height_bar(
                painter,
                self._project_np,
                self._scene_mins,
                self._scene_maxs,
                self._height_up_axis,
                self._height_meters_per_unit,
                self._height_reference_m,
            )

    def _emit_stats(self) -> None:
        if not self.model:
            text = "Preview idle"
        else:
            if self._gl_backend_active() and not self._gl_error:
                backend = "OpenGL"
            elif GL is None:
                backend = "CPU preview (PyOpenGL not installed)"
            elif self._gl_error:
                backend = f"CPU preview (OpenGL error: {self._gl_error})"
            else:
                backend = "CPU preview (OpenGL unavailable)"
            if self._overlay_error:
                backend = f"{backend} | overlay error: {self._overlay_error}"
            text = (
                f"{backend} | {self.model.vertex_count:,} verts | {self.model.triangle_count:,} tris | "
                f"{len(self.model.bones):,} bones | {self.model.morph_count:,} morphs | warnings {len(self.model.warnings)}"
            )
        # paintGL calls this every frame; only emit when the text actually changes.
        if text != self._last_stats_text:
            self._last_stats_text = text
            self.statsChanged.emit(text)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._last_mouse_pos = event.position().toPoint()
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        elif event.button() in (QtCore.Qt.MouseButton.RightButton, QtCore.Qt.MouseButton.MiddleButton):
            self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._last_mouse_pos is None:
            self._last_mouse_pos = event.position().toPoint()
            return
        current = event.position().toPoint()
        delta = current - self._last_mouse_pos
        self._last_mouse_pos = current
        buttons = event.buttons()
        if buttons & QtCore.Qt.MouseButton.LeftButton:
            self._drag_active = True
            self._view_yaw = _scalar_float(self._view_yaw, math.pi) + float(delta.x()) * 0.008
            self._view_pitch = max(-1.45, min(1.45, _scalar_float(self._view_pitch, 0.0) + float(delta.y()) * 0.008))
            self.update()
        elif buttons & (QtCore.Qt.MouseButton.RightButton | QtCore.Qt.MouseButton.MiddleButton):
            self._drag_active = True
            self._view_pan += QtCore.QPointF(delta.x(), delta.y())
            self.update()
        else:
            self._drag_active = False
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if not event.buttons():
            self._last_mouse_pos = None
            self._drag_active = False
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.reset_front_view()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            self._last_wheel_time = time.monotonic()
            self._view_zoom = max(0.2, min(8.0, _scalar_float(self._view_zoom, 1.0) * math.pow(1.0015, float(delta))))
            self.update()
        event.accept()


class SkeletonPlanPreviewWidget(QtWidgets.QWidget):
    """Interactive 3D-ish skeleton preview for step-3 spine repair plans."""

    statsChanged = QtCore.Signal(str)
    boneContextActionRequested = QtCore.Signal(str, str)
    DEFAULT_FRONT_YAW = 0.0
    DEFAULT_FRONT_PITCH = 0.0

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.analysis: dict[str, object] | None = None
        self.plan: dict[str, object] | None = None
        self._bones: dict[str, dict[str, object]] = {}
        self._scene_center = np.zeros(3, dtype=np.float64)
        self._scene_extent = 1.0
        self._default_yaw = self.DEFAULT_FRONT_YAW
        self._default_pitch = self.DEFAULT_FRONT_PITCH
        self._view_yaw = self._default_yaw
        self._view_pitch = self._default_pitch
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self._using_default_view = True
        self._last_mouse_pos: QtCore.QPoint | None = None
        self._hovered_bone: str | None = None
        self._hover_pos = QtCore.QPointF(0.0, 0.0)
        self._show_model = True
        self._show_bone_names = False
        self._bones_generation = 0
        self._projection_cache: tuple[tuple, tuple] | None = None
        self.setMinimumSize(520, 340)
        self.setMouseTracking(True)

    def clear(self) -> None:
        self.analysis = None
        self.plan = None
        self._bones = {}
        self._bones_generation += 1
        self._projection_cache = None
        self._default_yaw = self.DEFAULT_FRONT_YAW
        self._default_pitch = self.DEFAULT_FRONT_PITCH
        self._view_yaw = self._default_yaw
        self._view_pitch = self._default_pitch
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self._using_default_view = True
        self._hovered_bone = None
        self.update()
        self.statsChanged.emit("Skeleton preview idle")

    def set_model_visible(self, visible: bool) -> None:
        self._show_model = bool(visible)
        self.update()

    def set_bone_names_visible(self, visible: bool) -> None:
        self._show_bone_names = bool(visible)
        self.update()

    def reset_front_view(self, *_args: object) -> None:
        self._view_yaw = self._default_yaw
        self._view_pitch = self._default_pitch
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self._using_default_view = True
        self.update()

    def set_analysis(self, analysis: dict[str, object] | None, plan: dict[str, object] | None) -> None:
        self.analysis = analysis
        self.plan = plan
        self._bones = {}
        self._bones_generation += 1
        self._projection_cache = None
        if analysis:
            for raw in analysis.get("bones", []):
                if isinstance(raw, dict) and raw.get("name"):
                    self._bones[str(raw["name"])] = raw
        self._prepare_scene()
        self._update_default_front_view_from_landmarks()
        if self._using_default_view:
            self._view_yaw = self._default_yaw
            self._view_pitch = self._default_pitch
        self.update()
        count = len(self._bones)
        validation = self._validation()
        status = "valid" if validation.get("ok") else f"{len(validation.get('errors', []))} validation error(s)"
        if self._is_merge_plan():
            estimated = self.plan.get("estimated_final_bone_count") if self.plan else None
            limit = self.plan.get("bone_limit") if self.plan else None
            self.statsChanged.emit(f"Bone merge preview | {count:,} bones | estimated {estimated} / limit {limit} | {status}")
        else:
            self.statsChanged.emit(f"Spine preview | {count:,} bones | {status}")

    def _is_merge_plan(self) -> bool:
        return bool(self.plan and str(self.plan.get("kind") or "") == "sort_bones")

    def _prepare_scene(self) -> None:
        points: list[np.ndarray] = []
        for bone in self._bones.values():
            for key in ("head", "tail"):
                value = bone.get(key)
                if isinstance(value, list) and len(value) >= 3:
                    points.append(np.array(value[:3], dtype=np.float64))
        preview = self._model_preview()
        vertices = preview.get("vertices") if isinstance(preview, dict) else None
        if isinstance(vertices, list):
            for raw in vertices:
                if isinstance(raw, list) and len(raw) >= 3:
                    points.append(np.array(raw[:3], dtype=np.float64))
        for entry in self._target_entries().values():
            position = entry.get("position") if isinstance(entry, dict) else None
            if isinstance(position, dict):
                for key in ("head", "tail"):
                    value = position.get(key)
                    if isinstance(value, list) and len(value) >= 3:
                        points.append(np.array(value[:3], dtype=np.float64))
        if not points:
            self._scene_center = np.zeros(3, dtype=np.float64)
            self._scene_extent = 1.0
            return
        array = np.vstack(points)
        mins = array.min(axis=0)
        maxs = array.max(axis=0)
        self._scene_center = (mins + maxs) * 0.5
        self._scene_extent = max(0.2, float(np.max(maxs - mins)))

    def _bone_head_vector(self, names: list[str]) -> np.ndarray | None:
        for name in names:
            bone = self._bones.get(name)
            if not bone:
                continue
            head = bone.get("head")
            if isinstance(head, list) and len(head) >= 3:
                return np.array(head[:3], dtype=np.float64)
        return None

    def _update_default_front_view_from_landmarks(self) -> None:
        self._default_yaw = self.DEFAULT_FRONT_YAW
        self._default_pitch = self.DEFAULT_FRONT_PITCH
        pelvis = self._bone_head_vector(["ValveBiped.Bip01_Pelvis"])
        left_arm = self._bone_head_vector(
            [
                "ValveBiped.Bip01_L_UpperArm",
                "ValveBiped.Bip01_L_Clavicle",
                "ValveBiped.Bip01_L_Forearm",
                "ValveBiped.Bip01_L_Hand",
            ]
        )
        right_arm = self._bone_head_vector(
            [
                "ValveBiped.Bip01_R_UpperArm",
                "ValveBiped.Bip01_R_Clavicle",
                "ValveBiped.Bip01_R_Forearm",
                "ValveBiped.Bip01_R_Hand",
            ]
        )
        if pelvis is None or left_arm is None or right_arm is None:
            return
        left_dx = float(left_arm[0] - pelvis[0])
        right_dx = float(right_arm[0] - pelvis[0])
        # Source convention places the model's left side on positive X and right side on negative X.
        # If a blend has that axis reversed, use the opposite upright front view.
        if left_dx < right_dx:
            self._default_yaw = math.pi
            self._default_pitch = 0.0

    def _target_entries(self) -> dict[str, dict[str, object]]:
        if not self.plan or not isinstance(self.plan.get("targets"), dict):
            return {}
        return {str(key): value for key, value in self.plan["targets"].items() if isinstance(value, dict)}

    def _validation(self) -> dict[str, object]:
        if not self.analysis:
            return {"ok": False, "errors": [], "warnings": []}
        current = self.analysis.get("current_validation")
        if isinstance(current, dict):
            return current
        return {"ok": False, "errors": [], "warnings": []}

    def _model_preview(self) -> dict[str, object]:
        if not self.analysis:
            return {}
        preview = self.analysis.get("model_preview")
        return preview if isinstance(preview, dict) else {}

    def _vector_for_target(self, target: str, key: str = "head") -> np.ndarray | None:
        entry = self._target_entries().get(target)
        if not entry:
            return None
        if str(entry.get("action")) == "add":
            position = entry.get("position")
            if isinstance(position, dict):
                value = position.get(key)
                if isinstance(value, list) and len(value) >= 3:
                    return np.array(value[:3], dtype=np.float64)
        source = entry.get("source")
        if source and str(source) in self._bones:
            value = self._bones[str(source)].get(key)
            if isinstance(value, list) and len(value) >= 3:
                return np.array(value[:3], dtype=np.float64)
        return None

    def _projection_view_matrices(self) -> tuple[np.ndarray, np.ndarray]:
        width = max(1, self.width())
        height = max(1, self.height())
        aspect = width / height
        scene_extent = max(0.2, _scalar_float(self._scene_extent, 1.0))
        view_zoom = max(0.001, _scalar_float(self._view_zoom, 1.0))
        view_height = scene_extent / max(0.001, view_zoom * 0.78)
        view_width = view_height * aspect
        pixel_scale = min(width, height) * 0.78 * view_zoom / max(0.001, scene_extent)
        pan_x = -float(self._view_pan.x()) / max(0.001, pixel_scale)
        pan_y = float(self._view_pan.y()) / max(0.001, pixel_scale)
        left = -view_width * 0.5 + pan_x
        right = view_width * 0.5 + pan_x
        bottom = -view_height * 0.5 + pan_y
        top = view_height * 0.5 + pan_y
        near = -scene_extent * 4.0
        far = scene_extent * 4.0
        projection = np.array(
            [
                [2.0 / (right - left), 0.0, 0.0, -(right + left) / (right - left)],
                [0.0, 2.0 / (top - bottom), 0.0, -(top + bottom) / (top - bottom)],
                [0.0, 0.0, -2.0 / (far - near), -(far + near) / (far - near)],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        yaw_angle = _scalar_float(self._view_yaw, self._default_yaw)
        pitch_angle = _scalar_float(self._view_pitch, self._default_pitch)
        return projection, _upright_orbit_view_matrix(self._scene_center, yaw_angle, pitch_angle)

    def _project_np(self, positions: np.ndarray) -> list[QtCore.QPointF]:
        if len(positions) == 0:
            return []
        projection, view = self._projection_view_matrices()
        homogeneous = np.concatenate([positions[:, :3], np.ones((len(positions), 1), dtype=np.float64)], axis=1)
        clip = ((projection @ view) @ homogeneous.T).T
        safe_w = np.where(np.abs(clip[:, 3]) <= 1e-8, 1.0, clip[:, 3])
        ndc = clip[:, :3] / safe_w[:, None]
        x = self.width() * (0.5 + ndc[:, 0] * 0.5)
        y = self.height() * (0.5 - ndc[:, 1] * 0.5)
        return [QtCore.QPointF(float(px), float(py)) for px, py in zip(x, y)]

    def _project_one(self, vector: np.ndarray | None) -> QtCore.QPointF | None:
        if vector is None:
            return None
        return self._project_np(vector.reshape(1, 3))[0]

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.fillRect(self.rect(), QtGui.QColor(22, 24, 27))
            if not self._bones:
                painter.setPen(QtGui.QColor(230, 230, 230))
                painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Analyze a blend to preview the skeleton plan.")
                return
            if self._show_model:
                self._draw_model_preview(painter)
            self._draw_all_bones(painter)
            self._draw_side_landmarks(painter)
            if self._is_merge_plan():
                self._draw_merge_plan(painter)
            else:
                self._draw_target_chain(painter)
            if self._show_bone_names:
                self._draw_bone_names(painter)
            self._draw_hovered_bone(painter)
            self._draw_overlay(painter)
        finally:
            painter.end()

    def _draw_model_preview(self, painter: QtGui.QPainter) -> None:
        preview = self._model_preview()
        vertices = preview.get("vertices")
        if not isinstance(vertices, list) or not vertices:
            return
        vectors: list[np.ndarray] = []
        for raw in vertices:
            if isinstance(raw, list) and len(raw) >= 3:
                vectors.append(np.array(raw[:3], dtype=np.float64))
        if not vectors:
            return
        points = self._project_np(np.vstack(vectors))
        edges = preview.get("edges")
        painter.setPen(QtGui.QPen(QtGui.QColor(160, 170, 185, 38), 1))
        if isinstance(edges, list):
            for raw_edge in edges:
                if not isinstance(raw_edge, list) or len(raw_edge) < 2:
                    continue
                a = int(raw_edge[0])
                b = int(raw_edge[1])
                if 0 <= a < len(points) and 0 <= b < len(points):
                    painter.drawLine(points[a], points[b])
        painter.setPen(QtGui.QPen(QtGui.QColor(178, 190, 205, 55), 1))
        for point in points[:: max(1, len(points) // 3500)]:
            painter.drawPoint(point)

    def _draw_all_bones(self, painter: QtGui.QPainter) -> None:
        points = self._projected_bone_heads()
        painter.setPen(QtGui.QPen(QtGui.QColor(130, 142, 156, 150), BONE_LINE_WIDTH))
        for name, bone in self._bones.items():
            parent = bone.get("parent")
            if parent in points and name in points:
                painter.drawLine(points[str(parent)], points[name])

    def _draw_bone_names(self, painter: QtGui.QPainter) -> None:
        points = self._projected_bone_heads()
        painter.setFont(QtGui.QFont(self.font().family(), 7))
        painter.setPen(QtGui.QColor(220, 225, 232, 170))
        for name, point in points.items():
            painter.drawText(QtCore.QPointF(point.x() + 4.0, point.y() - 3.0), name)

    def _draw_side_landmarks(self, painter: QtGui.QPainter) -> None:
        colors = {
            "ValveBiped.Bip01_R_Clavicle": QtGui.QColor(255, 132, 92),
            "ValveBiped.Bip01_R_UpperArm": QtGui.QColor(255, 132, 92),
            "ValveBiped.Bip01_R_Thigh": QtGui.QColor(255, 132, 92),
            "ValveBiped.Bip01_L_Clavicle": QtGui.QColor(88, 196, 140),
            "ValveBiped.Bip01_L_UpperArm": QtGui.QColor(88, 196, 140),
            "ValveBiped.Bip01_L_Thigh": QtGui.QColor(88, 196, 140),
        }
        for name, color in colors.items():
            bone = self._bones.get(name)
            if not bone:
                continue
            head = bone.get("head")
            if not isinstance(head, list) or len(head) < 3:
                continue
            point = self._project_one(np.array(head[:3], dtype=np.float64))
            if point is None:
                continue
            painter.setBrush(color)
            painter.setPen(QtGui.QPen(QtGui.QColor(15, 15, 15), 1.4))
            painter.drawEllipse(point, BONE_POINT_RADIUS + 1.4, BONE_POINT_RADIUS + 1.4)

    def _draw_target_chain(self, painter: QtGui.QPainter) -> None:
        chain = ["ValveBiped.Bip01_Pelvis", "ValveBiped.Bip01_Spine", "ValveBiped.Bip01_Spine1", "ValveBiped.Bip01_Spine2", "ValveBiped.Bip01_Spine4"]
        colors = [QtGui.QColor(79, 192, 255), QtGui.QColor(88, 166, 255), QtGui.QColor(163, 113, 247), QtGui.QColor(255, 203, 107)]
        points = [self._project_one(self._vector_for_target(target, "head")) for target in chain]
        for index in range(len(points) - 1):
            if points[index] is None or points[index + 1] is None:
                continue
            painter.setPen(QtGui.QPen(colors[min(index, len(colors) - 1)], BONE_HIGHLIGHT_LINE_WIDTH - 1.0))
            painter.drawLine(points[index], points[index + 1])
        painter.setPen(QtGui.QPen(QtGui.QColor(245, 245, 245), 1.4))
        for target, point in zip(chain, points):
            if point is None:
                continue
            entry = self._target_entries().get(target, {})
            action = str(entry.get("action") or "")
            painter.setBrush(QtGui.QColor(255, 210, 120) if action == "add" else QtGui.QColor(235, 235, 235))
            painter.drawEllipse(point, BONE_POINT_RADIUS + 1.8, BONE_POINT_RADIUS + 1.8)
            painter.drawText(QtCore.QPointF(point.x() + 6.0, point.y() - 5.0), target.rsplit("_", 1)[-1])
        for target, entry in self._target_entries().items():
            if str(entry.get("action")) != "add":
                continue
            head = self._project_one(self._vector_for_target(target, "head"))
            tail = self._project_one(self._vector_for_target(target, "tail"))
            if head is None or tail is None:
                continue
            pen = QtGui.QPen(QtGui.QColor(255, 210, 120), BONE_LINE_WIDTH + 0.8)
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(head, tail)

    def _projection_view_key(self) -> tuple:
        return (
            _scalar_float(self._view_yaw, self._default_yaw),
            _scalar_float(self._view_pitch, self._default_pitch),
            _scalar_float(self._view_zoom, 1.0),
            float(self._view_pan.x()),
            float(self._view_pan.y()),
            self.width(),
            self.height(),
            self._bones_generation,
        )

    def _projected_bone_geometry(self) -> tuple[dict[str, QtCore.QPointF], list[str], np.ndarray, np.ndarray, np.ndarray]:
        """Projected bone heads, cached per view state and bones generation.

        Returns (points by name, names, screen coords (n, 2), child segment
        indices, parent segment indices) so hover hit-testing can run on numpy
        arrays instead of re-projecting every bone per mouse move/paint.
        """
        key = self._projection_view_key()
        if self._projection_cache is not None and self._projection_cache[0] == key:
            return self._projection_cache[1]
        names: list[str] = []
        vectors: list[np.ndarray] = []
        for name, bone in self._bones.items():
            head = bone.get("head")
            if isinstance(head, list) and len(head) >= 3:
                names.append(name)
                vectors.append(np.array(head[:3], dtype=np.float64))
        projected = self._project_np(np.vstack(vectors)) if vectors else []
        points = dict(zip(names, projected))
        coords = (
            np.array([[float(point.x()), float(point.y())] for point in projected], dtype=np.float64)
            if projected
            else np.zeros((0, 2), dtype=np.float64)
        )
        name_to_index = {name: index for index, name in enumerate(names)}
        seg_child: list[int] = []
        seg_parent: list[int] = []
        for index, name in enumerate(names):
            parent = self._bones[name].get("parent")
            parent_index = name_to_index.get(str(parent)) if parent else None
            if parent_index is not None:
                seg_child.append(index)
                seg_parent.append(parent_index)
        geometry = (
            points,
            names,
            coords,
            np.asarray(seg_child, dtype=np.intp),
            np.asarray(seg_parent, dtype=np.intp),
        )
        self._projection_cache = (key, geometry)
        return geometry

    def _projected_bone_heads(self) -> dict[str, QtCore.QPointF]:
        return self._projected_bone_geometry()[0]

    def _draw_merge_plan(self, painter: QtGui.QPainter) -> None:
        if not self.plan:
            return
        points = self._projected_bone_heads()
        protected = set(str(name) for name in self.plan.get("protected_bones", []) if name) if isinstance(self.plan.get("protected_bones"), list) else set()
        operations = self.plan.get("operations", [])
        enabled_ops = [entry for entry in operations if isinstance(entry, dict) and entry.get("enabled", True)] if isinstance(operations, list) else []
        disabled_ops = [entry for entry in operations if isinstance(entry, dict) and not entry.get("enabled", True)] if isinstance(operations, list) else []

        for entry in disabled_ops[:120]:
            source = str(entry.get("source") or "")
            target = str(entry.get("target") or "")
            source_point = points.get(source)
            target_point = points.get(target)
            if source_point is None or target_point is None:
                continue
            pen = QtGui.QPen(QtGui.QColor(145, 150, 160, 120), BONE_LINE_WIDTH)
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(source_point, target_point)
            painter.setPen(QtGui.QPen(QtGui.QColor(145, 150, 160, 170), 1.6))
            painter.drawLine(QtCore.QPointF(source_point.x() - 3, source_point.y() - 3), QtCore.QPointF(source_point.x() + 3, source_point.y() + 3))
            painter.drawLine(QtCore.QPointF(source_point.x() - 3, source_point.y() + 3), QtCore.QPointF(source_point.x() + 3, source_point.y() - 3))

        painter.setPen(QtGui.QPen(QtGui.QColor(87, 171, 90, 230), BONE_LINE_WIDTH + 0.8))
        painter.setBrush(QtGui.QColor(87, 171, 90, 190))
        for name in protected:
            point = points.get(name)
            if point is not None:
                painter.drawEllipse(point, BONE_POINT_RADIUS + 0.8, BONE_POINT_RADIUS + 0.8)

        for entry in enabled_ops[:140]:
            source = str(entry.get("source") or "")
            target = str(entry.get("target") or "")
            source_point = points.get(source)
            target_point = points.get(target)
            if source_point is None or target_point is None:
                continue
            round_index = int(entry.get("round", 0) or 0)
            color = QtGui.QColor(255, 158, 77, 180) if round_index else QtGui.QColor(255, 220, 112, 210)
            pen = QtGui.QPen(color, BONE_LINE_WIDTH + 0.8)
            if round_index == 0:
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(source_point, target_point)
            painter.setBrush(QtGui.QColor(248, 81, 73, 220))
            painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20), 1.4))
            painter.drawEllipse(source_point, BONE_POINT_RADIUS + 0.8, BONE_POINT_RADIUS + 0.8)
            painter.setBrush(QtGui.QColor(163, 113, 247, 220))
            painter.drawEllipse(target_point, BONE_POINT_RADIUS + 1.2, BONE_POINT_RADIUS + 1.2)

        if len(enabled_ops) > 140:
            painter.setPen(QtGui.QColor(255, 210, 120))
            painter.drawText(12, 66, f"Preview limited to first 140 enabled merge lines out of {len(enabled_ops):,}.")

    def _draw_overlay(self, painter: QtGui.QPainter) -> None:
        validation = self._validation()
        errors = validation.get("errors", []) if isinstance(validation.get("errors"), list) else []
        warnings = validation.get("warnings", []) if isinstance(validation.get("warnings"), list) else []
        painter.setPen(QtGui.QColor(235, 235, 235))
        if self._is_merge_plan() and self.plan:
            operations = self.plan.get("operations", [])
            enabled = sum(1 for entry in operations if isinstance(entry, dict) and entry.get("enabled", True)) if isinstance(operations, list) else 0
            disabled = sum(1 for entry in operations if isinstance(entry, dict) and not entry.get("enabled", True)) if isinstance(operations, list) else 0
            estimated = self.plan.get("estimated_final_bone_count", "?")
            limit = self.plan.get("bone_limit", "?")
            painter.drawText(12, 22, f"{len(self._bones):,} bones | enabled merges {enabled:,} | disabled {disabled:,} | estimated {estimated} / {limit} | errors {len(errors)} | warnings {len(warnings)}")
        else:
            painter.drawText(12, 22, f"{len(self._bones):,} bones | current validation errors {len(errors)} | warnings {len(warnings)}")
        if self.plan and isinstance(self.plan.get("warnings"), list) and self.plan["warnings"]:
            painter.setPen(QtGui.QColor(255, 210, 120))
            painter.drawText(12, 44, str(self.plan["warnings"][0]))
        if self._hovered_bone:
            painter.setPen(QtGui.QColor(235, 235, 235))
            painter.drawText(12, self.height() - 14, f"Hovered bone: {self._hovered_bone}")

    def _draw_hovered_bone(self, painter: QtGui.QPainter) -> None:
        if not self._hovered_bone or self._hovered_bone not in self._bones:
            return
        points = self._projected_bone_heads()
        point = points.get(self._hovered_bone)
        if point is None:
            return
        parent = self._bones[self._hovered_bone].get("parent")
        parent_point = points.get(str(parent)) if parent else None
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 230, 120), BONE_HIGHLIGHT_LINE_WIDTH))
        if parent_point is not None:
            painter.drawLine(parent_point, point)
        painter.setBrush(QtGui.QColor(255, 230, 120))
        painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20), 1.6))
        painter.drawEllipse(point, BONE_HIGHLIGHT_POINT_RADIUS, BONE_HIGHLIGHT_POINT_RADIUS)
        text = self._hovered_bone
        metrics = painter.fontMetrics()
        rect = metrics.boundingRect(text).adjusted(-6, -4, 6, 4)
        x = min(max(8.0, self._hover_pos.x() + 12.0), max(8.0, self.width() - rect.width() - 8.0))
        y = min(max(24.0, self._hover_pos.y() - 12.0), max(24.0, self.height() - rect.height() - 8.0))
        label_rect = QtCore.QRectF(x, y - rect.height(), rect.width(), rect.height())
        painter.setBrush(QtGui.QColor(16, 18, 22, 225))
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 230, 120), 1))
        painter.drawRoundedRect(label_rect, 4.0, 4.0)
        painter.setPen(QtGui.QColor(245, 245, 245))
        painter.drawText(label_rect.adjusted(6, 2, -6, -2), QtCore.Qt.AlignmentFlag.AlignVCenter, text)

    def _nearest_bone_at(self, position: QtCore.QPointF) -> str | None:
        _points, names, coords, seg_child, seg_parent = self._projected_bone_geometry()
        if not len(coords):
            return None
        best_name: str | None = None
        best_distance = 10.0
        cursor = np.array([float(position.x()), float(position.y())], dtype=np.float64)
        deltas = coords - cursor
        distances = np.hypot(deltas[:, 0], deltas[:, 1])
        head_index = int(np.argmin(distances))
        if float(distances[head_index]) < best_distance:
            best_distance = float(distances[head_index])
            best_name = names[head_index]
        if len(seg_child):
            starts = coords[seg_parent]
            spans = coords[seg_child] - starts
            length_sq = np.maximum((spans * spans).sum(axis=1), 1e-8)
            t = np.clip(((cursor - starts) * spans).sum(axis=1) / length_sq, 0.0, 1.0)
            nearest = starts + spans * t[:, None]
            seg_distances = np.hypot(nearest[:, 0] - cursor[0], nearest[:, 1] - cursor[1])
            seg_index = int(np.argmin(seg_distances))
            if float(seg_distances[seg_index]) < best_distance:
                best_distance = float(seg_distances[seg_index])
                best_name = names[int(seg_child[seg_index])]
        return best_name

    def _update_hovered_bone(self, position: QtCore.QPointF) -> None:
        hovered = self._nearest_bone_at(position)
        moved = abs(float(position.x() - self._hover_pos.x())) > 1.0 or abs(float(position.y() - self._hover_pos.y())) > 1.0
        self._hover_pos = position
        if hovered != self._hovered_bone:
            self._hovered_bone = hovered
            self.setToolTip(hovered or "")
            self.update()
        elif hovered and moved:
            self.update()

    def _protected_plan_sets(self) -> tuple[set[str], set[str], tuple[str, ...]]:
        if not self.plan:
            return set(), set(), ()
        protected = set(str(name) for name in self.plan.get("protected_bones", []) if name) if isinstance(self.plan.get("protected_bones"), list) else set()
        base = set(str(name) for name in self.plan.get("base_protected_bones", []) if name) if isinstance(self.plan.get("base_protected_bones"), list) else set()
        prefixes = tuple(str(prefix) for prefix in self.plan.get("protected_prefixes", []) if prefix) if isinstance(self.plan.get("protected_prefixes"), list) else ()
        return protected, base, prefixes

    def _enabled_operations_touching_bone(self, bone_name: str) -> list[dict[str, object]]:
        if not self.plan:
            return []
        raw = self.plan.get("operations", [])
        if not isinstance(raw, list):
            return []
        return [
            entry
            for entry in raw
            if isinstance(entry, dict)
            and entry.get("enabled", True)
            and (str(entry.get("source") or "") == bone_name or str(entry.get("target") or "") == bone_name)
        ]

    def _show_bone_context_menu(self, global_pos: QtCore.QPoint) -> None:
        bone_name = self._hovered_bone
        if not bone_name:
            return
        menu = QtWidgets.QMenu(self)
        title = menu.addAction(bone_name)
        title.setEnabled(False)
        if self._is_merge_plan():
            protected, base, prefixes = self._protected_plan_sets()
            is_prefix_protected = any(bone_name.startswith(prefix) for prefix in prefixes)
            is_core_protected = bone_name in base or is_prefix_protected
            is_protected = bone_name in protected or is_core_protected
            parent = self._bones.get(bone_name, {}).get("parent")
            if parent and not is_protected:
                action = menu.addAction("Merge to parent")
                action.triggered.connect(lambda _checked=False, b=bone_name: self.boneContextActionRequested.emit("merge_to_parent", b))
            if not is_protected:
                action = menu.addAction("Protect bone")
                action.triggered.connect(lambda _checked=False, b=bone_name: self.boneContextActionRequested.emit("protect", b))
            elif bone_name in protected and not is_core_protected:
                action = menu.addAction("Disable protect bone")
                action.triggered.connect(lambda _checked=False, b=bone_name: self.boneContextActionRequested.emit("unprotect", b))
            touching = self._enabled_operations_touching_bone(bone_name)
            if touching:
                action = menu.addAction("Cancel merge")
                action.triggered.connect(lambda _checked=False, b=bone_name: self.boneContextActionRequested.emit("cancel_merge", b))
        if len(menu.actions()) <= 1:
            no_action = menu.addAction("No editable action for this bone")
            no_action.setEnabled(False)
        menu.exec(global_pos)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.RightButton:
            self._update_hovered_bone(event.position())
            if self._hovered_bone:
                self._show_bone_context_menu(event.globalPosition().toPoint())
                event.accept()
                return
        self._last_mouse_pos = event.position().toPoint()
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        elif event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.buttons() == QtCore.Qt.MouseButton.NoButton:
            self._update_hovered_bone(event.position())
            super().mouseMoveEvent(event)
            return
        if self._last_mouse_pos is None:
            self._last_mouse_pos = event.position().toPoint()
            return
        current = event.position().toPoint()
        delta = current - self._last_mouse_pos
        self._last_mouse_pos = current
        buttons = event.buttons()
        if buttons & QtCore.Qt.MouseButton.LeftButton:
            self._view_yaw = _scalar_float(self._view_yaw, self._default_yaw) + float(delta.x()) * 0.008
            self._view_pitch = max(-math.pi * 0.5 + 0.02, min(math.pi * 0.5 - 0.02, _scalar_float(self._view_pitch, self._default_pitch) + float(delta.y()) * 0.008))
            self._using_default_view = False
            self.update()
        elif buttons & QtCore.Qt.MouseButton.MiddleButton:
            self._view_pan += QtCore.QPointF(delta.x(), delta.y())
            self._using_default_view = False
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if not event.buttons():
            self._last_mouse_pos = None
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.reset_front_view()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            self._view_zoom = max(0.2, min(8.0, _scalar_float(self._view_zoom, 1.0) * math.pow(1.0015, float(delta))))
            self._using_default_view = False
            self.update()
        event.accept()


class MaterialPreviewWidget(_PreviewWidgetBase):
    """Material-region preview for step 5 scans."""

    statsChanged = QtCore.Signal(str)

    MAX_TEXTURE_UPLOADS_PER_FRAME = 2

    VIEW_DIRECTIONS: dict[str, tuple[float, float]] = {
        "Front": (0.0, 0.0),
        "Back": (math.pi, 0.0),
        "Right": (-math.pi * 0.5, 0.0),
        "Left": (math.pi * 0.5, 0.0),
        "Top": (0.0, math.pi * 0.5),
        "Bottom": (0.0, -math.pi * 0.5),
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.scan: dict[str, object] | None = None
        self.plan: dict[str, object] | None = None
        self._materials: dict[str, dict[str, object]] = {}
        self._triangles: list[dict[str, object]] = []
        self._material_positions: dict[str, np.ndarray] = {}
        self._material_uvs: dict[str, np.ndarray] = {}
        self._material_base_vertex_data: dict[str, np.ndarray] = {}
        self._material_vertex_data: dict[str, np.ndarray] = {}
        self._material_flex_deltas: dict[str, dict[str, np.ndarray]] = {}
        self._material_texture_paths: dict[str, Path] = {}
        self._material_texture_ids: dict[str, int] = {}
        self._collision_lines: dict[str, np.ndarray] = {}
        self._collision_colors: dict[str, tuple[float, float, float, float]] = {}
        self._collision_labels: dict[str, str] = {}
        self._hovered_collision_uid = ""
        self._highlighted_collision_uids: set[str] = set()
        self._bone_lines: dict[str, np.ndarray] = {}
        self._bone_labels: dict[str, str] = {}
        self._hovered_bone_uid = ""
        self._highlighted_bone_uids: set[str] = set()
        self._bone_group_colors: dict[str, tuple[float, float, float]] = {}
        self._highlighted_uids: set[str] = set()
        self._flex_sources: dict[str, list[tuple[str, float]]] = {}
        self._flex_rest_values: dict[str, float] = {}
        self._flex_scales: dict[str, float] = {}
        self._enabled_flex_uids: set[str] = set()
        self._active_flex_uid = ""
        self._active_flex_value = 1.0
        self._isolated_bodygroup = ""
        # Manual Step 8/14 preview aids (render-only; never mutate plan data).
        self._bones_on_top = False
        self._show_bone_labels = False
        self._front_bodygroup = ""
        self._constraint_arcs: dict[str, np.ndarray] = {}
        self._scene_center = np.zeros(3, dtype=np.float64)
        self._scene_extent = 1.0
        self._scene_mins = np.zeros(3, dtype=np.float64)
        self._scene_maxs = np.ones(3, dtype=np.float64)
        self._height_meters_per_unit = 0.0
        self._height_up_axis = 2
        self._height_reference_m = HEIGHT_REFERENCE_METERS
        self._view_yaw, self._view_pitch = self.VIEW_DIRECTIONS["Front"]
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self._last_mouse_pos: QtCore.QPoint | None = None
        self._wireframe = False
        self._respect_alpha = False
        self._selected_uid = ""
        self._hovered_uid = ""
        self._gl_error = ""
        self._gl_error_count = 0
        # CPU (QPainter) fallback renderer state (issue #129).
        self._gl_disabled = False
        self._gl_failures = 0
        self._drag_active = False
        self._last_wheel_time = 0.0
        self._hover_update_queued = False
        self._texture_uploads_this_frame = 0
        self._texture_uploads_pending = False
        self._textured_preview_disabled = _truthy_env("MCI_DISABLE_TEXTURED_PREVIEW")
        self._preview_debug_enabled = _truthy_env("MCI_MATERIAL_PREVIEW_DEBUG")
        self._preview_debug_log_path = _preview_debug_log_path()
        self._empty_message = "Scan a blend to preview material regions."
        self._opengl_unavailable_message = "OpenGL material preview is unavailable."
        self._textured_disabled_message = "Textured preview disabled; using solid material colors."
        self.setMinimumSize(520, 340)
        self.setMouseTracking(True)

    def set_i18n_texts(self, texts: dict[str, str]) -> None:
        self._empty_message = str(texts.get("material_empty", self._empty_message) or self._empty_message)
        self._opengl_unavailable_message = str(texts.get("material_opengl_unavailable", self._opengl_unavailable_message) or self._opengl_unavailable_message)
        self._textured_disabled_message = str(texts.get("material_textured_disabled", self._textured_disabled_message) or self._textured_disabled_message)
        self.update()

    def clear(self) -> None:
        self.scan = None
        self.plan = None
        self._materials = {}
        self._triangles = []
        self._material_positions = {}
        self._material_uvs = {}
        self._material_base_vertex_data = {}
        self._material_vertex_data = {}
        self._material_flex_deltas = {}
        self._material_texture_paths = {}
        self._release_material_textures()
        self._collision_lines = {}
        self._collision_colors = {}
        self._collision_labels = {}
        self._hovered_collision_uid = ""
        self._highlighted_collision_uids = set()
        self._bone_lines = {}
        self._bone_labels = {}
        self._hovered_bone_uid = ""
        self._highlighted_bone_uids = set()
        self._bone_group_colors = {}
        self._highlighted_uids = set()
        self._flex_sources = {}
        self._flex_rest_values = {}
        self._flex_scales = {}
        self._enabled_flex_uids = set()
        self._active_flex_uid = ""
        self._active_flex_value = 1.0
        self._isolated_bodygroup = ""
        self._selected_uid = ""
        self._hovered_uid = ""
        self._gl_error = ""
        self._gl_error_count = 0
        self.reset_view("Front")
        self.update()
        self.statsChanged.emit("Material preview idle")

    def set_material_data(self, scan: dict[str, object] | None, plan: dict[str, object] | None) -> None:
        self.scan = scan
        self.plan = plan
        self._materials = {}
        if plan and isinstance(plan.get("materials"), list):
            for entry in plan["materials"]:
                if isinstance(entry, dict) and entry.get("uid"):
                    self._materials[str(entry["uid"])] = entry
        elif scan and isinstance(scan.get("materials"), list):
            for entry in scan["materials"]:
                if isinstance(entry, dict) and entry.get("uid"):
                    self._materials[str(entry["uid"])] = entry
        self._load_flex_plan(plan)
        preview = scan.get("model_preview") if isinstance(scan, dict) else None
        triangles = preview.get("triangles") if isinstance(preview, dict) else []
        self._triangles = [entry for entry in triangles if isinstance(entry, dict)] if isinstance(triangles, list) else []
        self._release_material_textures()
        self._gl_error = ""
        self._gl_error_count = 0
        self._build_material_arrays()
        self._prepare_scene()
        self.update()
        self._emit_stats()

    def update_material_entry(self, uid: str, **updates: object) -> None:
        uid = str(uid or "")
        if not uid:
            return
        entry = self._materials.get(uid)
        if entry is None:
            return
        entry.update(updates)
        self.update()
        self._emit_stats()

    def set_visible_bodygroups(self, bodygroups: set[str] | list[str] | tuple[str, ...]) -> None:
        enabled = {str(name) for name in bodygroups if str(name)}
        changed = False
        for entry in self._materials.values():
            bodygroup = str(entry.get("bodygroup") or "")
            if not bodygroup:
                continue
            keep = bodygroup in enabled
            if bool(entry.get("keep", True)) != keep:
                entry["keep"] = keep
                changed = True
            if not bool(entry.get("render_when_highlighted", False)):
                entry["render_when_highlighted"] = True
                changed = True
        if changed:
            self.update()
            self._emit_stats()

    def set_model_preview_data(self, model_preview: dict[str, object] | None) -> None:
        if not self.scan:
            self.set_material_data({"materials": [], "material_count": 0, "model_preview": model_preview or {}}, self.plan)
            return
        updated_scan = dict(self.scan)
        updated_scan["model_preview"] = model_preview or {}
        self.scan = updated_scan
        triangles = updated_scan.get("model_preview", {}).get("triangles") if isinstance(updated_scan.get("model_preview"), dict) else []
        self._triangles = [entry for entry in triangles if isinstance(entry, dict)] if isinstance(triangles, list) else []
        self._build_material_arrays()
        self._prepare_scene()
        self.update()
        self._emit_stats()

    def set_collision_overlay(self, collision_preview: dict[str, object] | None) -> None:
        grouped: dict[str, list[list[float]]] = {}
        colors: dict[str, tuple[float, float, float, float]] = {}
        labels: dict[str, str] = {}
        triangles = collision_preview.get("triangles") if isinstance(collision_preview, dict) else []
        if isinstance(triangles, list):
            for index, triangle in enumerate(triangles, start=1):
                if not isinstance(triangle, dict):
                    continue
                raw_points = triangle.get("points")
                if not isinstance(raw_points, list) or len(raw_points) < 3:
                    continue
                points: list[list[float]] = []
                for raw in raw_points[:3]:
                    if isinstance(raw, list) and len(raw) >= 3:
                        points.append([float(raw[0]), float(raw[1]), float(raw[2])])
                if len(points) != 3:
                    continue
                uid = str(triangle.get("uid") or triangle.get("bone") or f"collision_{index:03d}")
                labels[uid] = str(triangle.get("bone") or uid)
                grouped.setdefault(uid, []).extend([points[0], points[1], points[1], points[2], points[2], points[0]])
                raw_color = triangle.get("color")
                if isinstance(raw_color, list) and len(raw_color) >= 3:
                    colors[uid] = (
                        max(0.0, min(1.0, float(raw_color[0]))),
                        max(0.0, min(1.0, float(raw_color[1]))),
                        max(0.0, min(1.0, float(raw_color[2]))),
                        max(0.2, min(1.0, float(raw_color[3]) if len(raw_color) > 3 else 0.82)),
                    )
        self._collision_lines = {
            uid: np.ascontiguousarray(np.asarray(points, dtype=np.float32).reshape((-1, 3)), dtype=np.float32)
            for uid, points in grouped.items()
            if len(points) >= 2
        }
        self._collision_colors = colors
        self._collision_labels = labels
        self._prepare_scene()
        self.update()
        self._emit_stats()

    def set_hovered_collision(self, uid: str) -> None:
        new_uid = str(uid or "")
        if new_uid == self._hovered_collision_uid:
            return
        self._hovered_collision_uid = new_uid
        self.update()

    def set_highlighted_collisions(self, uids: set[str] | list[str] | tuple[str, ...]) -> None:
        next_uids = {str(uid) for uid in uids if uid}
        if next_uids == self._highlighted_collision_uids:
            return
        self._highlighted_collision_uids = next_uids
        self.update()

    def set_bone_overlay(self, bone_preview: dict[str, object] | list[dict[str, object]] | None) -> None:
        bones = bone_preview.get("bones") if isinstance(bone_preview, dict) else bone_preview
        if not isinstance(bones, list):
            bones = []
        positions: dict[str, list[float]] = {}
        parents: dict[str, str] = {}
        for raw in bones:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or raw.get("uid") or "")
            head = raw.get("head")
            if name and isinstance(head, list) and len(head) >= 3:
                positions[name] = [float(head[0]), float(head[1]), float(head[2])]
                parents[name] = str(raw.get("parent") or "")
        lines: dict[str, list[list[float]]] = {}
        labels: dict[str, str] = {}
        for name, head in positions.items():
            parent = parents.get(name, "")
            if parent and parent in positions:
                lines[name] = [positions[parent], head]
                labels[name] = name
        self._bone_lines = {
            uid: np.ascontiguousarray(np.asarray(points, dtype=np.float32).reshape((-1, 3)), dtype=np.float32)
            for uid, points in lines.items()
            if len(points) >= 2
        }
        self._bone_labels = labels
        self._prepare_scene()
        self.update()
        self._emit_stats()

    def set_hovered_bone_overlay(self, uid: str) -> None:
        new_uid = str(uid or "")
        if new_uid == self._hovered_bone_uid:
            return
        self._hovered_bone_uid = new_uid
        self.update()

    def set_highlighted_bone_overlay(self, uids: set[str] | list[str] | tuple[str, ...]) -> None:
        next_uids = {str(uid) for uid in uids if uid}
        if next_uids == self._highlighted_bone_uids:
            return
        self._highlighted_bone_uids = next_uids
        self.update()

    def set_bone_group_color_overlay(self, colors: dict[str, tuple[float, float, float]] | None) -> None:
        """Per-bone highlight colors, e.g. one distinct color per collision group."""
        next_colors: dict[str, tuple[float, float, float]] = {}
        for uid, rgb in (colors or {}).items():
            try:
                next_colors[str(uid)] = (float(rgb[0]), float(rgb[1]), float(rgb[2]))
            except Exception:
                continue
        if next_colors == self._bone_group_colors:
            return
        self._bone_group_colors = next_colors
        self.update()

    def set_flex_plan(self, plan: dict[str, object] | None) -> None:
        self.plan = plan
        self._load_flex_plan(plan)
        self._refresh_flex_vertex_data()
        self.update()

    def _plan_float(self, value: object, default: float) -> float:
        if value is None or value == "":
            return float(default)
        return float(value)

    def _load_flex_plan(self, plan: dict[str, object] | None) -> None:
        self._flex_sources = {}
        self._flex_rest_values = {}
        self._flex_scales = {}
        self._enabled_flex_uids = set()
        entries = []
        if plan and isinstance(plan.get("flexes"), list):
            entries = [entry for entry in plan["flexes"] if isinstance(entry, dict) and entry.get("uid")]
        by_uid = {str(entry["uid"]): entry for entry in entries}
        for entry in entries:
            uid = str(entry["uid"])
            enabled = bool(entry.get("enabled", True)) and str(entry.get("action") or "keep") != "remove"
            try:
                rest_value = self._plan_float(entry.get("rest_value", 0.0), 0.0)
                max_value = self._plan_float(entry.get("max_amplitude", 1.0), 1.0)
            except Exception:
                rest_value = 0.0
                max_value = 1.0
            self._flex_rest_values[uid] = rest_value
            self._flex_scales[uid] = max_value - rest_value
            if enabled:
                self._enabled_flex_uids.add(uid)
            sources: list[tuple[str, float]] = []
            for source in entry.get("source_flexes", []):
                if not isinstance(source, dict) or not source.get("uid"):
                    continue
                source_uid = str(source["uid"])
                source_entry = by_uid.get(source_uid)
                try:
                    if source_entry is not None:
                        source_rest = self._plan_float(source_entry.get("rest_value", 0.0), 0.0)
                        source_max = self._plan_float(source_entry.get("max_amplitude", 1.0), 1.0)
                        weight = source_max - source_rest
                    else:
                        raw_weight = source.get("weight", None)
                        if raw_weight not in (None, ""):
                            weight = float(raw_weight)
                        else:
                            source_rest = self._plan_float(source.get("rest_value", 0.0), 0.0)
                            source_max = self._plan_float(source.get("max_amplitude", 1.0), 1.0)
                            weight = source_max - source_rest
                except Exception:
                    weight = 1.0
                sources.append((source_uid, weight))
            if sources:
                self._flex_sources[uid] = sources

    def set_active_flex(self, uid: str, value: float = 1.0) -> None:
        self._active_flex_uid = str(uid or "")
        try:
            self._active_flex_value = float(value)
        except Exception:
            self._active_flex_value = 1.0
        self._refresh_flex_vertex_data()
        self.update()

    def set_isolated_bodygroup(self, bodygroup: str) -> None:
        self._isolated_bodygroup = str(bodygroup or "")
        self.update()

    def set_selected_material(self, uid: str) -> None:
        self._selected_uid = str(uid or "")
        self.update()

    def set_hovered_material(self, uid: str) -> None:
        new_uid = str(uid or "")
        if new_uid == self._hovered_uid:
            return
        self._hovered_uid = new_uid
        self._preview_log(f"hover uid={new_uid!r} name={self._materials.get(new_uid, {}).get('material_name', '')!r}")
        self._queue_hover_update()

    def set_highlighted_materials(self, uids: set[str] | list[str] | tuple[str, ...]) -> None:
        self._highlighted_uids = {str(uid) for uid in uids if uid}
        self.update()

    def set_wireframe_visible(self, visible: bool) -> None:
        self._wireframe = bool(visible)
        self.update()

    def set_respect_alpha(self, visible: bool) -> None:
        self._respect_alpha = bool(visible)
        self.update()

    def set_bones_on_top(self, visible: bool) -> None:
        """Draw the bone skeleton overlay in front of the mesh (manual Step 8)."""
        self._bones_on_top = bool(visible)
        self.update()

    def set_bone_labels_visible(self, visible: bool) -> None:
        """Draw per-bone name labels over the model (manual Step 8)."""
        self._show_bone_labels = bool(visible)
        self.update()

    def set_front_bodygroup(self, bodygroup: str) -> None:
        """Draw one bodygroup in front of everything without hiding the rest.

        Unlike set_isolated_bodygroup (a hard hide used by the flex tab), this
        keeps the whole character visible and re-draws the named bodygroup last
        with depth-testing disabled so it pops to the front (manual Step 8).
        """
        self._front_bodygroup = str(bodygroup or "")
        self.update()

    def set_constraint_arcs(self, arcs: dict[str, object] | None) -> None:
        """Set per-axis rotation-limit arc polylines (world space) for Step 14.

        ``arcs`` maps an axis/label key to an (N,3) array of points drawn as a
        depth-disabled line strip. Render-only; not added to the scene bounds.
        """
        cleaned: dict[str, np.ndarray] = {}
        if isinstance(arcs, dict):
            for key, value in arcs.items():
                try:
                    array = np.asarray(value, dtype=np.float32)
                except Exception:
                    continue
                if array.ndim == 2 and array.shape[0] >= 2 and array.shape[1] >= 3:
                    cleaned[str(key)] = array[:, :3].copy()
        self._constraint_arcs = cleaned
        self.update()

    def reset_view(self, direction: str = "Front") -> None:
        self._view_yaw, self._view_pitch = self.VIEW_DIRECTIONS.get(direction, self.VIEW_DIRECTIONS["Front"])
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self.update()

    def _build_material_arrays(self) -> None:
        grouped: dict[str, list[list[float]]] = {}
        grouped_uvs: dict[str, list[list[float]]] = {}
        grouped_flex_deltas: dict[str, dict[str, list[tuple[int, list[list[float]]]]]] = {}
        texture_paths: dict[str, Path] = {}
        # exists() costs a filesystem stat; cache it per unique texture string so
        # large triangle previews do not stat the same path hundreds of thousands of times.
        texture_path_cache: dict[str, Path | None] = {}
        for triangle in self._triangles:
            uid = str(triangle.get("material_uid") or "")
            raw_points = triangle.get("points")
            raw_uvs = triangle.get("uvs")
            if not uid or not isinstance(raw_points, list) or len(raw_points) < 3:
                continue
            bucket = grouped.setdefault(uid, [])
            uv_bucket = grouped_uvs.setdefault(uid, [])
            triangle_points: list[list[float]] = []
            for raw in raw_points[:3]:
                if isinstance(raw, list) and len(raw) >= 3:
                    triangle_points.append([float(raw[0]), float(raw[1]), float(raw[2])])
            if len(triangle_points) != 3:
                continue
            start_count = len(bucket)
            bucket.extend(triangle_points)
            if isinstance(raw_uvs, list) and len(raw_uvs) >= 3:
                for raw_uv in raw_uvs[:3]:
                    if isinstance(raw_uv, list) and len(raw_uv) >= 2:
                        uv_bucket.append([float(raw_uv[0]), float(raw_uv[1])])
                    else:
                        uv_bucket.append([0.0, 0.0])
            else:
                uv_bucket.extend([[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]])
            raw_flex_deltas = triangle.get("flex_deltas")
            if isinstance(raw_flex_deltas, dict):
                for flex_uid, raw_deltas in raw_flex_deltas.items():
                    if not isinstance(raw_deltas, list) or len(raw_deltas) < 3:
                        continue
                    deltas: list[list[float]] = []
                    for raw_delta in raw_deltas[:3]:
                        if isinstance(raw_delta, list) and len(raw_delta) >= 3:
                            deltas.append([float(raw_delta[0]), float(raw_delta[1]), float(raw_delta[2])])
                        else:
                            deltas.append([0.0, 0.0, 0.0])
                    flex_uid = str(flex_uid)
                    grouped_flex_deltas.setdefault(flex_uid, {}).setdefault(uid, []).append((start_count, deltas))
            texture_raw = str(triangle.get("texture_path") or self._materials.get(uid, {}).get("base_color_path") or "")
            if texture_raw:
                if texture_raw not in texture_path_cache:
                    candidate = Path(texture_raw)
                    texture_path_cache[texture_raw] = candidate if candidate.exists() else None
                path = texture_path_cache[texture_raw]
                if path is not None:
                    texture_paths[uid] = path
        self._material_positions = {
            uid: np.asarray(points, dtype=np.float32).reshape((-1, 3))
            for uid, points in grouped.items()
            if len(points) >= 3
        }
        self._material_uvs = {}
        for uid, points in self._material_positions.items():
            uvs = grouped_uvs.get(uid, [])
            if len(uvs) == len(points):
                self._material_uvs[uid] = np.asarray(uvs, dtype=np.float32).reshape((-1, 2))
        self._material_base_vertex_data = {}
        for uid, positions in self._material_positions.items():
            uvs = self._material_uvs.get(uid)
            if uvs is None or len(uvs) != len(positions):
                uvs = np.zeros((len(positions), 2), dtype=np.float32)
            interleaved = np.empty((len(positions), 5), dtype=np.float32)
            interleaved[:, 0:3] = positions
            interleaved[:, 3:5] = uvs
            self._material_base_vertex_data[uid] = np.ascontiguousarray(interleaved, dtype=np.float32)
        self._material_flex_deltas = {}
        for flex_uid, by_material in grouped_flex_deltas.items():
            converted: dict[str, np.ndarray] = {}
            for uid, spans in by_material.items():
                positions = self._material_positions.get(uid)
                if positions is None:
                    continue
                delta_array = np.zeros((len(positions), 3), dtype=np.float32)
                for start, deltas in spans:
                    if start < 0 or start + 3 > len(delta_array):
                        continue
                    delta_array[start : start + 3, :] = np.asarray(deltas[:3], dtype=np.float32).reshape((3, 3))
                converted[uid] = delta_array
            if converted:
                self._material_flex_deltas[flex_uid] = converted
        self._material_texture_paths = texture_paths
        self._refresh_flex_vertex_data()

    def _combined_flex_delta(self, uid: str, material_uid: str) -> np.ndarray | None:
        if not uid:
            return None
        direct = self._material_flex_deltas.get(uid, {}).get(material_uid)
        sources = self._flex_sources.get(uid, [])
        if not sources:
            return direct
        base = self._material_positions.get(material_uid)
        if base is None:
            return direct
        total = np.zeros((len(base), 3), dtype=np.float32)
        used = False
        if direct is not None and len(direct) == len(total):
            total += direct
            used = True
        for source_uid, weight in sources:
            delta = self._material_flex_deltas.get(source_uid, {}).get(material_uid)
            if delta is not None and len(delta) == len(total):
                total += delta * float(weight)
                used = True
        return total if used else None

    def _refresh_flex_vertex_data(self) -> None:
        self._material_vertex_data = {}
        active_uid = str(self._active_flex_uid or "")
        active_value = float(self._active_flex_value)
        active_scale = float(self._flex_scales.get(active_uid, 1.0))
        for uid, base_data in self._material_base_vertex_data.items():
            deformed = np.array(base_data, copy=True)
            for flex_uid in self._enabled_flex_uids:
                rest_value = float(self._flex_rest_values.get(flex_uid, 0.0))
                if abs(rest_value) <= 1e-8:
                    continue
                rest_delta = self._combined_flex_delta(flex_uid, uid)
                if rest_delta is not None and len(rest_delta) == len(deformed):
                    deformed[:, 0:3] += rest_delta * rest_value
            if active_uid and abs(active_value) > 1e-8:
                active_delta = self._combined_flex_delta(active_uid, uid)
                if active_delta is not None and len(active_delta) == len(deformed):
                    deformed[:, 0:3] += active_delta * active_value * active_scale
            self._material_vertex_data[uid] = np.ascontiguousarray(deformed, dtype=np.float32)

    def _queue_hover_update(self) -> None:
        if self._hover_update_queued:
            return
        self._hover_update_queued = True
        QtCore.QTimer.singleShot(16, self._flush_hover_update)

    def _flush_hover_update(self) -> None:
        self._hover_update_queued = False
        self.update()

    def _preview_log(self, message: str) -> None:
        if not self._preview_debug_enabled:
            return
        try:
            self._preview_debug_log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with self._preview_debug_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{timestamp} {message}\n")
        except Exception:
            pass

    def _gl_debug_error(self, context: str) -> int:
        if not GL or not self._preview_debug_enabled:
            return 0
        try:
            error = int(GL.glGetError())
        except Exception as exc:
            self._preview_log(f"glGetError failed after {context}: {exc}")
            return 0
        if error:
            self._gl_error_count += 1
            self._preview_log(f"GL error after {context}: 0x{error:04x} count={self._gl_error_count}")
            if self._gl_error_count >= 3 and not self._textured_preview_disabled:
                self._textured_preview_disabled = True
                self._preview_log("Disabled textured material preview after repeated GL errors.")
        return error

    def _prepare_scene(self) -> None:
        points: list[np.ndarray] = []
        for positions in self._material_positions.values():
            if len(positions):
                points.append(positions.astype(np.float64))
        for positions in self._collision_lines.values():
            if len(positions):
                points.append(positions.astype(np.float64))
        for positions in self._bone_lines.values():
            if len(positions):
                points.append(positions.astype(np.float64))
        if not points:
            self._scene_center = np.zeros(3, dtype=np.float64)
            self._scene_extent = 1.0
            self._scene_mins = np.zeros(3, dtype=np.float64)
            self._scene_maxs = np.ones(3, dtype=np.float64)
            return
        array = np.vstack(points)
        mins = array.min(axis=0)
        maxs = array.max(axis=0)
        self._scene_mins = mins
        self._scene_maxs = maxs
        self._scene_center = (mins + maxs) * 0.5
        self._scene_extent = max(0.2, float(np.max(maxs - mins)))

    def set_height_reference(self, meters_per_unit: float, up_axis: int = 2, reference_m: float = HEIGHT_REFERENCE_METERS) -> None:
        self._height_meters_per_unit = max(0.0, float(meters_per_unit))
        self._height_up_axis = int(up_axis)
        self._height_reference_m = float(reference_m)
        self.update()

    def _projection_view_matrices(self) -> tuple[np.ndarray, np.ndarray]:
        width = max(1, self.width())
        height = max(1, self.height())
        aspect = width / height
        scene_extent = max(0.2, _scalar_float(self._scene_extent, 1.0))
        view_zoom = max(0.001, _scalar_float(self._view_zoom, 1.0))
        view_height = scene_extent / max(0.001, view_zoom * 0.78)
        view_width = view_height * aspect
        pixel_scale = min(width, height) * 0.78 * view_zoom / max(0.001, scene_extent)
        pan_x = -float(self._view_pan.x()) / max(0.001, pixel_scale)
        pan_y = float(self._view_pan.y()) / max(0.001, pixel_scale)
        left = -view_width * 0.5 + pan_x
        right = view_width * 0.5 + pan_x
        bottom = -view_height * 0.5 + pan_y
        top = view_height * 0.5 + pan_y
        near = -scene_extent * 5.0
        far = scene_extent * 5.0
        projection = np.array(
            [
                [2.0 / (right - left), 0.0, 0.0, -(right + left) / (right - left)],
                [0.0, 2.0 / (top - bottom), 0.0, -(top + bottom) / (top - bottom)],
                [0.0, 0.0, -2.0 / (far - near), -(far + near) / (far - near)],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        yaw_angle = _scalar_float(self._view_yaw, 0.0)
        pitch_angle = _scalar_float(self._view_pitch, 0.0)
        return projection, _upright_orbit_view_matrix(self._scene_center, yaw_angle, pitch_angle)

    def _project_np(self, positions: np.ndarray) -> tuple[list[QtCore.QPointF], np.ndarray]:
        projection, view = self._projection_view_matrices()
        homogeneous = np.concatenate([positions[:, :3], np.ones((len(positions), 1), dtype=np.float64)], axis=1)
        view_space = (view @ homogeneous.T).T
        clip = (projection @ view_space.T).T
        safe_w = np.where(np.abs(clip[:, 3]) <= 1e-8, 1.0, clip[:, 3])
        ndc = clip[:, :3] / safe_w[:, None]
        x = self.width() * (0.5 + ndc[:, 0] * 0.5)
        y = self.height() * (0.5 - ndc[:, 1] * 0.5)
        return [QtCore.QPointF(float(px), float(py)) for px, py in zip(x, y)], view_space[:, 2]

    def _material_visible(self, uid: str) -> bool:
        entry = self._materials.get(uid)
        if not entry:
            return True
        if self._isolated_bodygroup:
            bodygroup = str(entry.get("bodygroup") or entry.get("material_name") or entry.get("proposed_name") or "")
            if bodygroup != self._isolated_bodygroup:
                return False
        if bool(entry.get("keep", True)):
            return True
        return bool(entry.get("render_when_highlighted", False)) and self._material_is_highlighted(uid)

    def _material_highlight_group(self, uid: str) -> str:
        entry = self._materials.get(uid, {})
        return str(entry.get("highlight_group") or entry.get("bodygroup_uid") or uid)

    def _material_is_highlighted(self, uid: str) -> bool:
        if not uid:
            return False
        if uid == self._hovered_uid:
            return True
        group_uid = self._material_highlight_group(uid)
        return bool(
            (self._hovered_uid and group_uid == self._hovered_uid)
            or uid in self._highlighted_uids
            or group_uid in self._highlighted_uids
        )

    def _material_rgba(self, uid: str, highlighted: bool = False) -> tuple[float, float, float, float]:
        entry = self._materials.get(uid, {})
        raw_color = entry.get("preview_color") or entry.get("swatch") or [0.75, 0.75, 0.75, 1.0]
        if not isinstance(raw_color, list):
            raw_color = [0.75, 0.75, 0.75, 1.0]
        values = [float(raw_color[index]) if index < len(raw_color) else 1.0 for index in range(4)]
        if max(values[:3]) - min(values[:3]) < 0.08 and max(values[:3]) > 0.82:
            seed = uid + str(entry.get("base_color_key") or entry.get("material_name") or "")
            value = sum((offset + 1) * ord(char) for offset, char in enumerate(seed))
            values[:3] = colorsys.hsv_to_rgb((value % 360) / 360.0, 0.62, 0.86)
        raw_alpha = entry.get("alpha")
        try:
            # Explicit None/empty check: alpha of exactly 0.0 is valid (fully
            # transparent) and must not fall back to opaque.
            alpha = values[3] if raw_alpha is None or raw_alpha == "" else float(raw_alpha)
        except (TypeError, ValueError):
            alpha = values[3]
        if not self._respect_alpha:
            alpha = 1.0
        elif alpha <= 0.02:
            alpha = 0.04
        if highlighted:
            alpha = max(alpha, 0.82)
            values = [min(1.0, value * 1.25 + 0.08) for value in values]
        return (
            max(0.0, min(1.0, values[0])),
            max(0.0, min(1.0, values[1])),
            max(0.0, min(1.0, values[2])),
            max(0.02, min(1.0, alpha)),
        )

    def _material_color(self, uid: str, highlighted: bool = False) -> QtGui.QColor:
        red, green, blue, alpha = self._material_rgba(uid, highlighted)
        return QtGui.QColor.fromRgbF(red, green, blue, alpha)

    def initializeGL(self) -> None:
        context = self.context()
        if context is not None:
            # Free GPU textures when the context goes away (widget teardown or
            # context recreation while docking/reparenting).
            context.aboutToBeDestroyed.connect(self._on_context_about_to_be_destroyed, QtCore.Qt.ConnectionType.DirectConnection)
        # A fresh context invalidates previously created texture names.
        self._material_texture_ids = {}
        if GL and self._gl_backend_active():
            try:
                self.makeCurrent()
            except Exception as exc:
                self._gl_error = str(exc)
                return
            try:
                GL.glClearColor(0.08, 0.09, 0.10, 1.0)
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glDisable(GL.GL_CULL_FACE)
            except Exception as exc:
                self._gl_error = str(exc)

    def _delete_gl_material_textures(self) -> None:
        """Delete GL texture objects; the GL context must be current."""
        ids = [texture_id for texture_id in self._material_texture_ids.values() if texture_id]
        self._material_texture_ids = {}
        if ids and GL:
            try:
                GL.glDeleteTextures(ids)
            except Exception:
                pass

    def _release_material_textures(self) -> None:
        if not self._material_texture_ids:
            return
        context = self.context()
        if GL and context is not None and context.isValid():
            try:
                self.makeCurrent()
                self._delete_gl_material_textures()
            finally:
                self.doneCurrent()
        else:
            self._material_texture_ids = {}

    def _on_context_about_to_be_destroyed(self) -> None:
        try:
            self.makeCurrent()
            self._delete_gl_material_textures()
        except Exception:
            self._material_texture_ids = {}
        finally:
            try:
                self.doneCurrent()
            except Exception:
                pass

    def _has_drawable_content(self) -> bool:
        return bool(self._material_positions or self._collision_lines or self._bone_lines)

    def _gl_backend_active(self) -> bool:
        """True while the widget may issue PyOpenGL calls: PyOpenGL imported,
        the startup probe passed, no local failure latch, and the widget is
        actually GL-backed (forced-CPU mode uses a plain QWidget base)."""
        return GL is not None and gl_runtime_usable() and not self._gl_disabled and isinstance(self, QOpenGLWidget)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        if isinstance(self, QOpenGLWidget):
            super().paintEvent(event)  # drives paintGL
            return
        self._render_frame()

    def paintGL(self) -> None:
        self._render_frame()

    def _render_frame(self) -> None:
        self._texture_uploads_this_frame = 0
        self._texture_uploads_pending = False
        gl_active = self._gl_backend_active()
        if gl_active:
            try:
                GL.glViewport(0, 0, *self._gl_viewport_size())
                GL.glClearColor(0.08, 0.09, 0.10, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
                if self._has_drawable_content():
                    self._draw_mesh_gl()
                    self._gl_error = ""
                self._gl_failures = 0
            except Exception as exc:
                self._gl_error = str(exc)
                self._gl_failures += 1
                if self._gl_failures >= 2:
                    # PyOpenGL cannot drive this Qt context (issue #129): stop
                    # issuing GL calls and render on the CPU from now on.
                    self._gl_disabled = True

        painter = QtGui.QPainter(self)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            if not self._has_drawable_content():
                painter.fillRect(self.rect(), QtGui.QColor(22, 24, 27))
                painter.setPen(QtGui.QColor(230, 230, 230))
                painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self._empty_message)
                return
            if not gl_active or self._gl_error:
                # CPU fallback: draw the whole scene (materials, collision,
                # bones, arcs) with QPainter so Step 8/14 bone/physics editing
                # stays usable without working OpenGL (issue #129).
                painter.fillRect(self.rect(), QtGui.QColor(22, 24, 27))
                try:
                    self._draw_scene_painter(painter)
                except Exception as exc:
                    self._preview_log(f"CPU fallback draw failed: {exc}")
                    painter.setPen(QtGui.QColor(255, 210, 120))
                    painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self._opengl_unavailable_message)
            self._draw_overlay(painter)
        finally:
            painter.end()
        if self._texture_uploads_pending:
            # Stream the remaining texture decodes/uploads over upcoming frames.
            QtCore.QTimer.singleShot(0, self.update)

    def _gl_viewport_size(self) -> tuple[int, int]:
        dpr = max(1.0, float(self.devicePixelRatioF()))
        return max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr)))

    # --- CPU (QPainter) fallback renderer (issue #129) --------------------

    def _cpu_interactive(self) -> bool:
        return self._drag_active or (time.monotonic() - self._last_wheel_time) < 0.25

    def _cpu_triangle_budget(self) -> int:
        budget = _cpu_preview_budget()
        # Rotating/zooming redraws every event; drop detail to stay responsive
        # and repaint at full quality once the interaction settles.
        return max(1500, budget // 4) if self._cpu_interactive() else budget

    def _cpu_project(self, positions: np.ndarray, pv: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Project (N,3) world positions to screen x/y plus NDC depth."""
        pos = np.asarray(positions, dtype=np.float64)[:, :3]
        homogeneous = np.concatenate([pos, np.ones((len(pos), 1), dtype=np.float64)], axis=1)
        clip = (pv @ homogeneous.T).T
        safe_w = np.where(np.abs(clip[:, 3]) <= 1e-8, 1.0, clip[:, 3])
        ndc = clip[:, :3] / safe_w[:, None]
        sx = self.width() * (0.5 + ndc[:, 0] * 0.5)
        sy = self.height() * (0.5 - ndc[:, 1] * 0.5)
        return sx, sy, ndc[:, 2]

    def _draw_scene_painter(self, painter: QtGui.QPainter) -> None:
        """Render the whole scene with QPainter when PyOpenGL is unavailable:
        depth-sorted solid-color materials, highlight outlines, the
        front-bodygroup pass, collision lines, the bone overlay, and the
        Step-14 constraint arcs (issue #129)."""
        projection, view = self._projection_view_matrices()
        pv = projection @ view
        self._painter_mesh_pass(painter, pv)
        self._painter_front_bodygroup_pass(painter, pv)
        self._painter_collision_pass(painter, pv)
        self._painter_bone_pass(painter, pv)
        self._painter_arc_pass(painter, pv)
        if self._cpu_interactive():
            # Repaint at full quality shortly after the interaction settles.
            QtCore.QTimer.singleShot(300, self.update)

    def _painter_mesh_pass(self, painter: QtGui.QPainter, pv: np.ndarray) -> None:
        blocks: list[np.ndarray] = []
        color_rows: list[np.ndarray] = []
        highlight_rows: list[np.ndarray] = []
        for uid, vertex_data in self._material_vertex_data.items():
            if not self._material_visible(uid) or len(vertex_data) < 3:
                continue
            face_count = len(vertex_data) // 3
            blocks.append(np.asarray(vertex_data[: face_count * 3, :3], dtype=np.float64))
            highlighted = self._material_is_highlighted(uid)
            color = np.array(self._material_rgba(uid, highlighted), dtype=np.float32)
            color_rows.append(np.repeat(color[None, :], face_count, axis=0))
            highlight_rows.append(np.full(face_count, highlighted, dtype=bool))
        if not blocks:
            return
        positions = np.vstack(blocks)
        colors = np.vstack(color_rows)
        highlighted_faces = np.concatenate(highlight_rows)
        sx, sy, depth = self._cpu_project(positions, pv)
        fx = sx.reshape(-1, 3)
        fy = sy.reshape(-1, 3)
        face_depth = depth.reshape(-1, 3).mean(axis=1)
        face_total = len(fx)
        area = np.abs(
            (fx[:, 1] - fx[:, 0]) * (fy[:, 2] - fy[:, 0]) - (fx[:, 2] - fx[:, 0]) * (fy[:, 1] - fy[:, 0])
        )
        budget = self._cpu_triangle_budget()
        if face_total > budget:
            keep = np.argpartition(area, face_total - budget)[-budget:]
        else:
            keep = np.arange(face_total)
        order = keep[np.argsort(face_depth[keep])[::-1]]  # far-to-near

        rgb = np.clip(colors[:, :3] * 255.0, 0.0, 255.0).astype(np.int32)
        alpha = np.clip(colors[:, 3] * 255.0, 0.0, 255.0).astype(np.int32)
        fxl = fx.tolist()
        fyl = fy.tolist()
        point = QtCore.QPointF
        polygon_cls = QtGui.QPolygonF
        wireframe = self._wireframe
        painter.save()
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            if wireframe:
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            else:
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
            last_key: tuple[int, int, int, int] | None = None
            for face_index in order.tolist():
                px = fxl[face_index]
                py = fyl[face_index]
                color_key = (rgb[face_index, 0], rgb[face_index, 1], rgb[face_index, 2], alpha[face_index])
                if color_key != last_key:
                    last_key = color_key
                    color = QtGui.QColor(int(color_key[0]), int(color_key[1]), int(color_key[2]), int(color_key[3]))
                    if wireframe:
                        painter.setPen(QtGui.QPen(color, 1.0))
                    else:
                        painter.setBrush(color)
                painter.drawConvexPolygon(
                    polygon_cls([point(px[0], py[0]), point(px[1], py[1]), point(px[2], py[2])])
                )
            highlight_kept = keep[highlighted_faces[keep]] if not wireframe else np.zeros(0, dtype=np.int64)
            if len(highlight_kept):
                # Yellow outline pass matching the GL highlight wireframe.
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.setPen(QtGui.QPen(QtGui.QColor(255, 219, 56, 255), 1.2))
                for face_index in highlight_kept.tolist():
                    px = fxl[face_index]
                    py = fyl[face_index]
                    painter.drawPolygon(
                        polygon_cls([point(px[0], py[0]), point(px[1], py[1]), point(px[2], py[2])])
                    )
        finally:
            painter.restore()

    def _painter_front_bodygroup_pass(self, painter: QtGui.QPainter, pv: np.ndarray) -> None:
        if not self._front_bodygroup:
            return
        painter.save()
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            point = QtCore.QPointF
            polygon_cls = QtGui.QPolygonF
            for uid, vertex_data in self._material_vertex_data.items():
                if self._material_bodygroup(uid) != self._front_bodygroup or not self._material_visible(uid):
                    continue
                face_count = len(vertex_data) // 3
                if face_count == 0:
                    continue
                red, green, blue, alpha = self._material_rgba(uid, True)
                painter.setBrush(QtGui.QColor.fromRgbF(red, green, blue, max(0.9, alpha)))
                sx, sy, depth = self._cpu_project(vertex_data[: face_count * 3, :3], pv)
                fx = sx.reshape(-1, 3)
                fy = sy.reshape(-1, 3)
                face_depth = depth.reshape(-1, 3).mean(axis=1)
                budget = self._cpu_triangle_budget()
                if face_count > budget:
                    area = np.abs(
                        (fx[:, 1] - fx[:, 0]) * (fy[:, 2] - fy[:, 0]) - (fx[:, 2] - fx[:, 0]) * (fy[:, 1] - fy[:, 0])
                    )
                    keep = np.argpartition(area, face_count - budget)[-budget:]
                else:
                    keep = np.arange(face_count)
                fxl = fx.tolist()
                fyl = fy.tolist()
                for face_index in keep[np.argsort(face_depth[keep])[::-1]].tolist():
                    px = fxl[face_index]
                    py = fyl[face_index]
                    painter.drawConvexPolygon(
                        polygon_cls([point(px[0], py[0]), point(px[1], py[1]), point(px[2], py[2])])
                    )
        finally:
            painter.restore()

    def _painter_collision_pass(self, painter: QtGui.QPainter, pv: np.ndarray) -> None:
        if not self._collision_lines:
            return
        painter.save()
        try:
            for uid, lines in self._collision_lines.items():
                if len(lines) < 2:
                    continue
                highlighted = uid == self._hovered_collision_uid or uid in self._highlighted_collision_uids
                if highlighted:
                    pen = QtGui.QPen(QtGui.QColor.fromRgbF(1.0, 0.78, 0.08, 1.0), 3.0)
                else:
                    color = self._collision_colors.get(uid, (0.2, 0.75, 1.0, 0.85))
                    pen = QtGui.QPen(
                        QtGui.QColor.fromRgbF(float(color[0]), float(color[1]), float(color[2]), float(color[3])), 1.6
                    )
                painter.setPen(pen)
                sx, sy, _depth = self._cpu_project(lines, pv)
                segments = [
                    QtCore.QLineF(float(sx[index]), float(sy[index]), float(sx[index + 1]), float(sy[index + 1]))
                    for index in range(0, (len(lines) // 2) * 2, 2)
                ]
                if segments:
                    painter.drawLines(segments)
        finally:
            painter.restore()

    def _painter_bone_pass(self, painter: QtGui.QPainter, pv: np.ndarray) -> None:
        if not self._bone_lines:
            return
        painter.save()
        try:
            for uid, lines in self._bone_lines.items():
                if len(lines) < 2:
                    continue
                group_color = self._bone_group_colors.get(uid)
                highlighted = uid == self._hovered_bone_uid or uid in self._highlighted_bone_uids or group_color is not None
                if highlighted:
                    width = BONE_HIGHLIGHT_LINE_WIDTH
                    point_radius = BONE_HIGHLIGHT_POINT_SIZE * 0.5
                    if group_color is not None and uid != self._hovered_bone_uid:
                        color = QtGui.QColor.fromRgbF(group_color[0], group_color[1], group_color[2], 1.0)
                    else:
                        color = QtGui.QColor.fromRgbF(1.0, 0.72, 0.05, 1.0)
                else:
                    width = BONE_OVERLAY_LINE_WIDTH
                    point_radius = BONE_OVERLAY_POINT_SIZE * 0.5
                    color = QtGui.QColor.fromRgbF(0.78, 0.84, 0.94, 0.86)
                sx, sy, _depth = self._cpu_project(lines, pv)
                painter.setPen(QtGui.QPen(color, width))
                for index in range(0, (len(lines) // 2) * 2, 2):
                    painter.drawLine(
                        QtCore.QPointF(float(sx[index]), float(sy[index])),
                        QtCore.QPointF(float(sx[index + 1]), float(sy[index + 1])),
                    )
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(color)
                for index in range(len(lines)):
                    painter.drawEllipse(QtCore.QPointF(float(sx[index]), float(sy[index])), point_radius, point_radius)
        finally:
            painter.restore()

    def _painter_arc_pass(self, painter: QtGui.QPainter, pv: np.ndarray) -> None:
        if not self._constraint_arcs:
            return
        axis_colors = {
            "x": (1.0, 0.38, 0.36, 1.0),
            "y": (0.42, 1.0, 0.48, 1.0),
            "z": (0.45, 0.62, 1.0, 1.0),
        }
        painter.save()
        try:
            for key, arr in self._constraint_arcs.items():
                if arr is None or len(arr) < 2:
                    continue
                color = (0.86, 0.88, 0.94, 1.0)
                lowered = str(key).lower()
                for prefix, value in axis_colors.items():
                    if lowered.startswith(prefix):
                        color = value
                        break
                sx, sy, _depth = self._cpu_project(arr, pv)
                painter.setPen(QtGui.QPen(QtGui.QColor.fromRgbF(*color), 2.6))
                painter.drawPolyline(
                    QtGui.QPolygonF([QtCore.QPointF(float(x), float(y)) for x, y in zip(sx, sy)])
                )
        finally:
            painter.restore()

    def _load_texture_image(self, path: Path) -> QtGui.QImage:
        image = QtGui.QImage(str(path))
        if image.isNull():
            try:
                from PIL import Image

                with Image.open(path) as pil_image:
                    rgba = pil_image.convert("RGBA")
                    data = rgba.tobytes("raw", "RGBA")
                    image = QtGui.QImage(data, rgba.width, rgba.height, QtGui.QImage.Format.Format_RGBA8888).copy()
            except Exception:
                image = QtGui.QImage()
        if not image.isNull():
            image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888).mirrored(False, True)
        return image

    def _texture_id_for_uid(self, uid: str) -> int:
        if uid in self._material_texture_ids:
            return self._material_texture_ids[uid]
        if not GL:
            return 0
        if self._texture_uploads_this_frame >= self.MAX_TEXTURE_UPLOADS_PER_FRAME:
            # Decode/upload at most a few textures per frame so the first paint
            # does not freeze on decoding many large images; the uid stays
            # uncached and is retried on the requeued update().
            self._texture_uploads_pending = True
            return 0
        self._texture_uploads_this_frame += 1
        path = self._material_texture_paths.get(uid)
        if path is None or not path.exists():
            self._material_texture_ids[uid] = 0
            self._preview_log(f"texture missing uid={uid!r} path={str(path) if path else ''!r}")
            return 0
        image = self._load_texture_image(path)
        if image.isNull():
            self._material_texture_ids[uid] = 0
            self._preview_log(f"texture load failed uid={uid!r} path={str(path)!r}")
            return 0
        try:
            texture_id = int(GL.glGenTextures(1))
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
            bits = image.constBits()
            data = bits.tobytes() if hasattr(bits, "tobytes") else bytes(bits)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, image.width(), image.height(), 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self._material_texture_ids[uid] = texture_id
            self._preview_log(f"texture loaded uid={uid!r} id={texture_id} size={image.width()}x{image.height()} path={str(path)!r}")
            self._gl_debug_error(f"texture upload {uid}")
            return texture_id
        except Exception as exc:
            self._material_texture_ids[uid] = 0
            self._preview_log(f"texture upload failed uid={uid!r} path={str(path)!r}: {exc}")
            return 0

    def _draw_mesh_gl(self) -> None:
        if not GL:
            return
        projection, view = self._projection_view_matrices()
        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glLoadMatrixf(np.ascontiguousarray(projection.T, dtype=np.float32))
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glLoadMatrixf(np.ascontiguousarray(view.T, dtype=np.float32))

        try:
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
        except Exception:
            pass

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE if self._wireframe else GL.GL_FILL)
        stride = 5 * 4
        for uid, vertex_data in self._material_vertex_data.items():
            if not self._material_visible(uid) or len(vertex_data) < 3:
                continue
            hovered = self._material_is_highlighted(uid)
            red, green, blue, alpha = self._material_rgba(uid, hovered)
            base_ptr = int(vertex_data.ctypes.data)
            GL.glVertexPointer(3, GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr))
            texture_id = 0 if hovered or self._wireframe or self._textured_preview_disabled else self._texture_id_for_uid(uid)
            if texture_id:
                GL.glEnable(GL.GL_TEXTURE_2D)
                GL.glBindTexture(GL.GL_TEXTURE_2D, texture_id)
                GL.glEnableClientState(GL.GL_TEXTURE_COORD_ARRAY)
                GL.glTexCoordPointer(2, GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr + 12))
                GL.glColor4f(1.0, 1.0, 1.0, alpha)
                draw_mode = "textured"
            else:
                GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
                GL.glDisable(GL.GL_TEXTURE_2D)
                GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
                GL.glColor4f(red, green, blue, alpha)
                draw_mode = "solid-hover" if hovered else "solid"
            self._preview_log(f"draw uid={uid!r} mode={draw_mode} vertices={len(vertex_data)} texture={texture_id}")
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, int(len(vertex_data)))
            self._gl_debug_error(f"draw {uid} {draw_mode}")

        highlighted_uids = {
            uid
            for uid in self._material_vertex_data
            if self._material_is_highlighted(uid)
        }
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glLineWidth(2.2)
        for uid in highlighted_uids:
            vertex_data = self._material_vertex_data.get(uid)
            if vertex_data is None or len(vertex_data) < 3 or not self._material_visible(uid):
                continue
            base_ptr = int(vertex_data.ctypes.data)
            GL.glColor4f(1.0, 0.86, 0.22, 1.0)
            GL.glVertexPointer(3, GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr))
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, int(len(vertex_data)))
            self._gl_debug_error(f"highlight {uid}")
        self._draw_front_bodygroup_gl(stride)
        self._draw_collision_overlay_gl(stride)
        self._draw_bone_overlay_gl()
        self._draw_constraint_arcs_gl()
        GL.glLineWidth(1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisableClientState(GL.GL_VERTEX_ARRAY)
        GL.glDisable(GL.GL_BLEND)
        GL.glDisable(GL.GL_TEXTURE_2D)

    def _draw_collision_overlay_gl(self, _stride: int) -> None:
        if not GL or not self._collision_lines:
            return
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glLineWidth(1.6)
        for uid, lines in self._collision_lines.items():
            if len(lines) < 2:
                continue
            highlighted = uid == self._hovered_collision_uid or uid in self._highlighted_collision_uids
            color = self._collision_colors.get(uid, (0.2, 0.75, 1.0, 0.85))
            if highlighted:
                GL.glDisable(GL.GL_DEPTH_TEST)
                GL.glLineWidth(3.0)
                GL.glColor4f(1.0, 0.78, 0.08, 1.0)
            else:
                GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glLineWidth(1.6)
                GL.glColor4f(float(color[0]), float(color[1]), float(color[2]), float(color[3]))
            GL.glVertexPointer(3, GL.GL_FLOAT, 0, ctypes.c_void_p(int(lines.ctypes.data)))
            GL.glDrawArrays(GL.GL_LINES, 0, int(len(lines)))
            self._gl_debug_error(f"collision overlay {uid}")
        GL.glLineWidth(1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)

    def _material_bodygroup(self, uid: str) -> str:
        entry = self._materials.get(uid, {})
        return str(entry.get("bodygroup") or entry.get("material_name") or entry.get("proposed_name") or "")

    def _draw_front_bodygroup_gl(self, stride: int) -> None:
        """Re-draw one bodygroup last, depth-disabled, so it pops to the front
        without hiding the rest of the character (manual collision-source hover)."""
        if not GL or not self._front_bodygroup:
            return
        front_uids = [
            uid
            for uid in self._material_vertex_data
            if self._material_bodygroup(uid) == self._front_bodygroup and self._material_visible(uid)
        ]
        if not front_uids:
            return
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE if self._wireframe else GL.GL_FILL)
        for uid in front_uids:
            vertex_data = self._material_vertex_data.get(uid)
            if vertex_data is None or len(vertex_data) < 3:
                continue
            red, green, blue, alpha = self._material_rgba(uid, True)
            GL.glColor4f(red, green, blue, max(0.9, alpha))
            GL.glVertexPointer(3, GL.GL_FLOAT, stride, ctypes.c_void_p(int(vertex_data.ctypes.data)))
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, int(len(vertex_data)))
            self._gl_debug_error(f"front bodygroup {uid}")
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
        GL.glEnable(GL.GL_DEPTH_TEST)

    def _draw_constraint_arcs_gl(self) -> None:
        """Draw Step-14 rotation-limit arcs (and a small axis triad) over the
        model, depth-disabled, colored per axis (X=red, Y=green, Z=blue)."""
        if not GL or not self._constraint_arcs:
            return
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glLineWidth(2.6)
        axis_colors = {
            "x": (1.0, 0.38, 0.36, 1.0),
            "y": (0.42, 1.0, 0.48, 1.0),
            "z": (0.45, 0.62, 1.0, 1.0),
        }
        for key, arr in self._constraint_arcs.items():
            if arr is None or len(arr) < 2:
                continue
            color = (0.86, 0.88, 0.94, 1.0)
            lowered = str(key).lower()
            for prefix, value in axis_colors.items():
                if lowered.startswith(prefix):
                    color = value
                    break
            GL.glColor4f(*color)
            # arr is stored as contiguous float32 by set_constraint_arcs.
            GL.glVertexPointer(3, GL.GL_FLOAT, 0, ctypes.c_void_p(int(arr.ctypes.data)))
            GL.glDrawArrays(GL.GL_LINE_STRIP, 0, int(len(arr)))
        GL.glLineWidth(1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)

    def _draw_bone_overlay_gl(self) -> None:
        if not GL or not self._bone_lines:
            return
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glLineWidth(BONE_OVERLAY_LINE_WIDTH)
        GL.glPointSize(BONE_OVERLAY_POINT_SIZE)
        for uid, lines in self._bone_lines.items():
            if len(lines) < 2:
                continue
            group_color = self._bone_group_colors.get(uid)
            highlighted = uid == self._hovered_bone_uid or uid in self._highlighted_bone_uids or group_color is not None
            if highlighted:
                GL.glDisable(GL.GL_DEPTH_TEST)
                GL.glLineWidth(BONE_HIGHLIGHT_LINE_WIDTH)
                GL.glPointSize(BONE_HIGHLIGHT_POINT_SIZE)
                if group_color is not None and uid != self._hovered_bone_uid:
                    GL.glColor4f(group_color[0], group_color[1], group_color[2], 1.0)
                else:
                    GL.glColor4f(1.0, 0.72, 0.05, 1.0)
            else:
                # Bones-on-top: skip depth so the mesh never hides the skeleton.
                if self._bones_on_top:
                    GL.glDisable(GL.GL_DEPTH_TEST)
                else:
                    GL.glEnable(GL.GL_DEPTH_TEST)
                GL.glLineWidth(BONE_OVERLAY_LINE_WIDTH)
                GL.glPointSize(BONE_OVERLAY_POINT_SIZE)
                GL.glColor4f(0.78, 0.84, 0.94, 0.86)
            GL.glVertexPointer(3, GL.GL_FLOAT, 0, ctypes.c_void_p(int(lines.ctypes.data)))
            GL.glDrawArrays(GL.GL_LINES, 0, int(len(lines)))
            GL.glDrawArrays(GL.GL_POINTS, 0, int(len(lines)))
        GL.glLineWidth(1.0)
        GL.glPointSize(1.0)
        GL.glEnable(GL.GL_DEPTH_TEST)

    def _draw_overlay(self, painter: QtGui.QPainter) -> None:
        visible = sum(int(len(positions) // 3) for uid, positions in self._material_positions.items() if self._material_visible(uid))
        total = sum(int(len(positions) // 3) for positions in self._material_positions.values())
        material_count = len(self._materials)
        cpu_suffix = "" if self._gl_backend_active() and not self._gl_error else " | CPU preview"
        painter.setPen(QtGui.QColor(235, 235, 235))
        painter.drawText(
            12,
            22,
            f"{material_count:,} materials | {visible:,}/{total:,} preview triangles | {'wireframe' if self._wireframe else 'colored'}{cpu_suffix}",
        )
        if self._textured_preview_disabled:
            painter.setPen(QtGui.QColor(255, 210, 120))
            painter.drawText(12, 44, self._textured_disabled_message)
        if self._active_flex_uid:
            painter.setPen(QtGui.QColor(255, 220, 130))
            painter.drawText(12, 66 if self._textured_preview_disabled else 44, f"Flex preview: {self._active_flex_uid} x {self._active_flex_value:.2f}")
        if self._collision_lines:
            y = 88 if self._active_flex_uid and self._textured_preview_disabled else 66 if self._active_flex_uid or self._textured_preview_disabled else 44
            painter.setPen(QtGui.QColor(120, 205, 255))
            label = self._collision_labels.get(self._hovered_collision_uid, self._hovered_collision_uid)
            suffix = f" | {label}" if label else ""
            painter.drawText(12, y, f"Physics overlay: {len(self._collision_lines):,} parts{suffix}")
        if self._bone_lines:
            y = 110 if self._collision_lines else 88 if self._active_flex_uid and self._textured_preview_disabled else 66 if self._active_flex_uid or self._textured_preview_disabled else 44
            painter.setPen(QtGui.QColor(220, 225, 235))
            label = self._bone_labels.get(self._hovered_bone_uid, self._hovered_bone_uid)
            suffix = f" | {label}" if label else ""
            painter.drawText(12, y, f"Bone overlay: {len(self._bone_lines):,} bones{suffix}")
        if self._show_bone_labels and self._bone_lines:
            self._draw_bone_labels(painter)
        if self._height_meters_per_unit > 0.0 and self._material_positions:
            draw_meter_height_bar(
                painter,
                lambda world: self._project_np(world)[0],
                self._scene_mins,
                self._scene_maxs,
                self._height_up_axis,
                self._height_meters_per_unit,
                self._height_reference_m,
            )

    def _draw_bone_labels(self, painter: QtGui.QPainter) -> None:
        """Draw bone name labels at each bone head (always over the mesh).

        Highlighted bones (collision-group members / selected / hovered) are
        emphasized and colored by their group so they stand out for selection.
        """
        heads: list[list[float]] = []
        uids: list[str] = []
        for uid, arr in self._bone_lines.items():
            if len(arr) >= 2:
                heads.append([float(arr[1][0]), float(arr[1][1]), float(arr[1][2])])
                uids.append(uid)
        if not heads:
            return
        points, _depth = self._project_np(np.asarray(heads, dtype=np.float64))
        old_font = painter.font()
        label_font = QtGui.QFont(old_font)
        label_font.setPointSize(max(7, old_font.pointSize() - 1))
        painter.setFont(label_font)
        visible_rect = self.rect().adjusted(-60, -16, 120, 28)
        for point, uid in zip(points, uids):
            if not visible_rect.contains(point.toPoint()):
                continue
            name = self._bone_labels.get(uid, uid)
            group_color = self._bone_group_colors.get(uid)
            highlighted = uid == self._hovered_bone_uid or uid in self._highlighted_bone_uids or group_color is not None
            if group_color is not None:
                fg = QtGui.QColor.fromRgbF(
                    min(1.0, group_color[0] * 1.15 + 0.12),
                    min(1.0, group_color[1] * 1.15 + 0.12),
                    min(1.0, group_color[2] * 1.15 + 0.12),
                )
            elif highlighted:
                fg = QtGui.QColor(255, 210, 90)
            else:
                fg = QtGui.QColor(210, 216, 226, 215)
            anchor = QtCore.QPointF(point.x() + 5.0, point.y() - 2.0)
            # Drop shadow for legibility over bright textures.
            painter.setPen(QtGui.QColor(12, 13, 15, 220))
            painter.drawText(QtCore.QPointF(anchor.x() + 1.0, anchor.y() + 1.0), name)
            painter.setPen(fg)
            painter.drawText(anchor, name)
        painter.setFont(old_font)

    def _emit_stats(self) -> None:
        if not self.scan:
            self.statsChanged.emit("Material preview idle")
            return
        material_count = int(self.scan.get("material_count", len(self._materials)) or len(self._materials))
        sampled = len(self._triangles)
        source = 0
        preview = self.scan.get("model_preview") if isinstance(self.scan, dict) else None
        if isinstance(preview, dict):
            source = int(preview.get("source_triangle_count", 0) or 0)
        cpu_suffix = "" if self._gl_backend_active() and not self._gl_error else " | CPU preview (OpenGL unavailable)"
        self.statsChanged.emit(
            f"Material preview | {material_count:,} materials | {sampled:,}/{source:,} sampled triangles{cpu_suffix}"
        )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._last_mouse_pos = event.position().toPoint()
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        elif event.button() in (QtCore.Qt.MouseButton.RightButton, QtCore.Qt.MouseButton.MiddleButton):
            self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._last_mouse_pos is None:
            self._last_mouse_pos = event.position().toPoint()
            return
        current = event.position().toPoint()
        delta = current - self._last_mouse_pos
        self._last_mouse_pos = current
        buttons = event.buttons()
        if buttons & QtCore.Qt.MouseButton.LeftButton:
            self._drag_active = True
            self._view_yaw = _scalar_float(self._view_yaw, 0.0) + float(delta.x()) * 0.008
            self._view_pitch = max(-math.pi * 0.5 + 0.02, min(math.pi * 0.5 - 0.02, _scalar_float(self._view_pitch, 0.0) + float(delta.y()) * 0.008))
            self.update()
        elif buttons & (QtCore.Qt.MouseButton.RightButton | QtCore.Qt.MouseButton.MiddleButton):
            self._drag_active = True
            self._view_pan += QtCore.QPointF(delta.x(), delta.y())
            self.update()
        else:
            self._drag_active = False
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if not event.buttons():
            self._last_mouse_pos = None
            self._drag_active = False
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.reset_view("Front")
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            self._last_wheel_time = time.monotonic()
            self._view_zoom = max(0.2, min(8.0, _scalar_float(self._view_zoom, 1.0) * math.pow(1.0015, float(delta))))
            self.update()
        event.accept()
