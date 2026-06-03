# Google Sheets Setup Guide

## One-time setup (5 minutes)

### Step 1 — Create a Google Cloud service account
1. Go to https://console.cloud.google.com/
2. Create a new project (or use an existing one)
3. Enable the **Google Sheets API**: APIs & Services → Library → search "Google Sheets API" → Enable
4. Enable the **Google Drive API** the same way
5. Go to APIs & Services → Credentials → Create Credentials → **Service Account**
6. Name it "lea-sourcing-tool", click Done
7. Click the service account → Keys tab → Add Key → JSON → Download

### Step 2 — Place credentials in the backend folder
Rename the downloaded file to:
```
sourcing-tool/backend/google_credentials.json
```
⚠️  Never commit this file to git. It is already in .gitignore.

### Step 3 — Share the Google Sheet with the service account
1. Open google_credentials.json and copy the `client_email` value
   (it looks like: lea-sourcing-tool@your-project.iam.gserviceaccount.com)
2. Open the LEA sourcing spreadsheet
3. Click Share → paste the service account email → set role to **Editor** → Done

### Step 4 — (Optional) Set the sheet tab name
If your tab is not named "Sheet1", add this to your .env file:
```
GOOGLE_SHEET_TAB=Sourcing Outbound Call Tracker
```

### Step 5 — Install dependencies
```bash
cd sourcing-tool/backend
pip install gspread google-auth
```

## That's it!
The "Link to Sheet" button in the app will now append rows to your spreadsheet.
