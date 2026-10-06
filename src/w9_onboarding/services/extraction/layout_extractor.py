"""Deterministic W-9 reader: anchor-based text fields + pixel analysis for checkboxes and signature ink.

Free, fast and explainable on known layouts; brittle on unseen ones, which is why a grounded LLM reads
the same page as a second opinion (see extractor.HybridExtractor). Needs line-level positions (local OCR,
PDF text layer, or LlamaParse + local geometry).
"""
from __future__ import annotations

import re
from typing import Optional

from PIL import Image
from rapidfuzz import fuzz

from w9_onboarding.domain.models import ExtractedW9, FieldCandidate, OcrLine, OcrResult
from w9_onboarding.utils.normalize import find_date, fix_ocr_digits, parse_date, respace, split_city_state_zip, squash, tin_from_text

# ---------------------------------------------------------------------------
# Form type detection (shared)
# ---------------------------------------------------------------------------
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"


def detect_form(ocr: OcrResult) -> tuple[FieldCandidate, FieldCandidate]:
    img_h = ocr.page_images[0].height if ocr.page_images else 10_000
    header = [l for l in ocr.lines if l.bbox[1] < img_h * 0.15]
    blob = squash(" ".join(l.text for l in header))
    conf = sum(l.confidence for l in header) / len(header) if header else 0.0
    if re.search(r"formw8(ben|bene|eci|exp|imy)?", blob) or "certificateofforeignstatus" in blob:
        sub = re.search(r"formw8(ben|bene|eci|exp|imy)?", blob)
        ft = FieldCandidate(value=f"W-8{(sub.group(1) or '').upper()}" if sub else "W-8", confidence=conf, source="layout:header")
    elif "formw9" in blob or "requestfortaxpayer" in blob:
        ft = FieldCandidate(value="W-9", confidence=conf, source="layout:header")
    else:
        ft = FieldCandidate(value="UNKNOWN", confidence=0.3, source="layout:header")
    rev = re.search(rf"rev({_MONTHS})(\d{{4}})", blob)
    rv = FieldCandidate(value=f"{rev.group(2)}-{rev.group(1)[:3]}" if rev else None,
                        confidence=conf if rev else 0.0, source="layout:header")
    return ft, rv


# ---------------------------------------------------------------------------
# Layout (deterministic) extractor
# ---------------------------------------------------------------------------
ANCHORS = {
    "legal": ["nameofentityindividual", "nameasshownonyourincometaxreturn"],
    "dba": ["businessnamedisregardedentityname"],
    "class": ["checktheappropriatebox", "checkappropriatebox"],
    "address": ["addressnumberstreetandaptorsuiteno"],
    "requester": ["requestersnameandaddress"],
    "csz": ["citystateandzipcode"],
    "account": ["listaccountnumbers"],
    "ssn": ["socialsecuritynumber"],
    "ein": ["employeridentificationnumber"],
    "part2": ["underpenaltiesofperjury"],
    "sign": ["signatureofus"],
}
CLASS_OPTIONS = [
    ("INDIVIDUAL_SOLE_PROPRIETOR", "individualsoleproprietor"),
    ("C_CORPORATION", "ccorporation"),
    ("S_CORPORATION", "scorporation"),
    ("PARTNERSHIP", "partnership"),
    ("TRUST_ESTATE", "trustestate"),
    ("LLC", "llcenterthetaxclassification"),
    ("OTHER", "otherseeinstructions"),
]
SSN_RE = re.compile(r"(?<!\d)(\d{3})-?(\d{2})-?(\d{4})(?!\d)")
EIN_RE = re.compile(r"(?<!\d)(\d{2})-?(\d{7})(?!\d)")


