# ─────────────────────────────────────────────────────────────────────────────
# OffGamers Weekly Product Events Tracker — Cloud Factory
#
# Runs every Thursday at 03:00 UTC, or manually from the GitHub Actions tab.
# Produces index.html and commits it back to the repo.
#
# REQUIRED secrets (Repo → Settings → Secrets and variables → Actions → New):
#   FIRECRAWL_API_KEY   — your Firecrawl key (starts with fc-…)
#   ANTHROPIC_API_KEY   — your Anthropic / Claude API key
#
# Tracker.xlsx must be committed to the repo root.
# ─────────────────────────────────────────────────────────────────────────────

name: Weekly Product Events Tracker

on:
  # ── Automatic: every Thursday at 03:00 UTC ──────────────────────────────
  schedule:
    - cron: '0 3 * * 4'

  # ── Manual: Actions tab → "Run workflow" button ─────────────────────────
  workflow_dispatch:

# Allow this workflow to commit index.html back to the repo
permissions:
  contents: write

jobs:
  run-tracker:
    runs-on: ubuntu-latest
    timeout-minutes: 60     # Safety cap — scraping 59 rows can take ~20 min

    steps:
      # 1. Check out the repo so we have Tracker.xlsx and main.py
      - name: Checkout repository
        uses: actions/checkout@v4

      # 2. Install Python 3.11
      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      # 3. Install Python libraries from requirements.txt
      - name: Install dependencies
        run: pip install -r requirements.txt

      # 4. Run the tracker — reads Tracker.xlsx, writes index.html
      - name: Run tracker
        env:
          FIRECRAWL_API_KEY: ${{ secrets.FIRECRAWL_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python main.py

      # 5. Commit the new index.html back to the repo
      - name: Commit updated index.html and summary.md
        run: |
          git config --global user.name  'github-actions[bot]'
          git config --global user.email 'github-actions[bot]@users.noreply.github.com'
          git add index.html summary.md
          git diff --staged --quiet || git commit -m "Update tracker report ($(date -u +%Y-%m-%d))"
          git push
