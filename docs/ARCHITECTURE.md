# 架构说明 —— 问剧（Wenju）

> 本文回答四件事：**包结构怎么分层**、**不同模型厂商怎么适配**、**多 Agent 各是谁**、
> **核心链路从小说到成片怎么走**。踩坑与调试经验见 [`DEVELOPMENT.md`](DEVELOPMENT.md)。

---

## 零、一页速览

**是什么**：把一部小说改编成短剧并出片的本地单机工具。对话是主入口，工具是手脚，
资产与版本全部落库。

```
用户 ←→ 对话 Agent（手写工具循环，23 个工具）
          ├── 改编工作流（LangGraph 7 节点 + interrupt 人审）→ 剧本版本
          ├── 剧组三函数（导演 → 美术 → 摄影）→ 分镜 / 视觉锚 / 视频提示词
          ├── 出片（Provider 抽象 → 队列 → 巡检 → 拼接 → 配音字幕）
          └── 资产库（角色/场景/声音/素材，一律按实体 id 索引）
```

**四种「东西」必须分清**（最容易混）：

| 形态 | 谁 | 特征 |
|---|---|---|
| **真正的 Agent** | 只有对话层 1 个 | 模型自己决定下一步：循环调工具，直到不需要为止 |
| **固定工作流** | 改编 7 节点图 | 流程由代码定死，模型只在 `plan`/`propose` 产出内容；`review` 处 `interrupt` 等人 |
| **模板函数** | 剧组三角色 | 单次结构化调用，没有自己的工具与上下文循环 |
| **纯规则** | `crew/dramaturgy.py` | 词表 + 正则，判定确定性，零 LLM |

**三条贯穿全局的约定**：

1. **视觉锚只产一次，下游逐字复用**——环境与人物描述由美术指导产出一次，代码逐字拼进每个镜头。
   风格段相反：**只补空、不覆盖**（body 已写光影/运镜就不再贴，否则模型收到两套矛盾指令）。
2. **AI 只提议，人做决定**——改编走四决策闭环；视频先审 Prompt 再审成片，全部进链式快照。
3. **能用规则判的绝不用 LLM 判**——空洞词、否定句、引用台词、跨镜锚复用都是确定性检查。

**改东西先看哪里**：

| 想改 | 去哪 |
|---|---|
| 写作 / 分镜规则 | `app/skills/*/SKILL.md`（不用动代码，改完跑 `python -m evals.run`） |
| 对话行为与工具 | `app/chat/loop.py`（内核）+ `app/chat/tools/*`（23 个工具按域拆） |
| 改编流程 | `app/agent/graph.py` + `nodes.py` |
| 模型厂商 | `app/llm/providers.py` 加一条 `ProviderSpec` |
| 出片供应商 | `app/video/providers/` + `registry.py` |
| 配置项 | `app/config.py`（走 Settings）或 `app/keys.py`（直读 environ，入口须 bootstrap） |

---

## 一、包结构与分层


```
app/
├── main.py / cli.py                         入口层（HTTP / CLI，都调 config.bootstrap_env）
├── api/           REST 路由包 —— 按业务域分模块
├── config.py / deps.py                      配置与依赖装配（全项目唯一装配点）
├── keys.py                                 同厂商多把 key 的轮换与冷却（「哪把能过用哪把」）
├── domain.py / store.py / workspace.py      领域模型 / 持久化 / 文件系统
├── memory.py                               三层用户记忆 + 会话滚动摘要
│
├── skills/     技能（长指令）—— SKILL.md 按需加载，代码只留装配
├── llm/        模型接入层   —— 对上提供能力，对下屏蔽厂商
├── pipeline/   内容生产线   —— 纯逻辑，不依赖 LangGraph / HTTP
├── agent/      改编智能体   —— LangGraph 状态图（固定流程 + interrupt 人机协同）+ 后台任务运行器
├── chat/       对话编排层   —— 手写工具循环（loop.py）+ 工具实现按域拆分（chat/tools/）
├── crew/       剧组 Agent   —— 导演 / 美术指导 / 摄影指导
├── media/      参考资产层   —— 资产键解析（assets：id 是主键、名字是别名）/ 定妆图 → 质检 → 注册 → 回填 / 配音 / 字幕
└── video/      视频生成层   —— Provider 抽象 + 队列 + 巡检 + 连续性 + 拼接 + 成片合成
```

**体量分布**（约 2.1 万行 Python，越靠上越重业务）：

| 包 / 模块 | 行数 | 说明 |
|---|---|---|
| `pipeline/` | 2870 | 内容生产线：导入、两阶段生成、patch、评审、题材画像 |
| `api/` | 2646 | 9 个域模块（+ common/schemas/deps），83 条路由 |
| `video/` | 2518 | Provider 适配 + 队列 + 巡检 + 拼接 + 合成 |
| `crew/` | 2262 | 三个剧组角色的编排（提示词正文已外置到 `skills/`） |
| `chat/` | 2528 | 内核 332 行 + 编排 826 行 + 23 个工具实现 |
| `media/` | 1846 | 参考资产 / 配音 / 字幕 / 资产实体层 |
| `agent/` | 1678 | 7 节点图 + 后台任务运行器 |
| `llm/` | 1302 | 门面 + 厂商表 + 视觉/文生图/TTS 客户端 |
| `skills/` | 284 | 只有加载器；**长指令在 8 个 SKILL.md 里**（不计入 Python 行数） |
| `store.py` | 1795 | 16 张表的持久化层（含出片意图 claim 表 `video_job_claims`，见 D-38） |
| `domain.py` | 614 | Pydantic 领域模型（含 id 格式约束） |

`app/api/` 内部再分一层，避免路由堆在一个大文件里：

