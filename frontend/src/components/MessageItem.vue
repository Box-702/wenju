<script setup>
// =====================================================================
// MessageItem.vue —— 消息渲染（创新设计）
//
// 设计理念：从「聊天气泡」转型为「创作工作台的行内注释」。
// - 用户消息：简洁的指令条，像编辑器的命令输入
// - Agent 回复：通栏注释，带可折叠的工作步骤面板
// - 工具调用：折叠的「工作步骤」面板，每个步骤有图标+状态
// - 载荷卡片：审阅建议 / 版本通知 / 后台任务卡（剧组与流水线的调用过程）
// =====================================================================

import { computed, ref, watch, onUnmounted } from 'vue'
import PatchSummaryCard from './PatchSummaryCard.vue'
import VersionCard from './VersionCard.vue'
import TaskCard from './TaskCard.vue'
import { mdToHtml, esc } from '../utils/markdown'
import { STAGE_LABELS } from '../stores/app'

const props = defineProps({
  message: { type: Object, required: true },
})

const isUser = computed(() => props.message.role === 'user')
const bodyHtml = computed(() =>
  isUser.value ? esc(props.message.content || '') : mdToHtml(props.message.content || ''))

const showCopy = computed(() =>
  !isUser.value && props.message.content && !props.message.streaming && !props.message.payloads?.length)

// ---- 工作步骤面板（替代 chips）----
const stepsExpanded = ref(false)
const STEP_COLLAPSE = 3

// ---- 思考过程（推理模型 reasoning 增量，流式实时展示）----
const thinkingExpanded = ref(false)
const thinkingBody = ref(null)
// 流式期间自动滚到最新一行；结束后交还滚动位置给用户。
watch(() => props.message.thinking, () => {
  const el = thinkingBody.value
  if (el && props.message.streaming) el.scrollTop = el.scrollHeight
})

// 工具名 → 用户能看懂的创作动作（唯一来源在 stores/app.js 的 STAGE_LABELS，
// 与输入区的阶段状态条共用同一张表）；未登记的工具一律显示「正在处理」。

// meta 事件是「这一轮」的元信息（模型/耗时），不是工作步骤，渲染在消息头上
const stepEvents = computed(() => (props.message.events || []).filter((e) => e.type !== 'meta'))

const turnMeta = computed(() => {
  const m = (props.message.events || []).find((e) => e.type === 'meta')
  return m || null
})

// 消息头的小字写**厂商**（谁答的）——模型名在下拉里能看到，不必每条重复。
const metaLabel = computed(() => {
  const t = turnMeta.value
  if (!t) return ''
  const ms = Number(t.ms || 0)
  const dur = ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
  return `${t.provider || t.model || ''} · 处理 ${dur}`.trim()
})

const visibleSteps = computed(() => {
  const rows = mergedSteps.value
  if (rows.length <= STEP_COLLAPSE || stepsExpanded.value) return rows
  return rows.slice(-STEP_COLLAPSE)
})
const hiddenCount = computed(() => Math.max(0, mergedSteps.value.length - STEP_COLLAPSE))

// ---- 步骤行：一次工具调用 = 一行 ----
// 后端发的是 tool_call / tool_result 两条事件；这里配对成一行，
// 显示「中文动作 · 结果」，而不是两行英文。
const mergedSteps = computed(() => {
  const rows = []
  for (const e of stepEvents.value) {
    if (e.type === 'tool_call') {
      rows.push({
        name: e.name || '',
        label: STAGE_LABELS[e.name] || `执行 ${e.name || '工具'}`,
        detail: '',
        done: false,
      })
    } else if (e.type === 'step') {
      rows.push({
        name: e.name || '',
        label: e.label || STAGE_LABELS[e.name] || '处理中',
        detail: '',
        done: true,
      })
    } else if (e.type === 'tool_result') {
      // 配对最近一个还没完成的同名调用
      const hit = [...rows].reverse().find((r) => !r.done && r.name === (e.name || ''))
      if (hit) {
        hit.detail = briefResult(e.summary)
        hit.done = true
      } else {
        rows.push({
          name: e.name || '',
          label: STAGE_LABELS[e.name] || `执行 ${e.name || '工具'}`,
          detail: briefResult(e.summary),
          done: true,
        })
      }
    }
  }
  return rows
})

/** 结果摘要：工具返回是给模型看的长文本，给用户看的要短。

    不按句号截断——get_status 这类工具会返回多段状态（剧本/镜头/合成），
    砍掉后面几段反而看不懂。改成按长度截断，且尽量落在标点上。
*/
function briefResult(text) {
  const s = String(text || '').trim().replace(/\s+/g, ' ')
  if (!s) return ''
  if (s.length <= 52) return s
  const cut = s.slice(0, 52)
  const m = cut.match(/^(.*[，,、；;：:。])[^，,、；;：:。]*$/)
  return (m ? m[1] : cut) + '…'
}

