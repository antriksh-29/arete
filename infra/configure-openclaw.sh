#!/usr/bin/env bash
# configure-openclaw.sh — applies tuned OpenClaw config knobs to the
# live ~/.openclaw/openclaw.json on the VPS. Idempotent; takes a
# timestamped backup before each mutation.
#
# Why this exists: openclaw.json is sensitive (gateway auth token,
# provider profiles) so we don't commit it. But several knobs benefit
# from version-controlled tuning. This script applies the desired
# config in a way that:
#   1. Doesn't store the secrets in the repo
#   2. Is replayable on every deploy
#   3. Logs what changed so unexpected drift is visible
#
# Knobs currently managed:
#   agents.defaults.bootstrapTotalMaxChars  ← 200000 → 60000
#       Drops per-turn bootstrap context from ~50K tokens to ~15K tokens.
#       Keeps the bot well under gpt-5.4-mini's 200K TPM ceiling
#       (was hitting it every few turns and silent-hanging — bug #1
#       in docs/E2E-FINDINGS.md).
#
#   agents.defaults.model.primary           ← deepseek/deepseek-v4-pro
#   agents.defaults.model.fallbacks         ← [moonshot/kimi-k2.6,
#                                              anthropic/claude-haiku-4-5,
#                                              openai/gpt-5.4-mini]
#       2026-05-26: promoted DeepSeek v4 pro to primary. Sonnet-tier
#       benchmarks at ~5× lower cost vs Kimi K2.6 for the agent's heavy-
#       context Telegram path. Kimi K2.6 demoted to fallback-1 — still
#       in the chain because (a) it covers DeepSeek's reported peak-time
#       503s and (b) it covers DeepSeek's known tool-call schema bug
#       (DeepSeek-V3 issue #1244: intermittently emits tool_calls as
#       plain text in `content`, non-deterministic). Both providers
#       route through their own circuit-breaker proxies (deepseek-proxy
#       at 127.0.0.1:18791, moonshot-proxy at 127.0.0.1:18790) for fast
#       first-line failover that hides behind the native fallbacks chain.
#
#       NOTE on the historical gpt-5.4-mini session-format bug:
#       OpenClaw saves OpenAI responses that include `reasoning` items
#       as message references, but fails to preserve the reasoning item
#       itself in session state — every OpenAI turn poison-pills the
#       session with `400 Item 'msg_X' was provided without its required
#       'reasoning' item: 'rs_X'`. To keep openai/gpt-5.4-mini as last-
#       resort fallback without re-triggering this, the session-rotator
#       service must rotate the session JSONL on every detected OpenAI
#       turn (already deployed). If this bug recurs, the right move is
#       to drop gpt-5.4-mini back out of the chain rather than
#       re-litigate session-format.
#
#   agents.defaults.models["deepseek/deepseek-v4-pro"].params.thinking
#                                           ← {type: "disabled"} (instant)
#       Mirrors the Kimi instant-mode discipline that prevents 180s
#       gateway timeouts. If you ever want to try thinking mode, set
#       DEEPSEEK_THINKING=enabled on the deepseek-proxy systemd unit —
#       no code change needed.
#
#   agents.defaults.models["moonshot/kimi-k2.6"].params.thinking
#                                           ← {type: "disabled"} (instant)
#
#   Per-agent model pins (signal-detector, feedback-learner) ←
#                                           ← deepseek/deepseek-v4-pro
#       Per user instruction 2026-05-26: signal-detector and
#       feedback-learner also route via DeepSeek. signal-detector runs
#       on every Telegram turn (RULE 0); feedback-learner is a
#       background analyser. A bare-string override has no fallback
#       chain — if DeepSeek's tool-call schema bug surfaces here, revert
#       these pins to moonshot/kimi-k2.6 first before touching the main
#       chain.
#
#       REVERT (if DeepSeek's reliability profile turns out worse than
#       expected): restore Kimi as primary by reverting this file or by
#       changing the values below to:
#         agents.defaults.model.primary    = "moonshot/kimi-k2.6"
#         agents.defaults.model.fallbacks  = ["openai/gpt-5.4-mini",
#                                             "anthropic/claude-haiku-4-5"]
#         per-agent pins (signal-detector, feedback-learner)
#                                          = "moonshot/kimi-k2.6"
#       See identity/MODEL_ROUTING.md for the full revert recipe.
#       See docs/KIMI-INTEGRATION-FINDINGS.md for prior Kimi cutover.
#
# Usage:
#   bash infra/configure-openclaw.sh          # apply (default)
#   bash infra/configure-openclaw.sh --check  # report current state, no writes
#   bash infra/configure-openclaw.sh --dry    # print what would change, no writes

