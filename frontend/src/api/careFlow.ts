import client from './client'

export interface CareFlowStartRequest {
  elder_id: string
  care_plan_id?: string
  initial_stage?: string
  discharge_order?: Record<string, unknown>  // SaaS 退住工单（入住时已知要出院可注入）
}

export interface CareFlowStartResponse {
  session_id: string
  current_stage: string
  ai_message: string
  structured_output: unknown
}

export interface CareFlowReport {
  session_id: string
  elder_id: string
  care_plan_id?: string
  stage_reached?: string
  total_turns?: number
  status?: string
  report: Record<string, unknown>
  created_at?: string
}

export interface CareFlowActiveSession {
  session_id: string
  elder_id: string
  staff_id: string
  current_stage: string
  total_turns: number
  started_at?: string
  updated_at?: string
}

export interface CareFlowActiveResponse {
  active: boolean
  session: CareFlowActiveSession | null
}

export const careFlowApi = {
  start: (data: CareFlowStartRequest) =>
    client.post<CareFlowStartResponse>('/care-flow/start', data),

  getActiveSession: (elderId: string) =>
    client.get<CareFlowActiveResponse>(`/care-flow/active/${elderId}`),

  getReport: (sessionId: string) =>
    client.get<CareFlowReport>(`/care-flow/${sessionId}/report`),
}

// ── 照护计划（护理等级 → 服务项 → 当天执行）──────────────────

/** 服务项：来自机构业务配置（care 不改它，只读并据此生成任务清单） */
export interface ServiceItem {
  item_id?: string
  project_name: string
  frequency_name?: string
  times_per_day?: number
  nurse_time_slots?: Array<{ startTime?: string; endTime?: string }>
  is_required?: boolean
  care_level?: string
  source?: string
}

/** 当天计划条目：status 由护工执行后回写 */
export interface DailyPlanItem {
  plan_item_id: string
  project_name: string
  nurse_time?: string
  carer_name?: string
  actual_care_time?: string
  status: 'planned' | 'done' | 'missed'
}

export interface PlanProgress {
  total: number
  done: number
  missed: number
  pending: number
}

/** 计划来源：决定"任务清单是谁定的"，界面上要如实展示 */
export type PlanSource = 'service_items' | 'ai_assessment' | 'merged' | 'pipeline' | 'missing'

export interface PlanOverview {
  session_id: string
  elder_id: string
  plan_missing: boolean
  plan_source: PlanSource
  care_level: string
  special_notes: string
  service_items: ServiceItem[]
  today_plan: DailyPlanItem[]
  progress: PlanProgress
  message: string
}

export const carePlanApi = {
  /** 计划总览：护理等级 + 服务项 + 当天计划与进度（不跑 LLM，很快） */
  getOverview: (sessionId: string) =>
    client.get<PlanOverview>(`/care-flow/sessions/${sessionId}/plan-overview`),

  /** 护工回写执行结果：done=完成 / missed=该做没做 */
  confirmItem: (
    planItemId: string,
    data: { status: 'done' | 'missed'; carer_name?: string; actual_care_time?: string },
  ) => client.post<{ plan_item_id: string; status: string; confirmed: boolean }>(
    `/care-flow/plan-items/${planItemId}/confirm`, data,
  ),
}

// ── 出站通知（Agent → SaaS）：回调配置 + 事件审计 ─────────────

export interface OutboundWebhookConfig {
  url: string
  secret: string          // 后端只回显打码前缀
  enabled: boolean
  updated_at?: string | null
}

export interface OutboundEvent {
  event_id: string
  tenant_id: string
  event_type: string
  aggregate_id: string
  payload: Record<string, unknown>
  status: 'pending' | 'sent' | 'failed'
  attempts: number
  last_error: string
  next_retry_at?: string | null
  created_at?: string | null
  sent_at?: string | null
}

export interface OutboundEventList {
  total: number
  events: OutboundEvent[]
}

export const outboundApi = {
  /** 读取本租户回调配置（密钥打码） */
  getWebhook: () =>
    client.get<{ configured: boolean; webhook: OutboundWebhookConfig | null }>(
      '/care-flow/webhooks',
    ),

  /** 保存回调配置：secret 省略 = 不改动现有密钥 */
  saveWebhook: (data: { url: string; secret?: string; enabled?: boolean }) =>
    client.put<{ configured: boolean; url: string; enabled: boolean }>(
      '/care-flow/webhooks',
      data,
    ),

  /** 出站事件列表（发件箱审计） */
  listEvents: (limit = 50) =>
    client.get<OutboundEventList>('/care-flow/events', { params: { limit } }),

  /** 把失败 / 已发事件重置为待投递，交给投递器重发 */
  redeliver: (eventId: string) =>
    client.post<{ event_id: string; status: string; message: string }>(
      `/care-flow/events/${eventId}/redeliver`,
    ),
}
