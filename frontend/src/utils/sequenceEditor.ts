import type { GeoWaypoint } from '../types'

export const geometryKeys = ['lat', 'lng', 'left_radius', 'right_radius', 'up_radius', 'down_radius', 'left_km', 'right_km', 'up_km', 'down_km', 'horizontal_step_km', 'vertical_step_km'] as const

export function editWaypoint(current: GeoWaypoint, patch: Partial<GeoWaypoint>, original?: GeoWaypoint): GeoWaypoint {
  const next = { ...current, ...patch }
  if (!geometryKeys.some(key => key in patch)) return next
  const baseline = original ?? current
  const unchanged = geometryKeys.every(key => Object.is(next[key], baseline[key]))
  return { ...next, scan_spans: unchanged ? baseline.scan_spans : undefined }
}

export function restoreGeometry(current: GeoWaypoint, original: GeoWaypoint): GeoWaypoint {
  const geometry = Object.fromEntries(geometryKeys.map(key => [key, original[key]]))
  return { ...current, ...geometry, scan_spans: original.scan_spans }
}

export function parseDraftNumber(text: string): number {
  return text.trim() === '' ? NaN : Number(text)
}

export function formatDraftNumber(value: number, digits = 7): string {
  return Number.isFinite(value) ? String(Number(value.toFixed(digits))) : ''
}
