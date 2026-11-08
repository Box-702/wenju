<template>
  <div class="video-player" v-if="url">
    <div class="player-header">
      <span class="player-title">{{ title || '视频预览' }}</span>
      <button class="small ghost" @click="$emit('close')"><AppIcon name="close" /></button>
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
.video-element { width: 100%; display: block; max-height: 400px; background: #000; }
.player-error { padding: 8px 14px; font-size: 12px; color: var(--red, #f87171); }
.player-meta {
  display: flex; gap: 16px; padding: 8px 14px; font-size: 11px; color: var(--text2, #666);
}
</style>
