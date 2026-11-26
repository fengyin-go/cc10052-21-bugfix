<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-if="meta.key === 'data_freshness'" class="legend-item pending-count" data-testid="pending-alert-count">
        待处置告警：{{ pendingAlertCount }}
      </span>
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <p v-if="successMessage" class="success-text" data-testid="action-success" role="status">
      {{ successMessage }}
    </p>
    <p v-if="errorMessage" class="error-text inline-error" data-testid="action-error" role="alert">
      {{ errorMessage }}
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div class="table-wrap">
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="String(row.id)">
        <tr :data-testid="`row-${row.id}`" :data-status="String(row.status)" :data-window="String(row.计算窗口 ?? '')">
          <td v-for="column in meta.fields" :key="column">
            <span :class="{ 'status-pill': column === '时效等级' || column === '当前状态', [`status-${row.status}`]: column === '时效等级' }">
              {{ row[column] ?? '—' }}
            </span>
          </td>
          <td>
            {{ row.status }}
            <span
              v-if="meta.key === 'data_freshness' && row.pending && row.status !== '正常'"
              class="pending-badge"
              :data-testid="`pending-alert-${row.id}`"
            >待处置告警</span>
          </td>
          <td class="row-actions">
            <input
              v-model="remarks[String(row.id)]"
              class="remark-input"
              :data-testid="`remark-${row.id}`"
              type="text"
              placeholder="凭证/处置说明（可选）"
            />
            <div class="action-buttons">
              <button
                v-for="action in meta.actions"
                :key="action"
                class="link"
                type="button"
                :data-testid="`action-${row.id}-${action}`"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
              <button
                class="link"
                type="button"
                :data-testid="`detail-${row.id}`"
                @click="toggleDetail(Number(row.id))"
              >
                凭证轨迹
              </button>
            </div>
          </td>
        </tr>
        <tr v-if="detailId === Number(row.id)" :data-testid="`detail-row-${row.id}`">
          <td :colspan="meta.fields.length + 2" class="evidence-cell">
            <div class="evidence-grid">
              <div><strong>告警凭证：</strong><span :data-testid="`credential-${row.id}`">{{ row.告警凭证 || row.缺测凭证 || '—' }}</span></div>
              <div><strong>作业批次：</strong>{{ row.作业批次 || '—' }}</div>
              <div><strong>实际观测时间：</strong>{{ row.实际观测时间 || '—' }}</div>
              <div><strong>缺数原因：</strong>{{ row.缺数原因 || '—' }}</div>
              <div class="evidence-full"><strong>办理轨迹：</strong>{{ row.办理轨迹摘要 || row.最近办理 || '—' }}</div>
            </div>
            <pre class="evidence-json" :data-testid="`evidence-detail-${row.id}`">{{ JSON.stringify(row, null, 2) }}</pre>
          </td>
        </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const remarks = ref<Record<string, string>>({})
const detailId = ref<number | null>(null)
function metricStatus(label: string, fallbackIndex: number): string {
  return meta.statuses.find((status) => label.includes(status)) ?? meta.statuses[fallbackIndex] ?? ''
}
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === metricStatus(meta.metrics[1], 1)).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === metricStatus(meta.metrics[2], meta.statuses.length - 1)).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)
const pendingAlertCount = computed(() =>
  rows.value.filter((row) => row.pending && row.status !== '正常' && row.status !== '已办结').length,
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

function toggleDetail(id: number) {
  detailId.value = detailId.value === id ? null : id
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  successMessage.value = ''
  const remark = remarks.value[String(row.id)]?.trim() ?? ''
  const context: Record<string, unknown> = {}
  if (remark.startsWith('{')) {
    try {
      Object.assign(context, JSON.parse(remark) as Record<string, unknown>)
    } catch {
      context.remark = remark
      context.reason = remark
    }
  } else if (remark) {
    context.remark = remark
    context.reason = remark
  }
  if (!context.告警凭证 && (row.告警凭证 || row.缺测凭证)) {
    context.告警凭证 = row.告警凭证 ?? row.缺测凭证
  }
  if (row.claim_token) context.claim_token = row.claim_token
  if (row.version !== undefined) context.version = row.version
  try {
    const result = await applyAction(meta.key, Number(row.id), action, context)
    if (!result.ok) throw new Error(result.message)
    successMessage.value = result.message
    if (result.entry && typeof result.entry === 'object') {
      const next = result.entry as EntryRow
      const index = rows.value.findIndex((item) => item.id === next.id)
      if (index >= 0) rows.value[index] = next
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
