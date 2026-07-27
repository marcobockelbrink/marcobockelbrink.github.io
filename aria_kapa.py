#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aria_kapa.py — Kapazitätsauswertung pro Cluster aus VMware Aria Operations (Suite API)

Berechnung:
  CPU-Kapazität  = Summe physischer Cores aller ESXi-Hosts im Cluster × Faktor (Standard 6)
  RAM-Kapazität  = Summe physischer RAM aller ESXi-Hosts im Cluster (1:1)
  Verbraucht     = Summe provisionierter vCPUs / RAM aller VMs im Cluster (inkl. powered-off)
  Frei           = Kapazität − Verbraucht

Aufruf:
  python3 aria_kapa.py --url https://aria-ops.firma.de --user admin [--password ...]
                       [--auth-source local] [--cpu-factor 6] [--insecure]
                       [--output kapa_dashboard.html]
  python3 aria_kapa.py --sample          # Demo mit Beispieldaten (ohne Verbindung)

Benötigt nur die Python-Standardbibliothek (Python 3.8+).
"""

VERSION = "2.30.0"

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

import argparse
import getpass
import gzip
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

# ---- Frontend aus Dateien laden (frontend/-Ordner neben dem Skript) ----
# Die HTML/CSS/JS-Blobs liegen als echte, review-/lintbare Dateien vor.
# Deploy: aria_kapa.py + frontend/ mitliefern (Override via KAPA_FRONTEND_DIR).
_FRONTEND_DIR = (os.environ.get("KAPA_FRONTEND_DIR")
                 or os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend"))

def _fe(name):
    """Frontend-Datei (UTF-8) aus _FRONTEND_DIR lesen."""
    try:
        with open(os.path.join(_FRONTEND_DIR, name), encoding="utf-8") as _f:
            return _f.read()
    except FileNotFoundError:
        raise SystemExit("Frontend-Datei fehlt: frontend/%s "
                         "(wird neben aria_kapa.py erwartet)" % name)

# ------------------------- vROps Suite-API: kapa/vrops.py --
from kapa.vrops import AriaOps

# -------------------------------------------------- Active Directory (LDAP) --

from kapa.ldap import (ldap_bind, ldap_member_of, ldap_user_attr,
                       ldap_group_members)

# ------------------------------- Datenablage: kapa/store.py --
from kapa.store import (ensure_dir, atomic_write, JsonStore, SqliteStore,
                        data_path, migrate_data_files)

# ------------------------------- Backup/Mail: kapa/-Module --
from kapa.util import _html_escape
from kapa.backup import sftp_backup, backup_rotate
from kapa.mail import (send_mail, reservation_mail_body,
                       reservation_mail_html, render_template,
                       _mail_values, MAIL_VARS,
                       DEFAULT_MAIL_SUBJECT, DEFAULT_MAIL_TEMPLATE)

# ------------------------------------------------------------- Datenabfrage --

from kapa.collect import (strip_uplinks, collect, build_summary,
                          sample_data)


# PowerCLI-Export für Offline-Quellen (Verwaltung -> Import, als .ps1-Download).
# Erzeugt genau das JSON, das POST /api/import erwartet. Bewusst nur mit
# PowerCLI-Bordmitteln und ohne Schreibzugriffe auf das vCenter.
POWERCLI_PS1 = _fe("kapa_export.ps1")

# ------------------------------------------------------------------ Dashboard --

def openapi_spec(lang="de"):
    """OpenAPI-3.0-Beschreibung der lesenden v1-API. Importierbar in Swagger
    Editor/Postman; die eingebaute Seite /api/v1/docs rendert sie direkt.
    lang="en" liefert englische Beschreibungstexte (Feldnamen/Werte bleiben
    unverändert Teil des stabilen v1-Vertrags)."""
    T = (lambda de, en: en) if lang == "en" else (lambda de, en: de)
    reservation = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": T("Eindeutige ID", "Unique ID")},
            "name": {"type": "string", "description": T("Bezeichnung / Projekt", "Name / project")},
            "change": {"type": "string", "description": T("Change-Nummer / Jira-Ticket (optional)", "Change number / Jira ticket (optional)")},
            "cluster": {"type": "string"},
            "vcpu": {"type": "integer"},
            "ram_gb": {"type": "integer"},
            "storage_gb": {"type": "integer", "description": T("nur informativ", "informational only")},
            "von": {"type": "string", "description": T("Anforderer", "Requester")},
            "abteilung": {"type": "string", "description": T("Team/Abteilung", "Team/department")},
            "created": {"type": "string", "description": T("gilt ab (ISO-Datum)", "valid from (ISO date)")},
            "approvals": {"type": "array", "description": T("bisherige Team-Freigaben (Prüfreihenfolge)", "team approvals so far (review order)"),
                          "items": {"type": "object", "properties": {
                              "team": {"type": "string"}, "by": {"type": "string"},
                              "on": {"type": "string"}, "comment": {"type": "string"}}}},
            "approved": {"type": "boolean", "description": T("vollständig genehmigt (alle Stufen)", "fully approved (all stages)")},
            "approved_on": {"type": "string"}, "approved_by": {"type": "string"},
            "rejected": {"type": "boolean"}, "rejected_on": {"type": "string"},
            "rejected_by": {"type": "string"}, "rejected_team": {"type": "string"},
            "cancelled": {"type": "boolean"}, "cancelled_on": {"type": "string"},
            "cancelled_by": {"type": "string"}, "comment": {"type": "string"},
        },
    }
    cluster = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "hostCount": {"type": "integer"}, "cores": {"type": "integer"},
            "vcpuCap": {"type": "number"}, "vcpuUsed": {"type": "number"},
            "vcpuFree": {"type": "number", "description": T("vor Abzug genehmigter Reservierungen", "before subtracting approved reservations")},
            "ramCap": {"type": "number"}, "ramUsed": {"type": "number"}, "ramFree": {"type": "number"},
            "storageCap": {"type": "number"}, "storageUsed": {"type": "number"}, "storageFree": {"type": "number"},
            "vmCount": {"type": "integer"}, "vmOff": {"type": "integer"},
            "workload": {"type": "integer", "nullable": True,
                         "description": T("vROps-Workload-Badge in % (nicht für Anforderer)", "vROps workload badge in % (not for requesters)")},
            "tanzuVcpu": {"type": "integer",
                          "description": T("vCPU-Äquivalent der Tanzu-Namespace-Reservierungen", "vCPU equivalent of Tanzu namespace reservations")},
            "tanzuRamGb": {"type": "number",
                           "description": T("RAM-Reservierungen der Tanzu-Namespaces (GB)", "RAM reservations of the Tanzu namespaces (GB)")},
            "namespaces": {"type": "array",
                           "description": T("Tanzu-/vSphere-Namespaces mit Reservierungen", "Tanzu/vSphere namespaces with reservations"),
                           "items": {"type": "object", "properties": {
                               "name": {"type": "string"}, "cpu_mhz": {"type": "number"},
                               "vcpu": {"type": "integer"}, "ram_gb": {"type": "number"}}}},
            "portgroups": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "vlan": {"type": "string"}}}},
        },
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": T("VMware Kapazitätsplanung – API", "VMware Capacity Planning – API"),
            "version": VERSION,
            "description": T("Stabile, **nur lesende** REST-API. Authentifizierung "
                             "per Bearer-Token (Admins erzeugen es im Tab „Verwaltung“) "
                             "oder per Browser-Session.",
                             "Stable, **read-only** REST API. Authentication via "
                             "bearer token (admins create it in the „Administration“ "
                             "tab) or via browser session. Field names and status "
                             "values are German – they are part of the stable v1 contract."),
        },
        "servers": [{"url": "/", "description": T("dieser Server (hinter einem "
                     "Proxy-Unterpfad wie /capa entsprechend anpassen)",
                     "this server (adjust accordingly behind a proxy sub-path "
                     "like /capa)")}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer",
                               "description": "Header: Authorization: Bearer kapa_…"},
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "kapa_session"},
            },
            "schemas": {"Reservation": reservation, "Cluster": cluster,
                        "Status": {"type": "object", "properties": {
                            "version": {"type": "string"}, "updated": {"type": "string"},
                            "refreshing": {"type": "boolean"},
                            "next": {"type": "integer", "nullable": True,
                                     "description": T("Sekunden bis zur nächsten Aktualisierung", "seconds until the next refresh")}}},
                        "Data": {"type": "object", "properties": {
                            "updated": {"type": "string"},
                            "clusters": {"type": "array", "items": {"$ref": "#/components/schemas/Cluster"}}}}},
        },
        "security": [{"bearerAuth": []}, {"cookieAuth": []}],
        "paths": {
            "/api/v1/storage-requests": {"get": {
                "summary": T("Storage-Erweiterungen (fürs Storage-Team)",
                             "Storage expansions (for the storage team)"),
                "description": T("Angefragte LUN-Vergrößerungen und neue LUNs, "
                                 "inkl. NAA-Kennung und der ESXi-Hosts des Clusters "
                                 "(Feld hosts = Liste aus {name, wwpns}: WWPNs der "
                                 "FC-HBAs fürs Zoning/Mapping und zur System-"
                                 "Identifikation). Standard: offene; status=alle "
                                 "für alle. Als CSV mit format=csv.",
                                 "Requested LUN expansions and new LUNs, incl. NAA "
                                 "and the cluster's ESXi hosts (field hosts = list "
                                 "of {name, wwpns}: FC-HBA WWPNs for zoning/mapping "
                                 "and system identification). Default: open ones; "
                                 "status=alle for all. As CSV with format=csv."),
                "parameters": [
                    {"name": "status", "in": "query", "schema": {"type": "string",
                     "enum": ["offen", "erledigt", "alle"], "default": "offen"}},
                    {"name": "format", "in": "query", "schema": {"type": "string",
                     "enum": ["json", "csv"], "default": "json"}},
                ],
                "responses": {"200": {"description": "OK", "content": {
                    "application/json": {"schema": {"type": "object"}},
                    "text/csv": {"schema": {"type": "string"}}}},
                    "401": {"description": T("Token/Anmeldung fehlt oder ungültig", "Token/sign-in missing or invalid")}}}},
            "/api/v1/storage-requests/{id}/done": {"post": {
                "summary": T("Storage-Erweiterung als erledigt melden (Schreibrecht „Storage“)",
                             "Mark a storage expansion as done (write permission „Storage“)"),
                "description": T("Setzt die Anfrage auf erledigt — für die "
                                 "Automatisierung des Storage-Teams. Token mit "
                                 "Schreibrecht „Storage“ nötig.",
                                 "Marks the request as done — for the storage "
                                 "team's automation. Requires a token with the "
                                 "„Storage“ write permission."),
                "parameters": [{"name": "id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "OK"},
                              "401": {"description": T("Token fehlt/ungültig", "token missing/invalid")},
                              "403": {"description": T("Token ohne Schreibrecht", "token lacks write permission")},
                              "404": {"description": T("nicht gefunden", "not found")}}}},
            "/api/v1/status": {"get": {
                "summary": T("Status & Aktualität", "Status & freshness"),
                "description": T("Version, Zeitpunkt des letzten Aria-Abrufs, ob gerade "
                                 "aktualisiert wird und Sekunden bis zum nächsten Abruf.",
                                 "Version, time of the last Aria refresh, whether a refresh "
                                 "is running and seconds until the next one."),
                "responses": {"200": {"description": "OK", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Status"}}}},
                    "401": {"description": T("Token/Anmeldung fehlt oder ungültig", "Token/sign-in missing or invalid")}}}},
            "/api/v1/data": {"get": {
                "summary": T("Cluster-Kapazitäten", "Cluster capacities"),
                "description": T("Cluster-Kennzahlen aus dem letzten Aria-Abruf. "
                                 "vcpuFree/ramFree sind VOR Abzug genehmigter "
                                 "Reservierungen. Als CSV (format=csv) zusätzlich "
                                 "mit effektiv freien Werten nach Reservierungen "
                                 "und Tanzu-Namespaces.",
                                 "Cluster metrics from the last Aria refresh. "
                                 "vcpuFree/ramFree are BEFORE subtracting approved "
                                 "reservations. As CSV (format=csv) additionally "
                                 "with effective free values after reservations "
                                 "and Tanzu namespaces."),
                "parameters": [
                    {"name": "format", "in": "query", "schema": {"type": "string",
                     "enum": ["json", "csv"], "default": "json"}},
                    {"name": "lang", "in": "query", "schema": {"type": "string",
                     "enum": ["de", "en"]},
                     "description": T("Sprache der CSV-Spalten (Standard: "
                                      "Accept-Language, sonst Deutsch)",
                                      "language of the CSV headers (default: "
                                      "Accept-Language, else German)")},
                ],
                "responses": {"200": {"description": "OK", "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Data"}},
                    "text/csv": {"schema": {"type": "string"}}}},
                    "401": {"description": T("Token/Anmeldung fehlt oder ungültig", "Token/sign-in missing or invalid")}}}},
            "/api/v1/reservations": {"get": {
                "summary": T("Reservierungen (Kapazitätsanfragen)", "Reservations (capacity requests)"),
                "description": T("Alle Reservierungen. Kombinierbare Filter; als CSV mit "
                                 "format=csv (Semikolon, Excel-tauglich).",
                                 "All reservations. Combinable filters; as CSV with "
                                 "format=csv (semicolon, Excel-ready). CSV headers/status "
                                 "follow Accept-Language or ?lang=de|en (default: German)."),
                "parameters": [
                    {"name": "cluster", "in": "query", "schema": {"type": "string"},
                     "description": T("nur dieses Cluster", "only this cluster")},
                    {"name": "abteilung", "in": "query", "schema": {"type": "string"},
                     "description": T("nur dieses Team/diese Abteilung", "only this team/department")},
                    {"name": "status", "in": "query", "schema": {"type": "string",
                     "enum": ["beantragt", "in Prüfung", "genehmigt", "abgelehnt", "storniert"]}},
                    {"name": "format", "in": "query", "schema": {"type": "string",
                     "enum": ["json", "csv"], "default": "json"}},
                    {"name": "lang", "in": "query", "schema": {"type": "string",
                     "enum": ["de", "en"]},
                     "description": T("Sprache der CSV-Spalten/Statuswerte (Standard: "
                                      "Accept-Language, sonst Deutsch)",
                                      "language of CSV headers/status values (default: "
                                      "Accept-Language, else German)")},
                ],
                "responses": {"200": {"description": "OK", "content": {
                    "application/json": {"schema": {"type": "array",
                        "items": {"$ref": "#/components/schemas/Reservation"}}},
                    "text/csv": {"schema": {"type": "string"}}}},
                    "401": {"description": T("Token/Anmeldung fehlt oder ungültig", "Token/sign-in missing or invalid")}},
                },
                "post": {
                    "summary": T("Reservierung anlegen (Schreibrecht „Reservierungen“)",
                                 "Create a reservation (write permission „Reservations“)"),
                    "description": T(
                        "Legt eine Kapazitätsanfrage an (Status „beantragt“, "
                        "durchläuft den normalen Genehmigungsprozess). Erfordert "
                        "ein Token mit dem Schreibrecht „Reservierungen“ "
                        "(Verwaltung → API-Tokens) oder eine Admin-Session.",
                        "Creates a capacity request (status „requested“, passes "
                        "through the normal approval process). Requires a token "
                        "with the „Reservations“ write permission "
                        "(Administration → API tokens) or an admin session."),
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {"type": "object", "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "cluster": {"type": "string"},
                                "change": {"type": "string"},
                                "vcpu": {"type": "integer"},
                                "ram_gb": {"type": "integer"},
                                "storage_gb": {"type": "integer"},
                                "von": {"type": "string", "description": T("Anforderer (Standard: api:<Tokenname>)", "requester (default: api:<token name>)")},
                                "abteilung": {"type": "string", "description": T("Team des Anforderers (für die Team-Sichtbarkeit)", "requester's team (for team visibility)")}}}}}},
                    "responses": {"201": {"description": T("angelegt", "created")},
                                  "401": {"description": T("Token fehlt/ungültig", "token missing/invalid")},
                                  "403": {"description": T("Token ohne Schreibrecht", "token lacks write permission")}}},
            },
            "/api/v1/reservations/{id}/approve": {"post": _v1_decide(
                T, "approve",
                T("Antrag freigeben (aktuelle Stufe; Schreibrecht „Genehmigungen“)",
                  "Approve the request's current stage (write permission „Approvals“)"))},
            "/api/v1/reservations/{id}/reject": {"post": _v1_decide(
                T, "reject",
                T("Antrag ablehnen (Schreibrecht „Genehmigungen“)",
                  "Reject the request (write permission „Approvals“)"))},
            "/api/v1/reservations/{id}/cancel": {"post": _v1_decide(
                T, "cancel",
                T("Antrag stornieren (Schreibrecht „Reservierungen“)",
                  "Cancel the request (write permission „Reservations“)"))},
        },
    }


def _v1_decide(T, op, summary):
    """POST-Operation für approve/reject/cancel in der OpenAPI-Spec."""
    return {
        "summary": summary,
        "description": T("Wirkt wie eine Admin-Entscheidung (keine "
                         "Team-Beschränkung); Actor im Audit-Log: api:<Tokenname>.",
                         "Acts like an admin decision (no team restriction); "
                         "actor in the audit log: api:<token name>."),
        "parameters": [{"name": "id", "in": "path", "required": True,
                        "schema": {"type": "string"},
                        "description": T("Kapa-ID der Reservierung", "capa ID of the reservation")}],
        "requestBody": {"required": False, "content": {"application/json": {
            "schema": {"type": "object", "properties": {
                "comment": {"type": "string", "maxLength": 64}}}}}},
        "responses": {"200": {"description": "OK"},
                      "401": {"description": T("Token fehlt/ungültig", "token missing/invalid")},
                      "403": {"description": T("Token ohne Schreibrecht", "token lacks write permission")},
                      "404": {"description": T("nicht gefunden oder bereits entschieden", "not found or already decided")}}}


# Selbst-enthaltene, offline lauffähige API-Doku (kein CDN/Swagger-UI nötig);
# rendert /api/v1/openapi.json und bietet ein einfaches „Ausführen" je Endpunkt.
API_DOCS_HTML = _fe("api-docs.html")


LOGIN_TEMPLATE = _fe("login.html")


def reviewer_doc_html(lang="de", version="", role_label="Reviewer"):
    """Selbst-enthaltenes, offline lauffähiges Reviewer-Handbuch (kein CDN),
    zweisprachig (serverseitig nach ?lang=/Accept-Language gerendert), theme-
    treu. Bewusst als eigene Seite (wie die API-Doku) angelegt, damit sie sich
    später leicht ausbauen lässt. Inhalt beschreibt die Reviewer-Sicht/-Rechte."""
    de = lang != "en"
    def L(d, e):
        return d if de else e
    rl = _html_escape(role_label or ("Reviewer" if not de else "Reviewer"))
    title = L("Reviewer-Handbuch", "Reviewer handbook")
    # Abschnitte als (Überschrift, HTML-Inhalt); leicht erweiterbar.
    sections = [
        (L("Deine Rolle", "Your role"),
         L("Als <b>{r}</b> prüfst du Kapazitätsanfragen und gibst sie frei oder "
           "lehnst sie ab. Du gehörst zu einem <b>Team</b> in der Prüfreihenfolge; "
           "handeln kannst du immer dann, wenn <b>dein Team an der Reihe</b> ist. "
           "Anträge stellen oder verwalten gehört nicht zu deiner Rolle.",
           "As a <b>{r}</b> you review capacity requests and approve or reject "
           "them. You belong to a <b>team</b> in the approval order; you can act "
           "whenever <b>your team is up</b>. Creating requests or administration "
           "is not part of your role.").replace("{r}", rl)),
        (L("Wo du arbeitest", "Where you work"),
         L("Deine Arbeitsfläche ist der Tab <b>„Genehmigungen“</b>. Dort siehst du "
           "je Antrag den Ziel-Cluster, die angefragten Ressourcen (vCPU, RAM, "
           "Storage), den Fortschritt und – wenn dein Team dran ist – die Knöpfe "
           "<b>Freigeben</b> und <b>Ablehnen</b>. Über der Tabelle kannst du "
           "<b>suchen</b>; bei vielen Anträgen <b>blätterst</b> du unten seitenweise "
           "(100, 200 oder 300 je Seite) – dasselbe gilt für alle großen Listen.",
           "Your workspace is the <b>“Approvals”</b> tab. For each request you see "
           "the target cluster, the requested resources (vCPU, RAM, storage), the "
           "progress and – when your team is up – the <b>Approve</b> and "
           "<b>Reject</b> buttons. You can <b>search</b> above the table; with many "
           "requests you <b>page</b> through them at the bottom (100, 200 or 300 per "
           "page) – the same applies to all large lists.")),
        (L("Der mehrstufige Prüfprozess", "The multi-stage review process"),
         L("Sind mehrere Teams konfiguriert, durchläuft ein Antrag sie "
           "<b>nacheinander</b>. Der Status wandert von <i>beantragt</i> → "
           "<i>in Prüfung</i> (sobald das erste Team freigegeben hat) → "
           "<i>genehmigt</i> (erst wenn <b>alle</b> Teams zugestimmt haben). Erst "
           "dann zählt der Antrag gegen die Kapazität. Ein <b>Mouseover</b> auf "
           "„in Prüfung“ zeigt, welche Teams (mit Person und Datum) schon "
           "freigegeben haben und wer als Nächstes dran ist. Freigeben kannst du "
           "nur in deiner Stufe; ablehnen kann jedes Team in seiner Stufe.",
           "If several teams are configured, a request passes through them "
           "<b>one after another</b>. The status moves from <i>requested</i> → "
           "<i>in review</i> (once the first team has approved) → <i>approved</i> "
           "(only when <b>all</b> teams have agreed). Only then does it count "
           "against capacity. A <b>mouseover</b> on “in review” shows which teams "
           "(with person and date) have already approved and who is next. You can "
           "approve only in your stage; any team can reject in its stage.")),
        (L("Freigeben & Ablehnen", "Approve & reject"),
         L("Beim Freigeben oder Ablehnen kannst du einen <b>Kommentar</b> "
           "hinterlassen (z. B. den Grund einer Ablehnung) – er steht im "
           "Audit-Log und in den Benachrichtigungen. Eine <b>Ablehnung</b> beendet "
           "den Antrag; er bleibt 31 Tage als Historie sichtbar (im Mouseover "
           "steht, in welcher Stufe abgelehnt wurde).",
           "When approving or rejecting you can leave a <b>comment</b> (e.g. the "
           "reason for a rejection) – it appears in the audit log and in the "
           "notifications. A <b>rejection</b> ends the request; it stays visible "
           "as history for 31 days (the mouseover shows in which stage it was "
           "rejected).")),
        (L("Passt es noch in den Cluster?", "Does it still fit the cluster?"),
         L("Die Übersicht zeigt je Antrag die <b>freie Kapazität</b> des "
           "Ziel-Clusters. Ein <b>⚠</b> warnt, wenn der Antrag rechnerisch nicht "
           "mehr hineinpasst – dann lohnt eine Rückfrage, bevor du freigibst. Die "
           "Freigabe wird dadurch nicht technisch blockiert; die Entscheidung "
           "bleibt bei dir.",
           "The overview shows the <b>free capacity</b> of the target cluster for "
           "each request. A <b>⚠</b> warns when the request no longer fits "
           "numerically – worth a query before you approve. Approval is not "
           "technically blocked by it; the decision stays with you.")),
        (L("Automatische Freigabe", "Automatic approval"),
         L("Administratoren können Stufen so einstellen, dass sie bei genügend "
           "freier Kapazität <b>automatisch</b> freigegeben werden (Freigebender: "
           "„Auto-Freigabe“). Greift eine Schwelle nicht oder fehlen Daten, landet "
           "der Antrag ganz normal bei deinem Team – die Auto-Freigabe lehnt nie "
           "ab und übergeht dich nicht.",
           "Administrators can set stages to be approved <b>automatically</b> when "
           "there is enough free capacity (approver: “Auto-approval”). If a "
           "threshold is not met or data is missing, the request comes to your "
           "team as usual – auto-approval never rejects and never overrides you.")),
        (L("Storage-Erweiterung anfragen", "Requesting a storage expansion"),
         L("Ist die Storage-Funktion aktiv, kannst du beim Freigeben zusätzlich "
           "eine <b>LUN-Vergrößerung oder eine neue LUN</b> anfragen (Knopf "
           "„+ Storage-Erweiterung“ im Freigabe-Dialog). Die Anfrage geht an das "
           "Storage-Team; der Cluster ist bereits vorbelegt.",
           "If the storage feature is active, you can additionally request a "
           "<b>LUN expansion or a new LUN</b> while approving (“+ Storage "
           "expansion” button in the approval dialog). The request goes to the "
           "storage team; the cluster is pre-filled.")),
        (L("Benachrichtigungen", "Notifications"),
         L("Ist ein SMTP-Server eingerichtet, wird deine <b>Team-Adresse</b> "
           "angeschrieben, sobald dein Team an der Reihe ist. Wartet ein Antrag zu "
           "lange, verschickt das System eine <b>Erinnerung</b>. Beides steuern "
           "Administratoren.",
           "If an SMTP server is configured, your <b>team address</b> is notified "
           "as soon as your team is up. If a request waits too long, the system "
           "sends a <b>reminder</b>. Both are managed by administrators.")),
        (L("Was du (standardmäßig) nicht siehst", "What you don’t see (by default)"),
         L("Verwaltung und Log bleiben Administratoren vorbehalten. <b>Host- und "
           "VM-Listen</b> in der Cluster-Detailansicht sind für Reviewer "
           "standardmäßig ausgeblendet; über die <b>Sichtbarkeits-Matrix</b> "
           "können Administratoren das je Rolle anpassen. Die reinen Zählwerte "
           "(Anzahl Hosts/VMs) bleiben sichtbar.",
           "Administration and log stay with administrators. <b>Host and VM "
           "lists</b> in the cluster detail view are hidden for reviewers by "
           "default; administrators can adjust this per role via the <b>visibility "
           "matrix</b>. The plain counts (number of hosts/VMs) stay visible.")),
        (L("Stornieren statt löschen", "Cancel instead of delete"),
         L("Anfragen werden nicht gelöscht, sondern <b>storniert</b> (durch Admin, "
           "die anfragende Person oder jemanden aus demselben Team). Eine "
           "stornierte Anfrage bleibt als Historie erhalten und zählt nicht mehr "
           "gegen die Kapazität.",
           "Requests are not deleted but <b>cancelled</b> (by an admin, the "
           "requesting person or someone from the same team). A cancelled request "
           "remains as history and no longer counts against capacity.")),
    ]
    body = "\n".join(
        f'<div class="sec"><h2>{h}</h2><p>{c}</p></div>' for h, c in sections)
    sub = L("Kurzanleitung für die Rolle „%s“ · v%s" % (rl, _html_escape(version)),
            "Quick guide for the “%s” role · v%s" % (rl, _html_escape(version)))
    foot = L("Diese Seite lässt sich erweitern – Rückfragen und Wünsche gern an "
             "die Administration. Self-contained, offline nutzbar.",
             "This page can be extended – questions and suggestions welcome to "
             "the administration. Self-contained, usable offline.")
    lang_attr = "de" if de else "en"
    return ("""<!DOCTYPE html>
