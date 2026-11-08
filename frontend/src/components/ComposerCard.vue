<script setup>
// =====================================================================
// ComposerCard.vue —— 输入卡片（首页与对话页共用同一套）
//
// 形制（照参考图）：
//   ┌──────────────────────────────────────────────┐
//   │ [+]  描述你想生成的内容，或点 ＋ 附上图片…       │
//   │                                                │
//   │ [模型 ▾][Skill]                      [⚙]  ( ➤ )   │
//   └──────────────────────────────────────────────┘
//
// 三个胶囊各司其职：
//   模型  —— 选哪个模型对话（直接切换，不进设置页）
//   Skill —— 打开技能页（选技能 / 导入新技能）
// =====================================================================

import { computed, nextTick, ref } from 'vue'
import AppIcon from './AppIcon.vue'

const text = defineModel('text', { type: String, default: '' })

const props = defineProps({
  placeholder: {
    type: String,
    default: '描述你想生成的内容，或点 ＋ 附上图片。',
  },
  /** 模型名（胶囊） */
  modelLabel: { type: String, default: '模型' },
  /** 阶段状态（生成中），显示在胶囊行左侧 */
  statusText: { type: String, default: '' },
  /** 上下文提示（只在有提示时显示） */
  hintText: { type: String, default: '' },
  canSend: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  /** 可被 @ 引用的资产 [{ kind, id, name, sub }] */
  assets: { type: Array, default: () => [] },
})

const emit = defineEmits([
  'send', 'pick-menu', 'open-settings', 'pick-model', 'open-skills', 'asset-picked',
])

const menuOpen = ref(false)

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
}

function onKeydown(e) {
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
                :class="['cc-mention-item', { active: i === mentionIndex }]"
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
          <span v-if="statusText" class="cc-status">
            <span class="cc-pulse" aria-hidden="true"></span>{{ statusText }}
          </span>
          <button class="chip" type="button" @click.stop="emit('pick-model')">
            <AppIcon name="sparkle" />
            <span>{{ modelLabel }}</span>
            <AppIcon name="chevron-down" class="cc-chev" />
          </button>
          <button class="chip" type="button" @click.stop="emit('open-skills')">
            <AppIcon name="folder" />
            <span>Skill</span>
          </button>
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
  width: 48px;
  height: 48px;
  border-radius: var(--radius-lg);
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

.cc-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 550;
  white-space: nowrap;
}

.cc-pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  animation: ccpulse 1.4s ease-in-out infinite;
}

@keyframes ccpulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
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
.cc-mention-item {
  display: flex; align-items: baseline; gap: 7px; width: 100%;
  padding: 6px 9px; border-radius: var(--radius-md);
  background: transparent; border: none; color: var(--muted);
  font-size: var(--fs-sm); font-family: inherit; text-align: left; cursor: pointer;
}
.cc-mention-item:hover, .cc-mention-item.active {
  background: rgba(255, 255, 255, .08); color: var(--ink);
}
.mi-name { color: var(--ink); font-weight: 500; flex: none; }
.mi-sub { font-size: 10px; color: var(--dim); flex: none; }
.mi-id { margin-left: auto; font-family: var(--mono); font-size: 10px; color: var(--dim); }

.cc-gear:hover { color: var(--ink); background: rgba(255, 255, 255, .07); }
.cc-gear .app-ic { width: 15px; height: 15px; }

/* 发送：圆形箭头钮。
   可用状态用**亮色**（亮绿渐变 + 外发光）—— 之前那版 #22c55e 收尾的深绿
   在深色极光背景上偏暗，用户还是一眼看不出「现在可以点了」。
   现在从亮薄荷绿到亮绿，并在钮外加一圈同色辉光，暗底上一眼就跳出来。 */
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
  background: linear-gradient(135deg, #d9f99d 0%, #a3e635 40%, #4ade80 100%);
  border-color: rgba(217, 249, 157, .55);
  color: #052e16;
  cursor: pointer;
  box-shadow: 0 0 0 4px rgba(163, 230, 53, .16),
              0 8px 22px rgba(74, 222, 128, .5);
}
.cc-send:not(:disabled):hover {
  filter: brightness(1.06);
  box-shadow: 0 0 0 5px rgba(163, 230, 53, .22),
              0 10px 26px rgba(74, 222, 128, .62);
}
.cc-send:not(:disabled):active { filter: brightness(.97); }
.cc-send .app-ic { width: 16px; height: 16px; transform: translateY(-0.5px); }

.ccpop-enter-active,
.ccpop-leave-active { transition: opacity 0.14s ease, transform 0.14s ease; }
.ccpop-enter-from,
.ccpop-leave-to { opacity: 0; transform: translateY(6px); }
</style>
