---
name: github
version: 1
script: null
mutating: true
cron: null
eval: openclaw/skills/github/eval.yaml
last_audited: 2026-05-16
description: |
  Use the `gh` CLI to interact with GitHub repositories: check PR status and CI
  logs, create or merge PRs, create or close issues, and query the GitHub API.
  WHEN: "check the CI status of PR #55", "create a PR for this branch",
  "list open issues in owner/repo", "merge PR #12", "view workflow run logs".
  NOT WHEN: local git operations (commit/push/pull); cloning repos; reviewing
  code diffs in depth (read files directly instead).
trigger: |
  - "check CI / PR status for PR #<N>"
  - "create a PR for this branch"
  - "merge PR #<N>"
  - "list open issues in <owner/repo>"
  - "create an issue in <repo>"
  - "view workflow run logs"
  - "query GitHub API for <data>"
  - "re-run failed CI jobs"
writes_to: []
brain_dirs_read: []
mutates_external_state: true
upstream: openclaw/openclaw@v2026.5.12 skills/github
---

# GitHub Skill

Use the `gh` CLI to interact with GitHub repositories, issues, PRs, and CI.

## When to Use

✅ **USE this skill when:**

- Checking PR status, reviews, or merge readiness
- Viewing CI/workflow run status and logs
- Creating, closing, or commenting on issues
- Creating or merging pull requests
- Querying GitHub API for repository data
- Listing repos, releases, or collaborators

## When NOT to Use

❌ **DON'T use this skill when:**

- Local git operations (commit, push, pull, branch) → use `git` directly
- Non-GitHub repos (GitLab, Bitbucket, self-hosted) → different CLIs
- Cloning repositories → use `git clone`
- Reviewing actual code changes → use `coding-agent` skill
- Complex multi-file diffs → use `coding-agent` or read files directly

## Setup

```bash
# Authenticate (one-time)
gh auth login

# Verify
gh auth status
```

### When the gateway HOME differs from the operator HOME

OpenClaw agent shells often run with a different `HOME` than the user that ran
`gh auth login` (per-agent codex homes, systemd `User=` services, sudo). `gh`
looks up its config under `$GH_CONFIG_DIR`, then `$XDG_CONFIG_HOME/gh`, then
`$HOME/.config/gh`, so the agent shell can report `not logged into any GitHub
hosts` even when the operator login is intact.

To point the gateway at the canonical `gh` config, set `GH_CONFIG_DIR` on the
service environment, e.g.

```bash
# Gateway service env file (example: ~/.openclaw/gateway.systemd.env)
GH_CONFIG_DIR=/path/to/operator/.config/gh
```

then restart the gateway. `openclaw doctor` warns when it detects an authenticated
`hosts.yml` outside the agent process's effective `gh` config dir.

## Common Commands

### Pull Requests

```bash
# List PRs
gh pr list --repo owner/repo

# Check CI status
gh pr checks 55 --repo owner/repo

# View PR details
gh pr view 55 --repo owner/repo

# Create PR
gh pr create --title "feat: add feature" --body "Description"

# Merge PR
gh pr merge 55 --squash --repo owner/repo
```

### Issues

```bash
# List issues
gh issue list --repo owner/repo --state open

# Create issue
gh issue create --title "Bug: something broken" --body "Details..."

# Close issue
gh issue close 42 --repo owner/repo
```

### CI/Workflow Runs

```bash
# List recent runs
gh run list --repo owner/repo --limit 10

# View specific run
gh run view <run-id> --repo owner/repo

# View failed step logs only
gh run view <run-id> --repo owner/repo --log-failed

# Re-run failed jobs
gh run rerun <run-id> --failed --repo owner/repo
```

### API Queries

```bash
# Get PR with specific fields
gh api repos/owner/repo/pulls/55 --jq '.title, .state, .user.login'

# List all labels
gh api repos/owner/repo/labels --jq '.[].name'

# Get repo stats
gh api repos/owner/repo --jq '{stars: .stargazers_count, forks: .forks_count}'
```

## JSON Output

Most commands support `--json` for structured output with `--jq` filtering:

```bash
gh issue list --repo owner/repo --json number,title --jq '.[] | "\(.number): \(.title)"'
gh pr list --json number,title,state,mergeable --jq '.[] | select(.mergeable == "MERGEABLE")'
```

## Templates

### PR Review Summary

```bash
# Get PR overview for review
PR=55 REPO=owner/repo
echo "## PR #$PR Summary"
gh pr view $PR --repo $REPO --json title,body,author,additions,deletions,changedFiles \
  --jq '"**\(.title)** by @\(.author.login)\n\n\(.body)\n\n📊 +\(.additions) -\(.deletions) across \(.changedFiles) files"'
gh pr checks $PR --repo $REPO
```

### Issue Triage

```bash
# Quick issue triage view
gh issue list --repo owner/repo --state open --json number,title,labels,createdAt \
  --jq '.[] | "[\(.number)] \(.title) - \([.labels[].name] | join(", ")) (\(.createdAt[:10]))"'
```

## Notes

- Always specify `--repo owner/repo` when not in a git directory
- Use URLs directly: `gh pr view https://github.com/owner/repo/pull/55`
- Rate limits apply; use `gh api --cache 1h` for repeated queries