<html lang="%s">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>try { var _t = new URLSearchParams(location.search).get("theme")
  || localStorage.getItem("kapa_theme");
  if (!_t && window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches) _t = "light";
  if (_t === "light") document.documentElement.setAttribute("data-theme", "light");
} catch (e) {}</script>
<title>%s</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --text:#e2e8f0;
          --muted:#94a3b8; --accent:#38bdf8; }
  html[data-theme="light"] { --bg:#eef2f7; --card:#ffffff; --line:#d4dbe5;
          --text:#1e293b; --muted:#5b6b7f; --accent:#0369a1; }
  * { box-sizing:border-box; }
  body { background:var(--bg); color:var(--text);
         font:15px/1.6 "Segoe UI",system-ui,sans-serif; margin:0; padding:28px; }
  .wrap { max-width:820px; margin:0 auto; }
  h1 { font-size:22px; margin:0 0 2px; }
  .sub { color:var(--muted); margin-bottom:22px; }
  a { color:var(--accent); }
  .sec { background:var(--card); border:1px solid var(--line); border-radius:12px;
         padding:14px 18px; margin-bottom:14px; }
  .sec h2 { font-size:16px; margin:0 0 6px; }
  .sec p { margin:0; color:var(--text); }
  .sec b { color:var(--text); }
  .foot { color:var(--muted); font-size:13px; margin-top:20px; }
  .back { display:inline-block; margin-bottom:18px; font-size:13px; }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="./">%s</a>
  <h1>%s</h1>
  <div class="sub">%s</div>
  %s
  <div class="foot">%s</div>
</div>
</body>
</html>
""" % (lang_attr, title,
       L("← Zurück zum Dashboard", "← Back to the dashboard"),
       title, sub, body, foot))


HTML_TEMPLATE = _fe("index.html")


def json_for_html(obj):
    """JSON so kodieren, dass es sicher in ein <script>-Tag eingebettet werden
    kann. json.dumps escaped `<`, `>`, `&` und die Zeilentrenner U+2028/U+2029
    nicht – ohne diese Ersetzung könnte ein aus Aria stammender Name wie
    `</script>...` aus dem Script-Tag ausbrechen (Stored XSS)."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))



def int_or(default):
    """argparse-Typ für Zahlen: ein leerer Wert (z. B. eine nicht gesetzte
    Umgebungsvariable, die systemd als '' durchreicht) ergibt den Standard,
    statt den Dienststart mit 'invalid int value' abzubrechen."""
    def conv(s):
        s = str(s or "").strip()
        return default if not s else int(s)
    return conv


def _drop_empty_long_opts(argv):
    """Langoptionen mit leerem Wert aus der Argumentliste entfernen.

    Die systemd-Unit baut Argumente wie '--backup-target ${BACKUP_TARGET}' aus
    kapa.env. Ist die Variable leer, käme '--backup-target ''' an und würde einen
    in der INI gesetzten Wert überschreiben (leer schlägt INI). Ein leerer Wert
    bedeutet in dieser App überall 'nicht gesetzt' – wir lassen ihn hier weg,
    damit stattdessen INI bzw. eingebauter Standard greift. '--opt=' und
    '--opt ''' werden beide verworfen; '--opt=wert' und Flags bleiben."""
    out, i, n = [], 0, len(argv)
    while i < n:
        a = argv[i]
        if a.startswith("--") and "=" in a:
            if a.split("=", 1)[1] == "":
                i += 1
                continue
            out.append(a)
        elif a.startswith("--") and i + 1 < n and argv[i + 1] == "":
            i += 2                     # '--opt' und der leere Folgewert: beide weg
            continue
        else:
            out.append(a)
        i += 1
    return out


def render_html(clusters, cpu_factor, serve_mode=False, updated=None, res_ttl=31,
                failover_hosts=1, userinfo=None, teams=None, rolenames=None,
                contact="", selector=None, backup=False, notify=None, prefs=None,
                announce=None, tanzu_mhz=2500, vis=None, sources=None):
    valid_days = res_ttl - 1 if res_ttl > 0 else 30
    resnote = (f"Neue Reservierungen gelten ab dem Anlagetag für {valid_days} Tage, "
               "zählen erst nach Genehmigung gegen die Kapazität und werden "
               + (f"nach {res_ttl} Tagen automatisch entfernt. " if res_ttl > 0
                  else "nicht automatisch entfernt. "))
    resnote += ("Speicherung zentral auf dem Server." if serve_mode
                else "Speicherung lokal im Browser.")
    if teams:
        resnote += (" Genehmigung mehrstufig: " + " → ".join(teams)
                    + " (erst wenn alle freigegeben haben, ist der Antrag genehmigt).")
    if failover_hosts == 1:
        failnote = " · Ausfallreserve (N+1): größter Host je Cluster abgezogen"
    elif failover_hosts > 1:
        failnote = (f" · Ausfallreserve (N+{failover_hosts}): größte {failover_hosts} "
                    "Hosts je Cluster abgezogen")
    else:
        failnote = ""
    return (HTML_TEMPLATE
            .replace("__DATA__", json_for_html(clusters))
            .replace("__FACTOR__", str(cpu_factor))
            .replace("__TANZU_MHZ__", str(int(tanzu_mhz)))
            .replace("__SERVE__", "true" if serve_mode else "false")
            .replace("__TTL__", str(res_ttl))
            .replace("__USERINFO__", json_for_html(userinfo))
            .replace("__TEAMS__", json_for_html(teams or []))
            .replace("__SELECTOR__", json_for_html(selector or []))
            .replace("__BACKUP__", "true" if backup else "false")
            .replace("__ROLENAMES__", json_for_html(rolenames or DEFAULT_ROLE_NAMES))
            .replace("__SOURCES__", json_for_html(sources or []))
            .replace("__NOTIFY__", json_for_html(notify or DEFAULT_NOTIFY))
            .replace("__PREFS__", json_for_html(prefs or {}))
            .replace("__ANNOUNCE__", json_for_html(announce))
            .replace("__VIS__", json_for_html(vis or {f: True for f in VIS_FEATURES}))
            .replace("__RESNOTE__", resnote)
            .replace("__FAILNOTE__", failnote)
            .replace("__VERSION__", VERSION)
            .replace("__CONTACT_FOOT__",
                     " · " + _html_escape(contact) if contact else "")
            .replace("__DATE__", updated or datetime.now().strftime("%d.%m.%Y %H:%M")))


def render_dashboard(clusters, cpu_factor, path, res_ttl=31, failover_hosts=1,
                     contact="", tanzu_mhz=2500):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(clusters, cpu_factor, res_ttl=res_ttl,
                            failover_hosts=failover_hosts, contact=contact,
                            tanzu_mhz=tanzu_mhz))

# ------------------------------------------------------------- Serve-Modus ---

