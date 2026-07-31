import asyncio
import logging
import os
import time
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8809386346:AAESBd_4bp-bbiHWvHG4eHdxiUFCi1qhDFc")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")
CHECK_INTERVAL = 15                            # Polling frequency in seconds

# STRICT SECURITY FILTERS
MIN_HOLDERS = 2000                             # Require at least 2,000 holders
MAX_TOP_10_SUPPLY_PCT = 30.0                   # Max allowed supply in Top 10 wallets
MAX_AIRDROP_PCT = 10.0                         # Max allowed Airdrop supply
MAX_SNIPE_PCT = 10.0                           # Max allowed Block 0 Snipe supply

MAX_RUGCHECK_SCORE = 500                       # Allow slightly higher score if all limits pass
MIN_LIQUIDITY_USD = 5000                       # Require at least $5k USD liquidity
MAX_WATCHLIST_SIZE = 20                        

WATCHLIST = {}
SEEN_MINTS = set()
PRICE_HISTORY = {}                             
# =======================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

def format_coin_age(pair_created_at_ms):
    if not pair_created_at_ms:
        return "N/A"
    created_at_sec = pair_created_at_ms / 1000.0
    diff_sec = time.time() - created_at_sec
    if diff_sec < 0: return "Just now"
    if diff_sec < 60: return f"{int(diff_sec)}s"
    elif diff_sec < 3600: return f"{int(diff_sec / 60)}m"
    elif diff_sec < 86400: return f"{int(diff_sec / 3600)}h"
    else: return f"{int(diff_sec / 86400)}d"

async def fetch_rugcheck_report(session, mint_address):
    """Fetch RugCheck security, parsing exact % for snipes, airdrops, and top holder risks"""
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report/summary"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                
                # Extract Holders (often at the top level or within a token object)
                total_holders = data.get("totalHolders", 0)
                if total_holders == 0 and data.get("token"):
                    total_holders = data.get("token", {}).get("holders", 0)
                
                # Check for authority risks
                mint_disabled = not any(r.get("name") == "Mint Authority Enabled" for r in risks)
                freeze_disabled = not any(r.get("name") == "Freeze Authority Enabled" for r in risks)
                
                # Calculate Top 10 percentage
                top_holders = data.get("topHolders", [])
                top_10_pct = sum(h.get("pct", 0) for h in top_holders[:10]) if top_holders else 0
                
                # Parse exact risk percentages for Airdrops and Snipes
                airdrop_pct = 0.0
                snipe_pct = 0.0
                for r in risks:
                    name = r.get("name", "").lower()
                    try:
                        # Attempt to parse numeric value from the risk profile
                        val = float(r.get("value", 0))
                    except (ValueError, TypeError):
                        val = 0.0
                    
                    if "airdrop" in name:
                        airdrop_pct = max(airdrop_pct, val)
                    if "snipe" in name:
                        snipe_pct = max(snipe_pct, val)

                status_str = "🟢 SAFE" if score < 200 else "⚠️ WARN" if score < 700 else "🚨 HIGH RISK"
                
                return {
                    "score": score,
                    "status": status_str,
                    "total_holders": total_holders,
                    "mint_disabled": "✅" if mint_disabled else "❌",
                    "freeze_disabled": "✅" if freeze_disabled else "❌",
                    "top_10_pct": round(top_10_pct, 1),
                    "airdrop_pct": round(airdrop_pct, 1),
                    "snipe_pct": round(snipe_pct, 1),
                    "risks_count": len(risks)
                }
    except Exception as e:
        logging.warning(f"RugCheck request failed for {mint_address}: {e}")
    
    return {
        "score": 9999, "status": "🚨 HIGH RISK",
        "total_holders": 0, "mint_disabled": "❌", "freeze_disabled": "❌",
        "top_10_pct": 100, "airdrop_pct": 100, "snipe_pct": 100, "risks_count": 99
    }

def detect_wash_trading(txns_1h, volume_1h):
    buys = txns_1h.get("buys", 0)
    sells = txns_1h.get("sells", 0)
    total_txns = buys + sells
    if total_txns == 0 or volume_1h == 0: return "⚪ Normal Volume"
    avg_trade_size = volume_1h / total_txns
    if volume_1h > 10000 and total_txns < 20: return "⚠️ WASH VOLUME DETECTED"
    elif avg_trade_size > 3000 and total_txns < 30: return "⚠️ SUSPICIOUS TRADE SIZE"
    return "✅ Organic Trade Ratio"

