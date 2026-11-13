<script setup>
// =====================================================================
// ComposerCard.vue —— 输入卡片（首页与对话页共用同一套）
//
// 形制（照参考图）：
//   ┌──────────────────────────────────────────────┐
//   │ [+]  描述你想生成的内容，或点 ＋ 附上图片…       │
//   │                                                │
//   │ [作品 ▾][模型 ▾]                       [⚙]  ( ➤ )   │
//   └──────────────────────────────────────────────┘
//
// 胶囊只放「每轮都要看一眼」的选择器：
//   作品  —— 这轮对话挂在哪部作品下
//   模型  —— 用哪个模型对话（直接切换，不进设置页）
// Skill / 生成资产是低频动作，收进「＋」菜单——
// 四个胶囊挤一行会换行（首页卡片 1040px 也救不了长作品名）。
// =====================================================================

import { computed, nextTick, onMounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { api } from '../api'
import { store, selectProject, loadProviders, loadChatPref, loadModelStatus, notify } from '../stores/app'

const text = defineModel('text', { type: String, default: '' })

const props = defineProps({
  placeholder: {
    type: String,
    default: '描述画面、人物与剧情，支持使用 @ 快速调用参考素材。内容将由 AI 生成。',
  },
  /** 上下文提示（只在有提示时显示） */
  hintText: { type: String, default: '' },
  canSend: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  /** 可被 @ 引用的资产 [{ kind, id, name, sub }] */
  assets: { type: Array, default: () => [] },
})

const emit = defineEmits([
  'send', 'pick-menu', 'open-settings', 'open-skills', 'asset-picked', 'new-project',
])

/** 让外层（比如「＋」菜单）能直接打开 @ 候选。 */
function openMention() {
  mentionOpen.value = true
}
defineExpose({ openMention })

// ---- 模型胶囊 + 下拉：当前供应商在「模型设置」里配好的模型清单 ----
const modelOpen = ref(false)

const modelLabel = computed(() => store.modelInfo?.name || store.modelInfo?.provider || '模型')

/** 当前供应商：优先用对话偏好指定的那家，否则第一个配好的。 */
const currentProvider = computed(() => {
  const list = store.providers || []
  const prefId = store.chatPref?.provider_id
  return list.find((p) => p.id === prefId) || list.find((p) => p.configured) || null
})

/** 某个厂商名下的模型（模型设置里配的清单；老配置只有单个 model 时视作一条）。 */
function modelsOf(provider) {
  const p = provider
  if (!p) return []
  const list = p.config?.models
  if (Array.isArray(list) && list.length) {
    return list.filter((m) => m && m.model).map((m) => ({
      name: m.name || m.model, model: m.model, enabled: m.enabled !== false,
    }))
  }
  const one = p.config?.model
  return one ? [{ name: one, model: one, enabled: true }] : []
}

const providerModels = computed(() => modelsOf(currentProvider.value))

/** 面板对话 tab 的清单：只列当前厂商的模型；当前生效模型若不在清单
    （比如来自 .env 且厂商没配），补一行保证「在用的看得见」。 */
const chatModels = computed(() => {
  const list = [...providerModels.value]
  const name = activeModelName.value
  if (name && !list.some((m) => m.model === name)) {
    list.push({ name, model: name, enabled: true, fromEnv: activeFromEnv.value })
  }
  return list
})

/** 所有已配置厂商的模型，按厂商分组。
    用户配了多个厂商时，下拉里能一次看到每家的模型（选中即切到那家）。 */
/** 当前生效的模型名（对话偏好优先，否则就是实际在用的那个）。
    偏好如果指向一个已经不存在的厂商（删过厂商又留了偏好），当作没有偏好。 */
const livePref = computed(() => {
  const pref = store.chatPref
  if (!pref?.provider_id) return null
  const exists = (store.providers || []).some((p) => p.id === pref.provider_id)
  return exists ? pref : null
})

const activeModelName = computed(
  () => livePref.value?.model_name || store.modelInfo?.name || '',
)

/** 当前生效的模型来自 .env、且不在厂商配置的清单里 —— 这时要在下拉里补一行，
    否则用户看到的「当前模型」在下拉里根本找不到。 */
const activeFromEnv = computed(
  () => !livePref.value?.model_name && store.modelInfo?.source === 'env',
)

/** 当前模型的厂商（下拉顶部右侧那行小字）。
    厂商 = 模型归属的那家；界面指定过就取那家，否则取 .env 里的那个。 */
const activeVendor = computed(() => {
  const pref = livePref.value
  if (pref) {
    const row = (store.providers || []).find((p) => p.id === pref.provider_id)
    if (row) return row.label || row.name
  }
  return store.modelInfo?.provider || ''
})

/** 下拉按厂商分组；**当前生效的模型一定在列表里**并打勾。 */
const modelGroups = computed(() => {
  const groups = (store.providers || [])
    .filter((p) => p.configured)
    .map((p) => ({ provider: p, label: p.label || p.name, models: [...modelsOf(p)] }))

  const name = activeModelName.value
  if (name && !groups.some((g) => g.models.some((m) => m.model === name))) {
    // 当前模型没被任何厂商列出：挂到它所属的那家（偏好指定 or 名字对得上），
    // 都没有就单开一组。
    const owner = groups.find(
      (g) => g.provider.id === livePref.value?.provider_id
        || modelsOf(g.provider).some((m) => m.model === name)
        // .env 里的模型没有 provider_id，用厂商名对一下（大小写不敏感）
        || String(g.provider.name || '').toLowerCase()
           === String(store.modelInfo?.provider || '').toLowerCase(),
    )
    const item = {
      name,
      model: name,
      enabled: true,
      fromEnv: activeFromEnv.value,
    }
    if (owner) owner.models = [...owner.models, item]
    else groups.push({ provider: { id: '', name: '', label: '当前使用' }, label: '当前使用', models: [item] })
  }
  return groups.filter((g) => g.models.length)
})

async function chooseModel(provider, m) {
  modelOpen.value = false
  try {
    await api('/models/preferences', 'PUT', {
      task_type: 'chat', provider_id: provider.id, model_name: m.model, is_default: true,
    })
    await Promise.all([loadModelStatus(), loadChatPref()])
    notify(`对话模型已切到 ${m.name}`, 'ok')
  } catch (e) {
    notify('切换失败：' + (e.detail || e.message))
  }
}

// ---- 模型面板：对话 / 生图 / 视频 三行（D-47 配套；厂商与 Key 管理仍走设置弹窗） ----
const panelTab = ref('chat')      // 当前展开的维度行
const allProviders = ref([])      // 全 kind 厂商（面板打开时拉）
const modelPrefs = ref([])        // 全部任务偏好

async function refreshPanel() {
  try { allProviders.value = await api('/providers') } catch { allProviders.value = [] }
  try { modelPrefs.value = await api('/models/preferences') || [] } catch { modelPrefs.value = [] }
}

const imageProviders = computed(() =>
  (allProviders.value || []).filter((p) => p.kind === 'image' && p.configured))
const videoProviders = computed(() =>
  (allProviders.value || []).filter((p) => p.kind === 'video' && p.configured))

/** 当前在用的生图厂商：偏好指定的那家优先，否则第一个配好的——
    面板只列这一家（选厂商/换 key 是完整设置的职责，用户定 2026-11-12）。 */
const currentImageProvider = computed(() => {
  const prefId = imagePref.value?.provider_id
  return (prefId && imageProviders.value.find((p) => p.id === prefId))
    || imageProviders.value[0] || null
})

/** 生图 tab 的可选模型：当前厂商配置里的清单；厂商没配清单时，
    按厂商家族给常用档（minimax→H3-Image 系，其余→cogview 系），
    并保证在用的那个一定在列。 */
const imageModelChoices = computed(() => {
  const p = currentImageProvider.value
  if (!p) return []
  const listed = modelsOf(p).map((m) => m.model)
  const base = listed.length ? listed
    : (String(p.name).toLowerCase().includes('minimax')
        ? IMAGE_MODELS.filter((m) => m.startsWith('MiniMax'))
        : IMAGE_MODELS.filter((m) => !m.startsWith('MiniMax')))
  const cur = currentImageLabel.value
  if (cur && cur !== '未配置' && !base.includes(cur)) base.unshift(cur)
  return base
})

const imagePref = computed(() =>
  modelPrefs.value.find((x) => x.task_type === 'image_gen' && x.provider_id) || null)
const currentImageLabel = computed(() => {
  if (imagePref.value?.model_name) return imagePref.value.model_name
  const p = imageProviders.value[0]
  return p?.config?.model || '未配置'
})
const currentVideoLabel = computed(() => {
  const p = videoProviders.value.find((x) => x.name === 'minimax') || videoProviders.value[0]
  return p?.config?.model || '未配置'
})

async function chooseImageModel(p, m) {
  modelOpen.value = false
  try {
    await api('/models/preferences', 'PUT', {
      task_type: 'image_gen', provider_id: p.id, model_name: m, is_default: true,
    })
    await Promise.all([loadModelStatus(), refreshPanel()])
    notify(`生图模型已切到 ${m}`, 'ok')
  } catch (e) {
    notify('切换失败：' + (e.detail || e.message))
  }
}

/** 视频档位切换 = 改厂商行的 config.model（与设置页同路径，后端热生效）。 */
async function chooseVideoModel(p, m) {
  modelOpen.value = false
  try {
    await api(`/providers/${p.id}`, 'PATCH', { config: { ...(p.config || {}), model: m } })
    await refreshPanel()
    notify(`视频模型已切到 ${m}`, 'ok')
  } catch (e) {
    notify('切换失败：' + (e.detail || e.message))
  }
}

const VIDEO_TIERS = ['MiniMax-H3-Lite', 'MiniMax-H3-Highspeed', 'MiniMax-H3']
const IMAGE_MODELS = ['MiniMax-H3-Image', 'cogview-4-250304', 'cogview-3-flash']

watch(modelOpen, (v) => { if (v) refreshPanel() })

onMounted(() => {
  loadProviders()
  loadChatPref()
  loadModelStatus()
})

const menuOpen = ref(false)

// ---- 作品选择：这轮对话算在哪部作品下 ----
const projectOpen = ref(false)

const projectOptions = computed(() =>
  [...(store.projects || [])].sort((a, b) => {
    const at = a.last_used_at || a.created_at || ''
    const bt = b.last_used_at || b.created_at || ''
    return bt.localeCompare(at)
  }),
)

/** 胶囊里只列最近 3 个——多了就成清单了，完整列表在「全部作品」页。 */
const RECENT_LIMIT = 3
const recentProjects = computed(() => projectOptions.value.slice(0, RECENT_LIMIT))

const projectLabel = computed(() => {
  const cur = (store.projects || []).find((p) => p.id === store.pid)
  return cur ? cur.title : '选择作品'
})

function pickProject(p) {
  if (!p?.id) return
  projectOpen.value = false
  selectProject(p.id)
}

// ---- @ 引用资产：打 @ 弹候选，选中后插入 @名字 ----
const mentionOpen = ref(false)
const mentionQuery = ref('')
const mentionIndex = ref(0)
const textEl = ref(null)

const mentionItems = computed(() => {
  const q = mentionQuery.value.trim().toLowerCase()
  const list = props.assets || []
  if (!q) return list.slice(0, 12)
  return list.filter((a) => (a.name || '').toLowerCase().includes(q)).slice(0, 12)
})

/** 光标前是不是正打在 @ 上（@后面还没空格） */
function mentionContext() {
  const el = textEl.value
  if (!el) return null
  const upto = (el.value || '').slice(0, el.selectionStart || 0)
  const m = upto.match(/@([^\s@]*)$/)
  return m ? m[1] : null
}

function syncMention() {
  const q = mentionContext()
  if (q === null) { mentionOpen.value = false; return }
  mentionQuery.value = q
  mentionIndex.value = 0
  mentionOpen.value = true
}

/** 选中候选：把 @名字 插到光标处，并通知外层记下这条引用 */
function pickMention(item) {
  const el = textEl.value
  if (!el) return
  const pos = el.selectionStart || 0
  const before = (el.value || '').slice(0, pos)
  const m = before.match(/@[^\s@]*$/)
  const start = m ? pos - m[0].length : pos
  const ins = `@${item.name} `
  const next = (el.value || '').slice(0, start) + ins + (el.value || '').slice(pos)
  text.value = next
  mentionOpen.value = false
  emit('asset-picked', item)
  nextTick(() => {
    el.focus()
    const caret = start + ins.length
    el.setSelectionRange(caret, caret)
  })
}

function onMentionKey(e) {
  if (!mentionOpen.value) return
  const n = mentionItems.value.length
  if (e.key === 'ArrowDown') { e.preventDefault(); mentionIndex.value = n ? (mentionIndex.value + 1) % n : 0; return }
  if (e.key === 'ArrowUp') { e.preventDefault(); mentionIndex.value = n ? (mentionIndex.value - 1 + n) % n : 0; return }
  if (e.key === 'Escape') { e.preventDefault(); mentionOpen.value = false; return }
  if (e.key === 'Enter' || e.key === 'Tab') {
    const item = mentionItems.value[mentionIndex.value]
    if (item) { e.preventDefault(); e.stopPropagation(); pickMention(item) }
  }
}

function toggleMenu() {
  menuOpen.value = !menuOpen.value
}

function closeMenu(e) {
  if (menuOpen.value && e.target.closest && !e.target.closest('.cc-plus-wrap')) {
    menuOpen.value = false
  }
  if (projectOpen.value && e.target.closest && !e.target.closest('.cc-project-wrap')) {
    projectOpen.value = false
  }
  if (modelOpen.value && e.target.closest && !e.target.closest('.cc-model-wrap')) {
    modelOpen.value = false
  }
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    if (modelOpen.value) { modelOpen.value = false; return }
    if (projectOpen.value) { projectOpen.value = false; return }
    if (menuOpen.value) { menuOpen.value = false; return }
  }
  // @ 候选打开时，方向键与回车归候选列表
  if (mentionOpen.value && ['ArrowDown', 'ArrowUp', 'Escape'].includes(e.key)) {
    onMentionKey(e)
    return
  }
  if (mentionOpen.value && (e.key === 'Enter' || e.key === 'Tab') && mentionItems.value.length) {
    onMentionKey(e)
    return
  }
  if (e.key !== 'Enter' || e.shiftKey) return
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  emit('send')
}

