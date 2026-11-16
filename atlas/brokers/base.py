"""Broker interface. Every adapter must report BROKER-CONFIRMED state; nothing here may assume a fill."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float


@dataclass
class Account:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    currency: str = "GBP"


@dataclass
class OrderStatus:
    broker_order_id: str
    status: str            # submitted | partially_filled | filled | cancelled | rejected
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    commission: float = 0.0
    message: str = ""


class Broker(ABC):
    name: str = "base"
    is_live: bool = False

    @abstractmethod
    def health(self) -> dict: ...

    @abstractmethod
    def account(self) -> Account: ...

    @abstractmethod
    def submit_market(self, symbol: str, side: str, qty: float, ref_price: float,
                      ref: str | None = None, limit_price: float | None = None) -> OrderStatus:
        """Market order, or - when `limit_price` (GBP) is given - a marketable limit order capped there."""

    def find_by_ref(self, ref: str) -> OrderStatus | None:
        """Look up an order by the platform's own reference (restart recovery). None = not found."""
        return None

    @abstractmethod
    def order_status(self, broker_order_id: str) -> OrderStatus: ...

    def cancel(self, broker_order_id: str) -> None:  # optional
        pass

    def close(self) -> None:  # release connections
        pass

    def cancel_all(self) -> int:
        """Cancel every working order at the broker; returns how many were working."""
        return 0
