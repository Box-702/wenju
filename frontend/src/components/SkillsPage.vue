<script setup>
// =====================================================================
// SkillsPage.vue —— 技能页（从输入区的 Skill 胶囊点进来）
//
// 一页两件事：
//   1. 看有哪些技能、干什么用（点一下 = 预填进输入框）
//   2. 导入新技能（上传 .md，带 frontmatter 的技能文件）
//
// 技能文件在服务端（app/skills/*/SKILL.md），导入的存数据库；
// 前端不复制清单，一律从 /api/skills 拉。
// =====================================================================

import { ref, onMounted, computed } from 'vue'
import { store, loadSkills, useSkill, notify } from '../stores/app'
import { api } from '../api'
import AppIcon from './AppIcon.vue'

const emit = defineEmits(['close'])

const importing = ref(false)
const fileInput = ref(null)
const importText = ref('')
const importOpen = ref(false)

const skills = computed(() => store.skills || [])

onMounted(() => {
  loadSkills()
})

function pick(s) {
  useSkill(s)
  emit('close')
}

function openImport() {
  importOpen.value = !importOpen.value
  importText.value = ''
}

async function onFileChosen(e) {
  const file = (e.target.files || [])[0]
  e.target.value = ''
  if (!file) return
  if (!/\.(md|markdown|txt)$/i.test(file.name)) {
    notify('技能文件请用 .md')
    return
  }
  const text = await file.text()
  importText.value = text
  importOpen.value = true
}

async function submitImport() {
  const text = importText.value.trim()
  if (!text) {
    notify('先选一个 .md 文件')
    return
  }
  importing.value = true
  try {
    const r = await api('/skills', 'POST', { content: text, filename: '' })
    notify(`已导入技能「${r.name || ''}」`, 'ok')
    importOpen.value = false
    importText.value = ''
    await loadSkills()
  } catch (e) {
    notify(`导入失败：${e.message}`)
  } finally {
    importing.value = false
  }
}
</script>

<template>
  <div class="page">
    <!-- 极光背景：与首页 / 其他整页浮层同一套 -->
    <div class="ps-aurora" aria-hidden="true">
      <span class="blob blob-blue"></span>
      <span class="blob blob-violet"></span>
      <span class="blob blob-red"></span>
      <span class="blob blob-teal"></span>
      <span class="grain"></span>
    </div>
    <header class="page-head">
      <button class="back" type="button" @click="emit('close')">
        <AppIcon name="arrow-left" /> 返回对话
      </button>
      <h2>技能</h2>
      <div class="head-actions">
        <input
          ref="fileInput"
          type="file"
          accept=".md,.markdown,.txt"
          hidden
          @change="onFileChosen"
        />
        <button class="btn-small" type="button" @click="openImport">
          <AppIcon name="plus" /> 导入技能
        </button>
        <button class="btn-small" type="button" @click="fileInput?.click()">选文件…</button>
      </div>
    </header>

    <div class="page-body">
      <p class="page-hint">
        技能是一份带说明的 Markdown（frontmatter 里写 name / description / when_to_use）。
        点一个技能 = 把它的适用场景预填进输入框，Agent 看到就会去加载它。
      </p>

      <div class="skill-list">
        <button v-for="s in skills" :key="s.name" class="skill-card" type="button" @click="pick(s)">
          <div class="skill-title">{{ s.name }}</div>
          <div class="skill-desc">{{ s.description }}</div>
          <div v-if="s.when_to_use" class="skill-when">适用：{{ s.when_to_use }}</div>
        </button>
        <div v-if="!skills.length" class="empty">还没有技能。导入一个 .md 开始。</div>
      </div>

      <div v-if="importOpen" class="import-box">
        <div class="import-head">
          <span>导入技能</span>
          <button class="btn-small" type="button" @click="importOpen = false">收起</button>
        </div>
        <textarea
          v-model="importText"
          class="import-text"
          rows="10"
          placeholder="粘贴或选择 .md 文件内容，例如：&#10;---&#10;name: my-skill&#10;description: 做什么用&#10;when_to_use: 什么时候用&#10;---&#10;&#10;具体步骤…"
        ></textarea>
        <button class="btn-small ok" type="button" :disabled="importing" @click="submitImport">
          {{ importing ? '导入中…' : '确认导入' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 整页浮层：底色与首页一致（极光 + 深底），不再是「死黑一块」 */
.page {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
.page-head, .page-body { position: relative; z-index: 1; }

.page-head {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--line);
}

.page-head h2 {
  margin: 0; flex: 1;
  font-size: 15px; font-weight: 600; letter-spacing: .04em; color: var(--ink);
}

.back {
  display: inline-flex; align-items: center; gap: 6px;
  height: 30px; padding: 0 10px; border-radius: var(--radius-sm);
  background: transparent; border: 1px solid var(--line);
  color: var(--muted); font-size: 12.5px; font-family: inherit; cursor: pointer;
}
.back:hover { color: var(--ink); background: rgba(255, 255, 255, .07); }
.back .app-ic { width: 14px; height: 14px; }

.head-actions { display: flex; align-items: center; gap: 8px; }

.page-body {
  flex: 1; overflow-y: auto;
  max-width: 860px; width: 100%; margin: 0 auto;
  padding: 22px 20px 48px;
}

.page-hint {
  margin: 0 0 16px; font-size: 12.5px; line-height: 1.7; color: var(--dim);
}

.skill-list { display: flex; flex-direction: column; gap: 8px; }

.skill-card {
  display: block; width: 100%; text-align: left;
  padding: 12px 14px; border-radius: var(--radius-lg);
  background: rgba(255, 255, 255, .05); border: 1px solid var(--line);
  color: inherit; font-family: inherit; cursor: pointer;
  transition: background 130ms var(--ease), border-color 130ms var(--ease);
}
.skill-card:hover { background: rgba(255, 255, 255, .09); border-color: var(--line-strong); }

.skill-title {
  font-size: 13px; font-weight: 600; color: var(--ink);
  font-family: var(--mono); margin-bottom: 4px;
}
.skill-desc { font-size: 12.5px; line-height: 1.6; color: var(--muted); }
.skill-when { margin-top: 5px; font-size: 11.5px; color: var(--dim); }

.empty { padding: 28px 0; text-align: center; font-size: 13px; color: var(--dim); }

.import-box {
  margin-top: 16px; padding: 14px;
  border-radius: var(--radius-lg); border: 1px solid var(--line);
  background: rgba(255, 255, 255, .04);
}
.import-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px; font-size: 12.5px; color: var(--muted);
}
.import-text {
  width: 100%; margin-bottom: 10px;
  background: rgba(0, 0, 0, .32); border: 1px solid var(--line); border-radius: var(--radius-md);
  padding: 10px 12px; color: var(--ink); font-family: var(--mono); font-size: 12px;
  resize: vertical;
}
</style>
