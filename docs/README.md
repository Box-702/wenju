# 文档索引 —— 问剧（Wenju）

> 四份文档各管一件事，**别从错的一份开始读**。

| 文档 | 回答什么 | 什么时候看 |
|---|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 包结构怎么分层、模型厂商怎么适配、多 Agent 各是谁、从小说到成片的完整链路 | 改代码前先看分层规则；想知道某个模块归谁管 |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | **踩过的坑**：每条都是「症状 → 根因 → 解法 → 涉及文件」；还有扩展指南与排查手册 | 遇到报错、行为不对、或者要加 Provider / Agent / 路由 |
| [`DECISIONS.md`](DECISIONS.md) | **技术选型的为什么**：每个决策的备选方案、决定、理由、以及「什么时候该推翻」 | 提新方案前先看——避免推翻已有决策，也避免重复调研 |
| [`DRAMATURGY.md`](DRAMATURGY.md) | 「先戏剧后语法」方法论与确定性审计规则 | 改提示词质量、审计规则、镜头卡字段时 |
| [`PLAN-shot-group.md`](PLAN-shot-group.md) | **镜头组架构 + Blender 白模约束层**：一次生成=一段戏（2-4 机位）、previz 反哺提示词、S1-S4 分期与回退策略 | 动分镜数据结构/批次粒度/配音时间轴之前必读 |
| [`PLAN-mediago-reference.md`](PLAN-mediago-reference.md) | **对标计划**：对照 mediago-drama 的强弱表与待办清单（生成幂等 / 防绕过提示词 / Agent 可读文档） | 排下一批要做什么、查「这个功能当初为什么不做」 |
| [`../evals/README.md`](../evals/README.md) | **提示词质量 eval**：怎么跑、怎么加 case、判定标准 | **改任何提示词之后**——这是唯一能提前发现「质量退化」的手段 |

## 推荐阅读顺序

**第一次接触项目**
1. 根 [`README.md`](../README.md) —— 是什么、怎么跑
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) —— 分层与链路
3. [`DECISIONS.md`](DECISIONS.md) —— 为什么是这样

**准备动手改**
1. [`DEVELOPMENT.md`](DEVELOPMENT.md) 第五节「踩坑记录」—— **先看你即将碰的那一层踩过什么**
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) 第一节的分层规则 —— 依赖方向不能反
3. [`DECISIONS.md`](DECISIONS.md) —— 你要动的东西是否已有选型决策
4. 要改提示词的话，再看 [`../evals/README.md`](../evals/README.md)

**排查问题**
1. [`DEVELOPMENT.md`](DEVELOPMENT.md) 第六节「排查手册」—— 症状 → 先查什么
2. 再翻第五节对应小节

**改了提示词之后**
1. `python -m evals.run` —— 确认确定性层没有 critical
2. `python -m evals.run --judge` —— 对比 judge 均分有没有掉

**想找下一批要做什么**
1. [`PLAN-agent-rebuild.md`](PLAN-agent-rebuild.md) —— 2.0 改造主计划（产品形态层面）
2. [`PLAN-mediago-reference.md`](PLAN-mediago-reference.md) —— 对标计划（具体待办层面）

## 文档维护约定

- **踩坑必须立刻记**：调试时解决了非平凡的问题，就往 `DEVELOPMENT.md` 第五节加一条，
  写清「症状 → 根因 → 解法 → 文件」。不必等一个版本结束——**隔一周就忘了根因**。
- **选型变更必须记**：换技术栈、换协议、加框架，在 `DECISIONS.md` **追加**一条新决策（D-XX），
  不要改写旧条目的结论——「当初为什么那么选」比「现在怎么选」更有价值。
  如果确实推翻了旧决策，在旧条目里标注「已被 D-XX 取代」并指向新条目。
- **数字要实测**：文档里的数量（工具数、测试数、容量上限）以代码为准，改了代码顺手改文档。
  测试数用 `pytest tests -q` 的实际输出。
- **踩坑条目要给文件路径**：写「解法」时同时写清楚改哪个文件，否则半年后没人知道从哪查起。

## 文档与代码的关系

文档是**给人看的**，代码是**给机器跑的**，两者都要活。规则很简单：

> 改了架构 → 改 `ARCHITECTURE.md`
> 踩了坑 → 改 `DEVELOPMENT.md`
> 换了选型 → 改 `DECISIONS.md`
> 改了提示词方法论 → 改 `DRAMATURGY.md`
> 改了提示词 → **跑 `evals/`**，然后改 `evals/rubric.md`（如果判定标准变了）
> 改了产品形态 → 改根 `README.md` 与 `PRODUCT.md`
