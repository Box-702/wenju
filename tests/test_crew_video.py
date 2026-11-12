# =====================================================================
# test_crew_video.py —— 剧组链路与视频适配层测试
#
# 覆盖本次开发新增/修复的核心逻辑（这些坑都真实踩过，固化成回归测试）：
#   - 导演拆解：全局重编号、枚举容错、时长钳制、连续性校验；
#   - 摄影指导：语言推断、锚逐字复用、按镜过滤人物、无风格指南时的退化；
#   - 连续性解析：reference_group / chain_from → reference_videos；
#   - 视频 Provider：状态映射的子串陷阱、嵌套 task、结果字段名。
# =====================================================================

from __future__ import annotations

import asyncio
import pytest

from app.crew.art_director import art_system
from app.crew.director import DirectorAgent
from app.crew.dp import DPAgent, dp_system, language_for
from app.domain import (
    CameraMovement,
    Character,
    Location,
    Scene,
    SceneAnchors,
    SceneBreakdown,
    Script,
    Shot,
    Source,
    StyleGuide,
)
from app.crew.dramaturgy import strip_spoken_quotes
from app.video.continuity import resolve_continuity, resolve_submission_references

# ---------- 测试夹具 ----------


def _script() -> Script:
    return Script(
        title="测试剧",
        logline="一行梗概",
        themes=["悬疑"],
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_a", name="齐夏"), Character(id="char_b", name="人羊")],
        locations=[Location(id="loc_room", name="密室")],
        scenes=[
            Scene(
                id="scene_001", title="第一场", chapter_refs=["ch_001"], location_id="loc_room",
                characters=["char_a", "char_b"], purpose="建立冲突", conflict="对峙",
            )
        ],
    )


def _director() -> DirectorAgent:
    # 这两个方法不依赖 LLM，直接构造实例即可。
    return DirectorAgent.__new__(DirectorAgent)


def _dp() -> DPAgent:
    return DPAgent.__new__(DPAgent)


# ---------- 导演：解析容错 ----------


def test_director_parse_renumbers_order_globally_and_remaps_chain():
    raw = [
        {"scene_id": "scene_001", "shots": [
            {"order": 0, "subject": "A", "duration_sec": 8, "reference_group": "room", "chain_from": None, "cut_reason": "起点"},
            {"order": 1, "subject": "B", "duration_sec": 8, "reference_group": "room", "chain_from": 0, "cut_reason": "切换"},
        ]},
        {"scene_id": "scene_002", "shots": [
            {"order": 0, "subject": "C", "duration_sec": 8, "reference_group": "room", "chain_from": None, "cut_reason": "新场起点"},
            {"order": 1, "subject": "D", "duration_sec": 8, "reference_group": "room", "chain_from": 0, "cut_reason": "切换"},
        ]},
    ]
    bds = _director()._parse_breakdowns(raw)
    orders = [(s.scene_id, s.order) for bd in bds for s in bd.shots]
    # order 必须是全片唯一且递增的（不是每场从 0 重来）
    assert orders == [("scene_001", 0), ("scene_001", 1), ("scene_002", 2), ("scene_002", 3)]
    # chain_from 从「场景内局部 order」映射到全局 order
    second_scene = bds[1].shots
    assert second_scene[0].chain_from is None
    assert second_scene[1].chain_from == 2


def test_director_parse_coerces_enum_and_clamps_duration():
    raw = [{"scene_id": "scene_001", "pacing": "fast-paced", "shots": [
        {"order": 0, "shot_type": "close-up", "camera_type": "push_in", "camera_speed": "slow",
         "subject": "A", "duration_sec": 0.8, "cut_reason": "x"},
        {"order": 1, "shot_type": "不存在的景别", "camera_type": "weird_cam", "camera_speed": "warp",
         "subject": "B", "duration_sec": 99, "cut_reason": "y"},
    ]}]
    bd = _director()._parse_breakdowns(raw)[0]
    assert bd.pacing == "medium"                    # 非法 pacing 兜底
    s0, s1 = bd.shots
    assert (s0.shot_type, s0.camera.type, s0.camera.speed) == ("close_up", "dolly", "slow")
    assert s0.duration_sec == 1.0                   # 0.8 被钳到下限
    assert (s1.shot_type, s1.camera.type, s1.camera.speed) == ("medium", "static", "medium")
    assert s1.duration_sec == 30.0                  # 99 被钳到上限


def test_director_parse_reads_scheduling_fields():
    """画面调度四件套（运镜轨迹/机位高度/空间关系/背景人物）必须落到 Shot 上。"""
    raw = [{"scene_id": "scene_001", "shots": [{
        "order": 0, "shot_type": "wide", "camera_type": "orbit", "camera_speed": "slow",
        "camera_path": "从人物正面起步，绕到背后约 180 度，半径保持两米",
        "camera_height": "与人物胸口齐平，随后升到过顶",
        "subject": "现场全景", "action": "围观者退开",
        "spatial": "齐夏1号在前景左侧，离镜头 1.5 米；人羊2号在后景，两人相隔约 3 米",
        "background_action": "两名警察维持边界，一人偶尔回头；居民在警戒带外探头低语",
        "lighting": "主光从左上方斜射，人物右侧落在阴影里",
        "duration_sec": 8, "cut_reason": "起点",
    }]}]
    shot = _director()._parse_breakdowns(raw)[0].shots[0]
    assert shot.camera.type == "orbit"          # 新增的环绕枚举
    assert "绕到背后" in shot.camera.path
    assert shot.camera.height.startswith("与人物胸口齐平")
    assert "齐夏1号" in shot.spatial
    assert "警察" in shot.background_action


