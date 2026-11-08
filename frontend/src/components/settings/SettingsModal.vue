<template>
  <Teleport to="body">
    <div v-if="open" class="modal-backdrop" @click.self="$emit('close')">
      <div class="modal-card wide">
        <div class="modal-head">
          <div>
            <h3 class="modal-title">模型与 API 设置</h3>
            <p class="modal-sub">对话、出图、视频分别用哪个模型，都在这里定</p>
          </div>
          <button class="modal-close" title="关闭" aria-label="关闭" @click="$emit('close')">
            <AppIcon name="close" />
          </button>
        </div>

        <div class="seg-tabs settings-tabs">
          <button
            v-for="(t, i) in tabs"
            :key="t.key"
            :class="['seg-tab', { active: tab === t.key }]"
            @click="tab = t.key"
          ><span class="seg-num">{{ String(i + 1).padStart(2, '0') }}</span>{{ t.label }}</button>
        </div>

        <div class="modal-body">
          <!-- 对话模型：左厂商列表 + 右详情面板（学 ZCode 的结构，不做厂商分类） -->
          <div v-if="tab === 'llm'" class="section llm-split">
            <!-- 左：厂商（扁平，不分组） -->
            <aside class="llm-side">
              <button
                v-for="p in llmProviders"
                :key="p.id"
                :class="['llm-side-item', { on: selectedProviderId === p.id }]"
                type="button"
                @click="selectedProviderId = p.id"
              >
                <span class="side-dot" :class="{ on: p.configured }"></span>
                <span class="side-name">{{ p.label || p.name }}</span>
                <span class="side-count">{{ providerModels(p).length }}</span>
              </button>
              <div v-if="!llmProviders.length" class="pop-empty">还没有厂商</div>
              <button class="llm-side-add" type="button" @click="openAddLlm()">+ 添加厂商</button>
            </aside>

            <!-- 右：选中厂商的设置 + 模型列表 -->
            <div class="llm-detail">
              <template v-if="selectedProvider">
                <div class="detail-head">
                  <span class="dh-name">{{ selectedProvider.label || selectedProvider.name }}</span>
                  <span v-if="chatPref?.provider_id === selectedProvider.id" class="using-badge">使用中</span>
                  <span class="dh-actions">
                    <button
                      v-if="chatPref?.provider_id !== selectedProvider.id"
                      class="mr-btn ok"
                      :disabled="!selectedProvider.configured"
                      @click="setAsDefault(selectedProvider)"
                    >设为默认</button>
                    <button class="mr-btn" :disabled="testingId === selectedProvider.id" @click="testProvider(selectedProvider)">
                      {{ testingId === selectedProvider.id ? '测试中…' : '测试连接' }}
                    </button>
                    <button class="mr-btn danger" @click="deleteLlm(selectedProvider)">删除厂商</button>
                  </span>
                </div>

                <div class="form-row">
                  <label>Base URL</label>
                  <input v-model.trim="providerDraft.base_url" class="modal-input" placeholder="https://api.deepseek.com/v1" />
                </div>
                <div class="form-row">
                  <label>API 格式</label>
                  <div class="format-fixed">OpenAI 兼容（v1/chat/completions）</div>
                </div>
                <div class="form-row">
                  <label>API Key</label>
                  <div class="key-row">
                    <input
                      :type="showProviderKey ? 'text' : 'password'"
                      v-model.trim="providerDraft.api_key"
                      class="modal-input"
                      :placeholder="selectedProvider.configured ? '已配置（留空 = 保持不变）' : 'sk-...'"
                    />
                    <button class="mr-btn" @click="showProviderKey = !showProviderKey">{{ showProviderKey ? '隐藏' : '显示' }}</button>
                  </div>
                </div>
                <div class="form-actions">
                  <button class="primary" :disabled="savingProvider" @click="saveProvider">
                    {{ savingProvider ? '保存中…' : '保存厂商设置' }}
                  </button>
                </div>

                <div class="detail-sub">
                  <span class="ds-title">模型列表</span>
                  <button class="add-model" type="button" @click="openAddModel(selectedProvider)">+ 添加模型</button>
                </div>
                <div class="model-list">
                  <div v-for="(m, mi) in providerModels(selectedProvider)" :key="m.model + mi" class="model-row">
                    <span class="mr-name">{{ m.name }}</span>
                    <span class="mr-id">{{ m.model }}</span>
                    <button class="mr-btn" title="编辑" @click="editModel(selectedProvider, mi)">改</button>
                    <button class="mr-btn danger" title="删除" @click="removeModel(selectedProvider, mi)">删</button>
                    <button
                      class="mr-toggle"
                      :class="{ on: m.enabled }"
                      :title="m.enabled ? '停用这个模型' : '启用这个模型'"
                      @click="toggleModel(selectedProvider, mi)"
                    ><span></span></button>
                  </div>
                  <div v-if="!providerModels(selectedProvider).length" class="pop-empty">
                    这个厂商还没有列出模型。加一个，或让 .env 提供默认模型名。
                  </div>
                </div>
              </template>
              <div v-else class="pop-empty">左边选一个厂商。</div>

              <!-- 添加/编辑厂商 -->
              <div v-if="showAddLlm || editingLlmRow" class="sub-modal llm-form">
                <h5>{{ editingLlmRow ? '编辑厂商' : '添加厂商' }}</h5>
                <div class="form-row">
                  <label>厂商预设</label>
                  <select v-model="llmForm.preset" class="modal-select" :disabled="!!editingLlmRow" @change="applyPreset()">
                    <option value="deepseek">DeepSeek</option>
                    <option value="zhipu">智谱 (BigModel)</option>
                    <option value="openai">OpenAI 兼容</option>
                    <option value="custom">自定义</option>
                  </select>
                </div>
                <div v-if="llmForm.preset === 'custom'" class="form-row">
                  <label>名称（唯一标识）</label>
                  <input v-model.trim="llmForm.name" class="modal-input" placeholder="my-provider" />
                </div>
                <div class="form-row">
                  <label>显示名</label>
                  <input v-model.trim="llmForm.label" class="modal-input" placeholder="我的模型" />
                </div>
                <div class="form-row">
                  <label>Base URL</label>
                  <input v-model.trim="llmForm.baseUrl" class="modal-input" placeholder="https://api.deepseek.com/v1" />
                </div>
                <div class="form-row">
                  <label>模型名</label>
                  <input v-model.trim="llmForm.model" class="modal-input" placeholder="deepseek-flash" />
                </div>
                <div class="form-row">
                  <label>API Key</label>
                  <div class="key-row">
                    <input
                      :type="showKey ? 'text' : 'password'"
                      v-model="llmForm.apiKey"
                      class="modal-input"
                      :placeholder="editingLlmRow ? '留空 = 保持不变' : 'sk-...'"
                    />
                    <button class="mr-btn" @click="showKey = !showKey">{{ showKey ? '隐藏' : '显示' }}</button>
                  </div>
                </div>
                <div v-if="llmFormError" class="test-result err">{{ llmFormError }}</div>
                <div class="form-actions">
                  <button class="primary" @click="saveLlm" :disabled="savingLlm">
                    {{ savingLlm ? '保存中…' : '保存' }}
                  </button>
                  <button class="ghost" @click="closeLlmForm">取消</button>
                </div>
              </div>

              <!-- 添加/编辑模型（行内表单） -->
              <div v-if="modelForm.open" class="sub-modal model-form">
                <h5>{{ modelForm.index >= 0 ? '编辑模型' : '添加模型' }}</h5>
                <div class="form-row">
                  <label>显示名</label>
                  <input v-model.trim="modelForm.name" class="modal-input" placeholder="例如 GLM-5.3" />
                </div>
                <div class="form-row">
                  <label>模型名</label>
                  <input v-model.trim="modelForm.model" class="modal-input" placeholder="例如 glm-5.3" />
                </div>
                <div class="form-actions">
                  <button class="primary" @click="submitModel">保存</button>
                  <button class="ghost" @click="modelForm.open = false">取消</button>
                </div>
              </div>
            </div>
          </div>

          <!-- 视频生成 -->
          <div v-if="tab === 'video'" class="section">
            <h4>视频生成（MiniMax 海螺）</h4>
            <div class="active-line">
              <template v-if="minimaxRow">
                <span :class="['status-dot', minimaxRow.configured ? 'on' : 'off']"></span>
                已配置 <strong>MiniMax</strong>
                · 模型 <strong>{{ minimaxModelLabel }}</strong>
                <span class="active-src">用自己的 Key，费用直接走你的 MiniMax 账户</span>
              </template>
              <template v-else>
                填入你的 MiniMax API Key 后即可生成视频
              </template>
            </div>
            <div class="form-row">
              <label>API Key</label>
              <div class="key-row">
                <input
                  :type="showVideoKey ? 'text' : 'password'"
                  v-model="videoForm.apiKey"
                  class="modal-input"
                  :placeholder="minimaxRow?.configured ? '留空 = 保持不变' : 'MiniMax 开放平台 API Key'"
                />
                <button class="small" @click="showVideoKey = !showVideoKey">{{ showVideoKey ? '隐藏' : '显示' }}</button>
              </div>
            </div>
            <div class="form-row">
              <label>模型</label>
              <select v-model="videoForm.model" class="modal-select">
                <option v-for="opt in VIDEO_MODEL_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                <option value="custom">自定义…</option>
              </select>
            </div>
            <div v-if="videoForm.model === 'custom'" class="form-row">
              <label>自定义模型名</label>
              <input v-model.trim="videoForm.customModel" class="modal-input" placeholder="例如 MiniMax-H3" />
            </div>
            <div class="form-row">
              <label>Base URL（可选）</label>
              <input v-model.trim="videoForm.baseUrl" class="modal-input" placeholder="默认 https://api.minimax.cn" />
            </div>
            <p class="pref-hint">
              海外账户填 https://api.minimaxi.com；Key 与模型只存在本机数据库，不经过任何第三方。
            </p>
            <div class="form-actions">
              <button class="primary" @click="saveMinimax">保存</button>
            </div>
          </div>

          <!-- 生图模型 -->
          <div v-if="tab === 'image'" class="section">
            <h4>生图模型（定妆图 / 场景参考图）</h4>
            <div class="active-line">
              <template v-if="imageInfo.available">
                当前生效：<strong>{{ imageInfo.label || imageInfo.provider }}</strong>
                <template v-if="imageInfo.model"> · {{ imageInfo.model }}</template>
                <span class="active-src">{{ imageSourceLabel }}</span>
              </template>
              <template v-else>未配置生图模型（需要智谱或 OpenAI 的 API Key）</template>
            </div>
            <p class="pref-hint">
              生图与智谱 GLM 共用同一个 API Key，无需重复填写；在这里选择定妆图、场景参考图用哪个模型出图。
            </p>
            <div class="form-row">
              <label>模型</label>
              <select v-model="imageForm.model" class="modal-select">
                <option v-for="opt in IMAGE_MODEL_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                <option value="custom">自定义…</option>
              </select>
            </div>
            <div v-if="imageForm.model === 'custom'" class="form-row">
              <label>自定义模型名</label>
              <input v-model.trim="imageForm.customModel" class="modal-input" placeholder="例如 cogview-4" />
            </div>
            <div class="form-actions">
              <button class="primary" @click="saveImageModel" :disabled="savingImage">
                {{ savingImage ? '保存中…' : '保存' }}
              </button>
              <button v-if="imageInfo.source === 'db'" class="ghost" @click="followEnvImage">恢复 .env 默认</button>
            </div>
          </div>

          <!-- 模型偏好 -->
          <div v-if="tab === 'prefs'" class="section">
            <h4>任务模型偏好</h4>
            <div class="active-line">
              对话任务：<strong>{{ chatPrefLabel }}</strong>
              <span class="active-src">{{ chatPref ? '界面指定' : '跟随 .env' }}</span>
            </div>
            <p class="pref-hint">
              在「对话模型」里选中厂商后点「设为默认」即可切换；切回 .env 默认用「恢复 .env 默认」。
            </p>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { api } from '../../api.js'
