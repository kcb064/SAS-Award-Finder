"""seats.aero fallback provider (Phase 4) — the pivot insurance made real.

Implements the same `AwardProvider` protocol as `sas_direct` on top of the seats.aero Partner API
(Pro subscription, ~$10/mo, 1,000 calls/day). A plain authenticated JSON API: no Cloudflare, no
browser — this is the contingency for when direct SAS scraping breaks, and the only path to
SkyTeam partner award space. All seats.aero knowledge lives in `endpoints.py` + `parser.py`.
"""
