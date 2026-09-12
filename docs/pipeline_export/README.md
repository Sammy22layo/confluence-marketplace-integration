# Pipeline exports

Snapshots of the Azure SQL control plane and audit log, taken while the ingestion layer was live. Exported because the Azure resources are time-limited: once the subscription credits expire the control database goes with them, and these files are the only durable record that the pipelines actually ran.

Exported 12 September 2026, covering 6–12 September.

---

## Files

| File | Rows | What it is |
|---|---|---|
| `ingestion_source.csv` | 18 | The manifest. One row per source, holding everything the pipeline needs to load it |
| `api_call.csv` | 42 | The API call registry. One row per HTTP request, with its fully-formed URL |
| `pipeline_run_log.csv` | 314 | The audit log. Every run of both pipelines across seven days |
| `run_summary.csv` | 3 | Aggregate counts by source type and status |

**Totals:** 301 successful runs, 13 failures, 0 orphans, 2,562 files moved.

---

## `ingestion_source.csv`

The table that makes one pipeline serve fifteen file sources. Every column that varies between sources lives here rather than on the ADF canvas: folder, file pattern, delimiter, date format, landing path, load type, expected file count.

Worth looking at `column_delimiter` and `date_format`. The South African vendor snapshot is semicolon-delimited with day-first dates while everything else is comma-delimited — handled as configuration, not as a second pipeline.

`load_type` drives a branch at runtime:

| Value | Sources | Behaviour |
|---|---|---|
| `FULL` | 9 Olist extracts | Every file, every run |
| `SNAPSHOT` | 2 merchant masters | All monthly snapshots, every run |
| `INCREMENTAL` | 4 ZA extracts | Only files modified since the last successful run |

Three rows have `source_type = 'rest'`. Those are the API sources, expanded into individual calls in the next file.

---

## `api_call.csv`

Forty-two rows, one per HTTP request: two exchange-rate series, four holiday calendars, thirty-six regional weather histories.

`relative_url` holds the complete path and query string. Any row can be verified by pasting `base_url + relative_url` into a browser, which is the point — the URLs are readable and testable, rather than assembled inside ADF expressions where neither is possible.

The flat structure exists because Azure Data Factory does not permit a ForEach inside a ForEach. Storing each call as its own row collapses what would have been a parent and child pipeline into one lookup and one loop.

---

## `pipeline_run_log.csv`

The audit trail. Every source load opens a row before the copy starts and closes it afterwards, whatever the outcome.

| Column | Meaning |
|---|---|
| `window_start` | Watermark floor, recorded for every source |
| `window_end` | Load ceiling, **frozen before the copy begins** |
| `files_read` | Files the copy actually moved |
| `status` | `SUCCEEDED` or `FAILED` |
| `error_message` | Populated on failure with the reason |

### The final run — 12 September, 17:31 to 17:42

Fifteen sources, all green, and the clearest single piece of evidence in the file:

| Load type | Sources | Files read | Why |
|---|---|---|---|
| `FULL` | 9 Olist extracts | 1 each | Full loads ignore the watermark entirely |
| `SNAPSHOT` | 2 merchant masters | 22 each | Snapshots copy every monthly file on every run |
| `INCREMENTAL` | 4 ZA extracts | 0 each | No new files had arrived since the previous run |

Note that the snapshot rows carry a populated `window_start` and still read all 22 files. The floor is recorded for every source, but the copy activity applies it only when the load type is incremental. Recording state and acting on it are separate concerns.

An earlier run on the same day shows `za_orders` reading exactly 1 after a single new file was added to its folder, while the other three incremental sources still read 0. Together those two runs demonstrate both halves of the incremental logic: correctly skipping what has not changed, and correctly catching what has.

### The 13 failures

Kept deliberately. A log that has only ever been green looks staged, and these fall into two honest categories.

**Three intentional tests**, run to prove the failure paths work rather than merely exist:

| Date | Source | Error | What it proves |
|---|---|---|---|
| 06 Sep | `olist_products` | ADLS `PathNotFound` with the full path | The copy-failure handler captures the underlying error |
| 06 Sep | `olist_sellers` | "expected at least 999 files, got 1" | The quality gate rejects a wrong count |
| 06 Sep | `olist_products` | ADLS `PathNotFound` | Repeat, confirming the handler is not incidental |

**Ten from two build-time regressions**, each fixed within the hour:

| Date | Sources | Cause |
|---|---|---|
| 12 Sep, 08:33 | 4 incremental | The quality gate had not yet been widened to treat zero new files as valid for an incremental load |
| 12 Sep, 09:35 | 2 snapshots | The last-modified filter was briefly applied to every load type rather than only to incremental loads |
| 12 Sep, 16:33 | 4 incremental | The widened gate expression reverted in the ADF editor and had to be re-entered |

That last one is worth its own note. Expressions in the ADF editor silently fail to persist if typed directly into a field rather than entered through Add dynamic content. It happened four times during this build. The failure mode is invisible — the field simply appears empty or reverts to an earlier value, and the pipeline runs with the previous definition.

The timestamps show each regression being identified and corrected in minutes rather than lingering.

---

## `run_summary.csv`

| Source type | Status | Runs | Files |
|---|---|---|---|
| file | SUCCEEDED | 205 | 2,562 |
| file | FAILED | 13 | — |
| rest | SUCCEEDED | 96 | — |

File counts are null for REST sources: a binary copy reports bytes and files, and there is exactly one file per API call by definition, so the count carries no information.

Row counts are null throughout. Source and sink share the same format with no column mapping, so ADF performs a byte-level passthrough and never parses the files. It genuinely does not know how many rows it moved. Row counts are established in the bronze layer, where something reads the records.
