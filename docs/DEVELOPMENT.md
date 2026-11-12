# 开发文档 —— 问剧（Wenju）

> 面向继续开发本项目的人。除了架构与开发指引，重点记录**踩过的坑**：
> 每条都写清「症状 → 根因 → 解法 → 涉及文件」，避免重复踩。
>
> 本文档基于真实调试过程整理（2026-09 ~ 2026-10），所有结论都在本机实测验证过。
>
> **选型理由**（为什么用这个技术、不为什么用那个）见 [`DECISIONS.md`](DECISIONS.md)；
> **分层结构与核心链路**见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

---

## 一、架构总览（当前链路）

> 包结构与分层规则见 [`ARCHITECTURE.md`](ARCHITECTURE.md)；这里只画数据流。

```
小说原文
  │
  ├─ pipeline/generation.py 两阶段生成：故事圣经 → 场景规划（按戏剧单元拆场）
  │                  产出 Script：scenes（目的/冲突/入出状态）+ beats（动作/对白）
  │
  ├─ crew/art_director.py   美术指导 → StyleGuide
  │                  ★ 视觉锚的唯一来源：
  │                    character_appearances（按人物名）、environment_descriptions（按地点名）
  │
  ├─ media/refs.py          参考资产流水线（图像级一致性）
  │                  定妆图生成（llm/image.py，CogView）→ 视觉质检（llm/vision.py，DeepSeek 视觉）
  │                  → 注册进 StyleGuide.reference_images → 回填 Shot.reference_images
  │
  ├─ crew/director.py       导演 → SceneBreakdown[]（分镜）
  │                  分镜定义：一个分镜 = 一段连贯的角色动作，或一段连贯的角色对话剧情
  │                  输出连续性计划：reference_group（共享环境分组）、chain_from（首尾接力）、cut_reason
  │
  ├─ crew/dp.py             摄影指导 → 视频提示词草稿（模型感知 + 锚逐字复用）
  │                  ↓
  │   视频工作台              用户逐镜编辑 / 保存 / 标记需修改 / 批准
  │                  只做组装：环境锚 + 出场人物锚 + 本镜动作运镜 + 风格
  │
  ├─ video/providers/*      视频 Provider 适配层（runway/kling/cogvideo/sora/minimax）
  ├─ video/queue.py         异步任务队列（线程池 + 轮询 + 状态回调写回 DB）
  ├─ video/continuity.py    把 reference_group/chain_from 解析成 reference_videos
  ├─ video/finalize.py      成片合成编排（最后一公里）
  │                  收集版本内成功镜头 → 下载 → 逐镜实测时长校准 → 拼装
  │                  → 配音（可选）→ ASS 字幕烧录（可选）→ 成片+SRT 注册为项目素材
  └─ video/assemble.py      FFmpeg 拼接成片
```

**分层原则**：视觉锚只产一次（美术指导），下游逐字复用；LLM 提示词与目标模型方言对齐。

---

## 二、开发环境与启动

```bash
# 依赖（uv 按 .python-version 自动准备解释器，按 uv.lock 精确安装）
uv sync

# 启动后端（开发模式）
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端
cd frontend && npm install && npm run build   # 构建产物由 FastAPI 托管
```

**关键约定**

- `.env` 是唯一配置入口；改完**必须重启 uvicorn**（`Settings` 是 `lru_cache` 单例，热改不生效）。
- 本机若没有项目自带 Postgres，可用 SQLite 独立跑，不碰别人的容器：
  ```bash
  DATABASE_URL="sqlite:///./data/dev.db" CHECKPOINTER=memory uv run python -m uvicorn app.main:app --port 8000
  ```
- 跑测试：`DATABASE_URL="sqlite:///./data/ci.db" CHECKPOINTER=memory uv run pytest tests/ -q`

---

## 三、核心数据流

| 产物 | 定义位置 | 说明 |
|---|---|---|
| `Script` | `app/domain.py` | 剧本：characters / locations / scenes（含 beats） |
| `SceneBreakdown` + `Shot` | `app/domain.py` | 分镜方案；`Shot` 承载连续性字段与视频状态 |
| `StyleGuide` | `app/domain.py` | 视觉风格指南；**角色造型与环境描述是全局唯一来源** |
| 视频提示词 | `Shot.video_prompt` | DP 提出的草稿；批准后才会喂给视频模型 |
| `VideoJob` | `app/store.py` | 视频任务；状态由队列回调写回 DB |

**`Shot` 字段速查**

- 基础：`shot_type / camera / subject / action / duration_sec / lighting / mood`
- 切镜理由：`cut_reason`（写不出理由就该合并）
- 连续性计划：`reference_group`（共享环境参考分组）、`chain_from`（本镜首帧取自哪一镜尾帧）
- 运行时输入：`reference_images / reference_videos / first_frame_image / last_frame_image`
- 生成状态：`video_prompt / video_url / video_job_id`
- Prompt 审阅：`prompt_status`（`needs_review` / `needs_revision` / `approved`）、`prompt_review_note`

### 3.1 Prompt 人机协同

摄影指导的职责是提出结构化 Prompt 草稿，不负责替作者做最终出片决定。视频工作台把每个镜头分成三个状态：

- `needs_review`：模型刚生成，或用户保存了草稿，不能提交视频任务；
- `needs_revision`：用户认为仍需修改，保留备注并继续编辑；
- `approved`：用户确认当前 Prompt，单镜和批量生成才允许使用。

每次 Prompt 操作都会基于当前 `VideoVersion` 创建新的手动版本，使用 `parent_version_id` 串成版本链。这样 Prompt 文本、审阅备注和批准时点都可追溯，不需要再引入单独的审批表。

后端有两道门：单镜提交前校验当前镜头状态，批量生成只收集 `approved` 镜头。前端按钮状态只是体验层提示，不能替代后端校验。

批量审阅调用 `POST /api/projects/{project_id}/video-versions/{version_id}/prompts/review`，只改变选中镜头的审阅状态并创建新快照。版本列表返回 `prompt_summary`，镜头历史调用 `GET /api/projects/{project_id}/video-versions/{version_id}/shots/{shot_id}/prompt-history`，沿父版本链返回备注、状态和 unified diff。

---

## 四、扩展指南

### 4.1 新增一个视频 Provider

1. 在 `app/video/providers/` 新建适配器，继承 `VideoProvider`（`app/video/base.py`），实现
   `create_job / poll_job / cancel_job`，并声明能力类属性（`max_duration_sec`、`supported_resolutions`、
   `chinese_prompt_support` 等）。
2. 在 `app/video/registry.py` 的 `_register_builtins()` 注册。
3. 在 DB 注册配置（REST）：`POST /api/providers`，`kind="video"`，填 `name/label/base_url/api_key`。
4. **先跑「Provider 体检」再接入**（见 5.1 末尾），别直接进流水线。

### 4.2 新增一个剧组 Agent

1. 在 `app/crew/` 新建文件，继承 `CrewAgent`（`app/crew/base.py`），实现 `run()`。
2. 在 `app/crew/__init__.py` 导出。
3. 若要在对话里触发，在 `app/chat/tools/` 对应域文件（screenplay/crew/video/memory_tools）加 `@tool` 并挂进该域 `build()` 的返回列表。
4. 参考 `director.py`：LLM 输出必须做**枚举容错 + 范围钳制 + 有界重做**（见 5.2）。

### 4.3 新增一个 API 路由

路由按业务域拆在 `app/api/` 下（system / projects / versions / agent_runs / chat / video）。
新增同域路由直接加在对应文件；新开一域就新建文件并在 `app/api/__init__.py` 的 include 列表里注册。

两条约定：

- 取值一律走 `from . import deps` 再 `deps.store()`，不要 `from ..deps import store` 绑名字——
  测试里 `monkeypatch.setattr(app.api.deps, "store", ...)` 一处即可覆盖全部路由。
- 路由只做参数校验与序列化；业务放 `pipeline/`、`agent/`、`store`（共享辅助放 `api/common.py`）。

---

## 五、踩坑记录

### 5.1 视频 Provider API

#### （1）模型名会失效，别照抄旧值

- **症状**：提交视频任务报 `incorrect model param input`。
- **根因**：MiniMax 旧模型名 `MiniMax-Hailuo-01` 已无效；旧接口的 `MiniMax-Hailuo-02` 也属 v1。
- **解法**：用 V2 接口的 `MiniMax-H3`（`/v2/video_generation`）。改模型前先探测。
- **文件**：`app/video/providers/minimax.py`

#### （2）状态映射的子串陷阱：`"success" in "succeeded"` 是 False

- **症状**：任务在服务商侧早已成功，队列却一直轮询到超时（每任务白等 10–20 分钟）。
- **根因**：`succeeded` 中间是 `succee` 而非 `succes`，所以 `"success" in status` 永远不成立，成功态被映射成 `generating`。
- **解法**：用 `"succeed" in status_str`；状态值统一 `lower()`。**所有适配器都要检查这一点。**
- **文件**：`app/video/providers/minimax.py`

#### （3）查询响应可能把任务对象嵌套在下层

- **症状**：查询接口返回 200，但代码读不到 `status`。
- **根因**：MiniMax V2 的查询响应是 `{"task": {"status": ..., "content": {"url": ...}}}`，顶层没有 `status`。
- **解法**：`task = data.get("task") if isinstance(data.get("task"), dict) else data`，再从 `task` 取状态与 `content.url`。

#### （4）查询路径不是创建路径

- **症状**：`GET /videos/generations/{id}` 返回 **404**。
- **根因**：智谱（CogVideoX）异步任务的查询走 `/async-result/{task_id}`，不是创建路径 + id。
- **解法**：创建 `POST /videos/generations`，查询 `GET /async-result/{id}`。
- **文件**：`app/video/providers/cogvideo.py`

#### （5）状态字段名与结果字段名逐家不同

- **症状**：智谱任务永远 `unknown`，成功也拿不到视频地址。
- **根因**：字段是 `task_status`（值为大写 `PROCESSING/SUCCESS/FAIL`），结果在 `video_result`（**单数**）`[0].url`。
- **解法**：字段名与大小写都要按各家实际响应处理，别假设统一。

#### （6）域名/平台可能不通

- **MiniMax**：`api.minimax.cn` 与 `api.minimaxi.com` 可用；`api.minimax.io`（国际站）**同 key 不通**。
- **智谱**：`open.bigmodel.cn/api/paas/v4`，同一个 key 同时可调 **LLM（GLM）+ 文生图（CogView）+ 视频（CogVideoX）**。

#### （7）免费档与付费档的能力差异要写清楚

- `cogvideox-flash`：**免费**（`usage.total_tokens=0`），但输出约 5s、720p、**无音轨、带水印**。
- `cogvideox-2 / cogvideox-3`：付费档，质量更好（旗舰是 `-3`）。
- MiniMax H3：带音轨，但按 token 计费（实测 6s/768P ≈ **195,294 token**）。

#### （8）内容审核：直陈暴力会被拒

- **症状**：`POST /videos/generations` 返回 **400 Bad Request**。
- **根因**：提示词直写「头部撞击桌面」「脑组织」等触发审核。
- **解法**：改成**暗示不直陈**——「手掌按下后脑的瞬间镜头猛地甩开，只留众人表情」，即可通过。

#### （9）Provider 注册表会把无 key 的实例缓存下来

- **症状**：视频任务失败，错误是 `Illegal header value b'Bearer '`（key 为空）。
- **根因**：先有一次不带 key 的 `get_provider()`（如仅用于费用估算），实例被缓存；后续带 key 的调用直接命中缓存，key 永远进不去。
- **解法**：`registry.get_provider()` 在缓存实例缺 key 而调用方带 key 时就地补齐。
- **文件**：`app/video/registry.py`

#### （10）余额不足是硬失败

- **症状**：`402 Payment Required`，或查询里 `error.code=1008 / message=insufficient balance`。
- **说明**：任务直接失败，需要充值后重试。这类失败**不要与内容审核混淆**（前者无 task_id，后者有 task_id 但 status=failed）。

#### （11）接入新 Provider 前先体检

拿各家 key 探活，避免把「模型名错 / 端点错 / 字段错」带进流水线：

```python
# 1) 鉴权探测：故意给非法参数，能返回「参数错」= 鉴权通过
# 2) 模型探测：给不存在的模型名，返回「模型不存在」= 端点正确
# 3) 真跑一条最短生成，确认状态映射与结果字段
```

---

### 5.2 LLM 输出容错（最容易炸的一层）

> 原则：**永远不要假设 LLM 的输出结构**。一个不合规字段会让整次生成抛异常、静默回退到本地演示数据。
> `app/pipeline/generation.py` 里所有 `_xxxIn` 模型都是为容错而存在的。

| 症状 | 根因 | 解法 |
|---|---|---|
| 整次生成 `mode=local-fallback` | 模型给 `scene_01_wake_up` 这类 id，不符合 `^scene_[0-9]{3,}$` | 场景 id 不合规时**回退纯数字序号**并保证唯一 |
| 同上 | 模型把 `dialogue` 输出成 `{"line": "..."}` 对象 | 加 `field_validator(mode="before")` 解包成字符串 |
| Bible 解析失败（`Field required`） | 模型只给 `name` 不给 `id` | 输出模型里 `id` 设为可选，后续按序号补齐 |
| 整场戏没有节拍 | 模型用 `action`/`dialogue` 数组而非 `beats` | 加兼容层 `_synth_beats()`，`beats` 为空时由数组合成 |
| 导演整次失败 | `camera_type` 不在枚举内 | 枚举容错：别名映射 + 兜底默认（见 `_pick_enum`） |
| 导演整次失败 | `duration_sec=0.8` 或 `20` 超出 `Shot` 范围 | 解析时钳制到合法区间 |
| 接力全部错位 | 多场时模型给的是「场景内局部 order」 | 解析时**全局重编号**，并把 `chain_from` 从局部映射到全局 |
| 多章素材只生成 1 场戏 | 场景规划退化（把整段塞进一场） | 加**退化护栏** `_is_degenerate_plan()` + 带反馈重做 |

**涉及的枚举与范围**（`app/domain.py`）：

- `shot_type`：extreme_wide/wide/medium/close_up/extreme_close_up/over_shoulder/pov
- `camera.type`：static/pan/tilt/dolly/tracking/crane/handheld/zoom/steady
- `camera.speed`：slow/medium/fast；`pacing`：slow/medium/fast/variable
- `Shot.duration_sec`：1–30（但目标模型可行区间通常 4–15，见 5.5）

---

### 5.3 对话模型配置

#### （1）优先级链：改 key 要连上一级一起注释

`app/llm/registry.py` 的默认优先级链是 **OPENAI > ZHIPUAI > DEEPSEEK**。只想切到 DeepSeek，
必须先把 `ZHIPUAI_API_KEY` 注释掉（或置空），否则不生效。
更省事的做法是直接指定：`CHAT_PROVIDER=deepseek`（视觉/文生图同理，见 `VISION_PROVIDER` / `IMAGE_PROVIDER`），
显式指定会跳过整条优先级链。

#### （2）推理模型的 `reasoning_content` 陷阱

- **症状**：模型调用成功（HTTP 200），但 LangChain 拿到的 `content` 是空的。
- **根因**：`deepseek-v4-pro` 是**推理模型**，默认把内容写进 `reasoning_content`，`content` 留空；`max_tokens` 太小时全被思考链吃掉。
- **解法**：显式关闭思考 —— `DEEPSEEK_THINKING=false`，`app/llm/providers.py` 会发 `extra_body={"thinking": {"type": "disabled"}}`，直出 `content`。实测开启思考约慢 2.2 倍（1.37s vs 3.08s）。

#### （3）改 `.env` 必须重启服务

`Settings` 是 `lru_cache` 单例，热改不生效。

---

### 5.4 数据与 ID

#### `normalize_id` 的双前缀 bug

- **症状**：场景的 `location_id` 变成 `loc_loc_main`，与地点 `loc_main` 对不上；依赖地点名的查找（如环境锚）全部落空。
- **根因**：主值 slug 为空、回退到 `fallback` 时，fallback 自带的 `loc_` 前缀**没被剥掉**，又拼了一次前缀。
- **解法**：fallback 也先剥前缀（抽成 `_strip_prefix()` 复用）。
- **文件**：`app/domain.py`

