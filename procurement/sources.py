"""The documents we index, their legal status, and the 2026 amendments (hand-encoded: the schedule is a table)."""
from pathlib import Path

GAZ = Path("procurement/raw/gazettes")   # tracked in git
LOCAL = Path("data/procurement")         # git-ignored; supplied by the owner

# status: current = in force; transitional = only for procurement actions begun before 2025-01-01 (up to contract award,
# PG2024 §1.4(e)); everything else in the 2006 family was repealed by PG2024 §1.4(d)(ii).
DOCS = [
    dict(id="PG2024", kind="guidelines", status="current", issued="2024-11-25", effective="2025-01-01",
         title="Procurement Guidelines - 2024 (Goods, Works and Non-consulting Services), NPC, Gazette 2412/01",
         file=GAZ / "2412-01_E.pdf"),
    dict(id="PM2024", kind="manual", status="current", issued="2025-01-01", effective="2025-01-01",
         title="Procurement Manual 2024 (Goods, Works and Non-consulting Services), NPC",
         file=LOCAL / "PROCUREMENT MANUAL 2024.pdf"),
    dict(id="CON", kind="constitution", status="current", issued="2023-03-29", effective="2022-10-31",
         title="Constitution of Sri Lanka, Chapter XIXB (National Procurement Commission), Revised Edition 2023, as amended up "
               "to 31 October 2022 (English text)",
         file=LOCAL / "Constitution 2023 (as amended to 2022-10-31).pdf"),
    dict(id="PG2006", kind="guidelines", status="transitional", issued="2006-01-01", effective="2006-01-01",
         title="Government Procurement Guidelines 2006 (Goods & Works), NPA",
         file=LOCAL / "ProcurementGuidelines2006_amded12June.pdf"),
    dict(id="PM2006", kind="manual", status="transitional", issued="2006-08-17", effective="2006-08-17",
         title="Procurement Manual 2006 (with Supplement 8), NPA",
         file=LOCAL / "Procurement Manual 2006.pdf"),
]
BY_ID = {d["id"]: d for d in DOCS}

# Gazette 2489/10 (2026-05-18, signed 2026-04-09): amendments to PG2024, effective as from 2025-01-01. Only rows that change
# the English text are attached to clauses; rows 01 and 05 change the Sinhala version only.
AMENDMENT = "Gazette 2489/10 (18.05.2026)"
AMENDMENTS = {
    "Definitions": 'The words "Procurement Entity" are replaced by "Procuring Entity".',
    "1.5": 'In paragraph (a), "Procurement Entity" is replaced by "Procuring Entity".',
    "1.5.4": 'In paragraph (a), "Procurement Entity" is replaced by "Procuring Entity".',
    "4.1.1": 'In paragraph (a), "Procurement Entity" is replaced by "Procuring Entity".',
    "4.1.4": 'In paragraph (b), "Procurement Entity" is replaced by "Procuring Entity".',
    "5.9": 'In paragraph (b), "Procurement Entity" is replaced by "Procuring Entity".',
    "1.3.2": 'In paragraph (b), the word "Officer" is replaced by "Official".',
    "2.8": ('The heading becomes "Payments for Members of Procurement Committees, Bid Evaluation Committees, Acceptance '
            'Committees, Variation Review Committees and Procurement Appeal Committees, Staff Officers and other Officers '
            'assisting in the Procurement Process", and paragraph (a) is replaced by: "Members of the PCs, BECs, ACs, VRCs, '
            'Appeal Committees, Alternate Members, Staff Officers and other officers with specific responsibilities assigned '
            'under these Guidelines shall be paid for their participation in the Procurement Process".'),
    "7.7": 'Stage Two, Step 01: the word "conditional" is replaced by "unconditional".',
    "8.2": "The Manual reference in the margin against Section 8.2 is replaced.",
    "11.3": 'In paragraph (c), the word "shall" is replaced by "may".',
}
