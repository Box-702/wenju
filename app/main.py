# =====================================================================
# main.py —— FastAPI 应用入口
#
# 职责：
#   - 挂载 REST 路由（/api/*）；
#   - 在根路径托管 Vue 前端构建产物（frontend/dist），用于演示「导入->生成->Agent->审阅」；
#   - 允许跨域（供前端调试）。
# =====================================================================

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from .api import router as api_router
from .config import bootstrap_env, get_settings

# 进程启动引导：.env 落进 os.environ + 注入 LANGSMITH -> LANGCHAIN 监控变量。
# 必须在导入后立刻执行——有一批参数（多 key 轮换、ffmpeg 路径）直接读
# os.environ、不经过 Settings（原因见 config.bootstrap_env 的注释）。
bootstrap_env()

# 站点目录：前端由 frontend/ 的 Vue 工程构建（frontend/dist），
# FastAPI 直接托管构建产物（API 与 Web 页面同源）。
_ROOT_DIR = Path(__file__).resolve().parent.parent
_DIST_DIR = _ROOT_DIR / "frontend" / "dist"


def _wire_video_job_writeback() -> None:
    """把视频队列的状态变化写回数据库。

    队列（VideoJobManager）只维护内存状态，而前端轮询的是 DB 里的
    video_jobs 表；启动时挂上这个回调，任务进度才能被 API 看到。
    """
    from datetime import UTC, datetime

    from .deps import store as get_store
    from .deps import video_manager

    status_map = {
        "pending": "pending",
        "submitting": "pending",
        "queued": "queued",
        "generating": "generating",
        "succeeded": "succeeded",
        "failed": "failed",
        "timeout": "failed",
        "cancelled": "cancelled",
    }

    def _writeback(job_id: str, info: dict[str, Any]) -> None:
        fields: dict[str, Any] = {"status": status_map.get(str(info.get("status")), "pending")}
        if info.get("external_task_id"):
            fields["external_task_id"] = info["external_task_id"]
        if info.get("video_url"):
            fields["video_url"] = info["video_url"]
        if info.get("error"):
            fields["error_message"] = str(info["error"])
        if info.get("finished_at"):
            fields["finished_at"] = datetime.now(UTC)
        try:
            get_store().update_video_job(job_id, **fields)
        except Exception:  # noqa: BLE001
            pass

    video_manager().on_complete(_writeback)


