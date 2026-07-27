# Architektur — VMware Kapazitätsplanung

> 🇬🇧 [English version: ARCHITECTURE.en.md](ARCHITECTURE.en.md)
>
> Stand: v3.0. Die Schaubilder sind Mermaid-Diagramme — GitHub rendert sie
> direkt im Browser.

## Leitidee

Seit **v3.0** besteht die Anwendung aus drei review-freundlichen Teilen:
`aria_kapa.py` (Einstieg, Serve-Setup, Hintergrund-Threads, Fachlogik), das
**Backend-Paket `kapa/`** (zehn Module: HTTP-Handler mit Dispatch-Tabellen,
vROps-Client, Datensammlung, Datenablage, LDAP, Mail, Backup, Auslieferung,
Konstanten, Helfer) und der **`frontend/`-Ordner** (HTML/CSS/JS als echte
Dateien, zur Laufzeit geladen). Die UI spricht ausschließlich die JSON-API
unter **einem Pfadraum `/api/v1/`** (API-first: Session-Cookie für die
Oberfläche, Bearer-Token für externe Anwendungen — getrennte Autorisierung,
gleicher Vertrag). Weiterhin nur Standardbibliothek (Python 3.8+): kein pip,
kein Build, keine Datenbank-Server — das Dashboard läuft auf jedem RHEL-Host
ohne Paket-Zoo, ein Update ist der Austausch von Skript + `kapa/` +
`frontend/`.

```mermaid
flowchart LR
    subgraph Nutzer
        B["Browser<br/>(Admin / Reviewer /<br/>Anforderer / Auditor)"]
        EXT["Externe Apps<br/>(Grafana, CMDB, …)<br/>Bearer-Token"]
        MON["Monitoring<br/>(Uptime-Check)"]
    end

    subgraph Host["Linux-Host"]
        NG["nginx :443<br/>TLS, Pfad /capa/"]
        APP["aria_kapa.py --serve<br/>127.0.0.1:8080<br/>systemd, User kapa"]
        DATA[("Datenablage<br/>/var/lib/kapa<br/>JSON oder SQLite")]
    end

    subgraph Extern["Umsysteme"]
        V1["vROps Quelle 1<br/>(RZ-Nord)"]
        V2["vROps Quelle n<br/>(RZ-Sued)"]
        ISO["Isoliertes vCenter<br/>(keine Netzanbindung)"]
        AD["Active Directory<br/>ldaps://"]
        SMTP["SMTP-Server"]
        BK["SFTP-Backupziel"]
        HOOK["Webhook-Ziele<br/>(ITSM, AWX, Pipelines, Chat)"]
    end

    B -->|HTTPS| NG
    EXT -->|HTTPS /api/v1| NG
    MON -->|/healthz| NG
    NG -->|HTTP lokal| APP
    APP <-->|Suite-API, OpsToken| V1
    APP <-->|Suite-API, OpsToken| V2
    ISO -.->|PowerCLI-Export<br/>JSON, Admin-Upload| APP
    APP -->|Simple Bind + memberOf| AD
    APP -->|Mails DE-Vorlage| SMTP
    APP -->|tar.gz 2x täglich| BK
    APP -->|Webhook-POST, signiert| HOOK
    APP <--> DATA
```

**Vertrauensgrenzen:** TLS endet am nginx; das Dashboard spricht nur
lokales HTTP. Zugriff auf vROps ausschließlich **lesend** (eigenes
Read-only-Servicekonto), je Quelle optional über einen eigenen Proxy.
Secrets liegen nie in der INI, sondern in `.pass`-Dateien (root:kapa, 0640).

## Innenleben des Prozesses

