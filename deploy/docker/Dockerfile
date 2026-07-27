# VMware Kapazitätsplanung – Container-Image auf Basis von Red Hat UBI 9.
# VMware Capacity Planning – container image based on Red Hat UBI 9.
# Nur Python-Standardbibliothek, daher genügt python3 im Minimal-Image.
# Standard library only, so python3 in the minimal image is all we need.
#
# Bauen (aus der Projektwurzel!) / build (from the project root!):
#   docker build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
# Läuft unverändert auch mit Podman / works unchanged with Podman:
#   podman build -f deploy/docker/Dockerfile -t kapa-dashboard:latest .
# Basis-Image per Digest gepinnt (reproduzierbar; Dependabot schlägt
# Digest-Updates als PR vor — ein Floating-Tag könnte es nicht bumpen).
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest@sha256:2e8edce823a48e51858f1fad3ff4cbf6875ce8a3f86b9eecf298bc2050c8652a

LABEL org.opencontainers.image.title="VMware Kapazitätsplanung" \
      org.opencontainers.image.source="https://github.com/marcobockelbrink/vmware" \
      org.opencontainers.image.description="Aria Operations Kapazitäts-Dashboard (stdlib-only)"

# tzdata bringt die Zonendateien mit, damit die in der Verwaltung einstellbare
# Anzeige-Zeitzone (z. B. Europe/Berlin) im Container aufgelöst werden kann;
# UBI-minimal enthält sie sonst nicht.
# tzdata ships the zone files so the display time zone configurable in the admin
# UI (e.g. Europe/Berlin) resolves inside the container; UBI-minimal lacks it.
# python3 + tzdata (für die einstellbare Anzeige-Zeitzone). Den kapa-Benutzer
# legen wir direkt in /etc/passwd an – das spart das shadow-utils-Paket und
# macht das Image kleiner. tzdata ships the zone files for the configurable
# display time zone; the user is added straight to /etc/passwd to avoid the
# shadow-utils package and keep the image smaller.
RUN microdnf install -y python3 tzdata \
    && microdnf clean all \
    && echo "kapa:x:1001:0:kapa service account:/opt/kapa:/sbin/nologin" >> /etc/passwd

WORKDIR /opt/kapa
COPY --chown=1001:0 aria_kapa.py /opt/kapa/aria_kapa.py
COPY --chown=1001:0 kapa/ /opt/kapa/kapa/
# Frontend (HTML/CSS/JS) liegt seit v2.30 als Dateien neben dem Skript.
COPY --chown=1001:0 frontend/ /opt/kapa/frontend/

# Daten-Volume. Gruppe 0 + g=u macht das Verzeichnis auch dann beschreibbar,
# wenn die Runtime eine beliebige UID vergibt (OpenShift-kompatibel).
# Data volume. Group 0 + g=u keeps the directory writable even when the
# runtime assigns an arbitrary UID (OpenShift-compatible).
RUN mkdir -p /opt/kapa/data \
    && chown -R 1001:0 /opt/kapa \
    && chmod -R g=u /opt/kapa

USER 1001
EXPOSE 8080
VOLUME ["/opt/kapa/data"]

# Health-Check ohne Zusatzpaket (kein curl im Minimal-Image): Python fragt
# /healthz ab. Kubernetes nutzt eigene Probes; das hilft plain Docker/Podman.
# Health check without extra packages (no curl in the minimal image).
HEALTHCHECK --interval=30s --timeout=4s --start-period=40s --retries=3 \
  CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"]

# Im Container an alle Interfaces binden (der Port wird vom Host/Proxy gemappt).
# Zusätzliche Optionen kommen über CMD und lassen sich beim Start überschreiben.
# Bind to all interfaces inside the container (the host/proxy maps the port).
# Extra options come via CMD and can be overridden at start, e.g.:
#   docker run ... kapa-dashboard:latest --config /etc/kapa/kapa.ini
ENTRYPOINT ["python3", "/opt/kapa/aria_kapa.py", "--serve", "--bind", "0.0.0.0", "--port", "8080"]
CMD ["--config", "/etc/kapa/kapa.ini"]