set -uo pipefail

CONFIG="$HOME/.openclaw/openclaw.json"
MODE="apply"
case "${1:-}" in
  --check) MODE="check" ;;
  --dry)   MODE="dry" ;;
esac

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: $CONFIG not found — is OpenClaw installed?"
  exit 1
fi

# All knobs we manage. Format: "<dot.path>=<value>".
# python types: ints stay ints. Strings need quoting. Bools as JSON.
DESIRED=(
  # bootstrapMaxChars is the PER-FILE limit. AGENTS.md is ~65K chars
  # as of 2026-05-15 (grew past the 60000 ceiling and was being
  # silently 9%-truncated per `openclaw doctor`). Bumped to 80000
  # to give ~15K headroom over current size — buys runway before the
  # next bump. Earlier history: bumped from 40000 → 50000 → 60000
  # each time AGENTS.md grew (routing rules at line 871+ were getting
  # cut, silently breaking daily-task-manager).
  "agents.defaults.bootstrapMaxChars=80000"
  # bootstrapTotalMaxChars is the WHOLE-bootstrap budget (AGENTS.md +
  # all loaded SKILL.md files). Bumped 60K → 100K so the full
  # AGENTS.md + a healthy chunk of SKILL.md content both fit. Still
  # well under gpt-5.4-mini's 200K TPM ceiling (the silent-hang
  # regression from bug #1).
  "agents.defaults.bootstrapTotalMaxChars=100000"

  # channels.telegram.commands.native ← false (set 2026-05-16 after
  # upgrading to openclaw 2026.5.12 + adding 16 upstream skills). Skill
  # catalog grew past Telegram's hard 100-command limit (116 configured),
  # which truncated registered /-commands and triggered the "menu text
  # exceeded the conservative 5700-character payload budget" warning on
  # every gateway start. Disabling native command registration removes
  # the cap entirely. Natural-language routing is unaffected — only the
  # /-commands menu in Telegram is hidden, which we don't use.
  "channels.telegram.commands.native=false"

  # tools.alsoAllow ← ["exec","process"] (set 2026-05-16 after openclaw
  # 2026.5.12 upgrade). Upstream change #47487 stopped the "messaging"
  # profile from implicitly widening to include exec/process just
  # because tools.exec was configured — every Telegram turn started
  # printing `tools policy: profile "messaging" (agent "main") has
  # configured tool sections (tools.exec) that no longer implicitly
  # widen the profile. Add alsoAllow: ["exec","process"] explicitly...`
  # The agent then silently could not shell-out, so every skill that
  # relies on `bash ~/bin/...` (web-search, mcporter-list, gifgrep,
  # daily-tasks, water-log, reading-checkin, etc.) silently soft-declined.
  # This restores the prior behavior: shell exec available to the main
  # agent on the Telegram channel.
  "tools.alsoAllow=[\"exec\",\"process\"]"

  # tools.deny ← ["composio__COMPOSIO_REMOTE_WORKBENCH"] (set 2026-05-20 as
  # part of Thin-Harness-Fat-Skills cost reduction). WORKBENCH ships a
  # ~10.5KB JSON schema on every model turn (largest single tool in the
  # tools array) and we never call it — the other Composio tools cover
  # our workflows. Denying it shaves ~21% off the per-turn tools payload
  # without touching the rest of the Composio surface.
  # tools.deny — Composio remote workbench only.
  #
  # 2026-05-25 REVERT: PR #145 added "message" to this list to close
  # Bug J (tech-narration leak via the native message tool). That fix
  # was wrong-shaped. visibleReplies=message_tool tells openclaw "every
  # visible reply MUST come from message(action=send)". Denying the
  # tool created an impossible invariant: the framework expected every
  # turn to terminate with a message(send) call but I made the tool
  # unavailable. The result was "⚠️ Agent couldn't generate a response"
  # appearing after every turn — usable reply landed via say.sh
  # side-channel, but openclaw correctly reported turn-completion
  # failure because no message(send) ever fired.
  #
  # The proper fix for Bug J is at the OPENCLAW HOOKS layer: wrap the
  # message-tool send path with a humanizer/reply-discipline filter
  # INSIDE the framework's normal flow, rather than bypassing it.
  # Probe v4 showed `openclaw hooks` exposes that surface. Follow-up
  # PR will implement that properly.
  "tools.deny=[\"composio__COMPOSIO_REMOTE_WORKBENCH\"]"

  # messages.visibleReplies ← "message_tool" (set 2026-05-24 to close the
  # reasoning-leak Fix B documented in docs/TODOS-relay-discipline.md).
  # Default behavior ("automatic") forwards every assistant.text emission
  # to Telegram as a separate sendMessage. That's why a single user
  # question produced 8 reasoning bubbles in the 2026-05-24 IMG_9914-9917
  # screenshots. The schema description: "message_tool keeps normal final
  # replies private and requires message(action=send) for visible output."
  #
  # Pairs with infra/say.sh — the bot's side-channel send wrapper that
  # pipes through humanizer + reply-discipline before calling Telegram's
  # Bot API directly. After this knob is on, the agent MUST call say.sh
  # (or openclaw's native message tool) to put text in front of the user.
  # Plain assistant.text becomes invisible by framework guarantee, not
  # by prompt promise.
  #
  # Risk: if the model forgets to call say.sh, the user sees silence
  # instead of a reasoning leak. That's louder than the leak — easy to
  # detect, easy to fix by reinforcing the messagePrefix.
  "messages.visibleReplies=\"message_tool\""

  # agents.defaults.compaction.maxActiveTranscriptBytes ← "80mb" (set
  # 2026-05-25 to address Bug H: the "🔄 I rotated to a fresh session..."
  # bubble that disrupts user flow). The default ~20mb triggers
  # compaction every ~130 messages of a normal-density chat, which the
  # user reports happens multiple times a day. Bumping to 80mb pushes
  # rotation events out by ~4× — about once every few days of heavy
  # use instead of daily. The active transcript itself is summarized
  # by the model's own context-window machinery; this knob only
  # controls when openclaw's local compaction kicks in.
  #
  # Pair this with `openclaw hooks disable compaction-notifier` (see
  # deploy-vps.sh step) which removes the visible chat bubble that
  # fires when compaction runs. Together: rotations are rarer AND
  # silent.
  "agents.defaults.compaction.maxActiveTranscriptBytes=\"80mb\""

  # agents.defaults.compaction.* tuning (set 2026-05-26 as PR-2 of the
  # Bug I family). Bug I (PR #149) addressed the signal-detector session
  # growing unbounded by retiring oversized session JSONL files before
  # each spawn. This block addresses the same failure-mode for the
  # MAIN agent's compaction path — where 22+ failed compactions/day
  # were burning ~$2-3 in LLM time before timing out at 60-85s.
  #
  # keepRecentTokens=25000 — preserve the last ~25k tokens of recent
  # conversation verbatim across compaction, giving the model enough
  # immediate context to keep skill-author flows / multi-turn debugs
  # coherent. Default is lower; raising it trades summary aggression
  # for continuity.
  #
  # reserveTokens=30000 — headroom reserved for reply + tool output
  # post-compaction. Generous because Kimi K2.6 turn outputs (gbrain
  # MCP, composio responses) can be large.
  #
  # midTurnPrecheck.enabled=true — Pi tool-loop precheck that catches
  # context pressure between tool calls, before the next assistant
  # turn balloons the request and tips into a hard 256k ceiling. Cheap
  # to enable, prevents the "context-pressure-then-fail" pattern that
  # gave us the 22 failed compactions.
  #
  # memoryFlush.enabled=true — pre-compaction memory flush writes
  # important state to gbrain BEFORE the heavy compaction summary
  # runs. Means if compaction times out mid-flight, we don't lose
  # active task context — it's already persisted as brain pages.
  #
  # truncateAfterCompaction=true — rotate the active session JSONL
  # after a successful compaction so subsequent turns load only the
  # post-compaction summary instead of replaying the entire pre-
  # compaction history. Without this, the session JSONL keeps the
  # old turns around even after summarization, which is exactly the
  # disk-bloat shape Bug I (in its signal-detector incarnation) hit.
  #
  # notifyUser=false — we already disabled the compaction-notifier
  # hook for Bug H. This is the second layer of belt-and-braces:
  # even if the hook re-enables itself on framework upgrade, the
  # config-level flag keeps user-visible compaction bubbles off.
  #
  # timeoutSeconds=60 — hard cap on a single compaction operation.
  # Probe v10 saw individual compaction attempts running 60-85s
  # against a 2.95MB session. 60s is the natural cliff; setting it
  # explicit means failures fail fast and visibly instead of hanging.
  #
  # All keys verified present in `openclaw config schema` output
  # 2026-05-26. Per-agent overrides at agents.list[*].compaction are
  # NOT supported by the schema (probe v10 confirmed), so these live
  # in agents.defaults — which also benefits signal-detector and any
  # subagent that runs compactions.
  "agents.defaults.compaction.keepRecentTokens=25000"
  "agents.defaults.compaction.reserveTokens=30000"
  "agents.defaults.compaction.midTurnPrecheck.enabled=true"
  "agents.defaults.compaction.memoryFlush.enabled=true"
  "agents.defaults.compaction.truncateAfterCompaction=true"
  "agents.defaults.compaction.notifyUser=false"
  "agents.defaults.compaction.timeoutSeconds=60"
)

