<script setup>
// =====================================================================
// ClipsView.vue —— 工作台·分镜页（片段编辑器，对标即梦「分镜」视图）
//
// 一个镜头组 = 一个片段 = 一次生成。卡片头部看组头（提示词/批准/出片/重出
// 都走组头），卡体逐机位微调参数（时长/景别，走成员自己的 id）。
// 批准不在本页做（D-43 硬闸门：确认流在全屏提示词编辑器里）。
// =====================================================================
import { computed, onMounted, reactive, ref } from 'vue'
import AppIcon from '../AppIcon.vue'
import {
  store,
  shownPrompt, groupHeadOf,
  loadShots, loadVideoJobs,
  openPromptEditor, saveShotParams, rerollShot,
} from '../../stores/app'

// ---------------------------------------------------------------------
// 加载与刷新
// ---------------------------------------------------------------------
const loading = ref(true)
const loadError = ref('')

async function refreshAll() {
  loading.value = true
  loadError.value = ''
  try {
    await Promise.all([loadShots(), loadVideoJobs()])
  } catch (e) {
    // 兜底：store 内部已各自 catch，这里只防渲染期意外抛错白屏
    loadError.value = e?.message || '分镜数据加载失败'
  } finally {
    loading.value = false
  }
}
onMounted(refreshAll)
// 注：loadShots 内部走的就是 video-version 详情接口（含每条组头原文/审计），
// 刷新按钮直接复用它即可，不必再裸调 api。

// 每张卡/每一行的在途锁：一次只允许一个写操作，防连点双提交。
const busy = reactive({})
async function run(key, fn) {
  if (busy[key]) return
  busy[key] = true
  try { await fn() } finally { busy[key] = false }
}

// ---------------------------------------------------------------------
// 分组：有 group_id 的按 (scene_id, group_id) 归一张片段卡，其余自成一张
// ---------------------------------------------------------------------
const cards = computed(() => {
  const shots = store.shots || []
  const map = new Map()
  for (const s of shots) {
    const key = s.group_id ? `g:${s.scene_id}|${s.group_id}` : `s:${s.id}`
    if (!map.has(key)) map.set(key, [])
    map.get(key).push(s)
  }
  const out = []
  for (const [key, members] of map) {
    members.sort(
      (a, b) => (a.group_index ?? 0) - (b.group_index ?? 0) || (a.order ?? 0) - (b.order ?? 0),
    )
    const head = members.find(m => m.group_prompt) || groupHeadOf(members[0]) || members[0]
    out.push({
      key,
      members,
      head,
      order: head.order ?? Math.min(...members.map(m => m.order ?? 0)),
    })
  }
  out.sort((a, b) => a.order - b.order)
  return out.map((c, i) => ({ ...c, no: i + 1 }))
})

const totalShots = computed(() => (store.shots || []).length)

function groupTotal(card) {
  return card.members.reduce((sum, m) => sum + (Number(m.duration_sec) || 0), 0)
}
function fmtDur(v) { return (Math.round(v * 10) / 10).toFixed(1) }

// ---------------------------------------------------------------------
// 状态徽标
// ---------------------------------------------------------------------
const PS_LABEL = { approved: '已批准', needs_review: '待审', needs_revision: '需修改' }
const PS_CLASS = { approved: 'ok', needs_review: 'warn', needs_revision: 'bad' }
function psBadge(head) {
  const st = head.prompt_status || 'needs_review'
  return { label: PS_LABEL[st] || '待审', cls: PS_CLASS[st] || 'warn' }
}

/** 组头在 videoJobs 里的最新任务（数组按时间序，取最后一条匹配）。 */
function latestJob(shotId) {
  let found = null
  for (const j of store.videoJobs || []) if (j.shot_id === shotId) found = j
  return found
}

function videoBadge(head) {
  const job = latestJob(head.id)
  const st = job && job.status
  if (job && st === 'succeeded' && job.video_url) return { label: '已出片', cls: 'ok' }
  if (st === 'generating' || st === 'pending' || st === 'queued') return { label: '生成中', cls: 'warn' }
  if (st === 'failed') return { label: '失败', cls: 'bad' }
  if (st === 'cancelled') return { label: '已取消', cls: 'dim' }
  // 批量在途兜底：任务列表里还没有这条、但批次正在跑它
  const b = store.videoBatch
  if (b && b.status === 'generating' && (b.shot_ids || []).includes(head.id)) {
    return { label: '生成中', cls: 'warn' }
  }
  return { label: '未生成', cls: 'dim' }
}

