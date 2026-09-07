import React from 'react'
import { Icon } from './Icons'

export type RecognitionMode = 'automatic' | 'sequence'

export default function RecognitionModeSwitch({ value, onChange, compact = false, lockedMode }: {
  value: RecognitionMode
  onChange: (value: RecognitionMode) => void
  compact?: boolean
  lockedMode?: RecognitionMode | null
}) {
  return <div className={`recognition-mode-switch ${compact ? 'compact' : ''}`} role="tablist" aria-label="手機辨識模式">
    <button type="button" role="tab" aria-selected={value === 'automatic'} className={value === 'automatic' ? 'active' : ''} disabled={Boolean(lockedMode && lockedMode !== 'automatic')} onClick={() => onChange('automatic')}>
      <Icon name="autoSearch" size={17}/><span><b>自動搜尋辨識</b>{!compact && <small>逐格滑動畫面</small>}</span>
    </button>
    <button type="button" role="tab" aria-selected={value === 'sequence'} className={value === 'sequence' ? 'active' : ''} disabled={Boolean(lockedMode && lockedMode !== 'sequence')} onClick={() => onChange('sequence')}>
      <Icon name="route" size={17}/><span><b>座標序列辨識</b>{!compact && <small>依序前往座標</small>}</span>
    </button>
  </div>
}
