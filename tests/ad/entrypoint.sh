#!/bin/sh
# Erststart: Domäne KAPA.LOCAL provisionieren (Samba erzeugt dabei auch
# selbstsignierte TLS-Zertifikate für LDAPS), danach Testnutzer/-gruppen
# anlegen. Folgestarts: nur noch Samba starten.
set -e

if [ ! -f /var/lib/samba/private/sam.ldb ]; then
    echo "== Provisioniere Test-Domaene KAPA.LOCAL ..."
    rm -f /etc/samba/smb.conf
    samba-tool domain provision \
        --realm=KAPA.LOCAL --domain=KAPA --server-role=dc \
        --dns-backend=SAMBA_INTERNAL --adminpass='Kapa-Admin1!'
    cp /var/lib/samba/private/krb5.conf /etc/krb5.conf || true
fi

samba --foreground --no-process-group &
SAMBA_PID=$!

if [ ! -f /var/lib/samba/.kapa-seeded ]; then
    # warten, bis LDAP antwortet, dann Testdaten anlegen
    i=0
    until samba-tool user list >/dev/null 2>&1 || [ $i -ge 30 ]; do
        i=$((i + 1)); sleep 2
    done
    echo "== Lege Testnutzer und -gruppen an ..."
    samba-tool user create anna.schmidt 'Kapa-Test1!' \
        --mail-address=anna.schmidt@kapa.local --given-name=Anna \
        --surname=Schmidt
    samba-tool user create jan.krause 'Kapa-Test1!' \
        --mail-address=jan.krause@kapa.local --given-name=Jan \
        --surname=Krause
    samba-tool group add KapaAdmins
    samba-tool group add KapaReviewer
    samba-tool group addmembers KapaAdmins anna.schmidt
    samba-tool group addmembers KapaReviewer jan.krause
    touch /var/lib/samba/.kapa-seeded
    echo "== Test-AD bereit: ldaps://localhost:6360 (KAPA.LOCAL)"
fi

wait $SAMBA_PID
