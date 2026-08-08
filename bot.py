"""
Telegram Bet Tracker Bot
Enregistre les paris du groupe, divise par 3, track P&L et dettes.

Usage principal :
  /lock 800 Strasbourg 1N2 3,10
  â Enregistre un pari de 800 CHF sur Strasbourg 1N2 Ã  cote 3.10

RÃ©sultat : rÃ©pondre au message de confirmation avec /win ou /loss
"""
import os
import re
import sqlite3
import logging
import aiohttp
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ââ Config ââââââââââââââââââââââââââââââââââââââââââââââââââ
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SHEETS_WEBHOOK_URL = os.environ.get("SHEETS_WEBHOOK_URL", "")
NB_PARTS = 3
DB_PATH = "bets.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ââ Database ââââââââââââââââââââââââââââââââââââââââââââââââ
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER,
            user_id     INTEGER,
            user_name   TEXT,
            description TEXT,
            stake       REAL,
            odds        REAL,
            status      TEXT DEFAULT 'pending',
            created_at  TEXT,
            resolved_at TEXT
        )
    """)
    con.commit()
    con.close()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

# ââ Helpers âââââââââââââââââââââââââââââââââââââââââââââââââ
def fmt(amount: float) -> str:
    return f"+{amount:.0f} CHF" if amount >= 0 else f"{amount:.0f} CHF"

def fmt_abs(amount: float) -> str:
    return f"{abs(amount):.0f} CHF"

async def sync_sheets(payload: dict):
    """POST data to Google Sheets webhook (fire-and-forget)."""
    if not SHEETS_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEETS_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                log.info(f"Sheets sync: {payload.get('action')} â {resp.status}")
    except Exception as e:
        log.warning(f"Sheets sync failed: {e}")


def get_total_pnl(con, chat_id: int) -> float:
    rows = con.execute(
        "SELECT status, stake, odds FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
        (chat_id,)
    ).fetchall()
    total = 0.0
    for r in rows:
        if r["status"] == "won":
            total += r["stake"] * (r["odds"] - 1) / NB_PARTS
        else:
            total -= r["stake"] / NB_PARTS
    return total

# ââ /lock â Enregistrer un pari âââââââââââââââââââââââââââââ
# Format: /lock <mise> <description> <cote>
# Exemples:
#   /lock 800 Strasbourg 1N2 3,10
#   /lock 500 chf Le Mans ML @ 1.70
#   /lock 200 Arsenal O2.5 buts 1,85
#
# RÃ¨gle : le premier nombre = mise, le dernier nombre = cote,
#         tout entre les deux = description

LOCK_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?)"      # groupe 1 : mise (premier nombre)
    r"\s*(?:chf)?\s+"          # optionnel "CHF" aprÃ¨s la mise
    r"(.+?)\s+"                # groupe 2 : description (tout au milieu)
    r"(?:@\s*)?"               # optionnel "@" avant la cote
    r"(\d+[.,]\d+)",           # groupe 3 : cote (dernier nombre avec dÃ©cimale)
    re.IGNORECASE
)

async def cmd_lock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/lock â register a new bet."""
    if not ctx.args:
        await update.message.reply_text(
            "Format : /lock <mise> <description> <cote>\n"
            "Ex: /lock 800 Strasbourg 1N2 3,10"
        )
        return

    raw = " ".join(ctx.args)
    m = LOCK_PATTERN.search(raw)
    if not m:
        await update.message.reply_text(
            "Format pas reconnu.\n"
            "Ex: /lock 800 Strasbourg 1N2 3,10"
        )
        return

    stake = float(m.group(1).replace(",", "."))
    desc = m.group(2).strip()
    # Clean description: remove trailing "chf", "@", stray punctuation
    desc = re.sub(r'\s*@\s*$', '', desc)
    desc = re.sub(r'\s+chf\s*$', '', desc, flags=re.IGNORECASE)
    odds = float(m.group(3).replace(",", "."))

    if stake <= 0 or odds < 1.01:
        await update.message.reply_text("Mise ou cote invalide.")
        return

    user = update.message.from_user
    now = datetime.now(timezone.utc).isoformat()

    con = db()
    cur = con.execute(
        """INSERT INTO bets (chat_id, message_id, user_id, user_name, description, stake, odds, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (update.message.chat_id, update.message.message_id, user.id, user.first_name,
         desc, stake, odds, now)
    )
    bet_id = cur.lastrowid
    con.commit()
    con.close()

    pp = stake / NB_PARTS
    gain_pp = stake * (odds - 1) / NB_PARTS

    text = (
        f"Pari #{bet_id} enregistre\n"
        f"   {desc} @ {odds:.2f}\n"
        f"   Mise : {stake:.0f} CHF ({pp:.0f}/pers.)\n"
        f"   Gain potentiel : {fmt(gain_pp)}/pers.\n\n"
        f"Resultat â repondre a ce message avec /win ou /loss"
    )
    await update.message.reply_text(text)

    # Sync to Google Sheets
    await sync_sheets({
        "action": "new_bet",
        "id": bet_id,
        "date": now[:10],
        "description": desc,
        "stake": stake,
        "odds": odds,
        "user_name": user.first_name
    })


# ââ /win /loss /void â RÃ©sultat d'un pari âââââââââââââââââââ
async def cmd_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/win or /loss or /void â mark bet result by replying to it."""
    msg = update.message
    command = msg.text.strip().split()[0].lower().lstrip("/")

    # Map command to status
    status_map = {"win": "won", "w": "won", "gagne": "won",
                  "loss": "lost", "lose": "lost", "l": "lost", "perdu": "lost",
                  "void": "void", "push": "void", "annule": "void"}
    status = status_map.get(command)
    if not status:
        return

    # Find the bet â either by reply or by ID argument
    con = db()
    bet = None

    # Method 1: reply to a message containing bet info
    if msg.reply_to_message:
        # Check if replying to original /lock message
        bet = con.execute(
            "SELECT * FROM bets WHERE chat_id = ? AND message_id = ? AND status = 'pending'",
            (msg.chat_id, msg.reply_to_message.message_id)
        ).fetchone()

        # Check if replying to bot confirmation (contains "Pari #X")
        if not bet and msg.reply_to_message.text:
            id_match = re.search(r"Pari #(\d+)", msg.reply_to_message.text)
            if id_match:
                bet = con.execute(
                    "SELECT * FROM bets WHERE id = ? AND chat_id = ? AND status = 'pending'",
                    (int(id_match.group(1)), msg.chat_id)
                ).fetchone()

    # Method 2: /win <id>
    if not bet and ctx.args:
        try:
            bet_id = int(ctx.args[0])
            bet = con.execute(
                "SELECT * FROM bets WHERE id = ? AND chat_id = ? AND status = 'pending'",
                (bet_id, msg.chat_id)
            ).fetchone()
        except (ValueError, IndexError):
            pass

    # Method 3: if only one pending bet, use that
    if not bet:
        pending = con.execute(
            "SELECT * FROM bets WHERE chat_id = ? AND status = 'pending'",
            (msg.chat_id,)
        ).fetchall()
        if len(pending) == 1:
            bet = pending[0]
        elif len(pending) > 1:
            con.close()
            lines = ["Plusieurs paris en attente â precise lequel :\n"]
            for p in pending:
                lines.append(f"  /win {p['id']}  â  {p['description']} @ {p['odds']:.2f}")
            await msg.reply_text("\n".join(lines))
            return

    if not bet:
        con.close()
        await msg.reply_text("Aucun pari en attente trouve. Utilise /pending pour voir la liste.")
        return

    # Update bet
    now = datetime.now(timezone.utc).isoformat()
    con.execute("UPDATE bets SET status = ?, resolved_at = ? WHERE id = ?", (status, now, bet["id"]))
    con.commit()

    stake = bet["stake"]
    odds = bet["odds"]

    if status == "won":
        profit_pp = stake * (odds - 1) / NB_PARTS
        result_text = f"GAGNE  {fmt(profit_pp)}/pers."
    elif status == "lost":
        result_text = f"PERDU  {fmt(-stake / NB_PARTS)}/pers."
    else:
        result_text = "ANNULE  0 CHF"

    total_pnl = get_total_pnl(con, msg.chat_id)
    con.close()

    text = (
        f"Pari #{bet['id']} : {result_text}\n"
        f"   {bet['description']} @ {odds:.2f}\n\n"
        f"P&L cumule : {fmt(total_pnl)}/pers."
    )
    await msg.reply_text(text)

    # Sync to Google Sheets
    await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status})


