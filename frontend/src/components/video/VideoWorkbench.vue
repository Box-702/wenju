<script setup>
// =====================================================================
// VideoWorkbench.vue —— 视频制作工作台
//
// 流程：摄影指导提出 Prompt → 人工编辑/批准 → 提交视频任务 →
// 可选的成片审批。分镜生成串行推进，前镜成片自动作为后镜接力参考。
// =====================================================================

import { computed, onMounted, ref } from 'vue'
import ShotList from './ShotList.vue'
import VideoPlayer from './VideoPlayer.vue'
import CastLibrary from './CastLibrary.vue'
import AppIcon from '../AppIcon.vue'
import {
  store, loadShots, loadVideoJobs, submitVideoJob, playVideo, closeVideo, openPromptEditor,
  closePromptEditor, loadPromptHistory, saveShotPrompt, bulkReviewPrompts,
  setVideoMode, setVideoResolution, setVideoAspect, setVideoSubtitle, startVideoBatch,
  pollVideoBatch, ensureBatchPolling, batchCommand,
  startFinalize, pollFinalize, ensureFinalizePolling,
} from '../../stores/app'

// 画幅：短剧产品默认竖屏 9:16
const ASPECTS = [
  { value: '9:16', label: '9:16 竖屏 · 短剧' },
  { value: '16:9', label: '16:9 横屏' },
  { value: '1:1', label: '1:1 方形' },
  { value: '4:3', label: '4:3 传统' },
  { value: '21:9', label: '21:9 宽银幕' },
]

const RESOLUTIONS = [
  { value: '', label: '默认（768P）' },
  { value: '480P', label: '480P（最快）' },
  { value: '768P', label: '768P' },
  { value: '2K', label: '2K（最清晰）' },
]

const batch = computed(() => store.videoBatch)
const running = computed(() => batch.value?.status === 'running')
const awaiting = computed(() => batch.value?.status === 'awaiting_approval')
const progressText = computed(() => {
  const b = batch.value
  if (!b) return ''
  return `${Math.min(b.current_index + 1, b.total)} / ${b.total} 镜`
})
const promptCount = computed(() => {
  const prompts = store.shots.filter((shot) => shot.video_prompt)
  return {
    total: prompts.length,
    approved: prompts.filter((shot) => shot.prompt_status === 'approved').length,
    pending: prompts.filter((shot) => shot.prompt_status !== 'approved').length,
  }
})
const versionStatusLabels = {
  empty: '无提示词',
  approved: '全部已批准',
  needs_review: '待审阅',
  needs_revision: '需修改',
  mixed: '部分已批准',
}

// 审批预览：当前镜最新的成功视频
const currentShot = computed(() => store.shots.find((s) => s.id === batch.value?.current_shot_id) || null)
const currentVideoUrl = computed(() => {
  const sid = batch.value?.current_shot_id
  if (!sid) return ''
  const jobs = (store.videoJobs || [])
    .filter((j) => j.shot_id === sid && j.status === 'succeeded' && j.video_url)
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
  return jobs[0]?.video_url || ''
})

// ---- 成片合成 ----
const finalize = computed(() => store.videoFinalize)
const finalizeRunning = computed(() => finalize.value?.status === 'running')
const finalizeResult = computed(() => finalize.value?.status === 'succeeded' ? finalize.value.result : null)
const succeededCount = computed(() =>
  (store.videoJobs || []).filter((j) => j.status === 'succeeded' && j.video_url).length
)
const finalizeTail = computed(() => (finalize.value?.messages || []).slice(-1)[0] || '正在准备镜头…')
const filmMetaText = computed(() => {
  const r = finalizeResult.value
  if (!r) return ''
  const bits = []
  if (r.duration_sec) bits.push(`${Number(r.duration_sec).toFixed(1)} 秒`)
  if (r.shots_used != null) bits.push(`${r.shots_used}/${r.shots_total} 镜`)
  const flags = []
  if (r.dubbed) flags.push('配音')
  if (r.subtitled) flags.push('字幕')
  if (flags.length) bits.push(flags.join('·'))
  return bits.join(' · ')
})

const starting = ref(false)
// 资产库：角色形象 + 声音 + 图片 + 场景的对照表（配音串角问题的根治入口）
const showLibrary = ref(false)
async function onStart() {
  starting.value = true
  try {
    await startVideoBatch()
  } finally {
    starting.value = false
  }
}

// 批次轮询收敛到 store 层（ensureBatchPolling）：切走视图也能继续跟进，
// 进度经 store.videoBatch 响应式同步回来；这里只负责进面板时的首次拉取。
onMounted(async () => {
  await Promise.all([loadShots(), loadVideoJobs(), pollVideoBatch(), pollFinalize()])
  ensureBatchPolling()
  ensureFinalizePolling()
})

