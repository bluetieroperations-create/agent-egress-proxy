"""
Render a signed receipt payload into a human-facing A4 invoice PDF.

No dependencies: this writes a minimal, valid PDF/1.4 by hand using the
built-in Helvetica fonts (which need no embedding), keeping the project's
stdlib-plus-`cryptography` footprint. The PDF is a *projection* of the
signed JSON receipt — the JSON remains the canonical, verifiable artifact;
the PDF is what an accountant files.

Amounts are formatted from integer base units (USDC, 6 decimals). When the
seller entity carries a `vat_rate` (percent, e.g. "19"), a VAT breakdown is
computed with Decimal (never float) treating the quoted amount as
VAT-inclusive; without it, no VAT figures are invented.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# A4 in PostScript points (1/72").
PAGE_W, PAGE_H = 595.28, 841.89
MARGIN = 56.0
USDC_DECIMALS = 6


def _esc(s: str) -> str:
    """Escape a string for a PDF literal string."""
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def usdc(base_units: str) -> str:
    """'5000' -> '0.005000'. Integer-only, no float."""
    try:
        n = int(base_units)
    except (TypeError, ValueError):
        return str(base_units)
    q = Decimal(n).scaleb(-USDC_DECIMALS)
    return f"{q:.6f}"


def vat_breakdown(gross_base: str, vat_rate: str):
    """Return (net, vat, gross) formatted strings treating gross as inclusive,
    or None if vat_rate is unusable."""
    try:
        gross = Decimal(int(gross_base)).scaleb(-USDC_DECIMALS)
        rate = Decimal(vat_rate)
    except Exception:
        return None
    if rate < 0:
        return None
    net = (gross / (1 + rate / 100)).quantize(Decimal("0.000001"), ROUND_HALF_UP)
    vat = (gross - net).quantize(Decimal("0.000001"), ROUND_HALF_UP)
    return f"{net:.6f}", f"{vat:.6f}", f"{gross:.6f}"


class _Canvas:
    """Accumulates content-stream ops in a top-left origin coordinate space
    (y grows downward), translating to PDF's bottom-left origin on emit."""

    def __init__(self):
        self._ops: list[str] = []

    def text(self, x, y, s, size=10, bold=False):
        font = "F2" if bold else "F1"
        self._ops.append(
            f"BT /{font} {size} Tf 1 0 0 1 {x:.2f} {PAGE_H - y:.2f} Tm "
            f"({_esc(s)}) Tj ET"
        )

    def line(self, x0, y0, x1, y1, width=0.6, gray=0.0):
        self._ops.append(
            f"{gray:.2f} G {width:.2f} w {x0:.2f} {PAGE_H - y0:.2f} m "
            f"{x1:.2f} {PAGE_H - y1:.2f} l S"
        )

    def stream(self) -> bytes:
        return "\n".join(self._ops).encode("latin-1", "replace")


def _wrap(s: str, width: int) -> list[str]:
    """Char-count wrap for long monospace-ish values (hex, URLs)."""
    s = str(s)
    return [s[i:i + width] for i in range(0, len(s), width)] or [""]


def _build_pdf(canvas: _Canvas) -> bytes:
    content = canvas.stream()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
         "/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> "
         "/Contents 6 0 R >>").encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        (b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
         + content + b"\nendstream"),
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n"
            "%%EOF\n").encode("latin-1")
    return bytes(out)


