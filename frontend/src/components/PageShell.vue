<script setup>
// =====================================================================
// PageShell.vue —— 全屏页面外壳
//
// 资产库 / 技能 / 产出 都是「从对话点进去的一个页面」，不是一个常驻面板。
// 这个壳负责：顶栏（返回 + 标题 + 右侧动作区）+ 内容区。
// =====================================================================

import AppIcon from './AppIcon.vue'

defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
  backText: { type: String, default: '返回对话' },
})

const emit = defineEmits(['close'])
</script>

<template>
  <div class="ps">
    <header class="ps-head">
      <button class="ps-back" type="button" @click="emit('close')">
        <AppIcon name="arrow-left" /> {{ backText }}
      </button>
      <div class="ps-title-wrap">
        <h2 class="ps-title">{{ title }}</h2>
        <span v-if="subtitle" class="ps-sub">{{ subtitle }}</span>
      </div>
      <div class="ps-actions">
        <slot name="actions" />
      </div>
    </header>
    <div class="ps-body">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.ps {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  flex-direction: column;
  background: rgba(8, 8, 12, .94);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
}

.ps-head {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--line);
}

.ps-back {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 11px;
  border-radius: var(--radius-md);
  background: transparent;
  border: 1px solid var(--line);
  color: var(--muted);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
  transition: color 130ms var(--ease), background 130ms var(--ease);
}

.ps-back:hover { color: var(--ink); background: rgba(255, 255, 255, .07); }
.ps-back .app-ic { width: 14px; height: 14px; }

.ps-title-wrap { flex: 1; min-width: 0; display: flex; align-items: baseline; gap: 10px; }

.ps-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: .04em;
  color: var(--ink);
}

.ps-sub { font-size: 12px; color: var(--dim); }

.ps-actions { display: flex; align-items: center; gap: 8px; }

.ps-body {
  flex: 1;
  overflow-y: auto;
  padding: 22px 20px 56px;
}
</style>
