"""The documents we index, their legal status, and the 2026 amendments (hand-encoded: the schedule is a table)."""
from pathlib import Path

GAZ = Path("procurement/raw/gazettes")   # tracked in git
LOCAL = Path("data/procurement")         # git-ignored; supplied by the owner
NPC = LOCAL / "npc"                      # git-ignored; `python procurement/scripts/npc_fetch.py --download --lang English`
TEXTS = Path("procurement/npc_texts")    # tracked: curated text of short NPC documents (see the header of each file)

# status: current = in force; transitional = only for procurement actions begun before 2025-01-01 (up to contract award,
# PG2024 §1.4(e)); everything else in the 2006 family was repealed by PG2024 §1.4(d)(ii).
GUIDANCE = ("Non-binding guidance: Ministry of Finance guide of February 2017, written mainly for FIDIC and ICTAD works contracts and before the "
            "2024 Guidelines. The contract's own conditions and PG2024/PM2024 prevail.")
# Passages with their own end date. They stay searchable: they still matter for what happened while they were in force. ask.py tells the model
# and adds a note once the date has passed. Add one whenever a new source says "valid until". Found with a search for such wording, 2026-11-07.
#   docs, until (ISO), shown as, note = the sentence for the reader; heading = only that passage; mentions = only if the answer speaks of it
TIME_LIMITS = (
    dict(docs=("PFC0326", "PFC0326i"), until="2026-09-30", shown="30 September 2026",
         note="PF Circular 03/2026 and its amendment 03/2026 (i) say their provisions are valid until 30 September 2026. That date has passed and these "
              "sources hold no later circular extending it, so check with the Treasury's Public Finance Department whether they still apply to your case. "
              "This limit is the circular's own: it does not end rules in the Guidelines or the Manual."),
    dict(docs=("FRA",), heading="FR 113(6)", mentions=r"113\s*\(6\)|advance\s+.?B.?\s+account", until="2025-03-31", shown="31 March 2025",
         note="The delegated authority in FR 113(6) was valid only until 31 March 2025, which has passed, and these sources hold no later extension."),
)
TEMPLATE = ("Our own summary of a model standard bidding document (a template, not a rule, and not the publisher's wording): the contract's own "
            "conditions decide the case. The Guidelines and Manual prevail.")
