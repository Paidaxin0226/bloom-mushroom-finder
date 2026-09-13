import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import type { DeviceStatus, MonthlyWorkflowStatus } from '../types'
import {
  approveMonthlyBackgrounds,
  configureMonthlyWorkflow,
  deleteMonthlyBackgrounds,
  getMonthlyWorkflow,
  uploadMonthlyActivityTarget,
} from '../services/api'
import { ActionButton, Field, PageHeader } from './Primitives'
import { WorkLayout, type JobUi } from './WorkflowPanels'
import MonthlyBackgroundReview from './MonthlyBackgroundReview'

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

export default function MonthlyActivityPanel({ status, jobs, effective }: {
  status: DeviceStatus
  jobs: JobUi
  effective: Record<string, unknown>
}) {
  const [cycle, setCycle] = useState(currentCycle())
  const [workflow, setWorkflow] = useState<MonthlyWorkflowStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [acting, setActing] = useState(false)
  const requestVersion = useRef(0)
  const lastJobId = useRef(jobs.active?.id)
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState(false)
  const [deleting, setDeleting] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const phone = (effective.phone_automation ?? {}) as Record<string, unknown>
  const generation = (effective.generation ?? {}) as Record<string, unknown>
  const collecting = jobs.active?.kind === 'monthly_background_collection'
  const training = jobs.active?.kind === 'monthly_training'
  const busy = Boolean(jobs.active)
  const controlsBusy = busy || loading || uploading || deleting !== null || acting

  const refresh = async (nextCycle = cycle, silent = false) => {
    const version = ++requestVersion.current
    if (!silent) { setLoading(true); setError('') }
    try {
      const state = await getMonthlyWorkflow(nextCycle)
      if (version !== requestVersion.current) return
      setWorkflow(state)
    } catch (reason) {
      if (version === requestVersion.current) setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      if (version === requestVersion.current) setLoading(false)
    }
  }

  useEffect(() => {
    setWorkflow(null)
    void refresh(cycle)
    return () => { requestVersion.current += 1 }
  }, [cycle])
  useEffect(() => {
    if (lastJobId.current && !jobs.active) void refresh(cycle)
    lastJobId.current = jobs.active?.id
  }, [jobs.active?.id])
  useEffect(() => {
    if (!collecting) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      await refresh(cycle, true)
      if (!cancelled) timer = setTimeout(() => void poll(), 5000)
    }
    timer = setTimeout(() => void poll(), 5000)
    return () => { cancelled = true; clearTimeout(timer); requestVersion.current += 1 }
  }, [cycle, collecting])

  const startCollection = async () => {
    if (controlsBusy) return
    setActing(true)
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
    finally { setActing(false) }
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
        setWorkflow(state)
      }
      if (state) setWorkflow(state)
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setUploading(false) }
  }

  const removeBackgrounds = async (paths: string[]) => {
    if (controlsBusy) return false
    setDeleting('batch')
    setError('')
    requestVersion.current += 1
    try { setWorkflow(await deleteMonthlyBackgrounds(cycle, paths)); return true }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); return false }
    finally { setDeleting(null) }
  }

  const approve = async () => {
    if (controlsBusy) return
    setActing(true)
    setError('')
    try { setWorkflow(await approveMonthlyBackgrounds(cycle)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setActing(false) }
  }

  const startTraining = async () => {
    if (controlsBusy) return
    setActing(true)
    setError('')
    try {
      await jobs.start('monthly_training', {
        cycle,
        composites_per_background: Number(generation.composites_per_background ?? 6),
        parallel_workers: Number(generation.parallel_workers ?? 4),
      })
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setActing(false) }
  }

  const ready = Boolean(workflow?.reviewed && workflow.activity_targets.length && workflow.resident_model)
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
        <Field label="活動月份"><input className="input" type="month" value={cycle} disabled={busy || uploading || deleting !== null || acting} onChange={(event) => setCycle(event.target.value)}/></Field>
        <div className="monthly-model-state">
          <span>固定背景蒐集路線</span>
          <b>{workflow?.background_route.name ?? '正在載入目前背景資料集城鎮'}</b>
          <small>{workflow ? `${workflow.background_route.waypoint_count} 個城鎮 · ${workflow.background_route.search_point_count.toLocaleString()} 個背景位置` : '—'}</small>
        </div>
        <div className="monthly-targets monthly-town-list">{workflow?.background_route.towns.map((town) => <span key={town}>{town.replaceAll('_', ' ')}</span>)}</div>
        <div className="notice">依目前背景資料集的城鎮，從中心逐圈拍滿縮小後的矩形範圍；每個方向最多 5 格，不需手動挑選。</div>
        <div className="form-actions wrap"><ActionButton kind="primary" icon="play" disabled={controlsBusy || !status.connected || !workflow?.background_route} onClick={() => void startCollection()}>蒐集本月背景</ActionButton><ActionButton kind="danger" icon="stop" disabled={!collecting} onClick={() => void jobs.stop()}>停止蒐集</ActionButton></div>
      </div>

      <div className="form-card">
        <h3>背景人工審核</h3>
        <div className="monthly-review-heading"><span>{workflow?.background_count ?? 0} 張背景</span><b className={workflow?.reviewed ? 'approved' : ''}>{workflow?.reviewed ? '已核准' : '等待人工檢查'}</b></div>
        <p className="form-help">點選縮圖可放大查看；含蘑菇的畫面請勾選後刪除。蒐集中每 5 秒更新，可先瀏覽，停止後再刪除與核准。</p>
        <MonthlyBackgroundReview key={cycle} cycle={cycle} backgrounds={workflow?.backgrounds ?? []} busy={controlsBusy} collecting={collecting} onDelete={removeBackgrounds} onRefresh={() => void refresh(cycle)} />
        {error && <div className="range-builder-error" role="alert">{error}</div>}
        <ActionButton kind="primary" disabled={controlsBusy || !workflow?.background_count || Boolean(workflow?.reviewed)} onClick={() => void approve()}>背景審核完成</ActionButton>
      </div>

      <div className="form-card">
        <h3>系統常駐模型與本月活動菇</h3>
        <div className="monthly-model-state"><span>目前常駐模型</span><b>{workflow?.resident_model ? `${workflow.resident_cycle ?? '月份未記錄'}｜不含活動菇` : '尚未建立'}</b><small>{workflow?.resident_class_names.length ?? 0} 個常駐類別</small></div>
        <p className="form-help">常駐模型由系統固定管理。第一階段訓練完成後會直接替換成加強版，舊常駐權重不保留。</p>
        <input ref={fileInput} hidden type="file" accept="image/png" multiple onChange={(event) => void selectTarget(event)}/>
        <div className="monthly-targets">
          {workflow?.activity_targets.map((target) => <span key={target.path}>{target.name}.png</span>)}
          {!workflow?.activity_targets.length && <small>尚未加入 {cycle} 活動菇素材</small>}
        </div>
        <ActionButton icon="box" disabled={controlsBusy || uploading} onClick={() => fileInput.current?.click()}>{uploading ? '正在加入…' : '加入本月活動菇透明 PNG'}</ActionButton>
      </div>

      <div className="form-card">
        <h3>自動兩階段訓練</h3>
        <ol className="monthly-training-plan"><li>以原始乾淨背景與當月核准背景、原有菇類合成資料，強化常駐模型。</li><li>以強化結果直接替換舊常駐權重。</li><li>以同一組背景混合原有菇類與本月活動菇，再訓練活動版。</li><li>活動版成為本月預設；程式自動清理合成資料、舊月份背景與已被取代的模型。</li></ol>
        {workflow?.background_policy && <div className="notice">背景組成：{workflow.baseline_background_count ?? 0} 張基礎＋{workflow.monthly_background_count ?? 0} 張當月核准背景。</div>}
        <div className="monthly-output-paths"><span>訓練暫存資料<b>{workflow?.intermediates_cleaned_at ? '已自動清理' : workflow?.resident_dataset ? '已準備' : '尚未產生'}</b></span><span>活動版模型<b>{workflow?.event_model ? `${cycle}｜含活動菇` : '尚未產生'}</b></span></div>
        {error && <div className="range-builder-error">{error}</div>}
        {loading && <div className="notice">正在載入每月流程狀態…</div>}
        <div className="form-actions"><ActionButton kind="primary" icon="play" disabled={controlsBusy || !ready} onClick={() => void startTraining()}>開始完整訓練流程</ActionButton><ActionButton kind="danger" icon="stop" disabled={!training} onClick={() => void jobs.stop()}>保存後停止</ActionButton></div>
      </div>
    </WorkLayout>
  </div>
}
