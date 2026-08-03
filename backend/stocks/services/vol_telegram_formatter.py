# vol_telegram_formatter.py
from datetime import datetime
from typing import Dict, Any, List
from django.utils import timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

def get_ist_time_str() -> str:
    return timezone.now().astimezone(IST).strftime("%I:%M %p %Z")

def format_entry_signal(sig_data: Dict[str, Any]) -> str:
    """
    Upgraded ENTRY SIGNAL layout — intraday-aware.
    Shows theta velocity score, expected move, and time regime when available.
    """
    symbol = sig_data.get("symbol", "UNKNOWN")
    spot = sig_data.get("spot_price", 0.0)
    lot_size = sig_data.get("lot_size", 0)
    confidence = sig_data.get("confidence", 90)

    # Extract leg data
    legs = sig_data.get("legs", [])
    ce_strike = pe_strike = ce_premium = pe_premium = 0.0
    ce_delta_val = pe_delta_val = 0.0
    ce_theta_score = pe_theta_score = 0.0
    expected_move = 0.0
    time_regime = ""
    is_intraday = False

    for leg in legs:
        if leg.get("action") == "SELL":
            strike = float(leg.get("strike", 0.0))
            premium = float(leg.get("sell_price", 0.0))
            if leg.get("option_type") == "CE":
                ce_strike, ce_premium = strike, premium
                ce_delta_val = abs(float(leg.get("delta", 0.0) or 0.0))
                ce_theta_score = float(leg.get("intraday_theta_score", 0.0) or 0.0)
                expected_move = float(leg.get("expected_move", 0.0) or 0.0)
                time_regime = leg.get("time_regime", "")
                is_intraday = bool(leg.get("is_intraday", False))
            elif leg.get("option_type") == "PE":
                pe_strike, pe_premium = strike, premium
                pe_delta_val = abs(float(leg.get("delta", 0.0) or 0.0))
                pe_theta_score = float(leg.get("intraday_theta_score", 0.0) or 0.0)

    net_credit = ce_premium + pe_premium
    ce_dist_pct = ((ce_strike - spot) / spot * 100.0) if spot > 0 else 0.0
    pe_dist_pct = ((spot - pe_strike) / spot * 100.0) if spot > 0 else 0.0

    # INTRADAY NRML mode: 15-20% target, 100% credit SL, 3:30 PM exit
    mode_tag = f"⚡ INTRADAY NRML · {time_regime}" if time_regime else "⚡ INTRADAY NRML"
    target_profit = net_credit * 0.15   # 15% of credit captured
    sl_amount = net_credit               # 100% of credit = SL
    profit_label = f"₹{target_profit:.2f} (15-20% of credit)"

    # Theta velocity display
    avg_theta_score = (ce_theta_score + pe_theta_score) / 2 if (ce_theta_score + pe_theta_score) > 0 else 0.0
    theta_bar = "🔥" if avg_theta_score > 0.002 else ("⚡" if avg_theta_score > 0.001 else "🌀")

    # EM display
    em_line = f"\n• Expected Move: ±₹{expected_move:.2f} ({(expected_move/spot*100):.2f}%)" if expected_move > 0 and spot > 0 else ""

    return f"""📊 *TradePulse AI | Specialist Signal*

*{symbol} — SHORT STRANGLE*  {mode_tag}
━━━━━━━━━━━━━━━

*Spot*: ₹{spot:,.2f}  |  *Lot*: {lot_size:,}
*Confidence*: {confidence}%

*SELL CE* {ce_strike:,.0f} @ ₹{ce_premium:.2f}  Δ{ce_delta_val:.2f}
*SELL PE* {pe_strike:,.0f} @ ₹{pe_premium:.2f}  Δ{pe_delta_val:.2f}

*Net Credit*: ₹{net_credit:.2f}

*Strike Distance*:
• CE: +{ce_dist_pct:.2f}%  |  PE: −{pe_dist_pct:.2f}%{em_line}

{theta_bar} *Theta Velocity*: {avg_theta_score:.5f}  |  *Risk*: 🟢 LOW

⏰ {get_ist_time_str()}
━━━━━━━━━━━━━━━
TradePulse AI Engine"""


