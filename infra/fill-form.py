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

# Synonym table — match common label variants to a canonical form so
# answer key "Phone" matches form field "Enter mobile number" / "Cell" /
# "Telefone" / "Tel." etc. Both keys and headings get normalized
# through this before matching.
_SYNONYMS = {
    # phone/mobile family
    "mobile": "phone", "cell": "phone", "cellular": "phone", "tel": "phone",
    "telephone": "phone", "mobil": "phone", "handy": "phone", "telefon": "phone",
    "telefono": "phone", "celular": "phone", "movil": "phone",
    # email family
    "email": "email", "e mail": "email", "mail": "email", "courriel": "email",
    # name family
    "fullname": "name", "full name": "name", "your name": "name", "given name": "name",
    "first name": "name", "last name": "name", "first": "name", "surname": "name",
    "nachname": "name", "vorname": "name", "nom": "name", "apellido": "name",
    # date/time
    "datum": "date", "fecha": "date", "tarih": "date",
    "uhrzeit": "time", "hora": "time",
    # party size
    "guests": "party", "people": "party", "diners": "party", "no of guests": "party",
    "personen": "party", "personas": "party",
    # noise words to strip — replaced with empty
    "enter": "", "your": "", "please": "", "the": "", "a ": " ", " an ": " ",
}


def _norm(s):
    out = _NORM.sub(" ", (s or "").lower()).strip()
    # Token-level synonym substitution
    parts = out.split()
    canonical = []
    for p in parts:
        if p in _SYNONYMS:
            v = _SYNONYMS[p]
            if v: canonical.append(v)
        else:
            canonical.append(p)
    return " ".join(canonical).strip()


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
    """Run a Python script under browser-harness. The CLI reads scripts
    from STDIN — not via -c (which silently prints the usage banner
    and exits 1)."""
    if not os.path.isfile(BH):
        raise RuntimeError(f"browser-harness CLI not found at {BH}")
    r = subprocess.run([BH], input=script, capture_output=True, text=True, timeout=timeout, env=ENV)
    if r.returncode != 0:
        log(f"bh stderr: {r.stderr[-500:]}")
        raise RuntimeError(f"browser-harness exit {r.returncode}: {r.stderr[-200:]}")
    return r.stdout


