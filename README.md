# 问剧（Wenju）

小说 → 剧本 → 短剧 全链路 Agent 工作台。

**只做一件事：把一部小说变成一部短剧。** 不做电影 / 剧集 / 舞台剧，不做 3D 预览，
也不做多厂商视频适配——每条旁支都要分走一份注意力，而这条链路还远没打磨好。
漫剧不是另一种体裁，它只是短剧的一种画风（见 `docs/DECISIONS.md` D-19）。

基于 LangGraph 的多 Agent 协作系统，模仿真实剧组分工（编剧生成 → 导演拆镜 → 美术定视觉 → 摄影出提示词），通过对话完成从导入小说到生成短剧的完整流程。AI 给出可解释、可逐条选择、可回滚的修改建议，由人来决定接受、编辑或拒绝。

- Python 3.12+ / LangGraph 1.x / LangChain 1.x / FastAPI
- 后端 FastAPI（REST + 流式对话），前端 Vue 3 + Vite
- 可选 Postgres（业务库 + checkpointer）
- 模型接入：统一门面 `app/llm/`，一套 OpenAI 兼容协议覆盖 OpenAI / DeepSeek / 智谱 GLM / Kimi / Qwen / Ollama；对话、视觉质检、文生图三维度可分别用不同厂商
- 视频生成：MiniMax 海螺（用户自带 Key 与模型名，在「设置 → 视频生成」里配置）

## 为什么做这个项目

市面上常见的剧本工具大多是「一次性生成」：输入小说，吐出一份剧本，结束。这类工具价值有限，因为：

1. 一次生成的剧本仍需要人工继续修改；
2. AI 无脑覆盖会破坏作者已有的结构；
3. 没有版本、没有回滚，也说不清「这条改动到底改了什么」。

这个项目把重心从「AI 一次生成」移到「AI 如何安全、可控地参与改写」，这才是 Agent 的核心命题。

## 功能

### 剧本改编

- **状态图编排**：LangGraph StateGraph + 条件边，模型是否用工具、用户是否接受都用边来表达。
- **ReAct 工具调用**：Agent 自主决定查看场景详情、搜索原文、查版本历史、校验。
- **人机协同**：`interrupt` 暂停图执行，`Command.resume` 恢复；支持接受 / 编辑 / 重新生成 / 拒绝四种决策。
- **自我审阅**：`guard` 节点先 dry-run 应用并自纠错，再交给人类。
- **评审打分 + 一致性保障**：guard 之上再跑一次 LLM 审阅，对提议按「忠实度 / 一致性 / 冲突 / 风格 / 结构」五维打分。
- **结构化输出**：patch 是原子操作，可逐条审、逐条接受、可回滚。
- **对话式交互**：用自然语言完成从导入、改编到审阅的整套流程。

### AI 短剧制作

