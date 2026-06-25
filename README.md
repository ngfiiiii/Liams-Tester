# Lobby Scout Pro Live — Flat Railway Build

This build has **no nested code folders**. Upload every file directly to the root of one GitHub repository.

Your repository must look like this:

```text
main.py
config.py
models.py
ocr.py
tracker.py
Dockerfile
Procfile
requirements.txt
README.md
.env.example
.dockerignore
```

## Railway variables

Required:

```text
DISCORD_TOKEN=your_bot_token
```

Recommended:

```text
DISCORD_GUILD_ID=your_server_id
DEFAULT_REGION=NAC
DEFAULT_PLATFORM=pc
LIVE_POLL_SECONDS=10
LIVE_MAX_MINUTES=30
LIVE_TOP_TEAMS=25
TRN_API_KEY=
```

## Deploy

1. Extract the ZIP.
2. Create a GitHub repository.
3. Upload all extracted files directly to the repository root. Do not upload the ZIP itself.
4. In Railway, deploy from that GitHub repository.
5. Leave Railway's Root Directory blank.
6. Add the variables above and redeploy.
7. Test `/bot_status` in Discord.

## Commands

- `/players` — screenshot, one-time lookup
- `/players_id` — pasted session ID/link, one-time lookup
- `/players_live` — screenshot, live updating lobby
- `/players_live_id` — pasted ID/link, live updating lobby
- `/bot_status` — bot health check