# messages.messagePrefix — managed as a here-string so multi-line is
# clean and we don't have to escape \n. POSITIVE-framed Kimi-friendly
# output discipline (the earlier "BANNED phrases" list backfired —
# Kimi K2.6 echoed the banned tokens). Re-runnable: this script
# overwrites VPS drift on every deploy.
MESSAGE_PREFIX="$(cat <<'PREFIX_EOF'
[SYSTEM PREAMBLE]
Visible-output rule (set 2026-05-24, framework-enforced via
messages.visibleReplies=message_tool): plain assistant text DOES NOT
reach the user. The ONLY way the user sees a reply is when you run
`exec ~/bin/say.sh "<text>"`. Everything else you emit is internal-only
and Antriksh will never see it.

This means your turn output is EXACTLY ONE of:
  (a) A tool call that does work (web-search, brain-ask, daily-tasks, etc.)
  (b) `exec ~/bin/say.sh "<text>"` to deliver the user-facing reply.

EVERY TURN MUST END WITH (b) — even casual chat ("hey", "what's up", "ok thanks").
If you skip the say.sh call, the user sees NOTHING — total silence. That's
worse than a leak. Even a one-word friend-voice reply ("hey, what's up?",
"ack", "👍") must be delivered via `exec ~/bin/say.sh "..."`.