# ---------- 导演：连续性校验 ----------


def test_director_validate_flags_bad_plan():
    bd = SceneBreakdown(scene_id="scene_001", shots=[
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8),
        # 无分组 + 时长越界 + 缺 cut_reason + 与上一镜同主体 + 悬空接力
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, duration_sec=2,
             subject="齐夏面部", chain_from=99),
        Shot(id="shot_scene_001_002", scene_id="scene_001", order=2, duration_sec=6,
             subject="齐夏面部", chain_from=1),
    ])
    issues = " ".join(_director()._validate_continuity([bd]))
    assert "reference_group" in issues        # 多镜无分组
    assert "超出生成器可行区间" in issues       # 2s 越界
    assert "cut_reason" in issues              # 缺切镜理由
    assert "同一主体" in issues                 # 重复特写
    assert "指向不存在的镜头" in issues          # 接力悬空


def test_director_validate_requires_camera_path_for_moving_shots():
    """运动镜头没写轨迹 = 模型只能自己乱动，必须拦下来（固定机位不受约束）。"""
    bd = SceneBreakdown(scene_id="scene_001", shots=[
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
             reference_group="room", cut_reason="起点",
             function="pressure", final_frame="镜头停在他背后",
             action="他下颌锁死，手撑在桌上", lighting="冷光从左上来",
             camera=CameraMovement(type="dolly", speed="slow")),          # 缺 path
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, duration_sec=8,
             subject="座钟", reference_group="room", cut_reason="视点转移",
             function="emotion", final_frame="秒针走完一格",
             action="秒针跳动，他眼神下沉", lighting="侧光扫过钟面",
             camera=CameraMovement(type="static")),                        # 固定机位，无需 path
    ])
    issues = " ".join(_director()._validate_continuity([bd]))
    assert "camera_path" in issues
    assert "shot_scene_001_001" not in issues       # 固定机位不报


def test_director_validate_accepts_good_plan():
    bd = SceneBreakdown(
        scene_id="scene_001",
        anchors=SceneAnchors(emotion="压抑", motif="钨丝灯亮斑", object="座钟",
                             break_point="发现无门", final_image="众人抬头"),
        shots=[
            Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
                 reference_group="room", cut_reason="起点",
                 function="pressure", final_frame="光斑停在他脸上",
                 action="他下颌锁死，手指抠着桌沿", lighting="钨丝灯从上方打亮斑",
                 background_action="秒针的滴答声持续"),
            Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, duration_sec=8,
                 subject="对话交锋", reference_group="room", chain_from=0, cut_reason="对话单元切换",
                 function="emotion", final_frame="两人隔桌对视定格",
                 action="她嘴唇压成一条线", lighting="侧光把半张脸切进阴影"),
        ],
    )
    assert _director()._validate_continuity([bd]) == []


# ---------- 摄影指导 ----------


def test_language_for_follows_provider_dialect():
    # MiniMax 原生吃中文：中文剧本就用中文提示词，英文剧本才转英文。
    assert language_for("minimax", "zh-CN") == "zh"
    assert language_for("", "zh-CN") == "zh"
    assert language_for("", "en-US") == "en"
    # 非中文原生 provider 一律英文——这类厂商只在显式配置时才会出现。
    assert language_for("runway", "zh-CN") == "en"


def test_dp_reuses_anchors_verbatim_and_filters_cast_per_shot():
    script = _script()
    guide = StyleGuide(
        character_appearances={"齐夏": "黑色短发、深灰卫衣的年轻男性。", "人羊": "戴山羊头面具的黑西服男人。"},
        environment_descriptions={"密室": "密闭的方形房间，钨丝灯昏黄。"},
    )
    shots = [
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, camera=CameraMovement()),
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, camera=CameraMovement()),
    ]
    # LLM 只回本镜动作 + 本镜出场人物
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["人羊"], "prompt_body": "镜头缓慢推近。"},
        {"shot_id": "shot_scene_001_001", "characters": ["齐夏"], "prompt_body": "齐夏翻开卡片。"},
    ]}]}
    out = _dp()._apply(shots, llm_out, script, "写实质感。", guide)
    env = "密闭的方形房间，钨丝灯昏黄"
    # 环境锚逐字复用（前面可能带 @场景名(编号) 的引用标记）
    assert all(env in s.video_prompt for s in out)
    assert "人羊" in out[0].video_prompt and "齐夏" not in out[0].video_prompt   # 按镜过滤
    assert "齐夏" in out[1].video_prompt and "人羊" not in out[1].video_prompt
    assert "戴山羊头面具的黑西服男人" in out[0].video_prompt         # 人物锚来自风格指南（逐字）


def test_dp_backfills_anchor_for_named_but_unlisted_character():
    """body 里点了名的角色，即使 LLM 的 characters 列表漏了，也必须补上外观锚。

    否则这个角色在画面里露脸却没有锚，跨镜必然长相漂移——
    「视觉锚只产一次、下游逐字复用」不能靠模型自觉。
    """
    script = _script()
    guide = StyleGuide(
        character_appearances={"齐夏": "黑色短发、深灰卫衣的年轻男性。", "人羊": "戴山羊头面具的黑西服男人。"},
        environment_descriptions={"密室": "密闭的方形房间，钨丝灯昏黄。"},
    )
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, camera=CameraMovement())]
    # LLM 只报了「齐夏」，但 body 里实际还有「人羊」出场
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["齐夏"],
         "prompt_body": "齐夏后退半步，人羊挡在门口。"},
    ]}]}
    out = _dp()._apply(shots, llm_out, script, "", guide)
    prompt = out[0].video_prompt
    assert "人羊" in prompt
    assert "戴山羊头面具的黑西服男人" in prompt, f"漏了补锚：{prompt}"
    assert "黑色短发、深灰卫衣的年轻男性" in prompt


