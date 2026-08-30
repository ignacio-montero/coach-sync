"""The HTTP boundary: extract.fetch_data_type and hevy.fetch_workouts.

WHY THESE ARE *INTEGRATION* TESTS, NOT UNIT TESTS
-------------------------------------------------
Everything else in this suite is a unit test: pure functions, dicts in, dicts
out. These two functions are different — their bug surface IS the boundary. The
things that go wrong are "did the filter survive onto page 2", "did we stop
after page 1 because a key was missing", "does an infinite nextPageToken hang
the cron". None of that is visible from the function's return value alone; you
have to observe the REQUESTS.

So we use `httpx.MockTransport`: a real `httpx.Client` with a fake network
underneath. That is a **fake**, not a mock — it is a working in-memory
implementation of the collaborator rather than a recorded expectation. It keeps
the client's real param encoding, header handling and redirect logic in the test
(which is where the bugs are) while removing the network (which is where the
flakiness is). Monkeypatching `fetch_data_type` itself would test nothing.

Pagination silently truncating is the highest-value bug class here: 65 of 75
workouts quietly missing looks exactly like "he stopped training".
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from coach_sync import extract, hevy
from coach_sync.datatypes import REGISTRY


class Recorder:
    """A fake Google Health endpoint that records every request it served."""

    def __init__(self, pages):
        self.pages = pages          # list of response bodies, in order
        self.requests = []

    def __call__(self, request):
        self.requests.append(dict(httpx.QueryParams(request.url.query)))
        index = min(len(self.requests) - 1, len(self.pages) - 1)
        return httpx.Response(200, json=self.pages[index])

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


# ------------------------------------------------------------ Google Health

def test_every_page_is_followed_and_concatenated():
    rec = Recorder([
        {"dataPoints": [{"n": 1}, {"n": 2}], "nextPageToken": "t1"},
        {"dataPoints": [{"n": 3}], "nextPageToken": "t2"},
        {"dataPoints": [{"n": 4}]},
    ])
    points = extract.fetch_data_type(rec.client(), REGISTRY["weight"], date(2026, 8, 24))
    assert [p["n"] for p in points] == [1, 2, 3, 4]
    assert len(rec.requests) == 3


def test_the_filter_is_carried_onto_every_page():
    """REGRESSION GUARD. If `filter` were dropped when `pageToken` was added,
    page 2 onwards would return unfiltered history — which does not crash, does
    not look wrong in the log, and quietly widens the whole dataset."""
    rec = Recorder([
        {"dataPoints": [{"n": 1}], "nextPageToken": "t1"},
        {"dataPoints": [{"n": 2}]},
    ])
    extract.fetch_data_type(rec.client(), REGISTRY["weight"], date(2026, 8, 24))
    expected = REGISTRY["weight"].filter_expr(date(2026, 8, 24))
    assert all(r.get("filter") == expected for r in rec.requests), rec.requests
    assert rec.requests[1]["pageToken"] == "t1"


def test_the_configured_page_size_is_sent_on_every_page():
    """sleep and exercise cap at 25; sending 100 is rejected by the API."""
    rec = Recorder([{"dataPoints": [], "nextPageToken": "t"}, {"dataPoints": []}])
    extract.fetch_data_type(rec.client(), REGISTRY["sleep"], date(2026, 8, 24))
    assert all(r["pageSize"] == "25" for r in rec.requests)


def test_a_type_with_no_server_side_filter_sends_none():
    """sleep and exercise are fetched unfiltered on purpose (their filter member
    name is unresolved) and windowed client-side instead. Sending a guessed
    filter would return INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER."""
    rec = Recorder([{"dataPoints": []}])
    extract.fetch_data_type(rec.client(), REGISTRY["exercise"], date(2026, 8, 24))
    assert "filter" not in rec.requests[0]


def test_the_hyphenated_path_is_used_in_the_url_not_the_underscored_id():
    """D-016: hyphens in paths, underscores in filters. Getting it wrong returns
    INVALID_PARENT_DATA_TYPE_COLLECTION, which reads like missing data."""
    seen = []

    def handler(request):
        seen.append(str(request.url.path))
        return httpx.Response(200, json={"dataPoints": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extract.fetch_data_type(client, REGISTRY["body_fat"], date(2026, 8, 24))
    assert seen[0].endswith("/dataTypes/body-fat/dataPoints")


@pytest.mark.parametrize("body", [
    {}, {"dataPoints": None}, {"dataPoints": []},
], ids=["no-key", "null", "empty"])
def test_an_empty_page_terminates_cleanly(body):
    rec = Recorder([body])
    assert extract.fetch_data_type(
        rec.client(), REGISTRY["weight"], date(2026, 8, 24)) == []


@pytest.mark.parametrize("token", ["", None, 0], ids=["empty", "null", "zero"])
def test_a_falsy_next_page_token_ends_pagination(token):
    rec = Recorder([{"dataPoints": [{"n": 1}], "nextPageToken": token}])
    points = extract.fetch_data_type(rec.client(), REGISTRY["weight"], date(2026, 8, 24))
    assert len(points) == 1 and len(rec.requests) == 1


def test_a_repeating_page_token_is_capped_rather_than_looping_forever():
    """A server that always returns the same token would otherwise hang the
    nightly job. The guard stops it; this pins WHERE it stops."""
    rec = Recorder([{"dataPoints": [{"n": 1}], "nextPageToken": "same"}])
    points = extract.fetch_data_type(rec.client(), REGISTRY["weight"], date(2026, 8, 24))
    assert len(rec.requests) == 201
    assert len(points) == 201


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_a_non_200_raises_loudly_instead_of_returning_a_partial_dataset(status):
    """The single most important property here. Returning [] on a 403 would
    write an EMPTY metrics_daily.csv over a good one, and the coach would read
    'no data this week' instead of 'the token expired'."""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text="nope")))
    with pytest.raises(RuntimeError) as exc:
        extract.fetch_data_type(client, REGISTRY["weight"], date(2026, 8, 24))
    assert str(status) in str(exc.value)
    assert "weight" in str(exc.value)


def test_a_mid_pagination_failure_raises_rather_than_returning_page_one():
    """Half a week of data silently written as if it were the whole week is
    worse than a failed run."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200, json={"dataPoints": [{"n": 1}], "nextPageToken": "t"})
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        extract.fetch_data_type(client, REGISTRY["weight"], date(2026, 8, 24))


