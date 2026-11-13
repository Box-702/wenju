<script setup>
// =====================================================================
// HomePage.vue —— 首页
//
// 结构：极光背景（多层模糊色斑）+ 居中 hero（标语 + 主输入卡片）+ 我的作品。
// 主输入卡片见 HomeComposer.vue。
// 点作品卡 = 进入工作区（store.mode 切到 workspace，三栏布局接管渲染）。
// =====================================================================
import { computed, ref } from 'vue'
import { store, selectProject, deleteProject, notify, openWorkspaceFolder } from '../stores/app.js'
import AppIcon from './AppIcon.vue'
import HomeComposer from './HomeComposer.vue'

/** 作品栏：按**最近使用**排序（最近聊过/跑过/出版本的那部排最前），
    没有使用记录的新作品退回按创建时间排。 */
const works = computed(() =>
  [...(store.projects || [])].sort((a, b) => {
    const at = a.last_used_at || a.created_at || ''
    const bt = b.last_used_at || b.created_at || ''
    return bt.localeCompare(at)
  }),
)

/** 入口引导（D-47）：点击只把引导语放进输入框——入口是糖，对话是本体。
    旧版还同时切「产出目标」并在发送时拼【小说改编】前缀，前缀链已整拆
    （污染用户原文 + Agent 误读意图，见 HomeComposer 注释 2026-11-13）。 */
function enter(pid) {
  selectProject(pid)
}

const composer = ref(null)

function chip(kind) {
  if (kind === 'novel') {
    composer.value?.setDraft('我想把一部小说改编成短剧，这是原文：')
  } else if (kind === 'prompt') {
    composer.value?.setDraft('按这段成品分镜提示词直接生成视频：\n')
  }
}

function onPickLibrary(kind) {
  if (!store.pid) {
    store.showNewProject = true
    notify('先新建作品，再挂载素材', 'ok')
    return
  }
  // 有作品：进工作区后在左栏资产面板里挑
  selectProject(store.pid)
}

async function remove(p) {
  if (!window.confirm(`删除作品《${p.title}》？该操作不可撤销。`)) return
  try {
    await deleteProject(p.id)
  } catch (e) {
    notify(`删除失败：${e.message}`)
  }
}

function formatDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`
}
</script>

<template>
  <div class="home">
    <!-- 极光背景：四团大面积模糊色斑，溢出到视口外 -->
    <div class="aurora" aria-hidden="true">
      <span class="blob blob-blue"></span>
      <span class="blob blob-violet"></span>
      <span class="blob blob-red"></span>
      <span class="blob blob-teal"></span>
      <span class="grain"></span>
    </div>

    <!-- 顶栏：极简，只有品牌与设置 -->
    <header class="home-top">
      <div class="home-brand">
        <span class="brand-name">问剧</span>
      </div>
      <!-- 只留设置：新建作品在下面的「我的作品」里有卡片入口，顶部再放一个是重复 -->
      <div class="home-top-actions">
        <button class="ghost small" title="设置" @click="store.showSettings = true">
          <AppIcon name="gear" />
        </button>
      </div>
    </header>

    <!-- 主体：标语 + 主输入卡片 -->
    <main class="home-stage">
      <h1 class="hero-title"><em>You call the drama</em></h1>
      <p class="hero-sub">小说进，短剧出</p>

      <!-- 入口指引（D-47）：一行淡字，/ 分隔；点击只把引导语放进输入框，一切仍走对话 -->
      <p class="hero-hints">
        <button class="hint" @click="chip('novel')">导入小说做剧</button>
        <span class="hint-sep" aria-hidden="true">/</span>
        <button class="hint" @click="chip('prompt')">按提示词直接生成</button>
      </p>

      <HomeComposer
        ref="composer"
        @pick-library="onPickLibrary"
        @open-skills="store.overlay = 'skills'"
      />
    </main>

    <!-- 我的作品：折线以下 -->
    <section class="home-works" aria-label="我的作品">
      <div class="works-head">
        <button class="works-main" title="查看全部作品" @click="store.overlay = 'tasks'">
          <span class="works-title">我的作品</span>
          <span class="works-count">{{ works.length ? `${works.length} 部` : '' }}</span>
        </button>
        <button
          v-if="store.workspace?.configured"
          class="works-open" title="在文件管理器里打开工作区目录（剧本与成片落盘处）"
          @click="openWorkspaceFolder('')"
        >打开工作区文件夹</button>
        <button class="works-more" title="查看全部作品" @click="store.overlay = 'tasks'">全部作品<AppIcon name="chevron-right" /></button>
      </div>

      <div class="works-grid">
        <button class="work-card new-card" @click="store.showNewProject = true">
          <span class="new-plus" aria-hidden="true"><AppIcon name="plus" /></span>
          <span class="new-text">新建作品</span>
          <span class="new-sub">导入小说原文</span>
        </button>

        <article v-for="p in works" :key="p.id" class="work-card" @click="enter(p.id)">
          <div class="work-cover" aria-hidden="true">
            <span class="work-glyph">{{ (p.title || '问')[0] }}</span>
          </div>
          <div class="work-body">
            <h3 class="work-title">{{ p.title }}</h3>
            <p class="work-meta">
              {{ formatDate(p.created_at) }} · 剧本 {{ p.version_count || 0 }} 版 · 改编 {{ p.run_count || 0 }} 次
            </p>
          </div>
          <button
            class="work-del" title="删除作品" aria-label="删除作品"
            @click.stop="remove(p)"
          ><AppIcon name="close" /></button>
        </article>
      </div>

      <p v-if="!works.length" class="works-empty">
        还没有作品。新建一部，导入小说原文，一分钟内看到第一版剧本。
      </p>
    </section>
  </div>
</template>

<style scoped>
.home {
  position: relative;
  min-height: 100vh;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  background: #08080c;
  color: #fff;
}

/* ---------- 极光背景 ---------- */
.aurora {
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  overflow: hidden;
}

.blob {
  position: absolute;
  border-radius: 50%;
  filter: blur(110px);
  opacity: 0.92;
}

/* 四团光斑铺满视口边缘：左上冷蓝、右上紫品红、右下赤红、左下暗青 */
.blob-blue {
  width: 900px;
  height: 700px;
  top: -320px;
  left: -220px;
  background: radial-gradient(circle at 50% 50%, rgba(37, 99, 235, 0.95), rgba(23, 44, 122, 0) 68%);
}

.blob-violet {
  width: 820px;
  height: 620px;
  top: -300px;
  right: -60px;
  background: radial-gradient(circle at 50% 50%, rgba(168, 85, 247, 0.9), rgba(76, 29, 149, 0) 68%);
}

.blob-red {
  width: 900px;
  height: 640px;
  right: -220px;
  bottom: -360px;
  background: radial-gradient(circle at 50% 50%, rgba(239, 68, 68, 0.85), rgba(127, 29, 29, 0) 68%);
}

.blob-teal {
  width: 700px;
  height: 540px;
  left: 2%;
  bottom: -360px;
  background: radial-gradient(circle at 50% 50%, rgba(13, 148, 136, 0.6), rgba(12, 74, 110, 0) 68%);
}

/* 细颗粒：压住大面积渐变的塑料感（太重会把颜色吃掉） */
.grain {
  position: absolute;
  inset: 0;
  opacity: 0.07;
  background-image: radial-gradient(rgba(255, 255, 255, 0.2) 1px, transparent 1px);
  background-size: 3px 3px;
  mix-blend-mode: overlay;
}

/* ---------- 顶栏 ---------- */
.home-top {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 26px;
}

.home-brand {
  display: flex;
  align-items: center;
  gap: 9px;
}

.brand-name {
  font-size: 15px;
  letter-spacing: 0.14em;
  color: rgba(255, 255, 255, 0.92);
}


.home-top-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.home-top-actions .ghost {
  color: rgba(255, 255, 255, 0.68);
  border-color: rgba(255, 255, 255, 0.14);
  background: rgba(255, 255, 255, 0.04);
}

.home-top-actions .ghost:hover {
  color: #fff;
  border-color: rgba(255, 255, 255, 0.26);
  background: rgba(255, 255, 255, 0.09);
}

/* ---------- 主体 ---------- */
.home-stage {
  position: relative;
  z-index: 2;
  flex: 1;
  /* 主输入卡片限宽居中：通栏的卡片会把视线拉散，也不像「一个对话框」 */
  --home-card-w: min(1040px, 92vw);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  padding: clamp(48px, 9vh, 108px) 24px 8px;
  text-align: center;
}

/* HomeComposer 的根元素就是 ComposerCard 的 .cc */
.home-stage > :deep(.cc) {
  max-width: var(--home-card-w);
  margin: 0 auto;
}

.hero-title {
  margin: 0;
  font-size: clamp(30px, 4.4vw, 46px);
  font-weight: 300;
  line-height: 1.18;
  letter-spacing: 0.01em;
  color: #fff;
  text-shadow: 0 2px 30px rgba(0, 0, 0, 0.35);
}

.hero-title em {
  font-style: italic;
  font-weight: 300;
}

.hero-sub {
  margin: 12px 0 18px;
  font-size: clamp(14px, 1.5vw, 17px);
  letter-spacing: 0.22em;
  color: rgba(255, 255, 255, 0.56);
}

/* 入口指引（D-47）：不是按钮——一行淡灰小字，/ 分隔，hover 提亮。
   视觉上是「提示」而不是「控件」，避免和输入卡片抢注意力。 */
.hero-hints {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: center;
  margin: 0 0 20px;
  font-size: 12.5px;
  color: rgba(255, 255, 255, 0.34);
}

.hero-hints .hint {
  padding: 2px 4px;
  border: none;
  background: transparent;
  color: inherit;
  font-size: inherit;
  font-family: inherit;
  cursor: pointer;
  transition: color 140ms var(--ease);
}

.hero-hints .hint:hover,
.hero-hints .hint:focus-visible {
  color: rgba(255, 255, 255, 0.72);
}

.hero-hints .hint-sep {
  color: rgba(255, 255, 255, 0.2);
  user-select: none;
}

/* ---------- 我的作品 ---------- */
.home-works {
  position: relative;
  z-index: 2;
  max-width: 1040px;
  width: 100%;
  margin: 0 auto;
  padding: 28px 24px 56px;
}

.works-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  width: 100%;
  margin-bottom: 14px;
}
.works-main {
  display: inline-flex; align-items: baseline; gap: 10px;
  padding: 0; background: none; border: none;
  color: inherit; font-family: inherit; cursor: pointer;
}
.works-main .works-title { cursor: inherit; }
.works-more {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 2px;
  font-size: 12px; color: var(--dim);
  padding: 0; background: none; border: none;
  font-family: inherit; cursor: pointer;
  transition: color 130ms var(--ease);
}
.works-more .app-ic { width: 13px; height: 13px; }
.works-more:hover { color: var(--ink); }
.works-open {
  font-size: 12px;
  color: var(--dim);
  text-decoration: underline dotted;
  text-underline-offset: 3px;
  cursor: pointer;
  transition: color 130ms var(--ease);
}
.works-open:hover,
.works-open:focus-visible { color: var(--ink); }

.works-title {
  font-size: 13px;
  letter-spacing: 0.16em;
  color: rgba(255, 255, 255, 0.58);
}

.works-count {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.34);
}

/* 作品栏：一行排开，不换行——放得下几张就放几张，其余横向滚动。
   换行会把首页顶出一屏，用户得往下滚才看到自己的作品。 */
.works-grid {
  display: flex;
  flex-wrap: nowrap;
  gap: 14px;
  /* overflow-x 会把四个方向都变成裁剪边界：卡片 hover 上浮 1px、
     Tab 聚焦的焦点环（:focus-visible 的 4px box-shadow）都会被切掉上边。
     外扩 6px 裁剪余量（负 margin 抵消占位，卡片仍与标题对齐）。 */
  overflow-x: auto;
  scrollbar-width: none;
  padding: 6px;
  margin: -6px;
}
.works-grid::-webkit-scrollbar { display: none; }
.works-grid > .work-card { flex: 0 0 212px; }

.work-card {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px;
  border-radius: var(--radius-lg);
  border: 1px solid rgba(255, 255, 255, 0.09);
  background: rgba(255, 255, 255, 0.045);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 0.16s ease, border-color 0.16s ease, transform 0.16s ease;
}

.work-card:hover {
  background: rgba(255, 255, 255, 0.08);
  border-color: rgba(255, 255, 255, 0.17);
  transform: translateY(-1px);
}

.new-card {
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  padding: 18px 16px;
}

.new-plus {
  width: 34px;
  height: 34px;
  border-radius: var(--radius-md);
  display: grid;
  place-items: center;
  background: rgba(255, 255, 255, 0.09);
  color: rgba(255, 255, 255, 0.85);
}

.new-text {
  font-size: 13.5px;
  color: rgba(255, 255, 255, 0.86);
}

.new-sub {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.42);
}

.work-cover {
  width: 42px;
  height: 42px;
  flex: none;
  border-radius: var(--radius-md);
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, rgba(59, 130, 246, 0.35), rgba(147, 51, 234, 0.35));
  color: rgba(255, 255, 255, 0.9);
  font-size: 17px;
}

.work-body {
  min-width: 0;
}

.work-title {
  margin: 0 0 3px;
  font-size: 13.5px;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.9);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.work-meta {
  margin: 0;
  font-size: 11.5px;
  color: rgba(255, 255, 255, 0.42);
}

.work-del {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 22px;
  height: 22px;
  border: 0;
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.5);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s ease, background 0.15s ease;
}

.work-card:hover .work-del {
  opacity: 1;
}

.work-del:hover {
  background: rgba(239, 68, 68, 0.22);
  color: #fff;
}

.works-empty {
  margin: 18px 0 0;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.42);
}

@media (max-width: 720px) {
  .home-stage {
    padding-top: 12px;
  }
  .hero-sub {
    margin-bottom: 20px;
    letter-spacing: 0.14em;
  }
  .home-top {
    padding: 14px 16px;
  }
}
</style>
