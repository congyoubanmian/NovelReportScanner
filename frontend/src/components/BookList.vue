<script setup>
import { computed, ref, watch } from 'vue'
import StatusTag from './StatusTag.vue'

const props = defineProps({ books: Array, profiles: Array })
const emit = defineEmits(['scan', 'batchScan', 'cancel', 'prioritize', 'moveQueued', 'delete', 'detail', 'profileChange'])
const selectedIds = ref([])

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

function isQueued(book) {
  return book.status === 'queued'
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

const selectableBooks = computed(() => displayBooks.value.filter(book => !isBusy(book)))
const selectableIds = computed(() => selectableBooks.value.map(book => book.id))
const selectedReadyIds = computed(() => selectedIds.value.filter(id => selectableIds.value.includes(id)))
const allReadySelected = computed(() => selectableIds.value.length > 0 && selectedReadyIds.value.length === selectableIds.value.length)

watch(() => props.books, () => {
  selectedIds.value = selectedReadyIds.value
}, { deep: true })

function toggleBookSelection(book, checked) {
  if (isBusy(book)) return
  selectedIds.value = checked
    ? Array.from(new Set([...selectedIds.value, book.id]))
    : selectedIds.value.filter(id => id !== book.id)
}

function toggleAllReady(checked) {
  selectedIds.value = checked ? selectableIds.value : []
}

function emitBatchScan() {
  const ids = selectedReadyIds.value
  if (!ids.length) return
  emit('batchScan', ids)
  selectedIds.value = []
}

function confirmDelete(book) {
  if (isBusy(book)) return
  if (!window.confirm(`确定删除《${book.name}》吗？这会删除上传的小说文件，历史报告会保留。`)) return
  emit('delete', book.id)
}
</script>

<template>
  <div class="card">
    <div class="card-title book-list-title">
      <span><span class="icon">📖</span> 书籍列表</span>
      <button
        class="btn btn-sm"
        :disabled="!selectedReadyIds.length"
        @click="emitBatchScan"
      >批量加入队列<span v-if="selectedReadyIds.length">({{ selectedReadyIds.length }})</span></button>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="select-col">
              <input
                type="checkbox"
                :checked="allReadySelected"
                :disabled="!selectableIds.length"
                @change="toggleAllReady($event.target.checked)"
              />
            </th>
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
            <td colspan="7" class="empty-cell">
              <div class="empty-state-small">
                <div class="icon">📂</div>
                <p>暂无书籍，请先上传小说</p>
              </div>
            </td>
          </tr>
          <tr v-for="book in displayBooks" :key="book.id">
            <td class="select-col">
              <input
                type="checkbox"
                :checked="selectedReadyIds.includes(book.id)"
                :disabled="isBusy(book)"
                @change="toggleBookSelection(book, $event.target.checked)"
              />
            </td>
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
                  v-if="isQueued(book)"
                  class="btn btn-sm btn-secondary"
                  @click="emit('cancel', book.id)"
                >取消排队</button>
                <button
                  v-if="isQueued(book)"
                  class="btn btn-sm btn-secondary"
                  @click="emit('moveQueued', book.id, 'up')"
                >上移</button>
                <button
                  v-if="isQueued(book)"
                  class="btn btn-sm btn-secondary"
                  @click="emit('moveQueued', book.id, 'down')"
                >下移</button>
                <button
                  v-if="isQueued(book)"
                  class="btn btn-sm btn-secondary"
                  @click="emit('prioritize', book.id)"
                >置顶</button>
                <button
                  class="btn btn-sm btn-secondary"
                  @click="emit('detail', book.id)"
                >详情</button>
                <button
                  class="btn btn-sm btn-danger"
                  @click="confirmDelete(book)"
                  :disabled="isBusy(book)"
                >删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.book-list-title {
  justify-content: space-between;
  gap: 12px;
}
.book-list-title > span {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}
.select-col {
  width: 42px;
  text-align: center;
}
.select-col input {
  cursor: pointer;
}
.select-col input:disabled {
  cursor: not-allowed;
}
</style>
