import client from './client'

export interface AlertTriggerRequest {
  elder_id: string
  device_id: string
  alert_type: string
  alert_timestamp?: string
}

export interface AlertTriggerResponse {
  alert_id: string
  session_id: string
  status: string
  review_data: unknown
}

export interface TrackItem {
  [key: string]: unknown
}

export interface AlertReview {
  alert_id: string
  elder_id: string
  device_id: string
  alert_type: string
  status: string
  risk_level?: string
  risk_assessment: Record<string, unknown>
  device_readings: TrackItem[]
  care_records: TrackItem[]
  health_indicators: TrackItem[]
  created_at?: string
  message: string
}

export interface AlertConfirmRequest {
  action: 'approve' | 'modify' | 'dismiss'
  modified_actions: Array<Record<string, unknown>>
  supervisor_id: string
  comment: string
}

export interface AlertConfirmResponse {
  alert_id: string
  status: string
  dispatch_order: Record<string, unknown> | null
  structured_output: Record<string, unknown> | null
}

export const alertApi = {
  trigger: (data: AlertTriggerRequest) =>
    client.post<AlertTriggerResponse>('/alert/trigger', data, { timeout: 300_000 }),

  getReview: (alertId: string) =>
    client.get<AlertReview>(`/alert/${alertId}/review`),

  confirm: (alertId: string, data: AlertConfirmRequest) =>
    client.post<AlertConfirmResponse>(`/alert/${alertId}/confirm`, data),
}
