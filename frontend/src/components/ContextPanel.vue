<script setup>
// =====================================================================
// ContextPanel.vue —— 通用侧边栏（对话上下文的视图，不是固定功能面板）
//
// 它显示什么，由**对话当前处于什么阶段**决定，而不是永远摆着三个 tab：
//
//   大纲 —— 用户让人改编时：当前剧本的结构（场次 / 每场发生了什么）
//   镜头 —— 分镜与已生成的视频（含下载）
//   资产 —— 从资产库调出任何东西（角色 / 场景 / 素材 / 成片）
//
// 资产库是**最终的归宿**：改编出来的东西、成片视频都进资产库；
// 侧边栏只是它的快捷视图——所以「资产」这一视图任何时候都在。
//
// 出片时侧边栏会自动弹出并切到「镜头」：这是用户此刻最想看的东西。
// =====================================================================

import { computed, ref, watch, onMounted } from 'vue'
import { store, loadShots, loadVideoJobs, loadAssets, loadOverview } from '../stores/app'
import AppIcon from './AppIcon.vue'
import ShotList from './video/ShotList.vue'
import { openPromptEditor, loadPromptHistory, loadSkills, useSkill } from '../stores/app'
import VideoPlayer from './video/VideoPlayer.vue'

const props = defineProps({
  /** 关闭时折叠 */
  collapsed: { type: Boolean, default: false },
})

const emit = defineEmits(['open-assets', 'open-skills', 'open-output'])

// ---- 可用视图（由上下文决定） ----
const hasScript = computed(() => !!store.viewerScript && (store.viewerScript.scenes || []).length > 0)
const hasShots = computed(() => (store.shots || []).length > 0)
const hasVideo = computed(() => (store.videoJobs || []).some((j) => j.status === 'succeeded' && j.video_url))

const views = computed(() => {
  const list = []
  if (hasScript.value) list.push({ key: 'outline', label: '大纲' })
  list.push({ key: 'shots', label: '镜头' })
  list.push({ key: 'assets', label: '资产' })
  list.push({ key: 'skills', label: '技能' })
  return list
})

// 当前视图失效时（比如剧本还没生成就切到了大纲）自动落到可用视图
watch(views, (list) => {
  if (!list.some((v) => v.key === store.sideView)) {
    store.sideView = list[0]?.key || 'assets'
  }
}, { immediate: true })

const script = computed(() => store.viewerScript || { scenes: [] })

/** 锚图核对总账：多少镜锁了锚、多少镜缺锚 / 没引用。
    缺锚的镜头，角色与场景跨镜会漂——这是批提示词时最先要看的一件事。 */
const anchorAudit = computed(() => {
  const refs = store.assetRefs || {}
  let locked = 0
  let missing = 0
  let none = 0
  for (const shot of store.shots || []) {
    const text = String(shot?.video_prompt || '')
    const ids = []
    for (const m of text.matchAll(/@[^@\s，。；;、（）()]{0,20}\(([a-z]+_[a-z0-9_]+)\)/gi)) {
      if (!ids.includes(m[1])) ids.push(m[1])
    }
    if (!ids.length) { none += 1; continue }
    const missingImg = ids.some((id) => !refs[id]?.image)
    if (missingImg) missing += 1
    else locked += 1
  }
  return { locked, missing, none, total: (store.shots || []).length }
})

const skills = computed(() => store.skills || [])

/** 分类编号：01 / 02 / 03 */
const pad2 = (n) => String(n).padStart(2, '0')
const charNames = computed(() => {
  const m = {}
  for (const c of script.value.characters || []) m[c.id] = c.name
  return m
})

/** 大纲：一幕一行，说清这场发生了什么。点开看细节（节拍 / 台词 / 动作）。 */
const openSceneId = ref('')

