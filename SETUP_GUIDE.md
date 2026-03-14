# 🚴 Race Analyzer — Deployment Guide
## One-time setup (~20 minutes)

---

### WHAT YOU NEED
- A phone or computer with a browser
- Your Anthropic API key (from claude.ai — you already have one)
- Free accounts on GitHub and Railway (both take 2 minutes to create)

---

### STEP 1 — Create a GitHub account
1. Go to **github.com**
2. Click "Sign up" — use any email
3. Verify your email and log in

---

### STEP 2 — Upload the app to GitHub
1. Once logged in, click the **+** button (top right) → "New repository"
2. Name it: `cycling-analyzer`
3. Make sure it's set to **Public**
4. Click **"Create repository"**
5. On the next page, click **"uploading an existing file"**
6. Drag and drop ALL the files from the zip I gave you into the upload box:
   - `app.py`
   - `requirements.txt`
   - `Procfile`
   - `railway.json`
   - the `templates` folder containing `index.html`
7. Click **"Commit changes"**

---

### STEP 3 — Create a Railway account
1. Go to **railway.app**
2. Click "Start a New Project" → "Login with GitHub"
3. Authorize Railway to access your GitHub

---

### STEP 4 — Deploy the app
1. In Railway, click **"New Project"**
2. Click **"Deploy from GitHub repo"**
3. Select your `cycling-analyzer` repository
4. Railway will start building automatically (takes about 2 minutes)
5. When it says **"Active"** — you're nearly done!

---

### STEP 5 — Add your API key
This is the only slightly technical step:
1. In Railway, click on your project
2. Click the **"Variables"** tab
3. Click **"New Variable"**
4. Name: `ANTHROPIC_API_KEY`
5. Value: paste your Anthropic API key (find it at console.anthropic.com → API Keys)
6. Click **"Add"** — Railway will restart automatically

---

### STEP 6 — Get your URL
1. In Railway, click the **"Settings"** tab
2. Under "Domains", click **"Generate Domain"**
3. You'll get a URL like: `cycling-analyzer-production.up.railway.app`
4. **Bookmark this on your phone!**

---

### USING IT AT A RACE
1. Open the bookmarked URL on your phone
2. Tap the upload zone → take photo(s) of the start list
3. Tap "Analyze Start List"
4. Wait 2-3 minutes → get your ranked table with wins/podiums/top10s

---

### COSTS
- **GitHub**: Free
- **Railway**: Free tier gives you $5/month credit — more than enough for occasional use
- **Anthropic API**: Very cheap — each analysis costs roughly $0.05-0.10

---

### PROBLEMS?
If something goes wrong, the most common fix is:
- Check that your ANTHROPIC_API_KEY is set correctly in Railway Variables
- Make sure the key starts with `sk-ant-`

---
