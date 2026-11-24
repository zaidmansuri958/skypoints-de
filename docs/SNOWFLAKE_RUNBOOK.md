# Snowflake runbook

How to run the pipeline on your Snowflake trial account, what success looks like, and what to
do when something fails. Everything here is driven by `python -m skypoints.snowflake_runner`.

> **Status:** the Python side is tested offline. The SQL has not yet been run on a real Snowflake
> account: use `demo` to prove it (section 6 lists where behaviour could differ).

## 1. One-time setup (PowerShell)

```powershell
cd D:\Flights_DE\skypoints
python -m pip install -r requirements.txt
```

Find your account identifier in Snowsight: click your name (bottom-left) -> **Account** ->
**View account details** -> copy the *account identifier* (looks like `myorg-myaccount`).

Set credentials as environment variables for this terminal session. Never paste them into
chat, code or a commit.

```powershell
$env:SNOWFLAKE_ACCOUNT  = "myorg-myaccount"
$env:SNOWFLAKE_USER     = "YOUR_USER"
$env:SNOWFLAKE_PASSWORD = "YOUR_PASSWORD"
# optional: $env:SNOWFLAKE_ROLE = "ACCOUNTADMIN"
```

**Account value:** use `<organization name>-<account name>` from Snowsight account details.

**If the password login is refused because MFA is required** (new accounts often enforce it), use
key-pair login. It needs no MFA prompt and works with the parallel sessions. Keep the key files
OUTSIDE the repo (the `.gitignore` also blocks `*.p8` / `*.pem`).

```powershell
$ssl = "C:\Program Files\Git\usr\bin\openssl.exe"          # ships with Git for Windows
& $ssl genrsa -out D:\Flights_DE\sf_rsa.pem 2048
& $ssl pkcs8 -topk8 -inform PEM -in D:\Flights_DE\sf_rsa.pem -out D:\Flights_DE\sf_key.p8 -nocrypt
& $ssl rsa -in D:\Flights_DE\sf_key.p8 -pubout -out D:\Flights_DE\sf_key.pub
(Get-Content D:\Flights_DE\sf_key.pub | Select-Object -Skip 1 | Select-Object -SkipLast 1) -join ""
```

Copy that single line, then in a Snowsight worksheet run (as ACCOUNTADMIN):
`ALTER USER YOUR_LOGIN_NAME SET RSA_PUBLIC_KEY='<the line>';`

```powershell
$env:SNOWFLAKE_PRIVATE_KEY_PATH = "D:\Flights_DE\sf_key.p8"      # instead of SNOWFLAKE_PASSWORD
```

`SNOWFLAKE_AUTHENTICATOR = "externalbrowser"` only works if your account has SSO configured; a plain
trial account does not. If every new connection still prompts for MFA, add `--serial` to `demo` or
`load`: one session, one transaction, no parallel connections.

## 2. Prove it works: the demo

```powershell
$env:PYTHONPATH = "src"
python -m skypoints.snowflake_runner demo
```

This (1) creates the database, an X-SMALL auto-suspending warehouse, tables, functions and
views, (2) loads the four sample sources and the redemption feed through the real SQL, and
(3) compares what Snowflake staged with `tests/golden/*.csv`, which the Python implementation
produced.

Success looks like:

```
[L2026...] ingest_members: OK 14 rows from 4 file(s)
[L2026...] gate: OK error rate 7.1%
[L2026...] stage: OK 13 rows staged
[L2026...] merge_countries: OK 13 winners, parallel
...
[L2026...] reconcile: OK
PARITY OK: Snowflake staging output matches the Python implementation exactly
```

If it prints `PARITY FAIL`, each line is a row that differs: `staging_only_in_expected` means
Snowflake did not produce a row Python did (or produced it differently), `..._in_actual` the
reverse. The row's file and number tell you which input to look at.

Exit codes (use them for alerting): `0` ok, `2` quality gate stopped the batch, `3` reconcile
found an inconsistency, `4` parity mismatch.

## 3. Look around in Snowsight

