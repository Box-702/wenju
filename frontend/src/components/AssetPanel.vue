<template>
  <div class="asset-panel">
    <!-- 新建作品 -->
    <div v-if="section === 'all'" class="panel-section">
      <button class="new-task" @click="store.showNewProject = true">
        <AppIcon name="plus" />
        <span>新建作品</span>
      </button>
    </div>

    <!-- 技能：主 Agent 能加载的技能；点一下预填进输入框 -->
    <div v-if="section === 'all'" class="panel-section">
      <div class="section-head">
        <span class="section-title">技能</span>
      </div>
      <button
        v-for="s in store.skills" :key="s.name"
        class="skill-item"
        :title="s.description"
        @click="useSkill(s)"
      >
        <span class="skill-name">{{ s.name }}</span>
        <span class="skill-desc">{{ s.description }}</span>
      </button>
      <div v-if="!store.skills.length" class="proj-empty">
        <p>没有可用技能。</p>
      </div>
    </div>

    <!-- 作品列表（每个作品一个工作区） -->
    <div v-if="section === 'all' || section === 'tasks'" class="panel-section">
      <div class="section-head">
        <span class="section-title">我的作品</span>
      </div>
      <div
        v-for="p in projects" :key="p.id"
        :class="['proj-item', { active: store.pid === p.id }]"
        @click="enterWorkspace(p.id)"
      >
        <span class="proj-name">{{ p.title }}</span>
      </div>
      <div v-if="!projects.length" class="proj-empty">
        <p>还没有作品。新建一部，导入小说原文就开始。</p>
      </div>
    </div>

    <!-- 资产库：当前作品的角色白模与场景图（提示词里的映射编号来自这里的 id） -->
    <div v-if="store.pid && (section === 'all' || section === 'assets')" class="panel-section library-section">
      <div class="section-head">
        <span class="section-title">资产库</span>
        <span class="head-tools">
          <input
            ref="importInput"
            type="file"
            accept="image/*,video/*"
            multiple
            hidden
            @change="onImportChosen"
          />
          <button class="btn-tiny" :disabled="importing" @click="importInput?.click()" title="导入图片/视频">
            <AppIcon name="upload" /> {{ importing ? '导入中…' : '导入' }}
          </button>
          <button class="btn-tiny" @click="loadLibrary" title="刷新" aria-label="刷新">⟳</button>
        </span>
      </div>

      <div v-if="lib.loading" class="lib-empty">加载中…</div>
      <template v-else>
        <!-- 概览：一眼看清这部作品攒了多少资产、还缺什么 -->
        <div class="lib-stats">
          <span class="stat"><b>{{ lib.characters.length }}</b> 角色</span>
          <span class="stat"><b>{{ lib.locations.length }}</b> 场景</span>
          <span class="stat" :class="{ warn: missingCount > 0 }"><b>{{ missingCount }}</b> 缺图</span>
          <span v-if="lib.visual_style" class="stat">画风 · {{ visualStyleLabel }}</span>
        </div>

        <!-- 缺什么：后端已经算好了（没定妆照会断人物一致性），直接摊开说 -->
        <div v-if="lib.warnings.length" class="lib-warn">
          <div v-for="(w, i) in lib.warnings" :key="i" class="warn-row">
            <AppIcon name="warning" /> <span>{{ w }}</span>
          </div>
        </div>

        <div class="lib-label"><span class="seg-num">01</span>角色 · 定妆照与音色</div>
        <div class="asset-grid">
          <div v-for="c in lib.characters" :key="c.char_id" class="asset-card">
            <div class="asset-pic">
              <img v-if="c.image" :src="c.image" :alt="c.name" loading="lazy" />
              <div v-else class="asset-placeholder">缺定妆照</div>
            </div>
            <div class="asset-meta">
              <div class="asset-line">
                <span class="asset-name">{{ c.name }}</span>
                <span class="asset-id">{{ c.char_id }}</span>
              </div>
              <div class="asset-line">
                <span class="asset-badge" :class="{ miss: !c.image }">{{ c.image ? '定妆照' : '缺定妆照' }}</span>
              </div>
              <div class="asset-voice" :class="{ none: !c.voice_label }">
                <AppIcon name="sound" />
                <span>{{ c.voice_label || '未指定音色' }}</span>
                <span v-if="c.gender" class="asset-gender">{{ genderZh(c.gender) }}</span>
                <span v-if="c.confirmed" class="asset-ok" title="音色已人工确认">已确认</span>
              </div>
              <p v-if="c.appearance" class="asset-desc" :title="c.appearance">{{ c.appearance }}</p>
              <div class="asset-nums">台词 {{ c.speech_chars }} 字 · {{ c.scenes }} 场</div>
            </div>
          </div>
        </div>

        <div class="lib-label"><span class="seg-num">02</span>场景 · 场景图</div>
        <div class="asset-grid">
          <div v-for="l in lib.locations" :key="l.loc_id" class="asset-card">
            <div class="asset-pic scene">
              <img v-if="l.image" :src="l.image" :alt="l.name" loading="lazy" />
              <div v-else class="asset-placeholder">缺场景图</div>
            </div>
            <div class="asset-meta">
              <div class="asset-line">
                <span class="asset-name">{{ l.name }}</span>
                <span class="asset-id">{{ l.loc_id }}</span>
              </div>
              <div class="asset-line">
                <span class="asset-badge" :class="{ miss: !l.image }">{{ l.image ? '场景图' : '缺场景图' }}</span>
              </div>
              <p v-if="l.description" class="asset-desc" :title="l.description">{{ l.description }}</p>
              <div class="asset-nums">{{ l.scenes }} 场</div>
            </div>
          </div>
        </div>

        <!-- 成片与导入的素材：资产库是所有产出的最终归宿 -->
        <div class="lib-label"><span class="seg-num">03</span>成片与素材</div>
        <div v-if="assets.length" class="asset-grid">
          <div v-for="a in assets" :key="a.id" class="asset-card">
            <div class="asset-pic scene">
              <video v-if="a.kind === 'video'" :src="a.url" muted playsinline preload="metadata"></video>
              <img v-else-if="a.kind === 'image'" :src="a.url" :alt="a.name" loading="lazy" />
              <div v-else class="asset-placeholder">{{ a.kind === 'audio' ? '音频' : '素材' }}</div>
            </div>
            <div class="asset-meta">
              <div class="asset-line">
                <span class="asset-name" :title="a.name">{{ a.name || '未命名' }}</span>
              </div>
              <div class="asset-line">
                <span class="asset-badge">{{ kindZh(a.kind) }}</span>
              </div>
              <div class="asset-nums">{{ sourceZh(a.source) }}</div>
              <a class="asset-dl" :href="a.url" :download="a.name || true" target="_blank" rel="noopener">
                <AppIcon name="download" /> 下载
              </a>
            </div>
          </div>
        </div>
        <div v-else class="lib-empty">还没有成片或导入的素材。出片后成片会出现在这里。</div>

        <div v-if="!lib.characters.length && !lib.locations.length" class="lib-empty">
          还没有资产。点下方「开始制作」，流水线会生成人物定妆照与场景图。
        </div>

        <button class="produce-btn" :disabled="producing" @click="startProduction">
          <AppIcon :name="producing ? 'pause' : 'play'" /> {{ producing ? '制作中…' : '开始制作（剧本→资产→提示词）' }}
        </button>
      </template>
    </div>

    <!-- 未选中作品：提示进入 -->
    <div v-else class="lib-empty" style="padding: 18px 14px;">
      左侧选择一部作品进入工作区；资产库会显示这部作品的人物与场景。
    </div>
  </div>
