"""
ETH Breakout 4h – alerte pe Telegram (rulează pe GitHub Actions)

Strategia validată în backtest (ETH 2017–2023), doar LONG:
  semnal la închiderea unei lumânări 4h dacă:
    1) close > maximul HIGH-urilor celor 20 de lumânări anterioare
    2) close-ul anterior <= maximul celor 20 de lumânări dinaintea lui (prima spargere)
    3) close > EMA200
  SL = intrare − 3 × ATR14 (Wilder) · TP = intrare + 3R · ieșire forțată după 7 zile

Ce trimite pe Telegram:
  🟢 semnal (intrare, SL, TP, mărime pentru riscul ales)
  ⏰ reminder cu 1 oră înainte și la ora exactă a ieșirii pe timp (7 zile)
  ✅/🔴 aviz când prețul atinge TP sau SL de la semnal

Configurare (variabile de mediu sau direct mai jos):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  – secretele din GitHub
  RISK_USD                          – risc pe trade (implicit 5)

Rulare:
  python3 ethbot.py          # buclă continuă (always-on task)
  python3 ethbot.py --once   # o singură verificare (task programat la fiecare oră)
  python3 ethbot.py --test   # trimite un mesaj de test pe Telegram
"""
import json, os, sys, time
from datetime import datetime, timedelta, timezone

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "PUNE_AICI_TOKENUL")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUNE_AICI_CHAT_ID")
RISK_USD = float(os.environ.get("RISK_USD", "5"))
SYMBOL = "ETHUSDT"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eth_breakout_state.json")

LOOKBACK, ATR_LEN, EMA_LEN, ATR_MULT, TP_R = 20, 14, 200, 3.0, 3.0
HOLD = timedelta(days=7)

KLINE_URLS = [
    "https://fapi.binance.com/fapi/v1/klines",          # futures (ca pe Binance Futures)
    "https://api.binance.com/api/v3/klines",            # spot
    "https://data-api.binance.vision/api/v3/klines",    # spot, oglindă publică
]

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bucharest")
except Exception:  # Python vechi
    TZ = timezone(timedelta(hours=2))


# ---------------------------------------------------------------- date
SOURCE_USED = None
H4 = 4 * 3600 * 1000


def _split(rows):
    now_ms = int(time.time() * 1000)
    closed = [x for x in rows if x["t"] + H4 <= now_ms]
    live = rows[-1] if rows and rows[-1]["t"] + H4 > now_ms else None
    return closed, live


def _binance(url, limit):
    r = requests.get(url, params={"symbol": SYMBOL, "interval": "4h", "limit": limit}, timeout=20)
    r.raise_for_status()
    return [dict(t=int(k[0]), o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4])) for k in r.json()]


def _bitfinex(limit):
    # format Bitfinex: [MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]; sort=1 = vechi -> nou
    r = requests.get("https://api-pub.bitfinex.com/v2/candles/trade:4h:tETHUSD/hist",
                     params={"limit": limit, "sort": 1}, timeout=20)
    r.raise_for_status()
    return [dict(t=int(k[0]), o=float(k[1]), h=float(k[3]), l=float(k[4]), c=float(k[2])) for k in r.json()]


def _kraken(limit):
    r = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": "ETHUSD", "interval": 240}, timeout=20)
    r.raise_for_status()
    res = r.json()["result"]
    key = [k for k in res if k != "last"][0]
    return [dict(t=int(k[0]) * 1000, o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4])) for k in res[key]][-limit:]


def fetch_klines(limit=1000):
    """Lumânări 4h ÎNCHISE + lumânarea în curs. Binance întâi; dacă e blocat (ex. servere din SUA), Bitfinex/Kraken."""
    global SOURCE_USED
    sources = [(u, (lambda u=u: _binance(u, limit))) for u in KLINE_URLS]
    sources += [("bitfinex", lambda: _bitfinex(limit)), ("kraken", lambda: _kraken(limit))]
    errors = []
    for name, fn in sources:
        try:
            closed, live = _split(fn())
            fresh = closed and (time.time() * 1000 - closed[-1]["t"]) < 3 * H4
            if len(closed) > EMA_LEN + LOOKBACK and fresh:
                SOURCE_USED = name
                return closed, live
            errors.append(f"{name}: date insuficiente/vechi")
        except Exception as e:  # încearcă următoarea sursă
            errors.append(f"{name}: {e}")
    raise RuntimeError("Nu am putut lua lumânările: " + " | ".join(errors))


# ---------------------------------------------------------------- calcule
def ema(values, n):
    a = 2 / (n + 1)
    out, e = [], values[0]
    for v in values:
        e = a * v + (1 - a) * e
        out.append(e)
    return out


def atr_wilder(rows, n=ATR_LEN):
    out, a = [], None
    for i, r in enumerate(rows):
        tr = r["h"] - r["l"] if i == 0 else max(r["h"] - r["l"], abs(r["h"] - rows[i - 1]["c"]), abs(r["l"] - rows[i - 1]["c"]))
        a = tr if a is None else a + (tr - a) / n
        out.append(a)
    return out


