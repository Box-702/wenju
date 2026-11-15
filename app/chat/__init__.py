# =====================================================================
# chat —— 对话式编排层（Codex / DSH 风格的工作形式）
#
# conductor.py 把「表单式工作台」升级为「对话式 Agent」：
#   - 内核：app/chat/loop.py 的显式工具循环（LLM → 工具 → LLM，不再用 LangGraph 图）；
#   - 工具实现：按域拆在 ./tools/（screenplay / crew / video / memory_tools）；
#   - 底层：复用 app/agent 的改编工作流（run_adaptation / resume）；
#   - 后台任务：剧组工具在 app/agent/tasks.py 的运行器里执行，进度写回任务面板。
#
# 服务入口：chat_once() / chat_stream()（SSE）/ _handle_resume()（确定性路径）。
# =====================================================================

from .conductor import (
    build_chat_tools,
    chat_once,
    load_history,
    main_skill_names,
    system_prompt,
    _handle_resume,
)

__all__ = [
    "build_chat_tools",
    "chat_once",
    "load_history",
    "main_skill_names",
    "system_prompt",
    "_handle_resume",
]
