import type { AdbDevice, DatasetInfo, DeviceStatus, JobRecord, ModelInfo, MonthlyWorkflowStatus, ProjectRouteInfo } from '../types'

export function resolveApiPort(): string {
  try {
    const params = new URLSearchParams(window.location.search)
    const port = params.get('apiPort')
    if (port) {
      localStorage.setItem('bloompilot.apiPort', port)
      return port
    }
    const saved = localStorage.getItem('bloompilot.apiPort')
    if (saved) return saved
  } catch { /* storage unavailable */ }
  return '18777'
}

const API = `http://127.0.0.1:${resolveApiPort()}`
export const websocketUrl = () => `ws://127.0.0.1:${resolveApiPort()}/ws`

async function fetchWithRetry(url: string, options: RequestInit, attempts: number): Promise<Response> {
  let lastError: unknown
  for (let index = 0; index < attempts; index += 1) {
    try {
      return await fetch(url, options)
    } catch (error) {
      lastError = error
      await new Promise((resolve) => setTimeout(resolve, Math.min(400 + index * 250, 1800)))
    }
  }
  throw lastError ?? new Error('BloomPilot API unavailable')
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const options: RequestInit = { method, headers: { 'Content-Type': 'application/json' } }
  if (body !== undefined) options.body = JSON.stringify(body)
  const response = await fetchWithRetry(`${API}${path}`, options, method === 'GET' ? 15 : 1)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = payload?.detail
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail ?? response.statusText))
  }
  return response.json() as Promise<T>
}

export const health = () => request<{ ok: boolean; version: string }>('GET', '/api/health')
export const listDevices = () => request<AdbDevice[]>('GET', '/api/device/list')
export const connectDevice = (serial: string) =>
  request<DeviceStatus>('POST', '/api/device/connect', { serial })
export const disconnectDevice = () => request<DeviceStatus>('DELETE', '/api/device/connect')
export const getDeviceStatus = () => request<DeviceStatus>('GET', '/api/device/status')
export const deviceCommand = (command: string, payload: Record<string, unknown> = {}) =>
  request<{ ok: boolean; message: string; status: DeviceStatus; [key: string]: unknown }>('POST', '/api/device/command', { command, payload })
export const openPhoneControl = () =>
  request<{ ok: boolean; active: boolean; serial: string | null; pid: number | null }>('POST', '/api/device/screen-control')
export const closePhoneControl = () =>
  request<{ ok: boolean; active: boolean; serial: string | null; pid: number | null }>('DELETE', '/api/device/screen-control')
export type ViewportDirection = 'west' | 'east' | 'north' | 'south'
export const calibrateViewport = (payload: {
  serial: string
  action: 'capture' | 'step' | 'recenter'
  direction?: ViewportDirection
  settle_seconds?: number
  swipe_duration_ms?: number
}) => request<{ ok: boolean; action: 'capture' | 'step' | 'recenter'; direction: ViewportDirection | null; screen_width: number; screen_height: number; preview: string }>(
  'POST',
  '/api/device/viewport-calibration',
  payload,
)

export interface DeviceScaleCalibration {
  serial: string
  calibrated_at: string
  screen_width: number
  screen_height: number
  origin_lat: number
  origin_lng: number
  east_probe_m: number
  north_probe_m: number
  east_pixels: number
  north_pixels: number
  longitude_degrees_per_pixel: number
  latitude_degrees_per_pixel: number
  meters_per_pixel_x: number
  meters_per_pixel_y: number
}

export const getDeviceScaleCalibration = (serial: string) =>
  request<{ calibration: DeviceScaleCalibration | null; required: boolean }>(
    'GET',
    `/api/device/scale-calibration?serial=${encodeURIComponent(serial)}`,
  )

export const calibrateDeviceScale = (payload: { serial: string; settle_seconds?: number }) =>
  request<{ ok: boolean; calibration: DeviceScaleCalibration; preview: string }>(
    'POST',
    '/api/device/scale-calibration',
    payload,
  )

export const listJobs = () => request<{ active: JobRecord | null; jobs: JobRecord[] }>('GET', '/api/jobs')
export const startJob = (kind: string, payload: Record<string, unknown>) =>
  request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(kind)}`, payload)
export const stopJob = (id: string) => request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(id)}/stop`)
export const pauseRecognition = (id: string) =>
  request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(id)}/pause`)
export const skipGeoWaypoint = (id: string, waypointId: string) =>
  request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(id)}/skip-waypoint`, { waypoint_id: waypointId })
