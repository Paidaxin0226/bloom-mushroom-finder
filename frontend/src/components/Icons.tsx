import React from 'react'

export type IconName = 'device' | 'map' | 'scan' | 'dataset' | 'train' | 'vision' | 'live' | 'phoneVision' | 'autoSearch' | 'route' | 'box' | 'refresh' | 'play' | 'stop' | 'pause' | 'restore' | 'folder' | 'pin' | 'steps' | 'chevron'

const paths: Record<IconName, React.ReactNode> = {
  device: <><rect x="6" y="2" width="12" height="20" rx="2"/><line x1="10" y1="18" x2="14" y2="18"/></>,
  map: <><polygon points="3,6 9,3 15,6 21,3 21,18 15,21 9,18 3,21"/><line x1="9" y1="3" x2="9" y2="18"/><line x1="15" y1="6" x2="15" y2="21"/></>,
  scan: <><path d="M4 7V4h3M17 4h3v3M20 17v3h-3M7 20H4v-3"/><circle cx="12" cy="12" r="3"/></>,
  dataset: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></>,
  train: <><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M7 11v5c3 2 7 2 10 0v-5"/><path d="M21 8v7"/></>,
  vision: <><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></>,
  live: <><rect x="3" y="5" width="18" height="14" rx="2"/><polygon points="10,9 16,12 10,15"/></>,
  phoneVision: <><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M8.5 11s1.2-2 3.5-2 3.5 2 3.5 2-1.2 2-3.5 2-3.5-2-3.5-2z"/><circle cx="12" cy="11" r="1"/><line x1="10" y1="18" x2="14" y2="18"/></>,
  autoSearch: <><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4"/><circle cx="12" cy="12" r="3"/><path d="M12 7v2M17 12h-2M12 17v-2M7 12h2"/></>,
  route: <><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h3a3 3 0 003-3V9a3 3 0 013-3"/><path d="M4 6c0 2 2 4 2 4s2-2 2-4a2 2 0 10-4 0z"/></>,
  box: <><path d="M21 8l-9 5-9-5"/><path d="M3 8l9-5 9 5v10l-9 5-9-5z"/><line x1="12" y1="13" x2="12" y2="23"/></>,
  refresh: <><polyline points="20 4 20 10 14 10"/><polyline points="4 20 4 14 10 14"/><path d="M5.1 9a8 8 0 0113.2-3L20 10M4 14l1.7 4A8 8 0 0018.9 15"/></>,
  play: <polygon points="7,4 20,12 7,20"/>,
  stop: <rect x="6" y="6" width="12" height="12" rx="1"/>,
  pause: <><line x1="9" y1="5" x2="9" y2="19"/><line x1="15" y1="5" x2="15" y2="19"/></>,
  restore: <><path d="M3 12a9 9 0 109-9 9.4 9.4 0 00-6.4 2.6L3 8"/><polyline points="3 3 3 8 8 8"/></>,
  folder: <path d="M3 6h7l2 2h9v11H3z"/>,
  pin: <><path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1116 0z"/><circle cx="12" cy="10" r="2.5"/></>,
  steps: <><path d="M8 3c2 0 3 3 2 5S7 12 5 11 4 8 5 6s1-3 3-3z"/><path d="M16 12c2 0 3 3 2 5s-3 4-5 3-1-3 0-5 1-3 3-3z"/></>,
  chevron: <polyline points="9 18 15 12 9 6"/>,
}

export function Icon({ name, size = 18, className }: { name: IconName; size?: number; className?: string }) {
  return <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>{paths[name]}</svg>
}