def signal_on_last(rows):
    """Verifică semnalul pe ULTIMA lumânare închisă. Întoarce dict sau None."""
    if len(rows) < LOOKBACK + 3:
        return None
    closes = [r["c"] for r in rows]
    e200 = ema(closes, EMA_LEN)
    atr = atr_wilder(rows)
    i = len(rows) - 1
    hh = max(r["h"] for r in rows[i - LOOKBACK:i])
    hh_prev = max(r["h"] for r in rows[i - 1 - LOOKBACK:i - 1])
    if closes[i] > hh and closes[i - 1] <= hh_prev and closes[i] > e200[i]:
        return dict(candle_t=rows[i]["t"], close=closes[i], level=hh, ema200=e200[i], atr=atr[i])
    return None


# ---------------------------------------------------------------- telegram & stare
def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text}, timeout=20)
    r.raise_for_status()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_signal_candle": 0, "active": None}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def fmt_local(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


# ---------------------------------------------------------------- logică
def check_once():
    state = load_state()
    rows, live = fetch_klines()
    hb = datetime.now(timezone.utc).strftime("%Y-%m")  # o modificare pe lună -> GitHub nu oprește botul
    if state.get("heartbeat") != hb:
        state["heartbeat"] = hb
        save_state(state)
    price = live["c"] if live else rows[-1]["c"]
    now = time.time()

    # 1) poziția activă: TP/SL atinse? reminder 7 zile?
    act = state.get("active")
    if act:
        hi = max([r["h"] for r in rows if r["t"] >= act["start_candle"]] + ([live["h"]] if live else []))
        lo = min([r["l"] for r in rows if r["t"] >= act["start_candle"]] + ([live["l"]] if live else []))
        if hi >= act["tp"]:
            send(f"✅ ETH a atins TP-ul ({act['tp']:.2f}). Dacă ordinul a fost pus, poziția s-a închis pe profit. Trece rezultatul în jurnal.")
            state["active"] = None
        elif lo <= act["sl"]:
            send(f"🔴 ETH a atins SL-ul ({act['sl']:.2f}). Dacă ordinul a fost pus, poziția s-a închis. Trece rezultatul în jurnal.")
            state["active"] = None
        else:
            if not act.get("warned") and now >= act["exit_ts"] - 3600:
                send(f"⏰ Peste ~1 oră ({fmt_local(act['exit_ts'])}) se împlinesc 7 zile de la semnal. Pregătește închiderea la piață.")
                act["warned"] = True
            if now >= act["exit_ts"]:
                send(f"⏰ Au trecut 7 zile – închide acum poziția ETH la piață (preț ~{price:.2f}). Trece rezultatul în jurnal.")
                state["active"] = None
        save_state(state)

    # 2) semnal nou pe ultima lumânare închisă
    sig = signal_on_last(rows)
    if sig and sig["candle_t"] > state.get("last_signal_candle", 0):
        state["last_signal_candle"] = sig["candle_t"]
        entry = price
        sl = entry - ATR_MULT * sig["atr"]
        tp = entry + TP_R * (entry - sl)
        sl_pct = (entry - sl) / entry * 100
        pos_usd = RISK_USD / (sl_pct / 100)
        exit_ts = now + HOLD.total_seconds()
        candle_close = (sig["candle_t"] / 1000) + 4 * 3600
        if state.get("active"):
            send(f"ℹ️ Semnal nou ETH (breakout la {sig['close']:.2f}), dar ai deja o poziție deschisă – regula e o singură poziție o dată, nu intri.")
        else:
            send("\n".join([
                "🟢 SEMNAL ETH LONG – breakout 4h",
                f"Lumânarea închisă la {fmt_local(candle_close)} a spart maximul pe 20 de lumânări ({sig['level']:.2f}) la {sig['close']:.2f}, peste EMA200 ({sig['ema200']:.2f}).",
                f"Intrare: ~{entry:.2f}",
                f"SL: {sl:.2f} (−{sl_pct:.1f}%)   [ATR {sig['atr']:.2f}]",
                f"TP: {tp:.2f} (+{TP_R * sl_pct:.1f}%)",
                f"Mărime pentru risc {RISK_USD:.0f} $: ~{pos_usd:.0f} USDC ≈ {pos_usd / entry:.3f} ETH. Pune SL și TP pe ordin.",
                f"Dacă nu atinge SL/TP până {fmt_local(exit_ts)}, închizi la piață (primești reminder).",
                "Nu e consultanță financiară.",
            ]))
            state["active"] = dict(start_candle=sig["candle_t"] + 4 * 3600 * 1000, entry=entry, sl=sl, tp=tp,
                                   exit_ts=exit_ts, warned=False)
        save_state(state)
    return price, sig


def main():
    if "--test" in sys.argv:
        rows, live = fetch_klines()
        send(f"🔧 Test bot ETH breakout: conexiune OK (sursă: {SOURCE_USED}). ETH ~{(live or rows[-1])['c']:.2f}, "
             f"maxim 20 lumânări {max(r['h'] for r in rows[-20:]):.2f}, EMA200 {ema([r['c'] for r in rows], EMA_LEN)[-1]:.2f}.")
        return
    if "--once" in sys.argv:
        check_once()
        return
    while True:
        try:
            check_once()
        except Exception as e:
            print(datetime.now(), "eroare:", e, flush=True)
        time.sleep(300)  # verifică la fiecare 5 minute


if __name__ == "__main__":
    main()