</template>

<script setup>
// =====================================================================
// AssetPanel.vue —— 左栏：作品入口 + 资产库
//
// 改版说明（工作区化）：原来这里是「对话列表」（全局会话 + 每项目多会话），
// 现在对话收敛为每作品一个（进入作品自动用默认会话），左栏让给资产库——
// 对话里引用的每个人物白模 / 场景图都能在这里按编号找到原图。
// =====================================================================
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { store, selectProject, notify, loadShots } from '../stores/app.js'
import { loadSkills, useSkill } from '../stores/app'
import { api, uploadAsset } from '../api.js'
import AppIcon from './AppIcon.vue'

const projects = computed(() => store.projects)
const props = defineProps({
  /** 显示哪一段：tasks = 我的作品（作品列表）；assets = 资产库 */
  section: { type: String, default: 'all' },
})

const lib = reactive({
  loading: false, characters: [], locations: [], warnings: [],
  visual_style: '', project_title: '',
})

/** 成片与导入的素材（视频/图片/音频）—— 资产库的另一半。 */
const assets = ref([])

const ASSET_KIND_ZH = { video: '成片', image: '图片', audio: '音频' }
const kindZh = (k) => ASSET_KIND_ZH[k] || '素材'
const SOURCE_ZH = { upload: '手动导入', finalize: '流水线产出', url: '外部链接' }
const sourceZh = (s) => SOURCE_ZH[s] || ''

