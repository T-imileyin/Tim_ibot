import asyncio
import logging
import os
import time
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8809386346:AAEt_7REbKpPEJIS5uV06GXbVCYMflE1M44")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "6411468031")
CHECK_INTERVAL = 15                            # Polling frequency in seconds

# Security & Scan Filters
MAX_TOP_10_SUPPLY_PCT = 30.0                   # Block coins where Top 10 hold > 30%
MAX_RUGCHECK_SCORE = 1000                      # Block coins rated HIGH RISK (> 1000)
MIN_LIQUIDITY_USD = 2000                       # Filter out sub-$2k liquidity pools
MAX_WATCHLIST_SIZE = 20                        # RAM Watchlist cap

# In-Memory Watchlist Storage: { mint_address: { "added_at": timestamp, "symbol": str } }
WATCHLIST = {}
SEEN_MINTS = set()
PRICE_HISTORY = {}                             # Memory tracker for Dip Detection
# =======================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

def format_coin_age(pair_created_at_ms):
    """Calculates readable coin age from UNIX timestamp"""
    if not pair_created_at_ms:
        return "N/A"
    
    created_at_sec = pair_created_at_ms / 1000.0
    diff_sec = time.time() - created_at_sec
    
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

async def fetch_rugcheck_report(session, mint_address):
    """Fetch RugCheck security, authority status, and holder clusters"""
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report/summary"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                
                mint_disabled = not any(r.get("name") == "Mint Authority Enabled" for r in risks)
                freeze_disabled = not any(r.get("name") == "Freeze Authority Enabled" for r in risks)
                
                top_holders = data.get("topHolders", [])
                top_10_pct = sum(h.get("pct", 0) for h in top_holders[:10]) if top_holders else 0
                
                # Check for single wallet clusters holding identical %
                cluster_detected = False
                if len(top_holders) >= 5:
                    pcts = [round(h.get("pct", 0), 2) for h in top_holders[:5]]
                    if len(set(pcts)) <= 2:  # High concentration of identical holder amounts
                        cluster_detected = True
                
                status_str = "🟢 SAFE" if score < 500 else "⚠️ WARN" if score < 1000 else "🚨 HIGH RISK"
                
                return {
                    "score": score,
                    "status": status_str,
                    "mint_disabled": "✅" if mint_disabled else "❌",
                    "freeze_disabled": "✅" if freeze_disabled else "❌",
                    "top_10_pct": round(top_10_pct, 1),
                    "cluster_warning": "🕸️ CLUSTER ALERT: Linked Holders" if cluster_detected else "✅ Dist. Normal",
                    "risks_count": len(risks)
                }
    except Exception as e:
        logging.warning(f"RugCheck request failed for {mint_address}: {e}")
    
    return {
        "score": 0, "status": "⚪ UNKNOWN",
        "mint_disabled": "❓", "freeze_disabled": "❓",
        "top_10_pct": 0, "cluster_warning": "❓ Unknown", "risks_count": 0
    }

def detect_wash_trading(txns_1h, volume_1h):
    """Calculates Real Volume vs Wash Volume Heuristic Ratio"""
    buys = txns_1h.get("buys", 0)
    sells = txns_1h.get("sells", 0)
    total_txns = buys + sells
    
    if total_txns == 0 or volume_1h == 0:
        return "⚪ Normal Volume"
        
    avg_trade_size = volume_1h / total_txns
    
    # High volume with suspicious low transaction count indicates wash bots
    if volume_1h > 10000 and total_txns < 15:
        return "⚠️ WASH VOLUME DETECTED (High Vol / Low Txns)"
    elif avg_trade_size > 5000 and total_txns < 30:
        return "⚠️ SUSPICIOUS VOLUME RATIO"
    return "✅ Organic Trade Ratio"

def detect_zombie_revival(pair_data):
    """Detect Zombie Coin Volume Spikes / Community Takeovers"""
    created_at = pair_data.get("pairCreatedAt", time.time() * 1000)
    age_hours = (time.time() - (created_at / 1000.0)) / 3600.0
    
    vol_5m = pair_data.get("volume", {}).get("m5", 0)
    vol_1h = pair_data.get("volume", {}).get("h1", 0)
    
    # Coin is older than 24 hours, but 5m volume represents > 60% of total 1h volume
    if age_hours > 24 and vol_1h > 1000 and (vol_5m / max(vol_1h, 1)) > 0.6:
        return "🧟 ZOMBIE REVIVAL / CTO SPIKE"
    return None

