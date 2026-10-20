import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { OperationCard } from './ui'

it('shows a specific timeout badge and the saved-content explanation', () => {
  render(<OperationCard operation={{
    id: 'timeout', goal_id: 'goal', kind: 'plan', status: 'failed', error_code: 'model_timeout',
    error_message: '模型响应超时，未能完成本次处理。已保存的内容仍然保留，可以重试。',
    created_at: '2026-10-20T15:00:00Z',
  }} />)
  expect(screen.getByText('模型响应超时', { exact: true })).toHaveClass('status-failed')
  expect(screen.getByText(/已保存的内容仍然保留/)).toBeInTheDocument()
  expect(screen.queryByText('暂时受阻')).not.toBeInTheDocument()
})

it('retains the generic failure label when an older error has no known timeout code', () => {
  render(<OperationCard operation={{
    id: 'old-failure', goal_id: 'goal', kind: 'plan', status: 'failed', error_code: 'agent_failed',
    error_message: '处理未完成，已保存原始内容，可以重试。', created_at: '2026-10-20T15:00:00Z',
  }} />)
  expect(screen.getByText('暂时受阻')).toBeInTheDocument()
  expect(screen.queryByText('模型响应超时')).not.toBeInTheDocument()
})
