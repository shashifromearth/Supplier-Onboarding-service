"""Generate synthetic W-9 test documents, per-tenant supplier masters and ground truth.

All companies, people and TINs are fictitious. The form is a simplified *replica* of the
IRS W-9 layout (2024 and 2018 revisions) - good enough to exercise layout, checkbox,
signature and OCR logic; it is labelled as a synthetic test document on every page.

Usage:  python scripts/generate_samples.py
"""
from __future__ import annotations

import csv
import io
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageEnhance, ImageFilter  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from w9_onboarding.utils.security import tin_fingerprint  # noqa: E402

DOCS = ROOT / "samples" / "documents"
TENANTS = ROOT / "samples" / "tenants"
W, H = letter

CLASS_BOXES = [  # (label, x, y_from_top)
    ("Individual/sole proprietor", 50, 186),
    ("C corporation", 190, 186),
    ("S corporation", 290, 186),
    ("Partnership", 390, 186),
    ("Trust/estate", 480, 186),
]


def T(y: float) -> float:
    return H - y


def draw_w9(c: canvas.Canvas, d: dict, rev: str = "2024", signed: bool = True, seed: int = 0) -> None:
    rnd = random.Random(seed)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, T(52), "Form W-9")
    c.setFont("Helvetica", 7)
    c.drawString(40, T(64), "(Rev. March 2024)" if rev == "2024" else "(Rev. October 2018)")
    c.drawString(40, T(76), "Department of the Treasury  Internal Revenue Service")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(160, T(52), "Request for Taxpayer")
    c.drawString(160, T(66), "Identification Number and Certification")
    c.setFont("Helvetica", 7)
    c.drawString(440, T(52), "Give form to the")
    c.drawString(440, T(61), "requester. Do not")
    c.drawString(440, T(70), "send to the IRS.")
    c.line(36, T(90), W - 36, T(90))

    def label(y: float, text: str, x: float = 40, size: float = 7.5) -> None:
        c.setFont("Helvetica", size)
        c.drawString(x, T(y), text)

    def value(y: float, text: str | None, x: float = 50, size: float = 11) -> None:
        if text:
            c.setFont("Helvetica", size)
            c.drawString(x, T(y), text)

    label(102, "1 Name of entity/individual. An entry is required. (For a sole proprietor or disregarded entity, enter the owner's name on line 1.)")
    value(118, d["legal_name"])
    c.line(36, T(124), W - 36, T(124))
    label(136, "2 Business name/disregarded entity name, if different from above.")
    value(152, d.get("dba_name"))
    c.line(36, T(158), W - 36, T(158))
    if rev == "2024":
        label(170, "3a Check the appropriate box for federal tax classification of the entity/individual whose name is entered on line 1. Check only one.")
    else:
        label(170, "3 Check appropriate box for federal tax classification of the person whose name is entered on line 1. Check only one.")

    cls = d.get("tax_classification")
    checked = {
        "INDIVIDUAL_SOLE_PROPRIETOR": "Individual/sole proprietor", "C_CORPORATION": "C corporation",
        "S_CORPORATION": "S corporation", "PARTNERSHIP": "Partnership", "TRUST_ESTATE": "Trust/estate",
    }.get(cls or "")

    def box(x: float, y: float, tick: bool) -> None:
        c.rect(x, T(y) - 1, 8, 8)
        if tick:
            c.setLineWidth(1.4)
            c.line(x + 1, T(y), x + 7, T(y) + 6)
            c.line(x + 1, T(y) + 6, x + 7, T(y))
            c.setLineWidth(1)

    for text, x, y in CLASS_BOXES:
        box(x, y, checked == text)
        label(y, text, x=x + 13, size=8)
    is_llc = bool(cls and cls.startswith("LLC"))
    box(50, 204, is_llc)
    label(204, "LLC. Enter the tax classification (C = C corporation, S = S corporation, P = Partnership)", x=63, size=8)
    if is_llc and cls != "LLC_UNSPECIFIED":
        value(204, cls[-1], x=470, size=11)
    box(50, 220, cls == "OTHER")
    label(220, "Other (see instructions)", x=63, size=8)
    if rev == "2024":
        label(238, "3b If on line 3a you checked Partnership or Trust/estate, and you have foreign partners, owners, or beneficiaries, check this box", size=6.5)
    label(254, "4 Exemptions (codes apply only to certain entities, not individuals): Exempt payee code (if any)", size=6.5)
    c.line(36, T(260), W - 36, T(260))
    label(272, "5 Address (number, street, and apt. or suite no.). See instructions.")
    label(272, "Requester's name and address (optional)", x=400, size=7)
    value(288, d.get("address"))
    value(288, d.get("requester", "KHM Demo Corp - Accounts Payable"), x=400, size=8)
    c.line(36, T(294), 390, T(294))
    label(306, "6 City, state, and ZIP code")
    csz = None
    if d.get("city"):
        csz = f"{d['city']}, {d['state']} {d['zip']}"
    value(322, csz)
    c.line(36, T(328), 390, T(328))
    label(340, "7 List account number(s) here (optional)")
    c.line(36, T(348), W - 36, T(348))

    c.setFillGray(0.0)
    c.rect(36, T(364), 40, 12, fill=1)
    c.setFillGray(1.0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, T(361), "Part I")
    c.setFillGray(0.0)
    c.drawString(84, T(361), "Taxpayer Identification Number (TIN)")
    label(378, "Enter your TIN in the appropriate box. The TIN provided must match the name given on line 1")
    label(388, "to avoid backup withholding. For individuals, this is generally your social security number.")
    label(378, "Social security number", x=420)
    c.rect(420, T(400), 140, 16)
    if d.get("tin_type") == "SSN":
        value(396, d["tin_display"], x=428, size=11)
    label(412, "or", x=420)
    label(424, "Employer identification number", x=420)
    c.rect(420, T(446), 140, 16)
    if d.get("tin_type") == "EIN":
        value(442, d["tin_display"], x=428, size=11)

    c.setFillGray(0.0)
    c.rect(36, T(470), 40, 12, fill=1)
    c.setFillGray(1.0)
    c.drawString(40, T(467), "Part II")
    c.setFillGray(0.0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(84, T(467), "Certification")
    cert = [
        "Under penalties of perjury, I certify that:",
        "1. The number shown on this form is my correct taxpayer identification number (or I am waiting for a number to be issued to me); and",
        "2. I am not subject to backup withholding because I am exempt, or I have not been notified by the Internal Revenue Service;",
        "3. I am a U.S. citizen or other U.S. person (defined below); and",
        "4. The FATCA code(s) entered on this form (if any) indicating that I am exempt from FATCA reporting is correct.",
    ]
    for i, line in enumerate(cert):
        label(484 + i * 10, line, size=6.5)
    c.line(36, T(540), W - 36, T(540))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, T(566), "Sign Here")
    label(566, "Signature of U.S. person", x=95, size=7)
    c.line(185, T(570), 380, T(570))
    label(566, "Date", x=400, size=7)
    value(566, d.get("signature_date_display"), x=425, size=10)
    if signed:
        c.setLineWidth(1.3)
        x = 195.0
        for _ in range(5):
            dx = rnd.uniform(18, 32)
            c.bezier(x, T(566) + rnd.uniform(-2, 2), x + dx * 0.3, T(566) + rnd.uniform(6, 14),
                     x + dx * 0.6, T(566) - rnd.uniform(4, 10), x + dx, T(566) + rnd.uniform(-2, 4))
            x += dx * 0.85
        c.setLineWidth(1)

    c.setFont("Helvetica-Oblique", 6.5)
    c.drawString(40, 30, "SYNTHETIC TEST DOCUMENT - simplified replica of IRS Form W-9 for software testing. All data is fictitious.")
    c.drawRightString(W - 40, 30, "Form W-9 (Rev. 3-2024)" if rev == "2024" else "Form W-9 (Rev. 10-2018)")


