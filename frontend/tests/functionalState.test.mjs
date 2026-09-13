import assert from 'node:assert/strict'
import test from 'node:test'
import { mergeJob, isActiveJob } from '../src/utils/jobState.ts'
import { parseRouteText, validCoordinate } from '../src/utils/routeImport.ts'

const job = (state, revision, extra = {}) => ({ id: 'job-a', kind: 'geo_sequence_recognition', state, revision, started_at: '2026-09-08T12:00:00Z', ...extra })

test('a delayed start or stop response cannot reactivate a completed job', () => {
  const completed = job('completed', 4)
  const history = mergeJob([completed], job('running', 2))
  assert.deepEqual(history, [completed])
  assert.equal(history.find(isActiveJob), undefined)
})
test('a delayed pause response cannot overwrite a newer resume or waypoint update', () => {
  const running = job('running', 9, { paused: false, current_waypoint: { waypoint_id: 'b' } })
  assert.deepEqual(mergeJob([running], job('running', 8, { paused: true })), [running])
})
test('terminal state also stays terminal with legacy unversioned responses', () => {
  assert.equal(mergeJob([job('cancelled', undefined)], job('stopping', undefined))[0].state, 'cancelled')
})
test('updating an older job preserves newest-first history', () => {
  const newer = job('running', 1, { id: 'new', started_at: '2026-09-08T13:00:00Z' })
  assert.deepEqual(mergeJob([newer], job('completed', 5)).map(item => item.id), ['new', 'job-a'])
})
test('empty and nonnumeric coordinates do not become latitude or longitude zero', () => {
  for (const value of ['', ' ', null, undefined, false, [], {}, NaN, Infinity]) {
    assert.equal(validCoordinate(value, 121), null)
    assert.equal(validCoordinate(25, value), null)
  }
  assert.deepEqual(validCoordinate('0', 0), { lat: 0, lng: 0 })
  assert.equal(validCoordinate(91, 121), null)
})
test('GPX import accepts either attribute order, spacing, and namespaces', () => {
  const input = '<gpx><rtept lon="121" lat="25"/><trkpt lat = "26" lon = "122"/><g:wpt lng="123" lat="27"/></gpx>'
  assert.deepEqual(parseRouteText(input, 'route.gpx'), [{lat:25,lng:121},{lat:26,lng:122},{lat:27,lng:123}])
})
test('device routes accept exported BloomPilot sequences', () => {
  assert.deepEqual(parseRouteText(JSON.stringify({sequence:{waypoints:[{lat:25,lng:121}]}}), 'route.json'), [{lat:25,lng:121}])
})
test('route import keeps the 500 point device limit and rejects invalid JSON points', () => {
  assert.equal(parseRouteText(JSON.stringify(Array.from({length:600}, () => ({lat:25,lng:121}))), 'route.json').length, 500)
  assert.deepEqual(parseRouteText(JSON.stringify([{lat:null,lng:121},{lat:'',lng:121},{lat:25,lng:121}]), 'route.json'), [{lat:25,lng:121}])
})
