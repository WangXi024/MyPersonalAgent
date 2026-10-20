import type { Accepted, Bootstrap, Conversation, ConversationModels, GoalDetail, GoalDraft, GoalInput, Memory, MessagesResponse, ModelConfig, ModelConfigInput, ModelConfigs, Operation, Settings, User } from './types'

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
  conversationModels: (id: string) => request<ConversationModels>(`/conversations/${encodeURIComponent(id)}/model`),
  selectConversationModel: (id: string, modelId: string, expectedVersion: number, key: string) => request<ConversationModels>(`/conversations/${encodeURIComponent(id)}/model`, { method: 'PATCH', body: { model_id: modelId, expected_version: expectedVersion }, key }),
  modelConfigs: () => request<ModelConfigs>('/model-configs'),
  saveModelConfig: (id: string | null, body: ModelConfigInput, key: string) => request<ModelConfig>(id ? `/model-configs/${encodeURIComponent(id)}` : '/model-configs', { method: id ? 'PUT' : 'POST', body, key }),
  setModelEnabled: (id: string, enabled: boolean, version: number, key: string) => request<ModelConfig>(`/model-configs/${encodeURIComponent(id)}/enabled`, { method: 'POST', body: { enabled, expected_version: version }, key }),
  testModelConfig: (id: string, version: number, key: string) => request<ModelConfig>(`/model-configs/${encodeURIComponent(id)}/test`, { method: 'POST', body: { expected_version: version }, key }),
  goalDrafts: (id: string) => request<{ items: GoalDraft[] }>(`/conversations/${encodeURIComponent(id)}/goal-drafts`),
  acceptGoalDraft: (id: string, body: GoalInput, key: string) => request<Accepted>(`/goal-drafts/${encodeURIComponent(id)}/accept`, { method: 'POST', body, key }),
  operation: (id: string) => request<Operation>(`/operations/${encodeURIComponent(id)}`),
  settings: () => request<Settings>('/settings'),
  updateSettings: (body: unknown, key: string) => request<Settings>('/settings', { method: 'PATCH', body, key }),
  memories: () => request<{ items: Memory[] }>('/memories'),
  updateMemory: (id: string, content: string, key: string) => request(`/memories/${id}`, { method: 'PATCH', body: { content }, key }),
  deleteMemory: (id: string, key: string) => request(`/memories/${id}`, { method: 'DELETE', key }),
}