- **只做短剧，画风可换**：改编类型专注短剧（竖屏、1–2 分钟、前 3 秒抓人），节奏默认 4–6 秒一镜、导演缺省 `pacing=fast`。画风是**正交维度**（实拍 / 二次元 / 三维 / 水墨）——漫剧不是另一种类型，而是「短剧 + 二次元画风」，共用同一套导演与节奏逻辑，只改定妆图、提示词画风声明与配音气质。
- **资产库（Casting Bible）**：一张表给出每个角色的**外貌锚 / 定妆图 / 性别（含判定依据）/ 音色（含分配依据）/ 台词量**，以及每个场景的环境锚与场景图。音色可直接改、可试听；性别改配音色池重排。成片报告里也会带上这份选角表，避免「男主开口是御姐音」这类问题只能等成片出来才发现。
- **剧组 Agent 系统**：导演 / 美术指导 / 摄影指导三个 Agent 各司其职（美术指导产出的角色造型与环境描述是全片视觉锚的唯一来源，下游逐字复用）。
- **参考资产流水线（图像级一致性）**：为角色与场景生成定妆图 → 视觉模型质检 → 注册为全局参考 → 回填到镜头，让视频模型「看见」同一个角色，而不是只靠文字描述。
- **镜头级视频生成**：镜头数由导演按剧情内容决定（分镜 = 一段连贯的角色动作或一段连贯的角色对话剧情）。摄影指导只提出 Prompt 草稿，用户可逐镜编辑、保存、标记需修改或批准；只有批准后的 Prompt 才能提交视频任务。首尾帧接力 + 参考图/参考视频锁一致性，最后 FFmpeg 拼接。
- **成片合成（最后一公里）**：`finalize` 把成功镜头下载后**逐镜实测时长校准**（provider 产出常与计划时长有偏差，不校准则字幕/配音越往后越飘），再拼装 → 配音（TTS 真实语音，模型本身产不出人声）→ ASS 字幕烧录，成片与字幕注册为项目素材直接播放。降级阶梯：配音失败交静片+估算字幕，烧录失败交无字幕成片，单镜失败跳过并警告。
- **视频 Provider 抽象层**：产品统一走 MiniMax（自带 Key，模型名可换）；底层保留统一接口的多厂商适配（Runway / Kling / CogVideoX / Sora），需要时可扩展启用。
- **一把 key 全链路**：MiniMax 同时接了对话（`MiniMax-Text-01`）、视频（H3）与配音（T2A）——已有 MiniMax key 的用户零配置即可跑通。
- **人审环节**：剧本改编走 `interrupt` 暂停 / `Command.resume` 恢复的四决策闭环（接受 / 编辑 / 重新生成 / 拒绝）；视频侧先审 Prompt，再审成片，所有决定都进入「分镜版本 → 提示词版本 → 成片版本」的链式快照。
- **版本迭代**：视频版本链式快照，支持回溯/分叉/里程碑标记。

> 开发指引与踩坑记录见 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)；
> 包结构、模型接入层、Agent 清单与核心链路见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)；
> 技术选型的「为什么」见 [`docs/DECISIONS.md`](docs/DECISIONS.md)。

### 工具集

**对话 Conductor 工具（22 个）**：剧本侧 `create_project`（超长原文主动提示）/ `map_novel` / `propose_arcs` / `generate_script` / `run_adaptation` / `get_script_overview` / `ask`；剧组 `breakdown_scenes` / `generate_style_guide` / `generate_video_prompts` / `generate_reference_assets`；视频侧 `list_video_shots` / `approve_video_prompts` / `submit_videos` / `get_video_jobs` / `rerun_video` / `finalize_film` / `check_finalize`；运维 `check_background_tasks`；记忆 `remember` / `recall`；技能 `load_skill`。从导入小说到出片的全流程都能在对话里完成；每轮的工具轨迹压缩后进入历史上下文，多轮不丢事实。

**主 Agent 按需加载技能**：系统提示只放「触发 → 动作」的路由表 + 四个技能的目录，真正怎么做（步骤顺序、硬门槛、失败处理）在 `app/skills/{novel-serial,crew-pipeline,production-delivery,script-review}/SKILL.md` 里，进入该领域时由 `load_skill` 按需拉取。实测（每例连跑 3 次）：长篇直接要初稿 3/3 会先加载细则并拒绝整本生成，改编 3/3、出片 3/3 在动手前加载，「拆好了吗」3/3 不多花一次往返。改流程只动 `.md`，不碰代码。

