# =====================================================================
# test_blockout.py —— 白模施工单（D-54：对话调整 → 确定性构建 → 白模视频）
#
# 全部离线：解析/脚本生成/工具层打桩，绝不跑真渲染。
# =====================================================================

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

VALID = """# 白模施工单 · 测试
duration_sec: 6

## 对象
- name=墙 shape=box pos=0,0,0 size=2,0.2,3
- name=角色 shape=box pos=1,0,0 size=0.4,0.3,1.7
- name=门 shape=plane pos=4,0,0 size=1,2,2.2

## 镜头
- shot=S1 label=广角 start=0 end=3 cam_pos=4,2,2 cam_look=0,0,1
- shot=S2 label=特写 start=3 end=6 cam_pos=1.5,-1,1.2 target=角色

## 动画
- obj=角色 move=path points=1,0,0;3,0,0 frames=1-72
- obj=门 rotate=z from=0 to=90 frames=100-144
"""


# ---------- 解析 ----------


def test_parse_sheet_合法施工单():
    from app.media.blockout import parse_sheet

    sheet, errors = parse_sheet(VALID)
    assert errors == []
    assert sheet["meta"]["fps"] == 24 and sheet["meta"]["duration_sec"] == 6
    assert sheet["meta"]["total_frames"] == 144          # fps × duration
    assert sheet["meta"]["resolution"] == (1280, 720)    # 缺省值
    assert sheet["meta"]["engine"] == "eevee"
    assert len(sheet["objects"]) == 3
    assert len(sheet["shots"]) == 2 and len(sheet["anim"]) == 2


def test_parse_sheet_缺省元信息_逐项必校():
    from app.media.blockout import parse_sheet

    # fps 缺省 24；resolution 写错、duration 缺、engine 写错 → 各报一条
    bad = VALID.replace(
        "duration_sec: 6\n",
        "duration_sec: 6\nresolution: 9999x720\nengine: cycles\n")
    sheet, errors = parse_sheet(bad)
    assert sheet is None                                  # fail-closed：有错不给半份单
    assert any("resolution" in e for e in errors)
    assert any("engine" in e for e in errors)


def test_parse_sheet_非法条目逐条说人话():
    from app.media.blockout import parse_sheet

    OBJ_ANCHOR = "- name=墙 shape=box pos=0,0,0 size=2,0.2,3"
    SHOT_ANCHOR = "- shot=S1 label=广角 start=0 end=3 cam_pos=4,2,2 cam_look=0,0,1"
    ANIM_ANCHOR = "- obj=角色 move=path points=1,0,0;3,0,0 frames=1-72"
    cases = [
        (OBJ_ANCHOR, "- name=墙2 shape=楔形 pos=0,0,0 size=1,1,1", "shape"),
        (OBJ_ANCHOR, "- name=墙 shape=box pos=1,1,1 size=1,1,1", "重复"),
        (SHOT_ANCHOR, "- shot=S9 start=0 end=3", "cam_pos"),
        (SHOT_ANCHOR, "- shot=S9 start=0 end=3 cam_pos=0,0,0 target=不存在", "target"),
        (ANIM_ANCHOR, "- obj=角色 move=path points=1,0,0 frames=1-10", "≥2 个路径点"),
        (ANIM_ANCHOR, "- obj=角色 rotate=z from=abc to=10 frames=1-10", "from/to"),
        (ANIM_ANCHOR, "- obj=角色 rotate=z from=0 to=90 frames=1-999", "frames"),
    ]
    for anchor, fragment, keyword in cases:
        sheet = VALID.replace(anchor, anchor + "\n" + fragment)
        _, errors = parse_sheet(sheet)
        assert any(keyword in e for e in errors), (keyword, errors)


def test_parse_sheet_未知小节报错():
    from app.media.blockout import parse_sheet

    _, errors = parse_sheet(VALID + "\n## 场景\n- foo=bar\n")
    assert any("场景" in e for e in errors)


# ---------- 构建脚本生成（确定性） ----------


