#!/usr/bin/env python3
"""Kleine geteilte Helfer ohne App-Zustand (v3.0)."""


def _html_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
