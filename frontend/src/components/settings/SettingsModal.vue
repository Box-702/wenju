<template>
  <Teleport to="body">
    <div v-if="open" class="modal-backdrop" @click.self="$emit('close')">
      <div class="modal-card wide settings-card">
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
          <!-- 对话模型：内置厂商目录 + 一步流（选厂商 → 贴 key → 拉模型 → 勾选保存）。
               左列是「目录 ∪ 已配置」的合并视图：内置厂商没配过显示「待配置」，
               配过的显示真实厂商行（带模型数）。D-42。 -->
          <div v-if="tab === 'llm'" class="section llm-split">
            <aside class="llm-side">
              <button
                v-for="it in leftItems"
                :key="it.key"
                :class="['llm-side-item', { on: itemSelected(it) }]"
                type="button"
                @click="selectItem(it)"
              >
                <span class="side-dot" :class="{ on: it.configured }"></span>
                <span class="side-name">{{ it.label }}</span>
                <span v-if="it.kind === 'row'" class="side-count">{{ it.count }}</span>
                <span v-else class="side-tag">待配置</span>
              </button>
              <button class="llm-side-add" type="button" @click="openCustomForm()">+ 自定义供应商</button>
            </aside>

            <!-- A. 内置厂商（未配置）：一步流 -->
            <div v-if="selSpec" class="llm-detail qc-panel">
              <div class="detail-head">
                <span class="dh-name">{{ selSpec.label }}</span>
                <span class="dh-actions">
                  <a v-if="selSpec.docs" class="small ghost" :href="selSpec.docs" target="_blank" rel="noopener">去拿 Key</a>
                </span>
              </div>
              <p class="pref-hint">贴 API Key → 点「拉取模型」，厂商返回什么就勾什么；拉不到时用内置目录兜底。</p>
              <div class="form-row">
                <label>Base URL</label>
                <input v-model.trim="qc.baseUrl" class="modal-input" placeholder="https://…" />
              </div>
              <div class="form-row">
                <label>API Key</label>
                <div class="key-row">
                  <input
                    :type="qc.showKey ? 'text' : 'password'"
                    v-model="qc.apiKey"
                    class="modal-input"
                    placeholder="sk-…"
                    @keyup.enter="qcProbe"
                  />
                  <button class="small ghost" @click="qc.showKey = !qc.showKey">{{ qc.showKey ? '隐藏' : '显示' }}</button>
                </div>
              </div>
              <div class="form-actions">
                <button class="primary" type="button" :disabled="!qc.apiKey || qc.probing" @click="qcProbe">
                  {{ qc.probing ? '查询厂商中…' : '拉取模型' }}
                </button>
              </div>
              <p v-if="qc.error" class="test-result err">{{ qc.error }}</p>
              <p v-if="qc.note" class="test-result">{{ qc.note }}</p>

              <template v-if="qc.models.length">
                <div class="detail-sub"><span class="ds-title">勾选要收进来的模型（{{ qc.picked.length }} / {{ qc.models.length }}）</span></div>
                <div class="disc-list">
                  <label v-for="m in qc.models" :key="m" class="disc-row">
                    <input type="checkbox" :value="m" v-model="qc.picked" />
                    <span class="dr-model">{{ m }}</span>
                  </label>
                </div>
                <label class="qc-default-row">
                  <input type="checkbox" v-model="qc.setAsDefault" /> 保存后设为默认对话模型
                </label>
                <div class="form-actions">
                  <button class="primary" type="button" :disabled="!qc.picked.length || qc.saving" @click="qcSave">
                    {{ qc.saving ? '保存中…' : `保存这 ${qc.picked.length} 个模型` }}
                  </button>
                </div>
              </template>
            </div>

            <!-- B. 已配置厂商：设置 + 模型清单 -->
            <div v-else class="llm-detail">
              <template v-if="selectedProvider">
                <div class="detail-head">
                  <span class="dh-name">{{ selectedProvider.label || selectedProvider.name }}</span>
                  <span v-if="chatPref?.provider_id === selectedProvider.id" class="using-badge">使用中</span>
                  <span class="dh-actions">
                    <button
                      v-if="chatPref?.provider_id !== selectedProvider.id"
                      class="small ok"
                      :disabled="!selectedProvider.configured"
                      @click="setAsDefault(selectedProvider)"
                    >设为默认</button>
                    <button class="small ghost" :disabled="testingId === selectedProvider.id" @click="testProvider(selectedProvider)">
                      {{ testingId === selectedProvider.id ? '测试中…' : '测试连接' }}
                    </button>
                    <button class="small ghost" :disabled="!selectedProvider.configured || discovering" @click="openDiscover(selectedProvider)">
                      {{ discovering ? '查询中…' : '发现模型' }}
                    </button>
                    <button class="small danger" @click="deleteLlm(selectedProvider)">删除厂商</button>
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
                    <button class="small ghost" @click="showProviderKey = !showProviderKey">{{ showProviderKey ? '隐藏' : '显示' }}</button>
                  </div>
                </div>
                <div class="form-actions">
                  <button class="primary" :disabled="savingProvider" @click="saveProvider">
                    {{ savingProvider ? '保存中…' : '保存厂商设置' }}
                  </button>
                </div>

                <div class="detail-sub">
                  <span class="ds-title">模型列表</span>
                  <button class="small ghost dashed" type="button" @click="openAddModel(selectedProvider)">+ 添加模型</button>
                </div>
                <div class="model-list">
                  <div v-for="(m, mi) in providerModels(selectedProvider)" :key="m.model + mi" class="model-row">
                    <span class="mr-name">{{ m.name }}</span>
                    <span class="mr-id">{{ m.model }}</span>
                    <button
                      class="icon-btn"
                      title="改这个模型（显示名 / 模型名）"
                      aria-label="编辑模型"
                      @click="editModel(selectedProvider, mi)"
                    ><AppIcon name="edit" /></button>
                    <button
                      class="icon-btn danger"
                      title="从清单里删掉这个模型"
                      aria-label="删除模型"
                      @click="removeModel(selectedProvider, mi)"
                    ><AppIcon name="trash" /></button>
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

              <!-- 自定义供应商：没有内置目录，贴 key 后靠厂商 /models 发现 -->
              <div v-if="showCustomForm" class="sub-modal llm-form">
                <h5>自定义供应商</h5>
                <p class="pref-hint">OpenAI 兼容协议（/v1/chat/completions）的服务都能接：中转站、自建网关、本地推理服务。</p>
                <div class="form-row">
                  <label>名称（唯一标识）</label>
                  <input v-model.trim="llmForm.name" class="modal-input" placeholder="my-provider" />
                </div>
                <div class="form-row">
                  <label>显示名</label>
                  <input v-model.trim="llmForm.label" class="modal-input" placeholder="我的中转站" />
                </div>
                <div class="form-row">
                  <label>Base URL</label>
                  <input v-model.trim="llmForm.baseUrl" class="modal-input" placeholder="https://你的网关/v1" />
                </div>
                <div class="form-row">
                  <label>API Key</label>
                  <div class="key-row">
                    <input
                      :type="showKey ? 'text' : 'password'"
                      v-model="llmForm.apiKey"
                      class="modal-input"
                      placeholder="sk-…"
                    />
                    <button class="small ghost" @click="showKey = !showKey">{{ showKey ? '隐藏' : '显示' }}</button>
                  </div>
                </div>
                <div v-if="llmFormError" class="test-result err">{{ llmFormError }}</div>
                <div class="form-actions">
                  <button class="primary" @click="saveCustomProvider" :disabled="savingLlm">
                    {{ savingLlm ? '保存中…' : '保存并进入模型勾选' }}
                  </button>
                  <button class="ghost" @click="closeLlmForm">取消</button>
                </div>
              </div>

              <!-- 发现模型：拿这把 key 去厂商问一遍，勾哪些用哪些（BYOK） -->
              <div v-if="discover.open" class="sub-modal discover">
                <h5>发现模型 · {{ discover.provider?.label || discover.provider?.name }}</h5>
                <p v-if="discover.error" class="test-result err">{{ discover.error }}</p>
                <div v-else-if="!discover.models.length" class="pop-empty">厂商没有返回模型清单。</div>
                <div v-else class="disc-list">
                  <label v-for="m in discover.models" :key="m" class="disc-row">
                    <input type="checkbox" :value="m" v-model="discover.picked" :disabled="discover.existing.has(m)" />
                    <span class="dr-model">{{ m }}</span>
                    <span v-if="discover.existing.has(m)" class="dr-have">已收录</span>
                  </label>
                </div>
                <div class="form-actions">
                  <button class="primary" :disabled="!discover.picked.length" @click="applyDiscover">
                    添加勾选的 {{ discover.picked.length }} 个
                  </button>
                  <button class="ghost" @click="discover.open = false">取消</button>
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

          <!-- 生图模型：与对话同款「左厂商列表 + 右模型清单」（2026-11-13 用户指正，
               旧版是单下拉框——厂商/模型混在一起，和对话体验两套语言） -->
          <div v-if="tab === 'image'" class="section llm-split">
            <aside class="llm-side">
              <button
                v-for="p in imageRows"
                :key="p.id"
                :class="['llm-side-item', { on: imgSelId === p.id }]"
                type="button"
                @click="imgSelId = p.id"
              >
                <span class="side-dot" :class="{ on: p.configured }"></span>
                <span class="side-name">{{ p.label || p.name }}</span>
                <span class="side-count">{{ providerModels(p).length }}</span>
              </button>
              <button
                :class="['llm-side-item', { on: imgSelId === '__openai' }]"
                type="button" @click="imgSelId = '__openai'"
              >
                <span class="side-dot"></span>
                <span class="side-name">OpenAI 兼容生图</span>
                <span class="side-tag">待配置</span>
              </button>
            </aside>

            <!-- 已配置厂商：key/URL + 模型清单（点模型=设默认） -->
            <div v-if="selImageRow" class="llm-detail">
              <div class="detail-head">
                <span class="dh-name">{{ selImageRow.label || selImageRow.name }}</span>
                <span v-if="imageActiveRowId === selImageRow.id" class="using-badge">使用中</span>
                <span class="dh-actions">
                  <button class="small ghost" :disabled="!selImageRow.configured" @click="testProvider(selImageRow)">
                    {{ testingId === selImageRow.id ? '测试中…' : '测试连接' }}
                  </button>
                </span>
              </div>
              <p class="pref-hint">
                点模型名直接切换在用的生图模型；智谱 CogView 与 GLM 对话共用同一个 Key。
              </p>
              <div class="form-row">
                <label>API Key</label>
                <div class="key-row">
                  <input
                    :type="showImageKey ? 'text' : 'password'"
                    v-model.trim="imageDraft.api_key"
                    class="modal-input"
                    :placeholder="selImageRow.configured ? '已配置（留空 = 保持不变）' : 'sk-…'"
                  />
                  <button class="small ghost" @click="showImageKey = !showImageKey">{{ showImageKey ? '隐藏' : '显示' }}</button>
                </div>
              </div>
              <div class="form-actions">
                <button class="primary" :disabled="savingImage" @click="saveImageProvider">
                  {{ savingImage ? '保存中…' : '保存厂商设置' }}
                </button>
              </div>

              <div class="detail-sub">
                <span class="ds-title">模型列表</span>
                <button class="small ghost" type="button" @click="openAddModel(selImageRow)">+ 添加模型</button>
              </div>
              <div class="model-list">
                <div
                  v-for="(m, mi) in providerModels(selImageRow)" :key="m.model + mi"
                  :class="['model-row', { active: imageActiveRowId === selImageRow.id && imageActiveModel(selImageRow) === m.model }]"
                >
                  <span class="mr-name">{{ m.name }}</span>
                  <span class="mr-id">{{ m.model }}</span>
                  <button
                    v-if="!(imageActiveRowId === selImageRow.id && imageActiveModel(selImageRow) === m.model)"
                    class="small ok" :disabled="!m.enabled"
                    @click="pickImageModel(selImageRow, m)"
                  >设为默认</button>
                  <span v-else class="mono ok">在用</span>
                  <button class="icon-btn" title="改这个模型" aria-label="编辑模型" @click="editModel(selImageRow, mi)">
                    <AppIcon name="edit" />
                  </button>
                  <button class="icon-btn danger" title="删掉这个模型" aria-label="删除模型" @click="removeModel(selImageRow, mi)">
                    <AppIcon name="trash" />
                  </button>
                </div>
                <div v-if="!providerModels(selImageRow).length" class="pop-empty">
                  这家还没有模型清单，加一个。
                </div>
              </div>
            </div>

            <!-- 待配置：OpenAI 兼容生图（gpt-image-1） -->
            <div v-else-if="imgSelId === '__openai'" class="llm-detail qc-panel">
              <div class="detail-head"><span class="dh-name">OpenAI 兼容生图</span></div>
              <p class="pref-hint">贴 API Key 保存即建厂商行，模型默认 gpt-image-1。</p>
              <div class="form-row">
                <label>Base URL</label>
                <input v-model.trim="openaiImage.baseUrl" class="modal-input" placeholder="https://api.openai.com/v1" />
              </div>
              <div class="form-row">
                <label>API Key</label>
                <input :type="showImageKey ? 'text' : 'password'" v-model="openaiImage.apiKey" class="modal-input" placeholder="sk-…" />
              </div>
              <div class="form-actions">
                <button class="primary" :disabled="!openaiImage.apiKey || savingImage" @click="createOpenaiImage">保存并启用</button>
              </div>
            </div>

            <div v-else class="llm-detail"><div class="pop-empty">左边选一个厂商。</div></div>
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
// 生图不再有静态下拉：厂商列表 + 模型清单同构对话模型（2026-11-13），
// 旧 IMAGE_MODEL_OPTIONS 死常量已删。
// H3 系列档位（官方定价页 2026-11-12）：本地调试默认 Lite，质量评审可切 H3。
// 旧 v1 模型（Hailuo-02/T2V-01/I2V-01）已从 V2 接口下线，不再列。
const VIDEO_MODEL_OPTIONS = [
  { value: 'MiniMax-H3-Lite', label: 'MiniMax-H3-Lite（默认，¥0.16-0.26/s，批量试错）' },
  { value: 'MiniMax-H3-Highspeed', label: 'MiniMax-H3-Highspeed（¥0.26-0.40/s，质量速度均衡）' },
  { value: 'MiniMax-H3', label: 'MiniMax-H3（¥0.50-0.80/s，画质优先·成片评审用）' },
]

