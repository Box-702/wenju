# =====================================================================
# test_beat_granularity.py —— 一个分镜 = 一个小事件（2026-11-20）
#
# 《替身v2》实测：模型给的一镜平均 7.4 个逗号短语（≈8 个动作）挤在 4–6 秒里，
# 视频模型做不完——人物被挤出画外（004_017 只剩半张脸）、动作根本没发生。
# 旧口径「段数下限 3 段 / 上限 时长÷3 → 打回重写」是治标：模型照旧写长，
# 而打回要烧一整轮导演调用。
#
# 现在改成确定性拆分（director.split_action_chains）：
#   一个分镜 = 一个主动作 + 两笔细节；过长的动作链拆成相邻分镜；
#   总时长守恒（出片单位是「片段」= 组，片段时长不变 → 花费与片长不变）。
# =====================================================================

from __future__ import annotations

from app.crew.director import (
    _renumber_shots,
    auto_group_scene,
    split_action_chains,
)
from app.domain import CameraMovement, SceneBreakdown, Shot


def _shot(action: str, **kw) -> Shot:
    data = {
        "id": "shot_scene_001_000",
        "scene_id": "scene_001",
        "order": 0,
        "shot_type": "medium",
        "camera": CameraMovement(type="dolly", direction="right", speed="medium",
                                 path="随人物横移两米", height="1.5m"),
        "subject": "林远-β",
        "action": action,
        "duration_sec": 5.0,
        "reference_group": "rg_1",
        "chain_from": None,
    }
    data.update(kw)
    return Shot.model_validate(data)


def _bd(shots: list[Shot]) -> list[SceneBreakdown]:
    return [SceneBreakdown(scene_id="scene_001", shots=shots)]


_LONG = ("林远-β站起来，走到橱柜前，把锡纸仔细折成一个小方块；"
         "他拉开橱柜最深处的那扇门，把锡纸塞进旧罐子后面；"
         "手从罐子后面抽出来，把罐子推回原位，关上柜门")


def test_动作链过长才拆():
    bds = _bd([_shot(_LONG)])
    n = split_action_chains(bds)
    shots = bds[0].shots
    assert n == 2                      # 8 个短语 → 3+3+2 → 3 段（多出 2 段）
    assert len(shots) == 3
    # 每段最多 3 个短语（一个主动作 + 两笔细节）
    for s in shots:
        assert len([p for p in s.action.split("，") if p.strip()]) <= 3


def test_正常分镜不拆():
    bds = _bd([_shot("他抓起衣服，绕过桌角走到舱前，手腕一松把瓶子丢到地板上")])
    assert split_action_chains(bds) == 0
    assert len(bds[0].shots) == 1


def test_拆完总时长守恒():
    bds = _bd([_shot(_LONG, duration_sec=6.0)])
    split_action_chains(bds)
    total = sum(s.duration_sec for s in bds[0].shots)
    # 守恒（允许每段下限 2 秒带来的小幅上浮，但不得翻倍）
    assert 6.0 <= total <= 8.0


def test_运镜只留第一段_末帧只留最后一段():
    bds = _bd([_shot(_LONG)])
    split_action_chains(bds)
    shots = bds[0].shots
    assert shots[0].camera.path == "随人物横移两米"
    assert all(not s.camera.path for s in shots[1:])
    assert all(s.camera.type == "static" for s in shots[1:])
    # final_frame 默认空，这里显式给一个，验证只落在最后一段
    bds2 = _bd([_shot(_LONG, final_frame="舱门合上，绿灯亮")])
    split_action_chains(bds2)
    assert bds2[0].shots[-1].final_frame == "舱门合上，绿灯亮"
    assert all(not s.final_frame for s in bds2[0].shots[:-1])


def test_台词落在含开口字样的那段():
    action = ("林远的视线从蛋糕上抬起来，看向同事甲的方向，嘴唇动了一下，"
              "他开口说话，声音比平时轻")
    bds = _bd([_shot(action, dialogue_ref="beat_007")])
    split_action_chains(bds)
    holders = [s for s in bds[0].shots if s.dialogue_ref]
    assert len(holders) == 1
    assert "开口说话" in holders[0].action


def test_接力只留第一段():
    bds = _bd([_shot(_LONG, chain_from=0)])
    split_action_chains(bds)
    shots = bds[0].shots
    assert shots[0].chain_from == 0
    assert all(s.chain_from is None for s in shots[1:])


def test_拆分后重排order与id不重复():
    bds = _bd([_shot(_LONG), _shot("同事乙从右侧凑过来，一只手搭在打印机顶上", order=1)])
    split_action_chains(bds)
    _renumber_shots(bds, 0)
    ids = [s.id for s in bds[0].shots]
    assert len(ids) == len(set(ids))          # id 唯一
    orders = [s.order for s in bds[0].shots]
    assert orders == sorted(orders) == list(range(len(orders)))
    # 拆链兄弟的 beat 标记
    assert [s.beat_total for s in bds[0].shots[:3]] == [3, 3, 3]
    assert [s.beat_index for s in bds[0].shots[:3]] == [0, 1, 2]


def test_拆链兄弟落进同一组():
    bds = _bd([_shot(_LONG)])
    split_action_chains(bds)
    _renumber_shots(bds, 0)
    auto_group_scene(bds)
    gids = {s.group_id for s in bds[0].shots}
    assert len(gids) == 1 and gids != {""}    # 同一组（= 一次生成 = 一个片段）
    assert [s.group_index for s in bds[0].shots] == [0, 1, 2]
