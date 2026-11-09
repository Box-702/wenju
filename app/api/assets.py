# =====================================================================
# assets.py —— 项目媒体素材（参考视频等）
#
# 两条入库通道：
#   - upload：文件本体直接存数据库（media_assets.content bytea），本地不落盘，
#     本服务提供回放；适合预览、归档。注意 MiniMax 等服务商拉取参考素材
#     要求公网可访问 URL，本机回放地址对他们不可达——那类素材请走 url 登记。
#   - url：直接登记外部公网链接（对象存储 / 图床 / 已生成成片的 CDN 地址）。
# =====================================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import common, deps
from ..store import gen_id

router = APIRouter()
log = logging.getLogger(__name__)

_ALLOWED_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi"}
# 音频：音色克隆的样本（角色专属音色的来源）
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_MAX_BYTES = 200 * 1024 * 1024  # 200MB

# 白模/定妆照等静态图片：用户自己导入的参考图
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB

_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
}

_IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


class AssetUrlCreate(BaseModel):
    """登记一个外部素材链接。"""

    url: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="", max_length=200)


def _asset_dict(a: Any) -> dict[str, Any]:
    return {
        "id": a.id,
        "project_id": a.project_id,
        "kind": a.kind,
        "name": a.name,
        "source": a.source,
        "url": a.url,
        "created_at": a.created_at.isoformat(),
    }


@router.get("/projects/{project_id}/assets")
def list_assets(project_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """列出项目的媒体素材（默认全部，可按 kind 过滤）。"""
    common.get_project(project_id)
    return [_asset_dict(a) for a in deps.store().list_assets(project_id, kind=kind)]


@router.post("/projects/{project_id}/assets/upload")
async def upload_asset(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008
    """上传一个媒体文件作为项目素材（文件本体存数据库，本地不落盘）。"""
    common.get_project(project_id)
    original = file.filename or "video.mp4"
    ext = Path(original).suffix.lower()
    if ext not in _ALLOWED_EXTS and ext not in _AUDIO_EXTS:
        raise HTTPException(
            400,
            f"不支持的格式 {ext}，可用：{' '.join(sorted(_ALLOWED_EXTS | _AUDIO_EXTS))}",
        )

    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > _MAX_BYTES:
            raise HTTPException(400, "文件超过 200MB 上限")

    asset_id = gen_id("asset")
    kind = "audio" if ext in _AUDIO_EXTS else "video"
    asset = deps.store().add_asset(
        project_id, kind=kind, name=original, source="upload",
        url=f"/api/assets/file/{asset_id}", id=asset_id, content=bytes(data),
    )
    return _asset_dict(asset)


@router.post("/projects/{project_id}/images/upload")
async def upload_image(project_id: str, file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008
    """上传一张参考图（角色定妆照 / 场景白模），字节入库，返回回放 URL。

    供资产库把 AI 生成的白模换成用户自己的图：前端拿到 url 后
    PATCH /library 把它绑到对应角色/场景上。
    """
    common.get_project(project_id)
    original = file.filename or "image.png"
    ext = Path(original).suffix.lower()
    if ext not in _IMAGE_EXTS:
        raise HTTPException(400, f"不支持的图片格式 {ext}，可用：{' '.join(sorted(_IMAGE_EXTS))}")

    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(400, "图片超过 20MB 上限")

    asset_id = gen_id("asset")
    asset = deps.store().add_asset(
        project_id, kind="image", name=original, source="upload",
        url=f"/api/assets/file/{asset_id}", id=asset_id, content=bytes(data),
    )
    return _asset_dict(asset)


@router.post("/projects/{project_id}/assets/url")
def add_asset_url(project_id: str, payload: AssetUrlCreate) -> dict[str, Any]:
    """登记一个外部素材 URL（公网直链，可直接喂给视频服务商）。"""
    common.get_project(project_id)
    url = payload.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL 必须以 http(s) 开头")
    name = payload.name.strip() or url.rsplit("/", 1)[-1] or "外部视频"
    asset = deps.store().add_asset(project_id, kind="video", name=name, source="url", url=url)
    return _asset_dict(asset)


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: str) -> dict[str, Any]:
    """删除素材记录；上传类文件尽力清理磁盘。

    Windows 下若回放流仍持有文件句柄，unlink 会失败——记录照删，
    留下的孤儿文件不阻塞删除操作（下次覆盖/手动清理）。
    """
    asset = deps.store().delete_asset(asset_id)
    if asset is None:
        raise HTTPException(404, "素材不存在")
    if asset.file_path:
        p = Path(deps.settings().effective_workspace_root) / asset.file_path
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("素材文件未能删除（可能正被回放占用）：%s -> %s", p, e)
    return {"deleted": asset_id}


@router.get("/assets/file/{asset_id}")
def get_asset_file(asset_id: str) -> Response:
    """回放素材：优先读数据库里的文件本体；历史磁盘素材向下兼容。"""
    asset = deps.store().get_asset(asset_id)
    if asset is None:
        raise HTTPException(404, "素材不存在")
    if asset.content is not None:
        suffix = Path(asset.name).suffix.lower()
        media_type = _MEDIA_TYPES.get(suffix) or _IMAGE_TYPES.get(suffix) or "application/octet-stream"
        quoted = quote(asset.name, safe="")
        return Response(
            content=asset.content,
            media_type=media_type,
            headers={"Content-Disposition": f"inline; filename*=utf-8''{quoted}"},
        )
    if not asset.file_path:
        raise HTTPException(404, "素材不存在")
    p = Path(deps.settings().effective_workspace_root) / asset.file_path
    if not p.is_file():
        raise HTTPException(404, "素材文件已丢失")
    return FileResponse(p, media_type=_MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        filename=asset.name)
