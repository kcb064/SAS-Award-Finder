"""NL search: the forced-tool Claude call is stubbed — these tests pin the prompt contract
(today's date, regions, homes) and the validation/fallback rules around the model's output."""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import anthropic
import pytest

from app.services.nl_search import NLParams, NLParseError, NLQueryService

REGIONS = ["ASIA", "EUROPE", "NORTH_AMERICA"]


def _tool_response(**overrides) -> SimpleNamespace:
    payload = {
        "origin_airports": [],
        "destination_airports": [],
        "destination_region": "ASIA",
        "date_from": (date.today() + timedelta(days=30)).isoformat(),
        "date_to": (date.today() + timedelta(days=60)).isoformat(),
        "cabin": "AB",
        "min_seats": 2,
        "voucher_intent": True,
        "trip_type": "RT",
        "summary": "Flights CPH → Asia, business, 2-for-1 voucher intent.",
    }
    payload.update(overrides)
    block = SimpleNamespace(type="tool_use", name="set_search", input=payload)
    return SimpleNamespace(content=[block], stop_reason="tool_use")


class StubClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                if error is not None:
                    raise error
                return response

        self.messages = _Messages()


def _service(client) -> NLQueryService:
    return NLQueryService(
        "key", model="claude-haiku-4-5-20251001",
        home_airports=["CPH", "OSL"], regions=REGIONS, client=client,
    )


async def test_happy_path_maps_to_params():
    client = StubClient(_tool_response())
    params = await _service(client).parse("flights to asia for my voucher")
    assert isinstance(params, NLParams)
    assert params.origins == ["CPH", "OSL"]      # empty origins -> home airports
    assert params.region == "ASIA"
    assert params.cabin == "AB"
    assert params.voucher_intent is True
    assert params.min_seats == 2
    assert "voucher" in params.summary


async def test_prompt_contains_today_homes_and_regions():
    client = StubClient(_tool_response())
    await _service(client).parse("anything")
    call = client.calls[0]
    assert date.today().isoformat() in call["system"]
    assert "CPH, OSL" in call["system"]
    assert all(r in call["system"] for r in REGIONS)
    assert call["tool_choice"] == {"type": "tool", "name": "set_search"}
    # The region enum in the tool schema is the injected list.
    schema = call["tools"][0]["input_schema"]
    assert schema["properties"]["destination_region"]["anyOf"][0]["enum"] == REGIONS


async def test_past_start_date_clamped_to_today():
    client = StubClient(_tool_response(
        date_from="2020-01-01",
        date_to=(date.today() + timedelta(days=10)).isoformat(),
    ))
    params = await _service(client).parse("flights soon")
    assert params.date_from == date.today().isoformat()


async def test_fully_past_window_is_rejected():
    client = StubClient(_tool_response(date_from="2020-10-01", date_to="2020-10-31"))
    with pytest.raises(NLParseError, match="past"):
        await _service(client).parse("flights last october")


async def test_named_airports_are_uppercased():
    client = StubClient(_tool_response(
        origin_airports=["arn"], destination_airports=["bkk", " nrt "],
        destination_region=None,
    ))
    params = await _service(client).parse("arn to bangkok or tokyo")
    assert params.origins == ["ARN"]
    assert params.destinations == ["BKK", "NRT"]
    assert params.region is None


async def test_api_error_becomes_nl_parse_error():
    client = StubClient(error=anthropic.AnthropicError("boom"))
    with pytest.raises(NLParseError, match="use the form"):
        await _service(client).parse("flights to asia")


async def test_missing_tool_use_becomes_nl_parse_error():
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="cannot")], stop_reason="end_turn",
    )
    client = StubClient(resp)
    with pytest.raises(NLParseError, match="use the form"):
        await _service(client).parse("gibberish")