**长篇小说改编**：单次剧本生成的原文容量约 1.2 万字，超长文本不再静默砍头——`create_project` 会如实提示规模；先 `map_novel`（后台 map-reduce 逐章摘要+出场人物，存 `projects.novel_map_json`）建章节地图，再 `propose_arcs` 提 2-3 个候选故事弧（清单带每章字数，单弧约束在 1.2 万字生成容量内，大弧自动拆上下部）；用户选定后 `generate_script(from_chapter, to_chapter)` 按章生成，「继续改编下一段」直接按容量从进度续接；连载推进时自动携带已改编章节的「前情提要」保持人物与情节衔接（进度锚 `projects.adapted_until`，项目 API 已暴露地图与进度）。**可靠性护栏**（deepseek 实测驱动）：模型摘要/剧本输出不守格式时结构化容错解析而非整体回退；回复声称了「已生成/已建成」但本轮没有真实工具返回时，系统自动更正并带纠错指令重跑一次，杜绝凭对话历史编造版本号。

**三层用户记忆 + 会话滚动摘要**（`app/memory.py`）：结构化记忆替代 RAG——preference（全局偏好）/ decision（项目决定）/ feedback（对话反馈）存 `memories` 表，按作用域注入系统提示；接受 / 拒绝审阅时自动沉淀（`learn_from_decision`）。长对话不再掐头：超过保留窗口的旧消息会被 LLM 压缩成事实清单（存 `conversations.summary_json`），上下文 = 摘要 + 最近消息，摘要失败自动回落为截断。

**改编图 ReAct 工具（7 个）**：

| 工具 | 说明 |
|------|------|
| `get_script_overview` | 查看剧本概况 |
| `get_scene_detail` | 查看场景详情 |
| `get_source_text` | 查看原文片段 |
| `search_source` | 在原文中搜索关键词 |
| `get_author_style` | 查看作者语言风格 |
| `list_versions` | 查看历史版本 |
| `validate_tool` | 校验剧本一致性 |

### 其他

- **聊天与项目解耦**：可直接开聊，无需先建项目。
- **工作区文件树**：左栏显示本地文件夹映射。
- **剧本导出**：一键导出为 `.txt / .md / .docx`。
- **剧本编辑器**：直接改台词、动作、场景，保存成新版本。
- **剧组 Agent**：导演拆解分镜 → 美术指导锁定视觉风格 → 摄影指导提出 Prompt → 人工逐镜批准 → 参考资产与视频生成。

## 架构

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  STAGE 1    │    │  STAGE 2    │    │  STAGE 3    │
│  小说 → 剧本 │───→│  剧本 → 分镜 │───→│  分镜 → 短剧 │
│  (编剧组)    │    │  (导演组)    │    │  (制作组)    │
└─────────────┘    └─────────────┘    └─────────────┘
```

分层（依赖单向向下，见 `docs/ARCHITECTURE.md`）：

- `app/llm/`：**统一模型接入层**。门面 `LLM` + 厂商表 + 配置解析（`.env` / DB），覆盖 chat / vision / image 三个能力维度。
- `app/pipeline/`：**内容生产线**。导入解析、两阶段生成、patch 与校验、题材识别、评审、导出；不依赖 LangGraph，也不感知 HTTP。
- `app/agent/`：**改编智能体**。`graph` / `nodes` / `tools` / `state` 是 LangGraph 状态图，`runner` 是薄服务层，`skills` 是后台任务运行器（剧组任务）。
- `app/chat/`：**对话式编排**（ChatConductor），绑定工具理解意图，底层复用 `app/agent` 的审阅工作流。
- `app/crew/`：**剧组 Agent**（导演 / 美术指导 / 摄影指导）。
- `app/media/`：**参考资产流水线**（定妆图 → 视觉质检 → 注册 → 回填镜头）。
- `app/video/`：视频 Provider 抽象层 + 异步任务队列 + 连续性解析 + FFmpeg 拼接。
- `app/api/`：**REST 路由包**，按域拆分为 system / projects / versions / agent_runs / chat / video；统一经 `api/deps.py` 取依赖。
- `app/store.py`：业务持久化（SQLAlchemy，Postgres / SQLite）。
- `frontend/`：Vue 3 + Vite 单页，三栏布局（聊天 + 创作区 + Inspector）。

### LangGraph 图拓扑

```text
START → context → plan ──(有工具调用)──> tools → plan   (ReAct 循环)
                        └──(无工具调用)──> propose → guard(自纠错)
                                                  │(问题)└→ propose（重做循环）
                                                  │(通过)└→ review(interrupt)
                       ┌──(accept/edit)──> apply ──> finalize ──> END
                       ├──(regenerate)───> propose
                       └──(reject)──────> finalize ──> END
