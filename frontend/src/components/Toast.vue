<script setup>
// =====================================================================
// Toast.vue —— 非阻塞的通知条（替代 alert / confirm 弹窗）
//
// 只在底部状态栏上方短暂出现，不打断当前操作，可点 × 关闭，几秒后自动消失。
// 类型：error(红) / ok(绿) / info(中性)。用 transform/opacity 做轻量入场，
// 不触发布局属性动画；尊重 prefers-reduced-motion。
// =====================================================================

import { store, dismissToast } from '../stores/app'
import AppIcon from './AppIcon.vue'

const TYPE_ICON = { error: 'warning', ok: 'check', info: 'info' }
</script>

<template>
  <div v-if="store.toast" class="toast" :class="store.toast.type || 'info'" role="status">
    <AppIcon :name="TYPE_ICON[store.toast.type] || 'info'" class="type-ic" />
    <span class="msg">{{ store.toast.message }}</span>
    <button class="close" :aria-label="store.toast.message" @click="dismissToast()"><AppIcon name="close" /></button>
  </div>
</template>

<style scoped>
.toast {
  position: fixed; top: 64px; left: 50%;
  transform: translateX(-50%);
  z-index: 90;
  display: flex; align-items: center; gap: 10px;
  min-width: 260px; max-width: 70vw;
  padding: 9px 12px;
  background: var(--panel2);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  box-shadow: 0 14px 40px oklch(0 0 0 / 0.4);
  font-size: 12.5px; color: var(--ink);
  animation: toast-in 180ms var(--ease);
}
/* 入场：只动透明度与位移（不是布局属性） */
@keyframes toast-in { from { opacity: 0; transform: translateX(-50%) translateY(-6px); } }

.type-ic { font-size: 14px; color: var(--muted); flex: none; }
.toast.error .type-ic { color: var(--bad); }
.toast.ok .type-ic { color: var(--ok); }
.toast.error { border-color: color-mix(in oklch, var(--bad) 45%, var(--line)); }
.toast.ok { border-color: color-mix(in oklch, var(--ok) 45%, var(--line)); }

.msg { flex: 1; min-width: 0; }
.close {
  display: inline-flex; align-items: center;
  background: transparent; border: none; color: var(--muted); cursor: pointer;
  font-size: 11px; line-height: 1; padding: 3px; border-radius: var(--radius-sm); flex: none;
}
.close:hover { color: var(--ink); background: color-mix(in oklch, var(--ink) 8%, transparent); }

@media (prefers-reduced-motion: reduce) {
  .toast { animation: none; }
}
</style>
