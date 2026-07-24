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

VERSION = "2.29.0"

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

# ---------------------------------------------------------------- Suite API --

class AriaOps:
    def __init__(self, base_url, username, password, auth_source="local",
                 verify_tls=True, proxy=""):
        self.base = base_url.rstrip("/") + "/suite-api/api"
        self.token = None
        self.ctx = None
        if not verify_tls:
            self.ctx = ssl.create_default_context()
            self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        # Optionaler HTTP(S)-Proxy für abgesicherte Umgebungen: alle Aria-Aufrufe
        # laufen dann über einen build_opener mit ProxyHandler (statt urlopen).
        self.opener = None
        if proxy:
            handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
            if self.ctx is not None:
                handlers.append(urllib.request.HTTPSHandler(context=self.ctx))
            self.opener = urllib.request.build_opener(*handlers)
        self._login(username, password, auth_source)

    def _open(self, req, timeout=120):
        if self.opener is not None:
            return self.opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, context=self.ctx, timeout=timeout)  # nosec B310 – URL stammt aus der Admin-Konfiguration (vROps, https), nicht aus Nutzereingaben

    def _request(self, method, path, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "OpsToken " + self.token)
        try:
            with self._open(req, timeout=120) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"HTTP {e.code} bei {method} {path}: {detail}") from None

    def _login(self, username, password, auth_source):
        body = {"username": username, "password": password}
        if auth_source and auth_source != "local":
            body["authSource"] = auth_source
        resp = self._request("POST", "/auth/token/acquire", body)
        self.token = resp["token"]

    def resources(self, resource_kind, adapter_kind="VMWARE"):
        """Alle Ressourcen eines Typs (paginiert)."""
        out, page = [], 0
        while True:
            resp = self._request("GET", "/resources", params={
                "adapterKind": adapter_kind, "resourceKind": resource_kind,
                "pageSize": 1000, "page": page})
            items = resp.get("resourceList", [])
            out.extend(items)
            total = resp.get("pageInfo", {}).get("totalCount", len(out))
            page += 1
            if len(out) >= total or not items:
                return out

    def latest_stats(self, resource_ids, stat_keys, chunk=500, progress=None):
        """Neueste Metrikwerte: {resourceId: {statKey: wert}} — Bulk-POST, GET-Fallback."""
        result = {}
        post_ok = True
        for i in range(0, len(resource_ids), chunk):
            ids = resource_ids[i:i + chunk]
            resp = None
            if post_ok:
                try:
                    resp = self._request("POST", "/resources/stats/latest/query",
                                         {"resourceId": ids, "statKey": stat_keys})
                except RuntimeError:
                    post_ok = False  # ältere Version -> GET-Fallback
            if resp is None:
                resp = self._stats_get(ids, stat_keys)
            for entry in resp.get("values", []):
                rid = entry.get("resourceId")
                stats = {}
                for s in entry.get("stat-list", {}).get("stat", []):
                    vals = s.get("data", [])
                    if vals:
                        stats[s.get("statKey", {}).get("key")] = vals[-1]
                result[rid] = stats
            if progress:
                progress(f"{min(i + chunk, len(resource_ids))} / {len(resource_ids)}")
        return result

    def _stats_get(self, ids, stat_keys, chunk=25):
        merged = {"values": []}
        for i in range(0, len(ids), chunk):
            params = [("resourceId", rid) for rid in ids[i:i + chunk]] + \
                     [("statKey", k) for k in stat_keys]
            url = self.base + "/resources/stats/latest?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            req.add_header("Authorization", "OpsToken " + self.token)
            with self._open(req, timeout=120) as r:
                merged["values"].extend(json.loads(r.read().decode()).get("values", []))
        return merged

    def properties(self, resource_ids, property_keys, chunk=500, progress=None):
        """Eigenschaften: {resourceId: {propertyKey: wert}} — bulk mit Fallback."""
        result = {}
        try:
            for i in range(0, len(resource_ids), chunk):
                resp = self._request("POST", "/resources/properties/latest/query",
                                     {"resourceIds": resource_ids[i:i + chunk],
                                      "propertyKeys": property_keys})
                for entry in resp.get("values", []):
                    rid = entry.get("resourceId")
                    props = {}
                    for p in entry.get("property-contents", {}).get("property-content", []):
                        vals = p.get("values") or p.get("data") or []
                        if vals:
                            props[p.get("statKey")] = vals[-1]
                    result[rid] = props
                if progress:
                    progress(f"{min(i + chunk, len(resource_ids))} / {len(resource_ids)}")
            return result
        except RuntimeError:
            pass  # ältere Version -> einzeln
        for rid in resource_ids:
            resp = self._request("GET", f"/resources/{rid}/properties")
            props = {}
            for p in resp.get("property", []):
                if p.get("name") in property_keys:
                    props[p["name"]] = p.get("value")
            result[rid] = props
        return result

    def statkeys(self, resource_id):
        """Alle verfügbaren Metrik-Schlüssel einer Ressource als Liste von
        Strings. In vROps taucht die LUN-/Device-NAA nicht als Property, sondern
        als Metrik-INSTANZ im Schlüssel auf (z. B. 'Devices|naa.6000…|…') — die
        NAA lässt sich dann direkt aus dem Schlüsselnamen lesen."""
        try:
            resp = self._request("GET", f"/resources/{resource_id}/statkeys")
        except RuntimeError:
            return []
        out = []
        for s in resp.get("stat-key", []) or resp.get("statKey", []):
            k = s.get("key") if isinstance(s, dict) else s
            if k:
                out.append(k)
        return out

    def related(self, resource_id, kinds=None, rel="ALL"):
        """Identifier der verwandten Ressourcen (optional gefiltert nach
        resourceKind, z. B. {'HostSystem'})."""
        resp = self._request("GET", f"/resources/{resource_id}/relationships",
                             params={"relationshipType": rel, "pageSize": 2000})
        out = []
        for r in resp.get("resourceList", []):
            kind = r.get("resourceKey", {}).get("resourceKindKey")
            if kinds is None or kind in kinds:
                out.append(r.get("identifier"))
        return out

    def all_properties(self, resource_id):
        """ALLE Eigenschaften einer Ressource als {key: value}. vSphere-Tags
        liefert vROps als Eigenschaften (nicht über /tags)."""
        resp = self._request("GET", f"/resources/{resource_id}/properties")
        out = {}
        for p in resp.get("property", []):
            name = p.get("name")
            if name:
                out[name] = p.get("value")
        return out

    def resources_by_tag(self, category, name, resource_kind=None,
                         adapter_kind="VMWARE"):
        """Ressourcen mit dem Tag category:name (paginiert). Best effort."""
        body = {"resourceTag": [{"category": category, "name": name}]}
        if resource_kind:
            body["resourceKind"] = [resource_kind]
        if adapter_kind:
            body["adapterKind"] = [adapter_kind]
        out, page = [], 0
        while True:
            resp = self._request("POST", "/resources/query", body=body,
                                 params={"pageSize": 1000, "page": page})
            items = resp.get("resourceList", [])
            out.extend(items)
            total = resp.get("pageInfo", {}).get("totalCount", len(out))
            page += 1
            if len(out) >= total or not items:
                return out

# -------------------------------------------------- Active Directory (LDAP) --

def ldap_bind(url, username, password, timeout=10, insecure=False):
    """Minimaler LDAP Simple Bind (RFC 4511) nur mit der Standardbibliothek.

    Gibt True zurück, wenn sich der Benutzer (UPN, z. B. user@firma.local)
    mit dem Passwort am AD anmelden kann. ldaps:// wird empfohlen; bei
    ldap:// geht das Passwort unverschlüsselt über das Netz."""
    import socket
    if not password:
        return False  # leeres Passwort wäre ein anonymer Bind (immer "Erfolg")
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host = u.hostname
    port = u.port or (636 if u.scheme == "ldaps" else 389)

    def ber_len(n):
        if n < 0x80:
            return bytes([n])
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(b)]) + b

    def tlv(tag, payload):
        return bytes([tag]) + ber_len(len(payload)) + payload

    # LDAPMessage { messageID=1, BindRequest { version=3, name, simple-Passwort } }
    bind_req = tlv(0x60, tlv(0x02, b"\x03")
                   + tlv(0x04, username.encode())
                   + tlv(0x80, password.encode()))
    msg = tlv(0x30, tlv(0x02, b"\x01") + bind_req)

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.sendall(msg)
        data = b""
        while len(data) < 12:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()

    # BindResponse suchen (Tag 0x61), erstes Element darin ist der resultCode
    i = data.find(b"\x61")
    if i < 0 or i + 2 >= len(data):
        raise RuntimeError("Unerwartete LDAP-Antwort")
    j = i + 1
    if data[j] & 0x80:          # lange Längenform überspringen
        j += (data[j] & 0x7F)
    j += 1
    if data[j] != 0x0A:         # ENUMERATED resultCode
        raise RuntimeError("Unerwartete LDAP-Antwort")
    result_code = data[j + 2]
    return result_code == 0


def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, payload):
    return bytes([tag]) + _ber_len(len(payload)) + payload


def _ber_int(n):
    """Nicht-negativen INTEGER minimal und positiv (kein gesetztes Vorzeichenbit)
    als BER-Wert kodieren."""
    if n <= 0:
        return b"\x00"
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    if b[0] & 0x80:                     # führendes Bit gesetzt -> sonst negativ
        b = b"\x00" + b
    return b


def _read_tlv(sock, buf):
    """Ein vollständiges BER-Element aus dem Socket lesen. Gibt (tag, value,
    restpuffer) zurück."""
    while len(buf) < 2:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("LDAP-Verbindung vorzeitig beendet")
        buf += chunk
    tag = buf[0]
    lb = buf[1]
    idx = 2
    if lb & 0x80:
        num = lb & 0x7F
        while len(buf) < 2 + num:
            buf += sock.recv(4096)
        length = int.from_bytes(buf[2:2 + num], "big")
        idx = 2 + num
    else:
        length = lb
    while len(buf) < idx + length:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("LDAP-Verbindung vorzeitig beendet")
        buf += chunk
    return tag, buf[idx:idx + length], buf[idx + length:]


def _iter_tlv(data):
    """Untergeordnete BER-Elemente einer SEQUENCE/SET durchlaufen."""
    i = 0
    while i + 1 < len(data):
        tag = data[i]
        lb = data[i + 1]
        i += 2
        if lb & 0x80:
            num = lb & 0x7F
            length = int.from_bytes(data[i:i + num], "big")
            i += num
        else:
            length = lb
        yield tag, data[i:i + length]
        i += length


def _dn_to_cn(dn):
    """CN aus einem DN ziehen: 'CN=KapaAdmins,OU=..' -> 'KapaAdmins'."""
    first = dn.split(",")[0].strip()
    if first[:3].upper() == "CN=":
        return first[3:]
    return first


def ldap_member_of(url, bind_dn, bind_pw, base_dn, user_upn,
                   timeout=10, insecure=False):
    """Über ein Service-Konto die AD-Gruppen (CNs) des Benutzers ermitteln.
    Bindet als bind_dn, sucht (userPrincipalName=user_upn) und liest memberOf.
    Gibt die Liste der Gruppen-CNs zurück (leer bei Fehler/keine Gruppen)."""
    import socket
    if not (bind_dn and bind_pw and base_dn and user_upn):
        return []
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        # 1) Bind als Service-Konto
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)       # LDAPMessage (BindResponse)
        # resultCode im BindResponse prüfen
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:                          # BindResponse
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            return []
        # 2) SearchRequest (messageID 2)
        filt = _tlv(0xA3, _tlv(0x04, b"userPrincipalName")
                    + _tlv(0x04, user_upn.encode()))
        attrs = _tlv(0x30, _tlv(0x04, b"memberOf"))
        search = _tlv(0x63,
                      _tlv(0x04, base_dn.encode())   # baseObject
                      + _tlv(0x0A, b"\x02")          # scope: subtree
                      + _tlv(0x0A, b"\x00")          # derefAliases: never
                      + _tlv(0x02, b"\x00")          # sizeLimit: 0
                      + _tlv(0x02, bytes([timeLimit := 30]))  # timeLimit
                      + _tlv(0x01, b"\x00")          # typesOnly: false
                      + filt + attrs)
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x02") + search))
        # 3) Antworten lesen bis SearchResultDone (0x65)
        cns = []
        for _ in range(1000):
            tag, val, buf = _read_tlv(sock, buf)    # LDAPMessage
            op = None
            for t, v in _iter_tlv(val):
                if t in (0x64, 0x65):
                    op = (t, v)
                    break
            if not op:
                continue
            if op[0] == 0x65:                        # SearchResultDone
                break
            # SearchResultEntry: objectName, PartialAttributeList
            parts = list(_iter_tlv(op[1]))
            for t, v in parts:
                if t == 0x30:                        # PartialAttributeList
                    for _t, attr in _iter_tlv(v):    # je Attribut
                        sub = list(_iter_tlv(attr))
                        if not sub:
                            continue
                        name = sub[0][1].decode(errors="replace")
                        if name.lower() != "memberof":
                            continue
                        for st, sv in sub[1:]:
                            if st == 0x31:            # SET OF values
                                for vt, vv in _iter_tlv(sv):
                                    cns.append(_dn_to_cn(vv.decode(errors="replace")))
        return cns
    finally:
        sock.close()


def ldap_user_attr(url, bind_dn, bind_pw, base_dn, user_upn, attr,
                   timeout=10, insecure=False):
    """Ein einzelnes AD-Attribut eines Benutzers über das Service-Konto lesen
    (z. B. 'mail', 'proxyAddresses'). Bindet als Service-Konto, sucht
    (userPrincipalName=user_upn) und gibt den ersten Attributwert als String
    zurück ('' bei Fehler/kein Wert)."""
    import socket
    if not (bind_dn and bind_pw and base_dn and user_upn and attr):
        return ""
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            return ""
        filt = _tlv(0xA3, _tlv(0x04, b"userPrincipalName")
                    + _tlv(0x04, user_upn.encode()))
        attrs = _tlv(0x30, _tlv(0x04, attr.encode()))
        search = _tlv(0x63,
                      _tlv(0x04, base_dn.encode())
                      + _tlv(0x0A, b"\x02") + _tlv(0x0A, b"\x00")
                      + _tlv(0x02, b"\x00") + _tlv(0x02, bytes([30]))
                      + _tlv(0x01, b"\x00") + filt + attrs)
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x02") + search))
        for _ in range(1000):
            tag, val, buf = _read_tlv(sock, buf)
            op = None
            for t, v in _iter_tlv(val):
                if t in (0x64, 0x65):
                    op = (t, v)
                    break
            if not op:
                continue
            if op[0] == 0x65:                        # SearchResultDone
                break
            for t, v in _iter_tlv(op[1]):
                if t == 0x30:                        # PartialAttributeList
                    for _t, a in _iter_tlv(v):
                        sub = list(_iter_tlv(a))
                        if not sub:
                            continue
                        if sub[0][1].decode(errors="replace").lower() != attr.lower():
                            continue
                        for st, sv in sub[1:]:
                            if st == 0x31:            # SET OF values
                                for vt, vv in _iter_tlv(sv):
                                    val = vv.decode(errors="replace").strip()
                                    if val:
                                        return val
        return ""
    finally:
        sock.close()


def ldap_group_members(url, bind_dn, bind_pw, base_dn, group_cn,
                       timeout=10, insecure=False, limit=500):
    """Direkte Benutzer-Mitglieder einer AD-Gruppe über das Service-Konto lesen.
    Bindet als Service-Konto, findet die Gruppe per (cn=group_cn) und sucht dann
    alle Benutzer mit (&(objectCategory=person)(memberOf=<GruppenDN>)) — eine
    einzige Suche, unabhängig von der Mitgliederzahl (kein AD-Range-Limit).
    Gibt {ok, error, group_dn, members:[{name,upn,sam}], truncated} zurück.
    Verschachtelte Gruppen werden NICHT aufgelöst (nur direkte Mitglieder)."""
    import socket
    res = {"ok": False, "error": "", "group_dn": "", "members": [], "truncated": False}
    if not (bind_dn and bind_pw and base_dn and group_cn):
        res["error"] = "AD ist nicht vollständig konfiguriert (Service-Konto/Base-DN)."
        return res
    u = urllib.parse.urlparse(url if "//" in url else "ldap://" + url)
    host, port = u.hostname, u.port or (636 if u.scheme == "ldaps" else 389)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        res["error"] = f"Keine Verbindung zum AD ({host}:{port}): {e}"
        return res
    try:
        if u.scheme == "ldaps":
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        # 1) Bind als Service-Konto
        bind_req = _tlv(0x60, _tlv(0x02, b"\x03")
                        + _tlv(0x04, bind_dn.encode())
                        + _tlv(0x80, bind_pw.encode()))
        sock.sendall(_tlv(0x30, _tlv(0x02, b"\x01") + bind_req))
        buf = b""
        tag, val, buf = _read_tlv(sock, buf)
        ok = False
        for t, v in _iter_tlv(val):
            if t == 0x61:
                for t2, v2 in _iter_tlv(v):
                    if t2 == 0x0A:
                        ok = (v2[:1] == b"\x00")
                        break
                break
        if not ok:
            res["error"] = "Bind mit dem Service-Konto fehlgeschlagen (Zugangsdaten?)."
            return res

        def _search(msgid, filt, want_attrs, cap):
            """Eine Suche ausführen; liefert (entries, truncated). Jede entry =
            (objectName, {attr_lower: [werte]})."""
            attrs = _tlv(0x30, b"".join(_tlv(0x04, a.encode()) for a in want_attrs))
            search = _tlv(0x63,
                          _tlv(0x04, base_dn.encode())
                          + _tlv(0x0A, b"\x02")            # scope: subtree
                          + _tlv(0x0A, b"\x00")            # derefAliases: never
                          + _tlv(0x02, _ber_int(cap))    # sizeLimit
                          + _tlv(0x02, bytes([30]))        # timeLimit
                          + _tlv(0x01, b"\x00") + filt + attrs)
            sock.sendall(_tlv(0x30, _tlv(0x02, bytes([msgid])) + search))
            entries, trunc = [], False
            for _ in range(5000):
                _tag, _val, buf2 = _read_tlv(sock, _search.buf)
                _search.buf = buf2
                op = None
                for t, v in _iter_tlv(_val):
                    if t in (0x64, 0x65):
                        op = (t, v)
                        break
                if not op:
                    continue
                if op[0] == 0x65:                          # SearchResultDone
                    for t, v in _iter_tlv(op[1]):
                        if t == 0x0A and v[:1] == b"\x04":  # resultCode 4 = sizeLimitExceeded
                            trunc = True
                        break
                    break
                # SearchResultEntry
                inner = list(_iter_tlv(op[1]))
                obj_name = ""
                amap = {}
                for t, v in inner:
                    if t == 0x04 and not obj_name:
                        obj_name = v.decode(errors="replace")
                    elif t == 0x30:                        # PartialAttributeList
                        for _t, a in _iter_tlv(v):
                            sub = list(_iter_tlv(a))
                            if not sub:
                                continue
                            nm = sub[0][1].decode(errors="replace").lower()
                            vals = []
                            for st, sv in sub[1:]:
                                if st == 0x31:
                                    for _vt, vv in _iter_tlv(sv):
                                        vals.append(vv.decode(errors="replace"))
                            amap[nm] = vals
                entries.append((obj_name, amap))
            return entries, trunc
        _search.buf = buf

        # 1) Gruppe per CN finden -> DN
        gfilt = _tlv(0xA3, _tlv(0x04, b"cn") + _tlv(0x04, group_cn.encode()))
        gents, _ = _search(2, gfilt, ["cn"], 2)
        gents = [e for e in gents if e[0]]
        if not gents:
            res["error"] = f"Gruppe „{group_cn}“ nicht im AD gefunden (unter {base_dn})."
            return res
        group_dn = gents[0][0]
        res["group_dn"] = group_dn

        # 2) direkte Benutzer-Mitglieder holen
        mfilt = _tlv(0xA0,
                     _tlv(0xA3, _tlv(0x04, b"objectCategory") + _tlv(0x04, b"person"))
                     + _tlv(0xA3, _tlv(0x04, b"memberOf") + _tlv(0x04, group_dn.encode())))
        ments, trunc = _search(3, mfilt, ["displayName", "userPrincipalName",
                                          "sAMAccountName"], limit)
        members = []
        for obj_name, amap in ments:
            def first(k):
                return (amap.get(k.lower()) or [""])[0]
            members.append({
                "name": first("displayName") or _dn_to_cn(obj_name),
                "upn": first("userPrincipalName"),
                "sam": first("sAMAccountName"),
            })
        members.sort(key=lambda m: (m["name"] or m["upn"] or "").lower())
        res["members"] = members
        res["truncated"] = trunc or len(members) >= limit
        res["ok"] = True
        return res
    except Exception as e:
        res["error"] = f"AD-Fehler: {e}"
        return res
    finally:
        try:
            sock.close()
        except OSError:
            pass

# ----------------------------------------------------------- Datendateien ----

def ensure_dir(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def atomic_write(path, text):
    """Datei atomar schreiben: erst in eine Temp-Datei im selben Verzeichnis,
    dann per os.replace umbenennen. Ein Absturz mitten im Schreiben kann die
    Zieldatei damit nicht mehr beschädigen (kein halb geschriebenes JSON)."""
    ensure_dir(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ----------------------------------------------------------- Datenspeicher ----
# Zwei Backends hinter derselben API:
#   JSON   – je Sammlung eine atomar geschriebene .json-Datei (Standard,
#            menschenlesbar/editierbar).
#   SQLite – eine data/kapa.db (Standardbibliothek, kein Server): kleine
#            Sammlungen als Key-Value-Blobs, Reservierungen als eigene Tabelle
#            mit INKREMENTELLEN Schreibzugriffen (nur die geänderte Zeile).

class JsonStore:
    """Backend: je Sammlung eine JSON-Datei, atomar geschrieben."""
    def __init__(self, paths):
        self.paths = paths            # {name: dateipfad}

    def load(self, name, default):
        p = self.paths[name]
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"{p} unlesbar, nutze Standard: {e}", file=sys.stderr)
        return default

    def save(self, name, obj):
        atomic_write(self.paths[name], _dumps(obj))

    # Reservierungen: JSON kennt keine Einzelzeilen -> ganze Liste atomar sichern
    def res_load(self):
        return self.load("res", [])

    def res_save_all(self, items):
        self.save("res", items)

    def res_put(self, entry, items):    # items = aktuelle In-Memory-Liste
        self.save("res", items)

    def res_delete(self, ids, items):
        self.save("res", items)

    def close(self):
        pass


class SqliteStore:
    """Backend: eine SQLite-Datei. Reservierungen inkrementell, kleine
    Sammlungen als JSON-Blobs in einer kv-Tabelle."""
    def __init__(self, db_path):
        import sqlite3
        ensure_dir(db_path)
        # Eine Verbindung, von mehreren HTTP-Threads genutzt -> eigener Lock,
        # weil execute+commit-Folgen sonst ineinanderlaufen können.
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv"
                        "(name TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS reservations"
                        "(id TEXT PRIMARY KEY, created TEXT, data TEXT NOT NULL)")
        self.db.commit()

    def load(self, name, default):
        with self.lock:
            row = self.db.execute("SELECT data FROM kv WHERE name=?",
                                  (name,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except ValueError:
                pass
        return default

    def save(self, name, obj):
        with self.lock:
            self.db.execute("INSERT INTO kv(name, data) VALUES(?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET data=excluded.data",
                            (name, json.dumps(obj, ensure_ascii=False)))
            self.db.commit()

    def res_load(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM reservations ORDER BY created").fetchall()
        return [json.loads(r[0]) for r in rows]

    def res_save_all(self, items):     # ganzen Bestand ersetzen (Import/Reconcile)
        with self.lock, self.db:
            self.db.execute("DELETE FROM reservations")
            self.db.executemany(
                "INSERT INTO reservations(id, created, data) VALUES(?,?,?)",
                [(e.get("id"), e.get("created", ""), json.dumps(e, ensure_ascii=False))
                 for e in items])

    def res_put(self, entry, items):   # inkrementell: nur diese Zeile (upsert)
        with self.lock:
            self.db.execute(
                "INSERT INTO reservations(id, created, data) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET created=excluded.created, "
                "data=excluded.data",
                (entry.get("id"), entry.get("created", ""),
                 json.dumps(entry, ensure_ascii=False)))
            self.db.commit()

    def res_delete(self, ids, items):  # inkrementell: nur diese Zeilen
        if ids:
            with self.lock:
                self.db.executemany("DELETE FROM reservations WHERE id=?",
                                    [(i,) for i in ids])
                self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


def data_path(path, base="data"):
    """JSON-Datendateien ohne Verzeichnisangabe landen im Datenordner (Standard
    data/, per --data-dir änderbar, z. B. /var/lib/kapa außerhalb des
    Deploy-Verzeichnisses); explizite Pfade (relativ mit Ordner oder absolut)
    bleiben unverändert."""
    if path and not os.path.dirname(path):
        return os.path.join(base, path)
    return path


def migrate_data_files(*paths):
    """Alt-Dateien aus dem Arbeitsverzeichnis in den Datenordner verschieben
    (frühere Versionen legten kapa_*.json direkt neben dem Skript ab)."""
    for p in paths:
        ensure_dir(p)
        old = os.path.basename(p)
        if (not os.path.exists(p) and os.path.exists(old)
                and os.path.abspath(old) != os.path.abspath(p)):
            os.replace(old, p)
            print(f"Datendatei verschoben: {old} -> {p}", file=sys.stderr)

# ---------------------------------------------------------- SFTP-Backup -----

def _ssh_cmd(args, prog):
    """Basis-Kommando + Umgebung für scp/sftp mit Key- oder Passwort-Auth.

    Wichtig für den systemd-Betrieb: der Dienst läuft gehärtet (ProtectHome,
    ProtectSystem=strict), das Home-Verzeichnis ist also nicht nutzbar. Deshalb
    liegt die known_hosts-Datei im beschreibbaren Datenordner und der Key wird
    ausschließlich über --backup-key angegeben (IdentitiesOnly)."""
    known = args.backup_known_hosts or os.path.join(
        os.path.dirname(args.res_file) or ".", "known_hosts")
    base = [prog, "-q", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known}",
            "-o", "ConnectTimeout=15", "-o", f"Port={args.backup_port}"]
    env = os.environ.copy()
    if args.backup_key:
        base += ["-i", args.backup_key, "-o", "IdentitiesOnly=yes",
                 "-o", "BatchMode=yes"]
    elif args.backup_password:
        env["SSHPASS"] = args.backup_password
        base = ["sshpass", "-e"] + base
    else:
        base += ["-o", "BatchMode=yes"]   # ssh-agent / Standard-Keys
    return base, env


def _ssh_run(cmd, env, timeout=180):
    import subprocess  # nosec B404 – Aufruf ist geprüft (Liste, kein shell)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)  # nosec B603 – Argumentliste, kein shell=True; Ziel/Programm aus der Admin-Konfiguration
    except FileNotFoundError:
        prog = cmd[0]
        if prog == "sshpass":
            raise RuntimeError("'sshpass' ist nicht installiert – für Passwort-Backups "
                               "installieren oder besser --backup-key (SSH-Key) nutzen") from None
        raise RuntimeError(f"Programm '{prog}' nicht gefunden (bitte installieren)") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd[0]} nach {timeout}s abgebrochen (Timeout – "
                           "Backup-Ziel/Port erreichbar?)") from None
    if r.returncode != 0:
        # Die konkrete Shell-Meldung (scp/sftp/ssh) durchreichen – meist steht
        # der eigentliche Grund in stderr (sonst stdout als Rückfall).
        err = (r.stderr.decode(errors="replace").strip()
               or r.stdout.decode(errors="replace").strip())
        raise RuntimeError(f"{cmd[0]} Exit-Code {r.returncode}"
                           + (f": {err}" if err else " (keine Meldung)"))
    return r.stdout.decode(errors="replace")


def sftp_backup(args):
    """Datendateien als tar.gz per scp auf das Backupziel kopieren.

    Authentifizierung per SSH-Key (--backup-key, empfohlen) oder Passwort
    (--backup-password bzw. BACKUP_PASSWORD; erfordert installiertes sshpass).
    Gibt den Namen des übertragenen Archivs zurück."""
    import tarfile
    import tempfile
    if not args.backup_target:
        raise RuntimeError("kein --backup-target konfiguriert")
    db = getattr(args, "db_file", "")
    files = [p for p in (args.cache, args.res_file, args.roles_file,
                         args.log_file, args.tokens_file, args.teams_file,
                         args.rolenames_file, args.selector_file, args.notify_file,
                         getattr(args, "prefs_file", ""),
                         getattr(args, "announce_file", ""),
                         getattr(args, "autoapprove_file", ""),
                         getattr(args, "visibility_file", ""),
                         getattr(args, "storagecfg_file", ""),
                         getattr(args, "storagereq_file", ""),
                         getattr(args, "netcfg_file", ""),
                         getattr(args, "import_file", ""),
                         getattr(args, "history_file", ""),
                         getattr(args, "refreshcfg_file", ""),
                         db, db + "-wal", db + "-shm")
             if p and os.path.exists(p)]
    if not files:
        raise RuntimeError("keine Datendateien vorhanden")
    name = "kapa_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".tar.gz"
    tmp = os.path.join(tempfile.gettempdir(), name)
    with tarfile.open(tmp, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=os.path.basename(f))
    cmd, env = _ssh_cmd(args, "scp")
    try:
        _ssh_run(cmd + [tmp, args.backup_target.rstrip("/") + "/" + name], env)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return name


def backup_rotate(args):
    """Backups älter als --backup-keep-days auf dem Ziel löschen (per sftp,
    funktioniert auch auf sftp-only-Servern). Gibt die Anzahl gelöschter
    Archive zurück."""
    import tempfile
    if args.backup_keep_days <= 0:
        return 0
    host, _, rdir = args.backup_target.partition(":")
    rdir = (rdir or ".").rstrip("/") or "/"

    def sftp_batch(commands):
        cmd, env = _ssh_cmd(args, "sftp")
        with tempfile.NamedTemporaryFile("w", suffix=".batch",
                                         delete=False) as b:
            b.write("\n".join(commands) + "\n")
            batch = b.name
        try:
            return _ssh_run(cmd + ["-b", batch, host], env)
        finally:
            try:
                os.remove(batch)
            except OSError:
                pass

    listing = sftp_batch([f"ls -1 {rdir}"])
    cutoff = ((datetime.now() - timedelta(days=args.backup_keep_days))
              .strftime("%Y%m%d_%H%M%S"))
    olds = sorted({m.group(0)
                   for m in re.finditer(r"kapa_backup_(\d{8}_\d{6})\.tar\.gz",
                                        listing)
                   if m.group(1) < cutoff})
    if olds:
        # führendes '-' = Fehler einzelner rm-Befehle ignorieren
        sftp_batch([f"-rm {rdir}/{name}" for name in olds])
    return len(olds)

# ----------------------------------------------------------- Mail-Reports ----

def send_mail(args, subject, body, extra_to=(), to_override=None, html=None):
    """Report-Mail über den konfigurierten SMTP-Server (--smtp-server).

    to_override: exakte Empfängerliste (aus den Mail-Regeln). Ist sie gesetzt,
    wird --smtp-to NICHT zusätzlich angeschrieben. html: optionale HTML-Fassung
    (multipart/alternative; Clients ohne HTML zeigen den Klartext)."""
    import smtplib
    from email.message import EmailMessage
    if not args.smtp_server:
        return
    if to_override is not None:
        seen, to = set(), []
        for t in to_override:
            t = str(t or "").strip()
            if "@" in t and t not in seen:
                seen.add(t)
                to.append(t)
    else:
        to = [t.strip() for t in (args.smtp_to or "").split(",") if t.strip()]
        to += [t for t in extra_to if t and "@" in t and t not in to]
    if not to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = args.smtp_from or "kapa-dashboard@localhost"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    host, _, port = args.smtp_server.partition(":")
    with smtplib.SMTP(host, int(port or 25), timeout=15) as smtp:
        if args.smtp_tls:
            smtp.starttls()
        if args.smtp_user:
            smtp.login(args.smtp_user, args.smtp_password or "")
        smtp.send_message(msg)


def _res_valid_date(r, res_ttl):
    if r.get("created"):
        try:
            d = datetime.fromisoformat(r["created"]) + \
                timedelta(days=(res_ttl - 1 if res_ttl > 0 else 30))
            return d.strftime("%d.%m.%Y")
        except ValueError:
            pass
    return ""


def _res_rows(r, res_ttl):
    """Feldliste (Label, Wert) für die Reservierungs-Mail."""
    return [
        ("ID", r.get("id") or "–"),
        ("Anfrage", r.get("name") or "?"),
        ("Change / Jira", r.get("change") or "–"),
        ("Cluster", r.get("cluster") or "?"),
        ("vCPU", r.get("vcpu", 0)),
        ("RAM", f"{r.get('ram_gb', 0)} GB"),
        ("Storage", f"{r.get('storage_gb') or 0} GB"),
        ("Team", r.get("abteilung") or "–"),
        ("Beantragt", f"von {r.get('von') or '–'} am {r.get('created') or '–'}"),
        ("Gültig bis", _res_valid_date(r, res_ttl) or "–"),
    ]


def reservation_mail_body(r, action, admin, res_ttl):
    """Klartext-Fassung (Fallback für Clients ohne HTML)."""
    approvals = r.get("approvals") or []
    freigaben = ("\n" + "\n".join(
        f"             - {a.get('team') or '?'}: {a.get('by') or '?'} "
        f"am {a.get('on') or '?'}" for a in approvals)) if approvals else " –"
    lines = [f"{label + ':':<14}{value}" for label, value in _res_rows(r, res_ttl)]
    return (f"Kapazitätsreservierung {action}\n\n"
            + "\n".join(lines) + "\n"
            f"{'Freigaben:':<14}{freigaben}\n"
            f"{'Kommentar:':<14}{r.get('comment') or '–'}\n\n"
            f"{action} von {admin} am {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")


# Im Mail-Template (und Betreff) nutzbare Variablen: {{name}} usw.
MAIL_VARS = [
    ("action", "Ereignis (beantragt / genehmigt / abgelehnt / wartet auf Freigabe …)"),
    ("name", "Bezeichnung / Projekt"),
    ("id", "Kapa-ID"),
    ("change", "Change / Jira-Ticket"),
    ("cluster", "Ziel-Cluster"),
    ("source", "vROps-Quelle"),
    ("vcpu", "vCPU-Anzahl"),
    ("ram_gb", "RAM (inkl. „GB“)"),
    ("storage_gb", "Storage (inkl. „GB“)"),
    ("von", "Anforderer"),
    ("team", "Team / Abteilung des Anforderers"),
    ("created", "Gilt ab (Anlagedatum)"),
    ("valid_until", "Gültig bis"),
    ("approvals", "Freigaben (Liste der Team-Freigaben)"),
    ("comment", "letzter Kommentar"),
    ("admin", "ausführende Person"),
    ("date", "Zeitpunkt der Mail"),
    ("current_team", "aktuell zuständiges Team (bei „Team ist dran“)"),
]

DEFAULT_MAIL_SUBJECT = "Kapazitätsreservierung {{action}}: {{name}} ({{cluster}})"

_ML = ("padding:7px 14px 7px 0;color:#6b7280;white-space:nowrap;"
       "border-bottom:1px solid #eef0f3;vertical-align:top")
_MV = "padding:7px 0;border-bottom:1px solid #eef0f3;vertical-align:top"
DEFAULT_MAIL_TEMPLATE = (
    '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
    'Helvetica,Arial,sans-serif;color:#1f2937;font-size:14px;line-height:1.5;max-width:600px">\n'
    '  <div style="font-size:17px;font-weight:600;margin:0 0 2px">Kapazitätsreservierung {{action}}</div>\n'
    '  <div style="color:#6b7280;font-size:13px;margin:0 0 16px">{{name}} &middot; {{cluster}}</div>\n'
    '  <table style="border-collapse:collapse;width:100%;border-top:1px solid #eef0f3">\n'
    f'    <tr><td style="{_ML}">ID</td><td style="{_MV}">{{{{id}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Change / Jira</td><td style="{_MV}">{{{{change}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Cluster</td><td style="{_MV}">{{{{cluster}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">vCPU</td><td style="{_MV}">{{{{vcpu}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">RAM</td><td style="{_MV}">{{{{ram_gb}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Storage</td><td style="{_MV}">{{{{storage_gb}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Team</td><td style="{_MV}">{{{{team}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Beantragt</td><td style="{_MV}">von {{{{von}}}} am {{{{created}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Gültig bis</td><td style="{_MV}">{{{{valid_until}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Freigaben</td><td style="{_MV}">{{{{approvals}}}}</td></tr>\n'
    f'    <tr><td style="{_ML}">Kommentar</td><td style="{_MV}">{{{{comment}}}}</td></tr>\n'
    '  </table>\n'
    '  <div style="color:#9ca3af;font-size:12px;margin-top:16px">{{action}} von {{admin}} am {{date}}</div>\n'
    '  <div style="color:#c3c8d0;font-size:11px;margin-top:4px">VMware Kapazitätsplanung</div>\n'
    '</div>')


def _mail_values(r, action, admin, res_ttl, team=None, html=True):
    """Werte für die Vorlagen-Variablen. html=True escaped für die HTML-Mail,
    html=False für den (plain) Betreff."""
    # html=False landet im Mail-Betreff: CR/LF dort immer zu Leerzeichen falten,
    # sonst lehnt EmailMessage den Header ab und der Versand scheitert still.
    e = _html_escape if html else (
        lambda x: " ".join(str(x if x is not None else "").split()))
    approvals = r.get("approvals") or []
    if html:
        appr = "<br>".join(
            f"{_html_escape(a.get('team') or '?')} · {_html_escape(str(a.get('by') or '?'))} "
            f"am {_html_escape(str(a.get('on') or '?'))}" for a in approvals) or "–"
    else:
        appr = ", ".join(f"{a.get('team') or '?'}: {a.get('by') or '?'}"
                         for a in approvals) or "–"
    return {
        "action": e(action), "id": e(r.get("id") or "–"), "name": e(r.get("name") or "?"),
        "change": e(r.get("change") or "–"), "cluster": e(r.get("cluster") or "?"),
        "source": e(r.get("source") or "–"), "vcpu": e(r.get("vcpu", 0)),
        "ram_gb": e(f"{r.get('ram_gb', 0)} GB"),
        "storage_gb": e(f"{r.get('storage_gb') or 0} GB"),
        "von": e(r.get("von") or "–"), "team": e(r.get("abteilung") or "–"),
        "created": e(r.get("created") or "–"),
        "valid_until": e(_res_valid_date(r, res_ttl) or "–"),
        "approvals": appr, "comment": e(r.get("comment") or "–"),
        "admin": e(admin or "System"),
        "date": e(datetime.now().strftime("%d.%m.%Y %H:%M")),
        "current_team": e(team or "–"),
    }


def render_template(tpl, values):
    """Platzhalter {{var}} in einer Vorlage durch die Werte ersetzen."""
    out = tpl
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def reservation_mail_html(r, action, admin, res_ttl, template=None, team=None):
    """HTML-Mail aus der (ggf. angepassten) Vorlage rendern."""
    return render_template(template or DEFAULT_MAIL_TEMPLATE,
                           _mail_values(r, action, admin, res_ttl, team, html=True))

# ------------------------------------------------------------- Datenabfrage --

def name_of(res):
    return res.get("resourceKey", {}).get("name", "?")


def is_uplink_pg(name, vlan):
    """dvSwitch-Uplink-/Trunk-Portgruppen erkennen (keine echten Netz-VLANs):
    Name enthält 'uplink' ODER die VLAN ist eine breite Trunk-Range (z. B.
    0-4094). Solche Gruppen tragen alle VLANs und gehören nicht in die
    VLAN-Übersicht."""
    if "uplink" in str(name or "").lower():
        return True
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(vlan or ""))
    return bool(m) and int(m.group(2)) - int(m.group(1)) >= 100


def strip_uplinks(clusters, hide=True):
    """Uplink-/Trunk-Portgruppen aus den fertigen Cluster-Dicts entfernen
    (an einer Stelle, damit Serve- und Demo-Modus gleich reagieren)."""
    if not hide:
        return clusters
    for c in clusters:
        pgs = c.get("portgroups")
        if pgs:
            c["portgroups"] = [p for p in pgs
                               if not is_uplink_pg(p.get("name"), p.get("vlan"))]
    return clusters


class _SkipTier(Exception):
    """Intern: Teilbereich per Intervall übersprungen (kein Fehler)."""


def collect(api, cpu_factor, progress=None, failover_hosts=1, exclude_tag="",
            tag_property="", vsan_factor=0.5, tanzu_mhz=2500, vlan_hint=None,
            min_lun_gb=0, exclude_names=None, vsan_overrides=None,
            net_exclude_names=None, net_exclude_vlans=None,
            skip=None, prev=None):
    """skip: Teilbereiche überspringen ("vms" | "network" | "storage") und
    stattdessen die Rohdaten des letzten Laufs (prev, je Cluster) übernehmen —
    Basis der gestaffelten Abruf-Intervalle. Cluster/Hosts/Tags (Skelett)
    werden immer frisch gelesen. Rückgabe: (fertige Cluster, Rohdaten)."""
    skip = set(skip or ())
    prev = prev or {}
    # Detail-Log geht ins stderr (journal); die Oberfläche bekommt nur einen
    # grob geschätzten Prozentwert über report().
    log = lambda m: print(m, file=sys.stderr)

    def report(pct):
        if progress:
            try:
                progress(str(int(pct)) + " %")
            except Exception:
                pass

    report(3)
    log("Lese Cluster ...")
    clusters = api.resources("ClusterComputeResource")
    # Workload-Badge je Cluster (vROps, 0-100 %) – best effort.
    cl_workload = {}
    try:
        cw = api.latest_stats([c["identifier"] for c in clusters], ["badge|workload"])
        for cid, st in cw.items():
            v = st.get("badge|workload")
            if v is not None:
                cl_workload[cid] = round(float(v))
        log(f"Cluster-Workload gelesen: {len(cl_workload)}/{len(clusters)}")
    except Exception as e:
        log(f"Cluster-Workload nicht verfügbar: {e}")
    report(8)
    log(f"Lese ESXi-Hosts ... ({len(clusters)} Cluster gefunden)")
    hosts = api.resources("HostSystem")
    report(15)
    log(f"Lese VMs ... ({len(hosts)} Hosts gefunden)")
    if "vms" in skip:
        vms = []
        log("VMs: übersprungen (Intervall) – Stand vom letzten Abruf")
    else:
        vms = api.resources("VirtualMachine")
        log(f"{len(vms)} VMs gefunden")
    report(22)

    # Systeme mit dem Ausschluss-Tag (z. B. Kapa_Filter:Ja) aus der Auswertung
    # nehmen – best effort, bei Fehler wird nichts ausgeschlossen.
    exclude_vms = set()
    if exclude_tag and ":" in exclude_tag and "vms" not in skip:
        cat, _, val = exclude_tag.partition(":")
        cat, val = cat.strip(), val.strip()
        if cat and val:
            try:
                log(f"Lese Systeme mit Tag {cat}={val} (werden ausgeschlossen) ...")
                tagged = api.resources_by_tag(cat, val, "VirtualMachine")
                exclude_vms = {t["identifier"] for t in tagged}
                log(f"{len(exclude_vms)} Systeme per Tag {cat}={val} ausgeschlossen")
            except Exception as e:
                log(f"Tag-Filter nicht anwendbar: {e}")

    host_ids = [h["identifier"] for h in hosts]
    vm_ids = [v["identifier"] for v in vms]

    report(30)
    log("Lese Host-Metriken (Cores, RAM) ...")
    host_stats = api.latest_stats(host_ids,
        ["cpu|corecount_provisioned", "mem|host_provisioned"],
        progress=lambda m: log(f"Host-Metriken: {m}"))
    log("Lese Host-Zuordnung (Cluster) + FC-HBA-Port-WWN ...")
    # WWPN der FC-HBAs je Host (fürs Storage-Team zur System-Identifikation, falls
    # dort die Hostnamen nicht gepflegt sind). vROps führt sie als Property
    # "storageAdapter:vmhbaN|port_WWN" – je HBA eine eigene Instanz, deren Nummer
    # variiert. Da die Instanznamen nicht vorab bekannt sind, fragen wir einen
    # breiten Adapter-Bereich im SELBEN Bulk-Aufruf ab (nur vorhandene Werte
    # kommen zurück). Wichtig: KEIN Property-Abruf je Host – bei vielen tausend
    # Hosts wäre das viel zu langsam.
    WWPN_KEYS = [f"storageAdapter:vmhba{n}|port_WWN" for n in range(0, 128)]
    host_props = api.properties(host_ids, ["summary|parentCluster"] + WWPN_KEYS,
        progress=lambda m: log(f"Host-Eigenschaften: {m}"))

    # port_WWN (Unterstrich!) je Adapter; Wert z. B. 21:00:34:80:0d:3f:10:7b.
    _PORTWWN_KEY_RX = re.compile(r"port[\s_]*wwn", re.I)
    _WWPN_RX = re.compile(r"(?:[0-9a-fA-F]{2}[:\-]){7}[0-9a-fA-F]{2}"
                          r"|\b0x[0-9a-fA-F]{16}\b|\b[0-9a-fA-F]{16}\b")

    def host_wwpns(props):
        """WWPNs eines Hosts aus den port_WWN-Properties (mehrere HBAs möglich)."""
        out, seen = [], set()
        for k, v in props.items():
            if not v or not _PORTWWN_KEY_RX.search(str(k)):
                continue
            for part in re.split(r"[,;\s]+", str(v)):
                part = part.strip()
                if not part:
                    continue
                m = _WWPN_RX.search(part)
                w = m.group(0) if m else part
                if w.lower() not in seen:
                    seen.add(w.lower())
                    out.append(w)
        return out

    # Diagnose: falls ein FC-Adapter außerhalb des abgefragten Bereichs (0–127)
    # liegt, wird er hier sichtbar (ein Property-Abruf für EINEN Beispiel-Host).
    if hosts:
        try:
            h0 = hosts[0]
            hp0 = api.all_properties(h0["identifier"])
            hits = {k: v for k, v in hp0.items()
                    if re.search(r"port[\s_]*wwn|storageadapter|hba|fibre",
                                 str(k), re.I)}
            log(f"Host-Properties (Beispiel '{name_of(h0)}') – Storage-Adapter/"
                "Port-WWN-Kandidaten: "
                + (", ".join(f"{k}={str(v)[:40]}" for k, v in
                             sorted(hits.items())[:12]) or "keine gefunden"))
        except Exception as e:
            log(f"WWPN-Diagnose übersprungen: {e}")

    report(50)
    # Disk je VM (für die Statistik „VMs werden größer"): der Schlüssel variiert
    # je vROps-Version -> Kandidaten im selben Bulk, erster Treffer gewinnt.
    VM_DISK_KEYS = ["config|hardware|disk_Space", "diskspace|provisioned_space",
                    "diskspace|provisionedSpace", "diskspace|used"]
    if "vms" in skip:
        vm_stats, vm_props = {}, {}
    else:
        log("Lese VM-Metriken (vCPU, RAM) ...")
        vm_stats = api.latest_stats(vm_ids,
            ["config|hardware|num_Cpu", "mem|guest_provisioned"] + VM_DISK_KEYS,
            progress=lambda m: log(f"VM-Metriken: {m}"))
        log("Lese VM-Eigenschaften (Cluster, PowerState) ...")
        vm_props = api.properties(vm_ids,
            ["summary|parentCluster", "summary|runtime|powerState",
             "config|hardware|numCpu", "config|hardware|memoryKB"],
            progress=lambda m: log(f"VM-Eigenschaften: {m}"))

    # Storage-Kapazität aus den Datastores (vSAN + angedockte FC-LUNs).
    # Best effort – schlägt es fehl, bleibt die Anzeige leer, der Rest läuft weiter.
    # Der Typ (VMFS/NFS/vSAN) kommt als Eigenschaft; die Schlüssel unterscheiden
    # sich je nach vROps-Version, deshalb mehrere Kandidaten in einem Bulk-Aufruf.
    DS_TYPE_KEYS = ["summary|type", "summary|datastore_type",
                    "config|fileSystemType", "summary|fileSystemType"]
    # NAA-Kennung des Backing-Devices (FC/iSCSI-LUNs, z. B. naa.6000…) — die
    # Schlüssel variieren je vROps-Version, deshalb Kandidaten im selben
    # Bulk-Aufruf. Rein informativ (Brücke zum Storage-Team/Array).
    DS_NAA_KEYS = ["summary|datastore|diskName", "config|extent|diskName",
                   "summary|diskName", "info|extent|diskName",
                   "config|vmfs|extent|diskName", "summary|canonicalName"]
    datastores, ds_stats, ds_props = [], {}, {}
    if "storage" in skip:
        log("Storage: übersprungen (Intervall) – Stand vom letzten Abruf")
    try:
        if "storage" in skip:
            raise _SkipTier()
        report(68)
        log("Lese Datastores (Storage-Kapazität) ...")
        datastores = api.resources("Datastore")
        ds_ids = [d["identifier"] for d in datastores]
        ds_stats = api.latest_stats(ds_ids,
            ["capacity|total_capacity", "capacity|used_space"],
            progress=lambda m: log(f"Datastore-Metriken: {m}"))
        ds_props = api.properties(ds_ids, DS_TYPE_KEYS + DS_NAA_KEYS,
            progress=lambda m: log(f"Datastore-Typ: {m}"))
        # Diagnose: Schlüssel der 1. Nicht-vSAN-Platte, die nach Device/NAA
        # aussehen — zum Ablesen des exakten Schlüssels am echten vROps.
        if datastores:
            try:
                d0 = next((d for d in datastores
                           if "vsan" not in name_of(d).lower()), datastores[0])
                did0 = d0["identifier"]
                p0 = api.all_properties(did0)
                hits = {k: v for k, v in p0.items()
                        if re.search(r"naa\.[0-9a-fA-F]{8,}", str(v))
                        or re.search(r"disk|device|canonical|extent", k, re.I)}
                log(f"Datastore-Properties (Beispiel '{name_of(d0)}') – "
                    "Device/NAA-Kandidaten: "
                    + (", ".join(f"{k}={str(v)[:60]}" for k, v in
                                 sorted(hits.items())[:8]) or "keine gefunden"))
                # Metrik-Schlüssel des Datastores: hier liegt die NAA je nach
                # vROps-Version (Gruppe 'Devices' mit naa.…-Instanzen).
                sk = api.statkeys(did0)
                sk_hits = [k for k in sk
                           if re.search(r"naa\.[0-9a-fA-F]{8,}|device", k, re.I)]
                log(f"Datastore-Metrik-Schlüssel (Beispiel '{name_of(d0)}', "
                    f"{len(sk)} gesamt) – Device/NAA-Kandidaten: "
                    + (", ".join(sorted(sk_hits)[:8]) or "keine gefunden"))
                # Fallback-Suche: NAA auch über verwandte Hosts sichtbar machen
                if not sk_hits:
                    for hid in (api.related(did0, {"HostSystem"}) or [])[:1]:
                        hk = [k for k in api.statkeys(hid)
                              if re.search(r"naa\.[0-9a-fA-F]{8,}", k)]
                        log("Host-Metrik-Schlüssel mit NAA (Beispiel-Host): "
                            + (", ".join(sorted(hk)[:6]) or "keine gefunden"))
            except Exception as e:
                log(f"NAA-Diagnose übersprungen: {e}")
    except _SkipTier:
        pass
    except Exception as e:
        log(f"Storage-Kapazität nicht verfügbar: {e}")

    _NAA_RX = re.compile(r"naa\.[0-9a-fA-F]{8,}")
    ds_statkey_naa = {}     # Cache: {datastoreId: "naa.…" oder ""}

    def _naa_from_statkeys(did):
        """NAA aus den Metrik-Schlüsseln des Datastores lesen (z. B.
        'Devices|naa.6000…|…'). Ergebnis pro Datastore gecacht, damit jeder
        Datastore höchstens einen /statkeys-Aufruf verursacht."""
        if did in ds_statkey_naa:
            return ds_statkey_naa[did]
        naa = ""
        try:
            for k in api.statkeys(did):
                m = _NAA_RX.search(k)
                if m:
                    naa = m.group(0)
                    break
        except Exception:
            naa = ""
        ds_statkey_naa[did] = naa
        return naa

    def ds_naa(did):
        """NAA/Device-Kennung eines Datastores. Zuerst aus den Properties
        (Kandidaten-Schlüssel), sonst aus den Metrik-Schlüsseln (Device-Metrik
        'Devices|naa.…'), da vROps die NAA je nach Version nur dort führt."""
        p = ds_props.get(did) or {}
        for k in DS_NAA_KEYS:
            v = str(p.get(k) or "").strip()
            if v:
                m = _NAA_RX.search(v)
                return m.group(0) if m else v[:80]
        return _naa_from_statkeys(did)

    def ds_type(did, name):
        """Storage-Typ eines Datastores (erste gefüllte Kandidaten-Eigenschaft)."""
        p = ds_props.get(did) or {}
        for k in DS_TYPE_KEYS:
            v = str(p.get(k) or "").strip()
            if v:
                return v
        # Fallback: vSAN-Datastores heißen praktisch immer so
        return "vSAN" if "vsan" in (name or "").lower() else ""

    def is_vsan(typ, name):
        return "vsan" in (typ or "").lower() or "vsan" in (name or "").lower()

    data = {}
    for c in clusters:
        data[name_of(c)] = {"hosts": [], "vms": [], "tags": [], "portgroups": [],
                            "workload": cl_workload.get(c["identifier"]),
                            "storage": {"cap_gb": 0.0, "used_gb": 0.0, "luns": []}}

    # vSphere-Tags je Cluster: vROps liefert sie als EIGENSCHAFTEN der Ressource
    # (/resources/{id}/properties), nicht über einen Tag-Endpunkt. Ohne
    # --tag-property werden alle Eigenschaften genommen, deren Schlüssel "tag"
    # enthält; das Log nennt die erkannten Schlüssel zur Kontrolle.
    report(80)
    log("Lese vSphere-Tags der Cluster (Eigenschaften) ...")
    seen_keys, raw_sample = set(), []
    for c in clusters:
        try:
            props = api.all_properties(c["identifier"])
        except Exception as e:
            log(f"Eigenschaften nicht lesbar – keine vSphere-Tags: {e}")
            break
        tags = []
        for k, v in sorted(props.items()):
            kl = k.lower()
            if tag_property:
                if not kl.startswith(tag_property.lower()):
                    continue
            elif "tag" not in kl:
                continue
            val = str(v or "").strip()
            if not val:
                continue
            seen_keys.add(k)
            if val[:1] in ("[", "{") and not raw_sample:
                raw_sample.append(f"{k} = {val[:200]}")
            for label in tag_labels(k, val):
                if label not in tags:
                    tags.append(label)
        data[name_of(c)]["tags"] = sorted(tags)
    found = sum(len(d["tags"]) for d in data.values())
    log(f"vSphere-Tags gefunden: {found}"
        + (f" · Eigenschaften: {', '.join(sorted(seen_keys))}" if seen_keys
           else " – keine Eigenschaft mit 'tag' im Namen gefunden"))
    if raw_sample:
        log(f"Tag-Rohwert (Auszug): {raw_sample[0]}")

    for h in hosts:
        rid = h["identifier"]
        cl = (host_props.get(rid) or {}).get("summary|parentCluster")
        if cl not in data:
            continue  # Standalone-Host ohne Cluster
        st = host_stats.get(rid) or {}
        cores = st.get("cpu|corecount_provisioned")
        ram_kb = st.get("mem|host_provisioned")
        data[cl]["hosts"].append({
            "name": name_of(h),
            "cores": int(cores) if cores else 0,
            "ram_gb": round((ram_kb or 0) / 1024 / 1024, 1),
            "wwpns": host_wwpns(host_props.get(rid) or {}),
        })
    _hw = [hh for d in data.values() for hh in d["hosts"]]
    _hww = sum(1 for hh in _hw if hh.get("wwpns"))
    log(f"WWPN erkannt: {_hww}/{len(_hw)} Hosts mit FC-HBA-Port-WWN")

    for v in vms:
        rid = v["identifier"]
        if rid in exclude_vms:
            continue  # per Tag ausgeschlossen (z. B. Kapa_Filter:Ja)
        p = vm_props.get(rid) or {}
        cl = p.get("summary|parentCluster")
        if cl not in data:
            continue
        st = vm_stats.get(rid) or {}
        vcpu = st.get("config|hardware|num_Cpu") or p.get("config|hardware|numCpu")
        ram_kb = st.get("mem|guest_provisioned") or p.get("config|hardware|memoryKB")
        power = str(p.get("summary|runtime|powerState", "?"))
        disk = next((st[k] for k in VM_DISK_KEYS if st.get(k)), 0)
        data[cl]["vms"].append({
            "name": name_of(v),
            "vcpu": int(float(vcpu)) if vcpu else 0,
            "ram_gb": round(float(ram_kb or 0) / 1024 / 1024, 1),
            "disk_gb": round(float(disk or 0), 1),
            "on": "on" in power.lower().replace(" ", ""),
        })
    if "vms" in skip:
        for cl in data:
            data[cl]["vms"] = (prev.get(cl) or {}).get("vms") or []
    else:
        _dvm = [v for d in data.values() for v in d["vms"]]
        _dn = sum(1 for v in _dvm if v.get("disk_gb"))
        log(f"VM-Disk erkannt: {_dn}/{len(_dvm)} VMs mit Disk-Wert"
            + ("" if _dn else " – Kandidaten-Schlüssel: " + ", ".join(VM_DISK_KEYS)))

    # Datastore -> Cluster über die angedockten Hosts. Jeder Datastore (auch ein
    # von allen Hosts gesehenes FC-LUN oder der vSAN-Datastore) ist in vROps EINE
    # Ressource und wird je Cluster genau EINMAL gezählt – kein Doppeln über die
    # Hosts. Ein cluster-übergreifend geteiltes LUN zählt in jedem Cluster, an
    # dessen Hosts es hängt, einmal.
    host_cluster = {h["identifier"]: (host_props.get(h["identifier"]) or {})
                    .get("summary|parentCluster") for h in hosts}
    # Je Cluster genügt EIN Host: alle Hosts eines Clusters sehen dieselben LUNs
    # (vSAN/FC). Das spart Abfragen (1 je Cluster statt 1 je Datastore). Liefert
    # ein Host nichts (z. B. Wartung), wird der nächste versucht.
    ds_by_id = {d["identifier"]: d for d in datastores}
    if datastores:
        log(f"Ordne {len(datastores)} Datastores den Clustern zu (über Hosts) ...")
    for cl in data:
        cl_hosts = [h["identifier"] for h in hosts
                    if host_cluster.get(h["identifier"]) == cl]
        seen = []
        for hid in cl_hosts:
            try:
                seen = api.related(hid, {"Datastore"})
            except Exception as e:
                log(f"Datastore-Beziehungen für {cl} nicht lesbar: {e}")
                seen = []
            if seen:
                break
        for did in dict.fromkeys(seen):        # dedupliziert, Reihenfolge egal
            ds = ds_by_id.get(did)
            st = ds_stats.get(did) or {}
            cap = st.get("capacity|total_capacity")
            used = st.get("capacity|used_space")
            if not ds or (not cap and not used):
                continue
            nm = name_of(ds)
            typ = ds_type(did, nm)
            # vSAN-Policy zehrt an der nutzbaren Kapazität (RAID-1 ~50 %, RAID-5
            # ~75/67 %, RAID-6 ~67 %). vROps liefert dazu KEINEN sauberen Wert,
            # nur physisch roh + physisch belegt – daher ein konfigurierbarer
            # Faktor (global, je Cluster überschreibbar). Kapazität UND Belegung
            # werden gerechnet; in der %-Auslastung kürzt sich der Faktor heraus
            # (used·f / cap·f = used/cap), sie bleibt also physisch korrekt.
            f = 1.0
            if is_vsan(typ, nm):
                f = (vsan_overrides or {}).get(cl, vsan_factor)
            raw_cap, raw_used = float(cap or 0), float(used or 0)
            eff_cap, eff_used = raw_cap * f, raw_used * f
            data[cl]["storage"]["cap_gb"] += eff_cap
            data[cl]["storage"]["used_gb"] += eff_used
            data[cl]["storage"]["luns"].append({
                "name": nm,
                "type": typ or "unbekannt",
                "naa": ds_naa(did),
                "cap_gb": eff_cap,
                "used_gb": eff_used,
                "raw_cap_gb": raw_cap,
                "factor": f})
    if datastores:
        typen = sorted({l["type"] for d in data.values() for l in d["storage"]["luns"]})
        log("Storage zugeordnet: "
            + (", ".join(f"{cl}={len(d['storage']['luns'])} LUNs / "
                         f"{round(d['storage']['cap_gb'])} GB"
                         for cl, d in data.items() if d["storage"]["luns"])
               or "keinem Cluster (Beziehungen prüfen)")
            + (f" · Typen: {', '.join(typen)}" if typen else "")
            + (f" · vSAN mit Faktor {vsan_factor} gerechnet"
               if any(l["factor"] != 1.0 for d in data.values()
                      for l in d["storage"]["luns"]) else "")
            + " · NAA erkannt: "
            + str(sum(1 for d in data.values()
                      for l in d["storage"]["luns"] if l.get("naa")))
            + "/" + str(sum(len(d["storage"]["luns"]) for d in data.values())))

    if "storage" in skip:
        for cl in data:
            data[cl]["storage"] = (prev.get(cl) or {}).get("storage") \
                or {"cap_gb": 0.0, "used_gb": 0.0, "luns": []}

    # dvSwitches + Portgruppen je Cluster (Netzwerk-Reiter im Popup + VLAN-Suche).
    # Zuordnung wie beim Storage über die Hosts: dvSwitch -> HostSystem ->
    # summary|parentCluster. Ein dvSwitch kann sich über mehrere Cluster
    # erstrecken; er (und seine Portgruppen) erscheint dann bei jedem. Die
    # VLAN-Nummer ist best effort (Property-Schlüssel je vROps-Version anders).
    # Fehler = leerer Bereich, der Rest läuft weiter.
    VLAN_IN_NAME = re.compile(r"vlan[\s_\-]?(\d{1,4})", re.I)
    try:
        if "network" in skip:
            for cl in data:
                data[cl]["portgroups"] = (prev.get(cl) or {}).get("portgroups") or []
            log("Netzwerk: übersprungen (Intervall) – Stand vom letzten Abruf")
            raise _SkipTier()
        report(90)
        log("Lese dvSwitches und Portgruppen ...")
        dvs_list = api.resources("VmwareDistributedVirtualSwitch")
        pg_list = api.resources("DistributedVirtualPortgroup")
        pg_name = {p["identifier"]: name_of(p) for p in pg_list}

        # VLAN kommt je Portgruppe aus /resources/{id}/properties. Die Schlüssel
        # heißen je nach vROps-Version anders, und über die Bulk-Abfrage kommen
        # diese Config-Properties oft nicht zurück – deshalb pro Portgruppe die
        # Eigenschaften einzeln holen (gecacht) und jeden Schlüssel mit "vlan" im
        # Namen auswerten. "0" ist ein gültiger Wert (untagged), Trunk-Ranges wie
        # "0-4094" werden unverändert übernommen.
        pg_prop_cache = {}

        def pg_props(pid):
            if pid not in pg_prop_cache:
                try:
                    pg_prop_cache[pid] = api.all_properties(pid)
                except Exception:
                    pg_prop_cache[pid] = {}
            return pg_prop_cache[pid]

        def _clean_vlan(v):
            s = str(v).strip()
            return "" if s.lower() in ("", "none", "null") else s

        vlan_cached = [0]

        def pg_vlan(pid):
            # VLAN-Cache: bekannte Portgruppen (per Name, aus dem letzten
            # Abruf) sparen den teuersten Teil – einen API-Aufruf JE
            # Portgruppe. Neue Portgruppen werden normal gelesen; einmal am
            # Tag liest do_refresh alles frisch (falls sich ein VLAN einer
            # bestehenden Portgruppe ändert).
            if vlan_hint:
                v = vlan_hint.get(pg_name.get(pid, ""))
                if v:
                    vlan_cached[0] += 1
                    return v
            props = pg_props(pid)
            vlanish = [(k, v) for k, v in props.items() if "vlan" in k.lower()]
            # bevorzugt einen konkreten ...vlanId-Wert
            for k, v in vlanish:
                if "vlanid" in k.lower().replace("|", "").replace("_", ""):
                    s = _clean_vlan(v)
                    if s:
                        return s
            # sonst irgendein vlan-Wert (z. B. Trunk-Range "0-4094")
            for k, v in vlanish:
                s = _clean_vlan(v)
                if s:
                    return s
            # Rückfall: VLAN aus dem Portgruppen-Namen (z. B. "...-VLAN205")
            m = VLAN_IN_NAME.search(pg_name.get(pid, ""))
            return m.group(1) if m else ""

        # Diagnose: echte Schlüssel/Werte der ersten Portgruppe ins Log, damit
        # sich der genaue VLAN-Schlüssel notfalls ablesen lässt.
        if pg_list:
            s0 = pg_props(pg_list[0]["identifier"])
            vlan0 = {k: s0[k] for k in s0 if "vlan" in k.lower()}
            log("Portgruppen-Properties (Beispiel 1. Portgruppe) – vlan-Schlüssel: "
                + (", ".join(f"{k}={s0[k]}" for k in vlan0) if vlan0
                   else "keiner mit 'vlan' im Namen"))
            log("Alle Property-Schlüssel der 1. Portgruppe: "
                + ((", ".join(sorted(s0)))[:600] or "keine"))

        # Der dvSwitch dient nur zur Zuordnung Portgruppe -> Cluster; sein Name
        # wird bewusst NICHT ausgeliefert (nur die Portgruppen je Cluster).
        for dv in dvs_list:
            did = dv["identifier"]
            try:
                dv_hosts = api.related(did, {"HostSystem"})
            except Exception as e:
                log(f"dvSwitch-Hosts nicht lesbar: {e}")
                dv_hosts = []
            dv_clusters = {host_cluster.get(h) for h in dv_hosts} & set(data)
            if not dv_clusters:
                continue
            try:
                dv_pg_ids = api.related(did, {"DistributedVirtualPortgroup"})
            except Exception:
                dv_pg_ids = []
            pgs = [{"name": pg_name.get(pid, "?"), "vlan": pg_vlan(pid)}
                   for pid in dict.fromkeys(dv_pg_ids) if pid in pg_name]
            for cl in dv_clusters:
                data[cl]["portgroups"].extend(pgs)
        # je Cluster nach Name deduplizieren und sortieren
        for d in data.values():
            seen, uniq = set(), []
            for pg in sorted(d["portgroups"], key=lambda x: x["name"].lower()):
                key = (pg["name"], pg["vlan"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(pg)
            d["portgroups"] = uniq
        n_pg = sum(len(d["portgroups"]) for d in data.values())
        log(f"dvSwitches: {len(dvs_list)}, Portgruppen: {len(pg_list)} · zugeordnet: "
            + (", ".join(f"{cl}={len(d['portgroups'])}"
                         for cl, d in data.items() if d["portgroups"])
               or "keinem Cluster (Beziehungen prüfen)")
            + (f" · {n_pg} Portgruppen sichtbar" if n_pg else "")
            + f" · VLANs: {vlan_cached[0]} aus Cache, {len(pg_prop_cache)} per API")
    except _SkipTier:
        pass
    except Exception as e:
        log(f"Netzwerk-Daten (dvSwitch) nicht verfügbar: {e}")

    # Tanzu-/vSphere-Namespaces: deren RESERVIERUNGEN zählen gegen die freie
    # Kapazität (analog zu genehmigten manuellen Reservierungen). Best effort –
    # Resource-Kind und Stat-Keys unterscheiden sich je nach vROps-Version,
    # deshalb Kandidatenlisten und ein ausführliches Log zum Gegenprüfen.
    # Ohne Tanzu findet der Abruf schlicht nichts und ändert nichts.
    if "vms" in skip:
        for cl in data:
            data[cl]["namespaces"] = (prev.get(cl) or {}).get("namespaces") or []
    NS_KINDS = [] if "vms" in skip else [
                "Namespace", "SupervisorNamespace", "K8sNamespace",
                "vSphereNamespace"]
    # CPU-Reservierung in MHz (Kandidaten), RAM-Reservierung mit Einheit je Key
    NS_CPU_KEYS = ["config|cpuAllocation|reservation", "cpu|reservation",
                   "configuration|cpuReservation", "summary|config|cpuReservation"]
    NS_MEM_KEYS = [("config|memAllocation|reservation", "MB"),
                   ("mem|reservation", "KB"),
                   ("configuration|memoryReservation", "MB"),
                   ("summary|config|memReservation", "MB")]
    try:
        report(92)
        namespaces = []
        for kind in NS_KINDS:
            try:
                namespaces = api.resources(kind)
            except Exception:
                namespaces = []
            if namespaces:
                log(f"Tanzu: {len(namespaces)} Namespaces gefunden (Kind: {kind})")
                break
        if namespaces:
            ns_ids = [n["identifier"] for n in namespaces]
            all_keys = NS_CPU_KEYS + [k for k, _ in NS_MEM_KEYS]
            ns_props = api.properties(ns_ids, all_keys)
            ns_stats = api.latest_stats(ns_ids, all_keys)
            hit_keys = set()
            for ns in namespaces:
                nid = ns["identifier"]
                nname = name_of(ns)
                merged = dict(ns_stats.get(nid) or {})
                merged.update({k: v for k, v in (ns_props.get(nid) or {}).items()
                               if v not in (None, "")})
                cpu_mhz = 0.0
                for k in NS_CPU_KEYS:
                    try:
                        v = float(merged.get(k))
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        cpu_mhz = v
                        hit_keys.add(k)
                        break
                ram_gb = 0.0
                for k, unit in NS_MEM_KEYS:
                    try:
                        v = float(merged.get(k))
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        ram_gb = v / (1024.0 * 1024.0 if unit == "KB" else 1024.0)
                        hit_keys.add(k)
                        break
                if cpu_mhz <= 0 and ram_gb <= 0:
                    continue
                # Cluster über die Beziehungen finden (Namespace -> Supervisor
                # = ClusterComputeResource; notfalls über einen Host)
                cl_name = ""
                try:
                    rel = api.related(nid, kinds={"ClusterComputeResource"})
                    if rel:
                        rid = rel[0]
                        cl_name = next((name_of(c) for c in clusters
                                        if c["identifier"] == rid), "")
                except Exception:
                    pass
                if cl_name and cl_name in data:
                    data[cl_name].setdefault("namespaces", []).append(
                        {"name": nname, "cpu_mhz": round(cpu_mhz),
                         "ram_gb": round(ram_gb, 1)})
                else:
                    log(f"Tanzu: Namespace '{nname}' keinem Cluster zuordenbar "
                        "(Beziehungen prüfen)")
            n_used = sum(len(d.get("namespaces") or []) for d in data.values())
            log("Tanzu: Reservierungen übernommen: "
                + (", ".join(f"{cl}={len(d['namespaces'])}"
                             for cl, d in data.items() if d.get("namespaces"))
                   or "keine (Reservierungen 0 oder Zuordnung fehlt)")
                + (f" · erkannte Schlüssel: {sorted(hit_keys)}" if n_used else ""))
        else:
            log("Tanzu: keine Namespace-Ressourcen gefunden (Kinds: "
                + ", ".join(NS_KINDS) + ") – ohne Tanzu ist das normal")
    except Exception as e:
        log(f"Tanzu-Namespaces nicht verfügbar: {e}")

    report(100)
    return (build_summary(data, cpu_factor, failover_hosts, tanzu_mhz,
                          min_lun_gb, exclude_names,
                          net_exclude_names, net_exclude_vlans), data)


def _name_excluded(name, patterns):
    """Namensfilter: Muster OHNE Wildcard = Teilstring (irgendwo im Namen),
    Muster MIT * oder ? = Glob über den ganzen Namen. Groß-/Kleinschreibung egal."""
    import fnmatch
    n = str(name or "").lower()
    for p in patterns:
        p = str(p or "").strip().lower()      # Muster ebenfalls klein -> case-egal
        if not p:
            continue
        if "*" in p or "?" in p:
            if fnmatch.fnmatch(n, p):          # n & p klein -> fnmatchcase-neutral
                return True
        elif p in n:
            return True
    return False


def _vlan_excluded(vlan, tokens):
    """VLAN-ID-Filter für Portgruppen: tokens sind einzelne IDs ("205") oder
    Bereiche ("100-110"). Trifft nur Portgruppen mit EINZELNER VLAN-ID –
    Trunk-Bereiche (z. B. "0-4094") bleiben unberührt (die blendet ohnehin
    schon die Uplink-Erkennung aus)."""
    s = str(vlan or "").strip()
    if not s.isdigit():
        return False
    v = int(s)
    for t in tokens:
        t = str(t or "").strip()
        if not t:
            continue
        if "-" in t:
            a, _, b = t.partition("-")
            if a.strip().isdigit() and b.strip().isdigit() \
                    and int(a) <= v <= int(b):
                return True
        elif t.isdigit() and int(t) == v:
            return True
    return False


def build_summary(data, cpu_factor, failover_hosts=1, tanzu_mhz=2500,
                  min_lun_gb=0, exclude_names=None,
                  net_exclude_names=None, net_exclude_vlans=None):
    """data: {cluster: {hosts:[{cores,ram_gb}], vms:[{vcpu,ram_gb,on}]}}

    Pro Cluster werden die größten `failover_hosts` Hosts als Ausfallreserve
    (N+1) von der Gesamtkapazität abgezogen. Tanzu-Namespace-Reservierungen
    (CPU in MHz, RAM in GB) werden als vCPU-Äquivalent (tanzu_mhz MHz je vCPU,
    0 = CPU nicht zählen) bzw. GB ausgewiesen und zählen wie genehmigte
    Reservierungen gegen die freie Kapazität."""
    clusters = []
    for cl, d in sorted(data.items()):
        ns_list = []
        for ns in (d.get("namespaces") or []):
            mhz = float(ns.get("cpu_mhz") or 0)
            vcpu = (int(-(-mhz // tanzu_mhz)) if tanzu_mhz > 0 and mhz > 0 else 0)
            ns_list.append({"name": ns.get("name") or "?",
                            "cpu_mhz": round(mhz),
                            "vcpu": vcpu,
                            "ram_gb": round(float(ns.get("ram_gb") or 0), 1)})
        ns_list.sort(key=lambda n: (n["ram_gb"], n["vcpu"]), reverse=True)
        tz_vcpu = sum(n["vcpu"] for n in ns_list)
        tz_ram = round(sum(n["ram_gb"] for n in ns_list), 1)
        n_spare = min(max(0, failover_hosts), max(0, len(d["hosts"]) - 1))
        spare_cores = sum(sorted((h["cores"] for h in d["hosts"]), reverse=True)[:n_spare])
        spare_ram = sum(sorted((h["ram_gb"] for h in d["hosts"]), reverse=True)[:n_spare])
        cores = sum(h["cores"] for h in d["hosts"]) - spare_cores
        host_ram = round(sum(h["ram_gb"] for h in d["hosts"]) - spare_ram, 1)
        vcpu_cap = cores * cpu_factor
        vcpu_used = sum(v["vcpu"] for v in d["vms"])
        ram_used = round(sum(v["ram_gb"] for v in d["vms"]), 1)
        storage = d.get("storage") or {}
        # Datastores KOMPLETT ausschließen (Liste, Kapazität, Belegung):
        #  - unter der Mindest-LUN-Größe (Brutto), und/oder
        #  - deren Name eines der Namensmuster enthält (z. B. "iso", "backup").
        excl = exclude_names or []
        luns_all = storage.get("luns") or []
        luns = [l for l in luns_all
                if (min_lun_gb <= 0
                    or float(l.get("raw_cap_gb") or l.get("cap_gb") or 0) >= min_lun_gb)
                and not _name_excluded(l.get("name") or "", excl)]
        if min_lun_gb > 0 or excl:
            stor_cap = round(sum(float(l.get("cap_gb") or 0) for l in luns), 1)
            stor_used = round(sum(float(l.get("used_gb") or 0) for l in luns), 1)
        else:
            stor_cap = round(float(storage.get("cap_gb") or 0), 1)
            stor_used = round(float(storage.get("used_gb") or 0), 1)
        storage = dict(storage, luns=luns)
        clusters.append({
            "name": cl,
            "hostCount": len(d["hosts"]),
            "cores": cores,
            "spareCores": spare_cores,
            "spareRamGb": round(spare_ram, 1),
            "vcpuCap": vcpu_cap,
            "vcpuUsed": vcpu_used,
            "vcpuFree": vcpu_cap - vcpu_used,
            "ramCap": host_ram,
            "ramUsed": ram_used,
            "ramFree": round(host_ram - ram_used, 1),
            "storageCap": stor_cap,
            "storageUsed": stor_used,
            "storageFree": round(stor_cap - stor_used, 1),
            "datastores": sorted(
                [{"name": l.get("name"),
                  "type": l.get("type") or "",
                  "naa": l.get("naa") or "",
                  "cap_gb": round(float(l.get("cap_gb") or 0), 1),
                  "used_gb": round(float(l.get("used_gb") or 0), 1),
                  "raw_cap_gb": round(float(l.get("raw_cap_gb") or 0), 1),
                  "factor": float(l.get("factor") or 1.0)}
                 for l in (storage.get("luns") or [])],
                key=lambda l: l["cap_gb"], reverse=True),
            "vmCount": len(d["vms"]),
            "vmOff": sum(1 for v in d["vms"] if not v["on"]),
            "tags": list(d.get("tags") or []),
            # Netzwerk-Filter (Verwaltung -> Netzwerk): Portgruppen nach Name
            # bzw. VLAN-ID ausblenden — wirkt überall (Cluster-Detail,
            # VLAN-Suche, Datenpaket).
            "portgroups": [p for p in (d.get("portgroups") or [])
                           if not _name_excluded(p.get("name") or "",
                                                 net_exclude_names or [])
                           and not _vlan_excluded(p.get("vlan"),
                                                  net_exclude_vlans or [])],
            "workload": d.get("workload"),
            "source": d.get("source"),
            "namespaces": ns_list,
            "tanzuVcpu": tz_vcpu,
            "tanzuRamGb": tz_ram,
            "hosts": d["hosts"],
            "vms": d["vms"],
        })
    return clusters

# ------------------------------------------------------------ Beispieldaten --

def sample_data(vsan_factor=0.5, vsan_overrides=None):
    import random
    random.seed(42)
    data = {}
    for ci in range(1, 4):
        cl = f"Cluster-{ci:02d}"
        hosts = [{"name": f"esx{ci}{hi:02d}.firma.local",
                  "cores": random.choice([32, 48, 64]),
                  "ram_gb": random.choice([512, 768, 1024]),
                  # zwei FC-HBA-Ports je Host (wie in echt, redundant verkabelt)
                  "wwpns": [f"20:00:00:25:b5:{ci:02x}:{hi:02x}:0{p}"
                            for p in range(2)]}
                 for hi in range(1, random.randint(4, 7))]
        vms = [{"name": f"vm-{ci}{vi:03d}",
                "vcpu": random.choice([2, 4, 4, 8, 8, 16]),
                "ram_gb": random.choice([8, 16, 16, 32, 64]),
                "on": random.random() > 0.1}
               for vi in range(1, random.randint(40, 120))]
        # Disk deterministisch aus RAM/vCPU ableiten — bewusst KEIN random:
        # ein zusätzlicher Zufallszug würde die geseedete Demo-Sequenz (seed 42)
        # verschieben und damit alle nachfolgenden Demo-Werte ändern.
        for v in vms:
            v["disk_gb"] = {8: 120, 16: 250, 32: 500, 64: 900}.get(
                v["ram_gb"], 150) + v["vcpu"] * 15
        # Beispiel-LUNs: ein großer vSAN-Datastore (Spiegelung -> 50 % nutzbar)
        # plus mehrere FC-LUNs (VMFS, brutto = nutzbar)
        raw = random.choice([40000, 80000])
        _vf = (vsan_overrides or {}).get(cl, vsan_factor)   # global bzw. je Cluster
        luns = [{"name": f"vsan-cl{ci}", "type": "vSAN", "factor": _vf,
                 "raw_cap_gb": raw, "cap_gb": raw * _vf, "used_gb": 0}]
        for li in range(1, random.randint(3, 7)):
            cap = random.choice([2000, 4000, 8000])
            luns.append({"name": f"FC-LUN-{ci}{li:02d}", "type": "VMFS",
                         "naa": f"naa.60060160a{ci}b{li:02d}00{random.randint(10**11, 10**12 - 1):x}",
                         "factor": 1.0, "raw_cap_gb": cap,
                         "cap_gb": cap, "used_gb": 0})
        # Ein paar benannte Datastores, damit sich der Namensfilter demonstrieren
        # lässt (z. B. „iso", „template*", „*service*").
        for nm, typ, cap in [(f"iso-library-{ci}", "NFS", 500),
                             (f"template-store-{ci}", "VMFS", 1000),
                             (f"app-service-{ci}", "VMFS", 2000)]:
            lun = {"name": nm, "type": typ, "factor": 1.0,
                   "raw_cap_gb": cap, "cap_gb": cap, "used_gb": 0}
            if typ != "NFS":
                lun["naa"] = f"naa.60060160a{ci}f{cap:04x}{random.randint(10**9, 10**10 - 1):x}"
            luns.append(lun)
        for l in luns:
            l["used_gb"] = round(l["cap_gb"] * random.uniform(0.35, 0.9))
        cap_gb = sum(l["cap_gb"] for l in luns)
        used_gb = sum(l["used_gb"] for l in luns)
        tags = [f"Standort: {random.choice(['RZ-Nord', 'RZ-Sued'])}",
                f"Umgebung: {random.choice(['Produktion', 'Test'])}",
                f"Betreuung: {random.choice(['Team Netzwerk', 'Team Betrieb'])}",
                "Kapa_Filter: Nein"]
        # Beispiel-Portgruppen, die (wie in echt) IP-Netze im Namen tragen –
        # so lässt sich die VLAN-Suche offline ausprobieren.
        pgs = []
        for pi in range(random.randint(3, 6)):
            octet = 10 + ci * 10 + pi
            vlan = 100 * ci + pi
            zweck = random.choice(["Prod", "Test", "Mgmt", "Backup", "DMZ"])
            pgs.append({"name": f"PG-{zweck}-10.{ci}.{octet}.0_24-VLAN{vlan}",
                        "vlan": str(vlan)})
        # Eine Uplink-/Trunk-Portgruppe (VLAN 0-4094) – wird standardmäßig
        # ausgeblendet (siehe --show-uplink-portgroups).
        pgs.append({"name": f"dvSwitch-{cl}-DVUplinks", "vlan": "0-4094"})
        pgs.sort(key=lambda x: x["name"].lower())
        # Tanzu-Demo: Cluster 1 und 3 tragen Namespace-Reservierungen
        namespaces = []
        if ci == 1:
            namespaces = [{"name": "ns-webshop-prod", "cpu_mhz": 20000, "ram_gb": 96.0},
                          {"name": "ns-ci-runner", "cpu_mhz": 8000, "ram_gb": 32.0}]
        elif ci == 3:
            namespaces = [{"name": "ns-data-analytics", "cpu_mhz": 12000, "ram_gb": 64.0}]
        data[cl] = {"hosts": hosts, "vms": vms, "tags": tags, "portgroups": pgs,
                    "workload": random.choice([38, 52, 61, 74, 83]),
                    "source": "RZ-Nord" if ci <= 2 else "RZ-Sued",
                    "namespaces": namespaces,
                    "storage": {"cap_gb": cap_gb, "used_gb": used_gb, "luns": luns}}
    return data


# PowerCLI-Export für Offline-Quellen (Verwaltung -> Import, als .ps1-Download).
# Erzeugt genau das JSON, das POST /api/import erwartet. Bewusst nur mit
# PowerCLI-Bordmitteln und ohne Schreibzugriffe auf das vCenter.
POWERCLI_PS1 = r"""#Requires -Modules VMware.PowerCLI
<#
  Kapa-Dashboard: Offline-Export eines vCenters ohne vROps-Anbindung.

  Aufruf (auf einem Rechner mit Netz zum isolierten vCenter):
    .\kapa_export.ps1 -Server vcenter.insel.local
    .\kapa_export.ps1 -Server vcenter.insel.local -Cluster "Prod*" -OutFile insel.json

  Ergebnis: eine JSON-Datei, die sich im Dashboard unter
  Verwaltung -> Import mit einem frei gewaehlten Quellnamen hochladen laesst.
  Erfasst je Cluster: ESXi-Hosts (Cores/RAM), VMs (vCPU/RAM/Power),
  Datastores (Groesse/Belegung/Typ) und Portgruppen mit VLAN-ID.
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$Cluster = "*",
    [string]$OutFile = "kapa_import.json"
)

Connect-VIServer -Server $Server | Out-Null

$result = @()
foreach ($cl in (Get-Cluster -Name $Cluster | Sort-Object Name)) {
    Write-Host "Lese Cluster $($cl.Name) ..."
    $vmhosts = @($cl | Get-VMHost)

    $hosts = @()
    foreach ($h in $vmhosts) {
        $hosts += [ordered]@{
            name   = $h.Name
            cores  = [int]$h.NumCpu
            ram_gb = [math]::Round($h.MemoryTotalGB, 1)
        }
    }

    $vms = @()
    foreach ($vm in ($cl | Get-VM)) {
        $vms += [ordered]@{
            name   = $vm.Name
            vcpu   = [int]$vm.NumCpu
            ram_gb = [math]::Round($vm.MemoryGB, 1)
            on     = ($vm.PowerState -eq "PoweredOn")
        }
    }

    $ds = @()
    foreach ($d in ($cl | Get-Datastore)) {
        $ds += [ordered]@{
            name    = $d.Name
            type    = "" + $d.Type
            cap_gb  = [math]::Round($d.CapacityGB, 1)
            used_gb = [math]::Round(($d.CapacityGB - $d.FreeSpaceGB), 1)
        }
    }

    # Portgruppen: Standard-vSwitches je Host + dvSwitches der Cluster-Hosts.
    # Es reicht die VLAN-ID (einzeln oder Bereich) - keine weiteren Details.
    $pgs = @{}
    foreach ($pg in ($vmhosts | Get-VirtualPortGroup -Standard -ErrorAction SilentlyContinue)) {
        $pgs[$pg.Name] = "" + $pg.VLanId
    }
    $vds = @($vmhosts | Get-VDSwitch -ErrorAction SilentlyContinue | Sort-Object -Unique Name)
    foreach ($sw in $vds) {
        foreach ($pg in (Get-VDPortgroup -VDSwitch $sw -ErrorAction SilentlyContinue)) {
            $v = $pg.VlanConfiguration
            if ($null -ne $v -and $null -ne $v.VlanId) {
                $pgs[$pg.Name] = "" + $v.VlanId
            } elseif ($null -ne $v -and $v.Ranges) {
                $pgs[$pg.Name] = (@($v.Ranges | ForEach-Object {
                    "$($_.StartVlanId)-$($_.EndVlanId)" }) -join ",")
            } elseif (-not $pgs.ContainsKey($pg.Name)) {
                $pgs[$pg.Name] = ""
            }
        }
    }
    $pglist = @()
    foreach ($k in ($pgs.Keys | Sort-Object)) {
        $pglist += [ordered]@{ name = $k; vlan = $pgs[$k] }
    }

    $result += [ordered]@{
        name       = $cl.Name
        hosts      = $hosts
        vms        = $vms
        datastores = $ds
        portgroups = $pglist
    }
}

[ordered]@{ clusters = $result } | ConvertTo-Json -Depth 6 |
    Out-File -FilePath $OutFile -Encoding UTF8
Write-Host "Fertig: $OutFile ($($result.Count) Cluster)"
"""

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
API_DOCS_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>/* Theme vor dem ersten Rendern setzen (kein Flackern). ?theme=light|dark
   in der URL überstimmt einmalig (z. B. für Kiosk-Anzeigen), sonst gilt die
   gespeicherte Wahl bzw. die Systemeinstellung. */
try { var _t = new URLSearchParams(location.search).get("theme")
            || localStorage.getItem("kapa_theme");
  if (!_t && window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches) _t = "light";
  if (_t === "light") document.documentElement.setAttribute("data-theme", "light");
} catch (e) {}</script>
<title>API-Dokumentation – VMware Kapazitätsplanung</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --text:#e2e8f0; --muted:#94a3b8;
          --accent:#38bdf8; --ok:#22c55e; --get:#0ea5e9; --field:#0b1220; }
  html[data-theme="light"] { --bg:#eef2f7; --card:#ffffff; --line:#d4dbe5;
          --text:#1e293b; --muted:#5b6b7f; --accent:#0369a1; --ok:#15803d;
          --get:#0284c7; --field:#f6f8fb; }
  html[data-theme="light"] .btn { color:#ffffff; }
  * { box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font:14px/1.55 "Segoe UI",system-ui,sans-serif;
         margin:0; padding:24px; }
  .wrap { max-width:960px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 2px; }
  .sub { color:var(--muted); margin-bottom:20px; }
  a { color:var(--accent); }
  .aanum { width:70px; background:var(--field); border:1px solid var(--line);
           color:var(--text); border-radius:6px; padding:4px 6px; text-align:center; }
  .authbox { background:var(--card); border:1px solid var(--line); border-radius:12px;
             padding:14px 16px; margin-bottom:20px; }
  .authbox label { font-size:12px; color:var(--muted); display:block; margin-bottom:4px; }
  .authbox input { width:100%; background:var(--field); border:1px solid var(--line); color:var(--text);
                   border-radius:8px; padding:9px 12px; font-size:13px; font-family:monospace; }
  .hint { color:var(--muted); font-size:12px; margin-top:6px; }
  .ep { background:var(--card); border:1px solid var(--line); border-radius:12px;
        margin-bottom:14px; overflow:hidden; }
  .ephead { display:flex; align-items:center; gap:10px; padding:12px 16px; cursor:pointer; }
  .method { font-weight:700; font-size:12px; padding:3px 8px; border-radius:6px;
            background:rgba(14,165,233,.15); color:var(--get); letter-spacing:.5px; }
  .method.post { background:rgba(34,197,94,.15); color:var(--ok); }
  .path { font-family:monospace; font-size:14px; }
  .summary { color:var(--muted); margin-left:auto; font-size:13px; }
  .epbody { padding:0 16px 16px; border-top:1px solid var(--line); }
  .epbody p { color:var(--muted); }
  table { border-collapse:collapse; width:100%; font-size:13px; margin:8px 0; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; }
  td input { background:var(--field); border:1px solid var(--line); color:var(--text);
             border-radius:6px; padding:5px 8px; font-size:13px; width:100%; }
  .btn { background:var(--accent); color:#08131f; border:none; border-radius:8px;
         padding:8px 14px; font-size:13px; font-weight:600; cursor:pointer; margin-top:6px; }
  pre { background:var(--field); border:1px solid var(--line); border-radius:8px; padding:12px;
        overflow:auto; font-size:12px; max-height:360px; }
  .status { font-weight:600; margin:8px 0 4px; }
  .foot { color:var(--muted); font-size:12px; margin-top:24px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>API-Dokumentation <span style="color:var(--muted);font-weight:400">· v__VERSION__</span></h1>
  <div class="sub">Lesende REST-API unter <code>/api/v1/</code> ·
    <a href="openapi.json">OpenAPI-Spec (JSON)</a> – importierbar in Swagger Editor/Postman.</div>

  <div class="authbox">
    <label for="tok">Bearer-Token (im Dashboard unter „Verwaltung → API-Tokens" erzeugen)</label>
    <input id="tok" placeholder="kapa_…" autocomplete="off" spellcheck="false">
    <div class="hint">Wird nur lokal im Browser gespeichert und bei „Ausführen" als
      <code>Authorization: Bearer …</code> mitgeschickt. Angemeldete Admins können auch ohne
      Token testen (Session-Cookie).</div>
  </div>

  <div id="eps"></div>
  <div class="foot">Self-contained – kein externes Swagger-UI/CDN. Details je Feld: siehe OpenAPI-Spec.</div>
</div>
<script>
const BASE = location.pathname.replace(/\/docs\/?$/, "");   // /api/v1  (auch hinter Proxy)
const tokEl = document.getElementById("tok");
try { tokEl.value = localStorage.getItem("kapa_api_token") || ""; } catch (e) {}
tokEl.addEventListener("input", () => { try { localStorage.setItem("kapa_api_token", tokEl.value); } catch (e) {} });

function esc(s){ return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

// Browsersprache nicht Deutsch -> englische Beschriftung. Die Endpunkt-Texte
// kommen bereits lokalisiert aus openapi.json (Accept-Language, serverseitig).
const IS_DE = (navigator.language || "de").toLowerCase().startsWith("de");
const L_EN = {
  "API-Dokumentation – VMware Kapazitätsplanung": "API documentation – VMware Capacity Planning",
  "API-Dokumentation": "API documentation",
  "Lesende REST-API unter": "Read-only REST API at",
  "OpenAPI-Spec (JSON)": "OpenAPI spec (JSON)",
  "– importierbar in Swagger Editor/Postman.": "– importable into Swagger Editor/Postman.",
  "Bearer-Token (im Dashboard unter „Verwaltung → API-Tokens\" erzeugen)":
    "Bearer token (create it in the dashboard under „Administration → API tokens“)",
  "Wird nur lokal im Browser gespeichert und bei „Ausführen\" als":
    "Stored only locally in the browser and sent along on „Run“ as",
  "mitgeschickt. Angemeldete Admins können auch ohne Token testen (Session-Cookie).":
    ". Signed-in admins can also test without a token (session cookie).",
  "Self-contained – kein externes Swagger-UI/CDN. Details je Feld: siehe OpenAPI-Spec.":
    "Self-contained – no external Swagger UI/CDN. Field details: see the OpenAPI spec.",
  "Parameter": "Parameter", "Bedeutung": "Meaning", "Wert (optional)": "Value (optional)",
  "Ausführen": "Run", "OpenAPI-Spec nicht ladbar.": "Could not load the OpenAPI spec.",
  "Fehler: ": "Error: ", "JSON-Body (optional)": "JSON body (optional)", "Pfad": "path"
};
function tr(s) { return IS_DE ? s : (L_EN[(s || "").replace(/\s+/g, " ").trim()] || s); }
if (!IS_DE) {
  document.documentElement.lang = "en";
  document.title = tr(document.title);
  const walk = n => {
    if (n.nodeType === 3) {
      const lead = n.data.match(/^\s*/)[0], tail = n.data.match(/\s*$/)[0];
      const t = tr(n.data);
      if (t !== n.data) n.data = lead + t + tail;
      return;
    }
    if (n.nodeType !== 1 || /^(SCRIPT|STYLE|PRE|CODE|INPUT)$/.test(n.nodeName)) return;
    let c = n.firstChild;
    while (c) { const nx = c.nextSibling; walk(c); c = nx; }
  };
  walk(document.body);
}

fetch("openapi.json").then(r => r.json()).then(spec => {
  const box = document.getElementById("eps");
  Object.keys(spec.paths).forEach(p => {
    ["get", "post"].forEach(m => {
      const op = spec.paths[p][m]; if (!op) return;
      const id = m + "_" + p.replace(/\W+/g, "_");
      const params = (op.parameters || []).filter(pa => pa.in !== "path");
      const pathParams = (op.parameters || []).filter(pa => pa.in === "path");
      const prows = params.concat(pathParams).map(pa => `<tr><td style="width:130px"><code>${esc(pa.name)}</code>${pa.in === "path" ? ' <span style="color:var(--muted)">(' + tr("Pfad") + ')</span>' : ''}</td>
        <td>${esc(pa.description || "")}${pa.schema && pa.schema.enum ? ' <span style="color:var(--muted)">('+pa.schema.enum.map(esc).join(" | ")+')</span>' : ''}</td>
        <td style="width:180px"><input data-p="${esc(pa.name)}" data-in="${esc(pa.in)}" data-ep="${id}" placeholder="${pa.schema && pa.schema.enum ? esc(pa.schema.enum[0]) : ''}"></td></tr>`).join("");
      const ptable = prows ? `<table><tr><th>${tr("Parameter")}</th><th>${tr("Bedeutung")}</th><th>${tr("Wert (optional)")}</th></tr>${prows}</table>` : "";
      const bodyBox = (m === "post") ? `<div style="margin-top:6px">
        <label style="font-size:12px;color:var(--muted)">${tr("JSON-Body (optional)")}</label>
        <textarea id="body_${id}" style="width:100%;min-height:70px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px;font-family:monospace;font-size:12px" placeholder='{"comment": "..."}'></textarea></div>` : "";
      box.insertAdjacentHTML("beforeend", `<div class="ep">
        <div class="ephead" onclick="var b=this.nextElementSibling; b.style.display = b.style.display==='none'?'':'none';">
          <span class="method${m === "post" ? " post" : ""}">${m.toUpperCase()}</span><span class="path">${esc(p)}</span>
          <span class="summary">${esc(op.summary || "")}</span></div>
        <div class="epbody" style="display:none">
          <p>${esc(op.description || "")}</p>
          ${ptable}${bodyBox}
          <button class="btn" onclick="run('${id}','${esc(p)}','${m}')">${tr("Ausführen")}</button>
          <div class="status" id="st_${id}"></div>
          <pre id="out_${id}" style="display:none"></pre>
        </div></div>`);
    });
  });
}).catch(() => { document.getElementById("eps").textContent = tr("OpenAPI-Spec nicht ladbar."); });

function run(id, path, method) {
  const st = document.getElementById("st_" + id), out = document.getElementById("out_" + id);
  const qs = [];
  let p2 = path;
  document.querySelectorAll(`input[data-ep="${id}"]`).forEach(i => {
    const v = i.value.trim();
    if (!v) return;
    if (i.dataset.in === "path") p2 = p2.replace("{" + i.dataset.p + "}", encodeURIComponent(v));
    else qs.push(encodeURIComponent(i.dataset.p) + "=" + encodeURIComponent(v));
  });
  const url = BASE + p2.replace(/^\/api\/v1/, "") + (qs.length ? "?" + qs.join("&") : "");
  const h = {};
  if (tokEl.value.trim()) h["Authorization"] = "Bearer " + tokEl.value.trim();
  const opts = { headers: h, method: (method || "get").toUpperCase() };
  const bEl = document.getElementById("body_" + id);
  if (bEl && bEl.value.trim()) { h["Content-Type"] = "application/json"; opts.body = bEl.value; }
  st.textContent = "… " + url;
  fetch(url, opts).then(async r => {
    const ct = r.headers.get("content-type") || "";
    const body = await r.text();
    st.textContent = "HTTP " + r.status + " · " + url;
    out.style.display = "";
    out.textContent = ct.includes("json") ? JSON.stringify(JSON.parse(body), null, 2) : body;
  }).catch(e => { st.textContent = tr("Fehler: ") + e.message; out.style.display = "none"; });
}
</script>
</body>
</html>"""


LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>/* Theme vor dem ersten Rendern setzen (kein Flackern). ?theme=light|dark
   in der URL überstimmt einmalig (z. B. für Kiosk-Anzeigen), sonst gilt die
   gespeicherte Wahl bzw. die Systemeinstellung. */
try { var _t = new URLSearchParams(location.search).get("theme")
            || localStorage.getItem("kapa_theme");
  if (!_t && window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches) _t = "light";
  if (_t === "light") document.documentElement.setAttribute("data-theme", "light");
} catch (e) {}</script>
<title>Anmeldung – VMware Kapazitätsplanung</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --line:#334155; --text:#e2e8f0;
          --muted:#94a3b8; --accent:#38bdf8; --field:#0b1220; --accent-text:#0b1220; }
  html[data-theme="light"] { --bg:#eef2f7; --card:#ffffff; --line:#d4dbe5;
          --text:#1e293b; --muted:#5b6b7f; --accent:#0369a1; --field:#f6f8fb;
          --accent-text:#ffffff; }
  body { background:var(--bg); color:var(--text); font:14px/1.5 "Segoe UI",system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .box { background:var(--card); border:1px solid var(--line); border-radius:12px;
         padding:28px; width:340px; }
  h1 { font-size:17px; margin:0 0 4px; color:var(--accent); }
  p { color:var(--muted); font-size:12px; margin:0 0 16px; }
  label { display:block; font-size:12px; color:var(--muted); margin:12px 0 4px; }
  input { width:100%; box-sizing:border-box; background:var(--field); border:1px solid var(--line);
          color:var(--text); border-radius:6px; padding:8px 10px; font-size:13px; }
  input:focus { outline:none; border-color:var(--accent); }
  button { width:100%; margin-top:18px; background:var(--accent); color:var(--accent-text); border:none;
           border-radius:8px; padding:9px; font-size:13px; font-weight:600; cursor:pointer; }
  .err { color:#ef4444; font-size:12px; margin-top:10px; min-height:16px; }
</style>
</head>
<body>
<form class="box" onsubmit="login(event)">
  <h1>VMware Kapazitätsplanung</h1>
  <p>Anmeldung mit Active-Directory-Konto</p>
  <label>Benutzername</label>
  <input id="u" autocomplete="username" placeholder="__LOGIN_HINT__" autofocus>
  <label>Passwort</label>
  <input id="p" type="password" autocomplete="current-password">
  <button>Anmelden</button>
  <div class="err" id="e"></div>
  <p style="margin:14px 0 0;text-align:center">Version __VERSION__</p>
  <p style="margin:6px 0 0;text-align:center;color:#64748b;font-size:11px">__CONTACT__</p>
</form>
<script>
async function login(ev) {
  ev.preventDefault();
  const e = document.getElementById("e");
  e.textContent = "";
  try {
    const r = await fetch("api/login", { method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ username: document.getElementById("u").value.trim(),
                             password: document.getElementById("p").value }) });
    if (r.ok) { location.reload(); return; }
    e.textContent = tr((await r.json()).error || "Anmeldung fehlgeschlagen.");
  } catch (x) { e.textContent = tr("Server nicht erreichbar."); }
}
// Browsersprache nicht Deutsch -> englische Beschriftung (Werte bleiben intern)
const L_EN = {
  "Anmeldung – VMware Kapazitätsplanung": "Sign in – VMware Capacity Planning",
  "VMware Kapazitätsplanung": "VMware Capacity Planning",
  "Anmeldung mit Active-Directory-Konto": "Sign in with your Active Directory account",
  "Benutzername": "Username", "Passwort": "Password", "Anmelden": "Sign in",
  "vorname.nachname": "first.last",
  "Active-Directory-Benutzername": "Active Directory user name",
  "Anmeldung fehlgeschlagen.": "Sign-in failed.",
  "Server nicht erreichbar.": "Server unreachable.",
  "Benutzername oder Passwort falsch.": "Wrong username or password.",
  "Die Eingabe im Benutzername-Feld sieht wie ein Passwort aus – bitte den Anmeldenamen eintragen.":
    "The input in the username field looks like a password – please enter your sign-in name.",
  "Zu viele Fehlversuche – bitte einige Minuten warten und erneut versuchen.":
    "Too many failed attempts – please wait a few minutes and try again."
};
const IS_DE = (navigator.language || "de").toLowerCase().startsWith("de");
function tr(s) { return IS_DE ? s : (L_EN[(s || "").trim()] || s); }
if (!IS_DE) {
  document.documentElement.lang = "en";
  document.title = tr(document.title);
  document.querySelectorAll("h1,p,label,button").forEach(el => {
    if (!el.children.length) el.textContent = tr(el.textContent);
  });
  const u = document.getElementById("u");
  if (u) u.placeholder = tr(u.placeholder);
}
</script>
</body>
</html>
"""


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


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>/* Theme vor dem ersten Rendern setzen (kein Flackern). ?theme=light|dark
   in der URL überstimmt einmalig (z. B. für Kiosk-Anzeigen), sonst gilt die
   gespeicherte Wahl bzw. die Systemeinstellung. */
try { var _t = new URLSearchParams(location.search).get("theme")
            || localStorage.getItem("kapa_theme");
  if (!_t && window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches) _t = "light";
  if (_t === "light") document.documentElement.setAttribute("data-theme", "light");
} catch (e) {}</script>
<title>VMware Kapazitätsübersicht pro Cluster</title>
<style>
  /* color-scheme koppelt native Controls (Datums-Picker, Kalender-Icon,
     Scrollbars) ans Theme – sonst ist z. B. das Kalender-Icon im Dunkeln
     unsichtbar. */
  :root { color-scheme: dark;
          --bg:#0f172a; --card:#1e293b; --line:#334155; --text:#e2e8f0;
          --muted:#94a3b8; --ok:#22c55e; --warn:#f59e0b; --crit:#ef4444;
          --accent:#38bdf8; --res:#818cf8; --field:#0b1220; --accent-text:#08131f; }
  /* Helles Theme: per Knopf in der Kopfleiste, gespeichert je Benutzer */
  html[data-theme="light"] {
    color-scheme: light;
    --bg:#eef2f7; --card:#ffffff; --line:#d4dbe5; --text:#1e293b;
    --muted:#5b6b7f; --ok:#15803d; --warn:#b45309; --crit:#dc2626;
    --accent:#0369a1; --res:#4f46e5; --field:#f6f8fb; --accent-text:#ffffff; }
  html[data-theme="light"] .btn.primary { color:#ffffff; }
  /* Datumsfelder: ganzes Feld anklickbar (Kalender öffnet), Icon betont */
  input[type=date] { cursor:pointer; }
  input[type=date]::-webkit-calendar-picker-indicator { cursor:pointer; opacity:.85; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text);
         font:14px/1.5 "Segoe UI",system-ui,sans-serif; padding:24px; }
  /* Links (z. B. API-Doku/OpenAPI-Spec) in Akzentfarbe statt Browser-Blau –
     das Standardblau ist im dunklen Theme kaum lesbar. */
  a { color:var(--accent); }
  h1 { font-size:22px; margin-bottom:4px; }
  .sub { color:var(--muted); margin-bottom:20px; }
  .tablewrap { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow-x:auto; }
  .kt { width:100%; border-collapse:collapse; font-size:13px; margin:0; }
  .kt th, .kt td { padding:9px 14px; border-bottom:1px solid var(--line); }
  .kt thead th { color:var(--muted); font-size:12px; background:var(--field); }
  .kt thead th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
  .kt thead th.sortable:hover { color:var(--text); }
  .kt thead th .sarr { opacity:.85; font-size:10px; margin-left:2px; }
  .kt tbody tr:hover td { background:#26334a; }
  .kt tbody tr:last-child td { border-bottom:none; }
  .kt .free { font-weight:600; }
  .trtotal td { background:linear-gradient(135deg,#1e293b,#16233b); font-weight:600; }
  .barcol { width:130px; min-width:110px; }
  .bar.mini { height:8px; }
  .hovercard { position:fixed; z-index:20; width:1180px; max-width:96vw; display:none;
               height:auto; max-height:86vh; min-width:420px; min-height:220px;
               overflow:auto; resize:both; border-radius:12px;
               box-shadow:0 14px 44px rgba(0,0,0,.55); }
  .hovercard .card { border-color:#3b5479; }
  .hovercard .card h2 { cursor:move; user-select:none; }
  .hovercard .card h2::before { content:"⠿ "; color:var(--muted); font-size:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px; }
  .card h2 { font-size:17px; margin-bottom:2px; color:var(--accent); }
  .meta { color:var(--muted); font-size:12px; margin-bottom:12px; }
  .metric { margin-bottom:14px; }
  .metric .row { display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px; }
  .bar { height:10px; background:var(--field); border-radius:5px; overflow:hidden; display:flex; }
  .bar i { display:block; height:100%; }
  .bar .r { background:repeating-linear-gradient(45deg,var(--res),var(--res) 4px,#4f46e5 4px,#4f46e5 8px); }
  .free { font-weight:600; }
  .kpis { display:flex; gap:14px; margin-top:10px; flex-wrap:wrap; }
  .ctabs { margin:0 0 14px; }
  .ctabs .tab { padding:5px 11px; font-size:12px; }
  .tagbox { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
  .tagbox h3 { font-size:12px; color:var(--muted); font-weight:600; margin-bottom:6px; }
  .srcbadge { display:inline-block; font-size:10px; background:var(--field); border:1px solid var(--line);
              border-radius:6px; padding:0 6px; margin-left:6px; color:var(--muted);
              vertical-align:middle; white-space:nowrap; }
  .tag { display:inline-block; background:var(--field); border:1px solid var(--line);
         border-radius:6px; padding:2px 8px; margin:0 4px 4px 0;
         font-size:11px; color:var(--text); }
  .selbar { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
            background:var(--field); border:1px solid var(--line); border-radius:10px;
            padding:10px 14px; margin-bottom:12px; }
  .selbar .sellabel { font-size:12px; color:var(--muted); font-weight:600; }
  .selbar label { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); }
  .selbar select { background:var(--bg); border:1px solid var(--line); color:var(--text);
                   border-radius:6px; padding:5px 8px; font-size:13px; }
  .selbar .btn { padding:5px 10px; }
  .netbox { margin-top:12px; }
  .netbox:first-child { margin-top:0; }
  .netbox h3 { font-size:13px; color:var(--text); margin-bottom:6px; }
  .pgbar { display:none; align-items:center; gap:10px; margin-top:10px; font-size:13px; flex-wrap:wrap; }
  .pgbar select { width:auto; padding:4px 6px; }
  .vlanbar { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
  .vlanbar input { flex:1; max-width:520px; background:var(--bg); border:1px solid var(--line);
                   color:var(--text); border-radius:8px; padding:9px 12px; font-size:14px; }
  .kpi { background:var(--field); border-radius:8px; padding:8px 12px; font-size:12px; color:var(--muted); }
  .kpi b { display:block; font-size:16px; color:var(--text); }
  details { margin-top:12px; }
  summary { cursor:pointer; color:var(--muted); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  td.num, th.num { text-align:right; }
  .off { color:var(--muted); font-style:italic; }
  .total { background:linear-gradient(135deg,#1e293b,#16233b); border-color:#3b5479; }
  .toolbar { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; align-items:center; }
  .filterbox { background:var(--field); border:1px solid var(--line); color:var(--text);
               border-radius:8px; padding:6px 12px; font-size:12px; width:220px; }
  .filterbox:focus { outline:none; border-color:var(--accent); }
  .tabs { display:inline-flex; background:var(--field); border:1px solid var(--line);
          border-radius:10px; padding:3px; gap:3px; margin-bottom:16px; }
  .tab { padding:6px 14px; font-size:13px; color:var(--muted); cursor:pointer; border-radius:8px; }
  .tab.active { background:var(--card); color:var(--text); }
  .subtabs { margin-bottom:18px; }
  .colmenu { position:relative; display:inline-block; font-size:12px; }
  /* In der Toolbar sitzt das ▾-Dropdown neben Buttons (.btn hat margin-top:6px);
     dieselbe Margin gibt beiden im Flex-Layout die gleiche vertikale Ausrichtung,
     damit das Dropdown nicht tiefer sitzt als der Knopf daneben. */
  .toolbar .colmenu { margin-top:6px; }
  .colmenu > summary { cursor:pointer; color:var(--muted); list-style:none; user-select:none;
                       border:1px solid var(--line); border-radius:8px; padding:5px 10px; background:var(--field); }
  .colmenu > summary::-webkit-details-marker { display:none; }
  .colmenu[open] > summary { color:var(--text); }
  .colmenu > div { position:absolute; z-index:16; top:calc(100% + 4px); right:0; min-width:180px;
                   background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px 10px;
                   box-shadow:0 10px 30px rgba(0,0,0,.5); max-height:340px; overflow:auto; }
  .colmenu label { display:block; padding:3px 2px; white-space:nowrap; color:var(--text); cursor:pointer; }
  .colmenu input[type=checkbox] { margin-right:6px; }
  .cl { color:var(--accent); cursor:pointer; }
  .cl:hover { text-decoration:underline; }
  .st { font-size:11px; padding:2px 8px; border-radius:10px; white-space:nowrap; }
  .st.ok { background:rgba(34,197,94,.15); color:var(--ok); }
  .st.pend { background:rgba(245,158,11,.15); color:var(--warn); }
  .st.prog { background:rgba(56,189,248,.15); color:#38bdf8; cursor:help; }
  .st.rej { background:rgba(239,68,68,.15); color:var(--crit); }
  .st.canc { background:rgba(148,163,184,.18); color:var(--muted); cursor:help; }
  .rid { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px;
         color:var(--muted); white-space:nowrap; }
  .btn.approve { border-color:var(--ok); color:var(--ok); }
  .btn.approve:hover { background:rgba(34,197,94,.15); }
  .btn.danger { border-color:var(--crit); color:var(--crit); font-weight:600; }
  .btn.danger:hover { background:rgba(239,68,68,.15); }
  .hc-close { position:absolute; top:10px; right:12px; z-index:1;
              background:none; border:none; color:var(--muted); cursor:pointer; font-size:14px; }
  .hc-close:hover { color:var(--text); }
  .foot { margin-top:28px; text-align:center; color:var(--muted); font-size:11px; }
  .sechead { font-size:13px; color:var(--muted); margin-bottom:8px; }
  .btn { background:var(--field); border:1px solid var(--line); color:var(--text);
         border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
  .btn:hover { border-color:var(--accent); }
  a.btn { text-decoration:none; display:inline-block; line-height:normal; }
  .resbox { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
  .resbox h3 { font-size:13px; color:var(--res); margin-bottom:6px; }
  .resform { display:grid; grid-template-columns:2fr 1.4fr 80px 90px 90px; gap:6px; margin-top:8px; }
  .resform input { background:var(--field); border:1px solid var(--line); color:var(--text);
                   border-radius:6px; padding:5px 8px; font-size:12px; width:100%; }
  .resform input:focus { outline:none; border-color:var(--res); }
  .resform button { grid-column:1 / -1; }   /* Beantragen-Knopf über die volle Breite */
  .del { background:none; border:none; color:var(--crit); cursor:pointer; font-size:13px; }
  .edit { background:none; border:none; color:var(--accent); cursor:pointer; font-size:13px; }
  .err { color:var(--crit); font-size:12px; margin-top:4px; display:none; }
  .btn.primary { background:var(--res); color:var(--field); border-color:var(--res); font-weight:600; }
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,.65); display:none;
              align-items:center; justify-content:center; z-index:10; }
  .modal-bg.open { display:flex; }
  .modal { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:22px; width:440px; max-width:92vw; }
  .modal h2 { color:var(--res); font-size:16px; margin-bottom:8px; }
  .modal label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  .modal input, .modal select, .modal textarea { width:100%; box-sizing:border-box;
           background:var(--field); border:1px solid var(--line); color:var(--text);
           border-radius:6px; padding:7px 9px; font-size:13px; font-family:inherit; resize:vertical; }
  .modal input:focus, .modal select:focus, .modal textarea:focus { outline:none; border-color:var(--res); }
  .modal .hint { font-size:12px; color:var(--accent); margin-top:10px; }
  .modal .actions { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
</style>
</head>
<body>
<h1>Kapazitätsübersicht pro Cluster</h1>
<!-- Benutzer / Theme / Abmelden: fest oben rechts, gegenüber von Info & Hilfe -->
<div id="userarea" style="position:absolute;top:22px;right:24px;display:flex;gap:8px;align-items:center">
  <span id="userbox" style="font-size:12px;color:var(--muted)"></span>
  <button class="btn" id="themeBtn" onclick="toggleTheme()" title="Hell/Dunkel umschalten">☀️</button>
  <button class="btn" id="logoutBtn" style="display:none" onclick="logout()">Abmelden</button>
</div>
<div class="sub">
  <button class="btn" onclick="showInfo('infoCalc','Info Kapa-Berechnung')">ℹ Info Kapa-Berechnung</button>
  <button class="btn" onclick="showInfo('infoHelp','Hilfe')">? Hilfe</button>
  <span style="color:var(--muted);font-size:12px;margin-left:8px">Stand: <span id="stand">__DATE__</span></span>
</div>
<div id="infoCalc" style="display:none">Quelle: VMware Aria Operations · CPU-Überprovisionierung: Faktor __FACTOR__ (physische Cores) · RAM 1:1 · alle VMs inkl. powered-off · „frei" berücksichtigt genehmigte Reservierungen__FAILNOTE__</div>
<div id="infoHelp" style="display:none">Klick auf den Clusternamen zeigt Details und Reservierungen. __RESNOTE__<div style="margin-top:12px">📖 <a href="reviewer-handbuch" target="_blank" rel="noopener">Reviewer-Handbuch öffnen</a></div></div>
<div class="modal-bg" id="infoBg" onclick="if(event.target===this)closeInfo()">
  <div class="modal">
    <h2 id="infoTitle"></h2>
    <div id="infoBody" style="font-size:13px;line-height:1.6;color:var(--text)"></div>
    <div class="actions"><button class="btn primary" onclick="closeInfo()">Schließen</button></div>
  </div>
</div>
<div class="modal-bg" id="annBg">
  <div class="modal" style="max-width:520px">
    <h2 id="annTitle" style="display:flex;align-items:center;gap:8px">📣 <span></span></h2>
    <div id="annBody" style="font-size:13px;line-height:1.6;color:var(--text);white-space:pre-wrap"></div>
    <div class="actions"><button class="btn primary" onclick="closeAnnounce()">Verstanden</button></div>
  </div>
</div>
<div class="toolbar">
  <input class="filterbox" id="filter" type="search" placeholder="Cluster filtern …" oninput="pageReset({kapa:'ktable',app:'atable',log:'ltable'}[VIEW]);render()">
  <button class="btn primary" id="newReqBtn" onclick="openModal()">+ Neue Kapazitätsanfrage</button>
  <button class="btn" id="refreshBtn" onclick="refreshData()">⟳ Jetzt aktualisieren</button>
  <details class="colmenu" id="refreshMenu" style="display:none"><summary>▾</summary>
    <div>
      <label><a href="#" onclick="refreshData();this.closest('details').open=false;return false">Alles aktualisieren</a></label>
      <label><a href="#" onclick="refreshData(['vms']);this.closest('details').open=false;return false">Nur Kapazität (VMs) <span id="tierVms" style="color:var(--muted)"></span></a></label>
      <label><a href="#" onclick="refreshData(['network']);this.closest('details').open=false;return false">Nur Netzwerk <span id="tierNetwork" style="color:var(--muted)"></span></a></label>
      <label><a href="#" onclick="refreshData(['storage']);this.closest('details').open=false;return false">Nur Storage <span id="tierStorage" style="color:var(--muted)"></span></a></label>
    </div></details>
  <span id="refreshStatus" style="font-size:12px;color:var(--muted)"></span>
  <span id="timer" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
  <a class="btn" id="csvBtn" href="api/v1/reservations?format=csv"
     download="reservierungen.csv" title="Reservierungen als CSV (Semikolon, für Excel)">CSV exportieren</a>
  <button class="btn" onclick="exportRes()">Reservierungen exportieren (JSON)</button>
  <label class="btn" id="importBtn">Reservierungen importieren (JSON)<input type="file" accept=".json" hidden onchange="importRes(event)"></label>
</div>
<div class="modal-bg" id="modalBg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h2>Neue Kapazitätsanfrage</h2>
    <label>Ziel-Cluster</label>
    <input id="mClusterSearch" type="search" placeholder="Cluster suchen …"
           oninput="fillClusterSelect()" autocomplete="off" style="margin-bottom:6px">
    <select id="mCluster" size="1" onchange="modalHint()"></select>
    <label>Bezeichnung / Projekt</label>
    <input id="mName" placeholder="z. B. SAP-Erweiterung Q4">
    <label>Change / Jira Ticket (optional)</label>
    <input id="mChange">
    <label>vCPU</label>
    <input id="mCpu" type="number" min="0" step="1" placeholder="0">
    <label>RAM (GB)</label>
    <input id="mRam" type="number" min="0" step="1" placeholder="0">
    <label>Storage (GB)</label>
    <input id="mStorage" type="number" min="0" step="1" placeholder="0">
    <div class="hint" id="mHint"></div>
    <div class="hint" id="mValid" style="color:var(--muted)"></div>
    <div class="err" id="mErr"></div>
    <div class="actions">
      <button class="btn" onclick="closeModal()">Abbrechen</button>
      <button class="btn primary" onclick="submitModal()">Beantragen</button>
    </div>
  </div>
</div>
<div class="modal-bg" id="cmtBg" onclick="if(event.target===this)cmtCancel()">
  <div class="modal">
    <h2 id="cmtTitle">Kommentar</h2>
    <div class="hint" id="cmtMsg" style="color:var(--muted);margin-bottom:10px"></div>
    <label>Kommentar (optional)</label>
    <textarea id="cmtInput" maxlength="64" rows="3" oninput="cmtCount()"
              placeholder="kurze Begründung, max. 64 Zeichen"></textarea>
    <div class="hint" id="cmtCnt" style="color:var(--muted);text-align:right"></div>
    <div class="actions">
      <button class="btn" id="cmtExtra" style="display:none;margin-right:auto" onclick="cmtExtra()"></button>
      <button class="btn" onclick="cmtCancel()">Abbrechen</button>
      <button class="btn primary" id="cmtOk" onclick="cmtConfirm()">OK</button>
    </div>
  </div>
</div>
<div class="modal-bg" id="askBg" onclick="if(event.target===this)askClose(false)">
  <div class="modal">
    <h2 id="askTitle"></h2>
    <div class="hint" id="askMsg" style="color:var(--muted);margin-bottom:16px;white-space:pre-line"></div>
    <div class="actions">
      <button class="btn" id="askCancel" onclick="askClose(false)">Abbrechen</button>
      <button class="btn primary" id="askOk" onclick="askClose(true)">OK</button>
    </div>
  </div>
</div>
<div class="tabs">
  <span class="tab active" id="tabKapa" onclick="setView('kapa')">Kapazität</span>
  <span class="tab" id="tabVlan" onclick="setView('vlan')">VLAN-Suche</span>
  <span class="tab" id="tabStor" onclick="setView('stor')">Storage</span>
  <span class="tab" id="tabRes" onclick="setView('res')">Reservierungen</span>
  <span class="tab" id="tabApp" onclick="setView('app')">Genehmigungen</span>
  <span class="tab" id="tabArch" onclick="setView('arch')">Archiv</span>
  <span class="tab" id="tabStat" onclick="setView('stat')">Statistik</span>
  <span class="tab" id="tabAdm" onclick="setView('adm')">Verwaltung</span>
  <span class="tab" id="tabLog" onclick="setView('log')">Log</span>
</div>
<div id="kapaView">
<div class="selbar" id="clusterSelector" style="display:none"></div>
<div style="text-align:right;margin-bottom:8px">
  <a class="btn" href="api/v1/data?format=csv" download="kapazitaet.csv"
     title="Kapazitätstabelle als CSV (Semikolon, für Excel) – inkl. effektiv freier Werte">Kapazität als CSV</a>
  <span id="colctl_ktable"></span></div>
<div class="tablewrap">
<table class="kt" id="ktable">
  <thead><tr><th>Cluster</th><th class="num">Hosts</th><th class="num">VMs</th>
    <th class="num">vCPU frei</th><th class="barcol">vCPU-Auslastung</th>
    <th class="num">RAM frei (GB)</th><th class="barcol">RAM-Auslastung</th>
    <th class="num">Storage frei (GB)</th><th class="barcol">Storage-Auslastung</th>
    <th class="num">Res.</th></tr></thead>
  <tbody id="ktbody"></tbody>
</table>
</div>
<div class="pgbar" id="pager_ktable"></div>
</div>
<div id="vlanView" style="display:none">
<div class="hint" style="color:var(--muted);margin:4px 0 10px">
  Portgruppen aller Cluster durchsuchen – z. B. nach einer IP-Adresse oder
  einem Netz aus dem Portgruppen-Namen. Das Ergebnis zeigt, an welchem Cluster
  das Netz hängt. Teil-Eingaben genügen
  (z. B. <code>10.2.30</code> oder <code>VLAN205</code>).</div>
<div class="vlanbar">
  <input id="vlanQ" placeholder="IP-Adresse, Netz oder Portgruppen-Name suchen …"
         oninput="pageReset('vtable');renderVlan()" autocomplete="off">
  <span id="vlanCount" style="color:var(--muted);font-size:13px"></span>
  <span id="colctl_vtable" style="margin-left:auto"></span>
</div>
<div class="tablewrap">
<table class="kt" id="vtable">
  <thead><tr><th>Portgruppe</th><th class="num">VLAN</th><th>Cluster</th></tr></thead>
  <tbody id="vtbody"></tbody>
</table>
</div>
<div class="pgbar" id="pager_vtable"></div>
</div>
<div id="resView" style="display:none">
<div class="vlanbar" style="margin-bottom:12px">
  <input id="resSearch" class="filterbox" style="max-width:520px" type="search"
         placeholder="Reservierungen durchsuchen – Name, Cluster, Change, Anforderer, Team, ID, Status …"
         oninput="pageReset('rtable');renderResTable()" autocomplete="off">
  <span id="resCount" style="color:var(--muted);font-size:13px"></span>
  <span id="colctl_rtable" style="margin-left:auto"></span>
</div>
<div class="tablewrap">
<table class="kt" id="rtable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>von</th><th>Team</th><th>gilt ab</th><th>gültig bis</th><th>Status</th><th id="thDec">entschieden von</th><th>Kommentar</th><th class="nosort"></th></tr></thead>
  <tbody id="rtbody"></tbody>
</table>
</div>
<div class="pgbar" id="pager_rtable"></div>
</div>
<div id="appView" style="display:none">
<div style="display:flex;align-items:center;margin-bottom:8px;font-size:13px">
  <span>📖 <a href="reviewer-handbuch" target="_blank" rel="noopener">Reviewer-Handbuch</a>
    <span style="color:var(--muted)">– wie Prüfen &amp; Freigeben funktioniert</span></span>
  <span id="colctl_atable" style="margin-left:auto"></span>
</div>
<div class="tablewrap">
<table class="kt" id="atable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th>
    <th class="num" title="Frei im Ziel-Cluster nach genehmigten Reservierungen">Cluster frei vCPU</th>
    <th class="num" title="Frei im Ziel-Cluster nach genehmigten Reservierungen">Cluster frei RAM</th>
    <th>von</th><th>Team</th><th>beantragt am</th><th>Fortschritt</th><th class="nosort">Aktion</th></tr></thead>
  <tbody id="atbody"></tbody>
</table>
</div>
<div class="pgbar" id="pager_atable"></div>
</div>
<div id="archView" style="display:none">
<div class="hint" style="color:var(--muted);margin:4px 0 10px">
  Archiv der <b>abgelehnten</b> und <b>stornierten</b> Kapazitätsanfragen (Historie,
  zählt nicht gegen die Kapazität). Sichtbarkeit wie bei den Reservierungen:
  Anforderer sehen die des eigenen Teams, Reviewer/Admin/Auditor alle.</div>
<div class="vlanbar" style="margin-bottom:12px">
  <input id="archSearch" class="filterbox" style="max-width:520px" type="search"
         placeholder="Archiv durchsuchen – Name, Cluster, Change, Anforderer, Team, ID, Status …"
         oninput="pageReset('artable');renderArchiveTable()" autocomplete="off">
  <span id="archCount" style="color:var(--muted);font-size:13px"></span>
  <span id="colctl_artable" style="margin-left:auto"></span>
</div>
<div class="tablewrap">
<table class="kt" id="artable">
  <thead><tr><th>ID</th><th>Anfrage / Projekt</th><th>Cluster</th><th>Change</th><th class="num">vCPU</th>
    <th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>von</th><th>Team</th>
    <th>angelegt</th><th>erledigt am</th><th>Status</th><th>durch</th><th>Kommentar</th></tr></thead>
  <tbody id="arbody"></tbody>
</table>
</div>
<div class="pgbar" id="pager_artable"></div>
</div>
<div id="statView" style="display:none">
<div class="hint" style="color:var(--muted);margin:4px 0 10px">
  Trends aus täglichen Snapshots der Datensammlung — z. B. ob VMs im Schnitt
  <b>größer</b> werden (RAM/Disk je VM). Die Historie wächst ab Einbau dieser
  Funktion; ältere Zeiträume füllen sich mit der Zeit.</div>
<div class="toolbar" style="margin-bottom:12px;flex-wrap:wrap">
  <label style="font-size:12px;color:var(--muted)">Zeitraum
    <select id="statRange" class="filterbox" style="margin-left:4px" onchange="renderStats()">
      <option value="30">30 Tage</option><option value="90">90 Tage</option>
      <option value="180">180 Tage</option><option value="365" selected>1 Jahr</option>
      <option value="730">2 Jahre</option></select></label>
  <label style="font-size:12px;color:var(--muted)">Cluster
    <select id="statCluster" class="filterbox" style="margin-left:4px;max-width:260px" onchange="renderStats()">
      <option value="">alle</option></select></label>
  <a class="btn" id="statCsvBtn" href="api/history?days=730&format=csv"
     download="kapa-statistik.csv" title="Historie als CSV (Semikolon, für Excel)">CSV exportieren</a>
  <span id="statInfo" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
</div>
<div id="statCharts" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px"></div>
</div>
<div id="storView" style="display:none">
<div class="hint" style="color:var(--muted);margin:4px 0 10px">
  Alle Datastores/LUNs über alle Cluster. <b>Neu angefragte Erweiterungen</b>
  sind hervorgehoben; das Storage-Team ruft sie per API ab
  (<code>/api/v1/storage-requests</code>, auch als CSV inkl. NAA).</div>
<div class="toolbar" style="margin-bottom:10px">
  <input class="filterbox" id="storFilter" type="search" placeholder="Storage filtern – Cluster, LUN, NAA, Typ …" oninput="pageReset('stortable');renderStorage()">
  <a class="btn" id="storCsvBtn" href="api/v1/storage-requests?format=csv&status=alle" download="storage-anfragen.csv" title="Storage-Anfragen als CSV (inkl. NAA)">Anfragen als CSV</a>
  <span id="storCount" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
  <span id="colctl_stortable"></span>
</div>
<div id="storReqBox"></div>
<div class="tablewrap">
<table class="kt" id="stortable">
  <thead><tr><th>Cluster</th><th>Datastore / LUN</th><th>Typ</th><th>NAA</th>
    <th class="num">Größe (GB)</th><th class="num">Belegt (GB)</th>
    <th class="num">Frei (GB)</th><th class="nosort">Erweiterung</th></tr></thead>
  <tbody id="storbody"></tbody>
</table>
</div>
<div class="pgbar" id="pager_stortable"></div>
</div>

<div id="admView" style="display:none">
<div class="tabs subtabs">
  <span class="tab active" id="atabUsers" onclick="setAdmTab('users')">Benutzer &amp; Rollen</span>
  <span class="tab" id="atabSel" onclick="setAdmTab('sel')">Cluster-Selektor</span>
  <span class="tab" id="atabMail" onclick="setAdmTab('mail')">Mail</span>
  <span class="tab" id="atabHooks" onclick="setAdmTab('hooks')">Webhooks</span>
  <span class="tab" id="atabAnn" onclick="setAdmTab('ann')">Ankündigung</span>
  <span class="tab" id="atabAuto" onclick="setAdmTab('auto')">Freigabe</span>
  <span class="tab" id="atabVis" onclick="setAdmTab('vis')">Sichtbarkeit</span>
  <span class="tab" id="atabStorCfg" onclick="setAdmTab('storcfg')">Storage</span>
  <span class="tab" id="atabNet" onclick="setAdmTab('net')">Netzwerk</span>
  <span class="tab" id="atabImp" onclick="setAdmTab('imp')">Import</span>
  <span class="tab" id="atabTok" onclick="setAdmTab('tok')">API-Tokens</span>
  <span class="tab" id="atabConf" onclick="setAdmTab('conf')">Backup &amp; Konfiguration</span>
</div>

<div id="admGrpHooks" style="display:none">
<div class="sechead">Webhooks (Ereignisse an externe Systeme)</div>
<div class="hint" style="color:var(--muted);margin-bottom:10px">
  Bei einem Ereignis schickt das Dashboard einen <b>HTTP-POST mit JSON</b> an die
  hinterlegten Ziele — für ITSM (ServiceNow/Jira), Automatisierung (Ansible AWX,
  n8n), Pipelines (GitLab/GitHub) oder Chat (Slack/Teams). Jeder Payload wird mit
  dem Secret <b>HMAC-SHA256-signiert</b> (Header <code>X-Kapa-Signature</code>),
  damit der Empfänger die Echtheit prüfen kann. Secrets werden nie zurück
  angezeigt und liegen bewusst <b>nicht im Backup</b>.</div>
<table class="kt" id="whtable" style="margin-bottom:10px">
  <thead><tr><th>Aktiv</th><th>Ziel-URL</th><th>Ereignisse</th>
    <th>Secret</th><th>Beschreibung</th><th class="nosort"></th></tr></thead>
  <tbody id="whbody"></tbody>
</table>
<button class="btn" onclick="addWebhook()">+ Webhook hinzufügen</button>
<button class="btn approve" onclick="saveWebhooks()">✓ Webhooks speichern</button>
<span id="whSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div>

<div id="admGrpAnn" style="display:none">
<div class="sechead">Ankündigung (Popup nach der Anmeldung)</div>
<div class="hint" style="color:var(--muted);margin-bottom:10px">
  Ist die Ankündigung aktiv, sieht jeder Benutzer sie <b>einmal</b> als Popup –
  nach dem Klick auf „Verstanden" erscheint sie nicht erneut. Eine Änderung an
  Titel oder Text zeigt sie allen Benutzern noch einmal. Beispiele: Neues aus
  einem Release, neue Datacenter/Cluster, Wartungsfenster.</div>
<label style="font-size:12px;color:var(--muted)">Titel</label>
<input id="annCfgTitle" class="filterbox" style="width:100%;max-width:520px;margin-bottom:10px"
       placeholder="z. B. Neu: Datacenter RZ-Sued verfügbar">
<label style="font-size:12px;color:var(--muted)">Text</label>
<textarea id="annCfgText" style="width:100%;max-width:640px;min-height:140px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:10px;font-size:13px;line-height:1.5"
          placeholder="Der Text des Popups (Zeilenumbrüche bleiben erhalten, kein HTML)."></textarea>
<div style="margin-top:10px">
  <label style="font-size:13px"><input type="checkbox" id="annCfgActive"> aktiv – Popup wird angezeigt</label>
</div>
<div style="margin-top:10px">
  <button class="btn approve" onclick="saveAnnounce()">✓ Ankündigung speichern</button>
  <button class="btn" onclick="previewAnnounce()">Vorschau</button>
  <span id="annSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div>
<div id="annMeta" style="color:var(--muted);font-size:12px;margin-top:8px"></div>
</div><!-- admGrpAnn -->

<div id="admGrpUsers">
<div class="sechead">Benutzer und Rollen</div>
<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
  <input class="filterbox" id="admUserFilter" type="search" style="max-width:280px"
         placeholder="Benutzer filtern …" oninput="render()">
  <span id="colctl_mtable" style="margin-left:auto"></span>
</div>
<div class="tablewrap">
<table class="kt" id="mtable">
  <thead><tr><th>Typ</th><th>Benutzer / AD-Gruppe</th><th>Rolle</th><th>Team</th><th class="srcCol nosort">vROps-Quellen</th><th class="nosort">Aktion</th></tr></thead>
  <tbody id="mtbody"></tbody>
</table>
</div>
<div class="sechead" style="margin-top:20px">Rollen-Bezeichnungen</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Die angezeigten Namen der Rollen sind frei wählbar. Die Rechte bleiben an der
  internen Rolle (linke Spalte) gebunden und ändern sich dadurch nicht.</div>
<div class="tablewrap">
<table class="kt" id="rntable">
  <thead><tr><th style="width:160px">Interne Rolle</th><th>Angezeigte Bezeichnung</th></tr></thead>
  <tbody id="rnbody"></tbody>
</table>
</div>
<div class="sechead" style="margin-top:20px">Genehmigungs-Teams (Prüfreihenfolge)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Anträge durchlaufen die Teams von oben nach unten. Erst wenn alle Teams
  freigegeben haben, ist ein Antrag genehmigt. Ohne Teams gilt einstufig
  (Admin genehmigt direkt). Reviewer werden oben ihrem Team zugewiesen.
  Die <b>E-Mail/Verteiler</b> je Team wird angeschrieben, sobald das Team im
  Workflow an der Reihe ist (siehe „Mail-Benachrichtigungen"); mit „✓ Team-Adressen
  speichern" sichern.</div>
<div class="tablewrap">
<table class="kt" id="tmtable">
  <thead><tr><th style="width:60px">Stufe</th><th>Team</th><th>E-Mail / Verteiler (Team ist dran)</th><th style="width:220px" class="nosort">Aktion</th></tr></thead>
  <tbody id="tmbody"></tbody>
</table>
</div>
<button class="btn approve" style="margin-top:8px" onclick="saveNotify()">✓ Team-Adressen speichern</button>
</div><!-- admGrpUsers -->

<div id="admGrpSel" style="display:none">
<div class="sechead">Cluster-Selektor (Filter nach vSphere-Tags)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Bis zu 3 Stufen, jede Stufe eine Tag-Kategorie. In der Kapazitätsübersicht
  erscheinen dann kaskadierende Auswahllisten (Stufe 2 zeigt nur Werte, die zur
  Wahl in Stufe 1 passen). Die Kategorien kommen aus den vorhandenen
  Cluster-Tags – sind noch keine Daten geladen, ist die Liste leer.</div>
<div class="tablewrap">
<table class="kt" id="seltable">
  <thead><tr><th style="width:60px">Stufe</th><th style="width:320px">Tag-Kategorie</th><th>Anzeigename im Selektor</th></tr></thead>
  <tbody id="selbody"></tbody>
</table>
</div>
</div><!-- admGrpSel -->

<div id="admGrpAuto" style="display:none">
<div class="sechead">Auto-Freigabe (Schwellenwerte)</div>
<div class="hint" style="color:var(--muted);margin-bottom:10px">
  Erfüllt der Ziel-Cluster <b>nach</b> Abzug des Antrags alle Schwellen, gibt
  das System markierte Stufen automatisch frei (Freigebender:
  „Auto-Freigabe", vollständig im Audit-Log). Geprüft wird bei der
  Antragstellung und immer, wenn eine Stufe neu an der Reihe ist. Greift eine
  Schwelle nicht oder fehlen Daten (z. B. kein Workload-Wert), geht der Antrag
  ganz normal an das Team — die Auto-Freigabe lehnt nie ab.</div>
<div style="margin-bottom:12px">
  <label style="font-size:13px"><input type="checkbox" id="aaEnabled"> aktiv – Auto-Freigabe einschalten</label>
</div>
<table class="kt" style="max-width:560px">
  <tr><td>vCPU frei mindestens</td>
      <td class="num"><input type="number" id="aaCpu" min="0" max="100" class="aanum"> %</td></tr>
  <tr><td>RAM frei mindestens</td>
      <td class="num"><input type="number" id="aaRam" min="0" max="100" class="aanum"> %</td></tr>
  <tr><td>Größte freie LUN mindestens frei</td>
      <td class="num"><input type="number" id="aaLun" min="0" max="100" class="aanum"> %</td></tr>
  <tr><td>Workload höchstens</td>
      <td class="num"><input type="number" id="aaWl" min="0" max="100" class="aanum"> %</td></tr>
</table>
<div class="sechead" style="margin-top:18px">Stufen mit Auto-Freigabe</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Nur angehakte Teams werden automatisch freigegeben — z. B. Team 1 prüft
  manuell, die weiteren Stufen laufen automatisch durch. Ohne Teams gilt der
  Haken sinngemäß für die einstufige Freigabe.</div>
<div id="aaTeams" style="margin-bottom:12px"></div>
<button class="btn approve" onclick="saveAutoApprove()">✓ Auto-Freigabe speichern</button>
<span id="aaSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div><!-- admGrpAuto -->

<div id="admGrpVis" style="display:none">
<div class="sechead">Sichtbarkeit (was sieht welche Rolle)</div>
<div class="hint" style="color:var(--muted);margin-bottom:10px">
  Haken = die Rolle sieht das Merkmal. Wirkt im UI <b>und</b> im Datenpaket
  (serverseitig entfernt). Administratoren sehen immer alles. Hier geht es nur
  um <b>Sichtbarkeit</b> — Rechte (Genehmigen, Verwaltung, Team-Sicht der
  Anforderer) bleiben fest an den Rollen.</div>
<div class="tablewrap" style="max-width:720px">
<table class="kt" id="vistable">
  <thead id="vishead"></thead>
  <tbody id="visbody"></tbody>
</table>
</div>
<button class="btn approve" style="margin-top:10px" onclick="saveVisibility()">✓ Sichtbarkeit speichern</button>
<span id="visSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div><!-- admGrpVis -->

<div id="admGrpStorCfg" style="display:none">
<div class="sechead">Storage-Erweiterungen</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Ist dies aktiv, können Freigebende beim Genehmigen und alle Berechtigten in
  der Storage-Übersicht eine LUN-Vergrößerung oder eine neue LUN anfragen. Das
  Storage-Team ruft die offenen Anfragen per API ab
  (<code>/api/v1/storage-requests</code>, auch CSV inkl. NAA) und meldet mit
  einem Token-Schreibrecht „Storage" die Umsetzung zurück.</div>
<div style="margin-bottom:18px">
  <label style="font-size:13px"><input type="checkbox" id="storEnabled" onchange="saveStorageCfg()"> Storage-Erweiterungen erlauben</label>
  <span id="storCfgSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div>
<div class="sechead">Mindest-LUN-Größe</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Datastores <b>kleiner</b> als dieser Wert werden <b>komplett</b> aus der
  Auswertung genommen — sie erscheinen nirgends (Storage-Übersicht, Cluster-
  Detail) und zählen auch <b>nicht</b> in die Storage-Kapazität/Auslastung.
  Praktisch, um kleine Boot-/ISO-/Scratch-Datastores auszublenden. 0 = alle
  anzeigen. Die Änderung löst gleich einen neuen Datenabruf aus.</div>
<div style="margin-bottom:16px">
  Mindestgröße: <input id="storMinLun" type="number" min="0" step="10"
    style="width:110px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:4px 6px;text-align:center"> GB
</div>
<div class="sechead">Namensfilter</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Datastores, deren <b>Name</b> einen dieser Begriffe enthält, werden ebenfalls
  <b>komplett</b> ausgeschlossen (überall, inkl. Kapazität). Mehrere durch Komma
  trennen, Groß-/Kleinschreibung egal — z. B. <code>iso, backup, scratch</code>.
  Ein Begriff wirkt als Teiltreffer (<code>service</code> erwischt auch
  <code>server-service-01</code>); <code>*</code>/<code>?</code> gehen als
  Platzhalter (<code>*-iso</code>, <code>lun-??-tmp</code>). Leer = kein Filter.</div>
<div style="margin-bottom:16px">
  <input id="storExclNames" class="filterbox" style="width:100%;max-width:520px"
    placeholder="z. B. iso, backup, template">
</div>
<div class="sechead">Maximale LUN-Größe (Anfrage-Limit)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Obergrenze für Storage-Anfragen (Vergrößerung und neue LUN). Größere
  Wünsche werden abgelehnt. Als internes Limit gedacht — Randnotiz: VMFS-6
  unterstützt ohnehin höchstens <b>64 TB</b> je Datastore. 0 = kein Limit.</div>
<div style="margin-bottom:16px">
  Maximum: <input id="storMaxLun" type="number" min="0" step="1"
    style="width:110px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:4px 6px;text-align:center"> TB
</div>
<div class="sechead">vSAN-Kapazität (Policy-Faktor)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  vROps liefert für vSAN <b>keinen</b> sauberen Nutzbar-nach-RAID-Wert, nur die
  <b>physische</b> Roh- und Belegt-Kapazität. Die <b>Auslastung in %</b> stimmt
  damit ohnehin; der Faktor rechnet nur die <b>absolute nutzbare</b> Kapazität aus
  der Policy (Annahme): RAID-1 ≈ 50 %, RAID-5 ≈ 75 % (3+1) bzw. 67 % (2+1),
  RAID-6 ≈ 67 %. Bei gemischten Policies je Cluster den passenden Standard wählen.</div>
<div style="margin-bottom:10px">
  Standard-Policy:
  <select id="vsanPolicy" class="filterbox" style="width:auto" onchange="vsanPresetToFactor('vsanPolicy','vsanFactor')"></select>
  &nbsp;Faktor: <input id="vsanFactor" type="number" min="0.05" max="1" step="0.01"
    style="width:90px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:4px 6px;text-align:center"
    oninput="vsanFactorToPreset('vsanFactor','vsanPolicy')">
  <span style="color:var(--muted);font-size:12px">(0,05–1,0 · z. B. 0,5 = 50 % nutzbar)</span>
</div>
<div class="hint" style="color:var(--muted);margin:4px 0 6px">Override je Cluster (optional – für abweichende Policies):</div>
<table class="kt" id="vsanOvrTable" style="max-width:560px;margin-bottom:8px">
  <thead><tr><th>Cluster</th><th>Policy</th><th class="num">Faktor</th><th class="nosort"></th></tr></thead>
  <tbody id="vsanOvrBody"></tbody>
</table>
<button class="btn" onclick="addVsanOverride()">+ Cluster-Override</button>
<div style="margin-top:14px"></div>
<button class="btn approve" onclick="saveStorageCfg()">✓ Speichern &amp; anwenden</button>
<span id="storMinSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div><!-- admGrpStorCfg -->

<div id="admGrpNet" style="display:none">
<div class="sechead">Netzwerk-Filter (Portgruppen ausblenden)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Portgruppen, die hier zutreffen, werden <b>komplett</b> ausgeblendet —
  in der VLAN-Suche, im Netzwerk-Reiter der Cluster-Details und im
  Datenpaket. Die Änderung löst gleich einen neuen Datenabruf aus.</div>
<div class="sechead">Namensfilter</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Portgruppen, deren <b>Name</b> einen dieser Begriffe enthält. Mehrere durch
  Komma trennen, Groß-/Kleinschreibung egal — <code>*</code>/<code>?</code>
  gehen als Platzhalter (<code>*-uplink</code>, <code>PG-Test-?</code>).
  Leer = kein Filter.</div>
<div style="margin-bottom:16px">
  <input id="netExclNames" class="filterbox" style="width:100%;max-width:520px"
    placeholder="z. B. heartbeat, *-replikation, PG-Test-*">
</div>
<div class="sechead">VLAN-ID-Filter</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Portgruppen mit diesen <b>VLAN-IDs</b>. Einzelne IDs und Bereiche, durch
  Komma getrennt — z. B. <code>99, 205, 3900-3999</code>. Wirkt nur auf
  Portgruppen mit einer einzelnen VLAN-ID (Trunk-Bereiche blendet bereits
  die Uplink-Erkennung aus). Leer = kein Filter.</div>
<div style="margin-bottom:16px">
  <input id="netExclVlans" class="filterbox" style="width:100%;max-width:520px"
    placeholder="z. B. 99, 205, 3900-3999">
</div>
<button class="btn approve" onclick="saveNetCfg()">✓ Speichern &amp; anwenden</button>
<span id="netCfgSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div><!-- admGrpNet -->

<div id="admGrpImp" style="display:none">
<div class="sechead">Offline-Quellen (Cluster ohne vROps-Anbindung)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Für Bereiche <b>ohne Netzanbindung</b> an ein vROps: Ein Kollege führt das
  PowerCLI-Skript gegen das isolierte vCenter aus und lädt das erzeugte JSON
  hier mit einem <b>Quellnamen</b> hoch. Die Cluster erscheinen dann wie eine
  eigene vROps-Quelle — inklusive Kapazitätsanfragen, VLAN-Suche und
  Storage-Übersicht. Die Daten sind <b>statisch</b> (Stand = Import-Datum,
  als Tag am Cluster sichtbar); die Auto-Freigabe klammert diese Cluster
  bewusst aus. Ein erneuter Import unter demselben Namen <b>ersetzt</b> die
  Quelle.</div>
<div style="margin-bottom:14px">
  <a class="btn" href="api/import/powercli" download="kapa_export.ps1"
     title="PowerCLI-Skript, das das Import-JSON erzeugt">⬇ PowerCLI-Skript (kapa_export.ps1)</a>
  <span style="color:var(--muted);font-size:12px;margin-left:8px">
    Aufruf: <code>.\kapa_export.ps1 -Server vcenter.insel.local</code></span>
</div>
<div class="sechead">JSON importieren</div>
<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
  <input class="filterbox" id="impSource" style="max-width:240px"
         placeholder="Quellname, z. B. RZ-Insel">
  <label class="btn" id="impFileBtn">JSON-Datei wählen &amp; importieren<input
    type="file" accept=".json,application/json" hidden onchange="importOffline(event)"></label>
  <span id="impStatus" style="font-size:12px;color:var(--muted)"></span>
</div>
<div class="tablewrap" style="max-width:820px">
<table class="kt" id="imptable">
  <thead><tr><th>Quelle</th><th class="num">Cluster</th><th class="num">Hosts</th>
    <th class="num">VMs</th><th>importiert am</th><th>durch</th>
    <th class="nosort">Aktion</th></tr></thead>
  <tbody id="impbody"></tbody>
</table>
</div>
<div class="sechead" style="margin-top:22px">Kapa-Anfragen aus CSV (XLS-Ablösung)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Übernimmt eure bestehende Excel-Liste: in Excel als <b>CSV speichern</b> und
  hier hochladen. Alle Zeilen kommen als <b>genehmigt</b> an (Freigebender
  „Import"); die Gültigkeit rechnet ab dem <b>Original-Datum</b> — ältere
  Einträge laufen entsprechend sofort ab (wird gemeldet). Bereits vorhandene
  Kapa-Nummern werden übersprungen, ein erneuter Import erzeugt also keine
  Duplikate. Spalten werden an der <b>Kopfzeile</b> erkannt — Reihenfolge
  egal, zusätzliche Spalten werden ignoriert:</div>
<div class="tablewrap" style="max-width:760px;margin-bottom:10px">
<table class="kt">
  <thead><tr><th>Spalte (Kopfzeile)</th><th>Pflicht</th><th>Format</th><th>Beispiel</th></tr></thead>
  <tbody>
    <tr><td><code>Kapa-Nummer</code></td><td>ja</td><td>wird die ID; muss eindeutig sein</td><td style="font-family:monospace">KAPA-2024-017</td></tr>
    <tr><td><code>Projekt</code></td><td>ja</td><td>Freitext (Name der Anfrage)</td><td>SAP-Erweiterung Q4</td></tr>
    <tr><td><code>Cluster</code></td><td>ja</td><td>Cluster-Name wie im Dashboard</td><td style="font-family:monospace">Cluster-01</td></tr>
    <tr><td><code>CPU</code></td><td>ja</td><td>ganze Zahl (vCPUs)</td><td style="font-family:monospace">16</td></tr>
    <tr><td><code>RAM</code></td><td>ja</td><td>ganze Zahl in <b>GB</b></td><td style="font-family:monospace">128</td></tr>
    <tr><td><code>Storage</code></td><td>ja</td><td>ganze Zahl in <b>GB</b> (Tausenderpunkt erlaubt)</td><td style="font-family:monospace">2.000</td></tr>
    <tr><td><code>Datum</code></td><td>ja</td><td>TT.MM.JJJJ (auch JJJJ-MM-TT)</td><td style="font-family:monospace">15.06.2026</td></tr>
    <tr><td><code>Change</code></td><td>–</td><td>Change-/Jira-Ticket</td><td style="font-family:monospace">OPS-4711</td></tr>
    <tr><td><code>Anforderer</code></td><td>–</td><td>Benutzer/Mail</td><td style="font-family:monospace">anna.schmidt@firma.local</td></tr>
    <tr><td><code>Team</code></td><td>–</td><td>Team-Name wie im Dashboard</td><td>Team Betrieb</td></tr>
  </tbody>
</table>
</div>
<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
  <a class="btn" href="api/import/reservations/beispiel" download="kapa-import-beispiel.csv"
     title="Vorlage mit Kopfzeile und zwei Beispielzeilen – in Excel öffnen und befüllen">⬇ Beispiel-CSV (Vorlage)</a>
  <label class="btn">CSV-Datei wählen &amp; importieren<input
    type="file" accept=".csv,text/csv" hidden onchange="importKapaCsv(event)"></label>
  <span id="kapaCsvStatus" style="font-size:12px;color:var(--muted)"></span>
</div>
</div><!-- admGrpImp -->

<div id="admGrpTok" style="display:none">
<div class="sechead">API-Tokens für externe Anwendungen (Endpunkte unter /api/v1/; Schreibrechte je Token per Klick)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  📖 <a href="api/v1/docs" target="_blank" rel="noopener">API-Dokumentation öffnen</a>
  (interaktiv, mit „Ausführen") · <a href="api/v1/openapi.json" target="_blank" rel="noopener">OpenAPI-Spec</a>
  zum Import in Swagger/Postman.</div>
<div style="text-align:right;margin-bottom:6px"><span id="colctl_ttable"></span></div>
<div class="tablewrap">
<table class="kt" id="ttable">
  <thead><tr><th>Anwendung</th><th>Token-Anfang</th><th>erstellt</th><th>von</th>
    <th>zuletzt benutzt</th><th class="nosort">Schreibrechte</th><th class="nosort">Aktion</th></tr></thead>
  <tbody id="ttbody"></tbody>
</table>
</div>
<div class="tablewrap" id="newToken" style="display:none;margin-top:10px;padding:14px">
  <div style="font-size:12px;color:var(--warn);margin-bottom:6px">
    Neues API-Token – wird nur EINMAL angezeigt, jetzt kopieren:</div>
  <code id="newTokenVal" style="font-size:13px;user-select:all;word-break:break-all"></code>
  <button class="btn" style="margin-left:10px"
          onclick="document.getElementById('newToken').style.display='none'">Ausblenden</button>
</div>
</div><!-- admGrpTok -->

<div id="admGrpMail" style="display:none">
<div class="sechead">Mail-Benachrichtigungen (je interner Rolle)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Legt fest, bei welchem Ereignis eine Mail rausgeht. <b>Anforderer</b> = der
  jeweilige Antragsteller (automatisch). <b>Admin/Auditor</b> = die eingetragene
  Verteiler-Adresse. <b>Reviewer / „Team ist dran"</b> = die Team-Adresse aus der
  Teams-Tabelle (Reiter „Benutzer &amp; Rollen"). Voraussetzung ist ein
  konfigurierter SMTP-Server. „Freigabe" meint die endgültige Genehmigung.
  <b>„Erinnerung"</b> mailt das gerade zuständige Team (bzw. den Admin-Verteiler),
  wenn ein Antrag zu lange auf seine Freigabe wartet.</div>
<div class="tablewrap">
<table class="kt" id="notifytable">
  <thead><tr><th style="width:150px">Interne Rolle</th><th>Verteiler-Adresse</th>
    <th class="num nosort">Anlage</th><th class="num nosort">Ablehnung</th>
    <th class="num nosort">Freigabe</th><th class="num nosort">Team ist dran</th>
    <th class="num nosort">Erinnerung</th></tr></thead>
  <tbody id="ntbody"></tbody>
</table>
</div>
<div style="margin-top:8px;font-size:13px">
  Erinnerung nach
  <input id="ntReminderDays" type="number" min="1" max="30" value="2"
         style="width:60px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:4px 6px;text-align:center">
  Tagen Wartezeit – danach alle so viele Tage erneut, bis entschieden ist.
</div>
<button class="btn approve" style="margin-top:8px" onclick="saveNotify()">✓ Mail-Regeln speichern</button>
<span id="notifySaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>

<div class="sechead" style="margin-top:22px">Mail-Vorlage (HTML)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Betreff und HTML-Text der Reservierungs-Mails. Verfügbare Variablen unten
  einfach anklicken, um sie an der Cursor-Position einzufügen. Leer lassen =
  eingebaute Standardvorlage. „Vorschau" rendert die Vorlage mit Beispieldaten.</div>
<div id="mailVars" style="margin-bottom:10px"></div>
<label style="font-size:12px;color:var(--muted)">Betreff</label>
<input id="tplSubject" class="filterbox" style="width:100%;margin-bottom:10px" placeholder="Standardbetreff" onfocus="MAIL_TPL_FOCUS='tplSubject'">
<label style="font-size:12px;color:var(--muted)">HTML-Text</label>
<textarea id="tplHtml" style="width:100%;min-height:220px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:10px;font-family:monospace;font-size:12px;line-height:1.5" placeholder="Standardvorlage (leer lassen)" onfocus="MAIL_TPL_FOCUS='tplHtml'"></textarea>
<div style="margin-top:8px">
  <button class="btn approve" onclick="saveMailTemplate()">✓ Vorlage speichern</button>
  <button class="btn" onclick="previewMail()">Vorschau</button>
  <button class="btn" onclick="resetMailTemplate()">Standard einsetzen</button>
  <span id="tplSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
</div>
<div id="mailPreview" style="display:none;margin-top:12px">
  <div class="hint" style="color:var(--muted);margin-bottom:4px">Vorschau (Beispieldaten) · Betreff: <b id="previewSubject"></b></div>
  <iframe id="previewFrame" sandbox="" style="width:100%;height:360px;border:1px solid var(--line);border-radius:8px;background:#fff"></iframe>
</div>

<div class="sechead" style="margin-top:20px">SMTP / Versand (aus der Konfiguration)</div>
<div id="configMail"></div>
</div><!-- admGrpMail -->

<div id="admGrpConf" style="display:none">
<div class="sechead">Datenabruf-Intervalle (gestaffelt)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Wie ein Cronjob: jeder Teilbereich hat seinen eigenen Takt — so muss nicht
  alles bei jedem Abruf gelesen werden. <b>Kapazität</b> (VMs, CPU/RAM/Disk,
  Tanzu), <b>Netzwerk</b> (Portgruppen/VLANs) und <b>Storage</b>
  (Datastores/LUNs). Cluster, Hosts und Tags laufen immer mit. Leer oder 0 =
  Standard-Intervall. Der ⟳-Knopf oben kann jederzeit alles oder einen
  einzelnen Bereich sofort aktualisieren.</div>
<table class="kt" style="max-width:460px;margin-bottom:10px">
  <tr><td>Kapazität (VMs)</td>
      <td class="num"><input type="number" id="rcVms" min="0" max="10080" class="aanum" style="width:90px"> min</td></tr>
  <tr><td>Netzwerk (Portgruppen)</td>
      <td class="num"><input type="number" id="rcNetwork" min="0" max="10080" class="aanum" style="width:90px"> min</td></tr>
  <tr><td>Storage (Datastores)</td>
      <td class="num"><input type="number" id="rcStorage" min="0" max="10080" class="aanum" style="width:90px"> min</td></tr>
</table>
<div class="sechead" style="margin-top:6px">Zeitzone der Anzeige</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Alle <b>angezeigten</b> Zeiten (Stand, Log, die Abgleich-Uhrzeiten im
  ⟳-Menü, Mail-Zeitstempel) erscheinen in dieser Zeitzone. Der Abruf-Takt
  selbst ist davon unberührt (er rechnet in Sekunden). Leer = Zeitzone des
  Servers. IANA-Name, z. B. <code>Europe/Berlin</code> – die Sommer-/Winterzeit
  wird damit automatisch berücksichtigt.</div>
<input id="rcTz" class="filterbox" style="max-width:260px" list="tzList"
       placeholder="z. B. Europe/Berlin" autocomplete="off">
<datalist id="tzList">
  <option value="Europe/Berlin"></option><option value="Europe/Vienna"></option>
  <option value="Europe/Zurich"></option><option value="UTC"></option>
</datalist>
<span id="rcNow" style="color:var(--muted);font-size:12px;margin-left:8px"></span>
<div style="margin-top:12px">
<button class="btn approve" onclick="saveRefreshCfg()">✓ Intervalle &amp; Zeitzone speichern</button>
<span id="rcSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
<span id="rcDefault" style="color:var(--muted);font-size:12px;margin-left:8px"></span>
</div>

<div id="backupSection" style="display:none">
  <div class="sechead">Backup</div>
  <div class="hint" style="color:var(--muted);margin-bottom:8px">
    Sichert alle Laufzeitdaten (Reservierungen, Rollen, Teams, Selektor, Log,
    Tokens) als tar.gz auf das konfigurierte SFTP-Ziel. Läuft automatisch nach
    dem konfigurierten Intervall – hier lässt sich ein Backup sofort auslösen.</div>
  <button class="btn primary" id="backupBtn" onclick="runBackup()">💾 Backup jetzt erstellen</button>
  <span id="backupStatus" style="font-size:12px;margin-left:10px"></span>
</div>
<div class="sechead" style="margin-top:20px">Konfiguration (schreibgeschützt)</div>
<div class="hint" style="color:var(--muted);margin-bottom:8px">
  Die im System gesetzten Werte (aus INI, kapa.env bzw. Kommandozeile). Nur zur
  Ansicht – Änderungen erfolgen in der Konfiguration und erfordern einen Neustart.
  <b>Passwörter werden nie angezeigt</b> (nur, ob gesetzt).</div>
<div id="configSheet"></div>
</div><!-- admGrpConf -->
</div>
<div id="logView" style="display:none">
<div class="toolbar" style="margin-bottom:8px;flex-wrap:wrap">
  <label style="font-size:12px;color:var(--muted)">Von
    <input type="date" id="logFrom" onchange="logDatePage()" onclick="openPicker(this)"
      style="background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:4px 6px;margin-left:4px"></label>
  <label style="font-size:12px;color:var(--muted)">Bis
    <input type="date" id="logTo" onchange="logDatePage()" onclick="openPicker(this)"
      style="background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:4px 6px;margin-left:4px"></label>
  <button class="btn" onclick="logClearDates()">Datum zurücksetzen</button>
  <span id="logInfo" style="font-size:12px;color:var(--muted);margin-left:auto"></span>
  <span id="colctl_ltable"></span>
</div>
<div class="tablewrap">
<table class="kt" id="ltable">
  <thead><tr><th>Zeit</th><th>Benutzer</th><th>Aktion</th><th>Details</th></tr></thead>
  <tbody id="ltbody"></tbody>
</table>
</div>
<div class="pgbar" id="pager_ltable"></div>
</div>
<div class="hovercard" id="hovercard"></div>
<div class="foot">VMware Kapazitätsplanung · Version __VERSION____CONTACT_FOOT__</div>
<script>
let CLUSTERS = __DATA__;
const FACTOR = __FACTOR__;
const TANZU_MHZ = __TANZU_MHZ__;   // MHz je vCPU-Äquivalent (Tanzu-Namespaces)
const SERVE = __SERVE__;
const TTL = __TTL__;
const ME = __USERINFO__;   // {user, role} bei aktivierter AD-Anmeldung, sonst null
let TEAMS = __TEAMS__;      // Genehmigungs-Teams in Prüfreihenfolge (leer = einstufig); auf der Verwaltungsseite pflegbar
let SELECTOR = __SELECTOR__; // Tag-Kategorien des Cluster-Selektors (max 3, kaskadierend)
let SEL_VALUES = {};         // gewählte Werte je Kategorie
let SRC_FILTER = null;       // vROps-Quellen-Filter (null = Default ableiten)
function clusterSources() {  // benannte vROps-Quellen im Datenbestand
  return [...new Set(CLUSTERS.map(c => c.source).filter(Boolean))].sort();
}
function effectiveSrc() {    // gewählte Quelle bzw. Default (bei genau einer Quelle diese)
  if (SRC_FILTER !== null) return SRC_FILTER;
  const s = clusterSources();
  return s.length === 1 ? s[0] : "";
}
const HAS_BACKUP = __BACKUP__; // ist ein SFTP-Backup-Ziel konfiguriert?
const LS_KEY = "aria_kapa_reservierungen";

// ---- vSphere-Tags / Cluster-Selektor ----
function tagCategories() {   // alle im Datenbestand vorkommenden Tag-Kategorien
  const set = new Set();
  CLUSTERS.forEach(c => (c.tags || []).forEach(t => {
    const i = t.indexOf(": "); if (i > 0) set.add(t.slice(0, i)); }));
  return [...set].sort();
}
function clusterTagVals(c, cat) {   // Werte, die Cluster c für Kategorie cat hat
  const p = cat + ": ";
  return (c.tags || []).filter(t => t.startsWith(p)).map(t => t.slice(p.length));
}
function selCat(i) { return SELECTOR[i] ? SELECTOR[i].category : ""; }
function selMatch(c) {       // erfüllt Cluster c Quellen-Filter + alle Selektor-Stufen?
  const src = effectiveSrc();
  if (src && c.source !== src) return false;
  return SELECTOR.every(s => {
    const v = SEL_VALUES[s.category];
    return !v || clusterTagVals(c, s.category).includes(v);
  });
}
// Werte für Stufe i – kaskadierend: nur die, die zur Quelle + höheren Stufen passen
function selectorOptions(i) {
  const src = effectiveSrc();
  const upper = SELECTOR.slice(0, i);
  const base = CLUSTERS.filter(c => (!src || c.source === src) && upper.every(s => {
    const v = SEL_VALUES[s.category]; return !v || clusterTagVals(c, s.category).includes(v); }));
  const vals = new Set();
  base.forEach(c => clusterTagVals(c, selCat(i)).forEach(v => vals.add(v)));
  return [...vals].sort();
}
function onSelectorChange(i, val) {
  SEL_VALUES[selCat(i)] = val;
  // tiefere Stufen zurücksetzen, wenn ihr Wert nicht mehr wählbar ist
  for (let j = i + 1; j < SELECTOR.length; j++) {
    const opts = selectorOptions(j);
    if (SEL_VALUES[selCat(j)] && !opts.includes(SEL_VALUES[selCat(j)]))
      SEL_VALUES[selCat(j)] = "";
  }
  render();
}
function onSourceChange(val) {
  SRC_FILTER = val;
  // Tag-Stufen zurücksetzen, deren Wert in der gewählten Quelle nicht mehr passt
  for (let j = 0; j < SELECTOR.length; j++) {
    const opts = selectorOptions(j);
    if (SEL_VALUES[selCat(j)] && !opts.includes(SEL_VALUES[selCat(j)]))
      SEL_VALUES[selCat(j)] = "";
  }
  render();
}
function resetSelector() { SEL_VALUES = {}; SRC_FILTER = null; render(); }
function renderClusterSelector() {
  const box = document.getElementById("clusterSelector");
  if (!box) return;
  const active = SELECTOR.filter(s => tagCategories().includes(s.category));
  const srcs = clusterSources();
  if (!active.length && !srcs.length) { box.innerHTML = ""; box.style.display = "none"; return; }
  box.style.display = "";
  const cur = effectiveSrc();
  const srcSel = srcs.length ? `<label>vROps
    <select onchange="onSourceChange(this.value)">
      <option value="">alle</option>
      ${srcs.map(v => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(v)}</option>`).join("")}
    </select></label>` : "";
  const any = SRC_FILTER !== null || SELECTOR.some(s => SEL_VALUES[s.category]);
  box.innerHTML = '<span class="sellabel">Cluster-Selektor:</span>' + srcSel +
    SELECTOR.map((s, i) => {
      if (!tagCategories().includes(s.category)) return "";
      const opts = selectorOptions(i);
      const sv = SEL_VALUES[s.category] || "";
      return `<label>${esc(s.label || s.category)}
        <select onchange="onSelectorChange(${i}, this.value)">
          <option value="">alle</option>
          ${opts.map(v => `<option value="${esc(v)}" ${sv === v ? "selected" : ""}>${esc(v)}</option>`).join("")}
        </select></label>`;
    }).join("") +
    (any ? '<button class="btn" onclick="resetSelector()">Zurücksetzen</button>' : "");
}

// ---- Rollen ----
const ROLE = ME ? ME.role : "admin";          // ohne AD-Anmeldung: Vollzugriff
const VIS = __VIS__;   // Sichtbarkeits-Flags der eigenen Rolle (Matrix in der Verwaltung)
const IS_ADMIN = ROLE === "admin";
const IS_REVIEWER = ROLE === "reviewer";
const CAN_REQUEST = IS_ADMIN || ROLE === "anforderer";
// Rollen-Bezeichnungen sind frei wählbar (Verwaltung); Schlüssel bleiben fest.
let ROLE_NAMES = __ROLENAMES__;
const SOURCES = __SOURCES__;   // konfigurierte vROps-Quellnamen (leer/1 = Feature inert)
if (SOURCES.length < 2) { const st = document.createElement('style'); st.textContent = '.srcCol{display:none}'; document.head.appendChild(st); }
const ROLE_ORDER = ["anforderer", "reviewer", "admin", "auditor"];
let NOTIFY = __NOTIFY__;    // Mail-Regeln je interner Rolle + Team-Adressen
let MAIL_VARS = [];        // verfügbare {{var}} für die Mail-Vorlage
let MAIL_DEF_TPL = "";     // eingebaute Standard-HTML-Vorlage
let MAIL_DEF_SUBJ = "";    // eingebauter Standardbetreff
let PREFS = __PREFS__;      // persönliche UI-Einstellungen (serverseitig je Benutzer)
let ANNOUNCE = __ANNOUNCE__; // aktive Ankündigung {id,title,text} oder null

// ---- Spalten ein-/ausblenden je Tabelle, pro Benutzer gespeichert ----
const USE_SERVER_PREFS = SERVE && !!ME;   // sonst localStorage (Demo/ohne Login)
let COLHIDE = (function () {
  if (USE_SERVER_PREFS) return (PREFS && PREFS.cols) || {};
  try { return JSON.parse(localStorage.getItem("kapa_cols") || "{}"); } catch (e) { return {}; }
})();
let _prefsTimer = null;
// Kompletter Prefs-Body: der Server ersetzt die Prefs bei jedem PUT komplett,
// deshalb müssen alle Teile (Spalten, Ankündigungs-Merker, Theme) immer mit.
function prefsBody() {
  const b = { cols: COLHIDE };
  if (PREFS && PREFS.announce_seen) b.announce_seen = PREFS.announce_seen;
  if (PREFS && PREFS.theme) b.theme = PREFS.theme;
  return b;
}
function saveColPrefs() {
  if (USE_SERVER_PREFS) {
    clearTimeout(_prefsTimer);
    _prefsTimer = setTimeout(() => {
      fetch("api/prefs", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(prefsBody()) }).catch(() => {});
    }, 400);
  } else {
    try { localStorage.setItem("kapa_cols", JSON.stringify(COLHIDE)); } catch (e) {}
  }
}

// ---- Hell/Dunkel-Umschalter (Kopfleiste), gespeichert je Benutzer ----
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}
function applyTheme(t) {
  if (t === "light") document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  const b = document.getElementById("themeBtn");
  if (b) { b.textContent = t === "light" ? "🌙" : "☀️";
           b.title = t === "light" ? "Dunkles Design" : "Helles Design"; }
  try { localStorage.setItem("kapa_theme", t); } catch (e) {}
}
function toggleTheme() {
  const t = currentTheme() === "light" ? "dark" : "light";
  PREFS = PREFS || {};
  PREFS.theme = t;
  applyTheme(t);
  if (USE_SERVER_PREFS)
    fetch("api/prefs", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prefsBody()) }).catch(() => {});
}
// Init: Server-Einstellung gewinnt (gilt über Geräte hinweg); sonst bleibt,
// was das Head-Snippet aus localStorage/prefers-color-scheme gesetzt hat.
applyTheme(USE_SERVER_PREFS && PREFS && PREFS.theme ? PREFS.theme : currentTheme());

// ---- Ankündigungs-Popup (einmal je Benutzer, Merker in den Prefs) ----
function announceSeenId() {
  if (USE_SERVER_PREFS) return (PREFS && PREFS.announce_seen) || "";
  try { return localStorage.getItem("kapa_announce_seen") || ""; } catch (e) { return ""; }
}
function maybeShowAnnounce() {
  if (!ANNOUNCE || !ANNOUNCE.id || announceSeenId() === ANNOUNCE.id) return;
  document.querySelector("#annTitle span").textContent = ANNOUNCE.title || "Ankündigung";
  document.getElementById("annBody").textContent = ANNOUNCE.text || "";
  document.getElementById("annBg").classList.add("open");
}
let _annPreview = false;
function closeAnnounce() {
  document.getElementById("annBg").classList.remove("open");
  if (_annPreview) { _annPreview = false; return; }   // Admin-Vorschau: kein Merker
  if (!ANNOUNCE || !ANNOUNCE.id) return;
  if (USE_SERVER_PREFS) {
    PREFS = PREFS || {};
    PREFS.announce_seen = ANNOUNCE.id;
    fetch("api/prefs", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prefsBody()) }).catch(() => {});
  } else {
    try { localStorage.setItem("kapa_announce_seen", ANNOUNCE.id); } catch (e) {}
  }
}
function applyCols(tableId) {
  const t = document.getElementById(tableId);
  if (!t || !t.tHead || !t.tHead.rows.length) return;
  const hid = COLHIDE[tableId] || {};
  const n = t.tHead.rows[0].cells.length;
  for (let i = 0; i < n; i++) {
    const show = !hid[i];
    t.tHead.rows[0].cells[i].style.display = show ? "" : "none";
    if (t.tBodies[0]) Array.from(t.tBodies[0].rows).forEach(r => {
      if (r.cells[i] && !r.cells[i].hasAttribute("colspan")) r.cells[i].style.display = show ? "" : "none";
    });
  }
}
function toggleCol(tableId, i) {
  const h = COLHIDE[tableId] || (COLHIDE[tableId] = {});
  if (h[i]) delete h[i]; else h[i] = true;
  if (!Object.keys(h).length) delete COLHIDE[tableId];
  applyCols(tableId); saveColPrefs();
}
function resetCols(tableId) { delete COLHIDE[tableId]; applyCols(tableId); saveColPrefs(); renderColMenu(tableId); }
function colMenuHtml(tableId) {
  const t = document.getElementById(tableId);
  if (!t || !t.tHead) return "";
  const hid = COLHIDE[tableId] || {};
  const items = Array.from(t.tHead.rows[0].cells).map((th, i) => {
    const label = (th.getAttribute("data-base") || th.textContent || "").trim() || ("Spalte " + (i + 1));
    return `<label><input type="checkbox" ${hid[i] ? "" : "checked"} onchange="toggleCol('${tableId}',${i})"> ${esc(label)}</label>`;
  }).join("");
  const anyHidden = Object.keys(hid).length;
  return `<details class="colmenu"><summary>⚙ Spalten${anyHidden ? " (" + anyHidden + " aus)" : ""}</summary>
    <div>${items}<label style="border-top:1px solid var(--line);margin-top:4px;padding-top:4px">
      <a href="#" onclick="resetCols('${tableId}');return false" style="color:var(--accent)">alle einblenden</a></label></div></details>`;
}
function renderColMenu(tableId) {
  const box = document.getElementById("colctl_" + tableId);
  if (box) box.innerHTML = colMenuHtml(tableId);
}
// Welche Ereignisse je Rolle wählbar sind (Rest = "–"); Reihenfolge = Spalten
const NOTIFY_EVENTS = [["created","Anlage"],["rejected","Ablehnung"],["approved","Freigabe"],["team_turn","Team ist dran"],["reminder","Erinnerung"]];
const NOTIFY_ROLE_EVENTS = {
  anforderer: ["created","rejected","approved"],
  admin:      ["created","rejected","approved","team_turn","reminder"],
  auditor:    ["created","rejected","approved","team_turn"],
  reviewer:   ["team_turn","reminder"],
};
// Löschen von Reservierungsanfragen ist deaktiviert – stattdessen Storno.
function canDel(r) { return false; }
// Storno: Admin, jemand aus derselben Abteilung oder der Anforderer selbst
// darf eine noch offene/genehmigte Anfrage zurückziehen (bleibt als „storniert“
// in der Historie). Bereits abgelehnte/stornierte lassen sich nicht stornieren.
function canCancel(r) {
  if (r.rejected || r.cancelled || r.foreign) return false;
  if (IS_ADMIN) return true;
  const dept = ME && ME.abteilung;
  return !!(dept && r.abteilung === dept) || !!(ME && r.von === ME.user);
}
// ---- Mehrstufiger Genehmigungsprozess ----
function stageOf(r) { return (r.approvals || []).length; }
function currentTeam(r) {
  if (!TEAMS.length) return null;
  const n = stageOf(r);
  return n < TEAMS.length ? TEAMS[n] : null;
}
// Darf der/die Angemeldete den Antrag in seiner aktuellen Stufe entscheiden?
function canDecide(r) {
  if (IS_ADMIN) return true;
  if (IS_REVIEWER && TEAMS.length && ME) return ME.abteilung === currentTeam(r);
  return false;
}
async function logout() {
  try { await fetch("api/logout", { method: "POST" }); } catch (e) {}
  location.reload();
}

// ---- Reservierungen (Serve-Modus: zentral auf dem Server, sonst localStorage) ----
let RES = [];
if (!SERVE) {
  try { RES = JSON.parse(localStorage.getItem(LS_KEY)) || []; } catch (e) {}
  if (TTL > 0) {
    const cutoff = Date.now() - TTL * 864e5;
    RES = RES.filter(r => !r.created || new Date(r.created).getTime() > cutoff);
  }
}
function saveLocal() { try { localStorage.setItem(LS_KEY, JSON.stringify(RES)); } catch (e) {} }
async function apiRes(method, path, body) {
  const r = await fetch("api/reservations" + (path || ""), {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function setRes(list) { if (Array.isArray(list)) { RES = list; render(); } }
function resFail() { notify("Reservierungen konnten nicht auf dem Server gespeichert werden."); }
// Nur genehmigte Reservierungen zählen gegen die Kapazität
function resFor(cl) { return RES.filter(r => r.cluster === cl && r.approved && !r.cancelled); }
function allFor(cl) { return RES.filter(r => r.cluster === cl); }
function fmtDate(iso) {
  if (!iso) return "–";
  const p = String(iso).slice(0, 10).split("-");
  return p.length === 3 ? p[2] + "." + p[1] + "." + p[0] : iso;
}
function validUntil(r) {
  if (!r.created) return "";
  const d = new Date(r.created + "T12:00:00");
  d.setDate(d.getDate() + (TTL > 0 ? TTL - 1 : 30));
  return d.toISOString().slice(0, 10);
}
function approvalsText(r) {
  // Liste der bisherigen Freigaben; ohne Namen, falls serverseitig gestrippt
  return (r.approvals || []).map(a =>
    a.team + (a.by ? " – " + a.by : "") + " (" + fmtDate(a.on) + ")").join("\n");
}
function stBadge(r) {
  const cmt = r.comment ? " · Kommentar: " + esc(r.comment) : "";
  if (r.rejected) {
    const stufe = r.rejected_team ? " in Stufe „" + esc(r.rejected_team) + "“" : "";
    return `<span class="st rej" title="abgelehnt${r.rejected_by ? " von " + esc(r.rejected_by) : ""}${stufe} am ${fmtDate(r.rejected_on)}${cmt}">abgelehnt</span>`;
  }
  if (r.cancelled)
    return `<span class="st canc" title="storniert${r.cancelled_by ? " von " + esc(r.cancelled_by) : ""} am ${fmtDate(r.cancelled_on)}${cmt}">storniert</span>`;
  if (r.approved)
    return `<span class="st ok" title="genehmigt${r.approved_by ? " von " + esc(r.approved_by) : ""} am ${fmtDate(r.approved_on)}${cmt}">genehmigt${r.approved_by === "Auto-Freigabe" ? " (auto)" : ""}</span>`;
  if (TEAMS.length && stageOf(r) > 0) {
    // teilweise freigegeben -> in Prüfung; Mouseover zeigt, wer schon freigab
    const done = approvalsText(r);
    const wartet = currentTeam(r) ? "\nwartet auf: " + currentTeam(r) : "";
    const tip = "Bereits freigegeben:\n" + esc(done) + esc(wartet);
    return `<span class="st prog" title="${tip}">in Prüfung (${stageOf(r)}/${TEAMS.length})</span>`;
  }
  const wartet = TEAMS.length ? ' title="wartet auf: ' + esc(TEAMS[0]) + '"' : "";
  return `<span class="st pend"${wartet}>beantragt</span>`;
}
function isPend(r) { return !r.approved && !r.rejected && !r.cancelled; }
function sumCpu(rv) { return rv.reduce((s,r)=>s+(r.vcpu||0),0); }
function sumRam(rv) { return Math.round(rv.reduce((s,r)=>s+(r.ram_gb||0),0)*10)/10; }

// Tanzu-Namespace-Reservierungen des Clusters (zählen wie genehmigte Reservierungen)
function tzOf(c) { return { cpu: c.tanzuVcpu || 0, ram: c.tanzuRamGb || 0 }; }
function freeAfter(c) {
  const rv = resFor(c.name), tz = tzOf(c);
  return { cpu: c.vcpuFree - sumCpu(rv) - tz.cpu,
           ram: Math.round((c.ramFree - sumRam(rv) - tz.ram) * 10) / 10,
           stor: Math.round(((c.storageFree || 0) - sumStorage(rv)) * 10) / 10,
           hasStor: (c.storageCap || 0) > 0 };
}

async function createRes(c, name, change, vcpu, ram, storage, errEl) {
  errEl.style.display = "none";
  if (!name || (vcpu <= 0 && ram <= 0 && storage <= 0)) {
    errEl.textContent = "Bitte Bezeichnung sowie vCPU, RAM und/oder Storage angeben.";
    errEl.style.display = "block"; return false;
  }
  const ch = String(change || "").trim();   // Change/Jira optional, frei wählbar
  const f = freeAfter(c);
  const over = vcpu > f.cpu || ram > f.ram || (f.hasStor && storage > f.stor);
  if (over) {
    const goOn = await askConfirm({
      title: "Freie Kapazität überschritten",
      okLabel: "Trotzdem beantragen", okClass: "danger",
      message: "Die Reservierung überschreitet die freie Kapazität von " +
        esc(c.name) + ".\nFrei: " + fmt(f.cpu) + " vCPU / " + fmt(f.ram) + " GB RAM" +
        (f.hasStor ? " / " + fmt(f.stor) + " GB Storage" : "") + "." });
    if (!goOn) return false;
  }
  const item = { cluster: c.name, name: name, change: ch, vcpu: vcpu,
                 ram_gb: ram, storage_gb: storage };
  if (SERVE) {
    apiRes("POST", "", item).then(setRes).catch(resFail);
  } else {
    item.id = Date.now() + "-" + Math.random().toString(36).slice(2,7);
    item.created = new Date().toISOString().slice(0,10);
    item.approvals = [];
    item.approved = false;
    RES.push(item); saveLocal(); render();
  }
  return true;
}

// ---- Kommentar-Dialog (schön statt prompt) ----
let _cmtResolve = null;
function askComment(opts) {
  return new Promise(resolve => {
    _cmtResolve = resolve;
    document.getElementById("cmtTitle").textContent = opts.title || "Kommentar";
    document.getElementById("cmtMsg").textContent = opts.message || "";
    const ok = document.getElementById("cmtOk");
    ok.textContent = opts.okLabel || "OK";
    ok.className = "btn primary" + (opts.okClass ? " " + opts.okClass : "");
    const inp = document.getElementById("cmtInput");
    inp.value = "";
    cmtCount();
    const ex = document.getElementById("cmtExtra");
    _cmtExtra = opts.onExtra || null;
    ex.style.display = opts.extraLabel ? "" : "none";
    ex.textContent = opts.extraLabel || "";
    document.getElementById("cmtBg").classList.add("open");
    setTimeout(() => inp.focus(), 30);
  });
}
let _cmtExtra = null;
function cmtExtra() { const f = _cmtExtra; if (f) f(); }
function closeComment() { document.getElementById("cmtBg").classList.remove("open"); _cmtResolve = null; }
function cmtCount() {
  const inp = document.getElementById("cmtInput");
  document.getElementById("cmtCnt").textContent = inp.value.length + " / 64 Zeichen";
}
function cmtClose(val) {
  document.getElementById("cmtBg").classList.remove("open");
  const r = _cmtResolve; _cmtResolve = null;
  if (r) r(val);
}
function cmtConfirm() { cmtClose(document.getElementById("cmtInput").value.trim().slice(0, 64)); }
function cmtCancel() { cmtClose(null); }

// ---- Schicke Bestätigung / Hinweis (ersetzt native confirm()/alert()) ----
let _askResolve = null;
function askConfirm(opts) {
  return new Promise(resolve => {
    _askResolve = resolve;
    document.getElementById("askTitle").textContent = opts.title || "Bestätigen";
    // opts.html = eigenes Formular (nur aus vertrauenswürdigem App-Code, nie
    // aus Fremddaten befüllen); sonst reiner Text.
    const msg = document.getElementById("askMsg");
    if (opts.html != null) {
      // Formular-Dialog: kein pre-line (sonst reißen Quelltext-Umbrüche das
      // Layout auf) und normale Textfarbe statt Grau.
      msg.innerHTML = opts.html;
      msg.style.whiteSpace = "normal";
      msg.style.color = "var(--text)";
    } else {
      msg.textContent = opts.message || "";
      msg.style.whiteSpace = "pre-line";
      msg.style.color = "var(--muted)";
    }
    const ok = document.getElementById("askOk");
    ok.textContent = opts.okLabel || "OK";
    ok.className = "btn primary" + (opts.okClass ? " " + opts.okClass : "");
    const cancel = document.getElementById("askCancel");
    cancel.style.display = opts.hideCancel ? "none" : "";
    cancel.textContent = opts.cancelLabel || "Abbrechen";
    document.getElementById("askBg").classList.add("open");
    setTimeout(() => ok.focus(), 30);
  });
}
function notify(message, title) {
  return askConfirm({ title: title || "Hinweis", message: message, okLabel: "OK", hideCancel: true });
}
function askClose(val) {
  document.getElementById("askBg").classList.remove("open");
  const r = _askResolve; _askResolve = null;
  if (r) r(!!val);
}

// ---- Info-/Hilfe-Popup ----
function showInfo(srcId, title) {
  document.getElementById("infoTitle").textContent = title;
  document.getElementById("infoBody").innerHTML = document.getElementById(srcId).innerHTML;
  document.getElementById("infoBg").classList.add("open");
}
function closeInfo() { document.getElementById("infoBg").classList.remove("open"); }

function rejectRes(id) {
  const r = RES.find(x => x.id === id);
  askComment({ title: "Antrag ablehnen", okLabel: "✕ Ablehnen", okClass: "danger",
    message: "„" + ((r && r.name) || "?") + "“ – die Ablehnung bleibt " +
             (TTL > 0 ? TTL : 31) + " Tage in der Historie sichtbar." }).then(c => {
    if (c === null) return;
    if (SERVE) apiRes("POST", "/" + encodeURIComponent(id) + "/reject",
                      { comment: c }).then(setRes).catch(resFail);
    else if (r) {
      r.rejected = true;
      r.rejected_on = new Date().toISOString().slice(0, 10);
      if (c) r.comment = c;
      saveLocal(); render();
    }
  });
}

function approveRes(id) {
  const r = RES.find(x => x.id === id);
  const team = r ? currentTeam(r) : null;
  askComment({ title: team ? "Freigeben (" + team + ")" : "Antrag genehmigen",
    okLabel: "✓ Bestätigen", message: "„" + ((r && r.name) || "?") + "“",
    // Bei aktiver Storage-Erweiterung ein Zusatz-Button im Kommentar-Dialog:
    extraLabel: (STOR.enabled && CAN_STORAGE && r) ? "+ Storage-Erweiterung" : "",
    onExtra: () => { closeComment(); openStorReq(r.cluster, "", "", 0, r.id, r.name); }
  }).then(c => {
    if (c === null) return;
    if (SERVE) apiRes("POST", "/" + encodeURIComponent(id) + "/approve",
                      { comment: c }).then(setRes).catch(resFail);
    else if (r) {
      r.approved = true;
      if (c) r.comment = c;
      saveLocal(); render();
    }
  });
}

function addRes(idx) {
  const c = CLUSTERS[idx];
  const g = s => document.getElementById("f" + idx + s);
  createRes(c, g("n").value.trim(), g("ch").value, parseInt(g("c").value) || 0,
            parseInt(g("r").value) || 0, parseInt(g("s").value) || 0, g("e"));
}
function delRes(id) {
  if (SERVE) apiRes("DELETE", "/" + encodeURIComponent(id)).then(setRes).catch(resFail);
  else { RES = RES.filter(r => r.id !== id); saveLocal(); render(); }
}
function cancelRes(id) {
  const r = RES.find(x => x.id === id);
  askComment({ title: "Anfrage stornieren", okLabel: "⦸ Stornieren", okClass: "danger",
    message: "„" + ((r && r.name) || "?") + "“ bleibt als „storniert“ in der " +
             "Historie und zählt nicht mehr gegen die Kapazität." }).then(c => {
    if (c === null) return;
    if (SERVE) apiRes("POST", "/" + encodeURIComponent(id) + "/cancel",
                      { comment: c }).then(setRes).catch(resFail);
    else if (r) {
      r.cancelled = true;
      r.cancelled_on = new Date().toISOString().slice(0, 10);
      if (c) r.comment = c;
      saveLocal(); render();
    }
  });
}

// ---- Dialog "Neue Kapazitätsanfrage" ----
function fillClusterSelect(prefIdx) {
  const sel = document.getElementById("mCluster");
  const q = (document.getElementById("mClusterSearch").value || "").trim().toLowerCase();
  const prev = sel.value;
  const opts = CLUSTERS.map((c, i) => ({ i: i, name: c.name, source: c.source || "" }))
                       .filter(o => !q || o.name.toLowerCase().includes(q)
                                    || o.source.toLowerCase().includes(q));
  sel.innerHTML = opts.length
    ? opts.map(o => `<option value="${o.i}">${esc(o.name)}${CLUSTERS[o.i].source ? " · " + esc(CLUSTERS[o.i].source) : ""}</option>`).join("")
    : `<option value="">(kein Cluster passt)</option>`;
  const want = (prefIdx !== undefined && prefIdx !== null) ? String(prefIdx) : prev;
  if (want !== "" && opts.some(o => String(o.i) === want)) sel.value = want;
  modalHint();
}
function openModal(prefIdx) {
  document.getElementById("mClusterSearch").value = "";
  fillClusterSelect(prefIdx);
  ["mName", "mChange", "mCpu", "mRam", "mStorage"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("mErr").style.display = "none";
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById("mValid").textContent =
    "Gilt ab heute (" + fmtDate(today) + ") bis " +
    fmtDate(validUntil({created: today})) + " · wirksam erst nach Genehmigung.";
  modalHint();
  document.getElementById("modalBg").classList.add("open");
  document.getElementById("mName").focus();
}
function closeModal() { document.getElementById("modalBg").classList.remove("open"); }
function modalHint() {
  const c = CLUSTERS[+document.getElementById("mCluster").value];
  if (!c) return;
  const f = freeAfter(c);
  document.getElementById("mHint").textContent =
    "Frei nach bestehenden Reservierungen: " + fmt(f.cpu) + " vCPU / " + fmt(f.ram) + " GB RAM"
    + (f.hasStor ? " / " + fmt(f.stor) + " GB Storage" : "");
}
async function submitModal() {
  const c = CLUSTERS[+document.getElementById("mCluster").value];
  const v = id => document.getElementById(id).value;
  const ok = await createRes(c, v("mName").trim(), v("mChange"),
                       parseInt(v("mCpu")) || 0, parseInt(v("mRam")) || 0,
                       parseInt(v("mStorage")) || 0,
                       document.getElementById("mErr"));
  if (ok) closeModal();
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeModal(); cmtCancel(); closeInfo(); hideCard(); }
  else if (e.key === "Enter" && (e.ctrlKey || e.metaKey) &&
           document.getElementById("cmtBg").classList.contains("open")) cmtConfirm();
});

function exportRes() {
  const blob = new Blob([JSON.stringify(RES, null, 2)], {type:"application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "kapa_reservierungen.json";
  a.click();
}
function importRes(ev) {
  const f = ev.target.files[0]; if (!f) return;
  f.text().then(t => {
    const d = JSON.parse(t);
    if (!Array.isArray(d)) { notify("Ungültige Datei."); return; }
    if (SERVE) apiRes("PUT", "", d).then(setRes).catch(resFail);
    else { RES = d; saveLocal(); render(); }
  }).catch(() => notify("Datei konnte nicht gelesen werden."));
  ev.target.value = "";
}

// ---- Darstellung ----
function pct(v, cap) { return cap > 0 ? Math.min(100, v / cap * 100) : 0; }
function color(p) { return p < 70 ? "var(--ok)" : p < 90 ? "var(--warn)" : "var(--crit)"; }
function fmt(n) { return n.toLocaleString("de-DE"); }
function esc(s) { return String(s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function metric(label, used, resv, cap, unit) {
  const pu = pct(used, cap);
  const pr = pct(used + resv, cap) - pu;
  const free = Math.round((cap - used - resv) * 10) / 10;
  const c = color(pu + pr);
  return `<div class="metric">
    <div class="row"><span>${label}</span>
      <span>${fmt(used)} belegt${resv ? " + " + fmt(resv) + " reserviert" : ""} / ${fmt(cap)} ${unit} ·
      <span class="free" style="color:${c}">${fmt(free)} ${unit} frei</span></span></div>
    <div class="bar"><i style="width:${pu}%;background:${color(pu)}"></i><i class="r" style="width:${pr}%"></i></div>
  </div>`;
}

function card(c, idx, isTotal) {
  const clRes = isTotal ? RES.filter(r => !TOTAL || TOTAL._names.has(r.cluster)) : allFor(c.name);
  const rv = clRes.filter(r => r.approved && !r.cancelled);
  const tz = tzOf(c);   // Tanzu-Namespace-Reservierungen zählen wie genehmigte
  const rvCpu = sumCpu(rv) + tz.cpu, rvRam = Math.round((sumRam(rv) + tz.ram) * 10) / 10,
        rvStor = sumStorage(rv);
  const hasStor = (c.storageCap || 0) > 0;
  const vmRows = (c.vms || []).sort((a,b)=>b.vcpu-a.vcpu).map(v =>
    `<tr class="${v.on?'':'off'}"><td>${esc(v.name)}${v.on?'':' (aus)'}</td>
     <td class="num">${v.vcpu}</td><td class="num">${fmt(v.ram_gb)}</td></tr>`).join("");
  const hostRows = (c.hosts || []).map(h =>
    `<tr><td>${esc(h.name)}</td><td class="num">${h.cores}</td><td class="num">${fmt(h.ram_gb)}</td></tr>`).join("");
  const ds = (c.datastores || []).slice().sort(DS_SORT === "util"
    ? (a, b) => (b.used_gb / (b.cap_gb || 1)) - (a.used_gb / (a.cap_gb || 1))
    : (a, b) => b.cap_gb - a.cap_gb);
  const dsRows = ds.map(d => {
    const p = d.cap_gb ? Math.round(d.used_gb / d.cap_gb * 100) : 0;
    // Bei vSAN zeigen wir die NUTZBARE Kapazität (Faktor) – Brutto im Tooltip
    const tipParts = [];
    if (d.factor && d.factor !== 1)
      tipParts.push(`brutto ${fmt(Math.round(d.raw_cap_gb))} GB · mit Faktor ${d.factor} als nutzbar gerechnet`);
    if (d.naa) tipParts.push(d.naa);
    const net = tipParts.length ? ` title="${tipParts.join(" · ")}"` : "";
    const typ = (d.factor && d.factor !== 1)
      ? `${esc(d.type)} <span style="color:var(--muted)">(${Math.round(d.factor * 100)} %)</span>`
      : esc(d.type || "–");
    return `<tr${net}><td>${esc(d.name)}</td><td>${typ}</td>
      <td class="num">${fmt(Math.round(d.cap_gb))}</td>
      <td class="num">${fmt(Math.round(d.used_gb))}</td>
      <td class="num" style="color:${color(p)}">${p}%</td>
      <td class="num">${fmt(Math.round((d.cap_gb - d.used_gb) * 10) / 10)}</td></tr>`;
  }).join("");
  const dsLink = m => `<a href="#" onclick="setDsSort('${m}');return false"${DS_SORT === m ? ' style="font-weight:600;color:var(--text)"' : ""}>${m === "size" ? "Größe" : "Belegung"}</a>`;
  const dsBlock = ds.length ? `
      <div style="margin:6px 0 8px;font-size:12px;color:var(--muted)">${ds.length} Datastores / LUNs · Sortierung: ${dsLink("size")} · ${dsLink("util")}${ds.some(d => d.factor && d.factor !== 1) ? " · vSAN wird als nutzbare Kapazität gerechnet (Spiegelung)" : ""}</div>
      <table><tr><th>Datastore / LUN</th><th>Typ</th><th class="num">Größe (GB)</th><th class="num">Belegt (GB)</th><th class="num">Belegt %</th><th class="num">Frei (GB)</th></tr>${dsRows}</table>`
    : `<div style="color:var(--muted);font-size:12px">Keine Storage-Daten aus Aria.</div>`;
  const tagBlock = (VIS.tags && (c.tags || []).length) ? `
    <div class="tagbox">
      <h3>vSphere-Tags</h3>
      <div>${(c.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    </div>` : "";
  // Reservierungen ANDERER Teams (foreign) tauchen für Anforderer nicht als
  // Zeile auf – sie zählen aber weiter in „reserviert"/„frei" hinein (rv).
  // Abgelehnte/stornierte liegen im Archiv und erscheinen hier nicht.
  const shownRes = clRes.filter(r => !r.foreign && !isArchived(r));
  const foreignN = clRes.filter(r => r.foreign).length;
  const resRows = shownRes.map(r =>
    `<tr><td>${esc(r.name)}${r.change ? ' <span style="color:var(--muted)">' + esc(r.change) + '</span>' : ''}${isTotal ? ' <span style="color:var(--muted)">(' + esc(r.cluster) + ')</span>' : ''}</td>
     <td class="num">${r.vcpu}</td><td class="num">${fmt(r.ram_gb)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     <td>${fmtDate(validUntil(r))}</td><td>${stBadge(r)}</td>
     <td>${canCancel(r) ? `<button class="del" title="Anfrage stornieren" onclick="cancelRes('${esc(r.id)}')">⦸ Storno</button>` : ""}</td></tr>`).join("");
  const foreignNote = foreignN ? `<div style="color:var(--muted);font-size:12px;margin-top:6px">+ ${foreignN} genehmigte Reservierung(en) anderer Teams – in „reserviert“ berücksichtigt.</div>` : "";
  const resTable = (shownRes.length ?
    `<table><tr><th>Anfrage</th><th class="num">vCPU</th><th class="num">RAM (GB)</th><th class="num">Storage (GB)</th><th>gültig bis</th><th>Status</th><th></th></tr>${resRows}</table>`
    : `<div style="color:var(--muted);font-size:12px">Keine Reservierungen.</div>`) + foreignNote;
  const spare = (c.spareCores || c.spareRamGb) ?
    ` · Ausfallreserve (N+1): ${fmt(c.spareCores)} Cores / ${fmt(c.spareRamGb)} GB abgezogen` : "";
  // ---- Inhalte je Reiter ----
  const paneCpu = `
    ${metric("vCPU (Cores × " + FACTOR + ")", c.vcpuUsed, rvCpu, c.vcpuCap, "vCPU")}
    ${metric("RAM", c.ramUsed, rvRam, c.ramCap, "GB")}
    <div class="kpis">
      <div class="kpi">frei nach Reservierungen<b>${fmt(c.vcpuFree - rvCpu)} vCPU / ${fmt(Math.round((c.ramFree - rvRam)*10)/10)} GB</b></div>
      <div class="kpi">reserviert<b>${fmt(rvCpu)} vCPU / ${fmt(rvRam)} GB</b></div>
      ${(tz.cpu || tz.ram) ? `<div class="kpi">davon Tanzu-Namespaces<b>${fmt(tz.cpu)} vCPU / ${fmt(tz.ram)} GB</b></div>` : ""}
      <div class="kpi">Ø VM<b>${c.vmCount?Math.round(c.vcpuUsed/c.vmCount*10)/10:0} vCPU / ${c.vmCount?Math.round(c.ramUsed/c.vmCount):0} GB</b></div>
      ${(c.workload != null && VIS.workload) ? `<div class="kpi">Workload (vROps)<b style="color:${color(c.workload)}">${c.workload} %</b></div>` : ""}
    </div>
    ${(c.namespaces || []).length ? `
    <div class="resbox">
      <h3>Tanzu-Namespaces (${c.namespaces.length})</h3>
      <div style="color:var(--muted);font-size:12px;margin:4px 0 6px">
        Kubernetes-Namespace-Reservierungen aus vROps – zählen wie genehmigte
        Reservierungen gegen die freie Kapazität (CPU: ${fmt(TANZU_MHZ)} MHz je vCPU).</div>
      <table><tr><th>Namespace</th><th class="num">CPU (MHz)</th><th class="num">vCPU-Äquiv.</th><th class="num">RAM (GB)</th>${isTotal ? "" : ""}</tr>
      ${c.namespaces.map(n => `<tr><td>${esc(n.name)}</td><td class="num">${fmt(n.cpu_mhz)}</td><td class="num">${fmt(n.vcpu)}</td><td class="num">${fmt(n.ram_gb)}</td></tr>`).join("")}</table>
    </div>` : ""}
    <div class="resbox">
      <h3>Kapazitätsreservierungen</h3>
      ${resTable}
      ${isTotal || !CAN_REQUEST ? "" : `
      <div class="resform">
        <input id="f${idx}n" placeholder="Bezeichnung / Projekt">
        <input id="f${idx}ch" placeholder="Change / Jira Ticket (optional)">
        <input id="f${idx}c" type="number" min="0" step="1" placeholder="vCPU">
        <input id="f${idx}r" type="number" min="0" step="1" placeholder="RAM GB">
        <input id="f${idx}s" type="number" min="0" step="1" placeholder="Storage GB">
        <button class="btn" onclick="addRes(${idx})">+ Beantragen</button>
      </div>
      <div class="err" id="f${idx}e"></div>`}
    </div>
    ${tagBlock}`;
  const paneStorage = `
    ${hasStor ? metric("Storage", c.storageUsed, rvStor, c.storageCap, "GB") : ""}
    ${hasStor ? `<div class="kpis">
      <div class="kpi">frei nach Reservierungen<b>${fmt(Math.round(((c.storageFree||0) - rvStor)*10)/10)} GB</b></div>
      <div class="kpi">reserviert<b>${fmt(rvStor)} GB</b></div>
    </div>` : ""}
    ${dsBlock}`;
  const paneHosts = `<table><tr><th>Host</th><th class="num">Cores</th><th class="num">RAM (GB)</th></tr>${hostRows}</table>`;
  const paneVms = `<table><tr><th>VM</th><th class="num">vCPU</th><th class="num">RAM (GB)</th></tr>${vmRows}</table>`;
  // ---- Netzwerk-Reiter: Portgruppen des Clusters (mit VLAN-/IP-Suche) ----
  const allPg = c.portgroups || [];
  const nPg = allPg.length;
  const nq = CARD_NET_Q.trim().toLowerCase();
  const shownPg = nq ? allPg.filter(p =>
      ((p.name || "") + " " + (p.vlan || "")).toLowerCase().includes(nq)) : allPg;
  const pgRows = shownPg.map(p =>
    `<tr><td>${esc(p.name)}</td><td class="num">${esc(p.vlan || "–")}</td></tr>`).join("")
    || `<tr><td colspan="2" style="color:var(--muted)">Keine Portgruppe passt zur Suche.</td></tr>`;
  const paneNet = nPg ? `<div class="netbox">
      <h3>Portgruppen <span style="color:var(--muted);font-weight:400">· ${nPg}</span></h3>
      <div class="vlanbar" style="margin-bottom:8px">
        <input id="cardNetQ" class="filterbox" style="max-width:340px"
               placeholder="VLAN / IP / Portgruppe suchen …" value="${esc(CARD_NET_Q)}"
               oninput="onCardNetInput()">
        <span id="cardNetCount" style="color:var(--muted);font-size:12px">${nq ? shownPg.length + " von " + nPg : ""}</span>
      </div>
      <table><thead><tr><th>Portgruppe</th><th class="num">VLAN</th></tr></thead>
        <tbody id="cardNetBody">${pgRows}</tbody></table>
    </div>`
    : `<div style="color:var(--muted);font-size:12px">Keine Portgruppen-Daten aus Aria.</div>`;

  // Gesamt-Karte hat keine Host-/VM-Listen
  // Reiter nach Sichtbarkeits-Matrix (Verwaltung -> Sichtbarkeit); der
  // Server strippt die Daten zusätzlich aus dem Payload (clusters_for).
  const avail = [["cpu", "CPU & RAM"]]
    .concat(VIS.storage ? [["storage", "Storage"]] : [])
    .concat(!isTotal && VIS.network ? [["net", "Netzwerk" + (nPg ? " (" + nPg + ")" : "")]] : [])
    .concat(!isTotal && VIS.hosts ? [["hosts", "Hosts (" + (c.hosts || []).length + ")"]] : [])
    .concat(!isTotal && VIS.vms ? [["vms", "VMs (" + c.vmCount + ")"]] : []);
  const tab = avail.some(t => t[0] === CARD_TAB) ? CARD_TAB : "cpu";
  const tabBar = `<div class="tabs ctabs">${avail.map(([k, l]) =>
    `<span class="tab ${tab === k ? "active" : ""}" onclick="setCardTab('${k}')">${l}</span>`).join("")}</div>`;
  const pane = tab === "storage" ? paneStorage : tab === "hosts" ? paneHosts
             : tab === "vms" ? paneVms : tab === "net" ? paneNet : paneCpu;

  return `<div class="card ${isTotal?'total':''}">
    <h2>${esc(c.name)}</h2>
    <div class="meta">${c.source?`Quelle: ${esc(c.source)} · `:''}${c.hostCount} Hosts · ${fmt(c.cores)} nutzbare Cores · ${c.vmCount} VMs${c.vmOff?` (davon ${c.vmOff} aus)`:''} · ${rv.length} genehmigt${clRes.filter(isPend).length?` / ${clRes.filter(isPend).length} beantragt`:''}${clRes.filter(r=>r.rejected).length?` / ${clRes.filter(r=>r.rejected).length} abgelehnt`:''}${spare}</div>
    ${tabBar}
    ${pane}
  </div>`;
}

// ---- Reiter in der Detailkarte ----
let CARD_TAB = "cpu";   // cpu | storage | net | hosts | vms
function setCardTab(t) { CARD_TAB = t; rerenderCard(); }

// ---- VLAN-/Portgruppen-Suche im Netzwerk-Reiter (nur diese Karte) ----
let CARD_NET_Q = "";
function onCardNetInput() {
  const el = document.getElementById("cardNetQ");
  CARD_NET_Q = el ? el.value : "";
  renderCardNetBody();     // nur die Tabelle neu füllen, Eingabefeld behält Fokus
}
function renderCardNetBody() {
  const body = document.getElementById("cardNetBody");
  if (!body) return;
  const c = hoverIdx >= 0 ? CLUSTERS[hoverIdx] : null;
  const all = (c && c.portgroups) || [];
  const q = CARD_NET_Q.trim().toLowerCase();
  const shown = q ? all.filter(p =>
      ((p.name || "") + " " + (p.vlan || "")).toLowerCase().includes(q)) : all;
  body.innerHTML = shown.map(p =>
    `<tr><td>${esc(p.name)}</td><td class="num">${esc(p.vlan || "–")}</td></tr>`).join("")
    || `<tr><td colspan="2" style="color:var(--muted)">Keine Portgruppe passt zur Suche.</td></tr>`;
  const cnt = document.getElementById("cardNetCount");
  if (cnt) cnt.textContent = q ? shown.length + " von " + all.length : "";
}

// ---- Storage-Detail (LUN-Liste) im Storage-Reiter ----
let DS_SORT = "size";
function setDsSort(m) { DS_SORT = m; rerenderCard(); }
function rerenderCard() {
  if (hoverIdx !== null && hc.style.display === "block")
    hc.innerHTML = '<button class="hc-close" title="Schließen" onclick="hideCard()">✕</button>' +
                   card(hoverIdx === -1 ? TOTAL : CLUSTERS[hoverIdx], hoverIdx, hoverIdx === -1);
}

// ---- Tabellenansicht mit Hover-Details ----
let TOTAL = null;

function srcBadge(src) {
  return src ? ` <span class="srcbadge" title="Datenquelle (vROps)">${esc(src)}</span>` : "";
}
function clusterSource(name) {
  const c = CLUSTERS.find(x => x.name === name);
  return c ? c.source : "";
}
function filteredIdx() {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  return CLUSTERS.map((c, i) => i)
                 .filter(i => (!q || CLUSTERS[i].name.toLowerCase().includes(q)
                              || (CLUSTERS[i].source || "").toLowerCase().includes(q))
                              && selMatch(CLUSTERS[i]));
}

function miniBar(used, resv, cap) {
  const pu = pct(used, cap), pr = pct(used + resv, cap) - pu;
  // Prozentwert als Tooltip: die Auslastung ist damit nicht nur über die Farbe
  // erkennbar (Farbsehschwäche).
  const tip = Math.round(pu + pr) + "% belegt" + (resv ? " (inkl. Reservierungen)" : "");
  return `<div class="bar mini" title="${tip}"><i style="width:${pu}%;background:${color(pu + pr)}"></i><i class="r" style="width:${pr}%"></i></div>`;
}

function row(c, idx, isTotal) {
  const clRes = isTotal ? RES.filter(r => TOTAL._names.has(r.cluster)) : allFor(c.name);
  const rv = clRes.filter(r => r.approved && !r.cancelled);
  const pend = clRes.filter(isPend).length;
  const tz = tzOf(c);
  const rvCpu = sumCpu(rv) + tz.cpu,
        rvRam = Math.round((sumRam(rv) + tz.ram) * 10) / 10,
        rvStor = sumStorage(rv);
  const fCpu = c.vcpuFree - rvCpu;
  const fRam = Math.round((c.ramFree - rvRam) * 10) / 10;
  const cCpu = color(pct(c.vcpuUsed + rvCpu, c.vcpuCap));
  const cRam = color(pct(c.ramUsed + rvRam, c.ramCap));
  const hasStor = (c.storageCap || 0) > 0;
  const fStor = Math.round(((c.storageFree || 0) - rvStor) * 10) / 10;
  const cStor = color(pct((c.storageUsed || 0) + rvStor, c.storageCap || 0));
  return `<tr class="${isTotal ? 'trtotal' : ''}">
    <td class="cl" title="Details anzeigen" onclick="toggleCard(${idx},this)">${esc(c.name)}${srcBadge(c.source)}</td>
    <td class="num">${fmt(c.hostCount)}</td>
    <td class="num">${fmt(c.vmCount)}</td>
    <td class="num free" style="color:${cCpu}">${fmt(fCpu)}</td>
    <td class="barcol">${miniBar(c.vcpuUsed, rvCpu, c.vcpuCap)}</td>
    <td class="num free" style="color:${cRam}">${fmt(fRam)}</td>
    <td class="barcol">${miniBar(c.ramUsed, rvRam, c.ramCap)}</td>
    <td class="num free ${hasStor ? 'cl' : ''}" style="color:${hasStor ? cStor : 'var(--muted)'}" title="${hasStor ? 'Datastores/LUNs anzeigen' : 'keine Storage-Daten aus Aria'}" ${hasStor ? `onclick="openStorage(${idx},this)"` : ''}>${hasStor ? fmt(fStor) : '–'}</td>
    <td class="barcol">${hasStor ? miniBar(c.storageUsed || 0, rvStor, c.storageCap) : ''}</td>
    <td class="num">${rv.length || "–"}${pend ? ` <span class="st pend">+${pend}</span>` : ""}</td></tr>`;
}

// ---- Ansichten: Kapazität / Reservierungen / Genehmigungen / Verwaltung ----
// endsWith statt ===, damit die Routen auch hinter einem Proxy-Unterpfad
// (z. B. https://host/capa/reservierungen) funktionieren
let VIEW = (location.pathname.endsWith("/storage") || location.hash === "#storage") ? "stor"
         : (location.pathname.endsWith("/vlan-suche") || location.hash === "#vlan-suche") ? "vlan"
         : (location.pathname.endsWith("/reservierungen") || location.hash === "#reservierungen") ? "res"
         : (location.pathname.endsWith("/genehmigungen") || location.hash === "#genehmigungen") ? "app"
         : (location.pathname.endsWith("/archiv") || location.hash === "#archiv") ? "arch"
         : (location.pathname.endsWith("/statistik") || location.hash === "#statistik") ? "stat"
         : (location.pathname.endsWith("/verwaltung") || location.hash === "#verwaltung") ? "adm"
         : (location.pathname.endsWith("/log") || location.hash === "#log") ? "log"
         : "kapa";
if ((VIEW === "adm" || VIEW === "log") && !IS_ADMIN) VIEW = "kapa";
if (VIEW === "stat" && (VIS.statistik === false || !SERVE)) VIEW = "kapa";

function setView(v) {
  VIEW = v;
  const tabs = { kapa: "tabKapa", vlan: "tabVlan", stor: "tabStor", res: "tabRes", app: "tabApp", arch: "tabArch", stat: "tabStat", adm: "tabAdm", log: "tabLog" };
  const views = { kapa: "kapaView", vlan: "vlanView", stor: "storView", res: "resView", app: "appView", arch: "archView", stat: "statView", adm: "admView", log: "logView" };
  for (const k in tabs) {
    document.getElementById(tabs[k]).classList.toggle("active", v === k);
    document.getElementById(views[k]).style.display = v === k ? "" : "none";
  }
  // Verwaltung hat eine eigene Filterbox im Tab „Benutzer und Rollen"
  document.getElementById("filter").style.display = (v === "vlan" || v === "stor" || v === "res" || v === "arch" || v === "stat" || v === "adm") ? "none" : "";
  document.getElementById("filter").placeholder =
    v === "kapa" ? "Cluster filtern …"
    : v === "log" ? "Log filtern …" : "Reservierungen filtern …";
  hideCard();   // VOR dem Hash-Schreiben: räumt ggf. den #cluster-Hash weg
  try {
    // Einen Deep-Link-Hash (#cluster=…) erhalten – er überlebt so das
    // initiale setView() und öffnet danach die Detailkarte.
    const keep = location.hash.startsWith("#cluster=") ? location.hash : "";
    history.replaceState(null, "",
      keep ? location.pathname + keep
      : v === "res" ? "#reservierungen" : v === "app" ? "#genehmigungen"
      : v === "arch" ? "#archiv" : v === "stat" ? "#statistik" : v === "adm" ? "#verwaltung" : v === "log" ? "#log"
      : v === "vlan" ? "#vlan-suche" : v === "stor" ? "#storage" : location.pathname);
  } catch (e) {}
  if (v === "stor") loadStorage();
  if (v === "stat") loadHistory();
  if (v === "adm") { loadRoles(); loadTokens(); loadTeams(); loadSelector(); loadRoleNames(); loadNotify(); loadWebhooks(); loadConfig(); loadAnnounce(); loadAutoApprove(); loadVisibility(); loadStorageCfg(); loadNetCfg(); loadImports(); loadRefreshCfg(); }
  if (v === "log") loadLog();
  render();
}

// ---- API-Tokens (Verwaltung) ----
let TOKENS = {};
async function apiTokens(method, path, body) {
  const r = await fetch("api/tokens" + (path || ""), {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadTokens() {
  apiTokens("GET").then(d => { TOKENS = d || {}; if (VIEW === "adm") render(); })
                  .catch(() => {});
}
// ---- Backup manuell auslösen ----
function runBackup() {
  const st = document.getElementById("backupStatus");
  const btn = document.getElementById("backupBtn");
  btn.disabled = true;
  st.style.color = "var(--muted)";
  st.textContent = "Backup läuft … (kann einen Moment dauern)";
  fetch("api/backup", { method: "POST" })
    .then(r => r.json())
    .then(d => {
      if (d.error) { st.style.color = "var(--crit)"; st.textContent = "Fehlgeschlagen: " + d.error; }
      else {
        st.style.color = "var(--ok)";
        st.textContent = "✓ Backup erstellt: " + d.backup
          + (d.rotated ? " · " + d.rotated + " alte(s) Archiv(e) gelöscht" : "");
      }
    })
    .catch(() => { st.style.color = "var(--crit)"; st.textContent = "Server nicht erreichbar."; })
    .finally(() => { btn.disabled = false; });
}
function addToken() {
  const n = document.getElementById("tknName").value.trim();
  if (!n) return;
  apiTokens("POST", "", { name: n }).then(d => {
    TOKENS = d.tokens || {};
    document.getElementById("newTokenVal").textContent = d.token;
    document.getElementById("newToken").style.display = "";
    render();
  }).catch(() => notify("Token konnte nicht erstellt werden."));
}
function delToken(id) {
  const t = TOKENS[id];
  askConfirm({ title: "API-Token widerrufen", okLabel: "Widerrufen", okClass: "danger",
    message: "„" + ((t && t.name) || id) + "“ widerrufen? Die Anwendung verliert sofort den Zugriff." })
    .then(ok => { if (!ok) return;
      apiTokens("DELETE", "/" + encodeURIComponent(id))
        .then(d => { TOKENS = d; render(); })
        .catch(() => notify("Widerruf fehlgeschlagen.")); });
}
function renderTokenTable() {
  const rows = Object.keys(TOKENS)
    .sort((a, b) => (TOKENS[a].created || "").localeCompare(TOKENS[b].created || ""))
    .map(id => { const t = TOKENS[id];
      return `<tr><td>${esc(t.name)}</td>
       <td style="font-family:monospace">${esc(t.prefix || "")}</td>
       <td>${fmtDate(t.created)}</td><td>${esc(t.created_by || "–")}</td>
       <td>${t.last_used ? esc(String(t.last_used).replace("T", " ")) : "nie"}</td>
       <td style="white-space:nowrap">
         <label title="POST /api/v1/reservations + /cancel"><input type="checkbox"
           ${t.write_res ? "checked" : ""} onchange="setTokenScope('${esc(id)}')"
           data-scope="res" data-tid="${esc(id)}"> Reservierungen</label><br>
         <label title="POST /api/v1/reservations/{id}/approve + /reject"><input type="checkbox"
           ${t.write_approve ? "checked" : ""} onchange="setTokenScope('${esc(id)}')"
           data-scope="approve" data-tid="${esc(id)}"> Genehmigungen</label><br>
         <label title="POST /api/v1/storage-requests/{id}/done"><input type="checkbox"
           ${t.write_storage ? "checked" : ""} onchange="setTokenScope('${esc(id)}')"
           data-scope="storage" data-tid="${esc(id)}"> Storage</label></td>
       <td><button class="del" onclick="delToken('${esc(id)}')">✕ Widerrufen</button></td></tr>`; })
    .join("");
  document.getElementById("ttbody").innerHTML =
    `<tr><td colspan="6"><input class="filterbox" style="width:100%" id="tknName"
       placeholder="Name der Anwendung, z. B. Grafana oder CMDB-Sync"
       onkeydown="if(event.key==='Enter')addToken()"></td>
     <td><button class="btn approve" onclick="addToken()">+ Token erzeugen</button></td></tr>` +
    (rows || `<tr><td colspan="7" style="color:var(--muted)">Keine API-Tokens vorhanden.</td></tr>`);
  reSort("ttable"); renderColMenu("ttable"); applyCols("ttable");
}
function setTokenScope(id) {
  const get = sc => { const el = document.querySelector(`input[data-tid="${CSS.escape(id)}"][data-scope="${sc}"]`);
                      return !!(el && el.checked); };
  fetch("api/tokens/" + encodeURIComponent(id), { method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ write_res: get("res"), write_approve: get("approve"), write_storage: get("storage") }) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => { if (d) { TOKENS = d; renderTokenTable(); } })
    .catch(() => notify("Ändern der Token-Rechte fehlgeschlagen."));
}

// ---- Audit-Log (nur Admins) ----
let LOGS = [];
function loadLog() {
  fetch("api/log").then(r => r.json())
    .then(d => { if (Array.isArray(d)) { LOGS = d; if (VIEW === "log") render(); } })
    .catch(() => {});
}
// Klick aufs Datumsfeld öffnet direkt den nativen Kalender (Browser folgt der
// Sprache: DE tt.mm.jjjj, EN mm/dd/yyyy). showPicker gibt es in modernen
// Browsern; sonst bleibt das native Kalender-Icon.
function openPicker(el) { try { if (el.showPicker) el.showPicker(); } catch (e) {} }
// ---- Generischer Seiten-Blätterer: EIN Mechanismus für alle großen Listen ----
// paginate(id, liste) liefert den Ausschnitt der aktuellen Seite und
// aktualisiert die Blätter-Leiste #pager_<id> (Zurück/Weiter + „pro Seite").
// Angebunden an: Kapazität, VLAN, Storage, Reservierungen, Genehmigungen,
// Archiv und Log – überall derselbe Code.
const PAGE_SIZES = [100, 200, 300];
const PAGE_STATE = {};            // tableId -> { page, size }
function pageState(id) {
  return PAGE_STATE[id] || (PAGE_STATE[id] = { page: 0, size: PAGE_SIZES[0] });
}
function pageReset(id) { if (id) pageState(id).page = 0; }   // z. B. nach neuer Suche
function pageGo(id, dir) { pageState(id).page += dir; (PAGE_RENDER[id] || function(){})(); }
function pageSize(id, n) {
  const st = pageState(id);
  st.size = parseInt(n) || PAGE_SIZES[0]; st.page = 0;
  (PAGE_RENDER[id] || function(){})();
}
function paginate(id, list) {
  const st = pageState(id);
  const pages = Math.max(1, Math.ceil(list.length / st.size));
  if (st.page > pages - 1) st.page = pages - 1;   // nach Filterwechsel in Reichweite halten
  if (st.page < 0) st.page = 0;
  const start = st.page * st.size;
  const slice = list.slice(start, start + st.size);
  renderPager(id, list.length, start, slice.length, pages, st);
  return slice;
}
function renderPager(id, total, start, shown, pages, st) {
  const bar = document.getElementById("pager_" + id);
  if (!bar) return;
  // Leiste erst zeigen, sobald mehr als die kleinste Seitengröße zusammenkommt
  if (total <= PAGE_SIZES[0]) { bar.style.display = "none"; bar.innerHTML = ""; return; }
  bar.style.display = "flex";
  const opts = PAGE_SIZES.map(n => `<option value="${n}"${n===st.size?" selected":""}>${n}</option>`).join("");
  bar.innerHTML =
    `<button class="btn" ${st.page<=0?"disabled":""} onclick="pageGo('${id}',-1)">← Zurück</button>
     <span style="color:var(--muted)">Einträge ${start+1}–${start+shown} · Seite ${st.page+1} von ${pages}</span>
     <button class="btn" ${st.page>=pages-1?"disabled":""} onclick="pageGo('${id}',1)">Weiter →</button>
     <label style="color:var(--muted);margin-left:auto">Pro Seite <select class="filterbox" onchange="pageSize('${id}',this.value)">${opts}</select></label>`;
}
// Zuordnung Tabelle -> Render-Funktion (Funktionsdeklarationen sind gehoistet).
const PAGE_RENDER = {
  ktable: render, vtable: renderVlan, stortable: renderStorage,
  rtable: renderResTable, atable: renderAppTable, artable: renderArchiveTable,
  ltable: renderLogTable,
};
function logClearDates() {
  document.getElementById("logFrom").value = "";
  document.getElementById("logTo").value = "";
  pageReset("ltable"); renderLogTable();
}
function logDatePage() { pageReset("ltable"); renderLogTable(); }   // Filter ändern -> Seite 1
function renderLogTable() {
  const q = (document.getElementById("filter").value || "").trim().toLowerCase();
  const from = (document.getElementById("logFrom") || {}).value || "";
  const to = (document.getElementById("logTo") || {}).value || "";
  const list = LOGS.filter(e => {
    if (q && !((e.user || "").toLowerCase().includes(q) ||
        (e.action || "").toLowerCase().includes(q) ||
        (e.detail || "").toLowerCase().includes(q))) return false;
    const day = (e.ts || "").slice(0, 10);       // ISO YYYY-MM-DD
    if (from && day < from) return false;
    if (to && day > to) return false;
    return true;
  });
  // Paginierung über den generischen Blätterer (LOGS ist neueste-zuerst)
  const slice = paginate("ltable", list);
  document.getElementById("ltbody").innerHTML = slice.map(e =>
    `<tr><td style="white-space:nowrap">${esc((e.ts || "").replace("T", " "))}</td>
     <td>${esc(e.user || "–")}</td><td>${esc(e.action || "")}</td>
     <td>${esc(e.detail || "")}</td></tr>`).join("") ||
    `<tr><td colspan="4" style="color:var(--muted)">Keine Log-Einträge${(q || from || to) ? " für diese Auswahl" : ""}.</td></tr>`;
  const info = document.getElementById("logInfo");
  if (info) info.textContent = list.length + (list.length === 1 ? " Eintrag" : " Einträge")
    + (list.length !== LOGS.length ? " (gefiltert von " + LOGS.length + ")" : "");
  reSort("ltable"); renderColMenu("ltable"); applyCols("ltable");
}

// ---- Verwaltung: AD-Benutzer → Rollen ----
let ROLES = {};   // {benutzer: rolle}
async function apiRoles(method, path, body) {
  const r = await fetch("api/roles" + (path || ""), {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadRoles() {
  apiRoles("GET").then(d => { ROLES = d || {}; if (VIEW === "adm") render(); })
                 .catch(() => {});
}
function addRole() {
  const kind = document.getElementById("admKind").value;
  let u = document.getElementById("admUser").value.trim();
  if (kind === "user") u = u.toLowerCase();
  const rl = document.getElementById("admRole").value;
  const ab = document.getElementById("admDept").value.trim();
  if (!u) return;
  apiRoles("POST", "", { user: u, role: rl, abteilung: ab, kind: kind, sources: collectSources("admSrc") })
    .then(d => { ROLES = d; render(); })
    .catch(() => notify("Speichern fehlgeschlagen."));
}
function delRole(u) {
  askConfirm({ title: "Rollenzuweisung entfernen", okLabel: "Entfernen", okClass: "danger",
    message: "Rollenzuweisung für „" + u + "“ entfernen?" }).then(ok => { if (!ok) return;
    apiRoles("DELETE", "/" + encodeURIComponent(u))
      .then(d => { ROLES = d; if (EDIT_USER === u) EDIT_USER = null; render(); })
      .catch(() => notify("Löschen fehlgeschlagen.")); });
}
// AD-Gruppen-Check: Mitglieder einer AD-Gruppe direkt im Verzeichnis nachschlagen,
// um zu prüfen, ob der Gruppenabruf (Service-Konto, Base-DN) sauber funktioniert.
const _grpChecking = {};
function checkGroup(cn) {
  if (_grpChecking[cn]) return;
  _grpChecking[cn] = true;
  fetch("api/ad/group-members", { method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({ cn: cn }) })
    .then(r => r.json())
    .then(d => {
      if (!d || d.error || !d.ok) {
        return askConfirm({ title: "AD-Gruppe: " + cn, okLabel: "Schließen", hideCancel: true,
          html: `<div style="color:var(--warn)">${esc((d && d.error) || "Unbekannter Fehler beim AD-Abruf.")}</div>` });
      }
      const m = d.members || [];
      const rows = m.length ? m.map(x =>
        `<div style="padding:4px 0;border-bottom:1px solid var(--line)">${esc(x.name || x.upn || x.sam || "?")}`
        + (x.upn ? ` <span style="color:var(--muted)">· ${esc(x.upn)}</span>` : "")
        + `</div>`).join("")
        : `<div style="color:var(--muted)">Keine direkten Benutzer-Mitglieder gefunden.</div>`;
      askConfirm({ title: "AD-Gruppe: " + cn, okLabel: "Schließen", hideCancel: true, html: `
        <div style="font-size:13px">
          <div style="margin-bottom:6px">${m.length} <span>direkte Mitglied(er)</span></div>
          ${d.truncated ? `<div style="color:var(--warn);font-size:12px;margin-bottom:8px">Liste gekürzt – es werden nicht alle Mitglieder angezeigt.</div>` : ""}
          <div style="color:var(--muted);font-size:11px;word-break:break-all;margin-bottom:10px">${esc(d.group_dn || "")}</div>
          <div style="max-height:300px;overflow:auto;padding-right:4px">${rows}</div>
          <div style="color:var(--muted);font-size:11px;margin-top:10px">Nur direkte Benutzer-Mitglieder — verschachtelte Gruppen werden nicht aufgelöst.</div>
        </div>` });
    })
    .catch(() => notify("AD-Abfrage fehlgeschlagen (Netzwerk/Server)."))
    .finally(() => { delete _grpChecking[cn]; });
}
let EDIT_USER = null;   // Benutzer, dessen Zeile gerade bearbeitet wird
function editRole(u) { EDIT_USER = u; render(); document.getElementById("editDept").focus(); }
function cancelEditRole() { EDIT_USER = null; render(); }
function saveEditRole(u) {
  const rl = document.getElementById("editRole").value;
  const ab = document.getElementById("editDept").value.trim();
  apiRoles("POST", "", { user: u, role: rl, abteilung: ab, sources: collectSources("editSrc") })
    .then(d => { ROLES = d; EDIT_USER = null; render(); })
    .catch(() => notify("Speichern fehlgeschlagen."));
}
// ---- Genehmigungs-Teams (Prüfreihenfolge) ----
async function apiTeams(method, body) {
  const r = await fetch("api/teams", {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadTeams() {
  apiTeams("GET").then(d => { TEAMS = (d && d.teams) || []; if (VIEW === "adm") render(); })
                 .catch(() => {});
}
function putTeams(list) {
  apiTeams("PUT", list).then(d => { TEAMS = (d && d.teams) || []; render(); })
                       .catch(() => notify("Speichern der Teams fehlgeschlagen."));
}
function addTeam() {
  const el = document.getElementById("newTeam");
  const t = (el.value || "").trim();
  if (!t) return;
  if (TEAMS.includes(t)) { notify("Team „" + t + "“ existiert bereits."); return; }
  el.value = "";
  putTeams(TEAMS.concat([t]));
}
function delTeam(i) {
  askConfirm({ title: "Team entfernen", okLabel: "Entfernen", okClass: "danger",
    message: "Team „" + TEAMS[i] + "“ aus dem Genehmigungsprozess entfernen?" })
    .then(ok => { if (ok) putTeams(TEAMS.filter((_, j) => j !== i)); });
}
function moveTeam(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= TEAMS.length) return;
  const t = TEAMS.slice();
  [t[i], t[j]] = [t[j], t[i]];
  putTeams(t);
}
let TEAM_EDIT = -1;   // Index des gerade umbenannten Teams
function editTeam(i) { TEAM_EDIT = i; render(); const el = document.getElementById("teamEdit"); if (el) { el.focus(); el.select(); } }
function cancelEditTeam() { TEAM_EDIT = -1; render(); }
function saveTeamRename(i) {
  const val = (document.getElementById("teamEdit").value || "").trim();
  const old = TEAMS[i];
  if (!val || val === old) { TEAM_EDIT = -1; render(); return; }
  fetch("api/teams/rename", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ old: old, new: val }) })
    .then(r => { if (!r.ok) throw new Error(); return r.json(); })
    .then(d => { if (d && d.teams) TEAMS = d.teams;
                 if (d && d.roles) ROLES = d.roles;
                 TEAM_EDIT = -1; render(); })
    .catch(() => notify("Umbenennen fehlgeschlagen (Name evtl. schon vergeben)."));
}
function teamMail(t) { return ((NOTIFY.team_email || {})[t]) || ""; }
function renderTeams() {
  const rows = TEAMS.map((t, i) => {
    const mailCell = `<td><input class="filterbox" style="width:100%" id="teamMail${i}"
         data-team="${esc(t)}" placeholder="team@firma.de" value="${esc(teamMail(t))}"></td>`;
    if (i === TEAM_EDIT) {
      return `<tr><td class="num">${i + 1}</td>
        <td><input class="filterbox" style="width:100%" id="teamEdit" value="${esc(t)}"
             onkeydown="if(event.key==='Enter')saveTeamRename(${i});if(event.key==='Escape')cancelEditTeam()"></td>
        ${mailCell}
        <td><button class="btn approve" onclick="saveTeamRename(${i})">✓ Speichern</button>
            <button class="btn" onclick="cancelEditTeam()">Abbrechen</button></td></tr>`;
    }
    return `<tr><td class="num">${i + 1}</td><td>${esc(t)}</td>
     ${mailCell}
     <td><button class="edit" title="nach oben" ${i === 0 ? "disabled" : ""} onclick="moveTeam(${i},-1)">↑</button>
         <button class="edit" title="nach unten" ${i === TEAMS.length - 1 ? "disabled" : ""} onclick="moveTeam(${i},1)">↓</button>
         <button class="edit" title="Team umbenennen" onclick="editTeam(${i})">✎ Umbenennen</button>
         <button class="del" title="Team entfernen" onclick="delTeam(${i})">✕ Entfernen</button></td></tr>`;
  }).join("");
  document.getElementById("tmbody").innerHTML =
    `<tr><td></td><td><input class="filterbox" style="width:100%" id="newTeam"
         placeholder="Neues Team, z. B. Team Betrieb"
         onkeydown="if(event.key==='Enter')addTeam()"></td><td></td>
     <td><button class="btn approve" onclick="addTeam()">+ Hinzufügen</button></td></tr>` +
    (rows || `<tr><td colspan="4" style="color:var(--muted)">Keine Teams – einstufig (Admin genehmigt direkt).</td></tr>`);
}

// ---- Mail-Benachrichtigungen (Matrix Rolle × Ereignis) ----
async function apiNotify(method, body) {
  const r = await fetch("api/notify", { method: method,
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadNotify() {
  apiNotify("GET").then(d => { if (d && d.notify) NOTIFY = d.notify;
                               if (d && d.vars) MAIL_VARS = d.vars;
                               if (d && d.default_template) MAIL_DEF_TPL = d.default_template;
                               if (d && d.default_subject) MAIL_DEF_SUBJ = d.default_subject;
                               if (VIEW === "adm") render(); }).catch(() => {});
}
function renderNotify() {
  const rows = ROLE_ORDER.map(role => {
    const rc = (NOTIFY.role || {})[role] || {};
    const allowed = NOTIFY_ROLE_EVENTS[role] || [];
    const addr = role === "anforderer"
        ? `<span style="color:var(--muted)">= Antragsteller (automatisch)</span>`
      : role === "reviewer"
        ? `<span style="color:var(--muted)">pro Team (Tabelle oben)</span>`
        : `<input class="filterbox" style="width:100%" id="nt_email_${role}"
             placeholder="verteiler@firma.de" value="${esc(rc.email || "")}">`;
    const cells = NOTIFY_EVENTS.map(([ev]) => allowed.includes(ev)
        ? `<td class="num"><input type="checkbox" id="nt_${role}_${ev}" ${rc[ev] ? "checked" : ""}></td>`
        : `<td class="num" style="color:var(--muted)">–</td>`).join("");
    return `<tr><td>${esc(ROLE_NAMES[role] || role)} <span style="color:var(--muted)">(${role})</span></td>
      <td>${addr}</td>${cells}</tr>`;
  }).join("");
  document.getElementById("ntbody").innerHTML = rows;
  const rd = document.getElementById("ntReminderDays");
  if (rd && document.activeElement !== rd) rd.value = NOTIFY.reminder_days || 2;
}
function collectNotifyBody() {
  const body = { role: {}, team_email: {} };
  ROLE_ORDER.forEach(role => {
    const rc = {};
    (NOTIFY_ROLE_EVENTS[role] || []).forEach(ev => {
      const el = document.getElementById("nt_" + role + "_" + ev);
      if (el) rc[ev] = el.checked;
    });
    const em = document.getElementById("nt_email_" + role);
    if (em) rc.email = em.value.trim();
    body.role[role] = rc;
  });
  document.querySelectorAll('#tmbody input[id^="teamMail"]').forEach(inp => {
    const t = inp.getAttribute("data-team"); const v = (inp.value || "").trim();
    if (t && v) body.team_email[t] = v;
  });
  const s = document.getElementById("tplSubject"), h = document.getElementById("tplHtml");
  body.template_subject = s ? s.value.trim() : (NOTIFY.template_subject || "");
  body.template_html = h ? h.value : (NOTIFY.template_html || "");
  const rd = document.getElementById("ntReminderDays");
  body.reminder_days = rd ? (parseInt(rd.value, 10) || 2) : (NOTIFY.reminder_days || 2);
  return body;
}
function saveNotify() {
  const body = collectNotifyBody();
  apiNotify("PUT", body).then(d => {
    if (d && d.notify) NOTIFY = d.notify;
    const st = document.getElementById("notifySaved");
    if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); }
    render();
  }).catch(() => notify("Speichern der Mail-Regeln fehlgeschlagen."));
}

// ---- Editierbare Mail-Vorlage (Betreff + HTML) ----
function renderMailTemplate() {
  const vb = document.getElementById("mailVars");
  if (vb) vb.innerHTML = (MAIL_VARS || []).map(([k, d]) =>
    `<button class="btn" style="margin:0 6px 6px 0;font-family:monospace" title="${esc(d)}"
       onclick="insertMailVar('{{${k}}}')">{{${esc(k)}}}</button>`).join("") || "";
  const s = document.getElementById("tplSubject");
  const h = document.getElementById("tplHtml");
  if (s && document.activeElement !== s) s.value = NOTIFY.template_subject || "";
  if (h && document.activeElement !== h) h.value = NOTIFY.template_html || "";
  if (s) s.placeholder = MAIL_DEF_SUBJ || "Standardbetreff";
}
let MAIL_TPL_FOCUS = "tplHtml";
function insertMailVar(v) {
  const el = document.getElementById(MAIL_TPL_FOCUS) || document.getElementById("tplHtml");
  if (!el) return;
  const s = el.selectionStart || 0, e = el.selectionEnd || 0;
  el.value = el.value.slice(0, s) + v + el.value.slice(e);
  el.focus(); el.selectionStart = el.selectionEnd = s + v.length;
}
function saveMailTemplate() {
  const body = collectNotifyBody();
  apiNotify("PUT", body).then(d => {
    if (d && d.notify) NOTIFY = d.notify;
    const st = document.getElementById("tplSaved");
    if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); }
  }).catch(() => notify("Speichern der Mail-Vorlage fehlgeschlagen."));
}
function resetMailTemplate() {
  const h = document.getElementById("tplHtml"), s = document.getElementById("tplSubject");
  if (h) h.value = MAIL_DEF_TPL || "";
  if (s) s.value = MAIL_DEF_SUBJ || "";
}
// ---- Ankündigung pflegen (Verwaltung -> Ankündigung) ----
let ANN_CFG = null;
function loadAnnounce() {
  fetch("api/announce").then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.announce) { ANN_CFG = d.announce; if (VIEW === "adm") renderAnnounce(); }
  }).catch(() => {});
}
function renderAnnounce() {
  if (!ANN_CFG) return;
  const t = document.getElementById("annCfgTitle");
  const x = document.getElementById("annCfgText");
  const a = document.getElementById("annCfgActive");
  if (t && document.activeElement !== t) t.value = ANN_CFG.title || "";
  if (x && document.activeElement !== x) x.value = ANN_CFG.text || "";
  if (a) a.checked = !!ANN_CFG.active;
  const m = document.getElementById("annMeta");
  if (m) m.textContent = ANN_CFG.updated_on
    ? "Zuletzt geändert: " + ANN_CFG.updated_on +
      (ANN_CFG.updated_by ? " durch " + ANN_CFG.updated_by : "") : "";
}
function saveAnnounce() {
  const body = {
    title: (document.getElementById("annCfgTitle") || {}).value || "",
    text: (document.getElementById("annCfgText") || {}).value || "",
    active: !!(document.getElementById("annCfgActive") || {}).checked };
  fetch("api/announce", { method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {
      if (d && d.announce) { ANN_CFG = d.announce; renderAnnounce(); }
      const st = document.getElementById("annSaved");
      if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); }
    }).catch(() => notify("Speichern der Ankündigung fehlgeschlagen."));
}
// ---- Sichtbarkeits-Matrix pflegen (Verwaltung -> Sichtbarkeit) ----
let VIS_CFG = null;
const VIS_LABELS = { workload: "Workload %", hosts: "Host-Liste",
  vms: "VM-Liste", network: "Netzwerk & VLAN-Suche",
  storage: "Storage-Drilldown (LUNs)", tags: "vSphere-Tags",
  decided_by: "Entschieden von", statistik: "Statistik (Trends)" };
function loadVisibility() {
  fetch("api/visibility").then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.visibility) { VIS_CFG = d; if (VIEW === "adm") renderVisibility(); }
  }).catch(() => {});
}
function renderVisibility() {
  if (!VIS_CFG) return;
  const roles = VIS_CFG.roles || [];
  const feats = VIS_CFG.features || [];
  document.getElementById("vishead").innerHTML =
    `<tr><th>Merkmal</th>${roles.map(r =>
      `<th class="num">${esc(ROLE_NAMES[r] || r)}</th>`).join("")}</tr>`;
  document.getElementById("visbody").innerHTML = feats.map(f =>
    `<tr><td>${esc(VIS_LABELS[f] || f)}</td>${roles.map(r =>
      `<td class="num"><input type="checkbox" data-visrole="${esc(r)}" data-visfeat="${esc(f)}"
         ${(VIS_CFG.visibility[r] || {})[f] ? "checked" : ""}></td>`).join("")}</tr>`).join("");
}
function saveVisibility() {
  const body = {};
  document.querySelectorAll("input[data-visrole]").forEach(cb => {
    const r = cb.getAttribute("data-visrole"), f = cb.getAttribute("data-visfeat");
    (body[r] = body[r] || {})[f] = cb.checked;
  });
  fetch("api/visibility", { method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {
      if (d && d.visibility) { VIS_CFG.visibility = d.visibility; renderVisibility(); }
      const st = document.getElementById("visSaved");
      if (st) { st.textContent = "✓ gespeichert – gilt beim nächsten Laden der Seite"; setTimeout(() => { if (st) st.textContent = ""; }, 4000); }
    }).catch(() => notify("Speichern der Sichtbarkeit fehlgeschlagen."));
}

// ---- Storage-Einstellungen (inkl. vSAN-Policy-Faktor) ----
// vROps liefert keinen sauberen „nutzbar"-Wert; die Faktoren sind Annahmen je
// RAID-Policy. RAID-5 (2+1) und RAID-6 haben beide ~0,667.
const VSAN_POLICIES = [
  ["RAID-1 (FTT=1) · 50 %", 0.5], ["RAID-1 (FTT=2) · 33 %", 0.333],
  ["RAID-5 (3+1) · 75 %", 0.75], ["RAID-5 (2+1) · 67 %", 0.667],
  ["RAID-6 (4+2) · 67 %", 0.667]];
function vsanOptions(sel) {
  const match = VSAN_POLICIES.some(([l,f]) => Math.abs(f - sel) < 0.005);
  return `<option value=""${match ? "" : " selected"}>eigener Wert</option>`
    + VSAN_POLICIES.map(([l,f]) =>
        `<option value="${f}"${Math.abs(f-sel)<0.005?" selected":""}>${esc(l)}</option>`).join("");
}
function vsanPresetToFactor(selId, facId) {
  const v = (document.getElementById(selId) || {}).value;
  const f = document.getElementById(facId);
  if (v && f) f.value = v;
}
function vsanFactorToPreset(facId, selId) {
  const f = parseFloat((document.getElementById(facId) || {}).value);
  const sel = document.getElementById(selId); if (!sel) return;
  let m = false;
  [...sel.options].forEach(o => {
    if (o.value && !m && Math.abs(parseFloat(o.value) - f) < 0.005) { sel.value = o.value; m = true; } });
  if (!m) sel.value = "";
}
function vsanOvrRow(cl, f) {
  return `<tr>
    <td><input class="filterbox vovrCl" style="width:160px" value="${esc(cl)}" placeholder="Cluster-Name"></td>
    <td><select class="filterbox vovrPol" style="width:auto" onchange="const t=this.closest('tr').querySelector('.vovrFac'); if(this.value) t.value=this.value">${vsanOptions(f)}</select></td>
    <td class="num"><input class="filterbox vovrFac" type="number" min="0.05" max="1" step="0.01" style="width:80px" value="${f}"></td>
    <td><button class="del" title="Override entfernen" onclick="this.closest('tr').remove()">✕</button></td>
  </tr>`;
}
function renderVsanOverrides(ovr) {
  const tb = document.getElementById("vsanOvrBody"); if (!tb) return;
  const rows = Object.entries(ovr || {});
  tb.innerHTML = rows.length ? rows.map(([cl,f]) => vsanOvrRow(cl, f)).join("") : "";
}
function addVsanOverride() {
  const tb = document.getElementById("vsanOvrBody");
  if (tb) tb.insertAdjacentHTML("beforeend", vsanOvrRow("", 0.5));
}
function collectVsanOverrides() {
  const out = {};
  [...document.querySelectorAll("#vsanOvrBody tr")].forEach(tr => {
    const cl = ((tr.querySelector(".vovrCl") || {}).value || "").trim();
    const f = parseFloat((tr.querySelector(".vovrFac") || {}).value);
    if (cl && f >= 0.05 && f <= 1) out[cl] = Math.round(f * 10000) / 10000;
  });
  return out;
}
function saveStorageCfg() {
  const on = !!(document.getElementById("storEnabled") || {}).checked;
  const mn = parseInt((document.getElementById("storMinLun") || {}).value, 10) || 0;
  const ex = (document.getElementById("storExclNames") || {}).value || "";
  const mxTb = parseFloat((document.getElementById("storMaxLun") || {}).value) || 0;
  const mx = Math.round(mxTb * 1024);   // Eingabe in TB, Speicherung in GB
  const vfac = parseFloat((document.getElementById("vsanFactor") || {}).value) || 0.5;
  fetch("api/storagecfg", { method: "PUT", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ enabled: on, min_lun_gb: mn, max_lun_gb: mx, exclude_names: ex,
                             vsan_factor: vfac, vsan_overrides: collectVsanOverrides() }) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => { STOR.enabled = !!d.enabled; STOR.max_lun_gb = d.max_lun_gb || 0;
      const vf = document.getElementById("vsanFactor");
      const vp = document.getElementById("vsanPolicy");
      if (d.vsan_factor != null) { if (vf) vf.value = d.vsan_factor; if (vp) vp.innerHTML = vsanOptions(d.vsan_factor); }
      renderVsanOverrides(d.vsan_overrides || {});
      ["storCfgSaved","storMinSaved"].forEach(id => { const st = document.getElementById(id);
        if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); } }); })
    .catch(() => notify("Speichern fehlgeschlagen."));
}
function loadStorageCfg() {
  fetch("api/storagecfg").then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    const el = document.getElementById("storEnabled");
    if (el) el.checked = !!d.enabled;
    const mn = document.getElementById("storMinLun");
    if (mn && document.activeElement !== mn) mn.value = d.min_lun_gb || 0;
    const ex = document.getElementById("storExclNames");
    if (ex && document.activeElement !== ex) ex.value = d.exclude_names || "";
    const mx = document.getElementById("storMaxLun");
    if (mx && document.activeElement !== mx) mx.value = d.max_lun_gb ? (d.max_lun_gb / 1024) : "";
    const fac = (d.vsan_factor != null ? d.vsan_factor : 0.5);
    const vp = document.getElementById("vsanPolicy"); if (vp) vp.innerHTML = vsanOptions(fac);
    const vf = document.getElementById("vsanFactor");
    if (vf && document.activeElement !== vf) vf.value = fac;
    renderVsanOverrides(d.vsan_overrides || {});
  }).catch(() => {});
}

// ---- Netzwerk-Filter pflegen (Verwaltung -> Netzwerk) ----
function loadNetCfg() {
  fetch("api/netcfg").then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    const nx = document.getElementById("netExclNames");
    if (nx && document.activeElement !== nx) nx.value = d.exclude_names || "";
    const vx = document.getElementById("netExclVlans");
    if (vx && document.activeElement !== vx) vx.value = d.exclude_vlans || "";
  }).catch(() => {});
}
function saveNetCfg() {
  const nx = (document.getElementById("netExclNames") || {}).value || "";
  const vx = (document.getElementById("netExclVlans") || {}).value || "";
  fetch("api/netcfg", { method: "PUT", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ exclude_names: nx, exclude_vlans: vx }) })
    .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
    .then(d => {
      const nx2 = document.getElementById("netExclNames");
      if (nx2) nx2.value = d.exclude_names || "";
      const vx2 = document.getElementById("netExclVlans");
      if (vx2) vx2.value = d.exclude_vlans || "";
      const ok = document.getElementById("netCfgSaved");
      if (ok) { ok.textContent = "✓ gespeichert";
                setTimeout(() => { ok.textContent = ""; }, 2500); }
      pollStatus();
    })
    .catch(e => notify("Speichern fehlgeschlagen" +
                       (e && e.message ? " (" + e.message + ")" : "") + "."));
}

// ---- Abruf-Intervalle (Verwaltung -> Backup & Konfiguration) ----
function loadRefreshCfg() {
  fetch("api/refreshcfg").then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    [["rcVms","vms"],["rcNetwork","network"],["rcStorage","storage"]].forEach(([id, k]) => {
      const el = document.getElementById(id);
      if (el && document.activeElement !== el) el.value = d.tiers[k] || "";
    });
    const df = document.getElementById("rcDefault");
    if (df) df.textContent = "(leer = Standard: " + d.default_min + " min)";
    const tz = document.getElementById("rcTz");
    if (tz && document.activeElement !== tz) tz.value = d.tz || "";
    showServerNow(d.now, d.tz);
  }).catch(() => {});
}
function showServerNow(now, tz) {
  const el = document.getElementById("rcNow");
  if (el) el.textContent = now
    ? ("aktuell am Server: " + now + (tz ? " (" + tz + ")" : "")) : "";
}
function saveRefreshCfg() {
  const val = id => parseInt((document.getElementById(id) || {}).value) || 0;
  const tz = ((document.getElementById("rcTz") || {}).value || "").trim();
  fetch("api/refreshcfg", { method: "PUT", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ vms: val("rcVms"), network: val("rcNetwork"),
                             storage: val("rcStorage"), tz: tz }) })
    .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)))
    .then(d => { const ok = document.getElementById("rcSaved");
      if (ok) { ok.textContent = "✓ gespeichert";
                setTimeout(() => { ok.textContent = ""; }, 2500); }
      showServerNow(d.now, d.tz); })
    .catch(e => notify((e && e.error) ? e.error : "Speichern fehlgeschlagen."));
}

// ---- Statistik: Trends aus der Tages-Historie (selbst gezeichnete SVGs) ----
let HISTORY = null;   // {tag: {cluster: {n,on,vcpu,ram,disk,nd,ramU,ramC,stU,stC,hr,src}}}
function loadHistory() {
  fetch("api/history?days=730").then(r => r.ok ? r.json() : Promise.reject())
    .then(d => { HISTORY = d.days || {}; fillStatClusters(); renderStats(); })
    .catch(() => { const b = document.getElementById("statCharts");
      if (b) b.innerHTML = `<div class="hint" style="color:var(--muted)">Noch keine Statistik-Daten (erster Snapshot entsteht mit dem nächsten Datenabruf).</div>`; });
}
function fillStatClusters() {
  const sel = document.getElementById("statCluster");
  if (!sel || !HISTORY) return;
  const names = new Set();
  Object.values(HISTORY).forEach(day => Object.keys(day).forEach(n => names.add(n)));
  const cur = sel.value;
  sel.innerHTML = `<option value="">alle</option>` + [...names].sort().map(n =>
    `<option value="${esc(n)}"${n === cur ? " selected" : ""}>${esc(n)}</option>`).join("");
}
function statSeries(days, cluster) {
  // je Tag über die gewählten Cluster aggregieren
  return days.map(d => {
    const day = HISTORY[d];
    let n = 0, on = 0, vcpu = 0, ram = 0, disk = 0, nd = 0,
        ramU = 0, ramC = 0, stU = 0, stC = 0, hr = [0,0,0,0,0,0];
    Object.keys(day).forEach(cl => {
      if (cluster && cl !== cluster) return;
      const e = day[cl];
      n += e.n || 0; on += e.on || 0; vcpu += e.vcpu || 0; ram += e.ram || 0;
      disk += e.disk || 0; nd += e.nd || 0;
      ramU += e.ramU || 0; ramC += e.ramC || 0; stU += e.stU || 0; stC += e.stC || 0;
      (e.hr || []).forEach((x, i) => { if (i < hr.length) hr[i] += x; });
    });
    return { d: d, n: n, on: on, vcpu: vcpu, ram: ram, disk: disk, nd: nd,
             ramU: ramU, ramC: ramC, stU: stU, stC: stC, hr: hr };
  });
}
function chartCard(title, pts, unit, dec) {
  // pts: [{d, y}] — eine Linie, Fläche darunter, min/max-Achse, letzter Wert groß
  const vals = pts.map(p => p.y).filter(v => v !== null && isFinite(v));
  if (!vals.length) return "";
  const w = 400, h = 170, padL = 8, padR = 8, padT = 30, padB = 22;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) { hi += 1; lo = Math.max(0, lo - 1); }
  const span = hi - lo;
  lo = Math.max(0, lo - span * 0.1); hi += span * 0.1;
  const X = i => padL + (w - padL - padR) * (pts.length < 2 ? 0.5 : i / (pts.length - 1));
  const Y = v => padT + (h - padT - padB) * (1 - (v - lo) / (hi - lo));
  const line = pts.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
  const area = line + `L${X(pts.length - 1).toFixed(1)},${h - padB}L${X(0).toFixed(1)},${h - padB}Z`;
  const f = v => v.toLocaleString("de-DE", { maximumFractionDigits: dec });
  const last = pts[pts.length - 1], first = pts[0];
  const delta = last.y - first.y;
  const dTxt = (delta >= 0 ? "+" : "") + f(delta) + (unit ? " " + unit : "");
  const dCol = delta > 0 ? "var(--warn)" : "var(--ok)";
  return `<div class="resbox" style="padding:12px 14px">
    <div style="display:flex;align-items:baseline;gap:8px">
      <b style="font-size:13px">${esc(title)}</b>
      <span style="margin-left:auto;font-size:18px;font-weight:700">${f(last.y)}${unit ? " " + esc(unit) : ""}</span>
      <span style="font-size:11px;color:${dCol}" title="Veränderung im Zeitraum">${esc(dTxt)}</span>
    </div>
    <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;display:block" preserveAspectRatio="none">
      <path d="${area}" fill="var(--accent)" opacity="0.12"></path>
      <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2"></path>
      <text x="${padL}" y="${h - 6}" font-size="10" fill="var(--muted)">${esc(fmtDate(first.d))}</text>
      <text x="${w - padR}" y="${h - 6}" font-size="10" fill="var(--muted)" text-anchor="end">${esc(fmtDate(last.d))}</text>
      <text x="${padL}" y="${padT - 16}" font-size="10" fill="var(--muted)">max ${f(hi)}</text>
    </svg></div>`;
}
function histoCard(first, last) {
  const labels = ["≤4", "≤8", "≤16", "≤32", "≤64", ">64"];
  const mx = Math.max(1, ...first.hr, ...last.hr);
  const bars = labels.map((lb, i) => {
    const h1 = Math.round((first.hr[i] || 0) / mx * 90);
    const h2 = Math.round((last.hr[i] || 0) / mx * 90);
    return `<div style="display:flex;flex-direction:column;align-items:center;gap:2px;flex:1">
      <div style="display:flex;align-items:flex-end;gap:3px;height:96px">
        <div title="früher: ${first.hr[i] || 0}" style="width:14px;height:${h1}px;background:var(--muted);opacity:.45;border-radius:3px 3px 0 0"></div>
        <div title="heute: ${last.hr[i] || 0}" style="width:14px;height:${h2}px;background:var(--accent);border-radius:3px 3px 0 0"></div>
      </div>
      <span style="font-size:10px;color:var(--muted)">${lb}</span></div>`;
  }).join("");
  return `<div class="resbox" style="padding:12px 14px">
    <div style="font-size:13px"><b>VM-Größenklassen nach RAM (GB)</b>
      <span style="font-size:11px;color:var(--muted);margin-left:8px">grau = ${esc(fmtDate(first.d))} · farbig = ${esc(fmtDate(last.d))}</span></div>
    <div style="display:flex;gap:6px;margin-top:10px">${bars}</div></div>`;
}
function renderStats() {
  const box = document.getElementById("statCharts");
  if (!box || !HISTORY) return;
  const rangeDays = parseInt(document.getElementById("statRange").value) || 365;
  const cluster = document.getElementById("statCluster").value || "";
  const cutoff = new Date(Date.now() - rangeDays * 86400000).toISOString().slice(0, 10);
  const days = Object.keys(HISTORY).filter(d => d >= cutoff).sort();
  const info = document.getElementById("statInfo");
  if (days.length < 2) {
    box.innerHTML = `<div class="hint" style="color:var(--muted)">Noch zu wenig Historie im gewählten Zeitraum (mindestens 2 Tage nötig). Die Daten wachsen mit jedem Tagesabruf.</div>`;
    if (info) info.textContent = days.length + (days.length === 1 ? " Datenpunkt" : " Datenpunkte");
    return;
  }
  const S = statSeries(days, cluster);
  if (info) info.textContent = days.length + " Datenpunkte · " + (cluster || "alle Cluster");
  const pick = (fn, dec) => S.map(e => ({ d: e.d, y: fn(e) })).filter(p => p.y !== null && isFinite(p.y));
  const cards = [
    chartCard("Ø RAM je VM", pick(e => e.n ? e.ram / e.n : null), "GB", 1),
    chartCard("Ø vCPU je VM", pick(e => e.n ? e.vcpu / e.n : null), "", 1),
    chartCard("Ø Disk je VM", pick(e => e.nd ? e.disk / e.nd : null), "GB", 0),
    chartCard("VM-Anzahl", pick(e => e.n), "", 0),
    chartCard("RAM-Auslastung", pick(e => e.ramC ? e.ramU / e.ramC * 100 : null), "%", 1),
    chartCard("Storage-Auslastung", pick(e => e.stC ? e.stU / e.stC * 100 : null), "%", 1),
  ].filter(Boolean);
  cards.push(histoCard(S[0], S[S.length - 1]));
  box.innerHTML = cards.join("");
}

// ---- Webhooks (Verwaltung -> Webhooks) ----
const WEBHOOK_EVENTS = [
  ["created","Anfrage angelegt"],["approved","Anfrage genehmigt"],
  ["rejected","Anfrage abgelehnt"],["team_turn","Team ist dran"],
  ["reminder","Erinnerung"],["storage_requested","Storage-Anfrage angelegt"],
  ["storage_done","Storage-Anfrage erledigt"],["imported","Offline-Quelle importiert"]];
let WEBHOOKS = [];
function loadWebhooks() {
  fetch("api/webhooks").then(r => r.ok ? r.json() : null).then(d => {
    if (d && Array.isArray(d.endpoints)) { WEBHOOKS = d.endpoints; renderWebhooks(); }
  }).catch(() => {});
}
function whEventsCell(selected) {
  const sel = new Set(selected || []);
  return `<div style="display:flex;flex-wrap:wrap;gap:2px 10px;max-width:360px">`
    + WEBHOOK_EVENTS.map(([k,l]) => `<label style="font-size:12px;white-space:nowrap;cursor:pointer"><input type="checkbox" class="whEv" data-ev="${k}"${sel.has(k)?" checked":""}> ${esc(l)}</label>`).join("")
    + `</div>`;
}
function renderWebhooks() {
  const tb = document.getElementById("whbody"); if (!tb) return;
  tb.innerHTML = WEBHOOKS.map(w => `<tr data-wid="${esc(w.id||"")}">
    <td><input type="checkbox" class="whActive"${w.active!==false?" checked":""}></td>
    <td><input class="filterbox whUrl" style="width:100%;min-width:210px" value="${esc(w.url||"")}" placeholder="https://…"></td>
    <td>${whEventsCell(w.events)}</td>
    <td><input class="filterbox whSecret" type="password" autocomplete="new-password" style="width:150px" placeholder="${w.has_secret?"•••• (gesetzt)":"optional"}"></td>
    <td><input class="filterbox whDesc" style="width:130px" value="${esc(w.desc||"")}" placeholder="z. B. AWX"></td>
    <td style="white-space:nowrap"><button class="btn" onclick="testWebhook(this)">Test</button> <button class="del" title="Entfernen" onclick="this.closest('tr').remove()">✕</button></td>
  </tr>`).join("") || `<tr><td colspan="6" style="color:var(--muted)">Noch kein Webhook konfiguriert.</td></tr>`;
}
function addWebhook() {
  WEBHOOKS = collectWebhooks();
  WEBHOOKS.push({ id:"", url:"", events:[], active:true, desc:"", has_secret:false });
  renderWebhooks();
}
function collectWebhooks() {
  return [...document.querySelectorAll("#whbody tr[data-wid]")].map(tr => {
    const url = ((tr.querySelector(".whUrl")||{}).value || "").trim();
    if (!url) return null;
    return { id: tr.getAttribute("data-wid") || "", url: url,
      active: (tr.querySelector(".whActive")||{}).checked !== false,
      secret: (tr.querySelector(".whSecret")||{}).value || "",
      desc: (tr.querySelector(".whDesc")||{}).value || "",
      has_secret: tr.querySelector(".whSecret") && tr.querySelector(".whSecret").placeholder.indexOf("gesetzt") >= 0,
      events: [...tr.querySelectorAll(".whEv:checked")].map(c => c.getAttribute("data-ev")) };
  }).filter(Boolean);
}
function saveWebhooks() {
  fetch("api/webhooks", { method:"PUT", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ endpoints: collectWebhooks() }) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => { if (d.endpoints) { WEBHOOKS = d.endpoints; renderWebhooks(); }
      const ok = document.getElementById("whSaved");
      if (ok) { ok.textContent = "✓ gespeichert"; setTimeout(()=>{ok.textContent="";},2500); } })
    .catch(() => notify("Speichern fehlgeschlagen."));
}
function testWebhook(btn) {
  const tr = btn.closest("tr");
  const url = ((tr.querySelector(".whUrl")||{}).value || "").trim();
  if (!url) return notify("Bitte zuerst eine Ziel-URL eintragen.");
  const secret = (tr.querySelector(".whSecret")||{}).value || "";
  btn.disabled = true; const old = btn.textContent; btn.textContent = "…";
  fetch("api/webhooks/test", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ url: url, secret: secret }) })
    .then(r => r.json())
    .then(d => notify(d.ok ? ("✓ Test gesendet – Empfänger antwortete mit HTTP " + d.status)
                            : ("Test fehlgeschlagen: " + (d.error || "unbekannt"))))
    .catch(() => notify("Test fehlgeschlagen (Netzwerk/Server)."))
    .then(() => { btn.disabled = false; btn.textContent = old; });
}
// ---- Offline-Quellen (Verwaltung -> Import) ----
let IMPORTS = [];
function loadImports() {
  fetch("api/import").then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.sources) { IMPORTS = d.sources; renderImports(); }
  }).catch(() => {});
}
function renderImports() {
  const tb = document.getElementById("impbody");
  if (!tb) return;
  tb.innerHTML = IMPORTS.map(s => `<tr>
    <td>${esc(s.name)}</td><td class="num">${s.clusters}</td>
    <td class="num">${s.hosts}</td><td class="num">${fmt(s.vms)}</td>
    <td>${esc(fmtDate(s.imported_on))}</td><td>${esc(s.imported_by || "–")}</td>
    <td><button class="del" title="Offline-Quelle entfernen" onclick="delImport('${esc(s.name)}')">✕ Löschen</button></td>
  </tr>`).join("") ||
    `<tr><td colspan="7" style="color:var(--muted)">Noch keine Offline-Quelle importiert.</td></tr>`;
}
function importOffline(ev) {
  const f = ev.target.files[0]; if (!f) return;
  ev.target.value = "";
  const src = (document.getElementById("impSource").value || "").trim();
  if (!src) { notify("Bitte zuerst einen Quellnamen eingeben (z. B. RZ-Insel)."); return; }
  const st = document.getElementById("impStatus");
  f.text().then(t => {
    const d = JSON.parse(t);
    let clusters = Array.isArray(d) ? d : (d && d.clusters);
    // PowerShells ConvertTo-Json macht aus einem einzelnen Cluster ein Objekt.
    if (clusters && !Array.isArray(clusters)) clusters = [clusters];
    if (!Array.isArray(clusters) || !clusters.length) { notify("Ungültige Datei: es fehlt die clusters-Liste."); return; }
    if (st) st.textContent = "importiere …";
    fetch("api/import", { method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ source: src, clusters: clusters }) })
      .then(r => r.json().then(j => ({ ok: r.ok, j: j })))
      .then(x => {
        if (!x.ok) { if (st) st.textContent = ""; return notify(x.j.error || "Import fehlgeschlagen."); }
        const skip = (x.j.skipped || []);
        if (st) { st.textContent = "✓ importiert (" + x.j.clusters + " Cluster) – Daten werden neu berechnet";
                  setTimeout(() => { st.textContent = ""; }, 5000); }
        if (skip.length) notify("Hinweis: " + skip.length + " Cluster ohne Hosts wurden übersprungen: "
                                + skip.join(", ") + ".\n(Ein Cluster ohne ESXi-Hosts hat keine planbare Kapazität.)");
        loadImports(); pollStatus();
      })
      .catch(() => { if (st) st.textContent = ""; notify("Import fehlgeschlagen (Netzwerk/Server)."); });
  }).catch(() => notify("Datei konnte nicht gelesen werden (kein gültiges JSON)."));
}
function importKapaCsv(ev) {
  const f = ev.target.files[0]; if (!f) return;
  ev.target.value = "";
  const st = document.getElementById("kapaCsvStatus");
  if (st) st.textContent = "importiere …";
  f.text().then(t =>
    fetch("api/import/reservations", { method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ csv: t }) })
      .then(r => r.json().then(j => ({ ok: r.ok, j: j })))
      .then(x => {
        if (st) st.textContent = "";
        if (!x.ok) return notify(x.j.error || "Import fehlgeschlagen.");
        let msg = x.j.imported + " Kapa-Anfragen übernommen (genehmigt)";
        if (x.j.skipped) msg += " · " + x.j.skipped + " übersprungen (Kapa-Nummer schon vorhanden)";
        if (x.j.expired) msg += " · " + x.j.expired + " sofort abgelaufen (Original-Datum älter als die Gültigkeit)";
        if ((x.j.unknown_clusters || []).length)
          msg += " · Unbekannte Cluster: " + x.j.unknown_clusters.join(", ");
        if ((x.j.errors || []).length)
          msg += "\n\nÜbersprungene Zeilen: \n" + x.j.errors.join("\n");
        notify(msg, "CSV-Import");
        apiRes("GET", "").then(setRes).catch(() => {});
      }))
    .catch(() => { if (st) st.textContent = "";
      notify("Datei konnte nicht gelesen werden."); });
}
function delImport(name) {
  askConfirm({ title: "Offline-Quelle entfernen", okLabel: "✕ Löschen", okClass: "danger",
    message: "Quelle „" + name + "“ samt ihrer Cluster aus der Übersicht entfernen?\n" +
             "Bestehende Kapazitätsanfragen auf diese Cluster bleiben erhalten." })
    .then(ok => { if (!ok) return;
      fetch("api/import/" + encodeURIComponent(name), { method: "DELETE" })
        .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
        .then(() => { loadImports(); pollStatus(); })
        .catch(e => notify("Löschen fehlgeschlagen" +
                           (e && e.message ? " (" + e.message + ")" : "") + ".")); });
}

// ---- Auto-Freigabe pflegen (Verwaltung -> Auto-Freigabe) ----
let AA_CFG = null;
function loadAutoApprove() {
  fetch("api/autoapprove").then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.autoapprove) { AA_CFG = d.autoapprove; if (VIEW === "adm") renderAutoApprove(); }
  }).catch(() => {});
}
function renderAutoApprove() {
  if (!AA_CFG) return;
  const set = (id, v) => { const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = v; };
  const en = document.getElementById("aaEnabled");
  if (en) en.checked = !!AA_CFG.enabled;
  set("aaCpu", AA_CFG.min_cpu_pct); set("aaRam", AA_CFG.min_ram_pct);
  set("aaLun", AA_CFG.min_lun_pct); set("aaWl", AA_CFG.max_workload_pct);
  const box = document.getElementById("aaTeams");
  if (box) box.innerHTML = (TEAMS.length ? TEAMS : []).map((t, i) =>
    `<label style="display:block;font-size:13px;margin:3px 0">
       <input type="checkbox" data-aateam="${esc(t)}"
         ${AA_CFG.teams && AA_CFG.teams[t] ? "checked" : ""}>
       Stufe ${i + 1}: ${esc(t)}</label>`).join("")
    || `<span style="color:var(--muted);font-size:12px">Keine Teams – einstufig (Haken entfällt, es gelten nur die Schwellen).</span>`;
}
function saveAutoApprove() {
  const num = id => parseInt((document.getElementById(id) || {}).value, 10) || 0;
  const teams = {};
  document.querySelectorAll("input[data-aateam]").forEach(cb => {
    if (cb.checked) teams[cb.getAttribute("data-aateam")] = true;
  });
  fetch("api/autoapprove", { method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ enabled: !!(document.getElementById("aaEnabled") || {}).checked,
        min_cpu_pct: num("aaCpu"), min_ram_pct: num("aaRam"),
        min_lun_pct: num("aaLun"), max_workload_pct: num("aaWl"), teams: teams }) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {
      if (d && d.autoapprove) { AA_CFG = d.autoapprove; renderAutoApprove(); }
      const st = document.getElementById("aaSaved");
      if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); }
    }).catch(() => notify("Speichern der Auto-Freigabe fehlgeschlagen."));
}

function previewAnnounce() {
  _annPreview = true;
  document.querySelector("#annTitle span").textContent =
    (document.getElementById("annCfgTitle") || {}).value || "Ankündigung";
  document.getElementById("annBody").textContent =
    (document.getElementById("annCfgText") || {}).value || "";
  document.getElementById("annBg").classList.add("open");
}

function previewMail() {
  const th = (document.getElementById("tplHtml") || {}).value || "";
  const ts = (document.getElementById("tplSubject") || {}).value || "";
  fetch("api/mail-preview", { method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ template_html: th, template_subject: ts }) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {
      const box = document.getElementById("mailPreview");
      const sub = document.getElementById("previewSubject");
      const fr = document.getElementById("previewFrame");
      if (sub) sub.textContent = d.subject || "";
      if (fr) fr.srcdoc = d.html || "";
      if (box) box.style.display = "";
    }).catch(() => notify("Vorschau fehlgeschlagen."));
}

// ---- Unter-Reiter der Verwaltung + read-only Konfiguration ----
let ADM_TAB = "users";
function setAdmTab(t) {
  ADM_TAB = t;
  const tabs = { users: "atabUsers", sel: "atabSel", mail: "atabMail",
                 hooks: "atabHooks", ann: "atabAnn", auto: "atabAuto", vis: "atabVis",
                 storcfg: "atabStorCfg", net: "atabNet", imp: "atabImp", tok: "atabTok", conf: "atabConf" };
  const grps = { users: "admGrpUsers", sel: "admGrpSel", mail: "admGrpMail",
                 hooks: "admGrpHooks", ann: "admGrpAnn", auto: "admGrpAuto", vis: "admGrpVis",
                 storcfg: "admGrpStorCfg", net: "admGrpNet", imp: "admGrpImp", tok: "admGrpTok", conf: "admGrpConf" };
  for (const k in tabs) {
    const tb = document.getElementById(tabs[k]); if (tb) tb.classList.toggle("active", k === t);
    const gr = document.getElementById(grps[k]); if (gr) gr.style.display = k === t ? "" : "none";
  }
}
let CONFIG = null;
function loadConfig() {
  fetch("api/config").then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.config) { CONFIG = d.config; if (VIEW === "adm") { renderConfig("configSheet"); renderConfig("configMail", "Mail / SMTP"); } }
  }).catch(() => {});
}
function renderConfig(containerId, onlyGroup) {
  const el = document.getElementById(containerId); if (!el) return;
  if (!CONFIG) { el.innerHTML = '<div style="color:var(--muted);font-size:12px">Lade Konfiguration …</div>'; return; }
  const groups = onlyGroup ? (CONFIG[onlyGroup] ? { [onlyGroup]: CONFIG[onlyGroup] } : {}) : CONFIG;
  const keys = Object.keys(groups);
  if (!keys.length) { el.innerHTML = '<div style="color:var(--muted);font-size:12px">–</div>'; return; }
  el.innerHTML = keys.map(g => {
    const rows = Object.keys(groups[g]).map(k =>
      `<tr><td style="color:var(--muted);width:300px">${esc(k)}</td><td>${esc(String(groups[g][k]))}</td></tr>`).join("");
    const head = onlyGroup ? "" : `<div class="sechead" style="margin-top:14px">${esc(g)}</div>`;
    return `${head}<div class="tablewrap"><table class="kt">${rows}</table></div>`;
  }).join("");
}

// ---- Cluster-Selektor konfigurieren ----
async function apiSelector(method, body) {
  const r = await fetch("api/selector", { method: method,
    headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadSelector() {
  apiSelector("GET").then(d => { if (d && d.selector) SELECTOR = d.selector;
                                 if (VIEW === "adm") render(); }).catch(() => {});
}
function saveSelector() {
  const list = [0, 1, 2].map(i => ({
      category: document.getElementById("sel" + i).value,
      label: (document.getElementById("sellbl" + i).value || "").trim()
    })).filter(x => x.category);
  apiSelector("PUT", list).then(d => {
    if (d && d.selector) SELECTOR = d.selector;
    const cats = SELECTOR.map(s => s.category);
    Object.keys(SEL_VALUES).forEach(k => { if (!cats.includes(k)) delete SEL_VALUES[k]; });
    const st = document.getElementById("selSaved");
    if (st) { st.textContent = "✓ gespeichert"; setTimeout(() => { if (st) st.textContent = ""; }, 2500); }
    render();
  }).catch(() => notify("Speichern des Selektors fehlgeschlagen."));
}
function renderSelector() {
  const all = [...new Set([...tagCategories(), ...SELECTOR.map(s => s.category)])].sort();
  const rows = [0, 1, 2].map(i => {
    const cur = SELECTOR[i] || {category: "", label: ""};
    const warn = cur.category && !tagCategories().includes(cur.category)
      ? ' <span class="st pend" title="Kategorie derzeit in keinem Cluster-Tag">⚠ derzeit ohne Werte</span>' : "";
    return `<tr><td class="num">${i + 1}</td>
      <td><select id="sel${i}" class="filterbox" style="max-width:280px">
        <option value="">– keine –</option>
        ${all.map(c => `<option value="${esc(c)}" ${cur.category === c ? "selected" : ""}>${esc(c)}</option>`).join("")}
      </select>${warn}</td>
      <td><input id="sellbl${i}" class="filterbox" style="width:100%"
           placeholder="Anzeigename (leer = Kategorie)" value="${esc(cur.label && cur.label !== cur.category ? cur.label : "")}"
           onkeydown="if(event.key==='Enter')saveSelector()"></td></tr>`;
  }).join("");
  document.getElementById("selbody").innerHTML = rows +
    `<tr><td></td><td colspan="2">
       <button class="btn approve" onclick="saveSelector()">✓ Selektor speichern</button>
       <span id="selSaved" style="color:var(--ok);font-size:12px;margin-left:8px"></span>
       ${all.length ? "" : '<span style="color:var(--muted);font-size:12px;margin-left:8px">Noch keine Tag-Kategorien – erst nach dem ersten Aria-Abruf verfügbar.</span>'}
     </td></tr>`;
}

// ---- Rollen-Bezeichnungen frei umbenennen ----
async function apiRoleNames(method, body) {
  const r = await fetch("api/rolenames", {
    method: method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function loadRoleNames() {
  apiRoleNames("GET").then(d => { if (d && d.rolenames) ROLE_NAMES = d.rolenames;
                                  if (VIEW === "adm") render(); }).catch(() => {});
}
function saveRoleNames() {
  const body = {};
  ROLE_ORDER.forEach(k => { body[k] = (document.getElementById("rn_" + k).value || "").trim(); });
  apiRoleNames("PUT", body).then(d => { if (d && d.rolenames) ROLE_NAMES = d.rolenames;
                                        render(); }).catch(() => notify("Speichern fehlgeschlagen."));
}
function renderRoleNames() {
  const rows = ROLE_ORDER.map(k =>
    `<tr><td style="color:var(--muted)">${esc(k)}</td>
     <td><input class="filterbox" style="width:100%" id="rn_${k}" value="${esc(ROLE_NAMES[k] || k)}"
          onkeydown="if(event.key==='Enter')saveRoleNames()"></td></tr>`).join("");
  document.getElementById("rnbody").innerHTML = rows +
    `<tr><td></td><td><button class="btn approve" onclick="saveRoleNames()">✓ Bezeichnungen speichern</button></td></tr>`;
}

// Rollen-Auswahl (Dropdown) und das rollenabhängige Feld (Team/Abteilung)
function roleSelect(id, sel, onchange) {
  return `<select id="${id}" class="filterbox" style="width:100%" onchange="${onchange}">
    ${ROLE_ORDER.map(x => `<option value="${x}" ${sel === x ? "selected" : ""}>${esc(ROLE_NAMES[x] || x)}</option>`).join("")}
  </select>`;
}
function roleField(id, role, val, u) {
  // Anforderer und Reviewer werden einem Team (aus teams.json) zugeordnet;
  // Admin/Auditor brauchen kein Team.
  if (role === "reviewer" || role === "anforderer")
    return `<select id="${id}" class="filterbox" style="width:100%">
      <option value="">${TEAMS.length ? "– Team wählen –" : "(erst Teams anlegen)"}</option>
      ${TEAMS.map(t => `<option value="${esc(t)}" ${val === t ? "selected" : ""}>${esc(t)}</option>`).join("")}
    </select>`;
  return `<input id="${id}" class="filterbox" style="width:100%" placeholder="– für diese Rolle nicht nötig" value="" disabled>`;
}
function syncRoleField(pfx, u) {
  const roleEl = document.getElementById(pfx === "adm" ? "admRole" : "editRole");
  const valEl = document.getElementById(pfx === "adm" ? "admDept" : "editDept");
  const val = valEl ? valEl.value : "";
  document.getElementById(pfx === "adm" ? "admFieldCell" : "editFieldCell").innerHTML =
    roleField(pfx === "adm" ? "admDept" : "editDept", roleEl.value, val, pfx === "edit" ? u : null);
}
// vROps-Quellen-Freigabe je Zuweisung (nur bei mehreren Quellen sinnvoll)
function srcCheckboxes(prefix, selected) {
  if (SOURCES.length < 2) return "";
  const sel = new Set(selected || []);
  return `<div id="${prefix}Box" style="display:flex;flex-wrap:wrap;gap:2px 10px;max-width:280px">`
    + SOURCES.map(n => `<label style="font-size:12px;white-space:nowrap;cursor:pointer"><input type="checkbox" class="srcCb" value="${esc(n)}"${sel.has(n) ? " checked" : ""}> ${esc(n)}</label>`).join("")
    + `<span style="color:var(--muted);font-size:11px;flex-basis:100%">nichts angehakt = alle Quellen</span></div>`;
}
function collectSources(prefix) {
  const box = document.getElementById(prefix + "Box");
  return box ? [...box.querySelectorAll(".srcCb:checked")].map(c => c.value) : [];
}
function srcDisplay(sel) {
  if (SOURCES.length < 2) return "";
  return (sel && sel.length) ? esc(sel.join(", "))
    : '<span style="color:var(--muted)">alle</span>';
}
function renderAdmTable() {
  const q = ((document.getElementById("admUserFilter") || {}).value || "").trim().toLowerCase();
  const users = Object.keys(ROLES).sort().filter(u =>
    !q || u.includes(q) || (ROLES[u].abteilung || "").toLowerCase().includes(q));
  const rows = users.map(u => {
    const r = ROLES[u];
    const isGroup = r && r.kind === "group";
    const typ = isGroup ? "AD-Gruppe" : "Benutzer";
    if (u === EDIT_USER) {
      const cur = ROLES[u] || {};
      return `<tr><td>${esc(typ)}</td><td>${esc(u)}</td>
        <td>${roleSelect("editRole", cur.role, "syncRoleField('edit','" + esc(u) + "')")}</td>
        <td id="editFieldCell">${roleField("editDept", cur.role, cur.abteilung || "", u)}</td>
        <td class="srcCol">${srcCheckboxes("editSrc", cur.sources || [])}</td>
        <td><button class="btn approve" onclick="saveEditRole('${esc(u)}')">✓ Speichern</button>
            <button class="btn" onclick="cancelEditRole()">Abbrechen</button></td></tr>`;
    }
    // Team-Zuweisung prüfen: nach Umbenennen/Löschen eines Teams (oder aus der
    // früheren Freitext-Abteilung) kann hier ein Team stehen, das es nicht gibt.
    const needsTeam = r.role === "anforderer" || r.role === "reviewer";
    const orphan = needsTeam && r.abteilung && TEAMS.length && !TEAMS.includes(r.abteilung);
    const warn = orphan
      ? ' <span class="st rej" title="Dieses Team gibt es nicht (mehr) – bitte neu zuweisen">⚠ unbekannt</span>'
      : (needsTeam && !r.abteilung ? ' <span class="st pend" title="Ohne Team: sieht nur eigene Anfragen bzw. kann nichts freigeben">⚠ kein Team</span>' : "");
    const mailCell = isGroup ? esc(u)
      : `<span title="${r.mail ? 'AD-Mail: ' + esc(r.mail) : 'keine AD-Mail aufgelöst (nur mit --ad-mail-attribute)'}">${esc(u)}${r.mail ? ' <span style="color:var(--muted)" title="AD-Mail: ' + esc(r.mail) + '">✉</span>' : ''}</span>`;
    return `<tr><td>${esc(typ)}</td><td>${mailCell}</td><td>${esc(ROLE_NAMES[r.role] || r.role)}</td>
     <td>${esc(r.abteilung || "–")}${warn}</td>
     <td class="srcCol">${srcDisplay(r.sources || [])}</td>
     <td>${isGroup ? `<button class="edit" title="Mitglieder der AD-Gruppe im AD nachschlagen" onclick="checkGroup('${esc(u)}')">👥 Mitglieder</button> ` : ""}<button class="edit" title="Rolle/Team bearbeiten" onclick="editRole('${esc(u)}')">✎ Bearbeiten</button>
         <button class="del" title="Zuweisung entfernen" onclick="delRole('${esc(u)}')">✕ Löschen</button></td></tr>`;
  }).join("");
  const firstRole = ROLE_ORDER[0];
  document.getElementById("mtbody").innerHTML =
    `<tr><td><select id="admKind" class="filterbox" style="width:100%" onchange="admKindSync()">
         <option value="user">Benutzer</option><option value="group">AD-Gruppe</option></select></td>
     <td><input class="filterbox" style="width:100%" id="admUser"
         placeholder="benutzer@firma.local oder vorname.nachname"></td>
     <td>${roleSelect("admRole", firstRole, "syncRoleField('adm')")}</td>
     <td id="admFieldCell">${roleField("admDept", firstRole, "", null)}</td>
     <td class="srcCol">${srcCheckboxes("admSrc", [])}</td>
     <td><button class="btn approve" onclick="addRole()">+ Zuweisen</button></td></tr>` +
    (rows || `<tr><td colspan="6" style="color:var(--muted)">Noch keine Rollen zugewiesen.</td></tr>`);
  reSort("mtable"); renderColMenu("mtable"); applyCols("mtable");
}
function admKindSync() {
  const g = document.getElementById("admKind").value === "group";
  document.getElementById("admUser").placeholder = g
    ? "AD-Gruppenname (CN), z. B. Kapa-Admins"
    : "benutzer@firma.local oder vorname.nachname";
}

// Status als reiner Text (für Filter/Export – stBadge liefert HTML)
function statusText(r) {
  if (r.rejected) return "abgelehnt";
  if (r.cancelled) return "storniert";
  if (r.approved) return "genehmigt";
  return (TEAMS.length && stageOf(r) > 0) ? "in Prüfung" : "beantragt";
}
function filterRes(list, q) {
  q = (q || "").trim().toLowerCase();
  const hit = r => [r.name, r.cluster, r.change, r.von, r.abteilung, r.id,
                    statusText(r)].some(v => (v || "").toLowerCase().includes(q));
  return list.filter(r => !q || hit(r))
    .slice().sort((a, b) => (a.cluster || "").localeCompare(b.cluster || "") ||
                            (a.created || "").localeCompare(b.created || ""));
}

function sumStorage(rv) { return Math.round(rv.reduce((s,r)=>s+(r.storage_gb||0),0)*10)/10; }

// Cluster-Name als Tabellenzelle – klickbar (öffnet dieselbe Detailkarte wie
// auf der Kapazitätsseite), sofern der Cluster in den aktuellen Daten existiert.
function clusterTd(name) {
  const i = CLUSTERS.findIndex(c => c.name === name);
  return i >= 0
    ? `<td class="cl" title="Cluster-Details anzeigen" onclick="toggleCard(${i}, this)">${esc(name)}${srcBadge(CLUSTERS[i].source)}</td>`
    : `<td>${esc(name || "–")}</td>`;
}

function isArchived(r) { return !!(r.rejected || r.cancelled); }

function renderResTable() {
  const q = (document.getElementById("resSearch") || {}).value || "";
  // „(anderes Team)" nicht auflisten; abgelehnte/stornierte liegen im Archiv
  const own = RES.filter(r => !r.foreign && !isArchived(r));
  const list = filterRes(own, q);
  const appr = list.filter(r => r.approved && !r.cancelled);
  const cnt = document.getElementById("resCount");
  if (cnt) cnt.textContent = q.trim() ? list.length + " von " + own.length + " Anfragen" : own.length + " Anfragen";
  const showDec = VIS.decided_by;
  const nCols = showDec ? 15 : 14;
  const rows = paginate("rtable", list).map(r =>
    `<tr><td class="rid" title="Eindeutige ID der Anfrage">${esc(r.id || "–")}</td>
     <td>${esc(r.name)}</td>${clusterTd(r.cluster)}<td>${esc(r.change || "–")}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     <td>${esc(r.von || "–")}</td><td>${esc(r.abteilung || "–")}</td>
     <td>${fmtDate(r.created)}</td><td>${fmtDate(validUntil(r))}</td><td>${stBadge(r)}</td>
     ${showDec ? `<td>${esc(r.approved_by || r.rejected_by || r.cancelled_by || "–")}</td>` : ""}
     <td>${esc(r.comment || "–")}</td>
     <td>${canCancel(r) ? `<button class="del" title="Anfrage stornieren" onclick="cancelRes('${esc(r.id)}')">⦸ Storno</button>` : ""}</td></tr>`).join("");
  document.getElementById("rtbody").innerHTML =
    `<tr class="trtotal"><td></td><td>Summe genehmigt (${appr.length} von ${list.length})</td><td></td><td></td>
     <td class="num">${fmt(sumCpu(appr))}</td><td class="num">${fmt(sumRam(appr))}</td><td class="num">${fmt(sumStorage(appr))}</td>
     <td colspan="${nCols - 7}"></td></tr>` +
    (rows || `<tr><td colspan="${nCols}" style="color:var(--muted)">Keine aktiven Reservierungen.</td></tr>`);
  reSort("rtable"); renderColMenu("rtable"); applyCols("rtable");
}

function renderArchiveTable() {
  const q = (document.getElementById("archSearch") || {}).value || "";
  const arch = RES.filter(r => !r.foreign && isArchived(r));
  const list = filterRes(arch, q);
  const cnt = document.getElementById("archCount");
  if (cnt) cnt.textContent = q.trim() ? list.length + " von " + arch.length + " Einträgen" : arch.length + " Einträge";
  const rows = paginate("artable", list).map(r => {
    const done = r.cancelled ? r.cancelled_on : r.rejected_on;
    const by = r.cancelled ? r.cancelled_by : r.rejected_by;
    return `<tr><td class="rid">${esc(r.id || "–")}</td>
     <td>${esc(r.name)}</td>${clusterTd(r.cluster)}<td>${esc(r.change || "–")}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     <td>${esc(r.von || "–")}</td><td>${esc(r.abteilung || "–")}</td>
     <td>${fmtDate(r.created)}</td><td>${fmtDate(done)}</td><td>${stBadge(r)}</td>
     <td>${esc(by || "–")}</td><td>${esc(r.comment || "–")}</td></tr>`;
  }).join("");
  document.getElementById("arbody").innerHTML =
    rows || `<tr><td colspan="14" style="color:var(--muted)">Archiv ist leer.</td></tr>`;
  reSort("artable"); renderColMenu("artable"); applyCols("artable");
}

function renderAppTable() {
  const list = filterRes(RES.filter(isPend), (document.getElementById("filter") || {}).value || "");
  const action = r => {
    if (canDecide(r)) {
      const team = currentTeam(r);
      const lbl = team ? "✓ Freigeben (" + esc(team) + ")" : "✓ Genehmigen";
      return `<button class="btn approve" onclick="approveRes('${esc(r.id)}')">${lbl}</button>
       <button class="btn" style="color:var(--crit)" title="Ablehnen"
               onclick="rejectRes('${esc(r.id)}')">✕ Ablehnen</button>`;
    }
    const team = currentTeam(r);
    return '<span class="st pend">wartet auf ' + (team ? esc(team) : "Genehmigung") + '</span>';
  };
  const freeCells = r => {
    const c = CLUSTERS.find(x => x.name === r.cluster);
    if (!c) return '<td class="num" colspan="2" style="color:var(--muted)">Cluster unbekannt</td>';
    const f = freeAfter(c);   // frei nach genehmigten Reservierungen
    const cpuOk = (r.vcpu || 0) <= f.cpu, ramOk = (r.ram_gb || 0) <= f.ram;
    const cell = (v, ok, unit) =>
      `<td class="num free" style="color:${ok ? "var(--ok)" : "var(--crit)"}"
           title="${ok ? "Antrag passt" : "Antrag überschreitet die freie Kapazität"}">${fmt(v)}${ok ? "" : " ⚠"}</td>`;
    return cell(f.cpu, cpuOk) + cell(f.ram, ramOk);
  };
  const rows = paginate("atable", list).map(r =>
    `<tr><td class="rid" title="Eindeutige ID der Anfrage">${esc(r.id || "–")}</td>
     <td>${esc(r.name)}</td>${clusterTd(r.cluster)}<td>${esc(r.change || "–")}</td>
     <td class="num">${fmt(r.vcpu || 0)}</td><td class="num">${fmt(r.ram_gb || 0)}</td><td class="num">${fmt(r.storage_gb || 0)}</td>
     ${freeCells(r)}
     <td>${esc(r.von || "–")}</td><td>${esc(r.abteilung || "–")}</td>
     <td>${fmtDate(r.created)}</td><td>${stBadge(r)}</td>
     <td>${action(r)}</td></tr>`).join("");
  document.getElementById("atbody").innerHTML =
    rows || `<tr><td colspan="14" style="color:var(--muted)">Keine offenen Anträge – alles genehmigt.</td></tr>`;
  reSort("atable"); renderColMenu("atable"); applyCols("atable");
}

// ---- VLAN-/Portgruppen-Suche (cluster-übergreifend) ----
// Hängt ein Netz an mehreren Clustern, liefert der Server die Portgruppe je
// Cluster einmal – die Suche zeigt dann pro Cluster eine Zeile, damit
// „wo hängt das Netz" vollständig ist.
let VLAN_INDEX = null;
function vlanIndex() {
  if (VLAN_INDEX) return VLAN_INDEX;
  const out = [];
  CLUSTERS.forEach((c, ci) => (c.portgroups || []).forEach(p =>
    out.push({ pg: p.name || "", vlan: p.vlan || "", cluster: c.name, cidx: ci })));
  VLAN_INDEX = out;
  return out;
}
// ---- Storage-Übersicht + Erweiterungs-Anfragen ----
let STOR = { enabled: false, requests: [] };
const CAN_STORAGE = IS_ADMIN || ROLE === "reviewer";   // wer anfragen darf
function loadStorage() {
  fetch("api/storage-requests").then(r => r.ok ? r.json() : null).then(d => {
    if (d) { STOR = d; if (VIEW === "stor") renderStorage(); }
  }).catch(() => {});
}
function allLuns() {   // alle Datastores mit Cluster-Bezug, flach
  const out = [];
  (CLUSTERS || []).forEach(c => (c.datastores || []).forEach(d =>
    out.push({ cluster: c.name, name: d.name, type: d.type || "–",
               naa: d.naa || "", cap: d.cap_gb || 0, used: d.used_gb || 0 })));
  return out;
}
// vSAN lässt sich nicht per LUN-Vergrößerung erweitern (Kapazität über Hosts);
// erweiterbar sind FC-/iSCSI-/NFS-LUNs.
function isVsanLun(l) { return /vsan/i.test((l.type || "") + " " + (l.name || "")); }
function renderStorage() {
  const q = (document.getElementById("storFilter").value || "").trim().toLowerCase();
  // Offene/erledigte Anfragen zuerst als eigener Block
  const reqs = (STOR.requests || []);
  const openReqs = reqs.filter(r => r.status === "offen");
  const box = document.getElementById("storReqBox");
  box.innerHTML = reqs.length ? `
    <div class="resbox" style="margin-bottom:14px">
      <h3>Storage-Erweiterungen (${openReqs.length} offen / ${reqs.length} gesamt)</h3>
      <table><tr><th>Cluster</th><th>Anfrage</th><th>NAA</th><th>angefragt</th><th>Status</th>${IS_ADMIN ? "<th></th>" : ""}</tr>
      ${reqs.slice().sort((a,b)=> (a.status>b.status?1:-1)).map(r => {
        const what = r.kind === "new" ? `neue LUN ${fmt(r.size_gb)} GB`
          : `${esc(r.lun_name)}: ${fmt(r.current_gb)} → <b>${fmt(r.target_gb)} GB</b>`;
        const done = r.status === "erledigt";
        return `<tr style="${done?'opacity:.55':'background:rgba(245,158,11,.10)'}">
          <td>${esc(r.cluster)}</td><td>${what}${r.comment?` <span style="color:var(--muted)">· ${esc(r.comment)}</span>`:""}</td>
          <td style="font-family:monospace;font-size:12px">${esc(r.naa||"–")}</td>
          <td>${esc(r.requested_by||"–")} · ${fmtDate(r.requested_on)}</td>
          <td>${done?`erledigt${r.done_by?" ("+esc(r.done_by)+")":""}`:'<b style="color:var(--warn)">offen</b>'}</td>
          ${IS_ADMIN?`<td style="white-space:nowrap"><button class="btn" onclick="toggleStorDone('${esc(r.id)}',${done?'false':'true'})">${done?"wieder offen":"✓ erledigt"}</button> <button class="del" title="Anfrage löschen (z. B. versehentlich angelegt)" onclick="delStorReq('${esc(r.id)}')">✕ Löschen</button></td>`:""}
        </tr>`; }).join("")}
      </table>
    </div>` : "";
  // LUN-Tabelle
  const pend = {};   // cluster|lun -> offene Ziel-GB
  openReqs.forEach(r => { if (r.kind === "expand") pend[r.cluster + "|" + r.lun_name] = r.target_gb; });
  const luns = allLuns().filter(l => !q ||
    (l.cluster + " " + l.name + " " + l.naa + " " + l.type).toLowerCase().includes(q));
  document.getElementById("storbody").innerHTML = paginate("stortable", luns).map(l => {
    const tgt = pend[l.cluster + "|" + l.name];
    const free = Math.round((l.cap - l.used) * 10) / 10;
    return `<tr${tgt?' style="background:rgba(245,158,11,.10)"':''}>
      ${clusterTd(l.cluster)}<td>${esc(l.name)}</td><td>${esc(l.type)}</td>
      <td style="font-family:monospace;font-size:12px">${esc(l.naa||"–")}</td>
      <td class="num">${fmt(Math.round(l.cap))}${tgt?` <span style="color:var(--warn)">→ ${fmt(tgt)}</span>`:""}</td>
      <td class="num">${fmt(Math.round(l.used))}</td>
      <td class="num">${fmt(free)}</td>
      <td>${tgt ? "angefragt" : (STOR.enabled && CAN_STORAGE && !isVsanLun(l) ? `<button class="btn" onclick="openStorReq('${esc(l.cluster)}','${esc(l.name)}','${esc(l.naa)}',${Math.round(l.cap)})">Erweitern</button>` : (isVsanLun(l) ? `<span style="color:var(--muted);font-size:12px">vSAN</span>` : ""))}</td>
    </tr>`; }).join("") ||
    `<tr><td colspan="8" style="color:var(--muted)">Keine Storage-Daten.</td></tr>`;
  const cnt = document.getElementById("storCount");
  if (cnt) cnt.textContent = luns.length + " LUNs";
  document.getElementById("storCsvBtn").style.display = IS_ADMIN ? "" : "none";
  reSort("stortable"); renderColMenu("stortable"); applyCols("stortable");
}
function toggleStorDone(id, done) {
  fetch("api/storage-request/" + encodeURIComponent(id), { method: "PUT",
      headers: {"Content-Type":"application/json"}, body: JSON.stringify({ done: done }) })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => { if (d.requests) { STOR.requests = d.requests; renderStorage(); } })
    .catch(() => notify("Ändern fehlgeschlagen."));
}
function delStorReq(id) {
  askConfirm({ title: "Storage-Anfrage löschen", okLabel: "✕ Löschen", okClass: "danger",
    message: "Diese Storage-Anfrage wirklich löschen? Das lässt sich nicht rückgängig machen." })
    .then(ok => { if (!ok) return;
      fetch("api/storage-request/" + encodeURIComponent(id), { method: "DELETE" })
        .then(r => r.ok ? r.json() : Promise.reject())
        .then(d => { if (d.requests) { STOR.requests = d.requests; renderStorage(); } })
        .catch(() => notify("Löschen fehlgeschlagen.")); });
}
// Erweiterungs-Dialog — genutzt aus der Storage-Seite UND aus dem Freigabe-Popup
function openStorReq(cluster, lun, naa, curGb, resId, resName) {
  // vSAN nicht zum Vergrößern anbieten – nur FC-/iSCSI-/NFS-LUNs
  const luns = allLuns().filter(l => l.cluster === cluster && !isVsanLun(l));
  const canExpand = luns.length > 0;
  const opts = luns.map(l => `<option value="${esc(l.name)}" data-naa="${esc(l.naa)}" data-cap="${Math.round(l.cap)}"${l.name===lun?" selected":""}>${esc(l.name)} (${fmt(Math.round(l.cap))} GB${l.naa?", "+esc(l.naa):""})</option>`).join("");
  const selLun = luns.find(l => l.name === lun) || luns[0];
  const initCap = selLun ? Math.round(selLun.cap) : 0;
  askConfirm({ title: "Storage-Erweiterung – " + cluster, okLabel: "✓ Anfragen",
    html: `
    <div style="font-size:13px;line-height:1.7">
      <label style="display:flex;align-items:center;gap:7px;color:var(--text);font-size:13px;margin:0 0 8px;cursor:pointer"><input type="radio" name="storKind" value="expand" style="width:auto;margin:0;flex:none" ${canExpand?"checked":"disabled"} onchange="storKindUI()"><span>Bestehende LUN vergrößern${canExpand?"":" <span style='color:var(--muted)'>(keine erweiterbare LUN)</span>"}</span></label>
      <label style="display:flex;align-items:center;gap:7px;color:var(--text);font-size:13px;margin:0 0 4px;cursor:pointer"><input type="radio" name="storKind" value="new" style="width:auto;margin:0;flex:none" ${canExpand?"":"checked"} onchange="storKindUI()"><span>Neue LUN anlegen</span></label>
      <div id="storExpand" style="margin-top:8px;${canExpand?"":"display:none"}">
        <div>LUN: <select id="storLun" class="filterbox" style="max-width:340px" onchange="storLunPick()">${opts}</select></div>
        <div style="margin-top:6px">Wunschgröße (GB): <input id="storTarget" type="number" min="1" class="filterbox" style="width:120px" placeholder="aktuell ${fmt(initCap)} GB"></div>
      </div>
      <div id="storNew" style="margin-top:8px;${canExpand?"display:none":""}">
        Neue LUN, Größe (TB): <input id="storTB" type="number" min="0.1" step="0.1" class="filterbox" style="width:120px" placeholder="z. B. 2">
      </div>
      <div style="margin-top:8px">Kommentar: <input id="storComment" class="filterbox" style="width:100%" placeholder="optional, z. B. Change/Grund"></div>
    </div>` })
    .then(ok => {
      if (!ok) return;
      const kind = document.querySelector('input[name="storKind"]:checked').value;
      const body = { cluster: cluster, kind: kind,
        comment: (document.getElementById("storComment")||{}).value || "",
        res_id: resId || "", res_name: resName || "" };
      let want = 0;
      if (kind === "expand") {
        const sel = document.getElementById("storLun");
        const o = sel.options[sel.selectedIndex];
        body.lun_name = sel.value; body.naa = o.getAttribute("data-naa") || "";
        body.current_gb = parseInt(o.getAttribute("data-cap")) || 0;
        body.target_gb = parseInt(document.getElementById("storTarget").value) || 0;
        want = body.target_gb;
        if (body.target_gb <= body.current_gb) return notify("Wunschgröße muss größer als die aktuelle Größe sein.");
      } else {
        body.size_gb = Math.round((parseFloat(document.getElementById("storTB").value) || 0) * 1024);
        want = body.size_gb;
        if (body.size_gb <= 0) return notify("Bitte eine Größe in TB angeben.");
      }
      if (STOR.max_lun_gb && want > STOR.max_lun_gb)
        return notify("Die Größe überschreitet das Maximum von " + fmt(Math.round(STOR.max_lun_gb / 1024 * 10) / 10) + " TB.");
      fetch("api/storage-request", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) })
        .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)))
        .then(d => { loadStorage(); notify("Storage-Erweiterung angefragt."); })
        .catch(e => notify((e && e.error) || "Anfrage fehlgeschlagen."));
    });
}
function storKindUI() {
  const isNew = document.querySelector('input[name="storKind"]:checked').value === "new";
  document.getElementById("storExpand").style.display = isNew ? "none" : "";
  document.getElementById("storNew").style.display = isNew ? "" : "none";
}
function storLunPick() {   // aktuelle Größe der gewählten LUN als Platzhalter
  const sel = document.getElementById("storLun");
  const t = document.getElementById("storTarget");
  if (!sel || !t || !sel.options.length) return;
  t.placeholder = "aktuell " + fmt(sel.options[sel.selectedIndex].getAttribute("data-cap")) + " GB";
}

function renderVlan() {
  const q = (document.getElementById("vlanQ").value || "").trim().toLowerCase();
  const all = vlanIndex();
  const hits = q ? all.filter(r =>
    r.pg.toLowerCase().includes(q) || String(r.vlan).toLowerCase().includes(q)) : all;
  document.getElementById("vtbody").innerHTML = paginate("vtable", hits).map(r =>
    `<tr><td>${esc(r.pg)}</td><td class="num">${esc(r.vlan || "–")}</td>
     <td class="cl" title="Cluster-Details anzeigen" onclick="toggleCard(${r.cidx}, this)">${esc(r.cluster)}${srcBadge(clusterSource(r.cluster))}</td></tr>`).join("")
    || `<tr><td colspan="3" style="color:var(--muted)">${all.length
         ? "Keine Portgruppe passt zur Suche." : "Keine Portgruppen-Daten aus Aria."}</td></tr>`;
  document.getElementById("vlanCount").textContent = all.length
    ? (q ? hits.length + " von " + all.length + " Portgruppen"
         : all.length + " Portgruppen gesamt") : "";
  reSort("vtable"); renderColMenu("vtable"); applyCols("vtable");
}

function render() {
  const pend = RES.filter(isPend).length;
  document.getElementById("tabApp").textContent = "Genehmigungen" + (pend ? " (" + pend + ")" : "");
  if (VIEW === "vlan") { renderVlan(); return; }
  if (VIEW === "stor") { renderStorage(); return; }
  if (VIEW === "res") { renderResTable(); return; }
  if (VIEW === "app") { renderAppTable(); return; }
  if (VIEW === "arch") { renderArchiveTable(); return; }
  if (VIEW === "adm") { renderAdmTable(); renderRoleNames(); renderTeams(); renderNotify(); renderMailTemplate(); renderAnnounce(); renderAutoApprove(); renderVisibility(); renderSelector(); renderTokenTable(); renderConfig("configSheet"); renderConfig("configMail", "Mail / SMTP"); setAdmTab(ADM_TAB); return; }
  if (VIEW === "log") { renderLogTable(); return; }
  renderClusterSelector();
  const idxs = filteredIdx();
  const vis = idxs.map(i => CLUSTERS[i]);
  TOTAL = {
    name: idxs.length === CLUSTERS.length ? "Gesamt (alle Cluster)" : "Gesamt (Filter)",
    hostCount: vis.reduce((s,c)=>s+c.hostCount,0),
    cores: vis.reduce((s,c)=>s+c.cores,0),
    vcpuCap: vis.reduce((s,c)=>s+c.vcpuCap,0),
    vcpuUsed: vis.reduce((s,c)=>s+c.vcpuUsed,0),
    ramCap: Math.round(vis.reduce((s,c)=>s+c.ramCap,0)*10)/10,
    ramUsed: Math.round(vis.reduce((s,c)=>s+c.ramUsed,0)*10)/10,
    storageCap: Math.round(vis.reduce((s,c)=>s+(c.storageCap||0),0)*10)/10,
    storageUsed: Math.round(vis.reduce((s,c)=>s+(c.storageUsed||0),0)*10)/10,
    vmCount: vis.reduce((s,c)=>s+c.vmCount,0),
    vmOff: vis.reduce((s,c)=>s+c.vmOff,0),
    spareCores: vis.reduce((s,c)=>s+(c.spareCores||0),0),
    spareRamGb: Math.round(vis.reduce((s,c)=>s+(c.spareRamGb||0),0)*10)/10,
    datastores: vis.reduce((s,c)=>s.concat(c.datastores||[]),[]),
    tanzuVcpu: vis.reduce((s,c)=>s+(c.tanzuVcpu||0),0),
    tanzuRamGb: Math.round(vis.reduce((s,c)=>s+(c.tanzuRamGb||0),0)*10)/10,
    namespaces: vis.reduce((s,c)=>s.concat(c.namespaces||[]),[]),
    _names: new Set(vis.map(c => c.name)),
  };
  TOTAL.vcpuFree = TOTAL.vcpuCap - TOTAL.vcpuUsed;
  TOTAL.ramFree = Math.round((TOTAL.ramCap - TOTAL.ramUsed)*10)/10;
  TOTAL.storageFree = Math.round((TOTAL.storageCap - TOTAL.storageUsed)*10)/10;
  document.getElementById("ktbody").innerHTML =
    (idxs.length ? paginate("ktable", idxs).map(i => row(CLUSTERS[i], i, false)).join("")
                 : '<tr><td colspan="10" style="color:var(--muted)">Kein Cluster entspricht dem Filter.</td></tr>');
  reSort("ktable"); renderColMenu("ktable"); applyCols("ktable");
  if (hoverIdx !== null && hc.style.display === "block")
    hc.innerHTML = '<button class="hc-close" title="Schließen" onclick="hideCard()">✕</button>' +
                   card(hoverIdx === -1 ? TOTAL : CLUSTERS[hoverIdx], hoverIdx, hoverIdx === -1);
}

// ---- Detail-Popover (Klick auf Clusternamen) ----
let hoverIdx = null;
const hc = document.getElementById("hovercard");

function showCard(idx, rowEl) {
  if (idx !== hoverIdx) CARD_NET_Q = "";   // beim Öffnen eines anderen Clusters Suche leeren
  hoverIdx = idx;
  hc.innerHTML = '<button class="hc-close" title="Schließen" onclick="hideCard()">✕</button>' +
                 card(idx === -1 ? TOTAL : CLUSTERS[idx], idx, idx === -1);
  hc.style.display = "block";
  // Ohne rowEl (Deep-Link) oben links positionieren
  const r = rowEl ? rowEl.getBoundingClientRect() : { bottom: 90, left: 40 };
  let top = r.bottom + 6;
  if (top + hc.offsetHeight > innerHeight - 10)
    top = Math.max(10, innerHeight - hc.offsetHeight - 10);
  hc.style.top = top + "px";
  hc.style.left = Math.min(Math.max(10, r.left + 60), Math.max(10, innerWidth - hc.offsetWidth - 10)) + "px";
  // Deep-Link: geöffneter Cluster steht teilbar in der URL (#cluster=Name)
  if (idx >= 0 && CLUSTERS[idx])
    history.replaceState(null, "", "#cluster=" + encodeURIComponent(CLUSTERS[idx].name));
}
function toggleCard(idx, cell) {
  if (hoverIdx === idx && hc.style.display === "block") hideCard();
  else showCard(idx, cell.parentElement);
}
function openStorage(idx, cell) {
  CARD_TAB = "storage";           // Klick auf den Storage-Wert -> Storage-Reiter
  showCard(idx, cell.parentElement);
}
function hideCard() {
  const wasOpen = hc.style.display === "block";
  hc.style.display = "none"; hoverIdx = null;
  // Hash nur löschen, wenn die Karte wirklich offen war – sonst würde der
  // Deep-Link-Hash beim initialen setView() verloren gehen.
  if (wasOpen && location.hash.startsWith("#cluster="))
    history.replaceState(null, "", location.pathname);
}
// Klick außerhalb schließt die Detailkarte. WICHTIG: in der Capture-Phase (true)
// prüfen – Elemente in der Karte (Reiter, Sortier-Links) ersetzen beim Klick den
// Karteninhalt. In der Bubble-Phase wäre e.target dann schon aus dem DOM entfernt
// und hc.contains(e.target) fälschlich false -> die Karte hätte sich geschlossen.
document.addEventListener("click", e => {
  if (hc.style.display === "block" && !hc.contains(e.target) && !e.target.closest(".cl"))
    hideCard();
  // Offene ▾-Dropdowns (Refresh-Menü, Spaltenmenüs) bei Klick außerhalb schließen —
  // native <details> bleiben sonst offen, bis man erneut auf den Knopf klickt.
  document.querySelectorAll("details.colmenu[open]").forEach(d => {
    if (!d.contains(e.target)) d.open = false;
  });
}, true);

// ---- Detailkarte per Titel (⠿) frei verschieben ----
(function () {
  let drag = false, sx = 0, sy = 0, ox = 0, oy = 0;
  hc.addEventListener("mousedown", e => {
    const h = e.target.closest("h2");
    if (!h || !hc.contains(h) || e.target.closest("button,a,input,select,textarea")) return;
    const r = hc.getBoundingClientRect();
    drag = true; sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
    hc.style.right = "auto"; hc.style.bottom = "auto";
    hc.style.left = ox + "px"; hc.style.top = oy + "px";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", e => {
    if (!drag) return;
    const nx = Math.max(4, Math.min(ox + e.clientX - sx, innerWidth - 60));
    const ny = Math.max(4, Math.min(oy + e.clientY - sy, innerHeight - 40));
    hc.style.left = nx + "px"; hc.style.top = ny + "px";
  });
  document.addEventListener("mouseup", () => { drag = false; document.body.style.userSelect = ""; });
})();

// ---- Rollenabhängige Sichtbarkeit ----
if (ME) {
  document.getElementById("userbox").textContent =
    ME.user + (ME.abteilung ? " · " + ME.abteilung : "") + " · " + (ROLE_NAMES[ROLE] || ROLE);
  document.getElementById("logoutBtn").style.display = "";
}
if (!CAN_REQUEST) document.getElementById("newReqBtn").style.display = "none";
if (!IS_ADMIN) document.getElementById("importBtn").style.display = "none";
if (!IS_ADMIN) document.getElementById("refreshBtn").style.display = "none";
if (IS_ADMIN && SERVE) document.getElementById("refreshMenu").style.display = "";
if (!IS_ADMIN || !SERVE) document.getElementById("tabAdm").style.display = "none";
if (!IS_ADMIN || !SERVE) document.getElementById("tabLog").style.display = "none";
if (HAS_BACKUP && SERVE) document.getElementById("backupSection").style.display = "";
if (!VIS.decided_by) {
  const th = document.getElementById("thDec");
  if (th) th.remove();   // Rolle sieht nicht, wer entschieden hat (Matrix)
}
if (!VIS.network) document.getElementById("tabVlan").style.display = "none";
if (VIS.statistik === false || !SERVE) document.getElementById("tabStat").style.display = "none";
if (!VIS.storage) document.getElementById("tabStor").style.display = "none";

// ---- Sortierbare Tabellen (Klick auf die Spaltenüberschrift) ----
const SORT_CFG = { ktable:{pin:0}, rtable:{pin:1}, atable:{pin:0}, artable:{pin:0},
                   ltable:{pin:0}, mtable:{pin:1}, ttable:{pin:1}, vtable:{pin:0},
                   stortable:{pin:0} };
const sortState = {};
function cellVal(td) {
  let t = (td ? td.textContent : "").trim();
  if (!t || t === "–" || t === "—") return { n: null, s: "" };
  const dm = t.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);       // Datum dd.mm.yyyy
  if (dm) return { n: Number(dm[3] + dm[2] + dm[1]), s: t };
  const cl = t.replace(/[⚠+]/g, "").trim();               // deutsche Zahl
  if (/^-?\d{1,3}(\.\d{3})*(,\d+)?$/.test(cl) || /^-?\d+(,\d+)?$/.test(cl)) {
    const v = parseFloat(cl.replace(/\./g, "").replace(",", "."));
    if (!isNaN(v)) return { n: v, s: t };
  }
  return { n: null, s: t.toLowerCase() };
}
function sortCmp(a, b, dir) {
  let r;
  if (a.n !== null && b.n !== null) r = a.n - b.n;
  else if (a.n !== null) r = -1;          // Zahlen vor Text
  else if (b.n !== null) r = 1;
  else r = a.s < b.s ? -1 : a.s > b.s ? 1 : 0;
  return dir === "desc" ? -r : r;
}
function applySort(id) {
  const st = sortState[id]; if (!st) return;
  const table = document.getElementById(id); if (!table) return;
  const tb = table.tBodies[0]; if (!tb) return;
  const pin = (SORT_CFG[id] || {}).pin || 0;
  const rows = Array.from(tb.rows);
  const pinned = rows.slice(0, pin);
  const rest = rows.slice(pin);
  const isPlace = r => r.cells.length === 1 && r.cells[0].hasAttribute("colspan");
  const data = rest.filter(r => !isPlace(r)), place = rest.filter(isPlace);
  if (data.length > 1)
    data.sort((x, y) => sortCmp(cellVal(x.cells[st.col]), cellVal(y.cells[st.col]), st.dir));
  [...pinned, ...data, ...place].forEach(r => tb.appendChild(r));
}
function markArrows(id) {
  const table = document.getElementById(id); if (!table || !table.tHead) return;
  const st = sortState[id];
  Array.from(table.tHead.rows[0].cells).forEach(th => {
    if (!th.classList.contains("sortable")) return;
    const base = th.getAttribute("data-base") || "";
    const active = st && st.col === th.cellIndex;
    th.innerHTML = base + (active ? '<span class="sarr">' + (st.dir === "asc" ? "▲" : "▼") + "</span>" : "");
  });
}
function sortBy(id, col) {
  const st = sortState[id];
  sortState[id] = { col: col, dir: (st && st.col === col && st.dir === "asc") ? "desc" : "asc" };
  markArrows(id); applySort(id);
}
function reSort(id) { if (sortState[id]) applySort(id); }
function initSortable() {
  Object.keys(SORT_CFG).forEach(id => {
    const table = document.getElementById(id); if (!table || !table.tHead) return;
    Array.from(table.tHead.rows[0].cells).forEach(th => {
      if (th.classList.contains("barcol") || th.classList.contains("nosort")) return;
      th.classList.add("sortable");
      th.setAttribute("data-base", th.textContent);
      th.addEventListener("click", () => sortBy(id, th.cellIndex));
    });
  });
}
initSortable();

setView(VIEW);
if (!SERVE) document.getElementById("refreshBtn").style.display = "none";
if (!SERVE) document.getElementById("csvBtn").style.display = "none";  // API nur im Serve-Modus

// ---- Live-Abruf & Auto-Update (nur im Serve-Modus) ----
let nextRefresh = null;   // Zeitpunkt (ms) der nächsten automatischen Aktualisierung

async function refreshData(parts) {
  try {
    await fetch("api/refresh", { method: "POST",
      headers: {"Content-Type": "application/json"},
      body: parts ? JSON.stringify({ parts: parts }) : undefined });
    pollStatus();
  }
  catch (e) { document.getElementById("refreshStatus").textContent = "Server nicht erreichbar."; }
}

let _reloaded401 = false;
async function pollStatus() {
  let s, resp;
  try { resp = await fetch("api/status"); s = await resp.json(); } catch (e) { return; }
  // Session abgelaufen/Server neu gestartet: einmalig neu laden -> Anmeldemaske
  // statt dauerhaft "Nicht angemeldet" in der Statuszeile.
  if (resp.status === 401 && ME && !_reloaded401) {
    _reloaded401 = true;
    location.reload();
    return;
  }
  const st = document.getElementById("refreshStatus");
  document.getElementById("refreshBtn").disabled = !!s.refreshing;
  nextRefresh = (s.next != null) ? Date.now() + s.next * 1000 : null;
  const tiers = s.tiers || {};
  [["tierVms","vms"],["tierNetwork","network"],["tierStorage","storage"]].forEach(([id, k]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = tiers[k] ? "· " + tiers[k].slice(-5) : "";
  });
  if (s.refreshing) st.textContent = "Lade Daten aus Aria … " + (s.progress || "");
  else if (s.error) st.textContent = "Fehler beim letzten Abruf: " + s.error;
  else st.textContent = "";
  if (!s.refreshing && s.updated &&
      s.updated !== document.getElementById("stand").textContent) {
    try {
      const d = await (await fetch("api/data")).json();
      CLUSTERS = d.clusters || [];
      document.getElementById("stand").textContent = d.updated || "";
      render();
    } catch (e) {}
  }
}

function tickTimer() {
  const el = document.getElementById("timer");
  if (nextRefresh === null) { el.textContent = ""; return; }
  const s = Math.max(0, Math.round((nextRefresh - Date.now()) / 1000));
  el.textContent = "Auto-Update in " + Math.floor(s / 60) + ":" +
                   String(s % 60).padStart(2, "0") + " min";
}

// Einmalige Migration: alte localStorage-Reservierungen auf den Server übernehmen
function migrateLocalRes(serverList) {
  if (serverList.length || localStorage.getItem(LS_KEY + "_migriert")) return;
  let old = [];
  try { old = JSON.parse(localStorage.getItem(LS_KEY)) || []; } catch (e) {}
  if (!old.length) return;
  askConfirm({ title: "Alte Reservierungen übernehmen?", okLabel: "Übernehmen",
    message: old.length + " Reservierung(en) aus dem Browser-Speicher gefunden " +
             "(alter Speicherort). Auf den Server übernehmen?\nSie erscheinen dann " +
             "mit Status „beantragt“ und können unter „Genehmigungen“ freigegeben werden." })
    .then(ok => {
      if (ok) apiRes("PUT", "", old).then(l => {
        setRes(l); localStorage.removeItem(LS_KEY); }).catch(resFail);
      try { localStorage.setItem(LS_KEY + "_migriert", "1"); } catch (e) {}
    });
}

if (SERVE) {
  apiRes("GET").then(l => { setRes(l); migrateLocalRes(l); }).catch(() => {});
  pollStatus();
  setInterval(pollStatus, 3000);
  setInterval(tickTimer, 1000);
}
maybeShowAnnounce();
if (SERVE) loadStorage();   // STOR.enabled fürs Freigabe-Popup + Storage-Seite

// Deep-Link: #cluster=Name öffnet die Detailkarte direkt (auch teilbar in
// Tickets/Chats); reagiert zusätzlich auf Hash-Änderungen zur Laufzeit.
function openClusterHash() {
  const m = location.hash.match(/^#cluster=(.+)$/);
  if (!m) return;
  const name = decodeURIComponent(m[1]);
  const i = CLUSTERS.findIndex(c => c.name === name);
  if (i >= 0) showCard(i, null);
}
openClusterHash();
window.addEventListener("hashchange", openClusterHash);

// ============================================================================
// Sprache: Deutsch ist die Quelle im Code. Browser auf Deutsch -> alles bleibt
// wie es ist. Jede andere Browsersprache -> Englisch: ein Wörterbuch übersetzt
// NUR die Anzeige (Textknoten + Attribute) per MutationObserver. Interne Werte
// (Status, API-Felder, gespeicherte Daten, Audit-Log) bleiben unverändert.
// ============================================================================
const LANG = ((navigator.language || "de").toLowerCase().startsWith("de")) ? "de" : "en";
const I18N = {
  // Kapa-CSV-Format-Doku
  "Übernimmt eure bestehende Excel-Liste: in Excel als CSV speichern und hier hochladen. Alle Zeilen kommen als genehmigt an (Freigebender „Import\"); die Gültigkeit rechnet ab dem Original-Datum — ältere Einträge laufen entsprechend sofort ab (wird gemeldet). Bereits vorhandene Kapa-Nummern werden übersprungen, ein erneuter Import erzeugt also keine Duplikate. Spalten werden an der Kopfzeile erkannt — Reihenfolge egal, zusätzliche Spalten werden ignoriert:": "Takes over your existing Excel list: save it as CSV in Excel and upload it here. All rows arrive as approved (approver “Import”); validity counts from the original date — older entries expire immediately accordingly (reported). Existing kapa numbers are skipped, so re-importing does not create duplicates. Columns are matched by the header row — any order, extra columns are ignored:",
  "Spalte (Kopfzeile)": "Column (header)",
  "Pflicht": "Required",
  "Format": "Format",
  "Beispiel": "Example",
  "ja": "yes",
  "wird die ID; muss eindeutig sein": "becomes the ID; must be unique",
  "Freitext (Name der Anfrage)": "free text (name of the request)",
  "Cluster-Name wie im Dashboard": "cluster name as shown in the dashboard",
  "ganze Zahl (vCPUs)": "whole number (vCPUs)",
  "ganze Zahl in GB": "whole number in GB",
  "ganze Zahl in GB (Tausenderpunkt erlaubt)": "whole number in GB (thousands dot allowed)",
  "TT.MM.JJJJ (auch JJJJ-MM-TT)": "DD.MM.YYYY (also YYYY-MM-DD)",
  "Change-/Jira-Ticket": "change/Jira ticket",
  "Benutzer/Mail": "user/mail",
  "Team-Name wie im Dashboard": "team name as shown in the dashboard",
  "⬇ Beispiel-CSV (Vorlage)": "⬇ Sample CSV (template)",
  "Vorlage mit Kopfzeile und zwei Beispielzeilen – in Excel öffnen und befüllen": "Template with header row and two sample rows – open in Excel and fill in",
  // Kapa-CSV-Import
  "Übernimmt eure bestehende Excel-Liste: in Excel als CSV speichern und hier hochladen. Erkannte Spalten (Reihenfolge egal, per Kopfzeile): Kapa-Nummer, Projekt, Cluster, CPU, RAM, Storage, Datum — optional Change, Anforderer, Team. Alle Zeilen kommen als genehmigt an (Freigebender „Import\"); die Gültigkeit rechnet ab dem Original-Datum — ältere Einträge laufen entsprechend sofort ab (wird gemeldet). Bereits vorhandene Kapa-Nummern werden übersprungen, ein erneuter Import erzeugt also keine Duplikate.": "Takes over your existing Excel list: save it as CSV in Excel and upload it here. Recognized columns (any order, by header): Kapa-Nummer, Projekt, Cluster, CPU, RAM, Storage, Datum — optional Change, Anforderer, Team. All rows arrive as approved (approver “Import”); validity counts from the original date — older entries expire immediately accordingly (reported). Existing kapa numbers are skipped, so re-importing does not create duplicates.",
  "Kapa-Anfragen aus CSV (XLS-Ablösung)": "Capacity requests from CSV (replacing the XLS)",
  "CSV-Datei wählen & importieren": "Choose & import CSV file",
  "CSV-Import": "CSV import",
  "CSV-Inhalt fehlt": "CSV content missing",
  // Gestaffelte Abruf-Intervalle
  "Wie ein Cronjob: jeder Teilbereich hat seinen eigenen Takt — so muss nicht alles bei jedem Abruf gelesen werden. Kapazität (VMs, CPU/RAM/Disk, Tanzu), Netzwerk (Portgruppen/VLANs) und Storage (Datastores/LUNs). Cluster, Hosts und Tags laufen immer mit. Leer oder 0 = Standard-Intervall. Der ⟳-Knopf oben kann jederzeit alles oder einen einzelnen Bereich sofort aktualisieren.": "Like a cron job: each area has its own pace — so not everything has to be read on every refresh. Capacity (VMs, CPU/RAM/disk, Tanzu), Network (port groups/VLANs) and Storage (datastores/LUNs). Clusters, hosts and tags always run along. Empty or 0 = default interval. The ⟳ button above can refresh everything or a single area at any time.",
  "Datenabruf-Intervalle (gestaffelt)": "Data refresh intervals (tiered)",
  "Kapazität (VMs)": "Capacity (VMs)",
  "Netzwerk (Portgruppen)": "Network (port groups)",
  "Storage (Datastores)": "Storage (datastores)",
  "✓ Intervalle speichern": "✓ Save intervals",
  "✓ Intervalle & Zeitzone speichern": "✓ Save intervals & time zone",
  "Webhooks (Ereignisse an externe Systeme)": "Webhooks (events to external systems)",
  "Bei einem Ereignis schickt das Dashboard einen HTTP-POST mit JSON an die hinterlegten Ziele — für ITSM (ServiceNow/Jira), Automatisierung (Ansible AWX, n8n), Pipelines (GitLab/GitHub) oder Chat (Slack/Teams). Jeder Payload wird mit dem Secret HMAC-SHA256-signiert (Header X-Kapa-Signature), damit der Empfänger die Echtheit prüfen kann. Secrets werden nie zurück angezeigt und liegen bewusst nicht im Backup.":
    "On an event the dashboard sends an HTTP POST with JSON to the configured targets — for ITSM (ServiceNow/Jira), automation (Ansible AWX, n8n), pipelines (GitLab/GitHub) or chat (Slack/Teams). Each payload is HMAC-SHA256-signed with the secret (header X-Kapa-Signature) so the receiver can verify authenticity. Secrets are never shown back and are deliberately not in the backup.",
  "Aktiv": "Active", "Ziel-URL": "Target URL", "Ereignisse": "Events",
  "+ Webhook hinzufügen": "+ Add webhook", "✓ Webhooks speichern": "✓ Save webhooks",
  "Anfrage angelegt": "Request created", "Anfrage genehmigt": "Request approved",
  "Anfrage abgelehnt": "Request rejected",
  "Storage-Anfrage angelegt": "Storage request created",
  "Storage-Anfrage erledigt": "Storage request done",
  "Offline-Quelle importiert": "Offline source imported",
  "Noch kein Webhook konfiguriert.": "No webhook configured yet.",
  "z. B. AWX": "e.g. AWX",
  "vSAN-Kapazität (Policy-Faktor)": "vSAN capacity (policy factor)",
  "vROps liefert für vSAN keinen sauberen Nutzbar-nach-RAID-Wert, nur die physische Roh- und Belegt-Kapazität. Die Auslastung in % stimmt damit ohnehin; der Faktor rechnet nur die absolute nutzbare Kapazität aus der Policy (Annahme): RAID-1 ≈ 50 %, RAID-5 ≈ 75 % (3+1) bzw. 67 % (2+1), RAID-6 ≈ 67 %. Bei gemischten Policies je Cluster den passenden Standard wählen.":
    "vROps provides no clean usable-after-RAID value for vSAN, only the physical raw and used capacity. Utilization in % is therefore already correct; the factor only derives the absolute usable capacity from the policy (an assumption): RAID-1 ≈ 50 %, RAID-5 ≈ 75 % (3+1) or 67 % (2+1), RAID-6 ≈ 67 %. For mixed policies, pick the right default per cluster.",
  "Standard-Policy:": "Default policy:",
  "Override je Cluster (optional – für abweichende Policies):":
    "Per-cluster override (optional – for differing policies):",
  "+ Cluster-Override": "+ Cluster override", "eigener Wert": "custom value",
  "Cluster-Name": "cluster name", "Faktor": "Factor",
  "(0,05–1,0 · z. B. 0,5 = 50 % nutzbar)": "(0.05–1.0 · e.g. 0.5 = 50 % usable)",
  "Zeitzone der Anzeige": "Display time zone",
  "z. B. Europe/Berlin": "e.g. Europe/Berlin",
  "Alle angezeigten Zeiten (Stand, Log, die Abgleich-Uhrzeiten im ⟳-Menü, Mail-Zeitstempel) erscheinen in dieser Zeitzone. Der Abruf-Takt selbst ist davon unberührt (er rechnet in Sekunden). Leer = Zeitzone des Servers. IANA-Name, z. B. Europe/Berlin – die Sommer-/Winterzeit wird damit automatisch berücksichtigt.": "All displayed times (last update, log, the sync times in the ⟳ menu, mail timestamps) appear in this time zone. The refresh schedule itself is unaffected (it counts in seconds). Empty = the server's time zone. IANA name, e.g. Europe/Berlin – daylight saving is then handled automatically.",
  "Alles aktualisieren": "Refresh everything",
  "Nur Kapazität (VMs)": "Capacity only (VMs)",
  "Nur Netzwerk": "Network only",
  "Nur Storage": "Storage only",
  "Abruf-Intervalle geändert": "Refresh intervals changed",
  // Statistik (Trends)
  "Trends aus täglichen Snapshots der Datensammlung — z. B. ob VMs im Schnitt größer werden (RAM/Disk je VM). Die Historie wächst ab Einbau dieser Funktion; ältere Zeiträume füllen sich mit der Zeit.": "Trends from daily snapshots of the data collection — e.g. whether VMs are getting bigger on average (RAM/disk per VM). The history grows from the moment this feature was added; older ranges fill up over time.",
  "Statistik": "Statistics",
  "Statistik (Trends)": "Statistics (trends)",
  "Zeitraum": "Time range",
  "1 Jahr": "1 year",
  "2 Jahre": "2 years",
  "Ø RAM je VM": "Avg RAM per VM",
  "Ø vCPU je VM": "Avg vCPU per VM",
  "Ø Disk je VM": "Avg disk per VM",
  "VM-Anzahl": "VM count",
  "VM-Größenklassen nach RAM (GB)": "VM size classes by RAM (GB)",
  "Veränderung im Zeitraum": "Change over the period",
  "alle Cluster": "all clusters",
  "Historie als CSV (Semikolon, für Excel)": "History as CSV (semicolon, for Excel)",
  "Noch zu wenig Historie im gewählten Zeitraum (mindestens 2 Tage nötig). Die Daten wachsen mit jedem Tagesabruf.": "Not enough history in the selected range yet (at least 2 days needed). The data grows with every daily refresh.",
  "Noch keine Statistik-Daten (erster Snapshot entsteht mit dem nächsten Datenabruf).": "No statistics data yet (the first snapshot is created with the next data refresh).",
  "Statistik ist für diese Rolle ausgeblendet": "Statistics are hidden for this role",
  // Offline-Quellen / Cluster-Import (Verwaltung -> Import)
  "Für Bereiche ohne Netzanbindung an ein vROps: Ein Kollege führt das PowerCLI-Skript gegen das isolierte vCenter aus und lädt das erzeugte JSON hier mit einem Quellnamen hoch. Die Cluster erscheinen dann wie eine eigene vROps-Quelle — inklusive Kapazitätsanfragen, VLAN-Suche und Storage-Übersicht. Die Daten sind statisch (Stand = Import-Datum, als Tag am Cluster sichtbar); die Auto-Freigabe klammert diese Cluster bewusst aus. Ein erneuter Import unter demselben Namen ersetzt die Quelle.": "For areas without network access to a vROps: a colleague runs the PowerCLI script against the isolated vCenter and uploads the resulting JSON here under a source name. The clusters then appear like a separate vROps source — including capacity requests, VLAN search and storage overview. The data is static (as of the import date, visible as a tag on the cluster); auto-approval deliberately excludes these clusters. Re-importing under the same name replaces the source.",
  "Aufruf: .\\kapa_export.ps1 -Server vcenter.insel.local": "Run: .\\kapa_export.ps1 -Server vcenter.island.local",
  "Offline-Quellen (Cluster ohne vROps-Anbindung)": "Offline sources (clusters without vROps connection)",
  "⬇ PowerCLI-Skript (kapa_export.ps1)": "⬇ PowerCLI script (kapa_export.ps1)",
  "PowerCLI-Skript, das das Import-JSON erzeugt": "PowerCLI script that produces the import JSON",
  "JSON importieren": "Import JSON",
  "Quellname, z. B. RZ-Insel": "Source name, e.g. DC-Island",
  "JSON-Datei wählen & importieren": "Choose & import JSON file",
  "Quelle": "Source",
  "importiert am": "imported on",
  "Noch keine Offline-Quelle importiert.": "No offline source imported yet.",
  "importiere …": "importing …",
  "✓ importiert – Daten werden neu berechnet": "✓ imported – data is being recalculated",
  "Bitte zuerst einen Quellnamen eingeben (z. B. RZ-Insel).": "Please enter a source name first (e.g. DC-Island).",
  "Ungültige Datei: es fehlt die clusters-Liste.": "Invalid file: the clusters list is missing.",
  "Import fehlgeschlagen.": "Import failed.",
  "Import fehlgeschlagen (Netzwerk/Server).": "Import failed (network/server).",
  "Datei konnte nicht gelesen werden (kein gültiges JSON).": "File could not be read (not valid JSON).",
  "Offline-Quelle entfernen": "Remove offline source",
  "Offline-Quelle importiert": "Offline source imported",
  "Offline-Quelle gelöscht": "Offline source deleted",
  "Quellname erforderlich (z. B. RZ-Insel)": "Source name required (e.g. DC-Island)",
  "clusters: Liste mit mindestens einem Cluster erwartet": "clusters: expected a list with at least one cluster",
  "kein verwertbarer Cluster im Import": "no usable cluster in the import",
  // Netzwerk-Filter (Verwaltung -> Netzwerk)
  "Portgruppen, die hier zutreffen, werden komplett ausgeblendet — in der VLAN-Suche, im Netzwerk-Reiter der Cluster-Details und im Datenpaket. Die Änderung löst gleich einen neuen Datenabruf aus.": "Port groups that match here are hidden completely — in the VLAN search, in the network tab of the cluster details and in the data package. The change immediately triggers a new data collection.",
  "Portgruppen, deren Name einen dieser Begriffe enthält. Mehrere durch Komma trennen, Groß-/Kleinschreibung egal — */? gehen als Platzhalter (*-uplink, PG-Test-?). Leer = kein Filter.": "Port groups whose name contains one of these terms. Separate several by comma, case-insensitive — */? work as wildcards (*-uplink, PG-Test-?). Empty = no filter.",
  "Portgruppen mit diesen VLAN-IDs. Einzelne IDs und Bereiche, durch Komma getrennt — z. B. 99, 205, 3900-3999. Wirkt nur auf Portgruppen mit einer einzelnen VLAN-ID (Trunk-Bereiche blendet bereits die Uplink-Erkennung aus). Leer = kein Filter.": "Port groups with these VLAN IDs. Single IDs and ranges, separated by comma — e.g. 99, 205, 3900-3999. Applies only to port groups with a single VLAN ID (trunk ranges are already hidden by the uplink detection). Empty = no filter.",
  "Netzwerk-Filter (Portgruppen ausblenden)": "Network filter (hide port groups)",
  "VLAN-ID-Filter": "VLAN ID filter",
  "✓ gespeichert": "✓ saved",
  "Netzwerk-Filter geändert": "Network filter changed",
  "z. B. heartbeat, *-replikation, PG-Test-*": "e.g. heartbeat, *-replication, PG-Test-*",
  "z. B. 99, 205, 3900-3999": "e.g. 99, 205, 3900-3999",
  // Audit-Log: Standard-Aktionen (Aktion-Spalte)
  "Aria-Abruf beendet": "Fetch from Aria finished",
  "Aria-Abruf fehlgeschlagen": "Fetch from Aria failed",
  "Antrag erstellt": "Request created",
  "Antrag erstellt (API)": "Request created (API)",
  "Antrag automatisch freigegeben": "Request auto-approved",
  "Auto-Freigabe nicht angewendet": "Auto-approval not applied",
  "Erinnerung gesendet": "Reminder sent",
  "Automatisches Backup": "Automatic backup",
  "Automatisches Backup fehlgeschlagen": "Automatic backup failed",
  "Backup-Rotation": "Backup rotation",
  "Backup-Rotation fehlgeschlagen": "Backup rotation failed",
  "Backup ausgelöst": "Backup triggered",
  "Backup fehlgeschlagen": "Backup failed",
  "API-Zugriff abgewiesen": "API access denied",
  "API-Schreibzugriff abgewiesen": "API write access denied",
  "👥 Mitglieder": "👥 Members",
  "direkte Mitglied(er)": "direct member(s)",
  "Liste gekürzt – es werden nicht alle Mitglieder angezeigt.": "List truncated – not all members are shown.",
  "Keine direkten Benutzer-Mitglieder gefunden.": "No direct user members found.",
  "Nur direkte Benutzer-Mitglieder — verschachtelte Gruppen werden nicht aufgelöst.": "Direct user members only — nested groups are not resolved.",
  "Unbekannter Fehler beim AD-Abruf.": "Unknown error during AD lookup.",
  "Bind mit dem Service-Konto fehlgeschlagen (Zugangsdaten?).": "Bind with the service account failed (credentials?).",
  "AD ist nicht vollständig konfiguriert (Service-Konto/Base-DN).": "AD is not fully configured (service account/base DN).",
  "Keine AD-Anmeldung konfiguriert (--ad-url fehlt).": "No AD login configured (--ad-url missing).",
  "AD-Abfrage fehlgeschlagen (Netzwerk/Server).": "AD query failed (network/server).",
  "Wunschgröße (GB):": "Desired size (GB):",
  "Kommentar:": "Comment:",
  "Neue LUN, Größe (TB):": "New LUN, size (TB):",
  "✓ Anfragen": "✓ Request",
  "optional, z. B. Change/Grund": "optional, e.g. change/reason",
  "(keine erweiterbare LUN)": "(no expandable LUN)",
  "Storage-Erweiterung angefragt": "Storage expansion requested",
  "Reviewer-Handbuch": "Reviewer handbook",
  "– wie Prüfen & Freigeben funktioniert": "– how reviewing & approving works",
  "Reviewer-Handbuch öffnen": "Open reviewer handbook",
  "Storage-Anfrage löschen": "Delete storage request",
  "Storage-Erweiterung gelöscht": "Storage expansion deleted",
  "Diese Storage-Anfrage wirklich löschen? Das lässt sich nicht rückgängig machen.":
    "Really delete this storage request? This cannot be undone.",
  "Anfrage löschen (z. B. versehentlich angelegt)":
    "Delete request (e.g. created by mistake)",
  "Storage-Erweiterung erledigt (API)": "Storage expansion completed (API)",
  "Storage-Erweiterung erledigt": "Storage expansion completed",
  "Storage-Erweiterung wieder offen": "Storage expansion reopened",
  "Anmeldung fehlgeschlagen": "Login failed",
  "Anmeldung gesperrt": "Login blocked",
  "Anmeldung": "Login",
  "Abmeldung": "Logout",
  "Datenabruf aus Aria gestartet": "Data fetch from Aria started",
  "API-Token erstellt": "API token created",
  "Team umbenannt": "Team renamed",
  "Reservierungen importiert": "Reservations imported",
  "Genehmigungs-Teams geändert": "Approval teams changed",
  "Cluster-Selektor geändert": "Cluster selector changed",
  "Rollen-Bezeichnungen geändert": "Role labels changed",
  "Mail-Regeln geändert": "Mail rules changed",
  "Sichtbarkeit geändert": "Visibility changed",
  "Storage-Einstellungen": "Storage settings",
  "Auto-Freigabe geändert": "Auto-approval changed",
  "Ankündigung geändert": "Announcement changed",
  "API-Token-Rechte geändert": "API token permissions changed",
  "API-Token widerrufen": "API token revoked",
  "Rolle entfernt": "Role removed",
  "AD-Gruppe entfernt": "AD group removed",
  "Rolle zugewiesen": "Role assigned",
  "AD-Gruppe zugewiesen": "AD group assigned",
  // Ganz-Satz-Hints (mit Inline-<b>/<code>, per i18nFlatten übersetzt)
  "Wird nur lokal im Browser gespeichert und bei „Ausführen\" als Authorization: Bearer … mitgeschickt. Angemeldete Admins können auch ohne Token testen (Session-Cookie).": "Stored only locally in the browser and sent with “Run” as Authorization: Bearer …. Signed-in admins can also test without a token (session cookie).",
  "Portgruppen aller Cluster durchsuchen – z. B. nach einer IP-Adresse oder einem Netz aus dem Portgruppen-Namen. Das Ergebnis zeigt, an welchem Cluster das Netz hängt. Teil-Eingaben genügen (z. B. 10.2.30 oder VLAN205).": "Search the port groups of all clusters – e.g. for an IP address or a network from the port-group name. The result shows which cluster the network is attached to. Partial input is enough (e.g. 10.2.30 or VLAN205).",
  "Archiv der abgelehnten und stornierten Kapazitätsanfragen (Historie, zählt nicht gegen die Kapazität). Sichtbarkeit wie bei den Reservierungen: Anforderer sehen die des eigenen Teams, Reviewer/Admin/Auditor alle.": "Archive of rejected and cancelled capacity requests (history, does not count against capacity). Visibility as with reservations: requesters see their own team's, reviewer/admin/auditor see all.",
  "Alle Datastores/LUNs über alle Cluster. Neu angefragte Erweiterungen sind hervorgehoben; das Storage-Team ruft sie per API ab (/api/v1/storage-requests, auch als CSV inkl. NAA).": "All datastores/LUNs across all clusters. Newly requested expansions are highlighted; the storage team fetches them via API (/api/v1/storage-requests, also as CSV incl. NAA).",
  "Ist die Ankündigung aktiv, sieht jeder Benutzer sie einmal als Popup – nach dem Klick auf „Verstanden\" erscheint sie nicht erneut. Eine Änderung an Titel oder Text zeigt sie allen Benutzern noch einmal. Beispiele: Neues aus einem Release, neue Datacenter/Cluster, Wartungsfenster.": "When the announcement is active, every user sees it once as a popup – after clicking “Understood” it does not appear again. A change to the title or text shows it to all users once more. Examples: news from a release, new datacenter/cluster, maintenance window.",
  "Anträge durchlaufen die Teams von oben nach unten. Erst wenn alle Teams freigegeben haben, ist ein Antrag genehmigt. Ohne Teams gilt einstufig (Admin genehmigt direkt). Reviewer werden oben ihrem Team zugewiesen. Die E-Mail/Verteiler je Team wird angeschrieben, sobald das Team im Workflow an der Reihe ist (siehe „Mail-Benachrichtigungen\"); mit „✓ Team-Adressen speichern\" sichern.": "Requests pass through the teams from top to bottom. Only once all teams have approved is a request granted. Without teams it is single-stage (admin approves directly). Reviewers are assigned to their team above. The per-team email/distribution list is notified as soon as the team is up in the workflow (see “Mail notifications”); save with “✓ Save team addresses”.",
  "Erfüllt der Ziel-Cluster nach Abzug des Antrags alle Schwellen, gibt das System markierte Stufen automatisch frei (Freigebender: „Auto-Freigabe\", vollständig im Audit-Log). Geprüft wird bei der Antragstellung und immer, wenn eine Stufe neu an der Reihe ist. Greift eine Schwelle nicht oder fehlen Daten (z. B. kein Workload-Wert), geht der Antrag ganz normal an das Team — die Auto-Freigabe lehnt nie ab.": "If the target cluster meets all thresholds after deducting the request, the system automatically approves marked stages (approver: “Auto-approval”, fully in the audit log). It is checked at submission and whenever a stage is newly up. If a threshold is not met or data is missing (e.g. no workload value), the request goes to the team as normal — auto-approval never rejects.",
  "Haken = die Rolle sieht das Merkmal. Wirkt im UI und im Datenpaket (serverseitig entfernt). Administratoren sehen immer alles. Hier geht es nur um Sichtbarkeit — Rechte (Genehmigen, Verwaltung, Team-Sicht der Anforderer) bleiben fest an den Rollen.": "Check = the role sees the feature. Applies in the UI and in the data package (removed server-side). Administrators always see everything. This is only about visibility — permissions (approve, administration, requesters' team view) stay fixed to the roles.",
  "Ist dies aktiv, können Freigebende beim Genehmigen und alle Berechtigten in der Storage-Übersicht eine LUN-Vergrößerung oder eine neue LUN anfragen. Das Storage-Team ruft die offenen Anfragen per API ab (/api/v1/storage-requests, auch CSV inkl. NAA) und meldet mit einem Token-Schreibrecht „Storage\" die Umsetzung zurück.": "When this is active, approvers (when granting) and all authorized users in the storage overview can request a LUN expansion or a new LUN. The storage team fetches the open requests via API (/api/v1/storage-requests, also CSV incl. NAA) and reports completion back with a token “Storage” write permission.",
  "Datastores, deren Name einen dieser Begriffe enthält, werden ebenfalls komplett ausgeschlossen (überall, inkl. Kapazität). Mehrere durch Komma trennen, Groß-/Kleinschreibung egal — z. B. iso, backup, scratch. Ein Begriff wirkt als Teiltreffer (service erwischt auch server-service-01); */? gehen als Platzhalter (*-iso, lun-??-tmp). Leer = kein Filter.": "Datastores whose name contains one of these terms are also fully excluded (everywhere, incl. capacity). Separate several by comma, case-insensitive — e.g. iso, backup, scratch. A term acts as a partial match (service also catches server-service-01); */? work as wildcards (*-iso, lun-??-tmp). Empty = no filter.",
  "Obergrenze für Storage-Anfragen (Vergrößerung und neue LUN). Größere Wünsche werden abgelehnt. Als internes Limit gedacht — Randnotiz: VMFS-6 unterstützt ohnehin höchstens 64 TB je Datastore. 0 = kein Limit.": "Upper limit for storage requests (expansion and new LUN). Larger requests are rejected. Meant as an internal limit — side note: VMFS-6 supports at most 64 TB per datastore anyway. 0 = no limit.",
  "Legt fest, bei welchem Ereignis eine Mail rausgeht. Anforderer = der jeweilige Antragsteller (automatisch). Admin/Auditor = die eingetragene Verteiler-Adresse. Reviewer / „Team ist dran\" = die Team-Adresse aus der Teams-Tabelle (Reiter „Benutzer & Rollen\"). Voraussetzung ist ein konfigurierter SMTP-Server. „Freigabe\" meint die endgültige Genehmigung. „Erinnerung\" mailt das gerade zuständige Team (bzw. den Admin-Verteiler), wenn ein Antrag zu lange auf seine Freigabe wartet.": "Defines on which event a mail goes out. Requester = the respective applicant (automatic). Admin/Auditor = the configured distribution address. Reviewer / “Team's turn” = the team address from the teams table (tab “Users & roles”). Requires a configured SMTP server. “Approval” means the final grant. “Reminder” mails the currently responsible team (or the admin distribution list) when a request has been waiting too long for its approval.",
  "Die im System gesetzten Werte (aus INI, kapa.env bzw. Kommandozeile). Nur zur Ansicht – Änderungen erfolgen in der Konfiguration und erfordern einen Neustart. Passwörter werden nie angezeigt (nur, ob gesetzt).": "The values set in the system (from INI, kapa.env or command line). View only – changes are made in the configuration and require a restart. Passwords are never shown (only whether set).",
// --- Navigation / Kopf ---
"Kapazitätsübersicht pro Cluster": "Capacity overview per cluster",
"VMware Kapazitätsübersicht pro Cluster": "VMware capacity overview per cluster",
"VMware Kapazitätsplanung": "VMware Capacity Planning",
"Kapazität": "Capacity", "VLAN-Suche": "VLAN search", "Reservierungen": "Reservations",
"Genehmigungen": "Approvals", "Archiv": "Archive", "Verwaltung": "Administration", "Log": "Log",
"ℹ Info Kapa-Berechnung": "ℹ How capacity is calculated", "? Hilfe": "? Help",
"Stand:": "Data as of:", "Abmelden": "Sign out",
"+ Neue Kapazitätsanfrage": "+ New capacity request",
"Neue Kapazitätsanfrage": "New capacity request",
"⟳ Jetzt aktualisieren": "⟳ Refresh now",
"CSV exportieren": "Export CSV",
"Reservierungen exportieren (JSON)": "Export reservations (JSON)",
"Reservierungen importieren (JSON)": "Import reservations (JSON)",
"Reservierungen als CSV (Semikolon, für Excel)": "Reservations as CSV (semicolon, Excel-ready)",
"Kapazität als CSV": "Capacity as CSV",
"Hell/Dunkel umschalten": "Toggle light/dark", "Helles Design": "Light theme",
"Dunkles Design": "Dark theme",
"Kapazitätstabelle als CSV (Semikolon, für Excel) – inkl. effektiv freier Werte":
  "Capacity table as CSV (semicolon, Excel-ready) – incl. effective free values",
"Cluster filtern …": "Filter clusters …", "Cluster suchen …": "Search clusters …",
"Schließen": "Close", "Ausblenden": "Hide", "Abbrechen": "Cancel", "Bestätigen": "Confirm",
"Hinweis": "Notice", "Übernehmen": "Apply", "Zurücksetzen": "Reset",
"⚙ Spalten": "⚙ Columns", "alle einblenden": "show all", "Spalten ein-/ausblenden": "Show/hide columns",
// --- Kapazitätstabelle ---
"Cluster": "Cluster", "Hosts": "Hosts", "VMs": "VMs", "vCPU frei": "vCPU free",
"vCPU-Auslastung": "vCPU usage", "RAM frei (GB)": "RAM free (GB)", "RAM-Auslastung": "RAM usage",
"Storage frei (GB)": "Storage free (GB)", "Storage-Auslastung": "Storage usage",
"Res.": "Res.", "Workload (vROps)": "Workload (vROps)",
"Cluster-Details anzeigen": "Show cluster details",
"Kein Cluster entspricht dem Filter.": "No cluster matches the filter.",
"(kein Cluster passt)": "(no matching cluster)",
"Gesamt (alle Cluster)": "Total (all clusters)", "Gesamt (Filter)": "Total (filtered)",
"Cluster-Selektor:": "Cluster selector:", "vROps": "vROps", "alle": "all",
"Antrag passt": "Request fits", "Antrag überschreitet die freie Kapazität": "Request exceeds free capacity",
// --- Detailkarte ---
"CPU & RAM": "CPU & RAM", "Netzwerk": "Network", "Storage": "Storage",
"Kapazitätsreservierungen": "Capacity reservations",
"frei nach Reservierungen": "free after reservations", "reserviert": "reserved",
"Keine Reservierungen.": "No reservations.", "Keine aktiven Reservierungen.": "No active reservations.",
"Datastores/LUNs anzeigen": "Show datastores/LUNs", "keine Storage-Daten aus Aria": "no storage data from Aria",
"Keine Storage-Daten aus Aria.": "No storage data from Aria.",
"Keine Portgruppen-Daten aus Aria.": "No port group data from Aria.",
"Keine Portgruppe passt zur Suche.": "No port group matches the search.",
"Datastore / LUN": "Datastore / LUN", "Größe (GB)": "Size (GB)", "Belegt (GB)": "Used (GB)",
"Frei (GB)": "Free (GB)", "Belegt %": "Used %", "Portgruppe": "Port group", "Portgruppen": "Port groups",
"vSphere-Tags": "vSphere tags", "Host": "Host", "Cores": "Cores", "RAM GB": "RAM GB",
"Storage GB": "Storage GB", "Server / Speicher": "Compute / memory",
"IP-Adresse, Netz oder Portgruppen-Name suchen …": "Search IP address, subnet or port group name …",
"VLAN / IP / Portgruppe suchen …": "Search VLAN / IP / port group …",
"Details anzeigen": "Show details",
"Portgruppen aller Cluster durchsuchen – z. B. nach einer IP-Adresse oder einem Netz aus dem Portgruppen-Namen. Das Ergebnis zeigt, an welchem Cluster das Netz hängt. Teil-Eingaben genügen (z. B.":
  "Search port groups across all clusters – e.g. by an IP address or a subnet that is part of the port group name. The result shows which cluster the network is attached to. Partial input is fine (e.g.",
// --- Formular neue Anfrage ---
"Ziel-Cluster": "Target cluster", "Bezeichnung / Projekt": "Name / project",
"Change / Jira Ticket (optional)": "Change / Jira ticket (optional)",
"Change / Jira-Ticket": "Change / Jira ticket",
"z. B. SAP-Erweiterung Q4": "e.g. SAP expansion Q4",
"Beantragen": "Submit request", "+ Beantragen": "+ Submit request",
"Freie Kapazität überschritten": "Free capacity exceeded",
"Trotzdem beantragen": "Request anyway",
"Die Reservierung überschreitet die freie Kapazität von": "The reservation exceeds the free capacity of",
"Cluster unbekannt": "Unknown cluster",
// --- Reservierungen / Archiv ---
"Reservierungen durchsuchen – Name, Cluster, Change, Anforderer, Team, ID, Status …":
  "Search reservations – name, cluster, change, requester, team, ID, status …",
"Archiv durchsuchen – Name, Cluster, Change, Anforderer, Team, ID, Status …":
  "Search archive – name, cluster, change, requester, team, ID, status …",
"Reservierungen filtern …": "Filter reservations …", "Log filtern …": "Filter log …",
"Benutzer filtern …": "Filter users …",
"Anfrage / Projekt": "Request / project", "Anfrage": "Request",
"Change": "Change", "vCPU": "vCPU", "RAM (GB)": "RAM (GB)", "Storage (GB)": "Storage (GB)",
"von": "by", "Team": "Team", "gilt ab": "valid from", "gültig bis": "valid until",
"Status": "Status", "entschieden von": "decided by", "beantragt am": "requested on",
"erledigt am": "closed on", "Fortschritt": "Progress",
"Kapa-ID": "Capa ID", "Eindeutige ID der Anfrage": "Unique request ID",
"beantragt": "requested", "genehmigt": "approved", "abgelehnt": "rejected",
"storniert": "cancelled", "durch": "by", "am": "on", "und": "and", "oder": "or",
"nie": "never", "nein": "no", "– keine –": "– none –",
"Archiv ist leer.": "The archive is empty.",
"Archiv der": "Archive of", "abgelehnten": "rejected", "stornierten": "cancelled",
"Kapazitätsanfragen (Historie, zählt nicht gegen die Kapazität). Sichtbarkeit wie bei den Reservierungen: Anforderer sehen die des eigenen Teams, Reviewer/Admin/Auditor alle.":
  "capacity requests (history – does not count against capacity). Visibility as with reservations: requesters see their own team's, reviewers/admins/auditors see all.",
"Anfrage stornieren": "Cancel request", "⦸ Storno": "⦸ Cancel",
"Kommentar": "Comment", "Kommentar (optional)": "Comment (optional)",
"kurze Begründung, max. 64 Zeichen": "short reason, max. 64 characters",
// --- Genehmigungen ---
"Cluster frei vCPU": "Cluster free vCPU", "Cluster frei RAM": "Cluster free RAM",
"Frei im Ziel-Cluster nach genehmigten Reservierungen": "Free in target cluster after approved reservations",
"Keine offenen Anträge – alles genehmigt.": "No open requests – everything approved.",
"Antrag ablehnen": "Reject request", "Ablehnen": "Reject", "✕ Ablehnen": "✕ Reject",
"Team ist dran": "team's turn",
// --- Verwaltung: Reiter + Benutzer/Rollen ---
"Benutzer & Rollen": "Users & roles", "Mail": "Mail", "Backup & Konfiguration": "Backup & configuration",
"Benutzer und Rollen": "Users and roles", "Benutzer / AD-Gruppe": "User / AD group",
"Benutzer": "User", "AD-Gruppe": "AD group", "Typ": "Type", "Rolle": "Role", "Aktion": "Action",
"benutzer@firma.local oder vorname.nachname": "user@example.com or first.last",
"– Team wählen –": "– select team –", "+ Zuweisen": "+ Assign",
"Rolle/Team bearbeiten": "Edit role/team", "Zuweisung entfernen": "Remove assignment",
"Rollenzuweisung entfernen": "Remove role assignment", "Entfernen": "Remove", "✕ Entfernen": "✕ Remove",
"Noch keine Rollen zugewiesen.": "No roles assigned yet.",
"Ohne Team: sieht nur eigene Anfragen bzw. kann nichts freigeben": "Without a team: sees only own requests / cannot approve",
"Dieses Team gibt es nicht (mehr) – bitte neu zuweisen": "This team no longer exists – please reassign",
"⚠ unbekannt": "⚠ unknown", "⚠ kein Team": "⚠ no team",
"Administrator": "Administrator", "Anforderer": "Requester", "Reviewer": "Reviewer",
"Technische Prüfung": "Technical audit",
"(admin)": "(admin)", "(anforderer)": "(requester)", "(reviewer)": "(reviewer)", "(auditor)": "(auditor)",
"AD-Mail: ": "AD mail: ",
"keine AD-Mail aufgelöst (nur mit --ad-mail-attribute)": "no AD mail resolved (requires --ad-mail-attribute)",
// --- Verwaltung: Rollen-Bezeichnungen / Teams ---
"Rollen-Bezeichnungen": "Role labels", "Interne Rolle": "Internal role",
"Angezeigte Bezeichnung": "Displayed label",
"Die angezeigten Namen der Rollen sind frei wählbar. Die Rechte bleiben an der internen Rolle (linke Spalte) gebunden und ändern sich dadurch nicht.":
  "Role display names can be chosen freely. Permissions stay bound to the internal role (left column) and are not affected.",
"✓ Bezeichnungen speichern": "✓ Save labels",
"Genehmigungs-Teams (Prüfreihenfolge)": "Approval teams (review order)",
"Anträge durchlaufen die Teams von oben nach unten. Erst wenn alle Teams freigegeben haben, ist ein Antrag genehmigt. Ohne Teams gilt einstufig (Admin genehmigt direkt). Reviewer werden oben ihrem Team zugewiesen. Die":
  "Requests pass through the teams from top to bottom. Only when all teams have approved is a request fully approved. Without teams, approval is single-stage (admin approves directly). Reviewers are assigned to their team above. The",
"E-Mail / Verteiler (Team ist dran)": "Email / list (team's turn)", "E-Mail/Verteiler": "Email/list",
"je Team wird angeschrieben, sobald das Team im Workflow an der Reihe ist (siehe „Mail-Benachrichtigungen\"); mit „✓ Team-Adressen speichern\" sichern.":
  "each team is notified as soon as it is up in the workflow (see \"Mail notifications\"); save with \"✓ Save team addresses\".",
"Neues Team, z. B. Team Betrieb": "New team, e.g. Operations team",
"+ Hinzufügen": "+ Add", "nach oben": "move up", "nach unten": "move down",
"✎ Umbenennen": "✎ Rename", "Team umbenennen": "Rename team", "Team entfernen": "Remove team",
"✓ Speichern": "✓ Save", "✓ Team-Adressen speichern": "✓ Save team addresses",
"Keine Teams – einstufig (Admin genehmigt direkt).": "No teams – single-stage (admin approves directly).",
"team@firma.de": "team@example.com", "verteiler@firma.de": "list@example.com",
// --- Verwaltung: Mail ---
"Mail-Benachrichtigungen (je interner Rolle)": "Mail notifications (per internal role)",
"Legt fest, bei welchem Ereignis eine Mail rausgeht.": "Defines which event triggers an email.",
"= der jeweilige Antragsteller (automatisch).": "= the respective requester (automatic).",
"= die eingetragene Verteiler-Adresse.": "= the configured distribution address.",
"= die Team-Adresse aus der Teams-Tabelle (Reiter „Benutzer & Rollen\"). Voraussetzung ist ein konfigurierter SMTP-Server. „Freigabe\" meint die endgültige Genehmigung.":
  "= the team address from the teams table (\"Users & roles\" tab). Requires a configured SMTP server. \"Approval\" means the final approval.",
"= Antragsteller (automatisch)": "= requester (automatic)",
"pro Team (Tabelle oben)": "per team (table above)",
"Admin/Auditor": "Admin/Auditor", "Reviewer / „Team ist dran\"": "Reviewer / \"team's turn\"",
"Anlage": "Created", "Ablehnung": "Rejection", "Freigabe": "Approval",
"Erinnerung": "Reminder", "Verteiler-Adresse": "Distribution address",
"„Erinnerung\"": "„Reminder“",
"mailt das gerade zuständige Team (bzw. den Admin-Verteiler), wenn ein Antrag zu lange auf seine Freigabe wartet.":
  "mails the team currently up (or the admin list) when a request has been waiting too long for its approval.",
"Erinnerung nach": "Remind after",
"Tagen Wartezeit – danach alle so viele Tage erneut, bis entschieden ist.":
  "days of waiting – then again every that many days until decided.",
"✓ Mail-Regeln speichern": "✓ Save mail rules",
"Mail-Vorlage (HTML)": "Mail template (HTML)",
"Betreff und HTML-Text der Reservierungs-Mails. Verfügbare Variablen unten einfach anklicken, um sie an der Cursor-Position einzufügen. Leer lassen = eingebaute Standardvorlage. „Vorschau\" rendert die Vorlage mit Beispieldaten.":
  "Subject and HTML body of the reservation emails. Click a variable below to insert it at the cursor position. Leave empty = built-in default template. \"Preview\" renders the template with sample data.",
"Betreff": "Subject", "HTML-Text": "HTML body",
"Standardbetreff": "Default subject", "Standardvorlage (leer lassen)": "Default template (leave empty)",
"✓ Vorlage speichern": "✓ Save template", "Vorschau": "Preview", "Standard einsetzen": "Insert default",
"Vorschau (Beispieldaten) · Betreff:": "Preview (sample data) · subject:",
"Vorschau fehlgeschlagen.": "Preview failed.",
"Speichern der Mail-Vorlage fehlgeschlagen.": "Saving the mail template failed.",
"Ereignis (beantragt / genehmigt / abgelehnt / wartet auf Freigabe …)": "Event (requested / approved / rejected / awaiting approval …)",
"Team / Abteilung des Anforderers": "Requester's team / department",
"Gilt ab (Anlagedatum)": "Valid from (creation date)", "Gültig bis": "Valid until",
"Freigaben (Liste der Team-Freigaben)": "Approvals (list of team approvals)",
"letzter Kommentar": "last comment", "ausführende Person": "acting person",
"Zeitpunkt der Mail": "time of the email",
"aktuell zuständiges Team (bei „Team ist dran“)": "team currently up (for \"team's turn\")",
"← Zurück": "← Back", "Weiter →": "Next →", "Pro Seite": "Per page",
"vROps-Quelle": "vROps source", "vROps-Quellen": "vROps sources",
"nichts angehakt = alle Quellen": "nothing ticked = all sources",
"alle Quellen": "all sources", "vCPU-Anzahl": "vCPU count",
"RAM (inkl. „GB“)": "RAM (incl. \"GB\")", "Storage (inkl. „GB“)": "Storage (incl. \"GB\")",
"SMTP / Versand (aus der Konfiguration)": "SMTP / delivery (from configuration)",
// --- Verwaltung: Selektor / Tokens / Backup / Konfiguration ---
"Cluster-Selektor (Filter nach vSphere-Tags)": "Cluster selector (filter by vSphere tags)",
"Bis zu 3 Stufen, jede Stufe eine Tag-Kategorie. In der Kapazitätsübersicht erscheinen dann kaskadierende Auswahllisten (Stufe 2 zeigt nur Werte, die zur Wahl in Stufe 1 passen). Die Kategorien kommen aus den vorhandenen Cluster-Tags – sind noch keine Daten geladen, ist die Liste leer.":
  "Up to 3 levels, each level one tag category. The capacity view then shows cascading dropdowns (level 2 only shows values matching the level-1 choice). Categories come from the existing cluster tags – if no data is loaded yet, the list is empty.",
"Stufe": "Level", "Tag-Kategorie": "Tag category", "Anzeigename im Selektor": "Display name in selector",
"Anzeigename (leer = Kategorie)": "Display name (empty = category)",
"Kategorie derzeit in keinem Cluster-Tag": "category currently in no cluster tag",
"Noch keine Tag-Kategorien – erst nach dem ersten Aria-Abruf verfügbar.": "No tag categories yet – available after the first Aria refresh.",
"✓ Selektor speichern": "✓ Save selector",
"⚠ derzeit ohne Werte": "⚠ currently without values",
"API-Tokens für externe Anwendungen (Endpunkte unter /api/v1/; Schreibrechte je Token per Klick)": "API tokens for external applications (endpoints under /api/v1/; write permissions per token, one click)",
"API-Dokumentation öffnen": "Open API documentation",
"(interaktiv, mit „Ausführen\") ·": "(interactive, with \"Run\") ·",
"OpenAPI-Spec": "OpenAPI spec", "zum Import in Swagger/Postman.": "for import into Swagger/Postman.",
"Anwendung": "Application", "Name der Anwendung, z. B. Grafana oder CMDB-Sync": "Application name, e.g. Grafana or CMDB sync",
"+ Token erzeugen": "+ Create token", "Token-Anfang": "Token prefix",
"zuletzt benutzt": "last used", "erstellt": "created", "angelegt": "created",
"✕ Widerrufen": "✕ Revoke", "API-Token widerrufen": "Revoke API token", "Widerrufen": "Revoke",
"Keine API-Tokens vorhanden.": "No API tokens.",
"Neues API-Token – wird nur EINMAL angezeigt, jetzt kopieren:": "New API token – shown only ONCE, copy it now:",
"Token konnte nicht erstellt werden.": "Token could not be created.",
"Widerruf fehlgeschlagen.": "Revoke failed.",
"Backup": "Backup", "💾 Backup jetzt erstellen": "💾 Create backup now",
"Sichert alle Laufzeitdaten (Reservierungen, Rollen, Teams, Selektor, Log, Tokens) als tar.gz auf das konfigurierte SFTP-Ziel. Läuft automatisch nach dem konfigurierten Intervall – hier lässt sich ein Backup sofort auslösen.":
  "Saves all runtime data (reservations, roles, teams, selector, log, tokens) as tar.gz to the configured SFTP target. Runs automatically at the configured interval – here you can trigger a backup immediately.",
"Backup läuft … (kann einen Moment dauern)": "Backup running … (may take a moment)",
"Konfiguration (schreibgeschützt)": "Configuration (read-only)",
"Die im System gesetzten Werte (aus INI, kapa.env bzw. Kommandozeile). Nur zur Ansicht – Änderungen erfolgen in der Konfiguration und erfordern einen Neustart.":
  "The values configured in the system (from INI, kapa.env or command line). View only – changes are made in the configuration and require a restart.",
"Passwörter werden nie angezeigt": "Passwords are never shown",
"(nur, ob gesetzt).": "(only whether they are set).",
"Lade Konfiguration …": "Loading configuration …",
"Datenquellen (vROps)": "Data sources (vROps)", "Datenquelle (vROps)": "Data source (vROps)",
"Berechnung": "Calculation", "CPU-Faktor": "CPU factor", "Failover-Hosts (N+1)": "Failover hosts (N+1)",
"vSAN-Faktor": "vSAN factor", "Reservierung gültig (Tage)": "Reservation valid (days)",
"Uplink-Portgruppen anzeigen": "Show uplink port groups", "Ausschluss-Tag": "Exclusion tag",
"Tag-Präfix": "Tag prefix", "(alle mit 'tag')": "(all containing 'tag')",
"Auto-Refresh (Sek.)": "Auto-refresh (sec.)", "Mail / SMTP": "Mail / SMTP",
"SMTP-Server": "SMTP server", "Absender": "Sender", "smtp-to (Fallback Admin)": "smtp-to (admin fallback)",
"STARTTLS": "STARTTLS", "SMTP-Passwort gesetzt": "SMTP password set",
"AD-Mail-Attribut": "AD mail attribute", "– (UPN)": "– (UPN)",
"Ziel": "Target", "Port": "Port", "Intervall (Sek.)": "Interval (sec.)",
"Aufbewahrung (Tage)": "Retention (days)", "SSH-Key": "SSH key",
"Backup-Passwort gesetzt": "Backup password set", "– (kein Backup)": "– (no backup)",
"– (kein Mailversand)": "– (no mail delivery)", "Active Directory": "Active Directory",
"Domäne": "Domain", "Admin-User": "Admin user", "Service-Konto (Bind-DN)": "Service account (bind DN)",
"Basis-DN": "Base DN", "Bind-Passwort gesetzt": "Bind password set",
"– (keine AD-Anmeldung)": "– (no AD sign-in)", "Kontakt / Impressum": "Contact / imprint",
"Webserver": "Web server", "Bind-Adresse": "Bind address", "Daten-Verzeichnis": "Data directory",
"Datenspeicher": "Data store",
// --- Log ---
"Zeit": "Time",
"Von": "From", "Bis": "To", "Datum zurücksetzen": "Reset dates",
"← Neuer": "← Newer", "Älter →": "Older →",
"Keine Log-Einträge": "No log entries",
"Keine Log-Einträge.": "No log entries.",
"Keine Log-Einträge für diese Auswahl.": "No log entries for this selection.",
// --- Statuszeile / Meldungen / Dialoge ---
"Server nicht erreichbar.": "Server unreachable.",
"Speichern fehlgeschlagen.": "Saving failed.",
"Löschen fehlgeschlagen.": "Deleting failed.",
"Speichern der Teams fehlgeschlagen.": "Saving teams failed.",
"Speichern des Selektors fehlgeschlagen.": "Saving the selector failed.",
"Speichern der Mail-Regeln fehlgeschlagen.": "Saving mail rules failed.",
"Umbenennen fehlgeschlagen (Name evtl. schon vergeben).": "Rename failed (name may already be taken).",
"Reservierungen konnten nicht auf dem Server gespeichert werden.": "Reservations could not be saved on the server.",
"Datei konnte nicht gelesen werden.": "File could not be read.",
"Ungültige Datei.": "Invalid file.",
"Alte Reservierungen übernehmen?": "Adopt old reservations?",
"Benutzername oder Passwort falsch.": "Wrong username or password.",
"Zu viele Fehlversuche – bitte einige Minuten warten und erneut versuchen.": "Too many failed attempts – please wait a few minutes and try again.",
"Nicht angemeldet": "Not signed in",
"Keine Berechtigung für diese Aktion": "No permission for this action",
"✎ Bearbeiten": "✎ Edit", "✕ Löschen": "✕ Delete",
"Hilfe": "Help", "Info Kapa-Berechnung": "How capacity is calculated",
// --- Ankündigung ---
"Ankündigung": "Announcement",
"Ankündigung (Popup nach der Anmeldung)": "Announcement (popup after sign-in)",
"Ist die Ankündigung aktiv, sieht jeder Benutzer sie": "While the announcement is active, every user sees it",
"einmal": "once",
"als Popup – nach dem Klick auf „Verstanden\" erscheint sie nicht erneut. Eine Änderung an Titel oder Text zeigt sie allen Benutzern noch einmal. Beispiele: Neues aus einem Release, neue Datacenter/Cluster, Wartungsfenster.":
  "as a popup – after clicking „Got it“ it does not appear again. Changing the title or text shows it to all users once more. Examples: release news, new datacenters/clusters, maintenance windows.",
"Titel": "Title", "Text": "Text",
"z. B. Neu: Datacenter RZ-Sued verfügbar": "e.g. New: datacenter RZ-Sued available",
"Der Text des Popups (Zeilenumbrüche bleiben erhalten, kein HTML).": "The popup text (line breaks are kept, no HTML).",
"aktiv – Popup wird angezeigt": "active – popup is shown",
"✓ Ankündigung speichern": "✓ Save announcement",
"Verstanden": "Got it",
"Speichern der Ankündigung fehlgeschlagen.": "Saving the announcement failed.",
// --- Tanzu-Namespaces ---
"davon Tanzu-Namespaces": "of which Tanzu namespaces",
"Namespace": "Namespace", "CPU (MHz)": "CPU (MHz)", "vCPU-Äquiv.": "vCPU equiv.",
// --- Token-Schreibrechte / Verwaltungs-Reiter ---
"Cluster-Selektor": "Cluster selector", "API-Tokens": "API tokens",
// --- Auto-Freigabe ---
"Auto-Freigabe": "Auto-approval",
"Auto-Freigabe (Schwellenwerte)": "Auto-approval (thresholds)",
"Erfüllt der Ziel-Cluster": "If the target cluster meets all thresholds",
"nach": "after",
"Abzug des Antrags alle Schwellen, gibt das System markierte Stufen automatisch frei (Freigebender: „Auto-Freigabe\", vollständig im Audit-Log). Geprüft wird bei der Antragstellung und immer, wenn eine Stufe neu an der Reihe ist. Greift eine Schwelle nicht oder fehlen Daten (z. B. kein Workload-Wert), geht der Antrag ganz normal an das Team — die Auto-Freigabe lehnt nie ab.":
  "subtracting the request, the system approves marked stages automatically (approver: „Auto-Freigabe“, fully audit-logged). Evaluated on request creation and whenever a stage becomes current. If a threshold is missed or data is missing (e.g. no workload value), the request simply goes to the team — auto-approval never rejects.",
"aktiv – Auto-Freigabe einschalten": "active – enable auto-approval",
"vCPU frei mindestens": "vCPU free at least",
"RAM frei mindestens": "RAM free at least",
"Größte freie LUN mindestens frei": "Largest free LUN at least free",
"Workload höchstens": "Workload at most",
"Stufen mit Auto-Freigabe": "Stages with auto-approval",
"Nur angehakte Teams werden automatisch freigegeben — z. B. Team 1 prüft manuell, die weiteren Stufen laufen automatisch durch. Ohne Teams gilt der Haken sinngemäß für die einstufige Freigabe.":
  "Only checked teams are approved automatically — e.g. team 1 reviews manually, the later stages pass through automatically. Without teams, the thresholds apply to the single-stage approval.",
"Keine Teams – einstufig (Haken entfällt, es gelten nur die Schwellen).": "No teams – single-stage (no checkboxes, only the thresholds apply).",
"✓ Auto-Freigabe speichern": "✓ Save auto-approval",
"Speichern der Auto-Freigabe fehlgeschlagen.": "Saving the auto-approval failed.",
"genehmigt (auto)": "approved (auto)",
// --- Sichtbarkeits-Matrix ---
"Sichtbarkeit": "Visibility",
"Sichtbarkeit (was sieht welche Rolle)": "Visibility (what each role sees)",
"Haken = die Rolle sieht das Merkmal. Wirkt im UI": "Check = the role sees the feature. Applies in the UI",
"im Datenpaket (serverseitig entfernt). Administratoren sehen immer alles. Hier geht es nur um":
  "in the data payload (stripped server-side). Administrators always see everything. This is only about",
"— Rechte (Genehmigen, Verwaltung, Team-Sicht der Anforderer) bleiben fest an den Rollen.":
  "— permissions (approving, administration, requesters' team view) stay fixed to the roles.",
"Merkmal": "Feature",
"Workload %": "Workload %", "Host-Liste": "Host list", "VM-Liste": "VM list",
"Netzwerk & VLAN-Suche": "Network & VLAN search",
"Storage-Drilldown (LUNs)": "Storage drill-down (LUNs)",
"Entschieden von": "Decided by",
"✓ Sichtbarkeit speichern": "✓ Save visibility",
"✓ gespeichert – gilt beim nächsten Laden der Seite": "✓ saved – takes effect on next page load",
"Speichern der Sichtbarkeit fehlgeschlagen.": "Saving the visibility failed.",
"Schreibrechte": "Write permissions",
// --- Storage-Erweiterungen ---
"Storage": "Storage",
"Storage filtern – Cluster, LUN, NAA, Typ …": "Filter storage – cluster, LUN, NAA, type …",
"Anfragen als CSV": "Requests as CSV", "Storage-Anfragen als CSV (inkl. NAA)": "Storage requests as CSV (incl. NAA)",
"Datastore / LUN": "Datastore / LUN", "NAA": "NAA", "Erweiterung": "Expansion",
"Keine Storage-Daten.": "No storage data.",
"Erweitern": "Expand", "angefragt": "requested",
"Storage-Erweiterung angefragt.": "Storage expansion requested.",
"Anfrage fehlgeschlagen.": "Request failed.",
"Ändern fehlgeschlagen.": "Change failed.",
"wieder offen": "reopen", "✓ erledigt": "✓ done",
"+ Storage-Erweiterung": "+ Storage expansion",
"Bestehende LUN vergrößern": "Grow an existing LUN",
"Neue LUN anlegen": "Create a new LUN",
"Wunschgröße muss größer als die aktuelle Größe sein.": "Target size must exceed the current size.",
"Bitte eine Größe in TB angeben.": "Please enter a size in TB.",
"Storage-Erweiterungen": "Storage expansions",
"Mindest-LUN-Größe": "Minimum LUN size",
"Datastores kleiner als dieser Wert werden komplett aus der Auswertung genommen — sie erscheinen nirgends (Storage-Übersicht, Cluster- Detail) und zählen auch nicht in die Storage-Kapazität/Auslastung. Praktisch, um kleine Boot-/ISO-/Scratch-Datastores auszublenden. 0 = alle anzeigen. Die Änderung löst gleich einen neuen Datenabruf aus.":
  "Datastores smaller than this value are removed entirely from the evaluation — they appear nowhere (storage overview, cluster detail) and do not count toward storage capacity/utilization either. Handy to hide small boot/ISO/scratch datastores. 0 = show all. The change triggers a fresh data fetch.",
"Mindestgröße:": "Minimum size:",
"Maximale LUN-Größe (Anfrage-Limit)": "Maximum LUN size (request limit)",
"Obergrenze für Storage-Anfragen (Vergrößerung und neue LUN). Größere Wünsche werden abgelehnt. Als internes Limit gedacht — Randnotiz: VMFS-6 unterstützt ohnehin höchstens":
  "Upper bound for storage requests (expansion and new LUN). Larger requests are rejected. Meant as an internal limit — side note: VMFS-6 supports at most",
"je Datastore. 0 = kein Limit.": "per datastore anyway. 0 = no limit.",
"Maximum:": "Maximum:",
"Namensfilter": "Name filter",
"Datastores, deren Name einen dieser Begriffe enthält, werden ebenfalls komplett ausgeschlossen (überall, inkl. Kapazität). Mehrere durch Komma trennen, Groß-/Kleinschreibung egal — z. B.":
  "Datastores whose name contains one of these terms are also excluded entirely (everywhere, incl. capacity). Separate several with commas, case-insensitive — e.g.",
"Ein Begriff wirkt als Teiltreffer (": "A term matches as a substring (",
"erwischt auch": "also catches",
"gehen als Platzhalter (": "work as wildcards (",
"Leer = kein Filter.": "Empty = no filter.",
". Leer = kein Namensfilter.": ". Empty = no name filter.",
"z. B. iso, backup, template": "e.g. iso, backup, template",
"✓ Speichern & anwenden": "✓ Save & apply",
"Storage-Erweiterungen erlauben": "Allow storage expansions",
"Storage-Erweiterungen erlauben ": "Allow storage expansions ",
"Ist dies aktiv, können Freigebende beim Genehmigen und alle Berechtigten in der Storage-Übersicht eine LUN-Vergrößerung oder eine neue LUN anfragen. Das Storage-Team ruft die offenen Anfragen per API ab":
  "When active, approvers (on approval) and everyone entitled (in the storage overview) can request a LUN expansion or a new LUN. The storage team fetches the open requests via the API",
", auch CSV inkl. NAA) und meldet mit einem Token-Schreibrecht „Storage\" die Umsetzung zurück.":
  ", also CSV incl. NAA) and reports completion with a token \"Storage\" write permission.",
"Ändern der Token-Rechte fehlgeschlagen.": "Changing the token permissions failed."
};
// Muster mit variablen Teilen (ganzer Text)
const I18N_RX = [
  [/^Auto-Update in (\d+:\d\d) min$/, "Auto-update in $1 min"],
  [/^aktuell am Server: /, "server time now: "],
  [/^in Prüfung \((\d+)\/(\d+)\)$/, "in review ($1/$2)"],
  [/^Summe genehmigt \((\d+) von (\d+)\)$/, "Total approved ($1 of $2)"],
  [/^(\d+) Anfragen$/, "$1 requests"], [/^(\d+) Anfrage$/, "$1 request"],
  [/^(\d+) Einträge$/, "$1 entries"], [/^(\d+) Eintrag$/, "$1 entry"],
  // Audit-Log: Aktionen mit dynamischem (API)-Anhang + reine Template-Details
  [/^Antrag genehmigt( \(API\))?$/, "Request approved$1"],
  [/^Antrag freigegeben( \(API\))?$/, "Request released$1"],
  [/^Antrag abgelehnt( \(API\))?$/, "Request rejected$1"],
  [/^Antrag storniert( \(API\))?$/, "Request cancelled$1"],
  [/^(\d+) Cluster, (\d+) VMs in ([\d.,]+) s$/, "$1 clusters, $2 VMs in $3 s"],
  [/^(\d+) Cluster, (\d+) VMs aus (\d+) Quellen in ([\d.,]+) s$/,
   "$1 clusters, $2 VMs from $3 sources in $4 s"],
  [/^nach ([\d.,]+) s: (.+)$/, "after $1 s: $2"],
  [/^Storage-Erweiterung – (.+)$/, "Storage expansion – $1"],
  [/^AD-Gruppe: (.+)$/, "AD group: $1"],
  [/^(\d+) Kapa-Anfragen übernommen \(genehmigt\)$/, "$1 capacity requests imported (approved)"],
  [/^(\d+) übersprungen \(Kapa-Nummer schon vorhanden\)$/, "$1 skipped (kapa number already exists)"],
  [/^(\d+) sofort abgelaufen \(Original-Datum älter als die Gültigkeit\)$/, "$1 expired immediately (original date older than the validity)"],
  [/^Unbekannte Cluster: (.+)$/, "Unknown clusters: $1"],
  [/^\(leer = Standard: (\d+) min\)$/, "(empty = default: $1 min)"],
  [/^(\d+) Tage$/, "$1 days"],
  [/^(\d+) Datenpunkte · (.+)$/, "$1 data points · $2"],
  [/^(\d+) Datenpunkte?$/, "$1 data points"],
  [/^grau = (.+) · farbig = (.+)$/, "grey = $1 · colored = $2"],
  [/^früher: (\d+)$/, "before: $1"],
  [/^heute: (\d+)$/, "today: $1"],
  [/^Quelle „(.+)“ samt ihrer Cluster aus der Übersicht entfernen\? Bestehende Kapazitätsanfragen auf diese Cluster bleiben erhalten\.$/,
   "Remove source „$1“ and its clusters from the overview? Existing capacity requests for these clusters are kept."],
  [/^Cluster „(.+)“: keine Hosts im Import$/, "Cluster „$1“: no hosts in the import"],
  [/^Speichern fehlgeschlagen \(HTTP (\d+)\)\.$/, "Saving failed (HTTP $1)."],
  [/^Gruppe „(.+)“ nicht im AD gefunden \(unter (.+)\)\.$/, "Group „$1“ not found in AD (under $2)."],
  [/^Keine Verbindung zum AD \((.+)\): (.+)$/, "No connection to AD ($1): $2"],
  [/^aktuell ([\d.,]+) GB$/, "current $1 GB"],
  [/^(\d+) Einträge \(gefiltert von (\d+)\)$/, "$1 entries (filtered from $2)"],
  [/^(\d+) Eintrag \(gefiltert von (\d+)\)$/, "$1 entry (filtered from $2)"],
  [/^Einträge (\d+)–(\d+) · Seite (\d+) von (\d+)$/, "Entries $1–$2 · Page $3 of $4"],
  [/^Genehmigungen \((\d+)\)$/, "Approvals ($1)"],
  [/^wartet auf: (.+)$/, "waiting for: $1"],
  [/^wartet auf (.+)$/, "waiting for $1"],
  [/^✓ Freigeben \((.+)\)$/, "✓ Approve ($1)"],
  [/^✓ Freigeben$/, "✓ Approve"],
  [/^(\d+) von (\d+)$/, "$1 of $2"],
  [/^(\d+)% belegt$/, "$1% used"],
  [/^(\d+)% belegt \(inkl\. Reservierungen\)$/, "$1% used (incl. reservations)"],
  [/^\+ (\d+) genehmigte Reservierung\(en\) anderer Teams – in „reserviert“ berücksichtigt\.$/,
   "+ $1 approved reservation(s) of other teams – included in \"reserved\"."],
  [/^brutto ([\d.,]+) GB · mit Faktor ([\d.,]+) als nutzbar gerechnet$/,
   "gross $1 GB · counted as usable with factor $2"],
  [/^AD-Mail: (.+)$/, "AD mail: $1"],
  [/^Spalte (\d+)$/, "Column $1"],
  [/^(\d+) alte\(s\) Archiv\(e\) gelöscht$/, "$1 old archive(s) deleted"],
  [/^Team „(.+)“ aus dem Genehmigungsprozess entfernen\?$/, "Remove team „$1“ from the approval process?"],
  [/^Zuletzt geändert: (.+?) durch (.+)$/, "Last changed: $1 by $2"],
  [/^Zuletzt geändert: (.+)$/, "Last changed: $1"],
  [/^Tanzu-Namespaces \((\d+)\)$/, "Tanzu namespaces ($1)"],
  [/^Stufe (\d+): (.+)$/, "Stage $1: $2"],
  [/^Kubernetes-Namespace-Reservierungen aus vROps – zählen wie genehmigte Reservierungen gegen die freie Kapazität \(CPU: ([\d.,]+) MHz je vCPU\)\.$/,
   "Kubernetes namespace reservations from vROps – count against free capacity like approved reservations (CPU: $1 MHz per vCPU)."],
  [/^Rollenzuweisung für „(.+)“ entfernen\?$/, "Remove role assignment for „$1“?"],
  [/^Quelle: VMware Aria Operations · CPU-Überprovisionierung: Faktor ([\d.,]+) \(physische Cores\).*abgezogen$/,
   "Source: VMware Aria Operations · CPU overcommit: factor $1 (physical cores) · RAM 1:1 · all VMs incl. powered-off · “free” accounts for approved reservations · failover spare (N+1): largest host per cluster deducted"],
  [/^Klick auf den Clusternamen zeigt Details und Reservierungen\. Neue Reservierungen gelten ab dem Anlagetag für (\d+) Tage.*nach (\d+) Tagen automatisch entfernt\. Speicherung zentral auf dem Server\. Genehmigung mehrstufig: (.+) \(erst wenn alle freigegeben haben, ist der Antrag genehmigt\)\.$/,
   "Click a cluster name to see details and reservations. New reservations are valid from the day of creation for $1 days, count against capacity only after approval and are removed automatically after $2 days. Stored centrally on the server. Multi-stage approval: $3 (a request is approved only once all teams have signed off)."],
  [/^Klick auf den Clusternamen zeigt Details und Reservierungen\. Neue Reservierungen gelten ab dem Anlagetag für (\d+) Tage.*nach (\d+) Tagen automatisch entfernt\. Speicherung zentral auf dem Server\.$/,
   "Click a cluster name to see details and reservations. New reservations are valid from the day of creation for $1 days, count against capacity only after approval and are removed automatically after $2 days. Stored centrally on the server."]
];
// Teil-Ersetzungen nur für zusammengesetzte Meta-Zeilen (Gate: enthält " · ")
const I18N_SUB = [
  [/(\d+) Kapa-Anfragen übernommen \(genehmigt\)/g, "$1 capacity requests imported (approved)"],
  [/(\d+) übersprungen \(Kapa-Nummer schon vorhanden\)/g, "$1 skipped (kapa number already exists)"],
  [/(\d+) sofort abgelaufen \(Original-Datum älter als die Gültigkeit\)/g, "$1 expired immediately (original date older than validity)"],
  [/Unbekannte Cluster: /g, "Unknown clusters: "],
  [/Übersprungene Zeilen: /g, "Skipped rows: "],
  [/VMware Kapazitätsplanung/g, "VMware Capacity Planning"],
  [/Quelle: /g, "Source: "], [/ nutzbare Cores/g, " usable cores"],
  [/\(davon (\d+) aus\)/g, "($1 powered off)"], [/(\d+) genehmigt/g, "$1 approved"],
  [/(\d+) beantragt/g, "$1 requested"], [/(\d+) abgelehnt/g, "$1 rejected"],
  [/größter Host als Reserve/g, "largest host held as spare"],
  [/Einträge (\d+)–(\d+)/g, "Entries $1–$2"], [/Seite (\d+) von (\d+)/g, "page $1 of $2"]
];
// Teil-Ersetzungen für Status-Tooltips ("genehmigt von X am Y · Kommentar: Z")
const I18N_TIP = [
  [/^Bereits freigegeben:/, "Already approved:"], [/wartet auf: /g, "waiting for: "],
  [/ von /g, " by "], [/ am /g, " on "], [/ in Stufe „/g, " at stage „"],
  [/· Kommentar: /g, "· comment: "],
  [/^abgelehnt/, "rejected"], [/^storniert/, "cancelled"], [/^genehmigt/, "approved"]
];
function i18nText(s) {
  const lead = s.match(/^\s*/)[0], tail = s.match(/\s*$/)[0];
  const key = s.replace(/\s+/g, " ").trim();
  if (!key) return s;
  const wrap = t => lead + t + tail;      // Leerraum um den Knoten erhalten
  const hit = I18N[key];
  if (hit !== undefined) return wrap(hit);
  for (const [rx, rep] of I18N_RX) if (rx.test(key)) return wrap(key.replace(rx, rep));
  if (/^(abgelehnt|storniert|genehmigt|Bereits freigegeben:)/.test(key)) {
    let out = key;
    for (const [rx, rep] of I18N_TIP) out = out.replace(rx, rep);
    return wrap(out);
  }
  if (key.includes(" · ")) {
    let out = key;
    for (const [rx, rep] of I18N_SUB) out = out.replace(rx, rep);
    return wrap(out);
  }
  return s;
}
const I18N_ATTRS = ["placeholder", "title", "aria-label", "data-label"];
const I18N_SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1 };
// Inline-Auszeichnung, die einen Satz sonst in unübersetzbare Fragmente zerlegt.
const I18N_INLINE = { B: 1, I: 1, EM: 1, STRONG: 1, U: 1, CODE: 1, SMALL: 1, MARK: 1 };
// Enthält das Element NUR Text + Inline-Auszeichnung (kein id-Kind, kein Block)?
// Dann darf sein gesamter Text als Einheit übersetzt werden (Hervorhebung wird
// dabei abgeflacht) — nötig, weil <b>/<code> mitten im Satz sonst Fragmente
// erzeugen, die einzeln nicht im Wörterbuch stehen.
function i18nFlatten(el) {
  let hasInline = false, txt = "";
  for (let c = el.firstChild; c; c = c.nextSibling) {
    if (c.nodeType === 3) { txt += c.data; continue; }
    if (c.nodeType !== 1) return null;
    if (!I18N_INLINE[c.nodeName] || c.id || c.firstElementChild) return null;
    hasInline = true; txt += c.textContent;
  }
  if (!hasInline || !txt.trim()) return null;
  const t = i18nText(txt);
  return t !== txt ? t : null;
}
function i18nTree(root) {
  if (root.nodeType === 3) {
    const p = root.parentNode;
    if (p && I18N_SKIP[p.nodeName]) return;
    const t = i18nText(root.data);
    if (t !== root.data) root.data = t;
    return;
  }
  if (root.nodeType !== 1 || I18N_SKIP[root.nodeName]) return;
  for (const a of I18N_ATTRS) {
    const v = root.getAttribute && root.getAttribute(a);
    if (v) { const t = i18nText(v); if (t !== v) root.setAttribute(a, t); }
  }
  if (root.nodeName === "INPUT" && (root.type === "button" || root.type === "submit") && root.value) {
    const t = i18nText(root.value);
    if (t !== root.value) root.value = t;
  }
  const flat = i18nFlatten(root);
  if (flat !== null) { root.textContent = flat; return; }
  let n = root.firstChild;
  while (n) { const next = n.nextSibling; i18nTree(n); n = next; }
}
if (LANG === "en") {
  document.documentElement.lang = "en";
  document.title = i18nText(document.title);
  i18nTree(document.body);
  new MutationObserver(muts => {
    for (const m of muts) {
      if (m.type === "characterData") i18nTree(m.target);
      else if (m.type === "attributes") {
        // dynamisch gesetzte title/placeholder (z. B. Theme-Knopf) übersetzen
        const el = m.target, v = el.getAttribute(m.attributeName);
        if (v) { const t = i18nText(v); if (t !== v) el.setAttribute(m.attributeName, t); }
      } else m.addedNodes.forEach(n => i18nTree(n));
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true,
                              attributes: true,
                              attributeFilter: ["title", "placeholder", "aria-label"] });
}
</script>
</body>
</html>
"""


def json_for_html(obj):
    """JSON so kodieren, dass es sicher in ein <script>-Tag eingebettet werden
    kann. json.dumps escaped `<`, `>`, `&` und die Zeilentrenner U+2028/U+2029
    nicht – ohne diese Ersetzung könnte ein aus Aria stammender Name wie
    `</script>...` aus dem Script-Tag ausbrechen (Stored XSS)."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def parse_tag_json(raw):
    """vSphere-Tags aus einem JSON-Wert (z. B. der Eigenschaft 'TagJson') lesen.

    Die Struktur unterscheidet sich je nach vROps-/vCenter-Version, deshalb
    werden die üblichen Formen abgefangen:
      ["Standort: RZ-Nord", ...]
      [{"category": "Standort", "name": "RZ-Nord"}, ...]   (auch categoryName/tagName)
      {"Standort": "RZ-Nord", "Umgebung": ["Test", "Prod"]}
      {"tags": [ ... ]}
    Rückgabe: Liste 'Kategorie: Wert' (bzw. nur Wert ohne Kategorie).
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []

    def add(cat, name):
        cat, name = str(cat or "").strip(), str(name or "").strip()
        if not name:
            return
        label = (f"{cat}: {name}"
                 if cat and cat.lower() not in ("tag", "tags", "tagjson") else name)
        if label not in out:
            out.append(label)

    def walk(node, cat=""):
        if isinstance(node, (str, int, float)):
            add(cat, node)
        elif isinstance(node, list):
            for item in node:
                walk(item, cat)
        elif isinstance(node, dict):
            c = (node.get("category") or node.get("categoryName")
                 or node.get("Category") or node.get("category_name") or "")
            n = (node.get("name") or node.get("tagName") or node.get("tag")
                 or node.get("Name") or node.get("value") or "")
            if n:
                add(c or cat, n)
                return
            for k, v in node.items():          # {"Standort": "RZ-Nord", ...}
                walk(v, k)

    walk(data)
    return out


def tag_labels(key, val):
    """Anzeige-Labels einer Tag-Eigenschaft. JSON-Werte (TagJson) werden
    aufgeschlüsselt; sonst wird 'Kategorie: Wert' aus Schlüssel/Wert gebildet."""
    s = str(val or "").strip()
    if not s:
        return []
    if s[:1] in ("[", "{"):
        # Rohes JSON niemals als Chip anzeigen – lieber nichts.
        return parse_tag_json(s)
    cat = (key.split(":")[-1] if ":" in key else key.split("|")[-1]).strip()
    return [s if not cat or cat.lower() in ("tag", "tags", "tagjson")
            else f"{cat}: {s}"]


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


def _html_escape(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


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
            route = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if route in ("/", "/index.html", "/reservierungen",
                         "/genehmigungen", "/archiv", "/statistik",
                         "/verwaltung", "/log"):
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
            elif route == "/api/data":
                s = self._require()
                if not s:
                    return
                self._json({"updated": state["updated"],
                            "clusters": clusters_for(s)})
            elif route == "/api/status":
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
            elif self.path == "/api/reservations":
                s = self._require()
                if not s:
                    return
                with res_lock:
                    prune_reservations()
                    self._json(visible_res(s))
            elif route == "/api/v1/openapi.json":
                self._send(json.dumps(openapi_spec(self._lang()), ensure_ascii=False, indent=2),
                           "application/json; charset=utf-8")
            elif route in ("/api/v1/docs", "/api/v1/docs/"):
                self._send(API_DOCS_HTML.replace("__VERSION__", VERSION),
                           "text/html; charset=utf-8")
            elif route in ("/reviewer-handbuch", "/reviewer-handbuch/",
                           "/reviewer-handbook"):
                # Reviewer-Handbuch (eigene, zweisprachige Doku-Seite, im UI
                # verlinkt; später erweiterbar).
                self._send(reviewer_doc_html(self._lang(), VERSION,
                                             role_names.get("reviewer") or "Reviewer"),
                           "text/html; charset=utf-8")
            elif route == "/healthz":
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
            elif route in ("/api/v1/reservations", "/api/v1/data",
                           "/api/v1/status", "/api/v1/storage-requests"):
                # Stabile v1-API für externe Anwendungen: Bearer-Token oder Session
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
            elif route == "/api/tokens":
                if not self._require("admin"):
                    return
                with tokens_lock:
                    self._json(token_list())
            elif route == "/api/roles":
                if not self._require("admin"):
                    return
                self._json(roles_with_mail())
            elif route == "/api/teams":
                if not self._require("admin"):
                    return
                with teams_lock:
                    self._json({"teams": list(approval_teams)})
            elif route == "/api/selector":
                if not self._require("admin"):
                    return
                with selector_lock:
                    self._json({"selector": list(cluster_selector)})
            elif route == "/api/rolenames":
                if not self._require("admin"):
                    return
                with rolenames_lock:
                    self._json({"rolenames": dict(role_names)})
            elif route == "/api/notify":
                if not self._require("admin"):
                    return
                with notify_lock:
                    self._json({"notify": json.loads(json.dumps(notify_cfg)),
                                "default_template": DEFAULT_MAIL_TEMPLATE,
                                "default_subject": DEFAULT_MAIL_SUBJECT,
                                "vars": MAIL_VARS})
            elif route == "/api/webhooks":
                if not self._require("admin"):
                    return
                # Secret NIE im Klartext an den Browser – nur ob eins gesetzt ist.
                with webhooks_lock:
                    eps = [{"id": e["id"], "url": e["url"], "events": e["events"],
                            "active": e["active"], "desc": e["desc"],
                            "has_secret": bool(e["secret"])} for e in webhook_cfg]
                self._json({"endpoints": eps, "events": list(WEBHOOK_EVENTS)})
            elif route == "/api/announce":
                if not self._require("admin"):
                    return
                with announce_lock:
                    self._json({"announce": json.loads(json.dumps(announce_cfg))})
            elif route == "/api/autoapprove":
                if not self._require("admin"):
                    return
                with autoapprove_lock:
                    self._json({"autoapprove": json.loads(json.dumps(autoapprove_cfg))})
            elif route == "/api/visibility":
                if not self._require("admin"):
                    return
                with visibility_lock:
                    self._json({"visibility": json.loads(json.dumps(visibility_cfg)),
                                "features": list(VIS_FEATURES),
                                "roles": list(VIS_ROLES)})
            elif route == "/api/storage-requests":
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
            elif route == "/api/storagecfg":
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
            elif route == "/api/netcfg":
                if not self._require("admin"):
                    return
                with net_lock:
                    self._json({"exclude_names": ", ".join(
                                    net_cfg.get("exclude_names") or []),
                                "exclude_vlans": ", ".join(
                                    net_cfg.get("exclude_vlans") or [])})
            elif route == "/api/import":
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
            elif route == "/api/import/reservations/beispiel":
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
            elif route == "/api/import/powercli":
                if not self._require("admin"):
                    return
                self._send(POWERCLI_PS1, "text/plain; charset=utf-8", headers={
                    "Content-Disposition":
                        'attachment; filename="kapa_export.ps1"'})
            elif route == "/api/refreshcfg":
                if not self._require("admin"):
                    return
                with refresh_lock:
                    self._json({"tiers": dict(refresh_cfg),
                                "default_min": max(1, interval // 60),
                                "tz": refresh_cfg.get("tz", ""),
                                "now": datetime.now().strftime("%d.%m.%Y %H:%M")})
            elif route == "/api/history":
                # Statistik-Historie (Trends). Sichtbarkeit per Matrix-Feature
                # „statistik" (Admin/Betrieb ohne Anmeldung sehen immer alles).
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
            elif route == "/api/config":
                if not self._require("admin"):
                    return
                self._json({"config": public_config()})
            elif route == "/api/prefs":
                s = self._require()
                if not s:
                    return
                self._json(user_prefs(s["user"]))
            elif route == "/api/log":
                if not self._require("admin"):
                    return
                self._json(read_log(1500))
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/api/storage-request":
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
            elif self.path == "/api/webhooks/test":
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
            elif self.path == "/api/login":
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
            elif self.path == "/api/logout":
                old = sessions.pop(_sess_key(self._cookie_token()), None)
                if old:
                    save_sessions()
                    audit(old["user"], "Abmeldung")
                secure = "" if args.cookie_insecure else " Secure;"
                self._json({"ok": True},
                           headers={"Set-Cookie": "kapa_session=; Max-Age=0; "
                                    f"HttpOnly;{secure} SameSite=Lax; Path=/"})
            elif self.path == "/api/refresh":
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
            elif self.path == "/api/v1/reservations":
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
            elif (self.path.startswith("/api/v1/reservations/")
                    and self.path.endswith(("/approve", "/reject", "/cancel"))):
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
            elif (self.path.startswith("/api/v1/storage-requests/")
                    and self.path.endswith("/done")):
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
            elif self.path == "/api/reservations":
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
            elif (self.path.startswith("/api/reservations/")
                    and self.path.endswith(("/approve", "/reject"))):
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
            elif (self.path.startswith("/api/reservations/")
                    and self.path.endswith("/cancel")):
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
            elif self.path == "/api/backup":
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
            elif self.path == "/api/tokens":
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
            elif self.path == "/api/roles":
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
            elif self.path == "/api/import":
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
            elif self.path == "/api/import/reservations":
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
            elif self.path == "/api/ad/group-members":
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
            elif self.path == "/api/teams/rename":
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
            else:
                self.send_error(404)

        def do_PUT(self):
            if self.path == "/api/reservations":
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
            elif self.path == "/api/teams":
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
            elif self.path == "/api/selector":
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
            elif self.path == "/api/rolenames":
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
            elif self.path == "/api/webhooks":
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
            elif self.path == "/api/notify":
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
            elif self.path == "/api/visibility":
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
            elif self.path == "/api/storagecfg":
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
            elif self.path == "/api/netcfg":
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
            elif self.path == "/api/refreshcfg":
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
            elif self.path.startswith("/api/storage-request/"):
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
            elif self.path == "/api/autoapprove":
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
            elif self.path == "/api/announce":
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
            elif self.path == "/api/mail-preview":
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
            elif self.path.startswith("/api/tokens/"):
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
            elif self.path == "/api/prefs":
                s = self._require()
                if not s:
                    return
                clean = clean_prefs(self._body())
                with prefs_lock:
                    all_prefs[s["user"] or ""] = clean
                    store.save("prefs", all_prefs)
                self._json(clean)
            else:
                self.send_error(404)

        def do_DELETE(self):
            if self.path.startswith("/api/reservations/"):
                # Löschen von Reservierungsanfragen ist deaktiviert – Anträge
                # laufen über Genehmigung/Ablehnung und den automatischen Ablauf.
                self._json({"error": "Das Löschen von Reservierungen ist "
                                     "deaktiviert."}, 403)
            elif self.path.startswith("/api/tokens/"):
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
            elif self.path.startswith("/api/roles/"):
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
            elif self.path.startswith("/api/import/"):
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
            elif self.path.startswith("/api/storage-request/"):
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
            else:
                self.send_error(404)

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
