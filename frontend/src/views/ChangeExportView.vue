<template>
  <section class="page" data-testid="change-export">
    <header class="page-head">
      <div>
        <h2>变更履历导出</h2>
        <p class="page-desc">
          按业务生效日期和变更ID导出SCD变更日志。每次交付都冻结日志截止点，
          补录的历史变更不会被带进已交付文件，可用台账里的边界随时复核。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/slow_change_log">返回变更日志</RouterLink>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="startExport">
      <label class="filter-item">
        <span>生效日期起</span>
        <input v-model="query.from" data-testid="export-from" placeholder="YYYY-MM-DD" />
      </label>
      <label class="filter-item">
        <span>生效日期止</span>
        <input v-model="query.to" data-testid="export-to" placeholder="YYYY-MM-DD" />
      </label>
      <label class="filter-item">
        <span>变更ID</span>
        <input v-model="query.changeId" data-testid="export-change-id" placeholder="按变更ID精确筛选" />
      </label>
      <label class="filter-item">
        <span>目标维度</span>
        <input v-model="query.dim" data-testid="export-dim" placeholder="如 DIM_CUSTOMER" />
      </label>
      <label class="filter-item">
        <span>每页行数</span>
        <input v-model.number="query.pageSize" data-testid="export-page-size" type="number" min="1" max="200" />
      </label>
      <button class="btn primary" data-testid="export-start" type="submit" :disabled="busy">
        新建导出批次
      </button>
    </form>

    <p class="status-legend">
      <span v-if="!session">尚未创建导出批次</span>
      <span v-else>
        批次 {{ session.export_id }} ｜ 截取边界 {{ session.boundary }}
        （{{ session.boundary_source === '沿用' ? '沿用原批次' : '本次新建' }}）
        ｜ 共 {{ session.total_rows }} 行 / {{ session.total_pages }} 页
        ｜ 已取回 {{ session.pages_fetched.length }} 页
      </span>
    </p>

    <div v-if="session" class="page-actions export-actions">
      <button class="btn" data-testid="export-next-page" type="button" :disabled="busy || !nextPage" @click="fetchNextPage">
        下载第 {{ nextPage ?? session.total_pages }} 页
      </button>
      <button class="btn ghost" data-testid="export-resume" type="button" :disabled="busy" @click="resumeExport">
        断线续做
      </button>
      <button class="btn" data-testid="export-complete" type="button" :disabled="busy" @click="completeExport">
        收口交付
      </button>
      <button class="btn ghost" data-testid="export-download" type="button" :disabled="!delivered" @click="downloadFile()">
        下载交付文件
      </button>
      <button class="btn ghost" data-testid="export-verify" type="button" :disabled="busy || !delivered" @click="verifyDelivered(delivered!.export_id)">
        按截取边界复核
      </button>
    </div>

    <table v-if="pageRows.length" class="data-table" data-testid="export-preview">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in pageRows" :key="String(row['变更ID'])">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
      </tbody>
    </table>

    <p v-if="verifyResult" class="verify-result" :class="verifyResult.ok ? 'verify-ok' : 'error-text'" data-testid="export-verify-result">
      复核 {{ verifyResult.export_id }}：
      {{ verifyResult.ok ? '通过' : '未通过' }}，
      边界 {{ verifyResult.boundary }}，行数 {{ verifyResult.row_count }}
      <span v-if="verifyResult.errors.length">｜ {{ verifyResult.errors.join('；') }}</span>
    </p>

    <h3 class="section-title">交付台账</h3>
    <table class="data-table" data-testid="export-ledger">
      <thead>
        <tr>
          <th>批次</th><th>状态</th><th>生效日期范围</th><th>变更ID</th>
          <th>目标维度</th><th>截取边界</th><th>行数</th><th>交付时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in manifests" :key="item.export_id" :data-testid="`ledger-${item.export_id}`">
          <td>{{ item.export_id }}</td>
          <td>{{ item.status }}</td>
          <td>{{ item.date_from || '不限' }} ~ {{ item.date_to || '不限' }}</td>
          <td>{{ item.change_id || '全部' }}</td>
          <td>{{ item.dim_code || '全部' }}</td>
          <td>{{ item.boundary }}</td>
          <td>{{ item.row_count }}</td>
          <td>{{ item.delivered_at }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`ledger-verify-${item.export_id}`" @click="verifyDelivered(item.export_id)">
              复核
            </button>
            <button class="link" type="button" :data-testid="`ledger-download-${item.export_id}`" @click="downloadFile(item)">
              下载
            </button>          </td>
        </tr>
        <tr v-if="!manifests.length">
          <td colspan="9" class="empty-state">暂无交付记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>交付文件首行标注日志截止点，所有行都落在该边界内</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  completeChangeExport,
  createChangeExport,
  downloadChangeExport,
  fetchChangeExportPage,
  fetchChangeExportResume,
  listChangeExports,
  verifyChangeExport,
  type ExportManifest,
  type ExportSession,
  type ExportVerification,
} from '@/api/backend-service'

const columns = ['变更ID', '目标维度', '变更字段', '前值', '后值', '生效日期', '日志序号', '变更时间', '变更原因']
const query = ref({ from: '', to: '', changeId: '', dim: '', pageSize: 20 })
const session = ref<ExportSession | null>(null)
const pageRows = ref<Record<string, string | number>[]>([])
const nextPage = ref<number | null>(1)
const delivered = ref<ExportManifest | null>(null)
const manifests = ref<ExportManifest[]>([])
const verifyResult = ref<ExportVerification | null>(null)
const errorMessage = ref('')
const busy = ref(false)

const lastFetchedPage = computed(() => {
  if (!session.value || !session.value.pages_fetched.length) return 0
  return Math.max(...session.value.pages_fetched)
})
void lastFetchedPage

async function refreshLedger() {
  manifests.value = await listChangeExports()
}

async function startExport() {
  busy.value = true
  errorMessage.value = ''
  verifyResult.value = null
  delivered.value = null
  try {
    session.value = await createChangeExport({ ...query.value })
    pageRows.value = []
    nextPage.value = 1
    await refreshLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出批次创建失败'
  } finally {
    busy.value = false
  }
}

async function fetchNextPage() {
  if (!session.value || !nextPage.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const page = await fetchChangeExportPage(session.value.export_id, nextPage.value)
    pageRows.value = page.rows
    session.value = {
      ...session.value,
      pages_fetched: [...new Set([...session.value.pages_fetched, page.page])].sort((a, b) => a - b),
    }
    nextPage.value = page.next_page
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出分页读取失败'
  } finally {
    busy.value = false
  }
}

async function resumeExport() {
  if (!session.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const resumed = await fetchChangeExportResume(session.value.export_id)
    session.value = resumed
    const remaining = resumed.remaining_pages ?? []
    nextPage.value = remaining.length ? remaining[0] : null
    pageRows.value = []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出续做失败'
  } finally {
    busy.value = false
  }
}

async function completeExport() {
  if (!session.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    delivered.value = await completeChangeExport(session.value.export_id)
    await refreshLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出收口失败'
  } finally {
    busy.value = false
  }
}

async function downloadFile(manifest?: ExportManifest) {
  const target = manifest ?? delivered.value
  if (!target) return
  try {
    await downloadChangeExport(target.export_id, target.filename)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付文件下载失败'
  }
}

async function verifyDelivered(exportId: string) {
  busy.value = true
  errorMessage.value = ''
  try {
    verifyResult.value = await verifyChangeExport(exportId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付文件复核失败'
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try {
    await refreshLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付台账读取失败'
  }
})
</script>
