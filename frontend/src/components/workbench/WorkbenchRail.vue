<script setup>
// =====================================================================
// WorkbenchRail.vue —— 工作台左侧竖排四栏（对标即梦：剧本 / 分镜 / 角色 / 分集视频）
//
// 对话仍是主入口（D-66），这一栏是「看与调」的并列主栏：
// 点当前已选中的项 = 回到对话（wbView 置 null）。
// =====================================================================
import AppIcon from '../AppIcon.vue'
import { store, setWbView } from '../../stores/app'

const VIEWS = [
  { key: 'script', label: '剧本', icon: 'file' },
  { key: 'clips', label: '分镜', icon: 'film' },
  { key: 'roles', label: '角色', icon: 'user' },
  { key: 'episodes', label: '分集', icon: 'panel' },
]
</script>

<template>
  <nav class="rail" aria-label="工作台视图">
    <button
      v-for="v in VIEWS" :key="v.key"
      class="rail-item" :class="{ active: store.wbView === v.key }"
      :title="v.label" @click="setWbView(v.key)"
    >
      <AppIcon :name="v.icon" />
      <span class="rail-label">{{ v.label }}</span>
    </button>
    <div class="rail-spacer" />
    <button
      class="rail-item" :class="{ active: !store.wbView }"
      title="回到对话" @click="store.wbView = null"
    >
      <AppIcon name="send" />
      <span class="rail-label">对话</span>
    </button>
  </nav>
</template>

<style scoped>
.rail {
  display: flex; flex-direction: column; gap: 2px;
  width: 64px; flex: none;
  padding: 10px 6px;
  border-right: 1px solid var(--line);
  background: var(--surface);
  height: 100%;
  overflow-y: auto;
}
.rail-item {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  padding: 9px 2px; border: none; border-radius: var(--radius-sm);
  background: transparent; color: var(--muted);
  font-size: 11px; line-height: 1.2; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
/* 图标尺寸跟 font-size（AppIcon 约定：1em） */
.rail-item :deep(svg) { width: 1.55em; height: 1.55em; }
.rail-item:hover { color: var(--ink); background: var(--surface-2); }
.rail-item.active {
  color: var(--ink); background: var(--surface-2);
  box-shadow: inset 2px 0 0 var(--gold);
}
.rail-label { letter-spacing: 0.02em; }
.rail-spacer { flex: 1; }
</style>