# Field discovery — covers Google Forms (div[role=listitem]) AND
# generic HTML forms (<label for=X> + input[id=X], or <input
# placeholder=X>), AND custom widgets that just have aria-label.
# Returns one entry per detected question with text/radios/checkboxes/
# select-options/date-input metadata.
FIELDS_JS = r"""
(function(){
  function visible(el){
    if (!el) return false;
    var r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    var s = window.getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  }
  function labelFor(el){
    // Direct <label for=...>
    if (el.id){
      var lab = document.querySelector('label[for="'+el.id+'"]');
      if (lab && lab.innerText.trim()) return lab.innerText.trim();
    }
    // aria-labelledby chain
    var lbid = el.getAttribute('aria-labelledby');
    if (lbid){
      var parts = lbid.split(/\s+/).map(function(x){
        var n = document.getElementById(x);
        return n ? (n.innerText||'').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ').trim();
    }
    // aria-label
    var al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    // Wrapping <label>
    var p = el.closest('label');
    if (p && p.innerText.trim()) return p.innerText.trim();
    // placeholder
    var ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    // name attribute as a last resort
    var n = el.getAttribute('name');
    if (n) return n;
    return '';
  }
  var seen = new Set();
  var fields = [];
  // Pass 1 — Google Forms-style listitems (rich heading + radios/checkboxes)
  Array.from(document.querySelectorAll('div[role="listitem"]')).forEach(function(li){
    if (!visible(li)) return;
    var h = li.querySelector('[role="heading"], legend');
    var heading = h && h.innerText.trim() ? h.innerText.trim().replace(/\s*\*\s*$/, '') : '';
    if (!heading) {
      var input = li.querySelector('input, textarea, select');
      if (input) heading = labelFor(input);
    }
    if (!heading) return;
    var key = 'gf|' + heading;
    if (seen.has(key)) return; seen.add(key);
    var input = li.querySelector('input[type="text"], input[type="email"], input[type="url"], input[type="tel"], input[type="number"], input[type="date"], input[type="time"], input[type="datetime-local"], textarea');
    var select = li.querySelector('select');
    var radios = Array.from(li.querySelectorAll('[role="radio"], input[type="radio"]')).map(function(r){
      var t = (r.getAttribute('data-value') || r.getAttribute('aria-label') || labelFor(r) || r.value || '').trim();
      return {text: t};
    }).filter(function(r){return r.text.length>0;});
    var checkboxes = Array.from(li.querySelectorAll('[role="checkbox"], input[type="checkbox"]')).map(function(c){
      var t = (c.getAttribute('data-answer-value') || c.getAttribute('aria-label') || labelFor(c) || c.value || '').trim();
      return {text: t};
    }).filter(function(c){return c.text.length>0;});
    fields.push({
      heading: heading,
      input_type: input ? input.type || 'text' : (select ? 'select' : null),
      select_options: select ? Array.from(select.options).map(function(o){return {text: o.text.trim(), value: o.value};}).filter(function(o){return o.text;}) : [],
      radios: radios,
      checkboxes: checkboxes,
    });
  });
  // Pass 2 — Plain HTML inputs not inside a listitem (generic forms,
  // checkout pages, hotel booking pages, etc.). Skip ones we already
  // captured.
  Array.from(document.querySelectorAll('input, textarea, select')).forEach(function(el){
    if (!visible(el)) return;
    if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.type === 'reset' || el.type === 'file') return;
    if (el.closest('div[role="listitem"]')) return;
    var heading = labelFor(el);
    if (!heading) return;
    var key = 'plain|' + heading;
    if (seen.has(key)) return; seen.add(key);
    if (el.tagName === 'SELECT') {
      fields.push({
        heading: heading, input_type: 'select',
        select_options: Array.from(el.options).map(function(o){return {text: o.text.trim(), value: o.value};}).filter(function(o){return o.text;}),
        radios: [], checkboxes: [],
      });
      return;
    }
    if (el.type === 'radio') {
      // Group by name. The group's heading should be the fieldset's
      // <legend>, NOT the first radio's individual label (that's the
      // option text). Falls back to aria-labelledby on the fieldset,
      // then to the individual label as a last resort.
      var name = el.getAttribute('name');
      var groupKey = 'radio-group|' + (name || heading);
      if (seen.has(groupKey)) return; seen.add(groupKey);
      var fs = el.closest('fieldset');
      var groupHeading = '';
      if (fs){
        var lg = fs.querySelector('legend');
        if (lg && lg.innerText.trim()) groupHeading = lg.innerText.trim();
      }
      if (!groupHeading){
        // aria-labelledby on a wrapper, or an ancestor [role=radiogroup]
        var rg = el.closest('[role="radiogroup"]');
        if (rg){
          var lblid = rg.getAttribute('aria-labelledby');
          if (lblid){
            var lab = document.getElementById(lblid);
            if (lab && lab.innerText.trim()) groupHeading = lab.innerText.trim();
          }
          var lab2 = rg.getAttribute('aria-label');
          if (!groupHeading && lab2) groupHeading = lab2.trim();
        }
      }
      if (!groupHeading) groupHeading = heading;
      var siblings = name ? Array.from(document.querySelectorAll('input[type="radio"][name="'+name+'"]')) : [el];
      fields.push({
        heading: groupHeading,
        input_type: null,
        select_options: [],
        radios: siblings.map(function(s){
          var t = labelFor(s) || s.value || '';
          return {text: t.trim()};
        }).filter(function(s){return s.text;}),
        checkboxes: [],
      });
      return;
    }
    if (el.type === 'checkbox') {
      // Checkbox groups: if there's a name and multiple checkboxes
      // share it, collapse into a single field. Otherwise treat as a
      // single yes/no checkbox.
      var name = el.getAttribute('name');
      var fs = el.closest('fieldset');
      var groupHeading = '';
      if (fs){
        var lg = fs.querySelector('legend');
        if (lg && lg.innerText.trim()) groupHeading = lg.innerText.trim();
      }
      if (name){
        var sibs = Array.from(document.querySelectorAll('input[type="checkbox"][name="'+name+'"]'));
        if (sibs.length > 1) {
          var groupKey = 'cb-group|' + name;
          if (seen.has(groupKey)) return; seen.add(groupKey);
          fields.push({
            heading: groupHeading || heading,
            input_type: null, select_options: [], radios: [],
            checkboxes: sibs.map(function(c){
              var t = labelFor(c) || c.value || '';
              return {text: t.trim()};
            }).filter(function(c){return c.text;}),
          });
          return;
        }
      }
      fields.push({
        heading: heading, input_type: null, select_options: [], radios: [],
        checkboxes: [{text: heading}],
      });
      return;
    }
    fields.push({
      heading: heading,
      input_type: el.type || 'text',
      select_options: [], radios: [], checkboxes: [],
    });
  });
  return JSON.stringify(fields);
})()
"""

# Find Next/Continue button so we can advance through multi-step flows
# WITHOUT clicking Submit. Distinguished from submit by text.
NEXT_BUTTON_JS = r"""
(function(){
  var btns = Array.from(document.querySelectorAll('[role="button"], button, input[type="submit"], input[type="button"], a'))
    .filter(function(b){
      var r = b.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return false;
      var s = window.getComputedStyle(b);
      return s.visibility !== 'hidden' && s.display !== 'none';
    });
  // Match Next/Continue/Forward in multiple languages, but NOT Submit/Send
  var nextRe = /^(next|continue|weiter|suivant|siguiente|continuar|continua|avanti|次へ|下一步|→)\b/i;
  var submitRe = /^(submit|send|senden|gönder|enviar|envoyer|invia|送信)\b/i;
  var b = btns.find(function(x){
    var t = (x.innerText || x.value || '').trim();
    return nextRe.test(t) && !submitRe.test(t);
  });
  if (!b) return JSON.stringify(null);
  return JSON.stringify({text: (b.innerText||b.value||'').trim()});
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
  function labelFor(el){
    if (el.id){
      var lab = document.querySelector('label[for="'+el.id+'"]');
      if (lab && lab.innerText.trim()) return lab.innerText.trim();
    }
    var p = el.closest('label');
    if (p && p.innerText.trim()) return p.innerText.trim();
    return el.getAttribute('aria-label') || el.value || el.getAttribute('name') || '';
  }
  // All data-bearing text-like inputs
  var texts = Array.from(document.querySelectorAll(
    'input[type="text"], input[type="email"], input[type="url"], input[type="tel"], ' +
    'input[type="number"], input[type="date"], input[type="time"], input[type="datetime-local"], ' +
    'input[type="search"], input[type="password"], textarea, input:not([type])'
  )).map(function(i){ return i.value; }).filter(function(v){ return v && v.length > 0; });
  // ARIA + HTML radios that are now checked
  var radios = Array.from(document.querySelectorAll('[role="radio"][aria-checked="true"]'))
    .map(function(r){ return (r.getAttribute('data-value') || r.innerText || '').trim(); });
  Array.from(document.querySelectorAll('input[type="radio"]:checked')).forEach(function(r){
    radios.push(labelFor(r));
  });
  // ARIA + HTML checkboxes that are now checked
  var checks = Array.from(document.querySelectorAll('[role="checkbox"][aria-checked="true"]'))
    .map(function(c){ return (c.getAttribute('data-answer-value') || c.innerText || '').trim(); });
  Array.from(document.querySelectorAll('input[type="checkbox"]:checked')).forEach(function(c){
    checks.push(labelFor(c));
  });
  // Native select selected values
  var selects = Array.from(document.querySelectorAll('select')).map(function(s){
    var o = s.options[s.selectedIndex];
    return o ? (o.text || o.value || '').trim() : '';
  }).filter(Boolean);
  return JSON.stringify({texts: texts, radios: radios, checks: checks, selects: selects});
})()
"""