/** 缺图数量：角色缺定妆照 + 场景缺场景图。这是出片能不能锁住一致性的前提。 */
const missingCount = computed(
  () => lib.characters.filter((c) => !c.image).length + lib.locations.filter((l) => !l.image).length,
)

const VISUAL_STYLE_ZH = {
  realistic: '实拍写实',
  anime: '二次元（漫剧）',
  '3d': '三维动画',
  ink: '水墨国风',
}
const visualStyleLabel = computed(() => VISUAL_STYLE_ZH[lib.visual_style] || lib.visual_style || '')

const GENDER_ZH = { male: '男', female: '女' }
function genderZh(g) {
  return GENDER_ZH[String(g || '').toLowerCase()] || ''
}

// ---- 导入素材 ----
const importInput = ref(null)
const importing = ref(false)

async function onImportChosen(e) {
  const files = [...(e.target.files || [])]
  e.target.value = ''
  if (!files.length || !store.pid) return
  importing.value = true
  try {
    for (const file of files) {
      await uploadAsset(store.pid, file)
    }
    notify(`已导入 ${files.length} 个素材`, 'ok')
    await loadLibrary()
  } catch (err) {
    notify(`导入失败：${err.message}`)
  } finally {
    importing.value = false
  }
}
const producing = ref(false)

function enterWorkspace(pid) {
  if (store.pid === pid) return
  selectProject(pid)
}

async function loadLibrary() {
  if (!store.pid) return
  lib.loading = true
  try {
    // 素材与成片一起拉；失败不阻断资产库主体（人物/场景）
    try {
      assets.value = await api(`/projects/${store.pid}/assets`)
    } catch {
      assets.value = []
    }
    const data = await api(`/projects/${store.pid}/library`)
    lib.characters = data.characters || []
    lib.locations = data.locations || []
    lib.warnings = data.warnings || []
    lib.visual_style = data.visual_style || ''
    lib.project_title = data.project_title || ''
  } catch (e) {
    notify('资产库加载失败：' + e.message)
  } finally {
    lib.loading = false
  }
}

async function startProduction() {
  if (!store.pid || producing.value) return
  producing.value = true
  try {
    const r = await api(`/projects/${store.pid}/production`, 'POST')
    notify('生产流水线已启动，完成后提示词进入待审', 'ok')
    // 轮询任务状态：结束后刷新资产库与右栏镜头（提示词版本已更新）。
    const taskId = r.task_id
    const timer = setInterval(async () => {
      try {
        const t = await api(`/tasks/${taskId}`)
        if (t.status === 'done' || t.status === 'failed') {
          clearInterval(timer)
          producing.value = false
          if (t.status === 'failed') notify('流水线失败：' + (t.result || ''))
          else notify('流水线完成：' + (t.result || ''), 'ok')
          await loadLibrary()
          await loadShots()
        }
      } catch { clearInterval(timer); producing.value = false }
    }, 3000)
  } catch (e) {
    notify('启动失败：' + e.message)
    producing.value = false
  }
}

// 进入新作品 / 资产可能被流水线更新 → 重新拉资产库
onMounted(() => { loadSkills() })

watch(() => store.pid, (pid) => { if (pid) loadLibrary() }, { immediate: true })
</script>

