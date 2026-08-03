from django.core.management.base import BaseCommand
from stocks.services.telegram_service import is_enabled, send_telegram_message, send_daily_picks_summary


class Command(BaseCommand):
    help = 'Send a test Telegram message to verify bot token and chat_id configuration'

    def add_arguments(self, parser):
        parser.add_argument(
            '--summary',
            action='store_true',
            help='Send the full daily picks summary instead of a test message',
        )
        parser.add_argument(
            '--mock',
            action='store_true',
            help='Simulate a live specialist signal lifecycle (creation, activation, exit)',
        )

    def handle(self, *args, **options):
        if not is_enabled():
            self.stdout.write(self.style.WARNING('Telegram alerts are disabled or credentials are missing.'))
            self.stdout.write('Set TELEGRAM_ALERTS_ENABLED=true, TELEGRAM_BOT_TOKEN, and TELEGRAM_CHAT_ID in backend/.env')
            return

        if options['summary']:
            self.stdout.write('Sending daily picks summary to Telegram...')
            success = send_daily_picks_summary()
            if success:
                self.stdout.write(self.style.SUCCESS('✅ Daily summary sent successfully!'))
            else:
                self.stdout.write(self.style.WARNING('No specialist signals found today (or send failed).'))
            return

        if options['mock']:
            self.stdout.write(self.style.SUCCESS('Simulating a live specialist Equity signal lifecycle...'))
            from stocks.models import SignalHistory
            from stocks.services.telegram_service import (
                maybe_send_telegram_new_signal,
                maybe_send_telegram_activation,
                maybe_send_telegram_exit
            )
            import time

            # 1. Create mock signal in DB (Equity only)
            mock_equity = SignalHistory.objects.create(
                symbol="RELIANCE",
                signal_type="STRANGLE",
                entry_price=2850.00,
                target=0,
                stop_loss=0,
                status=SignalHistory.Status.PENDING,
                category="specialist",
                metadata={
                    "legs": [
                        {"action": "SELL", "option_type": "CE", "strike": 2900, "sell_price": 45.50, "cmp": 45.50, "pnl": 0, "exchange": "NFO"},
                        {"action": "SELL", "option_type": "PE", "strike": 2800, "sell_price": 38.00, "cmp": 38.00, "pnl": 0, "exchange": "NFO"}
                    ],
                    "confidence": 92.5,
                    "rank": 1
                }
            )

            try:
                # ─── STEP 1: NEW SIGNAL CREATED ───
                self.stdout.write("👉 STEP 1: Sending New Signal Alert (Equity only)...")
                maybe_send_telegram_new_signal(mock_equity)
                time.sleep(2)

                # ─── STEP 2: SIGNAL ACTIVATED ───
                self.stdout.write("👉 STEP 2: Sending Signal Activation Alert...")
                # Equity Activation
                mock_equity.status = SignalHistory.Status.ACTIVE
                mock_equity.telegram_active_sent = False
                mock_equity.save()
                maybe_send_telegram_activation(mock_equity)
                time.sleep(2)

                # ─── STEP 3: SIGNAL EXITED (TARGET HIT) ───
                self.stdout.write("👉 STEP 3: Sending Target Hit Alert...")
                # Equity Exit
                mock_equity.status = SignalHistory.Status.HIT_TARGET
                mock_equity.metadata["final_pnl"] = 2505.00
                mock_equity.metadata["final_pnl_pct"] = 30.00
                mock_equity.metadata["legs"][0]["cmp"] = 31.85
                mock_equity.metadata["legs"][0]["pnl"] = 1365.00
                mock_equity.metadata["legs"][1]["cmp"] = 26.60
                mock_equity.metadata["legs"][1]["pnl"] = 1140.00
                mock_equity.telegram_exit_sent = False
                mock_equity.save()
                maybe_send_telegram_exit(mock_equity, "HIT_TARGET")

                self.stdout.write(self.style.SUCCESS("✅ All simulated steps for Equity completed! Check your Telegram channel!"))

            finally:
                # Clean up database completely
                self.stdout.write("🧹 Cleaning up mock signal from database...")
                mock_equity.delete()
                self.stdout.write(self.style.SUCCESS("✨ DB clean up complete!"))

            return

        # Send a test message
        test_msg = (
            "🧪 <b>TELEGRAM TEST MESSAGE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "✅ Your TradePulse AI bot is connected!\n"
            "\n"
            "You will receive:\n"
            "  📊 New specialist signal alerts\n"
            "  ✅ Signal activation notifications\n"
            "  🎯 Target hit / 🛑 SL hit alerts\n"
            "  📋 Daily option-selling picks summary\n"
            "\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <i>Powered by TradePulse AI</i>"
        )

        self.stdout.write('Sending test message to Telegram...')
        success = send_telegram_message(test_msg)

        if success:
            self.stdout.write(self.style.SUCCESS('✅ Telegram test message sent successfully! Check your channel.'))
        else:
            self.stdout.write(self.style.ERROR('❌ Telegram test message failed. Check bot token, chat_id, and bot admin permissions.'))

