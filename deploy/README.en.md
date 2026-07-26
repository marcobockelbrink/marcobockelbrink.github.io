# Deployment variants

> 🇩🇪 [Deutsche Fassung: README.md](README.md)

The application is a Python script (`aria_kapa.py`) plus the `frontend/` folder
(HTML/CSS/JS as files) — standard library only, Python 3.8+, no build. It ships
as a container — choose by environment:

| Variant | When | Directory |
|---|---|---|
| **Docker / Podman** | single host with a container runtime (compose, UBI9 image) | [`docker/`](docker/) |
| **Kubernetes** | cluster operation — plain manifests **or** Helm chart | [`kubernetes/`](kubernetes/) |

Both use the same **GHCR image**
(`ghcr.io/marcobockelbrink/kapa-dashboard`), built automatically on every
release (amd64 + arm64).

The **classic host installation** (systemd + nginx, no container) remains
available via the templates under [`../config/`](../config/) — step by step
in the comments of `config/kapa-dashboard.service`. SELinux note for nginx
as proxy: `setsebool -P httpd_can_network_connect 1`.

> History: the former **RPM** and **Ansible** variants were retired with
> v2.10 (last included in
> [v2.9.1](https://github.com/marcobockelbrink/vmware/releases/tag/v2.9.1)).

## Common foundations

- **Configuration**: one INI file (`config/kapa.ini.en.example`) — all
  non-secret options. Secrets (Aria/SMTP/backup passwords) as environment
  variables or a Kubernetes secret; on classic hosts as `.pass` files.
- **Data** lives under `/opt/kapa/data` (volume/PVC or `/var/lib/kapa`) and
  must survive updates. Restore: `config/RESTORE.en.md`.
- **TLS** terminates at the reverse proxy / ingress; the dashboard itself
  speaks HTTP only. Monitoring via **`/healthz`** (no sign-in).