```
api/
├── __init__.py    组装 /api 前缀的父 router
├── deps.py        依赖入口（路由统一 deps.store() 取值，测试可整体替换）
├── common.py      共享的序列化与落盘辅助
├── schemas.py     请求模型
├── system.py      运行状态 / 后台任务 / 工作目录
├── projects.py    项目生命周期、初稿生成、导入、落盘、笔记、文件浏览
├── versions.py    剧本版本（含导出）
├── agent_runs.py  改编智能体的运行与审阅
├── chat.py        对话式 Agent 与会话管理（含图片上传/回放）
├── production.py  生产工作流（一键跑「剧本→拆解→风格→资产图→提示词」）
├── cast.py        资产库：角色形象 / 声音 / 图片 / 场景的唯一入口
├── assets.py      项目媒体素材入库（只有「上传」一条通道，见 D-37）
└── video.py       Provider / 模型偏好 / 视频版本 / 生成任务
```

**依赖方向（单向向下，不允许反向或跨层回指）**

```
入口层 (api / cli / main)
   ↓
编排层 (chat / agent)  ──→  crew / media
   ↓                          ↓
生产线 (pipeline)          视频层 (video)
   ↓                          ↓
基础层 (llm / domain / store / config)
```

规则：

1. `pipeline/` 里不准出现 LangGraph 与 FastAPI 的 import（它是纯函数层，可单测、可复用）。
2. 需要模型能力时只 `from ..llm import LLM`，不允许任何模块自己 new `ChatOpenAI` 或直接读写 API key。
3. 路由不直接 `from ..deps import store`，一律经 `app/api/deps.py` 取值——依赖查找是运行时的，
   测试替换一处即可覆盖全部路由。
4. 所有单例（Store / LLM / 视频队列 / 后台任务管理器）只在 `app/deps.py` 里创建，
   入口层取值；测试可以单独构造。后台任务的落库依赖也在这里注入。
5. **`chat/` 是手写循环、`agent/` 是状态图**，两者不要混：对话需要「模型自己决定下一步」，
   用循环；改编是固定七步 + `interrupt` 人机协同，用图。状态里只放可序列化数据，
   store / llm / tools 通过闭包注入。
6. **配置分两类**：走 `Settings` 的（pydantic 直接读 `.env`）和直读 `os.environ` 的
   （多 key 轮换、`FFMPEG_PATH`、`LANGCHAIN_*`）。后者要求入口先调
   `config.bootstrap_env()`——`main.py` / `cli.py` 已调，新写脚本别忘了（见 DEVELOPMENT 5.25）。


---

## 二、模型厂商适配（`app/llm/`）

### 2.1 设计：一个门面 + 一张厂商表

| 文件 | 职责 |
|---|---|
| `client.py` | 门面 `LLM`：`chat()` / `structured()` / `system_prompt()` / `vision()` / `image()` |
| `base.py` | 数据契约：`Modality`（chat/vision/image）、`ModelConfig`、`ProviderSpec` |
| `providers.py` | 厂商表 + `build_chat_model()`（唯一的 `ChatOpenAI` 构造点） |
| `registry.py` | 配置解析：`.env` / DB → `ModelConfig` |
| `vision.py` | 视觉客户端（定妆图质检），OpenAI 兼容多模态消息 |
| `image.py` | 文生图客户端（CogView / OpenAI Images） |

**适配策略：全部走 OpenAI 兼容协议。** 国内厂商（DeepSeek / 智谱 / Moonshot / Qwen）
与本地服务（Ollama / vLLM）都提供 OpenAI 兼容端点，因此不需要为每家引入官方 SDK——
差异只落在三处：**默认端点、默认模型、额外请求体**，用一张声明表描述：

```python
ProviderSpec(
    name="deepseek", label="DeepSeek",
    default_base_url="https://api.deepseek.com", default_model="deepseek-chat",
    env_key="DEEPSEEK_API_KEY",
    supports_vision=True, vision_model="deepseek-v4-flash-vision-exp",
    extra_body=_deepseek_extra_body,   # 推理模型需显式关闭 thinking
)
```

### 2.2 已适配的厂商

| 厂商 | 对话 | 视觉质检 | 文生图 | 备注 |
|---|---|---|---|---|
| OpenAI 兼容（含自建网关 / vLLM） | ✅ | ✅ | ✅ | `OPENAI_BASE_URL` 指向任意兼容端点 |
| DeepSeek | ✅ | ✅ | — | 推理模型需 `DEEPSEEK_THINKING=false` 直出 content |
| 智谱 GLM | ✅ | ✅ | ✅ | 同一个 key 同时可调 GLM / CogView / CogVideoX |
| Moonshot Kimi | ✅ | — | — | 用真实环境变量 `MOONSHOT_API_KEY` 配置 |
| 通义千问 Qwen | ✅ | ✅ | — | 走 DashScope 兼容模式 |
| MiniMax | ✅ | — | — | **一把 key 打通对话 / 视频（H3）/ 配音（T2A）**；`/chat/completions` 不支持 `response_format=json_object`，结构化输出走降级路径（见 2.5） |
| Ollama（本地） | ✅ | ✅ | — | 无需 key；仅在 `CHAT_PROVIDER=ollama` 时启用 |
| 视频（独立体系） | — | — | — | **只配了 MiniMax 一家**（用户自带 Key + 模型名，存 `api_providers.config_json.model`）。`VideoProvider` 抽象仍在（`app/video/base.py`），但 2026-11-01 已删掉 Runway / Kling / CogVideoX / Sora 四个从未启用的适配器——留着只是「将来可能支持」的想象，代价是每次改接口都要跟着改四份没人跑的实现 |

