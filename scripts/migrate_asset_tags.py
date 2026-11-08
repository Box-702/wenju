"""把已生成的视频提示词里的资产标记迁移到 @ 语法。

背景：资产标记从 `［资产:char_005］` 改成 `@名字(编号)` 之后（D-33），
**已经生成的提示词还是旧写法**。用户看到的 50 条提示词都不符合新规则。

这里做一次确定性的迁移（不调模型、不重新生成）：
  旧：`咸亨酒店，…［资产:loc_main］。人物：小伙计［资产:char_005］：十五岁…`
  新：`@咸亨酒店(loc_main)：咸亨酒店，…。人物：@小伙计(char_005)：十五岁…`

名字取自剧本（char_id/loc_id → 名字），资产库里的编号就是权威。

用法：
    python scripts/migrate_asset_tags.py <project_id> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.domain import Script  # noqa: E402
from app.store import Store  # noqa: E402

# 旧标记：［资产:char_005］ / [asset:char_005]
OLD_TAG = re.compile(r"[［\[]\s*资产\s*:\s*([A-Za-z0-9_]+)\s*[］\]]")
NEW_TAG = re.compile(r"[［\[]\s*资产\s*:\s*([A-Za-z0-9_]+)\s*[］\]]")


def names_of(script: Script) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in script.characters:
        out[c.id] = c.name
    for l in script.locations:
        out[l.id] = l.name
    return out


def rewrite(text: str, names: dict[str, str]) -> tuple[str, int]:
    """把旧标记换成 @名字(编号)。返回 (新文本, 改了几处)。

    环境锚的旧位置在描述**末尾**（「<描述>［资产:loc_main］」），
    新写法要求标记在**前面**（「@咸亨酒店(loc_main)：<描述>」）——
    所以第一段要单独处理：把末尾的标记摘下来放到段首。
    """
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        aid = m.group(1)
        count += 1
        return f"@{names.get(aid, aid)}({aid})"

    if not text:
        return text, 0

    # 名字重复（新写法是「@名字(id)」，旧文本里名字还留在前面）：
    # 「小伙计@小伙计(char_005)」→「@小伙计(char_005)」
    def _dedup(m: re.Match[str]) -> str:
        return f"@{m.group(2)}({m.group(3)})"

    text = re.sub(
        r"([\u4e00-\u9fa5A-Za-z0-9_]+)@([\u4e00-\u9fa5A-Za-z0-9_]+)\((char_\w+)\)",
        _dedup,
        text,
    )

    # 第一段（环境）：标记在末尾 → 移到段首
    head, sep, rest = text.partition("。")
    m = re.search(r"[［\[]\s*资产\s*:\s*(loc_[A-Za-z0-9_]+)\s*[］\]]\s*$", head)
    if m:
        aid = m.group(1)
        desc = head[: m.start()].rstrip("，,、 ")
        head = f"@{names.get(aid, aid)}({aid})：{desc}"
        count += 1
        return head + sep + OLD_TAG.sub(repl, rest), count

    return OLD_TAG.sub(repl, text), count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = Store(get_settings().database_url)
    project = store.get_project(args.project_id)
    if project is None:
        print(f"作品不存在：{args.project_id}")
        return 1

    version = store.latest_version(project)
    if version is None:
        print("还没有剧本版本")
        return 1
    names = names_of(version.script)

    # 视频版本（提示词在这里）—— 所有版本都迁，链上任意一版都该是同一套写法
    vvers = store.list_video_versions(project.id)
    if not vvers:
        print("没有视频版本（提示词还没生成）")
        return 1

    total = 0
    for version in sorted(vvers, key=lambda v: v.created_at):
        shots = json.loads(version.shots_json or "[]")
        changed = 0
        for shot in shots:
            prompt = shot.get("video_prompt") or ""
            new_prompt, n = rewrite(prompt, names)
            total += n
            if n:
                changed += 1
                shot["video_prompt"] = new_prompt
        if changed and not args.dry_run:
            with store.session() as s:
                from app.store import VideoVersion

                row = s.get(VideoVersion, version.id)
                row.shots_json = json.dumps(shots, ensure_ascii=False)
                s.commit()
        if changed:
            print(f"  {version.id}（{version.label}）：{changed} 个镜头")
    if not total:
        print("没有需要迁移的旧标记")
        return 0
    print(f"共迁移 {total} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
