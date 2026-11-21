<script setup>
// =====================================================================
// HeaderBar.vue —— 顶栏（2026-11-21 收敛）
//
// 只剩 品牌字标 + 批次状态徽标。导航全部归最左图标栏（IconRail）：
// 资产库/产出/我的作品在图标栏有同名入口，顶栏那排是旧「右栏视图开关」
// 设计的残留（产出键还把人引到已删除的产出页），已按用户要求清掉。
// 上下文侧栏不再支持关闭（可拖右缘调宽），自然也没有开关按钮。
// =====================================================================

import { computed } from 'vue'
import { store, exitWorkspace } from '../stores/app'
import AppIcon from './AppIcon.vue'

// 批次状态徽标：批次在跑（running / awaiting_approval）时显示，点击直达分镜页。
const batchBadge = computed(() => {
  const b = store.videoBatch
  if (!b) return null
  const pos = `${Math.min(b.current_index + 1, b.total)}/${b.total}`
  if (b.status === 'running') return { status: 'running', text: `生成中 ${pos}` }
  if (b.status === 'awaiting_approval') return { status: 'awaiting', text: `待审批 ${pos}` }
  return null
})

function openWorkbench() {
  store.overlay = null
  store.wbView = 'clips'
}
</script>

<template>
  <header>
    <h1 class="brand" title="回到首页" @click="exitWorkspace">
      <span class="brand-name">问剧</span>
    </h1>

    <button
      v-if="batchBadge"
      :class="['batch-chip', batchBadge.status]"
      title="打开分镜页"
      @click="openWorkbench"
    >
      <span class="dot" aria-hidden="true"></span>{{ batchBadge.text }}
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

/* 批次状态：安静的文字状态，不用彩色胶囊 */
.batch-chip {
  display: inline-flex; align-items: center; gap: 6px; height: 28px;
  padding: 0 10px; border-radius: var(--radius-md); font-size: 11.5px; font-weight: 500;
  cursor: pointer; white-space: nowrap;
  border: none;
  color: var(--cyan);
  background: rgba(255, 255, 255, .05);
  transition: color var(--dur) var(--ease), border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.batch-chip:hover { background: rgba(255, 255, 255, .1); }
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