function playableUrl(head) {
  const job = latestJob(head.id)
  if (job && job.status === 'succeeded' && job.video_url) return job.video_url
  // 兜底：任务列表滞后时，镜头自带的成片地址也能播
  return head.video_url || ''
}

function playClip(card) {
  const url = playableUrl(card.head)
  if (!url) return
  store.playingVideo = { url, title: `片段 ${card.no} · ${card.head.subject || ''}` }
}

// ---------------------------------------------------------------------
// 参数编辑（对成员）
// ---------------------------------------------------------------------
const SHOT_TYPES = [
  ['wide', '远景'],
  ['medium', '中景'],
  ['close_up', '特写'],
  ['over_shoulder', '过肩'],
  ['pov', '主观'],
  ['extreme_wide', '大远景'],
  ['extreme_close_up', '大特写'],
]

function onDuration(shot, ev) {
  let v = parseFloat(ev.target.value)
  if (Number.isNaN(v)) { ev.target.value = shot.duration_sec ?? ''; return }
  v = Math.min(15, Math.max(1, Math.round(v * 2) / 2))
  ev.target.value = v
  if (v === Number(shot.duration_sec)) return
  run(`p:${shot.id}`, () => saveShotParams(shot, { duration_sec: v }))
}

function onShotType(shot, ev) {
  const v = ev.target.value
  if (!v || v === shot.shot_type) return
  run(`p:${shot.id}`, () => saveShotParams(shot, { shot_type: v }))
}

// ---------------------------------------------------------------------
// 提示词区（对组头）
// ---------------------------------------------------------------------
function promptText(card) { return shownPrompt(card.head) }
function promptLen(card) { return promptText(card).length }
function auditLines(card) {
  const a = card.head.prompt_audit
  return Array.isArray(a) ? a.filter(Boolean) : []
}

function memberLabel(shot, idx) {
  // 无 group_id 的自成片段：没有 group_index，按行序数
  return `分镜 ${(shot.group_index ?? idx) + 1}`
}

function openEditor(card) { openPromptEditor(card.head) }

