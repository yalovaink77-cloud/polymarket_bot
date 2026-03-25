import json
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Dict, List, Optional

import websocket
from loguru import logger

from orderbook_tracker.models import OrderBookSnapshot, PriceLevel

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_HISTORY = 120

class OrderBookTracker:
    def __init__(self, token_ids: List[str], market_ids: Dict[str, str], on_snapshot: Optional[Callable] = None):
        self.token_ids = token_ids
        self.market_ids = market_ids
        self.on_snapshot = on_snapshot
        self._bids: Dict[str, Dict[float, float]] = {t: {} for t in token_ids}
        self._asks: Dict[str, Dict[float, float]] = {t: {} for t in token_ids}
        self.history: Dict[str, deque] = {t: deque(maxlen=MAX_HISTORY) for t in token_ids}
        self._ws = None
        self._thread = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        logger.info(f"OrderBookTracker started for {len(self.token_ids)} token(s)")

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()
        logger.info("OrderBookTracker stopped")

    def latest_snapshot(self, token_id: str) -> Optional[OrderBookSnapshot]:
        hist = self.history.get(token_id)
        return hist[-1] if hist else None

    def _run_ws(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                logger.error(f"WebSocket error: {exc}")
            if self._running:
                logger.warning("WebSocket disconnected — reconnecting in 5s")
                time.sleep(5)

    def _on_open(self, ws):
        logger.info("WebSocket connected")
        subscribe_msg = {
            "auth": {},
            "type": "Market",
            "markets": [],
            "assets_ids": self.token_ids,
        }
        ws.send(json.dumps(subscribe_msg))

    def _on_message(self, ws, raw: str):
        try:
            events = json.loads(raw)
            if not isinstance(events, list):
                events = [events]
            for event in events:
                self._handle_event(event)
        except json.JSONDecodeError:
            logger.warning(f"Non-JSON message: {raw[:120]}")

    def _on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws, code, msg):
        logger.info(f"WebSocket closed (code={code})")

    def _handle_event(self, event: dict):
        event_type = event.get("event_type") or event.get("type")

        if event_type == "book":
            asset_id = event.get("asset_id")
            if asset_id not in self._bids:
                return
            self._process_full_book(asset_id, event)
            self._emit_snapshot(asset_id)

        elif event_type == "price_change":
            # price_changes is a list; each item carries its own asset_id
            for change in event.get("price_changes", []):
                asset_id = change.get("asset_id")
                if asset_id not in self._bids:
                    continue
                self._process_delta(asset_id, change)
                self._emit_snapshot(asset_id)

        elif event_type == "tick_size_change":
            asset_id = event.get("asset_id")
            if asset_id not in self._bids:
                return
            self._emit_snapshot(asset_id)

    def _emit_snapshot(self, asset_id: str):
        snap = self._build_snapshot(asset_id)
        self.history[asset_id].append(snap)
        if self.on_snapshot:
            try:
                self.on_snapshot(snap)
            except Exception as exc:
                logger.error(f"on_snapshot error: {exc}")

    def _process_full_book(self, token_id: str, event: dict):
        self._bids[token_id].clear()
        self._asks[token_id].clear()
        for level in event.get("bids", []):
            price, size = float(level["price"]), float(level["size"])
            if size > 0:
                self._bids[token_id][price] = size
        for level in event.get("asks", []):
            price, size = float(level["price"]), float(level["size"])
            if size > 0:
                self._asks[token_id][price] = size

    def _process_delta(self, token_id: str, event: dict):
        side = event.get("side", "").lower()
        price = float(event.get("price", 0))
        size = float(event.get("size", 0))
        book = self._bids[token_id] if side == "buy" else self._asks[token_id]
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size

    def _build_snapshot(self, token_id: str) -> OrderBookSnapshot:
        bids = sorted([PriceLevel(price=p, size=s) for p, s in self._bids[token_id].items()], key=lambda x: -x.price)
        asks = sorted([PriceLevel(price=p, size=s) for p, s in self._asks[token_id].items()], key=lambda x: x.price)
        return OrderBookSnapshot(
            market_id=self.market_ids.get(token_id, "unknown"),
            token_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
        )
