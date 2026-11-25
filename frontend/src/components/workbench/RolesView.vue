<script setup>
// =====================================================================
// RolesView.vue —— 工作台·资产画布
//
// 即梦的资产库页一句话能说完：**类型 tab（带计数）+ 缺口横幅 + 大卡网格**。
// 本页照这个形态重做（旧版只是包一层 AssetPanel，观感差距太大）：
//   - tab：角色 / 场景 / 图片 / 视频 / 音频，计数来自 library + assets；
//   - 上方是作品资产与产出图，下方是单图生成输入区；
//   - 大卡网格：角色卡 = 定妆照大图 + 名字 + 音色 chip + 缺图虚线占位 +
//     本地上传绑定（images/upload → PATCH library）；场景卡同构；媒体卡直接预览。
// 音色试听/克隆/演员库等深功能仍在「完整资产库」（AssetPanel 全屏页）里，
// 本页是即梦式的第一眼视图。
// =====================================================================
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import { api } from '../../api'
import { store, notify } from '../../stores/app'

const chars = ref([])
const locs = ref([])
const warnings = ref([])
const assets = ref([])
const loading = ref(false)
const tab = ref('characters')
let firstLoad = true
const lookDraft = ref({})
const anchorDraft = ref({})
const lookBusy = ref('')
const targetKind = ref('')
const targetId = ref('')
const imagePrompt = ref('')
const variantName = ref('')
const modelOptions = ref([])
const modelKey = ref('')
const ratio = ref('1:1')
const quality = ref('1K')

async function loadModels() {
  if (!store.pid) { modelOptions.value = []; modelKey.value = ''; return }
  try {
    modelOptions.value = await api(`/projects/${store.pid}/reference-assets/canvas/models`)
    if (!modelOptions.value.some(m => `${m.provider_id}|${m.model_name}` === modelKey.value)) {
      const first = modelOptions.value[0]
      modelKey.value = first ? `${first.provider_id}|${first.model_name}` : ''
    }
  } catch (e) { modelOptions.value = []; notify(`图片模型加载失败：${e.message}`, 'error') }
}
const selectedModel = computed(() => modelOptions.value.find(m => `${m.provider_id}|${m.model_name}` === modelKey.value))
const selectedTarget = computed(() => (targetKind.value === 'character' ? chars.value : locs.value)
  .find(a => (a.char_id || a.loc_id) === targetId.value))
const generateLabel = computed(() => selectedTarget.value
  ? `生成${selectedTarget.value.name}${targetKind.value === 'character' ? '定妆图' : '场景图'}`
  : '选择一个角色或场景')
function selectTarget(a, kind) {
  targetKind.value = kind
  targetId.value = a.char_id || a.loc_id
}
function onTargetChange(value) {
  const [kind, id] = value.split('|')
  targetKind.value = kind || ''
  targetId.value = id || ''
}
watch(selectedModel, (model) => {
  if (!model) return
  if (!model.ratios.includes(ratio.value)) ratio.value = model.ratios[0]
  if (!model.qualities.includes(quality.value)) quality.value = model.qualities[0]
})
async function addLook(c) {
  const d = lookDraft.value[c.char_id] || {}
  if (!d.name?.trim() || !d.description?.trim() || !d.available_from_shot_id?.trim()) {
    notify('填写造型名、描述和首次可用镜头 ID', 'info'); return
  }
  lookBusy.value = c.char_id
  try {
    await api(`/projects/${store.pid}/looks`, 'POST', { character_id: c.char_id, ...d })
    lookDraft.value[c.char_id] = { name: '', description: '', available_from_shot_id: '', identity_anchor: d.identity_anchor || '' }
    await refresh()
  } catch (e) { notify(`新增造型失败：${e.message}`, 'error') }
  finally { lookBusy.value = '' }
}
async function generateLook(look) {
  try {
    await api(`/projects/${store.pid}/looks/${look.id}/generate`, 'POST')
    notify('已开始生成此造型，完成后请检查与其他造型的脸部一致性', 'ok')
  } catch (e) { notify(`生成失败：${e.message}`, 'error') }
}
async function saveAnchor(c) {
  const description = (anchorDraft.value[c.char_id] || '').trim()
  if (!description) { notify('请填写可跨造型复用的脸部特征', 'info'); return }
  try {
    await api(`/projects/${store.pid}/characters/${c.char_id}/identity-anchor`, 'PUT', { description })
    notify('脸部特征已保存；文生图仍需人工检查同脸', 'ok')
  } catch (e) { notify(`保存失败：${e.message}`, 'error') }
}

