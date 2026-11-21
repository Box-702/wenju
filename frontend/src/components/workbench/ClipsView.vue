<script setup>
// =====================================================================
// ClipsView.vue —— 工作台·分镜页（即梦「分集视频」形态，整集同页）
//
// 与即梦对齐的三个决定（2026-11-21 用户拍板，取代前一天的「单片段视图」，
// 理由：逐个点片段才能看到提示词太低效——「每一集的分段的提示词应该
// 放在同一页里面」）：
//   1. 整集同页：当前集的全部片段竖排在一列里，每张卡 = 片段头 +
//      资产 chip + 场景锚 + 各分镜提示词（AssetRefText 行内渲染）+
//      审计 + 出片按钮。胶片条降级为「锚点导航」：点缩略图滚到对应卡。
//   2. 出片流程闸门（学即梦「完成每一项才会放行出片」）：
//      剧本 → 分镜 → 提示词批准 → 可出片，四步状态条常驻页头；
//      「可出片」在本集全部提示词批准前是锁定态。单张卡的花钱按钮
//      仍受「该片段已批准」约束（D-43 不变）+ 两步确认（第一下变红）。
//   3. 右侧预览 = **当前集全部已出片片段连着放**：播放列表按片段顺序
//      接力，进度条带章节刻度。
// 文案禁费用/积分字样（D-53），只报「项 / 秒」。
// =====================================================================
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import AssetRefText from '../AssetRefText.vue'
import {
  store, notify,
  shownPrompt, groupHeadOf,
  loadShots, loadVideoJobs, loadAssetRefs,
  openPromptEditor, saveShotParams,
  rerollShot, submitVideoJob, submitSampleVideo,
  startFinalize, ensureFinalizePolling,
} from '../../stores/app'

// ---------------------------------------------------------------------
// 数据：分组卡片 → 集 → 选中片段（选中 = 高亮 + 连播锚点，不再切视图）
// ---------------------------------------------------------------------
const loading = ref(true)
const loadError = ref('')
async function refreshAll() {
  loading.value = true
  loadError.value = ''
  try {
    // assetRefs 是 @引用 chip 的数据源：本页自己兜底拉一次，别依赖别的视图先加载
    await Promise.all([loadShots(), loadVideoJobs(), loadAssetRefs()])
  } catch (e) {
    loadError.value = e?.message || '分镜数据加载失败'
  } finally {
    loading.value = false
  }
}
onMounted(refreshAll)

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
      episode: head.episode_id || 'ep_1',
      order: head.order ?? Math.min(...members.map(m => m.order ?? 0)),
    })
  }
  out.sort((a, b) => a.order - b.order)
  return out.map((c, i) => ({ ...c, no: i + 1 }))
})

const episodes = computed(() => {
  const seen = new Map()
  for (const c of cards.value) {
    if (!seen.has(c.episode)) seen.set(c.episode, [])
    seen.get(c.episode).push(c)
  }
  return [...seen.entries()].map(([id, cs], i) => ({ id, no: i + 1, cards: cs }))
})

const epId = ref('')
const ep = computed(() => {
  const eps = episodes.value
  if (!eps.length) return null
  return eps.find(e => e.id === epId.value) || eps[0]
})
watch(ep, e => { if (e && e.id !== epId.value) epId.value = e.id })

const cardKey = ref('')
const card = computed(() => {
  const cs = ep.value?.cards || []
  if (!cs.length) return null
  return cs.find(c => c.key === cardKey.value) || cs[0]
})
watch(card, c => { if (c && c.key !== cardKey.value) cardKey.value = c.key })

// ---------------------------------------------------------------------
// 片段卡口径（同页多卡：全部按「传入卡片」取值，不再只有一张卡）
// ---------------------------------------------------------------------
function groupTotal(c) {
  return c.members.reduce((a, m) => a + Math.max(0, Number(m.duration_sec) || 0), 0)
}
function latestJob(id) {
  let found
  for (const j of store.videoJobs || []) if (j.shot_id === id) found = j
  return found
}
function playableUrl(c) {
  const job = latestJob(c.head.id)
  if (job && job.status === 'succeeded' && job.video_url) return job.video_url
  return c.head.video_url || ''
}
function videoBadge(c) {
  const job = latestJob(c.head.id)
  const st = job && job.status
  if (job && st === 'succeeded' && job.video_url) return { label: '已出片', cls: 'ok' }
  if (st === 'generating' || st === 'pending' || st === 'queued') return { label: '生成中', cls: 'warn' }
  if (st === 'failed') return { label: '失败', cls: 'bad' }
  if (st === 'cancelled') return { label: '已取消', cls: 'dim' }
  return { label: '未生成', cls: 'dim' }
}
function promptStatusBadge(c) {
  const s = c.head.prompt_status
  if (s === 'approved') return { label: '已批准', cls: 'ok' }
  if (s === 'needs_revision') return { label: '需修改', cls: 'bad' }
  return { label: '待审', cls: 'warn' }
}
function isApprovedOf(c) {
  return c?.head.prompt_status === 'approved'
}
function approvalTitle(c) {
  return isApprovedOf(c) ? '' : '提示词还没批准，先点提示词文本进审阅器批准'
}