function autoSize(e) {
  const el = e.target
  el.style.height = 'auto'
  el.style.height = Math.min(200, el.scrollHeight) + 'px'
}

/** 输入时：既调高度，也判断是不是正打在 @ 上 */
function onInput(e) {
  autoSize(e)
  syncMention()
}
</script>

<template>
  <div class="cc" @click="closeMenu">
    <div class="cc-card">
      <!-- 上：＋ 按钮 + 输入 -->
      <div class="cc-top">
        <div class="cc-plus-wrap">
          <button
            class="cc-plus"
            type="button"
            aria-label="添加素材"
            :aria-expanded="menuOpen"
            :disabled="busy"
            @click.stop="toggleMenu"
          >
            <AppIcon name="plus" />
          </button>
          <Transition name="ccpop">
            <div v-if="menuOpen" class="cc-menu" role="menu">
              <slot name="menu" :close="() => (menuOpen = false)" />
            </div>
          </Transition>
        </div>

        <div class="cc-input-wrap">
          <textarea
            ref="textEl"
            v-model="text"
            class="cc-text"
            rows="1"
            :placeholder="placeholder"
            @keydown="onKeydown"
            @input="onInput"
            @click="syncMention"
            @blur="mentionOpen = false"
          ></textarea>
          <slot name="attachments" />

          <!-- @ 候选：打 @ 弹出资产库里的角色 / 场景 / 成片 -->
          <Transition name="ccpop">
            <div v-if="mentionOpen && mentionItems.length" class="cc-mention" role="listbox">
              <div class="cc-mention-head">引用资产</div>
              <button
                v-for="(it, i) in mentionItems"
                :key="it.kind + ':' + it.id"
                :class="['menu-item', { on: i === mentionIndex }]"
                type="button"
                role="option"
                :aria-selected="i === mentionIndex"
                @mousedown.prevent="pickMention(it)"
                @mouseenter="mentionIndex = i"
              >
                <span class="mi-name">{{ it.name }}</span>
                <span class="mi-sub">{{ it.sub }}</span>
                <span class="mi-id">{{ it.id }}</span>
              </button>
            </div>
          </Transition>
        </div>
      </div>

      <!-- 下：胶囊行 + 齿轮 + 发送 -->
      <div class="cc-bottom">
        <div class="cc-pills">
          <!-- 作品选择：决定这轮对话挂在哪部作品下 -->
          <div class="cc-project-wrap">
            <button
              class="chip cc-project"
              type="button"
              title="选择这轮对话所属的作品"
              @click.stop="projectOpen = !projectOpen"
            >
              <AppIcon name="folder" />
              <span class="cp-label">{{ projectLabel }}</span>
              <AppIcon name="chevron-down" class="cc-chev" />
            </button>
            <Transition name="ccpop">
              <div v-if="projectOpen" class="cc-menu cc-projects" role="menu">
                <div class="cc-menu-head">这轮对话属于哪部作品</div>
                <button
                  v-for="p in recentProjects"
                  :key="p.id"
                  :class="['menu-item', { on: p.id === store.pid }]"
                  type="button"
                  role="menuitem"
                  @click.stop="pickProject(p)"
                >
                  <span class="pi-title">{{ p.title }}</span>
                  <span class="pi-meta">{{ p.version_count || 0 }} 版</span>
                </button>
                <div v-if="!recentProjects.length" class="cc-menu-empty">还没有作品</div>
                <div class="cc-menu-sep"></div>
                <button
                  class="menu-item"
                  type="button"
                  role="menuitem"
                  @click.stop="projectOpen = false; emit('new-project')"
                ><span class="pi-title">新建作品</span></button>
              </div>
            </Transition>
          </div>
          <!-- 阶段状态不在这里重复：对话顶部 StatusStrip 已带光点+耗时，
               输入条再挂一颗就是双份（用户指正 2026-11-13）。 -->
          <!-- 模型：下拉内容 = 当前供应商在「模型设置」里配好的模型清单 -->
          <div class="cc-model-wrap">
            <button
              class="chip cc-model"
              type="button"
              title="选择对话模型"
              @click.stop="modelOpen = !modelOpen"
            >
              <AppIcon name="gear" />
              <!-- 徽标常驻「模型设置」：模型名太长显示不过来，徽标不塞具体名字，
                   当前用了谁在面板三行里看（用户定，2026-11-12）。 -->
              <span>模型设置</span>
              <AppIcon name="chevron-down" class="cc-chev" />
            </button>
            <Transition name="ccpop">
              <div v-if="modelOpen" class="cc-model-pop mp-panel" role="menu">
                <!-- 三行：对话 / 生图 / 视频，点击行内展开该维度清单 -->
                <button class="mp-row" type="button" :class="{ on: panelTab === 'chat' }"
                        @click.stop="panelTab = 'chat'">
                  <span class="mr-key">对话</span>
                  <span class="mr-val">{{ modelLabel }}</span>
                  <AppIcon name="chevron-right" class="mr-chev" />
                </button>
                <button class="mp-row" type="button" :class="{ on: panelTab === 'image' }"
                        @click.stop="panelTab = 'image'">
                  <span class="mr-key">生图</span>
                  <span class="mr-val">{{ currentImageLabel }}</span>
                  <AppIcon name="chevron-right" class="mr-chev" />
                </button>
                <button class="mp-row" type="button" :class="{ on: panelTab === 'video' }"
                        @click.stop="panelTab = 'video'">
                  <span class="mr-key">视频</span>
                  <span class="mr-val">{{ currentVideoLabel }}</span>
                  <AppIcon name="chevron-right" class="mr-chev" />
                </button>

                <!-- 展开区：当前 tab 的可选清单 -->
                <div class="mp-body">
                  <!-- 对话：只列当前厂商的模型清单（换厂商/换 key 去完整设置） -->
                  <template v-if="panelTab === 'chat'">
                    <div class="mp-label">{{ activeVendor || '当前厂商' }}</div>
                    <button v-for="m in chatModels" :key="m.model"
                            class="mp-item" type="button" role="menuitemradio"
                            :aria-checked="m.model === activeModelName"
                            @click.stop="chooseModel(currentProvider, m)">
                      <span class="mi-name">{{ m.name }}</span>
                      <span class="mi-model">{{ m.model }}</span>
                      <span v-if="m.model === activeModelName" class="mi-check">✓</span>
                    </button>
                    <div v-if="!chatModels.length" class="mp-empty">当前厂商还没配模型。</div>
                  </template>

                  <!-- 生图：只列当前在用厂商的模型清单（换厂商去完整设置） -->
                  <template v-else-if="panelTab === 'image'">
                    <template v-if="currentImageProvider">
                      <div class="mp-label">{{ currentImageProvider.label || currentImageProvider.name }}</div>
                      <button v-for="m in imageModelChoices" :key="m"
                              class="mp-item" type="button" role="menuitemradio"
                              :aria-checked="m === currentImageLabel"
                              @click.stop="chooseImageModel(currentImageProvider, m)">
                        <span class="mi-name">{{ m }}</span>
                        <span v-if="m === currentImageLabel" class="mi-check">✓</span>
                      </button>
                    </template>
                    <div v-else class="mp-empty">还没有配置生图厂商，去「完整模型设置」里加。</div>
                  </template>

                  <!-- 视频：H3 三档（改厂商 config.model） -->
                  <template v-else-if="panelTab === 'video'">
                    <div class="mp-label">MiniMax H3 档位</div>
                    <button v-for="m in VIDEO_TIERS" :key="m" class="mp-item" type="button"
                            role="menuitemradio" :aria-checked="m === currentVideoLabel"
                            @click.stop="videoProviders[0] && chooseVideoModel(videoProviders[0], m)">
                      <span class="mi-name">{{ m }}</span>
                      <span v-if="m === currentVideoLabel" class="mi-check">✓</span>
                    </button>
                    <div v-if="!videoProviders.length" class="mp-empty">还没有配置视频厂商。</div>
                  </template>
                </div>

                <button class="mp-more" type="button" @click.stop="modelOpen = false; emit('open-settings')">
                  完整模型设置（厂商 / Key）<AppIcon name="chevron-right" />
                </button>
              </div>
            </Transition>
          </div>

          <span v-if="hintText" class="cc-hint">{{ hintText }}</span>
        </div>

        <div class="cc-right">
          <button class="cc-gear" type="button" title="设置" aria-label="设置" @click.stop="emit('open-settings')">
            <AppIcon name="gear" />
          </button>
          <button
            class="cc-send"
            type="button"
            :disabled="!canSend"
            :aria-label="canSend ? '发送' : '输入内容后发送'"
            title="发送（Enter）"
            @click.stop="emit('send')"
          >
            <AppIcon name="send" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cc { width: 100%; }

