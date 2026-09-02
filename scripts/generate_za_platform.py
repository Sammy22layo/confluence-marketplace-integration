"""
generate_za_platform.py
=======================

Generates the synthetic South African marketplace for Project Confluence,
plus the merchant master snapshots for both platforms.

The South African platform is fabricated, but it is derived from the real
Olist seller table so that the cross-platform merchant overlap is genuine.
Roughly 15% of ZA vendors are the same real business as a Brazilian seller,
carrying a deliberately different spelling of the name. That is what the
fuzzy entity resolution step in the silver layer has to find.

Deliberate imperfections baked in, because each one exists to be solved
downstream:

  * Different column names and dd/mm/yyyy date formatting
  * ZAR amounts requiring point-in-time FX conversion
  * A different order status vocabulary
  * A different SLA basis: ZA promises delivery from dispatch, Olist
    estimates from purchase. Unioning them naively is wrong.
  * Schema drift: promo_code appears from October 2017 onward
  * Late-arriving refunds, dropped 3-10 days after the order they reference
  * Monthly merchant snapshots in which tier, commission and status change

Everything is seeded, so two runs produce byte-identical output. That is
what lets the pipeline claim idempotency honestly.

Usage
-----
    python scripts/generate_za_platform.py
    python scripts/generate_za_platform.py --raw-dir data/raw --out-dir data/generated

Requires: pandas, numpy
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SEED = 20260829

WINDOW_START = datetime(2017, 1, 1)
WINDOW_END = datetime(2018, 10, 31)

N_ZA_VENDORS = 800           # total vendors on the ZA platform
OVERLAP_FRACTION = 0.15      # share of ZA vendors that are also BR sellers
N_ZA_ORDERS = 35_000         # ZA order volume across the whole window
N_ZA_CUSTOMERS = 24_000

PROMO_CODE_FROM = datetime(2017, 10, 1)   # schema drift begins here
REFUND_RATE = 0.031                       # share of ZA orders later refunded

TIERS = ["bronze", "silver", "gold", "platinum"]
COMMISSION_BY_TIER = {"bronze": 0.18, "silver": 0.15, "gold": 0.12, "platinum": 0.095}
MONTHLY_TIER_CHURN = 0.02        # share of merchants changing tier each month
MONTHLY_CITY_CHURN = 0.004       # share relocating
MONTHLY_STATUS_CHURN = 0.006     # share going dormant or reactivating

ZA_CITIES = [
    ("Johannesburg", "Gauteng", 2000), ("Cape Town", "Western Cape", 8000),
    ("Durban", "KwaZulu-Natal", 4000), ("Pretoria", "Gauteng", 1),
    ("Port Elizabeth", "Eastern Cape", 6000), ("Bloemfontein", "Free State", 9300),
    ("East London", "Eastern Cape", 5200), ("Polokwane", "Limpopo", 700),
    ("Nelspruit", "Mpumalanga", 1200), ("Kimberley", "Northern Cape", 8300),
    ("Rustenburg", "North West", 300), ("Stellenbosch", "Western Cape", 7600),
    ("Sandton", "Gauteng", 2196), ("Centurion", "Gauteng", 157),
    ("Soweto", "Gauteng", 1804), ("Pietermaritzburg", "KwaZulu-Natal", 3201),
]

BR_NAME_HEADS = [
    "Casa", "Loja", "Comercial", "Grupo", "Distribuidora", "Empório", "Ponto",
    "Mundo", "Espaço", "Central", "Arte", "Estilo", "Bella", "Nova", "Prime",
]
BR_NAME_STEMS = [
    "Verde", "Solar", "Aurora", "Atlântico", "Ipanema", "Serra", "Pampa",
    "Coral", "Vitória", "Sabiá", "Cristal", "Horizonte", "Marfim", "Onda",
    "Palmeira", "Rubi", "Sertão", "Tucano", "Âmbar", "Lírio", "Cedro",
    "Girassol", "Jacarandá", "Mangue", "Orquídea", "Pitanga", "Quartzo",
]
BR_NAME_TAILS = [
    "Comércio", "Distribuidora", "Importadora", "Store", "Shop", "Ltda",
    "Varejo", "Atacado", "Express", "Brasil",
]

ZA_NAME_HEADS = [
    "Kalahari", "Table", "Protea", "Umhlanga", "Drakensberg", "Highveld",
    "Karoo", "Boland", "Zulu", "Cape", "Vaal", "Tugela", "Amber", "Silver",
]
ZA_NAME_STEMS = [
    "Ridge", "Bay", "Peak", "Grove", "Trading", "Hollow", "Point", "Springs",
    "Fields", "Crest", "Wharf", "Junction", "Vale", "Reef",
]
ZA_NAME_TAILS = [
    "Trading", "(Pty) Ltd", "Wholesale", "Supplies", "Retail", "Distribution",
    "Group", "Enterprises", "CC",
]

ZA_STATUSES = ["COMPLETE", "DISPATCHED", "CANCELLED", "PENDING", "RETURNED"]
ZA_STATUS_WEIGHTS = [0.918, 0.026, 0.031, 0.012, 0.013]

PROMO_CODES = ["WELCOME10", "FREESHIP", "WINTER15", "BULK20", "LOYAL5", "FLASH25"]
ACQ_CHANNELS = ["organic", "paid_search", "social", "referral", "email", "affiliate"]
REFUND_REASONS = ["DAMAGED", "NOT_AS_DESCRIBED", "LATE_DELIVERY", "CHANGED_MIND", "DUPLICATE"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def stable_seed(key: str) -> int:
    """Deterministic per-entity seed. Python's hash() is salted per process,
    so it cannot be used where reproducibility matters."""
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16)


def month_starts(start: datetime, end: datetime) -> list[datetime]:
    out, cur = [], datetime(start.year, start.month, 1)
    while cur <= end:
        out.append(cur)
        cur = datetime(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return out


def br_merchant_name(seller_id: str) -> str:
    r = np.random.RandomState(stable_seed(seller_id))
    shape = r.randint(0, 3)
    head = BR_NAME_HEADS[r.randint(len(BR_NAME_HEADS))]
    stem = BR_NAME_STEMS[r.randint(len(BR_NAME_STEMS))]
    tail = BR_NAME_TAILS[r.randint(len(BR_NAME_TAILS))]
    if shape == 0:
        return f"{head} {stem}"
    if shape == 1:
        return f"{stem} {tail}"
    return f"{head} {stem} {tail}"


def za_variant_of(name: str, seller_id: str) -> str:
    """Same business, as the ZA platform recorded it. This is what the fuzzy
    matcher has to see through."""
    r = np.random.RandomState(stable_seed(seller_id + "za"))
    n = name
    swaps = {
        "Comércio": "Commerce", "Distribuidora": "Distribution",
        "Importadora": "Imports", "Varejo": "Retail", "Atacado": "Wholesale",
        "Ltda": "(Pty) Ltd", "Loja": "Store", "Casa": "House",
    }
    order = list(r.permutation(5))
    for style in order:
        if style == 0:                              # translate the suffix
            for k, v in swaps.items():
                n = n.replace(k, v)
        elif style == 1:                            # abbreviate the last word
            parts = n.split()
            if len(parts) > 1:
                parts[-1] = parts[-1][:4].rstrip(".") + "."
            n = " ".join(parts)
        elif style == 2:                            # strip accents, add entity
            n = (n.replace("é", "e").replace("â", "a").replace("ã", "a")
                  .replace("í", "i").replace("ó", "o").replace("ç", "c")) + " (Pty) Ltd"
        elif style == 3:                            # collapse whitespace, upper
            n = n.replace(" ", "").upper()
        else:                                       # single-character typo
            i = r.randint(1, max(2, len(n) - 1))
            n = n[:i] + n[i + 1:]
        if n != name:                               # a variant must differ
            break
    return n


def za_native_name(vendor_code: str) -> str:
    r = np.random.RandomState(stable_seed(vendor_code))
    head = ZA_NAME_HEADS[r.randint(len(ZA_NAME_HEADS))]
    stem = ZA_NAME_STEMS[r.randint(len(ZA_NAME_STEMS))]
    tail = ZA_NAME_TAILS[r.randint(len(ZA_NAME_TAILS))]
    return f"{head} {stem} {tail}" if r.rand() < 0.6 else f"{head} {stem}"


def fmt_za_datetime(ts) -> str | None:
    """ZA platform writes dd/mm/yyyy HH:MM. Ambiguous by design: 03/04/2017
    is 3 April, and anything parsing it as 4 March will be wrong."""
    return None if pd.isna(ts) else pd.Timestamp(ts).strftime("%d/%m/%Y %H:%M")


def fmt_za_date(ts) -> str | None:
    return None if pd.isna(ts) else pd.Timestamp(ts).strftime("%d/%m/%Y")


# --------------------------------------------------------------------------
# Merchant masters
# --------------------------------------------------------------------------

def build_br_base(sellers: pd.DataFrame, items: pd.DataFrame) -> pd.DataFrame:
    """Attach manufactured commercial attributes to the real seller list.
    Tier is derived from real GMV, so it is not arbitrary."""
    gmv = items.groupby("seller_id")["price"].sum().rename("gmv")
    df = sellers.merge(gmv, left_on="seller_id", right_index=True, how="left")
    df["gmv"] = df["gmv"].fillna(0.0)

    df["merchant_name"] = df["seller_id"].map(br_merchant_name)
    ranks = df["gmv"].rank(pct=True)
    df["tier"] = pd.cut(
        ranks, bins=[-0.01, 0.55, 0.85, 0.97, 1.0], labels=TIERS
    ).astype(str)
    df["commission_rate"] = df["tier"].map(COMMISSION_BY_TIER)
    df["status"] = "active"
    return df[["seller_id", "merchant_name", "tier", "commission_rate",
               "seller_city", "seller_state", "status"]].reset_index(drop=True)


def build_za_base(br_base: pd.DataFrame, rng: np.random.RandomState) -> pd.DataFrame:
    """ZA vendor list. Some are the same real business as a BR seller,
    the rest are ZA-only."""
    n_overlap = int(N_ZA_VENDORS * OVERLAP_FRACTION)
    overlap_ids = rng.choice(br_base["seller_id"].values, size=n_overlap, replace=False)
    br_lookup = br_base.set_index("seller_id")

    rows = []
    for i, sid in enumerate(overlap_ids):
        city, prov, pc = ZA_CITIES[rng.randint(len(ZA_CITIES))]
        rows.append({
            "vendor_code": f"ZAV{i + 1:05d}",
            "vendor_name": za_variant_of(br_lookup.at[sid, "merchant_name"], sid),
            "vendor_grade": br_lookup.at[sid, "tier"].upper(),
            "town": city, "province": prov, "postcode": pc,
            "_true_br_seller_id": sid,      # answer key, dropped before writing
        })
    for i in range(n_overlap, N_ZA_VENDORS):
        code = f"ZAV{i + 1:05d}"
        city, prov, pc = ZA_CITIES[rng.randint(len(ZA_CITIES))]
        rows.append({
            "vendor_code": code,
            "vendor_name": za_native_name(code),
            "vendor_grade": TIERS[rng.choice(4, p=[0.5, 0.3, 0.15, 0.05])].upper(),
            "town": city, "province": prov, "postcode": pc,
            "_true_br_seller_id": None,
        })

    df = pd.DataFrame(rows)
    df["comm_pct"] = df["vendor_grade"].str.lower().map(COMMISSION_BY_TIER) * 100
    df["active_flag"] = "Y"
    return df


def drift_snapshots(base: pd.DataFrame, months: list[datetime],
                    rng: np.random.RandomState, *, za: bool) -> dict:
    """Walk the master forward month by month, mutating a small share of rows.
    These changes are the entire reason SCD2 exists in the silver layer."""
    tier_col = "vendor_grade" if za else "tier"
    comm_col = "comm_pct" if za else "commission_rate"
    city_col = "town" if za else "seller_city"
    status_col = "active_flag" if za else "status"
    cities = [c[0] for c in ZA_CITIES] if za else base[city_col].dropna().unique().tolist()

    cur = base.copy()
    out = {}
    for m in months:
        n = len(cur)
        # tier moves, commission follows
        idx = rng.choice(n, size=int(n * MONTHLY_TIER_CHURN), replace=False)
        for i in idx:
            pos = TIERS.index(str(cur.iat[i, cur.columns.get_loc(tier_col)]).lower())
            new = TIERS[min(3, max(0, pos + (1 if rng.rand() < 0.7 else -1)))]
            cur.iat[i, cur.columns.get_loc(tier_col)] = new.upper() if za else new
            rate = COMMISSION_BY_TIER[new]
            cur.iat[i, cur.columns.get_loc(comm_col)] = rate * 100 if za else rate
        # relocations
        idx = rng.choice(n, size=max(1, int(n * MONTHLY_CITY_CHURN)), replace=False)
        for i in idx:
            cur.iat[i, cur.columns.get_loc(city_col)] = cities[rng.randint(len(cities))]
        # dormancy and reactivation
        idx = rng.choice(n, size=max(1, int(n * MONTHLY_STATUS_CHURN)), replace=False)
        on, off = ("Y", "N") if za else ("active", "inactive")
        for i in idx:
            j = cur.columns.get_loc(status_col)
            cur.iat[i, j] = off if cur.iat[i, j] == on else on

        snap = cur.copy()
        snap.insert(0, "snapshot_date", m.strftime("%d/%m/%Y") if za else m.strftime("%Y-%m-%d"))
        out[m] = snap
    return out


# --------------------------------------------------------------------------
# ZA transactions
# --------------------------------------------------------------------------

def build_za_customers(rng: np.random.RandomState) -> pd.DataFrame:
    span = (WINDOW_END - WINDOW_START).days
    # signups skew later as the platform grows
    offsets = (rng.beta(1.6, 1.0, N_ZA_CUSTOMERS) * span).astype(int)
    rows = []
    for i in range(N_ZA_CUSTOMERS):
        city, prov, pc = ZA_CITIES[rng.randint(len(ZA_CITIES))]
        rows.append({
            "buyer_ref": f"ZAC{i + 1:07d}",
            "buyer_uid": hashlib.md5(f"buyer{i}".encode()).hexdigest()[:16],
            "postcode": pc + rng.randint(0, 60),
            "town": city,
            "province": prov,
            "signup_dt": fmt_za_date(WINDOW_START + timedelta(days=int(offsets[i]))),
            "acq_channel": ACQ_CHANNELS[rng.choice(6, p=[.30, .22, .18, .12, .11, .07])],
        })
    return pd.DataFrame(rows)


def build_za_orders(vendors: pd.DataFrame, customers: pd.DataFrame,
                    rng: np.random.RandomState) -> tuple[pd.DataFrame, pd.DataFrame]:
    span = (WINDOW_END - WINDOW_START).days
    # order volume ramps over the window, with a Q4 lift
    offsets = np.sort((rng.beta(1.8, 1.1, N_ZA_ORDERS) * span).astype(int))
    vendor_codes = vendors["vendor_code"].values
    buyer_refs = customers["buyer_ref"].values

    orders, lines = [], []
    for i, off in enumerate(offsets):
        purchase = (WINDOW_START + timedelta(days=int(off))
                    + timedelta(hours=int(rng.randint(6, 23)),
                                minutes=int(rng.randint(0, 60))))
        status = ZA_STATUSES[rng.choice(5, p=ZA_STATUS_WEIGHTS)]
        ord_ref = f"ZA-{i + 1:08d}"

        dispatch = received = promised = None
        if status in ("COMPLETE", "DISPATCHED", "RETURNED"):
            dispatch = purchase + timedelta(days=float(rng.gamma(2.0, 1.1)))
            # ZA promises delivery from DISPATCH, not from purchase.
            # Olist estimates from purchase. Conforming these is the ops problem.
            promised = dispatch + timedelta(days=int(rng.choice([3, 4, 5, 7, 10],
                                                               p=[.2, .3, .25, .18, .07])))
            if status in ("COMPLETE", "RETURNED"):
                transit = float(rng.gamma(2.4, 1.9))
                received = dispatch + timedelta(days=transit)

        row = {
            "ord_ref": ord_ref,
            "buyer_ref": buyer_refs[rng.randint(len(buyer_refs))],
            "ord_dt": fmt_za_datetime(purchase),
            "ord_state": status,
            "dispatch_dt": fmt_za_datetime(dispatch),
            "received_dt": fmt_za_datetime(received),
            "promised_dt": fmt_za_date(promised),
        }
        if purchase >= PROMO_CODE_FROM:
            row["promo_code"] = (PROMO_CODES[rng.randint(len(PROMO_CODES))]
                                 if rng.rand() < 0.22 else None)
        orders.append(row)

        for ln in range(1 + int(rng.choice([0, 1, 2], p=[.78, .17, .05]))):
            unit = float(np.round(np.exp(rng.normal(6.1, 0.85)), 2))
            lines.append({
                "ord_ref": ord_ref,
                "line_no": ln + 1,
                "sku": f"SKU{rng.randint(10000, 99999)}",
                "vendor_code": vendor_codes[rng.randint(len(vendor_codes))],
                "qty": int(rng.choice([1, 2, 3], p=[.85, .12, .03])),
                "unit_price_zar": unit,
                "delivery_fee_zar": float(np.round(rng.uniform(35, 180), 2)),
            })

    return pd.DataFrame(orders), pd.DataFrame(lines)


def build_za_refunds(orders: pd.DataFrame, lines: pd.DataFrame,
                     rng: np.random.RandomState) -> pd.DataFrame:
    """Refunds land 3-10 days after the order. Some cross a month boundary,
    which is what makes them late-arriving facts rather than just extra rows."""
    value = (lines.assign(v=lines.unit_price_zar * lines.qty)
                  .groupby("ord_ref")["v"].sum())
    eligible = orders[orders.ord_state.isin(["COMPLETE", "RETURNED"])]
    picked = eligible.sample(frac=REFUND_RATE, random_state=SEED)

    rows = []
    for k, (_, o) in enumerate(picked.iterrows()):
        ordered = datetime.strptime(o.ord_dt, "%d/%m/%Y %H:%M")
        refunded = ordered + timedelta(days=int(rng.randint(3, 11)))
        if refunded > WINDOW_END:
            continue
        full = rng.rand() < 0.6
        amt = float(value.get(o.ord_ref, 0.0))
        rows.append({
            "refund_ref": f"RF-{k + 1:07d}",
            "ord_ref": o.ord_ref,
            "refund_dt": fmt_za_date(refunded),
            "refund_amt_zar": round(amt if full else amt * float(rng.uniform(0.2, 0.8)), 2),
            "reason_code": REFUND_REASONS[rng.choice(5, p=[.22, .19, .26, .24, .09])],
            "_drop_date": refunded,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="data/raw")
    ap.add_argument("--out-dir", default="data/generated")
    args = ap.parse_args()

    raw, out = Path(args.raw_dir), Path(args.out_dir)
    rng = np.random.RandomState(SEED)

    print(f"Reading Olist from {raw.resolve()}")
    sellers = pd.read_csv(raw / "olist_sellers_dataset.csv")
    items = pd.read_csv(raw / "olist_order_items_dataset.csv")

    months = month_starts(WINDOW_START, WINDOW_END)
    print(f"Window: {WINDOW_START:%Y-%m-%d} to {WINDOW_END:%Y-%m-%d} ({len(months)} months)")

    # -- merchant masters ---------------------------------------------------
    br_base = build_br_base(sellers, items)
    za_base = build_za_base(br_base, rng)

    crosswalk = za_base.loc[za_base._true_br_seller_id.notna(),
                            ["vendor_code", "_true_br_seller_id", "vendor_name"]]
    crosswalk = crosswalk.rename(columns={"_true_br_seller_id": "seller_id"})
    crosswalk = crosswalk.merge(br_base[["seller_id", "merchant_name"]], on="seller_id")

    za_base_public = za_base.drop(columns=["_true_br_seller_id"])

    br_snaps = drift_snapshots(br_base, months, rng, za=False)
    za_snaps = drift_snapshots(za_base_public, months, rng, za=True)

    d = out / "br_merchants"; d.mkdir(parents=True, exist_ok=True)
    for m, snap in br_snaps.items():
        snap.to_csv(d / f"br_merchants_{m:%Y-%m-%d}.csv", index=False)
    d = out / "za_merchants"; d.mkdir(parents=True, exist_ok=True)
    for m, snap in za_snaps.items():
        snap.to_csv(d / f"za_vendors_{m:%Y-%m-%d}.csv", index=False, sep=";")
    print(f"  merchant snapshots: {len(br_snaps)} BR + {len(za_snaps)} ZA")

    # -- customers ----------------------------------------------------------
    customers = build_za_customers(rng)
    d = out / "za_customers"; d.mkdir(parents=True, exist_ok=True)
    customers["_m"] = pd.to_datetime(customers.signup_dt, format="%d/%m/%Y").dt.to_period("M")
    for period, grp in customers.groupby("_m"):
        grp.drop(columns="_m").to_csv(
            d / f"za_customers_{period.strftime('%Y%m')}.csv", index=False)
    print(f"  customers: {len(customers):,} across {customers._m.nunique()} monthly files")

    # -- orders and lines ---------------------------------------------------
    orders, lines = build_za_orders(za_base_public, customers, rng)
    orders["_m"] = pd.to_datetime(orders.ord_dt, format="%d/%m/%Y %H:%M").dt.to_period("M")

    d_o = out / "za_orders"; d_o.mkdir(parents=True, exist_ok=True)
    d_l = out / "za_order_lines"; d_l.mkdir(parents=True, exist_ok=True)
    for period, grp in orders.groupby("_m"):
        g = grp.drop(columns="_m")
        # schema drift: the column only exists in files from Oct 2017 onward
        if period.to_timestamp() < PROMO_CODE_FROM and "promo_code" in g.columns:
            g = g.drop(columns=["promo_code"])
        g.to_csv(d_o / f"za_orders_{period.strftime('%Y%m')}.csv", index=False)
        lines[lines.ord_ref.isin(g.ord_ref)].to_csv(
            d_l / f"za_order_lines_{period.strftime('%Y%m')}.csv", index=False)
    print(f"  orders: {len(orders):,} | lines: {len(lines):,}")

    # -- refunds ------------------------------------------------------------
    refunds = build_za_refunds(orders, lines, rng)
    d = out / "za_refunds"; d.mkdir(parents=True, exist_ok=True)
    refunds["_w"] = pd.to_datetime(refunds._drop_date).dt.to_period("W")
    for period, grp in refunds.groupby("_w"):
        stamp = period.start_time.strftime("%Y%m%d")
        grp.drop(columns=["_w", "_drop_date"]).to_csv(
            d / f"za_refunds_{stamp}.csv", index=False)
    print(f"  refunds: {len(refunds):,} across {refunds._w.nunique()} drop files")

    # -- answer key ---------------------------------------------------------
    d = out / "_answer_key"; d.mkdir(parents=True, exist_ok=True)
    crosswalk.to_csv(d / "true_merchant_crosswalk.csv", index=False)
    print(f"  answer key: {len(crosswalk)} true cross-platform matches")
    print("\nDo not feed the answer key to the entity resolution step. "
          "Use it only to score how well the fuzzy matcher did.")


if __name__ == "__main__":
    main()
