// =====================================================================
// stores/app.js —— 全局状态 + 业务动作（单例 store）
//
// 用 Vue 的 reactive() 做轻量状态管理，不引入 Pinia，保持依赖极简。
// 组件只负责渲染与交互，所有数据请求/流转都收敛到本文件的导出函数。
// =====================================================================

import { reactive } from 'vue'
import { api, importProject, streamChat } from '../api'

// ---------------------------------------------------------------------
// 全局响应式状态
// ---------------------------------------------------------------------
export const store = reactive({
  // 当前选中的项目 / 对话
  pid: null,
  convId: null,

  // 项目树数据
  projects: [],        // 项目列表 [{ id, title, version_count }]
  convMap: {},         // pid -> 对话列表（懒加载缓存）
  scenesMap: {},       // pid -> 场景列表 [{id, title, characters, beats_count}]（懒加载）
  expanded: {},        // pid -> 是否展开

  // 全局对话（不绑定项目；在对话里创建项目后自动归档到该项目组下）
  globalConvs: [],     // [{ id, title, user_message_count, created_at }]
  // 工作区文件树
  workspaceTree: null, // { root, folders: [{name, type, children}] }

  // 对话与版本
  messages: [],        // 当前对话消息 [{ role, content, events, payloads, streaming }]
  assetIndex: [],      // 可被 @ 引用的资产清单 [{ kind, id, name, sub }]
  assetRefs: {},       // 资产引用索引 { char_xxx / loc_xxx → { name, image, kind } }
                       // 提示词里的 @引用 要显示成「小图 + 名字」，靠它
  assetPreview: null,  // 正在预览的资产（点提示词里的 @引用 打开）
  versions: [],        // 当前项目的版本列表

  // 左侧面板开关（默认展开，记忆在 localStorage）
  leftOpen: true,
  // 应用模式：home=品牌首页（作品列表） / workspace=创作工作区（三栏）
  mode: 'home',
  // 场景聚焦模式（点击左侧场景时设置，右栏显示单个场景详情）
  focusedSceneId: null,
  // 右侧查看面板开关（默认收起，记忆在 localStorage）
  rightOpen: false,

  // 右侧作品工作台：剧本（默认）/ 场景 / 视频
  view: 'video',   // 工作区化默认：右侧直接展示产出（提示词 + 视频）；剧本/场景一键切回
  viewerText: '',      // 最新版本剧本全文（纯文本，供复制）
  viewerScript: null,  // 最新版本结构化剧本（供剧本排版渲染）

  // 杂项标记
  streaming: false,            // 是否正在流式生成
  statusPhase: '',             // 顶部状态条：这一轮正在做什么
  statusActive: false,         // 是否正在工作（结束后条子淡出）
  statusElapsed: 0,            // 本轮已用时长（毫秒）
  streamingStage: '',          // 当前生成阶段（来自工具事件的用户可读文案，如「拆解镜头方案」）
  statusPhase: '',             // 顶部状态条的文案（思考 / 正在做什么 / 正在写）
  statusActive: false,         // 是否正在工作（结束后条子会淡出）
  statusElapsed: 0,            // 本轮已用时长（毫秒）
  focusSeq: 0,                 // 「聚焦输入框」信号：开始页主按钮等递增它，输入区 watch 后 focus
  checked: reactive(new Map()), // runId -> Set(已勾选 patch 下标)
  showNewProject: false,        // 新建作品弹窗开关
  showSettings: false,
  overlay: null,   // 全屏页面：assets | skills | output | tasks（null=关闭）
  sideView: 'assets',  // 侧栏视图：outline（大纲）| shots（镜头/提示词）| assets（资产）| skills（技能）
  assets: [],          // 当前项目的资产（角色/场景/素材/成片）——最终归宿
  providers: [],       // 可用模型列表（输入区「模型」胶囊下拉用）
  chatPref: null,       // 对话模型偏好 {provider_id, model_name} —— 下拉里标出当前选中
  modelInfo: null,                  // {available, provider, name, source} —— 顶栏徽标用
  hint: '',                     // 输入区下方提示文字
  workspace: null,              // 当前工作目录信息 { root, configured, exists }
  toast: null,                  // 非阻塞通知 { id, message, type }

  // 改编提议审阅抽屉（对话里只显示摘要，细节在抽屉里看）
  drawer: { open: false, payload: null },

  // 后台任务面板
  // ---- v3.0 视频制作 ----
  // 镜头数据
  shots: [],              // 当前项目的镜头列表
  videoVersions: [],      // 视频版本列表
  videoVersionId: null,   // 当前镜头列表所属的视频版本
  videoJobs: [],          // 视频生成任务列表
  overview: null,         // 作品「消耗/产出」概览（C7：场/镜/成片计数 + 出片预估费用）
  videoBatch: null,       // 批量生成批次状态（null/对象）
  videoMode: localStorage.getItem('sw_video_mode') || 'auto',   // 生成模式：auto | approval
  videoResolution: localStorage.getItem('sw_video_resolution') || '',  // 空=全局默认(768P)
  videoAspect: localStorage.getItem('sw_video_aspect') || '9:16',  // 画幅：短剧默认竖屏 9:16
  videoSubtitle: localStorage.getItem('sw_video_subtitle') !== 'off',  // 成片合成时烧台词字幕（默认开）
  videoFinalize: null,    // 成片合成任务状态（null/对象）
  // 视频播放器
  playingVideo: null,     // { url, title, meta }
  // Prompt 编辑
  editingPrompt: null,    // 正在编辑的 shot 对象
  promptHistory: [],      // 当前镜头沿视频版本父链的审阅历史
  promptHistoryLoading: false,

  // 开始页建议卡片 -> 输入框的填充通道（draftSeq 变化触发 ChatComposer 取稿）
  draft: '',
  draftSeq: 0,
  skills: [],            // 主 Agent 可用的技能清单（/api/skills）
})

