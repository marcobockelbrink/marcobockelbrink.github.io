# Restore-Anleitung — VMware Kapazitätsplanung

> 🇬🇧 [English version: RESTORE.en.md](RESTORE.en.md)

Die SFTP-Backups enthalten alle Laufzeitdaten des Dashboards als `tar.gz`:

| Datei | Inhalt | Kritisch? |
|---|---|---|
| `kapa_reservierungen.json` | Alle Kapazitätsanfragen inkl. Status, Freigaben, Kommentaren, Change-Nummern | **Ja** |
| `kapa_rollen.json` | Rollen-, Abteilungs- und Team-Zuweisungen | **Ja** |
| `kapa_teams.json` | Genehmigungs-Teams (Prüfreihenfolge) inkl. Team-Mailadressen | **Ja** |
| `kapa_selektor.json` | Cluster-Selektor (Tag-Filter-Stufen) | Ja |
| `kapa_rollennamen.json` | Frei gewählte Rollen-Bezeichnungen | Ja |
| `kapa_tokens.json` | API-Tokens (nur Hashes) | Ja |
| `kapa_mail.json` | Mail-Benachrichtigungsregeln je Rolle **+ editierbare Mail-Vorlage** (Betreff/HTML) | Ja |
| `kapa_prefs.json` | Persönliche UI-Einstellungen je Benutzer (Tabellenspalten, „Ankündigung gesehen") | Nein (Komfort) |
| `kapa_ankuendigung.json` | Ankündigungs-Popup (Titel/Text/aktiv) | Nein (Komfort) |
| `kapa_autofreigabe.json` | Auto-Freigabe (Schwellen + Team-Haken) | Ja |
| `kapa_sichtbarkeit.json` | Sichtbarkeits-Matrix je Rolle | Ja |
| `kapa_storagecfg.json` | Storage-Einstellungen (Erweiterungen, Mindest-LUN-Größe, Namensfilter) | Ja |
| `kapa_netcfg.json` | Netzwerk-Filter (Portgruppen nach Name/VLAN-ID ausblenden) | Ja |
| `kapa_import.json` | Offline-Quellen (manuell importierte Cluster ohne vROps) | Ja |
| `kapa_history.json` | Statistik-Historie (Tages-Snapshots für Trends) | Ja |
| `kapa_abrufintervalle.json` | gestaffelte Abruf-Intervalle je Teilbereich | Ja |
| `kapa_storage_anfragen.json` | Storage-Erweiterungs-Anfragen (fürs Storage-Team) | Ja |
| `kapa.db` (+ `-wal`/`-shm`) | Bei `storage = sqlite`: alle obigen Sammlungen in einer DB | **Ja** (statt der JSONs) |
| `kapa_log.jsonl` | Audit-Log | Ja (Nachvollziehbarkeit) |
| `kapa_cache.json` | Letzter Aria-Datenabruf | Nein (wird neu abgerufen) |

Bewusst **nicht** im Backup: `kapa_sessions.json` (aktive Anmelde-Sitzungen,
nur Hashes — Sitzungsmaterial gehört nicht auf den Backup-Server; nach einem
Restore melden sich die Benutzer einfach neu an).

Backups werden zweimal täglich erstellt (`--backup-interval 43200`) und auf
dem Ziel 30 Tage aufbewahrt (`--backup-keep-days 30`). Namensschema:
`kapa_backup_JJJJMMTT_HHMMSS.tar.gz`.

## Wiederherstellung (Standardinstallation unter /opt/kapa)

**1. Passendes Backup auf dem Backupserver finden:**

```bash
sftp backup@backupsrv.firma.local
sftp> ls -1 /backup/kapa
sftp> get /backup/kapa/kapa_backup_20260714_190000.tar.gz /tmp/
sftp> exit
```

**2. Dienst anhalten:**

```bash
sudo systemctl stop kapa-dashboard
```

**3. Aktuellen (defekten) Stand zur Sicherheit beiseitelegen:**

```bash
sudo mv /opt/kapa/data /opt/kapa/data.defekt.$(date +%Y%m%d)
sudo mkdir -p /opt/kapa/data
```

**4. Backup einspielen:**

```bash
sudo tar -xzf /tmp/kapa_backup_20260714_190000.tar.gz -C /opt/kapa/data
sudo chown -R kapa:kapa /opt/kapa/data
sudo chmod 600 /opt/kapa/data/*.json*
```

**5. Dienst starten und prüfen:**

```bash
sudo systemctl start kapa-dashboard
journalctl -u kapa-dashboard -n 20
```

Im Log sollte stehen: `Cache geladen: ...`, `Reservierungen geladen: ... (N)`.
Danach im Dashboard kontrollieren: Reservierungen vorhanden, Verwaltung zeigt
die Rollen, Tab „Log" enthält die Historie. Der Kapazitäts-Cache wird beim
ersten Auto-Refresh (spätestens nach 30 Minuten) ohnehin neu aus Aria geladen.

**6. Aufräumen**, wenn alles passt:

```bash
sudo rm -rf /opt/kapa/data.defekt.* /tmp/kapa_backup_*.tar.gz
```

## Einzelne Datei wiederherstellen

Soll z. B. nur eine versehentlich gelöschte Reservierung zurück, kann die
Datei einzeln aus dem Archiv geholt und über die Import-Funktion eingespielt
werden (ersetzt den Bestand!):

```bash
tar -xzf kapa_backup_....tar.gz kapa_reservierungen.json
```

Dann als Admin im Dashboard: „Reservierungen importieren (JSON)".

## Wiederherstellung auf einem neuen Host

1. Repository klonen bzw. `aria_kapa.py` **und den Ordner `frontend/`** nach `/opt/kapa/` kopieren
2. Installation gemäß Kommentar in `config/kapa-dashboard.service`
   (Benutzer, `/etc/kapa/kapa.ini`, `/etc/kapa/aria.pass`, nginx-Snippet)
3. Backup wie oben nach `/opt/kapa/data` entpacken — die Rollen- und
   Reservierungsdaten sind damit sofort wieder da
