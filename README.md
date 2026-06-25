# Lobby Scout Pro Live v2.5 — One-Message Dashboard

All project files stay directly in one folder. Upload every extracted file directly to the root of your GitHub repository.

## New in v2.5

- The full lobby is rendered into **one dashboard image attached to one Discord message**.
- Duos displays all returned teams in two columns, so a 50-team lobby no longer needs Previous/Next pages.
- Solos, Trios, and Squads automatically choose enough columns to show the complete returned lobby in one image.
- Each player keeps their individual PR whether their team is alive or eliminated.
- Each row also shows combined team PR, alive/out status, placement, elimination time, eliminations, and points when Tracker provides them.
- Newly eliminated teams are highlighted as **NEW OUT** on the next refresh.
- The sort menu, Refresh button, and Stop Live button remain available.
- Live polling remains fixed at **10 seconds** and automatically stops after **27 minutes**.

Discord may scale the image to fit the app window. On a small screen you may need to tap the image to read tiny text, but there is no lobby pagination and all returned teams are contained in the same Discord message.

## PR behavior

PR is collected from Fortnite Tracker's Stats table, live JSON responses, and available fallbacks. Alive/dead status does not remove PR: eliminated teams keep the same individual and combined PR values in the dashboard.

A player may still show `0 PR` when Fortnite Tracker itself has no PR value for that account.

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

## Commands

```text
/players screenshot:<image> mode:<Solos|Duos|Trios|Squads>
/players_id match_id_or_url:<id-or-link> mode:<Solos|Duos|Trios|Squads>
/players_live screenshot:<image> mode:<Solos|Duos|Trios|Squads>
/players_live_id match_id_or_url:<id-or-link> mode:<Solos|Duos|Trios|Squads>
/bot_status
```

## Railway update

1. Extract the ZIP.
2. Replace the old files in the root of your GitHub repository with every extracted file.
3. Commit the update.
4. In Railway, redeploy with the build cache cleared.