The only valid silent turn is when a tool call's stdout was ALREADY
piped to say.sh (so a say.sh call did happen, just inside the pipe).
If neither path fired, you have failed the turn.

For (b), `say.sh` runs the text through humanizer (strips em-dashes,
internal-state jargon, slugs, cron strings, gbrain wiki-link tags) and
reply-discipline (catches NO_REPLY / banned phrases). If it exits
nonzero, the rejection envelope is on stderr — read it, fix the
issue, retry.

`say.sh` ALSO auto-extracts `user_message` from JSON envelopes — if
your input parses as `{"ok":true,"user_message":"..."}`, only the
`user_message` value reaches Telegram. Safe to pipe raw script stdout.

DO NOT use the native `message` tool / `message(action=send)` /
`channels.telegram.send` — they bypass say.sh and skip humanizer +
reply-discipline. Even though the framework would deliver them under
`visibleReplies=message_tool`, you lose the jargon-stripping and
banned-phrase gates. ALWAYS go through `~/bin/say.sh`.

Reasoning, planning narration, and tool-call decision text are all
internal. The user will not see "I need to…", "Let me check…", "The
user wants…" — those emissions are dropped by the framework. Compose
freely, then call say.sh exactly once at the end of the turn with the
final user-facing text.

First tool call of every turn MUST be: exec ~/bin/spawn-signal-detector.sh "<user message verbatim>"

