"""Parse a creditor matrix out of a bankruptcy filing PDF.

Measured on a native (post-2020) filing: 211 unique creditors from one
certificate of service, ~1-2% name truncation. Scanned pre-2015 filings do
NOT parse this way -- see docs/CH11_STEP1.md.

Pure functions first (repo convention). pypdf is the only dependency.
"""
import re

STATE = (r"A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]"
         r"|N[CDEHJMVY]|O[HKR]|P[AR]|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY]")

# A matrix row ends "<2+ spaces> CITY ST ZIP". That tail is the anchor:
# everything before it is name + street.
ROW_TAIL = re.compile(rf"\s{{2,}}[A-Z .'\-]+?\s+({STATE})\s+\d{{5}}(?:-\d{{4}})?\b")

# The name ends where the address begins. Address starts with a street number,
# a PO box, or a routing prefix. "ONE" is deliberately NOT a marker here --
# it truncated CAPITAL ONE to CAPITAL in the first measured run.
ADDR_START = re.compile(
    r"\s(?=(?:\d+\s+[A-Z]|P\.?\s?O\.?\s*BOX|POST\s+OFFICE\s+BOX|C/O\b|ATTN\b))",
    re.IGNORECASE)

# Individuals are privacy-redacted in the matrix. Skipping them leaves
# businesses, which is what a counterparty graph needs.
REDACTED = "ADDRESS ON FILE"

BOILERPLATE = re.compile(r"^(Case\s|In re|Doc\s|\d+\s*$|Page\s)", re.IGNORECASE)


def unwrap(text):
    """Rejoin rows broken across lines by the 'UNITED STATES OF AMERICA' tail."""
    text = re.sub(r"UNITED\s*\n\s*STATES OF AMERICA", "USA", text)
    text = re.sub(r"UNITED STATES OF\s*\n\s*AMERICA", "USA", text)
    return text


def creditor_name(line):
    """Extract the creditor name from one matrix line, or None."""
    if len(line.rstrip()) < 8 or REDACTED in line:
        return None
    tail = ROW_TAIL.search(line)
    if not tail:
        return None
    head = line[:tail.start()].strip()
    name = ADDR_START.split(head, maxsplit=1)[0].strip(" ,.")
    if len(name) < 4 or BOILERPLATE.match(name):
        return None
    return name


def normalize(name):
    """Join key for matching one creditor across cases."""
    n = name.upper()
    n = re.sub(r"\b(INCORPORATED|INC|LLC|L\.L\.C|CORPORATION|CORP|COMPANY|CO|"
               r"LIMITED|LTD|LP|LLP|PLC)\b\.?", "", n)
    return re.sub(r"[^A-Z0-9]", "", n)


def parse_text(text):
    """All unique creditor names in an extracted filing."""
    seen, out = set(), []
    for line in unwrap(text).split("\n"):
        name = creditor_name(line)
        if not name:
            continue
        key = normalize(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def parse_pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    return parse_text("\n".join((p.extract_text() or "") for p in reader.pages))


if __name__ == "__main__":
    import sys
    for n in parse_pdf(sys.argv[1]):
        print(n)
