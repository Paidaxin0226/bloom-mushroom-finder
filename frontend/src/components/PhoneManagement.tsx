import React from 'react'
import type { PhoneState } from '../types'
import { phoneStateHint, phoneStateLabel } from '../utils/phoneState'

export default function PhoneManagement({ phone, busy, onWake }: { phone?: PhoneState; busy: boolean; onWake: () => Promise<void> }) {
  const online = phone?.adb_state === 'device'
  return <section className={`phone-management ${online ? 'online' : 'unavailable'}`} aria-label="手機狀態管理">
    <div className="phone-management-heading"><strong>手機狀態</strong><span>{online ? 'ADB 在線' : 'ADB 未就緒'}</span></div>
    <b>{phoneStateLabel(phone)}</b>
    <p>{phoneStateHint(phone)}</p>
    <div className="phone-management-footer">
      <span>{phone?.battery_percent == null ? '電量未知' : `${phone.battery_percent}%`}{phone?.power_connected ? ' · 已接電源' : ''}</span>
      <button className="action-btn" disabled={!online || busy} onClick={() => void onWake()}>喚醒螢幕</button>
    </div>
    {phone?.checked_at && <small>更新於 {new Date(phone.checked_at).toLocaleTimeString('zh-TW', { hour12: false })}</small>}
  </section>
}
