import { useEffect, useMemo, useRef, useState } from 'react'
import type { MonthlyBackground } from '../types'
import { monthlyBackgroundUrl } from '../services/api'
import { backgroundPage, backgroundTown, filterBackgrounds } from '../utils/monthlyBackgrounds'

function BackgroundImage({ src, name, thumbnail = false }: { src: string; name: string; thumbnail?: boolean }) {
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => { setFailed(false); setAttempt(0) }, [src])
  return failed ? <span className="monthly-image-error">圖片載入失敗
    <button type="button" onClick={event => { event.stopPropagation(); setFailed(false); setAttempt(value => value + 1) }}>重試圖片</button>
  </span> : <img loading={thumbnail ? 'lazy' : 'eager'} decoding="async" src={`${src}&retry=${attempt}`} alt={name} onError={() => setFailed(true)} />
}

export default function MonthlyBackgroundReview({ cycle, backgrounds, busy, collecting, onDelete, onRefresh }: {
  cycle: string; backgrounds: MonthlyBackground[]; busy: boolean; collecting: boolean
  onDelete: (paths: string[]) => Promise<boolean>; onRefresh: () => void
}) {
  const [query, setQuery] = useState('')
  const [town, setTown] = useState('')
  const [sort, setSort] = useState('newest')
  const [view, setView] = useState('grid')
  const [requestedPage, setRequestedPage] = useState(0)
  const [pageSize, setPageSize] = useState(24)
  const [selected, setSelected] = useState<string[]>([])
  const [pending, setPending] = useState<string[]>([])
  const [previewPath, setPreviewPath] = useState<string | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const filtered = useMemo(() => filterBackgrounds(backgrounds, query, town, sort), [backgrounds, query, town, sort])
  const towns = useMemo(() => Array.from(new Set(backgrounds.map(backgroundTown))).sort(), [backgrounds])
  const { page, pages, items } = backgroundPage(filtered, requestedPage, pageSize)
  const paths = useMemo(() => new Set(backgrounds.map(item => item.path)), [backgrounds])
  const selection = selected.filter(path => paths.has(path))
  const preview = backgrounds.find(item => item.path === previewPath)
  const previewIndex = filtered.findIndex(item => item.path === previewPath)
  useEffect(() => { setRequestedPage(0) }, [query, town, sort, pageSize])
  useEffect(() => { if (collecting) { setSelected([]); setPending([]) } }, [collecting])
  useEffect(() => { if (requestedPage !== page) setRequestedPage(page) }, [requestedPage, page])
  useEffect(() => { if (town && !towns.includes(town)) setTown('') }, [towns, town])
  useEffect(() => {
    if (preview && !dialog.current?.open) dialog.current?.showModal()
    if (!preview) dialog.current?.close()
  }, [preview])

  const toggle = (path: string) => setSelected(current => current.includes(path) ? current.filter(item => item !== path) : [...current.filter(item => paths.has(item)), path].slice(0, 500))
  const deletePending = async () => {
    if (busy || !pending.length) return
    if (await onDelete(pending)) {
      setSelected(current => current.filter(path => !pending.includes(path)))
      setPending([])
    }
  }
  const movePreview = (offset: number) => {
    const next = filtered[previewIndex + offset]
    if (next) setPreviewPath(next.path)
  }

  return <div className="monthly-review-browser">
    <div className="monthly-review-filters">
      <input className="input" aria-label="搜尋背景圖片" placeholder="搜尋檔名、城鎮或路徑" value={query} onChange={event => setQuery(event.target.value)}/>
      <select className="input" aria-label="背景城鎮" value={town} onChange={event => setTown(event.target.value)}><option value="">全部城鎮</option>{towns.map(item => <option key={item}>{item}</option>)}</select>
      <select className="input" aria-label="背景排序" value={sort} onChange={event => setSort(event.target.value)}><option value="newest">最新在前</option><option value="oldest">最早在前</option><option value="name">檔名順序</option></select>
    </div>
    <div className="monthly-review-toolbar">
      <span>符合 {filtered.length.toLocaleString()} / {backgrounds.length.toLocaleString()} 張</span>
      <button type="button" aria-pressed={view === 'grid'} onClick={() => setView('grid')}>縮圖</button>
      <button type="button" aria-pressed={view === 'list'} onClick={() => setView('list')}>清單</button>
      <button type="button" disabled={busy && !collecting} onClick={onRefresh}>重新整理</button>
    </div>
    <div className={`monthly-background-grid ${view === 'list' ? 'monthly-background-list' : ''}`}>
      {items.map(item => <figure key={`${item.path}:${item.modified_ns}`} className={selection.includes(item.path) ? 'selected' : ''}>
        <div className="monthly-thumbnail" role="button" tabIndex={0} aria-label={`放大 ${item.name}`} onClick={() => setPreviewPath(item.path)} onKeyDown={event => { if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); setPreviewPath(item.path) } }}>
          <BackgroundImage thumbnail src={monthlyBackgroundUrl(cycle, item.path, item.modified_ns, true)} name={item.name}/>
        </div>
        <figcaption>
          <label><input type="checkbox" aria-label={`選取 ${item.path}`} disabled={busy || pending.length > 0 || (selection.length >= 500 && !selection.includes(item.path))} checked={selection.includes(item.path)} onChange={() => toggle(item.path)}/><span title={item.path}><b>{backgroundTown(item)}</b>{item.name}<small>{(item.size_bytes / 1024).toFixed(0)} KB</small></span></label>
          <button type="button" disabled={busy || pending.length > 0} onClick={() => setPending([item.path])}>刪除</button>
        </figcaption>
      </figure>)}
      {!items.length && <div className="monthly-empty">{backgrounds.length ? '沒有符合條件的圖片，請調整篩選。' : collecting ? '正在蒐集，第一張背景儲存後會自動顯示。' : '尚無背景圖片；已蒐集的圖片會顯示在這裡。'}</div>}
    </div>
    <div className="monthly-pagination">
      <button disabled={page === 0} onClick={() => setRequestedPage(page - 1)}>上一頁</button>
      <span>{page + 1} / {pages} 頁</span>
      <button disabled={page + 1 === pages} onClick={() => setRequestedPage(page + 1)}>下一頁</button>
      <select aria-label="每頁圖片數" value={pageSize} onChange={event => setPageSize(Number(event.target.value))}>{[12, 24, 48].map(size => <option key={size} value={size}>{size} 張 / 頁</option>)}</select>
    </div>
    <div className="monthly-review-toolbar">
      <button disabled={busy || !items.length || pending.length > 0} onClick={() => setSelected(Array.from(new Set([...selection, ...items.map(item => item.path)])).slice(0, 500))}>選取本頁</button>
      <button disabled={busy || !selection.length || pending.length > 0} onClick={() => setSelected([])}>清除選取</button>
      <span>已選 {selection.length} 張（含其他頁，最多 500 張）</span>
      <button className="danger" disabled={busy || !selection.length || pending.length > 0} onClick={() => setPending(selection)}>刪除所選</button>
    </div>
    {pending.length > 0 && <div className="monthly-delete-confirm" role="alert">
      <b>確定刪除以下 {pending.length} 張背景？刪除後無法復原，需重新核准背景。</b>
      <ul>{pending.map(path => <li key={path}>{path}</li>)}</ul>
      <div className="monthly-review-toolbar"><button className="danger" disabled={busy} onClick={() => void deletePending()}>確認刪除 {pending.length} 張</button><button disabled={busy} onClick={() => setPending([])}>取消</button></div>
    </div>}
    <dialog ref={dialog} className="monthly-preview-dialog" aria-label="背景原圖預覽" onClose={() => setPreviewPath(null)} onClick={event => { if (event.target === dialog.current) dialog.current.close() }} onKeyDown={event => { if (event.key === 'ArrowLeft') movePreview(-1); if (event.key === 'ArrowRight') movePreview(1) }}>
      {preview && <div className="monthly-preview-content">
        <div className="monthly-review-toolbar"><b>{backgroundTown(preview)}</b><button onClick={() => dialog.current?.close()}>關閉預覽</button></div>
        <BackgroundImage src={monthlyBackgroundUrl(cycle, preview.path, preview.modified_ns)} name={preview.name}/>
        <div className="monthly-preview-path">{preview.path}<small>{(preview.size_bytes / 1024).toFixed(0)} KB · {new Date(preview.modified_ns / 1e6).toLocaleString()}</small></div>
        <div className="monthly-review-toolbar"><button disabled={previewIndex <= 0} onClick={() => movePreview(-1)}>上一張</button><span>{previewIndex + 1} / {filtered.length}</span><button disabled={previewIndex < 0 || previewIndex + 1 >= filtered.length} onClick={() => movePreview(1)}>下一張</button><button disabled={busy || pending.length > 0 || (selection.length >= 500 && !selection.includes(preview.path))} onClick={() => toggle(preview.path)}>{selection.includes(preview.path) ? '取消選取' : '選取此圖'}</button></div>
      </div>}
    </dialog>
  </div>
}
