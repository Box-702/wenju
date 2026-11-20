<script setup>
// =====================================================================
// EpisodesView.vue —— 工作台·分集视频页（即梦分集页形态，两层结构）
//
// 总览层：每集一张 16:9 封面卡（最新 succeeded 任务首帧），聚合该集
//         角色/场景数、成片进度与动作入口。
// 详情层：点「看片段」进入，横向片段胶片条（组头一条 = 一个片段），
//         点击已出片片段走全局播放器 playUrl。
// 数据全部来自全局 store（shots / videoJobs / overview / videoBatch）；
// 出片批次是全局的，按钮文案如实说明「全部已批准」。
// =====================================================================
import { computed, onMounted, reactive, ref, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import { api } from '../../api'
import {
  store, notify,
  loadShots, loadVideoJobs, loadOverview,
  startVideoBatch, pollVideoBatch, playUrl,
} from '../../stores/app'

// ---------------------------------------------------------------------
// 两层切换（组件内 ref，不动路由）
// ---------------------------------------------------------------------
const openIndex = ref(null) // null=总览层；数字=详情层的 episode_index
const detailEp = computed(
  () => episodes.value.find((e) => e.index === openIndex.value) || null,
)
function openEpisode(ep) { openIndex.value = ep.index }
function backToList() { openIndex.value = null }

const batchRunning = computed(() =>
  store.videoBatch?.status === 'running' || store.videoBatch?.status === 'awaiting_approval')

// 金色进度条：优先按已完成任务数，回落到批次游标（与 HeaderBar 同口径）
const batchPos = computed(() => {
  const b = store.videoBatch
  if (!b?.total) return 0
  const done = Object.keys(b.results || {}).length
  const cursor = Math.min((Number(b.current_index) || 0) + 1, b.total)
  return Math.max(done, cursor > b.total ? b.total : cursor)
})
const batchPercent = computed(() => {
  const b = store.videoBatch
  return b?.total ? Math.round((100 * batchPos.value) / b.total) : 0
})

/** 这一镜所属镜头组的组头 id（成片挂在组头上，按组头记数）。 */
function headIdOf(shot) {
  if (!shot.group_id) return shot.id
  const head = (store.shots || []).find((x) =>
    x.group_id === shot.group_id && x.scene_id === shot.scene_id && x.group_prompt)
  return head ? head.id : shot.id
}

// ---------------------------------------------------------------------
// 片段（= 镜头组，一次生成）：与 ClipsView 同口径的分组
// ---------------------------------------------------------------------
function buildClips(shots) {
  const map = new Map()
  for (const s of shots) {
    const key = s.group_id ? `g:${s.scene_id}|${s.group_id}` : `s:${s.id}`
    if (!map.has(key)) map.set(key, [])
    map.get(key).push(s)
  }
  const out = []
  for (const [, members] of map) {
    members.sort(
      (a, b) => (a.group_index ?? 0) - (b.group_index ?? 0) || (a.order ?? 0) - (b.order ?? 0),
    )
    const head = members.find((m) => m.group_prompt) || members[0]
    out.push({ head, members, order: head.order ?? Math.min(...members.map((m) => m.order ?? 0)) })
  }
  out.sort((a, b) => a.order - b.order)
  return out.map((c, i) => ({ ...c, no: i + 1 }))
}

/** 组头在 videoJobs 里的最新任务（数组按时间序，取最后一条匹配）。 */
function latestJobOf(shotId) {
  let found = null
  for (const j of store.videoJobs || []) {
    if (String(j.shot_id) === String(shotId)) found = j
  }
  return found
}

/** 一个片段的展示状态：done / generating / failed / none + 可播地址与秒数。 */
function clipInfo(clip) {
  const job = latestJobOf(clip.head.id)
  let status = 'none'
  let url = ''
  const st = job && job.status
  if (st === 'succeeded' && job.video_url) { status = 'done'; url = job.video_url }
  else if (st === 'generating' || st === 'pending' || st === 'queued') status = 'generating'
  else if (st === 'failed') status = 'failed'
  else if (st === 'cancelled') status = 'none'
  else if (clip.head.video_url) { status = 'done'; url = clip.head.video_url }
  // 批量在途兜底：任务列表还没有这条、但批次正在跑它
  const b = store.videoBatch
  if (status === 'none' && b && (b.status === 'running' || b.status === 'awaiting_approval')
    && (b.shot_ids || []).some((id) => String(id) === String(clip.head.id))) {
    status = 'generating'
  }
  const dur = clip.members.reduce((n, m) => n + (Number(m.duration_sec) || 0), 0)
  return { status, url, dur }
}

/** 正则扫提示词里的 @名(char_xxx) / @名(loc_xxx)，去重计数。 */
function assetCounts(shots) {
  const chars = new Set()
  const locs = new Set()
  for (const s of shots) {
    const text = [s.group_prompt, s.video_prompt, s.prompt].filter(Boolean).join('\n')
    for (const m of text.matchAll(/@[^\s（）()@，。；;、]{0,24}\((char|loc)_[a-z0-9_]+\)/gi)) {
      const id = m[0].slice(m[0].indexOf('(') + 1, m[0].lastIndexOf(')')).toLowerCase()
      ;(m[1].toLowerCase() === 'char' ? chars : locs).add(id)
    }
  }
  return { chars: chars.size, locs: locs.size }
}

// ---------------------------------------------------------------------
// 按 episode_index 聚合的分集卡片数据
// ---------------------------------------------------------------------
const episodes = computed(() => {
  const map = new Map()
  for (const shot of store.shots || []) {
    const idx = Number(shot.episode_index ?? 0)
    if (!map.has(idx)) map.set(idx, [])
    map.get(idx).push(shot)
  }
  const out = []
  for (const [index, shots] of [...map.entries()].sort((a, b) => a[0] - b[0])) {
    // episode_id 形如 ch_001 或 ch_001#2（同章拆出的第 2 段）
    const episodeId = String(shots.find((s) => s.episode_id)?.episode_id || '')
    const split = episodeId.includes('#')
      ? episodeId.split('#')[0].replace(/^ch_0*/, '') : ''

    const clips = buildClips(shots)
    const infos = clips.map((c) => ({ ...c, info: clipInfo(c) }))
    const total = clips.length
    const done = infos.filter((c) => c.info.status === 'done').length
    const generating = infos.filter((c) => c.info.status === 'generating').length
    const failed = infos.filter((c) => c.info.status === 'failed').length

    const planned = shots.reduce((n, s) => n + (Number(s.duration_sec) || 0), 0)
    const approvedCount = shots.filter((s) => s.prompt_status === 'approved').length
    const assets = assetCounts(shots)

    // 封面：该集最后一个 succeeded 任务的 video_url（任务 shot_id 命中本集镜头或组头）
    const idSet = new Set()
    for (const s of shots) { idSet.add(String(s.id)); idSet.add(String(headIdOf(s))) }
    let coverUrl = ''
    let latestAt = ''
    for (const j of store.videoJobs || []) {
      if (j.status !== 'succeeded' || !j.video_url) continue
      if (!idSet.has(String(j.shot_id))) continue
      const at = String(j.created_at || '')
      if (at >= latestAt) { latestAt = at; coverUrl = j.video_url }
    }

    // 状态徽标：在途 > 全部就绪 > 有失败 > 部分完成（也算生成中）> 未生成
    let badge
    if (generating) badge = { label: `生成中 ${done}/${total}`, cls: 'warn' }
    else if (total && done >= total) badge = { label: '已就绪', cls: 'ok' }
    else if (failed) badge = { label: '有失败', cls: 'bad' }
    else if (done) badge = { label: `生成中 ${done}/${total}`, cls: 'warn' }
    else badge = { label: '未生成', cls: 'dim' }

    out.push({
      index, no: index + 1, split, shots, clips: infos,
      total, done, planned, approvedCount, assets, coverUrl, badge,
    })
  }
  return out
})

const subtitle = computed(() => {
  const n = store.overview?.episodes ?? episodes.value.length
  return n ? `共 ${n} 集` : ''
})

async function refresh() {
  if (!store.pid) return
  try {
    await Promise.all([loadShots(), loadVideoJobs(), loadOverview()])
  } catch { /* 拉不到就先用现有数据 */ }
}

// ---------------------------------------------------------------------
// 动作
// ---------------------------------------------------------------------
/** 「生成剧本」：把该集引导话塞进对话输入框（与 HomePage 建议卡同一通道）。 */
function askScript(ep) {
  store.draft = '继续设计第 ' + ep.no + ' 集的分镜并出片'
  store.draftSeq += 1
  notify('已填到对话输入框，确认后即可发送', 'info')
}

/** 合成成片：按当前配音/字幕偏好提交，随后刷新批次状态。 */
async function finalize(ep) {
  if (!store.pid) return
  try {
    await api(`/projects/${store.pid}/video/finalize`, 'POST', {
      audio_mode: store.videoAudioMode,
      subtitles: store.videoSubtitle,
    })
    notify(`第 ${ep.no} 集成片合成已开始，稍候可在成片里查看`, 'ok')
    await pollVideoBatch()
  } catch (e) {
    notify(`合成成片失败：${e.message}`, 'error')
  }
}

/** 批量出片：批次是全局的（覆盖全部已批准镜头，不只这一集）。
 *  花钱动作两步确认（与 ClipsView 同口径）：第一下上膛，5 秒内再点才提交。 */
const armed = reactive({})
const armTimers = {}
function arm(key) {
  armed[key] = true
  clearTimeout(armTimers[key])
  armTimers[key] = setTimeout(() => { armed[key] = false }, 5000)
}
async function generateBatch(ep) {
  if (!store.pid) return
  if (!ep.approvedCount) {
    notify(`第 ${ep.no} 集还没有已批准的镜头，先去审阅提示词`, 'info')
    return
  }
  const key = `b:${ep.id}`
  if (!armed[key]) { arm(key); return }
  clearTimeout(armTimers[key])
  armed[key] = false
  await startVideoBatch()
}

function playClip(ep, clip) {
  if (clip.info.status !== 'done' || !clip.info.url) return
  playUrl(clip.info.url, `第${ep.no}集 片段${clip.no}`)
}

function pct(ep) {
  if (!ep.total) return 0
  return Math.round((100 * ep.done) / ep.total)
}

function fmtDur(v) { return Math.round((Number(v) || 0) * 10) / 10 }

onMounted(refresh)
watch(() => store.pid, () => { openIndex.value = null; refresh() })
</script>

<template>
  <div class="episodes-view">
    <!-- ================= 顶部全局条 ================= -->
    <header class="ep-head">
      <div class="head-line">
        <button v-if="detailEp" class="small ghost back-btn" type="button" @click="backToList">
          <AppIcon name="arrow-left" /> 分集列表
        </button>
        <h2 v-else class="title">分集视频</h2>
        <h2 v-if="detailEp" class="title">第 {{ detailEp.no }} 集</h2>
        <span v-if="!detailEp && subtitle" class="subtitle">{{ subtitle }}</span>
        <button class="small ghost refresh" type="button" @click="refresh">
          <AppIcon name="refresh" /> 刷新
        </button>
      </div>
      <div v-if="batchRunning" class="batch-bar" :title="`批次全局进行中 ${batchPos}/${store.videoBatch?.total}`">
        <div class="batch-fill" :style="{ width: batchPercent + '%' }" />
        <span class="batch-label">出片批次进行中 {{ batchPos }}/{{ store.videoBatch?.total }}</span>
      </div>
    </header>

    <div class="ep-scroll">
      <!-- 空态：还没有分镜 -->
      <div v-if="!store.shots.length" class="empty">
        <AppIcon name="film" class="empty-ic" />
        <p class="empty-title">还没有分镜——在对话里说「拆解镜头」</p>
      </div>

      <!-- ================= 总览层：分集卡片网格 ================= -->
      <div v-else-if="!detailEp" class="ep-grid">
        <div v-for="ep in episodes" :key="ep.index" class="ep-card">
          <!-- 封面：最后一个 succeeded 任务首帧 -->
          <div class="cover">
            <video
              v-if="ep.coverUrl"
              class="cover-vid"
              :src="ep.coverUrl + '#t=0.1'"
              preload="metadata"
              muted
              playsinline
            />
            <div v-else class="cover-ph">
              <AppIcon name="play" />
            </div>
            <span class="cover-dur mono">约 {{ Math.round(ep.planned) }}s</span>
          </div>

          <div class="card-body">
            <div class="ep-title-row">
              <span class="ep-no">第 {{ ep.no }} 集</span>
              <span v-if="ep.split" class="ep-split">（第 {{ ep.split }} 章拆分）</span>
              <span class="st" :class="ep.badge.cls">{{ ep.badge.label }}</span>
            </div>

            <div class="ep-stats">
              <span>角色 {{ ep.assets.chars }} · 场景 {{ ep.assets.locs }}</span>
              <span class="dot">·</span>
              <span>{{ ep.shots.length }} 镜 · 成片 {{ ep.done }}</span>
            </div>

            <div class="bar" :title="`已出 ${ep.done} / ${ep.total} 个片段`">
              <div class="bar-fill" :style="{ width: pct(ep) + '%' }" />
            </div>

            <div class="ep-actions">
              <button class="small ghost" type="button" @click="askScript(ep)">
                <AppIcon name="edit" /> 生成剧本
              </button>
              <button class="small primary" type="button" @click="openEpisode(ep)">
                看片段 <AppIcon name="chevron-right" />
              </button>
              <button
                class="small"
                type="button"
                :disabled="!ep.coverUrl"
                :title="ep.coverUrl ? '按当前配音/字幕偏好合成整片' : '这一集还没有可合成的成片'"
                @click="finalize(ep)"
              >
                <AppIcon name="film" /> 合成成片
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- ================= 详情层：片段胶片条 ================= -->
      <section v-else class="ep-detail">
        <div class="detail-meta">
          <span v-if="detailEp.split" class="ep-split">（第 {{ detailEp.split }} 章拆分）</span>
          <span class="st" :class="detailEp.badge.cls">{{ detailEp.badge.label }}</span>
          <span class="detail-planned mono">计划约 {{ Math.round(detailEp.planned) }} 秒</span>
        </div>

        <p class="strip-title">片段</p>
        <div class="film-strip">
          <button
            v-for="clip in detailEp.clips"
            :key="clip.head.id"
            class="strip-card"
            type="button"
            :disabled="clip.info.status !== 'done'"
            :title="clip.info.status === 'done' ? '点击播放' : '这一片段还没有成片'"
            @click="playClip(detailEp, clip)"
          >
            <span class="thumb">
              <video
                v-if="clip.info.url"
                class="thumb-vid"
                :src="clip.info.url + '#t=0.1'"
                preload="metadata"
                muted
                playsinline
              />
              <span v-else class="thumb-ph">
                <AppIcon name="film" />
              </span>
              <span v-if="clip.info.status === 'done'" class="thumb-play">
                <AppIcon name="play" />
              </span>
            </span>
            <span class="strip-row">
              <span class="st-dot" :class="clip.info.status" />
              <span>片段 {{ clip.no }} · {{ fmtDur(clip.info.dur) }}s</span>
            </span>
          </button>
        </div>

        <div class="detail-foot">
          <button
            class="small primary"
            type="button"
            :disabled="batchRunning"
            :class="{ danger: armed[`b:${detailEp.id}`] }"
            :title="armed[`b:${detailEp.id}`] ? '再点一次就真的提交批次（会开始花钱）' : '批次覆盖全部已批准镜头，不只这一集'"
            @click="generateBatch(detailEp)"
          >
            <AppIcon name="sparkle" /> {{ armed[`b:${detailEp.id}`] ? '确认提交批次？' : '批量出片（全部已批准）' }}
          </button>
          <button
            class="small"
            type="button"
            :disabled="!detailEp.coverUrl"
            @click="finalize(detailEp)"
          >
            <AppIcon name="film" /> 合成成片
          </button>
          <button class="small ghost" type="button" @click="askScript(detailEp)">
            <AppIcon name="edit" /> 生成剧本
          </button>
        </div>
        <p v-if="batchRunning" class="batch-note">出片批次为全局任务，进行中的进度会反映在各集。</p>

        <div class="detail-stats">
          <span>镜头 {{ detailEp.shots.length }}</span>
          <span class="dot">·</span>
          <span>已批 {{ detailEp.approvedCount }}</span>
          <span class="dot">·</span>
          <span>成片 {{ detailEp.done }}/{{ detailEp.total }}</span>
          <span class="dot">·</span>
          <span>约 {{ Math.round(detailEp.planned) }} 秒</span>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.episodes-view {
  display: flex; flex-direction: column;
  flex: 1; min-height: 0; height: 100%;
  overflow: hidden;
}

/* ---------- 顶部全局条 ---------- */
.ep-head {
  flex: none;
  padding: var(--space-4) var(--space-5) var(--space-3);
  border-bottom: 1px solid color-mix(in oklch, var(--line) 55%, transparent);
}
.head-line {
  display: flex; align-items: center; gap: var(--space-3);
}
.title {
  margin: 0; font-size: var(--fs-xl); font-weight: 650; color: var(--ink);
}
.back-btn {
  display: inline-flex; align-items: center; gap: var(--space-1);
  font-size: var(--fs-md);
}
.subtitle { font-size: var(--fs-sm); color: var(--muted); }
.refresh { margin-left: auto; display: inline-flex; align-items: center; gap: var(--space-1); }

.batch-bar {
  position: relative;
  margin-top: var(--space-3);
  height: 18px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-full);
  overflow: hidden;
}
.batch-fill {
  position: absolute; inset: 0 auto 0 0;
  background: var(--gold-soft);
  border-right: 2px solid var(--gold);
  transition: width 0.4s ease;
}
.batch-label {
  position: relative;
  display: block; text-align: center;
  font-size: var(--fs-xs); line-height: 16px;
  color: var(--gold);
}
.batch-note {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs); color: var(--gold);
}

