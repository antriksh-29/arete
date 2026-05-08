#!/usr/bin/env python3
# identity-audit.py — lint identity/*.md for transient content that
# probably doesn't belong in a constitutional document.
#
# Surfaces candidates; never edits. Triage is a judgment call only the
# user can make — false positives are fine, the point is to make
# transient content visible so it doesn't quietly rot.
#
# Categories:
#   date       — specific YYYY-MM-DD references (likely deadlines or
#                "as of <date>" markers that go stale)
#   state      — "currently", "right now", "in flight", "in progress"
#                (excluding repo paths)
#   recency    — "this week", "today", "yesterday", "recent", "latest"
#   status     — "TODO:", "FIXME:", "WIP:", "Phase X status:"
#   deadline   — words like "ends", "expires", "until <date>"
#
# Usage:
#   identity-audit.py                 → JSON envelope to stdout
#   identity-audit.py --markdown      → human-readable report
#   identity-audit.py --file identity/AGENTS.md  → audit one file
#   identity-audit.py --smoke

import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IDENTITY_DIR = REPO_ROOT / "identity"


# ── patterns ───────────────────────────────────────────────────

# Compile once. Each rule is (category, regex, why).
RULES = [
    # Specific date markers (YYYY-MM-DD)
    ("date", re.compile(r"\b(202[4-9]|20[3-9]\d)-\d{2}-\d{2}\b"),
     "specific date — likely a deadline or as-of marker that goes stale"),

    # State framings
    ("state", re.compile(r"\b(currently|right now|in flight|in progress|at the moment)\b", re.I),
     "in-flight state — likely belongs in a brain query, not a constitutional doc"),

    # Recency framings
    ("recency", re.compile(r"\b(this (week|month|sprint)|today's|yesterday's|recent(ly)?|latest)\b", re.I),
     "recency framing — content scoped to 'now' rots"),

    # Status / TODO markers
    ("status", re.compile(r"\b(TODO|FIXME|WIP|XXX)[:.\s]"),
     "status marker — should live in a tracker, not in identity"),

    # Deadlines
    ("deadline", re.compile(r"\b(soak ends?|expires?|deadline|due (by|on))\b", re.I),
     "deadline — auto-expires; better as a brain page or tracker"),
]


# ── exclusions ─────────────────────────────────────────────────
# Lines we should NOT flag regardless of regex match.

def is_excluded(line: str) -> bool:
    """Return True if this line should be skipped (false-positive guard)."""
    s = line.strip()
    # Frontmatter version / date fields are fine — they're metadata.
    if re.match(r"^(last_audited|version|created|updated):\s*\d{4}-\d{2}-\d{2}", s):
        return True
    # Lines inside code fences (caller handles fence state, not us).
    # Lines that are pure URLs or paths — common in routing docs.
    if re.match(r"^https?://\S+$", s):
        return True
    if re.match(r"^[\w./~\-]+$", s) and "/" in s:  # path-like
        return True
    # Cross-reference to other doc lines like "AGENTS.md line 1025–1050"
    if "AGENTS.md line" in line or "skills/" in line:
        return False  # path mention is fine but don't whitelist whole line
    return False


def is_in_code_fence(state: dict) -> bool:
    return state.get("in_fence", False)


# ── audit one file ─────────────────────────────────────────────

def audit_file(path: Path) -> list[dict]:
    findings = []
    state = {"in_fence": False}
    today = dt.date.today()
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        # Track fenced code blocks — we ignore everything inside them.
        if line.strip().startswith("```"):
            state["in_fence"] = not state["in_fence"]
            continue
        if state["in_fence"]:
            continue
        if is_excluded(line):
            continue
        for category, regex, why in RULES:
            m = regex.search(line)
            if not m:
                continue
            note = ""
            # For date hits, compute age — anything > 30 days old is
            # extra-suspicious (more likely truly stale).
            if category == "date":
                try:
                    d = dt.date.fromisoformat(m.group(0))
                    age = (today - d).days
                    if age > 30:
                        note = f" — {age}d old"
                    elif age < -90:
                        note = f" — {-age}d in the future (deadline?)"
                    else:
                        note = f" — {age}d offset"
                except Exception:
                    pass
            findings.append({
                "line": lineno,
                "category": category,
                "match": m.group(0),
                "why": why + note,
                "text": line.strip()[:120],
            })
            break   # one finding per line
    return findings


# ── audit all ──────────────────────────────────────────────────

def audit_all(target_file: str | None) -> dict:
    if target_file:
        p = Path(target_file).resolve()
        files = [p] if p.exists() else []
    else:
        files = sorted(IDENTITY_DIR.glob("*.md"))
    per_file = {}
    total = 0
    for f in files:
        f_abs = f.resolve()
        findings = audit_file(f_abs)
        if findings:
            try:
                key = str(f_abs.relative_to(REPO_ROOT))
            except ValueError:
                key = str(f_abs)
            per_file[key] = findings
            total += len(findings)
    return {
        "ok": True,
        "skill": "identity-audit",
        "files_scanned": len(files),
        "findings_total": total,
        "findings": per_file,
        "user_message": (
            f"identity-audit: {total} candidate(s) across {len(per_file)} file(s) "
            f"of {len(files)} scanned. Review and triage — no edits applied."
        ),
    }


# ── markdown formatter ─────────────────────────────────────────

def format_markdown(report: dict) -> str:
    out = []
    out.append(f"# Identity audit — {dt.date.today().isoformat()}\n")
    out.append(report["user_message"] + "\n")
    if report["findings_total"] == 0:
        out.append("No transient-content candidates found.\n")
        return "\n".join(out)
    out.append("\n## Findings by file\n")
    for fpath, findings in sorted(report["findings"].items()):
        out.append(f"\n### `{fpath}` — {len(findings)} candidate(s)\n")
        out.append("| Line | Category | Match | Why |")
        out.append("|---|---|---|---|")
        for f in findings:
            text = f["text"].replace("|", "\\|")
            match = f["match"].replace("|", "\\|")
            why = f["why"].replace("|", "\\|")
            out.append(f"| {f['line']} | {f['category']} | `{match}` | {why} |")
    out.append("\n## Triage notes\n")
    out.append("- This script flags candidates; it doesn't edit. Each line above")
    out.append("  is a judgment call: keep (constitutional), move (to a brain")
    out.append("  page or tracker), or delete (truly stale).")
    out.append("- Date hits older than 30 days are extra-suspect.")
    out.append("- `recency` and `state` hits often imply the content should be")
    out.append("  a `gbrain query` on demand, not a static line.")
    return "\n".join(out)


# ── CLI ────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if args and args[0] == "--smoke":
        print(json.dumps({"ok": True, "skill": "identity-audit",
                          "user_message": "identity-audit smoke ok"}))
        return

    fmt = "json"
    target = None
    i = 0
    while i < len(args):
        if args[i] == "--markdown":
            fmt = "markdown"
        elif args[i] == "--file":
            target = args[i + 1]
            i += 1
        i += 1

    report = audit_all(target)

    if fmt == "markdown":
        sys.stdout.write(format_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
