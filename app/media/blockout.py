# =====================================================================
# blockout.py —— 白模施工单：解析 + 确定性构建 + headless 渲染（D-54）
#
# 流程（产品内，全部对话驱动）：
#   施工单（创作者可读的 md，Agent 起草、用户对话调整）
#     → parse_sheet 严格校验（fail-closed，错误信息说人话）
#     → build_script 生成 build_scene.py（确定性代码，随单落盘——
#       「文字是资产、成品是衍生物」：同一份施工单永远渲出同一条片子）
#     → headless Blender 渲染 → mp4 注册为项目素材（source="blockout"）
#
# 产品边界（D-54）：只出「最终导出的白模视频」，没有任何 3D 视口/预览 UI；
# 用户调整的唯一入口是改施工单（对话里说，Agent 改单重存、再渲染）。
#
# 构建脚本内置两个实测坑（2026-11-14，DEVELOPMENT 5.58）：
#   · Blender 5.x 必须先 render.image_settings.media_type='VIDEO' 再
#     file_format='FFMPEG'，否则动态枚举里没有视频容器（报错还截断枚举）；
#   · 镜头标记必须绑 marker.camera——不绑则渲染全程停在活动相机上，
#     多镜头不切。
# =====================================================================

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SHAPES = {"box", "cylinder", "sphere", "plane"}
_ENGINES = {"eevee", "workbench"}
_AXES = {"x": 0, "y": 1, "z": 2}
_VEC3 = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
_SECTION_KEYS = {"对象": "objects", "镜头": "shots", "动画": "anim"}


# ---------- 施工单解析 ----------


def _vec3(value: str) -> tuple[float, float, float] | None:
    if not _VEC3.match(value):
        return None
    x, y, z = (float(v) for v in value.split(","))
    return (x, y, z)


def _frames(value: str, fps: float, duration: float) -> tuple[int, int] | None:
    m = re.match(r"^(\d+)-(\d+)$", value)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if not (1 <= a < b <= int(fps * duration)):
        return None
    return (a, b)


