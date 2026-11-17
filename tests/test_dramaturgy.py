# =====================================================================
# test_dramaturgy.py —— 「先戏剧，后语法」确定性审计核心测试
#
# 方法论落地的硬规则全部在本模块，这里固化每一类的判定与误伤边界：
#   - 占位词：检测 + 保守剥除（整段占位才删，混写句保留给审计告警）；
#   - 否定句：检测，但引号内的实体招牌（「禁止入内」）不误伤；
#   - 台词引用：说话动词后的引用要抓，招牌/字幕类引用不误伤；
#   - 细节律三细节、三工作 function、最终画面 final_frame、五锚点；
#   - 最终提示词审计：短 prompt / 占位词 / 否定句 / 台词 / 一镜多运镜。
# =====================================================================

from __future__ import annotations

from app.crew.dramaturgy import (
    audit_breakdowns,
    audit_screen_sides,
    audit_video_prompt,
    extract_screen_sides,
    find_camera_moves,
    find_negations,
    find_slop_words,
    refresh_prompt_audits,
    strip_camera_moves,
    strip_slop,
    strip_spoken_quotes,
    strip_style_block,
    three_details,
)
from app.domain import SceneAnchors, SceneBreakdown, Shot


# ---------- 占位词 ----------


def test_find_slop_words_zh_and_en():
    assert "电影感" in find_slop_words("夜景，电影感，雨")
    assert "cinematic" in find_slop_words("cinematic shot of a man")
    assert "high quality" in find_slop_words("A high quality portrait")
    assert find_slop_words("冰箱冷光熄灭在他脸上") == []
    assert find_slop_words("overcast daylight, chipped paint") == []


def test_strip_slop_drops_only_pure_segments():
    # 整段都是占位词 → 删
    assert strip_slop("夜景，电影感，雨") == "夜景，雨"
    assert strip_slop("epic, stunning, rainy street") == "rainy street"
    # 混写句保留（机械删词会破坏语法，交给审计告警）
    assert strip_slop("画面呈现电影感十足的夜景") == "画面呈现电影感十足的夜景"
    # 没有占位词时原样返回
    assert strip_slop(" fridge light dying on his face ") == " fridge light dying on his face "


# ---------- 否定句（含招牌误伤边界） ----------


def test_find_negations_detects_and_skips_sign_text():
    assert find_negations("她的手没有穿过带子") == ["没有"]
    assert find_negations("Do not let the hand pass through") == ["not"]
    # 实体招牌里的「禁止」不是给模型的否定指令
    assert find_negations("警戒带上写着「禁止入内」三个字") == []


# ---------- 台词引用 ----------


def test_strip_spoken_quotes_removes_dialogue_but_keeps_signs():
    # 官方音画句式（冒号挂引号；2026-11-13 判据放宽——任意「…说/喊/报告：「台词」」
    # 都算，旧字面判据把「哽咽说：」剥了，native 模式成片就没有台词人声）。
    official = "小赵快步迎上，开口报告：「死者从七楼摔下。」苏砚点头。"
    assert "死者从七楼摔下" in strip_spoken_quotes(official)
    assert "苏砚点头" in strip_spoken_quotes(official)
    # 旧写法：引号直接跟在说话动词后、没有冒号——会渲染成含混假语音，照旧剥除。
    legacy = "小赵快步迎上，开口报告「死者从七楼摔下。」苏砚点头。"
    assert "死者从七楼摔下" not in strip_spoken_quotes(legacy)
    sign = "警戒带上写着「禁止入内」三个字，雨丝不断。"
    assert strip_spoken_quotes(sign) == sign


# ---------- 细节律 ----------


def test_three_details_counts_categories():
    # 三类齐全：环境压力（lighting 字段）+ 身体微动作 + 声音锚点
    count, missing = three_details(
        lighting="钨丝灯在画面左上方",
        action="他下颌锁死，手指抠住门框",
        background_action="秒针的滴答声持续",
    )
    assert count == 3 and missing == []
    # 零细节 = 废镜头
    count, missing = three_details(action="他走过去", lighting="", background_action="")
    assert count == 0
    assert set(missing) == {"环境压力", "身体微动作", "声音锚点/视觉母题"}


# ---------- 导演侧：戏剧结构审计 ----------


def _shot(oid: str, **kw) -> Shot:
    defaults = dict(
        id=oid, scene_id="scene_001", order=0, duration_sec=8.0,
        function="pressure", final_frame="冰箱冷光熄灭在他脸上",
        action="他下颌锁死，手指停在门把上",
        lighting="唯一的冷光来自冰箱",
    )
    defaults.update(kw)
    return Shot(**defaults)