/* ---------- 滚动区 ---------- */
.ep-scroll {
  flex: 1; min-height: 0;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-6);
}

/* ---------- 总览网格 ---------- */
.ep-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-4);
}
.ep-card {
  overflow: hidden;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
}
.ep-card:hover { border-color: var(--line-strong); }

/* 封面 16:9 */
.cover {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--surface-2);
}
.cover-vid {
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
}
.cover-ph {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  color: var(--dim); font-size: var(--fs-display);
}
.cover-dur {
  position: absolute; right: var(--space-2); bottom: var(--space-2);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: rgba(0, 0, 0, 0.55);
  color: var(--ink);
  font-size: var(--fs-xs);
}

.card-body {
  display: flex; flex-direction: column; gap: var(--space-2);
  padding: var(--space-3) var(--space-4) var(--space-4);
}
.ep-title-row {
  display: flex; align-items: baseline; gap: var(--space-2);
}
.ep-no { font-size: var(--fs-lg); font-weight: 650; color: var(--ink); }
.ep-split { font-size: var(--fs-xs); color: var(--dim); }
.ep-title-row .st { margin-left: auto; align-self: center; }

/* 状态徽标：扁平文字标签（与全站 pill 同口径） */
.st {
  display: inline-block; padding: 0 2px;
  font-size: var(--fs-xs); color: var(--dim);
  white-space: nowrap;
}
.st.ok { color: var(--ok); }
.st.warn { color: var(--warn); }
.st.bad { color: var(--bad); }
.st.dim { color: var(--dim); }

