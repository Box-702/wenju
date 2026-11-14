# =====================================================================
# test_director_guard.py —— 导演分镜的确定性硬约束
#
# 《孔乙己》首版真跑暴露的两类问题，都是「规则写在技能里、但模型不照做」：
#   1. 一镜塞 5 个连续动作 → 视频模型演不完，动作糊成一团、手部穿模；
#   2. 一镜挤进两三个人说话 → 观众不知道是谁在说。
# 这里把它们变成断言：不满足就打回重拆，而不是祈祷模型听话。
# =====================================================================

from __future__ import annotations

from app.crew.director import DirectorAgent, action_budget, count_action_units
from app.domain import (
    Character,
    Location,
    Scene,
    SceneBreakdown,
    Script,
    ScriptBeat,
    Shot,
    Source,
)


def _script(beats: list[ScriptBeat]) -> Script:
    scene = Scene(
        id="scene_001", title="测试场", chapter_refs=["ch_001"], location_id="loc_a",
        characters=["char_a", "char_b", "char_c"], purpose="p", conflict="c", beats=beats,
    )
    return Script(
        title="测试", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="测", characters=[
            Character(id="char_a", name="甲"), Character(id="char_b", name="乙"),
            Character(id="char_c", name="丙"),
        ],
        locations=[Location(id="loc_a", name="屋")], scenes=[scene],
    )


def _check(shots: list[Shot], beats: list[ScriptBeat] | None = None) -> list[str]:
    agent = DirectorAgent.__new__(DirectorAgent)
    bd = SceneBreakdown(scene_id="scene_001", shots=shots)
    return agent._validate_continuity([bd], _script(beats or []))


def test_动作预算按时长算() -> None:
    assert action_budget(5.0) == 3      # 一个动作 + 两笔细节是常见写法
    assert action_budget(8.0) == 3
    assert action_budget(12.0) == 4
    assert count_action_units("甲推开门走进来；乙抬头看他；甲走到桌前站住") == 3


def test_一镜动作超载被打回() -> None:
    """《孔乙己》九镜全部超标（8–10 秒写 3–6 段动作），一个都没逃掉。"""
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
        action="甲推开门走进来；乙抬头看他；甲走到桌前站住；乙把碗放下",
    )]
    issues = _check(shots)
    assert any("段身体动作" in i for i in issues)


def test_动作密度合规时不再唠叨() -> None:
    # 新规则（2026-11-12 v3 复盘）下「合规」= ≥3 段、含一段位移级、不超上限
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
        action="甲推开门走进来，手指在门框上搭了一下；乙抬头看他；乙随即起身让出半条板凳",
    )]
    issues = _check(shots)
    assert not any("段身体动作" in i for i in issues)


def test_同段多人对话拆开后各自单说话人() -> None:
    """一段甲乙来回的对话按说话人拆成两镜：两个镜头都不该被判「多说话人」。"""
    beats = [
        ScriptBeat(id="beat_001", type="dialogue", speaker="char_a", line="你来了。"),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_b", line="我来了。"),
    ]
    shots = [
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
             action="甲坐在桌边，手指敲着碗沿；他抬眼看门口", dialogue_ref="beat_001"),
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, duration_sec=8,
             action="乙站在门口，手扶着门框；他朝桌边点头", dialogue_ref="beat_002",
             cut_reason="换说话人"),
    ]
    issues = _check(shots, beats)
    assert not any("个说话人" in i for i in issues)
    assert not any("没有任何镜头认领" in i for i in issues)


def test_一镜一说话人合规() -> None:
    beats = [
        ScriptBeat(id="beat_001", type="dialogue", speaker="char_a", line="你来了。"),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_a", line="坐罢。"),
    ]
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
        action="甲坐在桌边，手指敲着碗沿；他抬眼看门口", dialogue_ref="beat_001",
    )]
    issues = _check(shots, beats)
    assert not any("个说话人" in i for i in issues)
    assert not any("没有任何镜头认领" in i for i in issues)
    assert not any("念得完" in i for i in issues)


def test_对话没有镜头认领被打回() -> None:
    """没有 dialogue_ref 认领的对话，配音时无处安放，会落到画面无关的镜头上。"""
    beats = [
        ScriptBeat(id="beat_001", type="action", text="甲推门进来。"),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_a", line="温两碗酒。"),
    ]
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
        action="甲推门进来，手在门框上搭了一下；他抬头看柜台",
    )]
    issues = _check(shots, beats)
    assert any("没有任何镜头认领" in i for i in issues)