const outline = computed(() =>
  (script.value.scenes || []).map((sc, i) => ({
    id: sc.id,
    no: i + 1,
    title: sc.title || `第 ${i + 1} 场`,
    purpose: sc.purpose || '',
    conflict: sc.conflict || '',
    chars: (sc.characters || []).map((cid) => charNames.value[cid] || cid),
    beats: (sc.beats || []).length,
    // 细节：这一场具体写了什么
    beatList: (sc.beats || []).map((b) => ({
      type: b.type || '',
      text: b.text || '',
    })),
    dialogue: (sc.dialogue || []).map((d) => ({
      name: charNames.value[d.character_id || d.speaker] || d.character_id || d.speaker || '',
      text: d.text || d.line || '',
    })),
    action: (sc.action || []).filter(Boolean),
    open: openSceneId.value === sc.id,
  })),
)

function toggleScene(id) {
  openSceneId.value = openSceneId.value === id ? '' : id
}

/** 从侧栏打开提示词编辑器（编辑器在 App 层，这里只负责打开 + 拉历史）。 */
async function onEditPrompt(shot) {
  openPromptEditor(shot)
  await loadPromptHistory(shot.id)
}

/** 镜头视图：已生成的视频优先展示 */
const clips = computed(() =>
  (store.videoJobs || [])
    .filter((j) => j.status === 'succeeded' && j.video_url)
    .map((j) => ({
      id: j.id,
      shot_id: j.shot_id,
      url: j.video_url,
      label: shotLabel(j.shot_id),
    })),
)

function kindZh(k) {
  return { character: '定妆照', location: '场景图', video: '视频', image: '图片', audio: '音频' }[k] || '素材'
}

function shotLabel(shotId) {
  const s = (store.shots || []).find((x) => x.id === shotId)
  return s ? (s.subject || s.id) : (shotId || '')
}

/** 当前选中的视频（点缩略图切播放） */
const currentClip = ref(null)
watch(clips, (list) => {
  if (!currentClip.value && list.length) currentClip.value = list[0]
}, { immediate: true })

const assets = computed(() => store.assets || [])

/** 侧栏「资产」= 资产库的快捷视图，必须同源：
    定妆照 / 场景图（assetRefs，进作品时已加载）+ 成片与素材（media assets）。
    以前只列 media assets——孔乙己明明有 5 张定妆照，侧栏却说「还是空的」。 */
const KIND_ZH = { video: '视频', image: '图片', audio: '音频' }
const sidebarAssets = computed(() => {
  const out = []
  for (const [id, a] of Object.entries(store.assetRefs || {})) {
    if (!a.image) continue // 缺图的在资产库里有告警，这里不占行
    out.push({ id, name: a.name, kind: a.kind, image: a.image })
  }
  for (const a of assets.value) {
    const row = { id: a.id, name: a.name || '未命名', kind: a.kind, url: a.url }
    // 只有真能播/能看的才给媒体键；字幕（srt）之类点了没意义，只留下载
    if (a.kind === 'video') row.video = a.url
    else if (a.kind === 'audio') row.audio = a.url
    else if (a.kind === 'image') row.image = a.url
    out.push(row)
  }
  return out
})

function openPreview(item) {
  store.assetPreview = { ...item }
}

/** 消耗/产出概览（C7）：镜头视图顶部一行，纯创作者语言，无数据就整块不显示。 */
const overview = computed(() => store.overview)
// 用量标签（D-53）：界面不显示费用——只显示「生成多少秒 / 参考视频多长 / 参考图几张」。
const usageLabel = computed(() => {
  const o = overview.value
  if (!o) return ''
  const parts = []
  if (o.video_seconds_planned) parts.push(`生成 ${o.video_seconds_planned.toFixed(0)} 秒`)
  if (o.ref_video_count) {
    parts.push(o.ref_video_seconds
      ? `参考视频 ${o.ref_video_seconds.toFixed(0)} 秒`
      : `参考视频 ${o.ref_video_count} 条`)
  }
  if (o.ref_image_count) parts.push(`参考图 ${o.ref_image_count} 张`)
  return parts.join(' · ')
})

