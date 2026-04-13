

from dataclasses import dataclass
from typing import Dict, Optional
from loguru import logger
from config import settings

def _kelly_size(win_prob: float, entry_price: float, available_capital: float) -> float:
    """
    Half-Kelly pozisyon büyüklüğü hesaplar.
    win_prob : tahmini kazanma olasılığı (0–1)
    entry_price : kontrat mid fiyatı (0.01–0.99)
    """
    if entry_price <= 0.01 or entry_price >= 0.99 or win_prob <= 0.50:
        return 0.0
    b = (1.0 - entry_price) / entry_price  # net payout oranı
    q = 1.0 - win_prob
    kelly_full = (b * win_prob - q) / b
    kelly_half = max(0.0, kelly_full / 2.0)  # yarım Kelly — daha güvenli
    return round(kelly_half * available_capital, 2)

def _kelly_size(win_prob: float, entry_price: float, available_capital: float) -> float:
    """
    Half-Kelly pozisyon büyüklüğü hesaplar.
    win_prob : tahmini kazanma olasılığı (0–1)
    entry_price : kontrat mid fiyatı (0.01–0.99)
    """
    if entry_price <= 0.01 or entry_price >= 0.99 or win_prob <= 0.50:
        return 0.0
    b = (1.0 - entry_price) / entry_price  # net payout oranı
    q = 1.0 - win_prob
    kelly_full = (b * win_prob - q) / b
    kelly_half = max(0.0, kelly_full / 2.0)  # yarım Kelly — daha güvenli
    return round(kelly_half * available_capital, 2)

@dataclass
class RiskCheck:
    approved: bool
    reason: str
    max_size_usd: float = 0.0

class RiskManager:
    def status_summary(self):
        capital = settings.capital_total_usd
        deployed = self.total_deployed_usd
        return (
            f"Capital: ${capital:.0f} | "
            f"Deployed: ${deployed:.2f} ({100*deployed/capital:.1f}%) | "
            f"Positions: {len(self._positions)}"
        )

    def __init__(self):
        self._positions: Dict[str, dict] = {}

    def add_position(self, position_id: str, market_id: str, size_usd: float):
        self._positions[position_id] = {"market_id": market_id, "size_usd": size_usd}
        logger.info(f"Position added: {position_id} | {market_id} | ${size_usd:.2f}")

    def remove_position(self, position_id: str):
        self._positions.pop(position_id, None)
        logger.info(f"Position removed: {position_id}")

    @property
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

        def check(
            self,
            market_id: str,
            requested_size_usd: Optional[float] = None,
            composite_score: float = 0.0,
            entry_price: float = 0.5,
        ) -> RiskCheck:
            capital = settings.capital_total_usd
            max_position = capital * settings.max_position_pct
            max_correlated = capital * settings.max_correlated_pct

            if self.available_capital_usd <= 0:
                return RiskCheck(approved=False, reason="No available capital", max_size_usd=0.0)

            allowed_size = min(max_position, self.available_capital_usd)

            # --- Kelly boyutu ---
            # composite_score (0.75–1.0) → win_prob (0.55–0.75) aralığına map et
            if composite_score >= 0.75:
                win_prob = 0.50 + (composite_score * 0.25)
                win_prob = max(0.51, min(win_prob, 0.75))
                kelly = _kelly_size(win_prob, entry_price, self.available_capital_usd)
                if kelly > 0:
                    allowed_size = min(allowed_size, kelly)
                    logger.info(
                        f"Kelly applied: ${kelly:.2f} "
                        f"(composite={composite_score:.2f} win_prob={win_prob:.2f} entry={entry_price:.3f})"
                    )
            # --------------------

            if requested_size_usd and requested_size_usd > allowed_size:
                return RiskCheck(
                    approved=False,
                    reason=f"Size ${requested_size_usd:.2f} exceeds max ${allowed_size:.2f}",
                    max_size_usd=allowed_size,
                )

            existing_correlated = self.correlated_exposure(market_id)
            new_correlated = existing_correlated + (requested_size_usd or allowed_size)
            if new_correlated > max_correlated:
                headroom = max(0.0, max_correlated - existing_correlated)
                if headroom <= 0:
                    return RiskCheck(approved=False, reason="Correlated exposure limit reached", max_size_usd=0.0)
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