// ---------------------------------------------------------------------
// 非阻塞通知（替代 alert()：不打断、自动消失、可点关闭）
// ---------------------------------------------------------------------
let _toastTimer = null
export function notify(message, type = 'error') {
  store.toast = { id: Date.now(), message, type }
  clearTimeout(_toastTimer)
  _toastTimer = setTimeout(() => { store.toast = null }, 3800)
}
export function dismissToast() {
  clearTimeout(_toastTimer)
  store.toast = null
}

// 输入区提示：瞬时通知，几秒后自动消退；聚焦输入框也会立即清掉（见 ChatComposer）。
// 不要直接给 store.hint 赋值——那会让「对话已切换」这类提示永久挂在输入框里。
let _hintTimer = null
export function setHint(message) {
  store.hint = message
  clearTimeout(_hintTimer)
  _hintTimer = setTimeout(() => { store.hint = '' }, 4000)
}
export function clearHint() {
  clearTimeout(_hintTimer)
  store.hint = ''
}

// ---------------------------------------------------------------------
// 导航时序守卫：快速切换项目/对话时，慢响应不得覆盖新选中的数据。
// 每次导航（选项目/选对话）取一个自增 token，await 返回后 token 已过期
// 就直接丢弃本次结果。
// ---------------------------------------------------------------------
let _navSeq = 0
function _isCurrent(token) { return token === _navSeq }

/** 删除项目及其所有对话、版本。 */
export async function deleteProject(pid) {
  try {
    await api(`/projects/${pid}`, 'DELETE')
    // 清除本地状态
    if (store.pid === pid) { store.pid = null; store.convId = null; store.messages = [] }
    delete store.convMap[pid]
    delete store.expanded[pid]
    await loadTree()
    notify('已删除项目', 'ok')
  } catch (e) { notify('删除失败：' + e.message) }
}

// ---------------------------------------------------------------------
// 右侧查看面板开关
// ---------------------------------------------------------------------
/** 开/关左侧导航面板。 */
export function toggleLeftPanel() {
  store.leftOpen = !store.leftOpen
  localStorage.setItem('sw-layout:leftOpen', store.leftOpen ? '1' : '0')
}

/** 聚焦到单个场景（右栏显示场景详情）。传 null 退出聚焦。
 *  必须带上场景所属项目：右栏的剧本文本（viewerScript）可能还没加载，
 *  或停留在另一个项目上——只设 focusedSceneId 会渲染出空白。 */
export async function focusScene(sceneId, projectId) {
  store.focusedSceneId = sceneId
  if (sceneId && !store.rightOpen) {
    store.rightOpen = true
    localStorage.setItem('sw-layout:rightOpen', '1')
  }
  if (sceneId) store.view = 'scene'
  if (!sceneId) return
  // 场景所属项目与当前右栏上下文不一致（或尚未加载）时，先切过去再取剧本。
  if (projectId && store.pid !== projectId) {
    store.pid = projectId
    store.convId = null // 旧对话不属于新项目，交给后端回落到该项目默认对话
    await loadViewer()
  } else if (!store.viewerScript) {
    await loadViewer()
  }
}
/** 开/右侧查看面板；偏好记忆在 localStorage（默认收起）。 */
export function toggleRightPanel() {
  store.rightOpen = !store.rightOpen
  localStorage.setItem('sw-layout:rightOpen', store.rightOpen ? '1' : '0')
}
/** 恢复上次的面板开关偏好（App 挂载时调用）。 */
export function restorePanelPrefs() {
  store.leftOpen = localStorage.getItem('sw-layout:leftOpen') !== '0'  // 默认开
  store.rightOpen = localStorage.getItem('sw-layout:rightOpen') === '1'  // 默认关
}

// ---------------------------------------------------------------------
// 项目树（项目 -> 对话）
// ---------------------------------------------------------------------
/** 拉取项目列表；当前项目保持展开并刷新其对话。 */
export async function loadTree() {
  try {
    store.projects = await api('/projects')
  } catch (e) {
    notify('加载项目列表失败：' + e.message)
    return
  }
  // 当前选中项目保证处于展开态并加载对话
  if (store.pid && store.expanded[store.pid] !== false) {
    store.expanded[store.pid] = true
    await loadConversations(store.pid)
  }
}

/** 展开时懒加载某项目下的对话列表。 */
export async function loadConversations(pid, token = null) {
  try {
    const conversations = await api(`/projects/${pid}/conversations`)
    if (token !== null && !_isCurrent(token)) return
    store.convMap[pid] = conversations
  } catch {
    if (token === null || _isCurrent(token)) store.convMap[pid] = []
  }
}

/** 展开时懒加载某项目的场景列表（从最新版本提取）。 */
export async function loadScenes(pid) {
  try {
    const versions = await api(`/projects/${pid}/versions`)
    if (versions.length) {
      const v = await api(`/versions/${versions[0].id}`)
      const scenes = (v.script?.scenes || []).map((s) => ({
        id: s.id,
        title: s.title || s.id,
        characters: s.characters || [],
        beats_count: (s.beats || []).length,
        purpose: s.purpose || '',
      }))
      store.scenesMap[pid] = scenes
    } else {
      store.scenesMap[pid] = []
    }
  } catch { store.scenesMap[pid] = [] }
}

/** 折叠/展开项目节点。 */
export async function toggleProject(pid) {
  store.expanded[pid] = !store.expanded[pid]
  if (store.expanded[pid]) {
    await loadConversations(pid)
    await loadScenes(pid)
  }
}

/** 场景拖拽排序：乐观更新本地，落库为新版本；失败回滚重拉。 */
export async function reorderScenes(fromId, toId) {
  const pid = store.pid
  const scenes = store.scenesMap[pid] || []
  const fromIdx = scenes.findIndex((s) => s.id === fromId)
  const toIdx = scenes.findIndex((s) => s.id === toId)
  if (fromIdx < 0 || toIdx < 0) return
  const reordered = [...scenes]
  const [moved] = reordered.splice(fromIdx, 1)
  reordered.splice(toIdx, 0, moved)
  store.scenesMap[pid] = reordered
  const versionId = store.versions[0]?.id
  if (!versionId) { notify('没有可排序的剧本版本'); return }
  try {
    await api(`/versions/${versionId}/scene_order`, 'POST', { scene_ids: reordered.map((s) => s.id) })
    await Promise.all([loadViewer(), loadScenes(pid)])
    notify('场景顺序已更新', 'ok')
  } catch (e) {
    notify('排序失败：' + e.message)
    await loadScenes(pid)
  }
}

