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
# Context-awareness (added with the friend-voice retune):
#
#   --context chat          (default) — strictest. Strips internal-
#                            plumbing jargon (slugs, cron, commit URLs,
#                            model names) on top of the always-on rules.
#   --context internal_status — same as chat. Used by infra scripts that
#                                emit status from Alfred about himself.
#   --context composio_relay — relaxed. Skips the internal-jargon
#                                blocklist. Used when relaying content
#                                from a Composio-connected tool (GitHub,
#                                Slack, Asana, Gmail, etc.) where the
#                                user expects tech terms.
#   --context research_answer — relaxed. Skips the internal-jargon
#                                blocklist. Used when answering a
#                                user-initiated tech question.
#
# The always-on rules (em-dashes, sycophancy, filler vocab) run in
# every context.
#
# Usage:
#   echo "Stored — what a delightful insight." | humanizer.py
#   humanizer.py --smoke
#   humanizer.py --diff "<text>"      # show before/after side-by-side
#   echo "..." | humanizer.py --context composio_relay
#
# Result envelope (stdout JSON):
#   {"ok": true, "input": "<original>", "output": "<rewritten>",
#    "edits": [{"rule": "<name>", "matches": <count>}, ...]}

import json
import re
import sys

# ── always-on rules ────────────────────────────────────────────
#
# Each rule: (name, compiled regex, replacement OR callable). Order
# matters — earlier rules see the original text; later ones see the
# already-rewritten output.

ALWAYS_RULES = [
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
    ("syco_as_an_ai", re.compile(r"\bas an AI[,]?\s*", re.I), ""),
    ("syco_id_be_happy_to", re.compile(r"\bI'?d be happy to\s*", re.I), "I can "),

    # 6. Status-ping noise. The result is what reaches Antriksh, not
    #    the play-by-play. Strip "Fetching ...", "Now submitting ...",
    #    "The command ran successfully.", and similar narration.
    ("status_fetching", re.compile(r"^\s*Fetching\s+[^\n.]+\.\.\.?\s*$\n?", re.I | re.MULTILINE), ""),
    ("status_now_submitting", re.compile(r"^\s*Now submitting[^\n.]*\.?\s*$\n?", re.I | re.MULTILINE), ""),
    # "The command ran successfully…" — match anywhere in the message,
    # not just on its own line. The skill-author flow often appends
    # further narration on the same line.
    ("status_command_ran", re.compile(r"\bThe command ran successfully[^.]*\.\s*", re.I), ""),
    ("status_skill_prompt_was_sent", re.compile(r"\bThe skill prompt was sent to the user[^.]*\.\s*", re.I), ""),
    ("status_users_reply_will_arrive", re.compile(r"\bThe user's reply will arrive[^.]*\.\s*", re.I), ""),
    ("status_tool_call_meta", re.compile(r"\(message_id=\d+\)", re.I), ""),
    ("status_intent_tag", re.compile(r"\bwith INTENT=[A-Z_]+\b", re.I), ""),
    ("status_intent_tag_bare", re.compile(r"\bINTENT=[A-Z_]+\b", re.I), ""),
    ("status_future_turn", re.compile(r"\bon a future turn\b", re.I), ""),

    # 7. Tightness — collapse double spaces left by deletions.
    ("collapse_spaces", re.compile(r"  +"), " "),
    ("collapse_blank_lines", re.compile(r"\n{3,}"), "\n\n"),
]


# ── internal-plumbing rules (skip in composio_relay / research_answer) ──
#
# These strip Alfred's own implementation details: cron strings, slugs,
# commit URLs to this repo, model names, raw API errors, brain-page
# metadata. They only run when Alfred is talking about himself.

