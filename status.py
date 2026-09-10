import sqlite3
c = sqlite3.connect("/home/hunt/hunt/hunt/data/hunt.sqlite3")
print("decisions:", c.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()[0])
print("open:", c.execute("SELECT COUNT(*) FROM positions WHERE status=:s", {"s": "open"}).fetchone()[0])
closed = c.execute("SELECT COUNT(*), ROUND(COALESCE(SUM(pnl_sol),0),4) FROM positions WHERE status=:s", {"s": "closed"}).fetchone()
print("closed:", closed[0], "| pnl:", closed[1], "SOL")
