# =====================================================================
# test_location_resolution.py —— 场景地点解析（2026-11-25）
#
# 《劳山道士》真实项目实测：场景规划把 5 个场景全落到 loc_001（劳山），
# 王生家堂屋和观宇都被写成山道；下游提示词因此全部带 @劳山 锚。
# 这里钉死三级解析：id → 名字 → 场景文字；raw 只是 fallback 时以文字为准。
# =====================================================================

from __future__ import annotations

from app.pipeline.generation import inherit_previous_location, resolve_location

ALIAS = {
    "劳山": "loc_001", "观宇": "loc_002", "王生家": "loc_003",
    "loc_001": "loc_001", "loc_002": "loc_002", "loc_003": "loc_003",
}
ALIAS5 = {
    "劳山": "loc_001", "观宇": "loc_002", "宴席": "loc_003",
    "月宫": "loc_004", "王生家": "loc_005",
    "loc_001": "loc_001", "loc_002": "loc_002", "loc_003": "loc_003",
    "loc_004": "loc_004", "loc_005": "loc_005",
}
ALIAS3 = {
    "劳山": "loc_001", "观宇": "loc_002", "王生家": "loc_003",
    "loc_001": "loc_001", "loc_002": "loc_002", "loc_003": "loc_003",
}


def test_地点按名字解析() -> None:
    assert resolve_location("观宇", ALIAS, "loc_001") == "loc_002"
    assert resolve_location("王生家", ALIAS, "loc_001") == "loc_003"


def test_地点按id解析() -> None:
    assert resolve_location("loc_002", ALIAS, "loc_001") == "loc_002"


def test_模型给fallback时以场景文字为准() -> None:
    # 模型一刀切给了 loc_001；场景文字里出现「王生家堂屋」→ 纠正
    assert resolve_location(
        "loc_001", ALIAS, "loc_001", "王生家堂屋，王生额头鼓起大包") == "loc_003"
    # 同段文字同时出现「劳山」和「观宇」时，取更靠后的具体场景
    assert resolve_location(
        "loc_001", ALIAS, "loc_001", "劳山顶观宇，王生跨进门槛") == "loc_002"


def test_显式id无冲突时trust() -> None:
    assert resolve_location("loc_002", ALIAS, "loc_001", "无地点线索") == "loc_002"


def test_显式id与场景文字冲突时以场景文字为准() -> None:
    # 模型填了 loc_002，但动作里明确是王生家堂屋 → 以文字为准
    assert resolve_location("loc_002", ALIAS, "loc_001", "王生家堂屋") == "loc_003"


def test_近义场景词_月夜仙宴认到宴席() -> None:
    # 标题里有「宴」，正文有客/酒；宴席要压过观宇（堂内/道士）和月宫（月）
    assert resolve_location(
        "loc_001", ALIAS5, "loc_001",
        "傍晚观中，堂内道士与两位客人对坐饮酒，嫦娥从月中飘出",
        "月夜仙宴") == "loc_003"


def test_近义场景词_堂内认到观宇() -> None:
    # 辞行场没有地点全名，「走进堂内」+道士 → 观宇
    assert resolve_location(
        "loc_001", ALIAS5, "loc_001",
        "辞行求术 又一个月后，王生走进堂内，道士坐在蒲团上") == "loc_002"


def test_近义场景词_堂屋认到王生家() -> None:
    assert resolve_location(
        "loc_001", ALIAS5, "loc_001",
        "撞墙之后 王生家堂屋，妻子扶着桌沿笑弯了腰") == "loc_005"


def test_认不出时回退第一个地点() -> None:
    assert resolve_location("", ALIAS, "loc_001", "无地点线索") == "loc_001"


def test_v3_夜宴奇观从王生家纠正到观宇() -> None:
    # v3 没有宴席地点，模型把这场填成王生家；标题「夜宴奇观」+观内/道士 → 观宇
    assert resolve_location(
        "loc_003", ALIAS3, "loc_001",
        "傍晚观内，道士与客人对坐饮酒，众人争抢酒壶", "夜宴奇观") == "loc_002"


def test_v3_辞行传术从王生家纠正到观宇() -> None:
    assert resolve_location(
        "loc_003", ALIAS3, "loc_001",
        "王生走进堂内，道士坐在蒲团上抬眼看他们", "辞行") == "loc_002"


def test_弱线索场景沿用上一场地点() -> None:
    # 辞行/传术只有「道士」一个弱线索：前一场是观宇，就沿用观宇
    assert inherit_previous_location(
        "loc_003", "loc_002", False, "王生走到道士面前，摊开手") == "loc_002"


def test_强线索场景不沿用上一场() -> None:
    # 撞墙场有「王生家」全名（strong=True），不能被上一场观宇带走
    assert inherit_previous_location(
        "loc_003", "loc_002", True, "王生家堂屋，妻子笑弯了腰") == "loc_003"
    assert inherit_previous_location(
        "loc_003", None, False, "王生走到道士面前") == "loc_003"
