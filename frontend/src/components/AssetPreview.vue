<script setup>
// =====================================================================
// AssetPreview.vue —— 资产预览浮层（定妆照看大图 / 成片与素材直接播放）
//
// 全站唯一的媒体预览口：提示词里的 @引用、侧栏资产行、资产库卡片都打开它。
// 视频带 controls 直接播——「跑通生成出来的视频要能在应用里直接预览」，
// 不该只有下载链接。
// =====================================================================
import { computed } from 'vue'
import { store } from '../stores/app'

// 必须是 computed：store.assetPreview 是后打开才有的值，
// 直接在 setup 里取快照会永远是 null，预览打不开。
const asset = computed(() => store.assetPreview)

const KIND_LABEL = {
  character: '定妆照', location: '场景图', video: '视频', image: '图片', audio: '音频',
}
const kindLabel = computed(() => KIND_LABEL[asset.value?.kind] || '素材')
/** 下载指向的那份媒体（视频 > 音频 > 图片）。 */
const mediaUrl = computed(() => {
  const a = asset.value
  if (!a) return ''
  return a.video || a.audio || a.image || ''
})

function close() {
  store.assetPreview = null
}
</script>

<template>
  <Teleport to="body">
    <div v-if="asset" class="ap-wrap" @click.self="close">
      <figure class="ap-card">
        <video
          v-if="asset.video"
          class="ap-img ap-video"
          :src="asset.video"
          controls
          autoplay
          playsinline
        ></video>
        <audio v-else-if="asset.audio" class="ap-audio" :src="asset.audio" controls autoplay></audio>
        <img v-else-if="asset.image" class="ap-img" :src="asset.image" :alt="asset.name" />
        <div v-else class="ap-img ap-none">这个资产还没有可预览的内容</div>
        <figcaption class="ap-cap">
          <span class="ap-name">{{ asset.name }}</span>
          <span class="ap-id">{{ asset.id }}</span>
          <span class="ap-kind">{{ kindLabel }}</span>
          <a v-if="mediaUrl" class="ap-dl" :href="mediaUrl" :download="asset.name" target="_blank" rel="noopener">下载</a>
          <button class="ap-close" type="button" @click="close">关闭</button>
        </figcaption>
      </figure>
    </div>
  </Teleport>
</template>

<style scoped>
.ap-wrap {
  position: fixed; inset: 0; z-index: 200;
  display: grid; place-items: center;
  background: oklch(0 0 0 / .72);
  backdrop-filter: blur(6px);
  animation: fade-in 160ms var(--ease);
}
.ap-card {
  margin: 0; max-width: min(720px, 92vw); max-height: 88vh;
  display: flex; flex-direction: column; gap: 0;
  background: var(--panel-solid); border: 1px solid var(--line);
  border-radius: var(--radius-card); overflow: hidden;
  box-shadow: var(--shadow-3);
  animation: modal-pop 180ms var(--ease);
}
.ap-img {
  display: block; width: 100%; max-height: 72vh; object-fit: contain;
  background: rgba(0, 0, 0, .35);
}
.ap-none {
  display: grid; place-items: center; min-height: 220px;
  color: var(--dim); font-size: 13px;
}
/* 视频按自身比例放（竖屏短剧不该被拉成横条），黑底是播放器的一部分 */
.ap-video { width: auto; max-width: 100%; background: #000; }
.ap-audio { display: block; width: min(420px, 84vw); margin: 32px auto; }
.ap-cap {
  display: flex; align-items: center; gap: 10px;
  padding: 11px 14px; border-top: 1px solid var(--line);
}
.ap-name { font-size: 14px; color: var(--ink); font-weight: 600; }
.ap-id { font-family: var(--mono); font-size: 11px; color: var(--dim); }
.ap-kind { font-size: 11px; color: var(--muted); }
.ap-dl, .ap-close {
  margin-left: auto; font-size: 12px; color: var(--muted);
  text-decoration: none; padding: 3px 10px;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: transparent; cursor: pointer;
}
.ap-dl { margin-left: auto; }
.ap-close { margin-left: 0; }
.ap-dl:hover, .ap-close:hover { color: var(--ink); border-color: var(--line-strong); }
</style>
