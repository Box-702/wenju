<template>
  <div class="video-player" v-if="url">
    <div class="player-header">
      <span class="player-title">{{ title || '视频预览' }}</span>
      <button class="vp-close" type="button" aria-label="关闭预览" @click="$emit('close')"><AppIcon name="close" /></button>
    </div>
    <video
      ref="videoEl"
      :src="url"
      controls
      autoplay
      class="video-element"
      @ended="$emit('ended')"
      @error="onError"
    />
    <div v-if="error" class="player-error">{{ error }}</div>
    <div class="player-meta" v-if="meta">
      <span v-if="meta.duration">时长: {{ meta.duration.toFixed(1) }}s</span>
      <span v-if="meta.provider">来源: {{ meta.provider }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import AppIcon from '../AppIcon.vue'

const props = defineProps({
  url: { type: String, default: '' },
  title: { type: String, default: '' },
  meta: { type: Object, default: null },
})

const emit = defineEmits(['close', 'ended'])

const videoEl = ref(null)
const error = ref('')

function onError(e) {
  error.value = '视频加载失败'
}

watch(() => props.url, () => { error.value = '' })
</script>

<style scoped>
.video-player {
  background: var(--panel, #1a1a2e); border: 1px solid var(--border, #333);
  border-radius: var(--radius-md); overflow: hidden;
}
.player-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 10px 14px; border-bottom: 1px solid var(--border, #222);
}
.player-title { font-size: 13px; font-weight: 600; }
/* 关闭键：视觉 28px，命中区靠 ::after 外扩到 ≥40px（对齐 --tap 规范）；
   以前用 .small.ghost 只有 30x23，用户反馈「关闭按钮有点问题」= 按不中。 */
.vp-close {
  position: relative; flex: none;
  width: 28px; height: 28px; display: inline-grid; place-items: center;
  background: transparent; border: 1px solid var(--border, #333);
  border-radius: var(--radius-md); color: var(--muted, #999); cursor: pointer;
  transition: color 130ms ease, background 130ms ease, border-color 130ms ease;
}
.vp-close::after { content: ''; position: absolute; inset: -7px; }
.vp-close:hover { color: var(--ink, #eee); background: var(--panel2, #262637); border-color: var(--line-strong, #555); }
.video-element { width: 100%; display: block; max-height: 400px; background: #000; }
.player-error { padding: 8px 14px; font-size: 12px; color: var(--red, #f87171); }
.player-meta {
  display: flex; gap: 16px; padding: 8px 14px; font-size: 11px; color: var(--text2, #666);
}
</style>