#### 任务只在内存里会丢

视频任务与后台任务的状态若只在内存，进程重启即丢。两者现在都通过落库解决：
视频任务是「状态回调写回 DB」（`app/main.py` 的 `_wire_video_job_writeback`），
后台任务是「起止两个时刻各落库一次」（`app/agent/skills.py` 的 `SubAgentRunner._persist`
→ `subagent_tasks` 表，`/api/tasks` 合并内存态与历史态）。
注意：**进行中**的任务本身随线程终止，落库只保证已完成任务的历史可查，不假装它还在跑。

> 踩坑：测试里后台任务 runner 若不显式绑定测试库，会经由全局 `deps.store()` 去连 `.env`
> 里配置的开发库（本机是 Postgres），连接不可达时会把整个测试卡住。`tests/test_api.py`
> 的 client fixture 因此额外替换了 `app.api.deps.subagent_runner`。

---

### 5.5 视频一致性（提示词工程）

这是本项目最核心的领域经验。

#### （1）文生视频每个镜头是独立采样，只靠 prompt 无法跨镜一致

- **现象**：同一角色在不同镜头里长相/服装变化，场景也漂移（甚至漂成西方人）。
- **解法**（三层）：
  1. **视觉锚逐字复用**：环境描述与人物造型由美术指导产出一次，代码**逐字**拼进每个镜头（`app/crew/dp.py` 的 `_apply`）。不许每镜重新描述。
  2. **人物按镜过滤**：只列本镜实际出场的人，别把全场人物塞进每个镜头。
  3. **显式写「中国人 / 东亚面孔」**：否则模型默认漂成西方人。

#### （2）分镜切太碎 = 不连贯 + 更贵

- **错误做法**：真人影视的「覆盖式拍摄」思路（一人一切、正反打、谁说话切谁）。
- **正确做法**：**一个分镜 = 一段连贯的角色动作，或一段连贯的角色对话剧情**。具体执行：把该场节拍流按类型切成连续的「动作段」和「对话段」，每段一镜。
- **配套约束**：`cut_reason` 必填（写不出理由就合并）；禁止相邻镜头是同一主体的重复特写。

#### （3）镜头数不能盲目固定

- 既不能写死 8 镜，也不能写死 3 镜 —— **交给模型按内容判断**。
- 但必须给**约束 + 校验**：时长区间、切镜理由、分组与接力自洽（`director._validate_continuity`），不合格就带反馈重做。

#### （4）剧本层退化会毁掉下游

- **现象**：多章素材被塞进 1 场戏，下游导演只能被迫「一人一切」硬切。
- **解法**：上游强制「按戏剧单元拆场」（一场 = 同一地点 + 连续时间 + 一个戏剧目标 + 一场冲突），并加退化护栏。

#### （5）单镜时长必须落在模型可行区间

导演规划的时长要和目标模型对齐（如 CogVideoX 约 5s/条，MiniMax H3 支持 4–15s）。规划 2s / 20s 这种无法执行的时长会直接失败或被忽略。

#### （6）参考素材需要公开 URL

MiniMax / 智谱的参考图/参考视频都要公开 URL。**零托管技巧**：上一镜成片的 `video_url` 本身就是公开直链，可直接作为下一镜的 `reference_videos`（`app/video/continuity.py`）。

#### （7）账户余额要盯

MiniMax 按 token 计费（6s/768P ≈ 19.5 万 token/条）。反复「盲出片再返工」是最大的浪费——先做 previz/参考注册再出片。

---

### 5.6 编排与运行时
#### （1）对话式工具调用不可靠

- **症状**：用户/脚本让 Agent「生成风格指南」，模型回复"已启动任务 task_0001"，但**任务根本没创建**（它复述了历史任务号）。GLM 与 DeepSeek 都出现过。
- **判定方法**：不要相信回复文本里的任务号，要用**任务差集**（`GET /api/tasks` 前后对比）确认真实新任务。
- **建议**：确定性流水线直接调用 `run_director / run_art_director / run_dp`，不要绕道 LLM 选工具。

#### （2）`store` 参数遮蔽模块名

- **症状**：`'Store' object has no attribute 'Project'`。
- **根因**：函数参数 `store`（Store 实例）遮蔽了模块名，`store.Project` 解析成实例属性。
- **解法**：模型类显式导入：`from .store import Project`。

#### （3）API 只落库、不投递

- **症状**：视频任务永远 `pending`。
- **根因**：创建任务的接口只写了 DB，没有提交到异步队列；`retry` 也只重置状态、不重投。
- **解法**：创建/重试都调用统一的投递函数，状态由队列回调写回 DB。

---

### 5.7 本机环境

| 坑 | 现象 | 解法 |
|---|---|---|
| 系统代理劫持 localhost | 本机请求超时 / 被代理吞掉 | httpx 用 `trust_env=False` |
| 未安装 ffmpeg | 拼接报「FFmpeg 未找到」 | `pip install imageio-ffmpeg`，取其二进制放进 PATH |
| 文本文件是 GBK | `UnicodeDecodeError` | 按 `utf-8 → gb18030 → utf-16` 顺序尝试解码 |
| Windows 下 curl 传中文 JSON | `invalid unicode code point` | 改用 Python httpx 发请求 |
| Postgres 容器角色不存在 | `role "script" does not exist` | 本次开发用 SQLite（`DATABASE_URL=sqlite:///...`） |
| 轮询上限太低 | 高峰期任务被判超时 | 提高 `MAX_POLLS`（服务商侧排队可能超过 10 分钟） |

---

### 5.8 参考资产层（图像级一致性）

> 这是「工业级思路」的第一阶段：产角色/场景定妆图 → 视觉质检 → 注册为全局参考 →
> 回填到镜头。相关代码：`app/llm/image.py`（文生图）、`app/media/refs.py`（流水线）。

**为什么需要它**：文字锚只能「减少」跨镜漂移；把同一个角色的**图**作为参考喂给视频模型，
模型是「看见」同一个人，强度高一个量级。

**流程**：定妆 prompt → 文生图 → 视觉质检 →（不合格重生成，最多 N 次）→ 注册 → 回填镜头。

#### 文生图（智谱 CogView）的坑

- 端点是**同步**返回：`POST /images/generations` → `{"data":[{"url": "..."}]}`，直接取 `data[0].url`（不像视频是异步轮询）。
- 与 LLM / CogVideoX **共用同一个智谱 key**；key 存在 DB `api_providers` 里 `kind="image"` 的行（`resolve_image_key` 优先 DB，再回落环境变量/Settings）。
- 免费档 `cogvideox-3-flash`（文生图是 `cogview-3-flash`）出图**带平台水印**、**URL 有有效期（约 7 天）**；付费档质量更好。

#### 视觉质检（DeepSeek 视觉模型）的坑

- **图片是服务端拉取**：`image_url` 必须当时可达，否则报 `Failed to download image`。图片 URL 失效/被回收就会失败——**base64 data URI 也支持，更稳**（下载后转 `data:image/png;base64,...`）。
- **输出在 `reasoning_content`**：该模型是推理模型，`content` 可能是空的，要兼容读 `reasoning_content`（和对话模型同一个坑）。
- **判定标准必须「只对致命问题 FAIL」**：早期把「有无水印」「家具数量是否一致」也算 FAIL，结果**永远不合格、重试白跑**（平台水印是免费档必带的）。正确做法是只对「画面主体类型错 / 人物不对」这类致命问题判 FAIL，其余作为提示。
- **要处理角色自身设定**：戴面具/头盔的角色不能要求「东亚面孔」——否则必然 FAIL。质检标准里要写明例外。

#### 回填到镜头

- `apply_reference_images()` 按「场景 → 环境图、人物 → 角色图」把注册表映射进 `Shot.reference_images`（上限 9）。
- 参考注册表存在 `StyleGuide.reference_images`（`{名称: 图片URL}`），随风格指南一起持久化在 `projects.video_style_json` —— **不需要改数据库表**。

#### 一致性效果（已实测）与两个限制

- **效果**：把同一张角色定妆图作为**首帧**（图生视频）喂给 3 个不同动作的镜头，用视觉模型判定：t=0.2s 与 t=3.0s 均 **PASS**——三镜被判定为同一人物（连「颈部右侧的痣」都一致）。
- **限制 1：水印会传播**。智谱 CogView **各档（含付费 cogview-4）都带水印**，参考图的水印会出现在生成视频里。生产环境需要换无水印图源（自建开源模型 / 其他图床级服务）。
- **限制 2：智谱 CogVideoX 只有「首帧图」没有「参考图」**。真正的 reference_image（多主体参考）目前只有 MiniMax H3 支持；用首帧做的锁定其实更强（首帧就是那个角色），但灵活度较低。

---

### 5.9 成片合成与字幕（2026-10 最后一公里）

> 相关代码：`app/video/finalize.py`（合成编排）· `app/media/dubbing.py`（配音）· `app/media/subtitles.py`（字幕）

#### （1）双 deps 单例陷阱：根 `deps.py` 加了还不够

- **症状**：路由运行时 `AttributeError: module 'app.api.deps' has no attribute 'finalize_manager'`，
  但**导入期完全正常**（`import app.main` 不报错）。
- **根因**：项目有**两个** deps 模块——
  - `app/deps.py`：全项目唯一装配点，真正创建单例（`@lru_cache`）。
  - `app/api/deps.py`：API 层的晚绑定入口，**路由只认这个**。
  新增单例只在根 deps 加，API 层拿不到。
- **解法**：两个地方都要加（`app/deps.py` 建、`app/api/deps.py` 再导出）。
- **⚠️ 检测方法**：这类错误**只有发真实请求才会暴露**。单元测试用 `monkeypatch` 直接替换依赖，
  绕过了这条路径。**新加单例后必须用 TestClient 打一次真实 HTTP 请求冒烟**，
  只跑 `pytest` 是不够的。
- **文件**：`app/deps.py` · `app/api/deps.py`

#### （2）`.env` 只进了 Settings，没进 `os.environ`

- **症状**：视频能出片（key 走数据库 `api_providers`），但配音永远失败，报 key 缺失。
- **根因**：pydantic-settings 只把 `.env` 的值读进 `Settings` 对象，**不会导出到进程环境变量**。
  而 TTS 的回落链是 `resolve_tts() → os.environ["MINIMAX_API_KEY"]` 直接读环境变量——链是断的。
- **解法**：`app/main.py` 顶部加 `load_dotenv()`，进程启动时先落进环境变量。
  注意这与「改 `.env` 必须重启」是同一条约束的两面（见 5.3(3)）。
- **文件**：`app/main.py` · `app/llm/tts.py`

#### （3）provider 实际时长 ≠ 计划时长，时间轴会越往后漂越大

- **现象**：字幕和配音整体后移，末尾明显对不上画面。
- **根因**：v6 实测——计划 6+8+6=20s，成片实测 **21.2s**。按计划时长排配音/字幕，
  每一处偏差都往后累积。
- **解法**：`finalize` 在**下载之后**逐镜实测真实时长，用 `model_copy(update={"duration_sec": ...})`
  替换镜头时长，配音排程与字幕估算都用校准后的镜头。
- **文件**：`app/video/finalize.py::finalize_film`

#### （4）先测量，后 move

- **症状**：注册成片素材后拿到的时长是 0。
- **根因**：`_register_asset()` 会把文件 **move** 进素材目录，move 之后原路径不存在，
  再去量就量到空文件。
- **解法**：**时长必须在 move 之前量**。顺序反了就是 0。
- **文件**：`app/video/finalize.py`

#### （5）便携版 ffmpeg 没有 ffprobe

- **症状**：成片时长恒为 0；质检抽的 5 帧全挤在片头 8 秒内。
- **根因**：本机 `imageio-ffmpeg` 提供的便携二进制**只有 `ffmpeg.exe`，没有 `ffprobe.exe`**。
- **解法**：统一的三级降级链——**ffprobe → 解析 `ffmpeg -i` 的 stderr `Duration:` 行 → 返回 0 由调用方兜底**。
  `finalize._media_duration_sec` 与 `qa._duration` 两处都要有。
- **文件**：`app/video/finalize.py` · `app/video/qa.py`

#### （6）ASS 字幕烧录：Windows 盘符要转义

- **解法**：字幕滤镜的路径里的盘符冒号会被 FFmpeg 误解析，要写成 `C\:/...` 并用单引号包裹整个路径。
  用 `subtitles` 滤镜**单次重编码**，不要分两步（先烧字再转码，画质二次损失）。

#### （7）降级阶梯要写死，别让一步失败毁掉整条链路

- 配音失败 → 交**静片 + 估算字幕**；
- 烧录失败 → 交**无字幕成片**；
- 单镜下载失败 → **跳过并警告**，其余镜头照常合成。
- 真正致命的只有两种：**没有任何可用镜头**、**拼装失败**。

---

### 5.10 对话流式输出（2026-10）

#### （1）`ChatOpenAI` 不开 `streaming`，前端就没有流式

- **症状**：回复（含思考内容）整轮结束后一次性到达，前端完全没有流式观感。
- **根因**：`build_chat_model()` 的默认参数里没有 `streaming=True`。
- **解法**：`app/llm/providers.py::build_chat_model()` 统一带上 `"streaming": True`。

#### （2）`AIMessageChunk.type` 返回 `'AIMessageChunk'`，不是 `'ai'`

- **症状**：流式 token 一个都不出来。
- **根因**：代码写的是 `if getattr(msg_chunk, "type", "") != "ai": continue`，
  但 LangChain 的 `AIMessageChunk.type` 实际返回 **`'AIMessageChunk'`**，所有块都被过滤掉了。
- **解法**：**用 `isinstance` 判断**，不要比较 `type` 字符串：
  ```python
  if not isinstance(msg_chunk, AIMessage):
      continue
  ```
  字符串比较是这里最容易犯的错——它不报错，只是静默丢光。

#### （3）长篇建图要防重复

- **症状**：同一本书被反复建章节地图，每次几分钟，token 白烧。
- **解法**：`map_novel` 先查 `project.novel_map` 里已有章节数，已有则直接拒绝并提示用户
  「直接说挑一段改编」；只有用户明确要求重建时传 `rebuild=true`。

---

### 5.11 长篇改编与「模型编造」（2026-10）

#### （1）一次生成有容量上限，选段要按容量切

- **现象**：用户选了 3 章的区间，生成结果只覆盖了选段**开头一小段**。
- **根因**：单次生成的原文容量约 **1.2 万字**（`novel_map.ARC_CHAR_BUDGET = 12000`），
  超过就截断。注意这与「触发长篇提示」的阈值不是同一个数——
  后者是 `chat/tools/context.py` 的 `LONG_TEXT_CHARS = 1200 × 12 = 14400`（约 1.44 万字）。
- **解法**：有章节地图时，不传 `to_chapter` 就按 `next_to_chapter()` 按容量自动续接；
  选段超容量时在回复里**明确告知**「初稿只覆盖了选段开头，建议切到 2 章左右」。

#### （2）模型会在没调工具的情况下编造结果（deepseek 实测）

- **症状**：模型回复「已生成初稿版本 ver_xxx」，但**根本没有这个版本**——它照抄了历史消息里的
  轨迹格式，凭对话记忆编了一份结果。
- **根因**：上下文里存在「［本轮动作］生成初稿：已生成初稿版本 ver_xxx…」这类历史文本，
  模型把它当成了格式模板。
- **解法**：**确定性护栏**——`app/chat/conductor.py` 的 `_CLAIM_PATTERNS` 匹配「已生成初稿 / 已改编到第 /
  章节地图已建成 / 个候选故事弧 / 已提交出片 / 已批准」等结果短语；若本轮**没有任何真实工具返回**，
  就追加 `_CORRECTION_PROMPT` 让模型重跑一轮。回复末尾再追加 `_FABRICATED_NOTICE` 说明是误报。