async function refresh() {
  if (!store.pid) return
  try {
    await Promise.all([loadShots(store.pid), loadVideoJobs(), loadAssets(store.pid), loadOverview()])
  } catch {
    /* 拉不到就先用现有数据 */
  }
}

function downloadAsset(a) {
  if (!a.url) return
  const link = document.createElement('a')
  link.href = a.url
  link.download = a.name || 'asset'
  link.target = '_blank'
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

onMounted(() => {
  refresh()
  loadSkills()
})
watch(() => store.pid, refresh)
watch(() => store.videoBatch?.status, (st) => {
  if (st === 'succeeded' || st === 'running') refresh()
})
// 合成完成 → 成片刚落库，侧栏不刷新就看不到（用户实测：资产库有东西右侧栏空着）
watch(() => store.videoFinalize?.status, (st) => { if (st === 'succeeded') refresh() })
// 导入素材后（AssetPanel 改的是 store.assets 之外再拉一次），跟着刷新
watch(() => store.assetRefreshSeq, refresh)
</script>

<template>
  <aside class="cp">
    <!-- 视图切换：只显示当前上下文中可用的那些。
         用产出页那套「编号 + 下划线」分类样式（全站共用 .seg-tabs/.seg-tab）。 -->
    <nav class="cp-tabs seg-tabs" role="tablist">
      <button
        v-for="(v, i) in views" :key="v.key"
        :class="['seg-tab', { active: store.sideView === v.key }]"
        type="button"
        role="tab"
        @click="store.sideView = v.key"
      ><span class="seg-num">{{ pad2(i + 1) }}</span>{{ v.label }}</button>
      <span class="cp-spacer"></span>
      <button class="cp-icon" type="button" title="刷新" aria-label="刷新" @click="refresh">
        <AppIcon name="refresh" />
      </button>
    </nav>

    <!-- 大纲：改编时的上下文 -->
    <div v-if="store.sideView === 'outline'" class="cp-body">
      <div v-if="!outline.length" class="cp-empty">
        还没有剧本。让我帮你改编，或先导入一部作品。
      </div>
      <ol v-else class="outline">
        <li v-for="o in outline" :key="o.id" class="ol-item" :class="{ open: o.open }">
          <button class="ol-head" @click="toggleScene(o.id)">
            <span class="ol-no">{{ o.no }}</span>
            <span class="ol-title">{{ o.title }}</span>
            <span class="ol-toggle">{{ o.open ? '收起' : '细节' }}</span>
          </button>
          <p v-if="o.purpose" class="ol-purpose">{{ o.purpose }}</p>
          <p v-if="o.conflict" class="ol-conflict">冲突：{{ o.conflict }}</p>
          <div class="ol-meta">
            <span v-if="o.chars.length">{{ o.chars.join('、') }}</span>
            <span v-if="o.beats">{{ o.beats }} 个节拍</span>
          </div>

          <!-- 细节：这一场具体写了什么 -->
          <div v-if="o.open" class="ol-detail">
            <div v-if="o.action.length" class="od-sec">
              <span class="od-label">动作</span>
              <p v-for="(a, i) in o.action" :key="'a' + i" class="od-line">{{ a }}</p>
            </div>
            <div v-if="o.beatList.length" class="od-sec">
              <span class="od-label">节拍（{{ o.beatList.length }}）</span>
              <p v-for="(b, i) in o.beatList" :key="'b' + i" class="od-line">
                <span v-if="b.type" class="od-type">{{ b.type }}</span>{{ b.text }}
              </p>
            </div>
            <div v-if="o.dialogue.length" class="od-sec">
              <span class="od-label">台词（{{ o.dialogue.length }}）</span>
              <p v-for="(d, i) in o.dialogue" :key="'d' + i" class="od-line">
                <span v-if="d.name" class="od-who">{{ d.name }}</span>{{ d.text }}
              </p>
            </div>
            <div v-if="!o.action.length && !o.beatList.length && !o.dialogue.length" class="od-line dim">
              这一场还没有写进细节。
            </div>
          </div>
        </li>
      </ol>
    </div>

    <!-- 镜头：分镜 + 已生成的视频 -->
    <div v-else-if="store.sideView === 'shots'" class="cp-body">
      <!-- 消耗/产出概览（C7）：一行说清走到哪、用了多少资源（D-53：不显示费用）。
           数字全来自后端 overview（只聚合已落库的）；
           还没有镜头方案时不显示（「0 镜 · 成片 0」不是信息是噪音）。 -->
      <div v-if="overview && (overview.shots || overview.finalized)" class="cp-overview">
        <span>{{ overview.shots }} 镜</span>
        <span class="ov-sep">·</span>
        <span>已批准 {{ overview.shots_approved }}</span>
        <span class="ov-sep">·</span>
        <span>成片 {{ overview.shots_done }}</span>
        <template v-if="overview.shots_failed">
          <span class="ov-sep">·</span>
          <span class="ov-warn">失败 {{ overview.shots_failed }}</span>
        </template>
        <span v-if="usageLabel" class="ov-usage">{{ usageLabel }}</span>
        <span v-if="overview.finalized" class="ov-final">
          成片 {{ overview.finalized.duration_sec.toFixed(0) }} 秒{{ overview.finalized.dubbed ? ' · 已配音' : '' }}{{ overview.finalized.subtitled ? ' · 有字幕' : '' }}
        </span>
      </div>
      <div v-if="clips.length" class="clip-block">
        <div class="cp-subtitle">已生成视频（{{ clips.length }}）</div>
        <div class="clip-grid">
          <button
            v-for="c in clips" :key="c.id"
            :class="['clip', { active: currentClip?.id === c.id }]"
            type="button"
            @click="currentClip = c"
          >
            <span class="clip-label">{{ c.label }}</span>
          </button>
        </div>
        <div v-if="currentClip" class="clip-player">
          <VideoPlayer :url="currentClip.url" @close="currentClip = null" />
          <a class="cp-dl" :href="currentClip.url" download target="_blank" rel="noopener">
            <AppIcon name="download" /> 下载
          </a>
        </div>
      </div>

      <div class="cp-subtitle-row">
        <span class="cp-subtitle">镜头与提示词（{{ (store.shots || []).length }}）</span>
        <button class="cp-link" type="button" @click="emit('open-output')">打开产出页 →</button>
      </div>
      <!-- 锚图核对总账：缺锚的镜头角色跨镜会漂 -->
      <div v-if="anchorAudit.total" class="cp-audit">
        <span class="ok">已锁锚 {{ anchorAudit.locked }}</span>
        <span v-if="anchorAudit.missing" class="warn">缺图 {{ anchorAudit.missing }}</span>
        <span v-if="anchorAudit.none" class="warn">未引用 {{ anchorAudit.none }}</span>
      </div>
      <ShotList
        class="cp-shots"
        compact
        :shots="store.shots || []"
        :scenes="store.scenes || []"
        @edit-prompt="onEditPrompt"
      />
    </div>

    <!-- 技能：主 Agent 能按需加载的技能 -->
    <div v-else-if="store.sideView === 'skills'" class="cp-body">
      <div class="cp-subtitle-row">
        <span class="cp-subtitle">技能</span>
        <button class="cp-link" type="button" @click="emit('open-skills')">打开技能页 →</button>
      </div>
      <div v-if="!skills.length" class="cp-empty">还没有技能。导入一个 .md 开始。</div>
      <div v-else class="skill-list">
        <button
          v-for="sk in skills"
          :key="sk.name"
          class="skill-row"
          type="button"
          :title="sk.description"
          @click="useSkill(sk)"
        >
          <span class="sk-name">{{ sk.name }}</span>
          <span class="sk-desc">{{ sk.description }}</span>
        </button>
      </div>
    </div>

    <!-- 资产：从资产库调出任何东西 -->
    <div v-else class="cp-body">
      <div class="cp-subtitle-row">
        <span class="cp-subtitle">资产库</span>
        <button class="cp-link" type="button" @click="emit('open-assets')">打开资产库 →</button>
      </div>
      <div v-if="!sidebarAssets.length" class="cp-empty">
        资产库还是空的。生成的定妆照、成片都会存在这里。
      </div>
      <div v-else class="asset-list">
        <div v-for="a in sidebarAssets" :key="a.kind + ':' + a.id" class="asset-row">
          <button class="ar-main" type="button" :title="'点开预览 ' + a.name" @click="openPreview(a)">
            <span class="asset-kind">{{ kindZh(a.kind) }}</span>
            <span class="asset-name">{{ a.name }}</span>
          </button>
          <span class="asset-acts">
            <a class="cp-icon" :href="a.url || a.image || a.video || a.audio" download target="_blank" rel="noopener" title="下载">
              <AppIcon name="download" />
            </a>
          </span>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.cp {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
  background: var(--surface);
  border-left: 1px solid var(--line);
}

/* 页签外观走全站共用的 .seg-tabs/.seg-tab（编号 + 底部亮线），
   这里只补这个面板自己的布局：左右留白与右端的刷新按钮。 */
.cp-tabs {
  padding: 0 10px 0 4px;
  align-items: stretch;
}
.cp-tabs .seg-tab { padding: 7px 10px; min-height: 32px; }
.cp-icon { align-self: center; }

.cp-spacer { flex: 1; }

.cp-icon {
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  background: transparent;
  border: 1px solid transparent;
  color: var(--dim);
  cursor: pointer;
  text-decoration: none;
  transition: color 130ms var(--ease), background 130ms var(--ease);
}
.cp-icon:hover { color: var(--ink); background: rgba(255, 255, 255, .08); }
.cp-icon .app-ic { width: 13px; height: 13px; }

.cp-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 12px 24px;
}

