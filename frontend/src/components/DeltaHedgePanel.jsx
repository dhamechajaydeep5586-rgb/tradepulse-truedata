import React, { useState, useEffect, useCallback, useRef } from 'react';
import axios from '../api/axios';
import { useAuth } from '../context/AuthContext';
import useSignalNotification from '../hooks/useSignalNotification';

// Cash-settled underlyings — matches config_vol.INDEX_UNDERLYINGS on the backend.
// Everything else this panel shows is a single-stock underlying, i.e. physically
// settled (audit fix C4).
const INDEX_UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'];

const DeltaHedgePanel = () => {
    const { user } = useAuth();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [resetting, setResetting] = useState(false);
    const [scanning, setScanning] = useState(false);
    const [showTooltip, setShowTooltip] = useState(null);
    const { permission, requestPermission } = useSignalNotification("delta-hedge");
    const resetPollRef = useRef(null);
    // Same pattern as OptionBuyingTable.jsx: read market state via a ref updated
    // on every successful fetch (not a value captured once at effect-mount time),
    // so a tab left open across the market open/close boundary picks up the
    // correct cadence on its next tick instead of freezing at whichever cadence
    // was live when the effect first ran.
    const isOpenRef = useRef(false);
    const lastFetchAtRef = useRef(0);
    // Positions default to collapsed (a one-line summary) — with ~9 live positions
    // each rendering a full multi-row contract table, the page required a lot of
    // scrolling to see everything. Expand on demand instead.
    const [expandedPositions, setExpandedPositions] = useState(new Set());
    const togglePosition = (key) => {
        setExpandedPositions((prev) => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key); else next.add(key);
            return next;
        });
    };

    // Audit fix M17: no request-sequence guard existed on this 3s poller — an
    // out-of-order response under variable latency could overwrite fresher state
    // with stale data. requestSeqRef increments per issued request; a response
    // only applies if it's still the latest one.
    const requestSeqRef = useRef(0);

    const fetchData = useCallback(async (isRefresh = false, force = false) => {
        if (!isRefresh) setLoading(true);
        const mySeq = ++requestSeqRef.current;
        try {
            const url = force ? '/api/stocks/delta-hedge/?force=true' : '/api/stocks/delta-hedge/';
            const response = await axios.get(url);
            if (mySeq !== requestSeqRef.current) return; // a newer request already superseded this one
            setData(response.data);
            setError(null);
            isOpenRef.current = response.data?.market_status === "OPEN";
        } catch (err) {
            if (mySeq !== requestSeqRef.current) return;
            setError(err.message);
        } finally {
            if (mySeq === requestSeqRef.current) setLoading(false);
        }
    }, []);

    const handleForceScan = async () => {
        setScanning(true);
        try {
            await fetchData(true, true);
        } finally {
            setScanning(false);
        }
    };

    useEffect(() => {
        // 3s live refresh while the market is open; drops to a 30-min cadence
        // when closed, matching OptionBuyingTable's closed-market poll rate.
        const CHECK_MS = 3000;
        const CLOSED_POLL_MS = 30 * 60 * 1000;
        lastFetchAtRef.current = Date.now();
        fetchData();
        const interval = setInterval(() => {
            const now = Date.now();
            if (isOpenRef.current || now - lastFetchAtRef.current >= CLOSED_POLL_MS) {
                lastFetchAtRef.current = now;
                fetchData(true);
            }
        }, CHECK_MS);
        return () => clearInterval(interval);
    }, [fetchData]);

    const formatCurrency = (value) => {
        return new Intl.NumberFormat('en-IN', {
            style: 'currency',
            currency: 'INR',
            maximumFractionDigits: 2
        }).format(value);
    };

    const formatPercentage = (value) => {
        return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
    };

    const isMarketOpen = data?.market_status === "OPEN";

    // MCX removed platform-wide — backend never returns an 'MCX' section anymore, so
    // this only ever themes the NSE/NFO equity specialist column now.
    const getThemeClasses = () => ({
        border: 'border-indigo-500/20 hover:border-indigo-500/40',
        bg: 'bg-indigo-500/5',
        badge: 'bg-indigo-500/10 border-indigo-500/20 text-indigo-400',
        glow: 'bg-indigo-500/10',
        accent: 'text-indigo-400',
        label: 'Equity Specialist'
    });

    const HedgeInfoTooltip = ({ section }) => {
        const colorClass = 'indigo';

        return (
            <div className={`absolute z-30 bottom-full mb-2 left-0 w-80 p-4 bg-gray-900/95 border border-${colorClass}-500/30 rounded-xl shadow-2xl backdrop-blur-xl text-xs text-gray-300 leading-relaxed ring-1 ring-white/10`}>
                <div className={`flex items-center gap-2 mb-2 text-${colorClass}-400 font-bold uppercase tracking-wider`}>
                    <span className={`p-1 bg-${colorClass}-500/10 rounded`}>90% Win-Rate Strategy</span>
                </div>
                <p className="mb-2">
                    Professional <strong>Short Strangle</strong> for {section.underlying}.
                </p>
                <ul className="space-y-1 list-disc list-inside text-gray-400">
                    <li><span className="text-emerald-400">OTM Selling:</span> Profit from low-probability outcomes.</li>
                    <li><span className={`text-${colorClass}-400`}>Theta Burn:</span> Earn premium while price stays sideways.</li>
                    <li><span className="text-rose-400">Naked / Uncapped Risk:</span> No long-option hedge — a large move against either strike is not capped by an owned option. Managed only by systematic stop-loss and delta-breach auto-exit rules.</li>
                    {section.underlying && !INDEX_UNDERLYINGS.includes(section.underlying) && (
                        <li><span className="text-amber-400">Physical Settlement:</span> Single-stock options are American-style and physically settled — a leg that drifts/gaps ITM before expiry can be assigned, requiring physical delivery rather than a cash settlement.</li>
                    )}
                </ul>
                {section.assignment_risk && (
                    <p className="mt-2 pt-2 border-t border-amber-500/20 text-amber-400 font-semibold">
                        ⚠ Assignment risk: a short leg is nearing ITM on this physically-settled stock option.
                    </p>
                )}
            </div>
        );
    };

    const handleReset = async () => {
        if (!window.confirm("Are you sure you want to clear all current specialist setups? This will cancel existing trades and restart scanning.")) return;
        
        // Clear any existing reset poll
        if (resetPollRef.current) clearTimeout(resetPollRef.current);
        
        setResetting(true);
        try {
            const res = await axios.post('/api/stocks/delta-hedge/');
            const { created = 0 } = res.data || {};
            // Backend ran a synchronous scan — wait briefly then fetch
            // If it created signals, show them immediately; otherwise poll until signals appear
            resetPollRef.current = setTimeout(async () => {
                await fetchData(true);
                setResetting(false);
            }, created > 0 ? 800 : 2000);
        } catch (err) {
            setResetting(false);
            alert("Reset failed: " + err.message);
        }
    };

    // Cleanup reset poll on unmount
    useEffect(() => () => { if (resetPollRef.current) clearTimeout(resetPollRef.current); }, []);

    if (loading && !data) {
        return (
            <div className="rounded-3xl border border-white/5 bg-gray-950/50 p-8 backdrop-blur-2xl shadow-3xl">
                <div className="flex items-center justify-between mb-8">
                    <div className="h-6 w-48 bg-white/5 rounded-lg animate-pulse" />
                    <div className="h-6 w-32 bg-white/5 rounded-lg animate-pulse" />
                </div>
                <div className="space-y-6">
                    {[1, 2, 3].map(i => (
                        <div key={i} className="h-20 bg-white/5 rounded-2xl animate-pulse" />
                    ))}
                </div>
            </div>
        );
    }

    return (
        <section className="relative overflow-hidden rounded-3xl border border-white/5 bg-gray-950/40 p-1 md:p-6 backdrop-blur-3xl shadow-2xl transition-all duration-500 hover:border-white/10">
            {/* Background Glow */}
            <div className="absolute -top-24 -right-24 h-64 w-64 rounded-full bg-indigo-600/10 blur-[100px] pointer-events-none" />
            <div className="absolute -bottom-24 -left-24 h-64 w-64 rounded-full bg-emerald-600/10 blur-[100px] pointer-events-none" />

            <div className="relative px-2 md:px-0">
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8">
                    <div className="flex items-center gap-3">
                        <div className="p-2.5 bg-indigo-500/10 rounded-2xl border border-indigo-500/20">
                            <svg className="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
                            </svg>
                        </div>
                        <div>
                            <div className="flex items-center gap-2">
                                <h2 className="text-xl font-bold text-white tracking-tight">Institutional Hedge Panel</h2>
                                {data?.is_sideways !== undefined && (
                                    <span className={`text-[10px] px-2 py-0.5 rounded-full border font-black uppercase tracking-tighter ${
                                        data.is_sideways ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-400" : "bg-rose-500/10 border-rose-500/20 text-rose-400"
                                    }`}>
                                        {data.market_state}
                                    </span>
                                )}
                                {data?.is_degraded && (
                                    <span className="text-[10px] bg-amber-500/10 text-amber-500 px-2 py-0.5 rounded-full border border-amber-500/20 font-black uppercase tracking-tighter animate-pulse">
                                        {data.provider_status}
                                    </span>
                                )}
                            </div>
                            <p className="text-xs text-gray-400">Automated Hedged Options Logic</p>
                        </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-3 bg-white/5 p-1.5 rounded-2xl border border-white/10">
                        {/* Reset Strategy Button */}
                        <button 
                            onClick={handleReset}
                            disabled={resetting}
                            className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border text-[10px] font-black uppercase tracking-widest transition-colors group ${
                                resetting
                                    ? 'border-amber-500/30 bg-amber-500/5 text-amber-400 cursor-not-allowed'
                                    : 'border-rose-500/30 bg-rose-500/5 text-rose-400 hover:bg-rose-500/10'
                            }`}
                        >
                            {resetting ? (
                                <>
                                    <span className="h-3 w-3 rounded-full border-2 border-amber-400 border-t-transparent animate-spin" />
                                    Resetting...
                                </>
                            ) : (
                                <>
                                    <svg className="w-3.5 h-3.5 group-hover:rotate-180 transition-transform duration-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                                    </svg>
                                    Reset Strategy
                                </>
                            )}
                        </button>

                        {/* Force Scan — generates today's signal on demand if it hasn't run yet
                            (e.g. the 10:45 AM cron missed it); if one's already generated today
                            this just re-fetches current state instead of scanning again. */}
                        <button
                            onClick={handleForceScan}
                            disabled={scanning || resetting}
                            className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border text-[10px] font-black uppercase tracking-widest transition-colors group ${
                                scanning
                                    ? 'border-indigo-500/30 bg-indigo-500/5 text-indigo-400 cursor-not-allowed'
                                    : 'border-emerald-500/30 bg-emerald-500/5 text-emerald-400 hover:bg-emerald-500/10'
                            }`}
                            title="Scan now if today's signal hasn't been generated yet"
                        >
                            <span className={scanning ? "animate-spin" : ""}>⚡</span>
                            {scanning ? "Scanning..." : "Force Scan"}
                        </button>

                        {data?.trading_window !== undefined && (
                            <div className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border text-[10px] font-bold uppercase tracking-widest ${
                                data.trading_window ? "bg-indigo-500/10 border-indigo-500/20 text-indigo-400" : "bg-gray-500/10 border-gray-500/20 text-gray-400"
                            }`}>
                                ⏱️ {data.trading_window ? "Window Active" : "Window Closed"}
                            </div>
                        )}
                        <div className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border ${
                            isMarketOpen ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-400" : "bg-amber-500/10 border-amber-500/20 text-amber-400"
                        }`}>
                            <span className={`h-2 w-2 rounded-full ${isMarketOpen ? "bg-emerald-400 animate-pulse" : "bg-amber-400"}`} />
                            <span className="text-xs font-bold uppercase tracking-widest">{isMarketOpen ? "Live" : "Closed"}</span>
                        </div>

                        {data?.streamer_active && (
                            <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl border bg-blue-500/10 border-blue-500/20 text-blue-400 shadow-[0_0_15px_rgba(59,130,246,0.1)]">
                                <span className="h-2 w-2 rounded-full bg-blue-400 animate-ping" />
                                <span className="text-[10px] font-black uppercase tracking-widest">WebSocket Stream</span>
                            </div>
                        )}
                    </div>
                </div>

                {error && (
                    <div className="mb-6 p-4 rounded-2xl bg-rose-500/10 border border-rose-500/20 text-rose-400 text-sm flex items-center gap-3">
                        <svg className="w-5 h-5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                        </svg>
                        {error}
                    </div>
                )}

                {(!data || !data.sections) ? (
                    <div className="flex flex-col items-center justify-center py-20 text-center">
                        <div className="w-20 h-20 bg-white/5 rounded-full flex items-center justify-center mb-4 border border-white/10">
                            <svg className="w-10 h-10 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                            </svg>
                        </div>
                        <h3 className="text-gray-300 font-medium">No Optimal Hedge Conditions</h3>
                        <p className="text-xs text-gray-500 mt-1 max-w-xs mx-auto">
                            System is scanning for Sideways markets between 9:30 AM – 3:00 PM with institutional Value Area guards.
                        </p>
                    </div>
                ) : (
                    <div className="space-y-12">
                         {/* MCX removed platform-wide — backend never returns an 'MCX' section anymore,
                             so this only ever renders the NSE/NFO equity specialist column now. */}
                         {['NSE'].map((exch) => {
                            const group = data.sections.filter(s =>
                                (exch === 'NSE' && (s.exchange === 'NSE' || s.exchange === 'NFO'))
                            );
                            
                            const mainTheme = getThemeClasses();
                            return (
                                <div key={exch} className={`group p-4 md:p-6 rounded-3xl ${mainTheme.bg} border ${mainTheme.border} transition-all duration-300 relative overflow-hidden backdrop-blur-3xl`}>
                                    {/* Header */}
                                    <div className="flex items-center justify-between mb-8 pb-4 border-b border-white/5 relative z-10">
                                        <div className="flex items-center gap-4">
                                            <div className="p-2 rounded-xl bg-indigo-500/10 border border-white/5">
                                                <svg className="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
                                                </svg>
                                            </div>
                                            <div>
                                                <h4 className="text-xs font-black uppercase tracking-[0.2em] text-indigo-400 mb-1">
                                                    Equity Specialist Sector
                                                </h4>
                                                <h3 className="text-lg font-bold text-white tracking-tight">Institutional Stock Option Setup</h3>
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <span className={`px-3 py-1 rounded-full text-[10px] font-black uppercase tracking-tighter ${mainTheme.badge}`}>
                                                90%+ WIN-RATE SYSTEM
                                            </span>
                                        </div>
                                    </div>

                                    {/* Multiple Setups Container — tight spacing so collapsed (one-line)
                                        positions stack compactly; expanded ones get their own breathing
                                        room via the conditional divider below instead of a uniform gap. */}
                                    <div className="space-y-1 relative z-10">
                                        {group.length === 0 ? (
                                            <div className="flex flex-col items-center justify-center py-12 text-center bg-gray-950/20 rounded-2xl border border-dashed border-white/5 shadow-inner">
                                                <div className="w-12 h-12 bg-white/5 rounded-full flex items-center justify-center mb-3">
                                                    <svg className="w-6 h-6 text-gray-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                                                    </svg>
                                                </div>
                                                <h4 className="text-gray-400 text-sm font-bold uppercase tracking-widest">No Signals Found</h4>
                                                <p className="text-[10px] text-gray-600 mt-1 uppercase font-medium tracking-tighter">
                                                    Scanner is active. No sideways equities meet the institutional win-rate criteria currently.
                                                </p>
                                            </div>
                                        ) : (
                                            group.map((section, sIdx) => {
                                            const theme = getThemeClasses();
                                            const posKey = `${exch}-${sIdx}-${section.title}`;
                                            const isExpanded = expandedPositions.has(posKey);
                                            const combinedPnl = (section.legs || []).reduce((sum, leg) => sum + (leg.pnl || 0), 0);
                                            // "Live Tracking" should reflect this position's own exchange being open
                                            // right now, not just "trade not yet exited" — an NSE position sitting
                                            // ACTIVE after 3:30 PM close isn't actually live, it's holding overnight.
                                            const segmentOpen = !!data?.nse_open;
                                            const isLiveNow = section.status === 'ACTIVE' && segmentOpen;
                                            const isHoldingOvernight = section.status === 'ACTIVE' && !segmentOpen;
                                            return (
                                                <div key={sIdx} className="relative">
                                                    {/* Sub-Header per Stock/Index — click to expand/collapse the contract table */}
                                                    <button
                                                        type="button"
                                                        onClick={() => togglePosition(posKey)}
                                                        className={`w-full flex items-center justify-between group/head text-left ${isExpanded ? 'mb-4' : 'py-1'}`}
                                                        aria-expanded={isExpanded}
                                                    >
                                                        <div className="flex items-center gap-3">
                                                            <svg
                                                                className={`w-4 h-4 text-gray-500 transition-transform duration-200 ${isExpanded ? 'rotate-90' : ''}`}
                                                                fill="none" stroke="currentColor" viewBox="0 0 24 24"
                                                            >
                                                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 5l7 7-7 7" />
                                                            </svg>
                                                            <div className="w-1 h-6 rounded-full bg-indigo-500" />
                                                            <h3 className="text-base font-black text-white uppercase tracking-wider">{section.title}</h3>
                                                            {isLiveNow && (
                                                                <div className="flex items-center gap-2 px-2 py-0.5 rounded-md bg-emerald-500/10 border border-emerald-500/20">
                                                                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
                                                                    <span className="text-[9px] font-black text-emerald-400 uppercase tracking-widest">Live Tracking</span>
                                                                </div>
                                                            )}
                                                            {isHoldingOvernight && (
                                                                <div className="flex items-center gap-2 px-2 py-0.5 rounded-md bg-gray-500/10 border border-gray-500/20">
                                                                    <span className="h-1.5 w-1.5 rounded-full bg-gray-400" />
                                                                    <span className="text-[9px] font-black text-gray-400 uppercase tracking-widest">Holding Overnight</span>
                                                                </div>
                                                            )}
                                                            {!isExpanded && (
                                                                <span className={`text-sm font-mono font-black ${combinedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                                                                    {combinedPnl >= 0 ? '+' : ''}{formatCurrency(combinedPnl)}
                                                                </span>
                                                            )}
                                                        </div>
                                                        <div className="flex items-center gap-6">
                                                            <div className="text-right">
                                                                <div className="text-[10px] text-gray-500 uppercase tracking-widest font-bold">Added ❯ Entry</div>
                                                                <div className={`text-xs font-mono ${section.status === 'ACTIVE' ? 'text-emerald-400' : 'text-indigo-400/80'}`}>
                                                                    {section.signal_time} <span className="opacity-30">❯</span> {section.entry_time}
                                                                </div>
                                                                {section.status === 'PENDING' && section.grace_remaining > 0 && (
                                                                    <div className="flex items-center gap-1.5 mt-1 justify-end">
                                                                        <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
                                                                        <span className="text-[9px] font-mono text-amber-400 tracking-wider">
                                                                            Auto-entry in {Math.floor(section.grace_remaining / 60)}m {section.grace_remaining % 60}s
                                                                        </span>
                                                                    </div>
                                                                )}
                                                            </div>
                                                            <div className="text-right border-l border-white/10 pl-6">
                                                                <div className="text-[10px] text-gray-500 uppercase tracking-widest font-bold">Spot</div>
                                                                <div className="text-sm font-mono text-gray-300">{formatCurrency(section.spot_price)}</div>
                                                            </div>
                                                        </div>
                                                    </button>

                                                    {/* Specialist Table — collapsed by default, click the row above to expand */}
                                                    {isExpanded && (
                                                    <div className="overflow-x-auto rounded-2xl border border-white/5 bg-gray-950/50">
                                                        <table className="w-full text-left">
                                                            <thead>
                                                                <tr className="text-[10px] text-gray-500 uppercase tracking-widest border-b border-white/5">
                                                                    <th className="px-4 py-3 font-black">Contract</th>
                                                                    <th className="px-4 py-3 font-black">Strike</th>
                                                                    <th className="px-4 py-3 font-black">Action</th>
                                                                    <th className="px-4 py-3 text-center font-black">Lot</th>
                                                                    <th className="px-4 py-3 text-center font-black">1:2 RR Zones (TGT/SL)</th>
                                                                    <th className="px-4 py-3 text-right font-black">Execution (Entry ❯ CMP)</th>
                                                                    <th className="px-4 py-3 text-center font-black">Status</th>
                                                                    <th className="px-4 py-3 text-right text-[10px] font-black uppercase tracking-widest text-gray-500">P&L (M2M)</th>
                                                                </tr>
                                                            </thead>
                                                            <tbody className="divide-y divide-white/5">
                                                                {section.legs.map((leg, lIdx) => {
                                                                    const isPending = section.status === 'PENDING';
                                                                    const isTgtHit = leg.cmp <= leg.target_price && !isPending;
                                                                    const isSlHit = leg.cmp >= leg.stop_loss_price && !isPending;
                                                                    const isActive = section.status === 'ACTIVE' && !isTgtHit && !isSlHit;
                                                                    // A rebalance (see rebalance_delta_neutral_strangle) marks the old leg
                                                                    // EXPIRED and appends a replacement leg — without this check both ended
                                                                    // up rendered as ordinary rows via the target/SL logic above, with no
                                                                    // indication one of them was actually rolled away for delta-neutrality.
                                                                    const isRolled = leg.status === 'EXPIRED' && !!leg.exit_reason;

                                                                    return (
                                                                        <tr key={lIdx} className={`group/row hover:bg-white/5 transition-colors ${isRolled ? 'opacity-40' : ''}`}>
                                                                            <td className="px-4 py-4">
                                                                                <div className="text-sm font-bold text-white group-hover/row:text-indigo-300 transition-colors uppercase">
                                                                                    {leg.symbol}
                                                                                    {isRolled && <span className="ml-2 text-[9px] text-amber-400 normal-case font-normal">↻ rolled</span>}
                                                                                </div>
                                                                                <div className="text-[9px] text-gray-500 mt-1 uppercase tracking-tighter">
                                                                                    {isRolled ? leg.exit_reason : leg.expiry}
                                                                                </div>
                                                                            </td>
                                                                            <td className="px-4 py-4">
                                                                                <div className="flex items-center gap-2">
                                                                                    <span className="text-sm font-mono font-bold text-gray-200">{leg.strike}</span>
                                                                                    <span className={`text-[9px] px-1.5 py-0.5 rounded font-black ${
                                                                                        leg.option_type === 'CE' ? "bg-indigo-500/20 text-indigo-400" : "bg-pink-500/20 text-pink-400"
                                                                                    }`}>{leg.option_type}</span>
                                                                                </div>
                                                                            </td>
                                                                            <td className="px-4 py-4">
                                                                                <span className={`text-[9px] px-2 py-1 rounded-md font-black tracking-widest border ${
                                                                                    leg.action === 'SELL' ? "bg-rose-500/10 border-rose-500/20 text-rose-400" : "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
                                                                                }`}>{leg.action}</span>
                                                                            </td>
                                                                            <td className="px-4 py-4 text-center">
                                                                                <span className="text-xs font-mono font-bold text-gray-400">
                                                                                    {leg.lot_size || 1}
                                                                                </span>
                                                                            </td>
                                                                            <td className="px-4 py-4 text-center">
                                                                                <div className="flex flex-col items-center gap-1">
                                                                                    <div className={`text-[10px] font-mono px-2 py-0.5 rounded ${isTgtHit ? 'bg-emerald-500/20 text-emerald-400' : 'text-gray-400'}`}>
                                                                                        TGT: {leg.target_price > 0 ? `₹${leg.target_price.toFixed(2)}` : '--'}
                                                                                    </div>
                                                                                    <div className={`text-[10px] font-mono px-2 py-0.5 rounded ${isSlHit ? 'bg-rose-500/20 text-rose-400' : 'text-gray-400/50'}`}>
                                                                                        SL: {leg.stop_loss_price > 0 ? `₹${leg.stop_loss_price.toFixed(2)}` : '--'}
                                                                                    </div>
                                                                                </div>
                                                                            </td>
                                                                            <td className="px-4 py-4 text-right">
                                                                                <div className="flex flex-col items-end gap-1">
                                                                                    <div className="text-xs font-mono text-gray-500 uppercase tracking-tighter">
                                                                                        {isPending ? (section.grace_remaining > 0 ? 'Live Entry: ' : 'Est. Entry: ') : 'Entry: '}{leg.sell_price > 0 ? `₹${leg.sell_price.toFixed(2)}` : '--'}
                                                                                    </div>
                                                                                    <div className="text-sm font-mono font-black text-white flex items-center gap-2">
                                                                                        CMP: {leg.cmp > 0 ? `₹${leg.cmp.toFixed(2)}` : '--'}
                                                                                        <span className={`h-1.5 w-1.5 rounded-full ${isPending ? 'bg-gray-500' : (leg.cmp <= leg.sell_price ? "bg-emerald-400" : "bg-rose-400")}`} />
                                                                                        {leg.is_theoretical && (
                                                                                            <span
                                                                                                className="px-1 py-0.5 rounded text-[9px] font-bold uppercase tracking-tighter bg-amber-500/10 text-amber-400 border border-amber-500/20"
                                                                                                title="TrueData has no live quote for this leg right now — CMP is a Black-Scholes estimate, not a tradeable price."
                                                                                            >
                                                                                                Est.
                                                                                            </span>
                                                                                        )}
                                                                                    </div>
                                                                                 </div>
                                                                            </td>
                                                                            <td className="px-4 py-4 text-center">
                                                                                {isRolled ? (
                                                                                    <span className="px-2 py-1 rounded-md bg-amber-500/10 text-amber-400 text-[10px] font-black uppercase border border-amber-500/20 tracking-widest">↻ Rolled</span>
                                                                                ) : isPending ? (
                                                                                    <span className="px-2 py-1 rounded-md bg-amber-500/10 text-amber-500 text-[10px] font-black uppercase border border-amber-500/20 tracking-widest animate-pulse">Pending</span>
                                                                                ) : isTgtHit ? (
                                                                                    <span className="px-2 py-1 rounded-md bg-emerald-500/20 text-emerald-400 text-[10px] font-black uppercase ring-1 ring-emerald-500/30">Target Hit</span>
                                                                                ) : isSlHit ? (
                                                                                    <span className="px-2 py-1 rounded-md bg-rose-500/20 text-rose-400 text-[10px] font-black uppercase ring-1 ring-rose-500/30">SL Hit!</span>
                                                                                ) : (section.status === 'EXPIRED' || section.status === 'CANCELLED') ? (
                                                                                    section.was_activated ? (
                                                                                        <span className="px-2 py-1 rounded-md bg-gray-500/10 text-gray-400 text-[10px] font-black uppercase border border-gray-500/20 tracking-widest">Auto S.O</span>
                                                                                    ) : (
                                                                                        <span className="px-2 py-1 rounded-md bg-gray-500/10 text-gray-500 text-[10px] font-black uppercase border border-gray-500/10 tracking-widest">Filtered</span>
                                                                                    )
                                                                                ) : (
                                                                                    <span className="px-2 py-1 rounded-md bg-indigo-500/10 text-indigo-400 text-[10px] font-black uppercase border border-indigo-500/20 tracking-widest">Active</span>
                                                                                )}
                                                                            </td>
                                                                            <td className="px-4 py-4 text-right">
                                                                                <div className={`text-sm font-mono font-black ${(isPending || (!section.was_activated && (section.status === 'CANCELLED' || section.status === 'EXPIRED'))) ? 'text-gray-500 opacity-40' : (leg.pnl >= 0 ? "text-emerald-400" : "text-rose-400")}`}>
                                                                                    {leg.pnl > 0 ? '+' : ''}{formatCurrency(leg.pnl)}
                                                                                </div>
                                                                                <div className={`text-[10px] font-bold ${(isPending || (!section.was_activated && (section.status === 'CANCELLED' || section.status === 'EXPIRED'))) ? 'text-gray-500/30' : (leg.pnl_pct >= 0 ? "text-emerald-400/50" : "text-rose-400/50")}`}>
                                                                                    {formatPercentage(leg.pnl_pct)}
                                                                                </div>
                                                                            </td>
                                                                        </tr>
                                                                    );
                                                                })}
                                                            </tbody>
                                                        </table>
                                                    </div>
                                                    )}

                                                    {/* Spacer between setups — generous only when this position is
                                                        expanded (visually separates its table from the next row);
                                                        collapsed rows just get a thin divider, no extra whitespace. */}
                                                    {sIdx < group.length - 1 && (
                                                        <div className={`h-px bg-gradient-to-r from-transparent via-white/10 to-transparent ${isExpanded ? 'mt-8 mb-4' : 'my-1'}`} />
                                                    )}
                                                </div>
                                            );
                                        })
                                    )}
                                </div>
                                </div>
                            );
                        })}
                    </div>
                )}

                {data?.total_pnl !== undefined && data?.sections?.length > 0 && (
                    <div className="mt-12 p-6 rounded-3xl bg-gradient-to-br from-indigo-500/10 via-transparent to-emerald-500/10 border border-white/10 backdrop-blur-3xl shadow-xl flex items-center justify-between">
                        <div>
                            <div className="text-xs text-gray-400 uppercase tracking-widest font-black">Aggregate Realized P&L</div>
                            <div className="text-[10px] text-gray-500 mt-0.5 italic">Total across active Short Strangles</div>
                        </div>
                        <div className="text-right">
                            <div className={`text-3xl font-black tracking-tighter ${data.total_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                                {data.total_pnl >= 0 ? '+' : ''}{formatCurrency(data.total_pnl)}
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </section>
    );
};

export default DeltaHedgePanel;
