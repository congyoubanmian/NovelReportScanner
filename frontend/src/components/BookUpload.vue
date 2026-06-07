<script setup>
import { ref, computed } from 'vue'

const props = defineProps({ profiles: Array })
const emit = defineEmits(['uploaded'])

const file = ref(null)
const profile = ref('auto')
const uploading = ref(false)
const dragover = ref(false)
const fileName = computed(() => file.value?.name || '')

function onFileChange(e) {
  if (e.target.files?.length) file.value = e.target.files[0]
}
function onDrop(e) {
  dragover.value = false
  const dropped = e.dataTransfer.files[0]
  if (dropped && dropped.name.toLowerCase().endsWith('.txt')) {
    file.value = dropped
  }
}

async function submit() {
  if (!file.value) return
  uploading.value = true
  const fd = new FormData()
  fd.append('file', file.value)
  fd.append('profile', profile.value)
  try {
    await fetch('/upload', { method: 'POST', body: fd })
    file.value = null
    emit('uploaded')
  } catch (e) {
    alert('上传失败: ' + e.message)
  } finally {
    uploading.value = false
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
        <input type="file" accept=".txt" @change="onFileChange" />
        <div class="label">
          <template v-if="fileName">
            <strong>{{ fileName }}</strong>
          </template>
          <template v-else>
            点击或拖拽上传 <strong>.txt</strong> 小说文件
          </template>
        </div>
      </div>
      <select v-model="profile">
        <option v-for="p in profiles" :key="p.name" :value="p.name">
          {{ p.display_name || p.name }}
        </option>
      </select>
      <button class="btn" @click="submit" :disabled="!file || uploading">
        {{ uploading ? '上传中...' : '上传' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.upload-wrap { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end; }
.file-input-wrapper {
  position: relative; flex: 1; min-width: 260px;
  border: 2px dashed #d1d5db; border-radius: 10px;
  padding: 20px; text-align: center; cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
  background: #fafafa;
}
.file-input-wrapper:hover, .file-input-wrapper.dragover {
  border-color: #4f46e5; background: #eef2ff;
}
.file-input-wrapper input[type="file"] {
  position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
}
.file-input-wrapper .label { color: #6b7280; font-size: 0.88rem; pointer-events: none; }
.file-input-wrapper .label strong { color: #4f46e5; font-weight: 600; }
select {
  font-family: inherit; font-size: 0.92rem; border-radius: 10px;
  outline: none; transition: all 0.15s ease;
  padding: 10px 14px; border: 1px solid #e5e7eb;
  background: #fff; color: #1f2937; cursor: pointer;
}
select:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,0.12); }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 22px; border: none; cursor: pointer; font-weight: 500;
  background: #4f46e5; color: white; text-decoration: none; border-radius: 10px;
  font-size: 0.92rem; transition: all 0.15s ease;
}
.btn:hover:not(:disabled) { background: #4338ca; transform: translateY(-1px); }
.btn:disabled { background: #d1d5db; cursor: not-allowed; transform: none; color: #9ca3af; }
@media (max-width: 768px) {
  .upload-wrap { flex-direction: column; align-items: stretch; }
  .file-input-wrapper { min-width: auto; }
}
</style>