> 非 OpenAI 兼容的厂商（如 Anthropic Messages API）目前**没有**适配：需要为它单独实现
> 协议客户端，而不是加一条 `ProviderSpec`。这是有意的取舍——主流可选厂商里它不占多数，
> 且引入后要多维护一套 SDK 依赖。

### 2.3 配置解析优先级（`registry.py`）

```
显式指定（CHAT_PROVIDER / VISION_PROVIDER / IMAGE_PROVIDER）
   ↓ 没有
.env 厂商优先级链：OPENAI_* > ZHIPUAI_* > DEEPSEEK_*
   ↓ 都没有
数据库 api_providers / model_preferences（前端「设置 → 模型」里配的）
```

- **三个能力维度各自解析，互不干扰**：对话用 DeepSeek、质检用智谱视觉、文生图用 CogView 是允许的。
- 视觉未显式配置时，自动复用一个「自带视觉能力且 key 可用」的厂商，并把模型名换成该厂商的视觉模型。
- 文生图未显式配置时，按 `cogview` → `openai` 顺序找 key。
- 解析结果是一个 `ModelConfig`（`provider / model / api_key / base_url / extra_body / source`），
  上层只消费它，不关心来源是 `.env` 还是数据库。

### 2.4 新增一个厂商要改什么

1. 在 `providers.py` 的 `_register_builtins()` 加一条 `ProviderSpec`（若是 OpenAI 兼容，到此为止）。
2. 若该厂商需要额外请求体，给 `extra_body` 传一个函数。
3. 若它要能在 `.env` 里配置，在 `config.py` 加对应字段，并在 `registry._from_settings()` 里接上。
4. 若它提供新的能力形态（例如 TTS），在 `base.py` 的 `Modality` 加维度 + 新客户端文件。
5. 若它**不支持 `response_format=json_object`**，把 `ProviderSpec.supports_json_mode` 设为 `False`——
   `LLM.structured()` 会自动降级为「提示词要求 JSON + 容错解析 + 失败重采样」。

### 2.5 结构化输出的降级路径

`LLM.structured(schema)` 默认用 `json_mode`（`response_format=json_object`）。但不是所有
OpenAI 兼容服务都支持它——**MiniMax 实测报 `2013 unknown response_format type`**。

声明 `supports_json_mode=False` 的厂商会走降级实现（`client._JsonPromptStructured`）：

1. 把「只输出 JSON + 字段清单 + JSON Schema」追加到消息末尾；
2. 自己解析 JSON（容忍 markdown 围栏、前后缀文字）；
3. 解析失败带着错误信息重采样一次。

语义与 `json_mode` 等价，只多一次重试的机会。剧组链路本来就不用 `json_mode`
（走的是 `_invoke_json()` 的「普通对话 + 容错解析」），所以不受影响。

---

## 三、Agent 与技能：谁在编排，谁只是模板

先给结论，因为这块最容易想歪：

> **只有一个 LLM Agent（对话编排层），调度 23 个工具。剧组三个「角色」是函数，不是进程。**
> **「按需加载」只发生在主 Agent 那一层；剧组三个角色的技能是每次全量拼进系统提示的。**

为什么不做进程级多 Agent（每个角色独立上下文、独立工具集、互相通信）：它们各自只做
**一次结构化 LLM 调用 + 解析 + 校验**，没有多轮决策，也没有工具选择。套上进程级外壳只会
多出「状态怎么传、失败怎么回滚、上下文谁持有」三个问题。理由见 [`DECISIONS.md` D-04](DECISIONS.md)，
以及 [D-20](DECISIONS.md)（产品收敛到单一链路）。

### 3.1 对话编排（1 个）—— `app/chat/conductor.py` + `app/chat/loop.py`

| Agent | 角色 | 职责 |
|---|---|---|
| **ChatConductor** | 前厅 / 调度 | 绑定 23 个工具理解用户意图，把请求路由到确定性流水线或后台剧组任务 |

**内核是手写的显式循环**（`app/chat/loop.py`，2026-11-05 替换掉 LangGraph 的 StateGraph + ToolNode）：

```
for step in range(max_steps):        # 上限 8 轮，防止无限工具调用
    resp = model(messages, tools)
    没有 tool_calls → 结束
    for call: 执行工具 → messages.append(tool 结果)
```

- **消息用 OpenAI 兼容的 dict**（role/content/tool_calls/tool_call_id）——所有 provider 都是这个协议，
  协议本身不绑框架类型。LangChain 只在调模型那一刻被适配一次（`_to_lc_messages`），
  将来换自写 HTTP provider 只需替换这个适配层。
- 为什么不用 StateGraph：状态通道（reducer）行为不直观（踩过 `has_images` 这类键在节点间丢失），
  循环本来就是 while、画成图要多读一层，且调试是黑盒。见 [`DECISIONS.md` D-29](DECISIONS.md)。

关键设计：**审阅动作走确定性路径**（accept/reject 不经过 LLM），避免「对话式工具调用不可靠」导致误操作。

系统提示分两层：**触发器留在提示词**（决定该调哪个工具，每轮都要用），**执行细则进技能**
（`audience: main` 的四个 SKILL.md，由 `load_skill` 工具按需拉）。见 [`DECISIONS.md` D-14](DECISIONS.md)。

23 个工具的实现按域拆在 `app/chat/tools/`（conductor 只留编排，`build_chat_tools` 是装配壳）：
`screenplay.py`（项目与改编）/ `crew.py`（剧组）/ `video.py`（出片与素材）/ `memory_tools.py`（记忆与技能），
跨域小件（ToolContext、状态标签、文本截断）收在 `context.py`。分组（2026-11-01 由 26 个合到 19 个，此后随新增工具回到 22 个）：