def serve(args, password):
    import secrets
    import threading
    import time
    import uuid
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    def new_res_id():
        """Kapa-ID: konfigurierbares Präfix + Zufallsteil (Hex) in konfigurierter
        Länge (--id-prefix / --id-length bzw. INI)."""
        h = ""
        while len(h) < args.id_length:
            h += uuid.uuid4().hex
        return args.id_prefix + h[:args.id_length]

    state = {"clusters": [], "updated": None, "refreshing": False,
             "progress": "", "error": None, "last": None}
    interval = max(0, args.refresh_interval)
    migrate_data_files(args.cache, args.res_file, args.roles_file)

    # ---- Datenspeicher (JSON-Dateien oder SQLite) ----
    _coll_paths = {"res": args.res_file, "roles": args.roles_file,
                   "teams": args.teams_file, "selector": args.selector_file,
                   "rolenames": args.rolenames_file, "tokens": args.tokens_file,
                   "notify": args.notify_file, "webhooks": args.webhooks_file,
                   "prefs": args.prefs_file,
                   "announce": args.announce_file,
                   "autoapprove": args.autoapprove_file,
                   "sessions": args.sessions_file,
                   "visibility": args.visibility_file,
                   "storagecfg": args.storagecfg_file,
                   "storagereq": args.storagereq_file,
                   "netcfg": args.netcfg_file,
                   "manual": args.import_file,
                   "history": args.history_file,
                   "refreshcfg": args.refreshcfg_file}
    if args.storage == "sqlite":
        store = SqliteStore(args.db_file)
        # Einmal-Migration: vorhandene JSON-Daten in die (leere) DB übernehmen
        _MISS = object()
        for _n in ("roles", "teams", "selector", "rolenames", "tokens", "notify",
                   "webhooks", "prefs", "announce", "autoapprove", "visibility",
                   "storagecfg", "storagereq"):
            p = _coll_paths[_n]
            if os.path.exists(p) and store.load(_n, _MISS) is _MISS:
                try:
                    with open(p, encoding="utf-8") as f:
                        store.save(_n, json.load(f))
                    print(f"Migriert nach SQLite: {os.path.basename(p)}",
                          file=sys.stderr)
                except Exception as e:
                    print(f"Migration {p} fehlgeschlagen: {e}", file=sys.stderr)
        if os.path.exists(args.res_file) and not store.res_load():
            try:
                with open(args.res_file, encoding="utf-8") as f:
                    _lst = json.load(f)
                if isinstance(_lst, list) and _lst:
                    for _r in _lst:
                        if isinstance(_r, dict):
                            _r.setdefault("id", new_res_id())
                    store.res_save_all(_lst)
                    print(f"Migriert nach SQLite: {len(_lst)} Reservierungen",
                          file=sys.stderr)
            except Exception as e:
                print(f"Migration Reservierungen fehlgeschlagen: {e}", file=sys.stderr)
        print(f"Datenspeicher: SQLite ({args.db_file})", file=sys.stderr)
    else:
        store = JsonStore(_coll_paths)

    # ---- AD-Anmeldung, Sessions und Rollen ----
    auth_enabled = bool(args.ad_url)
    admin_seed = {u.strip().lower() for u in (args.admin_user or "").split(",") if u.strip()}
    # Sitzungen überleben einen Neustart (Release-Update!): persistiert wird
    # unter dem SHA-256-HASH des Cookie-Tokens (nie das Token selbst), Ablauf
    # wird beim Laden ausgesiebt. Gleitende Verlängerung landet gesammelt über
    # den Wartungs-Thread auf Platte (kein Write je Request).
    session_ttl = 12 * 3600

    def _sess_key(token):
        return hashlib.sha256(str(token or "").encode()).hexdigest()

    def _load_sessions():
        raw = store.load("sessions", None)
        out = {}
        now = time.time()
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict) and float(v.get("exp") or 0) > now:
                    out[str(k)] = v
        return out

    sessions = _load_sessions()      # sha256(token) -> {"user", "role", "exp", ...}
    sessions_dirty = [bool(sessions)]

    def save_sessions():
        store.save("sessions", dict(sessions))
        sessions_dirty[0] = False
    if sessions:
        print(f"Sitzungen geladen: {len(sessions)} (überleben Neustart)",
              file=sys.stderr)
    roles_lock = threading.Lock()
    VALID_ROLES = ("admin", "anforderer", "auditor", "reviewer")

    # ---- Sichtbarkeits-Matrix (Verwaltung -> Sichtbarkeit) ----
    VIS_ROLES = tuple(r for r in VALID_ROLES if r != "admin")
    visibility_lock = threading.Lock()

    def clean_visibility(raw):
        raw = raw if isinstance(raw, dict) else {}
        out = {}
        for role in VIS_ROLES:
            base = dict(DEFAULT_VISIBILITY.get(role)
                        or {f: True for f in VIS_FEATURES})
            got = raw.get(role) if isinstance(raw.get(role), dict) else {}
            for f in VIS_FEATURES:
                base[f] = bool(got.get(f, base.get(f, True)))
            out[role] = base
        return out

    visibility_cfg = clean_visibility(store.load("visibility", None))

    def save_visibility():
        store.save("visibility", visibility_cfg)

    def vis_for(role):
        """Effektive Sichtbarkeits-Flags einer Rolle. Admin und der Betrieb
        ohne Anmeldung sehen immer alles."""
        if not role or role == "admin":
            return {f: True for f in VIS_FEATURES}
        with visibility_lock:
            return dict(visibility_cfg.get(role)
                        or {f: True for f in VIS_FEATURES})

    # Mehrstufiger Genehmigungsprozess: Team-Namen (in Prüfreihenfolge) werden
    # auf der Admin-Seite gepflegt und in data/kapa_teams.json abgelegt.
    # --approval-teams dient nur noch als Erstbefüllung, falls die Datei fehlt.
    # Leer = einstufig (Admin genehmigt direkt). In-place mutiert, damit die
    # Closures (current_team, approve/reject) immer den aktuellen Stand sehen.
    teams_lock = threading.Lock()

    def clean_teams(seq):
        out = []
        for t in seq:
            t = str(t or "").strip()
            if t and t not in out:
                out.append(t)
        return out

    _MISS = object()

    def load_teams():
        d = store.load("teams", _MISS)
        if d is not _MISS:
            return clean_teams(d) if isinstance(d, list) else []
        # Kein Bestand: einmalig aus --approval-teams befüllen (Migration)
        seed = clean_teams((args.approval_teams or "").split(","))
        if seed:
            try:
                store.save("teams", seed)
            except OSError as e:
                print(f"Team-Datei nicht schreibbar: {e}", file=sys.stderr)
        return seed

    def save_teams():
        store.save("teams", approval_teams)

    approval_teams = load_teams()

    # ---- Cluster-Selektor: bis zu 3 Tag-Kategorien (kaskadierender Filter) ----
    selector_lock = threading.Lock()

    def clean_selector(seq):
        # Einträge: {"category": "<Tag-Kategorie>", "label": "<Anzeigename>"}.
        # Alte Form (Liste von Strings) wird migriert: label = category.
        out, seen = [], set()
        for item in seq:
            if isinstance(item, str):
                cat = label = item.strip()
            elif isinstance(item, dict):
                cat = str(item.get("category") or "").strip()
                label = str(item.get("label") or "").strip() or cat
            else:
                continue
            if cat and cat not in seen:
                seen.add(cat)
                out.append({"category": cat, "label": label})
        return out[:3]

    def load_selector():
        d = store.load("selector", None)
        if isinstance(d, list):
            return clean_selector(d)
        return []

    def save_selector():
        store.save("selector", cluster_selector)

    cluster_selector = load_selector()

    # ---- Frei wählbare Rollen-Bezeichnungen (Schlüssel bleiben fest) ----
    rolenames_lock = threading.Lock()

    def load_rolenames():
        d = dict(DEFAULT_ROLE_NAMES)
        raw = store.load("rolenames", None)
        if isinstance(raw, dict):
            for k in ROLE_KEYS:
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    d[k] = v.strip()
        return d

    def save_rolenames():
        store.save("rolenames", role_names)

    role_names = load_rolenames()

    # ---- Mail-Benachrichtigungsregeln (je interner Rolle + Team-Adressen) ----
    notify_lock = threading.Lock()

    def _merge_notify(raw):
        cfg = {"role": {}, "team_email": {}}
        rawrole = (raw or {}).get("role") if isinstance(raw, dict) else None
        rawrole = rawrole if isinstance(rawrole, dict) else {}
        for role, events in NOTIFY_ROLE_EVENTS.items():
            base = dict(DEFAULT_NOTIFY["role"][role])
            got = rawrole.get(role) if isinstance(rawrole.get(role), dict) else {}
            for ev in events:
                base[ev] = bool(got.get(ev, base.get(ev, False)))
            if role in ("admin", "auditor"):
                base["email"] = str(got.get("email") or "").strip()[:200]
            cfg["role"][role] = base
        rawmail = (raw or {}).get("team_email") if isinstance(raw, dict) else None
        if isinstance(rawmail, dict):
            for k, v in rawmail.items():
                cfg["team_email"][str(k)] = str(v or "").strip()[:200]
        # Editierbare Mail-Vorlage (leer = eingebaute Standardvorlage). HTML wird
        # NICHT escaped (Layout vom Admin); die Werte-Variablen sind escaped.
        if isinstance(raw, dict):
            th = raw.get("template_html")
            ts = raw.get("template_subject")
            if isinstance(th, str) and th.strip():
                cfg["template_html"] = th[:20000]
            if isinstance(ts, str) and ts.strip():
                cfg["template_subject"] = ts.strip()[:300]
        # Erinnerungs-Intervall (Tage Wartezeit bis zur Reminder-Mail, dann
        # alle N Tage erneut) – 1..30, Standard 2
        try:
            days = int((raw or {}).get("reminder_days"))
        except (TypeError, ValueError):
            days = DEFAULT_NOTIFY["reminder_days"]
        cfg["reminder_days"] = min(30, max(1, days))
        return cfg

    def load_notify():
        return _merge_notify(store.load("notify", None))

    def save_notify():
        store.save("notify", notify_cfg)

    notify_cfg = load_notify()

    # ---- Ausgehende Webhooks (Ereignisse an externe Systeme/Pipelines) ----
    # Bei einem Ereignis (Reservierungs-Lebenszyklus, Storage-Anfragen, Import)
    # schickt das Dashboard einen HTTP-POST mit signiertem JSON an konfigurierte
    # Ziele. Nur Admins pflegen die Ziele; die Secrets liegen bewusst NICHT im
    # Backup (wie kapa_sessions.json). Die Signatur (HMAC-SHA256) erlaubt dem
    # Empfänger, die Echtheit zu prüfen.
    webhooks_lock = threading.Lock()

    def _clean_webhooks(raw):
        if isinstance(raw, dict):
            raw = raw.get("endpoints")
        if not isinstance(raw, list):
            return []
        oneline = lambda v, n: " ".join(str(v or "").split())[:n]
        out, seen = [], set()
        for e in raw[:50]:
            if not isinstance(e, dict):
                continue
            url = oneline(e.get("url"), 400)
            if not re.match(r"^https?://", url, re.I):
                continue
            wid = re.sub(r"[^A-Za-z0-9]", "", str(e.get("id") or ""))[:16] \
                or uuid.uuid4().hex[:8]
            if wid in seen:
                wid = uuid.uuid4().hex[:8]
            seen.add(wid)
            evs = [x for x in (e.get("events") or []) if x in WEBHOOK_EVENTS]
            out.append({"id": wid, "url": url,
                        "secret": str(e.get("secret") or "")[:200],
                        "events": evs, "active": bool(e.get("active", True)),
                        "desc": oneline(e.get("desc"), 80)})
        return out

    webhook_cfg = _clean_webhooks(store.load("webhooks", None))

    def save_webhooks():
        with webhooks_lock:
            store.save("webhooks", {"endpoints": webhook_cfg})

    def _deliver_webhook(ep, event, body):
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "User-Agent": "kapa-dashboard/" + VERSION,
                   "X-Kapa-Event": event,
                   "X-Kapa-Delivery": uuid.uuid4().hex}
        if ep.get("secret"):
            sig = hmac.new(ep["secret"].encode("utf-8"), body,
                           hashlib.sha256).hexdigest()
            headers["X-Kapa-Signature"] = "sha256=" + sig
        last = ""
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(ep["url"], data=body,
                                             method="POST", headers=headers)
                # nosec B310: Schema in _clean_webhooks auf http/https begrenzt
                with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310
                    code = resp.status
                audit(None, "Webhook gesendet",
                      f"{event} → {ep['url']} (HTTP {code})")
                return
            except Exception as ex:
                last = str(ex)
                if attempt == 1:
                    time.sleep(1)
        audit(None, "Webhook fehlgeschlagen", f"{event} → {ep['url']}: {last}")

    def fire_webhooks(event, payload):
        """Ereignis an alle passenden Ziele senden (Hintergrund, nicht blockierend)."""
        if event not in WEBHOOK_EVENTS:
            return
        with webhooks_lock:
            targets = [dict(e) for e in webhook_cfg
                       if e.get("active") and event in (e.get("events") or [])]
        if not targets:
            return
        doc = {"event": event,
               "at": datetime.now().isoformat(timespec="seconds")}
        doc.update(payload)
        body = json.dumps(doc, ensure_ascii=False).encode("utf-8")

        def worker():
            for ep in targets:
                _deliver_webhook(ep, event, body)
        threading.Thread(target=worker, daemon=True).start()

    # ---- Persönliche UI-Einstellungen je Benutzer (z. B. Tabellenspalten) ----
    prefs_lock = threading.Lock()

    def load_all_prefs():
        d = store.load("prefs", None)
        return d if isinstance(d, dict) else {}

    all_prefs = load_all_prefs()

    def clean_prefs(body):
        """Nur erlaubte, kompakte Struktur speichern: cols = {tableId: {index: true}}."""
        out = {}
        cols = (body or {}).get("cols") if isinstance(body, dict) else None
        if isinstance(cols, dict):
            c = {}
            for tid, hidden in list(cols.items())[:20]:
                tid = str(tid)[:40]
                if isinstance(hidden, dict):
                    idxs = {}
                    for k, v in list(hidden.items())[:60]:
                        if str(k).lstrip("-").isdigit() and v:
                            idxs[str(int(k))] = True
                    if idxs:
                        c[tid] = idxs
            out["cols"] = c
        seen = (body or {}).get("announce_seen") if isinstance(body, dict) else None
        if isinstance(seen, str) and seen.strip():
            out["announce_seen"] = seen.strip()[:16]
        theme = (body or {}).get("theme") if isinstance(body, dict) else None
        if theme in ("light", "dark"):
            out["theme"] = theme
        return out

    def user_prefs(user):
        with prefs_lock:
            return json.loads(json.dumps(all_prefs.get(user or "", {})))

    # ---- Ankündigung (Popup nach der Anmeldung, einmal je Benutzer) ----
    announce_lock = threading.Lock()

    def clean_announce(raw, actor=""):
        """Nur die erlaubten Felder übernehmen. Die id ist ein Hash aus
        Titel+Text: Textänderung -> neue id -> jeder sieht das Popup erneut."""
        raw = raw if isinstance(raw, dict) else {}
        title = " ".join(str(raw.get("title") or "").split())[:120]
        text = str(raw.get("text") or "").strip()[:2000]
        return {"active": bool(raw.get("active")) and bool(text),
                "title": title, "text": text,
                "id": hashlib.sha256((title + "\n" + text).encode()).hexdigest()[:8],
                "updated_on": (datetime.now().strftime("%d.%m.%Y %H:%M") if actor
                               else str(raw.get("updated_on") or "")),
                "updated_by": actor or str(raw.get("updated_by") or "")}

    announce_cfg = clean_announce(store.load("announce", None))

    def save_announce():
        store.save("announce", announce_cfg)

    def public_announce():
        """Nur die aktive Ankündigung, reduziert auf das, was Clients brauchen."""
        with announce_lock:
            if not announce_cfg.get("active"):
                return None
            return {"id": announce_cfg["id"], "title": announce_cfg["title"],
                    "text": announce_cfg["text"]}

    def current_team(r):
        """Team, das als Nächstes freigeben muss – None, wenn keine Teams
        konfiguriert sind oder alle Stufen bereits freigegeben haben."""
        if not approval_teams:
            return None
        n = len(r.get("approvals") or [])
        return approval_teams[n] if n < len(approval_teams) else None

    # ---- Brute-Force-Bremse für den Login ----
    # Pro Schlüssel (Benutzer bzw. Absender-IP) werden Fehlversuche gezählt;
    # ab LOGIN_MAX innerhalb von LOGIN_WINDOW Sekunden wird für die Restzeit
    # des Fensters gesperrt. Verhindert Password-Spraying gegen das AD und ein
    # versehentliches Aussperren echter AD-Konten.
    LOGIN_MAX = 5
    LOGIN_WINDOW = 300
    login_fails = {}                 # key -> [Zeitstempel, ...]
    login_lock = threading.Lock()

    def login_blocked(*keys):
        now = time.time()
        with login_lock:
            for k in keys:
                hits = [t for t in login_fails.get(k, []) if now - t < LOGIN_WINDOW]
                login_fails[k] = hits
                if len(hits) >= LOGIN_MAX:
                    return True
            return False

    def login_note_fail(*keys):
        now = time.time()
        with login_lock:
            for k in keys:
                hits = [t for t in login_fails.get(k, []) if now - t < LOGIN_WINDOW]
                hits.append(now)
                login_fails[k] = hits

    def login_reset(*keys):
        with login_lock:
            for k in keys:
                login_fails.pop(k, None)

    def known_sources():
        """Bekannte vROps-Quellnamen für die Quellen-Freigabe: aus den
        konfigurierten [quelle:*]-Sektionen UND den tatsächlich in den Daten
        vorkommenden Quellen (deckt auch Offline-Importe und die Demo ab).
        Leer bzw. eine einzige Quelle = Feature inert."""
        names = {str(sc.get("name")).strip()
                 for sc in (args.sources or [])
                 if sc.get("name") and str(sc.get("name")).strip()}
        names |= {c.get("source") for c in state.get("clusters") or []
                  if c.get("source")}
        return sorted(names)

    def clean_sources(raw):
        """Freigegebene Quellnamen einer Zuweisung -> saubere Liste (Strings
        getrimmt, dedupliziert). Leer = keine Einschränkung / alle Quellen.
        Bewusst OHNE Namensvalidierung: ein noch unbekannter Name (Cluster
        gerade nicht im Datenstand) darf nicht verloren gehen; er trifft dann
        schlicht keinen Cluster."""
        if not isinstance(raw, (list, tuple)):
            return []
        out, seen = [], set()
        for x in raw:
            n = " ".join(str(x or "").split())[:120]
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return out[:30]

    def load_roles():
        """Rollen: {benutzer: {role, abteilung}}; alte Form {benutzer: rolle}
        wird beim Laden migriert."""
        d = store.load("roles", None)
        if isinstance(d, dict):
            out = {}
            for k, v in d.items():
                if isinstance(v, str) and v in VALID_ROLES:
                    out[str(k).lower()] = {"role": v, "abteilung": "",
                                           "kind": "user"}
                elif isinstance(v, dict) and v.get("role") in VALID_ROLES:
                    kind = "group" if v.get("kind") == "group" else "user"
                    key = str(k) if kind == "group" else str(k).lower()
                    out[key] = {"role": v["role"],
                                "abteilung": str(v.get("abteilung") or ""),
                                "kind": kind,
                                "sources": clean_sources(v.get("sources"))}
            return out
        return {}

    def save_roles():
        store.save("roles", roles)

    roles = load_roles()

    def normalize_user(name):
        name = str(name or "").strip().lower()
        if name and "@" not in name and "\\" not in name and args.ad_domain:
            name = name + "@" + args.ad_domain
        return name

    def suspicious_login_name(raw):
        """Heuristik: Sieht die Eingabe im Benutzername-Feld wie ein
        versehentlich eingefügtes PASSWORT aus (Passwort-Manager-Klassiker)?
        Dann weder ans AD schicken noch den Wert protokollieren — sonst stünde
        das Passwort im Klartext im Audit-Log. Bewusst einfach gehalten:
        AD-Namen bestehen aus Buchstaben/Ziffern/. @ \\ - _ und haben Struktur;
        Passwörter haben Sonderzeichen oder sind lange Zufallsketten."""
        u = str(raw or "").strip()
        if not u:
            return False
        if len(u) > 64:
            return True
        # Sonderzeichen/Leerzeichen, die in AD-Anmeldenamen nicht vorkommen
        if re.search(r"[^A-Za-z0-9.@\\\-_]", u):
            return True
        # lange Zufallskette ohne Namens-Trenner (kein . @ \), aber mit
        # Groß-/Kleinschreibung UND Ziffern gemischt
        if (len(u) >= 15 and not re.search(r"[.@\\]", u)
                and re.search(r"[a-z]", u) and re.search(r"[A-Z]", u)
                and re.search(r"\d", u)):
            return True
        return False

    ROLE_RANK = {"admin": 3, "reviewer": 2, "auditor": 1, "anforderer": 0}

    def role_entry(user):
        """Direkt zugewiesene Benutzerrolle (keine Gruppen)."""
        if user in admin_seed:
            return {"role": "admin", "abteilung": ""}
        e = roles.get(user)
        if e and e.get("kind", "user") != "group":
            return {"role": e["role"], "abteilung": e.get("abteilung", "")}
        return None

    def group_role(cns):
        """Beste Rolle aus den AD-Gruppen des Benutzers (höchste Berechtigung)."""
        cnset = {c.strip().lower() for c in cns if c and c.strip()}
        best = None
        with roles_lock:
            for key, e in roles.items():
                if e.get("kind") == "group" and key.strip().lower() in cnset:
                    if best is None or ROLE_RANK.get(e["role"], 0) > ROLE_RANK.get(best["role"], 0):
                        best = e
        if best:
            return {"role": best["role"], "abteilung": best.get("abteilung", "")}
        return None

    def effective_sources(user, cns):
        """Vereinigung der freigegebenen Quellen aus dem Direkt-Eintrag und
        ALLEN passenden Gruppen-Einträgen. Ein Treffer ohne Einschränkung
        (leere Liste) => alle Quellen. Rückgabe: Liste (leer = alle)."""
        cnset = {c.strip().lower() for c in (cns or []) if c and c.strip()}
        matched = []
        with roles_lock:
            de = roles.get(user)
            if de and de.get("kind", "user") != "group":
                matched.append(de)
            for key, e in roles.items():
                if e.get("kind") == "group" and key.strip().lower() in cnset:
                    matched.append(e)
        if not matched:
            return []
        allow = set()
        for e in matched:
            src = e.get("sources") or []
            if not src:
                return []          # ein Treffer = alle -> keine Einschränkung
            allow.update(src)
        return sorted(allow)

    def has_group_entries():
        with roles_lock:
            return any(e.get("kind") == "group" for e in roles.values())

    # AD-Mailadresse eines Benutzers (aus --ad-mail-attribute) auflösen, gecacht.
    # Dient auch der Sichtkontrolle in der Verwaltung (Mouseover je Benutzer).
    user_mail_cache = {}

    def resolve_user_mail(upn):
        if not (args.ad_mail_attribute and args.ad_bind_dn and upn):
            return ""
        if upn not in user_mail_cache:
            m = ""
            try:
                m = ldap_user_attr(args.ad_url, args.ad_bind_dn,
                                   args.ad_bind_password, args.ad_base_dn, upn,
                                   args.ad_mail_attribute,
                                   insecure=args.ad_insecure) or ""
            except Exception as e:
                print(f"AD-Mail für {upn} nicht auflösbar: {e}", file=sys.stderr)
            user_mail_cache[upn] = m
        return user_mail_cache[upn]

    def roles_with_mail():
        """Kopie der Rollen; Benutzer-Einträge um die aufgelöste AD-Mail
        ergänzt (nur wenn --ad-mail-attribute gesetzt ist)."""
        with roles_lock:
            items = list(roles.items())
        out = {}
        for k, v in items:
            e = dict(v)
            if e.get("kind", "user") != "group":
                m = resolve_user_mail(k)
                if m:
                    e["mail"] = m
            out[k] = e
        return out

    def public_config():
        """Im System gesetzte Werte für die schreibgeschützte Konfig-Ansicht.
        NIE Passwörter/Geheimnisse – nur, ob sie gesetzt sind (ja/nein)."""
        j = lambda b: "ja" if b else "nein"
        pw = lambda name: j(bool(getattr(args, name, "")))
        aria = {}
        for i, s in enumerate(sources):
            nm = s["name"] or "Standard"
            aria[f"{i + 1}. {nm}"] = (
                (s["url"] or "–")
                + (f" · Proxy {s['aria_proxy']}" if s["aria_proxy"] else " · direkt")
                + f" · Benutzer {s['user'] or '–'}"
                + " · Passwort " + j(bool(s["password"])))
        aria["Auto-Refresh (Sek.)"] = args.refresh_interval
        return {
            "Datenquellen (vROps)": aria or {"–": "keine Quelle konfiguriert"},
            "Berechnung": {
                "CPU-Faktor": args.cpu_factor,
                "Failover-Hosts (N+1)": args.failover_hosts,
                "vSAN-Faktor": args.vsan_factor,
                "Reservierung gültig (Tage)": args.res_ttl_days,
                "Uplink-Portgruppen anzeigen": j(args.show_uplink_portgroups),
                "Ausschluss-Tag": args.exclude_tag or "–",
                "Tag-Präfix": args.tag_property or "(alle mit 'tag')",
            },
            "Mail / SMTP": {
                "SMTP-Server": args.smtp_server or "– (kein Mailversand)",
                "Absender": args.smtp_from or "–",
                "smtp-to (Fallback Admin)": args.smtp_to or "–",
                "STARTTLS": j(args.smtp_tls),
                "SMTP-Passwort gesetzt": pw("smtp_password"),
                "AD-Mail-Attribut": args.ad_mail_attribute or "– (UPN)",
            },
            "Backup": {
                "Ziel": args.backup_target or "– (kein Backup)",
                "Port": args.backup_port,
                "Intervall (Sek.)": args.backup_interval,
                "Aufbewahrung (Tage)": args.backup_keep_days,
                "SSH-Key": args.backup_key or "–",
                "Backup-Passwort gesetzt": pw("backup_password"),
            },
            "Active Directory": {
                "URL": args.ad_url or "– (keine AD-Anmeldung)",
                "Domäne": args.ad_domain or "–",
                "Admin-User": args.admin_user or "–",
                "Service-Konto (Bind-DN)": args.ad_bind_dn or "–",
                "Basis-DN": args.ad_base_dn or "–",
                "Bind-Passwort gesetzt": pw("ad_bind_password"),
            },
            "Server / Speicher": {
                "Bind-Adresse": args.bind,
                "Port": args.port,
                "Datenspeicher": args.storage,
                "Daten-Verzeichnis": args.data_dir or "data",
                "Kontakt / Impressum": args.contact_info or "–",
            },
        }

    if auth_enabled:
        if not args.ad_url.startswith("ldaps://"):
            print("WARNUNG: --ad-url ohne ldaps:// – Passwörter gehen unverschlüsselt "
                  "über das Netz.", file=sys.stderr)
        if not admin_seed and not roles:
            print("WARNUNG: Kein Admin definiert (--admin-user) und Rollendatei leer – "
                  "alle AD-Nutzer melden sich als Anforderer an, niemand kann "
                  "genehmigen oder die Verwaltung öffnen.", file=sys.stderr)

    # Zwischen-Cache der letzten Abfrage von Platte laden
    if os.path.exists(args.cache):
        try:
            with open(args.cache, encoding="utf-8") as f:
                c = json.load(f)
            state["clusters"] = c.get("clusters", [])
            state["updated"] = c.get("updated")
            state["last"] = os.path.getmtime(args.cache)
            print(f"Cache geladen: {args.cache} (Stand {state['updated']}, "
                  f"{len(state['clusters'])} Cluster)", file=sys.stderr)
        except Exception as e:
            print(f"Cache unlesbar, starte leer: {e}", file=sys.stderr)

    # ---- Reservierungen: serverseitige Datei, Ablauf nach --res-ttl-days ----
    res_lock = threading.Lock()

    def prune_res(lst):
        if args.res_ttl_days <= 0:
            return lst
        cutoff = (datetime.now() - timedelta(days=args.res_ttl_days)).date().isoformat()
        # Abgelehnte und stornierte Anfragen bleiben DAUERHAFT im Archiv erhalten;
        # nur aktive/genehmigte laufen nach --res-ttl-days ab (geben Kapazität frei).
        return [r for r in lst
                if r.get("rejected") or r.get("cancelled")
                or str(r.get("created") or "9999") >= cutoff]

    def load_res():
        try:
            lst = store.res_load()
        except Exception as e:
            print(f"Reservierungen unlesbar, starte leer: {e}", file=sys.stderr)
            return []
        changed = False
        for r in lst:
            if isinstance(r, dict) and "id" not in r:
                r["id"] = new_res_id()
                changed = True
        kept = prune_res(lst)
        # Einmalige Reconciliation beim Start (nachgerüstete IDs / abgelaufene
        # Einträge dauerhaft festschreiben, damit der Speicher konsistent ist).
        if changed or len(kept) != len(lst):
            try:
                store.res_save_all(kept)
            except Exception as e:
                print(f"Reservierungen konnten nicht bereinigt werden: {e}",
                      file=sys.stderr)
        print(f"Reservierungen geladen: {len(kept)}", file=sys.stderr)
        return kept

    def save_res():
        """Ganze Liste sichern (JSON: Datei, SQLite: Tabelle neu aufbauen).
        Für einzelne Änderungen res_put()/res_drop() bevorzugen."""
        store.res_save_all(reservations)

    def res_put(entry):
        """Eine Reservierung sichern (SQLite: nur diese Zeile per Upsert)."""
        store.res_put(entry, reservations)

    def res_drop(ids):
        """Reservierungen mit diesen IDs aus dem Speicher entfernen."""
        store.res_delete(list(ids), reservations)

    def prune_reservations():
        """Abgelaufene Reservierungen aus Liste UND Speicher entfernen."""
        kept = prune_res(reservations)
        if len(kept) != len(reservations):
            keep_ids = {id(r) for r in kept}
            dropped = [r.get("id") for r in reservations if id(r) not in keep_ids]
            reservations[:] = kept
            res_drop([d for d in dropped if d])

    reservations = load_res()

    # ---- API-Tokens für externe Anwendungen (lesend + optionale Schreibrechte) ----
    tokens_lock = threading.Lock()

    def load_tokens():
        d = store.load("tokens", None)
        if isinstance(d, dict):
            return {k: v for k, v in d.items()
                    if isinstance(v, dict) and v.get("hash")}
        return {}

    def save_tokens():
        store.save("tokens", tokens)

    tokens = load_tokens()

    def token_list():
        """Tokenliste ohne Hashes (für die Verwaltung)."""
        return {tid: {k: v for k, v in t.items() if k != "hash"}
                for tid, t in tokens.items()}

    def res_status(r):
        if r.get("rejected"):
            return "abgelehnt"
        if r.get("cancelled"):
            return "storniert"
        if r.get("approved"):
            return "genehmigt"
        return "in Prüfung" if r.get("approvals") else "beantragt"

    def valid_until(r):
        if not r.get("created"):
            return ""
        try:
            d = (datetime.fromisoformat(str(r["created"]))
                 + timedelta(days=args.res_ttl_days - 1
                             if args.res_ttl_days > 0 else 30))
            return d.date().isoformat()
        except ValueError:
            return ""

    # Englische CSV-Fassung: nur Spaltennamen und Status-Anzeigewerte – die
    # JSON-API-Felder und -Werte bleiben unverändert (stabiler v1-Vertrag).
    CSV_STATUS_EN = {"beantragt": "requested", "in Prüfung": "in review",
                     "genehmigt": "approved", "abgelehnt": "rejected",
                     "storniert": "cancelled"}

    _CSV_NUM_RX = re.compile(r"[+-]?\d+([.,]\d+)?$")

    def _csv_safe(v):
        """CSV-/Formel-Injection abwehren: Excel & Co. führen Zellen aus, die
        mit = + - @ (oder Tab/CR) beginnen, als Formel aus. Solche Werte mit
        einem führenden Apostroph als Text neutralisieren. Reine Zahlen (auch
        negativ, z. B. „-22" freie Kapazität) sind unkritisch und bleiben, damit
        Excel weiter damit rechnen kann."""
        sv = "" if v is None else str(v)
        if sv[:1] in ("=", "+", "-", "@", "\t", "\r") \
                and not _CSV_NUM_RX.match(sv):
            return "'" + sv
        return sv

    def _wrow(w, cells):
        w.writerow([_csv_safe(c) for c in cells])

    def res_csv(rows, lang="de"):
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        if lang == "en":
            w.writerow(["id", "name", "change", "cluster", "vcpu", "ram_gb",
                        "storage_gb", "requested_by", "team", "valid_from",
                        "valid_until", "status", "decided_by", "approvals",
                        "comment"])
        else:
            w.writerow(["id", "name", "change", "cluster", "vcpu", "ram_gb",
                        "storage_gb", "von", "abteilung", "gilt_ab", "gueltig_bis",
                        "status", "entschieden_von", "freigaben", "kommentar"])
        for r in rows:
            freigaben = "; ".join(
                f"{a.get('team') or '?'}: {a.get('by') or '?'}"
                for a in (r.get("approvals") or []))
            status = res_status(r)
            if lang == "en":
                status = CSV_STATUS_EN.get(status, status)
            _wrow(w, [r.get("id", ""), r.get("name", ""), r.get("change", ""),
                        r.get("cluster", ""), r.get("vcpu", 0),
                        r.get("ram_gb", 0), r.get("storage_gb", 0),
                        r.get("von", ""),
                        r.get("abteilung", ""), r.get("created", ""),
                        valid_until(r), status,
                        r.get("approved_by") or r.get("rejected_by") or "",
                        freigaben, r.get("comment", "")])
        return buf.getvalue()

    def data_csv(clusters, lang="de"):
        """Kapazitätstabelle als CSV — inkl. effektiv freier Werte nach Abzug
        genehmigter Reservierungen und Tanzu-Namespaces (wie im UI)."""
        import csv
        import io
        with res_lock:
            rv = {}
            for r in reservations:
                if r.get("approved") and not r.get("cancelled"):
                    e = rv.setdefault(r.get("cluster") or "", [0, 0.0, 0.0])
                    e[0] += int(r.get("vcpu") or 0)
                    e[1] += float(r.get("ram_gb") or 0)
                    e[2] += float(r.get("storage_gb") or 0)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        if lang == "en":
            w.writerow(["cluster", "source", "hosts", "vms", "cores",
                        "vcpu_cap", "vcpu_used", "vcpu_free",
                        "reserved_vcpu", "tanzu_vcpu", "vcpu_free_effective",
                        "ram_cap_gb", "ram_used_gb", "ram_free_gb",
                        "reserved_ram_gb", "tanzu_ram_gb", "ram_free_effective_gb",
                        "storage_cap_gb", "storage_used_gb", "storage_free_gb",
                        "reserved_storage_gb", "storage_free_effective_gb",
                        "workload_pct"])
        else:
            w.writerow(["cluster", "quelle", "hosts", "vms", "cores",
                        "vcpu_kap", "vcpu_belegt", "vcpu_frei",
                        "reserviert_vcpu", "tanzu_vcpu", "vcpu_frei_effektiv",
                        "ram_kap_gb", "ram_belegt_gb", "ram_frei_gb",
                        "reserviert_ram_gb", "tanzu_ram_gb", "ram_frei_effektiv_gb",
                        "storage_kap_gb", "storage_belegt_gb", "storage_frei_gb",
                        "reserviert_storage_gb", "storage_frei_effektiv_gb",
                        "workload_pct"])
        for c in clusters:
            rc, rr, rs = rv.get(c.get("name") or "", [0, 0.0, 0.0])
            tz_c = int(c.get("tanzuVcpu") or 0)
            tz_r = float(c.get("tanzuRamGb") or 0)
            _wrow(w, [
                c.get("name", ""), c.get("source") or "",
                c.get("hostCount", 0), c.get("vmCount", 0), c.get("cores", 0),
                c.get("vcpuCap", 0), c.get("vcpuUsed", 0), c.get("vcpuFree", 0),
                rc, tz_c, c.get("vcpuFree", 0) - rc - tz_c,
                c.get("ramCap", 0), c.get("ramUsed", 0), c.get("ramFree", 0),
                round(rr, 1), tz_r,
                round(float(c.get("ramFree") or 0) - rr - tz_r, 1),
                c.get("storageCap", 0), c.get("storageUsed", 0),
                c.get("storageFree", 0), round(rs, 1),
                round(float(c.get("storageFree") or 0) - rs, 1),
                c.get("workload", "")])
        return buf.getvalue()

    def _req_host_cols(r):
        """(hosts, wwpns) für die CSV: Hosts mit ihren WWPNs gruppiert
        ('esx101 (wwpn|wwpn)') plus eine flache, deduplizierte WWPN-Liste."""
        hs = r.get("hosts") or []
        names, flat, seen = [], [], set()
        for h in hs:
            if isinstance(h, dict):
                ws = h.get("wwpns") or []
                names.append((h.get("name") or "")
                             + (f" ({'|'.join(ws)})" if ws else ""))
                for wpn in ws:
                    if wpn.lower() not in seen:
                        seen.add(wpn.lower())
                        flat.append(wpn)
            else:
                names.append(str(h))
        return "; ".join(names), ", ".join(flat)

    def storagereq_csv(rows, lang="de"):
        """Storage-Erweiterungen als CSV fürs Storage-Team (inkl. NAA, Hosts,
        WWPNs)."""
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        if lang == "en":
            w.writerow(["id", "cluster", "hosts", "wwpns", "kind", "lun", "naa",
                        "current_gb", "target_gb", "new_lun_gb", "comment",
                        "requested_by", "requested_on", "status", "reservation"])
        else:
            w.writerow(["id", "cluster", "hosts", "wwpns", "typ", "lun", "naa",
                        "aktuell_gb", "ziel_gb", "neue_lun_gb", "kommentar",
                        "angefragt_von", "angefragt_am", "status", "reservierung"])
        for r in rows:
            hosts_col, wwpns_col = _req_host_cols(r)
            _wrow(w, [r.get("id", ""), r.get("cluster", ""),
                        hosts_col, wwpns_col, r.get("kind", ""),
                        r.get("lun_name", ""), r.get("naa", ""),
                        r.get("current_gb", ""), r.get("target_gb", ""),
                        r.get("size_gb", ""), r.get("comment", ""),
                        r.get("requested_by", ""), r.get("requested_on", ""),
                        r.get("status", ""), r.get("res_name", "")])
        return buf.getvalue()

    # ---- Audit-Log (JSONL, nur für Admins einsehbar) ----
    # Rotation: ab LOG_MAX_BYTES wird die Datei zu .1 (…, .LOG_KEEP) weggerollt,
    # damit sie nicht unbegrenzt wächst. Gelesen wird nur das Dateiende.
    log_lock = threading.Lock()
    LOG_MAX_BYTES = 10 * 1024 * 1024
    LOG_KEEP = 3

    def rotate_log():
        """Nur mit gehaltenem log_lock aufrufen."""
        try:
            if os.path.getsize(args.log_file) < LOG_MAX_BYTES:
                return
            oldest = f"{args.log_file}.{LOG_KEEP}"
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(LOG_KEEP - 1, 0, -1):
                src, dst = f"{args.log_file}.{i}", f"{args.log_file}.{i + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            os.replace(args.log_file, f"{args.log_file}.1")
        except OSError as e:
            print(f"Audit-Log-Rotation fehlgeschlagen: {e}", file=sys.stderr)

    def audit(user, action, detail=""):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "user": user or "system", "action": action, "detail": detail}
        try:
            with log_lock:
                ensure_dir(args.log_file)
                with open(args.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                rotate_log()
        except OSError as e:
            print(f"Audit-Log nicht schreibbar: {e}", file=sys.stderr)

    def _tail_lines(path, limit):
        """Die letzten Zeilen einer Datei lesen, ohne sie ganz einzulesen."""
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk = min(size, max(64 * 1024, limit * 400))
                f.seek(size - chunk)
                data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        lines = data.splitlines()
        if chunk < size and lines:
            lines = lines[1:]      # erste, evtl. angeschnittene Zeile verwerfen
        return lines

    def read_log(limit=500):
        with log_lock:
            lines = _tail_lines(args.log_file, limit)
            # Direkt nach einer Rotation ist die aktuelle Datei fast leer –
            # dann die vorherige Generation mit heranziehen.
            if len(lines) < limit and os.path.exists(f"{args.log_file}.1"):
                lines = _tail_lines(f"{args.log_file}.1", limit - len(lines)) + lines
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return list(reversed(out))   # neueste zuerst

    def res_detail(r):
        stor = r.get("storage_gb") or 0
        return (f"{r.get('name', '?')} [{r.get('id') or '?'}] "
                f"({r.get('change') or 'ohne Change'}, "
                f"{r.get('cluster', '?')}, {r.get('vcpu', 0)} vCPU / "
                f"{r.get('ram_gb', 0)} GB RAM"
                + (f" / {stor} GB Storage" if stor else "") + ")")

    def _split_addrs(s):
        return [a for a in re.split(r"[,;\s]+", str(s or "")) if "@" in a]

    def mail_recipients(kind, r, team=None):
        """Empfänger für ein Ereignis nach den Mail-Regeln (Modell Gemischt)."""
        with notify_lock:
            roles = dict(notify_cfg.get("role") or {})
            team_email = dict(notify_cfg.get("team_email") or {})
        to = []
        # Anforderer -> der Antragsteller selbst
        if kind in ("created", "rejected", "approved") \
                and (roles.get("anforderer") or {}).get(kind):
            to += _split_addrs(r.get("von_mail") or r.get("von"))
        # Admin / Auditor -> feste Verteiler-Adresse (Admin fällt auf --smtp-to zurück)
        for role in ("admin", "auditor"):
            rc = roles.get(role) or {}
            if rc.get(kind):
                em = rc.get("email") or (args.smtp_to if role == "admin" else "")
                to += _split_addrs(em)
        # Team ist dran / Erinnerung -> Adresse des aktuell zuständigen Teams
        if kind in ("team_turn", "reminder") \
                and (roles.get("reviewer") or {}).get(kind) and team:
            to += _split_addrs(team_email.get(team))
        return to

    def mail_event(kind, r, team=None, actor="", days=None):
        """Ereignis-Mail nach den Mail-Regeln im Hintergrund verschicken.
        Feuert zuerst die ausgehenden Webhooks – unabhängig von der Mail-
        Konfiguration."""
        fire_webhooks(kind, {"actor": actor or "System", "team": team or "",
                             "reservation": public_res(r)})
        if not args.smtp_server:
            return
        to = mail_recipients(kind, r, team)
        if not to:
            return
        if kind == "team_turn":
            action = f"wartet auf Freigabe durch {team}"
        elif kind == "reminder":
            action = (f"wartet seit {days} Tagen auf Freigabe"
                      + (f" durch {team}" if team else ""))
        else:
            action = {"created": "beantragt", "rejected": "abgelehnt",
                      "approved": "genehmigt"}.get(kind, kind)
        with notify_lock:
            tpl_html = notify_cfg.get("template_html") or ""
            tpl_subj = notify_cfg.get("template_subject") or ""
        admin = actor or "System"
        subject = " ".join(render_template(
            tpl_subj or DEFAULT_MAIL_SUBJECT,
            _mail_values(r, action, admin, args.res_ttl_days, team,
                         html=False)).split())
        html = reservation_mail_html(r, action, admin, args.res_ttl_days,
                                     tpl_html or None, team)
        body = reservation_mail_body(r, action, admin, args.res_ttl_days)

        def worker():
            try:
                send_mail(args, subject, body, to_override=to, html=html)
            except Exception as e:
                print(f"Mail-Versand fehlgeschlagen: {e}", file=sys.stderr)
        threading.Thread(target=worker, daemon=True).start()

    # ---- Gemeinsame Zustandsübergänge (Session-UI UND v1-API nutzen sie) ----
    # Eine einzige Stelle für die Entscheidungs-Logik, damit UI- und
    # API-Verhalten nicht auseinanderlaufen können.

    def res_find_open(rid, for_cancel=False):
        """Offenen Antrag finden (unter res_lock aufrufen). Freigeben/Ablehnen
        nur solange unentschieden; Storno auch für bereits genehmigte."""
        return next((x for x in reservations if x.get("id") == rid
                     and not x.get("rejected") and not x.get("cancelled")
                     and (for_cancel or not x.get("approved"))), None)

    def res_apply_approve(r, actor, comment):
        """Aktuelle Stufe freigeben (unter res_lock). Rückgabe: Action-Text."""
        today = datetime.now().date().isoformat()
        if approval_teams:
            team = current_team(r)
            r.setdefault("approvals", []).append(
                {"team": team, "by": actor, "on": today, "comment": comment})
            if len(r["approvals"]) < len(approval_teams):
                return f"von {team} freigegeben"
        r["approved"] = True
        r["approved_on"] = today
        r["approved_by"] = actor
        if comment:
            r["comment"] = comment
        return "genehmigt"

    def res_apply_reject(r, actor, comment):
        """Ablehnen in der aktuellen Stufe (unter res_lock)."""
        team = current_team(r)
        r["rejected"] = True
        r["rejected_on"] = datetime.now().date().isoformat()
        r["rejected_by"] = actor
        if team:
            r["rejected_team"] = team
        if comment:
            r["comment"] = comment

    def res_apply_cancel(r, actor, comment):
        """Stornieren (unter res_lock)."""
        r["cancelled"] = True
        r["cancelled_on"] = datetime.now().date().isoformat()
        r["cancelled_by"] = actor
        if comment:
            r["comment"] = comment

    def res_decision_notify(op, snap, actor, comment, action=None, api=False):
        """Audit + Mails nach einer Entscheidung (AUSSERHALB von res_lock)."""
        tag = " (API)" if api else ""
        cmt = f", Kommentar: {comment}" if comment else ""
        if op == "approve":
            verb = ("Antrag genehmigt" if action == "genehmigt"
                    else "Antrag freigegeben")
            audit(actor, verb + tag, res_detail(snap) + f" – {action}{cmt}")
            if snap.get("approved"):
                mail_event("approved", snap, actor=actor)
            else:
                nt = current_team(snap)
                if nt:
                    mail_event("team_turn", snap, team=nt, actor=actor)
        elif op == "reject":
            audit(actor, "Antrag abgelehnt" + tag, res_detail(snap)
                  + (f" (Stufe {snap.get('rejected_team')})"
                     if snap.get("rejected_team") else "") + cmt)
            mail_event("rejected", snap, actor=actor)
        else:
            audit(actor, "Antrag storniert" + tag, res_detail(snap) + cmt)

    # ---- Auto-Freigabe: Schwellenwert-basierte automatische Stufen-Freigabe ----
    autoapprove_lock = threading.Lock()

    def clean_autoapprove(raw):
        raw = raw if isinstance(raw, dict) else {}
        pct = lambda k, d: min(100, max(0, int(raw.get(k, d) or 0)
                                        if str(raw.get(k, d)).lstrip("-").isdigit()
                                        else d))
        teams_raw = raw.get("teams") if isinstance(raw.get("teams"), dict) else {}
        return {"enabled": bool(raw.get("enabled")),
                "min_cpu_pct": pct("min_cpu_pct", 20),
                "min_ram_pct": pct("min_ram_pct", 20),
                "min_lun_pct": pct("min_lun_pct", 25),
                "max_workload_pct": pct("max_workload_pct", 70),
                "teams": {str(k): True for k, v in teams_raw.items() if v}}

    autoapprove_cfg = clean_autoapprove(store.load("autoapprove", None))

    def save_autoapprove():
        store.save("autoapprove", autoapprove_cfg)

    # ---- Storage-Erweiterungen (Freigabe-Workflow -> Storage-Team) ----
    # Beim Freigeben kann eine LUN-Vergrößerung oder eine neue LUN angefragt
    # werden; das Storage-Team ruft die offenen Anfragen (inkl. NAA) per API
    # ab und setzt sie nach Umsetzung auf "erledigt". Dynamisch schaltbar.
    storage_lock = threading.Lock()

    def _clean_excl(raw):
        # kommagetrennte Namensmuster -> saubere, kleingeschriebene Liste.
        # raw kann ein String (UI-Eingabe) ODER eine Liste sein (gespeicherte
        # Datei nach Neustart) — sonst würde str(liste) zu Müll-Mustern.
        if isinstance(raw, (list, tuple)):
            parts = [str(p) for p in raw]
        else:
            parts = re.split(r"[,\n;]+", str(raw or ""))
        return [p.strip().lower() for p in parts if p.strip()][:30]

    def _clean_vfac(v, default):
        # vSAN-Nutzbar-Faktor: 0,05–1,0 (z. B. 0,5 = RAID-1, 0,75 = RAID-5 3+1).
        try:
            return round(min(1.0, max(0.05, float(v))), 4)
        except (TypeError, ValueError):
            return default

    def _clean_overrides(raw):
        # Faktor je Cluster (Override des globalen Standards).
        out = {}
        if isinstance(raw, dict):
            for cl, v in list(raw.items())[:200]:
                name = " ".join(str(cl or "").split())[:120]
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if name and 0.05 <= f <= 1.0:
                    out[name] = round(f, 4)
        return out

    def load_storagecfg():
        raw = store.load("storagecfg", None) or {}
        def _pint(k):
            try:
                return max(0, int(float(raw.get(k) or 0)))
            except (TypeError, ValueError):
                return 0
        return {"enabled": bool(raw.get("enabled")), "min_lun_gb": _pint("min_lun_gb"),
                "max_lun_gb": _pint("max_lun_gb"),
                "exclude_names": _clean_excl(raw.get("exclude_names")),
                "vsan_factor": _clean_vfac(raw.get("vsan_factor"), args.vsan_factor),
                "vsan_overrides": _clean_overrides(raw.get("vsan_overrides"))}

    storage_cfg = load_storagecfg()

    # ---- Netzwerk-Filter (Portgruppen nach Name/VLAN-ID ausblenden) ----
    net_lock = threading.Lock()

    def _clean_vlans(raw):
        """Kommagetrennte VLAN-IDs/Bereiche -> saubere Token-Liste
        (nur Ziffern bzw. "a-b", max. 50). raw: String oder Liste."""
        if isinstance(raw, (list, tuple)):
            parts = [str(p) for p in raw]
        else:
            parts = re.split(r"[,\n;]+", str(raw or ""))
        out = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if p.isdigit():
                out.append(p)
            elif "-" in p:
                a, _, b = p.partition("-")
                if a.strip().isdigit() and b.strip().isdigit():
                    out.append(f"{int(a)}-{int(b)}")
        return out[:50]

    def load_netcfg():
        raw = store.load("netcfg", None) or {}
        return {"exclude_names": _clean_excl(raw.get("exclude_names")),
                "exclude_vlans": _clean_vlans(raw.get("exclude_vlans"))}

    net_cfg = load_netcfg()

    # ---- Offline-Quellen: manuell importierte Cluster (ohne vROps) ----------
    # Bereiche ohne Netzanbindung werden per PowerCLI-JSON exportiert und hier
    # unter einem festen Quellnamen importiert. Die Rohdaten laufen bei jedem
    # Abruf durch build_summary — dieselbe Kapazitäts-Mathematik wie echte
    # Quellen (Overcommit, N+1, vSAN-Faktor, Namens-/VLAN-Filter).
    import_lock = threading.Lock()
    import_sources = store.load("manual", None)
    import_sources = import_sources if isinstance(import_sources, dict) else {}

    def save_imports():
        store.save("manual", import_sources)

    def _num(v, cast=float, lo=0):
        try:
            return max(lo, cast(float(v or 0)))
        except (TypeError, ValueError):
            return lo

    def _as_list(x):
        """PowerShells ConvertTo-Json entpackt 1-elementige Arrays zu einem
        einzelnen Objekt (aus [ {...} ] wird { ... }). Wir nehmen ein einzelnes
        Objekt daher genauso an wie eine Liste – sonst schlüge der Import bei
        Clustern mit genau einem Host/VM/Datastore/Portgruppe fehl."""
        if isinstance(x, dict):
            return [x]
        if isinstance(x, (list, tuple)):
            return list(x)
        return []

    def clean_import_clusters(raw):
        """Import-JSON prüfen/normalisieren. Gibt (clusters, fehler, skipped)
        zurück. Einzelne Cluster ohne Hosts werden übersprungen (nicht der ganze
        Import abgebrochen); toleriert PowerShells 1-Element-Array-Entpackung."""
        raw = _as_list(raw)
        if not raw:
            return None, "clusters: Liste mit mindestens einem Cluster erwartet", []
        oneline = lambda v, n: " ".join(str(v or "").split())[:n]
        out, skipped = [], []
        for c in raw[:100]:
            if not isinstance(c, dict):
                continue
            name = oneline(c.get("name"), 120)
            if not name:
                continue                       # namenlose Einträge still überspringen
            hosts = [{"name": oneline(h.get("name"), 200),
                      "cores": _num(h.get("cores"), int),
                      "ram_gb": round(_num(h.get("ram_gb")), 1)}
                     for h in _as_list(c.get("hosts"))[:500]
                     if isinstance(h, dict)]
            if not hosts:
                skipped.append(name)           # ohne Hosts keine Kapazität
                continue
            vms = [{"name": oneline(v.get("name"), 200),
                    "vcpu": _num(v.get("vcpu"), int),
                    "ram_gb": round(_num(v.get("ram_gb")), 1),
                    "on": bool(v.get("on"))}
                   for v in _as_list(c.get("vms"))[:20000]
                   if isinstance(v, dict)]
            luns = []
            for d in _as_list(c.get("datastores") or c.get("luns"))[:500]:
                if not isinstance(d, dict):
                    continue
                nm = oneline(d.get("name"), 200)
                typ = oneline(d.get("type"), 40)
                cap = round(_num(d.get("cap_gb")), 1)
                used = min(round(_num(d.get("used_gb")), 1), cap)
                # vSAN-Policy-Faktor (global + Cluster-Override, wie bei collect()).
                _vf = (storage_cfg.get("vsan_factor") or args.vsan_factor)
                _vf = (storage_cfg.get("vsan_overrides") or {}).get(name, _vf)
                f = _vf if "vsan" in (typ + nm).lower() else 1.0
                luns.append({"name": nm, "type": typ or "unbekannt",
                             "naa": oneline(d.get("naa"), 80),
                             "factor": f, "raw_cap_gb": cap,
                             "cap_gb": round(cap * f, 1),
                             "used_gb": round(used * f, 1)})
            pgs = [{"name": oneline(p.get("name"), 200),
                    "vlan": oneline(p.get("vlan"), 20)}
                   for p in _as_list(c.get("portgroups"))[:2000]
                   if isinstance(p, dict) and p.get("name")]
            out.append({"name": name, "hosts": hosts, "vms": vms,
                        "luns": luns, "portgroups": pgs})
        if not out:
            msg = ("kein Cluster mit Hosts im Import (ohne Hosts: %s)"
                   % ", ".join(skipped[:10])) if skipped \
                  else "kein verwertbarer Cluster im Import"
            return None, msg, skipped
        return out, "", skipped

    def import_clusters_summary():
        """Alle Offline-Quellen als fertige Cluster-Dicts (wie von collect())."""
        with import_lock:
            srcs = json.loads(json.dumps(import_sources))
        result = []
        for src in sorted(srcs):
            entry = srcs[src]
            data = {}
            when = entry.get("imported_on") or ""
            try:
                when_de = datetime.fromisoformat(when).strftime("%d.%m.%Y")
            except ValueError:
                when_de = when
            for c in entry.get("clusters") or []:
                data[c["name"]] = {
                    "hosts": c.get("hosts") or [],
                    "vms": c.get("vms") or [],
                    "tags": [f"Import: {when_de}"] if when_de else [],
                    "portgroups": c.get("portgroups") or [],
                    "workload": None, "namespaces": [],
                    "storage": {
                        "cap_gb": sum(l["cap_gb"] for l in c.get("luns") or []),
                        "used_gb": sum(l["used_gb"] for l in c.get("luns") or []),
                        "luns": c.get("luns") or []}}
            if not data:
                continue
            with storage_lock:
                mn, sx = (storage_cfg.get("min_lun_gb", 0),
                          storage_cfg.get("exclude_names") or [])
            with net_lock:
                nn, nv = (net_cfg.get("exclude_names") or [],
                          net_cfg.get("exclude_vlans") or [])
            cl = build_summary(data, args.cpu_factor, args.failover_hosts,
                               args.tanzu_mhz_per_vcpu, mn, sx, nn, nv)
            for c in cl:
                c["source"] = src
                c["imported"] = True   # Auto-Freigabe überspringt statische Daten
            result.extend(cl)
        return result

    # ---- Statistik-Historie: ein kompakter Tages-Snapshot je Cluster ---------
    # Nach jedem erfolgreichen Abruf; Mehrfach-Abrufe am selben Tag überschreiben
    # den Tageswert. Grundlage für die Trend-Ansicht („VMs werden größer").
    history_lock = threading.Lock()
    history = store.load("history", None)
    history = history if isinstance(history, dict) else {}

    _RAM_BUCKETS = (4, 8, 16, 32, 64)   # GB; letzter Topf = darüber

    def _cluster_snapshot(c):
        vms = c.get("vms") or []
        buckets = [0] * (len(_RAM_BUCKETS) + 1)
        for v in vms:
            r = float(v.get("ram_gb") or 0)
            for i, lim in enumerate(_RAM_BUCKETS):
                if r <= lim:
                    buckets[i] += 1
                    break
            else:
                buckets[-1] += 1
        withdisk = [v for v in vms if v.get("disk_gb")]
        return {"src": c.get("source") or "",
                "n": len(vms),
                "on": sum(1 for v in vms if v.get("on")),
                "vcpu": sum(int(v.get("vcpu") or 0) for v in vms),
                "ram": round(sum(float(v.get("ram_gb") or 0) for v in vms), 1),
                "disk": round(sum(float(v.get("disk_gb") or 0)
                                  for v in withdisk), 1),
                "nd": len(withdisk),
                "ramU": c.get("ramUsed") or 0, "ramC": c.get("ramCap") or 0,
                "stU": c.get("storageUsed") or 0, "stC": c.get("storageCap") or 0,
                "vcU": c.get("vcpuUsed") or 0, "vcC": c.get("vcpuCap") or 0,
                "hr": buckets}

    def record_history(clusters):
        """Tages-Snapshot schreiben + alte Einträge nach --history-days kappen."""
        today = datetime.now().date().isoformat()
        snap = {c["name"]: _cluster_snapshot(c) for c in clusters if c.get("name")}
        if not snap:
            return
        with history_lock:
            history[today] = snap
            if args.history_days > 0:
                cutoff = (datetime.now().date()
                          - timedelta(days=args.history_days)).isoformat()
                for d in [d for d in history if d < cutoff]:
                    del history[d]
            store.save("history", history)

    def demo_backfill_history(clusters):
        """--sample: synthetische Historie (12 Monate, wachsender Trend), damit
        die Statistik sofort etwas zeigt. Nur wenn noch (fast) keine Daten da."""
        with history_lock:
            if len(history) >= 5:
                return
        import random
        base = {c["name"]: _cluster_snapshot(c) for c in clusters if c.get("name")}
        rnd = random.Random(42)              # deterministisch je Lauf
        with history_lock:
            for age in range(365, 0, -7):    # Wochenpunkte rückwärts
                day = (datetime.now().date() - timedelta(days=age)).isoformat()
                f = 1.0 - 0.38 * (age / 365.0)          # früher: weniger/kleiner
                fd = 1.0 - 0.5 * (age / 365.0)          # Disk wächst stärker
                snap = {}
                for name, b in base.items():
                    jitter = 1 + rnd.uniform(-0.03, 0.03)
                    n = max(1, int(b["n"] * f * jitter))
                    snap[name] = dict(b,
                        n=n, on=max(1, int(b["on"] * f * jitter)),
                        vcpu=int(b["vcpu"] * f * jitter),
                        ram=round(b["ram"] * f * f * jitter, 1),      # Ø wächst
                        disk=round(b["disk"] * fd * f * jitter, 1),
                        nd=n,
                        ramU=round(float(b["ramU"]) * f * jitter, 1),
                        stU=round(float(b["stU"]) * fd * jitter, 1),
                        vcU=int(b["vcU"] * f * jitter),
                        hr=[int(x * f * jitter) for x in b["hr"]])
                history[day] = snap
            store.save("history", history)

    storage_reqs = store.load("storagereq", None)
    storage_reqs = storage_reqs if isinstance(storage_reqs, list) else []

    def save_storage_reqs():
        store.save("storagereq", storage_reqs)

    def clean_storage_req(body, actor):
        """Eine Storage-Anfrage aus dem Freigabe-Dialog säubern. kind=expand
        (bestehende LUN auf target_gb) oder kind=new (neue LUN size_gb)."""
        b = body if isinstance(body, dict) else {}
        one = lambda v, n: " ".join(str(v or "").split())[:n]
        kind = "new" if b.get("kind") == "new" else "expand"
        try:
            size = int(float(b.get("size_gb") or 0))
            target = int(float(b.get("target_gb") or 0))
            cur = int(float(b.get("current_gb") or 0))
        except (TypeError, ValueError):
            return None
        req = {"id": new_res_id(), "res_id": one(b.get("res_id"), 40),
               "res_name": one(b.get("res_name"), 120),
               "cluster": one(b.get("cluster"), 120), "kind": kind,
               "comment": one(b.get("comment"), 200),
               "requested_by": actor,
               "requested_on": datetime.now().date().isoformat(),
               "status": "offen", "done_by": "", "done_on": ""}
        if kind == "expand":
            if not one(b.get("lun_name"), 200) or target <= cur:
                return None
            req.update(lun_name=one(b.get("lun_name"), 200),
                       naa=one(b.get("naa"), 80), current_gb=cur,
                       target_gb=target)
        else:
            if size <= 0:
                return None
            req.update(lun_name="", naa="", size_gb=size)
        return req

    def auto_check(r, cfg):
        """Schwellen gegen den Ziel-Cluster prüfen — NACH Abzug des Antrags.
        Rückgabe (ok, begruendung). Fehlende Daten -> konservativ ablehnen."""
        c = next((x for x in (state.get("clusters") or [])
                  if x.get("name") == r.get("cluster")), None)
        if not c:
            return False, "Cluster nicht im aktuellen Datenstand"
        if c.get("imported"):
            # Offline-Quelle: Zahlen sind statisch, echte Auslastung kann höher
            # liegen -> nie automatisch freigeben, immer ans Team.
            return False, "Offline-Quelle (statische Import-Daten) – manuelle Freigabe"
        rv_cpu = rv_ram = 0.0
        for x in reservations:                     # unter res_lock aufgerufen
            if x.get("approved") and not x.get("cancelled") \
                    and x.get("cluster") == c.get("name") \
                    and x.get("id") != r.get("id"):
                rv_cpu += int(x.get("vcpu") or 0)
                rv_ram += float(x.get("ram_gb") or 0)
        checks = []
        cap = float(c.get("vcpuCap") or 0)
        if cap <= 0:
            return False, "keine vCPU-Kapazität bekannt"
        free = (float(c.get("vcpuFree") or 0) - rv_cpu
                - float(c.get("tanzuVcpu") or 0) - int(r.get("vcpu") or 0))
        p = free / cap * 100
        if p < cfg["min_cpu_pct"]:
            return False, f"vCPU frei {p:.0f} % < {cfg['min_cpu_pct']} %"
        checks.append(f"vCPU frei {p:.0f} % ≥ {cfg['min_cpu_pct']} %")
        cap = float(c.get("ramCap") or 0)
        if cap <= 0:
            return False, "keine RAM-Kapazität bekannt"
        free = (float(c.get("ramFree") or 0) - rv_ram
                - float(c.get("tanzuRamGb") or 0) - float(r.get("ram_gb") or 0))
        p = free / cap * 100
        if p < cfg["min_ram_pct"]:
            return False, f"RAM frei {p:.0f} % < {cfg['min_ram_pct']} %"
        checks.append(f"RAM frei {p:.0f} % ≥ {cfg['min_ram_pct']} %")
        luns = c.get("datastores") or []
        if not luns:
            return False, "keine Storage-Daten für den Cluster"
        best = max(luns, key=lambda l: float(l.get("cap_gb") or 0)
                   - float(l.get("used_gb") or 0))
        lcap = float(best.get("cap_gb") or 0)
        if lcap <= 0:
            return False, "größte LUN ohne Kapazitätswert"
        lfree = (lcap - float(best.get("used_gb") or 0)
                 - float(r.get("storage_gb") or 0))
        p = lfree / lcap * 100
        if p < cfg["min_lun_pct"]:
            return False, (f"größte freie LUN '{best.get('name')}' "
                           f"{p:.0f} % < {cfg['min_lun_pct']} %")
        checks.append(f"LUN '{best.get('name')}' {p:.0f} % ≥ {cfg['min_lun_pct']} %")
        wl = c.get("workload")
        if wl is None:
            return False, "kein Workload-Wert aus vROps (konservativ blockiert)"
        if float(wl) > cfg["max_workload_pct"]:
            return False, f"Workload {wl} % > {cfg['max_workload_pct']} %"
        checks.append(f"Workload {wl} % ≤ {cfg['max_workload_pct']} %")
        return True, " · ".join(checks)

    def emit_auto_events(snap, events):
        """Audit-Einträge der Auto-Freigabe (nach dem res_lock ausgeben)."""
        for kind, msg in events:
            if kind == "ok":
                audit("Auto-Freigabe", "Antrag automatisch freigegeben",
                      res_detail(snap) + " – " + msg)
            else:
                audit("Auto-Freigabe", "Auto-Freigabe nicht angewendet",
                      res_detail(snap) + " – " + msg)

    def try_auto_approve(r):
        """Unter res_lock: auto-freigebbare Stufen kaskadierend freigeben.
        Läuft bei Antragstellung und nach jeder manuellen Freigabe. Rückgabe:
        Liste der Audit-Ereignisse (nach dem Lock ausgeben). Blockiert nie —
        greift eine Schwelle nicht, bleibt der Antrag einfach beim Team."""
        with autoapprove_lock:
            cfg = dict(autoapprove_cfg)
        if not cfg.get("enabled"):
            return []
        events = []
        while not r.get("approved") and not r.get("rejected") \
                and not r.get("cancelled"):
            team = current_team(r)
            if approval_teams and not cfg["teams"].get(team or ""):
                break                     # diese Stufe prüft manuell
            ok, why = auto_check(r, cfg)
            if not ok:
                events.append(("skip", f"{team or 'einstufig'}: {why}"))
                break
            action = res_apply_approve(r, "Auto-Freigabe", "")
            res_put(r)
            events.append(("ok", f"{action} – {why}"))
            if not approval_teams:
                break
        return events

    def public_res(r):
        """Reservierung ohne serverinterne Felder (z. B. die aufgelöste
        Empfänger-Mailadresse von_mail) – so wie sie an Clients gehen darf."""
        return {k: v for k, v in r.items() if k != "von_mail"}

    # Sichtbarkeits-Merkmal -> Feld(er) im Cluster-Payload
    _VIS_CLUSTER_KEYS = {"workload": "workload", "hosts": "hosts", "vms": "vms",
                         "network": "portgroups", "storage": "datastores",
                         "tags": "tags"}

    def clusters_for(s):
        """Cluster-Daten je Session: nach Sichtbarkeits-Matrix (Feature-Strip)
        UND nach den für den Benutzer freigegebenen vROps-Quellen. s ist ein
        Session-Dict, ein Rollen-String oder None (ohne Anmeldung = alles).
        Zählwerte hostCount/vmCount bleiben je Cluster für die Übersicht."""
        if isinstance(s, dict):
            role, allow = s.get("role"), s.get("sources") or []
        else:
            role, allow = s, []
        cl = state["clusters"]
        if allow:
            aset = set(allow)
            cl = [c for c in cl if c.get("source") in aset]
        vis = vis_for(role)
        strip = {key for feat, key in _VIS_CLUSTER_KEYS.items()
                 if not vis.get(feat, True)}
        if strip:
            return [{k: v for k, v in c.items() if k not in strip} for c in cl]
        return cl

    def allowed_sources(s):
        return set(s.get("sources") or []) if isinstance(s, dict) else set()

    def _src_ok(s, cluster_name, cl_src_map):
        """Darf die Session diesen Cluster (per Name) sehen? True, wenn keine
        Quellen-Einschränkung, die Quelle erlaubt ist ODER unbekannt (kein
        Datenverlust bei Clustern, die gerade nicht im Datenstand sind)."""
        allow = allowed_sources(s)
        if not allow:
            return True
        src = cl_src_map.get(cluster_name)
        return src is None or src in allow

    def _strip_decided(d):
        """„Entschieden von" entfernen: Namen der Entscheider und der
        Freigebenden je Stufe (der Fortschritt selbst bleibt sichtbar)."""
        d = {k: v for k, v in d.items()
             if k not in ("approved_by", "rejected_by", "cancelled_by")}
        if isinstance(d.get("approvals"), list):
            d["approvals"] = [{"team": a.get("team"), "on": a.get("on")}
                              for a in d["approvals"]]
        return d

    def visible_res(s):
        """Sichtbare Reservierungen je Rolle: Admin, Auditor und Reviewer sehen
        ALLE Anfragen (Reviewer/Auditor je nach Sichtbarkeits-Matrix ohne die
        Entscheider-Namen). Nur Anforderer sind auf ihr eigenes Team
        beschränkt – fremde genehmigte bleiben anonymisiert enthalten, damit
        die freie Kapazität stimmt. Zusätzlich nach freigegebenen vROps-Quellen
        gefiltert (leer = alle)."""
        cmap = {c.get("name"): c.get("source") for c in state["clusters"]}
        res_src = [r for r in reservations
                   if _src_ok(s, r.get("cluster"), cmap)]
        if s["role"] in ("admin", "auditor", "reviewer"):
            show_dec = vis_for(s["role"]).get("decided_by", True)
            return [public_res(r) if show_dec else _strip_decided(public_res(r))
                    for r in res_src]
        show_dec = vis_for(s["role"]).get("decided_by", True)
        team = s.get("abteilung") or ""
        out = []
        for r in res_src:
            mine = (team and r.get("abteilung") == team) or r.get("von") == s["user"]
            if mine:
                d = {k: v for k, v in r.items() if k != "von_mail"}
                if not show_dec:
                    d = _strip_decided(d)
                out.append(d)
            elif r.get("approved") and not r.get("cancelled"):
                # bewusst ohne Name, von, Change, Kommentar; storniert zählt nicht
                out.append({"id": r.get("id"), "cluster": r.get("cluster"),
                            "name": "(anderes Team)", "vcpu": r.get("vcpu"),
                            "ram_gb": r.get("ram_gb"),
                            "storage_gb": r.get("storage_gb"),
                            "created": r.get("created"),
                            "approved": True, "foreign": True})
        return out

    sources = getattr(args, "sources", []) or []
    vlan_full_ts = [0.0]   # Zeitpunkt des letzten VLAN-Voll-Abrufs

    # ---- Gestaffelte Abruf-Intervalle („Cron-Klassen") ----------------------
    # Drei Teilbereiche mit eigenem Takt: Kapazität (VMs/Tanzu), Netzwerk
    # (Portgruppen), Storage (Datastores). Cluster/Hosts/Tags laufen immer mit.
    # Intervalle in Minuten, 0 = Standard-Intervall (--interval). Rohdaten des
    # letzten Laufs je Quelle bleiben im Speicher; übersprungene Bereiche
    # übernehmen daraus (Neustart => erster Abruf wieder komplett).
    TIERS = ("vms", "network", "storage")
    refresh_lock = threading.Lock()

    # Anzeige-Zeitzone (frei konfigurierbar in der Verwaltung -> „Datenabruf").
    # Ein einziges time.tzset() wirkt prozessweit auf ALLE Zeitausgaben
    # (Stand, Log, Abgleich-Uhrzeiten, Mails) – der Abruf-Takt selbst rechnet
    # in Sekunden und bleibt davon unberührt. Leer = Zeitzone des Servers.
    _orig_tz = os.environ.get("TZ")
    # Strikt auf IANA-Zonennamen: Segmente aus [A-Za-z0-9_+-], per "/" getrennt,
    # KEINE Punkte/„..", kein führender/absoluter Pfad. Verhindert Path-Traversal
    # (sonst würde tz_available via os.path.exists zum Datei-Existenz-Orakel).
    _TZ_RX = re.compile(r"^[A-Za-z0-9_+-]{1,20}(?:/[A-Za-z0-9_+-]{1,20}){0,3}$")

    def clean_tz(raw):
        s = " ".join(str(raw or "").split())
        return s if _TZ_RX.match(s) else ""

    def tz_available(tz):
        # leer und UTC gelten immer; sonst muss die Zonendatei existieren
        # (tzdata; auf RHEL vorhanden, im Docker-Image mitinstalliert).
        if not tz or tz.upper() == "UTC":
            return True
        return os.path.exists("/usr/share/zoneinfo/" + tz)

    def apply_tz(tz):
        if not hasattr(time, "tzset"):   # nur Unix; Windows ohne tzset
            return
        if tz:
            os.environ["TZ"] = tz
        elif _orig_tz is not None:
            os.environ["TZ"] = _orig_tz  # zurück auf den Systemstand
        else:
            os.environ.pop("TZ", None)
        time.tzset()

    def _clean_refreshcfg(raw):
        raw = raw if isinstance(raw, dict) else {}
        out = {}
        for t in TIERS:
            try:
                out[t] = max(0, min(int(float(raw.get(t) or 0)), 10080))
            except (TypeError, ValueError):
                out[t] = 0
        out["tz"] = clean_tz(raw.get("tz"))
        return out

    refresh_cfg = _clean_refreshcfg(store.load("refreshcfg", None))
    if refresh_cfg.get("tz") and tz_available(refresh_cfg["tz"]):
        apply_tz(refresh_cfg["tz"])     # gespeicherte Anzeige-Zeitzone aktivieren
    tier_last = {t: 0.0 for t in TIERS}    # Epoche des letzten erfolgreichen Laufs
    raw_cache = {}                          # {quelle: rohdaten je Cluster}

    def tier_interval(t):
        with refresh_lock:
            m = refresh_cfg.get(t) or 0
        return m * 60 if m > 0 else max(interval, 60)

    def due_tiers():
        now = time.time()
        return {t for t in TIERS if now - tier_last[t] >= tier_interval(t)}

    def do_refresh(parts=None):
        # parts: None = fällige Teilbereiche (mind. einer erzwungen),
        # Menge/Liste = genau diese Teilbereiche (manueller Teil-Abruf).
        run = set(parts) & set(TIERS) if parts else due_tiers()
        if not run:
            run = set(TIERS)
        skip_tiers = set(TIERS) - run
        state.update(refreshing=True, error=None, progress="0 %")
        t0 = time.time()
        n = len(sources)
        label = ", ".join(s["name"] or s["url"] for s in sources) or "Demo"
        tier_note = ("alle Bereiche" if not skip_tiers else
                     "nur " + ", ".join(sorted(run)))
        print(f"Aria-Abruf gestartet ({label}) – {tier_note} ...", file=sys.stderr)
        # VLAN-Cache: bekannte Portgruppen-VLANs aus dem letzten Stand
        # wiederverwenden (spart einen API-Aufruf je Portgruppe); einmal am
        # Tag alles frisch lesen, falls sich ein VLAN geändert hat.
        if time.time() - vlan_full_ts[0] >= 86400:
            vlan_full_ts[0] = time.time()
            vlan_hint = None
            print("VLAN-Cache: täglicher Voll-Abruf", file=sys.stderr)
        else:
            vlan_hint = {p.get("name"): p.get("vlan")
                         for c in (state.get("clusters") or [])
                         for p in (c.get("portgroups") or []) if p.get("vlan")}
        try:
            if args.sample:
                time.sleep(2)  # Demo: Ladezeit simulieren
                clusters = build_summary(sample_data(
                                             storage_cfg.get("vsan_factor")
                                             or args.vsan_factor,
                                             storage_cfg.get("vsan_overrides") or {}),
                                         args.cpu_factor,
                                         args.failover_hosts,
                                         args.tanzu_mhz_per_vcpu,
                                         storage_cfg.get("min_lun_gb", 0),
                                         storage_cfg.get("exclude_names") or [],
                                         net_cfg.get("exclude_names") or [],
                                         net_cfg.get("exclude_vlans") or [])
            else:
                clusters, errors = [], []
                for i, s in enumerate(sources):
                    tag = s["name"] or s["url"]

                    def prog(m, tag=tag, i=i):
                        pref = f"Quelle {i + 1}/{n} ({tag}) · " if n > 1 else ""
                        state.update(progress=pref + m)
                    try:
                        api = AriaOps(s["url"], s["user"], s["password"],
                                      s["auth_source"], verify_tls=not s["insecure"],
                                      proxy=s["aria_proxy"])
                        # Ohne Rohdaten vom letzten Lauf (Start/Neustart)
                        # wird die Quelle komplett gelesen.
                        eff_skip = skip_tiers if tag in raw_cache else set()
                        cl, raw = collect(api, args.cpu_factor, progress=prog,
                                     failover_hosts=args.failover_hosts,
                                     exclude_tag=args.exclude_tag,
                                     tag_property=args.tag_property,
                                     vsan_factor=storage_cfg.get("vsan_factor")
                                                 or args.vsan_factor,
                                     vsan_overrides=storage_cfg.get("vsan_overrides") or {},
                                     tanzu_mhz=args.tanzu_mhz_per_vcpu,
                                     vlan_hint=vlan_hint,
                                     min_lun_gb=storage_cfg.get("min_lun_gb", 0),
                                     exclude_names=storage_cfg.get("exclude_names") or [],
                                     net_exclude_names=net_cfg.get("exclude_names") or [],
                                     net_exclude_vlans=net_cfg.get("exclude_vlans") or [],
                                     skip=eff_skip, prev=raw_cache.get(tag))
                        raw_cache[tag] = raw
                        for c in cl:
                            if s["name"]:
                                c["source"] = s["name"]
                        clusters += cl
                        print(f"Quelle '{tag}': {len(cl)} Cluster", file=sys.stderr)
                    except Exception as e:
                        errors.append(f"{tag}: {e}")
                        print(f"Quelle '{tag}' FEHLGESCHLAGEN: {e}", file=sys.stderr)
                if errors and not clusters:
                    raise RuntimeError("; ".join(errors))
                state["error"] = "Teilausfall: " + "; ".join(errors) if errors else None
            # Offline-Quellen (manuelle Importe) wie zusätzliche vROps anhängen
            manual = import_clusters_summary()
            if manual:
                clusters = clusters + manual
                print(f"Offline-Quellen: {len(manual)} Cluster aus "
                      f"{len({c['source'] for c in manual})} Import(en)",
                      file=sys.stderr)
            strip_uplinks(clusters, not args.show_uplink_portgroups)
            if args.sample:
                demo_backfill_history(clusters)
            record_history(clusters)
            state["clusters"] = clusters
            state["updated"] = datetime.now().strftime("%d.%m.%Y %H:%M")
            now = time.time()
            for t in run:
                tier_last[t] = now
            state["tiers"] = {t: datetime.fromtimestamp(tier_last[t])
                              .strftime("%d.%m.%Y %H:%M") if tier_last[t] else ""
                              for t in TIERS}
            ensure_dir(args.cache)
            with open(args.cache, "w", encoding="utf-8") as f:
                json.dump({"updated": state["updated"], "clusters": clusters},
                          f, ensure_ascii=False)
            dur = time.time() - t0
            nvm = sum(c.get("vmCount", 0) for c in clusters)
            src_note = f" aus {n} Quellen" if not args.sample and n > 1 else ""
            detail = f"{len(clusters)} Cluster, {nvm} VMs{src_note} in {dur:.1f} s"
            print(f"Aria-Abruf beendet: {detail}", file=sys.stderr)
            audit(None, "Aria-Abruf beendet", detail)
        except Exception as e:
            dur = time.time() - t0
            state["error"] = str(e)
            print(f"Aria-Abruf FEHLGESCHLAGEN nach {dur:.1f} s: {e}", file=sys.stderr)
            audit(None, "Aria-Abruf fehlgeschlagen", f"nach {dur:.1f} s: {e}")
        finally:
            state["last"] = time.time()
            state["refreshing"] = False

    def purge_stale():
        """Abgelaufene Sitzungen und alte Login-Fehlversuche verwerfen, damit
        beides nicht unbegrenzt im Speicher wächst."""
        now = time.time()
        for t in [t for t, s in list(sessions.items()) if s.get("exp", 0) <= now]:
            sessions.pop(t, None)
        with login_lock:
            for k in [k for k, v in list(login_fails.items())
                      if not v or now - max(v) > LOGIN_WINDOW]:
                login_fails.pop(k, None)

    def maintenance():
        while True:
            time.sleep(300)
            try:
                purge_stale()
            except Exception as e:
                print(f"Aufräumen fehlgeschlagen: {e}", file=sys.stderr)
            try:
                if sessions_dirty[0]:
                    save_sessions()
            except Exception as e:
                print(f"Sitzungs-Speicherung fehlgeschlagen: {e}", file=sys.stderr)

    def scheduler():
        # Erster Start ohne Cache: sofort alles holen
        if not state["clusters"] and not state["refreshing"]:
            do_refresh()
        # Heartbeat: alle 15 s prüfen, welche Teilbereiche fällig sind
        # (gestaffelte Intervalle aus der Verwaltung; 0 = --interval).
        while interval > 0:
            due = due_tiers()
            if due and not state["refreshing"]:
                do_refresh(due)
            time.sleep(15)

    def check_reminders():
        """Offene Anträge, die zu lange auf die aktuelle Stufe warten, per Mail
        erinnern (Team-Adresse und/oder Admin-Verteiler, je nach Mail-Regeln).
        Erinnert nach reminder_days Tagen Wartezeit und danach alle
        reminder_days erneut; der Merker (reminded_on/reminded_stage) liegt am
        Antrag, eine neue Stufe setzt ihn zurück."""
        if not args.smtp_server:
            return
        with notify_lock:
            days_cfg = int(notify_cfg.get("reminder_days") or 2)
        today = datetime.now().date()
        due = []
        with res_lock:
            for r in reservations:
                if r.get("approved") or r.get("rejected") or r.get("cancelled"):
                    continue
                appr = r.get("approvals") or []
                since_s = str((appr[-1].get("on") if appr else r.get("created")) or "")
                try:
                    since = datetime.strptime(since_s[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                waiting = (today - since).days
                if waiting < days_cfg:
                    continue
                stage = len(appr)
                last = None
                try:
                    last = datetime.strptime(
                        str(r.get("reminded_on") or "")[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass
                if (r.get("reminded_stage") == stage and last
                        and (today - last).days < days_cfg):
                    continue          # für diese Stufe erst kürzlich erinnert
                team = current_team(r)
                if not mail_recipients("reminder", r, team):
                    continue          # Erinnerung in den Mail-Regeln nicht aktiv
                r["reminded_on"] = today.isoformat()
                r["reminded_stage"] = stage
                res_put(r)
                due.append((dict(r), team, waiting))
        for snap, team, waiting in due:
            audit(None, "Erinnerung gesendet",
                  res_detail(snap) + f" – wartet seit {waiting} Tagen"
                  + (f" auf {team}" if team else ""))
            mail_event("reminder", snap, team=team, actor="System", days=waiting)

    def reminder_loop():
        time.sleep(90)   # nach dem Anlauf; danach stündlich prüfen
        while True:
            try:
                check_reminders()
            except Exception as e:
                print(f"Erinnerungs-Prüfung fehlgeschlagen: {e}", file=sys.stderr)
            time.sleep(3600)

    def backup_loop():
        time.sleep(60)   # erst nach dem Anlauf (Cache/Migration abgeschlossen)
        while True:
            t0 = time.time()
            print(f"Backup gestartet -> {args.backup_target} ...", file=sys.stderr)
            try:
                name = sftp_backup(args)
                dur = time.time() - t0
                print(f"Backup übertragen: {name} -> {args.backup_target} "
                      f"({dur:.1f} s)", file=sys.stderr)
                audit(None, "Automatisches Backup",
                      f"{name} -> {args.backup_target} in {dur:.1f} s")
                try:
                    n = backup_rotate(args)
                    if n:
                        audit(None, "Backup-Rotation",
                              f"{n} Archiv(e) älter als "
                              f"{args.backup_keep_days} Tage gelöscht")
                except Exception as e:
                    print(f"Backup-Rotation fehlgeschlagen: {e}", file=sys.stderr)
                    audit(None, "Backup-Rotation fehlgeschlagen", str(e))
            except Exception as e:
                dur = time.time() - t0
                print(f"Backup FEHLGESCHLAGEN nach {dur:.1f} s "
                      f"(Ziel {args.backup_target}): {e}", file=sys.stderr)
                audit(None, "Automatisches Backup fehlgeschlagen",
                      f"Ziel {args.backup_target}: {e}")
            if args.backup_interval <= 0:
                return
            time.sleep(args.backup_interval)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype, code=200, headers=None):
            data = body.encode() if isinstance(body, str) else body
            # Text-/JSON-Antworten ab 1 KiB gzip-komprimieren, wenn der Client
            # es kann – die Hauptseite mit eingebetteten Daten schrumpft damit
            # auf rund ein Fünftel (Level 5: guter Kompromiss aus CPU/Größe).
            encoding = None
            if (len(data) >= 1024
                    and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
                    and ctype.split(";")[0].strip() in (
                        "text/html", "application/json", "text/csv", "text/plain")):
                data = gzip.compress(data, 5)
                encoding = "gzip"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            # Sicherheits-Header (Defense-in-depth): Clickjacking verhindern,
            # MIME-Sniffing abschalten, Skripte/Objekte auf same-origin begrenzen
            # (fängt zusätzlich einen etwaigen HTML-Injection-Ausbruch ab).
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200, headers=None):
            self._send(json.dumps(obj, ensure_ascii=False),
                       "application/json; charset=utf-8", code, headers)

        MAX_BODY = 2 * 1024 * 1024   # 2 MiB – reicht für große Reservierungs-Importe

        def _body(self):
            # Body-Größe begrenzen: /api/login ruft das VOR der Anmeldung auf,
            # ein riesiger Content-Length würde sonst den Prozess-Speicher fluten.
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > self.MAX_BODY:
                    return None
                return json.loads(self.rfile.read(n).decode() or "null")
            except Exception:
                return None

        def _lang(self):
            """Antwortsprache für CSV/OpenAPI: ?lang=de|en gewinnt, sonst
            Accept-Language (de* -> de, sonst en); ohne Header (curl/Skripte)
            bleibt es Deutsch – bestehende Consumer sehen keine Änderung."""
            q = urllib.parse.urlsplit(self.path).query
            forced = (urllib.parse.parse_qs(q).get("lang") or [""])[0].lower()
            if forced in ("de", "en"):
                return forced
            al = (self.headers.get("Accept-Language") or "").strip().lower()
            if not al:
                return "de"
            return "de" if al.startswith("de") else "en"

        # ---- Sitzungen / Berechtigungen ----
        def _cookie_token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "kapa_session":
                    return v
            return None

        def _session(self):
            if not auth_enabled:
                return {"user": None, "role": "admin", "abteilung": ""}  # ohne AD wie bisher
            s = sessions.get(_sess_key(self._cookie_token()))
            if s and s["exp"] > time.time():
                s["exp"] = time.time() + session_ttl
                sessions_dirty[0] = True   # Wartungs-Thread persistiert gesammelt
                return s
            return None

        def _require(self, *allowed):
            s = self._session()
            if not s:
                self._json({"error": "Nicht angemeldet"}, 401)
                return None
            if allowed and s["role"] not in allowed:
                self._json({"error": "Keine Berechtigung für diese Aktion"}, 403)
                return None
            return s

        def _bearer(self):
            """API-Token aus dem Authorization-Header prüfen."""
            h = self.headers.get("Authorization") or ""
            if not h.startswith("Bearer "):
                return None
            th = hashlib.sha256(h[7:].strip().encode()).hexdigest()
            now = datetime.now()
            with tokens_lock:
                for tid, t in tokens.items():
                    if hmac.compare_digest(str(t.get("hash") or ""), th):
                        stale = str(t.get("last_used") or "")[:10] != now.date().isoformat()
                        t["last_used"] = now.isoformat(timespec="seconds")
                        if stale:
                            save_tokens()
                        return dict(t, id=tid)
            audit(None, "API-Zugriff abgewiesen", "ungültiges Bearer-Token")
            return None

        def _bearer_scope(self, flag, label):
            """Schreibzugriff der v1-API: Bearer-Token mit dem jeweiligen
            Schreibrecht (per Klick in der Verwaltung) ODER eine angemeldete
            Admin-Session (zum Testen im Browser). Rückgabe: Actor-Name oder
            None – die Fehlerantwort ist dann bereits gesendet."""
            h = self.headers.get("Authorization") or ""
            if h.startswith("Bearer "):
                t = self._bearer()
                if not t:
                    self._json({"error": "Ungültiges oder widerrufenes Token"}, 401)
                    return None
                if not t.get(flag):
                    audit(None, "API-Schreibzugriff abgewiesen",
                          f"Token '{t.get('name')}' ohne Recht „{label}“")
                    self._json({"error": f"Token ohne Schreibrecht „{label}“ "
                                         "(in der Verwaltung aktivierbar)"}, 403)
                    return None
                return "api:" + (t.get("name") or t.get("id") or "?")
            s = self._session()
            if s and s["role"] == "admin":
                return s["user"] or "Admin"
            self._json({"error": "Bearer-Token mit Schreibrecht erforderlich"}, 401)
            return None

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            self._route = parsed.path
            self._query = urllib.parse.parse_qs(parsed.query)
            fn = self._GET_ROUTES.get(self._route)
            if fn:
                return fn(self)
            self.send_error(404)

        def _get_page(self):
            s = self._session()
            if auth_enabled and not s:
                self._send(LOGIN_TEMPLATE.replace("__VERSION__", VERSION)
                           .replace("__CONTACT__", _html_escape(args.contact_info))
                           .replace("__LOGIN_HINT__",
                                    _html_escape(args.login_hint)),
                           "text/html; charset=utf-8")
                return
            userinfo = ({"user": s["user"], "role": s["role"],
                         "abteilung": s.get("abteilung") or ""}
                        if auth_enabled else None)
            self._send(render_html(clusters_for(userinfo if userinfo else None),
                                   args.cpu_factor,
                                   serve_mode=True,
                                   updated=state["updated"] or
                                   "noch keine Daten – erster Abruf läuft ...",
                                   res_ttl=args.res_ttl_days,
                                   failover_hosts=args.failover_hosts,
                                   userinfo=userinfo, teams=approval_teams,
                                   rolenames=role_names, contact=args.contact_info,
                                   selector=cluster_selector,
                                   backup=bool(args.backup_target),
                                   notify=notify_cfg,
                                   prefs=user_prefs(s["user"]) if s else {},
                                   announce=public_announce(),
                                   tanzu_mhz=args.tanzu_mhz_per_vcpu,
                                   vis=vis_for(userinfo["role"] if userinfo else None),
                                   sources=known_sources()),
                       "text/html; charset=utf-8")

        def _get_data(self):
            s = self._require()
            if not s:
                return
            self._json({"updated": state["updated"],
                        "clusters": clusters_for(s)})

        def _get_status(self):
            if not self._require():
                return
            nxt = None
            if interval > 0 and any(tier_last.values()):
                nxt = max(0, int(min(tier_last[t] + tier_interval(t)
                                     for t in TIERS) - time.time()))
            self._json({"refreshing": state["refreshing"],
                        "progress": state["progress"], "error": state["error"],
                        "updated": state["updated"], "next": nxt,
                        "tiers": state.get("tiers") or {}})

        def _get_reservations(self):
            s = self._require()
            if not s:
                return
            with res_lock:
                prune_reservations()
                self._json(visible_res(s))

        def _get_openapi(self):
            self._send(json.dumps(openapi_spec(self._lang()), ensure_ascii=False, indent=2),
                       "application/json; charset=utf-8")

        def _get_v1_docs(self):
            self._send(API_DOCS_HTML.replace("__VERSION__", VERSION),
                       "text/html; charset=utf-8")

        def _get_reviewer_doc(self):
            # Reviewer-Handbuch (eigene, zweisprachige Doku-Seite, im UI
            # verlinkt; später erweiterbar).
            self._send(reviewer_doc_html(self._lang(), VERSION,
                                         role_names.get("reviewer") or "Reviewer"),
                       "text/html; charset=utf-8")

        def _get_healthz(self):
            # Monitoring-Endpunkt: bewusst OHNE Authentifizierung, dafür
            # nur unkritische Betriebsdaten (keine Cluster-/Nutzerdaten).
            age = (int(time.time() - state["last"]) if state.get("last")
                   else None)
            self._json({
                "status": ("error" if state.get("error")
                           and not state.get("clusters") else "ok"),
                "version": VERSION,
                "updated": state["updated"] or None,
                "data_age_seconds": age,
                "refreshing": bool(state["refreshing"]),
                "clusters": len(state.get("clusters") or []),
                "error": state.get("error") or None})

        def _get_v1_group(self):
            # Stabile v1-API für externe Anwendungen: Bearer-Token oder Session
            route = self._route
            query = self._query
            tok = self._bearer()
            s = None
            if not tok:
                s = self._session()
                if not s:
                    self._json({"error": "Bearer-Token oder Anmeldung "
                                         "erforderlich"}, 401)
                    return
            if route == "/api/v1/storage-requests":
                # Für das Storage-Team: offene (Standard) oder alle Anfragen,
                # inkl. NAA — als JSON oder CSV für die Automatisierung.
                only = (query.get("status", ["offen"])[0] or "offen").lower()
                with storage_lock:
                    rows = [dict(x) for x in storage_reqs
                            if only == "alle" or x.get("status") == only]
                # ESXi-Hosts des jeweiligen Clusters mitgeben — das Storage-
                # Team braucht sie fürs Zoning/LUN-Mapping. Token (externe
                # App) bekommt sie immer; eine Session nur, wenn die Rolle
                # Host-Sicht hat (Sichtbarkeits-Matrix).
                if bool(tok) or (s and vis_for(s["role"]).get("hosts", True)):
                    # je Host Name + WWPNs der FC-HBAs (fürs Zoning und zur
                    # System-Identifikation, falls Namen nicht gepflegt sind)
                    hmap = {c.get("name"): [{"name": h.get("name"),
                                             "wwpns": h.get("wwpns") or []}
                                            for h in (c.get("hosts") or [])]
                            for c in state["clusters"]}
                    for r in rows:
                        r["hosts"] = hmap.get(r.get("cluster"), [])
                if query.get("format", [""])[0] == "csv":
                    self._send(storagereq_csv(rows, self._lang()),
                               "text/csv; charset=utf-8")
                else:
                    self._json({"requests": rows})
                return
            if route == "/api/v1/status":
                nxt = None
                if interval > 0 and state["last"]:
                    nxt = max(0, int(state["last"] + interval - time.time()))
                self._json({"version": VERSION, "updated": state["updated"],
                            "refreshing": state["refreshing"], "next": nxt})
            elif route == "/api/v1/data":
                # Token = externe Anwendung (Workload ok); Session eines
                # Anforderers bekommt den Workload wie im UI nicht.
                cl = state["clusters"] if tok else clusters_for(s)
                if query.get("format", [""])[0] == "csv":
                    self._send(data_csv(cl, self._lang()),
                               "text/csv; charset=utf-8")
                else:
                    self._json({"updated": state["updated"], "clusters": cl})
            else:
                with res_lock:
                    prune_reservations()
                    data = (visible_res(s) if s and not tok
                            else [public_res(r) for r in reservations])
                for key in ("cluster", "abteilung"):
                    if key in query:
                        data = [r for r in data if r.get(key) == query[key][0]]
                if "status" in query:
                    data = [r for r in data
                            if res_status(r) == query["status"][0]]
                if query.get("format", [""])[0] == "csv":
                    self._send(res_csv(data, self._lang()),
                               "text/csv; charset=utf-8")
                else:
                    self._json(data)

        def _get_tokens(self):
            if not self._require("admin"):
                return
            with tokens_lock:
                self._json(token_list())

        def _get_roles(self):
            if not self._require("admin"):
                return
            self._json(roles_with_mail())

        def _get_teams(self):
            if not self._require("admin"):
                return
            with teams_lock:
                self._json({"teams": list(approval_teams)})

        def _get_selector(self):
            if not self._require("admin"):
                return
            with selector_lock:
                self._json({"selector": list(cluster_selector)})

        def _get_rolenames(self):
            if not self._require("admin"):
                return
            with rolenames_lock:
                self._json({"rolenames": dict(role_names)})

        def _get_notify(self):
            if not self._require("admin"):
                return
            with notify_lock:
                self._json({"notify": json.loads(json.dumps(notify_cfg)),
                            "default_template": DEFAULT_MAIL_TEMPLATE,
                            "default_subject": DEFAULT_MAIL_SUBJECT,
                            "vars": MAIL_VARS})

        def _get_webhooks(self):
            if not self._require("admin"):
                return
            # Secret NIE im Klartext an den Browser – nur ob eins gesetzt ist.
            with webhooks_lock:
                eps = [{"id": e["id"], "url": e["url"], "events": e["events"],
                        "active": e["active"], "desc": e["desc"],
                        "has_secret": bool(e["secret"])} for e in webhook_cfg]
            self._json({"endpoints": eps, "events": list(WEBHOOK_EVENTS)})

        def _get_announce(self):
            if not self._require("admin"):
                return
            with announce_lock:
                self._json({"announce": json.loads(json.dumps(announce_cfg))})

        def _get_autoapprove(self):
            if not self._require("admin"):
                return
            with autoapprove_lock:
                self._json({"autoapprove": json.loads(json.dumps(autoapprove_cfg))})

        def _get_visibility(self):
            if not self._require("admin"):
                return
            with visibility_lock:
                self._json({"visibility": json.loads(json.dumps(visibility_cfg)),
                            "features": list(VIS_FEATURES),
                            "roles": list(VIS_ROLES)})

        def _get_storage_requests(self):
            # Storage-Übersicht: für jede angemeldete Rolle sichtbar
            sess = self._require()
            if not sess:
                return
            cmap = {c.get("name"): c.get("source")
                    for c in state["clusters"]}
            with storage_lock:
                reqs = [r for r in storage_reqs
                        if _src_ok(sess, r.get("cluster"), cmap)]
                self._json({"enabled": storage_cfg["enabled"],
                            "max_lun_gb": storage_cfg.get("max_lun_gb", 0),
                            "requests": json.loads(json.dumps(reqs))})

        def _get_storagecfg(self):
            if not self._require("admin"):
                return
            with storage_lock:
                self._json({"enabled": storage_cfg["enabled"],
                            "min_lun_gb": storage_cfg.get("min_lun_gb", 0),
                            "max_lun_gb": storage_cfg.get("max_lun_gb", 0),
                            "exclude_names": ", ".join(
                                storage_cfg.get("exclude_names") or []),
                            "vsan_factor": storage_cfg.get("vsan_factor",
                                                           args.vsan_factor),
                            "vsan_overrides": dict(
                                storage_cfg.get("vsan_overrides") or {})})

        def _get_netcfg(self):
            if not self._require("admin"):
                return
            with net_lock:
                self._json({"exclude_names": ", ".join(
                                net_cfg.get("exclude_names") or []),
                            "exclude_vlans": ", ".join(
                                net_cfg.get("exclude_vlans") or [])})

        def _get_import(self):
            if not self._require("admin"):
                return
            with import_lock:
                self._json({"sources": [
                    {"name": src,
                     "clusters": len(e.get("clusters") or []),
                     "hosts": sum(len(c.get("hosts") or [])
                                  for c in e.get("clusters") or []),
                     "vms": sum(len(c.get("vms") or [])
                                for c in e.get("clusters") or []),
                     "imported_on": e.get("imported_on") or "",
                     "imported_by": e.get("imported_by") or ""}
                    for src, e in sorted(import_sources.items())]})

        def _get_import_sample(self):
            # Vorlage für den Kapa-CSV-Import: Kopfzeile + Beispielzeilen,
            # mit BOM und Semikolon, damit Excel sie direkt sauber öffnet.
            if not self._require("admin"):
                return
            sample = ("﻿Kapa-Nummer;Projekt;Cluster;CPU;RAM;Storage;"
                      "Datum;Change;Anforderer;Team\r\n"
                      "KAPA-2024-017;SAP-Erweiterung Q4;Cluster-01;16;128;"
                      "2000;15.06.2026;OPS-4711;anna.schmidt@firma.local;"
                      "Team Betrieb\r\n"
                      "KAPA-2024-018;Fileservice Migration;Cluster-02;8;64;"
                      "12000;01.07.2026;;;\r\n")
            self._send(sample, "text/csv; charset=utf-8", headers={
                "Content-Disposition":
                    'attachment; filename="kapa-import-beispiel.csv"'})

        def _get_import_powercli(self):
            if not self._require("admin"):
                return
            self._send(POWERCLI_PS1, "text/plain; charset=utf-8", headers={
                "Content-Disposition":
                    'attachment; filename="kapa_export.ps1"'})

        def _get_refreshcfg(self):
            if not self._require("admin"):
                return
            with refresh_lock:
                self._json({"tiers": dict(refresh_cfg),
                            "default_min": max(1, interval // 60),
                            "tz": refresh_cfg.get("tz", ""),
                            "now": datetime.now().strftime("%d.%m.%Y %H:%M")})

        def _get_history(self):
            # Statistik-Historie (Trends). Sichtbarkeit per Matrix-Feature
            # „statistik" (Admin/Betrieb ohne Anmeldung sehen immer alles).
            query = self._query
            s = self._require()
            if not s:
                return
            if not vis_for(s["role"]).get("statistik", True):
                self._json({"error": "Statistik ist für diese Rolle "
                            "ausgeblendet"}, 403)
                return
            try:
                days = max(1, min(int(query.get("days", ["365"])[0]), 3660))
            except ValueError:
                days = 365
            cutoff = (datetime.now().date()
                      - timedelta(days=days)).isoformat()
            with history_lock:
                sel = {d: v for d, v in history.items() if d >= cutoff}
            if query.get("format", [""])[0] == "csv":
                import csv as _csv
                import io as _io
                buf = _io.StringIO()
                w = _csv.writer(buf, delimiter=";")
                w.writerow(["datum", "cluster", "quelle", "vms", "vms_an",
                            "vcpu_summe", "ram_gb_summe", "disk_gb_summe",
                            "vms_mit_disk", "ram_belegt", "ram_kap",
                            "storage_belegt", "storage_kap",
                            "vcpu_belegt", "vcpu_kap"])
                for d in sorted(sel):
                    for cl, e in sorted(sel[d].items()):
                        _wrow(w, [d, cl, e.get("src", ""), e.get("n", 0),
                                  e.get("on", 0), e.get("vcpu", 0),
                                  e.get("ram", 0), e.get("disk", 0),
                                  e.get("nd", 0), e.get("ramU", 0),
                                  e.get("ramC", 0), e.get("stU", 0),
                                  e.get("stC", 0), e.get("vcU", 0),
                                  e.get("vcC", 0)])
                self._send(buf.getvalue(), "text/csv; charset=utf-8")
            else:
                self._json({"days": sel})

        def _get_config(self):
            if not self._require("admin"):
                return
            self._json({"config": public_config()})

        def _get_prefs(self):
            s = self._require()
            if not s:
                return
            self._json(user_prefs(s["user"]))

        def _get_log(self):
            if not self._require("admin"):
                return
            self._json(read_log(1500))

        _GET_ROUTES = {
            "/": _get_page, "/index.html": _get_page,
            "/reservierungen": _get_page, "/genehmigungen": _get_page,
            "/archiv": _get_page, "/statistik": _get_page,
            "/verwaltung": _get_page, "/log": _get_page,
            "/api/data": _get_data,
            "/api/status": _get_status,
            "/api/reservations": _get_reservations,
            "/api/v1/openapi.json": _get_openapi,
            "/api/v1/docs": _get_v1_docs, "/api/v1/docs/": _get_v1_docs,
            "/reviewer-handbuch": _get_reviewer_doc,
            "/reviewer-handbuch/": _get_reviewer_doc,
            "/reviewer-handbook": _get_reviewer_doc,
            "/healthz": _get_healthz,
            "/api/v1/reservations": _get_v1_group, "/api/v1/data": _get_v1_group,
            "/api/v1/status": _get_v1_group,
            "/api/v1/storage-requests": _get_v1_group,
            "/api/tokens": _get_tokens,
            "/api/roles": _get_roles,
            "/api/teams": _get_teams,
            "/api/selector": _get_selector,
            "/api/rolenames": _get_rolenames,
            "/api/notify": _get_notify,
            "/api/webhooks": _get_webhooks,
            "/api/announce": _get_announce,
            "/api/autoapprove": _get_autoapprove,
            "/api/visibility": _get_visibility,
            "/api/storage-requests": _get_storage_requests,
            "/api/storagecfg": _get_storagecfg,
            "/api/netcfg": _get_netcfg,
            "/api/import": _get_import,
            "/api/import/reservations/beispiel": _get_import_sample,
            "/api/import/powercli": _get_import_powercli,
            "/api/refreshcfg": _get_refreshcfg,
            "/api/history": _get_history,
            "/api/config": _get_config,
            "/api/prefs": _get_prefs,
            "/api/log": _get_log,
        }

        def do_POST(self):
            fn = self._POST_ROUTES.get(self.path)
            if fn:
                return fn(self)
            for pred, pfn in self._POST_PATTERNS:
                if pred(self.path):
                    return pfn(self)
            self.send_error(404)

        def _post_storage_request(self):
            # Storage-Erweiterung anlegen — aus dem Freigabe-Dialog (mit
            # res_id, dann Reviewer-Team-Check) ODER direkt aus der
            # Storage-Übersicht (ohne res_id, Ad-hoc).
            s = self._require("admin", "reviewer")
            if not s:
                return
            with storage_lock:
                if not storage_cfg["enabled"]:
                    self._json({"error": "Storage-Erweiterungen sind nicht "
                                         "aktiviert."}, 403)
                    return
            body = self._body() or {}
            rid = str(body.get("res_id") or "")
            if rid and s["role"] == "reviewer":
                with res_lock:
                    r = next((x for x in reservations if x.get("id") == rid), None)
                    team = current_team(r) if r else None
                if not approval_teams or (s.get("abteilung") or "") != (team or ""):
                    self._json({"error": "Nur das aktuell zuständige Team "
                                         "darf das zum Antrag anfragen."}, 403)
                    return
            req = clean_storage_req(body, s["user"] or "")
            if req is None:
                self._json({"error": "Ungültige Storage-Anfrage (Cluster, "
                                     "LUN/Größe prüfen)"}, 400)
                return
            with storage_lock:
                maxg = storage_cfg.get("max_lun_gb", 0)
            want = req.get("target_gb") or req.get("size_gb") or 0
            if maxg and want > maxg:
                self._json({"error": f"Die angefragte Größe ({want} GB) "
                            f"überschreitet das Maximum von {maxg} GB "
                            f"({round(maxg / 1024, 1)} TB)."}, 400)
                return
            with storage_lock:
                storage_reqs.append(req)
                save_storage_reqs()
            detail = (f"neue LUN {req['size_gb']} GB" if req["kind"] == "new"
                      else f"{req['lun_name']} → {req['target_gb']} GB")
            audit(s["user"], "Storage-Erweiterung angefragt",
                  f"{req['cluster']} · {detail}"
                  + (f" (zu {req['res_name']})" if req["res_name"] else ""))
            fire_webhooks("storage_requested",
                          {"actor": s["user"] or "", "request": req})
            self._json({"request": req}, 201)

        def _post_webhooks_test(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body() or {}
            url = " ".join(str(body.get("url") or "").split())[:400]
            if not re.match(r"^https?://", url, re.I):
                self._json({"error": "Gültige http(s)-URL nötig"}, 400)
                return
            secret = str(body.get("secret") or "")[:200]
            if not secret:      # bekanntes Ziel ohne neues Secret -> gespeichertes
                with webhooks_lock:
                    for e in webhook_cfg:
                        if e["url"] == url and e.get("secret"):
                            secret = e["secret"]
                            break
            payload = {"event": "test",
                       "at": datetime.now().isoformat(timespec="seconds"),
                       "message": "Test-Webhook vom Kapa-Dashboard",
                       "by": s["user"] or ""}
            bodyb = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8",
                       "User-Agent": "kapa-dashboard/" + VERSION,
                       "X-Kapa-Event": "test",
                       "X-Kapa-Delivery": uuid.uuid4().hex}
            if secret:
                headers["X-Kapa-Signature"] = "sha256=" + hmac.new(
                    secret.encode("utf-8"), bodyb, hashlib.sha256).hexdigest()
            try:
                req = urllib.request.Request(url, data=bodyb, method="POST",
                                             headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310
                    code = resp.status
                audit(s["user"], "Webhook-Test gesendet", f"{url} (HTTP {code})")
                self._json({"ok": True, "status": code})
            except Exception as ex:
                self._json({"ok": False, "error": str(ex)[:200]})

        def _post_login(self):
            if not auth_enabled:
                self.send_error(404)
                return
            body = self._body() or {}
            # Passwort statt Benutzername eingefügt (Passwort-Manager)?
            # Dann: NICHT ans AD schicken, NICHT protokollieren (das wäre
            # ein Klartext-Passwort im Audit-Log), freundlicher Hinweis.
            if suspicious_login_name(body.get("username")):
                audit(None, "Anmeldung fehlgeschlagen",
                      "Eingabe im Benutzerfeld sah wie ein Passwort aus – "
                      "Wert bewusst nicht protokolliert")
                self._json({"error": "Die Eingabe im Benutzername-Feld "
                                     "sieht wie ein Passwort aus – bitte "
                                     "den Anmeldenamen eintragen."}, 401)
                return
            user = normalize_user(body.get("username"))
            pw = str(body.get("password") or "")
            # Einheitliche Antwort für „kein Konto", „keine Rolle" und
            # „falsches Passwort" – verrät nicht, welche Konten berechtigt sind.
            # (Keine IP im Log: hinter dem nginx-Proxy wäre das immer 127.0.0.1.)
            bad = {"error": "Benutzername oder Passwort falsch."}
            if login_blocked(user):
                audit(user, "Anmeldung gesperrt", "zu viele Fehlversuche")
                self._json({"error": "Zu viele Fehlversuche – bitte einige "
                                     "Minuten warten und erneut versuchen."}, 429)
                return
            if not user:
                login_note_fail(user)
                self._json(bad, 401)
                return
            try:
                ok = ldap_bind(args.ad_url, user, pw, insecure=args.ad_insecure)
            except Exception as e:
                self._json({"error": f"Active Directory nicht erreichbar: {e}"}, 502)
                return
            if not ok:
                login_note_fail(user)
                audit(user, "Anmeldung fehlgeschlagen", "falsches Passwort")
                self._json(bad, 401)
                return
            login_reset(user)
            # Rolle: 1) direkt zugewiesen (Verwaltung), 2) über AD-Gruppe,
            # 3) Standard = Anforderer. Ohne Rolle darf man nur beantragen.
            cns = []
            explicit = role_entry(user)
            source = ""
            if explicit:
                entry = explicit
            else:
                grp = None
                if args.ad_bind_dn and has_group_entries():
                    try:
                        cns = ldap_member_of(args.ad_url, args.ad_bind_dn,
                                             args.ad_bind_password, args.ad_base_dn,
                                             user, insecure=args.ad_insecure)
                        grp = group_role(cns)
                    except Exception as e:
                        print(f"AD-Gruppensuche fehlgeschlagen: {e}", file=sys.stderr)
                entry = grp or {"role": "anforderer", "abteilung": ""}
                source = " (AD-Gruppe)" if grp else " (Standard)"
            audit(user, "Anmeldung", f"Rolle: {entry['role']}{source}"
                  + (f", Abteilung/Team: {entry.get('abteilung')}"
                     if entry.get("abteilung") else ""))
            # Mailadresse des Anforderers: standardmäßig der Anmeldename (UPN);
            # optional aus einem anderen AD-Attribut (--ad-mail-attribute).
            mail_addr = ""
            if args.ad_mail_attribute and args.ad_bind_dn:
                try:
                    mail_addr = ldap_user_attr(
                        args.ad_url, args.ad_bind_dn, args.ad_bind_password,
                        args.ad_base_dn, user, args.ad_mail_attribute,
                        insecure=args.ad_insecure)
                except Exception as e:
                    print(f"AD-Mailattribut-Suche fehlgeschlagen: {e}",
                          file=sys.stderr)
            # Abgelaufene Sitzungen bei der Gelegenheit entsorgen, sonst
            # wächst das Dict über Monate unbegrenzt.
            now = time.time()
            for k in [k for k, v in sessions.items() if v["exp"] <= now]:
                sessions.pop(k, None)
            allowed_src = effective_sources(user, cns)
            token = secrets.token_urlsafe(32)
            sessions[_sess_key(token)] = {"user": user, "role": entry["role"],
                               "abteilung": entry.get("abteilung") or "",
                               "mail": mail_addr, "sources": allowed_src,
                               "exp": time.time() + session_ttl}
            save_sessions()
            secure = "" if args.cookie_insecure else " Secure;"
            self._json({"user": user, "role": entry["role"],
                        "abteilung": entry.get("abteilung") or ""},
                       headers={"Set-Cookie": f"kapa_session={token}; "
                                f"HttpOnly;{secure} SameSite=Lax; Path=/"})

        def _post_logout(self):
            old = sessions.pop(_sess_key(self._cookie_token()), None)
            if old:
                save_sessions()
                audit(old["user"], "Abmeldung")
            secure = "" if args.cookie_insecure else " Secure;"
            self._json({"ok": True},
                       headers={"Set-Cookie": "kapa_session=; Max-Age=0; "
                                f"HttpOnly;{secure} SameSite=Lax; Path=/"})

        def _post_refresh(self):
            if not self._require("admin"):
                return
            body = self._body() or {}
            parts = body.get("parts")
            if parts is not None:
                parts = [p for p in parts if p in TIERS] \
                    if isinstance(parts, list) else None
                if not parts:
                    self._json({"error": "parts: vms, network und/oder "
                                "storage erwartet"}, 400)
                    return
            if not state["refreshing"]:
                audit(self._session()["user"], "Datenabruf aus Aria gestartet",
                      "Bereiche: " + (", ".join(sorted(parts))
                                      if parts else "alle"))
                threading.Thread(target=do_refresh, args=(parts,),
                                 daemon=True).start()
            self._json({"started": True}, 202)

        # ---- v1-API: Schreib-Endpunkte (Bearer-Token mit Schreibrecht) ----
        # Bewusst kompakte Spiegelungen der Session-Handler mit
        # Admin-Semantik (keine Team-Beschränkung); Actor = "api:<Tokenname>".
        def _post_v1_reservations(self):
            actor = self._bearer_scope("write_res", "Reservierungen")
            if not actor:
                return
            item = self._body()
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                self._json({"error": "Ungültige Reservierung (name erforderlich)"}, 400)
                return
            oneline = lambda v, n: " ".join(str(v or "").split())[:n]
            von = oneline(item.get("von"), 120) or actor
            try:
                entry = {"id": new_res_id(),
                         "cluster": oneline(item.get("cluster"), 120),
                         "name": oneline(item.get("name"), 120),
                         "change": oneline(item.get("change"), 60),
                         "vcpu": int(float(item.get("vcpu") or 0)),
                         "ram_gb": int(float(item.get("ram_gb") or 0)),
                         "storage_gb": int(float(item.get("storage_gb") or 0)),
                         "von": von,
                         "von_mail": von if "@" in von else "",
                         "abteilung": oneline(item.get("abteilung"), 60),
                         "created": datetime.now().date().isoformat(),
                         "approvals": [],
                         "approved": False}
            except (TypeError, ValueError):
                self._json({"error": "Ungültige Zahlenwerte"}, 400)
                return
            with res_lock:
                prune_reservations()
                reservations.append(entry)
                res_put(entry)
                auto_events = try_auto_approve(entry)
                snap = dict(entry)
            self._json({"reservation": public_res(snap)}, 201)
            audit(actor, "Antrag erstellt (API)", res_detail(snap))
            emit_auto_events(snap, auto_events)
            mail_event("created", snap, actor=actor)
            if snap.get("approved"):
                mail_event("approved", snap, actor="Auto-Freigabe")
            elif approval_teams:
                mail_event("team_turn", snap, team=current_team(snap),
                           actor=actor)

        def _post_v1_res_decision(self):
            op = self.path.rsplit("/", 1)[1]
            flag, label = (("write_approve", "Genehmigungen")
                           if op in ("approve", "reject")
                           else ("write_res", "Reservierungen"))
            actor = self._bearer_scope(flag, label)
            if not actor:
                return
            rid = urllib.parse.unquote(
                self.path[len("/api/v1/reservations/"):-(len(op) + 1)])
            comment = str((self._body() or {}).get("comment") or "").strip()[:64]
            notify = None
            action = None
            auto_events = []
            with res_lock:
                r = res_find_open(rid, for_cancel=(op == "cancel"))
                if r is not None:
                    if op == "approve":
                        action = res_apply_approve(r, actor, comment)
                        res_put(r)
                        auto_events = try_auto_approve(r)
                    elif op == "reject":
                        res_apply_reject(r, actor, comment)
                        res_put(r)
                    else:
                        res_apply_cancel(r, actor, comment)
                        res_put(r)
                    notify = dict(r)
            if notify is None:
                self._json({"error": "Antrag nicht gefunden oder bereits "
                                     "entschieden."}, 404)
                return
            self._json({"reservation": public_res(notify)})
            res_decision_notify(op, notify, actor, comment,
                                action=action, api=True)
            emit_auto_events(notify, auto_events)

        def _post_v1_storage_done(self):
            # Storage-Team meldet Umsetzung: Schreibrecht „Storage"
            actor = self._bearer_scope("write_storage", "Storage")
            if not actor:
                return
            sid = urllib.parse.unquote(
                self.path[len("/api/v1/storage-requests/"):-len("/done")])
            with storage_lock:
                req = next((x for x in storage_reqs if x.get("id") == sid), None)
                if not req:
                    self._json({"error": "Anfrage nicht gefunden"}, 404)
                    return
                req["status"] = "erledigt"
                req["done_by"] = actor
                req["done_on"] = datetime.now().date().isoformat()
                save_storage_reqs()
                result = dict(req)
            audit(actor, "Storage-Erweiterung erledigt (API)",
                  req.get("lun_name") or f"neue LUN ({req.get('cluster')})")
            fire_webhooks("storage_done", {"actor": actor, "request": result})
            self._json({"request": result})

        def _post_reservations(self):
            s = self._require("admin", "anforderer")
            if not s:
                return
            item = self._body()
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                self._json({"error": "Ungültige Reservierung"}, 400)
                return
            # Change / Jira-Ticket ist freiwillig und frei wählbar (kein Format).
            # Freitextfelder: Whitespace (inkl. Zeilenumbrüche) zu Leerzeichen
            # falten und Länge begrenzen – ein \n im Namen würde sonst den
            # Mail-Betreff ungültig machen (EmailMessage lehnt CR/LF ab) und
            # so die Benachrichtigungen still unterdrücken.
            oneline = lambda v, n: " ".join(str(v or "").split())[:n]
            change = oneline(item.get("change"), 60)
            try:
                entry = {"id": new_res_id(),
                         "cluster": oneline(item.get("cluster"), 120),
                         "name": oneline(item.get("name"), 120),
                         "change": change,
                         "vcpu": int(float(item.get("vcpu") or 0)),
                         "ram_gb": int(float(item.get("ram_gb") or 0)),
                         "storage_gb": int(float(item.get("storage_gb") or 0)),
                         "von": s["user"] or "",
                         "von_mail": s.get("mail") or "",
                         "abteilung": s.get("abteilung") or "",
                         "created": datetime.now().date().isoformat(),
                         "approvals": [],
                         "approved": False}
            except (TypeError, ValueError):
                self._json({"error": "Ungültige Zahlenwerte"}, 400)
                return
            with res_lock:
                prune_reservations()
                reservations.append(entry)
                res_put(entry)
                auto_events = try_auto_approve(entry)
                snap = dict(entry)
                self._json(visible_res(s))
            audit(s["user"], "Antrag erstellt", res_detail(snap))
            emit_auto_events(snap, auto_events)
            mail_event("created", snap, actor=s["user"] or "")
            if snap.get("approved"):
                mail_event("approved", snap, actor="Auto-Freigabe")
            elif approval_teams:             # nächstes manuelles Team ist dran
                mail_event("team_turn", snap, team=current_team(snap),
                           actor=s["user"] or "")

        def _post_res_decision(self):
            op = self.path.rsplit("/", 1)[1]
            s = self._require("admin", "reviewer")
            if not s:
                return
            rid = urllib.parse.unquote(
                self.path[len("/api/reservations/"):-(len(op) + 1)])
            comment = str((self._body() or {}).get("comment") or "").strip()[:64]
            notify = None
            action = None
            err = None
            with res_lock:
                r = res_find_open(rid)
                if r is None:
                    err = ("Antrag nicht gefunden oder bereits entschieden.", 404)
                else:
                    team = current_team(r)
                    # Reviewer nur, wenn ihr Team gerade an der Reihe ist
                    if s["role"] == "reviewer" and (
                            not approval_teams
                            or (s.get("abteilung") or "") != (team or "")):
                        err = ("Ihr Team ist für diesen Antrag gerade nicht "
                               "an der Reihe.", 403)
                    elif op == "approve":
                        action = res_apply_approve(r, s["user"] or "", comment)
                        res_put(r)
                        # danach ggf. auto-freigebbare Folgestufen
                        auto_events = try_auto_approve(r)
                        notify = dict(r)
                    else:
                        res_apply_reject(r, s["user"] or "", comment)
                        notify = dict(r)
                        res_put(r)
                resp = None if err else visible_res(s)
            if err:
                self._json({"error": err[0]}, err[1])
                return
            self._json(resp)
            if notify:
                res_decision_notify(op, notify, s["user"] or "", comment,
                                    action=action)
                if op == "approve":
                    emit_auto_events(notify, auto_events)

        def _post_res_cancel(self):
            # Storno: jemand aus derselben Abteilung (oder der Anforderer
            # selbst bzw. ein Admin) zieht die Anfrage zurück. Sie bleibt als
            # „storniert" in der Historie und zählt nicht mehr gegen die
            # Kapazität.
            s = self._require("admin", "anforderer", "reviewer")
            if not s:
                return
            rid = urllib.parse.unquote(
                self.path[len("/api/reservations/"):-len("/cancel")])
            comment = str((self._body() or {}).get("comment") or "").strip()[:64]
            notify = None
            err = None
            with res_lock:
                r = res_find_open(rid, for_cancel=True)
                if r is None:
                    err = ("Antrag nicht gefunden oder bereits "
                           "abgeschlossen.", 404)
                else:
                    dept = s.get("abteilung") or ""
                    same_dept = bool(dept) and r.get("abteilung") == dept
                    if not (s["role"] == "admin" or same_dept
                            or r.get("von") == s["user"]):
                        err = ("Nur die eigene Abteilung (oder ein Admin) "
                               "darf diese Anfrage stornieren.", 403)
                    else:
                        res_apply_cancel(r, s["user"] or "", comment)
                        notify = dict(r)
                        res_put(r)
                resp = None if err else visible_res(s)
            if err:
                self._json({"error": err[0]}, err[1])
                return
            self._json(resp)
            if notify:
                res_decision_notify("cancel", notify, s["user"] or "", comment)

        def _post_backup(self):
            s = self._require("admin")
            if not s:
                return
            t0 = time.time()
            print(f"Backup (manuell durch {s['user'] or '?'}) -> "
                  f"{args.backup_target} ...", file=sys.stderr)
            try:
                with res_lock:
                    name = sftp_backup(args)
                dur = time.time() - t0
                print(f"Backup übertragen: {name} -> {args.backup_target} "
                      f"({dur:.1f} s)", file=sys.stderr)
                audit(s["user"], "Backup ausgelöst",
                      f"{name} -> {args.backup_target} in {dur:.1f} s")
                rotated = 0
                try:
                    rotated = backup_rotate(args)
                    if rotated:
                        audit(s["user"], "Backup-Rotation",
                              f"{rotated} Archiv(e) gelöscht")
                except Exception as e:
                    print(f"Backup-Rotation fehlgeschlagen: {e}", file=sys.stderr)
                    audit(s["user"], "Backup-Rotation fehlgeschlagen", str(e))
                self._json({"ok": True, "backup": name, "rotated": rotated})
            except Exception as e:
                dur = time.time() - t0
                msg = f"Ziel {args.backup_target}: {e}"
                print(f"Backup FEHLGESCHLAGEN nach {dur:.1f} s ({msg})",
                      file=sys.stderr)
                audit(s["user"], "Backup fehlgeschlagen", msg)
                self._json({"error": str(e)}, 502)

        def _post_tokens(self):
            s = self._require("admin")
            if not s:
                return
            name = str((self._body() or {}).get("name") or "").strip()
            if not name:
                self._json({"error": "Name der Anwendung erforderlich"}, 400)
                return
            raw = "kapa_" + secrets.token_urlsafe(24)
            tid = uuid.uuid4().hex[:8]
            with tokens_lock:
                tokens[tid] = {"name": name,
                               "hash": hashlib.sha256(raw.encode()).hexdigest(),
                               "prefix": raw[:11] + "…", "scope": "read",
                               "created": datetime.now().date().isoformat(),
                               "created_by": s["user"] or "",
                               "last_used": ""}
                save_tokens()
            audit(s["user"], "API-Token erstellt",
                  f"{name} ({raw[:11]}…, lesend – Schreibrechte per Klick)")
            self._json({"token": raw, "tokens": token_list()})

        def _post_roles(self):
            if not self._require("admin"):
                return
            body = self._body() or {}
            kind = "group" if body.get("kind") == "group" else "user"
            # Gruppennamen NICHT als UPN normalisieren
            key = (str(body.get("user") or "").strip() if kind == "group"
                   else normalize_user(body.get("user")))
            role = body.get("role")
            dept = str(body.get("abteilung") or "").strip()
            srcs = clean_sources(body.get("sources"))
            if not key or role not in VALID_ROLES:
                self._json({"error": "Name/Gruppe und gültige Rolle erforderlich"}, 400)
                return
            with roles_lock:
                roles[key] = {"role": role, "abteilung": dept, "kind": kind,
                              "sources": srcs}
                save_roles()
            self._json(roles_with_mail())
            audit(self._session()["user"],
                  "AD-Gruppe zugewiesen" if kind == "group" else "Rolle zugewiesen",
                  f"{key} -> {role}" + (f" ({dept})" if dept else "")
                  + (f" · Quellen: {', '.join(srcs)}" if srcs
                     else (" · alle Quellen" if known_sources() else "")))

        def _post_import(self):
            # Offline-Quelle importieren/ersetzen (PowerCLI-JSON + Quellname)
            s = self._require("admin")
            if not s:
                return
            body = self._body() or {}
            src = " ".join(str(body.get("source") or "").split())[:40]
            src = re.sub(r"[^\w .\-äöüÄÖÜß]", "", src).strip()
            if not src:
                self._json({"error": "Quellname erforderlich "
                            "(z. B. RZ-Insel)"}, 400)
                return
            raw = body.get("clusters")
            if raw is None and isinstance(body.get("data"), dict):
                raw = body["data"].get("clusters")
            cleaned, err, skipped = clean_import_clusters(raw)
            if err:
                self._json({"error": err}, 400)
                return
            with import_lock:
                replaced = src in import_sources
                import_sources[src] = {
                    "imported_on": datetime.now().date().isoformat(),
                    "imported_by": s["user"] or "",
                    "clusters": cleaned}
                save_imports()
            audit(s["user"], "Offline-Quelle importiert",
                  f"{src}: {len(cleaned)} Cluster, "
                  f"{sum(len(c['hosts']) for c in cleaned)} Hosts, "
                  f"{sum(len(c['vms']) for c in cleaned)} VMs"
                  + (f" · übersprungen (ohne Hosts): {', '.join(skipped)}"
                     if skipped else "")
                  + (" – ersetzt" if replaced else ""))
            if not state["refreshing"]:
                threading.Thread(target=do_refresh, daemon=True).start()
            fire_webhooks("imported",
                          {"actor": s["user"] or "", "source": src,
                           "clusters": len(cleaned), "skipped": skipped,
                           "replaced": replaced})
            self._json({"ok": True, "source": src,
                        "clusters": len(cleaned), "skipped": skipped}, 201)

        def _post_import_reservations(self):
            # Kapa-Anfragen aus einer Excel-CSV übernehmen (XLS-Ablösung).
            # Alle Zeilen kommen als GENEHMIGT an (Freigebender „Import"),
            # Gültigkeit rechnet ab dem Original-Datum aus der Datei.
            s = self._require("admin")
            if not s:
                return
            text = str((self._body() or {}).get("csv") or "")
            if not text.strip():
                self._json({"error": "CSV-Inhalt fehlt"}, 400)
                return
            import csv as _csv
            import io as _io
            text = text.lstrip("﻿")             # Excel-BOM
            delim = ";" if text.splitlines()[0].count(";") \
                >= text.splitlines()[0].count(",") else ","
            rows = list(_csv.reader(_io.StringIO(text), delimiter=delim))
            if len(rows) < 2:
                self._json({"error": "CSV braucht Kopfzeile + mindestens "
                            "eine Datenzeile"}, 400)
                return
            # Spaltenköpfe tolerant zuordnen
            ALIAS = {"id": ("kapa-nummer", "kapanummer", "kapa", "nummer", "id"),
                     "name": ("projekt", "projektname", "name", "bezeichnung",
                              "anfrage"),
                     "cluster": ("cluster", "ziel-cluster", "zielcluster"),
                     "vcpu": ("cpu", "vcpu", "vcpus", "cores"),
                     "ram_gb": ("ram", "ram_gb", "ram (gb)", "memory"),
                     "storage_gb": ("storage", "storage_gb", "storage (gb)",
                                    "disk", "platte"),
                     "created": ("datum", "datum der anfrage", "angefragt",
                                 "angefragt am", "beantragt am", "erstellt"),
                     "change": ("change", "jira", "ticket", "change/jira"),
                     "von": ("anforderer", "von", "requester"),
                     "abteilung": ("team", "abteilung")}
            head = [h.strip().lower().lstrip("﻿") for h in rows[0]]
            colmap = {}
            for field, names in ALIAS.items():
                for i, h in enumerate(head):
                    if h in names:
                        colmap[field] = i
                        break
            missing = [f for f in ("id", "name", "cluster", "vcpu",
                                   "ram_gb", "storage_gb", "created")
                       if f not in colmap]
            if missing:
                self._json({"error": "Spalten nicht gefunden: "
                            + ", ".join(missing) + " – erkannte Köpfe: "
                            + ", ".join(head)}, 400)
                return

            def _int_de(v):
                v = str(v or "").strip().replace(" ", "")
                if "." in v and "," in v:
                    v = v.replace(".", "").replace(",", ".")
                elif "," in v:
                    v = v.replace(",", ".")
                elif v.count(".") == 1 and len(v.split(".")[1]) == 3:
                    v = v.replace(".", "")     # 1.024 = Tausenderpunkt
                return int(float(v))

            def _date_de(v):
                v = str(v or "").strip()
                for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
                    try:
                        return datetime.strptime(v, fmt).date().isoformat()
                    except ValueError:
                        continue
                raise ValueError(f"Datum nicht lesbar: {v!r}")

            oneline = lambda v, n: " ".join(str(v or "").split())[:n]
            imported, skipped, errors = [], 0, []
            with res_lock:
                existing = {r.get("id") for r in reservations}
                known_cl = {c.get("name") for c in state.get("clusters") or []}
                unknown_cl = set()
                for ln, row in enumerate(rows[1:5001], start=2):
                    if not any(x.strip() for x in row):
                        continue
                    get = lambda f: (row[colmap[f]].strip()
                                     if f in colmap and colmap[f] < len(row)
                                     else "")
                    try:
                        rid = oneline(get("id"), 40)
                        if not rid:
                            raise ValueError("Kapa-Nummer fehlt")
                        if rid in existing:
                            skipped += 1
                            continue
                        cl = oneline(get("cluster"), 120)
                        if not cl:
                            raise ValueError("Cluster fehlt")
                        if cl not in known_cl:
                            unknown_cl.add(cl)
                        entry = {"id": rid,
                                 "name": oneline(get("name"), 120)
                                         or rid,
                                 "cluster": cl,
                                 "change": oneline(get("change"), 60),
                                 "vcpu": _int_de(get("vcpu")),
                                 "ram_gb": _int_de(get("ram_gb")),
                                 "storage_gb": _int_de(get("storage_gb")),
                                 "von": oneline(get("von"), 120)
                                        or (s["user"] or "Import"),
                                 "von_mail": "",
                                 "abteilung": oneline(get("abteilung"), 60),
                                 "created": _date_de(get("created")),
                                 "approvals": [],
                                 "approved": True,
                                 "approved_by": "Import",
                                 "approved_on": datetime.now().date()
                                                .isoformat()}
                        imported.append(entry)
                        existing.add(rid)
                    except (ValueError, IndexError, TypeError,
                            OverflowError, KeyError) as e:
                        errors.append(f"Zeile {ln}: {e}")
                if not imported and errors:
                    self._json({"error": "Keine Zeile importierbar – "
                                + "; ".join(errors[:5])}, 400)
                    return
                reservations.extend(imported)
                before = len(reservations)
                reservations[:] = prune_res(reservations)
                expired = before - len(reservations)
                save_res()
            audit(s["user"], "Kapa-Anfragen importiert (CSV)",
                  f"{len(imported)} übernommen (genehmigt, Freigebender "
                  f"„Import“), {skipped} übersprungen (ID vorhanden), "
                  f"{expired} sofort abgelaufen (Original-Datum + TTL)"
                  + (f"; unbekannte Cluster: {', '.join(sorted(unknown_cl))}"
                     if unknown_cl else ""))
            self._json({"imported": len(imported), "skipped": skipped,
                        "expired": expired,
                        "unknown_clusters": sorted(unknown_cl),
                        "errors": errors[:20]}, 201)

        def _post_ad_group_members(self):
            # AD-Gruppen-Check: direkte Benutzer-Mitglieder einer Gruppe holen,
            # damit Admins prüfen können, ob der Gruppenabruf sauber läuft.
            if not self._require("admin"):
                return
            cn = str((self._body() or {}).get("cn") or "").strip()
            if not cn:
                self._json({"error": "Gruppenname (CN) erforderlich"}, 400)
                return
            if not args.ad_url:
                self._json({"error": "Keine AD-Anmeldung konfiguriert "
                            "(--ad-url fehlt)."}, 400)
                return
            r = ldap_group_members(args.ad_url, args.ad_bind_dn,
                                   args.ad_bind_password, args.ad_base_dn, cn,
                                   insecure=args.ad_insecure)
            audit(s["user"] if (s := self._session()) else None,
                  "AD-Gruppe geprüft",
                  f"{cn}: " + (f"{len(r['members'])} Mitglied(er)"
                               if r["ok"] else f"Fehler – {r['error']}"))
            self._json(r)

        def _post_teams_rename(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body() or {}
            old = str(body.get("old") or "").strip()
            new = str(body.get("new") or "").strip()[:60]
            if not old or not new:
                self._json({"error": "alter und neuer Name erforderlich"}, 400)
                return
            with teams_lock, roles_lock:
                if old not in approval_teams:
                    self._json({"error": "Team nicht gefunden"}, 404)
                    return
                if new != old and new in approval_teams:
                    self._json({"error": "Ein Team mit diesem Namen "
                                         "existiert bereits."}, 400)
                    return
                approval_teams[approval_teams.index(old)] = new
                save_teams()
                # Team-Mail-Adresse auf den neuen Namen umziehen
                with notify_lock:
                    te = notify_cfg.get("team_email") or {}
                    if old in te:
                        te[new] = te.pop(old)
                        save_notify()
                # Auto-Freigabe-Haken auf den neuen Namen umziehen
                with autoapprove_lock:
                    ta = autoapprove_cfg.get("teams") or {}
                    if old in ta:
                        ta[new] = ta.pop(old)
                        save_autoapprove()
                # Zugewiesene Reviewer auf den neuen Team-Namen umziehen
                moved = 0
                for entry in roles.values():
                    if entry.get("role") == "reviewer" and entry.get("abteilung") == old:
                        entry["abteilung"] = new
                        moved += 1
                if moved:
                    save_roles()
                # Auch aktive Sessions aktualisieren (kein Neu-Login nötig)
                for sess in sessions.values():
                    if sess.get("role") == "reviewer" and sess.get("abteilung") == old:
                        sess["abteilung"] = new
            audit(s["user"], "Team umbenannt",
                  f"„{old}“ → „{new}“" + (f" ({moved} Reviewer übernommen)" if moved else ""))
            self._json({"teams": list(approval_teams), "roles": roles_with_mail()})

        _POST_ROUTES = {
            "/api/storage-request": _post_storage_request,
            "/api/webhooks/test": _post_webhooks_test,
            "/api/login": _post_login,
            "/api/logout": _post_logout,
            "/api/refresh": _post_refresh,
            "/api/v1/reservations": _post_v1_reservations,
            "/api/reservations": _post_reservations,
            "/api/backup": _post_backup,
            "/api/tokens": _post_tokens,
            "/api/roles": _post_roles,
            "/api/import": _post_import,
            "/api/import/reservations": _post_import_reservations,
            "/api/ad/group-members": _post_ad_group_members,
            "/api/teams/rename": _post_teams_rename,
        }
        # Muster-Routen (Präfix + Endung, ID steckt im Pfad); exakt schlägt zuerst zu.
        _POST_PATTERNS = [
            (lambda p: p.startswith("/api/v1/reservations/")
                and p.endswith(("/approve", "/reject", "/cancel")),
             _post_v1_res_decision),
            (lambda p: p.startswith("/api/v1/storage-requests/")
                and p.endswith("/done"),
             _post_v1_storage_done),
            (lambda p: p.startswith("/api/reservations/")
                and p.endswith(("/approve", "/reject")),
             _post_res_decision),
            (lambda p: p.startswith("/api/reservations/")
                and p.endswith("/cancel"),
             _post_res_cancel),
        ]

        def do_PUT(self):
            fn = self._PUT_ROUTES.get(self.path)
            if fn:
                return fn(self)
            for prefix, pfn in self._PUT_PREFIXES:
                if self.path.startswith(prefix):
                    return pfn(self)
            self.send_error(404)

        def _put_reservations(self):
            if not self._require("admin"):
                return
            data = self._body()
            if not isinstance(data, list):
                self._json({"error": "Liste erwartet"}, 400)
                return
            cleaned = []
            for r in data:
                if not isinstance(r, dict):
                    continue
                r = dict(r)
                r.setdefault("id", new_res_id())
                r.setdefault("created", datetime.now().date().isoformat())
                r.setdefault("approved", False)
                cleaned.append(r)
            with res_lock:
                reservations[:] = prune_res(cleaned)
                save_res()
                self._json([public_res(r) for r in reservations])
            audit(self._session()["user"], "Reservierungen importiert",
                  f"{len(cleaned)} Einträge (ersetzt Bestand)")

        def _put_teams(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body()
            if not isinstance(body, list):
                self._json({"error": "Liste von Team-Namen erwartet"}, 400)
                return
            new = clean_teams(body)
            with teams_lock:
                approval_teams[:] = new   # in-place, damit Closures es sehen
                save_teams()
            # verwaiste Team-Mail-Adressen entfernen
            with notify_lock:
                te = notify_cfg.get("team_email") or {}
                orphan = [k for k in te if k not in new]
                for k in orphan:
                    te.pop(k, None)
                if orphan:
                    save_notify()
            # verwaiste Auto-Freigabe-Haken entfernen
            with autoapprove_lock:
                ta = autoapprove_cfg.get("teams") or {}
                gone = [k for k in ta if k not in new]
                for k in gone:
                    ta.pop(k, None)
                if gone:
                    save_autoapprove()
            audit(s["user"], "Genehmigungs-Teams geändert",
                  " → ".join(new) if new else "(keine – einstufig)")
            self._json({"teams": list(approval_teams)})

        def _put_selector(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body()
            if not isinstance(body, list):
                self._json({"error": "Liste von Tag-Kategorien erwartet"}, 400)
                return
            new = clean_selector(body)
            with selector_lock:
                cluster_selector[:] = new
                save_selector()
            audit(s["user"], "Cluster-Selektor geändert",
                  " → ".join(f"{e['label']} ({e['category']})" for e in new)
                  if new else "(keiner)")
            self._json({"selector": list(cluster_selector)})

        def _put_rolenames(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body()
            if not isinstance(body, dict):
                self._json({"error": "Objekt mit Rollen-Bezeichnungen erwartet"}, 400)
                return
            with rolenames_lock:
                for k in ROLE_KEYS:
                    v = body.get(k)
                    if isinstance(v, str) and v.strip():
                        role_names[k] = v.strip()[:60]
                    else:
                        role_names[k] = DEFAULT_ROLE_NAMES[k]
                save_rolenames()
                result = dict(role_names)
            audit(s["user"], "Rollen-Bezeichnungen geändert",
                  ", ".join(f"{k}={result[k]}" for k in ROLE_KEYS))
            self._json({"rolenames": result})

        def _put_webhooks(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body() or {}
            raw = body.get("endpoints") if isinstance(body, dict) else body
            cleaned = _clean_webhooks(raw)
            with webhooks_lock:
                # Leeres Secret = unverändert -> gespeichertes Secret behalten.
                old = {e["id"]: e.get("secret", "") for e in webhook_cfg}
                for e in cleaned:
                    if not e["secret"] and e["id"] in old:
                        e["secret"] = old[e["id"]]
                webhook_cfg[:] = cleaned
            save_webhooks()
            audit(s["user"], "Webhooks geändert",
                  (f"{len(cleaned)} Ziel(e): "
                   + ", ".join(e["url"] for e in cleaned)) if cleaned
                  else "keine Ziele")
            eps = [{"id": e["id"], "url": e["url"], "events": e["events"],
                    "active": e["active"], "desc": e["desc"],
                    "has_secret": bool(e["secret"])} for e in cleaned]
            self._json({"endpoints": eps})

        def _put_notify(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body()
            if not isinstance(body, dict):
                self._json({"error": "Objekt mit Mail-Regeln erwartet"}, 400)
                return
            # team_email nur für tatsächlich existierende Teams behalten
            clean = _merge_notify(body)
            with teams_lock:
                valid = set(approval_teams)
            clean["team_email"] = {k: v for k, v in clean["team_email"].items()
                                   if k in valid and v}
            with notify_lock:
                notify_cfg.clear()
                notify_cfg.update(clean)
                save_notify()
                result = json.loads(json.dumps(notify_cfg))
            on = [f"{role}:{ev}" for role, evs in NOTIFY_ROLE_EVENTS.items()
                  for ev in evs if (result["role"].get(role) or {}).get(ev)]
            audit(s["user"], "Mail-Regeln geändert",
                  (", ".join(on) or "keine")
                  + (f"; Team-Adressen: {len(result['team_email'])}"
                     if result["team_email"] else "")
                  + ("; Vorlage angepasst" if result.get("template_html") else ""))
            self._json({"notify": result})

        def _put_visibility(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body()
            if not isinstance(body, dict):
                self._json({"error": "Objekt mit Sichtbarkeits-Matrix erwartet"}, 400)
                return
            clean = clean_visibility(body)
            with visibility_lock:
                visibility_cfg.clear()
                visibility_cfg.update(clean)
                save_visibility()
                result = json.loads(json.dumps(visibility_cfg))
            hidden = [f"{role}:{feat}" for role, flags in result.items()
                      for feat, on in flags.items() if not on]
            audit(s["user"], "Sichtbarkeit geändert",
                  "ausgeblendet: " + (", ".join(sorted(hidden)) or "nichts"))
            self._json({"visibility": result})

        def _put_storagecfg(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body() or {}
            on = bool(body.get("enabled"))
            try:
                mn = max(0, int(float(body.get("min_lun_gb") or 0)))
            except (TypeError, ValueError):
                mn = 0
            try:
                mx = max(0, int(float(body.get("max_lun_gb") or 0)))
            except (TypeError, ValueError):
                mx = 0
            excl = _clean_excl(body.get("exclude_names"))
            vfac = _clean_vfac(body.get("vsan_factor"), args.vsan_factor)
            vovr = _clean_overrides(body.get("vsan_overrides"))
            with storage_lock:
                changed = (mn != storage_cfg.get("min_lun_gb", 0)
                           or excl != (storage_cfg.get("exclude_names") or [])
                           or vfac != storage_cfg.get("vsan_factor")
                           or vovr != (storage_cfg.get("vsan_overrides") or {}))
                storage_cfg["enabled"] = on
                storage_cfg["min_lun_gb"] = mn
                storage_cfg["max_lun_gb"] = mx
                storage_cfg["exclude_names"] = excl
                storage_cfg["vsan_factor"] = vfac
                storage_cfg["vsan_overrides"] = vovr
                store.save("storagecfg", storage_cfg)
            audit(s["user"], "Storage-Einstellungen",
                  ("Erweiterungen " + ("aktiv" if on else "inaktiv"))
                  + (f"; Mindest-LUN {mn} GB" if mn else "; keine Mindest-LUN")
                  + (f"; Max-LUN {mx} GB" if mx else "")
                  + (f"; Namensfilter: {', '.join(excl)}" if excl else "")
                  + f"; vSAN-Faktor {vfac}"
                  + (f" (Overrides: {len(vovr)})" if vovr else ""))
            # Filter/Faktor wirken in der Datensammlung -> gleich neu abrufen
            if changed and not state["refreshing"]:
                threading.Thread(target=do_refresh, daemon=True).start()
            self._json({"enabled": on, "min_lun_gb": mn, "max_lun_gb": mx,
                        "exclude_names": ", ".join(excl),
                        "vsan_factor": vfac, "vsan_overrides": vovr})

        def _put_netcfg(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body() or {}
            nx = _clean_excl(body.get("exclude_names"))
            vx = _clean_vlans(body.get("exclude_vlans"))
            with net_lock:
                changed = (nx != (net_cfg.get("exclude_names") or [])
                           or vx != (net_cfg.get("exclude_vlans") or []))
                net_cfg["exclude_names"] = nx
                net_cfg["exclude_vlans"] = vx
                store.save("netcfg", net_cfg)
            audit(s["user"], "Netzwerk-Filter geändert",
                  (f"Namensfilter: {', '.join(nx)}" if nx else "kein Namensfilter")
                  + (f"; VLAN-IDs: {', '.join(vx)}" if vx else "; keine VLAN-IDs"))
            # Filter wirkt in der Datensammlung -> gleich neu abrufen
            if changed and not state["refreshing"]:
                threading.Thread(target=do_refresh, daemon=True).start()
            self._json({"exclude_names": ", ".join(nx),
                        "exclude_vlans": ", ".join(vx)})

        def _put_refreshcfg(self):
            s = self._require("admin")
            if not s:
                return
            _rawbody = self._body() or {}
            clean = _clean_refreshcfg(_rawbody)
            # Nicht-leere Eingabe, die die Formatprüfung nicht überlebt
            # (z. B. Path-Traversal), klar ablehnen statt still zu verwerfen.
            if str(_rawbody.get("tz") or "").strip() and not clean.get("tz"):
                self._json({"error": "Ungültiger Zonenname. Bitte einen "
                            "IANA-Namen wie „Europe/Berlin“ verwenden "
                            "(oder leer für die Server-Zeitzone)."}, 400)
                return
            if clean.get("tz") and not tz_available(clean["tz"]):
                self._json({"error": "Unbekannte Zeitzone „%s“. Bitte einen "
                            "IANA-Namen wie „Europe/Berlin“ verwenden "
                            "(oder leer für die Server-Zeitzone)."
                            % clean["tz"]}, 400)
                return
            with refresh_lock:
                refresh_cfg.clear()
                refresh_cfg.update(clean)
                store.save("refreshcfg", refresh_cfg)
            apply_tz(clean.get("tz"))    # Anzeige-Zeitzone sofort übernehmen
            audit(s["user"], "Abruf-Intervalle geändert",
                  " · ".join(f"{t}: " + (f"{clean[t]} min" if clean[t]
                                         else "Standard")
                             for t in TIERS)
                  + " · Zeitzone: " + (clean.get("tz") or "Server-Standard"))
            self._json({"tiers": dict(clean),
                        "default_min": max(1, interval // 60),
                        "tz": clean.get("tz", ""),
                        "now": datetime.now().strftime("%d.%m.%Y %H:%M")})

        def _put_storage_request(self):
            # erledigt/offen umschalten (Admin im UI)
            s = self._require("admin")
            if not s:
                return
            sid = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
            done = bool((self._body() or {}).get("done"))
            with storage_lock:
                req = next((x for x in storage_reqs if x.get("id") == sid), None)
                if not req:
                    self._json({"error": "Anfrage nicht gefunden"}, 404)
                    return
                req["status"] = "erledigt" if done else "offen"
                req["done_by"] = (s["user"] or "") if done else ""
                req["done_on"] = datetime.now().date().isoformat() if done else ""
                save_storage_reqs()
                reqsnap = dict(req)
                result = json.loads(json.dumps(storage_reqs))
            audit(s["user"], "Storage-Erweiterung " +
                  ("erledigt" if done else "wieder offen"),
                  req.get("lun_name") or f"neue LUN ({req.get('cluster')})")
            if done:
                fire_webhooks("storage_done",
                              {"actor": s["user"] or "", "request": reqsnap})
            self._json({"requests": result})

        def _put_autoapprove(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body()
            if not isinstance(body, dict):
                self._json({"error": "Objekt mit Auto-Freigabe-Konfiguration erwartet"}, 400)
                return
            clean = clean_autoapprove(body)
            with teams_lock:
                valid = set(approval_teams)
            clean["teams"] = {k: True for k in clean["teams"] if k in valid}
            with autoapprove_lock:
                autoapprove_cfg.clear()
                autoapprove_cfg.update(clean)
                save_autoapprove()
                result = json.loads(json.dumps(autoapprove_cfg))
            audit(s["user"], "Auto-Freigabe geändert",
                  ("aktiv" if result["enabled"] else "inaktiv")
                  + f" · vCPU ≥ {result['min_cpu_pct']} %, RAM ≥ "
                  + f"{result['min_ram_pct']} %, LUN ≥ {result['min_lun_pct']} %, "
                  + f"Workload ≤ {result['max_workload_pct']} %"
                  + (" · Teams: " + ", ".join(sorted(result["teams"]))
                     if result["teams"] else " · keine Team-Haken"))
            self._json({"autoapprove": result})

        def _put_announce(self):
            s = self._require("admin")
            if not s:
                return
            body = self._body()
            if not isinstance(body, dict):
                self._json({"error": "Objekt mit Ankündigung erwartet"}, 400)
                return
            clean = clean_announce(body, actor=s["user"] or "Admin")
            with announce_lock:
                announce_cfg.clear()
                announce_cfg.update(clean)
                save_announce()
                result = json.loads(json.dumps(announce_cfg))
            audit(s["user"], "Ankündigung geändert",
                  ("aktiv" if result["active"] else "deaktiviert")
                  + (f": {result['title']}" if result["title"] else ""))
            self._json({"announce": result})

        def _put_mail_preview(self):
            if not self._require("admin"):
                return
            body = self._body() or {}
            th = str(body.get("template_html") or "")[:20000] or DEFAULT_MAIL_TEMPLATE
            ts = str(body.get("template_subject") or "")[:300] or DEFAULT_MAIL_SUBJECT
            sample = {"id": "KAPA-1a2b3c", "name": "SAP HANA Erweiterung",
                      "change": "OPS-12345", "cluster": "Cluster-03",
                      "source": "RZ-Nord", "vcpu": 32, "ram_gb": 256,
                      "storage_gb": 2000, "von": "anna.schmidt@firma.local",
                      "abteilung": "Team Netzwerk", "created": "2026-07-15",
                      "approvals": [{"team": "Team Netzwerk", "by": "jan.krause",
                                     "on": "2026-07-16"}],
                      "comment": "bitte zeitnah umsetzen"}
            self._json({
                "subject": render_template(ts, _mail_values(
                    sample, "genehmigt", "tom.weber", args.res_ttl_days,
                    "Team Security", html=False)),
                "html": render_template(th, _mail_values(
                    sample, "genehmigt", "tom.weber", args.res_ttl_days,
                    "Team Security", html=True))})

        def _put_token(self):
            # Schreibrechte eines Tokens per Klick setzen (Verwaltung)
            s = self._require("admin")
            if not s:
                return
            tid = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
            body = self._body() or {}
            with tokens_lock:
                t = tokens.get(tid)
                if not t:
                    self._json({"error": "Token nicht gefunden"}, 404)
                    return
                t["write_res"] = bool(body.get("write_res"))
                t["write_approve"] = bool(body.get("write_approve"))
                t["write_storage"] = bool(body.get("write_storage"))
                t["scope"] = ("read"
                              + ("+res" if t["write_res"] else "")
                              + ("+approve" if t["write_approve"] else "")
                              + ("+storage" if t["write_storage"] else ""))
                save_tokens()
                name = t.get("name", tid)
                self._json(token_list())
            rights = [lbl for flag, lbl in (("write_res", "Reservierungen"),
                                            ("write_approve", "Genehmigungen"),
                                            ("write_storage", "Storage"))
                      if body.get(flag)]
            audit(s["user"], "API-Token-Rechte geändert",
                  f"{name}: " + " + ".join(["lesen"] + rights))

        def _put_prefs(self):
            s = self._require()
            if not s:
                return
            clean = clean_prefs(self._body())
            with prefs_lock:
                all_prefs[s["user"] or ""] = clean
                store.save("prefs", all_prefs)
            self._json(clean)

        _PUT_ROUTES = {
            "/api/reservations": _put_reservations,
            "/api/teams": _put_teams,
            "/api/selector": _put_selector,
            "/api/rolenames": _put_rolenames,
            "/api/webhooks": _put_webhooks,
            "/api/notify": _put_notify,
            "/api/visibility": _put_visibility,
            "/api/storagecfg": _put_storagecfg,
            "/api/netcfg": _put_netcfg,
            "/api/refreshcfg": _put_refreshcfg,
            "/api/autoapprove": _put_autoapprove,
            "/api/announce": _put_announce,
            "/api/mail-preview": _put_mail_preview,
            "/api/prefs": _put_prefs,
        }
        _PUT_PREFIXES = [
            ("/api/storage-request/", _put_storage_request),
            ("/api/tokens/", _put_token),
        ]

        def do_DELETE(self):
            # Präfix-Dispatch (ID steckt im Pfad) statt elif-Kette.
            for prefix, fn in self._DELETE_PREFIXES:
                if self.path.startswith(prefix):
                    return fn(self)
            self.send_error(404)

        def _del_reservation(self):
            # Löschen von Reservierungsanfragen ist deaktiviert – Anträge
            # laufen über Genehmigung/Ablehnung und den automatischen Ablauf.
            self._json({"error": "Das Löschen von Reservierungen ist "
                                 "deaktiviert."}, 403)

        def _del_token(self):
            s = self._require("admin")
            if not s:
                return
            tid = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
            with tokens_lock:
                removed = tokens.pop(tid, None)
                save_tokens()
                self._json(token_list())
            if removed:
                audit(s["user"], "API-Token widerrufen", removed.get("name", tid))

        def _del_role(self):
            s = self._require("admin")
            if not s:
                return
            raw = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
            with roles_lock:
                # AD-Gruppen sind case-sensitiv gespeichert, Benutzer klein –
                # daher zuerst exakt, dann klein probieren (nicht blind lower()).
                key = raw if raw in roles else raw.lower()
                removed = roles.pop(key, None)
                if removed:
                    save_roles()
            self._json(roles_with_mail())
            if removed:
                audit(s["user"],
                      "AD-Gruppe entfernt" if removed.get("kind") == "group"
                      else "Rolle entfernt",
                      f"{key} (war {removed.get('role')})")

        def _del_import(self):
            # Offline-Quelle entfernen — die Cluster verschwinden mit dem
            # nächsten Abruf aus der Übersicht.
            s = self._require("admin")
            if not s:
                return
            src = urllib.parse.unquote(self.path.split("/api/import/", 1)[1])
            with import_lock:
                removed = import_sources.pop(src, None)
                if removed:
                    save_imports()
            if not removed:
                self._json({"error": "Quelle nicht gefunden"}, 404)
                return
            audit(s["user"], "Offline-Quelle gelöscht",
                  f"{src} ({len(removed.get('clusters') or [])} Cluster)")
            if not state["refreshing"]:
                threading.Thread(target=do_refresh, daemon=True).start()
            self._json({"ok": True})

        def _del_storage_request(self):
            # Storage-Anfrage ganz entfernen (Admin im UI) – für versehentlich
            # angelegte Anfragen. "erledigt"/"offen" bleibt davon unberührt.
            s = self._require("admin")
            if not s:
                return
            sid = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
            with storage_lock:
                removed = next((x for x in storage_reqs
                                if x.get("id") == sid), None)
                if not removed:
                    self._json({"error": "Anfrage nicht gefunden"}, 404)
                    return
                storage_reqs[:] = [x for x in storage_reqs
                                   if x.get("id") != sid]
                save_storage_reqs()
                result = json.loads(json.dumps(storage_reqs))
            audit(s["user"], "Storage-Erweiterung gelöscht",
                  (removed.get("lun_name")
                   or f"neue LUN ({removed.get('cluster')})")
                  + f" – {removed.get('status')}")
            self._json({"requests": result})

        # Präfix -> Handler (Reihenfolge = ursprüngliche elif-Reihenfolge).
        _DELETE_PREFIXES = [
            ("/api/reservations/", _del_reservation),
            ("/api/tokens/", _del_token),
            ("/api/roles/", _del_role),
            ("/api/import/", _del_import),
            ("/api/storage-request/", _del_storage_request),
        ]

        def log_message(self, *a):
            pass

    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=maintenance, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    if args.backup_target:
        threading.Thread(target=backup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Dashboard läuft: http://localhost:{args.port}  (Strg+C zum Beenden)"
          + (f" · Auto-Refresh alle {interval // 60} min" if interval else "")
          + (f" · AD-Anmeldung: {args.ad_url}" if auth_enabled else " · ohne Anmeldung"),
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.", file=sys.stderr)

# ------------------------------------------------------------------- main ----

def apply_config_file(ap, path):
    """INI-Datei (Sektion [kapa]) als neue Defaults in den Parser übernehmen.
    Schlüssel entsprechen den Optionsnamen (Bindestrich oder Unterstrich)."""
    import configparser
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
    except (OSError, configparser.Error) as e:
        ap.error(f"Konfigurationsdatei {path}: {e}")
    sec = cp["kapa"] if cp.has_section("kapa") else cp[cp.default_section]
    by_dest = {a.dest: a for a in ap._actions}
    defaults = {}
    for key, raw in sec.items():
        dest = key.strip().lower().replace("-", "_")
        action = by_dest.get(dest)
        if action is None or dest in ("help", "version", "config"):
            ap.error(f"Unbekannte Option '{key}' in {path}")
        raw = raw.strip()
        if isinstance(action, argparse._StoreTrueAction):
            defaults[dest] = raw.lower() in ("1", "true", "yes", "ja", "on")
        elif callable(action.type):
            # getippte Optionen (int, float, int_or) auch aus der INI korrekt
            # konvertieren – sonst käme z. B. port als String an.
            try:
                defaults[dest] = action.type(raw)
            except (ValueError, argparse.ArgumentTypeError) as e:
                ap.error(f"Option '{key}' in {path}: ungültiger Wert '{raw}' ({e})")
        else:
            defaults[dest] = raw
    ap.set_defaults(**defaults)


_SOURCE_KEYS = {"url", "user", "auth-source", "auth_source", "insecure",
                "aria-proxy", "aria_proxy", "password", "password-file",
                "password_file"}


def parse_sources(path, ap=None):
    """[quelle:Name]-Sektionen der INI als benannte vROps-Datenquellen einlesen.
    Schlüssel je Quelle: url, user, auth-source, insecure, aria-proxy, password,
    password-file. Der Sektionsname nach 'quelle:' ist der Anzeigename.

    Fremde Schlüssel in einer [quelle:*]-Sektion sind fast immer ein
    verrutschter [kapa]-Eintrag (in INI-Dateien gehört ALLES nach einem
    Sektions-Header zu dieser Sektion!) – deshalb harter Fehler mit Hinweis,
    statt z. B. einen 'port' stillschweigend zu verschlucken."""
    import configparser
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
    except (OSError, configparser.Error):
        return []
    out = []
    for section in cp.sections():
        low = section.strip().lower()
        if not (low.startswith("quelle:") or low.startswith("source:")):
            continue
        name = section.split(":", 1)[1].strip() or "vROps"
        s = cp[section]
        alien = [k for k in s if k.strip().lower() not in _SOURCE_KEYS]
        if alien:
            msg = (f"Option(en) {', '.join(sorted(alien))} stehen in der Sektion "
                   f"[{section}], gehören aber vermutlich nach [kapa]. In "
                   "INI-Dateien gehört alles nach einem [Sektions]-Header zu "
                   "dieser Sektion – bitte die [kapa]-Schlüssel VOR die "
                   "[quelle:*]-Sektionen verschieben (oder die [quelle:*]-"
                   "Sektionen ans Dateiende).")
            if ap is not None:
                ap.error(msg)
            print(f"WARNUNG: {msg}", file=sys.stderr)
        pw = (s.get("password") or "").strip()
        pf = (s.get("password-file") or s.get("password_file") or "").strip()
        if not pw and pf:
            try:
                with open(pf, encoding="utf-8") as f:
                    pw = f.read().strip()
            except OSError as e:
                print(f"Quelle '{name}': Passwort-Datei {pf} unlesbar: {e}",
                      file=sys.stderr)
        if not pw:
            pw = os.environ.get("ARIA_PASSWORD", "")
        out.append({
            "name": name,
            "url": (s.get("url") or "").strip(),
            "user": (s.get("user") or "").strip(),
            "auth_source": (s.get("auth-source") or s.get("auth_source") or "local").strip(),
            "insecure": (s.get("insecure") or "").strip().lower()
            in ("1", "true", "yes", "ja", "on"),
            "aria_proxy": (s.get("aria-proxy") or s.get("aria_proxy") or "").strip(),
            "password": pw,
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Aria Ops Kapazitätsauswertung pro Cluster")
    ap.add_argument("--version", action="version", version=f"aria_kapa {VERSION}")
    ap.add_argument("--url", help="Basis-URL, z.B. https://aria-ops.firma.de")
    ap.add_argument("--source-name", default="",
                    help="Anzeigename der (einen) vROps-Quelle aus --url – wie "
                         "der Sektionsname bei [quelle:NAME]; erscheint als "
                         "Quellen-Badge am Cluster und im vROps-Quickfilter")
    ap.add_argument("--user", help="Benutzername")
    ap.add_argument("--password",
                    help="Passwort (alternativ --password-file oder Umgebungsvariable "
                         "ARIA_PASSWORD, sonst interaktive Abfrage)")
    ap.add_argument("--password-file",
                    help="Datei mit dem Aria-Passwort (z. B. systemd LoadCredential)")
    ap.add_argument("--smtp-password-file", help="Datei mit dem SMTP-Passwort")
    ap.add_argument("--backup-password-file", help="Datei mit dem Backup-SSH-Passwort")
    ap.add_argument("--auth-source", default="local", help="Auth-Quelle (Standard: local)")
    ap.add_argument("--cpu-factor", type=int_or(6), default=6, help="CPU-Überprovisionierungsfaktor (Standard: 6)")
    ap.add_argument("--tanzu-mhz-per-vcpu", type=int_or(2500), default=2500,
                    help="Umrechnung der Tanzu-Namespace-CPU-Reservierung (MHz) "
                         "in vCPU-Äquivalente (Standard: 2500 MHz je vCPU; "
                         "0 = CPU-Reservierungen der Namespaces nicht zählen)")
    ap.add_argument("--failover-hosts", type=int_or(1), default=1,
                    help="Ausfall-Hosts pro Cluster (N+1): die größten N Hosts werden "
                         "von der Kapazität abgezogen (Standard: 1, 0 = aus)")
    ap.add_argument("--exclude-tag", default="",
                    help="VMs mit diesem vROps-Tag aus der Auswertung ausschließen, "
                         "Format Kategorie:Wert, z. B. Kapa_Filter:Ja (leer = aus)")
    ap.add_argument("--vsan-factor", type=float, default=0.5,
                    help="Anteil der vSAN-Bruttokapazität, der als nutzbar zählt "
                         "(Standard: 0.5 für RAID-1/Spiegelung; 1 = brutto). "
                         "Wirkt auf Kapazität UND Belegung des vSAN-Datastores.")
    ap.add_argument("--tag-property", default="",
                    help="Eigenschafts-Präfix für vSphere-Tags des Clusters, z. B. "
                         "'summary|tag'. Ohne Angabe werden alle Eigenschaften "
                         "genommen, deren Schlüssel 'tag' enthält (das Log nennt "
                         "die erkannten Schlüssel)")
    ap.add_argument("--show-uplink-portgroups", action="store_true",
                    help="dvSwitch-Uplink-/Trunk-Portgruppen NICHT ausblenden. "
                         "Standard: ausblenden (Portgruppen mit 'uplink' im Namen "
                         "oder breiter VLAN-Trunk-Range wie 0-4094 sind keine "
                         "echten Netz-VLANs)")
    ap.add_argument("--contact-info", default="",
                    help="Kontakt-/Impressumszeile (Abteilung/Firma + Mailadresse "
                         "für Rückfragen), wird im Footer und auf der Login-Maske "
                         "angezeigt")
    ap.add_argument("--insecure", action="store_true", help="TLS-Zertifikat nicht prüfen (Self-Signed)")
    ap.add_argument("--aria-proxy", default="",
                    help="Optionaler HTTP(S)-Proxy für die Aria-Anfragen, z. B. "
                         "http://proxy.firma.local:3128 (für abgesicherte Umgebungen; "
                         "ohne Angabe direkte Verbindung)")
    ap.add_argument("--output", default="kapa_dashboard.html", help="Ausgabedatei")
    ap.add_argument("--json", help="Rohdaten zusätzlich als JSON speichern")
    ap.add_argument("--sample", action="store_true", help="Demo mit Beispieldaten")
    ap.add_argument("--serve", action="store_true",
                    help="Als lokaler Webserver laufen (Live-Abruf per Knopf, Disk-Cache)")
    ap.add_argument("--port", type=int_or(8080), default=8080, help="Port für --serve (Standard: 8080)")
    ap.add_argument("--bind", default="0.0.0.0",  # nosec B104 – im Container gewollt; hinter nginx/Proxy lokal binden (127.0.0.1)
                    help="Bind-Adresse für --serve")
    ap.add_argument("--data-dir", default="data",
                    help="Basisordner für alle Laufzeitdaten (Cache, Reservierungen, "
                         "Rollen, Teams, Log, Tokens). Standard: data/ neben dem "
                         "Skript. WICHTIG bei CI/CD: auf einen Pfad AUSSERHALB des "
                         "Deploy-Verzeichnisses legen (z. B. /var/lib/kapa), sonst "
                         "löscht die Pipeline die Daten bei jedem Deploy. Gilt für "
                         "alle *-file-Optionen ohne eigene Pfadangabe.")
    ap.add_argument("--cache", default="kapa_cache.json",
                    help="Cache-Datei der letzten Abfrage (Standard: <data-dir>/kapa_cache.json)")
    ap.add_argument("--refresh-interval", type=int_or(1800), default=1800,
                    help="Automatische Aktualisierung im Serve-Modus in Sekunden "
                         "(0 = aus, Standard: 1800 = 30 min)")
    ap.add_argument("--res-file", default="kapa_reservierungen.json",
                    help="Reservierungsdatei im Serve-Modus "
                         "(Standard: data/kapa_reservierungen.json)")
    ap.add_argument("--res-ttl-days", type=int_or(31), default=31,
                    help="Reservierungen nach N Tagen ab Anlage automatisch löschen "
                         "(0 = nie, Standard: 31)")
    ap.add_argument("--id-prefix", default="",
                    help="Präfix für die Kapa-ID neuer Reservierungen, z. B. 'KAPA-' "
                         "(Standard: leer). Erlaubt: Buchstaben/Ziffern/-/_")
    ap.add_argument("--id-length", type=int_or(12), default=12,
                    help="Anzahl Zeichen des Zufallsteils der Kapa-ID nach dem Präfix "
                         "(Standard: 12)")
    ap.add_argument("--approval-teams", default="",
                    help="Erstbefüllung der Genehmigungs-Teams (komma-getrennt, in "
                         "Prüfreihenfolge), z. B. 'Team Netzwerk,Team Security,Team "
                         "Betrieb'. Wird nur übernommen, wenn die Team-Datei noch "
                         "nicht existiert; danach erfolgt die Pflege auf der "
                         "Verwaltungsseite. Leer = einstufig (Admin genehmigt direkt).")
    ap.add_argument("--ad-url", default="",
                    help="Active Directory für die Anmeldung, z. B. "
                         "ldaps://dc01.firma.local (ohne Angabe: kein Login nötig)")
    ap.add_argument("--ad-domain", default="",
                    help="AD-Domäne für Benutzernamen ohne @, z. B. firma.local")
    ap.add_argument("--login-hint", default="Active-Directory-Benutzername",
                    help="Platzhalter im Benutzername-Feld der Anmeldemaske "
                         "(z. B. 'vorname.nachname' oder 'MUSTER\\\\benutzer')")
    ap.add_argument("--ad-insecure", action="store_true",
                    help="LDAPS-Zertifikat nicht prüfen (Self-Signed)")
    ap.add_argument("--ad-bind-dn", default="",
                    help="Service-Konto (DN oder UPN) für die AD-Gruppensuche; "
                         "ohne Angabe werden nur direkt zugewiesene Benutzer "
                         "berechtigt (keine AD-Gruppen)")
    ap.add_argument("--ad-bind-password", default="",
                    help="Passwort des Service-Kontos (alternativ "
                         "--ad-bind-password-file oder AD_BIND_PASSWORD)")
    ap.add_argument("--ad-bind-password-file", default="",
                    help="Datei mit dem Service-Konto-Passwort")
    ap.add_argument("--ad-base-dn", default="",
                    help="Basis-DN für die Gruppensuche, z. B. DC=firma,DC=local "
                         "(ohne Angabe aus --ad-domain abgeleitet)")
    ap.add_argument("--ad-mail-attribute", default="",
                    help="AD-Attribut, aus dem die Empfänger-Mailadresse des "
                         "Anforderers gelesen wird (z. B. 'mail'). Ohne Angabe wird "
                         "der Anmeldename (UPN) als Adresse verwendet. Erfordert ein "
                         "Service-Konto (--ad-bind-dn); wird bei der Anmeldung "
                         "aufgelöst und mit der Reservierung gespeichert")
    ap.add_argument("--cookie-insecure", action="store_true",
                    help="Session-Cookie ohne Secure-Flag setzen (nur für lokalen "
                         "HTTP-Test ohne HTTPS-Proxy; im Betrieb NICHT verwenden)")
    ap.add_argument("--admin-user", default="",
                    help="Immer-Admin(s), kommagetrennt, z. B. admin@firma.local "
                         "(Bootstrap für die Rollenverwaltung)")
    ap.add_argument("--roles-file", default="kapa_rollen.json",
                    help="Rollendatei (Standard: data/kapa_rollen.json)")
    ap.add_argument("--smtp-server", default="",
                    help="Mailserver für Reports, z. B. mail.firma.local:25 "
                         "(ohne Angabe: keine Mails)")
    ap.add_argument("--smtp-from", default="",
                    help="Absenderadresse (Standard: kapa-dashboard@localhost)")
    ap.add_argument("--smtp-to", default="",
                    help="Report-Empfänger, kommagetrennt; der Anforderer erhält "
                         "die Mail zusätzlich automatisch")
    ap.add_argument("--smtp-user", default="", help="SMTP-Anmeldung (optional)")
    ap.add_argument("--smtp-password", default="", help="SMTP-Passwort (optional)")
    ap.add_argument("--smtp-tls", action="store_true", help="STARTTLS verwenden")
    ap.add_argument("--config",
                    help="INI-Datei mit allen Optionen (Sektion [kapa], Schlüssel wie "
                         "die Optionsnamen); Kommandozeile überschreibt die Datei")
    ap.add_argument("--backup-target", default="",
                    help="SFTP/SCP-Backupziel, z. B. backup@srv:/backup/kapa "
                         "(ohne Angabe: kein Backup)")
    ap.add_argument("--backup-port", type=int_or(22), default=22, help="SSH-Port (Standard: 22)")
    ap.add_argument("--backup-key", default="",
                    help="SSH-Private-Key für das Backup (empfohlen). Muss vom "
                         "Dienst-Benutzer lesbar sein und AUSSERHALB des Home-"
                         "Verzeichnisses liegen (z. B. /etc/kapa/), weil die "
                         "systemd-Härtung ProtectHome=true Home-Verzeichnisse "
                         "ausblendet.")
    ap.add_argument("--backup-known-hosts", default="",
                    help="known_hosts-Datei für das Backup (Standard: "
                         "known_hosts im Datenordner – muss beschreibbar sein)")
    ap.add_argument("--backup-password", default="",
                    help="SSH-Passwort (alternativ BACKUP_PASSWORD; erfordert sshpass)")
    ap.add_argument("--backup-interval", type=int_or(43200), default=43200,
                    help="Backup-Intervall in Sekunden (Standard: 43200 = zweimal "
                         "täglich, 0 = nur einmal beim Start)")
    ap.add_argument("--backup-keep-days", type=int_or(30), default=30,
                    help="Backups auf dem Ziel nach N Tagen löschen "
                         "(Standard: 30, 0 = nie aufräumen)")
    ap.add_argument("--log-file", default="kapa_log.jsonl",
                    help="Audit-Log-Datei (Standard: data/kapa_log.jsonl)")
    ap.add_argument("--tokens-file", default="kapa_tokens.json",
                    help="API-Token-Datei (Standard: data/kapa_tokens.json)")
    ap.add_argument("--teams-file", default="kapa_teams.json",
                    help="Datei mit den Genehmigungs-Teams (Standard: "
                         "data/kapa_teams.json); Pflege über die Verwaltungsseite")
    ap.add_argument("--storage", default="json", choices=("json", "sqlite"),
                    help="Datenspeicherung: 'json' (Standard, je Sammlung eine "
                         "editierbare Datei) oder 'sqlite' (eine data/kapa.db, "
                         "Reservierungen mit inkrementellen Schreibzugriffen). "
                         "Beim Wechsel auf sqlite werden vorhandene JSON-Daten "
                         "einmalig automatisch übernommen.")
    ap.add_argument("--db-file", default="kapa.db",
                    help="SQLite-Datei bei --storage sqlite (Standard: data/kapa.db)")
    ap.add_argument("--selector-file", default="kapa_selektor.json",
                    help="Datei mit den Tag-Kategorien des Cluster-Selektors "
                         "(Standard: data/kapa_selektor.json); Pflege über die "
                         "Verwaltungsseite")
    ap.add_argument("--rolenames-file", default="kapa_rollennamen.json",
                    help="Datei mit den frei wählbaren Rollen-Bezeichnungen "
                         "(Standard: data/kapa_rollennamen.json); Pflege über die "
                         "Verwaltungsseite")
    ap.add_argument("--notify-file", default="kapa_mail.json",
                    help="Datei mit den Mail-Benachrichtigungsregeln je Rolle "
                         "(Standard: data/kapa_mail.json); Pflege über die "
                         "Verwaltungsseite")
    ap.add_argument("--webhooks-file", default="kapa_webhooks.json",
                    help="Datei mit den ausgehenden Webhook-Zielen (Standard: "
                         "data/kapa_webhooks.json); Pflege über die "
                         "Verwaltungsseite. Enthält Secrets → NICHT im Backup.")
    ap.add_argument("--prefs-file", default="kapa_prefs.json",
                    help="Datei mit den persönlichen UI-Einstellungen je Benutzer "
                         "(z. B. ein-/ausgeblendete Tabellenspalten; "
                         "Standard: data/kapa_prefs.json)")
    ap.add_argument("--storagecfg-file", default="kapa_storagecfg.json",
                    help="Datei: Schalter für Storage-Erweiterungen")
    ap.add_argument("--storagereq-file", default="kapa_storage_anfragen.json",
                    help="Datei mit den Storage-Erweiterungs-Anfragen "
                         "(fürs Storage-Team, per API abrufbar)")
    ap.add_argument("--netcfg-file", default="kapa_netcfg.json",
                    help="Datei: Netzwerk-Filter (Portgruppen nach Name/"
                         "VLAN-ID ausblenden)")
    ap.add_argument("--import-file", default="kapa_import.json",
                    help="Datei: manuell importierte Offline-Quellen "
                         "(Cluster ohne vROps-Anbindung, per PowerCLI-JSON)")
    ap.add_argument("--refreshcfg-file", default="kapa_abrufintervalle.json",
                    help="Datei: gestaffelte Abruf-Intervalle je Teilbereich")
    ap.add_argument("--history-file", default="kapa_history.json",
                    help="Datei: Tages-Snapshots für die Statistik (Trends)")
    ap.add_argument("--history-days", type=int, default=730,
                    help="Aufbewahrung der Statistik-Historie in Tagen "
                         "(Standard 730 = 2 Jahre; 0 = unbegrenzt)")
    ap.add_argument("--visibility-file", default="kapa_sichtbarkeit.json",
                    help="Datei mit der Sichtbarkeits-Matrix je Rolle "
                         "(Pflege über die Verwaltung)")
    ap.add_argument("--sessions-file", default="kapa_sessions.json",
                    help="Datei mit den aktiven Anmelde-Sitzungen (nur Hashes; "
                         "Sitzungen überleben so einen Dienst-Neustart)")
    ap.add_argument("--autoapprove-file", default="kapa_autofreigabe.json",
                    help="Datei mit der Auto-Freigabe-Konfiguration (Schwellen "
                         "je Cluster-Kapazität; Pflege über die Verwaltung)")
    ap.add_argument("--announce-file", default="kapa_ankuendigung.json",
                    help="Datei mit der Ankündigung (Popup nach der Anmeldung; "
                         "Standard: data/kapa_ankuendigung.json); Pflege über "
                         "die Verwaltungsseite")
    # Leere Werte von Langoptionen verwerfen, BEVOR geparst wird. Grund: Die
    # systemd-Unit baut Argumente wie "--backup-target ${BACKUP_TARGET}" aus
    # kapa.env; ist die Variable leer, käme ein leeres Argument an und würde den
    # INI-Wert überschreiben (leer schlägt INI). Ein leerer Wert bedeutet hier
    # überall "nicht gesetzt", also lassen wir ihn weg – dann greift die INI bzw.
    # der eingebaute Standard. (String-Pendant zu int_or() für Zahlen.)
    argv = _drop_empty_long_opts(sys.argv[1:])
    # Erst --config einlesen, dann endgültig parsen (CLI schlägt INI)
    pre, _ = ap.parse_known_args(argv)
    if pre.config:
        apply_config_file(ap, pre.config)
    args = ap.parse_args(argv)

    # JSON-Datendateien ohne Pfadangabe unter --data-dir ablegen (Standard data/)
    base = args.data_dir or "data"
    args.cache = data_path(args.cache, base)
    args.res_file = data_path(args.res_file, base)
    args.roles_file = data_path(args.roles_file, base)
    args.log_file = data_path(args.log_file, base)
    args.tokens_file = data_path(args.tokens_file, base)
    args.teams_file = data_path(args.teams_file, base)
    args.selector_file = data_path(args.selector_file, base)
    args.db_file = data_path(args.db_file, base)
    args.rolenames_file = data_path(args.rolenames_file, base)
    args.notify_file = data_path(args.notify_file, base)
    args.webhooks_file = data_path(args.webhooks_file, base)
    args.prefs_file = data_path(args.prefs_file, base)
    args.announce_file = data_path(args.announce_file, base)
    args.autoapprove_file = data_path(args.autoapprove_file, base)
    args.sessions_file = data_path(args.sessions_file, base)
    args.visibility_file = data_path(args.visibility_file, base)
    args.storagecfg_file = data_path(args.storagecfg_file, base)
    args.storagereq_file = data_path(args.storagereq_file, base)
    args.netcfg_file = data_path(args.netcfg_file, base)
    args.import_file = data_path(args.import_file, base)
    args.history_file = data_path(args.history_file, base)
    args.refreshcfg_file = data_path(args.refreshcfg_file, base)
    # Kapa-ID: Präfix säubern (IDs stehen in URLs) und Länge begrenzen
    args.id_prefix = re.sub(r"[^A-Za-z0-9_-]", "", args.id_prefix or "")[:20]
    args.id_length = min(40, max(4, args.id_length))
    if args.json:
        args.json = data_path(args.json, base)

    # Zugangsdaten: Parameter > Passwort-Datei > Umgebungsvariable
    def secret(value, path, env_key):
        if value:
            return value
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError as e:
                ap.error(f"Passwort-Datei {path}: {e}")
        return os.environ.get(env_key, "")

    args.password = secret(args.password, args.password_file, "ARIA_PASSWORD") or None
    args.smtp_password = secret(args.smtp_password, args.smtp_password_file,
                                "SMTP_PASSWORD")
    args.backup_password = secret(args.backup_password, args.backup_password_file,
                                  "BACKUP_PASSWORD")
    args.ad_bind_password = secret(args.ad_bind_password, args.ad_bind_password_file,
                                   "AD_BIND_PASSWORD")
    # Basis-DN aus der Domäne ableiten, falls nicht gesetzt (firma.local -> DC=firma,DC=local)
    if not args.ad_base_dn and args.ad_domain:
        args.ad_base_dn = ",".join("DC=" + p for p in args.ad_domain.split("."))

    # Datenquellen: mehrere benannte vROps aus [quelle:*]-Sektionen ODER – zur
    # Rückwärtskompatibilität – die eine Quelle aus --url/--user. Über
    # --source-name (INI: source-name) bekommt auch sie einen Anzeigenamen
    # (Quellen-Badge + vROps-Quickfilter), wie die [quelle:*]-Quellen.
    args.sources = parse_sources(pre.config, ap) if pre.config else []
    if not args.sources and args.url:
        args.sources = [{"name": (args.source_name or "").strip(),
                         "url": args.url, "user": args.user,
                         "auth_source": args.auth_source, "insecure": args.insecure,
                         "aria_proxy": args.aria_proxy, "password": args.password}]

    if args.serve:
        if not args.sample:
            valid = [s for s in args.sources if s["url"] and s["user"]]
            if not valid:
                ap.error("Keine Datenquelle konfiguriert: --url/--user angeben, "
                         "[quelle:*]-Sektionen in der INI pflegen oder --sample nutzen")
            for s in valid:
                if not s["password"]:
                    if len(valid) == 1:
                        s["password"] = getpass.getpass("Passwort: ")
                    else:
                        ap.error(f"Quelle '{s['name'] or 'vROps'}': kein Passwort "
                                 "(password-file in der [quelle:*]-Sektion setzen)")
            args.sources = valid
        serve(args, args.sources[0]["password"] if args.sources else None)
        return

    if args.sample:
        clusters = build_summary(sample_data(args.vsan_factor), args.cpu_factor,
                                 args.failover_hosts, args.tanzu_mhz_per_vcpu)
    else:
        valid = [s for s in args.sources if s["url"] and s["user"]]
        if not valid:
            ap.error("--url und --user sind erforderlich (oder --sample für Demo)")
        clusters = []
        for s in valid:
            pw = s["password"] or getpass.getpass(f"Passwort {s['name'] or ''}: ")
            api = AriaOps(s["url"], s["user"], pw, s["auth_source"],
                          verify_tls=not s["insecure"], proxy=s["aria_proxy"])
            cl = collect(api, args.cpu_factor, failover_hosts=args.failover_hosts,
                         exclude_tag=args.exclude_tag,
                         tag_property=args.tag_property,
                         vsan_factor=args.vsan_factor,
                         tanzu_mhz=args.tanzu_mhz_per_vcpu)
            for c in cl:
                if s["name"]:
                    c["source"] = s["name"]
            clusters += cl
        strip_uplinks(clusters, not args.show_uplink_portgroups)

    if args.json:
        ensure_dir(args.json)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(clusters, f, ensure_ascii=False, indent=2)

    render_dashboard(clusters, args.cpu_factor, args.output,
                     args.res_ttl_days, args.failover_hosts,
                     contact=args.contact_info,
                     tanzu_mhz=args.tanzu_mhz_per_vcpu)

    print(f"\n{'Cluster':<22}{'Hosts':>6}{'Cores':>7}{'vCPU-Kap':>10}{'vCPU-belegt':>12}"
          f"{'vCPU-frei':>10}{'RAM-Kap GB':>12}{'RAM-belegt':>12}{'RAM-frei':>10}")
    for c in clusters:
        print(f"{c['name']:<22}{c['hostCount']:>6}{c['cores']:>7}{c['vcpuCap']:>10}"
              f"{c['vcpuUsed']:>12}{c['vcpuFree']:>10}{c['ramCap']:>12}"
              f"{c['ramUsed']:>12}{c['ramFree']:>10}")
    print(f"\nDashboard geschrieben: {args.output}")


if __name__ == "__main__":
    main()
