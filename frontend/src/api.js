const API_BASE = ''
const REQUEST_TIMEOUT_MS = 15000
const UPLOAD_TIMEOUT_MS = 120000
const ACCESS_TOKEN_STORAGE_KEY = 'novel_report_scanner_access_token'

export function getAccessToken() {
  try {
    return window.localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY) || ''
  } catch {
    return ''
  }
}

export function setAccessToken(token) {
  try {
    const value = (token || '').trim()
    if (value) {
      window.localStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, value)
    } else {
      window.localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY)
    }
  } catch {
    // localStorage 不可用时只跳过持久化。
  }
}

export function withAccessToken(path) {
  const token = getAccessToken()
  if (!token || !path.startsWith('/')) return path
  const joiner = path.includes('?') ? '&' : '?'
  return `${path}${joiner}token=${encodeURIComponent(token)}`
}

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
      headers: _requestHeaders(options.headers),
      signal: options.signal || controller.signal
    })
    if (!res.ok) {
      const text = await res.text()
      throw new Error(_formatErrorResponse(text, res.status))
    }
    return res
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error(
        timeoutMs >= UPLOAD_TIMEOUT_MS ? '上传超时，请检查网络或减小文件大小' : '请求超时',
        { cause: e }
      )
    }
    throw e
  } finally {
    clearTimeout(timeout)
  }
}

function _formatErrorResponse(text, status) {
  if (!text) return `HTTP ${status}`
  try {
    const data = JSON.parse(text)
    const parts = [data.error, data.detail, data.hint].filter(Boolean)
    return parts.length ? parts.join('：') : text
  } catch {
    return text
  }
}

function _requestHeaders(headers = {}) {
  const merged = new Headers(headers)
  const token = getAccessToken()
  if (token) {
    merged.set('Authorization', `Bearer ${token}`)
  }
  return merged
}

function _writeHeaders(headers = {}) {
  const merged = new Headers(headers)
  merged.set('X-Web-Unsafe-Action', 'confirm')
  return merged
}

export function getState() {
  return _api('/api/state')
}

export function getDiagnostics() {
  return _api('/api/diagnostics')
}

export function getBookDetail(bookId) {
  return _api(`/api/book?id=${encodeURIComponent(bookId)}`)
}

export async function getTextFile(url, timeoutMs = REQUEST_TIMEOUT_MS) {
  const res = await _request(withAccessToken(url), {}, timeoutMs)
  return res.text()
}

export function setProfile(bookId, profile) {
  return _api('/api/profile', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_id: bookId, profile })
  })
}

export function updateRuntimeConfig(config) {
  return _api('/api/config', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ config })
  })
}

export function enqueueBook(bookId) {
  return _api('/api/enqueue', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_id: bookId })
  })
}

export function enqueueBooks(bookIds) {
  return _api('/api/enqueue-batch', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_ids: bookIds })
  })
}

export function cancelQueuedBook(bookId) {
  return _api('/api/cancel', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_id: bookId })
  })
}

export function prioritizeQueuedBook(bookId) {
  return _api('/api/prioritize', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_id: bookId })
  })
}

export function moveQueuedBook(bookId, direction) {
  return _api('/api/move-queue', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_id: bookId, direction })
  })
}

export function deleteBook(bookId) {
  return _api('/api/delete', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_id: bookId })
  })
}

export function deleteBooks(bookIds) {
  return _api('/api/delete-batch', {
    method: 'POST',
    headers: _writeHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ book_ids: bookIds })
  })
}

export function uploadBook(formData) {
  return _api(
    '/upload',
    {
      method: 'POST',
      headers: _writeHeaders(),
      body: formData
    },
    UPLOAD_TIMEOUT_MS
  )
}
