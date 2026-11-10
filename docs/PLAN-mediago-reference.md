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

> **进度（2026-11-10 第二批收尾）**：A 组全部完成——A1-a/b/c ✅（claim 表落地，见 D-38 与下方落地说明）、
> A2 ✅（**用进程内锁**实现，不是原计划的活动列，理由见 A2 节与 D-38）、A3 ✅、A4 ✅。
> A3 细节：CI 加一步 `uv run ruff check --select E9,F63,F7,F82,B app tests evals`；
> 存量 11 处致命违规当场清掉，其中 1 处是真 bug：`tests/test_assets.py` 里 `pytest.skip` 没导入 pytest，
> 本机没 ffmpeg 时会 NameError 而不是跳过。ruff 以 dev 依赖进 `pyproject.toml`（pin 0.14.0，
> CI `uv sync --frozen` 直接可用）。FastAPI 的 `File(...)` / `Form(...)` 默认参数写法是框架惯例
> 不是 bug，已在 `[tool.ruff.lint.flake8-bugbear] extend-immutable-calls` 里豁免 B008。
> B 组：B1/B2（第一批）+ B3/B4/B5 ✅（本批）。
> 待做：B6/B7/B8/B9（P2 打磨）· C1（Agent 可读文档）· C2/C3（P2）。

#### A1. 生成提交幂等 —— 同镜头重复提交会重复扣费 ⚠️ 最高优先 —— ✅ 已完成（A1-a/b/c，2026-11-10）

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

**A1 落地说明（2026-11-10，两批合成）**：实现比原计划多做了一层，**修得对**——

- 原计划「同镜头有在途任务就复用」会把「用户改了时长/画幅/参考素材再提交」也吞掉。
  这个边界是 `test_api.py` 既有的「时长显式覆盖」用例逼出来的（同镜头先投一次
  再用 `duration_sec=5` 重投，旧实现复用了第一次的任务，测试红）。
  所以幂等的判据是「**同镜头 + 同意图**」：意图 = prompt / provider / model /
  version_id + 时长、分辨率、画幅、首尾帧、参考图、音频、内容全部等价；
  任何一项变了就新建（换参数重出片是合法请求）。
- `reference_videos` 要单独处理：存下来的是「用户手挑 + 自动接力锚」合并的结果，
  接力锚随上一镜出没出完变化，不能比；只比用户手挑的那段。
- 等价定义：空串 / 空列表 / 没传视为同一件事；显式默认值 = 没传
  （`video_claim_key` 一处归一，`_same_intent` 同一套规则）。
- **A1-b 实际形态与原计划的差异**：表名是 `video_job_claims`（不是
  `generation_claims`）；「进入 provider 调用后失败不释放」落成了更精确的三条规则：
  ① 投递队列失败 → 任务标 failed → 终态钩子释放（此时服务商侧还没任务，
  重提不会重复计费）；② 进程死在「claim 了没建完任务」的缝里 → 10 分钟超时自愈；
  ③ 任务已真正提交出去后失败/成功 → claim 随终态释放，用户重提就是新一次出片
  （意图已被消费，挡着它只会把用户锁死）。sweeper 判僵尸 pending 为失败走的也是
  同一条释放路径。为什么不像 mediago 那样「失败也不释放」：那是给**一次性授权凭证**
  设计的（确认表单不可复用）；我们的 claim 只是**在途去重**，永久不释放会把
  失败意图的镜头锁死到重启。
- 批次互斥（顺带做的）：同一项目同时只允许一个批次在推进，第二个返回 409。
  两个批次会重复提交同一批镜头，还会互相抢「当前镜头」的完成回调
  （`_find_running` 按 current_shot_id 匹配，命中第一个就返回）。
  `BatchManager.running_for_project()` + `generate_batch` 入口检查。
- 非 claim 路径兜底（`_same_intent` 的第二个职责）：成片质检的自动重 roll
  直接 `create_video_job` 不占 claim——若用户此刻再点出片，claim 查不到、
  但镜头确实有在途任务，会第三次建档。所以 claim 未命中后还要查一次
  「同镜头在途的同意图任务」，命中就复用并把刚占的 claim 让回去。
