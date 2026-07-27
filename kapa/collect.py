#!/usr/bin/env python3
"""Datenabfrage aus vROps (collect), Zusammenfassung je Cluster
(build_summary), Uplink-/Namensfilter und die Demo-Daten
(sample_data). Aus aria_kapa.py ausgelagert (v3.0)."""
import re
import sys


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
