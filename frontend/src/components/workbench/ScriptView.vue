<script setup>
// =====================================================================
// ScriptView.vue —— 工作台·剧本页（对标即梦剧本页四段：设定 / 分集大纲 / 正文 / 历史修改）
//
// 数据来源全部走全局 store（storyPlan / versions / viewerScript / overview），
// 本组件只做「看与调」：进视图时按需补拉故事规划与版本列表，
// 编剧笔记走 GET/PUT /projects/{pid}/notes，停止输入 1.5s 后自动保存。
// 正文 tab 原样复用 ScreenplayEditor（原位编辑 + 自动保存）。
// =====================================================================

import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import ScreenplayEditor from '../ScreenplayEditor.vue'
import { api } from '../../api'
import { store, notify, loadStoryPlan, loadViewer } from '../../stores/app'

const TABS = [
  { key: 'settings', num: '01', label: '设定' },
  { key: 'outline', num: '02', label: '分集大纲' },
  { key: 'text', num: '03', label: '正文' },
  { key: 'history', num: '04', label: '历史修改' },
]
const tab = ref('settings')

// ---- 数据装载：进入视图时缺什么补什么（loadStoryPlan/loadViewer 自带导航守卫） ----
onMounted(async () => {
  if (!store.storyPlan) loadStoryPlan()
  if (!store.versions.length) loadViewer()
  loadNotes()
})
watch(() => store.pid, () => {
  notesLoaded.value = false  // 先关掉保存闸门：清空草稿不得触发自动保存
  notesDraft.value = ''
  loadNotes()
})

const latestVersionId = computed(() => store.versions[0]?.id || '')
const sp = computed(() => store.storyPlan)

const subtitle = computed(() => {
  if (sp.value?.status === 'proposed') return '提案待确认'
  const n = store.overview?.scenes ?? store.viewerScript?.scenes?.length
  return n ? `共 ${n} 场` : '剧本'
})

// ---- 角色定位显示 ----
const ROLE_LABELS = {
  protagonist: '主角', lead: '主角', hero: '主角',
  antagonist: '反派', villain: '反派',
  supporting: '配角', minor: '配角',
}
function roleLabel(role) {
  if (!role) return '配角'
  return ROLE_LABELS[String(role).toLowerCase()] || role
}

// ---- 关键事件按集分组 ----
const eventGroups = computed(() => {
  const events = sp.value?.understanding?.events || []
  const map = new Map()
  for (const ev of events) {
    const ep = ev.episode == null ? '未分集' : `第 ${ev.episode} 集`
    if (!map.has(ep)) map.set(ep, [])
    map.get(ep).push(ev)
  }
  return [...map.entries()].map(([ep, list]) => ({ ep, list }))
})

// ---- 分集大纲：每集事件数 ----
function eventCountFor(epIndex) {
  const events = sp.value?.understanding?.events || []
  return events.filter((e) => e.episode === epIndex).length
}

// ---- 版本来源徽标 ----
// 实际后端取值：generation | agent_adaptation | import | manual_edit（含 agent 别名），
// 未识别的来源直接原文显示，不猜。
const SOURCE_LABELS = {
  generation: '生成',
  manual: '手改', manual_edit: '手改',
  agent: '改编', agent_adaptation: '改编',
  import: '导入',
}
function sourceLabel(t) {
  if (!t) return '生成'
  return SOURCE_LABELS[t] || t
}

function fmtMinute(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d)) return String(iso)
  const p = (x) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ---- 编剧笔记（设定 tab 底部，自动保存） ----
// 与「基线文本」比较：回填/切项目带来的赋值不等于用户改动，只有
// 真正偏离基线的编辑才调度保存——比首帧标记更稳（不怕 watch flush 时序）。
const notesDraft = ref('')
const notesBaseline = ref('')
const notesLoaded = ref(false)
const notesStatus = ref('')   // '' | 'saving' | 'saved' | 'error'
let notesTimer = null

async function loadNotes() {
  notesLoaded.value = false
  if (!store.pid) return
  try {
    const r = await api(`/projects/${store.pid}/notes`)
    notesBaseline.value = r.notes || ''
    notesDraft.value = notesBaseline.value
    notesLoaded.value = true
  } catch {
    notesLoaded.value = false  // 接口不可用时整块隐藏，不影响页面其余部分
  }
}