UNOFFICIAL = ("Unofficial English translation made for this index from the Sinhala circular (no official English version is published); the Sinhala text is authoritative.")

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
# From nprocom.gov.lk (fetched 2026-11-06). `amends`: the document whose sections a supplement changes (same numbering);
# retrieval shows the supplement next to every hit on that section, and the amended chunks get a note.
_SUP = "Supplement {n} to the Procurement Manual 2024 (Goods, Works and Non-consulting Services), NPC"
DOCS += [
    dict(id=f"SUP{n}", kind="supplement", status="current", issued=iss, effective=eff, amends="PM2024", parser="curated",
         title=_SUP.format(n=n) + f", issued {iss}, effective {eff}", file=TEXTS / f"SUP{n}.md")
    for n, iss, eff in [("01", "2025-09-18", "2025-01-01"), ("02", "2025-12-12", "2025-12-12"), ("03", "2026-02-05", "2026-02-05"),
                        ("04", "2026-03-01", "2026-03-01"), ("05", "2026-06-25", "2026-06-25"), ("07", "2026-09-24", "2026-09-24")]
] + [
    dict(id="CG2007", kind="guidelines", status="current", issued="2007-08-01", effective="2007-08-01", parser="numbered",
         title="Guidelines for Selection and Employment of Consultants, August 2007 (NPA; still applied by the NPC to consulting "
               "services, NPC Circular 01/2023)", file=NPC / "consultancy" / "CONSULTANT_GUIDELINE_AUGUST_2007R1.pdf"),
    dict(id="CGS01", kind="supplement", status="current", issued="2025-12-12", effective="2025-12-12", amends="CG2007",
         parser="curated", title="Supplement 01 to the Consulting Services Manual 2007: revised threshold limits and payments, "
                                 "NPC, issued 2025-12-12", file=TEXTS / "CGS01.md"),
    dict(id="NPCC2024", kind="circular", status="current", issued="2024-12-19", effective="2025-01-01", parser="curated",
         title="NPC Circular No. 01/2024: Procurement Guidelines 2024 come into force on 1 January 2025", file=TEXTS / "NPCC2024.md"),
    dict(id="PFC0326", kind="circular", status="current", issued="2026-06-11", effective="2026-06-11", parser="curated",
         title="Public Finance Circular No. 03/2026: special measures for public procurement (global conflict, Ditwah cyclone), "
               "Treasury with NPC concurrence, valid until 30.09.2026", file=TEXTS / "PFC0326.md"),
    dict(id="PFC0326i", kind="circular", status="current", issued="2026-08-13", effective="2026-08-13", parser="curated",
         title="Public Finance Circular No. 03/2026 (i): amends PF Circular 03/2026 (bid-closing cut-off 31.03.2026; price "
               "adjustment for contracts under 3 months)", file=TEXTS / "PFC0326i.md"),
    dict(id="PFC0126", kind="circular", status="current", issued="2026-03-28", effective="2026-03-28", parser="curated",
         title="Public Finance Circular No. 01/2026: procurement may start before budgetary allocations; no award until budget "
               "is confirmed", file=TEXTS / "PFC0126.md"),
    dict(id="PFC0426", kind="circular", status="current", issued="2026-07-14", effective="2026-07-14", parser="curated",
         title="Public Finance Circular No. 04/2026: beneficial ownership disclosure and reporting for Ditwah-related procurement",
         file=TEXTS / "PFC0426.md"),
    dict(id="PFC0324", kind="circular", status="current", issued="2024-08-15", effective="2024-08-15", parser="curated",
         title="Public Finance Circular No. 03/2024: Public Financial Management Act No. 44 of 2024; FR 1992 continue where not "
               "inconsistent (s. 67(6))", file=TEXTS / "PFC0324.md"),
    dict(id="PFMA", kind="act", status="current", issued="2024-08-08", effective="2024-08-08", parser="act",
         title="Public Financial Management Act, No. 44 of 2024 (certified 08.08.2024)",
         file=LOCAL / "treasury" / "PFM Act No. 44 of 2024.pdf"),
    dict(id="GPMCM", kind="guidance", status="current", issued="2017-02-01", effective="2017-02-01", parser="curated", note=GUIDANCE,
         title="Guide to Project Management and Contract Management for Infrastructure Development Projects (Works), Ministry of "
               "Finance, February 2017: payment certification, withholding payments, the Engineer's functions",
         file=TEXTS / "GPMCM.md"),
    dict(id="SBDW", kind="template", status="current", issued="2007-01-01", effective="2007-01-01", parser="curated", note=TEMPLATE,
         title="Standard Bidding Document, Procurement of Works (ICTAD/SBD/01, Second Edition, January 2007), conditions of contract: "
               "change in law (cl. 45) and price adjustment (cl. 47)", file=TEXTS / "SBDW.md"),
    dict(id="SBDG", kind="template", status="current", issued="2006-01-01", effective="2006-01-01", parser="curated", note=TEMPLATE,
         title="Standard Bidding Document, Procurement of General Goods, National Competitive Bidding (NPA/Goods/SBD 01), general conditions "
               "of contract: contract price (cl. 14), taxes and duties (cl. 16), change in laws and regulations (cl. 30)", file=TEXTS / "SBDG.md"),
    dict(id="LCC2026", kind="guidance", status="current", issued="2026-06-30", effective="2026-06-30", parser="numbered",
         title="Detailed Guidance Note on Assessment of Life Cycle Cost / Total Cost of Ownership, NPC (2026)",
         file=NPC / "procurement-notices" / "DGNOAOLC_com.pdf"),
    dict(id="ESM2026", kind="manual", status="current", issued="2026-10-22", effective="2026-10-22", parser="numbered",
         title="Sector Specific Procurement Manual 2026: investor selection for electricity generation, energy storage, "
               "transmission capacity and ancillary services, NPC (in effect from 22 October 2026)",
         file=NPC / "sector-specific-manuals" / "2026.10.22-final-manual-1.pdf"),
    dict(id="FR", kind="regulations", status="current", issued="1993-10-29", effective="1992-01-01",
         title="Financial Regulations of the Government of Sri Lanka, 1992 edition (amendments up to 31.12.1991; Chapter XIII on "
               "supplies, tenders and works is not in this edition). Amended by Public Finance Circular 01/2020 (28.08.2020, "
               "consolidating earlier amendments), 01/2024, 02/2024 and 01/2024 (I): see FRA. In force under PFM Act No. 44 of "
               "2024 s. 67(6) where not inconsistent with the Act",
         file=LOCAL / "treasury" / "Financial Regulations (FR Manual).pdf"),
    # Sinhala/Tamil-only circulars, translated by us (2026-11-06) from the page images; every chunk carries the UNOFFICIAL note
    dict(id="FRA", kind="regulations", status="current", issued="2025-01-24", effective="2020-09-01", amends="FR",
         parser="curated", note=UNOFFICIAL,
         title="Amendments to the Financial Regulations 1992 by Public Finance Circulars 01/2020 (Part I, 28.08.2020), 01/2024 "
               "and 02/2024 (29.05.2024) and 01/2024 (I) (24.01.2025) - unofficial English translation of the Sinhala text",
         file=TEXTS / "FRA.md"),
    dict(id="PFC0120", kind="circular", status="current", issued="2020-08-28", effective="2020-09-01", parser="curated",
         note=UNOFFICIAL,
         title="Public Finance Circular No. 01/2020, Part II (consolidated Treasury instructions: electronic payments, air tickets, "
               "insurance, telephone limits, service fees, urgent services, assets, security) with amendments (i), (III), (IV) - "
               "unofficial English translation of the Sinhala text", file=TEXTS / "PFC0120.md"),
    dict(id="NPCC2023", kind="circular", status="historical", issued="2023-07-06", effective="2023-07-06", parser="curated",
         title="NPC Circular No. 01/2023: establishment of the NPC; interim use of the 2006/2007 guidelines (superseded for goods, "
               "works and non-consulting services by the 2024 Guidelines)", file=TEXTS / "NPCC2023.md"),
]
# CIDA: rules and regulations under the Construction Industry Development Act, No. 33 of 2014, as listed on cida.gov.lk "Rules & Regulations"
# (pages_e.php?id=82, fetched 2026-11-09; English PDFs in procurement/raw/cida/, one per gazette). One document per instrument (a gazette can
# hold several). Gazette 2430/13 (01.04.2025, in force from that date per CIDA's notice) repealed and substituted two schedules: the substituted
# text is indexed under the instrument it amends and the repealed text is not indexed (parse.parse_gazette). Later amendments that the page
# does not list are not here, and "current" means "listed by CIDA", not checked against every later gazette.
CIDA = Path("procurement/raw/cida")
CIDA_ACT = "made under the Construction Industry Development Act, No. 33 of 2014"
_G2430 = ("Schedule substituted by Gazette 2430/13 (01.04.2025), in force from 1 April 2025 (CIDA notice); the 2018 text it replaced is not "
          "in these sources.")
