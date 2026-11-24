# Security, privacy and data residency

Member data contains personal data (name, date of birth, and behaviour such as flights and
redemptions). This note says what the project enforces in code, and what is a design decision
for the business and legal teams that code cannot settle.

## What is enforced (sql/14_security.sql, 14b, 16; `snowflake_runner security` / `purge`)

| Control | Mechanism |
|---|---|
| Least privilege | Three roles: `SKYPOINTS_ADMIN` (runs the pipeline), `SKYPOINTS_PII_READER` (steward / support), `SKYPOINTS_ANALYST`. Analysts are granted only the curated views and tables, never RAW, staging, rejects, the run log or the identity table. |
| PII masking | Dynamic masking policies on `member_name` and `dob` in every country table. Anyone outside the admin and PII-reader roles sees `*** MASKED ***` / NULL. |
| Row-level access | A row access policy on every country table, the history table and the index: admin sees all; any other role sees only countries listed for it in `ROLE_COUNTRY_ACCESS`. |
| Inventory | `PII_CLASS` tags (`direct` for name, `quasi` for date of birth) so a catalogue or audit can list PII columns. |
| Retention | RAW, staging and reject tables hold *unmasked* source values (including bad values) for replay. `snowflake_runner purge --days N` deletes them after N days (default 30). Curated tables are never purged by it. |
| Name matching | `V_MEMBER_MATCH_CANDIDATES` compares names, so it is PII-reader only. For a masked analyst every name would be the same placeholder and everything would "match". |

Known limits of what is built:
- `age` is not masked, although it narrows a date of birth to a year. Mask or band it if that is too revealing.
- The re-run tolerance in `security` ignores errors on the statements that are expected to fail on a re-run (policy already attached). It matches on the statement, not the error text, because I could not check Snowflake's exact message. A genuinely different failure on those two statements would be hidden; review the log output on a first run.
- Masking and row access policies need Enterprise edition. Check yours before the interview.
- The pipeline must run as `SKYPOINTS_ADMIN` or `ACCOUNTADMIN`; under any other role the row policy hides rows from it.
- Nothing here encrypts beyond Snowflake's defaults, rotates credentials, or audits access. Use `SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY` for the last one.

## Data residency: a decision, not a setting

Some countries restrict where personal data of their residents may be stored or processed
(for example, India's DPDP Act and the EU's GDPR for transfers out of the EEA; the exact rules and
exemptions depend on the data and the lawyer's reading, and I have not given legal advice here).
Per-country tables do **not** by themselves satisfy that: they sit in the same Snowflake account and
region, so every country's data is physically in one place.

Options, from least to most separation:

1. **One account, one region, role-based separation (what is built).** Simple and cheap. Right only
   if legal confirms the chosen region is acceptable for every country's data.
2. **Per-region accounts, one per residency zone**, each running this same pipeline on its own
   countries' files, with the country list in `config.py` filtered per deployment. The global view
   is then built from non-personal aggregates only (tier counts, miles totals), replicated or shared across
   accounts. Strongest separation; most operational cost.
3. **Pseudonymise before landing.** Replace name and date of birth with a keyed token at the edge so
   the central account never holds direct identifiers. Cross-source identity matching (which needs
   names) would then have to run inside each region.

What I would ask before choosing: which countries actually have localisation rules for this data
category, whether derived data such as age or tier counts counts as personal data, and who the
data controller is for the member base. That is the question to put to legal and the data
protection officer; it should be answered before the account layout is fixed, because moving
data between regions afterwards is expensive.
