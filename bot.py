"""
Telegram Bet Tracker Bot
Deux modes :
  - Groupe (Bets Suisse) : divise par 3, CHF, bankroll commune
  - Duo (Alex-Rapha) : Tricount style, EUR, qui doit quoi à qui

Usage principal :
  /lock 800 Strasbourg 1N2 3,10
  → Enregistre un pari
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

# ── Config ──────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SHEETS_WEBHOOK_URL = os.environ.get("SHEETS_WEBHOOK_URL", "")
NB_PARTS = 3
DB_PATH = "bets.db"
DUO_CHAT_ID = int(os.environ.get("DUO_CHAT_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Mode helpers ─────────────────────────────────────────────
def is_duo(chat_id: int) -> bool:
    return DUO_CHAT_ID != 0 and chat_id == DUO_CHAT_ID

def cur(chat_id: int) -> str:
    return "EUR" if is_duo(chat_id) else "CHF"

def fmt(amount: float, chat_id: int = 0) -> str:
    c = cur(chat_id)
    return f"+{amount:.0f} {c}" if amount >= 0 else f"{amount:.0f} {c}"

def fmt_abs(amount: float, chat_id: int = 0) -> str:
    return f"{abs(amount):.0f} {cur(chat_id)}"

def bet_pnl(stake: float, odds: float, status: str) -> float:
    if status == "won":
        return stake * (odds - 1)
    elif status == "lost":
        return -stake
    return 0.0

def get_duo_balance(con, chat_id: int) -> tuple:
    """Compute Tricount balance. Returns (pnl_by_user, total_pnl, wins, losses, total_staked).
    Balance = pnl_B - pnl_A → if positive, B owes A."""
    rows = con.execute(
        "SELECT user_name, stake, odds, status FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
        (chat_id,)
    ).fetchall()
    pnl_by_user = {}
    wins_by_user = {}
    losses_by_user = {}
    total_staked = 0.0
    for r in rows:
        pnl = bet_pnl(r["stake"], r["odds"], r["status"])
        user = r["user_name"]
        pnl_by_user[user] = pnl_by_user.get(user, 0) + pnl
        total_staked += r["stake"]
        if r["status"] == "won":
            wins_by_user[user] = wins_by_user.get(user, 0) + 1
        else:
            losses_by_user[user] = losses_by_user.get(user, 0) + 1
    total_pnl = sum(pnl_by_user.values())
    total_wins = sum(wins_by_user.values())
    total_losses = sum(losses_by_user.values())
    return pnl_by_user, total_pnl, total_wins, total_losses, total_staked, wins_by_user, losses_by_user

def format_duo_debt(pnl_by_user: dict, chat_id: int) -> str:
    """Format who owes whom in duo mode."""
    users = sorted(pnl_by_user.keys())
    if len(users) < 2:
        if len(users) == 1:
            return f"Un seul joueur ({users[0]}) — pas encore de balance"
        return "Aucun pari resolu"
    a, b = users[0], users[1]
    # balance = pnl_b - pnl_a → positive means b owes a
    balance = pnl_by_user[b] - pnl_by_user[a]
    if balance > 0.5:
        return f"{b} doit {balance:.0f} {cur(chat_id)} a {a}"
    elif balance < -0.5:
        return f"{a} doit {abs(balance):.0f} {cur(chat_id)} a {b}"
    return "Vous etes a jour !"

# ── Database ────────────────────────────────────────────────
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

# ── Google Sheets sync ─────────────────────────────────────
async def sync_sheets(payload: dict):
    if not SHEETS_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEETS_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                log.info(f"Sheets sync: {payload.get('action')} → {resp.status}")
    except Exception as e:
        log.warning(f"Sheets sync failed: {e}")

# ── /lock — Enregistrer un pari ─────────────────────────────
LOCK_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?)"      # groupe 1 : mise
    r"\s*(?:chf|eur|€)?\s+"   # optionnel devise
    r"(.+?)\s+"                # groupe 2 : description
    r"(?:@\s*)?"               # optionnel "@"
    r"(\d+[.,]\d+)",           # groupe 3 : cote
    re.IGNORECASE
)

async def cmd_lock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        c = cur(update.message.chat_id)
        await update.message.reply_text(
            f"Format : /lock <mise> <description> <cote>\n"
            f"Ex: /lock 800 Strasbourg 1N2 3,10"
        )
        return

    raw = " ".join(ctx.args)
    m = LOCK_PATTERN.search(raw)
    if not m:
        await update.message.reply_text("Format pas reconnu.\nEx: /lock 800 Strasbourg 1N2 3,10")
        return

    stake = float(m.group(1).replace(",", "."))
    desc = m.group(2).strip()
    desc = re.sub(r'\s*@\s*$', '', desc)
    desc = re.sub(r'\s+(?:chf|eur|€)\s*$', '', desc, flags=re.IGNORECASE)
    odds = float(m.group(3).replace(",", "."))

    if stake <= 0 or odds < 1.01:
        await update.message.reply_text("Mise ou cote invalide.")
        return

    user = update.message.from_user
    chat_id = update.message.chat_id
    now = datetime.now(timezone.utc).isoformat()

    con = db()
    cur_ = con.execute(
        """INSERT INTO bets (chat_id, message_id, user_id, user_name, description, stake, odds, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (chat_id, update.message.message_id, user.id, user.first_name, desc, stake, odds, now)
    )
    bet_id = cur_.lastrowid
    con.commit()
    con.close()

    c = cur(chat_id)
    if is_duo(chat_id):
        gain = stake * (odds - 1)
        text = (
            f"Pari #{bet_id} enregistre\n"
            f"   {desc} @ {odds:.2f}\n"
            f"   Mise : {stake:.0f} {c} (par {user.first_name})\n"
            f"   Gain potentiel : {fmt(gain, chat_id)}\n\n"
            f"Resultat → repondre avec /win ou /loss"
        )
    else:
        pp = stake / NB_PARTS
        gain_pp = stake * (odds - 1) / NB_PARTS
        text = (
            f"Pari #{bet_id} enregistre\n"
            f"   {desc} @ {odds:.2f}\n"
            f"   Mise : {stake:.0f} {c} ({pp:.0f}/pers.)\n"
            f"   Gain potentiel : {fmt(gain_pp, chat_id)}/pers.\n\n"
            f"Resultat → repondre a ce message avec /win ou /loss"
        )
    await update.message.reply_text(text)

    sheet_tab = "Alex-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({
        "action": "new_bet",
        "id": bet_id,
        "date": now[:10],
        "description": desc,
        "stake": stake,
        "odds": odds,
        "user_name": user.first_name,
        "sheet_tab": sheet_tab
    })


