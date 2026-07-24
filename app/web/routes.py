"""HTTP routes + Jinja setup. Server-rendered pages (no SPA/build step).

Search is a plain GET form that re-renders with results, so the whole UI works without client JS.
Watch mutations are plain POST forms with a redirect back (flash via query param).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.fetch.budget import Budget, BudgetExceeded
from app.fetch.engine import FetchError
from app.models import CABIN_NAMES
from app.providers.base import SCOPE_NETWORK
from app.providers.sas_direct.endpoints import booking_url
from app.services.search import SearchService

log = logging.getLogger("award_finder.web")

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_int(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(value: object) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _ago(iso: object) -> str:
    """Coarse 'how stale is this' label for timestamps ('12m ago', '3h ago', '2d ago')."""
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 90:
        return "just now"
    if s < 90 * 60:
        return f"{s / 60:.0f}m ago"
    if s < 36 * 3600:
        return f"{s / 3600:.0f}h ago"
    return f"{s / 86400:.0f}d ago"


def _month_label(month: str) -> str:
    """'2026-11' -> 'Nov 2026'."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return month


templates.env.filters["int"] = _fmt_int
templates.env.filters["money"] = _fmt_money
templates.env.filters["ago"] = _ago
templates.env.filters["month_label"] = _month_label
templates.env.filters["region_label"] = lambda r: (r or "").replace("_", " ").title()
templates.env.globals["cabin_name"] = lambda c: CABIN_NAMES.get(c, c)


def _services(request: Request):
    return request.app.state.services


@router.get("/")
async def index(request: Request):
    return RedirectResponse(url="/search")


@router.get("/health")
async def health(request: Request):
    svc = _services(request)
    budget = Budget(svc.settings.db_path, svc.settings.daily_request_budget)
    return JSONResponse(
        {
            "status": "ok",
            "homes": svc.settings.home_airports,
            "budget_used": budget.used(),
            "budget_remaining": budget.remaining(),
            "fetcher_started": svc.fetcher.started,
        }
    )


def _watch_prefill_qs(form: dict, result) -> str:
    """Query string for the results page's 'Watch this route' link.

    Date windows the user left blank (e.g. arriving from Explore's 'Search this route') are
    filled from the dates actually found, so the watch form lands ready to submit instead of
    with empty required fields.
    """
    outs = [pt.trip.outbound_date for pt in result.trips]
    rets = [pt.trip.inbound_date for pt in result.trips if pt.trip.inbound_date]
    return urlencode({
        "origin": form["origin"],
        "destination": form["destination"],
        "trip_type": form["trip_type"],
        "cabin": form["cabin"],
        "out_from": form["out_from"] or (min(outs) if outs else ""),
        "out_to": form["out_to"] or (max(outs) if outs else ""),
        "ret_from": form["ret_from"] or (min(rets) if rets else ""),
        "ret_to": form["ret_to"] or (max(rets) if rets else ""),
        "min_stay_days": form["min_stay_days"],
        "max_stay_days": form["max_stay_days"],
        "min_seats": form["min_seats"],
    })


