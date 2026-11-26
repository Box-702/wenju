# =====================================================================
# export.py —— 剧本导出（把结构化 Script 渲染成标准剧本文件）
#
# 支持三种格式：
#   - txt  : 纯文本剧本（Screenplay Format，Courier/等宽阅读友好）
#   - md   : Markdown 剧本（带标题/粗体/引用，方便在线预览与编辑）
#   - docx : 真正的 Word 文档（OpenXML 由标准库 zipfile 直接生成，
#             不引入 python-docx 依赖，与 importer 读取 docx 的轻量策略一致）。
#
# 统一入口 export_script(script, fmt) -> (bytes, filename)。
# 渲染约定（对齐经典剧本排版）：
#   - 场景标题行：INT./EXT. 地点 - 时间（大写，左对齐）
#   - 动作行：左对齐，块级
#   - 角色名：居中大写
#   - 提示 (emotion)：居中对白下方，用斜体/圆括号
#   - 对白：左缩进
#   - 转场：右对齐（如 CUT TO:）
# =====================================================================

from __future__ import annotations

import re
import xml.sax.saxutils as saxutils
import zipfile
from io import BytesIO
from typing import Any

from ..domain import Script

# 一个「脚本行」的参考宽度（等宽字符），用于居中/右对齐。
_PAGE = 78

# 场景标题中的内景/外景判定关键字（中文优先，兼容英文）。
_INT_KEYS = ("内景", "室内", "屋内", "房间里", "INT", "INTERIOR")
_EXT_KEYS = ("外景", "室外", "户外", "屋外", "街头", "路上", "EXT", "EXTERIOR")


def _upper_zh(text: str) -> str:
    """把拉丁字母大写（中文不受影响），用于角色名/场景标题的全大写效果。"""
    return re.sub(r"[a-z]", lambda m: m.group(0).upper(), text)


def _scene_int_ext(scene: Any, loc_name: str) -> str:
    """推断场景是内景还是外景。

    优先级：场景标题 > 场景时间/notes > 地点描述 > 地点名；缺省 INT.。
    没有可靠的字段时返回 INT.（保守）。
    """
    haystack = " ".join(
        filter(
            None,
            [
                getattr(scene, "title", None) or "",
                getattr(scene, "entry_state", None) or "",
                getattr(scene, "exit_state", None) or "",
                loc_name,
            ],
        )
    )
    haystack_l = haystack.lower()
    if any(k.lower() in haystack_l for k in _EXT_KEYS):
        return "EXT."
    if any(k.lower() in haystack_l for k in _INT_KEYS):
        return "INT."
    return "INT."


