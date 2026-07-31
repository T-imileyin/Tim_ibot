import asyncio
import logging
import os
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import aiohttp

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Configuration Constants
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
CHAT_ID = os.getenv("CHAT_ID", "")  # Optional: default chat ID for auto-alerts

CHECK_INTERVAL = 30  # Seconds between automated DEX scans
MAX_WATCHLIST_SIZE = 20

# In-Memory Watchlist Storage (RAM)
WATCHLIST = {}  # Format: {mint_address: {"added_at": timestamp, "initial_mc": float}}

# Helper: Format Coin Age
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
    elif diff_sec < 2592000:
        return f"{int(diff_sec / 86400)}d"
    else:
        return f"{int(diff_sec / 2592000)}mo"

# Helper: Format Currency Numbers
def format_num(val):
    if not val:
        return "$0"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    elif val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:,.0f}"

# API Fetcher: DEX Screener Token Data
async def fetch_dex_pair_data(session, mint_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs")
                if pairs:
                    # Prefer Solana pairs
                    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                    return sol_pairs[0] if sol_pairs else pairs[0]
    except Exception as e:
        logging.error(f"Error fetching DEX Screener data for {mint_address}: {e}")
    return None

# API Fetcher: RugCheck Summary
async def fetch_rugcheck_data(session, mint_address):
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report/summary"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logging.error(f"Error fetching RugCheck data for {mint_address}: {e}")
    return None

# Build Soul-Styled Message
def build_soul_styled_message(pair_data, rug_data=None, note=""):
    base_token = pair_data.get("baseToken", {})
    symbol = base_token.get("symbol", "UNKNOWN")
    name = base_token.get("name", "")
    mint_addr = base_token.get("address", "")

    age_str = format_coin_age(pair_data.get("pairCreatedAt"))
    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
    liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
    liq_sol = pair_data.get("liquidity", {}).get("quote", 0)
    vol_1h = pair_data.get("volume", {}).get("h1", 0)

    # Price changes
    p_change_1h = pair_data.get("priceChange", {}).get("h1", 0)

    # Socials
    info = pair_data.get("info", {})
    socials = info.get("socials", [])
    websites = info.get("websites", [])
    has_x = "✅" if any(s.get("type") == "twitter" for s in socials) else "❌"
    has_web = "✅" if len(websites) > 0 else "❌"

    # Security & Intelligence Heuristics
    txns_1h = pair_data.get("txns", {}).get("h1", {})
    total_txns = txns_1h.get("buys", 0) + txns_1h.get("sells", 0)
    
    # Fake Volume Flag
    fake_vol_warning = ""
    if vol_1h > 100000 and total_txns < 30:
        fake_vol_warning = "\n⚠️ <b>ALERT:</b> High chance of Fake Wash Volume!"

    # RugCheck Status
    mint_auth = "Disabled ✅"
    freeze_auth = "Disabled ✅"
    if rug_data:
        risks = rug_data.get("risks", [])
        for r in risks:
            if "Mint Authority" in r.get("name", ""):
                mint_auth = "Enabled 🚨"
            if "Freeze Authority" in r.get("name", ""):
                freeze_auth = "Enabled 🚨"

    header_prefix = f"<b>[{note}]</b>\n" if note else ""

    msg = (
        f"{header_prefix}"
        f"💊🔁 <b>{name}</b> • <b>${symbol}</b>\n"
        f"🕒 <b>Age:</b> {age_str} [-{abs(p_change_1h):.0f}%] • 🤝 <b>CTO</b>\n"
        f"💰 <b>MC:</b> {format_num(mc)}\n"
        f"💧 <b>Liq:</b> {format_num(liq_usd)} [{liq_sol:,.0f} SOL]\n"
        f"📊 <b>Vol (1h):</b> {format_num(vol_1h)}{fake_vol_warning}\n\n"
        f"🦅 <b>Dex:</b> Paid✅ Ads❌ | 🔗 X:{has_x} WEB:{has_web}\n"
        f"🛡️ <b>Mint:</b> {mint_auth} | <b>Freeze:</b> {freeze_auth}\n\n"
        f"<code>{mint_addr}</code>"
    )

    # Action Buttons
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
    reply_markup = InlineKeyboardMarkup(keyboard)
    return msg, reply_markup

# Telegram Command: /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🤖 <b>Solana Scanner & Watchlist Bot Active!</b>\n\n"
        "<b>Commands:</b>\n"
        "• <code>/add &lt;mint_address&gt;</code> - Add coin to watchlist (max 20)\n"
        "• <code>/remove &lt;mint_address&gt;</code> - Remove coin from watchlist\n"
        "• <code>/list</code> - View tracked coins with live performance\n"
        "• <code>/clear</code> - Clear all coins from watchlist"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

# Telegram Command: /add <mint>
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(WATCHLIST) >= MAX_WATCHLIST_SIZE:
        await update.message.reply_text(f"❌ Watchlist is full! Maximum limit is {MAX_WATCHLIST_SIZE} coins.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Please provide a mint address:\n<code>/add &lt;mint_address&gt;</code>", parse_mode="HTML")
        return

    mint_addr = context.args[0].strip()

    if mint_addr in WATCHLIST:
        await update.message.reply_text("ℹ️ This coin is already in your watchlist.")
        return

    async with aiohttp.ClientSession() as session:
        pair_data = await fetch_dex_pair_data(session, mint_addr)
        if not pair_data:
            await update.message.reply_text("❌ Could not find token data on DEX Screener. Check the mint address.")
            return

        mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
        WATCHLIST[mint_addr] = {
            "added_at": time.time(),
            "initial_mc": mc,
            "symbol": pair_data.get("baseToken", {}).get("symbol", "UNKNOWN")
        }

        rug_data = await fetch_rugcheck_data(session, mint_addr)
        msg, reply_markup = build_soul_styled_message(pair_data, rug_data, note="ADDED TO WATCHLIST")
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=reply_markup)

