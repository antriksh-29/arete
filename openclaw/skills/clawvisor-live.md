---
name: clawvisor-live
version: 1.0.0
description: |
  Live Gmail + Calendar via clawvisor MCP tools. Read this when answering questions about today's calendar, finding recent emails, or pulling fresh data ClawVisor exposes.
---

# clawvisor-live — live Gmail + Calendar

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

