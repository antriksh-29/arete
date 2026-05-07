#!/usr/bin/env python3
"""Patch openclaw dist to remove NO_REPLY teaching from the system prompt.

We DON'T touch the runtime's NO_REPLY handling (that's already neutered via
config). We just stop telling the model to use it in the first place.
"""
import re, sys, os

F1 = "/home/botuser/.bun/install/cache/openclaw@2026.4.26@@@1/dist/system-prompt-Ble5uzBt.js"
F2 = "/home/botuser/.bun/install/cache/openclaw@2026.4.26@@@1/dist/get-reply-eY9NJdyX.js"

REPLACEMENT_BLOCK = '"## Reply Discipline", "Always produce a real, contextual reply on every direct conversation turn. Never emit a silent-reply sentinel like NO_REPLY; never leave a turn empty.", ""'

REPLACEMENT_LINE = '"Always produce a real, contextual reply. Never use a silent-reply sentinel like NO_REPLY."'

def patch_system_prompt(src: str) -> str:
    # Match the multi-arg lines.push(...) call that includes "## Silent Replies"
    pattern = re.compile(
        r'"## Silent Replies"[^;]*?✅ Right: \$\{SILENT_REPLY_TOKEN\}`, ""',
        re.DOTALL
    )
    new, n = pattern.subn(REPLACEMENT_BLOCK, src)
    return new, n

def patch_get_reply(src: str) -> str:
    # Two locations — both contain "If no response is needed, reply with exactly..."
    pattern1 = re.compile(
        r'`If no response is needed, reply with exactly "\$\{params\.silentToken\}" \(and nothing else\) so OpenClaw can send a short fallback reply\.`'
    )
    src, n1 = pattern1.subn(REPLACEMENT_LINE, src)
    # Second pattern — ternary block
    pattern2 = re.compile(
        r'`If no response is needed, reply with exactly "\$\{params\.silentToken\}" \(and nothing else\) so OpenClaw stays silent\. Do not add any other words, punctuation, tags, markdown/code blocks, or explanations\.`'
    )
    src, n2 = pattern2.subn(REPLACEMENT_LINE, src)
    pattern3 = re.compile(
        r'`If no response is needed, reply with exactly "\$\{params\.silentToken\}" \(and nothing else\) so OpenClaw can send a short fallback reply\. Do not add any other words, punctuation, tags, markdown/code blocks, or explanations\.`'
    )
    src, n3 = pattern3.subn(REPLACEMENT_LINE, src)
    return src, n1 + n2 + n3

for path, fn in [(F1, patch_system_prompt), (F2, patch_get_reply)]:
    backup = path + ".bak.before-noreply-patch"
    if not os.path.exists(backup):
        with open(path) as f: src_orig = f.read()
        with open(backup, "w") as f: f.write(src_orig)
    with open(path) as f: src = f.read()
    new, n = fn(src)
    with open(path, "w") as f: f.write(new)
    print(f"patched {os.path.basename(path)}: {n} replacement(s)")
    # Sanity assert
    if "Silent Replies" in new and path.endswith("system-prompt-Ble5uzBt.js"):
        print("  WARNING: 'Silent Replies' still present in system-prompt", file=sys.stderr)
    if "short fallback reply" in new:
        print("  WARNING: 'short fallback reply' still present", file=sys.stderr)
