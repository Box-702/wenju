<template>
  <div class="prompt-editor-wrap" v-if="shot">
    <div class="prompt-editor-scrim" @click="$emit('close')"></div>
    <div class="prompt-editor">
    <div class="editor-header">
      <div>
        <div class="eyebrow">HUMAN REVIEW</div>
        <h4>审阅镜头提示词</h4>
      </div>
      <button class="ghost" aria-label="关闭 Prompt 审阅" @click="$emit('close')">关闭</button>
    </div>

    <div class="shot-info">
      <span class="shot-badge">{{ shot.shot_type }}</span>
      <span class="shot-title">{{ shot.subject }}</span>
      <span class="review-status" :class="statusClass">{{ statusLabel }}</span>
    </div>

    <div class="editor-field">
      <label for="prompt-review-text">视频提示词</label>
      <textarea
        id="prompt-review-text"
        v-model="editedPrompt"
        class="prompt-textarea"
        rows="9"
        placeholder="输入这一个镜头最终要交给视频模型的描述"
      />
      <div class="field-foot">
        <span>这是当前镜头的最终出片指令，批准后才会进入生成队列。</span>
        <span class="char-count">{{ editedPrompt.length }} 字</span>
      </div>
    </div>

    <div v-if="auditItems.length" class="prompt-audit">
      <div class="audit-head">
        <span class="audit-mark"><AppIcon name="warning" /></span>
        <span>自动审计 · {{ auditItems.length }} 项提醒</span>
      </div>
      <p v-for="(item, i) in auditItems" :key="i" class="audit-item">{{ item }}</p>
      <p class="audit-foot">审计基于「先戏剧，后语法」的硬规则：占位词、否定句、台词引用、一镜多运镜。提醒不阻断批准，但建议先处理再出片。</p>
    </div>

    <div class="editor-field">
      <label>参考视频 <span>可选，≤3 条（从参考素材里选）</span></label>
      <div class="ref-video-list">
        <div v-for="(id, i) in refAssetIds" :key="id" class="ref-row">
          <span class="ref-idx">{{ i + 1 }}</span>
          <span class="ref-name">{{ assetName(id) }}</span>
          <button
            class="ref-del"
            title="移除这条参考视频"
            aria-label="移除参考视频"
            @click="refAssetIds.splice(i, 1)"
          ><AppIcon name="close" /></button>
        </div>
        <p v-if="!refAssetIds.length" class="ref-hint">
          参考素材里的白模 / previz 可以挂在这里，锁住这一镜的画风。
        </p>
        <button
          v-if="refAssetIds.length < 3"
          class="ref-add"
          @click="pickerOpen = !pickerOpen"
        >
          {{ pickerOpen ? '收起' : '＋ 从参考素材里选' }}
        </button>
      </div>
      <div v-if="pickerOpen" class="ref-picker">
        <button
          v-for="a in videoAssets"
          :key="a.id"
          class="ref-pick"
          :class="{ on: refAssetIds.includes(a.id) }"
          :disabled="!refAssetIds.includes(a.id) && refAssetIds.length >= 3"
          @click="toggleAsset(a.id)"
        >
          <span class="rp-name">{{ a.name }}</span>
          <span class="rp-meta">{{ assetMeta(a) }}</span>
        </button>
        <p v-if="!videoAssets.length" class="ref-empty">
          参考素材里还没有视频。到「资产库 → 参考素材」导入后再回来选。
        </p>
      </div>
      <div class="field-foot">
        <span>随任务作为 reference_video 提交；自动接力的前镜成片不会被它覆盖。</span>
      </div>
    </div>

    <div class="editor-field">
      <label for="prompt-review-note">审阅备注 <span>可选</span></label>
      <textarea
        id="prompt-review-note"
        v-model="note"
        class="note-textarea"
        rows="2"
        placeholder="记录为什么这样改，方便回看版本"
      />
    </div>

    <div class="history" v-if="historyLoading || history.length">
      <div class="history-head">
        <span>审阅历史</span>
        <span v-if="historyLoading">加载中…</span>
        <span v-else>{{ history.length }} 个版本</span>
      </div>
      <div v-if="history.length" class="history-list">
        <details v-for="(item, index) in history" :key="item.version_id" :open="index === 0">
          <summary>
            <span>{{ item.label || item.version_id }}</span>
            <span :class="['history-status', item.prompt_status]">{{ statusLabelFor(item.prompt_status) }}</span>
            <span>{{ formatTime(item.created_at) }}</span>
          </summary>
          <div class="history-body">
            <p v-if="item.prompt_review_note" class="history-note">{{ item.prompt_review_note }}</p>
            <div v-if="item.prompt_changed" class="diff">
              <div class="diff-title">与上一版本的差异</div>
              <pre>{{ item.diff.join('\n') }}</pre>
            </div>
            <div v-else class="history-unchanged">提示词与上一版本相同。</div>
          </div>
        </details>
      </div>
    </div>

    <div class="editor-actions">
      <button class="ghost" @click="$emit('close')">取消</button>
      <button class="ghost" :disabled="!editedPrompt.trim()" @click="review('needs_revision')">标记需修改</button>
      <button class="ghost" :disabled="!editedPrompt.trim()" @click="review('save')">保存草稿</button>
      <button class="ok" :disabled="!editedPrompt.trim()" @click="review('approve')">
        批准并解锁出片
      </button>
    </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import { store, loadAssets } from '../../stores/app.js'

