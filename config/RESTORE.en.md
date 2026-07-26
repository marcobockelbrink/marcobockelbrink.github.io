# Restore guide — VMware Capacity Planning

> 🇩🇪 [Deutsche Fassung: RESTORE.md](RESTORE.md)

The SFTP backups contain all runtime data of the dashboard as `tar.gz`:

| File | Contents | Critical? |
|---|---|---|
| `kapa_reservierungen.json` | all capacity requests incl. status, approvals, comments, change numbers | **yes** |
| `kapa_rollen.json` | role, department and team assignments | **yes** |
| `kapa_teams.json` | approval teams (review order) incl. team mail addresses | **yes** |
| `kapa_selektor.json` | cluster selector (tag filter levels) | yes |
| `kapa_rollennamen.json` | freely chosen role labels | yes |
| `kapa_tokens.json` | API tokens (hashes only) | yes |
| `kapa_mail.json` | mail notification rules per role **+ editable mail template** (subject/HTML) | yes |
| `kapa_prefs.json` | personal UI settings per user (table columns, "announcement seen") | no (convenience) |
| `kapa_ankuendigung.json` | announcement popup (title/text/active) | no (convenience) |
| `kapa_autofreigabe.json` | auto-approval (thresholds + team hooks) | yes |
| `kapa_sichtbarkeit.json` | visibility matrix per role | yes |
| `kapa_storagecfg.json` | storage settings (expansions, minimum LUN size, name filter) | yes |
| `kapa_netcfg.json` | network filter (hide port groups by name/VLAN ID) | yes |
| `kapa_import.json` | offline sources (manually imported clusters without vROps) | yes |
| `kapa_history.json` | statistics history (daily snapshots for trends) | yes |
| `kapa_abrufintervalle.json` | tiered refresh intervals per area | yes |
| `kapa_storage_anfragen.json` | storage expansion requests (for the storage team) | yes |
| `kapa.db` (+ `-wal`/`-shm`) | with `storage = sqlite`: all collections above in one DB | **yes** (instead of the JSONs) |
| `kapa_log.jsonl` | audit log | yes (traceability) |
| `kapa_cache.json` | last Aria data fetch | no (fetched again) |

Deliberately **not** in the backup: `kapa_sessions.json` (active sign-in
sessions, hashes only — session material does not belong on the backup
server; after a restore users simply sign in again).

Backups are created twice a day (`--backup-interval 43200`) and kept for
30 days on the target (`--backup-keep-days 30`). Naming scheme:
`kapa_backup_YYYYMMDD_HHMMSS.tar.gz`.

## Restore (standard installation under /opt/kapa)

**1. Find the right backup on the backup server:**

```bash
sftp backup@backupsrv.example.com
sftp> ls -1 /backup/kapa
sftp> get /backup/kapa/kapa_backup_20260714_190000.tar.gz /tmp/
sftp> exit
```

**2. Stop the service:**

```bash
sudo systemctl stop kapa-dashboard
```

**3. Set the current (broken) state aside, just in case:**

```bash
sudo mv /opt/kapa/data /opt/kapa/data.broken.$(date +%Y%m%d)
sudo mkdir -p /opt/kapa/data
```

**4. Restore the backup:**

```bash
sudo tar -xzf /tmp/kapa_backup_20260714_190000.tar.gz -C /opt/kapa/data
sudo chown -R kapa:kapa /opt/kapa/data
sudo chmod 600 /opt/kapa/data/*.json*
```

**5. Start the service and verify:**

```bash
sudo systemctl start kapa-dashboard
journalctl -u kapa-dashboard -n 20
```

The log should read: `Cache geladen: ...`, `Reservierungen geladen: ... (N)`.
Then check in the dashboard: reservations present, administration shows the
roles, the "Log" tab contains the history. The capacity cache is refreshed
from Aria on the first auto-refresh anyway (after 30 minutes at the latest).

**6. Clean up** once everything looks good:

```bash
sudo rm -rf /opt/kapa/data.broken.* /tmp/kapa_backup_*.tar.gz
```

## Restoring a single file

If, for example, only an accidentally deleted reservation needs to come back,
the file can be extracted individually and imported via the import function
(replaces the current data!):

```bash
tar -xzf kapa_backup_....tar.gz kapa_reservierungen.json
```

Then, as an admin in the dashboard: "Import reservations (JSON)".

## Restoring on a new host

1. Clone the repository or copy `aria_kapa.py` **and the `frontend/` folder** to `/opt/kapa/`
2. Install as documented in the comments of `config/kapa-dashboard.service`
   (user, `/etc/kapa/kapa.ini`, `/etc/kapa/aria.pass`, nginx snippet)
3. Extract the backup into `/opt/kapa/data` as above — roles and
   reservations are back immediately
