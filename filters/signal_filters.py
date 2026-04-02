from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np
from loguru import logger
from config import settings
from orderbook_tracker.models import OrderBookSnapshot

@dataclass
class SignalResult:
    token_id: str
    market_id: str
    imbalance_score: float = 0.0
    depth_ratio_score: float = 0.0
    zscore_score: float = 0.0
    overreaction_score: float = 0.0
    spread_zscore_score: float = 0.0
    concentration_score: float = 0.0
    imbalance_ratio: float = 0.0
    depth_ratio: float = 0.0
    mid_zscore: float = 0.0
    overreaction_ratio: float = 0.0
    spread_zscore: float = 0.0
    top3_concentration: float = 0.0
    composite_score: float = 0.0
    should_alert: bool = False
    # Performans takibi için ek alanlar
    entry_price: float = 0.0
    side: str = ""
    direction_type: str = ""  # "MOMENTUM" veya "MEAN-REV"
    price_direction: float = 0.0  # pozitif = fiyat EMA üzerinde, negatif = altında

    WEIGHTS: dict = field(default_factory=lambda: {
        # Aşırı uç orderbook dengesizliklerine daha az, fiyat hareketine biraz daha fazla ağırlık ver
        "imbalance": 0.15,
        "depth_ratio": 0.15,
        "zscore": 0.30,
        "overreaction": 0.20,
        "spread_zscore": 0.10,
        "concentration": 0.10,
    })

    def compute_composite(self):
        w = self.WEIGHTS
        self.composite_score = (
            w["imbalance"] * self.imbalance_score
            + w["depth_ratio"] * self.depth_ratio_score
            + w["zscore"] * self.zscore_score
            + w["overreaction"] * self.overreaction_score
            + w["spread_zscore"] * self.spread_zscore_score
            + w["concentration"] * self.concentration_score
        )
        # Eğer en üst 3 bid'e yoğunlaşma çok düşükse, sinyali gürültü olarak hafifçe cezalandır
        if self.top3_concentration and self.top3_concentration < settings.top3_concentration_min * 0.3:
            self.composite_score *= 0.7
        self.should_alert = self.composite_score >= settings.composite_score_min

    def summary(self):
        return (
            f"[{self.market_id}] composite={self.composite_score:.3f} "
            f"imbalance={self.imbalance_ratio:.2f} "
            f"depth={self.depth_ratio:.2f} "
            f"z={self.mid_zscore:.2f} "
            f"overreact={self.overreaction_ratio:.2f} "
            f"spread_z={self.spread_zscore:.2f} "
            f"conc={self.top3_concentration:.2f}"
        )

