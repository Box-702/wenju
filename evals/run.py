#!/usr/bin/env python
"""提示词质量 eval 的命令行入口。

用法：
    # 跑全部 case，只跑确定性层（快、便宜）
    python -m evals.run

    # 加上 LLM judge 打分（慢、贵，但能看到质量趋势）
    python -m evals.run --judge

    # 只跑某几条 case
    python -m evals.run --cases dialogue_tension,single_pressure

    # 目标视频 provider（决定提示词方言）；对话模型与产品同源（DB/BYOK，D-48）
    python -m evals.run --provider seedance --provider-label Seedance

    # 存 JSON 报告
    python -m evals.run --out eval_report.json

退出码：0 = 通过（无 critical），1 = 未通过，2 = 环境问题（没配 key 等）。
"""

from __future__ import annotations

import argparse
import sys

from .cases import ALL_CASES, get_case
from .runner import EvalRunner, build_eval_llm


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提示词质量 eval")
    parser.add_argument("--cases", default="", help="只跑指定 case（逗号分隔的 case id）")
    parser.add_argument("--judge", action="store_true", help="额外跑 LLM judge 打分")
    parser.add_argument("--provider", default="minimax", help="目标视频 provider（决定提示词方言）")
    parser.add_argument("--provider-label", default="MiniMax")
    parser.add_argument("--max-duration", type=float, default=6.0)
    parser.add_argument("--judge-max-shots", type=int, default=3)
    parser.add_argument("--out", default="", help="把 JSON 报告写到指定路径")
    parser.add_argument("--no-retry", action="store_true", help="critical 时不自动复跑确认")
    args = parser.parse_args(argv)

    if args.cases:
        ids = [c.strip() for c in args.cases.split(",") if c.strip()]
        cases = [get_case(i) for i in ids]
    else:
        cases = list(ALL_CASES)

    try:
        llm = build_eval_llm()
    except Exception as e:  # noqa: BLE001
        print(f"无法构造模型：{e}", file=sys.stderr)
        return 2
    if not llm.available:
        print("未配置可用的对话模型，无法跑 eval（在应用设置里配 key，与产品同源）", file=sys.stderr)
        return 2

    print(f"模型：{llm.config.provider if llm.config else '?'} / "
          f"{llm.config.model if llm.config else '?'}（温度 {llm.config.temperature if llm.config else '?'}）")
    print(f"case 数：{len(cases)}｜judge：{'开' if args.judge else '关'}\n")

    runner = EvalRunner(
        llm,
        provider=args.provider,
        provider_label=args.provider_label,
        max_duration=args.max_duration,
        use_judge=args.judge,
        judge_max_shots=args.judge_max_shots,
        retry_on_critical=not args.no_retry,
    )
    report = runner.run_all(cases)

    print()
    print(report.render())

    if args.out:
        report.save(args.out)
        print(f"\n报告已写入 {args.out}")

    if report.blocked_total:
        print(f"\n注意：{report.blocked_total} 条 case 被基础设施挡住（通常是模型余额不足），"
              "这些没有真正评估到。")
        return 2
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
