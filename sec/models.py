"""Pydantic models for SEC filing data."""

from pydantic import BaseModel
from datetime import date
from typing import Optional


class FilingMeta(BaseModel):
    accession_number: str
    form_type: str
    filed_date: date
    entity_name: str
    cik: str
    description: str = ""


class ParsedFiling(BaseModel):
    accession_number: str
    form_type: str
    filed_date: date
    entity_name: str
    sections: list[dict]   # [{"title": str, "text": str}]
    signal_tags: list[str]


class InsiderTransaction(BaseModel):
    accession_number: str
    filed_date: date
    entity_name: str          # company name
    insider_name: str
    insider_title: str
    transaction_type: str     # "buy" | "sell" | "other"
    transaction_code: str     # raw SEC code e.g. "P", "S", "A", "D"
    shares: float
    price_per_share: Optional[float]
    total_value: Optional[float]
    shares_owned_after: Optional[float]
    is_direct: bool           # direct ownership vs indirect (trust, family etc)
    signal_strength: str      # "strong" | "moderate" | "weak"