Its stdout ends with an INTENT ROUTER OUTPUT block. Follow it deterministically:

- INTENT=chat → compose the reply per AGENTS.md / SKILL.md, then MUST deliver via `exec ~/bin/say.sh "<reply text>"`. No exception. Even a one-word reply to "hey" goes through say.sh, or the user sees silence.
- INTENT=task_review → run ~/bin/daily-tasks.sh review --date <Y-M-D> --reply "<reply verbatim>", then `~/bin/say.sh -` (read stdout from a pipe) to deliver. Never claim "Applied 0 status updates" without a clean retry.
- INTENT=task_set → run ~/bin/daily-tasks.sh set with the task list; pipe stdout into `~/bin/say.sh -`.
- INTENT=task_add → run ~/bin/daily-tasks.sh add; pipe stdout into `~/bin/say.sh -`.
- INTENT=task_mark → run ~/bin/daily-tasks.sh mark / remove per recipe; pipe stdout into `~/bin/say.sh -`.
- INTENT=task_backlog → run ~/bin/daily-tasks.sh backlog [--age-gte N]; pipe stdout into `~/bin/say.sh -`. Do NOT compose from brain pages. Reply MUST start with "📋 Backlog —".
- INTENT=task_stats → run ~/bin/daily-tasks.sh stats --window <window>; pipe stdout into `~/bin/say.sh -`. Reply MUST start with "📊 Stats —". Never count from memory.
- INTENT=task_history → run ~/bin/daily-tasks.sh history <range>; pipe stdout into `~/bin/say.sh -`.
- INTENT=task_show → run ~/bin/daily-tasks.sh show <target>; pipe stdout into `~/bin/say.sh -` with status emojis intact. Never add suffixes like "— pending" or categorize into "Work"/"Personal" sections.
- INTENT=booking → run python3 ~/bin/book-reservation.py "<user msg>"; pipe envelope user_message into `~/bin/say.sh -`.
- INTENT=booking_confirm → run python3 ~/bin/book-reservation.py --submit; pipe into `~/bin/say.sh -`.
- INTENT=booking_pick_time → run python3 ~/bin/book-reservation.py --pick-time "<time>"; pipe into `~/bin/say.sh -`.
- INTENT=otp_relay → run book-reservation.py --resume "<otp>" or fill-form.py with the OTP; pipe into `~/bin/say.sh -`. Never refuse.
- INTENT=web_search → run bash ~/bin/web-search.sh "<paraphrased query>"; pipe markdown stdout into `~/bin/say.sh --context research_answer -`. (`research_answer` context tells humanizer to keep tech terms.) NEVER reply "I don't have web access" — the script always works.
- INTENT=mcporter_list → run bash ~/bin/mcporter-list.sh; pipe stdout into `~/bin/say.sh -`. If output is "No MCP servers configured.", deliver that — do not suggest `openclaw status` or `cat config.json` to the user.
- INTENT=gifgrep_search → run bash ~/bin/gifgrep.sh "<query>"; pipe stdout into `~/bin/say.sh -`. If output starts with "ABORT: KLIPY_API_KEY missing", deliver that line.
- INTENT=composio_connections → run bash ~/bin/composio-connections.sh (append " --all" if user asked for pending/initiated); pipe markdown stdout into `~/bin/say.sh --context composio_relay -`. (`composio_relay` context preserves the connector / tool names.) Never compose the connections list from memory.
- INTENT=skill_schedule_query → run `bash ~/bin/skill-schedule.sh "<slug-fragment>"` and pipe stdout into `~/bin/say.sh -`. The helper reads SKILL.md authoritatively; you MUST NOT answer the schedule from memory (it's stale half the time).
- INTENT=skill_describe_query → run `bash ~/bin/skill-describe.sh "<slug-fragment>"` and pipe stdout into `~/bin/say.sh -`. Returns friend-voice "your X runs at H:MMpm daily. <capability sentence>." DO NOT cat / Read the SKILL.md file yourself — that exposes YAML/slugs/version (the 2026-05-25 Bug K leak).
- INTENT=skill_mutation_edit → run `bash ~/bin/propose-skill-edit.sh "<slug-fragment>" --change cron --to-time "<H:MMam/pm>"` and pipe stdout into `~/bin/say.sh -`. This writes a pending file, returns a "moving your X to Y. cool?" message, and STOPS. DO NOT apply the change this turn — apply only fires when the user's confirmation arrives on the next turn (handled by INTENT=skill_apply_pending).
- INTENT=skill_apply_pending → run `bash ~/bin/apply-skill-edit.sh "<PENDING_SLUG>"` (the slug is in the router output) and pipe stdout into `~/bin/say.sh -`. apply-skill-edit.sh edits the workspace SKILL.md, kicks autosync, and returns a "moved your X to Y. done." envelope.
- INTENT=skill_cancel_pending → just send the friend-voice ack via say.sh as the router instructs. The pending file was already deleted server-side.

Tool results are authoritative. If a script returns "no tasks for <date>", deliver that — do not invent an empty list. If a script exits nonzero, deliver stderr — do not fabricate success.

Skill-author boundary (Bug M fix 2026-05-26): NEVER autonomously enter the skill-author flow. Specifically: never call `~/bin/elicit.ts`, `meta/skill-author/scripts/elicit.ts`, `refine-diff.ts`, `intake-from-context.ts`, `commit-and-deploy.ts`, or any `/teach` / `/refine` command on your own initiative. Skill-author runs ONLY when the INTENT ROUTER OUTPUT block explicitly tells you to (INTENT=skill_create / skill_refine / skill_create_oneshot / skill_authoring_continue / skill_mutation_edit / skill_apply_pending / skill_cancel_pending / skill_authoring_continue). If the router says INTENT=chat, you handle the message in chat. Examples of messages that look like skill-edit requests but ARE NOT — they are skill INVOCATIONS or chat:
  • "add this to our brain", "save this to brain", "remember this", "note this down", "save this thought" → invoke the `note` skill via direct `gbrain__put_page` MCP call or by running `note.py`. Never propose adding a new trigger phrase to the note skill.
  • "save this URL", "save this link", "add to my reading list" → invoke the reading-list skill, not refine it.
  • "what's my schedule for X" / "tell me about Y" → brain-ask or skill-describe, not refine.
The rule of thumb: if the user is asking you to DO something the bot already knows how to do, you DO IT — you do not modify the skill that does it. Modify only when the router explicitly says so.

Every turn ends with exactly one call to `~/bin/say.sh` unless the turn is genuinely a no-op (e.g., a heartbeat with nothing to surface). Silence is valid; mumbled reasoning is not — and the framework will eat it anyway.

User message follows:
PREFIX_EOF
)"