def test_dp_does_not_add_anchor_for_absent_character():
    """没在画面里点名的角色不该被塞进提示词。"""
    script = _script()
    guide = StyleGuide(
        character_appearances={"齐夏": "黑色短发。", "人羊": "戴山羊头面具。"},
        environment_descriptions={"密室": "密闭的方形房间。"},
    )
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, camera=CameraMovement())]
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["齐夏"], "prompt_body": "齐夏翻开卡片。"},
    ]}]}
    prompt = _dp()._apply(shots, llm_out, script, "", guide)[0].video_prompt
    assert "齐夏" in prompt
    assert "人羊" not in prompt, f"无关角色被塞进来了：{prompt}"


def test_dp_repairs_negations_once_and_keeps_positive_rewrite():
    """提示词里仍有否定句时，要有界修复一轮：只改含否定词的句子。

    规则层已尽力（写作规则 + 交稿前自查），但概率模型仍会漏；
    规则管不住的部分用一次有界修复兜底。
    """
    raw = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "prompt_body": "视线扫过空椅子但没有停留。"},
        {"shot_id": "shot_scene_001_001", "prompt_body": "他弯腰把杯子放进纸箱。"},
    ]}]}
    shot0 = Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                 video_prompt="视线扫过空椅子但没有停留。")
    shot1 = Shot(id="shot_scene_001_001", scene_id="scene_001", order=1,
                 video_prompt="他弯腰把杯子放进纸箱。")

    dp = _dp()
    calls: list[str] = []

    def fake_invoke(system: str, user: str) -> dict:
        calls.append(user)
        return {"shot_scene_001_000": "视线扫过空椅子，随即移开。"}

    dp._invoke_json = fake_invoke  # type: ignore[method-assign]
    assert dp._repair_negations([shot0, shot1], raw) is True
    assert len(calls) == 1
    assert "shot_scene_001_000" in calls[0]
    assert "shot_scene_001_001" not in calls[0]      # 只送有问题的镜头
    assert raw["scenes"][0]["shots"][0]["prompt_body"] == "视线扫过空椅子，随即移开。"
    assert raw["scenes"][0]["shots"][1]["prompt_body"] == "他弯腰把杯子放进纸箱。"


def test_dp_repair_rejects_rewrite_that_still_has_negation():
    """模型改完还带否定词的，不要采纳——免得来回改更糟。"""
    raw = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "prompt_body": "他没有回头。"},
    ]}]}
    shot = Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                video_prompt="他没有回头。")
    dp = _dp()
    dp._invoke_json = lambda s, u: {"shot_scene_001_000": "他仍然没有回头。"}  # type: ignore[method-assign]
    assert dp._repair_negations([shot], raw) is False
    assert raw["scenes"][0]["shots"][0]["prompt_body"] == "他没有回头。"


def test_dp_repair_does_nothing_when_prompts_are_clean():
    raw = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "prompt_body": "他弯腰把杯子放进纸箱。"},
    ]}]}
    shot = Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                video_prompt="他弯腰把杯子放进纸箱。")
    dp = _dp()

    def boom(s, u):
        raise AssertionError("干净时不该调用模型")

    dp._invoke_json = boom  # type: ignore[method-assign]
    assert dp._repair_negations([shot], raw) is False


def test_dp_repair_survives_model_failure():
    """修复是增强而非必需：模型不配合时不能连累已经拿到的结果。"""
    raw = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "prompt_body": "他没有回头。"},
    ]}]}
    shot = Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                video_prompt="他没有回头。")
    dp = _dp()

    def boom(s, u):
        raise RuntimeError("模型挂了")

    dp._invoke_json = boom  # type: ignore[method-assign]
    assert dp._repair_negations([shot], raw) is False


def test_dp_system_carries_the_five_prompt_rules():
    """图里那套提示词逻辑要固化成系统提示词，漏掉任何一条都会让画面失控。"""
    zh = dp_system(language="zh", provider_label="MiniMax", max_duration=10, has_anchors=True)
    for kw in ["虚拟无人机", "呼吸感", "光从哪个方向来", "编上号", "背景人物", "只写画面里看得见的东西",
               "不越轴", "主事件", "禁止否定句", "台词不进提示词",
               "最终画面收尾", "情绪只写身体", "占位词"]:
        assert kw in zh
    en = dp_system(language="en", provider_label="Runway", max_duration=10, has_anchors=True)
    for kw in ["virtual drone", "lens breathing", "light direction", "Number the main characters",
               "no axis crossing", "never negations", "Never put spoken lines",
               "final frame", "slop words"]:
        assert kw in en


def test_director_and_art_rules_lock_adherence_budget():
    """v2/v3 对比实测的四条教训要固化：越轴、动作节拍过载、视点摇摆、身份职业词。"""
    from app.crew.director import director_system

    for kw in ["运镜三禁", "越轴", "结尾不改景别", "物理过程的预算只给主事件", "第二次互动", "正向描述"]:
        assert kw in director_system()
    art = art_system("zh")
    for kw in ["客观机位", "视点切换", "禁止出现身份与职业词"]:
        assert kw in art


def test_drama_rules_locked_into_prompts():
    """方法论「先戏剧，后语法」要固化进剧组提示词：三工作、细节律、五锚点、最终画面、运镜动机。"""
    from app.crew.director import director_system

    for kw in ["先戏剧，后语法", "三工作规则", "细节律", "最终画面", "五锚点",
               "运镜必须有动机", "节奏阶梯", "不豁免"]:
        assert kw in director_system()
    art = art_system("zh")
    for kw in ["写实人物公式", "环境会表演", "环境压力"]:
        assert kw in art


