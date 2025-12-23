from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction, Subscription, SubscriptionStatus

# -----------------------------
# Vendor + amount normalization
# -----------------------------

_NOISE_TOKENS = {
    "payment", "payments", "purchase", "purchases", "receipt", "invoice", "order",
    "confirm", "confirmation", "subscription", "subs", "billing", "bill", "charges",
}

_SEPARATORS = ["•", "·", "|", "/", "\\", ",", ";", "—", "-", "_", ":", "(", ")", "[", "]", "{", "}", "*"]


def _normalize_vendor(v: str) -> str:
    """
    Normalize vendor strings so recurring charges group together.
    Keep this conservative (avoid over-merging).
    """
    s = (v or "").strip().lower()
    if not s:
        return ""

    # Common separators / cruft
    for ch in _SEPARATORS:
        s = s.replace(ch, " ")

    # Collapse whitespace + remove generic noise tokens
    parts = [p for p in s.split() if p and p not in _NOISE_TOKENS]

    # Drop trailing digits (often card suffix)
    while parts and parts[-1].isdigit():
        parts.pop()

    # Cap length to prevent runaway keys
    key = " ".join(parts[:6]).strip()
    return key or s


def _amount_to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _median(nums: list[float]) -> float | None:
    if not nums:
        return None
    s = sorted(nums)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


_MIN_EVIDENCE_CONFIDENCE = 0.5


def _meets_confidence(tx: Transaction, key: str, minimum: float | None) -> bool:
    if minimum is None:
        return True
    confidence = getattr(tx, "confidence", None)
    if not isinstance(confidence, dict):
        return True
    value = confidence.get(key)
    if value is None:
        return True
    try:
        return float(value) >= minimum
    except (TypeError, ValueError):
        return True


def _cluster_by_amount(
    items: list[Transaction],
    *,
    abs_tol: float = 1.0,
    pct_tol: float = 0.05,
) -> list[list[Transaction]]:
    """
    Split a vendor group into multiple clusters so we can detect multiple subs
    under the same vendor (e.g., different tiers/add-ons).
    """
    with_amount = [(t, _amount_to_float(getattr(t, "amount", None))) for t in items]
    with_amount = [(t, a) for (t, a) in with_amount if a is not None]

    # If we don't have amounts, treat as one cluster
    if not with_amount:
        return [items]

    # Sort by amount and cluster by tolerance
    with_amount.sort(key=lambda x: x[1])
    clusters: list[list[Transaction]] = []
    cur: list[Transaction] = []
    cur_center: float | None = None

    for t, a in with_amount:
        if cur_center is None:
            cur = [t]
            cur_center = a
            continue

        tol = max(abs_tol, abs(cur_center) * pct_tol)
        if abs(a - cur_center) <= tol:
            cur.append(t)
            # update center (running mean)
            cur_center = (cur_center * (len(cur) - 1) + a) / len(cur)
        else:
            clusters.append(cur)
            cur = [t]
            cur_center = a

    if cur:
        clusters.append(cur)

    # Add any items without amount to the largest cluster (best effort)
    no_amount = [t for (t, a) in [(t, _amount_to_float(getattr(t, "amount", None))) for t in items] if a is None]
    if no_amount:
        if clusters:
            largest = max(clusters, key=len)
            largest.extend(no_amount)
        else:
            clusters = [no_amount]

    return clusters


# -----------------------------
# Subscription inference helpers
# -----------------------------

def _pick_display_vendor(items: Iterable[Transaction]) -> str | None:
    names = [getattr(t, "vendor", None) for t in items if getattr(t, "vendor", None)]
    if not names:
        return None
    return Counter(names).most_common(1)[0][0]


def _date_list(items: Iterable[Transaction]):
    return sorted({getattr(t, "transaction_date", None) for t in items if getattr(t, "transaction_date", None)})