# ------------------------------------------------------------ Hevy

@pytest.fixture
def hevy_server(monkeypatch, tmp_path):
    """Swaps httpx.Client for one wired to a fake Hevy, and returns the log.

    monkeypatch is a pytest fixture that undoes its own patching at the end of
    the test — which is why it is preferred over assigning to the module
    attribute by hand.
    """
    def _serve(handler):
        log = []

        def wrapped(request):
            log.append(dict(httpx.QueryParams(request.url.query)))
            return handler(request)

        class Fake(httpx.Client):
            def __init__(self, **kw):
                super().__init__(transport=httpx.MockTransport(wrapped), **kw)

        monkeypatch.setattr(httpx, "Client", Fake)
        return log, tmp_path
    return _serve


def _page(request, page_count, per_page=2):
    page = int(httpx.QueryParams(request.url.query)["page"])
    return httpx.Response(200, json={
        "page": page, "page_count": page_count,
        "workouts": [{"id": "w{}-{}".format(page, i)} for i in range(per_page)]})


def test_hevy_follows_every_page_up_to_page_count(hevy_server):
    log, tmp = hevy_server(lambda r: _page(r, 3))
    path = hevy.fetch_workouts("key", tmp)
    assert [int(r["page"]) for r in log] == [1, 2, 3]
    assert len(json.loads(path.read_text())) == 6


def test_hevy_sends_the_maximum_page_size(hevy_server):
    """pageSize maxes at 10; anything smaller triples the request count."""
    log, tmp = hevy_server(lambda r: _page(r, 1))
    hevy.fetch_workouts("key", tmp)
    assert log[0]["pageSize"] == "10"