python3 - "$CONFIG" "$MODE" "$MESSAGE_PREFIX" "${DESIRED[@]}" <<'PYEOF'
import datetime, json, shutil, sys
from pathlib import Path

config_path = Path(sys.argv[1])
mode = sys.argv[2]
message_prefix = sys.argv[3]
desired = sys.argv[4:]

# Append the prefix knob to desired list so it goes through the same
# diff + write machinery as the numeric knobs.
desired = list(desired) + [("messages.messagePrefix", message_prefix)]

# Model routing knobs — paths contain "/" (model refs) and "." inside
# keys so we must use list-form paths to bypass the dotted split.
desired = desired + [
    # 2026-05-26: DeepSeek v4 pro promoted to primary; Kimi K2.6 demoted
    # to fallback-1. Revert recipe in the header comment block + in
    # identity/MODEL_ROUTING.md.
    (["agents", "defaults", "model", "primary"],
     "deepseek/deepseek-v4-pro"),
    (["agents", "defaults", "model", "fallbacks"],
     ["moonshot/kimi-k2.6", "anthropic/claude-haiku-4-5", "openai/gpt-5.4-mini"]),
    # Instant mode for DeepSeek — mirrors Kimi discipline. The proxy ALSO
    # injects this on every request as belt-and-braces; redundancy is fine
    # (DeepSeek treats the second of two identical `thinking` fields as
    # the effective one, and they're identical here).
    (["agents", "defaults", "models", "deepseek/deepseek-v4-pro", "params", "thinking"],
     {"type": "disabled"}),
    (["agents", "defaults", "models", "moonshot/kimi-k2.6", "params", "thinking"],
     {"type": "disabled"}),
    # 2026-05-25 REVERT: PR #145 set this to [] to block all message-tool
    # actions on the main agent. Reverted for the same reason as the
    # tools.deny revert above — broke the framework's "every turn ends
    # with message(send)" invariant. Empty allowlist meant every turn
    # produced "⚠️ Agent couldn't generate a response" even when the
    # actual reply succeeded via say.sh. Proper fix via openclaw hooks
    # is the follow-up.
    # NOTE: models.providers.{moonshot,deepseek}.apiKey is NOT managed here.
    # OpenClaw does not interpolate ${VAR} in models.providers (only
    # SecretRef syntax in some contexts that we couldn't confirm),
    # so the keys must be stored inline. They live in openclaw.json
    # as plaintext (file is mode 600) and are set during initial
    # provisioning. Don't add a knob for them — version-controlling
    # the plaintext keys would leak them. DEEPSEEK_API_KEY belongs in
    # the bot-cred vault (same as MOONSHOT_API_KEY) — deploy-vps.sh
    # reads it from there when seeding openclaw.json.
]