async function onEditPrompt(shot) {
  openPromptEditor(shot)
  await loadPromptHistory(shot.id)
}

async function onBulkApprove(shotIds) {
  await bulkReviewPrompts(shotIds, 'approve')
}

function formatVersionTime(value) {
  if (!value) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}
</script>

<template>
  <div class="workbench">
    <!-- 控制栏：模式 / 分辨率 / 启动 -->
    <div class="ctrl">
      <div class="ctrl-group">
        <span class="ctrl-label">资产</span>
        <button
          class="seg-btn"
          style="padding: 4px 10px; border: 1px solid var(--line); border-radius: var(--radius-sm); background: transparent; cursor: pointer"
          title="角色定妆图、音色、性别与场景图的对照表——配音前先在这里核对"
          @click="showLibrary = true"
        >资产库</button>
      </div>

      <div class="ctrl-group">
        <span class="ctrl-label">成片审批</span>
        <div class="seg">
          <button
            :class="['seg-btn', { active: store.videoMode === 'auto' }]"
            title="提示词批准后一次跑完全部镜头"
            @click="setVideoMode('auto')"
          >自动出片</button>
          <button
            :class="['seg-btn', { active: store.videoMode === 'approval' }]"
            title="每镜完成后暂停，预览后决定继续/重跑/终止"
            @click="setVideoMode('approval')"
          >逐镜审批</button>
        </div>
      </div>

      <div class="ctrl-group">
        <span class="ctrl-label">画幅</span>
        <select
          class="res-select"
          :value="store.videoAspect"
          @change="setVideoAspect($event.target.value)"
        >
          <option v-for="a in ASPECTS" :key="a.value" :value="a.value">{{ a.label }}</option>
        </select>
      </div>

      <div class="ctrl-group">
        <span class="ctrl-label">清晰度</span>
        <select
          class="res-select"
          :value="store.videoResolution"
          @change="setVideoResolution($event.target.value)"
        >
          <option v-for="r in RESOLUTIONS" :key="r.value" :value="r.value">{{ r.label }}</option>
        </select>
      </div>

      <div class="ctrl-group">
        <span class="ctrl-label">台词字幕</span>
        <div class="seg">
          <button
            :class="['seg-btn', { active: store.videoSubtitle }]"
            title="合成成片时把台词字幕烧进画面，同时附带 SRT 字幕文件"
            @click="setVideoSubtitle(true)"
          >开</button>
          <button
            :class="['seg-btn', { active: !store.videoSubtitle }]"
            title="成片不带字幕"
            @click="setVideoSubtitle(false)"
          >关</button>
        </div>
      </div>

      <button
        class="start-btn"
        :disabled="starting || running || !promptCount.approved"
        @click="onStart"
      >
        {{ running ? '批次进行中…' : `生成已批准镜头（${promptCount.approved}）` }}
      </button>
    </div>

    <div class="prompt-gate">
      <div>
        <strong>提示词人工审阅</strong>
        <span>摄影指导只提供草稿，批准后才会产生视频费用。</span>
      </div>
      <div class="prompt-count">
        <span class="approved">{{ promptCount.approved }} 已批准</span>
        <span v-if="promptCount.pending">{{ promptCount.pending }} 待处理</span>
        <span v-else-if="promptCount.total">全部已批准</span>
      </div>
    </div>

    <!-- 成片合成：拼装 + 配音 + 字幕（字幕开关在上方控制栏） -->
    <div class="film">
      <div class="film-head">
        <strong>成片</strong>
        <span v-if="finalizeResult">{{ filmMetaText }}</span>
        <span v-else-if="!succeededCount">还没有已生成的镜头</span>
      </div>

      <div v-if="finalizeRunning" class="film-status">
        <span class="film-spinner" aria-hidden="true"></span>合成中：{{ finalizeTail }}
      </div>
      <div v-else-if="finalize?.status === 'failed'" class="film-status bad">
        合成失败：{{ finalize?.error }}
      </div>

      <video
        v-if="finalizeResult?.video_url"
        :key="finalizeResult.video_asset_id"
        :src="finalizeResult.video_url"
        controls
        class="film-video"
      ></video>

      <div v-if="finalizeResult" class="film-actions">
        <a class="rv ok" :href="finalizeResult.video_url" download>下载成片</a>
        <a v-if="finalizeResult.srt_url" class="rv" :href="finalizeResult.srt_url" download>下载字幕（SRT）</a>
        <button
          class="rv"
          :disabled="finalizeRunning || !succeededCount"
          title="用已生成的镜头重新拼一版（配音与字幕按当前开关）"
          @click="startFinalize()"
        >重新合成</button>
      </div>
      <div v-else class="film-actions">
        <button
          class="rv ok"
          :disabled="finalizeRunning || !succeededCount"
          :title="succeededCount ? '把已生成的镜头拼装成完整成片（含配音与字幕）' : '先完成至少一个镜头的生成'"
          @click="startFinalize()"
        >合成成片{{ succeededCount ? `（${succeededCount} 镜）` : '' }}</button>
      </div>

      <div v-if="finalizeResult?.warnings?.length" class="film-warns">
        <span v-for="w in finalizeResult.warnings" :key="w">{{ w }}</span>
      </div>
    </div>

    <div v-if="store.videoVersions.length" class="version-strip">
      <div class="version-strip-head">
        <strong>提示词版本</strong>
        <span>{{ store.videoVersions.length }} 个快照</span>
      </div>
      <div class="version-list">
        <div
          v-for="version in store.videoVersions"
          :key="version.id"
          :class="['version-row', { current: version.current }]"
        >
          <span class="version-label">{{ version.label || version.id }}</span>
          <span class="version-status" :class="version.prompt_summary?.review_status">
            {{ versionStatusLabels[version.prompt_summary?.review_status] || '待审阅' }}
          </span>
          <span class="version-count">
            {{ version.prompt_summary?.approved_count || 0 }}/{{ version.prompt_summary?.prompt_count || 0 }} 已批准
          </span>
          <span class="version-time">{{ formatVersionTime(version.created_at) }}</span>
        </div>
      </div>
    </div>

    <!-- 批次进度 / 审批面板 -->
    <div v-if="batch && batch.status !== 'none'" class="batch" :class="'batch-' + batch.status">
      <div class="batch-head">
        <span class="batch-status">
          <template v-if="batch.status === 'running'">生成中 {{ progressText }}</template>
          <template v-else-if="awaiting">等待审批 {{ progressText }}</template>
          <template v-else-if="batch.status === 'done'">批次完成（{{ batch.total }} 镜）</template>
          <template v-else-if="batch.status === 'failed'">批次失败</template>
          <template v-else-if="batch.status === 'aborted'">已终止</template>
        </span>
        <span v-if="currentShot" class="batch-shot">{{ currentShot.subject || currentShot.id }}</span>
      </div>
      <div v-if="batch.error" class="batch-error">{{ batch.error }}</div>

      <!-- 审批预览 -->
      <div v-if="awaiting" class="review">
        <video v-if="currentVideoUrl" :src="currentVideoUrl" controls class="review-video" />
        <div v-else class="review-empty">成片地址加载中…</div>
        <div class="review-actions">
          <button class="rv ok" @click="batchCommand('approve')">✓ 通过，下一镜</button>
          <button class="rv" @click="batchCommand('reroll')">↻ 重跑本镜</button>
          <button class="rv danger" @click="batchCommand('abort')"><AppIcon name="close" /> 终止批次</button>
        </div>
      </div>
      <div v-else-if="running || batch.status === 'failed' || batch.status === 'aborted'" class="review-actions">
        <button v-if="running" class="rv danger" @click="batchCommand('abort')">终止批次</button>
        <button v-if="batch.status === 'failed'" class="rv" @click="batchCommand('reroll')">重跑当前镜</button>
      </div>
    </div>

    <!-- 镜头列表 -->
    <ShotList
      :shots="store.shots"
      :scenes="store.viewerScript?.scenes || []"
      @generate-video="submitVideoJob"
      @generate-all="onStart"
      @play-video="playVideo"
      @edit-prompt="onEditPrompt"
      @bulk-approve="onBulkApprove"
      @refresh="loadShots()"
    />

    <!-- 单镜播放器（store 控制） -->
    <VideoPlayer
      v-if="store.playingVideo"
      :url="store.playingVideo.url"
      :title="store.playingVideo.title"
      :meta="store.playingVideo.meta"
      @close="closeVideo()"
    />

    <!-- 资产库：角色形象 / 声音 / 图片 / 场景对照 -->
    <CastLibrary :open="showLibrary" @close="showLibrary = false" />
  </div>
