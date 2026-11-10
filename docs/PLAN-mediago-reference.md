# 借鉴计划：对照 mediago-drama（漫剧 Agent 工作台）

> 2026-11-09 · 调研对象：`github.com/mediago-dev/mediago-drama`（Apache-2.0，466 commits）。
> 本地只读克隆：`C:\Users\19387\Desktop\源码\mediago-drama`（**仅供对照阅读，不引它的依赖、不并入它的代码**）。
> 边界决策见 [D-36](DECISIONS.md#d-36-借鉴-mediago-drama-只移植具体手法不换-agent-内核)。

---

## 一、为什么做这次对照

问剧 2.0 在往「对话为主的作品流」收敛（D-24 / D-29 / PLAN-agent-rebuild），
需要确认两件事：

1. **我们比同类强在哪** —— 避免为了追新而把已有的优势改掉；
2. **我们缺什么** —— 特别是那些「用户看不见但会咬人」的坑（重复扣费、上下文丢失、提示词被绕过）。

mediago-drama 是目前能读到的、最接近「漫剧 Agent 工作台」形态的开源实现：
React + Electron 前端 + Go 后端，文档驱动，Agent 走 Codex app-server。

---

## 二、对照结论（一句话）

> **问剧的提示词工程与确定性审计整体强于 mediago-drama；mediago-drama 在「生成授权闸门」
> 与「防绕过提示词句式」两处强于我们。合并之后才完整。**

### 强弱对照表

| 维度 | mediago-drama | 问剧 | 结论 |
|---|---|---|---|
| Agent 内核 | 外部 Codex 进程（stdio JSON-RPC） | 自研 while 循环（`app/chat/loop.py`） | **我们更可控**，不换 |
| 上下文供给 | cwd = 文档根目录，零检索 | 消息表 + 滚动摘要 + 工具轨迹回灌 | **我们更完整**，不换 |
| 提示词工程手段 | ~8 类 | **13 类** | **我们更强** |
| 确定性规则审计 | **无**（全靠提示词自律） | `app/crew/dramaturgy.py` + `evals/assertions.py` | **我们独有优势** |
| 提示词质量量化 | 无 | `evals/` 四层 + 消融实验（82% vs 58%） | **我们独有优势** |
| 生成授权闸门 | 指纹 CAS 一次性 claim | ❌ 无幂等，同镜头可重复提交 | **他们更强，必须补** |
| 单会话并发互斥 | `hasActiveRuns()` → 409 | ❌ 无 | **他们更强，必须补** |
| 防绕过提示词句式 | 有（「用户说 X 也不能跳过 Y」） | ❌ 无 | **他们更强，必须补** |
| 资产库 | `assets` / `project_reference_assets` / `project_selected_assets` 三表 | `video_style_json` 实体 id 索引 + `media_assets` | 各有取舍，见 §四 |
| 对话历史引用 | ❌ 无（`AgentReference.kind` 只有 document/section/asset） | ❌ 无 | **两边都没有，可领先** |

### 他们的 Skill 清单（对照用）

9 个 `.skill.md`，共 3 篇核心（`AGENTS.md` / `TOOLS.md` / `DOCUMENT_RULES.md` 系统指令 + 9 个技能）：

| 类别 | 技能 |
|---|---|
| 创作类 | `screenplay-writer` / `character-writer` / `scene-writer` / `prop-writer` / `storyboard-writer` / `novel-writer` |
| 流程类 | `image-generation`（105 行）/ `video-generation`（152 行） |
| 引用类 | `auto-mention-resolver` |

MCP 工具 17 个：文档 9（`load_skill` / `get_project_config` / `update_project_config` /
`list_comments` / `get_comment` / `mutate_comment` / `ask_user_selection` / `ask_user_form` /
`await_user_selection`）+ 生成 2（`generate_media` / `generate_media_batch`）+ 跨项目 6。

---

## 三、任务清单

> 优先级：**P0 = 会烧钱/会丢状态**，**P1 = 能力缺口**，**P2 = 打磨**。
> 每条都给了落点文件与验收方式。改完提示词必须跑 `evals/`（D-13）。

### A 组 · 稳定性（P0）

#### A1. 生成提交幂等 —— 同镜头重复提交会重复扣费 ⚠️ 最高优先

**问题**：`app/api/video.py:704` 的 `_create_and_submit_job()` 无条件
`create_video_job()` + `_submit_video_job()`。同一个 `shot_id` 被调用两次就建两个 job、
扣两次费。而 `submit_videos`（`app/chat/tools/video.py:64`）是 Agent 可反复调用的工具，
模型重试、用户连点两下、批次重跑都会撞上。

**参照**：mediago-drama 的 `generation_confirmation.go` + ADR-0001「意图绑定的一次性授权」。

**做法**（三步，每步独立可上、可单独验收）：

| 步骤 | 内容 | 落点 |
|---|---|---|
| A1-a | 提交前查在途任务，命中则幂等返回既有 job，不新建 | `app/api/video.py` `_create_and_submit_job()` |
| A1-b | 新增 `generation_claims` 表：`intent_hash` / `status` / `task_ids` / `created_at`。同指纹重放返回原结果，异指纹拒绝，**进入 provider 调用后失败不释放 claim** | `app/store.py` + `app/api/video.py` |
| A1-c | Agent 路径强制带确认记录（复用 `approve_video_prompts` 闸门），无 claim 直接提交则拒绝 | `app/chat/tools/video.py` |

**验收**：重复调用 `submit_videos` 两次，`video_jobs` 表只多一条记录；
`tests/test_video_idempotency.py` 覆盖。

#### A2. 对话会话的 run 互斥

**问题**：`grep active_run app/chat/conductor.py` → 零结果。同一 conversation 并发两个请求，
会话消息交错、工具上下文串。

**参照**：`services/server/internal/service/agent/agent_sessions_service.go:278`
（`session.hasActiveRuns()` → 409）。

**做法**：`conversations` 表加 `active_run_id`；`SubmitAgentMessage` 时若已有在途 run
返回 409「该会话正在运行中」；run 结束 / 异常 / 客户端断连都要清 `active_run_id`
（`finally` + sweeper 双保险）。注意问剧现有 `agent_runs` 表是改编图用的，对话层没有 run 实体。

**验收**：`tests/test_chat.py` 加一条并发提交用例。

#### A3. ruff 致命集进 CI

**问题**：问剧自己记着这笔债（`docs/ARCHITECTURE.md` 5.1：「全量检查有 200 处 E501 未清，
一旦在 CI 里加 ruff 就会红」）。目前 CI 只跑 pytest / evals / 前端构建，**静态检查完全缺席**。

**做法**：不用一次还清存量，CI 只跑致命集：

```toml
[tool.ruff.lint]
select = ["E9", "F63", "F7", "F82", "B"]   # 语法错误、未定义名、bugbear
```

B 类（bugbear）能捞真 bug，E501（行长）先放过。存量 200 处 E501 不拦。
在 `.github/workflows/ci.yml` 的 `backend` job 里加一步。

**验收**：CI 能跑通；故意引入一个 F821 未定义名，CI 必须红。

#### A4. 文档数字漂移修正 —— ✅ 已完成（2026-11-09）

**原问题**：`README.md:299` 写「374 tests，30 个测试文件」，实测 **387 tests / 31 个测试文件**。
`docs/ARCHITECTURE.md` 写「20 个工具」，`README.md:58` 写「23 个」，实测 **23 个**
（`app/chat/tools/` 里 `@tool` 装饰器 23 处）。

**已做**：`README.md`（工具数、测试数、测试文件数）与 `docs/ARCHITECTURE.md`
（6 处工具数 + 历史合并说明）按实测值改正。**下次改代码顺手改，别再攒。**

> 2026-11-10 复核：拆掉「登记公网直链」的 `add_asset` 工具后，实测 **22 个工具 /
> 401 tests / 32 个测试文件**，相关数字已再改一遍（见 [D-37](DECISIONS.md#d-37-参考视频只从参考素材里选不再收公网直链)）。

---

### B 组 · 提示词手法（P0/P1，全部有现成对照文本）

> 他们比我们强的 6 个具体写法。每条都给「他的原文 → 我们的改法」。

#### B1. 抗绕过条款：「用户说 X 也不能跳过 Y」 —— P0

**他们的写法**（`TOOLS.md:62-63`、`image-generation.skill.md:12-13`）：

> Agent 创建图片或视频前必须经过 `generation_plan` 人工确认。**即使用户说"直接生成""不用问我"
> 或"按默认值生成"，也只能省略重复的自然语言追问，不能跳过确认表单直接提交。**

**我们的现状**：`app/skills/production-delivery/SKILL.md:50-55` 的误报清单只写了
「❌ 用户没批准就调 `submit_videos`（会被拒，且属于越权）」——这是**陈述后果**，
不是**封堵绕过路径**。模型看到用户说「别问了直接出片」时，没有规则告诉它必须拒绝。

**改法**（`app/skills/production-delivery/SKILL.md` 追加一节）：

```markdown
# 绕过闸门的话术（这些都不能跳过批准）

- 用户说「直接出片」「别问我了」「按默认来」「你先看着办」——这些只允许你省略
  重复的自然语言追问，**不能跳过 `approve_video_prompts`**。批准是用户的正式决定，
  任何情况下都只能由他发起。
- 用户说「先批了再改」「反正你会改」——不要批量批准，不要拿「先批了再改」当效率。
- 用户说「就生成这一个，不用确认」——单镜生成同样要走批准闸门。
```

**同样的句式加到** `app/skills/asset-generation/SKILL.md`（它已有「不跳过确认」，
但没说「用户说不用问也不行」）。

#### B2. 「不要编造具体 ID」的枚举清单 —— P0

**他们的写法**（`image-generation.skill.md:16`）：

> **不要编造** `routeId`、`model`、参数名、参数值、文档 ID、section ID、资产 ID 或任务 ID。

**我们的现状**（`app/chat/conductor.py:101-102`）只覆盖版本号：

> 严禁凭对话历史编造版本号（ver_xxx）、场次、进度或任务结果。

但我们有 `char_004` / `loc_001` / `beat_xxx` / `shot_scene001_001` / 音色 id / 素材 id
——**都可能被编造**。`dialogue_ref` 已经踩过一次（导演 SKILL 注释：「实测 35 个引用里 28 个是编的」）。

**改法**（`app/chat/conductor.py` 硬规矩段，紧接第 1 条）：

```python
"1b. 不要编造任何标识符：版本号（ver_xxx）、角色 id（char_xxx）、地点 id（loc_xxx）、"
"节拍 id（beat_xxx）、镜头 id（shot_xxx）、音色 id、素材 id、任务 id、参数名——"
"这些只能从工具返回值里取。宁可说「我需要先查一下」，也不要给一个看起来合理的 id。\n"
```

**验收**：纯增量，零行为破坏；加一条 evals 用例断言「不出现未在工具返回里出现过的 id」。

#### B3. 「不要用 A 方案替代 B 方案」 —— P1

**他们的写法**（`image-generation.skill.md:33`、`:47`）：

> 不要逐个参数追问，也不要用打包方案单选卡代替表单。
> 不要在表单前单独展示通用"风格选择"卡。

**我们的漏洞**：`app/skills/asset-generation/SKILL.md` 硬规矩 1 写了
「角色多的时候必须列清单让用户勾」——但没说「角色少的时候不要一个个问」。
模型完全可能对 3 个角色问 3 次。

**改法**（`app/skills/asset-generation/SKILL.md` 硬规矩 1）：

```markdown
1. **不替用户决定，也不零碎地问**：要生成多个资产时，一次列清单让用户勾选/取消，
   一次确认完。不要一个一个问「要生成谁？」「什么风格？」——反复追问既拖时间，
   又让用户失去对整体计划的掌控。要问就一次问完，用清单或表单。
```

同理适用于 `app/skills/script-review/SKILL.md`：patch 应**一次性列全部待决条目**
让用户逐条勾，不要问一句等一句。

#### B4. 「不因缺 X 而暂停」的免问清单 —— P1

**他们的写法**：三个创作类 Skill **各有一条**，句式一致
（`character-writer.skill.md:15` / `scene-writer.skill.md:15` / `prop-writer.skill.md:15`）：

> **不因缺少视觉风格而暂停、追问或要求用户先选择风格**；先完成角色文档。

**我们的现状**：`app/chat/conductor.py:105` 硬规矩 4 写「不确定时先问」，
但没写「什么时候不该问」。模型容易过度反问。

**我们的优势**：问剧完全有能力自己定（有 `adaptation_type` profile、
有 `projects.video_style.visual_style`），只是提示词没告诉它可以。

**改法**（`app/skills/style-guide/SKILL.md`）：

```markdown
- 画风未指定时不要停下来问：按项目既定画风走；项目也没定时按改编类型 profile 的
  默认画风走，并在 summary 里说明用了哪一套。用户要改会自己说。
- 同理，角色性别、年龄段等能从上下文推出的，不要问——推不出来就选一个合理的默认值
  并在 summary 里标注「按推断」。
```

**改法**（`app/skills/director-breakdown/SKILL.md`）：

```markdown
- 信息不足时优先做合理推断并推进，不要用「请补充更多信息」阻塞整个拆解。
  只有「影响剧情走向」的信息缺失才值得问；「影响镜头长什么样」的缺失自己定。
```

#### B5. 「禁止同上/沿用前镜」的省略禁令 —— P1

**他们的写法**（`storyboard-writer.skill.md:25`）：

> 以下基准对所有分镜默认生效，并且必须在每个镜头正文中**自包含写出，禁止写"同上""沿用前镜"**。

理由：**视频模型看的是这一条 prompt，不是上下文**。

**我们的情况有点特殊**：`app/skills/style-guide/SKILL.md` 的锚是「逐字复制进每一个镜头」
——**我们鼓励复用**，所以这条不能照抄。但**镜头内**的省略仍然要禁。

**改法**（`app/skills/video-prompt-zh/SKILL.md` 规则 1 之后加 1b）：

```markdown
1b. **每一镜的提示词必须自包含**：不得出现「同上」「沿用前镜」「参考上一镜」「如前所述」。
    需要复用的信息（造型锚、光线方向、环境特征、角色状态）必须在本镜内重新写出来——
    视频模型只看这一条 prompt，它没有上一镜。
    （注意：这与「风格指南的锚逐字复制」不矛盾。锚是**显式内容**的复制，
    「同上」是**指代**的省略。前者要重复，后者要展开。）
```

`video-prompt-en/SKILL.md` 同步加（英文版目前丢了中文版规则 8 的自查清单，
规则 1b 要两边都有，避免两个方言版行为分叉）。

#### B6. 「禁止为了凑满上限而拉长」的反向数值约束 —— P2

**他们的写法**（`storyboard-writer.skill.md:17-18`）：

> 只要加入下一个分镜后总时长不超过 15.00 秒，就放入同一组；……最后一组可以少于 15 秒，
> **禁止为了凑满 15 秒拉长节奏**。

**我们的现状**（`app/skills/director-breakdown/SKILL.md:58`）只给了上界：

> 单镜时长必须落在目标模型的单次生成区间内（**硬边界 4–15 秒**，超过会被校验打回；
> 日常创作建议控制在 4–10 秒

**只给了上界，没给「别贴着上界跑」的反向约束。**

**改法**（`app/skills/director-breakdown/SKILL.md` 时长那条之后）：

```markdown
- 不要把镜头一律写长：4 秒的镜头拉成 12 秒会把动作压成一团黏糊，也不要全写 4 秒——
  情绪停顿需要留白。按 pacing 档位表选，并且能在 director_notes 里说明为什么是这个时长。
- 也不要全写最短：每场里应该有一个明显长于其他的镜头，那是这一场的呼吸点。
```

#### B7. 为下游消费而写的元规则 —— P2

**他们的写法**（`novel-writer.skill.md:27`）：

> **可拆解**：关键人物、地点、道具、冲突和伏笔要具体，便于后续提取成角色设定、
> 场景设定、道具设定和分镜。

**改法**（`app/skills/director-breakdown/SKILL.md`）：

```markdown
- 每个镜头必须能被下游单独转成一条视频提示词：观众不看前一个镜头也能明白这一镜在讲什么。
  隐含前因后果的镜头（「他终于明白了」）必须把必要的视觉线索写出来。
```

#### B8. 内容安全边界 —— P2

**他们的写法**（`prop-writer.skill.md:26`）：

> 涉及武器、药品、危险装置等高风险道具时，只描述叙事和视觉设定，
> 不提供现实制作、伤害或规避监管细节。

**我们没有这一层。** 写实短剧涉及这类是常事，建议加到
`app/skills/director-breakdown/SKILL.md` 或 `app/skills/style-guide/SKILL.md`。

#### B9. 输出类型边界 —— P2

**他们的写法**（`novel-writer.skill.md:49`）：

> 不要写剧本格式、分镜字段或生图 Prompt，除非用户明确要求。

我们的 crew 三个角色之间有隐式边界（`app/skills/crew-pipeline/SKILL.md` 有依赖表），
但没写「不许越界产出」。建议在 `crew-pipeline` 的依赖表旁补一句边界声明。

---

### C 组 · 能力补齐（P1）

#### C1. Agent 可读项目文档 —— 最大的能力缺口

**问题**：问剧 Agent **读不到**剧本正文、角色外貌锚、分镜详情、风格指南。
`read_source` / `search_source` 只读小说原文（`projects.raw_text`）。
`get_status` 给的是摘要。

**后果**：用户说「把林墨那场戏改一下」，Agent 不知道林墨那场戏现在长什么样。

**他们的做法**：cwd = 文档根目录，模型用原生 read/write/edit。**这条我们不抄**——
我们没有 Codex 的原生文件工具，硬造一个文件层上下文层成本极高、收益低。

**我们的等价方案（两层都做）**：

| 层 | 做法 | 落点 |
|---|---|---|
| ① 轻量 | 每轮自动附「本项目可复用资源索引」（角色+外貌锚是否有图+音色、场景、镜头进度），按当前任务类型裁剪，上限 N 条 | `app/chat/conductor.py` `_prepare_turn` |
| ② 彻底 | 新增 `read_document` / `list_documents` 工具 | `app/chat/tools/` 新文件 |

**工具数红线**：现在是 23 个，Anthropic 的经验阈值是 30–50 开始出现「选不对工具」的退化。
加这 2 个到 25 仍在安全区，但**必须同时合并**：
`app/agent/tools.py` 里已有 `get_script_overview` / `get_scene_detail` / `get_source_text` /
`search_source` / `get_author_style` / `list_versions` 6 个，考虑并进 `read_document`。

#### C2. 结构化引用历史对话 —— 我们可以领先

**现状**：他们也没有。`AgentReference.kind` 只有 `document | section | asset`，
`mention://` scheme 只支持文档/章节/素材，请求契约里没有任何指向历史 messageId 的字段。

**我们的机会**：问剧的长对话靠滚动摘要（`app/memory.py:106`，300 字短句列表），
**摘要会丢细节**。引用机制是摘要的补丁——重要的历史用引用锚定，不靠摘要背。

**做法**：
1. `AgentReference.kind` 扩一个 `"message"`
2. 前端：历史消息 hover 出「引用」，塞 `{kind:"message", message_id, title}`
3. 后端 prompt 渲染成 `# 引用的历史对话` 段，**并把被引用的消息原文也带上**
   （只给标题等于没给）
4. 渲染形如：
   ```
   - [第 3 轮 · 用户] 把第三集改成雨夜场景
   - [第 7 轮 · 助手] 已按雨夜重排，共 42 镜
   ```

#### C3. 资产可被 Agent 发现

**现状**（2026-11-10 更新）：原来那个 `add_asset` 工具已随 D-37 整条拆掉
（参考视频只从参考素材里选）。Agent 仍然没有「列出可引用资产」的工具——
`get_status` 不返回资产清单。

**他们的现状**：更严——`packages/mcp` 里**零个** asset 工具，Agent 不能列举素材库；
`generate_media` 能带 `referenceAssetIds`，但 ID 必须由用户在弹窗里勾选，
Agent 原样带回，服务端强制比对。

**结论**：他们这条不必抄（他们的做法反而限制了 Agent）。我们要做的是
**让 Agent 能查资产 + 能引用资产**，比他们更开放：
- 加 `list_assets(project_id, kind?)` 工具
- `submit_videos` 接受资产 id 作为参考锚（镜头上的 `reference_asset_ids` 已经是这个形状）

---

## 四、明确不采纳的（避免反复调研）

| 他们有 | 我们不采纳 | 理由 |
|---|---|---|
| Codex app-server 作为 Agent 内核 | ❌ | 依赖外部二进制 + 账号体系；我们的自研 while 循环更可控、可测（`app/chat/loop.py` 已有 15 个测试）。D-29 已定 |
| cwd 作为文档上下文 | ❌ | 我们没有原生 read/write/edit 工具；硬造文件层上下文层成本极高。等价方案见 C1 |
| 每轮新起进程 + `thread/resume` 续接 | ❌ | 我们的消息本来就在 `chat_messages` 表，自己管上下文比借外部进程的记忆更可控 |
| Skill 正文全靠提示词自律、无代码校验 | ❌ | 这是他们的短板。我们有 `dramaturgy.py` + `evals/assertions.py`（D-05 / D-13） |
| 资产库三表结构 | ⚠️ 部分 | 我们的 `video_style_json` 实体 id 索引 + 名字兼容读取（D-34）解决了「改名后锚点全断」，
他们用 section-id 解决同一问题。我们不换，但 C1 的资源索引可以借鉴他们的「按 id 索引 + 面向用户的标题」思路 |
| Skill 的 `editable: false` 分层 | ⚠️ 部分 | 值得做（D-36 附注）：我们把 5 个 main 技能里的「铁律/硬规矩」逐条判定，
全局不变量下沉到 `conductor.py` 的代码层，技能只留流程 |

---

## 五、验证方式

| 阶段 | 命令 | 通过标准 |
|---|---|---|
| 改提示词后 | `python -m evals.run` | 确定性层 critical == 0 |
| 改提示词后 | `python -m evals.run --judge` | judge 均分不低于改前（阈值 75 / 观察线 85） |
| 改技能结构后 | `python -m evals.skill_eval --ablate --repeat 3` | 约束落地率不低于当前 82% |
| 改代码后 | `pytest tests -q` | 387 tests 全绿（改动后按实际数更新） |
| 静态检查 | `ruff check --select E9,F63,F7,F82,B .` | 0 error（接入 CI 后由 CI 保证） |

---

## 六、建议执行顺序

```
第 1 批（半天）：A1-a 幂等止血 · A3 ruff 进 CI · B1 抗绕过句式 · B2 不要编造 ID
第 2 批（1 天） ：A1-b/c 意图指纹 claim · A2 会话互斥 · B3/B4/B5 三条提示词
第 3 批（2-3 天）：C1 Agent 可读文档 · A4 文档数字修正 · B6/B7/B8/B9 打磨
第 4 批（按需） ：C2 引用历史对话 · C3 资产可发现
```

**第 1 批是纯增量、零破坏**，建议先做。其中 A1-a 是唯一一处「会真实烧钱」的洞。

---

## 七、相关文档

- [D-36 借鉴边界](DECISIONS.md#d-36-借鉴-mediago-drama-只移植具体手法不换-agent-内核)
- [D-05 确定性审计优先于 LLM 评判](DECISIONS.md) · [D-13 自建提示词质量评测框架](DECISIONS.md)
- [D-29 对话内核脱离 LangGraph](DECISIONS.md) · [D-30 技能拆分用消融对比验证](DECISIONS.md)
- [D-33 对话里用 @ 引用资产](DECISIONS.md) · [D-34 资产库两级导航](DECISIONS.md)
- [PLAN-agent-rebuild.md](PLAN-agent-rebuild.md) —— 2.0 改造主计划
- [`evals/README.md`](../evals/README.md) —— 提示词质量的唯一验收手段