/** 选中项目：重置对话选择，自动选第一个对话，刷新右侧面板。进入工作区模式。 */
export async function selectProject(pid) {
  const token = ++_navSeq
  store.mode = 'workspace'
  // 从「我的作品」这类整页进来时，把整页关掉——否则工作区被盖在下面
  store.overlay = null
  // 中止进行中的流式回复：流属于旧会话，继续写只会产生孤儿数据。
  if (_streamAbort) _streamAbort.abort()
  store.pid = pid
  store.convId = null
  store.expanded[pid] = true
  store.messages = [] // 先清空旧内容，避免等待期间显示上一个项目的消息
  await loadConversations(pid, token)
  if (!_isCurrent(token)) return
  const convs = store.convMap[pid] || []
  if (convs.length) await selectConversation(pid, convs[0].id, token)
  else {
    setHint('已选择作品，可以直接提改编需求')
    await loadViewer(token)
  }
}

/** 退出工作区回首页。中止进行中的流：流属于工作区上下文，回首页不再消费。 */
export function exitWorkspace() {
  if (_streamAbort) _streamAbort.abort()
  store.mode = 'home'
}

/** 选中对话：加载历史消息并刷新右侧面板。token 沿用发起导航时的值。 */
export async function selectConversation(pid, convId, token = ++_navSeq) {
  // 已经在这个对话里：不打断进行中的生成、不清屏。
  // 此前再点一次当前对话会 abort 流 + 清空消息，而生成中的内容尚未落库，
  // 拉回历史为空，看起来像「整个对话消失」。
  if (store.pid === pid && store.convId === convId && (store.streaming || store.messages.length)) {
    setHint('已在该对话中')
    return
  }
  _navSeq = token
  if (_streamAbort) _streamAbort.abort()
  store.pid = pid
  store.convId = convId
  setHint('对话已切换，各自独立上下文')
  store.messages = [] // 先清空旧对话内容，避免等待期间/失败时残留造成「串话」
  await loadHistory(token)
  if (!_isCurrent(token)) return
  await loadViewer(token)
}

/** 新建对话（自动命名，不弹窗）。若当前项目已有空白对话则直接选中，不重复创建。 */
export async function newConversation(pid) {
  // 检查是否有空白对话（无用户消息的对话）——利用列表接口已返回的 user_message_count，无需逐个请求。
  const convs = store.convMap[pid] || []
  const blank = convs.find((c) => (c.user_message_count ?? 0) === 0)
  if (blank) {
    setHint('当前已有空白对话，直接使用即可')
    await selectConversation(pid, blank.id)
    return
  }
  // 没有空白对话才真正创建
  const count = convs.length
  try {
    const c = await api(`/projects/${pid}/conversations`, 'POST', { title: `对话 ${count + 1}` })
    await loadConversations(pid) // 刷新左侧树的对话列表
    await selectConversation(pid, c.id)
  } catch (e) { notify('新建对话失败：' + e.message) }
}

/** 重命名对话（由树节点内联编辑提交）。pid 为该对话所属项目，刷新其对话列表。 */
export async function setConversationTitle(pid, convId, title) {
  const t = title.trim()
  if (!t) return
  try {
    await api(`/conversations/${convId}`, 'PATCH', { title: t })
    await loadConversations(pid || store.pid)
  } catch (e) { notify(e.message) }
}

/** 删除对话；若删的是当前对话则清空选择并回到项目空态。确认由调用方（内联两步）负责。
 *  pid 为该对话所属项目：从树上看可能不是当前选中的项目，刷新时不能混用 store.pid。 */
export async function deleteConversation(convId, pid = store.pid) {
  try {
    await api(`/conversations/${convId}`, 'DELETE')
    if (store.convId === convId) {
      store.convId = null
      store.messages = []
    }
    const target = pid || store.pid
    if (target) await loadConversations(target)
    else await loadGlobalConversations()
    notify('已删除对话', 'ok')
  } catch (e) { notify(e.message) }
}

// ---------------------------------------------------------------------
// 全局对话（不绑定项目）
// ---------------------------------------------------------------------

/** 加载全局对话列表。 */
export async function loadGlobalConversations() {
  try { store.globalConvs = await api('/conversations') } catch { store.globalConvs = [] }
}

/** 新建全局对话。 */
export async function newGlobalConversation() {
  const blank = store.globalConvs.find((c) => (c.user_message_count ?? 0) === 0)
  if (blank) {
    setHint('当前已有空白对话，直接使用即可')
    await selectGlobalConversation(blank.id)
    return
  }
  const count = store.globalConvs.length
  try {
    const c = await api('/conversations', 'POST', { title: `对话 ${count + 1}` })
    await loadGlobalConversations()
    await selectGlobalConversation(c.id)
  } catch (e) { notify('新建对话失败：' + e.message) }
}

/** 选中一个全局对话（不关联项目）。 */
export async function selectGlobalConversation(convId) {
  // 同 selectConversation：点击当前全局对话不做任何事，保护进行中的流。
  if (store.pid === null && store.convId === convId && (store.streaming || store.messages.length)) {
    setHint('已在该对话中')
    return
  }
  const token = ++_navSeq
  store.pid = null
  store.convId = convId
  store.focusedSceneId = null
  store.messages = []
  store.versions = []
  await loadHistory(token)
}

// ---------------------------------------------------------------------
// 工作区文件树
// ---------------------------------------------------------------------

/** 加载工作区目录结构。 */
export async function loadWorkspaceTree() {
  try {
    const ws = await api('/workspace')
    if (!ws.configured || !ws.root) {
      store.workspaceTree = null
      return
    }
    // 加载根目录下的项目文件夹
    const folders = []
    for (const p of store.projects) {
      try {
        const files = await api(`/projects/${p.id}/files`)
        if (files.folders) {
          folders.push({ name: p.title, pid: p.id, folders: files.folders })
        }
      } catch {}
    }
    store.workspaceTree = { root: ws.root, folders }
  } catch {
    store.workspaceTree = null
  }
}