class SignalFilter:
    HISTORY_SIZE = 60
    # Spread z-score sinyalleri için minimum toplam derinlik (çok sığ defterleri filtrelemek için)
    MIN_LIQUIDITY_FOR_SPREAD = 50.0

    def __init__(self, token_id: str, market_id: str):
        self.token_id = token_id
        self.market_id = market_id
        self._mid_history: List[float] = []
        self._spread_history: List[float] = []

    def evaluate(self, snapshot: OrderBookSnapshot) -> Optional[SignalResult]:
        if snapshot.mid_price is None:
            return None
        self._mid_history.append(snapshot.mid_price)
        if snapshot.spread is not None:
            self._spread_history.append(snapshot.spread)
        if len(self._mid_history) > self.HISTORY_SIZE:
            self._mid_history = self._mid_history[-self.HISTORY_SIZE:]
        if len(self._spread_history) > self.HISTORY_SIZE:
            self._spread_history = self._spread_history[-self.HISTORY_SIZE:]
        if len(self._mid_history) < 10:
            return None

        result = SignalResult(token_id=self.token_id, market_id=self.market_id)
        self._filter_imbalance(snapshot, result)
        self._filter_depth_ratio(snapshot, result)
        self._filter_mid_zscore(result)
        self._filter_overreaction(snapshot, result)
        self._filter_spread_zscore(result)
        self._filter_concentration(snapshot, result)

        # Çok sığ defter + yüksek spread z-score kombinasyonlarında spread sinyalini zayıflat
        total_liquidity = snapshot.total_bid_volume + snapshot.total_ask_volume
        if (
            total_liquidity < self.MIN_LIQUIDITY_FOR_SPREAD
            and result.spread_zscore > settings.spread_zscore_threshold
        ):
            logger.debug(
                f"Skipping spread component for {self.market_id} due to low liquidity: "
                f"liq={total_liquidity:.2f}, spread_z={result.spread_zscore:.2f}"
            )
            result.spread_zscore = 0.0
            result.spread_zscore_score = 0.0

        result.compute_composite()

        if result.should_alert:
            logger.info(f"ALERT — {result.summary()}")
        else:
            logger.debug(
                f"[{self.market_id[:10]}] composite={result.composite_score:.3f} "
                f"(imb={result.imbalance_score:.2f} depth={result.depth_ratio_score:.2f} "
                f"z={result.zscore_score:.2f} over={result.overreaction_score:.2f} "
                f"spr={result.spread_zscore_score:.2f} conc={result.concentration_score:.2f}) "
                f"history={len(self._mid_history)}"
            )
        return result

    def _filter_imbalance(self, snap, result):
        ask_vol = snap.top5_ask_volume
        if ask_vol == 0:
            return
        raw_ratio = snap.top5_bid_volume / ask_vol
        # Aşırı uç değerleri skorlamada normalize et (ör. 50x yerine 10x)
        ratio = max(min(raw_ratio, 10.0), 0.1)
        result.imbalance_ratio = raw_ratio
        if ratio > settings.imbalance_high or ratio < settings.imbalance_low:
            result.imbalance_score = 1.0
        else:
            distance = abs(ratio - 1.0)
            neutral_max = max(settings.imbalance_high - 1.0, 1.0 - settings.imbalance_low)
            result.imbalance_score = min(distance / neutral_max, 1.0)

    def _filter_depth_ratio(self, snap, result):
        total = snap.total_bid_volume + snap.total_ask_volume
        if total == 0:
            return
        minority = min(snap.total_bid_volume, snap.total_ask_volume)
        ratio = minority / total
        result.depth_ratio = ratio
        if ratio < settings.depth_ratio_min:
            result.depth_ratio_score = 1.0
        else:
            result.depth_ratio_score = max(0.0, min((settings.depth_ratio_min - ratio + 0.1) / 0.1, 1.0))

    def _filter_mid_zscore(self, result):
        prices = np.array(self._mid_history)
        if len(prices) < 10:
            return
        span = min(30, len(prices))
        alpha = 2 / (span + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = alpha * p + (1 - alpha) * ema
        std = prices.std()
        if std == 0:
            return
        delta = prices[-1] - ema
        zscore = abs(delta) / std
        result.mid_zscore = zscore
        result.zscore_score = min(zscore / settings.zscore_threshold, 1.0)
        result.price_direction = delta

    def _filter_overreaction(self, snap, result):
        prices = np.array(self._mid_history)
        if len(prices) < 15:
            return
        recent_change = abs(prices[-1] - prices[-15])
        rolling_vol = prices.std()
        if rolling_vol == 0:
            return
        ratio = recent_change / rolling_vol
        result.overreaction_ratio = ratio
        result.overreaction_score = min(ratio / settings.overreaction_threshold, 1.0)

    def _filter_spread_zscore(self, result):
        spreads = np.array(self._spread_history)
        if len(spreads) < 10:
            return
        mean_spread = spreads.mean()
        std_spread = spreads.std()
        if std_spread == 0:
            return
        spread_z = abs(spreads[-1] - mean_spread) / std_spread
        result.spread_zscore = spread_z
        result.spread_zscore_score = min(spread_z / settings.spread_zscore_threshold, 1.0)

    def _filter_concentration(self, snap, result):
        total_bid = snap.total_bid_volume
        if total_bid == 0:
            return
        conc = snap.top3_bid_volume / total_bid
        result.top3_concentration = conc
        result.concentration_score = min(conc / settings.top3_concentration_min, 1.0)
