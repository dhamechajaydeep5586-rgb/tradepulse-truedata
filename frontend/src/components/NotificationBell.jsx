import { useState, useEffect, useRef } from 'react';
import API from '../api/axios';

// Audit fix L1: Dashboard.jsx mounts NotificationBell twice simultaneously
// (mobile + desktop, CSS-hidden via Tailwind's md:hidden / hidden:md:flex, not
// unmounted) so both instances used to poll independently — doubling the
// request rate for no benefit. Rather than restructure Dashboard's responsive
// header layout (risk of visually breaking one breakpoint without a way to
// verify it here), the fetch/poll timer is lifted to a module-level singleton
// shared by every mounted instance: exactly one network poll regardless of how
// many bells are mounted, each instance just subscribes to the shared result.
let sharedState = { notifications: [], unreadCount: 0 };
const listeners = new Set();
let pollIntervalId = null;

async function fetchNotificationsShared() {
  try {
    const response = await API.get('/api/stocks/notifications/');
    sharedState = {
      notifications: response.data.notifications || [],
      unreadCount: response.data.unread_count || 0,
    };
    listeners.forEach((listener) => listener(sharedState));
  } catch (err) {
    console.error('Error fetching notifications:', err);
  }
}

// Audit fix (found in a follow-up audit): the L1 fix above made the 15s poll a
// shared singleton, but each mounted instance still registered its OWN
// `visibilitychange` listener — with the two simultaneously-mounted instances
// the L1 comment already describes (mobile + desktop, CSS-hidden not
// unmounted), a single tab-focus event fired fetchNotificationsShared() twice.
// Moved into the same singleton lifecycle as the poll interval: registered
// once when the first instance subscribes, removed once the last unsubscribes.
function handleVisibilityChange() {
  if (document.visibilityState === 'visible') {
    fetchNotificationsShared();
  }
}

function subscribeToNotifications(listener) {
  listeners.add(listener);
  if (listeners.size === 1) {
    fetchNotificationsShared();
    pollIntervalId = setInterval(fetchNotificationsShared, 15000); // Check every 15s
    document.addEventListener('visibilitychange', handleVisibilityChange);
  } else {
    // A second (or later) mounted instance starts from whatever the shared
    // singleton already knows, instead of waiting for the next 15s tick.
    listener(sharedState);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      if (pollIntervalId) {
        clearInterval(pollIntervalId);
        pollIntervalId = null;
      }
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    }
  };
}

export default function NotificationBell() {
  const [isOpen, setIsOpen] = useState(false);
  const [notifications, setNotifications] = useState(sharedState.notifications);
  const [unreadCount, setUnreadCount] = useState(sharedState.unreadCount);
  const dropdownRef = useRef(null);

  useEffect(() => {
    // visibilitychange handling now lives inside subscribeToNotifications'
    // singleton lifecycle (registered once for however many instances are
    // mounted) — see the comment above it.
    const unsubscribe = subscribeToNotifications((state) => {
      setNotifications(state.notifications);
      setUnreadCount(state.unreadCount);
    });

    return unsubscribe;
  }, []);

  // Click outside to close
  useEffect(() => {
    const handleClickOutside = (event) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleMarkAllRead = async () => {
    try {
      await API.patch('/api/stocks/notifications/', { action: 'mark_all_read' });
      fetchNotificationsShared();
    } catch (err) {
      console.error(err);
    }
  };

  const handleClearAll = async () => {
    try {
      await API.delete('/api/stocks/notifications/');
      fetchNotificationsShared();
    } catch (err) {
      console.error(err);
    }
  };

  const handleNotificationClick = async (id, isRead) => {
    if (isRead) return;
    try {
      await API.patch('/api/stocks/notifications/', { action: 'mark_read', id });
      fetchNotificationsShared();
    } catch (err) {
      console.error(err);
    }
  };

  const getTypeStyle = (type) => {
    switch (type) {
      case 'SUCCESS': return 'text-emerald-400 bg-emerald-400/10 border-emerald-500/20';
      case 'ALERT': return 'text-rose-400 bg-rose-400/10 border-rose-500/20';
      default: return 'text-blue-400 bg-blue-400/10 border-blue-500/20';
    }
  };

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="relative flex h-10 w-10 items-center justify-center rounded-lg bg-gray-800 border border-gray-700 text-gray-400 hover:text-white hover:bg-gray-700 transition"
      >
        <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
          <path d="M10 2a6 6 0 00-6 6v3.586l-.707.707A1 1 0 004 14h12a1 1 0 00.707-1.707L16 11.586V8a6 6 0 00-6-6zM10 18a3 3 0 01-3-3h6a3 3 0 01-3 3z" />
        </svg>
        {unreadCount > 0 && (
          <span className="absolute top-1.5 right-1.5 flex h-3 w-3">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-3 w-3 bg-rose-500 border border-gray-900"></span>
          </span>
        )}
      </button>

      {isOpen && (
        <div className="absolute right-0 mt-2 w-80 sm:w-96 origin-top-right rounded-xl bg-gray-900 border border-gray-800 shadow-2xl ring-1 ring-black ring-opacity-5 focus:outline-none z-50">
          <div className="flex items-center justify-between border-b border-gray-800 p-4">
            <h3 className="text-sm font-semibold text-white">Notifications</h3>
            <div className="flex gap-3">
              <button 
                onClick={handleMarkAllRead}
                className="text-xs text-gray-400 hover:text-white transition"
              >
                Mark all read
              </button>
              <button 
                onClick={handleClearAll}
                className="text-xs text-rose-400 hover:text-rose-300 transition"
              >
                Clear all
              </button>
            </div>
          </div>
          
          <div className="max-h-[28rem] overflow-y-auto p-2">
            {notifications.length === 0 ? (
              <div className="p-4 text-center text-sm text-gray-500">
                No notifications right now.
              </div>
            ) : (
              <ul className="space-y-2">
                {notifications.map((n) => (
                  <li
                    key={n.id}
                    onClick={() => handleNotificationClick(n.id, n.is_read)}
                    // Audit fix L2: click-only rows had no keyboard/screen-reader
                    // affordance — role="button" + tabIndex + onKeyDown lets
                    // keyboard users tab to a notification and activate it with
                    // Enter/Space, matching native button semantics.
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        handleNotificationClick(n.id, n.is_read);
                      }
                    }}
                    aria-label={`${n.title}. ${n.is_read ? 'Read' : 'Unread'}. ${n.message}`}
                    className={`cursor-pointer rounded-lg border p-3 transition ${
                      n.is_read
                        ? 'border-transparent bg-gray-800/50 hover:bg-gray-800 opacity-75'
                        : 'border-gray-700 bg-gray-800 hover:border-gray-600'
                    }`}
                  >
                    <div className="flex justify-between items-start gap-4">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border uppercase tracking-wider ${getTypeStyle(n.type)}`}>
                            {n.type}
                          </span>
                          {!n.is_read && (
                            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
                          )}
                        </div>
                        <p className="text-sm font-medium text-white mb-0.5">{n.title}</p>
                        <p className="text-xs text-gray-400 leading-relaxed">{n.message}</p>
                        <p className="text-[10px] text-gray-500 mt-2">
                          {new Date(n.created_at).toLocaleString([], { hour: '2-digit', minute: '2-digit', month: 'short', day: 'numeric' })}
                        </p>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
