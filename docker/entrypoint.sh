#!/bin/sh
# Container start: restore the index from R2 once, then serve. Fails loudly if the restore does not complete.
set -eu
cd /app
if [ ! -f data/bills/index.sqlite ] || [ ! -f data/bills/vectors.npz ]; then
  python scripts/fetch_index.py --bucket "${R2_BUCKET:?R2_BUCKET is required}" --data data/bills
fi
exec python -m billrag serve --host 0.0.0.0 --port 8000
