# ============================================================
#  TRADER.PY — Eksekusi buy/sell via Jupiter Aggregator (FIXED 2026)
#
#  FIX KRITIS:
#  - Signing VersionedTransaction dengan cara benar (solders)
#  - Konfirmasi tx setelah send
#  - Jupiter v6 support PumpSwap token secara otomatis
#    (Jupiter routing otomatis cari best path ke PumpSwap/Raydium)
#  - Semua dict access None-safe
# ============================================================
import requests
import logging
import base64
import time

from solders.keypair import Keypair                    # type: ignore
from solders.transaction import VersionedTransaction   # type: ignore
from solders.message import to_bytes_versioned         # type: ignore
from solana.rpc.api import Client                      # type: ignore
from solana.rpc.types import TxOpts                    # type: ignore
from solana.rpc.commitment import Confirmed            # type: ignore

from config import (
    PRIVATE_KEY, RPC_URL, SLIPPAGE_BPS,
    PRIORITY_FEE_LAMPORTS, BUY_AMOUNT_SOL
)

log = logging.getLogger("trader")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"


def _get_keypair() -> Keypair:
    return Keypair.from_base58_string(PRIVATE_KEY)


def _get_client() -> Client:
    return Client(RPC_URL)


def get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    retries: int = 3
) -> dict | None:
    """
    Minta quote dari Jupiter v6.
    Jupiter otomatis route ke PumpSwap atau Raydium sesuai best price.
    """
    params = {
        "inputMint":   input_mint,
        "outputMint":  output_mint,
        "amount":      amount_lamports,
        "slippageBps": SLIPPAGE_BPS,
    }
    for attempt in range(retries):
        try:
            r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=8)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                log.error(f"Jupiter quote error: {data['error']}")
                return None
            return data
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                log.error(f"Quote gagal setelah {retries}x: {e}")
    return None


def execute_swap(
    quote: dict,
    keypair: Keypair,
    client: Client
) -> str | None:
    """
    Eksekusi swap via Jupiter.

    Signing yang benar untuk VersionedTransaction (solders):
    1. Dapatkan serialized tx dari Jupiter /swap
    2. Deserialize sebagai VersionedTransaction
    3. Sign: signature = keypair.sign_message(to_bytes_versioned(tx.message))
    4. Rebuild: signed_tx = VersionedTransaction(tx.message, [signature])
    5. Kirim: client.send_raw_transaction(bytes(signed_tx))
    """
    try:
        wallet_pubkey = str(keypair.pubkey())

        # 1) Request swap tx dari Jupiter
        payload = {
            "quoteResponse":             quote,
            "userPublicKey":             wallet_pubkey,
            "wrapAndUnwrapSol":          True,
            "prioritizationFeeLamports": PRIORITY_FEE_LAMPORTS,
            "dynamicComputeUnitLimit":   True,
        }
        r = requests.post(JUPITER_SWAP_URL, json=payload, timeout=12)
        r.raise_for_status()
        swap_data = r.json()

        if "swapTransaction" not in swap_data:
            log.error(f"Jupiter swap error: {swap_data.get('error', swap_data)}")
            return None

        # 2) Decode base64
        raw_bytes = base64.b64decode(swap_data["swapTransaction"])

        # 3) Deserialize
        tx = VersionedTransaction.from_bytes(raw_bytes)

        # 4) Sign dengan benar
        msg_bytes  = to_bytes_versioned(tx.message)
        signature  = keypair.sign_message(msg_bytes)
        signed_tx  = VersionedTransaction(tx.message, [signature])

        # 5) Kirim
        opts = TxOpts(
            skip_preflight=False,
            preflight_commitment=Confirmed,
        )
        resp = client.send_raw_transaction(bytes(signed_tx), opts=opts)
        if resp.value is None:
            log.error("send_raw_transaction: response value None")
            return None

        sig = str(resp.value)
        log.info(f"TX sent ✅ https://solscan.io/tx/{sig}")

        # 6) Konfirmasi (max 60 detik)
        confirmed = _confirm_tx(client, sig, timeout=60)
        if not confirmed:
            log.warning(f"TX belum confirmed dalam 60s, cek manual: {sig}")

        return sig

    except Exception as e:
        log.error(f"execute_swap error: {e}", exc_info=True)
        return None


def _confirm_tx(client: Client, signature: str, timeout: int = 60) -> bool:
    """Poll sampai tx confirmed/finalized atau timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp     = client.get_signature_statuses([signature])
            statuses = resp.value or []
            if statuses and statuses[0] is not None:
                status = statuses[0]
                if status.err:
                    log.error(f"TX error on-chain: {status.err}")
                    return False
                conf = (status.confirmation_status or "").lower()
                if conf in ("confirmed", "finalized"):
                    return True
        except Exception as e:
            log.debug(f"confirm_tx poll: {e}")
        time.sleep(2)
    return False


def buy_token(
    token_address: str,
    sol_amount: float = BUY_AMOUNT_SOL
) -> dict:
    """
    Beli token dengan SOL via Jupiter.
    Jupiter otomatis route ke PumpSwap atau Raydium.
    """
    keypair  = _get_keypair()
    client   = _get_client()
    lamports = int(sol_amount * LAMPORTS)

    log.info(f"📈 BUY {token_address[:20]}... | {sol_amount} SOL")

    quote = get_quote(SOL_MINT, token_address, lamports)
    if not quote:
        return {"success": False, "tx": None, "price": 0, "out_amount": 0}

    out_amount     = int(quote.get("outAmount") or 0)
    price_estimate = lamports / out_amount if out_amount > 0 else 0

    tx      = execute_swap(quote, keypair, client)
    success = tx is not None

    return {
        "success":    success,
        "tx":         tx,
        "price":      price_estimate,
        "out_amount": out_amount,
    }


def sell_token(token_address: str, token_amount: int) -> dict:
    """
    Jual token kembali ke SOL via Jupiter.
    token_amount = raw amount (bukan desimal).
    """
    keypair = _get_keypair()
    client  = _get_client()

    log.info(f"📉 SELL {token_amount} of {token_address[:20]}...")

    quote = get_quote(token_address, SOL_MINT, token_amount)
    if not quote:
        return {"success": False, "tx": None, "sol_received": 0}

    out_lamports = int(quote.get("outAmount") or 0)
    sol_received = out_lamports / LAMPORTS

    tx      = execute_swap(quote, keypair, client)
    success = tx is not None

    return {
        "success":      success,
        "tx":           tx,
        "sol_received": sol_received,
    }


def get_token_balance(token_address: str) -> int:
    """Return saldo token (raw) via getTokenAccountsByOwner."""
    try:
        keypair       = _get_keypair()
        wallet_pubkey = str(keypair.pubkey())

        r = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet_pubkey,
                {"mint": token_address},
                {"encoding": "jsonParsed"}
            ]
        }, timeout=8)

        accounts = (r.json().get("result") or {}).get("value") or []
        if not accounts:
            return 0

        return int(
            accounts[0]
            .get("account", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
            .get("tokenAmount", {})
            .get("amount", "0")
        )
    except Exception as e:
        log.error(f"get_token_balance error: {e}")
        return 0