async function refresh() {
  if (!store.pid) { chars.value = []; locs.value = []; assets.value = []; warnings.value = []; return }
  loading.value = true
  try {
    const [lib, imgs, vids, auds] = await Promise.all([
      api(`/projects/${store.pid}/library`),
      api(`/projects/${store.pid}/assets?kind=image`),
      api(`/projects/${store.pid}/assets?kind=video`),
      api(`/projects/${store.pid}/assets?kind=audio`),
    ])
    chars.value = lib.characters || []
    for (const c of chars.value) lookDraft.value[c.char_id] ||= { name: '', description: '', available_from_shot_id: '', identity_anchor: '' }
    for (const c of chars.value) if (anchorDraft.value[c.char_id] === undefined) anchorDraft.value[c.char_id] = c.identity_anchor || ''
    locs.value = lib.locations || []
    warnings.value = lib.warnings || []
    assets.value = [...(imgs || []), ...(vids || []), ...(auds || [])]
    if (firstLoad) {
      tab.value = (chars.value.some(c => c.image) || locs.value.some(l => l.image)
        || assets.value.some(a => a.kind === 'image')) ? 'image' : 'characters'
      firstLoad = false
    }
  } catch (e) {
    notify(`资产加载失败：${e.message}`, 'error')
  } finally {
    loading.value = false
  }
}
onMounted(refresh)
watch(() => store.pid, refresh)
watch(() => store.assetRefreshSeq, refresh)
onMounted(loadModels)
watch(() => store.pid, loadModels)

const TABS = [
  { key: 'characters', label: '角色' },
  { key: 'locations', label: '场景' },
  { key: 'image', label: '图片' },
  { key: 'video', label: '视频' },
  { key: 'audio', label: '音频' },
]
const generatedImages = computed(() => [
  ...chars.value.flatMap(c => {
    const looks = (c.looks || []).filter(l => l.image)
    return looks.length
      ? looks.map(l => ({ id: l.id, name: `${c.name} · ${l.name}`, kind: 'image', url: l.image, source: 'generation' }))
      : (c.image ? [{ id: c.char_id, name: c.name, kind: 'image', url: c.image, source: 'generation' }] : [])
  }),
  ...locs.value.flatMap(l => {
    const states = (l.states || []).filter(s => s.image)
    return states.length
      ? states.map(s => ({ id: s.id, name: `${l.name} · ${s.name}`, kind: 'image', url: s.image, source: 'generation' }))
      : (l.image ? [{ id: l.loc_id, name: l.name, kind: 'image', url: l.image, source: 'generation' }] : [])
  }),
])
const allImages = computed(() => {
  const seen = new Set()
  return [...generatedImages.value, ...assets.value.filter(a => a.kind === 'image')]
    .filter(a => {
      if (!a.url || seen.has(a.url)) return false
      seen.add(a.url)
      return true
    })
})
const counts = computed(() => ({
  characters: chars.value.length,
  locations: locs.value.length,
  image: allImages.value.length,
  video: assets.value.filter(a => a.kind === 'video').length,
  audio: assets.value.filter(a => a.kind === 'audio').length,
}))
const shown = computed(() =>
  tab.value === 'characters' ? chars.value
  : tab.value === 'locations' ? locs.value
  : tab.value === 'image' ? allImages.value
  : assets.value.filter(a => a.kind === tab.value))

const missingImages = computed(() =>
  chars.value.filter(c => !c.has_image).length + locs.value.filter(l => !l.has_image).length)

