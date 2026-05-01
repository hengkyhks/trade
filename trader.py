# ============================================================
#  TRADER.PY — Eksekusi buy/sell via Jupiter Aggregator
#  Jupiter adalah DEX aggregator terbaik di Solana
# ============================================================
import requests
import logging
import base64
from solders.keypair import Keypair  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore
from solana.rpc.api import Client  # type: ignore
from config import (
    PRIVATE_KEY, RPC_URL, SLIPPAGE_BPS,
    PRIORITY_FEE_LAMPORTS, BUY_AMOUNT_SOL
)

log = logging.getLogger("trader")

SOL_MINT  = "So11111111111111111111111111111111111111112"
LAMPORTS  = 1_000_000_000  # 1 SOL = 1B lamports

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"


def _get_keypair() -> Keypair:
    return Keypair.from_base58_string(PRIVATE_KEY)


def _get_client() -> Client:
    return Client(RPC_URL)


def get_quote(input_mint: str, output_mint: str, amount_lamports: int) -> dict | None:
    """Minta quote dari Jupiter."""
    try:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount_lamports,
            "slippageBps": SLIPPAGE_BPS,
        }
        r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=5)
        return r.json()
    except Exception as e:
        log.error(f"Quote error: {e}")
        return None


def execute_swap(quote: dict, keypair: Keypair, client: Client) -> str | None:
    """
    Kirim swap transaction ke Solana.
    Return: tx signature atau None kalau gagal.
    """
    try:
        wallet_pubkey = str(keypair.pubkey())
        payload = {
            "quoteResponse": quote,
            "userPublicKey": wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": PRIORITY_FEE_LAMPORTS,
        }
        r = requests.post(JUPITER_SWAP_URL, json=payload, timeout=10)
        swap_data = r.json()

        if "swapTransaction" not in swap_data:
            log.error(f"Swap error: {swap_data}")
            return None

        # Deserialize & sign transaction
        raw_tx = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = keypair.sign_message(bytes(tx.message))

        # Kirim ke chain
        resp = client.send_raw_transaction(bytes(tx))
        sig = str(resp.value)
        log.info(f"TX sent: https://solscan.io/tx/{sig}")
        return sig

    except Exception as e:
        log.error(f"Swap execution error: {e}")
        return None


def buy_token(token_address: str, sol_amount: float = BUY_AMOUNT_SOL) -> dict:
    """
    Beli token dengan SOL.
    Return: {"success": bool, "tx": str, "price": float}
    """
    keypair = _get_keypair()
    client  = _get_client()

    lamports = int(sol_amount * LAMPORTS)
    log.info(f"📈 Buying {token_address} with {sol_amount} SOL...")

    quote = get_quote(SOL_MINT, token_address, lamports)
    if not quote:
        return {"success": False, "tx": None, "price": 0}

    # Estimasi harga dari quote
    out_amount = int(quote.get("outAmount", 0))
    price_estimate = lamports / out_amount if out_amount > 0 else 0

    tx = execute_swap(quote, keypair, client)
    success = tx is not None

    return {"success": success, "tx": tx, "price": price_estimate, "out_amount": out_amount}


def sell_token(token_address: str, token_amount: int) -> dict:
    """
    Jual token kembali ke SOL.
    token_amount = jumlah raw token (bukan desimal).
    Return: {"success": bool, "tx": str, "sol_received": float}
    """
    keypair = _get_keypair()
    client  = _get_client()

    log.info(f"📉 Selling {token_amount} of {token_address}...")

    quote = get_quote(token_address, SOL_MINT, token_amount)
    if not quote:
        return {"success": False, "tx": None, "sol_received": 0}

    out_lamports = int(quote.get("outAmount", 0))
    sol_received = out_lamports / LAMPORTS

    tx = execute_swap(quote, keypair, client)
    success = tx is not None

    return {"success": success, "tx": tx, "sol_received": sol_received}


def get_token_balance(token_address: str) -> int:
    """Return saldo token (raw amount) via JSON-RPC langsung, tanpa library SPL."""
    try:
        keypair = _get_keypair()
        wallet_pubkey = str(keypair.pubkey())

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet_pubkey,
                {"mint": token_address},
                {"encoding": "jsonParsed"}
            ]
        }
        r = requests.post(RPC_URL, json=payload, timeout=5)
        result = r.json().get("result", {})
        accounts = result.get("value", [])
        if not accounts:
            return 0
        amount_str = (
            accounts[0]
            .get("account", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
            .get("tokenAmount", {})
            .get("amount", "0")
        )
        return int(amount_str)
    except Exception as e:
        log.error(f"Balance check error: {e}")
        return 0