```mermaid
flowchart TB
    subgraph HTTP["ThreadingHTTPServer (ein Thread je Request)"]
        R1["Seiten-Routen<br/>/ /reservierungen /genehmigungen<br/>/archiv /verwaltung /log"]
        R2["Session-API<br/>/api/* (Cookie)"]
        R3["v1-API<br/>/api/v1/* (Bearer/Session)<br/>lesend + Schreibrechte je Token"]
        R4["/healthz (ohne Auth)"]
    end

    subgraph BG["Hintergrund-Threads (daemon)"]
        T1["scheduler<br/>gestaffelte Abrufe: Kapazität /<br/>Netzwerk / Storage je eigenem Takt"]
        T2["maintenance<br/>Ablauf/TTL, Log-Rotation"]
        T3["backup_loop<br/>SFTP 2x täglich + Rotation"]
        T4["reminder_loop<br/>stündlich: liegengebliebene<br/>Anträge anmahnen"]
    end

    subgraph CORE["Gemeinsamer Kern"]
        ST["state (Cluster-Daten<br/>aus letztem Abruf)"]
        RES["reservations + res_lock"]
        DEC["res_apply_approve/reject/cancel<br/>EINE Entscheidungs-Logik<br/>für UI und API"]
        MAIL["mail_event → Vorlage<br/>{{platzhalter}}, mehrsprachiger Betreff"]
        STORE["JsonStore / SqliteStore<br/>atomare Writes (mkstemp+rename)"]
    end

    R1 & R2 & R3 --> CORE
    T1 --> ST
    T2 & T3 --> STORE
    T4 --> DEC
    DEC --> MAIL
    RES <--> STORE
    ST <--> STORE
```

Kernregel seit v2.8.1: **Zustandsübergänge existieren genau einmal.**
Session-UI und Schreib-API rufen dieselben `res_apply_*`-Funktionen —
Verhalten kann nicht auseinanderlaufen (der Refactor fand prompt einen
Divergenz-Bug: stornierte Anträge waren über die UI noch genehmigbar).

## Datenfluss: Aria-Abruf

```mermaid
sequenceDiagram
    participant S as scheduler
    participant C as collect() je Quelle
    participant V as vROps Suite-API
    participant B as build_summary
    participant ST as state + Cache

    S->>C: do_refresh (alle Quellen nacheinander,<br/>nur fällige Teilbereiche VMs/Netzwerk/Storage<br/>je eigenem Intervall, Rest aus Rohdaten-Cache)
    C->>V: Cluster, Hosts, VMs (+ Metriken/Properties, Bulk)
    C->>V: Host-HBA-WWPNs (storageAdapter vmhbaN port_WWN,<br/>Kandidaten-Range im Bulk)
    C->>V: Datastores (Storage, vSAN-Faktor,<br/>NAA aus Properties ODER Metrik-Keys Devices naa)
    C->>V: Tags, Workload-Badge
    C->>V: dvSwitches → Portgruppen (VLAN-Cache,<br/>Voll-Abruf 1x täglich)
    C->>V: Tanzu-Namespaces (Reservierungen,<br/>Kandidaten-Keys, best effort)
    C->>B: Rohdaten je Cluster
    B->>B: N+1-Abzug, CPU-Faktor,<br/>Tanzu MHz→vCPU (aufgerundet)
    B->>B: Filter anwenden: Mindest-LUN + Storage-Namensfilter,<br/>Netzwerk-Filter (Portgruppen-Name/VLAN-ID)
    B->>ST: Cluster-Liste (+ source-Badge)
    ST->>ST: Tages-Snapshot je Cluster in die<br/>Statistik-Historie (Trends, 2 Jahre)
    Note over B,ST: Offline-Quellen (Import) laufen durch<br/>DIESELBE build_summary, gleiche Mathematik,<br/>angehängt mit imported=True
    Note over ST: Teilausfall-tolerant: fällt eine Quelle aus,<br/>liefern die übrigen weiter
```

Jeder Teilschritt ist **best effort**: Fehlt Storage/Netzwerk/Tanzu in einer
Umgebung, bleibt der jeweilige Teil leer und der Rest läuft weiter. Das Log
nennt je Schritt, was erkannt wurde (Schlüssel, Zuordnungen, Cache-Treffer) —
so lassen sich versionsabhängige vROps-Stat-Keys ohne Code-Änderung prüfen.

## Genehmigungs-Workflow

