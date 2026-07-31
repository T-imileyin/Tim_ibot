import asyncio
import logging
import time
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = "7123456789:AAEfGhIjKlMnOpQrStUvWxYz1234567"  # Ensure your real Bot Token is pasted here
TELEGRAM_CHAT_ID = "6411468031"                  # Updated with your Personal Chat ID
CHECK_INTERVAL = 15                              # Polling frequency in seconds

# Safety Filter Limits
MAX_TOP_10_SUPPLY_PCT = 30.0                     # Block coins where Top 10 hold > 30%
MAX_RUGCHECK_SCORE = 1000                        # Block coins rated HIGH RISK (> 1000)
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
    elif diff_sec < 2592000:
        return f"{int(diff_sec / 86400)}d"
    else:
        return f"{int(diff_sec / 2592000)}mo"

async def fetch_rugcheck_report(session, mint_address):
    """Fetch RugCheck security, authority status, and top supply concentrations"""
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report/summary"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                
                # Check critical authorities
                mint_disabled = not any(r.get("name") == "Mint Authority Enabled" for r in risks)
                freeze_disabled = not any(r.get("name") == "Freeze Authority Enabled" for r in risks)
                
                # Calculate Top 10 Holders Supply Percentage
                top_holders = data.get("topHolders", [])
                top_10_pct = sum(h.get("pct", 0) for h in top_holders[:10]) if top_holders else 0
                
                status_str = "🟢 SAFE" if score < 500 else "⚠️ WARN" if score < 1000 else "🚨 HIGH RISK"
                
                return {
                    "score": score,
                    "status": status_str,
                    "mint_disabled": "✅" if mint_disabled else "❌",
                    "freeze_disabled": "✅" if freeze_disabled else "❌",
                    "top_10_pct": round(top_10_pct, 1),
                    "risks_count": len(risks)
                }
    except Exception as e:
        logging.warning(f"RugCheck request failed for {mint_address}: {e}")
    
    return {
        "score": 0, "status": "⚪ UNKNOWN",
        "mint_disabled": "❓", "freeze_disabled": "❓",
        "top_10_pct": 0, "risks_count": 0
    }

