"""Corpus-style Workshop portrait (SPIC.png) - see agent_guide/07_icons_and_workshop_art.md.
Corpus-style Workshop portrait (SPIC.png): PMX imported with MMD Tools, posed by a dance VMD frame,
optional shape-key expression, 50 mm camera in front of the face, white world light, Cycles GPU,
transparent 2000x2000 film. Run inside the managed Blender:
  blender --background --factory-startup --python render_spic.py -- --pmx X.pmx --vmd motion.vmd
      --frames 60,130,334 --out-dir DIR [--res 400 --samples 32] [--final SPIC.png --morph Name=0.7 ...]
"""
import argparse, json, math, os, sys
import bpy
from mathutils import Vector
# repo tools dir: <repo>/agent_guide/resources/render_spic_cycles.py -> <repo>/tools (override with SCMI_TOOLS_DIR)
from pathlib import Path as _P
TOOLS = os.environ.get("SCMI_TOOLS_DIR") or str(_P(__file__).resolve().parents[2] / "tools")
sys.path.insert(0, TOOLS)
import blender_render_icon as bri

import re, bmesh

def base_name(m):
    return re.sub(r"\.\d+$", "", m.name) if m else ""

def transparent_ratio(mat):
    """Fraction of texels with alpha < 0.5 in the material base texture (sub-sampled)."""
    if not mat or not mat.use_nodes: return 0.0
    node = mat.node_tree.nodes.get("mmd_base_tex") or next((n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image), None)
    if not node or not node.image or not node.image.has_data: return 0.0
    img = node.image
    if img.channels < 4: return 0.0
    px = img.pixels[:]
    n = len(px) // 4
    step = max(1, n // 20000)
    cnt = tot = 0
    for i in range(0, n, step):
        tot += 1
        if px[4 * i + 3] < 0.5: cnt += 1
    return cnt / max(1, tot)

def remove_overlay_shells():
    """MMD models often carry '+' overlay materials: a duplicate of the clothes/skin faces with a mostly
    transparent shading texture. Coplanar with the base they z-fight in Cycles and punch holes; delete
    the overlay faces (the twin with the more transparent texture, or the one named '...+')."""
    removed = []
    for o in [x for x in bpy.data.objects if x.type == "MESH" and len(x.data.polygons) > 0]:
        me = o.data
        cent = {}
        for p in me.polygons:
            c = p.center
            cent.setdefault(p.material_index, set()).add((round(c.x, 4), round(c.y, 4), round(c.z, 4)))
        mats = [s.material for s in o.material_slots]
        drop = set()
        idxs = sorted(cent)
        for a in idxs:
            for b in idxs:
                if a >= b: continue
                shared = len(cent[a] & cent[b])
                if shared and shared >= 0.9 * min(len(cent[a]), len(cent[b])):
                    na, nb = base_name(mats[a]), base_name(mats[b])
                    if na.endswith("+") and not nb.endswith("+"): victim = a
                    elif nb.endswith("+") and not na.endswith("+"): victim = b
                    else: victim = a if transparent_ratio(mats[a]) >= transparent_ratio(mats[b]) else b
                    drop.add(victim)
        if drop:
            bm = bmesh.new(); bm.from_mesh(me)
            faces = [f for f in bm.faces if f.material_index in drop]
            bmesh.ops.delete(bm, geom=faces, context="FACES")
            bm.to_mesh(me); bm.free(); me.update()
            removed += [f"{o.name}:{mats[i].name if mats[i] else i}" for i in sorted(drop)]
    print("[SPIC] removed overlay shells:", removed, flush=True)
    return removed

def shape_key_names():
    names = []
    for o in bpy.data.objects:
        if o.type == "MESH" and o.data.shape_keys:
            names += [k.name for k in o.data.shape_keys.key_blocks[1:]]
    return names

def parse():
    p = argparse.ArgumentParser()
    p.add_argument("--pmx", required=True); p.add_argument("--vmd", required=True)
    p.add_argument("--frames", default="334"); p.add_argument("--out-dir", required=True)
    p.add_argument("--res", type=int, default=400); p.add_argument("--samples", type=int, default=32)
    p.add_argument("--final", default=""); p.add_argument("--morph", action="append", default=[])
    p.add_argument("--dist", type=float, default=0.88); p.add_argument("--height", type=float, default=0.10)
    p.add_argument("--target-drop", type=float, default=0.12); p.add_argument("--lens", type=float, default=50.0)
    p.add_argument("--yaw", type=float, default=0.0, help="camera orbit in degrees, positive moves the camera to the character left side")
    p.add_argument("--report", default="")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return p.parse_args(argv)

def setup_gpu():
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for t in ("OPTIX", "CUDA", "HIP"):
        try:
            prefs.compute_device_type = t; prefs.get_devices()
            ok = False
            for d in prefs.devices:
                d.use = (d.type == t)
                ok = ok or d.use
            if ok:
                bpy.context.scene.cycles.device = "GPU"; return t
        except Exception:
            continue
    bpy.context.scene.cycles.device = "CPU"; return "CPU"

def setup_scene(res, samples):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = samples; sc.cycles.use_denoising = True; sc.cycles.use_adaptive_sampling = True
    sc.render.resolution_x = res; sc.render.resolution_y = res; sc.render.resolution_percentage = 100
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"; sc.render.image_settings.color_mode = "RGBA"; sc.render.image_settings.color_depth = "8"
    sc.view_settings.view_transform = "AgX"; sc.view_settings.look = "None"; sc.view_settings.exposure = 0.0; sc.view_settings.gamma = 1.0
    w = sc.world or bpy.data.worlds.new("World"); sc.world = w
    w.use_nodes = True
    bg = w.node_tree.nodes.get("Background") or w.node_tree.nodes.new("ShaderNodeBackground")
    bg.inputs[0].default_value = (1, 1, 1, 1); bg.inputs[1].default_value = 1.0
    out = next((n for n in w.node_tree.nodes if n.type == "OUTPUT_WORLD"), None) or w.node_tree.nodes.new("ShaderNodeOutputWorld")
    if not any(l.to_node == out for l in w.node_tree.links):
        w.node_tree.links.new(bg.outputs[0], out.inputs[0])

def head_bone(arm):
    for name in ("頭", "head", "Head", "ValveBiped.Bip01_Head1"):
        pb = arm.pose.bones.get(name)
        if pb: return pb
    for pb in arm.pose.bones:
        if getattr(getattr(pb, "mmd_bone", None), "name_e", "").lower() == "head": return pb
    raise RuntimeError("no head bone")

def place_camera(arm, dist, height, target_drop, lens, yaw_deg):
    sc = bpy.context.scene
    cam = sc.camera
    if cam is None:
        cd = bpy.data.cameras.new("SPIC_Camera"); cam = bpy.data.objects.new("SPIC_Camera", cd); sc.collection.objects.link(cam); sc.camera = cam
    cam.data.type = "PERSP"; cam.data.lens = lens; cam.data.sensor_width = 36.0; cam.data.clip_start = 0.05
    pb = head_bone(arm)
    bpy.context.view_layer.update()
    head = arm.matrix_world @ pb.head
    target = head + Vector((0, 0, -target_drop))
    yaw = math.radians(yaw_deg)
    offset = Vector((math.sin(yaw) * dist, -math.cos(yaw) * dist, height))   # the character faces -Y
    cam.location = target + offset
    direction = target - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return {"head": [round(v, 3) for v in head], "target": [round(v, 3) for v in target], "camera": [round(v, 3) for v in cam.location],
            "rot_deg": [round(math.degrees(a), 1) for a in cam.rotation_euler]}

def apply_morphs(morphs):
    applied = {}
    for spec in morphs:
        name, _, val = spec.rpartition("=")
        val = float(val)
        for o in bpy.data.objects:
            if o.type == "MESH" and o.data.shape_keys and name in o.data.shape_keys.key_blocks:
                o.data.shape_keys.key_blocks[name].value = val; applied[name] = val
    return applied

def main():
    a = parse()
    os.makedirs(a.out_dir, exist_ok=True)
    bri.enable_mmd_tools(); bri.clear_scene()
    arm = bri.import_model(bri.Path(a.pmx))
    ops = bri.get_mmd_tool_ops()
    root = bri.find_mmd_root_or_armature(arm); bri.set_active(root)
    vmd = bri.Path(a.vmd)
    bri.call_operator(ops.import_vmd, filepath=str(vmd), directory=str(vmd.parent) + os.sep, files=[{"name": vmd.name}],
                      bone_mapper="PMX", margin=0, update_scene_settings=True, create_new_action=False, use_nla=False)
    removed = remove_overlay_shells()
    device = setup_gpu(); setup_scene(a.res, a.samples)
    print("[SPIC] device", device, flush=True)
    frames = [int(f) for f in a.frames.split(",") if f.strip()]
    report = {"device": device, "frames": {}, "morphs": {}, "removed_overlays": removed, "shape_keys": shape_key_names()}
    for f in frames:
        bpy.context.scene.frame_set(f); bpy.context.view_layer.update()
        report["morphs"] = apply_morphs(a.morph)
        cam_info = place_camera(arm, a.dist, a.height, a.target_drop, a.lens, a.yaw)
        out = a.final if (a.final and len(frames) == 1) else os.path.join(a.out_dir, "preview_%05d.png" % f)
        bpy.context.scene.render.filepath = out
        bpy.ops.render.render(write_still=True)
        report["frames"][f] = {"file": out, **cam_info}
        print("[SPIC] rendered", f, out, cam_info, flush=True)
    if a.report:
        json.dump(report, open(a.report, "w", encoding="utf-8"), indent=1)
    print("SPIC_DONE")

main()
