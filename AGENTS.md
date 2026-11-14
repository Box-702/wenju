# AGENTS.md —— 给在此仓库工作的 AI 代理/协作者

问剧（Wenju）：小说 → 短剧全链路 Agent 工作台。动手前先读这里，细节看 `docs/README.md` 索引。

## 硬性工作流（与提交绑定，不是提交之后的事）

1. **踩坑必须当场记** → `docs/DEVELOPMENT.md` 第五节，格式「症状 → 根因 → 解法 → 涉及文件」。
   修完一个非平凡 bug 就写，不留过夜；只写代码提交不写文档 = 违规。
2. **选型变更当场记** → `docs/DECISIONS.md` 追加 D-XX（背景/选项/决定/理由/什么时候该推翻），
   旧结论只标「已被 D-XX 取代」，不改写。
3. **数字要实测** → README / ARCHITECTURE 里的工具数、测试数（`pytest tests -q` 实际输出）、
   容量上限，改了代码顺手改文档。
4. **新模块要登记** → `docs/ARCHITECTURE.md` 第一节的包结构图。
5. **测试轮要记账** → `docs/DEVELOPMENT.md` 第七节「测试账本」：每轮花钱验证或专项排障，
   当场记「假设 → 过程 → 发现的问题 → 修改 → 效果 → 遗留」，花钱轮记实际费用 vs 预估。
   第五节记单个 bug 的解剖，第七节记一次实验的闭环——修完没记账 = 半次提交。
6. 做不到 1-5 时，当轮结尾显式列「待补文档清单」并立刻补。

## 动手前

- 改代码前看 `ARCHITECTURE.md` 分层规则（依赖方向不能反）。
- 提新方案前查 `DECISIONS.md`（别推翻已有决策、别重复调研）。
- 改任何提示词/SKILL.md 后跑 `python -m evals.run`（判定标准变了改 `evals/rubric.md`）。
- 报错先查 `DEVELOPMENT.md` 第六节排查手册。

## 运行与结构速查

- 启动：`uv run uvicorn app.main:app --port 8000 --reload` + `frontend/ npm run dev`（5173）。
- 测试：`uv run pytest tests -q`。
- 用户数据在 `~/.wenju/`（app.db + workspace/ + runs/），**不在项目 data/**（D-44）；
  项目文件夹保持零用户数据，别把产物写回 `data/`。
- 小说→成片直跑体检：`uv run python N2D/novel2video.py <小说.txt> --shot-limit N`，
  挂了用 `--resume <作品id>` 续跑（已出片/在途镜头不会重复烧钱）。
- 视频供应商：MiniMax H3 系列三档（默认 Lite，D-45）；计费按 档位×分辨率，
  参考视频按 input_seconds 与输出同价——动出片链路前读 `DEVELOPMENT.md` 5.37。

## 花钱纪律

MiniMax 按秒计费且接口非幂等（超时也计费）。任何会触发视频/图片生成的
调试，先估算并告知用户单价；批量提交必须走 `--shot-limit` 或分批。

## 边界

只做「小说 → 短剧」。不做电影/舞台剧/多厂商视频 UI（D-08/D-19）；不做 3D 预览视口——白模只走「施工单对话调整 → 导出视频」（D-54）；
前端只展示提示词与成片，不暴露工程内部；文案禁工程黑话。
