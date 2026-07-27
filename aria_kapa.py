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

from kapa.constants import (VERSION, ROLE_KEYS, DEFAULT_ROLE_NAMES,
                            NOTIFY_EVENTS, NOTIFY_ROLE_EVENTS,
                            WEBHOOK_EVENTS, VIS_FEATURES,
                            DEFAULT_VISIBILITY, DEFAULT_NOTIFY)

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

from kapa.util import _fe, _FRONTEND_DIR  # noqa: F401 (Re-Export)
from kapa.api import make_handler
from types import SimpleNamespace
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


# --------------------- Auslieferung/Doku: kapa/dashboard.py --
from kapa.dashboard import (POWERCLI_PS1, openapi_spec, API_DOCS_HTML,
                            LOGIN_TEMPLATE, reviewer_doc_html, HTML_TEMPLATE,
                            json_for_html, render_html, render_dashboard)



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

    # Anzeigename der Quellen-Auswahl im Selektor (leer = Standard „vROps") —
    # als 1-Element-Liste, damit Handler/Threads dieselbe Referenz teilen.
    selector_src_label = [""]

    def load_selector():
        d = store.load("selector", None)
        if isinstance(d, list):                # Altformat: nur die Stufen
            return clean_selector(d)
        if isinstance(d, dict):                # neues Format (seit v3.4)
            selector_src_label[0] = " ".join(
                str(d.get("source_label") or "").split())[:40]
            return clean_selector(d.get("entries") or [])
        return []

    def save_selector():
        store.save("selector", {"source_label": selector_src_label[0],
                                "entries": cluster_selector})

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
        # Sprechende Bereichs-Namen fürs Log — die gestaffelten Abrufe laufen
        # zu unterschiedlichen Zeiten, im Log soll stehen, WAS gelesen wurde.
        _TL = {"vms": "Kapazität (VMs/CPU/RAM/Tanzu)", "network": "Netzwerk",
               "storage": "Storage"}
        tier_note = ("alle Bereiche" if not skip_tiers else
                     "nur " + ", ".join(_TL.get(t, t) for t in sorted(run)))
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
            detail = (f"{tier_note} · {len(clusters)} Cluster, "
                      f"{nvm} VMs{src_note} in {dur:.1f} s")
            print(f"Aria-Abruf beendet: {detail}", file=sys.stderr)
            audit(None, "Aria-Abruf beendet", detail)
        except Exception as e:
            dur = time.time() - t0
            state["error"] = str(e)
            print(f"Aria-Abruf FEHLGESCHLAGEN nach {dur:.1f} s: {e}", file=sys.stderr)
            audit(None, "Aria-Abruf fehlgeschlagen",
                  f"{tier_note} · nach {dur:.1f} s: {e}")
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

    # ---- AppState: die frühere serve()-Closure als explizites Objekt ----
    app = SimpleNamespace()
    app.TIERS = TIERS; app.VALID_ROLES = VALID_ROLES; app.VIS_ROLES = VIS_ROLES
    app._clean_excl = _clean_excl; app._clean_overrides = _clean_overrides; app._clean_refreshcfg = _clean_refreshcfg
    app._clean_vfac = _clean_vfac; app._clean_vlans = _clean_vlans; app._clean_webhooks = _clean_webhooks
    app._merge_notify = _merge_notify; app._sess_key = _sess_key; app._src_ok = _src_ok
    app._wrow = _wrow; app.all_prefs = all_prefs; app.announce_cfg = announce_cfg
    app.announce_lock = announce_lock; app.apply_tz = apply_tz; app.approval_teams = approval_teams
    app.args = args; app.audit = audit; app.auth_enabled = auth_enabled
    app.autoapprove_cfg = autoapprove_cfg; app.autoapprove_lock = autoapprove_lock; app.clean_announce = clean_announce
    app.clean_autoapprove = clean_autoapprove; app.clean_import_clusters = clean_import_clusters; app.clean_prefs = clean_prefs
    app.clean_selector = clean_selector; app.clean_sources = clean_sources; app.clean_storage_req = clean_storage_req
    app.clean_teams = clean_teams; app.clean_visibility = clean_visibility; app.cluster_selector = cluster_selector
    app.selector_src_label = selector_src_label
    app.clusters_for = clusters_for; app.current_team = current_team; app.data_csv = data_csv
    app.do_refresh = do_refresh; app.effective_sources = effective_sources; app.emit_auto_events = emit_auto_events
    app.fire_webhooks = fire_webhooks; app.group_role = group_role; app.has_group_entries = has_group_entries
    app.history = history; app.history_lock = history_lock; app.import_lock = import_lock
    app.import_sources = import_sources; app.interval = interval; app.known_sources = known_sources
    app.login_blocked = login_blocked; app.login_note_fail = login_note_fail; app.login_reset = login_reset
    app.mail_event = mail_event; app.net_cfg = net_cfg; app.net_lock = net_lock
    app.new_res_id = new_res_id; app.normalize_user = normalize_user; app.notify_cfg = notify_cfg
    app.notify_lock = notify_lock; app.prefs_lock = prefs_lock; app.prune_res = prune_res
    app.prune_reservations = prune_reservations; app.public_announce = public_announce; app.public_config = public_config
    app.public_res = public_res; app.read_log = read_log; app.refresh_cfg = refresh_cfg
    app.refresh_lock = refresh_lock; app.res_apply_approve = res_apply_approve; app.res_apply_cancel = res_apply_cancel
    app.res_apply_reject = res_apply_reject; app.res_csv = res_csv; app.res_decision_notify = res_decision_notify
    app.res_detail = res_detail; app.res_find_open = res_find_open; app.res_lock = res_lock
    app.res_put = res_put; app.res_status = res_status; app.reservations = reservations
    app.role_entry = role_entry; app.role_names = role_names; app.rolenames_lock = rolenames_lock
    app.roles = roles; app.roles_lock = roles_lock; app.roles_with_mail = roles_with_mail
    app.save_announce = save_announce; app.save_autoapprove = save_autoapprove; app.save_imports = save_imports
    app.save_notify = save_notify; app.save_res = save_res; app.save_rolenames = save_rolenames
    app.save_roles = save_roles; app.save_selector = save_selector; app.save_sessions = save_sessions
    app.save_storage_reqs = save_storage_reqs; app.save_teams = save_teams; app.save_tokens = save_tokens
    app.save_visibility = save_visibility; app.save_webhooks = save_webhooks; app.selector_lock = selector_lock
    app.session_ttl = session_ttl; app.sessions = sessions; app.sessions_dirty = sessions_dirty
    app.state = state; app.storage_cfg = storage_cfg; app.storage_lock = storage_lock
    app.storage_reqs = storage_reqs; app.storagereq_csv = storagereq_csv; app.store = store
    app.suspicious_login_name = suspicious_login_name; app.teams_lock = teams_lock; app.tier_interval = tier_interval
    app.tier_last = tier_last; app.token_list = token_list; app.tokens = tokens
    app.tokens_lock = tokens_lock; app.try_auto_approve = try_auto_approve; app.tz_available = tz_available
    app.user_prefs = user_prefs; app.vis_for = vis_for; app.visibility_cfg = visibility_cfg
    app.visibility_lock = visibility_lock; app.visible_res = visible_res; app.webhook_cfg = webhook_cfg
    app.webhooks_lock = webhooks_lock
    Handler = make_handler(app)

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