// ---- 复制 ----
const copied = ref(false)
let copiedTimer = null
async function copyContent() {
  try {
    await navigator.clipboard.writeText(props.message.content || '')
    copied.value = true
    clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => (copied.value = false), 1400)
  } catch {}
}
onUnmounted(() => clearTimeout(copiedTimer))
</script>

<template>
  <div class="msg" :class="isUser ? 'user' : 'agent'">
    <!-- 用户：简洁指令条 -->
    <template v-if="isUser">
      <!-- 附带的图片：缩略图行，点击放大 -->
      <div v-if="(message.images || []).length" class="cmd-images">
        <a
          v-for="img in message.images"
          :key="img.id || img.url"
          class="cmd-image"
          :href="img.url"
          target="_blank"
          rel="noopener"
        >
          <img :src="img.url" :alt="img.name || '对话图片'" loading="lazy" />
        </a>
      </div>
      <div v-if="message.content" class="cmd-bar">
        <span class="cmd-text" v-html="bodyHtml"></span>
      </div>
    </template>

    <!-- Agent：行内注释风格 -->
    <template v-else>
      <!-- 思考过程：流式实时展示，结束后折叠可回看 -->
      <div v-if="message.thinking" class="thinking-panel">
        <div
          v-if="!message.streaming"
          class="thinking-header"
          @click="thinkingExpanded = !thinkingExpanded"
        >
          <span class="thinking-icon">✻</span>
          <span class="thinking-title">思考过程</span>
          <span class="steps-toggle">{{ thinkingExpanded ? '收起' : '展开' }}</span>
        </div>
        <div
          v-if="message.streaming || thinkingExpanded"
          class="thinking-body"
          :class="{ live: message.streaming }"
          ref="thinkingBody"
        >{{ message.thinking }}</div>
      </div>

      <!-- 工作步骤面板 -->
      <div v-if="stepEvents.length" class="steps-panel">
        <div class="steps-header" @click="stepsExpanded = !stepsExpanded">
          <span class="steps-icon">{{ message.streaming ? '· · ·' : '✓' }}</span>
          <span class="steps-title">
            {{ message.streaming ? '正在处理…' : `已完成 ${mergedSteps.length} 步` }}
          </span>
          <span class="steps-toggle">{{ stepsExpanded ? '收起' : '展开' }}</span>
        </div>
        <div v-if="stepsExpanded || stepEvents.length <= STEP_COLLAPSE" class="steps-body">
          <div v-if="hiddenCount > 0 && !stepsExpanded" class="steps-more" @click="stepsExpanded = true">
            还有 {{ hiddenCount }} 个步骤…
          </div>
          <div v-for="(r, i) in visibleSteps" :key="i" class="step-row" :class="{ done: r.done }">
            <span class="step-icon" aria-hidden="true">{{ r.done ? '✓' : '›' }}</span>
            <span class="step-label">{{ r.label }}</span>
            <span v-if="r.detail" class="step-detail">{{ r.detail }}</span>
            <span v-else-if="!r.done" class="step-detail">进行中</span>
          </div>
        </div>
      </div>

      <!-- 消息头：模型 · 耗时 · 复制 · 重试 -->
      <div v-if="metaLabel || showCopy" class="msg-meta">
        <span v-if="metaLabel" class="meta-info">{{ metaLabel }}</span>
        <span class="meta-spacer"></span>
        <button v-if="showCopy" class="meta-btn" @click="copyContent">{{ copied ? '✓ 已复制' : '⧉ 复制' }}</button>
      </div>

      <!-- 正文 -->
      <div v-if="message.content || !message.streaming" class="agent-body">
        <div class="content md" v-html="bodyHtml"></div>
      </div>
      <div v-else class="typing"><i></i><i></i><i></i></div>

      <!-- 载荷卡片 -->
      <template v-for="(p, i) in message.payloads" :key="i">
        <PatchSummaryCard v-if="p.type === 'patch_review'" :payload="p" />
        <VersionCard v-else-if="p.type === 'version_applied'" :payload="p" />
        <TaskCard v-else-if="p.type === 'sub_agent_started'" :payload="p" />
      </template>
    </template>
  </div>
</template>

<style scoped>
.msg { display: flex; flex-direction: column; width: 100%; margin-bottom: 16px; }
.msg.user { align-items: flex-end; }
.msg.agent { align-items: stretch; }

