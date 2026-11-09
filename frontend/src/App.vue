<script setup>
// =====================================================================
// App.vue —— 根组件：创作工作台布局
//
//   ┌─────────── 顶栏（品牌 + 批次状态 + 面板开关）──────────────┐
//   │ 作品导航      │ 创作区（对话）      │ 上下文侧栏      │
//   └──────────────────────────────────────────────────────┘
// =====================================================================

import { ref, onMounted, onUnmounted, watch } from 'vue'
import HeaderBar from './components/HeaderBar.vue'
import HomePage from './components/HomePage.vue'
import AssetPanel from './components/AssetPanel.vue'
import ChatThread from './components/ChatThread.vue'
import ChatComposer from './components/ChatComposer.vue'
import ContextPanel from './components/ContextPanel.vue'
import ViewerPanel from './components/ViewerPanel.vue'
import PageShell from './components/PageShell.vue'
import SkillsPage from './components/SkillsPage.vue'
import NewProjectModal from './components/NewProjectModal.vue'
import SettingsModal from './components/settings/SettingsModal.vue'
import PatchDrawer from './components/PatchDrawer.vue'
import PromptEditor from './components/video/PromptEditor.vue'
import AssetPreview from './components/AssetPreview.vue'
import Toast from './components/Toast.vue'
import { loadTree, loadWorkspace, loadWorkspaceTree, restorePanelPrefs, toggleRightPanel, notify, store, pollVideoBatch, ensureBatchPolling, loadModelStatus, loadAssetIndex, loadAssetRefs, closePromptEditor, saveShotPrompt } from './stores/app'
import { importProject } from './api'

// ---- 侧边栏自动弹出（通用上下文侧边栏的核心行为） ----
// 出片时：用户此刻最想看的是刚生成的东西 —— 自动弹出并切到「镜头」。
// 改编产出时：切到「大纲」，让用户看到刚才改了什么。
function autoOpenSidebar(kind) {
  store.rightOpen = true
  if (kind) store.sideView = kind
}

// 监听：批次开始跑 / 跑完 → 弹到「镜头」
watch(
  () => store.videoBatch?.status,
  (st) => {
    if (st === 'running' || st === 'succeeded') autoOpenSidebar('shots')
  },
)

// 监听：**版本号变了** → 弹到「大纲」
// 这是「用户让你帮忙改编」的那一刻——他要看的正是改成了什么样。
//
// 注意不能挂在 viewerScript 上：进入任何一个已有剧本的项目都会重载它，
// 那样侧边栏就是「默认打开」。这里只在版本号**变化**时弹，
// 首次加载项目不弹（侧边栏默认是关的）。
let _seenVersionId = null
watch(
  () => (store.versions[0]?.id) || '',
  (id) => {
    if (!id) { _seenVersionId = null; return }
    if (_seenVersionId === null) { _seenVersionId = id; return }  // 首次加载：不弹
    if (id !== _seenVersionId) {
      _seenVersionId = id
      autoOpenSidebar('outline')
    }
  },
)

/** 顶栏按钮 = 右侧栏视图开关：点一下右栏展开并切到对应视图；
    再点同一个则收起（侧栏收起时按钮也回到无边框的样子）。 */
function switchSideView(view) {
  if (store.rightOpen && store.sideView === view) {
    store.rightOpen = false
    return
  }
  store.sideView = view
  store.rightOpen = true
}

/** 保存提示词审阅结果（编辑器在 App 层，产出页与右侧栏共用同一份）。 */
async function onPromptReview(payload) {
  await saveShotPrompt(payload, payload.decision, payload.prompt_review_note)
}

// ---- 拖拽导入 ----
const dragging = ref(false)
let dragCounter = 0
function onDragEnter(e) {
  e.preventDefault()
  dragCounter++
  if (e.dataTransfer?.types?.includes('Files')) dragging.value = true
}
function onDragLeave(e) {
  e.preventDefault()
  dragCounter--
  if (dragCounter <= 0) { dragging.value = false; dragCounter = 0 }
}
function onDragOver(e) { e.preventDefault() }
async function onDrop(e) {
  e.preventDefault()
  dragging.value = false
  dragCounter = 0
  const file = e.dataTransfer?.files?.[0]
  if (!file) return
  const ext = file.name.split('.').pop()?.toLowerCase()
  if (!['txt', 'md', 'markdown', 'docx', 'pdf'].includes(ext)) {
    notify('不支持的文件格式，请上传 .txt / .md / .docx / .pdf')
    return
  }
  try {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('title', file.name.replace(/\.[^.]+$/, ''))
    fd.append('adaptation_type', 'short_drama')
    const r = await importProject(fd)
    await loadTree()
    notify(`已导入《${r.title}》`, 'ok')
  } catch (err) {
    notify('导入失败：' + err.message)
  }
}

