# Lobby Scout Pro

A safe external Discord bot for Fortnite tournament session pages.

It does **not** read Fortnite memory, sniff packets, inject into the game, or touch anti-cheat. It only uses:

1. A screenshot or pasted Fortnite Tracker session ID/link.
2. OCR to extract the 32-character session ID from the screenshot.
3. Fortnite Tracker's public session page for roster/stats/timeline data.
4. Optional TRN API PR fallback if you provide a working `TRN_API_KEY`.

## Main features

- `/players` — one-time screenshot lookup, sorted by PR.
- `/players_id` — one-time pasted ID/link lookup, sorted by PR.
- `/players_live` — screenshot lookup that starts a live Discord message.
- `/players_live_id` — pasted ID/link lookup that starts a live Discord message.
- Live message updates every few seconds.
- Shows each player's individual PR next to their name when Fortnite Tracker exposes it.
- Shows each duo/team's combined PR.
- Tracks eliminated/dead teams when Fortnite Tracker exposes team elimination/placement data.
- Shows newly eliminated teams.
- Discord controls:
  - Sort by alive first + PR
  - Sort by most PR
  - Sort by recent eliminations
  - Sort by death order
  - Sort by placement
  - Sort by most eliminations
  - Refresh now
  - Stop live monitor

## Discord commands

### One-time lookup

```txt
/players screenshot:<image> region:NAC platform:pc top:25
/players_id match_id_or_url:<id-or-link> region:NAC platform:pc top:25
```

### Live monitor

```txt
/players_live screenshot:<image> region:NAC platform:pc top:25 poll_seconds:10 max_minutes:30
/players_live_id match_id_or_url:<id-or-link> region:NAC platform:pc top:25 poll_seconds:10 max_minutes:30
```

`poll_seconds` has a hard minimum of 5 seconds. For Railway, 10-15 seconds is safer than hammering the page every second.

Live team lines look like this:

```txt
01 🟢 PlayerA (9,100 PR) / PlayerB (4,200 PR) — Team: 13,300 PR • alive • 3 elim • 12 pts
02 💀 PlayerC (7,000 PR) / PlayerD (2,500 PR) — Team: 9,500 PR • placed #38 at 6m12s
```

## Railway setup

1. Create a Discord bot at https://discord.com/developers/applications
2. Go to **Bot** tab, create/reset token, copy it.
3. Go to **OAuth2 > URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`, `Use Slash Commands`
4. Invite the bot to your server.
5. Upload this folder to a GitHub repo.
6. Railway: **New Project > Deploy from GitHub Repo**.
7. Add variables in Railway:
   - `DISCORD_TOKEN` = your bot token
   - `DISCORD_GUILD_ID` = your server ID, optional but recommended
   - `DEFAULT_REGION` = `NAC`, `NAE`, `NAW`, `EU`, `BR`, `OCE`, `ASIA`, or `ME`
   - `DEFAULT_PLATFORM` = `pc`
   - `LIVE_POLL_SECONDS` = `10`
   - `LIVE_MAX_MINUTES` = `30`
   - `LIVE_TOP_TEAMS` = `25`
   - `TRN_API_KEY` = optional Tracker API key
8. Deploy.

## Important notes

- Tournament/session pages are the intended target. Public/pubs matches usually will not have a public roster page.
- Fortnite Tracker session pages are dynamic. This bot uses headless Chromium through Playwright so it can read the rendered page.
- Live data is only as fast/complete as Fortnite Tracker makes it. The bot will keep updating one message, but it cannot force Tracker to reveal teams before Tracker has processed them.
- If Fortnite Tracker changes its page layout or blocks cloud browsers, the scraper may need an update.
- Railway restarts/redeploys clear active live monitors because monitor state is in memory.
