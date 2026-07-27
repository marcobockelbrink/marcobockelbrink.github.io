#!/usr/bin/env python3
"""Kleine geteilte Helfer ohne App-Zustand (v3.0)."""


def _html_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---- Frontend aus Dateien laden (frontend/-Ordner in der Projektwurzel) ----
# Die HTML/CSS/JS-Blobs liegen als echte, review-/lintbare Dateien vor.
# Deploy: aria_kapa.py + kapa/ + frontend/ mitliefern (Override via KAPA_FRONTEND_DIR).
import os

_FRONTEND_DIR = (os.environ.get("KAPA_FRONTEND_DIR")
                 or os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), "frontend"))


def _fe(name):
    """Frontend-Datei (UTF-8) aus _FRONTEND_DIR lesen."""
    try:
        with open(os.path.join(_FRONTEND_DIR, name), encoding="utf-8") as _f:
            return _f.read()
    except FileNotFoundError:
        raise SystemExit("Frontend-Datei fehlt: frontend/%s "
                         "(wird neben aria_kapa.py erwartet)" % name)
