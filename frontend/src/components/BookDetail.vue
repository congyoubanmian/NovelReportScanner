<script setup>
import { ref, computed, watch } from 'vue'
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

const activeProfilesText = computed(() => {
  const profiles = props.book?.active_profiles || []
  if (profiles.length) return profiles.join('、')
  return props.book?.active_profile || '—'
})

const profileText = computed(() => {
  const value = props.book?.profile
  if (Array.isArray(value)) return value.join('、')
  return value || '—'
})

function resolvedProfilesText(task) {
  const profiles = task.resolved_profiles || []
  if (profiles.length) return profiles.join('、')
  return task.resolved_profile || '—'
}

const preview = ref({
  url: '',
  name: '',
  content: '',
  loading: false,
  error: '',
  isJson: false,
})

const PREVIEW_MAX_CHARS = 50000

function canPreview(name) {
  const lower = (name || '').toLowerCase()
  return lower.endsWith('.txt') || lower.endsWith('.md') || lower.endsWith('.json') || lower.endsWith('.log')
}

async function previewFile(file) {
  const url = file.url
  const name = file.name
  preview.value = { url, name, content: '', loading: true, error: '', isJson: false }
  try {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    let text = await res.text()
    if (text.length > PREVIEW_MAX_CHARS) {
      text = text.slice(0, PREVIEW_MAX_CHARS) + '\n\n... [内容已截断，文件过大请下载查看完整版]'
    }
    const isJson = name.toLowerCase().endsWith('.json')
    if (isJson) {
      try {
        const parsed = JSON.parse(text)
        text = JSON.stringify(parsed, null, 2)
      } catch {
        // 非法 JSON，按纯文本展示
      }
    }
    preview.value = { url, name, content: text, loading: false, error: '', isJson }
  } catch (e) {
    preview.value = { url, name, content: '', loading: false, error: e.message, isJson: false }
  }
}

function closePreview() {
  preview.value = { url: '', name: '', content: '', loading: false, error: '', isJson: false }
}

// Auto-close preview when switching books
watch(() => props.book?.id, () => {
  closePreview()
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
        <span><span class="label">当前分类:</span> {{ profileText }}</span>
        <span><span class="label">实际扫描:</span> {{ activeProfilesText }}</span>
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
            <button
              v-if="canPreview(f.name)"
              class="preview-btn"
              :class="{ active: preview.url === f.url }"
              @click="previewFile(f)"
            >👁️ 预览</button>
          </li>
        </ul>
        <p v-else class="muted">暂无输出文件</p>
      </div>

      <!-- Preview Panel -->
      <div class="preview-panel" v-if="preview.url">
        <div class="preview-header">
          <span class="preview-title">📄 {{ preview.name }}</span>
          <div class="preview-actions">
            <a :href="preview.url" target="_blank" class="log-link">下载</a>
            <button class="preview-close" @click="closePreview">✕</button>
          </div>
        </div>
        <div class="preview-body">
          <div v-if="preview.loading" class="preview-loading">加载中...</div>
          <div v-else-if="preview.error" class="preview-error">加载失败: {{ preview.error }}</div>
          <pre v-else :class="['preview-content', { 'preview-json': preview.isJson }]">{{ preview.content }}</pre>
        </div>
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
                <td>{{ resolvedProfilesText(t) }}</td>
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
/* File list with preview buttons */
.file-list li {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.file-list li a {
  flex: 1;
  min-width: 0;
}
.preview-btn {
  padding: 4px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 0.78rem;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.preview-btn:hover {
  border-color: var(--primary);
  color: var(--primary);
  background: var(--primary-light);
}
.preview-btn.active {
  border-color: var(--primary);
  color: var(--primary);
  background: var(--primary-light);
}

/* Preview panel */
.preview-panel {
  margin: 20px 0;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--bg-card);
}
.preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg-hover);
  border-bottom: 1px solid var(--border-color);
  gap: 12px;
}
.preview-title {
  font-weight: 600;
  font-size: 0.9rem;
  color: var(--text-heading);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.preview-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}
.preview-close {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.preview-close:hover {
  background: var(--danger-bg);
  color: var(--danger);
}
.preview-body {
  max-height: 500px;
  overflow: auto;
}
.preview-loading,
.preview-error {
  padding: 40px;
  text-align: center;
  font-size: 0.9rem;
}
.preview-error {
  color: var(--danger);
}
.preview-content {
  margin: 0;
  padding: 16px;
  font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 0.82rem;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-primary);
  background: transparent;
}
.preview-json {
  white-space: pre;
  overflow-x: auto;
}

@media (max-width: 768px) {
  .preview-body {
    max-height: 350px;
  }
  .preview-header {
    padding: 10px 12px;
  }
}
</style>
