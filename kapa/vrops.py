#!/usr/bin/env python3
"""Aria Operations Suite-API-Client (Login, Ressourcen, Properties,
Metriken im Bulk, Tags) — nur Standardbibliothek.
Aus aria_kapa.py ausgelagert (v3.0)."""
import json
import ssl
import urllib.parse
import urllib.request


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
        # URL stammt aus der Admin-Konfiguration (vROps, https)
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        return urllib.request.urlopen(req, context=self.ctx, timeout=timeout)  # nosec B310 – URL stammt aus der Admin-Konfiguration (vROps, https), nicht aus Nutzereingaben

    def _request(self, method, path, body=None, params=None):
        url = self.base + path  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
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
            req = urllib.request.Request(url)  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
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
