import { ref } from 'vue'

const toast = ref(null)
let toastTimer = null

export function useToast() {
  function show(message, type = 'info') {
    if (toastTimer) clearTimeout(toastTimer)
    toast.value = { message, type }
    toastTimer = setTimeout(() => {
      toast.value = null
    }, 3000)
  }

  return {
    toast,
    show,
    success: (msg) => show(msg, 'success'),
    error: (msg) => show(msg, 'error'),
    info: (msg) => show(msg, 'info')
  }
}
