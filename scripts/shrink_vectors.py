#!/usr/bin/env python3
"""Convert vectors.npz to float16 in place (half the file and half the RAM; fingerprint and ids are kept).

  python scripts/shrink_vectors.py [--data data/bills]      # then: python scripts/upload_r2.py --include-index ...
"""
import argparse
from pathlib import Path

import numpy as np


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/bills")
    a = ap.parse_args()
    path = Path(a.data) / "vectors.npz"
    z = np.load(path)
    parts = {k: z[k] for k in z.files}
    if parts["vectors"].dtype == np.float16:
        print("already float16")
        return
    before = path.stat().st_size
    parts["vectors"] = parts["vectors"].astype("float16")
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, **parts)
    tmp.replace(path)
    print(f"{before / 1e6:.0f} MB -> {path.stat().st_size / 1e6:.0f} MB; in RAM {parts['vectors'].nbytes / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
