# Confluence — Post-Acquisition Marketplace Integration

> End-to-end data platform integrating two e-commerce marketplaces on different systems, into one warehouse with a single point-in-time financial truth.

**Stack:** Azure Data Factory · Azure Data Lake Storage Gen2 · Databricks (Delta Lake, Unity Catalog) · Azure SQL · Python · SQL · Power BI · MLflow

---

## The problem

Confluence Group runs a Brazilian marketplace and has acquired a South African one running on a different platform. Three things are broken:

1. **Revenue keeps moving.** Finance restates history at today's FX rate each month, so last quarter's USD number is different every time it's reported.
2. **Ops can't compare the two platforms.** Each defines "late delivery" differently.
3. **Commission is being double-paid.** Some merchants sell on both platforms under slightly different names.

**What this build delivers:** one conformed warehouse, one merchant master with full history, revenue fixed at the rate that applied on the day, and a model that flags orders likely to miss their promised delivery date.

---

## Architecture

<!-- docs/architecture.png -->

| Layer | Tool | Responsibility |
|---|---|---|
| Ingestion & orchestration | Azure Data Factory | Metadata-driven copy, API pulls, watermarks, scheduling, failure handling |
| Storage | ADLS Gen2 | Raw landing zone, partitioned by source and date |
| Transformation | Databricks + Delta Lake | Bronze → Silver → Gold, all SCD logic, data quality gates |
| Control plane | Azure SQL | Ingestion manifest, watermarks, audit log, quarantine |
| Serving | Databricks SQL | Gold views |
| Reporting | Power BI | Executive, ops, merchant and data-quality reporting |
| ML | Databricks + MLflow | Delivery SLA breach classifier |

---

## Data sources

**Real**

| Source | Volume | Link |
|---|---|---|
| Olist Brazilian E-Commerce | 99,441 orders · 112,650 order items · 3,095 sellers · Sept 2016–Oct 2018 | [Kaggle](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) |
| Frankfurter FX (ECB rates) | Daily BRL→USD and ZAR→USD | [frankfurter.dev](https://frankfurter.dev) |
| Nager.Date public holidays | BR and ZA | [date.nager.at](https://date.nager.at) |
| Open-Meteo historical archive | Daily precipitation and wind by city | [open-meteo.com](https://open-meteo.com) |

**Generated**

The South African platform is synthetic, produced by [`scripts/generate_za_platform.py`](scripts/generate_za_platform.py). It is derived from the real Olist seller and order data so that the cross-platform merchant overlap is genuine rather than invented. It deliberately contains:

- Different column names and `dd/mm/yyyy` date formatting
- ZAR amounts requiring FX conversion
- Schema drift: a `promo_code` column appearing partway through the period
- Late-arriving refunds landing days after the orders they reference
- Weekly merchant master snapshots in which tier, commission rate and status genuinely change

This is disclosed rather than hidden: no public dataset offers two marketplaces on incompatible systems with overlapping merchants, which is the exact problem the build exists to solve.

---

## What's implemented

**Ingestion**
- [ ] Metadata-driven pipeline: one parameterised pipeline serving all sources, driven by a control table
- [ ] Watermark-based incremental load with the ceiling frozen before the copy
- [ ] REST API ingestion with date-range chunking
- [ ] Schema drift handled without pipeline failure
- [ ] Idempotent reruns (identical row counts on repeat execution)

**Modelling**
- [ ] SCD Type 2 on merchants — commission rate resolved at time of sale
- [ ] SCD Type 1 on category translations
- [ ] SCD Type 0 on customer acquisition attributes
- [ ] Late-arriving facts (refunds)
- [ ] Late-arriving / inferred dimension members
- [ ] Fuzzy entity resolution across platforms with a crosswalk table
- [ ] FX date spine with forward fill across non-trading days

**Quality & governance**
- [ ] Data quality expectations with quarantine and reason codes
- [ ] Full audit log of every pipeline run
- [ ] Delta time travel: reproducing a prior period's reported numbers

**Analytics & ML**
- [ ] Star schema with conformed dimensions
- [ ] Delivery SLA breach classifier (base rate: 8.1% of delivered orders arrive late)
- [ ] Power BI report with RLS

---

## Results

<!-- Fill these in as you go. Numbers are what make this land. -->

| Finding | Value |
|---|---|
| Revenue difference: flat-rate vs point-in-time FX | _TBD_ |
| Duplicate merchants identified across platforms | _TBD_ |
| Commission over-payment surfaced | _TBD_ |
| SLA breach model — precision / recall | _TBD_ |
| Rows quarantined by data quality gates | _TBD_ |

---

## Screenshots

<!-- docs/screenshots/ — capture these BEFORE the Azure credits expire -->

| | |
|---|---|
| ADF pipeline canvas | _TBD_ |
| ADF monitor — successful runs | _TBD_ |
| Databricks job run | _TBD_ |
| SCD2 table before/after a merchant change | _TBD_ |
| Power BI report pages | _TBD_ |

Walkthrough video: _TBD_

---

## Run it yourself

```bash
git clone <repo-url>
cd confluence-marketplace-integration
pip install -r requirements.txt

# Download Olist from Kaggle into data/raw/
python scripts/generate_za_platform.py
```

Sample data lives in `data/samples/` so the notebooks can be read and understood without an Azure subscription.

---

## Repository structure

```
adf/            Pipeline, dataset, linked service and trigger JSON (synced from ADF)
databricks/     Bronze, silver, gold and ML notebooks
sql/            Control table DDL, gold views, analysis queries
scripts/        Data generation and API utilities
powerbi/        .pbix file and DAX measure documentation
data/samples/   Small representative samples for reproducibility
docs/           Architecture diagram, screenshots, schema mapping
```

---

## Author

Samuel Adebayo — [LinkedIn](#) · [Portfolio](#)