watch(notesDraft, (v) => {
  clearTimeout(notesTimer)
  if (v === notesBaseline.value) return
  if (!store.pid || !notesLoaded.value) return
  notesTimer = setTimeout(saveNotes, 1500)
})

async function saveNotes() {
  notesStatus.value = 'saving'
  const target = notesDraft.value
  try {
    await api(`/projects/${store.pid}/notes`, 'PUT', { notes: target })
    notesBaseline.value = target
    notesStatus.value = 'saved'
    setTimeout(() => { notesStatus.value = '' }, 2000)
  } catch (e) {
    notesStatus.value = 'error'
    notify(`笔记保存失败：${e.message}`)
  }
}

onBeforeUnmount(() => {
  clearTimeout(notesTimer)
})
</script>

<template>
  <div class="script-view">
    <!-- 标题行 -->
    <div class="sv-head">
      <h2 class="sv-title">剧本</h2>
      <span class="sv-sub">{{ subtitle }}</span>
      <div class="sv-tabs seg-tabs">
        <button
          v-for="t in TABS" :key="t.key"
          :class="['seg-tab', { active: tab === t.key }]"
          @click="tab = t.key"
        >
          <span class="seg-num">{{ t.num }}</span>{{ t.label }}
        </button>
      </div>
    </div>

    <div class="sv-body">
      <!-- ============ 01 设定 ============ -->
      <template v-if="tab === 'settings'">
        <div v-if="!sp" class="sv-empty">
          <AppIcon name="edit" class="empty-ic" />
          <p>还没有故事提案——在右侧对话里说「读原文做分集提案」</p>
        </div>

        <template v-else>
          <div v-if="sp.status === 'proposed'" class="tip-bar">
            <AppIcon name="warning" />
            <span>提案待确认——在对话里回复确认后才会按集扩写</span>
          </div>

          <!-- 故事内核卡 -->
          <section class="card">
            <h3 class="card-title">故事内核</h3>
            <p v-if="sp.understanding?.logline" class="logline">{{ sp.understanding.logline }}</p>
            <div v-if="sp.understanding?.core_conflict" class="conflict">
              <span class="k">核心冲突</span>
              <span class="v">{{ sp.understanding.core_conflict }}</span>
            </div>
            <div v-if="(sp.understanding?.themes || []).length" class="themes">
              <span v-for="th in sp.understanding.themes" :key="th" class="pill">{{ th }}</span>
            </div>
            <p v-if="!sp.understanding?.logline && !sp.understanding?.core_conflict && !(sp.understanding?.themes || []).length"
               class="muted-line">提案里还没有内核信息</p>
          </section>

          <!-- 人物小传 -->
          <section class="card">
            <h3 class="card-title">人物小传</h3>
            <div v-if="(sp.understanding?.characters || []).length" class="chars">
              <div v-for="c in sp.understanding.characters" :key="c.name" class="char">
                <div class="char-head">
                  <span class="char-name">{{ c.name }}</span>
                  <span class="char-role">{{ roleLabel(c.role) }}</span>
                </div>
                <p v-if="c.bio" class="char-bio">{{ c.bio }}</p>
              </div>
            </div>
            <p v-else class="muted-line">提案里暂无人物小传</p>
          </section>

          <!-- 关键事件 -->
          <section class="card">
            <h3 class="card-title">关键事件</h3>
            <div v-if="eventGroups.length" class="events">
              <div v-for="g in eventGroups" :key="g.ep" class="event-group">
                <div class="eg-title">{{ g.ep }}</div>
                <div v-for="ev in g.list" :key="ev.id" class="event-row">
                  <span class="ev-id">{{ ev.id }}</span>
                  <span class="ev-text">{{ ev.text }}</span>
                </div>
              </div>
            </div>
            <p v-else class="muted-line">提案里暂无关键事件</p>
          </section>

          <!-- 编剧笔记 -->
          <section v-if="notesLoaded" class="card">
            <h3 class="card-title">
              编剧笔记
              <span v-if="notesStatus" class="notes-status">
                {{ notesStatus === 'saving' ? '保存中…' : notesStatus === 'saved' ? '已保存' : '保存失败' }}
              </span>
            </h3>
            <textarea
              v-model="notesDraft"
              class="notes-area"
              rows="6"
              placeholder="记一些贯穿全片的设定、口头禅、伏笔约定——停止输入后自动保存"
            ></textarea>
          </section>
        </template>
      </template>

      <!-- ============ 02 分集大纲 ============ -->
      <template v-else-if="tab === 'outline'">
        <div v-if="!sp" class="sv-empty">
          <AppIcon name="edit" class="empty-ic" />
          <p>还没有分集提案——在右侧对话里说「读原文做分集提案」</p>
        </div>
        <div v-else-if="!(sp.episodes || []).length" class="sv-empty">
          <p>提案里还没有分集条目</p>
        </div>
        <div v-else class="eps">
          <section v-for="ep in sp.episodes" :key="ep.index" class="card ep-card">
            <div class="ep-head">
              <span class="ep-num">{{ ep.index }}</span>
              <h3 class="ep-title">第 {{ ep.index }} 集《{{ ep.title }}》</h3>
            </div>
            <p v-if="ep.hook" class="ep-hook">{{ ep.hook }}</p>
            <div class="ep-meta">
              <span v-if="ep.target_sec">约 {{ ep.target_sec }} 秒</span>
              <span v-if="ep.scenes_hint">约 {{ ep.scenes_hint }} 场</span>
              <span>{{ eventCountFor(ep.index) }} 个关键事件</span>
            </div>
          </section>
        </div>
      </template>

      <!-- ============ 03 正文 ============ -->
      <template v-else-if="tab === 'text'">
        <ScreenplayEditor
          v-if="store.viewerScript && latestVersionId"
          :script="store.viewerScript"
          :version-id="latestVersionId"
          standalone
          @saved="loadViewer()"
        />
        <div v-else class="sv-empty">
          <p>还没有剧本正文——在右侧对话里说「按提案写第 1 集剧本」</p>
        </div>
      </template>

      <!-- ============ 04 历史修改 ============ -->
      <template v-else>
        <div v-if="!store.versions.length" class="sv-empty">
          <p>还没有版本记录——生成或改编一次剧本后，这里会留下每一次修改</p>
        </div>
        <div v-else class="hist">
          <details v-for="v in store.versions" :key="v.id" class="hist-row">
            <summary class="hist-sum">
              <div class="hist-main">
                <span class="hist-label">{{ v.label || '（未命名版本）' }}</span>
                <span class="hist-sub">版本 {{ String(v.id).slice(-6) }} · {{ fmtMinute(v.created_at) }}</span>
              </div>
              <span class="badge">{{ sourceLabel(v.source_type) }}</span>
            </summary>
            <p class="hist-notes">{{ v.notes || '这一版没有留下说明' }}</p>
          </details>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.script-view {
  display: flex; flex-direction: column;
  min-height: 0; height: 100%; overflow: hidden;
  background: var(--bg);
}