.cc-card {
  position: relative;
  border-radius: var(--radius-card);
  padding: 14px 14px 12px;
  background: rgba(255, 255, 255, 0.055);
  border: 1px solid var(--line);
  backdrop-filter: var(--glass);
  -webkit-backdrop-filter: var(--glass);
  box-shadow: var(--shadow-3);
  transition: border-color 0.18s ease, background 0.18s ease;
}

.cc-card:focus-within {
  border-color: rgba(255, 255, 255, 0.2);
  background: rgba(255, 255, 255, 0.075);
}

.cc-top { display: flex; align-items: flex-start; gap: 12px; }

.cc-plus-wrap { position: relative; flex: none; }

.cc-plus {
  width: 56px;
  height: 56px;
  border-radius: 14px;
  display: grid;
  place-items: center;
  color: rgba(255, 255, 255, 0.82);
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.07);
  cursor: pointer;
  transition: background 0.16s ease, color 0.16s ease;
}

.cc-plus:hover:not(:disabled) { background: rgba(255, 255, 255, 0.17); color: #fff; }
.cc-plus:disabled { opacity: 0.4; cursor: not-allowed; }

.cc-menu {
  position: absolute;
  bottom: calc(100% + 10px);
  left: 0;
  min-width: 200px;
  padding: 6px;
  border-radius: var(--radius-lg);
  background: rgba(22, 22, 28, 0.97);
  border: 1px solid rgba(255, 255, 255, 0.1);
  box-shadow: var(--shadow-3);
  z-index: 30;
}

.cc-input-wrap { position: relative; flex: 1; min-width: 0; padding-top: 14px; }

.cc-text {
  width: 100%;
  min-height: 84px;
  max-height: 200px;
  resize: none;
  border: 0;
  outline: 0;
  background: transparent;
  color: rgba(255, 255, 255, 0.94);
  font-size: 14px;
  line-height: 1.65;
  font-family: inherit;
}
/* 聚焦不给任何特效：全局 :focus-visible 的绿色光环套在输入框里会变成「框里还有框」。
   卡片本身的 focus-within（底色微亮）已经够说明「正在输入」。 */
.cc-text:focus,
.cc-text:focus-visible {
  outline: none;
  box-shadow: none;
  border: 0;
}

.cc-text::placeholder { color: rgba(255, 255, 255, 0.34); }

.cc-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
}

