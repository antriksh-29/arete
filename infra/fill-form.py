#!/usr/bin/env python3
"""Fill a form (Google Forms or generic HTML) using browser-harness.

Architecture: ONE browser-harness invocation drives the entire flow
(navigate → query fields → match answers → fill → verify → screenshot
→ optional submit). Splitting across multiple `bh -c` calls loses
tab focus between calls because each invocation is a fresh Python
process attaching to the daemon.

Usage:
    python3 ~/bin/fill-form.py <url> <answers-json-file> [--submit] [--screenshot <path>]

Answers JSON shape (keys matched fuzzily against question labels):
    {"Full Name": "Antriksh Jain", "Email": "...", ...}
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

os.environ["PATH"] = "/home/botuser/.bun/bin:/home/botuser/.local/bin:" + os.environ.get("PATH", "")

BH = os.environ.get("BROWSER_HARNESS", "/home/botuser/.local/bin/browser-harness")
ENV = {**os.environ, "BU_CDP_URL": os.environ.get("BU_CDP_URL", "http://127.0.0.1:9222")}
SHOT_DEFAULT = "/tmp/fill-form-result.png"


def log(msg):
    print(f"[fill-form] {msg}", file=sys.stderr)


# ── fuzzy label matcher (runs in the OUTER Python, not the bh script) ──

_NORM = re.compile(r"[^\w\s]")


def _norm(s):
    return _NORM.sub(" ", (s or "").lower()).strip()


def build_match_map(answers, headings):
    """Return {heading: answer} for headings that match a key fuzzily."""
    out = {}
    keys = list(answers.keys())
    for heading in headings:
        h = _norm(heading)
        if not h:
            continue
        # Exact / containment first
        chosen = None
        for key in keys:
            k = _norm(key)
            if k and (k == h or k in h or h in k):
                chosen = key
                break
        if chosen is None:
            # Token overlap
            h_tok = set(t for t in h.split() if len(t) > 2)
            best = (0, None)
            for key in keys:
                k_tok = set(t for t in _norm(key).split() if len(t) > 2)
                n = len(h_tok & k_tok)
                if n > best[0]:
                    best = (n, key)
            if best[0] >= 2:
                chosen = best[1]
        if chosen is not None:
            out[heading] = answers[chosen]
    return out


def _pick_option(options, answer):
    """Find which option matches the user's answer. Used inside bh script
    for radios/dropdowns. Reimplemented here only for option logging."""
    a = _norm(str(answer))
    if not a:
        return None
    for o in options:
        ot = _norm(o)
        if ot == a or a in ot or ot in a:
            return o
    a_tok = set(t for t in a.split() if len(t) > 2)
    best = (0, None)
    for o in options:
        o_tok = set(t for t in _norm(o).split() if len(t) > 2)
        n = len(a_tok & o_tok)
        if n > best[0]:
            best = (n, o)
    return best[1] if best[0] >= 1 else None


def _split_multi(answer):
    if isinstance(answer, list):
        return [str(x) for x in answer]
    parts = re.split(r"[,;\n]|\sand\s", str(answer))
    return [p.strip() for p in parts if p.strip()]


# ── browser-harness driver ────────────────────────────────────────

def bh_run(script, timeout=180):
    if not os.path.isfile(BH):
        raise RuntimeError(f"browser-harness CLI not found at {BH}")
    r = subprocess.run([BH, "-c", script], capture_output=True, text=True, timeout=timeout, env=ENV)
    if r.returncode != 0:
        log(f"bh stderr: {r.stderr[-500:]}")
        raise RuntimeError(f"browser-harness exit {r.returncode}: {r.stderr[-200:]}")
    return r.stdout


# JS expression that walks the form and returns a JSON array of
# {heading, input_box: {x,y}|null, radios: [{text,x,y}], checkboxes: [...]}.
FIELDS_JS = r"""
(function(){
  function bb(el){ var r = el.getBoundingClientRect(); return {x: r.x + r.width/2, y: r.y + r.height/2}; }
  function getHeading(li){
    var h = li.querySelector('[role="heading"], legend');
    if (h && (h.innerText||'').trim()) return (h.innerText||'').trim().replace(/\s*\*\s*$/, '');
    // Fallback: aria-labelledby chain
    var input = li.querySelector('input, textarea');
    if (input) {
      var ids = (input.getAttribute('aria-labelledby')||'').split(/\s+/);
      for (var i=0;i<ids.length;i++){
        var lab = document.getElementById(ids[i]);
        if (lab && (lab.innerText||'').trim()) return (lab.innerText||'').trim();
      }
    }
    return '';
  }
  var fields = [];
  // Google Forms primary structure: each question is a div[role=listitem]
  var items = Array.from(document.querySelectorAll('div[role="listitem"]'));
  items.forEach(function(li){
    var heading = getHeading(li);
    if (!heading) return;
    var input = li.querySelector('input[type="text"], input[type="email"], input[type="url"], input[type="tel"], textarea');
    var inputBox = input ? bb(input) : null;
    var radios = Array.from(li.querySelectorAll('[role="radio"]')).map(function(r){
      var t = (r.getAttribute('data-value') || r.getAttribute('aria-label') || r.innerText || '').trim();
      return {text: t, x: bb(r).x, y: bb(r).y};
    }).filter(function(r){return r.text.length > 0;});
    var checkboxes = Array.from(li.querySelectorAll('[role="checkbox"]')).map(function(c){
      var t = (c.getAttribute('data-answer-value') || c.getAttribute('aria-label') || c.innerText || '').trim();
      return {text: t, x: bb(c).x, y: bb(c).y};
    }).filter(function(c){return c.text.length > 0;});
    fields.push({heading: heading, input: inputBox, radios: radios, checkboxes: checkboxes});
  });
  return JSON.stringify(fields);
})()
"""

SUBMIT_JS = r"""
(function(){
  var btns = Array.from(document.querySelectorAll('[role="button"], button, input[type="submit"]'));
  var b = btns.find(function(x){ return /^(submit|send|gönder|enviar)\b/i.test(((x.innerText||x.value||'').trim())); });
  if (!b) return JSON.stringify(null);
  var r = b.getBoundingClientRect();
  return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
})()
"""

VERIFY_JS = r"""
(function(){
  var texts = Array.from(document.querySelectorAll('input[type="text"], input[type="email"], input[type="url"], textarea'))
    .map(function(i){ return i.value; }).filter(function(v){ return v && v.length > 0; });
  var radios = Array.from(document.querySelectorAll('[role="radio"][aria-checked="true"]'))
    .map(function(r){ return (r.getAttribute('data-value') || r.innerText || '').trim(); });
  var checks = Array.from(document.querySelectorAll('[role="checkbox"][aria-checked="true"]'))
    .map(function(c){ return (c.getAttribute('data-answer-value') || c.innerText || '').trim(); });
  return JSON.stringify({texts: texts, radios: radios, checks: checks});
})()
"""


def build_driver_script(url, fill_plan, submit, shot_path):
    """Build the single browser-harness script that:
       1. new_tab + wait
       2. CDP get fields (writes ---FIELDS--- marker so outer Python could
          re-plan if needed — here we already have a fill_plan)
       3. For each plan step, click coords + type / click option
       4. CDP read back state
       5. screenshot
       6. optional submit
       7. print final envelope JSON between ---FINAL--- markers
    """
    plan_json = json.dumps(fill_plan)
    return f"""
