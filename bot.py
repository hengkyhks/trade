# ============================================================
#  BOT.PY — Main Orchestrator (2026 EDITION)
#
#  ALUR:
#  1. WebSocket subscribe ke MIGRATION_ACCOUNT
#     → detect PumpSwap migration (event "migrate") ← UTAMA 2026
#     → detect Raydium migration (event "initialize2") ← legacy
#  2. Resolve token mint dari tx
#  3. Tunggu konfirmasi listing di DEX (PumpSwap/Raydium)
#  4. Filter anti-rug (liquidity, volume, holder, mint authority)
#  5. Track harga, deteksi pola 3-dip
#  6. Buy saat dip3+bounce, sell saat TP/SL
#  7. Polling fallback tiap 60 detik (safety net)
#  8. Graceful shutdown: sell semua posisi terbuka saat Ctrl+C
# ============================================================
import sys
import time
import logging
import threading

from config import (
    POLL_INTERVAL_SEC, TAKE_PROFIT_PCT, STOP_LOSS_PCT,
    MAX_POSITIONS, LOG_FILE, BUY_AMOUNT_SOL,
    validate_config
)
from monitor import (
    PumpFunMigrationListener,
    get_current_price,
    is_on_supported_dex,
    get_tokens_near_bond,
)
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

# ── Global state (semua akses pakai tracker_lock) ─────────
active_trackers: dict[str, TokenTracker] = {}
positions:       dict[str, dict]         = {}
seen_addresses:  set[str]                = set()
tracker_lock = threading.Lock()

DEX_WAIT_MAX = 60    # max detik tunggu token listing di DEX
DEX_POLL_SEC = 3     # poll tiap 3 detik


# ── Callback dari WebSocket ───────────────────────────────

def on_token_bonded(token_info: dict):
    """
    Dipanggil saat migration account deteksi token baru.
    Bisa dari PumpSwap (migrate event) atau Raydium (initialize2).
    """
    address        = token_info.get("address")
    name           = token_info.get("name", "Unknown")
    migration_type = token_info.get("migration_type", "Unknown")

    if not address:
        return

    with tracker_lock:
        if address in seen_addresses:
            return
        seen_addresses.add(address)

        if len(positions) >= MAX_POSITIONS:
            log.info(f"[SKIP] {name}: max posisi ({MAX_POSITIONS}) tercapai")
            return

    log.info(f"[NEW] {migration_type} bond: {name} ({address[:20]}...)")
    threading.Thread(
        target=_process_token,
        args=(address, name, migration_type),
        daemon=True
    ).start()


def _process_token(address: str, name: str, migration_type: str):
    """
    1. Tunggu token listing di DEX (confirm via Dexscreener)
    2. Filter anti-rug
    3. Mulai tracking
    """
    log.info(f"[WAIT] Tunggu {name} ({migration_type}) listing di DEX...")
    deadline  = time.time() + DEX_WAIT_MAX
    dex_found = False
    dex_id    = ""

    while time.time() < deadline:
        listed, dex_id = is_on_supported_dex(address)
        if listed:
            dex_found = True
            break
        time.sleep(DEX_POLL_SEC)

    if not dex_found:
        log.info(f"[SKIP] {name}: tidak listing di DEX dalam {DEX_WAIT_MAX}s")
        return

    log.info(f"[DEX] {name} listing di {dex_id}! Jalankan filter...")

    # Filter anti-rug
    safe, reason = is_token_safe(address)
    if not safe:
        log.info(f"[FILTER] {name}: {reason}")
        return

    # Ambil harga listing
    listing_price = get_current_price(address)
    if listing_price <= 0:
        log.info(f"[SKIP] {name}: harga tidak tersedia")
        return

    log.info(
        f"[OK] {name} lolos filter | DEX: {dex_id} | "
        f"Price: ${listing_price:.8f}"
    )

    with tracker_lock:
        if address in active_trackers:
            return
        if len(positions) >= MAX_POSITIONS:
            log.info(f"[SKIP] {name}: max posisi penuh")
            return

        tracker = TokenTracker(
            address        = address,
            name           = name,
            listing_price  = listing_price,
            migration_type = migration_type,
        )
        active_trackers[address] = tracker

    threading.Thread(
        target=track_token_loop,
        args=(tracker,),
        daemon=True
    ).start()


# ── Price tracking loop ───────────────────────────────────

