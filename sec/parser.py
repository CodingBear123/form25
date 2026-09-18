"""Parse raw SEC filing dicts into structured, signal-ready fields."""

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from sec.models import ParsedFiling, InsiderTransaction
from datetime import date
from typing import Optional

# Keywords mapped to signal tags — extend as needed
_SIGNAL_KEYWORDS: dict[str, list[str]] = {
    "merger":           ["merger", "acquisition", "acquires", "acquired by", "definitive agreement"],
    "ceo_departure":    ["chief executive officer", "ceo", "president", "resign", "step down", "departure"],
    "fda_approval":     ["fda", "food and drug administration", "approved", "approval", "clearance", "510(k)"],
    "clinical_trial":   ["phase 1", "phase 2", "phase 3", "clinical trial", "trial results", "primary endpoint"],
    "insider_buy":      ["purchase", "acquired", "open market purchase"],
    "insider_sell":     ["sale", "sold", "disposed"],
    "revenue_guidance": ["guidance", "outlook", "revenue", "earnings per share", "eps"],
    "bankruptcy":       ["bankruptcy", "chapter 11", "chapter 7", "insolvency", "liquidat"],
    "offering":         ["public offering", "private placement", "follow-on offering", "shelf registration"],
    "dividend":         ["dividend", "special dividend", "distribution"],
}

# Form 4 transaction codes → human readable + buy/sell classification
_TRANSACTION_CODES: dict[str, tuple[str, str]] = {
    "P": ("Open market purchase", "buy"),
    "S": ("Open market sale", "sell"),
    "A": ("Award / grant", "other"),
    "D": ("Disposition to issuer", "sell"),
    "F": ("Tax withholding", "sell"),
    "M": ("Option exercise", "other"),
    "G": ("Gift", "other"),
    "J": ("Other acquisition/disposition", "other"),
    "C": ("Conversion of derivative", "other"),
    "E": ("Expiration of short derivative", "other"),
    "H": ("Expiration of long derivative", "other"),
    "I": ("Discretionary transaction", "other"),
    "L": ("Small acquisition", "buy"),
    "O": ("Exercise out-of-money derivative", "other"),
    "U": ("Tender of shares", "sell"),
    "W": ("Inherited", "other"),
    "X": ("Exercise/conversion of derivative", "other"),
    "Z": ("Voting trust deposit/withdrawal", "other"),
}

# Signal strength rules for insider buys
# Strong: open market purchase (P) by officer/director, meaningful size
# Moderate: award exercise then hold, or smaller open market buy
# Weak: everything else
def _buy_signal_strength(code: str, title: str, shares: float, price: Optional[float]) -> str:
    if code != "P":
        return "weak"
    title_lower = title.lower()
    is_senior = any(t in title_lower for t in ["ceo", "cfo", "president", "director", "chief"])
    value = (shares * price) if price else 0
    if is_senior and value >= 50_000:
        return "strong"
    if value >= 10_000:
        return "moderate"
    return "weak"


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)


def _strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.chunks)


def _split_sections(text: str) -> list[dict]:
    """Split filing text into rough sections on common SEC item headers."""
    pattern = re.compile(r"(Item\s+\d+[A-Z]?\.?\s+[A-Z][^\n]{3,80})", re.IGNORECASE)
    parts = pattern.split(text)
    sections: list[dict] = []
    if len(parts) <= 1:
        return [{"title": "Full Text", "text": text[:5000]}]
    for i in range(1, len(parts) - 1, 2):
        title = parts[i].strip()
        body = parts[i + 1].strip()
        sections.append({"title": title, "text": body[:3000]})
    return sections


def extract_signal_tags(text: str) -> list[str]:
    """Return signal tag strings whose keywords appear in *text*."""
    lower = text.lower()
    return [tag for tag, keywords in _SIGNAL_KEYWORDS.items() if any(kw in lower for kw in keywords)]


def _safe_float(val: Optional[str]) -> Optional[float]:
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