def test_audit_breakdowns_passes_complete_plan():
    bd = SceneBreakdown(
        scene_id="scene_001",
        anchors=SceneAnchors(emotion="饥饿即孤独", motif="玻璃上的反射", object="最后一段香肠",
                             break_point="他发现架子是空的", final_image="冷光熄灭在他脸上"),
        shots=[_shot("shot_scene_001_000")],
    )
    assert audit_breakdowns([bd]) == []


def test_audit_breakdowns_flags_wallpaper_and_empty_anchors():
    bd = SceneBreakdown(scene_id="scene_001", shots=[
        _shot("shot_scene_001_000", function="", final_frame="",
              action="他走过去", lighting=None),
    ])
    issues = " ".join(audit_breakdowns([bd]))
    assert "五锚点全部空缺" in issues
    assert "没有写 function" in issues
    assert "没有写 final_frame" in issues
    assert "零细节 = 废镜头" in issues


def test_audit_breakdowns_flags_thin_shot():
    bd = SceneBreakdown(scene_id="scene_001", shots=[
        # 只有环境压力一类细节
        _shot("shot_scene_001_000", action="他慢慢走过去"),
    ])
    issues = " ".join(audit_breakdowns([bd]))
    assert "一类细节" in issues


# ---------- 摄影指导侧：最终提示词审计 ----------


def test_audit_video_prompt_flags_all_killers():
    # 旧写法（无冒号）仍要拦；官方音画句式（冒号挂引号）2026-11-13 起保留不报。
    issues = audit_video_prompt({"video_prompt": "齐夏站在门口，开口说道「你终于来了。」"})
    joined = " ".join(issues)
    assert "引用台词" in joined
    assert audit_video_prompt({"video_prompt": "齐夏站在门口，开口说道：「你终于来了。」"}) == []


def test_audit_video_prompt_flags_slop_negation_and_multi_move():
    prompt = "电影感十足的走廊，镜头先推近又环绕，手没有碰到门。epic, masterpiece"
    issues = audit_video_prompt({"video_prompt": prompt})
    joined = " ".join(issues)
    assert "占位词" in joined
    assert "否定式描述" in joined
    assert "一镜多运镜" in joined


def test_audit_video_prompt_ignores_non_camera_movement_words():
    """「拉出一道冷光带」里的「拉出」不是 pull-out——不能报一镜多运镜。

    实测踩坑：修复前真实提示词 4/4 全被误判成「一镜多运镜」。
    """
    prompt = (
        "夜里的面馆，门外街灯冷白的光斜射进来，在靠门地面拉出一道冷光带。"
        "周野的筷子悬在碗上方停住，何清的勺子搁在碗边，两人都静止。"
    )
    assert audit_video_prompt({"video_prompt": prompt}) == []


def test_audit_video_prompt_excludes_film_wide_camera_style():
    """全片风格段每个镜头都带一遍，其中的运镜词不属于本镜——必须排除。

    否则所有镜头都会被误判成「一镜多运镜」，告警直接失去意义。
    """
    style = "客观机位，稳定为主，关键处缓慢推近。跟随人物时以缓慢横移和轻微推拉为主。"
    prompt = (
        "窄小面馆，钨丝灯从正上方偏后打下来，桌面被照出一圈暖黄。"
        "周野1号右手捏着筷子，把面条挑起来又放回去，何清2号低头喝汤。"
        "客观机位，稳定为主，关键处缓慢推近。跟随人物时以缓慢横移和轻微推拉为主，"
        "主色调 #1c1a17。"
    )
    issues = audit_video_prompt({"video_prompt": prompt}, style_context=style)
    assert "一镜多运镜" not in " ".join(issues)


def test_find_camera_moves_requires_camera_context():
    assert find_camera_moves("在靠门地面拉出一道冷光带") == []
    assert find_camera_moves("镜头极缓慢地向桌面推近") == ["推近"]
    assert set(find_camera_moves("镜头先推近再横移")) == {"推近", "横移"}


# ---------- 运镜检测分级（2026-11-17）：强/弱动词 + 语境作用域 ----------


