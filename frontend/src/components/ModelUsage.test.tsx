import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { ModelUsageData } from '../api/types'
import { ModelUsage } from './ModelUsage'

const usage: ModelUsageData = { model_name: '测试模型', model_id: 'test-model', provider: 'bailian', requests: 2,
  input_tokens: 1500, output_tokens: 300, total_tokens: 1800, usage_complete: true,
  estimated_cost_cny: '0.028800', cost_complete: true, pricing_known: true }

describe('model usage', () => {
  it('shows saved model, input/output tokens and a small CNY estimate without rounding to zero', () => {
    render(<ModelUsage usage={usage} />)
    expect(screen.getByText('百炼 测试模型')).toBeVisible()
    expect(screen.getByText('输入 1,500 / 输出 300 Token')).toBeVisible()
    expect(screen.getByText('2 次调用')).toBeVisible()
    expect(screen.getByText('估算 ¥0.028800')).toBeVisible()
  })
  it('does not present missing historical rates or timeout usage as free', async () => {
    render(<ModelUsage usage={{ ...usage, usage_complete: false, cost_complete: false, pricing_known: false, estimated_cost_cny: null }} />)
    expect(screen.getByText('费用未知')).toBeVisible()
    expect(screen.getByText('统计不完整')).toBeVisible()
    expect(screen.queryByText(/¥0/)).not.toBeInTheDocument()
    await userEvent.click(screen.getByText('费用未知'))
    expect(screen.getByText('缺少请求当时的费率，无法计算完整费用。')).toBeVisible()
  })
  it('labels a partial aggregate estimate and explains the conversation scope', async () => {
    render(<ModelUsage usage={{ ...usage, usage_complete: false, cost_complete: false }} aggregate />)
    expect(screen.getByText('1,800 Token')).toBeVisible()
    expect(screen.getByText('已知部分估算 ¥0.028800')).toBeVisible()
    await userEvent.click(screen.getByText('本对话用量'))
    expect(screen.getByText('统计本对话回复，包含失败尝试与重试；规划、评估在各自操作卡中显示。')).toBeVisible()
  })
})