// ---- 对话模型：内置目录 + 一步流（D-42）----
const showKey = ref(false)
const llmProviders = ref([])
const specs = ref([])          // GET /providers/specs：内置厂商目录

// 右侧详情面板：选中的厂商行 + 待保存的字段
const selectedProviderId = ref('')
const selectedSpecName = ref('')
const providerDraft = reactive({ base_url: '', api_key: '' })
const showProviderKey = ref(false)
const savingProvider = ref(false)
const testingId = ref('')

const selectedProvider = computed(
  () => (llmProviders.value || []).find((p) => p.id === selectedProviderId.value) || null,
)

/** specName → 已配置的 llm 行（同名的内置厂商只认第一行）。 */
function rowForSpec(name) {
  return (llmProviders.value || []).find((p) => p.name === name) || null
}

/** 左列 = 内置目录 ∪ 已配置行：目录按推荐顺序，自定义行（不在目录里的）追加在后。 */
const leftItems = computed(() => {
  const items = []
  const seen = new Set()
  for (const sp of specs.value) {
    seen.add(sp.name)
    const row = rowForSpec(sp.name)
    if (row) {
      items.push({ key: 'row:' + row.id, kind: 'row', id: row.id, label: row.label || sp.label,
        configured: row.configured, count: providerModels(row).length })
    } else {
      items.push({ key: 'spec:' + sp.name, kind: 'spec', spec: sp, label: sp.label, configured: false })
    }
  }
  for (const p of llmProviders.value) {
    if (!seen.has(p.name)) {
      items.push({ key: 'row:' + p.id, kind: 'row', id: p.id, label: p.label || p.name,
        configured: p.configured, count: providerModels(p).length })
    }
  }
  return items
})

