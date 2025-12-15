# app/services/vendor_service.py
import re
from typing import Optional
from sqlalchemy.orm import Session
from app.models_advanced import Vendor


VENDOR_CLEAN_REGEX = re.compile(r"[^a-z0-9]+")


def normalize_vendor_name(raw: str) -> str:
    """
    Turn noisy merchant names like 'UBER *TRIP HELP.UBER.COM' into a
    normalized key like 'uber'.
    """
    if not raw:
        return ""
    lower = raw.strip().lower()
    lower = lower.replace("*", " ").replace("@", " ")
    # remove domain for normalization key (we still keep website in Vendor)
    lower = re.sub(r"\.(com|net|org|io|co|au|uk|de|fr|ca)(/.*)?$", "", lower)
    cleaned = VENDOR_CLEAN_REGEX.sub(" ", lower).strip()
    if not cleaned:
        return ""
    # use first token as primary key, e.g. "spotify"
    return cleaned.split()[0]


def get_or_create_vendor(
    db: Session, raw_name: str, website: Optional[str] = None, support_email: Optional[str] = None
) -> Optional[Vendor]:
    normalized = normalize_vendor_name(raw_name)
    if not normalized:
        return None

    vendor = (
        db.query(Vendor)
        .filter(Vendor.normalized_name == normalized)
        .order_by(Vendor.id.asc())
        .first()
    )
    if vendor:
        # best-effort enrichment
        updated = False
        if website and not vendor.website:
            vendor.website = website
            updated = True
        if support_email and not vendor.support_email:
            vendor.support_email = support_email
            updated = True
        if updated:
            db.add(vendor)
            db.commit()
        return vendor

    vendor = Vendor(
        name=raw_name.strip()[:255],
        normalized_name=normalized,
        website=website,
        support_email=support_email,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor
