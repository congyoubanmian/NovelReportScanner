import { onMounted, onUnmounted } from 'vue'

export function usePolling(callback, intervalMs = 3000) {
  let timer = null

  function start() {
    if (timer || document.hidden) return
    timer = setInterval(callback, intervalMs)
  }

  function stop() {
    if (!timer) return
    clearInterval(timer)
    timer = null
  }

  function handleVisibilityChange() {
    if (document.hidden) {
      stop()
    } else {
      callback()
      start()
    }
  }

  onMounted(() => {
    callback()
    start()
    document.addEventListener('visibilitychange', handleVisibilityChange)
  })

  onUnmounted(() => {
    stop()
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  })

  return { start, stop }
}
