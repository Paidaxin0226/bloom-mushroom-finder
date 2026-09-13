import type { Coordinate } from '../types'

export function validCoordinate(lat: unknown, lng: unknown): Coordinate | null {
  const numeric = (value: unknown) => (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '')) && Number.isFinite(Number(value))
  if (!numeric(lat) || !numeric(lng)) return null
  const point = { lat: Number(lat), lng: Number(lng) }
  return Number.isFinite(point.lat) && Number.isFinite(point.lng) && Math.abs(point.lat) <= 90 && Math.abs(point.lng) <= 180 ? point : null
}

function collectJsonPoints(value: unknown, output: Coordinate[]) {
  if (output.length >= 500) return
  if (Array.isArray(value)) {
    if (value.length >= 2 && typeof value[0] === 'number' && typeof value[1] === 'number') {
      const point = validCoordinate(value[1], value[0])
      if (point) output.push(point)
      return
    }
    value.forEach((item) => collectJsonPoints(item, output))
    return
  }
  const item = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  if ('lat' in item && ('lng' in item || 'lon' in item)) {
    const point = validCoordinate(item.lat as number, (item.lng ?? item.lon) as number)
    if (point) output.push(point)
    return
  }
  if ('sequence' in item) collectJsonPoints(item.sequence, output)
  else if ('waypoints' in item) collectJsonPoints(item.waypoints, output)
  else if ('coordinates' in item) collectJsonPoints(item.coordinates, output)
  else if ('points' in item) collectJsonPoints(item.points, output)
  else if ('route' in item) collectJsonPoints(item.route, output)
  else if ('features' in item) collectJsonPoints(item.features, output)
  else if ('geometry' in item) collectJsonPoints(item.geometry, output)
}

export function parseRouteText(text: string, fileName: string): Coordinate[] {
  const output: Coordinate[] = []
  if (/\.(json|geojson)$/i.test(fileName)) {
    collectJsonPoints(JSON.parse(text), output)
  } else if (/\.(gpx|kml|xml)$/i.test(fileName)) {
    const tags = text.matchAll(/<(?:[\w-]+:)?(?:trkpt|rtept|wpt)\b[^>]*>/gi)
    for (const [tag] of tags) {
      if (output.length >= 500) break
      const lat = tag.match(/\blat\s*=\s*["']([^"']+)["']/i)?.[1]
      const lng = tag.match(/\b(?:lon|lng)\s*=\s*["']([^"']+)["']/i)?.[1]
      const point = validCoordinate(lat, lng)
      if (point) output.push(point)
    }
    const coordinateBlocks = [...text.matchAll(/<coordinates[^>]*>([\s\S]*?)<\/coordinates>/gi)]
    coordinateBlocks.forEach((block) => block[1].trim().split(/\s+/).forEach((pair) => {
      if (output.length >= 500) return
      const [lng, lat] = pair.split(','); const point = validCoordinate(lat, lng); if (point) output.push(point)
    }))
  } else {
    for (const match of text.matchAll(/(-?\d{1,2}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)/g)) {
      const point = validCoordinate(match[1], match[2]); if (point) output.push(point)
      if (output.length >= 500) break
    }
  }
  return output
}