```

## 快速开始

### 方式零：一键启动（Windows）

双击 [`start.bat`](start.bat) 即可。它会自动：生成零依赖配置（SQLite + 进程内 checkpoint，无需 Postgres）→ 同步 Python 依赖 → 首次运行构建前端 → 单进程同时服务后端与前端 → 打开浏览器 `http://127.0.0.1:8000`。

要启用完整 AI 能力，编辑生成的 `.env` 填入模型 key（完整可配项见 `.env.example`）；不填也能跑通全链路演示（本地规则回退）。改了前端代码后删掉 `frontend/dist` 再跑一次即可重新构建。

### 方式一：本机开发（前后端分离，热更新）

```bash
# 0. 安装 uv（一次性，包与解释器管理工具；详见 https://docs.astral.sh/uv/）
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 1. 安装依赖（自动创建 .venv，按 uv.lock 精确复现环境）
uv sync

# 2. 配置环境变量（可选，不配也能跑通演示）
# 复制 .env.example 为 .env，填入模型 key
cp .env.example .env
#    ⚠️ 这份模板指向 Postgres，**必须先起库**，否则第 4 步每个请求都会返回 503：
#      docker compose up -d          # 或见「方式二」
#    想零依赖跑，就把这两行改成：
#      DATABASE_URL=sqlite:///./data/dev.db
#      CHECKPOINTER=memory

# 3. 运行命令行演示
uv run python -m app.cli

# 4. 启动 API
uv run uvicorn app.main:app --port 8000 --reload

# 5. 启动前端
cd frontend && npm install && npm run dev
# 打开 http://localhost:5173
```

不配模型 key 时，全部走本地回退，链路依然闭环。CI 在每次 push / PR 时自动跑后端测试与前端构建（见 `.github/workflows/ci.yml`）。

### 视频 Prompt 审阅

导演拆镜和摄影指导完成后，视频工作台会把每个 Prompt 标为「待审阅」。用户可以：

1. 打开单个镜头，直接修改最终提示词；
2. 保存草稿，保留当前版本但暂不允许出片；
3. 标记需要修改，留下审阅备注；
4. 批准 Prompt，解锁单镜或批量视频生成。

每次保存都会创建新的视频版本快照，因此 Prompt 修改和审批决定都可以回看、比较和回退。可在镜头列表中多选待审阅镜头批量批准，版本栏会显示每个快照的批准比例与整体状态；打开单镜审阅面板还能查看父版本链和逐次差异。视频批次只会读取已批准的镜头。

### 方式二：用 Docker 提供 Postgres

```bash
# 1. 准备环境变量
cp .env.example .env

# 2. 启动 Postgres
docker compose up -d

# 3. 本机安装依赖并连 Postgres 启动
uv sync
export DATABASE_URL="postgresql+psycopg://script:script@localhost:5432/script_agent"
export CHECKPOINTER="postgres"
uv run uvicorn app.main:app --port 8000
```

两种方式访问入口相同：

- Web：http://localhost:8000
- API 文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/healthz

## 项目结构

