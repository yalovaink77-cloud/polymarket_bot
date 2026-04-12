from dataclasses import dataclass
from typing import Dict, Callable


@dataclass
class TradeRecord:
    trade_id: str
    market_id: str
    token_id: str
    side: str  # "YES" veya "NO"
    entry_price: float
    size_usd: float


class PerformanceTracker:
    """
    Basit bir sanal performans takipçisi.
    Sadece dry-run trade'leri kaydeder ve anlık PnL'i yaklaşık olarak hesaplar.
    """

    def __init__(self, starting_capital_usd: float):
        self.starting_capital = starting_capital_usd
        self.trades: Dict[str, TradeRecord] = {}

    def record_trade(
        self,
        trade_id: str,
        market_id: str,
        token_id: str,
        side: str,
        entry_price: float,
        size_usd: float,
    ):
        if entry_price <= 0 or size_usd <= 0:
            return
        self.trades[trade_id] = TradeRecord(
            trade_id=trade_id,
            market_id=market_id,
            token_id=token_id,
            side=side,
            entry_price=entry_price,
            size_usd=size_usd,
        )

    def mark_to_market(self, get_price: Callable[[str], float | None]) -> dict:
        """
        Dışarıdan verilen fiyat fonksiyonu ile yaklaşık portföy PnL'i hesaplar.
        get_price(token_id) -> güncel mid fiyatı [0,1]
        """
        total_pnl = 0.0
        open_positions = 0
        for trade in self.trades.values():
            current = get_price(trade.token_id)
            if current is None or trade.entry_price <= 0:
                continue
            if trade.side.upper() == "YES":
                # YES payı entry_yes üzerinden dolar→share
                shares = trade.size_usd / trade.entry_price
                delta = current - trade.entry_price
            else:
                # NO tarafı için dolar→share dönüşümü entry_no = (1-entry_yes) üzerinden yapılmalı.
                entry_no = max(1.0 - trade.entry_price, 0.001)
                shares = trade.size_usd / entry_no
                # NO payoff = (1 - price). PnL delta = exit_no - entry_no = (1-current) - (1-entry_yes)
                delta = (1 - current) - (1 - trade.entry_price)
            pnl = shares * delta
            total_pnl += pnl
            open_positions += 1
        equity = self.starting_capital + total_pnl
        return {
            "starting_capital": self.starting_capital,
            "open_positions": open_positions,
            "pnl_usd": total_pnl,
            "equity_usd": equity,
        }

