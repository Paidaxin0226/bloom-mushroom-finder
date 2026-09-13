import type { JobRecord } from '../types'

export const isActiveJob = (job: JobRecord) => ['queued', 'running', 'stopping'].includes(job.state)

// HTTP responses and WebSocket events may arrive in either order.
export function mergeJob(history: JobRecord[], incoming: JobRecord): JobRecord[] {
  const previous = history.find((job) => job.id === incoming.id)
  if (previous && (
    (previous.revision ?? 0) > (incoming.revision ?? 0)
    || (!isActiveJob(previous) && isActiveJob(incoming))
  )) return history
  return [incoming, ...history.filter((job) => job.id !== incoming.id)]
    .sort((left, right) => right.started_at.localeCompare(left.started_at))
}