INTERNAL_RULES = [
    # Cron expressions in any form. Five-field whitespace-separated
    # numerics-or-stars (with optional / , -). Avoid \b around the
    # tokens because `*` isn't a word character and would prevent the
    # first/last field from matching. Use lookarounds for whitespace
    # or string edges instead.
    ("cron_expression",
     re.compile(r"(?<![\d*,/-])([\d*,/-]+\s+){4}[\d*,/-]+(?![\d*,/-])"),
     "<schedule>"),

    # Skill slugs referenced as filesystem paths. Strip the
    # `user-skills/` prefix; the caller should rephrase to friendly
    # name. This catches the very common "Updated user-skills/foo" leak.
    ("slug_user_skills_path", re.compile(r"\buser-skills/[\w-]+(/[\w.]+)?", re.I), "this skill"),

    # GitHub URLs to the personal-assistant repo. Strip; don't surface
    # commit/file URLs about Alfred himself.
    ("repo_github_url", re.compile(r"https?://github\.com/antriksh-29/personal-assistant/\S+", re.I), ""),

    # "Commit: <sha>" / "Commit: <url>" footers that leak from skill-author
    # ship flows. Match the bare "Commit:" label too (its URL/SHA argument
    # is usually stripped by the URL or SHA rules above).
    ("commit_sha_line", re.compile(r"^\s*Commit:\s*[a-f0-9]{7,40}\s*$\n?", re.I | re.MULTILINE), ""),
    ("commit_url_line", re.compile(r"\bCommit:\s*(https?://\S+)?\s*", re.I), ""),
    ("file_url_line", re.compile(r"\bFile:\s*(https?://\S+)?\s*", re.I), ""),
    ("commit_sha_inline", re.compile(r"\bcommit\s+[a-f0-9]{7,40}\b", re.I), "the change"),

    # "Pushed to GitHub" victory laps.
    ("pushed_to_github", re.compile(r"\b—?\s*pushed to GitHub\.?\s*", re.I), ""),
    ("updated_user_skills", re.compile(r"✅?\s*Updated user-skills[^\n]*\n?", re.I), ""),

    # Internal-state vocabulary. The model loves these; users don't
    # know what they mean.
    # Order: vocab_rotated_session must run BEFORE vocab_fresh_session,
    # else the "fresh session" substring gets stripped first and the
    # broader pattern can no longer match.
    ("vocab_rotated_session", re.compile(r"\b(I )?rotated to a (fresh|new) session[^.\n]*\.?\s*", re.I), ""),
    ("vocab_fresh_session", re.compile(r"\bfresh session\b", re.I), ""),
    ("vocab_context_limit", re.compile(r"\b(close to the model'?s )?context limit\b", re.I), ""),
    # Bug H (2026-05-25): the compaction-notifier hook emits messages
    # like "🔄 I rotated to a fresh session — your conversation had
    # grown to N messages, close to the model's context limit. Earlier
    # conversation is saved so I can recall it from the brain pages."
    # Primary fix is disabling the hook (see infra/deploy-vps.sh).
    # These are defense-in-depth in case the hook re-enables or a
    # similar notifier surfaces later.
    ("compaction_emoji_lead", re.compile(r"^\s*🔄\s*", re.MULTILINE), ""),
    ("compaction_conversation_grown", re.compile(r"\byour conversation had grown to \d+ messages[^.]*\.\s*", re.I), ""),
    ("compaction_brain_pages_recall", re.compile(r"\bEarlier conversation is saved so I can recall it from the brain pages\.\s*", re.I), ""),
    ("compaction_session_compaction", re.compile(r"\bsession compaction (started|finished|complete)[^.]*\.\s*", re.I), ""),
    ("vocab_brain_page", re.compile(r"\bbrain page(s)?\b", re.I), "notes"),
    ("vocab_gateway_restart", re.compile(r"\bGateway was restarted[^.]*\.\s*", re.I), ""),
    # "fires at" / "to fire at" / "now fires at" — internal-cron-speak
    # for "is on for". Order: longest-match first.
    ("vocab_to_fire_at", re.compile(r"\bto fire at\b", re.I), "for"),
    ("vocab_now_fires_at", re.compile(r"\bnow fires at\b", re.I), "is on for"),
    ("vocab_fires_at", re.compile(r"\bfires? at\b", re.I), "is on for"),
    ("vocab_delivers_at", re.compile(r"\bdelivers? (this )?(daily |weekly )?at\b", re.I), "is on for"),
    ("vocab_scheduled_job", re.compile(r"\bscheduled job\b", re.I), ""),
    ("vocab_cron_job", re.compile(r"\bcron job\b", re.I), "scheduled task"),

    # Internal model/runtime names when self-referencing.
    ("model_kimi", re.compile(r"\b(Moonshot|Kimi(?:\s*K\d+(\.\d+)?)?)\b"), ""),
    ("model_openclaw", re.compile(r"\bOpenClaw\b"), ""),
    ("model_hermes", re.compile(r"\bHermes\b"), ""),

    # Raw API error JSON dumps that leak from one-shot LLM failures.
    # Match patterns like: {"error":{"message":"...","type":"..."}}
    # and the "LLM call failed: <provider> <status>:" preamble.
    ("api_error_json", re.compile(r'\{"error":\s*\{[^}]*\}[^}]*\}', re.I), "<api error>"),
    ("api_error_preamble", re.compile(r"\bLLM call failed:\s*[^.]+\.\s*", re.I), ""),

    # Telemetry: token counts, cache hits, spend strings.
    ("spend_line", re.compile(r"^\s*💰?\s*Spend on [\d-]+: \$[\d.]+[^\n]*\n?", re.I | re.MULTILINE), ""),
    ("token_counts", re.compile(r"\b\d{4,}\s+tokens\b[^.]*", re.I), ""),

    # Menu syntax footers.
    ("menu_yes_no_cancel", re.compile(r"\bReply yes to save it,?\s*no to start over,?\s*(or |and )?cancel to throw it away\.?\s*", re.I), ""),
    ("menu_approve_edit_reject", re.compile(r"\bReply:\s*approve[^.\n]*reject[^.\n]*\.?\s*", re.I), ""),
    ("menu_reply_to_slug", re.compile(r"↩\s*reply to /[\w-]+", re.I), ""),

    # Markdown headers/bullets in chat (Telegram doesn't render them
    # nicely anyway — they're a chat-as-document tell).
    ("md_heading", re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),
    ("md_bold", re.compile(r"\*\*([^*\n]+)\*\*"), r"\1"),

    # YAML frontmatter blocks + bare slug-shaped tokens (Bug K, 2026-05-25).
    # If the agent ever dumps a SKILL.md file's content (instead of
    # routing through skill-describe.sh), strip the YAML structure.
    # Strict-but-safe: only match the literal `---\n...\n---` opener +
    # closer with at most a few hundred chars between.
    ("yaml_frontmatter_block",
     re.compile(r"^---\s*\n.{1,1500}?\n---\s*\n?", re.DOTALL | re.MULTILINE),
     ""),
    # Bare frontmatter field lines that escaped the block matcher (e.g.
    # when the file lacks proper closer dashes). Match individual keys
    # we know live in our SKILL.md schema.
    ("yaml_skill_fields",
     re.compile(r"^(name|version|description|cron|created_by|source|created_at|last_authoring_turn|renamed_from|mutating|trigger|eval):\s*[^\n]*\n?",
                re.MULTILINE | re.IGNORECASE),
     ""),

    # GBrain wiki-link tags (e.g. `[[concepts/ai-agents]]`,
    # `[[companies/anthropic]]`, `[[people/manthangupta]]`,
    # `[[founder-mode]]`, `[[media/social/x-...]]`) are internal
    # gbrain navigation markers — they should NEVER reach the user.
    # The 2026-05-24 bookmarks-list regression (IMG_9923-9928) leaked
    # dozens of these into chat. Two-step strip:
    #   1. Replace `[[foo/bar-baz]]` with the human-readable form
    #      (last path component, dashes → spaces) since the surrounding
    #      sentence usually needs SOME noun in that slot.
    #   2. Bare `[[foo]]` (no slash) → just the inner text.
    # Strict-but-safe: leaves the rest of the sentence intact.
    ("wikilink_pathed",
     re.compile(r"\[\[([\w-]+/)+([\w-]+)\]\]"),
     lambda m: m.group(2).replace("-", " ")),
    ("wikilink_bare",
     re.compile(r"\[\[([^\]\n|]+)\]\]"),
     lambda m: m.group(1).replace("-", " ")),
]


