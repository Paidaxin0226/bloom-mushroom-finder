import assert from 'node:assert/strict'
import test from 'node:test'
import { editWaypoint, restoreGeometry, parseDraftNumber, formatDraftNumber } from '../src/utils/sequenceEditor.ts'

test('coordinate drafts distinguish blank or incomplete values from legitimate zero', () => {
  for (const value of ['', ' ', '-', 'hello']) assert.ok(Number.isNaN(parseDraftNumber(value)))
  assert.equal(parseDraftNumber('0'), 0)
  assert.equal(parseDraftNumber('-23.5'), -23.5)
  assert.equal(formatDraftNumber(1.55579666666, 3), '1.556')
})

test('restoring coordinates restores original coverage and preserves unrelated edits', () => {
  const original = { id: 'city', lat: 16.8557354, lng: 74.5618241, name: 'city', scan_spans: [[0, 0, 2]], wait_seconds: 10 }
  const changed = editWaypoint(original, {lat:16.8557355}, original)
  assert.equal(changed.scan_spans, undefined)
  const renamed = editWaypoint(changed, {name:'renamed', wait_seconds:20}, original)
  const restored = editWaypoint(renamed, {lat:original.lat}, original)
  assert.deepEqual(restored.scan_spans, original.scan_spans)
  assert.equal(restored.name, 'renamed')
  assert.equal(restored.wait_seconds, 20)
  assert.deepEqual(restoreGeometry(renamed, original).scan_spans, original.scan_spans)
  assert.deepEqual(original.scan_spans, [[0,0,2]])
})

test('name and wait edits do not remove built-up coverage', () => {
  const original = { lat:25, lng:121, scan_spans:[[0,0,1]] }
  assert.equal(editWaypoint(original, {name:'new'}).scan_spans, original.scan_spans)
  assert.equal(editWaypoint(original, {lat:25}).scan_spans, original.scan_spans)
})
