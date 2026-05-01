# ============================================================
#  SOLANA POST-BOND SCALPING BOT - CONFIG
#  Edit semua settings di sini sebelum jalanin bot
# ============================================================

# === WALLET ===
PRIVATE_KEY = ""  # base58 format dari Phantom

# === RPC ENDPOINT ===
# Gratis tapi lambat:
RPC_URL = ""
# Recommended (lebih cepat, daftar gratis di helius.dev):
# RPC_URL = "https://mainnet.helius-rpc.com/?api-key=API_KEY_KAMU"

# === TRADE SETTINGS ===
BUY_AMOUNT_SOL = 0.03          # SOL per entry (jangan gede-gede dulu)
MAX_POSITIONS   = 3            # Maksimal posisi terbuka bersamaan
SLIPPAGE_BPS    = 2000         # 20% slippage (volatile token)
PRIORITY_FEE_LAMPORTS = 500_000  # Priority fee biar transaksi ga gagal

# === 3-DIP STRATEGY ===
DIP1_DROP_PCT   = 0.20   # Dip pertama, minimal turun 20% dari ATH awal
DIP2_DROP_PCT   = 0.15   # Dip kedua, minimal turun 15%
DIP3_DROP_PCT   = 0.10   # Dip ketiga (entry signal)
BOUNCE_CONFIRM_PCT = 0.05  # Harga naik 5% dari dip = konfirmasi bounce

# === EXIT SETTINGS ===
TAKE_PROFIT_PCT = 0.35   # Sell kalau profit 35%
STOP_LOSS_PCT   = 0.15   # Cut loss kalau rugi 15%

# === FILTER TOKEN (Anti-Rug) ===
MIN_LIQUIDITY_USD    = 5_000    # Minimal likuiditas $5k di Raydium
MAX_TOP_HOLDER_PCT   = 0.15     # Reject kalau 1 wallet pegang >15% supply
MIN_HOLDER_COUNT     = 50       # Minimal 50 holder
MIN_VOLUME_5MIN_USD  = 1_000    # Volume 5 menit pertama minimal $1k

# === MONITORING ===
POLL_INTERVAL_SEC = 3    # Cek harga tiap 3 detik
LOG_FILE = "bot.log"