/** 当前右侧渲染的是不是「内置厂商快速配置」面板。 */
const selSpec = computed(() => {
  if (!selectedSpecName.value) return null
  return specs.value.find((s) => s.name === selectedSpecName.value) || null
})

function itemSelected(it) {
  return it.kind === 'row'
    ? !selectedSpecName.value && it.id === selectedProviderId.value
    : it.spec.name === selectedSpecName.value
}

function selectItem(it) {
  showCustomForm.value = false
  if (it.kind === 'spec') {
    selectedSpecName.value = it.spec.name
    selectedProviderId.value = ''
    resetQc(it.spec)
  } else {
    selectedSpecName.value = ''
    selectedProviderId.value = it.id
  }
}

// 快速配置（内置未配置厂商）：贴 key → probe → 勾选 → 保存。
const qc = reactive({
  baseUrl: '', apiKey: '', showKey: false,
  probing: false, models: [], picked: [],
  error: '', note: '', saving: false, setAsDefault: true,
})

function resetQc(spec) {
  qc.baseUrl = spec.base_url || ''
  qc.apiKey = ''
  qc.showKey = false
  qc.probing = false
  qc.models = []
  qc.picked = []
  qc.error = ''
  qc.note = ''
  qc.saving = false
  qc.setAsDefault = true
}

