import asyncio
import difflib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

CHECK_INTERVAL = 15
TRACKED_COIN_MAX_AGE_HOURS = 72
DEV_RECHECK_INTERVAL_SECONDS = 900

MIN_HOLDERS = 2000
MAX_TOP_10_SUPPLY_PCT = 30.0
MAX_RUGCHECK_SCORE = 500
MIN_LIQUIDITY_USD = 5000

DEXSCREENER_CONCURRENCY = 5
RUGCHECK_CONCURRENCY = 3
HELIUS_CONCURRENCY = 3
SOL_PRICE_CACHE_SECONDS = 60

KNOWN_TICKERS = ["BONK", "WIF", "POPCAT", "PNUT", "MOODENG", "FARTCOIN", "GOAT", "PEPE", "TRUMP"]
COPYCAT_SIMILARITY_THRESHOLD = 0.82

SEEN_FILE = "seen_mints.json"
TRACKED_COINS_FILE = "tracked_coins.json"

SEEN_MINTS = set()
TRACKED_COINS = {}
TRENDING_COUNT = {}  # PATCH 6 — TRENDING TRACKER

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

dex_semaphore = asyncio.Semaphore(DEXSCREENER_CONCURRENCY)
rug_semaphore = asyncio.Semaphore(RUGCHECK_CONCURRENCY)
helius_semaphore = asyncio.Semaphore(HELIUS_CONCURRENCY)

_sol_price_cache = {"price": 180.0, "ts": 0.0}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)


# ==================== PERSISTENCE ====================
def load_persistence():
    global SEEN_MINTS, TRACKED_COINS
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                SEEN_MINTS = set(json.load(f))
        if os.path.exists(TRACKED_COINS_FILE):
            with open(TRACKED_COINS_FILE, "r") as f:
                TRACKED_COINS = json.load(f)
    except Exception as e:
        logging.error(f"Error loading persistence files: {e}")


def save_persistence():
    try:
        seen_list = list(SEEN_MINTS)[-2000:]
        with open(SEEN_FILE, "w") as f:
            json.dump(seen_list, f)
        with open(TRACKED_COINS_FILE, "w") as f:
            json.dump(TRACKED_COINS, f)
    except Exception as e:
        logging.error(f"Error saving persistence files: {e}")


def prune_tracked_coins():
    cutoff = time.time() - (TRACKED_COIN_MAX_AGE_HOURS * 3600)
    to_remove = [m for m, info in TRACKED_COINS.items() if info.get("first_seen_ts", 0) < cutoff]
    for m in to_remove:
        del TRACKED_COINS[m]
    if to_remove:
        logging.info(f"Pruned {len(to_remove)} expired tracked coins.")
        save_persistence()


# ==================== HELPERS ====================
def format_coin_age(pair_created_at_ms):
    if not pair_created_at_ms:
        return "N/A"
    diff_sec = time.time() - (pair_created_at_ms / 1000.0)
    if diff_sec < 0:
        return "Just now"
    if diff_sec < 60:
        return f"{int(diff_sec)}s"
    elif diff_sec < 3600:
        return f"{int(diff_sec / 60)}m"
    elif diff_sec < 86400:
        return f"{int(diff_sec / 3600)}h"
    else:
        return f"{int(diff_sec / 86400)}d"


def is_valid_solana_address(text: str) -> bool:
    return bool(BASE58_RE.match(text))


def is_quiet_launch_hour(pair_created_at_ms) -> bool:
    if not pair_created_at_ms:
        return False
    dt = datetime.fromtimestamp(pair_created_at_ms / 1000.0, tz=timezone.utc)
    return 6 <= dt.hour < 10


def detect_wash_trading(txns_1h, volume_1h):
    buys = txns_1h.get("buys", 0)
    sells = txns_1h.get("sells", 0)
    total_txns = buys + sells
    if total_txns == 0 or volume_1h == 0:
        return "⚪ Normal Volume"
    avg_trade_size = volume_1h / total_txns
    if volume_1h > 10000 and total_txns < 20:
        return "⚠️ WASH VOLUME DETECTED"
    elif avg_trade_size > 3000 and total_txns < 30:
        return "⚠️ SUSPICIOUS TRADE SIZE"
    return "✅ Organic Trade Ratio"


