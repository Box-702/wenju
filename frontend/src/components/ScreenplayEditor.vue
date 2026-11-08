<script setup>
// =====================================================================
// ScreenplayEditor.vue —— 书稿式「原位编辑」剧本编辑器（所见即所得改剧本原文）
//
// 不做成一堆输入框，而是像一部真的剧本：场景标题、动作行、居中角色名、
// 斜体提示、缩进对白，直接点文字就能改（contenteditable，无框、随内容换行）。
// 仅保留「说话人 / 地点」这两个需要从已知列表挑选的小下拉。
//
// 保存：对比编辑前后结构，生成字段级 patch 操作，POST /api/versions/{id}/apply
//       生成一个「手动编辑」新版本（复用 patch 引擎，数据仍是结构化剧本）。
// =====================================================================

import { ref, computed, watch } from 'vue'
import EditableText from './EditableText.vue'
import AppIcon from './AppIcon.vue'
import { store, applyEdits, notify } from '../stores/app'

const props = defineProps({
  script: { type: Object, required: true },
  versionId: { type: String, required: true },
  // standalone=true：作为工作台默认视图常驻（隐藏「取消」按钮，保存后停留在编辑态）
  standalone: { type: Boolean, default: false },
})
const emit = defineEmits(['saved', 'cancel'])

// 本地可编辑副本（深拷贝）
const local = ref(JSON.parse(JSON.stringify(props.script)))
const saving = ref(false)
const editErr = ref('')
const dirty = ref(false)    // 有未保存的改动
const autoSaveStatus = ref('')  // '' | 'saving' | 'saved'

// 流式生成 / 接受改编 / 手动保存都会刷新 viewerScript 与 versionId
watch(
  () => [props.versionId, props.script],
  () => { local.value = JSON.parse(JSON.stringify(props.script)); dirty.value = false },
)

// 自动保存：改动后 3 秒无新编辑则自动保存
let autoSaveTimer = null
watch(local, () => {
  dirty.value = true
  autoSaveStatus.value = ''
  clearTimeout(autoSaveTimer)
  autoSaveTimer = setTimeout(autoSave, 3000)
}, { deep: true })

async function autoSave() {
  if (!dirty.value || saving.value) return
  autoSaveStatus.value = 'saving'
  try {
    await doSave()
    autoSaveStatus.value = 'saved'
    setTimeout(() => { autoSaveStatus.value = '' }, 2000)
  } catch {
    autoSaveStatus.value = ''
  }
}

const locs = computed(() => (local.value.locations || []).reduce((m, l) => ((m[l.id] = l.name), m), {}))
const chars = computed(() => (local.value.characters || []).reduce((m, c) => ((m[c.id] = c.name), m), {}))

function nextBeatId(beats) {
  let mx = 0
  for (const b of beats) { const m = /^beat_(\d+)$/.exec(b.id || ''); if (m) mx = Math.max(mx, parseInt(m[1], 10)) }
  let cand
  do { cand = `beat_${String(mx + 1).padStart(3, '0')}`; mx++ } while (beats.some((b) => b.id === cand))
  return cand
}

function addBeat(scene, kind) {
  const beats = scene.beats || (scene.beats = [])
  if (kind === 'dialogue') {
    beats.push({ id: nextBeatId(beats), type: 'dialogue', speaker: (scene.characters || [])[0] || (local.value.characters || [])[0]?.id || '', line: '', emotion: '' })
  } else if (kind === 'cue') {
    beats.push({ id: nextBeatId(beats), type: 'cue', text: '' })
  } else {
    beats.push({ id: nextBeatId(beats), type: 'action', text: '' })
  }
}
function removeBeat(scene, i) { (scene.beats || []).splice(i, 1) }

function buildOps(orig, edited) {
  const ops = []
  const os = orig?.scenes || []
  const es = edited?.scenes || []
  const n = Math.max(os.length, es.length)
  for (let i = 0; i < n; i++) {
    const o = os[i], e = es[i]
    if (!e) continue
    for (const f of ['title', 'purpose', 'conflict', 'entry_state', 'exit_state', 'time', 'location_id']) {
      const ov = o?.[f] ?? null, ev = e[f] ?? null
      if (ov !== ev) ops.push({ op: 'set', path: `/script/scenes/${i}/${f}`, value: ev })
    }
    const ob = (o?.beats || []), eb = (e?.beats || [])
    const obById = {}; for (const b of ob) obById[b.id] = b
    const ebIds = new Set(eb.map((b) => b.id))
    for (const b of eb) {
      const bv = b
      if (!obById[b.id]) ops.push({ op: 'add', path: `/script/scenes/${i}/beats/${b.id}`, value: bv })
      else if (JSON.stringify(obById[b.id]) !== JSON.stringify(bv)) ops.push({ op: 'set', path: `/script/scenes/${i}/beats/${b.id}`, value: bv })
    }
    for (const b of ob) if (!ebIds.has(b.id)) ops.push({ op: 'remove', path: `/script/scenes/${i}/beats/${b.id}` })
  }
  return ops
}

