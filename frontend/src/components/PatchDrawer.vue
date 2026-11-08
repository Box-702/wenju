<script setup>
// =====================================================================
// PatchDrawer.vue —— 改编提议审阅抽屉（从右侧拉出）
//
// 由对话里的摘要条（PatchSummaryCard）唤起：
//   - 按场景分组的逐条改动：勾选、增/删/改标签、新旧值对比；
//   - 「修改」类改动可直接**就地编辑新值**（不再手改 JSON）；
//   - 有就地改动时，接受会走「应用你编辑后的 patch」；
//   - 底部操作条：反馈重新生成 / 接受勾选 / 接受全部 / 拒绝。
// 关闭方式：ESC / 点遮罩 / 右上角 ✕。
// =====================================================================

import { computed, reactive, ref, watch, onUnmounted } from 'vue'
import { store, closePatchDrawer, resumeReview, toggleCheck, notify } from '../stores/app'
import { DIM_LABEL, ISSUE_CAT_LABEL, FIELD_LABEL, fmtVal, groupPatch } from '../utils/format'
import AppIcon from './AppIcon.vue'

const drawer = computed(() => store.drawer)
const payload = computed(() => store.drawer.payload)
const runId = computed(() => payload.value?.run_id || '')
const patch = computed(() => payload.value?.patch || [])
const groups = computed(() => groupPatch(patch.value))
const checked = computed(() => store.checked.get(runId.value) || new Set())
// 评审打分 + 一致性保障结果（后端 guard 的 LLM 审阅）
const review = computed(() => {
  const r = payload.value?.review
  if (!r) return null
  const issues = r.issues || []
  return { ...r, errors: issues.filter((i) => i.severity === 'error').length }
})

const feedback = ref('')
// 就地编辑：op 下标 -> 编辑后的字符串值
const edits = reactive({})

