from dataclasses import dataclass
from typing import Dict, Optional
from loguru import logger
from config import settings

@dataclass
class RiskCheck:
    approved: bool
    reason: str
    max_size_usd: float = 0.0

class RiskManager:
    def __init__(self):
        self._positions: Dict[str, dict] = {}

    def add_position(self, position_id: str, market_id: str, size_usd: float):
        self._positions[position_id] = {"market_id": market_id, "size_usd": size_usd}
        logger.info(f"Position added: {position_id} | {market_id} | ${size_usd:.2f}")

    def remove_position(self, position_id: str):
        self._positions.pop(position_id, None)
        logger.info(f"Position removed: {position_id}")

    @property
    def total_deployed_usd(self):
        return sum(p["size_usd"] for p in self._positions.values())

    @property
    def available_capital_usd(self):
        return settings.capital_total_usd - self.total_deployed_usd

    def correlated_exposure(self, market_id: str):
        return sum(p["size_usd"] for p in self._positions.values() if p["market_id"] == market_id)

    def check(self, market_id: str, requested_size_usd: Optional[float] = None) -> RiskCheck:
        capital = settings.capital_total_usd
        max_position = capital * settings.max_position_pct
        max_correlated = capital * settings.max_correlated_pct

        if self.available_capital_usd <= 0:
            return RiskCheck(approved=False, reason="No available capital", max_size_usd=0.0)

        allowed_size = min(max_position, self.available_capital_usd)
        if requested_size_usd and requested_size_usd > allowed_size:
            return RiskCheck(approved=False, reason=f"Size ${requested_size_usd:.2f} exceeds max ${allowed_size:.2f}", max_size_usd=allowed_size)

        existing_correlated = self.correlated_exposure(market_id)
        new_correlated = existing_correlated + (requested_size_usd or allowed_size)
        if new_correlated > max_correlated:
            headroom = max(0.0, max_correlated - existing_correlated)
            if headroom <= 0:
                return RiskCheck(approved=False, reason=f"Correlated exposure limit reached", max_size_usd=0.0)
            allowed_size = min(allowed_size, headroom)

        logger.info(f"Risk check PASSED — market={market_id} max_size=${allowed_size:.2f}")
        return RiskCheck(approved=True, reason="OK", max_size_usd=allowed_size)

    def status_summary(self):
        capital = settings.capital_total_usd
        deployed = self.total_deployed_usd
        return (
            f"Capital: ${capital:.0f} | "
            f"Deployed: ${deployed:.2f} ({100*deployed/capital:.1f}%) | "
            f"Positions: {len(self._positions)}"
        )