FRAMES_HELPER = r"""
function allDocs(){
  var docs = [document];
  try {
    Array.from(document.querySelectorAll('iframe')).forEach(function(f){
      try {
        var d = f.contentDocument;
        if (d && d !== document) docs.push(d);
      } catch (e) { /* cross-origin */ }
    });
  } catch (e) {}
  return docs;
}
function queryAll(sel){
  var out = [];
  allDocs().forEach(function(d){
    try { out = out.concat(Array.from(d.querySelectorAll(sel))); } catch(e){}
  });
  return out;
}
"""


NEXT_CLICK_JS = r"""
(function(){
  var btns = Array.from(document.querySelectorAll('[role="button"], button, input[type="submit"], input[type="button"], a'));
  var nextRe = /^(next|continue|weiter|suivant|siguiente|continuar|continua|avanti|次へ|下一步|→)\b/i;
  var submitRe = /^(submit|send|senden|gönder|enviar|envoyer|invia|送信)\b/i;
  var b = btns.find(function(x){
    var r = x.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    var s = window.getComputedStyle(x);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    var t = (x.innerText || x.value || '').trim();
    return nextRe.test(t) && !submitRe.test(t);
  });
  if (!b) return JSON.stringify({clicked: false});
  b.scrollIntoView({block: 'center'});
  b.click();
  return JSON.stringify({clicked: true, text: (b.innerText||b.value||'').trim()});
})()
"""

# When the discovery pass sees 0 actionable fields, look for a CTA
# that opens the actual booking/order/reservation form. Common pattern
# on restaurant aggregators (Zomato, District, OpenTable, Tock) and
# hotel booking landing pages: a single "Book a Table" / "Reserve" /
# "Make a reservation" / "Book now" CTA.
# Clicks a Lunch/Dinner/Brunch meal-period selector. Returns whether
# anything was clicked. Used as a separate step before time-slot
# matching, so the page has time to refresh slots between clicks.
MEAL_PERIOD_CLICK_JS = r"""
(function(targetTime){
""" + FRAMES_HELPER + r"""
  function visibleButtons(){
    return queryAll('button, [role="button"], a, div[tabindex="0"]')
      .filter(function(b){
        var r = b.getBoundingClientRect();
        var s = window.getComputedStyle(b);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      });
  }
  function parse24h(s){
    var m = s.match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
    if (!m) return null;
    var h = parseInt(m[1], 10);
    var mm = m[2] ? parseInt(m[2], 10) : 0;
    var ap = (m[3] || '').toLowerCase();
    if (ap === 'pm' && h < 12) h += 12;
    if (ap === 'am' && h === 12) h = 0;
    return h * 60 + mm;
  }
  var want = parse24h(String(targetTime));
  if (want === null) return JSON.stringify({clicked: false, reason: 'unparseable_time'});
  var meal = null, mealName = '';
  if (want >= 16 * 60) { meal = /^(dinner|abendessen|cena|dîner)\b/i; mealName = 'Dinner'; }
  else if (want >= 11 * 60) { meal = /^(lunch|mittagessen|almuerzo|déjeuner|pranzo)\b/i; mealName = 'Lunch'; }
  else { meal = /^(brunch|breakfast|frühstück|desayuno|petit\s+déjeuner|colazione)\b/i; mealName = 'Brunch'; }
  var btns = visibleButtons();
  var mb = btns.find(function(b){ return meal.test((b.innerText||'').trim()); });
  if (!mb) return JSON.stringify({clicked: false, reason: 'no_meal_button', wanted: mealName});
  var cls = (mb.className || '') + ' ' + (mb.getAttribute('aria-pressed') || '');
  var selected = /selected|active|pressed|true/i.test(cls);
  if (selected) return JSON.stringify({clicked: false, reason: 'already_selected', wanted: mealName});
  mb.scrollIntoView({block: 'center'});
  mb.click();
  return JSON.stringify({clicked: true, kind: 'meal_period', text: (mb.innerText||'').trim(), wanted: mealName});
})('TIME_PLACEHOLDER')
"""

