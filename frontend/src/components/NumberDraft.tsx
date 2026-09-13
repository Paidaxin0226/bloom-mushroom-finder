import React, { useEffect, useRef, useState } from 'react'
import { formatDraftNumber, parseDraftNumber } from '../utils/sequenceEditor'

// Keep the edit buffer separate from the numeric model: blank and '-' are not zero.
export function NumberDraft({ value, onValue, min = -Infinity, max = Infinity, digits = 7, ...props }: Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'min' | 'max'> & { value: number; onValue: (value: number) => void; min?: number; max?: number; digits?: number }) {
  const [draft, setDraft] = useState(() => formatDraftNumber(value, digits))
  const emitted = useRef(value)
  useEffect(() => {
    if (!Object.is(value, emitted.current)) setDraft(formatDraftNumber(value, digits))
    emitted.current = value
  }, [value, digits])
  const parsed = parseDraftNumber(draft)
  const invalid = !Number.isFinite(parsed) || parsed < min || parsed > max
  return <><input {...props} type="text" inputMode="decimal" value={draft} aria-invalid={invalid} onChange={event => {
    setDraft(event.target.value)
    emitted.current = parseDraftNumber(event.target.value)
    onValue(emitted.current)
  }}/>{invalid && <small className="range-builder-error">{draft.trim() ? '請輸入有效範圍內的數字。' : '此欄位不能留空。'}</small>}</>
}