.cp-empty {
  padding: 26px 8px;
  text-align: center;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--dim);
}

.cp-subtitle {
  margin: 0 0 8px;
  font-size: 11.5px;
  letter-spacing: .06em;
  color: var(--dim);
}

/* 消耗/产出概览（C7）：一行小字，复用现有 token，不发明新颜色 */
.cp-overview {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 5px;
  margin: 0 0 10px;
  padding: 6px 9px;
  font-size: 11.5px;
  color: var(--muted);
  background: rgba(255, 255, 255, .04);
  border: 1px solid var(--line, rgba(255, 255, 255, .08));
  border-radius: 8px;
}
.ov-sep { color: var(--dim); }
.ov-warn { color: var(--gold); }
.ov-usage { margin-left: auto; color: var(--dim); }
.ov-final { flex-basis: 100%; color: var(--dim); font-size: 11px; }

.cp-subtitle-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

/* 技能行：文字行，不做方框（与菜单行一致） */
.skill-list { display: flex; flex-direction: column; gap: 2px; }
.skill-row {
  display: flex; flex-direction: column; gap: 2px; width: 100%;
  padding: 7px 9px; border: none; border-radius: var(--radius-sm);
  background: transparent; color: var(--muted);
  font-family: inherit; text-align: left; cursor: pointer;
  transition: background 130ms var(--ease), color 130ms var(--ease);
}
.skill-row:hover { background: rgba(255, 255, 255, .08); color: var(--ink); }
.sk-name { font-size: var(--fs-sm); color: var(--ink); font-weight: 500; }
.sk-desc {
  font-size: var(--fs-xs); color: var(--dim); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}

