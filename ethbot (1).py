"""
ETH Breakout 4h – alerte pe Telegram (rulează pe GitHub Actions)

Strategii validate în backtest (2017–2021, verificate pe 2022–2023), semnal la închiderea unei lumânări 4h:
  LONG:  close > maximul celor 20 de lumânări anterioare (prima spargere) și close > EMA200
         SL = intrare − 3 × ATR14 · TP = intrare + 3R · ieșire forțată după 7 zile
  SHORT: close < minimul celor 20 de lumânări anterioare (prima spargere), close < EMA200
         și volumul lumânării > 1.5 × media volumului ultimelor 20 de lumânări
         SL = intrare + 2 × ATR14 · TP = intrare − 3R · ieșire forțată după 7 zile
  O singură poziție o dată.

Ce trimite pe Telegram:
  🟢 semnal LONG / 🔻 semnal SHORT (intrare, SL, TP, mărime pentru riscul ales)
  ✅ o dată pe zi (după 07:00 UTC): „bot activ” + situația pieței
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
SHORT_ATR_MULT, VOL_MULT = 2.0, 1.5
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
    return [dict(t=int(k[0]), o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), v=float(k[5])) for k in r.json()]


def _bitfinex(limit):
    # format Bitfinex: [MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]; sort=1 = vechi -> nou
    r = requests.get("https://api-pub.bitfinex.com/v2/candles/trade:4h:tETHUSD/hist",
                     params={"limit": limit, "sort": 1}, timeout=20)
    r.raise_for_status()
    return [dict(t=int(k[0]), o=float(k[1]), h=float(k[3]), l=float(k[4]), c=float(k[2]), v=float(k[5])) for k in r.json()]


def _kraken(limit):
    r = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": "ETHUSD", "interval": 240}, timeout=20)
    r.raise_for_status()
    res = r.json()["result"]
    key = [k for k in res if k != "last"][0]
    return [dict(t=int(k[0]) * 1000, o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]), v=float(k[6])) for k in res[key]][-limit:]


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


def signal_short_on_last(rows):
    """SHORT pe ULTIMA lumânare închisă: prima închidere sub minimul a 20 de lumânări, sub EMA200, cu volum mare."""
    if len(rows) < LOOKBACK + 3:
        return None
    closes = [r["c"] for r in rows]
    e200 = ema(closes, EMA_LEN)
    atr = atr_wilder(rows)
    i = len(rows) - 1
    ll = min(r["l"] for r in rows[i - LOOKBACK:i])
    ll_prev = min(r["l"] for r in rows[i - 1 - LOOKBACK:i - 1])
    vavg = sum(r["v"] for r in rows[i - LOOKBACK:i]) / LOOKBACK
    vr = rows[i]["v"] / vavg if vavg > 0 else 0
    if closes[i] < ll and closes[i - 1] >= ll_prev and closes[i] < e200[i] and vr > VOL_MULT:
        return dict(candle_t=rows[i]["t"], close=closes[i], level=ll, ema200=e200[i], atr=atr[i], vr=vr)
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
        side = act.get("side", "long")
        hi = max([r["h"] for r in rows if r["t"] >= act["start_candle"]] + ([live["h"]] if live else []))
        lo = min([r["l"] for r in rows if r["t"] >= act["start_candle"]] + ([live["l"]] if live else []))
        tp_hit = hi >= act["tp"] if side == "long" else lo <= act["tp"]
        sl_hit = lo <= act["sl"] if side == "long" else hi >= act["sl"]
        name = "LONG" if side == "long" else "SHORT"
        if sl_hit:  # dacă în aceeași perioadă s-au atins amândouă, presupunem SL (varianta prudentă)
            send(f"🔴 ETH a atins SL-ul ({act['sl']:.2f}) la poziția {name}. Dacă ordinul a fost pus, poziția s-a închis. Trece rezultatul în jurnal.")
            state["active"] = None
        elif tp_hit:
            send(f"✅ ETH a atins TP-ul ({act['tp']:.2f}) la poziția {name}. Dacă ordinul a fost pus, poziția s-a închis pe profit. Trece rezultatul în jurnal.")
            state["active"] = None
        else:
            if not act.get("warned") and now >= act["exit_ts"] - 3600:
                send(f"⏰ Peste ~1 oră ({fmt_local(act['exit_ts'])}) se împlinesc 7 zile de la semnal ({name}). Pregătește închiderea la piață.")
                act["warned"] = True
            if now >= act["exit_ts"]:
                send(f"⏰ Au trecut 7 zile – închide acum poziția {name} ETH la piață (preț ~{price:.2f}). Trece rezultatul în jurnal.")
                state["active"] = None
        save_state(state)

    # 2) semnal nou pe ultima lumânare închisă (long și short nu pot apărea simultan: unul cere peste EMA200, celălalt sub)
    sig, side = signal_on_last(rows), "long"
    if not sig:
        sig, side = signal_short_on_last(rows), "short"
    if sig and sig["candle_t"] > state.get("last_signal_candle", 0):
        state["last_signal_candle"] = sig["candle_t"]
        entry = price
        if side == "long":
            sl = entry - ATR_MULT * sig["atr"]
            tp = entry + TP_R * (entry - sl)
        else:
            sl = entry + SHORT_ATR_MULT * sig["atr"]
            tp = entry - TP_R * (sl - entry)
        sl_pct = abs(entry - sl) / entry * 100
        pos_usd = RISK_USD / (sl_pct / 100)
        exit_ts = now + HOLD.total_seconds()
        candle_close = (sig["candle_t"] / 1000) + 4 * 3600
        if state.get("active"):
            send(f"ℹ️ Semnal nou ETH {side.upper()} (la {sig['close']:.2f}), dar ai deja o poziție deschisă – regula e o singură poziție o dată, nu intri.")
        else:
            if side == "long":
                head = ["🟢 SEMNAL ETH LONG – breakout 4h",
                        f"Lumânarea închisă la {fmt_local(candle_close)} a spart maximul pe 20 de lumânări ({sig['level']:.2f}) la {sig['close']:.2f}, peste EMA200 ({sig['ema200']:.2f})."]
                lines = [f"SL: {sl:.2f} (−{sl_pct:.1f}%)   [ATR {sig['atr']:.2f}]", f"TP: {tp:.2f} (+{TP_R * sl_pct:.1f}%)"]
            else:
                head = ["🔻 SEMNAL ETH SHORT – spargere în jos 4h",
                        f"Lumânarea închisă la {fmt_local(candle_close)} a spart minimul pe 20 de lumânări ({sig['level']:.2f}) la {sig['close']:.2f}, sub EMA200 ({sig['ema200']:.2f}), cu volum {sig['vr']:.1f}× media."]
                lines = [f"SL: {sl:.2f} (+{sl_pct:.1f}%)   [ATR {sig['atr']:.2f}]", f"TP: {tp:.2f} (−{TP_R * sl_pct:.1f}%)"]
            send("\n".join(head + [f"Intrare: ~{entry:.2f}"] + lines + [
                f"Mărime pentru risc {RISK_USD:.0f} $: ~{pos_usd:.0f} USDC ≈ {pos_usd / entry:.3f} ETH. Pune SL și TP pe ordin.",
                f"Dacă nu atinge SL/TP până {fmt_local(exit_ts)}, închizi la piață (primești reminder).",
                "Nu e consultanță financiară.",
            ]))
            state["active"] = dict(side=side, start_candle=sig["candle_t"] + 4 * 3600 * 1000, entry=entry, sl=sl, tp=tp,
                                   exit_ts=exit_ts, warned=False)
        save_state(state)
    daily_status(state, rows, price)
    return price, sig


def daily_status(state, rows, price):
    """O dată pe zi (prima rulare după 07:00 UTC = 09:00/10:00 ora României): mesaj „bot activ” cu situația pieței.
    Salvează și data în stare -> un commit pe zi în GitHub, ca să se vadă din repo că botul rulează."""
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    if now_utc.hour < 7 or state.get("daily") == today:
        return
    closes = [r["c"] for r in rows]
    e200 = ema(closes, EMA_LEN)[-1]
    hh = max(r["h"] for r in rows[-LOOKBACK:])
    ll = min(r["l"] for r in rows[-LOOKBACK:])
    vavg = sum(r["v"] for r in rows[-LOOKBACK:]) / LOOKBACK
    dist = (price / e200 - 1) * 100
    if price > e200:
        trend = f"ETH e cu {dist:.1f}% PESTE EMA200 – LONG posibil la o închidere 4h peste {hh:.2f}"
    else:
        trend = f"ETH e cu {-dist:.1f}% sub EMA200 – SHORT posibil la o închidere 4h sub {ll:.2f} cu volum peste {VOL_MULT}× media"
    act = state.get("active")
    poz = (f"Poziție urmărită: {act.get('side', 'long').upper()} intrare {act['entry']:.2f}, SL {act['sl']:.2f}, TP {act['tp']:.2f}, ieșire pe timp {fmt_local(act['exit_ts'])}"
           if act else "Poziție urmărită: niciuna")
    send("\n".join([
        f"✅ Bot ETH activ – {fmt_local(time.time())}",
        f"ETH: {price:.2f}   EMA200 (4h): {e200:.2f}",
        f"Maxim 20 lumânări: {hh:.2f}   Minim 20 lumânări: {ll:.2f}",
        trend,
        poz,
    ]))
    state["daily"] = today
    save_state(state)


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
