import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional


def _utcnow() -> datetime:
    return datetime.utcnow()


def _clamp_binary_price(price: float) -> float:
    value = float(price)
    return min(max(value, 0.001), 0.999)


@dataclass
class DryRunOpenTrade:
    trade_id: str
    token_id: str
    market_id: str
    direction: str
    size_usd: float
    entry_mid_price: float
    opened_at: datetime
    resolve_at: datetime


@dataclass
class DryRunOutcome:
    trade_id: str
    token_id: str
    market_id: str
    direction: str
    size_usd: float
    entry_mid_price: float
    exit_mid_price: float
    pnl_usd: float
    return_pct: float
    won: bool
    opened_at: datetime
    closed_at: datetime
    stop_loss_hit: bool = False
    take_profit_hit: bool = False


class DryRunEvaluator:
    def __init__(
        self,
        horizon_seconds: int,
        min_trades: int,
        min_win_rate: float,
        min_net_pnl_usd: float,
        pnl_floor_price: float,
        state_file: str,
    ):
        self._horizon_seconds = max(int(horizon_seconds), 1)
        self._min_trades = max(int(min_trades), 1)
        self._min_win_rate = float(min_win_rate)
        self._min_net_pnl_usd = float(min_net_pnl_usd)
        self._pnl_floor_price = max(float(pnl_floor_price), 0.001)
        self._state_file = Path(state_file)

        self._lock = threading.Lock()
        self._open: Dict[str, DryRunOpenTrade] = {}
        self._closed_trades = 0
        self._wins = 0
        self._losses = 0
        self._net_pnl_usd = 0.0
        self._sum_return_pct = 0.0
        self._started_at = _utcnow()
        self._updated_at = _utcnow()

        self._load_state()

    @property
    def horizon_seconds(self) -> int:
        return self._horizon_seconds

    @property
    def criteria(self) -> dict:
        return {
            "min_trades": self._min_trades,
            "min_win_rate": self._min_win_rate,
            "min_net_pnl_usd": self._min_net_pnl_usd,
        }

    def record_open_trade(
        self,
        trade_id: str,
        token_id: str,
        market_id: str,
        direction: str,
        size_usd: float,
        entry_mid_price: float,
        opened_at: Optional[datetime] = None,
    ) -> bool:
        if not trade_id:
            return False
        opened_at = opened_at or _utcnow()
        resolve_at = opened_at + timedelta(seconds=self._horizon_seconds)
        trade = DryRunOpenTrade(
            trade_id=trade_id,
            token_id=token_id,
            market_id=market_id,
            direction=direction,
            size_usd=float(size_usd),
            entry_mid_price=_clamp_binary_price(entry_mid_price),
            opened_at=opened_at,
            resolve_at=resolve_at,
        )

        with self._lock:
            self._open[trade_id] = trade
            self._updated_at = _utcnow()
            self._save_state_no_lock()
        return True

    def resolve_with_snapshot(
        self,
        token_id: str,
        market_id: str,
        exit_mid_price: Optional[float],
        timestamp: Optional[datetime] = None,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
    ) -> List[DryRunOutcome]:
        if exit_mid_price is None:
            return []
        timestamp = timestamp or _utcnow()
        exit_price = _clamp_binary_price(exit_mid_price)

        outcomes: List[DryRunOutcome] = []
        with self._lock:
            for trade_id, trade in list(self._open.items()):
                if trade.token_id != token_id:
                    continue

                # Stop-loss / take-profit: horizon beklenmeden erken kapatma
                stop_loss_hit = False
                take_profit_hit = False
                if trade.size_usd > 0:
                    pnl_check = self._estimate_pnl_usd(
                        trade.direction, trade.size_usd, trade.entry_mid_price, exit_price
                    )
                    ret_check = pnl_check / trade.size_usd
                    if stop_loss_pct and stop_loss_pct > 0:
                        if ret_check <= -abs(stop_loss_pct):
                            stop_loss_hit = True
                    if take_profit_pct and take_profit_pct > 0:
                        if ret_check >= abs(take_profit_pct):
                            take_profit_hit = True

                early_exit = stop_loss_hit or take_profit_hit
                if timestamp < trade.resolve_at and not early_exit:
                    continue

                pnl = self._estimate_pnl_usd(trade.direction, trade.size_usd, trade.entry_mid_price, exit_price)
                ret = (pnl / trade.size_usd) if trade.size_usd > 0 else 0.0
                # Gerçek kazanç: en az %1 getiri olmalı (pnl >= 0 çok küçük hareketleri de win sayıyordu)
                # take_profit_hit garantili win; stop_loss_hit garantili loss
                if take_profit_hit:
                    won = True
                elif stop_loss_hit:
                    won = False
                else:
                    won = ret >= 0.01

                outcome = DryRunOutcome(
                    trade_id=trade_id,
                    token_id=trade.token_id,
                    market_id=market_id,
                    direction=trade.direction,
                    size_usd=trade.size_usd,
                    entry_mid_price=trade.entry_mid_price,
                    exit_mid_price=exit_price,
                    pnl_usd=pnl,
                    return_pct=ret,
                    won=won,
                    opened_at=trade.opened_at,
                    closed_at=timestamp,
                    stop_loss_hit=stop_loss_hit,
                    take_profit_hit=take_profit_hit,
                )
                outcomes.append(outcome)

                self._closed_trades += 1
                self._wins += int(won)
                self._losses += int(not won)
                self._net_pnl_usd += pnl
                self._sum_return_pct += ret
                self._open.pop(trade_id, None)

            if outcomes:
                self._updated_at = _utcnow()
                self._save_state_no_lock()

        return outcomes

    def ready_for_live(self) -> bool:
        summary = self.summary()
        return (
            summary["closed_trades"] >= self._min_trades
            and summary["win_rate"] >= self._min_win_rate
            and summary["net_pnl_usd"] >= self._min_net_pnl_usd
        )

    def summary(self) -> dict:
        with self._lock:
            return self._summary_no_lock()

    def _estimate_pnl_usd(self, direction: str, size_usd: float, entry_yes: float, exit_yes: float) -> float:
        entry_yes = _clamp_binary_price(entry_yes)
        exit_yes = _clamp_binary_price(exit_yes)

        if direction == "BUY_NO":
            denom = max(1.0 - entry_yes, self._pnl_floor_price)
            return size_usd * ((entry_yes - exit_yes) / denom)

        denom = max(entry_yes, self._pnl_floor_price)
        return size_usd * ((exit_yes - entry_yes) / denom)

    def _summary_no_lock(self) -> dict:
        win_rate = (self._wins / self._closed_trades) if self._closed_trades else 0.0
        avg_return_pct = (self._sum_return_pct / self._closed_trades) if self._closed_trades else 0.0
        return {
            "started_at": self._started_at.isoformat(),
            "updated_at": self._updated_at.isoformat(),
            "horizon_seconds": self._horizon_seconds,
            "open_trades": len(self._open),
            "closed_trades": self._closed_trades,
            "wins": self._wins,
            "losses": self._losses,
            "win_rate": win_rate,
            "net_pnl_usd": self._net_pnl_usd,
            "avg_return_pct": avg_return_pct,
            "criteria": self.criteria,
            "ready_for_live": (
                self._closed_trades >= self._min_trades
                and win_rate >= self._min_win_rate
                and self._net_pnl_usd >= self._min_net_pnl_usd
            ),
            "pending_trade_ids": list(self._open.keys()),
        }

    def _save_state_no_lock(self):
        payload = self._summary_no_lock()
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_state(self):
        if not self._state_file.exists():
            return

        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return

        with self._lock:
            self._closed_trades = int(data.get("closed_trades", 0))
            self._wins = int(data.get("wins", 0))
            self._losses = int(data.get("losses", 0))
            self._net_pnl_usd = float(data.get("net_pnl_usd", 0.0))
            self._sum_return_pct = float(data.get("avg_return_pct", 0.0)) * max(self._closed_trades, 0)

            started_at = data.get("started_at")
            updated_at = data.get("updated_at")
            try:
                if started_at:
                    self._started_at = datetime.fromisoformat(started_at)
                if updated_at:
                    self._updated_at = datetime.fromisoformat(updated_at)
            except Exception:
                self._started_at = _utcnow()
                self._updated_at = _utcnow()
