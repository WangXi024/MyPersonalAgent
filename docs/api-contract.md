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

## Goal edits, conversational adjustments, and general chat

- `PATCH /goals/{id}` accepts `{expected_version, title?, background?, outcome?, criteria?, deadline?}`. Only draft/active goals can be edited. A null deadline removes it; other supplied fields cannot be null. The authenticated user's explicit edit increments the goal control revision, supersedes the previous plan and unfinished background results, releases unstarted task reservations, and returns HTTP 202 for a new proposed plan. The goal remains draft until the user confirms that plan.
- A goal-scoped tutor reply can include a validated runtime `follow_up: {kind: "plan" | "recommendation", instruction, today_minutes?}`. The reply and its follow-up outbox are committed together using a stable logical key. `today_minutes` means **remaining time the user can schedule today**, never a permanent daily budget change. It can only reduce the available allocation. Plans generated this way contain `proposal.remaining_today_minutes` and `proposal.cap_date`; both generation and confirmation recheck the cap against all goals' remaining reservations. Already recorded study time is preserved.
- Valid current assessments can enqueue a next-action recommendation. Both generation and final publication obey the user's proactive setting, quiet hours, frequency and shared budget. Failed attempts can create review tasks referencing `review_of_task_id`; the source must belong to the same goal and have valid evidence for its current requirements. A goal at least seven days old can receive one confirmable weekly plan review per ISO week, using the same contact quota.
- `POST /assistant/conversation` creates or returns the authenticated user's single general conversation. The response is the ordinary Conversation shape, with `goal_id: null`. This mutation requires the usual CSRF token. It is intrinsically idempotent under the user lock.
- Existing conversation messages/read and operation cancel/retry endpoints support the general conversation. General messages do not accept task references. Global tutor replies can discuss the user's goal overview but cannot silently create goals or schedule tasks; the user uses the existing goal form when ready.
- `GET /bootstrap` includes the general conversation in `conversations`. Its operations have `goal_id: null, kind: "tutor"`; message shapes and SSE event names are unchanged. The global dialogue has a separate history from goal-scoped dialogues and from every other user.
- General conversation storage requires migration `0002_general_chat`; it makes `conversations.goal_id` nullable and adds a partial unique index enforcing at most one general conversation per user.
