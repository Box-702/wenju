<script setup>
// =====================================================================
// BatchStartedCard.vue —— 对话里的「出片批次已开始」卡
//
// 后端在批次启动成功后会发一条 batch_started 载荷（app/chat/tools/video.py），
// 但前端此前**没有任何分支渲染它**——用户只能在正文里读到一句「已开始出片」，
// 看不到进度。这张卡把批次进度就地显示出来，不用切走去看。
//
// 进度数据源是全局批次轮询（store.videoBatch / ensureBatchPolling），
// 与分镜页（ClipsView）读的是同一份，不会出现两处数字打架。
// =====================================================================

import { computed } from 'vue'
import { store } from '../stores/app'

const props = defineProps({
  payload: { type: Object, required: true }, // { type, batch_id, project_id }
})

/** 「看进度」直达分镜页（旧产出页已删，2026-11-21）。 */
function goClips() {
  store.overlay = null
  store.wbView = 'clips'
}

const batch = computed(() => store.videoBatch)

// 只有当前作品、且是同一批次时才把进度接到这张卡上；
// 换了作品或重开批次，卡片就停在「已开始」不装作还在跑。
const sameBatch = computed(
  () => batch.value?.id && batch.value.id === props.payload.batch_id,
)
const done = computed(() => Object.keys(batch.value?.results || {}).length)
const total = computed(() => batch.value?.total ?? 0)
const statusText = computed(() => {
  if (!sameBatch.value) return '已提交，按顺序生成中'
  const st = batch.value.status
  if (st === 'done') return `✓ 全部出片（${done.value}/${total.value}）`
  if (st === 'failed') return `中断：${batch.value.error || '未知原因'}`
  if (st === 'aborted') return '已终止'
  if (st === 'awaiting_approval') return '等待批准'
  return `生成中 ${done.value}/${total.value}`
})
const failed = computed(
  () => sameBatch.value && (batch.value.status === 'failed' || batch.value.status === 'aborted'),
)
</script>

<template>
  <div class="card batch" :class="{ failed }">
    <div class="top">
      <h3>🎬 出片已开始</h3>
      <span class="flex1"></span>
      <span class="mono" :class="failed ? 'err' : 'muted'">{{ statusText }}</span>
    </div>
    <p class="desc">
      批次 {{ payload.batch_id }} 已提交。出片按顺序推进，前一镜的成片会自动作为后一镜的参考，
      保证人物与光线连贯。
    </p>
    <div class="actions">
      <button class="btn ghost small" @click="goClips">看进度</button>
    </div>
  </div>
</template>

<style scoped>
.batch { padding: 12px 14px; }
.top { display: flex; align-items: center; gap: 10px; }
h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ink); }
.flex1 { flex: 1; }
.muted { font-size: 11.5px; color: var(--muted); }
.err { font-size: 11.5px; color: var(--ink); }
.desc { margin: 8px 0 10px; font-size: 12.5px; line-height: 1.7; color: var(--muted); }
.actions { display: flex; align-items: center; gap: 8px; }
.btn {
  padding: 7px 14px; border-radius: var(--radius-md); font-size: 12.5px;
  border: none; cursor: pointer; background: rgba(255, 255, 255, .06); color: var(--ink);
}
.btn.ghost { background: transparent; color: var(--muted); }
.btn.ghost.small { padding: 5px 10px; font-size: 12px; }
</style>
