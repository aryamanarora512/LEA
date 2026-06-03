# Deploying LEA to a Live Website

## Recommended stack: GitHub + Railway (~$5/month, 20 min setup)

---

## Step 1 — Push to GitHub

1. Go to https://github.com/new — create a private repo called `lea-sourcing-tool`
2. On your computer, open Terminal in the `sourcing-tool/` folder:

```bash
git init
git add .
git commit -m "Initial LEA sourcing tool"
git remote add origin https://github.com/YOUR_USERNAME/lea-sourcing-tool.git
git push -u origin main
```

The `.gitignore` already excludes your `.env` and `google_credentials.json`.

---

## Step 2 — Deploy on Railway

1. Go to https://railway.app — sign in with GitHub
2. Click **New Project → Deploy from GitHub repo**
3. Select `lea-sourcing-tool`
4. Railway will detect it's a Python app. Set the **Root Directory** to `backend`
5. It will build and try to start — it will fail until you add env variables (next step)

---

## Step 3 — Add environment variables on Railway

In your Railway project → **Variables** tab, add each line from your `.env` file:

```
NEWS_API_KEY=...
SERP_API_KEY=...
GROQ_API_KEY=...
YELP_API_KEY=...
HUNTER_API_KEY=...
COURTLISTENER_TOKEN=...
GOOGLE_SHEET_TAB=Sourcing Outbound Call Tracker
```

For `google_credentials.json` (the service account file), paste the entire JSON
as a single environment variable called `GOOGLE_CREDENTIALS_JSON`.

---

## Step 4 — Handle Google Credentials on Railway

Since you can't upload files to Railway, the app reads credentials from the
environment variable instead. The backend already handles this automatically:
if `google_credentials.json` doesn't exist as a file but `GOOGLE_CREDENTIALS_JSON`
is set as an env var, it uses that.

---

## Step 5 — Persistent database

Railway provides a persistent volume so your SQLite database survives deploys.
In Railway: **your service → Settings → Volumes → Add Volume**
- Mount path: `/app/data`

Then add one more env variable:
```
DB_PATH=/app/data/lea.db
```

---

## Step 6 — Custom domain (optional, ~$12/year)

1. Buy a domain at https://namecheap.com (e.g. `lea-intel.com`)
2. In Railway: **your service → Settings → Domains → Add Custom Domain**
3. Railway gives you DNS records — paste them into Namecheap's DNS settings
4. Done — your app is live at `https://lea-intel.com` within minutes

Without a custom domain, Railway gives you a free URL like:
`https://lea-sourcing-tool-production.up.railway.app`

---

## Alternative: Render (free tier available)

If you want to start free:
1. Go to https://render.com → New Web Service → Connect GitHub repo
2. Root directory: `backend`
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 4`

⚠️  Free tier on Render **sleeps after 15 minutes of inactivity** (first request takes ~30s to wake).
    Paid tier ($7/month) stays always-on.

---

## Security note for production

Once live, consider adding a simple password to the user picker so the URL
can't be accessed by anyone who finds it. Ask Aryaman to add HTTP Basic Auth
or a PIN entry screen.

---

## Summary

| What you need        | Where to get it          | Cost          |
|----------------------|--------------------------|---------------|
| Code hosting         | GitHub (free)            | Free          |
| App hosting          | Railway                  | ~$5/month     |
| Database persistence | Railway volume           | ~$0.25/month  |
| Custom domain        | Namecheap / Google       | ~$12/year     |
| Google Sheets        | Google Cloud (free tier) | Free          |
