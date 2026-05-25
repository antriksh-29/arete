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
#   agents.defaults.model.primary           ← moonshot/kimi-k2.6
#   agents.defaults.model.fallbacks         ← [haiku-4-5]
#       NOTE: gpt-5.4-mini was REMOVED from the chain 2026-05-15 due to
#       a session-format incompatibility: OpenClaw saves OpenAI responses
#       that include `reasoning` items as message references, but fails
#       to preserve the reasoning item itself in session state. The next
#       turn then sends a dangling reference to OpenAI which rejects
#       with `400 Item 'msg_X' was provided without its required
#       'reasoning' item: 'rs_X'`. This poison-pills the session — once
#       OpenAI serves a turn, every subsequent turn cascade-fails until
#       the session resets. Cleanest fix: keep OpenAI out of the chain
#       until OpenClaw fixes session-format normalization across
#       provider switches. Both Kimi and Haiku use simple message
#       format with no reasoning items, so no schema mismatch possible.
#   agents.defaults.models["moonshot/kimi-k2.6"].params.thinking
#                                           ← {type: "disabled"} (instant)
#
#   Per-agent model pins (signal-detector, feedback-learner) ←
#                                           ← moonshot/kimi-k2.6
#       Originally pinned to openai/gpt-5.4-mini for cost. But OpenAI's
#       responses-API messages include `reasoning` items that OpenClaw
#       doesn't preserve in session state — every signal-detector turn
#       on OpenAI poison-pills its session, surfacing as
#       "⚠️ Something went wrong" to the user (signal-detector runs on
#       every Telegram message via RULE 0). Pinning to Kimi keeps cost
#       low (~same $/token as gpt-5.4-mini), eliminates the schema
#       issue, and inherits instant-mode discipline from
#       agents.defaults.models.
#       Kimi K2.6 as primary with cross-provider fallback chain.
#       Instant mode is mandatory for the Telegram path — without it,
#       Kimi runs default thinking-mode generations that blow the 180s
#       request timeout (verified 2026-05-15 from gateway journal:
#       every Kimi turn timed out and fell through to gpt-5.4-mini).
#       Instant mode also prevents reasoning_content leaking into
#       user-facing text (the original 2026-05-14 symptom).
#       See docs/KIMI-INTEGRATION-FINDINGS.md for the resolution.
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
    # LOCKED 2026-05-15 by user order: Kimi K2.6 is the primary model,
    # gpt-5.4-mini and haiku are secondary. DO NOT CHANGE without
    # explicit user instruction.
    (["agents", "defaults", "model", "primary"],
     "moonshot/kimi-k2.6"),
    (["agents", "defaults", "model", "fallbacks"],
     ["openai/gpt-5.4-mini", "anthropic/claude-haiku-4-5"]),
    (["agents", "defaults", "models", "moonshot/kimi-k2.6", "params", "thinking"],
     {"type": "disabled"}),
    # 2026-05-25 REVERT: PR #145 set this to [] to block all message-tool
    # actions on the main agent. Reverted for the same reason as the
    # tools.deny revert above — broke the framework's "every turn ends
    # with message(send)" invariant. Empty allowlist meant every turn
    # produced "⚠️ Agent couldn't generate a response" even when the
    # actual reply succeeded via say.sh. Proper fix via openclaw hooks
    # is the follow-up.
    # NOTE: models.providers.moonshot.apiKey is NOT managed here.
    # OpenClaw does not interpolate ${VAR} in models.providers (only
    # SecretRef syntax in some contexts that we couldn't confirm),
    # so the key must be stored inline. It lives in openclaw.json
    # as plaintext (file is mode 600) and is set during initial
    # provisioning. Don't add a knob for it — version-controlling
    # the plaintext key would leak it.
]

# Per-agent model overrides — applied after the path-based knobs.
# Format: {"<agent.id>": "<provider/model>"}. Agents matching by id
# in agents.list[] will have their .model field set. Missing agents
# are reported but not added (defensive — don't invent agent rows).
PER_AGENT_MODEL = {
    # 2026-05-24: comment header above ALREADY said "Pinning to Kimi"
    # since the OpenAI session-format regression was found — but the
    # dict still said openai/gpt-5.4-mini. Every deploy ran this script
    # and reverted live fixes back to gpt. Today: gpt's Codex quota was
    # exhausted, the bare-string override has no fallback chain, every
    # user turn died on the signal-detector lane.
    # Comment + code now in sync.
    "signal-detector": "moonshot/kimi-k2.6",
    "feedback-learner": "moonshot/kimi-k2.6",
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
