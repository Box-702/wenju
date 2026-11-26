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
import PageShell from './components/PageShell.vue'
import SkillsPage from './components/SkillsPage.vue'
import NewProjectModal from './components/NewProjectModal.vue'
import SettingsModal from './components/settings/SettingsModal.vue'
import PatchDrawer from './components/PatchDrawer.vue'
import VideoPlayer from './components/video/VideoPlayer.vue'
import PromptEditor from './components/video/PromptEditor.vue'
import AssetPreview from './components/AssetPreview.vue'
import Toast from './components/Toast.vue'
import IconRail from './components/workbench/IconRail.vue'
import ScriptHistoryPanel from './components/workbench/ScriptHistoryPanel.vue'
import WorkbenchView from './components/workbench/WorkbenchView.vue'
import AgentPanel from './components/workbench/AgentPanel.vue'
import { store, loadTree, loadWorkspace, loadProjectFiles, restorePanelPrefs, toggleAgentPanel, setCtxOpen, notify, pollVideoBatch, ensureBatchPolling, loadModelStatus, loadAssetIndex, loadAssetRefs, closePromptEditor, saveShotPrompt, loadStoryPlan, closeVideo } from './stores/app'
import { initNav, pushNav } from './nav'
import { importProject } from './api'

// ---- 事件驱动的视图引导（即梦式布局改版） ----
// 第二列现在只在剧本页出现（修改历史），资产/分镜页全宽；
// 「出片/改编发生时把用户带到该看的东西」改成了「把主区切到对应工作台视图」。
// 规矩：**只在用户还没主动选视图（wbView=null）时引导**——
// 用户已经停在剧本页时，批次跑完不能把他劫持到分镜页。
function guideView(view) {
  if (store.mode !== 'workspace') return
  if (store.wbView) return
  store.wbView = view
}

// 监听：批次开始跑 / 跑完 → 引导到「分镜」（刚生成的东西都在那看）
watch(
  () => store.videoBatch?.status,
  (st) => {
    if (st === 'running' || st === 'succeeded') guideView('clips')
  },
)

// 监听：**版本号变了** → 引导到「剧本」（用户让你改编的那一刻，
// 他要看的正是改成了什么样）。首次加载项目不引导。
let _seenVersionId = null
watch(
  () => (store.versions[0]?.id) || '',
  (id) => {
    if (!id) { _seenVersionId = null; return }
    if (_seenVersionId === null) { _seenVersionId = id; return }  // 首次加载：不引导
    if (id !== _seenVersionId) {
      _seenVersionId = id
      guideView('script')
    }
  },
)

/** 保存提示词审阅结果（编辑器在 App 层，右侧栏与分镜页共用同一份）。 */
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

// ---- 响应式：≤1023px 面板抽屉化 ----
// 桌面（≥1024px）：左/中/右三栏；上下文侧栏常驻（不再支持关闭），
// 拖右缘调宽（220–480px，见下方拖拽逻辑）。
// 平板/移动（≤1023px）：左右面板变成覆盖式抽屉（fixed + 遮罩），
// 选中项目 / 对话后自动收起，任何视口不出现横向溢出。
const isNarrow = ref(window.innerWidth < 1024)
function onResize() {
  isNarrow.value = window.innerWidth < 1024
}
// 窄屏下选中项目 / 对话 → 收起抽屉（内容已切到中间列，面板挡视线）
// 视图变化（首页 / 工作区 / 工作台视图 / 各个页面）写进浏览器历史，返回键就能退回去
watch(() => [store.mode, store.pid, store.overlay, store.wbView], () => {
  pushNav()
})

watch(() => [store.pid, store.convId], () => {
  // 进入作品 → 拉一份可 @ 引用的资产清单（角色 / 场景 / 成片）与引用索引（编号 → 定妆照）
  loadAssetIndex()
  loadAssetRefs()
  // 工作目录里的真实文件树（右侧「工作目录」区渲染它）
  loadProjectFiles()
  if (!isNarrow.value) return
  store.rightOpen = false
  store.agentOpen = false
})