function resetEdits() { for (const k in edits) delete edits[k] }
watch(() => store.drawer.open, (open) => {
  if (open) {
    feedback.value = ''
    resetEdits()
    window.addEventListener('keydown', onKey)
  } else {
    window.removeEventListener('keydown', onKey)
  }
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
function onKey(e) { if (e.key === 'Escape' && store.drawer.open) closePatchDrawer() }

function opMeta(op) {
  return op === 'add' ? { label: '＋ 新增', cls: 'ok' }
    : op === 'remove' ? { label: '－ 删除', cls: 'bad' }
    : { label: '～ 修改', cls: 'warn' }
}
function sceneName(key) {
  const m = /^scene_(\d+)$/.exec(key)
  return m ? `场景 ${Number(m[1])}` : key
}

/** 某操作是否可就地编辑（仅字符串值的「修改」操作）。返回可编辑的当前值或 null。 */
function editableStr(op) {
  const v = op.after !== undefined ? op.after : op.value
  return typeof v === 'string' ? v : null
}

function buildEditedPatch(idxs) {
  if (!idxs.length) return null
  const out = []
  let changed = false
  for (const i of idxs) {
    const op = patch.value[i]
    if (!op) continue
    const cloned = JSON.parse(JSON.stringify(op))
    const edit = edits[i]
    const base = editableStr(op)
    if (edit !== undefined && base !== null && edit !== base) {
      cloned.value = edit
      if (cloned.after !== undefined) cloned.after = edit
      changed = true
    }
    out.push(cloned)
  }
  return changed ? out : null
}

function acceptSelected() {
  const idxs = [...checked.value]
  const edited = buildEditedPatch(idxs)
  if (edited) resumeReview(runId.value, 'edit', { patch: edited })
  else resumeReview(runId.value, 'accept', { patch_indexes: idxs })
}
function acceptAll() {
  const idxs = patch.value.map((_, i) => i)
  const edited = buildEditedPatch(idxs)
  if (edited) resumeReview(runId.value, 'edit', { patch: edited })
  else resumeReview(runId.value, 'accept', { patch_indexes: idxs })
}
function reject() { resumeReview(runId.value, 'reject', {}) }
function regenerate() { resumeReview(runId.value, 'regenerate', { feedback: feedback.value.trim() || '换个思路' }) }

// 一键重新生成反馈。
const REGEN_PRESETS = ['再口语一点', '节奏更紧凑', '加一点心理描写', '更催泪']
function regenerateWith(txt) { resumeReview(runId.value, 'regenerate', { feedback: txt }) }
</script>

<template>
  <!-- 点遮罩关闭；不用 Vue Transition -->
  <div v-if="drawer.open && payload" class="drawer-backdrop" @click.self="closePatchDrawer()">
    <aside class="drawer-card" role="dialog" aria-modal="true" aria-label="改编提议审阅">
      <header class="d-head">
        <div class="d-title">
          <h3><AppIcon name="edit" /> 改编提议（审阅）</h3>
          <div class="muted mono">run: {{ runId }}<template v-if="payload.error"> · <AppIcon name="warning" /> {{ payload.error }}</template></div>
        </div>
        <button class="x" aria-label="关闭" @click="closePatchDrawer()"><AppIcon name="close" /></button>
      </header>

      <div class="d-body">
        <!-- 评审打分 + 一致性保障 -->
        <section v-if="review" class="scene review-sec">
          <div class="scene-head">评审</div>
          <div class="rv-top">
            <span class="rv-score mono" :class="review.passed ? 'ok' : 'bad'">{{ review.overall_score }}<small>/100</small></span>
            <span class="rv-verdict" :class="review.passed ? 'ok' : 'bad'">{{ review.passed ? '通过' : '需调整' }}</span>
            <span v-if="review.errors" class="rv-err mono"><AppIcon name="warning" /> {{ review.errors }} 个 error</span>
          </div>
          <div v-if="review.dimensions && review.dimensions.length" class="rv-dims">
            <span v-for="d in review.dimensions" :key="d.name" class="rv-dim">
              <span class="rv-dim-n">{{ DIM_LABEL[d.name] || d.name }}</span>
              <span class="rv-dim-v mono">{{ d.score }}</span>
            </span>
          </div>
          <ul v-if="review.issues && review.issues.length" class="rv-issues">
            <li v-for="(it, i) in review.issues" :key="i" :class="it.severity">
              <span class="rv-cat">{{ ISSUE_CAT_LABEL[it.category] || it.category }}</span> {{ it.message }}
            </li>
          </ul>
          <p v-if="review.summary" class="rv-sum">{{ review.summary }}</p>
        </section>

        <section v-for="g in groups" :key="g.key" class="scene">
          <div class="scene-head">{{ sceneName(g.key) }}<span class="cnt">{{ g.items.length }} 处</span></div>
          <div v-for="{ op, idx } in g.items" :key="idx" class="op">
            <div class="op-head">
              <input type="checkbox" :checked="checked.has(idx)" @change="toggleCheck(runId, idx)" />
              <span class="pill" :class="opMeta(op.op).cls">{{ opMeta(op.op).label }}</span>
              <span class="pill">{{ FIELD_LABEL[op.field] || op.field || '' }}</span>
              <span v-if="op.beat_label" class="pill cue">{{ op.beat_label }}</span>
              <span v-for="(r, j) in op.risk || []" :key="j" class="pill warn">{{ r }}</span>
            </div>

            <div class="op-diff">
              <!-- 可编辑的「修改」：就地改新值 -->
              <template v-if="editableStr(op) !== null">
                <div v-if="op.before !== undefined" class="ln"><span class="old">{{ fmtVal(op.before) }}</span></div>
                <div class="edln">
                  <span class="edlbl">改为：</span>
                  <input class="inline-edit" :value="edits[idx] ?? editableStr(op)" @input="edits[idx] = $event.target.value" placeholder="编辑后的值" />
                </div>
              </template>
              <!-- 增/删或不可编辑：只读对比 -->
              <template v-else>
                <div v-if="op.before !== undefined"><span class="old">{{ fmtVal(op.before) }}</span></div>
                <div v-if="op.after !== undefined || op.value !== undefined">→ <span class="new">{{ fmtVal(op.after ?? op.value) }}</span></div>
              </template>
            </div>
          </div>
        </section>
        <div v-if="!groups.length" class="muted" style="padding: 16px">没有可落地的 patch 操作</div>
      </div>

      <footer class="d-foot">
        <div class="row fb">
          <input v-model="feedback" placeholder="重新生成的反馈（如：再口语一点）" style="flex: 1" />
          <button class="ghost" @click="regenerate">↻ 重新生成</button>
        </div>
        <div class="row chips">
          <span class="chip-lbl">快速反馈</span>
          <button v-for="pre in REGEN_PRESETS" :key="pre" class="mini chip" @click="regenerateWith(pre)">{{ pre }}</button>
        </div>
        <div class="row acts">
          <button class="danger" @click="reject">拒绝</button>
          <button @click="acceptSelected">接受勾选（{{ checked.size }}）</button>
          <button @click="acceptAll">接受全部</button>
        </div>
      </footer>
    </aside>
  </div>
</template>

<style scoped>
/* 外壳（遮罩 + 玻璃抽屉 + 滑入）走 style.css 的 .drawer-* 系列 */

.d-head {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 14px 16px 12px; border-bottom: 1px solid var(--line);
}
.d-title { flex: 1; min-width: 0; }
.d-title h3 { margin: 0 0 2px; font-size: 14px; }
.x {
  width: 28px; height: 28px; padding: 0; border-radius: var(--radius-sm); flex: none;
  background: transparent; border: 1px solid var(--line); color: var(--muted);
  display: inline-grid; place-items: center; font-size: 12px;
}
.x:hover { color: var(--ink); border-color: var(--line-strong); background: color-mix(in oklch, var(--ink) 6%, transparent); }

.d-body { flex: 1; overflow-y: auto; padding: 14px 16px; }
.scene { margin-bottom: 18px; }
.scene-head {
  font-size: 12.5px; font-weight: 700; color: var(--ink);
  padding-bottom: 6px; margin-bottom: 8px; border-bottom: 1px solid var(--line);
  display: flex; align-items: center; gap: 8px;
}
.scene-head .cnt { font-size: 11px; color: var(--dim); font-weight: 500; }

/* 评审打分 + 一致性保障 */
.review-sec { background: color-mix(in oklch, var(--panel2) 55%, transparent); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 9px 12px 10px; }
.review-sec .scene-head { border-bottom: 1px solid color-mix(in oklch, var(--ink) 9%, transparent); margin-bottom: 8px; }
.rv-top { display: flex; align-items: center; gap: 8px; }
.rv-score { font-size: 18px; font-weight: 700; }
.rv-score small { font-size: 11px; color: var(--muted); font-weight: 500; }
.rv-score.ok { color: var(--ok); }
.rv-score.bad { color: var(--bad); }
.rv-verdict { font-size: 12px; font-weight: 600; }
.rv-verdict.ok { color: var(--ok); }
.rv-verdict.bad { color: var(--bad); }
.rv-err { font-size: 11px; color: var(--bad); }
.rv-dims { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.rv-dim {
  display: inline-flex; align-items: center; gap: 5px;
  background: transparent; border: none; border-radius: 3px;
  padding: 2px 6px; font-size: 11px; color: var(--muted);
}
.rv-dim-n { color: var(--ink); }
.rv-dim-v { font-weight: 700; color: var(--ink); }
.rv-issues { margin: 8px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.rv-issues li { font-size: 11.5px; color: var(--muted); padding-left: 10px; position: relative; }
.rv-issues li::before { content: '·'; position: absolute; left: 0; color: var(--dim); }
.rv-issues li.error { color: var(--bad); }
.rv-issues li.warning { color: var(--warn); }
.rv-cat { font-weight: 600; }
.rv-sum { margin: 8px 0 0; font-size: 11.5px; color: var(--dim); }
.op {
  border: 1px solid var(--line); border-radius: var(--radius-md); padding: 8px 10px;
  margin-bottom: 6px; background: var(--code-bg);
  transition: border-color var(--dur) var(--ease);
}
.op:hover { border-color: var(--line-strong); }
.op-head { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.op-diff { margin-top: 6px; font-size: 12px; color: var(--muted); display: flex; flex-direction: column; gap: 5px; }
.op-diff .ln { max-width: 100%; }
.edln { display: flex; align-items: center; gap: 6px; }
.edlbl { color: var(--warn); font-size: 11px; flex: none; }
.inline-edit {
  flex: 1; min-width: 0; background: var(--panel2); border: 1px solid color-mix(in oklch, var(--warn) 45%, var(--line));
  border-radius: var(--radius-sm); color: var(--ink); padding: 4px 8px; font-size: 12.5px; font-family: inherit;
}
.inline-edit:focus { outline: none; border-color: color-mix(in oklch, var(--accent) 45%, var(--line)); }

.d-foot { border-top: 1px solid var(--line); padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; background: rgba(0, 0, 0, .3); }
.fb input { font-size: 12.5px; }
.chips { align-items: center; }
.chip-lbl { font-size: 11px; color: var(--dim); }
.mini.chip { color: var(--cue); }
.acts { justify-content: flex-end; }
</style>