def format_live_update(update_data: Dict[str, Any]) -> str:
    """
    Upgraded LIVE POSITION UPDATE layout matching requested format.
    """
    symbol = update_data.get("symbol", "UNKNOWN")
    spot = update_data.get("spot_price", 0.0)
    
    ce_strike = update_data.get("ce_strike", 0.0)
    ce_old = update_data.get("ce_old_premium", 0.0)
    ce_new = update_data.get("ce_new_premium", 0.0)
    ce_delta = update_data.get("ce_delta", 0.0)
    
    pe_strike = update_data.get("pe_strike", 0.0)
    pe_old = update_data.get("pe_old_premium", 0.0)
    pe_new = update_data.get("pe_new_premium", 0.0)
    pe_delta = update_data.get("pe_delta", 0.0)
    
    entry_comb = ce_old + pe_old
    current_comb = ce_new + pe_new
    
    theta_capture = 0.0
    if entry_comb > 0:
        theta_capture = ((entry_comb - current_comb) / entry_comb) * 100.0
        
    pnl = update_data.get("pnl", 0.0)
    pnl_pct = update_data.get("pnl_pct", 0.0)
    risk_state_str = update_data.get("risk_state", "🟢 LOW RISK")
    
    return f"""🔄 *TradePulse AI | Live Position Update*

*{symbol}*
━━━━━━━━━━━━━━━

*Spot*: ₹{spot:,.2f}

*CE* {ce_strike:,.0f}: ₹{ce_old:.2f} → ₹{ce_new:.2f}
*PE* {pe_strike:,.0f}: ₹{pe_old:.2f} → ₹{pe_new:.2f}

*Entry Premium*: ₹{entry_comb:.2f}
*Current Premium*: ₹{current_comb:.2f}

*Theta Capture*: {theta_capture:.1f}%
*P&L*: ₹{pnl:,.2f} ({"+" if pnl >= 0 else ""}{pnl_pct:.2f}%)

*SL Zone*: ₹{entry_comb * 1.30:.2f}
*Current Risk*: {risk_state_str}

*Short Delta*:
• CE: {ce_delta:.2f}
• PE: {pe_delta:.2f}

⏰ {get_ist_time_str()}
━━━━━━━━━━━━━━━
TradePulse AI Engine"""


def format_consolidated_entry_signals(signals_data: List[Dict[str, Any]]) -> str:
    """Format multiple specialist entry signals into a single, highly-readable, concise summary message."""
    msg = f"📊 *TradePulse AI | Daily Specialist Strangles*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for i, sig in enumerate(signals_data):
        symbol = sig.get("symbol", "UNKNOWN")
        spot = sig.get("spot_price", 0.0)
        lot_size = sig.get("lot_size", 0)
        confidence = sig.get("confidence", 90)
        legs = sig.get("legs", [])
        
        ce_strike = pe_strike = ce_premium = pe_premium = 0.0
        for leg in legs:
            if leg.get("action") == "SELL":
                strike = float(leg.get("strike", 0.0))
                premium = float(leg.get("sell_price", 0.0))
                if leg.get("option_type") == "CE":
                    ce_strike, ce_premium = strike, premium
                elif leg.get("option_type") == "PE":
                    pe_strike, pe_premium = strike, premium
                    
        net_credit = ce_premium + pe_premium
        ce_dist_pct = ((ce_strike - spot) / spot * 100.0) if spot > 0 else 0.0
        pe_dist_pct = ((spot - pe_strike) / spot * 100.0) if spot > 0 else 0.0
        target_profit = net_credit * 0.15   # 15-20% of credit
        sl_amount = net_credit               # 100% of credit
        
        msg += f"{i+1}. *{symbol}* — Spot: ₹{spot:,.2f} (Lot: {lot_size:,})\n"
        msg += f"   • *SELL CE* {ce_strike:,.0f} @ ₹{ce_premium:.2f} (+{ce_dist_pct:.1f}%)\n"
        msg += f"   • *SELL PE* {pe_strike:,.0f} @ ₹{pe_premium:.2f} (-{pe_dist_pct:.1f}%)\n"
        msg += f"   • *Credit*: ₹{net_credit:.2f}\n\n"
        
    msg += f"⏰ {get_ist_time_str()}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"TradePulse AI Engine"
    return msg


def format_consolidated_live_updates(updates_data: List[Dict[str, Any]]) -> str:
    """Format strangle positions into a consolidated live update message (calculated for 2 lots)."""
    msg = f"🔄 *TradePulse AI | Strangles Session Update*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"

    total_pnl = 0.0
    for i, up in enumerate(updates_data):
        symbol = up.get("symbol", "UNKNOWN")
        spot = up.get("spot_price", 0.0)

        ce_strike = up.get("ce_strike", 0.0)
        ce_old = up.get("ce_old_premium", 0.0)
        ce_new = up.get("ce_new_premium", 0.0)

        pe_strike = up.get("pe_strike", 0.0)
        pe_old = up.get("pe_old_premium", 0.0)
        pe_new = up.get("pe_new_premium", 0.0)

        pnl = round(up.get("pnl", 0.0), 2)
        if abs(pnl) == 0.0:
            pnl = 0.0
        pnl_pct = round(up.get("pnl_pct", 0.0), 2)
        if abs(pnl_pct) == 0.0:
            pnl_pct = 0.0
            
        total_pnl += pnl
        pnl_symbol = "🟢" if pnl >= 0 else "🔴"

        lot_size = up.get("lot_size", 0)
        msg += f"{i+1}. *{symbol}* — Spot: ₹{spot:,.2f} (Lot: {lot_size:,})\n"
        msg += f"   • CE {ce_strike:,.0f}: ₹{ce_old:.2f} → ₹{ce_new:.2f}\n"
        msg += f"   • PE {pe_strike:,.0f}: ₹{pe_old:.2f} → ₹{pe_new:.2f}\n"
        msg += f"   • P&L (2 Lots): {pnl_symbol} *₹{pnl:,.2f}* ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)\n\n"

    pnl_all_symbol = "🟢" if total_pnl >= 0 else "🔴"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{pnl_all_symbol} *Total Session P&L (2 Lots): ₹{total_pnl:,.2f}*\n"
    msg += f"⏰ {get_ist_time_str()}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"TradePulse AI Engine"
    return msg
