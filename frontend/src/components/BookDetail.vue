<script setup>
import { computed } from 'vue'
import StatusTag from './StatusTag.vue'

const props = defineProps({ book: Object })

const outputs = computed(() => props.book?.outputs || [])
const tasks = computed(() => {
  const list = props.book?.tasks || []
  return list.map(t => ({
    ...t,
    displayStatus: t.queue_position ? `${t.status} #${t.queue_position}` : t.status
  }))
})

const suggestions = computed(() => {
  const list = props.book?.profile_suggestions || []
  return list.map(s => ({
    name: s.display_name || s.name,
    score: s.score,
    words: (s.matched_keywords || []).slice(0, 5).join('、')
  }))
})
</script>

<template>
  <div class="card">
    <div class="card-title"><span class="icon">🔍</span> 书籍详情</div>

    <div v-if="!book" class="detail-empty">
      点击书籍列表中的「详情」查看任务历史和输出文件。
    </div>

    <template v-else>
      <div class="detail-header">
        <h3>{{ book.name }}</h3>
        <StatusTag :status="book.status" />
      </div>

      <div class="detail-meta">
        <span><span class="label">当前分类:</span> {{ book.profile }}</span>
        <span><span class="label">实际扫描:</span> {{ book.active_profile || '—' }}</span>
        <span>
          <span class="label">路径:</span>
          <code>{{ book.path }}</code>
        </span>
      </div>

      <div class="section" v-if="suggestions.length">
        <div class="section-title">🎯 自动建议</div>
        <div class="suggestion-chips">
          <span v-for="(s, i) in suggestions" :key="i" class="chip" :title="s.words">
            {{ s.name }} <span class="score">{{ s.score }}</span>
          </span>
        </div>
      </div>

      <div class="section">
        <div class="section-title">📁 输出文件</div>
        <ul class="file-list" v-if="outputs.length">
          <li v-for="f in outputs" :key="f.path">
            <a :href="f.url" target="_blank">{{ f.name }}</a>
          </li>
        </ul>
        <p v-else class="muted">暂无输出文件</p>
      </div>

      <div class="section">
        <div class="section-title">📜 任务历史</div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>任务ID</th><th>分类</th><th>实际分类</th><th>状态</th>
                <th>创建时间</th><th>结束/错误</th><th style="text-align:center">日志</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!tasks.length">
                <td colspan="7" class="empty-cell">暂无任务</td>
              </tr>
              <tr v-for="t in tasks" :key="t.id">
                <td class="mono">{{ t.id }}</td>
                <td>{{ t.profile }}</td>
                <td>{{ t.resolved_profile || '—' }}</td>
                <td><StatusTag :status="t.displayStatus" /></td>
                <td class="mono nowrap">{{ t.created_at || '—' }}</td>
                <td class="muted">{{ t.finished_at || t.error || '—' }}</td>
                <td style="text-align:center">
                  <a v-if="t.log_file" :href="t.log_file.url" target="_blank" class="log-link">📋 日志</a>
                  <span v-else>—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
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
.detail-empty { text-align: center; padding: 48px 20px; color: #6b7280; font-style: italic; font-size: 0.95rem; }
.detail-header { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }
.detail-header h3 { margin: 0; font-size: 1.25rem; color: #111827; }
.detail-meta { display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 22px; font-size: 0.88rem; color: #6b7280; }
.detail-meta span { display: inline-flex; align-items: center; gap: 6px; }
.detail-meta .label { font-weight: 500; color: #1f2937; }
.detail-meta code { background: #f3f4f6; padding: 2px 8px; border-radius: 4px; font-size: 0.78rem; font-family: monospace; }
.section { margin-bottom: 24px; }
.section:last-child { margin-bottom: 0; }
.section-title { font-size: 0.82rem; font-weight: 600; color: #374151; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.4px; }
.file-list { list-style: none; display: grid; gap: 8px; }
.file-list li a {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px; border-radius: 10px;
  background: #eef2ff; color: #4f46e5;
  text-decoration: none; font-weight: 500; font-size: 0.88rem;
  transition: all 0.15s;
}
.file-list li a:hover { background: #e0e7ff; transform: translateX(2px); }
.file-list li a::before { content: "📄"; }
.suggestion-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 12px; border-radius: 999px; font-size: 0.78rem;
  background: #f3f4f6; color: #374151; border: 1px solid #e5e7eb;
}
.chip .score { font-weight: 600; color: #4f46e5; }
.muted { color: #6b7280; font-size: 0.85rem; }
.table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid #e5e7eb; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
thead { background: #f9fafb; }
th { padding: 10px 14px; text-align: left; font-weight: 600; color: #374151; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #e5e7eb; white-space: nowrap; }
td { padding: 12px 14px; border-bottom: 1px solid #e5e7eb; vertical-align: middle; }
tbody tr:hover { background: #f9fafb; }
tbody tr:last-child td { border-bottom: none; }
.empty-cell { text-align: center; color: #6b7280; padding: 20px !important; }
.mono { font-family: monospace; font-size: 0.78rem; }
.nowrap { white-space: nowrap; }
.log-link { color: #4f46e5; text-decoration: none; font-weight: 500; font-size: 0.82rem; }
.log-link:hover { text-decoration: underline; }
@media (max-width: 768px) {
  .card { padding: 18px; }
  .detail-meta { gap: 10px; }
  th, td { padding: 8px 10px; }
}
</style>