def detect_copycat(name, symbol):
    candidates = list(KNOWN_TICKERS) + [info.get("symbol", "") for info in TRACKED_COINS.values()]
    symbol_upper = (symbol or "").upper()
    for candidate in candidates:
        if not candidate:
            continue
        ratio = difflib.SequenceMatcher(None, symbol_upper, candidate.upper()).ratio()
        if ratio >= COPYCAT_SIMILARITY_THRESHOLD and symbol_upper != candidate.upper():
            return candidate
    return None


def compute_holder_trend(mint_addr, current_holders):
    info = TRACKED_COINS.get(mint_addr)
    if not info or "holder_history" not in info or not info["holder_history"]:
        return "N/A (just started tracking)"
    first_ts, first_count = info["holder_history"][0]
    elapsed_min = max((time.time() - first_ts) / 60, 1)
    delta = current_holders - first_count
    rate = delta / elapsed_min
    if rate > 0.5:
        return f"📈 Growing (+{delta} holders since tracked)"
    elif rate < -0.2:
        return f"📉 Shrinking ({delta} holders since tracked)"
    return f"➡️ Flat ({delta:+d} holders since tracked)"


# 🔥 PATCH 3 — SNIPER SCORING SYSTEM
def sniper_score(pair, rug_data, unique_traders):
    score = 0

    liq = pair.get("liquidity", {}).get("usd", 0)
    vol = pair.get("volume", {}).get("h1", 0)
    pc = pair.get("priceChange", {}).get("h1", 0)
    holders = rug_data.get("total_holders", 0)
    top10 = rug_data.get("top_10_pct", 100)

    buys = pair.get("txns", {}).get("h1", {}).get("buys", 0)
    sells = pair.get("txns", {}).get("h1", {}).get("sells", 0)

    if liq > 20000: score += 15
    if liq > 50000: score += 10

    if vol > 10000: score += 15
    if vol > 50000: score += 10

    if pc > 5: score += 10
    if pc > 15: score += 10

    if holders > 2000: score += 10
    if holders > 5000: score += 10

    if top10 < 30: score += 10

    if buys > sells * 2:
        score += 10

    if unique_traders and unique_traders > 20:
        score += 10

    return min(score, 100)


# ==================== PRICE ====================
async def get_sol_price_usd(session):
    now = time.time()
    if now - _sol_price_cache["ts"] < SOL_PRICE_CACHE_SECONDS:
        return _sol_price_cache["price"]

    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                price = data.get("solana", {}).get("usd")
                if price:
                    _sol_price_cache["price"] = float(price)
                    _sol_price_cache["ts"] = now
                    return _sol_price_cache["price"]
    except Exception as e:
        logging.warning(f"CoinGecko SOL price fetch failed: {e}")

    try:
        url = "https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112"
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                price = data.get("So11111111111111111111111111111111111111112", {}).get("usdPrice")
                if price:
                    _sol_price_cache["price"] = float(price)
                    _sol_price_cache["ts"] = now
                    return _sol_price_cache["price"]
    except Exception as e:
        logging.warning(f"Jupiter lite SOL price fetch also failed: {e}")

    return _sol_price_cache["price"]