/* 锚图核对总账 */
.cp-audit {
  display: flex; gap: 10px; flex-wrap: wrap;
  padding: 2px 0 8px; font-size: var(--fs-xs);
}
.cp-audit .ok { color: var(--ok); }
.cp-audit .warn { color: var(--warn); }

.cp-link {
  background: transparent;
  border: 0;
  color: var(--accent);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  padding: 0;
}
.cp-link:hover { text-decoration: underline; }

/* ---- 大纲 ---- */
.outline { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.ol-head {
  display: flex; align-items: baseline; gap: 8px; width: 100%;
  padding: 0; background: none; border: none; color: inherit;
  font-family: inherit; font-size: inherit; text-align: left; cursor: pointer;
}
.ol-toggle {
  margin-left: auto; font-size: 10px; color: var(--dim); flex: none;
  transition: color 130ms var(--ease);
}
.ol-head:hover .ol-toggle { color: var(--ink); }
.ol-detail {
  margin-top: 8px; padding: 9px 10px;
  border-left: 2px solid var(--line-strong);
  background: rgba(255, 255, 255, .03);
  border-radius: 0 var(--radius-md) var(--radius-md) 0;
}
.od-sec { margin-bottom: 8px; }
.od-sec:last-child { margin-bottom: 0; }
.od-label { display: block; font-size: 10px; color: var(--dim); margin-bottom: 3px; letter-spacing: .04em; }
.od-line { margin: 0 0 3px; font-size: var(--fs-xs); color: var(--muted); line-height: 1.6; }
.od-line.dim { color: var(--dim); }
.od-type { color: var(--gold); margin-right: 5px; font-size: 10px; }
.od-who { color: var(--ink); margin-right: 5px; }

.ol-item {
  padding: 10px 12px;
  border-radius: var(--radius-lg);
  background: rgba(255, 255, 255, .04);
  border: 1px solid var(--line);
}

.ol-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; }