def draw_w8ben(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, T(52), "Form W-8BEN")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(170, T(48), "Certificate of Foreign Status of Beneficial Owner for United")
    c.drawString(170, T(62), "States Tax Withholding and Reporting (Individuals)")
    c.setFont("Helvetica", 7.5)
    c.drawString(40, T(100), "Part I Identification of Beneficial Owner")
    c.drawString(40, T(116), "1 Name of individual who is the beneficial owner")
    c.setFont("Helvetica", 11)
    c.drawString(50, T(132), "Henrik Larsen")
    c.setFont("Helvetica", 7.5)
    c.drawString(40, T(150), "2 Country of citizenship")
    c.setFont("Helvetica", 11)
    c.drawString(50, T(166), "Denmark")
    c.setFont("Helvetica", 7.5)
    c.drawString(40, T(184), "3 Permanent residence address (street, apt. or suite no., or rural route). Do not use a P.O. box.")
    c.setFont("Helvetica", 11)
    c.drawString(50, T(200), "Vesterbrogade 14, 1620 Copenhagen V")
    c.setFont("Helvetica", 7.5)
    c.drawString(40, T(220), "Under penalties of perjury, I declare that I have examined the information on this form.")
    c.drawString(40, T(240), "Sign Here   Signature of beneficial owner                                   Date (MM-DD-YYYY)  09-18-2026")
    c.setFont("Helvetica-Oblique", 6.5)
    c.drawString(40, 30, "SYNTHETIC TEST DOCUMENT - simplified replica for software testing. All data is fictitious.")


