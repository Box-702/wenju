// =====================================================================
// api.js —— 后端接口封装
//
// 职责：
//   - 统一 JSON 请求（自动拼 /api 前缀、解析错误 detail）；
//   - FormData 文件上传（导入项目）；
//   - SSE 流式对话：逐帧解析 event/data 并回调给调用方。
// =====================================================================

const BASE = '/api'

/** 通用 JSON 请求。path 形如 '/projects'；失败时抛出带 detail 的 Error。 */
export async function api(path, method = 'GET', body) {
  // FormData 直接给 fetch（浏览器自己带 multipart 头），其余按 JSON 发。
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  const res = await fetch(BASE + path, {
    method,
    headers: body && !isForm ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
  })
  if (!res.ok) {
    let msg = res.statusText
    try { const j = await res.json(); msg = j.detail || JSON.stringify(j) } catch { /* 保留 statusText */ }
    throw new Error(msg)
  }
  return res.json()
}

/** 导入项目（新建作品）：FormData 内含 title / adaptation_type / file 或 raw_text。 */
export async function importProject(formData) {
  const res = await fetch(BASE + '/projects/import', { method: 'POST', body: formData })
  if (!res.ok) {
    const j = await res.json().catch(() => ({}))
    throw new Error(j.detail || res.statusText)
  }
  return res.json()
}

/** 上传素材文件（参考视频等）。 */
export async function uploadAsset(projectId, file) {
  // 图片与视频走**不同的后端端点**：/assets/upload 只收视频，
  // /images/upload 只收图片。混着发会得到「不支持的格式」。
  const isImage = (file.type || '').startsWith('image/')
  const path = isImage
    ? `projects/${projectId}/images/upload`
    : `projects/${projectId}/assets/upload`
  const res = await fetch(`${BASE}/${path}`, {
    method: 'POST',
    body: (() => { const fd = new FormData(); fd.append('file', file); return fd })(),
  })
  if (!res.ok) {
    const j = await res.json().catch(() => ({}))
    throw new Error(j.detail || res.statusText)
  }
  return res.json()
}

/** 上传对话用图片（发送消息前调用，返回 { id, url }，随消息的 images 字段提交）。 */
export async function uploadChatImage(file) {
  const res = await fetch(`${BASE}/chat/images`, {
    method: 'POST',
    body: (() => { const fd = new FormData(); fd.append('file', file); return fd })(),
  })
  if (!res.ok) {
    const j = await res.json().catch(() => ({}))
    throw new Error(j.detail || res.statusText)
  }
  return res.json()
}

/** 列出项目素材。 */
export async function listAssets(projectId, kind) {
  const q = kind ? `?kind=${encodeURIComponent(kind)}` : ''
  return api(`/projects/${projectId}/assets${q}`)
}

/** 删除素材。 */
export async function deleteAsset(assetId) {
  return api(`/assets/${assetId}`, 'DELETE')
}

/**
 * SSE 流式对话。
 * @param {object} body  { project_id, conversation_id, message, meta }
 * @param {(ev: {event: string, data: any}) => void} onFrame 每解析出一帧回调一次
 * @param {AbortSignal} [signal] 取消流（切走会话/组件销毁时应中止，释放连接）
 */
export async function streamChat(body, onFrame, signal) {
  const res = await fetch(BASE + '/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) {
    const j = await res.json().catch(() => ({}))
    throw new Error(j.detail || res.statusText)
  }
  // 按字节读取，遇到空行（\n\n）切分成一帧再解析；
  // 无论正常结束还是中途出错/取消，都要释放 reader，避免连接与锁泄漏。
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const ev = parseFrame(frame)
        if (ev) onFrame(ev)
      }
    }
  } finally {
    try { await reader.cancel() } catch { /* 已结束/已取消时忽略 */ }
    reader.releaseLock()
  }
}

/** 解析单帧 SSE 文本：`event: xxx` + `data: {...}`，解析失败返回 null。 */
function parseFrame(frame) {
  let event = 'message'
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    // SSE 规范：多行 data 用 \n 连接（去掉冒号后的第一个空格），
    // 用 trim() 会破坏 JSON 字符串值里的行尾空格。
    else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /, '')
  }
  if (!data) return null
  try { return { event, data: JSON.parse(data) } } catch (e) {
    console.warn('SSE 帧解析失败，已跳过：', frame, e)
    return null
  }
}