# ── /win /loss /void — Résultat d'un pari ───────────────────
async def cmd_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    command = msg.text.strip().split()[0].lower().lstrip("/")

    status_map = {"win": "won", "w": "won", "gagne": "won",
                  "loss": "lost", "lose": "lost", "l": "lost", "perdu": "lost",
                  "void": "void", "push": "void", "annule": "void"}
    status = status_map.get(command)
    if not status:
        return

    con = db()
    bet = None

    if msg.reply_to_message:
        bet = con.execute(
            "SELECT * FROM bets WHERE chat_id = ? AND message_id = ? AND status = 'pending'",
            (chat_id, msg.reply_to_message.message_id)
        ).fetchone()
        if not bet and msg.reply_to_message.text:
            id_match = re.search(r"Pari #(\d+)", msg.reply_to_message.text)
            if id_match:
                bet = con.execute(
                    "SELECT * FROM bets WHERE id = ? AND chat_id = ? AND status = 'pending'",
                    (int(id_match.group(1)), chat_id)
                ).fetchone()

    if not bet and ctx.args:
        try:
            bet_id = int(ctx.args[0])
            bet = con.execute(
                "SELECT * FROM bets WHERE id = ? AND chat_id = ? AND status = 'pending'",
                (bet_id, chat_id)
            ).fetchone()
        except (ValueError, IndexError):
            pass

    if not bet:
        pending = con.execute(
            "SELECT * FROM bets WHERE chat_id = ? AND status = 'pending'",
            (chat_id,)
        ).fetchall()
        if len(pending) == 1:
            bet = pending[0]
        elif len(pending) > 1:
            con.close()
            lines = ["Plusieurs paris en attente — precise lequel :\n"]
            for p in pending:
                lines.append(f"  /win {p['id']}  →  {p['description']} @ {p['odds']:.2f}")
            await msg.reply_text("\n".join(lines))
            return

    if not bet:
        con.close()
        await msg.reply_text("Aucun pari en attente trouve. Utilise /pending pour voir la liste.")
        return

    now = datetime.now(timezone.utc).isoformat()
    con.execute("UPDATE bets SET status = ?, resolved_at = ? WHERE id = ?", (status, now, bet["id"]))
    con.commit()

    stake = bet["stake"]
    odds = bet["odds"]
    pnl = bet_pnl(stake, odds, status)

    if is_duo(chat_id):
        if status == "won":
            result_text = f"GAGNE  {fmt(pnl, chat_id)}"
        elif status == "lost":
            result_text = f"PERDU  {fmt(pnl, chat_id)}"
        else:
            result_text = f"ANNULE  0 {cur(chat_id)}"

        duo_data = get_duo_balance(con, chat_id)
        debt_text = format_duo_debt(duo_data[0], chat_id)
        con.close()

        text = (
            f"Pari #{bet['id']} : {result_text}\n"
            f"   {bet['description']} @ {odds:.2f}\n"
            f"   (par {bet['user_name']})\n\n"
            f"Balance : {debt_text}"
        )
    else:
        if status == "won":
            profit_pp = pnl / NB_PARTS
            result_text = f"GAGNE  {fmt(profit_pp, chat_id)}/pers."
        elif status == "lost":
            result_text = f"PERDU  {fmt(pnl / NB_PARTS, chat_id)}/pers."
        else:
            result_text = f"ANNULE  0 {cur(chat_id)}"

        rows = con.execute(
            "SELECT status, stake, odds FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
            (chat_id,)
        ).fetchall()
        total_pnl = sum(bet_pnl(r["stake"], r["odds"], r["status"]) / NB_PARTS for r in rows)
        con.close()

        text = (
            f"Pari #{bet['id']} : {result_text}\n"
            f"   {bet['description']} @ {odds:.2f}\n\n"
            f"P&L cumule : {fmt(total_pnl, chat_id)}/pers."
        )

    await msg.reply_text(text)

    sheet_tab = "Alex-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status, "sheet_tab": sheet_tab})


