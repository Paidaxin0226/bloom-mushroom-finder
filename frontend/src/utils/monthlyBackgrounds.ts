import type { MonthlyBackground } from '../types'

export function backgroundTown(background: MonthlyBackground): string {
  return (background.waypoint || background.path.split('/').at(-2) || background.route).replace(/^wp_\d+_/, '').replace(/^background-/, '').replaceAll('_', ' ')
}

export function filterBackgrounds(backgrounds: MonthlyBackground[], query: string, town: string, sort: string): MonthlyBackground[] {
  const needle = query.trim().toLocaleLowerCase()
  return backgrounds.filter(item => (!town || backgroundTown(item) === town)
    && (!needle || `${item.path} ${backgroundTown(item)}`.toLocaleLowerCase().includes(needle)))
    .sort((a, b) => sort === 'name' ? a.path.localeCompare(b.path)
      : (sort === 'oldest' ? a.modified_ns - b.modified_ns : b.modified_ns - a.modified_ns) || a.path.localeCompare(b.path))
}

export function backgroundPage<T>(items: T[], requested: number, pageSize: number) {
  const pages = Math.max(1, Math.ceil(items.length / pageSize))
  const page = Math.max(0, Math.min(requested, pages - 1))
  return { page, pages, items: items.slice(page * pageSize, (page + 1) * pageSize) }
}