def test_find_camera_moves_detects_move_far_from_camera_words():
    """机位词与运镜动词隔得远也必须命中——这是我们自己 SKILL.md 教的句式。

    旧实现用固定 14 字语境窗口，于是这句（机位词距「推进」22 字）检不出「推进」，
    审计器对它零告警，「一镜多运镜」也就零感知。
    """
    text = (
        "机位在苏砚左前方约两米五，齐腰高度略仰，沿通道中轴向前缓慢推进约六十厘米，"
        "由中景收至中近景，推进到位的最后半米减速停稳。"
    )
    assert "推进" in find_camera_moves(text)


def test_find_camera_moves_weak_verb_needs_clause_context():
    """弱动词（拉近/推入/后拉）只在同一分句有镜头语境词时才算运镜。"""
    assert find_camera_moves("两人之间的距离慢慢拉近") == []
    assert find_camera_moves("镜头向他的脸拉近") == ["拉近"]


def test_audit_video_prompt_flags_handheld_conflict_with_declared_move():
    """手持是独立机位类型——本镜已声明运镜又写手持晃动 = 一镜两种运动。"""
    prompt = (
        "镜头沿通道中轴缓慢向前推进约六十厘米，由中景收至中近景，末段减速停稳。"
        "机身带极轻的手持晃动，幅度小于一指宽。"
    )
    issues = audit_video_prompt({"video_prompt": prompt})
    assert "手持" in " ".join(issues)


def test_audit_video_prompt_allows_handheld_in_static_shot():
    """纯静止镜写手持晃动不报——规则 12 允许把它当真实感写。"""
    prompt = (
        "机位在老周右前方约两米，齐眼高度，中景，全程不移动；"
        "机身带极轻的手持晃动，焦点在他脸上短暂游移后重新合焦。"
    )
    assert audit_video_prompt({"video_prompt": prompt}) == []


def test_find_negations_grammar_layer_catches_synonyms():
    """词表之外的否定改写也要抓：没碰 / 未拿 / 别看。"""
    assert "没碰" in find_negations("他没碰那只碗")
    assert "未拿" in find_negations("她未拿杯子")
    assert "别看" in find_negations("别看镜头")
    # 正向表述不误伤：「脚步不停」是规则 8 自己推荐的写法。
    assert find_negations("低头快走掠过，脚步不停，衣角翻飞") == []


# ---------- 跨镜屏幕方位（轴线）----------


def test_extract_screen_sides_reads_name_and_side():
    sides = extract_screen_sides("苏砚在画面左侧，老周在画面右侧，两人相距三米。")
    assert sides.get("苏砚") == {"左"}
    assert sides.get("老周") == {"右"}


def test_extract_screen_sides_ignores_location_words():
    """「站在画面右侧墙边」里的「墙边」不是角色名，不能拿去比对左右。"""
    sides = extract_screen_sides("老周穿蓝色工装外套，站在画面右侧墙边。")
    assert "墙边" not in sides
    assert "老周" not in sides


def test_audit_screen_sides_flags_axis_flip_within_scene():
    """同一场戏里同一角色出现在画面两侧 = 疑似越轴（我们的第一杀手）。"""
    shots = [
        {"id": "shot_a", "scene_id": "scene_001", "order": 0,
         "video_prompt": "苏砚在画面左侧，老周在画面右侧，两人相距三米。"},
        {"id": "shot_b", "scene_id": "scene_001", "order": 1,
         "video_prompt": "苏砚在画面右侧，老周在画面左侧。"},
    ]
    issues = audit_screen_sides(shots)
    assert "shot_a" in issues and "shot_b" in issues
    assert "越轴" in " ".join(issues["shot_a"])


def test_audit_screen_sides_ignores_different_scenes():
    """不同场戏之间不比对——换场本来就换方位。"""
    shots = [
        {"id": "shot_a", "scene_id": "scene_001", "order": 0,
         "video_prompt": "苏砚在画面左侧。"},
        {"id": "shot_b", "scene_id": "scene_002", "order": 1,
         "video_prompt": "苏砚在画面右侧。"},
    ]
    assert audit_screen_sides(shots) == {}


def test_refresh_prompt_audits_adds_cross_shot_axis_issue():
    shots = [
        {"id": "shot_a", "scene_id": "scene_001", "order": 0,
         "video_prompt": "苏砚在画面左侧，老周在画面右侧，两人相距三米，风吹动他的衣角。"},
        {"id": "shot_b", "scene_id": "scene_001", "order": 1,
         "video_prompt": "苏砚在画面右侧，老周在画面左侧，远处货车驶过，尘土扬起。"},
    ]
    refresh_prompt_audits(shots)
    assert any("越轴" in i for i in shots[0]["prompt_audit"])
    assert any("越轴" in i for i in shots[1]["prompt_audit"])