// ---- 单图生成：用户在画布上选对象、提示词、模型、比例和清晰度 ----
const generating = ref(false)
let genTaskId = ''
let genTaskProjectId = ''
let pollTimer = null
async function generateCanvas() {
  if (!store.pid || generating.value || !selectedTarget.value || !selectedModel.value) return
  if (!imagePrompt.value.trim()) { notify('先描述想要的画面', 'info'); return }
  generating.value = true
  try {
    const r = await api(`/projects/${store.pid}/reference-assets/canvas`, 'POST', {
      target_kind: targetKind.value, target_id: targetId.value,
      prompt: imagePrompt.value.trim(), variant_name: variantName.value.trim(),
      provider_id: selectedModel.value.provider_id, model_name: selectedModel.value.model_name,
      ratio: ratio.value, quality: quality.value,
    })
    genTaskId = r.task_id || ''
    genTaskProjectId = store.pid
    if (genTaskId) sessionStorage.setItem(`wenju-image-task:${genTaskProjectId}`, genTaskId)
    notify(`正在生成${selectedTarget.value.name}的 1 张图片`, 'ok')
    pollTimer = setInterval(pollGenTask, 4000)
    pollGenTask()
  } catch (e) {
    notify(`启动失败：${e.message}`, 'error')
    generating.value = false
  }
}
async function pollGenTask() {
  if (!genTaskId) return
  const taskId = genTaskId
  const pid = genTaskProjectId
  try {
    const t = await api(`/tasks/${taskId}`)
    if (taskId !== genTaskId || pid !== store.pid) return
    if (t.status === 'done' || t.status === 'failed' || t.status === 'cancelled') {
      clearInterval(pollTimer); pollTimer = null
      sessionStorage.removeItem(`wenju-image-task:${pid}`)
      genTaskId = ''
      generating.value = false
      if (t.status === 'done') { notify(t.result || '图片生成完成', 'ok'); tab.value = 'image' }
      else notify(`图片生成${t.status === 'failed' ? '失败' : '已取消'}${t.result || t.error ? '：' + (t.result || t.error) : ''}`, 'error')
      await refresh()
      store.assetRefreshSeq += 1
    }
  } catch {
    clearInterval(pollTimer); pollTimer = null
    generating.value = false
    notify('进度查询中断，请稍后在任务面板查看', 'error')
  }
}
onBeforeUnmount(() => clearInterval(pollTimer))
function resumeGenTask() {
  if (genTaskProjectId !== store.pid) {
    clearInterval(pollTimer); pollTimer = null
    genTaskId = ''
    generating.value = false
  }
  if (!store.pid) return
  const saved = sessionStorage.getItem(`wenju-image-task:${store.pid}`)
  if (!saved || genTaskId === saved) return
  genTaskId = saved
  genTaskProjectId = store.pid
  generating.value = true
  clearInterval(pollTimer)
  pollTimer = setInterval(pollGenTask, 4000)
  pollGenTask()
}
onMounted(resumeGenTask)
watch(() => store.pid, resumeGenTask)

// ---- 本地上传绑定到角色/场景（即梦卡上的「上传」路） ----
const fileInput = ref(null)
const bindTarget = ref(null) // {id, kind, name}
function pickUpload(target) {
  bindTarget.value = target
  fileInput.value?.click()
}
async function onFilePicked(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file || !bindTarget.value || !store.pid) return
  const t = bindTarget.value
  try {
    const fd = new FormData()
    fd.append('file', file)
    const asset = await api(`/projects/${store.pid}/images/upload`, 'POST', fd)
    if (t.lookId) {
      await api(`/projects/${store.pid}/looks/${t.lookId}/image`, 'PUT', { image: asset.url })
    } else {
      const body = t.kind === 'character'
        ? { characters: { [t.id]: { image: asset.url } } }
        : { locations: { [t.id]: { image: asset.url } } }
      await api(`/projects/${store.pid}/library`, 'PATCH', body)
    }
    notify(`已把图片绑定到「${t.name}」`, 'ok')
    await refresh()
    store.assetRefreshSeq += 1
  } catch (err) {
    notify(`上传绑定失败：${err.message}`, 'error')
  }
}

function previewOf(a) {
  const isChar = 'char_id' in a
  const isLoc = 'loc_id' in a
  if (isChar || isLoc) {
    return { name: a.name, kind: isChar ? 'character' : 'location', image: a.image || '', id: isChar ? a.char_id : a.loc_id }
  }
  return {
    name: a.name, kind: a.kind, id: a.id,
    image: a.kind === 'image' ? a.url : '',
    video: a.kind === 'video' ? a.url : '',
    audio: a.kind === 'audio' ? a.url : '',
  }
}
</script>

