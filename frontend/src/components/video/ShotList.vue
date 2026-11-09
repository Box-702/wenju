<template>
  <div class="shot-list">
    <div class="shot-header">
      <h4><span class="section-mark">03</span> 镜头列表</h4>
      <div class="shot-actions">
        <button class="small ghost" @click="$emit('refresh')">刷新</button>
        <button
          v-if="pendingShots.length"
          class="small ghost"
          @click="selectAllPending"
        >{{ allPendingSelected ? '取消全选' : '全选待审阅' }}</button>
        <button
          v-if="selectedIds.length"
          class="small ok"
          @click="approveSelected"
        >批准选中（{{ selectedIds.length }}）</button>
        <button class="small ok" @click="$emit('generate-all')" v-if="shots.length">
          全部生成视频
        </button>
      </div>
    </div>

    <div v-if="!shots.length" class="empty-state">
      <p>还没有镜头数据</p>
      <p class="hint">请先在对话中说「拆镜头」或「设计镜头」</p>
    </div>

    <div v-else class="shots-by-scene">
      <div v-for="(group, sceneId) in groupedShots" :key="sceneId" class="scene-group">
        <div class="scene-label" @click="toggleScene(sceneId)">
          <span class="expand-icon" :class="{ open: expanded[sceneId] }"><AppIcon name="chevron-right" /></span>
          <span>{{ sceneLabel(sceneId) }}</span>
          <span class="shot-count">{{ group.length }} 镜</span>
        </div>
        <div v-if="expanded[sceneId]" class="scene-shots">
          <div
            v-for="shot in group"
            :key="shot.id"
            :class="['shot-card', { selected: selectedShot === shot.id }]"
            @click="$emit('select-shot', shot)"
          >
            <div class="shot-top">
              <input
                v-if="shot.video_prompt"
                type="checkbox"
                :checked="selectedIds.includes(shot.id)"
                :disabled="shot.prompt_status === 'approved'"
                :aria-label="`选择 ${shot.subject || shot.id}`"
                @click.stop
                @change="toggleSelected(shot.id, $event.target.checked)"
              />
              <span class="shot-type-badge">{{ shotTypeLabel(shot.shot_type) }}</span>
              <span class="shot-order">#{{ shot.order + 1 }}</span>
              <span class="shot-duration">{{ shot.duration_sec }}s</span>
              <span :class="['video-status', videoStatusClass(shot)]">{{ videoStatusLabel(shot) }}</span>
            </div>
            <div class="shot-subject">{{ shot.subject }}</div>
            <!-- 紧凑模式：提示词只露第一句，点「详情」才展开全文（窄栏里要能读） -->
            <div v-if="compact && shot.video_prompt" class="shot-prompt">
              <span :class="['prompt-label', shot.prompt_status]">
                {{ promptStatusLabel(shot) }}
              </span>
              <span class="prompt-text">{{ firstSentence(shot.video_prompt) }}</span>
              <button class="shot-more" type="button" @click.stop="openShot[shot.id] = !openShot[shot.id]">
                {{ openShot[shot.id] ? '收起' : '详情' }}
              </button>
            </div>
            <div v-else class="shot-action">{{ shot.action }}</div>
            <div v-if="!compact" class="shot-meta">
              <span v-if="shot.camera?.type !== 'static'" class="meta-tag">
                {{ cameraLabel(shot.camera) }}
              </span>
              <span v-if="shot.lighting" class="meta-tag">光线 {{ shot.lighting }}</span>
              <span v-if="shot.mood" class="meta-tag">情绪 {{ shot.mood }}</span>
              <span v-if="shot.reference_videos?.length" class="meta-tag ref-tag">
                参考视频 ×{{ shot.reference_videos.length }}
              </span>
            </div>
            <div v-if="!compact && shot.camera?.path" class="shot-path">运动 {{ truncate(shot.camera.path, 80) }}</div>
            <div v-if="!compact && shot.spatial" class="shot-path">空间 {{ truncate(shot.spatial, 80) }}</div>
            <div v-if="!compact && shot.background_action" class="shot-path">
              背景 {{ truncate(shot.background_action, 80) }}
            </div>
            <div v-if="!compact && shot.final_frame" class="shot-path">终帧 {{ truncate(shot.final_frame, 80) }}</div>
            <div v-if="shot.video_prompt && !compact" class="shot-prompt">
              <span :class="['prompt-label', shot.prompt_status]">
                {{ promptStatusLabel(shot) }}
              </span>
              <span v-if="shot.prompt_audit?.length" class="audit-tag" title="提示词自动审计提醒，在审阅面板查看详情">
                <AppIcon name="warning" /> {{ shot.prompt_audit.length }}
              </span>
              <span class="prompt-text">{{ truncate(shot.video_prompt, 100) }}</span>
            </div>
            <!-- 紧凑模式展开：全文提示词 + 运镜/空间等细节（可读优先） -->
            <div v-if="compact && openShot[shot.id]" class="shot-detail">
              <p v-if="shot.video_prompt" class="detail-prompt">{{ shot.video_prompt }}</p>
              <p v-if="shot.camera?.path" class="detail-line">运动：{{ shot.camera.path }}</p>
              <p v-if="shot.spatial" class="detail-line">空间：{{ shot.spatial }}</p>
              <p v-if="shot.background_action" class="detail-line">背景：{{ shot.background_action }}</p>
              <p v-if="shot.final_frame" class="detail-line">终帧：{{ shot.final_frame }}</p>
              <p v-if="shot.action" class="detail-line">动作：{{ shot.action }}</p>
            </div>
            <div class="shot-actions-row">
              <button
                class="small ok"
                @click.stop="$emit('generate-video', shot)"
                v-if="shot.video_prompt && shot.prompt_status === 'approved' && !shot.video_url"
              >生成视频</button>
              <button
                class="small ghost"
                @click.stop="$emit('play-video', shot)"
                v-if="shot.video_url"
              ><AppIcon name="play" /> 播放</button>
              <button
                class="small ghost"
                @click.stop="$emit('edit-prompt', shot)"
              >编辑提示词</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, reactive, watch } from 'vue'
