# ch11 — creditor matrix parser

Pulls creditor names out of bankruptcy filing PDFs.

```sh
python3 matrix_parse.py <filing.pdf>
python3 -m unittest test_matrix_parse -v
```

Works on native (post-2020) filings. **Does not work on scanned pre-2015
filings** — see `docs/CH11_STEP1.md`. PDFs are gitignored.
