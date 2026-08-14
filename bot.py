"""
Telegram Bet Tracker Bot
Deux modes :
  - Groupe (Bets Suisse) : divise par 3, CHF, bankroll commune
  - Duo (Kekko-Rapha) : Tricount style, EUR, qui doit quoi à qui

Usage principal :
  /lock 800 Strasbourg 1N2 3,10
  → Enregistre un par
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
DB_PATH = os.environ.get("DB_PATH", "/data/bets.db")
DUO_CHAT_ID = int(os.environ.get("DUO_CHAT_ID", "0"))
SHEET_ID_GROUP = "1izpo65I_FgrTUaarqiGCJHv7VQ2A-ixMOcnb7PJrU7k"
SHEET_ID_DUO   = "1oLodmWlhKfoSdcmgWeR42bcrCh_7YJUBJ9jMMps5EgU"
GROUP_DEFAULT_BETTOR = "Marco"
NAME_MAP = {"Twix": "Kekko"}

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

def get_duo_tricount(con, chat_id: int) -> dict:
    """Tricount balance for duo mode.
    Includes pending bets (avance de mise), resolved bets, shared expenses, direct transfers.
    Returns dict with balance (positive = b owes a, alphabetical order) or None."""
    bets = con.execute(
        "SELECT user_name, stake, odds, status FROM bets WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    txs = con.execute(
        "SELECT from_name, to_name, amount FROM transactions WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    expenses = con.execute(
        "SELECT paid_by, amount FROM expenses WHERE chat_id = ?", (chat_id,)
    ).fetchall()

    users = set()
    contribution = {}   # net amount each user contributed for the duo
    pending_stakes = {}
    pending_count = {}
    pnl = {}
    wins = {}
    losses = {}

    for b in bets:
        u = b["user_name"].strip().capitalize()
        users.add(u)
        S, O, st = b["stake"], b["odds"], b["status"]
        if st == "void":
            continue
        if st == "pending":
            contribution[u] = contribution.get(u, 0) + S
            pending_stakes[u] = pending_stakes.get(u, 0) + S
            pending_count[u] = pending_count.get(u, 0) + 1
        elif st == "won":
            contribution[u] = contribution.get(u, 0) + S - S * O  # paid S, collected S*O
            pnl[u] = pnl.get(u, 0) + S * (O - 1)
            wins[u] = wins.get(u, 0) + 1
        elif st == "lost":
            contribution[u] = contribution.get(u, 0) + S
            pnl[u] = pnl.get(u, 0) - S
            losses[u] = losses.get(u, 0) + 1

    expense_total = {}
    for e in expenses:
        u = e["paid_by"].strip().capitalize()
        users.add(u)
        contribution[u] = contribution.get(u, 0) + e["amount"]
        expense_total[u] = expense_total.get(u, 0) + e["amount"]

    for t in txs:
        users.add(t["from_name"])
        users.add(t["to_name"])

    users = sorted(users)
    if len(users) < 2:
        return None

    a, b = users[0], users[1]

    # Contribution balance: direct debt (no splitting for duo mode)
    contrib_bal = contribution.get(a, 0) - contribution.get(b, 0)

    # Direct transfers offset the debt
    net_b_to_a = 0.0
    for t in txs:
        if t["from_name"] == b and t["to_name"] == a:
            net_b_to_a += t["amount"]
        elif t["from_name"] == a and t["to_name"] == b:
            net_b_to_a -= t["amount"]

    balance = contrib_bal - net_b_to_a

    return {
        "balance": balance, "a": a, "b": b,
        "contribution": contribution, "pending_stakes": pending_stakes,
        "pending_count": pending_count, "pnl": pnl, "wins": wins, "losses": losses,
        "expense_total": expense_total, "net_b_to_a": net_b_to_a,
    }


def format_tricount_balance(tc: dict, chat_id: int) -> str:
    if not tc:
        return "Pas encore de donnees"
    bal = tc["balance"]
    a, b, c = tc["a"], tc["b"], cur(chat_id)
    if bal > 0.5:
        return f"{b} doit {bal:.0f} {c} a {a}"
    elif bal < -0.5:
        return f"{a} doit {abs(bal):.0f} {c} a {b}"
    return "Vous etes a jour !"


def get_transactions_net(con, chat_id: int) -> dict:
    """Returns {name: net_amount_sent}. Positive = has sent more than received."""
    rows = con.execute(
        "SELECT from_name, to_name, amount FROM transactions WHERE chat_id = ?",
        (chat_id,)
    ).fetchall()
    net = {}
    for r in rows:
        net[r["from_name"]] = net.get(r["from_name"], 0) + r["amount"]
        net[r["to_name"]] = net.get(r["to_name"], 0) - r["amount"]
    return net


def get_transactions_list(con, chat_id: int) -> list:
    return con.execute(
        "SELECT * FROM transactions WHERE chat_id = ? ORDER BY id DESC LIMIT 10",
        (chat_id,)
    ).fetchall()

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
    con.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            from_name   TEXT,
            to_name     TEXT,
            amount      REAL,
            description TEXT,
            created_at  TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            paid_by     TEXT,
            amount      REAL,
            description TEXT,
            created_at  TEXT
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
    # Route to correct spreadsheet based on sheet_tab
    tab = payload.get("sheet_tab", "Paris")
    payload["sheet_id"] = SHEET_ID_DUO if tab == "Kekko-Rapha" else SHEET_ID_GROUP
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
    r"(.+)\s+"                 # groupe 2 : description (greedy → last number = cote)
    r"(?:@\s*)?"               # optionnel "@"
    r"(\d+[.,]\d+)",           # groupe 3 : cote
    re.IGNORECASE
)

async def cmd_lock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        c = cur(update.message.chat_id)
        await update.message.reply_text(
            f"Format : /lock <mise> <description> <cote> [@nom]\n"
            f"Ex: /lock 800 Strasbourg 1N2 3,10\n"
            f"Ex: /lock 500 PSG ML 2,10 @Rapha"
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

    # Optional @name override after odds
    remainder = raw[m.end():].strip()
    override = re.match(r'@\s*(\S+)', remainder)
    if override:
        bettor_name = override.group(1).strip().capitalize()
    elif not is_duo(chat_id):
        bettor_name = GROUP_DEFAULT_BETTOR
    else:
        raw = user.first_name
        bettor_name = NAME_MAP.get(raw, raw)
    now = datetime.now(timezone.utc).isoformat()

    con = db()
    cur_ = con.execute(
        """INSERT INTO bets (chat_id, message_id, user_id, user_name, description, stake, odds, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (chat_id, update.message.message_id, user.id, bettor_name, desc, stake, odds, now)
    )
    bet_id = cur_.lastrowid
    con.commit()
    con.close()

    c = cur(chat_id)
    if is_duo(chat_id):
        gain = stake * (odds - 1)
        con2 = db()
        tc = get_duo_tricount(con2, chat_id)
        balance_text = format_tricount_balance(tc, chat_id)
        con2.close()
        text = (
            f"Pari #{bet_id} enregistre\n"
            f"   {desc} @ {odds:.2f}\n"
            f"   Mise : {stake:.0f} {c} (par {bettor_name})\n"
            f"   Gain potentiel : {fmt(gain, chat_id)}\n\n"
            f"Balance : {balance_text}\n"
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

    sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({
        "action": "new_bet",
        "id": bet_id,
        "date": now[:10],
        "description": desc,
        "stake": stake,
        "odds": odds,
        "user_name": bettor_name,
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

        tc = get_duo_tricount(con, chat_id)
        debt_text = format_tricount_balance(tc, chat_id)
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

    sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
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
        tc = get_duo_tricount(con, chat_id)
        con.close()

        if not tc:
            await update.message.reply_text("Aucune donnee.")
            return

        balance_text = format_tricount_balance(tc, chat_id)
        c = cur(chat_id)
        a, b = tc["a"], tc["b"]
        total_wins = sum(tc["wins"].values())
        total_losses = sum(tc["losses"].values())
        total_pnl = sum(tc["pnl"].values())
        total_staked = sum(s for u, s in tc["pnl"].items() for _ in [0])  # recalc below
        # recompute total staked from resolved bets
        total_resolved = total_wins + total_losses
        wr = (total_wins / total_resolved * 100) if total_resolved > 0 else 0

        lines = [f"SOLDE DUO\n\nBalance : {balance_text}\n"]
        for u in [a, b]:
            w = tc["wins"].get(u, 0)
            lo = tc["losses"].get(u, 0)
            p = tc["pending_count"].get(u, 0)
            pnl_val = tc["pnl"].get(u, 0)
            parts = [f"{w}W-{lo}L, P&L {fmt(pnl_val, chat_id)}"]
            if p > 0:
                parts.append(f"{p} pending ({tc['pending_stakes'][u]:.0f} {c})")
            lines.append(f"  {u} : {', '.join(parts)}")

        lines.append(f"\nTotal : {total_wins}W - {total_losses}L ({wr:.0f}%)")
        lines.append(f"P&L : {fmt(total_pnl, chat_id)}")
        if pending[0] > 0:
            lines.append(f"En attente : {pending[0]} paris ({pending[1]:.0f} {c})")
        if tc["expense_total"]:
            lines.append(f"\nDepenses : {sum(tc['expense_total'].values()):.0f} {c}")
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
        tc = get_duo_tricount(con, chat_id)
        tx_list = get_transactions_list(con, chat_id)
        exp_list = con.execute(
            "SELECT * FROM expenses WHERE chat_id = ? ORDER BY id DESC LIMIT 5", (chat_id,)
        ).fetchall()
        con.close()

        if not tc:
            await update.message.reply_text("Aucune donnee.")
            return

        balance_text = format_tricount_balance(tc, chat_id)
        c = cur(chat_id)
        a, b = tc["a"], tc["b"]

        lines = [f"TRICOUNT\n\n{balance_text}\n"]

        lines.append("--- Paris ---")
        for u in [a, b]:
            w = tc["wins"].get(u, 0)
            lo = tc["losses"].get(u, 0)
            p = tc["pending_count"].get(u, 0)
            pnl_val = tc["pnl"].get(u, 0)
            pend_val = tc["pending_stakes"].get(u, 0)
            parts = []
            if w + lo > 0:
                parts.append(f"{w}W-{lo}L P&L {fmt(pnl_val, chat_id)}")
            if p > 0:
                parts.append(f"{p} pending ({pend_val:.0f} {c})")
            if parts:
                lines.append(f"  {u} : {', '.join(parts)}")

        if tc["expense_total"]:
            lines.append("\n--- Depenses ---")
            for u in [a, b]:
                if u in tc["expense_total"]:
                    lines.append(f"  {u} a paye : {tc['expense_total'][u]:.0f} {c}")

        if exp_list:
            for e in exp_list[:3]:
                lines.append(f"    #{e['id']} {e['paid_by']} {e['amount']:.0f} {c} ({e['description']})")

        if tx_list:
            lines.append("\n--- Transferts ---")
            for tx in tx_list[:5]:
                lines.append(f"  {tx['from_name']}→{tx['to_name']} {tx['amount']:.0f} {c} ({tx['description']})")

        await update.message.reply_text("\n".join(lines))
        return

    # ── Split mode (original) ──
    rows = con.execute(
        "SELECT user_name, stake, odds, status FROM bets "
        "WHERE chat_id = ? AND status IN ('won','lost','pending')",
        (chat_id,)
    ).fetchall()
    tx_net = get_transactions_net(con, chat_id)
    con.close()

    if not rows and not tx_net:
        await update.message.reply_text("Aucun pari enregistre.")
        return

    fronted = {}
    collected = {}
    total_cost = total_returns = 0.0

    for r in rows:
        name = r["user_name"]
        fronted[name] = fronted.get(name, 0) + r["stake"]
        total_cost += r["stake"]
        if r["status"] == "won":
            payout = r["stake"] * r["odds"]
            collected[name] = collected.get(name, 0) + payout
            total_returns += payout

    # Include participants from both bets and transactions
    all_names = set(fronted) | set(collected) | set(tx_net.keys())
    balances = {}
    for name in all_names:
        f = fronted.get(name, 0)
        c_ = collected.get(name, 0)
        physical = c_ - f
        fair = (total_returns - total_cost) / NB_PARTS
        balances[name] = fair - physical

    # Apply transactions
    for name, net_sent in tx_net.items():
        balances[name] = balances.get(name, 0) + net_sent

    c = cur(chat_id)
    lines = ["DETTES\n"]
    for name, bal in sorted(balances.items(), key=lambda x: x[1]):
        if bal > 0.5:
            lines.append(f"  {name} : on lui doit {abs(bal):.0f} {c}")
        elif bal < -0.5:
            lines.append(f"  {name} : doit {abs(bal):.0f} {c} au groupe")
        else:
            lines.append(f"  {name} : a jour")

    debtors = sorted([(n, -bal) for n, bal in balances.items() if bal < -0.5], key=lambda x: -x[1])
    creditors = sorted([(n, bal) for n, bal in balances.items() if bal > 0.5], key=lambda x: -x[1])
    if debtors and creditors:
        lines.append("\nReglements :")
        di = ci = 0
        d = list(debtors)
        cr = list(creditors)
        while di < len(d) and ci < len(cr):
            transfer = min(d[di][1], cr[ci][1])
            lines.append(f"  {d[di][0]} → {cr[ci][0]} : {transfer:.0f} {c}")
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

    sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
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
            "Transactions :\n"
            "  /depense 80 restaurant — frais partage (tu as paye)\n"
            "  /depense Rapha 50 uber — frais partage (Rapha a paye)\n"
            "  /remb Rapha 200 a Kekko — transfert direct\n\n"
            "Stats :\n"
            "  /solde — balance Tricount entre vous deux\n"
            "  /dettes — detail qui doit quoi\n"
            "  /pending — paris en attente\n"
            "  /historique — 15 derniers paris\n"
            "  /stats — stats detaillees\n"
            "  /delete <id> — supprimer un pari\n"
            "  /deletetx <id> — supprimer un remb/depense"
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
            "Transactions :\n"
            "  /remb Marco 100 a Kekko — enregistrer un remboursement\n\n"
            "Stats :\n"
            "  /solde — P&L du groupe\n"
            "  /dettes — qui doit quoi a qui\n"
            "  /pending — paris en attente\n"
            "  /historique — 15 derniers paris\n"
            "  /stats — stats detaillees\n"
            "  /delete <id> — supprimer un pari\n"
            "  /deletetx <id> — supprimer un remb"
        )
    await update.message.reply_text(text)


# ── /remb — Transaction hors-paris ────────────────────────────
REMB_PATTERN = re.compile(
    r"(\w+)\s+"              # from
    r"(\d+(?:[.,]\d+)?)"     # amount
    r"\s*(?:€|eur|chf)?"     # optional currency
    r"\s*(?:à|a)\s+"         # "à" or "a"
    r"(\w+)"                 # to
    r"(?:\s+(.+))?",         # optional description
    re.IGNORECASE
)

async def cmd_remb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Format : /remb <de> <montant> à <vers> [description]\n"
            "Ex: /remb Rapha 200 à Kekko remboursement"
        )
        return

    raw = " ".join(ctx.args)
    m = REMB_PATTERN.search(raw)
    if not m:
        await update.message.reply_text("Format pas reconnu.\nEx: /remb Rapha 200 à Kekko remboursement")
        return

    from_name = m.group(1).capitalize()
    amount = float(m.group(2).replace(",", "."))
    to_name = m.group(3).capitalize()
    description = m.group(4).strip() if m.group(4) else "Transfert"

    if amount <= 0:
        await update.message.reply_text("Montant invalide.")
        return

    chat_id = update.message.chat_id
    now = datetime.now(timezone.utc).isoformat()
    c = cur(chat_id)

    con = db()
    cur_ = con.execute(
        "INSERT INTO transactions (chat_id, from_name, to_name, amount, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, from_name, to_name, amount, description, now)
    )
    tx_id = cur_.lastrowid
    con.commit()

    # Show updated balance
    if is_duo(chat_id):
        tc = get_duo_tricount(con, chat_id)
        debt_text = format_tricount_balance(tc, chat_id)
    else:
        debt_text = ""

    con.close()

    text = (
        f"Transaction #{tx_id} enregistree\n"
        f"   {from_name} → {to_name} : {amount:.0f} {c}\n"
        f"   Motif : {description}"
    )
    if debt_text:
        text += f"\n\nBalance : {debt_text}"
    await update.message.reply_text(text)

    sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({
        "action": "transaction",
        "id": tx_id,
        "date": now[:10],
        "from_name": from_name,
        "to_name": to_name,
        "amount": amount,
        "description": description,
        "sheet_tab": sheet_tab
    })


# ── /deletetx — Supprimer une transaction ou depense ────────
async def cmd_deletetx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not ctx.args:
        await update.message.reply_text("Usage : /deletetx <id>\nL'ID est affiche quand tu enregistres un /remb ou /depense.")
        return
    try:
        tx_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID invalide.")
        return

    con = db()
    # Try transactions first
    tx = con.execute(
        "SELECT * FROM transactions WHERE id = ? AND chat_id = ?",
        (tx_id, chat_id)
    ).fetchone()
    if tx:
        con.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        con.commit()
        con.close()
        await update.message.reply_text(
            f"Transaction #{tx_id} supprimee\n"
            f"   {tx['from_name']} -> {tx['to_name']} : {tx['amount']:.0f} {cur(chat_id)}\n"
            f"   Motif : {tx['description']}"
        )
        sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
        await sync_sheets({
            "action": "delete_transaction",
            "id": tx_id,
            "sheet_tab": sheet_tab
        })
        return

    # Try expenses
    exp = con.execute(
        "SELECT * FROM expenses WHERE id = ? AND chat_id = ?",
        (tx_id, chat_id)
    ).fetchone()
    if exp:
        con.execute("DELETE FROM expenses WHERE id = ?", (tx_id,))
        con.commit()
        con.close()
        await update.message.reply_text(
            f"Depense #{tx_id} supprimee\n"
            f"   Paye par {exp['paid_by']} : {exp['amount']:.0f} {cur(chat_id)}\n"
            f"   Motif : {exp['description']}"
        )
        sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
        await sync_sheets({
            "action": "delete_transaction",
            "id": tx_id,
            "sheet_tab": sheet_tab
        })
        return

    con.close()
    await update.message.reply_text(f"Transaction/depense #{tx_id} introuvable.")


# ── /depense — Frais partagé (Tricount) ─────────────────────
async def cmd_depense(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not is_duo(chat_id):
        await update.message.reply_text("Cette commande est reservee au mode duo.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "Format : /depense <montant> <description>\n"
            "Ex: /depense 80 restaurant\n"
            "Ex: /depense Rapha 50 uber"
        )
        return

    first = ctx.args[0]
    try:
        amount = float(first.replace(",", "."))
        raw_name = update.message.from_user.first_name
        paid_by = NAME_MAP.get(raw_name, raw_name)
        description = " ".join(ctx.args[1:]).strip() or "Depense partagee"
    except ValueError:
        paid_by = first.capitalize()
        if len(ctx.args) < 2:
            await update.message.reply_text("Montant manquant.\nEx: /depense 80 restaurant")
            return
        try:
            amount = float(ctx.args[1].replace(",", "."))
        except ValueError:
            await update.message.reply_text("Montant invalide.\nEx: /depense 80 restaurant")
            return
        description = " ".join(ctx.args[2:]).strip() or "Depense partagee"

    if amount <= 0:
        await update.message.reply_text("Montant invalide.")
        return

    now = datetime.now(timezone.utc).isoformat()
    c = cur(chat_id)

    con = db()
    cur_ = con.execute(
        "INSERT INTO expenses (chat_id, paid_by, amount, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, paid_by, amount, description, now)
    )
    exp_id = cur_.lastrowid
    con.commit()

    tc = get_duo_tricount(con, chat_id)
    balance_text = format_tricount_balance(tc, chat_id)
    con.close()

    text = (
        f"Depense #{exp_id} enregistree\n"
        f"   {paid_by} a paye {amount:.0f} {c} ({description})\n"
        f"   Part de chacun : {amount/2:.0f} {c}\n\n"
        f"Balance : {balance_text}"
    )
    await update.message.reply_text(text)

    await sync_sheets({
        "action": "expense",
        "id": exp_id,
        "date": now[:10],
        "paid_by": paid_by,
        "amount": amount,
        "description": description,
        "sheet_tab": "Kekko-Rapha"
    })


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

        tc = get_duo_tricount(con, chat_id)
        debt_text = format_tricount_balance(tc, chat_id)
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

    sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status, "sheet_tab": sheet_tab})