</template>

<style scoped>
.workbench { padding: 12px 16px; display: flex; flex-direction: column; gap: 12px; }
.ctrl { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.ctrl-group { display: flex; align-items: center; gap: 8px; }
.ctrl-label { font-size: 11px; color: var(--dim); font-weight: 600; }
.seg { display: flex; border: 1px solid var(--line); border-radius: var(--radius-md); overflow: hidden; }
.seg-btn {
  background: transparent; border: none; color: var(--muted);
  padding: 5px 12px; font-size: 12px; cursor: pointer; transition: all 150ms;
}
.seg-btn + .seg-btn { border-left: 1px solid var(--line); }
.seg-btn.active { background: var(--select); color: var(--ink); font-weight: 600; }
.res-select {
  background: var(--panel2); color: var(--ink); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: 5px 8px; font-size: 12px;
}
.start-btn {
  background: var(--accent-grad); color: var(--on-accent); border-color: transparent;
  box-shadow: 0 8px 24px rgba(34, 197, 94, .26);
  margin-left: auto; border: none;
  border-radius: var(--radius-md); padding: 8px 16px; font-size: 12.5px; font-weight: 650; cursor: pointer;
}
.start-btn:disabled { opacity: 0.45; cursor: not-allowed; }

.prompt-gate {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--radius-md);
  background: color-mix(in oklch, var(--panel2) 70%, transparent);
}
.prompt-gate strong { display: block; color: var(--ink); font-size: 12px; }
.prompt-gate span { color: var(--dim); font-size: 11px; }
.prompt-count { display: flex; gap: 10px; flex: none; font-size: 11px; }
.prompt-count .approved { color: var(--ok); }