import AppIcon from '../AppIcon.vue'

const props = defineProps({
  shots: { type: Array, default: () => [] },
  scenes: { type: Array, default: () => [] },
  selectedShot: { type: String, default: null },
  /** 紧凑模式：侧栏里用——窄栏铺满运镜/空间/背景/终帧会淹掉内容，
      收紧成「主体 + 状态 + 提示词首句」，点开才看全文。 */
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['select-shot', 'generate-video', 'generate-all', 'play-video', 'edit-prompt', 'bulk-approve', 'refresh'])

const expanded = reactive({})
/** 紧凑模式下，单条镜头的详情展开状态 */
const openShot = reactive({})
const selectedIds = ref([])

const groupedShots = computed(() => {
  const groups = {}
  for (const shot of props.shots) {
    const sid = shot.scene_id || 'unknown'
    if (!groups[sid]) groups[sid] = []
    groups[sid].push(shot)
  }
  // 每组按 order 排序
  for (const key of Object.keys(groups)) {
    groups[key].sort((a, b) => (a.order || 0) - (b.order || 0))
  }
  return groups
})
const pendingShots = computed(() => props.shots.filter(
  shot => shot.video_prompt && shot.prompt_status !== 'approved',
))
const allPendingSelected = computed(() => (
  pendingShots.value.length > 0 &&
  pendingShots.value.every(shot => selectedIds.value.includes(shot.id))
))

watch(() => props.shots, (shots) => {
  const valid = new Set(shots.map(shot => shot.id))
  selectedIds.value = selectedIds.value.filter(id => valid.has(id))
}, { deep: true })

// 默认展开所有场景
const _initExpanded = () => {
  for (const key of Object.keys(groupedShots.value)) {
    if (expanded[key] === undefined) expanded[key] = true
  }
}
_initExpanded()

function toggleScene(id) { expanded[id] = !expanded[id] }

function toggleSelected(id, selected) {
  selectedIds.value = selected
    ? [...new Set([...selectedIds.value, id])]
    : selectedIds.value.filter(item => item !== id)
}

function selectAllPending() {
  selectedIds.value = allPendingSelected.value
    ? []
    : pendingShots.value.map(shot => shot.id)
}

function approveSelected() {
  if (!selectedIds.value.length) return
  emit('bulk-approve', [...selectedIds.value])
  selectedIds.value = []
}

function sceneLabel(sceneId) {
  const sc = props.scenes.find(s => s.id === sceneId)
  return sc ? `${sc.id} ${sc.title}` : sceneId
}

const shotTypeLabels = {
  extreme_wide: '极远景', wide: '远景', medium: '中景',
  close_up: '近景', extreme_close_up: '特写', over_shoulder: '过肩', pov: '主观',
}
function shotTypeLabel(t) { return shotTypeLabels[t] || t }

function cameraLabel(cam) {
  if (!cam) return ''
  const labels = {
    pan: '摇', tilt: '俯仰', dolly: '推拉', tracking: '跟', crane: '升降',
    handheld: '手持', zoom: '变焦', orbit: '环绕', steady: '稳定',
  }
  const dir = cam.direction || ''
  const speed = cam.speed === 'slow' ? '慢' : cam.speed === 'fast' ? '快' : ''
  return `${speed}${labels[cam.type] || cam.type}${dir ? ' ' + dir : ''}`
}

function videoStatusClass(shot) {
  if (shot.video_url) return 'done'
  if (shot.video_job_id) return 'generating'
  if (shot.video_prompt && shot.prompt_status === 'approved') return 'ready'
  if (shot.video_prompt) return 'review'
  return 'pending'
}

function videoStatusLabel(shot) {
  if (shot.video_url) return '完成'
  if (shot.video_job_id) return '生成中'
  if (shot.video_prompt && shot.prompt_status !== 'approved') return '待审阅'
  if (shot.video_prompt) return '待生成'
  return '未准备'
}

function promptStatusLabel(shot) {
  return shot.prompt_status === 'approved' ? '已批准' : shot.prompt_status === 'needs_revision' ? '需修改' : '待审阅'
}

/** 提示词的第一句（紧凑模式用：够看出这条镜头在拍什么）。 */
function firstSentence(text) {
  const t = String(text || '').trim()
  const cut = t.split(/[。！？.!?]/)[0] || t
  return cut.slice(0, 60).trim()
}

function truncate(s, n) { return s && s.length > n ? s.slice(0, n) + '...' : s }
</script>

<style scoped>
.shot-list { padding: 12px; }
.shot-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.shot-header h4 { margin: 0; font-size: 14px; }
.section-mark { color: var(--gold); font: 700 9px/1 var(--mono); letter-spacing: .08em; }
.shot-actions { display: flex; gap: 6px; }

.empty-state { text-align: center; padding: 40px 20px; color: var(--text2, #666); }
.empty-state .hint { font-size: 12px; color: var(--text2, #555); }

.scene-group { margin-bottom: 8px; }
.scene-label {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  background: rgba(255,255,255,.02); border-radius: var(--radius-sm); cursor: pointer;
  font-size: 12px; font-weight: 600;
}
.scene-label:hover { background: rgba(255,255,255,.04); }
.expand-icon { display: inline-flex; font-size: 10px; color: var(--text2, #666); transition: transform 130ms var(--ease); }
.expand-icon.open { transform: rotate(90deg); }
.shot-count { margin-left: auto; font-size: 11px; color: var(--text2, #666); font-weight: 400; }

.scene-shots { padding: 4px 0 4px 16px; }
.shot-card {
  padding: 10px; margin-bottom: 6px; background: rgba(255,255,255,.02);
  border: 1px solid var(--border, #222); border-radius: var(--radius-sm); cursor: pointer;
  transition: border-color .15s;
}
.shot-card:hover { border-color: var(--line-strong); background: color-mix(in oklch, var(--ink) 3%, transparent); }
.shot-card.selected { border-color: color-mix(in oklch, var(--cyan) 65%, var(--line)); background: color-mix(in oklch, var(--cyan) 5%, transparent); }
.shot-card.selected-for-review { border-color: color-mix(in oklch, var(--ok) 55%, var(--line)); }

.shot-top { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }
.shot-type-badge {
  font-size: 10px; padding: 1px 6px; background: color-mix(in oklch, var(--violet) 10%, transparent);
  border-radius: 3px; color: var(--violet);
}
.shot-order { font-size: 11px; color: var(--text2, #666); }
.shot-duration { font-size: 10px; color: var(--text2, #555); }
.video-status { font-size: 12px; margin-left: auto; }
.video-status.done { color: var(--green, #4ade80); }
.video-status.generating { color: var(--gold, #e94560); }
.video-status.ready { color: var(--cyan, #22d3ee); }
.video-status.review { color: var(--warn); }
.video-status.pending { color: var(--text2, #444); }

.shot-subject { font-size: 13px; margin-bottom: 2px; }
.shot-action { font-size: 11px; color: var(--text2, #888); margin-bottom: 4px; }
.shot-meta { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 4px; }
.shot-path { font-size: 10px; color: var(--text2, #777); margin-bottom: 2px; line-height: 1.4; }
.meta-tag { font-size: 10px; padding: 1px 5px; background: rgba(255,255,255,.04); border-radius: 3px; color: var(--text2, #777); }

.shot-prompt { font-size: 10px; color: var(--text2, #666); margin-bottom: 6px; line-height: 1.4; }
.prompt-label { color: var(--warn); margin-right: 4px; }
.prompt-label.approved { color: var(--ok); }
.prompt-label.needs_revision { color: var(--bad); }
.audit-tag {
  display: inline-flex; align-items: center; gap: 3px;
  margin-right: 4px; padding: 0 4px; border-radius: 3px;
  color: var(--warn); border: 1px solid color-mix(in oklch, var(--warn) 40%, transparent);
  font-variant-numeric: tabular-nums;
}
.audit-tag .app-ic { font-size: 10px; }

.meta-tag.ref-tag {
  color: var(--cyan);
  background: color-mix(in oklch, var(--cyan) 10%, transparent);
}

/* 紧凑模式：提示词首句 + 详情按钮；展开后是可读的正文 */
.shot-more {
  flex: none; margin-left: 6px; padding: 0 6px;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: transparent; color: var(--dim);
  font-size: 10px; cursor: pointer;
}
.shot-more:hover { color: var(--ink); border-color: var(--line-strong); }
.shot-detail {
  margin: 6px 0 2px; padding: 8px 10px;
  border-left: 2px solid var(--line-strong);
  background: rgba(255, 255, 255, .03);
  border-radius: 0 var(--radius-md) var(--radius-md) 0;
}
.detail-prompt {
  margin: 0 0 6px; font-size: var(--fs-sm); line-height: 1.75; color: var(--ink);
  word-break: break-word;
}
.detail-line { margin: 0 0 3px; font-size: var(--fs-xs); line-height: 1.6; color: var(--muted); }

.shot-actions-row { display: flex; gap: 4px; }
</style>
