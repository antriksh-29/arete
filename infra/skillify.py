#!/usr/bin/env python3
# skillify — codifies the manual→prototype→eval→codify→cron→monitor
# discipline so the agent is forced to apply it instead of treating
# every recurring workflow as a one-off.
#
# Three subcommands:
#
#   --smoke         cold-boot callability check
#   checklist       prints the 6-stage cycle in a Telegram-friendly
#                   shape — agent shows this when starting a new skill
#                   prototype OR when the user asks "what's the
#                   discipline" / "what are the stages"
#   audit <name>    given an existing skill at skills/<name>/, reports
#                   which stages of the cycle are visible (SKILL.md,
#                   eval.yaml, deterministic script, cron, smoke-test
#                   wrapper). Surfaces gaps explicitly.
#
# Usage:
#   skillify.py --smoke
#   skillify.py checklist
#   skillify.py audit daily-tasks

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── the discipline ─────────────────────────────────────────────

CHECKLIST = [
    ("Concept", "Describe the process in plain language. What does the user say? What output do they need?"),
    ("Prototype", "Run it manually on 3–10 real items. NO SKILL.md yet — just see if the shape is right."),
    ("Evaluate", "Show the user the prototype's output. Iterate on tone, format, scope. Stop when the user says it's right."),
    ("Codify", "Write skills/<name>/SKILL.md following eval/schemas/skill.schema.md. Add eval.yaml with ≥ 20 trigger entries (positive + negative)."),
    ("Cron", "If recurring, schedule via gbrain autopilot or a systemd timer in infra/. If on-demand only, set cron: null."),
    ("Monitor", "Watch the first runs. Add a new eval.yaml entry every time the bot gets it wrong. Skill-drift weekly report (G8) catches silent decay."),
]


def cmd_checklist():
    out = "## Skillify discipline (the cycle that makes the system compounding)\n\n"
    for i, (stage, body) in enumerate(CHECKLIST, 1):
        out += f"{i}. **{stage}** — {body}\n"
    out += "\n*If the user asks for the same thing twice and it's not yet a skill, you failed step 1.*\n"
    print(out, end="")


# ── audit ──────────────────────────────────────────────────────

def cmd_audit(name: str):
    """Report which stages of the cycle are visible for skills/<name>/."""
    skill_dir = REPO_ROOT / "skills" / name
    findings = {}

    # Concept — implied by SKILL.md description field
    skill_md = skill_dir / "SKILL.md"
    findings["concept"] = skill_md.exists()

    # Prototype — we can't audit this directly, it's a process. Mark
    # as "implied present" if SKILL.md exists (means it was written).
    findings["prototype_implied"] = skill_md.exists()

    # Evaluate — eval.yaml present and ≥ 5 triggers
    eval_yaml = skill_dir / "eval.yaml"
    if eval_yaml.exists():
        text = eval_yaml.read_text()
        # Crude trigger count — count lines starting with "  - id:"
        n = sum(1 for L in text.splitlines() if L.strip().startswith("- id:"))
        findings["evaluate"] = {"present": True, "trigger_count": n, "meets_bar": n >= 5}
    else:
        findings["evaluate"] = {"present": False}

    # Codify — backing script referenced in SKILL.md exists
    script_path = None
    cron_value = None
    if skill_md.exists():
        for line in skill_md.read_text().splitlines():
            if line.startswith("script:"):
                script_path = line.split(":", 1)[1].strip()
            elif line.startswith("cron:"):
                cron_value = line.split(":", 1)[1].strip()
    if script_path:
        findings["codify"] = {
            "script": script_path,
            "exists": (REPO_ROOT / script_path).exists(),
        }
    else:
        findings["codify"] = {"script": None, "exists": False}

    # Cron — null is a valid choice (on-demand skill); otherwise the
    # value should be a cron string.
    findings["cron"] = {"declared": cron_value, "scheduled": cron_value not in (None, "null", "")}

    # Monitor — drop-in smoke wrapper at infra/smoke-tests/50-skill-<name>.sh
    smoke_wrapper = REPO_ROOT / "infra/smoke-tests" / f"50-skill-{name}.sh"
    findings["monitor"] = {"smoke_wrapper": smoke_wrapper.exists()}

    # Summary
    gaps = []
    if not findings["concept"]: gaps.append("missing SKILL.md")
    if not findings["evaluate"].get("present"): gaps.append("missing eval.yaml")
    elif not findings["evaluate"].get("meets_bar"): gaps.append(f"eval.yaml has only {findings['evaluate']['trigger_count']} triggers (target ≥ 5)")
    if not findings["codify"].get("exists"): gaps.append(f"script {findings['codify'].get('script') or '(unspecified)'} not found")
    if not findings["monitor"]["smoke_wrapper"]: gaps.append("no infra/smoke-tests/50-skill-<name>.sh wrapper")

    print(json.dumps({
        "ok": len(gaps) == 0,
        "skill": name,
        "stages": findings,
        "gaps": gaps,
        "user_message": (
            f"✓ {name}: skillify-clean across all 6 stages."
            if not gaps
            else f"✗ {name}: {len(gaps)} stage gap(s) — {'; '.join(gaps)}"
        ),
    }, indent=2))


# ── CLI ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] == "--smoke":
        if len(sys.argv) >= 2 and sys.argv[1] == "--smoke":
            print(json.dumps({"ok": True, "skill": "skillify",
                              "user_message": "skillify smoke ok"}))
            return
        sys.exit("Usage: skillify.py {checklist|audit <skill-name>|--smoke}")

    cmd = sys.argv[1]
    if cmd == "checklist":
        cmd_checklist()
    elif cmd == "audit":
        if len(sys.argv) < 3:
            sys.exit("Usage: skillify.py audit <skill-name>")
        cmd_audit(sys.argv[2])
    else:
        sys.exit(f"unknown subcommand: {cmd}")


if __name__ == "__main__":
    main()