# Picks a time-slot button matching the user's requested time. Pure
# slot click — assumes meal period has already been selected. Returns
# the list of visible slot texts if no match within tolerance, so the
# user can see what's available.
TIME_SLOT_CLICK_JS = r"""
(function(targetTime){
""" + FRAMES_HELPER + r"""
  function visibleButtons(){
    return queryAll('button, [role="button"], a, div[tabindex="0"]')
      .filter(function(b){
        var r = b.getBoundingClientRect();
        var s = window.getComputedStyle(b);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      });
  }
  function parse24h(s){
    var m = s.match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
    if (!m) return null;
    var h = parseInt(m[1], 10);
    var mm = m[2] ? parseInt(m[2], 10) : 0;
    var ap = (m[3] || '').toLowerCase();
    if (ap === 'pm' && h < 12) h += 12;
    if (ap === 'am' && h === 12) h = 0;
    return h * 60 + mm;
  }
  var want = parse24h(String(targetTime));
  if (want === null) return JSON.stringify({clicked: false, reason: 'unparseable_time', target: String(targetTime)});
  var btns = visibleButtons();
  var bestSlot = null, bestDelta = 1e9;
  for (var i = 0; i < btns.length; i++){
    var t = (btns[i].innerText || '').trim();
    if (t.length > 40) continue;  // ignore long buttons (search box etc)
    var tm = parse24h(t);
    if (tm === null) continue;
    var delta = Math.abs(tm - want);
    if (delta < bestDelta && delta <= 30){
      bestDelta = delta;
      bestSlot = btns[i];
    }
  }
  if (bestSlot){
    bestSlot.scrollIntoView({block: 'center'});
    bestSlot.click();
    return JSON.stringify({clicked: true, kind: 'time_slot', text: (bestSlot.innerText||'').trim().slice(0, 60), delta_min: bestDelta});
  }
  var available = btns.map(function(b){
    var t = (b.innerText||'').trim();
    return (t.length < 40 && parse24h(t) !== null) ? t.slice(0, 60) : null;
  }).filter(Boolean).slice(0, 20);
  return JSON.stringify({clicked: false, reason: 'no_slot_within_tolerance', target: String(targetTime), available: available});
})('TIME_PLACEHOLDER')
"""


# If the only thing blocking the primary CTA is an unselected
# "offer" / "discount" radio (District / Zomato style), click the
# first visible unselected radio on the page. Pure heuristic — only
# fires when other fill paths have produced 0 plan and CTA hasn't
# advanced.
OFFER_RADIO_CLICK_JS = r"""
(function(){
  var radios = Array.from(document.querySelectorAll('input[type="radio"]:not(:checked)'))
    .filter(function(r){
      var rect = r.getBoundingClientRect();
      var s = window.getComputedStyle(r);
      return rect.width > 0 && rect.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    });
  if (!radios.length) return JSON.stringify({clicked: false, reason: 'no_unselected_radio'});
  var r = radios[0];
  r.scrollIntoView({block: 'center'});
  r.click();
  // Read back label for diagnostics
  var lab = null;
  if (r.id) {
    var l = document.querySelector('label[for="'+r.id+'"]');
    if (l) lab = l.innerText.trim();
  }
  if (!lab) {
    var p = r.closest('label, [role="radio"]');
    if (p) lab = p.innerText.trim();
  }
  return JSON.stringify({clicked: true, kind: 'offer_radio', label: (lab || r.value || '').slice(0, 80)});
})()
"""


