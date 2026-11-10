<script setup>
// =====================================================================
// VoiceLibraryModal.vue —— 声音库管理（BYOK：拿你的 key 问厂商，勾哪些用哪些）
//
// 为什么要有它：音色池以前硬编码 8 个，用户以为「声音就这么多」；
// 实测 MiniMax /v1/get_voice 返回这把 key 可用的 303 个系统音色。
// 这个浮层把「发现 → 试听 → 勾选」一步做完：
//   - 勾选的进启用池：自动选角先用它们（排在内置 8 个前面），下拉里也排前面；
//   - 内置 8 个永远兜底（勾不勾选都在），没勾选过时行为和以前完全一致；
//   - other 组（厂商不给性别、id 无 male/female 前缀的）只能手动指定给角色，
//     不参与自动选角——猜性别出过「男主开口是女声」的事故，不冒险。
// 试听复用 POST /projects/{pid}/library/preview（念一句样音），需要先进入一部作品。
// =====================================================================
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import { store, notify } from '../stores/app'
import AppIcon from './AppIcon.vue'

const emit = defineEmits(['close'])

const state = reactive({ loading: false, error: '', all: [], enabled: new Set() })
const picked = ref([])          // 勾选的 voice_id（保存前是本地态）
const discovering = ref(false)
const previewing = ref('')

const GROUPS = [
  { key: 'male', label: '男声' },
  { key: 'female', label: '女声' },
  { key: 'other', label: '其他（只能手动指定）' },
]
const grouped = computed(() =>
  GROUPS.map((g) => ({
    ...g,
    items: state.all.filter((v) => (v.gender || 'other') === g.key),
  })).filter((g) => g.items.length),
)

async function loadPool() {
  state.loading = true
  state.error = ''
  try {
    const r = await api('/voices/pool')
    // 内置 8 个不在池表里（永远兜底），列表只展示「发现出来的」；
    // 没发现过时这里是空的——提示用户点发现，而不是冷场。
    state.all = (r.all || []).filter((v) => v.source !== 'clone')
    state.enabled = new Set((r.enabled || []).map((v) => v.voice_id))
    picked.value = [...state.enabled]
  } catch (e) {
    state.error = '加载音色池失败：' + (e.detail || e.message)
  } finally {
    state.loading = false
  }
}

async function discover() {
  discovering.value = true
  state.error = ''
  try {
    const r = await api('/voices/discover', 'POST', {})
    notify(`厂商返回 ${r.count} 个音色`, 'ok')
    await loadPool()
  } catch (e) {
    state.error = '发现音色失败：' + (e.detail || e.message)
  } finally {
    discovering.value = false
  }
}

async function save() {
  try {
    const r = await api('/voices/pool', 'PUT', { voice_ids: picked.value })
    notify(`已启用 ${r.enabled} 个音色（自动选角会优先用它们）`, 'ok')
    state.enabled = new Set(picked.value)
    emit('close')
  } catch (e) {
    notify('保存失败：' + (e.detail || e.message))
  }
}