def test_dp_build_prompt_surfaces_scheduling_fields():
    """运镜轨迹/空间关系必须进入 LLM 的输入，否则写 prompt_body 时无从下手。"""
    script = _script()
    bd = SceneBreakdown(scene_id="scene_001", shots=[
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
             camera=CameraMovement(type="dolly", path="沿中轴推进四米后停住", height="齐胸高度"),
             spatial="齐夏1号在前景，离镜头 1.5 米", background_action="记者压低声交谈"),
    ])
    prompt = _dp()._build_prompt(script, [bd], None, "zh", False)
    assert "沿中轴推进四米后停住" in prompt
    assert "齐胸高度" in prompt
    assert "齐夏1号" in prompt
    assert "记者压低声交谈" in prompt


def test_dp_apply_backfills_missing_camera_path_once():
    """LLM 漏写运镜时用导演方案兜底；已经写过（哪怕只是复述）就不能写第二遍。"""
    script = _script()

    def _run(body: str) -> str:
        shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                      camera=CameraMovement(type="dolly", path="从门口推进到桌前"))]
        llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
            {"shot_id": "shot_scene_001_000", "characters": ["齐夏"], "prompt_body": body},
        ]}]}
        return _dp()._apply(shots, llm_out, script, "", None)[0].video_prompt

    assert "从门口推进到桌前" in _run("齐夏抬头。")                      # 整段漏写 → 补上
    assert _run("镜头从门口推进到桌前，齐夏抬头。").count("从门口推进到桌前") == 1
    # 模型用自己的话复述轨迹（真实模型就是这么干的）时不许再追加一遍
    assert _run("摄影机缓慢向前推进，齐夏抬头。").count("运镜轨迹") == 0


def test_dp_apply_backfills_final_frame_and_audits():
    """U11 最终画面：body 漏写结束帧时用导演方案补成最后一句；已写过不重复。"""
    script = _script()

    def _run(body: str, final_frame: str = "冷光熄灭在他脸上") -> Shot:
        shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                      final_frame=final_frame, camera=CameraMovement())]
        llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
            {"shot_id": "shot_scene_001_000", "characters": ["齐夏"], "prompt_body": body},
        ]}]}
        return _dp()._apply(shots, llm_out, script, "", None)[0]

    assert _run("齐夏的下颌慢慢锁死。").video_prompt.rstrip("。").endswith("冷光熄灭在他脸上")
    assert _run("冷光熄灭在他脸上。").video_prompt.count("冷光熄灭在他脸上") == 1
    # 拼装结果要过第二道审计；干净 prompt 不产生提醒
    assert _run("齐夏的下颌慢慢锁死，秒针滴答。").prompt_audit == []


def test_dp_apply_strips_slop_segments_from_body():
    """整段占位词在拼装层机械剥掉；「最终画面」兜底在其后仍然生效。"""
    script = _script()
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                  final_frame="冰箱冷光熄灭在他脸上", camera=CameraMovement())]
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["齐夏"],
         "prompt_body": "他站在冰箱前，电影感，下颌锁死。"},
    ]}]}
    prompt = _dp()._apply(shots, llm_out, script, "", None)[0].video_prompt
    assert "电影感" not in prompt
    assert "下颌锁死" in prompt
    assert prompt.rstrip("。").endswith("冰箱冷光熄灭在他脸上")


def test_dp_build_prompt_carries_drama_fields():
    """功能/最终画面/五锚点必须进入 DP 的输入，否则 body 无从对齐戏剧结构。"""
    script = _script()
    bd = SceneBreakdown(
        scene_id="scene_001",
        anchors=SceneAnchors(emotion="压抑", motif="钨丝灯亮斑", object="座钟",
                             break_point="发现无门", final_image="众人抬头"),
        shots=[Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                    function="pressure", final_frame="光斑停在山羊头面具上",
                    camera=CameraMovement())],
    )
    prompt = _dp()._build_prompt(script, [bd], None, "zh", False)
    assert "功能 pressure" in prompt
    assert "光斑停在山羊头面具上" in prompt
    assert "本场五锚点" in prompt and "钨丝灯亮斑" in prompt


def test_dp_fallback_keeps_scheduling_fields():
    """无模型回退也要带上运镜/空间/背景，不能退化成一句话提示词。"""
    script = _script()
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                  camera=CameraMovement(type="orbit", path="绕人物一圈"),
                  spatial="两人相隔三米", background_action="警察维持边界")]
    res = _dp()._fallback(shots, script, "写实质感。", "zh", None)
    prompt = res.data[0].video_prompt
    assert "绕人物一圈" in prompt and "两人相隔三米" in prompt and "警察维持边界" in prompt


def test_dp_style_text_does_not_double_punctuate():
    """风格各段自带的句末标点要去掉，否则提示词末尾会出现「呼吸感。，#3a4a5a」。"""
    guide = StyleGuide(
        lighting_style="低调暖光。",
        camera_style="手持纪实，保留呼吸感。",
        color_palette=["#3a4a5a", "#6b7b8b"],
    )
    text = _dp()._style_text(guide)
    assert "。，" not in text
    assert text == "低调暖光，手持纪实，保留呼吸感，主色调 #3a4a5a、#6b7b8b"


