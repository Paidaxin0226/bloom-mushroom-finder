import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { DatasetInfo, DeviceStatus, JobRecord, ModelInfo } from '../types'
import type { DetectionLine, MemoryStatus } from '../hooks/useJobs'
import {
  calibrateViewport,
  getDatasets,
  type ViewportDirection,
} from '../services/api'
import { INFERENCE_IMAGE_SIZE, SQUARE_SIZE_PIXELS } from '../projectDefaults'
import { ActionButton, Field, JobConsole, JobProgress, ModelPicker, PageHeader, PreviewStage, Toggle } from './Primitives'
import { DiscordNotificationControls } from './DiscordNotificationControls'

export interface JobUi {
  active: JobRecord | null
  latest?: JobRecord | null
  cancelRecognitionPause: (jobId: string) => Promise<JobRecord>
  preview: string | null
  detections: DetectionLine[]
  memory: MemoryStatus | null
  logs: string[]
  start: (kind: string, payload: Record<string, unknown>) => Promise<JobRecord>
  stop: () => Promise<void>
  pauseRecognition: (jobId: string) => Promise<JobRecord>
  skipGeoWaypoint: (jobId: string, waypointId: string) => Promise<void>
  deleteGeoWaypoint: (jobId: string, waypointId: string) => Promise<JobRecord>
  openPhoneControl: () => Promise<void>
  teleportWhilePaused: (jobId: string, payload: { lat: number; lng: number; label?: string }) => Promise<JobRecord>
  resumeRecognition: (jobId: string, payload: { target_names?: string[]; participant_count?: number | null }) => Promise<JobRecord>
  pauseTraining: () => Promise<unknown>
  cancelTrainingStop: () => Promise<void>
}

const busyFor = (job: JobRecord | null, kind: string) => Boolean(job && job.kind === kind && ['queued', 'running', 'stopping'].includes(job.state))

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null
}

function settingsWithConfigSection(
  _settings: Record<string, unknown>,
  section: string,
  values: Record<string, unknown>,
): Record<string, unknown> {
  return { config_overrides: { [section]: values } }
}

function detectionLocationLabel(line: DetectionLine, detection: Record<string, unknown>): string {
  const precise = recordValue(detection.precise_coordinate)
  if (precise) {
    const lat = Number(precise.lat)
    const lng = Number(precise.lng)
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      return `精確 ${lat.toFixed(7)}, ${lng.toFixed(7)} · 未瞬移擷取`
    }
  }
  if (typeof detection.coordinate_error === 'string' && detection.coordinate_error) {
    return `精確座標未取得：${detection.coordinate_error}`
  }
  const estimate = recordValue(detection.estimated_coordinate)
  if (estimate) {
    const lat = Number(estimate.lat)
    const lng = Number(estimate.lng)
    const accuracy = Number(estimate.accuracy_m)
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      const suffix = Number.isFinite(accuracy) ? ` · 約 ±${Math.round(accuracy)} m` : ''
      return `約 ${lat.toFixed(7)}, ${lng.toFixed(7)}${suffix}`
    }
  }
  const location = recordValue(line.location)
  const point = recordValue(location?.point) ?? location
  const lat = Number(point?.lat)
  const lng = Number(point?.lng)
  return Number.isFinite(lat) && Number.isFinite(lng)
    ? `約 ${lat.toFixed(7)}, ${lng.toFixed(7)}`
    : '此模式沒有可用的中心座標'
}

function detectionCoordinate(detection: Record<string, unknown>): { lat: number; lng: number } | null {
  const coordinate = recordValue(detection.precise_coordinate) ?? recordValue(detection.estimated_coordinate)
  const lat = Number(coordinate?.lat)
  const lng = Number(coordinate?.lng)
  return Number.isFinite(lat) && Number.isFinite(lng) && lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180
    ? { lat, lng }
    : null
}

function detectionSourceLabel(line: DetectionLine): string {
  const location = recordValue(line.location)
  const point = recordValue(location?.point) ?? location
  const gridX = Number(point?.grid_x)
  const gridY = Number(point?.grid_y)
  const waypoint = typeof location?.waypoint_name === 'string' ? location.waypoint_name : ''
  const grid = Number.isFinite(gridX) && Number.isFinite(gridY)
    ? `滑動格 x=${gridX >= 0 ? '+' : ''}${gridX}, y=${gridY >= 0 ? '+' : ''}${gridY}`
    : ''
  return [waypoint, grid].filter(Boolean).join(' · ') || '目前畫面'
}

interface HitNotificationSettings {
  settings: Record<string, unknown>
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
}

