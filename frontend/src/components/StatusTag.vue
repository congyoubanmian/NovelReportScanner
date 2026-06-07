<script setup>
import { computed } from 'vue'

const props = defineProps({ status: String })

const map = {
  running: { cls: 'tag-running', icon: '▶️', label: '扫描中' },
  queued:  { cls: 'tag-queued',  icon: '⏳', label: '排队中' },
  completed:{cls:'tag-completed', icon:'✅', label:'已完成'},
  canceled:{cls:'tag-interrupted', icon:'⏹️', label:'已取消'},
  failed:  { cls: 'tag-failed',  icon: '❌', label: '失败' },
  interrupted:{cls:'tag-interrupted', icon:'⏸️', label:'中断'},
  idle:    { cls: 'tag-idle',    icon: '💤', label: '空闲' },
}
const m = computed(() => map[props.status] || { cls: 'tag-idle', icon: '', label: props.status || '未知' })
</script>

<template>
  <span class="tag" :class="m.cls">
    <span class="tag-dot" v-if="props.status === 'running'"></span>
    <span>{{ m.icon }} {{ m.label }}</span>
  </span>
</template>

<style scoped>
.tag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  white-space: nowrap;
  line-height: 1.4;
}
.tag-running { background: var(--info-bg); color: var(--info-text); }
.tag-queued  { background: var(--warning-bg); color: var(--warning-text); }
.tag-completed { background: var(--success-bg); color: var(--success-text); }
.tag-failed { background: var(--danger-bg); color: var(--danger-text); }
.tag-interrupted { background: var(--tag-interrupted-bg); color: var(--tag-interrupted-text); }
.tag-idle { background: var(--tag-idle-bg); color: var(--tag-idle-text); }
.tag-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; animation: pulse 1.8s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.25} }
</style>
