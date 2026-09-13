import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { Coordinate, DeviceStatus, GeoSequence, GeoWaypoint, ModelInfo, ProjectRouteInfo } from '../types'
import { type JobUi, TargetMultiSelect, useRecognitionFilterDefaults, WorkLayout } from './WorkflowPanels'
import { ActionButton, Field, ModelPicker, PageHeader, Toggle } from './Primitives'
import { Icon } from './Icons'
import MapView from './MapView'
import { NumberDraft } from './NumberDraft'
import { editWaypoint, restoreGeometry } from '../utils/sequenceEditor'
import {
  GEO_SEQUENCE_PAGE_SIZE,
  INFERENCE_IMAGE_SIZE,
  MAX_GEO_SEQUENCE_FILE_BYTES,
  MAX_GEO_SEQUENCE_SEARCH_POINTS,
  MAX_GEO_SEQUENCE_RADIUS,
  MAX_GEO_SEQUENCE_WAYPOINTS,
} from '../projectDefaults'
import { coverageKmToRadius, gridStepKm, radiusToCoverageKm } from '../routeCoverage'
import { deleteProjectRoute, getProjectRoute, getProjectRoutes, importProjectRoute } from '../services/api'

interface Props {
  status: DeviceStatus
  models: ModelInfo[]
  onRefreshModels: () => Promise<void>
  onSetDefaultModel: (modelPath: string) => Promise<void>
  jobs: JobUi
  settings: Record<string, unknown>
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
  modeSwitch?: React.ReactNode
  onOpenDeviceControl: () => void
  active: boolean
}

const DEFAULT_COORDINATE: Coordinate = { lat: 25.033, lng: 121.5654 }

const id = () => globalThis.crypto?.randomUUID?.() ?? `geo-${Date.now()}-${Math.random().toString(16).slice(2)}`

const waypoint = (coordinate: Coordinate, index: number): GeoWaypoint => ({
  id: id(),
  name: `節點 ${index + 1}`,
  lat: coordinate.lat,
  lng: coordinate.lng,
  wait_seconds: 10,
  left_radius: 3,
  right_radius: 3,
  up_radius: 4,
  down_radius: 4,
  spacing_m: 100,
  range_unit: 'km',
  left_km: radiusToCoverageKm(3, 'horizontal'),
  right_km: radiusToCoverageKm(3, 'horizontal'),
  up_km: radiusToCoverageKm(4, 'vertical'),
  down_km: radiusToCoverageKm(4, 'vertical'),
  horizontal_step_km: gridStepKm('horizontal'),
  vertical_step_km: gridStepKm('vertical'),
})

const newSequence = (coordinate: Coordinate): GeoSequence => ({
  version: 1,
  id: id(),
  name: '新的座標序列',
  updated_at: new Date().toISOString(),
  waypoints: [waypoint(coordinate, 0)],
})

const numberValue = (value: unknown, label: string): number => {
  if (value === null || typeof value === 'boolean' || (typeof value === 'string' && !value.trim())) throw new Error('數值不能留空。')
  const result = Number(value)
  if (!Number.isFinite(result)) throw new Error(`${label} 必須是數字。`)
  return result
}

const waypointPointCount = (item: GeoWaypoint): number => (
  item.scan_spans
    ? item.scan_spans.reduce((total, [, startX, endX]) => total + endX - startX + 1, 0)
    : (item.left_radius + item.right_radius + 1) * (item.up_radius + item.down_radius + 1)
)

