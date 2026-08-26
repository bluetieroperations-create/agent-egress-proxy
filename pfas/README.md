# pfas — which water systems break the EPA PFAS limits

```sh
curl -o u.zip https://www.epa.gov/system/files/other-files/2023-08/ucmr5-occurrence-data.zip
unzip u.zip -d ucmr5
python3 pfas_exceedance.py ucmr5/UCMR5_All.txt
python3 -m unittest test_pfas_exceedance -v
```

Free bulk download, no key. See `docs/PFAS_FINDINGS.md`.
