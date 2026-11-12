# 问剧 2.0 改造 Plan：通用对话 Agent + Skill 系统

> 2026-11-02 · 决策背景：竞品调研（docs/POSITIONING-2026-11.md）后的方向收敛——从「垂直工作流工具」转向「以短剧能力为内核的通用对话 Agent」

---

## 进度（2026-11-05 更新）

| 里程碑 | 项 | 状态 |
|---|---|---|
| **M1** | 1. 自研 AgentLoop + provider 层 | ✅ **已完成**（`app/chat/loop.py`，见 D-29）。transport 仍用 ChatOpenAI，`_to_lc_messages()` 是唯一切换点 |
| | 2. Session 落库与恢复（含图片引用） | ✅ 图片落库 + 常驻上下文已完成（D-26）；工具消息尚未单独落角色 |
| | 3. novel-to-drama 技能 → 交用户审阅 | ⚠️ 技能已存在但拆成了 4 个（`novel-serial` / `crew-pipeline` / `script-review` / `production-delivery`），**命名与 plan 不同**，待确认是否合并成一个 |
| **M2** | 4. SKILL.md 解析 / `/` 面板 / 导入 | ⚠️ 解析 + `load_skill` + 自动选择已有；**`/` 呼出面板与用户导入尚未做** |
| | 5. 其余内置技能 | ✅ 四个领域技能已落地（命名与 plan 不同，见上） |
| | 6. 资产工具化 + CastLibrary 移除 | ⚠️ 资产上传/入库已完成（D-23）；`search_assets` / `save_asset` / `attach_asset` 三个工具**尚未建** |
| **M3** | 7. 沟通协议写入提示 | ❌ 未开始 |
| | 8. `generate_video` 工具化（强制参数校验） | ⚠️ `submit_videos` 已有，但没做「风格/比例/时长」的强制确认 |
| **M4** | 9. 新布局 + 灰黑主题 | ⚠️ 工作区化 + 品牌首页已完成（`ca8329b` / `a9b92e5`），与 plan 的 iMini 布局**不完全一致**，待确认 |
| | 10. 全链路回归 | ⚠️ 待 M1-M3 收口后再做 |

