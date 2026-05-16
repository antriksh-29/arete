---
name: clawvisor-live
version: 1
script: null
mutating: false
cron: null
eval: skills/clawvisor-live/eval.yaml
last_audited: 2026-05-16
description: |
  Live Gmail + Calendar via the clawvisor MCP tools. Use when answering
  questions that require real-time inbox state, calendar events, or any data
  ClawVisor exposes through its MCP surface. Brain-first still applies — if
  gbrain can answer, query gbrain first.
  WHEN: "what's on my calendar today/tomorrow/this week", "am I free
  Friday afternoon", "find the email from <person> last week",
  "did anyone reply to <thread>", "pull <person>'s last email",
  "show full thread with <person>".
  NOT WHEN: brain already knows the answer (read brain page first per
  rule 9); historical research that can be served from the daily
  brain-collectors snapshot.
trigger: |
  - "what's on my calendar today/tomorrow"
  - "am I free <day> <time>"
  - "find/search email from <person>"
  - "did <person> reply to X"
  - "show me the thread with <person>"
  - "pull the full email body"
writes_to: []
brain_dirs_read: [people, companies, meetings]
mutates_external_state: false
---

# /clawvisor-live — live Gmail + Calendar via MCP

For real-time calendar and email queries, use the typed clawvisor MCP tools.
Do NOT shell out to curl ClawVisor directly — the MCP wrapper handles auth,
session_id, retries, and intent-coherence reasons for you.

| Tool | When to use |
|---|---|
| `clawvisor__calendar_list_events` | "What is on my calendar today/tomorrow/this-week", "Am I free Friday afternoon" |
| `clawvisor__calendar_get_event` | Need full attendee list / description / link for one specific event |
| `clawvisor__gmail_search` | "Find the email from Kaushik last week", "Did anyone reply to X", recent inbox state. Uses Gmail search syntax. |
| `clawvisor__gmail_get_message` | Snippet from search wasn't enough — need the full body |
| `clawvisor__gmail_get_thread` | Following the back-and-forth across multiple replies |

Account defaults to BOTH (`antriksh@getarete.co` + `antriksh1980@gmail.com`).
Pass `account: "all"` or omit to get both. Pass a specific email to scope.

**Brain-first still applies (rule 9).** If the brain already knows something
("what was the last meeting with Kaushik about?"), query gbrain first.
Use clawvisor MCP only when fresh / live data is required.