/* ---- 标题行 ---- */
.sv-head {
  flex: none;
  padding: var(--space-4) var(--space-5) 0;
}
.sv-title {
  margin: 0; font-size: var(--fs-lg); font-weight: 600; color: var(--ink);
  display: inline;
}
.sv-sub {
  margin-left: var(--space-3); font-size: var(--fs-xs); color: var(--muted);
}
.sv-tabs { margin-top: var(--space-3); }

/* ---- 内容滚动区 ---- */
.sv-body {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-8);
}

/* ---- 提示条 ---- */
.tip-bar {
  display: flex; align-items: center; gap: var(--space-2);
  background: var(--gold-soft); color: var(--ink);
  border: 1px solid var(--gold); border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm); margin-bottom: var(--space-4);
}
.tip-bar svg { width: 1em; height: 1em; color: var(--gold); flex: none; }

/* ---- 通用卡片 ---- */
.card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5);
  margin-bottom: var(--space-4);
}
.card-title {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-sm); font-weight: 600; color: var(--ink);
  display: flex; align-items: center; gap: var(--space-2);
}

/* ---- 故事内核 ---- */
.logline {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-md); line-height: 1.7; color: var(--ink);
}
.conflict { display: flex; gap: var(--space-2); margin-bottom: var(--space-3); font-size: var(--fs-sm); }
.conflict .k { color: var(--muted); flex: none; }
.conflict .v { color: var(--ink); line-height: 1.6; }
.themes { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.pill {
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 999px; padding: 3px 10px;
  font-size: var(--fs-xs); color: var(--muted);
}

/* ---- 人物小传 ---- */
.chars { display: flex; flex-direction: column; gap: var(--space-3); }
.char-head { display: flex; align-items: center; gap: var(--space-2); }
.char-name { font-size: var(--fs-sm); font-weight: 600; color: var(--ink); }
.char-role {
  font-size: var(--fs-xs); color: var(--gold);
  border: 1px solid var(--gold); border-radius: var(--radius-sm);
  padding: 1px 6px;
}
.char-bio { margin: var(--space-1) 0 0; font-size: var(--fs-sm); color: var(--muted); line-height: 1.6; }

/* ---- 关键事件 ---- */
.events { display: flex; flex-direction: column; gap: var(--space-3); }
.eg-title { font-size: var(--fs-xs); color: var(--muted); margin-bottom: var(--space-1); }
.event-row { display: flex; gap: var(--space-2); padding: 3px 0; font-size: var(--fs-sm); }
.ev-id {
  flex: none; font: 500 var(--fs-xs)/1.8 var(--mono); color: var(--dim);
  background: var(--surface-2); border-radius: var(--radius-sm); padding: 0 6px;
}
.ev-text { color: var(--ink); line-height: 1.6; }

/* ---- 编剧笔记 ---- */
.notes-status { font-size: var(--fs-xs); font-weight: 400; color: var(--muted); }
.notes-area {
  width: 100%; box-sizing: border-box; resize: vertical;
  background: var(--surface-2); color: var(--ink);
  border: 1px solid var(--line); border-radius: var(--radius-md);
  padding: var(--space-3); font-size: var(--fs-sm); line-height: 1.6;
  font-family: inherit;
}
.notes-area:focus { outline: none; border-color: var(--line-strong); }

/* ---- 分集大纲 ---- */
.eps { display: flex; flex-direction: column; }
.ep-card { position: relative; }
.ep-head { display: flex; align-items: baseline; gap: var(--space-3); margin-bottom: var(--space-2); }
.ep-num { font: 700 var(--fs-xs) var(--mono); color: var(--gold); letter-spacing: .08em; }
.ep-title { margin: 0; font-size: var(--fs-md); font-weight: 600; color: var(--ink); }
.ep-hook { margin: 0 0 var(--space-3); font-size: var(--fs-sm); color: var(--muted); line-height: 1.6; }
.ep-meta { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); font-size: var(--fs-xs); color: var(--dim); }

