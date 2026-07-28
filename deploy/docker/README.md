# Container-Image (Docker / Podman, Basis: UBI 9)

> 🇬🇧 [English version: README.en.md](README.en.md)

Für Umgebungen mit Container-Runtime. Das Image basiert auf Red Hat
`ubi9/ubi-minimal` und enthält nur `python3` plus das Skript – wenige MB, keine
externen Abhängigkeiten. Es läuft als **nicht-root** (UID 1001).

> Für Cluster-Betrieb siehe [`../kubernetes/`](../kubernetes/) (Manifeste +
> Helm-Chart); ohne Container-Runtime bleibt die klassische Installation über
> `config/` (systemd + nginx).

## Fertiges Image von GitHub Packages (GHCR)

Bei jedem Release wird das Image automatisch gebaut (GitHub Actions,
amd64 + arm64) und als GitHub Package veröffentlicht:

```bash
docker pull ghcr.io/marcobockelbrink/kapa-dashboard:latest
# oder eine feste Version (empfohlen für Produktion, gezielte Rollbacks):
docker pull ghcr.io/marcobockelbrink/kapa-dashboard:3.5.3
```

Solange das Package **privat** ist, vorher am Registry anmelden (PAT mit
`read:packages`): `docker login ghcr.io -u <github-user>`. Öffentlich schalten
geht auf GitHub unter *Package → Package settings → Change visibility*.

## Selbst bauen

Alternativ lokal bauen. Der Build-**Context ist die Projektwurzel** (dort liegt
`aria_kapa.py`); die Dockerfile kopiert dieses Skript **mit `kapa/`- und `frontend/`-Ordner** ins Image:

```bash
# aus der Projektwurzel
docker build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
# identisch mit Podman:
podman build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
```

Optional zusätzlich eine feste Version taggen (für gezielte Rollbacks), z. B.
`-t kapa-dashboard:latest -t kapa-dashboard:1.5`. Die App-Version steht immer im
Footer/Login und unter `aria_kapa.py --version`.

## Starten

Konfiguration am einfachsten per INI-Datei (aus `config/kapa.ini.example`
ableiten) plus Passwort als Umgebungsvariable:

```bash
docker run -d --name kapa \
  -p 127.0.0.1:8080:8080 \
  -e ARIA_PASSWORD='DAS-ARIA-PASSWORT' \
  -v kapa-data:/opt/kapa/data \
  -v "$PWD/kapa.ini:/etc/kapa/kapa.ini:ro" \
  ghcr.io/marcobockelbrink/kapa-dashboard:latest
```

(Beim selbst gebauten Image entsprechend `kapa-dashboard:latest`.)

> **Port im Container:** Der ENTRYPOINT setzt `--port 8080` fest — ein
> `port = …` in der INI wird im Container **ignoriert** (CLI schlägt INI).
> Den Außen-Port bestimmst du über das Host-Mapping, z. B.
> `-p 127.0.0.1:8888:8080`.

Der Dienst lauscht im Container auf `0.0.0.0:8080`; nach außen wird er nur an
`127.0.0.1` gemappt – **davor gehört ein Reverse Proxy mit TLS** (das Dashboard
selbst terminiert kein HTTPS). Ohne TLS-Proxy zusätzlich `--cookie-insecure` in
der INI setzen, sonst kommt das Session-Cookie (Secure-Flag) nicht durch.

### docker-compose

Siehe [`docker-compose.yml`](docker-compose.yml):

```bash
export ARIA_PASSWORD='DAS-ARIA-PASSWORT'
docker compose -f deploy/docker/docker-compose.yml up -d
```

## Konfiguration & Secrets

- **INI-Datei** (`/etc/kapa/kapa.ini`): alle Optionen wie Aria-URL, Benutzer,
  AD, SMTP, CPU-Faktor, Backup – Vorlage: `config/kapa.ini.example`.
- **Passwörter**: `ARIA_PASSWORD` / `SMTP_PASSWORD` / `BACKUP_PASSWORD` als
  Umgebungsvariable, oder als Datei einhängen und in der INI per
  `password-file = /run/secrets/aria` referenzieren (Docker-/Podman-Secrets).
- **Daten**: Volume auf `/opt/kapa/data` – enthält Reservierungen, Rollen,
  Audit-Log und Token-Hashes. Für Backups siehe `config/RESTORE.md`.

## Daten & Rechte

Das Datenverzeichnis gehört Gruppe `0` mit `g=u`-Rechten, damit es auch dann
beschreibbar ist, wenn die Runtime eine beliebige UID vergibt (z. B. unter
OpenShift). Bei einem normalen `docker run` als UID 1001 funktioniert das
ebenfalls.
