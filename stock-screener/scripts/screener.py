"""
Screener hebdomadaire RSI / MM50 / MM200 sur Nasdaq-100 + S&P 500.

Pour chaque action :
  - RSI(14) en clôture hebdomadaire < RSI_THRESHOLD -> catégorie "RSI survendu"
  - clôture hebdo proche de la MM50 hebdo (± TOUCH_PCT) -> catégorie "Touche MM50"
  - clôture hebdo proche de la MM200 hebdo (± TOUCH_PCT) -> catégorie "Touche MM200"

Une action déjà signalée cette semaine (même catégorie) n'est plus
resignalée avant le lundi suivant (déduplication via state.json).

Variables d'environnement :
  DISCORD_WEBHOOK_URL   (obligatoire)
  RSI_THRESHOLD         défaut 35
  TOUCH_PCT             défaut 0.015 (1.5%)
  BATCH_SIZE            défaut 40
  LOOKBACK_YEARS        défaut 6
  SEND_EMPTY_REPORT     défaut "true"
"""
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

TICKERS_PATH = "tickers.json"
STATE_PATH = "state.json"

RSI_THRESHOLD = float(os.environ.get("RSI_THRESHOLD", "35"))
TOUCH_PCT = float(os.environ.get("TOUCH_PCT", "0.015"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "40"))
LOOKBACK_YEARS = int(os.environ.get("LOOKBACK_YEARS", "6"))
SEND_EMPTY_REPORT = os.environ.get("SEND_EMPTY_REPORT", "true").lower() == "true"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

NY_TZ = ZoneInfo("America/New_York")

CAT_RSI = "rsi_oversold"
CAT_MM50 = "touch_mm50"
CAT_MM200 = "touch_mm200"

CAT_LABELS = {
    CAT_RSI: f"🔴 RSI hebdo < {RSI_THRESHOLD:.0f}",
    CAT_MM50: "🟠 Touche la MM50 hebdo",
    CAT_MM200: "🔵 Touche la MM200 hebdo",
}


# --------------------------------------------------------------------------
# Indicateurs
# --------------------------------------------------------------------------

def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def analyze_ticker(ticker: str, df: pd.DataFrame) -> list[dict]:
    """Retourne une liste de signaux (0 à 3) pour ce ticker sur la dernière
    bougie hebdo clôturée."""
    if df is None or df.empty or "Close" not in df:
        return []

    close = df["Close"].dropna()
    if len(close) < 15:  # pas assez d'historique même pour le RSI
        return []

    rsi = compute_rsi(close, 14)
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()

    last_close = close.iloc[-1]
    last_rsi = rsi.iloc[-1]
    last_ma50 = ma50.iloc[-1]
    last_ma200 = ma200.iloc[-1]

    if pd.isna(last_close) or pd.isna(last_rsi):
        return []

    signals = []

    if last_rsi < RSI_THRESHOLD:
        signals.append({
            "category": CAT_RSI,
            "ticker": ticker,
            "price": float(last_close),
            "detail": f"RSI {last_rsi:.1f}",
            "sort_key": float(last_rsi),
        })

    if not pd.isna(last_ma50) and last_ma50 > 0:
        dist = (last_close - last_ma50) / last_ma50
        if abs(dist) <= TOUCH_PCT:
            signals.append({
                "category": CAT_MM50,
                "ticker": ticker,
                "price": float(last_close),
                "detail": f"MM50={last_ma50:.2f} ({dist * 100:+.1f}%)",
                "sort_key": abs(float(dist)),
            })

    if not pd.isna(last_ma200) and last_ma200 > 0:
        dist = (last_close - last_ma200) / last_ma200
        if abs(dist) <= TOUCH_PCT:
            signals.append({
                "category": CAT_MM200,
                "ticker": ticker,
                "price": float(last_close),
                "detail": f"MM200={last_ma200:.2f} ({dist * 100:+.1f}%)",
                "sort_key": abs(float(dist)),
            })

    return signals


# --------------------------------------------------------------------------
# Téléchargement des données (par lots, avec retry)
# --------------------------------------------------------------------------

def download_batch(tickers: list[str], retries: int = 2) -> dict:
    """Télécharge les bougies hebdo pour un lot de tickers. Retourne
    {ticker: DataFrame}."""
    for attempt in range(1, retries + 1):
        try:
            data = yf.download(
                tickers=tickers,
                period=f"{LOOKBACK_YEARS}y",
                interval="1wk",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
            break
        except Exception as e:
            print(f"[WARN] Batch download échoué (tentative {attempt}/{retries}): {e}",
                  file=sys.stderr)
            if attempt == retries:
                return {}
            time.sleep(5)

    result = {}
    if isinstance(data.columns, pd.MultiIndex):
        for t in tickers:
            if t in data.columns.get_level_values(0):
                result[t] = data[t]
    else:
        # Un seul ticker dans le lot
        if len(tickers) == 1:
            result[tickers[0]] = data
    return result


def fetch_all(tickers: list[str]) -> dict:
    all_data = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f"Téléchargement lot {i // BATCH_SIZE + 1} "
              f"({len(batch)} tickers)...")
        all_data.update(download_batch(batch))
        time.sleep(1)  # petite pause pour ménager l'API Yahoo
    return all_data


