"""
Met à jour la liste des tickers Nasdaq-100 + S&P 500 depuis Wikipedia.
A lancer périodiquement (workflow séparé, ex: 1x/mois) car ces listes
changent peu souvent. Le screener quotidien lit tickers.json généré ici
plutôt que de re-scraper Wikipedia à chaque run (plus rapide, plus fiable).
"""
import io
import json
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

OUTPUT_PATH = "tickers.json"

# Wikipedia renvoie une 403 Forbidden aux requêtes sans User-Agent de type
# navigateur (ce que pandas.read_html envoie par défaut). On récupère donc
# le HTML nous-mêmes avec un User-Agent explicite, puis on le passe à
# pandas.read_html.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def normalize(ticker: str) -> str:
    """Yahoo Finance utilise '-' au lieu de '.' (ex: BRK.B -> BRK-B)."""
    return ticker.strip().upper().replace(".", "-")


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_sp500() -> set[str]:
    html = fetch_html(SP500_URL)
    tables = pd.read_html(io.StringIO(html))
    df = tables[0]
    col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    return {normalize(t) for t in df[col].astype(str)}


def fetch_nasdaq100() -> set[str]:
    html = fetch_html(NASDAQ100_URL)
    tables = pd.read_html(io.StringIO(html))
    # La table des composants change parfois d'index selon les éditions de la page,
    # on cherche celle qui contient une colonne Ticker/Symbol.
    for df in tables:
        cols = [c for c in df.columns if str(c).lower() in ("ticker", "symbol")]
        if cols:
            return {normalize(t) for t in df[cols[0]].astype(str)}
    raise RuntimeError("Impossible de trouver la table des tickers Nasdaq-100")


def main():
    try:
        sp500 = fetch_sp500()
    except Exception as e:
        print(f"[WARN] Echec récupération S&P 500: {e}", file=sys.stderr)
        sp500 = set()

    try:
        nasdaq100 = fetch_nasdaq100()
    except Exception as e:
        print(f"[WARN] Echec récupération Nasdaq-100: {e}", file=sys.stderr)
        nasdaq100 = set()

    all_tickers = sorted(t for t in (sp500 | nasdaq100) if t and t != "NAN")

    if len(all_tickers) < 400:
        print(f"[ERROR] Seulement {len(all_tickers)} tickers trouvés, "
              f"c'est suspect (attendu ~600). Abandon, tickers.json non modifié.",
              file=sys.stderr)
        sys.exit(1)

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(all_tickers),
        "sp500_count": len(sp500),
        "nasdaq100_count": len(nasdaq100),
        "tickers": all_tickers,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"OK: {len(all_tickers)} tickers écrits dans {OUTPUT_PATH} "
          f"(S&P500={len(sp500)}, Nasdaq100={len(nasdaq100)})")


if __name__ == "__main__":
    main()