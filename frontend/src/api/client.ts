import type { Accepted, Bootstrap, CodingRun, CodingRunInput, Conversation, ConversationModels, GoalDetail, GoalDraft, GoalInput, Memory, MessagesResponse, ModelConfig, ModelConfigInput, ModelConfigs, ModelUsageData, Operation, QuizAnswer, QuizAttempt, SandboxExecution, SandboxExecutionInput, Settings, User } from './types'
import type { VoiceCapabilities, VoiceConfig, VoiceConfigInput, VoiceSession, VoiceTicket } from '../voice/types'

let csrfToken = ''
export class ApiError extends Error {
  constructor(public status: number, message: string, public code = '', public requestId = '') { super(message); this.name = 'ApiError' }
}
export function setCsrfToken(token: string) { csrfToken = token }
export function newRequestKey() { return crypto.randomUUID() }
export async function request<T>(path: string, options: { method?: string; body?: unknown; key?: string; signal?: AbortSignal } = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  const multipart = options.body instanceof FormData
  if (options.body !== undefined && !multipart) headers['Content-Type'] = 'application/json'
  if (method !== 'GET') {
    headers['Idempotency-Key'] = options.key ?? newRequestKey()
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken
  }
  let response: Response
  try { response = await fetch(`/api/v1${path}`, { method, headers, body: options.body === undefined ? undefined : multipart ? options.body as FormData : JSON.stringify(options.body), credentials: 'same-origin', signal: options.signal }) }
  catch (error) { if (error instanceof DOMException && error.name === 'AbortError') throw error; throw new ApiError(0, '暂时无法连接服务。你的输入仍然保留，请稍后重试。', 'network_error') }
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('session-expired'))
    const detail = payload?.error ?? payload?.detail ?? payload
    throw new ApiError(response.status, typeof detail?.message === 'string' ? detail.message : response.status === 401 ? '登录已过期，请重新登录。' : response.status === 409 ? '内容已有更新，请刷新后再操作。' : response.status === 422 ? '请检查填写的内容。' : '请求未完成，请稍后重试。', detail?.code, detail?.request_id ?? payload?.request_id)
  }
  if (payload?.csrf_token) setCsrfToken(payload.csrf_token)
  return payload as T
}
export const api = {
  submitQuizAttempt: (operationId: string, quizId: string, answers: QuizAnswer[], key: string) => request<QuizAttempt>(`/operations/${encodeURIComponent(operationId)}/quizzes/${encodeURIComponent(quizId)}/attempts`, { method: 'POST', body: { answers }, key }),
  learningReport: async (goalId: string, signal?: AbortSignal) => {
    let response: Response
    try { response = await fetch(`/api/v1/goals/${encodeURIComponent(goalId)}/report.md`, { credentials: 'same-origin', headers: { Accept: 'text/markdown' }, signal }) }
    catch (error) { if (error instanceof DOMException && error.name === 'AbortError') throw error; throw new ApiError(0, '报告暂时无法下载，请稍后重试。') }
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      throw new ApiError(response.status, payload?.message || payload?.detail?.message || (response.status === 401 ? '登录已过期，请重新登录。' : response.status === 404 ? '这份学习报告不可用。' : '报告暂时无法下载，请稍后重试。'))
    }
    if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'text/markdown') throw new ApiError(0, '报告响应格式不正确，请稍后重试。')
    const filename = /^attachment;\s*filename="(learning-report-[a-zA-Z0-9_-]+\.md)"$/.exec(response.headers.get('Content-Disposition') ?? '')?.[1] ?? 'learning-report.md'
    return { blob: await response.blob(), filename }
  },
  login: (username: string, password: string) => request<{ user: User; csrf_token: string }>('/auth/login', { method: 'POST', body: { username, password } }),
  me: () => request<{ user: User; csrf_token: string }>('/auth/me'),
  assistantConversation: (key: string) => request<Conversation>('/assistant/conversation', { method: 'POST', body: {}, key }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  bootstrap: () => request<Bootstrap>('/bootstrap'),
  goal: (id: string) => request<GoalDetail>(`/goals/${encodeURIComponent(id)}`),
  updateGoal: (id: string, body: unknown, key: string) => request<Accepted>(`/goals/${id}`, { method: 'PATCH', body, key }),
  createGoal: (body: unknown, key: string) => request<Accepted>('/goals', { method: 'POST', body, key }),
  command: (path: string, body: unknown = {}, key?: string) => request<Accepted>(path, { method: 'POST', body, key }),
  upload: (file: File, key: string) => { const body = new FormData(); body.append('file', file); return request<{ id: string; filename: string }>('/attachments', { method: 'POST', body, key }) },
  messages: (id: string, after = 0) => request<MessagesResponse>(`/conversations/${encodeURIComponent(id)}/messages?after=${after}&limit=100`),
  conversationUsage: (id: string) => request<ModelUsageData>(`/conversations/${encodeURIComponent(id)}/usage`),
  conversationModels: (id: string) => request<ConversationModels>(`/conversations/${encodeURIComponent(id)}/model`),
  selectConversationModel: (id: string, modelId: string, expectedVersion: number, key: string) => request<ConversationModels>(`/conversations/${encodeURIComponent(id)}/model`, { method: 'PATCH', body: { model_id: modelId, expected_version: expectedVersion }, key }),
  modelConfigs: () => request<ModelConfigs>('/model-configs'),
  saveModelConfig: (id: string | null, body: ModelConfigInput, key: string) => request<ModelConfig>(id ? `/model-configs/${encodeURIComponent(id)}` : '/model-configs', { method: id ? 'PUT' : 'POST', body, key }),
  setModelEnabled: (id: string, enabled: boolean, version: number, key: string) => request<ModelConfig>(`/model-configs/${encodeURIComponent(id)}/enabled`, { method: 'POST', body: { enabled, expected_version: version }, key }),
  testModelConfig: (id: string, version: number, key: string) => request<ModelConfig>(`/model-configs/${encodeURIComponent(id)}/test`, { method: 'POST', body: { expected_version: version }, key }),
  goalDrafts: (id: string) => request<{ items: GoalDraft[] }>(`/conversations/${encodeURIComponent(id)}/goal-drafts`),
  acceptGoalDraft: (id: string, body: GoalInput, key: string) => request<Accepted>(`/goal-drafts/${encodeURIComponent(id)}/accept`, { method: 'POST', body, key }),
  operation: (id: string) => request<Operation>(`/operations/${encodeURIComponent(id)}`),
  runSubmission: (id: string, body: SandboxExecutionInput, key: string) => request<SandboxExecution>(`/submissions/${encodeURIComponent(id)}/executions`, { method: 'POST', body, key }),
  submissionExecutions: (id: string) => request<{ items: SandboxExecution[] }>(`/submissions/${encodeURIComponent(id)}/executions`),
  sandboxExecution: (id: string) => request<SandboxExecution>(`/sandbox-executions/${encodeURIComponent(id)}`),
  cancelSandboxExecution: (id: string, key: string) => request<SandboxExecution>(`/sandbox-executions/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {}, key }),
  createCodingRun: (id: string, body: CodingRunInput, key: string) => request<CodingRun>(`/submissions/${encodeURIComponent(id)}/coding-runs`, { method: 'POST', body, key }),
  codingRun: (id: string) => request<CodingRun>(`/coding-runs/${encodeURIComponent(id)}`),
  cancelCodingRun: (id: string, key: string) => request<CodingRun>(`/coding-runs/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {}, key }),
  voiceCapabilities: (id?: string) => request<VoiceCapabilities>(`/voice/capabilities${id ? `?conversation_id=${encodeURIComponent(id)}` : ''}`),
  voiceConfig: () => request<VoiceConfig>('/voice/config'),
  saveVoiceConfig: (body: VoiceConfigInput, key: string) => request<VoiceConfig>('/voice/config', { method: 'PUT', body, key }),
  createVoiceSession: (id: string, version: number, revision: number | null, key: string) => request<VoiceSession>(`/conversations/${encodeURIComponent(id)}/voice-sessions`, { method: 'POST', body: { expected_config_version: version, expected_goal_revision: revision }, key }),
  voiceTicket: (id: string, key: string) => request<VoiceTicket>(`/voice-sessions/${encodeURIComponent(id)}/connect-ticket`, { method: 'POST', body: {}, key }),
  voiceSession: (id: string) => request<VoiceSession>(`/voice-sessions/${encodeURIComponent(id)}`),
  stopVoiceSession: (id: string, key: string) => request<VoiceSession>(`/voice-sessions/${encodeURIComponent(id)}/stop`, { method: 'POST', body: {}, key }),
  settings: () => request<Settings>('/settings'),
  updateSettings: (body: unknown, key: string) => request<Settings>('/settings', { method: 'PATCH', body, key }),
  memories: () => request<{ items: Memory[] }>('/memories'),
  updateMemory: (id: string, content: string, key: string) => request(`/memories/${id}`, { method: 'PATCH', body: { content }, key }),
  deleteMemory: (id: string, key: string) => request(`/memories/${id}`, { method: 'DELETE', key }),
}