def test_dp_style_text_only_fills_gaps_in_shot_body():
    """风格段只补本镜没写的部分——不能拿全片风格覆盖本镜的具体描述。

    实测冲突：镜头写「固定不动」，末尾又被贴上「保留细微的手持浮动」「偶尔略高俯看」；
    镜头写「冷绿日光灯」，末尾又贴「暖黄灯为主光」。
    """
    guide = StyleGuide(
        lighting_style="低调暖光为主，钨丝灯从头顶压下来。",
        camera_style="客观机位，机位高度与视线齐平。",
        color_palette=["#111", "#222"],
    )
    # body 已经把光影和运镜都写了 → 风格段只剩色调
    body = "日光灯从正上方打下，镜头固定在桌前不动，他的下颌慢慢锁死。"
    text = _dp()._style_text(guide, body=body)
    assert "低调暖光" not in text, text
    assert "机位高度" not in text, text
    assert "主色调 #111、#222" in text, text


def test_dp_style_text_fills_lighting_when_body_is_silent():
    """body 没写光影时，风格段要把光线基调补上（填空）。"""
    guide = StyleGuide(
        lighting_style="低调暖光为主。",
        camera_style="客观机位。",
        color_palette=["#111"],
    )
    text = _dp()._style_text(guide, body="他站在门口。")
    assert "低调暖光" in text, text
    assert "客观机位" in text, text


def test_dp_style_text_strips_camera_movement_list():
    """camera_style 会被逐字拼进每个镜头——里面不能带运动清单。

    带了等于给每个镜头同时下三四种运动的指令，与本镜的运镜打架。
    """
    guide = StyleGuide(
        lighting_style="低调暖光。",
        camera_style=(
            "客观机位，稳定为主，关键处缓慢推近。"
            "摄影机高度大致与坐姿人物视线齐平。"
            "跟随人物时以缓慢横移和轻微推拉为主，保留真实拍摄的呼吸感。"
        ),
    )
    text = _dp()._style_text(guide)
    assert "推近" not in text, text
    assert "横移" not in text, text
    assert "跟随" not in text, text
    # 风格身份要留住
    assert "客观机位" in text, text
    assert "呼吸感" in text, text


def test_dp_prompt_has_no_conflicting_camera_moves():
    """端到端不变量：组装出的提示词里，风格段不能带第二套运镜指令。

    本镜的运镜由导演的 camera_path 决定；风格段若还带着别的运动清单，
    视频模型就会收到互相打架的指令。
    """
    from app.crew.dramaturgy import find_camera_moves

    guide = StyleGuide(
        lighting_style="低调暖光。",
        camera_style=(
            "客观机位，稳定为主，关键处缓慢推近。"
            "摄影机高度大致与坐姿人物视线齐平。"
            "跟随人物时以缓慢横移和轻微推拉为主，保留真实拍摄的呼吸感。"
        ),
    )
    script = _script()
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0,
        camera=CameraMovement(type="static", path="固定机位，人物轻微前倾时镜头不动"),
    )]
    llm_out = {"scenes": [{
        "scene_id": "scene_001",
        "environment": "昏暗房间，墙上一盏钨丝灯。",
        "characters": {"齐夏": "年轻男性，黑色短发。"},
        "shots": [{
            "shot_id": "shot_scene_001_000", "characters": ["齐夏"],
            "prompt_body": "齐夏站在桌前，手指在杯沿上敲了一下。",
        }],
    }]}
    style = _dp()._style_text(guide)
    out = _dp()._apply(shots, llm_out, script, style, guide)
    prompt = out[0].video_prompt or ""

    # 提示词整体不应出现多种运镜
    moves = set(find_camera_moves(prompt))
    assert len(moves) <= 1, f"提示词里出现了多种运镜：{moves}｜{prompt}"
    # 风格段本身已不含运动清单
    assert "横移" not in style and "推近" not in style


def test_dp_without_style_guide_falls_back_to_llm_anchors():
    script = _script()
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, camera=CameraMovement())]
    llm_out = {"scenes": [{
        "scene_id": "scene_001",
        "environment": "昏暗房间。",
        "characters": {"齐夏": "年轻男性。"},
        "shots": [{"shot_id": "shot_scene_001_000", "characters": ["齐夏"], "prompt_body": "推近。"}],
    }]}
    out = _dp()._apply(shots, llm_out, script, "写实。", None)
    assert out[0].video_prompt.startswith("昏暗房间")


# ---------- 连续性解析 ----------


def test_resolve_continuity_builds_reference_videos():
    shots = [
        {"id": "s0", "order": 0, "scene_id": "scene_001", "reference_group": "room", "chain_from": None, "video_url": "u0"},
        {"id": "s1", "order": 1, "scene_id": "scene_001", "reference_group": "room", "chain_from": 0},
        {"id": "s2", "order": 2, "scene_id": "scene_002", "reference_group": "other", "chain_from": None, "video_url": "u2"},
    ]
    resolve_continuity(shots)
    assert shots[1]["reference_videos"] == ["u0"]     # 组内锚 + 接力（去重后一条）
    assert "reference_videos" not in shots[0]          # 锚镜不引用自己
    assert "reference_videos" not in shots[2]          # 新组首镜不引用自己


# ---------- 视频 Provider：状态映射（真实踩过的字段/子串坑） ----------


class _FakeResp:
    def __init__(self, data: dict):
        self._data = data
        # Provider 的 key 轮换会读状态码判断「该不该换 key」，桩里给全字段。
        self.status_code = 200
        self.text = ""

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


def _monkeypatch_client(monkeypatch, module, data: dict) -> None:
    class _FakeClient:
        def __init__(self, *a, **k) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def get(self, *a, **k) -> _FakeResp:
            return _FakeResp(data)

        async def post(self, *a, **k) -> _FakeResp:
            return _FakeResp(data)

    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeClient)


