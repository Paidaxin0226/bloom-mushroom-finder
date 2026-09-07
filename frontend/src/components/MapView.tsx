import React, { useEffect, useRef } from 'react'
import L from 'leaflet'
import type { Coordinate } from '../types'

export interface MapContextRequest {
  coordinate: Coordinate
  clientX: number
  clientY: number
}

interface MapBookmark {
  id: string
  name: string
  point: Coordinate
}

interface Props {
  position?: Coordinate | null
  selected?: Coordinate | null
  onSelect?: (coordinate: Coordinate) => void
  onContextRequest?: (request: MapContextRequest) => void
  points?: Coordinate[]
  route?: Coordinate[]
  bookmarks?: MapBookmark[]
  tileTemplate?: string
  tileAttribution?: string
}

function markerIcon(className: string, title = '') {
  return L.divIcon({ className: '', html: `<div class="${className}" title="${title.replaceAll('"', '&quot;')}"><span></span></div>`, iconSize: [28, 28], iconAnchor: [14, 14] })
}

export default function MapView({ position, selected, onSelect, onContextRequest, points = [], route = [], bookmarks = [], tileTemplate = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', tileAttribution = '© OpenStreetMap' }: Props) {
  const host = useRef<HTMLDivElement | null>(null)
  const map = useRef<L.Map | null>(null)
  const tileLayer = useRef<L.TileLayer | null>(null)
  const currentMarker = useRef<L.Marker | null>(null)
  const selectedMarker = useRef<L.Marker | null>(null)
  const pointLayer = useRef<L.LayerGroup | null>(null)
  const routeLayer = useRef<L.Polyline | null>(null)
  const bookmarkLayer = useRef<L.LayerGroup | null>(null)
  const selectHandler = useRef(onSelect)
  const contextHandler = useRef(onContextRequest)

  useEffect(() => { selectHandler.current = onSelect }, [onSelect])
  useEffect(() => { contextHandler.current = onContextRequest }, [onContextRequest])

  useEffect(() => {
    if (!host.current || map.current) return
    const instance = L.map(host.current, { zoomControl: false, attributionControl: false, worldCopyJump: true, doubleClickZoom: true }).setView([25.033, 121.5654], 14)
    L.control.zoom({ position: 'topright' }).addTo(instance)
    L.control.attribution({ position: 'bottomright', prefix: false }).addAttribution('© OpenStreetMap').addTo(instance)
    instance.on('click', (event) => selectHandler.current?.({ lat: event.latlng.lat, lng: event.latlng.lng }))
    instance.on('contextmenu', (event) => {
      const source = event.originalEvent as MouseEvent | undefined
      source?.preventDefault()
      const coordinate = { lat: event.latlng.lat, lng: event.latlng.lng }
      selectHandler.current?.(coordinate)
      contextHandler.current?.({ coordinate, clientX: source?.clientX ?? 0, clientY: source?.clientY ?? 0 })
    })
    map.current = instance
    pointLayer.current = L.layerGroup().addTo(instance)
    bookmarkLayer.current = L.layerGroup().addTo(instance)
    return () => { instance.remove(); map.current = null }
  }, [])

  useEffect(() => {
    const instance = map.current
    if (!instance) return
    tileLayer.current?.remove()
    const source = /^https:\/\//i.test(tileTemplate) ? tileTemplate : 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'
    tileLayer.current = L.tileLayer(source, { maxZoom: 22, attribution: tileAttribution }).addTo(instance)
    tileLayer.current.bringToBack()
  }, [tileTemplate, tileAttribution])

  useEffect(() => {
    const instance = map.current
    if (!instance || !position) return
    if (!currentMarker.current) currentMarker.current = L.marker([position.lat, position.lng], { icon: markerIcon('map-user-marker') }).addTo(instance)
    else currentMarker.current.setLatLng([position.lat, position.lng])
    if (!selected) instance.panTo([position.lat, position.lng], { animate: true })
  }, [position?.lat, position?.lng])

  useEffect(() => {
    const instance = map.current
    if (!instance) return
    if (!selected) { selectedMarker.current?.remove(); selectedMarker.current = null; return }
    if (!selectedMarker.current) selectedMarker.current = L.marker([selected.lat, selected.lng], { icon: markerIcon('map-target-marker') }).addTo(instance)
    else selectedMarker.current.setLatLng([selected.lat, selected.lng])
  }, [selected?.lat, selected?.lng])

  useEffect(() => {
    const layer = pointLayer.current
    if (!layer) return
    layer.clearLayers()
    points.forEach((point, index) => L.circleMarker([point.lat, point.lng], { radius: index === 0 ? 5 : 4, color: '#6c8cff', fillColor: '#6c8cff', fillOpacity: .82, weight: 2 }).bindTooltip(String(index + 1), { permanent: true, direction: 'center', className: 'route-index' }).addTo(layer))
  }, [points])

  useEffect(() => {
    const instance = map.current
    if (!instance) return
    routeLayer.current?.remove()
    routeLayer.current = route.length > 1 ? L.polyline(route.map((point): L.LatLngTuple => [point.lat, point.lng]), { color: '#4ecdc4', weight: 5, opacity: .82 }).addTo(instance) : null
  }, [route])

  useEffect(() => {
    const layer = bookmarkLayer.current
    if (!layer) return
    layer.clearLayers()
    bookmarks.forEach((bookmark) => L.marker([bookmark.point.lat, bookmark.point.lng], { icon: markerIcon('map-bookmark-marker', bookmark.name) }).bindTooltip(bookmark.name).addTo(layer))
  }, [bookmarks])

  return <div className="map-host" ref={host}/>
}
