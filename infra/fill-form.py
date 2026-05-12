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
  var b = btns.find(function(x){ return /^(submit|send|senden|gönder|enviar|envoyer|invia|送信)\b/i.test(((x.innerText||x.value||'').trim())); });
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


# Single JS function that drives every fill step inside the page — no
# cached coordinates, scrolls each element into view before clicking,
# uses the native value-setter so React/Angular's controlled inputs
# pick up the change.
EXECUTE_PLAN_JS = r"""
(function(plan){
  function norm(s){ return (s||'').toLowerCase().replace(/[^\w\s]/g,' ').replace(/\s+/g,' ').trim(); }
  function softMatch(a, b){
    var na = norm(a), nb = norm(b);
    if (!na || !nb) return false;
    if (na === nb || na.indexOf(nb) >= 0 || nb.indexOf(na) >= 0) return true;
    var ta = new Set(na.split(' ').filter(function(t){return t.length>2;}));
    var tb = new Set(nb.split(' ').filter(function(t){return t.length>2;}));
    var overlap = 0;
    ta.forEach(function(t){ if (tb.has(t)) overlap++; });
    return overlap >= 1;
  }
  function findItem(heading){
    var items = Array.from(document.querySelectorAll('div[role="listitem"]'));
    var target = norm(heading);
    return items.find(function(li){
      var h = li.querySelector('[role="heading"], legend');
      if (!h) return false;
      var ht = norm(h.innerText);
      return ht === target || ht.indexOf(target) >= 0 || target.indexOf(ht) >= 0;
    });
  }
  function setNativeValue(el, value){
    var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) setter.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  var filled = [];
  var errors = [];
  for (var i = 0; i < plan.length; i++){
    var step = plan[i];
    try {
      var li = findItem(step.heading);
      if (!li){ errors.push({heading: step.heading, reason: 'item_not_found'}); continue; }
      li.scrollIntoView({block: 'center'});
      var input = li.querySelector('input[type="text"], input[type="email"], input[type="url"], input[type="tel"], textarea');
      if (input){
        input.focus();
        setNativeValue(input, String(step.answer));
        filled.push({heading: step.heading, mode: 'text', value: String(step.answer).slice(0, 80)});
        continue;
      }
      var radios = Array.from(li.querySelectorAll('[role="radio"]'));
      if (radios.length){
        var match = null;
        for (var r = 0; r < radios.length; r++){
          var label = radios[r].getAttribute('data-value') || radios[r].getAttribute('aria-label') || radios[r].innerText;
          if (softMatch(label, step.answer)) { match = radios[r]; break; }
        }
        if (match){ match.click(); filled.push({heading: step.heading, mode: 'radio', value: match.getAttribute('data-value') || match.innerText}); }
        else { errors.push({heading: step.heading, reason: 'no_radio_match', answer: String(step.answer),
                            options: radios.map(function(rr){return (rr.getAttribute('data-value')||rr.innerText||'').trim();})}); }
        continue;
      }
      var checkboxes = Array.from(li.querySelectorAll('[role="checkbox"]'));
      if (checkboxes.length){
        var parts = String(step.answer).split(/[,;]|\sand\s/).map(function(p){return p.trim();}).filter(Boolean);
        var clicked = [];
        for (var c = 0; c < checkboxes.length; c++){
          var lbl = checkboxes[c].getAttribute('data-answer-value') || checkboxes[c].getAttribute('aria-label') || checkboxes[c].innerText;
          for (var p = 0; p < parts.length; p++){
            if (softMatch(lbl, parts[p])) { checkboxes[c].click(); clicked.push(lbl.trim()); break; }
          }
        }
        if (clicked.length){ filled.push({heading: step.heading, mode: 'checkbox', value: clicked}); }
        else { errors.push({heading: step.heading, reason: 'no_checkbox_match', answer: String(step.answer),
                            options: checkboxes.map(function(cc){return (cc.getAttribute('data-answer-value')||cc.innerText||'').trim();})}); }
        continue;
      }
      errors.push({heading: step.heading, reason: 'no_input_radio_or_checkbox'});
    } catch (e) {
      errors.push({heading: step.heading, reason: 'js_exception', error: String(e)});
    }
  }
  return JSON.stringify({filled: filled, errors: errors});
})(PLAN_PLACEHOLDER)
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
    # Inject the plan into the JS template
    execute_js = EXECUTE_PLAN_JS.replace("PLAN_PLACEHOLDER", plan_json)
    shot_after_submit = shot_path.replace(".png", "-after-submit.png")
    return f"""
import json, time
new_tab({url!r})
wait_for_load()
time.sleep(4)

# Single CDP call that processes the entire fill plan inside the page.
# Scrolls each element into view, uses native value-setters, clicks
# options directly — no coord caching, no offscreen clicks.
r = cdp("Runtime.evaluate", expression={execute_js!r}, returnByValue=True)
plan_raw = r.get("result", {{}}).get("value", "{{}}")
plan_result = json.loads(plan_raw)
time.sleep(1)

# Verification: read back what's actually set.
verify_js = {VERIFY_JS!r}
v = cdp("Runtime.evaluate", expression=verify_js, returnByValue=True)
verify_raw = v.get("result", {{}}).get("value", "{{}}")
verify = json.loads(verify_raw)

capture_screenshot({shot_path!r})

submitted = False
submit = {submit!r}
if submit:
    submit_js = {SUBMIT_JS!r}
    # Use direct .click() rather than coords so scroll position doesn't matter
    submit_exec = '''
(function(){{
  var btns = Array.from(document.querySelectorAll('[role=\"button\"], button, input[type=\"submit\"]'));
  var b = btns.find(function(x){{ return /^(submit|send|senden|gönder|enviar|envoyer|invia|送信)\\\\b/i.test(((x.innerText||x.value||'').trim())); }});
  if (!b) return JSON.stringify(false);
  b.scrollIntoView({{block: 'center'}});
  b.click();
  return JSON.stringify(true);
}})()'''
    s = cdp("Runtime.evaluate", expression=submit_exec, returnByValue=True)
    s_raw = s.get("result", {{}}).get("value", "false")
    submitted = (s_raw == "true")
    if submitted:
        time.sleep(3)
        capture_screenshot({shot_after_submit!r})

print("---FINAL---")
print(json.dumps({{
    "fields_detected": len(json.loads({plan_json!r})),
    "filled": plan_result.get("filled", []),
    "errors": plan_result.get("errors", []),
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