.ep-stats {
  display: flex; align-items: center; gap: var(--space-1);
  font-size: var(--fs-sm); color: var(--muted);
}
.ep-stats .dot { color: var(--dim); }

.bar {
  height: 6px;
  background: var(--surface-2);
  border-radius: var(--radius-full);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: var(--gold);
  border-radius: inherit;
  transition: width 0.3s ease;
}

.ep-actions {
  display: flex; flex-wrap: wrap; gap: var(--space-2);
  margin-top: var(--space-1);
}
.ep-actions button { display: inline-flex; align-items: center; gap: var(--space-1); }

/* ---------- 详情层 ---------- */
.ep-detail {
  display: flex; flex-direction: column; gap: var(--space-3);
}
.detail-meta {
  display: flex; align-items: center; gap: var(--space-3);
}
.detail-planned { font-size: var(--fs-sm); color: var(--muted); }
.strip-title {
  margin: 0;
  font-size: var(--fs-md); font-weight: 600; color: var(--ink);
}

/* 片段胶片条：横向滚动 */
.film-strip {
  display: flex; gap: var(--space-3);
  overflow-x: auto;
  padding: var(--space-1) 0 var(--space-3);
}
.strip-card {
  display: flex; flex-direction: column; gap: var(--space-2);
  flex: none;
  width: 200px;
  padding: var(--space-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--muted);
  font-size: var(--fs-xs);
  text-align: left;
}
.strip-card:disabled { cursor: default; }
.strip-card:not(:disabled):hover { border-color: var(--line-strong); color: var(--ink); }