export function WorkLayout({ children, jobs, preview = false, previewSource, wideForm = false, hitNotifications }: { children: React.ReactNode; jobs: JobUi; preview?: boolean; previewSource?: string | null; wideForm?: boolean; hitNotifications?: HitNotificationSettings }) {
  const [hitAction, setHitAction] = useState<string | null>(null)
  const source = previewSource === undefined ? jobs.preview : previewSource
  const hits = jobs.detections.flatMap((line) => line.detections.map((detection, index) => ({
    key: `${line.id}-${index}`,
    line,
    detection,
  }))).slice(-12).reverse()
  return <div className={`workflow-grid ${wideForm ? 'wide-form' : ''}`}>
    <div className="workflow-form">{children}</div>
    <div className="workflow-output">
      {preview && <PreviewStage source={source}/>}
      <div className="output-card"><div className="output-card-title">工作狀態</div><JobProgress job={jobs.active ?? jobs.latest ?? null}/>{jobs.active?.pause_requested && <div className="notice">已要求暫停，完成目前操作後自動暫停。<button onClick={() => void jobs.cancelRecognitionPause(jobs.active!.id)}>取消暫停要求</button></div>}{jobs.memory && <div className={`memory-watchdog ${jobs.memory.pressure ? 'pressure' : ''} ${jobs.memory.restarting ? 'restarting' : ''}`}><div><span>手機 RAM</span><b>{(jobs.memory.available_mib / 1024).toFixed(2)} / {(jobs.memory.total_mib / 1024).toFixed(2)} GB 可用</b></div><div><span>Pikmin</span><b>{jobs.memory.game_pss_mib === null ? '讀取中' : `${Math.round(jobs.memory.game_pss_mib)} MB`}</b></div><div><span>監控狀態</span><b>{jobs.memory.restarting ? '正在重啟遊戲' : jobs.memory.pressure ? `壓力 ${jobs.memory.consecutive_pressure}/2` : `正常 · 已重啟 ${jobs.memory.restart_count} 次`}</b></div></div>}<JobConsole logs={jobs.logs}/></div>
      {(hitNotifications || hits.length > 0) && <div className="output-card detections-list">
        <div className="output-card-title hit-card-title"><span>即時命中</span>{hitNotifications && <i className={recordValue(hitNotifications.settings.discord_webhook)?.enabled === true ? 'enabled' : ''}>{recordValue(hitNotifications.settings.discord_webhook)?.enabled === true ? 'DISCORD ON' : 'SCREEN ONLY'}</i>}</div>
        {hitNotifications && <DiscordNotificationControls {...hitNotifications}/>}
        <div className="detection-items">
        {hits.length === 0 && <div className="empty-state compact">尚無命中；開始辨識後會即時顯示於此。</div>}
        {hits.map(({ key, line, detection }) => {
          const crop = typeof detection.crop_data_url === 'string' ? detection.crop_data_url : ''
          const confidence = Number(detection.confidence)
          const participantCount = Number(detection.participant_count)
          const participantConfidence = Number(detection.participant_count_confidence)
          const participantMethod = String(detection.participant_count_method ?? '')
          const participantTitle = participantMethod === 'number_absent'
            ? '畫面沒有數字，依規則記為 0 人'
            : Number.isFinite(participantConfidence) ? `數字辨識信心 ${Math.round(participantConfidence * 100)}%` : undefined
          const coordinate = detectionCoordinate(detection)
          const active = jobs.active
          const recognitionJob = Boolean(
            active
            && active.id === line.jobId
            && ['screen_recognition', 'geo_sequence_recognition', 'live_recognition'].includes(active.kind)
            && active.state === 'running',
          )
          const performHitAction = async () => {
            setHitAction(key)
            try {
              if (coordinate && active?.paused) {
                await jobs.teleportWhilePaused(line.jobId, {
                  ...coordinate,
                  label: String(detection.class_name ?? 'target'),
                })
              }
            } catch {
              // App-level wrappers already show the API error as a toast.
            } finally {
              setHitAction(null)
            }
          }
          return <article className={`detection-row ${crop ? '' : 'without-crop'}`} key={key}>
            {crop && <img className="detection-crop" src={crop} alt={`${String(detection.class_name ?? 'target')} 命中裁切圖`}/>}
            <div className="detection-detail">
              <header><b>{String(detection.class_name ?? 'target')}</b>{Number.isFinite(confidence) && <span>{Math.round(confidence * 100)}%</span>}{Number.isInteger(participantCount) && participantCount >= 0 && <span title={participantTitle}>人數 {participantCount}</span>}</header>
              <strong>{detectionLocationLabel(line, detection)}</strong>
              <small>{detectionSourceLabel(line)}</small>
              {recognitionJob && <div className="detection-actions">
                <button
                  className="detection-action"
                  type="button"
                  disabled={hitAction !== null || !coordinate || !active?.paused || active?.resuming}
                  onClick={() => void performHitAction()}
                >
                  {hitAction === key
                    ? '正在瞬移…'
                    : coordinate
                      ? active?.resuming ? '正在恢復辨識' : active?.paused ? '瞬移' : '請先暫停辨識'
                      : '沒有精確座標'}
                </button>
              </div>}
            </div>
          </article>
        })}
        </div>
      </div>}
    </div>
  </div>
}

