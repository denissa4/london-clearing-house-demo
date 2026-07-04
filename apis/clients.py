"""Upstream clients for the PoC data APIs.

Fetching is split from parsing so the parsers can be unit-tested against captured
payloads with no network (and with only the stdlib). `httpx` is imported lazily
inside the fetch helpers for the same reason.

All sources are public and need no API key. A descriptive User-Agent is sent
because some upstreams (e.g. the NY Fed) reject generic bot agents.
"""
import csv
import io
import time
import urllib.parse
from datetime import date, timedelta
from typing import Optional

USER_AGENT = "lch-mcp-poc/1.0 (+https://github.com/denissa4/london-clearing-house-demo)"
_TIMEOUT = 15.0

# Reference rates publish roughly once a business day, and GLEIF rate-limits to
# ~60 req/min — a short in-process TTL cache covers both comfortably.
_TTL_SECONDS = 3600
_CACHE: dict = {}


def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _TTL_SECONDS:
        return hit[1]
    return None


def _cache_put(key, value):
    _CACHE[key] = (time.time(), value)


def _get_json(url: str, headers: Optional[dict] = None):
    import httpx
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    r = httpx.get(url, headers=h, timeout=_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def _get_text(url: str, headers: Optional[dict] = None) -> str:
    import httpx
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    r = httpx.get(url, headers=h, timeout=_TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# Reference rates: SOFR (NY Fed), ESTR (ECB), SONIA (Bank of England)
# ---------------------------------------------------------------------------
RATE_META = {
    "SOFR": {"name": "Secured Overnight Financing Rate", "currency": "USD",
             "source": "Federal Reserve Bank of New York"},
    "ESTR": {"name": "Euro Short-Term Rate", "currency": "EUR",
             "source": "European Central Bank"},
    "SONIA": {"name": "Sterling Overnight Index Average", "currency": "GBP",
              "source": "Bank of England"},
}

# ECB SDMX series key for the €STR volume-weighted trimmed mean rate.
_ESTR_SERIES = "B.EU000A2X2A25.WT"


def parse_sofr(payload: dict) -> list[dict]:
    """NY Fed refRates JSON -> [{date, value, volume_billions}] ascending."""
    out = []
    for r in payload.get("refRates", []):
        if r.get("type") != "SOFR":
            continue
        out.append({"date": r.get("effectiveDate"),
                    "value": r.get("percentRate"),
                    "volume_billions": r.get("volumeInBillions")})
    out.sort(key=lambda x: x["date"] or "")
    return out


def parse_estr(payload: dict) -> list[dict]:
    """ECB SDMX-JSON -> [{date, value}] ascending."""
    series = payload["dataSets"][0]["series"]
    skey = next(iter(series))
    obs = series[skey]["observations"]
    times = payload["structure"]["dimensions"]["observation"][0]["values"]
    out = []
    for okey, val in obs.items():
        idx = int(okey)
        out.append({"date": times[idx]["id"], "value": val[0] if val else None})
    out.sort(key=lambda x: x["date"])
    return out


def parse_sonia(text: str) -> list[dict]:
    """BoE IADB CSV ('DATE,IUDSOIA', dates like '01 Jul 2026') -> [{date, value}] ascending."""
    out = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) < 2:
            continue
        raw = row[0].strip()
        try:
            iso = time.strftime("%Y-%m-%d", time.strptime(raw, "%d %b %Y"))
        except ValueError:
            iso = raw
        try:
            value = float(row[1])
        except ValueError:
            continue
        out.append({"date": iso, "value": value})
    out.sort(key=lambda x: x["date"])
    return out


def _to_boe_date(s: str) -> str:
    """'2026-07-01' -> '01/Jul/2026'; pass through if already in BoE format."""
    try:
        return date.fromisoformat(s).strftime("%d/%b/%Y")
    except ValueError:
        return s


def fetch_rate(rate_id: str, start: Optional[str] = None, end: Optional[str] = None) -> list[dict]:
    """Return observations (ascending) for a rate, optionally within [start, end] ISO dates."""
    rate_id = rate_id.upper()
    if rate_id not in RATE_META:
        raise KeyError(rate_id)
    ck = ("rate", rate_id, start, end)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    if rate_id == "SOFR":
        if start or end:
            url = ("https://markets.newyorkfed.org/api/rates/secured/sofr/search.json"
                   f"?startDate={start or '2018-01-01'}&endDate={end or date.today().isoformat()}")
        else:
            url = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json"
        data = parse_sofr(_get_json(url))
    elif rate_id == "ESTR":
        n = 250 if (start or end) else 1
        url = (f"https://data-api.ecb.europa.eu/service/data/EST/{_ESTR_SERIES}"
               f"?lastNObservations={n}&format=jsondata")
        data = parse_estr(_get_json(url, headers={"Accept": "application/json"}))
        if start:
            data = [d for d in data if d["date"] >= start]
        if end:
            data = [d for d in data if d["date"] <= end]
    else:  # SONIA
        if not start and not end:
            end_d = date.today()
            df, dt = (end_d - timedelta(days=30)).strftime("%d/%b/%Y"), end_d.strftime("%d/%b/%Y")
        else:
            df = _to_boe_date(start or "2018-01-01")
            dt = _to_boe_date(end or date.today().isoformat())
        url = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
               f"?csv.x=yes&SeriesCodes=IUDSOIA&UsingCodes=Y&CSVF=TN&Datefrom={df}&Dateto={dt}")
        data = parse_sonia(_get_text(url))

    _cache_put(ck, data)
    return data


