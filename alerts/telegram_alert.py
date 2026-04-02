import asyncio
from typing import Callable, Optional
from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from config import settings

class TelegramAlert:
    def __init__(self, on_approved: Optional[Callable] = None):
        self._app = None
        self._pending: dict = {}
        self.on_approved = on_approved

    async def start(self):
        self._app = Application.builder().token(settings.telegram_bot_token).build()
        await self._app.initialize()
        # Approval gerekmiyorsa polling başlatma — başka instance çakışmasını önler
        if settings.require_telegram_approval:
            self._app.add_handler(CallbackQueryHandler(self._handle_callback))
            await self._app.start()
            await self._app.updater.start_polling()
            logger.info("Telegram bot started (polling active)")
        else:
            await self._app.start()
            logger.info("Telegram bot started (send-only, no polling)")

    async def stop(self):
        if self._app:
            if settings.require_telegram_approval:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send_signal_alert(self, alert_id: str, signal, risk):
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[alert_id] = future

        direction = "BUY YES" if signal.imbalance_ratio > 1.0 else "BUY NO"
        text = (
            f"🚨 *Signal Alert*\n\n"
            f"Market: `{signal.market_id}`\n"
            f"Direction: *{direction}*\n\n"
            f"📊 *Scores*\n"
            f"• Composite: `{signal.composite_score:.3f}`\n"
            f"• Imbalance: `{signal.imbalance_ratio:.2f}`\n"
            f"• Depth ratio: `{signal.depth_ratio:.2f}`\n"
            f"• Mid Z-score: `{signal.mid_zscore:.2f}`\n"
            f"• Overreaction: `{signal.overreaction_ratio:.2f}`\n"
            f"• Spread Z: `{signal.spread_zscore:.2f}`\n"
            f"• Top3 conc: `{signal.top3_concentration:.2f}`\n\n"
            f"💰 Max allowed: `${risk.max_size_usd:.2f}`\n"
            f"Alert ID: `{alert_id}`"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ APPROVE", callback_data=f"approve:{alert_id}"),
            InlineKeyboardButton("❌ REJECT", callback_data=f"reject:{alert_id}"),
        ]])
        await self._app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        logger.info(f"Alert sent: {alert_id}")
        return future

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if ":" not in data:
            return
        action, alert_id = data.split(":", 1)
        future = self._pending.pop(alert_id, None)
        if future is None:
            await query.edit_message_text(f"⚠️ Alert `{alert_id}` already handled.")
            return
        if action == "approve":
            future.set_result(True)
            await query.edit_message_text(query.message.text + "\n\n✅ *APPROVED*", parse_mode="Markdown")
            logger.info(f"Trade APPROVED: {alert_id}")
            if self.on_approved:
                self.on_approved(alert_id)
        else:
            future.set_result(False)
            await query.edit_message_text(query.message.text + "\n\n❌ *REJECTED*", parse_mode="Markdown")
            logger.info(f"Trade REJECTED: {alert_id}")

    async def send_message(self, text: str):
        if self._app:
            try:
                await self._app.bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=text,
                    parse_mode="Markdown",
                )
            except Exception:
                # Markdown parse hatası olabilir, düz metin olarak tekrar dene
                try:
                    await self._app.bot.send_message(
                        chat_id=settings.telegram_chat_id,
                        text=text,
                    )
                except Exception as e:
                    logger.error(f"Telegram send_message failed: {e}")
