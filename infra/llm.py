#!/usr/bin/env python3
# Shared LLM caller with cross-provider failover.
#
# Policy (per identity/MODEL_ROUTING.md):
#   tier="mini"   → openai/gpt-5.4-mini   → fallback: anthropic/claude-haiku-4-5
#   tier="full"   → openai/gpt-5.4         → fallback: anthropic/claude-sonnet-4-6
#   tier="nano"   → openai/gpt-5.4-nano    → fallback: anthropic/claude-haiku-4-5
#   tier="vision" → openai/gpt-5.4-mini (vision) → fallback: openai/gpt-5.4 (vision)
#
# Failover triggers on:
#   - HTTP 401/403/429 (auth, billing, rate-limit)
#   - HTTP 5xx (provider outage)
#   - Network error
#
# Anthropic's Messages API and OpenAI's Responses API have different shapes;
# this module hides the difference. Callers pass system_prompt + user_msg
# and get back text.

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")


_PROVIDER_MAP = {
    "mini":   ("openai", "gpt-5.4-mini",   "anthropic", "claude-haiku-4-5"),
    "full":   ("openai", "gpt-5.4",         "anthropic", "claude-sonnet-4-6"),
    "nano":   ("openai", "gpt-5.4-nano",    "anthropic", "claude-haiku-4-5"),
    "vision": ("openai", "gpt-5.4-mini",    "openai",    "gpt-5.4"),
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


def call(system_prompt, user_msg, tier="mini", max_tokens=4000, tools=None, image_url=None, image_base64=None):
    """Call an LLM with primary provider + automatic fallback.

    Returns (text, provider_used) where provider_used is "openai" or "anthropic".
    Raises if BOTH providers fail.
    """
    if tier not in _PROVIDER_MAP:
        raise ValueError(f"unknown tier: {tier}")
    primary_provider, primary_model, fallback_provider, fallback_model = _PROVIDER_MAP[tier]

    if tools and primary_provider != "openai":
        _log(f"tools requested but primary is {primary_provider}; skipping tools on fallback")

    try:
        if primary_provider == "openai":
            text = _call_openai(primary_model, system_prompt, user_msg, max_tokens,
                                tools=tools, image_url=image_url, image_base64=image_base64)
        else:
            text = _call_anthropic(primary_model, system_prompt, user_msg, max_tokens,
                                    image_url=image_url, image_base64=image_base64)
        return text, primary_provider
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        _log(f"primary {primary_provider}/{primary_model} failed HTTP {e.code}: {body}")
        if e.code not in _FALLBACK_HTTP:
            raise
    except Exception as e:
        _log(f"primary {primary_provider}/{primary_model} failed: {e}")

    _log(f"falling back to {fallback_provider}/{fallback_model}")
    try:
        if fallback_provider == "openai":
            tools_for_fallback = tools if primary_provider == "openai" else None
            text = _call_openai(fallback_model, system_prompt, user_msg, max_tokens,
                                tools=tools_for_fallback, image_url=image_url, image_base64=image_base64)
        else:
            text = _call_anthropic(fallback_model, system_prompt, user_msg, max_tokens,
                                    image_url=image_url, image_base64=image_base64)
        return text, fallback_provider
    except Exception as e:
        _log(f"fallback {fallback_provider}/{fallback_model} also failed: {e}")
        raise RuntimeError(f"both primary and fallback failed: {e}")


if __name__ == "__main__":
    text, provider = call(
        "Reply with exactly the word PONG. No other text.",
        "ping",
        tier="mini",
        max_tokens=20,
    )
    print(f"reply: {text!r}  provider: {provider}")
