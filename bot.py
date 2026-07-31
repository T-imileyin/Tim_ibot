import asyncio
import logging
import os
import time
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==================== CONFIGURATION ====================
# Reads token from Railway Environment Variables first. 
# Fallback: Replace the second argument if you prefer hardcoding.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "YOUR_TELEGRAM_CHAT_ID_HERE")

CHECK_INTERVAL = 15            # Polling frequency in seconds
MAX_WATCHLIST_SIZE = 20        # Watchlist cap

# Safety Filter Limits
MAX_TOP_10_SUPPLY_PCT = 30.0   # Block coins where Top 10 hold > 30%
MAX_RUGCHECK_SCORE = 1000      # Block coins rated HIGH RISK (> 1000)
# =======================================================

# In-Memory Watchlist Storage (RAM)
WATCHLIST = {}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

def format_coin_age(pair_created_at_ms):
    """Calculates readable coin age from UNIX timestamp"""
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
    elif diff_sec < 2592000:
        return f"{int(diff_sec / 86400)}d"
    else:
        return f"{int(diff_sec / 2592000)}mo"

def format_num(val):
    """Formats numeric values into human-readable strings"""
    if not val:
        return "$0"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    elif val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:,.0f}"

async def fetch_rugcheck_report(session, mint_address):
    """Fetch RugCheck security, authority status, and top supply concentrations"""
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
                
                status_str = "🟢 SAFE" if score < 500 else "⚠️ WARN" if score < 1000 else "🚨 HIGH RISK"
                
                return {
                    "score": score,
                    "status": status_str,
                    "mint_disabled": "Disabled ✅" if mint_disabled else "Enabled 🚨",
                    "freeze_disabled": "Disabled ✅" if freeze_disabled else "Enabled 🚨",
                    "top_10_pct": round(top_10_pct, 1),
                    "risks_count": len(risks)
                }
    except Exception as e:
        logging.warning(f"RugCheck request failed for {mint_address}: {e}")
    
    return {
        "score": 0, "status": "⚪ UNKNOWN",
        "mint_disabled": "Unknown ❓", "freeze_disabled": "Unknown ❓",
        "top_10_pct": 0, "risks_count": 0
    }

