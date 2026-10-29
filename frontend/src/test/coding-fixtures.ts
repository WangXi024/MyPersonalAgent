import type { CodingRun } from '../api/types'

export const codingRun: CodingRun = {
  id: 'coding-1', operation_id: 'coding-op-1', goal_id: 'goal-1', task_id: 'task-1', submission_id: 'submission-1',
  attachment_id: null, instruction: '解释错误并修改已保存的代码。', status: 'running', stage: 'agent', summary: '',
  error_message: null, cleanup_done: false, workspace_created: true,
  created_at: '2026-10-29T04:00:00Z', updated_at: '2026-10-29T04:01:00Z',
  commands: [], artifacts: [], usage: null,
}
