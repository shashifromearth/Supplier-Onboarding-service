"""Render docs/img/architecture.png (needs matplotlib; dev-only)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

fig, ax = plt.subplots(figsize=(10, 5.6))
ax.set_xlim(0, 100); ax.set_ylim(0, 56); ax.axis("off")

def box(x, y, w, h, text, fc="#E8F0FE", ec="#1A4B9B", fs=7.5, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.25,rounding_size=0.8", fc=fc, ec=ec, lw=1))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, weight="bold" if bold else "normal")

def arrow(x1, y1, x2, y2):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1), arrowprops=dict(arrowstyle="->", color="#444", lw=1))

# edge + queue
box(2, 47, 22, 6, "PairSoft product\nupload W-9 (tenant T)", "#F3F3F3", "#666")
box(30, 47, 22, 6, "API edge (APIM)\ntenant from token", "#F3F3F3", "#666")
box(58, 47, 18, 6, "Blob + Service Bus\n(session = tenant)", "#F3F3F3", "#666")
box(82, 47, 16, 6, "Event \"w9.decided\"\n→ writer / review", "#F3F3F3", "#666")
arrow(24, 50, 30, 50); arrow(52, 50, 58, 50)

# worker band
ax.add_patch(FancyBboxPatch((1, 16), 98, 26, boxstyle="round,pad=0.2,rounding_size=1", fc="#FFFDF2", ec="#B38600", lw=1.2))
ax.text(3, 40, "W-9 Onboarding Worker (stateless, scale-out, one tenant per job)", fontsize=8, weight="bold", color="#8a6500")
stages = [("1 Ingest\nsniff, hash,\nlimits", 3), ("2 OCR\ntext layer →\nLlamaParse →\nRapidOCR", 17), ("3 Extract\nlayout (det.) +\nGemini→Groq\n(text-only)", 31),
          ("4 Validate\nIRS rules,\nconfidence\ncaps", 45), ("5 Match\nTIN blind index\n+ RapidFuzz", 59), ("6 Decide\nordered rules\n+ reasons", 73), ("7 Respond\nPII-safe JSON,\nTIN → vault", 86)]
for t, x in stages:
    box(x, 22, 12, 14, t, fs=7)
for (_, x) in stages[:-1]:
    arrow(x + 12.3, 29, x + 13.7, 29)
ax.text(50, 18, "every stage emits spans + metrics + audit event (no PII values)", ha="center", fontsize=7, style="italic")
arrow(64, 47, 50, 42)
arrow(92, 36, 90, 47)

# externals
ext = [("LlamaParse\n(sees unredacted doc\nDPA, region)", 9, "#FDECEA", "#B3261E"), ("Gemini → Groq\n(TIN-redacted text only)", 29, "#FDECEA", "#B3261E"),
       ("Policy / config\n+ calibration maps", 49, "#E6F4EA", "#1E7B34"), ("Supplier Master API\n(tenant-scoped)", 67, "#E6F4EA", "#1E7B34"), ("TIN vault\nKey Vault / HSM", 85, "#E6F4EA", "#1E7B34")]
for t, x, fc, ec in ext:
    box(x - 8, 3, 16, 8, t, fc, ec, fs=7)
arrow(23, 22, 13, 11); arrow(37, 22, 33, 11); arrow(51, 22, 53, 11); arrow(65, 22, 70, 11); arrow(92, 22, 88, 11)
ax.text(1, 0.5, "red = third-party processor (leaves our trust boundary)   green = inside our boundary", fontsize=7, color="#444")
plt.savefig("docs/img/architecture.png", dpi=170, bbox_inches="tight")
plt.close()

# ---- decision flow ----
fig, ax = plt.subplots(figsize=(10, 4.6))
ax.set_xlim(0, 100); ax.set_ylim(0, 46); ax.axis("off")
G, R, B = ("#E6F4EA", "#1E7B34"), ("#FDECEA", "#B3261E"), ("#E8F0FE", "#1A4B9B")
def bx(x, y, w, h, t, c=B, fs=7):
    box(x, y, w, h, t, c[0], c[1], fs)
bx(1, 18, 13, 10, "Extracted +\nvalidated W-9\nmatch result")
bx(19, 34, 17, 8, "Unreadable / not W-9\nor blocking flag?", fs=6.8)
bx(19, 18, 17, 10, "TIN fingerprint\nmatches in master?", fs=6.8)
bx(19, 2, 17, 10, "No TIN match:\nbest fuzzy candidate", fs=6.8)
bx(44, 36, 16, 6, "HUMAN_REVIEW\n+ suggested action", R)
bx(44, 24, 16, 8, "1 supplier, name ≥ 85,\nno material change", fs=6.8)
bx(44, 14, 16, 8, "name < 85 / material\nchange / 2+ suppliers", fs=6.8)
bx(44, 2, 16, 10, "different TIN & ≥ 90,\nambiguous, or 80–95\nband", fs=6.8)
bx(68, 24, 17, 8, "UPDATE_EXISTING_\nSUPPLIER", G)
bx(68, 14, 17, 8, "HUMAN_REVIEW\n(conflict codes)", R)
bx(68, 2, 17, 10, "nothing ≥ 80, or\ndifferent TIN < 90\n→ CREATE_NEW_SUPPLIER", G, 6.8)
bx(88, 14, 11, 8, "never a\nsilent merge", ("#FFF8E1", "#B38600"))
arrow(14, 23, 19, 23); arrow(7.5, 28, 19, 38); arrow(14, 21, 19, 8)
arrow(36, 38, 44, 39); arrow(36, 23, 44, 28); arrow(36, 21, 44, 18); arrow(36, 7, 44, 7)
arrow(60, 28, 68, 28); arrow(60, 18, 68, 18); arrow(60, 7, 68, 7)
arrow(85, 18, 88, 18)
ax.text(1, 43, "Decision flow (ordered, first match wins; blocking flags override everything)", fontsize=8, weight="bold")
plt.savefig("docs/img/decision_flow.png", dpi=170, bbox_inches="tight")
