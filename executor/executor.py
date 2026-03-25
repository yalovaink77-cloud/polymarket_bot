import uuid
from typing import Optional
from loguru import logger
from config import settings
from risk_manager.risk_manager import RiskCheck, RiskManager

class TradeExecutor:
    def __init__(self, risk_manager: RiskManager):
        self._risk = risk_manager
        self._client = self._init_client()

    def _init_client(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=settings.poly_api_key,
                api_secret=settings.poly_api_secret,
                api_passphrase=settings.poly_api_passphrase,
            )
            client = ClobClient(
                host="https://clob.polymarket.com",
                key=settings.poly_private_key,
                chain_id=137,
                creds=creds,
            )
            logger.info("ClobClient initialized")
            return client
        except Exception as exc:
            logger.error(f"ClobClient init failed: {exc}")
            return None

    def execute_trade(self, signal, risk_check: RiskCheck, size_usd: Optional[float] = None) -> Optional[str]:
        if self._client is None:
            logger.error("ClobClient not initialized")
            return None
        trade_size = min(size_usd or risk_check.max_size_usd, risk_check.max_size_usd)
        if trade_size <= 0:
            logger.error("Trade size is 0 — aborting")
            return None
        try:
            from py_clob_client.clob_types import MarketOrderArgs
            order_args = MarketOrderArgs(token_id=signal.token_id, amount=trade_size)
            resp = self._client.create_and_post_order(order_args)
            order_id = resp.get("orderID") or str(uuid.uuid4())
            self._risk.add_position(position_id=order_id, market_id=signal.market_id, size_usd=trade_size)
            logger.info(f"Trade executed | order_id={order_id} | size=${trade_size:.2f}")
            return order_id
        except Exception as exc:
            logger.error(f"Trade execution failed: {exc}")
            return None

    def dry_run(self, signal, risk_check: RiskCheck) -> str:
        fake_id = f"dry-{uuid.uuid4().hex[:8]}"
        logger.info(f"[DRY RUN] market={signal.market_id} size=${risk_check.max_size_usd:.2f} | id={fake_id}")
        self._risk.add_position(position_id=fake_id, market_id=signal.market_id, size_usd=risk_check.max_size_usd)
        return fake_id
