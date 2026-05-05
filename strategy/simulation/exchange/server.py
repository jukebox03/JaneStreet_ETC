import json
import socket
import threading
import time
from queue import Empty, Queue
from typing import List, Optional

from .config import (
    MARKET_TICK_INTERVAL, ROUND_DURATION, INTER_ROUND_PAUSE, SYMBOLS,
)
from .exchange import Exchange
from .market import MarketEngine
from .marketplace import MarketplaceBot


class ClientHandler:
    def __init__(self, conn: socket.socket, addr, exchange: Exchange):
        self.conn = conn
        self.addr = addr
        self.exchange = exchange
        self.team: Optional[str] = None
        self.send_queue: "Queue[Optional[dict]]" = Queue()
        self.running = True
        self._reader = conn.makefile("r", buffering=1, encoding="utf-8")
        self._write_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def _read_loop(self):
        try:
            while self.running:
                line = self._reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._send({"type": "error", "error": "invalid json"})
                    continue

                if self.team is None:
                    if msg.get("type") != "hello":
                        self._send({"type": "error", "error": "must hello first"})
                        continue
                    team = str(msg.get("team", "")).strip().upper()
                    if not team:
                        self._send({"type": "error", "error": "empty team name"})
                        continue
                    if team.startswith("_MP"):
                        self._send({"type": "error", "error": "reserved team name"})
                        continue
                    self.team = team
                    self.exchange.register_team(team, self._send)
                    self.exchange.send_hello(team)
                    print(f"[server] team connected: {team} from {self.addr}")
                else:
                    self.exchange.handle_message(self.team, msg)
        except (ConnectionResetError, OSError):
            pass
        except Exception as e:
            print(f"[server] client read error for {self.addr}: {e}")
        finally:
            self._close()

    def _write_loop(self):
        try:
            while self.running:
                try:
                    msg = self.send_queue.get(timeout=0.5)
                except Empty:
                    continue
                if msg is None:
                    break
                data = (json.dumps(msg) + "\n").encode("utf-8")
                try:
                    with self._write_lock:
                        self.conn.sendall(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        except Exception as e:
            print(f"[server] client write error for {self.addr}: {e}")
        finally:
            self._close()

    def _send(self, msg: dict):
        if self.running:
            self.send_queue.put(msg)

    def _close(self):
        if not self.running:
            return
        self.running = False
        try:
            self.send_queue.put(None)
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        if self.team:
            print(f"[server] team disconnected: {self.team}")


class Server:
    def __init__(self, host: str = "0.0.0.0", port: int = 25000,
                 activity: float = 1.0, round_duration: float = ROUND_DURATION,
                 inter_round_pause: float = INTER_ROUND_PAUSE,
                 max_rounds: Optional[int] = None,
                 print_interval: Optional[float] = None,
                 seed: Optional[int] = None, verbose: bool = False):
        self.host = host
        self.port = port
        self.activity = activity
        self.round_duration = round_duration
        self.inter_round_pause = inter_round_pause
        self.max_rounds = max_rounds
        self.print_interval = print_interval
        self.verbose = verbose
        self.market = MarketEngine(seed=seed, verbose=verbose)
        self.exchange = Exchange(verbose=verbose)
        self.marketplace = MarketplaceBot(self.exchange, self.market,
                                          activity=activity, seed=seed)
        self.running = False
        self.clients: List[ClientHandler] = []
        self._server_sock: Optional[socket.socket] = None

    def start(self):
        self.running = True
        threading.Thread(target=self._market_tick_loop, daemon=True).start()
        threading.Thread(target=self._round_loop, daemon=True).start()
        threading.Thread(target=self._pnl_loop, daemon=True).start()
        if self.print_interval and self.print_interval > 0:
            threading.Thread(target=self._market_print_loop, daemon=True).start()
        self.marketplace.start()
        self._accept_loop()

    def stop(self):
        self.running = False
        if self._server_sock:
            try:
                self._server_sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._server_sock.close()
            except Exception:
                pass

    def _accept_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(32)
        self._server_sock = s
        mode = "prod-like" if self.activity >= 1.0 else ("empty" if self.activity <= 0 else "slower")
        print(f"[server] listening on {self.host}:{self.port} "
              f"(activity={self.activity:.1f} [{mode}], round={self.round_duration:.0f}s)")
        print(f"[server] connect your bot with: --specific-address localhost:{self.port}")
        try:
            while self.running:
                try:
                    conn, addr = s.accept()
                except OSError:
                    break
                handler = ClientHandler(conn, addr, self.exchange)
                handler.start()
                self.clients.append(handler)
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _market_tick_loop(self):
        while self.running:
            try:
                self.market.tick()
            except Exception as e:
                print(f"[server] market tick error: {e}")
            time.sleep(MARKET_TICK_INTERVAL)

    def _round_loop(self):
        round_num = 0
        while self.running:
            round_num += 1
            elapsed = 0.0
            while self.running and elapsed < self.round_duration:
                time.sleep(1.0)
                elapsed += 1.0
            if not self.running:
                return
            print(f"[server] round {round_num} ending")
            self._print_pnl("final")
            self.exchange.close_round()
            if self.max_rounds is not None and round_num >= self.max_rounds:
                print(f"[server] reached max_rounds={self.max_rounds}, shutting down")
                self.stop()
                return
            time.sleep(self.inter_round_pause)
            print(f"[server] new round starting")
            self.market.reset()
            self.marketplace.reset()
            self.exchange.reset_round()

    def _pnl_loop(self):
        while self.running:
            time.sleep(30)
            if self.running:
                self._print_pnl("update")

    def _market_print_loop(self):
        while self.running:
            time.sleep(self.print_interval)
            if not self.running:
                return
            prices = ", ".join(
                f"{s}={self.market.state.get_fair(s):.0f}" for s in SYMBOLS
            )
            print(f"[market] {prices}")

    def _print_pnl(self, label: str):
        snapshot = self.exchange.pnl_snapshot(self.market.state.get_fair)
        if not snapshot:
            return
        print(f"[pnl/{label}] fair prices: " + ", ".join(
            f"{s}={self.market.state.get_fair(s):.0f}" for s in SYMBOLS
        ))
        for team, info in snapshot.items():
            pos_str = " ".join(f"{s}={p:+d}" for s, p in info["positions"].items() if p != 0)
            print(f"[pnl/{label}] {team}: P&L={info['pnl']:+,.0f} cash={info['cash']:+,d} {pos_str}")