// ---- 上下文侧栏宽度：拖右缘调宽；可收起；记忆开合与宽度 ----
// 220px 起（再窄内容挤爆），480px 封顶（再宽主区就没内容了）。
// 默认 260px；记忆在 localStorage；窄屏抽屉走媒体查询的 !important 宽度，不吃这份内联样式。
const CTX_MIN = 220
const CTX_MAX = 480
const ctxW = ref(Math.min(CTX_MAX, Math.max(CTX_MIN, Number(localStorage.getItem('sw-ctx-w2')) || 260)))
const ctxDragging = ref(false)
let ctxDrag = null
function onCtxHandleDown(e) {
  if (e.button !== 0) return
  e.preventDefault()
  ctxDrag = { x: e.clientX, w: ctxW.value }
  ctxDragging.value = true
  window.addEventListener('pointermove', onCtxHandleMove)
  window.addEventListener('pointerup', onCtxHandleUp)
}
function onCtxHandleMove(e) {
  if (!ctxDrag) return
  ctxW.value = Math.min(CTX_MAX, Math.max(CTX_MIN, ctxDrag.w + (e.clientX - ctxDrag.x)))
}
function onCtxHandleUp() {
  ctxDrag = null
  ctxDragging.value = false
  window.removeEventListener('pointermove', onCtxHandleMove)
  window.removeEventListener('pointerup', onCtxHandleUp)
  localStorage.setItem('sw-ctx-w2', String(ctxW.value))
}

// ---- 左栏：作品 + 资产库（工作区化：对话收敛为每作品一个）----


