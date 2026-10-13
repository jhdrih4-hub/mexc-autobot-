import queue
import threading
import time

import requests

from .utils import LOG, scrub


class TelegramNotifier:
    def __init__(self, enabled, token="", chat_id=""):
        self.enabled = bool(enabled and token and chat_id)
        self.token = token
        self.chat_id = chat_id
        self.api = f"https://api.telegram.org/bot{token}/sendMessage" if token else None
        self._q = queue.Queue(maxsize=30)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def send(self, text):
        if not self.enabled:
            return
        try:
            self._q.put_nowait(scrub(text))
        except queue.Full:
            pass  # drop-oldest policy: never block trading on notifications

    def _worker(self):
        last = 0.0
        while True:
            text = self._q.get()
            delay = max(0.0, 1.2 - (time.time() - last))
            if delay:
                time.sleep(delay)
            for _ in range(3):
                try:
                    r = requests.post(self.api,
                                      json={"chat_id": self.chat_id, "text": text},
                                      timeout=10)
                    if r.status_code == 200:
                        last = time.time()
                        break
                    if r.status_code == 429:
                        time.sleep(3)
                        continue
                    LOG.warning("telegram http %s", r.status_code)
                    break
                except Exception as e:
                    LOG.warning("telegram send failed: %s", e)
                    time.sleep(2)

    # ---- formatted events
    def order_filled(self, side, symbol, qty, price, fee, strategy):
        self.send(f"FILLED {side} {symbol}\nqty {qty:.6f} @ {price:.6f}\n"
                  f"fee {fee:.4f} USDT\nstrategy: {strategy}")

    def position_closed(self, symbol, tr):
        emoji = "+" if tr.net_pnl >= 0 else "-"
        self.send(f"CLOSED {symbol} ({tr.close_reason})\n"
                  f"net PnL {emoji}{abs(tr.net_pnl):.2f} USDT (fees {tr.fees:.4f})")

    def started(self, lines):
        self.send("BOT STARTED\n" + "\n".join(lines))

    def stopped(self, lines):
        self.send("BOT STOPPED\n" + "\n".join(lines))

    def daily(self, lines):
        self.send("DAILY REPORT\n" + "\n".join(lines))
