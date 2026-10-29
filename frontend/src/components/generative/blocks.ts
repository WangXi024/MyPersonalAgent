import type { GenUIBlock, ProgressSnapshot, QuizAttempt, QuizQuestion, ResourceListItem } from '../../api/types'

const MAX_BLOCKS = 6
const MAX_ITEMS = 10

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function text(value: unknown, max: number, allowEmpty = false): value is string {
  // Pydantic counts Unicode code points; JS string.length counts UTF-16 units.
  return typeof value === 'string' && value.length <= max * 2 && Array.from(value).length <= max
    && (allowEmpty || value.trim().length > 0)
}

/** Only explicit web links are actionable. A block can never name an API or component to execute. */
export function resourceUrl(value: unknown): URL | null {
  if (!text(value, 2048) || Array.from(value).some(char => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127)) return null
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !!url.hostname && !url.username && !url.password ? url : null
  } catch { return null }
}

function resource(value: unknown): value is ResourceListItem {
  return record(value) && text(value.id, 128) && text(value.title, 200) && !!resourceUrl(value.url)
    && text(value.snippet, 2000, true) && text(value.retrieved_at, 100) && Number.isFinite(Date.parse(value.retrieved_at))
}

function integer(value: unknown, max = 1_000_000): value is number { return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= max }
function question(value: unknown): value is QuizQuestion {
  return record(value) && text(value.id, 64) && text(value.prompt, 1000) && Array.isArray(value.choices)
    && value.choices.length >= 2 && value.choices.length <= 4 && value.choices.every(choice => record(choice)
      && typeof choice.id === 'string' && /^[a-d]$/.test(choice.id) && text(choice.text, 500))
    && new Set(value.choices.map(choice => choice.id)).size === value.choices.length
}

/** Feedback must describe these exact questions and choices, with internally consistent counts. */
export function validQuizAttempt(value: unknown, questions: QuizQuestion[], quizId: string): value is QuizAttempt {
  if (!record(value) || !text(value.id, 128) || value.quiz_id !== quizId || value.practice_only !== true
    || !integer(value.attempt_number, 5) || value.attempt_number < 1 || !text(value.submitted_at, 100) || !Number.isFinite(Date.parse(value.submitted_at))
    || value.total !== questions.length || !integer(value.correct_count, questions.length)
    || !Array.isArray(value.answers) || value.answers.length !== questions.length
    || !Array.isArray(value.feedback) || value.feedback.length !== questions.length) return false
  const known = new Map(questions.map(item => [item.id, item]))
  const selected = new Map<string, string>()
  for (const answer of value.answers) {
    if (!record(answer) || typeof answer.question_id !== 'string' || typeof answer.choice_id !== 'string'
      || selected.has(answer.question_id) || !known.get(answer.question_id)?.choices.some(choice => choice.id === answer.choice_id)) return false
    selected.set(answer.question_id, answer.choice_id)
  }
  const feedbackIds = new Set<string>()
  let correct = 0
  for (const item of value.feedback) {
    if (!record(item) || typeof item.question_id !== 'string' || feedbackIds.has(item.question_id)
      || selected.get(item.question_id) !== item.selected_choice_id
      || !known.get(item.question_id)?.choices.some(choice => choice.id === item.correct_choice_id)
      || typeof item.correct !== 'boolean' || item.correct !== (item.selected_choice_id === item.correct_choice_id)
      || !text(item.explanation, 2000, true)) return false
    feedbackIds.add(item.question_id)
    if (item.correct) correct++
  }
  return correct === value.correct_count
}

function progress(value: unknown): value is ProgressSnapshot {
  if (!record(value) || !text(value.goal_id, 128) || !text(value.title, 200)
    || !['draft', 'active', 'paused', 'completed', 'cancelled'].includes(value.goal_status as string)
    || !integer(value.goal_revision) || value.goal_revision < 1 || !(value.plan_id === null || text(value.plan_id, 128))
    || !text(value.as_of, 100) || !Number.isFinite(Date.parse(value.as_of)) || !record(value.tasks) || !record(value.skills) || !record(value.evidence)) return false
  const tasks = value.tasks
  const states = ['completed', 'in_progress', 'awaiting_review', 'needs_revision', 'ready', 'skipped'] as const
  return integer(tasks.total) && states.every(key => integer(tasks[key]))
    && states.reduce((sum, key) => sum + (tasks[key] as number), 0) === tasks.total
    && integer(value.skills.total) && integer(value.skills.needs_review) && value.skills.needs_review <= value.skills.total
    && integer(value.evidence.ai_review) && value.evidence.verified_tests === 0
}

/** Keep transport data outside React props until a bounded, known component has been validated. */
export function parseBlock(value: unknown): GenUIBlock | null {
  if (!record(value) || !text(value.id, 128) || !text(value.title, 160)) return null
  if (value.type === 'quiz' && text(value.operation_id, 128) && text(value.quiz_id, 64) && value.practice_only === true
    && integer(value.attempt_count, 5) && value.max_attempts === 5 && Array.isArray(value.questions)
    && value.questions.length >= 1 && value.questions.length <= 5 && value.questions.every(question)
    && new Set(value.questions.map(item => item.id)).size === value.questions.length
    && (value.attempt === null ? value.attempt_count === 0 : validQuizAttempt(value.attempt, value.questions, value.quiz_id)
      && value.attempt.attempt_number === value.attempt_count)) {
    return { type: 'quiz', id: value.id, title: value.title, operation_id: value.operation_id, quiz_id: value.quiz_id,
      practice_only: true, questions: value.questions, attempt: value.attempt as QuizAttempt | null, attempt_count: value.attempt_count, max_attempts: 5 }
  }
  if (value.type === 'progress' && progress(value.snapshot)) return { type: 'progress', id: value.id, title: value.title, snapshot: value.snapshot }
  if (!Array.isArray(value.items) || value.items.length === 0 || value.items.length > MAX_ITEMS) return null
  if (value.type === 'resource_list' && value.items.every(resource)
    && new Set(value.items.map(item => item.id)).size === value.items.length) {
    return { type: 'resource_list', id: value.id, title: value.title, items: value.items }
  }
  if (value.type === 'checklist' && value.items.every(item => text(item, 500))) {
    return { type: 'checklist', id: value.id, title: value.title, items: value.items }
  }
  return null
}

export function parseBlocks(value: unknown): (GenUIBlock | null)[] {
  if (value === undefined || value === null) return []
  if (!Array.isArray(value) || value.length > MAX_BLOCKS) return [null]
  const ids = new Set<string>()
  return value.map(item => {
    const block = parseBlock(item)
    if (!block || ids.has(block.id)) return null
    ids.add(block.id)
    return block
  })
}
