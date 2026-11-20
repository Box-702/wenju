<script setup>
// =====================================================================
// OverviewView.vue —— 作品总览（即梦式主区默认页：进度 + 四个入口）
//
// wbView=null 时主区渲染它：一眼看到「走到哪了」，点卡片进四视图。
// 数字全部来自后端 overview（只聚合已落库的，D-53 不出现费用）。
// =====================================================================
import { computed, onMounted } from 'vue'
import AppIcon from '../AppIcon.vue'
import { store, loadOverview } from '../../stores/app'

const ov = computed(() => store.overview)
const project = computed(() => store.projects.find(p => p.id === store.pid))
const stages = computed(() => ov.value?.progress_stages || [])
const pct = computed(() => ov.value?.progress_pct ?? 0)

const JUMPS = [
  { key: 'script', label: '剧本', icon: 'file', desc: '设定 · 分集大纲 · 正文 · 修改历史' },
  { key: 'clips', label: '分镜', icon: 'film', desc: '片段编辑器 · 逐镜微调与重出' },
  { key: 'roles', label: '角色', icon: 'user', desc: '定妆照 · 音色 · 场景图' },
  { key: 'episodes', label: '分集视频', icon: 'panel', desc: '出片进度 · 成片合成' },
]

onMounted(() => { if (!store.overview) loadOverview() })
</script>

<template>
  <div class="ov">
    <div class="ov-head">
      <h2>{{ project?.title || '作品总览' }}</h2>
      <p class="ov-sub">
        <template v-if="ov">
          {{ ov.scenes }} 场 · {{ ov.characters }} 角色 · {{ ov.episodes }} 集 ·
          {{ ov.shots }} 镜（已批 {{ ov.shots_approved }} · 成片 {{ ov.shots_done }}）
        </template>
        <template v-else>加载中…</template>
      </p>
    </div>

    <div v-if="stages.length" class="ov-progress">
      <div class="bar"><div class="bar-fill" :style="{ width: pct + '%' }" /></div>
      <div class="stage-row">
        <span v-for="st in stages" :key="st.key" class="stage-chip" :class="{ done: st.done }">
          <AppIcon :name="st.done ? 'check' : 'minus'" /> {{ st.label }}
        </span>
      </div>
    </div>

    <div class="ov-grid">
      <button v-for="j in JUMPS" :key="j.key" class="ov-card" @click="store.wbView = j.key">
        <span class="ov-ic"><AppIcon :name="j.icon" /></span>
        <span class="ov-label">{{ j.label }}</span>
        <span class="ov-desc">{{ j.desc }}</span>
      </button>
    </div>

    <p v-if="ov && !ov.scenes" class="ov-hint">
      还没有剧本——在右侧对话里说「读原文做分集提案」，或直接「生成初稿」。
    </p>
    <p v-else-if="!store.agentOpen" class="ov-hint">
      对话面板已收起——点左栏底部「对话」随时回来。
    </p>
  </div>
</template>

<style scoped>
.ov { height: 100%; overflow-y: auto; padding: var(--space-6) var(--space-5); }
.ov-head h2 { margin: 0 0 4px; font-size: var(--fs-display); font-weight: 600; color: var(--ink); }
.ov-sub { margin: 0 0 var(--space-5); color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; }
.ov-progress { margin-bottom: var(--space-5); }
.bar { height: 4px; border-radius: 2px; background: var(--surface-2); overflow: hidden; }
.bar-fill { height: 100%; background: var(--gold); transition: width 300ms var(--ease); }
.stage-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.stage-chip {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11.5px; color: var(--dim);
  padding: 3px 9px; border-radius: var(--radius-full);
  border: 1px solid var(--line); background: transparent;
}
.stage-chip.done { color: var(--ok); border-color: color-mix(in oklch, var(--ok) 40%, transparent); }
.stage-chip :deep(svg) { width: 10px; height: 10px; }
.ov-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: var(--space-3); }
.ov-card {
  display: flex; flex-direction: column; align-items: flex-start; gap: 8px;
  padding: var(--space-4); text-align: left;
  border: 1px solid var(--line); border-radius: var(--radius-card);
  background: var(--surface); color: var(--ink); cursor: pointer;
  transition: border-color var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.ov-card:hover { border-color: var(--gold); transform: translateY(-2px); }
.ov-ic {
  display: grid; place-items: center; width: 34px; height: 34px;
  border-radius: var(--radius-sm); background: var(--surface-2); color: var(--gold);
}
.ov-ic :deep(svg) { width: 16px; height: 16px; }
.ov-label { font-size: 14px; font-weight: 600; }
.ov-desc { font-size: 11.5px; color: var(--muted); line-height: 1.5; }
.ov-hint { margin-top: var(--space-5); color: var(--dim); font-size: 12px; }
</style>
