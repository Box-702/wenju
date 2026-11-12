# =====================================================================
# cases.py —— Golden case 集（提示词质量回归的输入）
#
# 这些 case 是 eval 的「测试集」。设计原则：
#   - 每个 case 是一个**有真实戏剧张力**的微型剧本，不是「场景1/角色A」这种假数据；
#     假数据测不出质量，只能测出代码不崩。
#   - 覆盖不同的失败模式：动作戏、对话戏、单人戏、群戏、对话与动作混合。
#   - 剧本要**短**（一场到两场），这样一次 eval 全跑完的成本可控。
#
# case 的用途不是「正确答案」——导演和摄影指导的产出没有唯一解，
# 而是要**稳定地暴露质量退化**：改了提示词之后，分镜是否还在做功、
# 提示词是否还具体、锚是否还逐字复用。
# =====================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain import Character, Location, Scene, Script, ScriptBeat, Source


@dataclass
class EvalCase:
    """一条 eval 用例。"""

    id: str
    name: str
    desc: str
    script: Script
    #: 这条 case 特别关注的点（写进报告，帮人理解失败是不是真的重要）
    watch_for: list[str] = field(default_factory=list)
    #: 该 case 是否走英文（provider 方言测试用）
    language: str = "zh"

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "desc": self.desc,
            "scenes": len(self.script.scenes),
            "characters": len(self.script.characters),
            "watch_for": self.watch_for,
        }


def _script(
    *,
    title: str,
    logline: str,
    themes: list[str],
    characters: list[Character],
    locations: list[Location],
    scenes: list[Scene],
    chapters: int = 1,
) -> Script:
    return Script(
        title=title,
        logline=logline,
        themes=themes,
        source=Source(chapter_count=chapters, chapter_ids=[f"ch_{i:03d}" for i in range(1, chapters + 1)]),
        characters=characters,
        locations=locations,
        scenes=scenes,
    )


# =====================================================================
# Case 1 —— 单人戏：压迫感来自环境，不来自动作
# =====================================================================

