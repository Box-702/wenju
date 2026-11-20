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
import ViewerPanel from './components/ViewerPanel.vue'
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
import AssetTree from './components/workbench/AssetTree.vue'
import ScriptHistoryPanel from './components/workbench/ScriptHistoryPanel.vue'
import WorkbenchView from './components/workbench/WorkbenchView.vue'
import AgentPanel from './components/workbench/AgentPanel.vue'
import { loadTree, loadWorkspace, loadProjectFiles, restorePanelPrefs, toggleRightPanel, toggleAgentPanel, notify, store, pollVideoBatch, ensureBatchPolling, loadModelStatus, loadAssetIndex, loadAssetRefs, closePromptEditor, saveShotPrompt, loadStoryPlan, closeVideo } from './stores/app'
import { initNav, pushNav } from './nav'
import { importProject } from './api'

// ---- 事件驱动的视图引导（即梦式布局改版） ----
// 第二列现在是资产树（AssetTree），没有 tab 体系了：
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

/** 顶栏按钮 = 工作台视图开关：资产库→角色页、产出→分镜页；
    再点同一个回总览。技能仍是全屏页。 */
function switchSideView(view) {
  const target = view === 'assets' ? 'roles' : view === 'shots' ? 'clips' : null
  if (!target) { store.overlay = 'skills'; return }
  store.wbView = store.wbView === target ? null : target
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
// 窄屏下右栏视图切换（如场景聚焦）同理收起左栏
watch(() => store.view, () => {
  if (isNarrow.value && store.rightOpen) store.agentOpen = false
})

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
  window.addEventListener('keydown', globalKey)
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
    <!-- 即梦式四区（D-73 布局修正）：
         最左细图标栏 ｜ 上下文栏（大纲/镜头/资产/白模）｜ 主内容区 ｜ 右侧常驻 Agent 对话面板。
         对话仍是完整的 Agent（42 个工具），只是位置从主栏搬进右栏——即梦把 Agent 放右边，
         我们照做；收起后右下角浮标能再打开。 -->
    <div class="stage-wrap">
      <IconRail />
      <div class="ctx-wrap" :class="{ closed: !store.rightOpen }">
        <!-- 即梦式：左栏随视图切换——剧本页看修改历史，其余页看资产树 -->
        <ScriptHistoryPanel v-if="store.wbView === 'script'" />
        <AssetTree v-else />
      </div>
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

  <!-- 全局视频播放器：任何视图设 store.playingVideo 都能弹（即梦式行内/浮层预览）。
       以前只挂在 VideoWorkbench，工作台的「播放」点了没反应就是这个原因。 -->
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

/* 第二列上下文栏（IconRail 右边）：定宽，收起时 width:0。
   即梦式布局里它是「文件/资产」列，不该吃掉主区——300px 定宽。
   position:relative 必给：.aurora 是 fixed z:0 的光斑层，static 的列会被
   整个盖黑（2026-11-20 截图验收抓到：图标栏/上下文栏整列发黑，内容其实都在）。 */
.ctx-wrap {
  position: relative;
  flex: none;
  width: 300px;
  min-height: 0;
  animation: ctx-slide-in 240ms var(--ease) backwards;
  transition: width 200ms var(--ease), opacity 200ms var(--ease);
  overflow: hidden;
  border-right: 1px solid var(--line);
  background: var(--surface);
}
.ctx-wrap.closed {
  width: 0;
  opacity: 0;
  pointer-events: none;
}

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

/* 产出页里的面板撑满 */
.page-viewer {
  display: flex;
  flex-direction: column;
  min-height: 0;
  width: 100%;
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