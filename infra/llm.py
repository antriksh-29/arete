#!/usr/bin/env python3
# Shared LLM caller with cross-provider failover.
#
# Policy (per identity/MODEL_ROUTING.md — 2026-05-26 DeepSeek experiment):
#   tier="mini"   → deepseek/deepseek-v4-pro
#                   → moonshot/kimi-k2.6
#                   → anthropic/claude-haiku-4-5
#                   → openai/gpt-5.4-mini
#   tier="full"   → deepseek/deepseek-v4-pro
#                   → moonshot/kimi-k2.6
#                   → anthropic/claude-sonnet-4-6
#                   → openai/gpt-5.4
#   tier="nano"   → openai/gpt-5.4-nano
#                   → anthropic/claude-haiku-4-5
#     (nano is reserved/unused — kept on a cheap chain that doesn't
#      involve DeepSeek so the experiment surface is contained)
#   tier="vision" → moonshot/kimi-k2.6
#                   → anthropic/claude-haiku-4-5
#                   → openai/gpt-5.4-mini
#     (DeepSeek v4 pro is TEXT-ONLY — explicitly excluded from this tier)
#
# Failover triggers on:
#   - HTTP 401/402/403/429 (auth, billing, rate-limit)
#   - HTTP 5xx (provider outage)
#   - Network error
# Anything else surfaces as a hard failure on that step (still falls
# through to the next link).
#
# Anthropic's Messages API, OpenAI's Responses API, DeepSeek's OpenAI-
# compatible chat API, and Moonshot's OpenAI-compatible chat API all
# have different shapes; this module hides the difference. Callers pass
# system_prompt + user_msg and get back text.
#
# REVERT PATH: to back out the DeepSeek experiment and restore the
# 2026-05-15 chain, replace _CHAINS below with:
#   "mini":   [("openai", "gpt-5.4-mini"),   ("anthropic", "claude-haiku-4-5")],
#   "full":   [("openai", "gpt-5.4"),         ("anthropic", "claude-sonnet-4-6")],
#   "nano":   [("openai", "gpt-5.4-nano"),    ("anthropic", "claude-haiku-4-5")],
#   "vision": [("openai", "gpt-5.4-mini"),    ("openai",    "gpt-5.4")],

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


# Chain definition: each tier maps to an ordered list of (provider, model)
# tuples. The runtime walks the chain top-to-bottom, falling over on the
# triggers below. First successful link wins; if all links fail, raises.
_CHAINS = {
    "mini": [
        ("deepseek",  "deepseek-v4-pro"),
        ("moonshot",  "kimi-k2.6"),
        ("anthropic", "claude-haiku-4-5"),
        ("openai",    "gpt-5.4-mini"),
    ],
    "full": [
        ("deepseek",  "deepseek-v4-pro"),
        ("moonshot",  "kimi-k2.6"),
        ("anthropic", "claude-sonnet-4-6"),
        ("openai",    "gpt-5.4"),
    ],
    "nano": [
        ("openai",    "gpt-5.4-nano"),
        ("anthropic", "claude-haiku-4-5"),
    ],
    "vision": [
        # DeepSeek v4 pro is text-only — must NOT appear here.
        ("moonshot",  "kimi-k2.6"),
        ("anthropic", "claude-haiku-4-5"),
        ("openai",    "gpt-5.4-mini"),
    ],
}

_FALLBACK_HTTP = {401, 402, 403, 429, 500, 502, 503, 504}


def _vault_get(key):
    r = subprocess.run(["bot-cred", "get", key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else os.environ.get(key, "")


def _log(msg):
    print(f"[llm] {msg}", file=sys.stderr)


def _call_openai(model, system, user, max_tokens, tools=None, image_url=None, image_base64=None):
    key = _vault_get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing")

    if image_url or image_base64:
        content = []
        if user:
            content.append({"type": "input_text", "text": user})
        if image_url:
            content.append({"type": "input_image", "image_url": image_url})
        elif image_base64:
            content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_base64}"})
        input_payload = [{"role": "user", "content": content}]
    else:
        input_payload = user

    payload = {
        "model": model,
        "input": input_payload,
        "instructions": system,
        "max_output_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read())
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    return c["text"].strip()
    raise RuntimeError(f"OpenAI returned no output_text: {json.dumps(resp)[:300]}")


def _call_anthropic(model, system, user, max_tokens, image_url=None, image_base64=None):
    key = _vault_get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing")

    if image_url or image_base64:
        content = []
        if image_url:
            content.append({"type": "image", "source": {"type": "url", "url": image_url}})
        elif image_base64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64}})
        if user:
            content.append({"type": "text", "text": user})
        messages = [{"role": "user", "content": content}]
    else:
        messages = [{"role": "user", "content": user}]

    payload = {
        "model": f"claude-{model}" if not model.startswith("claude-") else model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read())
    for block in resp.get("content", []):
        if block.get("type") == "text":
            return block["text"].strip()
    raise RuntimeError(f"Anthropic returned no text block: {json.dumps(resp)[:300]}")


