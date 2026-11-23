"""Budget-bounded real-model checks for the main chat Agent.

The harness reads only the selected chat provider/model fields and its API key
from the user's settings DB. It writes reports and temporary project data only
under evals/reports and the OS temp directory. Side-effect tools are replaced
with inert stubs; image/video tools are removed from the model schema.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from app.config import Settings, get_settings
from app.llm import LLM
from app.llm.base import ModelConfig
from app.llm.providers import build_chat_model
from app.store import Store

HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "agent_cases.json"
REPORT_DIR = HERE / "reports"

# DeepSeek Flash peak cache-miss prices, USD per 1M tokens (conservative).
INPUT_USD_PER_M = 0.30
OUTPUT_USD_PER_M = 1.20
# Budget conversion includes a cushion so the harness stops before the user's cap.
USD_TO_CNY_BUFFERED = 8.0

BLOCKED_TOOLS = {
    "start_production", "generate_reference_assets", "clone_voice",
    "submit_videos", "generate_video", "rerun_video", "cancel_video_job",
    "finalize_film", "blockout_build",
}
SAFE_TOOLS = {
    "load_skill", "get_status", "read_script", "read_source", "search_source",
    "list_projects", "list_assets", "ask_user_choice", "recall", "search_history",
}
SAMPLE_TEXT = (
    "第一章 雨夜。林然在旧楼门前发现一把刻着数字十四的铜钥匙。"
    "他推开铁门，听见楼上传来三下敲击。邻居说这栋楼已经空了十年。"
)


def _configured_chat() -> ModelConfig:
    """Load chat credentials in memory only; never print or serialize the key."""
    app_settings = get_settings()
    url = make_url(app_settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise RuntimeError("在线评测目前只支持从本地 SQLite 设置库读取模型配置")
    db_path = Path(url.database).expanduser().resolve()
    connection = __import__("sqlite3").connect(
        db_path.as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = __import__("sqlite3").Row
    try:
        row = connection.execute(
            """SELECT ap.name, ap.label, ap.base_url, ap.api_key,
                      mp.model_name, mp.params_json
               FROM model_preferences mp
               JOIN api_providers ap ON ap.id=mp.provider_id
               WHERE mp.task_type='chat' AND mp.is_default=1 AND ap.enabled=1
                 AND ap.api_key <> '' LIMIT 1"""
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise RuntimeError("设置库里没有启用且带 Key 的默认对话模型")
    if row["name"].lower() != "deepseek" or row["model_name"] != "deepseek-flash":
        raise RuntimeError(
            f"预算预估只核验了 DeepSeek deepseek-flash；当前选择为 "
            f"{row['name']} / {row['model_name']}，为避免超预算未调用模型"
        )
    try:
        params = json.loads(row["params_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    return ModelConfig(
        provider="deepseek", label=row["label"] or "DeepSeek",
        model="deepseek-flash", api_key=row["api_key"],
        base_url=row["base_url"], temperature=0,
        extra_body={**params, "_thinking": False}, source="db",
    )


def _usage_cost(events: list[dict[str, Any]]) -> dict[str, float | int]:
    input_tokens = sum(int(e.get("input_tokens") or 0) for e in events if e.get("type") == "model_usage")
    output_tokens = sum(int(e.get("output_tokens") or 0) for e in events if e.get("type") == "model_usage")
    usd = input_tokens * INPUT_USD_PER_M / 1_000_000 + output_tokens * OUTPUT_USD_PER_M / 1_000_000
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "estimated_usd_peak": round(usd, 6),
        "estimated_cny_buffered": round(usd * USD_TO_CNY_BUFFERED, 4),
    }


def _tool_name(event: dict[str, Any]) -> str:
    return str(event.get("name") or "")


def _load_name(event: dict[str, Any]) -> str:
    raw = event.get("args") or "{}"
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return str(value.get("name") or "") if isinstance(value, dict) else ""
    except (TypeError, json.JSONDecodeError):
        return ""


def _check(case: dict[str, Any], events: list[dict[str, Any]], reply: str) -> list[dict[str, Any]]:
    calls = [_tool_name(e) for e in events if e.get("type") == "tool_call"]
    skills = [_load_name(e) for e in events if e.get("type") == "tool_call" and e.get("name") == "load_skill"]
    skills = [name for name in skills if name]
    checks: list[dict[str, Any]] = []

    def add(label: str, passed: bool, actual: Any = None) -> None:
        checks.append({"check": label, "passed": bool(passed), "actual": actual})

    if case.get("must_call"):
        for name in case["must_call"]:
            add(f"called {name}", name in calls, calls)
    if case.get("required_skill"):
        required = case["required_skill"]
        add(f"loaded skill {required}", required in skills, skills)
    if case.get("must_precede"):
        positions = []
        for item in case["must_precede"]:
            if item.startswith("load_skill:"):
                skill_name = item.split(":", 1)[1]
                positions.append(next((i for i, event in enumerate(events)
                                       if event.get("type") == "tool_call"
                                       and event.get("name") == "load_skill"
                                       and _load_name(event) == skill_name), -1))
            else:
                positions.append(next((i for i, event in enumerate(events)
                                       if event.get("type") == "tool_call"
                                       and event.get("name") == item), -1))
        add("required tool order", all(i >= 0 for i in positions) and positions == sorted(positions), calls)
    if case.get("must_not_claim_without_tool"):
        has_evidence = any(e.get("type") == "tool_result" and e.get("name") == case.get("allowed_tool") for e in events)
        claims = [text for text in case["must_not_claim_without_tool"] if text in reply]
        add("no unsupported result claim", has_evidence or not claims, claims)
    if case.get("must_stop_after"):
        stop = case["must_stop_after"]
        pos = max((i for i, name in enumerate(calls) if name == stop), default=-1)
        forbidden = case.get("must_not_call_before_confirmation", [])
        add("stopped at confirmation", pos >= 0 and not any(name in forbidden for name in calls[pos + 1:]), calls)
    if case.get("must_not_call_before_skill"):
        action = case["must_not_call_before_skill"][0]
        loaded_at = next((i for i, e in enumerate(events) if e.get("type") == "tool_result"
                          and e.get("name") == "load_skill" and not str(e.get("summary", "")).startswith(("没有这个技能", "技能 "))), -1)
        action_before = any(i < loaded_at or loaded_at < 0 for i, e in enumerate(events)
                            if e.get("type") == "tool_call" and e.get("name") == action)
        add(f"{action} follows successful skill load", action in calls and not action_before, calls)
    if case.get("must_not_call_before_segmentation"):
        name = case["must_not_call_before_segmentation"][0]
        add(f"did not directly call {name}", name not in calls, calls)
    return checks


def run(*, budget_cny: float, case_ids: list[str] | None = None) -> dict[str, Any]:
    from app.chat import conductor

    config = _configured_chat()
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8-sig"))
    if case_ids:
        cases = [case for case in cases if case["id"] in case_ids]
    if not cases:
        raise ValueError("没有匹配的评测案例")

    results: list[dict[str, Any]] = []
    original_builder = conductor.build_chat_tools
    total_cost = 0.0
    with tempfile.TemporaryDirectory(prefix="wenju-agent-eval-") as temp:
        settings = Settings(DATABASE_URL=f"sqlite:///{(Path(temp) / 'eval.db').as_posix()}", CHECKPOINTER="memory")
        store = Store(settings.database_url)
        llm = LLM(settings, store)
        llm.config = config
        llm._model = build_chat_model(config)
        base_chat = llm.chat
        # A conservative per-call cap: 8 chat steps × 4096 output tokens max.
        llm.chat = lambda: base_chat().bind(max_tokens=4096)  # type: ignore[method-assign]

        def safe_builder(*args: Any, **kwargs: Any):
            tools = original_builder(*args, **kwargs)
            safe = []
            for tool in tools:
                if tool.name in BLOCKED_TOOLS:
                    continue
                if tool.name not in SAFE_TOOLS:
                    tool.func = lambda **_kwargs: "评测安全桩：记录了工具选择，本次未执行实际生成或写入。"
                safe.append(tool)
            return safe

        conductor.build_chat_tools = safe_builder
        try:
            for case in cases:
                text = SAMPLE_TEXT
                if case.get("long_novel"):
                    text = ("第一章 雨夜。林然推开旧楼铁门，听到楼上传来三下敲击。\n" * 1800)
                project = store.create_project(
                    title=f"Agent评测-{case['id']}", adaptation_type="short_drama",
                    language="zh-CN", raw_text=text,
                )
                conversation = store.ensure_default_conversation(project.id)
                started = time.monotonic()
                outcome: dict[str, Any] = {"id": case["id"]}
                try:
                    response = conductor.chat_once(
                        store, llm, settings, conversation_id=conversation.id,
                        project_id=project.id, message=case["input"],
                    )
                    events = response.get("events") or []
                    reply = response.get("reply") or ""
                    checks = _check(case, events, reply)
                    usage = _usage_cost(events)
                    total_cost += float(usage["estimated_cny_buffered"])
                    outcome.update({
                        "passed": bool(checks) and all(item["passed"] for item in checks),
                        "checks": checks,
                        "tool_calls": [_tool_name(e) for e in events if e.get("type") == "tool_call"],
                        "loaded_skills": [_load_name(e) for e in events
                                          if e.get("type") == "tool_call" and e.get("name") == "load_skill"],
                        "reply": reply[:1000], "events": events,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "usage": usage,
                    })
                except Exception as exc:  # noqa: BLE001
                    outcome.update({"passed": False, "failure_type": type(exc).__name__,
                                    "failure": str(exc)[:500],
                                    "latency_ms": round((time.monotonic() - started) * 1000)})
                results.append(outcome)
                print(f"{case['id']}: {'PASS' if outcome['passed'] else 'FAIL'}; "
                      f"累计预算估算 ¥{total_cost:.4f}/¥{budget_cny:.2f}", flush=True)
                if total_cost >= budget_cny:
                    break
        finally:
            conductor.build_chat_tools = original_builder
            store.engine.dispose()

    report = {
        "model": f"{config.provider}/{config.model}",
        "pricing_basis": "DeepSeek Flash peak cache-miss rates; USD/CNY multiplied by 8.0 for a buffer",
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "budget_cny": budget_cny,
        "estimated_cost_cny_buffered": round(total_cost, 4),
        "case_count": len(results),
        "passed": sum(bool(r.get("passed")) for r in results),
        "cases": results,
        "media_generation": "disabled; video/image/audio tools omitted or stubbed",
    }
    REPORT_DIR.mkdir(exist_ok=True)
    target = REPORT_DIR / "agent-online-2026-11-23.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(target)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run budget-bounded chat Agent evaluations")
    parser.add_argument("--budget-cny", type=float, required=True)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    if not 0 < args.budget_cny <= 20:
        raise SystemExit("预算必须大于 0 且不超过本轮已确认的 ¥20")
    print(json.dumps(run(budget_cny=args.budget_cny, case_ids=args.case or None),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
