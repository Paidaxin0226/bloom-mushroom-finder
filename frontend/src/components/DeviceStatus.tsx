import React, { useState } from 'react'
import type { AdbDevice, DeviceStatus as DeviceStatusType } from '../types'
import { Icon } from './Icons'

interface Props {
  devices: AdbDevice[]
  status: DeviceStatusType
  busy: boolean
  error: string | null
  onScan: () => void
  onConnect: (serial: string) => Promise<unknown>
  onDisconnect: () => Promise<unknown>
}

export default function DeviceStatus({ devices, status, busy, error, onScan, onConnect, onDisconnect }: Props) {
  const [serial, setSerial] = useState('')
  const selected = serial || devices.find((device) => device.state === 'device')?.serial || ''
  return <section className="sidebar-section device-section"><div className="section-heading"><span>ANDROID USB</span><button className="mini-btn" onClick={onScan} disabled={busy} title="重新掃描"><Icon name="refresh" size={14} /></button></div>{status.connected ? <><div className="device-info connected-card"><span className="device-status-dot connected"/><div><strong>{devices.find((item) => item.serial === status.serial)?.model || 'Android device'}</strong><small>{status.serial}</small></div></div><div className="device-metrics"><span><b>{status.current ? `${status.current.lat.toFixed(5)}, ${status.current.lng.toFixed(5)}` : '尚無座標'}</b>LOCATION</span><span><b>{status.running ? (status.paused ? '暫停' : '移動中') : '待命'}</b>STATE</span></div><button className="action-btn full danger-soft" onClick={() => void onDisconnect()} disabled={busy}>中斷連線</button></> : <><select className="select-field" value={selected} onChange={(event) => setSerial(event.target.value)}><option value="">選擇 USB 裝置</option>{devices.map((device) => <option key={device.serial} value={device.serial}>{device.model || device.serial} · {device.state}</option>)}</select><small>沿用 Android 系統的 USB 偵錯授權</small><button className="action-btn full primary" disabled={busy || !selected} onClick={() => void onConnect(selected)}>連接</button></>}{error && <div className="inline-error">{error}</div>}</section>
}