// ---- 可拉伸三栏 ----
const layoutEl = ref(null)
const MIN = { left: 200, right: 280, mid: 360 }
const SPLITS = 8
const readW = (key, def) => {
  const n = Number(localStorage.getItem('sw-layout:' + key))
  return Number.isFinite(n) && n >= 60 ? n : def
}
const leftW = ref(readW('left', 260))
const rightW = ref(readW('right', 360))

let drag = null
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi)
function clampAll() {
  if (!layoutEl.value) return
  const w = Math.max(0, layoutEl.value.getBoundingClientRect().width - SPLITS)
  const rightSpace = store.rightOpen ? MIN.right : 0
  const leftMax = Math.max(MIN.left, w - MIN.mid - rightSpace)
  leftW.value = clamp(leftW.value, MIN.left, leftMax)
  if (store.rightOpen) {
    const rightMax = Math.max(MIN.right, w - MIN.mid - leftW.value)
    rightW.value = clamp(rightW.value, MIN.right, rightMax)
  }
}

function startDrag(side, e) {
  if (!layoutEl.value || window.innerWidth < 760) return
  const rect = layoutEl.value.getBoundingClientRect()
  drag = { side, avail: rect.width - SPLITS, x: e.clientX, lw: leftW.value, rw: rightW.value }
  document.body.classList.add('layout-resizing')
  window.addEventListener('pointermove', onDrag)
  window.addEventListener('pointerup', endDrag)
  e.preventDefault()
}
function onDrag(e) {
  if (!drag) return
  if (e.buttons === 0) { endDrag(); return }
  const dx = e.clientX - drag.x
  if (drag.side === 'left') {
    leftW.value = clamp(
      drag.lw + dx,
      MIN.left,
      Math.max(MIN.left, drag.avail - MIN.mid - (store.rightOpen ? rightW.value : 0)),
    )
  } else {
    rightW.value = clamp(
      drag.rw - dx,
      MIN.right,
      Math.max(MIN.right, drag.avail - MIN.mid - leftW.value),
    )
  }
}
function endDrag() {
  if (!drag) return
  localStorage.setItem('sw-layout:left', String(Math.round(leftW.value)))
  localStorage.setItem('sw-layout:right', String(Math.round(rightW.value)))
  drag = null
  document.body.classList.remove('layout-resizing')
  window.removeEventListener('pointermove', onDrag)
  window.removeEventListener('pointerup', endDrag)
}

// Ctrl+I 切换上下文侧栏（大纲 / 镜头 / 资产）
function globalKey(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'i') {
    e.preventDefault()
    toggleRightPanel()
  }
}

// ---- 响应式：≤1023px 面板抽屉化 ----
// 桌面（≥1024px）：左/中/右三栏 + 拖拽分隔条（原行为）。
// 平板/移动（≤1023px）：左右面板变成覆盖式抽屉（fixed + 遮罩），
// 选中项目 / 对话后自动收起，任何视口不出现横向溢出。
const isNarrow = ref(window.innerWidth < 1024)
function onResize() {
  isNarrow.value = window.innerWidth < 1024
  clampAll()
}
// 窄屏下选中项目 / 对话 → 收起抽屉（内容已切到中间列，面板挡视线）
watch(() => [store.pid, store.convId], () => {
  // 进入作品 → 拉一份可 @ 引用的资产清单（角色 / 场景 / 成片）与引用索引（编号 → 定妆照）
  loadAssetIndex()
  loadAssetRefs()
  if (!isNarrow.value) return
  store.leftOpen = false
  store.rightOpen = false
})
// 窄屏下右栏视图切换（如场景聚焦）同理收起左栏
watch(() => store.view, () => {
  if (isNarrow.value && store.rightOpen) store.leftOpen = false
})

// ---- 左栏：作品 + 资产库（工作区化：对话收敛为每作品一个）----

watch(() => store.rightOpen, () => clampAll())

onMounted(async () => {
  restorePanelPrefs()
  // 窄屏首屏：抽屉默认收起（覆盖 restorePanelPrefs 的桌面默认值）
  if (isNarrow.value) {
    store.leftOpen = false
    store.rightOpen = false
  }
  clampAll()
  window.addEventListener('resize', onResize)
  window.addEventListener('keydown', globalKey)
  await loadWorkspace()
  await loadTree()
  await loadWorkspaceTree()
  // 启动时若已有进行中的批次，立刻接上全局轮询（刷新页面不断进度）
  await pollVideoBatch()
  ensureBatchPolling()
  await loadModelStatus()
})
onUnmounted(() => {
  endDrag()
  window.removeEventListener('resize', onResize)
  window.removeEventListener('keydown', globalKey)
})
</script>

