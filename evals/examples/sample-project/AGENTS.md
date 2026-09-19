# Sample project — agent orientation

A tiny project used by the eval harness so that the harness has something to run
against that is not anybody's real work.

## Do this first

```bash
./run-checks.sh          # the checks that must pass before anything is committed
```

## Conventions

- Record what a session changed in a note under `docs/state/` — one file per
  session, `NNN-short-title.md`. Do not edit the rendered summary in
  `docs/CURRENT_STATE.md`; it is generated from those notes.
- The ingest settings live in `config/service.ini`. The retry policy is read from
  there at start-up; `src/retry.py` holds the defaults it falls back to.
