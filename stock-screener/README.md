# Stock Screener — RSI / MM50 / MM200 hebdo (Nasdaq-100 + S&P 500)

Bot 100% GitHub Actions (pas de serveur à faire tourner) qui, chaque jour à la
clôture des marchés US :

1. Récupère les bougies **hebdomadaires** de toutes les actions du
   Nasdaq-100 + S&P 500.
2. Calcule pour chacune : **RSI(14) hebdo**, **MM50 hebdo**, **MM200 hebdo**.
3. Repère celles qui touchent actuellement leur MM50 ou MM200 (± 1.5% par
   défaut), ou dont le RSI hebdo est sous 35.
4. Envoie un rapport clair sur Discord, avec 3 sections distinctes.
5. **Ne resignale jamais une action déjà envoyée dans la semaine** — le
   compteur se remet à zéro chaque lundi.

## Structure du repo

```
.
├── requirements.txt
├── tickers.json                # généré automatiquement (liste des tickers)
├── state.json                  # généré/mis à jour automatiquement (dédup hebdo)
├── scripts/
│   ├── update_tickers.py       # rafraîchit tickers.json (Wikipedia)
│   └── screener.py             # calcule les indicateurs + envoie sur Discord
└── .github/workflows/
    ├── daily-report.yml        # tourne chaque jour ouvré à la clôture
    └── update-tickers.yml      # tourne 1x/mois pour rafraîchir la liste
```

## Installation

1. **Ajoute le secret Discord** dans ton repo :
   `Settings` → `Secrets and variables` → `Actions` → `New repository secret`
   - Nom : `DISCORD_WEBHOOK_URL`
   - Valeur : l'URL de ton webhook Discord

2. **Vérifie les permissions d'écriture des workflows** :
   `Settings` → `Actions` → `General` → `Workflow permissions` →
   coche **"Read and write permissions"**
   (nécessaire pour que le bot puisse committer `state.json` et `tickers.json`).

3. **Génère la première liste de tickers** : va dans l'onglet `Actions` →
   `Mise à jour liste des tickers` → `Run workflow`. Ça crée `tickers.json`.
   (Sans ça, le rapport quotidien échouera au premier lancement.)

4. **Teste le rapport manuellement** : onglet `Actions` →
   `Rapport quotidien RSI / MM50 / MM200` → `Run workflow`.
   Tu dois recevoir un embed sur Discord dans les minutes qui suivent.

C'est tout — ensuite les deux workflows tournent seuls, en cron, H24.

## Réglages disponibles

Modifiables directement dans `.github/workflows/daily-report.yml` (bloc `env:`) :

| Variable | Défaut | Effet |
|---|---|---|
| `RSI_THRESHOLD` | `35` | seuil RSI hebdo considéré survendu |
| `TOUCH_PCT` | `0.015` | tolérance pour considérer qu'un prix "touche" une MM (1.5%) |
| `BATCH_SIZE` | `40` | nb de tickers téléchargés par lot (Yahoo Finance) |
| `LOOKBACK_YEARS` | `6` | historique téléchargé (nécessaire pour la MM200 hebdo = 200 semaines ≈ 3.8 ans) |
| `SEND_EMPTY_REPORT` | `true` | si `false`, aucun message Discord n'est envoyé quand il n'y a aucun nouveau signal |

Exemple pour resserrer le seuil RSI et la tolérance MM :
```yaml
      - name: Lancer le screener
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          RSI_THRESHOLD: "30"
          TOUCH_PCT: "0.01"
        run: python scripts/screener.py
```

## Notes importantes

- **Cron en UTC / DST** : le cron `30 21 * * 1-5` tombe systématiquement
  après la clôture US (16h ET) été comme hiver, avec 30-90 min de marge pour
  que la bougie hebdo finale soit disponible côté Yahoo Finance.
- **Source de données** : Yahoo Finance via `yfinance`, gratuit mais non
  garanti par contrat — en cas de panne ponctuelle de leur API, un run peut
  échouer ou ramener moins de données ; ce n'est pas piloté par toi.
- **"Touche la MM"** est défini par proximité (± `TOUCH_PCT`), pas par un
  croisement exact, car un prix "touche" rarement une moyenne mobile à la
  clôture au centime près.
- **Dédup hebdo** : la clé de déduplication est `TICKER:catégorie`. Une
  action peut donc réapparaître dans une catégorie différente la même
  semaine (ex: signalée pour RSI lundi, puis pour MM200 jeudi si elle
  descend encore), mais jamais deux fois pour la même catégorie avant le
  lundi suivant.
- Ceci est un outil d'aide à la lecture technique, **pas un conseil
  financier**.