_G2430_SAVING = (" Under regulation 2 of those amending regulations, a registration made before then that has not expired, been suspended or "
                 "cancelled remains valid for its period.")
_STOP2430 = r"^\s*\(\d\)\s+by the repeal|^\s*\d+\.\s+Every registration made in terms of"
_CIDA = [  # id, gazette file, instrument (1 = first in the gazette), gazette date, short title, extra
    ("CIDAQP", "2057-05", 1, "2018-02-05", "Construction Industry Development (Registration of Qualified Persons) Regulation of 2017", {}),
    ("CIDACR", "2057-05", 2, "2018-02-05", "Construction Industry Development (Registration of Contractors) Regulation No. 1 of 2017; First and "
     "Second Schedules (registration criteria, grading, points; fees) as substituted by Gazette 2430/13 of 01.04.2025",
     dict(replaces=dict(gazette="2430-13", instrument=2, schedules={1: 1, 2: 2}, note=_G2430 + _G2430_SAVING, stop=_STOP2430))),
    ("CIDATA", "2057-05", 3, "2018-02-05", "Construction Industry Development (Technical Auditing Procedure and Fees) Regulation of 2017", {}),
    ("CIDARC", "2065-41", 1, "2018-04-05", "Construction Industry Development (Register of Contractors) Rules of 2017", {}),
    ("CIDAQMA", "2065-41", 2, "2018-04-05", "Construction Industry Development (Registration of Quality Management Auditors) Rules of 2017", {}),
    ("CIDAFC", "2085-19", 1, "2018-08-23", "Construction Industry Development (Temporary Registration of Foreign Contractors) Regulations of 2018", {}),
    ("CIDAICW", "2085-20", 1, "2018-08-23", "Construction Industry Development (Procedures and Standards for Identified Construction Work) "
     "Regulation of 2018", {}),
    ("CIDAADJ", "2113-63", 1, "2019-03-08", "Construction Industry Development (Registration of Adjudicators) Regulations of 2019; fee Schedule "
     "as substituted by Gazette 2430/13 of 01.04.2025",
     dict(replaces=dict(gazette="2430-13", instrument=1, schedules={1: 1}, note=_G2430, stop=_STOP2430))),
    ("CIDAADR", "2114-01", 1, "2019-03-11", "Construction Industry Development (Register of Adjudicators) Rules of 2019", {}),
    ("CIDAAPP", "2116-47", 1, "2019-03-28", "Construction Industry Development (Appeal Procedure) Regulations of 2019 (appeals to the Appeals "
     "Board against CIDA decisions)", {}),
    ("CIDAIS", "2116-48", 1, "2019-03-28", "Construction Industry (Information Secretariat) Regulations of 2019", {}),
    ("CIDAHM", "2116-49", 1, "2019-03-28", "Construction Industry Development (Registration of Heavy Construction Machinery and Equipment) "
     "Rules of 2019", {}),
    ("CIDASW", "2181-21", 1, "2020-06-25", "Construction Industry Development (Register of Skilled Construction Workers, Construction Site "
     "Supervisors, Middle Level Technical Officers and Other Professionals in the Construction Industry) Regulations of 2020", {}),
    ("CIDACIC", "2181-21", 2, "2020-06-25", "Construction Industry Development (Craft Identity Cards) Regulations of 2020", {}),
    ("CIDAQPD", "2181-21", 3, "2020-06-25", "Construction Industry Development (Disciplinary Procedure for Qualified Persons) Regulations of 2020", {}),
    ("CIDACD", "2181-21", 4, "2020-06-25", "Construction Industry Development (Guidelines for Disciplinary Procedure for Registered Contractors) "
     "Regulations of 2020 (acts or omissions and sanctions)", {}),
    ("CIDAPD", "2181-21", 5, "2020-06-25", "Construction Industry Development (Registration of Property Developers) Regulations of 2020", {}),
    ("CIDAMS", "2182-12", 1, "2020-06-30", "Construction Industry Development (Monitoring System of Contractors) Rules of 2020", {}),
    ("CIDAPI", "2182-12", 2, "2020-06-30", "Construction Industry Development (Power to Instruct and Request Information) Rules of 2020", {}),
    ("CIDAIMS", "2336-46", 1, "2023-06-14", "Construction Industry Development (Directory of Importers, Manufacturers and Suppliers) Rules 2023", {}),
]
DOCS += [
    dict(id=i, kind="regulations", status="current", issued=d, effective=x.get("effective", d), parser="gazette", instrument=n,
         title=f"{t}, {CIDA_ACT}, Gazette Extraordinary {int(g[:4])}/{int(g[5:])} "
               f"of {d[8:]}.{d[5:7]}.{d[:4]} (CIDA)",
         file=CIDA / f"{g}_E.pdf", **{k: v for k, v in x.items() if k != "effective"})
    for i, g, n, d, t, x in _CIDA
]
CIDA_IDS = tuple(i for i, *_ in _CIDA)
BY_ID = {d["id"]: d for d in DOCS}
CURRENT = tuple(d["id"] for d in DOCS if d["status"] == "current")
ALL = tuple(d["id"] for d in DOCS)