| 组 | 工具 |
|---|---|
| 项目与改编 | `create_project` `map_novel` `propose_arcs` `generate_script` `run_adaptation` `ask` |
| 剧组与素材 | `breakdown_scenes` `generate_style_guide` `generate_video_prompts` `generate_reference_assets` |
| 出片 | `approve_video_prompts` `submit_videos` `rerun_video` `finalize_film` |
| 状态与记忆 | `get_status` `remember` `recall` |
| 技能 | `load_skill` |

**查询只有一个入口 `get_status(project_id, detail="summary|shots")`。** 原来的五个查询工具
（`get_script_overview` / `list_video_shots` / `get_video_jobs` / `check_finalize` /
`check_background_tasks`）已合并：它们查的是同一个项目的不同侧面，模型经常选错，选错后
就会一本正经地说瞎话（实测出现过「项目里根本没有镜头方案」，而同一个数据直接查得到）。

> 工具数量逼近 30–50 会开始出现「模型选不对工具」的退化（Anthropic 官方建议的阈值）。
> **新增能力前先想能不能加参数，而不是加工具**——`get_status` 的 `detail` 就是这么来的。
> 详见 [`DECISIONS.md` D-12](DECISIONS.md) / [D-14](DECISIONS.md)。

### 3.2 剧组 Agent（3 个）—— `app/crew/`

| Agent | 类 | 输入 → 输出 | 关键约束 |
|---|---|---|---|
| 🎬 **导演** Director | `DirectorAgent` | 剧本 → `SceneBreakdown[]`（分镜） | 一个分镜 = 一段连贯动作 **或** 一段连贯对话；`cut_reason` 必填；**画面调度五件套**必写（`camera.path`/`camera.height` 运镜轨迹与机位高度、带方向的光线、`spatial` 空间关系、`background_action` 背景人物、`action` 的接触动作物理过程），运动镜头缺轨迹会被连续性校验打回；镜头数由模型按内容决定（不写死） |
| 🎨 **美术指导** ArtDirector | `ArtDirectorAgent` | 剧本 → `StyleGuide` | **全片视觉锚的唯一来源**：角色造型 + 环境描述只产一次，下游逐字复用；`camera_style` 要带全片运镜语法与「拍摄呼吸感」，环境描述要写清关键光源方向与常驻背景人群 |
| 📹 **摄影指导** DP | `DPAgent` | 剧本 + 分镜 + 风格锚 → `Shot.video_prompt` | 只做组装：环境锚 + 出场人物锚 + 本镜画面调度 + 风格；写作守七条铁律（动作与运镜并重、保留呼吸感、光影写方向、人物编号与距离、背景人物各有各的事、只写看得见的东西、接触动作写物理过程）；提示词语言与目标视频模型方言对齐 |

三者都继承 `CrewAgent`，输出统一的 `CrewTaskResult`（结构化数据 + 摘要 + 错误列表），
并且都有**规则兜底**：LLM 不可用时返回可用的降级结果，而不是报错中断。

三者的外壳是同一件事，而且**每次都是全量**：

```
render_skill() 取 SKILL.md 全文 → 拼进 system prompt → llm.chat() 一次 → 解析 JSON → 校验 → 不合格带批评重做
```

所以它们**不是按需加载**，也不需要——它们一次只干一件事，没有「该不该加载这份知识」的判断。
技能在这里的作用是**让规则可编辑、可 diff、可单独评测**，不是省 token。
（主 Agent 才是按需加载：目录常驻 424 字符，正文靠 `load_skill` 拉。见 3.6。）


### 3.3 后台任务运行器 —— `app/agent/tasks.py`

剧组工具（导演拆解 / 美术指导 / 摄影指导 / 参考资产）都是耗时操作，统一由 `SubAgentRunner`
在后台线程执行，通过 `update_step()` 实时上报进度到前端右栏任务面板；任务起止快照落库
（`subagent_tasks` 表），重启后历史仍可查询。

### 3.4 不是 Agent 的部分（容易混淆，单独说明）

- **改编图节点（7 个）**：`context → plan → propose → guard → review → apply → finalize`。
  它们是 `app/agent/graph.py` 里的固定编排节点，不是自主 Agent——流程由代码决定，
  模型只在 `plan` / `propose` 两个节点里产出内容。
- `guard` 是自纠错闸门（硬校验 + LLM 五维评审），`review` 是 `interrupt` 人机协同点。
- **剧组的三个角色不是嵌套 Agent**：`CrewAgent.run()` 走的是 `llm.chat()` **一次**结构化调用，
  没有自己的工具集和上下文循环。按业界（Anthropic）定义它们更接近「领域技能」而非 subagent。
  详见 [`DECISIONS.md` D-04](DECISIONS.md)。
- 成片拼接（`app/video/assemble.py`，FFmpeg）与成片合成（`app/video/finalize.py`）也不是 Agent。
- **技能（Skill）是另一回事**：见 3.6。`app/agent/tasks.py` 是后台任务运行器
  （原来的 `skills.py`，2026-10-29 正名）。

### 3.5 记忆层 —— `app/memory.py`

| 机制 | 说明 |
|---|---|
| 三层用户记忆 | `global → project → conversation` 三种作用域，由 `remember` / `recall` 两个工具读写；按作用域注入系统提示 |
| 滚动摘要 | 超过阈值把旧消息压成 ≤300 字事实清单（存 `conversations.summary_json`），**替代「掐头丢弃」**，长对话不丢事实 |
| 审阅决策自动学习 | 用户在审阅卡片上的接受/拒绝决策会被提炼成偏好记忆 |

