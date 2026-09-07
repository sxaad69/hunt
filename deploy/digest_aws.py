import sqlite3, time
c = sqlite3.connect('/home/hunt/hunt/hunt/data/hunt.sqlite3')
c.row_factory = sqlite3.Row
cut = time.time() - 86400
tot = c.execute('SELECT COUNT(*) FROM paper_decisions WHERE decided_at>=?', (cut,)).fetchone()[0]
acc = c.execute("SELECT COUNT(*) FROM paper_decisions WHERE decided_at>=? AND decision='ACCEPT'", (cut,)).fetchone()[0]
print(f'SCAN {tot} | accepted {acc} ({acc/max(tot,1)*100:.1f}%)')
print('REJECT-HISTOGRAM:')
for r in c.execute("SELECT CASE WHEN reason LIKE 'dust%' THEN 'dust' WHEN reason LIKE 'mcap_ceiling%' THEN 'ceiling' WHEN reason LIKE 'top10%' THEN 'top10' WHEN reason LIKE 'sniper%' THEN 'sniper' WHEN reason LIKE 'serial%' THEN 'serial_rugger' ELSE substr(reason,1,12) END g, COUNT(*) c FROM paper_decisions WHERE decided_at>=? AND decision='REJECT' GROUP BY 1 ORDER BY c DESC LIMIT 7", (cut,)):
    print(' ', r['g'], r['c'])
print('EXITS:')
for r in c.execute("SELECT exit_reason, COUNT(*) c, ROUND(SUM(pnl_sol),4) tot FROM positions WHERE opened_ts>=? AND status='closed' GROUP BY 1 ORDER BY tot", (cut,)):
    print(' ', r['exit_reason'], 'x'+str(r['c']), f"{r['tot']:+.4f}")
print('BANDS:')
for r in c.execute("SELECT CASE WHEN entry_price_usd/104.0*1e9 < 200 THEN 'a<200' WHEN entry_price_usd/104.0*1e9 < 1000 THEN 'b200-1000' WHEN entry_price_usd/104.0*1e9 <= 3000 THEN 'c1000-3000' ELSE 'd>3000legacy' END band, COUNT(*) n, SUM(CASE WHEN status='closed' AND pnl_sol>0 THEN 1 ELSE 0 END) w, ROUND(SUM(CASE WHEN status='closed' THEN pnl_sol ELSE 0 END),4) p FROM positions WHERE opened_ts>=? GROUP BY 1 ORDER BY 1", (cut,)):
    print(' ', r['band'], 'taken', r['n'], 'wins', r['w'] or 0, 'pnl', r['p'] or 0)
tr = c.execute("SELECT symbol, ROUND(pnl_sol,4) pnl, ROUND(entry_price_usd/104.0*1e9,0) mc FROM positions WHERE opened_ts>=? AND status='closed' ORDER BY pnl_sol DESC LIMIT 1", (cut,)).fetchone()
wr = c.execute("SELECT symbol, ROUND(pnl_sol,4) pnl FROM positions WHERE opened_ts>=? AND status='closed' ORDER BY pnl_sol ASC LIMIT 1", (cut,)).fetchone()
if tr: print('TOP:', tr['symbol'], f"{tr['pnl']:+.4f}", 'entry-mcap', tr['mc'])
if wr: print('WORST:', wr['symbol'], f"{wr['pnl']:+.4f}")
w = c.execute("SELECT AVG(d.top10), AVG(d.holders), COUNT(*) FROM paper_decisions d JOIN positions p ON p.mint=d.mint WHERE d.decided_at>=? AND p.pnl_sol>0 AND d.top10 IS NOT NULL", (cut,)).fetchone()
l = c.execute("SELECT AVG(d.top10), AVG(d.holders), COUNT(*) FROM paper_decisions d JOIN positions p ON p.mint=d.mint WHERE d.decided_at>=? AND p.pnl_sol<=0 AND d.top10 IS NOT NULL", (cut,)).fetchone()
if (w[3] or 0) + (l[3] or 0) > 0:
    print(f'INTEL winners n={w[3]} top10avg={w[0] or -1:.0f} hold={w[1] or -1:.0f} | losers n={l[3]} top10avg={l[0] or -1:.0f} hold={l[1] or -1:.0f}')
else:
    nb = c.execute("SELECT COUNT(*) FROM paper_decisions WHERE top10 IS NOT NULL AND decided_at>=?", (cut,)).fetchone()[0]
    print(f'INTEL: {nb} fingerprints (split pending)')
sr = c.execute("SELECT COUNT(*) FROM paper_decisions WHERE reason LIKE 'mcap_ceiling%' AND decided_at>=?", (cut,)).fetchone()[0]
mb = c.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND tp_tier>=2 AND opened_ts>=?", (cut,)).fetchone()[0]
print(f'SPECIES-B: {sr} | moon-bags-alive: {mb}')
