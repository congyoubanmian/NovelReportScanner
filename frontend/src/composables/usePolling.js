import { onMounted, onUnmounted } from 'vue'

export function usePolling(callback, intervalMs = 3000, options = {}) {
  let timer = null
  let running = false
  const autoStart = options.autoStart !== false

  async function runCallback() {
    if (running) return
    running = true
    try {
      await callback()
    } finally {
      running = false
    }
  }

  function start() {
    if (timer || document.hidden) return
    timer = setInterval(runCallback, intervalMs)
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
      runCallback()
      start()
    }
  }

  onMounted(() => {
    runCallback()
    if (autoStart) start()
    document.addEventListener('visibilitychange', handleVisibilityChange)
  })

  onUnmounted(() => {
    stop()
    document.removeEventListener('visibilitychange', handleVisibilityChange)
  })

  return { start, stop }
}