记忆是**按需加载**的：只有需要时才从 DB 读，不常驻上下文。

### 3.6 技能（Skill）—— `app/skills/`

长指令外置成 `SKILL.md`，代码只留装配逻辑。剧组三个角色的系统提示词
（合计约 2.2 万字符）和主 Agent 的四套工作流细则都在这里，不再硬编码在 `.py` 里。

按 **`audience`** 分两组（frontmatter 字段，写错会直接报错跳过）：

| 文件 | 受众 | 内容 | 谁用 |
|---|---|---|---|
| `director-breakdown/SKILL.md` | crew | 导演分镜方法论（三工作、细节律、五锚点、连续性计划） | `crew/director.py` |
| `video-prompt-zh/SKILL.md` | crew | 摄影指导写作规则（中文，十一条） | `crew/dp.py` |
| `video-prompt-en/SKILL.md` | crew | 同上（英文，目标模型方言） | `crew/dp.py` |
| `style-guide/SKILL.md` | crew | 美术指导方法论（视觉锚的唯一来源） | `crew/art_director.py` |
| `novel-serial/SKILL.md` | main | 长篇小说分段改编：建图 → 提弧 → 选段 → 连载续接 | 主 Agent 按需 |
| `crew-pipeline/SKILL.md` | main | 剧组四件事的依赖顺序与人工审阅边界 | 主 Agent 按需 |
| `production-delivery/SKILL.md` | main | 批准 → 提交 → 轮询 → 重跑 → 合成 | 主 Agent 按需 |
| `script-review/SKILL.md` | main | 改编提议的审阅闭环与事实约束 | 主 Agent 按需 |

两组的加载方式不同，**这才是「按需」与「全量」的分界线**：

| | 主 Agent（`audience: main`） | 剧组（`audience: crew`） |
|---|---|---|
| 谁决定加载 | 模型自己（看目录决定） | 代码按名写死 |
| 目录进上下文 | ✅ 424 字符常驻 | ❌ 人看不到目录 |
| 正文何时进 | 进入该领域时才 `load_skill` | 每次调用都全文拼进 system prompt |
| 省的是什么 | **token**（不用的规则不进上下文） | 省不了 token，省的是**改规则的摩擦** |
| 崩的方式 | 忘了 load → 凭印象办事 | 技能写错 → 三个角色同时跑偏 |

**渐进披露三级**：`skill_index()` 只给 name + description（很便宜）→ `render_skill(name)`
加载正文 → `skill_file(name, file)` 按需读附属文件。

**模板语法只有两种**：`{{var}}` 与 `{% if x %}…{% else %}…{% endif %}`。
不用 `str.format()`（提示词里有 JSON 示例，单花括号会被当占位符）；
不支持嵌套条件块。**缺变量直接报错**——静默留一个 `{{x}}` 进最终提示词比报错更糟。

改写作规则 → 直接改对应的 `SKILL.md`，不用动代码。

**这套拆开有没有用？已实测。** 用消融对比（把 `load_skill` 从工具集里摘掉，
看 Agent 行为退化多少）：5 条用例 × 3 次重复，约束落地率 **82%（有技能）vs 58%（无技能），
Δ +25%**。价值集中在带硬约束的地方——`crew-pipeline` 的依赖顺序一条就值 +60%；
反向用例（不该加载的请求）0% 差异，说明拆分没有引入过度触发。
方法与三个坑见 [`DECISIONS.md` D-30](DECISIONS.md)，可复跑：
`python -m evals.skill_eval --ablate --repeat 3`。

---

## 四、核心链路（端到端）

> 产品侧还有一条**一键入口**：`POST /projects/{id}/production`（对话工具 `start_production`）
> 按 剧本 → 拆解 → 风格 → 资产图 → 提示词 五阶段自动跑完，已完成的阶段自动复用。
> 下面画的是每一阶段内部的真实细节。

