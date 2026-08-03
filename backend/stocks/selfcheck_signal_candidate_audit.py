"""Phase 5 audit-trail check for `record_candidates()`
(doc/MARKET_DATA_ENGINE_ARCHITECTURE.md §9/§12 row 5).

Proves record_candidates() is a pure, best-effort observability side-effect:
- Accepted and rejected candidates both become `SignalCandidate` rows with the right
  status/reject_reason split.
- Missing optional keys (e.g. no rank_score) don't crash — defaults gracefully.
- Any exception raised while building/persisting rows is logged and swallowed, never
  propagated to the caller — a scan's actual signal generation must never break because
  the audit trail failed.

Mocks `SignalCandidate.objects.bulk_create` throughout so this never touches the real
network or the production Supabase DB — local dev points DATABASE_URL directly at
production, so a test that exercised the real ORM path here would read/write live
tables for no reason.
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import MagicMock, patch

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


from stocks.services.shared.portfolio_risk import record_candidates
from stocks.models import SignalCandidate

print("=" * 72)
print("1. Mixed accepted + rejected candidates -> correct count and status split")
print("=" * 72)

accepted = [
    {"symbol": "RELIANCE", "signal": "BUY", "entry": 2500.0, "stop_loss": 2480.0,
     "target": 2550.0, "rank_score": 82.5, "rank_factors": {"rs": 0.9}},
    {"symbol": "TCS", "signal": "SELL", "entry": 3600.0, "stop_loss": 3620.0,
     "target": 3550.0, "rank_score": 75.0, "rank_factors": {"rs": 0.7}},
]
rejected = [
    {"symbol": "HDFCBANK", "signal": "BUY", "entry": 1600.0, "stop_loss": 1590.0,
     "target": 1630.0, "rank_score": 60.0, "rank_factors": {},
     "reject_reason": "SECTOR_CAP:Financial Services"},
    {"symbol": "ICICIBANK", "signal": "BUY", "entry": 1100.0, "stop_loss": 1090.0,
     "target": 1130.0, "rank_score": 55.0, "rank_factors": {},
     "reject_reason": "CORRELATION_CAP:cluster2"},
    {"symbol": "ADANIENT", "signal": "BUY", "entry": 2900.0, "stop_loss": 2870.0,
     "target": 2960.0, "rank_score": 50.0, "rank_factors": {},
     "reject_reason": "PROMOTER_GROUP_CAP:ADANI"},
]

fake_regime = MagicMock()
fake_regime.as_dict.return_value = {"trend": "BULLISH", "vol": "NORMAL"}

with patch.object(SignalCandidate.objects, "bulk_create") as mocked:
    record_candidates("intraday", accepted, rejected, regime=fake_regime)

    check("bulk_create called exactly once", mocked.call_count == 1, str(mocked.call_count))
    rows = list(mocked.call_args[0][0])
    check("total rows == accepted + rejected", len(rows) == 5, str(len(rows)))

    accepted_rows = [r for r in rows if r.status == SignalCandidate.Status.ACCEPTED]
    rejected_rows = [r for r in rows if r.status == SignalCandidate.Status.REJECTED]
    check("2 ACCEPTED rows", len(accepted_rows) == 2, str(len(accepted_rows)))
    check("3 REJECTED rows", len(rejected_rows) == 3, str(len(rejected_rows)))
    check("accepted rows carry no reject_reason",
          all(r.reject_reason == "" for r in accepted_rows),
          str([r.reject_reason for r in accepted_rows]))

    reasons = sorted(r.reject_reason for r in rejected_rows)
    check("rejected rows carry their exact reject_reason",
          reasons == ["CORRELATION_CAP:cluster2", "PROMOTER_GROUP_CAP:ADANI",
                      "SECTOR_CAP:Financial Services"],
          str(reasons))

    check("engine tagged on every row", all(r.engine == "intraday" for r in rows))
    check("regime_snapshot uses regime.as_dict()",
          all(r.regime_snapshot == {"trend": "BULLISH", "vol": "NORMAL"} for r in rows),
          str(rows[0].regime_snapshot))

    tcs_row = next(r for r in rows if r.symbol == "TCS")
    check("direction mapped from 'signal' key", tcs_row.direction == "SELL", tcs_row.direction)
    check("stop mapped from 'stop_loss' key", float(tcs_row.stop) == 3620.0, str(tcs_row.stop))

print()
print("=" * 72)
print("2. Candidate missing optional keys (no rank_score, no rank_factors) doesn't crash")
print("=" * 72)

sparse = [{"symbol": "WIPRO", "entry": 400.0, "stop_loss": 395.0, "target": 410.0}]

with patch.object(SignalCandidate.objects, "bulk_create") as mocked:
    try:
        record_candidates("swing", sparse, [], regime=None)
        check("no exception raised for a sparse candidate dict", True)
    except Exception as exc:
        check("no exception raised for a sparse candidate dict", False, repr(exc))

    check("bulk_create still called", mocked.call_count == 1, str(mocked.call_count))
    row = mocked.call_args[0][0][0]
    check("rank_score defaults to None", row.rank_score is None, str(row.rank_score))
    check("rank_factors defaults to {}", row.rank_factors == {}, str(row.rank_factors))
    check("direction defaults to BUY when no 'signal'/'direction' key (swing is long-only)",
          row.direction == "BUY", row.direction)
    check("regime_snapshot defaults to {} when regime is None", row.regime_snapshot == {})

print()
print("=" * 72)
print("3. Exception inside record_candidates is swallowed, logged, and never propagates")
print("=" * 72)

with patch.object(SignalCandidate.objects, "bulk_create", side_effect=RuntimeError("boom")):
    with patch("stocks.services.shared.portfolio_risk.logger") as mocked_logger:
        try:
            record_candidates("intraday", accepted, rejected, regime=fake_regime)
            check("exception from bulk_create does NOT propagate to the caller", True)
        except Exception as exc:
            check("exception from bulk_create does NOT propagate to the caller", False, repr(exc))

        check("failure is logged (logger.exception called)",
              mocked_logger.exception.call_count == 1, str(mocked_logger.exception.call_count))

# Same check but forcing the failure earlier, inside row-building (bad regime object).
class BrokenRegime:
    def as_dict(self):
        raise ValueError("regime blew up")


with patch.object(SignalCandidate.objects, "bulk_create") as mocked:
    try:
        record_candidates("intraday", accepted, rejected, regime=BrokenRegime())
        check("exception from a broken regime.as_dict() does NOT propagate", True)
    except Exception as exc:
        check("exception from a broken regime.as_dict() does NOT propagate", False, repr(exc))
    check("bulk_create never reached when row-building fails earlier",
          mocked.call_count == 0, str(mocked.call_count))

print()
print("=" * 72)
if fails:
    print(f"{len(fails)} FAILURE(S):", fails)
    sys.exit(1)
else:
    print("ALL PASS — record_candidates() is a safe, defensive, best-effort audit sink.")
