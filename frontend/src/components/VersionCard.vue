<script setup>
// =====================================================================
// VersionCard.vue —— 「已生成新版本」卡片
//
// 展示新版本号与校验结果；点击「查看剧本」跳到右侧文本面板。
// =====================================================================

import { computed } from 'vue'
import { loadViewer, store } from '../stores/app'

const props = defineProps({
  payload: { type: Object, required: true }, // { type, version_id, validation_issues, fallback }
})

// 校验 error 数量决定校验徽章的红/绿
const errorCount = computed(() =>
  (props.payload.validation_issues || []).filter((i) => i.severity === 'error').length)

/** 刷新并跳到工作台剧本页（旧产出页已删，2026-11-21）。 */
async function viewScript() {
  await loadViewer()
  store.overlay = null
  store.wbView = 'script'
}
</script>

<template>
  <div class="card">
    <h3>✅ 已生成新版本</h3>
    <div class="row">
      <span class="pill ok">{{ payload.version_id || '' }}</span>
      <span class="pill" :class="errorCount ? 'bad' : 'ok'">校验 error {{ errorCount }}</span>
      <span v-if="payload.fallback" class="pill warn">兜底应用</span>
      <button class="ghost small" @click="viewScript">查看剧本</button>
    </div>
  </div>
</template>
