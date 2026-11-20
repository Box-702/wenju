<script setup>
// =====================================================================
// AgentPanel.vue —— 右侧常驻 Agent 对话面板（即梦式布局的「短剧 Agent」列）
//
// 对话不降级：还是那个能调 42 个工具的主 Agent，只是位置从主栏搬到右栏。
// 会话功能全部自持在卡片右上角（VSCode agent 插件式）：作品切换、新会话、
// 会话列表、设置、收起——不再借外部顶栏。
// 选中态统一语言：左侧圆角金色竖条 + 极淡金底 + 图标转金（不用硬 inset 边）。
// =====================================================================
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import ChatThread from '../ChatThread.vue'
import ChatComposer from '../ChatComposer.vue'
import AppIcon from '../AppIcon.vue'
import {
  store, createConversation, selectConversation,
  loadConversations, toggleAgentPanel,
} from '../../stores/app'

const projectName = computed(() => {
  const p = store.projects.find(x => x.id === store.pid)
  return p?.title || ''
})

const conversations = computed(() => store.convMap[store.pid] || [])
const currentConv = computed(
  () => conversations.value.find(c => c.id === store.convId) || null,
)

// ---- 会话列表浮层 ----
const listOpen = ref(false)
const listBtn = ref(null)
const listEl = ref(null)
async function toggleList() {
  listOpen.value = !listOpen.value
  if (listOpen.value && store.pid && !conversations.value.length) {
    await loadConversations(store.pid)
  }
}
function pickConv(id) {
  listOpen.value = false
  if (id && id !== store.convId) selectConversation(store.pid, id)
}
async function newConv() {
  listOpen.value = false
  await createConversation(store.pid)
}
function onDocClick(e) {
  if (!listOpen.value) return
  if (listBtn.value?.contains(e.target) || listEl.value?.contains(e.target)) return
  listOpen.value = false
}
watch(listOpen, async (v) => {
  await nextTick()
  if (v) document.addEventListener('click', onDocClick, true)
  else document.removeEventListener('click', onDocClick, true)
})
onBeforeUnmount(() => document.removeEventListener('click', onDocClick, true))

function fmtRel(iso) {
  if (!iso) return ''
  // 后端存的是 UTC 但 isoformat 不带时区后缀——裸解析会按本地时间算，
  // 「刚刚」会显示成「8 小时前」。无 Z/偏移时补 Z。
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + 'Z'
  const d = new Date(normalized)
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}
</script>

<template>
  <aside class="agent-panel" aria-label="短剧 Agent 对话">
    <div class="ap-head">
      <button class="ap-project" :title="projectName || '选择作品'"
        @click="store.overlay = 'tasks'">
        <span class="ap-dot" aria-hidden="true" />
        <span class="ap-name">{{ projectName || '未选作品' }}</span>
        <AppIcon name="chevron-down" />
      </button>

      <div class="ap-tools">
        <button class="ap-btn" title="新建会话" @click="newConv">
          <AppIcon name="plus" />
        </button>
        <div class="ap-conv-wrap">
          <button
            ref="listBtn" class="ap-btn ap-conv-btn" :class="{ on: listOpen }"
            title="会话列表" @click="toggleList"
          >
            <AppIcon name="menu" />
            <span class="ap-conv-cur">{{ currentConv?.title || '会话' }}</span>
          </button>
          <Transition name="pop">
            <div v-if="listOpen" ref="listEl" class="ap-pop" role="menu">
              <p class="ap-pop-title">会话 · {{ conversations.length }}</p>
              <div class="ap-pop-list">
                <button
                  v-for="c in conversations" :key="c.id"
                  class="ap-item" :class="{ on: c.id === store.convId }"
                  role="menuitem" @click="pickConv(c.id)"
                >
                  <span class="ap-item-name">{{ c.title || '新对话' }}</span>
                  <span class="ap-item-time">{{ fmtRel(c.updated_at) }}</span>
                </button>
                <p v-if="!conversations.length" class="ap-pop-empty">还没有会话</p>
              </div>
              <button class="ap-pop-new" @click="newConv">
                <AppIcon name="plus" /> 新建会话
              </button>
            </div>
          </Transition>
        </div>
        <button class="ap-btn" title="设置" @click="store.showSettings = true">
          <AppIcon name="gear" />
        </button>
        <button class="ap-btn" title="收起面板" @click="toggleAgentPanel()">
          <AppIcon name="close" />
        </button>
      </div>
    </div>

    <div class="ap-body">
      <ChatThread />
      <ChatComposer @open-skills="store.overlay = 'skills'" />
    </div>
  </aside>