class LayoutExtractor:
    name = "layout-v1"

    def __init__(self, calibrated: bool = True) -> None:
        self.calibrated = calibrated

    def extract(self, ocr: OcrResult) -> ExtractedW9:
        out = self._extract(ocr)
        if self.calibrated:
            from w9_onboarding.services.calibration import calibrate
            out = calibrate(out, ocr.provider)
        return out

    def _extract(self, ocr: OcrResult) -> ExtractedW9:
        out = ExtractedW9(extractor=self.name)
        out.form_type, out.form_revision = detect_form(ocr)
        if out.form_type.value != "W-9":
            return out  # do not try to read W-9 fields off a different form
        img = ocr.page_images[0]
        self.lines = [l for l in ocr.lines if l.page == 1]
        self.pt = img.width / 612.0  # pixels per PDF point (letter width); assumes page fills image
        self._blob = ocr.full_text
        a = {k: self._anchor(v) for k, v in ANCHORS.items()}

        def y0(key: str, default: float) -> float:
            return a[key].bbox[1] if a[key] else default

        x_split = a["requester"].bbox[0] - 4 if a["requester"] else img.width * 0.62

        out.legal_name = self._value_below(a["legal"], y0("dba", 1e9), x_max=img.width, kind="name")
        out.dba_name = self._value_below(a["dba"], y0("class", 1e9), x_max=img.width, kind="name", optional=True)
        out.address = self._value_below(a["address"], y0("csz", 1e9), x_max=x_split, kind="text")
        csz = self._value_below(a["csz"], y0("account", 1e9), x_max=x_split, kind="text")
        if not csz.value or not split_city_state_zip(csz.value)[0]:
            csz = self._find_csz_line(a, x_split) or csz
        if csz.value:
            city, state, zp = split_city_state_zip(csz.value)
            if city:
                for f, v in (("city", city), ("state", state), ("zip", zp)):
                    setattr(out, f, FieldCandidate(value=v, confidence=csz.confidence, source=csz.source,
                                                   evidence=csz.evidence, notes=list(csz.notes)))
            else:
                bad = FieldCandidate(value=None, confidence=0.2, source=csz.source, evidence=csz.evidence,
                                     notes=["city/state/zip line could not be parsed"])
                out.city, out.state, out.zip = bad, bad.model_copy(), bad.model_copy()
        self._tin(out, a, img)
        out.tax_classification = self._classification(a, img)
        out.signature_present, out.signature_date = self._signature(a, img)
        return out

    # -- helpers -------------------------------------------------------------
    def _anchor(self, keys: list[str], max_len_ratio: float = 1.8) -> Optional[OcrLine]:
        best, best_score = None, 0.0
        for l in self.lines:
            s = squash(l.text)
            for k in keys:
                if len(s) < len(k) * 0.6:
                    continue
                score = fuzz.partial_ratio(k, s)
                # Penalise long lines that merely *contain* the phrase (e.g. instructions text).
                if len(s) > len(k) * max_len_ratio + 40:
                    score -= 15
                if score > best_score:
                    best, best_score = l, score
        return best if best_score >= 80 else None

    def _find_csz_line(self, a: dict, x_max: float) -> Optional[FieldCandidate]:
        """Fallback when the 'City, state, and ZIP code' label is unreadable: find a line shaped like one."""
        top = a["address"].bbox[3] if a["address"] else 0
        bottom = a["account"].bbox[1] if a["account"] else 1e9
        for l in sorted(self.lines, key=lambda l: l.bbox[1]):
            if top < l.bbox[1] < bottom and l.bbox[0] < x_max:
                text, changed = respace(l.text)
                if split_city_state_zip(text)[0]:
                    return FieldCandidate(value=text, confidence=round(l.confidence * 0.9, 3), source="layout:csz-pattern",
                                          evidence=l.text, notes=["label not found; matched by City, ST ZIP pattern"])
        return None

    def _is_label(self, l: OcrLine) -> bool:
        s = squash(l.text)
        return any(fuzz.partial_ratio(k, s) >= 85 for ks in ANCHORS.values() for k in ks if len(s) >= len(k) * 0.6)

    def _value_below(self, anchor: Optional[OcrLine], stop_y: float, x_max: float, kind: str,
                     optional: bool = False) -> FieldCandidate:
        if anchor is None:
            return FieldCandidate(value=None, confidence=0.0, source="layout", notes=["label not found"])
        line_h = anchor.bbox[3] - anchor.bbox[1]
        cands = [l for l in self.lines
                 if l.bbox[1] > anchor.bbox[1] + line_h * 0.6 and l.bbox[1] < stop_y - 2
                 and l.bbox[0] < x_max and not self._is_label(l)]
        cands.sort(key=lambda l: l.bbox[1])
        if not cands:
            # Empty is a legitimate answer for optional fields (high confidence that it is blank).
            return FieldCandidate(value=None, confidence=0.9 if optional else 0.0, source="layout",
                                  notes=[] if optional else ["no value under label"])
        l = cands[0]
        text, changed = respace(l.text)
        text = re.sub(r"^[^\w]+", "", text).strip()  # strip stray glyphs from box borders
        conf = l.confidence * (0.9 if changed else 1.0)
        notes = ["OCR spacing repaired"] if changed else []
        return FieldCandidate(value=text or None, confidence=round(conf, 3), source="layout:value-below-label",
                              evidence=l.text, notes=notes)

    def _tin(self, out: ExtractedW9, a: dict, img: Image.Image) -> None:
        top = (a["ssn"] or a["ein"]).bbox[1] - 10 if (a["ssn"] or a["ein"]) else img.height * 0.4
        bottom = a["part2"].bbox[1] if a["part2"] else img.height * 0.65
        found: list[tuple[str, str, OcrLine, bool]] = []
        for l in self.lines:
            if not (top <= l.bbox[1] <= bottom) or self._is_label(l):
                continue
            raw = l.text.replace(" ", "")
            fixed, changed = fix_ocr_digits(raw)
            if not re.search(r"\d", raw):
                continue
            digits = re.sub(r"\D", "", fixed)
            if len(digits) != 9:
                continue
            # Which box is it in? nearest label above it.
            def dist(anc: Optional[OcrLine]) -> float:
                return l.bbox[1] - anc.bbox[1] if anc and l.bbox[1] >= anc.bbox[1] else 1e9
            box = "SSN" if dist(a["ssn"]) < dist(a["ein"]) else "EIN"
            shape = "SSN" if SSN_RE.fullmatch(fixed) and "-" in fixed else ("EIN" if EIN_RE.fullmatch(fixed) and "-" in fixed else box)
            found.append((digits, box, l, changed or box != shape))
        if not found:
            hit = tin_from_text(self._blob)    # positional read failed (e.g. parser merged label and value)
            if hit:
                out.tin = FieldCandidate(value=hit[0], confidence=hit[2], source="text:tin-pattern", notes=hit[3])
                out.tin_type = FieldCandidate(value=hit[1], confidence=hit[2], source="text:tin-pattern")
                return
            out.tin = FieldCandidate(value=None, confidence=0.0, source="layout:tin", notes=["no 9-digit TIN found in Part I"])
            out.tin_type = FieldCandidate(value=None, confidence=0.0, source="layout:tin")
            return
        notes = []
        if len({f[0] for f in found}) > 1:
            notes.append("both SSN and EIN boxes contain a number")
        digits, ttype, line, penal = found[0]
        if penal:
            notes.append("OCR digit substitution or dash pattern did not match box")
        conf = line.confidence * (0.85 if penal else 1.0) * (0.6 if len(found) > 1 else 1.0)
        out.tin = FieldCandidate(value=digits, confidence=round(conf, 3), source="layout:tin-box", evidence=None, notes=notes)
        out.tin_type = FieldCandidate(value=ttype, confidence=round(conf, 3), source="layout:tin-box")

    # -- pixel analysis --------------------------------------------------------
    def _dark(self, img: Image.Image, box: tuple[float, float, float, float], thresh: int = 120):
        x0, y0, x1, y1 = (int(max(0, v)) for v in box)
        crop = img.crop((x0, y0, max(x0 + 1, int(x1)), max(y0 + 1, int(y1)))).convert("L")
        w, h = crop.size
        px = crop.load()
        return [[px[x, y] < thresh for x in range(w)] for y in range(h)]

    def _checkbox_fill(self, img: Image.Image, label: OcrLine, label_x: float) -> tuple[float, bool]:
        """Find the square left of / at the start of a label and measure ink inside it.

        Returns (interior ink ratio, box_found). Looks for two vertical edges ~8pt apart.
        """
        pt = self.pt
        yc = (label.bbox[1] + label.bbox[3]) / 2
        win = (label_x - 20 * pt, yc - 8 * pt, label_x + 12 * pt, yc + 8 * pt)
        g = self._dark(img, win)
        if not g:
            return 0.0, False
        h, w = len(g), len(g[0])
        col_run = []
        for x in range(w):
            best = run = 0
            for y in range(h):
                run = run + 1 if g[y][x] else 0
                best = max(best, run)
            col_run.append(best)
        edges = [x for x in range(w) if col_run[x] >= 6.3 * pt]
        side = 8 * pt
        pair = None
        for i, xl in enumerate(edges):
            for xr in edges[i + 1:]:
                if 0.75 * side <= xr - xl <= 1.3 * side:
                    pair = (xl, xr)
                    break
            if pair:
                break
        if not pair:
            return 0.0, False
        xl, xr = pair
        rows = [y for y in range(h) if sum(g[y][xl:xr + 1]) >= 0.7 * (xr - xl)]
        if len(rows) < 2:
            return 0.0, False
        yt, yb = min(rows), max(rows)
        inset = max(3, int(1.8 * pt))
        inner = [g[y][x] for y in range(yt + inset, yb - inset + 1) for x in range(xl + inset, xr - inset + 1)]
        return (sum(inner) / len(inner) if inner else 0.0), True

    def _classification(self, a: dict, img: Image.Image) -> FieldCandidate:
        region_top = a["class"].bbox[3] if a["class"] else 0
        region_bot = (a["address"].bbox[1] if a["address"] else img.height * 0.4)
        region = [l for l in self.lines if region_top - 2 <= l.bbox[1] <= region_bot]
        results = []
        for code, key in CLASS_OPTIONS:
            best = None
            for l in region:  # pass 1: exact substring (shortest line wins -> the option label itself)
                s = squash(l.text)
                idx = s.find(key)
                if idx >= 0 and (best is None or len(s) < len(squash(best[0].text))):
                    best = (l, idx, s)
            if best is None:  # pass 2: tolerate OCR typos in the label, but never 1-char-off siblings
                for l in region:
                    s = squash(l.text)
                    if len(s) <= len(key) + 4 and fuzz.ratio(key, s) >= 88 and not any(
                            k2 != key and k2 in s for _, k2 in CLASS_OPTIONS):
                        best = (l, 0, s)
                        break
            if best is None:
                continue
            l, idx, s = best
            # x position of the option inside a (possibly merged) OCR line, by character ratio
            label_x = l.bbox[0] + (l.bbox[2] - l.bbox[0]) * (idx / max(1, len(s)))
            raw = l.text.lstrip()
            if idx == 0 and raw and not raw[0].isalnum():  # OCR swallowed the box glyph into the line
                label_x = l.bbox[0] + 12 * self.pt
            fill, found = self._checkbox_fill(img, l, label_x)
            results.append((code, fill, found, l))
        if not results:
            return FieldCandidate(value=None, confidence=0.0, source="layout:checkbox", notes=["classification options not found"])
        fills = sorted(r[1] for r in results)
        baseline = fills[len(fills) // 2]  # most boxes are empty: the median is the "empty" level
        checked = [r for r in results if r[1] >= 0.15 and r[1] - baseline >= 0.12]
        notes = [f"{sum(1 for r in results if r[2])}/{len(results)} checkboxes located"]
        if not checked:
            return FieldCandidate(value=None, confidence=0.8, source="layout:checkbox", notes=notes + ["no box checked"])
        if len(checked) > 1:
            return FieldCandidate(value="MULTIPLE", confidence=0.8, source="layout:checkbox",
                                  notes=notes + ["multiple boxes checked: " + ",".join(c[0] for c in checked)])
        code, fill, _, line = checked[0]
        empties = [r[1] for r in results if r is not checked[0]]
        margin = fill - (max(empties) if empties else 0.0)
        conf = min(line.confidence, 0.6 + min(0.39, margin * 2))
        if code == "LLC":
            letter = None
            for l in self.lines:
                if abs((l.bbox[1] + l.bbox[3]) / 2 - (line.bbox[1] + line.bbox[3]) / 2) < 8 * self.pt and l.bbox[0] > line.bbox[2]:
                    t = l.text.strip().upper()
                    if t in ("C", "S", "P"):
                        letter = t
            if letter is None:
                # OCR engines often drop isolated single characters: re-OCR just that region, upscaled.
                letter, score = self._reocr_letter(img, line)
                if letter:
                    notes.append(f"LLC letter recovered by targeted single-glyph re-OCR (score {score:.2f})")
                    conf *= 0.80  # weak evidence: below the critical threshold -> human confirms
            code = f"LLC_{letter}" if letter else "LLC_UNSPECIFIED"
        return FieldCandidate(value=code, confidence=round(conf, 3), source="layout:checkbox-ink", notes=notes)

    def _reocr_letter(self, img: Image.Image, line: OcrLine) -> tuple[Optional[str], float]:
        """Recover the single LLC letter (C/S/P) that page-level OCR dropped.

        Find the ink blob right of the LLC label, crop tightly and run the recogniser only.
        Single-glyph recognition is weak evidence, so the caller discounts confidence.
        """
        try:
            import numpy as np
            from w9_onboarding.infrastructure.ocr import _rapid_engine_get
        except ImportError:
            return None, 0.0
        engine = _rapid_engine_get()
        if engine is None:
            return None, 0.0
        pt = self.pt
        crop = img.crop((int(line.bbox[2] + 2 * pt), int(line.bbox[1] - 4 * pt),
                         int(min(img.width, line.bbox[2] + 150 * pt)), int(line.bbox[3] + 4 * pt))).convert("L")
        a = np.array(crop)
        ys, xs = np.where(a < 120)
        if len(xs) < 5:
            return None, 0.0
        pad = 4
        t = crop.crop((max(0, xs.min() - pad), max(0, ys.min() - pad), xs.max() + pad + 1, ys.max() + pad + 1))
        t = t.resize((max(1, int(t.width * 32 / t.height)), 32), Image.LANCZOS).convert("RGB")
        res = engine.text_recognizer([np.array(t)])[0]
        if res:
            text, score = res[0]
            letter = re.sub(r"[^A-Za-z]", "", text).upper()
            if letter in ("C", "S", "P"):
                return letter, float(score)
        return None, 0.0

    def _signature(self, a: dict, img: Image.Image) -> tuple[FieldCandidate, FieldCandidate]:
        sign = a["sign"]
        if sign is None:
            return (FieldCandidate(value=None, confidence=0.0, source="layout:signature", notes=["signature row not found"]),
                    FieldCandidate(value=None, confidence=0.0, source="layout:signature"))
        pt = self.pt
        yc = (sign.bbox[1] + sign.bbox[3]) / 2
        row = [l for l in self.lines if abs((l.bbox[1] + l.bbox[3]) / 2 - yc) < 10 * pt and l is not sign]
        date_line = next((l for l in sorted(row, key=lambda l: l.bbox[0]) if "date" in squash(l.text)[:6]), None)
        # Merged text layer: 'Date Sept 2, 2026' is one line; separate OCR boxes: 'Date' + '09/17/2026'
        x_end = date_line.bbox[0] - 4 * pt if date_line else sign.bbox[2] + 200 * pt
        # Signature of U.S. person label may be merged with 'Sign Here'; its right edge is the start of ink area
        sig_box = (sign.bbox[2] + 4 * pt, yc - 16 * pt, x_end, yc + 3 * pt)
        g = self._dark(img, sig_box)
        # drop ruled lines (rows that are mostly dark) - the signature underline is not a signature
        kept = [r for r in g if r and sum(r) < 0.5 * len(r)]
        ink = sum(sum(r) for r in kept) / max(1, sum(len(r) for r in kept))
        if ink >= 0.012:
            present = FieldCandidate(value=True, confidence=round(min(0.97, 0.75 + ink * 8), 3), source="layout:signature-ink",
                                     notes=[f"ink ratio {ink:.3f}"])
        elif ink <= 0.003:
            present = FieldCandidate(value=False, confidence=0.93, source="layout:signature-ink", notes=[f"ink ratio {ink:.3f}"])
        else:
            present = FieldCandidate(value=ink >= 0.007, confidence=0.55, source="layout:signature-ink",
                                     notes=[f"ambiguous ink ratio {ink:.3f}"])
        date_txt, dl = None, None
        for l in sorted(row, key=lambda l: l.bbox[0]):
            if l.bbox[0] < sign.bbox[2]:
                continue
            t, _ = respace(l.text)
            found = find_date(t)
            if found:
                date_txt, dl = found, l
                break
        if not date_txt:
            return present, FieldCandidate(value=None, confidence=0.85, source="layout:date", notes=["no date next to signature"])
        d = parse_date(date_txt)
        if d is None:
            return present, FieldCandidate(value=None, confidence=0.3, source="layout:date", evidence=date_txt,
                                           notes=[f"unparseable date '{date_txt}'"])
        return present, FieldCandidate(value=d.isoformat(), confidence=round(dl.confidence, 3), source="layout:date", evidence=date_txt)
