# app/ai/insights.py

def generate_monthly_insights(summary: dict) -> dict:
    """
    summary = output of /analytics/summary
    """
    return {
        "bullets": [],
        "recommendations": [],
        "confidence": 0.0,
    }

