<script setup>
// =====================================================================
// AppIcon.vue —— 全站统一图标系统（v1）
//
// 设计规则：
//   - 16×16 视窗，1.5 描边，round cap/join，currentColor 取色
//   - 只用几何语言：线 + 少量实心点，绝不混用填充风格
//   - 尺寸跟字号（1em），由 CSS 的 font-size 控制
//
// 用法：<AppIcon name="plus" />  <AppIcon name="close" class="i" />
// 名称见 ICONS。新图标先在这里定义，再在组件里用，禁止再写内联 SVG。
// =====================================================================
import { computed } from 'vue'

const props = defineProps({
  name: { type: String, required: true },
})

// 每个图标是一段 16×16 的 SVG 内部标记（stroke 风格统一由外层 svg 给出）
const ICONS = {
  plus: '<path d="M8 3.2v9.6M3.2 8h9.6" />',
  close: '<path d="M4 4l8 8M12 4l-8 8" />',
  check: '<path d="M3.2 8.6l3.2 3.2 6.4-7.6" />',
  edit: '<path d="M11.1 2.9l2 2L6 12l-2.8.8L4 10l7.1-7.1z" /><path d="M9.8 4.2l2 2" />',
  image: '<rect x="2.4" y="3" width="11.2" height="10" rx="1.4" /><circle cx="5.8" cy="6.6" r="1.1" /><path d="M2.8 11.6l3.3-3.1c.4-.37 1-.36 1.4.02l4.6 4.3M9.6 10.2l1.5-1.4c.4-.37 1-.37 1.4 0l2.3 2.2" />',
  minus: '<path d="M3.2 8h9.6" />',
  send: '<path d="M8 13V3.6M3.8 7.4L8 3.2l4.2 4.2" />',
  'arrow-down': '<path d="M8 3v9.4M3.8 8.6L8 12.8l4.2-4.2" />',
  'arrow-right': '<path d="M3.2 8h9.6M9 4.2L12.8 8 9 11.8" />',
  'arrow-left': '<path d="M12.8 8H3.2M7 4.2L3.2 8 7 11.8" />',
  'chevron-right': '<path d="M6 3.6L10.4 8 6 12.4" />',
  'chevron-down': '<path d="M3.6 6L8 10.4 12.4 6" />',
  menu: '<path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />',
  panel: '<rect x="2" y="2.8" width="12" height="10.4" rx="2" /><path d="M10.2 2.8v10.4" />',
  'panel-on': '<rect x="2" y="2.8" width="12" height="10.4" rx="2" /><path d="M10.2 2.8v10.4" /><rect x="10.9" y="3.6" width="2.3" height="8.8" rx="0.7" fill="currentColor" stroke="none" />',
  gear: '<circle cx="8" cy="8" r="2.1" /><path d="M8 1.8v1.9M8 12.3v1.9M1.8 8h1.9M12.3 8h1.9M3.6 3.6l1.35 1.35M11.05 11.05l1.35 1.35M12.4 3.6l-1.35 1.35M4.95 11.05L3.6 12.4" />',
  folder: '<path d="M2.2 4.6c0-.55.45-1 1-1h2.1c.3 0 .58.13.77.36l.55.64h5.16c.55 0 1 .45 1 1v5.8c0 .55-.45 1-1 1H3.2c-.55 0-1-.45-1-1V4.6z" fill="currentColor" stroke="none" />',
  'folder-open': '<path d="M2.2 4.6c0-.55.45-1 1-1h2.1c.3 0 .58.13.77.36l.55.64h5.16c.55 0 1 .45 1 1v1H2.2V4.6z" fill="currentColor" stroke="none" /><path d="M1.6 7h12.1l-1.3 4.3c-.13.42-.52.7-.96.7H3.2c-.55 0-1-.45-1-1V7z" fill="currentColor" stroke="none" />',
  warning: '<path d="M8 2.6l5.8 10.2H2.2L8 2.6z" /><path d="M8 6.6v2.9" /><circle cx="8" cy="11.3" r="0.7" fill="currentColor" stroke="none" />',
  info: '<circle cx="8" cy="8" r="5.8" /><path d="M8 7.4v3.4" /><circle cx="8" cy="5.2" r="0.7" fill="currentColor" stroke="none" />',
  trash: '<path d="M3 4.6h10M6.4 4.6V3.4c0-.4.3-.7.7-.7h1.8c.4 0 .7.3.7.7v1.2M4.4 4.6l.5 8c0 .5.45.9 1 .9h4.2c.55 0 1-.4 1-.9l.5-8" />',
  film: '<rect x="2.6" y="3.2" width="10.8" height="9.6" rx="1.2" /><path d="M2.6 6.1h10.8M5.4 3.2v2.9M8 3.2v2.9M10.6 3.2v2.9M5.4 9.5v3.3M8 9.5v3.3M10.6 9.5v3.3" stroke-width="1.2" />',
  play: '<path d="M5.6 3.8v8.4L12.4 8 5.6 3.8z" fill="currentColor" stroke="none" />',
  pause: '<path d="M5.4 3.6v8.8M10.6 3.6v8.8" stroke-width="1.8" />',
  stop: '<rect x="4.2" y="4.2" width="7.6" height="7.6" rx="1" fill="currentColor" stroke="none" />',
  search: '<circle cx="7.2" cy="7.2" r="4.2" /><path d="M10.4 10.4l3 3" />',
  upload: '<path d="M8 10.5V3.4M4.8 6.2L8 3l3.2 3.2M3 12.8h10" />',
  file: '<path d="M4 2.8h5l3 3v7.4H4V2.8z" /><path d="M9 2.8v3h3" />',
  sparkle: '<path d="M8 2.4c.5 2.6 1.5 3.6 4.1 4.1-2.6.5-3.6 1.5-4.1 4.1-.5-2.6-1.5-3.6-4.1-4.1 2.6-.5 3.6-1.5 4.1-4.1z" fill="currentColor" stroke="none" />',
  refresh: '<path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.8v2.4h-2.4" />',
  copy: '<rect x="5.4" y="5.4" width="7.4" height="7.4" rx="1" /><path d="M10.6 5.4V3.8c0-.55-.45-1-1-1H3.8c-.55 0-1 .45-1 1v5.8c0 .55.45 1 1 1h1.6" />',
  user: '<circle cx="8" cy="5.6" r="2.6" /><path d="M2.8 13.4c.7-2.4 2.8-3.7 5.2-3.7s4.5 1.3 5.2 3.7" />',
  download: '<path d="M8 3.2v7.1M4.8 7.5L8 10.7l3.2-3.2M3 13h10" />',
  eye: '<path d="M1.8 8s2.3-4 6.2-4 6.2 4 6.2 4-2.3 4-6.2 4S1.8 8 1.8 8z" /><circle cx="8" cy="8" r="1.9" />',
  sound: '<path d="M3.2 6.4h2.1l2.9-2.5v8.2L5.3 9.6H3.2z" /><path d="M10.9 6.3a2.8 2.8 0 0 1 0 3.4M12.7 4.5a5.2 5.2 0 0 1 0 7" />',
}

const html = computed(() => ICONS[props.name] || ICONS.info)
</script>

<template>
  <svg
    class="app-ic" viewBox="0 0 16 16" fill="none" aria-hidden="true"
    stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
    v-html="html"
  />
</template>

<style scoped>
.app-ic {
  width: 1em; height: 1em;
  display: inline-block; vertical-align: -0.125em;
  flex: none;
}
</style>
