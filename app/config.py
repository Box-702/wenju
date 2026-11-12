# =====================================================================
# config.py —— 全局配置
#
# 用 pydantic-settings 从环境变量 / .env 读取运行参数，集中在一处。
#
# 支持的模型/服务接入（全部可选，缺 key 自动走本地回退）：
#   - 对话模型：OPENAI_*（OpenAI 兼容）或 DEEPSEEK_*（DeepSeek 原生）；
#   - 监控：LANGSMITH_*（启动时映射为 LangChain 的 LANGCHAIN_* 环境变量）；
#   - 视频生成：MINIMAX_* / RUNWAY_* 等（通过 Provider 管理）。
# =====================================================================

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（仓库根），与 .env、docker-compose.yml 同级。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _default_db_path() -> Path:
    """默认数据库路径：产品主目录 ~/.wenju/app.db（见 app/home.py）。

    家目录不可写（异常环境/容器只读挂载）时回退项目 data/，不阻断启动。"""
    try:
        from .home import db_path, ensure_home

        ensure_home(_PROJECT_ROOT)
        return db_path()
    except Exception:  # noqa: BLE001
        return _PROJECT_ROOT / "data" / "script_agent.db"


class Settings(BaseSettings):
    """运行配置。所有字段均可被环境变量覆盖（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env") if (_PROJECT_ROOT / ".env").exists() else ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 业务数据库（默认 SQLite：本地工具零部署；可切 Postgres） ----------
    # 默认落产品主目录 ~/.wenju/app.db（仿 Codex：用户数据跟着用户走，不跟
    # 代码目录走；懒迁移见 app/home.py）。要切 Postgres 在 .env 里设 DATABASE_URL。
    database_url: str = Field(
        default_factory=lambda: f"sqlite:///{_default_db_path().as_posix()}",
        alias="DATABASE_URL",
    )

    # ---------- LangGraph checkpointer ----------
    # 桌面模式默认用内存（避免依赖 Postgres），开发模式也默认内存。
    checkpointer: str = Field(default="memory", alias="CHECKPOINTER")  # memory | postgres
    checkpoint_dsn: str = Field(default="", alias="CHECKPOINT_DSN")

    # ---------- 对话模型（OpenAI 兼容 / DeepSeek / 智谱） ----------
    # 默认优先级：OPENAI_* > ZHIPUAI_* > DEEPSEEK_*（见 app/llm/registry.py）。
    # 想换顺序或指定别的厂商，设 CHAT_PROVIDER=ollama/moonshot/qwen 等即可。
    chat_provider: str = Field(default="", alias="CHAT_PROVIDER")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    zhipuai_api_key: str = Field(default="", alias="ZHIPUAI_API_KEY")
    zhipuai_base_url: str = Field(default="https://open.bigmodel.cn/api/paas/v4/", alias="ZHIPUAI_BASE_URL")
    zhipuai_model: str = Field(default="GLM-5.3-Flash", alias="ZHIPUAI_MODEL_NAME")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL_NAME")
    deepseek_thinking: bool = Field(default=False, alias="DEEPSEEK_THINKING")
    # MiniMax：一把 key 打通对话 / 视频（H3）/ 配音（T2A）。
    # 注意：其 /chat/completions 不支持 response_format=json_object，
    # 结构化输出走 client.LLM.structured() 的降级路径。
    minimax_api_key: str = Field(default="", alias="MINIMAX_API_KEY")
    # 备用 key（逗号/分号分隔）：主 key 欠费或失效时自动顶上，
    # 轮换逻辑见 app/keys.py；顺序即优先级，冷却时长由 KEY_COOLDOWN_SEC 控制。
    minimax_api_keys: str = Field(default="", alias="MINIMAX_API_KEY_FALLBACKS")
    minimax_base_url: str = Field(default="https://api.minimax.cn/v1", alias="MINIMAX_BASE_URL")
    minimax_model: str = Field(default="MiniMax-Text-01", alias="MINIMAX_MODEL_NAME")
    # 本地模型（无 key，仅当 CHAT_PROVIDER=ollama 时启用）。
    ollama_base_url: str = Field(default="http://127.0.0.1:11434/v1", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="qwen2.5:7b", alias="OLLAMA_MODEL")

    # ---------- 视觉（定妆图质检）与文生图（参考资产） ----------
    # 留空则自动复用支持该能力的厂商 key（见 app/llm/registry.py 的 resolve_vision/resolve_image）。
    vision_provider: str = Field(default="", alias="VISION_PROVIDER")
    vision_api_key: str = Field(default="", alias="VISION_API_KEY")
    vision_base_url: str = Field(default="", alias="VISION_BASE_URL")
    vision_model: str = Field(default="", alias="VISION_MODEL")
    image_provider: str = Field(default="", alias="IMAGE_PROVIDER")
    image_api_key: str = Field(default="", alias="IMAGE_API_KEY")
    image_base_url: str = Field(default="", alias="IMAGE_BASE_URL")
    image_model: str = Field(default="", alias="IMAGE_MODEL")

    # ---------- 可选第三方服务 ----------
    # LangSmith 监控：开启后 LangGraph / LLM 运行轨迹自动上报。
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_endpoint: str = Field(default="https://api.smith.langchain.com", alias="LANGSMITH_ENDPOINT")
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="", alias="LANGSMITH_PROJECT")

    # ---------- 向量检索已移除，改用 Agent 工具模式 ----------

    # ---------- 审阅评分 / 一致性保障（评审打分 + 一致性校验） ----------
    # 有可用对话模型时，guard 节点会先跑一次 LLM 审阅：对改编提议做
    # 多维度打分（忠实度 / 一致性 / 冲突 / 风格 / 结构），并列出一致性问题。
    # 总分低于阈值或存在 error 级问题时会自动回炉重做（上限见 nodes.MAX_PROPOSE_ITERATIONS）。
    enable_review_scoring: bool = Field(default=True, alias="ENABLE_REVIEW_SCORING")
    review_score_threshold: int = Field(default=75, alias="REVIEW_SCORE_THRESHOLD")

    # ---------- 成片质检（视觉模型看抽帧 → 致命问题自动重 roll） ----------
    # 单次视频生成是抽签：成片后按致命问题（穿模/世界崩坏/严重畸变）质检，
    # 不合格自动用同参数重 roll，最多 max_reroll 次；判定只对致命问题 FAIL。
    video_qa_enabled: bool = Field(default=True, alias="VIDEO_QA_ENABLED")
    video_qa_max_reroll: int = Field(default=1, alias="VIDEO_QA_MAX_REROLL")
    video_qa_frames: int = Field(default=5, alias="VIDEO_QA_FRAMES")
    # 孤儿任务巡检：轮询线程活在内存里，进程重启后 DB 会停在 generating 且无人更新。
    # 巡检把「DB 说还在跑、内存没人盯」的任务重新挂回轮询（绝不再提交一次）。
    video_sweeper_enabled: bool = Field(default=True, alias="VIDEO_SWEEPER_ENABLED")
    video_sweeper_interval_sec: float = Field(default=60.0, alias="VIDEO_SWEEPER_INTERVAL_SEC")
    # ffmpeg 可执行文件路径（抽帧/拼接用）；留空则用 PATH 里的 ffmpeg。
    ffmpeg_path: str = Field(default="", alias="FFMPEG_PATH")

    # ---------- 视频生成全局策略 ----------
    # 默认生成分辨率：首次生成用低挡位（768P）快速廉价出片，满意后再指定
    # 高清晰度重生成。用户在请求参数里显式指定 resolution 时以用户为准；
    # 该值只是默认值，不写死——换 provider 时请按其方言设置（如 1280x720）。
    video_default_resolution: str = Field(default="768P", alias="VIDEO_DEFAULT_RESOLUTION")

    # ---------- 配音（TTS） ----------
    # 视频模型没有「文字→语音」通路，台词语音由 TTS 合成后按时间轴混入成片。
    # key 留空时回落 MINIMAX_API_KEY（TTS 与视频同厂商，零配置可用）。
    tts_provider: str = Field(default="minimax", alias="TTS_PROVIDER")
    tts_api_key: str = Field(default="", alias="TTS_API_KEY")
    tts_base_url: str = Field(default="", alias="TTS_BASE_URL")
    tts_model: str = Field(default="speech-02-hd", alias="TTS_MODEL")
    # 台词混入时的环境音音量（0-1），防止人声被环境音盖住。
    dubbing_bg_volume: float = Field(default=0.25, alias="DUBBING_BG_VOLUME")

    # ---------- 服务 ----------
    # 默认只监听本机：全部 API 无鉴权，且可设置任意工作目录，绝不能默认暴露到局域网。
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    output_language: str = Field(default="zh-CN", alias="OUTPUT_LANGUAGE")

    # ---------- 工作目录（默认产品主目录 ~/.wenju/workspace/） ----------
    # 剧本以「文件」形式存到 <家目录>/workspace/<剧名>/01_原稿 等子目录；
    # 数据库承担聊天与 Agent 工作流。WORKSPACE_ROOT 可指到任意盘。
    workspace_root: str = Field(default="", alias="WORKSPACE_ROOT")
    workspace_persist: bool = Field(default=True, alias="WORKSPACE_PERSIST")

    @property
    def effective_workspace_root(self) -> str:
        v = self.workspace_root.strip()
        if v:
            return v
        try:
            from .home import workspace_root

            return str(workspace_root())
        except Exception:  # noqa: BLE001
            return str(_PROJECT_ROOT / "data")

    # 兼容性：.env 中值被引号包裹时自动去引号（常见手写失误）。
    @field_validator("*", mode="before")
    @classmethod
    def _strip_quotes(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
                v = v[1:-1].strip()
        return v

    @property
    def model_available(self) -> bool:
        """是否配置了有效的对话模型 key。"""
        return bool(self.openai_api_key.strip() or self.zhipuai_api_key.strip() or self.deepseek_api_key.strip())

    @property
    def minimax_key_list(self) -> list[str]:
        """MiniMax 的 key 列表（主 key + 备用 key，去重保序）。"""
        from .keys import merge_keys

        return merge_keys(self.minimax_api_key, self.minimax_api_keys)

    @property
    def effective_checkpoint_dsn(self) -> str:
        """checkpointer 用的 DSN：优先专用值，否则复用业务库。"""
        return self.checkpoint_dsn.strip() or self.database_url


def apply_langsmith_env(settings: Settings) -> None:
    """把 .env 里的 LANGSMITH_* 映射成 LangChain 识别的 LANGCHAIN_* 环境变量。

    LangSmith 监控由 LangChain 客户端读取环境变量；在应用启动时注入，
    之后所有 LangGraph / LLM 调用都会自动上报运行轨迹。
    """
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_TRACING"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project or "script-agent"


@lru_cache
def get_settings() -> Settings:
    """缓存的配置单例。"""
    return Settings()


def bootstrap_env() -> Settings:
    """进程启动引导：把 .env 落进 os.environ，再注入 LangSmith 监控变量。

    **必须在每个真正的入口调用一次**（`main.py` / `cli.py`），否则会出现
    「配置存在但代码看不见」：有一批参数不走 `Settings`，而是直接读
    `os.environ`——

      - `app/keys.py`：`MINIMAX_API_KEY` / `MINIMAX_API_KEY_FALLBACKS` /
        `KEY_COOLDOWN_SEC`（多把 key 轮换与冷却）；
      - `app/video/qa.py`：`FFMPEG_PATH` 兜底；
      - LangChain：`LANGCHAIN_*`（由 `apply_langsmith_env` 写入）。

    `Settings` 自己会读 `.env`（`SettingsConfigDict(env_file=...)`），所以只跑
    Settings 的场合看起来一切正常；但上面这些直读 `os.environ` 的地方会拿到空值。
    实测症状：CLI 下 `minimax_keys()` 返回 0 把，备用 key 全部失效。

    `load_dotenv` 默认不覆盖已存在的环境变量——测试里用 monkeypatch 设置的值
    优先级更高，不会被 .env 盖掉。
    """
    from dotenv import load_dotenv

    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        # 允许从当前目录取 .env（例如在子目录里跑脚本）。
        load_dotenv()

    settings = get_settings()
    apply_langsmith_env(settings)
    return settings