// 资产 chip：从片段全部文本扫 @名(char_xxx)/@名(loc_xxx)
const REF_RE = /@[^@\s]{1,20}\((?:char|loc)_[a-z0-9_]+\)/gi
function assetChipsOf(c) {
  if (!c) return []
  const text = [shownPrompt(c.head), ...c.members.map(m => `${m.subject || ''} ${m.action || ''} ${m.video_prompt || ''}`)].join(' ')
  const ids = new Set()
  for (const m of text.matchAll(REF_RE)) {
    const id = m[0].match(/\(([a-z]+_[a-z0-9_]+)\)$/)?.[1]
    if (id) ids.add(id)
  }
  const refs = store.assetRefs || {}
  const out = [...ids].map(id => ({ id, ...refs[id] })).filter(r => r.name)
  return [...out.filter(r => r.kind !== 'location'), ...out.filter(r => r.kind === 'location')]
}
// 场景锚行：提示词里第一个 loc 引用原样交给 AssetRefText
function sceneAnchorOf(c) {
  if (!c) return ''
  return shownPrompt(c.head).match(/@[^@\s，。；;、（）()]{1,20}\(loc_[a-z0-9_]+\)/)?.[0] || ''
}

// ---------------------------------------------------------------------
// 出片流程闸门（J4）：剧本 → 分镜 → 提示词批准 → 可出片。
// 学即梦：没有完成上一步，下一步不放行——「可出片」在本集全部片段
// 的提示词都批准之前始终是锁定态，光标和按钮都过不去。
// ---------------------------------------------------------------------
const gate = computed(() => {
  const cs = ep.value?.cards || []
  const total = cs.length
  const approved = cs.filter(c => isApprovedOf(c)).length
  const scripted = (store.versions || []).length > 0
  const boarded = cards.value.length > 0
  const allApproved = total > 0 && approved === total
  const steps = [
    {
      key: 'script', label: '剧本',
      state: scripted ? 'done' : 'todo',
      note: scripted ? '已就绪' : '先生成剧本',
    },
    {
      key: 'board', label: '分镜',
      state: boarded ? 'done' : scripted ? 'todo' : 'locked',
      note: boarded ? `${total} 个片段` : '先拆解镜头',
    },
    {
      key: 'approve', label: '提示词批准',
      state: !boarded ? 'locked' : allApproved ? 'done' : approved > 0 ? 'partial' : 'todo',
      note: boarded ? `${approved}/${total} 已批准` : '先完成分镜',
    },
    {
      key: 'render', label: '可出片',
      state: boarded && allApproved ? 'ready' : 'locked',
      note: boarded && allApproved ? '全部就绪' : '批准全部提示词后解锁',
    },
  ]
  return { steps, total, approved, allApproved }
})
const showGate = computed(() => cards.value.length > 0 || (store.versions || []).length > 0)

// 合成全片（即梦分镜页顶栏同款）：把已出片段拼成整集成片（配音/字幕按设置）。
// 闸门未过（提示词没批全）时禁用；没有已出片段由 startFinalize 自己提示。
const finalizing = computed(() => store.videoFinalize?.status === 'running')
function composeAll() {
  if (!gate.value.allApproved || finalizing.value) return
  startFinalize()
  ensureFinalizePolling()
}

