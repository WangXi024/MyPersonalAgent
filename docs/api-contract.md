# Implementation API contract

Base `/api/v1`; all IDs are UUID strings, timestamps UTC ISO 8601. Dates YYYY-MM-DD.
Cookie `learning_session` is HttpOnly. `GET /auth/me` and login return `{user,csrf_token}`; store token in memory and send `X-CSRF-Token` on authenticated mutations. POST/PATCH/DELETE also require same-origin `Origin` (browsers send it automatically). Async commands require `Idempotency-Key` (UUID). Other mutations accept it; the client should provide one per action, retaining it on retry.

Error: `{code,message,request_id}`. Async response 202: `{operation_id,status,resource_id}`. Empty controls return current resource or operation; refetch detail/bootstrap after mutations.

User: `{id,username,timezone,daily_minutes,availability_version,memory_control_revision,proactive_enabled,quiet_start,quiet_end,learning_hour}`. Quiet/learning hours are integers 0..23.
Goal: `{id,user_id,title,background,outcome,criteria:string[],deadline:string|null,status,control_revision,active_plan_id,created_at,conversation_id?,unread_count?}`.
Task: `{id,goal_id,plan_id,milestone_id,title,instructions,criteria:string[],skills:string[],requirements_version,state_version,active_submission_id,estimated_minutes,reserved_minutes,scheduled_date,status,review_of_task_id,created_at}`.
Plan: `{id,goal_id,version,status,summary,proposal,availability_version,base_plan_id,accepted_at,created_at}`. Proposal: `{milestones:[{title,criteria}],tasks:[{title,instructions,criteria,skills,estimated_minutes,scheduled_date,milestone_index}],summary,message}`.
Operation: `{id,goal_id,kind,status,stage,result,error_code,error_message,created_at,updated_at}` (never internal agent history).
Event: `{id,seq,type,entity_id,entity_version,goal_id,operation_id,summary,created_at}`.
Message: `{id,conversation_id,operation_id,seq,role,type,content,task_id,proactive,control_revision,created_at}`.
Memory: `{id,source_id,source_type,version,content,status,suppressed,deleted,created_at}`.

`GET /bootstrap` -> `{user,goals:Goal[],tasks:Task[],operations:Operation[],events:Event[],conversations:[{id,goal_id,last_seq,read_seq,unread_count}],budget:{local_date,authorized_minutes,consumed_minutes,reserved_minutes,version},cursor:number}`.
`GET /goals` -> `{items:Goal[]}`.
`GET /goals/{id}` -> `{goal:Goal,plans:Plan[],milestones:[{id,goal_id,plan_id,title,criteria,position,status}],tasks:Task[],skills:[{id,name,status,evaluation_id,review_at}],conversation:{id,goal_id,last_seq,read_seq,unread_count},operations:Operation[],submissions:[...],evaluations:[...]}`.
`POST /goals` body `{title,background,outcome,criteria?:string[],deadline?:string}` -> async response resource_id=goal id, automatic plan operation.
`POST /goals/{id}/plan-proposals` or `/recommendations` body `{instruction?:string}` -> async response.
`POST /plans/{id}/accept` or `/reject` body `{expected_version:number}` -> plan.
`POST /goals/{id}/pause|resume|cancel|complete` body `{expected_version:control_revision}` -> goal.
`POST /tasks/{id}/start|skip` body `{expected_version:state_version}` -> task.
`POST /tasks/{id}/defer` body `{expected_version:state_version,scheduled_date:string}` -> task.
`POST /tasks/{id}/study-records` body `{attempt_id:string,minutes:number,estimated?:boolean}` -> record.
`POST /tasks/{id}/submissions` body `{text:string,attachment_ids?:string[],expected_version:state_version,attempt_id?:string,minutes?:number,estimated?:boolean}` -> async response resource_id=submission id.
`GET /submissions/{id}/evaluation` -> `{submission,evaluations:[{id,submission_id,passed,feedback,evidence_type,rubric_version,evaluator_version,valid,details,created_at}]}`.
`GET /conversations/{id}/messages?after=0&limit=100` -> `{items:Message[],last_seq,read_seq,has_more}` chronological.
`POST /conversations/{id}/messages` body `{content:string,task_id?:string}` -> async response.
`POST /conversations/{id}/read` body `{seq:number}` -> `{read_seq,unread_count}`.
`GET /operations/{id}` -> Operation; `POST /operations/{id}/cancel` -> Operation; `POST /operations/{id}/retry` -> async response (new idempotency key).
`GET /settings` -> User; `PATCH /settings` body `{timezone?,daily_minutes?,proactive_enabled?,quiet_start?,quiet_end?,learning_hour?,expected_version:availability_version}` -> User.
`GET /memories` -> `{items:Memory[]}`; `PATCH /memories/{id}` body `{content:string}` -> Memory; `DELETE /memories/{id}` -> `{status:'suppressed'}`.
`POST /attachments` multipart `file` -> `{id,filename,mime,size,sha256,status}`; authenticated download `/attachments/{id}/download`.
`GET /events?cursor=N` SSE uses event name `business`, `id: seq`, `data: Event`; reset event name `reset_required`, heartbeat comment. Last-Event-ID overrides query cursor. All event types listed in Event.type.