```mermaid
stateDiagram-v2
    [*] --> beantragt: Antrag (UI oder API)
    beantragt --> inPruefung: 1. Team gibt frei
    inPruefung --> inPruefung: weitere Stufe frei<br/>(Reihenfolge = Teams-Tabelle)
    inPruefung --> genehmigt: letzte Stufe frei
    beantragt --> abgelehnt: Team/Admin lehnt ab
    inPruefung --> abgelehnt: Team/Admin lehnt ab
    beantragt --> storniert: Anforderer/Team/Admin
    inPruefung --> storniert: Anforderer/Team/Admin
    genehmigt --> storniert: Storno (zählt nicht mehr)
    abgelehnt --> [*]: Archiv (dauerhaft)
    storniert --> [*]: Archiv (dauerhaft)
    genehmigt --> [*]: Ablauf nach res-ttl-days

    note right of inPruefung
        reminder_loop mailt das Team,
        wenn eine Stufe länger als
        reminder_days wartet
    end note
```

Optional gibt eine **Auto-Freigabe** je Team angehakte Stufen automatisch
frei, wenn der Ziel-Cluster nach Abzug des Antrags konfigurierte Schwellen
(vCPU/RAM/größte LUN/Workload) einhält — geprüft bei Antragstellung und
Stufenwechsel, konservativ (fehlende Daten blockieren), voll auditiert.
**Import-Cluster (Offline-Quellen) klammert sie grundsätzlich aus** — deren
Zahlen sind statisch, Anträge gehen dort immer an die Teams.
Erst der Status **genehmigt** zählt gegen die freie Kapazität — zusammen mit
den automatisch gelesenen **Tanzu-Namespace-Reservierungen**. Mails gehen je
Ereignis nach der Matrix in der Verwaltung (Anlage/Ablehnung/Freigabe/„Team
ist dran"/Erinnerung), gerendert über die **editierbare HTML-Vorlage**.
Dieselben Ereignisse (plus Storage-Anfragen und Offline-Import) feuern optional
**ausgehende Webhooks**: ein HTTP-POST mit JSON, **HMAC-SHA256-signiert**
(`X-Kapa-Signature`), im Hintergrund zugestellt und auditiert — für ITSM,
Automatisierung (AWX/n8n), Pipelines oder Chat. Webhook-Secrets liegen (wie die
Sitzungen) bewusst **nicht im Backup**.
Für Reviewer verlinkt die Genehmigungs-Ansicht ein **Reviewer-Handbuch**
(eigene zweisprachige Doku-Seite unter `/reviewer-handbuch`).

## Storage-Erweiterungen (Brücke zum Storage-Team)

Freigebende können beim Genehmigen — oder Berechtigte ad-hoc in der
Storage-Übersicht — eine **LUN-Vergrößerung oder neue LUN** anfragen
(vSAN ausgenommen). Die Anfragen landen in einer eigenen Sammlung und werden
vom Storage-Team **per API** abgeholt:

```mermaid
flowchart LR
    A["Freigabe-Dialog<br/>+ Storage-Erweiterung"] --> Q[("storagereq<br/>offen/erledigt")]
    B["Storage-Übersicht<br/>Erweitern je LUN"] --> Q
    Q -->|GET /api/v1/storage-requests<br/>JSON + CSV| T["Storage-Team /<br/>Automatisierung"]
    T -->|POST …/done<br/>Token-Schreibrecht Storage| Q
```

Jede Anfrage trägt zur Identifikation alles Nötige: **NAA** der LUN, die
**ESXi-Hosts des Clusters samt FC-HBA-WWPNs** (fürs Zoning) und optional den
Bezug zur Kapazitätsanfrage. Admin-Regeln in der Verwaltung: Mindest-LUN-Größe
und Namensfilter (Anzeige), **Maximal-Größe je Anfrage** (Limit, server- und
clientseitig geprüft).

## Offline-Quellen (Cluster-Import ohne vROps)

Bereiche ohne Netzanbindung exportiert ein Kollege mit dem mitgelieferten
**PowerCLI-Skript** (Download in der Verwaltung → Import); das JSON wird
unter einem **festen Quellnamen** hochgeladen. Die Rohdaten (Hosts, VMs,
Datastores, Portgruppen mit VLAN-ID) laufen bei **jedem Abruf** durch dasselbe
`build_summary` wie echte Quellen — identische Kapazitäts-Mathematik und
Filter. Mehrere Quellen parallel; Re-Import ersetzt, Löschen entfernt die
Cluster mit dem nächsten Abruf. Das Import-Datum steht als Tag am Cluster;
`imported=True` markiert die Cluster intern (Auto-Freigabe-Ausschluss).

## Sicherheit in Kürze

| Ebene | Mechanik |
|---|---|
| Anmeldung | LDAP Simple Bind (BER-kodiert → keine Filter-Injection), leeres Passwort abgewiesen, Login-Bremse 5/5 min, Passwort-Detektor im Benutzerfeld |
| Autorisierung | Rollen serverseitig erzwungen (Anforderer: Team-Sicht, kein Workload, kein „entschieden von"); Reviewer nur, wenn ihr Team dran ist |
| vROps-Quellen-Filter | Je Benutzer/AD-Gruppe auf bestimmte Quellen einschränkbar (leer = alle); serverseitig in `clusters_for`/`visible_res`/Storage-Anfragen erzwungen, greift damit für Cluster, VLAN, Storage, Reservierungen und Anfragen; mehrere Gruppen-Treffer werden vereinigt |
| Sessions | `secrets.token_urlsafe(32)`, Cookie `HttpOnly; Secure; SameSite=Lax` (CSRF-Schutz), Pruning beim Login |
| API | Tokens nur als SHA-256-Hash, `hmac.compare_digest`, Schreibrechte je Token einzeln zuschaltbar, alles auditiert |
| Ausgabe | Strikte CSP, `json_for_html` gegen `</script>`-Ausbruch, Escaping aller Fremddaten, Vorlagen-Vorschau im sandbox-iframe |
| Betrieb | systemd-Sandbox (ProtectSystem=strict), Dateien 0600 via mkstemp, Request-Limit 2 MiB, gzip nur für Text-Typen |

## Frontend

Eine einzige HTML-Seite (im Skript als Template eingebettet, Daten per
`__PLATZHALTER__` server-seitig injiziert), Views per `render()`-Dispatch
(Pfad oder Hash). Querschnittsfunktionen liegen als kleine Engines am
Skript-Ende:

- **i18n**: Deutsch ist Quelle; Browser ≠ deutsch → Wörterbuch (~500 Einträge)
  + Regex-Muster, ein MutationObserver übersetzt Textknoten **und** Attribute
  laufend. Elemente mit Inline-Auszeichnung (`<b>`/`<code>` mitten im Satz)
  werden als **ganzer Satz** übersetzt (i18nFlatten) — sonst zerfielen sie in
  unübersetzbare Fragmente. Auch Standard-Audit-Aktionen erscheinen übersetzt;
  gespeichert wird das Log weiter deutsch. API-Werte/Statuslogik bleiben
  deutsch (v1-Vertrag).
- **Theme**: CSS-Variablen, `data-theme="light"` am `<html>`, Kopf-Snippet
  gegen Flackern, Wahl in den Server-Prefs je Benutzer.
- **Prefs**: Spalten, „Ankündigung gesehen", Theme — ein PUT ersetzt komplett,
  darum baut `prefsBody()` immer den Vollzustand.
- **Deep-Links**: `#cluster=Name` öffnet die Detailkarte, Hash wird beim
  Öffnen gesetzt.
- **Listen**: sortierbare Tabellen, Spalten ein-/ausblendbar (Prefs) und ein
  **gemeinsamer Seiten-Blätterer** für alle großen Listen (Kapazität, VLAN,
  Storage, Reservierungen, Genehmigungen, Archiv, Log): ein Kern
  `paginate(id, liste)` liefert den Seiten-Ausschnitt und baut die Leiste
  `#pager_<id>` (100/200/300 je Seite); `PAGE_RENDER` bindet jede Tabelle an
  ihre Render-Funktion. Summen/Gesamtwerte rechnen über die volle Menge, nur
  die Anzeige wird geblättert.
- **Statistik**: Trend-Charts als **selbst gezeichnete SVGs** (kein CDN) aus
  der Tages-Historie — Ø RAM/vCPU/Disk je VM, VM-Anzahl, Auslastungen,
  Größenklassen-Vergleich; Sichtbarkeit über das Matrix-Feature „statistik".
- **Anzeige-Zeitzone**: alle Zeitausgaben nutzen `datetime.now()`/
  `fromtimestamp()` und damit die Prozess-Zeitzone. In der Verwaltung
  („Datenabruf") frei einstellbar — ein `time.tzset()` mit der gewählten
  IANA-Zone wirkt prozessweit auf **alle** Ausgaben (Stand, Log, Abgleich,
  Mails); der Scheduler rechnet in `time.time()`-Sekunden und bleibt unberührt.

## Datenhaltung

Alle Sammlungen (Reservierungen, Rollen, Teams, Selektor, Rollennamen,
Tokens, Mail-Regeln, Prefs, Ankündigung, Auto-Freigabe, Sessions,
Sichtbarkeit, Storage-Einstellungen, Storage-Anfragen, Netzwerk-Filter,
Offline-Quellen, Statistik-Historie, Abruf-Intervalle) laufen über eine
Store-Abstraktion:
**JSON-Dateien** (Standard, je Sammlung eine Datei) oder **SQLite** (eine
`kapa.db`, inkrementelle Reservierungs-Writes, automatische Einmal-Migration).
Schreiben immer atomar. Details und Restore: [`../config/RESTORE.md`](../config/RESTORE.md).

## Deployment

```mermaid
flowchart LR
    GH["GitHub-Repo<br/>+ Release-Tag v*"]
    GH -->|GitHub Actions| IMG["GHCR-Image<br/>kapa-dashboard:latest + :x.y<br/>amd64 + arm64"]
    IMG --> DOCK["Docker/Podman<br/>compose, UBI9, non-root"]
    IMG --> K8S["Kubernetes<br/>Manifeste oder Helm-Chart<br/>1 Replikat + PVC, /healthz-Probes"]
    GH --> HOSTS["Klassisch: systemd + nginx<br/>(Vorlagen unter config/)"]
```

Dasselbe Artefakt, Container-first — Auswahlhilfe in
[`../deploy/README.md`](../deploy/README.md).

## Bewusste Entscheidungen (Kurz-ADRs)

1. **Nur Standardbibliothek, minimaler Fußabdruck** — Betrieb ohne Paketmanagement,
   Update = Dateitausch; bezahlt mit eingebetteten Templates.
2. **Deutsch als Quellsprache + Übersetzungs-Engine** statt doppelter
   Templates — eine Pflegequelle, EN folgt automatisch; API bleibt stabil
   deutsch (v1-Vertrag).
3. **Best-effort-Datensammlung mit Kandidaten-Keys** — vROps-Versionen
   unterscheiden sich; lieber lückenhaft + gut geloggt als hart scheiternd.
4. **Tanzu konservativ gezählt** — Namespace-Reservierung zusätzlich zur
   VM-Belegung; mögliche Doppelzählung zugunsten sicherer Planung akzeptiert.
5. **Cluster-Name als Schlüssel über Quellen hinweg** (Variante A) —
   Voraussetzung eindeutige Namen; dafür bleiben Reservierungen beim
   Quellen-Umbau stabil.
6. **Fail-fast-Konfiguration** — unbekannte INI-Schlüssel und verrutschte
   `[quelle:*]`-Einträge brechen den Start mit Hinweis ab, statt still zu
   falschen Defaults zu führen.
7. **Instanzierte vROps-Schlüssel per Kandidaten-Range im Bulk** — NAA steckt
   je nach Version in Metrik-Keys (`Devices|naa…`), WWPNs in Properties
   (`storageAdapter:vmhbaN|port_WWN`). Statt einem Property-Abruf **je Host**
   (bei > 1000 Hosts zu langsam) werden Kandidaten-Schlüssel im vorhandenen
   Bulk-Aufruf mitgeholt; Diagnose-Zeilen im Log zeigen die echten Schlüssel.
8. **Offline-Quellen als statische vROps-Äquivalente** — Import-JSON läuft
   durch dieselbe `build_summary` statt eigener Rechenwege; ein einziges
   `imported`-Flag steuert die Sonderbehandlung (Auto-Freigabe-Ausschluss).
   Bezahlt mit bewusst statischen Daten (Import-Datum als Tag sichtbar).