function normalizeSequence(value: unknown, allowEmpty = false): GeoSequence {
  const envelope = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const candidateValue = envelope.sequence ?? value
  const candidate = Array.isArray(candidateValue)
    ? { name: '匯入的座標序列', waypoints: candidateValue }
    : candidateValue && typeof candidateValue === 'object'
      ? candidateValue as Record<string, unknown>
      : null
  if (!candidate || !Array.isArray(candidate.waypoints) || (!allowEmpty && candidate.waypoints.length === 0)) {
    throw new Error('序列檔案必須包含至少一個 waypoints 節點。')
  }
  if (candidate.waypoints.length > MAX_GEO_SEQUENCE_WAYPOINTS) {
    throw new Error(`座標序列最多只能有 ${MAX_GEO_SEQUENCE_WAYPOINTS} 個節點。`)
  }
  const used = new Set<string>()
  const waypoints = candidate.waypoints.map((raw, index) => {
    if (!raw || typeof raw !== 'object') throw new Error(`第 ${index + 1} 個節點格式錯誤。`)
    const item = raw as Record<string, unknown>
    let waypointId = String(item.id || id())
    while (used.has(waypointId)) waypointId = id()
    used.add(waypointId)
    const horizontalFallback = numberValue(item.horizontal_radius ?? 3, `第 ${index + 1} 個左右半徑`)
    const verticalFallback = numberValue(item.vertical_radius ?? 4, `第 ${index + 1} 個上下半徑`)
    const horizontalStepKm = numberValue(item.horizontal_step_km ?? gridStepKm('horizontal'), `第 ${index + 1} 個水平格距`)
    const verticalStepKm = numberValue(item.vertical_step_km ?? gridStepKm('vertical'), `第 ${index + 1} 個垂直格距`)
    if (horizontalStepKm <= 0 || verticalStepKm <= 0) throw new Error(`第 ${index + 1} 個節點的格距必須大於 0。`)
    const leftKm = numberValue(item.left_km ?? radiusToCoverageKm(numberValue(item.left_radius ?? horizontalFallback, `第 ${index + 1} 個左側格數`), 'horizontal', horizontalStepKm), `第 ${index + 1} 個左側 km 範圍`)
    const rightKm = numberValue(item.right_km ?? radiusToCoverageKm(numberValue(item.right_radius ?? horizontalFallback, `第 ${index + 1} 個右側格數`), 'horizontal', horizontalStepKm), `第 ${index + 1} 個右側 km 範圍`)
    const upKm = numberValue(item.up_km ?? radiusToCoverageKm(numberValue(item.up_radius ?? verticalFallback, `第 ${index + 1} 個上方格數`), 'vertical', verticalStepKm), `第 ${index + 1} 個上方 km 範圍`)
    const downKm = numberValue(item.down_km ?? radiusToCoverageKm(numberValue(item.down_radius ?? verticalFallback, `第 ${index + 1} 個下方格數`), 'vertical', verticalStepKm), `第 ${index + 1} 個下方 km 範圍`)
    const normalized: GeoWaypoint = {
      id: waypointId,
      name: String(item.name || `節點 ${index + 1}`).slice(0, 120),
      lat: numberValue(item.lat, `第 ${index + 1} 個緯度`),
      lng: numberValue(item.lng, `第 ${index + 1} 個經度`),
      wait_seconds: numberValue(item.wait_seconds ?? 10, `第 ${index + 1} 個等待秒數`),
      left_radius: coverageKmToRadius(leftKm, 'horizontal', horizontalStepKm),
      right_radius: coverageKmToRadius(rightKm, 'horizontal', horizontalStepKm),
      up_radius: coverageKmToRadius(upKm, 'vertical', verticalStepKm),
      down_radius: coverageKmToRadius(downKm, 'vertical', verticalStepKm),
      spacing_m: numberValue(item.spacing_m ?? 100, `第 ${index + 1} 個間距`),
      range_unit: 'km',
      left_km: leftKm,
      right_km: rightKm,
      up_km: upKm,
      down_km: downKm,
      horizontal_step_km: horizontalStepKm,
      vertical_step_km: verticalStepKm,
      coverage_source: typeof item.coverage_source === 'string' ? item.coverage_source : undefined,
      settlement_class: typeof item.settlement_class === 'string' ? item.settlement_class : undefined,
      settlement_id: typeof item.settlement_id === 'string' ? item.settlement_id : undefined,
      settlement_match: typeof item.settlement_match === 'string' ? item.settlement_match : undefined,
      settlement_distance_km: item.settlement_distance_km == null ? undefined : numberValue(item.settlement_distance_km, `第 ${index + 1} 個聚落距離`),
    }
    if (normalized.lat < -90 || normalized.lat > 90 || normalized.lng < -180 || normalized.lng > 180) {
      throw new Error(`第 ${index + 1} 個節點的經緯度超出範圍。`)
    }
    if (normalized.wait_seconds < 0 || normalized.wait_seconds > 3600) throw new Error(`第 ${index + 1} 個節點的等待秒數無效。`)
    const radii = [normalized.left_radius, normalized.right_radius, normalized.up_radius, normalized.down_radius]
    if (radii.some((radius) => !Number.isInteger(radius))) throw new Error(`第 ${index + 1} 個節點的四向搜尋範圍必須是整數。`)
    if ([leftKm, rightKm, upKm, downKm].some((distance) => distance < 0 || distance > 10000)) throw new Error(`第 ${index + 1} 個節點的四向 km 範圍無效。`)
    if (radii.some((radius) => radius < 0 || radius > MAX_GEO_SEQUENCE_RADIUS)) throw new Error(`第 ${index + 1} 個節點換算後的四向格數無效。`)
    if (normalized.spacing_m <= 0 || normalized.spacing_m > 100000) throw new Error(`第 ${index + 1} 個節點的間距無效。`)
    if (item.scan_spans != null) {
      if (!Array.isArray(item.scan_spans) || item.scan_spans.length === 0) throw new Error(`第 ${index + 1} 個節點的有效格帶格式錯誤。`)
      const rows = new Map<number, Array<[number, number]>>()
      let includesCenter = false
      normalized.scan_spans = item.scan_spans.map((rawSpan, spanIndex) => {
        if (!Array.isArray(rawSpan) || rawSpan.length !== 3) throw new Error(`第 ${index + 1} 個節點的第 ${spanIndex + 1} 個有效格帶格式錯誤。`)
        const span = rawSpan.map((value) => numberValue(value, `第 ${index + 1} 個節點的有效格帶`)) as [number, number, number]
        const [rowY, startX, endX] = span
        if (!span.every(Number.isInteger) || rowY < -normalized.down_radius || rowY > normalized.up_radius || startX < -normalized.left_radius || endX > normalized.right_radius || startX > endX) {
          throw new Error(`第 ${index + 1} 個節點的第 ${spanIndex + 1} 個有效格帶超出範圍。`)
        }
        const intervals = rows.get(rowY) ?? []
        intervals.push([startX, endX])
        rows.set(rowY, intervals)
        includesCenter ||= rowY === 0 && startX <= 0 && endX >= 0
        return span
      })
      for (const intervals of rows.values()) {
        intervals.sort((left, right) => left[0] - right[0])
        if (intervals.some((interval, intervalIndex) => intervalIndex > 0 && interval[0] <= intervals[intervalIndex - 1][1])) throw new Error(`第 ${index + 1} 個節點的有效格帶彼此重疊。`)
      }
      if (!includesCenter) throw new Error(`第 ${index + 1} 個節點的有效格帶必須包含中心格。`)
    }
    return normalized
  })
  const totalPoints = waypoints.reduce((total, item) => total + waypointPointCount(item), 0)
  if (totalPoints > MAX_GEO_SEQUENCE_SEARCH_POINTS) {
    throw new Error(`序列總搜尋點數 ${totalPoints} 超過上限 ${MAX_GEO_SEQUENCE_SEARCH_POINTS}。`)
  }
  return {
    version: 1,
    id: String(candidate.id || id()),
    name: String(candidate.name || '匯入的座標序列').slice(0, 120),
    updated_at: String(candidate.updated_at || new Date().toISOString()),
    waypoints,
  }
}

function storedSequences(settings: Record<string, unknown>): GeoSequence[] {
  const values = settings.geo_sequences
  if (!Array.isArray(values)) return []
  const result: GeoSequence[] = []
  for (const value of values) {
    try { result.push(normalizeSequence(value)) } catch { /* ignore damaged saved entry */ }
  }
  return result
}

