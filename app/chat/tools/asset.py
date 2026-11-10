"""对话层的「生成资产」工具：角色音色克隆。

为什么是工具而不是按钮：生成要用户逐项决定（给谁生成、声音描述、样本音频），
这些决定发生在对话里；工具让 Agent 能**问清楚再执行**，而不是一键开跑。
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from .context import ToolContext


def build_asset_tools(ctx: ToolContext) -> list[Any]:
    @tool
    def clone_voice(
        project_id: str,
        character: str,
        sample_asset_id: str,
        description: str = "",
        name: str = "",
    ) -> str:
        """给某个角色克隆一个专属音色（用样本音频 + 声音描述）。

        参数：project_id=项目 id；character=角色名或角色 id；sample_asset_id=样本音频的资产 id
        （先让用户把样本音频导入资产库，再把资产 id 传进来）；
        description=用户写的声音描述（如「低沉沙哑的中年男声，语速偏慢」）；
        name=这个音色的名字（可省略，默认用「角色名 的专属音色」）。

        克隆成功后音色会绑到这个角色上，之后配音就用它。
        """
        from ...config import get_settings
        from ...llm.registry import resolve_tts
        from ...llm.tts import build_tts_client
        from ...media.assets import build_index

        store = ctx.store
        project = ctx.project(project_id)
        version = store.latest_version(project)
        if version is None:
            return "还没有剧本版本，先先生成初稿。"

        # 角色 id
        idx = build_index(version.script)
        char_id = idx.resolve(character) or character
        char_name = idx.display(char_id) if char_id else character

        # 样本音频
        asset = store.get_asset(sample_asset_id)
        if asset is None:
            return f"找不到素材 {sample_asset_id}。先让用户把样本音频导入资产库，再把 id 传给我。"
        if asset.kind != "audio" or not asset.content:
            return f"素材「{asset.name}」不是可用的音频样本（需要音频文件）。"

        client = build_tts_client(resolve_tts(get_settings(), store))
        if client is None or not client.available:
            return "没有配置语音合成（需要 MINIMAX_API_KEY），暂时无法克隆音色。"

        voice_name = (name or "").strip() or f"{char_name} 的专属音色"
        try:
            voice_id = client.clone_voice(
                asset.content,
                filename=asset.name or "sample.mp3",
                preview_text=description or f"这是{char_name}的声音。",
            )
        except NotImplementedError as e:
            return f"这个语音厂商不支持克隆：{e}"
        except Exception as e:  # noqa: BLE001
            return f"音色克隆失败：{e}"

        store.save_user_voice(
            voice_id=voice_id,
            name=voice_name,
            project_id=project_id,
            character_id=char_id,
            sample_name=asset.name or "",
        )

        # 绑到角色：voice_casting（配音读它）+ voice_labels（显示名）+ voice_notes（描述）
        style = dict(store.get_video_style(project_id) or {})
        casting = dict(style.get("voice_casting") or {})
        labels = dict(style.get("voice_labels") or {})
        notes = dict(style.get("voice_notes") or {})
        casting[char_id] = voice_id
        labels[char_id] = voice_name
        if description:
            notes[char_id] = description
        style["voice_casting"] = casting
        style["voice_labels"] = labels
        style["voice_notes"] = notes
        store.merge_video_style(project_id, style)

        return (
            f"已为「{char_name}」生成音色「{voice_name}」（voice_id: {voice_id}），"
            f"已经绑到这个角色上，之后配音就用它。"
        )

    return [clone_voice]
