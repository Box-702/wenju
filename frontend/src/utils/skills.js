// =====================================================================
// skills.js —— 技能标记的解析与渲染
//
// 用户在输入框里「引用」一个技能（D-XX），正文里会带一段标记：
//     【技能：project-switch】
// 标记里用英文名（那是 load_skill 的参数，模型看得懂），界面上显示中文名。
//
// 渲染成什么：一个带前缀图标（sparkle）的高亮 chip ——
// 一眼看出「这条消息是技能触发的」，而不是普通的一句话。
// =====================================================================

import { esc } from './markdown'

/** 正文里的技能标记。名字用技能标识符（字母数字与 - _）。 */
const SKILL_MARK = /【技能：([A-Za-z0-9_-]+)】/g

/** 代码段（整块代码 / 行内代码）：里面的同名字面量不能被当成技能标记。 */
const CODE_SEG = /(<pre[\s\S]*?<\/pre>|<code>[\s\S]*?<\/code>)/gi

/** 技能图标：与 AppIcon 的 sparkle 同一个四芒星（统一图标，不新增素材）。 */
const SPARKLE_SVG =
  '<svg class="skill-ic" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
  '<path d="M8 2.4c.5 2.6 1.5 3.6 4.1 4.1-2.6.5-3.6 1.5-4.1 4.1' +
  '.5-2.6-1.5-3.6-4.1-4.1 2.6-.5 3.6-1.5 4.1-4.1z" fill="currentColor" stroke="none"/></svg>'

/** 技能清单 → {英文名: 中文名} 映射（缺中文名就用英文名兜底）。 */
export function skillTitleMap(skills) {
  const map = {}
  for (const s of skills || []) {
    if (s && s.name) map[s.name] = s.title || s.name
  }
  return map
}

/** 把正文里的技能标记换成带图标高亮的 chip。

    输入是已经转义过的 HTML（esc / mdToHtml 的输出），插入的是固定标记，
    所以是安全的；代码段整段跳过，避免代码里的同名字面量被误替换。 */
export function renderSkillChips(html, titleMap = {}) {
  if (!html || html.indexOf('【技能：') < 0) return html
  return html
    .split(CODE_SEG)
    .map((seg, i) => {
      if (i % 2) return seg // 奇数位是代码段，原样保留
      return seg.replace(SKILL_MARK, (_m, name) => {
        const title = titleMap[name] || name
        return (
          `<span class="skill-chip" data-skill="${esc(name)}">` +
          `${SPARKLE_SVG}<span class="skill-name">${esc(title)}</span></span>`
        )
      })
    })
    .join('')
}