# ==================== RUGCHECK ====================
async def fetch_rugcheck_report(session, mint_address):
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report"
    summary_url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report/summary"

    async with rug_semaphore:
        data = None
        try:
            async with session.get(url, timeout=8) as response:
                if response.status == 200:
                    data = await response.json()
        except Exception as e:
            logging.warning(f"RugCheck full report failed for {mint_address}: {e}")

        if data is None:
            try:
                async with session.get(summary_url, timeout=8) as response:
                    if response.status == 200:
                        data = await response.json()
            except Exception as e:
                logging.warning(f"RugCheck summary also failed for {mint_address}: {e}")

    if data is None:
        return {
            "ok": False, "score": 9999, "status": "🚨 UNVERIFIED",
            "total_holders": 0, "mint_disabled": "❌", "freeze_disabled": "❌",
            "top_10_pct": 100, "risks_count": 99, "risk_names": [],
            "mutable_metadata": "❌ Unknown", "lp_lock_pct": "N/A", "creator": None,
        }

    score = data.get("score", 0)
    risks = data.get("risks", [])
    total_holders = data.get("totalHolders", 0) or data.get("token", {}).get("holders", 0)
    mint_disabled = not any(r.get("name") == "Mint Authority Enabled" for r in risks)
    freeze_disabled = not any(r.get("name") == "Freeze Authority Enabled" for r in risks)
    mutable_flag = any("mutable" in r.get("name", "").lower() for r in risks)
    top_holders = data.get("topHolders", [])
    top_10_pct = sum(h.get("pct", 0) for h in top_holders[:10]) if top_holders else 0
    status_str = "🟢 SAFE" if score < 200 else "⚠️ WARN" if score < 700 else "🚨 HIGH RISK"

    lp_lock_pct = "N/A"
    try:
        markets = data.get("markets", [])
        if markets:
            lp_val = markets[0].get("lp", {}).get("lpLockedPct")
            if lp_val is not None:
                lp_lock_pct = f"{lp_val}%"
    except Exception:
        pass

    creator = data.get("creator") or data.get("token", {}).get("creator")

    return {
        "ok": True,
        "score": score,
        "status": status_str,
        "total_holders": total_holders,
        "mint_disabled": "✅" if mint_disabled else "❌",
        "freeze_disabled": "✅" if freeze_disabled else "❌",
        "top_10_pct": round(top_10_pct, 1),
        "risks_count": len(risks),
        "risk_names": [r.get("name", "") for r in risks][:5],
        "mutable_metadata": "⚠️ Mutable" if mutable_flag else "✅ Immutable",
        "lp_lock_pct": lp_lock_pct,
        "creator": creator,
    }


# ==================== HELIUS ====================
def helius_rpc_url():
    return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"


TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


async def fetch_accurate_holder_count(session, mint_address):
    if not HELIUS_API_KEY:
        return None
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            TOKEN_PROGRAM_ID,
            {
                "encoding": "jsonParsed",
                "filters": [
                    {"dataSize": 165},
                    {"memcmp": {"offset": 0, "bytes": mint_address}},
                ],
            },
        ],
    }
    async with helius_semaphore:
        try:
            async with session.post(helius_rpc_url(), json=payload, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    accounts = data.get("result", [])
                    holders = 0
                    for acc in accounts:
                        try:
                            amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
                            if amount and amount > 0:
                                holders += 1
                        except (KeyError, TypeError):
                            continue
                    return holders
        except Exception as e:
            logging.warning(f"Helius holder count fetch failed for {mint_address}: {e}")
    return None


async def fetch_wallet_sol_balance(session, wallet_address):
    if not HELIUS_API_KEY or not wallet_address:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet_address]}
    async with helius_semaphore:
        try:
            async with session.post(helius_rpc_url(), json=payload, timeout=8) as response:
                if response.status == 200:
                    data = await response.json()
                    lamports = data.get("result", {}).get("value")
                    if lamports is not None:
                        return lamports / 1_000_000_000
        except Exception as e:
            logging.warning(f"Helius balance fetch failed for {wallet_address}: {e}")
    return None


async def fetch_dev_token_transfers(session, dev_wallet, mint_address, limit=25):
    if not HELIUS_API_KEY or not dev_wallet:
        return None
    url = f"https://api.helius.xyz/v0/addresses/{dev_wallet}/transactions?api-key={HELIUS_API_KEY}&limit={limit}"
    async with helius_semaphore:
        try:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    txns = await response.json()
                    total_out = 0.0
                    for tx in txns:
                        for transfer in tx.get("tokenTransfers", []):
                            if (
                                transfer.get("mint") == mint_address
                                and transfer.get("fromUserAccount") == dev_wallet
                            ):
                                total_out += transfer.get("tokenAmount", 0)
                    return total_out
        except Exception as e:
            logging.warning(f"Helius transaction fetch failed for {dev_wallet}: {e}")
    return None


async def fetch_unique_traders_1h(session, mint_address, limit=40):
    if not HELIUS_API_KEY:
        return None
    url = f"https://api.helius.xyz/v0/addresses/{mint_address}/transactions?api-key={HELIUS_API_KEY}&limit={limit}"
    async with helius_semaphore:
        try:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    txns = await response.json()
                    one_hour_ago = time.time() - 3600
                    wallets = set()
                    for tx in txns:
                        if tx.get("timestamp", 0) >= one_hour_ago and tx.get("type") == "SWAP":
                            wallets.add(tx.get("feePayer"))
                    return len(wallets)
        except Exception as e:
            logging.warning(f"Helius unique-trader fetch failed for {mint_address}: {e}")
    return None


