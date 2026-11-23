<script setup>
// =====================================================================
// ScriptView.vue —— 工作台·剧本页（对标即梦短剧工作室的剧本形态）
//
// 三个 tab（修改历史在左栏，见 ScriptHistoryPanel）：
//   01 设定   —— 结构化表单卡：集数/受众、核心梗、一句话故事、人物表、事件底线
//   02 分集大纲—— 每集一张卡 + 底部节拍条（该集事件画成等宽小段）
//   03 正文   —— 只读 screenplay 排版渲染 viewerScript（不再挂编辑器，改稿走对话）
//
// 数据来源全部走全局 store（storyPlan / viewerScript / versions / overview），
// 进视图时缺什么补什么；编剧笔记仍走 GET/PUT /projects/{pid}/notes，
// 停止输入 1.5s 后自动保存（基线文本比较逻辑原样保留）。
// =====================================================================

import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import { api } from '../../api'
import { store, notify, loadStoryPlan, loadViewer, toggleAgentPanel } from '../../stores/app'

// 历史修改已迁去左栏（即梦「脚本修改历史」形态，随剧本页联动）——这里只剩三步。
const TABS = [
  { key: 'settings', num: '01', label: '设定' },
  { key: 'outline', num: '02', label: '分集大纲' },
  { key: 'text', num: '03', label: '正文' },
]
const tab = ref('settings')

// ---- 数据装载 ----
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

const sp = computed(() => store.storyPlan)
const hasChatModel = computed(() => store.modelInfo?.available === true)

const subtitle = computed(() => {
  if (sp.value?.status === 'proposed') return '提案待确认'
  const n = store.overview?.scenes ?? store.viewerScript?.scenes?.length
  return n ? `共 ${n} 场` : '剧本'
})

// =====================================================================
// 01 设定
// =====================================================================

// ---- 目标受众：只存本地 state，改动不假装能落库 ----
const audience = ref('')
watch(sp, (v) => {
  const themes = v?.understanding?.themes || []
  audience.value = themes.length ? themes.join(' · ') : '大众（偏男频）'
}, { immediate: true })
function audienceChanged() {
  notify('设定编辑暂未接入后端', 'info')
}

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

// ---- 全部节拍文本拼接（事件底线判据 + 人物台词量共用底料） ----
const allBeats = computed(() =>
  (store.viewerScript?.scenes || []).flatMap((s) => s.beats || []))
const allBeatText = computed(() =>
  allBeats.value.map((b) => `${b.text || ''}${b.line || ''}`).join('\n'))

// ---- 人物表：storyPlan 角色 ∪ viewerScript 身份（头像 / 台词量） ----
const charRows = computed(() => {
  const list = sp.value?.understanding?.characters || []
  const vsChars = store.viewerScript?.characters || []
  return list.map((c) => {
    const vc = vsChars.find((x) => x.name === c.name || x.id === c.name) || null
    const id = vc?.id || ''
    const ref_ = id ? store.assetRefs[id] : null
    const lines = allBeats.value.filter(
      (b) => (id && b.speaker === id) || (c.name && b.speaker === c.name)).length
    return {
      name: c.name || vc?.name || '未命名',
      role: c.role || vc?.role || '',
      bio: c.bio || vc?.description || '',
      image: ref_?.image || '',
      lines,
    }
  })
})

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

// 「拍到了」判据：事件文本里最长的 2+ 字片段是否出现在正文节拍文本里（纯前端字符串包含）。
function longestFragment(text) {
  const parts = String(text || '').split(/[^0-9A-Za-z\u4e00-\u9fff]+/)
  let best = ''
  for (const p of parts) if (p.length >= 2 && p.length > best.length) best = p
  return best
}
function eventCovered(ev) {
  const frag = longestFragment(ev?.text)
  if (!frag) return false
  return allBeatText.value.includes(frag)
}

// =====================================================================
// 02 分集大纲
// =====================================================================
function eventsFor(epIndex) {
  return (sp.value?.understanding?.events || []).filter((e) => e.episode === epIndex)
}
function sceneCountFor(ep) {
  const byEp = (store.viewerScript?.scenes || []).filter((s) => s.episode === ep.index).length
  return byEp || ep.scenes_hint || 0
}