// ---------------------------------------------------------------------
// 花钱动作：在途锁 + 两步确认（第一下变红问「确认？」，5 秒内再点才提交）。
// 同页多卡：armed/busy 的 key 带「动作:片段」，各卡互不干扰。
// ---------------------------------------------------------------------
const busy = reactive({})
async function run(key, fn) {
  if (busy[key]) return
  busy[key] = true
  try { await fn() } finally { busy[key] = false }
}
const armed = reactive({})
const armTimers = {}
function arm(key) {
  armed[key] = true
  clearTimeout(armTimers[key])
  armTimers[key] = setTimeout(() => { armed[key] = false }, 5000)
}
function disarm(key) { armed[key] = false; clearTimeout(armTimers[key]) }
async function confirmRun(key, fn) {
  if (!armed[key]) { arm(key); return }
  disarm(key)
  await run(key, fn)
}
onBeforeUnmount(() => Object.values(armTimers).forEach(clearTimeout))

function moneyAct(c, kind, fn) {
  const key = `${kind}:${c.key}`
  return () => {
    if (!isApprovedOf(c)) { notify('请先点这段提示词文本进审阅器批准', 'info'); return }
    confirmRun(key, fn)
  }
}

// ---------------------------------------------------------------------
// 整集连播播放器：已出片片段按顺序接力，章节刻度；点胶片条/卡片跳位
// ---------------------------------------------------------------------
const clips = computed(() => {
  const cs = (ep.value?.cards || []).filter(c => playableUrl(c))
  return cs.map(c => ({ key: c.key, no: c.no, url: playableUrl(c), sec: groupTotal(c) }))
})
const segStart = computed(() => {
  const out = []
  let t = 0
  for (const c of clips.value) { out.push(t); t += c.sec }
  return out
})
const totalSec = computed(() =>
  clips.value.length
    ? segStart.value[segStart.value.length - 1] + clips.value[clips.value.length - 1].sec
    : 0)

const videoEl = ref(null)
const idx = ref(0)
const playing = ref(false)
const muted = ref(false)
const curIn = ref(0)          // 当前片段内秒数
const cur = computed(() => (segStart.value[idx.value] ?? 0) + curIn.value)

function clipOfCard(key) { return clips.value.findIndex(c => c.key === key) }
watch(card, c => {
  const i = c ? clipOfCard(c.key) : -1
  if (i >= 0 && i !== idx.value) { idx.value = i; curIn.value = 0 }
})
watch(idx, async () => {
  await nextTick()
  const v = videoEl.value
  if (!v) return
  v.currentTime = 0
  curIn.value = 0
  if (playing.value) v.play().catch(() => { playing.value = false })
})
function onTimeUpdate() { curIn.value = videoEl.value?.currentTime || 0 }
function onEnded() {
  if (idx.value < clips.value.length - 1) idx.value += 1
  else playing.value = false
}
function togglePlay() {
  const v = videoEl.value
  if (!v) return
  if (v.paused) { playing.value = true; v.play().catch(() => { playing.value = false }) }
  else { v.pause(); playing.value = false }
}
function toggleMute() {
  muted.value = !muted.value
  if (videoEl.value) videoEl.value.muted = muted.value
}
function seekGlobal(t) {
  const cs = clips.value
  if (!cs.length) return
  let i = cs.length - 1
  for (let k = 0; k < cs.length; k++) {
    if (t < segStart.value[k] + cs[k].sec) { i = k; break }
  }
  const local = Math.max(0, Math.min(t - segStart.value[i], cs[i].sec - 0.1))
  if (i === idx.value) {
    if (videoEl.value) { videoEl.value.currentTime = local; curIn.value = local }
  } else {
    idx.value = i
    nextTick(() => { if (videoEl.value) { videoEl.value.currentTime = local; curIn.value = local } })
  }
  if (!playing.value) togglePlay()
}
function onScrubClick(e) {
  const bar = e.currentTarget
  const r = bar.getBoundingClientRect()
  const frac = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width))
  seekGlobal(frac * totalSec.value)
}
// 拖动中抑制 click：拖完 0px 内才算点击（selectCard 开头判 dragged）
const stripEl = ref(null)
let dragOrigin = null
const dragged = ref(false)
function onStripDown(e) {
  if (e.button !== 0 || !stripEl.value) return
  dragOrigin = { x: e.clientX, left: stripEl.value.scrollLeft }
  dragged.value = false
}
function onStripMove(e) {
  if (!dragOrigin || !stripEl.value) return
  const dx = e.clientX - dragOrigin.x
  if (Math.abs(dx) > 6) dragged.value = true
  if (dragged.value) stripEl.value.scrollLeft = dragOrigin.left - dx
}
function onStripUp() {
  dragOrigin = null
  // click 在 pointerup 之后触发，留一拍让 selectCard 读到 dragged 再复位
  setTimeout(() => { dragged.value = false }, 0)
}
function onStripWheel(e) {
  // 竖向滚轮/触控板纵滚映射为横滚（胶片条是横向溢出）
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && stripEl.value) {
    e.preventDefault()
    stripEl.value.scrollLeft += e.deltaY
  }
}

