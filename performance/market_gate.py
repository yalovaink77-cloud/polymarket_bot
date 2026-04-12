import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from loguru import logger


@dataclass
class MarketPerf:
    trades: int = 0
    wins: int = 0
    net_pnl_usd: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades else 0.0


class MarketPerformanceGate:
    """
    Dry-run trade_history.jsonl üzerinden token bazlı performans çıkarıp,
    kötü performanslı marketlerde yeni trade açmayı kısar.

    Varsayılan hedef: botun davranışını bozmamak için "soft gate" gibi kullanılmalı:
    main.py içinde sadece ENV ile açılınca devreye alınır.
    """

    def __init__(
        self,
        history_path: str = "logs/trade_history.jsonl",
        min_trades: int = 20,
        min_win_rate: float = 0.50,
        min_net_pnl_usd: float = 0.0,
        cache_seconds: float = 60.0,
    ):
        self._path = Path(history_path)
        self._min_trades = max(int(min_trades), 1)
        self._min_win_rate = float(min_win_rate)
        self._min_net_pnl_usd = float(min_net_pnl_usd)
        self._cache_seconds = float(cache_seconds)

        self._last_load_at = 0.0
        self._stats: Dict[str, MarketPerf] = {}

    def _load_if_needed(self):
        now = time.time()
        if self._last_load_at and (now - self._last_load_at) < self._cache_seconds:
            return
        self._last_load_at = now

        if not self._path.exists():
            self._stats = {}
            return

        stats: Dict[str, MarketPerf] = {}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    token_id = str(rec.get("token_id") or "")
                    if not token_id:
                        continue
                    pnl = float(rec.get("pnl_usd") or 0.0)
                    won = bool(rec.get("won"))
                    s = stats.get(token_id) or MarketPerf()
                    s.trades += 1
                    s.wins += int(won)
                    s.net_pnl_usd += pnl
                    stats[token_id] = s
        except Exception as exc:
            logger.warning(f"MarketPerformanceGate load failed: {exc}")
            self._stats = {}
            return

        self._stats = stats

    def stats_for(self, token_id: str) -> Optional[MarketPerf]:
        self._load_if_needed()
        return self._stats.get(str(token_id))

    def allowed(self, token_id: str) -> bool:
        """
        True → trade açılabilir.
        False → performans kriterleri sağlanmadığı için trade engellenmeli.
        """
        s = self.stats_for(token_id)
        if not s or s.trades < self._min_trades:
            return True
        if s.win_rate < self._min_win_rate:
            return False
        if s.net_pnl_usd < self._min_net_pnl_usd:
            return False
        return True

