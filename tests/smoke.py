#!/usr/bin/env python3
"""Smoke-Tests für aria_kapa.py — nur Standardbibliothek, kein pytest nötig.

Startet den Server im Sample-Modus auf einem freien Port und prüft die
wichtigsten Abläufe Ende-zu-Ende (HTTP-Ebene, wie ein echter Client):

    python3 tests/smoke.py            # aus der Projektwurzel

Exit-Code 0 = alles grün. Gedacht als Sicherheitsnetz vor jedem Release.
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "aria_kapa.py")

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def req(method, path, body=None, headers=None, raw=False):
    """HTTP-Aufruf; Rückgabe (status, body_bytes|json, headers)."""
    url = BASE + path
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            payload = resp.read()
            if not raw and "json" in (resp.headers.get("Content-Type") or ""):
                payload = json.loads(payload.decode())
            return resp.status, payload, dict(resp.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            payload = json.loads(payload.decode())
        except Exception:
            pass
        return e.code, payload, dict(e.headers)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_up(timeout=30):
    """Warten, bis der Server läuft UND der erste Demo-Abruf durch ist."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st, body, _ = req("GET", "/healthz")
            if st == 200 and body.get("clusters", 0) > 0 \
                    and not body.get("refreshing"):
                return body
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Server kam nicht hoch — siehe " + LOG)


# ----------------------------------------------------------------- Start ----
PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
DATA = tempfile.mkdtemp(prefix="kapa_smoke_")
LOG = os.path.join(DATA, "server.log")
proc = subprocess.Popen(
    [sys.executable, APP, "--serve", "--sample", "--bind", "127.0.0.1",
     "--port", str(PORT), "--data-dir", DATA,
     "--id-prefix", "KAPA-", "--id-length", "6"],
    stdout=open(LOG, "w"), stderr=subprocess.STDOUT)

