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

# Only the cities that carry real order volume. One call per city, not per order.
CITIES = [
    ("sao paulo",      "BR", -23.5505, -46.6333, "America/Sao_Paulo"),
    ("rio de janeiro", "BR", -22.9068, -43.1729, "America/Sao_Paulo"),
    ("belo horizonte", "BR", -19.9167, -43.9345, "America/Sao_Paulo"),
    ("curitiba",       "BR", -25.4284, -49.2733, "America/Sao_Paulo"),
    ("porto alegre",   "BR", -30.0346, -51.2177, "America/Sao_Paulo"),
    ("Johannesburg",   "ZA", -26.2041,  28.0473, "Africa/Johannesburg"),
    ("Cape Town",      "ZA", -33.9249,  18.4241, "Africa/Johannesburg"),
    ("Durban",         "ZA", -29.8587,  31.0218, "Africa/Johannesburg"),
    ("Pretoria",       "ZA", -25.7479,  28.2293, "Africa/Johannesburg"),
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
    banner("3. OPEN-METEO  (historical weather archive)")
    frames, findings = [], {}

    for name, cc, lat, lon, tz in CITIES:
        print(f"\n{name} ({cc}):")
        data, _ = call(OPENMETEO, {
            "latitude": lat, "longitude": lon,
            "start_date": WINDOW_START.isoformat(),
            "end_date": WINDOW_END.isoformat(),
            "daily": "precipitation_sum,wind_speed_10m_max,temperature_2m_max",
            "timezone": tz,
        })
        if not data:
            print("    ! if this 400s on wind_speed_10m_max, the older name")
            print("      windspeed_10m_max may be required. Check the reason.")
            continue

        daily = data.get("daily", {})
        df = pd.DataFrame(daily)
        nulls = df.isna().sum().to_dict()
        print(f"    days: {len(df):,}  |  units: {data.get('daily_units')}")
        print(f"    nulls: {nulls}")
        df.insert(0, "city", name)
        df.insert(1, "country_code", cc)
        frames.append(df)
        findings[name] = {"days": len(df), "nulls": nulls}

    if frames:
        allw = pd.concat(frames, ignore_index=True)
        allw.to_csv(out / "openmeteo_daily.csv", index=False)
        print(f"\n  total rows: {len(allw):,} across {len(frames)} cities")
        print("\n  >> One call per city covers the whole window, so the ADF")
        print("     ForEach iterates over CITIES, not over dates.")
        print("  >> Grain is city + date. Orders join on shipping city, which")
        print("     means the city name has to be conformed first.")
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
          f"{2 + 4 + len(CITIES)} "
          f"(2 FX + 4 holiday + {len(CITIES)} weather)")
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
