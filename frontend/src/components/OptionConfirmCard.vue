<script setup>
// =====================================================================
// OptionConfirmCard.vue —— 流程分叉确认卡（D-76，对标即梦「确认方向」卡）
//
// Agent 把下一步的分叉（改编重点 / 生成范围 / 音色方案 / 模型画幅）整理成
// radio 选项卡，用户点选 + 确认才推进——「每一项都是不会直接去出片的」。
// 机制与 D-43 批准卡同型：卡片只是界面，落账走 /chat 的确定性路径
// （meta.intent=confirm_options），模型不能替用户选。
// 就地终结：确认后 payload.status 置 confirmed，刷新页面由后端按 DB 回填。
// =====================================================================
import { computed, reactive, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { store, resolveOptions } from '../stores/app'

const props = defineProps({
  payload: { type: Object, required: true }, // { type, choice_id, title, questions, status }
})

const questions = computed(() => props.payload.questions || [])
const pending = computed(() => (props.payload.status || 'pending') === 'pending')

// 每题一个 Set(选项 id)：radio 题点选即替换，multi 题勾选增删
const picked = reactive(new Map())
function isSelected(qi, optId) {
  return (picked.get(qi) || new Set()).has(optId)
}
function pick(qi, q, optId) {
  if (!pending.value) return
  const cur = new Set(picked.get(qi) || [])
  if (q.multi) {
    cur.has(optId) ? cur.delete(optId) : cur.add(optId)
  } else {
    cur.clear()
    cur.add(optId)
  }
  picked.set(qi, cur)
}
// 单选题必须选完才允许确认（服务端同样 fail-closed 校验）
const allAnswered = computed(() =>
  questions.value.every((q, qi) => q.multi || (picked.get(qi)?.size || 0) > 0))

const note = ref('')
const busy = ref(false)
async function confirm() {
  if (!pending.value || busy.value || !allAnswered.value) return
  busy.value = true
  try {
    const choices = questions.value.map((q, qi) => ({
      question: q.question,
      option_ids: [...(picked.get(qi) || [])],
    }))
    await resolveOptions(props.payload.choice_id, choices, note.value.trim())
    props.payload.status = 'confirmed'
    props.payload.resolved = choices
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="opt-card" :class="{ done: !pending }">
    <div class="oc-head">
      <span class="oc-title">确认方向 · {{ payload.title || '下一步' }}</span>
      <span class="oc-badge" :class="pending ? 'wait' : 'ok'">
        <AppIcon :name="pending ? 'info' : 'check'" />
        {{ pending ? '待你拍板' : '已确认' }}
      </span>
    </div>

    <div v-for="(q, qi) in questions" :key="qi" class="oc-q">
      <p class="oc-qt">
        {{ qi + 1 }}. {{ q.question }}
        <em v-if="q.multi" class="oc-multi">可多选</em>
      </p>
      <div class="oc-opts">
        <button
          v-for="o in q.options" :key="o.id"
          class="oc-opt" :class="{ on: isSelected(qi, o.id) }"
          type="button" :disabled="!pending"
          @click="pick(qi, q, o.id)"
        >
          <span class="oc-radio" aria-hidden="true" />
          <span class="oc-body">
            <span class="oc-label">{{ o.label }}<em v-if="o.recommended" class="oc-rec">推荐</em></span>
            <span v-if="o.desc" class="oc-desc">{{ o.desc }}</span>
          </span>
        </button>
      </div>
    </div>

    <textarea
      v-if="pending" v-model="note" class="oc-note" rows="2"
      placeholder="补充说明（可不填）——比如「第一集优先，其余先出样片」"
    />

    <div class="oc-foot">
      <button
        v-if="pending" class="small primary" type="button"
        :disabled="!allAnswered || busy || !store.pid"
        :title="store.pid ? (allAnswered ? '提交后 Agent 按你的选择推进' : '还有单选题没选') : '先进入作品再拍板'"
        @click="confirm"
      >
        <AppIcon name="check" /> {{ busy ? '提交中…' : '确认' }}
      </button>
      <span v-else class="oc-done">已按你的选择推进</span>
      <span v-if="pending" class="oc-hint">拍板前 Agent 不会继续推进</span>
    </div>
  </div>
</template>

<style scoped>
.opt-card {
  margin: 10px 0 4px;
  border: 1px solid var(--sel-line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  padding: 12px 14px;
}
.opt-card.done { opacity: .72; }

.oc-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.oc-title { font-size: 13px; font-weight: 600; color: var(--ink); flex: 1; }
.oc-badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; padding: 2px 8px; border-radius: var(--radius-full);
  border: 1px solid var(--warn); color: var(--warn); flex: none;
}
.oc-badge.ok { border-color: var(--line-strong); color: var(--muted); }
.oc-badge :deep(svg) { width: 11px; height: 11px; }

.oc-q { margin-bottom: 10px; }
.oc-qt { margin: 0 0 6px; font-size: 12.5px; color: var(--ink); }
.oc-multi {
  font-style: normal; font-size: 10.5px; color: var(--dim);
  border: 1px solid var(--line); border-radius: 3px; padding: 0 5px; margin-left: 6px;
}
.oc-opts { display: flex; flex-direction: column; gap: 5px; }
.oc-opt {
  display: flex; align-items: flex-start; gap: 9px; width: 100%;
  padding: 8px 10px; text-align: left;
  border: none; border-radius: var(--radius-md);
  background: transparent; cursor: pointer;
  transition: background 130ms var(--ease);
}
.oc-opt:hover:not(:disabled) { background: rgba(255, 255, 255, .06); }
.oc-opt:disabled { cursor: default; }
.oc-opt.on {
  background: var(--sel-bg);
  box-shadow: inset 2px 0 0 0 var(--sel-bar);
}
.oc-radio {
  flex: none; width: 13px; height: 13px; margin-top: 2px;
  border-radius: 50%; border: 1.5px solid var(--line-strong);
  display: inline-block;
}
.oc-opt.on .oc-radio { border-color: var(--sel-bar); background: var(--sel-bar); }
.oc-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.oc-label { font-size: 12.5px; color: var(--ink); display: flex; align-items: center; gap: 6px; }
.oc-rec {
  font-style: normal; font-size: 10px; color: var(--ok);
  border: 1px solid var(--line); border-radius: 3px; padding: 0 5px;
}
.oc-desc { font-size: 11.5px; color: var(--dim); line-height: 1.5; }

.oc-note {
  width: 100%; margin-top: 2px; padding: 7px 9px;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--surface-2); color: var(--ink);
  font: inherit; font-size: 12px; resize: vertical;
}
.oc-note:focus { outline: none; border-color: var(--line-strong); }

.oc-foot { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.oc-done { font-size: 12px; color: var(--muted); }
.oc-hint { font-size: 11px; color: var(--dim); }
</style>
