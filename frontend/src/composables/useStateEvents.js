import { onMounted, onUnmounted } from 'vue'
import { withAccessToken } from '../api.js'

export function useStateEvents(onState, options = {}) {
  const eventsPath = options.eventsPath || '/api/events'
  const onOpen = options.onOpen || (() => {})
  const onFallback = options.onFallback || (() => {})
  const retryMs = options.retryMs || 5000
  let source = null
  let retryTimer = null

  function closeSource() {
    if (!source) return
    source.close()
    source = null
  }

  function clearRetry() {
    if (!retryTimer) return
    clearTimeout(retryTimer)
    retryTimer = null
  }

  function scheduleRetry() {
    if (retryTimer) return
    retryTimer = setTimeout(() => {
      retryTimer = null
      connect()
    }, retryMs)
  }

  function connect() {
    if (source || document.hidden) return
    if (typeof EventSource === 'undefined') {
      onFallback()
      return
    }
    clearRetry()
    source = new EventSource(withAccessToken(eventsPath))
    source.addEventListener('open', onOpen)
    source.addEventListener('state', (event) => {
      try {
        onState(JSON.parse(event.data))
      } catch {
        closeSource()
        onFallback()
        scheduleRetry()
      }
    })
    source.addEventListener('retry', () => {
      closeSource()
      connect()
    })
    source.addEventListener('error', () => {
      closeSource()
      onFallback()
      scheduleRetry()
    })
  }

  function handleVisibilityChange() {
    if (document.hidden) {
      clearRetry()
      closeSource()
    } else {
      connect()
    }
  }

  onMounted(() => {
    connect()
    document.addEventListener('visibilitychange', handleVisibilityChange)
  })

  onUnmounted(() => {
    clearRetry()
    closeSource()
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  })

  return { connect, close: closeSource }
}