export function BackgroundPanel({ serial, jobs, settings, effective, onSaveSettings }: {
  serial: string
  jobs: JobUi
  settings: Record<string, unknown>
  effective: Record<string, unknown>
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
}) {
  const phone = (effective.phone_automation ?? {}) as Record<string, unknown>
  const [city, setCity] = useState('taipei')
  const [horizontal, setHorizontal] = useState(Number(phone.horizontal_radius ?? 3))
  const [vertical, setVertical] = useState(Number(phone.vertical_radius ?? 4))
  const [settle, setSettle] = useState(Number(phone.settle_seconds ?? 4))
  const [savingDefaults, setSavingDefaults] = useState(false)
  useEffect(() => {
    setHorizontal(Number(phone.horizontal_radius ?? 3))
    setVertical(Number(phone.vertical_radius ?? 4))
    setSettle(Number(phone.settle_seconds ?? 4))
  }, [effective])
  const running = busyFor(jobs.active, 'background_collection')
  const valid = Number.isInteger(horizontal) && horizontal >= 0
    && Number.isInteger(vertical) && vertical >= 0
    && Number.isFinite(settle) && settle >= 0
  const saveDefaults = async () => {
    if (!valid) return
    setSavingDefaults(true)
    try {
      await onSaveSettings(settingsWithConfigSection(settings, 'phone_automation', {
        settle_seconds: settle,
        horizontal_radius: horizontal,
        vertical_radius: vertical,
      }))
    } catch { /* App already reports the API error */ }
    finally { setSavingDefaults(false) }
  }
  return <div className="page-shell"><PageHeader eyebrow="CAPTURE PIPELINE" title="背景蒐集" description="以角色為中心逐圈拖曳地圖，保存乾淨的手機原始畫面。"/><WorkLayout jobs={jobs}><div className="form-card"><h3>掃描設定</h3><Field label="城市資料夾"><input className="input" value={city} onChange={(event) => setCity(event.target.value)}/></Field><div className="two-column"><Field label="左右半徑"><input className="input" type="number" min="0" step="1" value={horizontal} onChange={(event) => setHorizontal(Number(event.target.value))}/></Field><Field label="上下半徑"><input className="input" type="number" min="0" step="1" value={vertical} onChange={(event) => setVertical(Number(event.target.value))}/></Field></div><Field label="畫面載入等待（秒）"><input className="input" type="number" min="0" step="0.5" value={settle} onChange={(event) => setSettle(Number(event.target.value))}/></Field>{!valid && <div className="range-builder-error">範圍必須是 0 以上的整數，等待時間不能小於 0。</div>}<ActionButton disabled={!valid || savingDefaults} onClick={() => void saveDefaults()}>{savingDefaults ? '正在儲存…' : '儲存目前值為背景蒐集預設'}</ActionButton><div className="form-actions"><ActionButton kind="primary" icon="play" disabled={!serial || Boolean(jobs.active) || !valid} onClick={() => void jobs.start('background_collection', { serial, city, horizontal_radius: horizontal, vertical_radius: vertical, settle_seconds: settle })}>開始蒐集</ActionButton><ActionButton kind="danger" icon="stop" disabled={!running} onClick={() => void jobs.stop()}>停止並回中心</ActionButton></div></div></WorkLayout></div>
}

