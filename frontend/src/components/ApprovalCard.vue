<script setup>
// =====================================================================
// ApprovalCard.vue —— 对话里的「批准镜头提示词」确认卡（D-43 硬闸门）
//
// Agent 只能发起批准请求（approve_video_prompts 工具），生效必须由用户点
// 这张卡的「确认批准」或回复「批准」——出片花钱，最终决定权不能交给模型。
// 点击后走与改编卡片相同的 meta 确定性路径（/chat 接口，不经过 LLM）。
// =====================================================================

import { computed, ref } from 'vue'
import { resolveApproval } from '../stores/app'

const props = defineProps({
  payload: { type: Object, required: true }, // { type, approval_id, shot_ids, status }
})

const status = computed(() => props.payload.status || 'pending')
const shotCount = computed(() => (props.payload.shot_ids || []).length)
const busy = ref(false)

async function decide(action) {
  if (busy.value) return
  busy.value = true
  try {
    await resolveApproval(props.payload.approval_id, action)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card approval" :class="{ resolved: status !== 'pending' }">
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
  </div>
</template>

<style scoped>
.approval { padding: 12px 14px; }
.approval.resolved { opacity: .72; }
.top { display: flex; align-items: center; gap: 10px; }
h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ink); }
.flex1 { flex: 1; }
.badge { font-size: 11.5px; color: var(--warn, #d6a419); }
.ok { font-size: 11.5px; color: var(--ok); }
.muted { font-size: 11.5px; color: var(--muted); }
.desc { margin: 8px 0 10px; font-size: 12.5px; line-height: 1.7; color: var(--muted); }
.actions { display: flex; gap: 8px; }
.btn {
  padding: 7px 14px; border-radius: var(--radius-md); font-size: 12.5px;
  border: 1px solid var(--line); cursor: pointer; background: transparent; color: var(--ink);
}
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent, #fff); font-weight: 600; }
.btn.ghost { color: var(--muted); }
.btn:disabled { opacity: .5; cursor: default; }
</style>
