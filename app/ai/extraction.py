from app.ai.client import client

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "amount": {"type": "number"},
        "currency": {"type": "string"},
        "transaction_date": {"type": "string"},
        "category": {"type": "string"},
        "is_subscription": {"type": "boolean"},
        "trial_end_date": {"type": ["string", "null"]},
        "renewal_date": {"type": ["string", "null"]},
        "confidence": {
            "type": "object",
            "additionalProperties": {"type": "number"}
        }
    },
    "required": ["vendor", "amount", "currency", "transaction_date"]
}


def extract_transaction_from_email(text: str) -> dict:
    """
    Takes a *single* email body (plain text) and returns structured data.
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract structured financial transaction data from emails. "
                    "Return ONLY valid JSON matching the schema. "
                    "If a field is unknown, set it to null."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "transaction",
                "schema": RECEIPT_SCHEMA,
            },
        },
    )

    return response.choices[0].message.parsed