@router.get("/search")
async def search(
    request: Request,
    origin: str | None = Query(default=None),
    destination: str | None = Query(default=None),
    trip_type: str = Query(default="RT"),
    cabin: str = Query(default=""),
    out_from: str = Query(default=""),
    out_to: str = Query(default=""),
    ret_from: str = Query(default=""),
    ret_to: str = Query(default=""),
    min_stay_days: int = Query(default=3),
    max_stay_days: int = Query(default=14),
    min_seats: int = Query(default=1),
    collapse: int = Query(default=1),
):
    svc = _services(request)
    settings = svc.settings
    search_svc: SearchService = svc.search
    origin = (origin or settings.default_home).upper()

    form = {
        "origin": origin,
        "destination": (destination or "").upper(),
        "trip_type": trip_type,
        "cabin": cabin,
        "out_from": out_from,
        "out_to": out_to,
        "ret_from": ret_from,
        "ret_to": ret_to,
        "min_stay_days": min_stay_days,
        "max_stay_days": max_stay_days,
        "min_seats": min_seats,
        "collapse": collapse,
    }

    context = {
        "request": request,
        "form": form,
        "homes": settings.home_airports,
        "destinations": svc.store.list_destinations(),
        "cabins": [("", "Any cabin"), ("AG", "Economy"), ("AP", "Premium Economy"), ("AB", "Business")],
        "result": None,
        "error": None,
        "voucher_count": settings.voucher_count,
        "today": date.today().isoformat(),
    }

    if destination:
        try:
            result = await search_svc.search(
                origin,
                destination,
                trip_type=trip_type,
                cabin=cabin or None,
                out_from=out_from or None,
                out_to=out_to or None,
                ret_from=ret_from or None,
                ret_to=ret_to or None,
                min_stay_days=min_stay_days,
                max_stay_days=max_stay_days,
                min_seats=min_seats,
                collapse=bool(collapse),
            )
            context["result"] = result
            context["watch_qs"] = _watch_prefill_qs(form, result)
        except BudgetExceeded as exc:
            context["error"] = f"Daily SAS request budget reached — {exc}. Try again tomorrow or raise AF_DAILY_REQUEST_BUDGET."
        except FetchError as exc:
            context["error"] = f"SAS blocked the fetch (Cloudflare). The browser session may need re-warming. Details: {exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("search failed")
            context["error"] = f"Search failed: {exc}"

    return templates.TemplateResponse("index.html", context)


# ---- SkyTeam discovery (Phase 5) -----------------------------------------------------