async function qcProbe() {
  const spec = selSpec.value
  if (!spec || !qc.apiKey.trim()) return
  qc.probing = true
  qc.error = ''
  qc.note = ''
  qc.models = []
  qc.picked = []
  try {
    const res = await api('/providers/probe', 'POST', {
      base_url: qc.baseUrl.trim(), api_key: qc.apiKey.trim(),
    })
    if (res.ok && (res.models || []).length) {
      qc.models = res.models
      qc.picked = res.models.slice(0, 5)   // 默认勾前 5 个（厂商按新→旧排），少点几下
    } else if (spec.chat_models && spec.chat_models.length) {
      // 厂商不支持列表 / key 被拒但目录在：用内置清单兜底，如实说明。
      qc.models = spec.chat_models
      qc.picked = spec.chat_models.slice(0, 2)
      qc.note = res.ok
        ? '厂商不支持模型列表，下面用内置目录，勾要收的即可。'
        : `厂商没连通（${res.error || '未知'}）。若确认 key 无误，可先勾内置目录保存，之后用「测试连接」复查。`
    } else {
      qc.error = res.error || '厂商没有返回模型清单'
    }
  } catch (e) {
    qc.error = '查询失败：' + (e.message || e)
  } finally {
    qc.probing = false
  }
}

async function qcSave() {
  const spec = selSpec.value
  if (!spec || !qc.picked.length) return
  qc.saving = true
  try {
    const models = qc.picked.map((m) => ({ name: m, model: m, enabled: true }))
    const config = { models, model: models[0].model }
    const existing = rowForSpec(spec.name)
    let rowId
    if (existing) {
      await api('/providers/' + existing.id, 'PATCH', {
        base_url: qc.baseUrl.trim(), api_key: qc.apiKey.trim(), config,
      })
      rowId = existing.id
    } else {
      const created = await api('/providers', 'POST', {
        kind: 'llm', name: spec.name, label: spec.label,
        base_url: qc.baseUrl.trim(), api_key: qc.apiKey.trim(),
        config, enabled: true,
      })
      rowId = created.id
    }
    if (qc.setAsDefault) {
      await api('/models/preferences', 'PUT', {
        task_type: 'chat', provider_id: rowId, model_name: models[0].model, is_default: true,
      })
    }
    selectedSpecName.value = ''
    selectedProviderId.value = rowId
    await Promise.all([loadProviders(), loadModelInfo(), loadChatPref()])
    notify(`已接入 ${spec.label} · ${models.length} 个模型`, 'ok')
  } catch (e) {
    qc.error = '保存失败：' + (e.message || e)
  } finally {
    qc.saving = false
  }
}

