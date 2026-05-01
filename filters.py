# ============================================================
#  FILTERS.PY — Anti-rug & token quality checks
# ============================================================
import requests
import logging
from config import (
    MIN_LIQUIDITY_USD, MAX_TOP_HOLDER_PCT,
    MIN_HOLDER_COUNT, MIN_VOLUME_5MIN_USD
)

log = logging.getLogger("filters")


def get_token_info_dexscreener(token_address: str) -> dict | None:
    """Ambil data token dari Dexscreener API (gratis, no key)."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=5)
        data = r.json()
        pairs = data.get("pairs", [])
        # Ambil pair Raydium dengan volume tertinggi
        raydium_pairs = [p for p in pairs if p.get("dexId") == "raydium"]
        if not raydium_pairs:
            return None
        return max(raydium_pairs, key=lambda p: float(p.get("volume", {}).get("m5", 0)))
    except Exception as e:
        log.error(f"Dexscreener error: {e}")
        return None


def get_holder_info(token_address: str) -> dict:
    """
    Ambil holder info dari Helius atau Solscan.
    Return: {"count": int, "top_holder_pct": float}
    """
    try:
        # Pakai Solscan public API
        url = f"https://public-api.solscan.io/token/holders?tokenAddress={token_address}&limit=20&offset=0"
        r = requests.get(url, timeout=5)
        data = r.json()
        holders = data.get("data", [])
        total_supply = sum(h.get("amount", 0) for h in holders)
        top_holder_pct = 0
        if total_supply > 0 and holders:
            top_holder_pct = holders[0].get("amount", 0) / total_supply
        count = data.get("total", len(holders))
        return {"count": count, "top_holder_pct": top_holder_pct}
    except Exception as e:
        log.warning(f"Holder check error: {e}")
        return {"count": 999, "top_holder_pct": 0}  # Default: lolos filter


def is_token_safe(token_address: str) -> tuple[bool, str]:
    """
    Jalanin semua filter. Return (True, "ok") atau (False, "alasan").
    """
    pair = get_token_info_dexscreener(token_address)
    if pair is None:
        return False, "Tidak ada pair Raydium ditemukan"

    # --- Cek likuiditas ---
    liquidity = float(pair.get("liquidity", {}).get("usd", 0))
    if liquidity < MIN_LIQUIDITY_USD:
        return False, f"Likuiditas terlalu kecil: ${liquidity:.0f}"

    # --- Cek volume 5 menit ---
    vol5m = float(pair.get("volume", {}).get("m5", 0))
    if vol5m < MIN_VOLUME_5MIN_USD:
        return False, f"Volume 5m terlalu kecil: ${vol5m:.0f}"

    # --- Cek price change (jangan beli yang sudah dump habis) ---
    price_change_1h = float(pair.get("priceChange", {}).get("h1", 0))
    if price_change_1h < -80:
        return False, f"Sudah dump -80% dalam 1 jam: {price_change_1h:.0f}%"

    # --- Cek holder ---
    holder_info = get_holder_info(token_address)
    if holder_info["count"] < MIN_HOLDER_COUNT:
        return False, f"Holder terlalu sedikit: {holder_info['count']}"
    if holder_info["top_holder_pct"] > MAX_TOP_HOLDER_PCT:
        return False, f"Top holder terlalu besar: {holder_info['top_holder_pct']*100:.1f}%"

    return True, "ok"
