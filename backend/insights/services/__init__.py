"""
insights.services — AI-powered market insight generation.

Public API
----------
generate_daily_insight(date)
"""

from .ai_insight_service import generate_daily_insight

__all__ = [
    'generate_daily_insight',
]