- **同类问题**见 5.6(1)：**不要相信回复文本里的任务号**，用任务差集确认真实新任务。

#### （3）摘要与剧本的输出容错

- 解法同 5.2 的思路：摘要/剧本的模型输出都要容错解析，**不能假设结构**。

---

### 5.12 依赖装配与单例（2026-10）

| 坑 | 现象 | 解法 |
|---|---|---|
| 双 deps 模块 | 路由运行时才报 `AttributeError`，导入期无感 | 根 `deps.py` 与 `api/deps.py` 两处都加；**必须用 TestClient 发真实请求冒烟**（见 5.9(1)） |
| `load_dotenv` 缺失 | 配了 `.env` 但 `os.environ` 为空，配音链断 | `app/main.py` 顶部 `load_dotenv()`（见 5.9(2)） |
| 路由直接绑依赖名 | 测试无法整体替换 | 路由一律 `from . import deps` 再 `deps.store()`（见 `DEVELOPMENT.md` 4.3） |
| `Settings` 是 `lru_cache` 单例 | 改 `.env` 不重启不生效 | 重启；或改完调 `reset_llm()` 热重建模型层 |

---

### 5.13 提示词质量 eval（2026-10）

> 详细说明见 [`../evals/README.md`](../evals/README.md)；选型理由见 [`DECISIONS.md`](DECISIONS.md) D-13。

#### （1）确定性断言最危险的失败模式是**误报**，不是漏报

- **现象**：审计器对**真实链路产出的提示词 4/4 全误报**「疑似一镜多运镜」，
  每个镜头的 `prompt_audit` 都挂着假告警，用户审阅时被反复干扰。
- **根因**：两处叠加——
  1. 提示词里带**全片风格段**（美术指导的 `camera_style`，描述整部片的运镜语法），
     每个镜头都带一遍。统计运镜时没排除它，等于把全片语法算成本镜的运镜。
  2. 运镜动词**没有镜头语境要求**：「在靠门地面拉出一道冷光带」里的「拉出」被当成 pull-out。
- **解法**：新增 `find_camera_moves()`（要求动词附近有镜头/摄影机/机位等语境词）
  与 `strip_style_block()`（剥掉全片风格段，容忍末尾标点从「。」变「，」），
  `audit_video_prompt()` 改为用这两者，并接受 `style_context` 参数。
- **调用点**：dp.py 传 `style`；`app/api/video.py` 传 `current.style_guide.camera_style`
  （新增 `_style_context()` 辅助）；`app/chat/conductor.py` 同样传。
- **⚠️ 教训**：断言库一定要有**录制真实产出**的离线回归层（`evals/fixtures/`）。
  只用手写的坏例子测断言库，测不出误报。

#### （2）LLM 输出不确定，回归信号会被抖动淹没

- **现象**：同一份代码两次跑出的分镜与提示词并不相同；默认温度 0.4 下差异更大。
- **根因**：`ModelConfig.temperature` 默认 0.4。采样带来的随机性会让「这次改动是不是退化了」
  这个问题无法回答。
- **解法**：
  1. eval 专用模型把温度钉到 **0**（贪心解码）——为此给 `LLM.__init__` 与 `build_llm`
     加了 `temperature` 覆盖参数（生产链路不受影响）。
  2. 出现 critical 时**自动复跑一次**，两次都失败才算稳定缺陷。
- **注意**：温度 0 也不保证逐字一致（远端模型有别的随机源），所以复跑机制不能省。

#### （3）模型余额耗尽不是质量回归

- **现象**：eval 跑着跑着出现 `402 Insufficient Balance`（智谱是 `429 code 1113`），
  case 全部失败。
- **根因**：把基础设施故障当成质量失败，报告里一片红，实际是没跑成。
- **解法**：`EvalRunner` 识别余额/限流/鉴权/连接类错误，标记为 **blocked**——
  **不计入失败**，但在报告里显式列出，并让 `python -m evals.run` 返回退出码 2。
  「跑不动」不能被误读成「跑过了且通过」。

#### （4）锚复用判定要看整体，不能只看开头几个字

- **现象**：`appear[:16] not in prompt` 判成「没复用人物锚」，但锚其实**完整地**在提示词里。
- **根因**：DP 偶尔会重写锚里的个别词，只比对前 16 字会把「轻微改写」误判成「完全没复用」。
- **解法**：改成**整段锚是否原样出现** + `difflib` 相似度分级——
  原样出现=通过；相似度 ≥85% 但非原样=warn（轻微改写）；<85%=critical（没复用）。

#### （5）eval 找出的提示词缺陷：否定句

- **现象**：judge 与确定性层同时报出提示词里有否定式描述——
  「眼睛没有看任何地方」「避免均匀平光」「低头快走掠过，没有停留」。
- **根因**：视频模型会把被否定的内容**照画出来**（这是提示词工程的常识，
  但写作规则里虽然写了「禁止否定句」，模型并不总是遵守；且美术指导的风格段里也会冒出来）。
- **解法**：在美术指导与摄影指导的写作规则里补强——
  ① 美术指导新增第 6 条：五样产出（会被**逐字复制进每个镜头**）一律不写否定句，
  给出正向改写示例；② 摄影指导第 8 条补上两类高发写法的改写示例。
- **实测**：10 条 case 的 DPT-04 从 8 条 critical 降到 **0 条**。

#### （6）全片风格段逐字贴进每个镜头 = 每个镜头都收到第二套矛盾指令

- **现象**：judge 的「克制度」维度只有 **44.9 分**（五维最低），其他四维都在 70-82。
- **根因（走了两次弯路才找准）**：`lighting_style` + `camera_style` + 色调 被整段贴在
  每一个镜头的末尾，而**镜头 body 自己已经写了光影和运镜**。于是同一段提示词里出现两套
  互相矛盾的规格，视频模型只能随机挑一套执行。judge 的实际投诉：
  - 「镜头固定不动」，末尾又被贴上「保留细微的手持浮动」「偶尔略高俯看」；
  - 「冷绿日光灯为主光」，末尾又贴「暖黄灯为主光」；
  - 分镜写「轻微仰拍」，末尾写「与视线齐平」「略高俯看」。
- **第一次修错了方向**：以为只是「运镜动词列举害的」，就去剥运镜子句——
  judge 只从 44.9 升到 47.0，基本没动。**证据在 judge 的 evidence 字段里，不在猜测里。**
- **真正的解法：风格段「只补空，不覆盖」**（`DPAgent._style_text(style_guide, body=...)`）：
  - body 已经写了光影 → 不贴 `lighting_style`；
  - body 已经写了摄影机/运镜 → 不贴 `camera_style`；
  - 色调永远贴（配色不与单镜描述冲突，且是跨镜统一的必要信息）。
  认知转变：**风格指南是给「写这个镜头的人」看的设计依据，不是要逐字贴给视频模型的文本。**
- **配套**：
  1. 美术指导的 `camera_style` 只写「不与具体镜头冲突」的风格身份，禁止列举具体运镜（给正反例）；
  2. `dramaturgy.strip_camera_moves()` 兜底剥离运镜子句（`camera_style` 要贴时用）；
  3. 摄影指导规则补「本镜运镜只写 camera_path 指定的那一种」。
- **实测**（10 条 golden case，同一套 case，deepseek-flash 温度 0）：

  | 维度 | 修复前 | 修复后 | 变化 |
  |---|---|---|---|
  | 克制度 restraint | 47.0 | **72.9** | **+25.9** |
  | 一致性 consistency | 62.6 | **78.1** | **+15.5** |
  | 可渲染性 renderable | 81.6 | 85.8 | +4.2 |
  | 戏剧性 dramatic | 80.2 | 80.0 | −0.2 |
  | 具体度 specificity | 84.3 | 84.5 | +0.2 |
  | **总分** | **72.9** | **81.0** | **+8.1** |

  确定性层 critical **0 条**、warn 1 条（残留一条「疑似一镜多运镜」提醒）。

- **测试**：`tests/test_crew_video.py` 锁住两条不变量——
  组装后提示词里的运镜动词不超过一种；body 已写光影/运镜时风格段不得重复贴。

#### （7）MiniMax 的 `/chat/completions` 不支持 `response_format=json_object`

- **症状**：结构化输出报 `2013 unknown response_format type`。
- **根因**：MiniMax 的标准 OpenAI 兼容路径能正常对话（`/v1/chat/completions` 可用），
  但**不认 `response_format`**。
- **解法**：`ProviderSpec` 加 `supports_json_mode` 字段；置 `False` 的厂商，
  `LLM.structured()` 自动降级为「提示词要求 JSON + 容错解析 + 失败重采样」
  （`client._JsonPromptStructured`）。
- **附带价值**：MiniMax 因此成为**一把 key 打通对话 / 视频（H3）/ 配音（T2A）**的选项。

#### （8）余额耗尽时三家一起倒，逼出了「blocked」这个状态

- **现象**：DeepSeek `402 Insufficient Balance`、智谱 `429 code 1113`、MiniMax `402 (1008)`。
- **根因**：`.env` 里的 key 都指向已耗尽的账户；`llm.available` 仍然是 True
  （配置存在 ≠ 能调用），所以 CI 会以「质量回归」的假象失败。
- **解法**：
  1. `EvalRunner` 识别余额 / 限流 / 鉴权 / 连接类错误 → 标记 **blocked**，不算质量失败；
  2. `evals/test_eval.py` 的 `eval_llm` 夹具**先探活一次**（`probe_llm`），探不通就 skip；
  3. `python -m evals.run` 遇 blocked 返回退出码 **2**（环境问题）而不是 1（质量失败）。
- **教训**：**「跑不动」必须和「跑过了且没通过」严格区分**。否则每次余额告急，
  质量信号就被基础设施噪音淹没。

#### （9）否定句靠提示词管不住，要有界修复兜底

- **现象**：写作规则第 8 条明写「禁止否定句」、还加了「交稿前逐句自查」，
  但概率模型仍会漏——10 条 case 里约 1 条稳定出现「视线扫过空椅子**但没有停留**」。
- **根因**：**负面约束对概率模型天然不可靠**。规则能降低发生率，降不到零。
- **解法**：`DPAgent._repair_negations()` —— 装配完成后检查哪些镜头的提示词仍含否定词，
  **只把这些问题镜头的 body** 送给模型要求改写成正向，然后重新装配。
  - **只修 1 轮**：修不掉就交给逐镜审计提醒，不开无限循环；
  - **只送有问题的镜头**，无关镜头不重写（省 token，也避免把好的改坏）；
  - **不采纳仍含否定词的改写**，免得来回改更糟；
  - **修复失败不连累结果**：模型不配合时保留原稿，只记一条 warning。
- **实测**：`object_carry` 从「两次复跑都 critical」变成连跑三次干净。

#### （10）技能模板不能用 `str.format()`

- **现象（预防性）**：写作规则里全是 JSON 示例——`{"scene_id": "scene_001"}`。
- **根因**：`str.format()` 会把**所有**单花括号当占位符，`{"scene_id"...}` 直接抛
  `KeyError: '"scene_id"'`。
- **解法**：模板只认双花括号 `{{var}}`，用正则替换，单花括号原样保留。
  条件块也只做一层（`{% if x %}…{% else %}…{% endif %}`），不支持嵌套。
- **另一半**：**缺变量必须报错**。静默留一个 `{{x}}` 进最终提示词，
  比运行时报错更糟——那会让模型看到一段字面量的花括号。
- **文件**：`app/skills/loader.py`

#### （11）有角色出场却没带外观锚

- **现象**：eval 报 `DPT-10 没有复用人物锚「徐姐」的造型（相似度 14%）`，
  两次复跑都在，是稳定缺陷。
- **根因**：摄影指导为每个镜头回一个 `characters` 列表，`_apply()` 按这个列表贴人物锚。
  模型偶尔漏报出场人物——body 里明明写了「徐姐横臂挡在楼梯口」，列表里却只有「马三」。
  于是这个角色在画面里露脸却没有外观锚，跨镜必然长相漂移。
- **解法**：`_apply()` 加**确定性补漏**——body 里点名出现的本场人物，即使 LLM 列表漏了，
  也把锚补进去。原则「视觉锚只产一次、下游逐字复用」不能靠模型自觉。
- **反向约束**：没在 body 里点名的角色**不许**塞进来（避免每个镜头都带全员锚，
  既浪费预算又让模型分不清主次）。
- **测试**：`test_dp_backfills_anchor_for_named_but_unlisted_character` +
  `test_dp_does_not_add_anchor_for_absent_character`。

---

### 5.14 主 Agent 按需加载技能（2026-10）

#### （1）把触发器一起搬进技能，模型就"不会做事了"

- **现象（预判，已避开）**：D-12 之后第一反应是"主 Agent 的 15 条规则也搬进技能"。
- **为什么错**：那 15 条是**路由信息**（用户说"出片"该调哪个工具），每一轮都要用。
  搬进技能 = 每轮多一次工具往返，而且**模型一旦没加载就等于不会做事**——
  这不是省 token，是把可靠性押在"模型每次都记得加载"上。
- **正确分界**：**"该做什么"留在提示词，"怎么做 / 什么顺序 / 什么会失败"进技能。**
  提示词因此只从 1890 字降到 1748 字（还多了 4 行技能目录）——**这次改造的收益不是变短，
  而是细则终于可写、可维护、可单测。**

#### （2）"进入领域前先加载技能"是句废话，得写成"按动作"

- **现象**：软要求版本实测"帮我出片吧" → **0/3 加载技能**，模型只调了 `list_video_shots`。
  它把"进入领域"理解成了"先查状态"，而不是"要动手了"。
- **解法**：按**动作**规定顺序——「本轮只要要动手（生成/改编/拆解/提交/批准/合成），
  第一个工具调用就该是 `load_skill`；只读盘点（看进度、看待批）可以先查，动手前补上」。
- **实测**：改完 3/3 稳定加载；「拆好了吗」3/3 不加载（纯盘点，不该浪费一次往返）。
- **教训**：**软要求写不进概率模型的行为里。要么给顺序，要么给反例。**

#### （3）`audience` 写错会「技能存在，但永远不被加载」

- **现象（预防性）**：frontmatter 里 `audience: mian` 拼错。
- **后果**：技能既进不了主 Agent 的目录、又混进剧组目录，表现为"文件明明在、
  模型从来不用"，这种静默失效最难查。
- **解法**：`_parse()` 里非法受众直接 `ValueError`，由既有逻辑 log.error 跳过该技能；
  测试 `test_unknown_audience_is_loud` 锁住这条。
- **顺带的边界**：主 Agent 的目录里**不许出现剧组技能**。主 Agent 不执行分镜，
  列进去既是白付 token，也会诱导它越位去"自己写分镜"——**目录本身就是一种授权**。

#### （4）`load_skill` 传错名字：返回文本，不要抛异常

- **原因**：工具抛异常会在当轮消息流里插一条错误，模型连"改口换名字"的机会都没有。
- **解法**：未知技能返回 `没有这个技能：xxx。可加载：a、b、c`——把可用清单直接给它。
- **测试**：`test_load_skill_rejects_crew_skill_and_typos`（含拼错、空串、剧组技能三种）。

#### （5）提示词是代码，要有预算和护栏测试

改造提示词最容易的失误是**漏掉一条流程**或**丢掉一条铁律**。三条测试兜住：

- `test_every_trigger_points_at_a_real_tool`：14 条触发器逐条断言"这一行指向真工具"，
  工具改名/删掉会直接红；
- `test_system_prompt_keeps_hard_rules`：事实约束、不替用户决定、MiniMax BYOK 不许丢；
- `test_system_prompt_stays_within_budget`：<2400 字，防止以后有人往提示词里堆细则。

外加 `test_main_skills_related_tools_exist`：每个技能末尾"相关工具"点名的必须是真实工具，
防止代码改名后技能文档悄悄失真。

### 5.15 三个「静默失败」（2026-10 全链路体检）

这一轮把服务真跑起来、用无头浏览器点了一遍界面，抓到的问题都属于同一类：
**不报错、不崩溃，只是安静地坏着**。