.thumb {
  position: relative;
  display: block;
  aspect-ratio: 16 / 9;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  overflow: hidden;
}
.thumb-vid {
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
}
.thumb-ph {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  color: var(--dim); font-size: var(--fs-xl);
}
.thumb-play {
  position: absolute; inset: 0;
  display: grid; place-items: center;
  color: var(--ink); font-size: var(--fs-xl);
  opacity: 0;
  background: rgba(0, 0, 0, 0.3);
  transition: opacity 0.15s ease;
}
.strip-card:not(:disabled):hover .thumb-play { opacity: 1; }

.strip-row {
  display: flex; align-items: center; gap: var(--space-2);
  white-space: nowrap;
}
.st-dot {
  flex: none;
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--dim); /* 灰=未生成 */
}
.st-dot.done { background: var(--ok); }        /* 绿=已出片 */
.st-dot.generating { background: var(--warn); } /* 黄=生成中 */
.st-dot.failed { background: var(--bad); }      /* 红=失败 */

.detail-foot button.danger {
  color: var(--bad);
  border-color: color-mix(in oklch, var(--bad) 55%, transparent);
}
.detail-foot {
  display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2);
}
.detail-foot button { display: inline-flex; align-items: center; gap: var(--space-1); }

.detail-stats {
  display: flex; align-items: center; gap: var(--space-1);
  font-size: var(--fs-sm); color: var(--muted);
}
.detail-stats .dot { color: var(--dim); }

/* ---------- 空态 ---------- */
.empty {
  display: grid; place-content: center; justify-items: center;
  min-height: 50%;
  text-align: center;
  gap: var(--space-3);
}
.empty-ic { font-size: 28px; color: var(--dim); }
.empty-title { margin: 0; font-size: var(--fs-md); color: var(--muted); }
</style>