// =====================================================================
// 03 正文（只读 screenplay 排版）
// =====================================================================
const screenplay = computed(() => {
  const vs = store.viewerScript
  if (!vs) return []
  const idToName = {}
  for (const c of vs.characters || []) idToName[c.id] = c.name
  const locName = {}
  for (const l of vs.locations || []) locName[l.id] = l.name
  const localCounter = {}
  return (vs.scenes || []).map((s, i) => {
    const ep = s.episode ?? 1
    localCounter[ep] = (localCounter[ep] || 0) + 1
    const nameOf = (x) => idToName[x] || x || ''
    return {
      key: s.id || i,
      ep,
      localNo: localCounter[ep],
      slug: [s.time, locName[s.location_id] || s.location_id].filter(Boolean).join(' '),
      cast: (s.characters || []).map(nameOf).join('、'),
      beats: (s.beats || []).map((b) => ({
        id: b.id,
        type: b.type,
        text: b.text || '',
        speaker: nameOf(b.speaker),
        emotion: b.emotion || '',
        line: b.line || '',
      })),
    }
  })
})
const totalChars = computed(() =>
  allBeats.value.reduce((n, b) => n + (b.text || '').length + (b.line || '').length, 0))

function goChat() {
  // 打开右侧 Agent 面板（若收起）并聚焦输入框——改稿在对话里说，不在这里编辑。
  if (!store.agentOpen) toggleAgentPanel()
  store.focusSeq += 1
  notify('在右侧对话里直接说改动需求即可', 'info')
}

