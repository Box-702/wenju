<template>
  <div class="tc" :class="{ done: terminal }">
    <button class="tc-head" type="button" :aria-expanded="open" @click="open = !open">
      <span class="tc-icon" :class="stateClass" aria-hidden="true">
        <span v-if="state === 'running'" class="tc-spin"></span>
        <template v-else-if="state === 'done'">✓</template>
        <template v-else-if="state === 'failed'">!</template>
        <template v-else>·</template>
      </span>
      <span class="tc-name">{{ title }}</span>
      <span class="tc-state">{{ stateLabel }}</span>
      <span class="tc-toggle">{{ open ? '收起' : '详情' }}</span>
    </button>

    <div v-if="open" class="tc-body">
      <ol v-if="steps.length" class="tc-steps">
        <li v-for="(s, i) in steps" :key="i" class="tc-step" :data-st="s.status">
          <span class="ts-dot" aria-hidden="true"></span>
          <span class="ts-label">{{ s.label }}</span>
          <span v-if="s.detail" class="ts-detail">{{ s.detail }}</span>
        </li>
      </ol>
      <p v-else class="tc-step-empty">
        {{ state === 'running' ? '正在排队启动…' : '这一步没有留下过程记录。' }}
      </p>
      <p v-if="resultText" class="tc-result">{{ resultText }}</p>
    </div>
  </div>
</template>

<script setup>
// =====================================================================
// TaskCard.vue —— 对话里的后台任务卡（美术指导 / 定妆图 / 拆解 / 出片…）
//
// 为什么要有它：剧组任务与流水线都在后台跑，此前对话里只有一句
// 「正在生成…」，用户看不到调用过程，也不知道跑到哪了——
// 「调用的过程要展示给用户，任务中与完成后都能展开详情」。
//
// 数据：crew/production 工具在轮次里发 sub_agent_started 载荷（task_id），
// 卡片拿它轮询 GET /api/tasks/{id}（内存优先、落库兜底，重启后历史仍可展开）。
// 终态（done/failed）自动停止轮询；历史消息回放同样适用——一次拉取即定稿。
// =====================================================================
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'

const props = defineProps({
  payload: { type: Object, required: true },
})

const open = ref(false)
const task = ref(null)
let timer = null

const title = computed(() => props.payload.name || '后台任务')
const state = computed(() => {
  const st = task.value?.status
  if (!st || st === 'pending') return 'pending'
  if (st === 'running') return 'running'
  if (st === 'done') return 'done'
  return 'failed'
})
const terminal = computed(() => state.value === 'done' || state.value === 'failed')
const stateClass = computed(() => `st-${state.value}`)
const stateLabel = computed(() => ({
  pending: '排队中', running: '进行中', done: '已完成', failed: '未完成',
}[state.value]))
const steps = computed(() => task.value?.steps || [])
const resultText = computed(() => {
  if (state.value === 'done') return task.value?.result || ''
  if (state.value === 'failed') return task.value?.result || task.value?.error || ''
  return ''
})

async function poll() {
  const id = props.payload.task_id
  if (!id || id === 'local') { task.value = null; return }
  try {
    const t = await api(`/tasks/${id}`)
    task.value = t
    if (t.status === 'done' || t.status === 'failed') {
      if (timer) { clearInterval(timer); timer = null }
    }
  } catch {
    // 任务查不到（进程重启且无快照）：停在当前显示，不打扰对话
    if (timer) { clearInterval(timer); timer = null }
  }
}

onMounted(() => {
  poll()
  timer = setInterval(poll, 2500)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.tc {
  margin: 6px 0; border: 1px solid var(--line); border-radius: var(--radius-md);
  background: rgba(255, 255, 255, .03); overflow: hidden;
}
.tc-head {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 8px 11px; background: transparent; border: 0;
  color: var(--muted); font: inherit; font-size: 12.5px; text-align: left; cursor: pointer;
}
.tc-head:hover { background: rgba(255, 255, 255, .05); color: var(--ink); }
.tc-icon {
  flex: none; width: 16px; height: 16px; display: grid; place-items: center;
  border-radius: 50%; font-size: 10px; line-height: 1;
  border: 1px solid var(--line-strong); color: var(--dim);
}
.tc-icon.st-done { border-color: var(--ok); color: var(--ok); }
.tc-icon.st-failed { border-color: var(--bad); color: var(--bad); }
.tc-icon.st-running { border-color: var(--accent); }
.tc-spin {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); animation: tc-pulse 1.1s ease-in-out infinite;
}
@keyframes tc-pulse { 0%, 100% { opacity: .35; transform: scale(.8); } 50% { opacity: 1; transform: scale(1); } }
.tc-name { flex: 1; min-width: 0; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-state { flex: none; color: var(--dim); font-size: 11px; }
.tc-toggle { flex: none; color: var(--dim); font-size: 11px; }

.tc-body { padding: 2px 11px 9px; border-top: 1px dashed var(--line); }
.tc-steps { list-style: none; margin: 6px 0 0; padding: 0; }
.tc-step {
  position: relative; display: flex; align-items: baseline; gap: 8px;
  padding: 3px 0 3px 14px; font-size: 11.5px; color: var(--muted);
}
.tc-step::before {
  content: ''; position: absolute; left: 3.5px; top: 0; bottom: 0;
  width: 1px; background: var(--line);
}
.tc-step:last-child::before { bottom: auto; height: 50%; }
.ts-dot {
  position: absolute; left: 0; top: 8px;
  width: 8px; height: 8px; border-radius: 50%;
  border: 1px solid var(--line-strong); background: var(--panel-solid);
}
.tc-step[data-st='done'] .ts-dot { border-color: var(--ok); background: var(--ok); }
.tc-step[data-st='running'] .ts-dot { border-color: var(--accent); background: var(--accent); }
.ts-label { flex: none; max-width: 62%; }
.ts-detail {
  min-width: 0; color: var(--dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tc-step-empty { margin: 7px 0 0; font-size: 11.5px; color: var(--dim); }
.tc-result {
  margin: 7px 0 0; padding: 6px 8px; border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, .04); font-size: 11.5px; line-height: 1.55;
  color: var(--muted); white-space: pre-wrap;
}
</style>
