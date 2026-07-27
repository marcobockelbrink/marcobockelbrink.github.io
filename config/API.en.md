# API for external applications (v1)

> 🇩🇪 [Deutsche Fassung: API.md](API.md)
>
> **Interactive in the dashboard:** `/api/v1/docs` (Swagger-style page with
> "Run", works offline) · **OpenAPI spec:** `/api/v1/openapi.json`
> (import into Swagger Editor/Postman). This file is the text version.
> Note: JSON field names and status values are German — they are part of the
> stable v1 contract.

The dashboard serves its API at `/api/v1/` — since v3.0 **one path space
for everything**: the endpoints described here form the stable contract for
external applications (**bearer token**); in addition the same path space
serves the entire web UI via session cookie (administration endpoints
deliberately require a sign-in, never a token). Admins create bearer tokens
in the "Administration → API" tab. The token is shown only once;
only a SHA-256 hash is stored. Tokens are individually revocable; every
creation/revocation and invalid access lands in the audit log.

```bash
TOKEN="kapa_..."
BASE="https://host/capa"     # or http://localhost:8080 without a proxy

curl -H "Authorization: Bearer $TOKEN" $BASE/api/v1/reservations
```

## Endpoints

### GET /api/v1/reservations

All reservations (unfiltered, like the admin view). Fields per entry:

| Field | Meaning |
|---|---|
| `id` | unique ID |
| `name` | name / project |
| `change` | change number / Jira ticket (e.g. `OPS-4711`, `INFRA-1042`) |
| `cluster` | target cluster |
| `vcpu`, `ram_gb`, `storage_gb` | requested capacity (storage informational only) |
| `von`, `abteilung` | requester and team/department |
| `created` | valid from (creation day, ISO date) |
| `approvals` | list of team approvals so far: `[{team, by, on, comment}]` (order = review order) |
| `approved`, `approved_on`, `approved_by` | fully approved (all stages), date + last approver |
| `rejected`, `rejected_on`, `rejected_by`, `rejected_team` | rejection status incl. stage |
| `cancelled`, `cancelled_on`, `cancelled_by` | cancellation (by team/requester/admin) |
| `comment` | last comment |

The derived status is: `abgelehnt` (rejected), else `storniert` (cancelled),
else `genehmigt` (approved), else `in Prüfung` (at least one but not all
approvals), else `beantragt` (requested).

**Filters** (combinable):

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/reservations?cluster=Cluster-01&status=genehmigt&abteilung=IT"
```

- `cluster=<name>` — a single cluster only
- `status=beantragt|in Prüfung|genehmigt|abgelehnt|storniert`
- `abteilung=<name>`

**CSV export** (semicolon-separated, Excel-ready):

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/reservations?format=csv" -o reservations.csv
```

CSV headers and status values follow `Accept-Language` (or explicitly
`?lang=de|en`); requests without the header get German. With `lang=en`:
`id;name;change;cluster;vcpu;ram_gb;storage_gb;requested_by;team;`
`valid_from;valid_until;status;decided_by;approvals;comment`

### GET /api/v1/data

Cluster capacities from the last Aria refresh:

```json
{"updated": "14.07.2026 19:00",
 "clusters": [{"name": "Cluster-01", "hostCount": 3, "cores": 64,
               "spareCores": 48, "spareRamGb": 1024.0,
               "vcpuCap": 384, "vcpuUsed": 370, "vcpuFree": 14,
               "ramCap": 1024.0, "ramUsed": 1504.0, "ramFree": -480.0,
               "storageCap": 24000.0, "storageUsed": 17826.0, "storageFree": 6174.0,
               "tanzuVcpu": 12, "tanzuRamGb": 128.0,
               "namespaces": [{"name": "ns-webshop-prod", "cpu_mhz": 20000,
                               "vcpu": 8, "ram_gb": 96.0}],
               "vmCount": 93, "vmOff": 8, "hosts": [...], "vms": [...]}]}
```

Note: `vcpuFree`/`ramFree` are **before** subtracting approved reservations;
the reservations come from `/api/v1/reservations` (status `genehmigt`).

**CSV export** (`format=csv`, semicolon; headers follow `Accept-Language`
or `?lang=de|en`): additionally with `reserved_*`, `tanzu_*` and
`*_free_effective` columns — the free capacity **after** approved
reservations and Tanzu namespaces, as in the UI.

### GET /api/v1/status

```json
{"version": "3.1.1", "updated": "27.07.2026 12:00",
 "refreshing": false, "next": 1234,
 "progress": "", "error": null,
 "tiers": {"vms": "27.07.2026 12:00", "network": "…", "storage": "…"}}
```

`next` = seconds until the next automatic refresh.

### GET /healthz

Monitoring endpoint **without authentication** (deliberately only
uncritical operational data): `status` (ok/error), `version`, `updated`,
`data_age_seconds`, `refreshing`, `clusters` (count), `error`.
Lives outside `/api/v1/` and is meant for uptime checks.

## Error codes

| Code | Meaning |
|---|---|
| 401 | no or invalid/revoked token |
| 404 | unknown endpoint |

## Notes

- The v1 paths stay stable; extensions arrive as new fields or parameters,
  existing fields do not change.
- **Language:** JSON field names and status values stay German (stable
  contract). CSV headers/status values and the OpenAPI descriptions follow
  `Accept-Language` or `?lang=de|en`; without the header (curl/scripts)
  German, unchanged.
- Browser sessions (signed-in admins) can call the v1 endpoints too, e.g.
  for testing.

### GET /api/v1/storage-requests

Requested **storage expansions** for the storage team — LUN expansions and new
LUNs, incl. the **NAA identifier**. Default: open requests; `status=alle` for
all, `status=erledigt` for completed. As **CSV** with `format=csv` — ready to
feed into storage automation.

## Write endpoints (write permissions per token)

Tokens are **read-only** by default. In the administration ("API tokens"
section) two write permissions can be enabled **per click** for each token;
every change lands in the audit log:

| Write permission | Endpoints |
|---|---|
| **Reservations** | `POST /api/v1/reservations` (create), `POST /api/v1/reservations/{id}/cancel` (cancel) |
| **Approvals** | `POST /api/v1/reservations/{id}/approve` (approve the current stage), `POST /api/v1/reservations/{id}/reject` (reject) |
| **Storage** | `POST /api/v1/storage-requests/{id}/done` (mark a storage expansion as done) |

```bash
# Create (status "beantragt", passes through the normal workflow):
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"SAP expansion","cluster":"Cluster-01","vcpu":8,"ram_gb":64,
       "storage_gb":500,"von":"cmdb@example.com","abteilung":"Network team"}' \
  $BASE/api/v1/reservations

# Approve / reject / cancel (comment optional, max. 64 characters):
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"comment":"looks good"}' $BASE/api/v1/reservations/KAPA-1a2b3c/approve
```

Notes:

- API decisions act like **admin decisions** (no team restriction); `approve`
  approves the **current stage** — the request is fully approved only once
  all stages have signed off.
- The actor in the audit log and mails is `api:<token name>`; `POST
  /api/v1/reservations` accepts `von` (requester) and `abteilung` (team, for
  team visibility).
- Mail notifications fire exactly like for UI actions.
- Errors: `403` = token lacks the required write permission, `404` = request
  not found or already decided.
