// =====================================================================
// nav.js —— 把视图状态和浏览器历史对上
//
// 问题：应用状态（首页 / 工作区 / 资产库 / 技能 / 产出 / 我的作品）只存在
// store 里，浏览器历史里什么都没有 → 按返回键直接离开应用。
//
// 做法：用 URL 的 hash 记「当前视图」，进/出页面时 pushState，
// 返回键触发 popstate → 按 hash 还原视图。没有引入路由库（保持零依赖）。
//
//   #/                       首页
//   #/works/<项目 id>          工作区
//   #/assets                 资产库
//   #/skills                 技能
//   #/output                 产出
//   #/tasks                  我的作品
// =====================================================================
import { store } from './stores/app'

const OVERLAYS = ['assets', 'skills', 'output', 'tasks']

/** 当前视图的标识：overlay 优先，其次工作区，最后首页。 */
export function currentView() {
  if (store.overlay && OVERLAYS.includes(store.overlay)) return store.overlay
  if (store.mode === 'workspace' && store.pid) return `works/${store.pid}`
  return 'home'
}

function viewFromHash() {
  const h = (location.hash || '').replace(/^#\/?/, '').trim()
  if (!h) return 'home'
  if (OVERLAYS.includes(h)) return h
  if (h.startsWith('works/')) return h
  return 'home'
}

let last = null

/** 把某个视图标识应用成界面状态。 */
async function applyView(view) {
  if (OVERLAYS.includes(view)) {
    store.overlay = view
    return
  }
  store.overlay = null
  if (view.startsWith('works/')) {
    const pid = view.slice('works/'.length)
    if (pid && pid !== store.pid) {
      const { selectProject } = await import('./stores/app')
      await selectProject(pid)
    } else if (store.mode !== 'workspace') {
      store.mode = 'workspace'
    }
    return
  }
  store.mode = 'home'
}

/** 初始化：接上返回键 / 前进键。 */
export function initNav() {
  last = currentView()
  // 首屏按 URL 恢复（比如刷新时停在资产库页）
  const v = viewFromHash()
  if (v !== 'home') applyView(v)

  window.addEventListener('popstate', (e) => {
    const view = (e.state && e.state.view) || viewFromHash()
    last = view
    applyView(view)
  })

  // 首条历史记录替换成当前视图，避免第一次按返回键直接离开应用
  history.replaceState({ view: last }, '', urlFor(last))
}

/** 状态变了就把新视图写进历史（只 push 真正不同的视图）。 */
export function pushNav() {
  const view = currentView()
  if (view === last) return
  last = view
  history.pushState({ view }, '', urlFor(view))
}

function urlFor(view) {
  const base = location.pathname + location.search
  if (view === 'home') return base
  return `${base}#/${view}`
}
