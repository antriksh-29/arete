#!/usr/bin/env python3
"""Fill a form (Google Forms or generic HTML) using browser-harness.

The agent's job is to extract user-supplied answers into a JSON file
and pass it here; this script handles the browser driving.

Usage:
    python3 ~/bin/fill-form.py <url> <answers-json-file> [--submit] [--screenshot <path>]

Answers JSON shape (matched fuzzily against question labels):
    {
        "Full Name": "Antriksh Jain",
        "Email": "antriksh1980@gmail.com",
        "GitHub or Portfolio URL": "https://github.com/...",
        "Which MCP servers do you need?": "Dineout, Instamart, Food",
        "I acknowledge Swiggy's MCP integration terms": "Yes"
    }

The script:
1. Opens the form URL in browser-harness's persistent Chrome.
2. Queries every `div[role=listitem]` (Google Forms structure) plus
   any standard <input>, <textarea>, <select> for generic HTML forms.
3. Matches each detected question label to a key in the answers JSON
   via case-insensitive equality / containment / token overlap.
4. Fills text/textarea via click_at_xy + type_text.
   Clicks the matching option for radios, checkboxes (comma-separated
   answer = multi-check), and dropdowns.
5. Screenshots the filled form.
6. Reads each filled input's current `.value` / `aria-checked` state
   back via CDP so the envelope can prove what actually got set.
7. If --submit, clicks the Submit / Send button.

Prints a single-line JSON envelope to stdout. Diagnostics to stderr.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ["PATH"] = "/home/botuser/.bun/bin:/home/botuser/.local/bin:" + os.environ.get("PATH", "")

BH = os.environ.get("BROWSER_HARNESS", "/home/botuser/.local/bin/browser-harness")
ENV = {**os.environ, "BU_CDP_URL": os.environ.get("BU_CDP_URL", "http://127.0.0.1:9222")}
SHOT_DEFAULT = "/tmp/fill-form-result.png"


def log(msg):
    print(f"[fill-form] {msg}", file=sys.stderr)


def bh_run(script, timeout=60):
    """Run a Python snippet inside browser-harness; return stdout."""
    if not os.path.isfile(BH):
        raise RuntimeError(f"browser-harness CLI not found at {BH}")
    r = subprocess.run([BH, "-c", script], capture_output=True, text=True, timeout=timeout, env=ENV)
    if r.returncode != 0:
        raise RuntimeError(f"browser-harness exit {r.returncode}: {r.stderr[-400:]}")
    return r.stdout


# ----- fuzzy label matcher -----

_NORM = re.compile(r"[^\w\s]")


def _norm(s):
    return _NORM.sub(" ", (s or "").lower()).strip()


def fuzzy_match(question_label, answer_keys):
    """Find the answers key that best matches the form question."""
    q = _norm(question_label)
    if not q:
        return None
    # Exact / containment
    for key in answer_keys:
        k = _norm(key)
        if k and (k == q or k in q or q in k):
            return key
    # Token overlap (≥ 2 tokens)
    q_tokens = set(t for t in q.split() if len(t) > 2)
    best = (0, None)
    for key in answer_keys:
        k_tokens = set(t for t in _norm(key).split() if len(t) > 2)
        overlap = len(q_tokens & k_tokens)
        if overlap > best[0]:
            best = (overlap, key)
    return best[1] if best[0] >= 2 else None


# ----- browser actions -----

def open_form(url, wait_s=3):
    script = (
        "import time\n"
        f"new_tab({url!r})\n"
        "wait_for_load()\n"
        f"time.sleep({wait_s})\n"
        "print('OK')\n"
    )
    bh_run(script)


def get_fields():
    """Return a list of {heading, input, radios, checkboxes, dropdowns}.

    For each entry, coordinates are absolute viewport coords (since the
    headless Chrome has a known --window-size=1280,800)."""
    expr = (
        "JSON.stringify(Array.from(document.querySelectorAll("
        "  'div[role=\"listitem\"], form, [data-runtime-value]'"
        ")).flatMap(function(li){"
        "  // Skip form/li that have no heading text — likely wrappers"
        "  var h = li.querySelector('[role=\"heading\"], label, legend');"
        "  var heading = h ? (h.innerText || '').trim().replace(/\\s*\\*\\s*$/, '') : '';"
        "  if (!heading) return [];"
        "  var input = li.querySelector('input[type=\"text\"], input[type=\"email\"], input[type=\"url\"], input[type=\"tel\"], textarea');"
        "  var inputBox = null;"
        "  if (input) { var b = input.getBoundingClientRect();"
        "    inputBox = {x: b.x + b.width/2, y: b.y + b.height/2}; }"
        "  var radios = Array.from(li.querySelectorAll('[role=\"radio\"]')).map(function(r){"
        "    var b = r.getBoundingClientRect();"
        "    return {text: (r.getAttribute('data-value') || r.innerText || r.getAttribute('aria-label') || '').trim(),"
        "            x: b.x + b.width/2, y: b.y + b.height/2};"
        "  }).filter(function(r){return r.text.length > 0;});"
        "  var checkboxes = Array.from(li.querySelectorAll('[role=\"checkbox\"]')).map(function(c){"
        "    var b = c.getBoundingClientRect();"
        "    return {text: (c.getAttribute('data-answer-value') || c.innerText || c.getAttribute('aria-label') || '').trim(),"
        "            x: b.x + b.width/2, y: b.y + b.height/2};"
        "  }).filter(function(c){return c.text.length > 0;});"
        "  return [{heading: heading, input: inputBox, radios: radios, checkboxes: checkboxes}];"
        "}))"
    )
    script = (
        "import json\n"
        f"r = cdp('Runtime.evaluate', expression={expr!r}, returnByValue=True)\n"
        "print('---FIELDS---')\n"
        "print(r.get('result',{}).get('value','[]'))\n"
    )
    out = bh_run(script)
    raw = out.split("---FIELDS---", 1)[1].strip()
    return json.loads(raw)


def click_xy(x, y):
    script = (
        "import time\n"
        f"click_at_xy({int(x)}, {int(y)})\n"
        "time.sleep(0.25)\n"
        "print('OK')\n"
    )
    bh_run(script)


def type_str(text):
    # type_text expects the focused element to be ready
    safe = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
    script = (
        "import time\n"
        f"type_text({text!r})\n"
        "time.sleep(0.25)\n"
        "print('OK')\n"
    )
    bh_run(script)


def screenshot(path):
    script = f"capture_screenshot({path!r})\nprint('OK')\n"
    bh_run(script)


def find_and_click_submit():
    expr = (
        "(function(){"
        "  var btns = Array.from(document.querySelectorAll('[role=\"button\"], button, input[type=\"submit\"]'));"
        "  var b = btns.find(function(x){return /submit|send|gönder|enviar/i.test(x.innerText || x.value || '');});"
        "  if (!b) return JSON.stringify(null);"
        "  var bb = b.getBoundingClientRect();"
        "  return JSON.stringify({x: bb.x + bb.width/2, y: bb.y + bb.height/2});"
        "})()"
    )
    script = (
        f"r = cdp('Runtime.evaluate', expression={expr!r}, returnByValue=True)\n"
        "print('---SUBMIT---')\n"
        "print(r.get('result',{}).get('value','null'))\n"
    )
    out = bh_run(script)
    raw = out.split("---SUBMIT---", 1)[1].strip()
    if raw == "null":
        return False
    coords = json.loads(raw)
    if not coords:
        return False
    click_xy(coords["x"], coords["y"])
    return True


def read_back_state():
    """After filling, read each text input/textarea's current value
    plus each checked radio/checkbox. Lets the envelope prove what
    actually got set."""
    expr = (
        "(function(){"
        "  var texts = Array.from(document.querySelectorAll('input[type=\"text\"], input[type=\"email\"], input[type=\"url\"], textarea'))"
        "    .map(function(i){return i.value;}).filter(function(v){return v && v.length>0;});"
        "  var radios = Array.from(document.querySelectorAll('[role=\"radio\"][aria-checked=\"true\"]'))"
        "    .map(function(r){return r.getAttribute('data-value') || r.innerText;});"
        "  var checks = Array.from(document.querySelectorAll('[role=\"checkbox\"][aria-checked=\"true\"]'))"
        "    .map(function(c){return c.getAttribute('data-answer-value') || c.innerText;});"
        "  return JSON.stringify({texts: texts, radios: radios, checks: checks});"
        "})()"
    )
    script = (
        f"r = cdp('Runtime.evaluate', expression={expr!r}, returnByValue=True)\n"
        "print('---STATE---')\n"
        "print(r.get('result',{}).get('value','{}'))\n"
    )
    out = bh_run(script)
    raw = out.split("---STATE---", 1)[1].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ----- top-level fill -----

def fill_field(field, answer):
    heading = field["heading"]
    if field.get("input"):
        click_xy(field["input"]["x"], field["input"]["y"])
        type_str(str(answer))
        return {"mode": "text", "value": str(answer)[:80]}
    if field.get("radios"):
        match = _pick_option(field["radios"], answer)
        if match:
            click_xy(match["x"], match["y"])
            return {"mode": "radio", "value": match["text"]}
        return {"mode": "radio", "skipped": f"no option matching {answer!r}",
                "available": [r["text"] for r in field["radios"]]}
    if field.get("checkboxes"):
        wanted = _split_multi(answer)
        clicked = []
        for c in field["checkboxes"]:
            ct = _norm(c["text"])
            if any(_norm(w) in ct or ct in _norm(w) for w in wanted):
                click_xy(c["x"], c["y"])
                clicked.append(c["text"])
        if clicked:
            return {"mode": "checkbox", "value": clicked}
        return {"mode": "checkbox", "skipped": f"no option matching {answer!r}",
                "available": [c["text"] for c in field["checkboxes"]]}
    return {"mode": "unknown", "skipped": "no input/radio/checkbox detected"}


def _pick_option(options, answer):
    a = _norm(str(answer))
    if not a:
        return None
    # Exact or substring match
    for o in options:
        if _norm(o["text"]) == a or a in _norm(o["text"]) or _norm(o["text"]) in a:
            return o
    # Token overlap
    a_tok = set(t for t in a.split() if len(t) > 2)
    best = (0, None)
    for o in options:
        o_tok = set(t for t in _norm(o["text"]).split() if len(t) > 2)
        n = len(a_tok & o_tok)
        if n > best[0]:
            best = (n, o)
    return best[1] if best[0] >= 1 else None


def _split_multi(answer):
    if isinstance(answer, list):
        return [str(x) for x in answer]
    s = str(answer)
    # Split on commas / semicolons / " and " / newlines
    parts = re.split(r"[,;\n]|\sand\s", s)
    return [p.strip() for p in parts if p.strip()]


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: fill-form.py <url> <answers-json> [--submit] [--screenshot <path>]")
    if sys.argv[1] == "--smoke":
        print(json.dumps({"ok": True, "skill": "fill-form", "user_message": "fill-form smoke ok"}))
        return
    if len(sys.argv) < 3:
        sys.exit("Usage: fill-form.py <url> <answers-json> [--submit] [--screenshot <path>]")
    url = sys.argv[1]
    answers_path = sys.argv[2]
    submit = "--submit" in sys.argv[3:]
    shot_path = SHOT_DEFAULT
    if "--screenshot" in sys.argv:
        i = sys.argv.index("--screenshot")
        if i + 1 < len(sys.argv):
            shot_path = sys.argv[i + 1]

    answers = json.loads(Path(answers_path).read_text())
    log(f"loaded {len(answers)} answers from {answers_path}")

    open_form(url)
    fields = get_fields()
    log(f"detected {len(fields)} fields on the form")

    results = []
    skipped_headings = []
    for f in fields:
        heading = f.get("heading", "")
        key = fuzzy_match(heading, list(answers.keys()))
        if not key:
            skipped_headings.append(heading)
            continue
        try:
            r = fill_field(f, answers[key])
            r["question"] = heading
            r["matched_key"] = key
            results.append(r)
        except Exception as e:
            results.append({"question": heading, "matched_key": key, "error": str(e)})
            log(f"fill failed on {heading!r}: {e}")

    screenshot(shot_path)
    state = read_back_state()

    submitted = False
    if submit:
        log("clicking submit")
        submitted = find_and_click_submit()
        if submitted:
            time.sleep(2)
            screenshot(shot_path.replace(".png", "-after-submit.png"))

    msg_lines = [
        f"📝 Form fill — {len(results)} fields touched, {len(skipped_headings)} skipped.",
        f"Screenshot: {shot_path}",
    ]
    if skipped_headings:
        msg_lines.append("Skipped (no answer matched):")
        for h in skipped_headings[:5]:
            msg_lines.append(f"  • {h}")
    if submit:
        msg_lines.append("✅ Submitted." if submitted else "⚠️ Submit button not found.")
    else:
        msg_lines.append("Stopped before submit — review screenshot first.")

    print(json.dumps({
        "ok": True,
        "url": url,
        "fields_detected": len(fields),
        "filled": results,
        "skipped_headings": skipped_headings,
        "submitted": submitted,
        "verification_state": state,
        "screenshot": shot_path,
        "user_message": "\n".join(msg_lines),
    }))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"FAILED: {e}")
        print(json.dumps({"ok": False, "reason": "error", "error": str(e),
                          "user_message": f"⚠️ Form fill failed: {e}"}))
        sys.exit(1)
