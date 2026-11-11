# =====================================================================
# agent —— 改编智能体（LangGraph 状态图）
#
#   state.py   AgentState 定义（可序列化，依赖通过闭包注入）
#   graph.py   图拓扑：context → plan(ReAct+tools) → propose → guard(自纠错)
#              → review(interrupt 人机协同) → apply → finalize
#   nodes.py   各节点实现（提示词、结构化输出、校验、落版）
#   tools.py   ReAct 工具集（剧本查询 / 原文检索 / 作者风格 / 版本）
#   runner.py  薄服务层：对接 HTTP、checkpointer 与运行记录
#   tasks.py   后台任务运行器（剧组工具的任务管理与进度上报）
#
# 为避免循环导入，本 __init__ 不做任何子模块导入；
# 使用方请显式 `from app.agent.graph import ...`。
# =====================================================================
