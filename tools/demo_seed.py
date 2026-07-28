#!/usr/bin/env python3
"""Reproduzierbarer Demo-Seed für die Doku-Screenshots.

Befüllt eine laufende --sample-Instanz mit Teams, AD-Gruppen, Tokens und einem
realistischen Satz Kapazitätsanfragen (mit generischen JIRA-Beispiel-Nummern,
KEINE echten Change-Präfixe). Danach lassen sich die Screenshots für
Reservierungen, Genehmigungen und Log erzeugen.

    python3 aria_kapa.py --serve --sample --port 8085 --data-dir /tmp/seed &
    python3 tools/demo_seed.py http://127.0.0.1:8085

Reihenfolge ist so gewählt, dass das Audit-Log am Ende eine schöne, sprechende
Historie zeigt (Rollen -> Tokens -> Import -> Antrag angelegt/freigegeben).
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8085").rstrip("/")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                                 headers={"Content-Type": "application/json"})
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # Dev-Werkzeug: Ziel ist der lokale Testserver
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else None


TEAMS = ["Team Betrieb", "Team Netzwerk", "Team Security"]


def ap(team, by, on):
    return {"team": team, "by": by, "on": on}


# 6 Reservierungen – generische JIRA-Beispiel-Tickets (OPS-, INFRA-, MON- …)
RES = [
    {"name": "Datenbank-Cluster PostgreSQL", "cluster": "Cluster-01",
     "change": "DB-1042", "vcpu": 24, "ram_gb": 192, "storage_gb": 3000,
     "von": "anna.schmidt@firma.local", "abteilung": "Team Betrieb",
     "created": "2026-07-12", "approved": True, "approved_by": "thomas.weber@firma.local",
     "approvals": [ap("Team Betrieb", "thomas.weber", "2026-07-13"),
                   ap("Team Netzwerk", "jan.krause", "2026-07-14"),
                   ap("Team Security", "lisa.brandt", "2026-07-16")]},
    {"name": "SAP HANA Erweiterung Q3", "cluster": "Cluster-01",
     "change": "OPS-2087", "vcpu": 32, "ram_gb": 256, "storage_gb": 2000,
     "von": "anna.schmidt@firma.local", "abteilung": "Team Betrieb",
     "created": "2026-07-19", "approvals": []},
    {"name": "Monitoring-Ausbau Prometheus", "cluster": "Cluster-01",
     "change": "MON-334", "vcpu": 12, "ram_gb": 96, "storage_gb": 1500,
     "von": "lisa.brandt@firma.local", "abteilung": "Team Security",
     "created": "2026-07-20", "approvals": []},
    {"name": "Fileservice Migration Welle 2", "cluster": "Cluster-02",
     "change": "INFRA-1571", "vcpu": 20, "ram_gb": 160, "storage_gb": 12000,
     "von": "jan.krause@firma.local", "abteilung": "Team Netzwerk",
     "created": "2026-07-13",
     "approvals": [ap("Team Betrieb", "thomas.weber", "2026-07-14"),
                   ap("Team Netzwerk", "jan.krause", "2026-07-15")]},
    {"name": "VDI-Ausbau Standort Nord", "cluster": "Cluster-02",
     "change": "VDI-909", "vcpu": 48, "ram_gb": 384, "storage_gb": 4000,
     "von": "jan.krause@firma.local", "abteilung": "Team Netzwerk",
     "created": "2026-07-17",
     "approvals": [ap("Team Betrieb", "thomas.weber", "2026-07-18")]},
    {"name": "Log-Management (SIEM) Stage", "cluster": "Cluster-03",
     "change": "SEC-4102", "vcpu": 16, "ram_gb": 128, "storage_gb": 6000,
     "von": "lisa.brandt@firma.local", "abteilung": "Team Security",
     "created": "2026-07-15",
     "approvals": [ap("Team Betrieb", "thomas.weber", "2026-07-16"),
                   ap("Team Netzwerk", "jan.krause", "2026-07-17")]},
]


def main():
    call("PUT", "/api/teams", TEAMS)
    # Rollen / AD-Gruppen (fürs Audit-Log)
    call("POST", "/api/roles", {"user": "revision@firma.local", "role": "auditor"})
    for user, role, dept in [
            ("G-VMware-Admins", "admin", ""),
            ("G-Kapa-Anforderer-Betrieb", "anforderer", "Team Betrieb"),
            ("G-Kapa-Reviewer-Betrieb", "reviewer", "Team Betrieb"),
            ("G-Revision", "auditor", "")]:
        call("POST", "/api/roles",
             {"user": user, "role": role, "kind": "group", "abteilung": dept})
    # API-Tokens (fürs Audit-Log)
    call("POST", "/api/tokens", {"name": "CMDB-Sync"})
    call("POST", "/api/tokens", {"name": "Grafana-Dashboard"})
    # Reservierungen importieren (ersetzt Bestand)
    call("PUT", "/api/reservations", RES)
    print(f"Import: {len(RES)} Reservierungen, Teams {TEAMS}")
    print("Jetzt Reservierungen/Genehmigungen screenshotten.")
    # Ein Antrag per Anlage + Freigabe -> erzeugt sprechende Log-Einträge
    r = call("POST", "/api/reservations",
             {"name": "K8s Ingress-Knoten", "cluster": "Cluster-03",
              "change": "NET-2203", "vcpu": 8, "ram_gb": 32, "storage_gb": 200})
    rid = None
    if isinstance(r, list) and r:
        rid = next((x["id"] for x in r if x.get("name") == "K8s Ingress-Knoten"), None)
    if rid:
        call("POST", f"/api/reservations/{rid}/approve", {"comment": "passt"})
        print(f"K8s-Antrag {rid} angelegt + einmal freigegeben (fürs Log).")
    print("Fertig.")


if __name__ == "__main__":
    main()
