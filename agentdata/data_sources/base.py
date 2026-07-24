"""
base.py -- the DataSource contract.

A DataSource is where raw records come from: a wholesale API, a bulk dataset,
a scraper, or (for dev/tests) a deterministic stub. Products depend on the
DataSource *interface*, so swapping the stub for a real licensed feed changes
one wiring line and nothing in the product.
"""
from __future__ import annotations


class DataSource:
    name = "base"

    def fetch(self, query):
        """query -> raw dict of normalized facts. Raise on hard failure;
        return {} for 'no records found' (a valid, cacheable answer)."""
        raise NotImplementedError
