<script setup>
// =====================================================================
// NewProjectModal.vue —— 新建作品弹窗
//
// 表单：剧名 + 改编类型 + 原著来源（上传 .txt/.md/.docx 或粘贴原文）。
// 支持点击选择与拖拽文件；提交走 /api/projects/import（FormData）。
// 体验：ESC/点遮罩关闭、打开时过渡 + 自动聚焦剧名、提交中禁用按钮。
// =====================================================================

import { ref, watch, nextTick, onUnmounted } from 'vue'
import { store, submitNewProject } from '../stores/app'
import AppIcon from './AppIcon.vue'

// 产品只做短剧。电影/剧集/舞台剧的入口已移除——多一个类型就多一条生成分支、
// 多一套没人维护的文案，而产品不会走到。老项目里已存的类型仍能正常打开。
const ADAPT_TYPE = 'short_drama'

// 画风：决定定妆图怎么出、提示词怎么写、配音用哪套声线。
// 二维与三维漫剧共用短剧节奏；未选画风时默认三维。
const VISUAL_STYLES = [
  { value: 'realistic', label: '实拍写实', hint: '真人质感，电影摄影' },
  { value: 'anime', label: '二维漫剧', hint: '日式二维动画，赛璐璐上色' },
  { value: '3d', label: '三维漫剧', hint: '精细角色建模，电影级光影' },
  { value: 'ink', label: '水墨国风', hint: '水墨笔触，留白构图' },
]
const ACCEPT = /\.(txt|md|markdown|docx|pdf)$/i

const title = ref('')
const adapt = ref(ADAPT_TYPE)
const visual = ref('3d')
const raw = ref('')
const file = ref(null)          // 选中的 File 对象
const dropText = ref('点击选择或拖入文件')
const dragOver = ref(false)
const err = ref('')
const fileInput = ref(null)
const titleInput = ref(null)    // 打开时自动聚焦
const submitting = ref(false)   // 提交中（导入需要分析，给足反馈）

// ESC 关闭（提交中不关闭）
function onKey(e) {
  if (e.key === 'Escape' && store.showNewProject && !submitting.value) store.showNewProject = false
}

// 每次打开：重置表单 + 聚焦剧名；关闭时移除键盘监听
watch(() => store.showNewProject, (open) => {
  if (open) {
    title.value = ''; raw.value = ''; file.value = null; err.value = ''; submitting.value = false
    dropText.value = '点击选择或拖入 .txt / .md / .docx / .pdf 文件'
    nextTick(() => titleInput.value?.focus())
    window.addEventListener('keydown', onKey)
  } else {
    window.removeEventListener('keydown', onKey)
  }
})
onUnmounted(() => window.removeEventListener('keydown', onKey))

/** 校验并记录选中的文件（点击与拖拽共用）。 */
function pickFile(f) {
  if (!ACCEPT.test(f.name)) {
    dropText.value = `不支持 ${f.name}，请选择 .txt/.md/.docx/.pdf`
    file.value = null
    return
  }
  file.value = f
  dropText.value = `${f.name}（${(f.size / 1024).toFixed(1)} KB）`
}

function onFileChange(e) { if (e.target.files.length) pickFile(e.target.files[0]) }
function onDrop(e) {
  dragOver.value = false
  if (e.dataTransfer.files.length) pickFile(e.dataTransfer.files[0])
}

/** 提交表单（校验 + 调 store 动作，错误就地显示）。 */
async function submit() {
  if (submitting.value) return
  err.value = ''
  if (!title.value.trim()) { err.value = '请填写剧名'; return }
  if (!file.value && !raw.value.trim()) { err.value = '请上传文件或粘贴原文'; return }
  submitting.value = true
  try {
    await submitNewProject({ title: title.value.trim(), adapt: adapt.value, visual: visual.value, file: file.value, raw: raw.value.trim() })
  } catch (e) { err.value = '创建失败：' + e.message } finally { submitting.value = false }
}
</script>

<template>
  <!-- 点遮罩关闭。不用 Vue Transition：其元素移除依赖 rAF/transitionend，
       在后台标签页会被冻结导致关不掉；这里用纯 CSS 入场动画（仅视觉，
       不影响 DOM 移除时机），v-if 关闭即瞬时移除，任何环境都可靠。 -->
  <div v-if="store.showNewProject" class="modal-backdrop" @click.self="!submitting && (store.showNewProject = false)">
    <div class="modal-card" role="dialog" aria-label="新建作品">
      <div class="modal-head">
        <div>
          <h3 class="modal-title">新建作品</h3>
          <p class="modal-sub">导入原著原文，就能开始改编</p>
        </div>
      </div>

      <label class="modal-label">剧名</label>
      <input ref="titleInput" v-model="title" class="modal-input" placeholder="例如：雨夜" @keydown.enter="submit" />

      <label class="modal-label">体裁</label>
      <div class="fixed-type">短剧 · 竖屏</div>

      <label class="modal-label">画风</label>
      <select v-model="visual" class="modal-select">
        <option v-for="s in VISUAL_STYLES" :key="s.value" :value="s.value">{{ s.label }}</option>
      </select>

      <label class="modal-label">原著文件</label>
      <div
        class="drop" :class="{ drag: dragOver, picked: file }"
        @click="fileInput.click()"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="onDrop"
      >
        <AppIcon :name="file ? 'file' : 'upload'" class="drop-ic" />
        <span>{{ dropText }}</span>
      </div>
      <input ref="fileInput" type="file" accept=".txt,.md,.markdown,.docx,.pdf" style="display: none" @change="onFileChange" />

      <label class="modal-label">或粘贴原文</label>
      <textarea v-model="raw" class="modal-textarea" placeholder="粘贴小说原文"></textarea>

      <div v-if="err" class="err"><AppIcon name="warning" /> {{ err }}</div>

      <div class="modal-foot">
        <button class="ghost" :disabled="submitting" @click="store.showNewProject = false">取消</button>
        <button class="primary" :disabled="submitting" @click="submit">{{ submitting ? '创建中…' : '创建作品' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 外壳（遮罩 / 玻璃卡 / 标签 / 输入框 / 底部按钮）统一走 style.css 的
   .modal-* 系列，这里只留这个弹窗自己的东西。 */
.fixed-type {
  font-size: var(--fs-md); color: var(--ink); padding: 8px 11px;
  border: 1px solid var(--line); border-radius: var(--radius-md);
  background: rgba(255, 255, 255, .03);
}
.drop {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  border: 1.5px dashed var(--line-strong); border-radius: var(--radius-lg); padding: 18px;
  text-align: center; color: var(--muted); font-size: var(--fs-md); cursor: pointer;
  transition: border-color var(--dur) var(--ease), color var(--dur) var(--ease), background-color var(--dur) var(--ease);
}
.drop-ic { font-size: 15px; flex: none; }
.drop.picked { color: var(--ink); border-color: var(--accent); border-style: solid; }
.drop:hover, .drop.drag { border-color: var(--accent); color: var(--ink); }
.drop.drag { background: var(--accent-soft); }
.err { display: flex; align-items: center; gap: 6px; color: var(--bad); font-size: var(--fs-sm); margin-top: 10px; }
</style>
