# SkyPoints Member Pipeline: Architecture (v2)

Target platform: **Snowflake** (`sql/`). `src/skypoints` is a Python reference implementation of
the same rules, used as the test oracle and for local demos. What changed from v1 and why is in
[V2_CHANGES.md](V2_CHANGES.md).

## 1. Goals

| Goal | How the design meets it |
|---|---|
| Scale to very large daily volumes | Bulk `COPY INTO`, set-based SQL only, incremental `MERGE`, per-country merges in parallel sessions |
| Memory-efficient | The edge converter and readers stream (generators); all heavy work happens in the warehouse |
| Fast reads | Country tables clustered on `member_key`; narrow `MEMBER_INDEX`; derived columns stored and refreshed incrementally |
| Messy, mixed sources | RAW is all text and never rejects; per-source formats live in config; bad rows are quarantined with reason codes |
| Safe to re-run | Every step is a `MERGE`, a replace by `load_id`, or an idempotent update |
| Explainable | History of changes, a run log, a reject table, and reconciliation checks after every load |

## 2. Flow

```
 Sources           Edge (Python)        Raw                Staging                    Targets
 IND.csv    ┐                                                                       ┌ MEMBER_IND
 USA.csv    ├─► edge.py ─► canonical ─► RAW_MEMBER_FILE ─► V_RAW_MEMBER_PARSED ─►   ├ MEMBER_USA   ─► MEMBER_ALL (view,
 AUS.xlsx   │   one CSV    CSV, PUT     (all VARCHAR)       TMP_PARSED               ├ MEMBER_AUS      generated)
 GLOBAL.txt ┘              + COPY                                │                  └ … (from config)
                                                                 ├─► validate ─► STG_REJECT ──► GATE (stops the batch)
                                                                 └─► STG_MEMBER ─► STG_APPLY (latest wins)
                                                                                       │
                              07a move-out (all countries) ─► 07b upsert (parallel) ─► 08 index + SCD2 history
                              09 snapshot soft-delete ─► 13 derived refresh ─► 12 reconcile ─► done

 redemptions.json ─► RAW_REDEMPTION_FEED (VARIANT) ─► FLATTEN + validate ─► REDEMPTION_FACT ─► join via member_key
```

Step order, with the SQL file for each, is enforced by `snowflake_runner.SnowflakeRunner.load` and
covered by offline tests that assert the order.

## 3. Key decisions

**Member identity is `source_system|member_id`.** The country files reuse IDs 1, 2, 3, so the bare
ID collides. Cross-source duplicates of the same person are *proposed* (`V_MEMBER_MATCH_CANDIDATES`)
for a human to confirm into `MEMBER_XREF`; nothing is merged automatically, because a wrong merge
is worse than a duplicate.

**Latest record wins.** Per member, order by newest feed date, then newest last flight date, then
last row, then file name (deterministic). A late or replayed older file never overwrites a newer
stored record. If the winner's country differs from `MEMBER_INDEX`, the member moves.

**Country moves are applied in two phases.** Phase 1 deletes moved members from their old country
table, for every country; phase 2 upserts into the new one, for every country, in parallel sessions.
All of phase 1 finishes before phase 2 starts, so a moved member is never in two tables at once.
`--serial` runs both phases in one transaction when strict atomicity matters more than speed.

**History (SCD2).** `MEMBER_HISTORY` records every version of country, tier, active flag and state
with `valid_from` / `valid_to`. Country moves are *derived* from consecutive history rows
(`V_MEMBER_MOVES`), so the move log can never disagree with the history.

**Missing members.** Feeds are `delta` by default (absence means nothing). For sources confirmed to
send full extracts (`--snapshot`), a member no longer listed is soft-deleted (`MISSING_FROM_FEED`,
with `missing_since`), never hard-deleted. A guard refuses to act on an empty file or one that lists
more than 20% fewer members than are active, because that pattern means a truncated file.

**Derived columns.** `age` and `stale_member` are stored and refreshed daily by touching only the
rows whose birthday or stale-flip date fell between the watermark and today. The refresh is proven
equal to a full recompute (730 consecutive days, leap-day births, missed-day gaps).

