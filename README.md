# EuroBonus Award Finder

Self-hosted, single-user finder for **SAS EuroBonus award flights** (SAS-operated metal). It reads
SAS's public award-finder feed, pairs outbound/return legs into bookable **round-trips**, prices them
from a maintained points/taxes zone table, and flags **SAS Amex 2-for-1 voucher**-eligible trips.

> **Status: Phase 4 (Value + fallback) — feature-complete.** On-demand round-trip / one-way search,
> background **watches** with **push alerts** (Telegram/Discord/ntfy via Apprise), an **Explore**
> page that ranks every destination by your interest × award availability, a **points-value (cpp)**
> column everywhere fed by manual cash quotes + zone estimates, a **seats.aero fallback provider**
> behind a config switch, and daily **snapshot pruning**.

## How it fetches (important)

SAS's site sits behind Cloudflare. The Phase 1 spike (see [docs/api-notes.md](docs/api-notes.md))
established the only working path:

- **Real Google Chrome** driven by Playwright (`channel="chrome"`), doing an **in-page `fetch()`**.
  Bundled Chromium / headless-shell and plain `httpx` are hard-403'd, and the `cf_clearance` cookie
  is TLS-fingerprint-bound so it can't be replayed outside the browser. Headless real Chrome works.
- Two feed shapes:
  - **Network feed** (`destinations=`): one request per origin → **outbound-only**, all ~139
    destinations. Refreshed on a schedule; powers the destination catalog and the Explore ranking.
  - **Route feed** (`destinations=XXX`): one request per route → **both** legs. Fetched on demand by
    Search and cached (default 15 min). This is what round-trip pairing needs.
- Requests are single-flight, jittered (default 4–8 s apart), and capped by a hard **daily budget**
  (default 500), all from your home residential IP.

## Watches & alerts (Phase 2)

Create a watch on **Watches** (or from a search result via *🔔 Watch this route*): route, cabin,
outbound + return date windows, stay-length bounds, minimum seats, and optionally **🎟️ voucher-hunt
mode** (only counts trips with ≥2 seats on both SAS-operated legs — what the Amex 2-for-1 needs).

A background sweep re-checks every enabled watch (default hourly, jittered; one request per watched
route regardless of how many watches share it). Alert types:

- **opened** — the watch went from no bookable trip to at least one. Round-trip alerts name **both
  dates** and only fire when *both* legs qualify; an outbound with no return inside the window stays
  silent.
- **price_drop** — the best bookable trip got cheaper (e.g. a cheaper cabin opened).
- **voucher_pair** — a bookable trip turned 2-for-1 eligible (≥2 seats both legs).
- **closed** — the last bookable trip disappeared. Negative alerts only come from fully-ok sweeps —
  a failed fetch never reads as "award gone".
- **ops** — the app needs attention (repeated fetch failures, feed format change, budget exhausted).

Every alert carries a **flysas.com pay-with-points deep link** with the dates pre-filled, lands in
the **Alerts** log, and is pushed via every `AF_NOTIFY_URLS` target — undelivered alerts retry every
5 minutes until they send. With no notify URLs configured, alerts are log-only.

## Explore (Phase 3)

**Explore** answers "where *could* I go?" without burning requests. It has two layers:

- **Destination ranking — free.** Aggregated from the latest network snapshot (already fetched on a
  schedule): per destination and cabin, how many dates have award seats and how many have **≥2 seats**
  (2-for-1 voucher potential). Ranked by `interest × availability`, where premium-cabin days weigh
  more (Business ×4, Premium ×2, day counts capped at 90 so a year of economy can't drown a month of
  business). Set a per-destination **interest weight** (0 = skip, ★ normal, ★★ high, ★★★ must-track)
  right on the page.
- **Round-trip leads — budgeted.** Confirming a return needs one route fetch per destination, so a
  **nightly sweep** (02:00–06:00 UTC, hard-capped at `AF_EXPLORE_SWEEP_BUDGET` route fetches per
  origin, default 25) walks the ranking — never-checked destinations first, then stalest — and stores
  the **best bookable round-trip per month & cabin** (default 3–14 day stays): dates, seats, points,
  taxes, voucher badge. Interest 0 removes a destination from the sweep entirely.

Each lead links straight to the **flysas.com booking flow** and to a **pre-filled watch** (whole-month
windows) so one click starts tracking the trip. *Refresh leads* on any destination re-checks it
immediately (1 fetch); *Refresh top leads now* runs the budgeted sweep on demand. A recent route
snapshot (e.g. you just searched the route) is reused instead of re-fetching — cached refreshes don't
spend budget.

## Points & taxes

The feed has seat availability but **no prices** (SAS award flights are fixed-price). Points and taxes
come from [`config/points_table.yaml`](config/points_table.yaml), a **seed zone table** — tune the
numbers against SAS's published award chart. They only affect the displayed points/cpp, never
availability. Verify the exact figures at booking.

## Points value (cpp) & cash fares (Phase 4)

Search results, Explore leads, and watch alerts show a **Value** figure: cents of cash value per
point, `cpp = (cash − taxes) × 100 / points`, plus the voucher-adjusted `cpp` when a trip is 2-for-1
eligible (one award's points buy the trip for two, so the value roughly doubles while both pax still
pay taxes).