/** 点卡片 / 胶片条：高亮该片段 + 把它滚进视野 + 连播跳到对应段首。
 *  不强制播放——用户可能只是想编辑这一段。 */
function selectCard(c) {
  if (dragged.value) return
  cardKey.value = c.key
  // getElementById 按字面值匹配（key 里的 : | 不用转义；CSS.escape 是给
  // querySelector 用的，用在这里反而永远匹配不上）
  const el = document.getElementById(`seg-${c.key}`)
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  const i = clipOfCard(c.key)
  if (i >= 0 && i !== idx.value) {
    idx.value = i
    nextTick(() => { if (videoEl.value) { videoEl.value.currentTime = 0; curIn.value = 0 } })
  }
}

function fmt(t) {
  const s = Math.max(0, Math.floor(t || 0))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

// ---------------------------------------------------------------------
// 分镜参数（时长 chip，即梦行内下拉）
// ---------------------------------------------------------------------
const DUR_OPTS = [3, 3.5, 4, 4.5, 5, 5.5, 6]
const durBusy = reactive({})
async function setDuration(shot, val) {
  const v = Number(val)
  if (!Number.isFinite(v) || v < 1 || v > 15) return
  if (durBusy[shot.id]) return
  durBusy[shot.id] = true
  await saveShotParams(shot, { duration_sec: v })
  durBusy[shot.id] = false
}

const hasAny = computed(() => cards.value.length > 0)
</script>

<template>
  <div class="clips">
    <header class="cl-head">
      <div class="crumb">
        <span class="crumb-root" @click="store.wbView = null">分镜视频</span>
        <AppIcon name="chevron-right" />
        <div class="ep-tabs">
          <button
            v-for="e in episodes" :key="e.id"
            class="ep-tab" :class="{ on: ep && e.id === ep.id }"
            @click="epId = e.id"
          >第 {{ e.no }} 集</button>
        </div>
        <template v-if="ep">
          <span class="crumb-cur">共 {{ ep.cards.length }} 个片段</span>
        </template>
      </div>
      <button class="ghost small" type="button" :disabled="loading" @click="refreshAll">
        <AppIcon name="refresh" /> {{ loading ? '刷新中' : '刷新' }}
      </button>
    </header>

    <!-- 出片流程闸门（即梦式：完成每一步才放行下一步） -->
    <nav v-if="showGate" class="gate" aria-label="出片流程">
      <template v-for="(s, i) in gate.steps" :key="s.key">
        <span v-if="i" class="g-sep"><AppIcon name="chevron-right" /></span>
        <span class="g-step" :class="s.state" :title="s.note">
          <span class="g-num">{{ i + 1 }}</span>
          <span class="g-label">{{ s.label }}</span>
          <span class="g-note">{{ s.note }}</span>
          <AppIcon v-if="s.state === 'done' || s.state === 'ready'" name="check" class="g-ic" />
          <AppIcon v-else-if="s.state === 'locked'" name="lock" class="g-ic" />
        </span>
      </template>
      <button
        class="g-final" type="button"
        :class="{ ready: gate.allApproved }"
        :disabled="!gate.allApproved || finalizing"
        :title="gate.allApproved ? '把已出片的片段拼成成片（配音/字幕按当前设置）' : '批准全部提示词后可合成全片'"
        @click="composeAll"
      >
        <AppIcon name="film" /> {{ finalizing ? '合成中…' : '合成全片' }}
      </button>
    </nav>

    <div v-if="loadError" class="cl-error">{{ loadError }}（点刷新重试）</div>

    <div v-else-if="!hasAny" class="cl-empty">
      <AppIcon name="film" />
      <p>还没有分镜——在右侧对话里说「拆解镜头」。</p>
    </div>

    <template v-else-if="ep">
      <div class="cl-body">
        <!-- 左：本集全部片段，一页摊开（提示词不用再逐个点开看） -->
        <section class="cards">
          <article
            v-for="c in ep.cards" :key="c.key"
            :id="`seg-${c.key}`"
            class="seg" :class="{ on: card && c.key === card.key }"
            @click="selectCard(c)"
          >
            <div class="sg-meta">
              <span class="sg-title">片段 {{ String(c.no).padStart(2, '0') }}</span>
              <span class="sg-sub">引用 {{ assetChipsOf(c).length }} 项资产 · 约 {{ groupTotal(c).toFixed(1) }} 秒 · {{ c.members.length }} 个分镜</span>
              <span class="badge" :class="promptStatusBadge(c).cls">{{ promptStatusBadge(c).label }}</span>
              <span class="badge" :class="videoBadge(c).cls">{{ videoBadge(c).label }}</span>
            </div>

            <div class="chips">
              <button
                v-for="a in assetChipsOf(c)" :key="a.id"
                class="chip" type="button" :title="`${a.name}（${a.id}）`"
                @click.stop="store.assetPreview = a"
              >
                <img v-if="a.image" :src="a.image" :alt="a.name" />
                <span v-else class="chip-ph"><AppIcon :name="a.kind === 'location' ? 'folder' : 'user'" /></span>
                <span class="chip-name">{{ a.name }}</span>
              </button>
            </div>

            <p v-if="sceneAnchorOf(c)" class="scene-line">
              本片段场景设定在：<AssetRefText :text="sceneAnchorOf(c)" />。
            </p>

            <div class="beats">
              <div v-for="(m, i) in c.members" :key="m.id" class="beat">
                <div class="beat-head" @click.stop>
                  <span class="beat-no">分镜 {{ i + 1 }}</span>
                  <select
                    class="dur-chip" :value="m.duration_sec"
                    :disabled="durBusy[m.id]"
                    title="时长（改动会建新版本）"
                    @change="setDuration(m, $event.target.value)"
                  >
                    <option v-for="d in DUR_OPTS" :key="d" :value="d">{{ d }}s</option>
                    <option v-if="!DUR_OPTS.includes(Number(m.duration_sec))" :value="m.duration_sec">
                      {{ m.duration_sec }}s
                    </option>
                  </select>
                </div>
                <div
                  class="beat-prompt" :title="approvalTitle(c) || '点击编辑并审阅提示词'"
                  @click.stop="openPromptEditor(c.head)"
                >
                  <AssetRefText :text="m.video_prompt || ''" />
                </div>
              </div>
            </div>

            <ul v-if="c.head.prompt_audit && c.head.prompt_audit.length" class="audits">
              <li v-for="(a, i) in c.head.prompt_audit" :key="i">
                <AppIcon name="warning" /> {{ a }}
              </li>
            </ul>

            <div class="acts" @click.stop>
              <button
                class="icon-btn" type="button"
                :class="{ danger: armed[`r:${c.key}`] }"
                :disabled="!isApprovedOf(c) || busy[`r:${c.key}`]"
                :title="armed[`r:${c.key}`] ? '再点一次就真的重出（会提交新任务）' : approvalTitle(c) || '重出该片段'"
                @click="moneyAct(c, 'r', () => rerollShot(c.head.id))()"
              >
                <AppIcon name="refresh" />
              </button>
              <button
                class="small" type="button"
                :class="{ danger: armed[`s:${c.key}`] }"
                :disabled="!isApprovedOf(c) || busy[`s:${c.key}`]"
                :title="armed[`s:${c.key}`] ? '再点一次确认提交' : approvalTitle(c) || '480P 快速验证'"
                @click="moneyAct(c, 's', () => submitSampleVideo(c.head))()"
              >
                <AppIcon name="sparkle" /> {{ busy[`s:${c.key}`] ? '提交中' : (armed[`s:${c.key}`] ? '确认出样片？' : '生成样片') }}
              </button>
              <button
                class="small primary" type="button"
                :class="{ danger: armed[`f:${c.key}`] }"
                :disabled="!isApprovedOf(c) || busy[`f:${c.key}`]"
                :title="armed[`f:${c.key}`] ? '再点一次确认提交' : approvalTitle(c) || '提交正式出片'"
                @click="moneyAct(c, 'f', () => submitVideoJob(c.head))()"
              >
                <AppIcon name="send" /> {{ busy[`f:${c.key}`] ? '提交中' : (armed[`f:${c.key}`] ? '确认出成片？' : '生成成片') }}
              </button>
              <span class="acts-note">约 {{ groupTotal(c).toFixed(1) }} 秒 · 样片为 480P 快速验证</span>
            </div>
          </article>
          <p v-if="!ep.cards.length" class="cards-empty">这一集还没有片段。</p>
        </section>

        <!-- 右：整集连播播放器（即梦预览位） -->
        <aside class="viewer">
          <template v-if="clips.length">
            <video
              ref="videoEl"
              class="vp"
              :src="clips[idx].url"
              :muted="muted"
              playsinline
              preload="metadata"
              @timeupdate="onTimeUpdate"
              @ended="onEnded"
              @pause="playing = false"
              @play="playing = true"
            />
            <div class="vp-chapter">
              第 {{ ep ? ep.no : 1 }} 集 · 当前片段 {{ String(clips[idx].no).padStart(2, '0') }} · 连播 {{ clips.length }} 段
            </div>
            <div class="vp-bar">
              <button class="vp-play" type="button" @click="togglePlay">
                <AppIcon :name="playing ? 'pause' : 'play'" />
              </button>
              <div class="vp-scrub" @click="onScrubClick">
                <span
                  v-for="(c, i) in clips" :key="c.key"
                  class="tick"
                  :style="{ left: (segStart[i] / (totalSec || 1)) * 100 + '%' }"
                />
                <span class="fill" :style="{ width: (cur / (totalSec || 1)) * 100 + '%' }" />
              </div>
              <span class="vp-time mono">{{ fmt(cur) }} / {{ fmt(totalSec) }}</span>
              <button class="vp-mute" type="button" :title="muted ? '取消静音' : '静音'" @click="toggleMute">
                <AppIcon :name="muted ? 'minus' : 'sound'" />
              </button>
            </div>
          </template>
          <div v-else class="vp-none">
            <AppIcon name="film" />
            <p>本集还没有已出片的片段。</p>
            <p class="dim">生成成片后，这里会把整集连着放出来。</p>
          </div>
        </aside>
      </div>

      <!-- 底部胶片条：点缩略图 = 滚到对应片段卡 + 连播跳位（可拖动横滚） -->
      <footer
        ref="stripEl" class="strip"
        @pointerdown="onStripDown" @pointermove="onStripMove"
        @pointerup="onStripUp" @pointerleave="onStripUp" @wheel="onStripWheel"
      >
        <button
          v-for="c in ep.cards" :key="c.key"
          class="strip-item" :class="{ on: card && c.key === card.key }"
          type="button" @click="selectCard(c)"
        >
          <span class="thumb">
            <video v-if="playableUrl(c)" :src="playableUrl(c) + '#t=0.1'" preload="metadata" muted playsinline />
            <AppIcon v-else name="film" />
          </span>
          <span class="strip-label">片段 {{ String(c.no).padStart(2, '0') }} · {{ groupTotal(c).toFixed(0) }}s</span>
          <span class="dot" :class="promptStatusBadge(c).cls" title="提示词状态" />
          <span class="dot v" :class="videoBadge(c).cls" title="出片状态" />
        </button>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.clips { display: flex; flex-direction: column; height: 100%; min-height: 0; }

.cl-head {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 10px 16px; flex: none;
  border-bottom: 1px solid var(--line);
}
.crumb { display: flex; align-items: center; gap: 6px; min-width: 0; color: var(--muted); font-size: 12.5px; }
.crumb :deep(svg) { width: 11px; height: 11px; flex: none; }
.crumb-root { cursor: pointer; }
.crumb-root:hover { color: var(--ink); }
.ep-tabs { display: flex; gap: 2px; }
.ep-tab {
  border: none; background: transparent; color: var(--muted);
  font-size: 12.5px; padding: 3px 8px; border-radius: var(--radius-sm); cursor: pointer;
}
.ep-tab.on {
  color: var(--ink);
  background: var(--sel-bg);
  box-shadow: inset 0 -2px 0 var(--sel-bar);
}
.crumb-cur { color: var(--dim); }

.cl-error {
  margin: 12px 16px; padding: 8px 12px; border-radius: var(--radius-sm);
  border: 1px solid color-mix(in oklch, var(--bad) 45%, transparent);
  color: var(--bad); font-size: 12px; flex: none;
}
.cl-empty {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 10px; color: var(--muted);
}
.cl-empty :deep(svg) { width: 28px; height: 28px; }

/* ---- 出片流程闸门条 ---- */
.gate {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 9px 16px; flex: none;
  border-bottom: 1px solid var(--line);
  background: color-mix(in oklch, var(--surface-2) 55%, transparent);
}
.g-step {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 4px 10px; border-radius: var(--radius-full);
  border: none; background: rgba(255, 255, 255, .05);
  font-size: 12px; color: var(--muted);
}
.g-num {
  display: inline-grid; place-items: center;
  width: 16px; height: 16px; border-radius: 50%;
  border: 1px solid var(--line-strong);
  font-family: var(--mono); font-size: 10px; color: var(--dim);
}
.g-label { color: var(--ink); font-weight: 600; }
.g-note { font-size: 11px; color: var(--dim); }
.g-ic { font-size: 12px; flex: none; }
.g-step.done { background: rgba(255, 255, 255, .08); }
.g-step.done .g-ic { color: var(--ok); }
.g-step.done .g-note { color: var(--muted); }
.g-step.partial { background: color-mix(in oklch, var(--warn) 8%, transparent); }
.g-step.partial .g-note { color: var(--warn); }
.g-step.locked { opacity: .68; }
.g-step.locked .g-ic { color: var(--dim); }
.g-step.ready {
  background: rgba(255, 255, 255, .1);
}
.g-step.ready .g-ic { color: var(--ink); }
.g-step.ready .g-note { color: var(--ink); }
.g-sep { color: var(--dim); display: inline-flex; }
.g-sep :deep(svg) { width: 10px; height: 10px; }
.g-final {
  margin-left: auto; flex: none;
  display: inline-flex; align-items: center; gap: 6px;
  height: 26px; padding: 0 10px; border-radius: var(--radius-full);
  border: none; background: rgba(255, 255, 255, .06);
  color: var(--muted); font-size: 11.5px; cursor: pointer;
}
.g-final:hover:not(:disabled) { color: var(--ink); background: rgba(255, 255, 255, .12); }
.g-final:disabled { opacity: .45; cursor: not-allowed; }
.g-final.ready { color: var(--ink); background: rgba(255, 255, 255, .1); }
.g-final :deep(svg) { width: 12px; height: 12px; }

.cl-body {
  flex: 1; min-height: 0;
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 420px);
  gap: 14px; padding: 14px 16px; overflow: hidden;
}

