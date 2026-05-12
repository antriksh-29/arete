#!/usr/bin/env python3
"""Reusable cookie-injection helper for browser-harness.

Stores per-domain cookie blobs in the bot-cred vault under
`SITE_COOKIES__<domain>` (domain normalized to its registrable form).
Future skills that need an authenticated browser context call:

    python3 ~/bin/browser-login.py inject <domain>

which loads the vault blob, injects every cookie via CDP
`Network.setCookie`, and returns a JSON envelope on stdout.

Subcommands:
    list                List domains with stored cookies + expiry.
    inject <domain>     Inject vault cookies into the running headless
                        Chrome. Use BEFORE the first new_tab() call
                        in any browser-harness script for that domain.
    remove <domain>     Delete the vault entry for a domain.
    test <domain> [url] Inject + navigate + return the first 400 chars
                        of body text + the page title for agent
                        verification.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ["PATH"] = "/home/botuser/.bun/bin:" + os.environ.get("PATH", "")

VAULT_PREFIX = "SITE_COOKIES__"


def log(msg):
    print(f"[browser-login] {msg}", file=sys.stderr)


def vault_get(key):
    r = subprocess.run(["bot-cred", "get", key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def vault_set(key, value):
    r = subprocess.run(["bot-cred", "set", key, value], capture_output=True, text=True)
    return r.returncode == 0


def vault_list():
    r = subprocess.run(["bot-cred", "list"], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip().startswith(VAULT_PREFIX)]


def vault_remove(key):
    r = subprocess.run(["bot-cred", "remove", key], capture_output=True, text=True)
    return r.returncode == 0


def normalize_domain(domain):
    """Strip protocol/path; lowercase; remove `www.`. Keep the rest of
    the host intact (so `platform.openai.com` stays distinct from
    `api.openai.com` — they have different cookie scopes)."""
    d = domain.strip().lower()
    if d.startswith("http://"): d = d[7:]
    if d.startswith("https://"): d = d[8:]
    d = d.split("/", 1)[0]
    d = re.sub(r"^www\.", "", d)
    return d


def vault_key(domain):
    safe = re.sub(r"[^a-z0-9.-]", "_", normalize_domain(domain))
    return f"{VAULT_PREFIX}{safe.replace('.', '_')}"


def parse_cookies(blob, domain_hint=""):
    """Accept four shapes:
    - EditThisCookie / DevTools 'Copy as JSON': list of dicts with
      `name`, `value`, `domain`, `path`, `secure`, `httpOnly`,
      `sameSite`, `expirationDate` (or `expires`).
    - Netscape cookies.txt: `# domain TAB flag TAB path TAB secure TAB expiry TAB name TAB value` lines.
    - Flat dict {name: value} — scoped to domain_hint.
    - HTTP `Cookie:` request header — `name=value; name=value; ...`.
      Captured by copying the cookie line from DevTools → Network tab →
      Headers → Request Headers. This is the easiest non-extension path
      and includes httpOnly cookies (because the BROWSER sends them;
      only JS can't read them).

    domain_hint: when format doesn't carry domain info (flat map,
    Cookie header), attach this domain to every cookie. Caller should
    pass the normalized target domain (e.g. 'linkedin.com').

    Returns a list of CDP-shaped cookie dicts ready for Network.setCookie."""
    cookies = []
    text = blob.strip()
    # Strip a leading 'Cookie:' / 'cookie:' label if present (common
    # when the user copies the whole header line from DevTools).
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    # Try JSON first.
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for c in data:
                if not isinstance(c, dict):
                    continue
                ck = _from_dict(c)
                if ck:
                    if "domain" not in ck and domain_hint:
                        ck["domain"] = "." + domain_hint
                    cookies.append(ck)
            return cookies
        if isinstance(data, dict):
            # Could be a {name: value} map or a single cookie object
            if "name" in data and "value" in data:
                ck = _from_dict(data)
                if ck:
                    if "domain" not in ck and domain_hint:
                        ck["domain"] = "." + domain_hint
                    return [ck]
            for name, val in data.items():
                ck = {"name": name, "value": str(val), "path": "/"}
                if domain_hint:
                    ck["domain"] = "." + domain_hint
                cookies.append(ck)
            return cookies
    except json.JSONDecodeError:
        pass
    # Try Netscape cookies.txt
    netscape_hits = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) >= 7:
            domain, _flag, path, secure, expiry, name, value = parts[:7]
            ck = {"name": name, "value": value, "domain": domain, "path": path or "/", "secure": secure.upper() == "TRUE"}
            try:
                if expiry and int(expiry) > 0:
                    ck["expires"] = int(expiry)
            except ValueError:
                pass
            cookies.append(ck)
            netscape_hits += 1
    if netscape_hits:
        return cookies
    # Last resort: HTTP Cookie header format `name=value; name=value`.
    # No structural fields — caller must supply domain_hint.
    if ";" in text or "=" in text:
        for raw in text.replace("\n", " ").split(";"):
            kv = raw.strip()
            if not kv or "=" not in kv:
                continue
            name, _, value = kv.partition("=")
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            ck = {"name": name, "value": value, "path": "/"}
            if domain_hint:
                ck["domain"] = "." + domain_hint
            cookies.append(ck)
    return cookies


def _from_dict(c):
    if "name" not in c or "value" not in c:
        return None
    out = {"name": c["name"], "value": str(c["value"])}
    for k_src, k_dst in (("domain", "domain"), ("path", "path"), ("secure", "secure"),
                         ("httpOnly", "httpOnly"), ("sameSite", "sameSite")):
        if k_src in c and c[k_src] is not None:
            out[k_dst] = c[k_src]
    # ExpirationDate is the EditThisCookie convention (float, seconds)
    for k in ("expirationDate", "expires", "expiry"):
        if k in c and c[k] not in (None, "", 0):
            try:
                out["expires"] = float(c[k])
            except (TypeError, ValueError):
                pass
            break
    # CDP wants `sameSite` as Strict|Lax|None title-case
    if "sameSite" in out and isinstance(out["sameSite"], str):
        v = out["sameSite"].lower()
        if v in ("strict", "lax", "none"):
            out["sameSite"] = v.capitalize()
        elif v in ("no_restriction", "unspecified"):
            out["sameSite"] = "None"
        else:
            del out["sameSite"]
    return out


def find_bh():
    p = shutil.which("browser-harness") or os.path.expanduser("~/.local/bin/browser-harness")
    return p if os.path.isfile(p) else None


def inject_cookies(domain, cookies):
    """Run browser-harness with a CDP setCookie per cookie. Returns
    (ok, count_set, errors)."""
    bh = find_bh()
    if not bh:
        return False, 0, "browser-harness CLI not found"
    if not cookies:
        return False, 0, "no cookies to inject"
    # Build a python snippet that calls cdp Network.setCookie for each.
    # Pre-serialize once; the script just iterates.
    payload = json.dumps(cookies)
    script = (
        "import json\n"
        f"cookies = json.loads({payload!r})\n"
        "ok = 0\n"
        "errs = []\n"
        "for c in cookies:\n"
        "    try:\n"
        "        r = cdp('Network.setCookie', **c)\n"
        "        if r.get('success', True):\n"
        "            ok += 1\n"
        "        else:\n"
        "            errs.append(c.get('name'))\n"
        "    except Exception as e:\n"
        "        errs.append(f\"{c.get('name')}: {e}\")\n"
        "print('---BL-OK---'); print(ok)\n"
        "print('---BL-ERRS---'); print(json.dumps(errs[:20]))\n"
    )
    env = {**os.environ, "BU_CDP_URL": "http://127.0.0.1:9222"}
    r = subprocess.run([bh], input=script, capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0:
        return False, 0, r.stderr[-300:]
    out = r.stdout
    try:
        ok_n = int(out.split("---BL-OK---", 1)[1].split("---BL-ERRS---", 1)[0].strip())
    except (IndexError, ValueError):
        ok_n = 0
    try:
        errs = json.loads(out.split("---BL-ERRS---", 1)[1].strip())
    except (IndexError, ValueError, json.JSONDecodeError):
        errs = []
    return ok_n > 0, ok_n, errs


def navigate_and_capture(url, timeout_s=45):
    """Open URL in headless Chrome, return (title, text, final_url)."""
    bh = find_bh()
    if not bh:
        return "", "", url
    script = (
        "import time, json\n"
        f"new_tab({url!r})\n"
        "wait_for_load()\n"
        "time.sleep(2)\n"
        "info = page_info()\n"
        "text = cdp('Runtime.evaluate', expression='document.body.innerText', returnByValue=True).get('result',{}).get('value','')\n"
        "print('---BL-INFO---'); print(json.dumps(info)[:4000])\n"
        "print('---BL-TEXT---'); print((text or '')[:4000])\n"
        "print('---BL-END---')\n"
    )
    env = {**os.environ, "BU_CDP_URL": "http://127.0.0.1:9222"}
    r = subprocess.run([bh], input=script, capture_output=True, text=True, timeout=timeout_s, env=env)
    if r.returncode != 0:
        log(f"navigate failed: {r.stderr[-200:]}")
        return "", "", url
    out = r.stdout
    info_raw = out.split("---BL-INFO---", 1)[-1].split("---BL-TEXT---", 1)[0].strip() if "---BL-INFO---" in out else ""
    text_raw = out.split("---BL-TEXT---", 1)[-1].split("---BL-END---", 1)[0].strip() if "---BL-TEXT---" in out else ""
    try:
        info = json.loads(info_raw) if info_raw else {}
    except json.JSONDecodeError:
        info = {}
    return info.get("title", ""), text_raw, info.get("url", url)


# ----- subcommands -----

def cmd_store(domain, blob):
    domain = normalize_domain(domain)
    cookies = parse_cookies(blob, domain_hint=domain)
    if not cookies:
        print(json.dumps({"ok": False, "reason": "parse_failed",
                          "user_message": "🔒 Couldn't parse any cookies from that blob. Send a JSON array of cookie objects or a Netscape cookies.txt block."}))
        return 1
    key = vault_key(domain)
    if not vault_set(key, json.dumps(cookies)):
        print(json.dumps({"ok": False, "reason": "vault_write_failed",
                          "user_message": "🔒 Failed to write cookies to vault."}))
        return 1
    # Compute the earliest expiry for a friendly user message
    expires = [c.get("expires") for c in cookies if c.get("expires")]
    soonest_iso = ""
    if expires:
        try:
            t = min(float(e) for e in expires)
            soonest_iso = datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
        except Exception:
            pass
    msg = f"🔒 Stored {len(cookies)} cookies for {domain}."
    if soonest_iso:
        msg += f"\nEarliest expiry: {soonest_iso}."
    print(json.dumps({"ok": True, "domain": domain, "count": len(cookies),
                      "soonest_expiry": soonest_iso, "user_message": msg}))
    return 0


def cmd_test(domain, probe_url):
    domain = normalize_domain(domain)
    raw = vault_get(vault_key(domain))
    if not raw:
        print(json.dumps({"ok": False, "reason": "no_cookies",
                          "user_message": f"🔒 No cookies stored for {domain}. Run `/site-login {domain}` first."}))
        return 1
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "reason": "vault_corrupt",
                          "user_message": "🔒 Stored cookies are not valid JSON. Re-import."}))
        return 1
    ok, set_count, errs = inject_cookies(domain, cookies)
    if not ok:
        print(json.dumps({"ok": False, "reason": "inject_failed", "errors": errs,
                          "user_message": f"🔒 Could not inject cookies: {errs}"}))
        return 1
    title, text, final_url = navigate_and_capture(probe_url or f"https://{domain}/")
    looks_logged_in = bool(text) and "log in" not in (text[:200].lower())
    snippet = text[:400].replace("\n", " ")
    msg_lines = [
        f"🔒 Injected {set_count} cookies, probed {final_url}",
        f"Title: {title}",
        f"First 400 chars: {snippet}",
        f"Looks logged in: {looks_logged_in}",
    ]
    print(json.dumps({"ok": True, "domain": domain, "set_count": set_count,
                      "title": title, "snippet": snippet,
                      "looks_logged_in": looks_logged_in,
                      "user_message": "\n".join(msg_lines)}))
    return 0


def cmd_inject(domain):
    """Used by other workflow scripts before they navigate. Returns
    JSON envelope; no probe URL."""
    domain = normalize_domain(domain)
    raw = vault_get(vault_key(domain))
    if not raw:
        print(json.dumps({"ok": False, "reason": "no_cookies"}))
        return 1
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "reason": "vault_corrupt"}))
        return 1
    ok, n, errs = inject_cookies(domain, cookies)
    print(json.dumps({"ok": ok, "domain": domain, "set_count": n, "errors": errs}))
    return 0 if ok else 1


def cmd_list():
    keys = vault_list()
    domains = [k[len(VAULT_PREFIX):].replace("_", ".") for k in keys]
    if not domains:
        print(json.dumps({"ok": True, "domains": [], "user_message": "🔒 No site cookies stored yet."}))
        return 0
    print(json.dumps({"ok": True, "domains": domains,
                      "user_message": "🔒 Site cookies stored for:\n" + "\n".join(f"• {d}" for d in domains)}))
    return 0


def cmd_remove(domain):
    domain = normalize_domain(domain)
    key = vault_key(domain)
    if not vault_get(key):
        print(json.dumps({"ok": False, "reason": "no_cookies",
                          "user_message": f"🔒 No cookies for {domain} to remove."}))
        return 1
    if not vault_remove(key):
        print(json.dumps({"ok": False, "reason": "vault_remove_failed",
                          "user_message": f"🔒 Failed to remove cookies for {domain}."}))
        return 1
    print(json.dumps({"ok": True, "user_message": f"🗑️ Removed cookies for {domain}."}))
    return 0


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: browser-login.py {store|test|inject|list|remove} ...")
    cmd = sys.argv[1]
    if cmd == "--smoke":
        print(json.dumps({"ok": True, "skill": "site-login", "user_message": "site-login smoke ok"}))
        return
    if cmd == "store":
        if len(sys.argv) < 4:
            sys.exit("Usage: browser-login.py store <domain> <path-to-json-blob>")
        domain = sys.argv[2]
        blob_path = sys.argv[3]
        if not os.path.isfile(blob_path):
            sys.exit(f"ABORT: cookie file not found: {blob_path}")
        blob = Path(blob_path).read_text()
        sys.exit(cmd_store(domain, blob))
    if cmd == "test":
        if len(sys.argv) < 3:
            sys.exit("Usage: browser-login.py test <domain> [<probe-url>]")
        domain = sys.argv[2]
        probe = sys.argv[3] if len(sys.argv) > 3 else None
        sys.exit(cmd_test(domain, probe))
    if cmd == "inject":
        if len(sys.argv) < 3:
            sys.exit("Usage: browser-login.py inject <domain>")
        sys.exit(cmd_inject(sys.argv[2]))
    if cmd == "list":
        sys.exit(cmd_list())
    if cmd == "remove":
        if len(sys.argv) < 3:
            sys.exit("Usage: browser-login.py remove <domain>")
        sys.exit(cmd_remove(sys.argv[2]))
    sys.exit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e),
                          "user_message": "⚠️ site-login operation failed — check the logs."}))
        sys.exit(1)