# ââ /solde ââââââââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_solde(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()

    rows = con.execute(
        "SELECT status, stake, odds FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
        (chat_id,)
    ).fetchall()

    total_pnl = 0.0
    wins = losses = 0
    total_staked = 0.0
    for r in rows:
        total_staked += r["stake"]
        if r["status"] == "won":
            total_pnl += r["stake"] * (r["odds"] - 1) / NB_PARTS
            wins += 1
        else:
            total_pnl -= r["stake"] / NB_PARTS
            losses += 1

    pending = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets WHERE chat_id = ? AND status = 'pending'",
        (chat_id,)
    ).fetchone()
    con.close()

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    roi = (total_pnl / (total_staked / NB_PARTS) * 100) if total_staked > 0 else 0

    text = (
        f"SOLDE DU GROUPE\n\n"
        f"P&L par personne : {fmt(total_pnl)}\n"
        f"Paris : {wins}W - {losses}L ({wr:.0f}%)\n"
        f"ROI : {roi:+.1f}%\n"
        f"Mise totale : {total_staked:.0f} CHF"
    )
    if pending[0] > 0:
        text += f"\n\nEn attente : {pending[0]} paris ({pending[1]:.0f} CHF)"
    await update.message.reply_text(text)


# ââ /historique âââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_historique(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    rows = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? ORDER BY id DESC LIMIT 15",
        (update.message.chat_id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Aucun pari enregistre.")
        return

    icons = {"pending": "â³", "won": "â", "lost": "â", "void": "â©ï¸"}
    lines = ["HISTORIQUE (15 derniers)\n"]
    for r in rows:
        icon = icons.get(r["status"], "?")
        if r["status"] == "won":
            result = fmt(r["stake"] * (r["odds"] - 1) / NB_PARTS)
        elif r["status"] == "lost":
            result = fmt(-r["stake"] / NB_PARTS)
        elif r["status"] == "void":
            result = "0"
        else:
            result = "pending"
        date_str = r["created_at"][:10] if r["created_at"] else "?"
        lines.append(
            f"{icon} #{r['id']} {date_str} | {r['description']} "
            f"@ {r['odds']:.2f} | {r['stake']:.0f} CHF | {result}"
        )
    await update.message.reply_text("\n".join(lines))


# ââ /stats ââââââââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    rows = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? AND status IN ('won','lost') ORDER BY id",
        (update.message.chat_id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Pas encore de paris resolus.")
        return

    wins = losses = 0
    total_pnl = 0.0
    total_staked = 0.0
    best_win = 0.0
    worst_loss = 0.0
    streak = max_streak = 0
    last_status = None

    for r in rows:
        total_staked += r["stake"]
        if r["status"] == "won":
            wins += 1
            p = r["stake"] * (r["odds"] - 1) / NB_PARTS
            total_pnl += p
            best_win = max(best_win, p)
        else:
            losses += 1
            lo = r["stake"] / NB_PARTS
            total_pnl -= lo
            worst_loss = min(worst_loss, -lo)

        if r["status"] == last_status:
            streak += 1
        else:
            streak = 1
            last_status = r["status"]
        max_streak = max(max_streak, streak)

    total = wins + losses
    wr = wins / total * 100
    roi = total_pnl / (total_staked / NB_PARTS) * 100 if total_staked > 0 else 0
    avg_odds = sum(r["odds"] for r in rows) / len(rows)
    avg_stake = total_staked / total

    text = (
        f"STATISTIQUES\n\n"
        f"Paris : {total} ({wins}W - {losses}L)\n"
        f"Win rate : {wr:.1f}%\n"
        f"ROI : {roi:+.1f}%\n\n"
        f"P&L/pers. : {fmt(total_pnl)}\n"
        f"Mise totale : {total_staked:.0f} CHF\n"
        f"Mise moy. : {avg_stake:.0f} CHF\n"
        f"Cote moy. : {avg_odds:.2f}\n\n"
        f"Best : {fmt(best_win)}/pers.\n"
        f"Worst : {fmt(worst_loss)}/pers.\n"
        f"Max serie : {max_streak}"
    )
    await update.message.reply_text(text)


# ââ /dettes âââââââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_dettes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()
    rows = con.execute(
        "SELECT user_id, user_name, stake, odds, status FROM bets "
        "WHERE chat_id = ? AND status IN ('won','lost','pending')",
        (chat_id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Aucun pari enregistre.")
        return

    fronted = {}
    collected = {}
    names = {}
    total_cost = total_returns = 0.0

    for r in rows:
        uid = r["user_id"]
        names[uid] = r["user_name"]
        fronted[uid] = fronted.get(uid, 0) + r["stake"]
        total_cost += r["stake"]
        if r["status"] == "won":
            payout = r["stake"] * r["odds"]
            collected[uid] = collected.get(uid, 0) + payout
            total_returns += payout

    balances = {}
    all_uids = set(fronted) | set(collected)
    for uid in all_uids:
        f = fronted.get(uid, 0)
        c = collected.get(uid, 0)
        physical = c - f
        fair = (total_returns - total_cost) / NB_PARTS
        balances[uid] = fair - physical

    lines = ["DETTES\n"]
    for uid, bal in sorted(balances.items(), key=lambda x: x[1]):
        name = names.get(uid, "?")
        if bal > 0.5:
            lines.append(f"  {name} : on lui doit {fmt_abs(bal)}")
        elif bal < -0.5:
            lines.append(f"  {name} : doit {fmt_abs(bal)} au groupe")
        else:
            lines.append(f"  {name} : a jour")

    # Settlements
    debtors = sorted([(uid, -bal) for uid, bal in balances.items() if bal < -0.5], key=lambda x: -x[1])
    creditors = sorted([(uid, bal) for uid, bal in balances.items() if bal > 0.5], key=lambda x: -x[1])
    if debtors and creditors:
        lines.append("\nReglements :")
        di = ci = 0
        d = list(debtors)
        c = list(creditors)
        while di < len(d) and ci < len(c):
            transfer = min(d[di][1], c[ci][1])
            lines.append(f"  {names[d[di][0]]} â {names[c[ci][0]]} : {transfer:.0f} CHF")
            d[di] = (d[di][0], d[di][1] - transfer)
            c[ci] = (c[ci][0], c[ci][1] - transfer)
            if d[di][1] < 0.5: di += 1
            if c[ci][1] < 0.5: ci += 1

    await update.message.reply_text("\n".join(lines))


# ââ /pending ââââââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    rows = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? AND status = 'pending' ORDER BY id",
        (update.message.chat_id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Aucun pari en attente.")
        return

    lines = ["PARIS EN ATTENTE\n"]
    for r in rows:
        lines.append(
            f"#{r['id']} | {r['description']} @ {r['odds']:.2f} | "
            f"{r['stake']:.0f} CHF ({r['stake']/NB_PARTS:.0f}/pers.)"
        )
    lines.append(f"\nâ /win <id> ou /loss <id> pour marquer le resultat")
    await update.message.reply_text("\n".join(lines))


# ââ /delete âââââââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /delete <id>")
        return
    try:
        bet_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID invalide.")
        return

    con = db()
    bet = con.execute(
        "SELECT * FROM bets WHERE id = ? AND chat_id = ?",
        (bet_id, update.message.chat_id)
    ).fetchone()
    if not bet:
        con.close()
        await update.message.reply_text(f"Pari #{bet_id} introuvable.")
        return
    con.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    con.commit()
    con.close()
    await update.message.reply_text(f"Pari #{bet_id} supprime ({bet['description']}).")

    # Sync to Google Sheets
    await sync_sheets({"action": "delete_bet", "id": bet_id})


# ── /edit — Modifier un pari pending ─────────────────────────
# Format: /edit <id> mise <valeur> | /edit <id> cote <valeur> | /edit <id> desc <texte>

EDIT_PATTERN = re.compile(
    r"(\d+)\s+(mise|cote|desc(?:ription)?)\s+(.+)",
    re.IGNORECASE
)

async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/edit <id> <champ> <valeur> — modifier un pari pending."""
    if not ctx.args:
        await update.message.reply_text(
            "Format : /edit <id> <champ> <valeur>\n"
            "Champs : mise, cote, desc\n"
            "Ex: /edit 3 mise 1000\n"
            "Ex: /edit 3 cote 2.50\n"
            "Ex: /edit 3 desc PSG ML"
        )
        return

    raw = " ".join(ctx.args)
    m = EDIT_PATTERN.search(raw)
    if not m:
        await update.message.reply_text(
            "Format pas reconnu.\n"
            "Ex: /edit 3 mise 1000"
        )
        return

    bet_id = int(m.group(1))
    field = m.group(2).lower()
    value = m.group(3).strip()

    con = db()
    bet = con.execute(
        "SELECT * FROM bets WHERE id = ? AND chat_id = ? AND status = 'pending'",
        (bet_id, update.message.chat_id)
    ).fetchone()

    if not bet:
        con.close()
        await update.message.reply_text(f"Pari #{bet_id} introuvable ou deja resolu.")
        return

    if field == "mise":
        try:
            new_val = float(value.replace(",", "."))
        except ValueError:
            con.close()
            await update.message.reply_text("Mise invalide.")
            return
        if new_val <= 0:
            con.close()
            await update.message.reply_text("Mise invalide.")
            return
        con.execute("UPDATE bets SET stake = ? WHERE id = ?", (new_val, bet_id))
        display = f"Mise → {new_val:.0f} CHF"
    elif field == "cote":
        try:
            new_val = float(value.replace(",", "."))
        except ValueError:
            con.close()
            await update.message.reply_text("Cote invalide.")
            return
        if new_val < 1.01:
            con.close()
            await update.message.reply_text("Cote invalide.")
            return
        con.execute("UPDATE bets SET odds = ? WHERE id = ?", (new_val, bet_id))
        display = f"Cote → {new_val:.2f}"
    else:  # desc / description
        con.execute("UPDATE bets SET description = ? WHERE id = ?", (value, bet_id))
        display = f"Description → {value}"

    con.commit()

    # Reload updated bet for display
    bet = con.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    con.close()

    pp = bet["stake"] / NB_PARTS
    gain_pp = bet["stake"] * (bet["odds"] - 1) / NB_PARTS

    text = (
        f"Pari #{bet_id} modifie\n"
        f"  {display}\n\n"
        f"  {bet['description']} @  {bet['odds']:.2f}\n"
        f"  Mise : {bet['stake']:.0f} CHF ({pp:.0f}/pers.)\n"
        f"  Gain potentiel : {fmt(gain_pp)}/pers."
    )
    await update.message.reply_text(text)

    # Sync to Google Sheets — delete old row + insert updated
    await sync_sheets({"action": "delete_bet", "id": bet_id})
    await sync_sheets({
        "action": "new_bet",
        "id": bet_id,
        "date": bet["created_at"][:10] if bet["created_at"] else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "description": bet["description"],
        "stake": bet["stake"],
        "odds": bet["odds"],
        "user_name": bet["user_name"]
    })


# ââ /help âââââââââââââââââââââââââââââââââââââââââââââââââââ
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "BET TRACKER\n\n"
        "Enregistrer un pari :\n"
        "  /lock 800 Strasbourg 1N2 3,10\n"
        "  /lock 500 Le Mans ML 1.70\n\n"
        "Resultat :\n"
        "  /win  (repondre au pari ou /win <id>)\n"
        "  /loss (repondre au pari ou /loss <id>)\n"
        "  /void (annule/rembourse)\n\n"
        "Modifier :\n"
        "  /edit <id> mise 1000\n"
        "  /edit <id> cote 2.50\n"
        "  /edit <id> desc PSG ML\n\n"
        "Stats :\n"
        "  /solde â P&L du groupe\n"
        "  /dettes â qui doit quoi a qui\n"
        "  /pending â paris en attente\n"
        "  /historique â 15 derniers paris\n"
        "  /stats â stats detaillees\n"
        "  /delete <id> â supprimer un pari"
    )
    await update.message.reply_text(text)


# ââ Fallback : reply gagnÃ©/perdu ââââââââââââââââââââââââââââ
async def on_reply_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Allow marking results by replying 'gagnÃ©' or 'perdu' to the bot message."""
    msg = update.message
    if not msg or not msg.reply_to_message or not msg.text:
        return

    text_lower = msg.text.strip().lower()
    won_words = {"gagnÃ©", "gagne", "win", "won", "w", "gg"}
    lost_words = {"perdu", "perd", "lose", "lost", "l"}
    void_words = {"annulÃ©", "annule", "void", "push", "nul"}

    status = None
    if text_lower in won_words:
        status = "won"
    elif text_lower in lost_words:
        status = "lost"
    elif text_lower in void_words:
        status = "void"

    if not status:
        return

    con = db()
    bet = None

    # Check reply to /lock message
    bet = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? AND message_id = ? AND status = 'pending'",
        (msg.chat_id, msg.reply_to_message.message_id)
    ).fetchone()

    # Check reply to bot confirmation
    if not bet and msg.reply_to_message.text:
        id_match = re.search(r"Pari #(\d+)", msg.reply_to_message.text)
        if id_match:
            bet = con.execute(
                "SELECT * FROM bets WHERE id = ? AND chat_id = ? AND status = 'pending'",
                (int(id_match.group(1)), msg.chat_id)
            ).fetchone()

    if not bet:
        con.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    con.execute("UPDATE bets SET status = ?, resolved_at = ? WHERE id = ?", (status, now, bet["id"]))
    con.commit()

    if status == "won":
        pp_result = fmt(bet["stake"] * (bet["odds"] - 1) / NB_PARTS)
        result_text = f"GAGNE  {pp_result}/pers."
    elif status == "lost":
        result_text = f"PERDU  {fmt(-bet['stake'] / NB_PARTS)}/pers."
    else:
        result_text = "ANNULE"

    total_pnl = get_total_pnl(con, msg.chat_id)
    con.close()

    text = (
        f"Pari #{bet['id']} : {result_text}\n"
        f"   {bet['description']} @ {bet['odds']:.2f}\n\n"
        f"P&L cumule : {fmt(total_pnl)}/pers."
    )
    await msg.reply_text(text)

    # Sync to Google Sheets
    await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status})


# ââ Main ââââââââââââââââââââââââââââââââââââââââââââââââââââ
def main():
    if not BOT_TOKEN:
        print("ERROR: Set BOT_TOKEN environment variable")
        return

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # /lock â register bet
    app.add_handler(CommandHandler("lock", cmd_lock))

    # /win /loss /void â mark result
    for cmd in ["win", "w", "gagne", "loss", "lose", "l", "perdu", "void", "push", "annule"]:
        app.add_handler(CommandHandler(cmd, cmd_result))

    # Other commands
    app.add_handler(CommandHandler("solde", cmd_solde))
    app.add_handler(CommandHandler("historique", cmd_historique))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("dettes", cmd_dettes))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # Fallback: reply with "gagnÃ©"/"perdu" (no command prefix)
    app.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND,
        on_reply_result
    ))

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