async function doSave() {
  if (saving.value) return
  editErr.value = ''
  const draft = JSON.parse(JSON.stringify(local.value))
  for (const sc of draft.scenes || []) {
    sc.beats = (sc.beats || []).filter((b) => {
      if (b.type === 'dialogue') return (b.line || '').trim()
      return (b.text || '').trim()
    })
  }
  const ops = buildOps(props.script, draft)
  if (!ops.length) { editErr.value = '没有改动'; return }
  saving.value = true
  try {
    await applyEdits(props.versionId, ops)
    dirty.value = false
    notify('已保存为新版本', 'ok')
    emit('saved')
  } catch (e) { editErr.value = e.message } finally { saving.value = false }
}

function save() { doSave() }
function cancel() { emit('cancel') }
</script>

<template>
  <div class="ed">
    <!-- 顶部工具条 -->
    <div class="ed-bar">
      <span class="ed-title">{{ standalone ? '剧本' : '编辑剧本' }}</span>
      <span v-if="autoSaveStatus === 'saving'" class="ed-autosave">自动保存中…</span>
      <span v-else-if="autoSaveStatus === 'saved'" class="ed-autosave ed-saved">✓ 已自动保存</span>
      <span v-else-if="dirty" class="ed-autosave ed-dirty">● 未保存</span>
      <span class="ed-hint">点击文字即可修改 · 自动保存为新版本</span>
      <span class="spacer"></span>
      <button v-if="!standalone" class="ghost small" :disabled="saving" @click="cancel">取消</button>
      <button class="small" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存为新版本' }}</button>
    </div>
    <div v-if="editErr" class="ed-err"><AppIcon name="warning" /> {{ editErr }}</div>

    <div class="ed-body">
      <div
        v-for="(sc, si) in local.scenes || []"
        :key="sc.id || si"
        class="ed-scene"
      >
        <!-- 场景标题行 -->
        <div class="ed-heading">
          <span class="ed-num">{{ String(si + 1).padStart(2, '0') }}</span>
          <EditableText class="ed-title" :model-value="sc.title" ph="场景标题" single-line @update:model-value="v => sc.title = v" />
          <select v-model="sc.location_id" class="ed-loc" title="地点">
            <option value="" disabled>地点</option>
            <option v-for="(name, lid) in locs" :key="lid" :value="lid">{{ name }}</option>
          </select>
          <EditableText class="ed-time" :model-value="sc.time" ph="时间" single-line @update:model-value="v => sc.time = v" />
        </div>

        <!-- 场景目的 / 冲突（动作行，可改） -->
        <div class="ed-action"><EditableText :model-value="sc.purpose" ph="场景目的（动作）" @update:model-value="v => sc.purpose = v" /></div>
        <div class="ed-action"><EditableText :model-value="sc.conflict" ph="场景冲突" @update:model-value="v => sc.conflict = v" /></div>

        <!-- 节拍：书稿式排版，点文字即改 -->
        <div v-for="(b, bi) in sc.beats || []" :key="b.id" class="ed-row" :class="'bt-' + b.type">
          <!-- 对白 -->
          <template v-if="b.type === 'dialogue'">
            <div class="ed-speaker">
              <select v-model="b.speaker" class="ed-spk" title="说话人">
                <option value="" disabled>角色</option>
                <option v-for="(name, cid) in chars" :key="cid" :value="cid">{{ name }}</option>
              </select>
              <EditableText v-if="b.emotion" class="ed-emotion" :model-value="b.emotion" ph="" single-line @update:model-value="v => b.emotion = v" />
            </div>
            <div class="ed-dialogue"><EditableText :model-value="b.line" ph="台词" @update:model-value="v => b.line = v" /></div>
          </template>
          <!-- 舞台提示 -->
          <div v-else-if="b.type === 'cue'" class="ed-cue"><EditableText :model-value="b.text" ph="（舞台提示）" @update:model-value="v => b.text = v" /></div>
          <!-- 动作 -->
          <div v-else class="ed-action"><EditableText :model-value="b.text" ph="动作描写" @update:model-value="v => b.text = v" /></div>

          <button class="mini del" title="删除该节拍" @click="removeBeat(sc, bi)"><AppIcon name="close" /></button>
        </div>

        <div v-if="!sc.beats || !sc.beats.length" class="ed-beats-empty">本场景还没有节拍，点击下方添加。</div>
        <div class="ed-add">
          <button class="mini" @click="addBeat(sc, 'action')"><AppIcon name="plus" /> 动作</button>
          <button class="mini" @click="addBeat(sc, 'dialogue')"><AppIcon name="plus" /> 对白</button>
          <button class="mini" @click="addBeat(sc, 'cue')"><AppIcon name="plus" /> 提示</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ed { display: flex; flex-direction: column; min-height: 0; }
