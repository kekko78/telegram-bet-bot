// ═══════════════════════════════════════════════════════════════
// ADD THIS TO YOUR EXISTING Code.gs — paste inside the doPost(e)
// function's switch/if block, as a new case for action "full_sync"
// ═══════════════════════════════════════════════════════════════
//
// In your doPost(e), add:
//
//   if (action === "full_sync") {
//     return handleFullSync(data);
//   }
//
// Then paste the function below OUTSIDE doPost:

function handleFullSync(data) {
  var ss = SpreadsheetApp.openById(data.sheet_id);
  var sheetTab = data.sheet_tab;  // "Paris" or "Kekko-Rapha"
  var bets = data.bets || [];
  var transactions = data.transactions || [];
  var expenses = data.expenses || [];
  var isDuo = (sheetTab === "Kekko-Rapha");
  var NB_PARTS = 3;

  // ── 1. Rebuild Paris / Kekko-Rapha tab ──────────────────────
  var parisSheet = ss.getSheetByName(sheetTab);
  if (parisSheet) {
    var lastRow = parisSheet.getLastRow();
    if (lastRow > 1) {
      parisSheet.getRange(2, 1, lastRow - 1, parisSheet.getMaxColumns()).clearContent();
    }

    var cumulPnl = 0;
    for (var i = 0; i < bets.length; i++) {
      var b = bets[i];
      var row = i + 2;
      var stake = b.stake || 0;
      var odds = b.odds || 0;
      var status = (b.status || "pending").toUpperCase();

      var pnl = "";
      var pnlPers = "";
      if (status === "WON") {
        pnl = stake * (odds - 1);
        pnlPers = pnl / NB_PARTS;
        cumulPnl += pnl;
      } else if (status === "LOST") {
        pnl = -stake;
        pnlPers = pnl / NB_PARTS;
        cumulPnl += pnl;
      }

      if (isDuo) {
        // Duo columns: ID, Date, Par, Description, Mise, Cote, Statut, P&L
        parisSheet.getRange(row, 1).setValue(b.id);
        parisSheet.getRange(row, 2).setValue(b.date || "");
        parisSheet.getRange(row, 3).setValue(b.user_name || "");
        parisSheet.getRange(row, 4).setValue(b.description || "");
        parisSheet.getRange(row, 5).setValue(stake);
        parisSheet.getRange(row, 6).setValue(odds);
        parisSheet.getRange(row, 7).setValue(status);
        if (pnl !== "") parisSheet.getRange(row, 8).setValue(pnl);
      } else {
        // Group columns: A=ID, B=Date, C=Description, D=Mise, E=Mise/pers, F=Cote, G=Par, H=Statut, I=P&L, J=P&L/pers, K=Cumul P&L, L=Cumul P&L/pers
        parisSheet.getRange(row, 1).setValue(b.id);
        parisSheet.getRange(row, 2).setValue(b.date || "");
        parisSheet.getRange(row, 3).setValue(b.description || "");
        parisSheet.getRange(row, 4).setValue(stake);
        parisSheet.getRange(row, 5).setValue(Math.round(stake / NB_PARTS * 100) / 100);
        parisSheet.getRange(row, 6).setValue(odds);
        parisSheet.getRange(row, 7).setValue(b.user_name || "");
        parisSheet.getRange(row, 8).setValue(status);
        if (pnl !== "") {
          parisSheet.getRange(row, 9).setValue(pnl);
          parisSheet.getRange(row, 10).setValue(Math.round(pnlPers));
        }
        parisSheet.getRange(row, 11).setValue(cumulPnl);
        parisSheet.getRange(row, 12).setValue(Math.round(cumulPnl / NB_PARTS));
      }
    }
  }

  // ── 2. Rebuild Transactions tab ─────────────────────────────
  var txSheet = ss.getSheetByName("Transactions");
  if (txSheet) {
    var lastRowTx = txSheet.getLastRow();
    if (lastRowTx > 1) {
      txSheet.getRange(2, 1, lastRowTx - 1, txSheet.getMaxColumns()).clearContent();
    }

    var allTx = [];
    for (var j = 0; j < transactions.length; j++) {
      var t = transactions[j];
      allTx.push({id: t.id, date: t.date, from: t.from_name, to: t.to_name, amount: t.amount, desc: t.description});
    }
    for (var k = 0; k < expenses.length; k++) {
      var e = expenses[k];
      allTx.push({id: e.id, date: e.date, from: e.paid_by, to: "DEPENSE", amount: e.amount, desc: e.description});
    }
    allTx.sort(function(a, b) { return a.id - b.id; });

    for (var m = 0; m < allTx.length; m++) {
      var tx = allTx[m];
      var txRow = m + 2;
      // Columns: A=ID, B=Date, C=De, D=Vers, E=Montant, F=Description
      txSheet.getRange(txRow, 1).setValue(tx.id);
      txSheet.getRange(txRow, 2).setValue(tx.date || "");
      txSheet.getRange(txRow, 3).setValue(tx.from);
      txSheet.getRange(txRow, 4).setValue(tx.to);
      txSheet.getRange(txRow, 5).setValue(tx.amount);
      txSheet.getRange(txRow, 6).setValue(tx.desc || "");
    }
  }

  // ── 3. Recalculate Dettes tab ───────────────────────────────
  if (!isDuo) {
    var detteSheet = ss.getSheetByName("Dettes");
    if (detteSheet) {
      var fronted = {};
      var collected = {};
      var totalCost = 0, totalReturns = 0;
      var nbPending = 0, nbTotal = bets.length;

      for (var p = 0; p < bets.length; p++) {
        var bet = bets[p];
        var uname = bet.user_name || "Unknown";
        var st = (bet.status || "pending").toLowerCase();
        fronted[uname] = (fronted[uname] || 0) + bet.stake;
        totalCost += bet.stake;
        if (st === "won") {
          var payout = bet.stake * bet.odds;
          collected[uname] = (collected[uname] || 0) + payout;
          totalReturns += payout;
        }
        if (st === "pending") nbPending++;
      }

      // Transaction net
      var txNet = {};
      for (var q = 0; q < transactions.length; q++) {
        var tr = transactions[q];
        txNet[tr.from_name] = (txNet[tr.from_name] || 0) + tr.amount;
        txNet[tr.to_name] = (txNet[tr.to_name] || 0) - tr.amount;
      }

      // All participant names
      var allNames = {};
      for (var key in fronted) allNames[key] = true;
      for (var key in collected) allNames[key] = true;
      for (var key in txNet) allNames[key] = true;
      var names = Object.keys(allNames).sort();

      var fair = (totalReturns - totalCost) / NB_PARTS;

      var balances = {};
      for (var n = 0; n < names.length; n++) {
        var name = names[n];
        var f = fronted[name] || 0;
        var c = collected[name] || 0;
        var physical = c - f;
        balances[name] = fair - physical + (txNet[name] || 0);
      }

      // Settled P&L per person
      var settledPnl = 0;
      for (var s = 0; s < bets.length; s++) {
        var sb = bets[s];
        var sst = (sb.status || "").toLowerCase();
        if (sst === "won") settledPnl += sb.stake * (sb.odds - 1);
        else if (sst === "lost") settledPnl -= sb.stake;
      }
      var pnlPerPers = Math.round(settledPnl / NB_PARTS);

      // Write Dettes tab
      var now = Utilities.formatDate(new Date(), "Europe/Zurich", "dd/MM/yyyy HH:mm");
      detteSheet.getRange("A2").setValue("Groupe Suisse • Derniere MAJ: " + now);

      // Clear data rows (5-7)
      detteSheet.getRange(5, 1, 3, 6).clearContent();

      for (var r = 0; r < names.length; r++) {
        var nm = names[r];
        var dRow = 5 + r;
        var avance = fronted[nm] || 0;
        var pnlP = (avance > 0) ? pnlPerPers : 0;
        var txN = txNet[nm] || 0;
        var bal = Math.round(balances[nm]);

        detteSheet.getRange(dRow, 1).setValue(nm);           // Joueur
        detteSheet.getRange(dRow, 2).setValue(avance);        // Avance
        detteSheet.getRange(dRow, 3).setValue(pnlP);          // P&L/pers
        detteSheet.getRange(dRow, 4).setValue(txN);           // Transactions
        detteSheet.getRange(dRow, 5).setValue(bal);           // Balance
        if (bal > 0) {
          detteSheet.getRange(dRow, 6).setValue("On lui doit " + Math.abs(bal) + " CHF");
        } else if (bal < 0) {
          detteSheet.getRange(dRow, 6).setValue("Doit " + Math.abs(bal) + " CHF au groupe");
        } else {
          detteSheet.getRange(dRow, 6).setValue("A jour");
        }
      }

      // Summary row 9
      var debtors = [];
      for (var nm2 in balances) {
        if (balances[nm2] < -0.5) debtors.push(nm2 + " doit " + Math.abs(Math.round(balances[nm2])) + " CHF");
      }
      detteSheet.getRange(9, 1).setValue(debtors.join(" | ") || "Tout le monde est a jour");

      // Stats row 10
      var statsText = nbTotal + " paris au total  •  " + nbPending + " en attente  •  Mise totale: " + Math.round(totalCost) + " CHF";
      detteSheet.getRange(10, 1).setValue(statsText);
    }
  }

  return ContentService.createTextOutput(JSON.stringify({status: "ok"}))
    .setMimeType(ContentService.MimeType.JSON);
}
