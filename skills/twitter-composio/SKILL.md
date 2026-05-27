---
name: twitter-composio
version: 1
script: null
mutating: true
cron: null
eval: skills/twitter-composio/eval.yaml
last_audited: 2026-05-18
description: |
  Twitter / X actions on the user's own account via the Composio MCP
  Twitter toolkit. Connected as toolkit `twitter`, account
  `twitter_not-tenne` (default). Supports read (recent search, post
  lookup, your own timeline, your mentions), write (post tweet,
  delete tweet, upload media), and the standard Twitter API v2
  surface that Composio wraps.
  WHEN: "show my last N tweets", "post a tweet: <text>", "tweet
  with image", "delete tweet <id>", "search twitter for X", "my
  mentions", "who replied to my tweet about Y", "look up tweet <url>".
  NOT WHEN: ingesting someone else's thread URL into the brain
  (→ ingest-x-thread for that — uses stored cookies, captures full
  thread + quoted tweets + notable replies). NOT WHEN drafting a
  tweet for review without posting (→ x-draft — uses voice corpus).
trigger: |
  - "show my last <N> tweets" / "what did I tweet recently"
  - "post a tweet: <text>" / "tweet this: <text>"
  - "tweet with image <path>: <text>"
  - "delete tweet <id>"
  - "search twitter for <query>" / "find tweets about <topic>"
  - "my mentions today" / "who mentioned me"
  - "look up tweet <url-or-id>"
  - "how many tweets about <topic> last week"
writes_to: []
brain_dirs_read: []
mutates_external_state: true   # posts, deletes, likes are public+irreversible
---

# /twitter-composio — Twitter / X actions via Composio MCP

For YOUR own X account actions, this is the path — NOT the cookie-based
ingest-x-thread (that's only for capturing someone else's thread into
the brain). Composio has the `twitter` toolkit connected at account
`twitter_not-tenne` (default). The agent reaches it through the
standard Composio MCP pattern: `COMPOSIO_SEARCH_TOOLS` to find the
right `TWITTER_*` slug, then `COMPOSIO_MULTI_EXECUTE_TOOL` to invoke.

## Known tool slugs (audited 2026-05-18)

| Slug | What it does | Required args |
|---|---|---|
| `TWITTER_RECENT_SEARCH` | Search last 7 days of tweets (Twitter v2 syntax) | `query` |
| `TWITTER_FULL_ARCHIVE_SEARCH` | Search the full archive (academic / paid tier) | `query` |
| `TWITTER_CREATION_OF_A_POST` | Post a new tweet (text + optional media_ids) | `text` |
| `TWITTER_POST_LOOKUP_BY_POST_ID` | Fetch one tweet by ID | `id` |
| `TWITTER_POST_LOOKUP_BY_POST_IDS` | Fetch multiple tweets by IDs (≤100) | `ids` |
| `TWITTER_POST_DELETE_BY_POST_ID` | Delete one of YOUR tweets | `id` |
| `TWITTER_UPLOAD_MEDIA` | Upload image/video, returns media_id | `media_data` (base64) or `media_url` |
| `TWITTER_SEARCH_RECENT_COUNTS` | Count tweets matching a query (no text content) | `query` |

For "my own timeline / my mentions / who am I", the tool slugs aren't
all known offline — discover them at runtime via:

```
COMPOSIO_SEARCH_TOOLS arguments={"query": "my user timeline tweets me lookup"}
COMPOSIO_SEARCH_TOOLS arguments={"query": "twitter mentions of me notifications"}
```

Composio returns 5-10 candidate slugs with descriptions. Pick the
matching one, fetch its schema with `COMPOSIO_GET_TOOL_SCHEMAS`, then
invoke.

## Two-step pattern (always)

```
1. COMPOSIO_MULTI_EXECUTE_TOOL with:
   tools = [
     { "tool_slug": "TWITTER_<NAME>",
       "arguments": { ...per the schema... },
       "account": "twitter_not-tenne"   (or omit — default is correct)
     }
   ]
   sync_response_to_workbench = false
   thought = "one-line rationale"

2. Read response.data — the Twitter API response shape passes through
   (data.tweets[], data.text, data.id, etc.). Surface only the useful
   bits to the user — full API JSON is overkill in Telegram.
```

## Write-action safety (MANDATORY)

Posting / deleting / liking / following / DMing are publicly visible +
irreversible. Pattern:

```
User: post a tweet: "shipped browser-harness write-as-you-learn today"
Bot:  Draft:
        "shipped browser-harness write-as-you-learn today"
        (will post to @<your handle> via composio)
      Reply OK to publish, EDIT <new text> to change, CANCEL to drop.
User: OK
Bot:  Posted. https://x.com/<handle>/status/<id>
```

Same pattern as fill-form / book-anything: show what's about to happen,
wait for explicit confirmation, then execute. The TWITTER_CREATION_OF_A_POST
call only fires after user types OK.

For READ actions (search, lookup, my recent tweets, my mentions),
no confirmation gate — just run.

## Common recipes

### Show my last N tweets