export function DatasetPanel({ jobs, inventory, settings, effective, onSaveSettings }: {
  jobs: JobUi
  inventory: Record<string, unknown> | null
  settings: Record<string, unknown>
  effective: Record<string, unknown>
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
}) {
  const generation = (effective.generation ?? {}) as Record<string, unknown>
  const [composites, setComposites] = useState(Number(generation.composites_per_background ?? 6))
  const [parallelWorkers, setParallelWorkers] = useState(Number(generation.parallel_workers ?? 4))
  const [savingDefaults, setSavingDefaults] = useState(false)
  useEffect(() => {
    setComposites(Number(generation.composites_per_background ?? 6))
    setParallelWorkers(Number(generation.parallel_workers ?? 4))
  }, [effective])
  const running = busyFor(jobs.active, 'dataset')
  const backgrounds = Array.isArray(inventory?.backgrounds) ? inventory?.backgrounds.length : Number(inventory?.background_count ?? 0)
  const targets = Array.isArray(inventory?.targets) ? inventory?.targets.length : Number(inventory?.target_count ?? 0)
  const validWorkers = Number.isInteger(parallelWorkers) && parallelWorkers > 0
  const validComposites = Number.isInteger(composites) && composites > 0
  const saveDefaults = async () => {
    if (!validWorkers || !validComposites) return
    setSavingDefaults(true)
    try {
      await onSaveSettings(settingsWithConfigSection(settings, 'generation', {
        composites_per_background: composites,
        square_size_pixels: SQUARE_SIZE_PIXELS,
        parallel_workers: parallelWorkers,
      }))
    } catch { /* App already reports the API error */ }
    finally { setSavingDefaults(false) }
  }
  return <div className="page-shell"><PageHeader eyebrow="SYNTHETIC DATA" title="資料集產生" description="從多城市背景與透明 target 產生固定 256px 方框資料集。"/><div className="metric-row"><div className="metric-card"><span>BACKGROUNDS</span><b>{backgrounds || 0}</b></div><div className="metric-card"><span>TARGETS</span><b>{targets || 0}</b></div><div className="metric-card accent"><span>EXPECTED IMAGES</span><b>{Number(backgrounds || 0) * composites}</b></div></div><WorkLayout jobs={jobs}><div className="form-card"><h3>合成參數</h3><Field label="每張背景合成數"><input className="input" type="number" min="1" step="1" value={composites} onChange={(event) => setComposites(Number(event.target.value))}/></Field><Field label="平行合成工作數" hint="正整數，無程式上限；實際速度與可用數量受 CPU／記憶體限制"><input className="input" type="number" min="1" step="1" value={parallelWorkers} onChange={(event) => setParallelWorkers(Number(event.target.value))}/></Field><div className="notice">方框固定 {SQUARE_SIZE_PIXELS} × {SQUARE_SIZE_PIXELS}px；{validWorkers ? `每完成 ${parallelWorkers} 張更新進度` : '請輸入正整數工作數'}，最後一批也會回報。</div>{(!validWorkers || !validComposites) && <div className="range-builder-error">合成數與平行工作數都必須是大於 0 的整數。</div>}<ActionButton disabled={!validWorkers || !validComposites || savingDefaults} onClick={() => void saveDefaults()}>{savingDefaults ? '正在儲存…' : '儲存目前值為資料集預設'}</ActionButton><div className="form-actions"><ActionButton kind="primary" icon="play" disabled={Boolean(jobs.active) || !validWorkers || !validComposites} onClick={() => void jobs.start('dataset', { composites_per_background: composites, parallel_workers: parallelWorkers })}>產生資料集</ActionButton><ActionButton kind="danger" icon="stop" disabled={!running} onClick={() => void jobs.stop()}>停止</ActionButton></div></div></WorkLayout></div>
}

