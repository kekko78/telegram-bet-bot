# Telegram Bet Tracker Bot

Bot de suivi de paris pour un groupe de 3 personnes. Enregistre les paris, divise la mise par 3, track le P&L et les dettes.

## Setup

### 1. Créer le bot sur Telegram

1. Ouvrir Telegram, chercher **@BotFather**
2. Envoyer `/newbot`
3. Choisir un nom (ex: "Bet Tracker")
4. Choisir un username (ex: "notre_bet_tracker_bot")
5. Copier le **token** que BotFather te donne

### 2. Déployer sur Railway (gratuit)

1. Aller sur [railway.app](https://railway.app) et se connecter avec GitHub
2. **New Project** → **Deploy from GitHub repo**
3. Push ce dossier sur un repo GitHub (privé c'est ok)
4. Dans Railway, aller dans **Variables** et ajouter :
   - `BOT_TOKEN` = le token de BotFather
5. Railway va build et lancer le bot automatiquement

### Alternative : Render (gratuit aussi)

1. Aller sur [render.com](https://render.com)
2. **New** → **Background Worker**
3. Connecter le repo GitHub
4. Build command : `pip install -r requirements.txt`
5. Start command : `python bot.py`
6. Ajouter la variable d'environnement `BOT_TOKEN`

### 3. Ajouter le bot au groupe

1. Dans le groupe Telegram, ajouter le bot par son username
2. Rendre le bot **admin** (pour qu'il puisse lire les messages)
3. C'est prêt !

## Commandes

| Commande | Description |
|----------|-------------|
| `/lock 800 Strasbourg 1N2 3,10` | Enregistrer un pari |
| `/win` | Marquer comme gagné (répondre au pari ou `/win <id>`) |
| `/loss` | Marquer comme perdu |
| `/void` | Annuler/rembourser |
| `/solde` | P&L du groupe |
| `/dettes` | Qui doit quoi à qui |
| `/pending` | Paris en attente |
| `/historique` | 15 derniers paris |
| `/stats` | Statistiques détaillées |
| `/delete <id>` | Supprimer un pari |

## Format du /lock

```
/lock <mise> <description> <cote>
```

Exemples :
- `/lock 800 Strasbourg 1N2 3,10`
- `/lock 500 Le Mans ML 1.70`
- `/lock 200 Arsenal O2.5 buts 1,85`

La virgule et le point marchent pour la cote. "CHF" et "@" sont optionnels.