# Notes on the amended sections, so a reader of the Manual text alone sees that it changed.
SUPPLEMENT_NOTES = [  # (amended doc, ref, heading must contain or None, note)
    ("PM2024", "2.8", None, "Supplement 01 (18.09.2025) adds payments for Variation Review Committees and Procurement Appeal "
                            "Committees; Supplement 04 (01.03.2026) adds payments for Acceptance Committees."),
    ("PM2024", "1.5", "Annexure III", "Replaced by Supplement 02 (12.12.2025): a Non-collusion Declaration replaces this form."),
    ("PM2024", "8.2", None, "Replaced by Supplement 02 (12.12.2025): intention to award within 7 calendar days of the PC determination."),
    ("PM2024", "1.4", None, "Supplement 03 (05.02.2026) adds (d): unsolicited proposals shall not be entertained."),
    ("PM2024", "5.6.1", None, "The CIDA grade tables are amended by Supplement 05 (25.06.2026)."),
    ("PM2024", "3.1.6", None, "Supplement 07 (24.09.2026) adds special circumstance ix (Government Institutions, under LKR 2 million)."),
    ("CG2007", "2.10", None, "Threshold limits revised by Supplement 01 to the Consulting Services Manual (12.12.2025)."),
    ("CG2007", "2.7", None, "Payments revised by Supplement 01 to the Consulting Services Manual (12.12.2025)."),
]
# FR sections that PF Circulars 01/2020, 01/2024 and 02/2024 replaced or amended (texts in FRA, unofficial translation).
FR_AMENDED = ("104", "108", "113", "118", "124", "125", "238", "269", "371", "503", "756", "767", "770", "855")
FR_NOTES = {n: (f"This regulation was amended by Public Finance Circulars (see FRA {n}, an unofficial translation of the Sinhala "
                "circulars): the 1992 text and amounts here may be out of date.") for n in FR_AMENDED}