async def fetch_dex_pair_data(session, mint_address):
    """Fetch live Solana pair metrics from DEX Screener"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get("pairs", [])
                if pairs:
                    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                    return sol_pairs[0] if sol_pairs else pairs[0]
    except Exception as e:
        logging.error(f"Error fetching pair data for {mint_address}: {e}")
    return None

def build_soul_styled_message(pair_data, rug_data, note=""):
    """Formats pair & audit data into Soul Scanner layout"""
    base_token = pair_data.get("baseToken", {})
    symbol = base_token.get("symbol", "UNKNOWN")
    name = base_token.get("name", "")
    mint_addr = base_token.get("address", "")
    
    created_at = pair_data.get("pairCreatedAt")
    age_str = format_coin_age(created_at)
    
    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
    liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
    liq_sol = pair_data.get("liquidity", {}).get("quote", 0)
    vol_1h = pair_data.get("volume", {}).get("h1", 0)
    p_change_1h = pair_data.get("priceChange", {}).get("h1", 0)
    
    # Fake Volume / Wash Trading Shield
    txns_1h = pair_data.get("txns", {}).get("h1", {})
    total_txns = txns_1h.get("buys", 0) + txns_1h.get("sells", 0)
    fake_vol_warning = ""
    if vol_1h > 100000 and total_txns < 30:
        fake_vol_warning = "\n⚠️ <b>ALERT:</b> High chance of Fake Wash Volume!"
    
    # Socials
    info = pair_data.get("info", {})
    socials = info.get("socials", [])
    websites = info.get("websites", [])
    has_x = "✅" if any(s.get("type") == "twitter" for s in socials) else "❌"
    has_web = "✅" if len(websites) > 0 else "❌"
    
    top_10 = rug_data["top_10_pct"]
    bundle_bar = "📦" * min(int(top_10 // 10), 10) if top_10 > 0 else "📦 Clean"
    header_prefix = f"<b>[{note}]</b>\n" if note else ""

    msg = (
        f"{header_prefix}"
        f"💊🔁 <b>{name}</b> • <b>${symbol}</b>\n"
        f"🕒 <b>Age:</b> {age_str} [{p_change_1h:+.0f}%] • 🤝 <b>CTO</b>\n"
        f"💰 <b>MC:</b> {format_num(mc)} • 🔝 <b>Status:</b> {rug_data['status']}\n"
        f"💧 <b>Liq:</b> {format_num(liq_usd)} [{liq_sol:,.0f} SOL]\n"
        f"📊 <b>Vol (1h):</b> {format_num(vol_1h)}{fake_vol_warning}\n\n"
        f"🦅 <b>Dex:</b> Paid✅ Ads❌ | 🔗 X:{has_x} WEB:{has_web}\n"
        f"🛡️ <b>Mint:</b> {rug_data['mint_disabled']} | <b>Freeze:</b> {rug_data['freeze_disabled']}\n"
        f"👥 <b>Top 10 Supply:</b> {top_10}% ({bundle_bar})\n\n"
        f"<code>{mint_addr}</code>"
    )
    return msg

def get_quick_buy_keyboard(mint_addr):
    """Generates Telegram Inline Quick Buy Buttons"""
    keyboard = [
        [
            InlineKeyboardButton("📊 DEXScreener", url=f"https://dexscreener.com/solana/{mint_addr}"),
            InlineKeyboardButton("⚡ Photon", url=f"https://photon-sol.tinyastro.io/en/r/@bot/{mint_addr}")
        ],
        [
            InlineKeyboardButton("🤖 Trojan", url=f"https://t.me/solana_trojanbot?start=r-user-{mint_addr}"),
            InlineKeyboardButton("🚀 BonkBot", url=f"https://t.me/bonkbot_bot?start={mint_addr}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== TELEGRAM COMMANDS ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🤖 <b>Solana Scanner & Watchlist Active!</b>\n\n"
        "<b>Commands:</b>\n"
        "• <code>/check &lt;mint&gt;</code> - Perform live risk/token analysis\n"
        "• <code>/add &lt;mint&gt;</code> - Add coin to active watchlist (max 20)\n"
        "• <code>/remove &lt;mint&gt;</code> - Remove coin from watchlist\n"
        "• <code>/list</code> - View tracked coins with live performance\n"
        "• <code>/clear</code> - Reset entire watchlist"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: <code>/check &lt;mint_address&gt;</code>", parse_mode="HTML")
        return
    
    mint_addr = context.args[0].strip()
    status_msg = await update.message.reply_text(f"🔍 Scanning token <code>{mint_addr[:8]}...</code>", parse_mode="HTML")
    
    async with aiohttp.ClientSession() as session:
        pair_data = await fetch_dex_pair_data(session, mint_addr)
        if not pair_data:
            await status_msg.edit_text("❌ Token not found or missing liquidity pair on DEX Screener.")
            return
            
        rug_data = await fetch_rugcheck_report(session, mint_addr)
        msg = build_soul_styled_message(pair_data, rug_data, note="MANUAL CHECK")
        reply_markup = get_quick_buy_keyboard(mint_addr)
        await status_msg.edit_text(text=msg, parse_mode="HTML", reply_markup=reply_markup)

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(WATCHLIST) >= MAX_WATCHLIST_SIZE:
        await update.message.reply_text(f"❌ Watchlist is full! Maximum limit is {MAX_WATCHLIST_SIZE} coins.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/add &lt;mint_address&gt;</code>", parse_mode="HTML")
        return

    mint_addr = context.args[0].strip()
    if mint_addr in WATCHLIST:
        await update.message.reply_text("ℹ️ This coin is already in your watchlist.")
        return

    async with aiohttp.ClientSession() as session:
        pair_data = await fetch_dex_pair_data(session, mint_addr)
        if not pair_data:
            await update.message.reply_text("❌ Could not find token data on DEX Screener.")
            return

        mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
        WATCHLIST[mint_addr] = {
            "added_at": time.time(),
            "initial_mc": mc,
            "symbol": pair_data.get("baseToken", {}).get("symbol", "UNKNOWN")
        }

        rug_data = await fetch_rugcheck_report(session, mint_addr)
        msg = build_soul_styled_message(pair_data, rug_data, note="ADDED TO WATCHLIST")
        reply_markup = get_quick_buy_keyboard(mint_addr)
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=reply_markup)

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/remove &lt;mint_address&gt;</code>", parse_mode="HTML")
        return

    mint_addr = context.args[0].strip()
    if mint_addr in WATCHLIST:
        symbol = WATCHLIST[mint_addr].get("symbol", "")
        del WATCHLIST[mint_addr]
        await update.message.reply_text(f"✅ Removed <b>${symbol}</b> from watchlist.", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Coin not found in watchlist.")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    WATCHLIST.clear()
    await update.message.reply_text("🧹 Watchlist cleared completely!")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST:
        await update.message.reply_text("📋 Watchlist is empty. Add coins with <code>/add &lt;mint&gt;</code>", parse_mode="HTML")
        return

    status_msg = await update.message.reply_text("🔄 Fetching live status...")
    items_text = []

    async with aiohttp.ClientSession() as session:
        for idx, (mint_addr, data) in enumerate(list(WATCHLIST.items()), start=1):
            pair_data = await fetch_dex_pair_data(session, mint_addr)
            if not pair_data:
                items_text.append(f"{idx}. <code>{mint_addr[:8]}...</code> - Data fetch failed")
                continue

            symbol = pair_data.get("baseToken", {}).get("symbol", "UNKNOWN")
            age_str = format_coin_age(pair_data.get("pairCreatedAt"))
            mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
            liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
            p_change_1h = pair_data.get("priceChange", {}).get("h1", 0)

            init_mc = data.get("initial_mc", mc)
            pnl_perc = (((mc - init_mc) / init_mc) * 100) if init_mc > 0 else 0
            pnl_str = f"+{pnl_perc:.1f}%" if pnl_perc >= 0 else f"{pnl_perc:.1f}%"

            items_text.append(
                f"<b>{idx}. ${symbol}</b> • <code>{mint_addr[:6]}...{mint_addr[-4:]}</code>\n"
                f"   🕒 Age: {age_str} | 💧 Liq: {format_num(liq_usd)} | 💰 MC: {format_num(mc)}\n"
                f"   📈 PnL: <b>{pnl_str}</b> | 1h: {p_change_1h:+.1f}%\n"
            )

    report = f"📋 <b>WATCHLIST PERFORMANCE ({len(WATCHLIST)}/{MAX_WATCHLIST_SIZE})</b>\n\n" + "\n".join(items_text)
    await status_msg.edit_text(report, parse_mode="HTML")

# Background Scanner Loop (Watchlist Dip Detection)
async def background_watchlist_scanner(app):
    while True:
        try:
            if WATCHLIST:
                async with aiohttp.ClientSession() as session:
                    for mint_addr in list(WATCHLIST.keys()):
                        pair_data = await fetch_dex_pair_data(session, mint_addr)
                        if pair_data:
                            p_change_1h = pair_data.get("priceChange", {}).get("h1", 0)
                            if -60 <= p_change_1h <= -20:
                                rug_data = await fetch_rugcheck_report(session, mint_addr)
                                msg = build_soul_styled_message(pair_data, rug_data, note="DIP DETECTED 📉")
                                reply_markup = get_quick_buy_keyboard(mint_addr)
                                if TELEGRAM_CHAT_ID and "YOUR_" not in TELEGRAM_CHAT_ID:
                                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"Error in background scanner: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

def main():
    # Token Safety Gate
    if not TELEGRAM_BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
        print("❌ CRITICAL ERROR: TELEGRAM_BOT_TOKEN is invalid or missing.")
        print("Please set your TELEGRAM_BOT_TOKEN environment variable in Railway or edit Line 11.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("list", list_command))

    logging.info("🚀 Solana Watchlist & Safety Scanner started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()