import json, time
new_tab({url!r})
wait_for_load()
time.sleep(4)

# Re-query fields fresh in this same session so coordinates are current.
fields_js = {FIELDS_JS!r}
r = cdp("Runtime.evaluate", expression=fields_js, returnByValue=True)
fields_raw = r.get("result", {{}}).get("value", "[]")
fields = json.loads(fields_raw)
print("---FIELDS-COUNT---")
print(len(fields))

# Index fields by heading so we can apply the outer plan.
by_heading = {{f["heading"]: f for f in fields}}

plan = json.loads({plan_json!r})
applied = []
errors = []
for step in plan:
    heading = step["heading"]
    answer = step["answer"]
    f = by_heading.get(heading)
    if not f:
        errors.append({{"heading": heading, "reason": "heading_not_in_dom"}})
        continue
    try:
        if f["input"]:
            click_at_xy(int(f["input"]["x"]), int(f["input"]["y"]))
            time.sleep(0.3)
            type_text(str(answer))
            time.sleep(0.3)
            applied.append({{"heading": heading, "mode": "text", "value": str(answer)[:80]}})
            continue
        if f["radios"]:
            # Choose option matching the answer (substring / token overlap)
            ans = str(answer).lower().strip()
            choice = None
            for r in f["radios"]:
                t = r["text"].lower().strip()
                if t == ans or ans in t or t in ans:
                    choice = r
                    break
            if not choice:
                a_tok = set(w for w in ans.replace(",", " ").split() if len(w) > 2)
                best = (0, None)
                for r in f["radios"]:
                    t_tok = set(w for w in r["text"].lower().split() if len(w) > 2)
                    n = len(a_tok & t_tok)
                    if n > best[0]:
                        best = (n, r)
                choice = best[1] if best[0] >= 1 else None
            if choice:
                click_at_xy(int(choice["x"]), int(choice["y"]))
                time.sleep(0.3)
                applied.append({{"heading": heading, "mode": "radio", "value": choice["text"]}})
            else:
                errors.append({{"heading": heading, "reason": "no_radio_match",
                                "answer": str(answer), "options": [r["text"] for r in f["radios"]]}})
            continue
        if f["checkboxes"]:
            # Comma-separated answer -> multi-check
            parts = [p.strip().lower() for p in str(answer).replace(";", ",").split(",") if p.strip()]
            clicked = []
            for c in f["checkboxes"]:
                ct = c["text"].lower().strip()
                if any(p == ct or p in ct or ct in p for p in parts):
                    click_at_xy(int(c["x"]), int(c["y"]))
                    time.sleep(0.2)
                    clicked.append(c["text"])
            if clicked:
                applied.append({{"heading": heading, "mode": "checkbox", "value": clicked}})
            else:
                errors.append({{"heading": heading, "reason": "no_checkbox_match",
                                "answer": str(answer), "options": [c["text"] for c in f["checkboxes"]]}})
            continue
        errors.append({{"heading": heading, "reason": "no_input_radio_or_checkbox_detected"}})
    except Exception as e:
        errors.append({{"heading": heading, "reason": "fill_exception", "error": str(e)}})

