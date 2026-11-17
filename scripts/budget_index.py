#!/usr/bin/env python3
"""The Budget service's index (data/budget/index.sqlite) <-> R2, like scripts/procurement_index.py (same code, other keys): the index is
built from PDFs that live outside git (`python -m budget build`), so upload it after each rebuild; the deploy job fetches it (target budget).

  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY python scripts/budget_index.py upload   # after `python -m budget build`
  python scripts/budget_index.py fetch                                                      # deploy job: R2 -> data/budget/

R2 keys: budget/index.sqlite, budget/index.json ({sha256, bytes, uploaded}). Public documents only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import procurement_index as pi  # noqa: E402

pi.KEY, pi.META, pi.LOCAL = "budget/index.sqlite", "budget/index.json", Path("data/budget/index.sqlite")

if __name__ == "__main__":
    pi.main()