def test_hevy_sends_the_api_key_header(hevy_server, monkeypatch, tmp_path):
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("api-key")
        return httpx.Response(200, json={"page": 1, "page_count": 1, "workouts": []})

    class Fake(httpx.Client):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "Client", Fake)
    hevy.fetch_workouts("secret-key", tmp_path)
    assert seen["key"] == "secret-key"


def test_hevy_stops_at_a_single_page(hevy_server):
    log, tmp = hevy_server(lambda r: _page(r, 1))
    hevy.fetch_workouts("key", tmp)
    assert len(log) == 1


@pytest.mark.xfail(strict=True, reason=(
    "BUG (latent, upstream-triggered): page_count defaults to 1 when absent, so "
    "if Hevy ever renames or drops that field the fetch silently stops after "
    "page 1 — 10 of 75 workouts, no error, no warning. Adherence and every lift "
    "history would quietly collapse. Safer: keep paging while a page returns a "
    "full PAGE_SIZE of workouts."))
def test_BUG_hevy_does_not_silently_truncate_when_page_count_is_missing(hevy_server):
    def handler(request):
        page = int(httpx.QueryParams(request.url.query)["page"])
        if page > 3:
            return httpx.Response(200, json={"workouts": []})
        return httpx.Response(200, json={
            "workouts": [{"id": "w{}-{}".format(page, i)} for i in range(10)]})

    log, tmp = hevy_server(handler)
    path = hevy.fetch_workouts("key", tmp)
    assert len(json.loads(path.read_text())) == 30


def test_hevy_401_is_an_actionable_exit_not_a_generic_error(hevy_server):
    """401 means the key or the Pro subscription lapsed — a human action, so it
    gets its own message rather than being buried in a stack trace."""
    log, tmp = hevy_server(lambda r: httpx.Response(401, text="unauthorized"))
    with pytest.raises(SystemExit) as exc:
        hevy.fetch_workouts("key", tmp)
    assert "HEVY_API_KEY" in str(exc.value)


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_hevy_other_failures_raise_rather_than_writing_an_empty_file(hevy_server, status):
    """Writing an empty hevy_workouts_*.json would become the 'latest' capture
    and erase the lift history on the next build."""
    log, tmp = hevy_server(lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(RuntimeError):
        hevy.fetch_workouts("key", tmp)
    assert list(tmp.glob("hevy_workouts_*.json")) == []


def test_hevy_writes_the_raw_capture_to_disk_for_offline_reparsing(hevy_server):
    """The raw landing zone (bronze layer): the parser can be fixed and re-run
    without re-authenticating or re-fetching."""
    log, tmp = hevy_server(lambda r: _page(r, 1))
    path = hevy.fetch_workouts("key", tmp)
    assert path.parent == tmp and path.name.startswith("hevy_workouts_")
    assert json.loads(path.read_text())


def test_latest_raw_picks_the_newest_capture(tmp_path):
    """Timestamps are %Y%m%dT%H%M%SZ, so lexicographic sort == chronological.
    That is only true for zero-padded UTC stamps — pin it before someone
    'improves' the filename format."""
    for stamp in ["20260830T090000Z", "20260830T120017Z", "20260829T235959Z"]:
        (tmp_path / "weight_{}.json".format(stamp)).write_text("[]")
    assert extract.latest_raw(tmp_path, "weight").name == "weight_20260830T120017Z.json"


def test_latest_raw_is_none_when_nothing_has_been_fetched(tmp_path):
    assert extract.latest_raw(tmp_path, "weight") is None


def test_latest_raw_does_not_confuse_prefixed_type_names(tmp_path):
    """`daily_heart_rate_variability` must not match a `daily_heart_rate_*` file
    and vice versa — the glob is `name_*`, so a shared prefix is a real risk."""
    (tmp_path / "daily_resting_heart_rate_20260830T120000Z.json").write_text("[]")
    assert extract.latest_raw(tmp_path, "daily_heart_rate_variability") is None
