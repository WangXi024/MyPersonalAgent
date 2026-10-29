import type { GenUIBlock, ResourceListItem } from '../../api/types'

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

/** Keep transport data outside React props until a bounded, known component has been validated. */
export function parseBlock(value: unknown): GenUIBlock | null {
  if (!record(value) || !text(value.id, 128) || !text(value.title, 160)
    || !Array.isArray(value.items) || value.items.length === 0 || value.items.length > MAX_ITEMS) return null
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