# Telegram Command: /remove <mint>
async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: <code>/remove &lt;mint_address&gt;</code>", parse_mode="HTML")
        return

    mint_addr = context.args[0].strip()
    if mint_addr in WATCHLIST:
        symbol = WATCHLIST[mint_addr].get("symbol", "")
        del WATCHLIST[mint_addr]
        await update.message.reply_text(f"✅ Removed <b>${symbol}</b> (<code>{mint_addr}</code>) from watchlist.", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Coin not found in watchlist.")

# Telegram Command: /clear
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    WATCHLIST.clear()
    await update.message.reply_text("🧹 Watchlist cleared completely!")

# Telegram Command: /list
async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST:
        await update.message.reply_text("📋 Your watchlist is currently empty. Add coins with <code>/add &lt;mint&gt;</code>", parse_mode="HTML")
        return

    status_msg = await update.message.reply_text("🔄 Fetching live data for tracked coins...")

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

            # PnL performance since added
            init_mc = data.get("initial_mc", mc)
            pnl_perc = (((mc - init_mc) / init_mc) * 100) if init_mc > 0 else 0
            pnl_str = f"+{pnl_perc:.1f}%" if pnl_perc >= 0 else f"{pnl_perc:.1f}%"

            items_text.append(
                f"<b>{idx}. ${symbol}</b> • <code>{mint_addr[:6]}...{mint_addr[-4:]}</code>\n"
                f"   🕒 Age: {age_str} | 💧 Liq: {format_num(liq_usd)} | 💰 MC: {format_num(mc)}\n"
                f"   📈 PnL: <b>{pnl_str}</b> | 1h Change: {p_change_1h:.1f}%\n"
            )

    report = f"📋 <b>LIVE WATCHLIST ({len(WATCHLIST)}/{MAX_WATCHLIST_SIZE})</b>\n\n" + "\n".join(items_text)
    await status_msg.edit_text(report, parse_mode="HTML")

# Background Scanner Loop (Dip & Rebound Alerts for Tracked Coins)
async def background_watchlist_scanner(app):
    while True:
        try:
            if WATCHLIST:
                async with aiohttp.ClientSession() as session:
                    for mint_addr in list(WATCHLIST.keys()):
                        pair_data = await fetch_dex_pair_data(session, mint_addr)
                        if pair_data:
                            p_change_1h = pair_data.get("priceChange", {}).get("h1", 0)
                            # Alert if tracked coin dips significantly (-20% to -60%)
                            if -60 <= p_change_1h <= -20:
                                rug_data = await fetch_rugcheck_data(session, mint_addr)
                                msg, reply_markup = build_soul_styled_message(pair_data, rug_data, note="DIP DETECTED 📉")
                                if CHAT_ID:
                                    await app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"Error in background scanner: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

# Main Application Entry Point
def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("❌ Please set your TELEGRAM_BOT_TOKEN environment variable!")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Add Command Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("list", list_command))

    logging.info("Bot started successfully!")
    app.run_polling()

if __name__ == "__main__":
    main()