def test_台词念不完被打回() -> None:
    """10 秒镜头塞 60 字台词：每秒 4 字念不完，配音只能赶语速。"""
    long_line = "温两碗酒要一碟茴香豆这一段话一共六十字用来测试朗读时长是否够用真的挺长的啊"
    beats = [ScriptBeat(id="beat_001", type="dialogue", speaker="char_a", line=long_line)]
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=8,
        action="甲站在柜台前，手撑着台面；他抬眼看掌柜", dialogue_ref="beat_001",
    )]
    issues = _check(shots, beats)
    assert any("念得完" in i for i in issues)


def test_台词归属按说话人切断() -> None:
    """同一人连说的几句归同一镜，换人就断开——否则台词会从一个人嘴里念完。"""
    beats = [
        ScriptBeat(id="beat_001", type="dialogue", speaker="char_a", line="第一句。"),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_a", line="第二句。"),
        ScriptBeat(id="beat_003", type="dialogue", speaker="char_b", line="第三句。"),
    ]
    shots = [
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, dialogue_ref="beat_001"),
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, dialogue_ref="beat_003"),
    ]
    from app.domain import assign_dialogue_ranges

    ranges = assign_dialogue_ranges(shots, beats)
    assert [b.id for b in ranges["shot_scene_001_000"]] == ["beat_001", "beat_002"]
    assert [b.id for b in ranges["shot_scene_001_001"]] == ["beat_003"]


def test_一镜一人多笔细节不误伤() -> None:
    """同一个人一个动作写三笔细节（搁碗→抬下巴→指点）是正常写法，不该打回。"""
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=5,
        action="甲把空碗往柜台上一搁，碗底磕出脆响；他下巴一抬，嘴角咧开；手指点向对面",
    )]
    issues = _check(shots)
    assert not any("段身体动作" in i for i in issues)


# ---------- 分批拆解：长剧本不会因输出截断而整批失败 ----------


def _multi_scene_script(n: int) -> Script:
    scenes = [
        Scene(
            id=f"scene_{i:03d}", title=f"场{i}", chapter_refs=["ch_001"], location_id="loc_a",
            characters=["char_a"], purpose="p", conflict="c",
            beats=[ScriptBeat(id=f"beat_{i:03d}", type="action", text="甲站着")],
        )
        for i in range(1, n + 1)
    ]
    return Script(
        title="测试", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="测", characters=[Character(id="char_a", name="甲")],
        locations=[Location(id="loc_a", name="屋")], scenes=scenes,
    )


def _scene_json(scene_id: str, n_shots: int = 1, start: int = 0) -> dict:
    return {
        "scene_id": scene_id,
        "director_notes": "备注",
        "pacing": "fast",
        "shots": [
            {
                "order": start + i, "shot_type": "medium", "camera_type": "static",
                "subject": "甲", "action": "甲站着", "duration_sec": 4.0,
                "function": "establish", "final_frame": "甲站着",
                "reference_group": scene_id,
            }
            for i in range(n_shots)
        ],
    }


def test_长剧本按场分批拆且_order_全局连续():
    """7 场戏不能一次输出（会截断），必须分批；合并后 order 全局唯一且连续。"""
    from app.crew.director import MAX_SCENES_PER_BATCH

    script = _multi_scene_script(7)
    agent = DirectorAgent.__new__(DirectorAgent)
    calls: list[int] = []

    def fake_invoke_json(system, user):
        # 从 prompt 里数这一批有几场
        n = user.count("--- scene_")
        calls.append(n)
        ids = [line.split()[1] for line in user.splitlines() if line.startswith("--- scene_")]
        return [_scene_json(i, 1) for i in ids]

    agent._invoke_json = fake_invoke_json
    agent.llm = type("L", (), {"available": True})()
    agent._validate_continuity = lambda bds, sc=None, **kw: []   # 这里只验分批，不验内容

    result = agent.run(script)

    assert result.success
    assert len(calls) == 4                      # 7 场 / 每批 2 → 4 批
    assert max(calls) <= MAX_SCENES_PER_BATCH
    orders = [s.order for bd in result.data for s in bd.shots]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)      # 无重复
    assert len(orders) == 7