def build_soul_styled_message(pair_data, rug_data):
    """Formats pair & audit data into Soul Scanner style layout"""
    base_token = pair_data.get("baseToken", {})
    symbol = base_token.get("symbol", "UNKNOWN")
    name = base_token.get("name", "")
    mint_addr = base_token.get("address", "")
    
    # Age & Financial Metrics
    created_at = pair_data.get("pairCreatedAt")
    age_str = format_coin_age(created_at)
    
    mc = pair_data.get("fdv") or pair_data.get("marketCap", 0)
    mc_formatted = f"${mc:,.0f}" if mc < 1000000 else f"${mc/1000000:.2f}M"
    
    liq_usd = pair_data.get("liquidity", {}).get("usd", 0)
    liq_sol = pair_data.get("liquidity", {}).get("quote", 0)
    liq_formatted = f"${liq_usd:,.0f}" if liq_usd < 1000000 else f"${liq_usd/1000000:.2f}M"
    
    vol_1h = pair_data.get("volume", {}).get("h1", 0)
    vol_formatted = f"${vol_1h:,.0f}" if vol_1h < 1000000 else f"${vol_1h/1000000:.1f}k"
    
    # Smart Money / KOL Badges
    boosts = pair_data.get("boosts", {}).get("active", 0)
    smart_money_badge = "🧠 <b>Smart Money:</b> High Interest 🔥" if boosts > 10 or mc > 500000 else "🧠 <b>Smart Money:</b> Neutral"
    
    # Social Media Badges
    info = pair_data.get("info", {})
    socials = info.get("socials", [])
    websites = info.get("websites", [])
    has_x = "✅" if any(s.get("type") == "twitter" for s in socials) else "❌"
    has_web = "✅" if len(websites) > 0 else "❌"
    
    # Top 10 Bundle Bar
    top_10 = rug_data["top_10_pct"]
    bundle_bar = "📦" * min(int(top_10 // 10), 10) if top_10 > 0 else "📦 (Clean)"
    
    # Assemble Message Body
    msg = (
        f"💊🔁 <b>{name}</b> • <b>${symbol}</b>\n"
        f"🕒 <b>Age:</b> {age_str} • 🤝 <b>CTO:</b> N/A\n"
        f"💰 <b>MC:</b> {mc_formatted} • 🔝 <b>Status:</b> {rug_data['status']}\n"
        f"💧 <b>Liq:</b> {liq_formatted} [{liq_sol:,.0f} SOL]\n"
        f"📊 <b>Vol (1h):</b> {vol_formatted}\n\n"
        f"🛡️ <b>Security:</b> Mint: {rug_data['mint_disabled']} | Freeze: {rug_data['freeze_disabled']}\n"
        f"{smart_money_badge}\n"
        f"🦅 <b>Dex:</b> Boosts⚡ {boosts} | 🔗 X:{has_x} WEB:{has_web}\n"
        f"👥 <b>Top 10 Supply:</b> {top_10}%\n"
        f"🎯 <b>Supply Concentration:</b>\n{bundle_bar}\n\n"
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
    seen_mints = set()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tokens = await fetch_latest_pairs(session)
                for token in tokens:
                    mint_addr = token.get("tokenAddress")
                    chain = token.get("chainId")
                    
                    if chain == "solana" and mint_addr and mint_addr not in seen_mints:
                        seen_mints.add(mint_addr)
                        pair_data = await fetch_dex_pair_data(session, mint_addr)
                        
                        if pair_data:
                            # Fetch Security & Audit Data
                            rug_data = await fetch_rugcheck_report(session, mint_addr)
                            
                            # FILTER 1: Skip coins with high RugCheck risk scores
                            if rug_data["score"] >= MAX_RUGCHECK_SCORE:
                                logging.info(f"Skipping {mint_addr}: High risk score ({rug_data['score']})")
                                continue
                            
                            # FILTER 2: Skip coins with heavy top 10 supply / snipes
                            if rug_data["top_10_pct"] > MAX_TOP_10_SUPPLY_PCT:
                                logging.info(f"Skipping {mint_addr}: Top 10 supply too high ({rug_data['top_10_pct']}%)")
                                continue
                            
                            # If coin passes all checks, send Telegram alert
                            msg = build_soul_styled_message(pair_data, rug_data)
                            reply_markup = get_quick_buy_keyboard(mint_addr)
                            
                            await bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=msg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                reply_markup=reply_markup
                            )
            except Exception as e:
                logging.error(f"Error in polling loop: {e}")
                
            await asyncio.sleep(CHECK_INTERVAL)

# Commands
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Solana DEX Scanner Active with Risk & Snipe Filters!")

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows manual scan of any token: /check <mint_address>"""
    if not context.args:
        await update.message.reply_text("Usage: `/check <solana_mint_address>`", parse_mode="Markdown")
        return
    
    mint_addr = context.args[0]
    await update.message.reply_text(f"🔍 Scanning token `{mint_addr}`...", parse_mode="Markdown")
    
    async with aiohttp.ClientSession() as session:
        pair_data = await fetch_dex_pair_data(session, mint_addr)
        if not pair_data:
            await update.message.reply_text("❌ Token not found or missing liquidity pair on DEX Screener.")
            return
            
        rug_data = await fetch_rugcheck_report(session, mint_addr)
        msg = build_soul_styled_message(pair_data, rug_data)
        reply_markup = get_quick_buy_keyboard(mint_addr)
        
        await update.message.reply_text(
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )

async def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("check", check_command))
    
    # Correct Async Application lifecycle initialization
    async with app:
        await app.start()
        await app.updater.start_polling()
        
        # Start background polling task
        asyncio.create_task(poll_dex_screener(app.bot))
        
        logging.info("🚀 Solana DEX Scanner initialized...")
        
        # Keep application running indefinitely
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
