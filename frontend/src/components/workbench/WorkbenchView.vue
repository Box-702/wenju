<script setup>
// =====================================================================
// WorkbenchView.vue —— 工作台主栏的分发器：按 store.wbView 渲染三视图之一
//
// 四视图对标即梦短剧工作室（D-73/D-99）：分集视频是父页，分镜页只负责当前集：
//   script   剧本页（设定 / 分集大纲 / 正文 / 历史修改）
//   roles    角色页（资产库 + 音色 + 缺口）
//   episodes 分集视频（每集一张卡，选集后进分镜）
//   clips    分镜页（当前集片段同页 + 连播 + 合成全片）
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
    <EpisodesView v-else-if="store.wbView === 'episodes'" />
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
