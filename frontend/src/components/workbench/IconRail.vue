<script setup>
// =====================================================================
// IconRail.vue —— 最左细图标栏（即梦式）
//
// 上段：当前视图的文件导航（剧本/分镜/角色/分集，图标 + 短字）。
// 下段：作品、新建、辅助面板、设置。
// 对话不在这里——对话是右侧常驻面板（AgentPanel），会话功能自持在那张卡片右上角。
// =====================================================================
import AppIcon from '../AppIcon.vue'
import { store, toggleAgentPanel } from '../../stores/app'

const VIEWS = [
  { key: 'script', label: '剧本', icon: 'file' },
  { key: 'clips', label: '分镜', icon: 'film' },
  { key: 'roles', label: '角色', icon: 'user' },
  { key: 'episodes', label: '分集', icon: 'panel' },
]
</script>

<template>
  <nav class="icon-rail" aria-label="工作台导航">
    <!-- 总览（旧版是品牌字「问」方块——语义不明，2026-11-21 用户指出无意义，
         改成与其它栏位一致的「图标 + 总览」项） -->
    <button class="rail-item" :class="{ active: !store.wbView }"
      title="作品总览" @click="store.wbView = null">
      <AppIcon name="home" />
      <span class="rail-label">总览</span>
    </button>
    <div class="rail-sep" />
    <button
      v-for="v in VIEWS" :key="v.key"
      class="rail-item" :class="{ active: store.wbView === v.key }"
      :title="v.label" @click="store.wbView = v.key"
    >
      <AppIcon :name="v.icon" />
      <span class="rail-label">{{ v.label }}</span>
    </button>
    <div class="rail-spacer" />
    <button class="rail-item" :class="{ active: store.agentOpen }"
      title="Agent 对话面板" @click="toggleAgentPanel()">
      <AppIcon name="send" />
      <span class="rail-label">对话</span>
    </button>
    <button class="rail-item" title="我的作品" @click="store.overlay = 'tasks'">
      <AppIcon name="folder" />
    </button>
    <button class="rail-item" :class="{ active: store.overlay === 'skills' }"
      title="技能" @click="store.overlay = 'skills'">
      <AppIcon name="sparkle" />
      <span class="rail-label">技能</span>
    </button>
    <button class="rail-item" title="设置" @click="store.showSettings = true">
      <AppIcon name="gear" />
    </button>
  </nav>
</template>

<style scoped>
.icon-rail {
  /* position:relative 必给：.aurora 是 fixed z:0 的光斑层，
     static 的列会被它整个盖黑（2026-11-20 截图验收抓到）。 */
  position: relative;
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  width: 56px; flex: none;
  padding: 8px 4px 10px;
  border-right: 1px solid var(--line);
  background: var(--surface);
  height: 100%;
}
.rail-sep { width: 24px; height: 1px; background: var(--line); margin: 4px 0 8px; }
.rail-item {
  position: relative;
  display: flex; flex-direction: column; align-items: center; gap: 3px;
  width: 44px; padding: 8px 2px; border: none; border-radius: var(--radius-sm);
  background: transparent; color: var(--muted);
  font-size: 10.5px; line-height: 1.2; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.rail-item :deep(svg) { width: 1.5em; height: 1.5em; }
.rail-item:hover { color: var(--ink); background: var(--surface-2); }
/* 选中态统一语言（2026-11-21 重做「白条亮字」，金底金条光晕全废）：
   白 8% 底 + 左侧 2px 白条（不发光）+ 图标文字转纯白。
   令牌在 style.css（--sel-bg / --sel-bar），全站选中都按这套走。 */
.rail-item.active {
  color: var(--ink);
  background: var(--sel-bg);
}
.rail-item.active :deep(svg) { color: var(--ink); }
.rail-item.active::before {
  content: ''; position: absolute; left: 2px; top: 7px; bottom: 7px;
  width: 2px; border-radius: 2px; background: var(--sel-bar);
}
.rail-spacer { flex: 1; }
</style>
