import { useCallback, useEffect, useState } from 'react'
import * as api from '../services/api'
import type { AdbDevice, DeviceStatus, PhoneState, ServerEvent } from '../types'
import { newerPhoneState, visibleDeviceStatus } from '../utils/phoneState'

const LAST_USB_DEVICE_KEY = 'bloompilot.lastUsbDevice'

function lastUsbDevice(): string {
  try { return window.localStorage.getItem(LAST_USB_DEVICE_KEY)?.trim() || '' }
  catch { return '' }
}

function rememberUsbDevice(serial: string): void {
  try { window.localStorage.setItem(LAST_USB_DEVICE_KEY, serial) }
  catch { /* Storage can be unavailable without preventing USB control. */ }
}

function automaticDevice(devices: AdbDevice[]): AdbDevice | undefined {
  const ready = devices.filter((device) => device.state === 'device')
  const previousSerial = lastUsbDevice()
  return ready.find((device) => device.serial === previousSerial)
    ?? (ready.length === 1 ? ready[0] : undefined)
}

export function useDevice(subscribe: (handler: (event: ServerEvent) => void) => () => void) {
  const [devices, setDevices] = useState<AdbDevice[]>([])
  const [status, setStatus] = useState<DeviceStatus>({ connected: false })
  const [phone, setPhone] = useState<PhoneState>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const scan = useCallback(async () => {
    setBusy(true)
    setError(null)
    try { setDevices(await api.listDevices()) }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setBusy(false) }
  }, [])

  const refresh = useCallback(async () => {
    try { setStatus(await api.getDeviceStatus()) }
    catch { setStatus((previous) => ({ ...previous, connected: false })) }
  }, [])

  const observedSerial = status.serial || lastUsbDevice()
  useEffect(() => {
    if (!observedSerial) return
    let cancelled = false
    let timer: number
    const poll = async () => {
      try {
        const next = await api.getPhoneState(observedSerial)
        if (!cancelled) setPhone((previous) => newerPhoneState(previous, next))
      } catch {
        if (!cancelled) setPhone({ serial: observedSerial, adb_state: 'unknown', screen_state: 'unknown', locked: null, battery_percent: null, power_connected: null, checked_at: new Date().toISOString(), error: '手機狀態查詢失敗，正在重新確認。' })
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 5000)
    }
    void poll()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [observedSerial])

  const wake = useCallback(async () => {
    if (!observedSerial) return
    setBusy(true)
    setError(null)
    try {
      const next = await api.wakePhone(observedSerial)
      setPhone((previous) => newerPhoneState(previous, next))
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally { setBusy(false) }
  }, [observedSerial, refresh])

  const connect = useCallback(async (serial: string) => {
    setBusy(true)
    setError(null)
    try {
      const next = await api.connectDevice(serial)
      rememberUsbDevice(serial)
      setStatus(next)
      return next
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason)
      setError(message)
      throw reason
    } finally { setBusy(false) }
  }, [])

  const disconnect = useCallback(async () => {
    setBusy(true)
    try { setStatus(await api.disconnectDevice()) }
    finally { setBusy(false) }
  }, [])

  const command = useCallback(async (name: string, payload: Record<string, unknown> = {}) => {
    setError(null)
    try {
      const result = await api.deviceCommand(name, payload)
      setStatus({ ...result.status, connected: true, serial: status.serial })
      return result
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      throw reason
    }
  }, [status.serial])

  useEffect(() => subscribe((event) => {
    const next = event.device
    if (event.type === 'device_update' && next) {
      setStatus((previous) => ({
        ...next,
        scale_calibration_required: next.scale_calibration_required
          ?? (previous.serial === next.serial ? previous.scale_calibration_required : undefined),
      }))
    }
  }), [subscribe])

  useEffect(() => {
    let cancelled = false

    const initialize = async () => {
      setBusy(true)
      setError(null)
      try {
        const [nextDevices, currentStatus] = await Promise.all([
          api.listDevices(),
          api.getDeviceStatus(),
        ])
        if (cancelled) return
        setDevices(nextDevices)
        setStatus(currentStatus)

        if (currentStatus.connected) {
          if (currentStatus.serial) rememberUsbDevice(currentStatus.serial)
          return
        }

        const candidate = automaticDevice(nextDevices)
        if (!candidate) return
        const connectedStatus = await api.connectDevice(candidate.serial)
        rememberUsbDevice(candidate.serial)
        if (!cancelled) setStatus(connectedStatus)
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        if (!cancelled) setBusy(false)
      }
    }

    void initialize()
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer: number
    const delay = status.connected ? 2000 : 5000
    const poll = async () => {
      await refresh()
      if (!cancelled) timer = window.setTimeout(() => void poll(), delay)
    }
    timer = window.setTimeout(() => void poll(), delay)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [status.connected, refresh])

  const currentPhone = phone?.serial === observedSerial ? phone : status.phone
  const visibleStatus = visibleDeviceStatus(status, currentPhone)
  return { devices, status: visibleStatus, busy, error, scan, refresh, connect, disconnect, command, wake }
}
