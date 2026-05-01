# ============================================================
#  BOT.PY — Main orchestrator (fixed: dedup + raydium wait)
# ============================================================
import sys
import time
import logging
import threading

from config import (
    POLL_INTERVAL_SEC, TAKE_PROFIT_PCT, STOP_LOSS_PCT,
    MAX_POSITIONS, LOG_FILE, BUY_AMOUNT_SOL
)
from monitor import PumpFunListener, get_current_price, is_on_raydium, get_tokens_near_bond
from filters import is_token_safe
from dip_detector import TokenTracker, DipPhase
from trader import buy_token, sell_token, get_token_balance

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("bot")

# ── State global ────────────────────────────────────────────
active_trackers: dict[str, TokenTracker] = {}
positions: dict[str, dict] = {}
seen_addresses: set[str] = set()   # <-- dedup cache
tracker_lock = threading.Lock()

RAYDIUM_WAIT_MAX = 60   # detik maksimal tunggu token masuk Raydium
RAYDIUM_POLL_SEC = 3    # cek tiap 3 detik


def on_token_bonded(token_info: dict):
    address = token_info.get("address")
    name    = token_info.get("name", "Unknown")
    if not address:
        return

    with tracker_lock:
        # ── DEDUP: skip kalau sudah pernah diproses ──
        if address in seen_addresses:
            return
        seen_addresses.add(address)

        if len(positions) >= MAX_POSITIONS:
            log.info(f"[SKIP] {name}: max posisi ({MAX_POSITIONS}) tercapai")
            return

    log.info(f"[NEW] Token bond: {name} ({address})")

    # Jalankan di thread terpisah supaya tidak blocking
    t = threading.Thread(target=_process_token, args=(address, name), daemon=True)
    t.start()


def _process_token(address: str, name: str):
    """Tunggu token masuk Raydium, lalu jalankan filter & tracking."""

    # ── Tunggu sampai token ada di Raydium (max 60 detik) ──
    log.info(f"[WAIT] Tunggu {name} masuk Raydium...")
    deadline = time.time() + RAYDIUM_WAIT_MAX
    on_raydium = False
    while time.time() < deadline:
        if is_on_raydium(address):
            on_raydium = True
            break
        time.sleep(RAYDIUM_POLL_SEC)

    if not on_raydium:
        log.info(f"[SKIP] {name}: tidak masuk Raydium dalam {RAYDIUM_WAIT_MAX}s")
        return

    log.info(f"[RAYDIUM] {name} sudah listing!")

    # ── Filter anti-rug ──
    safe, reason = is_token_safe(address)
    if not safe:
        log.info(f"[FILTER] {name}: {reason}")
        return

    # ── Ambil harga listing ──
    listing_price = get_current_price(address)
    if listing_price <= 0:
        log.info(f"[SKIP] {name}: harga tidak tersedia")
        return

    log.info(f"[OK] {name} lolos filter. Price: ${listing_price:.8f}")

    with tracker_lock:
        if address in active_trackers:
            return
        if len(positions) >= MAX_POSITIONS:
            log.info(f"[SKIP] {name}: max posisi sudah penuh")
            return

        tracker = TokenTracker(
            address=address,
            name=name,
            listing_price=listing_price,
            ath=listing_price,
            last_local_high=listing_price,
        )
        active_trackers[address] = tracker

    t = threading.Thread(target=track_token_loop, args=(tracker,), daemon=True)
    t.start()


def track_token_loop(tracker: TokenTracker):
    log.info(f"[TRACK] Mulai tracking {tracker.name}")

    while tracker.phase not in (DipPhase.DONE,):
        time.sleep(POLL_INTERVAL_SEC)
        price = get_current_price(tracker.address)
        if price <= 0:
            continue

        phase = tracker.update_price(price)

        if phase == DipPhase.ENTRY and tracker.address not in positions:
            log.info(f"[BUY] {tracker.name} @ ${price:.8f}")
            result = buy_token(tracker.address, BUY_AMOUNT_SOL)
            if result["success"]:
                token_balance = get_token_balance(tracker.address)
                positions[tracker.address] = {
                    "amount": token_balance,
                    "entry_price": price,
                    "tx_buy": result["tx"],
                }
                tracker.entry_price = price
                tracker.phase = DipPhase.HOLDING
                log.info(f"[BUY OK] Balance: {token_balance} | TX: {result['tx']}")
            else:
                log.error(f"[BUY FAIL] {tracker.name}")
                tracker.phase = DipPhase.DONE

        elif phase == DipPhase.HOLDING and tracker.address in positions:
            pos = positions[tracker.address]
            pnl = tracker.get_pnl(price)
            if pnl >= TAKE_PROFIT_PCT:
                log.info(f"[TP] {tracker.name}: +{pnl*100:.1f}% @ ${price:.8f}")
                _do_sell(tracker, pos, price, "take_profit")
            elif pnl <= -STOP_LOSS_PCT:
                log.info(f"[SL] {tracker.name}: {pnl*100:.1f}% @ ${price:.8f}")
                _do_sell(tracker, pos, price, "stop_loss")

    with tracker_lock:
        active_trackers.pop(tracker.address, None)
    log.info(f"[DONE] Selesai tracking {tracker.name}")


def _do_sell(tracker: TokenTracker, pos: dict, price: float, reason: str):
    result = sell_token(tracker.address, pos["amount"])
    pnl_pct = tracker.get_pnl(price) * 100
    if result["success"]:
        log.info(
            f"[SELL OK] {tracker.name} [{reason}] | "
            f"PnL: {pnl_pct:+.1f}% | SOL: {result['sol_received']:.4f} | TX: {result['tx']}"
        )
    else:
        log.error(f"[SELL FAIL] {tracker.name} - MANUAL SELL di Raydium sekarang!")
    positions.pop(tracker.address, None)
    tracker.phase = DipPhase.DONE


def polling_loop():
    log.info("[POLL] Polling fallback loop started")
    while True:
        time.sleep(60)  # polling tiap 60 detik, lebih hemat
        tokens = get_tokens_near_bond()
        for t in tokens:
            on_token_bonded(t)  # dedup sudah handle duplikat


def main():
    log.info("=" * 55)
    log.info("  SOLANA POST-BOND SCALPING BOT - Starting...")
    log.info(f"  Max posisi  : {MAX_POSITIONS}")
    log.info(f"  Buy amount  : {BUY_AMOUNT_SOL} SOL")
    log.info(f"  Take profit : {TAKE_PROFIT_PCT*100:.0f}%")
    log.info(f"  Stop loss   : {STOP_LOSS_PCT*100:.0f}%")
    log.info("=" * 55)

    listener = PumpFunListener(callback_bond=on_token_bonded)
    listener.start()

    poll_thread = threading.Thread(target=polling_loop, daemon=True)
    poll_thread.start()

    try:
        while True:
            time.sleep(15)
            with tracker_lock:
                log.info(
                    f"[STATUS] Tracking: {len(active_trackers)} | "
                    f"Positions: {len(positions)}/{MAX_POSITIONS} | "
                    f"Seen: {len(seen_addresses)} tokens"
                )
    except KeyboardInterrupt:
        log.info("Bot dihentikan.")
        listener.stop()


if __name__ == "__main__":
    main()