def _median_gap_days(dates) -> int | None:
    if len(dates) < 2:
        return None
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    return sorted(gaps)[len(gaps) // 2]


def _gap_variability_days(dates, median_gap: int) -> int | None:
    if len(dates) < 3 or not median_gap:
        return None
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    dev = [abs(g - median_gap) for g in gaps]
    return sorted(dev)[len(dev) // 2]


def _gap_skipped_cycles(dates, median_gap: int) -> int:
    if len(dates) < 2 or not median_gap:
        return 0
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return 0
    threshold = int(median_gap * 1.5)
    return sum(1 for g in gaps if g > threshold)


def _amount_variability(amounts: list[float]) -> float | None:
    if len(amounts) < 2:
        return None
    med = _median(amounts)
    if med is None:
        return None
    dev = [abs(a - med) for a in amounts]
    if not dev:
        return None
    return float(sorted(dev)[len(dev) // 2])


def _roll_forward(date_val, gap_days: int, *, today):
    """
    If last+gap is in the past (missed cycles), roll forward a few cycles.
    """
    if not date_val or not gap_days:
        return None
    d = date_val
    # limit roll to avoid infinite loops on bad inputs
    for _ in range(0, 24):
        if d >= today:
            return d
        d = d + timedelta(days=gap_days)
    return d


def _confidence_and_reasons(
    *,
    dates,
    median_gap: int | None,
    variability: int | None,
    amount_variability: float | None,
    skipped_cycles: int,
    flagged_count: int,
    last_date,
    amount_median: float | None,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    n = len(dates)
    if n >= 3:
        score += 0.45
        reasons.append(f"Found {n} charges for this vendor.")
    elif n == 2:
        score += 0.25
        reasons.append("Found 2 charges for this vendor.")
    else:
        reasons.append("Only 1 charge found (lower confidence).")

    if median_gap:
        score += 0.15
        reasons.append(f"Median interval between charges is ~{median_gap} days.")
        if variability is not None:
            if variability <= 3:
                score += 0.15
                reasons.append("Charge interval is consistent (low variance).")
            elif variability <= 7:
                score += 0.08
                reasons.append("Charge interval is moderately consistent.")
            else:
                reasons.append("Charge interval varies a lot (lower confidence).")

    if flagged_count > 0:
        score += 0.15
        reasons.append("At least one email/transaction was flagged as subscription/trial/renewal.")

    if last_date:
        score += 0.10
        reasons.append(f"Most recent charge on {last_date.isoformat()}.")

    if amount_median is not None:
        score += 0.05
        reasons.append("Charge amount is available.")
        if amount_variability is not None:
            if amount_variability <= max(0.5, amount_median * 0.03):
                score += 0.05
                reasons.append("Charge amounts are consistent.")
            else:
                reasons.append("Charge amounts vary across cycles.")

    if skipped_cycles > 0:
        reasons.append(f"{skipped_cycles} longer-than-usual gap(s) in charges detected.")

    # clamp
    score = max(0.0, min(1.0, score))
    return score, reasons


def recompute_subscriptions(db: Session, *, user_id: int) -> None:
    """
    Rebuild subscriptions for a user.

    Goals:
    - Preserve user-ignored subscriptions (do not delete them).
    - Detect recurring subscriptions by vendor + amount cluster.
    - Use flagged transactions (is_subscription / trial_end_date / renewal_date) as strong evidence,
      but still compute cadence when possible.
    - Store explainability in Subscription.meta (confidence, reasons, evidence transaction ids).
    """
    now = datetime.now(timezone.utc).date()

    # Fetch all transactions for this user (latest first)
    txs = (
        db.execute(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.transaction_date.desc().nullslast())
        )
        .scalars()
        .all()
    )

    # Preserve ignored subscriptions (so ignore survives recompute)
    ignored = (
        db.query(Subscription)
        .filter(Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.ignored)
        .all()
    )
    ignored_keys: set[tuple[str, float | None, str | None]] = set()
    for s in ignored:
        vkey = _normalize_vendor(getattr(s, "vendor_name", "") or "")
        amt = _amount_to_float(getattr(s, "amount", None))
        ignored_keys.add((vkey, amt, getattr(s, "currency", None)))

    # Group transactions by normalized vendor
    vendor_groups: dict[tuple[str, str | None], list[Transaction]] = defaultdict(list)
    for tx in txs:
        v = getattr(tx, "vendor", None)
        d = getattr(tx, "transaction_date", None)
        if not v or not d:
            continue
        currency = getattr(tx, "currency", None)
        vendor_groups[(_normalize_vendor(v), currency)].append(tx)

    # Delete old subscriptions except ignored
    before = db.query(Subscription).filter(Subscription.user_id == user_id).count()
    db.query(Subscription).filter(
        Subscription.user_id == user_id,
        Subscription.status != SubscriptionStatus.ignored,
    ).delete(synchronize_session=False)

    created = 0

    for (vendor_key, vendor_currency), items in vendor_groups.items():
        if not vendor_key:
            continue

        # Split into multiple subs if needed
        clusters = _cluster_by_amount(items)

        for cluster_items in clusters:
            # Determine median amount (for ignore matching + display)
            amounts = [_amount_to_float(getattr(t, "amount", None)) for t in cluster_items]
            amounts = [a for a in amounts if a is not None]
            amount_median = _median(amounts)

            # Skip if user previously ignored this vendor+amount
            if (vendor_key, amount_median, vendor_currency) in ignored_keys:
                continue

            dates = _date_list(cluster_items)
            if not dates:
                continue

            # Flagged evidence from extraction/LLM
            flagged = [
                t for t in cluster_items
                if getattr(t, "is_subscription", False)
                or getattr(t, "trial_end_date", None)
                or getattr(t, "renewal_date", None)
            ]

            amount_evidence = any(
                _amount_to_float(getattr(t, "amount", None)) is not None
                and _meets_confidence(t, "amount", _MIN_EVIDENCE_CONFIDENCE)
                for t in cluster_items
            )
            trial_evidence = any(
                getattr(t, "trial_end_date", None)
                and _meets_confidence(t, "date", _MIN_EVIDENCE_CONFIDENCE)
                for t in cluster_items
            )
            renewal_evidence = any(
                getattr(t, "renewal_date", None)
                and _meets_confidence(t, "date", _MIN_EVIDENCE_CONFIDENCE)
                for t in cluster_items
            )
            concrete_evidence = amount_evidence or trial_evidence or renewal_evidence

            # Cadence inference (if possible)
            median_gap = _median_gap_days(dates)
            if median_gap is not None and (median_gap < 7 or median_gap > 400):
                # Too weird to treat as recurring
                median_gap = None

            if len(dates) == 1:
                if not (trial_evidence or renewal_evidence):
                    continue
            elif median_gap is None:
                if not concrete_evidence:
                    continue

            variability = _gap_variability_days(dates, median_gap) if median_gap else None
            skipped_cycles = _gap_skipped_cycles(dates, median_gap) if median_gap else 0

            last_date = max(dates)

            # Choose next renewal:
            # 1) explicit renewal_date (prefer latest in the future)
            explicit_renewals = sorted(
                {t.renewal_date for t in flagged if getattr(t, "renewal_date", None)},
                reverse=True,
            )
            next_renewal = next((d for d in explicit_renewals if d >= now), None)

            # 2) trial_end_date (prefer latest in the future)
            trial_dates = sorted(
                {t.trial_end_date for t in flagged if getattr(t, "trial_end_date", None)},
                reverse=True,
            )
            trial_end = next((d for d in trial_dates if d >= now), None)

            predicted_is_estimated = False

            # 3) cadence prediction
            if next_renewal is None and median_gap is not None:
                predicted_is_estimated = True
                next_renewal = _roll_forward(last_date + timedelta(days=median_gap), median_gap, today=now)

            if next_renewal is None:
                # If we don't have a next date, only create a sub if we have strong flagged evidence.
                if not flagged:
                    continue

            # Status logic:
            # if your enum doesn’t have trial/canceled, fall back to active but keep meta.kind.
            status_trial = getattr(SubscriptionStatus, "trial", None)
            status_canceled = getattr(SubscriptionStatus, "canceled", None)

            if trial_end is not None and len(dates) <= 1:
                status = status_trial or SubscriptionStatus.active
                kind = "trial"
            else:
                # Active window based on cadence, fallback 60 days
                if median_gap:
                    active_window = int(max(45, min(730, median_gap * 2)))
                else:
                    active_window = 60

                if (now - last_date).days > active_window:
                    status = status_canceled or SubscriptionStatus.active
                    kind = "inactive"
                else:
                    status = SubscriptionStatus.active
                    kind = "active"

            # Explainability
            amount_variability = _amount_variability(amounts) if amounts else None
            confidence, reasons = _confidence_and_reasons(
                dates=dates,
                median_gap=median_gap,
                variability=variability,
                amount_variability=amount_variability,
                skipped_cycles=skipped_cycles,
                flagged_count=len(flagged),
                last_date=last_date,
                amount_median=amount_median,
            )

            # Display vendor name: most common raw vendor string
            display_vendor = _pick_display_vendor(cluster_items) or vendor_key

            # Evidence transaction ids (best effort)
            evidence_ids = [
                getattr(t, "id", None)
                for t in sorted(
                    cluster_items,
                    key=lambda x: getattr(x, "transaction_date", now),
                    reverse=True
                )[:8]
            ]
            evidence_ids = [i for i in evidence_ids if i is not None]

            # Currency: first non-null currency we see in this cluster
            currency = next((getattr(t, "currency", None) for t in cluster_items if getattr(t, "currency", None)), None)

            predicted_next_renewal = (
                next_renewal.isoformat()
                if predicted_is_estimated and next_renewal is not None
                else None
            )

            # Store meta keys aligned to your insights endpoint/schema
            db.add(
                Subscription(
                    user_id=user_id,
                    vendor_name=display_vendor,
                    amount=amount_median,
                    currency=currency,
                    billing_cycle_days=median_gap,
                    last_charge_date=last_date,
                    next_renewal_date=next_renewal or trial_end,
                    trial_end_date=trial_end,
                    status=status,
                    meta={
                        # provenance
                        "source": "recompute_v2",
                        "kind": kind,
                        "vendor_key": vendor_key,

                        # counts
                        "count": len(cluster_items),
                        "flagged_count": len(flagged),

                        # cadence (old + new keys)
                        "median_gap_days": median_gap,
                        "gap_variability_days": variability,
                        "skipped_cycles": skipped_cycles,
                        "amount_variability": amount_variability,
                        "cadence_days": median_gap,
                        "cadence_variance_days": variability,

                        # prediction
                        "predicted_next_renewal_date": predicted_next_renewal,
                        "predicted_is_estimated": bool(predicted_is_estimated),

                        # explainability
                        "confidence": float(confidence),
                        "reasons": reasons[:8],
                        "evidence_tx_ids": evidence_ids,
                    },
                )
            )
            created += 1

    db.commit()

    after = db.query(Subscription).filter(Subscription.user_id == user_id).count()
    print(
        f"[recompute_subscriptions] deleted {before - len(ignored)} old, preserved {len(ignored)} ignored, "
        f"created {created}, now {after} subscriptions for user {user_id}"
    )