<template>
  <!-- 首页模式：品牌 + 我的作品；点作品进入工作区 -->
  <HomePage v-if="store.mode === 'home'" />

  <!-- 工作区模式：三栏创作布局 -->
  <template v-else>
    <!-- 极光背景（弱化版）：内容区仍以对话为主，光斑只做底 -->
    <div class="aurora soft" aria-hidden="true">
      <span class="blob blob-blue"></span>
      <span class="blob blob-violet"></span>
      <span class="blob blob-red"></span>
      <span class="grain"></span>
    </div>
    <HeaderBar
      @switch-view="switchSideView"
      @open-tasks="store.overlay = 'tasks'"
    />
    <!-- 拖拽导入覆盖层 -->
    <Transition name="fade">
      <div v-if="dragging" class="drop-overlay" @dragleave="onDragLeave" @drop="onDrop">
        <div class="drop-zone">
          <div class="drop-icon">DROP</div>
          <div class="drop-text">松开以导入剧本文件</div>
          <div class="drop-hint">.txt / .md / .docx / .pdf</div>
        </div>
      </div>
    </Transition>
    <!-- 对话是主体，右侧是随上下文变化的通用侧边栏 -->
    <div class="stage-wrap">
      <main class="stage">
        <ChatThread />
        <ChatComposer @open-skills="store.overlay = 'skills'" />
      </main>
      <div class="ctx-wrap" :class="{ closed: !store.rightOpen }">
        <ContextPanel
          @open-assets="store.overlay = 'assets'"
          @open-skills="store.overlay = 'skills'"
          @open-output="store.overlay = 'output'"
        />
      </div>
    </div>
  </template>

  <!-- 页面：资产库 / 技能 / 产出 / 我的作品。
       放在工作区分支**外面**——这些是「点进去的一个新页面」，从首页的输入框
       也能进（首页的 Skill 胶囊、@ 引用都指向它们）；之前挂在 v-else 里，
       首页点「Skill」只改状态不渲染，看起来就是「点不了」。 -->
  <PageShell
    v-if="store.overlay === 'assets'"
    title="资产库"
    :subtitle="store.pid ? '角色定妆照 · 角色音色 · 场景图 · 成片' : '先选一部作品'"
    @close="store.overlay = null"
  >
    <AssetPanel section="assets" />
  </PageShell>

  <SkillsPage v-if="store.overlay === 'skills'" @close="store.overlay = null" />

  <PageShell
    v-if="store.overlay === 'output'"
    title="产出"
    subtitle="剧本 · 场景 · 视频"
    @close="store.overlay = null"
  >
    <ViewerPanel class="page-viewer" />
  </PageShell>

  <PageShell
    v-if="store.overlay === 'tasks'"
    title="我的作品"
    subtitle="每部作品一个对话"
    @close="store.overlay = null"
  >
    <AssetPanel section="tasks" />
  </PageShell>

  <NewProjectModal />
  <SettingsModal
    :open="store.showSettings"
    @close="store.showSettings = false; loadModelStatus()"
  />
    <!-- 资产大图预览：点提示词里的 @引用 打开 -->
  <AssetPreview />

  <!-- 提示词编辑器：提到 App 层，右侧栏与产出页都能打开同一份 -->
  <PromptEditor
    v-if="store.editingPrompt"
    :shot="store.editingPrompt"
    :history="store.promptHistory"
    :history-loading="store.promptHistoryLoading"
    @close="closePromptEditor"
    @review="onPromptReview"
  />
  <PatchDrawer />
  <Toast />
</template>

<style scoped>
/* 舞台 + 右侧上下文栏 */
.stage-wrap {
  display: flex;
  height: calc(100dvh - var(--header-h));
  min-width: 0;
  min-height: 0;
}

/* 右栏展开时与对话区**左右平分**（各一半）。
   收起时 width:0，对话列独占整屏并在视口正中。 */
.ctx-wrap {
  flex: none;
  width: 50%;
  min-height: 0;
  transition: width 200ms var(--ease), opacity 200ms var(--ease);
  overflow: hidden;
}
.ctx-wrap.closed {
  width: 0;
  opacity: 0;
  pointer-events: none;
}

/* 主舞台：对话流 + 输入区 */
/* flex:1 必给——.stage-wrap 是 flex 行，不给的话它按内容宽排，整块会挤到左边 */
.stage {
  display: flex;
  flex: 1;
  flex-direction: column;
  height: calc(100dvh - var(--header-h));
  min-width: 0;
  min-height: 0;
  position: relative;
}