def rewrite(text: str, context: str = "chat"):
    edits = []
    out = text

    rules = list(ALWAYS_RULES)
    if context in ("chat", "internal_status"):
        # Internal rules apply where Alfred talks about himself.
        # Inserted BEFORE the always-on rules so the always-on
        # collapse_spaces/blank_lines can clean up afterward.
        rules = INTERNAL_RULES + ALWAYS_RULES

    for name, pat, repl in rules:
        new, n = pat.subn(repl, out)
        if n:
            edits.append({"rule": name, "matches": n})
        out = new
    # Trim leading/trailing whitespace introduced by removals.
    out = out.strip()
    return out, edits


# ── CLI ────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    context = "chat"
    # Pull --context out of args without disturbing positional handling.
    if "--context" in args:
        i = args.index("--context")
        if i + 1 < len(args):
            context = args[i + 1]
            del args[i:i + 2]

    if args and args[0] == "--smoke":
        print(json.dumps({"ok": True, "skill": "humanizer",
                          "user_message": "humanizer smoke ok"}))
        return

    if args and args[0] == "--diff":
        text = " ".join(args[1:]) or sys.stdin.read()
        out, edits = rewrite(text, context=context)
        sys.stdout.write("--- before ---\n")
        sys.stdout.write(text + "\n")
        sys.stdout.write(f"--- after (context={context}) ---\n")
        sys.stdout.write(out + "\n")
        sys.stdout.write("--- edits ----\n")
        for e in edits:
            sys.stdout.write(f"  {e['rule']:30s} {e['matches']}\n")
        return

    text = sys.stdin.read()
    out, edits = rewrite(text, context=context)
    print(json.dumps({"ok": True, "input": text, "output": out, "edits": edits,
                      "context": context},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