**One definition of the rules.** `config.py` defines countries, aliases, per-source date formats and
allowed values; the runner seeds `DIM_*` tables from it and generates the country tables and the
`MEMBER_ALL` view. Parsing exists once in SQL (`CLEAN_TXT`, `PARSE_SRC_DATE`, `V_RAW_MEMBER_PARSED`).
The SQL and Python are still two implementations; the golden files and parity query exist to catch
drift.

**Redemptions.** The fact stores the stable `member_key`, resolved at load time and re-resolved for
orphans on every later load. The member's *current* country is looked up when read, so a move never
leaves a stale country on a redemption. The partner feed carries no country, so it joins to the
`GLOBAL` id space unless the feed adds `source_system`.

**Quality gate.** Row errors go to `STG_REJECT`; warnings keep the row. If the error rate exceeds the
threshold (default 2%), the batch is stopped *before* anything is staged or merged.

**Reconciliation.** `MEMBER_INDEX` is a derived structure. After every load, `12_reconcile.sql`
proves the index, the country tables and the history agree; the load fails if they do not.

## 4. Performance notes

- Files of 100-250 MB compressed for `COPY`; the same canonical CSV is what Snowpipe would auto-ingest.
- Staging tables are `TRANSIENT` (no fail-safe cost) and are purged on a retention schedule.
- Incremental by `load_id`: no step rescans history.
- Country tables clustered on `member_key`; `REDEMPTION_FACT` on `txn_date`.
- The redemption read view probes each country table by clustered `member_key`. That is cheaper than a
  scan but still touches every country table. **Measure it on production-sized data**; if it
  matters, store `country_code` on the fact and refresh it from the index.
- X-SMALL warehouse, 60-second auto-suspend for the trial; size per load in production.

## 5. Data quality rules

| Rule | Severity | Catches |
|---|---|---|
| `MANDATORY` | ERROR | missing name, member id, enrollment date |
| `BAD_DATE` | ERROR for enrollment, else WARNING | unparseable dates, e.g. AUS `2021-13-13` |
| `DATE_ORDER` | ERROR for future enrollment, else WARNING | future dates, flight before enrollment |
| `AGE_RANGE` | WARNING | age outside 0-110 |
| `UNKNOWN_COUNTRY` | ERROR | country not in the registry |
| `COUNTRY_ALIAS` | WARNING | `AU` -> `AUS`, `PHIL` -> `PHL` |
| `ID_FORMAT` | WARNING | non-numeric id; id length differs from the file's most common length |
| `DUP_KEY` | WARNING | same member twice in a batch |
| `TIER_CODE`, `ACTIVE_FLAG`, `POST_CODE`, `FIELD_LENGTH` | WARNING | domain, post code and spec-length breaches |
| `ROW_SHAPE`, `HEADER_SPEC` | ERROR / WARNING | wrong field count; header missing spec columns |
| `JSON_PARSE`, `JSON_SHAPE`, `MILES`, `STATUS`, `UNKNOWN_SOURCE` | redemption rules | bad JSON, non-positive miles, unknown status |

Spec issues to raise with the source team: the spec marks Member Name (not Member ID) as the key;
detail rows have 10 fields where the spec lists 11 (Post Code); the staging header says `County`;
DOB loses its leading zero and its day/month order is ambiguous; USA dates lose a leading zero.

## 6. Repo map

```
docs/                    ARCHITECTURE.md, V2_CHANGES.md, SNOWFLAKE_RUNBOOK.md, SECURITY_AND_RESIDENCY.md
sql/00-02                setup, raw/staging DDL, target DDL
sql/03*                  parsing functions and view, parse-load
sql/04-07b               validate, stage, latest-wins, country move-out and upsert
sql/08-13                index + history, snapshot, redemptions, identity, reconcile, derived refresh
sql/14*, 16              security, retention
sql/15*                  parity tables and comparison
src/skypoints/           config, readers, edge, stage, validate, transform, history, derived,
                         identity, redemptions, pipeline, reference, golden, sqlrender, snowflake_runner
tests/                   pytest suite; tests/golden holds the Python-produced expected output
```
