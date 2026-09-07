// ═══════════════════════════════════════════════════════════════
// LORO TAB HANDLING — Add to your existing Code.gs
// ═══════════════════════════════════════════════════════════════
//
// The Loro tab uses the same structure as the Kekko-Rapha (duo) tab:
//   Columns: A=ID, B=Date, C=Par, D=Description, E=Mise, F=Cote, G=Statut, H=P&L
//
// The existing handlers (new_bet, update_bet, delete_bet, full_sync)
// already route by sheet_tab. You just need to make sure:
//
// 1. The "Loro" tab exists in the Kekko-Rapha spreadsheet
//    (same SHEET_ID_DUO = "1oLodmWlhKfoSdcmgWeR42bcrCh_7YJUBJ9jMMps5EgU")
//
// 2. Create the tab with headers:
//    A1=ID, B1=Date, C1=Par, D1=Description, E1=Mise, F1=Cote, G1=Statut, H1=P&L
//
// 3. In your doPost(e), the existing routing should already work because
//    the bot sends sheet_tab="Loro" and the Code.gs uses ss.getSheetByName(sheetTab).
//
// The full_sync handler treats Loro like duo mode (isDuo check uses sheetTab === "Kekko-Rapha"),
// so you need to update the isDuo check:

// CHANGE THIS LINE in handleFullSync:
//   var isDuo = (sheetTab === "Kekko-Rapha");
// TO:
//   var isDuo = (sheetTab === "Kekko-Rapha" || sheetTab === "Loro");

// That's it — Loro uses the same duo column layout (ID, Date, Par, Description, Mise, Cote, Statut, P&L)
// and the P&L is calculated the same way (no NB_PARTS division since it's 50/50 between the two).
//
// The Loro P&L is NOT divided by NB_PARTS in full_sync because isDuo=true skips the /NB_PARTS logic.
// This is correct: Loro bets are 50/50 between Kekko and Rapha, and the raw P&L in the sheet
// represents the total P&L of the bet. The /pers. split is done in the bot display only.