```sql
USE SCHEMA SKYPOINTS.CORE;
SELECT table_name, country_code, member_name, tier_code, age, stale_member, record_status FROM MEMBER_ALL ORDER BY 1, 3;
SELECT * FROM STG_REJECT WHERE source_system <> 'REDEMPTION' ORDER BY source_file, source_row;   -- the quarantine
SELECT * FROM REDEMPTION_WITH_MEMBER;                                                           -- JSON joined to members
SELECT * FROM MEMBER_HISTORY ORDER BY member_key, valid_from;                                   -- SCD2
SELECT * FROM PIPELINE_RUN_LOG ORDER BY logged_at;                                              -- what ran
```

To show a country move live, edit a copy of the GLOBAL file so a member has a new `Country`
and a later feed date in the file name (e.g. `GLOBAL_20240116.txt`), then:

```powershell
python -m skypoints.snowflake_runner load --members sample_data\GLOBAL_20240116.txt --as-of 2026-11-21 --max-error-rate 0.5
```

```sql
SELECT * FROM V_MEMBER_MOVES;                       -- the move, derived from history
SELECT * FROM MEMBER_HISTORY WHERE member_key = 'GLOBAL|223457' ORDER BY valid_from;
```

## 4. Everyday operation

```powershell
python -m skypoints.snowflake_runner load --members IND.csv USA.csv AUS.xlsx GLOBAL_20240115.txt `
    --redemptions redemptions_20240115.json --as-of 2026-11-21 --max-error-rate 0.02
```

- `--snapshot IND,USA` tells the run those files are **full extracts**, so members they no longer
  list are soft-deleted (`MISSING_FROM_FEED`). Leave it off for delta files. Confirm with the
  source team which mode each feed uses before turning it on.
- `--serial` runs the country merges in one transaction instead of parallel sessions. Use it
  when readers must never see an intermediate state; it is slower on large loads.
- A rerun of the same files is safe: every step is an idempotent MERGE or replace.
- Schedule it with whatever you already run jobs on (Windows Task Scheduler, cron, GitHub
  Actions, Airflow) and alert on a non-zero exit code. `PIPELINE_RUN_LOG` records each step.
- For continuous ingestion at scale, the same canonical CSV the edge step writes is what a
  Snowpipe on `@LANDING` would auto-ingest; the SQL steps after `RAW_MEMBER_FILE` are unchanged.

## 5. Cost on a trial account

X-SMALL warehouse, `AUTO_SUSPEND = 60`: the demo uses a few seconds of compute. Check the
credit balance in Snowsight under **Admin -> Cost management**. To remove everything:

```sql
DROP DATABASE IF EXISTS SKYPOINTS;
DROP WAREHOUSE IF EXISTS SKYPOINTS_WH;
```

## 6. Where behaviour could differ on another account

The first run on a Standard-edition account needed no SQL changes. These are the places where
Snowflake's behaviour could still differ (another edition, region or setting), so look here first
if the demo errors or the parity diff is non-empty:

1. **Date parsing of non-padded dates** (`TRY_TO_DATE('1/1/2022','MM/DD/YYYY')`). If Snowflake
   rejects single-digit months/days, the IND rows show up in the parity diff. Fix in `PARSE_SRC_DATE`.
2. **`QUALIFY` after a `WHERE ... NOT EXISTS`** in `04_validate.sql` (duplicate key rule).
3. **`INSERT ... SELECT ... UNION ALL`** column type unification in `04_validate.sql`
   (a `NULL` literal in the first branch is cast to `VARCHAR` to avoid this).
4. **`PUT` with a Windows path** containing spaces. The runner quotes it and uses forward slashes.
5. **Parallel sessions**: each opens its own connection; if MFA prompts on every connection use
   a key-pair or a network policy exemption, or run `--serial`.
6. **Row access / masking policies and tags** (`sql/14_*`) need **Enterprise** edition. On a Standard
   account the `security` command fails at the first masking policy; skip it and present the security
   layer as designed and syntax-reviewed but not executed. Nothing else in the pipeline needs Enterprise.

Each of these is a one-line SQL change; the offline tests cover the orchestration around them.