def test_build_script_内置两个实测坑():
    """5.x 视频输出：media_type='VIDEO' 必须先于 file_format='FFMPEG'（坑 1）；
    切镜标记必须绑相机（坑 2，否则全程单机位）。"""
    from app.media.blockout import build_script, parse_sheet

    sheet, _ = parse_sheet(VALID)
    script = build_script(sheet, "C:/out/白模.mp4")
    assert script.index("media_type = 'VIDEO'") < script.index("file_format = 'FFMPEG'")
    assert ".camera = bpy.data.objects['CAM_S1']" in script
    assert ".camera = bpy.data.objects['CAM_S2']" in script
    assert "to_track_quat('-Z', 'Y')" in script           # 相机看向由代码算，不靠模型
    assert "OUT = 'C:/out/白模.mp4'" in script and "sc.render.filepath = OUT" in script


def test_build_script_动画与引擎():
    from app.media.blockout import build_script, parse_sheet

    sheet, _ = parse_sheet(VALID)
    script = build_script(sheet, "out.mp4")
    assert "keyframe_insert('location'" in script
    # 插值用「插入前设全局偏好」——5.x 移除了 Action.fcurves，遍历写法跨不了版本
    assert "keyframe_new_interpolation_type = 'LINEAR'" in script
    assert ".fcurves" not in script
    assert "keyframe_insert('rotation_euler'" in script
    # target 相机 = 真跟踪（COPY_LOCATION 空物体 + TRACK_TO），
    # 不是建脚本时算死的旋转——角色走动相机必须跟
    assert "constraints.new(type='COPY_LOCATION')" in script
    assert "constraints.new(type='TRACK_TO')" in script

    sheet["meta"]["engine"] = "workbench"
    assert "BLENDER_WORKBENCH" in build_script(sheet, "out.mp4")


# ---------- Blender 发现 ----------


def test_discover_blender_env优先(monkeypatch):
    import app.media.blockout as bm

    probe = str(Path(__file__))
    monkeypatch.setenv("BLENDER_PATH", probe)
    assert bm.discover_blender() == probe


def test_discover_blender_找不到给None(monkeypatch):
    import app.media.blockout as bm

    monkeypatch.delenv("BLENDER_PATH", raising=False)
    monkeypatch.setattr(bm.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(Path(__file__).parent))  # 没有 Programs/Blender/*
    assert bm.discover_blender() is None


# ---------- 工具层 ----------


def _ctx(monkeypatch, tmp_path):
    from app.chat.tools.blockout import build_blockout_tools
    from app.chat.tools.context import Collector, ToolContext

    store = SimpleNamespace(
        get_project=lambda pid: SimpleNamespace(id=pid) if pid == "p1" else None)
    ctx = ToolContext(store=store, llm=SimpleNamespace(), collector=Collector(), runner=None)
    import app.home as home_mod

    monkeypatch.setattr(home_mod, "workspace_root", lambda: Path(tmp_path))
    monkeypatch.setattr(home_mod, "ensure_home", lambda: None)
    tools = {t.name: t for t in build_blockout_tools(ctx)}
    return tools


def test_save_非法不落盘_合法落盘(monkeypatch, tmp_path):
    tools = _ctx(monkeypatch, tmp_path)
    bad = tools["blockout_sheet_save"].invoke(
        {"project_id": "p1", "content": "## 对象\n- name=墙 shape=楔形 pos=0,0,0 size=1,1,1"})
    assert "施工单没保存" in bad
    assert not (tmp_path / "blender" / "p1" / "施工单.md").exists()

    ok = tools["blockout_sheet_save"].invoke(
        {"project_id": "p1", "content": VALID, "note": "初稿"})
    assert "施工单已保存（初稿）" in ok and "2 个镜头" in ok
    assert (tmp_path / "blender" / "p1" / "施工单.md").read_text(encoding="utf-8") == VALID


def test_build_没施工单先拦(monkeypatch, tmp_path):
    tools = _ctx(monkeypatch, tmp_path)
    out = tools["blockout_build"].invoke({"project_id": "p1"})
    assert "还没有施工单" in out


def test_build_同意图不重复发起(monkeypatch, tmp_path):
    tools = _ctx(monkeypatch, tmp_path)
    tools["blockout_sheet_save"].invoke({"project_id": "p1", "content": VALID})

    import app.deps as app_deps

    starts: list[str] = []

    class _Mgr:
        def start(self, pid):
            starts.append(pid)
            return {"status": "running"}

        def get(self, pid):
            return None

    monkeypatch.setattr(app_deps, "blockout_manager", lambda: _Mgr())
    out = tools["blockout_build"].invoke({"project_id": "p1"})
    assert "后台渲染" in out and starts == ["p1"]


