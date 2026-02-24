from datetime import datetime
from typing import List
from pydantic import BaseModel, Field

class PriceLevel(BaseModel):
    price: float
    size: float

class OrderBookSnapshot(BaseModel):
    market_id: str
    token_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    bids: List[PriceLevel] = []
    asks: List[PriceLevel] = []

    @property
    def best_bid(self):
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self):
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self):
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self):
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def top5_bid_volume(self):
        return sum(p.size for p in self.bids[:5])

    @property
    def top5_ask_volume(self):
        return sum(p.size for p in self.asks[:5])

    @property
    def top3_bid_volume(self):
        return sum(p.size for p in self.bids[:3])

    @property
    def total_bid_volume(self):
        return sum(p.size for p in self.bids)

    @property
    def total_ask_volume(self):
        return sum(p.size for p in self.asks)