onMounted(async () => {
  initNav()
  restorePanelPrefs()
  // 窄屏首屏：两个抽屉默认收起（覆盖 restorePanelPrefs 的桌面默认值）
  if (isNarrow.value) {
    store.rightOpen = false
    store.agentOpen = false
  }
  window.addEventListener('resize', onResize)
  await loadWorkspace()
  await loadTree()
  await loadProjectFiles()
  // 启动时若已有进行中的批次，立刻接上全局轮询（刷新页面不断进度）
  await pollVideoBatch()
  ensureBatchPolling()
  await loadModelStatus()
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  window.removeEventListener('pointermove', onCtxHandleMove)
  window.removeEventListener('pointerup', onCtxHandleUp)
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
    <HeaderBar />
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
    <!-- 即梦式四区（D-73 布局修正）：
         最左细图标栏 ｜ 上下文栏（仅剧本页：修改历史）｜ 主内容区 ｜ 右侧常驻 Agent 对话面板。
         资产库/分镜页不再常驻资产树——即梦参考里没有这条固定左栏，且资产页自己有
         类型 tab 与大卡网格，常驻树只会重复占宽（2026-11-26 用户截图反馈）。 -->
    <div class="stage-wrap">
      <IconRail />
      <!-- 上下文侧栏：只在剧本页放修改历史。桌面拖右缘调宽（220–480px，
           记忆在 localStorage）；窄屏仍走抽屉媒体查询。 -->
      <div
        v-if="store.wbView === 'script' && store.ctxOpen"
        class="ctx-wrap" :class="{ closed: isNarrow && !store.rightOpen, resizing: ctxDragging }"
        :style="isNarrow ? null : { width: ctxW + 'px' }"
      >
        <ScriptHistoryPanel @collapse="setCtxOpen(false)" />
      </div>
      <div
        v-if="!isNarrow && store.wbView === 'script' && store.ctxOpen"
        class="ctx-handle" :class="{ on: ctxDragging }"
        title="拖动调宽（220–480px）"
        @pointerdown="onCtxHandleDown"
      />
      <main class="stage">
        <WorkbenchView />
      </main>
      <div class="agent-wrap" :class="{ closed: !store.agentOpen }">
        <AgentPanel />
      </div>
    </div>
    <button
      v-if="!store.agentOpen"
      class="agent-reopen ghost"
      title="打开 Agent 对话面板"
      @click="toggleAgentPanel()"
    >
      对话
    </button>
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

  <!-- 全局视频播放器：任何视图设 store.playingVideo 都能弹（即梦式行内/浮层预览）。 -->
  <div v-if="store.playingVideo" class="modal-backdrop" @click.self="closeVideo()">
    <div class="modal-card wide">
      <VideoPlayer
        :url="store.playingVideo.url"
        :title="store.playingVideo.title"
        :meta="store.playingVideo.meta"
        @close="closeVideo()"
      />
    </div>
  </div>
  <Toast />
</template>

<style scoped>
/* 舞台 + 右侧上下文栏 */
/* 进入工作区的入场动效：自上而下渐入。
   幅度刻意小（12px）——太大会像「弹出来」，不像「打开」。 */
@keyframes ctx-slide-in {
  from { opacity: 0; transform: translateX(10px); }
  to { opacity: 1; transform: none; }
}

@keyframes stage-drop-in {
  from { opacity: 0; transform: translateY(-12px); }
  to { opacity: 1; transform: none; }
}

.stage-wrap {
  display: flex;
  height: calc(100dvh - var(--header-h));
  min-width: 0;
  min-height: 0;
}

/* 第二列上下文栏（IconRail 右边，仅剧本页）：宽度由拖拽手柄决定
   （220–480px，默认 260px，记忆在 localStorage）。
   position:relative 必给：.aurora 是 fixed z:0 的光斑层，static 的列会被
   整个盖黑（2026-11-20 截图验收抓到：图标栏/上下文栏整列发黑，内容其实都在）。 */
.ctx-wrap {
  position: relative;
  flex: none;
  width: 260px; /* 兜底值；实际宽度走内联样式（拖拽记忆） */
  min-height: 0;
  animation: ctx-slide-in 240ms var(--ease) backwards;
  transition: width 160ms var(--ease), opacity 200ms var(--ease);
  overflow: hidden;
  border-right: 1px solid var(--line);
  background: var(--surface);
}
/* 拖动中关掉宽度过渡，否则手柄和列会互相追着跑 */
.ctx-wrap.resizing { transition: none; }
.ctx-wrap.closed {
  width: 0;
  opacity: 0;
  pointer-events: none;
}

/* 拖宽手柄：叠在侧栏右缘的细命中区（净占宽 0），hover/拖动时亮出竖线 */
.ctx-handle {
  position: relative; flex: none; width: 7px;
  margin-left: -4px; margin-right: -3px;
  cursor: col-resize; z-index: 5;
}
.ctx-handle::after {
  content: ''; position: absolute; top: 0; bottom: 0; left: 3px; width: 2px;
  border-radius: 1px; background: transparent;
  transition: background 130ms var(--ease);
}
.ctx-handle:hover::after { background: var(--sel-line); }
.ctx-handle.on::after { background: var(--sel-bar); }

/* 右侧 Agent 对话面板（即梦式常驻右栏）：收起时 width:0，主区自动补位。
   同 ctx-wrap：必须 relative，否则光斑层盖住面板底。 */
.agent-wrap {
  position: relative;
  flex: none;
  width: clamp(360px, 30vw, 460px);
  min-height: 0;
  transition: width 200ms var(--ease), opacity 200ms var(--ease);
  overflow: hidden;
}
.agent-wrap.closed {
  width: 0;
  opacity: 0;
  pointer-events: none;
}

/* 收起后的重开浮标（右下角，即梦把 Agent 收进图标时也有类似入口） */
.agent-reopen {
  position: fixed; right: 18px; bottom: 18px; z-index: 70;
  height: 34px; padding: 0 14px; border-radius: var(--radius-full);
  border: 1px solid var(--line-strong);
  background: var(--panel-solid, var(--surface));
  box-shadow: var(--shadow-2);
  font-size: 12.5px;
}

/* 主舞台：对话流 + 输入区 */
/* flex:1 必给——.stage-wrap 是 flex 行，不给的话它按内容宽排，整块会挤到左边 */
.stage {
  display: flex;
  flex: 1;
  flex-direction: column;
  /* 进入工作区：从上方渐入（配合 rightOpen 的收起 → 展开也有同样的入场感） */
  animation: stage-drop-in 280ms var(--ease) backwards;
  height: calc(100dvh - var(--header-h));
  min-width: 0;
  min-height: 0;
  position: relative;
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

/* ---------- 响应式：≤1023px 上下文栏与 Agent 面板抽屉化 ---------- */
/* 即梦式四区在窄屏收不成四列：第二列（上下文栏）与右列（Agent 面板）
   变覆盖式抽屉，主内容区保持全宽；开合仍由 store.rightOpen / agentOpen 驱动。 */
@media (max-width: 1023px) {
  .ctx-wrap,
  .agent-wrap {
    position: fixed;
    top: var(--header-h); bottom: 0;
    z-index: 60;
    width: min(86vw, 400px) !important;
    background: var(--surface);
    box-shadow: var(--shadow-3);
    transition: transform 280ms var(--ease), visibility 280ms;
  }
  .ctx-wrap { left: 56px; border-right: 1px solid var(--line); }
  .agent-wrap { right: 0; border-left: 1px solid var(--line); }
  /* 抵消桌面的 width+opacity 塌缩：抽屉用 transform 出入场 */
  .ctx-wrap.closed,
  .agent-wrap.closed {
    opacity: 1;
    visibility: hidden;
    pointer-events: none;
  }
  .ctx-wrap.closed { transform: translateX(-102%); }
  .agent-wrap.closed { transform: translateX(102%); }
  .stage { width: 100%; }
}

@media (max-width: 760px) {
  .drop-zone { width: min(86vw, 360px); padding: 36px 22px; text-align: center; }
  .drop-text { font-size: 16px; }
}
</style>