CTA_CLICK_JS = r"""
(function(){
""" + FRAMES_HELPER + r"""
  var btns = queryAll('[role="button"], button, input[type="submit"], input[type="button"], a');
  var ctaRe = /^(book(\s+a\s+table)?|reserve(\s+a\s+table)?|make\s+(a\s+)?reservation|book\s+now|order\s+(now|online)|reserve\s+now|table\s+booking|proceed(\s+to\s+(book|checkout|payment))?|continue\s+to\s+(book|checkout)|confirm\s+(and\s+)?(book|booking|reservation)|tisch\s+reservieren|tisch\s+buchen|reservar|réserver|prenotare)\b/i;
  var consentRe = /^(accept(\s+all)?|alle\s+akzeptieren|akzeptieren|tout\s+accepter|aceptar(\s+todo)?|accetta(\s+tutto)?|reject|alle\s+ablehnen)\b/i;
  var visible = btns.filter(function(x){
    var r = x.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    var s = window.getComputedStyle(x);
    return s.visibility !== 'hidden' && s.display !== 'none' && (r.bottom > 0 && r.top < (window.innerHeight + 500));
  });
  // First — handle consent walls (Google/EU GDPR pages). Prefer "Accept all" so the page proceeds.
  var consent = visible.find(function(x){
    var t = (x.innerText || x.value || '').trim();
    return /^(accept(\s+all)?|alle\s+akzeptieren|akzeptieren|tout\s+accepter|aceptar(\s+todo)?|accetta)\b/i.test(t);
  });
  if (consent){
    consent.scrollIntoView({block: 'center'});
    consent.click();
    return JSON.stringify({clicked: true, kind: 'consent', text: (consent.innerText||consent.value||'').trim()});
  }
  // Booking CTA
  var cta = visible.find(function(x){
    var t = (x.innerText || x.value || '').trim();
    return ctaRe.test(t);
  });
  if (cta){
    var disabled = cta.disabled === true || cta.getAttribute('aria-disabled') === 'true' || cta.getAttribute('disabled') !== null;
    if (!disabled){
      var cs = window.getComputedStyle(cta);
      if (parseFloat(cs.opacity) < 0.6 || cs.pointerEvents === 'none') disabled = true;
    }
    if (disabled){
      return JSON.stringify({clicked: false, reason: 'cta_disabled', text: (cta.innerText||cta.value||'').trim()});
    }
    cta.scrollIntoView({block: 'center'});
    cta.click();
    return JSON.stringify({clicked: true, kind: 'cta', text: (cta.innerText||cta.value||'').trim()});
  }
  return JSON.stringify({clicked: false});
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
  function labelFor(el){
    if (el.id){
      var lab = document.querySelector('label[for="'+el.id+'"]');
      if (lab && lab.innerText.trim()) return lab.innerText.trim();
    }
    var lbid = el.getAttribute('aria-labelledby');
    if (lbid){
      var parts = lbid.split(/\s+/).map(function(x){
        var n = document.getElementById(x);
        return n ? (n.innerText||'').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ').trim();
    }
    var al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    var p = el.closest('label');
    if (p && p.innerText.trim()) return p.innerText.trim();
    var ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    var n = el.getAttribute('name');
    return n || '';
  }
  function findInputByLabel(heading){
    var target = norm(heading);
    var inputs = Array.from(document.querySelectorAll('input, textarea, select'));
    return inputs.find(function(el){
      if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button') return false;
      var r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return false;
      var lbl = norm(labelFor(el));
      return lbl && (lbl === target || lbl.indexOf(target) >= 0 || target.indexOf(lbl) >= 0);
    });
  }
  function _scoreMatch(a, b){
    var nA = norm(a), nB = norm(b);
    if (!nA || !nB) return 0;
    if (nA === nB) return 100;
    if (nA.indexOf(nB) >= 0 || nB.indexOf(nA) >= 0) return 50;
    var tA = new Set(nA.split(' ').filter(function(t){return t.length > 2;}));
    var tB = new Set(nB.split(' ').filter(function(t){return t.length > 2;}));
    var overlap = 0;
    tA.forEach(function(t){ if (tB.has(t)) overlap++; });
    var shorter = Math.min(tA.size, tB.size);
    // Require FULL coverage of the shorter token set (and shorter >= 2)
    if (shorter >= 2 && overlap === shorter) return 30;
    // Strict fallback: at least 2 distinct token matches (avoids weak
    // single-word ties like "Pizza Size" vs "Pizza Toppings")
    if (overlap >= 2) return 20;
    return 0;
  }
  function findGroupByHeading(heading){
    // Match a radio/checkbox group by its <fieldset><legend>...</legend>
    // or [role=radiogroup][aria-labelledby/aria-label]. Pick the BEST
    // scoring candidate, not the first fuzzy hit — otherwise "Pizza
    // Toppings" matches the "Pizza Size" fieldset.
    var best = {score: 0, group: null};
    var fieldsets = Array.from(document.querySelectorAll('fieldset'));
    for (var i = 0; i < fieldsets.length; i++) {
      var fs = fieldsets[i];
      var lg = fs.querySelector('legend');
      if (!lg) continue;
      var s = _scoreMatch(lg.innerText, heading);
      if (s > best.score) {
        best = {score: s, group: {
          radios: Array.from(fs.querySelectorAll('input[type="radio"]')),
          checks: Array.from(fs.querySelectorAll('input[type="checkbox"]')),
        }};
      }
    }
    var rgs = Array.from(document.querySelectorAll('[role="radiogroup"]'));
    for (var j = 0; j < rgs.length; j++){
      var rg = rgs[j];
      var lblid = rg.getAttribute('aria-labelledby');
      var lab = lblid ? document.getElementById(lblid) : null;
      var lbl = (lab && lab.innerText) ? lab.innerText : (rg.getAttribute('aria-label') || '');
      if (!lbl) continue;
      var s2 = _scoreMatch(lbl, heading);
      if (s2 > best.score) {
        best = {score: s2, group: {
          radios: Array.from(rg.querySelectorAll('input[type="radio"], [role="radio"]')),
          checks: [],
        }};
      }
    }
    // Require minimum quality — exact/substring or full-token-coverage
    return best.score >= 30 ? best.group : null;
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
  function setSelectValue(sel, value){
    var target = norm(value);
    var match = Array.from(sel.options).find(function(o){
      return softMatch(o.text, value) || norm(o.value) === target;
    });
    if (!match) return false;
    sel.value = match.value;
    sel.dispatchEvent(new Event('input', {bubbles: true}));
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  }
  var filled = [];
  var errors = [];
  for (var i = 0; i < plan.length; i++){
    var step = plan[i];
    try {
      // Try Google-Forms-style listitem first; fall back to plain
      // <label for=X> + input[id=X] / <select> pair if no listitem;
      // then fieldset-legend / role=radiogroup group lookup.
      var li = findItem(step.heading);
      // Plain-input path
      if (!li){
        // Try group lookup BEFORE per-input lookup so "Pizza Size"
        // picks the fieldset, not a single radio's individual label
        var group = findGroupByHeading(step.heading);
        if (group){
          group.radios[0] && group.radios[0].scrollIntoView({block: 'center'});
          var match = group.radios.find(function(r){ return softMatch(labelFor(r) || r.value, step.answer); });
          if (match){
            match.click();
            filled.push({heading: step.heading, mode: 'radio', value: labelFor(match) || match.value});
            continue;
          }
          // For checkbox groups: comma-split the answer and check matching ones
          if (group.checks.length){
            var parts = String(step.answer).split(/[,;]|\sand\s/).map(function(s){return s.trim();}).filter(Boolean);
            var clicked = [];
            for (var ci = 0; ci < group.checks.length; ci++){
              var c = group.checks[ci];
              for (var pi = 0; pi < parts.length; pi++){
                if (softMatch(labelFor(c) || c.value, parts[pi])) {
                  if (!c.checked) c.click();
                  clicked.push(labelFor(c) || c.value);
                  break;
                }
              }
            }
            if (clicked.length){ filled.push({heading: step.heading, mode: 'checkbox', value: clicked}); continue; }
          }
          errors.push({heading: step.heading, reason: 'no_group_option_match', answer: String(step.answer),
                       options: group.radios.map(function(r){return labelFor(r) || r.value;}).concat(
                                group.checks.map(function(c){return labelFor(c) || c.value;}))});
          continue;
        }
        var plainInput = findInputByLabel(step.heading);
        if (plainInput){
          plainInput.scrollIntoView({block: 'center'});
          if (plainInput.tagName === 'SELECT'){
            var ok = setSelectValue(plainInput, step.answer);
            if (ok) filled.push({heading: step.heading, mode: 'select', value: String(step.answer)});
            else errors.push({heading: step.heading, reason: 'no_select_option', answer: String(step.answer),
                              options: Array.from(plainInput.options).map(function(o){return o.text;})});
          } else if (plainInput.type === 'checkbox') {
            var want = /^(yes|true|on|1|checked|✓)$/i.test(String(step.answer).trim());
            if (want !== plainInput.checked) plainInput.click();
            filled.push({heading: step.heading, mode: 'checkbox', value: want});
          } else if (plainInput.type === 'radio') {
            // Group lookup: same name with matching label
            var name = plainInput.getAttribute('name');
            var siblings = name ? Array.from(document.querySelectorAll('input[type="radio"][name="'+name+'"]')) : [plainInput];
            var match = siblings.find(function(s){ return softMatch(labelFor(s) || s.value, step.answer); });
            if (match){ match.click(); filled.push({heading: step.heading, mode: 'radio', value: labelFor(match)||match.value}); }
            else errors.push({heading: step.heading, reason: 'no_radio_match', answer: String(step.answer)});
          } else {
            plainInput.focus();
            setNativeValue(plainInput, String(step.answer));
            filled.push({heading: step.heading, mode: 'text', input_type: plainInput.type, value: String(step.answer).slice(0, 80)});
          }
          continue;
        }
        errors.push({heading: step.heading, reason: 'item_not_found'});
        continue;
      }
      li.scrollIntoView({block: 'center'});
      var input = li.querySelector('input[type="text"], input[type="email"], input[type="url"], input[type="tel"], input[type="number"], input[type="date"], input[type="time"], input[type="datetime-local"], textarea');
      var liSelect = li.querySelector('select');
      if (input){
        input.focus();
        setNativeValue(input, String(step.answer));
        filled.push({heading: step.heading, mode: 'text', input_type: input.type, value: String(step.answer).slice(0, 80)});
        continue;
      }
      if (liSelect){
        var ok = setSelectValue(liSelect, step.answer);
        if (ok) filled.push({heading: step.heading, mode: 'select', value: String(step.answer)});
        else errors.push({heading: step.heading, reason: 'no_select_option', answer: String(step.answer),
                          options: Array.from(liSelect.options).map(function(o){return o.text;})});
        continue;
      }
      // Old listitem text path (preserved as fallback)
      var oldInput = li.querySelector('input[type="text"], input[type="email"], input[type="url"], input[type="tel"], textarea');
      if (oldInput){
        oldInput.focus();
        setNativeValue(oldInput, String(step.answer));
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


def build_driver_script(url, all_answers, submit, shot_path, max_steps=6):
    """Build a browser-harness script that runs a multi-step fill loop:
        1. new_tab + wait
        2. Loop up to max_steps:
           a. discover all visible fields on the current page
           b. fuzzy-match remaining unfilled answers to those fields
           c. apply the plan via in-page JS (scroll + native setter +
              click)
           d. verify what got set; capture screenshot for this step
           e. find a Next/Continue button. If present and we filled
              ≥ 1 field this iteration, click it and re-loop.
           f. If no Next + Submit button exists and submit=True,
              click Submit. Done.
           g. If no Next and no Submit, stop.
        3. Print the final envelope JSON.
    """
    answers_json = json.dumps(all_answers)
    fields_js = FIELDS_JS
    exec_js_tpl = EXECUTE_PLAN_JS
    verify_js = VERIFY_JS
    next_click_js = NEXT_CLICK_JS
    cta_click_js = CTA_CLICK_JS
    offer_radio_click_js = OFFER_RADIO_CLICK_JS
    time_slot_click_js = TIME_SLOT_CLICK_JS
    meal_period_click_js = MEAL_PERIOD_CLICK_JS
    shot_after_submit = shot_path.replace(".png", "-after-submit.png")
    shot_step_prefix = shot_path.replace(".png", "-step")
    return f"""
