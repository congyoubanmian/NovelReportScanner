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
/* Component-specific styles only */
</style>
