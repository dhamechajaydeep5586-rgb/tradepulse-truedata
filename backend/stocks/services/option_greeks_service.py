import math
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)

def normal_cdf(x):
    """Cumulative distribution function for the standard normal distribution."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def calculate_greeks(spot: float, strike: float, t_days: float, r: float = 0.07, sigma: float = 0.15, option_type: str = 'CE'):
    """
    JD Option Greeks Engine (Black-Scholes).
    spot: Current underlying price
    strike: Option strike price
    t_days: Days until expiry
    r: Risk-free rate (7% default)
    sigma: Implied Volatility (0.15 default)
    """
    t_years = max(0.0001, t_days / 365.0)
    
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * t_years) / (sigma * math.sqrt(t_years))
        d2 = d1 - sigma * math.sqrt(t_years)

        if option_type == 'CE':
            delta = normal_cdf(d1)
        else: # PE
            delta = normal_cdf(d1) - 1

        # Gamma
        gamma = (1.0 / (spot * sigma * math.sqrt(2 * math.pi * t_years))) * math.exp(-0.5 * d1**2)
        
        # Theta (Per Day)
        term1 = -(spot * sigma * math.exp(-0.5 * d1**2)) / (2 * math.sqrt(2 * math.pi * t_years))
        if option_type == 'CE':
            theta = (term1 - r * strike * math.exp(-r * t_years) * normal_cdf(d2)) / 365.0
        else:
            theta = (term1 + r * strike * math.exp(-r * t_years) * normal_cdf(-d2)) / 365.0

        return {
            "delta": round(delta, 4),
            "theta": round(theta, 4),
            "gamma": round(gamma, 6)
        }
    except Exception as e:
        logger.error(f"Greeks calculation error: {e}")
        return {"delta": 0, "theta": 0, "gamma": 0}

def estimate_iv(spot: float, strike: float, t_days: float, premium: float, option_type: str = 'CE') -> float:
    """
    Estimate Implied Volatility (IV) using Newton-Raphson.
    """
    if t_days <= 0:
        return 0.20

    iv = 0.20 # Starting guess
    try:
        for _ in range(10):
            greeks = calculate_greeks(spot, strike, t_days, 0.07, iv, option_type)
            # Simplified BS price for CE
            t_years = t_days / 365.0
            d1 = (math.log(spot / strike) + (0.07 + 0.5 * iv**2) * t_years) / (iv * math.sqrt(t_years))
            d2 = d1 - iv * math.sqrt(t_years)
            if option_type == 'CE':
                price = spot * normal_cdf(d1) - strike * math.exp(-0.07 * t_years) * normal_cdf(d2)
            else:
                price = strike * math.exp(-0.07 * t_years) * normal_cdf(-d2) - spot * normal_cdf(-d1)

            diff = premium - price
            if abs(diff) < 0.01: break

            # Vega approximation
            vega = spot * math.sqrt(t_years) * (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * d1**2)
            if vega > 0.01:
                iv += diff / vega
            else:
                break
    except (ZeroDivisionError, ValueError):
        return 0.20

    return round(max(0.01, min(2.0, iv)), 4)

def calculate_theoretical_premium(spot: float, strike: float, t_days: float, r: float = 0.07, sigma: float = 0.20, option_type: str = 'CE') -> float:
    """
    Calculate theoretical option premium using Black-Scholes.
    """
    t_years = max(0.0001, t_days / 365.0)
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * t_years) / (sigma * math.sqrt(t_years))
        d2 = d1 - sigma * math.sqrt(t_years)
        
        if option_type == 'CE':
            price = spot * normal_cdf(d1) - strike * math.exp(-r * t_years) * normal_cdf(d2)
        else:
            price = strike * math.exp(-r * t_years) * normal_cdf(-d2) - spot * normal_cdf(-d1)
            
        return round(max(0.05, price), 2)
    except Exception as e:
        logger.error(f"Error calculating theoretical premium: {e}")
        return 0.0

