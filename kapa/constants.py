#!/usr/bin/env python3
"""Zentrale Konstanten: Version, Rollen, Ereignisse, Sichtbarkeits-
und Benachrichtigungs-Standards. Aus aria_kapa.py ausgelagert (v3.0)."""

VERSION = "3.4.0"

# Interne Rollen-Schlüssel (steuern die Rechte, unveränderlich) und ihre
# Standard-Bezeichnungen. Die Bezeichnungen lassen sich auf der Verwaltungsseite
# frei umbenennen (data/kapa_rollennamen.json) – die Schlüssel bleiben gleich.
ROLE_KEYS = ("admin", "anforderer", "reviewer", "auditor")
DEFAULT_ROLE_NAMES = {"admin": "Administrator", "anforderer": "Anforderer",
                      "reviewer": "Reviewer", "auditor": "Technische Prüfung"}

# Mail-Benachrichtigungen je interner Rolle. Ereignisse: created (Anlage),
# rejected (Ablehnung), approved (endgültige Genehmigung), team_turn (ein Team
# ist im Freigabe-Workflow an der Reihe). Empfänger (Modell "Gemischt"):
#   anforderer -> der Antragsteller (r.von), kein eigenes Adressfeld
#   admin/auditor -> feste Verteiler-Adresse (Feld "email")
#   reviewer/team_turn -> die pro Team hinterlegte Adresse (team_email)
NOTIFY_EVENTS = ("created", "rejected", "approved", "team_turn", "reminder")
# Welche Ereignisse je Rolle überhaupt wählbar sind (Rest wird als "–" gezeigt):
NOTIFY_ROLE_EVENTS = {
    "anforderer": ("created", "rejected", "approved"),
    "admin":      ("created", "rejected", "approved", "team_turn", "reminder"),
    "auditor":    ("created", "rejected", "approved", "team_turn"),
    "reviewer":   ("team_turn", "reminder"),
}
# Ausgehende Webhooks: welche Ereignisse einen HTTP-POST auslösen können.
WEBHOOK_EVENTS = ("created", "approved", "rejected", "team_turn", "reminder",
                  "storage_requested", "storage_done", "imported")
# Sichtbarkeits-Matrix: WAS eine Rolle sieht (Admin sieht immer alles).
# Bewusst nur Sichtbarkeit, keine Rechte — der Workflow bleibt hart verdrahtet.
VIS_FEATURES = ("workload", "hosts", "vms", "network", "storage", "tags",
                "decided_by", "statistik")
DEFAULT_VISIBILITY = {
    "anforderer": {"workload": False, "hosts": False, "vms": False,
                   "network": True, "storage": True, "tags": True,
                   "decided_by": False, "statistik": True},
    "reviewer":   {"workload": True, "hosts": False, "vms": False,
                   "network": True, "storage": True, "tags": True,
                   "decided_by": True, "statistik": True},
    "auditor":    {f: True for f in VIS_FEATURES},
}

DEFAULT_NOTIFY = {
    "role": {
        "anforderer": {"created": False, "rejected": True,  "approved": True},
        "admin":      {"created": False, "rejected": False, "approved": False,
                       "team_turn": False, "reminder": False, "email": ""},
        "auditor":    {"created": False, "rejected": False, "approved": False,
                       "team_turn": False, "email": ""},
        "reviewer":   {"team_turn": True, "reminder": False},
    },
    "team_email": {},        # {Team-Name: Verteiler-Adresse}
    "reminder_days": 2,      # Erinnerung nach N Tagen Wartezeit (dann alle N Tage)
}
