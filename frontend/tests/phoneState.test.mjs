import assert from 'node:assert/strict'
import test from 'node:test'
import { newerPhoneState, phoneStateHint, phoneStateLabel, visibleDeviceStatus } from '../src/utils/phoneState.ts'

const phone = (adb_state, screen_state = 'unknown', locked = null) => ({ serial: 'phone', adb_state, screen_state, locked, battery_percent: null, power_connected: null, checked_at: '', error: null })

test('screen off remains an online phone instead of being called powered off', () => {
  const sleeping = phone('device', 'off')
  assert.equal(phoneStateLabel(sleeping), '螢幕關閉')
  assert.match(phoneStateHint(sleeping), /仍在線/)
  assert.equal(visibleDeviceStatus({ connected: true }, sleeping).connected, true)
})
test('missing device is offline without claiming shutdown or unplug as a fact', () => {
  const missing = phone('missing')
  assert.equal(phoneStateLabel(missing), '裝置離線')
  assert.match(phoneStateHint(missing), /可能已關機或 USB 中斷/)
})
test('offline physical state clears stale movement and live GPS', () => {
  const result = visibleDeviceStatus({ connected: true, running: true, paused: true, current: {lat:25,lng:121} }, phone('offline'))
  assert.equal(result.connected, false)
  assert.equal(result.running, false)
  assert.equal(result.paused, false)
  assert.equal(result.current, undefined)
})
test('an awake screen with secure lock still asks for manual unlock', () => {
  assert.match(phoneStateLabel(phone('device', 'on', true)), /已鎖定/)
  assert.match(phoneStateHint(phone('device', 'on', true)), /手機上解鎖/)
})
test('failed physical probe cannot keep showing an online device', () => {
  assert.equal(visibleDeviceStatus({connected:true}, phone('unknown')).connected, false)
  assert.equal(phoneStateLabel(phone('unknown')), '無法確認手機狀態')
})
test('a delayed pre-wake poll cannot overwrite the newer awake result', () => {
  const awake = {...phone('device','on'),checked_at:'2026-09-11T13:00:05Z'}
  const sleeping = {...phone('device','off'),checked_at:'2026-09-11T13:00:00Z'}
  assert.equal(newerPhoneState(awake,sleeping),awake)
  assert.equal(newerPhoneState(awake,{...sleeping,serial:'another-phone'}).serial,'another-phone')
})