# ---------- CLI 入口 ----------


def test_cli_audits_text_and_reports_issues(capsys):
    """手写提示词的人要能直接把文本丢进来当场看告警。"""
    from app.crew.dramaturgy import _cli

    assert _cli(["--text", "镜头缓慢推近他的脸，机身带极轻的手持晃动。"]) == 0
    out = capsys.readouterr().out
    assert "手持" in out

    assert _cli(["--text", "镜头缓慢推近他的脸，焦点在他脸上短暂游移后重新合焦。"]) == 0
    assert "未发现问题" in capsys.readouterr().out


def test_cli_json_output(capsys):
    import json as _json

    from app.crew.dramaturgy import _cli

    _cli(["--text", "电影感的走廊，镜头先推近又环绕。", "--json"])
    data = _json.loads(capsys.readouterr().out)
    assert any("占位词" in i for i in data["issues"])
    assert any("一镜多运镜" in i for i in data["issues"])


def test_strip_style_block_handles_trailing_punctuation_change():
    """风格段进提示词时末尾「。」会变成「，」——剥离必须容忍这个差异。"""
    style = "客观机位，稳定为主，关键处缓慢推近。呼吸感来自细微的手持浮动。"
    prompt = "面馆里灯光昏黄。客观机位，稳定为主，关键处缓慢推近。呼吸感来自细微的手持浮动，主色调 #111。"
    stripped = strip_style_block(prompt, style)
    assert "推近" not in stripped
    assert "面馆里灯光昏黄" in stripped


def test_audit_video_prompt_clean_prompt_passes():
    prompt = (
        "密闭的方形房间，钨丝灯从长桌正上方打下亮斑。人物：齐夏：黑色短发、深灰卫衣。"
        "镜头从后墙沿中轴缓慢推进，齐夏的下颌慢慢锁死，指节抵在桌沿，秒针的滴答声持续。"
        "冷光熄灭在他脸上。"
    )
    assert audit_video_prompt({"video_prompt": prompt}) == []


def test_refresh_prompt_audits_updates_shot_dicts_in_place():
    shots = [
        # 旧写法（引号直挂说话动词、无冒号）仍报「引用台词」。
        {"id": "a", "video_prompt": "他说道「进来。」"},
        {"id": "b", "video_prompt": ""},
    ]
    refresh_prompt_audits(shots)
    assert any("引用台词" in i for i in shots[0]["prompt_audit"])
    assert shots[1]["prompt_audit"] == []


# ---------- 运镜剥离（camera_style 逐字进每个镜头，不能带运动清单） ----------


def test_strip_camera_moves_removes_movement_list_but_keeps_identity():
    """风格段要剥掉「列举具体运镜」的子句，保留不冲突的风格身份。

    背景：camera_style 会被逐字拼进每个镜头。若它写着「以缓慢横移、推近、推拉为主」，
    每个镜头都会收到三四种运动的矛盾指令——而每个镜头本来就只有一种运镜。
    """
    style = (
        "客观机位，稳定为主，关键处缓慢推近。"
        "摄影机高度大致与坐姿人物视线齐平（约1.1米），偶尔略高于桌面俯看两人。"
        "跟随人物时略微提前或滞后半拍，镜头偶尔被前景的碗筷、蒸汽或路过的人短暂遮挡；"
        "快速摇移后有短暂失焦再合焦。"
        "运动节奏克制，以缓慢推近、轻微横移为主，保留真实拍摄的呼吸感，不做零瑕疵的机械运动。"
    )
    out = strip_camera_moves(style)

    # 运动清单必须没了
    for verb in ("推近", "横移", "跟随"):
        assert verb not in out, f"「{verb}」应被剥掉：{out}"
    # 风格身份必须还在
    for keep in ("客观机位", "视线齐平", "呼吸感", "失焦"):
        assert keep in out, f"「{keep}」不该被剥掉：{out}"
    # 不留悬空标点
    assert "，。" not in out and "。。" not in out, out


def test_strip_camera_moves_is_idempotent_and_safe_on_empty():
    assert strip_camera_moves("") == ""
    text = "客观机位，稳定为主。"
    assert strip_camera_moves(text) == strip_camera_moves(strip_camera_moves(text))


def test_strip_camera_moves_keeps_style_without_movements_intact():
    """不含运镜的风格描述不该被动过。"""
    style = "客观旁观视点，机位高度大致与坐姿视线齐平，保留细微的手持浮动。"
    assert strip_camera_moves(style) == style
