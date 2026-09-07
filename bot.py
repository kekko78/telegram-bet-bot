"""
Telegram Bet Tracker Bot
Deux modes :
  - Groupe (Bets Suisse) : divise par 3, CHF, bankroll commune
  - Duo (Kekko-Rapha) : Tricount style, EUR, qui doit quoi à qui

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
DB_PATH = os.environ.get("DB_PATH", "/data/bets.db")
DUO_CHAT_ID = int(os.environ.get("DUO_CHAT_ID", "0"))
SHEET_ID_GROUP = "1izpo65I_FgrTUaarqiGCJHv7VQ2A-ixMOcnb7PJrU7k"
SHEET_ID_DUO   = "1oLodmWlhKfoSdcmgWeR42bcrCh_7YJUBJ9jMMps5EgU"
GROUP_DEFAULT_BETTOR = "Marco"
ANNEXE_HANDLER = "Kekko"  # Alex handles annexe payments/recoveries
NAME_MAP = {"Twix": "Kekko"}

def parse_name_override(raw: str, update) -> tuple:
    """Parse @name from text OR Telegram mention entities.
    Returns (name_or_None, cleaned_raw).
    Handles autocomplete mentions where Telegram strips the @ character.
    """
    # 1. Try literal @name in text
    m = re.search(r'@\s*(\S+)\s*$', raw)
    if m:
        name = m.group(1).strip().capitalize()
        name = NAME_MAP.get(name, name)
        return name, raw[:m.start()].strip()
    # 2. Try Telegram mention entities (autocomplete strips @)
    if update.message and update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                text = update.message.text[entity.offset:entity.offset + entity.length]
                name = text.lstrip("@").strip().capitalize()
                name = NAME_MAP.get(name, name)
                clean = text.lstrip("@")
                raw = re.sub(r'\s*' + re.escape(clean) + r'\s*$', '', raw, flags=re.IGNORECASE).strip()
                return name, raw
            elif entity.type == "text_mention":
                raw_name = entity.user.first_name
                name = NAME_MAP.get(raw_name, raw_name)
                display = update.message.text[entity.offset:entity.offset + entity.length]
                raw = re.sub(r'\s*' + re.escape(display) + r'\s*$', '', raw, flags=re.IGNORECASE).strip()
                return name, raw
    return None, raw

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
    Returns dict with balance (positive = b owes a, alphabetical order) or None.
    Excludes Loro bets (separate CHF tracking)."""
    bets = con.execute(
        "SELECT user_name, stake, odds, status FROM bets WHERE chat_id = ? AND (is_loro IS NULL OR is_loro = 0)", (chat_id,)
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


def get_group_tricount(con, chat_id: int) -> dict:
    """Tricount balance for group mode (3-way split).
    Returns {name: balance} where positive = group owes you."""
    bets = con.execute(
        "SELECT user_name, stake, odds, status FROM bets WHERE chat_id = ? AND status != 'void'",
        (chat_id,)
    ).fetchall()
    txs = con.execute(
        "SELECT from_name, to_name, amount FROM transactions WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    expenses = con.execute(
        "SELECT paid_by, amount FROM expenses WHERE chat_id = ?", (chat_id,)
    ).fetchall()

    # Net outflow per person (positive = paid out, negative = received)
    outflow = {}

    for b in bets:
        name = b["user_name"]
        outflow[name] = outflow.get(name, 0) + b["stake"]
        if b["status"] == "won":
            outflow[name] -= b["stake"] * b["odds"]

    for e in expenses:
        name = e["paid_by"]
        # amount > 0: paid expense (outflow increases)
        # amount < 0: retrait (outflow decreases = received money)
        outflow[name] = outflow.get(name, 0) + e["amount"]

    for t in txs:
        outflow[t["from_name"]] = outflow.get(t["from_name"], 0) + t["amount"]
        outflow[t["to_name"]] = outflow.get(t["to_name"], 0) - t["amount"]

    total_outflow = sum(outflow.values())
    fair_share = total_outflow / NB_PARTS

    balances = {}
    for name, out in outflow.items():
        balances[name] = out - fair_share
    return balances


def format_group_tricount(balances: dict, chat_id: int) -> str:
    if not balances:
        return "Pas encore de donnees"
    c = cur(chat_id)
    # Find settlements
    debtors = [(n, -b) for n, b in balances.items() if b < -0.5]
    creditors = [(n, b) for n, b in balances.items() if b > 0.5]
    if not debtors and not creditors:
        return "Tout le monde est a jour"
    parts = []
    for n, amt in sorted(creditors, key=lambda x: -x[1]):
        parts.append(f"on doit {amt:.0f} {c} a {n}")
    for n, amt in sorted(debtors, key=lambda x: -x[1]):
        parts.append(f"{n} doit {amt:.0f} {c}")
    return " | ".join(parts)


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
    # Add annexe columns (safe to re-run)
    for col, typedef in [("annexe_name", "TEXT"), ("annexe_stake", "REAL DEFAULT 0"),
                         ("is_loro", "INTEGER DEFAULT 0")]:
        try:
            con.execute(f"ALTER TABLE bets ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # column already exists
    con.execute("""
        CREATE TABLE IF NOT EXISTS loro_capital (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            amount      REAL NOT NULL,
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
    payload["sheet_id"] = SHEET_ID_DUO if tab in ("Kekko-Rapha", "Loro") else SHEET_ID_GROUP
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

    # Optional @name or @loro override after odds
    remainder = raw[m.end():].strip()

    # Check for @loro (Swiss book, CHF, 50/50 Kekko-Rapha)
    is_loro_bet = False
    loro_match = re.search(r'@\s*loro\b', remainder, re.IGNORECASE)
    if loro_match:
        is_loro_bet = True
        bettor_name = "Loro"
        remainder = (remainder[:loro_match.start()] + remainder[loro_match.end():]).strip()
    else:
        override = re.match(r'@\s*(\S+)', remainder)
        if override:
            bettor_name = override.group(1).strip().capitalize()
            bettor_name = NAME_MAP.get(bettor_name, bettor_name)
            remainder = remainder[override.end():].strip()
        else:
            # Check Telegram mention entities (autocomplete strips @)
            mention_name = None
            if update.message and update.message.entities:
                for entity in update.message.entities:
                    if entity.type in ("mention", "text_mention"):
                        if entity.type == "mention":
                            text = update.message.text[entity.offset:entity.offset + entity.length]
                            mention_name = text.lstrip("@").strip().capitalize()
                        else:
                            mention_name = entity.user.first_name
                        mention_name = NAME_MAP.get(mention_name, mention_name)
                        # Remove from remainder
                        display = update.message.text[entity.offset:entity.offset + entity.length].lstrip("@")
                        remainder = re.sub(r'\s*' + re.escape(display), '', remainder, flags=re.IGNORECASE).strip()
                        break
            if mention_name:
                bettor_name = mention_name
            elif not is_duo(chat_id):
                bettor_name = GROUP_DEFAULT_BETTOR
            else:
                raw = user.first_name
                bettor_name = NAME_MAP.get(raw, raw)

    # Parse -NomAnnexe MONTANT (group mode + Loro)
    annexe_name = None
    annexe_stake = 0.0
    if not is_duo(chat_id) or is_loro_bet:
        annexe_match = re.search(r'-(\w+)\s+(\d+(?:[.,]\d+)?)', remainder)
        if annexe_match:
            annexe_name = annexe_match.group(1).capitalize()
            annexe_stake = float(annexe_match.group(2).replace(",", "."))
            if annexe_stake >= stake:
                await update.message.reply_text("La mise annexe doit etre inferieure a la mise totale.")
                return

    now = datetime.now(timezone.utc).isoformat()

    con = db()
    cur_ = con.execute(
        """INSERT INTO bets (chat_id, message_id, user_id, user_name, description, stake, odds, status, created_at, annexe_name, annexe_stake, is_loro)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (chat_id, update.message.message_id, user.id, bettor_name, desc, stake, odds, now, annexe_name, annexe_stake, 1 if is_loro_bet else 0)
    )
    bet_id = cur_.lastrowid
    con.commit()
    con.close()

    if is_loro_bet:
        # Loro mode: CHF, 50/50 split (minus annexe if any)
        loro_s = stake - annexe_stake
        pp = loro_s / 2
        gain_loro = loro_s * (odds - 1)
        gain_pp = gain_loro / 2
        if annexe_name:
            gain_annexe = annexe_stake * (odds - 1)
            text = (
                f"Pari #{bet_id} enregistre [LORO]\n"
                f"   {desc} @ {odds:.2f}\n"
                f"   Mise : {stake:.0f} CHF\n"
                f"   ├ Duo : {loro_s:.0f} CHF ({pp:.0f}/pers.)\n"
                f"   └ Annexe ({annexe_name}) : {annexe_stake:.0f} CHF\n"
                f"   Gain potentiel : +{gain_pp:.0f}/pers. (+{gain_annexe:.0f} {annexe_name})\n\n"
                f"Resultat → repondre avec /win ou /loss"
            )
        else:
            text = (
                f"Pari #{bet_id} enregistre [LORO]\n"
                f"   {desc} @ {odds:.2f}\n"
                f"   Mise : {stake:.0f} CHF ({pp:.0f}/pers.)\n"
                f"   Gain potentiel : +{gain_loro:.0f} CHF (+{gain_pp:.0f}/pers.)\n\n"
                f"Resultat → repondre avec /win ou /loss"
            )
        await update.message.reply_text(text)
        sync_payload_loro = {
            "action": "new_bet",
            "id": bet_id,
            "date": now[:10],
            "description": desc,
            "stake": stake,
            "odds": odds,
            "user_name": "Loro",
            "sheet_tab": "Loro"
        }
        if annexe_name:
            sync_payload_loro["annexe_name"] = annexe_name
            sync_payload_loro["annexe_stake"] = annexe_stake
        await sync_sheets(sync_payload_loro)
        return

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
    elif annexe_name:
        trio_s = stake - annexe_stake
        pp = trio_s / NB_PARTS
        gain_pp = trio_s * (odds - 1) / NB_PARTS
        text = (
            f"Pari #{bet_id} enregistre\n"
            f"   {desc} @ {odds:.2f}\n"
            f"   Mise : {stake:.0f} {c}\n"
            f"   ├ Trio : {trio_s:.0f} {c} ({pp:.0f}/pers.)\n"
            f"   └ Annexe ({annexe_name}) : {annexe_stake:.0f} {c}\n"
            f"   Gain potentiel : {fmt(gain_pp, chat_id)}/pers.\n"
            f"   Avance {annexe_stake:.0f} {c} par {bettor_name} pour {annexe_name}\n\n"
            f"Resultat → repondre a ce message avec /win ou /loss"
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
    sync_payload = {
        "action": "new_bet",
        "id": bet_id,
        "date": now[:10],
        "description": desc,
        "stake": stake,
        "odds": odds,
        "user_name": bettor_name,
        "sheet_tab": sheet_tab
    }
    if annexe_name:
        sync_payload["annexe_name"] = annexe_name
        sync_payload["annexe_stake"] = annexe_stake
    await sync_sheets(sync_payload)


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
            "SELECT * FROM bets WHERE chat_id = ? AND message_id = ?",
            (chat_id, msg.reply_to_message.message_id)
        ).fetchone()
        if not bet and msg.reply_to_message.text:
            id_match = re.search(r"Pari #(\d+)", msg.reply_to_message.text)
            if id_match:
                bet = con.execute(
                    "SELECT * FROM bets WHERE id = ? AND chat_id = ?",
                    (int(id_match.group(1)), chat_id)
                ).fetchone()

    if not bet and ctx.args:
        try:
            bet_id = int(ctx.args[0])
            bet = con.execute(
                "SELECT * FROM bets WHERE id = ? AND chat_id = ?",
                (bet_id, chat_id)
            ).fetchone()
        except (ValueError, IndexError):
            pass

    # Si le pari a deja le statut demande, on ignore
    if bet and bet["status"] == status:
        con.close()
        await msg.reply_text(f"Pari #{bet['id']} est deja {status.upper()}.")
        return

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
    annexe_s = bet["annexe_stake"] or 0
    annexe_n = bet["annexe_name"] or ""
    bet_is_loro = bool(bet["is_loro"]) if bet["is_loro"] else False

    if bet_is_loro:
        # Loro mode: CHF, 50/50 split (minus annexe)
        loro_s = stake - annexe_s
        pnl_duo = bet_pnl(loro_s, odds, status)
        pnl_pp = pnl_duo / 2
        if status == "won":
            result_text = f"GAGNE  +{pnl_pp:.0f}/pers."
        elif status == "lost":
            result_text = f"PERDU  {pnl_pp:.0f}/pers."
        else:
            result_text = "ANNULE"

        annexe_text = ""
        if annexe_s > 0 and status != "void":
            pnl_annexe = bet_pnl(annexe_s, odds, status)
            if status == "won":
                annexe_text = f"\n   Annexe ({annexe_n}) : Kekko doit {annexe_s * (odds - 1):.0f} CHF a {annexe_n}"
            elif status == "lost":
                annexe_text = f"\n   Annexe ({annexe_n}) : {annexe_n} doit {annexe_s:.0f} CHF a Kekko"

        # Cumul Loro P&L (excluding annexe from per-person)
        loro_rows = con.execute(
            "SELECT status, stake, odds, annexe_stake FROM bets WHERE chat_id = ? AND is_loro = 1 AND status IN ('won','lost')",
            (chat_id,)
        ).fetchall()
        total_loro = sum(bet_pnl(r["stake"] - (r["annexe_stake"] or 0), r["odds"], r["status"]) / 2 for r in loro_rows)
        con.close()

        text = (
            f"Pari #{bet['id']} : {result_text} [LORO]\n"
            f"   {bet['description']} @ {odds:.2f}"
            f"{annexe_text}\n\n"
            f"P&L cumule Loro : {total_loro:+.0f} CHF/pers."
        )
        await msg.reply_text(text)
        await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status, "sheet_tab": "Loro"})
        return

    if is_duo(chat_id):
        pnl = bet_pnl(stake, odds, status)
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
        trio_s = stake - annexe_s
        trio_pnl = bet_pnl(trio_s, odds, status)
        if status == "won":
            result_text = f"GAGNE  {fmt(trio_pnl / NB_PARTS, chat_id)}/pers."
        elif status == "lost":
            result_text = f"PERDU  {fmt(trio_pnl / NB_PARTS, chat_id)}/pers."
        else:
            result_text = f"ANNULE  0 {cur(chat_id)}"

        rows = con.execute(
            "SELECT status, stake, odds, annexe_stake FROM bets WHERE chat_id = ? AND status IN ('won','lost') AND (is_loro IS NULL OR is_loro = 0)",
            (chat_id,)
        ).fetchall()
        total_pnl = sum(bet_pnl(r["stake"] - (r["annexe_stake"] or 0), r["odds"], r["status"]) / NB_PARTS for r in rows)
        con.close()

        annexe_text = ""
        if annexe_s > 0 and status != "void":
            c = cur(chat_id)
            bettor = bet["user_name"]
            if status == "won":
                annexe_profit = annexe_s * (odds - 1)
                annexe_text = f"\n   Annexe ({annexe_n}) : {bettor} doit {annexe_profit:.0f} {c} a {ANNEXE_HANDLER}"
            elif status == "lost":
                annexe_text = f"\n   Annexe ({annexe_n}) : avance de {annexe_s:.0f} {c} (inchangee)"

        text = (
            f"Pari #{bet['id']} : {result_text}\n"
            f"   {bet['description']} @ {odds:.2f}"
            f"{annexe_text}\n\n"
            f"P&L cumule : {fmt(total_pnl, chat_id)}/pers."
        )

    await msg.reply_text(text)

    sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status, "sheet_tab": sheet_tab})


