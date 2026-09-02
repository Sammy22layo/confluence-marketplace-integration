"""
smoke_test_apis.py
==================

Verifies the three free APIs before a single ADF Web activity is built.

Everything this script finds is free to find. The same problem discovered
inside ADF costs debug runs, and debug runs are billed.

It answers four questions:

  1. Do the endpoints work, and what exactly comes back?
  2. How many calls does a full history need? (drives ForEach design)
  3. How big is the FX gap? (drives the date spine in silver)
  4. What are the join grains? (drives the conformed model)

Samples are written to data/samples/ so the repo stays reproducible for
anyone reading it without an Azure subscription.

Usage
-----
    python scripts/smoke_test_apis.py
    python scripts/smoke_test_apis.py --out-dir data/samples

Requires: requests, pandas
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

WINDOW_START = date(2017, 1, 1)
WINDOW_END = date(2018, 10, 31)
TIMEOUT = 30

FRANKFURTER = "https://api.frankfurter.dev/v1"
NAGER = "https://date.nager.at/api/v3"
OPENMETEO = "https://archive-api.open-meteo.com/v1/archive"

# Weather is pulled at REGION grain, not city grain.
#
# Olist customers span 4,119 distinct cities, so no practical city list gets
# useful coverage: the top 5 reach 28% of orders and the top 20 only 42%.
# There are 27 Brazilian states and 9 South African provinces, and every
# order carries one. Pulling a representative city per region gives 100%
# join coverage at the cost of precision within large states.
#
# Known limitation, stated in the README: Sao Paulo state is roughly the
# size of the UK, so its capital's weather is a coarse proxy for the whole
# region. Acceptable, because the feature is a proxy for route conditions
# over a multi-day transit window either way.
#
# (region_code, region_name, country_code, representative_city, lat, lon)
REGIONS = [
    # --- Brazil: 27 state capitals -------------------------------------
    ("AC", "Acre",                "BR", "Rio Branco",      -9.9747, -67.8100),
    ("AL", "Alagoas",             "BR", "Maceio",          -9.6658, -35.7353),
    ("AP", "Amapa",               "BR", "Macapa",           0.0389, -51.0664),
    ("AM", "Amazonas",            "BR", "Manaus",          -3.1190, -60.0217),
    ("BA", "Bahia",               "BR", "Salvador",       -12.9777, -38.5016),
    ("CE", "Ceara",               "BR", "Fortaleza",       -3.7319, -38.5267),
    ("DF", "Distrito Federal",    "BR", "Brasilia",       -15.7939, -47.8828),
    ("ES", "Espirito Santo",      "BR", "Vitoria",        -20.3155, -40.3128),
    ("GO", "Goias",               "BR", "Goiania",        -16.6869, -49.2648),
    ("MA", "Maranhao",            "BR", "Sao Luis",        -2.5297, -44.3028),
    ("MT", "Mato Grosso",         "BR", "Cuiaba",         -15.6014, -56.0979),
    ("MS", "Mato Grosso do Sul",  "BR", "Campo Grande",   -20.4697, -54.6201),
    ("MG", "Minas Gerais",        "BR", "Belo Horizonte", -19.9167, -43.9345),
    ("PA", "Para",                "BR", "Belem",           -1.4558, -48.5044),
    ("PB", "Paraiba",             "BR", "Joao Pessoa",     -7.1195, -34.8450),
    ("PR", "Parana",              "BR", "Curitiba",       -25.4284, -49.2733),
    ("PE", "Pernambuco",          "BR", "Recife",          -8.0476, -34.8770),
    ("PI", "Piaui",               "BR", "Teresina",        -5.0892, -42.8019),
    ("RJ", "Rio de Janeiro",      "BR", "Rio de Janeiro", -22.9068, -43.1729),
    ("RN", "Rio Grande do Norte", "BR", "Natal",           -5.7945, -35.2110),
    ("RS", "Rio Grande do Sul",   "BR", "Porto Alegre",   -30.0346, -51.2177),
    ("RO", "Rondonia",            "BR", "Porto Velho",     -8.7619, -63.9039),
    ("RR", "Roraima",             "BR", "Boa Vista",        2.8235, -60.6758),
    ("SC", "Santa Catarina",      "BR", "Florianopolis",  -27.5954, -48.5480),
    ("SP", "Sao Paulo",           "BR", "Sao Paulo",      -23.5505, -46.6333),
    ("SE", "Sergipe",             "BR", "Aracaju",        -10.9472, -37.0731),
    ("TO", "Tocantins",           "BR", "Palmas",         -10.1689, -48.3317),
    # --- South Africa: 9 provinces -------------------------------------
    ("GP", "Gauteng",             "ZA", "Johannesburg",   -26.2041,  28.0473),
    ("WC", "Western Cape",        "ZA", "Cape Town",      -33.9249,  18.4241),
    ("KZN", "KwaZulu-Natal",      "ZA", "Durban",         -29.8587,  31.0218),
    ("EC", "Eastern Cape",        "ZA", "Gqeberha",       -33.9608,  25.6022),
    ("FS", "Free State",          "ZA", "Bloemfontein",   -29.0852,  26.1596),
    ("LP", "Limpopo",             "ZA", "Polokwane",      -23.9045,  29.4689),
    ("MP", "Mpumalanga",          "ZA", "Mbombela",       -25.4753,  30.9694),
    ("NW", "North West",          "ZA", "Mahikeng",       -25.8560,  25.6403),
    ("NC", "Northern Cape",       "ZA", "Kimberley",      -28.7282,  24.7499),
]


def banner(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def call(url: str, params: dict | None = None) -> tuple[object | None, dict]:
    """Single request with timing. Never raises: a failed endpoint is a
    finding, not a crash."""
    meta = {"url": url, "params": params or {}}
    try:
        t0 = time.perf_counter()
        r = requests.get(url, params=params, timeout=TIMEOUT)
        meta["ms"] = round((time.perf_counter() - t0) * 1000)
        meta["status"] = r.status_code
        meta["bytes"] = len(r.content)
        if r.status_code != 200:
            meta["body"] = r.text[:400]
            print(f"  FAILED  {r.status_code}  {r.url}")
            print(f"          {r.text[:300]}")
            return None, meta
        print(f"  ok  {r.status_code}  {meta['ms']:>5} ms  {meta['bytes']:>9,} bytes")
        return r.json(), meta
    except Exception as e:                                    # noqa: BLE001
        meta["status"] = "EXCEPTION"
        meta["body"] = str(e)
        print(f"  FAILED  {type(e).__name__}: {e}")
        return None, meta


# --------------------------------------------------------------------------
# 1. Frankfurter — FX
# --------------------------------------------------------------------------

def test_frankfurter(out: Path) -> dict:
    banner("1. FRANKFURTER  (FX rates, ECB)")
    findings = {}

    print("\nSingle date, BRL -> USD:")
    single, _ = call(f"{FRANKFURTER}/2017-06-15", {"base": "BRL", "symbols": "USD"})
    if single:
        print(f"    {json.dumps(single)[:200]}")

    for cur in ("BRL", "ZAR"):
        print(f"\nFull window, {cur} -> USD "
              f"({WINDOW_START} .. {WINDOW_END}):")
        data, meta = call(
            f"{FRANKFURTER}/{WINDOW_START}..{WINDOW_END}",
            {"base": cur, "symbols": "USD"},
        )
        if not data:
            findings[cur] = {"ok": False}
            continue

        rates = data.get("rates", {})
        got = sorted(pd.to_datetime(list(rates.keys())).date)
        calendar_days = (WINDOW_END - WINDOW_START).days + 1
        missing = calendar_days - len(got)

        # longest run of consecutive calendar days with no published rate
        longest, run, cur_day = 0, 0, WINDOW_START
        have = set(got)
        while cur_day <= WINDOW_END:
            run = run + 1 if cur_day not in have else 0
            longest = max(longest, run)
            cur_day += timedelta(days=1)

        print(f"    rates returned : {len(got):,}")
        print(f"    calendar days  : {calendar_days:,}")
        print(f"    MISSING DAYS   : {missing:,}  ({missing / calendar_days:.1%})")
        print(f"    longest gap    : {longest} consecutive days")
        print(f"    first / last   : {got[0]} / {got[-1]}")

        (out / f"frankfurter_{cur.lower()}_usd.json").write_text(
            json.dumps(data, indent=2)[:400_000])
        findings[cur] = {"ok": True, "returned": len(got),
                         "missing": missing, "longest_gap": longest}

    print("\n  >> One call covers the entire window. No pagination needed.")
    print("  >> The missing days are weekends and ECB holidays. Any order")
    print("     placed on one of them has NO rate. This is why silver needs")
    print("     a date spine with forward fill, not a plain join.")
    return findings


# --------------------------------------------------------------------------
# 2. Nager.Date — public holidays
# --------------------------------------------------------------------------

def test_nager(out: Path) -> dict:
    banner("2. NAGER.DATE  (public holidays)")
    rows, findings = [], {}

    for country in ("BR", "ZA"):
        for year in (2017, 2018):
            print(f"\n{country} {year}:")
            data, _ = call(f"{NAGER}/PublicHolidays/{year}/{country}")
            if not data:
                continue
            print(f"    holidays: {len(data)}")
            if year == 2017 and country == "BR":
                print(f"    keys: {sorted(data[0].keys())}")
                print(f"    sample: {json.dumps(data[0])[:220]}")
            for h in data:
                rows.append({
                    "country_code": country,
                    "holiday_date": h.get("date"),
                    "local_name": h.get("localName"),
                    "name": h.get("name"),
                    "is_global": h.get("global"),
                    "counties": ",".join(h.get("counties") or []),
                    "types": ",".join(h.get("types") or []),
                })
            findings[f"{country}{year}"] = len(data)

    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(out / "nager_holidays.csv", index=False)
        regional = (~df.is_global.fillna(True)).sum()
        print(f"\n  total rows: {len(df)}  |  non-national (regional): {regional}")
        print("\n  >> 4 calls total, one per country-year. A ForEach over a")
        print("     country x year array is the natural ADF shape.")
        if regional:
            print("  >> Some holidays are regional, not national. Grain is")
            print("     country + date + county, NOT country + date.")
    return findings


# --------------------------------------------------------------------------
# 3. Open-Meteo — historical weather
# --------------------------------------------------------------------------

def test_openmeteo(out: Path) -> dict:
    banner(f"3. OPEN-METEO  (historical weather archive, {len(REGIONS)} regions)")
    frames, findings, failed = [], {}, []

    for code, region, cc, city, lat, lon in REGIONS:
        data, _ = call(OPENMETEO, {
            "latitude": lat, "longitude": lon,
            "start_date": WINDOW_START.isoformat(),
            "end_date": WINDOW_END.isoformat(),
            "daily": "precipitation_sum,wind_speed_10m_max,temperature_2m_max",
            "timezone": "auto",
        })
        if not data:
            print(f"    ^ {cc}-{code} {region} FAILED")
            print("      if this 400s on wind_speed_10m_max, the older name")
            print("      windspeed_10m_max may be required. Check the reason.")
            failed.append(f"{cc}-{code}")
            continue

        df = pd.DataFrame(data.get("daily", {}))
        tz = data.get("timezone")
        nulls = int(df.isna().sum().sum())
        print(f"  {cc}-{code:<4} {region:<20} {city:<16} "
              f"{len(df):>4} days  tz={tz}  nulls={nulls}")

        df.insert(0, "region_code", code)
        df.insert(1, "region_name", region)
        df.insert(2, "country_code", cc)
        df.insert(3, "observed_city", city)
        df.insert(4, "resolved_timezone", tz)
        frames.append(df)
        findings[f"{cc}-{code}"] = {"days": len(df), "nulls": nulls, "tz": tz}

    if not frames:
        return findings

    allw = pd.concat(frames, ignore_index=True)
    allw.to_csv(out / "openmeteo_daily_by_region.csv", index=False)

    expected = (WINDOW_END - WINDOW_START).days + 1
    short = {k: v["days"] for k, v in findings.items() if v["days"] != expected}
    tzs = sorted({v["tz"] for v in findings.values()})

    print(f"\n  regions: {len(frames)}/{len(REGIONS)}   rows: {len(allw):,}")
    print(f"  expected {expected} days each; regions not matching: {short or 'none'}")
    print(f"  distinct timezones resolved: {len(tzs)} -> {tzs}")
    if failed:
        print(f"  FAILED regions: {failed}")

    print("\n  >> ForEach iterates over REGIONS, not dates. One call per region.")
    print("  >> Join key is country_code + region_code + date. Both are clean")
    print("     codes on the order side, so no name conforming is needed.")
    print("  >> Brazil spans several timezones. 'auto' resolves each from its")
    print("     coordinates, and the resolved value is stored for the record.")
    return findings


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/samples")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Window : {WINDOW_START} to {WINDOW_END}")
    print(f"Samples: {out.resolve()}")

    fx = test_frankfurter(out)
    hol = test_nager(out)
    wx = test_openmeteo(out)

    banner("SUMMARY")
    ok = all(v.get("ok") for v in fx.values()) and bool(hol) and bool(wx)
    print(f"  Frankfurter : {'pass' if fx and all(v.get('ok') for v in fx.values()) else 'FAIL'}")
    print(f"  Nager.Date  : {'pass' if hol else 'FAIL'}")
    print(f"  Open-Meteo  : {'pass' if wx else 'FAIL'}")
    print(f"\n  Total API calls for a full load: "
          f"{2 + 4 + len(REGIONS)} "
          f"(2 FX + 4 holiday + {len(REGIONS)} weather)")
    print("  At roughly $1 per 1,000 ADF activity runs, the API ingestion")
    print("  side of this project costs approximately nothing.")

    (out / "_smoke_test_findings.json").write_text(
        json.dumps({"fx": fx, "holidays": hol, "weather": wx}, indent=2, default=str))
    print(f"\n  Findings written to {out / '_smoke_test_findings.json'}")
    print("  Commit data/samples/ — it is what makes the repo readable"
          " without an Azure subscription.")
    if not ok:
        print("\n  One or more endpoints failed. Paste the output above.")


if __name__ == "__main__":
    main()
