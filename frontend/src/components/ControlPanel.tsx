import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { Coordinate, DeviceStatus } from '../types'
import type { MapContextRequest } from './MapView'
import { Icon } from './Icons'
import { parseRouteText, validCoordinate } from '../utils/routeImport'
import { ActionButton, Field, Toggle } from './Primitives'

type Tab = 'location' | 'fitness' | 'apps' | 'connection'
type MovementMode = 'teleport' | 'navigate' | 'loop' | 'multistop' | 'random' | 'joystick' | 'flower'
type JsonRecord = Record<string, unknown>

interface Props {
  status: DeviceStatus
  selected: Coordinate | null
  onSelectedChange: (coordinate: Coordinate) => void
  command: (name: string, payload?: Record<string, unknown>) => Promise<unknown>
  contextRequest?: MapContextRequest | null
  onContextClose?: () => void
  onDraftChange?: (points: Coordinate[]) => void
}

interface Bookmark { id: string; name: string; point: Coordinate }
interface SavedRoute { id: string; name: string; points: Coordinate[] }
interface HookApp { packageName: string; label: string; known: boolean; manual: boolean; locationEnabled: boolean; stepEnabled: boolean; lastSeenAt: number }
interface RelayViewer { id: string; name: string; deviceId: string; accessRole: 'controller' | 'readonly'; connectedAt: number; pairingCode?: string }

const asRecord = (value: unknown): JsonRecord => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
const asArray = <T,>(value: unknown): T[] => Array.isArray(value) ? value as T[] : []
const numberValue = (value: unknown, fallback: number) => typeof value === 'number' && Number.isFinite(value) ? value : fallback
const boolValue = (value: unknown, fallback = false) => typeof value === 'boolean' ? value : fallback
const stringValue = (value: unknown, fallback = '') => typeof value === 'string' ? value : fallback

function distanceMeters(points: Coordinate[]) {
  let total = 0
  for (let index = 1; index < points.length; index += 1) {
    const a = points[index - 1], b = points[index]
    const lat1 = a.lat * Math.PI / 180, lat2 = b.lat * Math.PI / 180
    const dLat = lat2 - lat1, dLng = (b.lng - a.lng) * Math.PI / 180
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2
    total += 6371000 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h))
  }
  return total
}

