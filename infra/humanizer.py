#!/usr/bin/env python3
# humanizer — deterministic text rewriter that strips "signs of
# AI-generated writing" before the agent sends a message. Per the v2.1
# PRD: removes em-dashes (→ commas / periods / dashes), inflated
# symbolism, vague attributions, and a known AI-vocabulary stop-list.
#
# Pure regex. No external calls. Cheap (microseconds). Idempotent.
# Complements identity/SOUL.md voice tuning — SOUL.md says HOW to
# write; humanizer enforces what NOT to slip in.
#
# Usage:
#   echo "Stored — what a delightful insight." | humanizer.py
#   humanizer.py --smoke
#   humanizer.py --diff "<text>"      # show before/after side-by-side
#
# Result envelope (stdout JSON):
#   {"ok": true, "input": "<original>", "output": "<rewritten>",
#    "edits": [{"rule": "<name>", "matches": <count>}, ...]}

import json
import re
import sys

# ── rules ──────────────────────────────────────────────────────

# Each rule: (name, compiled regex, replacement OR callable). Order
# matters — earlier rules see the original text; later ones see the
# already-rewritten output.

RULES = [
    # 1. Em-dashes — the canonical AI tell. Replace with a comma when
    #    flanked by spaces, with a period when starting a clause, with
    #    a regular hyphen otherwise. Default heuristic: comma.
    ("em_dash_inline", re.compile(r"\s+—\s+"), ", "),
    ("em_dash_clause", re.compile(r"^—\s*", re.MULTILINE), ""),
    ("em_dash_residual", re.compile(r"—"), "-"),

    # 2. AI-vocabulary stop-list (case-insensitive, word-boundary).
    #    These get DELETED — model loves them, humans rarely use them
    #    in casual replies. The sentence usually reads better without.
    ("vocab_delve", re.compile(r"\b(let me |I'll |we'll )?delve into\b", re.I), "look at"),
    ("vocab_tapestry", re.compile(r"\bthe (rich |intricate )?tapestry of\b", re.I), ""),
    ("vocab_navigate_landscape", re.compile(r"\bnavigat(e|ing) the (complex |evolving |modern )?landscape\b", re.I), ""),
    ("vocab_realm", re.compile(r"\bin the realm of\b", re.I), "in"),
    ("vocab_furthermore", re.compile(r"\bfurthermore,\s*", re.I), ""),
    ("vocab_moreover", re.compile(r"\bmoreover,\s*", re.I), ""),
    ("vocab_in_essence", re.compile(r"\bin essence,?\s*", re.I), ""),
    ("vocab_it_is_worth_noting", re.compile(r"\bit('s| is) worth noting that\s*", re.I), ""),
    ("vocab_pivotal", re.compile(r"\bpivotal\b", re.I), "key"),
    ("vocab_meticulous", re.compile(r"\bmeticulous(ly)?\b", re.I), "careful"),
    ("vocab_seamless", re.compile(r"\bseamlessly?\b", re.I), ""),

    # 3. Inflated symbolism / hyperbole. Strip the cliches.
    ("inflate_symphony", re.compile(r"\b(a |the )?(beautiful |elegant )?symphony of\b", re.I), ""),
    ("inflate_dance_of", re.compile(r"\b(a |the )?(delicate |intricate )?dance of\b", re.I), ""),
    ("inflate_wealth_of", re.compile(r"\ba wealth of\b", re.I), "many"),
    ("inflate_treasure_trove", re.compile(r"\b(a |the )?treasure trove of\b", re.I), "lots of"),

    # 4. Vague attributions. Replace with concrete language or strip.
    ("vague_some_say", re.compile(r"\bsome (people |researchers |experts )?say(s)? that\s*", re.I), ""),
    ("vague_studies_show", re.compile(r"\b(many |various )?studies show that\s*", re.I), ""),
    ("vague_it_could_be_argued", re.compile(r"\bit could be argued (that )?\s*", re.I), ""),
    ("vague_widely_regarded", re.compile(r"\b(is |are )?widely regarded as\b", re.I), "is"),

    # 5. Sycophantic openers — never the right answer.
    ("syco_great_question", re.compile(r"^(\s*)(great|excellent|wonderful|interesting|good) question[!.]?\s*", re.I | re.MULTILINE), r"\1"),
    ("syco_certainly", re.compile(r"^certainly[,!.]?\s*", re.I | re.MULTILINE), ""),
    ("syco_absolutely", re.compile(r"^absolutely[,!.]?\s*", re.I | re.MULTILINE), ""),
    ("syco_happy_to_help", re.compile(r"\bhappy to help[!.]?\s*", re.I), ""),

    # 6. Tightness — collapse double spaces left by deletions.
    ("collapse_spaces", re.compile(r"  +"), " "),
    ("collapse_blank_lines", re.compile(r"\n{3,}"), "\n\n"),
]


def rewrite(text: str):
    edits = []
    out = text
    for name, pat, repl in RULES:
        new, n = pat.subn(repl, out)
        if n:
            edits.append({"rule": name, "matches": n})
        out = new
    # Trim leading/trailing whitespace introduced by removals.
    out = out.strip()
    return out, edits


# ── CLI ────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        print(json.dumps({"ok": True, "skill": "humanizer",
                          "user_message": "humanizer smoke ok"}))
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--diff":
        text = " ".join(sys.argv[2:]) or sys.stdin.read()
        out, edits = rewrite(text)
        sys.stdout.write("--- before ---\n")
        sys.stdout.write(text + "\n")
        sys.stdout.write("--- after ----\n")
        sys.stdout.write(out + "\n")
        sys.stdout.write("--- edits ----\n")
        for e in edits:
            sys.stdout.write(f"  {e['rule']:30s} {e['matches']}\n")
        return

    text = sys.stdin.read()
    out, edits = rewrite(text)
    print(json.dumps({"ok": True, "input": text, "output": out, "edits": edits},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
