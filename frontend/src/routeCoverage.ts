import { VIEWPORT_WIDTH_M } from './projectDefaults'

export const REFERENCE_SCREEN_WIDTH_PX = 1080
export const REFERENCE_SCREEN_HEIGHT_PX = 2340

export type CoverageAxis = 'horizontal' | 'vertical'

const swipePixels = (axis: CoverageAxis) => axis === 'horizontal'
  ? Math.abs(Math.round(REFERENCE_SCREEN_WIDTH_PX * 0.17) - Math.round(REFERENCE_SCREEN_WIDTH_PX * 0.83))
  : Math.abs(Math.round(REFERENCE_SCREEN_HEIGHT_PX * 0.15) - Math.round(REFERENCE_SCREEN_HEIGHT_PX * 0.81))

export const viewportHalfKm = (axis: CoverageAxis, stepKm?: number) => {
  const pixels = axis === 'horizontal' ? REFERENCE_SCREEN_WIDTH_PX : REFERENCE_SCREEN_HEIGHT_PX
  const halfKm = VIEWPORT_WIDTH_M / REFERENCE_SCREEN_WIDTH_PX * pixels / 2000
  // A calibrated step covers the same reference swipe percentage.
  return stepKm === undefined ? halfKm : halfKm * (gridStepKm(axis, stepKm) / gridStepKm(axis))
}

export const gridStepKm = (axis: CoverageAxis, stepKm?: number) => {
  if (stepKm !== undefined) return Number.isFinite(stepKm) && stepKm > 0 ? stepKm : Number.NaN
  return swipePixels(axis) * (VIEWPORT_WIDTH_M / REFERENCE_SCREEN_WIDTH_PX) / 1000
}

export const coverageKmToRadius = (distanceKm: number, axis: CoverageAxis, stepKm?: number) => {
  if (!Number.isFinite(distanceKm) || distanceKm < 0) return Number.NaN
  return Math.max(0, Math.ceil(Math.max(0, distanceKm - viewportHalfKm(axis, stepKm)) / gridStepKm(axis, stepKm)))
}

export const radiusToCoverageKm = (radius: number, axis: CoverageAxis, stepKm?: number) => (
  viewportHalfKm(axis, stepKm) + Math.max(0, radius) * gridStepKm(axis, stepKm)
)