</template>

<style scoped>
.agent-panel {
  display: flex; flex-direction: column;
  height: 100%; min-width: 0; min-height: 0;
  background: var(--bg);
  border-left: 1px solid var(--line);
}
.ap-head {
  position: relative; z-index: 20;
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 7px 8px 7px 10px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  flex: none;
}
.ap-project {
  display: inline-flex; align-items: center; gap: 6px;
  min-width: 0; padding: 4px 8px; border-radius: var(--radius-sm);
  border: 1px solid transparent; background: transparent;
  color: var(--ink); font-size: 12.5px; font-weight: 600; cursor: pointer;
}
.ap-project:hover { border-color: var(--line); background: var(--surface-2); }
.ap-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--gold); flex: none; }
.ap-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 120px; }
.ap-project :deep(svg) { width: 11px; height: 11px; color: var(--muted); flex: none; }

.ap-tools { display: flex; align-items: center; gap: 2px; flex: none; }
.ap-btn {
  display: inline-flex; align-items: center; gap: 5px;
  height: 28px; padding: 0 7px; border-radius: var(--radius-sm);
  border: 1px solid transparent; background: transparent;
  color: var(--muted); font-size: 12px; cursor: pointer;
  transition: color 130ms var(--ease), background 130ms var(--ease), border-color 130ms var(--ease);
}
.ap-btn :deep(svg) { width: 14px; height: 14px; flex: none; }
.ap-btn:hover { color: var(--ink); background: var(--surface-2); }
/* 选中态语言：淡金底 + 图标转金（不用硬边） */
.ap-btn.on { color: var(--ink); background: color-mix(in oklch, var(--gold) 10%, transparent); }
.ap-btn.on :deep(svg) { color: var(--gold); }
.ap-conv-wrap { position: relative; }
.ap-conv-cur {
  max-width: 84px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 会话浮层 */
.ap-pop {
  position: absolute; top: calc(100% + 6px); right: 0; z-index: 40;
  width: 240px; display: flex; flex-direction: column;
  border: 1px solid var(--line-strong); border-radius: var(--radius-md);
  background: var(--surface); box-shadow: var(--shadow-3); overflow: hidden;
}
.ap-pop-title {
  margin: 0; padding: 8px 12px; font-size: 11px; letter-spacing: .04em;
  color: var(--dim); border-bottom: 1px solid var(--line);
  font-family: var(--mono);
}
.ap-pop-list { max-height: 260px; overflow-y: auto; padding: 4px; }
.ap-item {
  position: relative; display: flex; align-items: center; gap: 8px;
  width: 100%; padding: 7px 10px 7px 12px; text-align: left;
  border: none; border-radius: var(--radius-sm);
  background: transparent; color: var(--muted); font-size: 12.5px; cursor: pointer;
}
.ap-item:hover { background: var(--surface-2); color: var(--ink); }
/* 选中会话：左侧圆角金色竖条 + 淡金底 */
.ap-item.on {
  color: var(--ink);
  background: color-mix(in oklch, var(--gold) 9%, transparent);
}
.ap-item.on::before {
  content: ''; position: absolute; left: 3px; top: 6px; bottom: 6px;
  width: 3px; border-radius: 2px; background: var(--gold);
  box-shadow: 0 0 6px color-mix(in oklch, var(--gold) 55%, transparent);
}
.ap-item-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ap-item-time { flex: none; font-size: 10.5px; color: var(--dim); }
.ap-pop-empty { margin: 8px; font-size: 11.5px; color: var(--dim); text-align: center; }
.ap-pop-new {
  display: flex; align-items: center; gap: 6px;
  padding: 8px 12px; border: none; border-top: 1px solid var(--line);
  background: transparent; color: var(--gold); font-size: 12px; cursor: pointer;
}
.ap-pop-new:hover { background: var(--surface-2); }
.ap-pop-new :deep(svg) { width: 12px; height: 12px; }

.pop-enter-active, .pop-leave-active { transition: opacity 140ms var(--ease), transform 140ms var(--ease); }
.pop-enter-from, .pop-leave-to { opacity: 0; transform: translateY(-4px); }

.ap-body {
  flex: 1; min-height: 0;
  display: flex; flex-direction: column;
}
/* 面板比主栏窄：对话列不再居中限宽，贴边排布；内边距收紧 */
.ap-body :deep(.thread) { padding: 16px 12px 8px; }
.ap-body :deep(.thread-inner) { max-width: none; }
.ap-body :deep(.composer) { padding: 8px 12px 14px; }
.ap-body :deep(.composer > :deep(.cc)) { max-width: none; }
</style>
