# SkyPoints Member Pipeline

Splits a global loyalty-member base into per-country tables, flattens the partner redemption JSON
feed, and quarantines bad data. Snowflake SQL lives in `sql/`; `src/skypoints` is a tested Python
reference implementation of the same rules.

- Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Run it locally

```bash
pip install -r requirements.txt
python -m pytest -q
PYTHONPATH=src python -m skypoints \
  --members sample_data/IND.csv sample_data/USA.csv sample_data/AUS.xlsx sample_data/GLOBAL_20240115.txt \
  --redemptions sample_data/redemptions_20240115.json --out out --as-of 2026-11-21 --max-error-rate 0.10
```