def _wire_video_qa() -> None:
    """成片自动质检 + 有界重 roll：单次生成是抽签，闭环把它变良率。

    任务成功后抽帧给视觉模型判定（致命问题才 FAIL），不合格自动用同参数
    重新提交，最多 VIDEO_QA_MAX_REROLL 次；重 roll 出来的任务走同一条
    队列与回调，attempt 计数保证有界。任何失败只打日志，不影响任务状态。
    """
    import logging

    from .api.video import _job_params, _resolve_video_provider
    from .deps import store as get_store
    from .deps import video_manager
    from .video.qa import reroll_if_needed

    log = logging.getLogger(__name__)

    def _resubmit(job: Any) -> None:
        video_manager().submit(
            job.id,
            _resolve_video_provider(job.provider),
            job.prompt,
            _job_params(dict(job.params or {})),
        )

    def _on_succeeded(job_id: str, info: dict[str, Any]) -> None:
        settings = get_settings()
        if not settings.video_qa_enabled or str(info.get("status")) != "succeeded":
            return
        try:
            reroll_if_needed(
                job_id,
                info,
                store=get_store(),
                submit=_resubmit,
                max_reroll=max(0, settings.video_qa_max_reroll),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("成片质检重 roll 失败（不影响任务状态）：%s", e)

    video_manager().on_complete(_on_succeeded)


def _wire_video_batch() -> None:
    """批量生成批次推进：任务终态（成功/失败）驱动批次前进或进入审批暂停。"""
    import logging

    from .deps import batch_manager, store as get_store, video_manager

    log = logging.getLogger(__name__)

    def _on_finished(job_id: str, info: dict[str, Any]) -> None:
        status = str(info.get("status"))
        if status not in ("succeeded", "failed", "timeout", "cancelled"):
            return
        try:
            j = get_store().get_video_job(job_id)
            if j is None or not j.project_id:
                return
            batch = batch_manager().on_job_finished(j.project_id, j.shot_id, j)
            if batch is not None and batch.status == "awaiting_approval":
                log.info("批次 %s 已暂停等待审批（镜 %d/%d）",
                         batch.id, batch.current_index + 1, len(batch.shot_ids))
        except Exception as e:  # noqa: BLE001
            log.warning("批次推进失败（不影响任务状态）：%s", e)

    video_manager().on_complete(_on_finished)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：启动时接管孤儿任务，关闭时停掉巡检线程。

    为什么放在 lifespan 而不是 `create_app()` 里：`create_app()` 在测试里被反复
    调用（每个用例一个新 app），放那里会攒出一堆线程；而 lifespan 只在真的跑
    ASGI 服务时触发（`TestClient` 不带 `with` 时并不触发）。
    """
    import logging

    settings = get_settings()
    log = logging.getLogger(__name__)
    # BYOK 入库（D-41）：把 .env 现有 key 幂等搬进 DB，之后 DB 是唯一权威。
    # 失败不阻断启动——env 兜底链还在，最坏情况是行为同从前。
    try:
        from .deps import store as _seed_store
        from .llm.seed import seed_keys_from_env
        seed_keys_from_env(settings, _seed_store())
    except Exception as e:  # noqa: BLE001
        log.warning("BYOK 配置入库失败（不阻断服务）：%s", e)
    if settings.video_sweeper_enabled:
        from .video.sweeper import start_sweeper

        try:
            start_sweeper(interval_sec=settings.video_sweeper_interval_sec)
        except Exception as e:  # noqa: BLE001
            log.warning("孤儿任务巡检启动失败（不影响服务）：%s", e)
    yield
    from .video.queue import shutdown_manager
    from .video.sweeper import stop_sweeper

    stop_sweeper()
    try:
        shutdown_manager()
    except Exception:  # noqa: BLE001
        pass


def create_app() -> FastAPI:
    settings = get_settings()
    # LangSmith 监控变量已由模块级 bootstrap_env() 注入；这里只取 settings。
    _wire_video_job_writeback()
    _wire_video_qa()
    _wire_video_batch()
    app = FastAPI(
        title="问剧（Wenju）",
        version="0.3.0",
        description="小说 → 剧本 → AI 短剧 全链路 Agent 工作台",
        lifespan=_lifespan,
    )

    # 开发环境 CORS（前端可能用不同端口）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    # Vue 构建产物里的静态资源（js/css 等），存在才挂载。
    if (_DIST_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST_DIR / "assets"), name="assets")
    # public/ 下的东西是原样拷进 dist 根目录的：favicon 与品牌页不在 /assets 里，
    # 不单独挂载就会 404（浏览器控制台里能直接看到一条红色报错）。
    if (_DIST_DIR / "brand").is_dir():
        app.mount("/brand", StaticFiles(directory=_DIST_DIR / "brand"), name="brand")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        icon = _DIST_DIR / "favicon.svg"
        if not icon.exists():
            raise HTTPException(404, "favicon 未构建")
        return FileResponse(icon, media_type="image/svg+xml")

    @app.exception_handler(OperationalError)
    def on_db_unavailable(_request: Request, exc: OperationalError) -> JSONResponse:
        """数据库连不上时给一条人话，而不是 500 + 堆栈。

        Postgres 没起是本地最常见的失败：没有这个处理器，前端拿到的是一坨
        500 HTML，只会在界面上滚一圈「未知错误」。
        """
        return JSONResponse(
            status_code=503,
            content={
                "detail": "数据库连接失败：请确认 DATABASE_URL 指向的数据库已启动；"
                "本机免部署可改成 sqlite:///./data/app.db（见 .env.example）。"
                f"原始错误：{exc.orig}"[:400],
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        # 托管 Vue 构建产物（frontend/dist）；未构建时给出提示。
        index = _DIST_DIR / "index.html"
        if index.exists():
            return HTMLResponse(index.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<h1>剧本智能体</h1>"
            "<p>前端未构建。请先在 <code>frontend/</code> 目录运行 "
            "<code>npm run build</code>，或访问 <a href=\"/docs\">/docs</a> 查看 API。</p>"
        )

    return app


app = create_app()
