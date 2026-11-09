<script setup>
// =====================================================================
// ChatThread.vue —— 中间对话流
//
// 职责：
//   - 状态分流：首创态（无项目 + 无对话）渲染品牌开始页；
//     编辑态的空对话渲染轻量空态（项目上下文 + 示例），不再回到大开始页；
//   - 渲染消息列表；
//   - 智能滚动：用户停在底部时才跟随流式输出；向上滚动后出现
//     「回到最新」悬浮按钮；切换对话或发送消息时强制回底部。
// =====================================================================

import { ref, computed, watch, nextTick } from 'vue'
import MessageItem from './MessageItem.vue'
import StatusStrip from './StatusStrip.vue'
import WelcomeHero from './WelcomeHero.vue'
import { store, focusComposer } from '../stores/app'
import AppIcon from './AppIcon.vue'

const thread = ref(null)        // 滚动容器
const nearBottom = ref(true)    // 用户是否停在底部（80px 阈值）

// 首次创作态：还没有任何项目，也没有任何消息——只有这时才出现大开始页。
const isFirstRun = computed(() => !store.pid && !store.messages.length)

// 编辑态空对话：已有项目但当前对话为空——给轻量空态，不回大开始页。
const projectName = computed(() => store.projects.find((p) => p.id === store.pid)?.title || '')

// 编辑态示例：点击填入输入框（不自动发送）。
const SUGGESTIONS = [
  '生成初稿',
  '把当前剧本的对白改得更口语一点',
  '帮我把当前故事拆解成可生成的分镜',
]
function fillSuggestion(text) {
  store.draft = text
  store.draftSeq++
}

function onScroll() {
  const el = thread.value
  if (!el) return
  nearBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function toBottom() {
  nextTick(() => { const el = thread.value; if (el) el.scrollTop = el.scrollHeight })
}

function jumpToLatest() {
  nearBottom.value = true
  toBottom()
}

// 流式内容增长：仅当用户停在底部才跟随滚动
watch(() => store.messages.map((m) => m.content + m.events.length + m.payloads.length).join('|'),
  () => { if (nearBottom.value) toBottom() })

// 消息条数变化（切换对话 / 新发送）：视为用户主动，强制回底部
watch(() => store.messages.length, () => { nearBottom.value = true; toBottom() })
</script>

<template>
  <div class="thread-wrap">
    <div ref="thread" class="thread" @scroll.passive="onScroll">
      <div class="thread-inner">
        <!-- 最前面：这一轮 Agent 正在做什么（思考 / 调工具 / 写回复） -->
        <StatusStrip
          :phase="store.statusPhase"
          :active="store.statusActive"
          :elapsed="store.statusElapsed"
        />

        <!-- 首次创作态：品牌开始页 -->
        <WelcomeHero v-if="isFirstRun" />

        <!-- 编辑态空对话：轻量空态（项目上下文 + 下一步示例） -->
        <div v-else-if="!store.messages.length" class="editing-empty">
          <p class="ee-title">{{ projectName ? `《${projectName}》` : '新的对话' }}</p>
          <p class="ee-sub">从这里继续。可以从下面任选一步，也可以直接描述你的想法。</p>
          <div class="ee-suggestions" role="list" aria-label="下一步建议">
            <button
              v-for="s in SUGGESTIONS"
              :key="s"
              class="ee-chip"
              role="listitem"
              @click="fillSuggestion(s)"
            >{{ s }}</button>
          </div>
          <button class="ee-focus" @click="focusComposer()">输入我的想法</button>
        </div>

        <!-- 消息列表 -->
        <template v-else>
          <MessageItem v-for="(m, i) in store.messages" :key="i" :message="m" />
        </template>
      </div>
    </div>

    <!-- 向上翻阅历史时出现，点击回到最新消息 -->
    <button v-show="!nearBottom" class="jump ghost" @click="jumpToLatest"><AppIcon name="arrow-down" /> 回到最新</button>
  </div>
</template>

<style scoped>
.thread-wrap { flex: 1; min-height: 0; position: relative; display: flex; }
.thread { flex: 1; overflow-y: auto; padding: 28px clamp(var(--space-4), 4vw, 42px) 14px; }
.thread-inner { max-width: var(--thread-w); margin: 0 auto; }
.thread-inner { max-width: var(--thread-w); margin: 0 auto; }

/* 编辑态空对话：轻量、有上下文、有下一步 */
.editing-empty {
  padding: var(--space-7) var(--space-2) var(--space-6);
  text-align: center;
  display: flex; flex-direction: column; align-items: center; gap: var(--space-3);
}
.ee-title { margin: 0; font-size: var(--fs-title); font-weight: 700; color: var(--ink); }
.ee-sub { margin: 0; color: var(--muted); font-size: var(--fs-md); line-height: var(--lh-body); }
.ee-suggestions {
  display: flex; gap: var(--space-2); flex-wrap: wrap; justify-content: center;
  margin-top: var(--space-2);
}
.ee-chip {
  min-height: 34px;
  padding: 0 var(--space-4);
  background: rgba(255, 255, 255, .05);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  color: var(--muted);
  font-size: var(--fs-sm);
  font-weight: 400;
  transition: color var(--dur) var(--ease), border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.ee-chip:hover:not(:disabled) {
  color: var(--ink);
  border-color: var(--line-strong);
  background: rgba(255, 255, 255, .1);
}
.ee-focus {
  margin-top: var(--space-3); min-height: 42px; padding: 0 var(--space-6);
  border-radius: var(--radius-md); font-size: var(--fs-md); font-weight: 500;
  background: rgba(255, 255, 255, .07);
  border-color: var(--line-strong);
}
.ee-focus:hover:not(:disabled) { background: rgba(255, 255, 255, .13); }

/* 回到最新：悬浮在对话区底部中央 */
.jump {
  position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%);
  z-index: 5; background: var(--panel-solid); border: 1px solid var(--line-strong);
  backdrop-filter: var(--glass); -webkit-backdrop-filter: var(--glass);
  box-shadow: var(--shadow-2);
}
</style>
