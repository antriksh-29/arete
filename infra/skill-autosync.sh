#!/usr/bin/env bash
# skill-autosync.sh — mirror workspace user-skill edits back to the
# personal-assistant git repo + push to GitHub, the same way
# ~/.brain syncs to antriksh-brain.
#
# Architectural shift from the immutable-bit approach (#113):
#
#   chattr +i failed because botuser has NOPASSWD sudo. The bot
#   ran `sudo chattr -i`, wrote, and continued. Wrong abstraction.
#
#   Brain pattern (works): scripts edit ~/.brain/ files freely,
#   then `cd ~/.brain && git commit && git push`. Trust the writer,
#   commit & push the change so it's visible everywhere.
#
#   This script applies the same pattern to user-skills:
#     - inotifywait on $WORKSPACE/skills/<user-skill-slug>/
#     - On change: copy workspace -> repo, update jobs.json cron if
#       SKILL.md `cron:` frontmatter changed, commit, push, ack
#       the user on Telegram with the GitHub commit URL.
#
# Failure modes addressed:
#   1. Bot edits workspace file -> previously: change lost on next
#      deploy. Now: pushed to GitHub within seconds.
#   2. Bot updates SKILL.md cron field but not jobs.json -> previously:
#      schedule didn't actually change. Now: jobs.json updated to
#      match SKILL.md frontmatter on every edit.
#   3. No Telegram confirmation -> previously: user couldn't tell if
#      the change actually persisted. Now: explicit message with
#      "Pushed to GitHub: <url>" link.
#
# Trust model: this script trusts whatever lands in the workspace
# files. The audit daemon (#110-#112) still runs as a tripwire — if
# anything goes wrong (failed safety check, malformed file), it can
# revert. For v1 we ship trust-and-push; future revs can layer
# per-edit safety scans before push.
#
# SSH key: ~/.ssh/github_personal_assistant must already be in the
# ssh-agent or git's IdentityFile config (it is — see ~/.ssh/config
# `Host github.com-personal-assistant`).
#
# Usage:
#   skill-autosync.sh --watch          # systemd-run forever
#   skill-autosync.sh --once           # one pass for slugs with drift

set -uo pipefail

REPO="${REPO:-$HOME/personal-assistant}"
WORKSPACE="${WORKSPACE:-$HOME/.openclaw/workspace}"
WORKSPACE_SKILLS="$WORKSPACE/skills"
REPO_US="$REPO/user-skills"
JOBS_JSON="${JOBS_JSON:-$HOME/.openclaw/cron/jobs.json}"
LOG="/tmp/skill-autosync.log"
TG_SEND="$HOME/bin/send-telegram.sh"
# Trust-ticket dir — sanctioned writers (apply-skill-edit.sh,
# reverse_sync_skill_md_from_jobs_json below) drop a ticket here BEFORE
# editing a workspace user-skill SKILL.md. mirror_to_repo requires a
# matching ticket before pushing to GitHub.
TRUST_DIR="${TRUST_DIR:-$HOME/.brain-bootstrap-raw/state/skill-edit-trust}"
TRUST_TTL_SECONDS=300
mkdir -p "$TRUST_DIR"
MODE="${1:---once}"

log() {
  local msg="[$(date -Iseconds)] $*"
  echo "$msg" | tee -a "$LOG" >&2
}

