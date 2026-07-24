# SAS award-search API notes — Phase 0 findings

Captured 2026-07-13 via anonymous browser network inspection of flysas.com (no login). All
findings from the public site; no credentials used. This is the deliverable that gates Phase 1.

> **Phase 1 spike update (2026-07-13)** — two Phase-0 assumptions were corrected. See
> [§Phase 1 spike results](#phase-1-spike-results-2026-07-13) at the bottom. TL;DR:
> **(1)** plain `httpx` and bundled/headless-shell Chromium are hard-403'd; fetching requires
> **real Google Chrome (`channel="chrome"`) via Playwright**, and an **in-page `fetch()`** — the
> `cf_clearance` cookie is bound to the browser's TLS fingerprint, so httpx cookie-replay does
> **not** work. **(2)** The all-destinations feed is **outbound-only**; the **inbound** (return)
> array is unlocked only by **scoping to a single destination** (`destinations=XXX`), not by month.
> Round-trips therefore need one scoped request per route.

## Headline result

**One anonymous HTTP GET returns the entire SAS award network from an origin — every
destination, ~a full year of dates, per-cabin seat counts.** This collapses the rate-limit /
nightly-budget problem the plan worried about. For SAS-operated award seats, a single request
per home airport snapshots everything.

## The workhorse endpoint

```
GET https://www.flysas.com/bff/award-finder/destinations/v1
    ?market=en
    &origin=CPH                 # IATA origin
    &destinations=              # empty = ALL destinations; or CSV e.g. "BOS" to scope
    &selectedMonth=             # empty = ALL months (~1 year); or "YYYYMM" e.g. 202612
    &passengers=2               # affects which dates qualify (need N seats)
    &direct=false
    &availability=true          # false = catalog only (empty availability arrays)
    &selectedFlightClass=       # empty = all cabins; or AG/AP/AB to filter
```

- **Auth**: none. Works logged out.
- **Cache**: `Cache-Control: public, max-age=600` (Cloudflare-fronted; responses cacheable 10 min).
- **Size**: `origin=CPH, all dests, all months` ≈ **1.48 MB**, 139 destinations, dates
  2026-06-24 → 2027-05-18. `all dests, one month` ≈ smaller; `one dest, all months` ≈ 5–7 KB.
- **Cabin codes**: `AG` = Economy, `AP` = Premium, `AB` = Business.

### Response shape
Array of destination objects:
```jsonc
[{
  "airportCode": "BOS", "cityName": "Boston", "countryName": "United States of America",
  "cityCode": "BOS", "long": -71.0079, "lat": 42.36197,
  "flightClasses": ["AG","AP","AB"],          // cabins this route is ever sold in
  "distanceToCity": 5.6,
  "image": "https://components.flysas.com/content/assets/images/destination/bos.jpg?w=300",
  "availability": {
    "outbound": [
      { "key": 261102, "date": "2026-11-02", "availableSeatsTotal": 20, "AG": 10, "AP": 8, "AB": 2 },
      // ...one entry per bookable date; cabin keys present only when seats > 0 in that cabin
    ],
    "inbound": [ /* same shape */ ]
  }
}]
```

### What it gives us (directly usable)
- **Per-date, per-cabin seat counts** for every SAS-operated award route from an origin.
- Exactly what **watches** need (diff seat counts over time) and what **voucher logic** needs
  (`AB >= 2` on a SAS-operated flight ⇒ 2-for-1 eligible).
- The full **destination catalog** (name, country, coords, image, cabins) for the Explore UI —
  no need to hand-maintain `destinations.yaml`; seed it from `availability=false`.

### What it does NOT give (must come from elsewhere)
- **No points price, no taxes/fees.** SAS's own copy: award flights have a *fixed point price*.
  → Model points cost as a **maintained regional/zone table** keyed by (origin zone, dest zone,
  cabin), not scraped per search. Taxes/fees are small and fixed-ish per route; can be a table
  or verified at booking.
- **No flight numbers, times, or connection detail.** Each entry is "on this date, N award
  seats exist in cabin X to this city" (direct or via a SAS hub). Fine for discovery + alerting;
  the actual flight/segment selection happens in the booking flow.
- **SAS-operated metal only.** SkyTeam partner award space (Air France/KLM/Delta/etc.) is NOT
  in this feed — it lives in the booking flow (see below).

## Supporting endpoints (anonymous, same origin)
- `GET /bff/location-picker/locations/v2?market=en` — full airport/location list (autocomplete).
- `GET /bff/location-picker/suggestions/shared/v1?market=en&origin=CPH` — suggested destinations.
- `GET /bff/datepicker/flights/direct/v2?origin=CPH&destination=BOS&tripType=OW` — map of
  `date → 1` for dates with **direct** flights (existence flag only, no price/seats).
- `POST /api/session/validate` — fired on load; part of session bootstrap.

## The hard tier: booking-flow search (dynamic price, taxes, partners)
Submitting the real "Pay with points" search (`/en?payWithPoints=&tripType=OW&origin=CPH&
destination=BOS&outboundDate=YYYY-MM-DD`) triggered a **Cloudflare "Just a moment…" managed
challenge that did NOT auto-clear in the automated browser within ~60s.** The award-finder BFF
pages loaded fine before and after — the hard challenge is specific to the booking results flow.

Implications:
- Actual **points prices, taxes, and SkyTeam partner award** results live behind this harder
  challenge. Getting them programmatically needs a persistent, well-warmed real-Chromium session
  (Playwright, `user_data_dir`), and even then it's the fragile part.
- **This is exactly where the seats.aero fallback provider earns its keep** — it already
  aggregates partner award space and pricing.

## Architecture consequences (feeds the plan revision)
1. **Primary provider = the award-finder BFF.** Poll 1 request per home airport (all dests, all
   months). Cheap enough to refresh several times a day. No per-route sweeping, minimal
   rate-limit risk. Likely replayable with plain `httpx` once a Cloudflare clearance cookie is
   obtained from a warmed browser session — **verify in Phase 1** (test httpx + cf_clearance vs.
   mandatory browser-context fetch).
2. **Explore, Search, and Watches all read from the same snapshot.** Explore = rank the snapshot;
   Search = filter it; Watches = diff successive snapshots. The 1.5 MB blob is the whole dataset.
3. **Points/taxes = maintained table**, applied at display time to compute cpp and voucher value.
4. **Booking-flow scrape + seats.aero = the secondary tier** for dynamic pricing and partner
   awards, behind the provider abstraction, added later.

## Fixtures captured
- `tests/fixtures/award_finder_availability_cph_bos.json` — single dest (CPH→BOS), all months, full.
- `tests/fixtures/award_finder_all_dests_cph_202612_trimmed.json` — all-dests shape, trimmed to 3.

## Open questions for Phase 1
- ~~httpx + `cf_clearance` cookie replay vs. mandatory browser-context fetch?~~ **Answered below:
  browser-context (real Chrome, in-page fetch) is mandatory; httpx replay fails.**
- Does `passengers=N` change *which dates appear*, or just filter client-side? (Appears to gate
  dates to those with ≥N seats — confirm.)
- Confirm the fixed points-price zone table values (build from SAS's published award chart).
- Booking-flow: is the managed challenge passable with a persistent warmed session at low volume?

---

## Phase 1 spike results (2026-07-13)

Ran a fetch-strategy spike from Kevin's Windows dev machine (residential IP). Two findings
reshape the fetcher and the round-trip data flow.

### Finding 1 — the fetch path: real Chrome + in-page fetch is mandatory
Tested, in order, against `GET /bff/award-finder/destinations/v1`:

| Approach | Result |
|---|---|
| Plain `httpx` (HTTP/2, full browsery headers) | **403** — Cloudflare "Denied boarding" HTML |
| Playwright **bundled Chromium**, headless-shell | **403** (UA literally says `HeadlessChrome`) |
| Playwright **real Google Chrome** (`channel="chrome"`), **headless**, in-page `fetch()` | **200 JSON** ✅ (cold *and* warm profile) |
| httpx replay using the browser's exported `cf_clearance` cookie + exact UA | **403** |

Conclusions:
- **Use real Google Chrome via `channel="chrome"`, not bundled Chromium.** The bundled build's
  fingerprint is blocked. Real Chrome **headless** works — no xvfb / headed display needed.
- **You MUST strip the `Headless` token from the User-Agent.** Even *real* Google Chrome, run
  headless, reports `HeadlessChrome/<ver>` in `navigator.userAgent`, and Cloudflare hard-blocks
  that token (403 "Denied boarding"). Setting a `user_agent` that replaces `HeadlessChrome` with
  `Chrome` clears it — cold and warm. This was the difference between a working spike and a 403 in
  the integrated engine. `app/fetch/engine.py` derives this automatically: it reads the native UA
  from a throwaway launch and strips `Headless`, keeping the real installed version (so the UA stays
  consistent with client hints across Chrome updates — don't hardcode a version).
- **Fetch through an in-page `fetch()`** inside the warmed page (carries the browser's TLS/JA3 +
  cookies + client hints). A persistent `user_data_dir` profile keeps the session across restarts.
- **httpx cookie-replay is dead.** `cf_clearance` is pinned to the browser's TLS fingerprint, so
  replaying it from httpx still 403s. There is no cheap non-browser steady-state path; every fetch
  goes through Chrome. (Mitigation for load: the endpoint is `Cache-Control: max-age=600`, so
  Chrome/CDN cache absorbs repeats; and the payloads are small.)
- Launch flags that mattered: `--disable-blink-features=AutomationControlled`, plus a small init
  script nulling `navigator.webdriver`. `--no-sandbox`/`--disable-dev-shm-usage` for container use.

**Docker consequence:** the base image must contain **real Google Chrome stable**. Use the
Playwright Python base image and `playwright install --with-deps chrome` (installs Google Chrome,
not just Chromium). Launch with `channel="chrome"`, `headless=True`, persistent `user_data_dir` on
the `/data` volume.

### Finding 2 — inbound (return) availability needs a per-destination scoped request
The Phase-0 note claimed "both directions come back in one response." That is **only true for a
destination-scoped request.** Measured, same warmed session:

| Request | dests | outbound entries | inbound entries |
|---|---:|---:|---:|
| `destinations=BOS&selectedMonth=202612` | 1 | 50 | **44** |
| `destinations=BOS&selectedMonth=` (all months) | 1 | 173 | **131** |
| `destinations=&selectedMonth=202612` (all dests) | 95 | 5015 | **0** |
| `destinations=&selectedMonth=` (all dests, all months) | 139 | 19431 | **0** |

So **destination-scoping** unlocks `inbound`, *not* the month filter. The all-destinations network
feed is **outbound-only**.

**Fetch model (revised):**
1. **Network feed** — `destinations=` (empty) → 1 request per origin → **outbound-only**, whole
   network (~139 dests, ~1.48 MB, ~290 ms). Powers Explore ranking and Search's destination list.
2. **Route feed** — `destinations=XXX` → 1 request per (origin, dest) → **both outbound + inbound**,
   all months, ~5–7 KB. Required for round-trip pairing (`services/trips.py`). Powers a specific
   Search and each Watch (one small scoped request per refresh).

This keeps the rate-limit / budget layer meaningful: a round-trip Search or Watch refresh = 1
scoped request; Explore's "where can I go" list = the single network feed; enriching an Explore
lead into a round-trip = 1 scoped request per candidate destination (nightly-budgeted).

### Fixtures captured (Phase 1)
- `tests/fixtures/network_cph_outbound.json` — real network feed trimmed to 5 dests (BOS/EWR/LHR/
  CDG/OSL), outbound-only (inbound arrays empty) — the all-dests shape.
- `tests/fixtures/award_finder_availability_cph_bos.json` — scoped CPH→BOS, **both directions**
  populated — the round-trip-pairing shape (from Phase 0, still valid).

---

## Phase 4 notes (2026-07-14): seats.aero fallback — assumptions to verify at subscription

The `seats_aero` provider (`AF_PROVIDER=seats_aero`) was written against the **published** Partner
API docs (https://developers.seats.aero) but has NOT been exercised against a live key — Kevin
hasn't subscribed yet. It's fixture-tested (`tests/fixtures/seats_aero_search_cph_bos.json` is
authored, not recorded). When a key arrives, verify these assumptions; any mismatch is confined to
`app/providers/seats_aero/{endpoints,parser}.py`:

1. `GET /partnerapi/search` accepts `origin_airport` WITHOUT `destination_airport` (used for the
   network scope). If it 400s, switch the network scope to `GET /partnerapi/availability` +
   client-side origin filter.
2. Comma lists (`origin_airport=CPH,BOS&destination_airport=CPH,BOS`) return both directions in one
   call (used for the route scope).
3. Pagination is `hasMore` + `cursor` echoed back as a query param; page cap 10 × take 1000.
4. Per-cabin fields are `{Y,W,J,F}Available` / `...RemainingSeats` / `...Airlines` and route
   metadata is `Route.{OriginAirport,DestinationAirport,Source}` with `Source == "eurobonus"`.
5. Auth is the `Partner-Authorization` header (sent on every request).

Mapping decisions (ours, not the API's): Y→AG, W→AP, J→AB, F skipped; `RemainingSeats == 0` while
available ⇒ recorded as 1 seat ("at least one"); `is_sas_operated` only when the airline list is
exactly `SK` — so 2-for-1 voucher logic can never fire on partner metal or unknown carriers.

---

## Phase 5 notes (2026-07-24): SkyTeam tab (LIVE-VERIFIED seats.aero) + NL search

The SkyTeam tab (`/skyteam`) runs a SECOND seats.aero provider instance ALONGSIDE sas_direct
(needs only `AF_SEATS_AERO_API_KEY`, not `AF_PROVIDER`), with its own provider-scoped daily
budget (`AF_SEATS_AERO_DAILY_BUDGET`). Results are **live-only**: fetched via
`SeatsAeroProvider.search_entries()` → `parse_partner_rows()` and rendered, never persisted —
seats.aero itself is the cache.

### Live verification results (first real key, 2026-07-24)

The Phase 4 assumptions were checked against the real API. Outcomes:

1. **WRONG — origin-only search returns nothing.** `origin_airport` without
   `destination_airport` → HTTP 200 with an empty `data` array. The tab therefore REQUIRES
   destinations (a region expands to a ≤30-code list; `SkyTeamService` rejects empty
   destination sets before spending budget). This also means the legacy fallback provider's
   NETWORK scope (`AF_PROVIDER=seats_aero`) silently returns empty catalogs.
2. **OK** — comma lists work on both sides (`origin_airport=CPH,OSL&destination_airport=BKK,NRT`
   returned all combinations).
3. Pagination shape (`hasMore`/`cursor`) present as expected (large responses not yet observed
   paginating; `hasMore` was false up to 280 entries).
4. **Field names OK, but no `eurobonus` source exists.** seats.aero does NOT index SAS
   EuroBonus at all. CPH–BKK returned sources: flyingblue, delta, virginatlantic, united,
   aeroplan, etihad. **Consequence: the `AF_PROVIDER=seats_aero` fallback can never return
   EuroBonus-priced data** — its parse path filters `Source == "eurobonus"` and will always be
   empty. The SkyTeam tab instead reads SkyTeam programs (`AF_SKYTEAM_SOURCES`, default
   `flyingblue`) — their availability is the shared SkyTeam partner space EuroBonus can also
   book, but the **mileage prices are that program's, not EuroBonus's** (the UI says so).
5. **OK** — `Partner-Authorization` header authenticates.
6. Per-cabin fields (recorded in `tests/fixtures/seats_aero_search_live_cph_bkk.json`, a real
   trimmed response): `{L}MileageCost` is a **string**, `"0"` means "no figure" (parsed to
   None); `{L}TotalTaxes` is an int in **minor currency units** of `TaxesCurrency` (48100 USD
   = $481.00, 394500 DKK = 3 945 kr — parser divides by 100); `{L}Direct` is a bool. Every
   field also has a `...Raw` twin — the plain fields are seats.aero's "reasonably priced"
   filtered view and are the ones we read.

NL search (`app/services/nl_search.py`): one Claude Haiku forced-tool call parses the query into
structured params; region expansion and filtering stay deterministic in `SkyTeamService`.
Anthropic calls are not budget-tracked in `provider_calls`.

### `/routes` — the SkyTeam region-expansion catalog (live-verified 2026-07-24)

`GET /partnerapi/routes?source=flyingblue` returns a flat JSON array (no pagination) of
`{ID, OriginAirport, OriginRegion, DestinationAirport, DestinationRegion, Distance, Source}` —
4,212 routes / ~760 KB for flyingblue, 22 of them from CPH. Two properties make it the right
catalog for region searches:

1. The pairs are **exactly the markets `/search` can answer** for that source — expanding a
   region into anything else burns budget on guaranteed-empty queries.
2. It covers the partner network SAS never flies, which the SAS catalog can't provide.

Caveat: `*Region` values are continents only (North America, South America, Europe, Asia,
Africa, Oceania). Scandinavia hides inside "Europe" and the Middle East inside "Asia", so
`SkyTeamService.expand_region` refines with `airport_zones` overrides and SAS-catalog country
names where available, and falls back to the continent for airports nobody knows. The map is
cached in memory per source for 24h (`ROUTES_TTL_S`); each fetch is one budgeted call recorded
as scope `routes` with the source in the origin column.
