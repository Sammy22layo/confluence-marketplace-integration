# Synthetic data — full disclosure

Part of this project's data is fabricated. This document says exactly which part, how it was made, what was deliberately planted in it, and what it does not prove.

It exists so that nobody has to work this out for themselves.

---

## Why fabricate anything

The project is about integrating two marketplaces after an acquisition. That requires two systems that disagree with each other — different column names, different date formats, different currencies, different definitions of "late", and the same merchant appearing on both under different names.

No public dataset offers that. Olist provides one excellent Brazilian marketplace. There is no matching second marketplace with overlapping sellers, and there never will be, because the overlap is the commercially sensitive part.

The options were to drop the acquisition scenario and build a conventional single-source warehouse, or to fabricate the second platform. The second was chosen because the integration problems — conforming schemas, resolving entities across systems, reconciling incompatible SLA definitions — are the problems worth demonstrating, and they cannot be demonstrated with one source.

---

## What is real and what is not

### Real, unmodified

| Data | Detail |
|---|---|
| Olist orders, order items, payments, reviews, products, geolocation | 99,441 orders across Sept 2016 – Oct 2018, exactly as published on Kaggle |
| Olist seller identifiers, cities and states | 3,095 sellers, real IDs, real locations |
| Olist customer identifiers and locations | 96,096 distinct customers across 4,119 cities |
| Delivery dates, actual and estimated | The source of the 8.1% late-delivery rate and the ML label |
| Exchange rates | European Central Bank daily reference rates via Frankfurter |
| Public holidays | Nager.Date, Brazil and South Africa, 2017–2018 |
| Historical weather | Open-Meteo archive, 24,084 daily observations across 36 regions |

### Generated

| Data | Detail |
|---|---|
| The entire South African platform | 35,000 orders, 44,375 order lines, 24,000 customers, 800 vendors |
| South African vendor master | 22 monthly snapshots |
| **Brazilian merchant commercial attributes** | Names, tiers, commission rates and status — 22 monthly snapshots |
| Refunds | 1,003 across 94 weekly drop files, South Africa only |

That third row deserves emphasis, because it is easy to miss. **The Brazilian merchant master is also generated.** Olist's seller table contains only an identifier, a zip prefix, a city and a state. It has no business name, no tier, no commission rate and no status history. Every one of those attributes was manufactured.

What remains real on the Brazilian side is the seller identifier and location, and the transactional data those sellers actually generated.

---

## How the generator works

[`scripts/generate_za_platform.py`](../scripts/generate_za_platform.py) reads the real Olist files and derives the synthetic platform from them, rather than inventing it from nothing.

**Merchant tiers are derived from real sales.** Each Brazilian seller's gross merchandise value is computed from the real order items table, and tiers are assigned by percentile rank. So a platinum merchant is genuinely one of the highest-selling sellers in the real data, and the commission rate attached to that tier is applied to real transactions. The tier is invented; the ranking behind it is not.

**The cross-platform overlap is keyed to real sellers.** 120 South African vendors — 15% of 800 — are marked as the same real business as a specific Olist seller. Their names are the Brazilian name put through one of five deliberate corruptions: a translated suffix, an abbreviated last word, stripped accents plus a corporate suffix, collapsed whitespace in uppercase, or a single-character deletion. Every pair is guaranteed to differ from its original.

**The merchant masters drift over time.** Walking month by month, roughly 2% of merchants change tier with the commission rate following, 0.4% relocate, and 0.6% switch between active and dormant. These changes are the entire reason the silver layer needs slowly changing dimensions — without them, an SCD Type 2 implementation would have nothing to track.

**Everything is seeded.** Two runs produce byte-identical output, verified by diffing two complete runs. That is what allows the pipeline to claim idempotency without hand-waving.

---

## What was planted deliberately

Each of these exists because a specific part of the build needs to solve it. None is random noise.

| Planted | Solved by |
|---|---|
| Semicolon delimiter on the vendor snapshots, everything else comma | A parameterised dataset taking the delimiter from the manifest |
| `dd/mm/yyyy` dates — `03/04/2017` is 3 April, not 4 March | Explicit date parsing, recorded in the manifest as `date_format` |
| A different order status vocabulary — `COMPLETE`, `DISPATCHED`, `CANCELLED` | A conformance mapping onto one canonical set |
| ZAR amounts | Point-in-time FX conversion against a forward-filled date spine |
| **Schema drift** — `promo_code` appears from October 2017 onward | Ingestion that survives a column appearing mid-stream |
| **Late-arriving facts** — refunds land 3–10 days after their orders, some crossing a month boundary | A separate fact applied by merge, without restating the original |
| **A different SLA basis** — ZA promises from dispatch, Olist estimates from purchase | Storing both promises and exposing the basis as a column, rather than inventing a common one |
| **Merchant name variants** across platforms | Fuzzy entity resolution with a crosswalk table |
| **Attributes that change monthly** | SCD Type 2, with commission resolved at the time of sale |

---

## The answer key

`data/generated/_answer_key/true_merchant_crosswalk.csv` lists the 120 vendor pairs that are genuinely the same business.

It is **not uploaded to the data lake** and **not referenced by any pipeline**. It exists solely to score the fuzzy matcher after the fact: how many true pairs were found, how many were missed, and how many false matches were produced.

Feeding it into the matching step would make the entity resolution meaningless. Holding it back is what makes the resulting precision and recall figures worth reporting.

---

## What this data does not prove

Stated plainly, because the alternative is letting a reader assume more than is warranted.

**The South African transactional data has no real-world validity.** Order values, volumes, timing, geography and refund rates are invented distributions chosen to look plausible. No conclusion about South African e-commerce, consumer behaviour or logistics can be drawn from it. Any analysis restricted to the ZA platform is analysis of a simulation.

**The Brazilian merchant commercial data is equally invented.** Commission rates of 9.5% to 18% are plausible marketplace figures but were not sourced from Olist or from any real marketplace. The revenue and commission totals in the final reports are arithmetically correct and commercially fictional.

**The merchant overlap rate is a choice, not a finding.** Fifteen percent was selected to make entity resolution non-trivial without making it trivial. Real cross-platform seller overlap after an acquisition could be one percent or fifty.

**The name corruptions are systematic.** Five transformation styles applied by a seeded random choice. Real name variation across systems is messier and less uniform, so a matcher scoring well here would not necessarily score well on production data.

### What it does demonstrate

The engineering problems are real regardless of the data's provenance. A semicolon delimiter behaves identically whether the file behind it is real or generated. A column appearing mid-stream breaks a pipeline the same way. A merchant whose commission rate changed in March genuinely requires point-in-time resolution to bill correctly in February.

The synthetic platform is a test harness, deliberately constructed to contain the problems a real integration would contain. The solutions built against it are the point; the data itself is scaffolding.

---

## Reproducing it

```bash
pip install -r requirements.txt
# Olist CSVs must be in data/raw/
python scripts/generate_za_platform.py
```

Output: 205 files, roughly 15 MB, in `data/generated/`.

| Folder | Files |
|---|---|
| `br_merchants/` | 22 monthly snapshots |
| `za_merchants/` | 22 monthly snapshots, semicolon-delimited |
| `za_orders/` | 22 monthly extracts |
| `za_order_lines/` | 22 monthly extracts |
| `za_customers/` | 22 monthly extracts |
| `za_refunds/` | 94 weekly drop files |
| `_answer_key/` | 1 file, held back from the lake |

Configuration sits at the top of the script: the seed, the date window, vendor and order counts, the overlap fraction, the month the schema drift begins, the refund rate, and the monthly churn rates. Changing the seed produces a different but structurally identical dataset.
