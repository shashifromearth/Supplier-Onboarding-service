"""Render a Markdown doc to PDF with reportlab (dev-only).  usage: python scripts/build_pdf.py docs/DESIGN.md docs/DESIGN.pdf"""
import re, sys
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Image, KeepTogether, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle)

src, out = Path(sys.argv[1]), Path(sys.argv[2])
ss = getSampleStyleSheet()
body = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=9.8, leading=13.5, spaceAfter=6, alignment=TA_LEFT)
h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=19, leading=23, spaceAfter=8, alignment=TA_LEFT)
h2 = ParagraphStyle("h2", parent=ss["Heading1"], fontSize=13, leading=16, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1A4B9B"))
h3 = ParagraphStyle("h3", parent=ss["Heading2"], fontSize=10.5, leading=13, spaceBefore=6, spaceAfter=3)
cell = ParagraphStyle("c", parent=body, fontSize=8.4, leading=10.4, spaceAfter=0)
cellh = ParagraphStyle("ch", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
bul = ParagraphStyle("bu", parent=body, leftIndent=12, bulletIndent=2, spaceAfter=2)
code = ParagraphStyle("code", fontName="Courier", fontSize=7, leading=8.5)

def inline(t):
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"`([^`]+)`", r'<font face="Courier" size="8">\1</font>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", t)
    return t

def table(rows):
    cells = [[r.strip().strip("|") for r in [x]][0].split("|") for x in rows]
    cells = [[c.strip() for c in row] for row in cells]
    hdr, data = cells[0], cells[2:]
    n = len(hdr)
    data = [r + [""] * (n - len(r)) for r in data]
    W = 7.0 * inch
    lens = [max(len(hdr[i]), *(len(r[i]) for r in data)) for i in range(n)]
    w = [max(14, min(l, 120)) ** 0.8 for l in lens]
    tot = sum(w); widths = [W * x / tot for x in w]
    tdata = [[Paragraph(inline(h), cellh) for h in hdr]] + [[Paragraph(inline(c), cell) for c in r] for r in data]
    t = Table(tdata, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A4B9B")), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BBBBBB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7FC")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return [t, Spacer(1, 6)]

lines = src.read_text(encoding="utf-8").splitlines()
story, i, para = [], 0, []
def flush():
    global para
    if para:
        story.append(Paragraph(inline(" ".join(para)), body)); para = []
while i < len(lines):
    ln = lines[i]
    if ln.startswith("```"):
        flush(); j = i + 1; blk = []
        while not lines[j].startswith("```"): blk.append(lines[j]); j += 1
        story += [Preformatted("\n".join(blk), code), Spacer(1, 6)]; i = j + 1; continue
    if ln.startswith("|"):
        flush(); j = i
        while j < len(lines) and lines[j].startswith("|"): j += 1
        story += table(lines[i:j]); i = j; continue
    m = re.match(r"!\[[^\]]*\]\(([^)]+)\)", ln)
    if m:
        flush(); p = src.parent / m.group(1)
        from reportlab.lib.utils import ImageReader
        iw, ih = ImageReader(str(p)).getSize(); w = 7.0 * inch
        story += [Image(str(p), width=w, height=w * ih / iw), Spacer(1, 6)]; i += 1; continue
    if ln.startswith("# "): flush(); story.append(Paragraph(inline(ln[2:]), h1))
    elif ln.startswith("## "): flush(); story.append(Paragraph(inline(ln[3:]), h2))
    elif ln.startswith("### "): flush(); story.append(Paragraph(inline(ln[4:]), h3))
    elif re.match(r"^\s*([-*]|\d+\.)\s+", ln): flush(); story.append(Paragraph(inline(re.sub(r"^\s*([-*]|\d+\.)\s+", "", ln)), bul, bulletText="•"))
    elif not ln.strip(): flush()
    else: para.append(ln.strip())
    i += 1
flush()

def footer(c, d):
    c.saveState(); c.setFont("Helvetica", 7.5); c.setFillColor(colors.grey)
    c.drawString(0.75 * inch, 0.45 * inch, out.stem.replace("_", " ") + " · W-9 Supplier Onboarding · KHM Technology")
    c.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Page {d.page}"); c.restoreState()
SimpleDocTemplate(str(out), pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.7 * inch, bottomMargin=0.75 * inch,
                  title=src.stem, author="Karl, KHM Technology").build(story, onFirstPage=footer, onLaterPages=footer)
