<script setup>
// =====================================================================
// ProductionBudgetCard.vue —— 「生产流水线要动多少资源」的确认卡
//
// 一键跑完生产流水线会花钱（模型调用 + 逐张生图）。Agent 只能**发起**
// （start_production 工具），真正开跑由用户点这张卡的「开始制作」触发——
// 服务端硬闸门，模型伪造不了（同 D-43 的出片批准）。
//
// 卡上报的是**确定的事实**（复用哪些阶段、要补几张图、之后出片多少秒），
// 不是报价——D-53 定了界面不显示费用预估，因为预估与实付会脱节。
// =====================================================================

import { computed, ref } from 'vue'
import { confirmProduction } from '../stores/app'

const props = defineProps({
  payload: { type: Object, required: true }, // { type, approval_id, project_id, limit, estimate }
})

const busy = ref(false)
const est = computed(() => props.payload.estimate || {})
const stages = computed(() => est.value.stages || [])
const toRun = computed(() => stages.value.filter((s) => s.action === 'run'))

async function start() {
  if (busy.value) return
  busy.value = true
  try {
    await confirmProduction(props.payload.approval_id)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card budget">
    <div class="top">
      <h3>🎬 开始制作前看一眼</h3>
      <span class="flex1"></span>
      <span class="mono muted">{{ toRun.length }} 个阶段要跑</span>
    </div>
    <p class="desc">
      这条流水线要动模型、还要逐张生参考图。下面是这次的实际用量——已完成的阶段会复用，
      不会重复做。
    </p>
    <ul class="stages">
      <li v-for="s in stages" :key="s.key">
        <span class="st-label">{{ s.label }}</span>
        <span class="st-action" :class="s.action">{{ s.action === 'reuse' ? '复用' : '要跑' }}</span>
        <span class="st-detail">{{ s.detail }}</span>
      </li>
    </ul>
    <div v-if="est.images_to_make" class="line">
      要生成 <strong>{{ est.images_to_make }}</strong> 张定妆照 / 场景图
    </div>
    <div v-if="est.film_units" class="line">
      出片阶段：{{ est.film_units }} 段、合计 {{ Math.round(est.film_seconds) }} 秒
      <span class="muted">（批准提示词后才真的花钱生成）</span>
    </div>
    <div v-if="payload.limit" class="line">本轮只做前 {{ payload.limit }} 场戏</div>
    <div class="actions">
      <button class="btn primary" :disabled="busy" @click="start">
        {{ busy ? '已启动…' : '开始制作' }}
      </button>
      <span class="muted small">点这个才会真的开跑</span>
    </div>
  </div>
</template>

<style scoped>
.budget { padding: 12px 14px; }
.top { display: flex; align-items: center; gap: 10px; }
h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ink); }
.flex1 { flex: 1; }
.muted { color: var(--muted); font-size: 11.5px; }
.small { font-size: 11px; }
.desc { margin: 8px 0 8px; font-size: 12.5px; line-height: 1.7; color: var(--muted); }
.stages { list-style: none; margin: 0 0 8px; padding: 0; }
.stages li {
  display: flex; align-items: baseline; gap: 8px;
  font-size: 12px; line-height: 1.9;
}
.st-label { flex: none; min-width: 56px; color: var(--ink); font-weight: 600; }
.st-action { flex: none; font-size: 10.5px; color: var(--muted); }
.st-action.run { color: var(--warn, #d6a419); }
.st-detail { color: var(--muted); }
.line { font-size: 12px; color: var(--muted); line-height: 1.8; }
.actions { display: flex; align-items: center; gap: 8px; margin-top: 10px; }
.btn {
  padding: 7px 14px; border-radius: var(--radius-md); font-size: 12.5px;
  border: none; cursor: pointer; background: rgba(255, 255, 255, .06); color: var(--ink);
}
.btn.primary { background: #fff; color: #0a0a0f; font-weight: 600; }
.btn:disabled { opacity: .5; cursor: default; }
</style>
