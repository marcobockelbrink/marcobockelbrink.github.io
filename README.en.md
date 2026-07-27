# VMware Capacity Planning (Aria Operations)

> 🇩🇪 [Deutsche Fassung: README.md](README.md) — the German README is the
> primary document; this translation follows each release.

[![CodeQL](https://github.com/marcobockelbrink/vmware/actions/workflows/codeql.yml/badge.svg)](https://github.com/marcobockelbrink/vmware/actions/workflows/codeql.yml)
[![Security-Scan](https://github.com/marcobockelbrink/vmware/actions/workflows/security.yml/badge.svg)](https://github.com/marcobockelbrink/vmware/actions/workflows/security.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/marcobockelbrink/vmware/badge)](https://securityscorecards.dev/viewer/?uri=github.com/marcobockelbrink/vmware)
[![Docker-Image (GHCR)](https://github.com/marcobockelbrink/vmware/actions/workflows/docker.yml/badge.svg)](https://github.com/marcobockelbrink/vmware/actions/workflows/docker.yml)
[![Dependency-Review](https://github.com/marcobockelbrink/vmware/actions/workflows/dependency-review.yml/badge.svg)](https://github.com/marcobockelbrink/vmware/actions/workflows/dependency-review.yml)
![standard library only](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-success)

**▶ [View the live demo](https://marcobockelbrink.github.io/vmware/)** — static snapshot with sample data, no sign-in.

Per-cluster capacity reporting from VMware Aria Operations with a browser-based
dashboard and a reservation workflow for upcoming capacity requests.

![Dashboard with demo data](docs/screenshot.png)

*Capacity overview with demo data: free vCPU, RAM and storage capacity per
cluster with usage bars, source badge (multiple vROps), vROps quick filter in
the selector and column configuration (`python3 aria_kapa.py --sample --serve`).*

## Feature overview

A Python script + `kapa/` backend package + `frontend/` folder (standard library only, **no pip, no build**) that turns
Aria Operations data into a web dashboard. The UI language follows the browser:
German browsers get German, everything else English — including the login page,
the **API docs/OpenAPI spec** and the **CSV export** (headers/status values via
`Accept-Language` or `?lang=de|en`). The JSON API field names and status values
(v1 contract) and the stored data stay German. The audit log is stored in
German as the canonical record; in the English view the UI **does translate the
standard actions**, though (entered names, clusters and comments stay as typed).

**Capacity & reporting**
- **Multiple named vROps sources** (optional): 1–3 (or more) Aria Operations systems, each with or without a proxy, merged into one overview; every cluster carries a source badge (requires unique cluster names). The **single source** from `[kapa]` can be named too via `source-name`
- Free **vCPU / RAM / storage** per cluster with usage bars; free = capacity − used − approved reservations
- **N+1 failover reserve** (`--failover-hosts`), **vSAN factor** for usable net capacity, VM **exclusion by tag**, **minimum LUN size** and **name filter** (exclude datastores by name substring or `*`/`?` wildcards, e.g. ISO/backup, entirely; Administration → Storage)
- **Storage drill-down** per LUN/datastore (sortable), **vSphere tags** per cluster
- **Workload %** per cluster from vROps (hidden from requesters — also server-side)
- **Tanzu/Kubernetes namespaces**: CPU/RAM **reservations of vSphere namespaces**
  automatically count against free capacity (like approved reservations);
  per-cluster drill-down, `tanzu-mhz-per-vcpu` for the MHz→vCPU conversion
- **Cluster selector**: vROps source filter + up to 3 cascading tag filters; **filter/search fields**, **sortable tables**
- **Pagination** on all large lists (capacity, VLAN search, storage, reservations, approvals, archive, log): one shared bar with **100 / 200 / 300 entries per page** — a single mechanism for every table
- **Show/hide columns** in all data tables ("⚙ Columns", stored per user)
- **Light/dark mode**: ☀️/🌙 button in the header, stored per user; otherwise
  follows the system setting (`prefers-color-scheme`); `?theme=light|dark`
  overrides once (e.g. kiosk displays)
- **Auto-refresh** with estimated progress; export as **CSV/JSON**

**Network & VLAN**
- **Network tab** per cluster: port groups with VLAN numbers, searchable in place
- **VLAN search** across all clusters (IP/subnet/name → which cluster carries it)
- **Uplink/trunk port groups** (VLAN 0-4094) are hidden by default (`--show-uplink-portgroups`)

**Reservations & approval workflow**
- Capacity requests with optional **change/Jira ticket**; dedicated reservation page with **search field** and totals row; **configurable capa-ID format** (`id-prefix`/`id-length`)
- **Multi-stage approval** via teams (review order), approve/reject/cancel, status history
- **Auto-approval** (Administration → Auto-approval): if the target cluster meets
  configurable thresholds after subtracting the request (vCPU/RAM free %, largest
  free LUN %, workload ≤ %), **stages checked per team are approved automatically**
  (e.g. stage 1 manual, stages 2+3 automatic) — approver "Auto-Freigabe", badge
  "approved (auto)", every evaluation incl. numbers in the audit log; never blocks,
  never rejects
- **Archive** for rejected/cancelled requests (own menu item, permanent, same team visibility)
- **Capacity CSV import** (Administration → Import): upload your existing Excel
  list as CSV (Excel quirks like semicolons, BOM, `DD.MM.YYYY`, thousands dots
  are understood; columns matched by header). Entries arrive as **approved**
  (approver "Import"), validity from the original date; existing kapa numbers
  are skipped (no duplicates on re-import)
- **Statistics (trends)**: its own tab fed by daily snapshots of the data
  collection — avg RAM/vCPU/**disk per VM**, VM count, RAM/storage utilization
  as time-series charts (self-drawn SVGs, no CDN) plus **VM size classes** in a
  before/after comparison. Answers "are our VMs getting bigger?" with data.
  Range & cluster selectable, **CSV export**; history kept 2 years
  (`--history-days`), visibility per role via the matrix
- **Storage expansions** (dynamically enabled): approvers request — on approval
  or in the new **storage overview** (menu between VLAN search and reservations)
  — a **LUN expansion** or a **new LUN**; the storage team fetches the open
  requests **via the API** (`/api/v1/storage-requests`, also CSV **incl. NAA and
  the cluster's ESXi hosts with their FC-HBA WWPNs** – for zoning/LUN mapping and
  system identification when SAN names aren't maintained) and reports completion
  with a token "Storage" write permission. Configurable
  (Administration → Storage): **maximum size** per request (request limit),
  minimum LUN size and name filter
- Automatic **expiry** after `--res-ttl-days`; warning when a request exceeds free capacity

**Roles, AD & security**
- Roles **Admin / Reviewer / Requester / Auditor**, freely renamable; team-based visibility
- **Per-user/AD-group vROps source filter** (via checkboxes): an assignment can be restricted to specific sources (nothing ticked = all sources). The filter carries through **everything** the user sees — clusters, VLAN search, storage, reservations, capacity requests. Multiple group matches are merged **permissively (union)**; admins are restrictable too
- **AD sign-in** (LDAP, stdlib only), **AD groups** as permission subjects, recipient mail from a selectable **AD attribute**; **sessions survive service restarts** (release updates without re-login)
- Hardening: CSP/security headers, `Secure` cookies, **login throttle**, stored-XSS-safe rendering
- **Password detector** in the sign-in form: if the username input looks like a
  password (pasted from a password manager), it goes **neither to AD nor into the
  audit log** — the user gets a friendly hint instead

**Mail notifications**
- Configurable per internal role: **created / rejected / approved / "team's turn" / reminder**
- **Reminder mails** for stalled requests: if a request waits longer than
  x days (configurable, default 2) for its current approval stage, the team
  currently up (and/or the admin list) is reminded — then again every x days
  until decided
- Mixed recipients: requester automatically, admin/auditor via distribution list, teams via their own address
- Clean **HTML emails** (+ plain-text fallback)
- **Editable mail template** (Administration → Mail): subject + HTML fully customizable with
  `{{placeholders}}` (click inserts them at the cursor position), **live preview** with
  sample data in a sandboxed iframe, "insert default"; empty = built-in template

**Outbound webhooks** (Administration → Webhooks)
- Events (created/approved/rejected/"team's turn"/reminder, storage requests, offline
  import) trigger an **HTTP POST with JSON** to configurable targets — for **ITSM**
  (ServiceNow/Jira), **automation** (Ansible AWX, n8n), **pipelines** (GitLab/GitHub) or
  **chat** (Slack/Teams)
- Every payload is **HMAC-SHA256-signed** (`X-Kapa-Signature`) so the receiver can verify
  authenticity; per-target **test button**; non-blocking, audited; secrets never shown back
  and **not in the backup**. Details: [`config/WEBHOOKS.en.md`](config/WEBHOOKS.en.md)

**Administration (admin UI)**
- Sub-tabs **Users & roles / Cluster selector / Mail / Announcement / Approval / Visibility / Storage / Network / Import / API tokens / Backup & configuration**
- **Offline sources (cluster import)**: areas **without network access** to a
  vROps are exported with the bundled **PowerCLI script** (download in the
  import tab) and uploaded as JSON under a **fixed source name**. The clusters
  appear like a separate vROps source — incl. capacity requests, VLAN search
  and storage overview; same capacity math (overcommit, N+1, vSAN factor,
  name/VLAN filters). Data is static (import date as a tag on the cluster);
  **auto-approval deliberately excludes imported clusters**. Re-importing
  replaces the source; multiple sources can coexist
- **Visibility matrix**: per role (except admin), checkboxes control which
  features it sees — workload, host/VM lists, network & VLAN search, storage
  drill-down, vSphere tags, "decided by". Applies in the UI **and**
  server-side in the payload; permissions (approving, administration, team
  view) stay fixed to the roles
- **Announcement popup**: admins publish an announcement on demand (title +
  text, activatable) — every user sees it **once** after sign-in ("Got it"
  marker per user); changing the text shows it to everyone again. Ideal for
  release news, new datacenters or maintenance windows
- **Read-only configuration sheet** (all configured values, passwords only as "set: yes/no")
- **API tokens** for the v1 REST API; **write permissions per token, one click**
  (create/cancel reservations, approve/reject), one-click backup

**Operations & tech**
- Data stored as **JSON files or SQLite** (`--storage`), **atomic** writes
- **Performance**: gzip-compressed responses (page ~¼, JSON ~⅛ of the size) and a
  **VLAN cache** — port group VLANs are reused between refreshes (full re-read
  once a day) instead of one API call per port group
- **Tiered refresh intervals** (Administration → Backup & configuration): like a
  cron job, each area gets its own pace — **capacity** (VMs), **network** (port
  groups), **storage** (datastores), e.g. 60/180/360 minutes; skipped areas keep
  the state of the last run. The ⟳ button additionally offers admins **"refresh
  everything / capacity only / network only / storage only" right now** (per-area
  timestamps shown)
- **Configurable display time zone** (same page): all displayed times (last
  update, log, sync times, mail timestamps) appear in the chosen IANA zone
  (e.g. `Europe/Berlin`, incl. automatic daylight saving); empty = server time
  zone. The refresh schedule itself counts in seconds and is unaffected
- **SFTP backup** with rotation, **audit log** (JSONL, rotating), **`/healthz`**
  for monitoring (no sign-in: status, data age, cluster count)
- **One INI** for all non-secret settings, secrets as `.pass` files; optional **Aria proxy**
- Ships as **Docker/Podman** or **Kubernetes** (manifests + Helm chart; ready-made image on **GHCR**, built automatically on every release) — classic **systemd + nginx** still possible

Details for each area in the sections below. The **overall architecture with
diagrams** (system context, data flows, workflow, deployment) lives in
[`docs/ARCHITECTURE.en.md`](docs/ARCHITECTURE.en.md).

## Dashboard

- **Compact table view**: free **vCPU, RAM and storage** capacity per cluster
  (after subtracting approved reservations) with usage bars. The calculation
  notes sit behind the "ℹ How capacity is calculated" and "? Help" buttons.
- **Detail card with tabs**: clicking a cluster name opens the details, split
  into **CPU & RAM** (usage, key figures, reservations incl. request form and
  the cluster's **vSphere tags** below), **Storage** (usage and every LUN,
  sortable by size/usage), **Network** (the cluster's port groups with their
  VLAN numbers, **searchable in place by IP/VLAN/name**) and — admins and
  auditors only — **Hosts** and **VMs** (reviewers and requesters do not see
  these tabs; the lists are also stripped from the server payload). Clicking the storage value jumps straight to the storage tab. The
  card is wide and can be freely resized from its bottom-right corner.
- **VLAN search** (tab "VLAN search", between capacity and reservations):
  searches the port groups of **all** clusters. Because port group names
  contain the IP subnets, a partial input (e.g. `10.2.30` or `VLAN205`)
  immediately shows **which cluster** a network is attached to. Results as a
  sortable table of port group / VLAN / cluster.
  - **Network filter** (Administration → Network): hides port groups by
    **name** (partial match/wildcards, like the storage name filter) and/or
    **VLAN ID** (single IDs and ranges, e.g. `99, 3900-3999`) everywhere —
    VLAN search, cluster detail and data package.
- **Filter field** for clusters and reservations (also matches change number,
  requester, team, status and ID)
- **Cluster selector**: quick filters above the capacity list. With multiple
  vROps sources a **"vROps" filter comes first** (the source name from the
  INI); with a single source it is preselected. It is followed by up to three
  **cascading** dropdowns based on the vSphere tags (e.g. environment →
  location → operations team); tag values follow the chosen source. Level 2
  only shows values matching the level-1 choice. Which tag categories form the
  levels is freely configured in the "Administration" tab ("Cluster selector"
  sub-tab); each level can carry its own **display name** (e.g. category
  "Standort" → label "Data center"). Saved via "✓ Save selector". Values come
  live from the tags.
- **Sortable tables**: clicking a column header sorts ascending/descending
  (numeric, by date or text) — in all data tables (capacity, reservations,
  approvals, log, users/roles, tokens). Approval teams keep their manual
  review order.
- **Show/hide columns**: via the **"⚙ Columns"** button — in **all data
  tables** (capacity, reservations, approvals, archive, log, users/roles, API
  tokens, VLAN search) — individual columns can be hidden and shown again. The
  selection is stored **per user** (server-side when signed in, otherwise
  locally in the browser). The small config tables with fixed order (approval
  teams, role labels, cluster selector) deliberately keep their layout.
- **Dedicated reservation page** (tab "Reservations") with all **active**
  capacity requests, status and totals row (with search field)
- **Approval dashboard** (tab "Approvals"): approve or reject open requests
- **Archive** (tab "Archive"): rejected and cancelled requests as history
  (searchable, does not count against capacity). They are kept permanently and
  no longer appear in the active reservation list. **Visibility as with
  reservations**: requesters see their own team's, reviewers/admins/auditors
  see all.
- **Audit log** (tab "Log", admins only) with a **from/to date filter**
  (clicking the field opens a **calendar** in the browser language) and
  **pagination** (100 entries per page): records sign-ins (including failed
  ones), requests, approvals/rejections, cancellations, imports, role changes
  and backups to `data/kapa_log.jsonl`. The file **rotates automatically** at
  10 MB (`.1` … `.3`) and the view only reads the file tail — the log can
  neither grow without bounds nor slow down page loads.
- **Export**: reservations as **CSV** (semicolon, Excel-ready) or as JSON via
  the header buttons; the **capacity table** as CSV via the "Capacity as CSV"
  button (incl. effective free values after reservations and Tanzu; also via
  API: `/api/v1/data?format=csv`).
- **Deep links**: `#cluster=Name` in the URL opens the detail card directly —
  an open cluster automatically appears shareable in the address bar
  (handy for tickets and chats).
- **Auto-refresh** in serve mode (default: every 30 minutes, visible
  countdown) plus a "⟳ Refresh now" button

## Screenshots

All captures use demo data (`python3 aria_kapa.py --sample --serve`).
The UI is bilingual — German browsers see it in German, all others in English.

**Reservations** — all requests with ID, change number, team, vCPU/RAM/storage
and status: `requested`, `in review (n/3)`, `approved`, `rejected` and
`cancelled`. Every column sorts on click, "⦸ Cancel" withdraws a request:

![Reservations](docs/screenshot-reservierungen.png)

**Approvals** — open requests with the target cluster's free capacity
(⚠ marks requests that no longer fit), the progress of the multi-stage
approval and the button for the team currently up:

![Approvals](docs/screenshot-genehmigungen.png)

**Statistics** — trends from daily snapshots: avg RAM/vCPU/disk per VM,
VM count and utilizations as time series, plus the VM size classes in a
before/after comparison:

![Statistics](docs/screenshot-statistik.png)

**Administration** (admins only) — users **and AD groups** with role and team,
freely renamable role labels and the approval teams in their review order;
cluster selector, mail, announcement, auto-approval, visibility, API tokens
and configuration live in their own sub-tabs:

![Administration](docs/screenshot-verwaltung.png)

**Log** (admins only) — audit log with sign-ins, requests, approvals,
rejections, cancellations and backups:

![Audit log](docs/screenshot-log.png)

**Sign-in** with an Active Directory account:

![Login](docs/screenshot-login.png)

## Calculation

- **CPU capacity** = sum of physical cores of all ESXi hosts in the cluster × overcommit factor (default: 6)
- **RAM capacity** = sum of physical RAM of all hosts (1:1)
- **Storage capacity** = sum of the capacity of all datastores attached to the
  cluster hosts (vSAN **and** external FC LUNs). Mapping follows the host
  relationships in Aria (datastore → attached hosts → cluster); each datastore
  counts **exactly once per cluster**, even if all hosts see it (no
  double-counting of shared LUNs). If no capacity is reported, the column
  shows "–". The refresh logs the mapped datastores, per-cluster totals and
  detected storage types — useful for verification.
- **vSAN counts as usable capacity**: because the vSAN policy eats into usable
  capacity (RAID-1 ≈ 50 %, RAID-5 ≈ 75/67 %, RAID-6 ≈ 67 %), gross capacity only
  counts partially. **vROps provides no clean value** for this (only physical raw
  + used), hence a **configurable factor**: global as a policy dropdown **and
  overridable per cluster** (Administration → Storage), seeded via `--vsan-factor`
  (default `0.5`). It applies to **capacity and usage**; in the **% utilization it
  cancels out**, so that stays physically correct. The
  LUN list shows the **storage type** (vSAN/VMFS/NFS) and, for vSAN, the gross
  capacity in a tooltip. For FC/iSCSI LUNs the **NAA identifier** of the
  backing device (`naa.6000…`) is additionally read best effort — first from the
  datastore properties, otherwise from the datastore's **metric keys** (in many
  vROps versions the NAA is not a property but a metric instance in the
  **`Devices`** group, e.g. `Devices|naa.6000…|…`; the identifier is read
  straight from the key name). Shown in the LUN tooltip, included in the payload
  (`/api/v1/data`, field `naa` per datastore) and thus in the cache. For
  verification against your vROps the log lists both the inspected **properties**
  and the **metric keys** (`Datastore-Properties (Beispiel …)`,
  `Datastore-Metrik-Schlüssel (Beispiel …)`, `NAA erkannt: X/Y`) — if a datastore
  finds none, a related host is additionally checked for `naa.…` keys. The type comes from the datastore properties; if none
  is delivered, name-based detection kicks in.
  - **LUN detail**: clicking the storage value (or the cluster name) opens the
    detail card with **every single datastore/LUN** — sortable by **size** or
    **usage**, with size, used space, usage % and free space.
- **Failover reserve (N+1)**: per cluster, the largest host (cores and RAM) is
  subtracted from total capacity (`--failover-hosts`, default: 1, `0` = off);
  storage is unaffected.
- **Used** = provisioned vCPUs / RAM of all VMs, or used datastore space (incl. powered-off)
- **Free** = capacity − used − approved reservations (for vCPU, RAM and storage)
- **Exclusion by tag**: with `--exclude-tag Kapa_Filter:Ja`, VMs carrying the
  given vROps tag (category:value) are excluded from usage.
- **vSphere tags**: cluster tags come from the resource **properties**
  (`/resources/{id}/properties`) and are shown as chips in the detail card
  ("CPU & RAM" tab). By default all properties whose key contains `tag` are
  used; `--tag-property` narrows this to a prefix (e.g. `summary|tag`). If a
  property contains **JSON** (e.g. `TagJson`), it is unpacked and only the
  tags are listed — raw JSON never reaches the display. After each refresh the
  log lists the detected keys and an excerpt of the raw value — handy for
  fine-tuning.
- **dvSwitches / port groups**: Aria delivers distributed switches
  (`VmwareDistributedVirtualSwitch`) and port groups
  (`DistributedVirtualPortgroup`) as separate resources. Cluster mapping
  works — as with storage — via the attached hosts
  (dvSwitch → HostSystem → `summary|parentCluster`); the VLAN number is read
  best effort from the port group properties (per port group via
  `/resources/{id}/properties`, any key containing "vlan"). VLANs of known port
  groups come from the **cache of the previous refresh** (a full re-read runs
  once a day — log: `VLANs: X aus Cache, Y per API`). If the fetch
  fails, the network tab stays empty and everything else keeps working. The
  log reports `dvSwitches: N, Portgruppen: M · zugeordnet: …` — check there
  after the next refresh.
  **Uplink/trunk port groups** (name contains "uplink" or the VLAN is a wide
  trunk range like `0-4094`) are not real network VLANs and are **hidden** by
  default; `--show-uplink-portgroups` brings them back.
- **Workload %**: the vROps workload badge per cluster (`badge|workload`) is
  read best effort and shown as a key figure in the cluster detail card —
  **hidden from the requester role** (neither in the UI nor in the payload).
  Log: `Cluster-Workload gelesen: N/M`.
- **Tanzu/vSphere namespaces**: if vSphere with Tanzu runs on a cluster, the
  **namespace reservations** (CPU/RAM) are read from vROps and count — like
  approved manual reservations — against free capacity. The CPU reservation
  arrives in **MHz** and is converted to **vCPU equivalents** (rounded up) via
  `tanzu-mhz-per-vcpu` (default 2500, `0` = do not count CPU). The detail card
  shows a dedicated "Tanzu namespaces" section listing every namespace (MHz,
  vCPU equivalent, RAM) plus the "of which Tanzu namespaces" key figure. TKG
  worker VMs are already included in usage as regular VMs — the namespace
  reservation additionally covers committed capacity that has not
  materialized yet (deliberately conservative). The fetch is best effort:
  resource kind and stat keys vary by vROps version, so the system probes
  candidates and logs the outcome (`Tanzu: … Namespaces gefunden`,
  `erkannte Schlüssel: …`) — check the log against your real vROps after the
  first refresh. Without Tanzu the fetch finds nothing and changes nothing.
- **Cluster detail card everywhere**: clicking a **cluster name** opens the
  detail card — not only in the capacity overview but also in reservations,
  approvals and the VLAN search.
- The calculation notes and help live behind the **"ℹ How capacity is
  calculated"** and **"? Help"** buttons (tidy header).

## Usage

Only Python 3.8+ required, no extra packages — runs directly on any Linux host.

**Server mode** (recommended): the page loads instantly from the file cache
(`data/kapa_cache.json`); on the very first start without a cache the data is
fetched automatically. After that it refreshes every 30 minutes or on demand:

```bash
python3 aria_kapa.py --url https://aria-ops.example.com --user admin --insecure --serve
# Dashboard: http://localhost:8080  ·  Reservations: http://localhost:8080/reservierungen
```

**One-off snapshot** (static HTML, reservations then live in the browser only):

```bash
python3 aria_kapa.py --url https://aria-ops.example.com --user admin --insecure
```

**Demo without an Aria connection:**

```bash
python3 aria_kapa.py --sample                # static
python3 aria_kapa.py --sample --serve        # server mode
```

A pre-built demo ships in the repo as
[`kapa_dashboard_demo.html`](kapa_dashboard_demo.html) — download it and open
it in a browser, no installation needed (reservations then live in the
browser's localStorage only).

## Reservations (capacity requests)

Create via the dialog ("+ New capacity request") or directly in a cluster's
detail card; export/import as JSON.

- **Unique ID**: every request automatically receives a unique ID on creation
  (12 characters, format configurable via `id-prefix`/`id-length`). It appears
  as the first column in the "Reservations" and "Approvals" tables and also in
  the report email, the CSV export (`/api/v1/reservations?format=csv`) and the
  audit log — every request can be referenced unambiguously.
- **Change / Jira ticket (optional)**: every request can carry a change number
  or Jira ticket — free-form, no fixed format, **not mandatory**. The value
  appears in the overviews and the report email; if absent, "–" is shown.
- **Resources**: each request records **vCPU**, **RAM (GB)** and
  **storage (GB)** as **integers**. vCPU and RAM count against calculated
  cluster capacity; the storage size is carried with the request and shown
  everywhere.
- **Validity**: reservations are automatically valid from their creation day
  for 30 days; the "valid until" date is shown on every reservation.
- **Multi-stage approval process**: with teams configured, every request
  passes through them **in order**. The status moves from "requested" →
  "in review" (once the first team has approved) → "approved" (only when
  **all** teams have approved). Only then does the request count against
  capacity. While **"in review"**, a mouseover shows which teams (with person
  and date) have already approved and which team is next. A team can only
  approve when it is its turn; every team can also reject at its stage.
  Without teams the process is single-stage (admin approves directly).
  - **Managing teams**: in the "Administration" tab ("Approval teams"
    section) — add, order via ↑/↓, **rename** (✎, the order is kept and
    assigned reviewers move along automatically) and remove. Stored in
    `data/kapa_teams.json`. The `--approval-teams` parameter only serves as
    **initial seeding** if that file does not exist yet.
  - **Assigning reviewers to a team**: when assigning roles ("Users and
    roles" section), the *Reviewer* role picks its team from a **dropdown** of
    existing teams. Only users assigned this way may approve at that stage
    (enforced server-side).
- **Approval overview** (tab "Approvals"): shows per request the free capacity
  of the target cluster (⚠ if it no longer fits), the progress and — for the
  team currently up, and admins — the approve/reject buttons. At the top a
  **reviewer handbook** (its own bilingual doc page at `/reviewer-handbuch`,
  also in the "? Help" popup) links the key workflows for reviewers — extendable
  later.
- **User guide** for all roles (`/benutzerhandbuch`, English `/user-guide`):
  step-by-step from signing in through "requesting capacity" to
  cancel/archive — bilingual, uses the configured role names, linked in the
  footer and the "? Help" popup.
- **Rejections** stay visible for 31 days (from rejection) as history
  (status "rejected"; the mouseover shows at which stage).
- **Cancellation**: requests cannot be deleted, only **cancelled**. Allowed
  for an admin, the requester, or **someone from the same team** ("⦸ Cancel"
  button in the reservation list). A cancelled request gets status
  "cancelled", stays as history and no longer counts against capacity.
- **Comment**: when approving/rejecting/cancelling, a comment (e.g. a reason,
  **max. 64 characters**) can be captured via a lean dialog; it appears in the
  reservation overview and the report email.
- **Decided by**: the overview shows which admin approved or rejected — hidden
  from requesters (column and data field are stripped server-side); admins and
  auditors see it.
- **Mail notifications** (requires an SMTP server via `--smtp-server`): in
  **Administration** you define per internal role which event triggers an
  email — **created**, **rejected**, **approved** (final approval) and
  **"team's turn"** (a team is up in the approval workflow). Recipients:
  - **Requester** → the respective requester (automatic). By default the
    sign-in name (UPN) serves as the address; with `--ad-mail-attribute mail`
    (or similar) a freely selectable **AD attribute** is read instead
    (requires the `--ad-bind-dn` service account; resolved at sign-in and
    stored with the reservation),
  - **Admin/Auditor** → a freely entered distribution address each
    (admin falls back to `--smtp-to` if the field is empty),
  - **"Team's turn"** → the address stored per approval team.

  The email contains the reservation data; delivery is asynchronous and
  best-effort (errors only in the log). On **creation** of a request the first
  team is notified automatically, after each approval the next one.

  **Editing the mail template:** in the same tab, the **subject** and **HTML
  body** of the emails are fully customizable. Available variables (e.g.
  `{{name}}`, `{{cluster}}`, `{{vcpu}}`, `{{ram_gb}}`, `{{approvals}}`,
  `{{von}}`, `{{action}}`) appear as clickable chips and are inserted at the
  cursor position; values are HTML-escaped server-side (the admin's layout
  HTML is preserved). "**Preview**" renders the template with sample data in
  an isolated `sandbox` iframe, "**Insert default**" loads the built-in
  template. Empty fields = built-in default. Stored in `kapa_mail.json`
  (no password), changes are audit-logged.
- **Serve mode**: reservations live centrally on the server in
  `data/kapa_reservierungen.json` — every user sees the same state.
- **Static HTML**: stored locally in the browser (localStorage).
- **Automatic expiry**: reservations are deleted `--res-ttl-days` days after
  creation (default: 31, `0` = never); the displayed validity ends one day
  earlier (30 days).

## Data store

All writes are **atomic** (write to a temp file first, then rename), so a
crash mid-save can never leave a half-written, corrupted file. `--storage`
(or `storage =` in the INI) selects the backend:

- **`json`** (default): one readable, hand-editable `.json` file per
  collection in `--data-dir` (`kapa_reservierungen.json`, `kapa_rollen.json`,
  `kapa_teams.json` …). Entirely sufficient for normal operation.
- **`sqlite`**: a single `data/kapa.db` (SQLite ships with the Python
  standard library — **no** extra module, no server, no port). Reservations
  are written **incrementally** (only the changed row instead of the whole
  list); the small collections live as key-value entries in the same file.
  Worth it only with very many (several thousand) active reservations.

On the **first switch** to `sqlite` the dashboard migrates existing JSON data
**automatically once** into the new `kapa.db` (roles, teams, selector, role
labels, tokens and all reservations). The JSON files remain as a safety net —
switching back to `json` stays possible at any time. The audit log
(`kapa_log.jsonl`) and the Aria cache remain separate files in both modes.

## API for external applications

Under `/api/v1/` there is a stable REST API for external
applications (Grafana, CMDB, reporting …). Admins create named bearer tokens
in the "Administration" tab (shown only once, only the hash is stored,
individually revocable, usage audit-logged):

```bash
curl -H "Authorization: Bearer kapa_..." \
  "https://host/capa/api/v1/reservations?status=genehmigt&format=csv"
```

Read endpoints: `/api/v1/reservations` (filters: `cluster`, `status`,
`abteilung`; `format=csv`), `/api/v1/data` (cluster capacities),
`/api/v1/status`. **Write** (write permissions per token, one click in the
administration, audit-logged): `POST /api/v1/reservations` (create) and
`…/{id}/cancel` with the "Reservations" permission, `…/{id}/approve` and
`…/{id}/reject` with the "Approvals" permission — details in
[`config/API.en.md`](config/API.en.md).

**Language:** JSON field names and status values are part of the stable v1
contract and stay German (`status=genehmigt` etc.). **CSV headers/status
values** and the **OpenAPI descriptions**, however, follow `Accept-Language`
(or explicitly `?lang=de|en`) — requests without the header (curl, scripts)
keep getting German, so existing consumers see no change.

**Interactive docs in the dashboard**: at **`/api/v1/docs`** (also linked from
"Administration → API") — a self-contained, offline-capable
Swagger-style page with a "Run" button per endpoint. The machine-readable
**OpenAPI 3.0 spec** lives at **`/api/v1/openapi.json`** and imports into
Swagger Editor, Postman etc. Text version: [`config/API.en.md`](config/API.en.md).

## Role concept and AD sign-in

With `--ad-url`, serve mode requires signing in with an Active Directory
account (LDAP simple bind, standard library only):

```bash
python3 aria_kapa.py --url https://aria-ops.example.com --user svc-aria --serve \
  --ad-url ldaps://dc01.example.com --ad-domain example.com \
  --admin-user first.last@example.com
```

| Role | Permissions |
|---|---|
| **Requester** | Submit capacity requests; withdraw own still-open requests; sees only requests of the **own team**, not who decided; no host/VM lists, no workload (default — adjustable via the visibility matrix) |
| **Reviewer** | Member of an approval team; approves or rejects requests **when their team is up** ("Approvals" tab); sees all requests but no administration/log and no host/VM lists (default — adjustable via the visibility matrix) |
| **Administrator** | Approve/reject at any stage (with comment), refresh Aria data, manage all reservations, import, manage roles/teams ("Administration" tab); sees everything |
| **Auditor** | View all data and pages — no changes possible |

- **Assigning roles**: "Administration" tab — enter the AD user name, pick a
  role and, for **requesters and reviewers** alike, the **team** (one of the
  approval teams managed in the same tab, via dropdown); admin and auditor
  need no team. Stored in `data/kapa_rollen.json`. Existing assignments can be
  edited or removed with a click.
- **Team visibility (requesters only)**: a **requester** only sees the
  requests of the **own team** in the reservation list (foreign approved ones
  stay included anonymized as "(other team)" so free capacity stays correct).
  **Reviewers, admins and auditors see all** requests — the multi-stage
  approval process is unaffected.
- **Default role**: every user who successfully signs in against AD
  **without** an explicit assignment automatically counts as a **requester** —
  they can submit requests but approve nothing. Reviewer, admin and auditor
  rights only exist through an explicit assignment.
- **Authorizing AD groups**: administration can also assign a role (and team)
  to an entire **AD group** (type "AD group") — exactly like a user. Every
  member of the group then receives that role. This requires a **service
  account** (`--ad-bind-dn`/`--ad-bind-password`/`--ad-base-dn`) with which
  the system looks up the user's AD groups (`memberOf`) after sign-in.
  Directly assigned user roles take precedence; with multiple groups the
  highest permission wins.
- **AD group check**: next to each AD group, administration shows a **"👥
  Members"** button. A click looks the group up live in the directory and shows
  its **direct user members** (name · UPN) in a popup — handy to verify that the
  service account, base DN and group name resolve cleanly. Uses a single search
  (`(memberOf=…)`); nested groups are not resolved.
- **Renaming role labels**: the displayed names of the four roles are freely
  changeable in the "Administration" tab ("Role labels" section), stored in
  `data/kapa_rollennamen.json`. Internal role keys and therefore
  **permissions stay unchanged** — only the display changes.
- **Bootstrap**: `--admin-user` (comma-separated) defines always-admins so the
  first admin can open the administration.
- User names without `@` are automatically completed with `--ad-domain`
  (`max` → `max@example.com`).
- All permissions are enforced **server-side**; the UI additionally hides
  disallowed actions.
- Use `ldaps://` — with `ldap://` passwords cross the network unencrypted
  (`--ad-insecure` for self-signed certificates).
- Without `--ad-url` everything runs as before without sign-in (full access).

### Hardening

- **Session cookie** with `HttpOnly`, `SameSite=Lax` and `Secure`. Since the
  dashboard runs behind the HTTPS nginx, `Secure` is the default; only for a
  local HTTP test without a proxy can it be disabled via `--cookie-insecure`.
- **Security headers** on every response: `Content-Security-Policy`,
  `X-Frame-Options: DENY` (no clickjacking), `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: same-origin`.
- **Output escaping**: names coming from Aria (clusters, hosts, VMs) are
  embedded script-tag-safe so they cannot inject JavaScript.
- **Login throttle**: after 5 failed attempts per user, sign-in is blocked for
  a few minutes with `429` (protection against password spraying). A uniform
  error message does not reveal which accounts are authorized. AD outages
  deliberately do not count as failed attempts.
- **Request size** is limited (2 MiB) so a large body cannot overload the
  service.

## Configuration (one simple model)

There is deliberately **one** configuration file and one clear principle —
every setting has **exactly one source**:

| What | Where |
|---|---|
| **All non-secret settings** (Aria URL/user, calculation, network, mail server, backup target, AD connection, server port …) | **`kapa.ini`** (template: [`config/kapa.ini.en.example`](config/kapa.ini.en.example)) |
| **Secrets** (passwords, SSH key) | separate **`.pass` files** (root:kapa, `0640`); the INI only names the **path** (`password-file`, `ad-bind-password-file`, …) |
| **Business data** (roles, teams, mail rules, selector, tokens) | **admin UI** → data store under `--data-dir` |

```bash
python3 aria_kapa.py --config /etc/kapa/kapa.ini --serve
```

The systemd unit calls exactly that — there is **no `kapa.env`** anymore that
could override values. Command-line arguments override the INI (for ad-hoc
tests); unknown keys are rejected with an error. The configured values can be
inspected read-only in the admin UI under **"Backup & configuration"**
(passwords only as "set: yes/no").

> Migration from older versions (kapa.env + KAPA_EXTRA_ARGS): move the values
> from `kapa.env` into `kapa.ini`, put passwords into `.pass` files and
> reference their paths in the INI, then install the simplified unit.
> `kapa.env` is no longer needed (details: [`config/kapa.env.example`](config/kapa.env.example)).

**SFTP backup**: with `--backup-target backup@srv:/backup/kapa` the data files
(reservations, roles, audit log, cache) are regularly transferred as `tar.gz`
via scp — default: **twice a day** (`--backup-interval 43200`). **Rotation**:
archives older than 30 days are deleted automatically on the target
(`--backup-keep-days`, via sftp, works on sftp-only servers too).
Authentication preferably via SSH key (`--backup-key`); a password
(`--backup-password` or `BACKUP_PASSWORD`) only works with `sshpass`
installed. Admins can trigger a backup **manually** at any time — in the
"Administration" tab ("Backup" section) or directly via `POST /api/backup`.
Results (including errors) land in the audit log.

**Restore**: step-by-step guide in [`config/RESTORE.en.md`](config/RESTORE.en.md).

## Options

| Option | Description |
|---|---|
| `--config kapa.ini` | Load all options from an INI file |
| `--cpu-factor 6` | CPU overcommit factor |
| `--failover-hosts 1` | Failover hosts per cluster (N+1), `0` = off |
| `--auth-source local` | Auth source (e.g. AD source) |
| `--insecure` | Skip TLS certificate verification (self-signed) |
| `--aria-proxy http://proxy:3128` | optional HTTP(S) proxy for the Aria requests (locked-down environments) |
| `--serve --port 8080` | Web server mode |
| `--bind 0.0.0.0` | Bind address for `--serve` |
| `--refresh-interval 1800` | Auto-refresh in seconds (`0` = off) |
| `--data-dir /var/lib/kapa` | Base folder for all runtime data (default `data/`); with CI/CD choose a path outside the deploy directory |
| `--cache kapa_cache.json` | File cache of the last fetch |
| `--res-file data/kapa_reservierungen.json` | Reservation file (serve mode) |
| `--res-ttl-days 31` | Delete reservations after N days (`0` = never) |
| `--id-prefix KAPA-`, `--id-length 12` | Capa-ID format: prefix + N random hex characters |
| `--exclude-tag Kapa_Filter:Ja` | Exclude VMs with this vROps tag (category:value) from the evaluation |
| `--contact-info "…"` | Contact/imprint line (footer + login) for inquiries |
| `--ad-bind-dn`, `--ad-bind-password`, `--ad-base-dn` | Service account for AD group authorization (memberOf lookup) |
| `--approval-teams "A,B,C"` | **Initial seeding** of the approval teams (only if `--teams-file` is missing); manage in "Administration" afterwards |
| `--teams-file data/kapa_teams.json` | File with the approval teams (managed via the admin page) |
| `--rolenames-file data/kapa_rollennamen.json` | File with the freely renamable role labels (managed via the admin page) |
| `--ad-url ldaps://dc01…` | Enable AD sign-in |
| `--ad-domain example.com` | Domain for user names without `@` |
| `--login-hint "…"` | Placeholder in the username field of the sign-in form (default: "Active-Directory-Benutzername") |
| `--ad-insecure` | Skip LDAPS certificate verification |
| `--cookie-insecure` | Session cookie without `Secure` (local HTTP test only) |
| `--admin-user a@…,b@…` | Always-admins (bootstrap) |
| `--roles-file data/kapa_rollen.json` | Roles file |
| `--smtp-server mail.example.com:25` | Mail server for reports |
| `--smtp-from`, `--smtp-to` | Sender / report recipients (comma-separated) |
| `--smtp-user`, `--smtp-password`, `--smtp-tls` | SMTP auth / STARTTLS |
| `--backup-target user@srv:/path` | SFTP/SCP backup target |
| `--backup-key`, `--backup-password` | SSH key (recommended) or password (needs sshpass) |
| `--backup-port 22`, `--backup-interval 43200` | SSH port / backup interval in s (2×/day) |
| `--backup-keep-days 30` | Rotation: delete older archives on the target |
| `--password-file file` | Aria password from a `.pass` file (path belongs in the INI) |
| `--ad-bind-password-file`, `--smtp-password-file`, `--backup-password-file` | same for the AD service account / SMTP / backup |
| `--log-file data/kapa_log.jsonl` | Audit log file |
| `--tokens-file data/kapa_tokens.json` | API token file |
| `--output file.html` | Output file (static mode) |
| `--json file.json` | Raw data additionally as JSON |

All JSON data files (cache, reservations, roles, teams, log, tokens, `--json`
export) live in the `data/` folder by default, which is fully excluded from
the repository via `.gitignore`. The base folder is freely selectable via
`--data-dir`; explicit paths (e.g. `--cache /path/cache.json`) are respected.

> **Important with CI/CD (GitLab pipeline etc.):** put the runtime data
> **outside** the deploy directory via `--data-dir` (e.g. `/var/lib/kapa`).
> `data/` is gitignored, i.e. not part of the repository/artifact. If the
> pipeline deploys over the target directory (via `git clean -fdx`,
> `rsync --delete` or "empty and refill"), it deletes the adjacent `data/`
> folder on **every** deploy. Under `/var/lib/kapa` the data stays untouched.
> The shipped systemd unit is already configured that way.

## Running on a Linux host (systemd + nginx)

Ready-made templates live under [`config/`](config/):

- **`config/kapa-dashboard.service`** — systemd unit: runs as its own `kapa`
  user under `/opt/kapa`, binds only to `127.0.0.1:8080`, restarts on
  failure, hardened sandbox. Simply calls `--config /etc/kapa/kapa.ini
  --serve` (no `EnvironmentFile`, no `${VARS}`). Installation steps are
  documented as comments in the file.
- **`config/kapa.ini.example`** (English: `kapa.ini.en.example`) — the single configuration file
  (`/etc/kapa/kapa.ini`, mode 640): Aria, calculation, network, server, AD,
  mail, backup. Recommendation: use a dedicated **read-only service account**
  in Aria Operations — the script only reads.
- **Passwords as separate `.pass` files** (root:kapa, `0640`); the INI only
  names the path. Example for Aria (same for `ad_bind.pass`, `smtp.pass`,
  `backup.pass`):
  ```bash
  sudo sh -c 'echo "THE-ARIA-PASSWORD" > /etc/kapa/aria.pass'
  sudo chown root:kapa /etc/kapa/aria.pass && sudo chmod 640 /etc/kapa/aria.pass
  ```
  then set `password-file = /etc/kapa/aria.pass` in the INI. The password
  never shows up in `ps aux` or `systemctl show`. Precedence everywhere:
  parameter > password file > environment variable (`ARIA_PASSWORD` etc. as
  an optional fallback, see `config/kapa.env.example`).
- **`config/nginx-kapa.conf`** — snippet for the existing 443 server: serves
  the dashboard at `https://<host>/capa/` (redirect `/capa` → `/capa/`,
  prefix stripping, cookie path). The web UI uses relative API paths and thus
  works unchanged under the sub-path. Include it, then
  `nginx -t && systemctl reload nginx`.

Without `--ad-url` the built-in web server has no authentication — only run
it in a trusted management network then. TLS is the reverse proxy's job; the
dashboard itself speaks plain HTTP on localhost.

The running version is shown in the web UI footer and via
`aria_kapa.py --version`.

### Smoke tests

An end-to-end test suite (standard library only, no pytest) runs against a
freshly started sample server before every release:

```bash
python3 tests/smoke.py     # 23 checks, ~15 s, exit code 0 = green
```

Covered: gzip, free-text hardening, token write permissions incl. two-stage
approval, CSV/language, OpenAPI, mail rules/template, announcement, prefs
and the INI section guard.

### Delivery: Docker & Kubernetes

Besides the manual installation from `config/` (systemd + nginx), ready-made
container variants live under [`deploy/`](deploy/):

- **[`deploy/docker/`](deploy/docker/)** — container image based on Red Hat
  UBI 9 (runs as non-root, works with Podman too) incl. `docker-compose.yml`.
  On every release the image is built automatically and published as a
  **GitHub package**: `docker pull ghcr.io/marcobockelbrink/kapa-dashboard:latest`
  (amd64 + arm64, fixed version tags for rollbacks).
- **[`deploy/kubernetes/`](deploy/kubernetes/)** — cluster operation via
  **plain manifests** (`kubectl apply`: deployment, service, PVC, ConfigMap
  INI, secret, ingress example) or the **Helm chart**
  (`deploy/kubernetes/helm/kapa-dashboard`). Single replica with a PVC,
  liveness/readiness on `/healthz`, OpenShift-compatible.

The former RPM/Ansible variants were retired with v2.10 (last in v2.9.1).
Details and the decision guide: [`deploy/README.en.md`](deploy/README.en.md).