```
Wenju/
├── README.md
├── pyproject.toml
├── docker-compose.yml        # Postgres
├── .env.example
│
├── app/
│   ├── main.py               # FastAPI 入口
│   ├── config.py             # 配置管理（.env → Settings）
│   ├── deps.py               # 依赖单例装配点
│   ├── domain.py             # 领域模型（Script + Shot + StyleGuide）
│   ├── store.py              # 业务持久化
│   ├── workspace.py          # 工作目录文件树
│   │
│   ├── api/                  # ★ REST 路由包（按业务域拆分）
│   │   ├── system.py         #   运行状态 / 后台任务 / 工作目录
│   │   ├── projects.py       #   项目生命周期、导入、落盘、笔记、文件
│   │   ├── versions.py       #   剧本版本 + 导出
│   │   ├── agent_runs.py     #   改编智能体的运行与审阅
│   │   ├── chat.py           #   对话式 Agent 与会话管理
│   │   └── video.py          #   Provider / 模型偏好 / 视频版本 / 生成任务
│   │
│   ├── llm/                  # ★ 统一模型接入层
│   │   ├── client.py         #   LLM 门面（chat / structured / vision / image）
│   │   ├── providers.py      #   厂商表（OpenAI 兼容 / DeepSeek / 智谱 / Kimi / Qwen / Ollama）
│   │   ├── registry.py       #   配置解析（.env 优先级链 + DB 偏好）
│   │   ├── vision.py         #   视觉质检客户端
│   │   └── image.py          #   文生图客户端
│   │
│   ├── pipeline/             # ★ 内容生产线（无 LangGraph / 无 HTTP 依赖）
│   │   ├── generation.py     #   两阶段生成：故事圣经 → 场景规划
│   │   ├── patch.py          #   结构化 patch + 剧本校验
│   │   ├── profiles.py       #   改编类型 profile
│   │   ├── knowledge.py      #   题材识别 + 作者风格
│   │   ├── review.py         #   五维评审打分
│   │   ├── importer.py       #   文件导入解析
│   │   ├── export.py         #   导出 txt / md / docx
│   │   └── chunking.py       #   文本切片
│   │
│   ├── agent/                # ★ 改编智能体
│   │   ├── graph.py          #   LangGraph 图拓扑
│   │   ├── nodes.py          #   图节点实现
│   │   ├── tools.py          #   ReAct 工具集
│   │   ├── state.py          #   AgentState
│   │   ├── runner.py         #   运行服务（启动 / 恢复 / 兜底）
│   │   └── tasks.py          #   后台任务运行器（剧组任务进度上报）
│   │
│   ├── skills/               # ★ 技能（长指令外置，按需加载）
│   │   ├── loader.py         #   加载器（frontmatter + 渐进披露 + 轻量模板 + 受众分组）
│   │   ├── director-breakdown/SKILL.md   # 导演分镜方法论（crew）
│   │   ├── video-prompt-zh/SKILL.md      # 摄影指导写作规则（中文，crew）
│   │   ├── video-prompt-en/SKILL.md      # 摄影指导写作规则（英文，crew）
│   │   ├── style-guide/SKILL.md          # 美术指导方法论（crew）
│   │   ├── novel-serial/SKILL.md         # 长篇分段改编（main，主 Agent 按需加载）
│   │   ├── crew-pipeline/SKILL.md        # 剧组四件事的依赖与审阅边界（main）
│   │   ├── production-delivery/SKILL.md  # 批准 → 出片 → 合成（main）
│   │   └── script-review/SKILL.md        # 改编审阅闭环（main）
│   │
│   ├── chat/
│   │   ├── conductor.py      # 对话编排：系统提示 + 对话图 + 轮次服务
│   │   └── tools/            # 22 个工具的实现，按域拆分（screenplay/crew/video/production/memory_tools）
│   ├── media/
│   │   └── refs.py           # 参考资产流水线（定妆图 → 质检 → 回填）
│   ├── crew/                 # 剧组 Agent
│   │   ├── director.py       #   导演（分镜）
│   │   ├── art_director.py   #   美术指导（视觉锚唯一来源）
│   │   └── dp.py             #   摄影指导（视频提示词构建）
│   ├── video/                # 视频生成
│   │   ├── base.py           #   Provider 抽象基类
│   │   ├── registry.py       #   Provider 注册表
│   │   ├── queue.py          #   异步任务队列
│   │   ├── continuity.py     #   连续性解析（接力 → 参考视频）
│   │   ├── assemble.py       #   FFmpeg 拼接
│   │   ├── finalize.py       #   成片合成（拼装 → 配音 → 字幕烧录）
│   │   └── providers/        #   5 个 Provider 适配器
│
├── frontend/
│   └── src/
│       ├── App.vue           # 三栏布局
│       ├── stores/app.js     # 全局状态
│       └── components/
│           ├── ChatList.vue          # 聊天列表
│           ├── WorkspaceTree.vue     # 工作区文件树
│           ├── SettingsModal.vue     # 模型/Provider 设置
│           ├── ShotList.vue          # 镜头列表
│           ├── VideoPlayer.vue       # 视频播放器
│           └── ...
│
├── docs/                     # ★ 文档
│   ├── README.md             #   文档索引（先读这个）
│   ├── ARCHITECTURE.md       #   分层结构 / 模型接入 / Agent 清单 / 核心链路
│   ├── DEVELOPMENT.md         #   开发指引 + 踩坑记录 + 排查手册
│   ├── DECISIONS.md          #   技术选型记录（为什么用这个）
│   └── DRAMATURGY.md         #   「先戏剧后语法」方法论
│
├── evals/                    # ★ 提示词质量 eval（改提示词后跑这个）
│   ├── README.md             #   怎么跑、怎么加 case、判定标准
│   ├── rubric.md             #   五维评分量表
│   ├── assertions.py         #   确定性断言库
│   ├── cases.py              #   10 条 golden case
│   ├── runner.py             #   引擎
│   └── fixtures/             #   录制的真实产出（离线回归用）
│
└── tests/                    # 测试套件（426 tests，34 个测试文件）
```