```
① 导入              小说 / 剧本文件（txt / md / docx / 粘贴）
  pipeline/importer.py    编码容错解析 → 原文 + 章节结构

② 生成初稿          pipeline/generation.py（两阶段，防退化）
  Stage 1  _stage_bible    故事圣经：人物 / 地点 / 主题 / 梗概
  Stage 2  _stage_scenes   按戏剧单元拆场（一地 + 连续时间 + 一个目标 + 一场冲突）
                           → Script：scenes（目的/冲突/入出状态）+ beats（动作/对白）
  ★ 容错层：模型 id 不合规 / 结构错位 / 多章塌成一场，都有兼容与退化护栏

③ 改编              app/agent/graph.py（人机协同闭环）
  context → plan(ReAct 查原文/查版本) → propose(结构化 patch)
          → guard(硬校验 + 五维评审，不合格回炉，上限 3 次)
          → review(interrupt：接受 / 编辑 / 重新生成 / 拒绝)
          → apply(落成新版本) → finalize

④ 视觉锚            crew/art_director.py
  StyleGuide：角色造型（character_appearances）+ 环境描述（environment_descriptions）

⑤ 参考资产          app/media/refs.py + app/llm/image.py + app/llm/vision.py
  定妆 prompt → 文生图 → 视觉质检（不合格重生成，≤2 次）→ 注册为全局参考
  → apply_reference_images() 按「场景→环境图 / 人物→角色图」回填到每个镜头

⑥ 分镜              crew/director.py
  SceneBreakdown[] + 连续性计划：reference_group（共享环境分组）、chain_from（首尾帧接力）
  画面调度：camera.path/height（镜头轨迹与机位高度）、带方向的光线、spatial、background_action、
  action 的接触动作物理过程（接触点 / 先后节拍 / 身体姿态 / 不穿模的让位关系）

⑦ 视频提示词        crew/dp.py → 视频工作台 Prompt 人审
  摄影指导提出 Shot.video_prompt 草稿：环境锚 + 出场人物锚 + 本镜画面调度 + 风格
  （锚逐字复用）。用户可以逐镜编辑、保存草稿、标记需修改或批准。
  每次人工操作创建新的 VideoVersion 快照；只有 prompt_status=approved 的镜头可提交。
  工作台支持多选镜头批量审批，版本列表展示 Prompt 审阅汇总，单镜可沿 parent_version_id 查看历史与差异。
  写作规则共十一条，正文在 `app/skills/video-prompt-zh/SKILL.md`（英文版 -en）：
  动作与运镜并重 / 呼吸感 / 光的方向 / 人物编号与距离 / 背景人物各有各的事 /
  只写看得见的东西 / 接触动作物理过程（一镜只给一个主事件）/ 禁止否定句 /
  台词不进提示词 / 以 final_frame 收尾 / 情绪翻译成身体线索并禁占位词
  （LLM 整段漏写运镜时用 camera.path 兜底补一次）

⑧ 视频生成          app/video/
  registry 选 Provider → queue 提交 + 轮询 + 状态回调写回 DB
  continuity.resolve_continuity() 已实现「reference_group / chain_from → reference_videos」
  （上一镜成片的公开直链作为下一镜的参考素材，无需额外图床）——
  单镜和批量投递都会再次校验 Prompt 是否已批准

⑨ 拼接成片          app/video/assemble.py（FFmpeg concat / filter_complex）

⑩ 成片合成          app/video/finalize.py（最后一公里，后台线程）
  收集版本内成功镜头（成片 URL 取各镜最近一次成功 video_job，不信任快照回填）
  → 下载 → 逐镜实测时长校准（provider 产出常与计划时长有偏差）
  → 拼装 → 配音（可选，TTS 实测时间轴）→ ASS 字幕烧录（可选）
  → 成片 + SRT 注册为项目素材（source="finalize"），前端经 /api/assets/file/{id} 播放
  降级阶梯：配音失败→静片+估算字幕；烧录失败→无字幕成片；单镜下载失败→跳过并警告
  致命只有两种：没有任何可用镜头、拼装失败
  入口三处：工作台成片卡片 / 对话工具 finalize_film / POST /projects/{id}/video/finalize
```

**两条贯穿全链路的原则**

1. **视觉锚只产一次，下游逐字复用**——这是跨镜一致性的第一道保障。
2. **AI 只提议，人做决定**——改编走 `interrupt` 四决策闭环；视频先审 Prompt，再审成片，所有操作都进入可回溯的链式快照。

---

## 五、技术债与已完成的整理

### 5.0 曾经的四个结构缺口（2026-11-01 真跑一条完整链路后确认，现已全部修复）

这四条当时不是「代码风格」，是**会让产品出错**的缺口。留在这里是因为它们揭示了
「看起来在跑」和「真的对」之间的差距——修复顺序也值得复用：先做实体层，再做校验，最后拆编排。

| # | 缺口 | 现在怎么表现的 | 修法 |
|---|---|---|---|
| 1 | ~~资产按名字做 key~~ | 角色锚、定妆图、音色、性别分存四个 dict，主键都是**中文名**。改一次名四份锚点全断且不报错 | ✅ **已修（见 5.2）**：统一按实体 id 索引，老数据读取期兼容 |
| 2 | **台词引用不校验真伪** | 实测 35 个引用里 28 个是模型编的 id（`beat_003_dlg_001`）。配音找不到台词就静默跳过，画面照常生成，两条线永不交汇 | ✅ 已修：prompt 补 beat id + 校验反向检查（见 DEVELOPMENT 5.22） |
| 3 | ~~没有服务端轮询器~~ | 出片进度靠前端轮询驱动（`/batch/advance`）。脱离前端跑批量，任务会停在 `submitted` 不动。这是「任务队列缺失」的真正形态——缺的不是 broker，是**驱动状态机的那只手** | ✅ **已修（见 5.3）**：sweeper 后台线程接管孤儿任务 |
| 4 | ~~`conductor.py` 1595 行~~ | 对话层同时干着：拼系统提示、读写 DB、算配音时间轴、拼提示词参数、管批次进度。每加一个功能都在同一个文件里打补丁 | ✅ **已修（见 5.4）**：工具实现按域拆进 `app/chat/tools/`，conductor 只剩编排（718 行）；随后内核也从 LangGraph 图换成手写循环（见 3.1 / D-29） |

> 1 和 2 有同一个根：**没有实体层**。搞完 1 之后，「台词归属、快照锁定」也有了落点
> ——`dialogue_ref` 指向的是 `beat_xxx`，而 beat 属于 `scene_xxx`，链路终于是一条而不是几张散表。

### 5.2 实体化：资产按 id 索引（2026-11-01 已做）

**病灶**：五份资产（外貌锚 / 环境锚 / 定妆图 / 音色 / 性别）全部以**中文名**做键。
名字是模型随手写的、也是用户随时会改的；改一次名，五份锚点同时失联——**而且不报错**，
查不到就当没有：定妆图不出、音色回落到推断、外貌锚消失。后来每镜都像换了个人，
却查不出是哪一步丢的。

**做法**（`app/media/assets.py`，一个模块一条约定）：

| 环节 | 用什么键 | 谁负责转换 |
|---|---|---|
| 模型产出 | **名字** | —— |
| 落库 | **id** | `art_director._parse_guide` 调 `rekey()` |
| 存储 | **id** | `{"char_004": …}` / `{"loc_001": …}` |
| 拼提示词 | **名字** | `as_name_map()`（视频模型不认识 `char_004`） |
| 老数据 | 名字（未迁移） | 读取时 `lookup()` / `rekey()` 两种都认 |

