import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import type { DeviceStatus, MonthlyWorkflowStatus } from '../types'
import {
  approveMonthlyBackgrounds,
  configureMonthlyWorkflow,
  deleteMonthlyBackground,
  getMonthlyWorkflow,
  monthlyBackgroundUrl,
  uploadMonthlyActivityTarget,
} from '../services/api'
import { ActionButton, Field, PageHeader } from './Primitives'
import { WorkLayout, type JobUi } from './WorkflowPanels'

function currentCycle(): string {
  const date = new Date()
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}

function fileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('無法讀取活動菇圖片'))
    reader.readAsDataURL(file)
  })
}

function shortPath(path: string | null | undefined): string {
  if (!path) return '尚未產生'
  return path.replaceAll('\\', '/').split('/').slice(-3).join('/')
}

export default function MonthlyActivityPanel({ status, jobs, effective }: {
  status: DeviceStatus
  jobs: JobUi
  effective: Record<string, unknown>
}) {
  const [cycle, setCycle] = useState(currentCycle())
  const [workflow, setWorkflow] = useState<MonthlyWorkflowStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState(false)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [backgroundPage, setBackgroundPage] = useState(0)
  const fileInput = useRef<HTMLInputElement>(null)
  const phone = (effective.phone_automation ?? {}) as Record<string, unknown>
  const generation = (effective.generation ?? {}) as Record<string, unknown>
  const collecting = jobs.active?.kind === 'monthly_background_collection'
  const training = jobs.active?.kind === 'monthly_training'
  const busy = Boolean(jobs.active)

  const refresh = async (nextCycle = cycle) => {
    setLoading(true)
    setError('')
    try {
      const state = await getMonthlyWorkflow(nextCycle)
      setWorkflow(state)
      setBackgroundPage(0)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void refresh(cycle) }, [cycle])
  useEffect(() => {
    if (!jobs.active) void refresh(cycle)
  }, [jobs.active?.id])

  const startCollection = async () => {
    setError('')
    try {
      const saved = await configureMonthlyWorkflow(cycle)
      setWorkflow(saved)
      await jobs.start('monthly_background_collection', {
        cycle,
        serial: status.serial,
        settle_seconds: Number(phone.settle_seconds ?? 4),
        swipe_duration_ms: Number(phone.swipe_duration_ms ?? 800),
      })
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
  }

  const selectTarget = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    if (!files.length) return
    setUploading(true)
    setError('')
    try {
      let state: MonthlyWorkflowStatus | null = null
      for (const file of files) {
        state = await uploadMonthlyActivityTarget(cycle, file.name, await fileAsDataUrl(file))
      }
      if (state) setWorkflow(state)
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setUploading(false) }
  }

  const removeBackground = async (path: string) => {
    setDeleting(path)
    setError('')
    try { setWorkflow(await deleteMonthlyBackground(cycle, path)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setDeleting(null) }
  }

  const approve = async () => {
    setError('')
    try { setWorkflow(await approveMonthlyBackgrounds(cycle)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
  }

  const startTraining = async () => {
    setError('')
    try {
      await jobs.start('monthly_training', {
        cycle,
        composites_per_background: Number(generation.composites_per_background ?? 6),
        parallel_workers: Number(generation.parallel_workers ?? 4),
      })
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
  }

  const ready = Boolean(workflow?.reviewed && workflow.activity_targets.length && workflow.resident_model)
  const pageSize = 48
  const backgroundPages = Math.max(1, Math.ceil((workflow?.backgrounds.length ?? 0) / pageSize))
  const visibleBackgrounds = workflow?.backgrounds.slice(backgroundPage * pageSize, (backgroundPage + 1) * pageSize) ?? []
  useEffect(() => {
    if (backgroundPage >= backgroundPages) setBackgroundPage(backgroundPages - 1)
  }, [backgroundPage, backgroundPages])
  return <div className="page-shell monthly-page">
    <PageHeader eyebrow="MONTHLY MODEL FLOW" title="每月活動菇模型" description="保留乾淨基礎背景，混入當月審核背景；先強化常駐模型，再訓練本月活動版。"/>
    <div className="monthly-steps">
      <div className={workflow?.collection_completed_at ? 'done' : 'active'}><b>1</b><span>固定路線蒐集</span></div>
      <div className={workflow?.reviewed ? 'done' : workflow?.background_count ? 'active' : ''}><b>2</b><span>人工刪除與核准</span></div>
      <div className={workflow?.activity_targets.length ? 'done' : ''}><b>3</b><span>加入活動菇</span></div>
      <div className={workflow?.completed_at ? 'done' : ready ? 'active' : ''}><b>4</b><span>兩階段訓練</span></div>
    </div>
    <WorkLayout jobs={jobs} preview={collecting} wideForm>
      <div className="form-card">
        <h3>月份與背景路線</h3>
        <Field label="活動月份"><input className="input" type="month" value={cycle} disabled={busy} onChange={(event) => setCycle(event.target.value)}/></Field>
        <div className="monthly-model-state">
          <span>固定背景蒐集路線</span>
          <b>{workflow?.background_route.name ?? '正在載入目前背景資料集城鎮'}</b>
          <small>{workflow ? `${workflow.background_route.waypoint_count} 個城鎮 · ${workflow.background_route.search_point_count.toLocaleString()} 個背景位置` : '—'}</small>
        </div>
        <div className="monthly-targets monthly-town-list">{workflow?.background_route.towns.map((town) => <span key={town}>{town.replaceAll('_', ' ')}</span>)}</div>
        <div className="notice">路線固定取自目前背景資料集的城鎮；每月重新蒐集相同區域，不需手動挑選。</div>
        <div className="form-actions wrap"><ActionButton kind="primary" icon="play" disabled={busy || !status.connected || !workflow?.background_route} onClick={() => void startCollection()}>蒐集本月背景</ActionButton><ActionButton kind="danger" icon="stop" disabled={!collecting} onClick={() => void jobs.stop()}>停止蒐集</ActionButton></div>
      </div>

      <div className="form-card">
        <h3>背景人工審核</h3>
        <div className="monthly-review-heading"><span>{workflow?.background_count ?? 0} 張背景</span><b className={workflow?.reviewed ? 'approved' : ''}>{workflow?.reviewed ? '已核准' : '等待人工檢查'}</b></div>
        <p className="form-help">逐張查看；任何包含蘑菇目標的畫面請直接刪除。檔名已標記 target 的畫面也會由程式排除。</p>
        <div className="monthly-background-grid">
          {visibleBackgrounds.map((background) => <figure key={background.path}>
            <img loading="lazy" src={monthlyBackgroundUrl(cycle, background.path, background.modified_ns)} alt={background.name}/>
            <figcaption><span title={background.path}>{background.name}</span><button disabled={busy || deleting === background.path} onClick={() => void removeBackground(background.path)}>{deleting === background.path ? '刪除中' : '刪除'}</button></figcaption>
          </figure>)}
          {!workflow?.backgrounds.length && <div className="monthly-empty">完成固定路線蒐集後，背景會出現在這裡。</div>}
        </div>
        {Boolean(workflow?.backgrounds.length) && <div className="monthly-pagination"><button disabled={backgroundPage <= 0} onClick={() => setBackgroundPage((page) => Math.max(0, page - 1))}>上一頁</button><span>{backgroundPage + 1} / {backgroundPages}</span><button disabled={backgroundPage + 1 >= backgroundPages} onClick={() => setBackgroundPage((page) => Math.min(backgroundPages - 1, page + 1))}>下一頁</button></div>}
        <ActionButton kind="primary" disabled={busy || !workflow?.background_count || Boolean(workflow?.reviewed)} onClick={() => void approve()}>背景審核完成</ActionButton>
      </div>

      <div className="form-card">
        <h3>系統常駐模型與本月活動菇</h3>
        <div className="monthly-model-state"><span>目前常駐模型</span><b>{shortPath(workflow?.resident_model)}</b><small>{workflow?.resident_class_names.length ?? 0} 個常駐類別 · {workflow?.resident_updated_at ?? '尚未建立'}</small></div>
        <p className="form-help">常駐模型由系統固定管理。第一階段訓練完成後會直接替換成加強版，舊常駐權重不保留。</p>
        <input ref={fileInput} hidden type="file" accept="image/png" multiple onChange={(event) => void selectTarget(event)}/>
        <div className="monthly-targets">
          {workflow?.activity_targets.map((target) => <span key={target.path}>{target.name}.png</span>)}
          {!workflow?.activity_targets.length && <small>尚未加入 {cycle} 活動菇素材</small>}
        </div>
        <ActionButton icon="box" disabled={busy || uploading} onClick={() => fileInput.current?.click()}>{uploading ? '正在加入…' : '加入本月活動菇透明 PNG'}</ActionButton>
      </div>

      <div className="form-card">
        <h3>自動兩階段訓練</h3>
        <ol className="monthly-training-plan"><li>以原始乾淨背景與當月核准背景、原有菇類合成資料，強化常駐模型。</li><li>以強化結果直接替換舊常駐權重。</li><li>以同一組背景混合原有菇類與本月活動菇，再訓練活動版。</li><li>活動版成為本月預設；程式自動清理合成資料、舊月份背景與已被取代的模型。</li></ol>
        {workflow?.background_policy && <div className="notice">背景組成：{workflow.baseline_background_count ?? 0} 張基礎＋{workflow.monthly_background_count ?? 0} 張當月核准背景。</div>}
        <div className="monthly-output-paths"><span>訓練暫存資料<b>{workflow?.intermediates_cleaned_at ? '已自動清理' : shortPath(workflow?.resident_dataset)}</b></span><span>活動版模型<b>{shortPath(workflow?.event_model)}</b></span></div>
        {error && <div className="range-builder-error">{error}</div>}
        {loading && <div className="notice">正在載入每月流程狀態…</div>}
        <div className="form-actions"><ActionButton kind="primary" icon="play" disabled={busy || !ready} onClick={() => void startTraining()}>開始完整訓練流程</ActionButton><ActionButton kind="danger" icon="stop" disabled={!training} onClick={() => void jobs.stop()}>保存後停止</ActionButton></div>
      </div>
    </WorkLayout>
  </div>
}
