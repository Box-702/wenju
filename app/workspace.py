# =====================================================================
# workspace.py —— 工作目录（真实关联电脑文件夹的结构化工作流程）
#
# 通过在磁盘上维护一套有纪律的目录结构，让「问剧」真正落到本地文件：
#
#   <工作目录>/
#     <剧名/项目名>/                    # 一个剧本一个文件夹
#       01_原稿/       原著 / 原始文本    # 导入时写入
#       02_版本/       每次生成的剧本快照  # 生成 / 接受改编后写入
#       03_导出/       用户导出的 .txt/.md/.docx
#       04_知识库/     项目知识 / 备忘录
#     _README.txt     说明这个目录结构的读我
#
# Workspace 类负责：
#   - 解析 / 创建工作目录根（root）；
#   - 按剧名生成安全、稳定的项目文件夹名（不依赖 DB id，便于用户直接去磁盘找）；
#   - 写入原稿 / 版本 / 导出 / 知识条目，并返回落盘路径；
#   - 提供目录树的文本摘要（供 UI 展示）。
#
# 与数据库分工：数据库存业务数据（可查询、可版本化回滚）；磁盘文件夹是给
# 用户「看得见、摸得着」的真实产物。两者互为镜像，写入失败不影响主流程。
# =====================================================================

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

# 子目录约定（数字前缀保证磁盘上按流程顺序排列）。
# 05-07 是视频链路产物（2026-11-12 对齐「工作区按产物类型分层」的思路）：
# 分镜/提示词快照、定妆图与场景图、成片——清理/迁移/授权按类整夹操作。
SUBDIRS: tuple[tuple[str, str], ...] = (
    ("01_原稿", "原始文本 / 原著"),
    ("02_版本", "每次生成的剧本快照"),
    ("03_导出", "导出的 .txt / .md / .docx"),
    ("04_知识库", "项目知识与备忘录"),
    ("05_分镜", "分镜表与提示词快照（.md）"),
    ("06_素材", "定妆图 / 场景参考图"),
    ("07_成片", "合成成片与字幕"),
)


def sanitize_folder(name: str) -> str:
    """把剧名变成安全的文件夹名（去掉路径分隔符与非法字符，去首尾空白）。"""
    name = (name or "未命名剧本").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80] or "未命名剧本"


# 运行时可通过 POST /api/workspace 覆盖默认工作目录（内存级，进程存活期内有效）。
# FastAPI 同步路由跑在线程池里，全局状态读写都要过这把锁，避免切换目录的瞬间
# 被并发请求读到不一致的 (root, persist) 组合。
_runtime_root: str | None = None
_runtime_persist: bool | None = None
_runtime_lock = threading.Lock()


def configure_root(root: str | None, persist: bool | None = None) -> Workspace:
    """把运行时工作目录设为指定路径（并立即校验/创建）。

    persist=None 表示沿用默认（=配置值 True）；False 表示「仅应用内、不落盘文件」。
    """
    global _runtime_root, _runtime_persist
    with _runtime_lock:
        _runtime_root = root
        if persist is not None:
            _runtime_persist = persist
        return Workspace(root, persist=_runtime_persist)


def current_workspace(default_root: str | None = None, default_persist: bool = True) -> Workspace:
    """获取当前生效的工作目录：优先运行时设置，否则用配置默认值。"""
    with _runtime_lock:
        root = _runtime_root if _runtime_root is not None else default_root
        persist = _runtime_persist if _runtime_persist is not None else default_persist
    return Workspace(root, persist=persist)


def workspace_mode(persist: bool, root: str | None, default_root: str | None) -> str:
    """推导工作目录模式：in_app / default / custom。"""
    if not persist:
        return "in_app"
    return "custom" if (root and root != default_root) else "default"


# 同一时刻只允许一个原生目录对话框：并发请求会创建多个 Tk root（Windows 上易崩），
# 且每个对话框会占住一个线程池 worker 直到用户选完。
_pick_dir_lock = threading.Lock()