<style scoped>
.asset-panel { display: flex; flex-direction: column; height: 100%; overflow-y: auto; }
.panel-section { padding: 4px 0 10px; }
.library-section { border-top: 1px solid color-mix(in oklch, var(--line) 55%, transparent); }
.section-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 14px 6px;
}
.section-title {
  font-size: 9.5px; font-weight: 650; color: var(--dim);
  text-transform: uppercase; letter-spacing: .14em;
}
.btn-new {
  width: 28px; height: 28px; border-radius: var(--radius-sm);
  border: 1px solid var(--line); background: transparent;
  color: var(--muted); cursor: pointer; font-size: 12px; line-height: 1;
  display: flex; align-items: center; justify-content: center; padding: 0;
  transition: color 150ms var(--ease), border-color 150ms var(--ease), background 150ms var(--ease);
}
.btn-new:hover {
  border-color: color-mix(in oklch, var(--gold) 45%, var(--line));
  color: var(--gold); background: var(--gold-soft);
}
.btn-tiny {
  background: transparent; border: none; border-radius: 4px;
  cursor: pointer; font-size: 12px; padding: 4px 6px;
  opacity: .55; color: var(--muted); transition: opacity 120ms, background 120ms;
}
.btn-tiny:hover { opacity: 1; background: color-mix(in oklch, var(--ink) 8%, transparent); }

/* 新任务入口 */
.new-task {
  display: flex; align-items: center; gap: 9px;
  width: 100%; padding: 9px 11px; border-radius: var(--radius-md);
  background: rgba(255, 255, 255, .05); border: 1px solid var(--line);
  color: var(--ink); font-size: 13px; font-family: inherit; cursor: pointer;
  transition: background 130ms var(--ease), border-color 130ms var(--ease);
}
.new-task:hover {
  background: rgba(255, 255, 255, .1); border-color: var(--line-strong);
}
.new-task .app-ic { opacity: .8; }

/* 技能条目 */
.skill-item {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  width: 100%; padding: 7px 10px; border-radius: var(--radius-md);
  background: transparent; border: 1px solid transparent;
  color: var(--muted); font-family: inherit; text-align: left; cursor: pointer;
  transition: background 130ms var(--ease), color 130ms var(--ease);
}
.skill-item:hover {
  background: rgba(255, 255, 255, .07); color: var(--ink);
}
.skill-name {
  font-size: 12.5px; font-weight: 500; color: var(--ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;
}
.skill-desc {
  font-size: 11px; line-height: 1.45; color: var(--dim);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}

.head-tools { display: inline-flex; align-items: center; gap: 6px; }
.head-tools .btn-tiny { display: inline-flex; align-items: center; gap: 4px; }
.head-tools .app-ic { width: 12px; height: 12px; }

.proj-item {
  position: relative;
  display: flex; align-items: center; padding: 10px 12px 10px 14px;
  margin: 1px 6px; border-radius: var(--radius-sm);
  cursor: pointer; font-size: 12.5px; color: var(--muted);
  transition: background .14s var(--ease), color .14s var(--ease);
}
.proj-item:hover { background: color-mix(in oklch, var(--ink) 5%, transparent); color: var(--ink); }
.proj-item.active {
  background: color-mix(in oklch, var(--gold) 9%, transparent); color: var(--ink);
}
.proj-item.active::before {
  content: ''; position: absolute; left: 0; top: 7px; bottom: 7px; width: 2px;
  border-radius: 2px; background: var(--gold);
}
.proj-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.proj-empty { padding: 10px 14px; font-size: 12px; color: var(--dim); line-height: 1.7; }

.lib-label {
  padding: 10px 14px 4px;
  font-size: 11px; font-weight: 650; color: var(--muted);
}
.asset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(152px, 1fr));
  gap: 10px;
  margin-bottom: 4px;
}
/* 概览：角色数 / 场景数 / 缺图数 —— 纯文字，用竖线分隔，不做胶囊 */
.lib-stats {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 0;
  margin: 2px 0 10px; color: var(--muted); font-size: var(--fs-xs);
}
.stat {
  display: inline-flex; align-items: baseline; gap: 4px;
  padding: 0 10px; color: var(--muted);
}
.stat:first-child { padding-left: 0; }
.stat + .stat { border-left: 1px solid var(--line); }
.stat b { color: var(--ink); font-weight: 600; font-size: var(--fs-sm); }
.stat.warn { color: var(--warn); }
.stat.warn b { color: var(--warn); }

