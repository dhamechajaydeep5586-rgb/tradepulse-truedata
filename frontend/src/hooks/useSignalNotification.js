import { useState, useEffect, useRef, useCallback } from 'react';

export default function useSignalNotification(category = "intraday") {
  const [permission, setPermission] = useState("default");
  
  // Track symbols that have ALREADY triggered a notification 
  // to avoid spamming the user on every 15-second refresh.
  const notifiedSymbols = useRef(new Set());

  useEffect(() => {
    if (!("Notification" in window)) {
      console.warn("This browser does not support desktop notification");
      return;
    }
    setPermission(Notification.permission);
  }, []);

  const requestPermission = useCallback(async () => {
    if (!("Notification" in window)) return;
    const p = await Notification.requestPermission();
    setPermission(p);
    
    if (p === "granted") {
      new Notification("Notifications Enabled", {
        body: `You will now receive alerts for new ${category} signals.`,
        icon: "/favicon.ico", // Attempt to use standard favicon
      });
    }
  }, [category]);

  const notifyNewSignals = useCallback((signals) => {
    if (permission !== "granted") return;
    if (!signals || signals.length === 0) return;

    const newAlerts = [];
    
    signals.forEach((s) => {
      // Create a unique key for the signal (symbol + direction + level)
      // This way if a stock flips from BUY to SELL, or gets a new entry price, it alerts again.
      const sigKey = `${s.symbol}-${s.signal}-${s.entry}`;
      
      if (!notifiedSymbols.current.has(sigKey)) {
        newAlerts.push(s);
        notifiedSymbols.current.add(sigKey);
      }
    });

    if (newAlerts.length > 0) {
      const isMultiple = newAlerts.length > 1;
      const title = isMultiple 
        ? `${newAlerts.length} New ${category.charAt(0).toUpperCase() + category.slice(1)} Signals!` 
        : `New ${newAlerts[0].signal} Signal: ${newAlerts[0].symbol}`;
      
      const body = isMultiple
        ? newAlerts.map(s => `${s.symbol} (${s.signal} @ ₹${s.entry})`).join(", ")
        : `Target: ₹${newAlerts[0].target} | SL: ₹${newAlerts[0].stop_loss}\nReason: ${newAlerts[0].reason}`;

      try {
        new Notification(title, {
          body,
          icon: "/favicon.ico",
          tag: "live-signal-alert", // Groups notifications together to prevent flooding the screen
          renotify: true,
        });
      } catch (err) {
        console.error("Notification Error:", err);
      }
    }
  }, [permission, category]);

  // Expose an explicit "clear reset" if desired
  const clearCache = useCallback(() => {
    notifiedSymbols.current.clear();
  }, []);

  return { permission, requestPermission, notifyNewSignals, clearCache };
}