import AppIcon from '../AppIcon.vue'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close'])

const tab = ref('llm')
const tabs = [
  { key: 'llm', label: '对话模型' },
  { key: 'image', label: '生图模型' },
  { key: 'video', label: '视频生成' },
  { key: 'prefs', label: '模型偏好' },
]

// ---- 可选模型清单（value=custom 时用自定义输入框） ----
const IMAGE_MODEL_OPTIONS = [
  { value: 'cogview-3-flash', label: 'CogView-3-Flash（速度快，有免费额度）' },
  { value: 'cogview-4-250304', label: 'CogView-4（质量更好，推荐出定妆图）' },
  { value: 'cogview-4', label: 'CogView-4（最新版本号）' },
]
const VIDEO_MODEL_OPTIONS = [
  { value: 'MiniMax-H3', label: 'MiniMax-H3（默认，性价比高）' },
  { value: 'MiniMax-Hailuo-02', label: 'MiniMax-Hailuo-02（质量更好）' },
  { value: 'T2V-01', label: 'T2V-01（文生视频经典款）' },
  { value: 'I2V-01', label: 'I2V-01（图生视频）' },
]

// ---- 对话模型 ----
const showKey = ref(false)
const llmForm = reactive({ provider: 'openai', apiKey: '', baseUrl: '', model: '' })
const llmProviders = ref([])

