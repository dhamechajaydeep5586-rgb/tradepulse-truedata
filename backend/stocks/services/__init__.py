"""
stocks.services — business-logic layer for stock data pipeline.

Public API
----------
fetch_and_store_bhavcopy(date)
"""

import logging

from .bhavcopy_service import fetch_and_store_bhavcopy

logger = logging.getLogger(__name__)

__all__ = [
    'fetch_and_store_bhavcopy',
]