# ---------- 台词引用：必须指向本场真实存在的对白节拍 ----------
#
# 《孔乙己》真跑时 62 镜里 35 个标了 dialogue_ref，对照剧本只有 3 个是真的，
# 28 个是模型自己拼的（beat_003_dlg_001）——因为 prompt 里当时没给 beat id。
# 后果不是报错，是「配音静默跳过 / 画面照着别人的手」，两条线永不交汇。


def _beats_with_dialogue() -> list[ScriptBeat]:
    return [
        ScriptBeat(id="beat_001", type="action", text="甲把铜钱排在柜台上"),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_a", line="九文大钱。"),
        ScriptBeat(id="beat_003", type="dialogue", speaker="char_b", line="又来赊账？"),
    ]


def test_编造的台词引用被打回():
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=4,
        action="甲把钱排在柜台上", dialogue_ref="beat_002_dlg_001",
    )]
    issues = _check(shots, _beats_with_dialogue())
    assert any("在剧本里不存在" in i for i in issues), issues


def test_指向动作节拍的引用被打回():
    """引用了一个真实存在的 id，但它是动作节拍——同样配不出音。"""
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=4,
        action="甲把钱排在柜台上", dialogue_ref="beat_001",
    )]
    issues = _check(shots, _beats_with_dialogue())
    assert any("不是对白节拍" in i for i in issues), issues


def test_正确的台词引用不报错():
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=4,
        action="甲把钱排在柜台上", dialogue_ref="beat_002", dialogue_at=1.0,
    )]
    issues = _check(shots, _beats_with_dialogue())
    assert not [i for i in issues if "不存在" in i or "不是对白节拍" in i], issues


def test_导演prompt里必须带beat_id():
    """模型编 id 的根因是 prompt 没给 id——这里锁住「给了」。"""
    script = _multi_scene_script(1)
    agent = DirectorAgent.__new__(DirectorAgent)
    prompt = agent._build_prompt(script, script.scenes[:1], "short_drama", "realistic")
    assert "[动作 beat_001]" in prompt


# =====================================================================
# 确定性片长闸门 enforce_duration_budget（2026-11-12 孔乙己实测后加：
# 批评回喂管不住的镜头数，代码来管——每镜都是真金白银）
# =====================================================================

from app.crew.director import CAP_SHOT_SEC, enforce_duration_budget


def _bd(scene_id: str, shots: list[Shot]) -> SceneBreakdown:
    return SceneBreakdown(scene_id=scene_id, shots=shots)


def _shot(order: int, *, dur: float = 5.0, fn: str = "action",
          dlg: str | None = None, scene_id: str = "scene_001") -> Shot:
    return Shot(
        id=f"shot_{scene_id[6:]}_{order:03d}", scene_id=scene_id, order=order,
        duration_sec=dur, function=fn, dialogue_ref=dlg,
    )


def test_闸门_预算内原样返回():
    shots = [_shot(i, dur=4) for i in range(5)]  # 20s
    out, notes = enforce_duration_budget([_bd("scene_001", shots)], 120)
    assert sum(len(b.shots) for b in out) == 5 and not notes


def test_闸门_超长镜头先封顶():
    shots = [_shot(i, dur=12) for i in range(20)]  # 240s → 封顶后 120s，恰好进预算
    out, notes = enforce_duration_budget([_bd("scene_001", shots)], 120)
    assert all(s.duration_sec <= CAP_SHOT_SEC for b in out for s in b.shots)
    assert sum(len(b.shots) for b in out) == 20  # 封顶就够了，不删镜
    assert any("封顶" in n for n in notes)


def test_闸门_删镜优先壁纸与无台词():
    # 10 镜 × 6s = 60s… 不超。构造超支：20 镜 6s=120s 再塞 10 镜 → 必须删
    shots = []
    for i in range(15):
        shots.append(_shot(i, dur=6, fn="action", dlg=f"beat_{i:03d}"))   # 有台词正镜
    for i in range(15, 25):
        shots.append(_shot(i, dur=6, fn=""))                               # 壁纸无台词
    out, notes = enforce_duration_budget([_bd("scene_001", shots)], 120)
    kept = [s for b in out for s in b.shots]
    assert sum(s.duration_sec for s in kept) <= 120
    # 先删的应该是壁纸（后 10 个 order 更大）
    assert all(s.order < 15 or s.function for s in kept) or \
        sum(1 for s in kept if not s.function) < 10
    assert any("删掉" in n for n in notes)