def test_minimax_poll_unwraps_nested_task_and_success_substring(monkeypatch):
    """MiniMax：任务对象嵌在 task 下；'succeeded' 不含子串 'success'，必须能正确映射。"""
    import app.video.providers.minimax as mm
    _monkeypatch_client(monkeypatch, mm, {"task": {"status": "succeeded", "content": {"url": "http://x/b.mp4"}}})
    st = asyncio.run(mm.MiniMaxProvider(api_key="k").poll_job("t2"))
    assert st.status == "succeeded"
    assert st.video_url == "http://x/b.mp4"


def test_minimax_poll_maps_processing_to_generating(monkeypatch):
    import app.video.providers.minimax as mm
    _monkeypatch_client(monkeypatch, mm, {"task": {"status": "Processing"}})
    st = asyncio.run(mm.MiniMaxProvider(api_key="k").poll_job("t3"))
    assert st.status == "generating"
    assert st.video_url is None


def test_minimax_create_job_uses_user_model(monkeypatch):
    """用户在设置里选的模型必须进请求体；未配置时回落默认 H3。"""
    import app.video.providers.minimax as mm

    bodies = []

    class _CaptureClient:
        def __init__(self, *a, **k) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url: str, **k):
            bodies.append(k.get("json"))
            return _FakeResp({"task_id": "t9"})

    monkeypatch.setattr(mm.httpx, "AsyncClient", _CaptureClient)
    asyncio.run(mm.MiniMaxProvider(api_key="k", model="MiniMax-H3-Pro").create_job("p"))
    asyncio.run(mm.MiniMaxProvider(api_key="k").create_job("p"))
    assert bodies[0]["model"] == "MiniMax-H3-Pro"
    assert bodies[1]["model"] == "MiniMax-H3"


def test_registry_refreshes_cached_provider_model():
    """设置里改模型后，注册表缓存的实例要就地热更新（改配置无需重启）。"""
    from app.video.registry import ProviderRegistry
    from app.video.providers.minimax import MiniMaxProvider

    reg = ProviderRegistry()
    reg.register_class(MiniMaxProvider)
    p1 = reg.get_provider("minimax", api_key="k")
    p2 = reg.get_provider("minimax", api_key="k", model="MiniMax-H3-Pro")
    assert p1 is p2
    assert p2.model == "MiniMax-H3-Pro"


# ---------- 提交时兜底：台词剥除 + 参考解析 ----------


def test_dp_apply_strips_quoted_dialogue_from_prompt_body():
    """DP 组装最终提示词时也要过一遍台词兜底，引用台词不能漏进成片提示词。"""
    script = _script()
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                  camera=CameraMovement(type="static"))]
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["齐夏"],
         "prompt_body": "齐夏站在门口，开口说道：「你终于来了。」然后抬头。"},
    ]}]}
    out = _dp()._apply(shots, llm_out, script, "", None)[0].video_prompt
    assert "你终于来了" not in out
    assert "然后抬头" in out


def test_dp_apply_tags_anchors_with_asset_ids():
    """提示词里的每个视觉锚（人物/场景）都带资产库映射编号。

    用户拿到提示词后要能回答「这个人用的哪份资产」：顺着 @齐夏(char_a) 找到
    资产库条目，换一张定妆图就能影响所有引用它的镜头。
    """
    script = _script()
    guide = StyleGuide(
        character_appearances={"char_a": "二十岁男性，短发，深色夹克"},
        environment_descriptions={"loc_room": "昏暗的密室，铁桌居中"},
    )
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                  camera=CameraMovement(type="static"))]
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["齐夏"],
         "prompt_body": "齐夏推门而入。"},
    ]}]}
    out = _dp()._apply(shots, llm_out, script, "", guide)[0].video_prompt
    assert "@齐夏(char_a)" in out        # 人物锚：@名字(编号)
    assert "@密室(loc_room)" in out       # 环境锚：@名字(编号)

    # 无风格指南：人物名仍可从剧本解析出编号；环境锚没有可靠归属，不乱标。
    out2 = _dp()._apply(
        [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
              camera=CameraMovement(type="static"))],
        llm_out, script, "", None,
    )[0].video_prompt
    assert "@齐夏(char_a)" in out2
    assert "@密室(loc_room)" not in out2


def test_resolve_submission_references_chains_videos_and_fills_images():
    """提交时解析：前镜成片接力（chain_from / reference_group）+ 定妆图回填。"""
    script = _script()
    plan = [
        {"id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
         "reference_group": "ext_01", "chain_from": None},
        {"id": "shot_scene_001_001", "scene_id": "scene_001", "order": 1,
         "reference_group": "ext_01", "chain_from": 0},
    ]
    registry = {"密室": "http://img/env", "齐夏": "http://img/charA"}

    refs = resolve_submission_references(
        shot_id="shot_scene_001_001", shot_plan=plan,
        completed_urls={"shot_scene_001_000": "http://v/0.mp4"},
        image_registry=registry, script=script,
    )
    # 接力：镜 2 拿到镜 1 的成片；镜 1 自己（无更早成片）为空。
    assert refs["reference_videos"] == ["http://v/0.mp4"]
    # 定妆图：场景环境图在前、人物图按出场顺序在后。
    assert refs["reference_images"] == ["http://img/env", "http://img/charA"]

    first = resolve_submission_references(
        shot_id="shot_scene_001_000", shot_plan=plan,
        completed_urls={}, image_registry=registry, script=script,
    )
    assert first["reference_videos"] == []
    assert first["reference_images"] == ["http://img/env", "http://img/charA"]


# ---------- 成片质检 + 有界重 roll ----------


