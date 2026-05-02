# ============================================================
#  MONITOR.PY — Deteksi bond + harga (2026 EDITION)
#
#  TEKNIK TERBARU:
#  1. WebSocket subscribe ke MIGRATION_ACCOUNT (39azUYFWPz3...)
#     → Tangkap event "migrate" (PumpSwap) DAN "initialize2" (Raydium)
#     → Ini JAUH lebih cepat dari polling Dexscreener (< 1 detik)
#
#  2. Dual detection:
#     - Log mengandung "migrate"       → token ke PumpSwap (95%+ kasus)
#     - Log mengandung "initialize2"   → token ke Raydium  (legacy)
#
#  3. Fallback polling Dexscreener tiap 60 detik
#     (untuk token yang luput dari WS)
#
#  4. get_current_price() support PumpSwap DAN Raydium pairs
#
#  PROGRAM IDs VALID 2026:
#   - Pump.fun  : 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
#   - PumpSwap  : pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA
#   - Migration : 39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg
#   - Raydium   : 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8
# ============================================================
import requests
import websocket          # type: ignore
import json
import logging
import threading
import time
from typing import Callable

from config import (
    RPC_URL,
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    RAYDIUM_PROGRAM_ID,
    MIGRATION_ACCOUNT,
    SUPPORTED_DEX_IDS,
)

log = logging.getLogger("monitor")

# Akun sistem Solana yang harus di-exclude saat parse tx
SYSTEM_ACCOUNTS = {
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    RAYDIUM_PROGRAM_ID,
    MIGRATION_ACCOUNT,
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1pi",
    "SysvarRent111111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",  # Wrapped SOL
}


# ── Helpers ──────────────────────────────────────────────────

def _rpc_http_url() -> str:
    return (
        RPC_URL
        .replace("wss://", "https://")
        .replace("ws://", "http://")
    )


def _rpc_ws_url() -> str:
    return (
        RPC_URL
        .replace("https://", "wss://")
        .replace("http://", "ws://")
    )


# ── Harga & DEX check ────────────────────────────────────────

def get_current_price(token_address: str, retries: int = 3) -> float:
    """
    Ambil harga token dari Dexscreener.
    Support PumpSwap DAN Raydium (cari pair dengan likuiditas tertinggi).
    """
    for attempt in range(retries):
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
            r = requests.get(url, timeout=7)
            r.raise_for_status()
            pairs = r.json().get("pairs") or []

            # Filter hanya DEX yang kita kenal
            valid_pairs = [
                p for p in pairs
                if (p.get("dexId") or "").lower() in SUPPORTED_DEX_IDS
            ]
            if not valid_pairs:
                return 0.0

            # Ambil pair dengan likuiditas tertinggi
            best = max(
                valid_pairs,
                key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0)
            )
            return float(best.get("priceUsd") or 0)

        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                log.debug(f"get_current_price error ({token_address[:8]}): {e}")
    return 0.0