.ol-no {
  flex: none;
  width: 18px; height: 18px;
  border-radius: var(--radius-sm);
  display: grid; place-items: center;
  background: rgba(255, 255, 255, .08);
  color: var(--muted);
  font-size: 10.5px;
  font-family: var(--mono);
}

.ol-title { font-size: 13px; font-weight: 600; color: var(--ink); }

.ol-purpose { margin: 0 0 3px; font-size: 12px; line-height: 1.6; color: var(--muted); }
.ol-conflict { margin: 0 0 4px; font-size: 11.5px; line-height: 1.6; color: var(--dim); }
.ol-meta {
  display: flex; gap: 10px; flex-wrap: wrap;
  font-size: 11px; color: var(--dim);
}

/* ---- 镜头 ---- */
.clip-block { margin-bottom: 16px; }

.clip-grid {
  display: flex; flex-wrap: wrap; gap: 6px;
  margin-bottom: 10px;
}

.clip {
  padding: 5px 9px;
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, .05);
  border: 1px solid var(--line);
  color: var(--muted);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.clip:hover { color: var(--ink); background: rgba(255, 255, 255, .09); }
.clip.active { color: var(--ink); background: rgba(255, 255, 255, .13); }

.clip-player { display: flex; flex-direction: column; gap: 8px; }
.clip-player :deep(video) { width: 100%; border-radius: var(--radius-md); }

.cp-dl {
  display: inline-flex; align-items: center; gap: 5px;
  align-self: flex-start;
  padding: 5px 10px;
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, .06);
  border: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
  text-decoration: none;
}
.cp-dl:hover { color: var(--ink); background: rgba(255, 255, 255, .11); }
.cp-dl .app-ic { width: 13px; height: 13px; }

.cp-shots { min-height: 0; }

/* ---- 资产 ---- */
.asset-list { display: flex; flex-direction: column; gap: 4px; }

.asset-row {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 9px;
  border-radius: var(--radius-md);
  background: rgba(255, 255, 255, .035);
  border: 1px solid transparent;
}
.asset-row:hover { background: rgba(255, 255, 255, .07); }

/* 整行是个「点开预览」的按钮：透明底、无边框、内容左对齐 */
.ar-main {
  display: flex; align-items: center; gap: 8px;
  flex: 1; min-width: 0;
  padding: 0; background: transparent; border: 0;
  font: inherit; text-align: left; color: inherit; cursor: pointer;
}

.asset-kind {
  flex: none;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(255, 255, 255, .08);
  color: var(--dim);
  font-size: 10px;
  font-family: var(--mono);
}

.asset-name {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asset-acts { display: flex; gap: 2px; flex: none; }
</style>
