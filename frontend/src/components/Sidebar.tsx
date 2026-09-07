import React from 'react'
import type { PageKey } from '../types'
import type { IconName } from './Icons'
import { Icon } from './Icons'

const pages: Array<{ key: PageKey; label: string; icon: IconName; group: string }> = [
  { key: 'device', label: '裝置控制', icon: 'device', group: 'CONTROL' },
  { key: 'monthly', label: '每月活動菇模型', icon: 'train', group: 'VISION PIPELINE' },
  { key: 'recognition', label: '手機辨識', icon: 'phoneVision', group: 'RECOGNITION' },
]

export default function Sidebar({ active, onChange, device }: { active: PageKey; onChange: (page: PageKey) => void; device: React.ReactNode }) {
  let group = ''
  return <aside className="sidebar"><div className="sidebar-header"><div className="brand-mark"><span /><span /><span /></div><div><h1>BloomPilot</h1><p>Movement · Vision · YOLO</p></div></div><div className="sidebar-content">{device}<nav className="sidebar-section nav-section">{pages.map((page) => { const heading = page.group !== group ? (group = page.group, <div className="nav-group" key={`${page.group}-heading`}>{page.group}</div>) : null; return <React.Fragment key={page.key}>{heading}<button className={`nav-item ${active === page.key ? 'active' : ''}`} onClick={() => onChange(page.key)}><Icon name={page.icon}/><span>{page.label}</span><Icon name="chevron" size={13} className="nav-chevron"/></button></React.Fragment> })}</nav></div><div className="sidebar-footer"><span className="status-connection-dot connected"/><span>USB local bridge</span><b>v0.2.0</b></div></aside>
}