// =====================================================================
// 编剧笔记（设定 tab 底部，自动保存）
// 与「基线文本」比较：回填/切项目带来的赋值不等于用户改动，只有
// 真正偏离基线的编辑才调度保存——比首帧标记更稳（不怕 watch flush 时序）。
// =====================================================================
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
          <p v-if="hasChatModel">还没有故事提案——在对话里说「读原文做分集提案」</p>
          <template v-else>
            <p>这是演示初稿。配置模型后，可继续生成分集提案并拆分镜。</p>
            <button class="ghost small" @click="store.showSettings = true">配置模型</button>
          </template>
        </div>

        <template v-else>
          <div v-if="sp.status === 'proposed'" class="tip-bar">
            <AppIcon name="warning" />
            <span>提案待确认——在右侧对话回复确认</span>
          </div>

          <!-- 顶部两列：集数 / 目标受众 -->
          <div class="sv-grid2">
            <div class="field-card">
              <span class="field-label">集数</span>
              <div class="field-num">{{ (sp.episodes || []).length }}</div>
              <span class="field-note">由分集提案确定</span>
            </div>
            <div class="field-card">
              <span class="field-label">目标受众</span>
              <input
                v-model="audience"
                class="field-input"
                @change="audienceChanged"
              />
              <span class="field-note">本地草稿，暂未接入后端</span>
            </div>
          </div>

          <!-- 核心梗 -->
          <section class="card">
            <h3 class="card-title">核心梗</h3>
            <p v-if="sp.understanding?.core_conflict" class="big-text">{{ sp.understanding.core_conflict }}</p>
            <p v-if="sp.understanding?.logline" class="big-text alt">{{ sp.understanding.logline }}</p>
            <p v-if="!sp.understanding?.core_conflict && !sp.understanding?.logline"
               class="muted-line">提案里还没有核心梗信息</p>
          </section>

          <!-- 一句话故事 -->
          <section class="card">
            <h3 class="card-title">一句话故事</h3>
            <p v-if="sp.understanding?.logline" class="oneliner">{{ sp.understanding.logline }}</p>
            <p v-else class="muted-line">提案里还没有 logline</p>
          </section>

          <!-- 人物表 -->
          <section class="card">
            <h3 class="card-title">人物</h3>
            <div v-if="charRows.length" class="char-table">
              <div v-for="(c, i) in charRows" :key="c.name + i" class="char-row">
                <span class="avatar">
                  <img v-if="c.image" :src="c.image" :alt="c.name" />
                  <AppIcon v-else name="user" />
                </span>
                <span class="c-name">{{ c.name }}</span>
                <span class="c-role">{{ roleLabel(c.role) }}</span>
                <span class="c-bio">{{ c.bio }}</span>
                <span class="c-lines" :title="'正文里该角色的台词节页数'">{{ c.lines }} 句台词</span>
              </div>
            </div>
            <p v-else class="muted-line">提案里暂无人物</p>
          </section>

          <!-- 事件底线 -->
          <section class="card">
            <h3 class="card-title">事件底线</h3>
            <div v-if="eventGroups.length" class="events">
              <div v-for="g in eventGroups" :key="g.ep" class="event-group">
                <div class="eg-title">{{ g.ep }}</div>
                <div v-for="ev in g.list" :key="ev.id" class="event-row">
                  <span class="ev-id">{{ ev.id }}</span>
                  <span class="ev-text">{{ ev.text }}</span>
                  <span :class="['ev-cover', { hit: eventCovered(ev) }]">
                    <AppIcon v-if="eventCovered(ev)" name="check" />
                    {{ eventCovered(ev) ? '拍到了' : '未落剧本' }}
                  </span>
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
          <p>还没有分集提案——点左栏「对话」回去说「读原文做分集提案」</p>
        </div>
        <div v-else-if="!(sp.episodes || []).length" class="sv-empty">
          <p>提案里还没有分集条目</p>
        </div>
        <div v-else class="eps">
          <section v-for="ep in sp.episodes" :key="ep.index" class="card ep-card">
            <div class="ep-head">
              <span class="ep-num">{{ String(ep.index).padStart(2, '0') }}</span>
              <h3 class="ep-title">第 {{ ep.index }} 集《{{ ep.title }}》</h3>
            </div>
            <p v-if="ep.hook" class="ep-hook">{{ ep.hook }}</p>
            <div class="ep-meta">
              <span v-if="ep.target_sec">约 {{ ep.target_sec }} 秒</span>
              <span>{{ eventsFor(ep.index).length }} 个关键事件</span>
              <span v-if="sceneCountFor(ep)">约 {{ sceneCountFor(ep) }} 场</span>
            </div>
            <!-- 节拍条：该集事件画成等宽小段 -->
            <div v-if="eventsFor(ep.index).length" class="beat-bar">
              <span
                v-for="ev in eventsFor(ep.index)" :key="ev.id"
                class="beat-seg" :title="ev.text"
              ></span>
            </div>
          </section>
        </div>
      </template>

      <!-- ============ 03 正文 ============ -->
      <template v-else-if="tab === 'text'">
        <div v-if="!screenplay.length" class="sv-empty">
          <p v-if="hasChatModel">还没有剧本正文——在对话里说「按提案写第 1 集剧本」</p>
          <template v-else>
            <p>配置模型后，可根据分集提案生成正式剧本。</p>
            <button class="ghost small" @click="store.showSettings = true">配置模型</button>
          </template>
        </div>
        <template v-else>
          <div class="sp-toolbar">
            <span class="sp-count">共 {{ screenplay.length }} 场 · {{ totalChars }} 字</span>
            <button class="ghost small" @click="goChat">去对话里改</button>
          </div>
          <div class="sp-page">
            <section v-for="sc in screenplay" :key="sc.key" class="sp-scene">
              <div class="sp-head">场 {{ sc.ep }}-{{ sc.localNo }}</div>
              <div v-if="sc.slug" class="sp-slug">{{ sc.slug }}</div>
              <div v-if="sc.cast" class="sp-cast">出场人物：{{ sc.cast }}</div>
              <div v-for="b in sc.beats" :key="b.id" class="sp-line">
                <template v-if="b.type === 'dialogue'">
                  <strong class="sp-speaker">{{ b.speaker }}</strong>
                  <span v-if="b.emotion" class="sp-emo">（{{ b.emotion }}）</span>
                  <span class="sp-dialogue">：{{ b.line }}</span>
                </template>
                <template v-else>
                  <span class="sp-tri">△</span><span>{{ b.text }}</span>
                </template>
              </div>
            </section>
          </div>
        </template>
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

/* ---- 设定：顶部两列字段卡 ---- */
.sv-grid2 {
  display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-4);
  margin-bottom: var(--space-4);
}
.field-card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: var(--space-3) var(--space-4);
  display: flex; flex-direction: column; gap: var(--space-2);
}
.field-label { font-size: var(--fs-xs); color: var(--muted); }
.field-num {
  font: 600 var(--fs-xl)/1 var(--mono); color: var(--ink);
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: var(--radius-md); padding: var(--space-2) var(--space-3);
}
.field-input {
  background: var(--surface-2); color: var(--ink);
  border: 1px solid var(--line); border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3); font-size: var(--fs-sm); font-family: inherit;
}
.field-input:focus { outline: none; border-color: var(--line-strong); }
.field-note { font-size: var(--fs-xs); color: var(--dim); }

