import asyncio
import logging
import aiohttp
from telegram import Bot

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = "8809386346:AAEt_7REbKpPEJIS5uV06GXbVCYMflE1M44"
TELEGRAM_CHAT_ID = "6411468031"

CHECK_INTERVAL = 15  # Scan interval in seconds

# Your Strict Criteria
MIN_HOLDERS = 2000
MAX_TOP10_HOLDERS_PCT = 25.0
MAX_SNIPER_PCT = 10.0
# =================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
seen_tokens = set()

def format_number(val):
    """Formats raw numbers into readable strings (e.g. $2.1M, $146K)."""
    if val is None:
        return "N/A"
    val = float(val)
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    elif val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:.1f}"

async def fetch_dex_pair_data(session: aiohttp.ClientSession, mint_address: str) -> dict:
    """Fetches market cap, liquidity, volume, and social links from DEX Screener."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    # Return primary/highest liquidity pair
                    return pairs[0]
    except Exception as e:
        logging.error(f"DEX Screener fetch error: {e}")
    return {}

async def audit_token_rugcheck(session: aiohttp.ClientSession, mint_address: str) -> tuple[bool, str, dict]:
    """Audits token security & holder metrics via RugCheck API."""
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report"
    try:
        async with session.get(url, timeout=12) as resp:
            if resp.status != 200:
                return False, f"RugCheck API returned status {resp.status}", {}

            data = await resp.json()

            # 1. Authority Checks
            mint_disabled = not data.get("token", {}).get("mintAuthority")
            freeze_disabled = not data.get("token", {}).get("freezeAuthority")

            if not mint_disabled:
                return False, "Mint authority is enabled", {}
            if not freeze_disabled:
                return False, "Freeze authority is enabled", {}

            # 2. Holder Metrics
            total_holders = data.get("totalHolders", 0)
            if total_holders < MIN_HOLDERS:
                return False, f"Holders too low ({total_holders} < {MIN_HOLDERS})", {}

            # Calculate Top 10 Holders Percentage
            top_holders = data.get("topHolders", [])
            top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
            if top10_pct > MAX_TOP10_HOLDERS_PCT:
                return False, f"Top 10 holders too high ({top10_pct:.1f}% > {MAX_TOP10_HOLDERS_PCT}%)", {}

            # 3. Sniper / Block 0 Check
            risks = data.get("risks", [])
            sniper_pct = 0.0
            for r in risks:
                if "sniper" in r.get("name", "").lower() or "block 0" in r.get("description", "").lower():
                    sniper_pct = float(r.get("value", 0.0))

            if sniper_pct > MAX_SNIPER_PCT:
                return False, f"Block 0/Sniper allocation too high ({sniper_pct:.1f}% > {MAX_SNIPER_PCT}%)", {}

            # Extract Dev details & Bundles
            dev_address = data.get("creator", "Unknown")
            dev_sol_bal = data.get("creatorBalance", 0) / 1e9 if data.get("creatorBalance") else 0.0

            parsed_data = {
                "holders": total_holders,
                "top10_pct": top10_pct,
                "sniper_pct": sniper_pct,
                "dev_address": dev_address,
                "dev_sol": dev_sol_bal,
                "risks_count": len(risks),
                "score": data.get("score", 0)
            }

            return True, "Passed all criteria", parsed_data

    except Exception as e:
        return False, f"RugCheck Audit Exception: {e}", {}

async def send_ttf_styled_alert(bot: Bot, pair: dict, audit: dict):
    """Formats and dispatches the exact TTF-styled Telegram notification card."""
    base_token = pair.get("baseToken", {})
    token_name = base_token.get("name", "Unknown Token")
    token_symbol = base_token.get("symbol", "UNKNOWN")
    mint_addr = base_token.get("address", "")

    mc = format_number(pair.get("fdv") or pair.get("marketCap"))
    liq = format_number(pair.get("liquidity", {}).get("usd"))
    vol_1h = format_number(pair.get("volume", {}).get("h1"))
    
    dex_url = pair.get("url", f"https://dexscreener.com/solana/{mint_addr}")
    solscan_url = f"https://solscan.io/token/{mint_addr}"

    message = (
        f"💊🔁 *{token_name}* • `${token_symbol}`\n"
        f"🕒 *Age:* New Listing • 🤝 *CTO Check:* Passed\n"
        f"💰 *MC:* `{mc}`\n"
        f"💧 *Liq:* `{liq}`\n"
        f"📊 *Vol (1h):* `{vol_1h}`\n\n"
        f"🦅 *Dex:* [DexScreener]({dex_url})\n"
        f"⚡ *Scans Audit:* Safe Score `{audit.get('score')}` | 🔗 [Solscan]({solscan_url})\n"
        f"👥 *Hodls:* `{audit.get('holders'):,}` • *Top 10:* `{audit.get('top10_pct'):.1f}%`\n\n"
        f"🔫 *Snipers / Block 0:* `{audit.get('sniper_pct'):.1f}%` 🤍\n"
        f"🎯 *Top 10 Supply:* `{audit.get('top10_pct'):.1f}%`\n\n"
        f"🛠️ *Dev Wallet:* `{audit.get('dev_sol'):.2f} SOL`\n"
        f"📋 *Contract:* `{mint_addr}`"
    )

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def main():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logging.info("🚀 TTF Token Filter Bot Active! Polling DEX Screener...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Poll DEX Screener for recently created/updated profiles
                async with session.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10) as resp:
                    if resp.status == 200:
                        profiles = await resp.json()
                        for p in profiles:
                            if p.get("chainId") != "solana":
                                continue

                            mint_addr = p.get("tokenAddress")
                            if not mint_addr or mint_addr in seen_tokens:
                                continue

                            seen_tokens.add(mint_addr)

                            # Perform Security & Holder Checks
                            passed, reason, audit_metrics = await audit_token_rugcheck(session, mint_addr)
                            
                            if passed:
                                pair_data = await fetch_dex_pair_data(session, mint_addr)
                                if pair_data:
                                    logging.info(f"✅ PASSED: {mint_addr}")
                                    await send_ttf_styled_alert(bot, pair_data, audit_metrics)
                            else:
                                logging.info(f"❌ REJECTED ({mint_addr[:8]}...): {reason}")

            except Exception as e:
                logging.error(f"Polling loop error: {e}")

            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
python-telegram-bot==22.8
aiohttp==3.14.3