/* ---- 左：本集全部片段（同页摊开） ---- */
.cards { overflow-y: auto; min-height: 0; display: flex; flex-direction: column; gap: 12px; }
.seg {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-lg); padding: 16px 18px;
  scroll-margin-top: 8px;
  transition: border-color 150ms var(--ease), box-shadow 150ms var(--ease);
}
.seg.on {
  border-color: var(--sel-line);
  box-shadow: inset 3px 0 0 0 var(--sel-bar);
}
.sg-meta { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.sg-title { font-size: 16px; font-weight: 700; color: var(--ink); }
.sg-sub { font-size: 11.5px; color: var(--muted); flex: 1; min-width: 160px; }
.badge {
  font-size: 11px; padding: 2px 8px; border-radius: var(--radius-full);
  border: 1px solid var(--line); color: var(--dim); flex: none;
}
.badge.ok { color: var(--ok); border-color: color-mix(in oklch, var(--ok) 40%, transparent); }
.badge.warn { color: var(--warn); border-color: color-mix(in oklch, var(--warn) 40%, transparent); }
.badge.bad { color: var(--bad); border-color: color-mix(in oklch, var(--bad) 40%, transparent); }

.chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 8px 3px 3px; border-radius: var(--radius-sm);
  border: none; background: rgba(255, 255, 255, .06);
  color: var(--ink); font-size: 11.5px; cursor: pointer;
}
.chip:hover { background: rgba(255, 255, 255, .12); }
.chip img { width: 22px; height: 22px; border-radius: 4px; object-fit: cover; }
.chip-ph {
  width: 22px; height: 22px; border-radius: 4px; display: grid; place-items: center;
  background: var(--panel); color: var(--muted);
}
.chip-ph :deep(svg) { width: 12px; height: 12px; }

