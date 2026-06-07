const API_BASE = ''
const REQUEST_TIMEOUT_MS = 15000

async function api(path, options = {}) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: options.signal || controller.signal
    })
    if (!res.ok) {
      const text = await res.text()
      throw new Error(text || `HTTP ${res.status}`)
    }
    return res.json()
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error('请求超时')
    }
    throw e
  } finally {
    clearTimeout(timeout)
  }
}

export function getState() {
  return api('/api/state')
}

export function getBookDetail(bookId) {
  return api(`/api/book?id=${encodeURIComponent(bookId)}`)
}

export function setProfile(bookId, profile) {
  return api('/api/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_id: bookId, profile })
  })
}

export function enqueueBook(bookId) {
  return api('/api/enqueue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ book_id: bookId })
  })
}

export function uploadBook(formData) {
  return api('/upload', {
    method: 'POST',
    body: formData
  })
}