def _xml_text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_form4(raw: dict) -> list[dict]:
    """
    Parse a Form 4 filing (insider transaction) into a list of InsiderTransaction dicts.

    Form 4 XML structure:
      <ownershipDocument>
        <issuer> ... </issuer>
        <reportingOwner> ... </reportingOwner>
        <nonDerivativeTable>
          <nonDerivativeTransaction> ... </nonDerivativeTransaction>
        </nonDerivativeTable>
      </ownershipDocument>

    Returns a list because one Form 4 can contain multiple transactions.
    """
    raw_text = raw.get("raw_text", "")
    filed_date = raw.get("filed_date", "")
    accession_number = raw.get("accession_number", "")
    entity_name = raw.get("entity_name", "")

    # Form 4 is XML — strip any leading HTML envelope if present
    xml_match = re.search(r"<ownershipDocument.*?>.*?</ownershipDocument>", raw_text, re.DOTALL | re.IGNORECASE)
    if not xml_match:
        return []

    try:
        root = ET.fromstring(xml_match.group(0))
    except ET.ParseError:
        return []

    # Reporting owner info
    owner_el = root.find(".//reportingOwner")
    insider_name = _xml_text(owner_el.find(".//rptOwnerName") if owner_el is not None else None)
    insider_title = _xml_text(owner_el.find(".//officerTitle") if owner_el is not None else None)
    is_director = _xml_text(owner_el.find(".//isDirector") if owner_el is not None else None) == "1"
    is_officer = _xml_text(owner_el.find(".//isOfficer") if owner_el is not None else None) == "1"

    if not insider_title:
        insider_title = "Director" if is_director else ("Officer" if is_officer else "Other")

    transactions: list[dict] = []

    for txn_el in root.findall(".//nonDerivativeTransaction"):
        code_el = txn_el.find(".//transactionCode")
        code = _xml_text(code_el).upper() if code_el is not None else ""
        _, txn_type = _TRANSACTION_CODES.get(code, ("Unknown", "other"))

        shares_el = txn_el.find(".//transactionShares/value")
        price_el = txn_el.find(".//transactionPricePerShare/value")
        owned_el = txn_el.find(".//sharesOwnedFollowingTransaction/value")
        direct_el = txn_el.find(".//directOrIndirectOwnership/value")

        shares = _safe_float(_xml_text(shares_el)) or 0.0
        price = _safe_float(_xml_text(price_el))
        owned_after = _safe_float(_xml_text(owned_el))
        is_direct = _xml_text(direct_el).upper() == "D"
        total_value = (shares * price) if price else None

        signal_strength = _buy_signal_strength(code, insider_title, shares, price) if txn_type == "buy" else "weak"

        transactions.append(
            InsiderTransaction(
                accession_number=accession_number,
                filed_date=date.fromisoformat(filed_date),
                entity_name=entity_name,
                insider_name=insider_name,
                insider_title=insider_title,
                transaction_type=txn_type,
                transaction_code=code,
                shares=shares,
                price_per_share=price,
                total_value=total_value,
                shares_owned_after=owned_after,
                is_direct=is_direct,
                signal_strength=signal_strength,
            ).model_dump(mode="json")
        )

    return transactions


def parse_filing(raw: dict) -> dict:
    """
    Parse a raw filing dict (from edgar.fetch_filings) into structured fields.

    Returns a ParsedFiling-shaped dict with sections and signal_tags.
    """
    raw_text = raw.get("raw_text", "")
    plain = _strip_html(raw_text) if re.search(r"<[a-z][\s\S]*?>", raw_text) else raw_text

    sections = _split_sections(plain)
    signal_tags = extract_signal_tags(plain)

    return ParsedFiling(
        accession_number=raw["accession_number"],
        form_type=raw["form_type"],
        filed_date=date.fromisoformat(raw["filed_date"]),
        entity_name=raw["entity_name"],
        sections=sections,
        signal_tags=signal_tags,
    ).model_dump(mode="json")
