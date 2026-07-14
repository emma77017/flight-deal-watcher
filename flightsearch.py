"""Google Flights round-trip search via fast-flights, with a fault-tolerant parser.

The stock fast_flights parser crashes on itineraries without a price
(common in business class), so we parse the raw JS payload ourselves and
skip anything malformed instead of dying.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from selectolax.lexbor import LexborHTMLParser

from fast_flights import Passengers, create_query, fetch_flights_html
from fast_flights.querying import FlightQuery

log = logging.getLogger(__name__)


class SearchError(Exception):
    """Fetch failed or the page didn't contain flight data (possible block)."""


@dataclass
class Leg:
    from_code: str
    to_code: str
    dep: datetime
    arr: datetime
    duration_min: int
    plane: str


@dataclass
class Itinerary:
    price_total: int          # for all passengers, taxes included
    airlines: list[str]
    legs: list[Leg]
    layovers_min: list[int] = field(default_factory=list)

    @property
    def stops(self) -> int:
        return len(self.legs) - 1

    @property
    def total_duration_min(self) -> int:
        return sum(l.duration_min for l in self.legs) + sum(self.layovers_min)


@dataclass
class SearchResult:
    itineraries: list[Itinerary]
    url: str
    # Google's price insight for this route+dates (total for all passengers)
    current_lowest: int | None = None
    typical_low: int | None = None
    typical_high: int | None = None


def _to_dt(date_part, time_part) -> datetime:
    y, m, d = date_part
    hh, mm = 0, 0
    if isinstance(time_part, list):
        if len(time_part) >= 1 and time_part[0] is not None:
            hh = time_part[0]
        if len(time_part) >= 2 and time_part[1] is not None:
            mm = time_part[1]
    return datetime(y, m, d, hh, mm)


def _parse_itinerary(k) -> Itinerary | None:
    flight = k[0]
    try:
        price = k[1][0][1]
    except (IndexError, TypeError):
        price = None
    if not price:  # "price unavailable" rows are useless for deal hunting
        return None

    airlines = [a for a in flight[1] if isinstance(a, str)]
    legs = []
    for seg in flight[2]:
        legs.append(
            Leg(
                from_code=seg[3],
                to_code=seg[6],
                dep=_to_dt(seg[20], seg[8]),
                arr=_to_dt(seg[21], seg[10]),
                duration_min=seg[11] or 0,
                plane=seg[17] or "",
            )
        )
    if not legs:
        return None

    layovers = []
    for prev, nxt in zip(legs, legs[1:]):
        # both times are local at the connection airport, so the difference is real
        layovers.append(int((nxt.dep - prev.arr).total_seconds() // 60))

    return Itinerary(price_total=int(price), airlines=airlines, legs=legs, layovers_min=layovers)


def _parse_payload(payload, url: str) -> SearchResult:
    itineraries: list[Itinerary] = []
    seen = set()
    # payload[2] and payload[3] are two result groups ("other" / "best")
    for grp in (2, 3):
        part = payload[grp] if len(payload) > grp else None
        if not (isinstance(part, list) and part and isinstance(part[0], list)):
            continue
        for k in part[0]:
            try:
                it = _parse_itinerary(k)
            except Exception as e:
                log.debug("skipping unparseable itinerary: %s", e)
                continue
            if it is None:
                continue
            key = (it.price_total, tuple((l.from_code, l.to_code, l.dep.isoformat()) for l in it.legs))
            if key in seen:
                continue
            seen.add(key)
            itineraries.append(it)

    result = SearchResult(itineraries=itineraries, url=url)
    # price insights: [_, [_, current], [_, low], [_, diff], [_, typical_low], [_, typical_high], ...]
    try:
        ins = payload[5]
        if isinstance(ins, list) and len(ins) >= 6:
            result.current_lowest = ins[1][1] if isinstance(ins[1], list) else None
            result.typical_low = ins[4][1] if isinstance(ins[4], list) else None
            result.typical_high = ins[5][1] if isinstance(ins[5], list) else None
    except (IndexError, TypeError):
        pass
    return result


def search_round_trip(
    origin: str,
    destination: str,
    dep_date: str,
    ret_date: str | None,
    *,
    adults: int = 2,
    seat: str = "business",
    currency: str = "USD",
    max_stops: int = 1,
) -> SearchResult:
    """One search. With ret_date, itineraries are outbound options priced for the
    full round trip; with ret_date=None it's a one-way search.

    Raises SearchError when the page couldn't be fetched or parsed at all.
    """
    flights = [FlightQuery(date=dep_date, from_airport=origin, to_airport=destination)]
    if ret_date:
        flights.append(FlightQuery(date=ret_date, from_airport=destination, to_airport=origin))
    q = create_query(
        flights=flights,
        trip="round-trip" if ret_date else "one-way",
        seat=seat,
        passengers=Passengers(adults=adults),
        currency=currency,
        max_stops=max_stops,
    )
    url = q.url()

    try:
        html = fetch_flights_html(q)
    except Exception as e:
        raise SearchError(f"fetch failed: {e}") from e

    try:
        parser = LexborHTMLParser(html)
        script = parser.css_first(r"script.ds\:1")
        if script is None:
            raise SearchError("no flight data in page (blocked or layout change?)")
        data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
        if data.endswith("errorHasStatus: true"):
            return SearchResult(itineraries=[], url=url)  # genuinely no flights
        payload = json.loads(data)
        return _parse_payload(payload, url)
    except SearchError:
        raise
    except Exception as e:
        # any unexpected page/format change must degrade to a countable failure,
        # never crash a scan
        raise SearchError(f"parse failed ({type(e).__name__}): {e}") from e
