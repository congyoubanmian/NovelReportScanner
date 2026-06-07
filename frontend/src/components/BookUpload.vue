<script setup>
import { ref, computed } from 'vue'
import { uploadBook } from '../api.js'

const props = defineProps({ profiles: Array })
const emit = defineEmits(['uploaded', 'error'])

const files = ref([])
const autoProfile = ref(true)
const selectedProfiles = ref([])
const uploading = ref(false)
const dragover = ref(false)

const fileNames = computed(() => files.value.map(f => f.name).join(', '))
const manualProfiles = computed(() => (props.profiles || []).filter(p => p.name !== 'auto'))

function toggleAuto() {
  autoProfile.value = true
  selectedProfiles.value = []
}

function toggleProfile(profileName, checked) {
  autoProfile.value = false
  if (checked) {
    selectedProfiles.value = Array.from(new Set([...selectedProfiles.value, profileName]))
  } else {
    selectedProfiles.value = selectedProfiles.value.filter(name => name !== profileName)
  }
  if (!selectedProfiles.value.length) {
    autoProfile.value = true
  }
}

function onFileChange(e) {
  if (e.target.files?.length) {
    files.value = Array.from(e.target.files).filter(f => f.name.toLowerCase().endsWith('.txt'))
  }
}

function onDrop(e) {
  dragover.value = false
  const dropped = Array.from(e.dataTransfer.files || [])
    .filter(f => f.name.toLowerCase().endsWith('.txt'))
  if (dropped.length) {
    files.value = dropped
  }
}

async function submit() {
  if (!files.value.length) return
  uploading.value = true
  let successCount = 0

  for (const file of files.value) {
    const fd = new FormData()
    fd.append('file', file)
    if (autoProfile.value || !selectedProfiles.value.length) {
      fd.append('profile', 'auto')
    } else {
      selectedProfiles.value.forEach(name => fd.append('profile', name))
    }
    try {
      await uploadBook(fd)
      successCount++
    } catch (e) {
      emit('error', `《${file.name}》上传失败: ${e.message}`)
    }
  }

  files.value = []
  uploading.value = false

  if (successCount > 0) {
    emit('uploaded')
  }
}
</script>

<template>
  <div class="card">
    <div class="card-title"><span class="icon">⬆️</span> 上传小说</div>
    <div class="upload-wrap">
      <div
        class="file-input-wrapper"
        :class="{ dragover }"
        @dragover.prevent="dragover = true"
        @dragleave.prevent="dragover = false"
        @drop.prevent="onDrop"
      >
        <input type="file" accept=".txt" multiple @change="onFileChange" />
        <div class="label">
          <template v-if="fileNames">
            <strong>{{ fileNames }}</strong>
          </template>
          <template v-else>
            点击或拖拽上传 <strong>.txt</strong> 小说文件（支持多选）
          </template>
        </div>
      </div>
      <div class="profile-picker">
        <label class="profile-option">
          <input type="checkbox" :checked="autoProfile" @change="toggleAuto" />
          <span>自动前三</span>
        </label>
        <div class="profile-options">
          <label v-for="p in manualProfiles" :key="p.name" class="profile-option">
            <input
              type="checkbox"
              :checked="selectedProfiles.includes(p.name)"
              @change="toggleProfile(p.name, $event.target.checked)"
            />
            <span>{{ p.display_name || p.name }}</span>
          </label>
        </div>
      </div>
      <button class="btn" @click="submit" :disabled="!files.length || uploading">
        {{ uploading ? '上传中...' : `上传${files.length > 1 ? ` (${files.length}个)` : ''}` }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.upload-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: flex-end;
}
.file-input-wrapper {
  position: relative;
  flex: 1;
  min-width: 260px;
  border: 2px dashed #d1d5db;
  border-radius: 10px;
  padding: 20px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
  background: var(--bg-upload);
}
[data-theme="dark"] .file-input-wrapper {
  border-color: #4b5563;
}
.file-input-wrapper:hover,
.file-input-wrapper.dragover {
  border-color: var(--primary);
  background: var(--primary-light);
}
.file-input-wrapper input[type="file"] {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
  width: 100%;
  height: 100%;
}
.file-input-wrapper .label {
  color: var(--text-muted);
  font-size: 0.88rem;
  pointer-events: none;
}
.file-input-wrapper .label strong {
  color: var(--primary);
  font-weight: 600;
}
select {
  font-family: inherit;
  font-size: 0.92rem;
  border-radius: 10px;
  outline: none;
  transition: all 0.15s ease;
  padding: 10px 14px;
  border: 1px solid var(--border-color);
  background: var(--bg-card);
  color: var(--text-primary);
  cursor: pointer;
}
select:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12);
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 22px;
  border: none;
  cursor: pointer;
  font-weight: 500;
  background: var(--primary);
  color: white;
  text-decoration: none;
  border-radius: 10px;
  font-size: 0.92rem;
  transition: all 0.15s ease;
}
.btn:hover:not(:disabled) {
  background: var(--primary-hover);
  transform: translateY(-1px);
}
.btn:disabled {
  background: #d1d5db;
  cursor: not-allowed;
  transform: none;
  color: #9ca3af;
}
@media (max-width: 768px) {
  .upload-wrap {
    flex-direction: column;
    align-items: stretch;
  }
  .file-input-wrapper {
    min-width: auto;
  }
}
</style>
