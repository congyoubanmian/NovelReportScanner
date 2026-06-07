import { ref, onMounted } from 'vue'

const STORAGE_KEY = 'nrs-theme'

function getInitialTheme() {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'dark' || stored === 'light') return stored
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

const theme = ref(getInitialTheme())

export function useTheme() {
  function apply(t) {
    document.documentElement.setAttribute('data-theme', t)
    localStorage.setItem(STORAGE_KEY, t)
    theme.value = t
  }

  function toggle() {
    apply(theme.value === 'dark' ? 'light' : 'dark')
  }

  onMounted(() => {
    apply(theme.value)
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    mq.addEventListener('change', (e) => {
      if (!localStorage.getItem(STORAGE_KEY)) {
        apply(e.matches ? 'dark' : 'light')
      }
    })
  })

  return {
    theme,
    toggle,
    isDark: () => theme.value === 'dark',
  }
}