- 工具话术跟上：`submit_videos` 如实报告「几个新建、几个本来就在跑」；
  `rerun_video` 对仍在生成的镜头直接拒掉并给出路（等它出完 / 先取消），不静默复用。
- API 响应加 `reused` 字段（True = 复用了既有任务），前端可据此提示。
- **A1-c 落地形态**：Agent 没有单独的确认凭证系统——问剧的「用户点头」本来就在
  对话层（`approve_video_prompts` 硬闸门，未批准的镜头 `submit_videos` 直接拒绝，
  B1 又封堵了话术绕过）。claim 覆盖了「点头之后、执行环节里」的重放风险，
  这就是 A1-c 要的「无授权不得提交」闭环，不另造 selectionId 体系。

**验收**：重复调用 `submit_videos` 两次，`video_jobs` 表只多一条记录 ✅；
`tests/test_video_idempotency.py`（14 用例：HTTP 幂等 / 参数变化放行 / 批次互斥 /
claim 生命周期四态 / 缝里拒绝 / 崩溃自愈 / 质检重 roll 兜底 / 归一化规则）。

#### A2. 对话会话的 run 互斥 —— ✅ 已完成（2026-11-10，改用进程内锁）

**问题**：`grep active_run app/chat/conductor.py` → 零结果。同一 conversation 并发两个请求，
会话消息交错、工具上下文串。

**参照**：`services/server/internal/service/agent/agent_sessions_service.go:278`
（`session.hasActiveRuns()` → 409）。

**实现与原计划的差异（结论性变更，理由见 D-38）**：原计划「conversations 表加
`active_run_id` + sweeper 双保险」**改成了进程内 `threading` 锁**
（`conductor.py` 的 `acquire_thread` / `release_thread`）。因为对话 run **不跨请求存活**
——一轮对话就活在这个请求的线程里，进程重启它自然没了；落库方案需要的 stale 超时、
启动清零、心跳，全是为「活得比请求久的资源」准备的清理路径，对话不是这种资源。
进程内锁天然覆盖这三个出口：正常结束（`finally` 释放）、出错（同一 `finally`）、
客户端断开（SSE generator 被关闭时其 `finally` 释放——这是最容易被忘的锁死源，
有专门测试 `test_stream_releases_after_client_disconnect` 锁住）。
构造 SSE 响应阶段就抛异常的边角也有出口（`handed_off` 标志交棒）。

**保留原计划的部分**：审阅动作（resume）不占位不报错——它是确定性路径、秒回，
跟 LLM 轮并发执行不会互踩。

**验收**：`tests/test_chat_mutex.py` 7 用例——忙时非流式/流式都 409、resume 放行、
正常完成后释放、**客户端断开后释放**（轮询 50 次兜底异步收尾）、连续两轮可用、
两线程并发占位恰一个成功。

#### A3. ruff 致命集进 CI —— ✅ 已完成（2026-11-10）

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

**原问题**：`README.md:299` 写「374 tests，30 个测试文件」，实测（2026-11-10）**411 tests / 33 个测试文件**
（基线 402 / 32；本批新增 `tests/test_video_idempotency.py` 9 个用例）。
`docs/ARCHITECTURE.md` 写「20 个工具」，`README.md:58` 写「23 个」，实测 **23 个**
（`app/chat/tools/` 里 `@tool` 装饰器 23 处）。

**已做**：`README.md`（工具数、测试数、测试文件数）与 `docs/ARCHITECTURE.md`
（6 处工具数 + 历史合并说明）按实测值改正。**下次改代码顺手改，别再攒。**

