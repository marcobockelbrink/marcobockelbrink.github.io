# Container image (Docker / Podman, base: UBI 9)

> 🇩🇪 [Deutsche Fassung: README.md](README.md)

For environments with a container runtime. The image is based on Red Hat
`ubi9/ubi-minimal` and contains only `python3` plus the script — a few MB, no
external dependencies. It runs as **non-root** (UID 1001).

> For cluster operation see [`../kubernetes/`](../kubernetes/) (manifests +
> Helm chart); without a container runtime the classic installation via
> `config/` (systemd + nginx) remains available.

## Ready-made image from GitHub Packages (GHCR)

On every release the image is built automatically (GitHub Actions,
amd64 + arm64) and published as a GitHub package:

```bash
docker pull ghcr.io/marcobockelbrink/kapa-dashboard:latest
# or a fixed version (recommended for production, targeted rollbacks):
docker pull ghcr.io/marcobockelbrink/kapa-dashboard:3.6.1
```

While the package is **private**, sign in to the registry first (PAT with
`read:packages`): `docker login ghcr.io -u <github-user>`. Making it public
is done on GitHub under *Package → Package settings → Change visibility*.

## Building yourself

Alternatively build locally. The build **context is the project root** (that
is where `aria_kapa.py` lives); the Dockerfile copies this script **with the `kapa/` and `frontend/` folders** into
the image:

```bash
# from the project root
docker build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
# identical with Podman:
podman build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
```

Optionally also tag a fixed version (for targeted rollbacks), e.g.
`-t kapa-dashboard:latest -t kapa-dashboard:3.6.1`. The app version is always
shown in the footer/login and via `aria_kapa.py --version`.

## Running

Configuration is easiest via an INI file (derive it from
`config/kapa.ini.en.example`) plus the password as an environment variable:

```bash
docker run -d --name kapa \
  -p 127.0.0.1:8080:8080 \
  -e ARIA_PASSWORD='THE-ARIA-PASSWORD' \
  -v kapa-data:/opt/kapa/data \
  -v "$PWD/kapa.ini:/etc/kapa/kapa.ini:ro" \
  ghcr.io/marcobockelbrink/kapa-dashboard:latest
```

(With a self-built image use `kapa-dashboard:latest` accordingly.)

> **Port inside the container:** the ENTRYPOINT fixes `--port 8080` — a
> `port = …` in the INI is **ignored** inside the container (CLI beats INI).
> Choose the external port via the host mapping, e.g.
> `-p 127.0.0.1:8888:8080`.

Inside the container the service listens on `0.0.0.0:8080`; externally it is
mapped to `127.0.0.1` only — **a reverse proxy with TLS belongs in front**
(the dashboard itself does not terminate HTTPS). Without a TLS proxy also set
`--cookie-insecure` in the INI, otherwise the session cookie (Secure flag)
will not make it through.

### docker-compose

See [`docker-compose.yml`](docker-compose.yml):

```bash
export ARIA_PASSWORD='THE-ARIA-PASSWORD'
docker compose -f deploy/docker/docker-compose.yml up -d
```

## Configuration & secrets

- **INI file** (`/etc/kapa/kapa.ini`): all options like Aria URL, user, AD,
  SMTP, CPU factor, backup — template: `config/kapa.ini.en.example`.
- **Passwords**: `ARIA_PASSWORD` / `SMTP_PASSWORD` / `BACKUP_PASSWORD` as
  environment variables, or mount them as files and reference them in the INI
  via `password-file = /run/secrets/aria` (Docker/Podman secrets).
- **Data**: volume on `/opt/kapa/data` — contains reservations, roles, audit
  log and token hashes. For backups see `config/RESTORE.en.md`.

## Data & permissions

The data directory belongs to group `0` with `g=u` permissions so it stays
writable even when the runtime assigns an arbitrary UID (e.g. under
OpenShift). A normal `docker run` as UID 1001 works as well.
