"""对话层的「生成资产」工具：角色音色克隆。

为什么是工具而不是按钮：生成要用户逐项决定（给谁生成、声音描述、样本音频），
这些决定发生在对话里；工具让 Agent 能**问清楚再执行**，而不是一键开跑。
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from langchain_core.tools import tool

from .context import ToolContext


def build_asset_tools(ctx: ToolContext) -> list[Any]:
    @tool
    def create_location_state(project_id: str, location: str, name: str,
                              description: str) -> str:
        """为已有地点建立一个新的剧情状态（如夜晚停电、火灾后）。只登记描述，不生成图片；之后用 generate_reference_assets 的 location_state_id 单独生图。参数：location=地点名或 id；name=状态名称；description=该状态画面细节。"""
        p = ctx.project(project_id)
        version = ctx.store.latest_version(p)
        if not version:
            return "还没有剧本，无法建立地点状态。"
        from ...media.assets import build_index
        idx = build_index(version.script)
        lid = idx.resolve(location) or location
        if lid not in {loc.id for loc in version.script.locations}:
            return "剧本中找不到该地点，请先核对地点名称。"
        if not name.strip() or not description.strip():
            return "状态名称和画面描述都不能为空。"
        sid = f"location_state_{uuid4().hex[:12]}"
        style = ctx.store.get_video_style(p.id) or {}
        states = dict(style.get("location_states") or {})
        states[sid] = {"id": sid, "location_id": lid, "name": name.strip(),
                       "description": description.strip(), "image": "", "status": "draft"}
        ctx.store.merge_video_style(p.id, {"location_states": states})
        return f"已为{idx.display(lid)}建立场景状态「{name.strip()}」（{sid}）；尚未生成图片。"

    @tool
    def select_location_state(project_id: str, scope: str, target_id: str,
                              state_id: str) -> str:
        """把已保存的地点状态用于一个场次或镜头。scope=scene/shot；target_id=场次或镜头 id；state_id 从 list_assets 取得。必须按用户确认的剧情范围选择，不要自动套用到所有镜头。"""
        p = ctx.project(project_id)
        version = ctx.store.latest_version(p)
        if not version:
            return "还没有剧本，无法选择场景状态。"
        style = ctx.store.get_video_style(p.id) or {}
        from ...media.location_states import all_states
        state = all_states(style, version.script).get(state_id)
        if not state:
            return "场景状态不存在，请先用 list_assets 查 id。"
        if scope == "scene":
            scene = next((s for s in version.script.scenes if s.id == target_id), None)
            key = "scene_location_state_ids"
        elif scope == "shot":
            video = ctx.latest_video_version(p)
            shot = next((s for s in (video.shots if video else [])
                         if isinstance(s, dict) and s.get("id") == target_id), None)
            scene = next((s for s in version.script.scenes if s.id == shot.get("scene_id")), None) if shot else None
            key = "shot_location_state_ids"
        else:
            return "scope 只能是 scene 或 shot。"
        if not scene or scene.location_id != state.get("location_id"):
            return "这个场次/镜头不存在，或状态不属于它的地点。"
        mapping = dict(style.get(key) or {})
        mapping[target_id] = state_id
        ctx.store.merge_video_style(p.id, {key: mapping})
        return f"已为 {target_id} 选用场景状态「{state.get('name') or state_id}」。"

    @tool
    def list_assets(project_id: str, kind: str = "") -> str:
        """列出资产库：上传过的素材（参考视频/音频样本/图片）、已注册的定妆图与场景图、已克隆的音色，带 id。用户要「看看资产库 / 有哪些素材」时调用；clone_voice 需要音频样本 id、submit 需要参考视频时，id 都从这里取，不要让用户去界面里找。参数：project_id=项目 id；kind 可选 video/audio/image，留空=全部。"""
        p = ctx.project(project_id)
        blocks: list[str] = []

        assets = ctx.store.list_assets(p.id, kind=(kind.strip() or None))
        if assets:
            lines = [f"【素材】{len(assets)} 条："]
            for a in assets[:30]:
                label = {"video": "视频", "audio": "音频", "image": "图片"}.get(a.kind, a.kind)
                # list_assets 不再加载 content 本体（大 BLOB 排序会撑爆 SQLite），
                # 大小用 SQL 层算好的 content_size。
                cs = getattr(a, "content_size", None)
                size = f"（{cs // 1024} KB）" if cs else ""
                lines.append(f"- [{a.id}] {label}《{a.name or '未命名'}》{size}")
            if len(assets) > 30:
                lines.append(f"（其余 {len(assets) - 30} 条略）")
            blocks.append("\n".join(lines))
        else:
            blocks.append("【素材】资产库里没有上传过素材。")

        style = ctx.store.get_video_style(p.id) or {}
        refs = style.get("reference_images") or {}
        if refs:
            blocks.append("【定妆图/场景图】已注册 " + "、".join(f"{k}→参考图" for k in list(refs)[:12]) + "（按实体 id 索引）")
        states = style.get("location_states") or {}
        if states:
            blocks.append("【场景状态】\n" + "\n".join(
                f"- [{sid}] {item.get('location_id')} / {item.get('name')}（{'有图' if item.get('image') else '待生成'}）"
                for sid, item in list(states.items())[:20] if isinstance(item, dict)))

        voices = ctx.store.list_user_voices(p.id)
        if voices:
            lines = ["【专属音色】"]
            for v in voices[:12]:
                lines.append(f"- {v.name}（voice_id: {v.voice_id}，角色 {v.character_id or '未绑定'}）")
            blocks.append("\n".join(lines))

        return "\n".join(blocks)

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
            return f"找不到素材 {sample_asset_id}。先让用户把样本音频导入资产库，再用 list_assets 查到 id 传给我。"
        # 归属校验（2026-11-11 权限审计）：素材必须属于本项目——否则能拿
        # B 作品的音频给 A 的角色克隆音色，跨作品混数据。
        if asset.project_id != project.id:
            return f"素材 {sample_asset_id} 不属于本项目《{project.title}》，不能拿来克隆本项目的音色。"
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

    @tool
    def search_assets(project_id: str, kind: str = "", query: str = "") -> str:
        """在资产库里搜东西（用户说「那条白模」「之前导的那个视频」时用）。

        返回带 id 的条目——后面 save_asset / attach_asset 都要用这个 id。
        参数：project_id=项目 id；kind 可选 video/audio/image，留空=全部；
        query 可选，按名字或类型关键词过滤。
        """
        p = ctx.project(project_id)
        k = (kind or "").strip()
        q = (query or "").strip()
        lines: list[str] = []

        assets = ctx.store.list_assets(p.id, kind=(k or None))
        hits = [
            a for a in assets
            if not q or q in (a.name or "") or q in (a.source or "") or q == (a.kind or "")
        ]
        if hits:
            lines.append(f"【素材】{len(hits)} 条：")
            for a in hits[:30]:
                label = {"video": "视频", "audio": "音频", "image": "图片"}.get(a.kind, a.kind)
                lines.append(f"- [{a.id}] {label}《{a.name or '未命名'}》"
                             f"（{a.source or '导入'}）")
            if len(hits) > 30:
                lines.append(f"（其余 {len(hits) - 30} 条略）")
        else:
            lines.append("【素材】没有匹配的素材。")

        style = ctx.store.get_video_style(p.id) or {}
        refs = style.get("reference_images") or {}
        if refs:
            lines.append("【已注册的定妆照 / 场景图】")
            for cid, url in list(refs.items())[:12]:
                lines.append(f"- {cid} → {url}")

        voices = ctx.store.list_user_voices(p.id)
        if voices:
            lines.append("【专属音色】")
            for v in voices[:12]:
                lines.append(f"- [voice_id: {v.voice_id}] {v.name}"
                             f"（角色 {v.character_id or '未绑定'}）")
        return "\n".join(lines)

    @tool
    def save_asset(project_id: str, entity: str, asset_id: str, note: str = "") -> str:
        """把一个素材存成某个角色 / 场景的参考图（定妆照 / 场景图）。

        参数：project_id=项目 id；entity=角色名或场景名（如「孔乙己」「咸亨酒店」）；
        asset_id=素材 id（用 search_assets 查）；note 可选，画面要求备注。
        """
        p = ctx.project(project_id)
        name = (entity or "").strip()
        aid = (asset_id or "").strip()
        if not name or not aid:
            return "要指定「哪个角色/场景」和「哪个素材 id」。"
        asset = ctx.store.get_asset(aid)
        if asset is None:
            return f"没有这个素材：{aid}。用 search_assets 看可用的 id。"
        if asset.project_id != p.id:
            return f"素材 {aid} 不属于本作品，不能存进资产库。"
        if asset.kind != "image":
            return "只有图片能当定妆照 / 场景图。"

        from ...media.assets import build_index
        from ...media.library import apply_library_patch

        script = None
        version = ctx.store.latest_version(p)
        if version is not None:
            script = version.script
        idx = build_index(script)
        entity_id = idx.resolve(name)
        if not entity_id:
            return f"剧本里没有「{name}」这个角色或场景，名字要跟剧本对上。"
        char_ids = {c.id for c in (script.characters if script else [])}
        loc_ids = {l.id for l in (script.locations if script else [])}
        if entity_id in char_ids:
            patch_data = {"characters": {name: {"image": asset.url}}}
            what = "定妆照"
        elif entity_id in loc_ids:
            patch_data = {"locations": {name: {"image": asset.url}}}
            what = "场景图"
        else:
            return f"「{name}」在剧本里既不是角色也不是场景。"
        if (note or "").strip():
            key = "characters" if entity_id in char_ids else "locations"
            patch_data[key][name]["note"] = note.strip()

        style = ctx.store.get_video_style(p.id) or {}
        merged = apply_library_patch(style, patch_data, script)
        ctx.store.set_video_style(p.id, merged)
        return f"已把素材 {aid} 存成「{name}」的{what}。"

    @tool
    def attach_asset(project_id: str, asset_id: str, shot_ids: str = "") -> str:
        """把一个素材挂到镜头上（当参考视频 / 参考图，出片时一起发给模型）。

        参数：project_id=项目 id；asset_id=素材 id（用 search_assets 查）；
        shot_ids 可选，逗号分隔的镜头 id，留空=当前方案的全部镜头。
        """
        p = ctx.project(project_id)
        aid = (asset_id or "").strip()
        if not aid:
            return "要指定素材 id（用 search_assets 查）。"
        asset = ctx.store.get_asset(aid)
        if asset is None:
            return f"没有这个素材：{aid}。用 search_assets 看可用的 id。"
        if asset.project_id != p.id:
            return f"素材 {aid} 不属于本作品，不能挂到镜头上。"

        v = ctx.latest_video_version(p)
        if v is None or not v.shots:
            return "还没有镜头方案，先「拆解镜头」。"
        wanted = {s.strip() for s in (shot_ids or "").split(",") if s.strip()}
        shots = [dict(s) for s in v.shots if isinstance(s, dict)]
        touched = 0
        for s in shots:
            if wanted and s.get("id") not in wanted:
                continue
            refs = list(s.get("reference_asset_ids") or [])
            if aid in refs:
                continue
            if len(refs) >= 3:
                continue
            refs.append(aid)
            s["reference_asset_ids"] = refs
            touched += 1
        if not touched:
            if wanted:
                return "这些镜头都挂满 3 条参考素材了（上限 3）。"
            return "这个素材已经挂在所有镜头上了。"
        new_version = ctx.store.create_video_version(
            project_id=p.id, shots=shots, style_guide=v.style_guide,
            source_type="manual", label="挂参考素材",
            notes=f"asset:{aid}", parent_version_id=v.id,
        )
        return f"已把素材 {aid} 挂到 {touched} 个镜头上（新镜头版本 {new_version.id}）。"

    return [list_assets, create_location_state, select_location_state,
            clone_voice, search_assets, save_asset, attach_asset]