> 2026-11-10 复核：拆掉「登记公网直链」的 `add_asset` 工具后，实测 **22 个工具 /
> 401 tests / 32 个测试文件**，相关数字已再改一遍（见 [D-37](DECISIONS.md#d-37-参考视频只从参考素材里选不再收公网直链)）。

---

### B 组 · 提示词手法（P0/P1，全部有现成对照文本）

> 他们比我们强的 6 个具体写法。每条都给「他的原文 → 我们的改法」。

#### B1. 抗绕过条款：「用户说 X 也不能跳过 Y」 —— P0 —— ✅ 已完成（2026-11-10）

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

#### B2. 「不要编造具体 ID」的枚举清单 —— P0 —— ✅ 已完成（2026-11-10）

> 落地时压缩了措辞：系统提示有 2400 字预算（`test_system_prompt_stays_within_budget`），
> 原文加进去超了 147 字。最终形态 = 硬规矩 1 保留「严禁凭对话历史编造版本号（ver_xxx）」原句
> （`test_system_prompt_keeps_hard_rules` 断言的就是这句话），新增 1b 一行点名其余 id 类型
> ——`1b. 同样禁止编造角色/地点/节拍/镜头 id、音色 id、素材 id、任务 id、参数名——只能取工具返回值`。
> 最终 2378 字。比原方案略弱（id 前缀示例只剩 ver_xxx），但预算硬约束是实测出来的，不能绕过。

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

#### B3. 「不要用 A 方案替代 B 方案」 —— P1 —— ✅ 已完成（2026-11-10）

> 落地：`asset-generation` 硬规矩 1（「不替用户决定，也不零碎地问」+「要问就一次问完」）
> 与 `script-review`「卡片上的四个动作」末尾（整批摊开、不逐条追问）。

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

#### B4. 「不因缺 X 而暂停」的免问清单 —— P1 —— ✅ 已完成（2026-11-10，落点修正）

> **落地位置与原计划的差异**：原计划往 `style-guide` / `director-breakdown` 里加——
> 这两个是 **crew 技能**，由代码直接塞进单次结构化调用（D-04：剧组是函数不是 Agent），
> 它们根本没有「向用户提问」的通道，写了也无处生效。真正的过度反问发生在
> **主 Agent** 侧（conductor 硬规矩 4「不确定时先问」没有下界）。
> 所以免问清单落在了主 Agent 会加载的 `asset-generation` SKILL.md（「一、先问清楚」
> 末尾新增「这些不用问，问了就是打扰」：画风沿用项目既定/默认、能推断的按推断写并汇报注明、
> 只有影响剧情走向的缺失才值得问）。这是「先问，再做」流程里唯一「不用问」的白名单，
> 与 B3「一次问完」同处一节，读的人不会漏。

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

#### B5. 「禁止同上/沿用前镜」的省略禁令 —— P1 —— ✅ 已完成（2026-11-10）

> 落地：`video-prompt-zh` 与 `video-prompt-en` 均在规则 1 之后加了 **1b 自包含禁令**
> （两个方言版同步，避免行为分叉）。编号用 1b 不重排 1–11——frontmatter 描述、
> `crew-pipeline` 的「十一条写作规则」引用都不受影响。
> 与「锚逐字复用」的边界在条目里写死了：锚 = 同样内容再写一遍（要重复），
> 指代 = 内容省略成引用（要展开）。

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
| 改代码后 | `pytest tests -q` | 426 tests 全绿（测试数随每批新增，以实际输出为准） |
| 静态检查 | `ruff check --select E9,F63,F7,F82,B .` | 0 error（接入 CI 后由 CI 保证） |

---

## 六、建议执行顺序

```
第 1 批（半天）：✅ 已完成（2026-11-10）
  A1-a 幂等（含意图比对与批次互斥）· A3 ruff 进 CI · B1 抗绕过句式 · B2 不要编造 ID · A4 数字修正
第 2 批（1 天） ：A1-b/c 意图指纹 claim · A2 会话互斥 · B3/B4/B5 三条提示词
第 3 批（2-3 天）：C1 Agent 可读文档 · B6/B7/B8/B9 打磨
第 4 批（按需） ：C2 引用历史对话 · C3 资产可发现
```

---

## 七、相关文档

- [D-36 借鉴边界](DECISIONS.md#d-36-借鉴-mediago-drama-只移植具体手法不换-agent-内核)
- [D-05 确定性审计优先于 LLM 评判](DECISIONS.md) · [D-13 自建提示词质量评测框架](DECISIONS.md)
- [D-29 对话内核脱离 LangGraph](DECISIONS.md) · [D-30 技能拆分用消融对比验证](DECISIONS.md)
- [D-33 对话里用 @ 引用资产](DECISIONS.md) · [D-34 资产库两级导航](DECISIONS.md)
- [PLAN-agent-rebuild.md](PLAN-agent-rebuild.md) —— 2.0 改造主计划
- [`evals/README.md`](../evals/README.md) —— 提示词质量的唯一验收手段