def build_advanced_card(pair_data, rug_data, extra_flag=None):
    base_token = pair_data.get("baseToken", {})
    symbol = base_token.get("symbol", "UNKNOWN")
    name = base_token.get("name", "")
    mint_addr = base_token.get("address", "")
    
    age_str = format_coin_age(pair_data.get("pairCreatedAt"))
    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
    mc_formatted = f"${mc:,.0f}" if mc < 1000000 else f"${mc/1000000:.2f}M"
    liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
    liq_formatted = f"${liq_usd:,.0f}" if liq_usd < 1000000 else f"${liq_usd/1000000:.2f}M"
    
    vol_1h = pair_data.get("volume", {}).get("h1", 0)
    txns_1h = pair_data.get("txns", {}).get("h1", {})
    wash_status = detect_wash_trading(txns_1h, vol_1h)
    
    banner = f"💊🔁 <b>SOLANA STRICT SCANNER</b>" if not extra_flag else f"🚨 <b>{extra_flag}</b>"

    msg = (
        f"{banner}\n"
        f"<b>{name}</b> • <b>${symbol}</b>\n"
        f"🕒 <b>Age:</b> {age_str} | 💰 <b>MC:</b> {mc_formatted}\n"
        f"💧 <b>Liq:</b> {liq_formatted} | 🔝 <b>Status:</b> {rug_data['status']}\n\n"
        f"👩‍👧‍👦 <b>Holders:</b> {rug_data['total_holders']} | <b>Top10:</b> {rug_data['top_10_pct']}%\n"
        f"💸 <b>Airdrops:</b> {rug_data['airdrop_pct']}%\n"
        f"🥡 <b>Block 0 Snipes:</b> {rug_data['snipe_pct']}%\n\n"
        f"🛡️ <b>Security:</b> Mint: {rug_data['mint_disabled']} | Freeze: {rug_data['freeze_disabled']}\n"
        f"📊 <b>Volume Integrity:</b> {wash_status}\n\n"
        f"<code>{mint_addr}</code>"
    )
    return msg

def get_quick_buy_keyboard(mint_addr):
    keyboard = [
        [
            InlineKeyboardButton("🎯 Buy on Trojan", url=f"https://t.me/solana_trojanbot?start=r-user-{mint_addr}"),
            InlineKeyboardButton("⚡ Buy on BonkBot", url=f"https://t.me/bonkbot_bot?start=ref-{mint_addr}")
        ],
        [
            InlineKeyboardButton("🔮 Photon", url=f"https://photon-sol.tinyastro.io/en/lp/{mint_addr}"),
            InlineKeyboardButton("📊 DEXScreener", url=f"https://dexscreener.com/solana/{mint_addr}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def fetch_latest_pairs(session):
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                return await response.json()
    except Exception as e:
        logging.error(f"Error fetching tokens from DEX Screener: {e}")
    return []

async def fetch_dex_pair_data(session, mint_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get("pairs", [])
                if pairs:
                    return pairs[0]
    except Exception as e:
        logging.error(f"Error fetching pair data for {mint_address}: {e}")
    return None

async def poll_dex_screener(bot):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tokens = await fetch_latest_pairs(session)
                for token in tokens:
                    mint_addr = token.get("tokenAddress")
                    chain = token.get("chainId")
                    
                    if chain == "solana" and mint_addr and mint_addr not in SEEN_MINTS:
                        SEEN_MINTS.add(mint_addr)
                        pair_data = await fetch_dex_pair_data(session, mint_addr)
                        
                        if pair_data:
                            liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
                            if liq_usd < MIN_LIQUIDITY_USD:
                                continue
                                
                            rug_data = await fetch_rugcheck_report(session, mint_addr)
                            
                            # ==========================================
                            # 🔥 STRICT REJECTION LOGIC
                            # ==========================================
                            if (
                                rug_data["score"] > MAX_RUGCHECK_SCORE or 
                                rug_data["total_holders"] < MIN_HOLDERS or
                                rug_data["top_10_pct"] > MAX_TOP_10_SUPPLY_PCT or
                                rug_data["airdrop_pct"] > MAX_AIRDROP_PCT or
                                rug_data["snipe_pct"] > MAX_SNIPE_PCT
                            ):
                                continue
                            
                            msg = build_advanced_card(pair_data, rug_data)
                            reply_markup = get_quick_buy_keyboard(mint_addr)
                            
                            await bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=msg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                reply_markup=reply_markup
                            )
            except Exception as e:
                logging.error(f"Error in scanner loop: {e}")
                
            await asyncio.sleep(CHECK_INTERVAL)

# ==================== COMMAND HANDLERS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "🚀 <b>Strict Solana Pro Scanner Active!</b>\nFiltering logic applied: >2000 Holders, <30% Top 10, <10% Airdrops, <10% Snipes."
    await update.message.reply_text(msg, parse_mode="HTML")

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/check <solana_mint_address>`", parse_mode="Markdown")
        return
    
    mint_addr = context.args[0]
    async with aiohttp.ClientSession() as session:
        pair_data = await fetch_dex_pair_data(session, mint_addr)
        if not pair_data:
            await update.message.reply_text("❌ Token not found.")
            return
            
        rug_data = await fetch_rugcheck_report(session, mint_addr)
        msg = build_advanced_card(pair_data, rug_data, extra_flag="MANUAL AUDIT REPORT")
        reply_markup = get_quick_buy_keyboard(mint_addr)
        
        await update.message.reply_text(text=msg, parse_mode="HTML", reply_markup=reply_markup)

async def main():
    if not TELEGRAM_BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        logging.critical("CRITICAL ERROR: TELEGRAM_BOT_TOKEN is invalid or missing.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("check", check_command))
    
    async with app:
        await app.start()
        await app.updater.start_polling()
        asyncio.create_task(poll_dex_screener(app.bot))
        logging.info("🚀 Ultra Strict Scanner Started!")
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
