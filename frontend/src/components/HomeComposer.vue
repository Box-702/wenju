<script setup>
// =====================================================================
// HomeComposer.vue —— 首页主输入卡片
//
// 卡片本体在 ComposerCard.vue（首页与工作区共用同一份）；这里只管首页特有的：
// 产出目标、素材菜单、参数胶囊、图片上传。
// =====================================================================

import { ref, computed, onMounted } from 'vue'
import { nextTick } from 'vue'
import { store, sendMessage, loadModelStatus, loadProviders, notify } from '../stores/app'
import { uploadChatImage } from '../api'
import ComposerCard from './ComposerCard.vue'

const props = defineProps({
  /** 产出目标：决定发送时给 Agent 的意图前缀 */
  goal: { type: String, default: 'novel-to-drama' },
})
const emit = defineEmits(['pick-library', 'open-skills'])

const text = ref('')
const fileInput = ref(null)
const card = ref(null)
const attachedImages = ref([])
const uploadingCount = ref(0)

const GOALS = [
  { id: 'novel-to-drama', label: '小说改编' },
  { id: 'storyboard', label: '分镜设计' },
  { id: 'production', label: '成片交付' },
]

// 「+」菜单：只留当前模型里真实存在的东西。
// 旧模型那套「主体库（支持音色）」「音色库」已删——它们跟「资产库」是同一个面板，
// 点了也只是打开左栏，属于名不副实的重复入口。


const goalLabel = computed(() => (GOALS.find((g) => g.id === props.goal) || GOALS[0]).label)
const canSend = computed(() => (!!text.value.trim() || attachedImages.value.some((i) => i.id))
  && !store.streaming && uploadingCount.value === 0)

onMounted(() => {
  loadModelStatus()
  // 模型下拉的候选来自这里；不拉的话下拉是空的（点了像没反应）
  loadProviders()
})

/** 「＋」= 往这条消息里加什么：图片 / 资产引用。 */
function pickImages() {
  fileInput.value?.click()
}

function openMention() {
  card.value?.openMention?.()
}

function onFileChosen(e) {
  const files = [...(e.target.files || [])]
  e.target.value = ''
  for (const f of files) addImage(f)
}

async function addImage(file) {
  if (!file.type.startsWith('image/')) {
    notify('目前只支持上传图片')
    return
  }
  const item = { id: null, url: '', name: file.name, uploading: true, preview: URL.createObjectURL(file) }
  attachedImages.value.push(item)
  uploadingCount.value++
  try {
    const res = await uploadChatImage(file)
    item.id = res.id
    item.url = res.url
  } catch (e) {
    notify(`图片上传失败：${e.message}`)
    const i = attachedImages.value.indexOf(item)
    if (i >= 0) attachedImages.value.splice(i, 1)
  } finally {
    item.uploading = false
    uploadingCount.value--
  }
}

function removeImage(item) {
  const i = attachedImages.value.indexOf(item)
  if (i >= 0) {
    URL.revokeObjectURL(item.preview)
    attachedImages.value.splice(i, 1)
  }
}

// ---- @ 引用的资产 ----
const refs = ref([])
function onAssetPicked(item) {
  if (!item?.id) return
  if (refs.value.some((r) => r.id === item.id)) return
  refs.value = [...refs.value, { kind: item.kind, id: item.id, name: item.name }]
}

function send() {
  const t = text.value.trim()
  if ((!t && !attachedImages.value.some((i) => i.id)) || store.streaming || uploadingCount.value > 0) return
  const imageIds = attachedImages.value.filter((i) => i.id).map((i) => i.id)
  attachedImages.value.forEach((i) => URL.revokeObjectURL(i.preview))
  text.value = ''
  attachedImages.value = []
  // 产出目标作为意图前缀，让 Agent 一上来就知道这轮要什么
  const prefix = goalLabel.value ? `【${goalLabel.value}】` : ''
  const used = refs.value
  refs.value = []
  sendMessage(prefix + t, imageIds, used)
  // 发送即进对话视图：首页不渲染消息流，不切过去用户会看到「输入了毫无反应」。
  // 对话优先——首页输入框是「开一部新作品的起点」，不是独立模式。
  store.mode = 'workspace'
}

/** 「＋」菜单里的「生成资产」：放一句触发语进输入框，剩下的由用户写、agent 按技能走流程。 */
function startAssetGen() {
  const lead = '帮我生成资产：'
  if (!text.value.includes(lead)) {
    text.value = text.value ? `${text.value} ${lead}` : lead
  }
  nextTick(() => {
    const el = card.value?.$el?.querySelector('textarea')
    if (el) {
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
    }
  })
}

/** 首页入口芯片用：把引导语放进输入框并聚焦（D-47：入口是糖，对话是本体）。 */
function setDraft(lead) {
  text.value = lead
  nextTick(() => {
    const el = card.value?.$el?.querySelector('textarea')
    if (el) {
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
    }
  })
}

defineExpose({ setDraft })

</script>

<template>
  <ComposerCard
    ref="card"
    v-model:text="text"
    :can-send="canSend"
    :busy="store.streaming"
    @send="send"
    @open-settings="store.showSettings = true"
    @open-skills="emit('open-skills')"
    :assets="store.assetIndex"
    @asset-picked="onAssetPicked"
    @new-project="store.showNewProject = true"
  >
    <template #menu="{ close }">
      <input
        ref="fileInput"
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        multiple
        hidden
        @change="onFileChosen"
      />
      <!-- 「＋」= 往这条消息里加什么 -->
      <button class="menu-item" type="button" role="menuitem" @click.stop="close(); pickImages()">
        上传图片
      </button>
      <button class="menu-item" type="button" role="menuitem" @click.stop="close(); openMention()">
        引用资产
      </button>
      <button class="menu-item" type="button" role="menuitem" @click.stop="close(); startAssetGen()">
        生成资产
      </button>
      <button class="menu-item" type="button" role="menuitem" @click.stop="close(); emit('open-skills')">
        技能
      </button>
      <button
        v-if="!store.pid"
        class="menu-item"
        type="button"
        role="menuitem"
        @click.stop="close(); store.showNewProject = true"
      >新建作品</button>
    </template>

    <template #attachments>
      <div v-if="attachedImages.length" class="hc-strip">
        <div v-for="item in attachedImages" :key="item.preview" class="hc-thumb">
          <img :src="item.preview" alt="待发送图片" />
          <button class="hc-thumb-x" type="button" aria-label="移除图片" @click.stop="removeImage(item)">
            ✕
          </button>
          <span v-if="item.uploading" class="hc-thumb-mask">上传中</span>
        </div>
      </div>
    </template>
  </ComposerCard>
</template>

<style scoped>
.hc-strip {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.hc-thumb {
  position: relative;
  width: 56px;
  height: 56px;
  border-radius: var(--radius-md);
  overflow: hidden;
  border: 1px solid var(--line);
}

.hc-thumb img { width: 100%; height: 100%; object-fit: cover; }

.hc-thumb-x {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 18px;
  height: 18px;
  border: 0;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: rgba(0, 0, 0, 0.66);
  color: #fff;
  cursor: pointer;
  font-size: 10px;
  line-height: 1;
}

.hc-thumb-mask {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgba(0, 0, 0, 0.55);
  font-size: 11px;
  color: rgba(255, 255, 255, 0.85);
}

/* 菜单条目：挂在 ComposerCard 的插槽里，样式跟着卡片走 */

</style>
