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
    """MiniMax（海螺）视频生成，V2 任务接口。厂商名不带模型名（D-48：
    MiniMax 就是厂商，H3/H3-Lite 是它的模型档位）。"""

    name = "minimax"
    label = "MiniMax"
    supports_text_to_video = True
    supports_image_to_video = True
    max_duration_sec = 15.0
    pricing_per_sec = 0.045  # 粗略估算值，仅用于费用预估展示
    supported_resolutions = ["480P", "768P", "2K"]
    chinese_prompt_support = True

    # H3 系列档位表（官方定价页 2026-11-12 截图逐格核对）。要点：
    # - 秒价**按分辨率分档**（H3: 2K ¥0.80 / 768P ¥0.50；Highspeed: 768P ¥0.40 /
    #   480P ¥0.26；Lite: 768P ¥0.26 / 480P ¥0.16）；
    # - 计费公式 total_seconds = input_seconds + output_seconds——参考视频与
    #   输出同价；
    # - 图片输入每次任务**前 5 张免费**，超出按张计（H3/Highspeed ¥0.20、
    #   Lite ¥0.13），单价不随分辨率变。
    # 本地调试默认 Lite（_DEFAULT_MODEL）；设置页三档可切。
    _DEFAULT_MODEL = "MiniMax-H3-Lite"
    _TIERS: dict[str, dict[str, Any]] = {
        "MiniMax-H3": {
            "res": ("768P", "2K"), "min_dur": 4,
            "per_sec": {"768P": 0.50, "2K": 0.80}, "image_cny": 0.20},
        "MiniMax-H3-Highspeed": {
            "res": ("480P", "768P"), "min_dur": 4,
            "per_sec": {"480P": 0.26, "768P": 0.40}, "image_cny": 0.20},
        "MiniMax-H3-Lite": {
            "res": ("480P", "768P"), "min_dur": 4,
            "per_sec": {"480P": 0.16, "768P": 0.26}, "image_cny": 0.13},
        # Max 不在这页定价表上（极速生成档），能力按文档、费率沿用 Highspeed 近似。
        "MiniMax-H3-Max": {
            "res": ("480P", "768P"), "min_dur": 5,
            "per_sec": {"480P": 0.26, "768P": 0.40}, "image_cny": 0.20},
    }
    _FREE_IMAGES_PER_TASK = 5

    def _tier(self) -> dict[str, Any]:
        return self._TIERS.get(self.model or self._DEFAULT_MODEL,
                               self._TIERS[self._DEFAULT_MODEL])

    # 任务 → 提交时用的 key。跨账号轮换后，查询必须用「提交它的那个账号」的
    # key，否则会拿到 invalid task_id。进程内字典足够：任务在同一个进程里轮询。
    _TASK_KEYS: dict[str, str] = {}
    _TASK_KEYS_CAP = 512

    def __init__(self, api_key: str = "", base_url: str = "", **kwargs: Any) -> None:
        # key 只从调用方注入（DB api_providers 行，D-48：env 不再是配置入口）。
        # 备用 key 故障转移（_ring → minimax_keys 读 FALLBACKS）保留——那是安全网。
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
          {"type": "audio_url", "audio_url": {"url": ...}, "role": "reference_audio"}
        数量上限（官方文档）：首/尾帧各 ≤2、参考图 ≤9、参考视频 ≤3、参考音频 ≤3，混合 ≤12。
        互斥（官方文档 2026-11-12 确认）：**图生视频（first/last_frame）与多模态
        参考（reference_*）不可混用**——混了服务端行为未定义，这里按「参考优先、
        丢弃首尾帧」收敛并警告（接力参考是我们链路的主用法）。
        """
        if params.content:
            return list(params.content)

        has_ref = bool(params.reference_images or params.reference_videos
                       or params.reference_audios)
        content: list[dict[str, Any]] = []
        if prompt.strip():
            content.append({"type": "text", "text": prompt})
        if has_ref and (params.image_url or params.last_frame_image):
            log.warning("MiniMax V2：首尾帧与参考素材互斥，本镜按参考优先提交（丢弃首/尾帧图）")
        elif not has_ref:
            if params.image_url:
                content.append({"type": "image_url", "image_url": self._media(params.image_url), "role": "first_frame"})
            if params.last_frame_image:
                content.append({"type": "image_url", "image_url": self._media(params.last_frame_image), "role": "last_frame"})
        for url in params.reference_images[:9]:
            content.append({"type": "image_url", "image_url": self._media(url), "role": "reference_image"})
        for url in params.reference_videos[:3]:
            content.append({"type": "video_url", "video_url": self._media(url), "role": "reference_video"})
        for url in params.reference_audios[:3]:
            content.append({"type": "audio_url", "audio_url": self._media(url), "role": "reference_audio"})

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
        tier = self._tier()
        # 分辨率按**当前档位**的白名单收敛（2026-11-12 文档确认：Highspeed/Lite/Max
        # 不支持 2K；旗舰 H3 反而没有 480P）。此前只对照全类 supported_resolutions，
        # 切到 Highspeed 后传 2K 会被服务端拒。优先级：调用方显式 > extra > 全局默认。
        from ..base import default_resolution

        allowed = tier["res"]
        candidates = [
            params.resolution if params.resolution in allowed else "",
            str(extra.pop("resolution", "") or "") if extra.get("resolution") in allowed else "",
            default_resolution() if default_resolution() in allowed else "",
        ]
        resolution = next((c for c in candidates if c), allowed[-1] if allowed else "768P")
        # 时长：档位下限（Max 不支持 4s）到 15s。
        duration = max(tier["min_dur"], min(15, int(params.duration_sec or 6)))
        body: dict[str, Any] = {
            # 模型名：用户设置里显式选的优先，缺省 H3。
            "model": self.model or self._DEFAULT_MODEL,
            "content": content,
            "duration": duration,
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

        cost = self.estimate_cost_real(params, duration, resolution)
        return VideoJobResponse(external_task_id=task_id, status="queued", cost_estimate=cost)

    def estimate_cost_real(self, params: VideoJobParams, out_sec: int,
                           resolution: str = "") -> float:
        """按官方计费公式估算费用（返回美元——DB 的 cost_estimate 前端按 USD 展示）。

        计费：(输出秒 + 输入视频秒) × **档位×分辨率**秒价（¥）
        + 超出免费额度的输入图 × 档位图价，再折美元。
        输入视频秒数按素材形态近似：裁尾接力片段 ≈2.5s，整条按本镜输出秒近似
        ——宁可高估给用户看，别再像旧版低估 5 倍（¥8.3/镜 vs 旧估 ¥1.3）。"""
        tier = self._tier()
        # 缺省按档位主档（元组首项：H3=768P、Lite/Highspeed=480P）估，
        # 传进来的分辨率不在白名单（如 WxH 方言）也落主档，别按最贵档虚报。
        res = resolution if resolution in tier["res"] else tier["res"][0]
        per_sec = tier["per_sec"][res]
        approx_in_sec = sum(
            2.5 if str(u).startswith("data:") else float(out_sec)
            for u in (params.reference_videos or [])[:3]
        )
        billable_images = max(0, len(params.reference_images or []) - self._FREE_IMAGES_PER_TASK)
        cny = (out_sec + approx_in_sec) * per_sec + billable_images * tier["image_cny"]
        return round(cny / 7.1, 4)

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