/** 选中厂商变化 → 把它的 Base URL 载进草稿（key 不回显，只提示「已配置」） */
watch(selectedProvider, (p) => {
  providerDraft.base_url = p?.base_url || ''
  providerDraft.api_key = ''
  showProviderKey.value = false
})

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

// ---- 发现模型：拿 key 调厂商 /models，勾选入清单（后端 testProvider 已返回全量） ----
const discovering = ref(false)
const discover = reactive({ open: false, provider: null, models: [], picked: [], existing: new Set(), error: '' })

async function openDiscover(p) {
  discover.open = true
  discover.provider = p
  discover.models = []
  discover.picked = []
  discover.error = ''
  discover.existing = new Set(providerModels(p).map((m) => m.model))
  discovering.value = true
  try {
    const res = await api('/providers/' + p.id + '/test', 'POST')
    if (!res.ok) {
      discover.error = res.error || res.detail || '连不上厂商'
      return
    }
    const names = res.models || []
    if (!names.length) discover.error = '这家厂商没有返回模型清单（可能不支持列表接口）'
    discover.models = names
    // 默认勾上厂商清单里、我们还没收录的第一批——少点几下
    discover.picked = names.filter((n) => !discover.existing.has(n)).slice(0, 5)
  } catch (e) {
    discover.error = '查询失败：' + (e.message || e)
  } finally {
    discovering.value = false
  }
}