.film {
  border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 12px 14px;
  background: var(--panel2); display: flex; flex-direction: column; gap: 10px;
}
.film-head { display: flex; align-items: baseline; gap: 10px; }
.film-head strong { color: var(--ink); font-size: 12.5px; }
.film-head span { color: var(--dim); font-size: 11.5px; }
.film-status { display: flex; align-items: center; gap: 8px; font-size: 11.5px; color: var(--dim); }
.film-status.bad { color: var(--danger, #e5484d); }
.film-spinner {
  width: 10px; height: 10px; flex: none; border-radius: 50%;
  border: 2px solid var(--line); border-top-color: var(--cyan);
  animation: film-spin 0.9s linear infinite;
}
@keyframes film-spin { to { transform: rotate(360deg); } }
.film-video { width: 100%; max-width: 560px; border-radius: var(--radius-md); background: #000; }
.film-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.film-warns { display: flex; flex-direction: column; gap: 3px; font-size: 11px; color: var(--warn); }

.version-strip {
  border: 1px solid var(--line); border-radius: var(--radius-md); padding: 10px 12px;
  background: color-mix(in oklch, var(--panel2) 70%, transparent);
}
.version-strip-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 7px; font-size: 11px; color: var(--dim);
}
.version-strip-head strong { color: var(--ink); font-size: 12px; }
.version-list { display: flex; flex-direction: column; gap: 4px; }
.version-row {
  display: grid; grid-template-columns: minmax(120px, 1fr) auto auto auto;
  gap: 10px; align-items: center; padding: 5px 7px; border-radius: var(--radius-sm);
  font-size: 10.5px; color: var(--dim);
}
.version-row.current { background: color-mix(in oklch, var(--cyan) 7%, transparent); color: var(--ink); }
.version-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.version-status.approved { color: var(--ok); }
.version-status.needs_review, .version-status.mixed { color: var(--warn); }
.version-status.needs_revision { color: var(--bad); }
.version-count, .version-time { white-space: nowrap; }

.batch { border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 12px 14px; background: var(--panel2); }
.batch-awaiting_approval { border-color: color-mix(in oklch, var(--warn) 45%, var(--line)); }
.batch-head { display: flex; align-items: center; gap: 12px; }
.batch-status { font-size: 12.5px; font-weight: 600; color: var(--ink); }
.batch-shot { font-size: 11.5px; color: var(--dim); }
.batch-error { margin-top: 6px; font-size: 11.5px; color: var(--danger, #e5484d); }

.review { margin-top: 10px; }
.review-video { width: 100%; max-width: 560px; border-radius: var(--radius-md); background: #000; }
.review-empty { font-size: 12px; color: var(--dim); padding: 8px 0; }
.review-actions { display: flex; gap: 8px; margin-top: 10px; }
.rv {
  border: 1px solid var(--line); background: var(--panel2); color: var(--ink);
  border-radius: var(--radius-md); padding: 6px 14px; font-size: 12.5px; cursor: pointer;
}
.rv.ok {
  background: var(--accent-grad); color: var(--on-accent); border-color: transparent;
  box-shadow: 0 8px 24px rgba(34, 197, 94, .26); font-weight: 600; }
.rv.danger { color: var(--danger, #e5484d); }

@media (max-width: 620px) {
  .prompt-gate { align-items: flex-start; flex-direction: column; gap: 6px; }
  .version-row { grid-template-columns: 1fr auto; }
  .version-count, .version-time { display: none; }
}
</style>