// ---------------------------------------------------------------------
// 对话历史
// ---------------------------------------------------------------------
/** 拉取对话的历史消息；token 过期（用户已切走）时不写入，避免旧响应覆盖新会话。 */
export async function loadHistory(token = _navSeq) {
  let messages = []
  if (store.convId) {
    try { messages = await api(`/conversations/${store.convId}/messages`) } catch { messages = [] }
  }
  if (!_isCurrent(token)) return
  store.messages = messages.map((m) => ({
    role: m.role, content: m.content,
    events: m.events || [], payloads: m.payloads || [], images: m.images || [], streaming: false,
  }))
}

// ---------------------------------------------------------------------
// 右侧查看面板
// ---------------------------------------------------------------------
/** 加载最新版本全文 + 结构化剧本；token 过期时不写入。 */
export async function loadViewer(token = _navSeq) {
  if (!store.pid) { store.viewerText = ''; store.viewerScript = null; return }
  try {
    const versions = await api(`/projects/${store.pid}/versions`)
    if (!_isCurrent(token)) return
    store.versions = versions
    if (versions.length) {
      const t = await api(`/versions/${versions[0].id}/text`)
      if (!_isCurrent(token)) return
      store.viewerText = t.text
      try {
        const full = await api(`/versions/${versions[0].id}`)
        if (!_isCurrent(token)) return
        store.viewerScript = full.script || null
      } catch { store.viewerScript = null }
    } else {
      store.viewerText = ''
      store.viewerScript = null
    }
  } catch (e) {
    if (!_isCurrent(token)) return
    store.viewerScript = null
    notify('剧本内容加载失败：' + e.message)
  }
}

/** 切换查看面板 tab。 */
export function showView(name) {
  store.view = name
}

// ---------------------------------------------------------------------
// 导出剧本（.txt / .md / .docx）+ 工作目录
// ---------------------------------------------------------------------
const EXPORT_EXT = { txt: 'txt', md: 'md', docx: 'docx' }

/** 加载可用模型列表（输入区「模型」胶囊的下拉用）。 */
export async function loadProviders() {
  try {
    const list = await api('/providers?kind=llm')
    store.providers = list || []
  } catch {
    store.providers = []
  }
  return store.providers
}

/** 加载「对话模型」的偏好（当前用哪个供应商 / 哪个模型）。 */
export async function loadChatPref() {
  try {
    const prefs = await api('/models/preferences')
    store.chatPref = prefs.find((x) => x.task_type === 'chat' && x.provider_id) || null
  } catch {
    store.chatPref = null
  }
  return store.chatPref
}

/** 拉一份「资产引用索引」：编号 → { name, image, kind }。
    提示词里的 @林然(char_005) 要渲染成小图，靠它把编号对上定妆照。 */
export async function loadAssetRefs(pid = store.pid) {
  if (!pid) { store.assetRefs = {}; return }
  const map = {}
  try {
    const lib = await api(`/projects/${pid}/library`)
    for (const c of lib.characters || []) {
      if (c.char_id) map[c.char_id] = { name: c.name, image: c.image || '', kind: 'character' }
    }
    for (const l of lib.locations || []) {
      if (l.loc_id) map[l.loc_id] = { name: l.name, image: l.image || '', kind: 'location' }
    }
  } catch { /* 没有剧本时索引为空，不影响显示 */ }
  store.assetRefs = map
}

/** 加载项目资产（侧边栏「资产」视图 / 资产库页共用）。 */
export async function loadAssets(projectId) {
  const pid = projectId || store.pid
  if (!pid) { store.assets = []; return [] }
  try {
    const list = await api(`/projects/${pid}/assets`)
    store.assets = list || []
  } catch {
    store.assets = []
  }
  return store.assets
}

/** 加载技能清单（侧边栏「技能」段渲染用）。 */
export async function loadSkills() {
  try {
    const data = await api('/skills')
    store.skills = data.skills || []
  } catch {
    store.skills = []
  }
}

/** 点技能 = 把它的适用场景预填进输入框（用户再改改就能发）。 */
export function useSkill(skill) {
  if (!skill) return
  store.draft = skill.when_to_use
    ? `${skill.when_to_use}，请帮我推进。`
    : skill.description || ''
  store.draftSeq += 1
}

/** 给版本打里程碑标记（draft/candidate/final / null 清除）。用于定稿管理。 */
export async function setVersionMilestone(versionId, milestone) {
  return api(`/versions/${versionId}/milestone`, 'POST', { milestone })
}

/** 把剧本编辑器的字段级改动应用到版本上，生成一个新的「手动编辑」版本。 */
export async function applyEdits(versionId, ops) {
  return api(`/versions/${versionId}/apply`, 'POST', { ops })
}

/** 触发浏览器下载某版本文本；成功后刷新工作目录（文件也会落盘到 03_导出）。 */
export async function exportVersion(versionId, fmt) {
  fmt = EXPORT_EXT[fmt] || 'txt'
  const res = await fetch(`/api/versions/${versionId}/export?fmt=${fmt}`)
  if (!res.ok) {
    const j = await res.json().catch(() => ({}))
    throw new Error(j.detail || res.statusText)
  }
  const disp = res.headers.get('Content-Disposition') || ''
  let filename = `剧本.${fmt}`
  // 优先取 RFC 5987 的 filename*（保留中文名），退回 ASCII filename。
  const star = disp.match(/filename\*=UTF-8''([^;]+)/i)
  if (star) {
    try { filename = decodeURIComponent(star[1]) } catch { filename = star[1] }
  } else {
    const m = disp.match(/filename="?([^";]+)"?/)
    if (m) filename = m[1]
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
  await loadWorkspace() // 导出后工作目录里多了 03_导出 文件
}

/** 拉取当前工作目录配置。 */
export async function loadWorkspace() {
  try { store.workspace = await api('/workspace') } catch { store.workspace = null }
}