/* 产出页里的面板撑满 */
.page-viewer {
  display: flex;
  flex-direction: column;
  min-height: 0;
  width: 100%;
}

.layout {
  display: flex;
  height: calc(100dvh - var(--header-h));
  min-width: 0;
  position: relative;
}
.pane { min-width: 0; flex: none; }
.pane-left {
  display: flex; flex-direction: column; overflow: hidden;
  background: rgba(255, 255, 255, 0.035);
  backdrop-filter: var(--glass);
  -webkit-backdrop-filter: var(--glass);
  border-right: 1px solid var(--line);
}
.left-body { flex: 1; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
.left-body > * { flex: 1; min-height: 0; }
.pane-mid { display: flex; flex-direction: column; min-height: 0; min-width: 0; flex: 1; }
.split { flex: none; width: 8px; cursor: col-resize; position: relative; }
.split::before {
  content: ''; position: absolute; top: 0; bottom: 0; left: 3px; width: 2px;
  border-radius: 2px;
  background: color-mix(in oklch, var(--line) 45%, transparent);
  transition: background var(--dur) var(--ease), width var(--dur) var(--ease);
}
.split:hover::before, body.layout-resizing .split::before {
  background: var(--gold); width: 2px; left: 3px;
}

/* 面板容器：始终渲染，用 width + opacity 过渡实现开合 */
.panel-wrap {
  display: flex; flex: none;
  width: calc(var(--pw) + 8px); /* 面板宽度 + 分隔条 */
  opacity: 1;
  overflow: hidden;
  transition: width 320ms var(--ease), opacity 250ms var(--ease);
}
.panel-wrap.closed {
  width: 0;
  opacity: 0;
}
/* 右侧面板：从右侧展开 */
.panel-right-wrap {
  transition: width 320ms var(--ease), opacity 250ms var(--ease);
}

/* 拖拽导入覆盖层 */
.drop-overlay {
  position: fixed; inset: 0; z-index: 9000;
  background: color-mix(in oklch, var(--bg) 85%, transparent);
  backdrop-filter: blur(8px);
  display: flex; align-items: center; justify-content: center;
}
.drop-zone {
  display: flex; flex-direction: column; align-items: center; gap: 12px;
  padding: 40px 60px; border-radius: var(--radius-md);
  border: 1px dashed var(--line-strong);
  background: var(--panel);
  box-shadow: var(--shadow-3);
}
.drop-icon {
  display: grid; place-items: center; width: 60px; height: 60px; border-radius: var(--radius-md);
  border: 1px solid var(--line-strong);
  color: var(--gold); font: 500 12px/1 var(--mono); letter-spacing: 0.12em;
  background: transparent;
}
.drop-text { font-size: 18px; font-weight: 600; color: var(--ink); }
.drop-hint { font-size: 13px; color: var(--muted); }
.fade-enter-active, .fade-leave-active { transition: opacity 200ms var(--ease); }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* ---------- 响应式：≤1023px 面板抽屉化 ---------- */
/* 左右面板统一为覆盖式抽屉（fixed + 遮罩），中间列保持全宽；
   开合由 store.leftOpen / rightOpen 驱动，选中内容后自动收起。 */
@media (max-width: 1023px) {
  .layout { height: calc(100dvh - var(--header-h)); }
  .split { display: none; }
  .panel-wrap,
  .panel-wrap.panel-right-wrap {
    position: fixed;
    top: var(--header-h); bottom: 0;
    z-index: 60;
    width: min(86vw, 400px) !important;
    background: var(--panel);
    box-shadow: var(--shadow-3);
    transition: transform 280ms var(--ease), visibility 280ms;
  }
  .panel-wrap.panel-left-wrap { left: 0; border-right: 1px solid var(--line); }
  .panel-wrap.panel-right-wrap { right: 0; border-left: 1px solid var(--line); }
  .panel-wrap .pane { width: 100% !important; height: 100%; }
  /* 抵消桌面的 width+opacity 塌缩：抽屉用 transform 出入场 */
  .panel-wrap.closed {
    width: min(86vw, 400px) !important;
    opacity: 1;
    visibility: hidden;
    pointer-events: none;
  }
  .panel-wrap.panel-left-wrap.closed { transform: translateX(-102%); }
  .panel-wrap.panel-right-wrap.closed { transform: translateX(102%); }
  .pane-mid { width: 100%; }
  .drawer-scrim {
    position: fixed;
    top: var(--header-h); left: 0; right: 0; bottom: 0;
    z-index: 55;
    background: oklch(0 0 0 / .45);
  }
}

@media (max-width: 760px) {
  .drop-zone { width: min(86vw, 360px); padding: 36px 22px; text-align: center; }
  .drop-text { font-size: 16px; }
}
</style>