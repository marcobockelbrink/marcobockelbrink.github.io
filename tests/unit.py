#!/usr/bin/env python3
"""Unit-Tests für den Rechenkern und die reinen Helfer der kapa/-Module —
nur Standardbibliothek (unittest), kein Server nötig.

    python3 tests/unit.py

Die Smoke-Suite (tests/smoke.py) ruft diese Datei mit auf; einzeln laufen
lassen geht jederzeit. Getestet wird die MATHEMATIK, der die Kapazitäts-
zahlen im Dashboard vertrauen — nicht das HTTP-Verhalten (das macht smoke).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kapa.collect import build_summary, is_uplink_pg, _name_excluded, \
    _vlan_excluded, parse_tag_json                      # noqa: E402
from kapa.mail import render_template, _mail_values     # noqa: E402
from kapa.util import _html_escape                      # noqa: E402
from kapa.dashboard import json_for_html                # noqa: E402
from kapa.store import JsonStore, SqliteStore, atomic_write  # noqa: E402


def _cluster(hosts, vms, namespaces=None, storage=None):
    """Minimal-Rohdaten im collect()-Format für build_summary."""
    return {"hosts": hosts, "vms": vms,
            "namespaces": namespaces or [],
            "storage": storage or {"cap_gb": 0.0, "used_gb": 0.0, "luns": []},
            "tags": {}, "portgroups": [], "workload": None}


class TestKapazitaetsMathe(unittest.TestCase):
    """build_summary: die Kernrechnung des ganzen Dashboards."""

    def test_grundrechnung_vcpu_ram(self):
        data = {"C1": _cluster(
            hosts=[{"name": "h1", "cores": 32, "ram_gb": 512.0},
                   {"name": "h2", "cores": 32, "ram_gb": 512.0}],
            vms=[{"name": "v1", "vcpu": 8, "ram_gb": 64.0, "on": True},
                 {"name": "v2", "vcpu": 4, "ram_gb": 32.0, "on": False}])}
        c = build_summary(data, cpu_factor=6, failover_hosts=0)[0]
        self.assertEqual(c["cores"], 64)
        self.assertEqual(c["vcpuCap"], 64 * 6)
        # ausgeschaltete VMs zählen mit (provisioniert ist provisioniert)
        self.assertEqual(c["vcpuUsed"], 12)
        self.assertEqual(c["vcpuFree"], 64 * 6 - 12)
        self.assertEqual(c["ramCap"], 1024.0)
        self.assertEqual(c["ramUsed"], 96.0)
        self.assertEqual(c["ramFree"], 928.0)

    def test_n_plus_1_zieht_groessten_host_ab(self):
        data = {"C1": _cluster(
            hosts=[{"name": "klein", "cores": 16, "ram_gb": 256.0},
                   {"name": "GROSS", "cores": 64, "ram_gb": 1024.0},
                   {"name": "mittel", "cores": 32, "ram_gb": 512.0}],
            vms=[])}
        c = build_summary(data, cpu_factor=6, failover_hosts=1)[0]
        self.assertEqual(c["spareCores"], 64)          # der größte
        self.assertEqual(c["spareRamGb"], 1024.0)
        self.assertEqual(c["cores"], 16 + 32)
        self.assertEqual(c["ramCap"], 256.0 + 512.0)

    def test_n_plus_2_und_untergrenze_ein_host_bleibt(self):
        data = {"C1": _cluster(
            hosts=[{"name": "h1", "cores": 10, "ram_gb": 100.0},
                   {"name": "h2", "cores": 20, "ram_gb": 200.0}],
            vms=[])}
        # failover_hosts=5, aber mindestens EIN Host bleibt übrig
        c = build_summary(data, cpu_factor=6, failover_hosts=5)[0]
        self.assertEqual(c["spareCores"], 20)          # nur 1 abgezogen
        self.assertEqual(c["cores"], 10)

    def test_tanzu_mhz_zu_vcpu_aufgerundet(self):
        data = {"C1": _cluster(
            hosts=[{"name": "h1", "cores": 32, "ram_gb": 512.0}],
            vms=[],
            namespaces=[{"name": "ns1", "cpu_mhz": 5000, "ram_gb": 16.0},
                        {"name": "ns2", "cpu_mhz": 2500, "ram_gb": 8.0}])}
        c = build_summary(data, cpu_factor=6, failover_hosts=0,
                          tanzu_mhz=2500)[0]
        # 5000/2500=2, 2500/2500=1 -> 3; 5001 MHz wären ceil() -> 3 je ns
        self.assertEqual(c["tanzuVcpu"], 3)
        self.assertEqual(c["tanzuRamGb"], 24.0)
        # tanzu_mhz=0: CPU bewusst nicht zählen
        c0 = build_summary(data, cpu_factor=6, failover_hosts=0,
                           tanzu_mhz=0)[0]
        self.assertEqual(c0["tanzuVcpu"], 0)

    def test_tanzu_aufrundung_krumm(self):
        data = {"C1": _cluster(
            hosts=[{"name": "h1", "cores": 8, "ram_gb": 64.0}], vms=[],
            namespaces=[{"name": "ns", "cpu_mhz": 2501, "ram_gb": 1.0}])}
        c = build_summary(data, cpu_factor=6, failover_hosts=0,
                          tanzu_mhz=2500)[0]
        self.assertEqual(c["tanzuVcpu"], 2)            # aufgerundet, nie ab

    def test_min_lun_filter_nutzt_brutto(self):
        # cap_gb ist ggf. schon vSAN-faktorisiert; die Mindestgröße prüft
        # gegen raw_cap_gb (brutto), damit der Faktor den Filter nicht kippt.
        storage = {"cap_gb": 0, "used_gb": 0, "luns": [
            {"name": "klein", "cap_gb": 400.0, "raw_cap_gb": 400.0,
             "used_gb": 100.0},
            {"name": "vsan-gross", "cap_gb": 500.0, "raw_cap_gb": 1000.0,
             "used_gb": 200.0}]}
        data = {"C1": _cluster(
            hosts=[{"name": "h1", "cores": 8, "ram_gb": 64.0}], vms=[],
            storage=storage)}
        c = build_summary(data, cpu_factor=6, failover_hosts=0,
                          min_lun_gb=500)[0]
        self.assertEqual([l["name"] for l in c["datastores"]],
                         ["vsan-gross"])
        self.assertEqual(c["storageCap"], 500.0)       # faktorisierte Kap.
        self.assertEqual(c["storageUsed"], 200.0)

    def test_namensfilter_wildcards(self):
        storage = {"cap_gb": 0, "used_gb": 0, "luns": [
            {"name": "prod-lun-01", "cap_gb": 100.0, "used_gb": 10.0},
            {"name": "backup-lun", "cap_gb": 100.0, "used_gb": 10.0},
            {"name": "ISO_store", "cap_gb": 100.0, "used_gb": 10.0}]}
        data = {"C1": _cluster(
            hosts=[{"name": "h1", "cores": 8, "ram_gb": 64.0}], vms=[],
            storage=storage)}
        c = build_summary(data, cpu_factor=6, failover_hosts=0,
                          exclude_names=["backup", "iso*"])[0]
        self.assertEqual([l["name"] for l in c["datastores"]],
                         ["prod-lun-01"])
        self.assertEqual(c["storageCap"], 100.0)


class TestFilterHelfer(unittest.TestCase):
    def test_name_excluded(self):
        self.assertTrue(_name_excluded("Backup-LUN-7", ["backup"]))
        self.assertTrue(_name_excluded("iso_images", ["iso*"]))
        self.assertTrue(_name_excluded("ds-temp-01", ["*temp*"]))
        self.assertFalse(_name_excluded("prod-01", ["backup", "iso"]))
        self.assertFalse(_name_excluded("prod-01", []))

    def test_vlan_excluded(self):
        self.assertTrue(_vlan_excluded("100", ["100", "200"]))
        self.assertFalse(_vlan_excluded("150", ["100", "200"]))

    def test_uplink_erkennung(self):
        self.assertTrue(is_uplink_pg("dvs-DVUplinks-42", "0-4094"))
        self.assertFalse(is_uplink_pg("pg-prod-100", "100"))

    def test_parse_tag_json(self):
        # Rückgabe ist eine Liste "Kategorie: Wert" (versionsabhängige
        # Eingabeformen werden vereinheitlicht).
        raw = json.dumps([{"category": "Umgebung", "name": "Prod"},
                          {"category": "Standort", "name": "RZ-Nord"}])
        tags = parse_tag_json(raw)
        self.assertIn("Umgebung: Prod", tags)
        self.assertIn("Standort: RZ-Nord", tags)
        self.assertEqual(parse_tag_json("kein json"), [])


class TestMailRendering(unittest.TestCase):
    RES = {"id": "KAPA-test01", "name": "Testprojekt", "cluster": "C1",
           "change": "OPS-1", "vcpu": 8, "ram_gb": 64, "storage_gb": 500,
           "von": "user@firma.local", "abteilung": "Team A",
           "created": "2026-07-01", "approvals": []}

    def test_platzhalter_ersetzt(self):
        vals = _mail_values(self.RES, "genehmigt", "admin", 31, html=False)
        out = render_template("{{id}}/{{cluster}}/{{action}}", vals)
        self.assertEqual(out, "KAPA-test01/C1/genehmigt")

    def test_html_werte_escaped(self):
        boese = dict(self.RES, name="<script>alert(1)</script>")
        vals = _mail_values(boese, "genehmigt", "admin", 31, html=True)
        out = render_template("{{name}}", vals)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_unbekannter_platzhalter_bleibt_harmlos(self):
        vals = _mail_values(self.RES, "genehmigt", "admin", 31, html=False)
        out = render_template("x {{gibtsnicht}} y", vals)
        self.assertIn("x ", out)
        self.assertIn(" y", out)


class TestEscaping(unittest.TestCase):
    def test_html_escape(self):
        self.assertEqual(_html_escape('<b>&"'), "&lt;b&gt;&amp;\"")
        self.assertEqual(_html_escape(None), "")

    def test_json_for_html_bricht_nicht_aus_script(self):
        out = json_for_html({"n": "</script><script>alert(1)</script>"})
        self.assertNotIn("</script>", out)
        self.assertNotIn(" ", json_for_html({"n": "a b"}))


class TestStore(unittest.TestCase):
    def test_jsonstore_roundtrip_atomar(self):
        d = tempfile.mkdtemp(prefix="kapa_unit_")
        try:
            p = os.path.join(d, "kapa_x.json")
            st = JsonStore({"x": p})
            st.save("x", {"a": 1, "u": "ä"})
            self.assertEqual(st.load("x", None), {"a": 1, "u": "ä"})
            # atomar: keine tmp-Reste neben der Datei
            self.assertEqual([f for f in os.listdir(d)
                              if not f.endswith(".json")], [])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_sqlitestore_roundtrip_und_reservierungen(self):
        d = tempfile.mkdtemp(prefix="kapa_unit_")
        try:
            st = SqliteStore(os.path.join(d, "kapa.db"))
            st.save("roles", {"u1": {"role": "admin"}})
            self.assertEqual(st.load("roles", None), {"u1": {"role": "admin"}})
            items = [{"id": "K1", "name": "A"}, {"id": "K2", "name": "B"}]
            st.res_put(items[0], items)
            st.res_put(items[1], items)
            st.res_delete(["K1"], [items[1]])
            self.assertEqual([r["id"] for r in st.res_load()], ["K2"])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_atomic_write_ersetzt_inhalt(self):
        d = tempfile.mkdtemp(prefix="kapa_unit_")
        try:
            p = os.path.join(d, "f.txt")
            atomic_write(p, "eins")
            atomic_write(p, "zwei")
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), "zwei")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
