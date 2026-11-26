<script setup>
// =====================================================================
// ScriptHistoryPanel.vue —— 剧本页左侧「脚本修改历史」
//
// 每条 = 人话描述 + 版本号 + 时间，点开看说明；当前版本高亮（金色竖条）。
// 标题栏右侧面板图标可收起（2026-11-26 用户要求），收起后由 App.vue 在
// 主区左缘显示竖排「修改历史」标签恢复；开合状态记在 localStorage。
// =====================================================================
import { computed } from 'vue'
import AppIcon from '../AppIcon.vue'
import { store } from '../../stores/app'

const emit = defineEmits(['collapse'])

// 实际后端取值：generation | agent_adaptation | import | manual_edit
const SOURCE_LABELS = {
  generation: '生成',
  manual: '手改', manual_edit: '手改',
  agent: '改编', agent_adaptation: '改编',
  import: '导入',
}
function sourceLabel(t) {
  if (!t) return '生成'
  return SOURCE_LABELS[t] || t
}

function fmtMinute(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d)) return String(iso)
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

const versions = computed(() => store.versions || [])
</script>

<template>
  <div class="hist-panel">
    <div class="hp-head">
      <span class="hp-title">脚本修改历史</span>
      <span class="hp-cnt">{{ versions.length }}</span>
      <button class="hp-collapse" type="button" title="收起修改历史" @click="emit('collapse')">
        <AppIcon name="panel" />
      </button>
    </div>
    <div class="hp-scroll">
      <p v-if="!versions.length" class="hp-empty">
        还没有版本记录——生成或改编一次剧本后，这里会留下每一次修改。
      </p>
      <details v-for="(v, i) in versions" :key="v.id" class="hp-row" :class="{ on: i === 0 }">
        <summary class="hp-sum">
          <span class="hp-desc">{{ v.label || '（未命名版本）' }}</span>
          <span class="hp-meta">
            <span class="hp-badge">{{ sourceLabel(v.source_type) }}</span>
            版本 {{ String(v.id).slice(-6) }} · {{ fmtMinute(v.created_at) }}
          </span>
        </summary>
        <p class="hp-notes">{{ v.notes || '这一版没有留下说明' }}</p>
      </details>
    </div>
    <div class="hp-foot">
      <AppIcon name="info" />
      <span>最新一版即当前生效的剧本</span>
    </div>
  </div>
</template>

<style scoped>
.hist-panel { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.hp-head {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 12px 8px; flex: none;
}
.hp-title { font-size: 14px; font-weight: 600; color: var(--ink); }
.hp-cnt {
  font-family: var(--mono); font-size: 10px; color: var(--gold);
  padding: 1px 6px; border-radius: 3px;
  border: 1px solid color-mix(in oklch, var(--gold) 40%, transparent);
}
.hp-collapse {
  margin-left: auto; width: 24px; height: 24px;
  display: grid; place-items: center; flex: none;
  border: none; border-radius: 5px; background: transparent;
  color: var(--dim); cursor: pointer;
}
.hp-collapse:hover { color: var(--ink); background: var(--surface-2); }
.hp-collapse :deep(svg) { width: 13px; height: 13px; }
.hp-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 0 8px 8px; }
.hp-empty { padding: 16px 8px; font-size: 12px; color: var(--dim); text-align: center; line-height: 1.7; }
.hp-row {
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--surface); margin-bottom: 6px; overflow: hidden;
}
.hp-row.on {
  border-color: var(--sel-line);
  background: var(--sel-bg);
  box-shadow: inset 2px 0 0 0 var(--sel-bar);
}
.hp-sum {
  display: flex; flex-direction: column; gap: 3px;
  padding: 8px 10px; cursor: pointer; list-style: none;
}
.hp-sum::-webkit-details-marker { display: none; }
.hp-sum:hover { background: var(--surface-2); }
.hp-desc { font-size: 12px; color: var(--ink); line-height: 1.5; }
.hp-meta { font-size: 10.5px; color: var(--dim); display: flex; align-items: center; gap: 6px; }
.hp-badge {
  font-style: normal; color: var(--muted);
  padding: 0 5px; border-radius: 3px;
  border: 1px solid var(--line);
}
.hp-notes {
  margin: 0; padding: 8px 10px; border-top: 1px dashed var(--line);
  font-size: 11.5px; color: var(--muted); line-height: 1.6;
}
.hp-foot {
  display: flex; align-items: center; gap: 6px; flex: none;
  padding: 8px 12px; border-top: 1px solid var(--line);
  font-size: 10.5px; color: var(--dim);
}
.hp-foot :deep(svg) { width: 11px; height: 11px; flex: none; }
</style>