.scene-line { margin: 0 0 12px; font-size: 12.5px; color: var(--muted); }

.beats { display: flex; flex-direction: column; gap: 12px; }
.beat { border-top: 1px dashed var(--line); padding-top: 10px; }
.beat-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.beat-no { font-size: 12.5px; font-weight: 600; color: var(--ink); }
.dur-chip {
  height: 22px; padding: 0 4px; border-radius: var(--radius-sm);
  border: 1px solid var(--line); background: var(--surface-2);
  color: var(--gold); font-size: 11.5px; font-family: var(--mono);
}
.beat-prompt {
  font-size: 12.5px; line-height: 1.7; color: var(--ink);
  cursor: text; border-radius: var(--radius-sm); padding: 4px 6px; margin: -4px -6px;
}
.beat-prompt:hover { background: color-mix(in oklch, var(--surface-2) 60%, transparent); }

.audits { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.audits li { display: flex; gap: 6px; align-items: baseline; font-size: 11.5px; color: var(--warn); }
.audits :deep(svg) { width: 11px; height: 11px; flex: none; transform: translateY(1px); }

.acts { display: flex; align-items: center; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.icon-btn {
  width: 30px; height: 28px; display: inline-grid; place-items: center;
  border: none; border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, .06); color: var(--muted); cursor: pointer;
}
.icon-btn:hover { color: var(--ink); background: rgba(255, 255, 255, .12); }
.icon-btn:disabled, .acts button:disabled { opacity: .45; cursor: not-allowed; }
.acts button.danger { color: var(--bad); background: color-mix(in oklch, var(--bad) 14%, transparent); }
.acts-note { font-size: 11px; color: var(--dim); margin-left: auto; }
.cards-empty { padding: 24px; text-align: center; color: var(--dim); font-size: 12.5px; }

/* ---- 右：连播播放器 ---- */
.viewer {
  display: flex; flex-direction: column; gap: 8px; min-height: 0;
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-lg); padding: 12px;
  align-self: start; position: sticky; top: 0;
}
.vp { width: 100%; border-radius: var(--radius-md); background: #000; aspect-ratio: 16/9; object-fit: contain; }
.vp-chapter { font-size: 11.5px; color: var(--muted); }
.vp-bar { display: flex; align-items: center; gap: 8px; }
.vp-play, .vp-mute {
  width: 28px; height: 28px; flex: none; display: inline-grid; place-items: center;
  border: none; border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, .06); color: var(--ink); cursor: pointer;
}
.vp-play:hover, .vp-mute:hover { background: rgba(255, 255, 255, .12); }
.vp-play :deep(svg), .vp-mute :deep(svg) { width: 12px; height: 12px; }
.vp-scrub {
  position: relative; flex: 1; height: 6px; border-radius: 3px;
  background: var(--surface-2); cursor: pointer; overflow: hidden;
}
.vp-scrub .fill { position: absolute; inset: 0 auto 0 0; background: var(--gold); opacity: .8; }
.vp-scrub .tick { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--bg); }
.vp-time { font-size: 11px; color: var(--muted); flex: none; }
.vp-none {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 8px; color: var(--muted); text-align: center; padding: 20px;
}
.vp-none :deep(svg) { width: 26px; height: 26px; }
.vp-none .dim { color: var(--dim); font-size: 11.5px; }

