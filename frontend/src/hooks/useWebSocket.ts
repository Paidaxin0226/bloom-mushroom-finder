import { useCallback, useEffect, useRef, useState } from 'react'
import { websocketUrl } from '../services/api'
import type { ServerEvent } from '../types'

type Subscriber = (event: ServerEvent) => void

export function useWebSocket() {
  const [connected, setConnected] = useState(false)
  const subscribers = useRef(new Set<Subscriber>())
  const reconnectTimer = useRef<number | null>(null)

  const subscribe = useCallback((subscriber: Subscriber) => {
    subscribers.current.add(subscriber)
    return () => subscribers.current.delete(subscriber)
  }, [])

  useEffect(() => {
    let disposed = false
    let socket: WebSocket | null = null
    let retries = 0

    const connect = () => {
      if (disposed) return
      socket = new WebSocket(websocketUrl())
      socket.onopen = () => {
        retries = 0
        setConnected(true)
      }
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as ServerEvent
          subscribers.current.forEach((subscriber) => subscriber(event))
        } catch { /* ignore malformed event */ }
      }
      socket.onclose = () => {
        setConnected(false)
        if (!disposed) {
          retries += 1
          reconnectTimer.current = window.setTimeout(connect, Math.min(500 + retries * 500, 5000))
        }
      }
      socket.onerror = () => socket?.close()
    }

    connect()
    return () => {
      disposed = true
      if (reconnectTimer.current !== null) window.clearTimeout(reconnectTimer.current)
      if (socket?.readyState === WebSocket.CONNECTING) {
        socket.onmessage = null
        socket.onerror = null
        socket.onclose = null
        socket.onopen = () => socket?.close()
      } else {
        socket?.close()
      }
    }
  }, [])

  return { connected, subscribe }
}
