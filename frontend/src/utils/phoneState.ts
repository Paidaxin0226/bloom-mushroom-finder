import type { DeviceStatus, PhoneState } from '../types'

export function newerPhoneState(previous: PhoneState | undefined, incoming: PhoneState): PhoneState {
  if (previous?.serial === incoming.serial && Date.parse(previous.checked_at) > Date.parse(incoming.checked_at)) return previous
  return incoming
}

export function visibleDeviceStatus(status: DeviceStatus, phone?: PhoneState): DeviceStatus {
  const connected = status.connected && (!phone || phone.adb_state === 'device')
  return { ...status, serial: status.serial || phone?.serial, phone, connected,
    running: connected && status.running, paused: connected && status.paused,
    current: connected ? status.current : undefined }
}

export function phoneStateLabel(phone?: PhoneState): string {
  if (!phone) return '尚未確認手機狀態'
  if (phone.adb_state === 'missing' || phone.adb_state === 'offline') return '裝置離線'
  if (phone.adb_state === 'unauthorized') return '等待 USB 偵錯授權'
  if (phone.adb_state === 'unselected') return '尚未選擇手機'
  if (phone.adb_state === 'recovery' || phone.adb_state === 'bootloader' || phone.adb_state === 'sideload') return '手機處於系統維護模式'
  if (phone.adb_state !== 'device') return '無法確認手機狀態'
  const screen = { on: '螢幕開啟', off: '螢幕關閉', dozing: '螢幕待機', unknown: '螢幕狀態未知' }[phone.screen_state]
  return `${screen}${phone.locked === true ? ' · 已鎖定' : ''}`
}

export function phoneStateHint(phone?: PhoneState): string {
  if (!phone) return '選擇並連接手機後會自動檢查狀態。'
  if (phone.adb_state === 'missing' || phone.adb_state === 'offline') return '可能已關機或 USB 中斷；請確認手機已開機並接上 USB。'
  if (phone.adb_state === 'unauthorized') return '請解鎖手機並允許這台電腦的 USB 偵錯。'
  if (phone.adb_state !== 'device') return phone.error || '等待手機回到 Android 並恢復 ADB 連線。'
  if (phone.locked) return '可喚醒螢幕；PIN、密碼或圖形鎖需在手機上解鎖。'
  if (phone.screen_state === 'off' || phone.screen_state === 'dozing') return '手機仍在線，可喚醒螢幕後繼續操作。'
  return phone.error || '透過 ADB 自動檢查，與 LocWarp 控制服務分開偵測。'
}
