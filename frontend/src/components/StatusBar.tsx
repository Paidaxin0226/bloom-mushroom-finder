import React, { useEffect, useState } from 'react'
import type { DeviceStatus, JobRecord } from '../types'
import { phoneStateLabel } from '../utils/phoneState'

export default function StatusBar({ backendConnected, device, job }: { backendConnected: boolean; device: DeviceStatus; job: JobRecord | null }) {
  const [now, setNow] = useState(new Date())
  useEffect(() => { const timer = window.setInterval(() => setNow(new Date()), 1000); return () => window.clearInterval(timer) }, [])
  const coord = device.current ? `${device.current.lat.toFixed(6)}, ${device.current.lng.toFixed(6)}` : '—'
  return <footer className="status-bar"><div className="status-item"><span className={`status-connection-dot ${backendConnected ? 'connected' : 'disconnected'}`}/><strong>{backendConnected ? 'Backend connected' : 'Backend disconnected'}</strong></div><i/><div className="status-item"><span>ANDROID</span><b className={device.connected ? 'green' : ''}>{device.connected ? device.serial : device.phone ? phoneStateLabel(device.phone) : '未連接'}</b></div><i/><div className="status-item mono"><span>GPS</span><b>{coord}</b></div><i/><div className="status-item"><span>JOB</span><b className={job ? 'blue' : ''}>{job ? `${job.kind} · ${job.state}` : 'IDLE'}</b></div><span className="status-spacer"/><div className="status-item mono muted">{now.toLocaleDateString('zh-TW')} {now.toLocaleTimeString('zh-TW', { hour12: false })}</div></footer>
}
