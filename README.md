# Opportunity Scout

Scans opportunity sites and search feeds every day for internships, fellowships,
scholarships, MOOCs, certificates and research-writing programmes for political
science graduates. It scores each item, reads the deadline, removes duplicates,
and posts a digest to your group.

## What you get each day
- A web page (docs/index.html) with a search box, grouped by type, with deadlines
- "Closing soon" list (next 14 days) built from everything it has ever found
- A Telegram message and/or email digest
- data/all_opportunities.csv, a running list you can open in Excel or Sheets

## Set it up (about 15 minutes, free, runs without your laptop)
1. Create a free GitHub account, make a new repository, upload everything in this folder.
2. Settings > Pages > deploy from branch `main`, folder `/docs`. Your group gets one link.
3. Telegram (best for a group): message @BotFather, send /newbot, copy the token.
   Add the bot to your group or channel, then get the chat id (message the group,
   open https://api.telegram.org/bot<TOKEN>/getUpdates and read "chat":{"id":...}).
4. Settings > Secrets and variables > Actions > add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   Email is optional: SMTP_HOST, SMTP_USER, SMTP_PASS, MAIL_TO (comma separated).
5. Actions tab > daily-opportunity-scout > Run workflow. After that it runs by itself at 7am Nigeria time.
   WhatsApp has no free official bot, so post the Pages link in the WhatsApp group, or use Telegram.

## Run on your own computer instead
    pip install -r requirements.txt
    python scout.py --deep --no-notify
    open docs/index.html

## Make it yours (config.yaml)
- Add or remove feeds, search queries and scoring words. No code needed.
- `min_score` controls how strict it is. Raise it for fewer, better results.
- Scores: field match + research-writing boost + free/funded + Africa/Nigeria eligibility,
  minus fees, nationality restrictions and senior-level roles.

## Know its limits
- Some feed URLs may change or block automated access. The run log and the digest footer
  list any source that failed; fix the URL or switch it off.
- Deadlines are read from text, so a few will be missing or wrong. Always confirm on the official page before applying.
- Search results are leads. Watch for fake or fee-charging "scholarships".