# ==================== MESSAGE CARD ====================
def build_advanced_card(pair_data, rug_data, sol_price_usd, dev_info=None,
                        holder_trend=None, copycat_of=None, unique_traders=None,
                        extra_flag=None):
    base_token = pair_data.get("baseToken", {})
    symbol = base_token.get("symbol", "UNKNOWN")
    name = base_token.get("name", "")
    mint_addr = base_token.get("address", "")

    age_str = format_coin_age(pair_data.get("pairCreatedAt"))
    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
    mc_formatted = f"${mc:,.0f}" if mc < 1_000_000 else f"${mc / 1_000_000:.2f}M"

    liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
    liq_sol = pair_data.get("liquidity", {}).get("quote", 0)
    if liq_usd == 0 and liq_sol > 0:
        liq_usd = liq_sol * sol_price_usd
    liq_formatted = f"${liq_usd:,.0f}" if liq_usd < 1_000_000 else f"${liq_usd / 1_000_000:.2f}M"

    vol_1h = pair_data.get("volume", {}).get("h1", 0)
    txns_1h = pair_data.get("txns", {}).get("h1", {})
    wash_status = detect_wash_trading(txns_1h, vol_1h)

    header_title = extra_flag if extra_flag else f"{name} • ${symbol}"
    risk_list = ", ".join(rug_data["risk_names"]) if rug_data["risk_names"] else "None flagged"

    lines = [
        f"💊 <b>{header_title}</b>",
        f"{rug_data['status']} • Score: {rug_data['score']}",
    ]
    if copycat_of:
        lines.append(f"🚩 <b>Name similar to:</b> {copycat_of} — check it's not a copycat")
    if is_quiet_launch_hour(pair_data.get("pairCreatedAt")):
        lines.append("🌙 Launched during historically thinner overnight hours (heuristic, not a rule)")

    lines += [
        "",
        f"🕒 <b>Age:</b> {age_str}",
        f"💰 <b>MC:</b> {mc_formatted}",
        f"💧 <b>Liq:</b> {liq_formatted} [{liq_sol:.1f} SOL] • LP locked: {rug_data['lp_lock_pct']}",
        f"📊 <b>Vol (1h):</b> ${vol_1h:,.0f} — {wash_status}",
    ]
    if unique_traders is not None:
        lines.append(f"👤 <b>Unique traders (1h, approx):</b> {unique_traders}")

    lines += [
        "",
        f"👥 <b>Holders:</b> {rug_data['total_holders']} • <b>Top 10:</b> {rug_data['top_10_pct']}%",
        f"📈 <b>Holder trend:</b> {holder_trend or 'N/A'}",
        f"🔒 <b>Mint disabled:</b> {rug_data['mint_disabled']} • <b>Freeze disabled:</b> {rug_data['freeze_disabled']} • <b>Metadata:</b> {rug_data['mutable_metadata']}",
        f"⚠️ <b>Flags:</b> {risk_list}",
    ]

    if dev_info:
        lines += [
            "",
            f"🛠️ <b>Dev wallet:</b> {dev_info.get('short_addr', 'N/A')}",
            f"💰 <b>Dev SOL balance:</b> {dev_info.get('sol_balance_str', 'N/A')}",
            f"📤 <b>Dev token transfers out:</b> {dev_info.get('sold_str', 'N/A')}",
        ]

    lines += ["", f"<code>{mint_addr}</code>"]
    return "\n".join(lines)


def get_quick_buy_keyboard(mint_addr):
    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{mint_addr}"),
        ],
        [
            InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{mint_addr}"),
            InlineKeyboardButton("Trojan", url=f"https://t.me/solana_trojanbot?start=r-YOUR_REF_CODE-{mint_addr}"),
            InlineKeyboardButton("Photon", url=f"https://photon-sol.tinyastro.io/en/lp/{mint_addr}"),
        ],
        [
            InlineKeyboardButton("BonkBot", url=f"https://t.me/bonkbot_bot?start=ref_YOUR_REF_CODE_ca_{mint_addr}"),
            InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{mint_addr}?chain=solana"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ==================== DEXSCREENER ====================
async def fetch_latest_pairs(session):
    endpoints = [
        "https://api.dexscreener.com/token-profiles/latest/v1",
        "https://api.dexscreener.com/latest/dex/search?q=solana"
    ]
    for url in endpoints:
        try:
            async with dex_semaphore:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, list):
                            return data
                        elif isinstance(data, dict):
                            return data.get("tokens", data.get("pairs", []))
        except Exception as e:
            logging.warning(f"Failed fetching from {url}: {e}")
    return []