const props = defineProps({
  shot: { type: Object, default: null },
  history: { type: Array, default: () => [] },
  historyLoading: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'review'])

const editedPrompt = ref('')
const note = ref('')
// 参考视频：选参考素材（资产库里的本地文件），存的是素材 id。
const refAssetIds = ref([])
const pickerOpen = ref(false)

watch(() => props.shot, (s) => {
  editedPrompt.value = s?.video_prompt || ''
  note.value = s?.prompt_review_note || ''
  refAssetIds.value = [...(s?.reference_asset_ids || [])]
}, { immediate: true })

// 参考素材随作品变：打开审阅器时拉一次最新的。
watch(() => store.pid, (pid) => { if (pid) loadAssets(pid) }, { immediate: true })

/** 参考素材里可当参考视频的条目：本地导入的视频。 */
const videoAssets = computed(() =>
  (store.assets || []).filter(a => a.kind === 'video' && a.source === 'upload'),
)

function assetName(id) {
  const hit = (store.assets || []).find(a => a.id === id)
  return hit ? hit.name : '（素材已删除）'
}

function assetMeta(a) {
  if (a.duration_sec) return `${a.duration_sec.toFixed(1)}s`
  return ''
}

function toggleAsset(id) {
  const i = refAssetIds.value.indexOf(id)
  if (i >= 0) refAssetIds.value.splice(i, 1)
  else if (refAssetIds.value.length < 3) refAssetIds.value.push(id)
}

const statusLabel = computed(() => ({
  approved: '已批准',
  needs_review: '待审阅',
  needs_revision: '需修改',
}[props.shot?.prompt_status] || '待审阅'))

const statusClass = computed(() => props.shot?.prompt_status || 'needs_review')

const auditItems = computed(() => {
  // 展示服务端落库的确定性审计结论（DP 生成时与人工保存时都会刷新）。
  return Array.isArray(props.shot?.prompt_audit) ? props.shot.prompt_audit : []
})

function review(decision) {
  emit('review', {
    ...props.shot,
    video_prompt: editedPrompt.value.trim(),
    prompt_review_note: note.value.trim(),
    reference_asset_ids: [...refAssetIds.value],
    decision,
  })
}

function statusLabelFor(status) {
  return ({ approved: '已批准', needs_review: '待审阅', needs_revision: '需修改' }[status] || '待审阅')
}

function formatTime(value) {
  if (!value) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}
</script>

<style scoped>
/* 浮层：编辑器在 App 层，从侧栏或产出页打开都是这一块 */
.prompt-editor-wrap { position: fixed; inset: 0; z-index: 120; display: grid; place-items: center; }
.prompt-editor-scrim { position: absolute; inset: 0; background: oklch(0 0 0 / .55); }
.prompt-editor {
  position: relative; z-index: 1;
  width: min(760px, 94vw); max-height: 86vh; overflow-y: auto;
  background: var(--panel-solid); border: 1px solid var(--line);
  border-radius: var(--radius-card); padding: 18px 20px;
  box-shadow: var(--shadow-3);
  animation: modal-pop 180ms var(--ease);
}
@keyframes modal-pop { from { opacity: 0; transform: scale(.97) translateY(8px); } }
.editor-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.editor-header h4 { margin: 3px 0 0; font-size: 15px; }
.eyebrow { color: var(--gold); font: 700 9px/1 var(--mono); letter-spacing: .12em; }

.shot-info { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; font-size: 13px; }
.shot-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.shot-badge { font-size: 10px; padding: 2px 6px; background: var(--violet-soft); border-radius: 3px; color: var(--violet); }
.review-status { margin-left: auto; font-size: 11px; white-space: nowrap; }
.review-status.approved { color: var(--ok); }
.review-status.needs_review { color: var(--warn); }
.review-status.needs_revision { color: var(--bad); }

.editor-field { margin-bottom: 12px; }
.editor-field label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; }
.editor-field label span { color: var(--dim); font-size: 11px; }