export function TrainingPanel({ jobs, settings, effective, onSaveSettings }: {
  jobs: JobUi
  settings: Record<string, unknown>
  effective: Record<string, unknown>
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
}) {
  const training = (effective.training ?? {}) as Record<string, unknown>
  const [dataset, setDataset] = useState('')
  const [datasets, setDatasets] = useState<DatasetInfo[]>([])
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState('')
  const [cancelling, setCancelling] = useState(false)
  const [epochs, setEpochs] = useState(Number(training.epochs ?? 100))
  const [imageSize, setImageSize] = useState(Number(training.image_size ?? 1024))
  const [batch, setBatch] = useState(String(training.batch ?? 3))
  const [savingDefaults, setSavingDefaults] = useState(false)
  const running = busyFor(jobs.active, 'training')
  const stopping = running && jobs.active?.state === 'stopping'
  const scan = async () => {
    setScanning(true)
    setScanError('')
    try {
      const found = await getDatasets()
      setDatasets(found)
      setDataset((selected) => found.some((item) => item.path === selected) ? selected : found[0]?.path ?? '')
    } catch (error) { setScanError(error instanceof Error ? error.message : String(error)) }
    finally { setScanning(false) }
  }
  useEffect(() => { void scan() }, [])
  useEffect(() => {
    setEpochs(Number(training.epochs ?? 100))
    setImageSize(Number(training.image_size ?? 1024))
    setBatch(String(training.batch ?? 3))
  }, [effective])
  const cancelStop = async () => {
    setCancelling(true)
    try { await jobs.cancelTrainingStop() } finally { setCancelling(false) }
  }
  const trainingValid = Number.isInteger(epochs) && epochs > 0
    && Number.isInteger(imageSize) && imageSize > 0
    && Number.isInteger(Number(batch)) && Number(batch) > 0
  const saveDefaults = async (): Promise<boolean> => {
    if (!trainingValid) return false
    setSavingDefaults(true)
    try {
      await onSaveSettings(settingsWithConfigSection(settings, 'training', {
        epochs,
        image_size: imageSize,
        batch: Number(batch),
      }))
      return true
    } catch { return false }
    finally { setSavingDefaults(false) }
  }
  const startTraining = async () => {
    if (!await saveDefaults()) return
    try { await jobs.start('training', { dataset }) }
    catch { /* App already reports the API error */ }
  }
  return <div className="page-shell"><PageHeader eyebrow="ULTRALYTICS ENGINE" title="模型訓練" description="直接使用專案生成資料；安全停止會在本輪 checkpoint 保存後生效。"/><WorkLayout jobs={jobs}><div className="form-card"><h3>訓練來源與參數</h3><Field label="專案資料集" hint="自動掃描 data/generated，預設選取目前資料集"><select className="select-field" value={dataset} disabled={scanning || running} onChange={(event) => setDataset(event.target.value)}>{!datasets.length && <option value="">{scanning ? '正在掃描…' : '尚無資料集，請先產生'}</option>}{datasets.map((item) => <option key={item.path} value={item.path}>{item.latest ? '★ ' : ''}{item.name} · {item.image_count} 張 · {item.class_count} 類</option>)}</select></Field><ActionButton icon="refresh" disabled={scanning || running} onClick={() => void scan()}>重新掃描資料集</ActionButton>{scanError && <div className="range-builder-error">{scanError}</div>}<div className="three-column"><Field label="EPOCHS"><input className="input" type="number" min="1" step="1" disabled={running} value={epochs} onChange={(event) => setEpochs(Number(event.target.value))}/></Field><Field label="訓練 IMAGE SIZE"><input className="input" type="number" min="1" step="1" disabled={running} value={imageSize} onChange={(event) => setImageSize(Number(event.target.value))}/></Field><Field label="BATCH 上限"><input className="input" type="number" min="1" step="1" disabled={running} value={batch} onChange={(event) => setBatch(event.target.value)}/></Field></div>{!trainingValid && <div className="range-builder-error">epochs、image size 與 batch 都必須是大於 0 的整數。</div>}<ActionButton disabled={running || savingDefaults || !trainingValid} onClick={() => void saveDefaults()}>{savingDefaults ? '正在儲存…' : '儲存目前值為訓練預設'}</ActionButton>{stopping && <div className="notice">正在等待本輪保存。停止尚未生效前可取消；已停止時請使用「開始／繼續訓練」。</div>}<div className="form-actions wrap"><ActionButton kind="primary" icon="play" disabled={Boolean(jobs.active) || !dataset || scanning || savingDefaults || !trainingValid} onClick={() => void startTraining()}>開始／繼續訓練</ActionButton><ActionButton icon="pause" disabled={!running || stopping} onClick={() => void jobs.pauseTraining()}>保存後暫停</ActionButton><ActionButton kind="danger" icon="stop" disabled={!running || stopping} onClick={() => void jobs.stop()}>安全停止</ActionButton>{stopping && <ActionButton icon="play" disabled={cancelling} onClick={() => void cancelStop()}>{cancelling ? '正在取消…' : '取消安全停止'}</ActionButton>}</div></div></WorkLayout></div>
}

interface RecognitionProps {
  status: DeviceStatus
  models: ModelInfo[]
  onRefreshModels: () => Promise<void>
  onSetDefaultModel: (modelPath: string) => Promise<void>
  jobs: JobUi
  settings: Record<string, unknown>
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
  headerActions?: React.ReactNode
}

const rangeDirections: { key: ViewportDirection; label: string }[] = [
  { key: 'west', label: '左' },
  { key: 'east', label: '右' },
  { key: 'north', label: '上' },
  { key: 'south', label: '下' },
]

export function TargetMultiSelect({ options, value, onChange, disabled = false }: { options: string[]; value: string[]; onChange: (value: string[]) => void; disabled?: boolean }) {
  const allSelected = options.length > 0 && options.every((name) => value.includes(name))
  const toggle = (name: string) => {
    onChange(value.includes(name) ? value.filter((item) => item !== name) : [...value, name])
  }
  return <div className="target-multi-select" role="group" aria-label="目標種類多選">
    <label className={`target-option all ${allSelected ? 'selected' : ''}`}><input type="checkbox" checked={allSelected} disabled={disabled || options.length === 0} onChange={(event) => onChange(event.target.checked ? [...options] : [])}/><span>全部目標</span><small>全選</small></label>
    {options.map((name) => <label className={`target-option ${value.includes(name) ? 'selected' : ''}`} key={name}><input type="checkbox" checked={value.includes(name)} disabled={disabled} onChange={() => toggle(name)}/><span>{name}</span></label>)}
    {options.length === 0 && <div className="target-options-empty">此模型沒有可讀取的類別清單，將辨識全部目標。</div>}
  </div>
}

