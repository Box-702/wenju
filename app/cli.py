# =====================================================================
# cli.py —— 命令行端到端演示
#
# 用一条命令跑通整套流程（无需 Web）：
#   导入示例文本 -> 生成剧本版本 -> Agent 提改编建议 -> 审阅 -> 接受 -> 新版本
#
# 主要用于：在无前端、甚至无模型 key 的情况下，快速验证整个
# LangGraph 人机协同 Agent 链路是否闭环。
# =====================================================================

from __future__ import annotations

import json
from typing import Any

from .agent import runner as agent_svc
from .deps import llm, settings, store
from .pipeline.generation import generate_script
from .pipeline.patch import validate_script

SAMPLE_TEXT = (
    "第一章　雨夜\n"
    "凌晨三点，滨江路的路灯在雨里像一团化不开的黄。林然把车停在旧楼前，"
    "熄了火，雨水顺着挡风玻璃往下淌。收音机里播着今天的新闻：城东旧货市场"
    "失火，无人伤亡。他盯着那栋楼，深吸一口气，推开车门。\n"
    "楼里没有灯。走廊尽头的铁门虚掩着，锁扣上挂着一根断了一半的铜钥匙。"
    "林然捡起来看了看，铜钥上面刻着一行小字：十四。他走进铁门，脚下是潮湿的"
    "水泥地，空气里有股烧焦的霉味。\n"
    "阿姐坐在里头的一把折叠椅上，背对着门，头发散下来。她说：“你终于来了。”"
    "林然没答话，把钥匙放在她面前的桌角。阿姐没回头，指尖在桌面上敲了两下："
    "“钥匙不对。”\n"
    "灯突然亮了。角落里站着一个穿红雨衣的男人，把一张照片按在桌上，照片上"
    "是旧货市场失火前夜的样子，里头有一扇锈蚀的铁门，门上写着十四。\n"
    "林然的手机在口袋里震了一下。屏幕上是一条陌生短信：别信你姐，她骗你。"
)


def _print_json(label: str, payload: Any) -> None:
    print(f"\n===== {label} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    # 与 Web 入口一致：CLI 也必须先把 .env 落进 os.environ，
    # 否则多 key 轮换（app/keys.py）、FFMPEG_PATH 这些直读环境变量的功能会拿到空值。
    from .config import bootstrap_env

    bootstrap_env()

    cfg = settings()
    store_ = store()
    # 与 Web 入口一致：CLI 也把 .env 现有配置幂等入库（D-41），
    # 否则 CLI 跑出来的模型和 Web 不是同一套。
    try:
        from .llm.seed import seed_keys_from_env
        seed_keys_from_env(cfg, store_)
    except Exception:  # noqa: BLE001
        pass
    llm_ = llm()

    print("【1】创建项目")
    project = store_.create_project(
        title="雨夜",
        adaptation_type="short_drama",
        language="zh-CN",
        raw_text=SAMPLE_TEXT,
    )
    print(f"项目 id: {project.id}，模型可用: {llm_.available}")

    print("\n【2】生成剧本版本")
    script, artifacts = generate_script(
        llm_, cfg, title=project.title, raw_text=project.raw_text,
        adaptation_type=project.adaptation_type, language=project.language,
    )
    version = store_.create_version(
        project, script, source_type="generation", label="初始生成", notes=f"模式={artifacts.get('mode')}"
    )
    issues = validate_script(script)
    print(f"版本 id: {version.id}，生成模式: {artifacts.get('mode')}，校验问题: {len(issues)}")
    print(f"场景数: {len(script.scenes)}，人物: {[c.name for c in script.characters]}")

    print("\n【3】启动 Agent 改编运行（跑到 review 中断）")
    run = agent_svc.start_agent_run(
        store_, llm_, cfg,
        project=project,
        base_version=version,
        instruction="把对白改得更口语、节奏更紧凑，并把场景标题改得更抓人。",
        scene_ids=[],
    )
    _print_json("Agent 提议", {"run_id": run["run_id"], "status": run["status"], "plan": run["plan"], "patch": run["patch"]})

    print("\n【4】接受全部 patch")
    result = agent_svc.resume_agent_run(
        store_, llm_, cfg,
        run_id=run["run_id"],
        action="accept",
        patch_indexes=None,
    )
    _print_json("接受结果", result)

    print("\n【5】查看新版本校验")
    if result.get("new_version_id"):
        new_ver = store_.get_version(result["new_version_id"])
        new_issues = validate_script(new_ver.script)
        print(f"新版本: {new_ver.id}，校验问题: {len(new_issues)}")
        for c in new_ver.script.scenes:
            print(f"  - 场景 {c.id} {c.title}")
    print("\n完成。")


if __name__ == "__main__":
    main()
