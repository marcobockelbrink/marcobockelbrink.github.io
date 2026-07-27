# Test-AD für echte LDAPS-Tests (Entwicklung)

Ein echter **Samba Active Directory Domain Controller** im Container —
Domäne `KAPA.LOCAL`, LDAPS auf `ldaps://localhost:6360` (selbstsigniert).

Warum ein echtes AD statt OpenLDAP? Die Kapa-Anmeldung nutzt AD-Eigenheiten,
die plain LDAP nicht kann: **Simple Bind mit UPN** (`user@kapa.local`),
**memberOf** am Benutzer und **objectCategory**-Filter. Nur gegen ein echtes
AD ist der Test aussagekräftig. Bewusst selbst gebaut auf `debian:12-slim`
(kein Dritt-Image).

## Verwendung

```bash
cd tests/ad
docker compose up -d        # Erststart provisioniert die Domäne (~1 Minute)
cd ../..
python3 tests/ldap_live.py  # echter LDAPS-Test (Bind/memberOf/Attribute/Gruppen)
```

Aufräumen (löscht auch die Domäne): `docker compose down -v`

## Testdaten (nur Entwicklung — bewusst simple Passwörter)

| Konto | Passwort | Gruppen |
|---|---|---|
| `Administrator@kapa.local` | `Kapa-Admin1!` | (Domänen-Admin, dient als Bind-Konto) |
| `anna.schmidt@kapa.local` | `Kapa-Test1!` | KapaAdmins |
| `jan.krause@kapa.local` | `Kapa-Test1!` | KapaReviewer |

Beide Nutzer haben ein `mail`-Attribut (für `--ad-mail-attribute`-Tests).

## Dashboard dagegen laufen lassen

```bash
python3 aria_kapa.py --serve --sample \
  --ad-url ldaps://localhost:6360 --ad-insecure \
  --ad-bind-dn 'Administrator@kapa.local' \
  --ad-base-dn 'DC=kapa,DC=local' \
  --admin-user anna.schmidt@kapa.local
```

(`--ad-insecure`, weil das Zertifikat selbstsigniert ist. Das Bind-Passwort
fragt der Start ab bzw. kommt aus einer `.pass`-Datei.)