三条设计约束：

1. **不要求模型写 id**。让 LLM 输出 `char_004` 的准确率很低，写错就是静默丢锚点。
   模型照旧写名字，**格式由代码保证**。
2. **老数据不需要一次性迁移**。读取路径两种键都认；用户第一次改资产时
   `apply_library_patch` 顺手把整份表规整成 id 键（否则只迁移被改到的那个角色，
   改个名照样丢）。
3. **重名不猜**。两个角色同名时 `resolve()` 返回 None，键原样保留——
   猜错等于把 A 的脸贴到 B 身上，比没有更糟。

**代价（诚实记录）**：迁移之前写入的名字键，在角色**改过名之后**就再也对不上了
（没有改名历史，无从映射）。所以「改名不丢锚点」只对迁移之后的写入成立；
新项目从第一笔数据起就是 id 键，不受影响。

### 5.3 服务端轮询器：接管孤儿出片任务（2026-11-01 已做）

**病灶**：视频任务的轮询线程活在进程内存里。进程一重启，DB 里那条记录停在
`generating`，而没有任何人再去更新它——服务商那边其实早就出片了。用户看到
「一直生成中」；这个状态**没有出口**（不超时、不失败），手动重试还会重复提交、
重复计费。

**做法**（`app/video/sweeper.py`）：

- 扫描状态为 `submitting/queued/generating` 且带 `external_task_id` 的任务，
  走 `manager.attach()` 从**轮询**开始接管——绝不能 `create_job`，重提一次
  就是两条片子两份钱（`_poll_until_done` 从提交路径抽出，两条路共用）。
- 两类直接判失败并说明，不留着转圈：
  - 没有任务号 → 死在「提交」与「拿到任务号」之间，服务商侧没有这条任务；
  - 服务商不可用 → 接不了就明说，不要静默卡住。
- 触发：启动时扫一次（立刻接上重启前的活），之后周期巡检兜零星情况；
  幂等，内存里已在盯的跳过。配置项 `VIDEO_SWEEPER_ENABLED` / `_INTERVAL_SEC`。

**顺带修掉的退出挂死**：线程池线程是非守护线程，解释器退出时要 join——
不 `shutdown()` 的话 pytest 会「343 passed 之后命令行卡住」。现在
`VideoJobManager.shutdown()` + lifespan 关闭时 `shutdown_manager()`；
测试夹具用 `shutdown(wait=True)` 趁轮询间隔还是毫秒级把线程收干净
（monkeypatch 一撤销，间隔变回 5 秒，晚收就拖死退出）。



### 5.4 对话层拆分：工具实现出 `conductor.py`（2026-11-02 已做）

**病灶**：`conductor.py` 1595 行，系统提示、19 个工具实现、对话图、历史压缩、
编造拦截、SSE 流式全部在一个文件里。每加一个工具都要在同一个文件里打补丁，
改动半径无限大。

**做法**：

| 位置 | 放什么 |
|---|---|
| `app/chat/tools/context.py` | `ToolContext`（store / llm / collector / runner 一轮一份）+ 跨域取数助手（`project` / `latest_video_version` / `latest_job_by_shot`）+ 状态标签 + `short()` |
| `app/chat/tools/screenplay.py` | 剧本域：建项目 / 章节地图 / 选段 / 初稿 / 改编 / 咨询 |
| `app/chat/tools/crew.py` | 剧组域：拆解 / 风格指南 / 定妆图 / 提示词（都是「启动后台任务」） |
| `app/chat/tools/video.py` | 出片域：批准 / 提交 / 重跑 / 合成（查进度在状态域） |
| `app/chat/tools/memory_tools.py` | 轻件域：remember / recall / load_skill |
| `conductor.py`（745 行） | 只剩编排：系统提示、对话图、轮次服务、审阅确定性路径 |

两条拆分约束：

1. **工具仍是闭包工厂**：每个域一个 `build(ctx)`，返回绑定上下文的 `@tool`
   列表。不搞工具类、不搞注册表装饰器——19 个工具的装配就一屏，显式比聪明好。
2. **conductor 不准再 import 工具的实现细节**：它只经 `tools.build_chat_tools`
   拿工具列表。测试 `test_skills` 的 AST 扫描改为全目录扫 `app/chat/tools/`，
   防止有人把工具悄悄加回去绕过「技能 ↔ 工具名对齐」检查。

### 5.1 已解决

| 位置 | 原问题 | 处理 |
|---|---|---|
| `app/api.py` | 单文件 1400+ 行，业务逻辑写在路由里 | 拆成 `app/api/` 包（域模块 + common/schemas/deps）；路由表逐条比对无增删 |
| `app/chat/conductor.py` | 部分工具直接用 `store.session()` 操作 ORM，绕过 `Store` | 收敛为 `Store` 方法（`get/set_version_breakdown`、`get/set/merge_video_style`） |
| `app/agent/tasks.py` | 后台任务只在内存，进程重启即丢 | 起止两个时刻快照落库（`subagent_tasks` 表）；`/api/tasks` 合并内存态与历史态 |
| `app/api/video.py` | `cancel` 只改本地状态，未通知服务商 | 先尽力调用 provider 的 `cancel_job`，失败不影响本地状态收尾 |
| `app/api/system.py` | `/status` 用 openai/deepseek 三元表达式猜模型名（漏了智谱） | 改为读 `LLM.describe()` 的解析结果，另附视觉模型状态 |

仍有待处理：