/* ---- 设定：大字卡 ---- */
.big-text {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-lg); line-height: 1.7; color: var(--ink);
}
.big-text.alt { font-size: var(--fs-md); color: var(--muted); margin-bottom: 0; }
.oneliner {
  margin: 0;
  font-size: var(--fs-lg); line-height: 1.7; color: var(--ink);
}

/* ---- 设定：人物表 ---- */
.char-table { display: flex; flex-direction: column; }
.char-row {
  display: grid;
  grid-template-columns: 32px 90px 52px 1fr auto;
  align-items: center; gap: var(--space-3);
  padding: var(--space-2) 0;
  border-bottom: 1px dashed var(--line);
}
.char-row:last-child { border-bottom: none; }
.avatar {
  width: 32px; height: 32px; border-radius: var(--radius-md);
  background: var(--surface-2); border: 1px solid var(--line);
  display: grid; place-items: center; overflow: hidden;
  color: var(--dim); font-size: var(--fs-sm);
}
.avatar svg { width: 1em; height: 1em; }
.avatar img { width: 100%; height: 100%; object-fit: cover; }
.c-name { font-size: var(--fs-sm); font-weight: 600; color: var(--ink); }
.c-role {
  font-size: var(--fs-xs); color: var(--gold); text-align: center;
  border: 1px solid var(--gold); border-radius: var(--radius-sm);
  padding: 1px 6px;
}
.c-bio {
  font-size: var(--fs-sm); color: var(--muted); line-height: 1.5;
  overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.c-lines { font: 400 var(--fs-xs)/1 var(--mono); color: var(--dim); flex: none; }

/* ---- 设定：事件底线 ---- */
.events { display: flex; flex-direction: column; gap: var(--space-3); }
.eg-title { font-size: var(--fs-xs); color: var(--muted); margin-bottom: var(--space-1); }
.event-row {
  display: flex; align-items: baseline; gap: var(--space-2);
  padding: 3px 0; font-size: var(--fs-sm);
}
.ev-id {
  flex: none; font: 500 var(--fs-xs)/1.8 var(--mono); color: var(--dim);
  background: var(--surface-2); border-radius: var(--radius-sm); padding: 0 6px;
}
.ev-text { color: var(--ink); line-height: 1.6; }
.ev-cover {
  flex: none; margin-left: auto; display: inline-flex; align-items: center; gap: 4px;
  font-size: var(--fs-xs); border-radius: 999px; padding: 1px 8px;
  border: 1px solid var(--line); color: var(--dim); background: var(--surface-2);
}
.ev-cover.hit { color: var(--gold); border-color: var(--gold); background: var(--gold-soft); }
.ev-cover svg { width: .9em; height: .9em; }

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
.beat-bar {
  display: flex; gap: 2px; height: 12px; margin-top: var(--space-3);
  border-radius: var(--radius-sm); overflow: hidden;
}
.beat-seg {
  flex: 1; background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 3px; cursor: default; transition: background .12s, border-color .12s;
}
.beat-seg:hover { background: var(--gold-soft); border-color: var(--gold); }

/* ---- 正文（screenplay 排版） ---- */
.sp-toolbar {
  display: flex; align-items: center; justify-content: space-between; gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.sp-count { font-size: var(--fs-xs); color: var(--muted); font-family: var(--mono); }
.sp-page {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: var(--space-5) var(--space-6);
  font-family: var(--mono);
  font-size: var(--fs-md); line-height: 1.7; color: var(--ink);
}
.sp-scene { padding-bottom: var(--space-4); }
.sp-scene + .sp-scene {
  border-top: 1px dashed var(--line);
  padding-top: var(--space-4); margin-top: var(--space-2);
}
.sp-head { font-weight: 700; color: var(--ink); margin-bottom: 2px; }
.sp-slug { color: var(--muted); }
.sp-cast { color: var(--dim); margin-bottom: var(--space-2); }
.sp-line { margin: 2px 0; white-space: pre-wrap; }
.sp-tri { color: var(--gold); margin-right: 6px; }
.sp-speaker { font-weight: 700; color: var(--ink); }
.sp-emo { color: var(--muted); font-style: italic; }
.sp-dialogue { color: var(--ink); }

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
  margin: 0; padding: var(--space-3) var(--space-4) var(--space-4);
  font-size: var(--fs-sm); color: var(--muted); line-height: 1.7;
  white-space: pre-wrap; border-top: 1px dashed var(--line);
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