/** 设置工作目录根路径与落盘模式；成功后刷新配置与右侧面板。persist=false 表示仅应用内、不落盘。 */
export async function setWorkspace(root, persist = true) {
  const r = await api('/workspace', 'POST', { root: root || '', persist })
  store.workspace = r
  await loadViewer()
}

/** 弹出系统原生文件夹选择对话框（Windows）；选择即设为工作目录。返回选中的路径。
 *  用户取消或弹出失败时返回 null（失败会抛出 Error，由调用方提示手动输入）。 */
export async function selectWorkspaceDirectory() {
  const r = await api('/workspace/select')
  if (r.cancelled) return null
  store.workspace = r
  await loadViewer()
  return r.path
}

/** 把当前项目落盘到工作目录（原稿 + 最新版本），返回目录树文本。 */
export async function syncProjectToWorkspace() {
  if (!store.pid) throw new Error('请先选择项目')
  const r = await api(`/projects/${store.pid}/structure`, 'POST')
  await loadWorkspace()
  return r.structure || ''
}

// ---------------------------------------------------------------------
// 发送消息（SSE 流式）
// ---------------------------------------------------------------------
/** 进行中的流式请求控制器：切换项目/对话时中止，避免流写进已离开的会话。 */
let _streamAbort = null

/** 工具名 → 生成阶段（用户可读）。唯一来源：输入区的阶段状态条与
 *  消息流的工作步骤面板都从这里取文案，未登记的工具显示「正在处理」。 */
export const STAGE_LABELS = {
  generate_script: '撰写剧本初稿',
  run_adaptation: '按你的要求改编剧本',
  get_script_overview: '通读剧本结构',
  ask: '查阅剧本内容',
  breakdown_scenes: '拆解镜头方案',
  generate_style_guide: '统一全片视觉风格',
  generate_video_prompts: '撰写镜头提示词',
  generate_reference_assets: '生成定妆图与环境参考图',
  get_scene_detail: '细读场景',
  get_source_text: '查阅原著',
  search_source: '在原著中查找资料',
  web_search: '联网查找资料',
  get_author_style: '分析作者文风',
  list_versions: '整理版本历史',
  validate_tool: '检查剧本一致性',
  list_video_shots: '盘点镜头与出片进度',
  approve_video_prompts: '批准镜头提示词',
  submit_videos: '提交视频生成',
  get_video_jobs: '查询视频任务状态',
  rerun_video: '重新生成镜头',
  check_background_tasks: '查看后台任务进度',
  finalize_film: '合成完整成片',
  check_finalize: '查询成片进度',
  map_novel: '建立章节地图',
  propose_arcs: '挑选改编段落',
  create_project: '新建作品',
  get_status: '查看项目状态',
  start_production: '启动制作流水线',
  load_skill: '读取技能细则',
  remember: '记住这条偏好',
  recall: '回忆你的偏好',
}

/** 聚焦底部输入框（开始页主按钮等调用）。 */
export function focusComposer() {
  store.focusSeq++
}

/** 发送一条用户消息（可带图片 id 列表），流式接收回复并实时更新最后一条 assistant 消息。 */
/** 用生图模型给资产库补定妆照 / 场景图（后台跑，完成后刷新清单）。
    输入框「＋」里的「生成资产图」走这里。 */
export async function generateAssetImages() {
  if (!store.pid) { notify('先选一部作品', 'ok'); return null }
  try {
    const r = await api(`/projects/${store.pid}/reference-assets`, 'POST')
    notify('正在生成定妆照与场景图…', 'ok')
    await loadAssetIndex()
    return r
  } catch (e) {
    notify('生成失败：' + (e.detail || e.message))
    return null
  }
}

/** 拉一份「可 @ 引用的资产清单」：角色 / 场景 / 成片素材。
    输入框里打 @ 会用它做候选列表。 */
export async function loadAssetIndex(pid = store.pid) {
  if (!pid) { store.assetIndex = []; return }
  const out = []
  try {
    const lib = await api(`/projects/${pid}/library`)
    for (const c of lib.characters || []) {
      out.push({
        kind: 'character', id: c.char_id || '', name: c.name || '',
        sub: c.image ? '定妆照' : '缺定妆照',
      })
    }
    for (const l of lib.locations || []) {
      out.push({
        kind: 'location', id: l.loc_id || '', name: l.name || '',
        sub: l.image ? '场景图' : '缺场景图',
      })
    }
  } catch { /* 没有剧本时资产库是空的，不影响对话 */ }
  try {
    const assets = await api(`/projects/${pid}/assets`)
    for (const a of assets || []) {
      out.push({
        kind: 'asset', id: a.id || '', name: a.name || '未命名',
        sub: a.kind === 'video' ? '成片' : a.kind === 'audio' ? '音频' : '图片',
      })
    }
  } catch { /* 素材接口失败不影响角色/场景引用 */ }
  store.assetIndex = out
}

let _statusTimer = null

export async function sendMessage(text, imageIds = [], refs = []) {
  if (store.streaming || (!text && !imageIds.length)) return
  // 先渲染用户气泡 + 一个「打字中」的 assistant 气泡
  // 带图时用回放 url 预览（与历史消息渲染同一数据形状）。
  const images = imageIds.map((id) => ({ id, url: `/api/chat/images/${id}` }))
  store.messages.push({ role: 'user', content: text, images, events: [], payloads: [], streaming: false })
  const reply = reactive({ role: 'assistant', content: '', events: [], payloads: [], streaming: true })
  store.messages.push(reply)

  store.streaming = true
  store.streamingStage = ''
  store.statusActive = true
  store.statusPhase = '正在思考'
  store.statusElapsed = 0
  _statusTimer = setInterval(() => { store.statusElapsed += 100 }, 100)
  _streamAbort = new AbortController()
  try {
    await streamChat(
      {
        project_id: store.pid, conversation_id: store.convId, message: text,
        meta: null, images: imageIds,
        // 用户用 @ 引用了哪些资产（角色 / 场景 / 成片）
        refs: (refs || []).map((r) => ({ kind: r.kind || '', id: r.id || '', name: r.name || '' })),
      },
      (ev) => handleStreamEvent(ev, reply),
      _streamAbort.signal,
    )
  } catch (e) {
    // 主动切走会话导致的中止不算错误，保留已收到的部分内容即可。
    if (e && e.name !== 'AbortError') reply.content = `请求失败：${e.message}`
  } finally {
    _streamAbort = null
    if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null }
    reply.streaming = false
    store.streaming = false
    store.streamingStage = ''
    store.statusActive = false
    setTimeout(() => {
      if (!store.streaming) { store.statusPhase = ''; store.statusElapsed = 0 }
    }, 2600)
    await loadTree()
  }
}

