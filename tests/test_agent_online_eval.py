import json
from pathlib import Path

from evals.agent_online import (
    BLOCKED_TOOLS,
    INPUT_USD_PER_M,
    OUTPUT_USD_PER_M,
    _check,
    _usage_cost,
)


def test_fixed_agent_eval_manifest_has_five_coverage_areas():
    cases = json.loads(Path("evals/agent_cases.json").read_text(encoding="utf-8-sig"))
    assert len(cases) == 5
    assert {case["id"] for case in cases} == {
        "agent_tool_selection_read_status",
        "agent_skill_before_generation",
        "agent_confirmation_stops_execution",
        "agent_refuses_invented_status",
        "agent_long_novel_segments",
    }


def test_online_harness_blocks_media_generation():
    assert {"submit_videos", "generate_video", "generate_reference_assets"} <= BLOCKED_TOOLS


def test_cost_estimate_uses_peak_cache_miss_rates_and_buffered_fx():
    result = _usage_cost([
        {"type": "model_usage", "input_tokens": 1_000_000, "output_tokens": 1_000_000}
    ])
    assert result["estimated_usd_peak"] == INPUT_USD_PER_M + OUTPUT_USD_PER_M
    assert result["estimated_cny_buffered"] == round((INPUT_USD_PER_M + OUTPUT_USD_PER_M) * 8, 4)


def test_agent_case_checker_accepts_load_before_long_novel_map():
    events = [
        {"type": "tool_call", "name": "load_skill", "args": '{"name":"novel-serial"}'},
        {"type": "tool_result", "name": "load_skill", "summary": "正文"},
        {"type": "tool_call", "name": "map_novel", "args": "{}"},
    ]
    case = {"required_skill": "novel-serial", "must_precede": ["load_skill:novel-serial", "map_novel"],
            "must_not_call_before_segmentation": ["generate_script"]}
    assert all(check["passed"] for check in _check(case, events, "地图已启动"))
