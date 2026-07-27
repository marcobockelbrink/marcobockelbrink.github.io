#!/usr/bin/env python3
"""HTTP-Handler des Dashboards: Routen-Dispatch (Tabellen) und alle
Endpunkt-Handler. Die Fabrik make_handler(app) bindet den geteilten
App-Zustand (AppState aus serve()) als Closure — die Handler-Bodies
sind wortgleich zur früheren Fassung in aria_kapa.py (v3.0)."""
import gzip
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

from kapa.constants import (VERSION, ROLE_KEYS, DEFAULT_ROLE_NAMES,
                            NOTIFY_ROLE_EVENTS, WEBHOOK_EVENTS,
                            VIS_FEATURES)
from kapa.util import _html_escape
from kapa.mail import (_mail_values, render_template, MAIL_VARS,
                       DEFAULT_MAIL_SUBJECT, DEFAULT_MAIL_TEMPLATE)
from kapa.backup import sftp_backup, backup_rotate
from kapa.ldap import (ldap_bind, ldap_member_of, ldap_user_attr,
                       ldap_group_members)
from kapa.dashboard import (API_DOCS_HTML, LOGIN_TEMPLATE, POWERCLI_PS1,
                            openapi_spec, render_html, reviewer_doc_html,
                            user_doc_html)