def test_check_状态分支(monkeypatch, tmp_path):
    tools = _ctx(monkeypatch, tmp_path)

    import app.deps as app_deps

    class _Mgr:
        def __init__(self, rec):
            self._rec = rec

        def get(self, pid):
            return self._rec

    monkeypatch.setattr(app_deps, "blockout_manager", lambda: _Mgr(None))
    assert "还没发起过" in tools["blockout_check"].invoke({"project_id": "p1"})

    monkeypatch.setattr(app_deps, "blockout_manager",
                        lambda: _Mgr({"status": "running", "messages": ["开始渲染：144 帧"]}))
    assert "渲染中" in tools["blockout_check"].invoke({"project_id": "p1"})

    monkeypatch.setattr(app_deps, "blockout_manager", lambda: _Mgr(
        {"status": "failed", "error": "Blender 没找到"}))
    assert "失败" in tools["blockout_check"].invoke({"project_id": "p1"})

    rec = {"status": "succeeded",
           "result": {"asset_id": "asset_x", "video_url": "/api/assets/file/asset_x",
                      "duration_sec": 6.0}}
    monkeypatch.setattr(app_deps, "blockout_manager", lambda: _Mgr(rec))
    out = tools["blockout_check"].invoke({"project_id": "p1"})
    assert "渲染完成" in out and "asset_x" in out


# ---------- 管理器（打桩 subprocess，不跑真渲染） ----------


def test_manager_成功路径登记资产(monkeypatch, tmp_path):
    import app.media.blockout as bm

    monkeypatch.setattr(bm, "discover_blender", lambda: "blender.exe")
    monkeypatch.setattr(bm.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(bm, "_duration_sec", lambda p: 6.0)

    registered: dict = {}

    def fake_store():
        return SimpleNamespace(add_asset=lambda pid, **kw: registered.update(kw, project_id=pid))

    monkeypatch.setattr(bm, "_default_store", fake_store)
    d = tmp_path / "blender" / "p1"
    d.mkdir(parents=True)
    (d / "施工单.md").write_text(VALID, encoding="utf-8")
    mp4 = d / "白模.mp4"
    mp4.write_bytes(b"fake video bytes")
    monkeypatch.setattr(bm, "blockout_dir", lambda pid: d)

    mgr = bm.BlockoutManager()
    mgr.start("p1")
    # 线程很快，轮询等它收尾（get 每次返回副本，必须反复取）
    import time

    rec = mgr.get("p1")
    for _ in range(100):
        if rec["status"] != "running":
            break
        time.sleep(0.02)
        rec = mgr.get("p1")
    assert rec["status"] == "succeeded", rec.get("error")
    assert rec["result"]["asset_id"]
    assert registered["source"] == "blockout" and registered["id"] == rec["result"]["asset_id"]
    assert (d / "build_scene.py").exists()                # 构建脚本随单落盘


def test_manager_失败路径报日志尾部(monkeypatch, tmp_path):
    import app.media.blockout as bm

    monkeypatch.setattr(bm, "discover_blender", lambda: "blender.exe")
    monkeypatch.setattr(bm.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr="bad args"))
    monkeypatch.setattr(
        bm, "_default_store", lambda: SimpleNamespace(add_asset=lambda *a, **k: None))
    d = tmp_path / "blender" / "p1"
    d.mkdir(parents=True)
    (d / "施工单.md").write_text(VALID, encoding="utf-8")
    monkeypatch.setattr(bm, "blockout_dir", lambda pid: d)

    mgr = bm.BlockoutManager()
    mgr.start("p1")
    import time

    rec = mgr.get("p1")
    for _ in range(100):
        if rec["status"] != "running":
            break
        time.sleep(0.02)
        rec = mgr.get("p1")
    assert rec["status"] == "failed"
    assert "bad args" in rec["error"] or "日志尾部" in rec["error"]


# ---------- 身份色（2026-11-15：白模要能认人） ----------
#
# 白模渲出来是一堆几何体，用户要靠颜色认人——「把那个高的挪开」得先说得清是哪个。
# 颜色只进白模渲染，绝不进最终视频提示词。


