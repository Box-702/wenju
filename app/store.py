# =====================================================================
# store.py —— 业务数据持久化（SQLAlchemy + Postgres，可回退 SQLite）
#
# 保存三类对象：
#   Project       作品（标题、类型、语言、原始文本、指向当前版本）
#   ScriptVersion 剧本快照（内容为 JSON，支持父子版本链）
#   AgentRun      Agent 运行记录（计划、patch、状态、审阅决定）
#
# 这个「应用层存储」与 LangGraph 的 checkpointer 是两层不同的东西：
#   - 本文件：保存「业务数据」——项目 / 版本 / Agent 运行记录本身（默认 Postgres）；
#   - checkpointer：保存「图执行到哪一步」的状态，用于中断后恢复（见 graph.py）。
# 两者配合才能实现「页面刷新后审阅建议还在、接受后生成新版本」。
#
# 驱动选择：通过 DATABASE_URL 前缀自动判断。
#   - postgresql+psycopg://...  -> Postgres（生产 / Docker 默认）
#   - sqlite:///...             -> SQLite（本地开发 / 自动测试，无需外部服务）
# 这保证了在没起 Postgres 的机器上也能单测核心逻辑。
# =====================================================================

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from sqlalchemy import ForeignKey, LargeBinary, create_engine, desc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .domain import Script

#: Postgres 连接超时（秒）。libpq 默认「一直等」，数据库没起来时请求会永久挂住，
#: 所以必须给一个上限：宁可 5 秒后报错，也不要让界面无限转圈。
_DB_CONNECT_TIMEOUT_SEC: int = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))


def gen_id(prefix: str) -> str:
    """生成带前缀的短 id（如 ``run_xxx``）。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json_load(raw: str | None, default: Any = None) -> Any:
    """安全解析 JSON 字符串, 失败返回 default。"""
    if not raw:
        return default if default is not None else []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []


def _now() -> datetime:
    return datetime.now(UTC)


def _as_datetime(value: Any) -> datetime | None:
    """把 datetime 或 ISO 字符串统一成 datetime；解析不了返回 None。

    SubAgentTask.to_dict() 输出的是 ISO 字符串，落库时要还原成 datetime
    才能写进 DateTime 列（否则会被静默丢弃）。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _ensure_sqlite_dir(database_url: str) -> None:
    """SQLite 文件库需要父目录存在，否则建表会失败。"""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path_text = database_url[len(prefix) :]
    if not path_text or path_text == ":memory:":
        return
    Path(path_text).parent.mkdir(parents=True, exist_ok=True)


def connect_args_for(database_url: str) -> dict[str, Any]:
    """按驱动给出 create_engine 的 connect_args。

    - SQLite：关掉单线程检查（FastAPI 线程池会跨线程用同一连接）；
    - Postgres：给 connect_timeout。libpq 默认「一直等」，数据库没起来时
      每个请求都会永久挂住——前端只能看到转圈，连报错都拿不到。
    """
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    if database_url.startswith(("postgresql", "postgres")):
        return {"connect_timeout": _DB_CONNECT_TIMEOUT_SEC}
    return {}


class Base(DeclarativeBase):
    """ORM 基类。"""


