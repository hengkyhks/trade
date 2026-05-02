# ============================================================
#  FILTERS.PY — Anti-rug & token quality checks (2026)
#
#  UPDATE:
#  - Dexscreener filter sekarang cek pumpswap DAN raydium
#  - Holder check via Solana RPC (bukan Solscan yang deprecated)
#  - Tambah cek mintAuthority / freezeAuthority
#  - Fail-open jika API error (token tidak diblok karena API lambat)
# ============================================================
import requests
import logging
from config import (
    MIN_LIQUIDITY_USD, MAX_TOP_HOLDER_PCT,
    MIN_HOLDER_COUNT, MIN_VOLUME_5MIN_USD,
    RPC_URL, SUPPORTED_DEX_IDS
)

log = logging.getLogger("filters")


def get_token_info_dexscreener(token_address: str) -> dict | None:
    """
    Ambil data pair terbaik dari Dexscreener.
    Cari PumpSwap dulu, fallback ke Raydium.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []

        valid = [
            p for p in pairs
            if (p.get("dexId") or "").lower() in SUPPORTED_DEX_IDS
        ]
        if not valid:
            return None

        # Ambil pair dengan likuiditas terbesar
        return max(
            valid,
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0)
        )
    except Exception as e:
        log.error(f"Dexscreener error: {e}")
        return None


def get_mint_authority_info(token_address: str) -> dict:
    """
    Cek mintAuthority dan freezeAuthority via Solana RPC.
    Jika masih aktif = risiko rug / mint serangan.
    """
    try:
        r = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [
                token_address,
                {"encoding": "jsonParsed", "commitment": "confirmed"}
            ]
        }, timeout=8)
        info = (
            r.json()
            .get("result", {})
            .get("value", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
        )
        return {
            "mint_authority":   info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
        }
    except Exception as e:
        log.warning(f"Mint authority check error: {e}")
        # Fail-open: anggap tidak ada authority
        return {"mint_authority": None, "freeze_authority": None}


def get_holder_info(token_address: str) -> dict:
    """
    Ambil info holder via Solana RPC:
    - getTokenLargestAccounts untuk top holder %
    - getProgramAccounts untuk total count
    """
    try:
        # Top holder check
        r = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [token_address, {"commitment": "confirmed"}]
        }, timeout=8)
        accounts = r.json().get("result", {}).get("value", [])

        if not accounts:
            return {"count": 999, "top_holder_pct": 0.0}

        total = sum(int(a.get("amount", 0)) for a in accounts)
        top   = int(accounts[0].get("amount", 0)) if accounts else 0
        top_pct = top / total if total > 0 else 0.0

        # Holder count
        count = _get_holder_count(token_address)
        return {"count": count, "top_holder_pct": top_pct}

    except Exception as e:
        log.warning(f"Holder check error: {e}")
        return {"count": 999, "top_holder_pct": 0.0}


def _get_holder_count(token_address: str) -> int:
    """Hitung jumlah token accounts via getProgramAccounts."""
    try:
        r = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getProgramAccounts",
            "params": [
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                {
                    "encoding": "base64",
                    "filters": [
                        {"dataSize": 165},
                        {"memcmp": {"offset": 0, "bytes": token_address}}
                    ],
                    "commitment": "confirmed",
                    "withContext": False,
                }
            ]
        }, timeout=12)
        return len(r.json().get("result", []))
    except Exception as e:
        log.warning(f"Holder count error: {e}")
        return 999  # Fail-open


def is_token_safe(token_address: str) -> tuple[bool, str]:
    """
    Jalankan semua filter.
    Return: (True, "ok") atau (False, "alasan gagal")
    """
    # ── 1. Cek pair di DEX ───────────────────────────────────
    pair = get_token_info_dexscreener(token_address)
    if pair is None:
        return False, "Tidak ada pair di PumpSwap/Raydium"

    # ── 2. Cek likuiditas ────────────────────────────────────
    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    if liquidity < MIN_LIQUIDITY_USD:
        return False, f"Likuiditas terlalu kecil: ${liquidity:.0f}"

    # ── 3. Cek volume 5 menit ────────────────────────────────
    vol5m = float((pair.get("volume") or {}).get("m5") or 0)
    if vol5m < MIN_VOLUME_5MIN_USD:
        return False, f"Volume 5m terlalu kecil: ${vol5m:.0f}"

    # ── 4. Cek price change (sudah dump?) ────────────────────
    price_change_1h = float((pair.get("priceChange") or {}).get("h1") or 0)
    if price_change_1h < -80:
        return False, f"Sudah dump -80% dalam 1 jam: {price_change_1h:.0f}%"

    # ── 5. Cek mint / freeze authority ───────────────────────
    auth = get_mint_authority_info(token_address)
    if auth["mint_authority"]:
        return False, f"mintAuthority masih aktif (risiko rug)"
    if auth["freeze_authority"]:
        return False, f"freezeAuthority masih aktif (risiko freeze)"

    # ── 6. Cek holder ────────────────────────────────────────
    holder = get_holder_info(token_address)
    if holder["count"] < MIN_HOLDER_COUNT:
        return False, f"Holder terlalu sedikit: {holder['count']}"
    if holder["top_holder_pct"] > MAX_TOP_HOLDER_PCT:
        return False, (
            f"Top holder terlalu dominan: "
            f"{holder['top_holder_pct']*100:.1f}%"
        )

    return True, "ok"