def test_施工单接受颜色并拒绝非法色名():
    from app.media.blockout import parse_sheet

    sheet = """
fps: 24
resolution: 960x540
duration_sec: 4
engine: workbench

## 对象
- name=孔乙己  shape=box  pos=2,1,0.85  size=0.4,0.3,1.7  color=red
- name=墙    shape=box  pos=0,0,0  size=0.3,3,4  color=gray

## 镜头
- shot=S1  start=0  end=4  cam_pos=4,2,3  cam_look=0,0,1
"""
    parsed, errors = parse_sheet(sheet)
    assert parsed is not None, errors
    assert errors == []
    assert parsed["objects"][0]["color"] == "red"


def test_非法色名会被挡下():
    from app.media.blockout import parse_sheet

    sheet = """
fps: 24
resolution: 960x540
duration_sec: 4
engine: workbench

## 对象
- name=孔乙己  shape=box  pos=2,1,0.85  size=0.4,0.3,1.7  color=chartreuse

## 镜头
- shot=S1  start=0  end=4  cam_pos=4,2,3  cam_look=0,0,1
"""
    parsed, errors = parse_sheet(sheet)
    assert parsed is None
    assert any("color" in e for e in errors)


def test_中文色名也认():
    from app.media.blockout import normalize_color

    assert normalize_color("红") == "red"
    assert normalize_color("RED") == "red"
    assert normalize_color("青") == "cyan"
    assert normalize_color("不存在") is None


def test_同名永远同色():
    """自动配色按名字哈希——颜色乱跳等于没有颜色，用户建不起心智模型。"""
    from app.media.blockout import color_of

    a = color_of({"name": "孔乙己"})
    b = color_of({"name": "孔乙己"})
    c = color_of({"name": "掌柜"})
    assert a == b
    assert a != c


def test_显式颜色覆盖自动色():
    from app.media.blockout import color_of

    assert color_of({"name": "孔乙己", "color": "blue"}) == "blue"


def test_图例给出颜色与对象的对照():
    """没有图例，颜色就是密码：用户看到一堆彩色方块仍不知道哪个是谁。"""
    from app.media.blockout import color_legend

    legend = color_legend({"objects": [
        {"name": "孔乙己", "color": "red"},
        {"name": "墙"},
    ]})
    assert [x["name"] for x in legend] == ["孔乙己", "墙"]
    assert legend[0]["color"] == "red"
    assert len(legend[0]["rgb"]) == 3


def test_构建脚本带上颜色():
    """材质要真的用上颜色，而不是仍然一水的灰。"""
    from app.media.blockout import build_script, parse_sheet

    sheet = """
fps: 24
resolution: 960x540
duration_sec: 4
engine: workbench

## 对象
- name=孔乙己  shape=box  pos=2,1,0.85  size=0.4,0.3,1.7  color=red

## 镜头
- shot=S1  start=0  end=4  cam_pos=4,2,3  cam_look=0,0,1
"""
    parsed, errors = parse_sheet(sheet)
    assert parsed is not None, errors
    script = build_script(parsed, "out.mp4")
    assert "Base Color" in script
    assert "(0.85, 0.15, 0.15)" in script, "red 的 RGB 要写进材质"
    assert "gray(tone)" not in script


# ---------- S3 反哺：施工单的空间事实进组提示词（D-56） ----------


SHEET = """
label: 排钱
scene_id: scene_001
group_id: g1
fps: 24
resolution: 960x540
duration_sec: 8
engine: workbench

## 对象
- name=孔乙己  shape=box  pos=2,1,0.85  size=0.4,0.3,1.7  color=red
- name=柜台    shape=box  pos=0,0,0.5    size=2,0.8,1

## 镜头
- shot=S1  start=0  end=4  cam_pos=4,2,3  cam_look=0,0,1
- shot=S2  start=4  end=8  cam_pos=2,-3,1.5  target=孔乙己
"""


def _sheet():
    from app.media.blockout import parse_sheet

    parsed, errors = parse_sheet(SHEET)
    assert parsed is not None, errors
    return parsed


