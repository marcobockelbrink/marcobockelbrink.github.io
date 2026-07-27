#!/usr/bin/env python3
"""Auslieferung: OpenAPI-Spec, API-Doku-/Login-/Dashboard-Templates
(aus frontend/), Reviewer-Handbuch und das HTML-Rendering
(render_html/render_dashboard). Aus aria_kapa.py ausgelagert (v3.0)."""
import json
from datetime import datetime

from kapa.constants import (VERSION, DEFAULT_ROLE_NAMES, DEFAULT_NOTIFY,
                            VIS_FEATURES)
from kapa.util import _fe, _html_escape


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
                                     "description": T("Sekunden bis zur nächsten Aktualisierung", "seconds until the next refresh")},
                            "progress": {"type": "string",
                                         "description": T("Fortschrittstext während eines Abrufs", "progress text while a refresh runs")},
                            "error": {"type": "string", "nullable": True},
                            "tiers": {"type": "object",
                                      "description": T("Stand je Teilbereich (vms/network/storage)", "per-area freshness (vms/network/storage)")}}},
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
