<script setup>
// =====================================================================
// EpisodesView.vue —— 工作台·分集视频（父页）
//
// 即梦的「分集视频」页：每集一张卡，先看全局状态，再「进入本集」。
// 分镜页只负责当前集，不再重复放这张选集卡（D-99）。
// =====================================================================
import { computed, onMounted, ref } from 'vue'
import { api } from '../../api'
import { store, notify } from '../../stores/app'
import AppIcon from '../AppIcon.vue'

const loading = ref(true)
const error = ref('')
const data = ref({ active: 1, episodes: [] })
const busy = ref(0)

const episodes = computed(() => data.value.episodes || [])
const activeRow = computed(() => episodes.value.find((e) => e.episode === data.value.active) || null)

function statusOf(ep) {
  if (ep.complete) return { label: '已完成', cls: 'ok' }
  if (ep.generated > 0) return { label: `部分出片 ${ep.generated}/${ep.units || 0}`, cls: 'warn' }
  if (ep.started) return { label: '进行中', cls: 'warn' }
  return { label: '未开始', cls: 'dim' }
}
function progressOf(ep) {
  if (!ep.units) return 0
  return Math.round((ep.generated / ep.units) * 100)
}
function canEnter(ep) {
  if (ep.active || ep.complete || ep.episode < data.value.active) return true
  return ep.episode === data.value.active + 1 && !!activeRow.value?.complete
}
function lockNote(ep) {
  if (canEnter(ep)) return ''
  return `先完成第 ${data.value.active} 集`
}

async function load() {
  if (!store.pid) return
  loading.value = true
  error.value = ''
  try {
    data.value = await api(`/projects/${store.pid}/episodes`)
  } catch (e) {
    error.value = e?.message || '分集加载失败'
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function enter(ep) {
  if (!canEnter(ep)) {
    notify(lockNote(ep), 'info')
    return
  }
  busy.value = ep.episode
  try {
    await api(`/projects/${store.pid}/episode/switch?episode=${ep.episode}`, 'POST')
    store.wbView = 'clips'
    await load()
  } catch (e) {
    notify(e?.message || '切换失败', 'error')
  } finally {
    busy.value = 0
  }
}

function play(ep) {
  if (!ep.preview_url) return
  store.playingVideo = { url: ep.preview_url, title: `第 ${ep.episode} 集`, meta: {} }
}
</script>

<template>
  <div class="episodes">
    <header class="ev-head">
      <div>
        <h2>分集视频</h2>
        <p v-if="episodes.length">共 {{ episodes.length }} 集 · 当前第 {{ data.active }} 集</p>
      </div>
      <button class="ghost small" type="button" @click="load">
        <AppIcon name="refresh" /> {{ loading ? '刷新中' : '刷新' }}
      </button>
    </header>

    <div v-if="error" class="ev-error">{{ error }}（点刷新重试）</div>
    <div v-else-if="loading && !episodes.length" class="ev-empty">正在加载分集…</div>
    <div v-else-if="!episodes.length" class="ev-empty">还没有分集。先在剧本页完成分集提案与剧本。</div>

    <section v-else class="ev-grid">
      <article
        v-for="ep in episodes" :key="ep.episode"
        class="ev-card" :class="{ active: ep.active, done: ep.complete }"
      >
        <div class="ev-top">
          <strong>第 {{ ep.episode }} 集</strong>
          <span class="ev-status" :class="statusOf(ep).cls">{{ statusOf(ep).label }}</span>
        </div>
        <div class="ev-stats">
          <span>{{ ep.scenes }} 场</span>
          <span>{{ ep.shots }} 分镜</span>
          <span>{{ ep.units }} 单元</span>
          <span>{{ ep.duration_sec }} 秒</span>
        </div>
        <div class="ev-bar"><i :style="{ width: progressOf(ep) + '%' }"></i></div>
        <div class="ev-foot">
          <span class="ev-note">
            {{ ep.units ? `批准 ${ep.approved}/${ep.units} · 出片 ${ep.generated}/${ep.units}` : '还没有分镜' }}
          </span>
          <div class="ev-actions">
            <button
              class="ghost small" type="button"
              :disabled="!ep.preview_url"
              :title="ep.preview_url ? '播放已出片段' : '本集还没有可播放的片段'"
              @click="play(ep)"
            >
              播放
            </button>
            <button
              class="ghost small" type="button"
              :disabled="!canEnter(ep) || busy === ep.episode"
              :title="lockNote(ep) || '进入本集'"
              @click="enter(ep)"
            >
              <AppIcon v-if="!canEnter(ep)" name="lock" />
              {{ busy === ep.episode ? '进入中…' : ep.active ? '回到本集' : '进入本集' }}
            </button>
          </div>
        </div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.episodes { flex: 1; min-height: 0; overflow: auto; padding: 22px 26px 40px; }
.ev-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; }
.ev-head h2 { margin: 0; font-size: 20px; font-weight: 600; }
.ev-head p { margin: 6px 0 0; color: var(--muted); font-size: 12.5px; }
.ev-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 14px; margin-top: 20px; }
.ev-card { padding: 14px 16px; border: 1px solid var(--line); border-radius: var(--radius-sm); background: var(--surface); }
.ev-card.active { border-color: var(--line-strong); background: var(--sel-bg); }
.ev-card.done { border-color: color-mix(in oklch, var(--ok, #58a86a) 45%, var(--line)); }
.ev-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.ev-status { font-size: 11.5px; padding: 2px 7px; border-radius: 999px; border: 1px solid var(--line); }
.ev-status.ok { color: var(--ok, #58a86a); border-color: color-mix(in oklch, var(--ok, #58a86a) 45%, var(--line)); }
.ev-status.warn { color: var(--warn, #c99a2e); border-color: color-mix(in oklch, var(--warn, #c99a2e) 45%, var(--line)); }
.ev-status.dim { color: var(--muted); }
.ev-stats { display: flex; gap: 12px; margin-top: 12px; color: var(--muted); font-size: 12px; }
.ev-bar { height: 3px; margin-top: 12px; background: var(--surface-2); border-radius: 999px; overflow: hidden; }
.ev-bar i { display: block; height: 100%; background: var(--sel-bar); }
.ev-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; }
.ev-actions { display: flex; gap: 6px; flex: none; }
.ev-note { color: var(--muted); font-size: 11.5px; }
.ev-empty, .ev-error { margin-top: 30px; color: var(--muted); font-size: 13px; }
.ev-error { color: var(--warn, #c99a2e); }
</style>