async def fetch_dex_pair_data(session, mint_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    async with dex_semaphore:
        try:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    pairs = data.get("pairs", [])
                    if pairs:
                        return pairs[0]
        except Exception as e:
            logging.warning(f"Failed fetching pair data for {mint_address}: {e}")
    return None


# ==================== ANALYSIS ORCHESTRATION ====================
async def build_dev_info(session, creator_wallet, mint_addr):
    if not creator_wallet:
        return None
    sol_balance = await fetch_wallet_sol_balance(session, creator_wallet)
    sold_amount = await fetch_dev_token_transfers(session, creator_wallet, mint_addr)
    return {
        "short_addr": f"{creator_wallet[:4]}...{creator_wallet[-4:]}",
        "sol_balance_str": f"{sol_balance:.2f} SOL" if sol_balance is not None else "N/A (set HELIUS_API_KEY)",
        "sold_str": f"{sold_amount:,.0f} tokens moved out" if sold_amount is not None else "N/A (set HELIUS_API_KEY)",
    }


def ensure_tracked(mint_addr, symbol, mc, creator):
    if mint_addr not in TRACKED_COINS:
        now_ts = time.time()
        TRACKED_COINS[mint_addr] = {
            "entry_mc": mc,
            "last_mc": mc,
            "symbol": symbol,
            "ath_multiplier": 1.0,
            "first_seen_ts": now_ts,
            "creator": creator,
            "holder_history": [],
            "last_dev_check_ts": now_ts,
            "last_score": 0,
        }


async def run_full_analysis(session, mint_addr, extra_flag=None, record_snapshot=True):
    pair_data = await fetch_dex_pair_data(session, mint_addr)
    if not pair_data:
        return None, None

    sol_price = await get_sol_price_usd(session)
    rug_data = await fetch_rugcheck_report(session, mint_addr)

    accurate_holders = await fetch_accurate_holder_count(session, mint_addr)
    if accurate_holders is not None:
        rug_data["total_holders"] = accurate_holders

    symbol = pair_data.get("baseToken", {}).get("symbol", "")
    name = pair_data.get("baseToken", {}).get("name", "")
    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)

    ensure_tracked(mint_addr, symbol, mc, rug_data.get("creator"))

    if record_snapshot:
        info = TRACKED_COINS[mint_addr]
        info.setdefault("holder_history", []).append([time.time(), rug_data["total_holders"]])
        info["holder_history"] = info["holder_history"][-20:]
        save_persistence()

    dev_info = await build_dev_info(session, rug_data.get("creator"), mint_addr)
    holder_trend = compute_holder_trend(mint_addr, rug_data["total_holders"])
    copycat_of = detect_copycat(name, symbol)
    unique_traders = await fetch_unique_traders_1h(session, mint_addr)

    msg = build_advanced_card(
        pair_data, rug_data, sol_price,
        dev_info=dev_info, holder_trend=holder_trend,
        copycat_of=copycat_of, unique_traders=unique_traders,
        extra_flag=extra_flag,
    )
    return msg, rug_data