import json, time, re

NORM_RE = re.compile(r'[^\\w\\s]')
SYNONYMS = {{
    'mobile': 'phone', 'cell': 'phone', 'cellular': 'phone', 'tel': 'phone',
    'telephone': 'phone', 'mobil': 'phone', 'handy': 'phone', 'telefon': 'phone',
    'telefono': 'phone', 'celular': 'phone', 'movil': 'phone',
    'email': 'email', 'mail': 'email', 'courriel': 'email',
    'fullname': 'name', 'surname': 'name', 'firstname': 'name', 'lastname': 'name',
    'nachname': 'name', 'vorname': 'name', 'nom': 'name', 'apellido': 'name',
    'guests': 'party', 'people': 'party', 'diners': 'party',
    'personen': 'party', 'personas': 'party',
    # noise words — drop entirely
    'enter': '', 'your': '', 'please': '', 'the': '',
    'no': '', 'of': '', 'number': '', 'address': '',
}}

def norm(s):
    out = NORM_RE.sub(' ', (s or '').lower()).strip()
    parts = out.split()
    canon = []
    for p in parts:
        if p in SYNONYMS:
            v = SYNONYMS[p]
            if v: canon.append(v)
        else:
            canon.append(p)
    return ' '.join(canon).strip()


def soft_match(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta = set(t for t in na.split() if len(t) > 2)
    tb = set(t for t in nb.split() if len(t) > 2)
    return len(ta & tb) >= 2


def build_plan(headings, answers, used_keys):
    plan = []
    remaining_keys = [k for k in answers if k not in used_keys]
    for h in headings:
        chosen = None
        for k in remaining_keys:
            if soft_match(k, h):
                chosen = k; break
        if chosen:
            plan.append({{'heading': h, 'answer': answers[chosen]}})
            remaining_keys.remove(chosen)
            used_keys.add(chosen)
    return plan


all_answers = json.loads({answers_json!r})
used_keys = set()
all_filled = []
all_errors = []
step_screenshots = []
step_records = []

new_tab({url!r})
wait_for_load()
time.sleep(4)

for step in range({max_steps}):
    # 1. Discover current page fields
    r = cdp('Runtime.evaluate', expression={fields_js!r}, returnByValue=True)
    fields_raw = r.get('result', {{}}).get('value', '[]')
    fields = json.loads(fields_raw)
    headings = [f['heading'] for f in fields]

    # 2. Build plan from remaining answers
    plan = build_plan(headings, all_answers, used_keys)

    if not plan and step == 0:
        # First page has no matches at all — might be a landing page
        # with a "Book now" / "Reserve" CTA. Try Next/Continue.
        pass

    # 3. Apply plan via in-page JS
    if plan:
        exec_js = ({exec_js_tpl!r}).replace('PLAN_PLACEHOLDER', json.dumps(plan))
        rr = cdp('Runtime.evaluate', expression=exec_js, returnByValue=True)
        plan_result = json.loads(rr.get('result', {{}}).get('value', '{{}}'))
        all_filled += [dict(f, step=step) for f in plan_result.get('filled', [])]
        all_errors += [dict(e, step=step) for e in plan_result.get('errors', [])]
        time.sleep(1)

    # 4. Verify + screenshot for this step
    v = cdp('Runtime.evaluate', expression={verify_js!r}, returnByValue=True)
    verify = json.loads(v.get('result', {{}}).get('value', '{{}}'))
    shot_p = '{shot_step_prefix}-' + str(step) + '.png'
    capture_screenshot(shot_p)
    step_screenshots.append(shot_p)
    step_records.append({{'step': step, 'fields_seen': len(headings),
                          'plan_size': len(plan), 'verify_state': verify}})

    # 5. Look for Next button. If found, click and re-loop.
    nb = cdp('Runtime.evaluate', expression={next_click_js!r}, returnByValue=True)
    nb_result = json.loads(nb.get('result', {{}}).get('value', '{{}}'))
    if nb_result.get('clicked'):
        time.sleep(3)  # let next page load
        continue

    # 6. If we saw 0 fillable form fields this step, the page might
    #    be a button-grid booking widget (District/OpenTable/Resy
    #    style). Try, in order:
    #    (a) Click a time-slot button matching the user's requested time
    #    (b) Click an "Accept all" consent button
    #    (c) Click a "Book a Table" / "Reserve" CTA
    if not plan:
        ts_answer = None
        for k in all_answers:
            if k in used_keys: continue
            nk = norm(k)
            if any(w in nk for w in ['time', 'slot', 'when', 'hour']):
                ts_answer = all_answers[k]; break  # do NOT consume yet — we may try twice

        # (a) If we have a time target AND haven't already done a
        #     meal-period click, try clicking Lunch/Dinner/Brunch first
        #     so the slot list refreshes. ONE meal-period click per run.
        if ts_answer and not any(r.get('meal_period_clicked') for r in step_records):
            mp_js = ({meal_period_click_js!r}).replace('TIME_PLACEHOLDER', str(ts_answer).replace("'", ""))
            mp = cdp('Runtime.evaluate', expression=mp_js, returnByValue=True)
            mp_result = json.loads(mp.get('result', {{}}).get('value', '{{}}'))
            step_records[-1]['meal_period_clicked'] = mp_result
            if mp_result.get('clicked'):
                time.sleep(4)  # let the time-slot row refresh
                continue

        # (b) Time-slot button
        if ts_answer:
            ts_js = ({time_slot_click_js!r}).replace('TIME_PLACEHOLDER', str(ts_answer).replace("'", ""))
            ts = cdp('Runtime.evaluate', expression=ts_js, returnByValue=True)
            ts_result = json.loads(ts.get('result', {{}}).get('value', '{{}}'))
            step_records[-1]['time_slot_attempt'] = ts_result
            if ts_result.get('clicked'):
                used_keys.add(k)
                all_filled.append({{'heading': 'time_slot', 'mode': 'click', 'value': ts_result.get('text'), 'step': step}})
                time.sleep(3)
                continue
            else:
                all_errors.append({{'heading': 'time_slot', 'reason': ts_result.get('reason', 'no_slot_click'),
                                    'available': ts_result.get('available', []), 'step': step}})

        # (c) CTA / consent
        cta_js = {cta_click_js!r}
        cc = cdp('Runtime.evaluate', expression=cta_js, returnByValue=True)
        cc_result = json.loads(cc.get('result', {{}}).get('value', '{{}}'))
        if cc_result.get('clicked'):
            step_records[-1]['cta_clicked'] = cc_result
            # Loop detection: if we just clicked the SAME CTA text 2 steps
            # in a row without progress (same fields_seen), stop — clicks
            # are being silently rejected (auth wall, etc).
            prev = step_records[-2] if len(step_records) >= 2 else None
            if prev and prev.get('cta_clicked', {{}}).get('text') == cc_result.get('text') and prev.get('fields_seen') == step_records[-1].get('fields_seen'):
                all_errors.append({{'heading': cc_result.get('text'), 'reason': 'cta_no_progress',
                                    'step': step, 'note': 'Clicked same CTA twice with no DOM change — likely auth wall, disabled state, or modal-only flow'}})
                break
            time.sleep(4)
            continue
        if cc_result.get('reason') == 'cta_disabled':
            # CTA exists but greyed out — try clicking the first visible
            # unselected radio (offer/promo radios on booking pages),
            # then retry CTA next iteration.
            offer_js = {offer_radio_click_js!r}
            of = cdp('Runtime.evaluate', expression=offer_js, returnByValue=True)
            of_result = json.loads(of.get('result', {{}}).get('value', '{{}}'))
            step_records[-1]['offer_radio_clicked'] = of_result
            if of_result.get('clicked'):
                time.sleep(2)
                continue
            # Nothing else to unblock — surface the disabled CTA
            all_errors.append({{'heading': cc_result.get('text'), 'reason': 'cta_disabled_no_unblock',
                                'step': step,
                                'note': 'Primary CTA is disabled and no required radio/checkbox found to unblock it. Likely auth wall or app-only flow.'}})
            break

    break  # no Next button, no actionable CTA; we're stuck or done

# Final screenshot
capture_screenshot({shot_path!r})

submitted = False
submit = {submit!r}
if submit:
    submit_exec = '''
(function(){{
  var btns = Array.from(document.querySelectorAll('[role="button"], button, input[type="submit"]'));
  var b = btns.find(function(x){{ return /^(submit|send|senden|gönder|enviar|envoyer|invia|送信|book|reserve|confirm|complete|place order|pay)\\b/i.test(((x.innerText||x.value||'').trim())); }});
  if (!b) return JSON.stringify(false);
  b.scrollIntoView({{block: 'center'}});
  b.click();
  return JSON.stringify(true);
}})()
'''
    s = cdp('Runtime.evaluate', expression=submit_exec, returnByValue=True)
    s_raw = s.get('result', {{}}).get('value', 'false')
    submitted = (s_raw == 'true')
    if submitted:
        time.sleep(4)
        capture_screenshot({shot_after_submit!r})

# Final verification on last page
v2 = cdp('Runtime.evaluate', expression={verify_js!r}, returnByValue=True)
final_verify = json.loads(v2.get('result', {{}}).get('value', '{{}}'))

print('---FINAL---')
print(json.dumps({{
    'fields_detected_total': len(all_filled) + len(all_errors),
    'filled': all_filled,
    'errors': all_errors,
    'step_records': step_records,
    'step_screenshots': step_screenshots,
    'verification': final_verify,
    'submitted': submitted,
    'used_answer_keys': sorted(list(used_keys)),
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

    max_steps = 6
    if "--max-steps" in sys.argv:
        i = sys.argv.index("--max-steps")
        if i + 1 < len(sys.argv):
            try: max_steps = int(sys.argv[i + 1])
            except ValueError: pass

    # Single browser-harness invocation that handles the multi-step
    # form/booking flow end-to-end: discover fields → match remaining
    # answers → fill → screenshot → Next? → repeat → Submit (if asked).
    driver = build_driver_script(url, answers, submit, shot_path, max_steps=max_steps)
    out2 = bh_run(driver, timeout=360)
    final_raw = out2.split("---FINAL---", 1)[1].strip()
    result = json.loads(final_raw)
    used_keys = set(result.get("used_answer_keys", []))
    unused_answer_keys = [k for k in answers if k not in used_keys]
    skipped_headings = [e["heading"] for e in result.get("errors", []) if e.get("reason") == "item_not_found"]

    # Deliver the screenshot directly via Telegram. We don't rely on
    # the agent to remember to call send-telegram-photo.sh — the script
    # that produces the screenshot also delivers it.
    photo_sent = False
    photo_send_err = ""
    send_helper = os.path.expanduser("~/bin/send-telegram-photo.sh")
    if os.path.isfile(send_helper) and os.path.isfile(shot_path):
        caption = "form filled — review before submit" if not submit else "form submitted — confirmation page"
        ps_path = shot_path.replace(".png", "-after-submit.png") if (submit and result.get("submitted")) else shot_path
        try:
            r = subprocess.run([send_helper, ps_path, caption], capture_output=True, text=True, timeout=30)
            photo_sent = r.returncode == 0
            if not photo_sent:
                photo_send_err = (r.stderr or r.stdout or "")[-200:]
        except Exception as e:
            photo_send_err = str(e)

    steps_run = len(result.get("step_records", []))
    msg_lines = [
        f"📝 Form fill — {len(result['filled'])} fields filled across {steps_run} step(s), {len(result['errors'])} errors.",
        f"Screenshot: {shot_path}",
    ]
    if unused_answer_keys:
        msg_lines.append(f"Unused answer keys ({len(unused_answer_keys)}): {', '.join(unused_answer_keys[:5])}")
    if result["errors"]:
        msg_lines.append("Errors:")
        for e in result["errors"][:5]:
            msg_lines.append(f"  • step {e.get('step','?')} | {e['heading']}: {e['reason']}")
    if submit:
        msg_lines.append("✅ Submitted." if result["submitted"] else "⚠️ Submit button not found on the final page.")
    else:
        msg_lines.append("Stopped before submit — review screenshot first.")

    if photo_sent:
        msg_lines.append("📸 Screenshot delivered.")
    elif photo_send_err:
        msg_lines.append(f"📸 Screenshot send failed: {photo_send_err}")

    print(json.dumps({
        "ok": True,
        "url": url,
        "fields_detected_total": result.get("fields_detected_total", 0),
        "filled": result["filled"],
        "errors": result["errors"],
        "step_records": result.get("step_records", []),
        "step_screenshots": result.get("step_screenshots", []),
        "unused_answer_keys": unused_answer_keys,
        "submitted": result["submitted"],
        "verification": result["verification"],
        "screenshot": shot_path,
        "photo_sent": photo_sent,
        "photo_send_error": photo_send_err,
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