# Per-agent model overrides — applied after the path-based knobs.
# Format: {"<agent.id>": "<provider/model>"}. Agents matching by id
# in agents.list[] will have their .model field set. Missing agents
# are reported but not added (defensive — don't invent agent rows).
PER_AGENT_MODEL = {
    # 2026-05-26: moved to DeepSeek primary per user instruction.
    # WARNING: a bare-string override has no fallback chain — if
    # DeepSeek's intermittent tool-call schema bug (DeepSeek-V3#1244)
    # surfaces on the signal-detector lane, every Telegram turn dies.
    # Watch the journal for the first 24h after deploy; revert to
    # "moonshot/kimi-k2.6" at the first sign of trouble. signal-detector
    # is on the user's critical path (RULE 0 of AGENTS.md), so this is
    # the highest-risk pin.
    "signal-detector": "deepseek/deepseek-v4-pro",
    "feedback-learner": "deepseek/deepseek-v4-pro",
}

with open(config_path) as f:
    cfg = json.load(f)

def _parts(path):
    return path if isinstance(path, list) else path.split(".")

def _fmt(path):
    return path if isinstance(path, str) else ".".join(str(p) for p in path)

def _has(obj, k):
    if isinstance(obj, dict):
        return k in obj
    if isinstance(obj, list) and isinstance(k, int):
        return 0 <= k < len(obj)
    return False