<template>
  <div class="roles">
    <input ref="fileInput" type="file" accept="image/*" hidden @change="onFilePicked" />
    <div class="canvas-output">

    <header class="rl-head">
      <div class="canvas-heading">
        <strong>资产库</strong>
        <span>选择角色或场景，在下方描述下一张图</span>
      </div>
      <div class="rl-tabs seg-tabs">
        <button
          v-for="(t, i) in TABS" :key="t.key"
          class="seg-tab" :class="{ active: tab === t.key }"
          @click="tab = t.key"
        >
          <span class="seg-num">{{ String(i + 1).padStart(2, '0') }}</span>
          {{ t.label }} <em class="cnt">{{ counts[t.key] }}</em>
        </button>
      </div>
      <div class="rl-acts">
        <button class="ghost small" type="button" title="完整资产库：音色试听/克隆、演员库、导入"
          @click="store.overlay = 'assets'">
          完整资产库
        </button>
      </div>
    </header>

    <!-- 缺口横幅：说明缺口，不从这里直接触发付费批量任务。 -->
    <div v-if="tab === 'characters' || tab === 'locations'" class="gap-bar" :class="{ clean: !missingImages }">
      <template v-if="missingImages">
        <AppIcon name="warning" />
        <span>还有 {{ missingImages }} 个角色或场景没有图片。选中一项，在下方生成一张。</span>
      </template>
      <template v-else>
        <AppIcon name="check" />
        <span>全部形象已就位</span>
      </template>
    </div>
    <details v-if="warnings.length" class="warn-details">
      <summary>制作提醒 · {{ warnings.length }}</summary>
      <p v-for="(w, i) in warnings" :key="'w' + i" class="warn-line">
        <AppIcon name="info" /> {{ w }}
      </p>
    </details>

    <p v-if="loading" class="rl-state">加载中…</p>
    <div v-else-if="!store.pid" class="rl-state">还没有进入作品。</div>

    <!-- 角色 / 场景：大卡网格 -->
    <div v-else-if="tab === 'characters' || tab === 'locations'" class="grid">
      <article v-for="a in shown" :key="a.char_id || a.loc_id" class="card"
        :class="{ selected: targetId === (a.char_id || a.loc_id) && targetKind === (tab === 'characters' ? 'character' : 'location') }">
        <button class="card-media" type="button"
          :title="a.image ? '点开大图' : '还没有形象图'"
          @click="a.image && (store.assetPreview = previewOf(a))">
          <img v-if="a.image" :src="a.image" :alt="a.name" loading="lazy" />
          <span v-else class="ph">
            <AppIcon :name="tab === 'characters' ? 'user' : 'folder'" />
            <em>{{ tab === 'characters' ? '缺定妆照' : '缺场景图' }}</em>
          </span>
        </button>
        <div class="card-body">
          <p class="card-name">
            {{ a.name }}
            <em v-if="tab === 'characters' && a.role === 'protagonist'" class="tag-lead">主角</em>
          </p>
          <p class="card-sub">
            <span v-if="tab === 'characters'" class="voice-chip">
              <AppIcon name="sound" /> {{ a.voice_label || '未配音色' }}
            </span>
            <span v-else>{{ a.scenes || 0 }} 场戏</span>
          </p>
          <details v-if="tab === 'characters'" class="look-details" @click.stop>
            <summary>造型与脸部设定</summary>
            <div class="look-list">
            <input v-model.trim="anchorDraft[a.char_id]" placeholder="跨造型脸部特征，如眉眼、痣、鼻梁" aria-label="跨造型脸部特征" />
            <button class="mini" type="button" @click="saveAnchor(a)">保存脸部特征</button>
            <div v-for="look in a.looks || []" :key="look.id" class="look-row">
              <img v-if="look.image" :src="look.image" :alt="look.name" />
              <span>{{ look.name }} · {{ look.description || '待补描述' }}<small v-if="look.available_from_shot_id">从 {{ look.available_from_shot_id }} 起</small></span>
              <button v-if="!look.image" class="mini" type="button" @click="generateLook(look)">生成</button>
              <button class="mini" type="button" @click="pickUpload({ id: a.char_id, kind: 'character', name: look.name, lookId: look.id })">上传</button>
            </div>
            <input v-model.trim="lookDraft[a.char_id].name" placeholder="新造型名称" aria-label="新造型名称" />
            <input v-model.trim="lookDraft[a.char_id].identity_anchor" placeholder="共用面部特征（各造型一致）" aria-label="共用面部特征" />
            <input v-model.trim="lookDraft[a.char_id].description" placeholder="服饰、发型、身体状态" aria-label="新造型描述" />
            <input v-model.trim="lookDraft[a.char_id].available_from_shot_id" placeholder="首次可用镜头 ID" aria-label="首次可用镜头" />
            <button class="mini" type="button" :disabled="lookBusy === a.char_id" @click="addLook(a)">新增造型</button>
            </div>
          </details>
          <div class="card-foot">
            <button class="mini select-asset" type="button"
              @click="selectTarget(a, tab === 'characters' ? 'character' : 'location')">
              <AppIcon name="sparkle" /> {{ targetId === (a.char_id || a.loc_id) ? '已选中' : `为${a.name}生成` }}
            </button>
            <button class="mini" type="button"
              :title="tab === 'characters' ? '本地上传图片并绑定为该角色形象' : '本地上传图片并绑定为该场景图'"
              @click="pickUpload({ id: a.char_id || a.loc_id, kind: tab === 'characters' ? 'character' : 'location', name: a.name })">
              <AppIcon name="upload" /> {{ tab === 'characters' ? '上传形象' : '上传场景图' }}
            </button>
          </div>
        </div>
      </article>
      <p v-if="!shown.length" class="rl-state">
        还没有{{ tab === 'characters' ? '角色——在对话里生成初稿后自动建档' : '场景' }}。
      </p>
    </div>

    <!-- 媒体资产：图片 / 视频 / 音频 -->
    <div v-else class="grid">
      <article v-for="a in shown" :key="a.id" class="card">
        <button class="card-media" type="button" title="点开预览" @click="store.assetPreview = previewOf(a)">
          <img v-if="a.kind === 'image'" :src="a.url" :alt="a.name" loading="lazy" />
          <video v-else-if="a.kind === 'video'" :src="a.url + '#t=0.1'" preload="metadata" muted playsinline />
          <span v-else class="ph"><AppIcon name="sound" /><em>音频</em></span>
        </button>
        <div class="card-body">
          <p class="card-name">{{ a.name }}</p>
          <p class="card-sub">{{ a.source === 'upload' ? '上传素材' : '生成产物' }}</p>
        </div>
      </article>
      <p v-if="!shown.length" class="rl-state">还没有{{ TABS.find(t => t.key === tab)?.label }}资产。</p>
    </div>
    </div>

    <form class="canvas-composer" @submit.prevent="generateCanvas">
      <div class="composer-top">
        <div>
          <span class="composer-eyebrow">图片生成</span>
          <strong>{{ selectedTarget ? `为「${selectedTarget.name}」生成新图片` : '选一个角色或场景开始' }}</strong>
        </div>
        <span class="composer-hint">每次 1 张 · 按模型计费 · 质检不自动重生</span>
      </div>
      <div class="target-row">
        <label class="target-select">对象
          <select :value="targetKind && targetId ? `${targetKind}|${targetId}` : ''"
            @change="onTargetChange($event.target.value)">
            <option value="">选择要生成的角色或场景</option>
            <optgroup label="角色">
              <option v-for="c in chars" :key="c.char_id" :value="`character|${c.char_id}`">{{ c.name }}</option>
            </optgroup>
            <optgroup label="场景">
              <option v-for="l in locs" :key="l.loc_id" :value="`location|${l.loc_id}`">{{ l.name }}</option>
            </optgroup>
          </select>
        </label>
        <label class="version-name">版本名 <small>可选</small>
          <input v-model.trim="variantName" maxlength="40" placeholder="如：雨夜、受伤后" />
        </label>
      </div>
      <label class="prompt-label" for="asset-image-prompt">描述想要的画面</label>
      <textarea id="asset-image-prompt" v-model="imagePrompt" maxlength="4000" rows="3"
        placeholder="写下人物或场景的变化、光线、构图和氛围。会沿用作品画风，旧图会保留。" />
      <div class="composer-bottom">
        <label>图片模型
          <select v-model="modelKey" :disabled="!modelOptions.length">
            <option v-if="!modelOptions.length" value="">尚未配置图片模型</option>
            <option v-for="m in modelOptions" :key="`${m.provider_id}|${m.model_name}`"
              :value="`${m.provider_id}|${m.model_name}`">{{ m.label }} · {{ m.model_name }}</option>
          </select>
        </label>
        <label>比例
          <select v-model="ratio" :disabled="!selectedModel">
            <option v-for="r in selectedModel?.ratios || []" :key="r" :value="r">{{ r }}</option>
          </select>
        </label>
        <label>清晰度
          <select v-model="quality" :disabled="!selectedModel">
            <option v-for="q in selectedModel?.qualities || []" :key="q" :value="q">{{ q }}</option>
          </select>
        </label>
        <button class="generate-button" type="submit"
          :disabled="generating || !selectedTarget || !selectedModel || !imagePrompt.trim()">
          <AppIcon name="sparkle" /> {{ generating ? '正在生成…' : generateLabel }}
        </button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.roles { display: flex; flex-direction: column; height: 100%; min-height: 0; overflow: hidden; }