.cc-pills {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.cc-chev { opacity: 0.5; }

.cc-hint {
  font-size: 12px;
  color: var(--dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cc-right { display: flex; align-items: center; gap: 8px; flex: none; }

/* 齿轮：低调的图标钮 */
.cc-gear {
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: var(--radius-md);
  display: grid;
  place-items: center;
  background: transparent;
  border: 1px solid transparent;
  color: var(--dim);
  cursor: pointer;
  transition: color 130ms var(--ease), background 130ms var(--ease);
}
/* @ 候选列表：贴着输入框上方浮起 */
.cc-mention {
  position: absolute; left: 0; right: 0; bottom: calc(100% + 6px); z-index: 30;
  max-height: 240px; overflow-y: auto;
  background: var(--panel-solid); border: 1px solid var(--line);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-2);
  padding: 4px;
}
.cc-mention-head {
  padding: 5px 9px 6px; font-size: 10px; color: var(--dim);
  letter-spacing: .04em; border-bottom: 1px solid var(--line); margin-bottom: 3px;
}
.mi-name { color: var(--ink); font-weight: 500; flex: none; }
.mi-sub { font-size: 10px; color: var(--dim); flex: none; }
.mi-id { margin-left: auto; font-family: var(--mono); font-size: 10px; color: var(--dim); }

.cc-gear:hover { color: var(--ink); background: rgba(255, 255, 255, .07); }

/* 作品选择胶囊 + 下拉 */
.cc-project-wrap { position: relative; }
.cc-model-wrap { position: relative; }
.cc-model { max-width: 190px; }
.cc-model span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 模型下拉：标记与样式同在 ComposerCard（别再放插槽——插槽内容带父组件的
   scope id，scoped 样式匹配不上，弹层会变成内联的一行）。 */
.cc-model-pop {
  position: absolute; bottom: calc(100% + 6px); left: 0; z-index: 40;
  min-width: 260px; max-width: 340px;
  background: var(--panel-solid); border: 1px solid var(--line);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-2);
  padding: 4px;
}
.mp-head {
  display: flex; align-items: baseline; gap: 8px;
  padding: 7px 9px; border-bottom: 1px solid var(--line); margin-bottom: 3px;
}
.mp-provider { font-size: var(--fs-sm); color: var(--ink); font-weight: 600; }
.mp-hint { margin-left: auto; font-size: 10px; color: var(--dim); }
.mp-label {
  padding: 7px 9px 4px; font-size: 10px; color: var(--muted);
  letter-spacing: .04em; font-weight: 600;
}
.mp-label:first-of-type { padding-top: 5px; }
.mp-item {
  display: flex; align-items: baseline; gap: 8px; width: 100%;
  padding: 7px 9px; border-radius: var(--radius-md);
  background: transparent; border: none; color: var(--muted);
  font-size: var(--fs-sm); font-family: inherit; text-align: left; cursor: pointer;
}
.mp-item:hover { background: rgba(255, 255, 255, .08); color: var(--ink); }
.mp-item.static { cursor: default; color: var(--muted); }
.mp-item.static:hover { background: transparent; }
.mp-item[aria-checked="true"] { color: var(--ink); }
.mp-item .mi-name { color: var(--ink); font-weight: 500; flex: none; }
.mp-item .mi-model {
  font-family: var(--mono); font-size: 10px; color: var(--dim);
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mp-item .mi-check { color: var(--accent); flex: none; font-size: 12px; }
.mp-item .mi-src { flex: none; font-size: 10px; color: var(--dim); }

/* 模型面板三行（对话/生图/视频）：行=维度+当前值，点击切展开区 */
.mp-panel { min-width: 280px; }
.mp-row {
  display: flex; align-items: center; gap: 10px; width: 100%;
  padding: 8px 10px; border: none; border-radius: var(--radius-md);
  background: transparent; color: var(--muted);
  font-size: var(--fs-sm); font-family: inherit; text-align: left; cursor: pointer;
}
.mp-row:hover { background: rgba(255, 255, 255, .06); }
.mp-row.on { background: rgba(255, 255, 255, .08); color: var(--ink); }
.mp-row .mr-key { flex: none; width: 34px; color: var(--muted); font-weight: 600; }
.mp-row.on .mr-key { color: var(--ink); }
.mp-row .mr-val {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: var(--mono); font-size: 11px; color: var(--dim);
}
.mp-row.on .mr-val { color: var(--muted); }
.mp-row .mr-chev { flex: none; width: 11px; height: 11px; opacity: .4; transform: rotate(90deg); }
.mp-row.on .mr-chev { opacity: .8; }
.mp-body {
  border-top: 1px solid var(--line); margin-top: 3px; padding-top: 2px;
  max-height: 240px; overflow-y: auto;
}
.mp-more {
  display: flex; align-items: center; gap: 6px; width: 100%;
  margin-top: 3px; padding: 8px 10px;
  border: none; border-top: 1px solid var(--line); border-radius: 0 0 var(--radius-lg) var(--radius-lg);
  background: transparent; color: var(--muted);
  font-size: var(--fs-sm); font-family: inherit; text-align: left; cursor: pointer;
}
.mp-more:hover { color: var(--ink); background: rgba(255, 255, 255, .05); }
.mp-more .app-ic { width: 11px; height: 11px; margin-left: auto; opacity: .5; }
.mp-empty { padding: 9px; font-size: var(--fs-xs); color: var(--dim); line-height: 1.6; }
.cc-project { max-width: 190px; }
/* 作品名超长就截断：不给它撑破胶囊的宽度 */
.cc-project .cp-label {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cc-projects {
  bottom: calc(100% + 6px); left: 0; right: auto; min-width: 220px;
  background: var(--panel-solid); border: 1px solid var(--line);
  border-radius: var(--radius-lg); padding: 4px; box-shadow: var(--shadow-2);
}
.cc-menu-head {
  padding: 5px 9px 6px; font-size: 10px; color: var(--dim);
  letter-spacing: .04em; border-bottom: 1px solid var(--line); margin-bottom: 3px;
}
.cc-menu-empty { padding: 8px 9px; font-size: var(--fs-sm); color: var(--dim); }
.pi-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pi-meta { margin-left: auto; font-size: 10px; color: var(--dim); flex: none; }
.pi-chev { width: 13px; height: 13px; flex: none; opacity: .5; }
.cc-menu-sep { height: 1px; background: var(--line); margin: 4px 2px; }
.cc-gear .app-ic { width: 15px; height: 15px; }

/* 发送：圆形箭头钮。
   可用状态用**全站主行动绿**（--accent-grad，与首页 CTA 同一套渐变）——
   自创的柠檬绿 (#d9f99d→#4ade80) 颜色难看，与首页也不像一家人；
   回到设计系统的绿后既醒目又和首页一致。 */
.cc-send {
  width: 34px;
  height: 34px;
  padding: 0;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: rgba(255, 255, 255, .07);
  border: 1px solid var(--line);
  color: rgba(255, 255, 255, .34);
  cursor: not-allowed;
  transition: background 150ms var(--ease), color 150ms var(--ease),
              border-color 150ms var(--ease), box-shadow 150ms var(--ease), filter 150ms var(--ease);
}
.cc-send:not(:disabled) {
  background: var(--accent-grad);
  border-color: transparent;
  color: var(--on-accent);
  cursor: pointer;
  box-shadow: 0 0 0 4px rgba(74, 222, 128, .14),
              0 8px 22px rgba(74, 222, 128, .42);
}
.cc-send:not(:disabled):hover {
  filter: brightness(1.06);
  box-shadow: 0 0 0 5px rgba(74, 222, 128, .2),
              0 10px 26px rgba(74, 222, 128, .55);
}
.cc-send:not(:disabled):active { filter: brightness(.97); }
.cc-send .app-ic { width: 16px; height: 16px; transform: translateY(-0.5px); }

.ccpop-enter-active,
.ccpop-leave-active { transition: opacity 0.14s ease, transform 0.14s ease; }
.ccpop-enter-from,
.ccpop-leave-to { opacity: 0; transform: translateY(6px); }
</style>