def get(obj, path):
    for k in _parts(path):
        if not _has(obj, k):
            return None
        obj = obj[k]
    return obj

def set_(obj, path, value):
    parts = _parts(path)
    for k in parts[:-1]:
        if not _has(obj, k):
            # Auto-create dicts only — array slots must already exist.
            if isinstance(obj, dict):
                obj[k] = {}
            else:
                raise KeyError(f"cannot traverse into {type(obj).__name__} at key {k!r}")
        obj = obj[k]
    last = parts[-1]
    if isinstance(obj, list) and isinstance(last, int):
        obj[last] = value
    else:
        obj[last] = value

def parse_spec(spec):
    """Each entry is either a 'k=v' string (numeric/JSON-literal knobs)
    or a (path, value) tuple for pre-parsed string knobs like the
    message prefix."""
    if isinstance(spec, tuple):
        return spec
    path, raw_val = spec.split("=", 1)
    try:
        new_val = json.loads(raw_val)
    except json.JSONDecodeError:
        new_val = raw_val
    return path, new_val

changes = []
for spec in desired:
    path, new_val = parse_spec(spec)
    current = get(cfg, path)
    if current != new_val:
        changes.append((path, current, new_val))

# Per-agent model overrides (lookup by id in agents.list[])
agent_list = cfg.get("agents", {}).get("list", []) or []
agent_index = {a.get("id"): i for i, a in enumerate(agent_list) if isinstance(a, dict)}
for agent_id, target_model in PER_AGENT_MODEL.items():
    if agent_id not in agent_index:
        print(f"  WARN: per-agent override skipped — no agents.list[] entry with id={agent_id}", file=sys.stderr)
        continue
    idx = agent_index[agent_id]
    current = agent_list[idx].get("model")
    if current != target_model:
        changes.append((["agents", "list", idx, "model"], current, target_model))

if mode == "check":
    print(f"=== {config_path}")
    print(f"=== checking {len(desired)} managed knob(s)")
    for spec in desired:
        path, target = parse_spec(spec)
        current = get(cfg, path)
        tag = "✓" if current == target else "✗"
        # Truncate long string values in the check display
        cur_disp = repr(current)
        tgt_disp = repr(target)
        if len(cur_disp) > 80: cur_disp = cur_disp[:80] + "..."
        if len(tgt_disp) > 80: tgt_disp = tgt_disp[:80] + "..."
        print(f"  {tag} {_fmt(path)} = {cur_disp}  (target {tgt_disp})")
    sys.exit(0 if not changes else 1)

if not changes:
    print(f"✓ openclaw.json already matches desired config ({len(desired)} knob(s) checked)")
    sys.exit(0)

print(f"=== {len(changes)} change(s) {'would be' if mode == 'dry' else 'will be'} applied")
for path, current, new_val in changes:
    cur_disp = repr(current)
    tgt_disp = repr(new_val)
    if len(cur_disp) > 80: cur_disp = cur_disp[:80] + "..."
    if len(tgt_disp) > 80: tgt_disp = tgt_disp[:80] + "..."
    print(f"  {_fmt(path)}: {cur_disp} → {tgt_disp}")

if mode == "dry":
    sys.exit(0)

# Backup before mutating
backup = config_path.with_suffix(config_path.suffix + f".bak.{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}")
shutil.copy(config_path, backup)
print(f"  backup: {backup}")

for path, _, new_val in changes:
    set_(cfg, path, new_val)

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"✓ wrote {config_path}")
print()
print("NOTE: openclaw-gateway must be restarted for config changes to take effect.")
print("       systemctl --user restart openclaw-gateway.service")
PYEOF
