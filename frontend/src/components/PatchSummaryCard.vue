<script setup>
// =====================================================================
// PatchSummaryCard.vue —— 对话里的「改编提议」摘要条
//
// 只显示最外层信息：涉及哪些场景、各多少处修改（+新增 -删除 ～修改）。
// 点击摘要条 / 场景芯片 → 打开右侧审阅抽屉（PatchDrawer）看逐条细节。
// =====================================================================

import { computed } from 'vue'
import { openPatchDrawer } from '../stores/app'
import { groupPatch, patchStats } from '../utils/format'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  payload: { type: Object, required: true }, // { type, run_id, patch, plan, error }
})

const patch = computed(() => props.payload.patch || [])
const stats = computed(() => patchStats(patch.value))
const groups = computed(() => groupPatch(patch.value))
const review = computed(() => {
  const r = props.payload.review
  if (!r) return null
  const issues = r.issues || []
  return { ...r, errors: issues.filter((i) => i.severity === 'error').length }
})

/** 场景显示名：无标题时把 scene_001 简化成「场景 1」。 */
function sceneName(key) {
  const m = /^scene_(\d+)$/.exec(key)
  return m ? `场景 ${Number(m[1])}` : key
}
</script>

<template>
  <div class="card summary" role="button" tabindex="0" @click="openPatchDrawer(payload)" @keydown.enter="openPatchDrawer(payload)" @keydown.space.prevent="openPatchDrawer(payload)">
    <div class="top">
      <h3>✍️ 改编提议</h3>
      <span class="stats mono">+{{ stats.add }} −{{ stats.remove }} ～{{ stats.modify }}</span>
      <span class="flex1"></span>
      <span class="detail">审阅改动 →</span>
    </div>

    <!-- 评审打分 + 一致性保障结果（后端 guard 的 LLM 审阅） -->
    <div v-if="review" class="review" :class="{ bad: !review.passed }">
      <span class="score mono">{{ review.overall_score }}<small>/100</small></span>
      <span class="score-lbl">{{ review.passed ? '评审通过' : '需调整' }}</span>
      <span v-if="review.errors" class="iss mono"><AppIcon name="warning" /> {{ review.errors }} 个问题</span>
      <span v-else-if="review.issues && review.issues.length" class="iss mono"><AppIcon name="warning" /> {{ review.issues.length }} 条建议</span>
    </div>

    <!-- 涉及的场景：只给名字和数量，细节在抽屉里 -->
    <div v-if="groups.length" class="chips">
      <button v-for="g in groups" :key="g.key" class="scene-chip" @click.stop="openPatchDrawer(payload)">
        {{ sceneName(g.key) }}<span class="cnt">{{ g.items.length }} 处</span>
      </button>
    </div>
    <div v-else class="muted">没有可落地的 patch 操作</div>

    <div v-if="payload.error" class="muted err"><AppIcon name="warning" /> {{ payload.error }}</div>
  </div>
</template>

<style scoped>
.summary { cursor: pointer; transition: border-color var(--dur) var(--ease), background-color var(--dur) var(--ease); }
.summary:hover { border-color: var(--line-strong); background: color-mix(in oklch, var(--ink) 2%, var(--panel)); }
.summary:focus-visible { outline: 2px solid color-mix(in oklch, var(--accent) 45%, transparent); outline-offset: 2px; }
.top { display: flex; align-items: center; gap: 10px; }
h3 { margin: 0; font-size: 13px; font-weight: 600; color: var(--ink); }
.stats { color: var(--muted); font-size: 11.5px; }
.stats::first-letter { color: var(--ok); }
.flex1 { flex: 1; }
.detail { color: var(--muted); font-size: 12px; font-weight: 600; }
.summary:hover .detail { color: var(--ink); }
.review {
  display: flex; align-items: center; gap: 8px; margin-top: 10px;
  padding: 6px 8px; border-radius: var(--radius-sm); background: color-mix(in oklch, var(--ok) 8%, transparent);
}
.review.bad { background: color-mix(in oklch, var(--bad) 9%, transparent); }
.review .score { font-weight: 700; color: var(--ok); font-size: 14px; }
.review.bad .score { color: var(--bad); }
.review .score small { font-size: 10px; color: var(--muted); font-weight: 500; }
.review .score-lbl { font-size: 12px; font-weight: 600; color: var(--ink); }
.review .iss { font-size: 11px; color: var(--bad); }
.chips { display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 10px; }
.scene-chip {
  background: transparent; border: none; color: var(--muted);
  border-radius: 3px; padding: 2px 4px; font-size: 11.5px; font-weight: 400;
  display: inline-flex; align-items: center; gap: 5px;
}
.scene-chip:hover { color: var(--ink); background: color-mix(in oklch, var(--ink) 6%, transparent); }
.scene-chip .cnt { color: var(--dim); font-size: 10.5px; }
.err { margin-top: 8px; color: var(--bad); }
</style>
