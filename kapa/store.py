#!/usr/bin/env python3
"""Datenablage: atomare Datei-Writes, JsonStore (eine JSON-Datei je
Sammlung) und SqliteStore (eine DB-Datei), plus data_path/-Migration.
Aus aria_kapa.py ausgelagert (v3.0)."""
from contextlib import suppress
import json
import threading
import os
import sys
import tempfile


# ----------------------------------------------------------- Datendateien ----

def ensure_dir(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def atomic_write(path, text):
    """Datei atomar schreiben: erst in eine Temp-Datei im selben Verzeichnis,
    dann per os.replace umbenennen. Ein Absturz mitten im Schreiben kann die
    Zieldatei damit nicht mehr beschädigen (kein halb geschriebenes JSON)."""
    ensure_dir(path)
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp)
        raise


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ----------------------------------------------------------- Datenspeicher ----
# Zwei Backends hinter derselben API:
#   JSON   – je Sammlung eine atomar geschriebene .json-Datei (Standard,
#            menschenlesbar/editierbar).
#   SQLite – eine data/kapa.db (Standardbibliothek, kein Server): kleine
#            Sammlungen als Key-Value-Blobs, Reservierungen als eigene Tabelle
#            mit INKREMENTELLEN Schreibzugriffen (nur die geänderte Zeile).

class JsonStore:
    """Backend: je Sammlung eine JSON-Datei, atomar geschrieben."""
    def __init__(self, paths):
        self.paths = paths            # {name: dateipfad}

    def load(self, name, default):
        p = self.paths[name]
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"{p} unlesbar, nutze Standard: {e}", file=sys.stderr)
        return default

    def save(self, name, obj):
        atomic_write(self.paths[name], _dumps(obj))

    # Reservierungen: JSON kennt keine Einzelzeilen -> ganze Liste atomar sichern
    def res_load(self):
        return self.load("res", [])

    def res_save_all(self, items):
        self.save("res", items)

    def res_put(self, entry, items):    # items = aktuelle In-Memory-Liste
        self.save("res", items)

    def res_delete(self, ids, items):
        self.save("res", items)

    def close(self):
        pass


class SqliteStore:
    """Backend: eine SQLite-Datei. Reservierungen inkrementell, kleine
    Sammlungen als JSON-Blobs in einer kv-Tabelle."""
    def __init__(self, db_path):
        import sqlite3
        ensure_dir(db_path)
        # Eine Verbindung, von mehreren HTTP-Threads genutzt -> eigener Lock,
        # weil execute+commit-Folgen sonst ineinanderlaufen können.
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv"
                        "(name TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS reservations"
                        "(id TEXT PRIMARY KEY, created TEXT, data TEXT NOT NULL)")
        self.db.commit()

    def load(self, name, default):
        with self.lock:
            row = self.db.execute("SELECT data FROM kv WHERE name=?",
                                  (name,)).fetchone()
        if row:
            with suppress(ValueError):
                return json.loads(row[0])
        return default

    def save(self, name, obj):
        with self.lock:
            self.db.execute("INSERT INTO kv(name, data) VALUES(?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET data=excluded.data",
                            (name, json.dumps(obj, ensure_ascii=False)))
            self.db.commit()

    def res_load(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM reservations ORDER BY created").fetchall()
        return [json.loads(r[0]) for r in rows]

    def res_save_all(self, items):     # ganzen Bestand ersetzen (Import/Reconcile)
        with self.lock, self.db:
            self.db.execute("DELETE FROM reservations")
            self.db.executemany(
                "INSERT INTO reservations(id, created, data) VALUES(?,?,?)",
                [(e.get("id"), e.get("created", ""), json.dumps(e, ensure_ascii=False))
                 for e in items])

    def res_put(self, entry, items):   # inkrementell: nur diese Zeile (upsert)
        with self.lock:
            self.db.execute(
                "INSERT INTO reservations(id, created, data) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET created=excluded.created, "
                "data=excluded.data",
                (entry.get("id"), entry.get("created", ""),
                 json.dumps(entry, ensure_ascii=False)))
            self.db.commit()

    def res_delete(self, ids, items):  # inkrementell: nur diese Zeilen
        if ids:
            with self.lock:
                self.db.executemany("DELETE FROM reservations WHERE id=?",
                                    [(i,) for i in ids])
                self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


def data_path(path, base="data"):
    """JSON-Datendateien ohne Verzeichnisangabe landen im Datenordner (Standard
    data/, per --data-dir änderbar, z. B. /var/lib/kapa außerhalb des
    Deploy-Verzeichnisses); explizite Pfade (relativ mit Ordner oder absolut)
    bleiben unverändert."""
    if path and not os.path.dirname(path):
        return os.path.join(base, path)
    return path


def migrate_data_files(*paths):
    """Alt-Dateien aus dem Arbeitsverzeichnis in den Datenordner verschieben
    (frühere Versionen legten kapa_*.json direkt neben dem Skript ab)."""
    for p in paths:
        ensure_dir(p)
        old = os.path.basename(p)
        if (not os.path.exists(p) and os.path.exists(old)
                and os.path.abspath(old) != os.path.abspath(p)):
            os.replace(old, p)
            print(f"Datendatei verschoben: {old} -> {p}", file=sys.stderr)