.canvas-output { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow-y: auto; }
.rl-head {
  display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px;
  padding: 18px 18px 0; flex: none;
}
.canvas-heading { display: flex; flex-direction: column; gap: 3px; width: 100%; }
.canvas-heading strong { color: var(--ink); font-size: 18px; }
.canvas-heading span { color: var(--muted); font-size: 12px; }
.rl-tabs { flex: 1; min-width: 0; overflow-x: auto; }
.cnt { font-style: normal; color: var(--gold); font-family: var(--mono); font-size: 10px; margin-left: 4px; }
.rl-acts { flex: none; padding-bottom: 6px; }

.gap-bar {
  display: flex; align-items: center; gap: 10px;
  margin: 12px 18px 0; padding: 9px 12px; flex: none;
  border: 1px solid color-mix(in oklch, var(--warn) 40%, transparent);
  border-radius: var(--radius-md);
  background: color-mix(in oklch, var(--warn) 7%, transparent);
  color: var(--ink); font-size: 12.5px;
}
.gap-bar.clean {
  border-color: color-mix(in oklch, var(--ok) 35%, transparent);
  background: color-mix(in oklch, var(--ok) 6%, transparent);
  color: var(--muted);
}
.gap-bar span { flex: 1; }
.gap-bar :deep(svg) { width: 14px; height: 14px; color: var(--warn); flex: none; }
.gap-bar.clean :deep(svg) { color: var(--ok); }
.warn-line {
  display: flex; gap: 8px; align-items: baseline;
  margin: 6px 0 0; font-size: 11.5px; color: var(--dim);
}
.warn-line :deep(svg) { width: 11px; height: 11px; flex: none; }
.warn-details { margin: 8px 18px 0; color: var(--dim); font-size: 11px; }
.warn-details summary { cursor: pointer; }
.rl-state { padding: 24px; text-align: center; color: var(--dim); font-size: 12.5px; }