// ---- 右侧详情面板：选中的厂商 + 待保存的字段 ----
const selectedProviderId = ref('')
const providerDraft = reactive({ base_url: '', api_key: '' })
const showProviderKey = ref(false)
const savingProvider = ref(false)
const testingId = ref('')

const selectedProvider = computed(
  () => (llmProviders.value || []).find((p) => p.id === selectedProviderId.value) || null,
)

/** 选中厂商变化 → 把它的 Base URL 载进草稿（key 不回显，只提示「已配置」） */
watch(selectedProvider, (p) => {
  providerDraft.base_url = p?.base_url || ''
  providerDraft.api_key = ''
  showProviderKey.value = false
})

/** 列表加载后：默认选中「使用中」的那个，否则第一个 */
watch(llmProviders, (list) => {
  if (!list.length) { selectedProviderId.value = ''; return }
  const still = list.find((p) => p.id === selectedProviderId.value)
  if (still) return
  const pref = list.find((p) => p.id === chatPref.value?.provider_id)
  selectedProviderId.value = (pref || list[0]).id
}, { immediate: true })

async function saveProvider() {
  const p = selectedProvider.value
  if (!p) return
  savingProvider.value = true
  try {
    const patch = { base_url: providerDraft.base_url }
    if (providerDraft.api_key) patch.api_key = providerDraft.api_key
    await api('/providers/' + p.id, 'PATCH', patch)
    providerDraft.api_key = ''
    await loadProviders()
    notify('厂商设置已保存', 'ok')
  } catch (e) {
    notify('保存失败：' + e.message)
  } finally {
    savingProvider.value = false
  }
}

