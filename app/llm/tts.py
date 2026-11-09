# =====================================================================
# tts.py —— 语音合成客户端（TTS）
#
# 用途：给成片配音——视频模型没有「文字→语音」通路（只会产出含混的
# 假说话），台词的语音必须由 TTS 单独合成，再按时间轴混到成片上
# （见 app/media/dubbing.py）。
#
# 与 image.py / vision.py 同构：一个客户端抽象 + 厂商实现 + 工厂函数，
# 上层只依赖 TTSClient 接口。
#
# MiniMax T2A v2 实测约定：
#   POST {base}/v1/t2a_v2
#   body: {model, text, voice_setting:{voice_id, speed}, audio_setting:{format:"mp3"}}
#   响应: {"data": {"audio": "<hex 编码的 mp3 字节>"}}；错误走 base_resp。
# =====================================================================

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..keys import (
    RATE_LIMIT_COOLDOWN_SEC,
    error_message,
    is_rate_limited,
    key_failure,
    mask,
    minimax_keys,
    ring_for,
)
from .base import ModelConfig

log = logging.getLogger(__name__)


def gen_voice_id() -> str:
    """克隆音色的 id：MiniMax 要求 8~256 位、字母数字下划线。"""
    import uuid

    return uuid.uuid4().hex[:16]


class TTSClient(ABC):
    """语音合成客户端抽象。"""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return bool(self.config.api_key and self.config.model)

    @property
    def label(self) -> str:
        return self.config.label or self.config.provider

    @abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "male-qn-qingse",
        speed: float = 1.0,
        timeout: float = 120.0,
    ) -> bytes:
        """合成一段语音，返回音频字节（mp3）。失败抛异常。"""
        ...

    def clone_voice(
        self,
        sample: bytes,
        *,
        filename: str = "sample.mp3",
        voice_id: str = "",
        preview_text: str = "",
        timeout: float = 180.0,
    ) -> str:
        """用一段样本音频克隆出专属音色，返回新的 voice_id。

        默认不支持（只有 MiniMax 实现了）；不支持的厂商抛异常，前端据此提示。
        """
        raise NotImplementedError(f"{self.label} 不支持音色克隆")


class MiniMaxTTSClient(TTSClient):
    """MiniMax T2A v2 语音合成。"""

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "male-qn-qingse",
        speed: float = 1.0,
        timeout: float = 120.0,
    ) -> bytes:
        body: dict[str, Any] = {
            "model": self.config.model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": max(0.5, min(2.0, speed)),
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            ring = ring_for("minimax", minimax_keys(self.config.api_key))
            candidates = ring.candidates()
            if not candidates:
                raise RuntimeError("MiniMax TTS 失败：没有配置 API Key")
            failures: list[str] = []
            for key in candidates:
                resp = client.post(
                    self.config.endpoint("v1/t2a_v2"),
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    data = resp.text
                # key 级错误（401/402/1004/1008）换下一把；参数错误直接抛。
                if key_failure(resp.status_code, data):
                    reason = error_message(data, resp.status_code)
                    ring.mark_bad(
                        key, reason,
                        cooldown=RATE_LIMIT_COOLDOWN_SEC if is_rate_limited(resp.status_code, data) else None,
                    )
                    failures.append(f"{mask(key)} {reason}")
                    continue
                resp.raise_for_status()
                ring.mark_ok(key)
                return self._parse_audio(data)

        raise RuntimeError(f"MiniMax TTS 失败：所有 key 都不可用（{'；'.join(failures)}）")

    def clone_voice(
        self,
        sample: bytes,
        *,
        filename: str = "sample.mp3",
        voice_id: str = "",
        preview_text: str = "",
        timeout: float = 180.0,
    ) -> str:
        """MiniMax 音色克隆：上传样本 → 克隆 → 返回新 voice_id。"""
        ring = ring_for("minimax", minimax_keys(self.config.api_key))
        candidates = ring.candidates()
        if not candidates:
            raise RuntimeError("音色克隆失败：没有配置 MiniMax API Key")
        failures: list[str] = []
        for key in candidates:
            try:
                with httpx.Client(trust_env=False, timeout=timeout) as client:
                    up = client.post(
                        self.config.endpoint("files/upload"),
                        headers={"Authorization": f"Bearer {key}"},
                        files={"file": (filename, sample, "application/octet-stream")},
                        data={"purpose": "voice_clone"},
                    )
                    up.raise_for_status()
                    file_id = str((up.json() or {}).get("file_id") or "")
                    if not file_id:
                        raise RuntimeError("上传样本后没拿到 file_id")

                    body = {
                        "file_id": int(file_id),
                        "voice_id": voice_id or f"wenju_voice_{gen_voice_id()}",
                        # 克隆时要给一段文本，厂商用它对齐音色
                        "text": (preview_text or "这是我的声音，用来给角色配音。")[:200],
                    }
                    resp = client.post(
                        self.config.endpoint("voice_clone"),
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json=body,
                    )
                    data = resp.json()
                    if key_failure(resp.status_code, data):
                        reason = error_message(data, resp.status_code)
                        ring.mark_bad(key, reason)
                        failures.append(f"{mask(key)} {reason}")
                        continue
                    resp.raise_for_status()
                    new_id = str((data or {}).get("voice_id") or body["voice_id"])
                    ring.mark_ok(key)
                    return new_id
            except Exception as e:  # noqa: BLE001
                failures.append(f"{mask(key)} {e}")
        raise RuntimeError(f"音色克隆失败：{'；'.join(failures)}")

    @staticmethod
    def _parse_audio(data: object) -> bytes:
        """从 T2A 响应里取出音频字节；错误走 base_resp。"""
        if not isinstance(data, dict):
            raise RuntimeError(f"MiniMax TTS 响应不是 JSON：{str(data)[:200]}")
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status") not in (0, None):
            raise RuntimeError(f"MiniMax TTS 失败：{base_resp.get('status_msg')}")
        audio_hex = (data.get("data") or {}).get("audio") or ""
        if not audio_hex:
            raise RuntimeError(f"MiniMax TTS 响应中没有音频：{str(data)[:200]}")
        return bytes.fromhex(audio_hex)


def build_tts_client(config: ModelConfig | None) -> TTSClient | None:
    """按 provider 标识构建 TTS 客户端；未知厂商返回 None。"""
    if config is None or not config.api_key:
        return None
    if config.provider == "minimax":
        return MiniMaxTTSClient(config)
    log.warning("未知的 TTS provider：%s", config.provider)
    return None


# 常用中文音色（MiniMax 系统音色）。
VOICE_FEMALE_YOUNG = "female-shaonv"    # 少女
VOICE_FEMALE_MATURE = "female-yujie"    # 御姐
VOICE_MALE_YOUNG = "male-qn-qingse"     # 青涩青年
VOICE_MALE_MATURE = "male-qn-badao"     # 磁性男声
