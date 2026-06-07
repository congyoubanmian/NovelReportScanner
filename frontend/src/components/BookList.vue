<script setup>
import { computed } from 'vue'
import StatusTag from './StatusTag.vue'

const props = defineProps({ books: Array, profiles: Array })
const emit = defineEmits(['scan', 'detail', 'profileChange'])

function profileOptions(selected) {
  return props.profiles || []
}

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
          <tr v-for="book in books" :key="book.id">
            <td class="col-name">{{ book.name }}</td>
            <td>
              <select
                :value="book.profile"
                @change="emit('profileChange', book.id, $event.target.value)"
                :disabled="isBusy(book)"
              >
                <option v-for="p in profiles" :key="p.name" :value="p.name">
                  {{ p.display_name || p.name }}
                </option>
              </select>
            </td>
            <td>
              <div class="suggestion-chips" v-if="renderSuggestions(book)?.length">
                <span
                  v-for="(s, i) in renderSuggestions(book)"
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
.card {
  background: #fff; border-radius: 14px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.04);
  padding: 28px; margin-bottom: 20px;
  border: 1px solid #e5e7eb;
}
.card-title {
  font-size: 1.1rem; font-weight: 600; margin: 0 0 20px;
  display: flex; align-items: center; gap: 10px; color: #111827;
}
.card-title .icon { font-size: 1.35rem; line-height: 1; }
.table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid #e5e7eb; }
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
thead { background: #f9fafb; }
th {
  padding: 12px 16px; text-align: left; font-weight: 600;
  color: #374151; font-size: 0.75rem; text-transform: uppercase;
  letter-spacing: 0.5px; border-bottom: 1px solid #e5e7eb; white-space: nowrap;
}
td { padding: 14px 16px; border-bottom: 1px solid #e5e7eb; vertical-align: middle; }
tbody tr { transition: background 0.15s; }
tbody tr:hover { background: #f9fafb; }
tbody tr:last-child td { border-bottom: none; }
.col-name { font-weight: 500; color: #111827; }
.col-msg { color: #6b7280; font-size: 0.82rem; max-width: 300px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.empty-cell { padding: 28px !important; }
.empty-state-small { text-align: center; color: #6b7280; }
.empty-state-small .icon { font-size: 2.5rem; margin-bottom: 8px; opacity: 0.5; }
.empty-state-small p { font-size: 0.9rem; }
.muted { color: #6b7280; font-size: 0.85rem; }
select {
  font-family: inherit; font-size: 0.82rem; border-radius: 8px;
  outline: none; padding: 6px 10px; border: 1px solid #e5e7eb;
  background: #fff; color: #1f2937; cursor: pointer;
}
select:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,0.12); }
select:disabled { background: #f3f4f6; cursor: not-allowed; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 4px;
  padding: 6px 14px; border: none; cursor: pointer; font-weight: 500;
  background: #4f46e5; color: white; border-radius: 8px; font-size: 0.82rem;
}
.btn:hover:not(:disabled) { background: #4338ca; }
.btn:disabled { background: #d1d5db; cursor: not-allowed; color: #9ca3af; }
.btn-secondary { background: #6b7280; }
.btn-secondary:hover { background: #4b5563; }
.suggestion-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 4px 10px; border-radius: 999px; font-size: 0.75rem;
  background: #f3f4f6; color: #374151; border: 1px solid #e5e7eb;
}
.chip .score { font-weight: 600; color: #4f46e5; }
@media (max-width: 768px) {
  .card { padding: 18px; }
  th, td { padding: 10px 12px; }
  .actions { justify-content: flex-start; }
}
</style>