/** 一个厂商下的模型清单（老配置只有单个 model 时视作一条）。 */
function providerModels(p) {
  const list = p?.config?.models
  if (Array.isArray(list) && list.length) {
    return list
      .filter((m) => m && m.model)
      .map((m) => ({ name: m.name || m.model, model: m.model, enabled: m.enabled !== false }))
  }
  const one = p?.config?.model
  return one ? [{ name: one, model: one, enabled: true }] : []
}

/** 当前生效的模型 = 第一个启用的。 */
function activeOf(models) {
  const on = models.find((m) => m.enabled)
  return on ? on.model : ''
}

/** 把模型清单写回厂商配置（同时同步 config.model，老代码路径继续可用）。 */
async function saveModels(p, models) {
  const config = { ...(p.config || {}), models, model: activeOf(models) }
  try {
    await api('/providers/' + p.id, 'PATCH', { config })
    await loadProviders()
    notify('已更新模型清单', 'ok')
  } catch (e) {
    notify('更新失败：' + e.message)
  }
}

function toggleModel(p, index) {
  const models = providerModels(p).map((m, i) => (i === index ? { ...m, enabled: !m.enabled } : m))
  return saveModels(p, models)
}

function removeModel(p, index) {
  const models = providerModels(p).filter((_, i) => i !== index)
  return saveModels(p, models)
}

// 添加 / 编辑模型的行内表单
const modelForm = reactive({ open: false, provider: null, index: -1, name: '', model: '' })
function openAddModel(p) {
  modelForm.open = true
  modelForm.provider = p
  modelForm.index = -1
  modelForm.name = ''
  modelForm.model = ''
}
function editModel(p, index) {
  const m = providerModels(p)[index]
  if (!m) return
  modelForm.open = true
  modelForm.provider = p
  modelForm.index = index
  modelForm.name = m.name
  modelForm.model = m.model
}
async function submitModel() {
  const p = modelForm.provider
  const name = modelForm.name.trim() || modelForm.model.trim()
  const model = modelForm.model.trim()
  if (!p || !model) { notify('模型名必填'); return }
  const models = providerModels(p).slice()
  const item = { name, model, enabled: true }
  if (modelForm.index >= 0) models[modelForm.index] = item
  else models.push(item)
  modelForm.open = false
  await saveModels(p, models)
}
const activeModel = ref({ provider: '-', name: '' })
const activeSource = ref('env')
const chatPref = ref(null)          // task_type=chat 的偏好（含 provider_id）
const showAddLlm = ref(false)
const editingLlmRow = ref(null)
const llmFormError = ref('')
const savingLlm = ref(false)

