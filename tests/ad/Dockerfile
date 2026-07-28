# Test-AD für die Kapa-Entwicklung: ein echter Samba Active Directory
# Domain Controller (Domäne KAPA.LOCAL) mit LDAPS auf Port 636.
#
# Bewusst SELBST gebaut auf dem offiziellen Debian-Image statt eines
# Dritt-Images vom Docker Hub — passt zur Pinning-/Provenance-Linie des
# Projekts. Nur für Entwicklung/Tests, nicht für Produktion.
#
#   docker compose up -d          (erster Start provisioniert ~1 Minute)
#   python3 tests/ldap_live.py    (echter LDAPS-Test gegen den Container)
# Digest-gepinnt (Scorecard/Reproduzierbarkeit); Container läuft bewusst
# als root — ein Samba-AD-DC braucht das (NT-ACLs, Ports, winbind).
FROM debian:12-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        samba samba-ad-provision samba-dsdb-modules samba-vfs-modules \
        winbind krb5-config acl attr \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 636
ENTRYPOINT ["/entrypoint.sh"]