def _screenplay_lines(script: Script, *, markdown: bool = False) -> list[str]:
    """把结构化剧本渲染成剧本文本行序列。`markdown=True` 时加轻量 md 标记。"""
    locs = {loc.id: loc.name for loc in script.locations}
    chars = {c.id: c.name for c in script.characters}

    lines: list[str] = []

    def blank() -> None:
        lines.append("")

    # ---- 标题页 ----
    if markdown:
        lines.append(f"# 《{script.title}》")
        lines.append("")
        if script.logline:
            lines.append(f"> {script.logline}")
            lines.append("")
    else:
        lines.append(_upper_zh(script.title).center(_PAGE).rstrip())
        blank()
        if script.logline:
            lines.append(script.logline)
            blank()

    # ---- 片头人物/地点清单（剧本开篇的「人物表」）----
    if script.characters:
        if markdown:
            lines.append("## 角色")
            lines.append("")
            for c in script.characters:
                role = _role_zh(c.role)
                lines.append(f"- **{c.name}**{('（' + role + '）') if role else ''}")
            lines.append("")
        else:
            blank()
            lines.append(_upper_zh("人物").center(_PAGE).rstrip())
            blank()
            for c in script.characters:
                role = _role_zh(c.role)
                lines.append(f"{c.name}{('（' + role + '）') if role else ''}")
            blank()

    for i, sc in enumerate(script.scenes, 1):
        loc = locs.get(sc.location_id, sc.location_id) or "场景"
        int_ext = _scene_int_ext(sc, loc)
        time_str = (sc.time or "").strip()

        # ---- 场景标题行 ----
        header = f"{int_ext} {_upper_zh(loc)}"
        if time_str:
            header += f" - {time_str}"
        if markdown:
            lines.append(f"## {header}")
            lines.append("")
        else:
            lines.append(f"  {header}")
            blank()

        # ---- 目的/冲突作为动作段落 ----
        for para in (sc.purpose, sc.conflict):
            if para and para.strip():
                lines.append(f"{para.strip()}")
                blank()

        # ---- 节拍流：动作 / 对白 / cue ----
        for b in sc.beats or []:
            btype = b.type
            if btype == "dialogue":
                speaker = chars.get(b.speaker, b.speaker or "") or ""
                speaker_u = _upper_zh(speaker)
                if markdown:
                    lines.append(f"**{speaker_u}**")
                else:
                    lines.append(speaker_u.center(_PAGE).rstrip())
                lines.append("")
                if b.emotion:
                    if markdown:
                        lines.append(f"*（{b.emotion}）*")
                    else:
                        lines.append(f"    （{b.emotion}）")
                line = str(b.line or "").strip()
                if line:
                    if markdown:
                        lines.append(line)
                        lines.append("")
                    else:
                        lines.append(f"        {line}")
                        blank()
            elif btype == "os":
                speaker = chars.get(b.speaker, b.speaker or "") or ""
                line = str(b.line or b.text or "").strip()
                if line:
                    if markdown:
                        lines.append(f"**{_upper_zh(speaker)}（内心OS）**")
                        lines.append("")
                        lines.append(line)
                        lines.append("")
                    else:
                        lines.append(f"{_upper_zh(speaker)}（内心OS）".center(_PAGE).rstrip())
                        lines.append("")
                        lines.append(f"        {line}")
                        blank()
            elif btype in ("flashback", "sound", "subtitle", "card"):
                label = {"flashback": "闪回", "sound": "音效",
                         "subtitle": "字幕", "card": "卡点"}[btype]
                body = str(b.text or "").strip()
                if body:
                    if markdown:
                        lines.append(f"**【{label}】** {body}")
                        lines.append("")
                    else:
                        lines.append(f"【{label}】 {body}")
                        blank()
            elif btype == "cue":
                cue = str(b.text or "").strip()
                if cue:
                    if markdown:
                        lines.append(f"*（{cue}）*")
                        lines.append("")
                    else:
                        lines.append(f"      （{cue}）")
                        blank()
            else:
                action = str(b.text or "").strip()
                if action:
                    lines.append(action)
                    blank()

        # ---- 场景之间加转场 ----
        if i < len(script.scenes):
            if markdown:
                lines.append("---")
                lines.append("")
            else:
                lines.append("                          CUT TO:")
                blank()

    return lines


def _role_zh(role: str | None) -> str:
    """把 RoleType 映射为中文角色定位（用于人物表）。"""
    if not role:
        return ""
    table = {
        "protagonist": "主角",
        "antagonist": "反派",
        "supporting": "配角",
        "mentor": "导师",
        "foil": "对照",
        "other": "其他",
    }
    return table.get(role, role)


# ---------- 各格式渲染 ----------


def script_to_screenplay(script: Script) -> str:
    """标准剧本纯文本（.txt）。"""
    return "\n".join(_screenplay_lines(script, markdown=False)).rstrip()


def script_to_markdown(script: Script) -> str:
    """Markdown 剧本（.md）。"""
    return "\n".join(_screenplay_lines(script, markdown=True)).rstrip()


# ---------- .docx（OpenXML）----------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _esc_xml(text: str) -> str:
    """XML 转义（同时保留中文原文）。"""
    return saxutils.escape(text)


def _w_para(text: str, *, bold: bool = False, italic: bool = False, indent: int = 0) -> str:
    """生成一个 w:p 段落（可选粗体/斜体/首行缩进）。"""
    run_props = ""
    if bold:
        run_props = '<w:rPr><w:b/></w:rPr>'
    if italic:
        run_props = '<w:rPr><w:i/></w:rPr>'
    ppr = ""
    if indent:
        ppr = f'<w:pPr><w:ind w:left="{int(indent * 240)}"/></w:pPr>'
    return (
        f'<w:p>{ppr}<w:r>{run_props}<w:t xml:space="preserve">'
        f"{_esc_xml(text)}</w:t></w:r></w:p>"
    )