const PRESETS = {
  deepseek: { label: 'DeepSeek', name: 'deepseek', baseUrl: 'https://api.deepseek.com/v1', model: 'deepseek-flash' },
  zhipu: { label: '智谱 (BigModel)', name: 'zhipu', baseUrl: 'https://open.bigmodel.cn/api/paas/v4/', model: 'GLM-5.3-Flash' },
  openai: { label: 'OpenAI 兼容', name: 'openai', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  custom: { label: '', name: '', baseUrl: '', model: '' },
}
const PRESET_LABELS = { deepseek: 'DeepSeek', zhipu: '智谱', openai: 'OpenAI 兼容' }

const activeSourceLabel = computed(() =>
  ({ db: '界面指定', env: '.env 配置', default: '未配置' }[activeSource.value] || activeSource.value))
const chatPrefLabel = computed(() => {
  if (!chatPref.value) return '跟随 .env 默认'
  const p = llmProviders.value.find(x => x.id === chatPref.value.provider_id)
  const model = chatPref.value.model_name || p?.config?.model || ''
  return (p?.label || chatPref.value.provider_id) + (model ? ' · ' + model : '')
})
const envModelLabel = computed(() => {
  if (activeSource.value !== 'env') return '见 .env'
  return activeModel.value.provider + (activeModel.value.name ? ' · ' + activeModel.value.name : '')
})

async function loadModelInfo() {
  try {
    const d = await api('/status')
    activeModel.value = d.model || { provider: '-', name: '' }
    activeSource.value = d.model?.source || 'env'
  } catch { /* 保持占位 */ }
}

async function loadChatPref() {
  try {
    const prefs = await api('/models/preferences')
    chatPref.value = prefs.find(x => x.task_type === 'chat' && x.provider_id) || null
  } catch { chatPref.value = null }
}

async function setAsDefault(p) {
  try {
    await api('/models/preferences', 'PUT', {
      task_type: 'chat',
      provider_id: p.id,
      model_name: p.config?.model || '',
      is_default: true,
    })
    await Promise.all([loadChatPref(), loadModelInfo(), loadProviders()])
  } catch (e) {
    alert('设置失败：' + e.message)
  }
}

async function followEnv() {
  try {
    // provider_id 置空 = 清除界面指定，回落到 .env 链
    await api('/models/preferences', 'PUT', {
      task_type: 'chat', provider_id: null, model_name: '', is_default: true,
    })
    await Promise.all([loadChatPref(), loadModelInfo()])
  } catch (e) {
    alert('恢复失败：' + e.message)
  }
}

function applyPreset() {
  const pre = PRESETS[llmForm.preset]
  if (!pre || editingLlmRow.value) return
  if (!llmForm.label) llmForm.label = pre.label
  llmForm.baseUrl = pre.baseUrl
  llmForm.model = pre.model
}

function openAddLlm() {
  llmForm.preset = 'deepseek'
  llmForm.name = ''
  llmForm.label = ''
  llmForm.baseUrl = PRESETS.deepseek.baseUrl
  llmForm.model = PRESETS.deepseek.model
  llmForm.apiKey = ''
  llmFormError.value = ''
  showAddLlm.value = true
}

function editLlm(p) {
  editingLlmRow.value = p
  llmForm.preset = PRESET_LABELS[p.name] ? p.name : 'custom'
  llmForm.name = p.name
  llmForm.label = p.label
  llmForm.baseUrl = p.base_url || ''
  llmForm.model = p.config?.model || ''
  llmForm.apiKey = ''
  llmFormError.value = ''
}

function closeLlmForm() {
  showAddLlm.value = false
  editingLlmRow.value = null
  llmForm.apiKey = ''
  llmFormError.value = ''
}

async function saveLlm() {
  const model = llmForm.model.trim()
  const baseUrl = llmForm.baseUrl.trim()
  if (!model) { llmFormError.value = '模型名必填'; return }
  if (!baseUrl) { llmFormError.value = 'Base URL 必填'; return }
  if (!editingLlmRow.value && llmForm.preset === 'custom' && !llmForm.name.trim()) {
    llmFormError.value = '自定义厂商需要名称'; return
  }
  if (!editingLlmRow.value && !llmForm.apiKey.trim()) {
    llmFormError.value = 'API Key 必填'; return
  }
  savingLlm.value = true
  llmFormError.value = ''
  try {
    if (editingLlmRow.value) {
      const models = [{ name: model, model, enabled: true }]
      await api('/providers/' + editingLlmRow.value.id, 'PATCH', {
        label: llmForm.label.trim(),
        base_url: baseUrl,
        config: { models, model },
        api_key: llmForm.apiKey.trim() || undefined,
      })
    } else {
      const preset = PRESETS[llmForm.preset]
      const name = llmForm.preset === 'custom' ? llmForm.name.trim() : preset.name
      const label = llmForm.label.trim() || preset.label || name
      // 模型配置存成**清单**：与「模型列表」是同一份数据，加完厂商就能在那儿继续加模型。
      const models = [{ name: model, model, enabled: true }]
      // 同名厂商不重复建：用户可以反复「添加厂商」来改配置，而不是攒出两行同名。
      const same = (llmProviders.value || []).find((p) => p.name === name)
      if (same) {
        await api('/providers/' + same.id, 'PATCH', {
          label, base_url: baseUrl,
          config: { models, model },
          api_key: llmForm.apiKey.trim() || undefined,
        })
      } else {
        await api('/providers', 'POST', {
          kind: 'llm', name, label, base_url: baseUrl,
          api_key: llmForm.apiKey.trim(), config: { models, model }, enabled: true,
        })
      }
    }
    closeLlmForm()
    await Promise.all([loadProviders(), loadModelInfo()])
  } catch (e) {
    llmFormError.value = '保存失败：' + e.message
  } finally {
    savingLlm.value = false
  }
}

async function testProvider(p) {
  testingId.value = p.id
  try {
    const res = await api('/providers/' + p.id + '/test', 'POST')
    notify(res.ok ? `连接成功（${res.label || res.provider}）` : `连接失败：${res.error}`, res.ok ? 'ok' : 'err')
  } catch (e) {
    notify('测试失败：' + e.message)
  } finally {
    testingId.value = ''
  }
}

async function deleteLlm(p) {
  if (!confirm('确定删除厂商「' + (p.label || p.name) + '」？它的模型清单会一起没了。')) return
  try {
    await api('/providers/' + p.id, 'DELETE')
    if (chatPref.value?.provider_id === p.id) chatPref.value = null
    if (selectedProviderId.value === p.id) selectedProviderId.value = ''
    await Promise.all([loadProviders(), loadModelInfo()])
  } catch (e) {
    alert('删除失败：' + e.message)
  }
}

// ---- 视频 Provider（MiniMax，用户自带 Key 与模型）----
const videoProviders = ref([])
const showVideoKey = ref(false)
const videoForm = reactive({ apiKey: '', model: 'MiniMax-H3', customModel: '', baseUrl: '' })
const minimaxRow = computed(() => videoProviders.value.find(p => p.name === 'minimax'))
const minimaxModelLabel = computed(() => minimaxRow.value?.config?.model || 'MiniMax-H3')

// ---- 生图模型（与智谱 GLM 共用 key，用户只选模型名）----
const imageInfo = ref({ available: false, provider: '', label: '', model: '', source: 'none' })
const imageForm = reactive({ model: 'cogview-4-250304', customModel: '' })
const savingImage = ref(false)
const imageSourceLabel = computed(() =>
  ({ db: '界面指定', env: '.env 配置', default: '未配置' }[imageInfo.value.source] || imageInfo.value.source))

async function loadImageInfo() {
  try {
    const d = await api('/status')
    imageInfo.value = d.image || { available: false }
    // 回填下拉：当前生效的模型在预设里就选中，否则进自定义
    const current = imageInfo.value.model
    if (current && !IMAGE_MODEL_OPTIONS.some((o) => o.value === current)) {
      imageForm.model = 'custom'
      imageForm.customModel = current
    } else if (current) {
      imageForm.model = current
    }
  } catch { /* 保持占位 */ }
}

async function saveImageModel() {
  const model = imageForm.model === 'custom' ? imageForm.customModel.trim() : imageForm.model
  if (!model) { alert('请选择或填写模型名'); return }
  savingImage.value = true
  try {
    // 生图配置挂在 kind=image 的 cogview 行上（key 缺省复用 .env 的智谱 key）
    let row = (await api('/providers?kind=image')).find((p) => p.name === 'cogview')
    if (row) {
      await api('/providers/' + row.id, 'PATCH', { config: { model } })
    } else {
      row = await api('/providers', 'POST', {
        kind: 'image', name: 'cogview', label: '智谱 CogView',
        api_key: '', config: { model }, enabled: true,
      })
    }
    await api('/models/preferences', 'PUT', {
      task_type: 'image_gen', provider_id: row.id, model_name: model, is_default: true,
    })
    await loadImageInfo()
  } catch (e) {
    alert('保存失败：' + e.message)
  } finally {
    savingImage.value = false
  }
}

async function followEnvImage() {
  try {
    await api('/models/preferences', 'PUT', {
      task_type: 'image_gen', provider_id: null, model_name: '', is_default: true,
    })
    await loadImageInfo()
  } catch (e) {
    alert('恢复失败：' + e.message)
  }
}

// ---- 模型偏好 ----

const taskLabels = { screenplay: '剧本生成', shot_design: '镜头设计', video_gen: '视频生成', image_gen: '图片生成', tts: '语音合成' }
const taskLabel = (t) => taskLabels[t] || t

async function loadProviders() {
  try {
    const all = await api('/providers')
    videoProviders.value = all.filter(p => p.kind === 'video')
    llmProviders.value = all.filter(p => p.kind === 'llm').map(p => ({ ...p, testing: false, testResult: null }))
  } catch {}
  syncVideoForm()
  if (showAddLlm.value && !editingLlmRow.value && !llmForm.label) applyPreset()
}

/** 用已保存的 MiniMax 配置回填表单（Key 永远留空，表示「保持不变」）。 */
function syncVideoForm() {
  videoForm.apiKey = ''
  const current = minimaxRow.value?.config?.model || 'MiniMax-H3'
  if (!VIDEO_MODEL_OPTIONS.some((o) => o.value === current) && !videoForm.customModel) {
    videoForm.model = 'custom'
    videoForm.customModel = current
  } else if (VIDEO_MODEL_OPTIONS.some((o) => o.value === current)) {
    videoForm.model = current
  }
  videoForm.baseUrl = minimaxRow.value?.base_url || ''
}

async function saveMinimax() {
  const key = videoForm.apiKey.trim()
  const model = videoForm.model === 'custom' ? videoForm.customModel.trim() : videoForm.model
  if (!minimaxRow.value && !key) {
    alert('请先填入 MiniMax API Key')
    return
  }
  try {
    const config = model ? { model } : {}
    if (minimaxRow.value) {
      await api(`/providers/${minimaxRow.value.id}`, 'PATCH', {
        api_key: key || undefined,
        base_url: videoForm.baseUrl,
        config,
      })
    } else {
      await api('/providers', 'POST', {
        kind: 'video',
        name: 'minimax',
        label: 'MiniMax 海螺',
        api_key: key,
        base_url: videoForm.baseUrl,
        config,
      })
    }
    await loadProviders()
  } catch (e) {
    alert(`保存失败：${e.message}`)
  }
}

watch(() => props.open, (v) => {
  if (v) {
    loadProviders()
    loadModelInfo()
    loadChatPref()
    loadImageInfo()
  }
})

onMounted(() => {
  if (props.open) {
    loadProviders()
    loadModelInfo()
    loadChatPref()
    loadImageInfo()
  }
})
</script>

<style scoped>
/* 外壳走 style.css 的 .modal-* 系列；这里只留设置页自己的东西。 */
.settings-tabs { margin: 14px 0 16px; align-self: flex-start; }
.modal-body { padding: 0; }
.section h4 { margin: 0 0 14px; font-size: var(--fs-md); color: var(--ink); font-weight: 600; }

.active-line {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 9px 12px; margin-bottom: 14px;
  border: 1px solid var(--line); border-radius: var(--radius-lg);
  background: var(--panel2); color: var(--muted); font-size: var(--fs-sm);
}
.active-line strong { color: var(--ink); }
.active-src {
  margin-left: auto; font-size: var(--fs-xs); color: var(--dim);
}

.llm-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.llm-card {
  border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 12px 14px;
  background: var(--panel); display: flex; flex-direction: column; gap: 7px;
}
.llm-card.active { border-color: color-mix(in oklch, var(--accent) 50%, var(--line)); background: var(--accent-soft); }
.llm-card.add {
  align-items: center; justify-content: center; cursor: pointer;
  border-style: dashed; color: var(--muted); font-size: var(--fs-md); padding: 14px;
  transition: color 150ms var(--ease), border-color 150ms var(--ease), background 150ms var(--ease);
}
.llm-card.add:hover { color: var(--ink); border-color: var(--accent); background: var(--accent-soft); }
.llm-main { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.llm-name { font-size: var(--fs-md); font-weight: 600; color: var(--ink); }
.llm-meta { font-size: var(--fs-xs); color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.using-badge {
  align-self: flex-start; font-size: var(--fs-xs); color: var(--accent);
}
.llm-actions { display: flex; gap: 6px; flex-wrap: wrap; }

/* 两栏：左厂商列表 + 右详情面板 */
.llm-split { display: grid; grid-template-columns: 190px 1fr; gap: 16px; }
.llm-side {
  display: flex; flex-direction: column; gap: 2px;
  padding-right: 12px; border-right: 1px solid var(--line);
  min-height: 320px;
}
.llm-side-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 7px 9px; border-radius: var(--radius-md);
  background: transparent; border: none; color: var(--muted);
  font-size: var(--fs-sm); font-family: inherit; text-align: left; cursor: pointer;
  transition: background 130ms var(--ease), color 130ms var(--ease);
}
.llm-side-item:hover { background: rgba(255, 255, 255, .06); color: var(--ink); }
.llm-side-item.on { background: rgba(255, 255, 255, .1); color: var(--ink); }
.side-dot {
  width: 7px; height: 7px; border-radius: 50%; flex: none;
  background: var(--dim);
}
.side-dot.on { background: var(--ok); }
.side-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.side-count { font-size: 10px; color: var(--dim); flex: none; font-variant-numeric: tabular-nums; }
.llm-side-add {
  margin-top: auto; padding: 6px 9px; border-style: dashed; color: var(--muted);
  text-align: left;
}
.llm-detail { min-width: 0; }
.detail-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.dh-name { font-size: var(--fs-lg); font-weight: 600; color: var(--ink); }
.dh-actions { margin-left: auto; display: flex; gap: 6px; }
.format-fixed {
  padding: 8px 12px; border: 1px solid var(--line); border-radius: var(--radius-md);
  background: rgba(255, 255, 255, .03); color: var(--muted); font-size: var(--fs-sm);
}
.detail-sub {
  display: flex; align-items: center; gap: 10px;
  margin: 18px 0 8px; padding-top: 14px; border-top: 1px solid var(--line);
}
.ds-title { font-size: var(--fs-sm); color: var(--muted); font-weight: 600; }
.detail-sub .add-model { margin-left: auto; }

/* 模型行的开关 */
.mr-toggle {
  width: 30px; height: 17px; flex: none; padding: 0;
  border-radius: var(--radius-full); border: 1px solid var(--line);
  background: rgba(255, 255, 255, .06); cursor: pointer; position: relative;
  transition: background 140ms var(--ease), border-color 140ms var(--ease);
}
.mr-toggle span {
  position: absolute; top: 1px; left: 1px;
  width: 13px; height: 13px; border-radius: 50%;
  background: var(--dim);
  transition: transform 140ms var(--ease), background 140ms var(--ease);
}
.mr-toggle.on { background: var(--accent-soft); border-color: color-mix(in oklch, var(--accent) 45%, transparent); }
.mr-toggle.on span { transform: translateX(13px); background: var(--accent); }

/* 模型清单：厂商下列出的模型，可逐个启用 / 改 / 删 */
.model-list { display: flex; flex-direction: column; gap: 4px; margin: 2px 0 4px; }
.model-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 9px; border: 1px solid var(--line); border-radius: var(--radius-md);
  background: rgba(0, 0, 0, .2);
}
.mr-name { font-size: var(--fs-sm); color: var(--ink); flex: none; }
.mr-id {
  font-family: var(--mono); font-size: 10px; color: var(--dim);
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mr-btn {
  flex: none; padding: 1px 8px; font-size: 11px; border-radius: var(--radius-sm);
  background: transparent; border: 1px solid var(--line); color: var(--muted);
  cursor: pointer;
}
.mr-btn:hover { color: var(--ink); border-color: var(--line-strong); }
.mr-btn.on { color: var(--accent); border-color: color-mix(in oklch, var(--accent) 40%, transparent); }
.mr-btn.ok { color: var(--accent); border-color: color-mix(in oklch, var(--accent) 40%, transparent); }
.mr-btn.danger { color: var(--bad); border-color: color-mix(in oklch, var(--bad) 35%, transparent); }
.add-model {
  align-self: flex-start; padding: 3px 9px; font-size: 11.5px;
  border-style: dashed; color: var(--muted);
}
.follow-env {
  width: 100%; padding: 8px; border: 1px dashed var(--line-strong); border-radius: var(--radius-md);
  background: transparent; color: var(--muted); font-size: var(--fs-sm); cursor: pointer;
  transition: color 150ms var(--ease), border-color 150ms var(--ease);
}
.follow-env:hover { color: var(--ink); border-color: var(--line-strong); }
.pref-hint { font-size: var(--fs-sm); color: var(--muted); line-height: 1.65; }
.sub-modal h5 { margin: 0 0 12px; font-size: var(--fs-md); color: var(--ink); font-weight: 600; }
.form-row { margin-bottom: 12px; }
.form-row label { display: block; font-size: var(--fs-sm); color: var(--muted); margin-bottom: 5px; }
.key-row { display: flex; gap: 8px; }
.key-row .modal-input { flex: 1; }
.test-result { margin-top: 8px; padding: 8px 12px; border-radius: var(--radius-md); font-size: var(--fs-sm); }
.test-result.ok { background: rgba(74, 222, 128, .1); color: var(--ok); }
.test-result.err { background: color-mix(in oklch, var(--bad) 12%, transparent); color: var(--bad); }

.status-dot { width: 8px; height: 8px; border-radius: 50%; }
.status-dot.on { background: var(--ok); }
.status-dot.off { background: var(--dim); }

.sub-modal {
  margin-top: 16px; padding: 16px; background: var(--panel);
  border: 1px solid var(--line); border-radius: var(--radius-lg);
}
.form-actions { display: flex; gap: 8px; margin-top: 14px; }
</style>