# Identify slugs that have repo + workspace counterparts
list_managed_slugs() {
  [ -d "$REPO_US" ] || return 0
  for d in "$REPO_US"/*/; do
    [ -f "${d}SKILL.md" ] || continue
    local slug
    slug=$(basename "$d")
    [ -d "$WORKSPACE_SKILLS/$slug" ] || continue
    echo "$slug"
  done
}

# Detect which delivery path a periodic skill should use:
#   • POOL skill  — SKILL.md has a `## Insight pool` (or similar `## ... pool`)
#                   section, the dispatcher picks an item from the pool and
#                   sends it. systemd timer + run-periodic-skill.py path.
#   • FETCH skill — no pool section; SKILL.md describes what to fetch and
#                   format. OpenClaw cron + agentTurn payload, Kimi reads
#                   SKILL.md and runs the fetch via available tools.
#
# Returns "pool" or "fetch" on stdout. Architecture flowing from the
# 2026-05-22 image-v33-followup investigation: PR #116 incorrectly
# made systemd canonical for ALL periodic skills, breaking fetch-style
# ones (YC launches, YouTube recommender) which never had a pool.
classify_skill() {
  local slug="$1"
  local skill_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
  [ -f "$skill_md" ] || { echo "fetch"; return 0; }
  # Look for any `## ... pool` heading anywhere in the body. Catches
  # "## Insight pool", "## Fact pool", "## Quote pool", etc.
  if grep -qE '^##\s+[A-Za-z ]*[Pp]ool\b' "$skill_md" 2>/dev/null; then
    echo "pool"
  else
    echo "fetch"
  fi
}

# Install/update the systemd timer + service for one periodic skill.
# Reads `cron:` from SKILL.md frontmatter, invokes the installer that
# generates the unit files. ALSO removes any matching OpenClaw cron
# entry from jobs.json — the systemd timer is now the canonical
# delivery path (replaces the broken agentTurn dispatch which
# misrouted periodic fires into the skill-author refine flow).
sync_periodic_timer_for_slug() {
  local slug="$1"
  local skill_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
  [ -f "$skill_md" ] || return 0
  # Extract cron value from frontmatter
  local cron_val
  cron_val=$(awk '/^---$/{c++} c==1 && /^cron:/{gsub(/^cron:[[:space:]]*"?|"?[[:space:]]*$/, "", $0); print; exit}' "$skill_md" | head -1)
  local installer="$REPO/infra/install-periodic-skill-timer.sh"
  if [ -z "$cron_val" ] || [ "$cron_val" = "null" ]; then
    # Manual skill: remove any leftover timer
    if systemctl --user list-units --all --no-pager 2>/dev/null | grep -q "skill-${slug}\\.timer"; then
      systemctl --user disable --now "skill-${slug}.timer" >/dev/null 2>&1 || true
      rm -f "$HOME/.config/systemd/user/skill-${slug}.timer" "$HOME/.config/systemd/user/skill-${slug}.service"
      systemctl --user daemon-reload
      log "removed periodic timer for manual skill: $slug"
    fi
  else
    # Don't gate on the +x bit — observed 2026-05-22: the file lost
    # its executable mode and sync_periodic_timer_for_slug skipped
    # SILENTLY (no error, no log line) for weeks. The PG insights
    # timer stayed pinned to 11:30 IST even after /refine moved it
    # to 13:30 because every reconcile pass hit this branch and
    # noped. Always invoke via `bash` so a missing exec bit can't
    # silently break delivery.
    if [ ! -f "$installer" ]; then
      log "  ERROR: install-periodic-skill-timer.sh missing at $installer — cannot install timer for $slug"
    else
      if [ ! -x "$installer" ]; then
        log "  WARN: installer is not executable; invoking via bash explicitly"
      fi
      bash "$installer" "$slug" "$cron_val" "Asia/Kolkata" >> "$LOG" 2>&1 && \
        log "installed/updated periodic timer: $slug (cron=$cron_val)" || \
        log "  ERROR: installer failed for $slug (cron=$cron_val) — see log above for details"
    fi
  fi
  # ALWAYS drop any OpenClaw cron entry for this slug — the systemd timer
  # is the canonical dispatcher now.
  if [ -f "$JOBS_JSON" ]; then
    python3 - "$slug" "$JOBS_JSON" <<'PY'
import json, sys
slug, p = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p))
except Exception:
    sys.exit(0)
before = len(d.get("jobs", []))
d["jobs"] = [j for j in d.get("jobs", []) if (j.get("name") or "").lower() != slug.lower()]
if len(d["jobs"]) != before:
    open(p, "w").write(json.dumps(d, indent=2))
    print(f"removed OpenClaw cron entry for {slug} (replaced by systemd timer)")
PY
  fi
}

# Reverse direction: if jobs.json has a different cron for this slug
# than the workspace SKILL.md, write SKILL.md to match jobs.json. This
# catches the 2026-05-24 failure: agent ran `openclaw cron edit ... --cron
# "0 16 * * *"` directly on the live jobs.json (per AGENTS.md PR #135's
# fork between pool/fetch skills), workspace SKILL.md was never touched,
# inotify never fired, and the schedule change never reached GitHub.
#
# Reconciliation rule: jobs.json wins. It's the live runtime — whatever
# fires the cron is what the user actually has. SKILL.md is the durable
# truth in the repo, so write jobs.json's expr into SKILL.md and let the
# normal mirror_to_repo flow push it.
#
# Cron-only changes don't bump version or last_authoring_turn, so the
# regression guard (PR #135) is a no-op here — it only fires on version
# downgrades or last_authoring rollbacks.
reverse_sync_skill_md_from_jobs_json() {
  local slug="$1"
  local skill_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
  [ -f "$skill_md" ] || return 0
  [ -f "$JOBS_JSON" ] || return 0

  TRUST_DIR="$TRUST_DIR" TRUST_TTL_SECONDS="$TRUST_TTL_SECONDS" \
  python3 - "$slug" "$skill_md" "$JOBS_JSON" <<'PY'
import json, re, sys
slug, skill_md, jobs_path = sys.argv[1], sys.argv[2], sys.argv[3]

# Read jobs.json cron expr for this slug. Match by job name first (the
# canonical identity field after PR #135's rename support), then by
# description containing the slug as a fallback for legacy jobs.
try:
    jobs = json.load(open(jobs_path))
except Exception:
    sys.exit(0)
matching = [j for j in jobs.get("jobs", []) if (j.get("name") or "").lower() == slug.lower()]
if not matching:
    matching = [j for j in jobs.get("jobs", []) if slug.lower() in (j.get("description") or "").lower()]
if not matching:
    sys.exit(0)
jobs_cron = (matching[0].get("schedule") or {}).get("expr")
if not jobs_cron:
    sys.exit(0)

# Read SKILL.md cron.
text = open(skill_md).read()
fm_m = re.match(r"^---\n(.*?)\n---", text, re.S)
if not fm_m:
    sys.exit(0)
fm = fm_m.group(1)
md_cron_m = re.search(r'^cron:\s*"?([^"\n]+)"?\s*$', fm, re.M)
md_cron = md_cron_m.group(1).strip().strip('"') if md_cron_m else None

# Identical → no drift, no work.
if md_cron == jobs_cron:
    sys.exit(0)

# If SKILL.md has cron: null (manual skill) and jobs.json has a real expr,
# that's a schema inconsistency — don't auto-resolve, log + bail. The
# normal sync_cron_for_slug path will handle dropping the stale jobs.json
# entry if SKILL.md is really meant to be manual.
if not md_cron or md_cron.lower() in ("null", "none"):
    print(f"SKIP_REVERSE_SYNC {slug}: SKILL.md has cron=null but jobs.json has {jobs_cron!r}; needs manual reconciliation", file=sys.stderr)
    sys.exit(0)

# Write jobs.json's expr into SKILL.md frontmatter. Preserve quoting
# style if the existing cron field had quotes.
import shutil, hashlib, json, os, time, tempfile
shutil.copy(skill_md, skill_md + ".bak-reverse-sync")
new_fm = re.sub(
    r'^(cron:\s*)"?[^"\n]+"?\s*$',
    rf'\g<1>"{jobs_cron}"',
    fm,
    count=1,
    flags=re.M,
)
new_text = f"---\n{new_fm}\n---" + text[fm_m.end():]
new_bytes = new_text.encode("utf-8")

# Drop a trust ticket so mirror_to_repo's gate accepts this edit. Same
# shape as apply-skill-edit.sh's ticket.
trust_dir = os.environ.get("TRUST_DIR")
if trust_dir:
    os.makedirs(trust_dir, exist_ok=True)
    sha = hashlib.sha256(new_bytes).hexdigest()
    ticket = {
        "slug": slug,
        "expected_sha256": sha,
        "created_at": int(time.time()),
        "ttl_seconds": int(os.environ.get("TRUST_TTL_SECONDS", "300")),
        "issuer": "reverse_sync_skill_md_from_jobs_json",
    }
    fd, tmp = tempfile.mkstemp(dir=trust_dir, prefix=f".{slug}.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(ticket, f)
    os.replace(tmp, os.path.join(trust_dir, f"{slug}.json"))

open(skill_md, "wb").write(new_bytes)
print(f"REVERSE_SYNCED {slug}: SKILL.md cron {md_cron!r} -> {jobs_cron!r} (jobs.json was authoritative)")
PY
}

# Gate: refuse to mirror a workspace user-skill SKILL.md unless a
# matching trust ticket exists. apply-skill-edit.sh (the sanctioned
# user-facing edit path) and reverse_sync_skill_md_from_jobs_json (the
# sanctioned `openclaw cron edit` reconciliation path) both drop a
# ticket BEFORE their write. The agent's direct-exec bypass (`exec cat
# > .../SKILL.md`) leaves no ticket — this guard reverts the workspace
# file from the repo and alerts the user.
#
# Background: 2026-05-26 PG insights cron landed as `30 17 * * *` (5:30pm)
# instead of the user-asked `0 17 * * *` (5pm) because the agent bypassed
# propose-skill-edit.sh and constructed the cron string by hand, keeping
# the existing minute=30. The two-phase pipeline's deterministic Python
# parser would have converted "5pm" correctly. This gate makes the
# pipeline the only path that reaches GitHub.
#
# Returns 0 (allow) and consumes the ticket; non-zero (block) and
# reverts workspace from repo + sends Telegram alert.
guard_against_unsanctioned_edit() {
  local slug="$1"
  local ws="$WORKSPACE_SKILLS/$slug/SKILL.md"
  local rp="$REPO_US/$slug/SKILL.md"
  [ -f "$ws" ] || return 0       # nothing to mirror, nothing to gate
  [ -f "$rp" ] || return 0       # first-time slug — no repo baseline yet, allow
  if cmp -s "$ws" "$rp"; then
    return 0                     # no drift, no edit to gate
  fi
  TRUST_DIR="$TRUST_DIR" TRUST_TTL="$TRUST_TTL_SECONDS" SLUG="$slug" WS="$ws" RP="$rp" \
    python3 - <<'PY' 2>>"$LOG"
import hashlib, json, os, shutil, subprocess, sys, time
slug    = os.environ["SLUG"]
ws      = os.environ["WS"]
rp      = os.environ["RP"]
trust_dir = os.environ["TRUST_DIR"]
ttl     = int(os.environ.get("TRUST_TTL", "300"))

ws_bytes = open(ws, "rb").read()
ws_sha = hashlib.sha256(ws_bytes).hexdigest()
ticket_path = os.path.join(trust_dir, f"{slug}.json")

ticket = None
if os.path.isfile(ticket_path):
    try:
        ticket = json.loads(open(ticket_path).read())
    except Exception:
        ticket = None

now = int(time.time())
reason = None
if not ticket:
    reason = "no ticket"
elif (now - int(ticket.get("created_at", 0))) > int(ticket.get("ttl_seconds", ttl)):
    reason = f"ticket expired (age={now - int(ticket.get('created_at', 0))}s)"
elif ticket.get("expected_sha256") != ws_sha:
    reason = "ticket sha256 mismatch (workspace content does not match sanctioned write)"

if reason is None:
    # Sanctioned. Consume the ticket and allow the mirror.
    try:
        os.remove(ticket_path)
    except FileNotFoundError:
        pass
    sys.exit(0)

# Unsanctioned. Revert workspace from repo. Internal-only log line —
# no user-facing message about the bypass; the recovery happens silently
# in the backend.
ts = time.strftime("%Y-%m-%dT%H:%M:%S")
print(f"[{ts}] UNSANCTIONED_EDIT_BLOCKED slug={slug}: {reason}", file=sys.stderr)
# Quarantine the rogue workspace copy so investigators can inspect it.
quarantine = ws + ".unsanctioned-quarantine." + time.strftime("%Y%m%d-%H%M%S")
shutil.copy(ws, quarantine)
shutil.copy(rp, ws)
print(f"[{ts}] REVERTED slug={slug}: workspace SKILL.md restored from repo; rogue copy at {quarantine}", file=sys.stderr)

# Stale ticket gets cleared so it does not poison the next legitimate edit.
if ticket is not None:
    try:
        os.remove(ticket_path)
    except FileNotFoundError:
        pass

# Auto-recovery: if there's a recent pending-skill-edit for this slug,
# the user's original intent was preserved deterministically by
# propose-skill-edit.sh's parser (e.g. "5pm" → "0 17 * * *"). Spawn
# apply-skill-edit.sh in the background to run the sanctioned alternate
# workflow. autosync's notify_user() then sends the standard friend-voice
# "moved your <skill> to <time>. done." message — from the user's
# perspective only the success confirmation appears, no leak that the
# bot tried the wrong path first.
#
# If no pending file exists (e.g. the agent went straight to direct
# Edit without first calling propose-skill-edit), we have no way to
# recover the user intent — silent revert is the safe outcome.
pending_path = os.path.expanduser(f"~/.brain-bootstrap-raw/state/pending-skill-edits/{slug}.json")
pending = None
if os.path.isfile(pending_path):
    try:
        pending = json.loads(open(pending_path).read())
    except Exception:
        pending = None

if pending and isinstance(pending, dict):
    expires = int(pending.get("expires_at", 0))
    if int(time.time()) <= expires:
        apply_script = os.path.expanduser("~/bin/apply-skill-edit.sh")
        if os.path.isfile(apply_script):
            # Detach so the spawned process outlives this guard. It will
            # block on the same one_pass flock briefly until the current
            # pass releases, then run with a fresh trust ticket from
            # apply-skill-edit and mirror via the sanctioned path.
            subprocess.Popen(
                [apply_script, slug],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[{ts}] AUTO_RECOVERED slug={slug}: spawned apply-skill-edit (pending edit found)", file=sys.stderr)

sys.exit(2)
PY
  return $?
}

# Update jobs.json so a SKILL.md cron field change actually fires.
# Pulls the `cron:` value from SKILL.md frontmatter and rewrites the
# matching job's schedule.expr. Adds the job if it doesn't exist yet.
sync_cron_for_slug() {
  local slug="$1"
  local skill_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
  [ -f "$skill_md" ] || return 0
  [ -f "$JOBS_JSON" ] || return 0

  python3 - "$slug" "$skill_md" "$JOBS_JSON" <<'PY'
import json, re, sys, shutil, uuid
slug, skill_md, jobs_path = sys.argv[1], sys.argv[2], sys.argv[3]

# Parse the `cron:` field out of SKILL.md frontmatter (between leading --- markers)
text = open(skill_md).read()
m = re.match(r"^---\n(.*?)\n---", text, re.S)
if not m:
    sys.exit(0)
fm = m.group(1)
cron_match = re.search(r'^cron:\s*"?([^"\n]+)"?\s*$', fm, re.M)
if not cron_match:
    sys.exit(0)
cron_val = cron_match.group(1).strip().strip('"')
if not cron_val or cron_val.lower() in ("null", "none"):
    # Manual skill — ensure any matching cron job is removed
    cron_val = None

jobs = json.load(open(jobs_path))
shutil.copy(jobs_path, jobs_path + ".bak-autosync")

# Find a job whose description references this slug
matching = [j for j in jobs["jobs"] if slug in (j.get("description") or "").lower()]
changed = False

if cron_val is None:
    # Manual mode: drop any matching job
    if matching:
        jobs["jobs"] = [j for j in jobs["jobs"] if j not in matching]
        changed = True
        print(f"removed cron entry for manual skill {slug}")
elif matching:
    job = matching[0]
    current = job.get("schedule", {}).get("expr")
    # Always upgrade legacy schema (prompt only) to OpenClaw's expected
    # shape (payload+delivery). Without these fields the cron module
    # throws "Cannot read properties of undefined" at fire time and
    # NOTHING actually fires. Lesson learned the hard way from the
    # 11:30 IST PG cron that never fired (#114 follow-up).
    needs_schema_upgrade = "payload" not in job or "delivery" not in job
    if current != cron_val:
        job["schedule"]["expr"] = cron_val
        changed = True
        print(f"updated {slug} cron: {current} -> {cron_val}")
    if needs_schema_upgrade:
        job["name"] = slug
        job["sessionTarget"] = "isolated"
        job["wakeMode"] = "now"
        job["payload"] = {"kind": "agentTurn", "message": f"Run the {slug} skill and deliver its output per user-skills/{slug}/SKILL.md."}
        # Read user's Telegram chat ID from the bot-cred vault. Without
        # `to`, the cron fires but delivery fails (`Delivering to
        # Telegram requires target <chatId>`) — that's the 11:30 IST
        # bug that took 5 PRs to find.
        import subprocess
        try:
            chat_id = subprocess.check_output(["/usr/local/bin/bot-cred", "get", "TELEGRAM_USER_ID"], text=True).strip().splitlines()[0]
        except Exception:
            chat_id = ""
        job["delivery"] = {"mode": "announce", "channel": "last", "to": chat_id}
        job.pop("prompt", None)  # legacy field — OpenClaw doesn't read it
        if "createdAtMs" not in job or isinstance(job.get("createdAtMs"), str):
            import time as _t
            job["createdAtMs"] = int(_t.time() * 1000)
            job.pop("createdAt", None)
        changed = True
        print(f"upgraded {slug} cron schema (added payload+delivery+to={chat_id})")
    # Update description so it matches the actual schedule (helps the user
    # see at a glance what's set without re-reading the cron expression).
    desc_time = ""
    parts = cron_val.split()
    if len(parts) == 5 and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
        try:
            desc_time = f"{int(parts[1]):02d}:{int(parts[0]):02d} IST"
        except (ValueError, TypeError):
            pass
    if desc_time:
        new_desc = f"{slug} skill — daily {desc_time}"
        if job.get("description") != new_desc:
            job["description"] = new_desc
            changed = True
else:
    # Add a new cron entry using OpenClaw's correct schema (mirrors the
    # working CLI-generated shape; delivery requires `to: <chat_id>`).
    import subprocess, time as _t
    try:
        chat_id = subprocess.check_output(["/usr/local/bin/bot-cred", "get", "TELEGRAM_USER_ID"], text=True).strip().splitlines()[0]
    except Exception:
        chat_id = ""
    jobs["jobs"].append({
        "id": str(uuid.uuid4()),
        "name": slug,
        "description": f"{slug} skill (auto-registered from SKILL.md)",
        "enabled": True,
        "createdAtMs": int(_t.time() * 1000),
        "schedule": {"kind": "cron", "expr": cron_val, "tz": "Asia/Kolkata"},
        "sessionTarget": "isolated",
        "wakeMode": "now",
        "payload": {"kind": "agentTurn", "message": f"Run the {slug} skill and deliver its output per user-skills/{slug}/SKILL.md."},
        "delivery": {"mode": "announce", "channel": "last", "to": chat_id},
        "state": {},
    })
    changed = True
    print(f"registered cron for {slug}: {cron_val} -> {chat_id}")

if changed:
    open(jobs_path, "w").write(json.dumps(jobs, indent=2))
PY
}

# Normalize SKILL.md body text so the human-readable description matches
# the actual `cron:` frontmatter value. The bot tends to change the
# `cron:` field but leave the body text saying the old time, which is
# confusing on GitHub. This patches the workspace file in place BEFORE
# mirroring, so repo + workspace + jobs.json all show the same time.
normalize_skill_body() {
  local slug="$1"
  local skill_md="$WORKSPACE_SKILLS/$slug/SKILL.md"
  [ -f "$skill_md" ] || return 0
  python3 - "$skill_md" <<'PY'
import re, sys
p = sys.argv[1]
text = open(p).read()
m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
if not m: sys.exit(0)
fm, body = m.group(1), m.group(2)
cm = re.search(r'^cron:\s*"?([^"\n]+)"?\s*$', fm, re.M)
if not cm: sys.exit(0)
cron_val = cm.group(1).strip().strip('"')
parts = cron_val.split()
if len(parts) != 5: sys.exit(0)
mm, hh = parts[0], parts[1]
try:
    hh_int, mm_int = int(hh), int(mm)
except (ValueError, TypeError):
    sys.exit(0)
hhmm = f"{hh_int:02d}:{mm_int:02d}"
# Patch the description / body wherever a stale schedule mention lives.
new_fm = re.sub(r'schedule `[0-9 \*/-]+`', f'schedule `{cron_val}`', fm)
new_body = re.sub(r'schedule:\s*`[0-9 \*/-]+`', f'schedule: `{cron_val}`', body)
new_body = re.sub(r'(Fires once per day at )\d{1,2}:\d{2}( IST\.)', rf'\g<1>{hhmm}\g<2>', new_body)
new_body = re.sub(r'schedule `[0-9 \*/-]+`', f'schedule `{cron_val}`', new_body)
out = f"---\n{new_fm}\n---\n{new_body}"
if out != text:
    open(p, "w").write(out)
    print(f"normalized body text to match cron {cron_val} ({hhmm} IST)")
PY
}

# Regression guard: reject a workspace SKILL.md if mirroring it would
# DOWNGRADE the repo version. Post-2026-05-23 incident: an unrelated
# elicit/skill-author turn left a stale 2026-05-20 copy of
# paul-graham-insights/SKILL.md in the workspace; this script blindly
# committed it, version went 0.1.1 → 0.1.0, last_authoring rolled back,
# forked_from field disappeared, and the deploy installer pushed the
# PG cron from 1:30pm to 9:00am. The user noticed when the next-day
# schedule was wrong.
#
# A regression looks like ANY of:
#   - frontmatter `version:` decreased (semver-aware compare)
#   - frontmatter `last_authoring_turn:` is older than the repo's
#   - the repo had `forked_from:` and the workspace copy doesn't
#
# Returns 0 (allow) if safe; non-zero (abort + log) if the workspace
# copy looks like a regression. Caller skips mirroring on non-zero.
guard_against_regression() {
  local slug="$1"
  local ws="$WORKSPACE_SKILLS/$slug/SKILL.md"
  local rp="$REPO_US/$slug/SKILL.md"
  [ -f "$ws" ] || return 0
  [ -f "$rp" ] || return 0  # no repo version yet — first sync is always OK
  python3 - "$ws" "$rp" "$slug" <<'PY' || return $?
import re, sys
ws_path, rp_path, slug = sys.argv[1], sys.argv[2], sys.argv[3]

def parse_fm(p):
    txt = open(p).read()
    m = re.match(r"^---\n(.*?)\n---", txt, re.S)
    return m.group(1) if m else ""

def field(fm, key):
    m = re.search(rf'^{key}:\s*"?([^"\n]+)"?\s*$', fm, re.M)
    return m.group(1).strip().strip('"') if m else None

def semver_lt(a, b):
    # a < b? Parse "x.y.z"; missing/non-numeric components → fall back
    # to string compare (which is what semver.lt does for malformed).
    def parts(s):
        return tuple(int(x) if x.isdigit() else 0 for x in (s or "0").split("."))
    return parts(a) < parts(b)

ws_fm, rp_fm = parse_fm(ws_path), parse_fm(rp_path)
ws_ver, rp_ver = field(ws_fm, "version"), field(rp_fm, "version")
ws_lat, rp_lat = field(ws_fm, "last_authoring_turn"), field(rp_fm, "last_authoring_turn")
ws_fork, rp_fork = field(ws_fm, "forked_from"), field(rp_fm, "forked_from")

reasons = []
if ws_ver and rp_ver and semver_lt(ws_ver, rp_ver):
    reasons.append(f"version downgrade: {rp_ver} → {ws_ver}")
if ws_lat and rp_lat and ws_lat < rp_lat:
    reasons.append(f"last_authoring_turn rolled back: {rp_lat} → {ws_lat}")
if rp_fork and not ws_fork:
    reasons.append(f"forked_from field dropped (was {rp_fork})")

if reasons:
    print(f"REGRESSION_BLOCKED slug={slug}: {'; '.join(reasons)}", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
PY
}

# Mirror one slug's workspace files into the repo. Returns 0 if anything
# changed, 1 if no changes. Doesn't push — caller batches push.
mirror_to_repo() {
  local slug="$1"
  # Trust-ticket gate FIRST. Blocks the agent's direct-exec bypass of
  # the propose-skill-edit/apply-skill-edit pipeline. If this fires, the
  # workspace SKILL.md has already been reverted to the repo version by
  # the guard, so subsequent `cmp -s` checks return identical and
  # mirroring is a no-op for this slug.
  if ! guard_against_unsanctioned_edit "$slug"; then
    log "  SKIP mirror_to_repo($slug): unsanctioned-edit guard fired — workspace reverted, push refused"
    return 1
  fi
  # Regression check NEXT. If the workspace copy would downgrade the
  # repo version, abort mirroring for this slug entirely — better to
  # leave the repo intact and surface the conflict than silently push
  # a regression. The error is logged so a human can investigate.
  if ! guard_against_regression "$slug" 2>>"$LOG"; then
    log "  SKIP mirror_to_repo($slug): regression guard fired (see log for details)"
    # Move the offending workspace SKILL.md aside so the watcher doesn't
    # keep re-firing on the same drift every minute.
    local ws="$WORKSPACE_SKILLS/$slug/SKILL.md"
    if [ -f "$ws" ]; then
      mv "$ws" "${ws}.regression-quarantine.$(date -u +%Y%m%d-%H%M%S)"
      log "    quarantined offending workspace SKILL.md → ${ws}.regression-quarantine.*"
    fi
    return 1
  fi
  normalize_skill_body "$slug"
  local changed=0
  for f in SKILL.md eval.yaml; do
    local ws="$WORKSPACE_SKILLS/$slug/$f"
    local rp="$REPO_US/$slug/$f"
    [ -f "$ws" ] || continue
    if [ ! -f "$rp" ] || ! cmp -s "$ws" "$rp"; then
      cp "$ws" "$rp" && changed=1
    fi
  done
  return $((1 - changed))
}

# Commit & push all changes in $REPO_US. Returns 0 if pushed, 1 if no
# changes / push failed.
commit_and_push() {
  local commit_msg="$1"
  cd "$REPO" || return 1
  if git diff --quiet user-skills/ && git diff --cached --quiet user-skills/; then
    return 1  # nothing staged
  fi
  git add user-skills/ || return 1
  if git diff --cached --quiet; then
    return 1
  fi
  if ! git commit -m "$commit_msg" --quiet 2>&1 | tee -a "$LOG" >/dev/null; then
    log "commit failed"
    return 1
  fi
  if ! git push origin main --quiet 2>&1 | tee -a "$LOG" >/dev/null; then
    log "push failed — commit is local at $(git rev-parse HEAD)"
    return 1
  fi
  return 0
}

# Send a friend-voice Telegram confirmation. No slug, no commit URL, no
# file URL — those are internal-state per identity/SOUL.md hard blocklist.
# The user sees a one-line "I tweaked your X" message with the schedule
# change in plain English if relevant. Slug → friendly name conversion:
# pull the `title` (or fallback to `description`) from the SKILL.md
# frontmatter; if missing, replace hyphens with spaces.
notify_user() {
  local slug="$1"
  local _sha="$2"           # kept for back-compat; not surfaced to user
  local cron_change="$3"    # e.g. "6:15pm → 8:30pm" (plain English now)
  local skill_md="$REPO/user-skills/$slug/SKILL.md"

  # Friendly name: title from frontmatter > description > slug-with-spaces.
  local friendly
  friendly=$(awk '
    /^---$/ {fm = !fm; next}
    fm && /^title:/ {sub(/^title:[[:space:]]*/, ""); gsub(/^"|"$/, ""); print; exit}
  ' "$skill_md" 2>/dev/null)
  if [ -z "$friendly" ]; then
    # Fallback: convert "paul-graham-insights" → "paul graham insights"
    friendly=$(echo "$slug" | tr '-' ' ')
  fi

  local msg
  if [ -n "$cron_change" ]; then
    msg="moved your ${friendly} to ${cron_change}. done."
  else
    msg="tweaked your ${friendly}. done."
  fi
  [ -x "$TG_SEND" ] && "$TG_SEND" "$msg" >/dev/null 2>&1 || true
}

# Compute a friend-voice description of a cron change for the user msg.
# Output: "6:15pm" or "8:30am" for the after-time only — the from-time
# is now elided (per friend-voice, "moved your X to 8:30pm" reads cleaner
# than "from 6:15pm to 8:30pm" in chat). Returns empty if no change.
#
# Non-daily expressions fall through to a slug-of-last-resort: skip the
# message entirely (return empty string), since translating arbitrary
# cron rhythms to plain English is non-trivial. The notify_user caller
# then falls back to "tweaked your <X>. done." with no time delta.
describe_cron_change() {
  local _slug="$1"
  local before="$2"
  local after="$3"
  if [ "$before" = "$after" ]; then echo ""; return; fi
  # Daily ("M H * * *"): convert to friendly "h:mmam/pm" lowercase.
  echo "$after" | awk '
    {
      if ($3=="*" && $4=="*" && $5=="*") {
        m = $1+0; h = $2+0;
        if (h == 0)      { printf "%d:%02dam", 12, m }
        else if (h < 12) { printf "%d:%02dam", h, m }
        else if (h == 12){ printf "%d:%02dpm", 12, m }
        else             { printf "%d:%02dpm", h-12, m }
      }
    }
  '
}

# Single pass: for each managed slug, mirror workspace -> repo, sync
# cron if changed, commit + push if anything changed. One commit per
# pass batches multiple slug changes if they happened at once.
#
# IMPORTANT: this function is wrapped in a global flock (see one_pass()
# wrapper below). All concurrent invocations serialize — the watcher's
# inotify-triggered runs and the explicit kick from apply-skill-edit
# can never interleave on the trust-ticket consumption step.
_one_pass_body() {
  local slugs_changed=()
  local cron_changes=()

  for slug in $(list_managed_slugs); do
    local before_cron after_cron
    before_cron=$(jq -r ".jobs[] | select(.description | test(\"$slug\"; \"i\")) | .schedule.expr" "$JOBS_JSON" 2>/dev/null | head -1)
    # Reverse-sync FIRST: if the agent edited jobs.json directly (via
    # `openclaw cron edit`) without touching SKILL.md, pull jobs.json's
    # expr into SKILL.md so the workspace becomes the durable record.
    # This makes mirror_to_repo (next line) detect a real change and push
    # it. Without this, jobs.json edits never reach GitHub.
    reverse_sync_skill_md_from_jobs_json "$slug" 2>>"$LOG"
    if mirror_to_repo "$slug"; then
      slugs_changed+=("$slug")
      # PR 5 (post-image-v33-followup): classify skill type and route to
      # the correct delivery path. Pool skills run via systemd timer +
      # run-periodic-skill.py; fetch skills run via OpenClaw cron +
      # agentTurn (Kimi reads SKILL.md and runs the fetch via tools).
      # Both attempting to register simultaneously creates a race where
      # the wrong path wins and the skill fires nothing or errors out.
      local skill_type
      skill_type=$(classify_skill "$slug")
      log "  slug=$slug type=$skill_type — routing to appropriate delivery"
      if [ "$skill_type" = "pool" ]; then
        # Pool skill: systemd timer is canonical. sync_periodic_timer_for_slug
        # ALSO drops any leftover OpenClaw cron entry.
        sync_periodic_timer_for_slug "$slug"
      else
        # Fetch skill: OpenClaw cron is canonical. Ensure no leftover
        # systemd timer exists, then register the cron entry.
        if systemctl --user list-units --all --no-pager 2>/dev/null | grep -q "skill-${slug}\\.timer"; then
          systemctl --user disable --now "skill-${slug}.timer" >/dev/null 2>&1 || true
          rm -f "$HOME/.config/systemd/user/skill-${slug}.timer" "$HOME/.config/systemd/user/skill-${slug}.service" \
                "$HOME/.config/systemd/user/timers.target.wants/skill-${slug}.timer"
          systemctl --user daemon-reload
          log "removed stray systemd timer for fetch skill: $slug"
        fi
        sync_cron_for_slug "$slug" 2>/dev/null || true
      fi
      after_cron=$(jq -r ".jobs[] | select(.description | test(\"$slug\"; \"i\")) | .schedule.expr" "$JOBS_JSON" 2>/dev/null | head -1)
      local change_desc
      change_desc=$(describe_cron_change "$slug" "$before_cron" "$after_cron")
      cron_changes+=("$slug:$change_desc")
    fi
  done

  if [ ${#slugs_changed[@]} -eq 0 ]; then
    return 0
  fi

  local msg="user-skill autosync: $(IFS=, ; echo "${slugs_changed[*]}")"
  log "$msg"
  if commit_and_push "feat(user-skills): autosync from workspace — ${slugs_changed[*]}"; then
    local sha
    sha=$(cd "$REPO" && git rev-parse HEAD)
    log "pushed $sha"
    # Per-slug Telegram notification
    for entry in "${cron_changes[@]}"; do
      local slug="${entry%%:*}"
      local change="${entry#*:}"
      notify_user "$slug" "$sha" "$change"
    done
  else
    log "commit_and_push returned non-zero (may be a transient git/push error)"
  fi
}

# Global serialization wrapper. Concurrent one_pass invocations (the
# watcher's inotify-triggered runs + the explicit `--once` kick from
# apply-skill-edit.sh) must NOT interleave on the trust-ticket
# consumption step in mirror_to_repo's guard_against_unsanctioned_edit —
# if process A consumes the ticket between process B's isfile() and
# open(), B would falsely conclude no ticket exists and revert a
# legitimate edit. flock makes that race impossible.
ONE_PASS_LOCK="/tmp/skill-autosync.one-pass.lock"
one_pass() {
  (
    flock -x 200 || { log "one_pass: failed to acquire lock"; exit 1; }
    _one_pass_body
  ) 200>"$ONE_PASS_LOCK"
}

watch_mode() {
  log "skill-autosync watch_mode start (repo=$REPO workspace=$WORKSPACE)"
  one_pass  # initial sync of any pending drift

  if ! command -v inotifywait >/dev/null 2>&1; then
    log "inotifywait missing — fallback to 30s polling"
    while true; do
      sleep 30
      one_pass
    done
  fi

  while true; do
    # Wait for any change in the workspace skills dir OR jobs.json.
    # jobs.json gets edited when the agent runs `openclaw cron edit`
    # directly — without this watch, the change would only be detected
    # by the 60s timeout fallback, leaving up to a minute of drift.
    #
    # -t 60 timeouts so we still re-pass every minute as a safety net.
    # head -10 caps how many events we consume per wake (multi-file
    # edits debounce naturally).
    local watch_paths=("$WORKSPACE_SKILLS")
    [ -f "$JOBS_JSON" ] && watch_paths+=("$JOBS_JSON")
    inotifywait -r -e modify,close_write,moved_to,create --timeout 60 \
      "${watch_paths[@]}" 2>/dev/null | head -10 \
      | while read -r _ _; do : ; done
    # Debounce: brief delay so multi-file edits (SKILL.md + eval.yaml,
    # or `openclaw cron edit` writing jobs.json then triggering a hot
    # reload) batch into a single commit.
    sleep 2
    one_pass
  done
}

# Reconcile mode: run the delivery-path setup for EVERY managed skill,
# without requiring workspace drift. Called from deploy-vps.sh after
# sync_user_skills() lands files in the workspace, so a freshly deployed
# skill always has its systemd timer or OpenClaw cron registered — even
# when the inotify watcher missed the file-create event (PR #116-#121
# race: deploy restarts the autosync service at the exact moment new
# files land, tearing down the watcher right when it would have fired).
reconcile_all() {
  log "skill-autosync reconcile_all start (deterministic timer/cron setup)"
  for slug in $(list_managed_slugs); do
    local skill_type
    skill_type=$(classify_skill "$slug")
    log "  reconciling $slug ($skill_type)"
    # Pull any jobs.json drift into SKILL.md first so the rest of the
    # pass operates on a unified source of truth. Same rationale as
    # one_pass — covers the case where a deploy lands after the agent
    # made a cron edit via the OpenClaw CLI between heartbeats.
    reverse_sync_skill_md_from_jobs_json "$slug" 2>>"$LOG"
    if [ "$skill_type" = "pool" ]; then
      sync_periodic_timer_for_slug "$slug"
    else
      # remove any stale systemd timer first
      if systemctl --user list-units --all --no-pager 2>/dev/null | grep -q "skill-${slug}\\.timer"; then
        systemctl --user disable --now "skill-${slug}.timer" >/dev/null 2>&1 || true
        rm -f "$HOME/.config/systemd/user/skill-${slug}.timer" "$HOME/.config/systemd/user/skill-${slug}.service" \
              "$HOME/.config/systemd/user/timers.target.wants/skill-${slug}.timer"
        systemctl --user daemon-reload
        log "    removed stray systemd timer (skill is fetch-type, OpenClaw cron is canonical)"
      fi
      sync_cron_for_slug "$slug" 2>/dev/null || true
    fi
  done
  log "skill-autosync reconcile_all done"
}

case "$MODE" in
  --watch) watch_mode ;;
  --reconcile) reconcile_all ;;
  --once|*) one_pass ;;
esac
