import type { VoiceCapabilities, VoiceConfig, VoiceSession } from '../voice/types'

export const voiceConfig: VoiceConfig = { version: 1, provider: null, model_id: null, voice: null, endpoint: null, api_version: null, enabled: false, key_configured: false, updated_at: null }
export const voiceCapabilities: VoiceCapabilities = { status: 'unconfigured', reason: '尚未选择语音供应商。', can_manage: true, can_start: false, config: voiceConfig,
  providers: [{ id: 'openai', name: 'OpenAI', dependency_ready: true }, { id: 'azure', name: 'Azure', dependency_ready: true }],
  limits: { max_session_seconds: 300, daily_seconds: 900, idle_seconds: 60, max_concurrent: 1 }, budget: { available_seconds: 900, reserved_seconds: 0, consumed_seconds: 0 }, latest_session: null }
export const voiceSession: VoiceSession = { id: 'voice-1', conversation_id: 'conversation-1', goal_id: null, status: 'pending', config: { version: 1, provider: 'openai', model_id: 'test-realtime-model', voice: null }, max_seconds: 300, duration_seconds: 0, remaining_seconds: 300,
  created_at: '2026-10-29T01:00:00Z', updated_at: '2026-10-29T01:00:00Z', started_at: null, closed_at: null, stop_requested: false, cleanup_done: false, reason: null, error_message: null,
  usage: { requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, usage_complete: false }, transcripts: [] }