class Project(Base):
    """作品。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    adaptation_type: Mapped[str] = mapped_column(default="short_drama")
    language: Mapped[str] = mapped_column(default="zh-CN")
    raw_text: Mapped[str] = mapped_column(default="")
    notes: Mapped[str] = mapped_column(default="")  # 编剧圣经 / 设定备忘（自由文本）
    status: Mapped[str] = mapped_column(default="ready")
    current_version_id: Mapped[str | None] = mapped_column(default=None)
    # ---- v3.0 视频制作 ----
    current_video_version_id: Mapped[str | None] = mapped_column(default=None)
    video_style_json: Mapped[str] = mapped_column(default="{}")
    # ---- 长篇改编 ----
    # 章节地图：{"chapters":[{index,title,summary,characters,chars}],"total_chars","pseudo"}
    novel_map_json: Mapped[str] = mapped_column(default="{}")
    # 连载进度锚：已改编到的章号（0 = 还没按章选段生成过）
    adapted_until: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    versions: Mapped[list[ScriptVersion]] = relationship(back_populates="project")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="project")
    conversations: Mapped[list[Conversation]] = relationship(back_populates="project")
    video_versions: Mapped[list[VideoVersion]] = relationship(back_populates="project")
    video_jobs: Mapped[list[VideoJob]] = relationship(back_populates="project")

    @property
    def novel_map(self) -> dict[str, Any]:
        return _json_load(self.novel_map_json, {})


class ScriptVersion(Base):
    """剧本快照。内容以 JSON 存储，可追溯版本链。"""

    __tablename__ = "script_versions"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_version_id: Mapped[str | None] = mapped_column(default=None)
    source_type: Mapped[str] = mapped_column(default="generation")  # generation|agent_adaptation|import|manual_edit
    label: Mapped[str | None] = mapped_column(default=None)
    notes: Mapped[str | None] = mapped_column(default=None)
    milestone: Mapped[str | None] = mapped_column(default=None)  # draft|candidate|final
    content_json: Mapped[str] = mapped_column(default="{}")
    # ---- v3.0 场景拆解 ----
    breakdown_json: Mapped[str] = mapped_column(default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_now)

    project: Mapped[Project] = relationship(back_populates="versions")

    @property
    def content(self) -> dict[str, Any]:
        """解析后的剧本字典。"""
        return json.loads(self.content_json or "{}")

    @property
    def script(self) -> Script:
        """通过领域模型访问剧本。"""
        return Script.model_validate(self.content)


class AgentRun(Base):
    """一次 Agent 改编运行记录。"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    base_version_id: Mapped[str]
    result_version_id: Mapped[str | None] = mapped_column(default=None)
    user_prompt: Mapped[str] = mapped_column(default="")
    scene_ids_json: Mapped[str] = mapped_column(default="[]")
    plan_json: Mapped[str] = mapped_column(default="[]")
    patch_json: Mapped[str] = mapped_column(default="[]")
    steps_json: Mapped[str] = mapped_column(default="[]")
    status: Mapped[str] = mapped_column(default="pending")  # pending|reviewing|accepted|rejected|applied
    decision_json: Mapped[str] = mapped_column(default="null")
    model: Mapped[str | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    project: Mapped[Project] = relationship(back_populates="agent_runs")

    # ---- 便捷存取序列化字段 ----
    @property
    def scene_ids(self) -> list[str]:
        return _json_load(self.scene_ids_json, [])

    @property
    def plan(self) -> list[str]:
        return _json_load(self.plan_json, [])

    @property
    def patch(self) -> list[dict[str, Any]]:
        return _json_load(self.patch_json, [])

    @property
    def steps(self) -> list[str]:
        return _json_load(self.steps_json, [])

    @property
    def decision(self) -> dict[str, Any] | None:
        return _json_load(self.decision_json, None)


class Conversation(Base):
    """对话（会话线程）。

    可以属于某个项目，也可以是全局独立对话（project_id 为 null）。
    每个对话独立保存消息历史。
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True, default=None)
    title: Mapped[str] = mapped_column(default="新对话")
    # 滚动摘要（JSON）：{"summary": 文本, "covered_count": 已压缩进摘要的消息条数}。
    # 长对话只把摘要 + 最近消息喂给模型，避免掐头丢事实。
    summary_json: Mapped[str] = mapped_column(default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
    project: Mapped[Project | None] = relationship(back_populates="conversations")

    @property
    def summary(self) -> dict[str, Any]:
        return _json_load(self.summary_json, {})


class MediaAsset(Base):
    """项目媒体素材（参考视频等）。

    source=upload / finalize：文件本体存数据库（content bytea），url 为本服务回放
    地址，本地不落盘；历史记录可能仍带 file_path（相对 workspace_root 的磁盘文件，
    回放端点向下兼容）。source=url：直接登记外部公网链接（MiniMax 等服务商要求公网可访问）。
    """

    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(default="video")          # video | image | audio
    name: Mapped[str] = mapped_column(default="")
    source: Mapped[str] = mapped_column(default="upload")        # upload | url | finalize
    url: Mapped[str] = mapped_column(default="")
    file_path: Mapped[str | None] = mapped_column(default=None)  # 兼容历史：相对 workspace_root
    content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)  # 文件本体（DB 存储）
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ChatMessage(Base):
    """对话式 Agent 的消息记录。

    ``thread_id`` 是这条消息所属会话的 id（等于 Conversation.id），
    一个项目下的多个对话彼此隔离；还没有项目的全局对话用 ``global``。
    ``payload`` / ``events`` 保存该条助手消息附带的结构化数据
    （审阅卡片、工具轨迹等），供前端渲染。
    """

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(default="user")  # user | assistant
    content: Mapped[str] = mapped_column(default="")
    payload_json: Mapped[str] = mapped_column(default="[]")
    events_json: Mapped[str] = mapped_column(default="[]")
    # 本条消息附带的图片（[{id,url,mime,name}]）——历史回放与 LLM 上下文都靠它重建。
    images_json: Mapped[str] = mapped_column(default="[]")
    created_at: Mapped[datetime] = mapped_column(default=_now)

    @property
    def payload(self) -> list[dict[str, Any]]:
        return _json_load(self.payload_json, [])

    @property
    def events(self) -> list[dict[str, Any]]:
        return _json_load(self.events_json, [])

    @property
    def images(self) -> list[dict[str, Any]]:
        return _json_load(self.images_json, [])


class ChatImage(Base):
    """对话中上传的图片（字节入库，本地不落盘）。

    与消息解耦存表：前端先传图拿 id，发消息时带上 id 列表；消息落库时把
    {id,url} 快照写进 ChatMessage.images_json。这样多轮对话重建上下文时
    能随时取回原图字节（转 base64 data URL 喂给多模态模型），图片不会
    「聊两句就忘」。
    """

    __tablename__ = "chat_images"

    id: Mapped[str] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(index=True, default="")
    mime: Mapped[str] = mapped_column(default="image/png")
    name: Mapped[str] = mapped_column(default="")
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Memory(Base):
    """用户级三层记忆（替代 RAG 的结构化记忆）。

    kind 决定活多久：preference（长期偏好）> decision（项目内决定）> feedback（对话内反馈）；
    scope 决定谁能看到：global > project > conversation。注入系统提示时按
    全局 → 项目 → 对话的顺序合并，具体决定天然覆盖一般偏好。
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(default="preference")  # preference | decision | feedback
    scope: Mapped[str] = mapped_column(default="global")  # global | project | conversation
    project_id: Mapped[str | None] = mapped_column(index=True, default=None)
    conversation_id: Mapped[str | None] = mapped_column(index=True, default=None)
    content: Mapped[str] = mapped_column(default="")
    source: Mapped[str] = mapped_column(default="user")  # user | auto（从审阅决策学到的）
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)


# =====================================================================
# v3.0 视频制作相关表
# =====================================================================


class ApiProvider(Base):
    """API Provider 配置（LLM / 视频 / 图片 / TTS）。"""

    __tablename__ = "api_providers"

    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(default="llm")  # llm | video | image | tts
    name: Mapped[str] = mapped_column(default="")  # runway / kling / openai 等
    label: Mapped[str] = mapped_column(default="")  # 显示名
    base_url: Mapped[str] = mapped_column(default="")
    api_key: Mapped[str] = mapped_column(default="")
    config_json: Mapped[str] = mapped_column(default="{}")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    @property
    def config(self) -> dict[str, Any]:
        return _json_load(self.config_json, {})


class UserSkill(Base):
    """用户导入的技能（一份带 frontmatter 的 Markdown）。

    内置技能在 app/skills/*/SKILL.md（代码的一部分，跟版本走）；
    用户导入的存这里（用户数据，按 D-23 全存数据库）。
    """

    __tablename__ = "user_skills"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(index=True, unique=True)
    description: Mapped[str] = mapped_column(default="")
    when_to_use: Mapped[str] = mapped_column(default="")
    audience: Mapped[str] = mapped_column(default="main")
    content: Mapped[str] = mapped_column(default="")  # 原始 Markdown（含 frontmatter）
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ModelPreference(Base):
    """用户模型偏好：为每种任务类型指定默认 provider + model。"""

    __tablename__ = "model_preferences"

    id: Mapped[str] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(default="")  # screenplay | shot_design | video_gen | image_gen | tts
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("api_providers.id"), default=None)
    model_name: Mapped[str] = mapped_column(default="")
    params_json: Mapped[str] = mapped_column(default="{}")
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    @property
    def params(self) -> dict[str, Any]:
        return _json_load(self.params_json, {})