def is_on_supported_dex(token_address: str) -> tuple[bool, str]:
    """
    Cek apakah token sudah listing di PumpSwap atau Raydium.
    Return: (True, dex_id) atau (False, "")
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        for p in pairs:
            dex_id = (p.get("dexId") or "").lower()
            if dex_id in SUPPORTED_DEX_IDS:
                return True, dex_id
        return False, ""
    except Exception:
        return False, ""


# ── Polling fallback ─────────────────────────────────────────

def get_tokens_near_bond(max_results: int = 30) -> list[dict]:
    """
    Polling fallback: ambil token baru dari Dexscreener (PumpSwap + Raydium).
    Dijalankan tiap 60 detik sebagai safety net jika WS miss.
    """
    result = []
    seen = set()

    # Coba PumpSwap dulu (prioritas 2026)
    for dex_path in ["pumpswap", "raydium"]:
        try:
            url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{dex_path}"
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            pairs = (r.json().get("pairs") or [])[:max_results]
            pairs.sort(key=lambda p: p.get("pairCreatedAt", 0), reverse=True)

            for p in pairs:
                base = p.get("baseToken") or {}
                addr = base.get("address")
                if not addr or addr in seen:
                    continue
                seen.add(addr)
                result.append({
                    "address":  addr,
                    "name":     base.get("name", "Unknown")[:30],
                    "symbol":   base.get("symbol", "???"),
                    "dex":      dex_path,
                    "progress": 1.0,
                })
        except Exception as e:
            log.warning(f"[POLL] Dexscreener {dex_path} error: {e}")

    log.info(f"[POLL] {len(result)} token baru dari polling")
    return result


# ── Transaction parser ───────────────────────────────────────

def _resolve_token_from_tx(signature: str) -> dict | None:
    """
    Parse transaksi untuk ekstrak mint address token baru.
    Strategi: cek postTokenBalances dulu (paling akurat),
    fallback ke accountKeys.
    """
    try:
        r = requests.post(
            _rpc_http_url(),
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "confirmed",
                    }
                ]
            },
            timeout=8
        )
        data = r.json()
        tx = data.get("result") or {}
        if not tx:
            return None

        # ── Prioritas 1: postTokenBalances ───────────────────
        post_balances = (
            tx.get("meta", {})
            .get("postTokenBalances", [])
        )
        mint_candidates = [
            b.get("mint") for b in post_balances
            if b.get("mint") and b.get("mint") not in SYSTEM_ACCOUNTS
        ]
        if mint_candidates:
            # Ambil mint dengan balance terbesar (biasanya token baru)
            mint = mint_candidates[0]
            return {
                "address": mint,
                "name":    f"Token-{mint[:6]}",
                "symbol":  "NEW",
                "progress": 1.0,
            }

        # ── Prioritas 2: accountKeys fallback ────────────────
        account_keys = (
            tx.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )
        for a in account_keys:
            if not isinstance(a, dict):
                continue
            pubkey = a.get("pubkey", "")
            if pubkey in SYSTEM_ACCOUNTS:
                continue
            if a.get("signer"):
                continue
            return {
                "address": pubkey,
                "name":    f"Token-{pubkey[:6]}",
                "symbol":  "NEW",
                "progress": 1.0,
            }

        return None

    except Exception as e:
        log.debug(f"_resolve_token_from_tx error: {e}")
        return None


# ── WebSocket Listener ────────────────────────────────────────
#
#  Cara kerjanya (2026):
#  Subscribe ke logsSubscribe dengan mentions: [MIGRATION_ACCOUNT]
#
#  Pump.fun migration account (39azUYFWPz3...) memanggil:
#    - "migrate" instruction → PumpSwap (95%+ token baru 2026)
#    - "initialize2" instruction → Raydium (legacy)
#
#  Kedua event bisa ditangkap dari log yang sama.
#  Ini JAUH lebih cepat dari polling Dexscreener.
# ────────────────────────────────────────────────────────────

class PumpFunMigrationListener:
    """
    Real-time listener untuk Pump.fun bond events via WebSocket.
    Detect migrasi ke PumpSwap (default 2026) DAN Raydium (legacy).
    """

    def __init__(self, callback_bond: Callable):
        self.callback_bond    = callback_bond
        self._ws              = None
        self._thread          = None
        self._running         = False
        self._ws_url          = _rpc_ws_url()
        self._seen_sigs: set  = set()   # Dedup di level WS
        self._sig_lock        = threading.Lock()

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("🔌 PumpFun Migration WebSocket listener started")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = lambda ws, e: log.error(f"WS error: {e}"),
                    on_close   = lambda ws, *a: log.warning("WS closed, reconnecting..."),
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.error(f"WS exception: {e}")
            if self._running:
                log.info("WS reconnecting in 5s...")
                time.sleep(5)

    def _on_open(self, ws):
        """
        Subscribe ke log yang menyebut MIGRATION_ACCOUNT.
        Ini adalah account yang menangani SEMUA migrasi Pump.fun,
        baik ke PumpSwap maupun ke Raydium.
        """
        ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [MIGRATION_ACCOUNT]},
                {"commitment": "confirmed"}
            ]
        }))
        log.info(
            f"✅ Subscribed ke Pump.fun Migration Account: "
            f"{MIGRATION_ACCOUNT[:20]}... "
            f"(deteksi PumpSwap + Raydium)"
        )

    def _on_message(self, ws, raw: str):
        try:
            data   = json.loads(raw)
            # Skip subscription ack
            if "result" in data and "params" not in data:
                return

            params = data.get("params") or {}
            value  = (params.get("result") or {}).get("value") or {}
            logs   = value.get("logs") or []
            sig    = value.get("signature", "")

            if not sig or not logs:
                return

            # Dedup signature
            with self._sig_lock:
                if sig in self._seen_sigs:
                    return
                self._seen_sigs.add(sig)
                # Bersihkan cache jika terlalu besar
                if len(self._seen_sigs) > 5000:
                    self._seen_sigs.clear()

            # ── Detect jenis migrasi dari logs ───────────────
            logs_lower = [l.lower() for l in logs]

            is_pumpswap = any(
                "migrate" in l or "create_pool" in l
                for l in logs_lower
            )
            is_raydium = any(
                "initialize2" in l
                for l in logs_lower
            )

            if not is_pumpswap and not is_raydium:
                return

            migration_type = "PumpSwap" if is_pumpswap else "Raydium"
            log.info(f"🔥 [{migration_type}] Bond detected! TX: {sig[:20]}...")

            threading.Thread(
                target=self._handle_migration,
                args=(sig, migration_type),
                daemon=True
            ).start()

        except Exception as e:
            log.debug(f"WS parse error: {e}")

    def _handle_migration(self, signature: str, migration_type: str):
        """
        Resolve token address dari tx, lalu callback.
        """
        token_info = _resolve_token_from_tx(signature)
        if not token_info:
            log.debug(f"[{migration_type}] Tidak bisa resolve token dari {signature[:20]}")
            return

        token_info["migration_type"] = migration_type
        log.info(
            f"[{migration_type}] ✅ New token: "
            f"{token_info['address'][:20]}..."
        )
        self.callback_bond(token_info)