.grid {
  flex: 1; min-height: 180px;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  align-content: start; gap: 14px; padding: 14px 18px 20px;
}
.card {
  display: flex; flex-direction: column;
  border: 1px solid var(--line); border-radius: var(--radius-lg);
  background: var(--surface); overflow: hidden;
  transition: border-color 150ms var(--ease);
}
.card:hover { border-color: var(--line-strong); }
.card.selected { border-color: var(--gold); box-shadow: 0 0 0 1px color-mix(in oklch, var(--gold) 35%, transparent); }
.card-media {
  display: block; width: 100%; aspect-ratio: 4/3; padding: 0;
  border: none; background: #000; cursor: zoom-in;
}
.card-media img, .card-media video { width: 100%; height: 100%; object-fit: cover; display: block; }
.ph {
  width: 100%; height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 6px;
  background: var(--surface-2); color: var(--dim);
}
.ph :deep(svg) { width: 22px; height: 22px; }
.ph em { font-style: normal; font-size: 11px; }
.card-body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 6px; }
.card-name {
  margin: 0; font-size: 13px; font-weight: 600; color: var(--ink);
  display: flex; align-items: center; gap: 6px;
}
.tag-lead {
  font-style: normal; font-size: 10px; color: var(--gold);
  padding: 1px 6px; border-radius: 3px;
  border: 1px solid color-mix(in oklch, var(--gold) 45%, transparent);
}
.card-sub { margin: 0; font-size: 11.5px; color: var(--muted); }
.voice-chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 8px; border-radius: var(--radius-full);
  border: 1px solid var(--line); background: var(--surface-2);
  font-size: 11px; color: var(--muted);
}
.voice-chip :deep(svg) { width: 11px; height: 11px; }
.card-foot { margin-top: 2px; }
.card-foot { display: flex; gap: 6px; flex-wrap: wrap; }
.select-asset { background: color-mix(in oklch, var(--gold) 15%, var(--surface-2)); color: var(--ink); }
.look-details { border-top: 1px solid var(--line); padding-top: 7px; }
.look-details summary { color: var(--muted); font-size: 11px; cursor: pointer; }
.look-list { display: grid; gap: 5px; }
.look-details .look-list { padding-top: 8px; }
.look-list input { width: 100%; min-width: 0; background: var(--surface-2); color: var(--ink); border: 1px solid var(--line); border-radius: 4px; padding: 5px; }
.look-row { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); }
.look-row img { width: 32px; height: 32px; object-fit: cover; }
.look-row span { flex: 1; min-width: 0; }
.look-row small { display: block; color: var(--dim); }
.mini {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 8px; border-radius: var(--radius-sm);
  border: none; background: rgba(255, 255, 255, .06);
  color: var(--muted); font-size: 11px; cursor: pointer;
}
.mini:hover { color: var(--ink); background: rgba(255, 255, 255, .12); }
.mini :deep(svg) { width: 11px; height: 11px; }
.canvas-composer {
  flex: none; display: grid; gap: 9px; padding: 15px 18px 17px;
  border-top: 1px solid var(--line-strong); background: var(--panel-solid);
  box-shadow: 0 -8px 25px rgba(0, 0, 0, .16);
}
.composer-top { display: flex; align-items: end; justify-content: space-between; gap: 8px; }
.composer-top > div { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.composer-eyebrow { color: var(--gold); font-size: 11px; white-space: nowrap; }
.composer-top strong { color: var(--ink); font-size: 13px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.composer-hint { color: var(--dim); font-size: 10.5px; text-align: right; }
.target-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, .7fr); gap: 8px; }
.canvas-composer label { display: grid; gap: 4px; color: var(--muted); font-size: 11px; }
.canvas-composer label small { color: var(--dim); }
.canvas-composer select, .canvas-composer input, .canvas-composer textarea {
  width: 100%; min-width: 0; border: 1px solid var(--line-strong); border-radius: 7px;
  background: var(--surface-2); color: var(--ink); font: inherit; font-size: 12px;
  padding: 7px 9px; outline: none;
}
.canvas-composer :is(select, input, textarea):focus { border-color: var(--gold); }
.canvas-composer textarea { min-height: 68px; max-height: 140px; resize: vertical; line-height: 1.5; }
.composer-bottom { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(70px, .6fr) minmax(75px, .6fr) auto; gap: 8px; align-items: end; }
.generate-button { display: inline-flex; gap: 6px; align-items: center; justify-content: center;
  min-height: 32px; border: 0; border-radius: 7px; padding: 7px 12px;
  background: var(--gold); color: #1a150d; font-weight: 700; font-size: 12px; cursor: pointer; white-space: nowrap; }
.generate-button:disabled { cursor: not-allowed; opacity: .5; }
.generate-button :deep(svg) { width: 13px; height: 13px; }
@media (max-width: 760px) {
  .composer-bottom { grid-template-columns: minmax(0, 1fr) 90px 90px; }
  .generate-button { grid-column: 1 / -1; }
  .composer-hint { display: none; }
}
</style>
