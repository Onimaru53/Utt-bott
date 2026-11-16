import os
import json
import requests
import pandas as pd

# ====== AYARLAR ======
TIMEFRAMES = ["15m", "1h", "4h"]
SETTINGS = [(2, 1, "short"), (2, 300, "long")]  # (Key Value, ATR Period, yön)
# =====================

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")
STATE_FILE = "state.json"
TF_MIN = {"15m": 15, "1h": 60, "4h": 240}
OANDA_GRAN = {"15m": "M15", "1h": "H1", "4h": "H4"}


def tg(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


def only_closed(df, tf):
    now = pd.Timestamp.now(tz="UTC")
    return df[df.index + pd.Timedelta(minutes=TF_MIN[tf]) <= now]


def get_oanda(tf):
    r = requests.get(
        "https://api-fxpractice.oanda.com/v3/instruments/XAU_USD/candles",
        params={"granularity": OANDA_GRAN[tf], "count": 2000, "price": "M"},
        headers={"Authorization": f"Bearer {OANDA_TOKEN}"},
        timeout=30,
    )
    r.raise_for_status()
    rows = [
        {
            "datetime": c["time"],
            "open": float(c["mid"]["o"]),
            "high": float(c["mid"]["h"]),
            "low": float(c["mid"]["l"]),
            "close": float(c["mid"]["c"]),
        }
        for c in r.json()["candles"]
        if c.get("complete")
    ]
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    return df.set_index("datetime").sort_index()


def get_yahoo(tf):  # yedek kaynak
    import yfinance as yf

    t = yf.Ticker("XAUUSD=X")
    if tf == "15m":
        df = t.history(period="30d", interval="15m")
    else:
        df = t.history(period="365d", interval="1h")
    df = df.rename(columns=str.lower)[["open", "high", "low", "close"]]
    df.index = pd.to_datetime(df.index, utc=True)
    if tf == "4h":
        df = df.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
    return df


def get_data(tf):
    if OANDA_TOKEN:
        try:
            return only_closed(get_oanda(tf), tf), "OANDA"
        except Exception as e:
            print(tf, "OANDA hatası, Yahoo'ya geçiliyor:", e)
    return only_closed(get_yahoo(tf), tf), "Yahoo"


def ut_bot(df, a, c):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / c, adjust=False).mean()  # Pine atr = RMA
    nloss = (a * atr).values
    src = close.values

    n = len(src)
    stop = [0.0] * n
    for i in range(1, n):
        p = stop[i - 1]
        if src[i] > p and src[i - 1] > p:
            stop[i] = max(p, src[i] - nloss[i])
        elif src[i] < p and src[i - 1] < p:
            stop[i] = min(p, src[i] + nloss[i])
        elif src[i] > p:
            stop[i] = src[i] - nloss[i]
        else:
            stop[i] = src[i] + nloss[i]

    i = n - 1  # son kapanmış mum
    buy = src[i] > stop[i] and src[i - 1] <= stop[i - 1]
    sell = src[i] < stop[i] and src[i - 1] >= stop[i - 1]
    return buy, sell, src[i]


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"checked": {}, "sent": {}}


def main():
    state = load_state()
    now = pd.Timestamp.now(tz="UTC")

    for tf in TIMEFRAMES:
        step = TF_MIN[tf] if tf != "4h" else 60
        expected = now.floor(f"{step}min")
        if state["checked"].get(tf) == expected.isoformat():
            continue  # bu aralık zaten kontrol edildi

        try:
            df, source = get_data(tf)
        except Exception as e:
            print(tf, "veri alınamadı:", e)
            continue
        if len(df) < 50:
            continue

        last_close = df.index[-1] + pd.Timedelta(minutes=TF_MIN[tf])
        if tf != "4h" and last_close < expected:
            # veri henüz güncellenmedi: sonraki çalışmada tekrar dene
            # (hafta sonu gibi durumlarda 20 dk sonra vazgeç)
            if now - expected > pd.Timedelta(minutes=20):
                state["checked"][tf] = expected.isoformat()
            continue

        state["checked"][tf] = expected.isoformat()
        if now - last_close > pd.Timedelta(minutes=2 * TF_MIN[tf]):
            continue  # eski mum, sinyal gönderme

        for a, c, side in SETTINGS:
            buy, sell, price = ut_bot(df, a, c)
            key = f"{tf}|{a}|{c}|{side}"
            if state["sent"].get(key) == last_close.isoformat():
                continue
            if buy and side == "long":
                tg(f"🟢 UT LONG | XAUUSD {tf} | Ayar {a}/{c} | Fiyat: {price:.2f}")
                state["sent"][key] = last_close.isoformat()
            if sell and side == "short":
                tg(f"🔴 UT SHORT | XAUUSD {tf} | Ayar {a}/{c} | Fiyat: {price:.2f}")
                state["sent"][key] = last_close.isoformat()

    json.dump(state, open(STATE_FILE, "w"))


if __name__ == "__main__":
    main()
