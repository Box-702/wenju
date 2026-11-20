<script setup>
// =====================================================================
// AgentPanel.vue —— 右侧常驻 Agent 对话面板（即梦式布局的「短剧 Agent」列）
//
// 对话不降级：还是那个能调 42 个工具的主 Agent，只是位置从主栏搬到右栏。
// 顶部一行：作品名 + 会话切换 + 收起按钮；主体复用 ChatThread / ChatComposer。
// =====================================================================
import { computed } from 'vue'
import ChatThread from '../ChatThread.vue'
import ChatComposer from '../ChatComposer.vue'
import AppIcon from '../AppIcon.vue'
import { store, selectConversation, toggleAgentPanel } from '../../stores/app'

const projectName = computed(() => {
  const p = store.projects.find(x => x.id === store.pid)
  return p?.title || ''
})

const conversations = computed(() => store.convMap[store.pid] || [])

function switchConv(id) {
  if (id && id !== store.convId) selectConversation(store.pid, id)
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
      <div class="ap-actions">
        <select
          v-if="conversations.length > 1"
          class="ap-conv"
          :value="store.convId || ''"
          title="切换会话"
          @change="switchConv($event.target.value)"
        >
          <option v-for="(c, i) in conversations" :key="c.id" :value="c.id">
            会话 {{ conversations.length - i }}
          </option>
        </select>
        <button class="ap-close ghost small" title="收起对话面板" @click="toggleAgentPanel()">
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
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 8px 10px;
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
.ap-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 140px; }
.ap-project :deep(svg) { width: 11px; height: 11px; color: var(--muted); flex: none; }
.ap-actions { display: flex; align-items: center; gap: 6px; flex: none; }
.ap-conv {
  height: 26px; padding: 0 6px; border-radius: var(--radius-sm);
  border: 1px solid var(--line); background: var(--surface-2);
  color: var(--muted); font-size: 11.5px;
}
.ap-close { padding: 0 6px; height: 26px; }
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