def render_invoice(payload: dict, *, verify_url: str | None = None,
                   issuer_kid: str | None = None) -> bytes:
    """Render a receipt payload into invoice PDF bytes."""
    s = payload.get("settlement", {})
    c = payload.get("commerce", {})
    ent = c.get("seller_entity", {}) if isinstance(c.get("seller_entity"), dict) else {}

    cv = _Canvas()
    y = MARGIN

    # -- header --
    cv.text(MARGIN, y, "RECEIPT / INVOICE", size=20, bold=True)
    cv.text(PAGE_W - MARGIN - 180, y - 12, "x402 machine payment", size=9)
    cv.text(PAGE_W - MARGIN - 180, y, f"Receipt no. {payload.get('sequence', '?')}",
            size=10, bold=True)
    y += 18
    cv.line(MARGIN, y, PAGE_W - MARGIN, y, width=1.0)
    y += 22

    # -- id / dates --
    def field(label, value, indent=0, bold_value=False):
        nonlocal y
        cv.text(MARGIN + indent, y, label, size=9, bold=True)
        # 68 chars keeps a 66-char tx hash (0x + 64) on one line at 9pt within
        # the value column; longer values (URLs) wrap.
        for i, chunk in enumerate(_wrap(value, 68)):
            cv.text(MARGIN + indent + 130, y, chunk, size=9, bold=bold_value)
            y += 13
        y += 3

    field("Receipt ID", payload.get("receipt_id", ""))
    field("Issued (UTC)", payload.get("issued_at", ""))
    field("Spec", payload.get("spec", ""))

    y += 6
    cv.text(MARGIN, y, "Seller", size=11, bold=True); y += 16
    field("Seller ID", payload.get("seller_id", ""))
    if ent:
        for key in ("name", "vat_id", "country", "address"):
            if key in ent:
                field(key.replace("_", " ").title(), ent[key])
    else:
        cv.text(MARGIN + 130, y, "(no seller entity details provided)", size=9)
        y += 16

    y += 6
    cv.text(MARGIN, y, "Purchase", size=11, bold=True); y += 16
    field("Resource", c.get("resource", ""))
    field("Description", c.get("description", ""))
    if "request_hash" in c:
        field("Request hash", c["request_hash"])
    if "response_hash" in c:
        field("Response hash", c["response_hash"])

    y += 6
    cv.text(MARGIN, y, "Settlement (on-chain)", size=11, bold=True); y += 16
    field("Network", s.get("chain", ""))
    field("Asset", s.get("asset", "USDC"))
    field("Transaction", s.get("tx_hash", ""))
    field("Payer", s.get("payer", ""))
    field("Payee", s.get("payee", ""))
    field("Verified", f"{s.get('verified')} (method: {s.get('verification_method')})")

    # -- amounts box --
    y += 10
    cv.line(MARGIN, y, PAGE_W - MARGIN, y, width=0.8)
    y += 18
    amount = usdc(s.get("amount_base_units", "0"))
    vr = ent.get("vat_rate") if ent else None
    bd = vat_breakdown(s.get("amount_base_units", "0"), vr) if vr else None
    if bd:
        net, vat, gross = bd
        cv.text(MARGIN, y, "Net", size=10); cv.text(PAGE_W - MARGIN - 140, y,
                f"{net} USDC", size=10); y += 15
        cv.text(MARGIN, y, f"VAT ({vr}%, inclusive)", size=10)
        cv.text(PAGE_W - MARGIN - 140, y, f"{vat} USDC", size=10); y += 15
        cv.text(MARGIN, y, "Total paid", size=11, bold=True)
        cv.text(PAGE_W - MARGIN - 140, y, f"{gross} USDC", size=11, bold=True); y += 18
    else:
        cv.text(MARGIN, y, "Total paid", size=11, bold=True)
        cv.text(PAGE_W - MARGIN - 140, y, f"{amount} USDC", size=11, bold=True); y += 16
        if vr is None and ent:
            cv.text(MARGIN, y, "VAT: not specified by seller", size=8, bold=False)
            y += 14

    # -- verification footer --
    y = PAGE_H - MARGIN - 66
    cv.line(MARGIN, y, PAGE_W - MARGIN, y, width=0.6, gray=0.5)
    y += 14
    cv.text(MARGIN, y, "Cryptographically signed (Ed25519).", size=8, bold=True)
    y += 12
    if issuer_kid:
        cv.text(MARGIN, y, f"Issuer key id: {issuer_kid}", size=8); y += 11
    if verify_url:
        cv.text(MARGIN, y, f"Verify: {verify_url}", size=8); y += 11
    cv.text(MARGIN, y,
            "The signed JSON receipt is the canonical record; this PDF is a projection of it.",
            size=8)

    return _build_pdf(cv)
