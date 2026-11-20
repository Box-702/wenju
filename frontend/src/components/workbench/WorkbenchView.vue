<script setup>
// =====================================================================
// WorkbenchView.vue —— 工作台主栏的分发器：按 store.wbView 渲染四视图之一
//
// 四视图对标即梦短剧工作室（D-73）：
//   script   剧本页（设定 / 分集大纲 / 正文 / 历史修改）
//   clips    分镜页（片段编辑器：参数 + 提示词 + 重出）
//   roles    角色页（资产库 + 音色 + 缺口）
//   episodes 分集视频页（每集一卡 + 批量 + 成片）
// =====================================================================
import { store } from '../../stores/app'
import OverviewView from './OverviewView.vue'
import ScriptView from './ScriptView.vue'
import ClipsView from './ClipsView.vue'
import RolesView from './RolesView.vue'
import EpisodesView from './EpisodesView.vue'
</script>

<template>
  <div class="wb-view">
    <OverviewView v-if="!store.wbView" />
    <ScriptView v-else-if="store.wbView === 'script'" />
    <ClipsView v-else-if="store.wbView === 'clips'" />
    <RolesView v-else-if="store.wbView === 'roles'" />
    <EpisodesView v-else-if="store.wbView === 'episodes'" />
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