def test_qa_check_aggregates_fatal_frames_and_skips_without_frames(monkeypatch):
    """致命问题才 FAIL、逐帧聚合；抽帧失败降级为 skipped，绝不阻断任务。"""
    from app.video import qa

    class FakeVision:
        available = True

        def __init__(self, answers: list[str]) -> None:
            self.answers = list(answers)

        def ask(self, image_url: str, question: str) -> str:
            return self.answers.pop(0)

    monkeypatch.setattr(qa, "extract_frames", lambda url, count=5: ["data:a", "data:b", "data:c"])
    res = qa.qa_check("http://v", FakeVision([
        "VERDICT: PASS\nISSUE: none",
        "VERDICT: FAIL\nISSUE: 带子从肩膀穿过身体",
        "VERDICT: PASS\nISSUE: none",
    ]))
    assert res["verdict"] == "fail"
    assert res["frames"] == 3
    assert "带子从肩膀穿过身体" in res["issues"][0]

    monkeypatch.setattr(qa, "extract_frames", lambda url, count=5: [])
    assert qa.qa_check("http://v", FakeVision([]))["verdict"] == "skipped"

    # 解析不到判定时保守按通过（避免误判引发无意义的重 roll）
    assert qa._parse_answer("模型输出无法解析") == (True, "（未能解析判定，按通过处理）")


def test_reroll_if_needed_bounded_and_records_verdict(monkeypatch):
    """不合格且有预算 → 同参数重 roll（qa_attempt+1）；预算用尽 → 留备注不重 roll；合格 → 不动。"""
    from types import SimpleNamespace

    from app.video import qa

    def make_store(params: dict):
        created: list[dict] = []
        updates: list[dict] = []

        job = SimpleNamespace(
            id="j1", project_id="p", shot_id="s", provider="minimax", model="MiniMax-H3",
            prompt="P", params=params, version_id="v", cost_estimate=0.36,
            status="succeeded", video_url="http://v/0.mp4",
        )
        store = SimpleNamespace(
            get_video_job=lambda job_id: job,
            update_video_job=lambda job_id, **fields: updates.append(fields),
            create_video_job=lambda **kw: (created.append(kw), SimpleNamespace(id="j2"))[1],
        )
        return store, created, updates

    monkeypatch.setattr(qa, "qa_check", lambda url: {"verdict": "fail", "issues": ["第2帧：手穿模"], "frames": 5})
    submitted: list[str] = []
    submit = lambda job: submitted.append(job.id)  # noqa: E731

    # 第 0 次：不合格且有预算 → 反馈驱动重 roll（缺陷含「穿模」→ 兜底追加降级指令；
    # _llm_rewrite 打桩掉，测试不碰真实对话模型）
    monkeypatch.setattr(qa, "_llm_rewrite", lambda p, i: None)
    store, created, updates = make_store({"duration_sec": 8})
    new_id = qa.reroll_if_needed("j1", {"status": "succeeded"}, store=store,
                                 submit=submit, max_reroll=1)
    assert new_id == "j2" and submitted == ["j2"]
    assert created[0]["params"]["qa_attempt"] == 1
    assert created[0]["shot_id"] == "s"
    assert created[0]["prompt"].startswith("P") and "遮挡与让位" in created[0]["prompt"]
    assert created[0]["params"].get("qa_rewritten") is True

    # 第 1 次（预算用尽）：不再重 roll，写 error_message 备注
    store, created, updates = make_store({"duration_sec": 8, "qa_attempt": 1})
    new_id = qa.reroll_if_needed("j1", {"status": "succeeded"}, store=store,
                                 submit=submit, max_reroll=1)
    assert new_id is None and not created
    assert "QA 未通过" in updates[-1]["error_message"]

    # 合格：什么都不做，但 verdict 已记录
    monkeypatch.setattr(qa, "qa_check", lambda url: {"verdict": "pass", "issues": [], "frames": 5})
    store, created, updates = make_store({"duration_sec": 8})
    assert qa.reroll_if_needed("j1", {"status": "succeeded"}, store=store,
                               submit=submit, max_reroll=1) is None
    assert not created and updates[0]["params"]["qa"]["verdict"] == "pass"


def test_dp_final_frame_not_duplicated_when_body_paraphrases_it():
    """兜底不能因为「模型换了开头几个字」就把整句再追一遍。

    旧判定是 `_norm_key(final_frame)[:8] in body`：模型写成
    「他退到门口，冷光这才从他脸上熄下去」时前 8 字对不上，于是同一句
    「最终画面」连着出现两遍——视频模型会当成两个连续动作。
    """
    script = _script()
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                  final_frame="冷光熄灭在他脸上", camera=CameraMovement())]
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["齐夏"],
         "prompt_body": "他退到门口，冰箱的冷光这才一点一点熄灭在他脸上。"},
    ]}]}
    prompt = _dp()._apply(shots, llm_out, script, "", None)[0].video_prompt
    assert prompt.count("冷光") == 1, prompt


def test_dp_final_frame_still_backfilled_when_truly_missing():
    """改写不等于覆盖：真没写结束帧时必须补上（别把兜底也一起废掉）。"""
    from app.crew.dp import _covers

    # 同一句的轻微省字 / 语序变化都算写过
    assert _covers("冷光熄灭在他脸上", "冷光熄在他脸上")
    assert _covers("冷光熄灭在他脸上", "熄灭在他脸上的，是冰箱那点冷光")
    # 只提到「冷光」但没写结束状态，不算写过
    assert not _covers("冷光熄灭在他脸上", "冷光从冰箱里漏出来，照亮地板")
    assert not _covers("冷光熄灭在他脸上", "齐夏推开门走进走廊，脚步声很轻")


