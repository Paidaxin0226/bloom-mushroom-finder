import React, { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from './services/api'
import { useWebSocket } from './hooks/useWebSocket'
import { useDevice } from './hooks/useDevice'
import { useJobs } from './hooks/useJobs'
import type { Coordinate, ModelInfo, PageKey } from './types'
import Sidebar from './components/Sidebar'
import DeviceStatus from './components/DeviceStatus'
import StatusBar from './components/StatusBar'
import MapView, { type MapContextRequest } from './components/MapView'
import ControlPanel from './components/ControlPanel'
import GeoPanel from './components/GeoPanel'
import RecognitionModeSwitch, { type RecognitionMode } from './components/RecognitionModeSwitch'
import { RecognitionPanel } from './components/WorkflowPanels'
import MonthlyActivityPanel from './components/MonthlyActivityPanel'

const DEFAULT_CENTER = { lat: 25.033, lng: 121.5654 }

export default function App() {
  const websocket = useWebSocket()
  const device = useDevice(websocket.subscribe)
  const jobs = useJobs(websocket.subscribe)
  const [page, setPage] = useState<PageKey>('device')
  const [selected, setSelected] = useState<Coordinate>(DEFAULT_CENTER)
  const [mapContext, setMapContext] = useState<MapContextRequest | null>(null)
  const [deviceDraft, setDeviceDraft] = useState<Coordinate[]>([])
  const [recognitionMode, setRecognitionMode] = useState<RecognitionMode>('automatic')
  const [models, setModels] = useState<ModelInfo[]>([])
  const [settings, setSettings] = useState<Record<string, unknown>>({})
  const [effective, setEffective] = useState<Record<string, unknown>>({})
  const [toast, setToast] = useState<string | null>(null)

  const showError = useCallback((error: unknown) => {
    setToast(error instanceof Error ? error.message : String(error))
    window.setTimeout(() => setToast(null), 4500)
  }, [])

  const refreshModels = useCallback(async () => {
    try { setModels(await api.getModels()) } catch (error) { showError(error) }
  }, [showError])
  const refreshSettings = useCallback(async () => {
    try {
      const result = await api.getSettings()
      setSettings(result.settings)
      setEffective(result.effective_config)
    } catch (error) { showError(error) }
  }, [showError])
  const saveSettings = useCallback(async (next: Record<string, unknown>) => {
    try {
      const result = await api.saveSettings(next)
      setSettings(result.settings)
      setEffective(result.effective_config)
      setToast('設定已保存')
      window.setTimeout(() => setToast(null), 2200)
    } catch (error) { showError(error); throw error }
  }, [showError])
  const setDefaultRecognitionModel = useCallback(async (modelPath: string) => {
    try {
      await api.setDefaultModel(modelPath)
      await Promise.all([refreshModels(), refreshSettings()])
      setToast('預設辨識模型已保存')
      window.setTimeout(() => setToast(null), 2200)
    } catch (error) { showError(error); throw error }
  }, [refreshModels, refreshSettings, showError])
  useEffect(() => {
    void refreshModels()
    void refreshSettings()
  }, [refreshModels, refreshSettings])

  useEffect(() => {
    if (!device.status.current) return
    setSelected((current) => current === DEFAULT_CENTER ? device.status.current! : current)
  }, [device.status.current?.lat, device.status.current?.lng])

  useEffect(() => {
    if (jobs.active === null && ['completed', 'cancelled'].includes(jobs.history[0]?.state ?? '')) {
      void refreshModels()
    }
  }, [jobs.active?.id, jobs.history[0]?.state])

  const jobUi = useMemo(() => ({
    active: jobs.active,
    preview: jobs.preview,
    detections: jobs.detections,
    memory: jobs.memory,
    logs: jobs.logs,
    start: async (kind: string, payload: Record<string, unknown>) => {
      try { return await jobs.start(kind, payload) }
      catch (error) { showError(error); throw error }
    },
    stop: async () => { try { await jobs.stop() } catch (error) { showError(error) } },
    pauseRecognition: async (jobId: string) => {
      try { return await jobs.pauseRecognition(jobId) }
      catch (error) { showError(error); throw error }
    },
    skipGeoWaypoint: async (jobId: string, waypointId: string) => {
      try { await jobs.skipGeoWaypoint(jobId, waypointId) }
      catch (error) { showError(error); throw error }
    },
    deleteGeoWaypoint: async (jobId: string, waypointId: string) => {
      try { return await jobs.deleteGeoWaypoint(jobId, waypointId) }
      catch (error) { showError(error); throw error }
    },
    openPhoneControl: async () => {
      try { await api.openPhoneControl() }
      catch (error) { showError(error); throw error }
    },
    teleportWhilePaused: async (jobId: string, payload: { lat: number; lng: number; label?: string }) => {
      try { return await jobs.teleportWhilePaused(jobId, payload) }
      catch (error) { showError(error); throw error }
    },
    resumeRecognition: async (jobId: string, payload: { target_names?: string[]; participant_count?: number | null }) => {
      try { return await jobs.resumeRecognition(jobId, payload) }
      catch (error) { showError(error); throw error }
    },
    cancelTrainingStop: async () => { try { await jobs.cancelTrainingStop() } catch (error) { showError(error) } },
    pauseTraining: async () => { try { return await jobs.pauseTraining() } catch (error) { showError(error); throw error } },
  }), [jobs.active, jobs.preview, jobs.detections, jobs.memory, jobs.logs, jobs.start, jobs.stop, jobs.pauseRecognition, jobs.skipGeoWaypoint, jobs.deleteGeoWaypoint, jobs.teleportWhilePaused, jobs.resumeRecognition, jobs.cancelTrainingStop, jobs.pauseTraining, showError])

  const deviceCard = <DeviceStatus devices={device.devices} status={device.status} busy={device.busy} error={device.error} onScan={() => void device.scan()} onConnect={device.connect} onDisconnect={device.disconnect}/>
  const mapPage = page === 'device'

  const activeRecognitionMode: RecognitionMode | null = jobs.active?.kind === 'geo_sequence_recognition'
    ? 'sequence'
    : jobs.active?.kind === 'screen_recognition'
      ? 'automatic'
      : null
  const recognitionSwitch = <RecognitionModeSwitch value={recognitionMode} onChange={setRecognitionMode} lockedMode={activeRecognitionMode}/>

  return <div className="app-layout">
    <Sidebar active={page} onChange={setPage} device={deviceCard}/>
    <main className={`main-content ${mapPage ? 'map-page' : ''}`}>
      {page === 'device' && <>
        <MapView
          position={device.status.current}
          selected={selected}
          onSelect={(coordinate) => { setSelected(coordinate); setMapContext(null) }}
          onContextRequest={setMapContext}
          points={deviceDraft}
          route={Array.isArray(device.status.route) ? device.status.route as Coordinate[] : []}
          bookmarks={Array.isArray(device.status.bookmarks) ? device.status.bookmarks as Array<{ id: string; name: string; point: Coordinate }> : []}
          tileTemplate={typeof (device.status.settings as Record<string, unknown> | undefined)?.map === 'object' ? String(((device.status.settings as Record<string, unknown>).map as Record<string, unknown>).template || 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png') : undefined}
          tileAttribution={typeof (device.status.settings as Record<string, unknown> | undefined)?.map === 'object' ? String(((device.status.settings as Record<string, unknown>).map as Record<string, unknown>).attribution || '© OpenStreetMap') : undefined}
        />
        <div className="map-overlay-title"><span>DEVICE CONTROL</span><b>{device.status.connected ? device.status.serial : '等待 Android USB 配對'}</b></div>
        <ControlPanel status={device.status} selected={selected} onSelectedChange={setSelected} command={device.command} contextRequest={mapContext} onContextClose={() => setMapContext(null)} onDraftChange={setDeviceDraft}/>
      </>}
      {page === 'monthly' && <MonthlyActivityPanel status={device.status} jobs={jobUi} effective={effective}/>}
      {page === 'recognition' && recognitionMode === 'automatic' && <RecognitionPanel
        status={device.status}
        models={models}
        onRefreshModels={refreshModels}
        onSetDefaultModel={setDefaultRecognitionModel}
        jobs={jobUi}
        settings={settings}
        onSaveSettings={saveSettings}
        headerActions={recognitionSwitch}
      />}
      <div className="recognition-workflow-layer" hidden={page !== 'recognition' || recognitionMode !== 'sequence'}>
        <GeoPanel
          active={page === 'recognition' && recognitionMode === 'sequence'}
          status={device.status}
          models={models}
          onRefreshModels={refreshModels}
          onSetDefaultModel={setDefaultRecognitionModel}
          jobs={jobUi}
          settings={settings}
          onSaveSettings={saveSettings}
          onOpenDeviceControl={() => setPage('device')}
          modeSwitch={<RecognitionModeSwitch value={recognitionMode} onChange={setRecognitionMode} lockedMode={activeRecognitionMode}/>}
        />
      </div>
      <StatusBar backendConnected={websocket.connected} device={device.status} job={jobs.active}/>
      {toast && <div className="toast">{toast}</div>}
    </main>
  </div>
}