/* ---- 底部胶片条（锚点导航，可拖动横滚） ---- */
.strip {
  flex: none; display: flex; gap: 10px; padding: 12px 16px;
  border-top: 1px solid var(--line);
  overflow-x: auto; scrollbar-width: thin;
  cursor: grab; user-select: none; touch-action: pan-y;
}
.strip:active { cursor: grabbing; }
.strip-item {
  position: relative; flex: none; display: flex; flex-direction: column; gap: 5px;
  padding: 7px 8px; border-radius: var(--radius-md);
  border: none; background: transparent;
  color: var(--muted); font-size: 11.5px; cursor: pointer;
}
.strip-item:hover { background: rgba(255, 255, 255, .05); }
.strip-item.on {
  background: var(--sel-bg);
  color: var(--ink);
  box-shadow: inset 0 2px 0 0 var(--sel-bar);
}
.thumb {
  display: grid; place-items: center; width: 150px; height: 84px;
  border-radius: 6px; overflow: hidden; background: #000;
}
.thumb video { width: 100%; height: 100%; object-fit: cover; pointer-events: none; }
.thumb :deep(svg) { width: 22px; height: 22px; color: var(--dim); }
.strip-label { white-space: nowrap; }
/* 双状态点：左=提示词批准态，右=出片态 */
.dot { position: absolute; top: 8px; left: 8px; width: 6px; height: 6px; border-radius: 50%; background: var(--dim); }
.dot.ok { background: var(--ok); }
.dot.warn { background: var(--warn); }
.dot.bad { background: var(--bad); }
.dot.v { left: auto; right: 8px; }

@media (max-width: 1100px) {
  .cl-body { grid-template-columns: minmax(0, 1fr); overflow-y: auto; }
  .viewer { min-height: 320px; }
}
</style>
