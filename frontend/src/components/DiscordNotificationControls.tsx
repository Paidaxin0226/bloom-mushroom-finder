import { useEffect, useState } from 'react'
import { getDiscordBotChannels, testDiscordNotification, type DiscordChannel, type DiscordNotificationConfig } from '../services/api'
import { ActionButton, Field, Toggle } from './Primitives'

const channelLabel = (channel: DiscordChannel) => `${channel.guild_name} / ${channel.category_name ? `${channel.category_name} / ` : ''}#${channel.name}`

export function DiscordNotificationControls({ settings, onSaveSettings }: {
  settings: Record<string, unknown>
  onSaveSettings: (settings: Record<string, unknown>) => Promise<void>
}) {
  const stored = (settings.discord_webhook ?? {}) as Partial<DiscordNotificationConfig> & { enabled?: boolean; channel_label?: string }
  const storedMode = stored.mode ?? (stored.webhook_url ? 'webhook' : 'bot')
  const [enabled, setEnabled] = useState(stored.enabled === true)
  const [mode, setMode] = useState(storedMode)
  const [webhookUrl, setWebhookUrl] = useState(stored.webhook_url ?? '')
  const [botToken, setBotToken] = useState(stored.bot_token ?? '')
  const [channelId, setChannelId] = useState(stored.channel_id ?? '')
  const [savedChannelLabel, setSavedChannelLabel] = useState(stored.channel_label ?? '')
  const [channels, setChannels] = useState<DiscordChannel[]>([])
  const [botName, setBotName] = useState('')
  const [busy, setBusy] = useState<'save' | 'test' | 'connect' | null>(null)
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)

  useEffect(() => {
    setEnabled(stored.enabled === true)
    setMode(storedMode)
    setWebhookUrl(stored.webhook_url ?? '')
    setBotToken(stored.bot_token ?? '')
    setChannelId(stored.channel_id ?? '')
    setSavedChannelLabel(stored.channel_label ?? '')
    setChannels([])
    setBotName('')
  }, [stored.enabled, storedMode, stored.webhook_url, stored.bot_token, stored.channel_id, stored.channel_label])

  const credentialReady = mode === 'bot' ? Boolean(botToken.trim()) : Boolean(webhookUrl.trim())
  const selectedChannel = channels.find((channel) => channel.id === channelId)
  const label = selectedChannel ? channelLabel(selectedChannel) : savedChannelLabel

  const connect = async () => {
    const result = await getDiscordBotChannels(botToken.trim())
    setChannels(result.channels)
    setBotName(result.bot_name)
    const current = result.channels.find((channel) => channel.id === channelId)
    const selected = current ?? (result.channels.length === 1 && !result.warnings.length ? result.channels[0] : undefined)
    setChannelId(selected?.id ?? '')
    setSavedChannelLabel(selected ? channelLabel(selected) : '')
    if (result.warnings.length) {
      setMessage({ kind: 'error', text: result.warnings.join(' ') })
    } else {
      setMessage({ kind: 'success', text: selected ? `已連線 ${result.bot_name}，播報至 ${channelLabel(selected)}。` : `已連線 ${result.bot_name}，請選擇播報頻道。` })
    }
    return selected
  }

  const act = async (action: 'save' | 'test' | 'connect') => {
    setBusy(action)
    setMessage(null)
    try {
      if (action === 'connect') {
        await connect()
        return
      }
      let target = channelId
      let targetLabel = label
      if (mode === 'bot' && !target && (enabled || action === 'test')) {
        const selected = await connect()
        if (!selected) return
        target = selected.id
        targetLabel = channelLabel(selected)
      }
      const config: DiscordNotificationConfig = { mode, webhook_url: webhookUrl.trim(), bot_token: botToken.trim(), channel_id: target }
      if (action === 'save') {
        await onSaveSettings({ ...settings, discord_webhook: { ...config, enabled, channel_label: targetLabel } })
        setMessage({ kind: 'success', text: enabled ? 'Discord 命中播報已啟用，命中時會附上目標圖片。' : 'Discord 命中播報已停用。' })
      } else {
        const result = await testDiscordNotification(config)
        setMessage({ kind: 'success', text: `${result.message} 設定變更請按「儲存播報設定」。` })
      }
    } catch (error) {
      setMessage({ kind: 'error', text: error instanceof Error ? error.message : String(error) })
    } finally {
      setBusy(null)
    }
  }

  return <div className="discord-webhook-controls">
    <div className="discord-webhook-heading">
      <div><b>Discord 命中播報</b><small>目標圖片、參與人數、辨識信心與地圖連結，一則看清楚。</small></div>
      <Toggle checked={enabled} onChange={(value) => { if (!busy) { setEnabled(value); setMessage(null) } }} label={enabled ? '啟用' : '停用'}/>
    </div>
    <fieldset className="discord-notification-fields" disabled={busy !== null}>
      <Field label="播報方式">
        <select className="input" aria-label="Discord 播報方式" value={mode} onChange={(event) => { setMode(event.target.value as typeof mode); setMessage(null) }}>
          <option value="bot">Discord 機器人 · Bot Token</option>
          <option value="webhook">Discord Webhook</option>
        </select>
      </Field>
      {mode === 'bot' ? <>
        <Field label="BOT TOKEN" hint="貼上 Token 即可取得頻道；請先將機器人邀請到伺服器。Token 會儲存在本機。">
          <input className="input" type="password" aria-label="Discord Bot Token" autoComplete="off" spellCheck={false} placeholder="貼上 Discord Bot Token" value={botToken} onChange={(event) => {
            setBotToken(event.target.value)
            setChannelId('')
            setSavedChannelLabel('')
            setChannels([])
            setBotName('')
            setMessage(null)
          }}/>
        </Field>
        <div className="discord-webhook-actions">
          <ActionButton disabled={!botToken.trim() || busy !== null} onClick={() => void act('connect')}>{busy === 'connect' ? '正在取得頻道…' : '連線並取得頻道'}</ActionButton>
          {botName && <span className="discord-bot-name">已連線 · {botName}</span>}
        </div>
        {(channels.length > 0 || channelId) && <Field label="播報頻道" hint="只有一個可用頻道時自動選取；多個頻道請選擇目的地。">
          <select className="input" aria-label="Discord 播報頻道" value={channelId} onChange={(event) => { setChannelId(event.target.value); setSavedChannelLabel(''); setMessage(null) }}>
            <option value="">請選擇播報頻道</option>
            {channelId && !selectedChannel && <option value={channelId}>{savedChannelLabel || `已儲存頻道 ${channelId}`}</option>}
            {channels.map((channel) => <option value={channel.id} key={channel.id}>{channelLabel(channel)}</option>)}
          </select>
        </Field>}
        <small className="discord-notification-hint">頻道需允許：檢視頻道、傳送訊息、嵌入連結、附加檔案。</small>
      </> : <Field label="WEBHOOK URL" hint="僅接受 Discord 官方 HTTPS Webhook；網址會儲存在本機。">
        <input className="input" type="password" aria-label="Discord Webhook URL" autoComplete="off" spellCheck={false} placeholder="https://discord.com/api/webhooks/…" value={webhookUrl} onChange={(event) => { setWebhookUrl(event.target.value); setMessage(null) }}/>
      </Field>}
    </fieldset>
    <div className="discord-webhook-actions">
      <ActionButton disabled={busy !== null || (enabled && !credentialReady)} onClick={() => void act('save')}>{busy === 'save' ? '正在儲存…' : '儲存播報設定'}</ActionButton>
      <ActionButton disabled={busy !== null || !credentialReady} onClick={() => void act('test')}>{busy === 'test' ? '正在傳送…' : '傳送測試播報'}</ActionButton>
    </div>
    {message && <div role="status" className={`discord-webhook-message ${message.kind}`}>{message.text}</div>}
  </div>
}
