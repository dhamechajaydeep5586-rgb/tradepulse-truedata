# risk_state_engine.py
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Any

logger = logging.getLogger(__name__)

class RiskState(Enum):
    LOW = "🟢 LOW RISK"
    MODERATE = "🟡 MODERATE RISK"
    HIGH = "🟠 HIGH RISK"
    CRITICAL = "🔴 CRITICAL RISK"

def classify_risk_state(premium_expansion_pct: float, max_short_delta: float) -> RiskState:
    """
    Dynamic risk classification engine.
    """
    try:
        # Evaluate Critical condition first
        if premium_expansion_pct > 30.0 or max_short_delta > 0.65:
            return RiskState.CRITICAL
        
        # Evaluate High condition
        if premium_expansion_pct > 20.0 or max_short_delta > 0.50:
            return RiskState.HIGH
            
        # Evaluate Moderate condition
        if premium_expansion_pct > 10.0 or max_short_delta > 0.35:
            return RiskState.MODERATE
            
        return RiskState.LOW
    except Exception as e:
        logger.error(f"[RISK_ENGINE] Classification error: {e}", exc_info=True)
        return RiskState.LOW