class VideoJob(Base):
    """视频生成任务。"""

    __tablename__ = "video_jobs"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version_id: Mapped[str | None] = mapped_column(default=None)
    shot_id: Mapped[str] = mapped_column(default="")
    provider: Mapped[str] = mapped_column(default="")
    model: Mapped[str] = mapped_column(default="")
    prompt: Mapped[str] = mapped_column(default="")
    params_json: Mapped[str] = mapped_column(default="{}")
    status: Mapped[str] = mapped_column(default="pending")  # pending|queued|generating|succeeded|failed|cancelled
    external_task_id: Mapped[str | None] = mapped_column(default=None)
    video_url: Mapped[str | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    cost_estimate: Mapped[float | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)

    project: Mapped[Project] = relationship(back_populates="video_jobs")

    @property
    def params(self) -> dict[str, Any]:
        return _json_load(self.params_json, {})


class VideoVersion(Base):
    """视频版本快照：记录某次完整的分镜/镜头状态。"""

    __tablename__ = "video_versions"

    id: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_version_id: Mapped[str | None] = mapped_column(default=None)
    source_type: Mapped[str] = mapped_column(default="agent")  # agent | manual | generation
    label: Mapped[str | None] = mapped_column(default=None)
    notes: Mapped[str | None] = mapped_column(default=None)
    milestone: Mapped[str | None] = mapped_column(default=None)  # draft | candidate | final
    shots_json: Mapped[str] = mapped_column(default="[]")
    style_guide_json: Mapped[str] = mapped_column(default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_now)

    project: Mapped[Project] = relationship(back_populates="video_versions")

    @property
    def shots(self) -> list[dict[str, Any]]:
        return _json_load(self.shots_json, [])

    @property
    def style_guide(self) -> dict[str, Any]:
        return _json_load(self.style_guide_json, {})


class SubAgentTaskRow(Base):
    """后台任务的落库快照。

    剧组任务在线程里跑，内存态随进程消失；这里保存「启动」与「结束」两个时刻的
    快照，使已完成的任务历史在重启后仍可查询（进行中的任务本身已随线程终止，
    不假装它还活着）。
    """

    __tablename__ = "subagent_tasks"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="pending")  # pending|running|done|failed
    steps_json: Mapped[str] = mapped_column(default="[]")
    result: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


# ---------- 会话 / 初始化 ----------