# ==================== MAIN POLLING LOOP ====================
async def poll_dex_screener(bot):
    backoff = CHECK_INTERVAL
    load_persistence()
    last_prune = time.time()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                sol_price = await get_sol_price_usd(session)
                tokens = await fetch_latest_pairs(session)
                backoff = CHECK_INTERVAL

                for token in tokens:
                    mint_addr = token.get("tokenAddress") or token.get("address")
                    chain = token.get("chainId")

                    # 🔥 PATCH 2 — FIX SEEN LOGIC
                    if chain != "solana" or not mint_addr:
                        continue

                    if mint_addr in SEEN_MINTS:
                        continue

                    pair_data = await fetch_dex_pair_data(session, mint_addr)
                    if not pair_data:
                        continue

                    liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
                    liq_sol = pair_data.get("liquidity", {}).get("quote", 0)
                    if liq_usd == 0 and liq_sol > 0:
                        liq_usd = liq_sol * sol_price

                    rug_data = await fetch_rugcheck_report(session, mint_addr)
                    if not rug_data["ok"]:
                        continue

                    accurate_holders = await fetch_accurate_holder_count(session, mint_addr)
                    if accurate_holders is not None:
                        rug_data["total_holders"] = accurate_holders

                    # 🔥 PATCH 4 — STRICT ENTRY FILTER
                    if not (
                        liq_usd > 20000 and
                        pair_data.get("volume", {}).get("h1", 0) > 10000 and
                        pair_data.get("priceChange", {}).get("h1", 0) > 5 and
                        rug_data["total_holders"] >= 2000
                    ):
                        continue

                    if (
                        rug_data["score"] > MAX_RUGCHECK_SCORE or
                        rug_data["top_10_pct"] > MAX_TOP_10_SUPPLY_PCT
                    ):
                        continue

                    # 🔥 PATCH 2 — Mark SEEN ONLY AFTER passing all filters
                    SEEN_MINTS.add(mint_addr)

                    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
                    symbol = pair_data.get("baseToken", {}).get("symbol", "UNKNOWN")
                    now_ts = time.time()

                    ensure_tracked(mint_addr, symbol, mc, rug_data.get("creator"))
                    TRACKED_COINS[mint_addr]["holder_history"] = [[now_ts, rug_data["total_holders"]]]

                    name = pair_data.get("baseToken", {}).get("name", "")
                    dev_info = await build_dev_info(session, rug_data.get("creator"), mint_addr)
                    holder_trend = compute_holder_trend(mint_addr, rug_data["total_holders"])
                    copycat_of = detect_copycat(name, symbol)
                    unique_traders = await fetch_unique_traders_1h(session, mint_addr)

                    msg = build_advanced_card(
                        pair_data, rug_data, sol_price,
                        dev_info=dev_info, holder_trend=holder_trend,
                        copycat_of=copycat_of, unique_traders=unique_traders,
                    )

                    # 🔥 PATCH 7 — SMART MONEY SIGNAL (USING HELIUS)
                    if unique_traders and unique_traders > 25:
                        msg = "🧠 <b>SMART MONEY ACTIVE</b>\n" + msg

                    # 🔥 PATCH 5 — LATE ENTRY SIGNAL
                    score = sniper_score(pair_data, rug_data, unique_traders)
                    prev_score = TRACKED_COINS[mint_addr].get("last_score", 0)

                    if score >= 75 and prev_score < 75:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text="🔁 <b>LATE SNIPER ENTRY</b>\n" + msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=get_quick_buy_keyboard(mint_addr),
                        )
                    else:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=get_quick_buy_keyboard(mint_addr),
                        )

                    TRACKED_COINS[mint_addr]["last_score"] = score

                    # 🔥 PATCH 6 — TRENDING ENGINE
                    TRENDING_COUNT[mint_addr] = TRENDING_COUNT.get(mint_addr, 0) + 1
                    if TRENDING_COUNT[mint_addr] >= 3:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text="🔥 <b>TRENDING COIN</b>\n" + msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=get_quick_buy_keyboard(mint_addr),
                        )
                        TRENDING_COUNT[mint_addr] = 0

                    save_persistence()

                # Re-check tracked coins: price milestones + periodic holder/dev snapshots
                for mint_addr, info in list(TRACKED_COINS.items()):
                    pdata = await fetch_dex_pair_data(session, mint_addr)
                    if not pdata:
                        continue

                    curr_mc = pdata.get("fdv") or pdata.get("marketCap", 0)
                    entry_mc = info["entry_mc"]
                    if entry_mc > 0:
                        mult = curr_mc / entry_mc
                        if mult >= info["ath_multiplier"] * 2:
                            info["ath_multiplier"] = mult
                            save_persistence()
                            await bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=(f"🚀 <b>MILESTONE ALERT!</b>\n${info['symbol']} hit <b>{mult:.1f}x</b> "
                                      f"from entry! (MC: ${curr_mc:,.0f})\n<code>{mint_addr}</code>"),
                                parse_mode="HTML",
                            )

                    if time.time() - info.get("last_dev_check_ts", 0) > DEV_RECHECK_INTERVAL_SECONDS:
                        rug_data = await fetch_rugcheck_report(session, mint_addr)
                        if rug_data["ok"]:
                            info.setdefault("holder_history", []).append([time.time(), rug_data["total_holders"]])
                            info["holder_history"] = info["holder_history"][-20:]
                        info["last_dev_check_ts"] = time.time()
                        save_persistence()

                        dev_wallet = info.get("creator")
                        if dev_wallet and HELIUS_API_KEY:
                            total_sold = await fetch_dev_token_transfers(session, dev_wallet, mint_addr)
                            if total_sold is not None:
                                previously_reported = info.get("dev_sold_reported", 0.0)
                                new_amount = total_sold - previously_reported
                                if new_amount > 0.01:
                                    await bot.send_message(
                                        chat_id=TELEGRAM_CHAT_ID,
                                        text=(f"🔴 <b>DEV ACTIVITY:</b> ${info['symbol']} dev wallet moved "
                                              f"{new_amount:,.0f} more tokens out (total so far: {total_sold:,.0f}).\n"
                                              f"<code>{mint_addr}</code>"),
                                        parse_mode="HTML",
                                    )
                                info["dev_sold_reported"] = total_sold
                                save_persistence()

                if time.time() - last_prune > 3600:
                    prune_tracked_coins()
                    last_prune = time.time()

            except Exception as e:
                logging.error(f"Error in scanner loop (backing off): {e}")
                backoff = min(backoff * 2, 120)

            await asyncio.sleep(backoff)


