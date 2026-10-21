import type { ModelUsageData } from '../api/types'
import './model-usage.css'

function costLabel(usage: ModelUsageData) {
  if (usage.estimated_cost_cny === null) return '费用未知'
  return `${usage.cost_complete ? '估算' : '已知部分估算'} ¥${Number(usage.estimated_cost_cny).toFixed(6)}`
}

export function ModelUsage({ usage, aggregate = false }: { usage?: ModelUsageData | null; aggregate?: boolean }) {
  if (!usage || (!aggregate && usage.requests === 0)) return null
  const provider = ({ bailian: '百炼', zhipu: '智谱', ark: '火山方舟', gateway: '' })[usage.provider ?? 'gateway']
  return <details className={`model-usage${aggregate ? ' conversation-usage' : ''}`} aria-label={aggregate ? '本对话累计用量' : '模型用量'}>
    <summary>
      <span>{aggregate ? '本对话用量' : [provider, usage.model_name ?? usage.model_id ?? '历史模型未记录'].filter(Boolean).join(' ')}</span>
      <span>{aggregate ? `${usage.total_tokens.toLocaleString('zh-CN')} Token` : `输入 ${usage.input_tokens.toLocaleString('zh-CN')} / 输出 ${usage.output_tokens.toLocaleString('zh-CN')} Token`}</span>
      <span>{usage.requests} 次调用</span><span>{costLabel(usage)}</span>
      {!usage.usage_complete && <span>统计不完整</span>}
    </summary>
    <div className="model-usage-detail">
      <p>已记录输入 {usage.input_tokens.toLocaleString('zh-CN')}、输出 {usage.output_tokens.toLocaleString('zh-CN')} Token，合计 {usage.total_tokens.toLocaleString('zh-CN')} Token。</p>
      <p>{aggregate ? '统计本对话回复，包含失败尝试与重试；规划、评估在各自操作卡中显示。' : '调用次数包含失败尝试、工具续接和输出纠正。'}</p>
      {!usage.usage_complete && <p>部分请求仍在处理、未返回用量，或历史记录缺少完整性信息；已记录 Token 可能少于实际消耗。</p>}
      {!usage.pricing_known && usage.requests > 0 && <p>缺少请求当时的费率，无法计算完整费用。</p>}
      <p>费用按请求保存的费率估算，实际费用以供应商账单为准。</p>
    </div>
  </details>
}
