<script setup>
// =====================================================================
// RolesView.vue —— 工作台·角色页（对标即梦资产库）
//
// 主体直接复用 AssetPanel（section="assets"）：角色定妆照 / 场景图 /
// 音色试听 / 克隆 / 缺口徽标 / 演员库都在它里面。本页只加一层页头
// （标题 + 概览计数 + 音色规范说明）和「没选作品」的空态引导。
// =====================================================================
import { computed, onMounted, watch } from 'vue'
import AssetPanel from '../AssetPanel.vue'
import { store, loadOverview } from '../../stores/app'

/** 副标题：角色数 / 定妆图数，数字全部来自后端 overview；为 null 时显示空。 */
const subtitle = computed(() => {
  const o = store.overview
  if (!o) return ''
  return `${o.characters ?? 0} 个角色 · ${o.reference_images ?? 0} 张定妆图`
})

function refreshOverview() {
  if (store.pid) loadOverview()
}

onMounted(refreshOverview)
watch(() => store.pid, refreshOverview)
</script>

<template>
  <div class="roles-view">
    <header class="roles-head">
      <h2 class="title">角色</h2>
      <p v-if="subtitle" class="subtitle">{{ subtitle }}</p>
      <p class="voice-note">
        每个角色一把嗓子：在对话里说「给 X 指定音色 Y」，出片的人声会向它靠。
      </p>
    </header>

    <!-- 主体：复用资产库面板（它自己在内部滚动） -->
    <div v-if="store.pid" class="panel-host">
      <AssetPanel section="assets" />
    </div>

    <!-- 没选作品：空态引导 -->
    <div v-else class="empty">
      <p class="empty-title">还没有进入作品</p>
      <p class="empty-sub">回到对话里选一部作品（或新建一部），角色定妆照和音色都会在这里出现。</p>
    </div>
  </div>
</template>

<style scoped>
.roles-view {
  display: flex; flex-direction: column;
  flex: 1; min-height: 0; height: 100%;
  overflow: hidden;
}
.roles-head {
  flex: none;
  padding: var(--space-4) var(--space-5) var(--space-3);
  border-bottom: 1px solid color-mix(in oklch, var(--line) 55%, transparent);
}
.title {
  margin: 0; font-size: var(--fs-xl); font-weight: 650; color: var(--ink);
}
.subtitle {
  margin: var(--space-1) 0 0;
  font-size: var(--fs-sm); color: var(--muted);
}
.voice-note {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs); color: var(--dim); line-height: 1.6;
}
.panel-host {
  flex: 1; min-height: 0;
  display: flex; flex-direction: column;
  padding: 0 var(--space-5);
  overflow: hidden;
}
.empty {
  flex: 1; min-height: 0;
  display: grid; place-content: center; justify-items: center;
  gap: var(--space-2);
  padding: var(--space-6);
  text-align: center;
}
.empty-title { margin: 0; font-size: var(--fs-lg); color: var(--muted); }
.empty-sub { margin: 0; font-size: var(--fs-sm); color: var(--dim); line-height: 1.6; max-width: 34em; }
</style>