export function useRecognitionFilterDefaults({ settings, options, model, onSaveSettings }: {
  settings: Record<string, unknown>
  options: string[]
  model: string
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
}) {
  const [selectedTargetNames, setSelectedTargetNames] = useState<string[]>([])
  const [participantCount, setParticipantCount] = useState('')
  const [savingFilterDefaults, setSavingFilterDefaults] = useState(false)
  const appliedDefaults = useRef('')
  const stored = recordValue(settings.recognition_filter_defaults)
  const storedNames = Array.isArray(stored?.target_names)
    ? stored.target_names.filter((name): name is string => typeof name === 'string')
    : null
  const storedAllTargets = stored?.all_targets === true
  const storedParticipant = stored?.participant_count
  const optionsKey = JSON.stringify(options)
  const defaultsKey = JSON.stringify([storedAllTargets, storedNames, storedParticipant])

  useEffect(() => {
    const key = `${model}\n${optionsKey}\n${defaultsKey}`
    if (appliedDefaults.current === key) return
    appliedDefaults.current = key
    const available = new Set(options)
    const selected = storedAllTargets ? options : storedNames?.filter((name) => available.has(name)) ?? options
    setSelectedTargetNames(options.length > 0 && selected.length === 0 ? [...options] : selected)
    const count = Number(storedParticipant)
    setParticipantCount(
      storedParticipant !== null && storedParticipant !== undefined && Number.isInteger(count) && count >= 0
        ? String(count)
        : '',
    )
  }, [model, optionsKey, defaultsKey])

  const participantCountValid = participantCount === '' || (Number.isInteger(Number(participantCount)) && Number(participantCount) >= 0)
  const targetSelectionValid = options.length === 0 || selectedTargetNames.length > 0
  const saveFilterDefaults = async () => {
    if (!participantCountValid || !targetSelectionValid) return
    setSavingFilterDefaults(true)
    try {
      await onSaveSettings({
        recognition_filter_defaults: {
          all_targets: options.length === 0 || options.every((name) => selectedTargetNames.includes(name)),
          target_names: options.length > 0 ? selectedTargetNames : [],
          participant_count: participantCount === '' ? null : Number(participantCount),
        },
      })
    } catch { /* App already reports the API error */ }
    finally { setSavingFilterDefaults(false) }
  }

  return {
    selectedTargetNames,
    setSelectedTargetNames,
    participantCount,
    setParticipantCount,
    participantCountValid,
    targetSelectionValid,
    savingFilterDefaults,
    saveFilterDefaults,
  }
}

