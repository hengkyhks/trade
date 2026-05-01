# ============================================================
#  MONITOR.PY — Pantau token post-bond via Dexscreener
# ============================================================
import requests
import websocket  # type: ignore
import json
import logging
import threading
import time
from typing import Callable
from config import RPC_URL

log = logging.getLogger("monitor")
RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


# ── Polling: Dexscreener new pairs (lebih reliable dari Pump.fun) ──

def get_tokens_near_bond(min_progress: float = 0.80) -> list[dict]:
    """
    Ambil token baru di Raydium dari Dexscreener.
    min_progress diabaikan di sini karena kita langsung cari yang sudah listing.
    """
    try:
        # Token Solana baru listing di Raydium, sort by age
        url = "https://api.dexscreener.com/token-profiles/latest/v1"
        r = requests.get(url, timeout=8)
        if r.status_code != 200 or not r.text.strip():
            raise ValueError("Empty response")

        tokens = r.json()
        if not isinstance(tokens, list):
            raise ValueError("Unexpected format")

        result = []
        for t in tokens:
            if t.get("chainId") != "solana":
                continue
            result.append({
                "address": t.get("tokenAddress"),
                "name": t.get("description", "Unknown")[:30],
                "symbol": "NEW",
                "progress": 1.0,  # Sudah listing = sudah post-bond
            })

        log.info(f"[POLL] {len(result)} token baru dari Dexscreener")
        return result

    except Exception as e:
        log.warning(f"[POLL] Dexscreener error: {e}")
        return []


# ── Harga & Raydium check ────────────────────────────────────

def get_current_price(token_address: str) -> float:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=5)
        pairs = r.json().get("pairs", [])
        raydium = [p for p in pairs if p.get("dexId") == "raydium"]
        if not raydium:
            return 0
        best = max(raydium, key=lambda p: float(p.get("liquidity", {}).get("usd", 0)))
        return float(best.get("priceUsd", 0))
    except:
        return 0


def is_on_raydium(token_address: str) -> bool:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=5)
        pairs = r.json().get("pairs", [])
        return any(p.get("dexId") == "raydium" for p in pairs)
    except:
        return False


# ── Helius WebSocket ─────────────────────────────────────────

def _rpc_http_url() -> str:
    return RPC_URL.replace("wss://", "https://").replace("ws://", "http://")


class PumpFunListener:
    def __init__(self, callback_bond: Callable):
        self.callback_bond = callback_bond
        self._ws = None
        self._thread = None
        self._running = False
        self._ws_url = RPC_URL.replace("https://", "wss://").replace("http://", "ws://")

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Helius WebSocket listener started")

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()

    def _run(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, e: log.error(f"WS error: {e}"),
                    on_close=lambda ws, *a: log.warning("WS closed, reconnecting..."),
                )
                self._ws.run_forever(ping_interval=30)
                if self._running:
                    time.sleep(5)
            except Exception as e:
                log.error(f"WS exception: {e}")
                time.sleep(5)

    def _on_open(self, ws):
        ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [RAYDIUM_AMM]},
                {"commitment": "confirmed"}
            ]
        }))
        log.info("Subscribed to Raydium AMM logs via Helius")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "result" in data:
                return
            params = data.get("params", {})
            value  = params.get("result", {}).get("value", {})
            logs   = value.get("logs", [])
            is_init = any("initialize" in l.lower() for l in logs)
            if not is_init:
                return
            signature = value.get("signature", "")
            if signature:
                threading.Thread(
                    target=self._resolve_new_token,
                    args=(signature,), daemon=True
                ).start()
        except Exception as e:
            log.debug(f"WS parse error: {e}")

    def _resolve_new_token(self, signature: str):
        try:
            r = requests.post(_rpc_http_url(), json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
            }, timeout=5)
            tx = r.json().get("result", {})
            if not tx:
                return
            accounts = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            known = {RAYDIUM_AMM, "11111111111111111111111111111111",
                     "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
            candidates = [a.get("pubkey") for a in accounts
                          if isinstance(a, dict) and a.get("pubkey") not in known]
            if candidates:
                addr = candidates[0]
                log.info(f"[WS] New pool detected: {addr[:20]}...")
                self.callback_bond({
                    "address": addr,
                    "name": f"Token-{addr[:6]}",
                    "symbol": "NEW",
                    "progress": 1.0,
                })
        except Exception as e:
            log.debug(f"Resolve tx error: {e}")