async function applyDiscover() {
  const p = discover.provider
  if (!p || !discover.picked.length) return
  const models = providerModels(p).slice()
  for (const name of discover.picked) {
    if (!models.some((m) => m.model === name)) models.push({ name, model: name, enabled: true })
  }
  discover.open = false
  await saveModels(p, models)
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

// 自定义供应商表单（D-42：没有内置目录，贴 key 后靠厂商 /models 发现）
const showCustomForm = ref(false)
const llmForm = reactive({ name: '', label: '', baseUrl: '', apiKey: '' })
const llmFormError = ref('')
const savingLlm = ref(false)

const chatPrefLabel = computed(() => {
  if (!chatPref.value) return '跟随 .env 默认'
  const p = llmProviders.value.find(x => x.id === chatPref.value.provider_id)
  const model = chatPref.value.model_name || p?.config?.model || ''
  return (p?.label || chatPref.value.provider_id) + (model ? ' · ' + model : '')
})

async function loadModelInfo() {
  try {
    const d = await api('/status')
    activeModel.value = d.model || { provider: '-', name: '' }
    activeSource.value = d.model?.source || 'env'
  } catch { /* 保持占位 */ }
}

/** 内置厂商目录：进设置页拉一次，左列据此渲染「待配置」项。 */
async function loadSpecs() {
  try { specs.value = await api('/providers/specs') } catch { specs.value = [] }
}

/** 列表/目录变化后维持选中：还在就不动；不在就落到「使用中」的行，
    一个行都没有时选目录第一项（新装用户第一眼就是「给 DeepSeek 贴 key」，不用先点一下）。
    必须放在 chatPref 声明之后：immediate 的回调在 setup 期同步执行，
    提前引用未声明的 const 会 TDZ 报错。 */
watch([llmProviders, specs], () => {
  const items = leftItems.value
  if (!items.length) return
  const still = items.find((it) => itemSelected(it))
  if (still) return
  const prefRow = items.find(
    (it) => it.kind === 'row' && chatPref.value?.provider_id === it.id,
  )
  const firstRow = items.find((it) => it.kind === 'row')
  const pick = prefRow || firstRow || items[0]
  selectItem(pick)
}, { immediate: true })

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

function openCustomForm() {
  llmForm.name = ''
  llmForm.label = ''
  llmForm.baseUrl = ''
  llmForm.apiKey = ''
  llmFormError.value = ''
  showCustomForm.value = true
  // 自定义走「行」视图之外的独立面板；清掉 spec 选中，右侧腾出表单空间。
  selectedSpecName.value = ''
  selectedProviderId.value = ''
}

function closeLlmForm() {
  showCustomForm.value = false
  llmForm.apiKey = ''
  llmFormError.value = ''
}

/** 只建厂商行（不预设模型）：保存后自动选中它，右侧引导用户「发现模型」勾选。 */
async function saveCustomProvider() {
  const name = llmForm.name.trim()
  const baseUrl = llmForm.baseUrl.trim()
  if (!name) { llmFormError.value = '名称必填（唯一标识，如 my-gateway）'; return }
  if (!baseUrl) { llmFormError.value = 'Base URL 必填'; return }
  if (!llmForm.apiKey.trim()) { llmFormError.value = 'API Key 必填'; return }
  savingLlm.value = true
  llmFormError.value = ''
  try {
    const label = llmForm.label.trim() || name
    const same = rowForSpec(name)
    let rowId
    if (same) {
      // 同名不重复建：反复「自定义供应商」来改配置，而不是攒两行同名。
      await api('/providers/' + same.id, 'PATCH', {
        label, base_url: baseUrl, api_key: llmForm.apiKey.trim(),
      })
      rowId = same.id
    } else {
      const created = await api('/providers', 'POST', {
        kind: 'llm', name, label, base_url: baseUrl,
        api_key: llmForm.apiKey.trim(), config: {}, enabled: true,
      })
      rowId = created.id
    }
    closeLlmForm()
    await Promise.all([loadProviders(), loadModelInfo()])
    selectedProviderId.value = rowId
    notify('厂商已保存，点「发现模型」拉清单', 'ok')
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
const videoForm = reactive({ apiKey: '', model: 'MiniMax-H3-Lite', customModel: '', baseUrl: '' })
const minimaxRow = computed(() => videoProviders.value.find(p => p.name === 'minimax'))
const minimaxModelLabel = computed(() => minimaxRow.value?.config?.model || 'MiniMax-H3-Lite')

// ---- 生图模型：厂商列表 + 模型清单（2026-11-13 与对话模型同构，用户指正）----
const imageInfo = ref({ available: false, provider: '', label: '', model: '', source: 'none' })
const imageProviders = ref([])
const imagePref = ref(null)      // task_type=image_gen 的偏好（provider_id + model_name）
const imgSelId = ref('')
const savingImage = ref(false)
const showImageKey = ref(false)
const imageDraft = reactive({ api_key: '' })
const openaiImage = reactive({ baseUrl: '', apiKey: '' })

/** 厂商列 = kind=image 的行；「在用」判定 = 偏好指的那家，没偏好时后端
    按优先级链自动选（cogview > openai），展示上以偏好为主、行清单为辅。 */
const imageRows = computed(() => imageProviders.value)
const selImageRow = computed(() => imageRows.value.find(p => p.id === imgSelId.value) || null)

/** 某厂商行当前生效的模型名。判定与后端 registry 同口径：
    偏好指定 > 后端 /status 报的实际在用厂商 > 厂商默认。 */
function imageActiveModel(p) {
  if (!p) return ''
  if (imagePref.value?.provider_id === p.id && imagePref.value.model_name) {
    return imagePref.value.model_name
  }
  if (!imagePref.value && imageInfo.value.provider === p.name) {
    return imageInfo.value.model || p.config?.model || ''
  }
  return p.config?.model || ''
}
const imageActiveRowId = computed(() =>
  imagePref.value?.provider_id
  || imageRows.value.find(p => p.name === imageInfo.value.provider)?.id
  || imageRows.value[0]?.id || '')

async function loadImagePrefs() {
  try {
    const prefs = await api('/models/preferences')
    imagePref.value = prefs.find(x => x.task_type === 'image_gen' && x.provider_id) || null
  } catch { imagePref.value = null }
}

// 列表变化后维持选中：先落在「使用中」的厂商，再落第一行
watch(imageRows, (rows) => {
  if (!rows.length) { imgSelId.value = ''; return }
  if (rows.some(p => p.id === imgSelId.value)) return
  const prefRow = rows.find(p => p.id === imagePref.value?.provider_id)
  imgSelId.value = (prefRow || rows[0]).id
})

/** 点模型名 = 设默认：写 image_gen 偏好（provider_id + model_name），热生效。 */
async function pickImageModel(p, m) {
  savingImage.value = true
  try {
    await api('/models/preferences', 'PUT', {
      task_type: 'image_gen', provider_id: p.id, model_name: m.model, is_default: true,
    })
    // 同厂商切模型也要把 config.model 对齐（偏好缺失时的兜底口径）
    await api('/providers/' + p.id, 'PATCH', { config: { ...(p.config || {}), model: m.model } })
    await Promise.all([loadImagePrefs(), loadProviders(), loadImageInfo()])
    notify(`生图模型已切到 ${m.name || m.model}`, 'ok')
  } catch (e) {
    notify('切换失败：' + (e.message || e))
  } finally {
    savingImage.value = false
  }
}

async function saveImageProvider() {
  const p = selImageRow.value
  if (!p) return
  savingImage.value = true
  try {
    const patch = {}
    if (imageDraft.api_key) patch.api_key = imageDraft.api_key
    if (Object.keys(patch).length) await api('/providers/' + p.id, 'PATCH', patch)
    imageDraft.api_key = ''
    await Promise.all([loadProviders(), loadImageInfo()])
    notify('厂商设置已保存', 'ok')
  } catch (e) {
    notify('保存失败：' + e.message)
  } finally {
    savingImage.value = false
  }
}

async function createOpenaiImage() {
  if (!openaiImage.apiKey) { notify('API Key 必填'); return }
  savingImage.value = true
  try {
    await api('/providers', 'POST', {
      kind: 'image', name: 'openai', label: 'OpenAI 生图',
      base_url: openaiImage.baseUrl, api_key: openaiImage.apiKey,
      config: { model: 'gpt-image-1' }, enabled: true,
    })
    openaiImage.apiKey = ''
    await Promise.all([loadProviders(), loadImageInfo()])
    notify('OpenAI 生图已添加', 'ok')
  } catch (e) {
    notify('添加失败：' + (e.message || e))
  } finally {
    savingImage.value = false
  }
}

async function loadImageInfo() {
  try {
    const d = await api('/status')
    imageInfo.value = d.image || { available: false }
  } catch { /* 保持占位 */ }
}

// ---- 模型偏好 ----

const taskLabels = { screenplay: '剧本生成', shot_design: '镜头设计', video_gen: '视频生成', image_gen: '图片生成', tts: '语音合成' }
const taskLabel = (t) => taskLabels[t] || t

async function loadProviders() {
  try {
    const all = await api('/providers')
    videoProviders.value = all.filter(p => p.kind === 'video')
    imageProviders.value = all.filter(p => p.kind === 'image')
    llmProviders.value = all.filter(p => p.kind === 'llm').map(p => ({ ...p, testing: false, testResult: null }))
  } catch {}
  syncVideoForm()
}

/** 用已保存的 MiniMax 配置回填表单（Key 永远留空，表示「保持不变」）。 */
function syncVideoForm() {
  videoForm.apiKey = ''
  const current = minimaxRow.value?.config?.model || 'MiniMax-H3-Lite'
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
    loadSpecs()
    loadProviders()
    loadModelInfo()
    loadChatPref()
    loadImagePrefs()
    loadImageInfo()
  }
})

onMounted(() => {
  if (props.open) {
    loadSpecs()
    loadProviders()
    loadModelInfo()
    loadChatPref()
    loadImagePrefs()
    loadImageInfo()
  }
})
</script>

<style scoped>
/* 外壳走 style.css 的 .modal-* 系列；这里只留设置页自己的东西。 */
.settings-tabs { margin: 14px 0 16px; align-self: flex-start; }
.modal-body { padding: 0; }

/* 弹窗高度固定不随内容跳动：标题 + 页签钉住，body 吃满剩余高度自己滚。
   外壳 .modal-card 的 max-height 只限上限，内容少时卡片会缩，
   切页签 / 切厂商高度就跳；这里给固定高度，多余内容进滚动区。 */
.settings-card {
  height: min(720px, 88vh);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.settings-card .modal-body {
  flex: 1; min-height: 0;
  overflow-y: auto;
}
/* 两栏各自滚：左厂商列表再长也不撑高弹窗，右详情独立滚动 */
.settings-card .llm-split { min-height: 0; height: 100%; }
.settings-card .llm-side {
  min-height: 0; overflow-y: auto;
  /* 加按钮钉在列表底部，列表本身滚 */
}
.settings-card .llm-detail {
  min-height: 0; overflow-y: auto;
  padding-right: 4px; /* 给滚动条留位，不压内容 */
}
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
.llm-card.active { border-color: var(--line-strong); background: rgba(255, 255, 255, .07); }
.llm-card.add {
  align-items: center; justify-content: center; cursor: pointer;
  border-style: dashed; color: var(--muted); font-size: var(--fs-md); padding: 14px;
  transition: color 150ms var(--ease), border-color 150ms var(--ease), background 150ms var(--ease);
}
.llm-card.add:hover { color: var(--ink); border-color: var(--line-strong); background: rgba(255, 255, 255, .07); }
.llm-main { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.llm-name { font-size: var(--fs-md); font-weight: 600; color: var(--ink); }
.llm-meta { font-size: var(--fs-xs); color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.using-badge {
  align-self: flex-start; font-size: var(--fs-xs); color: var(--ink);
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
.side-dot.on { background: #fff; }
.side-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.side-count { font-size: 10px; color: var(--dim); flex: none; font-variant-numeric: tabular-nums; }
.side-tag { font-size: 10px; color: var(--dim); flex: none; }
.llm-side-add {
  margin-top: auto; padding: 6px 9px; border-style: dashed; color: var(--muted);
  text-align: left;
}
.llm-detail { min-width: 0; }
/* 内置厂商快速配置面板：贴 key → 拉取 → 勾选 → 保存，一屏完成 */
.qc-panel .key-row { width: 100%; }
.qc-default-row {
  display: flex; align-items: center; gap: 8px; margin-top: 10px;
  font-size: var(--fs-sm); color: var(--muted); cursor: pointer;
}
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
/* 开关：小圆角轨道（不是胶囊），滑块是方块 —— 与全站小圆角语言一致 */
.mr-toggle {
  width: 32px; height: 18px; flex: none; padding: 0;
  border-radius: var(--radius-sm); border: 1px solid var(--line);
  background: rgba(255, 255, 255, .06); cursor: pointer; position: relative;
  transition: background 140ms var(--ease), border-color 140ms var(--ease);
}
.mr-toggle span {
  position: absolute; top: 2px; left: 2px;
  width: 12px; height: 12px; border-radius: 2px;
  background: var(--dim);
  transition: transform 140ms var(--ease), background 140ms var(--ease);
}
.mr-toggle.on { background: rgba(255, 255, 255, .18); border-color: var(--line-strong); }
.mr-toggle.on span { transform: translateX(14px); background: #fff; }

/* 图标按钮：表格行里的小操作（编辑 / 删除） */
.icon-btn {
  width: 24px; height: 24px; flex: none; padding: 0;
  display: inline-grid; place-items: center;
  border-radius: var(--radius-sm); border: 1px solid transparent;
  background: transparent; color: var(--dim); cursor: pointer;
  transition: color 130ms var(--ease), background 130ms var(--ease), border-color 130ms var(--ease);
}
.icon-btn:hover { color: var(--ink); background: rgba(255, 255, 255, .07); border-color: var(--line); }
.icon-btn.danger:hover { color: var(--ink); border-color: var(--line-strong); background: rgba(255, 255, 255, .09); }
.icon-btn .app-ic { width: 13px; height: 13px; }

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
.test-result.ok { background: rgba(255, 255, 255, .08); color: var(--ink); }
.test-result.err { background: rgba(255, 255, 255, .08); color: var(--ink); }

.status-dot { width: 8px; height: 8px; border-radius: 50%; }
.status-dot.on { background: #fff; }
.status-dot.off { background: var(--dim); }

.sub-modal {
  margin-top: 16px; padding: 16px; background: var(--panel);
  border: 1px solid var(--line); border-radius: var(--radius-lg);
}
.form-actions { display: flex; gap: 8px; margin-top: 14px; }

/* 发现模型：勾选清单 */
.disc-list {
  display: flex; flex-direction: column; gap: 2px;
  max-height: 300px; overflow-y: auto; margin-top: 4px;
}
.disc-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 8px; border-radius: var(--radius-sm);
  font-size: var(--fs-sm); color: var(--ink); cursor: pointer;
}
.disc-row:hover { background: rgba(255, 255, 255, .05); }
.disc-row input[type='checkbox'] { accent-color: #fff; flex: none; }
.dr-model { font-family: var(--mono); font-size: 12px; }
.dr-have { margin-left: auto; font-size: 10.5px; color: var(--dim); flex: none; }
</style>
