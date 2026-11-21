<script setup>
// =====================================================================
// WorkbenchView.vue —— 工作台主栏的分发器：按 store.wbView 渲染三视图之一
//
// 三视图对标即梦短剧工作室（D-73；2026-11-21 回查截图删「分集」独立视图，
// 分集切换在分镜页面包屑、合成全片在闸门条）：
//   script   剧本页（设定 / 分集大纲 / 正文 / 历史修改）
//   roles    角色页（资产库 + 音色 + 缺口）
//   clips    分镜页（整集片段同页 + 连播 + 合成全片）
// =====================================================================
import { store } from '../../stores/app'
import OverviewView from './OverviewView.vue'
import ScriptView from './ScriptView.vue'
import ClipsView from './ClipsView.vue'
import RolesView from './RolesView.vue'
</script>

<template>
  <div class="wb-view">
    <OverviewView v-if="!store.wbView" />
    <ScriptView v-else-if="store.wbView === 'script'" />
    <ClipsView v-else-if="store.wbView === 'clips'" />
    <RolesView v-else-if="store.wbView === 'roles'" />
  </div>
</template>

<style scoped>
.wb-view {
  display: flex; flex-direction: column;
  flex: 1; min-width: 0; min-height: 0;
  height: 100%;
  background: var(--bg);
  overflow: hidden;
}
</style>