/** 处理单帧 SSE 事件，更新回复消息对象。 */
function handleStreamEvent(ev, reply) {
  if (ev.event === 'tool_call' || ev.event === 'tool_result') {
    // 必须带上 type：SSE 的类型在 ev.event 上，而事件体本身没有 type。
    // 少了它，步骤面板按 type 分派时就匹配不到任何分支（历史回放的消息
    // 因为服务端存了 type 所以正常，只有正在流式的那条会显示「已完成 0 步」）。
    reply.events.push({ type: ev.event, ...ev.data })
    // 阶段状态：工具开始即更新文案，供输入区状态条实时展示。
    if (ev.event === 'tool_call') {
      store.streamingStage = STAGE_LABELS[ev.data.name] || '正在处理'
      store.statusPhase = `正在${store.streamingStage}`
    }
  } else if (ev.event === 'token') {
    reply.content += ev.data.delta
    if (!store.statusPhase || store.statusPhase === '正在思考') store.statusPhase = '正在写回复'
  } else if (ev.event === 'thinking') {
    // 推理模型的思考增量：实时展示，流结束后折叠；不进正文。
    reply.thinking = (reply.thinking || '') + ev.data.delta
    if (!store.statusPhase) store.statusPhase = '正在思考'
  } else if (ev.event === 'done') {
    reply.content = ev.data.reply || reply.content
    reply.payloads = ev.data.payloads || []
    // 消息头（模型 · 耗时）：服务端随 done 一起给，补进事件流的首位，
    // 与历史回放的消息保持一致。
    if (ev.data.turn_meta) {
      reply.events.unshift({ type: 'meta', ...ev.data.turn_meta })
    }
    afterAgentDone(ev.data)
  } else if (ev.event === 'error') {
    reply.content = `出错：${ev.data.message}`
  }
}

/** done 事件后的收尾：首轮对话时挂上项目/对话，并刷新右侧面板与后台任务。 */
async function afterAgentDone(data) {
  if (data.project_id && !store.pid) {
    store.pid = data.project_id
    store.convId = data.thread_id || store.convId
    store.expanded[store.pid] = true
    await loadConversations(store.pid)
    await loadTree()
  }
  if (store.pid) await loadViewer()
}

// ---------------------------------------------------------------------
// 审阅动作（接受 / 拒绝 / 重新生成 / 编辑 patch）
// ---------------------------------------------------------------------
/** 勾选/取消某个 patch 操作。 */
export function toggleCheck(runId, i) {
  const set = store.checked.get(runId) || new Set()
  set.has(i) ? set.delete(i) : set.add(i)
  store.checked.set(runId, set)
}

/** 打开/关闭审阅详情抽屉（payload 为 patch_review 消息载荷）。 */
export function openPatchDrawer(payload) {
  store.drawer.payload = payload
  store.drawer.open = true
}
export function closePatchDrawer() {
  store.drawer.open = false
}

/** 向后端恢复审阅流程（resume）；完成后关闭抽屉并刷新右侧面板。 */
export async function resumeReview(runId, action, opts = {}) {
  if (store.streaming) return
  // 在对话里补一条用户操作记录 + 打字中气泡
  const summary = action === 'accept'
    ? (opts.patch_indexes?.length ? `接受改编提议（勾选 ${opts.patch_indexes.length} 项）` : '接受全部改编提议')
    : action === 'reject' ? '拒绝这次改编提议'
    : action === 'regenerate' ? `重新生成（反馈：${opts.feedback || '换个思路'}）`
    : '编辑 patch 后接受'
  store.messages.push({ role: 'user', content: summary, events: [], payloads: [], streaming: false })
  const reply = reactive({ role: 'assistant', content: '', events: [], payloads: [], streaming: true })
  store.messages.push(reply)

  store.streaming = true
  const meta = { intent: 'resume', run_id: runId, action, patch_indexes: opts.patch_indexes ?? null, feedback: opts.feedback ?? null, patch: opts.patch ?? null }
  try {
    const r = await api('/chat', 'POST', { project_id: store.pid, conversation_id: store.convId, message: '', meta })
    reply.content = r.reply
    reply.events = r.events || []
    reply.payloads = r.payloads || []
    await loadViewer()
  } catch (e) {
    reply.content = '操作失败：' + e.message
  } finally {
    reply.streaming = false
    store.streaming = false
    store.drawer.open = false
  }
}

// ---------------------------------------------------------------------
// 新建作品（导入项目）
// ---------------------------------------------------------------------
/** 提交新建作品表单；成功后选中新作品（及其首个对话）。 */
export async function submitNewProject({ title, adapt, visual, file, raw }) {
  const fd = new FormData()
  fd.append('title', title)
  fd.append('adaptation_type', adapt)
  // 画风与改编类型正交：short_drama 可以是实拍，也可以是二次元（漫剧）。
  fd.append('visual_style', visual || 'realistic')
  if (file) fd.append('file', file)
  else fd.append('raw_text', raw)
  const r = await importProject(fd)
  store.showNewProject = false
  await loadTree()
  await selectProject(r.id)
  if (r.conversation_id) await selectConversation(r.id, r.conversation_id)
  for (const w of r.warnings || []) notify(w)
}

// =====================================================================
// v3.0 视频制作相关函数
// =====================================================================

