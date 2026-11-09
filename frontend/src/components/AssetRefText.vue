<script setup>
// =====================================================================
// AssetRefText.vue —— 把提示词里的 @资产引用 渲染成「小图 + 名字」
//
// 为什么：提示词里的 `@林然(char_005)` 是一串编号，用户看不出那是哪位、
// 长什么样；而资产库（定妆照 / 场景图）就是**一致性锚**——把锚摆在文字旁边，
// 一眼就能核对「这一镜用的就是这张定妆照」，点一下还能放大比对。
//
// 资产来源：store.assetRefs（{ char_xxx / loc_xxx → { name, image, kind } }）
// =====================================================================
import { computed } from 'vue'
import { store } from '../stores/app'

const props = defineProps({
  /** 提示词原文 */
  text: { type: String, default: '' },
})

// 匹配 @名字(编号)：编号形如 char_005 / loc_001
const PATTERN = /@([^@\s，。；;、（）()]{0,20})\(([a-z]+_[a-z0-9_]+)\)/gi

const parts = computed(() => {
  const raw = props.text || ''
  const out = []
  let last = 0
  for (const m of raw.matchAll(PATTERN)) {
    const start = m.index ?? 0
    if (start > last) out.push({ raw: raw.slice(last, start), asset: null })
    const id = m[2]
    const asset = (store.assetRefs || {})[id] || null
    out.push({ raw: m[0], asset: asset ? { ...asset, id } : null })
    last = start + m[0].length
  }
  if (last < raw.length) out.push({ raw: raw.slice(last), asset: null })
  return out
})

function preview(asset) {
  if (!asset) return
  store.assetPreview = asset
}
</script>

<template>
  <span class="ref-text">
    <template v-for="(p, i) in parts" :key="i">
      <button
        v-if="p.asset"
        class="ref-chip"
        type="button"
        :title="`${p.asset.name}（${p.asset.id}）· 点开预览`"
        @click.stop="preview(p.asset)"
      >
        <img v-if="p.asset.image" class="rc-thumb" :src="p.asset.image" :alt="p.asset.name" />
        <span v-else class="rc-thumb rc-none" aria-hidden="true">◻</span>
        <span class="rc-name">{{ p.asset.name || p.raw }}</span>
      </button>
      <template v-else>{{ p.raw }}</template>
    </template>
  </span>
</template>

<style scoped>
/* @引用：文字里内嵌一张小图（小图本身就是资产库里的定妆照/场景图） */
.ref-chip {
  display: inline-flex; align-items: center; gap: 4px;
  margin: 0 1px; padding: 1px 6px 1px 2px;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, .04);
  color: var(--ink); font: inherit; font-size: .95em;
  cursor: pointer; vertical-align: middle;
  transition: border-color 130ms var(--ease), background 130ms var(--ease);
}
.ref-chip:hover { border-color: var(--accent); background: rgba(255, 255, 255, .09); }
.rc-thumb {
  width: 16px; height: 16px; border-radius: 3px;
  object-fit: cover; flex: none; background: rgba(0, 0, 0, .3);
}
.rc-none {
  display: inline-grid; place-items: center;
  font-size: 9px; color: var(--dim);
}
.rc-name { white-space: nowrap; }
</style>
