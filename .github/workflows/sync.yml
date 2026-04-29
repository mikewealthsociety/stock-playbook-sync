name: Daily Stock Playbook Sync

on:
  schedule:
    # Runs daily at 14:00 UTC = 7:00 AM Phoenix time (Arizona doesn't observe DST)
    - cron: '0 14 * * *'
  # Allow manual trigger from the Actions tab (handy for testing)
  workflow_dispatch:

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 350  # Generous ceiling in case of free-tier rate limiting

    steps:
      - name: Check out repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install requests

      - name: Run sync
        env:
          POLYGON_API_KEY: ${{ secrets.POLYGON_API_KEY }}
          NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}
          NOTION_DATABASE_ID: ${{ secrets.NOTION_DATABASE_ID }}
        run: python sync.py