.ed-bar { display: flex; align-items: center; gap: 10px; padding: 8px 16px; border-bottom: 1px solid var(--line); }
.ed-title { font-weight: 700; font-size: 13px; }
.ed-hint { font-size: 11px; color: var(--dim); }
.spacer { flex: 1; }
.ed-err { color: var(--bad); font-size: 12px; padding: 8px 16px 0; }

/* 正文：书稿列，左留白给悬停的删除 ✕，右留白避免贴边 */
.ed-body { flex: 1; overflow-y: auto; padding: 24px 30px 30px 38px; }
.ed-scene { margin-bottom: 26px; }
.ed-scene:last-child { margin-bottom: 8px; }

/* 场景标题：左侧色条 + 加粗，同书稿 */
.ed-heading {
  font-weight: 700; color: var(--ink); letter-spacing: 0.02em;
  border-bottom: 1px solid color-mix(in oklch, var(--dlg) 55%, var(--line));
  padding-bottom: 7px; margin-bottom: 12px;
  display: flex; align-items: baseline; gap: 8px;
}
.ed-num { font-size: 11px; color: var(--dim); font-family: var(--mono); font-variant-numeric: tabular-nums; min-width: 18px; }
.ed-title { flex: 1; }
.ed-autosave { font-size: 11px; color: var(--dim); flex: none; }
.ed-saved { color: var(--ok); }
.ed-dirty { color: var(--amber); }
.ed-time { color: var(--ink); }

/* 动作行 */
.ed-action { margin: 8px 0; max-width: 62ch; line-height: 1.7; }

/* 对白：角色名居中，情绪斜体，台词缩进 */
.ed-speaker { text-align: center; font-weight: 700; letter-spacing: 0.04em; margin-top: 12px; display: flex; justify-content: center; gap: 6px; }
.ed-emotion { color: var(--muted); font-style: italic; font-size: 12.5px; font-weight: 400; }
.ed-dialogue { margin: 4px 0 2px; padding-left: 12%; max-width: 48ch; line-height: 1.7; }
.ed-cue { text-align: center; color: var(--muted); font-style: italic; font-size: 12.5px; margin: 2px 0; line-height: 1.7; }

/* 说话人 / 地点：极简下拉，只在需要时显得像控件 */
select.ed-loc, select.ed-spk {
  appearance: none; -webkit-appearance: none; background: transparent; border: none;
  color: var(--muted); font: inherit; padding: 0 2px; border-radius: 4px; cursor: pointer; max-width: 140px;
}
select.ed-loc:hover, select.ed-spk:hover { background: color-mix(in oklch, var(--ink) 5%, transparent); }
select.ed-loc:focus, select.ed-spk:focus { outline: none; box-shadow: 0 0 0 1px color-mix(in oklch, var(--accent) 30%, transparent); }

/* 删除 ✕：悬停该节拍才出现，贴在左侧留白，不挤正文 */
.ed-row { position: relative; border-radius: var(--radius-sm); transition: background var(--dur) var(--ease); }
.ed-row:hover { background: color-mix(in oklch, var(--ink) 4%, transparent); }
.ed-row .mini.del { position: absolute; left: -24px; top: 6px; opacity: 0; background: transparent; border: none; color: var(--dim); padding: 0 4px; font-size: 11px; }
.ed-row:hover .mini.del { opacity: 1; }
.ed-row .mini.del:hover { color: var(--bad); opacity: 1; }

.ed-beats-empty { color: var(--dim); font-size: 12px; margin: 10px 0 4px; }
.ed-add { display: flex; gap: 4px; margin: 4px 0 0; }
.mini { background: transparent; border: none; color: var(--muted); padding: 2px 6px; font-size: 11px;
  font-weight: 500; line-height: 1.6; cursor: pointer; border-radius: var(--radius-sm);
  transition: color var(--dur) var(--ease), background var(--dur) var(--ease); }
.mini:hover { color: var(--ink); background: color-mix(in oklch, var(--ink) 5%, transparent); }
.ed-add .mini { color: var(--muted); }
.ed-add .mini:hover { color: var(--ink); background: color-mix(in oklch, var(--ink) 5%, transparent); }
</style>
