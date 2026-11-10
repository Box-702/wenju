# =====================================================================
# minimax.py —— MiniMax H3（海螺）视频生成适配器（V2 接口）
#
# API：https://api.minimax.cn / https://api.minimaxi.com（国内站，二选一）
# 文档：开发指南 > API > 视频 > MiniMax-H3 > 创建视频生成任务
# 认证：Bearer API Key
# 请求体为多模态 content 数组（文本 / 图片 / 视频 / 音频），
# 文生视频只需一个 text 项；duration 4-15s；分辨率 480P / 768P / 2K。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

import httpx

from ...keys import (
    RATE_LIMIT_COOLDOWN_SEC,
    QUOTA_HINTS,
    error_message,
    is_rate_limited,
    key_failure,
    minimax_keys,
    mask,
    ring_for,
)
from ..base import (
    DEFAULT_ASPECT,
    VideoJobParams,
    VideoJobResponse,
    VideoJobStatus,
    VideoProvider,
)

log = logging.getLogger(__name__)


def _payload(resp: httpx.Response) -> Any:
    """尽量解析响应体；非 JSON 时退回文本，供错误分类使用。"""
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return resp.text


def _invalid_task(data: Any) -> bool:
    """响应是否表示「这个账号查不到该任务」（2013 / 2014）。

    跨账号轮换时会出现：key 本身是好的，只是任务属于另一个账号。
    这种情形要换 key 继续查，但绝不能把那把好 key 判成失效。
    """
    if not isinstance(data, dict):
        return False
    base = data.get("base_resp")
    if isinstance(base, dict):
        if base.get("status_code") in (2013, 2014):
            return True
    err = data.get("error")
    if isinstance(err, dict):
        message = str(err.get("message", ""))
        return "(2013)" in message or "(2014)" in message or "invalid task" in message.lower()
    return False


def _has_task_object(data: Any) -> bool:
    """查询响应里是否直接带着「任务本身」。

    带任务就是事实，**必须先于 key 判据处理**：任务失败的死因写在
    task.error 里（如 1008 insufficient balance），这段 JSON 里恰好也有
    "1008" 字样——不先拦下来，body 扫描的 key 判据会把它当成「这把 key 坏了」，
    把好端端的 key 环全部冷却，最后报「所有 key 都不可用」。
    完整跑（2026-11-10）实测：账号没钱让两把 key 双双进 600s 冷却，
    真实死因（insufficient balance）反而被埋掉了。
    """
    return isinstance(data, dict) and isinstance(data.get("task"), dict)


