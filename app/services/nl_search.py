"""Natural-language search: one Claude Haiku call turns a free-text query into structured
SkyTeam search params via a forced tool call, so the answer is always schema-valid JSON.

Deliberately thin: Claude only PARSES ("Asia in October, voucher") — region expansion, seat
filters, and the actual availability search stay in SkyTeamService, where they're deterministic
and testable. Calls are not counted against `provider_calls` budgets: Haiku costs fractions of a
cent and never touches SAS or seats.aero.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import anthropic

log = logging.getLogger("award_finder.nl")


class NLParseError(RuntimeError):
    """The query couldn't be turned into search params (API error, refusal, bad output)."""


@dataclass(frozen=True, slots=True)
class NLParams:
    origins: list[str]
    destinations: list[str]
    region: str | None
    date_from: str | None
    date_to: str | None
    cabin: str | None            # AG/AP/AB
    min_seats: int | None
    voucher_intent: bool
    trip_type: str | None        # "RT"/"OW"/None
    summary: str                 # one-sentence interpretation, shown to the user


_TOOL_NAME = "set_search"


def _tool_schema(regions: list[str]) -> dict:
    return {
        "name": _TOOL_NAME,
        "description": "Record the structured award-flight search this query asks for.",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin_airports": {
                    "type": "array", "items": {"type": "string"},
                    "description": "IATA codes; empty when the query names no origin.",
                },
                "destination_airports": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Specific destination IATA codes, if any were named.",
                },
                "destination_region": {
                    "anyOf": [{"type": "string", "enum": regions}, {"type": "null"}],
                    "description": "Broad destination area (continent-level asks) — prefer this over guessing airports.",
                },
                "date_from": {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "YYYY-MM-DD"},
                "date_to": {"anyOf": [{"type": "string"}, {"type": "null"}],
                            "description": "YYYY-MM-DD"},
                "cabin": {"anyOf": [{"type": "string", "enum": ["AG", "AP", "AB"]},
                                    {"type": "null"}]},
                "min_seats": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "voucher_intent": {"type": "boolean"},
                "trip_type": {"anyOf": [{"type": "string", "enum": ["RT", "OW"]},
                                        {"type": "null"}]},
                "summary": {"type": "string"},
            },
            "required": [
                "origin_airports", "destination_airports", "destination_region",
                "date_from", "date_to", "cabin", "min_seats", "voucher_intent",
                "trip_type", "summary",
            ],
            "additionalProperties": False,
        },
    }


def _system_prompt(today: str, home_airports: list[str], regions: list[str]) -> str:
    return (
        f"You translate award-flight search queries into structured parameters via the "
        f"{_TOOL_NAME} tool. Today is {today}.\n"
        f"- The user's home airports are {', '.join(home_airports)}; leave origin_airports "
        f"empty unless the query names a different origin.\n"
        f"- Allowed destination_region values (zone groups from the EuroBonus points table): "
        f"{', '.join(regions)}. For continent-level asks ('Asia', 'the Middle East') set "
        f"destination_region and leave destination_airports empty; for named cities/airports "
        f"use IATA codes.\n"
        f"- Month or season mentions resolve to the NEXT future occurrence: full calendar "
        f"bounds (e.g. 'October' -> the coming October 1st to 31st).\n"
        f"- Cabin: economy -> AG, premium economy -> AP, business -> AB. Null when unstated.\n"
        f"- voucher_intent is true when the query mentions a 2-for-1 / companion voucher or "
        f"traveling as a pair on one award.\n"
        f"- summary: one short sentence stating your interpretation, e.g. "
        f"'Flights CPH → Asia, 2026-10-01 to 2026-10-31, any cabin, 2-for-1 voucher intent.'"
    )


class NLQueryService:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        home_airports: list[str],
        regions: list[str],
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._homes = home_airports
        self._regions = regions
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=15.0, max_retries=1,
        )

    async def parse(self, query: str) -> NLParams:
        today = date.today()
        try:
            msg = await self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=_system_prompt(today.isoformat(), self._homes, self._regions),
                tools=[_tool_schema(self._regions)],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": query}],
            )
        except anthropic.AnthropicError as exc:
            log.warning("NL parse API call failed: %s", exc)
            raise NLParseError(
                f"Couldn't reach the language model — use the form below. ({exc.__class__.__name__})"
            ) from exc

        tool_use = next(
            (b for b in msg.content if getattr(b, "type", None) == "tool_use"), None
        )
        if tool_use is None:
            log.warning("NL parse returned no tool_use (stop_reason=%s)", msg.stop_reason)
            raise NLParseError("Couldn't interpret the query — use the form below.")
        data = tool_use.input

        def _codes(values: object) -> list[str]:
            if not isinstance(values, list):
                return []
            return [str(v).strip().upper() for v in values if str(v).strip()]

        date_from = self._valid_date(data.get("date_from"))
        date_to = self._valid_date(data.get("date_to"))
        today_iso = today.isoformat()
        # Fully-past windows mean the model ignored "next occurrence" — reject rather than
        # silently search the wrong dates.
        if date_to and date_to < today_iso:
            raise NLParseError(
                "The query resolved to dates in the past — add a year (e.g. 'October 2027') "
                "or use the form below."
            )
        if date_from and date_from < today_iso:
            date_from = today_iso

        origins = _codes(data.get("origin_airports")) or list(self._homes)
        region = data.get("destination_region") or None
        return NLParams(
            origins=origins,
            destinations=_codes(data.get("destination_airports")),
            region=str(region).upper() if region else None,
            date_from=date_from,
            date_to=date_to,
            cabin=data.get("cabin") or None,
            min_seats=data.get("min_seats"),
            voucher_intent=bool(data.get("voucher_intent")),
            trip_type=data.get("trip_type") or None,
            summary=str(data.get("summary") or "").strip() or "Interpreted your query.",
        )

    @staticmethod
    def _valid_date(value: object) -> str | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError:
            return None