@router.get("/skyteam")
async def skyteam_page(
    request: Request,
    q: str = Query(default=""),
    origin: str = Query(default=""),
    destination: str = Query(default=""),
    region: str = Query(default=""),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    cabin: str = Query(default=""),
    min_seats: int = Query(default=1),
    voucher: int = Query(default=0),
    direct: int = Query(default=0),
    submitted: int = Query(default=0),
):
    svc = _services(request)
    settings = svc.settings

    form = {
        "q": q.strip(),
        "origin": origin.strip().upper(),
        "destination": destination.strip().upper(),
        "region": region.strip().upper(),
        "date_from": date_from,
        "date_to": date_to,
        "cabin": cabin,
        "min_seats": min_seats,
        "voucher": voucher,
        "direct": direct,
    }
    context = {
        "request": request,
        "form": form,
        "homes": settings.home_airports,
        "destinations": svc.store.list_destinations(),
        "cabins": CABIN_CHOICES,
        "regions": svc.skyteam.region_names() if svc.skyteam else [],
        "nl_enabled": svc.nl is not None,
        "setup_notice": svc.skyteam is None,
        "result": None,
        "error": None,
        "interpreted": None,
        "voucher_note": False,
        "today": date.today().isoformat(),
    }
    if svc.skyteam is None:
        return templates.TemplateResponse("skyteam.html", context)

    run_search = bool(submitted or form["destination"] or form["region"])

    if form["q"]:
        if svc.nl is None:
            context["error"] = (
                "Natural-language search needs AF_ANTHROPIC_API_KEY — use the form instead."
            )
        else:
            from app.services.nl_search import NLParseError

            try:
                params = await svc.nl.parse(form["q"])
            except NLParseError as exc:
                context["error"] = str(exc)
            else:
                form.update({
                    "origin": ",".join(params.origins),
                    "destination": ",".join(params.destinations),
                    "region": params.region or "",
                    "date_from": params.date_from or "",
                    "date_to": params.date_to or "",
                    "cabin": params.cabin or "",
                    "min_seats": params.min_seats or 1,
                    "voucher": 1 if params.voucher_intent else 0,
                })
                context["interpreted"] = params.summary
                context["voucher_note"] = params.voucher_intent
                run_search = True

    if run_search and not context["error"]:
        origins = [o for o in form["origin"].split(",") if o.strip()] or settings.home_airports
        dests = [d.strip() for d in form["destination"].split(",") if d.strip()] or None
        min_s = int(form["min_seats"] or 1)
        if form["voucher"]:
            # Voucher hunting: only SAS-operated legs with 2+ CONFIRMED seats can carry a 2-for-1.
            min_s = max(2, min_s)
        try:
            context["result"] = await svc.skyteam.search(
                origins=origins,
                destinations=dests,
                region=form["region"] or None,
                date_from=form["date_from"] or None,
                date_to=form["date_to"] or None,
                cabin=form["cabin"] or None,
                min_seats=min_s,
                sas_only=bool(form["voucher"]),
                direct_only=bool(form["direct"]),
            )
        except BudgetExceeded as exc:
            context["error"] = (
                f"Daily seats.aero budget reached — {exc}. Raise AF_SEATS_AERO_DAILY_BUDGET "
                "or try again tomorrow."
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                context["error"] = (
                    "seats.aero rejected the API key — check AF_SEATS_AERO_API_KEY and that the "
                    "Pro subscription is active."
                )
            elif code == 429:
                context["error"] = "seats.aero rate limit hit — wait a minute and retry."
            else:
                context["error"] = f"seats.aero returned HTTP {code}."
        except ValueError as exc:
            context["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("skyteam search failed")
            context["error"] = f"Search failed: {exc}"

    return templates.TemplateResponse("skyteam.html", context)


# ---- watches (Phase 2) ---------------------------------------------------------------

CABIN_CHOICES = [("", "Any cabin"), ("AG", "Economy"), ("AP", "Premium Economy"), ("AB", "Business")]


@router.get("/watches")
async def watches_page(request: Request):
    svc = _services(request)
    q = request.query_params
    # Arriving via a 'Watch this route' / '🔔 Watch' link: the form below is prefilled but
    # nothing is saved yet — the template shows a banner so that's obvious.
    prefilled = bool(q.get("destination"))
    prefill = {
        "label": q.get("label", ""),
        "origin": (q.get("origin") or svc.settings.default_home).upper(),
        "destination": (q.get("destination") or "").upper(),
        "trip_type": q.get("trip_type", "RT"),
        "cabin": q.get("cabin", ""),
        "date_from": q.get("out_from", ""),
        "date_to": q.get("out_to", ""),
        "return_from": q.get("ret_from", ""),
        "return_to": q.get("ret_to", ""),
        "min_stay_days": q.get("min_stay_days", "3"),
        "max_stay_days": q.get("max_stay_days", "14"),
        "min_seats": q.get("min_seats", "1"),
    }
    return templates.TemplateResponse(
        "watches.html",
        {
            "request": request,
            "watches": svc.watches.list_all(),
            "prefill": prefill,
            "prefilled": prefilled,
            "homes": svc.settings.home_airports,
            "destinations": svc.store.list_destinations(),
            "cabins": CABIN_CHOICES,
            "flash_ok": q.get("ok"),
            "flash_err": q.get("err"),
            "notify_enabled": svc.notifier.enabled,
            "today": date.today().isoformat(),
        },
    )


@router.post("/watches")
async def create_watch(
    request: Request,
    origin: str = Form(...),
    destination: str = Form(...),
    trip_type: str = Form(default="RT"),
    label: str = Form(default=""),
    cabin: str = Form(default=""),
    date_from: str = Form(...),
    date_to: str = Form(...),
    return_from: str = Form(default=""),
    return_to: str = Form(default=""),
    min_stay_days: int = Form(default=3),
    max_stay_days: int = Form(default=14),
    min_seats: int = Form(default=1),
    sas_only: int = Form(default=0),
    voucher_mode: int = Form(default=0),
):
    svc = _services(request)
    try:
        watch_id = svc.watches.create(
            origin=origin.strip(), destination=destination.strip(), trip_type=trip_type,
            label=label.strip() or None, cabin=cabin or None,
            date_from=date_from, date_to=date_to,
            return_from=return_from or None, return_to=return_to or None,
            min_stay_days=min_stay_days, max_stay_days=max_stay_days, min_seats=min_seats,
            sas_only=bool(sas_only), voucher_mode=bool(voucher_mode),
        )
    except ValueError as exc:
        return RedirectResponse(url=f"/watches?err={quote(str(exc))}", status_code=303)
    msg = f"Watch #{watch_id} created — first check runs on the next sweep, or hit Check now."
    return RedirectResponse(url=f"/watches?ok={quote(msg)}", status_code=303)


@router.post("/watches/{watch_id}/toggle")
async def toggle_watch(request: Request, watch_id: int):
    svc = _services(request)
    watch = svc.watches.get(watch_id)
    if watch is None:
        return RedirectResponse(url="/watches?err=Watch not found", status_code=303)
    svc.watches.set_enabled(watch_id, not watch.enabled)
    state = "paused" if watch.enabled else "resumed"
    return RedirectResponse(url=f"/watches?ok=Watch {state}", status_code=303)


@router.post("/watches/{watch_id}/delete")
async def delete_watch(request: Request, watch_id: int):
    svc = _services(request)
    svc.watches.delete(watch_id)
    return RedirectResponse(url="/watches?ok=Watch deleted", status_code=303)


@router.post("/watches/{watch_id}/run")
async def run_watch_now(request: Request, watch_id: int):
    svc = _services(request)
    try:
        new_alerts = await svc.watch_runner.run_watch(watch_id)
    except BudgetExceeded as exc:
        return RedirectResponse(url=f"/watches?err={quote(str(exc))}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        log.exception("manual watch run failed")
        return RedirectResponse(url=f"/watches?err={quote(f'Check failed: {exc}')}", status_code=303)
    msg = f"Checked — {new_alerts} new alert(s)" if new_alerts else "Checked — no changes"
    return RedirectResponse(url=f"/watches?ok={quote(msg)}", status_code=303)


# ---- cash fares / points value (Phase 4) -----------------------------------------------


@router.post("/value/cash-fare")
async def set_cash_fare(
    request: Request,
    origin: str = Form(...),
    destination: str = Form(...),
    cabin: str = Form(...),
    trip_type: str = Form(default="RT"),
    price: str = Form(default=""),
    next: str = Form(default="/search"),
):
    """Save a manually looked-up cash price for a route+cabin (drives cpp). An empty or zero
    price clears the manual quote so the zone estimate takes over again."""
    svc = _services(request)
    base = next if next.startswith("/") and not next.startswith("//") else "/search"
    sep = "&" if "?" in base else "?"
    try:
        raw = price.replace(",", "").replace("$", "").strip()
        if raw and float(raw) > 0:
            svc.cash_fares.set_fare(origin, destination, cabin, trip_type, float(raw))
            msg = (f"Cash price ${float(raw):,.0f} saved for {origin.upper()}–"
                   f"{destination.upper()} {cabin} ({trip_type})")
        else:
            removed = svc.cash_fares.clear(origin, destination, cabin, trip_type)
            msg = ("Manual cash price cleared — using the zone estimate again"
                   if removed else "No manual cash price was set")
    except ValueError as exc:
        return RedirectResponse(url=f"{base}{sep}err={quote(str(exc))}", status_code=303)
    return RedirectResponse(url=f"{base}{sep}ok={quote(msg)}", status_code=303)


@router.get("/alerts")
async def alerts_page(request: Request):
    svc = _services(request)
    return templates.TemplateResponse(
        "alerts.html",
        {
            "request": request,
            "alerts": svc.alerts.recent(limit=200),
            "notify_enabled": svc.notifier.enabled,
        },
    )


@router.post("/refresh")
async def refresh_network(request: Request, next: str = Form(default="/search")):
    """Manually refresh the outbound-only network snapshot for home airports (fills the catalog
    and the Explore overview). `next` picks the page to bounce back to."""
    svc = _services(request)
    base = next if next.startswith("/") and not next.startswith("//") else "/search"
    sep = "&" if "?" in base else "?"
    errors = []
    for origin in svc.settings.home_airports:
        try:
            pf = await svc.provider.fetch(SCOPE_NETWORK, origin)
            svc.store.persist(pf)
            log.info("manual network refresh ok: %s (%d dests)", origin, len(pf.feed.destinations))
        except Exception as exc:  # noqa: BLE001
            log.exception("manual network refresh failed for %s", origin)
            errors.append(f"{origin}: {exc}")
    url = base + (f"{sep}refresh_error=" + quote(",".join(errors)) if errors else f"{sep}refreshed=1")
    return RedirectResponse(url=url, status_code=303)


# ---- explore (Phase 3) -----------------------------------------------------------------


def _month_bounds(month: str) -> tuple[str, str]:
    """First and last day of a 'YYYY-MM' month."""
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    last = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _lead_view(lead: dict, city_name: str | None, stay: tuple[int, int], value=None) -> dict:
    """A lead row plus the links the template renders: booking deep link + watch-prefill query.

    The watch prefill widens the lead's exact dates to whole months (clamped to today), so the
    watch keeps alerting about the month even after this particular date pair fills up.
    """
    today = date.today().isoformat()
    out_from, out_to = _month_bounds(lead["month"])
    ret_from, ret_to = _month_bounds(lead["inbound_date"][:7])
    watch_qs = urlencode({
        "origin": lead["origin"],
        "destination": lead["destination"],
        "trip_type": "RT",
        "cabin": lead["cabin"],
        "out_from": max(out_from, today),
        "out_to": out_to,
        "ret_from": max(ret_from, today),
        "ret_to": ret_to,
        "min_stay_days": stay[0],
        "max_stay_days": stay[1],
        "label": f"{city_name or lead['destination']} {_month_label(lead['month'])}",
    })
    return {
        **lead,
        "voucher_eligible": bool(lead["voucher_eligible"]),
        "book_url": booking_url(
            lead["origin"], lead["destination"], lead["outbound_date"], lead["inbound_date"],
        ),
        "watch_qs": watch_qs,
        "cpp": value.cpp if value else None,
        "cpp_voucher": value.cpp_voucher if value else None,
        "cash_total": value.cash_total if value else None,
        "cash_source": value.cash_source if value else None,
    }


@router.get("/explore")
async def explore_page(
    request: Request,
    origin: str | None = Query(default=None),
    region: str | None = Query(default=None),
):
    svc = _services(request)
    settings = svc.settings
    origin = (origin or settings.default_home).upper()
    region = (region or "").strip().upper() or None
    q = request.query_params

    overview, snap = svc.explore.overview(origin)
    # Ranks and region counts come from the FULL ranking, so a filtered view keeps the global
    # rank numbers and every region chip stays visible for switching.
    ranks = {o.code: i for i, o in enumerate(overview, start=1)}
    region_counts: dict[str, int] = {}
    for o in overview:
        region_counts[o.region] = region_counts.get(o.region, 0) + 1
    total_dests = len(overview)
    if region:
        overview = [o for o in overview if o.region == region]

    stay = (settings.explore_min_stay_days, settings.explore_max_stay_days)
    leads_raw = svc.explore.leads_for(origin)
    cities = {o.code: o.city_name for o in overview}
    countries = {o.code: o.country_name for o in overview}
    origin_country = svc.store.country_for(origin)
    visible = {o.code for o in overview}
    leads = {
        dest: [
            _lead_view(
                l, cities.get(dest), stay,
                svc.values.value_for(
                    origin=l["origin"], destination=l["destination"], cabin=l["cabin"],
                    trip_type="RT", points_total=l["points_total"],
                    taxes_total=l["taxes_total"],
                    voucher_eligible=bool(l["voucher_eligible"]),
                    origin_country=origin_country, dest_country=countries.get(dest),
                ),
            )
            for l in rows
        ]
        for dest, rows in leads_raw.items()
        if dest in visible
    }

    return templates.TemplateResponse(
        "explore.html",
        {
            "request": request,
            "origin": origin,
            "region": region,
            "regions": sorted(region_counts.items()),
            "total_dests": total_dests,
            "ranks": ranks,
            "homes": settings.home_airports,
            "overview": overview,
            "snapshot": snap,
            "leads": leads,
            "stay": stay,
            "sweep_budget": settings.explore_sweep_budget,
            "flash_ok": q.get("ok") or ("Network snapshot refreshed" if q.get("refreshed") else None),
            "flash_err": q.get("err") or q.get("refresh_error"),
        },
    )


def _explore_url(origin: str, region: str = "") -> str:
    """Base redirect target for Explore POSTs, keeping the active region filter."""
    url = f"/explore?origin={origin}"
    if region:
        url += f"&region={quote(region)}"
    return url


@router.post("/explore/interest")
async def set_interest(
    request: Request,
    origin: str = Form(...),
    destination: str = Form(...),
    interest: int = Form(...),
    region: str = Form(default=""),
):
    svc = _services(request)
    back = _explore_url(origin, region)
    try:
        svc.explore.set_interest(destination, interest)
    except ValueError as exc:
        return RedirectResponse(url=f"{back}&err={quote(str(exc))}", status_code=303)
    return RedirectResponse(
        url=f"{back}&ok={quote(f'{destination.upper()} interest set to {interest}')}",
        status_code=303,
    )


@router.post("/explore/refresh")
async def refresh_leads(
    request: Request,
    origin: str = Form(...),
    destination: str = Form(...),
    region: str = Form(default=""),
):
    """Fetch one route feed now and recompute its round-trip leads."""
    svc = _services(request)
    back = _explore_url(origin, region)
    try:
        count, cached = await svc.explore_sweeper.refresh_destination(origin, destination)
    except BudgetExceeded as exc:
        return RedirectResponse(url=f"{back}&err={quote(str(exc))}", status_code=303)
    except FetchError as exc:
        msg = f"SAS blocked the fetch (Cloudflare): {exc}"
        return RedirectResponse(url=f"{back}&err={quote(msg)}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        log.exception("explore refresh failed")
        return RedirectResponse(
            url=f"{back}&err={quote(f'Refresh failed: {exc}')}", status_code=303
        )
    src = "from a fresh cached snapshot" if cached else "live"
    msg = (
        f"{destination.upper()}: {count} round-trip lead(s) {src}"
        if count else f"{destination.upper()}: no bookable round-trips within {svc.settings.explore_min_stay_days}–{svc.settings.explore_max_stay_days} day stays"
    )
    return RedirectResponse(url=f"{back}&ok={quote(msg)}", status_code=303)


@router.post("/explore/sweep")
async def run_explore_sweep(
    request: Request, origin: str = Form(...), region: str = Form(default="")
):
    """Run a budgeted sweep for one origin right now (same path the nightly job takes)."""
    svc = _services(request)
    back = _explore_url(origin, region)
    try:
        summary = await svc.explore_sweeper.run_origin(origin)
    except Exception as exc:  # noqa: BLE001
        log.exception("manual explore sweep failed")
        return RedirectResponse(
            url=f"{back}&err={quote(f'Sweep failed: {exc}')}", status_code=303
        )
    msg = (
        f"Swept {summary['fetched'] + summary['cached']} of {summary['queued']} queued routes "
        f"({summary['fetched']} fetched, {summary['cached']} from cache) — "
        f"{summary['leads']} lead(s)"
        + (f", {summary['failed']} failed" if summary["failed"] else "")
    )
    return RedirectResponse(url=f"{back}&ok={quote(msg)}", status_code=303)


@router.get("/status")
async def status(request: Request):
    svc = _services(request)
    budget = Budget(svc.settings.db_path, svc.settings.daily_request_budget)
    from app import db

    conn = db.connect(svc.settings.db_path)
    try:
        snaps = [
            dict(r)
            for r in conn.execute(
                """SELECT origin, scope, destination, fetched_at, status, dest_count, byte_size
                   FROM availability_snapshots ORDER BY fetched_at DESC LIMIT 20"""
            ).fetchall()
        ]
        calls_today = [
            dict(r)
            for r in conn.execute(
                """SELECT scope, origin, destination, status, http_status, byte_size, duration_ms, created_at
                   FROM provider_calls ORDER BY id DESC LIMIT 20"""
            ).fetchall()
        ]
    finally:
        conn.close()

    sa_budget = None
    if svc.skyteam is not None:
        sa_budget = Budget(
            svc.settings.db_path, svc.settings.seats_aero_daily_budget, provider="seats_aero",
        )
    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "snapshots": snaps,
            "calls": calls_today,
            "budget_used": budget.used(),
            "budget_remaining": budget.remaining(),
            "budget_limit": svc.settings.daily_request_budget,
            "sa_budget_used": sa_budget.used() if sa_budget else None,
            "sa_budget_limit": svc.settings.seats_aero_daily_budget if sa_budget else None,
            "fetcher_started": svc.fetcher.started,
            "homes": svc.settings.home_airports,
        },
    )