**下一步建议**（2026-11-12 起以第十节 P0-P5 为准）：本节原建议的 M2/M3 遗留项
排到 P5；工作流入口、预算闸门、白模 previz 三个新概念见[第十节](#十2026-11-12-修订工作流入口--预算闸门--白模-previz)。

---

## 一、定位（一句话）

**一个类 iMini 的通用对话创作 Agent**：对话是唯一交互入口，内置短剧领域技能（小说转剧本、分镜、视频生成），资产库由 Agent 内部管理，视频生成是 Agent 的一个工具，用户可自定义 Skill。

## 二、范围：做什么 / 不做什么

### 做
| 能力 | 说明 |
|---|---|
| 通用对话 Agent | 任何创作类请求都能聊；短剧链路通过内置 Skill 提供 |
| Skill 系统 | 内置技能（小说转剧本/分镜/视频生成）+ 用户自定义导入，`/` 呼出 |
| 资产库内部化 | 角色/场景/白模/素材不再有 UI 操作入口，全部变成 Agent 工具 |
| 图片能力 | 对话中读图、识图、存图、引用历史图（已有雏形，按 Pi 模式增强） |
| 上下文持久化 | 中断、重启服务后对话无缝继续（已有机制，补强 session 层） |
| 分段式视频生成 | Agent 主动建议按集/场/段生成，每段与用户确认后再执行 |
| 决策沟通协议 | 生成视频前必与用户确认：风格、画面比例、时长 |
| 视频生成工具化 | 内置 MiniMax 降级为 Agent 的一个工具（generate_video） |
| UI 全面重构 | 布局完全模仿 imini.com/agent，配色默认灰黑基调 |

### 不做（iMini 有但我们没有的，先不做）
- 云端积分/订阅计费体系（我们是 BYOK 本地工具）
- Design 灵感社区、Cloud Computer 云电脑
- 模板广场、多人协作
- iMini 的模型聚合调度（我们保持用户自配模型）

## 三、架构改造

### 3.1 Agent 内核：脱离 LangGraph（参考 Pi 的轮子）

现状：conductor 用 LangGraph StateGraph + ToolNode 跑 ReAct 循环，checkpointer 已是 memory。问题：图/状态通道行为不直观（踩过 has_images 丢键的坑），依赖重、调试黑盒。

**新内核（自研轻量 loop，参考 `@earendil-works/pi-agent-core` 的设计）：**

```
AgentLoop.run(messages, tools):
  while steps < MAX_STEPS:
    resp = llm.chat_completions(messages, tools, stream=True)   # OpenAI 兼容协议
    yield 流式增量（content / tool_call 增量）
    if resp 没有 tool_calls: break
    for call in resp.tool_calls:
        result = execute(call)          # 工具注册表分发
        messages.append(tool 结果消息)
  return final_text, events
```

- **消息格式**：直接用 OpenAI 兼容的 dict 消息（role/content/tool_calls/tool_call_id），不再用 LangChain 消息类型——我们所有 provider（DeepSeek/智谱/OpenAI/Qwen）都是 OpenAI 兼容端点，参考 Pi `pi-ai` 的统一流式封装自写 ~200 行 provider 层
- **工具协议**：JSON Schema 声明 + 注册表分发（替代 bind_tools/ToolNode）
- **checkpointer 彻底移除**：单轮内状态就是 messages 列表；跨轮上下文继续走现有的「每轮从 SQLite 重建」（这套机制本来就比 checkpointer 可控）
- LangChain 依赖先保留给 crew/production 管线（作为被工具调用的执行器），内核不再依赖它

### 3.2 从 Pi 借鉴的四个机制

| Pi 的轮子 | 我们的落地 |
|---|---|
| read 工具直读图片（图片→ImageContent 进上下文） | `read_image` 工具：Agent 可主动读取对话图片/资产图，识图结果进上下文；纯文本模型时自动拦截并换视觉模型（我们已有 chat_multimodal，合并进来） |
| session 持久化与恢复 | Session 层：messages 全量落 SQLite（现有 chat_messages 扩展 role=tool 记录），服务重启/页面刷新后 Agent 从库重建完整上下文继续工作 |
| SKILL.md 技能协议 | 技能 = Markdown 文件（frontmatter: name/description/when_to_use + 正文指令），按需注入系统提示（我们已有渐进披露雏形，对齐 Pi 协议格式） |
| 按需加载不常驻 | 技能正文不进系统提示，Agent 调 `load_skill` 拉取——目录只占几行（已有，保留） |

### 3.3 工具清单（Agent 可用）

**资产类（资产库内部化的接口）**
- `search_assets(kind, query)` — 找角色/场景/素材
- `save_asset(kind, name, image_ids|file, notes)` — 存图/建角色/建场景（含用户上传的图）
- `attach_asset(asset_id, shot_ids)` — 把资产关联到镜头
- `list_project_assets()` — 项目资产概况

**创作类（复用现有管线，包装为工具）**
- `novel_to_script(...)` — 调现有 crew 管线
- `generate_storyboard(...)` — 分镜
- `generate_image(prompt, ref_asset_ids)` — 定妆照/场景图（CogView）
- `generate_video(shot_id, style, ratio, duration)` — **视频生成工具**，参数强制走沟通协议（见 §五）

**会话类**
- `read_image(image_id)`、`load_skill(name)`、`remember(...)`（已有）

## 四、Skill 系统

### 4.1 格式（Pi SKILL.md 兼容）

```markdown
---
name: novel-to-drama
description: 把小说改编成短剧剧本（分集+分镜+角色表）
when_to_use: 用户上传小说/要求把文字作品转成短剧时
---

## 工作流程
1. 先读小说，向用户确认：目标时长/集数、题材基调、目标平台比例
2. 产出分集大纲 → 逐集与用户确认
3. ...
## 输出规范
...
```

### 4.2 内置技能（首发 4 个）

1. **novel-to-drama**：小说→剧本（我们的核心经验封装：拆文→分集→人物小传→分镜表）
2. **segmented-video**：分段式视频生成策略（强制沟通节奏：风格→比例→时长→分段确认→逐段生成→合成）
3. **asset-director**：资产管理规范（何时建角色/场景、一致性约束怎么写进提示词）
4. **general-creation**：通用兜底（非短剧请求的通用创作助手行为）

### 4.3 用户自定义 Skill

- **导入**：Skills 页上传 `.md`（单个）或 `.zip`（含附属文件），解析 frontmatter 存入 SQLite（遵守全存 DB 决策）
- **选择**：输入框 `/` 呼出技能面板手动指定；Agent 依据 description 自动匹配
- **管理**：启用/停用/删除；内置技能可停用不可删

> ⚠️ 按用户要求：**novel-to-drama 技能封装初稿完成后，先给用户过目确认，再继续后续里程碑。**

## 五、沟通协议（Agent 行为规范，写入系统提示）

1. **分段式生成**：涉及视频产出时，Agent 必须建议按「集 → 场 → 镜头段」推进，禁止一次性生成整部；每段生成前展示该段计划（镜头数、预计时长）
2. **生成前必问**（缺一项就先问再干）：
   - 视觉风格（写实/国漫/韩漫/自定义参考图）
   - 画面比例（9:16 竖屏 / 16:9 横屏 / 其他）
   - 单段时长（建议 15s 起步）
3. **决策点确认**：剧本定稿前、资产形象确认前、批量生成前——各设一次明确确认（用现有审阅卡片机制承载）
4. 用户明确说「直接生成」时跳过追问但记录偏好到记忆

## 六、UI 重构（模仿 imini.com/agent，灰黑基调）

### 布局映射

```
┌────────────┬──────────────────────────────────┐
│ 左侧栏      │  中央对话流                        │
│            │  ┌────────────────────────────┐  │
│ ○ 新任务    │  │ user 消息（含图片缩略图）      │  │
│ ▦ 技能      │  │ agent 消息                   │  │
│            │  │  [模型徽标 · 处理 Xs] [复制][重试]│
│ ────────   │  │  审阅卡片/确认卡片（内嵌）      │  │
│ 任务历史    │  └────────────────────────────┘  │
│  ├ 任务A    │  ┌────────────────────────────┐  │
│  ├ 任务B    │  │ 输入区：📎 │ /技能 │ 模型选择 ▾ │→│
│            │  └────────────────────────────┘  │
└────────────┴──────────────────────────────────┘
```

- **左侧栏**：新任务（= 新对话/可绑定项目）、技能入口、任务历史列表（现有对话树收编于此）
- **消息流**：每条 agent 消息头部带「模型名 · 处理耗时」徽标 + 复制/重试；工具执行过程以可折叠时间线展示（现有 events 流增强）
- **输入区**：图片上传按钮（已有）、`/` 技能呼出面板、模型选择下拉（已有数据源）、发送
- **移除的 UI**：CastLibrary 直接操作页、命令面板、HeaderBar 上与对话无关的入口——资产操作全部通过对话完成
- **配色**：iMini 的布局结构 + 灰黑基调（深灰面板 #1a1c1f 层级、黑色主背景、灰色描边，金色仅保留为强调色）

## 七、里程碑

### M1 · Agent 内核 + 首个 Skill（先给用户看）
1. 自研 AgentLoop + OpenAI 兼容 provider 层（流式 + tools），替换 conductor 的 LangGraph 图
2. Session 落库与恢复（含工具消息、图片引用）
3. **封装 novel-to-drama 技能 → 交用户审阅** ✋ 停点
### M2 · Skill 系统 + 资产工具化
4. SKILL.md 解析、`/` 面板、导入存储、自动选择
5. segmented-video / asset-director / general-creation 技能
6. 资产工具（search/save/attach），CastLibrary UI 移除
### M3 · 沟通协议 + 视频工具
7. 系统提示写入沟通协议；确认卡片机制
8. generate_video 工具化（强制参数校验：风格/比例/时长）
### M4 · UI 全面重构
9. 新布局（左栏/消息流/输入区）+ 灰黑主题
10. 全链路回归：导入小说 → 确认 → 分集 → 分镜 → 分段生成 → 成片

### 验收标准
- 重启服务后对话上下文无损继续
- 纯对话完成一次完整短剧产出，全程 Agent 主动分段+确认
- 用户导入一个自定义 skill 并被 Agent 正确选用
- LangGraph 从 agent 链路依赖中移除（crew 内部暂保留）

## 八、风险与待确认

1. **pi-agent 是 TypeScript 生态**，我们后端是 Python——plan 按「借鉴设计、Python 自研」走；若你本意是直接嵌入 pi-agent-core（Node 侧跑 agent），需要提前说，架构会不同
2. 通用 Agent + 短剧 Skill 的产品边界：非短剧请求做到「能用」水平即可，不追 iMini 的全能
3. 工具消息落库会放大存储，注意 chat_messages 表的体积控制（工具结果截断存储）

## 九、相关计划

- [`PLAN-mediago-reference.md`](PLAN-mediago-reference.md) —— 对标 mediago-drama 的待办清单：
  生成提交幂等（会重复扣钱的洞）、防绕过提示词句式、Agent 可读项目文档。
  见 [D-36](DECISIONS.md#d-36-借鉴-mediago-drama-只移植具体手法不换-agent-内核)。

---

## 十、2026-11-12 修订：工作流入口 + 预算闸门 + 白模 previz

> 触发：孔乙己账单日（50 元一天烧穿，见 DEVELOPMENT.md 5.37）之后，与用户
> 对齐了主 Agent 的下一层形态。本节**不推翻**第一~九节，是在「通用对话
> Agent + Skill」骨架上补的三个概念，状态以本节为准。

### 10.1 工作流入口（新形态，介于「工具」与「技能」之间）

用户提出的模式：**主聊天 = 通用视频生成对话 Agent；每条重链路 = 一个工作流入口**。
落到我们的架构：

```
主 Agent（一个内核，D-28）
 ├─ 工具（单步动作）：read_script / get_status / remember …
 ├─ 技能（领域知识，SKILL.md 按需加载）：crew-pipeline / production-delivery …
 └─ 工作流（多阶段长任务，有预算、有断点、有产物）：
     ├─ N2D 小说→成片（已存在：对话里 start_production 走的就是它；
     │   N2D/novel2video.py 是它的无人值守体检外壳）
     ├─ 白模 previz（规划中，见 10.3）
     ├─ 镜头修复（规划中：质检 FAIL 批量降级重 roll，今天已做成 reroll 逻辑）
     └─ 连载推进（map_novel/propose_arcs 已有，串成一键）
```

设计约束（都是账单日的教训）：
- **入口在对话里**（一句话触发 + 确认卡），不做 UI 按钮——北极星不变（[[agent-first-vision]]）。
- **每个工作流自带预算闸门**：开跑前报「N 镜 × ¥x = 总额」，超阈值必须用户确认；
  分批默认（--shot-limit 语义），失败可 `--resume` 语义续跑不重复烧钱。
- **工作流之间共享三件基础设施**：资产库、版本链、批准硬闸门（D-43）。
- 入口膨胀的防线已有：系统提示只放「触发→动作」路由表，细则在 SKILL.md
  按需加载（实测 3/3 命中，见 skill-mechanism）。

### 10.2 视频供应商与档位策略（D-45 的延伸）

- 三档可切已落地（Lite 默认/Highspeed/H3），Seedance（火山方舟）适配器已写好
  未校准——**A/B 流程**：方舟 key → probe_seedance.py 校准字段 → 同分镜 5 镜双跑
  → 比一次通过率/单镜成本/跨镜一致性 → 数据决定默认档与是否双供应商。
- 不做的事：多供应商 UI 暴露（D-08 不变，A/B 是工程流程不是产品功能）。

### 10.3 Blender 白模 previz（新增工作流，方向已认可）

定位：**N2D 里插入的可选阶段**，不是独立产品——

```
拆镜 → 【白模 previz（可选）】→ 提示词 → 批准 → 出片
```

- 目的：把**方向性错误**（运镜/构图/镜头取舍）拦在免费阶段，把**质感错误**
  留给付费重 roll——直接回应「每镜至少打回一次」的成本痛点。
- 实现路径：文字/分镜 → LLM 生成 Blender Python（场景搭建+相机动画）→
  EEVEE 快渲 5s/镜动态分镜 → 用户在批准卡上改运镜砍镜头 → 才进 DP 提示词。
- 环境已就绪（2026-11-12）：Blender 5.2.2 便携版 + 官方 mcp 扩展 + blmcp 桥
  全链路接通（9876 端口）。
- 安全约束：Blender MCP = 任意 Python 执行，**必须走 D-43 批准闸门**，
  Agent 只发起、用户确认才执行；长渲染走后台任务+完成通知，不占对话线程。
- 依赖降级：Blender 没开时入口如实说「白模预览不可用，直接进文字审阅」。
- 排期：**先 POC**（文字→单场景白模→EEVEE 渲 5 秒），POC 成立再进对话工作流。

### 10.4 里程碑重排（覆盖第七节的「下一步建议」）

| 序 | 项 | 依赖 |
|---|---|---|
| P0 | v3 全片重跑验证今天全部修复（横屏/镜头语言/Lite/裁尾/静片） | 已完成改造，待开跑 |
| P1 | DIR-01 细节律存量缺陷专项（evals critical=4，归因实验确认非 11-12 引入） | 无 |
| P2 | 白模 previz POC（单场景 5 秒） | Blender 已就绪 |
| P3 | Seedance 探针校准 + 5 镜 A/B | 火山方舟 key（用户提供） |
| P4 | 工作流入口对话化收口（start_production 报预算 + 确认卡 + 续跑语义） | P0 数据 |
| P5 | M3 沟通协议 / 资产工具补全（原 plan 遗留） | 排后 |
