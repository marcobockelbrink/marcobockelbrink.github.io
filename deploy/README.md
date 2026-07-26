# Deployment-Varianten

> 🇬🇧 [English version: README.en.md](README.en.md)

Die Anwendung ist ein Python-Skript (`aria_kapa.py`) plus der `frontend/`-Ordner
(HTML/CSS/JS als Dateien) — nur Standardbibliothek, Python 3.8+, kein Build.
Auslieferung als Container — wähle nach Umgebung:

| Variante | Wann | Verzeichnis |
|---|---|---|
| **Docker / Podman** | Einzelhost mit Container-Runtime (compose, UBI9-Image) | [`docker/`](docker/) |
| **Kubernetes** | Cluster-Betrieb — einfache Manifeste **oder** Helm-Chart | [`kubernetes/`](kubernetes/) |

Beide nutzen dasselbe **GHCR-Image**
(`ghcr.io/marcobockelbrink/kapa-dashboard`), das bei jedem Release
automatisch gebaut wird (amd64 + arm64).

Daneben bleibt die **klassische Host-Installation** (systemd + nginx, ohne
Container) über die Vorlagen unter [`../config/`](../config/) möglich —
Schritt-für-Schritt in den Kommentaren von `config/kapa-dashboard.service`.
SELinux-Hinweis für nginx als Proxy: `setsebool -P httpd_can_network_connect 1`.

> Historie: Die früheren **RPM**- und **Ansible**-Varianten wurden mit v2.10
> eingestellt (zuletzt enthalten in
> [v2.9.1](https://github.com/marcobockelbrink/vmware/releases/tag/v2.9.1)).

## Gemeinsame Grundlagen

- **Konfiguration**: eine INI-Datei (`config/kapa.ini.example`, englisch:
  `kapa.ini.en.example`) — alle nicht-geheimen Optionen. Secrets
  (Aria-/SMTP-/Backup-Passwort) als Umgebungsvariablen bzw.
  Kubernetes-Secret; auf klassischen Hosts als `.pass`-Dateien.
- **Daten** liegen unter `/opt/kapa/data` (Volume/PVC bzw. `/var/lib/kapa`)
  und müssen Updates überleben. Restore: `config/RESTORE.md`.
- **TLS** terminiert am Reverse Proxy / Ingress; das Dashboard selbst
  spricht nur HTTP. Monitoring über **`/healthz`** (ohne Anmeldung).