# Read back what's actually set on the page.
verify_js = {VERIFY_JS!r}
v = cdp("Runtime.evaluate", expression=verify_js, returnByValue=True)
verify_raw = v.get("result", {{}}).get("value", "{{}}")
verify = json.loads(verify_raw)

capture_screenshot({shot_path!r})

submitted = False
submit = {submit!r}
if submit:
    submit_js = {SUBMIT_JS!r}
    s = cdp("Runtime.evaluate", expression=submit_js, returnByValue=True)
    s_raw = s.get("result", {{}}).get("value", "null")
    s_obj = json.loads(s_raw) if s_raw != "null" else None
    if s_obj:
        click_at_xy(int(s_obj["x"]), int(s_obj["y"]))
        time.sleep(3)
        capture_screenshot({shot_path.replace(".png", "-after-submit.png")!r})
        submitted = True

print("---FINAL---")
print(json.dumps({{
    "fields_detected": len(fields),
    "filled": applied,
    "errors": errors,
    "verification": verify,
    "submitted": submitted,
}}))
"""


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: fill-form.py <url> <answers-json> [--submit]")
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
    log(f"loaded {len(answers)} answers")

    # First pass: navigate + harvest headings only, so we can pre-build
    # the fill plan in outer Python. We do this in a SINGLE bh-c that
    # also flips to the form tab and returns the list — keeping it
    # within one daemon attach.
    discover_script = (
        "import json, time\n"
        f"new_tab({url!r})\n"
        "wait_for_load()\n"
        "time.sleep(4)\n"
        f"r = cdp('Runtime.evaluate', expression={FIELDS_JS!r}, returnByValue=True)\n"
        "print('---FIELDS---')\n"
        "print(r.get('result', {}).get('value', '[]'))\n"
    )
    out = bh_run(discover_script)
    fields_raw = out.split("---FIELDS---", 1)[1].strip()
    fields = json.loads(fields_raw)
    headings = [f["heading"] for f in fields]
    log(f"discovered {len(headings)} headings")

    matches = build_match_map(answers, headings)
    log(f"matched {len(matches)} of {len(answers)} answers to {len(headings)} headings")
    skipped_headings = [h for h in headings if h not in matches]
    unused_answer_keys = [k for k in answers if not any(_norm(k) in _norm(h) or _norm(h) in _norm(k) for h in matches)]

    fill_plan = [{"heading": h, "answer": matches[h]} for h in matches]

    # Second pass: navigate AGAIN (fresh attach), apply plan, verify,
    # screenshot, optional submit. All in ONE bh-c.
    driver = build_driver_script(url, fill_plan, submit, shot_path)
    out2 = bh_run(driver, timeout=240)
    final_raw = out2.split("---FINAL---", 1)[1].strip()
    result = json.loads(final_raw)

    msg_lines = [
        f"📝 Form fill — {len(result['filled'])} fields filled, {len(result['errors'])} errors, {len(skipped_headings)} unmatched.",
        f"Screenshot: {shot_path}",
    ]
    if skipped_headings:
        msg_lines.append("Unmatched form headings (no answer provided):")
        for h in skipped_headings[:5]:
            msg_lines.append(f"  • {h}")
    if result["errors"]:
        msg_lines.append("Errors:")
        for e in result["errors"][:5]:
            msg_lines.append(f"  • {e['heading']}: {e['reason']}")
    if submit:
        msg_lines.append("✅ Submitted." if result["submitted"] else "⚠️ Submit button not found.")
    else:
        msg_lines.append("Stopped before submit — review screenshot first.")

    print(json.dumps({
        "ok": True,
        "url": url,
        "fields_detected": result["fields_detected"],
        "filled": result["filled"],
        "errors": result["errors"],
        "skipped_headings": skipped_headings,
        "unused_answer_keys": unused_answer_keys,
        "submitted": result["submitted"],
        "verification": result["verification"],
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