# ==================== COMMAND & MESSAGE HANDLERS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    helius_note = "" if HELIUS_API_KEY else "\n\n⚠️ HELIUS_API_KEY not set — dev wallet tracking is disabled."
    msg = (
        "🚀 <b>Solana Scanner Active</b>\n"
        "Scanning Dexscreener for new Solana pairs, cross-checking against RugCheck, "
        "and tracking dev wallets, holder growth, LP locks, and copycat names."
        f"{helius_note}\n\nSend a contract address any time, or use /check &lt;address&gt;."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <mint_address>")
        return

    mint_addr = context.args[0].strip()
    if not is_valid_solana_address(mint_addr):
        await update.message.reply_text("❌ Invalid Solana mint address format.")
        return

    status_msg = await update.message.reply_text("🔍 Fetching token analysis...")

    async with aiohttp.ClientSession() as session:
        msg, rug_data = await run_full_analysis(session, mint_addr, extra_flag="🔍 Manual Lookup")
        if not msg:
            await status_msg.edit_text("❌ Could not retrieve pair data from DexScreener.")
            return

        await status_msg.edit_text(
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_quick_buy_keyboard(mint_addr)
        )


async def handle_contract_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message and update.message.text else ""
    if is_valid_solana_address(text):
        status_msg = await update.message.reply_text("🔍 Analyzing mint address...")
        async with aiohttp.ClientSession() as session:
            msg, rug_data = await run_full_analysis(session, text, extra_flag="🔍 Direct Address Lookup")
            if not msg:
                await status_msg.edit_text("❌ Could not retrieve pair data for this token.")
                return

            await status_msg.edit_text(
                text=msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=get_quick_buy_keyboard(text)
            )


async def handle_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Refreshing data...")

    data = query.data
    if data.startswith("refresh:"):
        mint_addr = data.split("refresh:")[1]
        async with aiohttp.ClientSession() as session:
            msg, rug_data = await run_full_analysis(
                session, mint_addr, extra_flag="🔄 Refreshed Snapshot", record_snapshot=True
            )
            if msg:
                try:
                    await query.edit_message_text(
                        text=msg,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=get_quick_buy_keyboard(mint_addr)
                    )
                except Exception as e:
                    logging.warning(f"Error updating message: {e}")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Exception while handling an update: {context.error}")


# ✅ PATCH 1 — FIX TELEGRAM MAIN & RUN_POLLING
async def main():
    if not TELEGRAM_BOT_TOKEN:
        logging.critical("TELEGRAM_BOT_TOKEN not set")
        return

    if not TELEGRAM_CHAT_ID:
        logging.critical("CHAT_ID not set")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_contract_message))
    app.add_handler(CallbackQueryHandler(handle_refresh_callback, pattern=r"^refresh:"))
    app.add_error_handler(global_error_handler)

    asyncio.create_task(poll_dex_screener(app.bot))

    logging.info("🚀 Bot started")

    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