def make_pdf(path: Path, d: dict | None, **kw) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("W-9 synthetic test document")
    if d is None:
        draw_w8ben(c)
    else:
        draw_w9(c, d, **kw)
    c.showPage()
    c.save()


def pdf_to_image(pdf_path: Path, out: Path, dpi: int = 150, photo: bool = False,
                 rotate: float = 0.8, blur: float = 0.7, seed: int = 7) -> None:
    import pypdfium2 as pdfium
    img = pdfium.PdfDocument(str(pdf_path))[0].render(scale=dpi / 72).to_pil().convert("RGB")
    if photo:  # simulate a phone photo / poor scan: skew, blur, uneven light, noise, JPEG
        img = img.rotate(rotate, expand=False, fillcolor=(235, 235, 230), resample=Image.BICUBIC)
        img = img.filter(ImageFilter.GaussianBlur(blur))
        grad = Image.linear_gradient("L").resize(img.size).point(lambda p: 200 + p // 5)
        img = Image.composite(img, Image.new("RGB", img.size, (175, 170, 160)), grad)
        img = ImageEnhance.Contrast(img).enhance(0.85)
        rnd = random.Random(seed)
        px = img.load()
        for _ in range(img.width * img.height // 40):
            x, y = rnd.randrange(img.width), rnd.randrange(img.height)
            v = rnd.randint(-40, 40)
            r, g, b = px[x, y]
            px[x, y] = (max(0, min(255, r + v)), max(0, min(255, g + v)), max(0, min(255, b + v)))
        img.save(out, "JPEG", quality=72)
    else:
        img.save(out, "PNG")


# ---------------------------------------------------------------------------
# Scenario definitions (ground truth lives next to the generator so they never drift)
# ---------------------------------------------------------------------------
def ein(s: str) -> dict:
    return {"tin": s.replace("-", ""), "tin_type": "EIN", "tin_display": s}


SCENARIOS: list[dict] = [
    dict(file="01_new_supplier_clean.pdf", tenant="tenant_acme_mfg",
         purpose="Clean digital W-9 from a supplier not in the master",
         data=dict(legal_name="Bluewater Analytics LLC", dba_name=None, tax_classification="LLC_S",
                   address="2140 Riverside Dr Ste 210", city="Austin", state="TX", zip="78741",
                   **ein("74-3091862"), signature_date="2026-09-14", signature_date_display="09/14/2026"),
         expected=dict(decision="CREATE_NEW_SUPPLIER", supplier_id=None)),
    dict(file="02_existing_refresh.pdf", tenant="tenant_acme_mfg",
         purpose="Existing supplier re-submits W-9; name formatted differently, same TIN",
         data=dict(legal_name="Acme Industrial Supply, Inc.", dba_name="Acme Supply", tax_classification="C_CORPORATION",
                   address="1200 W Lake St", city="Chicago", state="IL", zip="60607",
                   **ein("36-4817265"), signature_date="2026-09-02", signature_date_display="Sept 2, 2026"),
         expected=dict(decision="UPDATE_EXISTING_SUPPLIER", supplier_id="SUP-1001")),
    dict(file="03_existing_address_change.pdf", tenant="tenant_acme_mfg",
         purpose="Same TIN + name, new address -> update with non-material change flagged",
         data=dict(legal_name="Northwind Traders LLC", dba_name=None, tax_classification="LLC_C",
                   address="900 Pacific Ave Suite 300", city="Tacoma", state="WA", zip="98402",
                   **ein("41-2938476"), signature_date="2026-09-21", signature_date_display="2026-09-21"),
         expected=dict(decision="UPDATE_EXISTING_SUPPLIER", supplier_id="SUP-1002")),
    dict(file="04_name_match_tin_conflict.pdf", tenant="tenant_acme_mfg",
         purpose="Name + address look identical to an existing supplier but TIN differs (duplicate or impostor?)",
         data=dict(legal_name="Contoso Ltd.", dba_name=None, tax_classification="C_CORPORATION",
                   address="77 Market St", city="Boston", state="MA", zip="02110",
                   **ein("27-9934012"), signature_date="2026-09-25", signature_date_display="09/25/2026"),
         expected=dict(decision="HUMAN_REVIEW", supplier_id=None, review_code="NAME_MATCH_TIN_CONFLICT")),
    dict(file="05_same_tin_different_name.pdf", tenant="tenant_acme_mfg",
         purpose="TIN already on file under a different legal name (restructure, or payment-redirect fraud)",
         data=dict(legal_name="Blue Yonder Freight Partners LP", dba_name=None, tax_classification="PARTNERSHIP",
                   address="3100 Industrial Pkwy", city="Dayton", state="OH", zip="45414",
                   **ein("52-1847390"), signature_date="2026-09-28", signature_date_display="09/28/2026"),
         expected=dict(decision="HUMAN_REVIEW", supplier_id=None, review_code="TIN_NAME_CONFLICT")),
    dict(file="06_missing_signature.pdf", tenant="tenant_acme_mfg",
         purpose="New supplier, form not signed -> invalid for tax reporting",
         signed=False,
         data=dict(legal_name="Riverbend Catering Co", dba_name=None, tax_classification="S_CORPORATION",
                   address="45 Mill St", city="Burlington", state="VT", zip="05401",
                   **ein("03-5527714"), signature_date=None, signature_date_display=None),
         expected=dict(decision="HUMAN_REVIEW", supplier_id=None, review_code="MISSING_SIGNATURE")),
    dict(file="07_invalid_tin.pdf", tenant="tenant_acme_mfg",
         purpose="EIN with a prefix the IRS never issues",
         data=dict(legal_name="Summit Peak Consulting LLC", dba_name="Summit Peak", tax_classification="LLC_P",
                   address="610 Aspen Way", city="Boulder", state="CO", zip="80302",
                   **ein("07-4418290"), signature_date="2026-09-30", signature_date_display="9/30/2026"),
         expected=dict(decision="HUMAN_REVIEW", supplier_id=None, review_code="INVALID_EIN_PREFIX")),
    dict(file="08_fuzzy_ambiguous.pdf", tenant="tenant_acme_mfg",
         purpose="Similar name to a legacy record with no TIN on file; address differs -> ambiguous",
         data=dict(legal_name="Tailspin Toys Co", dba_name=None, tax_classification="C_CORPORATION",
                   address="18 Elm Rd", city="Akron", state="OH", zip="44308",
                   **ein("46-3301299"), signature_date="2026-10-01", signature_date_display="10/01/2026"),
         expected=dict(decision="HUMAN_REVIEW", supplier_id=None, review_code="FUZZY_MATCH_REVIEW_BAND")),
    dict(file="09_phone_photo_existing.jpg", tenant="tenant_acme_mfg", image="photo",
         purpose="Phone photo (skew, blur, noise) of an existing supplier's W-9",
         data=dict(legal_name="Litware Inc", dba_name=None, tax_classification="C_CORPORATION",
                   address="2450 Bayshore Pkwy", city="Mountain View", state="CA", zip="94043",
                   **ein("94-3328810"), signature_date="2026-09-17", signature_date_display="09/17/2026"),
         expected=dict(decision="UPDATE_EXISTING_SUPPLIER", supplier_id="SUP-1006")),
    dict(file="10_wrong_form_w8ben.pdf", tenant="tenant_acme_mfg", w8=True,
         purpose="Foreign supplier sent a W-8BEN instead of a W-9",
         data=dict(legal_name=None, dba_name=None, tax_classification=None, address=None, city=None, state=None,
                   zip=None, tin=None, tin_type=None, signature_date=None),
         expected=dict(decision="HUMAN_REVIEW", supplier_id=None, review_code="FORM_TYPE_NOT_W9")),
    dict(file="11_sole_prop_ssn.png", tenant="tenant_acme_mfg", image="scan",
         purpose="Sole proprietor using an SSN, scanned image",
         data=dict(legal_name="Maria L Gonzalez", dba_name="Gonzalez Landscaping", tax_classification="INDIVIDUAL_SOLE_PROPRIETOR",
                   address="1934 Cactus Wren Ln", city="Mesa", state="AZ", zip="85203",
                   tin="523814470", tin_type="SSN", tin_display="523-81-4470",
                   signature_date="2026-09-08", signature_date_display="09/08/2026"),
         expected=dict(decision="CREATE_NEW_SUPPLIER", supplier_id=None)),
    dict(file="12_rev2018_tin_backfill.pdf", tenant="tenant_acme_mfg", rev="2018",
         purpose="Old form revision; legacy master record has no TIN -> backfill (name+address corroborate)",
         data=dict(legal_name="Wide World Importers LLC", dba_name=None, tax_classification="LLC_C",
                   address="88 Dockside Ave", city="Savannah", state="GA", zip="31401",
                   **ein("58-2210934"), signature_date="2022-03-12", signature_date_display="03/12/2022"),
         expected=dict(decision="UPDATE_EXISTING_SUPPLIER", supplier_id="SUP-1007")),
    dict(file="13_cross_tenant_isolation.pdf", tenant="tenant_globex",
         purpose="Same supplier as #02 but submitted to a different tenant: must NOT match tenant_acme_mfg's record",
         data=dict(legal_name="Acme Industrial Supply, Inc.", dba_name="Acme Supply", tax_classification="C_CORPORATION",
                   address="1200 W Lake St", city="Chicago", state="IL", zip="60607",
                   **ein("36-4817265"), signature_date="2026-09-02", signature_date_display="09/02/2026"),
         expected=dict(decision="CREATE_NEW_SUPPLIER", supplier_id=None)),
]

# (supplier_id, legal, dba, tin_or_None, classification, address, city, state, zip, last_updated)
ACME_MASTER = [
    ("SUP-1001", "ACME INDUSTRIAL SUPPLY INC", "Acme Supply", "364817265", "C_CORPORATION", "1200 West Lake Street", "Chicago", "IL", "60607", "2024-02-11"),
    ("SUP-1002", "Northwind Traders LLC", None, "412938476", "LLC_C", "455 Harbor Blvd", "Tacoma", "WA", "98402", "2023-07-30"),
    ("SUP-1003", "Contoso Limited", None, "275512098", "C_CORPORATION", "77 Market Street", "Boston", "MA", "02110", "2025-01-05"),
    ("SUP-1004", "Fabrikam Inc", None, "521847390", "C_CORPORATION", "3100 Industrial Pkwy", "Dayton", "OH", "45414", "2022-11-19"),
    ("SUP-1005", "Tailspin Toy Company Inc", None, None, "C_CORPORATION", "2207 Commerce Dr", "Columbus", "OH", "43215", "2019-05-02"),
    ("SUP-1006", "Litware, Inc.", None, "943328810", "C_CORPORATION", "2450 Bayshore Parkway", "Mountain View", "CA", "94043", "2024-08-14"),
    ("SUP-1007", "Wide World Importers", None, None, "LLC_C", "88 Dockside Avenue", "Savannah", "GA", "31401", "2018-09-09"),
    ("SUP-1008", "Proseware Inc", None, "811170234", "C_CORPORATION", "12 Harbor View Rd", "Portland", "ME", "04101", "2021-03-03"),
    ("SUP-1009", "PROSEWARE, INC.", None, None, "C_CORPORATION", "12 Harbor View Road", "Portland", "ME", "04101", "2016-06-21"),
    ("SUP-1010", "Adventure Works Cycles", None, "331029384", "C_CORPORATION", "4100 Trail Ridge Rd", "Bend", "OR", "97701", "2024-04-01"),
    ("SUP-1011", "Alpine Ski House LLC", None, "844410293", "LLC_S", "9 Summit Rd", "Park City", "UT", "84060", "2023-12-12"),
    ("SUP-1012", "Coho Winery", "Coho Vineyards", "680019283", "S_CORPORATION", "1500 Vine St", "Napa", "CA", "94559", "2024-10-10"),
    ("SUP-1013", "Fourth Coffee Company", None, "910028374", "C_CORPORATION", "300 Pike St", "Seattle", "WA", "98101", "2025-03-14"),
    ("SUP-1014", "Graphic Design Institute", None, "201938475", "OTHER", "55 Canal St", "New York", "NY", "10013", "2022-06-30"),
    ("SUP-1015", "Lucerne Publishing", None, "131928374", "C_CORPORATION", "800 Lexington Ave", "New York", "NY", "10065", "2024-01-20"),
    ("SUP-1016", "Margie's Travel", None, "560192837", "PARTNERSHIP", "21 Ocean Dr", "Miami Beach", "FL", "33139", "2023-09-01"),
    ("SUP-1017", "Southridge Video LLC", None, "860293847", "LLC_P", "1777 Desert Sky Rd", "Phoenix", "AZ", "85004", "2024-05-05"),
    ("SUP-1018", "Trey Research Inc", None, "770192836", "C_CORPORATION", "140 Research Blvd", "Durham", "NC", "27703", "2025-02-02"),
    ("SUP-1019", "VanArsdel Ltd", None, "391028375", "C_CORPORATION", "601 Lake Ave", "Milwaukee", "WI", "53202", "2021-08-18"),
    ("SUP-1020", "Consolidated Messenger", None, "620192847", "S_CORPORATION", "4 Courier Ln", "Nashville", "TN", "37203", "2024-07-07"),
    ("SUP-1021", "Relecloud Inc", None, "450192873", "C_CORPORATION", "2600 Cloud Way", "Fargo", "ND", "58102", "2025-05-25"),
    ("SUP-1022", "Lamna Healthcare Company", None, "650192833", "C_CORPORATION", "77 Wellness Pkwy", "Orlando", "FL", "32801", "2023-03-30"),
    ("SUP-1023", "A. Datum Corporation", None, "222019384", "C_CORPORATION", "15 Analytics Ct", "Princeton", "NJ", "08540", "2024-11-11"),
    ("SUP-1024", "Blue Yonder Airlines", None, "870192845", "C_CORPORATION", "1 Airport Rd", "Denver", "CO", "80249", "2022-02-22"),
    ("SUP-1025", "Bellows College", None, "060192834", "OTHER", "100 College Ave", "Hartford", "CT", "06106", "2020-09-15"),
    ("SUP-1026", "Best For You Organics Company", None, "930192844", "LLC_S", "88 Farm Rd", "Eugene", "OR", "97401", "2024-06-06"),
    ("SUP-1027", "Humongous Insurance", None, "230192855", "C_CORPORATION", "500 Risk Plaza", "Hartford", "CT", "06103", "2023-10-10"),
    ("SUP-1028", "Woodgrove Supply Partners", None, "580192866", "PARTNERSHIP", "19 Timber Ln", "Macon", "GA", "31201", "2022-12-01"),
    ("SUP-1029", "Northwind Electric Cars", None, "410192877", "C_CORPORATION", "700 Volt Ave", "Spokane", "WA", "99201", "2025-04-04"),
    ("SUP-1030", "Bluewater Marine Supply", None, "590192888", "C_CORPORATION", "3 Pier Rd", "Mobile", "AL", "36602", "2024-03-03"),
]

GLOBEX_MASTER = [
    ("GBX-2001", "Initech Software LLC", None, "472019384", "LLC_C", "4120 Freidrich Ln", "Austin", "TX", "78744", "2025-01-01"),
    ("GBX-2002", "Umbrella Logistics Inc", None, "381920394", "C_CORPORATION", "1 Raccoon Way", "Lansing", "MI", "48933", "2024-04-04"),
    ("GBX-2003", "Hooli Office Products", None, "770192800", "S_CORPORATION", "123 Main St", "Fresno", "CA", "93721", "2023-03-03"),
    ("GBX-2004", "Vandelay Industries", "Vandelay Imports", "131111222", "C_CORPORATION", "200 Park Ave", "New York", "NY", "10166", "2024-09-09"),
    ("GBX-2005", "Acme Packaging Co", None, "391234567", "C_CORPORATION", "50 Box Rd", "Racine", "WI", "53403", "2022-02-02"),
]


def write_master(tenant: str, rows: list[tuple]) -> None:
    d = TENANTS / tenant
    d.mkdir(parents=True, exist_ok=True)
    cols = ["supplier_id", "legal_name", "dba_name", "tin_fingerprint", "tin_last4", "tax_classification",
            "address", "city", "state", "zip", "bank_account_ref", "status", "last_updated"]
    with open(d / "supplier_master.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for sid, legal, dba, tin, cls, addr, city, st, z, upd in rows:
            w.writerow([sid, legal, dba or "", tin_fingerprint(tenant, tin) if tin else "", tin[-4:] if tin else "",
                        cls, addr, city, st, z, f"vault://{tenant}/bank/{sid.lower()}", "ACTIVE", upd])


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    write_master("tenant_acme_mfg", ACME_MASTER)
    write_master("tenant_globex", GLOBEX_MASTER)
    truth = []
    for i, s in enumerate(SCENARIOS):
        out = DOCS / s["file"]
        pdf_path = out if out.suffix == ".pdf" else DOCS / f"_tmp_{i}.pdf"
        make_pdf(pdf_path, None if s.get("w8") else s["data"], **({} if s.get("w8") else
                 dict(rev=s.get("rev", "2024"), signed=s.get("signed", True), seed=i)))
        if out.suffix != ".pdf":
            pdf_to_image(pdf_path, out, dpi=150 if s.get("image") == "photo" else 170, photo=s.get("image") == "photo")
            pdf_path.unlink()
        d = s["data"]
        truth.append({
            "file": s["file"], "tenant_id": s["tenant"], "purpose": s["purpose"],
            "fields": {
                "legal_name": d.get("legal_name"), "dba_name": d.get("dba_name"),
                "tax_classification": d.get("tax_classification"), "address": d.get("address"),
                "city": d.get("city"), "state": d.get("state"), "zip": d.get("zip"),
                "tin": d.get("tin"), "tin_type": d.get("tin_type"),
                "signature_present": None if s.get("w8") else s.get("signed", True),
                "signature_date": d.get("signature_date"),
            },
            "expected": s["expected"],
        })
    (ROOT / "samples" / "ground_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(f"Wrote {len(SCENARIOS)} documents to {DOCS}")


if __name__ == "__main__":
    main()