def test_闸门_带台词的镜头最后才动():
    shots = [_shot(i, dur=6, fn="action", dlg="beat_001") for i in range(3)]
    shots += [_shot(i, dur=6, fn="") for i in range(3, 25)]  # 22 个壁纸
    out, _ = enforce_duration_budget([_bd("scene_001", shots)], 120)
    kept = [s for b in out for s in b.shots]
    # 3 个有台词的都保住了
    assert sum(1 for s in kept if s.dialogue_ref) == 3


def test_闸门_悬空接力引用被修复():
    shots = [_shot(0, dur=6, fn="action", dlg="beat_001"),
             _shot(1, dur=6, fn="action", dlg="beat_002")]
    shots[1].chain_from = 0
    shots += [_shot(i, dur=6, fn="") for i in range(2, 30)]
    # 让 order=0 先被删（壁纸优先删，但 0 有台词……改构造：0 是壁纸）
    shots[0].function = ""
    shots[0].dialogue_ref = None
    out, _ = enforce_duration_budget([_bd("scene_001", shots)], 120)
    kept = [s for b in out for s in b.shots]
    orders = {s.order for s in kept}
    for s in kept:
        if s.chain_from is not None:
            assert s.chain_from in orders and s.chain_from < s.order


def test_动作下限_两段微动作被打回():
    """v3 实测教训：24 镜 22 镜只写 2 段微动作 → 模型循环播放成 PPT。
    校验器必须打回 <3 段的 action。"""
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=4,
        action="孔乙己嘴唇抿成一条线；右手仍按在柜台上没收回来",
    )]
    issues = _check(shots)
    assert any("至少 3 段" in i for i in issues), issues


def test_动作下限_三段全微动也被打回():
    """段数够但全是面部/手指微动，同样循环感——要一段位移级大动作。"""
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=4,
        action="他抿了一下嘴；眉毛压低；指节慢慢收紧",
    )]
    issues = _check(shots)
    assert any("位移级" in i for i in issues), issues


def test_动作下限_有位移三段的合格镜头不误伤():
    shots = [Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=4,
        action="孔乙己俯身从怀里摸出钱袋；他把九枚铜钱一枚枚按在柜台面上；手从柜台收回，退后半步站直",
    )]
    issues = _check(shots)
    assert not [i for i in issues if "至少 3 段" in i or "位移级" in i], issues


# =====================================================================
# 镜头组（S1，PLAN-shot-group / D-46）
# =====================================================================


def test_组归一化_不连续自动拆组():
    """模型把不相邻的镜头标同组 → normalize 拆键，校验器不再报。"""
    from app.crew.director import _normalize_groups

    a = _shot(0, dur=4); a.group_id = "g1"
    b = _shot(1, dur=4); b.group_id = "g2"
    c = _shot(2, dur=4); c.group_id = "g1"   # 不连续
    bd = _bd("scene_001", [a, b, c])
    _normalize_groups([bd])
    assert a.group_id == "" or c.group_id != a.group_id  # g1 被拆开
    # 单成员组清空键
    assert not (a.group_id and sum(1 for x in (a, b, c) if x.group_id == a.group_id) < 2)


def test_组归一化_超15秒从尾拆():
    from app.crew.director import GROUP_MAX_SEC, _normalize_groups

    shots = []
    for i in range(4):
        s = _shot(i, dur=5); s.group_id = "g1"; shots.append(s)
    _normalize_groups([_bd("scene_001", shots)])
    g1 = [s for s in shots if s.group_id.startswith("g1")]
    for gid in {s.group_id for s in g1}:
        total = sum(s.duration_sec for s in shots if s.group_id == gid)
        assert total <= GROUP_MAX_SEC, (gid, total)
    # 前 3 个（15s）成组，第 4 个落单
    assert shots[0].group_id and shots[2].group_id == shots[0].group_id
    assert shots[3].group_id == ""


