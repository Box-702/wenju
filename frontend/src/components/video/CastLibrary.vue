<script setup>
// =====================================================================
// CastLibrary.vue —— 资产库（Casting Bible）
//
// 一个角色 = 一张定妆图 + 一段固定外貌锚 + 一个音色 + 一条性别判定依据。
// 以前这三样分散在不同结构里、还都靠运行期现算，结果就是「男主开口是御姐音」
// 这种事只能等成片出来才发现。这里把它们摆到一张表上，并且允许改：
//
//   · 音色可以换（换完立刻落库，下次出片就用新的）
//   · 性别可以改（改完音色池重新分配）
//   · 可以点「试听」先听一句，再决定要不要这个声音
//
// 场景同理：一张场景图 + 一段环境描述，出片时作为环境一致性锚。
// =====================================================================

import { computed, ref, watch } from 'vue'
import { api } from '../../api'
import { store, notify } from '../../stores/app'
import AppIcon from '../AppIcon.vue'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['close'])

const loading = ref(false)
const lib = ref(null)
const playing = ref('')       // 正在试听的 voice_id
const saving = ref('')        // 正在保存的角色名

const characters = computed(() => lib.value?.characters || [])
const locations = computed(() => lib.value?.locations || [])
const warnings = computed(() => lib.value?.warnings || [])
const styleLabel = computed(() => {
  const map = { realistic: '实拍写实', anime: '二次元（漫剧）', '3d': '三维动画', ink: '水墨国风' }
  return map[lib.value?.visual_style] || '未设定'
})

/** 音色按性别分组，方便在下拉里找。 */
const voiceGroups = computed(() => {
  const opts = lib.value?.voice_options || []
  return [
    { label: '男声', items: opts.filter((o) => o.gender === 'male') },
    { label: '女声', items: opts.filter((o) => o.gender === 'female') },
  ].filter((g) => g.items.length)
})

async function load() {
  if (!store.pid) return
  loading.value = true
  try {
    lib.value = await api(`/projects/${store.pid}/library`)
  } catch (e) {
    notify(`资产库读取失败：${e.message}`)
  } finally {
    loading.value = false
  }
}

/** 改角色属性（性别 / 音色 / 定妆图）——落库后整库返回，直接替换。 */
async function patchCharacter(name, change) {
  saving.value = name
  try {
    lib.value = await api(`/projects/${store.pid}/library`, 'PATCH', {
      characters: { [name]: change },
    })
    if (change.voice_id) notify(`${name} 的音色已更新`)
    if (change.image) notify(`${name} 的定妆图已更新`)
  } catch (e) {
    notify(`保存失败：${e.message}`)
  } finally {
    saving.value = ''
  }
}

/** 改场景图 / 环境描述。 */
async function patchLocation(name, change) {
  saving.value = name
  try {
    lib.value = await api(`/projects/${store.pid}/library`, 'PATCH', {
      locations: { [name]: change },
    })
    if (change.image) notify(`${name} 的场景图已更新`)
  } catch (e) {
    notify(`保存失败：${e.message}`)
  } finally {
    saving.value = ''
  }
}

// ---- 白模上传：用户自己的图替代 AI 生成的定妆照/场景图 ----
const uploadTarget = ref(null)   // { kind: 'character' | 'location', name }
const fileInput = ref(null)

function pickImage(kind, name) {
  uploadTarget.value = { kind, name }
  fileInput.value?.click()
}

async function onImagePicked(e) {
  const f = e.target.files?.[0]
  e.target.value = ''
  const t = uploadTarget.value
  if (!f || !t) return
  if (!/\.(png|jpe?g|webp|gif)$/i.test(f.name)) {
    notify('仅支持 png / jpg / webp / gif 图片')
    return
  }
  saving.value = t.name
  try {
    const fd = new FormData()
    fd.append('file', f)
    const res = await fetch(`/api/projects/${store.pid}/images/upload`, { method: 'POST', body: fd })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      throw new Error(j.detail || res.statusText)
    }
    const { url } = await res.json()
    if (t.kind === 'character') await patchCharacter(t.name, { image: url })
    else await patchLocation(t.name, { image: url })
  } catch (err) {
    notify(`图片上传失败：${err.message}`)
    saving.value = ''
  }
}

/** 改性别会连带重新选音色，所以清掉人工指定的音色。 */
function onGender(name, gender) {
  patchCharacter(name, { gender, voice_id: '' })
}

