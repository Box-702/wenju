<script setup>
// =====================================================================
// StatusStrip.vue —— 对话最上方的「正在做什么」状态条
//
// 为什么在最前面：Agent 干活时，用户第一眼要看到的是「它现在在干什么」，
// 而不是去消息里翻工具轨迹。这条贴在对话流顶部，一眼可见。
//
// 动效克制：一个呼吸的光点 + 一道扫过的高光，不做转圈菊花。
// =====================================================================
import { computed } from 'vue'

const props = defineProps({
  /** 当前阶段文案（空 = 空闲） */
  phase: { type: String, default: '' },
  /** 正在工作（true）还是刚结束（false，只闪一下再收） */
  active: { type: Boolean, default: false },
  /** 已用时长（毫秒） */
  elapsed: { type: Number, default: 0 },
})

const label = computed(() => props.phase || '正在思考')
const secs = computed(() => (props.elapsed / 1000).toFixed(1))
</script>

<template>
  <div v-if="phase || active" class="status-strip" :class="{ live: active }">
    <span class="ss-dot" aria-hidden="true"></span>
    <span class="ss-label">{{ label }}</span>
    <span v-if="active" class="ss-time">{{ secs }}s</span>
  </div>
</template>

<style scoped>
/* 状态条：贴在对话流顶部，轻、稳、不抢注意力 */
.status-strip {
  position: sticky;
  top: 0;
  z-index: 6;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  align-self: flex-start;
  margin: 2px auto 10px;
  padding: 6px 14px 6px 11px;
  border-radius: var(--radius-full);
  border: 1px solid var(--line);
  background: var(--surface-2);
  color: var(--muted);
  font-size: 12.5px;
  box-shadow: var(--shadow-1);
  animation: ss-in 220ms var(--ease) both;
}

/* 呼吸的光点：亮一下、灭一下，像在「想」 */
.ss-dot {
  position: relative;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 0 color-mix(in oklch, var(--accent) 55%, transparent);
  animation: ss-breathe 1.6s var(--ease) infinite;
  flex: none;
}

/* 扫过的高光：文字**常驻实色**（--ink），一道更亮的白光带循环掠过——
   旧版把底色做成 transparent、文字全靠光带照亮，光带不在时几乎看不见（用户指正）。 */
.ss-label {
  position: relative;
  background-image: linear-gradient(
    100deg,
    var(--ink) 40%,
    #fff 50%,
    var(--ink) 60%
  );
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: ss-sweep 2.4s linear infinite;
}

.ss-time {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

@keyframes ss-breathe {
  0%, 100% { opacity: .55; box-shadow: 0 0 0 0 color-mix(in oklch, var(--accent) 40%, transparent); }
  50% { opacity: 1; box-shadow: 0 0 0 5px color-mix(in oklch, var(--accent) 0%, transparent); }
}
@keyframes ss-sweep {
  0% { background-position: 130% 0; }
  100% { background-position: -30% 0; }
}
@keyframes ss-in {
  from { opacity: 0; transform: translateY(-6px); }
  to { opacity: 1; transform: none; }
}
</style>