def track_token_loop(tracker: TokenTracker):
    log.info(f"[TRACK] 🔍 Mulai tracking {tracker.name} ({tracker.migration_type})")

    while True:
        time.sleep(POLL_INTERVAL_SEC)

        with tracker_lock:
            if tracker.phase == DipPhase.DONE:
                break

        price = get_current_price(tracker.address)
        if price <= 0:
            continue

        with tracker_lock:
            phase = tracker.update_price(price)

        # ── BUY trigger ─────────────────────────────────
        if phase == DipPhase.ENTRY:
            with tracker_lock:
                already_in = tracker.address in positions
            if not already_in:
                log.info(f"[BUY] {tracker.name} @ ${price:.8f}")
                result = buy_token(tracker.address, BUY_AMOUNT_SOL)

                if result["success"]:
                    balance = get_token_balance(tracker.address)
                    if balance == 0:
                        balance = result.get("out_amount", 0)

                    with tracker_lock:
                        positions[tracker.address] = {
                            "amount":      balance,
                            "entry_price": price,
                            "tx_buy":      result["tx"],
                        }
                        tracker.entry_price = price
                        tracker.phase       = DipPhase.HOLDING

                    log.info(
                        f"[BUY OK] {tracker.name} | "
                        f"Balance: {balance} | TX: {result['tx']}"
                    )
                else:
                    log.error(f"[BUY FAIL] {tracker.name}")
                    with tracker_lock:
                        tracker.phase = DipPhase.DONE

        # ── TP / SL check ────────────────────────────────
        elif phase == DipPhase.HOLDING:
            with tracker_lock:
                pos = positions.get(tracker.address)
            if pos is None:
                continue

            pnl = tracker.get_pnl(price)

            if pnl >= TAKE_PROFIT_PCT:
                log.info(
                    f"[TP] {tracker.name}: +{pnl*100:.1f}% @ ${price:.8f}"
                )
                _do_sell(tracker, pos, price, "take_profit")

            elif pnl <= -STOP_LOSS_PCT:
                log.info(
                    f"[SL] {tracker.name}: {pnl*100:.1f}% @ ${price:.8f}"
                )
                _do_sell(tracker, pos, price, "stop_loss")

    with tracker_lock:
        active_trackers.pop(tracker.address, None)
    log.info(f"[DONE] Selesai tracking {tracker.name}")


def _do_sell(tracker: TokenTracker, pos: dict, price: float, reason: str):
    result  = sell_token(tracker.address, pos["amount"])
    pnl_pct = tracker.get_pnl(price) * 100

    if result["success"]:
        log.info(
            f"[SELL OK] {tracker.name} [{reason}] | "
            f"PnL: {pnl_pct:+.1f}% | "
            f"SOL: {result['sol_received']:.4f} | "
            f"TX: {result['tx']}"
        )
    else:
        log.error(
            f"[SELL FAIL] {tracker.name} [{reason}] — "
            f"MANUAL SELL! Amount: {pos['amount']}"
        )

    with tracker_lock:
        positions.pop(tracker.address, None)
        tracker.phase = DipPhase.DONE


# ── Polling fallback ──────────────────────────────────────

def polling_loop():
    """
    Safety net: polling tiap 60 detik untuk token yang miss dari WS.
    Ambil token terbaru dari PumpSwap + Raydium via Dexscreener.
    """
    log.info("[POLL] Polling fallback started (interval: 60s)")
    while True:
        time.sleep(60)
        try:
            tokens = get_tokens_near_bond()
            for t in tokens:
                on_token_bonded(t)
        except Exception as e:
            log.warning(f"[POLL] Error: {e}")


# ── Entry point ───────────────────────────────────────────

def main():
    validate_config()

    log.info("=" * 60)
    log.info("  🚀 SOLANA POST-BOND SCALPING BOT 2026")
    log.info("  Target: PumpSwap (95%+) + Raydium (legacy)")
    log.info(f"  Max posisi  : {MAX_POSITIONS}")
    log.info(f"  Buy amount  : {BUY_AMOUNT_SOL} SOL")
    log.info(f"  Take profit : {TAKE_PROFIT_PCT*100:.0f}%")
    log.info(f"  Stop loss   : {STOP_LOSS_PCT*100:.0f}%")
    log.info("=" * 60)

    # Start WS listener (deteksi real-time < 1 detik)
    listener = PumpFunMigrationListener(callback_bond=on_token_bonded)
    listener.start()

    # Start polling fallback
    poll_thread = threading.Thread(target=polling_loop, daemon=True)
    poll_thread.start()

    try:
        while True:
            time.sleep(15)
            with tracker_lock:
                n_track = len(active_trackers)
                n_pos   = len(positions)
                n_seen  = len(seen_addresses)
                pos_info = [
                    f"{k[:6]}:{v.get('amount',0)}"
                    for k, v in positions.items()
                ]
            log.info(
                f"[STATUS] Tracking: {n_track} | "
                f"Posisi: {n_pos}/{MAX_POSITIONS} | "
                f"Seen: {n_seen} | "
                f"Holdings: {pos_info or 'none'}"
            )

    except KeyboardInterrupt:
        log.info("\n🛑 Bot dihentikan. Menutup semua posisi...")
        listener.stop()

        # Graceful exit: sell semua posisi terbuka
        with tracker_lock:
            open_pos     = dict(positions)
            open_tracker = dict(active_trackers)

        if open_pos:
            log.info(f"Menutup {len(open_pos)} posisi...")
            for addr, pos in open_pos.items():
                price   = get_current_price(addr)
                tracker = open_tracker.get(addr)
                if tracker:
                    _do_sell(tracker, pos, price, "shutdown")
                else:
                    result = sell_token(addr, pos["amount"])
                    if result["success"]:
                        log.info(f"[SELL OK] {addr[:20]} shutdown")
                    else:
                        log.error(f"[SELL FAIL] {addr[:20]} — manual sell!")

        log.info("✅ Bot selesai.")


if __name__ == "__main__":
    main()
