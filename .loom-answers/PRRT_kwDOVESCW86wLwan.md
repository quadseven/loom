`_gather_asks` filters out entries without `number` before constructing `keyed`.
The later `i["number"]` access therefore only sees dictionaries with that key,
so the reported `KeyError` path is already guarded.