CASE_SINGLE_PRESSURE = EvalCase(
    id="single_pressure",
    name="单人压迫戏",
    desc="一个人被环境困住。没有对手，靠物理空间和母题制造压力——"
         "最考验导演会不会拍成「一个人站着」的壁纸镜头。",
    watch_for=["镜头是否施加了压力而非只是记录", "细节律是否命中三类", "母题是否跨镜重复"],
    script=_script(
        title="雨夜独白",
        logline="一个躲债的人在废弃电话亭里等一个不会来的电话。",
        themes=["困顿", "等待"],
        characters=[Character(id="char_lin", name="林晚", role="protagonist")],
        locations=[Location(id="loc_booth", name="废弃电话亭")],
        scenes=[
            Scene(
                id="scene_001",
                title="电话亭",
                chapter_refs=["ch_001"],
                location_id="loc_booth",
                characters=["char_lin"],
                purpose="建立被围困的状态",
                conflict="想离开但走不了",
                entry_state="刚逃出来，浑身湿透",
                exit_state="决定不走",
                action=["林晚挤进电话亭，拉下锈死的门"],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 2 —— 对话戏：两个人，一句话不说但有交锋
# =====================================================================

CASE_DIALOGUE_TENSION = EvalCase(
    id="dialogue_tension",
    name="对峙对话戏",
    desc="两个角色在一张桌子上谈一笔交易，全程没有动作冲突。"
         "最考验摄影指导会不会写出「两人对坐」这种空洞画面。",
    watch_for=["对话戏的镜头是否仍做功", "是否写成静态对坐", "人物锚是否逐字复用"],
    script=_script(
        title="分账",
        logline="两个旧合伙人分道扬镳的那顿饭。",
        themes=["背叛", "清算"],
        characters=[
            Character(id="char_zhou", name="周野"),
            Character(id="char_he", name="何清"),
        ],
        locations=[Location(id="loc_noodle", name="路边面馆")],
        scenes=[
            Scene(
                id="scene_001",
                title="面馆",
                chapter_refs=["ch_001"],
                location_id="loc_noodle",
                characters=["char_zhou", "char_he"],
                purpose="摊牌",
                conflict="谁先开口承认是自己动了手脚",
                entry_state="两人假装吃饭",
                exit_state="桌子上一半的钱",
                action=["周野把筷子横在碗上", "何清没抬头"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 3 —— 动作戏：接触动作的物理过程
# =====================================================================

CASE_CONTACT_ACTION = EvalCase(
    id="contact_action",
    name="接触动作戏",
    desc="一次推搡。考验摄影指导有没有写清接触点、先后节拍、让位关系——"
         "这是视频模型最容易穿模的地方。",
    watch_for=["接触动作是否写了物理过程", "是否出现穿模风险", "一镜是否只有一种运镜"],
    script=_script(
        title="楼梯间",
        logline="一个追债的人在楼梯间被人拦下。",
        themes=["对抗", "退让"],
        characters=[
            Character(id="char_ma", name="马三"),
            Character(id="char_xu", name="徐姐"),
        ],
        locations=[Location(id="loc_stair", name="消防楼梯间")],
        scenes=[
            Scene(
                id="scene_001",
                title="拦下",
                chapter_refs=["ch_001"],
                location_id="loc_stair",
                characters=["char_ma", "char_xu"],
                purpose="拦下",
                conflict="要钱还是要说法",
                entry_state="马三在逃",
                exit_state="马三被按在墙上",
                action=["徐姐横臂挡在楼梯口", "马三肩膀撞上墙面"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 4 —— 群戏：背景人物各干各的
# =====================================================================

CASE_CROWD = EvalCase(
    id="crowd_background",
    name="公共场合群戏",
    desc="主角在一群陌生人中间。考验导演有没有写 background_action——"
         "背景人物同步动作会让画面死板。",
    watch_for=["背景人物是否各有各的事", "是否写成所有人同步反应", "主体是否从人群中可辨"],
    script=_script(
        title="早高峰",
        logline="一个失业的人在早高峰的地铁站里看着别人上班。",
        themes=["疏离", "落差"],
        characters=[Character(id="char_su", name="苏桥")],
        locations=[Location(id="loc_station", name="地铁站换乘厅")],
        scenes=[
            Scene(
                id="scene_001",
                title="换乘厅",
                chapter_refs=["ch_001"],
                location_id="loc_station",
                characters=["char_su"],
                purpose="建立落差",
                conflict="他和所有人走反方向",
                entry_state="刚被通知裁员",
                exit_state="逆着人流走",
                action=["苏桥站在人流中间不动"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 5 —— 两场戏：跨场一致性与接力
# =====================================================================

CASE_TWO_SCENES = EvalCase(
    id="two_scenes_chain",
    name="两场戏 + 接力",
    desc="两个场景，检查跨场的一致性（同一人物、同一光影基调）与"
         "chain_from 接力链路是否自洽。",
    watch_for=["跨场人物锚是否一致", "接力链路是否自洽", "环境锚是否按场区分"],
    script=_script(
        title="钥匙",
        logline="一个女人在出租屋找到一把不该存在的钥匙。",
        themes=["秘密", "追索"],
        characters=[
            Character(id="char_jiang", name="江予"),
            Character(id="char_qi", name="齐夏"),
        ],
        locations=[
            Location(id="loc_room", name="出租屋"),
            Location(id="loc_corridor", name="楼道"),
        ],
        scenes=[
            Scene(
                id="scene_001",
                title="抽屉",
                chapter_refs=["ch_001"],
                location_id="loc_room",
                characters=["char_jiang"],
                purpose="发现",
                conflict="这把钥匙是谁的",
                entry_state="在找东西",
                exit_state="手里攥着钥匙",
                action=["江予拉开床头柜抽屉"],
                dialogue=[],
                beats=[],
            ),
            Scene(
                id="scene_002",
                title="敲门",
                chapter_refs=["ch_001"],
                location_id="loc_corridor",
                characters=["char_jiang", "char_qi"],
                purpose="对峙",
                conflict="要不要开门",
                entry_state="拿着钥匙站在门外",
                exit_state="门开了一条缝",
                action=["齐夏的手停在门把上"],
                dialogue=[],
                beats=[],
            ),
        ],
        chapters=1,
    ),
)


# =====================================================================
# Case 6 —— 突发：情绪骤变（考验 final_frame）
# =====================================================================

CASE_EMOTION_FLIP = EvalCase(
    id="emotion_flip",
    name="情绪骤变",
    desc="从平静到崩溃的一瞬。考验导演有没有写清 final_frame——"
         "视频模型把最后一帧当情绪目的地，没写就会漂。",
    watch_for=["final_frame 是否写清可见状态", "情绪变化是否可见", "是否只有抽象描述"],
    script=_script(
        title="电话",
        logline="一个母亲接到孩子学校的电话。",
        themes=["恐惧", "失去"],
        characters=[Character(id="char_sun", name="孙曼")],
        locations=[Location(id="loc_school_gate", name="学校门口")],
        scenes=[
            Scene(
                id="scene_001",
                title="校门口",
                chapter_refs=["ch_001"],
                location_id="loc_school_gate",
                characters=["char_sun"],
                purpose="崩溃",
                conflict="老师说出那个名字",
                entry_state="在校门口等孩子",
                exit_state="钥匙掉在地上",
                action=["孙曼握着手机的手垂下去"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 7 —— 物件承担戏剧：人不在画面中心
# =====================================================================

CASE_OBJECT_CARRY = EvalCase(
    id="object_carry",
    name="物件承担戏剧",
    desc="一个人站在自己的旧屋门口，真正承载戏剧的是屋里的物件（空椅子、没扔的杯子）。"
         "考验导演会不会把主体写成「人」而让物件沦为背景——"
         "也考验摄影指导有没有把物件写成可渲染的实体。",
    watch_for=["物件是否承担戏剧功能", "是否只写人、不写物件", "细节律是否仍命中"],
    script=_script(
        title="空椅子",
        logline="一个人搬走后回来拿最后一个杯子。",
        themes=["缺席", "结束"],
        characters=[Character(id="char_yin", name="殷路")],
        locations=[Location(id="loc_apartment", name="旧客厅")],
        scenes=[
            Scene(
                id="scene_001",
                title="旧客厅",
                chapter_refs=["ch_001"],
                location_id="loc_apartment",
                characters=["char_yin"],
                purpose="呈现缺席",
                conflict="东西都还在，人已经不在",
                entry_state="钥匙还能开门",
                exit_state="只带走了一个杯子",
                action=["殷路站在门口没进去"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 8 —— 长动作链：一镜内多节拍
# =====================================================================

CASE_CHAIN_ACTION = EvalCase(
    id="chain_action",
    name="连续动作链",
    desc="一个人完成一串连续动作。考验导演会不会切成一堆碎片——"
         "分镜 = 一段连贯动作，不是「一人一切」。",
    watch_for=["是否被切成碎片", "是否出现切镜理由缺失", "连续性校验是否自洽"],
    script=_script(
        title="收拾",
        logline="一个失败的父亲在给孩子收拾书包。",
        themes=["愧疚", "父爱"],
        characters=[Character(id="char_guo", name="郭川")],
        locations=[Location(id="loc_childroom", name="儿童房")],
        scenes=[
            Scene(
                id="scene_001",
                title="书包",
                chapter_refs=["ch_001"],
                location_id="loc_childroom",
                characters=["char_guo"],
                purpose="用动作代替语言",
                conflict="说不出口的话都在手上",
                entry_state="孩子已经睡了",
                exit_state="书包拉链拉上",
                action=["郭川把作业本一本本塞进书包", "拉链卡住又拉开重拉"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 9 —— 室外夜景：光线方向与母题
# =====================================================================

CASE_NIGHT_EXTERIOR = EvalCase(
    id="night_exterior",
    name="室外夜景",
    desc="霓虹夜街。考验摄影指导有没有写清光源方向、有没有让光「永远黏在脸上」——"
         "这是夜景镜头最大的失败模式。",
    watch_for=["光线是否写了方向", "是否出现「光打在脸上」式黏光", "视觉母题是否成立"],
    script=_script(
        title="夜街",
        logline="一个躲债的人在霓虹灯下等一个不会来的人。",
        themes=["等待", "虚无"],
        characters=[Character(id="char_liang", name="梁述")],
        locations=[Location(id="loc_street", name="霓虹夜街")],
        scenes=[
            Scene(
                id="scene_001",
                title="街口",
                chapter_refs=["ch_001"],
                location_id="loc_street",
                characters=["char_liang"],
                purpose="消耗时间",
                conflict="对方不会来",
                entry_state="提前两小时到",
                exit_state="还在等",
                action=["梁述站在便利店灯箱下看表"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 10 —— 威胁：慢节奏但每镜都在收紧
# =====================================================================

CASE_SLOW_THREAT = EvalCase(
    id="slow_threat",
    name="慢节奏威胁",
    desc="一场没有爆发的威胁戏。考验导演会不会用「一人一切」凑镜头数——"
         "慢戏的镜头数应该由内容决定，不是越多越好。",
    watch_for=["镜头数是否由内容决定", "是否出现无功能的凑数镜头", "压力是否逐镜收紧"],
    script=_script(
        title="谈判",
        logline="一个债主在慢节奏里等欠债人开口。",
        themes=["压迫", "沉默"],
        characters=[
            Character(id="char_bai", name="白老板"),
            Character(id="char_dou", name="窦连"),
        ],
        locations=[Location(id="loc_office", name="空办公室")],
        scenes=[
            Scene(
                id="scene_001",
                title="等",
                chapter_refs=["ch_001"],
                location_id="loc_office",
                characters=["char_bai", "char_dou"],
                purpose="施压",
                conflict="谁先开口承认还不上",
                entry_state="两个人坐下",
                exit_state="窦连站起来走了",
                action=["白老板翻着账本不看人"],
                dialogue=[],
                beats=[],
            )
        ],
    ),
)


# =====================================================================
# Case 11 —— 排钱戏：镜头组语法的主战场（2026-11-12 D-46 / PLAN-shot-group S1）
# =====================================================================

CASE_MONEY_GROUP = EvalCase(
    id="money_group",
    name="排钱多机位戏",
    desc="穷客人把仅有的几枚铜钱一枚枚排在柜台上，掌柜从不耐烦到眯眼打量。"
         "关键道具揭示 = 镜头组的主战场：导演应把「排钱」拆成同一次生成的"
         "2-3 个机位（主体中景 → 钱的特写 → 掌柜过肩反应），而不是三个孤立镜头。",
    watch_for=[
        "是否产出 group_id 非空的镜头组（组=2-4 机位、总时长 ≤15s、order 连续）",
        "组内每机位是否只一个说话人/主体",
        "轴线：掌柜与客人在画面左右是否跨机位一致",
        "排钱是否被写成位移级动作弧线而非微动作循环",
    ],
    script=_script(
        title="柜前",
        logline="穷客人排出最后几枚铜钱，掌柜的态度从嫌弃变成打量。",
        themes=["体面", "窘迫"],
        characters=[
            Character(id="char_ke", name="客人", role="protagonist"),
            Character(id="char_zhang", name="掌柜", role="antagonist"),
        ],
        locations=[Location(id="loc_dang", name="当铺柜台")],
        scenes=[
            Scene(
                id="scene_001",
                title="排钱",
                chapter_refs=["ch_001"],
                location_id="loc_dang",
                characters=["char_ke", "char_zhang"],
                purpose="用一件道具完成权力关系的翻转",
                conflict="客人要体面，掌柜要拿捏",
                entry_state="客人袖口里攥着几枚铜钱",
                exit_state="掌柜沉默地把钱推回一半",
                action=["客人俯身把铜钱一枚枚排在柜台上"],
                beats=[
                    ScriptBeat(id="beat_001", type="action",
                               text="客人从袖中摸出钱袋，铜钱磕在柜台木面上"),
                    ScriptBeat(id="beat_002", type="dialogue", speaker="char_zhang",
                               line="就这些？"),
                    ScriptBeat(id="beat_003", type="action",
                               text="客人把最后一枚钱按在队列末尾，指节压得发白"),
                    ScriptBeat(id="beat_004", type="dialogue", speaker="char_ke",
                               line="一文不少。"),
                ],
            )
        ],
    ),
)


ALL_CASES: list[EvalCase] = [
    CASE_SINGLE_PRESSURE,
    CASE_DIALOGUE_TENSION,
    CASE_CONTACT_ACTION,
    CASE_CROWD,
    CASE_TWO_SCENES,
    CASE_EMOTION_FLIP,
    CASE_OBJECT_CARRY,
    CASE_CHAIN_ACTION,
    CASE_NIGHT_EXTERIOR,
    CASE_SLOW_THREAT,
    CASE_MONEY_GROUP,
]


def get_case(case_id: str) -> EvalCase:
    for c in ALL_CASES:
        if c.id == case_id:
            return c
    raise KeyError(f"未知 case：{case_id}")


def case_ids() -> list[str]:
    return [c.id for c in ALL_CASES]
