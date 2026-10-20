export interface User { id: string; username: string; timezone: string; daily_minutes: number; availability_version: number; memory_control_revision: number; proactive_enabled: boolean; quiet_start: number; quiet_end: number; learning_hour: number; can_manage_models?: boolean }
export type GoalStatus = 'draft' | 'active' | 'paused' | 'completed' | 'cancelled'
export type TaskStatus = 'ready' | 'in_progress' | 'awaiting_review' | 'needs_revision' | 'completed' | 'skipped' | 'cancelled'
export type OperationStatus = 'accepted' | 'running' | 'waiting_user' | 'succeeded' | 'failed' | 'superseded' | 'cancelled'
export interface Goal { id: string; title: string; background: string; outcome: string; criteria: string[]; deadline: string | null; status: GoalStatus; control_revision: number; active_plan_id: string | null; conversation_id?: string; created_at: string; unread_count?: number }
export interface Milestone { id: string; plan_id: string; title: string; criteria: string[]; status: string; position: number }
export interface Task { id: string; plan_id: string; goal_id: string; milestone_id: string | null; title: string; instructions: string; criteria: string[]; skills: string[]; estimated_minutes: number; reserved_minutes: number; scheduled_date: string | null; status: TaskStatus; state_version: number; requirements_version: number; active_submission_id: string | null; review_of_task_id: string | null }
export interface Plan { id: string; goal_id: string; version: number; status: string; summary: string; proposal: { remaining_today_minutes?: number; cap_date?: string; milestones: { title: string; criteria: string[] }[]; tasks: { title: string; instructions: string; criteria: string[]; skills: string[]; estimated_minutes: number; scheduled_date: string; milestone_index: number }[]; summary: string; message: string }; created_at: string }
export interface Evaluation { id: string; submission_id: string; passed: boolean; feedback: string; evidence_type: string; valid: boolean; details: Record<string, unknown>; created_at: string }
export interface Submission { id: string; task_id: string; text: string; created_at: string; attachment_ids?: string[] }
export interface Message { id: string; seq: number; role: string; type: string; content: string; task_id?: string | null; operation_id?: string | null; created_at: string }
export interface Operation { id: string; kind: string; status: OperationStatus; goal_id: string | null; stage?: string | null; error_code?: string | null; error_message?: string | null; created_at: string; updated_at?: string }
export interface Activity { seq: number; type: string; summary: string; goal_id?: string | null; operation_id?: string | null; created_at: string }
export interface DailyBudget { local_date: string; authorized_minutes: number; consumed_minutes: number; reserved_minutes: number }
export type Settings = User
export interface Memory { id: string; content: string; source_type: string; status: string; suppressed: boolean; deleted: boolean; created_at: string }
export interface Conversation { id: string; goal_id: string | null; last_seq: number; read_seq: number; unread_count: number }
export interface ConversationModels { items: { id: string; name: string; provider: 'bailian' | 'zhipu' | 'ark' | 'gateway' }[]; model_id: string; version: number }
export type ModelProvider = 'bailian' | 'zhipu' | 'ark'
export interface ModelConfigInput { name: string; provider: ModelProvider; model_id: string; api_base: string; api_key?: string; input_cny_per_million: string; output_cny_per_million: string; usd_cny_rate: string; expected_version: number; source_model_id?: string }
export interface ModelConfig extends Omit<ModelConfigInput, 'api_key' | 'expected_version' | 'source_model_id'> { id: string; enabled: boolean; version: number; key_configured: boolean; created_at: string; test_result: { status: 'passed' | 'failed'; message: string; at: string; estimated_cost_cny?: string; input_tokens?: number; output_tokens?: number } | null }
export interface PresetModel { id: string; name: string; provider: ModelProvider | 'gateway'; api_base: string | null; input_cny_per_million: string | null; output_cny_per_million: string | null; usd_cny_rate: string }
export interface ModelConfigs { items: ModelConfig[]; presets: PresetModel[] }
export interface Bootstrap { task_counts?: { awaiting_review: number }; user: User; cursor: number; goals: Goal[]; tasks: Task[]; operations: Operation[]; events: Activity[]; budget: DailyBudget; conversations: Conversation[] }
export interface GoalDetail { goal: Goal; plans: Plan[]; milestones: Milestone[]; tasks: Task[]; evaluations: Evaluation[]; submissions: Submission[]; skills: { id: string; name: string; status: string; evaluation_id: string | null; review_at: string | null }[]; operations: Operation[]; conversation: Conversation }
export interface Accepted { operation_id: string; status: string; resource_id?: string }
export interface MessagesResponse { items: Message[]; read_seq: number; last_seq: number; has_more: boolean }
export interface GoalInput { title: string; background: string; outcome: string; criteria: string[]; deadline: string | null }
export interface GoalDraft { operation_id: string; message_id: string; status: 'pending' | 'accepted' | 'superseded'; draft: GoalInput; goal_id: string | null; plan_operation_id: string | null }