function routeFileName(sequence: GeoSequence): string {
  const stableId = sequence.id.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 36) || 'sequence'
  return `${sequence.name || 'sequence'}-${stableId}.json`
}

export default function GeoPanel({ status, models, onRefreshModels, onSetDefaultModel, jobs, settings, onSaveSettings, modeSwitch, onOpenDeviceControl, active }: Props) {
  const [model, setModel] = useState(models.find((item) => item.default)?.path ?? models[0]?.path ?? '')
  const [confidence, setConfidence] = useState(0.45)
  const [movement, setMovement] = useState<'teleport' | 'navigate'>('teleport')
  const [bookmark, setBookmark] = useState(true)
  const [returnOrigin, setReturnOrigin] = useState(true)
  const [randomizeWaypoints, setRandomizeWaypoints] = useState(false)
  const [recognitionControlBusy, setRecognitionControlBusy] = useState(false)
  const [skipBusy, setSkipBusy] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [starting, setStarting] = useState(false)
  const [projectRoutes, setProjectRoutes] = useState<ProjectRouteInfo[]>([])
  const [selectedProjectRoute, setSelectedProjectRoute] = useState('')
  const selectedProjectRouteRef = useRef(selectedProjectRoute)
  selectedProjectRouteRef.current = selectedProjectRoute
  const [routeBusy, setRouteBusy] = useState(false)
  const routeListVersion = useRef(0)
  const [sequence, setSequence] = useState<GeoSequence>(() => newSequence(status.current ?? DEFAULT_COORDINATE))
  const [sequenceDirty, setSequenceDirty] = useState(true)
  const [waypointPage, setWaypointPage] = useState(0)
  const [nodeQuery, setNodeQuery] = useState('')
  const [onlyFiltered, setOnlyFiltered] = useState(false)
  const edited = useRef(false)
  const originals = useRef(new Map<string, GeoWaypoint>())
  const initialCoordinate = useRef(Boolean(status.current))
  useEffect(() => {
    if (initialCoordinate.current || !status.current || edited.current || selectedProjectRoute) return
    initialCoordinate.current = true
    setSequence(newSequence(status.current))
  }, [status.current, selectedProjectRoute])
  const [discardAction, setDiscardAction] = useState<(() => void) | null>(null)
  const deferDiscard = (action: () => void) => {
    if (!edited.current || !sequenceDirty) return false
    setDiscardAction(() => action)
    return true
  }
  const createSequence = () => { setSelectedProjectRoute(''); setSequence(newSequence(status.current ?? DEFAULT_COORDINATE)); resetDraft(); setSequenceDirty(true); setWaypointPage(0) }
  const resetDraft = (clearFilter = true) => { edited.current = false; originals.current.clear(); if (clearFilter) { setNodeQuery(''); setOnlyFiltered(false) } }
  const validationError = useMemo(() => {
    try { normalizeSequence(sequence); return '' }
    catch (error) { return error instanceof Error ? error.message : String(error) }
  }, [sequence])
  const [sequenceMessage, setSequenceMessage] = useState<string | null>(null)
  const [savingSequence, setSavingSequence] = useState(false)
  const [deleteConfirmation, setDeleteConfirmation] = useState<ProjectRouteInfo | null>(null)
  const [sequenceManagerOpen, setSequenceManagerOpen] = useState(false)
  const [mapPickerOpen, setMapPickerOpen] = useState(false)
  const [mapDraft, setMapDraft] = useState<Coordinate>(status.current ?? DEFAULT_COORDINATE)
  const legacyMigrationAttempted = useRef(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const running = Boolean(
    jobs.active?.kind === 'geo_sequence_recognition'
    && ['queued', 'running', 'stopping'].includes(jobs.active?.state ?? ''),
  )
  const paused = Boolean(running && jobs.active?.paused)
  const pausePending = Boolean(running && jobs.active?.pause_requested)
  const deleting = deleteBusy || Boolean(running && jobs.active?.deleting_waypoint)
  const skipping = skipBusy || deleting || Boolean(running && jobs.active?.skipping_waypoint)
  const filterLocked = running && (!paused || Boolean(jobs.active?.resuming))
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
  useEffect(() => { if (active) void onRefreshModels() }, [active, onRefreshModels])
  useEffect(() => {
    if (!active) return
    let cancelled = false
    const version = ++routeListVersion.current
    void getProjectRoutes()
      .then((routes) => { if (!cancelled && version === routeListVersion.current) setProjectRoutes(routes) })
      .catch((error) => {
        if (!cancelled) setSequenceMessage(`無法掃描專案 routes：${error instanceof Error ? error.message : String(error)}`)
      })
    return () => { cancelled = true }
  }, [active])
  useEffect(() => {
    if (
      !active
      || legacyMigrationAttempted.current
      || !Object.prototype.hasOwnProperty.call(settings, 'geo_sequences')
    ) return
    legacyMigrationAttempted.current = true
    const legacySequences = storedSequences(settings)
    void (async () => {
      setRouteBusy(true)
      routeListVersion.current += 1
      try {
        for (const legacy of legacySequences) {
          await importProjectRoute(routeFileName(legacy), {
            schema: 'bloompilot.geo-sequence.v1',
            sequence: legacy,
          })
        }
        const routes = await getProjectRoutes()
        setProjectRoutes(routes)
        await onSaveSettings({ geo_sequences: null })
        setSequenceMessage(
          legacySequences.length
            ? `已將 ${legacySequences.length} 條本機序列遷移到專案 routes/imported。`
            : '已移除舊版空白本機序列設定。',
        )
      } catch (error) {
        setSequenceMessage(`本機序列遷移失敗，舊資料仍保留：${error instanceof Error ? error.message : String(error)}`)
      } finally {
        setRouteBusy(false)
      }
    })()
  }, [active, settings, onSaveSettings])
  useEffect(() => {
    if (!active) {
      setSequenceManagerOpen(false)
      setMapPickerOpen(false)
    }
  }, [active])
  useEffect(() => {
    if (!sequenceManagerOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !mapPickerOpen) setSequenceManagerOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [sequenceManagerOpen, mapPickerOpen])
  useEffect(() => {
    if (!mapPickerOpen) return
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMapPickerOpen(false) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [mapPickerOpen])

  useEffect(() => {
    if (!sequenceManagerOpen) { setDeleteConfirmation(null); setDiscardAction(null) }
  }, [sequenceManagerOpen])

  const totalPoints = useMemo(
    () => sequence.waypoints.reduce((total, item) => total + waypointPointCount(item), 0),
    [sequence.waypoints],
  )
  const filteredNodes = useMemo(() => sequence.waypoints.map((item, index) => ({ item, index })).filter(({ item }) => !nodeQuery.trim() || item.name.toLocaleLowerCase().includes(nodeQuery.trim().toLocaleLowerCase())), [sequence.waypoints, nodeQuery])
  const waypointPageCount = Math.max(1, Math.ceil(filteredNodes.length / GEO_SEQUENCE_PAGE_SIZE))
  const visibleWaypointStart = Math.min(waypointPage, waypointPageCount - 1) * GEO_SEQUENCE_PAGE_SIZE
  const visibleWaypoints = useMemo(
    () => filteredNodes.slice(visibleWaypointStart, visibleWaypointStart + GEO_SEQUENCE_PAGE_SIZE),
    [filteredNodes, visibleWaypointStart],
  )

  useEffect(() => {
    if (waypointPage >= waypointPageCount) setWaypointPage(waypointPageCount - 1)
  }, [waypointPage, waypointPageCount])

  const message = (value: string) => {
    setSequenceMessage(value)
    // Keep errors and save feedback visible until the next user action.
  }
  const refreshProjectRoutes = async (showMessage = false) => {
    setRouteBusy(true)
    routeListVersion.current += 1
    try {
      const routes = await getProjectRoutes()
      setProjectRoutes(routes)
      if (selectedProjectRoute && !routes.some((route) => route.path === selectedProjectRoute)) {
        setSelectedProjectRoute('')
      }
      if (showMessage) message(`已掃描專案 routes，共 ${routes.length} 條路線`)
      return routes
    } catch (error) {
      message(`掃描路線失敗：${error instanceof Error ? error.message : String(error)}`)
      return null
    } finally {
      setRouteBusy(false)
    }
  }
  const loadProjectRoute = async (path: string, discard = false) => {
    if (!discard && deferDiscard(() => void loadProjectRoute(path, true))) return
    if (!path) {
      setSelectedProjectRoute('')
      return
    }
    setRouteBusy(true)
    routeListVersion.current += 1
    try {
      const loaded = normalizeSequence(await getProjectRoute(path), true)
      setSelectedProjectRoute(path)
      resetDraft()
      setSequence(loaded)
      setSequenceDirty(false)
      setWaypointPage(0)
      message(`已載入「${loaded.name}」的 ${loaded.waypoints.length} 個節點`)
    } catch (error) {
      message(`載入路線失敗：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setRouteBusy(false)
    }
  }
  const updateWaypoint = (index: number, patch: Partial<GeoWaypoint>) => {
    setSequenceDirty(true)
    edited.current = true
    const source = sequence.waypoints[index]
    if (source?.scan_spans && !originals.current.has(source.id)) originals.current.set(source.id, source)
    setSequence((current) => ({
      ...current,
      waypoints: current.waypoints.map((item, itemIndex) => itemIndex === index ? editWaypoint(item, patch, originals.current.get(item.id)) : item),
    }))
  }
  const moveWaypoint = (index: number, direction: -1 | 1) => {
    const target = index + direction
    if (target < 0 || target >= sequence.waypoints.length) return
    edited.current = true
    setSequenceDirty(true)
    setSequence((current) => {
      const waypoints = [...current.waypoints]
      ;[waypoints[index], waypoints[target]] = [waypoints[target], waypoints[index]]
      return { ...current, waypoints }
    })
  }
  const addWaypoint = (coordinate: Coordinate) => {
    edited.current = true
    setSequenceDirty(true)
    setSequence((current) => ({ ...current, waypoints: [...current.waypoints, waypoint(coordinate, current.waypoints.length)] }))
  }
  const saveSequence = async () => {
    setSavingSequence(true)
    routeListVersion.current += 1
    try {
      const normalized = normalizeSequence({ ...sequence, updated_at: new Date().toISOString() })
      const selectedRoute = projectRoutes.find((route) => route.path === selectedProjectRoute)
      const selectedFileName = selectedRoute?.imported
        ? selectedRoute.path.split('/').at(-1)
        : undefined
      const result = await importProjectRoute(selectedFileName || routeFileName(normalized), {
        schema: 'bloompilot.geo-sequence.v1',
        sequence: normalized,
      })
      setProjectRoutes((routes) => [result.route, ...routes.filter((route) => route.path !== result.route.path)])
      resetDraft(false)
      setSequence(normalized)
      setSelectedProjectRoute(result.route.path)
      setSequenceDirty(false)
      message(`「${normalized.name}」已保存至 routes/${result.route.path}`)
      return { sequence: normalized, routePath: result.route.path }
    } catch (error) {
      message(`保存失敗：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setSavingSequence(false)
    }
  }
  const deleteSequence = async () => {
    const selectedRoute = deleteConfirmation
    if (!selectedRoute?.imported || routeBusy || savingSequence) return
    setSavingSequence(true)
    routeListVersion.current += 1
    try {
      await deleteProjectRoute(selectedRoute.path)
      // Commit deletion before any optional refresh can fail.
      setProjectRoutes((routes) => routes.filter((route) => route.path !== selectedRoute.path))
      setDeleteConfirmation(null)
      setSelectedProjectRoute('')
      setSequence(newSequence(status.current ?? DEFAULT_COORDINATE))
      setSequenceDirty(true)
      setWaypointPage(0)
      message(`已從 routes/${selectedRoute.path} 刪除序列`)
    } catch (error) {
      message(`刪除失敗：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setSavingSequence(false)
    }
  }
  const applyImport = async (sourceName: string, value: unknown) => {
    const imported = normalizeSequence(value)
    setRouteBusy(true)
    routeListVersion.current += 1
    try {
      const result = await importProjectRoute(sourceName, {
        schema: 'bloompilot.geo-sequence.v1',
        sequence: imported,
      })
      setProjectRoutes((routes) => [result.route, ...routes.filter((route) => route.path !== result.route.path)])
      setSelectedProjectRoute(result.route.path)
      resetDraft()
      setSequence(imported)
      setSequenceDirty(false)
      setWaypointPage(0)
      message(`已匯入 ${imported.waypoints.length} 個節點並保存至 routes/${result.route.path}`)
    } finally {
      setRouteBusy(false)
    }
  }
  const importSequence = async (discard = false) => {
    if (!discard && deferDiscard(() => void importSequence(true))) return
    try {
      if (!window.electronAPI) {
        fileInput.current?.click()
        return
      }
      const result = await window.electronAPI.importGeoSequence()
      if (result) await applyImport(result.name, result.data)
    } catch (error) { message(error instanceof Error ? error.message : String(error)) }
  }
  const importBrowserFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    try {
      const file = event.target.files?.[0]
      if (file) {
        if (file.size > MAX_GEO_SEQUENCE_FILE_BYTES) throw new Error('座標序列檔案不能超過 32 MB。')
        await applyImport(file.name, JSON.parse(await file.text()))
      }
    } catch (error) { message(error instanceof Error ? error.message : String(error)) }
    finally { event.target.value = '' }
  }
  const exportSequence = async () => {
    try {
      const payload = {
        schema: 'bloompilot.geo-sequence.v1',
        exported_at: new Date().toISOString(),
        sequence: normalizeSequence(sequence, true),
      }
      if (window.electronAPI) {
        const result = await window.electronAPI.exportGeoSequence(sequence.name, payload)
        if (result.ok) message(`序列已匯出${result.path ? `：${result.path}` : ''}`)
        return
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${sequence.name || 'geo-sequence'}.json`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (error) { message(error instanceof Error ? error.message : String(error)) }
  }

  const start = async () => {
    if (starting || routeBusy || savingSequence) return
    let normalized: GeoSequence
    try { normalized = normalizeSequence(onlyFiltered ? { ...sequence, waypoints: filteredNodes.map(({ item }) => item) } : sequence) }
    catch (error) { message(error instanceof Error ? error.message : String(error)); return }
    setStarting(true)
    try {
      let routePath = selectedProjectRoute
      if (sequenceDirty || !routePath) {
        const saved = await saveSequence()
        if (!saved) return
        normalized = onlyFiltered ? normalizeSequence({ ...saved.sequence, waypoints: filteredNodes.map(({ item }) => item) }) : saved.sequence
        routePath = saved.routePath
      }
      await jobs.start('geo_sequence_recognition', {
        serial: status.serial,
        model_path: model,
        route_path: routePath,
        sequence_name: normalized.name,
        waypoints: normalized.waypoints,
        confidence,
        movement_mode: movement,
        bookmark_detection: bookmark,
        return_to_origin: returnOrigin,
        target_names: selectedTargetNames.length ? selectedTargetNames : undefined,
        participant_count: participantCount === '' ? undefined : Number(participantCount),
        randomize_waypoints: randomizeWaypoints,
      })
    } catch { /* App-level wrapper reports the error. */ }
    finally { setStarting(false) }
  }

  const pauseOrResume = async () => {
    if (!jobs.active || recognitionControlBusy || skipping) return
    if (pausePending) { await jobs.cancelRecognitionPause(jobs.active.id); return }
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

  const skipWaypoint = async () => {
    const job = jobs.active
    if (!job?.current_waypoint || skipping || recognitionControlBusy) return
    setSkipBusy(true)
    try { await jobs.skipGeoWaypoint(job.id, job.current_waypoint.waypoint_id) }
    catch { /* App-level wrapper reports the error. */ }
    finally { setSkipBusy(false) }
  }

  const deleteWaypoint = async () => {
    const job = jobs.active
    if (!job?.current_waypoint || !job.route_path || skipping || recognitionControlBusy || routeBusy || savingSequence) return
    const { waypoint_id: waypointId, waypoint_name: waypointName } = job.current_waypoint
    setDeleteBusy(true)
    try {
      await jobs.deleteGeoWaypoint(job.id, waypointId)
      // The source route belongs to the running job, even if the editor changed routes.
      if (selectedProjectRouteRef.current === job.route_path) {
        setSequence((current) => ({
          ...current,
          updated_at: new Date().toISOString(),
          waypoints: current.waypoints.filter((item) => item.id !== waypointId),
        }))
      }
      message(`已從 routes/${job.route_path} 永久刪除「${waypointName}」，正在前往下一節點；若已到最後一點則結束序列。`)
      await refreshProjectRoutes()
    } catch { /* App-level wrapper reports the error. */ }
    finally { setDeleteBusy(false) }
  }

  const defaultNodeCoordinate = () => (
    sequence.waypoints[sequence.waypoints.length - 1]
    ?? status.current
    ?? DEFAULT_COORDINATE
  )
  const openMapPicker = () => {
    const coordinate = status.current ?? defaultNodeCoordinate()
    setMapDraft({ lat: coordinate.lat, lng: coordinate.lng })
    setMapPickerOpen(true)
  }

  return <div className="page-shell geo-page">
    <PageHeader eyebrow="PHONE VISION" title="座標序列辨識" description="城市之間前往中心座標；城市內以滑動手機地圖搜尋有效聚落格。" actions={modeSwitch}/>
    <WorkLayout jobs={jobs} preview wideForm hitNotifications={{ settings, onSaveSettings }}>
      <div className="form-card sequence-workflow-card">
        <div className="sequence-run-context"><b>{running ? '執行中的路線' : '本次執行方式'}</b><span>{running ? (jobs.active?.sequence_name || jobs.active?.route_path || '座標序列') : (randomizeWaypoints ? '隨機順序' : '依列表順序')}</span>{running && jobs.active?.current_waypoint && <small>目前節點：{jobs.active.current_waypoint.waypoint_name}</small>}</div>
        <div className="device-context-card">
          <div className={`device-context-icon ${status.connected ? 'connected' : ''}`}><Icon name="phoneVision" size={19}/></div>
          <div><span>辨識裝置</span><b>{status.connected ? status.serial : '尚未連接手機'}</b><small>{status.current ? `${status.current.lat.toFixed(6)}, ${status.current.lng.toFixed(6)}` : '目前座標尚未取得'}</small></div>
          <button type="button" onClick={onOpenDeviceControl}>前往裝置控制</button>
        </div>
        <Field label="專案訓練模型"><ModelPicker models={models} value={model} onChange={setModel} onRefresh={onRefreshModels} onSetDefault={onSetDefaultModel}/></Field>
        <div className="notice">推論尺寸固定 {INFERENCE_IMAGE_SIZE}。</div>

        <section className="embedded-section">
          <div className="section-title">命中篩選</div>
          <Field label="目標種類（可複選）"><TargetMultiSelect options={modelTargetNames} value={selectedTargetNames} onChange={setSelectedTargetNames} disabled={filterLocked}/></Field>
          <Field label="人數" hint="留空代表不限；0 代表沒有顯示數字"><input className="input" type="number" min="0" step="1" placeholder="不限" value={participantCount} disabled={filterLocked} onChange={(event) => setParticipantCount(event.target.value)}/></Field>
          {!participantCountValid && <div className="range-builder-error">人數必須是 0 以上的整數。</div>}
          {!targetSelectionValid && <div className="range-builder-error">請至少選擇一個目標。</div>}
          <ActionButton disabled={filterLocked || !participantCountValid || !targetSelectionValid || savingFilterDefaults} onClick={() => void saveFilterDefaults()}>{savingFilterDefaults ? '正在儲存…' : '儲存篩選為預設'}</ActionButton>
        </section>

        <section className="sequence-manager-entry embedded-section">
          <div className="sequence-manager-summary">
            <div className="sequence-manager-icon"><Icon name="route" size={18}/></div>
            <div><span>編輯中的路線</span><b>{sequence.name || '未命名序列'}</b><small>{sequence.waypoints.length.toLocaleString()} 個座標 · {totalPoints.toLocaleString()} 個搜尋點</small></div>
            <span className={`sequence-save-state ${sequenceDirty ? 'dirty' : 'saved'}`}>{routeBusy ? '處理中…' : savingSequence ? '保存中…' : sequenceDirty ? '未保存' : selectedProjectRoute ? '已保存至 routes' : '新序列'}</span>
          </div>
          <ActionButton icon="route" disabled={starting || deleteBusy} onClick={() => setSequenceManagerOpen(true)}>管理序列</ActionButton>
        </section>

        <details className="advanced-settings">
          <summary><span><b>進階執行設定</b><small>移動策略、信心與完成後行為</small></span><span>展開</span></summary>
          <div className="advanced-settings-body">
            <Field label="信心"><NumberDraft className="input" min={0.01} max={0.99} digits={2} value={confidence} onValue={setConfidence}/></Field>
            <Field label="節點間移動策略"><div className="segmented"><button className={movement === 'teleport' ? 'active' : ''} onClick={() => setMovement('teleport')}>瞬移（預設）</button><button className={movement === 'navigate' ? 'active' : ''} onClick={() => setMovement('navigate')}>導航</button></div></Field>
            <Toggle checked={bookmark} onChange={setBookmark} label="將命中座標加入手機收藏"/>
            <Toggle checked={returnOrigin} onChange={setReturnOrigin} label="序列完成後恢復執行前位置"/>
            <Toggle checked={randomizeWaypoints} onChange={setRandomizeWaypoints} label="每次隨機執行節點順序"/>
          </div>
        </details>

        {validationError && <div className="range-builder-error">{validationError}</div>}
        {sequenceMessage && <div className="sequence-message">{sequenceMessage}</div>}
        <div className="form-actions wrap sequence-controls">
          <ActionButton kind="primary" icon="play" disabled={!Number.isFinite(confidence) || confidence < 0.01 || confidence > 0.99 || Boolean(validationError) || (onlyFiltered && !filteredNodes.length) || !status.connected || !model || Boolean(jobs.active) || !sequence.waypoints.length || !participantCountValid || !targetSelectionValid || starting || routeBusy || savingSequence || deleteBusy} onClick={() => void start()}>{starting ? '正在保存並啟動…' : `執行 ${onlyFiltered ? filteredNodes.length : sequence.waypoints.length} 點 · ${randomizeWaypoints ? "隨機" : "依序"}`}</ActionButton>
          <ActionButton icon={paused ? 'play' : 'pause'} disabled={!running || jobs.active?.state !== 'running' || recognitionControlBusy || skipping || !participantCountValid || !targetSelectionValid} onClick={() => void pauseOrResume()}>{pausePending ? '取消暫停要求' : recognitionControlBusy ? paused ? '正在重啟並等待…' : '正在安全暫停…' : paused ? '繼續辨識' : '暫停辨識'}</ActionButton>
          <ActionButton icon="chevron" disabled={!running || jobs.active?.state !== 'running' || !jobs.active.current_waypoint || paused || pausePending || Boolean(jobs.active.resuming) || recognitionControlBusy || skipping} onClick={() => void skipWaypoint()}>{skipping && !deleting ? '正在跳過節點…' : '跳過目前節點'}</ActionButton>
          <ActionButton kind="danger" disabled={!running || jobs.active?.state !== 'running' || !jobs.active.current_waypoint || !jobs.active.route_path || paused || pausePending || Boolean(jobs.active.resuming) || recognitionControlBusy || skipping || routeBusy || savingSequence} onClick={() => void deleteWaypoint()}>{deleting ? '正在刪除並前往下一節點…' : '刪除目前節點並前往下一節點'}</ActionButton>
          {paused && <ActionButton icon="phoneVision" disabled={recognitionControlBusy} onClick={() => void jobs.openPhoneControl()}>重新開啟手機控制</ActionButton>}
          <ActionButton kind="danger" icon="stop" disabled={!running} onClick={() => void jobs.stop()}>停止辨識</ActionButton>
        </div>
        <p className="field-hint">跳過會保留節點；刪除會直接從目前執行的 route 永久移除節點並保存。兩者都保留已取得的結果，結束目前操作後前往下一點；最後一點則結束序列。暫停時請先繼續辨識。未保存的序列會在執行前自動保存至 routes。</p>
      </div>
    </WorkLayout>

    {sequenceManagerOpen && <div className="sequence-manager-backdrop" onMouseDown={() => setSequenceManagerOpen(false)}>
      <div className="sequence-manager-dialog" role="dialog" aria-modal="true" aria-label="序列管理" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="eyebrow">SEQUENCE MANAGER</span><h3>序列管理</h3></div><button type="button" aria-label="關閉序列管理" onClick={() => setSequenceManagerOpen(false)}>×</button></header>
        <fieldset className="sequence-manager-body sequence-manager-fields" disabled={routeBusy || savingSequence || starting || deleteBusy || Boolean(deleteConfirmation) || Boolean(discardAction)}>
          <section className="sequence-library">
            <div className="sequence-library-title"><div className="section-title">專案序列</div><span className={`sequence-save-state ${sequenceDirty ? 'dirty' : 'saved'}`}>{routeBusy ? '處理中…' : savingSequence ? '保存中…' : sequenceDirty ? '有未保存變更' : selectedProjectRoute ? '已保存至 routes' : '新序列'}</span></div>
            <Field label="routes 序列"><div className="picker-row"><select className="select-field" disabled={routeBusy} value={selectedProjectRoute} onChange={(event) => void loadProjectRoute(event.target.value)}><option value="">選擇序列（{projectRoutes.length}）</option>{projectRoutes.map((route) => <option key={route.path} value={route.path}>{route.imported ? '✎ ' : ''}{route.name}{route.waypoint_count !== null ? ` · ${route.waypoint_count.toLocaleString()} 點` : ''}</option>)}</select><button type="button" className="icon-btn" disabled={routeBusy} title="重新掃描 routes" onClick={() => void refreshProjectRoutes(true)}>↻</button><button type="button" className="icon-btn sequence-new" title="新增序列" onClick={() => { if (!deferDiscard(createSequence)) createSequence() }}>＋</button></div></Field>
            <Field label="序列名稱"><input className="input" value={sequence.name} maxLength={120} onChange={(event) => { edited.current = true; setSequence({ ...sequence, name: event.target.value }); setSequenceDirty(true) }}/></Field>
            <div className="sequence-toolbar"><button className="save-sequence" disabled={savingSequence || !sequenceDirty || Boolean(validationError)} onClick={() => void saveSequence()}>{savingSequence ? '保存中…' : '保存到專案 routes'}</button><button disabled={routeBusy} onClick={() => void importSequence()}>匯入 JSON</button><button onClick={() => void exportSequence()}>匯出 JSON</button><button className="danger-text" disabled={savingSequence || !projectRoutes.find((route) => route.path === selectedProjectRoute)?.imported} onClick={() => setDeleteConfirmation(projectRoutes.find((route) => route.path === selectedProjectRoute) ?? null)}>刪除自訂序列</button></div>
            <input ref={fileInput} className="hidden-file" type="file" accept="application/json,.json" onChange={(event) => void importBrowserFile(event)}/>
          </section>

          <section className="sequence-editor">
            <div className="sequence-editor-head"><div><span className="section-title">執行節點</span><small>{sequence.waypoints.length.toLocaleString()} 個座標 · {totalPoints.toLocaleString()} 個搜尋點</small></div></div>
            <Field label="搜尋城市／國家／節點名稱"><input className="input" value={nodeQuery} onChange={event => { setNodeQuery(event.target.value); setWaypointPage(0); setOnlyFiltered(false) }} placeholder="例如：Japan、Sangli"/></Field>
            <small>找到 {filteredNodes.length.toLocaleString()} 個節點</small>
            {nodeQuery.trim() && <Toggle label="本次只執行搜尋結果" checked={onlyFiltered} onChange={setOnlyFiltered}/>}
            {running && jobs.active?.current_waypoint && <button type="button" onClick={() => { const index = sequence.waypoints.findIndex(item => item.id === jobs.active?.current_waypoint?.waypoint_id); if (index < 0) { message('目前執行節點不在此編輯路線中。'); return }; setNodeQuery(''); setOnlyFiltered(false); setWaypointPage(Math.floor(index / GEO_SEQUENCE_PAGE_SIZE)) }}>跳到執行節點</button>}
            <div className="coordinate-source-actions">
              <button type="button" onClick={() => addWaypoint(defaultNodeCoordinate())}><Icon name="pin" size={14}/>手動新增</button>
              <button type="button" disabled={!status.current} onClick={() => status.current && addWaypoint(status.current)}><Icon name="phoneVision" size={14}/>手機目前座標</button>
              <button type="button" onClick={openMapPicker}><Icon name="map" size={14}/>開啟地圖選取</button>
            </div>
            {waypointPageCount > 1 && <div className="waypoint-pagination"><button type="button" disabled={waypointPage === 0} onClick={() => setWaypointPage((page) => Math.max(0, page - 1))}>上一頁</button><label>第 <input aria-label="節點頁碼" type="number" min="1" max={waypointPageCount} value={waypointPage + 1} onChange={(event) => setWaypointPage(Math.min(waypointPageCount - 1, Math.max(0, Number(event.target.value) - 1)))} /> / {waypointPageCount} 頁</label><button type="button" disabled={waypointPage >= waypointPageCount - 1} onClick={() => setWaypointPage((page) => Math.min(waypointPageCount - 1, page + 1))}>下一頁</button></div>}
            <div className="waypoint-list">{visibleWaypoints.map(({ item, index }) => {
              return <article className="waypoint-card" key={item.id}>
              <header><span className="waypoint-index">{String(index + 1).padStart(2, '0')}</span><input aria-label={`節點 ${index + 1} 名稱`} value={item.name} maxLength={120} onChange={(event) => updateWaypoint(index, { name: event.target.value })}/><div className="waypoint-actions"><button title="上移" disabled={index === 0} onClick={() => moveWaypoint(index, -1)}>↑</button><button title="下移" disabled={index === sequence.waypoints.length - 1} onClick={() => moveWaypoint(index, 1)}>↓</button><button title="刪除節點" disabled={sequence.waypoints.length === 1} onClick={() => { edited.current = true; setSequence({ ...sequence, waypoints: sequence.waypoints.filter((_, itemIndex) => itemIndex !== index) }); setSequenceDirty(true) }}>×</button></div></header>
              <div className="coord-grid"><Field label="LAT"><NumberDraft className="input mono" min={-90} max={90} value={item.lat} onValue={lat => updateWaypoint(index, { lat })}/></Field><Field label="LNG"><NumberDraft className="input mono" min={-180} max={180} value={item.lng} onValue={lng => updateWaypoint(index, { lng })}/></Field></div>
              {originals.current.has(item.id) && !item.scan_spans && <div className="notice">座標或範圍已變更，建成區篩選已改為完整矩形；此節點會搜尋更多區域。<button type="button" onClick={() => updateWaypoint(index, restoreGeometry(item, originals.current.get(item.id)!))}>還原原座標與建成區範圍</button></div>}
              <div className="waypoint-settings"><Field label="到點等待 s"><NumberDraft className="input" min={0} max={3600} value={item.wait_seconds} onValue={wait_seconds => updateWaypoint(index, { wait_seconds })}/></Field><Field label={`向左範圍 km（${item.left_radius} 格）`}><NumberDraft className="input" min={0} max={10000} digits={3} value={item.left_km ?? 0} onValue={value => updateWaypoint(index, { left_km: value, left_radius: coverageKmToRadius(value, 'horizontal', item.horizontal_step_km) })}/></Field><Field label={`向右範圍 km（${item.right_radius} 格）`}><NumberDraft className="input" min={0} max={10000} digits={3} value={item.right_km ?? 0} onValue={value => updateWaypoint(index, { right_km: value, right_radius: coverageKmToRadius(value, 'horizontal', item.horizontal_step_km) })}/></Field><Field label={`向上範圍 km（${item.up_radius} 格）`}><NumberDraft className="input" min={0} max={10000} digits={3} value={item.up_km ?? 0} onValue={value => updateWaypoint(index, { up_km: value, up_radius: coverageKmToRadius(value, 'vertical', item.vertical_step_km) })}/></Field><Field label={`向下範圍 km（${item.down_radius} 格）`}><NumberDraft className="input" min={0} max={10000} digits={3} value={item.down_km ?? 0} onValue={value => updateWaypoint(index, { down_km: value, down_radius: coverageKmToRadius(value, 'vertical', item.vertical_step_km) })}/></Field></div>
            </article>})}</div>
          </section>
          {validationError && <div className="range-builder-error">{validationError}</div>}
          {sequenceMessage && <div className="sequence-message">{sequenceMessage}</div>}
        </fieldset>
        {discardAction && <div className="sequence-delete-confirmation" role="alertdialog" aria-label="未保存的序列修改">
          <p>此序列有未保存修改。要先保存，還是捨棄修改？</p>
          <ActionButton disabled={savingSequence} onClick={() => setDiscardAction(null)}>繼續編輯</ActionButton>
          <ActionButton disabled={savingSequence || Boolean(validationError)} onClick={() => void (async () => { const action = discardAction; if (await saveSequence()) { setDiscardAction(null); action() } })()}>保存後繼續</ActionButton>
          <ActionButton kind="danger" disabled={savingSequence} onClick={() => { const action = discardAction; setDiscardAction(null); action() }}>捨棄修改</ActionButton>
        </div>}
        {deleteConfirmation && <div className="sequence-delete-confirmation" role="alertdialog" aria-label="確認刪除序列">
          <p>從專案 routes 刪除「{deleteConfirmation.name}」？</p>
          <ActionButton disabled={savingSequence} onClick={() => setDeleteConfirmation(null)}>取消刪除</ActionButton>
          <ActionButton kind="danger" disabled={savingSequence} onClick={() => void deleteSequence()}>{savingSequence ? '刪除中…' : '確認刪除'}</ActionButton>
        </div>}
        <footer><div><span>目前序列</span><b>{sequence.name || '未命名序列'} · {sequence.waypoints.length.toLocaleString()} 個座標</b></div><ActionButton kind="primary" onClick={() => setSequenceManagerOpen(false)}>完成</ActionButton></footer>
      </div>
    </div>}

    {mapPickerOpen && <div className="map-picker-backdrop" onMouseDown={() => setMapPickerOpen(false)}>
      <div className="map-picker-dialog" role="dialog" aria-modal="true" aria-label="地圖選取座標" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><span className="eyebrow">COORDINATE PICKER</span><h3>從地圖選取節點座標</h3></div><button type="button" aria-label="關閉地圖" onClick={() => setMapPickerOpen(false)}>×</button></header>
        <div className="map-picker-map"><MapView position={status.current} selected={mapDraft} onSelect={setMapDraft}/></div>
        <footer><div><span>已選座標</span><b className="mono">{mapDraft.lat.toFixed(7)}, {mapDraft.lng.toFixed(7)}</b></div><div><ActionButton onClick={() => setMapPickerOpen(false)}>取消</ActionButton><ActionButton kind="primary" onClick={() => { addWaypoint(mapDraft); setMapPickerOpen(false) }}>加入序列</ActionButton></div></footer>
      </div>
    </div>}
  </div>
}