def detect_dip_rebound(mint_addr, current_price):
    """Tracks price changes to detect dip and rebounds"""
    if mint_addr not in PRICE_HISTORY:
        PRICE_HISTORY[mint_addr] = current_price
        return None
        
    old_price = PRICE_HISTORY[mint_addr]
    PRICE_HISTORY[mint_addr] = current_price
    
    if old_price == 0:
        return None
        
    pct_change = ((current_price - old_price) / old_price) * 100
    if pct_change <= -20.0:
        return f"📉 DIP ALERT: Dropped {pct_change:.1f}% (Rebound Opportunity)"
    elif pct_change >= 50.0:
        return f"🚀 PUMP ALERT: Surge +{pct_change:.1f}%"
    return None

def build_advanced_card(pair_data, rug_data, extra_flag=None):
    """Formats pair & security data into full signal card layout"""
    base_token = pair_data.get("baseToken", {})
    symbol = base_token.get("symbol", "UNKNOWN")
    name = base_token.get("name", "")
    mint_addr = base_token.get("address", "")
    
    created_at = pair_data.get("pairCreatedAt")
    age_str = format_coin_age(created_at)
    
    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
    mc_formatted = f"${mc:,.0f}" if mc < 1000000 else f"${mc/1000000:.2f}M"
    
    liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
    liq_formatted = f"${liq_usd:,.0f}" if liq_usd < 1000000 else f"${liq_usd/1000000:.2f}M"
    
    price_usd = float(pair_data.get("priceUsd", 0))
    vol_1h = pair_data.get("volume", {}).get("h1", 0)
    txns_1h = pair_data.get("txns", {}).get("h1", {})
    
    wash_status = detect_wash_trading(txns_1h, vol_1h)
    zombie_status = detect_zombie_revival(pair_data)
    dip_status = detect_dip_rebound(mint_addr, price_usd)
    
    # Priority banner header
    banner = "💊🔁 <b>SOLANA GEM SCANNER</b>"
    if extra_flag:
        banner = f"🚨 <b>{extra_flag}</b>"
    elif zombie_status:
        banner = f"🧟 <b>{zombie_status}</b>"
    elif dip_status:
        banner = f"📉 <b>{dip_status}</b>"

    info = pair_data.get("info", {})
    socials = info.get("socials", [])
    has_x = "✅" if any(s.get("type") == "twitter" for s in socials) else "❌"
    has_web = "✅" if len(info.get("websites", [])) > 0 else "❌"
    
    top_10 = rug_data["top_10_pct"]
    bundle_bar = "📦" * min(int(top_10 // 10), 10) if top_10 > 0 else "📦 Clean"

    msg = (
        f"{banner}\n"
        f"<b>{name}</b> • <b>${symbol}</b>\n"
        f"🕒 <b>Age:</b> {age_str} | 💰 <b>MC:</b> {mc_formatted}\n"
        f"💧 <b>Liq:</b> {liq_formatted} | 🔝 <b>Status:</b> {rug_data['status']}\n\n"
        f"🛡️ <b>Security:</b> Mint: {rug_data['mint_disabled']} | Freeze: {rug_data['freeze_disabled']}\n"
        f"👥 <b>Top 10 Supply:</b> {top_10}% ({rug_data['cluster_warning']})\n"
        f"🎯 <b>Supply Bar:</b> {bundle_bar}\n"
        f"📊 <b>Volume Integrity:</b> {wash_status}\n"
        f"🔗 <b>Socials:</b> X: {has_x} | Web: {has_web}\n\n"
        f"<code>{mint_addr}</code>"
    )
    return msg

def get_quick_buy_keyboard(mint_addr):
    """Generates Telegram Inline Quick Buy Buttons"""
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
                            
                            # Filters
                            if rug_data["score"] >= MAX_RUGCHECK_SCORE or rug_data["top_10_pct"] > MAX_TOP_10_SUPPLY_PCT:
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
                logging.error(f"Error in main scanner loop: {e}")
                
            await asyncio.sleep(CHECK_INTERVAL)

# ==================== COMMAND HANDLERS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🚀 <b>Solana Pro Scanner & Watchlist Active!</b>\n\n"
        "<b>Commands:</b>\n"
        "• `/check <mint>` - Instant audit (Zombie, Wash, RugCheck, Clusters)\n"
        "• `/add <mint>` - Add coin to RAM Watchlist (Max 20)\n"
        "• `/remove <mint>` - Remove coin from Watchlist\n"
        "• `/list` - View live Watchlist status report\n"
        "• `/clear` - Clear all saved watchlist coins"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/check <solana_mint_address>`", parse_mode="Markdown")
        return
    
    mint_addr = context.args[0]
    await update.message.reply_text(f"🔍 Auditing token `{mint_addr}`...", parse_mode="Markdown")
    
    async with aiohttp.ClientSession() as session:
        pair_data = await fetch_dex_pair_data(session, mint_addr)
        if not pair_data:
            await update.message.reply_text("❌ Token not found or missing DEX Screener pair.")
            return
            
        rug_data = await fetch_rugcheck_report(session, mint_addr)
        msg = build_advanced_card(pair_data, rug_data, extra_flag="MANUAL AUDIT REPORT")
        reply_markup = get_quick_buy_keyboard(mint_addr)
        
        await update.message.reply_text(
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/add <solana_mint_address>`", parse_mode="Markdown")
        return
    
    if len(WATCHLIST) >= MAX_WATCHLIST_SIZE:
        await update.message.reply_text(f"⚠️ Watchlist is full! Maximum limit is {MAX_WATCHLIST_SIZE} coins. Use `/remove` to free up space.")
        return
        
    mint_addr = context.args[0]
    
    async with aiohttp.ClientSession() as session:
        pair_data = await fetch_dex_pair_data(session, mint_addr)
        symbol = pair_data.get("baseToken", {}).get("symbol", "TOKEN") if pair_data else "TOKEN"
        
    WATCHLIST[mint_addr] = {"added_at": time.time(), "symbol": symbol}
    await update.message.reply_text(f"✅ Saved <b>${symbol}</b> (<code>{mint_addr}</code>) to Watchlist! [{len(WATCHLIST)}/{MAX_WATCHLIST_SIZE}]", parse_mode="HTML")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/remove <solana_mint_address>`", parse_mode="Markdown")
        return
    
    mint_addr = context.args[0]
    if mint_addr in WATCHLIST:
        symbol = WATCHLIST[mint_addr].get("symbol", "Token")
        del WATCHLIST[mint_addr]
        await update.message.reply_text(f"🗑️ Removed <b>${symbol}</b> from Watchlist. [{len(WATCHLIST)}/{MAX_WATCHLIST_SIZE}]", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Token not found in active RAM watchlist.")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    WATCHLIST.clear()
    await update.message.reply_text("🧹 Watchlist cleared completely.")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST:
        await update.message.reply_text("📋 Your RAM Watchlist is empty. Add coins with `/add <mint>`.", parse_mode="Markdown")
        return
    
    await update.message.reply_text("🔄 Fetching live status report for watchlist...")
    
    msg = f"📋 <b>RAM Watchlist Status Report [{len(WATCHLIST)}/{MAX_WATCHLIST_SIZE}]</b>\n\n"
    
    async with aiohttp.ClientSession() as session:
        for idx, (mint, meta) in enumerate(WATCHLIST.items(), 1):
            pair_data = await fetch_dex_pair_data(session, mint)
            if pair_data:
                symbol = pair_data.get("baseToken", {}).get("symbol", meta["symbol"])
                mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
                mc_formatted = f"${mc:,.0f}" if mc < 1000000 else f"${mc/1000000:.2f}M"
                vol_1h = pair_data.get("volume", {}).get("h1", 0)
                vol_formatted = f"${vol_1h:,.0f}" if vol_1h < 1000000 else f"${vol_1h/1000000:.1f}k"
                
                msg += f"{idx}. <b>${symbol}</b> • MC: {mc_formatted} | 1h Vol: {vol_formatted}\n<code>{mint}</code>\n\n"
            else:
                msg += f"{idx}. <b>${meta['symbol']}</b> • (Data Unavailable)\n<code>{mint}</code>\n\n"
                
    await update.message.reply_text(msg, parse_mode="HTML")

async def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logging.critical("CRITICAL ERROR: TELEGRAM_BOT_TOKEN is invalid or missing.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Register Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("list", list_command))
    
    async with app:
        await app.start()
        await app.updater.start_polling()
        
        asyncio.create_task(poll_dex_screener(app.bot))
        logging.info("🚀 Solana Pro Scanner Bot Started Successfully!")
        
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
