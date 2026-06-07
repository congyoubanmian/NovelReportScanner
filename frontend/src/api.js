const API_BASE = ''

async function api(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, options)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `HTTP ${res.status}`)
  }
  return res.json()
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
