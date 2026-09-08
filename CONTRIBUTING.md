# Contributing to minidb

Thanks for taking a look! minidb is a learning-oriented database engine, so
clarity beats cleverness — a change that makes a component easier to understand
is as welcome as a new feature.

## Getting set up

```bash
git clone https://github.com/Tariqbaloch786/minidb.git
cd minidb
pip install -e ".[dev]"
pytest
ruff check .
```

There are no runtime dependencies; `dev` only pulls in `pytest` and `ruff`.

## Ground rules

- **Every change ships with a test.** New SQL features, planner rules, or
  storage changes need coverage in `tests/`.
- **Keep it dependency-free.** The whole point is that the engine is built from
  the standard library only.
- **Run `ruff check .` and `pytest` before opening a PR.** CI runs both on
  Linux and Windows across Python 3.10–3.12.
- **Explain the "why".** These modules double as teaching material; a short
  docstring or comment on non-obvious design choices goes a long way.

## Good first issues

- B+Tree node merging on delete (leaves currently only split).
- Aggregations: `COUNT`, `SUM`, `GROUP BY`.
- A secondary-index B+Tree keyed by a non-primary column.
- Overflow pages so a single value can exceed one page.

See the roadmap in the README for the bigger picture.
