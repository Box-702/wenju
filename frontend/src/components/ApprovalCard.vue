<script setup>
// =====================================================================
// ApprovalCard.vue —— 对话里的「批准镜头提示词」确认卡（D-43 硬闸门）
//
// Agent 只能发起批准请求（approve_video_prompts 工具），生效必须由用户点
// 这张卡的「确认批准」或回复「批准」——出片花钱，最终决定权不能交给模型。
// 点击后走与改编卡片相同的 meta 确定性路径（/chat 接口，不经过 LLM）。
//
// 2026-11-13 用户指正「这是给人用的产品」：批准后不再让用户打字说「出片」，
// 卡片就地长出出片按钮——审核 → 批准 → 出片 → 进度，一条流程在一张卡里走完。
// =====================================================================

import { computed, ref } from 'vue'
import { resolveApproval, submitApprovedBatch, store, showView } from '../stores/app'

const props = defineProps({
  payload: { type: Object, required: true }, // { type, approval_id, shot_ids, status }
})

const status = computed(() => props.payload.status || 'pending')
const shotCount = computed(() => (props.payload.shot_ids || []).length)
const busy = ref(false)

// 出片按钮状态机：idle → running（批次在推进）→ done / error
const filmPhase = ref('idle')
const filmError = ref('')
const batch = computed(() => store.videoBatch)
const batchProgress = computed(() => {
  const b = batch.value
  if (!b?.id) return ''
  const done = Object.keys(b.results || {}).length
  return `${done}/${b.total ?? shotCount.value}`
})

async function decide(action) {
  if (busy.value) return
  busy.value = true
  try {
    await resolveApproval(props.payload.approval_id, action)
  } finally {
    busy.value = false
  }
}

async function makeFilm() {
  if (filmPhase.value === 'running') return
  filmPhase.value = 'running'
  filmError.value = ''
  const r = await submitApprovedBatch()
  if (r.ok) return // 进度由全局批次轮询推进，batch computed 会跟上
  // 400「还没有批准」等确定性拒绝：退回可重试态并把原因说人话
  filmPhase.value = 'error'
  filmError.value = r.error || '提交失败'
}

// 批次终态跟着全局 videoBatch 走（同一作品同一批次语义）
const batchDone = computed(
  () => filmPhase.value === 'running' && batch.value?.status === 'done',
)
const batchFailed = computed(
  () => filmPhase.value === 'running'
    && (batch.value?.status === 'failed' || batch.value?.status === 'aborted'),
)
const batchFailNote = computed(() => batch.value?.error || '批次中断（常见原因：API Key 余额不足）')
</script>

<template>
  <div class="card approval" :class="{ resolved: status === 'cancelled' || status === 'expired' }">
    <div class="top">
      <h3>🔐 批准镜头提示词</h3>
      <span class="flex1"></span>
      <span v-if="status === 'pending'" class="mono badge">待你确认</span>
      <span v-else-if="status === 'confirmed'" class="mono ok">已批准</span>
      <span v-else-if="status === 'cancelled'" class="mono muted">已取消</span>
      <span v-else class="mono muted">已过期</span>
    </div>
    <p class="desc">
      Agent 请求批准 {{ shotCount }} 个镜头的提示词。批准之后这些镜头才能提交出片（会花你的 API 额度）——
      确认权在你，不在 Agent。
    </p>
    <div v-if="status === 'pending'" class="actions">
      <button class="btn primary" :disabled="busy" @click="decide('confirm')">确认批准</button>
      <button class="btn ghost" :disabled="busy" @click="decide('cancel')">先不批准</button>
    </div>

    <!-- 批准后：出片按钮就在这张卡上长出来，不用再打字说「出片」 -->
    <div v-else-if="status === 'confirmed'" class="actions film-row">
      <template v-if="filmPhase === 'idle' || filmPhase === 'error'">
        <button class="btn primary" @click="makeFilm">▶ 出片（{{ shotCount }} 镜）</button>
        <span v-if="filmPhase === 'error'" class="mono err">{{ filmError }}</span>
      </template>
      <template v-else-if="filmPhase === 'running' && !batchDone && !batchFailed">
        <span class="mono run">生成中 {{ batchProgress }} · 前一镜完成自动接后一镜</span>
        <span class="flex1"></span>
        <button class="btn ghost small" @click="showView('video')">看进度</button>
      </template>
      <template v-else-if="batchFailed">
        <span class="mono err">{{ batchFailNote }}</span>
        <span class="flex1"></span>
        <button class="btn ghost" @click="filmPhase = 'idle'">重试出片</button>
      </template>
      <template v-else>
        <span class="mono ok">✓ 全部镜头已出片</span>
        <span class="flex1"></span>
        <button class="btn ghost small" @click="showView('video')">合成成片</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.approval { padding: 12px 14px; }
.approval.resolved { opacity: .72; }
.top { display: flex; align-items: center; gap: 10px; }
h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ink); }
.flex1 { flex: 1; }
.badge { font-size: 11.5px; color: var(--warn, #d6a419); }
.ok { font-size: 11.5px; color: var(--ink); }
.muted { font-size: 11.5px; color: var(--muted); }
.desc { margin: 8px 0 10px; font-size: 12.5px; line-height: 1.7; color: var(--muted); }
.actions { display: flex; align-items: center; gap: 8px; }
.film-row { margin-top: 2px; }
.run { font-size: 12px; color: var(--muted); }
.err { font-size: 11.5px; color: var(--ink); }
.btn {
  padding: 7px 14px; border-radius: var(--radius-md); font-size: 12.5px;
  border: 1px solid var(--line); cursor: pointer; background: transparent; color: var(--ink);
}
.btn.primary { background: #fff; border-color: transparent; color: #0a0a0f; font-weight: 600; }
.btn.ghost { color: var(--muted); }
.btn.ghost.small { padding: 5px 10px; font-size: 12px; }
.btn:disabled { opacity: .5; cursor: default; }
</style>
