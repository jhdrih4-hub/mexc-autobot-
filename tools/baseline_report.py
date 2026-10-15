# F0 BASELINE — SPOT/SWAP ayrı performans raporu (yalnız-okuma)
# Koşum: python tools/baseline_report.py   (repo kökünde)
import sqlite3, sys

c = sqlite3.connect("trading.db")
def rows(q, *p):
    return c.execute(q, p).fetchall()

print("=" * 62)
print("F0 BASELINE RAPORU")
print("=" * 62)

for mkt in ("spot", "swap"):
    print(f"\n--- {mkt.upper()} ---")
    j = rows("""SELECT COUNT(*), SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END),
                ROUND(SUM(net_pnl),2), ROUND(AVG(net_pnl),2),
                ROUND(SUM(fees),2), ROUND(AVG(duration_s)/60.0,1),
                MIN(entry_ts), MAX(exit_ts)
             FROM trade_journal WHERE market_type=?""", (mkt,))
    if not j or j[0][0] == 0:
        print("  veri yok"); continue
    n, wins, pnl, avg_pnl, fees, dur_min, t0, t1 = j[0]
    losses = n - (wins or 0)
    wins = wins or 0
    gross_w = rows("""SELECT ROUND(SUM(net_pnl),2) FROM trade_journal
                      WHERE market_type=? AND net_pnl>0""", (mkt,))[0][0] or 0.0
    gross_l = abs(rows("""SELECT ROUND(SUM(net_pnl),2) FROM trade_journal
                          WHERE market_type=? AND net_pnl<=0""", (mkt,))[0][0] or 0.0)
    pf = round(gross_w / gross_l, 2) if gross_l > 0 else float("inf")
    print(f"  İşlem sayısı      : {n}")
    print(f"  Kazanan/Kaybeden  : {wins}/{losses}  (win-rate {100*wins/max(n,1):.0f}%)")
    print(f"  Net PnL           : {pnl} USDT")
    print(f"  İşlem başına ort. : {avg_pnl} USDT")
    print(f"  Toplam ücret      : {fees} USDT")
    print(f"  Profit Factor     : {pf}")
    print(f"  Ort. süre         : {dur_min} dk")
    print(f"  İlk/son işlem     : {t0} → {t1}")

print("\n--- GENEL ---")
tot = rows("SELECT ROUND(SUM(net_pnl),2), COUNT(*) FROM trade_journal")[0]
print(f"  Journal toplam    : {tot[1]} işlem | net {tot[0]} USDT")
print(f"  Not: unrealized (açık pozisyonlar) bu raporda YOK — log SWAP DURUM'a bakınız")
