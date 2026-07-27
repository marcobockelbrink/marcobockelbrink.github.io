#!/usr/bin/env python3
"""Echter LDAPS-Test gegen das Test-AD (tests/ad/, Samba DC KAPA.LOCAL) —
prüft GENAU die vier Funktionen, die der Anmelde-Pfad des Dashboards nutzt:
ldap_bind (UPN!), ldap_member_of, ldap_user_attr, ldap_group_members.

Opt-in: braucht den laufenden Container (docker compose up -d in tests/ad).

    KAPA_TEST_AD=ldaps://localhost:6360 python3 tests/ldap_live.py
    (ohne Umgebungsvariable wird ldaps://localhost:6360 angenommen)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kapa.ldap import ldap_bind, ldap_member_of, ldap_user_attr, \
    ldap_group_members                                   # noqa: E402

URL = os.environ.get("KAPA_TEST_AD", "ldaps://localhost:6360")
BASE_DN = "DC=kapa,DC=local"
BIND_DN = "Administrator@kapa.local"
BIND_PW = "Kapa-Admin1!"

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


print(f"== Live-LDAPS gegen {URL} (selbstsigniert -> insecure) ==")

# 1) Simple Bind mit UPN — exakt wie der Dashboard-Login
check("UPN-Bind mit korrektem Passwort",
      ldap_bind(URL, "anna.schmidt@kapa.local", "Kapa-Test1!", insecure=True))
check("UPN-Bind mit falschem Passwort wird abgelehnt",
      not ldap_bind(URL, "anna.schmidt@kapa.local", "falsch!", insecure=True))
check("Leeres Passwort wird abgelehnt (kein anonymer Bind)",
      not ldap_bind(URL, "anna.schmidt@kapa.local", "", insecure=True))
check("Unbekannter Nutzer wird abgelehnt",
      not ldap_bind(URL, "gibtsnicht@kapa.local", "Kapa-Test1!", insecure=True))

# 2) memberOf — Grundlage der Rollen-Zuordnung über AD-Gruppen
cns = ldap_member_of(URL, BIND_DN, BIND_PW, BASE_DN,
                     "anna.schmidt@kapa.local", insecure=True)
check("memberOf: anna.schmidt ist in KapaAdmins",
      "KapaAdmins" in cns, str(cns))
cns2 = ldap_member_of(URL, BIND_DN, BIND_PW, BASE_DN,
                      "jan.krause@kapa.local", insecure=True)
check("memberOf: jan.krause ist in KapaReviewer (und nicht in KapaAdmins)",
      "KapaReviewer" in cns2 and "KapaAdmins" not in cns2, str(cns2))

# 3) Attribut-Abfrage — Empfänger-Mail aus dem AD (--ad-mail-attribute)
mail = ldap_user_attr(URL, BIND_DN, BIND_PW, BASE_DN,
                      "anna.schmidt@kapa.local", "mail", insecure=True)
check("Mail-Attribut wird gelesen", mail == "anna.schmidt@kapa.local", mail)

# 4) Gruppen-Mitglieder — der AD-Gruppen-Check in der Verwaltung
r = ldap_group_members(URL, BIND_DN, BIND_PW, BASE_DN, "KapaAdmins",
                       insecure=True)
check("Gruppen-Mitglieder: KapaAdmins enthaelt anna.schmidt",
      r.get("ok") and any(m.get("upn") == "anna.schmidt@kapa.local"
                          for m in r.get("members") or []), str(r)[:200])
r2 = ldap_group_members(URL, BIND_DN, BIND_PW, BASE_DN, "GibtEsNicht",
                        insecure=True)
check("Unbekannte Gruppe liefert sauberen Fehler statt Crash",
      not r2.get("ok") and r2.get("error"), str(r2)[:120])

print(f"\n{PASS} ok, {len(FAIL)} fehlgeschlagen"
      + (": " + ", ".join(FAIL) if FAIL else " — alles grün ✅"))
sys.exit(1 if FAIL else 0)