def test_组校验_合法组不唠叨_坏组报():
    good = []
    for i in range(3):
        s = _shot(i, dur=4); s.group_id = "g1"; s.group_index = i; good.append(s)
    issues = _check(good)
    assert not [x for x in issues if "镜头组" in x], issues
    # index 断链
    bad = [_shot(i, dur=4) for i in range(3)]
    for i, s in enumerate(bad):
        s.group_id = "g1"; s.group_index = i
    bad[2].group_index = 7
    issues = _check(bad)
    assert any("group_index" in x for x in issues), issues


def test_闸门按组删_不毁半段戏():
    from app.crew.director import enforce_duration_budget

    shots = []
    for i in range(3):
        s = _shot(i, dur=5, fn="emotion", dlg=f"beat_{i}")
        shots.append(s)
    # 一个 3 机组（15s）+ 6 个散镜（30s）：预算 20 → 应整组保住或整组删，不留残组
    for i in range(3, 9):
        shots.append(_shot(i, dur=5, fn=""))
    g = [_shot(0, dur=5), _shot(1, dur=5), _shot(2, dur=5)]
    for k, s in enumerate(g):
        s.group_id = "g1"; s.group_index = k; s.function = "emotion"
        s.dialogue_ref = f"beat_{k}"
    alls = g + [shots[j] for j in range(3, 9)]
    for k, s in enumerate(alls):
        s.order = k
    out, _notes = enforce_duration_budget([_bd("scene_001", alls)], 20)
    kept = [s for b in out for s in b.shots]
    g1 = [s for s in kept if s.group_id.startswith("g1")]
    # 组要么完整（3 员）要么整体消失，绝不残 1-2 员
    assert len(g1) in (0, 3), [s.id for s in g1]


def test_extract_title_书名行与章节噪声():
    """命名兜底提取器（2026-11-12）：真实素材格式必须对上。"""
    from app.pipeline.knowledge import extract_title

    # 孔乙己.txt 实际格式：首行书名
    assert extract_title("孔乙己\n鲁迅（1919年3月）\n\n鲁镇的酒店的格局……") == "孔乙己"
    # 首行是章节号 → 取正文首句截断
    t = extract_title("第1章\n\n我们所要介绍的是祥子，不是骆驼。")
    assert t and "，" not in t and len(t) <= 12
    # 空文本
    assert extract_title("") == "未命名作品"


# =====================================================================
# 跨场景组键隔离 + 最小体量（2026-11-14 P2 收口）
# =====================================================================


def test_组归一化_跨场景重复组键不合并():
    """组键只在场景内唯一：两个场景复用同名 group_id 时绝不并成一组
    （组=一次生成，跨场合并等于把两段不相邻的戏塞进同一次调用）。"""
    from app.crew.director import _normalize_groups

    def _grouped(order: int, scene_id: str) -> Shot:
        s = _shot(order, dur=4, scene_id=scene_id)
        s.group_id = "g1"
        return s

    a, b = _grouped(0, "scene_001"), _grouped(1, "scene_001")
    c, d = _grouped(0, "scene_002"), _grouped(1, "scene_002")
    _normalize_groups([_bd("scene_001", [a, b]), _bd("scene_002", [c, d])])
    # 各场各自成组，成员互不串场；键名可以撞（下游按 场景+组键 认）
    assert [s.group_id for s in (a, b)] == ["g1", "g1"]
    assert [s.group_id for s in (c, d)] == ["g1", "g1"]
    assert [s.group_index for s in (a, b)] == [0, 1]
    assert [s.group_index for s in (c, d)] == [0, 1]


def test_组归一化_不足最小体量拆回单镜():
    """组总时长 < GROUP_MIN_SEC → 整组拆回单镜。旧行为只在校验器里报告，
    模型拿不到批评就没人管，短组照样混出片（归一化阶段就地执行）。"""
    from app.crew.director import GROUP_MIN_SEC, _normalize_groups

    def _tagged(order: int, dur: float, gid: str) -> Shot:
        s = _shot(order, dur=dur)
        s.group_id = gid
        return s

    a, b = _tagged(0, 1.5, "g1"), _tagged(1, 1.5, "g1")
    c, d = _tagged(2, 2.0, "g2"), _tagged(3, 2.0, "g2")
    _normalize_groups([_bd("scene_001", [a, b, c, d])])
    assert a.group_id == "" and b.group_id == ""          # 3s < 4s 最小体量 → 拆
    assert c.group_id == "g2" and d.group_id == "g2"      # 4s == 最小体量 → 保留
    assert GROUP_MIN_SEC == 4.0
