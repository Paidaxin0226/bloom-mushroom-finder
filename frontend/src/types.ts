export type PageKey =
  | 'device'
  | 'monthly'
  | 'recognition'

export interface AdbDevice {
  serial: string
  state: string
  model: string
}

export interface Coordinate {
  lat: number
  lng: number
}

export interface GeoWaypoint extends Coordinate {
  id: string
  name: string
  wait_seconds: number
  left_radius: number
  right_radius: number
  up_radius: number
  down_radius: number
  horizontal_radius?: number
  vertical_radius?: number
  spacing_m: number
  range_unit: 'km'
  left_km: number
  right_km: number
  up_km: number
  down_km: number
  horizontal_step_km: number
  vertical_step_km: number
  coverage_source?: string
  settlement_class?: string
  settlement_id?: string
  settlement_match?: string
  settlement_distance_km?: number
  scan_spans?: Array<[number, number, number]>
}

export interface GeoSequence {
  version: 1
  id: string
  name: string
  updated_at: string
  waypoints: GeoWaypoint[]
}

export interface ProjectRouteInfo {
  path: string
  name: string
  size_bytes: number
  waypoint_count: number | null
  search_point_count: number | null
  updated_at: string
  imported: boolean
}

export interface DeviceStatus {
  connected: boolean
  serial?: string
  scale_calibration_required?: boolean
  current?: Coordinate
  running?: boolean
  paused?: boolean
  mode?: string
  speedKmh?: number
  injected?: number
  [key: string]: unknown
}

export type JobState = 'queued' | 'running' | 'stopping' | 'completed' | 'cancelled' | 'error'

export interface JobRecord {
  id: string
  kind: string
  state: JobState
  message: string
  completed: number
  total: number
  started_at: string
  finished_at?: string | null
  result?: unknown
  error?: string | null
  paused?: boolean
  resuming?: boolean
  current_waypoint?: {
    sequence_index: number
    waypoint_id: string
    waypoint_name: string
    total_points: number
  } | null
  skipping_waypoint?: boolean
  deleting_waypoint?: boolean
  route_path?: string | null
  excursion?: {
    origin: Coordinate
    target: Coordinate
    label?: string
    serial?: string
  } | null
  resume_coordinate?: Coordinate | null
  resume_serial?: string | null
  recognition_filter?: {
    target_names?: string[] | null
    participant_count?: number | null
  } | null
}

export interface ModelInfo {
  path: string
  name: string
  size_bytes: number
  default: boolean
  class_names?: string[]
}

export interface DatasetInfo {
  path: string
  name: string
  latest: boolean
  image_count: number
  class_count: number
}

export interface MonthlyBackground {
  path: string
  name: string
  route: string
  size_bytes: number
  modified_ns: number
}

export interface MonthlyActivityTarget {
  path: string
  name: string
  size_bytes: number
}

export interface MonthlyWorkflowStatus {
  cycle: string
  fixed_routes: string[]
  resident_model: string | null
  resident_updated_at?: string | null
  resident_source?: string | null
  resident_class_names: string[]
  backgrounds: MonthlyBackground[]
  background_count: number
  reviewed: boolean
  reviewed_at?: string | null
  activity_targets: MonthlyActivityTarget[]
  route_paths: string[]
  background_route: {
    name: string
    towns: string[]
    waypoint_count: number
    search_point_count: number
  }
  collection_completed_at?: string | null
  background_policy?: string | null
  baseline_background_count?: number
  monthly_background_count?: number
  intermediates_cleaned_at?: string | null
  resident_dataset?: string | null
  resident_training_run?: string | null
  event_dataset?: string | null
  event_model?: string | null
  completed_at?: string | null
}

export interface ServerEvent {
  type: string
  state?: string
  job?: JobRecord
  job_id?: string
  data_url?: string
  device?: DeviceStatus
  location?: unknown
  detections?: Array<Record<string, unknown>>
  memory?: Record<string, unknown>
  detail?: unknown
  settings?: Record<string, unknown>
  message?: string
}