def test_dp_injects_detailed_anchor_only_for_subject():
    """只给本镜主体注详细外观。

    真跑《孔乙己》时，一个「酒客甲开口」的特写里挤进四个人，最抢眼的是背景里
    戴帽喝酒的那位——因为提示词把全场每个人的完整外貌都写了进去，模型只好都画。
    """
    script = _script()
    guide = StyleGuide(
        character_appearances={
            "齐夏": "黑色短发、深灰卫衣的年轻男性。",
            "人羊": "戴山羊头面具的黑西服男人。",
        },
        environment_descriptions={"密室": "密闭的方形房间，钨丝灯昏黄。"},
    )
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0,
        subject="人羊挡在门口", camera=CameraMovement(),
    )]
    llm_out = {"scenes": [{"scene_id": "scene_001", "shots": [
        {"shot_id": "shot_scene_001_000", "characters": ["人羊", "齐夏"],
         "prompt_body": "齐夏后退半步；人羊抬手拦住他。"},
    ]}]}
    out = _dp()._apply(shots, llm_out, script, "", guide)
    prompt = out[0].video_prompt

    assert "戴山羊头面具的黑西服男人" in prompt          # 主体有详细锚
    assert "深灰卫衣" not in prompt, f"背景人物不该带详细外貌：{prompt}"


def test_短剧默认竖屏():
    """产品只做短剧，短剧是竖屏。所有提交入口必须共用同一个默认值，
    否则「忘了传画幅」就会出一版横屏成片（2026-11-01 实测 1344x768）。"""
    from app.video.base import DEFAULT_ASPECT, VideoJobParams

    assert DEFAULT_ASPECT == "9:16"
    assert VideoJobParams().aspect_ratio == "9:16"


# ---------- H3 档位护栏（2026-11-12 账单复盘 + 文档确认） ----------


def _capture_bodies(monkeypatch, mm):
    bodies: list[dict] = []

    class _FakeResp:
        def __init__(self, payload: dict) -> None: self._p = payload
        def raise_for_status(self) -> None: ...
        def json(self) -> dict: return self._p
        headers: dict = {"content-type": "application/json"}
        status_code = 200

    class _CaptureClient:
        def __init__(self, *a, **k) -> None: ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a) -> bool: return False
        async def post(self, url, **k):
            bodies.append(k.get("json"))
            return _FakeResp({"task_id": "t9"})

    monkeypatch.setattr(mm.httpx, "AsyncClient", _CaptureClient)
    return bodies


def test_h3档位收敛分辨率与时长(monkeypatch):
    """Highspeed 没有 2K、Max 不支持 4s——create_job 必须按档位收敛，
    而不是只对照全类 supported_resolutions。"""
    from app.video import providers
    from app.video.base import VideoJobParams
    from app.video.providers import minimax as mm

    bodies = _capture_bodies(monkeypatch, mm)
    p = mm.MiniMaxProvider(api_key="k", model="MiniMax-H3-Highspeed")
    asyncio.run(p.create_job("p", VideoJobParams(duration_sec=4, resolution="2K")))
    assert bodies[-1]["resolution"] in ("480P", "768P")

    p2 = mm.MiniMaxProvider(api_key="k", model="MiniMax-H3-Max")
    asyncio.run(p2.create_job("p", VideoJobParams(duration_sec=4, resolution="480P")))
    assert bodies[-1]["duration"] >= 5  # Max 下限 5s


def test_首尾帧与参考素材互斥时参考优先(monkeypatch):
    """官方文档：first/last_frame 与 reference_* 不可混用。
    混了按参考优先提交（接力是我们链路的主用法），丢弃首尾帧并警告。"""
    from app.video.base import VideoJobParams
    from app.video.providers import minimax as mm

    bodies = _capture_bodies(monkeypatch, mm)
    p = mm.MiniMaxProvider(api_key="k")
    params = VideoJobParams(duration_sec=4, image_url="http://a/f.png",
                            reference_videos=["http://a/prev.mp4"])
    asyncio.run(p.create_job("p", params))
    roles = [c.get("role") for c in bodies[-1]["content"]]
    assert "first_frame" not in roles
    assert "reference_video" in roles
    # 无参考时首帧照常
    asyncio.run(p.create_job("p", VideoJobParams(duration_sec=4, image_url="http://a/f.png")))
    roles2 = [c.get("role") for c in bodies[-1]["content"]]
    assert "first_frame" in roles2


def test_计费估算含输入秒数():
    """旧版只按输出秒估（¥1.3/镜 vs 实际 ¥8.3）。新公式必须把
    参考视频秒数与参考图张数算进去。"""
    from app.video.base import VideoJobParams
    from app.video.providers.minimax import MiniMaxProvider

    p = MiniMaxProvider(api_key="k", model="MiniMax-H3")
    params = VideoJobParams(
        duration_sec=4,
        reference_images=["http://a/1.png"] * 4,
        reference_videos=["data:video/mp4;base64,AAA", "http://a/whole.mp4"],
    )
    usd = p.estimate_cost_real(params, 4)
    cny = usd * 7.1
    # 输出4s + 裁尾2.5s + 整条≈4s = 10.5s × ¥0.5 + 4图 × ¥0.2 = ¥6.05
    assert 5.5 <= cny <= 7.0, cny
    # 纯文生（无参考）= 输出秒价
    plain = MiniMaxProvider(api_key="k", model="MiniMax-H3")
    assert plain.estimate_cost_real(VideoJobParams(duration_sec=6), 6) * 7.1 == pytest.approx(3.0, abs=0.01)