#### （1）静态资源只挂了 `/assets`，favicon 一直在 404

- **现象**：`/` 正常、界面正常，只有控制台一条红：`favicon.svg 404`。
  `dist/brand/` 同理——`public/` 里的东西是拷到 `dist/` 根目录的，不在 `/assets` 下。
- **根因**：`main.py` 只 `mount("/assets", ...)`，`/favicon.svg` 与 `/brand/*` 没有任何路由。
- **修法**：显式 `/favicon.svg` 路由 + 存在才挂 `/brand`。
- **教训**：**只测 API 永远发现不了这类问题**——它只发生在浏览器里。
  现在 `test_favicon_is_served` 兜住（CI 后端 job 没有 dist 时自动 skip）。

#### （2）Postgres 连不上时，请求会**永久挂住**（不是报错）

- **现象**：`.env` 指向的 Postgres 没起，`curl /api/projects` 等 70 秒没有任何响应，
  进程日志里连一条错误都没有。界面上就是无限转圈。
- **根因**：libpq 的 `connect_timeout` **默认是"一直等"**，而 `create_engine` 没传这个参数
  （`store.py` 的注释里写着"需要连接超时等参数"，代码里是空 dict——注释骗了人）。
- **修法**：`connect_args_for()` 按驱动给参数，Postgres 一律带
  `connect_timeout`（默认 5 秒，`DB_CONNECT_TIMEOUT` 可覆盖）；
  另外加 `OperationalError` 处理器，返回 **503 + 人话**（告诉用户去改 `DATABASE_URL` 或起容器），
  而不是 500 + 堆栈。
- **教训**：**超时要显式给**。`localhost` 会同时解析 v4/v6，两轮各 5 秒 → 实际约 10 秒才报错，
  这个量级可以接受；但"无限等"是不可接受的。

#### （3）导出文件名是 `雨夜归人_vver_xxx.txt`

- **根因**：`f"{title}_v{version_id}"`，而 `version_id` 本身已经带 `ver_` 前缀。
- **顺带**：`Content-Disposition` 的 ASCII 退路把所有中文替换成下划线，
  纯中文标题会得到 `____.docx`——老客户端拿到一个没有名字的文件。
  现在收敛连续下划线，去掉扩展名后没有 ASCII 字母数字就退成 `script.docx`。
- **教训**：拼接 id 时**别假设它不带前缀**；`filename*` 之外那条退路也得看一眼。

#### （4）首屏背景图链的是第三方博客图床

- **现象**：`WelcomeHero.vue` 的 `.hero-image` 第一层 `url()` 指到一个博客图床的 webp，
  浏览器以 `ERR_BLOCKED_BY_ORB` 拦掉——设计想要的底纹从来没有真正出现过，
  首屏还一直在为一个死链发请求。
- **修法**：改成自绘的 `public/brand/hero-texture.svg`（冷光 + 斜向光带 + 胶片颗粒），
  零外部依赖：断网、内网、图床挂掉看到的是同一块光影。
- **教训**：**首屏不要有外链资源**。这类失败在控制台里只是一行，在视觉上是"看起来就是黑的"。

#### （5）体检方法本身值得复用

```bash
# 1) 起服务（SQLite 免部署）
DATABASE_URL="sqlite:///./data/smoke.db" CHECKPOINTER=memory \
  python -m uvicorn app.main:app --port 8124

# 2) 真跑一遍：导入 → 生成 → 对话流式 → 版本/导出
# 3) 真点一遍：Chrome CDP（--remote-debugging-port）无头截图 + 收集 console/Network 事件
```

第 3 步是这次最有价值的：`Log.entryAdded` + `Network.loadingFailed` 一起看，
才能拿到"404 的是哪个 URL""被 ORB 拦的是哪张图"——只看截图只能看出"这里有点空"。

#### （6）测试清理用 `ignore_errors=True`，`data/` 悄悄堆了几十个目录

- **现象**：`data/` 下有几十个 `test_*.db` 与 `workspace_test_*` 目录（几 MB）。
- **根因**：两个夹具的 teardown 分别是 `unlink` 包 `except OSError: pass` 和
  `shutil.rmtree(..., ignore_errors=True)`。Windows 上句柄释放有延迟，
  删除偶尔失败 → **失败被完全吞掉**，跑一轮就留几个。
- **修法**：重试 3–4 次（含 `-wal`/`-shm`），仍然失败就 `warnings.warn`，
  让 pytest 摘要里看得见。
- **教训**：清理代码里 `ignore_errors=True` 等于"以后没人知道这里漏了"。
  要么重试，要么出声，别静默。

### 5.15 第一次跑通 judge 基线之后（2026-10-30）

#### （1）平均分没信息量，要去看「哪一维塌了」

第一次拿到真实分数：10 条 case、平均 **82.5**、critical=0。这个数字本身说明不了什么。
真正有用的是**逐维拆开的分布**：八条 case 的连贯性都在 93–95，只有两条是
**37.5 和 55.0**；而且 judge 给的两条证据是同一件事——「前序写窗光，提示词写小夜灯」。
**平均分把一条结构性缺陷稀释成了 5 分。** 看基线先看离群值。

#### （2）「锚复用检查全绿」不等于链路上没矛盾

`dialogue_tension` 的录制样本：美术指导写「头顶一盏裸露的钨丝灯泡、桌面暖黄」，
导演分镜的 `lighting` 写「头顶一根冷白日光灯管」。提示词逐字复用了环境锚，
所以 `DPT-09/11`（锚复用）全绿——但两层的**物理设定**是打架的。
锚复用只保证「下游没有重写上游」，**不保证上游之间自洽**。这类检查必须单独一层
（现在的 `LNG-04`）。教训：每条「一致性」断言都要问清它比的是哪两层。

#### （3）判定方向要想清楚：谁在谁之前跑

发现上述矛盾后，第一反应是往**导演技能**里加一句「光源服从美术指导的 `lighting_style`」。
写完才发现调用顺序是 **导演 → 美术指导 → 摄影**：导演根本看不到风格指南，
这句话是空话（而且方向反了，`director_notes` 才是传给美术指导的输入）。
正确写法是让**美术指导**把分镜里的 `lighting` 统一成一个环境锚。
**改提示词前先确认这一步能拿到什么输入**，否则写出来的是一条永远不会生效的规则。

#### （4）「全被挡住」时报告仍打印「通过」＝假绿灯

余额耗尽那一跑，10 条 case 全部 `blocked`（`402 Insufficient Balance`），
报告结尾却是 `结论：通过`——因为判定条件是「critical=0 且没有 case 失败」，
而 blocked 既不算 critical 也不算失败。**余额耗尽的 CI 会亮绿灯，而什么都没评估。**
改成三态：全部挡住 → 「无法评估」；部分挡住 → 「通过，但有 N 条未评估」。
只要报告里存在「没跑到」的 case，就不能出现干净的「通过」。

#### （5）兜底逻辑用「前 N 个字」判断覆盖，一定会重复追加

`dp.py` 的最终画面兜底用 `_norm_key(final_frame)[:8] in body` 判断模型写没写。
模型只要在锚前面加个主语（「他退到门口，**冷光这才熄灭在他脸上**」），前 8 字就对不上，
于是整句锚又追一遍——实测同一句连着出现两遍，视频模型会当成两个连续动作。
改法：滑窗覆盖率（4 字窗 ≥60% 或 2 字窗 ≥65%）。标定用的六组真实改写在
`tests/test_crew_video.py` 里，2 字窗把「已写过」与「真没写」分在 0.71 / 0.14。
**判「有没有写过」不要比前缀，要比片段覆盖率。**

---

### 5.16 多把 key 并存：先分清「key 坏」还是「请求坏」（2026-10-30）

视频 key 余额耗尽时（`402 insufficient balance (1008)`）整条出片链路停摆，只能手动改
`.env` 重启。做成 key 环后，最要紧的不是「怎么切」，而是**判据**：

| 响应 | 该不该换 key | 为什么 |
|---|---|---|
| `401` / `(1004)` 鉴权失败 | 换 | key 本身无效，重试同一把永远失败 |
| `402` / `(1008)` 余额不足 | 换，并冷却 | 同上；冷却到期自动复活，充值后无需重启 |
| `400` / `(2013)` 参数错误 | **不换** | 换 key 也没用；误判会把好 key 全判死十分钟 |
| `(2013) invalid task_id`（查询时） | 换 key 继续查，**不冷却** | 任务属于另一个账号，key 本身是好的 |

两个容易踩的实现点：

1. **任务要认领账号**。视频任务是账号级资源，用 A 提交、B 查会拿到 2013。提交成功时把
   `task_id → key` 记进 `MiniMaxProvider._TASK_KEYS`，轮询优先用绑定的那把；只有查不到时
   才去试别的 key。
2. **不要让失败静默改写顺序**。冷却只是把 key 排到候选末尾，不是删除——全部在冷却期时
   仍按最早恢复的顺序兜底试一次。真全挂了，报错要写清「哪一把、为什么」，
   key 用 `mask()` 打码，别把完整 key 泄进日志。

**测这一类代码别只测 happy path**：`tests/test_keys.py` 里有一半用例是「不该发生的事」——
参数错误不换 key、2013 不冷却、全挂时报错文案带原因、空配置不崩。

---

### 5.17 只渲染一部分镜头时，台词会「挤」到别的镜头上（2026-10-31）

第一次拿公版短篇《孔乙己》走完整链路（导入 → 改编 → 分镜 → 出片 → 配音 → 合成），
为了省额度只批准了 9 镜中的 3 镜。结果成片出现两个只有「真跑一遍」才看得见的问题：

1. **两句字幕叠在同一秒**。台词归属是「按场景的 beats 段对齐到该场镜头」的，
   但参与对齐的只有**已渲染**的镜头——没拍出来的那几镜的台词照样被均分下来，
   全压到 1 个镜头的时间窗口里。
2. **整片被 1.6 倍速压缩**。台词总跨度 34.7s > 片长 19.8s，配音层按「跨度比」提速兜底。
   提速本身没错，错的是让它去兜「本来就不该出现」的台词。

另外 TTS 撞到 `1002 rate limit exceeded(RPM)` 时直接「跳过」，17 句里静默丢了 4 句——
那一版成片里有人说话却没声音，观众只会觉得配音没做好。

**修法**：
1. `plan_dubbing(..., expected_shots=...)`：传入该场在版本里的**原始镜头数**。
   当 `原始数 > 已渲染数` 时只保留能通过 `dialogue_ref` 明确归属的台词，其余丢弃并记进
   `plan.warnings`（进成片报告的 `warnings`）。段对齐在部分渲染下必然错位，整场放弃。
2. 字幕估算路径 `timeline_estimated` 复用同一套判据，避免「配音丢了、字幕还在」。
3. TTS 加退避重试：`rate limit / 1002 / 429 / timeout / 502-504` 按 2^n 秒重试，
   最多 3 次；`没有音频` 这类厂商真拒绝不重试（重试也没用，干等十几秒）。

**代价（要如实说）**：部分渲染时该场会变成**无声**。这是正确的取舍——宁缺毋滥，
总比「两句字幕叠在一起 + 全片赶语速」强。要听得到对白，就得把该场的镜头整场出完。

**什么时候该推翻**：如果将来做了「按场出片」的批量编排（一场要么全出、要么不出），
这个判断就该挪到出片侧，配音层不该再猜。

**验证**：`tests/test_dubbing.py` 新增 5 条（部分渲染丢弃 / 全片不误伤 /
dialogue_ref 保留 / 限流重试 / 非限流不重试）；重跑同一项目的合成，
日志从「34.7s 塞进 19.8s、按 1.6 倍速重合成」变成三条明确的跳过说明。

---

### 5.18 「动作乱、声音不搭、不知道谁说的」是三个不同层的问题（2026-10-31）

5.17 修完「部分渲染台词挤压」，整场重跑仍然被一句话否掉：
**「声音和动作都很乱」「完全不知道是谁和谁说的」**。逐层拆开是四件事：

| 症状 | 真正的根因 | 位置 |
|---|---|---|
| 动作糊成一团、手部穿模 | 8–10 秒的一镜里写了 3–6 段连续动作，最长一句 89 字 | 导演分镜 |
| 动作被硬压短 | 出片时长写死 `params.get("duration_sec") or 6`，导演写的 8/10 秒没生效 | 出片接口 |
| 声音和画面对不上 | 每句台词都排在「镜头起点 + 0.4 秒」，没有「这句该在第几秒说」 | 配音排程 |
| 不知道谁在说 | 一个镜头填了 dialogue_ref，其余台词全被塞给**同一个**镜头 | 配音归属 |

四条里只有一条是「渲染层」，三条是**上游数据结构就没表达清楚**。

**修法（按层）**：

1. **导演分镜 → 硬断言**。技能文件里早就写了「一镜只有一个主事件」「对话时长 ≥ 字数 ÷ 4」，
   但模型不照做（与 5.14 同一条教训：软要求写不进概率行为）。改成确定性校验，
   不满足就把 critique 回喂重拆。
2. **出片时长 → 三级优先**。`调用方显式指定 > 导演的 duration_sec > 节奏默认值`
   （`app/video/rhythm.py`：slow 8s / medium 6s / fast 4s）。最终值写回落库 params，
   重试和批量重投口径一致。
3. **台词 → 加 `dialogue_at`**（镜头内第几秒开口）。配音按它落位，并夹在本镜范围内，
   绝不让声音盖到下一镜的画面上。
4. **台词归属 → `assign_dialogue_ranges`**。按 `dialogue_ref` 的分界点切：一镜只拿
   「从它引用的那句起、往后同一个说话人连续说的几句」。断言和配音**共用同一份口径**，
   否则会出现「断言说对了、配音还是错的」。

**三个踩坑（都花了真实额度/时间）**：

- **用正则判「谁在做动作」必错**。想抓「一镜三人各动各的」，就用「角色名出现几次」，
  结果 `酒客甲下巴朝孔乙己一抬` 被判成两个主体在做动作。它让导演白改三轮，还把一个
  本来合格的版本（10 镜、只差 0.2 秒）改坏成 11 镜 7 个问题。**结论：语义判断别用正则兜，
  机械层只兜「明显塞太满」**。
- **critique 一次给太多，模型越改越散**。11 条批评一起回喂，问题数从 2 涨到 7。
  限量 5 条（`MAX_CRITIQUE_SHOWN`）后才收敛。
- **「分号段数」≠「动作数」**。`把碗搁在柜台上；下巴一抬；手指点过去` 是一个动作的三笔
  细节，所以阈值下限是 3 段而不是 2 段。

**怎么验证修法真的有效**：拿**旧分镜**回放**新断言**——9 镜全部动作超标、13 段对话没有
镜头认领。这一步很关键：它证明「成片乱」不是渲染运气差，而是分镜本身不可执行。
如果旧数据能过新断言，说明断言写松了。

**第四个坑：提示词把全场人物的外貌都写了进去**。重出片后拿到一个「酒客甲开口」的特写，
画面里挤了四个人，最抢眼的是背景里戴帽喝酒的那位，主体反而成了右侧背影。原因是
`cast` 取了 LLM 列的「在场人物」全量注详细外观锚——连只该当背景的人都写了几十字的
皮肤、眉骨、服装。改成**只给本镜主体注详细锚**（主体按导演写的 `subject` 判定，
其次本镜 body 里点名的人），背景人物交给 `background_action` 描述行为、不给外貌。
这一条改完还没在真实出片里验证过（额度用尽），下次有额度时优先回归。

**相关**：D-17 · `tests/test_director_guard.py` · `app/video/rhythm.py`

---

### 5.19 女声从男角色嘴里出来：音色是「按序号发」的（2026-10-31）

5.18 修完分镜与台词落位，重新合成成片，用户又指出一层：

> 女声音的角色从男角色嘴中出来了。

把《孔乙己》的配音日志和选角结果摊开看，《孔乙己》五个角色里**三个拿到了女声**：

