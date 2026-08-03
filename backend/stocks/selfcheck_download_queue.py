"""Phase 3 parity/behavior check for stocks.services.market_data.download_queue
(doc/MARKET_DATA_ENGINE_ARCHITECTURE.md §2, §9, §12).

Covers: enqueue dedup via the partial unique constraint, drain_once marking
DONE/FAILED correctly, attempts/last_error bookkeeping on failure, and
batch_limit bounding a single drain call. Mocks `market_data.gateway.request_candles`
and `svc.get_token_map` — never hits the real network or a real candle fetch.

This DOES touch the real ORM (creates/deletes DownloadRequest rows), same as any
other standalone script test in this app (e.g. selfcheck_market_data_gateway.py mocks
the network boundary but still runs against DATABASE_URL, which locally points
directly at production Supabase) — so every row created here is deleted at the
end of each section, and the model is otherwise inert (nothing reads/writes it
yet outside this queue).
"""
import os
import sys
import django

sys.path.insert(0, "/home/jd/tradeplusai/tradepulse-ai/backend")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import MagicMock, patch

from stocks.models import DownloadRequest

fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


def cleanup(symbol_prefix="ZZTEST_"):
    DownloadRequest.objects.filter(symbol__startswith=symbol_prefix).delete()


if __name__ == "__main__":
    from stocks.services.market_data import download_queue

    cleanup()

    print("=" * 72)
    print("1. enqueue() dedup — repeat calls while PENDING reuse the same row")
    print("=" * 72)

    row1 = download_queue.enqueue("ZZTEST_ABC", "NSE", "FIVE_MINUTE", requested_by="test")
    row2 = download_queue.enqueue("ZZTEST_ABC", "NSE", "FIVE_MINUTE", requested_by="test")
    count = DownloadRequest.objects.filter(symbol="ZZTEST_ABC", exchange="NSE", interval="FIVE_MINUTE").count()
    check("second enqueue() returns the same row id", row1.id == row2.id, f"{row1.id} vs {row2.id}")
    check("only one row exists in the DB", count == 1, str(count))
    cleanup()

    print()
    print("=" * 72)
    print("2. drain_once() marks a mocked-successful fetch as DONE")
    print("=" * 72)

    req_done = DownloadRequest.objects.create(symbol="ZZTEST_DONE", exchange="NSE", interval="FIVE_MINUTE")
    fake_svc = MagicMock(name="TrueDataService")
    fake_svc.get_token_map.return_value = {"ZZTEST_DONE": "99999"}

    with patch("stocks.services.market_data.download_queue.gateway.request_candles") as mocked_rc:
        summary = download_queue.drain_once(fake_svc, batch_limit=200)

    req_done.refresh_from_db()
    check("row marked DONE", req_done.status == DownloadRequest.Status.DONE, req_done.status)
    check("request_candles called once", mocked_rc.call_count == 1, str(mocked_rc.call_count))
    check("summary counts 1 done", summary.get("done") == 1, str(summary))
    cleanup()

    print()
    print("=" * 72)
    print("3. drain_once() marks a mocked-failing fetch as FAILED, attempts++, last_error set")
    print("=" * 72)

    req_fail = DownloadRequest.objects.create(symbol="ZZTEST_FAIL", exchange="NSE", interval="FIVE_MINUTE")
    fake_svc2 = MagicMock(name="TrueDataService")
    fake_svc2.get_token_map.return_value = {"ZZTEST_FAIL": "88888"}

    with patch(
        "stocks.services.market_data.download_queue.gateway.request_candles",
        side_effect=RuntimeError("boom: simulated fetch failure"),
    ):
        summary2 = download_queue.drain_once(fake_svc2, batch_limit=200)

    req_fail.refresh_from_db()
    check("row marked FAILED", req_fail.status == DownloadRequest.Status.FAILED, req_fail.status)
    check("attempts incremented to 1", req_fail.attempts == 1, str(req_fail.attempts))
    check("last_error captured", "boom" in req_fail.last_error, req_fail.last_error)
    check("summary counts 1 failed", summary2.get("failed") == 1, str(summary2))
    check("drain_once did not raise", True)
    cleanup()

    print()
    print("=" * 72)
    print("4. batch_limit bounds a single drain_once() call")
    print("=" * 72)

    made = [
        DownloadRequest.objects.create(symbol=f"ZZTEST_BATCH_{i}", exchange="NSE", interval="FIVE_MINUTE")
        for i in range(5)
    ]
    fake_svc3 = MagicMock(name="TrueDataService")
    fake_svc3.get_token_map.return_value = {r.symbol: str(10000 + i) for i, r in enumerate(made)}

    with patch("stocks.services.market_data.download_queue.gateway.request_candles") as mocked_rc3:
        summary3 = download_queue.drain_once(fake_svc3, batch_limit=2)

    touched = DownloadRequest.objects.filter(symbol__startswith="ZZTEST_BATCH_").exclude(status=DownloadRequest.Status.PENDING).count()
    still_pending = DownloadRequest.objects.filter(symbol__startswith="ZZTEST_BATCH_", status=DownloadRequest.Status.PENDING).count()
    check("only batch_limit rows touched", touched == 2, str(touched))
    check("remaining rows left PENDING for next drain", still_pending == 3, str(still_pending))
    check("summary picked == batch_limit", summary3.get("picked") == 2, str(summary3))
    cleanup()

    print()
    print("=" * 72)
    if fails:
        print(f"{len(fails)} FAILURE(S):", fails)
        sys.exit(1)
    else:
        print("ALL PASS — download_queue enqueue/drain mechanics verified.")