class MiniMaxProvider(VideoProvider):
    """MiniMax H3（海螺）视频生成，V2 任务接口。"""

    name = "minimax"
    label = "MiniMax H3"
    supports_text_to_video = True
    supports_image_to_video = True
    max_duration_sec = 15.0
    pricing_per_sec = 0.045  # 粗略估算值，仅用于费用预估展示
    supported_resolutions = ["480P", "768P", "2K"]
    chinese_prompt_support = True

    # 任务 → 提交时用的 key。跨账号轮换后，查询必须用「提交它的那个账号」的
    # key，否则会拿到 invalid task_id。进程内字典足够：任务在同一个进程里轮询。
    _TASK_KEYS: dict[str, str] = {}
    _TASK_KEYS_CAP = 512

    def __init__(self, api_key: str = "", base_url: str = "", **kwargs: Any) -> None:
        import os
        api_key = api_key or os.environ.get("MINIMAX_API_KEY", "")
        super().__init__(api_key, base_url, **kwargs)
        self._last_key = ""
        if not self.base_url:
            self.base_url = "https://api.minimax.cn"

    # ---- key 轮换 ----

    def _ring(self):
        """候选 key 环：本实例的 key 优先，其后是 .env 里的备用 key。"""
        return ring_for("minimax", minimax_keys(self.api_key))

    @classmethod
    def _remember(cls, task_id: str, key: str) -> None:
        cls._TASK_KEYS[task_id] = key
        if len(cls._TASK_KEYS) > cls._TASK_KEYS_CAP:
            # 粗暴但够用：超限时丢掉最早写入的一批。
            for stale in list(cls._TASK_KEYS)[: cls._TASK_KEYS_CAP // 4]:
                cls._TASK_KEYS.pop(stale, None)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float = 60.0,
        action: str = "请求",
        task_id: str = "",
    ) -> Any:
        """带 key 轮换的请求：key 级错误（401/402/1004/1008）就换下一把重试。

        参数错误（400/2013 等）不换 key——换 key 也没用，直接抛出去让上层看见。
        """
        ring = self._ring()
        candidates = ring.candidates()
        bound = self._TASK_KEYS.get(task_id) if task_id else ""
        if bound and bound in candidates:
            candidates = [bound] + [k for k in candidates if k != bound]
        if not candidates:
            raise RuntimeError(f"MiniMax {action}失败：没有配置 API Key")

        failures: list[str] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for key in candidates:
                headers = {"Authorization": f"Bearer {key}"}
                if method.upper() == "GET":
                    resp = await client.get(url, headers=headers)
                else:
                    headers["Content-Type"] = "application/json"
                    resp = await client.post(url, headers=headers, json=body)

                data = _payload(resp)
                # 带回任务对象的响应 = 事实，先交给调用方映射，别过 key 判据。
                # （任务的失败码长什么样都不该让一把好 key 背锅冷却。）
                # 查询（GET）如此；创建被限流时 MiniMax 也会回 {"task":…}——同理，
                # poll 早就有 type==error 的分支兜住提交侧的真错误。
                # 不带 task 的纯错误信封（type==error）仍走 key 判据——那才是 key 的事。
                if not _has_task_object(data) and key_failure(resp.status_code, data):
                    reason = error_message(data, resp.status_code)
                    ring.mark_bad(
                        key, reason,
                        cooldown=RATE_LIMIT_COOLDOWN_SEC if is_rate_limited(resp.status_code, data) else None,
                    )
                    failures.append(f"{mask(key)} {reason}")
                    continue
                # 账号里查不到这个任务：可能是换了账号，继续试下一把，不判 key 坏。
                if task_id and resp.status_code < 400 and _invalid_task(data):
                    failures.append(f"{mask(key)} 查不到该任务")
                    continue

                resp.raise_for_status()
                ring.mark_ok(key)
                self._last_key = key  # 供 create_job 记录「这笔任务属于哪个账号」
                if task_id:
                    self._remember(task_id, key)
                return data

        raise RuntimeError(
            f"MiniMax {action}失败：所有 key 都不可用（{'；'.join(failures)}）"
        )

    async def _send_with_backoff(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float = 60.0,
        action: str = "请求",
        task_id: str = "",
        attempts: int = 4,
    ) -> Any:
        """在 ``_send`` 之外再包一层「限流退避」。

        为什么必须有这一层：MiniMax 对**并发任务数**有限制，批量出片时连着提交
        十几镜会直接吃到 429。旧代码把它当普通失败抛出去，结果是一整批镜头全挂
        （2026-11-01 实测：13 镜 1 秒间隔提交，13 个全 429 失败）。
        429 是「现在不行，等会儿可以」，不是「这个请求有问题」——必须退避重试。

        注意 key 轮换在 ``_send`` 里已经做过，这一层只管时间维度的重试，
        不要重复换 key（换 key 解决不了并发限流，换账号也一样受限）。
        """
        import asyncio

        last: Exception | None = None
        for i in range(attempts):
            try:
                return await self._send(
                    method, url, body=body, timeout=timeout,
                    action=action, task_id=task_id,
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 429:
                    raise
                last = e
                if i == attempts - 1:
                    break
                # 2/4/8 秒退避；并发限流通常几秒内就腾出配额。
                wait = 2 ** (i + 1)
                log.warning("MiniMax %s被限流（429），%ss 后重试（第 %d/%d 次）",
                            action, wait, i + 2, attempts)
                await asyncio.sleep(wait)
            except RuntimeError as e:
                msg = str(e).lower()
                if "所有 key 都不可用" not in str(e):
                    raise
                # 额度用尽（2056 / 用量上限）等多久都不会恢复，退避只是白等。
                if any(h.lower() in msg for h in QUOTA_HINTS):
                    raise
                # 全部 key 都被限流时 _send 会抛「所有 key 都不可用（… Too Many Requests）」。
                # 这也只是时间问题，该退避。但欠费/鉴权失败（1004/1008）不能退避——
                # 那是要人工处理的，重试一百次也一样。
                limited = "too many requests" in msg or "429" in msg or "rate limit" in msg
                if not limited:
                    raise
                last = e
                if i == attempts - 1:
                    break
                wait = 2 ** (i + 1)
                log.warning("MiniMax %s：全部 key 被限流，%ss 后重试（第 %d/%d 次）",
                            action, wait, i + 2, attempts)
                await asyncio.sleep(wait)
        raise RuntimeError(f"MiniMax {action}被限流：重试 {attempts} 次仍 429") from last

    # ---- 多模态 content 组装（V2 接口：文本 / 图片 / 视频 / 音频） ----

    @staticmethod
    def _media(url: str) -> dict[str, Any]:
        """V2 的媒体地址是结构化对象：{"url": ...}。"""
        return {"url": url}

    def _build_content(self, prompt: str, params: VideoJobParams) -> list[dict[str, Any]]:
        """把统一参数组装成 V2 content 数组。

        项格式（经服务端实测确认）：
          {"type": "text", "text": ...}
          {"type": "image_url", "image_url": {"url": ...}, "role": "first_frame|last_frame|reference_image"}
          {"type": "video_url", "video_url": {"url": ...}, "role": "reference_video"}
          {"type": "audio_url", "audio_url": {"url": ...}}
        数量上限（官方文档）：首/尾帧各 ≤2、参考图 ≤9、参考视频 ≤3、参考音频 ≤3，混合 ≤12。
        """
        if params.content:
            return list(params.content)

        content: list[dict[str, Any]] = []
        if prompt.strip():
            content.append({"type": "text", "text": prompt})
        if params.image_url:
            content.append({"type": "image_url", "image_url": self._media(params.image_url), "role": "first_frame"})
        if params.last_frame_image:
            content.append({"type": "image_url", "image_url": self._media(params.last_frame_image), "role": "last_frame"})
        for url in params.reference_images:
            content.append({"type": "image_url", "image_url": self._media(url), "role": "reference_image"})
        for url in params.reference_videos:
            content.append({"type": "video_url", "video_url": self._media(url), "role": "reference_video"})
        for url in params.reference_audios:
            content.append({"type": "audio_url", "audio_url": self._media(url)})

        if not content:
            raise ValueError("prompt 与多模态输入（首尾帧 / 参考素材）至少要有一项")
        media_count = sum(1 for item in content if item.get("type") != "text")
        if media_count > 12:
            raise ValueError("多模态素材总数超过上限（≤12）")
        return content

    async def create_job(self, prompt: str, params: VideoJobParams | None = None) -> VideoJobResponse:
        params = params or VideoJobParams()
        content = self._build_content(prompt, params)

        extra = dict(params.extra)
        # 分辨率/画幅用本 provider 的方言（480P/768P/2K）。优先级：调用方显式
        # 指定 > extra > 全局默认配置（VIDEO_DEFAULT_RESOLUTION，默认 768P 低挡位）。
        from ..base import default_resolution
        resolution = (
            params.resolution
            if params.resolution in self.supported_resolutions
            else extra.pop("resolution", None)
            or (default_resolution() if default_resolution() in self.supported_resolutions else "768P")
        )
        body: dict[str, Any] = {
            # 模型名：用户设置里显式选的优先，缺省 H3。
            "model": self.model or "MiniMax-H3",
            "content": content,
            # H3 支持 4-15 秒，夹到合法区间，缺省 6 秒。
            "duration": max(4, min(15, int(params.duration_sec or 6))),
            "resolution": resolution,
            "ratio": extra.pop("ratio", params.aspect_ratio or DEFAULT_ASPECT),
        }
        body.update(extra)

        data = await self._send_with_backoff(
            "POST",
            f"{self.base_url}/v2/video_generation",
            body=body,
            timeout=60,
            action="提交",
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"MiniMax 提交失败：响应不是 JSON（{str(data)[:120]}）")

        # V2 的错误返回 {"type":"error","error":{"message":...}}，HTTP 状态也可能是 4xx。
        if data.get("type") == "error" or not data.get("task_id"):
            message = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else str(data)
            raise RuntimeError(f"MiniMax 提交失败：{message}")

        task_id = str(data["task_id"])
        # 记下提交用的 key：跨账号轮换后查询必须回到同一个账号，否则查不到任务。
        if self._last_key:
            self._remember(task_id, self._last_key)

        cost = self.estimate_cost(body["duration"])
        return VideoJobResponse(external_task_id=task_id, status="queued", cost_estimate=cost)

    async def poll_job(self, external_task_id: str) -> VideoJobStatus:
        data = await self._send_with_backoff(
            "GET",
            f"{self.base_url}/v2/query/video_generation/{external_task_id}",
            timeout=30,
            action="查询",
            task_id=external_task_id,
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"MiniMax 查询失败：响应不是 JSON（{str(data)[:120]}）")

        if data.get("type") == "error":
            err = data.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else str(data)
            raise RuntimeError(f"MiniMax 查询失败：{message}")

        # V2 的任务对象嵌在 "task" 键下：{"task": {"id":..., "status":..., "content": {...}}}
        task = data.get("task") if isinstance(data.get("task"), dict) else data
        status_str = str(task.get("status", "")).lower()
        # 注意 "succeeded" 并不包含子串 "success"（中间是 "succee"），必须用 "succeed" 匹配。
        if "succeed" in status_str or status_str in {"completed", "done"}:
            mapped = "succeeded"
        elif "fail" in status_str:
            mapped = "failed"
        elif "cancel" in status_str:
            mapped = "cancelled"
        elif "queue" in status_str:
            mapped = "queued"
        else:
            mapped = "generating"

        # 成功后视频直链在 task.content.url（无需再用 file_id 换取）。
        video_url = None
        if mapped == "succeeded":
            content = task.get("content")
            if isinstance(content, dict):
                video_url = content.get("url")

        # 失败原因在**任务对象**里：{"task":{"status":"failed","error":{"code":"1008",
        # "message":"insufficient balance"}}}。曾经从外层 data 找 error（那里没有），
        # 结果是任务明明死因明确（账号没钱），用户看到的却是空的 failed——
        # 完整跑（2026-11-10）时撞出来的：排查全靠人工再查一遍厂商接口。
        error_message: str | None = None
        if mapped == "failed":
            task_err = task.get("error")
            if isinstance(task_err, dict):
                code = str(task_err.get("code") or "").strip()
                message = str(task_err.get("message") or "").strip()
                error_message = f"{message} ({code})" if code else message
            elif task_err:
                error_message = str(task_err)
            if not error_message:
                error_message = str(data.get("error") or "").strip() or None

        return VideoJobStatus(
            external_task_id=external_task_id,
            status=mapped,
            progress=1.0 if mapped == "succeeded" else 0.5 if mapped == "generating" else 0.0,
            video_url=video_url,
            error_message=error_message,
            raw=data,
        )

    async def cancel_job(self, external_task_id: str) -> bool:
        log.info("MiniMax V2 取消任务 %s：需要 DELETE 接口，暂未接入", external_task_id)
        return False
