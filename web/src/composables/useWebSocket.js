import { ref } from 'vue'
import { useDashboardStore } from '../stores/dashboard'
import { useTasksStore } from '../stores/tasks'
import { useChatStore } from '../stores/chat'
import { useScheduleStore } from '../stores/schedule'

export const wsConnected = ref(false)
let socket = null
let reconnectTimer = null
let started = false

function wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws`
}

function onMessage(msg) {
  const dashboard = useDashboardStore()
  const tasks = useTasksStore()
  const chat = useChatStore()
  const event = msg.event
  const data = msg.data || {}
  if (event === 'task_updated') {
    tasks.fetchTasks()
    dashboard.fetchDashboard()
  }
  if (event === 'sync_status') {
    dashboard.setSyncPulse(true)
  }
  if (event === 'chat_response' && data.text) {
    chat.appendBot(data.text, data.draft)
  }
  if (event === 'new_email') {
    dashboard.fetchDashboard()
    tasks.fetchTasks()
  }
  if (event === 'schedule_updated') {
    dashboard.fetchDashboard()
    const schedule = useScheduleStore()
    if (schedule.date) {
      schedule.fetchDay(schedule.date)
    }
  }
}

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return
  }
  socket = new WebSocket(wsUrl())
  socket.onopen = () => {
    wsConnected.value = true
  }
  socket.onclose = () => {
    wsConnected.value = false
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(connect, 3000)
  }
  socket.onmessage = (ev) => {
    try {
      onMessage(JSON.parse(ev.data))
    } catch {
      /* ignore */
    }
  }
}

export function useWebSocket() {
  function start() {
    if (started) return
    started = true
    connect()
  }
  function stop() {
    started = false
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = null
    if (socket) {
      socket.close()
      socket = null
    }
    wsConnected.value = false
  }
  return { wsConnected, start, stop }
}