/** 加载项目的镜头列表（从最新 video version 读取）。 */
export async function loadShots() {
  if (!store.pid) {
    store.shots = []
    store.videoVersions = []
    store.videoVersionId = null
    return
  }
  try {
    const versions = await api(`/projects/${store.pid}/video-versions`)
    store.videoVersions = versions
    if (versions.length > 0) {
      const latest = await api(`/video-versions/${versions[0].id}`)
      store.videoVersionId = latest.id
      store.shots = latest.shots || []
    } else {
      store.shots = []
      store.videoVersionId = null
    }
  } catch {
    store.shots = []
    store.videoVersionId = null
  }
}

/** 加载当前镜头的 Prompt 审阅历史与版本差异。 */
export async function loadPromptHistory(shotId) {
  if (!store.pid || !store.videoVersionId || !shotId) {
    store.promptHistory = []
    return
  }
  store.promptHistoryLoading = true
  try {
    const data = await api(
      `/projects/${store.pid}/video-versions/${store.videoVersionId}/shots/${encodeURIComponent(shotId)}/prompt-history`,
    )
    store.promptHistory = data.history || []
  } catch {
    store.promptHistory = []
  } finally {
    store.promptHistoryLoading = false
  }
}

/** 拉取当前生效的对话模型信息（顶栏徽标 + 设置弹窗展示）。 */
export async function loadModelStatus() {
  try {
    const d = await api('/status')
    store.modelInfo = d.model || null
  } catch { store.modelInfo = null }
}

/** 加载项目的视频生成任务列表。 */
export async function loadVideoJobs() {
  if (!store.pid) { store.videoJobs = []; return }
  try {
    store.videoJobs = await api(`/projects/${store.pid}/video/jobs`)
  } catch { store.videoJobs = [] }
}

/** 加载作品的「消耗/产出」概览（C7）。失败留空：小卡整块不显示，不骗数字。 */
export async function loadOverview() {
  if (!store.pid) { store.overview = null; return }
  try {
    store.overview = await api(`/projects/${store.pid}/overview`)
  } catch { store.overview = null }
}

/** 提交视频生成任务。 */
export async function submitVideoJob(shot) {
  if (!store.pid || !shot?.video_prompt) return
  if (shot.prompt_status !== 'approved') {
    notify('请先编辑并批准这个镜头的 Prompt', 'info')
    openPromptEditor(shot)
    return
  }
  // 视频生成固定走 MiniMax；模型名取设置里配置的，缺省 H3
  let provider = 'minimax'
  let model = 'MiniMax-H3'
  try {
    const providers = await api('/providers?kind=video')
    const minimax = providers.find(p => p.name === 'minimax' && p.configured && p.enabled)
      || providers.find(p => p.configured && p.enabled)
    if (minimax) {
      provider = minimax.name
      model = minimax.config?.model || model
    }
  } catch {}

  try {
    const job = await api(`/projects/${store.pid}/video/generate`, 'POST', {
      shot_id: shot.id,
      provider,
      model,
      prompt: shot.video_prompt,
      version_id: store.videoVersionId,
      params: { aspect_ratio: store.videoAspect || null },
    })
    notify(`视频任务已提交：${shot.subject || shot.id}`, 'ok')
    await loadVideoJobs()
    return job
  } catch (e) {
    notify(`提交失败：${e.message}`, 'error')
  }
}

/** 设置生成模式（持久化到本地，下次进入记住选择）。 */
export function setVideoMode(mode) {
  store.videoMode = mode
  localStorage.setItem('sw_video_mode', mode)
}

/** 设置分辨率覆盖（空串 = 跟随全局默认 768P）。 */
export function setVideoResolution(res) {
  store.videoResolution = res
  localStorage.setItem('sw_video_resolution', res)
}

/** 设置视频画幅（9:16 竖屏 / 16:9 横屏 / 1:1 等）。 */
export function setVideoAspect(ratio) {
  store.videoAspect = ratio
  localStorage.setItem('sw_video_aspect', ratio)
}

/** 设置成片字幕烧录开关（合成成片时生效，默认开）。 */
export function setVideoSubtitle(on) {
  store.videoSubtitle = !!on
  localStorage.setItem('sw_video_subtitle', on ? 'on' : 'off')
}

/** 启动批量生成（自动/审批模式由 store.videoMode 决定）。 */
export async function startVideoBatch() {
  if (!store.pid) return
  const approved = store.shots.filter((shot) => shot.video_prompt && shot.prompt_status === 'approved')
  if (!approved.length) {
    notify('请先逐镜批准视频 Prompt，再开始批量生成', 'info')
    return
  }
  try {
    store.videoBatch = await api(`/projects/${store.pid}/video/generate-batch`, 'POST', {
      mode: store.videoMode,
      resolution: store.videoResolution || null,
      aspect_ratio: store.videoAspect || null,
    })
    notify(`批量生成已启动（${store.videoMode === 'auto' ? '自动' : '审批'}模式）`, 'ok')
    await loadVideoJobs()
    ensureBatchPolling()
  } catch (e) {
    notify(`批量生成启动失败：${e.message}`, 'error')
  }
}

/** 查询最新批次进度。 */
export async function pollVideoBatch() {
  if (!store.pid) return
  try {
    const b = await api(`/projects/${store.pid}/video/batch`)
    store.videoBatch = b.id ? b : null
    if (b.id && (b.status === 'running' || b.status === 'awaiting_approval')) await loadVideoJobs()
  } catch { /* 忽略轮询错误 */ }
}

let batchPollTimer = null
/**
 * 全局批次轮询：只要批次在跑（running / awaiting_approval），不管用户
 * 停在哪个视图都持续跟进，并在状态跃迁时给出通知。幂等——重复调用只
 * 会保留一个定时器；批次进入终态（done/failed/aborted）后自动停止。
 */
