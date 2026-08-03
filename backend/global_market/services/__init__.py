"""
global_market.services — business-logic layer for global market data.

Public API
----------
fetch_global_market_data(date)
calculate_market_bias_for_date(date)
"""

from .data_fetch_service import fetch_global_market_data
from .market_bias_service import calculate_market_bias_for_date

__all__ = [
    'fetch_global_market_data',
    'calculate_market_bias_for_date',
]