export const deleteGeoWaypoint = (id: string, waypointId: string) =>
  request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(id)}/delete-waypoint`, { waypoint_id: waypointId })
export const teleportWhilePaused = (id: string, payload: { lat: number; lng: number; label?: string }) =>
  request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(id)}/visit-hit`, payload)
export const resumeRecognition = (
  id: string,
  payload: { target_names?: string[]; participant_count?: number | null },
) => request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(id)}/resume`, payload)
export const cancelTrainingStop = (id: string) => request<{ job: JobRecord }>('POST', `/api/jobs/${encodeURIComponent(id)}/cancel-stop`)
export const pauseTraining = () => request<{ requested: string }>('POST', '/api/training/pause')
export const getTrainingStatus = () => request<Record<string, unknown>>('GET', '/api/training/status')

export const getModels = () => request<ModelInfo[]>('GET', '/api/models')
export const setDefaultModel = (modelPath: string) =>
  request<{ model: ModelInfo }>('PUT', '/api/models/default', { model_path: modelPath })
export const getDatasets = () => request<DatasetInfo[]>('GET', '/api/datasets')
export const getProjectRoutes = () => request<ProjectRouteInfo[]>('GET', '/api/routes')
export const getProjectRoute = (path: string) => request<unknown>('GET', `/api/routes/content?path=${encodeURIComponent(path)}`)
export const importProjectRoute = (sourceName: string, data: unknown) =>
  request<{ route: ProjectRouteInfo }>('POST', '/api/routes/import', { source_name: sourceName, data })
export const deleteProjectRoute = (path: string) =>
  request<{ route: ProjectRouteInfo }>('DELETE', `/api/routes?path=${encodeURIComponent(path)}`)
export const getMonthlyWorkflow = (cycle: string) =>
  request<MonthlyWorkflowStatus>('GET', `/api/monthly-activity/status?cycle=${encodeURIComponent(cycle)}`)
export const configureMonthlyWorkflow = (cycle: string) =>
  request<MonthlyWorkflowStatus>('PUT', '/api/monthly-activity/config', { cycle })
export const uploadMonthlyActivityTarget = (cycle: string, filename: string, data: string) =>
  request<MonthlyWorkflowStatus>('POST', '/api/monthly-activity/target', { cycle, filename, data })
export const monthlyBackgroundUrl = (cycle: string, path: string, version = 0) =>
  `${API}/api/monthly-activity/background?cycle=${encodeURIComponent(cycle)}&path=${encodeURIComponent(path)}&v=${version}`
export const deleteMonthlyBackground = (cycle: string, path: string) =>
  request<MonthlyWorkflowStatus>('DELETE', `/api/monthly-activity/background?cycle=${encodeURIComponent(cycle)}&path=${encodeURIComponent(path)}`)
export const approveMonthlyBackgrounds = (cycle: string) =>
  request<MonthlyWorkflowStatus>('POST', '/api/monthly-activity/approve-backgrounds', { cycle })
export const getSettings = () => request<{ settings: Record<string, unknown>; effective_config: Record<string, unknown> }>('GET', '/api/settings')
export const saveSettings = (settings: Record<string, unknown>) =>
  request<{ settings: Record<string, unknown>; effective_config: Record<string, unknown> }>('PUT', '/api/settings', settings)
export const testDiscordWebhook = (webhookUrl: string) =>
  request<{ ok: boolean; message: string }>('POST', '/api/notifications/discord/test', { webhook_url: webhookUrl })

export interface DiscordChannel {
  id: string
  name: string
  guild_name: string
  category_name: string
}

export interface DiscordNotificationConfig {
  mode: 'webhook' | 'bot'
  webhook_url: string
  bot_token: string
  channel_id: string
}

export const getDiscordBotChannels = (botToken: string) =>
  request<{ bot_name: string; channels: DiscordChannel[]; warnings: string[] }>(
    'POST', '/api/notifications/discord/channels', { bot_token: botToken },
  )

export const testDiscordNotification = (config: DiscordNotificationConfig) =>
  request<{ ok: boolean; message: string; channel_id?: string }>('POST', '/api/notifications/discord/test', config)