def test_空间锚说的是方位与距离():
    from app.media.blockout import spatial_anchor

    anchor = spatial_anchor(_sheet())
    assert anchor.startswith("空间锚：")
    assert "S1" in anchor and "S2" in anchor
    assert "孔乙己" in anchor
    # 方位词：相对镜头看向的左右前后
    assert any(w in anchor for w in ("右", "左", "正"))
    assert "m" in anchor


def test_空间锚是确定性的():
    """同样的单子必须永远生成同样的锚——锚要能被逐字复用。"""
    from app.media.blockout import spatial_anchor

    assert spatial_anchor(_sheet()) == spatial_anchor(_sheet())


def test_空间锚里没有颜色():
    """颜色是给人看渲染的；写进提示词会让视频模型画红盒子。"""
    from app.media.blockout import spatial_anchor

    anchor = spatial_anchor(_sheet())
    for word in ("红", "red", "蓝", "blue", "红色"):
        assert word not in anchor


def test_空间键要绑场次():
    from app.media.blockout import spatial_key

    sheet = _sheet()
    assert spatial_key(sheet) == "scene_001::g1"
    sheet["meta"].pop("scene_id")
    assert spatial_key(sheet) == "", "没绑场次就不反哺"


def test_保存施工单会落下空间事实(store, tmp_path, monkeypatch):
    from app.media import blockout as blockout_mod

    monkeypatch.setattr(blockout_mod, "blockout_dir", lambda pid: tmp_path / pid)
    project = store.create_project(
        title="排钱", raw_text="原文", adaptation_type="short_drama", language="zh-CN",
    )
    anchor = blockout_mod.save_spatial_facts(store, project.id, _sheet())
    assert anchor.startswith("空间锚：")

    style = store.get_video_style(project.id)
    assert style["spatial_anchors"]["scene_001::g1"] == anchor
    facts = style["blockout_spatial"]["scene_001::g1"]
    assert [o["name"] for o in facts["objects"]] == ["孔乙己", "柜台"]
    assert facts["objects"][0]["pos"] == [2.0, 1.0, 0.85]
    assert len(facts["shots"]) == 2


def test_组提示词把空间锚当第三个固定锚段():
    """空间锚与 env / cast 锚同性质：确定性、逐字复用、模型不许改写。"""
    from app.crew.dp import _fold_group_prompts
    from app.domain import Shot

    shots = [
        Shot(id="shot_a_000", scene_id="scene_001", order=0, subject="客人",
              shot_type="medium", group_id="g1", group_index=0, duration_sec=4),
        Shot(id="shot_a_001", scene_id="scene_001", order=1, subject="掌柜",
              shot_type="close_up", group_id="g1", group_index=1, duration_sec=3),
    ]
    parts = {
        "shot_a_000": {"env": "环境锚：旧当铺。", "cast": "人物锚：客人。",
                        "body": "排钱。", "style": "风格段。"},
        "shot_a_001": {"env": "环境锚：旧当铺。", "cast": "人物锚：掌柜。",
                        "body": "打量。", "style": "风格段。"},
    }
    anchor = "空间锚：S1 机位高 3m，孔乙己在镜头右前方 3.2m。"
    _fold_group_prompts(
        shots, parts, language="zh", provider="minimax",
        spatial_anchors={"scene_001::g1": anchor},
    )
    gp = shots[0].group_prompt
    assert gp is not None
    assert anchor in gp
    # 顺序：环境 → 人物 → 空间锚 → 机位正文 → 风格
    assert gp.index("环境锚") < gp.index("人物锚") < gp.index("空间锚") < gp.index("[Shot 1]")


def test_没有白模就不占提示词预算():
    from app.crew.dp import _fold_group_prompts
    from app.domain import Shot

    shots = [
        Shot(id="shot_a_000", scene_id="scene_001", order=0, subject="客人",
              shot_type="medium", group_id="g1", group_index=0, duration_sec=4),
        Shot(id="shot_a_001", scene_id="scene_001", order=1, subject="掌柜",
              shot_type="close_up", group_id="g1", group_index=1, duration_sec=3),
    ]
    parts = {
        "shot_a_000": {"env": "E", "cast": "C", "body": "b1", "style": "S"},
        "shot_a_001": {"env": "E", "cast": "C", "body": "b2", "style": "S"},
    }
    _fold_group_prompts(shots, parts, language="zh", provider="minimax")
    assert "空间锚" not in (shots[0].group_prompt or "")