The cash side can't be scraped — SAS's booking flow (which has cash prices) sits behind a hard
Cloudflare challenge — so cash comes from two tiers:

1. **Manual quotes** (authoritative): check the real fare on flysas.com / Google Flights and save it
   via the *"Real cash price"* form under any search result. Latest quote per route+cabin wins;
   saving an empty price clears back to the estimate.
2. **Zone estimates** (rough, marked *est.*): `cash_estimates` in `points_table.yaml`, round-trip per
   cabin by zone pair. One-way trips use half the round-trip figure.

## Fallback provider: seats.aero (Phase 4)

If direct SAS scraping breaks (or you want SkyTeam partner coverage), switch to the
[seats.aero Partner API](https://developers.seats.aero) (Pro subscription, ~$10/mo, 1,000 calls/day):

```
AF_PROVIDER=seats_aero
AF_SEATS_AERO_API_KEY=sk-your-key
```

Search, watches, Explore, and alerts all keep working unchanged — the provider implements the same
two feed scopes (route = both directions in one call via comma lists; network = outbound sweep), the
same rate-limit/budget layer, and maps seats.aero cabins (Y/W/J) onto AG/AP/AB. `is_sas_operated`
is only set when a cabin's airline list is exactly SK, so voucher logic never fires on partner metal.
**Note:** written against the published API docs but not yet exercised against a live key — expect to
touch only `app/providers/seats_aero/{endpoints,parser}.py` if a field is off.

## Housekeeping

A daily prune job (default retention **30 days**, `AF_SNAPSHOT_RETENTION_DAYS`) deletes aged-out raw
snapshot files + observation history — always keeping each route's newest snapshot — and drops
already-departed dates from the current-state and Explore-lead tables.

## Run locally (dev)

```bash
python -m venv .venv && . .venv/Scripts/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m playwright install chrome                  # real Google Chrome
cp .env.example .env                                 # edit AF_HOME_AIRPORTS, notify URLs
uvicorn app.main:app --reload --port 8617
```

Open http://localhost:8617/search. First run: click **Status → Refresh network catalog now** to
populate the destination picker (or just type an IATA code and search — a route search fetches on
demand).

Run the tests: `pytest -q`.

## Deploy on Dockge / TrueNAS SCALE

The container needs **real Google Chrome**, which the Dockerfile installs on top of Playwright's base
image. A GitHub Actions workflow ([.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml))
builds that image on every push to `main` and publishes it as
**`ghcr.io/kcb064/sas-award-finder:latest`** (amd64), so the stack just pulls — no build on the NAS.

1. Copy `docker-compose.yml` and `.env.example` into a Dockge stack directory (or clone the repo).
2. Create `.env` from `.env.example` (set `AF_HOME_AIRPORTS`, `AF_NOTIFY_URLS`, and optionally
   `AF_BASIC_AUTH_USER`/`AF_BASIC_AUTH_PASS`).
3. If the GHCR package is private, log the Docker host in first:
   `docker login ghcr.io -u kcb064` with a PAT that has `read:packages`.
4. In Dockge, deploy the stack. It pulls the image and starts on port **8617**.

```yaml
# docker-compose.yml (included)
services:
  award-finder:
    image: ghcr.io/kcb064/sas-award-finder:latest
    restart: unless-stopped
    env_file: [.env]
    volumes:
      - ./data:/data        # SQLite (WAL), browser profile, raw snapshots
      - ./config:/config    # points_table.yaml (seeded from image defaults on first run)
    ports:
      - "8617:8617"
    shm_size: "1gb"
```

To build locally instead (e.g. while iterating on the Dockerfile), swap `image:` for `build: .`.

### Remote access + auth

The app has **no built-in login**. Two options:

- **Cloudflare tunnel + Cloudflare Access** (recommended): don't publish the port; add
  `award-finder:8617` as a hostname in your existing cloudflared config and gate it with Access.
- **Basic auth**: set `AF_BASIC_AUTH_USER` / `AF_BASIC_AUTH_PASS` in `.env` to require credentials on
  every page (except `/health`).

### Notifications

`AF_NOTIFY_URLS` is a comma-separated list of [Apprise](https://github.com/caronc/apprise) URLs, e.g.
`tgram://BOT_TOKEN/CHAT_ID`, `discord://WEBHOOK_ID/WEBHOOK_TOKEN`, `ntfy://TOPIC`. Watch alerts and
ops alerts go to every listed target.

## Layout

```
app/
├── main.py, config.py, db.py, models.py, scheduler.py
├── migrations/00{1,2,3}_*.sql
├── fetch/{engine,ratelimit,budget}.py      # browser-context fetcher, pacing, daily budget
├── providers/base.py, registry.py
├── providers/sas_direct/{provider,endpoints,parser}.py   # the only SAS-specific code
├── providers/seats_aero/{provider,endpoints,parser}.py   # Phase 4: the only seats.aero code
├── services/{search,trips,value,snapshots}.py            # pairing, pricing/voucher/cpp, persistence
├── services/{watches,diffing,notify}.py                  # Phase 2: sweeps, diff, Apprise alerts
├── services/explore.py                                   # Phase 3: ranking, leads, budgeted sweep
└── web/{routes,templates,static}
tests/               # parser, trips, value/cpp, store, diffing, watches, notify, explore,
                     # seats_aero, pruning
docs/api-notes.md    # Phase 0/1 endpoint findings
```
