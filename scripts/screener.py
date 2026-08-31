"""
Screener hebdomadaire RSI / MM50 / MM200 sur Nasdaq-100 + S&P 500.

Pour chaque action :
  - RSI(14) en clôture hebdomadaire < RSI_THRESHOLD -> catégorie "RSI survendu"
  - clôture hebdo proche de la MM50 hebdo (± TOUCH_PCT) -> catégorie "Touche MM50"
  - clôture hebdo proche de la MM200 hebdo (± TOUCH_PCT) -> catégorie "Touche MM200"

Une action déjà signalée cette semaine (même catégorie) n'est plus
resignalée avant le lundi suivant (déduplication via state.json).

Le rapport est généré en PDF (tableaux, triés par market cap décroissante) et
envoyé en pièce jointe sur Discord, avec un court message texte en résumé.

Variables d'environnement :
  DISCORD_WEBHOOK_URL   (obligatoire)
  RSI_THRESHOLD         défaut 35
  TOUCH_PCT             défaut 0.015 (1.5%)
  BATCH_SIZE            défaut 40
  LOOKBACK_YEARS         défaut 6
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
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

TICKERS_PATH = "tickers.json"
STATE_PATH = "state.json"
PDF_PATH = "report.pdf"

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
    CAT_RSI: f"RSI hebdo < {RSI_THRESHOLD:.0f}",
    CAT_MM50: "Touche la MM50 hebdo",
    CAT_MM200: "Touche la MM200 hebdo",
}

CAT_EMOJIS = {
    CAT_RSI: "🔴",
    CAT_MM50: "🟠",
    CAT_MM200: "🔵",
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
        time.sleep(1)
    return all_data


def fetch_market_caps(tickers: list[str]) -> dict:
    """Récupère la capitalisation boursière uniquement pour les tickers
    passés en argument (typiquement : ceux qui ont un nouveau signal
    aujourd'hui, donc une petite liste -> pas besoin de batcher)."""
    caps = {}
    for t in tickers:
        try:
            fi = yf.Ticker(t).fast_info
            mc = None
            for key in ("market_cap", "marketCap"):
                try:
                    val = fi[key]
                    if val:
                        mc = val
                        break
                except Exception:
                    continue
            caps[t] = float(mc) if mc else None
        except Exception as e:
            print(f"[WARN] Market cap indisponible pour {t}: {e}", file=sys.stderr)
            caps[t] = None
    return caps


def format_market_cap(mc) -> str:
    if not mc:
        return "N/A"
    if mc >= 1e12:
        return f"{mc / 1e12:.2f}T$"
    if mc >= 1e9:
        return f"{mc / 1e9:.2f}B$"
    if mc >= 1e6:
        return f"{mc / 1e6:.1f}M$"
    return f"{mc:.0f}$"


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
        return set()
    return set(state.get("sent", []))


def save_state(week_key: str, sent: set):
    with open(STATE_PATH, "w") as f:
        json.dump({"week": week_key, "sent": sorted(sent)}, f, indent=2)


# --------------------------------------------------------------------------
# Génération du PDF
# --------------------------------------------------------------------------

def make_table(data: list[list[str]]) -> Table:
    t = Table(data, hAlign="LEFT",
              colWidths=[2.3 * cm, 2.6 * cm, 2.2 * cm, None])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f2f2")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def build_multi_signal_rows(new_signals_by_cat: dict) -> dict:
    """Regroupe par ticker les catégories touchées, ne garde que ceux qui
    valident 2 catégories ou plus."""
    by_ticker = {}
    for cat, sigs in new_signals_by_cat.items():
        for s in sigs:
            entry = by_ticker.setdefault(s["ticker"], {"cats": {}, "price": s["price"]})
            entry["cats"][cat] = s["detail"]
    return {t: d for t, d in by_ticker.items() if len(d["cats"]) >= 2}


def esc(text: str) -> str:
    """Echappe les caractères spéciaux XML pour les Paragraph reportlab
    (reportlab interprète le contenu des Paragraph comme du XML/HTML léger,
    donc '&', '<', '>' doivent être échappés pour s'afficher correctement)."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_pdf_report(new_signals_by_cat: dict, market_caps: dict,
                      now: datetime, scanned: int, failed: int, out_path: str):
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    elements = []

    elements.append(Paragraph(
        f"Rapport hebdo RSI / MM50 / MM200 — {now.strftime('%d/%m/%Y')}",
        styles["Title"]))
    subtitle = esc(f"Scan Nasdaq-100 + S&P 500 · {scanned} actions analysées")
    if failed:
        subtitle += esc(f" · {failed} échecs de téléchargement")
    elements.append(Paragraph(subtitle, styles["Normal"]))
    elements.append(Paragraph(
        "Une action n'est resignalée qu'une fois par semaine. "
        "Classement par capitalisation boursière décroissante.",
        styles["Italic"]))
    elements.append(Spacer(1, 0.6 * cm))

    # --- Tableau 1 : actions validant plusieurs indicateurs ---
    multi = build_multi_signal_rows(new_signals_by_cat)
    multi_sorted = sorted(
        multi.items(),
        key=lambda kv: (market_caps.get(kv[0]) or -1),
        reverse=True,
    )
    elements.append(Paragraph("Actions validant plusieurs indicateurs", styles["Heading2"]))
    if multi_sorted:
        data = [["Ticker", "Market Cap", "Prix", "Indicateurs validés"]]
        for ticker, d in multi_sorted:
            cats_str = " + ".join(CAT_LABELS[c] for c in d["cats"])
            data.append([ticker, format_market_cap(market_caps.get(ticker)),
                         f"{d['price']:.2f}$", cats_str])
        elements.append(make_table(data))
    else:
        elements.append(Paragraph("Aucune action ne valide plusieurs indicateurs aujourd'hui.",
                                   styles["Normal"]))
    elements.append(Spacer(1, 0.6 * cm))

    # --- Un tableau par indicateur ---
    for cat in (CAT_RSI, CAT_MM50, CAT_MM200):
        elements.append(Paragraph(CAT_LABELS[cat], styles["Heading2"]))
        sigs = new_signals_by_cat.get(cat, [])
        sigs_sorted = sorted(
            sigs, key=lambda s: (market_caps.get(s["ticker"]) or -1), reverse=True
        )
        if sigs_sorted:
            data = [["Ticker", "Market Cap", "Prix", "Détail"]]
            for s in sigs_sorted:
                data.append([s["ticker"], format_market_cap(market_caps.get(s["ticker"])),
                             f"{s['price']:.2f}$", s["detail"]])
            elements.append(make_table(data))
        else:
            elements.append(Paragraph("Aucun nouveau signal.", styles["Normal"]))
        elements.append(Spacer(1, 0.5 * cm))

    doc.build(elements)


# --------------------------------------------------------------------------
# Envoi Discord (PDF en pièce jointe)
# --------------------------------------------------------------------------

def build_summary_text(new_signals_by_cat: dict, multi_count: int,
                        scanned: int, now: datetime) -> str:
    counts = {cat: len(sigs) for cat, sigs in new_signals_by_cat.items()}
    lines = [
        f"📊 **Rapport hebdo RSI / MM50 / MM200 — {now.strftime('%d/%m/%Y')}**",
        f"Scan Nasdaq-100 + S&P 500 · {scanned} actions analysées",
        f"🏆 {multi_count} action(s) valident plusieurs indicateurs",
        f"{CAT_EMOJIS[CAT_RSI]} {counts.get(CAT_RSI, 0)} nouveaux signaux RSI < {RSI_THRESHOLD:.0f} · "
        f"{CAT_EMOJIS[CAT_MM50]} {counts.get(CAT_MM50, 0)} touchent la MM50 · "
        f"{CAT_EMOJIS[CAT_MM200]} {counts.get(CAT_MM200, 0)} touchent la MM200",
        "📎 Détail complet (tableaux triés par market cap) dans le PDF ci-dessous.",
    ]
    return "\n".join(lines)


def send_discord_with_pdf(summary_text: str, pdf_path: str):
    if not DISCORD_WEBHOOK_URL:
        print("[ERROR] DISCORD_WEBHOOK_URL non défini.", file=sys.stderr)
        sys.exit(1)
    payload = {"content": summary_text}
    with open(pdf_path, "rb") as f:
        files = {"file": (os.path.basename(pdf_path), f, "application/pdf")}
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            data={"payload_json": json.dumps(payload)},
            files=files,
            timeout=30,
        )
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
    already_sent = load_state(week_key)

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

    # Market cap uniquement pour les tickers concernés par un nouveau signal
    signal_tickers = sorted({s["ticker"] for sigs in new_signals_by_cat.values() for s in sigs})
    print(f"Récupération du market cap pour {len(signal_tickers)} ticker(s)...")
    market_caps = fetch_market_caps(signal_tickers)

    multi = build_multi_signal_rows(new_signals_by_cat)

    build_pdf_report(new_signals_by_cat, market_caps, now,
                      scanned=len(price_data), failed=failed, out_path=PDF_PATH)
    print(f"PDF généré : {PDF_PATH}")

    summary_text = build_summary_text(new_signals_by_cat, len(multi), len(price_data), now)
    send_discord_with_pdf(summary_text, PDF_PATH)
    print("Rapport envoyé sur Discord (PDF en pièce jointe).")

    save_state(week_key, already_sent | newly_sent)
    print(f"State sauvegardé ({STATE_PATH}), semaine {week_key}.")


if __name__ == "__main__":
    main()
