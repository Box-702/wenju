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
import { store, loadShots, loadVideoJobs, loadAssets, notify } from '../stores/app'
import AppIcon from './AppIcon.vue'
import ShotList from './video/ShotList.vue'
import VideoPlayer from './video/VideoPlayer.vue'

const props = defineProps({
  /** 关闭时折叠 */
  collapsed: { type: Boolean, default: false },
})

const emit = defineEmits(['open-assets'])

// ---- 可用视图（由上下文决定） ----
const hasScript = computed(() => !!store.viewerScript && (store.viewerScript.scenes || []).length > 0)
const hasShots = computed(() => (store.shots || []).length > 0)
const hasVideo = computed(() => (store.videoJobs || []).some((j) => j.status === 'succeeded' && j.video_url))

const views = computed(() => {
  const list = []
  if (hasScript.value) list.push({ key: 'outline', label: '大纲' })
  if (hasShots.value) list.push({ key: 'shots', label: '镜头' })
  list.push({ key: 'assets', label: '资产' })
  return list
})

// 当前视图失效时（比如剧本还没生成就切到了大纲）自动落到可用视图
watch(views, (list) => {
  if (!list.some((v) => v.key === store.sideView)) {
    store.sideView = list[0]?.key || 'assets'
  }
}, { immediate: true })

const script = computed(() => store.viewerScript || { scenes: [] })
const charNames = computed(() => {
  const m = {}
  for (const c of script.value.characters || []) m[c.id] = c.name
  return m
})

/** 大纲：一幕一行，说清这场发生了什么 */
const outline = computed(() =>
  (script.value.scenes || []).map((sc, i) => ({
    id: sc.id,
    no: i + 1,
    title: sc.title || `第 ${i + 1} 场`,
    purpose: sc.purpose || '',
    conflict: sc.conflict || '',
    chars: (sc.characters || []).map((cid) => charNames.value[cid] || cid),
    beats: (sc.beats || []).length,
  })),
)

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

async function refresh() {
  if (!store.pid) return
  try {
    await Promise.all([loadShots(store.pid), loadVideoJobs(), loadAssets(store.pid)])
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

async function copyUrl(a) {
  try {
    await navigator.clipboard.writeText(a.url)
    notify('链接已复制', 'ok')
  } catch {
    notify('复制失败')
  }
}

onMounted(refresh)
watch(() => store.pid, refresh)
watch(() => store.videoBatch?.status, (st) => {
  if (st === 'succeeded' || st === 'running') refresh()
})
</script>

<template>
  <aside class="cp">
    <!-- 视图切换：只显示当前上下文中可用的那些 -->
    <nav class="cp-tabs" role="tablist">
      <button
        v-for="v in views" :key="v.key"
        :class="['cp-tab', { active: store.sideView === v.key }]"
        type="button"
        role="tab"
        @click="store.sideView = v.key"
      >{{ v.label }}</button>
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
        <li v-for="o in outline" :key="o.id" class="ol-item">
          <div class="ol-head">
            <span class="ol-no">{{ o.no }}</span>
            <span class="ol-title">{{ o.title }}</span>
          </div>
          <p v-if="o.purpose" class="ol-purpose">{{ o.purpose }}</p>
          <p v-if="o.conflict" class="ol-conflict">冲突：{{ o.conflict }}</p>
          <div class="ol-meta">
            <span v-if="o.chars.length">{{ o.chars.join('、') }}</span>
            <span v-if="o.beats">{{ o.beats }} 个节拍</span>
          </div>
        </li>
      </ol>
    </div>

    <!-- 镜头：分镜 + 已生成的视频 -->
    <div v-else-if="store.sideView === 'shots'" class="cp-body">
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
          <VideoPlayer :src="currentClip.url" />
          <a class="cp-dl" :href="currentClip.url" download target="_blank" rel="noopener">
            <AppIcon name="download" /> 下载
          </a>
        </div>
      </div>

      <div class="cp-subtitle">镜头（{{ (store.shots || []).length }}）</div>
      <ShotList
        class="cp-shots"
        :shots="store.shots || []"
        :scenes="store.scenes || []"
      />
    </div>

    <!-- 资产：从资产库调出任何东西 -->
    <div v-else class="cp-body">
      <div class="cp-subtitle-row">
        <span class="cp-subtitle">资产库</span>
        <button class="cp-link" type="button" @click="emit('open-assets')">打开资产库 →</button>
      </div>
      <div v-if="!assets.length" class="cp-empty">
        资产库还是空的。生成的白模、成片都会存在这里。
      </div>
      <div v-else class="asset-list">
        <div v-for="a in assets" :key="a.id" class="asset-row">
          <span class="asset-kind">{{ a.kind }}</span>
          <span class="asset-name">{{ a.name }}</span>
          <span class="asset-acts">
            <button v-if="a.url" class="cp-icon" type="button" title="复制链接" @click="copyUrl(a)">
              <AppIcon name="copy" />
            </button>
            <a v-if="a.url" class="cp-icon" :href="a.url" download target="_blank" rel="noopener" title="下载">
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
  background: rgba(255, 255, 255, .03);
  backdrop-filter: var(--glass);
  -webkit-backdrop-filter: var(--glass);
  border-left: 1px solid var(--line);
}

.cp-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
}

.cp-tab {
  height: 26px;
  padding: 0 11px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
  transition: background 130ms var(--ease), color 130ms var(--ease);
}
.cp-tab:hover { color: var(--ink); background: rgba(255, 255, 255, .07); }
.cp-tab.active {
  color: var(--ink);
  background: rgba(255, 255, 255, .1);
  border-color: var(--line);
}

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

.cp-subtitle-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

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