# ── /solde ──────────────────────────────────────────────────
async def cmd_solde(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()

    pending = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets WHERE chat_id = ? AND status = 'pending'",
        (chat_id,)
    ).fetchone()

    if is_duo(chat_id):
        pnl_by_user, total_pnl, wins, losses, total_staked, w_by_user, l_by_user = get_duo_balance(con, chat_id)
        con.close()

        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0
        roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0
        debt_text = format_duo_debt(pnl_by_user, chat_id)

        lines = [f"SOLDE DUO\n\nBalance : {debt_text}\n"]
        for user in sorted(pnl_by_user.keys()):
            w = w_by_user.get(user, 0)
            lo = l_by_user.get(user, 0)
            lines.append(f"Paris par {user} : {w}W-{lo}L, P&L {fmt(pnl_by_user[user], chat_id)}")

        lines.append(f"\nTotal : {wins}W - {losses}L ({wr:.0f}%)")
        lines.append(f"ROI : {roi:+.1f}%")
        lines.append(f"Mise totale : {total_staked:.0f} {cur(chat_id)}")
        if pending[0] > 0:
            lines.append(f"\nEn attente : {pending[0]} paris ({pending[1]:.0f} {cur(chat_id)})")
        await update.message.reply_text("\n".join(lines))
    else:
        rows = con.execute(
            "SELECT status, stake, odds FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
            (chat_id,)
        ).fetchall()
        con.close()

        total_pnl = 0.0
        wins = losses = 0
        total_staked = 0.0
        for r in rows:
            total_staked += r["stake"]
            pnl = bet_pnl(r["stake"], r["odds"], r["status"])
            total_pnl += pnl / NB_PARTS
            if r["status"] == "won":
                wins += 1
            else:
                losses += 1

        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0
        roi = (total_pnl / (total_staked / NB_PARTS) * 100) if total_staked > 0 else 0

        text = (
            f"SOLDE DU GROUPE\n\n"
            f"P&L par personne : {fmt(total_pnl, chat_id)}\n"
            f"Paris : {wins}W - {losses}L ({wr:.0f}%)\n"
            f"ROI : {roi:+.1f}%\n"
            f"Mise totale : {total_staked:.0f} {cur(chat_id)}"
        )
        if pending[0] > 0:
            text += f"\n\nEn attente : {pending[0]} paris ({pending[1]:.0f} {cur(chat_id)})"
        await update.message.reply_text(text)


