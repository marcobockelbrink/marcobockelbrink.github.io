#!/usr/bin/env python3
"""SFTP-Backup (tar.gz via ssh/sftp-Batch) und Rotation.
Aus aria_kapa.py ausgelagert (v3.0); nimmt args als Parameter,
kein App-Zustand."""
from contextlib import suppress
import os
import re
from datetime import datetime, timedelta


def _ssh_cmd(args, prog):
    """Basis-Kommando + Umgebung für scp/sftp mit Key- oder Passwort-Auth.

    Wichtig für den systemd-Betrieb: der Dienst läuft gehärtet (ProtectHome,
    ProtectSystem=strict), das Home-Verzeichnis ist also nicht nutzbar. Deshalb
    liegt die known_hosts-Datei im beschreibbaren Datenordner und der Key wird
    ausschließlich über --backup-key angegeben (IdentitiesOnly)."""
    known = args.backup_known_hosts or os.path.join(
        os.path.dirname(args.res_file) or ".", "known_hosts")
    base = [prog, "-q", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known}",
            "-o", "ConnectTimeout=15", "-o", f"Port={args.backup_port}"]
    env = os.environ.copy()
    if args.backup_key:
        base += ["-i", args.backup_key, "-o", "IdentitiesOnly=yes",
                 "-o", "BatchMode=yes"]
    elif args.backup_password:
        env["SSHPASS"] = args.backup_password
        base = ["sshpass", "-e"] + base
    else:
        base += ["-o", "BatchMode=yes"]   # ssh-agent / Standard-Keys
    return base, env


def _ssh_run(cmd, env, timeout=180):
    import subprocess  # nosec B404 – Aufruf ist geprüft (Liste, kein shell)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)  # nosec B603 – Argumentliste, kein shell=True; Ziel/Programm aus der Admin-Konfiguration
    except FileNotFoundError:
        prog = cmd[0]
        if prog == "sshpass":
            raise RuntimeError("'sshpass' ist nicht installiert – für Passwort-Backups "
                               "installieren oder besser --backup-key (SSH-Key) nutzen") from None
        raise RuntimeError(f"Programm '{prog}' nicht gefunden (bitte installieren)") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{cmd[0]} nach {timeout}s abgebrochen (Timeout – "
                           "Backup-Ziel/Port erreichbar?)") from None
    if r.returncode != 0:
        # Die konkrete Shell-Meldung (scp/sftp/ssh) durchreichen – meist steht
        # der eigentliche Grund in stderr (sonst stdout als Rückfall).
        err = (r.stderr.decode(errors="replace").strip()
               or r.stdout.decode(errors="replace").strip())
        raise RuntimeError(f"{cmd[0]} Exit-Code {r.returncode}"
                           + (f": {err}" if err else " (keine Meldung)"))
    return r.stdout.decode(errors="replace")


def sftp_backup(args):
    """Datendateien als tar.gz per scp auf das Backupziel kopieren.

    Authentifizierung per SSH-Key (--backup-key, empfohlen) oder Passwort
    (--backup-password bzw. BACKUP_PASSWORD; erfordert installiertes sshpass).
    Gibt den Namen des übertragenen Archivs zurück."""
    import tarfile
    import tempfile
    if not args.backup_target:
        raise RuntimeError("kein --backup-target konfiguriert")
    db = getattr(args, "db_file", "")
    files = [p for p in (args.cache, args.res_file, args.roles_file,
                         args.log_file, args.tokens_file, args.teams_file,
                         args.rolenames_file, args.selector_file, args.notify_file,
                         getattr(args, "prefs_file", ""),
                         getattr(args, "announce_file", ""),
                         getattr(args, "autoapprove_file", ""),
                         getattr(args, "visibility_file", ""),
                         getattr(args, "storagecfg_file", ""),
                         getattr(args, "storagereq_file", ""),
                         getattr(args, "netcfg_file", ""),
                         getattr(args, "import_file", ""),
                         getattr(args, "history_file", ""),
                         getattr(args, "refreshcfg_file", ""),
                         db, db + "-wal", db + "-shm")
             if p and os.path.exists(p)]
    if not files:
        raise RuntimeError("keine Datendateien vorhanden")
    name = "kapa_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".tar.gz"
    tmp = os.path.join(tempfile.gettempdir(), name)
    with tarfile.open(tmp, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=os.path.basename(f))
    cmd, env = _ssh_cmd(args, "scp")
    try:
        _ssh_run(cmd + [tmp, args.backup_target.rstrip("/") + "/" + name], env)
    finally:
        with suppress(OSError):
            os.remove(tmp)
    return name


def backup_rotate(args):
    """Backups älter als --backup-keep-days auf dem Ziel löschen (per sftp,
    funktioniert auch auf sftp-only-Servern). Gibt die Anzahl gelöschter
    Archive zurück."""
    import tempfile
    if args.backup_keep_days <= 0:
        return 0
    host, _, rdir = args.backup_target.partition(":")
    rdir = (rdir or ".").rstrip("/") or "/"

    def sftp_batch(commands):
        cmd, env = _ssh_cmd(args, "sftp")
        with tempfile.NamedTemporaryFile("w", suffix=".batch",
                                         delete=False) as b:
            b.write("\n".join(commands) + "\n")
            batch = b.name
        try:
            return _ssh_run(cmd + ["-b", batch, host], env)
        finally:
            with suppress(OSError):
                os.remove(batch)

    listing = sftp_batch([f"ls -1 {rdir}"])
    cutoff = ((datetime.now() - timedelta(days=args.backup_keep_days))
              .strftime("%Y%m%d_%H%M%S"))
    olds = sorted({m.group(0)
                   for m in re.finditer(r"kapa_backup_(\d{8}_\d{6})\.tar\.gz",
                                        listing)
                   if m.group(1) < cutoff})
    if olds:
        # führendes '-' = Fehler einzelner rm-Befehle ignorieren
        sftp_batch([f"-rm {rdir}/{name}" for name in olds])
    return len(olds)

# ----------------------------------------------------------- Mail-Reports ----