def pick_directory(initial: str | None = None) -> str | None:
    """打开系统原生文件夹选择对话框，返回选中的绝对路径；取消返回 None。

    tkinter 在 Windows 上走 Tk 的 ``tk_chooseDirectory``，弹的是原生 Windows
    「选择文件夹」对话框（与 Codex/Zed 这类桌面应用的目录选择一致）。
    无 GUI 会话时（headless）抛 RuntimeError，调用方应捕获并提示手动输入。
    """
    with _pick_dir_lock:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"无法打开文件夹选择对话框：{e}") from e
        try:
            root.withdraw()  # 隐藏主窗口，只弹出文件夹对话框
            try:
                root.attributes("-topmost", True)
            except Exception:  # noqa: BLE001  某些环境不支持 topmost
                pass
            picked = filedialog.askdirectory(
                initialdir=initial or None,
                mustexist=True,
                title="选择问剧工作目录",
            )
            return picked or None
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"无法打开文件夹选择对话框：{e}") from e
        finally:
            # 无论取消、选完还是异常，都要销毁 Tk root，否则线程池线程上残留 GUI 资源。
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass


class Workspace:
    """封装工作目录（一个可配置的磁盘根）。"""

    def __init__(self, root: str | os.PathLike[str] | None, *, persist: bool = True) -> None:
        self.root = Path(root).expanduser() if root else None
        self.persist = True if persist is None else bool(persist)

    @property
    def configured(self) -> bool:
        return bool(self.root) and bool(self.persist)

    def ensure_root(self) -> Path:
        """确保根目录存在并返回它。未配置时抛 ValueError。"""
        if not self.root:
            raise ValueError("工作目录未配置。")
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def project_dir(self, title: str) -> Path:
        """返回（并创建）对应剧目的项目文件夹。"""
        root = self.ensure_root()
        pdir = root / sanitize_folder(title)
        pdir.mkdir(parents=True, exist_ok=True)
        for sub, _ in SUBDIRS:
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        return pdir

    def _within_pdir(self, title: str, sub: str, filename: str) -> Path:
        pdir = self.project_dir(title)
        fname = sanitize_folder(filename.replace("/", "-").replace("\\", "-"))
        return pdir / sub / fname

    # ---- 写入各项产物 ----
    # 仅应用内模式（persist=False）时不落盘：所有 save 都返回 None。

    def _assert_persist(self) -> None:
        if not self.persist:
            raise RuntimeError("当前为「仅应用内」模式，不会写入磁盘。")

    def save_original(self, title: str, source_file: str, text: str) -> Path | None:
        """保存原著 / 原始文本到「01_原稿」。返回落盘路径；仅应用内模式返回 None。"""
        self._assert_persist()
        ext = Path(source_file or "").suffix or ".txt"
        fname = f"原著_{sanitize_folder(title)}{ext}"
        path = self._within_pdir(title, "01_原稿", fname)
        path.write_text(text, encoding="utf-8")
        self.write_readme()
        return path

    def save_version(self, title: str, label: str, text: str, *, ext: str = ".txt") -> Path | None:
        """保存一份剧本版本到「02_版本」。返回落盘路径；仅应用内模式返回 None。"""
        self._assert_persist()
        label = sanitize_folder(label or "剧本版本")
        fname = f"{label}{ext}"
        path = self._within_pdir(title, "02_版本", fname)
        path.write_text(text, encoding="utf-8")
        self.write_readme()
        return path

    def save_export(self, title: str, label: str, data: bytes, ext: str) -> Path | None:
        """保存导出的剧本文件到「03_导出」。返回落盘路径；仅应用内模式返回 None。"""
        self._assert_persist()
        label = sanitize_folder(label or "剧本导出")
        fname = f"{label}{ext}"
        path = self._within_pdir(title, "03_导出", fname)
        path.write_bytes(data)
        self.write_readme()
        return path

    def save_note(self, title: str, filename: str, text: str) -> Path | None:
        """保存知识条目 / 备忘录到「04_知识库」。返回落盘路径；仅应用内模式返回 None。"""
        self._assert_persist()
        fname = sanitize_folder(filename.replace("/", "-").replace("\\", "-")) or "笔记.md"
        path = self._within_pdir(title, "04_知识库", fname)
        path.write_text(text, encoding="utf-8")
        self.write_readme()
        return path

    def remove_project(self, title: str) -> bool:
        """删除某个剧目在磁盘上的整棵文件夹（原稿/版本/导出/知识库）。

        只删除项目自己那个目录，不动工作目录根或其它项目。项目不存在返回 False。
        用于项目删除后清理磁盘残留。
        """
        if not self.root:
            return False
        pdir = self.root / sanitize_folder(title)
        if not pdir.exists():
            return False
        import shutil

        shutil.rmtree(pdir, ignore_errors=True)
        return True

    def list_project_files(self, title: str) -> dict | None:
        """列出某个剧目文件夹里的文件（按子目录分组）。仅落盘模式且目录存在时返回。"""
        if not self.persist or not self.root:
            return None
        pdir = self.root / sanitize_folder(title)
        folders: list[dict] = []
        if pdir.is_dir():
            for code, label in SUBDIRS:
                sub = pdir / code
                files: list[dict] = []
                if sub.is_dir():
                    for f in sorted(sub.iterdir()):
                        if f.is_file() and not f.name.startswith("."):
                            st = f.stat()
                            files.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
                if files:
                    folders.append({"code": code, "label": label, "files": files})
        return {"root": str(pdir), "folders": folders}

    def resolve_file(self, title: str, relpath: str) -> Path | None:
        """把相对路径解析到项目文件夹内的真实文件（防目录穿越）；不存在返回 None。"""
        if not self.persist or not self.root:
            return None
        pdir = (self.root / sanitize_folder(title)).resolve()
        try:
            target = (pdir / relpath).resolve()
            target.relative_to(pdir)
        except (ValueError, OSError):
            return None
        return target if target.is_file() else None

    # ---- 结构说明 & 摘要 ----

    def write_readme(self) -> None:
        """在工作目录根写/更新一份说明，解释目录约定（结构化的入口）。"""
        try:
            root = self.ensure_root()
        except ValueError:
            return
        readme = root / "_README.txt"
        lines = ["问剧 · 工作目录说明", "=" * 40, ""]
        lines.append("每个剧本一个文件夹，内部按流程自动分格：")
        lines.append("")
        for code, label in SUBDIRS:
            lines.append(f"  {code}/  <-  {label}")
        lines.append("")
        lines.append("这些目录由问剧在导入 / 生成 / 导出时自动维护，")
        lines.append("你直接去磁盘里对应的文件夹就能找到你的剧本文件。")
        readme.write_text("\n".join(lines), encoding="utf-8")

    def tree_text(self, title: str) -> str:
        """返回某个剧目文件夹的文本摘要（供 UI 展示 / 日志）。"""
        try:
            pdir = self.project_dir(title)
        except ValueError:
            return "（工作目录未配置）"
        out = [str(pdir.relative_to(self.root)) if self.root else str(pdir), ""]
        for code, label in SUBDIRS:
            sub = pdir / code
            files = sorted([f.name for f in sub.glob("*") if f.is_file()])
            out.append(f"{code}/  {label}" + ("：" + "; ".join(files) if files else ""))
        return "\n".join(out)

    def info(self, default_root: str | None = None) -> dict[str, Any]:
        """返回工作目录的配置摘要（供 URL 返回给前端）。"""
        mode = workspace_mode(self.persist, str(self.root) if self.root else None, default_root)
        return {
            "root": str(self.root) if self.root else None,
            "persist": self.persist,
            "mode": mode,
            "configured": self.configured,
            "exists": bool(self.root and self.root.exists()),
            "note": "当前为「仅应用内」模式，不写磁盘文件（数据库照常存储）。"
            if not self.persist
            else ("工作目录未配置，剧本产物暂不落盘。" if not self.root else None),
        }


def open_in_file_manager(path: Path) -> None:
    """在系统文件管理器里打开一个已存在的目录（Windows 资源管理器 / macOS Finder / Linux 文件管理器）。

    走系统级命令而非 webbrowser：目标目录可能是 UNC 或带中文名，explorer.exe
    对这类路径的 `file://` URL 处理不可靠，命令行参数才是稳的。失败抛异常，
    由调用方转成提示。
    """
    import subprocess
    import sys

    target = str(path)
    if sys.platform == "win32":
        # explorer 打开文件夹；分隔符归一成 Windows 风格，否则它会把正斜杠当参数解析失败
        subprocess.Popen(["explorer", target.replace("/", "\\")], shell=False)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target], shell=False)
    else:
        subprocess.Popen(["xdg-open", target], shell=False)