| 角色 | 旧逻辑分到的 | 真实身份 |
|---|---|---|
| 酒客甲 | `female-yujie`（御姐音） | 男 |
| 酒客乙 | `female-yujie`（御姐音） | 男 |
| 我 | `female-yujie`（御姐音） | 男（小伙计） |
| 孔乙己 | `male-qn-badao` | 男 |
| 掌柜 | `male-qn-badao` | 男 |

根因在 `app/media/dubbing.py::assign_voices` 的兜底：

```python
alternation = [VOICE_FEMALE_MATURE, VOICE_MALE_MATURE]   # ← 从女声起头
for c in script.characters:
    if c.name not in voices:
        voices[c.name] = alternation[i % 2]              # ← 按出场序号发声音
```

三个问题叠在一起：

1. **列表从女声起头**。第一个推断不出性别的角色必然拿到御姐音，后面按男女隔一交替。
   《孔乙己》的剧本里五个角色一个性别线索都没有（全是「酒客甲」「掌柜」这种功能性名字），
   于是变成「一女一男一女一男一女」。
2. **推断只看名字和 personality 里的关键词**，其中「她」还被当成女性线索——描述里
   出现「她的父亲」就会把父亲判成女的。
3. **音色按序号发，不按角色**。同一场里多个男角色共用一把嗓子，用户说「不知道是谁和谁说的」
   有一半是这里来的——声音没有区分度，台词归属再准也听不出来。

**修法**（详见 D-18）：把「角色→音色」从运行期猜测改成**资产库里的显式字段**，
推断降级为兜底，且兜底方向改成**默认男声**（「女声从男人嘴里出来」比反向刺耳得多）。
挑音色的顺序也改了：主角 → 反派 → 配角，同档按台词字数降序——因为剧本阶段常把所有人
一律标成 `supporting`（《孔乙己》五个角色全是），只按 role 排等于没排。

顺带修了两个隐性 bug：

- 美术指导原先用 `set_video_style` **整体覆盖**风格 JSON，会把资产库里人工指定的音色冲掉；
  改成 `merge_video_style`。
- `app/api/video.py` 里 `getattr(version, "pacing", None)`——`VideoVersion` 根本没有 `pacing`
  字段，所以「按场景节奏取值」从来没生效过，一直用的 medium。

**教训**：凡是「按序号/顺序分配」的地方都要问一句——这个顺序有意义吗？`[女, 男]` 交替
这种写法看起来是「随机但稳定」，实际上是**把性别当成了序号**。

**相关**：D-18 · `tests/test_cast.py` · `app/media/casting.py`

---

### 5.20 短剧与漫剧：先想清楚「谁包含谁」再写代码（2026-10-31）

用户提出要区分短剧和漫剧，同时又说「我感觉只有画风上的差距，这两个是不同内容还是谁包含谁」。

第一版实现把「漫剧」加成了第六种 `AdaptationType`（`anime_drama`），配有独立的节奏表、
独立的 profile、独立的音色池。写完后发现这是在给一个**画风**造一套**体裁**的壳：

- 短剧按「载体 + 时长 + 分发」定义，对面是电影/长剧集；
- 漫剧按「画风 + 呈现介质」定义，对面是实拍短剧。

两者不在同一根轴上，**漫剧是短剧的子类**。做成并列类型会让导演拆镜、节奏、校验全分叉，
而画风真正只影响三件事：定妆图 prompt、提示词画风声明、配音气质。于是回退成
`visual_style` 正交维度（详见 D-19）。

**判断方法**（下次遇到「这两个是不是一类」时可以直接用）：把两个词分别放回它们所属的
**分类轴**上问「它的对面是什么」。如果对面是另外一个词，说明它们不是并列关系——
短剧的对面是电影，漫剧的对面是实拍；短剧和漫剧根本不是对立面。

**相关**：D-19 · `app/pipeline/visual_styles.py` · `app/video/rhythm.py`

---

### 5.21 「拆不动、拍太长、发不出」：真跑一遍才暴露的三层断裂（2026-11-01）

产品收窄到「小说 → 短剧」之后重跑《孔乙己》，从导入到出片，卡在三个地方。
每一处都不是玄学，是「代码里没有这个约束」。

#### (1) 拆不动：JSON 被截断，整批作废

7 场戏一次性输出分镜 JSON，模型的单次输出有上限，JSON 从中间被硬切断，
`json.loads` 必然失败 → 整批 0 镜。重试两次也一样，因为**同样的输入必然同样的长度**。

三次真跑，三次都截断在同一处：

