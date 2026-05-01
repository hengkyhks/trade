# ============================================================
#  DIP_DETECTOR.PY — Track harga & deteksi pola 3 dip
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
    WATCHING    = "watching"     # Baru listing, pantau dulu
    AFTER_DIP1  = "after_dip1"  # Dip 1 confirmed
    AFTER_DIP2  = "after_dip2"  # Dip 2 confirmed
    ENTRY       = "entry"        # Dip 3 confirmed + bounce → BUY
    HOLDING     = "holding"      # Sudah beli, tunggu exit
    DONE        = "done"         # Trade selesai


@dataclass
class PricePoint:
    price: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenTracker:
    address: str
    name: str
    listing_price: float

    phase: DipPhase = DipPhase.WATCHING
    prices: list = field(default_factory=list)

    ath: float = 0.0          # All-time high sejak listing
    last_local_high: float = 0.0
    last_local_low: float = 0.0
    dip_count: int = 0

    entry_price: float = 0.0
    entry_time: float = 0.0


    def update_price(self, new_price: float) -> DipPhase:
        """
        Feed harga baru, return phase saat ini.
        Panggil tiap polling interval.
        """
        self.prices.append(PricePoint(new_price))

        # Update ATH
        if new_price > self.ath:
            self.ath = new_price
            self.last_local_high = new_price

        # Kalau sudah di fase HOLDING atau DONE, skip deteksi
        if self.phase in (DipPhase.HOLDING, DipPhase.DONE):
            return self.phase

        self._detect_dip(new_price)
        return self.phase


    def _detect_dip(self, price: float):
        if self.ath == 0:
            return

        drop_from_ath = (self.ath - price) / self.ath
        drop_from_last_high = (self.last_local_high - price) / self.last_local_high if self.last_local_high > 0 else 0

        # --- Deteksi dip berdasarkan phase ---
        if self.phase == DipPhase.WATCHING:
            if drop_from_ath >= DIP1_DROP_PCT:
                self.dip_count = 1
                self.last_local_low = price
                self.phase = DipPhase.AFTER_DIP1
                log.info(f"[{self.name}] 🔴 DIP 1 detected! Drop: {drop_from_ath*100:.1f}%")

        elif self.phase == DipPhase.AFTER_DIP1:
            # Update local low
            if price < self.last_local_low:
                self.last_local_low = price
            # Tunggu bounce dulu (konfirmasi dip 1 selesai)
            bounce = (price - self.last_local_low) / self.last_local_low
            if bounce >= BOUNCE_CONFIRM_PCT:
                self.last_local_high = price  # Reset high baru
                # Sekarang tunggu dip 2
            # Deteksi dip 2
            if drop_from_last_high >= DIP2_DROP_PCT and price < self.last_local_low * 1.05:
                self.dip_count = 2
                self.last_local_low = price
                self.phase = DipPhase.AFTER_DIP2
                log.info(f"[{self.name}] 🔴 DIP 2 detected! Drop: {drop_from_last_high*100:.1f}%")

        elif self.phase == DipPhase.AFTER_DIP2:
            if price < self.last_local_low:
                self.last_local_low = price
            bounce = (price - self.last_local_low) / self.last_local_low
            if bounce >= BOUNCE_CONFIRM_PCT:
                self.last_local_high = price
            # Deteksi dip 3 + bounce = ENTRY SIGNAL
            if drop_from_last_high >= DIP3_DROP_PCT and price < self.last_local_low * 1.05:
                self.last_local_low = price
                log.info(f"[{self.name}] 🔴 DIP 3 detected! Drop: {drop_from_last_high*100:.1f}%")
            # Konfirmasi bounce setelah dip 3
            if self.last_local_low > 0:
                bounce_from_3 = (price - self.last_local_low) / self.last_local_low
                if bounce_from_3 >= BOUNCE_CONFIRM_PCT:
                    self.dip_count = 3
                    self.phase = DipPhase.ENTRY
                    log.info(f"[{self.name}] ✅ ENTRY SIGNAL! Bounce {bounce_from_3*100:.1f}% dari dip 3")


    def get_pnl(self, current_price: float) -> float:
        """Return PnL persen dari entry."""
        if self.entry_price == 0:
            return 0
        return (current_price - self.entry_price) / self.entry_price


    def summary(self, current_price: float) -> str:
        pnl = self.get_pnl(current_price) * 100
        return (
            f"Token: {self.name} | Phase: {self.phase.value} | "
            f"Price: {current_price:.8f} | ATH: {self.ath:.8f} | "
            f"Dips: {self.dip_count} | PnL: {pnl:+.1f}%"
        )
