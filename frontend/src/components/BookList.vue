<script setup>
import { computed } from 'vue'
import StatusTag from './StatusTag.vue'

const props = defineProps({ books: Array, profiles: Array })
const emit = defineEmits(['scan', 'detail', 'profileChange'])

const manualProfiles = computed(() => (props.profiles || []).filter(p => p.name !== 'auto'))

function renderSuggestions(book) {
  const suggestions = book.profile_suggestions || []
  if (!suggestions.length) return null
  return suggestions.map(s => {
    const words = (s.matched_keywords || []).slice(0, 5).join('、')
    return {
      name: s.display_name || s.name,
      score: s.score,
      words,
      title: words
    }
  })
}

function isBusy(book) {
  return book.status === 'queued' || book.status === 'running'
}

function profileValues(book) {
  const value = book.profile
  if (Array.isArray(value)) return value
  return value ? [value] : ['auto']
}

function isAutoProfile(book) {
  return profileValues(book).includes('auto')
}

function isProfileChecked(book, profileName) {
  return profileValues(book).includes(profileName)
}

function toggleAuto(book) {
  emit('profileChange', book.id, 'auto')
}

function toggleManualProfile(book, profileName, checked) {
  const current = profileValues(book).filter(name => name !== 'auto')
  const next = checked
    ? Array.from(new Set([...current, profileName]))
    : current.filter(name => name !== profileName)
  emit('profileChange', book.id, next.length ? next : 'auto')
}

const displayBooks = computed(() => (props.books || []).map(book => ({
  ...book,
  suggestions: renderSuggestions(book) || []
})))
</script>

<template>
  <div class="card">
    <div class="card-title"><span class="icon">📖</span> 书籍列表</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>书名</th>
            <th>分类</th>
            <th>自动建议</th>
            <th>状态</th>
            <th>消息</th>
            <th style="text-align:right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!books.length">
            <td colspan="6" class="empty-cell">
              <div class="empty-state-small">
                <div class="icon">📂</div>
                <p>暂无书籍，请先上传小说</p>
              </div>
            </td>
          </tr>
          <tr v-for="book in displayBooks" :key="book.id">
            <td class="col-name">{{ book.name }}</td>
            <td>
              <div class="profile-picker">
                <label class="profile-option">
                  <input
                    type="checkbox"
                    :checked="isAutoProfile(book)"
                    :disabled="isBusy(book)"
                    @change="toggleAuto(book)"
                  />
                  <span>自动前三</span>
                </label>
                <div class="profile-options">
                  <label v-for="p in manualProfiles" :key="p.name" class="profile-option">
                    <input
                      type="checkbox"
                      :checked="isProfileChecked(book, p.name)"
                      :disabled="isBusy(book)"
                      @change="toggleManualProfile(book, p.name, $event.target.checked)"
                    />
                    <span>{{ p.display_name || p.name }}</span>
                  </label>
                </div>
              </div>
            </td>
            <td>
              <div class="suggestion-chips" v-if="book.suggestions.length">
                <span
                  v-for="(s, i) in book.suggestions"
                  :key="i"
                  class="chip"
                  :title="s.title"
                >
                  {{ s.name }} <span class="score">{{ s.score }}</span>
                </span>
              </div>
              <span v-else class="muted">暂无</span>
            </td>
            <td><StatusTag :status="book.status || 'idle'" /></td>
            <td class="col-msg">{{ book.message || '' }}</td>
            <td style="text-align:right">
              <div class="actions">
                <button
                  class="btn btn-sm"
                  @click="emit('scan', book.id)"
                  :disabled="isBusy(book)"
                >加入队列</button>
                <button
                  class="btn btn-sm btn-secondary"
                  @click="emit('detail', book.id)"
                >详情</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
/* Component-specific styles only */
</style>