FR_NOTES.update({n: ("The authority limits for dealing with losses (FR 108) were revised by PF Circulars 01/2020 and 02/2024: "
                     "see FRA 108. Amounts in this 1992 text are out of date.") for n in ("105", "106", "107", "109", "110",
                                                                                       "111", "112")})
# 1992 text that the circulars replaced outright: not indexed or shown, so its old amounts can never be quoted. ref -> what to cut.
FR_REPLACED = {"104": r"\(1\) Inquiry and fixing of responsibility.*?(?=\(3\) Preliminary Report)",  # FR 104(3)-(4) stay in force
               "108": r".*", "269": r".*", "371": r".*", "855": r".*"}
FR_GENERAL_NOTE = ("Financial Regulations 1992 text. Amended regulations are in FRA (unofficial translation of PF Circular 01/2020 "
                   "and later circulars); check an amount or a limit before relying on it.")
NOT_INDEXED = """On nprocom.gov.lk but not indexed: Supplement 06 to the Manual 2024 (Sinhala and Tamil only, no English
version on the site); NPC Circular 01/2026 and the circular on preparation of procurement plans 2026 (Sinhala only); the FAQ
(Sinhala only); the draft Guidelines for Selection and Employment of Consultants (07.08.2026, a draft); the Arbitration Act;
Sinhala/Tamil versions; the Standard Bidding Document for national shopping (NSProcedures-Goods); Public Finance Circular
03/2026 itself (only its amendment 03/2026 (i) is on the site; the base circular is indexed from treasury.gov.lk).
From treasury.gov.lk: the guidelines and forms that PF Circular 01/2020 points to on the PFD website (except Guideline No. 10, in
PFC0120 §5). PF Circular 01/2020 (ii) (English, 20.12.2022, service fees for 2023) was revoked by 01/2020 (III)."""

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
