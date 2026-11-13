<script setup>

const emit = defineEmits(['switch-view', 'open-tasks'])
// =====================================================================
// HeaderBar.vue —— 顶栏
//
// 品牌 + 批次状态徽标 + 面板开关。命令面板与模型入口已按「简化 UI」
// 方向移除：模型状态放在输入区工具行，模型配置在设置弹窗里。
// 桌面端使用原生窗口标题栏，不需要自定义窗口控制按钮。
// =====================================================================

import { computed } from 'vue'
import { store, toggleRightPanel, exitWorkspace } from '../stores/app'
import AppIcon from './AppIcon.vue'

// 批次状态徽标：批次在跑（running / awaiting_approval）时显示，点击直达视频工作台。
const batchBadge = computed(() => {
  const b = store.videoBatch
  if (!b) return null
  const pos = `${Math.min(b.current_index + 1, b.total)}/${b.total}`
  if (b.status === 'running') return { status: 'running', text: `生成中 ${pos}` }
  if (b.status === 'awaiting_approval') return { status: 'awaiting', text: `待审批 ${pos}` }
  return null
})

function openWorkbench() {
  store.view = 'video'
  if (!store.rightOpen) toggleRightPanel()
}
</script>

<template>
  <header>
    <!-- 我的作品（点开是一个页面，不再是常驻侧栏） -->
    <button
      class="ghost nav-toggle"
      title="我的作品"
      @click="emit('open-tasks')"
    >
      <AppIcon name="folder-open" />
    </button>

    <h1 class="brand" title="回到首页" @click="exitWorkspace">
      <span class="brand-name">问剧</span>
    </h1>

    <button
      v-if="batchBadge"
      :class="['batch-chip', batchBadge.status]"
      title="打开视频工作台"
      @click="openWorkbench"
    >
      <span class="dot" aria-hidden="true"></span>{{ batchBadge.text }}
    </button>

    <!-- 顶栏按钮 = 右侧栏的视图开关：默认不显式（无边框），
         正显示这个视图时才有边框；右侧栏收起就没有边框。 -->
    <button
      :class="['ghost', 'page-btn', { on: store.rightOpen && store.sideView === 'assets' }]"
      title="资产库（角色 / 场景 / 素材）"
      @click="emit('switch-view', 'assets')"
    >
      <AppIcon name="folder" /> <span>资产库</span>
    </button>

    <button
      :class="['ghost', 'page-btn', { on: store.rightOpen && store.sideView === 'skills' }]"
      title="技能"
      @click="emit('switch-view', 'skills')"
    >
      <AppIcon name="sparkle" /> <span>技能</span>
    </button>

    <button
      :class="['ghost', 'page-btn', { on: store.rightOpen && store.sideView === 'shots' }]"
      title="产出（镜头与提示词）"
      @click="emit('switch-view', 'shots')"
    >
      <AppIcon name="film" /> <span>产出</span>
    </button>

    <button
      class="ghost panel-toggle"
      :class="{ on: store.rightOpen }"
      title="上下文侧栏（大纲 / 镜头 / 资产）"
      aria-label="切换上下文侧栏"
      @click="toggleRightPanel()"
    >
      <AppIcon name="panel" />
    </button>
  </header>
</template>

<style scoped>
header {
  position: relative; z-index: 3;
  display: flex; align-items: center; gap: 10px; padding: 8px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  min-height: var(--header-h);
}
h1 {
  font-size: 13px; margin: 0; display: flex; align-items: center; gap: 7px;
  font-weight: 700; letter-spacing: 0.02em; flex: none; color: var(--ink);
  cursor: pointer;
}
/* 品牌字标：plain 文字，不做渐变 */
.brand-name {
  white-space: nowrap;
  font-size: 15px; font-weight: 600; letter-spacing: 0.06em;
  color: var(--ink);
}
header button {
  position: relative;
  flex: none; white-space: nowrap; height: var(--control-h); padding: 0 10px;
  display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px;
}
/* 可视 36px，命中区外扩到 ≥44px（伪元素承接点击） */
header button::after { content: ''; position: absolute; inset: -4px -6px; }

/* 导航开关 */
.nav-toggle { padding: 0 8px; width: 32px; }
.nav-toggle .app-ic { width: 14px; height: 14px; }
.nav-toggle.on { color: var(--ink); }

/* 顶栏按钮：平时不显式（透明底、无边框），右侧栏正显示它时才有边框；
   右侧栏收起 → 又回到无边框的样子。 */
.page-btn {
  display: inline-flex; align-items: center; gap: 5px;
  height: var(--control-h); padding: 0 10px;
  color: var(--muted); font-size: 12.5px;
  border-color: transparent;
  background: transparent;
  transition: color var(--dur) var(--ease), border-color var(--dur) var(--ease),
              background var(--dur) var(--ease);
}
.page-btn:hover {
  color: var(--ink);
  border-color: var(--line);
  background: rgba(255, 255, 255, .06);
}
.page-btn.on {
  color: var(--ink);
  border-color: var(--line-strong);
  background: rgba(255, 255, 255, .09);
}
.page-btn .app-ic { width: 13px; height: 13px; }

.panel-toggle { margin-left: auto; }
.panel-toggle .app-ic { width: 13px; height: 13px; }
.panel-toggle.on { background: var(--select); border-color: var(--line-strong); color: var(--ink); }

/* 批次状态：安静的文字状态，不用彩色胶囊 */
.batch-chip {
  display: inline-flex; align-items: center; gap: 6px; height: 28px;
  padding: 0 10px; border-radius: var(--radius-md); font-size: 11.5px; font-weight: 500;
  cursor: pointer; white-space: nowrap;
  border: 1px solid var(--line);
  color: var(--cyan);
  background: rgba(255, 255, 255, .05);
  transition: color var(--dur) var(--ease), border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.batch-chip:hover { border-color: var(--line-strong); background: rgba(255, 255, 255, .1); }
.batch-chip .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.batch-chip.running .dot { animation: batch-pulse 1.2s ease-in-out infinite; }
.batch-chip.awaiting {
  color: var(--gold);
}
@keyframes batch-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.72); }
}

@media (max-width: 760px) {
  header { min-height: var(--header-h); padding: 6px 10px; gap: 7px; }
}
</style>