def script_to_docx(script: Script) -> bytes:
    """把剧本渲染成真正的 .docx（Word 文档）。返回文件字节。"""
    body: list[str] = []

    def para(text: str, **kw) -> None:
        if text:
            body.append(_w_para(text, **kw))

    def para_center(text: str, **kw) -> None:
        if not text:
            return
        body.append(
            f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
            f'<w:r>{("<w:rPr><w:b/></w:rPr>" if kw.get("bold") else "")}'
            f'<w:t xml:space="preserve">{_esc_xml(text)}</w:t></w:r></w:p>'
        )

    locs = {loc.id: loc.name for loc in script.locations}
    chars = {c.id: c.name for c in script.characters}

    # 标题页
    para_center(_upper_zh(script.title), bold=True)
    body.append(_w_para(""))
    if script.logline:
        para_center(script.logline)
        body.append(_w_para(""))

    # 角色表
    if script.characters:
        para_center(_upper_zh("人物"), bold=True)
        body.append(_w_para(""))
        for c in script.characters:
            role = _role_zh(c.role)
            para_center(f"{c.name}{('（' + role + '）') if role else ''}")
        body.append(_w_para(""))

    for i, sc in enumerate(script.scenes, 1):
        loc = locs.get(sc.location_id, sc.location_id) or "场景"
        int_ext = _scene_int_ext(sc, loc)
        header = f"{int_ext} {_upper_zh(loc)}"
        if (sc.time or "").strip():
            header += f" - {sc.time.strip()}"
        para(header, bold=True)
        body.append(_w_para(""))

        for txt in (sc.purpose, sc.conflict):
            if txt and txt.strip():
                para(txt.strip())
                body.append(_w_para(""))

        for b in sc.beats or []:
            if b.type == "dialogue":
                speaker = _upper_zh(chars.get(b.speaker, b.speaker or "") or "")
                para_center(speaker, bold=True)
                if b.emotion:
                    body.append(_w_para(f"（{b.emotion}）", italic=True))
                line = str(b.line or "").strip()
                if line:
                    body.append(_w_para(line, indent=2))
                body.append(_w_para(""))
            elif b.type == "cue":
                cue = str(b.text or "").strip()
                if cue:
                    body.append(_w_para(f"（{cue}）", italic=True))
            else:
                action = str(b.text or "").strip()
                if action:
                    para(action)
                    body.append(_w_para(""))

        if i < len(script.scenes):
            body.append(_w_para("CUT TO:"))
            body.append(_w_para(""))

    document = _docx_document(body)
    return _make_docx_zip(document)


def _docx_document(body: list[str]) -> str:
    """拼出 <w:document>（含样式以设默认字体/字号/行距）。"""
    content = "".join(body)
    # 默认字体设为宋体/等宽感觉的正文，行距 1.5，字体 22 half-point (=11pt Courier 感)。
    styles = (
        '<w:styles>'
        '<w:docDefaults>'
        '<w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:eastAsia="SimSun"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/>'
        '</w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        '</w:docDefaults>'
        '</w:styles>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{styles}{content}"
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        '</w:sectPr></w:body></w:document>'
    )


def _docx_relationships() -> str:
    """document.xml.rels：声明可选的 styles 关系（若使用）。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )


def _docx_styles() -> str:
    """最小 styles.xml，保证 Word 能正常打开并应用默认字体。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:docDefaults/></w:styles>"
    )


def _content_types() -> str:
    """[Content_Types].xml。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )


def _make_docx_zip(document_xml: str) -> bytes:
    """把各部分打包成 .docx（zip）字节。"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types())
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", _docx_relationships())
        zf.writestr("word/styles.xml", _docx_styles())
    return buf.getvalue()


# ---------- HTML 打印版（PDF 用浏览器打印）----------