# ── /solde ──────────────────────────────────────────────────
async def cmd_solde(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()

    loro_filter = " AND (is_loro IS NULL OR is_loro = 0)" if is_duo(chat_id) else ""
    pending = con.execute(
        f"SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets WHERE chat_id = ? AND status = 'pending'{loro_filter}",
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
            "SELECT status, stake, odds, annexe_stake FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
            (chat_id,)
        ).fetchall()
        expenses = con.execute(
            "SELECT paid_by, amount, description FROM expenses WHERE chat_id = ?",
            (chat_id,)
        ).fetchall()
        balances = get_group_tricount(con, chat_id)
        con.close()

        total_pnl = 0.0
        wins = losses = 0
        total_staked = 0.0
        for r in rows:
            trio_s = r["stake"] - (r["annexe_stake"] or 0)
            total_staked += trio_s
            pnl = bet_pnl(trio_s, r["odds"], r["status"])
            total_pnl += pnl / NB_PARTS
            if r["status"] == "won":
                wins += 1
            else:
                losses += 1

        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0
        roi = (total_pnl / (total_staked / NB_PARTS) * 100) if total_staked > 0 else 0
        c = cur(chat_id)

        total_depots = sum(e["amount"] for e in expenses if e["amount"] > 0)
        total_retraits = sum(-e["amount"] for e in expenses if e["amount"] < 0)

        text = (
            f"SOLDE DU GROUPE\n\n"
            f"P&L par personne : {fmt(total_pnl, chat_id)}\n"
            f"Paris : {wins}W - {losses}L ({wr:.0f}%)\n"
            f"ROI : {roi:+.1f}%\n"
            f"Mise totale : {total_staked:.0f} {c}"
        )
        if total_depots > 0:
            text += f"\nDepots : {total_depots:.0f} {c}"
        if total_retraits > 0:
            text += f"\nRetraits : {total_retraits:.0f} {c}"
        if pending[0] > 0:
            text += f"\n\nEn attente : {pending[0]} paris ({pending[1]:.0f} {c})"
        if balances:
            text += f"\n\n{format_group_tricount(balances, chat_id)}"
        await update.message.reply_text(text)


# ── /soldeloro — P&L des paris Loro (CHF) ──────────────────
async def cmd_solde_loro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()

    rows = con.execute(
        "SELECT status, stake, odds, annexe_stake, annexe_name FROM bets WHERE chat_id = ? AND is_loro = 1 AND status IN ('won','lost') ORDER BY id",
        (chat_id,)
    ).fetchall()
    pending = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(stake), 0) FROM bets WHERE chat_id = ? AND is_loro = 1 AND status = 'pending'",
        (chat_id,)
    ).fetchone()
    con.close()

    if not rows and pending[0] == 0:
        await update.message.reply_text("Aucun pari Loro enregistre.")
        return

    wins = losses = 0
    total_pnl_duo = 0.0
    total_pnl_rago = 0.0
    total_staked = 0.0
    for r in rows:
        a_s = r["annexe_stake"] or 0
        duo_s = r["stake"] - a_s
        total_pnl_duo += bet_pnl(duo_s, r["odds"], r["status"])
        if a_s > 0:
            total_pnl_rago += bet_pnl(a_s, r["odds"], r["status"])
        total_staked += r["stake"]
        if r["status"] == "won":
            wins += 1
        else:
            losses += 1

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    roi = (total_pnl_duo / total_staked * 100) if total_staked > 0 else 0
    pnl_pp = total_pnl_duo / 2

    # Capital tracking
    con2 = db()
    cap_rows = con2.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM loro_capital WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()
    cap_ops = con2.execute(
        "SELECT COUNT(*) FROM loro_capital WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()
    con2.close()

    total_deposits = cap_rows[0]
    total_pnl_full = total_pnl_duo + total_pnl_rago  # full P&L for capital
    pending_stakes = pending[1] if pending[0] > 0 else 0

    text = f"SOLDE LORO (CHF)\n"

    if total_deposits != 0 or cap_ops[0] > 0:
        capital_now = total_deposits + total_pnl_full - pending_stakes
        text += (
            f"\nCapital depose : {total_deposits:.0f} CHF"
            f"\nP&L resolu : {total_pnl_full:+.0f} CHF"
        )
        if pending[0] > 0:
            text += f"\nEn jeu : {pending_stakes:.0f} CHF ({pending[0]} paris)"
        text += f"\nDisponible : {capital_now:.0f} CHF\n"

    text += (
        f"\nParis : {wins}W - {losses}L ({wr:.0f}%)\n"
        f"P&L duo : {total_pnl_duo:+.0f} CHF ({pnl_pp:+.0f}/pers.)"
    )
    if pnl_pp >= 0:
        text += f"\n  → Kekko doit {pnl_pp:.0f} CHF a Rapha"
    else:
        text += f"\n  → Rapha doit {-pnl_pp:.0f} CHF a Kekko"
    text += f"\nROI : {roi:+.1f}%"
    if total_pnl_rago != 0:
        text += f"\n\nRago (sur compte Kekko) : {total_pnl_rago:+.0f} CHF"
    if pending[0] > 0 and (total_deposits == 0 and cap_ops[0] == 0):
        text += f"\n\nEn attente : {pending[0]} paris ({pending_stakes:.0f} CHF)"

    await update.message.reply_text(text)


# ── /depotLoro /retraitLoro ────────────────────────────────
async def cmd_depot_loro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    parts = update.message.text.strip().split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Usage : /depotLoro 2000 [description]")
        return
    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Montant invalide.")
        return
    if amount <= 0:
        await update.message.reply_text("Le montant doit etre positif.")
        return
    desc = parts[2] if len(parts) > 2 else "depot"
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    con.execute(
        "INSERT INTO loro_capital (chat_id, amount, description, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, amount, desc, now)
    )
    con.commit()
    total = con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM loro_capital WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()[0]
    con.close()
    await update.message.reply_text(f"Depot Loro : +{amount:.0f} CHF ({desc})\nCapital depose total : {total:.0f} CHF")


async def cmd_retrait_loro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    parts = update.message.text.strip().split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Usage : /retraitLoro 500 [description]")
        return
    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Montant invalide.")
        return
    if amount <= 0:
        await update.message.reply_text("Le montant doit etre positif.")
        return
    desc = parts[2] if len(parts) > 2 else "retrait"
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    con.execute(
        "INSERT INTO loro_capital (chat_id, amount, description, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, -amount, desc, now)
    )
    con.commit()
    total = con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM loro_capital WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()[0]
    con.close()
    await update.message.reply_text(f"Retrait Loro : -{amount:.0f} CHF ({desc})\nCapital depose total : {total:.0f} CHF")


# ── /historique ─────────────────────────────────────────────
async def cmd_historique(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()
    loro_filter = " AND (is_loro IS NULL OR is_loro = 0)" if is_duo(chat_id) else ""
    rows = con.execute(
        f"SELECT * FROM bets WHERE chat_id = ?{loro_filter} ORDER BY id DESC LIMIT 15",
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
        a_s = (r["annexe_stake"] or 0) if not duo else 0
        trio_s = r["stake"] - a_s
        pnl = bet_pnl(trio_s if not duo else r["stake"], r["odds"], r["status"])
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
        annexe_tag = f" [+{r['annexe_name']}]" if not duo and (r["annexe_stake"] or 0) > 0 else ""
        lines.append(
            f"{icon} #{r['id']} {date_str}{par} | {r['description']} "
            f"@ {r['odds']:.2f} | {r['stake']:.0f} {c}{annexe_tag} | {result}"
        )
    await update.message.reply_text("\n".join(lines))


# ── /stats ──────────────────────────────────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()
    loro_filter = " AND (is_loro IS NULL OR is_loro = 0)" if is_duo(chat_id) else ""
    rows = con.execute(
        f"SELECT * FROM bets WHERE chat_id = ? AND status IN ('won','lost'){loro_filter} ORDER BY id",
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
        a_s = (r["annexe_stake"] or 0) if not duo else 0
        trio_s = r["stake"] - a_s
        total_staked += trio_s
        pnl = bet_pnl(trio_s, r["odds"], r["status"]) / divisor
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

    # ── Group tricount mode ──
    balances = get_group_tricount(con, chat_id)

    # Get extra info for display
    bets_rows = con.execute(
        "SELECT user_name, stake, odds, status FROM bets WHERE chat_id = ? AND status IN ('won','lost')",
        (chat_id,)
    ).fetchall()
    pending_rows = con.execute(
        "SELECT user_name, COUNT(*) as cnt, SUM(stake) as total FROM bets WHERE chat_id = ? AND status = 'pending' GROUP BY user_name",
        (chat_id,)
    ).fetchall()
    exp_rows = con.execute(
        "SELECT * FROM expenses WHERE chat_id = ? ORDER BY id DESC LIMIT 10",
        (chat_id,)
    ).fetchall()
    tx_list = get_transactions_list(con, chat_id)
    con.close()

    if not balances and not bets_rows:
        await update.message.reply_text("Aucune donnee.")
        return

    c = cur(chat_id)
    lines = ["DETTES\n"]

    # Show balances
    for name, bal in sorted(balances.items(), key=lambda x: x[1]):
        if bal > 0.5:
            lines.append(f"  {name} : on lui doit {abs(bal):.0f} {c}")
        elif bal < -0.5:
            lines.append(f"  {name} : doit {abs(bal):.0f} {c} au groupe")
        else:
            lines.append(f"  {name} : a jour")

    # P&L from bets
    total_pnl = 0.0
    wins = losses = 0
    for r in bets_rows:
        pnl = bet_pnl(r["stake"], r["odds"], r["status"])
        total_pnl += pnl
        if r["status"] == "won":
            wins += 1
        else:
            losses += 1
    if wins + losses > 0:
        lines.append(f"\n--- Paris : {wins}W-{losses}L, P&L {fmt(total_pnl / NB_PARTS, chat_id)}/pers. ---")

    # Pending
    for p in pending_rows:
        lines.append(f"  {p['user_name']} : {p['cnt']} pending ({p['total']:.0f} {c})")

    # Expenses & retraits
    deps = [e for e in exp_rows if e["amount"] > 0]
    rets = [e for e in exp_rows if e["amount"] < 0]
    if deps:
        lines.append("\n--- Depenses ---")
        for e in deps[:5]:
            lines.append(f"  #{e['id']} {e['paid_by']} a paye {e['amount']:.0f} {c} ({e['description']})")
    if rets:
        lines.append("\n--- Retraits ---")
        for e in rets[:5]:
            desc = e['description'].replace("[RETRAIT] ", "")
            lines.append(f"  #{e['id']} {e['paid_by']} a recu {abs(e['amount']):.0f} {c} ({desc})")

    # Transfers
    if tx_list:
        lines.append("\n--- Transferts ---")
        for tx in tx_list[:5]:
            lines.append(f"  {tx['from_name']}→{tx['to_name']} {tx['amount']:.0f} {c} ({tx['description']})")

    # Settlement suggestions
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

    # Show active annexe bets
    annexe_bets = [r for r in rows if (r["annexe_stake"] or 0) > 0]
    if annexe_bets:
        lines.append("\nAnnexe :")
        for r in annexe_bets:
            a_s = r["annexe_stake"] or 0
            a_n = r["annexe_name"] or "?"
            bettor = r["user_name"]
            if r["status"] == "pending":
                lines.append(f"  #{r['id']} {a_n} {a_s:.0f} {c} (pending) — avance par {bettor}")
            elif r["status"] == "lost":
                lines.append(f"  #{r['id']} {a_n} {a_s:.0f} {c} (perdu) — {ANNEXE_HANDLER} doit {a_s:.0f} a {bettor}")
            elif r["status"] == "won":
                profit = a_s * (r["odds"] - 1)
                lines.append(f"  #{r['id']} {a_n} {a_s:.0f} {c} (gagne) — {bettor} doit {profit:.0f} a {ANNEXE_HANDLER}")

    await update.message.reply_text("\n".join(lines))


# ── /pending ────────────────────────────────────────────────
async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    con = db()
    loro_filter = " AND (is_loro IS NULL OR is_loro = 0)" if is_duo(chat_id) else ""
    rows = con.execute(
        f"SELECT * FROM bets WHERE chat_id = ? AND status = 'pending'{loro_filter} ORDER BY id",
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
        a_s = (r["annexe_stake"] or 0) if not duo else 0
        trio_s = r["stake"] - a_s
        pp = "" if duo else f" ({trio_s/NB_PARTS:.0f}/pers.)"
        annexe_tag = f" [+{r['annexe_name']} {a_s:.0f}]" if a_s > 0 else ""
        lines.append(
            f"#{r['id']}{par} | {r['description']} @ {r['odds']:.2f} | "
            f"{r['stake']:.0f} {c}{annexe_tag}{pp}"
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
    bet_is_loro = bool(bet["is_loro"]) if bet["is_loro"] else False
    con.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    con.commit()
    con.close()
    tag = " [LORO]" if bet_is_loro else ""
    await update.message.reply_text(f"Pari #{bet_id} supprime ({bet['description']}){tag}.")

    if bet_is_loro:
        sheet_tab = "Loro"
    elif is_duo(chat_id):
        sheet_tab = "Kekko-Rapha"
    else:
        sheet_tab = "Paris"
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
            f"  /lock 500 Basel ML 2,10 @loro — pari Loro (CHF)\n"
            f"  /lock 500 Basel ML 2,10 -Rago 100 @loro — Loro + annexe\n"
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
            "  /soldeloro — solde + capital Loro (CHF)\n"
            "  /depotloro 2000 — ajouter capital au kiosque\n"
            "  /retraitloro 500 — retirer capital du kiosque\n"
            "  /delete <id> — supprimer un pari\n"
            "  /deletetx <id> — supprimer un remb/depense"
        )
    else:
        text = (
            "BET TRACKER\n\n"
            "Enregistrer un pari :\n"
            "  /lock 800 Strasbourg 1N2 3,10\n"
            "  /lock 500 Le Mans ML 1.70\n"
            "  /lock 7000 Real ML 1,50 -Julien 1000\n"
            "     → annexe : 1000 pour Julien, 6000 trio\n\n"
            "Resultat :\n"
            "  /win  (repondre au pari ou /win <id>)\n"
            "  /loss (repondre au pari ou /loss <id>)\n"
            "  /void (annule/rembourse)\n\n"
            "Transactions :\n"
            "  /remb Marco 100 a Kekko — transfert direct\n"
            "  /depense 935 bet365 depot @Kekko — avance partagee\n"
            "  /retrait 6000 bet365 @Kekko — retrait partage\n\n"
            "Stats :\n"
            "  /solde — P&L du groupe\n"
            "  /dettes — qui doit quoi a qui\n"
            "  /pending — paris en attente\n"
            "  /historique — 15 derniers paris\n"
            "  /stats — stats detaillees\n"
            "  /delete <id> — supprimer un pari\n"
            "  /deletetx <id> — supprimer un remb/depense"
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
    duo = is_duo(chat_id)
    parts = 2 if duo else NB_PARTS

    if not ctx.args:
        await update.message.reply_text(
            "Format : /depense <montant> <description> [@nom]\n"
            "Ex: /depense 80 restaurant\n"
            "Ex: /depense 935 bet365 depot @Kekko"
        )
        return

    # Parse: either "/depense 80 desc" or "/depense Rapha 50 desc"
    raw = " ".join(ctx.args)

    # Check for @name override (handles both text @name and Telegram mention entities)
    paid_by, raw = parse_name_override(raw, update)

    # Parse amount + description
    first = raw.split()[0] if raw else ""
    try:
        amount = float(first.replace(",", "."))
        description = raw[len(first):].strip() or "Depense partagee"
        if not paid_by:
            raw_name = update.message.from_user.first_name
            paid_by = NAME_MAP.get(raw_name, raw_name)
    except ValueError:
        if not paid_by:
            paid_by = first.capitalize()
        rest = raw[len(first):].strip()
        if not rest:
            await update.message.reply_text("Montant manquant.\nEx: /depense 80 restaurant")
            return
        amt_str = rest.split()[0]
        try:
            amount = float(amt_str.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Montant invalide.\nEx: /depense 80 restaurant")
            return
        description = rest[len(amt_str):].strip() or "Depense partagee"

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

    if duo:
        tc = get_duo_tricount(con, chat_id)
        balance_text = format_tricount_balance(tc, chat_id)
    else:
        balances = get_group_tricount(con, chat_id)
        balance_text = format_group_tricount(balances, chat_id)
    con.close()

    text = (
        f"Depense #{exp_id} enregistree\n"
        f"   {paid_by} a paye {amount:.0f} {c} ({description})\n"
        f"   Part de chacun : {amount/parts:.0f} {c}\n\n"
        f"Balance : {balance_text}"
    )
    await update.message.reply_text(text)

    sheet_tab = "Kekko-Rapha" if duo else "Paris"
    await sync_sheets({
        "action": "expense",
        "id": exp_id,
        "date": now[:10],
        "paid_by": paid_by,
        "amount": amount,
        "description": description,
        "sheet_tab": sheet_tab
    })


# ── /retrait — Retrait partagé (inverse de depense) ──────────
async def cmd_retrait(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    duo = is_duo(chat_id)
    parts = 2 if duo else NB_PARTS

    if not ctx.args:
        await update.message.reply_text(
            "Format : /retrait <montant> <description> [@nom]\n"
            "Ex: /retrait 6000 bet365 @Kekko\n"
            "Le montant est reparti : chacun recoit sa part."
        )
        return

    raw = " ".join(ctx.args)

    # Check for @name override (handles both text @name and Telegram mention entities)
    received_by, raw = parse_name_override(raw, update)
    if not received_by:
        raw_name = update.message.from_user.first_name
        received_by = NAME_MAP.get(raw_name, raw_name)

    # Parse amount + description
    first = raw.split()[0] if raw else ""
    try:
        amount = float(first.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Montant invalide.\nEx: /retrait 6000 bet365 @Kekko")
        return
    description = raw[len(first):].strip() or "Retrait"

    if amount <= 0:
        await update.message.reply_text("Montant invalide.")
        return

    now = datetime.now(timezone.utc).isoformat()
    c = cur(chat_id)

    # Store as negative expense: received_by got money FROM the group
    con = db()
    cur_ = con.execute(
        "INSERT INTO expenses (chat_id, paid_by, amount, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, received_by, -amount, f"[RETRAIT] {description}", now)
    )
    exp_id = cur_.lastrowid
    con.commit()

    if duo:
        tc = get_duo_tricount(con, chat_id)
        balance_text = format_tricount_balance(tc, chat_id)
    else:
        balances = get_group_tricount(con, chat_id)
        balance_text = format_group_tricount(balances, chat_id)
    con.close()

    text = (
        f"Retrait #{exp_id} enregistre\n"
        f"   {received_by} a recu {amount:.0f} {c} ({description})\n"
        f"   Part de chacun : {amount/parts:.0f} {c}\n\n"
        f"Balance : {balance_text}"
    )
    await update.message.reply_text(text)

    sheet_tab = "Kekko-Rapha" if duo else "Paris"
    await sync_sheets({
        "action": "expense",
        "id": exp_id,
        "date": now[:10],
        "paid_by": received_by,
        "amount": -amount,
        "description": f"[RETRAIT] {description}",
        "sheet_tab": sheet_tab
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

    stake = bet["stake"]
    odds = bet["odds"]
    annexe_s = bet["annexe_stake"] or 0
    annexe_n = bet["annexe_name"] or ""
    bet_is_loro = bool(bet["is_loro"]) if bet["is_loro"] else False

    if bet_is_loro:
        loro_s = stake - annexe_s
        pnl_duo = bet_pnl(loro_s, odds, status)
        pnl_pp = pnl_duo / 2
        if status == "won":
            result_text = f"GAGNE  +{pnl_pp:.0f}/pers."
        elif status == "lost":
            result_text = f"PERDU  {pnl_pp:.0f}/pers."
        else:
            result_text = "ANNULE"

        annexe_text = ""
        if annexe_s > 0 and status != "void":
            if status == "won":
                annexe_text = f"\n   Annexe ({annexe_n}) : Kekko doit {annexe_s * (odds - 1):.0f} CHF a {annexe_n}"
            elif status == "lost":
                annexe_text = f"\n   Annexe ({annexe_n}) : {annexe_n} doit {annexe_s:.0f} CHF a Kekko"

        loro_rows = con.execute(
            "SELECT status, stake, odds, annexe_stake FROM bets WHERE chat_id = ? AND is_loro = 1 AND status IN ('won','lost')",
            (chat_id,)
        ).fetchall()
        total_loro = sum(bet_pnl(r["stake"] - (r["annexe_stake"] or 0), r["odds"], r["status"]) / 2 for r in loro_rows)
        con.close()
        reply = (
            f"Pari #{bet['id']} : {result_text} [LORO]\n"
            f"   {bet['description']} @ {odds:.2f}"
            f"{annexe_text}\n\n"
            f"P&L cumule Loro : {total_loro:+.0f} CHF/pers."
        )
        await msg.reply_text(reply)
        await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status, "sheet_tab": "Loro"})
        return

    if is_duo(chat_id):
        pnl = bet_pnl(stake, odds, status)
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
            f"   {bet['description']} @ {odds:.2f}\n"
            f"   (par {bet['user_name']})\n\n"
            f"Balance : {debt_text}"
        )
    else:
        trio_s = stake - annexe_s
        trio_pnl = bet_pnl(trio_s, odds, status)
        if status == "won":
            result_text = f"GAGNE  {fmt(trio_pnl / NB_PARTS, chat_id)}/pers."
        elif status == "lost":
            result_text = f"PERDU  {fmt(trio_pnl / NB_PARTS, chat_id)}/pers."
        else:
            result_text = "ANNULE"

        rows = con.execute(
            "SELECT status, stake, odds, annexe_stake FROM bets WHERE chat_id = ? AND status IN ('won','lost') AND (is_loro IS NULL OR is_loro = 0)",
            (chat_id,)
        ).fetchall()
        total_pnl = sum(bet_pnl(r["stake"] - (r["annexe_stake"] or 0), r["odds"], r["status"]) / NB_PARTS for r in rows)
        con.close()

        annexe_text = ""
        if annexe_s > 0 and status != "void":
            c = cur(chat_id)
            bettor = bet["user_name"]
            if status == "won":
                annexe_profit = annexe_s * (odds - 1)
                annexe_text = f"\n   Annexe ({annexe_n}) : {bettor} doit {annexe_profit:.0f} {c} a {ANNEXE_HANDLER}"
            elif status == "lost":
                annexe_text = f"\n   Annexe ({annexe_n}) : avance de {annexe_s:.0f} {c} (inchangee)"

        reply = (
            f"Pari #{bet['id']} : {result_text}\n"
            f"   {bet['description']} @ {odds:.2f}"
            f"{annexe_text}\n\n"
            f"P&L cumule : {fmt(total_pnl, chat_id)}/pers."
        )

    await msg.reply_text(reply)

    sheet_tab = "Kekko-Rapha" if is_duo(chat_id) else "Paris"
    await sync_sheets({"action": "update_bet", "id": bet["id"], "status": status, "sheet_tab": sheet_tab})


# ── /sync — Pousser la DB vers Google Sheets ────────────────
async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not SHEETS_WEBHOOK_URL:
        await update.message.reply_text("SHEETS_WEBHOOK_URL non configure.")
        return

    duo = is_duo(chat_id)
    sheet_tab = "Kekko-Rapha" if duo else "Paris"
    sheet_id = SHEET_ID_DUO if duo else SHEET_ID_GROUP

    con = db()
    bets_rows = con.execute(
        "SELECT id, description, stake, odds, user_name, status, created_at, is_loro FROM bets WHERE chat_id = ? ORDER BY id",
        (chat_id,)
    ).fetchall()
    tx_rows = con.execute(
        "SELECT id, from_name, to_name, amount, description, created_at FROM transactions WHERE chat_id = ? ORDER BY id",
        (chat_id,)
    ).fetchall()
    exp_rows = con.execute(
        "SELECT id, paid_by, amount, description, created_at FROM expenses WHERE chat_id = ? ORDER BY id",
        (chat_id,)
    ).fetchall()
    con.close()

    bets = []
    loro_bets = []
    for r in bets_rows:
        entry = {
            "id": r[0], "description": r[1], "stake": r[2], "odds": r[3],
            "user_name": r[4], "status": r[5], "date": r[6]
        }
        if r[7]:  # is_loro
            loro_bets.append(entry)
        else:
            bets.append(entry)

    transactions = []
    for r in tx_rows:
        transactions.append({
            "id": r[0], "from_name": r[1], "to_name": r[2],
            "amount": r[3], "description": r[4], "date": r[5]
        })

    expenses = []
    for r in exp_rows:
        expenses.append({
            "id": r[0], "paid_by": r[1], "amount": r[2],
            "description": r[3], "date": r[4]
        })

    payload = {
        "action": "full_sync",
        "sheet_id": sheet_id,
        "sheet_tab": sheet_tab,
        "bets": bets,
        "transactions": transactions,
        "expenses": expenses
    }

    loro_info = f" + {len(loro_bets)} Loro" if loro_bets else ""
    await update.message.reply_text(f"Sync en cours... ({len(bets)} paris{loro_info}, {len(transactions)} tx, {len(expenses)} depenses)")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEETS_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    await update.message.reply_text(f"Erreur Sheets: HTTP {resp.status}")
                    return
                data = await resp.json(content_type=None)
    except Exception as e:
        await update.message.reply_text(f"Erreur: {e}")
        return

    if data.get("status") != "ok":
        await update.message.reply_text(f"Erreur: {data}")
        return

    synced = data.get("synced_bets", len(bets))
    msg_text = f"Sync OK ! {synced} paris synchronises."

    # Sync Loro bets separately
    if loro_bets:
        loro_payload = {
            "action": "full_sync",
            "sheet_id": SHEET_ID_DUO,
            "sheet_tab": "Loro",
            "bets": loro_bets,
            "transactions": [],
            "expenses": []
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(SHEETS_WEBHOOK_URL, json=loro_payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        msg_text += f" + {len(loro_bets)} Loro."
                    else:
                        msg_text += f" (Loro sync erreur: HTTP {resp.status})"
        except Exception as e:
            msg_text += f" (Loro sync erreur: {e})"

    await update.message.reply_text(msg_text)


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

# ── /sync — Resynchroniser toute la DB vers Google Sheets ──
async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not SHEETS_WEBHOOK_URL:
        await update.message.reply_text("SHEETS_WEBHOOK_URL non configure.")
        return

    duo = is_duo(chat_id)
    sheet_tab = "Kekko-Rapha" if duo else "Paris"
    sheet_id = SHEET_ID_DUO if duo else SHEET_ID_GROUP

    await update.message.reply_text("Synchronisation en cours...")

    con = db()
    bets = con.execute(
        "SELECT * FROM bets WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()
    txs = con.execute(
        "SELECT * FROM transactions WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()
    exps = con.execute(
        "SELECT * FROM expenses WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()
    con.close()

    bets_data = []
    for b in bets:
        bd = {
            "id": b["id"], "description": b["description"],
            "stake": b["stake"], "odds": b["odds"],
            "user_name": b["user_name"], "status": b["status"],
            "date": (b["created_at"] or "")[:10],
            "event_date": b["event_date"] if "event_date" in b.keys() else None,
        }
        if b["annexe_name"]:
            bd["annexe_name"] = b["annexe_name"]
            bd["annexe_stake"] = b["annexe_stake"] or 0
        bets_data.append(bd)

    tx_data = []
    for t in txs:
        tx_data.append({
            "id": t["id"], "from_name": t["from_name"],
            "to_name": t["to_name"], "amount": t["amount"],
            "description": t["description"],
            "date": (t["created_at"] or "")[:10],
        })
    for e in exps:
        tx_data.append({
            "id": e["id"], "from_name": e["paid_by"],
            "to_name": "DEPENSE", "amount": e["amount"],
            "description": e["description"],
            "date": (e["created_at"] or "")[:10],
        })

    payload = {
        "action": "full_sync",
        "sheet_tab": sheet_tab,
        "sheet_id": sheet_id,
        "bets": bets_data,
        "transactions": tx_data,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SHEETS_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    await update.message.reply_text(
                        f"Sync OK \u2014 {len(bets_data)} paris, {len(tx_data)} transactions envoy\u00e9s au Sheet."
                    )
                else:
                    await update.message.reply_text(f"Erreur Sheets: HTTP {resp.status}")
    except Exception as e:
        await update.message.reply_text(f"Erreur sync: {e}")


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
    app.add_handler(CommandHandler("soldeloro", cmd_solde_loro))
    app.add_handler(CommandHandler("depotloro", cmd_depot_loro))
    app.add_handler(CommandHandler("retraitloro", cmd_retrait_loro))
    app.add_handler(CommandHandler("historique", cmd_historique))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("dettes", cmd_dettes))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("deletetx", cmd_deletetx))
    app.add_handler(CommandHandler("remb", cmd_remb))
    app.add_handler(CommandHandler("depense", cmd_depense))
    app.add_handler(CommandHandler("retrait", cmd_retrait))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("restore", cmd_restore))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("sync", cmd_sync))

    app.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND,
        on_reply_result
    ))


    async def post_init(application):
        await application.bot.set_my_commands([
            ("lock", "Enregistrer un pari"),
            ("solde", "Voir le solde P&L"),
            ("soldeloro", "P&L paris Loro (CHF)"),
            ("dettes", "Voir qui doit quoi"),
            ("pending", "Paris en cours"),
            ("historique", "Derniers paris resolus"),
            ("stats", "Statistiques detaillees"),
            ("depense", "Depense partagee"),
            ("retrait", "Retrait partage"),
            ("remb", "Remboursement / transfert"),
            ("delete", "Supprimer un pari"),
            ("deletetx", "Supprimer un remb/depense"),
            ("sync", "Resync DB vers Google Sheets"),
            ("help", "Aide et commandes"),
        ])
    app.post_init = post_init
    log.info(f"Bot started (DUO_CHAT_ID={DUO_CHAT_ID})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
