import React, { useState } from 'react'
import type { JobRecord, ModelInfo } from '../types'
import { Icon } from './Icons'
import { sanitizeTerminalOutput } from '../utils/terminalOutput'

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return <label className="field"><span className="field-label">{label}</span>{children}{hint && <span className="field-hint">{hint}</span>}</label>
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (value: boolean) => void; label: string }) {
  return <label className="toggle-row"><button type="button" role="switch" aria-checked={checked} className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><span /></button><span>{label}</span></label>
}

export function ModelPicker({ models, value, onChange, onRefresh, onSetDefault }: { models: ModelInfo[]; value: string; onChange: (value: string) => void; onRefresh: () => Promise<void>; onSetDefault: (value: string) => Promise<void> }) {
  const [savingDefault, setSavingDefault] = useState(false)
  const selectedIsDefault = models.some((model) => model.path === value && model.default)
  const saveDefault = async () => {
    if (!value || selectedIsDefault) return
    setSavingDefault(true)
    try { await onSetDefault(value) } catch { /* App already reports the API error */ }
    finally { setSavingDefault(false) }
  }
  return <div className="picker-row model-picker-row"><select className="select-field" value={value} onChange={(event) => onChange(event.target.value)}><option value="">{models.length ? '選擇專案訓練模型' : '尚無專案訓練模型'}</option>{models.map((model) => <option key={model.path} value={model.path}>{model.default ? '★ ' : ''}{model.name} · {model.path.replaceAll('\\', '/').split('/').slice(-3).join('/')}</option>)}</select><button className="icon-btn" type="button" title="重新掃描專案模型" onClick={() => void onRefresh()}><Icon name="refresh" /></button><button className="model-default-btn" type="button" disabled={!value || selectedIsDefault || savingDefault} onClick={() => void saveDefault()}>{savingDefault ? '保存中…' : selectedIsDefault ? '目前預設' : '設為預設'}</button></div>
}

export function ActionButton({ children, icon, kind = 'default', disabled, onClick }: { children: React.ReactNode; icon?: Parameters<typeof Icon>[0]['name']; kind?: 'default' | 'primary' | 'danger'; disabled?: boolean; onClick?: () => void }) {
  return <button type="button" className={`action-btn ${kind}`} disabled={disabled} onClick={onClick}>{icon && <Icon name={icon} size={16} />}{children}</button>
}

export function JobProgress({ job }: { job: JobRecord | null }) {
  if (!job) return <div className="empty-state compact">目前沒有執行中的工作</div>
  const output = job.result && typeof job.result === 'object' && 'output' in job.result ? String(job.result.output) : ''
  const percent = job.total > 0 ? Math.min(100, Math.round(job.completed / job.total * 100)) : 0
  return <div className="job-progress"><div className="job-progress-head"><span className={`state-dot ${job.state}`} /><strong>{job.kind === 'geo_sequence_recognition' ? '座標序列辨識' : job.kind}</strong><span>{({ running: '執行中', queued: '準備中', stopping: '停止中', completed: '已完成', cancelled: '已停止', error: '失敗' } as Record<string, string>)[job.state] ?? job.state}</span><span className="mono">{job.total > 0 ? `${job.completed}/${job.total}` : '—'}</span></div><div className="progress-track"><div className="progress-fill" style={{ width: job.total > 0 ? `${percent}%` : '22%' }} /></div><div className="job-message">{sanitizeTerminalOutput(job.message) || '準備中…'}</div>{output && <div className="job-message" style={{ overflowWrap: 'anywhere', userSelect: 'text' }}>結果位置：{output}</div>}</div>
}

export function JobConsole({ logs }: { logs: string[] }) {
  const lines = logs.map(sanitizeTerminalOutput).filter(Boolean)
  return <div className="console">{lines.length ? lines.map((line, index) => <div key={`${index}-${line}`}><span className="console-index">{String(index + 1).padStart(3, '0')}</span>{line}</div>) : <div className="console-muted">工作訊息會顯示在這裡</div>}</div>
}

export function PreviewStage({ source, title = '即時預覽' }: { source: string | null; title?: string }) {
  return <div className="preview-stage"><div className="preview-title"><span>{title}</span><span className={`live-indicator ${source ? 'active' : ''}`}><i />{source ? 'LIVE · 等比例完整畫面' : 'IDLE'}</span></div><div className="preview-canvas">{source ? <div className="preview-frame" role="img" aria-label={title} style={{ backgroundImage: `url(${source})` }} /> : <div className="preview-empty"><Icon name="vision" size={42} /><span>等待手機畫面</span></div>}</div></div>
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: React.ReactNode }) {
  return <header className="page-header"><div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</header>
}