function onVoice(name, voiceId) {
  patchCharacter(name, { voice_id: voiceId })
}

/** 试听：让后端用这个音色读一句样音。 */
async function preview(member) {
  if (playing.value) return
  playing.value = member.voice_id
  try {
    const res = await fetch(`/api/projects/${store.pid}/library/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice_id: member.voice_id, text: '', gender: member.gender }),
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      throw new Error(j.detail || res.statusText)
    }
    const url = URL.createObjectURL(await res.blob())
    const audio = new Audio(url)
    audio.onended = () => { URL.revokeObjectURL(url); playing.value = '' }
    audio.onerror = () => { URL.revokeObjectURL(url); playing.value = '' }
    await audio.play()
  } catch (e) {
    notify(`试听失败：${e.message}`)
    playing.value = ''
  }
}

const GENDER_LABEL = { male: '男', female: '女', unknown: '未定' }

// 每次打开都重新拉：资产是会被美术指导/定妆图流程改写的，缓存会骗人。
watch(() => props.open, (open) => { if (open) load() })
</script>

<template>
  <div v-if="props.open" class="lib-backdrop" @click.self="emit('close')">
    <div class="lib">
      <header>
        <div>
          <h3>资产库</h3>
          <div class="sub">
            画风：{{ styleLabel }} · {{ characters.length }} 个角色 · {{ locations.length }} 个场景
          </div>
        </div>
        <div class="head-actions">
          <button class="ghost" :disabled="loading" @click="load">{{ loading ? '读取中…' : '刷新' }}</button>
          <button class="ghost" @click="emit('close')">关闭</button>
        </div>
      </header>

      <div v-if="loading && !lib" class="empty">正在读取资产…</div>

      <template v-else-if="lib">
        <div v-if="warnings.length" class="warns">
          <div v-for="(w, i) in warnings" :key="i" class="warn-row"><AppIcon name="warning" /> {{ w }}</div>
        </div>

        <h4>角色</h4>
        <div class="grid">
          <div v-for="c in characters" :key="c.name" class="card">
            <div class="portrait" :class="{ busy: saving === c.name }">
              <img v-if="c.image" :src="c.image" :alt="c.name" />
              <div v-else class="no-img">无定妆图</div>
              <button class="img-up" title="上传自己的定妆图" @click="pickImage('character', c.name)">
                <AppIcon name="upload" /> 上传
              </button>
            </div>
            <div class="info">
              <div class="name-row">
                <span class="name">{{ c.name }}</span>
                <span class="badge" :class="c.gender">{{ GENDER_LABEL[c.gender] }}</span>
                <span v-if="c.confirmed" class="badge ok">已确认</span>
              </div>
              <div class="meta">{{ c.basis }} · {{ c.speech_chars }} 字台词 · {{ c.scenes }} 场</div>

              <div class="row">
                <label>性别</label>
                <select :value="c.gender" :disabled="saving === c.name" @change="onGender(c.name, $event.target.value)">
                  <option value="male">男</option>
                  <option value="female">女</option>
                </select>
                <label>音色</label>
                <select :value="c.voice_id" :disabled="saving === c.name" @change="onVoice(c.name, $event.target.value)">
                  <optgroup v-for="g in voiceGroups" :key="g.label" :label="g.label">
                    <option v-for="o in g.items" :key="o.voice_id" :value="o.voice_id">{{ o.label }}</option>
                  </optgroup>
                </select>
                <button class="play" :disabled="!!playing" @click="preview(c)">
                  <AppIcon :name="playing === c.voice_id ? 'pause' : 'play'" /> {{ playing === c.voice_id ? '播放中' : '试听' }}
                </button>
              </div>

              <div class="look" :title="c.appearance || '还没有外貌锚'">
                {{ c.appearance || '（还没有外貌锚，出片时会失去人物一致性）' }}
              </div>
            </div>
          </div>
        </div>

        <h4>场景</h4>
        <div class="grid">
          <div v-for="l in locations" :key="l.name" class="card loc">
            <div class="portrait" :class="{ busy: saving === l.name }">
              <img v-if="l.image" :src="l.image" :alt="l.name" />
              <div v-else class="no-img">无场景图</div>
              <button class="img-up" title="上传自己的场景图" @click="pickImage('location', l.name)">
                <AppIcon name="upload" /> 上传
              </button>
            </div>
            <div class="info">
              <div class="name-row"><span class="name">{{ l.name }}</span></div>
              <div class="meta">{{ l.scenes }} 场</div>
              <div class="look">{{ l.description || '（还没有环境描述）' }}</div>
            </div>
          </div>
        </div>
      </template>
    </div>
    <input ref="fileInput" type="file" accept=".png,.jpg,.jpeg,.webp,.gif" style="display: none" @change="onImagePicked" />
  </div>
</template>

<style scoped>
.lib-backdrop {
  position: fixed; inset: 0; background: oklch(0 0 0 / 0.55); z-index: 60;
  display: flex; align-items: center; justify-content: center;
}
.lib {
  background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-lg);
  width: 920px; max-width: 94vw; max-height: 88vh; overflow-y: auto;
  padding: 18px 20px; box-shadow: 0 20px 60px oklch(0 0 0 / 0.5);
}
header { display: flex; justify-content: space-between; align-items: flex-start; }
h3 { margin: 0 0 2px; font-size: 16px; }
h4 { margin: 18px 0 8px; font-size: 13px; color: var(--muted); font-weight: 500; }
.sub { color: var(--muted); font-size: 12px; }
.head-actions { display: flex; gap: 6px; }
.warns {
  margin-top: 10px; padding: 8px 10px; border-radius: var(--radius-sm); font-size: 12px;
  background: oklch(0.85 0.12 80 / 0.12); border: 1px solid oklch(0.8 0.12 80 / 0.35);
  color: var(--ink); display: flex; flex-direction: column; gap: 4px;
}
.warn-row { display: flex; align-items: center; gap: 6px; }
.warn-row .app-ic { color: var(--warn); flex: none; }
.empty { color: var(--muted); font-size: 12.5px; padding: 20px 0; }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.card {
  display: flex; gap: 10px; padding: 10px; border: 1px solid var(--line);
  border-radius: var(--radius-md); background: var(--bg, transparent);
}
.portrait {
  position: relative;
  width: 76px; height: 96px; flex: 0 0 auto; border-radius: var(--radius-sm); overflow: hidden;
  background: oklch(0 0 0 / 0.15); display: flex; align-items: center; justify-content: center;
}
.portrait img { width: 100%; height: 100%; object-fit: cover; }
.portrait.busy { opacity: 0.55; }
/* 上传白模：悬停浮现的小按钮 */
.img-up {
  position: absolute; left: 0; right: 0; bottom: 0;
  display: flex; align-items: center; justify-content: center; gap: 3px;
  padding: 3px 0; border: none; cursor: pointer;
  background: oklch(0 0 0 / 0.62); color: #fff;
  font-size: 10px; line-height: 1;
  opacity: 0; transition: opacity 140ms var(--ease);
}
.img-up .app-ic { font-size: 11px; }
.portrait:hover .img-up { opacity: 1; }
.no-img { font-size: 10.5px; color: var(--muted); text-align: center; padding: 4px; }
.info { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 5px; }
.name-row { display: flex; align-items: center; gap: 6px; }
.name { font-size: 13.5px; font-weight: 600; }
.badge {
  font-size: 10px; padding: 1px 6px; border-radius: 3px;
  border: none; color: var(--muted);
  background: color-mix(in oklch, var(--ink) 5%, transparent);
}
.badge.male { color: oklch(0.72 0.10 240); background: oklch(0.6 0.10 240 / 0.14); }
.badge.female { color: oklch(0.72 0.11 340); background: oklch(0.6 0.11 340 / 0.14); }
.badge.ok { color: var(--good, oklch(0.72 0.13 150)); background: oklch(0.6 0.13 150 / 0.14); }
.meta { font-size: 11px; color: var(--muted); }
.row { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.row label { font-size: 10.5px; color: var(--muted); }
.row select {
  font-size: 11.5px; padding: 2px 4px; max-width: 118px;
  background: transparent; color: var(--ink); border: 1px solid var(--line); border-radius: var(--radius-sm);
}
.play {
  font-size: 11px; padding: 2px 8px; border-radius: 3px; cursor: pointer;
  background: transparent; border: 1px solid var(--line); color: var(--muted);
}
.play:hover:not(:disabled) { color: var(--accent); border-color: var(--accent); }
.look {
  font-size: 11px; color: var(--muted); line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.ghost {
  background: transparent; border: 1px solid var(--line); border-radius: var(--radius-sm);
  padding: 4px 10px; font-size: 12px; color: var(--muted); cursor: pointer;
}
.ghost:hover { color: var(--ink); border-color: var(--accent); }
</style>