`POST /memories/{id}/retry` retries a failed sync using a new Idempotency-Key. Deleted sources only appear as redacted tombstones if external cleanup failed; they remain excluded from recall.

Runtime boundary `load_context` contains bounded context plus 14 days of shared budgets. Each budget has `available_minutes` and `replaceable_minutes` (the current goal's unstarted reservations; usable when replacing a plan, not when appending a recommendation). Memories are source metadata only; external recalled content must pass current source/version validation. Attachments include extracted text or an explicit unavailable warning.

## Model usage

Operation responses additionally include `usage`; assistant messages include the same field for their operation (user messages return null). Shape: `{model_name,model_id,provider,requests,input_tokens,output_tokens,total_tokens,usage_complete,estimated_cost_cny,cost_complete,pricing_known}`. Missing historical model metadata is null or the saved gateway alias; it is never replaced with the conversation's current selection. CNY amounts are decimal strings to six places, or null when unknown. Raw pricing/attempt metadata and credentials are not exposed.

`GET /conversations/{id}/usage` returns the numeric/boolean/cost fields above aggregated over all tutor operations whose saved conversation ID matches, including failures, automatic and manual retries. It checks ownership, counts each operation once, and is independent of message pagination or the recent bootstrap window. Plans, assessments, Hindsight and model connection tests are outside this conversation-reply total; planning/assessment usage is shown on their operation cards.

Migration `0005_model_usage` adds nullable `operations.usage_details`. New model operations snapshot the selected model display metadata and configured CNY input/output rates when accepted. Each provider attempt reserves its existing request counter; token reports are recorded once per attempt number. Attempts without a returned token report remain incomplete, even if a later retry succeeds. Historical rows remain null, retain existing token counts, and show unknown historical rates/completeness. No historical fees are backfilled from today's rates. Estimates use recorded input/output totals and saved rates, without provider-specific cache/tier adjustments; they are not invoices.

`operation.usage_updated` SSE events refresh counters and the conversation total as attempts start or report usage. The UI labels partial totals and unknown costs explicitly; zero unreported tokens cannot be presented as a free failed request.

## Goal edits, conversational adjustments, and general chat

- `PATCH /goals/{id}` accepts `{expected_version, title?, background?, outcome?, criteria?, deadline?}`. Only draft/active goals can be edited. A null deadline removes it; other supplied fields cannot be null. The authenticated user's explicit edit increments the goal control revision, supersedes the previous plan and unfinished background results, releases unstarted task reservations, and returns HTTP 202 for a new proposed plan. The goal remains draft until the user confirms that plan.
- A goal-scoped tutor reply can include a validated runtime `follow_up: {kind: "plan" | "recommendation", instruction, today_minutes?}`. The reply and its follow-up outbox are committed together using a stable logical key. `today_minutes` means **remaining time the user can schedule today**, never a permanent daily budget change. It can only reduce the available allocation. Plans generated this way contain `proposal.remaining_today_minutes` and `proposal.cap_date`; both generation and confirmation recheck the cap against all goals' remaining reservations. Already recorded study time is preserved.
- Valid current assessments can enqueue a next-action recommendation. Both generation and final publication obey the user's proactive setting, quiet hours, frequency and shared budget. Failed attempts can create review tasks referencing `review_of_task_id`; the source must belong to the same goal and have valid evidence for its current requirements. A goal at least seven days old can receive one confirmable weekly plan review per ISO week, using the same contact quota.
- `POST /assistant/conversation` creates or returns the authenticated user's single general conversation. The response is the ordinary Conversation shape, with `goal_id: null`. This mutation requires the usual CSRF token. It is intrinsically idempotent under the user lock.
- Existing conversation messages/read and operation cancel/retry endpoints support the general conversation. General messages do not accept task references. Global tutor replies can discuss the user's goal overview and prepare an editable goal draft. A goal is created only after the user explicitly confirms its card or submits the existing goal form.
- `GET /bootstrap` includes the general conversation in `conversations`. Its operations have `goal_id: null, kind: "tutor"`; message shapes and SSE event names are unchanged. The global dialogue has a separate history from goal-scoped dialogues and from every other user.
- General conversation storage requires migration `0002_general_chat`; it makes `conversations.goal_id` nullable and adds a partial unique index enforcing at most one general conversation per user.

### Existing-goal commands from chat

Tutor output may contain up to three `actions`, with at most one action per goal. An action has `kind` (`retry_plan`, `replan`, `update_goal`, or `cancel_goal`), `goal_id`, `expected_revision`, and `request_quote` copied from the triggering user message. `retry_plan` requires the failed planning `operation_id`; `update_goal` requires concrete `changes` limited to `background`, `outcome`, `criteria`, and `deadline`. An optional `instruction` passes the user's scheduling choice to the planner. Actions cannot be combined with `goal_draft` or `follow_up`. New goals and proposed plans still require their existing explicit confirmation.

Under the user transaction lock, the backend checks ownership, goal scope, revision, quoted request, and allowed state before executing each command. The same goal-edit/cancel rules apply as in the UI. Failed checks produce a blocked receipt rather than a claimed success. A repeated retry reuses the current planning operation when one is already underway. Stable per-source keys and terminal-result checks prevent duplicate commits after activity retries.

New tutor contexts include `chat_request: {content, bound_goal_id, targets}`; each target carries its current `status` and `control_revision`. Before committing, an output validator asks the model to correct actions quoting another message, targeting an ended/out-of-scope goal, or using a stale revision. This uses the existing output retry/request budget. Exhausted action corrections fail with `invalid_chat_action` and publish no misleading assistant reply. Transaction checks still guard state changes after the context snapshot. Old recorded contexts without `chat_request` retain their replay path.

For historical tutor replies where every action was blocked, `/operations/{id}/retry` also accepts a `succeeded` source. It retries the saved user message manually, excludes that turn's previous assistant reply, and records `retry_of_operation_id`. Repeated requests reuse an accepted/running/succeeded retry; mixed applied/blocked replies cannot be retried this way. The frontend offers “重新处理这条消息” without automatically sending a model request.

Assistant messages now include `actions: [{kind, status: "applied" | "blocked", goal_id, goal_title, operation_id, message, changes?}]`, loaded from their saved operation result. Receipts link to the actual plan operation; SSE refreshes its status. The accepted goal card uses current saved goal fields and the latest plan operation, and offers a direct retry when planning failed.

Global context includes the latest ten unfinished goals with their saved requirements, revisions, plan/task counts, recent plan summaries and latest planning operation. A global `plan: null` is not evidence that those goals lack plans. Tutor history contains only bounded public turns from its own conversation, excluding future user turns and previous internal snapshots. Planning receives the latest saved goal fields plus the source chat request and recent public context. No schema migration is required.

## Personal conversation goal drafts

A global tutor may return `goal_draft: {title, background, outcome, criteria, deadline}` after gathering enough information. The object rejects unknown fields; title is trimmed and 1..200 characters, background/outcome are trimmed and 1..8000 characters, criteria contains 1..20 trimmed strings of 1..500 characters each, and deadline is `YYYY-MM-DD` or null. Goal-scoped operations and a reply combining `goal_draft` with `follow_up` cannot publish a goal draft.

The draft is saved in the completed tutor operation's result JSON. Its operation remains `succeeded` with `goal_id: null`; drafting does not create a Goal, plan operation, task, or change user settings. Publishing a new draft supersedes earlier pending drafts in that conversation while preserving accepted drafts and historical messages.

`GET /conversations/{id}/goal-drafts` returns:

```json
{
  "items": [
    {
      "operation_id": "source-tutor-operation-id",
      "message_id": "assistant-message-id",
      "status": "pending",
      "draft": {
        "title": "学习 Python",
        "background": "零基础，每天有半小时",
        "outcome": "制作文件整理工具",
        "criteria": ["独立完成文件分类并解释错误处理"],
        "deadline": null
      },
      "goal_id": null,
      "goal_status": null,
      "goal_control_revision": null,
      "plan_operation_id": null
    }
  ]
}
```

The endpoint checks conversation ownership and returns the most recent 30 draft cards in message order. Status is `pending`, `accepted`, or `superseded`; goal conversations return an empty array. Accepted cards return the user's confirmed, edited draft and the created goal/plan-operation identifiers, plus the linked goal's current `goal_status` and `goal_control_revision` (null if unavailable). Historical acceptance does not mean the goal remains active: cancelled cards and action receipts are marked as history, and a new learning intent may produce a new pending draft without restoring the old goal.

`POST /goal-drafts/{operation_id}/accept` accepts the full edited draft object with the same validation above, the usual CSRF protection, and an `Idempotency-Key`. It returns HTTP 202 `{operation_id, status, resource_id}` where `operation_id` is the new plan operation and `resource_id` is the Goal ID. Confirmation atomically creates the Goal in `draft` state, its conversation, the plan operation and its outbox entry. Generated plans still require the existing plan confirmation step before activation.

The original tutor operation remains global and completed. Same-content confirmation retries with either the same or a fresh key return the original acceptance response and cannot create a second goal. Each request key is recorded as a completed control operation; it cannot be reused for another command. The plan operation uses a stable key derived from the draft operation. A different confirmation payload after acceptance returns HTTP 409 `goal_draft_conflict`; an obsolete draft returns HTTP 409 `goal_draft_superseded`; invalid fields return 422; another user's draft returns 404.

SSE business events `goal_draft.proposed`, `goal_draft.superseded`, and `goal_draft.accepted` prompt the frontend to refresh cards. Ordinary goal, operation and message events continue unchanged. `load_context.goal_drafts` supplies the global Agent with the latest 10 draft status summaries (`operation_id`, `status`, `title`, `goal_id`, `goal_status`, `goal_control_revision`, `plan_operation_id`) so historical acceptance is distinguished from current goal state. No database migration is needed for drafts.

## Explicit Coder runs

`POST /submissions/{id}/coding-runs` requires the usual authentication, CSRF and `Idempotency-Key`. Body: `expected_version` (task), `expected_goal_revision`, `attachment_id` (null for the saved body), and `instruction` (1–4000 characters). It returns HTTP 202 with the persisted run. Only the current saved submission in the active plan is accepted; Python source must be UTF-8 and at most 1 MiB. Model selection is frozen from the goal conversation when the run is created.

| Endpoint | Result |
| --- | --- |
| `GET /submissions/{id}/coding-runs` | Owned submission's latest runs |
| `GET /coding-runs/{id}` | Status, command receipts, generated files and usage |
| `POST /coding-runs/{id}/cancel` | Idempotent stop request; does not claim cleanup until confirmed |
| `GET /coding-runs/{id}/artifacts/{artifact_id}` | Owned generated file as an attachment |

Goal detail includes `coding_runs`; `coding.run_updated` refreshes the persistent snapshot. `workspace_created=false` means creation has not been confirmed, including a lost Open response. `cleanup_done` is the authoritative cleanup acknowledgement. Command `exit_code=null` stays unknown; after confirmed cleanup an unfinished command is `interrupted`, never inferred successful from stdout. Closing/retrying never creates a replacement workspace or runs the model again.

Defaults: 5 runs per rolling 24 hours, one in-flight run per user (including unconfirmed cleanup), 30 minutes per workspace and 120 seconds per command. Generated files are capped at 8 files, 8 MiB each and 16 MiB total. No network or credentials enter the workspace. Generated files do not replace the submission, create an evaluation, or advance mastery. Node restart invalidates the workspace; application Worker retries can reattach while the same node remains alive. Disabled service returns 503, stale versions 409, limits 429, and another user's resources 404.

## Realtime voice

Voice uses an explicitly configured native realtime provider. It does not inherit the chat gateway/model/key. Saving a disabled draft with `provider=null` performs no provider call. Administrators use `GET/PUT /voice/config`; PUT requires CSRF, an idempotency key and `expected_version`. Optional `api_key` is write-only and encrypted locally. Provider/endpoint changes clear the old credential. Public config never includes keys, ciphertext, nonces or credential references.

| Endpoint | Purpose |
| --- | --- |
| `GET /voice/capabilities?conversation_id=...` | Provider/dependency readiness, allowed start/reason, limits, remaining time and latest owned session |
| `POST /conversations/{id}/voice-sessions` | Explicit reservation bound to config version, conversation, user and login session |
| `POST /voice-sessions/{id}/connect-ticket` | 30-second single-use ticket; stored as a hash |
| `GET /voice-sessions/{id}` | Saved status, final/interrupted transcripts and usage |
| `POST /voice-sessions/{id}/stop` | Idempotent stop request; confirmed closure uses `cleanup_done` |

Session creation accepts `expected_config_version` and an optional `expected_goal_revision` for goal conversations. Normal HTTP authentication, Origin and CSRF rules apply. WebSocket `/voice-sessions/{id}/socket` checks Cookie and exact Origin, then requires first JSON `{"type":"authenticate","ticket":"..."}`. It atomically consumes the ticket before constructing the provider. Tickets/keys never go in URLs.

`session.ready` supplies actual input/output sample rates, PCM16LE mono format and interruption support. Binary frames carry PCM; `audio.chunk` announces `response_id`, monotonic `sequence` and byte count before its paired binary frame. Transcript events use stable `index` and `speaker`. Client `playback.ack`/`interrupt` reports response-specific actual `played_bytes`; network delivery is only an upper bound. Client `stop` closes the session. No automatic media reconnection occurs; refresh restores saved text/status.

Defaults are 300 seconds per session, 900 per local day, one concurrent session per user and 60 seconds without activity. Reservations are separate from learning minutes and chat monthly spending. A crashed owner is settled conservatively; unknown cleanup retains the slot until the absolute session deadline. Cookie revocation, account/goal/memory/config changes stop the connection. These limits are not a guaranteed monetary cap.

Raw audio is not persisted or sent to Temporal; final/interrupted transcripts and usage are saved. `voice.session_updated` refreshes snapshots. Realtime has no business action tools: users edit a transcript, append it to the ordinary message composer and explicitly send through the existing validated learning workflow.

## Teaching and component interactions

New `learning_*_v3` Agents use the `learning-teaching-ui-v1` Temporal patch, keeping V1/V2 names and output schemas registered. The fixed `teaching-v1` pack exposes three deferred instruction capabilities (retrieval practice, debugging and evidence-based feedback); its version and SHA-256 are saved with the result. It loads no host files or executable skills. Official sliding-window compaction retains the pinned current request and business snapshot, with explicit truncation of selected tool results to 12,000 characters and no overflow workspace or summary model. Request limits remain six model calls and 80,000 tokens per operation.

`search_conversation_history(query, run_id?)` uses official scoped Harness search plus bounded literal Chinese matching. Scope comes from the operation, never a model-supplied user/goal: only earlier public user/assistant turns in the same owned conversation, strictly before the saved message sequence and operation creation time. Up to 200 turns / 64,000 source characters are examined, each turn capped at 4,000 characters; eight distinct tool calls and 12,000 result characters per operation. Raw model history, private answer keys, memory and future turns are excluded. Scope revocation stops retrieval.

V3 retains `resource_list` and `checklist`, adding:

| Component | Trusted publication contract |
| --- | --- |
| `quiz` | 1–5 questions, 2–4 unique choices per question. Initial public blocks have no answer/explanation; `operation_id`, `quiz_id`, `practice_only=true`, `attempt_count`, `max_attempts=5` and latest `attempt` support refresh. |
| `progress` | Model supplies only type/title. Server fills the bound goal/current-plan snapshot, task counts, current applicable skill/review counts and evidence counts; `as_of` identifies the saved snapshot. No model-supplied percentage or mastery claim. |

`POST /operations/{operation_id}/quizzes/{quiz_id}/attempts` requires login, CSRF and `Idempotency-Key`. Body is `{answers: [{question_id: "q1", choice_id: "a"}]}` with exactly one valid choice for every question. It returns the persisted attempt (`attempt_number`, answers, correct count, feedback and `practice_only=true`). Repeating the same key/content returns the same attempt, conflicting content returns 409, a sixth distinct attempt returns 429, and another user's operation returns 404. Per-user transaction locking protects parallel retries. `quiz.attempted` refreshes the owned operation; existing public message pagination continues unchanged. Practice never modifies Task, Evaluation, study-time budgets or mastery. Private answer records, request keys and history audit hashes are excluded from public operation responses.

`GET /goals/{goal_id}/report.md` downloads an owned UTF-8 Markdown attachment, including ended goals. It reads actual bounded database task/submission/evaluation/receipt records without a model; labels current versus historical evidence and preserves `ai_review`/unverified execution distinctions. No chat, voice transcripts, memory, credentials, model traces, attachments or program logs are exported. Limits: 512 KiB overall, 40 current/20 historical task details and 40 recent submissions/evaluations. User/model text is escaped or indented as literal text. Response uses an ASCII UUID filename, `private, no-store` and `nosniff`; unauthenticated requests return 401 and foreign/malformed IDs return 404. No schema migration is added.