export function ensureBatchPolling() {
  if (batchPollTimer) return
  batchPollTimer = setInterval(async () => {
    const b = store.videoBatch
    if (!store.pid || !b || (b.status !== 'running' && b.status !== 'awaiting_approval')) {
      clearInterval(batchPollTimer)
      batchPollTimer = null
      return
    }
    const before = b.status
    await pollVideoBatch()
    const after = store.videoBatch
    if (before === 'running' && after?.status === 'awaiting_approval') {
      notify('镜头成片完成，等待审批', 'info')
    }
    if (after?.status === 'done' && before !== 'done') {
      notify(`批次完成（${after.total} 镜）`, 'ok')
      await loadShots()
    }
  }, 3000)
}

/** 向批次下达命令：approve / reroll / abort。 */
export async function batchCommand(command) {
  const b = store.videoBatch
  if (!b?.id) return
  try {
    store.videoBatch = await api(`/video/batches/${b.id}/command`, 'POST', { command })
    const zh = { approve: '已放行下一镜', reroll: '正在重跑当前镜', abort: '批次已终止' }
    notify(zh[command] || '命令已执行', 'ok')
    await loadVideoJobs()
  } catch (e) {
    notify(`命令失败：${e.message}`, 'error')
  }
}

/** 发起成片合成（拼装 + 配音 + 字幕按当前开关，后台执行）。 */
export async function startFinalize() {
  if (!store.pid) return
  const done = (store.videoJobs || []).filter((j) => j.status === 'succeeded' && j.video_url).length
  if (!done) {
    notify('还没有已生成的镜头，先出片再合成成片', 'info')
    return
  }
  try {
    store.videoFinalize = await api(`/projects/${store.pid}/video/finalize`, 'POST', {
      subtitles: store.videoSubtitle,
      dubbing: true,
    })
    notify('成片合成已开始（后台执行）', 'ok')
    ensureFinalizePolling()
  } catch (e) {
    notify(`成片合成启动失败：${e.message}`, 'error')
  }
}

/** 查询成片合成进度；进程重启后内存任务丢失，从素材库恢复最近一次成片。 */
export async function pollFinalize() {
  if (!store.pid) return
  try {
    const rec = await api(`/projects/${store.pid}/video/finalize/latest`)
    if (rec.id) {
      store.videoFinalize = rec
      return
    }
    const assets = await api(`/projects/${store.pid}/assets`)
    const byNew = (a, b) => (b.created_at || '').localeCompare(a.created_at || '')
    const film = assets.filter((a) => a.source === 'finalize' && a.kind === 'video').sort(byNew)[0]
    if (!film) {
      store.videoFinalize = null
      return
    }
    const srt = assets.filter((a) => a.source === 'finalize' && a.kind === 'subtitle').sort(byNew)[0]
    store.videoFinalize = {
      id: `asset:${film.id}`, status: 'succeeded',
      messages: [], error: null,
      result: {
        video_url: film.url, video_asset_id: film.id,
        srt_url: srt?.url || null, srt_asset_id: srt?.id || null,
        duration_sec: null, dubbed: null, subtitled: null,
        shots_used: null, shots_total: null, warnings: [],
      },
    }
  } catch { /* 忽略轮询错误 */ }
}

let finalizePollTimer = null
/**
 * 成片合成轮询：running 时全局跟进（与批次轮询同模式），终态自动停止并通知。
 */
export function ensureFinalizePolling() {
  if (finalizePollTimer) return
  finalizePollTimer = setInterval(async () => {
    const rec = store.videoFinalize
    if (!store.pid || !rec || rec.status !== 'running') {
      clearInterval(finalizePollTimer)
      finalizePollTimer = null
      return
    }
    await pollFinalize()
    const after = store.videoFinalize
    if (after?.status === 'succeeded') {
      notify('成片已合成完成，可在作品工作台播放', 'ok')
    } else if (after?.status === 'failed') {
      notify(`成片合成失败：${after.error || '未知原因'}`, 'error')
    }
  }, 3000)
}

/** 打开视频播放器。 */
export function playVideo(shot) {
  if (shot?.video_url) {
    store.playingVideo = { url: shot.video_url, title: shot.subject || shot.id, meta: { provider: shot.video_job_id } }
  }
}

/** 关闭视频播放器。 */
export function closeVideo() {
  store.playingVideo = null
}

/** 打开 Prompt 编辑器。 */
export function openPromptEditor(shot) {
  store.editingPrompt = shot
  store.promptHistory = []
}

/** 关闭 Prompt 编辑器。 */
export function closePromptEditor() {
  store.editingPrompt = null
  store.promptHistory = []
}

/** 批量应用 Prompt 审阅决定，服务端会创建新的可回滚视频版本。 */
export async function bulkReviewPrompts(shotIds, decision = 'approve', note = '') {
  if (!store.pid || !store.videoVersionId || !shotIds?.length) return
  try {
    const result = await api(
      `/projects/${store.pid}/video-versions/${store.videoVersionId}/prompts/review`,
      'POST',
      { shot_ids: shotIds, decision, note },
    )
    await loadShots()
    notify(`已批量处理 ${result.reviewed_count} 个镜头`, 'ok')
    return result
  } catch (e) {
    notify(`批量审阅失败：${e.message}`, 'error')
    throw e
  }
}

/** 保存并审阅镜头 Prompt，服务端会创建新的可回滚视频版本。 */
export async function saveShotPrompt(shot, decision = 'save', note = '') {
  if (!store.pid || !shot || !store.videoVersionId) return
  try {
    await api(
      `/projects/${store.pid}/video-versions/${store.videoVersionId}/shots/${encodeURIComponent(shot.id)}/prompt`,
      'PUT',
      {
        prompt: shot.video_prompt,
        decision,
        note,
        // 用户从参考素材里挑的参考视频（素材 id，≤3）；无则传空数组清空
        reference_asset_ids: [...(shot.reference_asset_ids || [])],
      },
    )
    await loadShots()
    const labels = {
      save: 'Prompt 草稿已保存',
      approve: 'Prompt 已批准，可以出片',
      needs_revision: '已标记为需要修改',
    }
    notify(labels[decision] || 'Prompt 已保存', 'ok')
    if (store.editingPrompt?.id === shot.id) closePromptEditor()
  } catch (e) {
    notify(`Prompt 保存失败：${e.message}`, 'error')
  }
}

