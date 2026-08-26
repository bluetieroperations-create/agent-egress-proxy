# labclosure — week-one spike

Detects NIH-funded labs whose support has ended, from the free public
RePORTER API. See `docs/WEEK_ONE_FINDINGS.md` for measured results.

```sh
python3 fetch_reporter.py MI     # writes reporter_MI.json (~25s, 17.9k awards)
python3 run.py MI                # candidates -> national check -> list
python3 -m unittest test_lab_signal -v
```

`lab_signal.py` is pure + stdlib; all network I/O is in `fetch_reporter.py`
and `run.py`. Corpus JSON is not committed — regenerate it with the fetcher.

## Week two

```sh
python3 nsf_fetch.py MI          # sweep active NSF awards (~35s)
python3 nsf_tighten.py           # NSF overlap, three match tiers
```

**Read `docs/WEEK_TWO_FINDINGS.md` before building on this.** The lab-level
closure signal was verified against public faculty pages and does not mean what
it appears to. The institution-level aggregate is unaffected.