## 环境变量

模型接入统一由 `app/llm/registry.py` 解析：**显式指定 > `.env` 优先级链（OPENAI > ZHIPUAI > DEEPSEEK）> 数据库里配的 provider**。
对话、视觉质检、文生图三维度可分别配置，互相独立。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CHAT_PROVIDER` | 强制指定对话厂商（`openai` / `deepseek` / `zhipu` / `minimax` / `ollama` / `moonshot` / `qwen`） | 按优先级链自动选 |
| `ZHIPUAI_API_KEY` | 智谱 key（GLM 对话 / CogView 文生图 / CogVideoX 视频共用） | - |
| `DEEPSEEK_API_KEY` | DeepSeek key（也用于默认视觉质检模型） | - |
| `OPENAI_API_KEY` | OpenAI 或任意兼容服务的 key | - |
| `MINIMAX_API_KEY` | MiniMax key（**对话 / 视频 H3 / 配音 T2A 一把 key 全链路**） | - |
| `MINIMAX_API_KEY_FALLBACKS` | 备用 key（逗号分隔，可多把）。主 key 欠费/失效时自动顶上，顺序即优先级；坏 key 冷却期间不再试 | - |
| `KEY_COOLDOWN_SEC` | key 被判失效后的冷却时长（秒），到期自动复活，无需重启 | `600` |
| `DEEPSEEK_THINKING` | 推理模型是否开启思考链 | `false` |
| `VISION_PROVIDER` / `VISION_MODEL` / `VISION_API_KEY` | 视觉质检单独配置（留空则自动复用支持视觉的厂商） | 自动 |
| `IMAGE_PROVIDER` / `IMAGE_MODEL` / `IMAGE_API_KEY` | 文生图单独配置（`cogview` / `openai`） | `cogview` → `openai` |
| `DATABASE_URL` | 数据库连接串（`sqlite:///./data/script_agent.db` 或 `postgresql+psycopg://…`） | 项目内 SQLite |
| `DB_CONNECT_TIMEOUT` | 数据库连接超时（秒）。不给的话 libpq 会一直等，库没起来时请求永久挂住 | `5` |
| `CHECKPOINTER` | checkpointer 类型（`memory` / `postgres`） | `memory` |
| `TTS_PROVIDER` / `TTS_MODEL` / `TTS_API_KEY` | 配音单独配置；`TTS_API_KEY` 留空时回落 `MINIMAX_API_KEY` | `minimax` / `speech-02-hd` |
| `VIDEO_QA_ENABLED` / `VIDEO_QA_FRAMES` / `VIDEO_QA_MAX_REROLL` | 成片抽帧质检与有界重 roll | `true` / `5` / `1` |
| `VIDEO_SWEEPER_ENABLED` / `VIDEO_SWEEPER_INTERVAL_SEC` | 孤儿视频任务巡检（重启后把无人轮询的任务挂回去） | `true` / `60` |
| `WORKSPACE_PERSIST` / `WORKSPACE_ROOT` | 是否把剧本/版本落成文件；`false` 时只留应用内 | `true` / `data/` |
| `FFMPEG_PATH` | ffmpeg 可执行文件路径（抽帧/拼接），留空用 PATH 里的 | - |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | LangSmith 轨迹上报；启动时映射为 `LANGCHAIN_*` | `false` |

