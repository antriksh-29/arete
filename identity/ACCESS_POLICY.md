# Access Policy

Data sensitivity tiers for content the agent ingests, stores, or transmits.

This file is consulted whenever the agent is about to:

- Log content to Axiom or local logs
- Send content to an external LLM provider (Anthropic, OpenAI, Groq)
- Write content to the brain (markdown + PGLite)
- Push content to GitHub (brain repo or bot-config repo)
- Display content in a Telegram reply

Two tiers. If a piece of content's tier is unclear, default to **Tier 1**
(more restrictive). Better to redact than to leak.

---

## Tier 1 — Credentials / Secrets (highest restriction)

**What's in this tier:**

- API keys, OAuth tokens, refresh tokens, all credentials in
  `/var/lib/bot-creds/vault.db`
- The Fernet master key at `/etc/bot-secrets/master.key`
- SSH private keys (`~/.ssh/id_ed25519*`, `~/.ssh/github_brain*`)
- Bank account numbers, credit/debit card numbers, UPI IDs, payment IDs
- Government IDs (Aadhaar, PAN, passport numbers)
- Two-factor authentication seeds, TOTP secrets, backup codes
- Anything explicitly marked "confidential" or `#tier1` by Antriksh

**Rules:**

- **Never log plaintext.** Redact in any structured log line; replace with
  `<REDACTED:tier1>`.
- **Never send to an external LLM** unless Antriksh has explicitly confirmed
  in the current session via inline button or token reply. No standing
  permission, no "auto-OK".
- **Never write to a brain page.** If a Tier 1 value appears in ingested
  content (e.g., a bank email contains an account number), strip it before
  ingesting. The page records "an account number was mentioned" not the
  number itself.
- **Never push to GitHub.** Even via accidental brain-repo commit. Phase 6
  pre-commit hook should scan for credential-shaped strings.
- **Never display in Telegram replies.** If Antriksh asks "what's my Anthropic
  API key?", refuse and surface as a constitution violation (AGENTS.md rule 2).

## Tier 2 — Everything else (default)

**What's in this tier:**

Almost all content the agent will ever see. Personal life context belongs here,
not segregated:

- Personal journaling, diary entries, emotional reflections
- Conversations about relationships (romantic, family, conflicts)
- Health context: doctor visits, medications, symptoms (when not Tier 1)
- Therapy-adjacent content
- Legal matters in progress
- Negotiations in progress (job offers, deals, contracts)
- Communications with lawyers, doctors, therapists
- Work content
- Meeting notes, calendar events
- Web content fetched via web_fetch
- Forwarded messages, emails, tweets, articles
- General chat with the bot

**Rules:**

- **Full processing allowed.** Logging, LLM calls, brain writes, GitHub pushes
  (private repos only), Telegram replies — all permitted.
- **The brain SHOULD have this context.** A personal assistant that doesn't
  know what's going on in your life is a search interface, not an assistant.
  Personal/relationships/health/legal content goes into the brain so the agent
  can use it for synthesis, retrieval, and contextual replies.
- **Citations required** per gbrain filing rules. Every fact gets `[Source: ...]`.
- **GitHub: private repos only.** `antriksh-brain` and `antriksh-bot-config`
  are both private. The pre-commit hook still scans for Tier 1 leaks.
- **Anthropic preferred over OpenAI/Groq** when the LLM call has a choice
  (Anthropic's data-handling posture is the strongest among providers we use).

---

## How content gets tiered

Default: Tier 2.

**Auto-promotion to Tier 1:**

- Content matches credential regex (e.g., `sk-ant-`, `gho_`, AWS-style key shape)
- Path-traversal request to a known credential dir (`/etc/bot-secrets/`,
  `/var/lib/bot-creds/`, `~/.ssh/`)
- Email body contains words alongside numbers shaped like account/card numbers
- Antriksh marks content `#tier1` explicitly

**Override:** Antriksh can mark any specific content with `#tier1` to force
restriction. Override is sticky for that page. There's no `#tier2` tag because
Tier 2 is the default.

---

## When in doubt

Default to Tier 2. Tier 1 is reserved for actual credentials and high-stakes
identifiers — not a general "this feels sensitive" bucket.

If you find yourself unsure whether something is Tier 1, ask Antriksh in chat:

> "This message includes what looks like an account number. Treat it as a
> credential (don't store) or just a reference (store with a note)?"

He'll answer. Default to Tier 1 until he answers.

---

## Note on this design

PRD §8.7 originally specified four tiers (Financial / Personal / Public /
Shareable). Antriksh consolidated to two on 2026-04-29: secrets are different
in kind from everything else, but personal context (relationships, health,
legal) should live in the brain alongside work content because that's how a
personal assistant becomes useful. Splitting them by sensitivity keeps the
brain shallow and the agent uninformed about the user's actual life.