# --------------------------------------------------------------------------
# Déduplication hebdomadaire
# --------------------------------------------------------------------------

def current_week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def load_state(week_key: str) -> set:
    if not os.path.exists(STATE_PATH):
        return set()
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()
    if state.get("week") != week_key:
        return set()  # nouvelle semaine -> reset
    return set(state.get("sent", []))


def save_state(week_key: str, sent: set):
    with open(STATE_PATH, "w") as f:
        json.dump({"week": week_key, "sent": sorted(sent)}, f, indent=2)


# --------------------------------------------------------------------------
# Rapport Discord
# --------------------------------------------------------------------------

def format_lines(signals: list[dict]) -> list[str]:
    signals_sorted = sorted(signals, key=lambda s: s["sort_key"])
    return [
        f"**{s['ticker']}** — {s['price']:.2f}$ · {s['detail']}"
        for s in signals_sorted
    ]


def chunk_field_value(lines: list[str], max_chars: int = 1000) -> list[str]:
    """Découpe une liste de lignes en blocs respectant la limite Discord
    (1024 caractères par valeur de field)."""
    chunks, current, current_len = [], [], 0
    for line in lines:
        if current_len + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or ["Aucun nouveau signal cette semaine."]


def build_embeds(new_signals_by_cat: dict, scanned: int, failed: int, now: datetime) -> list[dict]:
    fields = []
    for cat in (CAT_RSI, CAT_MM50, CAT_MM200):
        lines = format_lines(new_signals_by_cat.get(cat, []))
        if not lines:
            fields.append({
                "name": CAT_LABELS[cat],
                "value": "Aucun nouveau signal.",
                "inline": False,
            })
            continue
        chunks = chunk_field_value(lines)
        for idx, chunk in enumerate(chunks):
            label = CAT_LABELS[cat] if len(chunks) == 1 else f"{CAT_LABELS[cat]} ({idx + 1}/{len(chunks)})"
            fields.append({"name": label, "value": chunk, "inline": False})

    embed = {
        "title": f"📊 Rapport hebdo RSI / MM50 / MM200 — {now.strftime('%d/%m/%Y')}",
        "description": (
            f"Scan Nasdaq-100 + S&P 500 · {scanned} actions analysées"
            + (f" · {failed} échecs de téléchargement" if failed else "")
            + "\n_Une action n'est resignalée qu'une fois par semaine._"
        ),
        "color": 0x2ecc71,
        "fields": fields,
        "footer": {"text": f"Clôture du {now.strftime('%d/%m/%Y')} (heure NY)"},
    }
    return [embed]


def send_discord(embeds: list[dict]):
    if not DISCORD_WEBHOOK_URL:
        print("[ERROR] DISCORD_WEBHOOK_URL non défini.", file=sys.stderr)
        sys.exit(1)
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": embeds}, timeout=15)
    if resp.status_code >= 300:
        print(f"[ERROR] Discord a répondu {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    now = datetime.now(NY_TZ)

    if not os.path.exists(TICKERS_PATH):
        print(f"[ERROR] {TICKERS_PATH} introuvable. "
              f"Lance d'abord scripts/update_tickers.py.", file=sys.stderr)
        sys.exit(1)

    with open(TICKERS_PATH) as f:
        tickers = json.load(f)["tickers"]

    print(f"{len(tickers)} tickers à analyser.")

    week_key = current_week_key(now)
    already_sent = load_state(week_key)  # set de "TICKER:category"

    price_data = fetch_all(tickers)
    failed = len(tickers) - len(price_data)
    if failed:
        print(f"[WARN] {failed} tickers sans données (delisting, ticker "
              f"invalide, échec réseau...).", file=sys.stderr)

    new_signals_by_cat = {CAT_RSI: [], CAT_MM50: [], CAT_MM200: []}
    newly_sent = set()

    for ticker, df in price_data.items():
        try:
            signals = analyze_ticker(ticker, df)
        except Exception as e:
            print(f"[WARN] Analyse échouée pour {ticker}: {e}", file=sys.stderr)
            continue

        for sig in signals:
            key = f"{sig['ticker']}:{sig['category']}"
            if key in already_sent:
                continue
            new_signals_by_cat[sig["category"]].append(sig)
            newly_sent.add(key)

    total_new = sum(len(v) for v in new_signals_by_cat.values())
    print(f"{total_new} nouveaux signaux (déjà envoyés cette semaine: {len(already_sent)}).")

    if total_new == 0 and not SEND_EMPTY_REPORT:
        print("Aucun nouveau signal et SEND_EMPTY_REPORT=false -> pas d'envoi Discord.")
        return

    embeds = build_embeds(new_signals_by_cat, scanned=len(price_data), failed=failed, now=now)
    send_discord(embeds)
    print("Rapport envoyé sur Discord.")

    save_state(week_key, already_sent | newly_sent)
    print(f"State sauvegardé ({STATE_PATH}), semaine {week_key}.")


if __name__ == "__main__":
    main()
