"""The prices cost per 1,000 decisions is computed from, each with where and when it was checked.

``prices.yaml`` maps a backend name to its price. A backend with no entry, or one whose
price is null, gets no cost: nothing is guessed. Every price carries the date it was
read from the provider's page, because prices change. Self-hosted backends are priced
per GPU hour, with the measured latency.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

PRICES = Path(__file__).parent / "prices.yaml"


class Price(BaseModel):
    """One backend's price."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = None
    input_per_million_usd: float | None = None
    output_per_million_usd: float | None = None
    per_hour_usd: float | None = None
    """For a self-hosted backend: what one hour of its machine costs."""
    source: str
    checked: dt.date
    note: str = ""

    @model_validator(mode="after")
    def _one_kind(self) -> Price:
        if self.per_hour_usd is not None and self.input_per_million_usd is not None:
            msg = "a price is per token or per hour, not both"
            raise ValueError(msg)
        return self


def load_prices(path: Path = PRICES) -> dict[str, Price | None]:
    """Every recorded price; a null entry means the price is not known."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        name: None if entry is None or entry.get("unknown") else Price.model_validate(entry)
        for name, entry in raw.items()
    }


__all__ = ["PRICES", "Price", "load_prices"]