```
1. COMPOSIO_SEARCH_TOOLS query: "my user timeline last tweets I posted"
2. From the results pick the slug for "user timeline by id" — likely
   TWITTER_USERS_TWEETS_BY_USER_ID. If you need your own user id, first
   call TWITTER_USER_LOOKUP_ME (also from SEARCH_TOOLS).
3. Execute with max_results=N (typically 5-10).
4. Format response.data[*].text + tweet URL into a markdown list.
```

### Post a tweet (text only)

```
1. Show draft to user, wait for OK / EDIT / CANCEL
2. On OK: COMPOSIO_MULTI_EXECUTE_TOOL with:
     tool_slug = "TWITTER_CREATION_OF_A_POST"
     arguments = {"text": "<approved-text>"}
3. Reply with the resulting tweet URL.
```

### Search Twitter

```
COMPOSIO_MULTI_EXECUTE_TOOL with:
  tool_slug = "TWITTER_RECENT_SEARCH"
  arguments = {"query": "<twitter-search-syntax>", "max_results": 10}

Twitter search syntax supports: from:user, to:user, has:images,
has:links, is:reply, lang:en, "exact phrase", -exclude, since:date.
Same syntax as the x.com search bar.
```

### Look up a specific tweet by URL

```
1. Parse the ID from the URL: x.com/<user>/status/<ID> → <ID>
2. TWITTER_POST_LOOKUP_BY_POST_ID with arguments = {"id": "<ID>"}
3. Surface text + author + engagement counts.
```

## When the user's request is ambiguous

- "summarize this tweet: <url>" → could be twitter-composio OR
  ingest-x-thread. Default: twitter-composio if just-summarize;
  ingest-x-thread if "save this to my reading list" or
  "ingest this thread".
- "draft a tweet about X" → x-draft (uses voice corpus). Not this.
- "post the tweet you just drafted" → twitter-composio (the post step).

## Rate limits

Twitter's free tier is tight: ~50 tweets/day, ~300 reads/15min.
If a request hits 429, the wrapped response will say so. Surface
the error verbatim ("Twitter rate-limited; try again in N min") —
do NOT retry silently or claim success.

## ⚠️ KNOWN ISSUE — Composio platform bug (active 2026-05-18)

**Every `TWITTER_*` tool call currently returns:**
```
'latin-1' codec can't encode characters in position 58-66: ordinal not in range(256)
```

Reproduced live on the simplest possible call (`TWITTER_RECENT_SEARCH`
with `query: "openclaw"`). Position 58-66 = inside Composio's
auth/header construction, not in our query — so the bug fires
regardless of input. Affects: `TWITTER_RECENT_SEARCH`,
`TWITTER_POST_LOOKUP_BY_POST_ID`, `TWITTER_SEARCH_RECENT_COUNTS`,
and almost certainly the rest (didn't probe writes for safety).

**When the agent hits this error, do NOT loop retrying.** Surface
the bug to the user and offer the fallback below. Do NOT pretend
the call succeeded.

### Fallback: browser-harness with x.com cookies

The agent can still drive x.com via the persistent headless Chrome
*if* the user has imported their x.com cookies via /site-login. Check
with `arete-cred list | grep SITE_COOKIES__x` — if present:

```bash
python3 ~/bin/browser-login.py inject x.com
browser-harness <<'PY'
goto_url("https://x.com/<username>/with_replies")
wait_for_load()
import time; time.sleep(2)
text = cdp("Runtime.evaluate",
    expression="document.body.innerText",
    returnByValue=True)["result"]["value"]
print(text[:3000])
PY
```

This works for read paths (show my tweets, my mentions, lookup tweet
by URL, search via the x.com search bar). For write paths (post a
tweet, delete, like) the agent should EXPLICITLY refuse to
auto-click through the x.com UI — composio's CREATION_OF_A_POST is
the right primitive for those and we should wait for the bug fix.

If `SITE_COOKIES__x_com` is NOT in the vault, reply:

> Composio's Twitter integration has a known encoding bug right now
> (server-side, not fixable from here). To use Twitter via the bot
> in the meantime, import your x.com cookies once via /site-login:
> see `skills/site-login/SKILL.md`. Or wait — we'll know it's fixed
> when the next probe stops returning the latin-1 error.

### How we'll know when to revert this fallback

The bug is on Composio's side. Re-probe once a week (or when the user
asks):

```bash
ssh bot 'python3 /tmp/probe-tw-encoding.py'  # if still on the VPS
```

When that returns a SUCCESS payload instead of the latin-1 error,
delete this "KNOWN ISSUE" section and remove the fallback rule.
Don't leave it in once Composio fixes — the cookie path is strictly
worse (login wall risk, manual cookie rotation, browser-harness
overhead per call vs. one Composio MCP call).

## Brain capture

When the user posts a meaningful tweet via the bot, OPTIONALLY append
it to `{{user.brain_path}}/originals/x-posts.md` so the voice corpus stays
current. Not required for every tweet — only when the agent recognizes
genuine original thinking (per the signal-detector pattern).