// ---------------------------------------------------------------------
// 胶片条：滚动定位到对应卡片
// ---------------------------------------------------------------------
const cardEls = {}
function setCardEl(key, el) {
  if (el) cardEls[key] = el
  else delete cardEls[key]
}
function scrollToCard(key) {
  const el = cardEls[key]
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
</script>

<template>
  <div class="clips-view">
    <!-- 顶部 -->
    <header class="clips-head">
      <div class="head-text">
        <h2 class="clips-title">分镜</h2>
        <p class="clips-sub">
          <template v-if="loadError">加载出问题了</template>
          <template v-else>{{ cards.length }} 个片段 · 共 {{ totalShots }} 镜</template>
        </p>
      </div>
      <button class="ghost small head-refresh" type="button" :disabled="loading" @click="refreshAll">
        <AppIcon name="refresh" /> {{ loading ? '刷新中' : '刷新' }}
      </button>
    </header>

    <!-- 主区：片段卡列表 -->
    <div class="clips-scroll">
      <p v-if="loadError" class="clips-error">
        <AppIcon name="warning" /> {{ loadError }}，点右上角刷新重试
      </p>

      <div v-if="loading && !cards.length" class="clips-empty">
        <span class="breathing">正在加载分镜……</span>
      </div>

      <div v-else-if="!cards.length" class="clips-empty">
        <AppIcon name="film" class="empty-ic" />
        <p>还没有分镜——在右侧对话里说「拆解镜头」</p>
      </div>

      <section
        v-for="card in cards"
        :key="card.key"
        :ref="el => setCardEl(card.key, el)"
        class="clip-card animate-in"
      >
        <!-- 头部行 -->
        <div class="card-head">
          <span class="card-no">片段 {{ card.no }}</span>
          <span class="card-meta">{{ card.members.length }} 个分镜</span>
          <span class="card-meta mono">{{ fmtDur(groupTotal(card)) }}s</span>
          <span class="st" :class="psBadge(card.head).cls">{{ psBadge(card.head).label }}</span>
          <span class="st" :class="videoBadge(card.head).cls">{{ videoBadge(card.head).label }}</span>
          <button
            v-if="playableUrl(card.head)"
            class="small play-btn"
            type="button"
            @click="playClip(card)"
          >
            <AppIcon name="play" /> 播放
          </button>
        </div>

        <!-- 卡体：逐机位 -->
        <div class="card-body">
          <div v-for="(shot, idx) in card.members" :key="shot.id" class="shot-row">
            <span class="row-label">{{ memberLabel(shot, idx) }}</span>
            <input
              class="dur-input mono"
              type="number"
              step="0.5"
              min="1"
              max="15"
              :value="shot.duration_sec ?? ''"
              :disabled="busy[`p:${shot.id}`]"
              title="时长（秒）"
              @change="onDuration(shot, $event)"
            />
            <select
              class="type-select"
              :value="shot.shot_type || ''"
              :disabled="busy[`p:${shot.id}`]"
              title="景别"
              @change="onShotType(shot, $event)"
            >
              <option v-if="!shot.shot_type" value="" disabled>景别</option>
              <option v-for="[val, label] in SHOT_TYPES" :key="val" :value="val">{{ label }}</option>
              <option v-if="shot.shot_type && !SHOT_TYPES.some(t => t[0] === shot.shot_type)" :value="shot.shot_type">
                {{ shot.shot_type }}
              </option>
            </select>
            <span class="subject" :title="shot.subject || ''">{{ shot.subject || '（无主体）' }}</span>
            <button
              class="ghost small row-reroll"
              type="button"
              :disabled="busy[`r:${shot.id}`]"
              title="重出这个片段（组内统一由组头重跑）"
              @click="run(`r:${shot.id}`, () => rerollShot(shot.id))"
            >
              <AppIcon name="refresh" /> 重出
            </button>
          </div>
        </div>

        <!-- 提示词区（组头原文） -->
        <div class="prompt-zone" title="点击进入全屏编辑/审阅" @click="openEditor(card)">
          <p v-if="promptText(card)" class="prompt-preview">{{ promptText(card) }}</p>
          <p v-else class="prompt-preview prompt-none">（这一片还没有提示词，点击去编辑器生成/补写）</p>
          <div class="prompt-meta">
            <span class="mono">{{ promptLen(card) }} 字</span>
            <span v-if="promptLen(card) > 250" class="st warn">超预算</span>
            <AppIcon name="edit" class="meta-ic" />
          </div>
          <ul v-if="auditLines(card).length" class="audit-list">
            <li v-for="(line, i) in auditLines(card)" :key="i" class="audit-item">
              <AppIcon name="warning" /> <span>{{ line }}</span>
            </li>
          </ul>
        </div>

        <!-- 底部动作条 -->
        <div class="card-foot">
          <button class="small" type="button" @click="openEditor(card)">
            <AppIcon name="edit" /> 编辑提示词
          </button>
          <button class="small" type="button" title="批准走全屏编辑器的确认流（D-43 硬闸门）" @click="openEditor(card)">
            <AppIcon name="check" /> 审阅/批准
          </button>
          <button
            class="small foot-reroll"
            type="button"
            :disabled="busy[`r:${card.head.id}`]"
            @click="run(`r:${card.head.id}`, () => rerollShot(card.head.id))"
          >
            <AppIcon name="refresh" /> 重出片段
          </button>
        </div>
      </section>
    </div>

    <!-- 底部胶片条 -->
    <nav v-if="cards.length" class="film-strip">
      <button
        v-for="card in cards"
        :key="card.key"
        class="strip-item"
        type="button"
        @click="scrollToCard(card.key)"
      >
        <AppIcon name="film" />
        <span>片段 {{ card.no }} · {{ fmtDur(groupTotal(card)) }}s</span>
      </button>
    </nav>
  </div>
</template>

<style scoped>
.clips-view {
  display: flex; flex-direction: column;
  flex: 1; min-height: 0; height: 100%;
}

/* ---------- 顶部 ---------- */
.clips-head {
  display: flex; align-items: center; gap: var(--space-3);
  padding: var(--space-4) var(--space-5) var(--space-2);
}
.head-text { min-width: 0; }
.clips-title { margin: 0; font-size: var(--fs-lg); font-weight: 600; color: var(--ink); }
.clips-sub { margin: 2px 0 0; font-size: var(--fs-sm); color: var(--muted); }
.head-refresh { margin-left: auto; flex: none; }

/* ---------- 主滚动区 ---------- */
.clips-scroll {
  flex: 1; min-height: 0;
  overflow-y: auto;
  padding: var(--space-2) var(--space-5) var(--space-5);
}

.clips-empty {
  display: grid; place-items: center; gap: var(--space-3);
  padding: var(--space-8) var(--space-4);
  color: var(--muted); font-size: var(--fs-md); text-align: center;
}
.empty-ic { font-size: 28px; color: var(--dim); }
.clips-error {
  display: flex; align-items: center; gap: var(--space-2);
  margin: 0 0 var(--space-3); padding: var(--space-2) var(--space-3);
  border: 1px solid var(--line); border-radius: var(--radius-md);
  background: var(--surface-2);
  color: var(--muted); font-size: var(--fs-sm);
}

/* ---------- 片段卡 ---------- */
.clip-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: var(--space-3) var(--space-4) var(--space-3);
  margin-top: var(--space-3);
  scroll-margin-top: var(--space-3);
}
.clip-card:hover { border-color: var(--line-strong); }

