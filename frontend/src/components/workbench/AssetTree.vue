<script setup>
// =====================================================================
// AssetTree.vue —— 第二列：即梦式「资产库」树
//
// 即梦左栏不是 tab 堆，而是一棵能搜的资产树：搜索框 → 角色组 → 场景组，
// 每条 = 缩略图 + 名字（缺图给占位），点条目开大图；底部「＋ 创建」跳资产库页。
// 数据 GET /projects/{pid}/library（角色带音色/定妆照，场景带图）。
// =====================================================================
import { computed, onMounted, ref, watch } from 'vue'
import AppIcon from '../AppIcon.vue'
import { api } from '../../api'
import { store, notify } from '../../stores/app'

const chars = ref([])
const locs = ref([])
const loading = ref(false)
const q = ref('')

async function refresh() {
  if (!store.pid) { chars.value = []; locs.value = []; return }
  loading.value = true
  try {
    const lib = await api(`/projects/${store.pid}/library`)
    chars.value = lib.characters || []
    locs.value = lib.locations || []
  } catch (e) {
    notify(`资产加载失败：${e.message}`, 'error')
  } finally { loading.value = false }
}
onMounted(refresh)
watch(() => store.pid, refresh)
// 资产图生成完成等信号：assetRefreshSeq 变了重拉
watch(() => store.assetRefreshSeq, refresh)

const kw = computed(() => q.value.trim().toLowerCase())
const fChars = computed(() =>
  chars.value.filter(c => !kw.value || c.name?.toLowerCase().includes(kw.value)))
const fLocs = computed(() =>
  locs.value.filter(l => !kw.value || l.name?.toLowerCase().includes(kw.value)))

function open(a) {
  if (!a) return
  store.assetPreview = {
    name: a.name, kind: a.char_id ? 'character' : 'location',
    image: a.image || '', id: a.char_id || a.loc_id || '',
  }
}
</script>

<template>
  <div class="tree">
    <div class="tree-head">
      <span class="tree-title">资产库</span>
      <button class="tree-link" type="button" @click="store.overlay = 'assets'">打开资产库 →</button>
    </div>
    <label class="tree-search">
      <AppIcon name="search" />
      <input v-model="q" type="search" placeholder="搜索角色 / 场景" aria-label="搜索资产" />
    </label>

    <div class="tree-scroll">
      <p v-if="loading" class="tree-state">加载中…</p>
      <p v-else-if="!chars.length && !locs.length" class="tree-state">
        还没有资产——在对话里说「生成定妆照」。
      </p>

      <template v-else>
        <section v-if="fChars.length" class="tree-group">
          <h4>角色 <span class="cnt">{{ fChars.length }}</span></h4>
          <button
            v-for="c in fChars" :key="c.char_id"
            class="node" type="button" @click="open(c)"
          >
            <span class="thumb" :class="{ missing: !c.image }">
              <img v-if="c.image" :src="c.image" :alt="c.name" loading="lazy" />
              <AppIcon v-else name="user" />
            </span>
            <span class="node-body">
              <span class="node-name">{{ c.name }}</span>
              <span class="node-sub">
                {{ c.voice_label || '未配音色' }}
                <em v-if="!c.has_image" class="miss-tag">缺定妆照</em>
              </span>
            </span>
          </button>
        </section>

        <section v-if="fLocs.length" class="tree-group">
          <h4>场景 <span class="cnt">{{ fLocs.length }}</span></h4>
          <button
            v-for="l in fLocs" :key="l.loc_id"
            class="node" type="button" @click="open(l)"
          >
            <span class="thumb" :class="{ missing: !l.image }">
              <img v-if="l.image" :src="l.image" :alt="l.name" loading="lazy" />
              <AppIcon v-else name="folder" />
            </span>
            <span class="node-body">
              <span class="node-name">{{ l.name }}</span>
              <span class="node-sub">
                {{ l.scenes || 0 }} 场
                <em v-if="!l.has_image" class="miss-tag">缺场景图</em>
              </span>
            </span>
          </button>
        </section>
      </template>
    </div>

    <button class="tree-create" type="button" @click="store.overlay = 'assets'">
      <AppIcon name="plus" /> 创建
    </button>
  </div>
</template>

<style scoped>
.tree { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.tree-head {
  display: flex; align-items: baseline; justify-content: space-between;
  padding: 12px 12px 8px; flex: none;
}
.tree-title { font-size: 14px; font-weight: 600; color: var(--ink); }
.tree-link {
  border: none; background: transparent; color: var(--muted);
  font-size: 11px; cursor: pointer; padding: 0;
}
.tree-link:hover { color: var(--gold); }
.tree-search {
  display: flex; align-items: center; gap: 6px;
  margin: 0 12px 8px; padding: 0 8px; height: 28px; flex: none;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--surface-2); color: var(--muted);
}
.tree-search :deep(svg) { width: 12px; height: 12px; flex: none; }
.tree-search input {
  flex: 1; min-width: 0; border: none; background: transparent; outline: none;
  color: var(--ink); font-size: 12px;
}
.tree-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 0 8px 8px; }
.tree-state { padding: 16px 8px; font-size: 12px; color: var(--dim); text-align: center; }
.tree-group { margin-bottom: 10px; }
.tree-group h4 {
  display: flex; align-items: center; gap: 6px;
  margin: 6px 4px; font-size: 11px; font-weight: 500;
  letter-spacing: .04em; color: var(--dim);
}
.cnt { font-family: var(--mono); color: var(--gold); }
.node {
  position: relative; display: flex; align-items: center; gap: 8px;
  width: 100%; padding: 5px 6px; text-align: left;
  border: none; border-radius: var(--radius-sm);
  background: transparent; cursor: pointer;
}
.node:hover { background: var(--surface-2); }
/* 按下瞬间（白 8% 底，同全站选中语言） */
.node:active { background: var(--sel-bg); }
.thumb {
  width: 34px; height: 34px; flex: none; border-radius: 6px; overflow: hidden;
  display: grid; place-items: center;
  background: var(--panel); border: 1px solid var(--line);
  color: var(--muted);
}
.thumb :deep(svg) { width: 14px; height: 14px; }
.thumb img { width: 100%; height: 100%; object-fit: cover; }
.thumb.missing { border-style: dashed; color: var(--dim); }
.node-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.node-name {
  font-size: 12.5px; color: var(--ink);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.node-sub { font-size: 10.5px; color: var(--dim); display: flex; gap: 6px; align-items: center; }
.miss-tag {
  font-style: normal; color: var(--warn);
  padding: 0 5px; border-radius: 3px;
  background: color-mix(in oklch, var(--warn) 12%, transparent);
}
.tree-create {
  display: flex; align-items: center; gap: 6px; flex: none;
  margin: 8px 12px 12px; padding: 8px 10px;
  border: 1px dashed var(--line-strong); border-radius: var(--radius-sm);
  background: transparent; color: var(--muted); font-size: 12px; cursor: pointer;
}
.tree-create:hover { color: var(--ink); border-color: var(--gold); }
.tree-create :deep(svg) { width: 12px; height: 12px; }
</style>