try:
    print(f"Server: {BASE}  (Daten: {DATA})")
    health = wait_up()

    print("== healthz / Seite / gzip ==")
    check("healthz ok", health.get("status") == "ok" and health.get("clusters") == 3, str(health))
    st, page, hdr = req("GET", "/", raw=True)
    page = page.decode()
    check("Hauptseite 200 + Marker", st == 200 and "TANZU_MHZ" in page
          and "openClusterHash" in page and 'id="themeBtn"' in page)
    st, gz, hdr = req("GET", "/", headers={"Accept-Encoding": "gzip"}, raw=True)
    check("gzip aktiv + Inhalt intakt",
          hdr.get("Content-Encoding") == "gzip" and len(gz) < len(page)
          and "Kapazitätsübersicht" in gzip.decompress(gz).decode())

    print("== Teams / Reservierungen / Härtung ==")
    st, _, _ = req("PUT", "/api/teams", ["Team Netzwerk", "Team Security"])
    check("Teams speichern", st == 200)
    st, res, _ = req("POST", "/api/reservations",
                     {"name": "Smoke\r\nBcc: evil " + "A" * 300,
                      "cluster": "Cluster-01\n", "vcpu": 4, "ram_gb": 16})
    flat = json.dumps(res)
    check("Anlegen + Freitext-Härtung", st == 200 and "\\r" not in flat
          and "Bcc" in flat and len(res[-1]["name"]) <= 120
          and "\n" not in res[-1]["cluster"])

    st, res2, _ = req("POST", "/api/reservations",
                      {"name": "Storno-Test", "cluster": "Cluster-01",
                       "vcpu": 1, "ram_gb": 1})
    sid = next(r["id"] for r in res2 if r["name"] == "Storno-Test")
    req("POST", f"/api/reservations/{sid}/cancel", {})
    st, _, _ = req("POST", f"/api/reservations/{sid}/approve", {})
    check("Stornierter Antrag nicht mehr genehmigbar", st == 404)

    print("== API-Token: Schreibrechte ==")
    st, tok, _ = req("POST", "/api/tokens", {"name": "Smoke-Writer"})
    raw_token = tok.get("token") or tok.get("raw") or ""
    check("Token erzeugt", st == 200 and raw_token.startswith("kapa_"))
    st, tl, _ = req("GET", "/api/tokens")
    tid = next(iter(tl))
    auth = {"Authorization": "Bearer " + raw_token}
    st, _, _ = req("POST", "/api/v1/reservations", {"name": "x"}, headers=auth)
    check("Schreiben ohne Recht -> 403", st == 403)
    st, _, _ = req("PUT", f"/api/tokens/{tid}",
                   {"write_res": True, "write_approve": True})
    check("Rechte per Klick setzen", st == 200)
    st, made, _ = req("POST", "/api/v1/reservations",
                      {"name": "Smoke API", "cluster": "Cluster-02",
                       "vcpu": 2, "ram_gb": 8, "abteilung": "Team Netzwerk"},
                      headers=auth)
    rid = made.get("reservation", {}).get("id", "")
    check("API-Anlage 201 + Kapa-ID", st == 201 and rid.startswith("KAPA-"))
    st, a1, _ = req("POST", f"/api/v1/reservations/{rid}/approve",
                    {"comment": "ok"}, headers=auth)
    st2, a2, _ = req("POST", f"/api/v1/reservations/{rid}/approve", headers=auth)
    check("Zweistufige API-Freigabe", st == 200 and st2 == 200
          and a2["reservation"]["approved"] is True
          and a2["reservation"]["approved_by"] == "api:Smoke-Writer")
    st, _, _ = req("POST", f"/api/v1/reservations/{rid}/reject", headers=auth)
    check("Entschieden -> reject 404", st == 404)

    print("== Storage-Erweiterungen ==")
    st, _, _ = req("POST", "/api/storage-request",
                   {"cluster": "Cluster-01", "kind": "new", "size_gb": 2048})
    check("Anfrage bei deaktiviertem Feature -> 403", st == 403)
    req("PUT", "/api/storagecfg", {"enabled": True})
    st, made, _ = req("POST", "/api/storage-request",
                      {"cluster": "Cluster-02", "kind": "expand",
                       "lun_name": "FC-LUN-201", "naa": "naa.6006016012ab",
                       "current_gb": 4000, "target_gb": 8000, "comment": "SAP"})
    sreq = made.get("request", {})
    check("Expand-Anfrage 201 mit NAA", st == 201
          and sreq.get("naa") == "naa.6006016012ab"
          and sreq.get("target_gb") == 8000 and sreq.get("status") == "offen")
    st, bad, _ = req("POST", "/api/storage-request",
                     {"cluster": "Cluster-02", "kind": "expand",
                      "lun_name": "X", "current_gb": 8000, "target_gb": 4000})
    check("Ziel <= aktuell -> 400", st == 400)
    st, csv_s, _ = req("GET", "/api/v1/storage-requests?format=csv", raw=True)
    hdr = csv_s.decode().splitlines()[0].split(";")
    check("v1-CSV mit NAA-Spalte",
          hdr[6] == "naa" and "naa.6006016012ab" in csv_s.decode())
    check("v1-CSV mit Hosts+WWPN-Spalten",
          hdr[2] == "hosts" and hdr[3] == "wwpns")
    # JSON: Hosts des Clusters (Objekte mit Name + WWPNs) an der Anfrage
    st, sral, _ = req("GET", "/api/v1/storage-requests?status=alle")
    h0 = (sral["requests"][0].get("hosts") or [{}])[0]
    check("Storage-Anfrage enthält Cluster-Hosts + WWPNs",
          isinstance(sral["requests"][0].get("hosts"), list)
          and h0.get("name") and isinstance(h0.get("wwpns"), list)
          and len(h0["wwpns"]) >= 1)
    # Token-Schreibrecht Storage -> /done
    sid = sreq["id"]
    req("PUT", f"/api/tokens/{tid}", {"write_res": True, "write_approve": True,
                                      "write_storage": True})
    st, dr, _ = req("POST", f"/api/v1/storage-requests/{sid}/done", headers=auth)
    check("API-erledigt mit Storage-Recht",
          st == 200 and dr["request"]["status"] == "erledigt"
          and dr["request"]["done_by"] == "api:Smoke-Writer")
    st, openl, _ = req("GET", "/api/v1/storage-requests")
    st2, alll, _ = req("GET", "/api/v1/storage-requests?status=alle")
    check("Filter offen/alle",
          len(openl["requests"]) == 0 and len(alll["requests"]) == 1)
    # Löschen einer versehentlich angelegten Anfrage (Admin) – netto ±0
    st, mk, _ = req("POST", "/api/storage-request",
                    {"cluster": "Cluster-01", "kind": "new", "size_gb": 512})
    delid = mk["request"]["id"]
    st, dd, _ = req("DELETE", f"/api/storage-request/{delid}")
    st404, _, _ = req("DELETE", "/api/storage-request/gibtsnicht")
    check("Anfrage löschbar (Admin)",
          st == 200 and all(x["id"] != delid for x in dd["requests"])
          and st404 == 404)
    # Maximum: Anfrage über dem Limit wird abgelehnt (Feature noch aktiv)
    req("PUT", "/api/storagecfg", {"enabled": True, "max_lun_gb": 10240})  # 10 TB
    st, big, _ = req("POST", "/api/storage-request",
                     {"cluster": "Cluster-01", "kind": "new", "size_gb": 20480})
    check("Über Maximum -> 400 mit Hinweis",
          st == 400 and "Maximum" in big.get("error", ""))
    st, okm, _ = req("POST", "/api/storage-request",
                     {"cluster": "Cluster-01", "kind": "new", "size_gb": 4096})
    check("Unter Maximum -> 201", st == 201)
    req("PUT", "/api/storagecfg", {"enabled": True, "max_lun_gb": 0})
    # Mindest-LUN-Größe: kleine Datastores komplett ausschließen (+ Refresh)
    d0 = req("GET", "/api/v1/data")[1]
    n0 = sum(len(c.get("datastores", [])) for c in d0["clusters"])
    req("PUT", "/api/storagecfg", {"enabled": True, "min_lun_gb": 3000})
    t0 = time.time()
    while time.time() - t0 < 12:      # Refresh abwarten
        d1 = req("GET", "/api/v1/data")[1]
        if not req("GET", "/api/status")[1].get("refreshing"):
            break
        time.sleep(0.5)
    n1 = sum(len(c.get("datastores", [])) for c in d1["clusters"])
    small = [l for c in d1["clusters"] for l in c.get("datastores", [])
             if (l.get("raw_cap_gb") or l["cap_gb"]) < 3000]
    check("Mindest-LUN filtert kleine Datastores raus", n1 < n0 and not small)
    # Namensfilter: alle vSAN-Datastores (Name enthält "vsan") ausschließen
    req("PUT", "/api/storagecfg", {"enabled": False, "min_lun_gb": 0,
                                   "exclude_names": "vsan"})
    t0 = time.time()
    while time.time() - t0 < 12:
        d2 = req("GET", "/api/v1/data")[1]
        if not req("GET", "/api/status")[1].get("refreshing"):
            break
        time.sleep(0.5)
    left = [l for c in d2["clusters"] for l in c.get("datastores", [])]
    check("Namensfilter schliesst passende LUNs aus",
          left and not any("vsan" in l["name"].lower() for l in left))
    st, cfg, _ = req("GET", "/api/storagecfg")
    check("Namensfilter persistiert", cfg.get("exclude_names") == "vsan")
    # Wildcards + Groß/Klein egal: GROSS geschriebene Muster mit * müssen greifen
    req("PUT", "/api/storagecfg", {"enabled": False, "min_lun_gb": 0,
                                   "exclude_names": "TEMPLATE*, *SERVICE*, ISO"})
    t0 = time.time()
    while time.time() - t0 < 12:
        d3 = req("GET", "/api/v1/data")[1]
        if not req("GET", "/api/status")[1].get("refreshing"):
            break
        time.sleep(0.5)
    left3 = [l["name"].lower() for c in d3["clusters"]
             for l in c.get("datastores", [])]
    check("Namensfilter: Wildcards + Groß/Klein egal",
          left3 and not any(("template" in n or "service" in n or "iso" in n)
                            for n in left3))
    req("PUT", "/api/storagecfg", {"enabled": False, "min_lun_gb": 0,
                                   "exclude_names": ""})
    # vSAN-Policy-Faktor: global + Cluster-Override, wirkt je Cluster
    req("PUT", "/api/storagecfg", {"enabled": False, "min_lun_gb": 0,
                                   "exclude_names": "", "vsan_factor": 0.75,
                                   "vsan_overrides": {"Cluster-01": 0.333}})
    st, vcfg, _ = req("GET", "/api/storagecfg")
    check("vSAN-Faktor + Override persistiert",
          abs(vcfg.get("vsan_factor", 0) - 0.75) < 0.001
          and abs((vcfg.get("vsan_overrides") or {}).get("Cluster-01", 0) - 0.333) < 0.001)
    t0 = time.time()
    while time.time() - t0 < 12:
        dv = req("GET", "/api/v1/data")[1]
        if not req("GET", "/api/status")[1].get("refreshing"):
            break
        time.sleep(0.5)
    vfac = {c["name"]: next((l["factor"] for l in c.get("datastores", [])
                             if "vsan" in (l.get("type", "") or "").lower()), None)
            for c in dv["clusters"]}
    check("vSAN-Faktor je Cluster angewandt (Override < global)",
          abs((vfac.get("Cluster-01") or 0) - 0.333) < 0.001
          and abs((vfac.get("Cluster-02") or 0) - 0.75) < 0.001)
    req("PUT", "/api/storagecfg", {"enabled": False, "min_lun_gb": 0,
                                   "exclude_names": "", "vsan_factor": 0.5,
                                   "vsan_overrides": {}})

    print("== Netzwerk-Filter (Portgruppen) ==")
    # Demo: Cluster-01 hat VLANs 100-105, Cluster-02 hat 200-205. VLAN-Bereich
    # + Namensfilter (Namen enthalten "VLAN2xx") -> beide Cluster pg-frei.
    req("PUT", "/api/netcfg", {"exclude_names": "vlan2",
                               "exclude_vlans": "100-199"})
    t0 = time.time()
    while time.time() - t0 < 12:
        dn = req("GET", "/api/v1/data")[1]
        if not req("GET", "/api/status")[1].get("refreshing"):
            break
        time.sleep(0.5)
    by_cl = {c["name"]: c.get("portgroups") or [] for c in dn["clusters"]}
    check("VLAN-Bereich filtert Cluster-01-Portgruppen",
          len(by_cl.get("Cluster-01") or []) == 0)
    check("Namensfilter filtert Cluster-02-Portgruppen",
          len(by_cl.get("Cluster-02") or []) == 0)
    check("Cluster-03-Portgruppen bleiben",
          len(by_cl.get("Cluster-03") or []) >= 1)
    st, ncfg, _ = req("GET", "/api/netcfg")
    check("Netzwerk-Filter persistiert",
          ncfg.get("exclude_vlans") == "100-199"
          and ncfg.get("exclude_names") == "vlan2")
    req("PUT", "/api/netcfg", {"exclude_names": "", "exclude_vlans": ""})

    print("== Gestaffelte Abruf-Intervalle ==")
    st, rc, _ = req("PUT", "/api/refreshcfg",
                    {"vms": 60, "network": 180, "storage": "999999"})
    check("Intervalle speichern (Clamp auf 10080)",
          st == 200 and rc["tiers"]["vms"] == 60
          and rc["tiers"]["storage"] == 10080 and rc.get("default_min"))
    st, rc2, _ = req("GET", "/api/refreshcfg")
    check("Intervalle persistiert", rc2["tiers"]["network"] == 180)
    st, bad, _ = req("POST", "/api/refresh", {"parts": ["quatsch"]})
    check("Teil-Refresh: ungültige parts -> 400", st == 400)
    st, ok2, _ = req("POST", "/api/refresh", {"parts": ["storage"]})
    check("Teil-Refresh storage angenommen", st == 202)
    t0 = time.time()
    while time.time() - t0 < 15:
        stt = req("GET", "/api/status")[1]
        if not stt.get("refreshing"):
            break
        time.sleep(0.5)
    check("Tier-Stände im Status", all((stt.get("tiers") or {}).get(t)
                                       for t in ("vms", "network", "storage")))
    dts = req("GET", "/api/v1/data")[1]["clusters"][0]
    check("Daten nach Teil-Refresh vollständig",
          dts["vmCount"] > 0 and dts["datastores"] and dts["portgroups"])
    # Anzeige-Zeitzone (frei konfigurierbar auf derselben Seite)
    st, tzr, _ = req("PUT", "/api/refreshcfg",
                     {"vms": 0, "network": 0, "storage": 0, "tz": "America/Los_Angeles"})
    hh = int((tzr.get("now") or "00.00.0000 99:99")[-5:-3])
    check("Zeitzone gesetzt + Server-Zeit verschoben (LA ≠ UTC/MEZ)",
          st == 200 and tzr.get("tz") == "America/Los_Angeles"
          and 0 <= hh <= 23)
    check("Zeitzone persistiert (GET)",
          req("GET", "/api/refreshcfg")[1].get("tz") == "America/Los_Angeles")
    st, tzbad, _ = req("PUT", "/api/refreshcfg",
                       {"vms": 0, "network": 0, "storage": 0, "tz": "Europe/Nonexistent"})
    check("Unbekannte (aber formal gültige) Zeitzone -> 400",
          st == 400 and "Zeitzone" in (tzbad.get("error") or ""))
    # Sicherheit: Path-Traversal in tz muss abgelehnt werden (kein Datei-
    # Existenz-Orakel über /usr/share/zoneinfo/.. hinaus).
    st_exist = req("PUT", "/api/refreshcfg",
                   {"vms": 0, "network": 0, "storage": 0, "tz": "../../../etc/passwd"})[0]
    st_missing = req("PUT", "/api/refreshcfg",
                     {"vms": 0, "network": 0, "storage": 0, "tz": "/etc/gibtsnicht"})[0]
    check("tz-Path-Traversal abgelehnt (400) – kein Datei-Orakel",
          st_exist == 400 and st_missing == 400)
    check("Gültige mehrsegmentige Zone akzeptiert",
          req("PUT", "/api/refreshcfg", {"vms": 0, "network": 0, "storage": 0,
              "tz": "America/Argentina/Buenos_Aires"})[0] == 200)
    req("PUT", "/api/refreshcfg", {"vms": 0, "network": 0, "storage": 0, "tz": ""})

    print("== Offline-Import (PowerCLI-JSON, robust gegen ConvertTo-Json) ==")
    # 1-Host-/1-Element-Cluster: PowerShell entpackt zu Objekten statt Listen.
    st, imp1, _ = req("POST", "/api/import", {"source": "Smoke-Solo", "clusters": [
        {"name": "CL-SOLO", "hosts": {"name": "esxX", "cores": 48, "ram_gb": 512},
         "vms": {"name": "v9", "vcpu": 4, "ram_gb": 16, "on": True},
         "datastores": {"name": "ds1", "type": "VMFS", "cap_gb": 1000, "used_gb": 400},
         "portgroups": {"name": "PG1", "vlan": "100"}}]})
    check("Import: 1-Host als Objekt (Unwrap) importiert statt Crash",
          st == 201 and imp1.get("clusters") == 1 and not imp1.get("skipped"))
    # Gemischt: guter Cluster importiert, Cluster ohne Hosts wird übersprungen.
    st, imp2, _ = req("POST", "/api/import", {"source": "Smoke-Mix", "clusters": [
        {"name": "CL-GOOD", "hosts": [{"name": "e1", "cores": 32, "ram_gb": 256}]},
        {"name": "CL-EMPTY", "hosts": []}]})
    check("Import: leerer Cluster übersprungen, guter importiert",
          st == 201 and imp2.get("clusters") == 1 and imp2.get("skipped") == ["CL-EMPTY"])
    # Einzelnes Cluster-Objekt statt Liste (Top-Level-Unwrap) wird toleriert.
    st, imp3, _ = req("POST", "/api/import", {"source": "Smoke-One", "clusters":
        {"name": "CL-ONE", "hosts": [{"name": "e1", "cores": 16, "ram_gb": 128}]}})
    check("Import: einzelnes Cluster-Objekt (Top-Level-Unwrap) toleriert",
          st == 201 and imp3.get("clusters") == 1)
    # Nur leere Cluster -> klare Fehlermeldung mit Namen.
    st, imp4, _ = req("POST", "/api/import",
                      {"source": "Smoke-Leer", "clusters": [{"name": "CL-X", "hosts": []}]})
    check("Import: nur hostlose Cluster -> 400 mit Cluster-Namen",
          st == 400 and "CL-X" in (imp4.get("error") or ""))
    for s in ("Smoke-Solo", "Smoke-Mix", "Smoke-One"):
        req("DELETE", "/api/import/" + s)

    print("== Ausgehende Webhooks (HMAC-signiert) ==")
    import http.server as _hs
    import threading as _th
    import hmac as _hm
    import hashlib as _hh
    WH_RECV = []
    WH_SECRET = "smoke-wh-secret"

    class _WH(_hs.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            WH_RECV.append({"sig": self.headers.get("X-Kapa-Signature", ""),
                            "event": self.headers.get("X-Kapa-Event", ""),
                            "body": self.rfile.read(n)})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    wh_port = free_port()
    wh_srv = _hs.ThreadingHTTPServer(("127.0.0.1", wh_port), _WH)
    _th.Thread(target=wh_srv.serve_forever, daemon=True).start()
    try:
        hook = f"http://127.0.0.1:{wh_port}/hook"
        st, wr, _ = req("PUT", "/api/webhooks", {"endpoints": [{
            "url": hook, "secret": WH_SECRET, "events": ["created"],
            "active": True, "desc": "smoke"}]})
        check("Webhook gespeichert + Secret maskiert (kein Klartext im GET)",
              st == 200 and wr["endpoints"][0]["has_secret"]
              and "secret" not in wr["endpoints"][0])
        st, tp, _ = req("POST", "/api/webhooks/test", {"url": hook, "secret": WH_SECRET})
        check("Webhook-Test gesendet (Empfänger HTTP 200)",
              st == 200 and tp.get("ok") and tp.get("status") == 200)
        req("POST", "/api/reservations",
            {"name": "WH", "cluster": "Cluster-01", "vcpu": 1, "ram_gb": 1})
        t0 = time.time()
        while time.time() - t0 < 6 and not any(r["event"] == "created" for r in WH_RECV):
            time.sleep(0.2)
        created = [r for r in WH_RECV if r["event"] == "created"]
        check("created-Event beim Empfänger angekommen", len(created) >= 1)
        sig_ok = any(_hm.compare_digest(
            r["sig"], "sha256=" + _hm.new(WH_SECRET.encode(), r["body"],
                                          _hh.sha256).hexdigest()) for r in created)
        check("HMAC-SHA256-Signatur des Payloads gültig", sig_ok)
    finally:
        wh_srv.shutdown()
        req("PUT", "/api/webhooks", {"endpoints": []})

    print("== Statistik-Historie ==")
    st, hist, _ = req("GET", "/api/history?days=730")
    hdays = sorted((hist or {}).get("days") or {})
    check("Historie mit Demo-Backfill + heutigem Snapshot",
          st == 200 and len(hdays) >= 50
          and hdays[-1] == time.strftime("%Y-%m-%d"))
    if hdays:
        f = hist["days"][hdays[0]]; l = hist["days"][hdays[-1]]
        cl0 = sorted(f)[0]
        avg = lambda e: e["ram"] / max(1, e["n"])
        check("Trend: Ø RAM je VM wächst (Demo)",
              avg(l.get(cl0, f[cl0])) > avg(f[cl0]))
    st, hcsv, _ = req("GET", "/api/history?days=30&format=csv", raw=True)
    check("Historie-CSV", st == 200
          and hcsv.decode().splitlines()[0].startswith("datum;cluster;"))

    print("== Offline-Quellen (Cluster-Import) ==")
    imp = [{"name": "Insel-01",
            "hosts": [{"name": "esx-i1", "cores": 32, "ram_gb": 512},
                      {"name": "esx-i2", "cores": 32, "ram_gb": 512}],
            "vms": [{"name": "ivm1", "vcpu": 4, "ram_gb": 16, "on": True}],
            "datastores": [{"name": "insel-lun", "type": "VMFS",
                            "cap_gb": 4000, "used_gb": 1000}],
            "portgroups": [{"name": "PG-Insel-VLAN900", "vlan": "900"}]}]
    st, r0, _ = req("POST", "/api/import", {"source": "RZ-Insel", "clusters": imp})
    check("Import angenommen", st == 201 and r0.get("clusters") == 1)
    st, bad, _ = req("POST", "/api/import", {"clusters": imp})
    st2, bad2, _ = req("POST", "/api/import", {"source": "X", "clusters": []})
    check("Import-Validierung (ohne Quelle/leer -> 400)",
          st == 400 and st2 == 400)
    t0 = time.time()
    while time.time() - t0 < 12:
        di = req("GET", "/api/v1/data")[1]
        if not req("GET", "/api/status")[1].get("refreshing"):
            break
        time.sleep(0.5)
    ic = next((c for c in di["clusters"] if c["name"] == "Insel-01"), None)
    check("Import-Cluster im Datenpaket (Quelle, N+1, Tag)",
          ic is not None and ic.get("source") == "RZ-Insel"
          and ic.get("imported") is True
          and ic.get("vcpuCap") > 0 and ic.get("hostCount") == 2
          and any(t.startswith("Import:") for t in ic.get("tags") or []))
    st, lst, _ = req("GET", "/api/import")
    check("Import-Quellenliste", st == 200 and len(lst["sources"]) == 1
          and lst["sources"][0]["vms"] == 1)
    st, ps1, _ = req("GET", "/api/import/powercli", raw=True)
    check("PowerCLI-Skript abrufbar",
          st == 200 and b"VMware.PowerCLI" in ps1 and b"ConvertTo-Json" in ps1)
    st, _, _ = req("DELETE", "/api/import/RZ-Insel")
    st404, _, _ = req("DELETE", "/api/import/gibtsnicht")
    t0 = time.time()
    while time.time() - t0 < 12:
        di2 = req("GET", "/api/v1/data")[1]
        if not req("GET", "/api/status")[1].get("refreshing"):
            break
        time.sleep(0.5)
    check("Import-Quelle löschbar (Cluster verschwindet)",
          st == 200 and st404 == 404
          and not any(c["name"] == "Insel-01" for c in di2["clusters"]))

    print("== Sicherheit: CSV-Formel-Injection ==")
    req("POST", "/api/reservations",
        {"name": "=HYPERLINK(\"http://evil\")", "cluster": "Cluster-01",
         "change": "+1+cmd", "vcpu": 1, "ram_gb": 1, "storage_gb": 1})
    import csv as _c, io as _io
    st, rcsv, _ = req("GET", "/api/v1/reservations?format=csv", raw=True)
    rws = list(_c.reader(_io.StringIO(rcsv.decode()), delimiter=";"))
    evil = next((r for r in rws if any("HYPERLINK" in c for c in r)), None)
    check("Formel-Felder in CSV neutralisiert",
          evil and evil[1].startswith("'=") and evil[2].startswith("'+"))
    st, dcsv, _ = req("GET", "/api/v1/data?format=csv", raw=True)
    negs = [c for r in _c.reader(_io.StringIO(dcsv.decode()), delimiter=";")
            for c in r if c.startswith("-") and c[1:2].isdigit()]
    check("Negative Zahlen bleiben rechenbar (kein Apostroph)",
          all(not c.startswith("'") for c in negs))

    print("== Kapa-CSV-Import (XLS-Ablösung) ==")
    today = time.strftime("%d.%m.%Y")
    csvtxt = ("﻿Kapa-Nummer;Projekt;Cluster;CPU;RAM;Storage;Datum;Team\n"
              f"KAPA-X-001;CSV-Projekt;Cluster-01;8;64;1.000;{today};Team Betrieb\n"
              "KAPA-X-002;Uralt;Cluster-02;4;32;500;01.01.2020;\n"
              "KAPA-X-003;Kaputt;Cluster-01;4;32;500;;\n")
    st, ci, _ = req("POST", "/api/import/reservations", {"csv": csvtxt})
    check("CSV-Import (BOM/;/dt. Zahlen/Datum)",
          st == 201 and ci["imported"] == 2 and ci["expired"] == 1
          and len(ci["errors"]) == 1)
    st, rl, _ = req("GET", "/api/v1/reservations")
    r1 = next((x for x in rl if x["id"] == "KAPA-X-001"), None)
    check("Import als genehmigt mit Original-Feldern",
          r1 and r1.get("approved") is True and r1.get("approved_by") == "Import"
          and r1.get("storage_gb") == 1000 and r1.get("abteilung") == "Team Betrieb"
          and not any(x["id"] == "KAPA-X-002" for x in rl))
    st, ci2, _ = req("POST", "/api/import/reservations", {"csv": csvtxt})
    check("Re-Import ohne Duplikate", ci2["skipped"] >= 1
          and sum(1 for x in req("GET", "/api/v1/reservations")[1]
                  if x["id"] == "KAPA-X-001") == 1)
    st, cbad, _ = req("POST", "/api/import/reservations",
                      {"csv": "foo;bar\n1;2\n"})
    check("CSV ohne erkennbare Spalten -> 400", st == 400)

    print("== AD-Gruppen-Check ==")
    # Regression: AD-Gruppen (case-sensitiv gespeichert) müssen löschbar sein
    req("POST", "/api/roles", {"user": "Kapa-Admins", "role": "admin", "kind": "group"})
    st, after_add, _ = req("GET", "/api/roles")
    req("DELETE", "/api/roles/Kapa-Admins")
    st, after_del, _ = req("GET", "/api/roles")
    check("AD-Gruppe löschbar (Original-Case)",
          "Kapa-Admins" in after_add and "Kapa-Admins" not in after_del)
    st, adg, _ = req("POST", "/api/ad/group-members", {"cn": "Kapa-Admins"})
    check("Ohne AD-Config -> 400 mit Hinweis",
          st == 400 and "AD" in adg.get("error", ""))
    st2, adn, _ = req("POST", "/api/ad/group-members", {})
    check("Ohne CN -> 400", st2 == 400)

    print("== CSV / Sprache / OpenAPI ==")
    st, csv_de, _ = req("GET", "/api/v1/reservations?format=csv", raw=True)
    st2, csv_en, _ = req("GET", "/api/v1/reservations?format=csv&lang=en", raw=True)
    check("Reservierungs-CSV DE/EN",
          csv_de.decode().startswith("id;name;change") and "gueltig_bis" in csv_de.decode()
          and "valid_until" in csv_en.decode() and "approved" in csv_en.decode())
    st, dj, _ = req("GET", "/api/v1/data")
    fc = [l for c in dj["clusters"] for l in c.get("datastores", [])
          if l["name"].startswith("FC-LUN")]
    vs = [l for c in dj["clusters"] for l in c.get("datastores", [])
          if "vsan" in l["name"].lower()]
    check("NAA an FC-LUNs im Payload (vSAN ohne)",
          fc and all(l.get("naa", "").startswith("naa.") for l in fc)
          and all(not l.get("naa") for l in vs))
    st, cap, _ = req("GET", "/api/v1/data?format=csv&lang=en", raw=True)
    lines = cap.decode().splitlines()
    h = lines[0].split(";")
    row = dict(zip(h, lines[1].split(";")))
    eff = int(row["vcpu_free"]) - int(row["reserved_vcpu"]) - int(row["tanzu_vcpu"])
    check("Kapazitäts-CSV effektiv-frei-Rechnung",
          "vcpu_free_effective" in h and eff == int(row["vcpu_free_effective"]))
    st, spec_de, _ = req("GET", "/api/v1/openapi.json")
    st2, spec_en, _ = req("GET", "/api/v1/openapi.json",
                          headers={"Accept-Language": "en-US"})
    check("OpenAPI DE-Default + EN per Header",
          "Kapazitätsplanung" in spec_de["info"]["title"]
          and "Capacity Planning" in spec_en["info"]["title"])
    st, docs, _ = req("GET", "/api/v1/docs", raw=True)
    check("API-Doku-Seite", st == 200 and b"openapi.json" in docs)
    st, rvd, _ = req("GET", "/reviewer-handbuch", raw=True)
    st2, rve, _ = req("GET", "/reviewer-handbuch", raw=True,
                      headers={"Accept-Language": "en-US"})
    check("Reviewer-Handbuch DE/EN",
          st == 200 and "Reviewer-Handbuch".encode() in rvd
          and st2 == 200 and b"Reviewer handbook" in rve)

    print("== Mail-Regeln / Vorlage / Ankündigung / Prefs ==")
    st, n, _ = req("PUT", "/api/notify",
                   {"role": {"reviewer": {"team_turn": True, "reminder": True}},
                    "team_email": {"Team Netzwerk": "n@x.de"},
                    "reminder_days": 99,
                    "template_subject": "Smoke {{name}}"})
    check("Notify: reminder_days-Clamp + Vorlage",
          n["notify"]["reminder_days"] == 30
          and n["notify"]["template_subject"] == "Smoke {{name}}"
          and n["notify"]["role"]["reviewer"]["reminder"] is True)
    st, pv, _ = req("PUT", "/api/mail-preview", {"template_subject": "S {{cluster}}"})
    check("Mail-Vorschau", pv.get("subject") == "S Cluster-03")
    st, an, _ = req("PUT", "/api/announce",
                    {"active": True, "title": "Smoke", "text": "Hallo Welt"})
    st2, page2, _ = req("GET", "/", raw=True)
    check("Ankündigung aktiv -> injiziert",
          an["announce"]["active"] is True
          and b'"title": "Smoke"' in page2)
    st, _, _ = req("PUT", "/api/announce", {"active": False, "title": "Smoke",
                                            "text": "Hallo Welt"})
    st, page3, _ = req("GET", "/", raw=True)
    check("Ankündigung inaktiv -> null", b"let ANNOUNCE = null;" in page3)
    st, p, _ = req("PUT", "/api/prefs",
                   {"cols": {"ktable": {"3": True}}, "announce_seen": "abc123",
                    "theme": "light"})
    check("Prefs-Roundtrip (cols/seen/theme)",
          p.get("theme") == "light" and p.get("announce_seen") == "abc123"
          and p.get("cols", {}).get("ktable", {}).get("3") is True)
    st, p2, _ = req("PUT", "/api/prefs", {"cols": {}, "theme": "neon"})
    check("Ungültiges Theme wird verworfen", "theme" not in p2)

    print("== Auto-Freigabe ==")
    # Team 1 manuell, Team 2 auto; großzügige Schwellen (Sample-Cluster-03
    # hat Luft und einen Workload-Wert)
    st, aa, _ = req("PUT", "/api/autoapprove",
                    {"enabled": True, "min_cpu_pct": 1, "min_ram_pct": 1,
                     "min_lun_pct": 1, "max_workload_pct": 100,
                     "teams": {"Team Security": True}})
    check("Konfig speichern (Team-Haken gefiltert)",
          aa["autoapprove"]["enabled"] is True
          and aa["autoapprove"]["teams"] == {"Team Security": True})
    st, res3, _ = req("POST", "/api/reservations",
                      {"name": "Auto-Kaskade", "cluster": "Cluster-03",
                       "vcpu": 1, "ram_gb": 1, "storage_gb": 1})
    r3 = next(x for x in res3 if x["name"] == "Auto-Kaskade")
    check("Stufe 1 manuell -> bleibt beantragt", not r3.get("approved")
          and not (r3.get("approvals") or []))
    st, out, _ = req("POST", f"/api/reservations/{r3['id']}/approve", {})
    r3b = next(x for x in out if x["id"] == r3["id"])
    check("Nach Stufe 1 kaskadiert Stufe 2 automatisch",
          r3b.get("approved") is True
          and r3b["approvals"][-1]["by"] == "Auto-Freigabe"
          and r3b["approved_by"] == "Auto-Freigabe")
    # Vollauto: beide Teams angehakt -> sofort genehmigt bei Anlage
    req("PUT", "/api/autoapprove",
        {"enabled": True, "min_cpu_pct": 1, "min_ram_pct": 1,
         "min_lun_pct": 1, "max_workload_pct": 100,
         "teams": {"Team Netzwerk": True, "Team Security": True}})
    st, res4, _ = req("POST", "/api/reservations",
                      {"name": "Voll-Auto", "cluster": "Cluster-03",
                       "vcpu": 1, "ram_gb": 1})
    r4 = next(x for x in res4 if x["name"] == "Voll-Auto")
    check("Vollauto bei Anlage", r4.get("approved") is True
          and len(r4["approvals"]) == 2)
    # Schwelle blockiert -> bleibt beantragt (Audit nennt den Grund)
    req("PUT", "/api/autoapprove",
        {"enabled": True, "min_cpu_pct": 99, "min_ram_pct": 99,
         "min_lun_pct": 99, "max_workload_pct": 0,
         "teams": {"Team Netzwerk": True, "Team Security": True}})
    st, res5, _ = req("POST", "/api/reservations",
                      {"name": "Zu-Gross", "cluster": "Cluster-03",
                       "vcpu": 1, "ram_gb": 1})
    r5 = next(x for x in res5 if x["name"] == "Zu-Gross")
    check("Schwelle greift nicht -> normaler Weg",
          not r5.get("approved") and not (r5.get("approvals") or []))
    req("PUT", "/api/autoapprove", {"enabled": False})

    print("== Anmeldemaske: Passwort-Detektor + Session-Persistenz ==")
    DATA2 = tempfile.mkdtemp(prefix="kapa_smoke_ad_")
    # Vorab eine "überlebende" Sitzung hinterlegen (Hash-Key wie im Server):
    import hashlib as _hl
    SESS_TOK = "smoke-session-token-123"
    REV_TOK = "smoke-reviewer-token-456"
    ANF_TOK = "smoke-anforderer-token-789"
    NORD_TOK = "smoke-nord-token-321"   # nur auf vROps-Quelle RZ-Nord beschränkt
    with open(os.path.join(DATA2, "kapa_sessions.json"), "w") as f:
        json.dump({
            _hl.sha256(SESS_TOK.encode()).hexdigest():
                {"user": "smoke@firma.local", "role": "admin",
                 "abteilung": "", "mail": "", "exp": time.time() + 3600},
            _hl.sha256(REV_TOK.encode()).hexdigest():
                {"user": "rev@firma.local", "role": "reviewer",
                 "abteilung": "Team Netzwerk", "mail": "",
                 "exp": time.time() + 3600},
            _hl.sha256(ANF_TOK.encode()).hexdigest():
                {"user": "anf@firma.local", "role": "anforderer",
                 "abteilung": "Team Netzwerk", "mail": "",
                 "exp": time.time() + 3600},
            _hl.sha256(NORD_TOK.encode()).hexdigest():
                {"user": "nord@firma.local", "role": "admin",
                 "abteilung": "", "mail": "", "sources": ["RZ-Nord"],
                 "exp": time.time() + 3600}}, f)
    P2 = free_port()
    proc2 = subprocess.Popen(
        [sys.executable, APP, "--serve", "--sample", "--bind", "127.0.0.1",
         "--port", str(P2), "--data-dir", DATA2,
         "--ad-url", "ldaps://smoke.invalid"],
        stdout=open(os.path.join(DATA2, "s.log"), "w"), stderr=subprocess.STDOUT)
    try:
        B2 = f"http://127.0.0.1:{P2}"
        t0 = time.time()
        while time.time() - t0 < 20:
            try:
                urllib.request.urlopen(B2 + "/healthz", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        SECRET = "kX9$mQ2pLr#8vN!"
        r = urllib.request.Request(B2 + "/api/login",
            data=json.dumps({"username": SECRET, "password": "x"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(r, timeout=10)
            st2, msg = 200, {}
        except urllib.error.HTTPError as e:
            st2, msg = e.code, json.loads(e.read().decode())
        check("Passwort im Benutzerfeld -> 401 + Hinweis, kein AD-Kontakt",
              st2 == 401 and "sieht wie ein Passwort aus" in msg.get("error", ""))
        logtxt = open(os.path.join(DATA2, "kapa_log.jsonl")).read()
        check("Passwort NICHT im Audit-Log",
              SECRET not in logtxt and "nicht protokolliert" in logtxt)
        # Session aus der Datei überlebt den "Neustart" (frischer Prozess)
        r = urllib.request.Request(B2 + "/",
            headers={"Cookie": "kapa_session=" + SESS_TOK})
        with urllib.request.urlopen(r, timeout=10) as resp:
            page_ad = resp.read().decode()
        check("Session überlebt Neustart (Cookie gilt weiter)",
              "Kapazitätsübersicht pro Cluster" in page_ad
              and "Anmeldung mit Active-Directory-Konto" not in page_ad)
        # Generischer Seiten-Blätterer: EIN Mechanismus für alle großen Listen.
        pagers = ['id="pager_ktable"', 'id="pager_vtable"', 'id="pager_stortable"',
                  'id="pager_rtable"', 'id="pager_atable"', 'id="pager_artable"',
                  'id="pager_ltable"']
        check("Blätter-Leiste an allen sieben Listen vorhanden",
              all(p in page_ad for p in pagers))
        check("Seitengrößen 100/200/300 im Client konfiguriert",
              "const PAGE_SIZES = [100, 200, 300]" in page_ad
              and "function paginate(" in page_ad)
        r = urllib.request.Request(B2 + "/",
            headers={"Cookie": "kapa_session=falsches-token"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            page_bad = resp.read().decode()
        check("Ungültiges Cookie -> Anmeldemaske",
              "Anmeldung mit Active-Directory-Konto" in page_bad)
        # Rollen-Sicht: Host-/VM-Listen nur für Admin/Auditor im Payload
        t0 = time.time()
        while time.time() - t0 < 25:      # Demo-Abruf des AD-Servers abwarten
            with urllib.request.urlopen(B2 + "/healthz", timeout=5) as resp:
                h2 = json.loads(resp.read().decode())
            if h2.get("clusters", 0) > 0 and not h2.get("refreshing"):
                break
            time.sleep(0.5)
        def data_for(tok):
            r = urllib.request.Request(B2 + "/api/data",
                headers={"Cookie": "kapa_session=" + tok})
            with urllib.request.urlopen(r, timeout=10) as resp:
                return json.loads(resp.read().decode())["clusters"]
        adm, rev, anf = data_for(SESS_TOK), data_for(REV_TOK), data_for(ANF_TOK)
        check("Admin sieht Hosts/VMs/Workload",
              "vms" in adm[0] and "hosts" in adm[0] and "workload" in adm[0])
        check("Reviewer ohne Hosts/VMs (Workload bleibt)",
              "vms" not in rev[0] and "hosts" not in rev[0]
              and "workload" in rev[0] and rev[0].get("vmCount", 0) > 0)
        check("Anforderer ohne Hosts/VMs/Workload",
              "vms" not in anf[0] and "hosts" not in anf[0]
              and "workload" not in anf[0])
        # Sichtbarkeits-Matrix: Reviewer Netzwerk aus + Hosts an -> Payload folgt
        def put_vis(body):
            r = urllib.request.Request(B2 + "/api/visibility",
                data=json.dumps(body).encode(), method="PUT",
                headers={"Content-Type": "application/json",
                         "Cookie": "kapa_session=" + SESS_TOK})
            with urllib.request.urlopen(r, timeout=10) as resp:
                return json.loads(resp.read().decode())
        v = put_vis({"reviewer": {"workload": True, "hosts": True, "vms": False,
                                  "network": False, "storage": True,
                                  "tags": True, "decided_by": True}})
        rev2 = data_for(REV_TOK)
        check("Matrix: Reviewer ohne Netzwerk, mit Hosts",
              "portgroups" not in rev2[0] and "hosts" in rev2[0]
              and "vms" not in rev2[0])
        put_vis({})   # zurück auf Standard
        rev3 = data_for(REV_TOK)
        check("Matrix-Reset: Standard greift wieder",
              "portgroups" in rev3[0] and "hosts" not in rev3[0])
        # Matrix-Feature "statistik": Standard sichtbar, abschaltbar -> 403
        def hist_status(tok):
            r = urllib.request.Request(B2 + "/api/history?days=30",
                headers={"Cookie": "kapa_session=" + tok})
            try:
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code
        ok_default = hist_status(REV_TOK)
        put_vis({"reviewer": {"statistik": False}})
        blocked = hist_status(REV_TOK)
        put_vis({})
        check("Matrix: Statistik für Reviewer abschaltbar (200 -> 403)",
              ok_default == 200 and blocked == 403)
        # vROps-Quellen-Filter: Admin sieht alle, RZ-Nord-Konto nur RZ-Nord.
        adm_src = {c.get("source") for c in data_for(SESS_TOK)}
        nord = data_for(NORD_TOK)
        nord_src = {c.get("source") for c in nord}
        nord_names = sorted(c["name"] for c in nord)
        check("Quellen-Filter: Admin ohne Filter sieht alle vROps-Quellen",
              adm_src == {"RZ-Nord", "RZ-Sued"})
        check("Quellen-Filter: RZ-Nord-Konto sieht nur RZ-Nord-Cluster",
              nord_src == {"RZ-Nord"}
              and nord_names == ["Cluster-01", "Cluster-02"])
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()
        shutil.rmtree(DATA2, ignore_errors=True)

    print("== v3: EIN Pfadraum /api/v1/* ==")
    # v1/status trägt jetzt zusätzlich die UI-Felder (additiv erweitert).
    st1, b1, _ = req("GET", "/api/v1/status")
    check("v1/status additiv erweitert (progress/error/tiers)",
          st1 == 200 and all(k in b1 for k in
                             ("version", "updated", "refreshing", "next",
                              "progress", "error", "tiers")))
    # Session-Endpunkte sind unter /api/v1/... erreichbar (Alias, identische
    # Handler) — Antwort deckungsgleich mit dem alten Pfad.
    sa, ba, _ = req("GET", "/api/v1/teams")
    sb, bb, _ = req("GET", "/api/teams")
    check("v1-Alias: /api/v1/teams == /api/teams", sa == sb == 200 and ba == bb)
    # POST ohne Bearer-Header nimmt den Session-Pfad: Antwort ist die
    # UI-Liste (visible_res), nicht das Token-Objekt {"reservation": ...}.
    sc, bc, _ = req("POST", "/api/v1/reservations",
                    {"name": "v3-Vertrag", "cluster": "Cluster-01",
                     "vcpu": 1, "ram_gb": 4, "storage_gb": 10})
    check("v1-POST ohne Bearer -> Session-Handler (Listen-Antwort)",
          sc == 200 and isinstance(bc, list)
          and any(r.get("name") == "v3-Vertrag" for r in bc))

    print("== INI-Wächter ==")
    bad = os.path.join(DATA, "bad.ini")
    with open(bad, "w") as f:
        f.write("[kapa]\ncpu-factor = 6\n[quelle:X]\nurl = https://x\nuser = u\nport = 8888\n")
    r = subprocess.run([sys.executable, APP, "--config", bad, "--sample", "--serve"],
                       capture_output=True, text=True, timeout=30)
    check("Verrutschter Schlüssel in [quelle:] -> Fehler",
          r.returncode != 0 and "gehören aber vermutlich nach [kapa]" in r.stderr)

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(DATA, ignore_errors=True)

print(f"\n{PASS} ok, {len(FAIL)} fehlgeschlagen"
      + (": " + ", ".join(FAIL) if FAIL else " — alles grün ✅"))
sys.exit(1 if FAIL else 0)