.prompt-audit {
  margin: -4px 0 12px; padding: 9px 11px;
  background: color-mix(in oklch, var(--warn, #eab308) 9%, transparent);
  border: 1px solid color-mix(in oklch, var(--warn, #eab308) 35%, var(--line));
  border-radius: var(--radius-sm);
}
.audit-head { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--warn, #eab308); margin-bottom: 5px; }
.audit-mark { font-size: 12px; }
.audit-item { margin: 3px 0; font-size: 11.5px; line-height: 1.5; color: var(--ink); }
.audit-foot { margin-top: 6px; font-size: 10px; color: var(--dim); line-height: 1.4; }
.prompt-textarea {
  width: 100%; padding: 10px; background: var(--code-bg); border: 1px solid var(--line);
  border-radius: var(--radius-sm); color: var(--text, #eee); font-size: 13px; font-family: inherit;
  resize: vertical; box-sizing: border-box; line-height: 1.5;
}
.prompt-textarea:focus { outline: none; border-color: var(--gold); }
.note-textarea {
  width: 100%; padding: 8px 10px; background: var(--code-bg); border: 1px solid var(--line);
  border-radius: var(--radius-sm); color: var(--ink); font-size: 12px; font-family: inherit;
  resize: vertical; box-sizing: border-box; line-height: 1.45;
}
.note-textarea:focus { outline: none; border-color: var(--gold); }
.ref-video-list { display: flex; flex-direction: column; gap: 6px; }
.ref-row { display: flex; align-items: center; gap: 6px; }
.ref-idx {
  flex: none; width: 18px; height: 18px; display: grid; place-items: center;
  border-radius: 4px; border: 1px solid var(--line);
  color: var(--cyan); font: 600 10px/1 var(--mono);
}
.ref-name {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-size: 12px; color: var(--ink);
}
.ref-empty { font-size: 11.5px; line-height: 1.6; color: var(--dim); }
.ref-hint { margin: 0; font-size: 11.5px; line-height: 1.6; color: var(--dim); }
.ref-del {
  flex: none; width: 22px; height: 22px; border: 1px solid var(--line);
  border-radius: var(--radius-sm); background: transparent; color: var(--muted);
  cursor: pointer; font-size: 12px; line-height: 1;
}
.ref-del:hover { color: var(--bad); border-color: var(--bad); }
.ref-add {
  align-self: flex-start; padding: 4px 10px; border: 1px dashed var(--line);
  border-radius: var(--radius-sm); background: transparent; color: var(--muted);
  cursor: pointer; font-size: 11px;
}
.ref-add:hover { color: var(--ink); border-color: var(--line-strong); }
/* 参考素材选择面板 */
.ref-picker {
  display: flex; flex-direction: column; gap: 4px;
  margin: 6px 0 2px; padding: 6px;
  background: var(--code-bg); border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  max-height: 168px; overflow-y: auto;
}
.ref-pick {
  display: flex; align-items: baseline; gap: 8px; width: 100%;
  padding: 5px 7px; border: 1px solid transparent; border-radius: var(--radius-sm);
  background: transparent; color: var(--muted);
  cursor: pointer; font-size: 12px; text-align: left;
}
.ref-pick:hover:not(:disabled) { background: rgba(255, 255, 255, .05); color: var(--ink); }
.ref-pick.on { border-color: var(--cyan); color: var(--ink); }
.ref-pick:disabled { opacity: .45; cursor: default; }
.rp-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rp-meta { flex: none; font: 10px/1 var(--mono); color: var(--dim); }
.history { margin: 4px 0 14px; border-top: 1px solid var(--line); padding-top: 10px; }
.history-head { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; margin-bottom: 5px; }
.history-head span:last-child { color: var(--dim); }
.history-list { display: flex; flex-direction: column; gap: 4px; }
.history-list details { border: 1px solid var(--line); border-radius: var(--radius-sm); overflow: hidden; }
.history-list summary {
  display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px;
  align-items: center; padding: 7px 9px; cursor: pointer; color: var(--muted); font-size: 11px;
}
.history-list summary span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-status.approved { color: var(--ok); }
.history-status.needs_review { color: var(--warn); }
.history-status.needs_revision { color: var(--bad); }
.history-body { padding: 0 9px 9px; }
.history-note { margin: 4px 0 8px; color: var(--dim); font-size: 11px; }
.diff { background: var(--code-bg); border-radius: var(--radius-sm); padding: 7px; }
.diff-title { color: var(--dim); font-size: 10px; margin-bottom: 4px; }
.diff pre { margin: 0; white-space: pre-wrap; word-break: break-word; color: var(--muted); font: 11px/1.45 var(--mono); }
.history-unchanged { color: var(--dim); font-size: 10.5px; }
.field-foot { display: flex; justify-content: space-between; gap: 12px; margin-top: 5px; color: var(--dim); font-size: 10.5px; line-height: 1.4; }
.char-count { flex: none; font-variant-numeric: tabular-nums; }
.editor-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }

@media (max-width: 620px) {
  .field-foot { display: block; }
  .char-count { display: block; margin-top: 3px; }
  .editor-actions { justify-content: stretch; }
  .editor-actions > * { flex: 1; }
}
</style>