def _call_openai_compat_chat(model, system, user, max_tokens, base_url, api_key_var,
                             extra_payload=None, image_url=None, image_base64=None,
                             reject_images=False):
    """Generic OpenAI-chat-compatible caller — used for DeepSeek and Moonshot.
    Both providers expose /v1/chat/completions with the standard messages-array
    shape. `extra_payload` lets the caller inject provider-specific fields
    (e.g. DeepSeek's `thinking: {type: disabled}`)."""
    key = _vault_get(api_key_var)
    if not key:
        raise RuntimeError(f"{api_key_var} missing")

    if image_url or image_base64:
        if reject_images:
            raise RuntimeError(f"{model} is text-only; vision tier must not route here")
        # OpenAI chat-completions vision shape (Moonshot accepts this)
        content = []
        if user:
            content.append({"type": "text", "text": user})
        if image_url:
            content.append({"type": "image_url", "image_url": {"url": image_url}})
        elif image_base64:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    else:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if extra_payload:
        payload.update(extra_payload)

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read())
    for choice in resp.get("choices", []):
        msg = choice.get("message") or {}
        text = msg.get("content")
        if text:
            return text.strip()
    raise RuntimeError(f"{base_url} returned no message content: {json.dumps(resp)[:300]}")


def _call_deepseek(model, system, user, max_tokens, image_url=None, image_base64=None):
    # Instant mode by default — matches Kimi discipline that prevents 180s
    # gateway timeouts. To experiment with thinking mode from Python, set
    # the env var DEEPSEEK_THINKING=enabled before running.
    thinking_mode = os.environ.get("DEEPSEEK_THINKING", "disabled").lower()
    extra = {"thinking": {"type": "enabled" if thinking_mode == "enabled" else "disabled"}}
    if thinking_mode == "enabled":
        effort = os.environ.get("DEEPSEEK_REASONING_EFFORT", "")
        if effort:
            extra["reasoning_effort"] = effort
    return _call_openai_compat_chat(
        model, system, user, max_tokens,
        base_url="https://api.deepseek.com",
        api_key_var="DEEPSEEK_API_KEY",
        extra_payload=extra,
        image_url=image_url, image_base64=image_base64,
        reject_images=True,  # text-only — defense in depth
    )


def _call_moonshot(model, system, user, max_tokens, image_url=None, image_base64=None):
    extra = {"thinking": {"type": "disabled"}}
    return _call_openai_compat_chat(
        model, system, user, max_tokens,
        base_url="https://api.moonshot.ai",
        api_key_var="MOONSHOT_API_KEY",
        extra_payload=extra,
        image_url=image_url, image_base64=image_base64,
    )


def _dispatch(provider, model, system, user, max_tokens, tools, image_url, image_base64):
    if provider == "openai":
        return _call_openai(model, system, user, max_tokens,
                            tools=tools, image_url=image_url, image_base64=image_base64)
    if provider == "anthropic":
        return _call_anthropic(model, system, user, max_tokens,
                               image_url=image_url, image_base64=image_base64)
    if provider == "deepseek":
        return _call_deepseek(model, system, user, max_tokens,
                              image_url=image_url, image_base64=image_base64)
    if provider == "moonshot":
        return _call_moonshot(model, system, user, max_tokens,
                              image_url=image_url, image_base64=image_base64)
    raise ValueError(f"unknown provider: {provider}")


def call(system_prompt, user_msg, tier="mini", max_tokens=4000, tools=None, image_url=None, image_base64=None):
    """Call an LLM, walking the tier's chain top-to-bottom on failover.

    Returns (text, provider_used). Raises RuntimeError if every link fails.
    """
    if tier not in _CHAINS:
        raise ValueError(f"unknown tier: {tier}")
    chain = _CHAINS[tier]

    if tools:
        # Tools currently only flow into the OpenAI Responses API call. If
        # we fall over to a non-OpenAI link, the request loses tools. That
        # matches prior behavior (the previous 2-tuple impl also stripped
        # tools on fallback). Loud log so callers can see it happen.
        non_openai = [p for p, _ in chain if p != "openai"]
        if non_openai:
            _log(f"tools requested; non-openai links {non_openai} will receive request without tools")

    last_err = None
    for i, (provider, model) in enumerate(chain):
        # Strip tools when the link isn't openai
        effective_tools = tools if provider == "openai" else None
        try:
            text = _dispatch(provider, model, system_prompt, user_msg, max_tokens,
                             effective_tools, image_url, image_base64)
            if i > 0:
                _log(f"chain[{i}] {provider}/{model} answered after {i} fallback(s)")
            return text, provider
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            _log(f"chain[{i}] {provider}/{model} HTTP {e.code}: {body}")
            last_err = e
            if e.code not in _FALLBACK_HTTP:
                # Non-failover code: still try the next link (the user paid
                # to set up a chain; surface the error only if everything
                # fails). This is more aggressive than the old 2-tuple impl
                # which raised immediately on non-failover codes from the
                # primary. Justified: with a 4-link chain, more aggressive
                # failover is closer to user intent. If this causes noise,
                # tighten back to `raise` here.
                continue
        except Exception as e:
            _log(f"chain[{i}] {provider}/{model} failed: {e}")
            last_err = e
    raise RuntimeError(f"all {len(chain)} links in tier={tier} failed; last error: {last_err}")


if __name__ == "__main__":
    text, provider = call(
        "Reply with exactly the word PONG. No other text.",
        "ping",
        tier="mini",
        max_tokens=20,
    )
    print(f"reply: {text!r}  provider: {provider}")