def script_to_printable_html(script: Script) -> str:
    """生成适合浏览器打印的 HTML 页面（用于 PDF 导出）。"""
    locs = {loc.id: loc.name for loc in script.locations}
    chars = {c.id: c.name for c in script.characters}

    lines: list[str] = []
    lines.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    lines.append(f"<title>{_esc_xml(script.title)}</title>")
    lines.append("""<style>
      @page { size: A4; margin: 2.5cm; }
      body { font-family: 'Courier New', 'SimSun', monospace; font-size: 11pt; line-height: 1.6; color: #1a1a1a; }
      h1 { text-align: center; font-size: 18pt; margin-bottom: 0.5em; }
      .logline { text-align: center; font-style: italic; margin-bottom: 2em; color: #555; }
      .characters { text-align: center; margin-bottom: 2em; }
      .characters h2 { font-size: 12pt; margin-bottom: 0.5em; }
      .characters p { margin: 2px 0; }
      .scene-header { font-weight: bold; margin-top: 1.5em; margin-bottom: 0.5em; text-transform: uppercase; }
      .purpose, .conflict { margin: 4px 0; color: #444; }
      .action { margin: 8px 0; }
      .speaker { text-align: center; font-weight: bold; text-transform: uppercase; margin-top: 12px; margin-bottom: 2px; }
      .emotion { text-align: center; font-style: italic; color: #666; margin-bottom: 2px; }
      .dialogue { margin-left: 2.5em; margin-bottom: 8px; }
      .cue { text-align: center; font-style: italic; color: #666; margin: 8px 0; }
      .transition { text-align: right; margin: 12px 0; font-weight: bold; }
      .page-break { page-break-after: always; }
    </style></head><body>""")

    # 标题页
    lines.append(f"<h1>{_esc_xml(_upper_zh(script.title))}</h1>")
    if script.logline:
        lines.append(f"<p class='logline'>{_esc_xml(script.logline)}</p>")

    # 角色表
    if script.characters:
        lines.append("<div class='characters'><h2>人物</h2>")
        for c in script.characters:
            role = _role_zh(c.role)
            lines.append(f"<p>{_esc_xml(c.name)}{'（' + _esc_xml(role) + '）' if role else ''}</p>")
        lines.append("</div>")

    lines.append("<div class='page-break'></div>")

    # 场景
    for i, sc in enumerate(script.scenes, 1):
        loc = locs.get(sc.location_id, sc.location_id) or "场景"
        int_ext = _scene_int_ext(sc, loc)
        header = f"{int_ext} {_upper_zh(loc)}"
        if (sc.time or "").strip():
            header += f" - {sc.time.strip()}"
        lines.append(f"<div class='scene-header'>{_esc_xml(header)}</div>")

        for txt in (sc.purpose, sc.conflict):
            if txt and txt.strip():
                lines.append(f"<p class='purpose'>{_esc_xml(txt.strip())}</p>")

        for b in sc.beats or []:
            if b.type == "dialogue":
                speaker = _upper_zh(chars.get(b.speaker, b.speaker or "") or "")
                lines.append(f"<div class='speaker'>{_esc_xml(speaker)}</div>")
                if b.emotion:
                    lines.append(f"<div class='emotion'>（{_esc_xml(b.emotion)}）</div>")
                line = str(b.line or "").strip()
                if line:
                    lines.append(f"<div class='dialogue'>{_esc_xml(line)}</div>")
            elif b.type == "cue":
                cue = str(b.text or "").strip()
                if cue:
                    lines.append(f"<div class='cue'>（{_esc_xml(cue)}）</div>")
            else:
                action = str(b.text or "").strip()
                if action:
                    lines.append(f"<p class='action'>{_esc_xml(action)}</p>")

        if i < len(script.scenes):
            lines.append("<div class='transition'>CUT TO:</div>")

    lines.append("</body></html>")
    return "\n".join(lines)


# ---------- 统一入口 ----------

# 导出格式 -> (扩展名, MIME)
EXPORT_FORMATS: dict[str, dict[str, str]] = {
    "txt": {"ext": ".txt", "mime": "text/plain; charset=utf-8"},
    "md": {"ext": ".md", "mime": "text/markdown; charset=utf-8"},
    "docx": {"ext": ".docx", "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "pdf": {"ext": ".html", "mime": "text/html; charset=utf-8"},
}


def export_script(script: Script, fmt: str) -> tuple[bytes, str]:
    """按格式导出剧本, 返回 (字节, 建议文件名无扩展名)."""
    fmt = (fmt or "txt").lower()
    if fmt == "txt":
        return script_to_screenplay(script).encode("utf-8"), EXPORT_FORMATS["txt"]["ext"]
    if fmt == "md":
        return script_to_markdown(script).encode("utf-8"), EXPORT_FORMATS["md"]["ext"]
    if fmt == "docx":
        return script_to_docx(script), EXPORT_FORMATS["docx"]["ext"]
    if fmt == "pdf":
        return script_to_printable_html(script).encode("utf-8"), EXPORT_FORMATS["pdf"]["ext"]
    raise ValueError(f"不支持的导出格式：{fmt}，可选：{'/'.join(EXPORT_FORMATS)}")