function downloadText(name: string, text: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function routeGpx(points: Coordinate[]) {
  return `<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Bloom Mushroom Finder"><rte><name>LocWarp route</name>${points.map((point) => `<rtept lat="${point.lat}" lon="${point.lng}"/>`).join('')}</rte></gpx>`
}

function routeKml(points: Coordinate[]) {
  return `<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>LocWarp route</name><LineString><coordinates>${points.map((point) => `${point.lng},${point.lat},0`).join(' ')}</coordinates></LineString></Placemark></Document></kml>`
}

function routeGeoJson(points: Coordinate[]) {
  return JSON.stringify({ type: 'FeatureCollection', features: [{ type: 'Feature', properties: { name: 'LocWarp route' }, geometry: { type: 'LineString', coordinates: points.map((point) => [point.lng, point.lat]) } }] }, null, 2)
}

function SegmentedTabs({ tab, onChange }: { tab: Tab; onChange: (tab: Tab) => void }) {
  return <nav className="mobile-control-tabs" aria-label="手機功能頁">{([
    ['location', '定位', 'map'], ['fitness', '步數', 'steps'], ['apps', 'Apps', 'device'], ['connection', '連線', 'live'],
  ] as const).map(([key, label, icon]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => onChange(key)}><Icon name={icon}/><span>{label}</span></button>)}</nav>
}

export default function ControlPanel({ status, selected, onSelectedChange, command, contextRequest, onContextClose, onDraftChange }: Props) {
  const [tab, setTab] = useState<Tab>('location')
  const [mode, setMode] = useState<MovementMode>('teleport')
  const [lat, setLat] = useState('25.033000')
  const [lng, setLng] = useState('121.565400')
  const [search, setSearch] = useState('')
  const [searchResults, setSearchResults] = useState<Array<{ label: string; lat: number; lng: number }>>([])
  const [speed, setSpeed] = useState(10.8)
  const [minSpeed, setMinSpeed] = useState('')
  const [maxSpeed, setMaxSpeed] = useState('')
  const [straightLine, setStraightLine] = useState(false)
  const [draft, setDraft] = useState<Coordinate[]>([])
  const [routeName, setRouteName] = useState('我的路線')
  const [bookmarkName, setBookmarkName] = useState('新收藏')
  const [laps, setLaps] = useState(0)
  const [stopSeconds, setStopSeconds] = useState(5)
  const [routeLoop, setRouteLoop] = useState(false)
  const [randomRadius, setRandomRadius] = useState(150)
  const [pauseEnabled, setPauseEnabled] = useState(false)
  const [pauseMin, setPauseMin] = useState(5)
  const [pauseMax, setPauseMax] = useState(20)
  const [autoStopMinutes, setAutoStopMinutes] = useState(0)
  const [restoreOnFinish, setRestoreOnFinish] = useState(false)
  const [resumeAfterRestart, setResumeAfterRestart] = useState(true)
  const [mapTileMode, setMapTileMode] = useState('online_cache')
  const [mapTemplate, setMapTemplate] = useState('https://tile.openstreetmap.org/{z}/{x}/{y}.png')
  const [mapAttribution, setMapAttribution] = useState('© OpenStreetMap contributors')
  const [interceptMapLinks, setInterceptMapLinks] = useState(false)
  const [joystickSize, setJoystickSize] = useState(210)
  const [mapOverlaySize, setMapOverlaySize] = useState(280)
  const [flowerRadius, setFlowerRadius] = useState(30)
  const [flowerSpacing, setFlowerSpacing] = useState(7)
  const [steps, setSteps] = useState(1000)
  const [multiplier, setMultiplier] = useState(1)
  const [hookApps, setHookApps] = useState<HookApp[]>([])
  const [manualPackage, setManualPackage] = useState('')
  const [diagnostics, setDiagnostics] = useState('')
  const [history, setHistory] = useState<Array<{ id: string; timestampMs: number; type: string; message: string; success: boolean }>>([])
  const [relayUrl, setRelayUrl] = useState('')
  const [relayQr, setRelayQr] = useState('')
  const [message, setMessage] = useState('在地圖按一下選點；按右鍵開啟手機版長按選單')
  const routeInput = useRef<HTMLInputElement | null>(null)
  const flowerInput = useRef<HTMLInputElement | null>(null)
  const backupInput = useRef<HTMLInputElement | null>(null)
  const hydrated = useRef('')
  const joyLastSent = useRef(0)

  const settings = asRecord(status.settings)
  const movementSettings = asRecord(settings.movement)
  const overlaySettings = asRecord(settings.overlays)
  const mapSettings = asRecord(settings.map)
  const stepSettings = asRecord(settings.steps)
  const stepStatus = asRecord(status.steps)
  const hookScope = asRecord(status.hookScope)
  const flowerStatus = asRecord(status.flower)
  const relay = asRecord(status.relay)
  const bookmarks = asArray<Bookmark>(status.bookmarks)
  const routes = asArray<SavedRoute>(status.routes)
  const pendingViewers = asArray<RelayViewer>(relay.pendingViewers)
  const relayViewers = asArray<RelayViewer>(relay.viewers)

  useEffect(() => {
    if (!selected) return
    setLat(selected.lat.toFixed(7)); setLng(selected.lng.toFixed(7))
  }, [selected?.lat, selected?.lng])

  useEffect(() => { onDraftChange?.(draft) }, [draft, onDraftChange])

  useEffect(() => {
    if (!status.connected) { hydrated.current = ''; return }
    const key = JSON.stringify([status.serial, settings])
    if (!Object.keys(settings).length || hydrated.current === key) return
    hydrated.current = key
    setSpeed(numberValue(movementSettings.speedKmh, numberValue(status.speedKmh, 10.8)))
    setStraightLine(boolValue(movementSettings.straightLine, boolValue(status.straightLine)))
    setAutoStopMinutes(numberValue(movementSettings.autoStopMinutes, 0))
    setRestoreOnFinish(boolValue(movementSettings.restoreOnFinish))
    setResumeAfterRestart(boolValue(movementSettings.resumeAfterRestart, true))
    setJoystickSize(numberValue(overlaySettings.joystickSizeDp, 210))
    setMapOverlaySize(numberValue(overlaySettings.mapSizeDp, 280))
    setMapTileMode(stringValue(mapSettings.mode, 'online_cache'))
    setMapTemplate(stringValue(mapSettings.template, 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'))
    setMapAttribution(stringValue(mapSettings.attribution, '© OpenStreetMap contributors'))
    setInterceptMapLinks(boolValue(mapSettings.interceptMapLinks))
    setSteps(numberValue(stepSettings.defaultSteps, 1000))
    setMultiplier(numberValue(stepSettings.walkingMultiplier, 1))
    setFlowerRadius(numberValue(flowerStatus.radiusMeters, 30))
    setFlowerSpacing(numberValue(flowerStatus.spacingMeters, 7))
    setRelayUrl(stringValue(relay.baseUrl))
    setHookApps(asArray<HookApp>(status.hookApps))
  }, [status.connected, status.serial, settings, movementSettings, overlaySettings, mapSettings, stepSettings, flowerStatus, relay, status.hookApps, status.speedKmh, status.straightLine])

  const coordinate = () => validCoordinate(lat, lng)
  const usePath = !straightLine
  const distance = useMemo(() => distanceMeters(draft), [draft])
  const estimateMinutes = speed > 0 ? distance / 1000 / speed * 60 : 0

  const run = async (name: string, payload: JsonRecord = {}, success?: string) => {
    try {
      const result = await command(name, payload) as JsonRecord
      setMessage(success || stringValue(result.message, '指令已完成'))
      return result
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error)); throw error
    }
  }

  const runMovement = async () => {
    const point = coordinate()
    if (!point && !['loop', 'multistop', 'flower'].includes(mode)) { setMessage('座標格式不正確'); return }
    if (point) onSelectedChange(point)
    const common: JsonRecord = { speedKmh: speed, usePath, pauseEnabled, pauseMin, pauseMax }
    if (minSpeed) common.minKmh = Number(minSpeed)
    if (maxSpeed) common.maxKmh = Number(maxSpeed)
    if (mode === 'teleport' || mode === 'navigate') await run(mode, { ...common, ...point })
    else if (mode === 'loop') await run('loop', { ...common, points: draft, laps })
    else if (mode === 'multistop') await run('multiStop', { ...common, points: draft, stopSeconds, loop: routeLoop })
    else if (mode === 'random' && point) await run('randomWalk', { ...common, ...point, radiusM: randomRadius })
    else if (mode === 'joystick') await run('startJoystick', { ...common, ...point })
    else if (mode === 'flower') await run('startFlower', { ...common, radiusMeters: flowerRadius, spacingMeters: flowerSpacing })
  }

  const saveMovementSettings = () => run('setMovementSettings', { speedKmh: speed, minKmh: minSpeed ? Number(minSpeed) : undefined, maxKmh: maxSpeed ? Number(maxSpeed) : undefined, straightLine, autoStopMinutes, restoreOnFinish, resumeAfterRestart })

  const addSelected = () => {
    const point = coordinate(); if (!point) { setMessage('請先選取有效座標'); return }
    setDraft((items) => [...items, point].slice(0, 500))
  }

  const moveDraft = (index: number, delta: number) => setDraft((items) => {
    const target = index + delta; if (target < 0 || target >= items.length) return items
    const next = [...items]; [next[index], next[target]] = [next[target], next[index]]; return next
  })

  const searchAddress = async () => {
    if (!search.trim()) return
    const result = await run('searchAddress', { query: search.trim() })
    setSearchResults(asArray(result?.results))
  }

  const importRoute = async (file?: File) => {
    if (!file) return
    try {
      const points = parseRouteText(await file.text(), file.name)
      if (!points.length) throw new Error('檔案內找不到有效座標')
      setDraft(points); setMessage(`已匯入 ${points.length} 個路線節點${points.length === 500 ? '（已達手機單一路線上限）' : ''}`)
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)) }
    if (routeInput.current) routeInput.current.value = ''
  }

  const importFlowerTargets = async (file?: File) => {
    if (!file) return
    try {
      const points = parseRouteText(await file.text(), file.name)
      if (!points.length) throw new Error('檔案內找不到有效座標')
      await run('addFlowerTargets', { points })
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)) }
    if (flowerInput.current) flowerInput.current.value = ''
  }

  const loadHookApps = async () => {
    const result = await run('listHookApps')
    setHookApps(asArray<HookApp>(result?.apps))
  }

  const runDiagnostics = async () => {
    const result = await run('diagnostics'); setDiagnostics(stringValue(result?.text))
  }

  const loadHistory = async () => {
    const result = await run('history'); setHistory(asArray(result?.entries))
  }

  const exportBackup = async () => {
    const result = await run('exportBackup')
    const backup = stringValue(result?.backup)
    if (backup) downloadText(`LocWarp-${new Date().toISOString().slice(0, 10)}.json`, backup)
  }

  const importBackup = async (file?: File) => {
    if (!file) return
    const text = await file.text()
    if (text.length > 4 * 1024 * 1024) { setMessage('備份檔超過 4 MB'); return }
    if (!window.confirm('還原會覆蓋手機目前的收藏、路線、Hook、顯示與安全設定，並停止定位與 Relay。確定繼續？')) return
    await run('importBackup', { backup: text })
    hydrated.current = ''
    if (backupInput.current) backupInput.current.value = ''
  }

  const mapContextAction = async (action: 'teleport' | 'path' | 'waypoint' | 'bookmark') => {
    const point = contextRequest?.coordinate
    if (!point) return
    onContextClose?.()
    if (action === 'teleport') await run('teleport', { ...point, speedKmh: speed })
    else if (action === 'path') await run('navigate', { ...point, speedKmh: speed, usePath: true })
    else if (action === 'waypoint') setDraft((items) => [...items, point].slice(0, 500))
    else await run('addBookmark', { ...point, name: `${point.lat.toFixed(5)}, ${point.lng.toFixed(5)}` })
  }

  const sendJoystick = (event: React.PointerEvent<HTMLDivElement>, end = false) => {
    const now = performance.now(); if (!end && now - joyLastSent.current < 90) return
    joyLastSent.current = now
    if (end) { void run('joystickInput', { x: 0, y: 0 }, '搖桿已回中'); return }
    const rect = event.currentTarget.getBoundingClientRect()
    const x = Math.max(-1, Math.min(1, (event.clientX - (rect.left + rect.width / 2)) / (rect.width / 2)))
    const y = Math.max(-1, Math.min(1, ((rect.top + rect.height / 2) - event.clientY) / (rect.height / 2)))
    event.currentTarget.setPointerCapture(event.pointerId)
    void command('joystickInput', { x, y }).catch((error) => setMessage(error instanceof Error ? error.message : String(error)))
  }

  const locationPanel = <>
    <section className="control-section compact-section"><div className="section-title-row"><div className="section-title">位置模擬</div><Toggle checked={boolValue(status.mockEnabled)} onChange={(enabled) => void run('setEnabled', { enabled })} label={boolValue(status.mockEnabled) ? '已開啟' : '已關閉'}/></div>
      <div className="search-row"><input className="input" value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void searchAddress() }} placeholder="搜尋地名或地址"/><ActionButton icon="scan" onClick={() => void searchAddress()}>搜尋</ActionButton></div>
      {searchResults.length > 0 && <div className="search-results">{searchResults.map((item) => <button key={`${item.lat}-${item.lng}`} onClick={() => { const point = { lat: item.lat, lng: item.lng }; onSelectedChange(point); setSearchResults([]) }}><b>{item.label}</b><small>{item.lat.toFixed(6)}, {item.lng.toFixed(6)}</small></button>)}</div>}
      <div className="coord-grid"><Field label="LATITUDE"><input className="input mono" value={lat} onChange={(event) => setLat(event.target.value)}/></Field><Field label="LONGITUDE"><input className="input mono" value={lng} onChange={(event) => setLng(event.target.value)}/></Field></div>
      <div className="button-row"><ActionButton icon="pin" onClick={addSelected}>加入節點</ActionButton><ActionButton onClick={() => { if (status.current) onSelectedChange(status.current) }}>目前位置</ActionButton><ActionButton onClick={async () => { const text = await navigator.clipboard.readText(); const match = text.match(/(-?\d+(?:\.\d+)?)\D+(-?\d+(?:\.\d+)?)/); const point = match && validCoordinate(match[1], match[2]); if (point) onSelectedChange(point) }}>貼上座標</ActionButton></div>
    </section>
    <section className="control-section"><div className="section-title">移動模式</div><div className="mode-grid mobile-mode-grid">{([
      ['teleport', '瞬移', 'pin'], ['navigate', '導航', 'map'], ['loop', '循環', 'refresh'], ['multistop', '多站', 'route'], ['random', '隨機漫步', 'scan'], ['joystick', '搖桿', 'device'], ['flower', '種花', 'live'],
    ] as const).map(([key, label, icon]) => <button key={key} className={`mode-btn ${mode === key ? 'active' : ''}`} onClick={() => setMode(key)}><Icon name={icon}/><span>{label}</span></button>)}</div>
      {['loop', 'multistop'].includes(mode) && <div className="route-editor"><div className="route-summary"><b>{draft.length} 個節點</b><span>{distance >= 1000 ? `${(distance / 1000).toFixed(2)} km` : `${Math.round(distance)} m`} · 約 {Math.ceil(estimateMinutes)} 分</span></div>{draft.map((point, index) => <div className="route-point-row" key={`${index}-${point.lat}-${point.lng}`}><button onClick={() => onSelectedChange(point)}><b>{index + 1}</b><span className="mono">{point.lat.toFixed(6)}, {point.lng.toFixed(6)}</span></button><button onClick={() => moveDraft(index, -1)}>↑</button><button onClick={() => moveDraft(index, 1)}>↓</button><button onClick={() => setDraft((items) => items.filter((_, itemIndex) => itemIndex !== index))}>×</button></div>)}<div className="button-row wrap"><ActionButton onClick={() => routeInput.current?.click()}>匯入</ActionButton><ActionButton disabled={!draft.length} onClick={() => downloadText('LocWarp-route.json', JSON.stringify({ name: routeName, points: draft }, null, 2))}>JSON</ActionButton><ActionButton disabled={!draft.length} onClick={() => downloadText('LocWarp-route.gpx', routeGpx(draft), 'application/gpx+xml')}>GPX</ActionButton><ActionButton disabled={!draft.length} onClick={() => downloadText('LocWarp-route.kml', routeKml(draft), 'application/vnd.google-earth.kml+xml')}>KML</ActionButton><ActionButton disabled={!draft.length} onClick={() => downloadText('LocWarp-route.geojson', routeGeoJson(draft), 'application/geo+json')}>GeoJSON</ActionButton><ActionButton disabled={!draft.length} onClick={() => setDraft((items) => [...items].reverse())}>反轉</ActionButton><ActionButton disabled={!draft.length} kind="danger" onClick={() => setDraft([])}>清除</ActionButton></div><input ref={routeInput} hidden type="file" accept=".json,.geojson,.gpx,.kml,.xml,.txt" onChange={(event) => void importRoute(event.target.files?.[0])}/>
        <div className="save-row"><input className="input" value={routeName} onChange={(event) => setRouteName(event.target.value)}/><ActionButton disabled={draft.length < 2} onClick={() => void run('addRoute', { name: routeName, points: draft })}>保存路線</ActionButton></div></div>}
      {mode === 'loop' && <div className="two-column"><Field label="圈數 · 0 為不限"><input className="input" type="number" min="0" value={laps} onChange={(event) => setLaps(Number(event.target.value))}/></Field></div>}
      {mode === 'multistop' && <div className="two-column"><Field label="停留秒數"><input className="input" type="number" min="0" value={stopSeconds} onChange={(event) => setStopSeconds(Number(event.target.value))}/></Field><Toggle checked={routeLoop} onChange={setRouteLoop} label="完成後循環"/></div>}
      {mode === 'random' && <Field label="活動半徑 · m"><input className="input" type="number" min="10" value={randomRadius} onChange={(event) => setRandomRadius(Number(event.target.value))}/></Field>}
      {mode === 'flower' && <><div className="two-column"><Field label="種花半徑 · 20–38 m"><input className="input" type="number" min="20" max="38" value={flowerRadius} onChange={(event) => setFlowerRadius(Number(event.target.value))}/></Field><Field label="掃描間距 · 4–10 m"><input className="input" type="number" min="4" max="10" value={flowerSpacing} onChange={(event) => setFlowerSpacing(Number(event.target.value))}/></Field></div><div className="button-row wrap"><ActionButton disabled={!coordinate()} onClick={() => { const point = coordinate(); if (point) void run('addFlowerTargets', { points: [point] }) }}>加入選取節點</ActionButton><ActionButton onClick={() => flowerInput.current?.click()}>匯入節點</ActionButton><ActionButton kind="danger" onClick={() => void run('clearFlowerTargets')}>清空</ActionButton></div><input ref={flowerInput} hidden type="file" accept=".json,.geojson,.gpx,.kml,.xml,.txt" onChange={(event) => void importFlowerTargets(event.target.files?.[0])}/><div className="library-list compact">{asArray<{ id: string; lat: number; lng: number; passesCompleted: number }>(flowerStatus.targets).map((target) => <div key={target.id}><button onClick={() => onSelectedChange(target)}><b>{target.lat.toFixed(5)}, {target.lng.toFixed(5)}</b><small>{target.passesCompleted}/2 輪</small></button><button onClick={() => void run('deleteFlowerTarget', { id: target.id })}>刪除</button></div>)}</div></>}
      {mode === 'joystick' && <div className="desktop-joystick" role="application" aria-label="定位搖桿" onPointerDown={sendJoystick} onPointerMove={(event) => { if (event.buttons) sendJoystick(event) }} onPointerUp={(event) => sendJoystick(event, true)} onPointerCancel={(event) => sendJoystick(event, true)}><span>拖曳控制方向</span></div>}
      <ActionButton kind="primary" icon="play" disabled={!status.connected || ((mode === 'loop' || mode === 'multistop') && draft.length < 2)} onClick={() => void runMovement()}>{mode === 'teleport' ? '瞬移到選取位置' : mode === 'flower' && boolValue(flowerStatus.active) ? '種花模式執行中' : '開始執行'}</ActionButton>
      <div className="button-row"><ActionButton icon={status.paused ? 'play' : 'pause'} disabled={!status.running} onClick={() => void run(status.paused ? 'resume' : 'pause')}>{status.paused ? '繼續' : '暫停'}</ActionButton><ActionButton icon="stop" kind="danger" onClick={() => void run(mode === 'flower' ? 'stopFlower' : 'stop')}>停止</ActionButton><ActionButton icon="restore" onClick={() => void run('restore')}>還原真實位置</ActionButton></div>
    </section>
    <details className="control-disclosure"><summary>速度、停留與安全設定</summary><div className="control-section"><Field label={`固定速度 · ${speed.toFixed(1)} km/h`}><input className="range" type="range" min="0.5" max="200" step="0.5" value={speed} onChange={(event) => setSpeed(Number(event.target.value))}/></Field><div className="two-column"><Field label="最低隨機速度"><input className="input" type="number" value={minSpeed} onChange={(event) => setMinSpeed(event.target.value)} placeholder="不啟用"/></Field><Field label="最高隨機速度"><input className="input" type="number" value={maxSpeed} onChange={(event) => setMaxSpeed(event.target.value)} placeholder="不啟用"/></Field></div><Toggle checked={straightLine} onChange={setStraightLine} label="直線移動（關閉道路規劃）"/><Toggle checked={pauseEnabled} onChange={setPauseEnabled} label="路線節點隨機停留"/>{pauseEnabled && <div className="two-column"><Field label="最短停留秒數"><input className="input" type="number" value={pauseMin} onChange={(event) => setPauseMin(Number(event.target.value))}/></Field><Field label="最長停留秒數"><input className="input" type="number" value={pauseMax} onChange={(event) => setPauseMax(Number(event.target.value))}/></Field></div>}<Field label="自動停止分鐘 · 0 為關閉"><input className="input" type="number" min="0" max="1440" value={autoStopMinutes} onChange={(event) => setAutoStopMinutes(Number(event.target.value))}/></Field><Toggle checked={restoreOnFinish} onChange={setRestoreOnFinish} label="移動完成後還原真實位置"/><Toggle checked={resumeAfterRestart} onChange={setResumeAfterRestart} label="App 重啟後恢復移動"/><ActionButton onClick={() => void saveMovementSettings()}>套用設定</ActionButton></div></details>
    <details className="control-disclosure"><summary>收藏與已存路線</summary><div className="control-section"><div className="save-row"><input className="input" value={bookmarkName} onChange={(event) => setBookmarkName(event.target.value)}/><ActionButton disabled={!coordinate()} onClick={() => { const point = coordinate(); if (point) void run('addBookmark', { name: bookmarkName, ...point }) }}>收藏選取位置</ActionButton></div><div className="library-list">{bookmarks.map((bookmark) => <div key={bookmark.id}><button onClick={() => onSelectedChange(bookmark.point)}><b>{bookmark.name}</b><small>{bookmark.point.lat.toFixed(5)}, {bookmark.point.lng.toFixed(5)}</small></button><button onClick={() => void run('deleteBookmark', { id: bookmark.id })}>刪除</button></div>)}{routes.map((route) => <div key={route.id}><button onClick={() => { setDraft(route.points); if (route.points[0]) onSelectedChange(route.points[0]) }}><b>{route.name}</b><small>{route.points.length} 個節點</small></button><button onClick={() => void run('deleteRoute', { id: route.id })}>刪除</button></div>)}</div></div></details>
    <details className="control-disclosure"><summary>浮動控制與地圖來源</summary><div className="control-section"><div className="two-column"><Field label="手機浮動搖桿尺寸"><input className="input" type="number" min="150" max="310" step="20" value={joystickSize} onChange={(event) => setJoystickSize(Number(event.target.value))}/></Field><Field label="手機浮動地圖尺寸"><input className="input" type="number" min="220" max="380" step="20" value={mapOverlaySize} onChange={(event) => setMapOverlaySize(Number(event.target.value))}/></Field></div><div className="button-row"><ActionButton onClick={() => void run('setOverlay', { kind: 'joystick', visible: !boolValue(overlaySettings.joystickVisible), sizeDp: joystickSize })}>{boolValue(overlaySettings.joystickVisible) ? '關閉手機搖桿' : '開啟手機搖桿'}</ActionButton><ActionButton onClick={() => void run('setOverlay', { kind: 'map', visible: !boolValue(overlaySettings.mapVisible), sizeDp: mapOverlaySize })}>{boolValue(overlaySettings.mapVisible) ? '關閉浮動地圖' : '開啟浮動地圖'}</ActionButton></div><Field label="圖磚模式"><select className="select-field" value={mapTileMode} onChange={(event) => setMapTileMode(event.target.value)}><option value="online_cache">線上並快取</option><option value="offline_only">僅離線快取</option></select></Field><Field label="HTTPS 圖磚 URL"><input className="input mono" value={mapTemplate} onChange={(event) => setMapTemplate(event.target.value)}/></Field><Field label="圖資署名"><input className="input" value={mapAttribution} onChange={(event) => setMapAttribution(event.target.value)}/></Field><Toggle checked={interceptMapLinks} onChange={setInterceptMapLinks} label="攔截手機地圖連結"/><div className="button-row"><ActionButton onClick={() => void run('setMapSettings', { mode: mapTileMode, template: mapTemplate, attribution: mapAttribution, interceptMapLinks })}>套用地圖設定</ActionButton><ActionButton kind="danger" onClick={() => void run('clearMapCache')}>清除手機快取</ActionButton></div><small className="field-hint">手機快取：{numberValue(mapSettings.cachedTiles, 0)} 張 · {(numberValue(mapSettings.cachedBytes, 0) / 1048576).toFixed(1)} MB</small></div></details>
  </>

  const fitnessPanel = <><section className="control-section"><div className="control-status-card"><span className={`state-dot ${boolValue(stepStatus.running) ? 'running' : ''}`}/><div><b>{stringValue(stepStatus.mode, 'none')} · {stringValue(stepStatus.state, 'idle')}</b><small>已送出 {numberValue(stepStatus.injected, 0)} / {numberValue(stepStatus.target, 0) || '∞'} 步</small></div></div><Field label="指定步數 · 1–200,000"><input className="input mono" type="number" min="1" max="200000" value={steps} onChange={(event) => setSteps(Number(event.target.value))}/></Field><div className="button-row"><ActionButton kind="primary" onClick={() => void run('startSteps', { steps })}>指定步數</ActionButton><ActionButton onClick={() => void run('startStepLoop')}>循環新增</ActionButton></div><div className="button-row"><ActionButton disabled={!boolValue(stepStatus.running)} onClick={() => void run('toggleStepsPause')}>{boolValue(stepStatus.paused) ? '繼續' : '暫停'}</ActionButton><ActionButton kind="danger" onClick={() => void run('stopSteps')}>終止</ActionButton></div></section><section className="control-section"><div className="section-title">跟隨走路</div><Toggle checked={boolValue(stepStatus.followEnabled, boolValue(stepSettings.followEnabled))} onChange={(enabled) => void run('setStepFollow', { enabled })} label="依定位移動距離同步增加步數"/><small className="field-hint">{boolValue(stepStatus.followEligible) ? `跟隨中 · ${numberValue(stepStatus.speedKmh, 0).toFixed(1)} km/h` : '等待速度不超過 15 km/h 的定位移動'}</small><Field label="真實步數放大倍數 · 1–100"><input className="input" type="number" min="1" max="100" value={multiplier} onChange={(event) => setMultiplier(Number(event.target.value))}/></Field><ActionButton onClick={() => void run('setStepSettings', { defaultSteps: steps, walkingMultiplier: multiplier })}>保存步數設定</ActionButton></section></>

  const appsPanel = <><section className="control-section"><div className="section-title-row"><div className="section-title">LSPosed 作用域與 Hook</div><ActionButton icon="refresh" onClick={() => void loadHookApps()}>重新整理</ActionButton></div><small className="field-hint">{boolValue(hookScope.official) ? `官方作用域 · ${stringValue(hookScope.framework, 'LSPosed')}` : `備援作用域${stringValue(hookScope.error) ? ` · ${stringValue(hookScope.error)}` : ''}`}</small>{!boolValue(hookScope.official) && <div className="save-row"><input className="input mono" value={manualPackage} onChange={(event) => setManualPackage(event.target.value)} placeholder="com.example.app"/><ActionButton onClick={async () => { await run('addHookApp', { packageName: manualPackage }); setManualPackage(''); await loadHookApps() }}>補入 App</ActionButton></div>}<div className="hook-app-list">{hookApps.map((app) => <div className="hook-app-card" key={app.packageName}><div><b>{app.label}</b><small className="mono">{app.packageName}</small>{app.lastSeenAt > 0 && <small>最後握手：{new Date(app.lastSeenAt).toLocaleString()}</small>}</div><Toggle checked={app.locationEnabled} onChange={async (enabled) => { await run('setHook', { packageName: app.packageName, locationEnabled: enabled }); setHookApps((items) => items.map((item) => item.packageName === app.packageName ? { ...item, locationEnabled: enabled } : item)) }} label="Location Hook"/><Toggle checked={app.stepEnabled} onChange={async (enabled) => { await run('setHook', { packageName: app.packageName, stepEnabled: enabled }); setHookApps((items) => items.map((item) => item.packageName === app.packageName ? { ...item, stepEnabled: enabled } : item)) }} label="Step Hook"/>{app.manual && <button className="text-btn danger-text" onClick={async () => { await run('removeHookApp', { packageName: app.packageName }); await loadHookApps() }}>從備援清單移除</button>}</div>)}</div></section><details className="control-disclosure" open><summary>診斷與操作紀錄</summary><div className="control-section"><div className="button-row wrap"><ActionButton onClick={() => void runDiagnostics()}>執行診斷</ActionButton><ActionButton onClick={() => void navigator.clipboard.writeText(diagnostics)}>複製報告</ActionButton><ActionButton onClick={async () => { const result = await run('historyCsv'); downloadText('LocWarp-history.csv', stringValue(result?.text), 'text/csv') }}>匯出 CSV</ActionButton><ActionButton kind="danger" onClick={async () => { await run('clearHistory'); setHistory([]) }}>清除歷史</ActionButton></div>{diagnostics && <pre className="diagnostics-output">{diagnostics}</pre>}<ActionButton onClick={() => void loadHistory()}>載入操作歷史</ActionButton><div className="history-list">{history.map((entry) => <div key={entry.id} className={entry.success ? '' : 'error'}><time>{new Date(entry.timestampMs).toLocaleString()}</time><b>{entry.type}</b><span>{entry.message}</span></div>)}</div></div></details></>

  const connectionPanel = <><section className="control-section"><div className="section-title">電腦 USB 控制</div><div className="control-status-card"><span className={`state-dot ${boolValue(relay.usbRunning, status.connected) ? 'running' : ''}`}/><div><b>{boolValue(relay.usbRunning, status.connected) ? 'USB 控制已啟動' : 'USB 控制未啟動'}</b><small className="mono">{status.serial || '尚未連接'}</small>{stringValue(relay.usbError) && <small className="danger-text">{stringValue(relay.usbError)}</small>}</div></div><ActionButton kind="danger" onClick={() => { if (window.confirm('停止後目前 USB 連線會中斷；可從左側裝置區重新連接並啟動。確定停止？')) void run('stopUsbControl') }}>停止手機 USB 控制</ActionButton></section><section className="control-section"><div className="section-title">Relay 遠端控制</div><Field label="中繼伺服器 URL"><input className="input" value={relayUrl} onChange={(event) => setRelayUrl(event.target.value)} placeholder="https://relay.example.com"/></Field>{!boolValue(relay.running) ? <ActionButton kind="primary" onClick={() => void run('startRelay', { baseUrl: relayUrl })}>啟動 Relay</ActionButton> : <><div className="relay-url"><span>{boolValue(relay.connected) ? '已連線' : '連線中'}</span><code>{stringValue(relay.publicUrl, '等待控制網址')}</code></div>{relayQr && <img className="relay-qr" src={relayQr} alt="Relay 遠端控制 QR Code"/>}<div className="button-row wrap"><ActionButton onClick={() => void navigator.clipboard.writeText(stringValue(relay.publicUrl))}>複製網址</ActionButton><ActionButton onClick={async () => { const result = await run('relayQr'); setRelayQr(stringValue(result?.dataUrl)) }}>顯示 QR</ActionButton><ActionButton onClick={async () => { setRelayQr(''); await run('rotateRelay') }}>更換 QR／網址</ActionButton><ActionButton kind="danger" onClick={async () => { setRelayQr(''); await run('stopRelay') }}>停止</ActionButton></div></>}<details className="inner-disclosure"><summary>伺服器裝置授權</summary><code className="key-block">SHA-256 {stringValue(relay.admissionFingerprint, 'unavailable')}\n{stringValue(relay.admissionPublicKeyEntry)}</code><ActionButton onClick={() => void navigator.clipboard.writeText(stringValue(relay.admissionPublicKeyEntry))}>複製白名單公鑰</ActionButton></details>{pendingViewers.map((viewer) => <div className="relay-viewer pending" key={viewer.id}><div><b>{viewer.name || viewer.id}</b><small>配對碼 {viewer.pairingCode || '—'} · 要求 {viewer.accessRole === 'controller' ? '控制' : '唯讀'}</small></div><div className="button-row"><ActionButton onClick={() => void run('decideRelayViewer', { id: viewer.id, approved: true, role: 'controller' })}>允許控制</ActionButton><ActionButton onClick={() => void run('decideRelayViewer', { id: viewer.id, approved: true, role: 'readonly' })}>僅查看</ActionButton><ActionButton kind="danger" onClick={() => void run('decideRelayViewer', { id: viewer.id, approved: false })}>拒絕</ActionButton></div></div>)}{relayViewers.map((viewer) => <div className="relay-viewer" key={viewer.id}><div><b>{viewer.name || viewer.id}</b><small className="mono">{viewer.deviceId}</small></div><select className="select-field" value={viewer.accessRole} onChange={(event) => void run('relayViewerRole', { id: viewer.id, role: event.target.value })}><option value="controller">可控制</option><option value="readonly">僅查看</option></select><ActionButton kind="danger" onClick={() => void run('disconnectRelayViewer', { id: viewer.id })}>中斷</ActionButton></div>)}</section><section className="control-section"><div className="section-title">可攜式備份</div><div className="button-row"><ActionButton icon="folder" onClick={() => void exportBackup()}>匯出手機設定</ActionButton><ActionButton icon="restore" onClick={() => backupInput.current?.click()}>還原備份</ActionButton></div><input ref={backupInput} hidden type="file" accept="application/json,.json" onChange={(event) => void importBackup(event.target.files?.[0])}/></section></>

  return <aside className="floating-control mobile-control"><div className="control-title"><div><span className="eyebrow">LOCWARP ANDROID</span><h2>手機完整控制</h2></div><span className={`engine-state ${status.running ? 'running' : ''}`}><i/>{status.running ? (status.paused ? 'PAUSED' : 'RUNNING') : status.connected ? 'READY' : 'OFFLINE'}</span></div><SegmentedTabs tab={tab} onChange={setTab}/><div className="mobile-control-content">{tab === 'location' ? locationPanel : tab === 'fitness' ? fitnessPanel : tab === 'apps' ? appsPanel : connectionPanel}</div><div className="control-message">{message}</div>{contextRequest && <div className="map-context-menu" style={{ left: Math.min(contextRequest.clientX, window.innerWidth - 190), top: Math.min(contextRequest.clientY, window.innerHeight - 210) }} onMouseLeave={onContextClose}><b>{contextRequest.coordinate.lat.toFixed(6)}, {contextRequest.coordinate.lng.toFixed(6)}</b><button onClick={() => void mapContextAction('teleport')}>瞬移到這裡</button><button onClick={() => void mapContextAction('path')}>沿道路導航</button><button onClick={() => void mapContextAction('waypoint')}>加入路線節點</button><button onClick={() => void mapContextAction('bookmark')}>新增收藏</button></div>}</aside>
}
