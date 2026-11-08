<template>
  <div class="workspace-tree">
    <div class="ws-header">
      <div>
        <span class="ws-eyebrow">FILES</span>
        <span class="ws-title">工作区</span>
      </div>
      <button class="btn-tiny" @click="onRefresh" title="刷新" aria-label="刷新">↻</button>
    </div>

    <div v-if="!tree" class="ws-empty">
      <p>未配置工作目录</p>
      <button class="btn-small" @click="$emit('open-workspace')">设置工作目录</button>
    </div>

    <div v-else class="ws-content">
      <div class="ws-root">{{ tree.root }}</div>
      <div v-for="folder in tree.folders" :key="folder.name" class="ws-folder">
        <div class="folder-header" @click="toggle(folder.name)">
          <span class="expand-icon" :class="{ open: isOpen(folder.name) }"><AppIcon name="chevron-right" /></span>
          <span class="folder-icon"><AppIcon name="folder" /></span>
          <span class="folder-name">{{ folder.name }}</span>
        </div>
        <div v-if="isOpen(folder.name)" class="folder-children">
          <div
            v-for="sub in (folder.folders || [])"
            :key="sub.name"
            class="folder-item"
          >
            <span class="item-icon"><AppIcon :name="sub.type === 'dir' ? 'folder' : 'file'" /></span>
            <span class="item-name">{{ sub.name }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive } from 'vue'
import { store, loadWorkspaceTree } from '../stores/app.js'
import AppIcon from './AppIcon.vue'

const tree = computed(() => store.workspaceTree)
const openFolders = reactive(new Set())

function isOpen(name) { return openFolders.has(name) }
function toggle(name) {
  if (openFolders.has(name)) openFolders.delete(name)
  else openFolders.add(name)
}

function onRefresh() {
  loadWorkspaceTree()
}
</script>

<style scoped>
.workspace-tree { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.ws-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 14px; border-bottom: 1px solid var(--line);
}
.ws-header > div { display: flex; flex-direction: column; gap: 4px; }
.ws-eyebrow { color: var(--cyan); font: 700 9px/1 var(--mono); letter-spacing: .14em; }
.ws-title { font-size: 13px; font-weight: 650; color: var(--ink); }
.btn-tiny {
  background: transparent; border: 1px solid var(--line); color: var(--muted); border-radius: var(--radius-sm);
  cursor: pointer; font-size: 15px; padding: 0; width: 25px; height: 25px;
  opacity: .8; display: grid; place-items: center;
}
.btn-tiny:hover { opacity: 1; }

.ws-empty { padding: 20px 12px; text-align: center; color: var(--text2, #666); font-size: 12px; }
.btn-small {
  margin-top: 8px; padding: 5px 12px; border: 1px solid var(--border, #333);
  border-radius: var(--radius-sm); background: none; color: var(--text, #eee); cursor: pointer; font-size: 11px;
}
.btn-small:hover { border-color: var(--gold); color: var(--ink); background: color-mix(in oklch, var(--gold) 6%, transparent); }

.ws-content { flex: 1; overflow-y: auto; padding: 4px 0; }
.ws-root { padding: 4px 12px; font-size: 10px; color: var(--text2, #555); word-break: break-all; }

.ws-folder { }
.folder-header {
  display: flex; align-items: center; gap: 5px; padding: 5px 12px;
  cursor: pointer; font-size: 12px; color: var(--muted);
}
.folder-header:hover { background: rgba(255,255,255,.03); }
.expand-icon { display: inline-flex; font-size: 10px; color: var(--cyan); transition: transform 130ms var(--ease); }
.expand-icon.open { transform: rotate(90deg); }
.folder-icon { display: inline-flex; font-size: 13px; color: var(--cyan); }
.folder-name { flex: 1; }
.folder-children { padding-left: 12px; }
.folder-item {
  display: flex; align-items: center; gap: 5px; padding: 3px 12px;
  font-size: 11px; color: var(--dim);
}
.item-icon { width: 14px; display: inline-flex; color: var(--dim); font-size: 12px; }
</style>