| 位置 | 问题 | 影响 | 建议 |
|---|---|---|---|
| 全仓 | `ruff` 全量检查有 431 处存量（大头是 `E501` 长行 348 处、`I001` 导入序 39 处）——**2026-11-10 已把「致命集」进 CI**：`ruff check --select E9,F63,F7,F82,B`（语法错误、未定义名、bugbear）只拦真 bug，全量规则照旧红 | 全量进 CI 会红到没人愿意修；致命集先让「真 bug 会被拦住」这件事成立 | 存量债逐步清：`--fix` 可自动修 76 处（导入序/未用导入），长行需要人工判断拆法。`app/api/`、`app/llm/` 这两个新目录目前是全绿的 |
| `app/chat/` | 每轮对话重建 `AgentLoop` 与全部工具闭包 | 实测开销可忽略（纯对象装配，无 I/O；LLM 调用才是耗时大头） | 仅在压测显示瓶颈后再做，需要先把 collector 从闭包挪进显式状态 |
| `app/agent/tasks.py` | 进行中的任务只有内存态（历史已落库） | 重启后运行中的任务面板清空——这是真实情况，不假装还活着 | 若要多实例部署再接队列 |
| `app/api/` | 全部路由无鉴权，且默认可设置任意工作目录 | 仅限本机使用 | 已默认 `API_HOST=127.0.0.1`；若要做多用户需先加鉴权 |

本轮（2026-10-29）全链路体检修掉的「静默失败」：

| 位置 | 原问题 | 处理 |
|---|---|---|
| `app/main.py` | 只 `mount("/assets")`，`/favicon.svg`、`/brand/*` 全 404（只挂 `/` 与 `/assets`） | 显式 favicon 路由 + 存在才挂 `/brand` |
| `app/store.py` | `create_engine` 没传 `connect_timeout`，Postgres 没起时请求**永久挂住**且无日志 | `connect_args_for()` 按驱动给参数（Postgres 带 5s 超时，`DB_CONNECT_TIMEOUT` 可覆盖） |
| `app/main.py` | 数据库不可用返回 500 + 堆栈 | `OperationalError` → 503 + 说明怎么改 `DATABASE_URL` |
| `app/api/versions.py` | 导出文件名 `雨夜归人_vver_xxx.txt`（`version_id` 自带 `ver_`，又拼了个 `v`） | 去掉多余前缀；ASCII 退路纯中文时退成 `script.txt` |
| `frontend/src/components/WelcomeHero.vue` | 首屏底纹链第三方博客图床（已被 ORB 拦），设计想有的底纹从未出现 | 改本地 `public/brand/hero-texture.svg`，零外部依赖 |

> 静态资源托管是单进程三条路由：`/`（读 `dist/index.html`）、`/assets`（构建产物）、
> `/favicon.svg` + `/brand`（`public/` 原样拷贝的部分）。**新加 `public/` 下的目录时，
> 记得同步在 `main.py` 挂一个 mount**，否则只会在浏览器控制台里以 404 出现。


---

## 六、扩展速查

| 想做什么 | 改哪里 |
|---|---|
| 加一个模型厂商 | `app/llm/providers.py` 加一条 `ProviderSpec`；需要 `.env` 配置就同时在 `config.py` 与 `registry._from_settings()` 补字段 |
| 换首屏底纹 / 品牌图 | 放 `frontend/public/brand/`（构建时原样拷到 `dist/brand/`）；新目录要同时在 `app/main.py` 补一个 `mount` |
| 加一个剧组 Agent | `app/crew/` 新建继承 `CrewAgent` 的文件 → `crew/__init__.py` 导出 → 要在对话里触发就在 `app/chat/tools/crew.py`（或对应域文件）加 `@tool` 并挂进 `build()` 返回列表 |
| 加一个视频 Provider | `app/video/providers/` 新建继承 `VideoProvider` 的适配器 → `video/registry.py` 的 `_register_builtins()` 注册 → **同时清掉 5 处硬编码 `provider="minimax"`**（`chat/tools/video.py` 两处、`api/video.py` 两处、`crew/dp.py` 的 `_ZH_PROVIDERS`），再跑「Provider 体检」（见 DEVELOPMENT.md 5.1） |
| 加一个 API 路由 | 在 `app/api/` 对应域模块里加 `@router.xxx`；新域就新建文件并在 `api/__init__.py` 的列表里注册 |
| 加一个持久化字段 | `app/store.py` 加模型字段（新表由 `create_all` 自动建；给老表加列要在 `_ensure_columns()` 里补 ALTER） |
| 加一个生成阶段 | `app/pipeline/` 加模块，由 `agent/nodes.py` 或 `chat/tools/` 对应域模块调用 |
| 改剧组写作规则 | 直接改 `app/skills/<技能名>/SKILL.md`，**不用动代码**；改完跑 `python -m evals.run` 确认质量没退化 |
| 换对话内核 / 加 transport | 内核在 `app/chat/loop.py`；换 provider 只需重写 `_to_lc_messages()` 这一个适配层 |
| 加一把备用 key | 不用改代码：`.env` 里 `MINIMAX_API_KEY_FALLBACKS` 逗号分隔，轮换逻辑见 `app/keys.py` |
| 加一个技能 | 建 `app/skills/<name>/SKILL.md`（frontmatter 必填 `name` + `description` + `audience`），代码里 `render_skill(name, **vars)` 取正文 |
| 加一个查询能力 | **不要加新工具**——先看 `get_status` 的 `detail` 参数能不能扩。五个查询工具合并成一个就是因为模型会选错，选错后它会编结果 |
| 改完工具名 | 全仓搜一遍技能里的旧名；`tests/test_skills.py::test_skill_tool_refs_exist` 会拦住漏改（技能里提到的工具必须真实存在） |