# ── /historique ─────────────────────────────────────────────
async def cmd_historique(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()
    rows = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? ORDER BY id DESC LIMIT 15",
        (chat_id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Aucun pari enregistre.")
        return

    icons = {"pending": "⏳", "won": "✅", "lost": "❌", "void": "↩️"}
    duo = is_duo(chat_id)
    c = cur(chat_id)
    lines = ["HISTORIQUE (15 derniers)\n"]
    for r in rows:
        icon = icons.get(r["status"], "?")
        pnl = bet_pnl(r["stake"], r["odds"], r["status"])
        if r["status"] == "won":
            result = fmt(pnl / NB_PARTS, chat_id) if not duo else fmt(pnl, chat_id)
        elif r["status"] == "lost":
            result = fmt(pnl / NB_PARTS, chat_id) if not duo else fmt(pnl, chat_id)
        elif r["status"] == "void":
            result = "0"
        else:
            result = "pending"
        date_str = r["created_at"][:10] if r["created_at"] else "?"
        par = f" [{r['user_name']}]" if duo else ""
        lines.append(
            f"{icon} #{r['id']} {date_str}{par} | {r['description']} "
            f"@ {r['odds']:.2f} | {r['stake']:.0f} {c} | {result}"
        )
    await update.message.reply_text("\n".join(lines))


# ── /stats ──────────────────────────────────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()
    rows = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? AND status IN ('won','lost') ORDER BY id",
        (chat_id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Pas encore de paris resolus.")
        return

    duo = is_duo(chat_id)
    divisor = 1 if duo else NB_PARTS
    wins = losses = 0
    total_pnl = 0.0
    total_staked = 0.0
    best_win = 0.0
    worst_loss = 0.0
    streak = max_streak = 0
    last_status = None

    for r in rows:
        total_staked += r["stake"]
        pnl = bet_pnl(r["stake"], r["odds"], r["status"]) / divisor
        total_pnl += pnl
        if r["status"] == "won":
            wins += 1
            best_win = max(best_win, pnl)
        else:
            losses += 1
            worst_loss = min(worst_loss, pnl)
        if r["status"] == last_status:
            streak += 1
        else:
            streak = 1
            last_status = r["status"]
        max_streak = max(max_streak, streak)

    total = wins + losses
    wr = wins / total * 100
    roi = total_pnl / (total_staked / divisor) * 100 if total_staked > 0 else 0
    avg_odds = sum(r["odds"] for r in rows) / len(rows)
    avg_stake = total_staked / total
    c = cur(chat_id)
    suffix = "" if duo else "/pers."

    text = (
        f"STATISTIQUES\n\n"
        f"Paris : {total} ({wins}W - {losses}L)\n"
        f"Win rate : {wr:.1f}%\n"
        f"ROI : {roi:+.1f}%\n\n"
        f"P&L{suffix} : {fmt(total_pnl, chat_id)}\n"
        f"Mise totale : {total_staked:.0f} {c}\n"
        f"Mise moy. : {avg_stake:.0f} {c}\n"
        f"Cote moy. : {avg_odds:.2f}\n\n"
        f"Best : {fmt(best_win, chat_id)}{suffix}\n"
        f"Worst : {fmt(worst_loss, chat_id)}{suffix}\n"
        f"Max serie : {max_streak}"
    )
    await update.message.reply_text(text)


# ── /dettes ─────────────────────────────────────────────────
async def cmd_dettes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()

    if is_duo(chat_id):
        pnl_by_user, total_pnl, wins, losses, total_staked, w_by_user, l_by_user = get_duo_balance(con, chat_id)
        con.close()
        debt_text = format_duo_debt(pnl_by_user, chat_id)
        lines = [f"BALANCE\n\n{debt_text}\n"]
        for user in sorted(pnl_by_user.keys()):
            w = w_by_user.get(user, 0)
            lo = l_by_user.get(user, 0)
            lines.append(f"  {user} : {w}W-{lo}L, P&L {fmt(pnl_by_user[user], chat_id)}")
        await update.message.reply_text("\n".join(lines))
        return

    # ── Split mode (original) ──
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
        c_ = collected.get(uid, 0)
        physical = c_ - f
        fair = (total_returns - total_cost) / NB_PARTS
        balances[uid] = fair - physical

    c = cur(chat_id)
    lines = ["DETTES\n"]
    for uid, bal in sorted(balances.items(), key=lambda x: x[1]):
        name = names.get(uid, "?")
        if bal > 0.5:
            lines.append(f"  {name} : on lui doit {abs(bal):.0f} {c}")
        elif bal < -0.5:
            lines.append(f"  {name} : doit {abs(bal):.0f} {c} au groupe")
        else:
            lines.append(f"  {name} : a jour")

    debtors = sorted([(uid, -bal) for uid, bal in balances.items() if bal < -0.5], key=lambda x: -x[1])
    creditors = sorted([(uid, bal) for uid, bal in balances.items() if bal > 0.5], key=lambda x: -x[1])
    if debtors and creditors:
        lines.append("\nReglements :")
        di = ci = 0
        d = list(debtors)
        cr = list(creditors)
        while di < len(d) and ci < len(cr):
            transfer = min(d[di][1], cr[ci][1])
            lines.append(f"  {names[d[di][0]]} → {names[cr[ci][0]]} : {transfer:.0f} {c}")
            d[di] = (d[di][0], d[di][1] - transfer)
            cr[ci] = (cr[ci][0], cr[ci][1] - transfer)
            if d[di][1] < 0.5: di += 1
            if cr[ci][1] < 0.5: ci += 1

    await update.message.reply_text("\n".join(lines))


# ── /pending ────────────────────────────────────────────────
async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()
    rows = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? AND status = 'pending' ORDER BY id",
        (chat_id,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Aucun pari en attente.")
        return

    duo = is_duo(chat_id)
    c = cur(chat_id)
    lines = ["PARIS EN ATTENTE\n"]
    for r in rows:
        par = f" [{r['user_name']}]" if duo else ""
        pp = "" if duo else f" ({r['stake']/NB_PARTS:.0f}/pers.)"
        lines.append(
            f"#{r['id']}{par} | {r['description']} @ {r['odds']:.2f} | "
            f"{r['stake']:.0f} {c}{pp}"
        )
    lines.append(f"\n→ /win <id> ou /loss <id> pour marquer le resultat")
    await update.message.reply_text("\n".join(lines))


# ── /delete ─────────────────────────────────────────────────
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
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
        (bet_id, chat_id)
    ).fetchone()
    if not bet:
        con.close()
        await update.message.reply_text(f"Pari #{bet_id} introuvable.")
        return
    con.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    con.commit()
    con.close()
    await update.message.reply_text(f"Pari #{bet_id} supprime ({bet['description']}).")

    sheet_tab = "Alex-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({"action": "delete_bet", "id": bet_id, "sheet_tab": sheet_tab})


# ── /help ───────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    duo = is_duo(chat_id)
    c = cur(chat_id)

    if duo:
        text = (
            "BET TRACKER (mode duo)\n\n"
            "Enregistrer un pari :\n"
            f"  /lock 800 Strasbourg 1N2 3,10\n"
            f"  → enregistre 800 {c} par toi pour l'autre\n\n"
            "Resultat :\n"
            "  /win  (repondre au pari ou /win <id>)\n"
            "  /loss (repondre au pari ou /loss <id>)\n\n"
            "Stats :\n"
            "  /solde — balance entre vous deux\n"
            "  /dettes — qui doit quoi\n"
            "  /pending — paris en attente\n"
            "  /historique — 15 derniers paris\n"
            "  /stats — stats detaillees\n"
            "  /delete <id> — supprimer un pari"
        )
    else:
        text = (
            "BET TRACKER\n\n"
            "Enregistrer un pari :\n"
            "  /lock 800 Strasbourg 1N2 3,10\n"
            "  /lock 500 Le Mans ML 1.70\n\n"
            "Resultat :\n"
            "  /win  (repondre au pari ou /win <id>)\n"
            "  /loss (repondre au pari ou /loss <id>)\n"
            "  /void (annule/rembourse)\n\n"
            "Stats :\n"
            "  /solde — P&L du groupe\n"
            "  /dettes — qui doit quoi a qui\n"
            "  /pending — paris en attente\n"
            "  /historique — 15 derniers paris\n"
            "  /stats — stats detaillees\n"
            "  /delete <id> — supprimer un pari"
        )
    await update.message.reply_text(text)


# ── Fallback : reply gagné/perdu ────────────────────────────
async def on_reply_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.reply_to_message or not msg.text:
        return

    chat_id = msg.chat_id
    text_lower = msg.text.strip().lower()
    won_words = {"gagné", "gagne", "win", "won", "w", "gg"}
    lost_words = {"perdu", "perd", "lose", "lost", "l"}
    void_words = {"annulé", "annule", "void", "push", "nul"}

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
    bet = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? AND message_id = ? AND status = 'pending'",
        (chat_id, msg.reply_to_message.message_id)
    ).fetchone()

    if not bet and msg.reply_to_message.text:
        id_match = re.search(r"Pari #(\d+)", msg.reply_to_message.text)
        if id_match:
            bet = con.execute(
                "SELECT * FROM bets WHERE id = ? AND chat_id = ? AND status = 'pending'",
                (int(id_match.group(1)), chat_id)
            ).fetchone()

    if not bet:
        con.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    con.execute("UPDATE bets SET status = ?, resolved_at = ? WHERE id = ?", (status, now, bet["id"]))
    con.commit()

    pnl = bet_pnl(bet["stake"], bet["odds"], status)

    if is_duo(chat_id):
        if status == "won":
            result_text = f"GAGNE  {fmt(pnl, chat_id)}"
        elif status == "lost":
            result_text = f"PERDU  {fmt(pnl, chat_id)}"
        else:
            result_text = "ANNULE"

        duo_data = get_duo_balance(con, chat_id)
        debt_text = format_duo_debt(duo_data[0], chat_id)
        con.close()

        reply = (
            f"Pari #{bet['id']} : {result_text}\n"
            f"   {bet['description']} @ {bet['odds']:.2f}\n"
            f"   (par {bet['user_name']})\n\n"
            f"Balance : {debt_text}"
        )
    else:
        if status == "won":
            pp_result = fmt(pnl / NB_PARTS, chat_id)
            result_text = f"GAGNE  {pp_result}/pers."
        elif status == "lost":
            result_text = f"PERDU  {fmt(pnl / NB_PARTS, chat_id)}/pers."
        else:
            result_text = "ANNULE"

        rows = con.execute(
            "SELECT status, stake, odds FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
            (chat_id,)
        ).fetchall()
        total_pnl = sum(bet_pnl(r["stake"], r["odds"], r["status"]) / NB_PARTS for r in rows)
        con.close()

        reply = (
            f"Pari #{bet['id']} : {result_text}\n"
            f"   {bet['description']} @ {bet['odds']:.2f}\n\n"
            f"P&L cumule : {fmt(total_pnl, chat_id)}/pers."
        )

    await msg.reply_text(reply)

    sheet_tab = "Alex-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status, "sheet_tab": sheet_tab})


# ── Main ────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("ERROR: Set BOT_TOKEN environment variable")
        return

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("lock", cmd_lock))
    for cmd in ["win", "w", "gagne", "loss", "lose", "l", "perdu", "void", "push", "annule"]:
        app.add_handler(CommandHandler(cmd, cmd_result))

    app.add_handler(CommandHandler("solde", cmd_solde))
    app.add_handler(CommandHandler("historique", cmd_historique))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("dettes", cmd_dettes))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    app.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND,
        on_reply_result
    ))

    log.info(f"Bot started (DUO_CHAT_ID={DUO_CHAT_ID})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