/** 试听：念一句样音。voice_id 是全局的，但试听接口挂在项目下——没进作品就如实说。 */
async function preview(v) {
  if (!store.pid) { notify('先进入一部作品再试听', 'info'); return }
  if (previewing.value) return
  previewing.value = v.voice_id
  try {
    const res = await fetch(`/api/projects/${store.pid}/library/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice_id: v.voice_id, gender: v.gender, text: '' }),
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      throw new Error(j.detail || res.statusText)
    }
    const url = URL.createObjectURL(await res.blob())
    const audio = new Audio(url)
    audio.play()
    audio.onended = () => URL.revokeObjectURL(url)
  } catch (e) {
    notify(`试听失败：${e.message}`)
  } finally {
    previewing.value = ''
  }
}

function toggle(vid) {
  const i = picked.value.indexOf(vid)
  if (i >= 0) picked.value.splice(i, 1)
  else picked.value.push(vid)
}

const dirty = computed(() => {
  if (picked.value.length !== state.enabled.size) return true
  return picked.value.some((v) => !state.enabled.has(v))
})

onMounted(loadPool)
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')">
    <div class="modal-card wide" role="dialog" aria-label="声音库">
      <div class="modal-head">
        <div>
          <h3 class="modal-title">声音库</h3>
          <p class="modal-sub">用你配置的 MiniMax Key 查询可用音色；勾选的会优先用于自动选角</p>
        </div>
        <button class="modal-close" type="button" aria-label="关闭" @click="emit('close')"><AppIcon name="close" /></button>
      </div>

      <div class="vl-actions">
        <button class="small ghost" type="button" :disabled="discovering" @click="discover">
          {{ discovering ? '查询中…' : '从厂商刷新' }}
        </button>
        <span class="vl-count">
          {{ state.all.length ? `${state.all.length} 个 · 已启用 ${picked.length} 个` : '池还是空的' }}
        </span>
      </div>
      <p v-if="state.error" class="vl-err">{{ state.error }}</p>

      <div v-if="state.loading" class="pop-empty">加载中…</div>
      <div v-else-if="!state.all.length && !state.error" class="pop-empty vl-empty">
        还没查询过。点「从厂商刷新」，把你这把 Key 可用的音色拉回来再勾选。
      </div>

      <div v-else class="vl-groups">
        <section v-for="g in grouped" :key="g.key" class="vl-group">
          <h5>{{ g.label }} <span class="vg-n">{{ g.items.length }}</span></h5>
          <div class="vl-list">
            <label v-for="v in g.items" :key="v.voice_id" class="vl-row">
              <input type="checkbox" :checked="picked.includes(v.voice_id)" @change="toggle(v.voice_id)" />
              <span class="vr-name">{{ v.name }}</span>
              <span class="vr-id">{{ v.voice_id }}</span>
              <button
                class="vr-play"
                type="button"
                :disabled="previewing === v.voice_id"
                title="试听一句"
                @click.prevent="preview(v)"
              >{{ previewing === v.voice_id ? '…' : '试听' }}</button>
            </label>
          </div>
        </section>
      </div>

      <div class="modal-foot">
        <button class="ghost" type="button" @click="emit('close')">取消</button>
        <button class="primary" type="button" :disabled="!dirty" @click="save">
          保存勾选（{{ picked.length }}）
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.vl-actions { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.pop-empty { padding: 14px 8px; font-size: 12px; color: var(--dim); }
.vl-count { font-size: 11.5px; color: var(--dim); }
.vl-err { margin: 0 0 10px; font-size: 12px; color: var(--bad); }
.vl-empty { padding: 22px 8px; text-align: center; }
.vl-groups { max-height: 52vh; overflow-y: auto; }
.vl-group { margin-bottom: 14px; }
.vl-group h5 {
  margin: 0 0 6px; font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .08em;
}
.vg-n { color: var(--dim); font-weight: 400; }
.vl-list { display: flex; flex-direction: column; gap: 1px; }
.vl-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 8px; border-radius: var(--radius-sm);
  font-size: 12.5px; color: var(--ink); cursor: pointer;
}
.vl-row:hover { background: rgba(255, 255, 255, .05); }
.vl-row input[type='checkbox'] { accent-color: var(--accent); flex: none; }
.vr-name { flex: none; max-width: 46%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vr-id {
  flex: 1; min-width: 0; font-family: var(--mono); font-size: 10.5px; color: var(--dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.vr-play {
  flex: none; padding: 2px 9px; font-size: 11px; border-radius: var(--radius-sm);
  background: transparent; border: 1px solid var(--line); color: var(--dim); cursor: pointer;
}
.vr-play:hover:not(:disabled) { color: var(--ink); border-color: var(--line-strong); }
.vr-play:disabled { opacity: .5; cursor: default; }
</style>
