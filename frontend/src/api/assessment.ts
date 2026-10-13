import client from './client'

export interface DimensionScore {
  key: string
  dimension: string
  score: number
  weight: number
  issues: string[]
  suggestions?: string[]
}

export interface AssessmentIssue {
  priority: 'high' | 'medium' | 'low'
  dimension: string
  description: string
  location?: string
  suggestion?: string
}

export interface AssessmentResponse {
  assessment_id: string
  status: string
  weighted_score: number
  summary: Record<string, unknown>
  dimension_scores: DimensionScore[]
  issues: AssessmentIssue[]
}

export const assessmentApi = {
  upload: (elderId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return client.post<AssessmentResponse>('/assessment/upload', form, {
      params: { elder_id: elderId },
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300_000,  // 五维度评审为多轮 LLM 调用
    })
  },

  get: (assessmentId: string) =>
    client.get<AssessmentResponse>(`/assessment/${assessmentId}`),
}