.card-head {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--line);
}
.card-no { font-size: var(--fs-md); font-weight: 600; color: var(--ink); }
.card-meta { font-size: var(--fs-xs); color: var(--dim); }
.card-meta.mono { color: var(--muted); }
.play-btn { margin-left: auto; flex: none; }

/* 状态徽标：扁平文字标签，颜色只写在文字上（与全站 pill 同口径） */
.st {
  display: inline-block; padding: 0 2px;
  font-size: 11px; color: var(--dim);
}
.st.ok { color: var(--ok); }
.st.warn { color: var(--warn); }
.st.bad { color: var(--bad); }
.st.dim { color: var(--dim); }

/* ---------- 机位行 ---------- */
.card-body { padding: var(--space-2) 0; }
.shot-row {
  display: flex; align-items: center; gap: var(--space-2);
  padding: var(--space-1) 0;
  min-width: 0;
}
.row-label {
  flex: none; width: 62px;
  font-size: var(--fs-xs); color: var(--muted);
}
.dur-input {
  flex: none; width: 64px; height: var(--control-h);
  padding: 0 var(--space-2);
  font-size: var(--fs-xs);
}
.type-select {
  flex: none; height: var(--control-h);
  font-size: var(--fs-xs);
}
.subject {
  flex: 1; min-width: 0;
  font-size: var(--fs-sm); color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.row-reroll { flex: none; }

/* ---------- 提示词区 ---------- */
.prompt-zone {
  cursor: pointer;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  padding: var(--space-2) var(--space-3);
}
.prompt-zone:hover { border-color: var(--line-strong); }
.prompt-preview {
  margin: 0;
  font-size: var(--fs-sm); color: var(--muted); line-height: var(--lh-body);
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3;
  overflow: hidden; white-space: pre-wrap;
}
.prompt-none { color: var(--dim); }
.prompt-meta {
  display: flex; align-items: center; gap: var(--space-2);
  margin-top: var(--space-1);
  font-size: var(--fs-xs); color: var(--dim);
}
.meta-ic { font-size: var(--fs-xs); color: var(--dim); margin-left: auto; }
.audit-list {
  list-style: none; margin: var(--space-2) 0 0; padding: 0;
}
.audit-item {
  display: flex; align-items: flex-start; gap: var(--space-1);
  font-size: var(--fs-xs); color: var(--muted);
  margin-top: 2px;
}
.audit-item svg { flex: none; margin-top: 2px; }

/* ---------- 卡片动作条 ---------- */
.card-foot {
  display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2);
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--line);
}
.foot-reroll { margin-left: auto; }

/* ---------- 底部胶片条 ---------- */
.film-strip {
  display: flex; gap: var(--space-2);
  flex: none;
  overflow-x: auto;
  padding: var(--space-2) var(--space-5) var(--space-3);
  border-top: 1px solid var(--line);
  background: var(--surface);
}
.strip-item {
  display: inline-flex; align-items: center; gap: 6px;
  flex: none;
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  color: var(--muted);
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.strip-item:hover { color: var(--ink); border-color: var(--line-strong); }
</style>
