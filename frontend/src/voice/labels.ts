const reasons: Record<string, string> = {
  ready: '语音配置已就绪，点击开始后才连接供应商。',
  feature_disabled: '部署尚未启用实时语音。可以先保存配置并测试本机麦克风。',
  configuration_disabled: '实时语音尚未启用。',
  provider_not_configured: '尚未完成语音供应商、模型和密钥配置。',
  provider_unsupported: '当前部署暂不支持此语音供应商。',
  dependency_unavailable: '所选供应商的语音依赖尚未就绪。',
  credentials_unavailable: '服务端语音密钥暂不可用，请联系部署管理员。',
  conversation_unavailable: '当前对话暂不能开始语音。',
  voice_busy: '另一个语音会话正在运行或等待结束，请先结束该会话。',
  voice_budget_exceeded: '今天的可用语音额度不足以开始新会话。',
}
export function voiceReason(value: string) { return reasons[value] ?? (/[^\u0020-\u007e]/.test(value) ? value : '语音暂不可用，请检查配置后重试。') }
