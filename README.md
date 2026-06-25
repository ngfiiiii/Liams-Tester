# Lobby Scout Pro Live v2.3 — Automatic Full-Lobby Build

All project files stay directly in one folder. Upload every extracted file directly to the root of your GitHub repository.

## Changes in this build

- Live refresh is automatically fixed at **10 seconds**.
- Live monitoring automatically stops after **27 minutes**.
- Removed the old `top`, `poll_seconds`, and `max_minutes` command inputs.
- Every lobby command now asks for **Solos, Duos, Trios, or Squads**.
- The selected format automatically targets the full lobby:
  - Solos: up to 100 players
  - Duos: up to 50 teams
  - Trios: up to 34 teams
  - Squads: up to 25 teams
- The monitor tracks every team returned from the first refresh.
- Previous/Next buttons let you browse the full lobby without exceeding Discord embed limits.
- Every player still shows individual PR, with combined team PR and alive/dead state.

## Railway variables

Required:

```text
DISCORD_TOKEN=your_bot_token
```

Recommended/optional:

```text
DISCORD_GUILD_ID=your_server_id
DEFAULT_REGION=NAC
DEFAULT_PLATFORM=pc
TRN_API_KEY=
```

You no longer need `LIVE_POLL_SECONDS`, `LIVE_MAX_MINUTES`, `LIVE_TOP_TEAMS`, `TOP_N`, or `MAX_ROWS`.

## Commands

```text
/players screenshot:<image> mode:<Solos|Duos|Trios|Squads>
/players_id match_id_or_url:<id-or-link> mode:<Solos|Duos|Trios|Squads>
/players_live screenshot:<image> mode:<Solos|Duos|Trios|Squads>
/players_live_id match_id_or_url:<id-or-link> mode:<Solos|Duos|Trios|Squads>
/bot_status
```

### Example

```text
/players_live screenshot:<image> mode:Duos region:NAC platform:pc
```

The bot then checks Fortnite Tracker every 10 seconds for 27 minutes, tracks all returned duo teams, shows individual PR and team PR, marks eliminations, and provides sorting plus page controls.
