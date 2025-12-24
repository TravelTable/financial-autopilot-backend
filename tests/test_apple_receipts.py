from datetime import datetime, timezone
from decimal import Decimal

from app.extractors.apple_receipt import is_apple_receipt, parse_apple_receipt


def test_parse_typical_subscription_receipt():
    subject = "Your receipt from Apple"
    from_email = "Apple <no_reply@email.apple.com>"
    body = """
    Apple Receipt
    Order ID: MK8V2LL/A
    App: ChatGPT
    Subscription: Pro Monthly
    Total: $19.99
    Order Date: January 5, 2024
    Country/Region: United States
    """
    assert is_apple_receipt(subject, from_email, body, None) is True
    parsed = parse_apple_receipt(body, None)
    assert parsed is not None
    assert parsed.app_name == "ChatGPT"
    assert parsed.subscription_display_name == "Pro Monthly"
    assert parsed.amount == Decimal("19.99")
    assert parsed.currency == "USD"
    assert parsed.order_id == "MK8V2LL/A"
    assert parsed.purchase_date_utc is not None
    assert parsed.purchase_date_utc.tzinfo == timezone.utc


def test_parse_app_name_without_subscription_tier():
    subject = "Your receipt from Apple"
    from_email = "Apple <no_reply@email.apple.com>"
    body = """
    Apple Receipt
    Order Number: W123456789
    Product: Notion
    Total: A$ 9.99
    Purchase Date: 2024-02-14
    """
    assert is_apple_receipt(subject, from_email, body, None) is True
    parsed = parse_apple_receipt(body, None)
    assert parsed is not None
    assert parsed.app_name == "Notion"
    assert parsed.subscription_display_name is None
    assert parsed.amount == Decimal("9.99")
    assert parsed.currency == "AUD"
    assert parsed.purchase_date_utc == datetime(2024, 2, 14, tzinfo=timezone.utc)


def test_non_apple_email_is_rejected():
    subject = "Apple harvest schedule"
    from_email = "Farm Updates <newsletter@farm.example.com>"
    body = """
    Our apple harvest schedule is now available.
    Download the PDF for your local co-op.
    """
    assert is_apple_receipt(subject, from_email, body, None) is False