# ── /sync — Pousser l'état de la DB vers Google Sheets ────────
async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Export all bets + transactions from DB to Sheet (reverse of /restore)."""
    chat_id = update.message.chat_id
    if not SHEETS_WEBHOOK_URL:
        await update.message.reply_text("SHEETS_WEBHOOK_URL non configure.")
        return

    duo = is_duo(chat_id)
    sheet_tab = "Kekko-Rapha" if duo else "Paris"
    sheet_id = SHEET_ID_DUO if duo else SHEET_ID_GROUP

    con = db()
    bets = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? ORDER BY id",
        (chat_id,)
    ).fetchall()
    transactions = con.execute(
        "SELECT * FROM transactions WHERE chat_id = ? ORDER BY id",
        (chat_id,)
    ).fetchall()
    expenses = con.execute(
        "SELECT * FROM expenses WHERE chat_id = ? ORDER BY id",
        (chat_id,)
    ).fetchall()
    con.close()

    bets_data = []
    for b in bets:
        bets_data.append({
            "id": b["id"],
            "date": (b["created_at"] or "")[:10],
            "description": b["description"],
            "stake": b["stake"],
            "odds": b["odds"],
            "user_name": b["user_name"],
            "status": b["status"]
        })

    tx_data = []
    for t in transactions:
        tx_data.append({
            "id": t["id"],
            "date": (t["created_at"] or "")[:10],
            "from_name": t["from_name"],
            "to_name": t["to_name"],
            "amount": t["amount"],
            "description": t["description"]
        })

    exp_data = []
    for e in expenses:
        exp_data.append({
            "id": e["id"],
            "date": (e["created_at"] or "")[:10],
            "paid_by": e["paid_by"],
            "amount": e["amount"],
            "description": e["description"]
        })

    payload = {
        "action": "full_sync",
        "sheet_id": sheet_id,
        "sheet_tab": sheet_tab,
        "bets": bets_data,
        "transactions": tx_data,
        "expenses": exp_data
    }

    await update.message.reply_text(
        f"Sync en cours... ({len(bets_data)} paris, {len(tx_data)} tx, {len(exp_data)} dep)"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEETS_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    await update.message.reply_text(
                        f"Sync terminee !\n"
                        f"  {len(bets_data)} paris\n"
                        f"  {len(tx_data)} transactions\n"
                        f"  {len(exp_data)} depenses\n\n"
                        f"Le Google Sheet est maintenant a jour."
                    )
                else:
                    await update.message.reply_text(f"Erreur Sheets: HTTP {resp.status}")
    except Exception as e:
        await update.message.reply_text(f"Erreur: {e}")


# ── /restore — Re-importer les paris depuis Google Sheets ───
async def cmd_restore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not SHEETS_WEBHOOK_URL:
        await update.message.reply_text("SHEETS_WEBHOOK_URL non configure.")
        return

    duo = is_duo(chat_id)
    sheet_tab = "Kekko-Rapha" if duo else "Paris"
    sheet_id = SHEET_ID_DUO if duo else SHEET_ID_GROUP
    payload = {"action": "export_data", "sheet_tab": sheet_tab, "sheet_id": sheet_id}

    await update.message.reply_text("Restauration en cours depuis Google Sheets...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEETS_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    await update.message.reply_text(f"Erreur Sheets: HTTP {resp.status}")
                    return
                data = await resp.json(content_type=None)
    except Exception as e:
        await update.message.reply_text(f"Erreur: {e}")
        return

    if data.get("status") != "ok":
        await update.message.reply_text("Reponse invalide du serveur Sheets.")
        return

    con = db()
    # Clear existing data for this chat
    con.execute("DELETE FROM bets WHERE chat_id = ?", (chat_id,))
    con.execute("DELETE FROM transactions WHERE chat_id = ?", (chat_id,))
    con.execute("DELETE FROM expenses WHERE chat_id = ?", (chat_id,))
    # Reset auto-increment so new IDs start after the highest remaining ID
    for tbl in ("bets", "transactions", "expenses"):
        max_id = con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {tbl}").fetchone()[0]
        if max_id > 0:
            con.execute(f"UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (max_id, tbl))
        else:
            con.execute(f"DELETE FROM sqlite_sequence WHERE name = ?", (tbl,))

    nb_bets = 0
    for b in data.get("bets", []):
        if not b.get("id"):
            continue
        status = (b.get("status") or "PENDING").strip().upper()
        status_map = {"WON": "won", "LOST": "lost", "PENDING": "pending", "VOID": "void"}
        status = status_map.get(status, "pending")
        date_str = str(b.get("date", ""))[:10]
        con.execute(
            "INSERT INTO bets (chat_id, description, stake, odds, user_name, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, b.get("description", ""), float(b.get("stake", 0)),
             float(b.get("odds", 0)), b.get("user_name", ""), status, date_str)
        )
        nb_bets += 1

    nb_tx = 0
    for t in data.get("transactions", []):
        if not t.get("id"):
            continue
        to_name = t.get("to_name", "")
        date_str = str(t.get("date", ""))[:10]
        if to_name == "DEPENSE":
            con.execute(
                "INSERT INTO expenses (chat_id, paid_by, amount, description, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, t.get("from_name", ""), float(t.get("amount", 0)),
                 t.get("description", ""), date_str)
            )
        else:
            con.execute(
                "INSERT INTO transactions (chat_id, from_name, to_name, amount, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, t.get("from_name", ""), to_name,
                 float(t.get("amount", 0)), t.get("description", ""), date_str)
            )
        nb_tx += 1

    con.commit()
    con.close()

    await update.message.reply_text(
        f"Restauration terminee !\n"
        f"  {nb_bets} paris importes\n"
        f"  {nb_tx} transactions importees\n\n"
        f"Utilisez /pending ou /historique pour verifier."
    )


# ── Main ────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("ERROR: Set BOT_TOKEN environment variable")
        return

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    async def post_init(application):
        await application.bot.set_my_commands([
            ("lock", "Enregistrer un pari"),
            ("solde", "Voir le solde P&L"),
            ("dettes", "Voir qui doit quoi"),
            ("pending", "Paris en cours"),
            ("historique", "Derniers paris resolus"),
            ("stats", "Statistiques detaillees"),
            ("depense", "Depense partagee (duo)"),
            ("remb", "Remboursement / transfert"),
            ("delete", "Supprimer un pari"),
            ("deletetx", "Supprimer un remb/depense"),
            ("help", "Aide et commandes"),
        ])
    app.post_init = post_init

    app.add_handler(CommandHandler("lock", cmd_lock))
    for cmd in ["win", "w", "gagne", "loss", "lose", "l", "perdu", "void", "push", "annule"]:
        app.add_handler(CommandHandler(cmd, cmd_result))

    app.add_handler(CommandHandler("solde", cmd_solde))
    app.add_handler(CommandHandler("historique", cmd_historique))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("dettes", cmd_dettes))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("deletetx", cmd_deletetx))
    app.add_handler(CommandHandler("remb", cmd_remb))
    app.add_handler(CommandHandler("depense", cmd_depense))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("sync", cmd_sync))

    app.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND,
        on_reply_result
    ))

    log.info(f"Bot started (DUO_CHAT_ID={DUO_CHAT_ID})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
