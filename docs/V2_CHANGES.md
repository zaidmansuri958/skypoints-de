# v1.0 -> v2: what was wrong, what changed, what is still open

v1.0 (tag `v1.0`, 67 tests) met the assessment brief. A design review of it found 11 weaknesses.
Each was fixed in its own commit so the history reads as a review trail:
`git log --oneline v1.0..HEAD`.

Status key: **Fixed** = behaviour is implemented and tested. **Mitigated** = improved, with a
known remaining limit. **Open** = needs something outside this repo.

| # | Weakness in v1.0 | Status | Commit | How it is verified |
|---|---|---|---|---|
| 1 | No history; a country move left no persisted trace | **Fixed** | `66b8db0` | 9 tests: SCD2 versions, point-in-time lookup, idempotent replay, moves derived from history |
| 2 | Members missing from a feed stayed ACTIVE forever | **Fixed** | `df087b9` | Soft-delete only for confirmed snapshot feeds; guard blocks empty / truncated files; invalid rows still count as "seen" |
| 3 | Same person in two sources = two members | **Mitigated** | `d266544` | Candidates are proposed for human confirmation; they are not merged. The two member records still exist until confirmed |
| 4 | `age` / `stale_member` went stale daily; a second "live" view duplicated them | **Fixed** | `df087b9` | Incremental refresh equals a full recompute on every day for 2 years, incl. leap-day births and missed-day gaps. The duplicate view was removed |
| 5 | Per-country sprawl; hard-coded view; the doc claimed parallel merges the code did not do | **Fixed** | `6ad28f0`, `0de3438` | Countries come from `config.py`; view and tables are generated; merges really run in parallel sessions in two phases (test asserts all deletes precede all upserts) |
| 6 | `MEMBER_INDEX` could silently disagree with the tables | **Fixed** | `960b7a6` | `reconcile()` / `12_reconcile.sql` after every load; 7 tests inject each kind of inconsistency |
| 7 | Redemption join through a view over all countries; stale-country risk | **Mitigated** | `960b7a6` | Fact stores `member_key`, orphans re-linked each load, country looked up live. The read view still probes every country table (cheaper than a scan, not free): measure on real volume |
| 8 | Rules existed twice (SQL and Python), only Python tested | **Mitigated** | `9968e44`, `6ad28f0` | One config feeds both; parsing defined once in SQL; golden files + parity query diff Snowflake against Python. Still two implementations |
| 9 | Ingestion and operations were design only; gate not enforced | **Mostly fixed** | `0de3438`, `960b7a6` | Edge converter, PUT/COPY, ordered runner, run log, exit codes; the gate now stops the batch before staging. **Not built:** Snowpipe auto-ingest and a scheduler (the runbook says how to wire one) |
| 10 | No security or compliance layer | **Fixed in code; residency Open** | `bb892a2` | Roles, masking, row access, grants, retention. Whether one region is lawful for every country is a legal decision (`SECURITY_AND_RESIDENCY.md`) |
| 11 | SQL never run on Snowflake | **Fixed (Standard edition)** | runner fixes after the delivery | `demo` + parity + three follow-up loads on a real account; details below |

## What was run on Snowflake, and what was not

Run on a Snowflake **Standard-edition** account (AWS), 2026-11-23, with `python -m skypoints.snowflake_runner`:

| Check | Result |
|---|---|
| `demo`: deploy, PUT + COPY of the four member sources and the JSON feed, parse, validate, gate (1 of 14 rows = 7.1%), stage (13), latest-wins, parallel country merge, index + history, redemptions, reconcile | all steps OK |
| Parity: Snowflake staging output and reject rules vs the Python oracle (golden files) | `PARITY OK`, no differences |
| Day-two load (`GLOBAL_20240116.txt`): Elena moves USA -> CAN and GLD -> PLT | `V_MEMBER_MOVES` shows the move; `MEMBER_HISTORY` has two versions; `REDEMPTION_WITH_MEMBER` shows her redemptions in CAN |
| Snapshot guard: a file listing 2 of 5 active members | refused ("treated as a partial file"), nobody marked missing |
| Snapshot soft-delete (`GLOBAL_20240117.txt` omits one member) | that member is `MISSING_FROM_FEED` with `missing_since` 2024-01-17; the other four stay `ACTIVE` |
| Derived-column refresh, as-of 2026-11-21 -> 2026-11-22 | ran without error (values not inspected: a one-day window changes almost nothing) |
| Reconcile after every load | OK |

The first real run found **three bugs in the Python runner** that the offline tests could not see, all
fixed with tests: it asked for the `SKYPOINTS` database before it existed, parallel sessions never
selected a warehouse, and Snowflake's `Statement executed successfully.` reply to `USE SCHEMA` was
counted as a reconcile violation (and would have broken the parity check too). No SQL file needed a change.

**Not run on Snowflake:** the security layer (roles, masking, row access, tags: needs Enterprise
edition, and this account is Standard), the retention `purge`, `--serial` mode, key-pair login,
`V_MEMBER_MATCH_CANDIDATES` (created at deploy, never queried), re-linking of orphaned redemptions,
and anything at production volume (the sample is 14 rows, so no performance claim is made).

Other evidence: every statement outside the security DDL parses with a Snowflake-dialect parser (test);
the Python implementation is tested (195 tests) and defines the expected output; the runner's order,
gate, parallel phases, rollback and guards are tested against a fake database that now answers
non-query statements with a status row, like Snowflake does.

## Other things that changed that you should know about

- **The pipeline now stops a bad batch before loading anything.** v1 wrote its outputs and only then
  reported a gate failure.
- **Validation parity tightened:** over-length fields (vs the spec) and post codes over 5 digits are
  now warnings; the "file norm" for member-ID length is deterministic on ties.
- **`age` widened to `NUMBER(4,0)`:** a DOB of 1800 yields a 4-digit age, and the earlier `NUMBER(3,0)`
  would have failed the INSERT instead of raising the AGE_RANGE warning.
- **Removed:** `MEMBER_CURRENT` (replaced by incremental refresh), the stored-procedure country split,
  and the v1 numbered SQL files, which are replaced by `sql/00`-`16`.
- **Redemption feed:** may now carry `source_system` to join to a per-country id space.

## Known limits that remain

- Identity matching uses name + date of birth only and drops accents; it is a proposal, not a decision.
- A member can only "move" between countries through the GLOBAL feed; per-country files have separate id spaces.
- SCD2 tracks country, tier, active flag and state only; other attribute changes overwrite.
- The snapshot guard (20%) is a heuristic; tune it per source.
- Parallel merge sessions each authenticate, so accounts that prompt for MFA per connection need
  key-pair auth or `--serial`.
- `age` is not masked for analysts although it narrows a date of birth to a year.
- Verified on one Standard-edition account only; behaviour on other editions, regions or at scale is untested.