/* 缺图告警：后端算好的事实，直接摊开 */
.lib-warn {
  display: flex; flex-direction: column; gap: 4px;
  margin-bottom: 12px; padding: 8px 10px;
  border: 1px solid color-mix(in oklch, var(--warn) 32%, var(--line));
  border-radius: var(--radius-lg);
  background: color-mix(in oklch, var(--warn) 8%, transparent);
}
.warn-row {
  display: flex; align-items: flex-start; gap: 6px;
  font-size: var(--fs-xs); color: var(--muted); line-height: 1.5;
}
.warn-row .app-ic { width: 13px; height: 13px; flex: none; margin-top: 1px; color: var(--warn); }

/* 资产卡：图 + 档案信息 */
.asset-card {
  border: 1px solid var(--line); border-radius: var(--radius-lg);
  background: var(--panel); overflow: hidden;
  transition: border-color 130ms var(--ease), transform 130ms var(--ease);
}
.asset-card:hover { border-color: var(--line-strong); transform: translateY(-1px); }
.asset-pic { position: relative; aspect-ratio: 3 / 4; background: rgba(0, 0, 0, .28); }
.asset-pic.scene { aspect-ratio: 16 / 10; }
.asset-pic img { display: block; width: 100%; height: 100%; object-fit: cover; }
.asset-placeholder {
  display: grid; place-items: center; height: 100%;
  color: var(--dim); font-size: var(--fs-xs);
  background: repeating-linear-gradient(
    45deg, rgba(255, 255, 255, .025) 0 8px, transparent 8px 16px
  );
}
/* 徽标从「压在图上的胶囊」改成「图下方的一行字」——压图需要底色，
   底色正是胶囊感的来源。 */
.asset-badge {
  color: var(--dim); font-size: 10px; letter-spacing: .02em;
}
.asset-badge.miss { color: var(--warn); }

.asset-meta { padding: 8px 9px 9px; display: flex; flex-direction: column; gap: 4px; }
.asset-line { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.asset-name {
  font-size: var(--fs-md); color: var(--ink); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.asset-id { font-family: var(--mono); font-size: 10px; color: var(--dim); flex: none; }
.asset-voice {
  display: flex; align-items: center; gap: 4px;
  font-size: var(--fs-xs); color: var(--muted);
  min-width: 0;
}
.asset-voice .app-ic { width: 12px; height: 12px; flex: none; opacity: .8; }
.asset-voice span:first-of-type { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-voice.none { color: var(--dim); }
.asset-gender { flex: none; color: var(--dim); font-size: 10px; }
.asset-ok { flex: none; color: var(--accent); font-size: 10px; }
.asset-desc {
  margin: 0; font-size: var(--fs-xs); color: var(--dim); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.asset-nums { font-size: 10px; color: var(--dim); font-variant-numeric: tabular-nums; }
.asset-pic video { display: block; width: 100%; height: 100%; object-fit: cover; }
.asset-dl {
  display: inline-flex; align-items: center; gap: 4px; align-self: flex-start;
  margin-top: 2px; padding: 2px 8px; border-radius: var(--radius-md);
  border: 1px solid var(--line); color: var(--muted);
  font-size: 11px; text-decoration: none;
  transition: color 130ms var(--ease), border-color 130ms var(--ease), background 130ms var(--ease);
}
.asset-dl:hover { color: var(--ink); border-color: var(--line-strong); background: rgba(255, 255, 255, .06); }
.asset-dl .app-ic { width: 12px; height: 12px; }

.lib-empty { padding: 8px 14px; font-size: var(--fs-xs); color: var(--dim); line-height: 1.7; }
.produce-btn {
  background: var(--accent-grad); color: var(--on-accent); border-color: transparent;
  box-shadow: 0 8px 24px rgba(34, 197, 94, .26);
  display: block; width: calc(100% - 24px); margin: 10px 12px 4px;
  min-height: 38px; border-radius: var(--radius-md);
  border: 1px solid transparent;
  cursor: pointer; font-size: 12px; font-weight: 650;
  transition: filter 150ms var(--ease), transform 100ms var(--ease);
}
.produce-btn:hover:not(:disabled) { filter: brightness(1.08); }
.produce-btn:active:not(:disabled) { transform: scale(.985); }
.produce-btn:disabled { opacity: .6; cursor: default; }
</style>
