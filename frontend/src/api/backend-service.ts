import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const filename = filenameFromDisposition(response.headers.get('Content-Disposition'))
  saveBlob(await response.blob(), filename ?? `${key}.csv`)
}

// ---------------------------------------------------------------------------
// 变更履历导出：按生效日期 + 变更ID 分批导出，支持中断续做和交付复核
// ---------------------------------------------------------------------------
export type ExportQuery = {
  from: string
  to: string
  changeId: string
  dim: string
  pageSize: number
}

export type ExportSession = {
  export_id: string
  status: string
  boundary: number
  boundary_source?: string
  date_from: string
  date_to: string
  change_id: string
  dim_code: string
  page_size: number
  total_rows: number
  total_pages: number
  pages_fetched: number[]
  remaining_pages?: number[]
}

export type ExportPage = {
  export_id: string
  boundary: number
  page: number
  total_rows: number
  total_pages: number
  next_page: number | null
  header: string
  csv: string
  rows: Record<string, string | number>[]
}

export type ExportManifest = {
  export_id: string
  status: string
  filename: string
  boundary: number
  date_from: string
  date_to: string
  change_id: string
  dim_code: string
  row_count: number
  page_size: number
  pages: number
  pages_fetched: number[]
  sha256: string
  delivered_at: string
}

export type ExportVerification = {
  export_id: string
  ok: boolean
  boundary: number
  row_count: number
  replay_row_count?: number
  sha256: string
  errors: string[]
}

function exportParams(query: ExportQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.from) params.set('from', query.from)
  if (query.to) params.set('to', query.to)
  if (query.changeId) params.set('change_id', query.changeId)
  if (query.dim) params.set('dim', query.dim)
  params.set('page_size', String(query.pageSize))
  return params
}

async function jsonOrThrow<T>(response: Response, message: string): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail?.detail ?? message)
  }
  return (await response.json()) as T
}

export async function createChangeExport(query: ExportQuery): Promise<ExportSession> {
  const response = await request(`/api/slow_change_log/exports?${exportParams(query)}`, {
    method: 'POST',
  })
  return jsonOrThrow<ExportSession>(response, '导出批次创建失败')
}

export async function fetchChangeExportPage(
  exportId: string,
  page: number,
): Promise<ExportPage> {
  const response = await request(`/api/slow_change_log/exports/${exportId}/pages/${page}`)
  return jsonOrThrow<ExportPage>(response, '导出分页读取失败')
}

export async function fetchChangeExportResume(exportId: string): Promise<ExportSession> {
  const response = await request(`/api/slow_change_log/exports/${exportId}/resume`)
  return jsonOrThrow<ExportSession>(response, '导出续做信息读取失败')
}

export async function completeChangeExport(exportId: string): Promise<ExportManifest> {
  const response = await request(`/api/slow_change_log/exports/${exportId}/complete`, {
    method: 'POST',
  })
  return jsonOrThrow<ExportManifest>(response, '导出收口失败')
}

export async function listChangeExports(): Promise<ExportManifest[]> {
  const response = await request('/api/slow_change_log/exports')
  if (!response.ok) throw new Error('交付台账读取失败')
  return await response.json()
}

export async function verifyChangeExport(exportId: string): Promise<ExportVerification> {
  const response = await request(`/api/slow_change_log/exports/${exportId}/verify`)
  return jsonOrThrow<ExportVerification>(response, '交付文件复核失败')
}

export async function downloadChangeExport(
  exportId: string,
  fallbackName?: string,
): Promise<void> {
  const response = await request(`/api/slow_change_log/exports/${exportId}/file`)
  if (!response.ok) throw new Error('交付文件下载失败')
  const filename = filenameFromDisposition(response.headers.get('Content-Disposition'))
  saveBlob(await response.blob(), filename ?? fallbackName ?? `${exportId}.csv`)
}

function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null
  const match = /filename="?([^";]+)"?/.exec(header)
  return match ? match[1] : null
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function listProcessingRecords(): Promise<Record<string, string | number>[]> {
  const response = await request('/api/slow_change_log/processing-records')
  if (!response.ok) throw new Error('处理记录读取失败')
  return await response.json()
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
