const API_BASE = ''
const REQUEST_TIMEOUT_MS = 15000
const UPLOAD_TIMEOUT_MS = 120000

async function _api(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const res = await _request(path, options, timeoutMs)
  return res.json()
}

async function _request(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: options.signal || controller.signal
    })
    if (!res.ok) {
      const text = await res.text()
      throw new Error(text || `HTTP ${res.status}`)
    }
    return res
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error(timeoutMs >= UPLOAD_TIMEOUT_MS ? '上传超时，请检查网络或减小文件大小' : '请求超时')
    }
    throw e
  } finally {
    clearTimeout(timeout)
  }
}

export function getState() {
  return _api('/api/state')
}

export function getBookDetail(bookId) {
  return _api(`/api/book?id=${encodeURIComponent(bookId)}`)
}

export async function getTextFile(url, timeoutMs = REQUEST_TIMEOUT_MS) {
  const res = await _request(url, {}, timeoutMs)
  return res.text()
}

export function setProfile(bookId, profile) {
  return _api('/api/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_id: bookId, profile })
  })
}

export function enqueueBook(bookId) {
  return _api('/api/enqueue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_id: bookId })
  })
}

export function enqueueBooks(bookIds) {
  return _api('/api/enqueue-batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_ids: bookIds })
  })
}

export function uploadBook(formData) {
  return _api('/upload', {
    method: 'POST',
    body: formData
  }, UPLOAD_TIMEOUT_MS)
}
