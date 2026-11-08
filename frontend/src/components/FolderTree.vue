<script setup>
// =====================================================================
// FolderTree.vue —— 工作目录的文件夹结构树（美观化）
//
// 把一个「根目录 + 若干子目录」渲染成一棵轻量的文件夹树：
//   📁 工作目录/<root>
//     ├ 📁 01_原稿    原著 / 原始文本
//     └ 📁 02_版本    每次生成的剧本快照
// 用于工作目录设置弹窗与右侧「同步到工作目录」区域，保持同一套视觉语言。
// =====================================================================

import FolderIcon from './FolderIcon.vue'

const props = defineProps({
  root: { type: String, default: '' },        // 工作目录根路径（可空）
  items: {
    type: Array,
    default: () => [
      { code: '01_原稿', name: '原著 / 原始文本' },
      { code: '02_版本', name: '每次生成的剧本快照' },
      { code: '03_导出', name: '导出的 .txt / .md / .docx' },
      { code: '04_知识库', name: '项目知识与备忘录' },
    ],
  },
})
</script>

<template>
  <div class="ftree">
    <!-- 根目录 -->
    <div class="frow root">
      <FolderIcon :open="true" class="ic" />
      <span class="name" :title="root">{{ root || '工作目录' }}</span>
    </div>
    <!-- 子目录 -->
    <div class="fchildren">
      <div v-for="(it, i) in items" :key="it.code" class="frow child">
        <span class="branch">{{ i < items.length - 1 ? '├' : '└' }}</span>
        <FolderIcon class="ic" />
        <span class="code">{{ it.code }}</span>
        <span class="desc">{{ it.name }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ftree { font-size: 12.5px; line-height: 1.5; }
.frow { display: flex; align-items: center; gap: 7px; padding: 4px 6px; border-radius: var(--radius-sm); min-width: 0; }
.frow:hover { background: color-mix(in oklch, var(--ink) 4%, transparent); }
.frow .ic { width: 15px; height: 15px; color: color-mix(in oklch, var(--ink) 70%, var(--muted)); flex: none; }
.frow.root { font-weight: 650; }
.frow.root .name { color: var(--ink); }
.fchildren { margin-left: 13px; }
.frow.child { padding-left: 2px; }
.branch { color: var(--dim); font-family: var(--mono); font-size: 11px; width: 12px; flex: none; }
.code { color: var(--ink); font-weight: 600; white-space: nowrap; }
.desc { color: var(--muted); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