/* 用户指令条 */
.cmd-bar {
  display: inline-flex; align-items: baseline; gap: 8px;
  max-width: min(82%, 620px); padding: 10px 15px;
  background: rgba(255, 255, 255, .08);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg); border-bottom-right-radius: 5px;
  backdrop-filter: var(--glass); -webkit-backdrop-filter: var(--glass);
  box-shadow: var(--shadow-2);
}
.cmd-text { font-size: 15px; color: var(--ink); line-height: 1.7; font-weight: 400; white-space: pre-wrap; word-break: break-word; }

/* 用户消息附带的图片 */
.cmd-images { display: flex; gap: 6px; justify-content: flex-end; flex-wrap: wrap; margin-bottom: 6px; }
.cmd-image { display: block; border-radius: var(--radius-md); overflow: hidden; border: 1px solid var(--line); max-width: min(60%, 280px); }
.cmd-image img { display: block; max-height: 180px; width: auto; max-width: 100%; object-fit: cover; }

/* 工作步骤面板 */
/* 工具调用 / 系统步骤：一条时间线，小字，没有卡片包裹。
   目的是让用户看懂「这一步在做什么」，而不是看一堆英文工具名。 */
.steps-panel {
  margin-bottom: 10px;
  border-left: 1px solid var(--line);
  padding-left: 12px;
}
.steps-header {
  display: flex; align-items: center; gap: 8px; padding: 2px 0;
  cursor: pointer; user-select: none; font-size: 11.5px;
}
.steps-header:hover { color: var(--ink); }
.steps-icon { flex: none; }
.steps-title { flex: 1; color: var(--dim); font-weight: 400; }
.steps-toggle { flex: none; color: var(--dim); font-size: 11px; }
.steps-body { padding: 3px 0 2px; display: flex; flex-direction: column; gap: 4px; }
.steps-more {
  font-size: 11px; color: var(--dim); padding: 3px 0; cursor: pointer;
}
.steps-more:hover { color: var(--muted); }
.step-row {
  display: flex; align-items: baseline; gap: 7px;
  font-size: 11.5px; line-height: 1.6; color: var(--muted);
}
.step-icon { flex: none; width: 12px; text-align: center; color: var(--dim); }
.step-label { flex: none; color: var(--muted); }
.step-detail {
  flex: 1; min-width: 0; color: var(--dim);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.step-row.done .step-icon { color: var(--ok); }

/* 思考过程（推理模型 reasoning 实时流） */
.thinking-panel { margin-bottom: 8px; }
.thinking-header {
  display: inline-flex; align-items: center; gap: 8px; padding: 5px 10px;
  cursor: pointer; user-select: none; font-size: 12px;
  color: var(--dim); border-radius: var(--radius-sm);
}
.thinking-header:hover { background: color-mix(in oklch, var(--ink) 3%, transparent); }
.thinking-icon { flex: none; }
.thinking-title { color: var(--muted); }
.thinking-body {
  white-space: pre-wrap; word-break: break-word;
  font-size: 12.5px; line-height: 1.7; color: var(--dim);
  border-left: 2px solid var(--line);
  padding: 2px 0 2px 12px; margin-left: 2px;
}
.thinking-body.live { max-height: 150px; overflow-y: auto; }

/* Agent 正文 */
.agent-body { display: flex; flex-direction: column; min-width: 0; max-width: 100%; position: relative; }
/* 聊天正文：字号比一般 UI 大一档（阅读为主），行距宽松，字重用常规。
   太小的字在中文下会发糊，太粗的字压行——长段落尤其明显。 */
.content {
  display: block; min-width: 0; max-width: 74ch; word-break: break-word; text-align: left;
  font-size: 15.5px; line-height: 1.85; font-weight: 400; color: var(--ink);
}
/* 消息头：模型徽标 · 处理耗时 · 操作 */
.msg-meta {
  display: flex; align-items: center; gap: 10px;
  margin: 0 0 6px; min-height: 20px;
}
.meta-info {
  font-size: 11px; color: var(--dim); letter-spacing: .02em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.meta-spacer { flex: 1; }
.meta-btn {
  height: 22px; padding: 0 8px; border-radius: var(--radius-sm);
  background: transparent; border: 1px solid transparent;
  color: var(--dim); font-size: 11px; font-family: inherit; cursor: pointer;
  transition: color 130ms var(--ease), background 130ms var(--ease);
}
.meta-btn:hover {
  color: var(--ink); background: rgba(255, 255, 255, .07);
  border-color: var(--line);
}

.copy {
  display: block; margin-top: 2px; background: transparent; border: 1px solid transparent;
  color: var(--muted); font-size: 11px; padding: 2px 8px; border-radius: 4px;
  opacity: 0; pointer-events: none; width: fit-content;
}
.agent-body:hover .copy { opacity: 1; pointer-events: auto; }
.copy:hover { color: var(--ink); background: var(--panel2); border-color: var(--line); }
</style>
