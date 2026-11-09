"""Documents whose text was transcribed from a scanned copy (OCR + manual review), not taken from a PDF's text layer.

Side table `transcribed_docs` (like `glossary` / `bill_meta`: `chunks`, fingerprint and vectors are untouched; every read tolerates a
missing table). Filled by `python -m billrag add-base-act --text` and by `refresh_bills.py` from the reviewed texts in sources/base_acts/.
Every answer whose evidence includes such a document gets NOTE under it (web.py), the answering model gets STATUS beside the passage
(acts.status_lines), and its Sources line says so. Owner's wording, 2026-11-09: OCR'd and manually reviewed, but check the physical
document before a high-stakes decision.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcribed_docs(bill_key TEXT PRIMARY KEY, method TEXT NOT NULL, reviewed TEXT, note TEXT);
"""
METHOD = "OCR of a scanned copy, manually reviewed"

NOTE = ("The text of {titles} was read from a scanned copy by OCR and checked by hand, so a small reading error is still possible. "
        "Before a high-stakes decision, check the wording against the official printed document.")
STATUS = (" Its text was transcribed from a scanned copy (OCR, manually reviewed), not taken from an official text file: it is reliable "
          "for what the law says, but for a high-stakes point the reader should check the official printed document.")
SOURCE_TAG = " - transcribed from a scan"


def record(store, key, method=METHOD, reviewed=None, note=None):
    store.db.executescript(SCHEMA)
    store.db.execute("INSERT OR REPLACE INTO transcribed_docs VALUES(?,?,?,?)", (key, method, reviewed, note))


def forget(store, key):
    try:
        store.db.execute("DELETE FROM transcribed_docs WHERE bill_key=?", (key,))
    except Exception:
        pass


def keys(store):
    try:
        return {r[0] for r in store.db.execute("SELECT bill_key FROM transcribed_docs")}
    except Exception:  # an index without the table: nothing transcribed
        return set()


def is_transcribed(store, key):
    return key in keys(store)


def note(store, evidence_keys):
    """The fixed note for an answer, or None when no evidence document was transcribed."""
    have = keys(store)
    hits = [k for k in dict.fromkeys(evidence_keys) if k in have]
    if not hits:
        return None
    titles = []
    for k in hits:
        b = store.bill(k) if hasattr(store, "bill") else None
        titles.append((b or {}).get("title") or k)
    return NOTE.format(titles=", ".join(titles))
