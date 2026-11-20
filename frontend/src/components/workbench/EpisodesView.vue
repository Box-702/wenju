<script setup>
// =====================================================================
// EpisodesView.vue —— 工作台·分集视频页（对标即梦分集卡片）
//
// 数据全部来自全局 store（shots / videoJobs / overview / videoBatch），
// 本页只做「按集聚合 + 每集一卡」的展示与动作入口。
// 注意：出片批次是全局的（不区分集），按钮文案如实写「出片（全部已批准）」。
// =====================================================================
import { computed, onMounted, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import { api } from '../../api'
import {
  store, notify,
  loadShots, loadVideoJobs, loadOverview,
  startVideoBatch, pollVideoBatch,
} from '../../stores/app'

const batchRunning = computed(() =>
  store.videoBatch?.status === 'running' || store.videoBatch?.status === 'awaiting_approval')

/** 出过片（succeeded）的任务所对应的镜头 id 集合。 */
const succeededShotIds = computed(() => {
  const set = new Set()
  for (const j of store.videoJobs || []) {
    if (j.status === 'succeeded' && j.shot_id) set.add(String(j.shot_id))
  }
  return set
})

/** 这一镜所属镜头组的组头 id（成片挂在组头上，按组头记数）。 */
function headIdOf(shot) {
  if (!shot.group_id) return shot.id
  const head = (store.shots || []).find((x) =>
    x.group_id === shot.group_id && x.scene_id === shot.scene_id && x.group_prompt)
  return head ? head.id : shot.id
}

/** 按 episode_index 聚合的分集卡片数据。 */
const episodes = computed(() => {
  const map = new Map()
  for (const shot of store.shots || []) {
    const idx = Number(shot.episode_index ?? 0)
    if (!map.has(idx)) map.set(idx, [])
    map.get(idx).push(shot)
  }
  const succeeded = succeededShotIds.value
  const out = []
  for (const [index, shots] of [...map.entries()].sort((a, b) => a[0] - b[0])) {
    // episode_id 形如 ch_001 或 ch_001#2（同章拆出的第 2 段）
    const episodeId = String(shots.find((s) => s.episode_id)?.episode_id || '')
    const split = episodeId.includes('#')
      ? episodeId.split('#')[0].replace(/^ch_0*/, '') : ''

    const idSet = new Set()
    for (const s of shots) { idSet.add(String(s.id)); idSet.add(String(headIdOf(s))) }

    const done = shots.filter((s) => succeeded.has(String(s.id)) || succeeded.has(String(headIdOf(s)))).length
    const planned = shots.reduce((n, s) => n + (Number(s.duration_sec) || 0), 0)
    const approvedCount = shots.filter((s) => s.prompt_status === 'approved').length

    // 该集最近一条 succeeded 任务的 video_url（任务 shot_id 命中本集镜头或组头）
    let latestUrl = ''
    let latestAt = ''
    for (const j of store.videoJobs || []) {
      if (j.status !== 'succeeded' || !j.video_url) continue
      if (!idSet.has(String(j.shot_id))) continue
      const at = String(j.created_at || '')
      if (at >= latestAt) { latestAt = at; latestUrl = j.video_url }
    }

    out.push({ index, shotCount: shots.length, done, planned, approvedCount, latestUrl, split })
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

/** 出片：批次是全局的，这里只保证「这一集至少有已批准镜头」再触发。 */
async function generate(ep) {
  if (!store.pid) return
  if (!ep.approvedCount) {
    notify(`第 ${ep.index + 1} 集还没有已批准的镜头，先去审阅提示词`, 'info')
    return
  }
  await startVideoBatch()
}

/** 合成成片：按当前配音/字幕偏好提交，随后刷新批次状态。 */
async function finalize() {
  if (!store.pid) return
  try {
    await api(`/projects/${store.pid}/video/finalize`, 'POST', {
      audio_mode: store.videoAudioMode,
      subtitles: store.videoSubtitle,
    })
    notify('成片合成已开始，稍候可在成片里查看', 'ok')
    await pollVideoBatch()
  } catch (e) {
    notify(`合成成片失败：${e.message}`, 'error')
  }
}

function playLatest(ep) {
  if (!ep.latestUrl) return
  store.playingVideo = { url: ep.latestUrl, title: `第 ${ep.index + 1} 集 · 最新成片` }
}

function pct(ep) {
  if (!ep.shotCount) return 0
  return Math.round((100 * ep.done) / ep.shotCount)
}

onMounted(refresh)
watch(() => store.pid, refresh)
</script>

<template>
  <div class="episodes-view">
    <header class="ep-head">
      <div class="head-line">
        <h2 class="title">分集视频</h2>
        <span v-if="subtitle" class="subtitle">{{ subtitle }}</span>
        <button class="small ghost refresh" @click="refresh">
          <AppIcon name="refresh" /> 刷新
        </button>
      </div>
      <p v-if="batchRunning" class="batch-note">出片批次进行中，各集进度会自动更新。</p>
    </header>

    <div class="ep-scroll">
      <!-- 空态：还没有分镜 -->
      <div v-if="!store.shots.length" class="empty">
        <p class="empty-title">还没有分镜——在对话里说「拆解镜头」</p>
      </div>

      <!-- 每集一卡 -->
      <div v-else class="ep-grid">
        <div v-for="ep in episodes" :key="ep.index" class="ep-card">
          <div class="ep-title-row">
            <span class="ep-no">第 {{ ep.index + 1 }} 集</span>
            <span v-if="ep.split" class="ep-split">（第 {{ ep.split }} 章拆分）</span>
            <span class="ep-status" :class="{ full: ep.done >= ep.shotCount && ep.shotCount > 0 }">
              <AppIcon v-if="ep.done >= ep.shotCount && ep.shotCount > 0" name="check" />
              <AppIcon v-else name="film" />
            </span>
          </div>

          <div class="ep-stats">
            <span>{{ ep.shotCount }} 镜</span>
            <span class="dot">·</span>
            <span>成片 {{ ep.done }}</span>
            <span class="dot">·</span>
            <span>约 {{ Math.round(ep.planned) }} 秒</span>
          </div>

          <div class="bar" :title="`已出 ${ep.done} / ${ep.shotCount} 镜`">
            <div class="bar-fill" :style="{ width: pct(ep) + '%' }" />
          </div>

          <div class="ep-actions">
            <button
              class="small primary"
              :disabled="batchRunning"
              title="批次覆盖全部已批准镜头，不只这一集"
              @click="generate(ep)"
            >出片（全部已批准）</button>
            <button class="small ok" @click="finalize">合成成片</button>
            <button v-if="ep.latestUrl" class="small ghost" @click="playLatest(ep)">
              <AppIcon name="play" /> 播放最新
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.episodes-view {
  display: flex; flex-direction: column;
  flex: 1; min-height: 0; height: 100%;
  overflow: hidden;
}
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
.subtitle { font-size: var(--fs-sm); color: var(--muted); }
.refresh { margin-left: auto; display: inline-flex; align-items: center; gap: var(--space-1); }
.batch-note {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs); color: var(--gold);
}

.ep-scroll {
  flex: 1; min-height: 0;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5) var(--space-6);
}
.ep-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-3);
}
.ep-card {
  display: flex; flex-direction: column; gap: var(--space-2);
  padding: var(--space-4);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
}
.ep-title-row {
  display: flex; align-items: baseline; gap: var(--space-2);
}
.ep-no { font-size: var(--fs-lg); font-weight: 650; color: var(--ink); }
.ep-split { font-size: var(--fs-xs); color: var(--dim); }
.ep-status {
  margin-left: auto; align-self: center;
  font-size: var(--fs-md); color: var(--muted);
}
.ep-status.full { color: var(--ok); }
.ep-stats {
  display: flex; align-items: center; gap: var(--space-1);
  font-size: var(--fs-sm); color: var(--muted);
}
.ep-stats .dot { color: var(--dim); }
.bar {
  height: 6px;
  background: var(--surface-2);
  border-radius: var(--radius-full, 999px);
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

.empty {
  display: grid; place-content: center; justify-items: center;
  min-height: 50%;
  text-align: center;
}
.empty-title { margin: 0; font-size: var(--fs-md); color: var(--muted); }
</style>
