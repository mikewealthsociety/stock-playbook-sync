# Notion <> Polygon Stock Playbook Sync

Updates four historical price columns (5D, 1M, 6M, 1Y ago) in your Notion Stock Playbook database every day, using Polygon.io as the price source.

## Setup (one-time, ~20 minutes)

### 1. Create a Notion integration

1. Go to https://www.notion.so/profile/integrations
2. Click **New integration**
3. Name it something like "Stock Playbook Sync"
4. Associate it with your workspace
5. Click **Save**, then go to the **Configuration** tab
6. Under **Capabilities**, make sure **Read content** and **Update content** are both checked
7. Copy the **Internal Integration Secret** — this is your `NOTION_TOKEN`. Starts with `ntn_` or `secret_`. Save it somewhere safe.

### 2. Share the Stock Playbook database with the integration

1. Open your Stock Playbook database in Notion
2. Click the `•••` menu in the top right
3. Scroll to **Connections** → **Connect to** → search for and select your new integration
4. Confirm the connection

Without this step, the integration can see nothing.

### 3. Get the database ID

The database ID is in the URL when you have the database open. It looks like:

```
https://www.notion.so/yourworkspace/abc123def456...?v=xyz
                                   ^^^^^^^^^^^^^^^
                                   This 32-char string
```

Copy that 32-character string. This is your `NOTION_DATABASE_ID`.

### 4. Verify your Notion column names match exactly

The script expects these property names in your database, **case-sensitive**:

- `Ticker` (Text type)
- `Price 5D Ago` (Number type)
- `Price 1M Ago` (Number type)
- `Price 6M Ago` (Number type)
- `Price 1Y Ago` (Number type)
- `Last Market Sync` (Date type)

If any name differs, either rename in Notion or edit the constants at the top of `sync.py`.

### 5. Set up the GitHub repo

1. Create a new private repo at https://github.com/new (call it whatever — `stock-playbook-sync` is fine)
2. Add the two files: `sync.py` and `.github/workflows/sync.yml`
3. Commit and push

### 6. Add secrets to the repo

In the GitHub repo: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add three secrets:

| Name | Value |
|---|---|
| `POLYGON_API_KEY` | Your Polygon API key |
| `NOTION_TOKEN` | The integration secret from step 1 |
| `NOTION_DATABASE_ID` | The 32-char ID from step 3 |

### 7. Test it

Go to the **Actions** tab in your repo → click **Daily Stock Playbook Sync** → click **Run workflow**.

Watch the log. You should see each ticker get processed, prices fetched, and Notion rows updated. Then check your Stock Playbook in Notion — the four price columns and Last Market Sync should be populated.

After this manual test passes, it'll run automatically every day at 7am Phoenix time.

## Local testing (optional)

If you want to test from your laptop before deploying:

```bash
pip install requests
export POLYGON_API_KEY="your-key"
export NOTION_TOKEN="your-token"
export NOTION_DATABASE_ID="your-db-id"
python sync.py
```

## Notes on behavior

- **Lookback dates** target 5, 30, 182, and 365 calendar days ago. The script automatically walks back up to 7 days from the target if the market was closed (weekends/holidays), so 5D Ago might actually be 7 days ago if the target landed on a Sunday after a Friday holiday.
- **Missing data** is left as `None` (blank in Notion) rather than zeroed out, so you can see at a glance which tickers had a problem.
- **Rate limiting** is auto-detected. On a 429 from Polygon, the script bumps its delay to 13s between calls (matching free tier's 5/min limit) and retries with exponential backoff. If you upgrade Polygon, the script just runs faster automatically.
- **Last Market Sync** stamps every successfully-updated row with the UTC timestamp of the run, so you can spot stale rows.
