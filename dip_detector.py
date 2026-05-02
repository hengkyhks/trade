# ============================================================
#  DIP_DETECTOR.PY — Track harga & deteksi pola 3-dip (FIXED)
#
#  FIX:
#  - ath / last_local_high diinit dari listing_price di __post_init__
#  - Logic dip 2 tidak false-trigger di iterasi yang sama dengan bounce
#  - Flag _dip3_seen memastikan entry hanya setelah dip3 + bounce
#  - get_pnl() aman jika entry_price = 0
# ============================================================
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from config import (
    DIP1_DROP_PCT, DIP2_DROP_PCT, DIP3_DROP_PCT,
    BOUNCE_CONFIRM_PCT
)

log = logging.getLogger("dip_detector")


class DipPhase(Enum):
    WATCHING   = "watching"    # Baru listing, pantau
    AFTER_DIP1 = "after_dip1" # Dip 1 confirmed
    AFTER_DIP2 = "after_dip2" # Dip 2 confirmed
    ENTRY      = "entry"       # Dip 3 + bounce → BUY
    HOLDING    = "holding"     # Sudah beli, tunggu exit
    DONE       = "done"        # Trade selesai


@dataclass
class PricePoint:
    price: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenTracker:
    address: str
    name: str
    listing_price: float
    migration_type: str = "PumpSwap"   # "PumpSwap" atau "Raydium"

    phase: DipPhase = DipPhase.WATCHING
    prices: list    = field(default_factory=list)

    ath:              float = 0.0
    last_local_high:  float = 0.0
    last_local_low:   float = 0.0
    dip_count:        int   = 0
    _dip3_seen:       bool  = field(default=False, repr=False)

    entry_price: float = 0.0
    entry_time:  float = 0.0

    def __post_init__(self):
        if self.listing_price > 0:
            if self.ath == 0.0:
                self.ath = self.listing_price
            if self.last_local_high == 0.0:
                self.last_local_high = self.listing_price

    def update_price(self, new_price: float) -> DipPhase:
        if new_price <= 0:
            return self.phase

        self.prices.append(PricePoint(new_price))

        if new_price > self.ath:
            self.ath = new_price
            self.last_local_high = new_price

        if self.phase in (DipPhase.HOLDING, DipPhase.DONE):
            return self.phase

        self._detect_dip(new_price)
        return self.phase

    def _detect_dip(self, price: float):
        if self.ath == 0 or self.last_local_high == 0:
            return

        drop_from_ath        = (self.ath - price) / self.ath
        drop_from_local_high = (
            (self.last_local_high - price) / self.last_local_high
            if self.last_local_high > 0 else 0
        )

        # ── WATCHING → AFTER_DIP1 ───────────────────────────
        if self.phase == DipPhase.WATCHING:
            if drop_from_ath >= DIP1_DROP_PCT:
                self.dip_count      = 1
                self.last_local_low = price
                self.phase          = DipPhase.AFTER_DIP1
                log.info(
                    f"[{self.name}] 🔴 DIP 1! "
                    f"Drop ATH: {drop_from_ath*100:.1f}%"
                )

        # ── AFTER_DIP1 → AFTER_DIP2 ─────────────────────────
        elif self.phase == DipPhase.AFTER_DIP1:
            if price < self.last_local_low:
                self.last_local_low = price

            bounce = (
                (price - self.last_local_low) / self.last_local_low
                if self.last_local_low > 0 else 0
            )
            # Bounce dip1 → update local high baru
            if bounce >= BOUNCE_CONFIRM_PCT and price > self.last_local_high:
                self.last_local_high = price

            # Dip 2: harus dari local high BARU (setelah bounce dip1)
            # dan local high harus di bawah ATH (artinya bounce belum recovery)
            if (
                drop_from_local_high >= DIP2_DROP_PCT
                and price < self.last_local_low * 1.05
                and self.last_local_high < self.ath * 0.95
            ):
                self.dip_count      = 2
                self.last_local_low = price
                self.phase          = DipPhase.AFTER_DIP2
                log.info(
                    f"[{self.name}] 🔴 DIP 2! "
                    f"Drop local high: {drop_from_local_high*100:.1f}%"
                )

        # ── AFTER_DIP2 → ENTRY ──────────────────────────────
        elif self.phase == DipPhase.AFTER_DIP2:
            if price < self.last_local_low:
                self.last_local_low = price

            bounce = (
                (price - self.last_local_low) / self.last_local_low
                if self.last_local_low > 0 else 0
            )
            # Bounce dip2 → update local high untuk dip3
            if bounce >= BOUNCE_CONFIRM_PCT and price > self.last_local_high:
                self.last_local_high = price

            # Set flag dip3 (belum ENTRY, tunggu bounce konfirmasi)
            if (
                not self._dip3_seen
                and drop_from_local_high >= DIP3_DROP_PCT
                and price < self.last_local_low * 1.05
                and self.last_local_high < self.ath * 0.95
            ):
                self._dip3_seen     = True
                self.last_local_low = price
                log.info(
                    f"[{self.name}] 🔴 DIP 3! "
                    f"Drop: {drop_from_local_high*100:.1f}% — "
                    f"Tunggu bounce..."
                )

            # Bounce setelah dip3 = ENTRY SIGNAL
            if self._dip3_seen and self.last_local_low > 0:
                bounce3 = (price - self.last_local_low) / self.last_local_low
                if bounce3 >= BOUNCE_CONFIRM_PCT:
                    self.dip_count = 3
                    self.phase     = DipPhase.ENTRY
                    log.info(
                        f"[{self.name}] ✅ ENTRY! "
                        f"Bounce {bounce3*100:.1f}% dari dip3"
                    )

    def get_pnl(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price

    def summary(self, current_price: float) -> str:
        pnl = self.get_pnl(current_price) * 100
        return (
            f"[{self.migration_type}] {self.name} | "
            f"Phase: {self.phase.value} | "
            f"Price: {current_price:.8f} | ATH: {self.ath:.8f} | "
            f"Dips: {self.dip_count} | PnL: {pnl:+.1f}%"
        )