# ---------------------------------------------------------------------------
# Legal entities: GLEIF Global LEI index
# ---------------------------------------------------------------------------
GLEIF_BASE = "https://api.gleif.org/api/v1"
_GLEIF_HEADERS = {"Accept": "application/vnd.api+json"}


def _entity_from_record(rec: dict) -> dict:
    a = rec.get("attributes", {}) or {}
    ent = a.get("entity", {}) or {}
    legal = ent.get("legalAddress", {}) or {}
    return {
        "lei": rec.get("id"),
        "name": (ent.get("legalName") or {}).get("name"),
        "status": ent.get("status"),
        "legal_form": (ent.get("legalForm") or {}).get("id"),
        "jurisdiction": ent.get("jurisdiction"),
        "legal_address": {
            "country": legal.get("country"),
            "city": legal.get("city"),
            "postal_code": legal.get("postalCode"),
        },
        "registration_status": (a.get("registration") or {}).get("status"),
    }


def _gleif_404_to_none(fn):
    """Run fn(); treat an httpx 404 as 'no record' -> None."""
    import httpx
    try:
        return fn()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


def search_entities(name: str, limit: int = 10) -> list[dict]:
    ck = ("ent-search", name.lower(), limit)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    q = urllib.parse.quote(name)
    url = f"{GLEIF_BASE}/lei-records?filter[entity.legalName]={q}&page[size]={limit}"
    payload = _get_json(url, headers=_GLEIF_HEADERS)
    out = [_entity_from_record(r) for r in payload.get("data", [])]
    _cache_put(ck, out)
    return out


def get_entity(lei: str) -> Optional[dict]:
    lei = lei.upper()
    ck = ("ent", lei)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    payload = _gleif_404_to_none(
        lambda: _get_json(f"{GLEIF_BASE}/lei-records/{lei}", headers=_GLEIF_HEADERS))
    out = _entity_from_record(payload["data"]) if payload else None
    _cache_put(ck, out)
    return out


def get_relationships(lei: str) -> dict:
    lei = lei.upper()

    def _rel(kind):
        payload = _gleif_404_to_none(
            lambda: _get_json(f"{GLEIF_BASE}/lei-records/{lei}/{kind}", headers=_GLEIF_HEADERS))
        data = payload.get("data") if payload else None
        return _entity_from_record(data) if data else None

    return {
        "lei": lei,
        "direct_parent": _rel("direct-parent"),
        "ultimate_parent": _rel("ultimate-parent"),
    }
