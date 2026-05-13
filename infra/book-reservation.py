#!/usr/bin/env python3
"""book-reservation.py — backward-compatibility shim.

The skill was renamed to /book-anything (multi-modality: restaurants +
movies + events + generic URL paste). This shim preserves the old path
so any AGENTS.md drift, deploy scripts, cron entries, or pre-deploy
cache that still references ~/bin/book-reservation.py keeps working.

Delegates 100% to book-anything.py — no logic lives here. Will be
removed once we've verified no surface references the old path.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TARGET = os.path.join(_HERE, "book-anything.py")

if not os.path.exists(_TARGET):
    # When deployed via deploy-vps.sh both files land in ~/bin/ — try the
    # canonical location.
    _TARGET = os.path.expanduser("~/bin/book-anything.py")

os.execv(sys.executable, [sys.executable, _TARGET] + sys.argv[1:])