def parse_sheet(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """解析施工单。返回 (sheet | None, 错误清单)——有错时 sheet 为 None
    （fail-closed：半份施工单不进构建）。"""
    errors: list[str] = []
    meta: dict[str, Any] = {"label": ""}
    section: str | None = None
    objects: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    anims: list[dict[str, Any]] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") and not line.startswith("##"):
            continue
        if line.startswith("## "):
            title = line[3:].strip()
            section = _SECTION_KEYS.get(title)
            if section is None:
                errors.append(f"第 {lineno} 行：小节「{title}」不认识（只支持 对象/镜头/动画）")
                section = None
            continue
        if line.startswith("##"):
            continue
        if section is None:
            # 头部元信息（第一个小节之前）：key: value
            if ":" in line:
                key, _, value = line.partition(":")
                key, value = key.strip(), value.split("#")[0].strip()
                if key in ("fps", "resolution", "duration_sec", "engine", "label"):
                    meta[key] = value
            continue
        if not line.startswith("- "):
            continue
        # 条目行：- key=value key=value ...（值内不允许空格）
        item: dict[str, str] = {}
        for tok in line[2:].split():
            key, sep, value = tok.partition("=")
            if not sep:
                errors.append(f"第 {lineno} 行：条目里的「{tok}」不是 key=value 写法")
                continue
            item[key] = value
        if section == "objects":
            objects.append(item)
        elif section == "shots":
            shots.append(item)
        else:
            anims.append(item)

    # ---- 元信息校验 ----
    fps = float(meta.get("fps") or 24)
    if not (1 <= fps <= 120):
        errors.append(f"fps={meta.get('fps')} 不在 1-120 内")
    duration = float(meta.get("duration_sec") or 0)
    if not (1 <= duration <= 120):
        errors.append("duration_sec 必须写，且在 1-120 秒内（白模不是正片）")
    res = str(meta.get("resolution") or "1280x720")
    m = re.match(r"^(\d+)[xX](\d+)$", res)
    if not m or not all(128 <= int(v) <= 4096 for v in m.groups()):
        errors.append(f"resolution={res} 要写成 宽x高（各 128-4096）")
    engine = str(meta.get("engine") or "eevee").lower()
    if engine not in _ENGINES:
        errors.append(f"engine={engine} 只支持 {'/'.join(sorted(_ENGINES))}")

    total_frames = int(fps * duration) if duration else 0

    # ---- 对象 ----
    names: set[str] = set()
    for it in objects:
        name = it.get("name", "")
        if not name:
            errors.append("对象条目缺 name")
        elif name in names:
            errors.append(f"对象名「{name}」重复")
        names.add(name)
        if it.get("shape") not in _SHAPES:
            allowed = "/".join(sorted(_SHAPES))
            errors.append(f"对象「{name}」的 shape={it.get('shape')} 只支持 {allowed}")
        if _vec3(it.get("pos", "")) is None:
            errors.append(f"对象「{name}」的 pos 要写成 x,y,z（如 pos=0,0,0）")
        size = _vec3(it.get("size", ""))
        if size is None or any(v <= 0 for v in size):
            errors.append(f"对象「{name}」的 size 要写成正数 宽,深,高（如 size=1,1,2）")
        tone = it.get("tone")
        if tone is not None:
            try:
                if not (0.0 <= float(tone) <= 1.0):
                    raise ValueError
            except ValueError:
                errors.append(f"对象「{name}」的 tone 在 0-1 之间（灰度）")

    # ---- 镜头 ----
    shot_ids: set[str] = set()
    for it in shots:
        sid = it.get("shot", "")
        if not sid:
            errors.append("镜头条目缺 shot（镜头名）")
        elif sid in shot_ids:
            errors.append(f"镜头名「{sid}」重复")
        shot_ids.add(sid)
        try:
            start, end = float(it.get("start", -1)), float(it.get("end", -1))
            if not (0 <= start < end <= duration):
                errors.append(f"镜头「{sid}」的 start/end 要满足 0 ≤ start < end ≤ duration_sec")
        except ValueError:
            errors.append(f"镜头「{sid}」的 start/end 必须是秒数")
        if _vec3(it.get("cam_pos", "")) is None:
            errors.append(f"镜头「{sid}」缺 cam_pos=x,y,z（相机位置）")
        if _vec3(it.get("cam_look", "")) is None and not it.get("target"):
            errors.append(f"镜头「{sid}」要给 cam_look=x,y,z 或 target=对象名（看哪）")
        if it.get("target") and it["target"] not in names:
            errors.append(f"镜头「{sid}」的 target={it['target']} 不是「对象」里已有的名字")

    # ---- 动画 ----
    for it in anims:
        obj = it.get("obj", "")
        if obj not in names:
            errors.append(f"动画条目的 obj={obj or '（缺）'} 不是「对象」里已有的名字")
            continue
        frames = _frames(it.get("frames", ""), fps, duration) if it.get("frames") else None
        if frames is None:
            errors.append(
                f"「{obj}」的 frames 要写成 起帧-止帧（如 frames=1-72，且 ≤ {total_frames}）")
        if it.get("move") == "path":
            pts = [p for p in (it.get("points", "").split(";") or []) if p]
            if len(pts) < 2 or any(_vec3(p) is None for p in pts):
                errors.append(f"「{obj}」的 points 要 ≥2 个路径点（x,y;z,y;z，分号分隔）")
        elif it.get("rotate") in _AXES:
            try:
                float(it.get("from", "x"))
                float(it.get("to", "x"))
            except ValueError:
                # 旋转方向是合法表达（鞠躬=过去再回来），只拦非数字
                errors.append(f"「{obj}」的 from/to 必须是数字（度）")
        else:
            errors.append(f"「{obj}」的动画只支持 move=path 或 rotate=x/y/z")

    if errors:
        return None, errors
    return {
        "meta": {
            "label": str(meta.get("label") or ""),
            "fps": int(fps),
            "resolution": (int(m.group(1)), int(m.group(2))),
            "duration_sec": duration,
            "engine": engine,
            "total_frames": total_frames,
        },
        "objects": objects,
        "shots": shots,
        "anim": anims,
    }, []


# ---------- 构建脚本生成（确定性：同一施工单 → 同一脚本） ----------


def build_script(sheet: dict[str, Any], out_mp4: str) -> str:
    """把施工单编译成 headless bpy 脚本文本。纯字符串生成，无副作用。"""
    meta = sheet["meta"]
    fps, total = meta["fps"], meta["total_frames"]
    w, h = meta["resolution"]
    lines = [
        "# 由施工单自动生成（app/media/blockout.py）——手改会被下次构建覆盖，要改去改施工单",
        "import bpy",
        "from mathutils import Vector",
        f"OUT = {out_mp4!r}",
        f"FPS = {fps}",
        "",
        "for o in list(bpy.data.objects):",
        "    bpy.data.objects.remove(o, do_unlink=True)",
        "",
        "def gray(tone):",
        "    m = bpy.data.materials.new('gray_%d' % int(tone * 100))",
        "    m.use_nodes = True",
        "    bsdf = next(n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')",
        "    bsdf.inputs['Base Color'].default_value = (tone, tone, tone, 1)",
        "    return m",
        "",
        "def add_obj(name, shape, pos, size, tone):",
        "    if shape in ('box', 'plane'):",
        "        bpy.ops.mesh.primitive_cube_add(location=pos)",
        "    elif shape == 'cylinder':",
        "        bpy.ops.mesh.primitive_cylinder_add(location=pos)",
        "    else:",
        "        bpy.ops.mesh.primitive_uv_sphere_add(location=pos)",
        "    o = bpy.context.active_object",
        "    o.name = name",
        "    sx, sy, sz = size",
        "    if shape == 'plane':",
        "        o.scale = (sx / 2, sy / 2, 0.01)",
        "    else:",
        "        o.scale = (sx / 2, sy / 2, sz / 2)",
        "    o.data.materials.append(gray(tone))",
        "    return o",
        "",
    ]

    tone_default = 0.75
    for it in sheet["objects"]:
        pos = _vec3(it["pos"])
        size = _vec3(it["size"])
        tone = float(it.get("tone", tone_default))
        lines.append(
            f"add_obj({it['name']!r}, {it['shape']!r}, {pos!r}, {size!r}, {tone})")

    sc_lines = ["", "sc = bpy.context.scene"]
    for it in sheet["shots"]:
        sid = it["shot"]
        cam = f"CAM_{sid}"
        pos = _vec3(it["cam_pos"])
        sc_lines += [
            f"cd = bpy.data.cameras.new({cam!r})",
            f"cam = bpy.data.objects.new({cam!r}, cd)",
            "sc.collection.objects.link(cam)",
            f"cam.location = {pos!r}",
        ]
        if it.get("cam_look"):
            look = _vec3(it["cam_look"])
            sc_lines.append(
                f"cam.rotation_euler = (Vector({look!r}) - Vector({pos!r}))"
                ".to_track_quat('-Z', 'Y').to_euler()")
        else:
            # target=对象名 是**真跟踪**：空物体 COPY_LOCATION 跟随目标、相机
            # TRACK_TO 空物体——旋转只在建脚本时算一次的话，角色一动相机就
            # 不跟了（首版实现就是这个坑：视口里「看着对」、渲染全程脱靶）。
            sc_lines += [
                f"tgt = bpy.data.objects.new('TGT_{sid}', None)",
                "sc.collection.objects.link(tgt)",
                "_cc = tgt.constraints.new(type='COPY_LOCATION')",
                f"_cc.target = bpy.data.objects[{it['target']!r}]",
                "_tc = cam.constraints.new(type='TRACK_TO')",
                "_tc.target = tgt",
                "_tc.track_axis = 'TRACK_NEGATIVE_Z'",
                "_tc.up_axis = 'UP_Y'",
            ]

    sc_lines += [
        f"sc.frame_start, sc.frame_end = 1, {total}",
        f"sc.render.fps = {fps}",
        f"sc.render.resolution_x, sc.render.resolution_y = {w}, {h}",
        # 清场把默认灯光也删了：白模必须自带太阳光 + 环境光，否则全片近黑
        "sun = bpy.data.lights.new('SUN', type='SUN')",
        "sun.energy = 3.0",
        "so = bpy.data.objects.new('SUN', sun)",
        "sc.collection.objects.link(so)",
        "so.rotation_euler = (0.9, 0.0, 0.6)",
        "if sc.world is None:",
        "    sc.world = bpy.data.worlds.new('W')",
        "sc.world.use_nodes = True",
        "_bg = next(n for n in sc.world.node_tree.nodes if n.type == 'BACKGROUND')",
        "_bg.inputs[0].default_value = (0.85, 0.85, 0.88, 1)",
        "_bg.inputs[1].default_value = 1.0",
    ]
    engine = meta["engine"]
    if engine == "workbench":
        sc_lines.append("sc.render.engine = 'BLENDER_WORKBENCH'")
    else:
        sc_lines += [
            "try:",
            "    sc.render.engine = 'BLENDER_EEVEE'",
            "except TypeError:",
            "    sc.render.engine = 'BLENDER_WORKBENCH'  # 引擎名变了就降级，渲染不出黑块",
        ]
    # 渲染输出：5.x 先 media_type 再 file_format（坑 #1）
    sc_lines += [
        "ifs = sc.render.image_settings",
        "if 'media_type' in ifs.bl_rna.properties:",
        "    ifs.media_type = 'VIDEO'",
        "ifs.file_format = 'FFMPEG'",
        "sc.render.ffmpeg.format = 'MPEG4'",
        "sc.render.ffmpeg.codec = 'H264'",
        "sc.render.filepath = OUT",
        "",
    ]
    # 切镜：标记绑相机（坑 #2）
    for it in sheet["shots"]:
        frame = max(1, int(round(float(it["start"]) * fps)) + 1)
        sc_lines.append(
            f"_m = sc.timeline_markers.new(name={it['shot']!r}, frame={frame})")
        sc_lines.append(f"_m.camera = bpy.data.objects['CAM_{it['shot']}']")
    # 动画节拍。插值用「插入前设全局偏好」而不是事后遍历 fcurves——
    # Blender 5.x 移除了 Action.fcurves（动画系统改 slot 化），遍历写法跨不了版本。
    sc_lines += [
        "try:",
        "    bpy.context.preferences.edit.keyframe_new_interpolation_type = 'LINEAR'",
        "except Exception:",
        "    pass",
        "",
    ]
    for it in sheet["anim"]:
        fa, fb = _frames(it["frames"], fps, meta["duration_sec"])
        if it.get("move") == "path":
            pts = [_vec3(p) for p in it["points"].split(";") if p]
            sc_lines.append(f"_o = bpy.data.objects[{it['obj']!r}]")
            for i, pt in enumerate(pts):
                t = fa + (fb - fa) * i / (len(pts) - 1)
                sc_lines.append(
                    f"_o.location = {pt!r}; _o.keyframe_insert('location', frame={t:.0f})")
        else:
            axis = _AXES[it["rotate"]]
            deg_from, deg_to = float(it["from"]), float(it["to"])
            sc_lines.append(f"_o = bpy.data.objects[{it['obj']!r}]")
            sc_lines.append(
                f"_o.rotation_euler[{axis}] = __import__('math').radians({deg_from})"
                f"; _o.keyframe_insert('rotation_euler', frame={fa})")
            sc_lines.append(
                f"_o.rotation_euler[{axis}] = __import__('math').radians({deg_to})"
                f"; _o.keyframe_insert('rotation_euler', frame={fb})")
    sc_lines += [
        "",
        "bpy.ops.render.render(animation=True)",
        "print('RENDER_DONE:', OUT)",
        "",
    ]
    return "\n".join(lines + sc_lines)


def _duration_sec(path: Path) -> float:
    """实测媒体时长（imageio-ffmpeg 自带二进制，无 ffprobe 时解析 -i 输出）。"""
    import re as _re

    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([ff, "-i", str(path)], capture_output=True, text=True, timeout=30)
        m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            h, mi, sec = (float(x) for x in m.groups())
            return h * 3600 + mi * 60 + sec
    except Exception as e:  # noqa: BLE001
        log.debug("白模时长探测失败：%s", e)
    return 0.0


# ---------- Blender 发现 ----------


def discover_blender() -> str | None:
    """找本机 Blender：BLENDER_PATH 环境变量 > PATH > 常见安装位置。"""
    from ..config import bootstrap_env

    bootstrap_env()
    import os

    explicit = os.environ.get("BLENDER_PATH", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("blender")
    if found:
        return found
    import sys

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or ""
        for cand in Path(local).glob("Programs/Blender/*/blender.exe") if local else []:
            return str(cand)
    return None


# ---------- 后台渲染管理（仿 FinalizeManager：线程 + 状态注册表） ----------


def _default_store():
    from ..deps import store as _store

    return _store()


def blockout_dir(project_id: str) -> Path:
    from ..home import ensure_home, workspace_root

    ensure_home()
    d = Path(workspace_root()) / "blender" / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_blockout_manager() -> BlockoutManager:
    global _manager
    if _manager is None:
        _manager = BlockoutManager()
    return _manager


_manager: BlockoutManager | None = None


class BlockoutManager:
    """每个项目同一时刻只跑一个白模渲染（内存状态，重启即作废）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._jobs.get(project_id)
            if rec and rec["status"] == "running":
                return rec
            rec = {
                "project_id": project_id, "status": "running",
                "messages": [], "error": None, "result": None,
                "created_at": time.time(),
            }
            self._jobs[project_id] = rec
        t = threading.Thread(target=self._run, args=(rec, project_id),
                             daemon=True, name=f"blockout-{project_id}")
        t.start()
        return rec

    def get(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._jobs.get(project_id)
            return dict(rec) if rec else None

    def _emit(self, rec: dict[str, Any], msg: str) -> None:
        log.info("[blockout] %s", msg)
        rec["messages"].append(msg)
        del rec["messages"][:-30]

    def _run(self, rec: dict[str, Any], project_id: str) -> None:
        try:
            result = self._build(rec, project_id)
            rec["result"] = result
            rec["status"] = "succeeded"
        except Exception as e:  # noqa: BLE001
            log.exception("白模渲染失败")
            rec["error"] = str(e)
            rec["status"] = "failed"

    def _build(self, rec: dict[str, Any], project_id: str) -> dict[str, Any]:
        d = blockout_dir(project_id)
        sheet_path = d / "施工单.md"
        if not sheet_path.exists():
            raise RuntimeError("项目还没有施工单（先说「做白模」起草一份）")
        sheet, errors = parse_sheet(sheet_path.read_text(encoding="utf-8"))
        if sheet is None:
            raise RuntimeError("施工单有问题，先改单：\n" + "\n".join(f"- {e}" for e in errors))
        meta = sheet["meta"]
        self._emit(rec, f"施工单解析通过：{len(sheet['objects'])} 对象 / "
                        f"{len(sheet['shots'])} 镜头 / {meta['duration_sec']:.0f}s")

        out_mp4 = d / "白模.mp4"
        script = build_script(sheet, str(out_mp4))
        script_path = d / "build_scene.py"
        script_path.write_text(script, encoding="utf-8")

        blender = discover_blender()
        if blender is None:
            raise RuntimeError(
                "本机没找到 Blender（白模渲染靠它，不花钱）。"
                "装好后重启应用，或把 blender.exe 路径写进环境变量 BLENDER_PATH。")
        self._emit(rec, f"开始渲染：{meta['total_frames']} 帧（{blender}）")
        proc = subprocess.run(
            [blender, "--background", "--python", str(script_path)],
            capture_output=True, text=True, timeout=1800,
        )
        if not out_mp4.exists():
            tail = (proc.stderr or proc.stdout or "")[-400:]
            raise RuntimeError(f"Blender 没有产出视频文件。日志尾部：\n{tail}")
        duration = _duration_sec(out_mp4)
        from ..store import gen_id

        asset_id = gen_id("asset")
        url = f"/api/assets/file/{asset_id}"
        store = _default_store()
        store.add_asset(
            project_id, kind="video", name=f"白模·{meta['label'] or '未命名'}",
            source="blockout", url=url, id=asset_id,
            content=out_mp4.read_bytes(), duration_sec=duration or None,
        )
        self._emit(rec, f"渲染完成（{duration:.1f}s），已登记为项目素材 {asset_id}")
        return {
            "asset_id": asset_id, "video_url": url,
            "duration_sec": round(duration, 2),
            "sheet": str(sheet_path), "script": str(script_path),
        }