export function RecognitionPanel({ status, models, onRefreshModels, onSetDefaultModel, jobs, settings, onSaveSettings, headerActions }: RecognitionProps) {
  const serial = status.serial ?? ''
  const [model, setModel] = useState(models.find((item) => item.default)?.path ?? models[0]?.path ?? '')
  const [confidence, setConfidence] = useState(0.45)
  const [leftRadius, setLeftRadius] = useState(3)
  const [rightRadius, setRightRadius] = useState(3)
  const [upRadius, setUpRadius] = useState(4)
  const [downRadius, setDownRadius] = useState(4)
  const [builderOpen, setBuilderOpen] = useState(false)
  const [builderBusy, setBuilderBusy] = useState(false)
  const [builderIndex, setBuilderIndex] = useState(0)
  const [builderCounts, setBuilderCounts] = useState<Record<ViewportDirection, number>>({ west: 0, east: 0, north: 0, south: 0 })
  const [builderWait, setBuilderWait] = useState(2)
  const [builderPreview, setBuilderPreview] = useState<string | null>(null)
  const [builderMessage, setBuilderMessage] = useState('')
  const [builderError, setBuilderError] = useState('')
  const [recognitionControlBusy, setRecognitionControlBusy] = useState(false)
  const builderActive = useRef(false)
  const modelTargetNames = useMemo(() => models.find((item) => item.path === model)?.class_names ?? [], [models, model])
  const {
    selectedTargetNames,
    setSelectedTargetNames,
    participantCount,
    setParticipantCount,
    participantCountValid,
    targetSelectionValid,
    savingFilterDefaults,
    saveFilterDefaults,
  } = useRecognitionFilterDefaults({ settings, options: modelTargetNames, model, onSaveSettings })
  useEffect(() => { if (!models.some((item) => item.path === model)) setModel(models.find((item) => item.default)?.path ?? models[0]?.path ?? '') }, [models, model])
  useEffect(() => { void onRefreshModels() }, [onRefreshModels])
  useEffect(() => () => {
    if (builderActive.current && serial) {
      void calibrateViewport({ serial, action: 'recenter', settle_seconds: 0 }).catch(() => undefined)
    }
  }, [serial])
  const kind = 'screen_recognition'
  const running = busyFor(jobs.active, kind)
  const paused = Boolean(running && jobs.active?.paused)
  const filterLocked = running && (!paused || Boolean(jobs.active?.resuming))

  const pauseOrResume = async () => {
    if (!jobs.active || recognitionControlBusy) return
    setRecognitionControlBusy(true)
    try {
      if (paused) {
        await jobs.resumeRecognition(jobs.active.id, {
          target_names: selectedTargetNames.length ? selectedTargetNames : undefined,
          participant_count: participantCount === '' ? null : Number(participantCount),
        })
      } else {
        await jobs.pauseRecognition(jobs.active.id)
      }
    } catch { /* App-level wrapper reports the error. */ }
    finally { setRecognitionControlBusy(false) }
  }

  const currentDirection = rangeDirections[builderIndex] ?? rangeDirections[0]
  const runCalibration = async (action: 'step' | 'recenter', direction?: ViewportDirection) => {
    setBuilderBusy(true)
    setBuilderError('')
    try {
      const response = await calibrateViewport({
        serial,
        action,
        direction,
        settle_seconds: Math.max(0, builderWait),
      })
      setBuilderPreview(response.preview)
      return true
    } catch (error) {
      setBuilderError(error instanceof Error ? error.message : String(error))
      return false
    } finally {
      setBuilderBusy(false)
    }
  }

  const openRangeBuilder = async () => {
    builderActive.current = true
    setBuilderOpen(true)
    setBuilderIndex(0)
    setBuilderCounts({ west: 0, east: 0, north: 0, south: 0 })
    setBuilderPreview(null)
    setBuilderMessage('正在回到中心，準備建立左邊範圍…')
    if (await runCalibration('recenter')) setBuilderMessage('已回到中心。請逐格建立左邊範圍。')
  }

  const stepRangeBuilder = async () => {
    if (builderCounts[currentDirection.key] >= 50) return
    if (await runCalibration('step', currentDirection.key)) {
      const count = builderCounts[currentDirection.key] + 1
      setBuilderCounts((previous) => ({ ...previous, [currentDirection.key]: count }))
      setBuilderMessage(`${currentDirection.label}邊目前 ${count} 格；若還不夠，再按一次。`)
    }
  }

  const finishRangeDirection = async () => {
    if (!await runCalibration('recenter')) return
    if (builderIndex < rangeDirections.length - 1) {
      const next = rangeDirections[builderIndex + 1]
      setBuilderIndex(builderIndex + 1)
      setBuilderMessage(`${currentDirection.label}邊已記錄 ${builderCounts[currentDirection.key]} 格並回正。現在建立${next.label}邊。`)
      return
    }
    setLeftRadius(builderCounts.west)
    setRightRadius(builderCounts.east)
    setUpRadius(builderCounts.north)
    setDownRadius(builderCounts.south)
    builderActive.current = false
    setBuilderOpen(false)
    setBuilderMessage(`已套用範圍：左 ${builderCounts.west}、右 ${builderCounts.east}、上 ${builderCounts.north}、下 ${builderCounts.south}。`)
  }

  const cancelRangeBuilder = async () => {
    if (!await runCalibration('recenter')) return
    builderActive.current = false
    setBuilderOpen(false)
    setBuilderMessage('已取消建立範圍，手機已回到中心。')
  }

  const startRecognition = () => {
    void jobs.start(kind, {
      serial,
      model_path: model,
      confidence,
      left_radius: leftRadius,
      right_radius: rightRadius,
      up_radius: upRadius,
      down_radius: downRadius,
      precise_coordinates: true,
      target_names: selectedTargetNames.length ? selectedTargetNames : undefined,
      participant_count: participantCount === '' ? undefined : Number(participantCount),
    })
  }

  return <div className="page-shell">
    <PageHeader eyebrow="PHONE VISION" title="自動搜尋辨識" description="逐格滑動手機地圖；畫面變化時拍照辨識，穩定 1 秒後前往下一格。" actions={headerActions}/>
    <WorkLayout jobs={jobs} preview previewSource={builderOpen ? builderPreview : undefined} hitNotifications={{ settings, onSaveSettings }}>
      <div className="form-card">
        <h3>辨識設定</h3>
        <Field label="專案訓練模型"><ModelPicker models={models} value={model} onChange={setModel} onRefresh={onRefreshModels} onSetDefault={onSetDefaultModel}/></Field>
        <div className="section-title">命中篩選</div>
        <Field label="目標種類（可複選）"><TargetMultiSelect options={modelTargetNames} value={selectedTargetNames} onChange={setSelectedTargetNames} disabled={filterLocked}/></Field>
        <Field label="人數" hint="留空代表不限；0 代表沒有顯示數字"><input className="input" type="number" min="0" step="1" placeholder="不限" value={participantCount} disabled={filterLocked} onChange={(event) => setParticipantCount(event.target.value)}/></Field>
        {!participantCountValid && <div className="range-builder-error">人數必須是 0 以上的整數。</div>}
        {!targetSelectionValid && <div className="range-builder-error">請至少選擇一個目標。</div>}
        <ActionButton disabled={filterLocked || !participantCountValid || !targetSelectionValid || savingFilterDefaults} onClick={() => void saveFilterDefaults()}>{savingFilterDefaults ? '正在儲存…' : '儲存篩選為預設'}</ActionButton>
        <div className="two-column">
          <Field label="信心門檻"><input className="input" type="number" min="0.01" max="0.99" step="0.01" value={confidence} onChange={(event) => setConfidence(Number(event.target.value))}/></Field>
          <Field label="固定推論尺寸"><div className="notice">{INFERENCE_IMAGE_SIZE}</div></Field>
        </div>
        <>
          <div className="section-title">四向搜尋範圍</div>
          <div className="direction-range-grid">
            <Field label="左"><input className="input" type="number" min="0" max="50" disabled={builderOpen} value={leftRadius} onChange={(event) => setLeftRadius(Number(event.target.value))}/></Field>
            <Field label="右"><input className="input" type="number" min="0" max="50" disabled={builderOpen} value={rightRadius} onChange={(event) => setRightRadius(Number(event.target.value))}/></Field>
            <Field label="上"><input className="input" type="number" min="0" max="50" disabled={builderOpen} value={upRadius} onChange={(event) => setUpRadius(Number(event.target.value))}/></Field>
            <Field label="下"><input className="input" type="number" min="0" max="50" disabled={builderOpen} value={downRadius} onChange={(event) => setDownRadius(Number(event.target.value))}/></Field>
          </div>
          <div className="notice">搜尋矩形：{leftRadius + rightRadius + 1} × {upRadius + downRadius + 1}，共 {(leftRadius + rightRadius + 1) * (upRadius + downRadius + 1)} 格。</div>
          {!builderOpen && <ActionButton disabled={!serial || Boolean(jobs.active)} onClick={() => void openRangeBuilder()}>互動建立範圍</ActionButton>}
          {builderOpen && <div className="range-builder">
            <div className="range-builder-steps">{rangeDirections.map((direction, index) => <div key={direction.key} className={`range-builder-step ${index === builderIndex ? 'active' : ''} ${index < builderIndex ? 'done' : ''}`}><span>{direction.label}</span><b>{builderCounts[direction.key]}</b></div>)}</div>
            <div className="range-builder-heading"><div><span>目前建立</span><strong>{currentDirection.label}邊範圍</strong></div><Field label="預覽等待（秒）"><input className="input" type="number" min="0" max="30" step="0.5" disabled={builderBusy} value={builderWait} onChange={(event) => setBuilderWait(Number(event.target.value))}/></Field></div>
            <p>每按一次只移動一格，右側預覽會更新。覺得範圍足夠後，按回正繼續下一方向。</p>
            <div className="range-builder-actions">
              <ActionButton kind="primary" disabled={builderBusy || builderCounts[currentDirection.key] >= 50} onClick={() => void stepRangeBuilder()}>向{currentDirection.label}一格並預覽（+1）</ActionButton>
              <ActionButton disabled={builderBusy} onClick={() => void finishRangeDirection()}>{currentDirection.label}邊夠了，回正{builderIndex === rangeDirections.length - 1 ? '並套用' : '繼續'}</ActionButton>
              <ActionButton kind="danger" disabled={builderBusy} onClick={() => void cancelRangeBuilder()}>取消並回正</ActionButton>
            </div>
            {builderBusy && <div className="range-builder-status">手機動作中，正在等待預覽…</div>}
            {!builderBusy && builderMessage && <div className="range-builder-status">{builderMessage}</div>}
            {builderError && <div className="range-builder-error">{builderError}</div>}
          </div>}
          {!builderOpen && builderMessage && <div className="range-builder-status">{builderMessage}</div>}
        </>
        <div className="form-actions wrap">
          <ActionButton kind="primary" icon="play" disabled={!serial || !model || Boolean(jobs.active) || builderOpen || builderBusy || !status.current || !participantCountValid || !targetSelectionValid} onClick={startRecognition}>開始自動搜尋</ActionButton>
          <ActionButton icon={paused ? 'play' : 'pause'} disabled={!running || jobs.active?.state !== 'running' || recognitionControlBusy || !participantCountValid || !targetSelectionValid} onClick={() => void pauseOrResume()}>{recognitionControlBusy ? paused ? '正在重啟並等待…' : '正在安全暫停…' : paused ? '繼續辨識' : '暫停辨識'}</ActionButton>
          {paused && <ActionButton icon="phoneVision" disabled={recognitionControlBusy} onClick={() => void jobs.openPhoneControl()}>重新開啟手機控制</ActionButton>}
          <ActionButton kind="danger" icon="stop" disabled={!running} onClick={() => void jobs.stop()}>停止</ActionButton>
        </div>
      </div>
    </WorkLayout>
  </div>
}