def make_handler(app):
    """Handler-Klasse an den AppState binden (Zustand, Locks, Logik).
    Die Namen unten sind die frühere serve()-Closure — jetzt explizit
    als AppState-Attribute übergeben (in-place-Mutation, wie gehabt)."""
    TIERS = app.TIERS
    VALID_ROLES = app.VALID_ROLES
    VIS_ROLES = app.VIS_ROLES
    _clean_excl = app._clean_excl
    _clean_overrides = app._clean_overrides
    _clean_refreshcfg = app._clean_refreshcfg
    _clean_vfac = app._clean_vfac
    _clean_vlans = app._clean_vlans
    _clean_webhooks = app._clean_webhooks
    _merge_notify = app._merge_notify
    _sess_key = app._sess_key
    _src_ok = app._src_ok
    _wrow = app._wrow
    all_prefs = app.all_prefs
    announce_cfg = app.announce_cfg
    announce_lock = app.announce_lock
    apply_tz = app.apply_tz
    approval_teams = app.approval_teams
    args = app.args
    audit = app.audit
    auth_enabled = app.auth_enabled
    autoapprove_cfg = app.autoapprove_cfg
    autoapprove_lock = app.autoapprove_lock
    clean_announce = app.clean_announce
    clean_autoapprove = app.clean_autoapprove
    clean_import_clusters = app.clean_import_clusters
    clean_prefs = app.clean_prefs
    clean_selector = app.clean_selector
    clean_sources = app.clean_sources
    clean_storage_req = app.clean_storage_req
    clean_teams = app.clean_teams
    clean_visibility = app.clean_visibility
    cluster_selector = app.cluster_selector
    clusters_for = app.clusters_for
    current_team = app.current_team
    data_csv = app.data_csv
    do_refresh = app.do_refresh
    effective_sources = app.effective_sources
    emit_auto_events = app.emit_auto_events
    fire_webhooks = app.fire_webhooks
    group_role = app.group_role
    has_group_entries = app.has_group_entries
    history = app.history
    history_lock = app.history_lock
    import_lock = app.import_lock
    import_sources = app.import_sources
    interval = app.interval
    known_sources = app.known_sources
    login_blocked = app.login_blocked
    login_note_fail = app.login_note_fail
    login_reset = app.login_reset
    mail_event = app.mail_event
    net_cfg = app.net_cfg
    net_lock = app.net_lock
    new_res_id = app.new_res_id
    normalize_user = app.normalize_user
    notify_cfg = app.notify_cfg
    notify_lock = app.notify_lock
    prefs_lock = app.prefs_lock
    prune_res = app.prune_res
    prune_reservations = app.prune_reservations
    public_announce = app.public_announce
    public_config = app.public_config
    public_res = app.public_res
    read_log = app.read_log
    refresh_cfg = app.refresh_cfg
    refresh_lock = app.refresh_lock
    res_apply_approve = app.res_apply_approve
    res_apply_cancel = app.res_apply_cancel
    res_apply_reject = app.res_apply_reject
    res_csv = app.res_csv
    res_decision_notify = app.res_decision_notify
    res_detail = app.res_detail
    res_find_open = app.res_find_open
    res_lock = app.res_lock
    res_put = app.res_put
    res_status = app.res_status
    reservations = app.reservations
    role_entry = app.role_entry
    role_names = app.role_names
    rolenames_lock = app.rolenames_lock
    roles = app.roles
    roles_lock = app.roles_lock
    roles_with_mail = app.roles_with_mail
    save_announce = app.save_announce
    save_autoapprove = app.save_autoapprove
    save_imports = app.save_imports
    save_notify = app.save_notify
    save_res = app.save_res
    save_rolenames = app.save_rolenames
    save_roles = app.save_roles
    save_selector = app.save_selector
    save_sessions = app.save_sessions
    save_storage_reqs = app.save_storage_reqs
    save_teams = app.save_teams
    save_tokens = app.save_tokens
    save_visibility = app.save_visibility
    save_webhooks = app.save_webhooks
    selector_lock = app.selector_lock
    session_ttl = app.session_ttl
    sessions = app.sessions
    sessions_dirty = app.sessions_dirty
    state = app.state
    storage_cfg = app.storage_cfg
    storage_lock = app.storage_lock
    storage_reqs = app.storage_reqs
    storagereq_csv = app.storagereq_csv
    store = app.store
    suspicious_login_name = app.suspicious_login_name
    teams_lock = app.teams_lock
    tier_interval = app.tier_interval
    tier_last = app.tier_last
    token_list = app.token_list
    tokens = app.tokens
    tokens_lock = app.tokens_lock
    try_auto_approve = app.try_auto_approve
    tz_available = app.tz_available
    user_prefs = app.user_prefs
    vis_for = app.vis_for
    visibility_cfg = app.visibility_cfg
    visibility_lock = app.visibility_lock
    visible_res = app.visible_res
    webhook_cfg = app.webhook_cfg
    webhooks_lock = app.webhooks_lock

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
            if "Cache-Control" not in (headers or {}):
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

        def _json(self, obj, code=200, headers=None, etag=False):
            body = json.dumps(obj, ensure_ascii=False)
            if etag and code == 200:
                # Schwacher ETag über den (unkomprimierten) Inhalt: unveränderte
                # Poll-Antworten kosten nur noch ein 304 statt des vollen Bodys.
                # "no-cache" (statt no-store) heißt: speichern erlaubt, aber vor
                # jeder Verwendung revalidieren — fetch() macht das transparent.
                tag = 'W/"' + hashlib.sha256(body.encode()).hexdigest()[:20] + '"'
                if tag in (self.headers.get("If-None-Match") or ""):
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Vary", "Accept-Encoding")
                    self.end_headers()
                    return
                h = dict(headers or {})
                h["ETag"] = tag
                h.setdefault("Cache-Control", "no-cache")
                return self._send(body, "application/json; charset=utf-8",
                                  code, h)
            self._send(body, "application/json; charset=utf-8", code, headers)

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

        @staticmethod
        def _v1_alt(path):
            """v3: EIN Pfadraum. Unbekannte /api/v1/X-Pfade werden auf die
            Session-API /api/X abgebildet — identische Handler, identische
            Rollen-/Team-Prüfungen (Session-only; Token greifen hier nicht)."""
            if path.startswith("/api/v1/"):
                return "/api/" + path[len("/api/v1/"):]
            return None

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            self._route = parsed.path
            self._query = urllib.parse.parse_qs(parsed.query)
            fn = self._GET_ROUTES.get(self._route)
            if fn:
                return fn(self)
            alt = self._v1_alt(self._route)
            if alt and alt in self._GET_ROUTES:
                self._route = alt
                return self._GET_ROUTES[alt](self)
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
                        "clusters": clusters_for(s)}, etag=True)

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
                self._json(visible_res(s), etag=True)

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

        def _get_user_doc(self):
            # Benutzerhandbuch für alle Rollen (Schwester des Reviewer-
            # Handbuchs) — zweisprachig, mit den konfigurierten Rollennamen.
            with rolenames_lock:
                rn = dict(role_names)
            self._send(user_doc_html(self._lang(), VERSION, rn),
                       "text/html; charset=utf-8")

        def _get_healthz(self):
            # Monitoring-Endpunkt: bewusst OHNE Authentifizierung, dafür
            # nur unkritische Betriebsdaten (keine Cluster-/Nutzerdaten).
            now = time.time()
            age = int(now - state["last"]) if state.get("last") else None
            self._json({
                "status": ("error" if state.get("error")
                           and not state.get("clusters") else "ok"),
                "version": VERSION,
                "updated": state["updated"] or None,
                "data_age_seconds": age,
                # Alter je gestaffeltem Teilbereich — so kann das Monitoring
                # alarmieren, wenn z. B. nur der Storage-Abruf hängt.
                "tiers_age_seconds": {t: (int(now - tier_last[t])
                                          if tier_last.get(t) else None)
                                      for t in TIERS},
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
                # v3: Felder additiv erweitert (progress/error/tiers, next
                # tier-genau) — die UI liest den Status jetzt auch hier.
                nxt = None
                if interval > 0 and any(tier_last.values()):
                    nxt = max(0, int(min(tier_last[t] + tier_interval(t)
                                         for t in TIERS) - time.time()))
                self._json({"version": VERSION, "updated": state["updated"],
                            "refreshing": state["refreshing"], "next": nxt,
                            "progress": state["progress"],
                            "error": state["error"],
                            "tiers": state.get("tiers") or {}})
            elif route == "/api/v1/data":
                # Token = externe Anwendung (Workload ok); Session eines
                # Anforderers bekommt den Workload wie im UI nicht.
                cl = state["clusters"] if tok else clusters_for(s)
                if query.get("format", [""])[0] == "csv":
                    self._send(data_csv(cl, self._lang()),
                               "text/csv; charset=utf-8")
                else:
                    self._json({"updated": state["updated"], "clusters": cl},
                               etag=True)
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
                    self._json(data, etag=True)

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
            "/benutzerhandbuch": _get_user_doc,
            "/benutzerhandbuch/": _get_user_doc,
            "/user-guide": _get_user_doc,
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
            alt = self._v1_alt(self.path)
            if alt:
                fn = self._POST_ROUTES.get(alt)
                if fn:
                    self.path = alt        # ID-Parser arbeiten auf self.path
                    return fn(self)
                for pred, pfn in self._POST_PATTERNS:
                    if pred(alt):
                        self.path = alt
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
            # v3, EIN Pfadraum: ohne Bearer-Header übernimmt der Session-
            # Handler — identische Rollen-Prüfung und Antwort wie im UI.
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                self.path = "/api/reservations"
                return self._post_reservations()
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
            # v3, EIN Pfadraum: ohne Bearer-Header übernehmen die Session-
            # Handler — inkl. Rollen- UND Team-an-der-Reihe-Prüfung (die
            # Token-Semantik ohne Team-Bindung gilt nur für echte Token).
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                self.path = "/api/" + self.path[len("/api/v1/"):]
                if op == "cancel":
                    return self._post_res_cancel()
                return self._post_res_decision()
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
            alt = self._v1_alt(self.path)
            if alt:
                fn = self._PUT_ROUTES.get(alt)
                if fn:
                    self.path = alt
                    return fn(self)
                for prefix, pfn in self._PUT_PREFIXES:
                    if alt.startswith(prefix):
                        self.path = alt
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
            alt = self._v1_alt(self.path)
            if alt:
                for prefix, fn in self._DELETE_PREFIXES:
                    if alt.startswith(prefix):
                        self.path = alt
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
    return Handler
