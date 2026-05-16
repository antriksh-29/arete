---
name: google-composio
version: 1
script: infra/composio_client.py
mutating: true
cron: null
eval: skills/google-composio/eval.yaml
last_audited: 2026-05-16
description: |
  Live Google Calendar + Gmail through Composio's MCP gateway. Replaces
  the prior ClawVisor wrapper (retired 2026-05-16) — same use cases,
  one consumer-key endpoint, supports both READ and WRITE.
  WHEN: "what's on my calendar today/tomorrow", "am I free Friday",
  "find the email from X last week", "did Y reply", "show me the thread
  with Z", "draft an email to A", "create an event tomorrow at 3pm".
  NOT WHEN: brain already knows the answer (gbrain__query first per
  rule 9); historical research best served by the 7am brain collectors'
  pre-enriched pages.
trigger: |
  - "what's on my calendar today/tomorrow"
  - "am I free <day> <time>"
  - "find/search email from <person>"
  - "did <person> reply to X"
  - "show me the thread with <person>"
  - "pull the full email body"
  - "draft / send an email to <addr>"
  - "create a calendar event for <when> with <person>"
writes_to: []
brain_dirs_read: [people, companies, meetings]
mutates_external_state: true   # Composio can SEND emails + WRITE events
---

# /google-composio — live Gmail + Calendar via Composio MCP

Routes through the `composio` MCP server (configured in
`mcp.servers.composio` in openclaw.json) at `https://connect.composio.dev/mcp`.
Auth is one `x-consumer-api-key` header sourced from the `COMPOSIO_API_KEY`
vault slot.

## Accounts

Two Google accounts are connected; pass `account` to scope a call.

| Toolkit | Alias / ID | Email |
|---|---|---|
| gmail | `work` (default) | antriksh@getarete.co |
| gmail | `personal` | antriksh1980@gmail.com |
| googlecalendar | `personal` (default) | antriksh@getarete.co |
| googlecalendar | `googlecalendar_casse-wikeno` (no alias) | antriksh1980@gmail.com |

Omit `account` for the default. Pass alias OR connection ID for a specific
account. The googlecalendar "personal" alias is mislabeled (it's the WORK
email) — the personal-email calendar has no alias so we use its stable ID.

## Core tools

| Tool | Use when |
|---|---|
| `GOOGLECALENDAR_EVENTS_LIST` | "What's on my calendar", "am I free", "next meeting" |
| `GOOGLECALENDAR_EVENTS_GET` | Need full attendee list + description + Meet link for one event |
| `GOOGLECALENDAR_CREATE_EVENT` | "Schedule X with Y at <time>" — write path |
| `GOOGLECALENDAR_PATCH_EVENT` | "Move the X call to <time>" — write path |
| `GMAIL_FETCH_EMAILS` | "Find email from X", "search inbox for Y" — uses Gmail search syntax |
| `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID` | Snippet wasn't enough — fetch full body |
| `GMAIL_SEND_EMAIL` | "Email X about Y" — write path |
| `GMAIL_CREATE_EMAIL_DRAFT` | Draft for review before sending |

## Script-side usage (Python)

```python
import composio_client
events = composio_client.calendar_list_events(
    account="personal",
    time_min="2026-05-17T00:00:00+05:30",
    time_max="2026-05-17T23:59:59+05:30",
    time_zone="Asia/Kolkata",
)
msgs = composio_client.gmail_search(query="is:unread newer_than:1d",
                                    account="work", max_results=20)
```

The wrappers normalize Composio's response shape (which wraps
`{response: {data: ...}}` around payloads) and return flat dicts.

## Script-side usage (Node)

```js
import { calendarList, gmailSearch } from './composio_client.mjs';
const { data: events } = await calendarList({
    account: 'personal', from, to, maxResults: 25 });
const { data: { messages } } = await gmailSearch({
    account: 'work', query: 'newer_than:1d', maxResults: 50 });
```

Used by `tools/collectors/{email,calendar}-collector.mjs` (7am IST
brain-enrichment job).

**Brain-first still applies (rule 9).** Query gbrain first for anything
the brain may already know; use Composio only when fresh / live data
is required, or when the request is to MUTATE state (send / create).