```
导演拆解失败（scene_004、scene_005、scene_006）：无法解析 JSON 响应：```json
[ { "scene_id": "scene_004", "director_notes": "这场戏是孔乙己全片唯一一次主动「给予」…
```

修法三层：
1. **按场分批**（2 场/批），合并后 order 全局重编号（`start_order` 参数）。
2. **截断救援**（`crew/base.py::salvage_objects`）：按括号深度切出所有完整的顶层
   对象，丢掉被切坏的那一个。已经生成好的场景不该因为最后一个没写完而全丢。
3. **缺场补拆**：救援后少了的场景单独再拆一次（一次一场，输出足够短）。

#### (2) 拍太长：60 镜 / 295 秒，而短剧是 120 秒

拆出来 62 镜、293 秒——一部 2 分钟的短剧，拆出了 5 分钟的片子。生成成本三倍，
观众也早划走了。

先在导演层加片长预算（`rhythm.DURATION_BUDGET_SEC`，短剧 120 秒，超 1.5 倍报错），
**但没用**：分批拆解后模型只看到自己那 2 场，「整片 120 秒」对它没有任何约束力。
改成按场数折算「本批只有 34 秒、6–8 个镜头」，才有约束力。

真正的根因还在更上游。把剧本摊开数：

```
scene_001: 3 句对话, 52 字      scene_005: 8 句对话, 123 字
scene_002: 4 句对话, 77 字      scene_006: 5 句对话, 60 字
scene_003: 3 句对话, 50 字      scene_007: 0 句对话, 0 字
scene_004: 5 句对话, 109 字
全片 28 句 / 471 字 ≈ 118 秒纯念白
```

471 字对白念完就是 118 秒——两分钟的片子只剩 2 秒给画面。而「一镜一说话人」
的规则下，镜头数被台词数锁死。**片长是从剧本层开始失控的**，下游再努力也压不回来。

于是把预算往上游推：
- `validate_script` 新增对白预算 warning（短剧成片 120 秒 → 对白 ≤264 字）；
- profile 的 script_blocks 写明「单集对白 200–260 字，其余留给动作与停顿」。

#### (3) 发不出：429 不是限流，是额度用尽

13 镜 1 秒间隔提交，13 个全 429 failed。

第一反应是并发限流，于是加退避重试（2/4/8 秒）——**还是全挂**。
把两把 key 分开测才发现：

```
sk-cp-…agvg -> FAIL 429
sk-api…0swM -> OK 458583578866260
```

429 是**按账号**算的，主 key 的额度用尽，备用 key 一次就过。而旧代码
`key_failure()` 只认 401/402/403 和 1004/1008，**429 被当成普通错误抛出去**，
多 key 轮换在最需要它的时候反而不生效。

更细的一层：MiniMax 把「Token Plan 用量上限」（2056）也回成 429，但它和真限流
处理方式相反——真限流等 60 秒就好，额度用尽等多久都不会恢复。混为一谈的代价是
日志报「限流」，用户等一小时还是 429，却不知道要充值。所以：

- `key_failure` 把 429 算作 key 级（换 key）；
- `is_rate_limited` 用 `_QUOTA_HINTS`（2056 / 用量上限 / 套餐）把额度用尽排除掉；
- 冷却按类型分：真限流 60 秒，额度用尽 600 秒；
- 退避重试只对真限流生效，额度问题直接抛给用户看。

**顺带确认**：批量出片本身是**串行**的（`batch.py` 逐镜推进，等这一镜成片才提交
下一镜），所以产品路径不会撞并发。13 镜全挂是我手动并发提交造成的——但这层保护
值得有，因为 API 是开放的。

**相关**：D-20 · `app/crew/base.py` · `app/video/rhythm.py` · `app/keys.py`

---

### 5.22 「台词硬塞」的根因不是配音，是导演没见过 beat id（2026-11-01）

现象：成片里有人说话，画面却对着别人的手；有些台词干脆没声音。

先查了配音层——它按 `dialogue_ref` 找台词，找不到就跳过并记 warning，逻辑没毛病。
于是把 62 个镜头的 `dialogue_ref` 逐个对照剧本：

| 指向 | 数量 |
|---|---|
| 真实的对白节拍 | **3** |
| 动作节拍（本来就没台词） | 4 |
| **剧本里不存在的 id** | **28** |

那 28 个长这样：`beat_003_dlg_001`——剧本里只有 `beat_003`。

**根因在 prompt，不在校验**。导演 prompt 列节拍时是这样写的：

```
[对白] 孔乙己（嘲讽）：偷书不算偷
```

**没有 id**。模型手里没有可引用的真 id，只能自己造一个。校验里只有「每句台词都要
被某个镜头认领」这个正向检查，没有反向的「镜头标的 ref 是不是真的存在」——所以
编造 id 能一路绿灯到出片。

修三处：

1. prompt 里每个节拍带上 id：`[对白 beat_003] 孔乙己（嘲讽）：…`；
2. 校验补反向检查：ref 不存在 → 打回并说明「照抄节拍清单里的 id」；ref 指向动作
   节拍 → 单独提示（它确实存在，但配不出音）；
3. 技能补两条铁律：id 照抄不许编；有台词的镜头 `subject` 必须包含说话人。

第 3 条的后半句**只写进技能，没做机械校验**：按角色名匹配分不清「甲指着乙」和
「乙在做动作」，这个项目之前试过按名字数主体，实测全是误报，已撤回（见
`director.action_budget` 的注释）。能用规则兜住的兜住，兜不住的老实交给提示词。

**相关**：`app/crew/director.py` · `app/skills/director-breakdown/SKILL.md`

---

### 5.23 改个角色名，定妆图和音色就全丢了——而且不报错（2026-11-01）

现象：长篇改到后半段，同一个角色「长得像换了个人」，音色也变了。没有任何异常日志。

查存储：五份资产全按**中文名**做键。

```python
character_appearances: {"孔乙己": "四十岁上下……"}
reference_images:      {"孔乙己": "https://…"}
voice_casting:         {"孔乙己": "male-qn-badao"}
character_genders:     {"孔乙己": "male"}
```

名字一变，四处同时查不到 → 定妆图不出、音色回落到推断、外貌锚消失。**每一处都是
`dict.get()` 返回 None，都有合法的兜底**——所以既不崩也不报错，只是画面慢慢变味。

修法是加一层键解析（`app/media/assets.py`，详见 D-22）：

- 存储用 id，模型仍写名字，落库前 `rekey()` 转换（**格式由代码保证，不指望模型**）；
- 读的时候两种键都认，老数据不用迁移；
- 用户第一次改资产时，整份表顺手规整成 id 键；
- 重名不猜（猜错等于把 A 的脸贴 B 身上）。

真数据验证（《孔乙己》，老数据全是名字键）：8 个角色的外貌锚全部读到，音色仍然
八人八声。

**这次也留下一个诚实的边界**：迁移之前写入的名字键，在角色**改过名之后**无法再
映射（没有改名历史）。所以「改名不丢锚点」只对迁移后的写入成立。

**相关**：D-22 · `app/media/assets.py` · `tests/test_entity_assets.py`

---

### 5.24 换成手写循环后，工具参数是「分片」到的（2026-11-05）

> 背景：对话内核从 LangGraph 的 StateGraph/ToolNode 换成 `app/chat/loop.py` 的显式循环（D-29）。

#### （1）流式下 tool_calls 的 args 是**字符串分片**拼出来的

- **坑**：`ChatOpenAI.stream()` 吐的 `AIMessageChunk` 里，工具参数不是 dict，而是
  逐片到达的 JSON 字符串（`{"x":` + `42}`）。用 `chunk.tool_calls` 直接读第一片会拿到
  **半个参数的 JSON**，工具就会带着残缺参数执行。
- **解法**：把分片**累加**成一个完整的 chunk（`acc = acc + chunk`），再用聚合块的
  `tool_calls`。LangChain 的 `AIMessageChunk.__add__` 会负责把字符串片段拼好并解析。
- **回归**：`tests/test_loop.py::test_streaming_aggregates_split_tool_call_arguments`
  专门把参数从中间切成两片验证这条。

#### （2）dict 消息不能直接喂给 `ChatOpenAI`

- **坑**：把 OpenAI 原生的 `tool_calls: [{id, type:"function", function:{name, arguments}}]`
  直接当 dict 传进 `ChatOpenAI.invoke()`，LangChain 会当成「已经是 ToolCall 结构」，
  `args` 字段读不到——参数静默变空。
- **解法**：协议层和框架层分开。内核用 OpenAI 原生 dict；调模型前由
  `_to_lc_messages()` 转成 LangChain 消息，工具调用转成 `{name, args, id}` 三元组。
- **为什么要较真**：这个适配层是将来换自写 HTTP provider 时的**唯一切换点**，
  所以用测试把契约钉死（`test_tool_message_dict_survives_round_trip`）。

#### （3）工具异常不能抛出去

- **坑**：ToolNode 默认会把工具异常包成错误文本回给模型；手写循环如果直接 `raise`，
  一次工具失败就会打断整轮对话。
- **解法**：`_invoke_tool()` 捕获所有异常，返回 `工具执行失败：…` 文本，交给模型判断
  怎么补救。未知工具名也返回可操作提示（列出可用工具）。

#### （4）轮数上限要留「回答的那一轮」

- **坑**：`for step in range(max_steps)` 里每轮都执行工具，跑满上限时**最后一条助手消息
  还没生成**，用户会看到一句「工具调用太多」而不是答案。
- **解法**：循环跑 `max_steps + 1` 次，最后一次不再执行工具，只收回答；
  真跑满时明确告知「这轮工具调用有点多，先停在这里」。

#### （5）迁移时的做法：先钉行为，再换实现

换内核前先把 348 个单测跑绿当作行为基线；换完必须仍然 348 绿（只有 2 个直接断言
LangChain 消息类型的测试需要改成断言 dict 协议）。然后再用**真实模型**冒烟：
流式 token、工具调用参数、工具结果回灌、落库，四项都验。

**相关**：D-29 · `app/chat/loop.py` · `tests/test_loop.py`

---

### 5.25 「我改了 .env 怎么不生效」——两条读取路径 + 一条优先级压制（2026-11-05）

排查一次「更新 .env 后没反应」，发现是三个独立原因叠在一起。三个都容易复发。

#### （1）有一批配置不走 Settings，而是直接读 `os.environ`

- **症状**：CLI / 自写脚本里，`.env` 明明配了 `MINIMAX_API_KEY_FALLBACKS`，
  但 `minimax_keys()` 返回 **0 把**——备用 key 完全没被用上。
- **根因**：`Settings` 自己会读 `.env`（`SettingsConfigDict(env_file=...)`），
  但 `app/keys.py`（多 key 轮换 + 冷却）、`app/video/qa.py`（`FFMPEG_PATH` 兜底）、
  LangChain 的 `LANGCHAIN_*` 都是**直接读 `os.environ`**。而 `load_dotenv()` 之前只在
  `app/main.py` 里调——CLI 等入口从来没把 `.env` 落进进程环境。
- **解法**：`config.bootstrap_env()` 统一做两件事（`load_dotenv` + LangSmith 映射），
  `app/main.py` 与 `app/cli.py` 都调它。新写入口（脚本 / worker）也要调。
- **判据**：`minimax_keys()` 从 0 把变成实际配置的条数。
- **测试**：`tests/test_bootstrap.py`（含「入口必须调用 bootstrap_env」的断言）。

#### （2）`Settings` 读了 `.env` ≠ 配置生效：界面偏好优先级更高

- **症状**：`.env` 写 `DEEPSEEK_MODEL_NAME=deepseek-flash`，`/api/status` 却报
  `deepseek-v4-pro`，且 `source: db`。
- **根因**：这是**有意设计**——`resolve_chat` 里「用户在设置里选的模型」压过 `.env`
  （否则设置入口形同虚设）。库里留着一条旧偏好，就一直压着新的 `.env`。
- **解法**：界面上点「恢复 .env 默认」（写一条 `provider_id=null` 的偏好），
  或调新增的 `DELETE /api/models/preferences/{task_type}`。
  排查时先看 `/api/status` 的 `source` 字段：`db` = 界面偏好生效，`env` = .env 生效。

#### （3）`reset_llm()` 假设 llm 是 `lru_cache` 函数

- **症状**：`AttributeError: 'function' object has no attribute 'cache_clear'`
  （在 `set_preference` / `clear_preference` 的收尾处）。
- **根因**：`reset_llm()` 里直接 `llm.cache_clear()`；测试会把 `deps.llm`
  替换成普通 lambda，于是炸在请求收尾，整个接口 500。
- **解法**：`getattr(llm, "cache_clear", None)` 取，拿不到就跳过。
  这类「清理动作」不该成为接口失败的原因。

**通用教训**：`.env` 有三条落地路径——`Settings`、`os.environ`、数据库偏好。
new 一个配置项时先想清楚它走哪条，并在文档里写明；排查「不生效」时三条都要看。

**相关**：`app/config.py::bootstrap_env` · `app/keys.py` · `app/llm/registry.py::resolve_chat` · `.env.example`

---

### 5.26 测「技能有没有用」：消融对比，以及三个把人带沟里的坑（2026-11-06）

> 背景：主 Agent 的四个技能是按阶段拆开的。「拆开到底有没有用」不能靠论证，
> 要靠测量。见 [`DECISIONS.md` D-30](DECISIONS.md)。

#### （1）消融组里模型**照样会尝试调用**被摘掉的工具

- **症状**：消融组（`load_skill` 已从工具集移除）的报告里显示「已加载 production-delivery」。
- **根因**：系统提示写着「本轮第一个工具调用就应该是 load_skill」，模型照做，
  发起了一个**指向不存在工具**的调用。循环记下 tool_call 事件、返回「未知工具：…」。
  harness 把「尝试」当成「加载成功」→ 消融组显示技能可用 → **测量直接失效**。
- **解法**：`loaded_skills()` 只认**成功**的加载——用工具结果排除掉
  「没有这个技能：…」「未知工具：…」「技能 X 读取失败」三种失败形态。
- **教训**：做消融实验时，「对照组被正确削弱」本身要单独验证，不能假设。

#### （2）用例必须让领域动作**可执行**

- **症状**：第一版用例全 FAIL，且两组表现几乎一样。
- **根因**：用例建的项目**只有原文、没有剧本**。Agent 调 `get_status` 发现没剧本，
  于是回「还没有剧本」——它是对的，但测出来的是「它知道自己没剧本」，
  不是「技能让它去做对的事」。
- **解法**：每条用例按需预置内容——`prepare="script"`（生成剧本版本）
  或 `prepare="shots"`（再跑到分镜 + 视频提示词，且全标成**待批准**）。
  「出片前要先批准」这条约束，只有在真的有未批准镜头时才被考验。

#### （3）单次结果方差大到不能下结论

- **症状**：同一用例在两轮里做出不同选择（一次调了 `map_novel`，一次直接
  `generate_script`）。
- **根因**：温度 0 也不保证远端模型逐字一致。
- **解法**：`--repeat N`（默认 3）跑多次，**看按用例聚合后的比例**，不看单次。
  报告默认只打聚合表，`--verbose` 才看逐次明细。

#### 一个附带发现

消融对比顺带证明了**拆分没有引入过度触发**：反向用例（与短剧无关的请求）
在两组都是 6/6——说明「不该加载」这条判断在拆分后依然成立。
这本来就是拆分的理由之一，但此前只是论证，现在是实测。

**相关**：`evals/skill_eval.py` · `evals/skill_cases.py` · `tests/test_skill_eval.py`

### 5.27 前端重写期的四个坑：漏 import、SSE 少字段、端口被抢、管道吃进程（2026-11-07）

> 背景：2026-11-06/07 把前端整体重写并对齐视觉，连着踩了四个坑。
> 前两个是**代码层**的，后两个是**环境层**的——都会表现成「界面莫名不对 / 服务莫名不在」。

#### （1）模板里用了组件却忘了 import → 整页空白

- **症状**：点「产出」页，页面只有标题栏，body 完全空白；控制台无报错。
- **根因**：`App.vue` 里写了 `<ViewerPanel class="page-viewer" />`，但重写时**漏了 import**。
  Vue 遇到未解析的组件，会把它当成**原生自定义元素**渲染成空标签——不报错、不警告，
  页面就是空的。DOM 里会看到 `<viewerpanel class="page-viewer"></viewerpanel>`。
- **解法**：补 import。**判据**：DOM 里出现小写连写标签（`<viewerpanel>`）就是漏 import。
- **预防**：加一条自查脚本，扫所有 `.vue`：模板里用到的 `<Xxx>` 是否有对应 import。
  （2026-11-07 的扫描结果：全库只此一处漏。）

#### （2）SSE 事件体里没有 `type` → 步骤面板永远是「已完成 0 步」

- **症状**：**刷新后**看历史消息，工具步骤显示正常；**正在流式**的那条消息，
  步骤面板却是空的（标题写「已完成 0 步」，下面一行都没有）。
- **根因**：后端 SSE 把类型放在 `ev.event` 上，**事件体 `ev.data` 本身没有 `type` 字段**；
  只有落库的历史事件才带 `type`（`{"type": ev.type, **ev.data}`）。
  前端把 `ev.data` 直接塞进消息的 events，再按 `e.type` 分派——流式那条一条都匹配不上。
- **解法**：前端入队时补 `type: ev.event`；后端 `done` 事件带 `turn_meta`（模型 · 耗时），
  流式消息也能有消息头。
- **教训**：**「刷新后正常、流式中不对」几乎总是两套数据形状不一致**，
  不是组件逻辑问题。查这类问题先对比刷新前后的 DOM / 数据形状。

#### （3）8000 端口被另一个项目同时占着 → 请求随机落到别人身上

- **症状**：`curl http://127.0.0.1:8000/openapi.json` 返回的是**另一个应用**的接口文档
  （`title: guanwater`），而 `/api/status` 又返回本项目的模型信息；`GET /` 直接 404。
  两个服务都在日志里打请求，两边都以为是对方。
- **根因**：Windows 上两个进程可以同时绑定 `127.0.0.1:8000`（`SO_REUSEADDR`），
  内核把连接**轮流**分给它们——于是表现成「时好时坏、串味」。
- **解法**：`netstat -ano | grep :8000` 看有几个 LISTENING；用 PID 对照进程。
  本项目默认 8000，与别的项目撞了就换端口（或让对方换）。
- **附带**：本项目的后端若被别人的前端轮询，日志里会出现
  `/api/delivery/home-orders`、`/api/auth/login` 这类**本项目没有**的 404——
  看到它们先怀疑端口串了，而不是以为本项目多了接口。

#### （4）`uvicorn ... | tail -N` → 日志管道一满，服务自己退场

- **症状**：后台起的服务「跑着跑着就没了」，退出码还是 **0**（看着像正常结束），
  端口随即空出来。
- **根因**：`tail -40` 读满 40 行就退出 → 管道另一端的 uvicorn 收到 SIGPIPE → 一起死。
- **解法**：后台起服务**不要接管道**；要看日志就读输出文件
  （`run_in_background` 会给一个 .log 路径）。

#### 一个附带发现：自动化点击会误伤真实数据

浏览器里按文字找按钮点，**点到的可能是删除按钮**。2026-11-07 的实测里，
一次「关闭弹窗」的连点命中了设置弹窗里的「删除」，把用户自建的模型行
（`DeepSeek V4 Pro`）删掉了。事后只能按 `.env` 重建（模型偏好表本来就是空的，
生效模型自动回落到 `.env`，所以没有留下悬空引用）。
**判据**：凡是带 `删除 / 清空 / 重置` 字样的按钮，自动化脚本里要用更严格的选择器；
测试数据与真实数据共用同一个库时，测试前先备份 `data/script_agent.db`。

**相关**：`frontend/src/App.vue` · `frontend/src/stores/app.js` ·
`app/chat/loop.py` · `app/chat/conductor.py` · `app/main.py`

### 5.28 功能体检抓出来的四个真 bug（2026-11-08）

> 背景：用户问「测试连接这些功能真实有效吗，还有没有别的功能不能正常实现」。
> 于是做了一轮系统体检：只读接口全打一遍 + 22 个 Agent 工具逐个验 + 会写数据的功能实调。

#### （1）`POST /api/chat`（非流式）整个 500

- **症状**：前端主用流式（`/chat/stream`），所以界面上看不出来；直接打非流式接口 500。
- **根因**：给 `chat_once` 的**调用处**加了 `refs=`，但 `chat_once` 自己没这个参数 → TypeError。
- **教训**：加参数要改**定义 + 调用点 + 转发层**三层。少改一层就是运行时 TypeError，
  而流式路径不经过它，所以界面测试根本发现不了。

#### （2）模型漏传 `project_id` → 工具失败或答错

- **症状**：问「原文里孔乙己第一次出场怎么写的」，Agent 答「我没有能读取原文的工具」；
  又一次问「项目到哪一步了」，Agent 答「剧本还没有版本」——可明明有 7 场版本、50 个镜头。
- **根因**：模型调工具时会漏掉 `project_id` 或传空串。
  `read_source` 缺参数 → 工具直接校验失败（它就放弃了）；
  `get_status("")` → 查到空项目 → 回答「还没有版本」。
- **解法**：**在工具调用的收口处统一补齐**——`AgentLoop._invoke_tool` 里，若工具有
  `project_id` 且传空，就填上「本轮对话所属的作品」（对话层知道当前打开的是哪部作品）。
  一处修好，所有作品级工具都受益，不用逐个工具改签名。

#### （3）资产库「导入」能选图片，但后端只收视频

- **症状**：点「导入」→ 选一张 png → 「不支持的格式 .png，可用：.avi .m4v …」。
- **根因**：前端 `uploadAsset` 一律打 `/assets/upload`（只收视频），但按钮的 `accept`
  写着 `image/*,video/*`；后端其实**另有** `/images/upload` 收图片。
- **解法**：前端按文件类型分流（图片 → `/images/upload`，视频 → `/assets/upload`）。

#### （4）「测试连接」是假的

- **症状**：key 过期、写错、少个字符，都显示「连接成功」。
- **根因**：实现只检查「key 非空 + 厂商名能解析」，**从不真调厂商接口**。
- **解法**：真的打一次——`GET {base}/models`（OpenAI 兼容协议都支持）；不支持的回落
  `max_tokens=1` 的最小 chat 请求；401/403 明确报「Key 被拒绝」，网络不通报「连不上」。
  视频厂商没有统一探活接口，就如实说只校验了 key 配置。
- **教训**：凡是叫「测试 / 验证 / 检查」的按钮，要么真做，要么别叫这个名。

**体检清单（可复用）**：只读接口全打一遍 → 工具逐个实调（只读的那几个）→
会写数据的功能实调（跑完清理）→ 真跑一轮对话看工具轨迹。

### 5.29 参考视频改走「参考素材」：服务商到底吃哪种地址（2026-11-10）

> 背景：用户说「怎么还在搞公网参考视频这种东西啊，要传参考视频的话会在参考素材里面引用」。
> 原来的做法是让用户在提示词里手填一条公网 mp4 直链——那等于把「参考素材」这个
> 统一入口又劈成了两半。这条路彻底拆掉，改成从参考素材里挑。

#### （1）服务商只认两种地址（实测，不是猜的）

对着 MiniMax H3 V2 接口逐条打探针，结论：

| 塞进 `video_url.url` 的东西 | 服务商怎么说 |
|---|---|
| `https://cdn…/a.mp4` | 收（它自己能去拉） |
| `/api/assets/file/asset_x`（本机回放地址） | **拒**：`must be http(s):// or data:...;base64` |
| `{"file_id": 123}` | **拒**：`content[1].video_url.url is empty` |
| `data:video/mp4;base64,…` | **收**，而且真能出片（跑了一整轮，出了 mp4） |

所以「本机素材 → 服务商」只有一条路：**提交前内联成 data URI**。
`files/upload` 也不行——它只有 `voice_clone` 一个 purpose，没有视频用的
（探针：`invalid file purpose`）。

#### （2）参考视频有三条硬约束

- **时长 2000–15000 ms**。超了报 `video duration 21167 ms, expected [2000, 15000] ms`——
  注意这是**服务端校验**，创建任务时不一定立刻报，可能任务建好了才失败。
  所以在**保存提示词**时就量一遍并当场拒绝，别让用户白跑一次生成。
- **帧率 23.976–60 fps**。15fps 的屏幕录制素材任务建得好，生成阶段才报
  `video fps 15.000, expected [23.976, 60]`（2026-11-10 完整跑撞出来的第二条：
  同样是挂载时就量、不合格当场拒）。
- **数量 ≤3**（参考图 ≤9、混合 ≤12）。超了报 `参考视频最多 3 个`。

量时长用 ffprobe；便携版 ffmpeg 常缺 ffprobe，回退解析 `ffmpeg -i` 的 Duration 行
（`app/media/probe.py`，和 `app/video/qa.py` 同样处理）。

#### （3）内联不是免费的：体积会膨胀

base64 比原文大 1/3，且一次请求里所有参考素材加起来都算在同一个 body 里。
30MB 的 body 服务商能收，但再大就不行了。`app/media/inline.py` 给了两道闸：
单条 ≤25MB、单次任务合计 ≤60MB。超限的条目**跳过**（宁可少带一条参考，
也不让整批出片失败）。

#### （4）内联发生在哪一步

不在落库时——落库存的是素材 id 对应的回放地址（小、可反查）；
在**喂给服务商之前**（`app/api/video.py::_job_params`）才展开成 data URI。
理由：重试、批量重投都从库里读同一份 params，落库时就内联会让 DB 里躺一份
几十 MB 的 base64，而且素材换了还得重算。

**教训**：凡是「本机文件要喂给云端服务商」的场景，先探清服务商到底吃哪种
形态（本项目这条是：只吃 http(s) 或 data:base64）。别假设「给个 URL 就行」——
本机回放地址是相对路径，服务商在云上根本拉不到。

#### （6）任务失败 ≠ key 坏了：poll 把厂商事实错判成鉴权问题（已修，见 0d6514b）

- **症状**：批次里一镜 failed，报错是「MiniMax 查询失败：所有 key 都不可用
  （sk-api… HTTP 200；sk-cp-… HTTP 200）」——两把好 key 双双进 600s 冷却，
  真实死因（task.error: 1008 insufficient balance）完全看不见。
- **根因**：两个错叠在一起。① `_send` 的 body 扫描在**响应全文**里找 "1008"，
  而任务对象 `{"task":{"status":"failed","error":{"code":"1008"}}}` 里恰好带着它——
  查询带回任务 = 厂商在说事实，不该过 key 判据；② `poll_job` 从**外层** `data.error`
  取失败原因，但 V2 把它嵌在 `task.error` 里，取不到就永远是空的。
- **解法**：`_has_task_object` 先于 key 判据；error_message 从 `task.error` 提取。
  反向护栏测试锁住：不带 task 的纯错误信封（1004）仍须判 key 坏。
- **教训**：多 key 轮换的判据只该看「这次请求是不是 key 的问题」，
  别把响应 body 当字符串扫错误码——业务事实里也藏着同一段数字。

#### （5）顺带修掉一个旧坑：用户上传的定妆照根本送不出去

- **症状**：定妆照用 AI 生成时一切正常；换成用户自己上传的图片，这一镜就出不了片。
- **根因**：资产库把上传的图片记成 `/api/assets/file/xxx`（本机相对地址），
  原样塞进 `reference_images` 喂给服务商 → 被判 invalid param。
  同一类地址问题，但**只影响用户上传的图**——AI 生成的定妆照是厂商给的公网 URL，所以看不出来。
- **解法**：内联那一步同时管 `reference_images` / `reference_videos` / `reference_audios`：
  凡是从本机回放端点出来的地址，一律转成 data URI。

### 5.30 只在崩溃路径触发的 TypeError：数据库读回的 datetime 是 naive（2026-11-10）

> 背景：加出片 claim 表（D-38）时，空 claim 的「10 分钟超时自愈」判断
> `(_now() - existing.created_at).total_seconds()` 在真实提交路径上从没被执行过，
> 测试全绿；是专门伪造一条 11 分钟前的空 claim 才把它逼出来。

- **症状**：崩溃自愈路径一跑就 `TypeError: can't subtract offset-naive and offset-aware datetimes`。
- **根因**：`_now()` 返回 **aware**（带 tzinfo）的 UTC，但 SQLAlchemy 的
  `DateTime` 列**不带 timezone**，从库里读回的 `created_at` 是 **naive**。
  aware − naive 直接抛错。SQLite 和 Postgres 都这样——别指望换后端能掩盖。
- **解法**：新增 `_utcnow_naive()`（`datetime.now(UTC).replace(tzinfo=None)`），
  所有「和时间列做减法/比年龄」一律用它。**写进库用 `_now()`，从库里读出来比较用
  `_utcnow_naive()`**，两套语义分开，别混。见 `app/store.py`。
- **教训**：**「只在异常/恢复路径上跑的代码，测试必须显式构造那条路径的状态」**。
  超时判断这种代码，正常提交永远走不到，常规用例测不出——要么用 `freezegun` 冻时间，
  要么像这里一样直接改 `created_at` 造一个超期的行。恢复逻辑不测等于没写。

### 5.31 显示名退化成拼音：名层护栏，不靠提示词自律（2026-11-10）

> 背景：完整跑实测，deepseek 把故事圣经的人物/地点 name 写成
> `lin_ran` / `door_person` / `loc_main`（id 形态的 slug）。名字是下游一切
> 的地基：选角拿它给音色起中文名、DP 把它写进提示词人物字典、资产库按它
> 显示——一处退化，整条链跟着英文。用户拍板「老毛病从根本上修复」。

- **症状**：剧本角色叫 `lin_ran`，成片台词、资产库、音色命名全跟着歪。
- **根因**：deepseek 把「JSON 里字段该长什么样」的惯性串染到显示名上——
  它给 id 用拼音很规矩，但 name 本该抄原著原文，它照抄了 id 的构词法。
  提示词喊「用中文名」治不了根本（这正是模型格式遵循弱点（见 5.11/5.12 系列）清单里的一员）。
- **解法（按 D-05：能用规则判的绝不用 LLM 判）**：
  `app/pipeline/generation.py` 的 `_bible_naming_issue` —— 中文输出时
  任何 name 不含汉字（`一–鿿`）即判退化，**带修正指令有界重跑一次**
  （与 `_is_degenerate_plan` 同一模式）；重跑仍不合格就停手、在 artifacts
  记 `naming_degraded`，`generate_script` 工具如实把它说给用户，不装没事。
  判定只在 `language` 以 zh 开头时生效——英文原著用拉丁名是正当的，不许误伤。
- **教训**：对格式遵循不稳的模型，**关键不变量要代码裁决 + 有界重试 + 失败如实上报**，
  三段缺一不可；只加提示词句子 = 把同一张彩票再刮一次。

### 5.32 首页输入框直聊：后端走通了，前端断在三处（2026-11-10）

> 背景：用户从首页输入框直接发「帮我做成短剧：…」，**毫无反应**。
> 浏览器实测：Agent 其实真调了 `create_project`、作品建出来了——断的是流程设计。

- **断点①（发送后无反馈）**：回复推进 `store.messages`，但首页（`mode=home`）
  根本不渲染对话流。用户看到文字被清空、页面纹丝不动，以为没发出去。
  **修**：首页 `send()` 里 `store.mode = 'workspace'`——发送即进对话视图。
- **断点②（第二轮必 404）**：done 事件回传 `thread_id="global"`（伪 id），
  前端把它当 `conversation_id` 带上，第二轮打到后端查不到 id 为 "global" 的
  对话 → 404「对话不存在」。**修**：见迁移。
- **断点③（历史孤儿）**：首轮消息落在全局线程，进那部作品看对话是空的。
- **一个动作修②③**：`_finalize_turn` 里，本轮从全局线程开聊、Agent 中途建了
  作品 → `store.move_chat_messages(GLOBAL_THREAD, 新作品默认对话)`，
  done 回**真实 conv id**。迁整段是刻意的：本轮上下文里模型看到的就是这些历史，
  「把作品聊出来」的来路全在这段对话里。
- **配套（同轮兜底）**：`AgentLoop._invoke_tool` 的 project_id 兜底从
  构造时死值改为**动态读 collector**——`create_project` 之后同一轮里的
  `generate_script` 等工具漏传 project_id 也能兜到新作品。
- **教训**：**入口即流程**。输入框挂在首页，就必须把「首页发送之后发生什么」
  设计完整：即时反馈、线程归属、下一轮接续，少一环用户看到的就是「没反应」。

### 5.33 定妆照画风漂移的五张五种画风：锚串味 + 质检门空转 + negative 没接线（2026-11-10）

> 背景：用户看资产库截图发现「画风都不一样，也和角色不太匹配」。
> 孔乙己（anime 画风）5 张定妆照实测：厚涂/写实照片感/Q 版表情包/黑白素描/精致美型——五种画风。

- **根因①：锚就串了味**。美术指导（deepseek）给 anime 项目写出
  「真实皮肤毛孔与细微油光、雀斑、风吹日晒纹理」——这是**实拍锚**。
  提示词里明确要求动画该写发色瞳色，模型没听话，代码层没有裁决。
  锚是定妆图 prompt 与 DP 提示词的唯一来源，矛盾指令喂进文生图 → 四不像。
  **修**：`ArtDirectorAgent._style_mismatch` 确定性检出（非写实画风 + 单角色
  ≥2 个实拍纹理词）→ 带修正指令有界重跑一次（同剧本层名护栏打法，D-05）。
  实测重跑一次即洗白：新锚全部改写成「赛璐璐上色、暖米色平涂、瞳色深褐」。
- **根因②：质检门是开的**。`_QA_CHARACTER` 只拦「构图 / 东亚面孔」，
  还特意写了「服装细节出入不算 FAIL」——画风不符照样放行。
  **修**：加 C) 画风明显不符（同一大类的流派差异不拦，避免 D-13 的全员误杀）；
  要求倒数第二行给依据，任务步骤详情里看得见为什么重来。
  **踩坑**：判定解析必须**只看最后一行**——全文找子串时，依据里的
  「不合格」字样会把 PASS 翻成 FAIL（首轮实测 22/23 全 FAIL 就是这么来的）。
- **根因③：negative 字段从来没接线**。每个画风 spec 都定义了 `negative`
  （anime 排「真人、写实照片」），但 `ImageClient.generate()` 压根没这个参数——
  死配置。cogview 默认出实拍质感，没有反向词压制。
  **修**：`generate(prompt, negative_prompt=…)` 接进 CogView/OpenAI 兼容客户端
  （实测支持）；构图排除项（多人/背面/远景）也进字段**不写正文**——
  否定句写正文会反噬（[[video-prompt-adherence-budget]] 的四杀手之一）。
  实测画风类 FAIL 从「主要死因」降到偶发。
- **剩下的诚实结论**：cogview-3-flash 对「单人正面半身」的构图服从仍不稳
  （一次通过率约 1/3，重试后 3/6 合格、3 张保留不合格末张）。这是模型能力上限，
  不是流程 bug——流程的职责是**检出来 + 如实标注**，不是假装全过。
  质检不过时保留最后一张并在任务详情里写明，用户看得见、可点名重生成。

**教训**：「质检门存在」≠「质检门有效」。判据要覆盖真正的致命漂移（画风是
一致性第一位的），解析要认死格式（最后一行），排除项要走结构化字段而不是正文否定句。

### 5.34 BYOK 的应有之义：拿用户的 key 去厂商问一遍，而不是硬编码几个（2026-11-10）

> 背景：用户问「声音库这么少，这些声音是调模型生成的还是直接找的？」。
> 真相是第三种：我们硬编码了 8 个 MiniMax 音色 ID。而 BYOK 产品里，
> 用户拿自己的 key，本该看到**这把 key 可用的全部**，自己勾选。

- **发现接口是真的**（实测）：`POST /v1/get_voice {"voice_type":"all"}` 返回该账号
  **303 个系统音色**。对话模型同理——`GET {base}/models` 对 OpenAI 兼容厂商有效，
  而且现有「测试连接」早就在调它了，只是把结果当一次性提示扔掉，没变成可勾选清单。
- **三个必须如实处理的边界**：
  1. **厂商不返回性别**。音色只有 `{voice_id, voice_name, description:[], created_time}`，
     性别只能从 `male-`/`female-` 前缀推。实测 303 个里只有 8 个带前缀（正好是我们硬编码那批），
     其余 287 个（聪明男童/俊朗男友/病娇弟弟…）语义上有性别但推不出来 → 归 other。
     **other 绝不进自动选角池**（猜性别出过「男主开口是女声」的事故，见 5.19），
     只出现在下拉里供人工指定。
  2. **视频模型发现这条路是断的**。MiniMax `/v1/models` 只返回 M 系列对话模型，
     不含 `MiniMax-H3`——所以视频模型保持手填，UI 如实说明，不假装能发现。
  3. **重新发现 ≠ 重新勾选**。`upsert_voice_pool` 只补 name/gender 不动 enabled，
     否则用户点一次刷新就把勾掉的音色全冲没了。
- **零破坏的注入方式**：`cast_script`/`voice_options`/`build_library` 加 `voice_pool` 参数，
  **空池时回退内置 8 个**（按画风排序）。老项目与全部测试跑在「没勾选」上，行为一字不变。
  勾选的排池首，自动选角先拿勾的——这才是「识别 → 勾选 → 用的就是勾的」的闭环。
- **踩坑**：`voice_options` 一度按 gender+name 重排，把「勾选在前」的优先级打乱了。
  池的顺序（build_voice_pool）就是优先级，**展示层不许再排序**；前端自己按 gender 分组即可。
- **发现音色显示名不能漏**：303 个音色不在内置 VOICE_LABELS 里，配音进度/成片报告
  会显示 `clever_boy` 这种原始 id（D-24 禁工程黑话）。解法：`Casting.voice_names` 带
  voice_id→厂商名，`CastMember.label_hint` + `Casting.label_for()` 统一解析，
  配音链路（dubbing/finalize）也传同一份池。

**教训**：BYOK 不是「让用户填 key」就完了——key 是用户的，能力清单也是用户的，
该由厂商告诉用户有什么，而不是我们替用户决定「8 个够了」。

---

### 5.35 CI 沉默的红：push 后 0 秒失败，本地全绿（2026-11-10）

> 背景：推「消耗概览」那批时顺手拉 CI 状态，发现一片 failure——
> 追下去才知道 **10-28 起（最后一次绿是 10-12），每次 push 都在 0 秒红**，没人看过一眼。
> 修复是分层的：门一开，抓到一层债。

#### （1）根因：job 级 `if` 读了 `secrets` —— 工作流编译期直接失败

- **症状**：run 0 秒 completed/failure，`jobs` 与 check-runs **全空**（连 checkout 都没跑）；
  本地 `yaml.safe_load` 却完全合法。GitHub 只给一句 "This run likely failed because of a
  workflow file issue"。**YAML 合法 ≠ Actions 语法合法**——Actions 的 job 级 `if:` 不允许
  `secrets` 上下文，整份文件在编译期被拒，这就是"没有 job 的红"。
- **引入**：49957a0（10-28 加 evals 框架）写的 `if: ${{ github.event_name == 'push' || secrets.EVAL_API_KEY != '' }}`。
  语义也写反了——本意是「PR 拿不到 secret 所以不跑」，写成了「push 无条件跑」。
- **解法**：GitHub 官方模式——前置一个 `eval-gate` job，在 **step 级**（step env 里读 secret 是合法的）
  把「有没有 key」写成 output，`eval-llm` 的 if 只看 `needs.eval-gate.outputs.has-key`。

#### （2）门开了，ruff 首跑就抓到存量债

工作流修好后第一个真正执行的新步骤 = 587d6c1 加的 ruff 致命集，
当场抓出 BYOK 音色提交（a01b201，11-10）写的 `app/api/cast.py` 两处 B904（except 里 raise 不带 from）。
那两处新代码写的时候门禁正在沉默地红——**不是新代码突然变坏，是它全跑在"CI 不存在"的世界里**。

#### （3）裸 pytest 和 `python -m pytest` 不是同一个 sys.path

ruff 绿了之后，「跑测试」又红：`ModuleNotFoundError: No module named 'evals'`（tests/test_skill_eval.py）。
- **根因**：本地我们惯用 `python -m pytest`——`-m` 会把仓库根塞进 sys.path，所以非安装包的
  `evals` 也能 import；CI 用 `uv run pytest tests`（裸 pytest，rootdir 不进 sys.path），
  `app` 是安装包没事，`evals` 不是。本地怎么跑都绿，CI 必红。
- **解法**：`pyproject.toml` 的 `[tool.pytest.ini_options]` 加 `pythonpath = ["."]`，
  让任何启动方式（CI / IDE / 裸命令）行为一致，而不是赌调用姿势。
- **验证**：本地用与 CI 相同的裸 `.venv/Scripts/pytest.exe tests -q` 先复现红，
  加配置后复现绿（462 passed），再 `-m` 对跑一遍确认两种启动方式一致。

**教训（最贵的）**：「CI 红了没人看」比「没配 CI」更糟——红久了大家就默认它该红，
新加的门禁等于没上。以后新加任何门禁（ruff 就是例子），上线当天必须确认它真的跑过一次；
每次 push 扫一眼 `gh run list`，要么就别装。

---

### 5.36 「空 env」测试不空：Settings 的 property 合并多个字段，覆盖一个不够（2026-11-10）

> 背景：写 BYOK 种子测试（`test_byok_seed.py`）时，有一条要验证「env 里没 key → 什么都不种」。
> 测试里 `Settings(MINIMAX_API_KEY="", …)` 显式把主 key 置空了，结果 seed 还是种出了一行
> `tts/minimax`——"空 env"根本不空。

- **症状**：`assert acts == []` 失败，`acts` 里多一个 `tts/minimax`；`video/minimax` 却没种出来
  （说明 `minimax_keys()` 读 os.environ 是空的，问题只在 Settings 这一侧）。
- **根因**：`Settings.minimax_key_list` 是个 **computed property**，它 merge 了
  `minimax_api_key` **和** `minimax_api_key_fallbacks` 两个字段。测试只把前者置空，
  后者仍被 pydantic-settings 从 `.env` 的 `MINIMAX_API_KEY_FALLBACKS` 读了进来——
  于是 `resolve_tts` 拿到一把真实 key，种子就"尽责"地入库了。
  这是 5.25「三条配置路径」的延伸：**property 汇聚多源时，单字段覆盖 = 没覆盖。**
- **解法**：测试的 `_settings()` helper 里把 `MINIMAX_API_KEY` 和 `MINIMAX_API_KEY_FALLBACKS`
  **两个字段都显式置空**；video 分支读 os.environ 的部分，用 `monkeypatch.delenv` 清掉
  `MINIMAX_API_KEY/KEYS/FALLBACKS` 三个环境变量才算真"空 env"。
- **教训**：给「读取配置的代码」写测试时，先数清楚它读了几条路径（Settings 字段 ×N +
  os.environ ×M），**每条都要在测试里显式隔离**，漏一条就会出现"我以为关了其实没关"的假绿。
  这类坑不会在实现里暴露（生产环境本来就该读 .env），只在测试里暴露——所以测试必须
  从"它到底读哪些源"出发设计，而不是从"我传了哪些参数"出发。

### 5.37 账单解剖：一次充值烧穿背后的五个坑（2026-11-12）

> 背景：孔乙己体检连跑两版，50 元充值一天烧穿（余额 -4.28）。逐笔查
> `GET /v2/query/video_generation/{task_id}` 的 usage 字段才发现：**我们
> 对 MiniMax 计费模型的理解从头到尾是错的**。这一节五个坑都有代码修复
> 与测试锁行为，记录的是「为什么会错成这样」。

#### (1) 回调指纹含 video_url：签名直链每轮询都变，重 roll 触发两次

`VideoJobManager._update` 用 `(status, video_url, error)` 做「状态有变化」
的指纹。MiniMax 成片是**带签名的临时 OSS 直链**，每次轮询 query 参数都
不同 → 已成功任务被反复判成"状态变化" → on_complete 反复触发 → 质检
对同一任务重 roll 两次「第 1 次」（日志里 `vjob_682b055ab2e3` 出现两次），
每次重 roll = 一次真金白银的重复生成。
- **修法**：指纹降为 `(status, bool(video_url), error)`——url 首次出现仍
  触发一次，签名刷新不再触发；`reroll_if_needed` 再加幂等保险（params
  已有 `qa` 结论的任务直接返回）。
- **教训**：任何「用值变化当事件」的指纹，都要先问这个值稳不稳定。
  URL 尤其危险——签名/时效/CDN 都会让它变，而语义上根本没变。

#### (2) 片长预算只是「回喂批评」：批评可以不理，钱不能不理

导演拆镜超预算不是一次事故是规律：孔乙己 2685 字，三次跑拆出
45 / 39 / 53 镜（目标 24-30 镜 / 120s），每次都「如实报告超限但照交」。
`_validate_continuity` 的预算检查只进 critique 循环，模型三轮不理就
带病放行。
- **修法**：`enforce_duration_budget` 确定性闸门——单镜封顶 6s → 删壁纸
  （function 不在三工作）与无台词镜头（带台词最后动，防丢配音）→ 修
  悬空 chain_from。两条出口（校验通过 / 迭代耗尽）都必须过闸。
- **效果**：同素材 v2 拆 44 镜 → 闸门压到 25 镜 / 120s，平均 4.78s/镜。

#### (3) 计费公式：input_seconds 与输出同价，参考视频比正片还贵

真实 usage：一镜 ¥8.3 = 输出 6s×¥0.50 + **输入视频 9s×¥0.50** + 4图×¥0.20。
三个此前全错的认知：
- 接力参考视频按**秒**与输出同价计费——整条 4-6s 前镜成片喂进去，
  输入比输出贵一倍。衔接只需要前镜**结尾**：`clip_tail_data_uri` 投递时
  裁尾 2.5s 内联（ffmpeg 下载+裁+转码，失败回退整条），输入 9s→4s。
- 图片输入**每任务前 5 张免费**，超出才按张（¥0.20，Lite ¥0.13）。
- 秒价按**档位×分辨率**双维（H3: 768P ¥0.50 / 2K ¥0.80；Highspeed
  ¥0.26/0.40；Lite ¥0.16/0.26），不是模型名单价。
- **修法**：`_TIERS` 档位表 + `estimate_cost_real`（回填锚之后算，回填前
  参考数量未知必低估）+ 本地调试默认 Lite。典型镜头 ¥8.3 → ¥1.2。
- **另**：文档确认 first/last_frame 与 reference_* **互斥**、audio 项必须
  带 `role=reference_audio`、Max 不支持 4s——`_build_content`/`create_job`
  加了护栏（混用时参考优先 + 警告；分辨率/时长按档位收敛）。

#### (4) 续跑只认 succeeded：在途任务被重复提交，双份钱

进程崩在批次中途时，服务商侧任务还在跑（照样计费、sweeper 会接回），
但 DB 状态是 generating——N2D 续跑的批准名单只剔 succeeded，把在途镜头
重新批准提交了（账本里 `shot_001_001` 两条 succeeded）。
- **修法**：名单扩为「跳过 succeeded + pending/queued/generating」；
  failed/cancelled 仍可重提（合法重出片）。

#### (5) 用户数据跟着代码目录走：两个项目文件夹互相认错

11/2 重新 clone 出 `wenju` 副本后，数据散在两个 `data/` 里——在哪个目录
跑服务就只能看见那个目录的作品，「改名了」的错觉由此而来（实际文件夹
从未改名，是复制）。
- **修法**：`app/home.py` 产品主目录 `~/.wenju/`（仿 Codex：app.db +
  workspace/ + runs/），懒迁移只认正名库文件、只补空缺不覆盖，旧库保留
  可回退。项目文件夹从此零用户数据。测试用 `WENJU_HOME` 钉临时目录隔离。

**排查手册增补**：余额烧得比预期快 → 先查 usage 的 input_seconds（5.37(3)）
再看同一 shot 是否多条任务（5.37(1)(4)）；导演镜数超预算 → 看闸门日志
「压到 N 镜」（5.37(2)）。

### 5.38 矫枉过正：防穿模的「静止姿态」把主角饿成了 PPT（2026-11-12）

> 背景：v3 全片跑完，成本与良率都达标了（¥2/镜、83% 一次过），用户看片后
> 一句「83 秒全在重复一个 3-4 秒的动作」把另一层问题钉死。

- **症状**：24 镜里 22 镜的 `action` 只写了 2 段，且全是面部/手指微动
  （「嘴唇抿成一条线」「右手仍按在柜台上」）+ 21/24 固定机位——4 秒静态
  画面里只有一个微动作，视频模型唯一能做的就是把它循环播放，整片像 PPT。
- **根因（规则设计的钟摆错误）**：`action_budget` 只有**上限**（防动作过载
  穿模），没有下限；5b 群戏降级规则（「其余人物改静止大姿态」「接触只保留
  一步」）本意管背景人物，模型扩大执行到**主角**——防畸变的代价是没人动。
  批评回喂也再次失效：镜头语言审计（静态>75%）v3 只回喂了一轮，模型交回
  21/24 静态——**回喂管不住的，和片长一样，只能靠确定性校验**。
- **修法**：① SKILL.md 动作预算改为「上下限都查」：每镜 ≥3 段、其中至少
  1 段位移级大动作（起身/绕过/跨步/俯身…），情绪翻译成位移；明确
  「静止大姿态只给背景与次要人物，主角永远要有动作弧线」。
  ② 校验器加两条硬检查：`units < 3` 打回；三段全无 `_MOVEMENT_RE`
  位移词打回（词表客观可查，不靠模型自觉）。③ 存量测试样例同步合规。
- **教训**：**每条「防过载」规则都要配一条「防不足」的对偶检查**——
  上限管住后模型会贴着地板写。以及：同一批评回喂一轮不改，就该升级为
  确定性校验，别再信「如实报告」。（镜头语言审计 v3 实测只把特写压到
  33%——阈值内但观感仍偏紧，位移下限是补在动作层的第二道。）

---

## 六、排查手册（症状 → 先查什么）

| 症状 | 先查 |
|---|---|
| 首页输入框发了没反应 | 发送即切工作区（HomeComposer.send）；第二轮 404 看 done 是否回真实 conv id，见 5.32 |
| 人物/地点名是拼音 slug（如 lin_ran） | 名层护栏日志（`名退化…重跑故事圣经`）与 artifacts.naming_degraded，见 5.31 |
| 剧本生成 `mode=local-fallback` | 服务端日志里的 validation error（`app/pipeline/generation.py` 的容错模型是否漏了字段） |
| 提示词里缺少某个锚 | 风格指南是否生成（`projects.video_style_json`）；锚的键是否对得上实体 id（老数据是名字键，`assets.lookup` 两种都认，见 5.23）；`normalize_id` 是否又出双前缀 |
| 角色前后不像同一个人 | 五份资产里该角色的键还对不对得上（名字键 + 改过名 = 静默丢锚，见 5.23）；`build_library` 里 `appearance` 是否为空 |
| 有人在说话，画面却对着别人的手 | 该镜 `dialogue_ref` 是否是编造的 id（见 5.22）；配音找不到台词会静默跳过 |
| 视频任务失败 | 错误信息里是 `402/insufficient balance`（余额）还是 `400`（内容审核）还是 `Invalid header`（provider key） |
| 任务长时间 `generating` | 直接拿 `external_task_id` 查服务商真实状态，再对照状态映射（子串陷阱） |
| 后台任务没动静 | 用 `GET /api/tasks` 确认任务是否真的创建（别信回复文本） |
| 分镜切得很碎 | 上游是否把多章塞成了一场戏；导演提示词是否被改回了「覆盖式拍摄」 |
| 参考图质检永远不合格 | 判定标准是否把「水印 / 细节数量」也算 FAIL；角色是否戴面具（东亚面孔那条要写例外） |
| 视觉质检报 `Failed to download image` | 图片 URL 是否失效；改用 base64 data URI |
| 成片合成接口 500 且报 `AttributeError` | 新单例只在根 `deps.py` 加了，忘了 `app/api/deps.py`（见 5.9(1)） |
| 配音永远失败但视频能出 | `.env` 没落进 `os.environ`，TTS 回落链断了（见 5.9(2)） |
| 字幕/配音整体后移、末尾对不上 | 用了计划时长而非实测时长；是否走了逐镜校准（见 5.9(3)） |
| 成片时长恒为 0 | 便携 ffmpeg 缺 ffprobe；是否走了 stderr 兜底（见 5.9(5)） |
| 前端没有流式效果 | `build_chat_model()` 少了 `streaming=True`（见 5.10(1)） |
| 工具收到的参数是空的 / 半个 JSON | 流式分片没聚合，直接读了 `chunk.tool_calls`（见 5.24(1)） |
| 对话里模型说「我没有这个工具」 | 工具没绑上，或 dict 消息转 LangChain 时工具调用结构不对（见 5.24(2)） |
| 改了 .env 但模型/Key 没变 | ① 入口没调 `bootstrap_env()`；② 界面偏好压过 .env（看 `/api/status` 的 source）；见 5.25 |
| 备用 key 不生效 | `os.environ` 里没有 → 入口没 bootstrap（见 5.25(1)） |
| 流式 token 一个都不出来 | 用 `type == "ai"` 过滤 chunk，应改 `isinstance`（见 5.10(2)） |
| 模型报「已生成 ver_xxx」但版本不存在 | 编造护栏没触发，或本轮确实调过工具；查 `_CLAIM_PATTERNS`（见 5.11(2)） |
| 长篇选段只生成了开头一小段 | 选段超单次容量（约 1.2 万字），需切小（见 5.11(1)） |
| 技能文件明明在、模型从来不用 | `audience` 是否拼对（`crew`/`main`）；main 技能是否真的进了 `system_prompt()` 的目录（见 5.14(3)） |
| 模型没加载细则就直接动手 | 提示词的排序规则是否被改回了软要求「进入领域前先加载」，要按**动作**写（见 5.14(2)） |
| `load_skill` 报「没有这个技能」 | 是不是让主 Agent 去加载了剧组技能；或技能名拼错（返回里带可用清单） |
| eval 报告「通过」但一条都没跑 | 先看 `blocked_total`：`402 Insufficient Balance` 会让全部 case 被挡住（见 5.15(4)） |
| 同一句在提示词里出现两遍 | `dp.py` 的 `_covers` 是否被改回比前缀；`audit_video_prompt` 是否报了「同一句写了两遍」（见 5.15(5)） |
| 同一场的光忽明忽暗／脸光乱跳 | `LNG-04`（分镜 `lighting` 与风格指南光源冲突）、`DPT-13/14`（提示词换光源/翻光位）（见 5.15(2)） |
| 界面能开、点一下就一直转圈 | 数据库没起。看 `connect_timeout`（`DB_CONNECT_TIMEOUT`）与 503 的说明文案（见 5.15(2)） |
| 控制台一条 favicon/品牌资源 404 | 只挂了 `/assets`；`public/` 里的文件在 dist 根目录（见 5.15(1)） |
| 首屏右侧一片死黑 / ORB 拦截 | `WelcomeHero.vue` 的外链背景图；应走本地 `brand/hero-texture.svg`（见 5.15(4)） |
| 下载的剧本文件名出现 `vver_` | 版本号被拼了两次前缀（见 5.15(3)） |
| 视频/配音全挂，报 `402 insufficient balance` | 那把 key 欠费。多配几把到 `MINIMAX_API_KEY_FALLBACKS`；看 `ring.snapshot()` 谁在冷却（见 5.16） |
| 换了 key 后仍查不到视频进度 | 任务属于另一个账号：查询要回到提交它的那把 key（`_TASK_KEYS`，见 5.16） |
| 一次参数写错后所有 key 都不好使 | 判据把 400/2013 当成了 key 失效，误冷却了（见 5.16 判据表） |
| 成片两句字幕叠在同一秒 | 只渲染了部分镜头，台词却按整场对齐（见 5.17） |
| 成片整片语速飞快 | 同上，配音在 1.6 倍速兜底；台词总跨度超片长（见 5.17） |
| 有人说话却没声音 | TTS 撞限流被跳过；看 `[配音] 触发限流…重试` 日志（见 5.17） |
| 男角色开口是女声 | `assign_voices` 的男女交替兜底（已废弃）；现在查资产库里该角色的 `voice_casting` 与 gender（见 5.19） |
| 同一场几个角色声音一样 | 音色池溢出到池尾，或角色名与资产库键对不上；看 `[配音] 选角：…` 日志（见 5.19） |
| 成片音色和资产库里改的不一致 | 美术指导是否用 `set_video_style` 覆盖掉了 `voice_casting`（应 merge，见 5.19） |
| 「按场景节奏取值」从来不生效 | `VideoVersion` 没有 `pacing` 字段，`getattr` 恒为 None（见 5.19） |
| 导演报「无法解析 JSON 响应」且响应以 ```json 开头 | 输出被截断。看 `MAX_SCENES_PER_BATCH`、`salvage_objects` 是否被绕过（见 5.21(1)） |
| 拆出的镜头数是成片时长的 2–3 倍 | 先数剧本对白字数：>264 字必然拆爆。看 `_dialogue_budget_issues` 的 warning（见 5.21(2)） |
| 批量出片整批 429 | 先分清是限流还是额度用尽（2056）。`ring.snapshot()` 看哪把 key 在冷却（见 5.21(3)） |
| 主 key 429 但备用 key 明明能用 | `key_failure()` 是否把 429 当成非 key 错误（见 5.21(3)） |
| 非流式对话接口 500，界面却正常 | 流式路径不经过它；查 `chat_once` 的参数转发（见 5.28(1)） |
| Agent 说「没有这个工具」/ 状态答错 | 模型漏传 `project_id`；看工具轨迹里 args 是否为空（见 5.28(2)） |
| 上传素材报「不支持的格式 .png」 | 前端没分流图片/视频端点（见 5.28(3)） |
| 「测试连接」永远成功 | 它只查 key 非空，没真调厂商接口（见 5.28(4)） |
| 点某个页面整个空白、控制台无报错 | 模板里的组件漏了 import；DOM 里出现小写标签就是它（见 5.27(1)） |
| 刷新后正常、流式中步骤面板是空的 | SSE 事件体没有 `type`，前端要自己补（见 5.27(2)） |
| 接口时好时坏、返回了别的应用的文档 | 8000 端口被两个进程同时绑定，内核轮流分发（见 5.27(3)） |
| 后端「自己退了」，退出码 0 | 启动命令接了 `tail`，管道读满把 uvicorn 带走了（见 5.27(4)） |
| 出片报「参考视频 X 时长 Y 秒」 | 服务商只收 2–15 秒的参考视频；入库时量过，见 5.29(2) |
| 出片报「must be http(s):// or data:...;base64」 | 传进去了本机回放地址；参考素材要内联，见 5.29(1) |
| 声音库只有几个音色 | 没点「声音库 → 从厂商刷新」；发现≠勾选，勾了才进自动选角池，见 5.34 |
| 定妆照画风不对/角色不像 | 锚是否串了实拍纹理词（美术指导日志「画风串染」）；质检依据行；见 5.33 |
| 出片报「所有 key 都不可用（… HTTP 200…）」 | 其实是任务级失败（欠费等）被 body 扫描误判成 key 坏；查厂商任务原话，见 5.29(6) |
