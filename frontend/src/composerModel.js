// =====================================================================
// composerModel.js —— 输入区「模型胶囊 + 下拉」的共用逻辑
//
// 为什么抽出来：首页（HomeComposer）与工作区（ChatComposer）各写一份时，
// 首页那份漏了 @pick-model 绑定与 #pills 插槽 → 模型胶囊点了没反应。
// 两边共用同一个 composable，以后不会再漂。
// =====================================================================
import { computed, ref } from 'vue'
import { api } from './api'
import { store, loadModelStatus, loadProviders, notify } from './stores/app'

export function useModelMenu() {
  const openMenu = ref('') // '' | 'model'

  const modelLabel = computed(() => {
    const m = store.modelInfo
    if (!m?.available) return '模型'
    return m.name || m.provider
  })

  const modelOptions = computed(() =>
    (store.providers || []).filter((p) => p.configured && p.enabled !== false),
  )

  function toggleMenu(name) {
    openMenu.value = openMenu.value === name ? '' : name
  }

  async function chooseModel(p) {
    openMenu.value = ''
    try {
      await api('/models/preferences', 'PUT', {
        task_type: 'chat',
        provider_id: p.id,
        model_name: p.config?.model || '',
        is_default: true,
      })
      await Promise.all([loadModelStatus(), loadProviders()])
      notify(`对话模型已切到 ${p.label || p.name}`, 'ok')
    } catch (e) {
      notify(`切换失败：${e.message}`)
    }
  }

  return { openMenu, modelLabel, modelOptions, toggleMenu, chooseModel }
}

/** 模型下拉的模板片段（两个输入区都用同一份，避免再漂）。 */
export const MODEL_POPUP = `
<div v-if="openMenu === 'model'" class="cc-pop">
  <button
    v-for="p in modelOptions" :key="p.id"
    class="cc-pop-item" type="button"
    @click.stop="chooseModel(p)"
  >
    <span class="pop-label">{{ p.label || p.name }}</span>
    <span class="pop-desc">{{ p.config?.model || p.name }}</span>
  </button>
  <div v-if="!modelOptions.length" class="pop-empty">没有可用模型，去设置里配一个。</div>
</div>
`