> **两类配置的坑**：`MINIMAX_API_KEY_FALLBACKS`、`KEY_COOLDOWN_SEC`、`FFMPEG_PATH`、
> `LANGSMITH_*` 由代码**直接读 `os.environ`**，不经过 `Settings`。所有入口都必须调用
> `config.bootstrap_env()`（`main.py` / `cli.py` 已调用），否则这些配置会静默取默认值。
>
> **界面偏好优先于 `.env`**：在「设置 → 模型」里选过模型后，`.env` 的模型名不再生效；
> 点「恢复 .env 默认」或调 `DELETE /api/models/preferences/chat` 清除即可。

## 运行测试

```bash
python -m pytest tests -q
# 374 passed
```

## 提示词质量 eval

`tests/` 测「代码不崩」，**eval 测「提示词质量没退化」**——改提示词没有编译器，
只有 eval 能提前告诉你这次改动是不是把质量拉垮了。

```bash
python -m evals.run              # 跑 10 条 golden case（需要对话模型）
python -m evals.run --judge      # 额外跑 LLM judge 打分（看质量趋势）
pytest evals -q                  # 离线层：不需要 key

python -m evals.skill_eval --ablate --repeat 3   # 技能加载 eval（消融对比）
```

它已经抓到过三个真问题：审计器对真实提示词 4/4 误报「一镜多运镜」、
提示词里混进否定句（视频模型会把被否定的内容照画出来）、
每镜都带的全片风格段与本镜运镜指令互相矛盾。

另有一套 eval 专门回答「技能拆开到底有没有用」——用**消融对比**（把 `load_skill`
从工具集里摘掉，看 Agent 行为退化多少）：实测约束落地率 **82%（有技能）vs 58%（无技能），
Δ +25%**；价值集中在带硬约束的地方（依赖顺序一条就值 +60%）。

详见 [`evals/README.md`](evals/README.md)。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。

## 致谢

- [visual-skills](https://github.com/smixs/visual-skills)（Serge Shima，CC BY 4.0） - 《AIGC 视觉内容提示词方法论》，本项目 `app/crew/dramaturgy.py` 的确定性审计规则（场景公式、细节律、五锚点、空洞词表、否定句检测）源自该方法论，按 CC BY 4.0 要求署名
- [LangGraph](https://github.com/langchain-ai/langgraph) - 有状态 Agent 编排框架
- [LangChain](https://github.com/langchain-ai/langchain) - LLM 应用开发框架
- [FastAPI](https://github.com/tiangolo/fastapi) - 现代 Python Web 框架
- [PostgreSQL](https://www.postgresql.org/) - 关系型数据库
