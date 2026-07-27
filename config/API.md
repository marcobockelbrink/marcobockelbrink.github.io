# API für externe Anwendungen (v1)

> 🇬🇧 [English version: API.en.md](API.en.md)

> **Interaktiv im Dashboard:** `/api/v1/docs` (Swagger-artige Seite mit
> „Ausführen", offline lauffähig) · **OpenAPI-Spec:** `/api/v1/openapi.json`
> (Import in Swagger Editor/Postman). Diese Datei ist die Textfassung.

Das Dashboard stellt unter `/api/v1/` seine API bereit — seit v3.0 **ein
Pfadraum für alles**: Die hier beschriebenen Endpunkte bilden den stabilen
Vertrag für externe Anwendungen (**Bearer-Token**); zusätzlich bedient
derselbe Pfadraum mit Session-Cookie die komplette Weboberfläche
(Verwaltungs-Endpunkte bewusst nur mit Anmeldung, nie per Token).
Bearer-Tokens erzeugen Admins im Reiter „Verwaltung → API". Das Token wird
nur einmal angezeigt; gespeichert wird ausschließlich ein SHA-256-Hash.
Tokens sind einzeln widerrufbar; jede Erstellung/Widerruf und ungültige
Zugriffe landen im Audit-Log.

```bash
TOKEN="kapa_..."
BASE="https://host/capa"     # bzw. http://localhost:8080 ohne Proxy

curl -H "Authorization: Bearer $TOKEN" $BASE/api/v1/reservations
```

## Endpunkte

### GET /api/v1/reservations

Alle Reservierungen (ungefiltert, wie die Admin-Sicht). Felder je Eintrag:

| Feld | Bedeutung |
|---|---|
| `id` | Eindeutige ID |
| `name` | Bezeichnung / Projekt |
| `change` | Change-Nummer / Jira-Ticket (z. B. `OPS-4711`, `INFRA-1042`) |
| `cluster` | Ziel-Cluster |
| `vcpu`, `ram_gb`, `storage_gb` | Angefragte Kapazität (Storage nur informativ) |
| `von`, `abteilung` | Anforderer und Abteilung |
| `created` | Gilt ab (Anlagetag, ISO-Datum) |
| `approvals` | Liste der bisherigen Team-Freigaben: `[{team, by, on, comment}]` (Reihenfolge = Prüfreihenfolge) |
| `approved`, `approved_on`, `approved_by` | vollständig genehmigt (alle Stufen), Datum + letzter Freigebender |
| `rejected`, `rejected_on`, `rejected_by`, `rejected_team` | Ablehnungsstatus inkl. Stufe |
| `cancelled`, `cancelled_on`, `cancelled_by` | Storno (durch Abteilung/Anforderer/Admin) |
| `comment` | letzter Kommentar |

Der abgeleitete Status ergibt sich als: `abgelehnt` (rejected), sonst
`storniert` (cancelled), sonst `genehmigt` (approved), sonst `in Prüfung`
(mindestens eine, aber nicht alle Freigaben), sonst `beantragt`.

**Filter** (kombinierbar):

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/reservations?cluster=Cluster-01&status=genehmigt&abteilung=IT"
```

- `cluster=<Name>` — nur ein Cluster
- `status=beantragt|in Prüfung|genehmigt|abgelehnt|storniert`
- `abteilung=<Name>`

**CSV-Export** (Semikolon-getrennt, für Excel):

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/reservations?format=csv" -o reservierungen.csv
```

CSV-Spalten: `id;name;change;cluster;vcpu;ram_gb;storage_gb;von;abteilung;`
`gilt_ab;gueltig_bis;status;entschieden_von;freigaben;kommentar`

### GET /api/v1/data

Cluster-Kapazitäten aus dem letzten Aria-Abruf:

```json
{"updated": "14.07.2026 19:00",
 "clusters": [{"name": "Cluster-01", "hostCount": 3, "cores": 64,
               "spareCores": 48, "spareRamGb": 1024.0,
               "vcpuCap": 384, "vcpuUsed": 370, "vcpuFree": 14,
               "ramCap": 1024.0, "ramUsed": 1504.0, "ramFree": -480.0,
               "storageCap": 24000.0, "storageUsed": 17826.0, "storageFree": 6174.0,
               "tanzuVcpu": 12, "tanzuRamGb": 128.0,
               "namespaces": [{"name": "ns-webshop-prod", "cpu_mhz": 20000,
                               "vcpu": 8, "ram_gb": 96.0}],
               "vmCount": 93, "vmOff": 8, "hosts": [...], "vms": [...]}]}
```

Hinweis: `vcpuFree`/`ramFree` sind **vor** Abzug genehmigter Reservierungen;
die Reservierungen liefert `/api/v1/reservations` (Status `genehmigt`).

**CSV-Export** (`format=csv`, Semikolon; Spalten folgen `Accept-Language`
bzw. `?lang=de|en`): zusätzlich mit `reserviert_*`-, `tanzu_*`- und
`*_frei_effektiv`-Spalten — also der freien Kapazität **nach** genehmigten
Reservierungen und Tanzu-Namespaces, wie im UI.

### GET /api/v1/status

```json
{"version": "3.1.1", "updated": "27.07.2026 12:00",
 "refreshing": false, "next": 1234,
 "progress": "", "error": null,
 "tiers": {"vms": "27.07.2026 12:00", "network": "…", "storage": "…"}}
```

`next` = Sekunden bis zur nächsten automatischen Aktualisierung.

### GET /healthz

Monitoring-Endpunkt **ohne Authentifizierung** (bewusst nur unkritische
Betriebsdaten): `status` (ok/error), `version`, `updated`,
`data_age_seconds`, `refreshing`, `clusters` (Anzahl), `error`.
Liegt außerhalb von `/api/v1/` und ist für Uptime-Checks gedacht.

## Fehlercodes

| Code | Bedeutung |
|---|---|
| 401 | Kein oder ungültiges/widerrufenes Token |
| 404 | Unbekannter Endpunkt |

## Hinweise

- Die v1-Pfade bleiben stabil; Erweiterungen kommen als neue Felder oder
  Parameter, bestehende Felder ändern sich nicht.
- **Sprache:** JSON-Feldnamen und Statuswerte bleiben deutsch (stabiler
  Vertrag). CSV-Spalten/Statuswerte und die OpenAPI-Beschreibungen folgen
  `Accept-Language` bzw. `?lang=de|en`; ohne Header (curl/Skripte) unverändert
  Deutsch.
- Browser-Sessions (angemeldete Admins) können die v1-Endpunkte ebenfalls
  aufrufen, z. B. zum Testen.

### GET /api/v1/storage-requests

Angefragte **Storage-Erweiterungen** fürs Storage-Team — LUN-Vergrößerungen
und neue LUNs, inkl. **NAA-Kennung**. Standard: offene Anfragen; `status=alle`
für alle, `status=erledigt` für abgeschlossene. Als **CSV** mit `format=csv`
(Spalten u. a. `cluster;typ;lun;naa;aktuell_gb;ziel_gb;neue_lun_gb;status`) —
direkt in die Storage-Automatisierung kippbar.

## Schreib-Endpunkte (Schreibrechte je Token)

Tokens sind standardmäßig **lesend**. In der Verwaltung (Abschnitt
„API-Tokens") lassen sich je Token **per Klick** zwei Schreibrechte
aktivieren; jede Änderung landet im Audit-Log:

| Schreibrecht | Endpunkte |
|---|---|
| **Reservierungen** | `POST /api/v1/reservations` (anlegen), `POST /api/v1/reservations/{id}/cancel` (stornieren) |
| **Genehmigungen** | `POST /api/v1/reservations/{id}/approve` (aktuelle Stufe freigeben), `POST /api/v1/reservations/{id}/reject` (ablehnen) |
| **Storage** | `POST /api/v1/storage-requests/{id}/done` (Storage-Erweiterung als erledigt melden) |

```bash
# Anlegen (Status „beantragt", durchläuft den normalen Workflow):
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"SAP-Erweiterung","cluster":"Cluster-01","vcpu":8,"ram_gb":64,
       "storage_gb":500,"von":"cmdb@firma.local","abteilung":"Team Netzwerk"}' \
  $BASE/api/v1/reservations

# Freigeben / Ablehnen / Stornieren (Kommentar optional, max. 64 Zeichen):
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"comment":"passt"}' $BASE/api/v1/reservations/KAPA-1a2b3c/approve
```

Hinweise:

- API-Entscheidungen wirken wie **Admin-Entscheidungen** (keine
  Team-Beschränkung); `approve` gibt die jeweils **aktuelle Stufe** frei —
  erst wenn alle Stufen freigegeben sind, ist der Antrag genehmigt.
- Actor in Audit-Log und Mails ist `api:<Tokenname>`; bei `POST
  /api/v1/reservations` lassen sich `von` (Anforderer) und `abteilung`
  (Team, für die Team-Sichtbarkeit) mitgeben.
- Mail-Benachrichtigungen laufen wie bei Aktionen im UI.
- Fehler: `403` = Token ohne das nötige Schreibrecht, `404` = Antrag nicht
  gefunden oder bereits entschieden.