/* ---- 历史修改 ---- */
.hist { display: flex; flex-direction: column; gap: var(--space-2); }
.hist-row {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-md); overflow: hidden;
}
.hist-sum {
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  cursor: pointer; list-style: none;
}
.hist-sum::-webkit-details-marker { display: none; }
.hist-sum:hover { background: var(--surface-2); }
.hist-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.hist-label { font-size: var(--fs-sm); color: var(--ink); }
.hist-sub { font-size: var(--fs-xs); color: var(--dim); font-family: var(--mono); }
.hist-notes {
  margin: 0; padding: 0 var(--space-4) var(--space-4);
  font-size: var(--fs-sm); color: var(--muted); line-height: 1.7;
  white-space: pre-wrap; border-top: 1px dashed var(--line); padding-top: var(--space-3);
}
.badge {
  flex: none; font-size: var(--fs-xs); color: var(--muted);
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 999px; padding: 2px 8px;
}

/* ---- 空态 ---- */
.sv-empty {
  display: grid; place-items: center; justify-items: center;
  min-height: 220px; color: var(--muted); font-size: var(--fs-sm);
  text-align: center; gap: var(--space-2);
}
.empty-ic { font-size: var(--fs-display); color: var(--dim); }
.empty-ic svg { width: 1em; height: 1em; }
.muted-line { margin: 0; font-size: var(--fs-sm); color: var(--dim); }
</style>