class Store:
    """封装 SQLAlchemy 会话的访问对象。"""

    def __init__(self, database_url: str) -> None:
        # 判断驱动：SQLite 需要单线程检查关闭，且父目录要存在；
        # Postgres 需要连接超时（见 connect_args_for）。
        is_sqlite = database_url.startswith("sqlite")
        connect_args = connect_args_for(database_url)
        if is_sqlite:
            _ensure_sqlite_dir(database_url)
        # pool_pre_ping：连接前探活，避免数据库重启后拿到失效连接。
        self.engine = create_engine(
            database_url,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        if is_sqlite:
            # 多线程共享 engine 时（FastAPI 线程池），SQLite 需要 WAL + busy_timeout，
            # 否则并发写极易报 "database is locked"。
            from sqlalchemy import event

            @event.listens_for(self.engine, "connect")
            def _sqlite_pragma(dbapi_conn: Any, _record: Any) -> None:
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """轻量迁移：给已存在的表补缺失列（如 milestone / notes / video 字段）。

        新库由 create_all 直接建出含新列的表；老库（如已有 dev.db）不会自动加列，
        这里用 ALTER TABLE 补齐，避免启动后查询这些列报错。
        """
        try:
            from sqlalchemy import inspect, text

            insp = inspect(self.engine)
            tables = set(insp.get_table_names())

            # ---- projects 表 ----
            cols_proj = {c["name"] for c in insp.get_columns("projects")} if "projects" in tables else set()
            with self.engine.begin() as conn:
                if "notes" not in cols_proj:
                    conn.execute(text("ALTER TABLE projects ADD COLUMN notes VARCHAR"))
                if "current_video_version_id" not in cols_proj:
                    conn.execute(text("ALTER TABLE projects ADD COLUMN current_video_version_id VARCHAR"))
                if "video_style_json" not in cols_proj:
                    conn.execute(text("ALTER TABLE projects ADD COLUMN video_style_json VARCHAR DEFAULT '{}'"))
                if "novel_map_json" not in cols_proj:
                    conn.execute(text("ALTER TABLE projects ADD COLUMN novel_map_json VARCHAR DEFAULT '{}'"))
                if "adapted_until" not in cols_proj:
                    conn.execute(text("ALTER TABLE projects ADD COLUMN adapted_until INTEGER DEFAULT 0"))

            # ---- script_versions 表 ----
            cols_ver = {c["name"] for c in insp.get_columns("script_versions")} if "script_versions" in tables else set()
            with self.engine.begin() as conn:
                if "milestone" not in cols_ver:
                    conn.execute(text("ALTER TABLE script_versions ADD COLUMN milestone VARCHAR"))
                if "breakdown_json" not in cols_ver:
                    conn.execute(text("ALTER TABLE script_versions ADD COLUMN breakdown_json VARCHAR DEFAULT '{}'"))

            # ---- media_assets 表：content bytea（文件本体入库）----
            if "media_assets" in tables:
                cols_asset = {c["name"] for c in insp.get_columns("media_assets")}
                if "content" not in cols_asset:
                    with self.engine.begin() as conn:
                        conn.execute(text("ALTER TABLE media_assets ADD COLUMN content BYTEA"))

            # ---- conversations 表：滚动摘要列（老库补列）----
            if "conversations" in tables:
                cols_conv = {c["name"] for c in insp.get_columns("conversations")}
                if "summary_json" not in cols_conv:
                    with self.engine.begin() as conn:
                        conn.execute(text("ALTER TABLE conversations ADD COLUMN summary_json VARCHAR DEFAULT '{}'"))

            # ---- chat_messages 表：消息附带图片列（老库补列）----
            if "chat_messages" in tables:
                cols_msg = {c["name"] for c in insp.get_columns("chat_messages")}
                if "images_json" not in cols_msg:
                    with self.engine.begin() as conn:
                        conn.execute(text("ALTER TABLE chat_messages ADD COLUMN images_json VARCHAR DEFAULT '[]'"))

            # ---- conversations 表：project_id 需可空（全局对话）----
            # 「聊天与项目解耦」只在新库里生效；老 Postgres 库的列仍是 NOT NULL，
            # 导致建全局对话必撞 NotNullViolation。SQLite 不支持 DROP NOT NULL，
            # 但 SQLite 库都是模型改版后新建的（create_all 即 nullable），无需处理。
            if "conversations" in tables and not self.engine.url.get_backend_name().startswith("sqlite"):
                cols_conv = {c["name"]: c for c in insp.get_columns("conversations")}
                if cols_conv.get("project_id") and not cols_conv["project_id"].get("nullable"):
                    with self.engine.begin() as conn:
                        conn.execute(text("ALTER TABLE conversations ALTER COLUMN project_id DROP NOT NULL"))

        except Exception as e:  # noqa: BLE001
            # 表不存在或已是最新：迁移失败不阻塞启动，但必须可观测——
            # 静默失败会让老库缺列的报错延后到业务查询时才出现，难以排查。
            import logging

            logging.getLogger(__name__).warning("轻量迁移未完成（可能表结构已最新）：%s", e)

    def session(self):
        return self.session_factory()


    # ---- Project ----
    def create_project(self, *, title: str, adaptation_type: str, language: str, raw_text: str) -> Project:
        with self.session() as s:
            p = Project(
                id=gen_id("proj"),
                title=title,
                adaptation_type=adaptation_type,
                language=language,
                raw_text=raw_text,
            )
            s.add(p)
            s.commit()
            s.refresh(p)
            return p

    def get_project(self, project_id: str) -> Project | None:
        with self.session() as s:
            return s.get(Project, project_id)

    def delete_project(self, project_id: str) -> None:
        """删除项目及其所有关联数据（对话、消息、版本、运行记录、媒体素材、视频任务）。"""
        with self.session() as s:
            # 批量删除对话消息（先查出对话 id，再按 thread_id 批量删）
            conv_ids = [
                c.id for c in s.query(Conversation.id).filter_by(project_id=project_id).all()
            ]
            if conv_ids:
                s.query(ChatMessage).filter(ChatMessage.thread_id.in_(conv_ids)).delete()
                # 对话上传的图片本体一并清理。
                s.query(ChatImage).filter(ChatImage.thread_id.in_(conv_ids)).delete()
            # 删除对话
            s.query(Conversation).filter_by(project_id=project_id).delete()
            # 删除版本
            s.query(ScriptVersion).filter_by(project_id=project_id).delete()
            # 删除运行记录
            s.query(AgentRun).filter_by(project_id=project_id).delete()
            # 删除媒体素材（含入库的文件本体）
            s.query(MediaAsset).filter_by(project_id=project_id).delete()
            # 删除视频任务和版本
            s.query(VideoJob).filter_by(project_id=project_id).delete()
            s.query(VideoVersion).filter_by(project_id=project_id).delete()
            # 删除项目
            s.query(Project).filter_by(id=project_id).delete()
            s.commit()

    def list_projects(self) -> list[Project]:
        with self.session() as s:
            return s.query(Project).order_by(desc(Project.created_at)).all()

    def list_project_summaries(self) -> list[dict[str, Any]]:
        """批量查询项目摘要（版本数 / 运行数 / 最新运行），避免 N+1。"""
        from sqlalchemy import func

        with self.session() as s:
            projects = s.query(Project).order_by(desc(Project.created_at)).all()
            if not projects:
                return []
            pids = [p.id for p in projects]

            # 版本计数
            ver_counts: dict[str, int] = {}
            for pid, cnt in (
                s.query(ScriptVersion.project_id, func.count())
                .filter(ScriptVersion.project_id.in_(pids))
                .group_by(ScriptVersion.project_id)
                .all()
            ):
                ver_counts[pid] = cnt

            # 运行计数 + 最新运行
            run_counts: dict[str, int] = {}
            latest_runs: dict[str, AgentRun] = {}
            all_runs = (
                s.query(AgentRun)
                .filter(AgentRun.project_id.in_(pids))
                .order_by(AgentRun.created_at.desc())
                .all()
            )
            seen: set[str] = set()
            for r in all_runs:
                run_counts[r.project_id] = run_counts.get(r.project_id, 0) + 1
                if r.project_id not in seen:
                    latest_runs[r.project_id] = r
                    seen.add(r.project_id)

            # ---- 「最近使用」信号 ----
            # 首页的作品栏按最近使用排序。不用单独的 last_opened 字段：
            # 最近一次**对话 / 运行 / 出版本**三者里最新的那次，就是这部作品
            # 最近被用过的时间——既反映真实使用，又不用改表结构。
            last_used: dict[str, Any] = {}
            for r in all_runs:
                ts = r.created_at
                if r.project_id not in last_used or ts > last_used[r.project_id]:
                    last_used[r.project_id] = ts
            ver_rows = (
                s.query(ScriptVersion.project_id, func.max(ScriptVersion.created_at))
                .filter(ScriptVersion.project_id.in_(pids))
                .group_by(ScriptVersion.project_id)
                .all()
            )
            for pid, ts in ver_rows:
                if pid not in last_used or ts > last_used[pid]:
                    last_used[pid] = ts
            conv_owner = {
                c.id: c.project_id
                for c in s.query(Conversation).filter(Conversation.project_id.in_(pids)).all()
            }
            if conv_owner:
                msg_rows = (
                    s.query(ChatMessage.thread_id, func.max(ChatMessage.created_at))
                    .filter(ChatMessage.thread_id.in_(list(conv_owner)))
                    .group_by(ChatMessage.thread_id)
                    .all()
                )
                for thread_id, ts in msg_rows:
                    pid = conv_owner.get(thread_id)
                    if pid and (pid not in last_used or ts > last_used[pid]):
                        last_used[pid] = ts

            return [
                {
                    "id": p.id,
                    "title": p.title,
                    "adaptation_type": p.adaptation_type,
                    "language": p.language,
                    "status": p.status,
                    "current_version_id": p.current_version_id,
                    "created_at": p.created_at.isoformat(),
                    "version_count": ver_counts.get(p.id, 0),
                    "run_count": run_counts.get(p.id, 0),
                    # 最近使用时间（对话/运行/出版本里最新的一次）
                    "last_used_at": (
                        last_used[p.id].isoformat() if p.id in last_used else p.created_at.isoformat()
                    ),
                    "latest_run": {
                        "status": latest_runs[p.id].status,
                        "updated_at": latest_runs[p.id].updated_at.isoformat(),
                    }
                    if p.id in latest_runs
                    else None,
                }
                for p in projects
            ]

    def get_project_notes(self, project_id: str) -> str:
        p = self.get_project(project_id)
        return p.notes if p else ""

    def set_project_notes(self, project_id: str, notes: str) -> Project | None:
        """保存项目的编剧圣经 / 设定备忘（自由文本）。"""
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return None
            p.notes = notes
            p.updated_at = _now()
            s.commit()
            s.refresh(p)
            return p

    def set_novel_map(self, project_id: str, novel_map: dict[str, Any]) -> Project | None:
        """保存章节地图（长篇选段改编的依据）。"""
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return None
            p.novel_map_json = json.dumps(novel_map, ensure_ascii=False)
            p.updated_at = _now()
            s.commit()
            s.refresh(p)
            return p

    def set_adapted_until(self, project_id: str, chapter_index: int) -> Project | None:
        """推进连载进度锚（已改编到第几章）。"""
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return None
            p.adapted_until = int(chapter_index)
            p.updated_at = _now()
            s.commit()
            s.refresh(p)
            return p

    # ---- 视觉风格指南（StyleGuide：随项目持久化，含参考资产注册表）----

    def get_video_style(self, project_id: str) -> dict[str, Any]:
        """读取项目的视觉风格指南；未生成时返回空 dict。"""
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return {}
            return _json_load(p.video_style_json, {}) or {}

    def set_video_style(self, project_id: str, style: dict[str, Any]) -> dict[str, Any]:
        """整体覆盖写入视觉风格指南，返回写入后的内容。"""
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return {}
            p.video_style_json = json.dumps(style, ensure_ascii=False)
            p.updated_at = _now()
            s.commit()
            return dict(style)

    def merge_video_style(self, project_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """局部合并（例如仅回填 reference_images），保留已有字段。"""
        with self.session() as s:
            p = s.get(Project, project_id)
            if not p:
                return {}
            merged = {**(_json_load(p.video_style_json, {}) or {}), **patch}
            p.video_style_json = json.dumps(merged, ensure_ascii=False)
            p.updated_at = _now()
            s.commit()
            return merged

    # ---- ScriptVersion ----
    def create_version(
        self,
        project: Project,
        script: Script,
        *,
        source_type: str,
        label: str | None = None,
        notes: str | None = None,
        parent_version_id: str | None = None,
        set_current: bool = True,
        milestone: str | None = None,
    ) -> ScriptVersion:
        with self.session() as s:
            v = ScriptVersion(
                id=gen_id("ver"),
                project_id=project.id,
                parent_version_id=parent_version_id,
                source_type=source_type,
                label=label,
                notes=notes,
                milestone=milestone,
                content_json=script.model_dump_json(exclude_none=True),
            )
            s.add(v)
            if set_current:
                db_project = s.query(Project).filter_by(id=project.id).first()
                if db_project:
                    db_project.current_version_id = v.id
                    db_project.updated_at = _now()
            s.commit()
            s.refresh(v)
            return v

    def get_version(self, version_id: str) -> ScriptVersion | None:
        with self.session() as s:
            return s.get(ScriptVersion, version_id)

    def list_versions(self, project_id: str) -> list[ScriptVersion]:
        with self.session() as s:
            return (
                s.query(ScriptVersion)
                .filter_by(project_id=project_id)
                .order_by(desc(ScriptVersion.created_at))
                .all()
            )

    def latest_version(self, project: Project) -> ScriptVersion | None:
        """取项目当前版本；没有则取最近一条版本。"""
        with self.session() as s:
            if project.current_version_id:
                v = s.get(ScriptVersion, project.current_version_id)
                if v:
                    return v
            return (
                s.query(ScriptVersion)
                .filter_by(project_id=project.id)
                .order_by(desc(ScriptVersion.created_at))
                .first()
            )

    def set_version_milestone(self, version_id: str, milestone: str | None) -> ScriptVersion | None:
        """给某版本打里程碑标记（draft/candidate/final）。传 None 清除。"""
        with self.session() as s:
            v = s.get(ScriptVersion, version_id)
            if not v:
                return None
            v.milestone = milestone
            s.commit()
            s.refresh(v)
            return v

    def get_version_breakdown(self, version_id: str) -> list[dict[str, Any]]:
        """读取某版本的分镜方案（导演 Agent 的产出）；没有则返回空列表。"""
        with self.session() as s:
            v = s.get(ScriptVersion, version_id)
            if not v:
                return []
            data = _json_load(v.breakdown_json, [])
            return data if isinstance(data, list) else []

    def set_version_breakdown(self, version_id: str, breakdown: list[dict[str, Any]]) -> bool:
        """保存分镜方案到版本上（与剧本文本一起随版本走）。"""
        with self.session() as s:
            v = s.get(ScriptVersion, version_id)
            if not v:
                return False
            v.breakdown_json = json.dumps(breakdown, ensure_ascii=False)
            s.commit()
            return True

    # ---- AgentRun ----
    def create_agent_run(
        self,
        *,
        run_id: str | None = None,
        project_id: str,
        base_version_id: str,
        user_prompt: str,
        scene_ids: list[str],
        plan: list[str] | None = None,
        patch: list[dict[str, Any]] | None = None,
        steps: list[str] | None = None,
        status: str = "pending",
        decision: dict[str, Any] | None = None,
        model: str | None = None,
        error_message: str | None = None,
    ) -> AgentRun:
        with self.session() as s:
            r = AgentRun(
                id=run_id or gen_id("run"),
                project_id=project_id,
                base_version_id=base_version_id,
                user_prompt=user_prompt,
                scene_ids_json=json.dumps(scene_ids),
                plan_json=json.dumps(plan or []),
                patch_json=json.dumps(patch or []),
                steps_json=json.dumps(steps or []),
                status=status,
                decision_json=json.dumps(decision) if decision is not None else "null",
                model=model,
                error_message=error_message,
            )
            s.add(r)
            s.commit()
            s.refresh(r)
            return r

    def get_agent_run(self, run_id: str) -> AgentRun | None:
        with self.session() as s:
            return s.get(AgentRun, run_id)

    def update_agent_run(self, run_id: str, **fields: Any) -> AgentRun | None:
        with self.session() as s:
            r = s.get(AgentRun, run_id)
            if not r:
                return None
            for key, value in fields.items():
                if not hasattr(r, key):
                    continue
                if key in {"scene_ids", "plan", "patch", "steps"}:
                    setattr(r, f"{key}_json", json.dumps(value))
                elif key == "decision":
                    r.decision_json = json.dumps(value) if value is not None else "null"
                else:
                    setattr(r, key, value)
            s.commit()
            s.refresh(r)
            return r

    def list_agent_runs(self, project_id: str) -> list[AgentRun]:
        with self.session() as s:
            return (
                s.query(AgentRun)
                .filter_by(project_id=project_id)
                .order_by(desc(AgentRun.created_at))
                .all()
            )

    # ---- ChatMessage（对话式 Agent）----

    def save_chat_message(
        self,
        *,
        thread_id: str,
        role: str,
        content: str,
        payload: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> ChatMessage:
        with self.session() as s:
            m = ChatMessage(
                id=gen_id("msg"),
                thread_id=thread_id,
                role=role,
                content=content,
                payload_json=json.dumps(payload or [], ensure_ascii=False),
                events_json=json.dumps(events or [], ensure_ascii=False),
                images_json=json.dumps(images or [], ensure_ascii=False),
            )
            s.add(m)
            s.commit()
            s.refresh(m)
            return m

    def list_chat_messages(self, thread_id: str, limit: int = 200) -> list[ChatMessage]:
        with self.session() as s:
            # 取「最新 limit 条」再按时间正序返回：长对话要保留的是最近上下文，
            # 而不是开头几条。
            rows = (
                s.query(ChatMessage)
                .filter_by(thread_id=thread_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(limit)
                .all()
            )
            return list(reversed(rows))

    # ---- ChatImage（对话上传的图片，字节入库）----

    def save_chat_image(
        self, *, thread_id: str, mime: str, name: str, content: bytes,
    ) -> ChatImage:
        with self.session() as s:
            img = ChatImage(
                id=gen_id("cimg"), thread_id=thread_id or "",
                mime=mime or "image/png", name=name or "",
                content=content,
            )
            s.add(img)
            s.commit()
            s.refresh(img)
            return img

    def get_chat_image(self, image_id: str) -> ChatImage | None:
        with self.session() as s:
            return s.get(ChatImage, image_id)

    def list_chat_images(self, thread_id: str) -> list[ChatImage]:
        with self.session() as s:
            return (
                s.query(ChatImage)
                .filter_by(thread_id=thread_id)
                .order_by(ChatImage.created_at.asc())
                .all()
            )

    # ---- Conversation（项目下的对话线程）----

    # ---- Media assets ----
    def add_asset(self, project_id: str, *, kind: str, name: str, source: str,
                  url: str, file_path: str | None = None, id: str | None = None,
                  content: bytes | None = None) -> MediaAsset:
        with self.session() as s:
            a = MediaAsset(id=id or gen_id("asset"), project_id=project_id, kind=kind,
                           name=name, source=source, url=url, file_path=file_path,
                           content=content)
            s.add(a)
            s.commit()
            s.refresh(a)
            return a

    def list_assets(self, project_id: str, kind: str | None = None) -> list[MediaAsset]:
        with self.session() as s:
            q = s.query(MediaAsset).filter_by(project_id=project_id)
            if kind:
                q = q.filter_by(kind=kind)
            return q.order_by(MediaAsset.created_at.desc()).all()

    def get_asset(self, asset_id: str) -> MediaAsset | None:
        with self.session() as s:
            return s.get(MediaAsset, asset_id)

    def delete_asset(self, asset_id: str) -> MediaAsset | None:
        with self.session() as s:
            a = s.get(MediaAsset, asset_id)
            if not a:
                return None
            s.delete(a)
            s.commit()
            return a

    def create_conversation(self, project_id: str | None = None, title: str = "新对话") -> Conversation:
        with self.session() as s:
            c = Conversation(id=gen_id("conv"), project_id=project_id, title=title)
            s.add(c)
            s.commit()
            s.refresh(c)
            return c

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self.session() as s:
            return s.get(Conversation, conversation_id)

    def list_conversations(self, project_id: str) -> list[Conversation]:
        with self.session() as s:
            return (
                s.query(Conversation)
                .filter_by(project_id=project_id)
                .order_by(Conversation.updated_at.desc())
                .all()
            )

    def list_global_conversations(self) -> list[Conversation]:
        """列出全局对话（不属于任何项目）。"""
        with self.session() as s:
            return (
                s.query(Conversation)
                .filter(Conversation.project_id.is_(None))
                .order_by(Conversation.updated_at.desc())
                .all()
            )

    def count_user_messages(self, conversation_id: str) -> int:
        """统计对话中用户消息的数量（用于判断对话是否为空白）。"""
        with self.session() as s:
            return (
                s.query(ChatMessage)
                .filter_by(thread_id=conversation_id, role="user")
                .count()
            )

    def rename_conversation(self, conversation_id: str, title: str) -> Conversation | None:
        with self.session() as s:
            c = s.get(Conversation, conversation_id)
            if not c:
                return None
            c.title = title
            c.updated_at = _now()
            s.commit()
            s.refresh(c)
            return c

    def delete_conversation(self, conversation_id: str) -> bool:
        with self.session() as s:
            c = s.get(Conversation, conversation_id)
            if not c:
                return False
            s.query(ChatMessage).filter_by(thread_id=conversation_id).delete()
            # 对话上传的图片本体随对话一起清理（bytes 会在 SQLite 里越攒越大）。
            s.query(ChatImage).filter_by(thread_id=conversation_id).delete()
            s.delete(c)
            s.commit()
            return True

    def set_conversation_summary(self, conversation_id: str, summary: dict[str, Any]) -> None:
        """保存滚动摘要（压缩历史时更新）。对话不存在时静默跳过。"""
        with self.session() as s:
            c = s.get(Conversation, conversation_id)
            if not c:
                return
            c.summary_json = json.dumps(summary, ensure_ascii=False)
            s.commit()

    # ---- Memory（三层用户记忆）----

    def add_memory(
        self,
        *,
        kind: str,
        scope: str,
        content: str,
        project_id: str | None = None,
        conversation_id: str | None = None,
        source: str = "user",
    ) -> Memory:
        with self.session() as s:
            m = Memory(
                id=gen_id("mem"),
                kind=kind,
                scope=scope,
                project_id=project_id,
                conversation_id=conversation_id,
                content=content,
                source=source,
            )
            s.add(m)
            s.commit()
            s.refresh(m)
            return m

    def list_memories(
        self,
        *,
        project_id: str | None = None,
        conversation_id: str | None = None,
        kinds: list[str] | None = None,
    ) -> list[Memory]:
        """列出对当前上下文可见的记忆：全局全给；项目/对话记忆按归属过滤。

        顺序（注入提示词时的优先级）：global → project → conversation，
        同层内新的在前。
        """
        with self.session() as s:
            q = s.query(Memory).filter(Memory.active.is_(True))
            if kinds:
                q = q.filter(Memory.kind.in_(kinds))
            rows = q.order_by(Memory.created_at.desc()).all()
            scope_order = {"global": 0, "project": 1, "conversation": 2}
            visible = [
                m for m in rows
                if m.scope == "global"
                or (m.scope == "project" and m.project_id and m.project_id == project_id)
                or (m.scope == "conversation" and m.conversation_id and m.conversation_id == conversation_id)
            ]
            return sorted(visible, key=lambda m: (scope_order[m.scope], m.created_at), reverse=False)

    def delete_memory(self, memory_id: str) -> bool:
        with self.session() as s:
            m = s.get(Memory, memory_id)
            if not m:
                return False
            s.delete(m)
            s.commit()
            return True

    # =====================================================================
    # v3.0 视频制作相关 CRUD
    # =====================================================================

    # ---- ApiProvider ----

    def create_api_provider(
        self,
        *,
        kind: str,
        name: str,
        label: str,
        base_url: str = "",
        api_key: str = "",
        config: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> ApiProvider:
        with self.session() as s:
            p = ApiProvider(
                id=gen_id("prov"),
                kind=kind,
                name=name,
                label=label,
                base_url=base_url,
                api_key=api_key,
                config_json=json.dumps(config or {}, ensure_ascii=False),
                enabled=enabled,
            )
            s.add(p)
            s.commit()
            s.refresh(p)
            return p

    def get_api_provider(self, provider_id: str) -> ApiProvider | None:
        with self.session() as s:
            return s.get(ApiProvider, provider_id)

    def list_api_providers(self, kind: str | None = None) -> list[ApiProvider]:
        with self.session() as s:
            q = s.query(ApiProvider)
            if kind:
                q = q.filter_by(kind=kind)
            return q.order_by(ApiProvider.created_at.desc()).all()

    def update_api_provider(self, provider_id: str, **fields: Any) -> ApiProvider | None:
        with self.session() as s:
            p = s.get(ApiProvider, provider_id)
            if not p:
                return None
            for key, value in fields.items():
                if key == "config":
                    p.config_json = json.dumps(value, ensure_ascii=False)
                elif hasattr(p, key):
                    setattr(p, key, value)
            p.updated_at = _now()
            s.commit()
            s.refresh(p)
            return p

    def delete_api_provider(self, provider_id: str) -> bool:
        with self.session() as s:
            p = s.get(ApiProvider, provider_id)
            if not p:
                return False
            # 先解除模型偏好对该 provider 的引用（FK 约束 + 语义上偏好随之失效）
            s.query(ModelPreference).filter_by(provider_id=provider_id).delete()
            s.delete(p)
            s.commit()
            return True

    # ---- ModelPreference ----

    # ---- UserSkill ----

    def save_user_skill(
        self,
        *,
        name: str,
        description: str,
        when_to_use: str,
        audience: str,
        content: str,
    ) -> UserSkill:
        """保存（或覆盖）一个用户导入的技能。"""
        with self.session() as s:
            row = s.query(UserSkill).filter_by(name=name).first()
            if row is None:
                row = UserSkill(id=gen_id("uskill"), name=name)
                s.add(row)
            row.description = description
            row.when_to_use = when_to_use
            row.audience = audience
            row.content = content
            s.commit()
            s.refresh(row)
            return row

    def list_user_skills(self) -> list[dict[str, Any]]:
        """列出用户导入的技能（按名字排序）。"""
        with self.session() as s:
            rows = s.query(UserSkill).order_by(UserSkill.name).all()
            return [
                {
                    "name": r.name,
                    "description": r.description,
                    "when_to_use": r.when_to_use,
                    "audience": r.audience,
                    "content": r.content,
                }
                for r in rows
            ]

    def delete_user_skill(self, name: str) -> bool:
        """删除一个用户导入的技能。"""
        with self.session() as s:
            row = s.query(UserSkill).filter_by(name=name).first()
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    def get_model_preference(self, task_type: str) -> ModelPreference | None:
        with self.session() as s:
            return s.query(ModelPreference).filter_by(task_type=task_type, is_default=True).first()

    def list_model_preferences(self) -> list[ModelPreference]:
        with self.session() as s:
            return s.query(ModelPreference).order_by(ModelPreference.task_type).all()

    def clear_model_preference(self, task_type: str) -> int:
        """清除某类任务的模型偏好，返回删掉的行数。

        偏好一旦写入就一直压过 `.env`（UI 选择优先）。没有清除入口的话，
        用户改了 `.env` 的模型名却怎么都不生效、也看不出原因——所以必须留这个出口。
        清空后该任务回落到 `.env` 的厂商优先级链。
        """
        with self.session() as s:
            deleted = (
                s.query(ModelPreference)
                .filter_by(task_type=task_type)
                .delete(synchronize_session=False)
            )
            s.commit()
            return int(deleted)

    def set_model_preference(
        self,
        *,
        task_type: str,
        provider_id: str | None,
        model_name: str,
        params: dict[str, Any] | None = None,
        is_default: bool = True,
    ) -> ModelPreference:
        with self.session() as s:
            # 先清除同类型的旧默认
            if is_default:
                s.query(ModelPreference).filter_by(task_type=task_type, is_default=True).update({"is_default": False})
            pref = ModelPreference(
                id=gen_id("pref"),
                task_type=task_type,
                provider_id=provider_id,
                model_name=model_name,
                params_json=json.dumps(params or {}, ensure_ascii=False),
                is_default=is_default,
            )
            s.add(pref)
            s.commit()
            s.refresh(pref)
            return pref

    # ---- VideoJob ----

    def create_video_job(
        self,
        *,
        project_id: str,
        shot_id: str,
        provider: str,
        model: str,
        prompt: str,
        params: dict[str, Any] | None = None,
        version_id: str | None = None,
        cost_estimate: float | None = None,
    ) -> VideoJob:
        with self.session() as s:
            j = VideoJob(
                id=gen_id("vjob"),
                project_id=project_id,
                version_id=version_id,
                shot_id=shot_id,
                provider=provider,
                model=model,
                prompt=prompt,
                params_json=json.dumps(params or {}, ensure_ascii=False),
                cost_estimate=cost_estimate,
            )
            s.add(j)
            s.commit()
            s.refresh(j)
            return j

    def get_video_job(self, job_id: str) -> VideoJob | None:
        with self.session() as s:
            return s.get(VideoJob, job_id)

    def list_video_jobs(self, project_id: str) -> list[VideoJob]:
        with self.session() as s:
            return (
                s.query(VideoJob)
                .filter_by(project_id=project_id)
                .order_by(VideoJob.created_at.desc())
                .all()
            )

    def list_unfinished_video_jobs(self, limit: int = 200) -> list[VideoJob]:
        """还没到终态的任务（供启动时接管孤儿任务，见 app/video/sweeper.py）。

        这些状态在数据库里都表示「还在跑」，但进程一旦重启，内存里的轮询线程
        就没了——数据库会永远停在 `generating`，用户看到的是「卡住了」。
        """
        with self.session() as s:
            return (
                s.query(VideoJob)
                .filter(VideoJob.status.in_(("pending", "submitting", "queued", "generating")))
                .order_by(VideoJob.created_at.asc())
                .limit(limit)
                .all()
            )

    def update_video_job(self, job_id: str, **fields: Any) -> VideoJob | None:
        with self.session() as s:
            j = s.get(VideoJob, job_id)
            if not j:
                return None
            for key, value in fields.items():
                if key == "params":
                    j.params_json = json.dumps(value, ensure_ascii=False)
                elif hasattr(j, key):
                    setattr(j, key, value)
            s.commit()
            s.refresh(j)
            return j

    # ---- VideoVersion ----

    def create_video_version(
        self,
        *,
        project_id: str,
        shots: list[dict[str, Any]],
        style_guide: dict[str, Any] | None = None,
        source_type: str = "agent",
        label: str | None = None,
        notes: str | None = None,
        parent_version_id: str | None = None,
        set_current: bool = True,
    ) -> VideoVersion:
        with self.session() as s:
            v = VideoVersion(
                id=gen_id("vver"),
                project_id=project_id,
                parent_version_id=parent_version_id,
                source_type=source_type,
                label=label,
                notes=notes,
                shots_json=json.dumps(shots, ensure_ascii=False),
                style_guide_json=json.dumps(style_guide or {}, ensure_ascii=False),
            )
            s.add(v)
            if set_current:
                db_proj = s.query(Project).filter_by(id=project_id).first()
                if db_proj:
                    db_proj.current_video_version_id = v.id
                    db_proj.updated_at = _now()
            s.commit()
            s.refresh(v)
            return v

    def get_video_version(self, version_id: str) -> VideoVersion | None:
        with self.session() as s:
            return s.get(VideoVersion, version_id)

    def list_video_versions(self, project_id: str) -> list[VideoVersion]:
        with self.session() as s:
            return (
                s.query(VideoVersion)
                .filter_by(project_id=project_id)
                .order_by(VideoVersion.created_at.desc())
                .all()
            )

    def set_video_version_milestone(self, version_id: str, milestone: str | None) -> VideoVersion | None:
        with self.session() as s:
            v = s.get(VideoVersion, version_id)
            if not v:
                return None
            v.milestone = milestone
            s.commit()
            s.refresh(v)
            return v

    def ensure_default_conversation(self, project_id: str) -> Conversation:
        """项目没有对话时自动建一个「默认对话」，保证任何项目都能直接开聊。"""
        with self.session() as s:
            existing = (
                s.query(Conversation)
                .filter_by(project_id=project_id)
                .order_by(Conversation.created_at.asc())
                .first()
            )
            if existing:
                return existing
        return self.create_conversation(project_id, title="默认对话")

    # ---- SubAgentTask ----

    def save_subagent_task(self, task: dict[str, Any]) -> None:
        """按 task_id upsert 一条后台任务快照（由 SubAgentRunner 在起止时刻调用）。"""
        with self.session() as s:
            row = s.get(SubAgentTaskRow, task["id"])
            if row is None:
                row = SubAgentTaskRow(id=task["id"])
                s.add(row)
            row.name = task.get("name", "")
            row.status = task.get("status", "pending")
            row.steps_json = json.dumps(task.get("steps") or [], ensure_ascii=False)
            row.result = task.get("result")
            created = _as_datetime(task.get("created_at"))
            if created is not None:
                row.created_at = created
            row.finished_at = _as_datetime(task.get("finished_at"))
            s.commit()

    def get_subagent_task(self, task_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(SubAgentTaskRow, task_id)
            return _subagent_task_dict(row) if row else None

    def list_subagent_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        """按创建时间倒序返回历史任务（含进行中的快照）。"""
        with self.session() as s:
            rows = (
                s.query(SubAgentTaskRow)
                .order_by(desc(SubAgentTaskRow.created_at))
                .limit(limit)
                .all()
            )
            return [_subagent_task_dict(r) for r in rows]


def _subagent_task_dict(row: SubAgentTaskRow) -> dict[str, Any]:
    """把落库的任务行还原成 API / 前端使用的形状。"""
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "steps": _json_load(row.steps_json, []),
        "result": row.result,
        "created_at": row.created_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
