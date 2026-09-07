import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../services/api'
import type { JobRecord, ServerEvent } from '../types'

export interface DetectionLine {
  id: number
  jobId: string
  location: unknown
  detections: Array<Record<string, unknown>>
}

export interface MemoryStatus {
  total_mib: number
  available_mib: number
  available_percent: number
  game_pss_mib: number | null
  available_threshold_mib: number
  game_pss_threshold_mib: number
  pressure: boolean
  pressure_reason: string
  consecutive_pressure: number
  restart_count: number
  restarting: boolean
}

export function useJobs(subscribe: (handler: (event: ServerEvent) => void) => () => void) {
  const [active, setActive] = useState<JobRecord | null>(null)
  const [history, setHistory] = useState<JobRecord[]>([])
  const [preview, setPreview] = useState<string | null>(null)
  const [detections, setDetections] = useState<DetectionLine[]>([])
  const [memory, setMemory] = useState<MemoryStatus | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const detectionId = useRef(0)

  const refresh = useCallback(async () => {
    const result = await api.listJobs()
    setActive(result.active)
    setHistory(result.jobs)
  }, [])

  const start = useCallback(async (kind: string, payload: Record<string, unknown>) => {
    setPreview(null)
    setDetections([])
    setMemory(null)
    setLogs([])
    const result = await api.startJob(kind, payload)
    setActive(result.job)
    setHistory((items) => [result.job, ...items.filter((item) => item.id !== result.job.id)])
    return result.job
  }, [])

  const stop = useCallback(async () => {
    if (!active) return
    const result = await api.stopJob(active.id)
    setActive(result.job)
  }, [active])

  const pauseRecognition = useCallback(async (jobId: string) => {
    const result = await api.pauseRecognition(jobId)
    setActive(result.job)
    setHistory((items) => [result.job, ...items.filter((item) => item.id !== result.job.id)])
    return result.job
  }, [])

  const skipGeoWaypoint = useCallback(async (jobId: string, waypointId: string) => {
    await api.skipGeoWaypoint(jobId, waypointId)
    // The worker may already have advanced by the time the request returns.
    // Fetch current state instead of restoring the response's pending state.
    await refresh()
  }, [refresh])

  const deleteGeoWaypoint = useCallback(async (jobId: string, waypointId: string) => {
    const result = await api.deleteGeoWaypoint(jobId, waypointId)
    // Persistence already succeeded; a refresh failure must not hide that result.
    await refresh().catch(() => undefined)
    return result.job
  }, [refresh])

  const teleportWhilePaused = useCallback(async (
    jobId: string,
    payload: { lat: number; lng: number; label?: string },
  ) => {
    const result = await api.teleportWhilePaused(jobId, payload)
    setActive(result.job)
    setHistory((items) => [result.job, ...items.filter((item) => item.id !== result.job.id)])
    return result.job
  }, [])

  const resumeRecognition = useCallback(async (
    jobId: string,
    payload: { target_names?: string[]; participant_count?: number | null },
  ) => {
    const result = await api.resumeRecognition(jobId, payload)
    setActive(result.job)
    setHistory((items) => [result.job, ...items.filter((item) => item.id !== result.job.id)])
    return result.job
  }, [])

  const cancelTrainingStop = useCallback(async () => {
    if (!active || active.kind !== 'training' || active.state !== 'stopping') return
    const result = await api.cancelTrainingStop(active.id)
    setActive(result.job)
  }, [active])

  useEffect(() => subscribe((event) => {
    if (event.type === 'job_update' && event.job) {
      const job = event.job
      setHistory((items) => [job, ...items.filter((item) => item.id !== job.id)])
      if (['queued', 'running', 'stopping'].includes(job.state)) setActive(job)
      else setActive((current) => current?.id === job.id ? null : current)
      if (job.message) setLogs((items) => items.at(-1) === job.message ? items : [...items.slice(-199), job.message])
    }
    if (event.type === 'preview' && event.data_url) setPreview(event.data_url)
    if (event.type === 'memory' && event.memory) setMemory(event.memory as unknown as MemoryStatus)
    if (event.type === 'notification_error' && event.message) {
      setLogs((items) => items.at(-1) === event.message ? items : [...items.slice(-199), event.message!])
    }
    if (event.type === 'detections' && event.job_id) {
      const hits = (event.detections ?? []).filter((item) => item && typeof item === 'object')
      if (!hits.length) return
      detectionId.current += 1
      setDetections((items) => {
        const next = [...items, {
          id: detectionId.current,
          jobId: event.job_id!,
          location: event.location,
          detections: hits,
        }]
        const kept: DetectionLine[] = []
        let remainingHits = 40
        for (let index = next.length - 1; index >= 0 && remainingHits > 0; index -= 1) {
          const line = next[index]
          const retained = line.detections.slice(-remainingHits)
          kept.push(retained.length === line.detections.length ? line : { ...line, detections: retained })
          remainingHits -= retained.length
        }
        return kept.reverse()
      })
    }
  }), [subscribe])

  useEffect(() => { void refresh() }, [refresh])

  return {
    active,
    history,
    preview,
    detections,
    memory,
    logs,
    refresh,
    start,
    stop,
    pauseRecognition,
    skipGeoWaypoint,
    deleteGeoWaypoint,
    teleportWhilePaused,
    resumeRecognition,
    cancelTrainingStop,
    pauseTraining: api.pauseTraining,
  }
}
