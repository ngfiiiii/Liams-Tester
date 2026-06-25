# Lobby Scout Pro Live v2.6 — Stable 27-Minute Message Editing

All project files are directly in one folder. Upload every extracted file to the root of your GitHub repository.

## Fixed in v2.6

This version fixes both errors:

```text
discord.errors.NotFound: 404 Not Found (error code: 10008): Unknown Message
AttributeError: 'LiveLobbyView' object has no attribute 'disable_all_items'
```

### Why the 404 happened

The old live dashboard was created as a Discord interaction follow-up/webhook message. Interaction tokens are temporary, so a long-running monitor could lose the ability to edit that message before the full 27-minute session ended.

The live dashboard is now sent as a normal bot/channel message. It can be edited throughout the entire monitor without depending on the temporary slash-command webhook token.

### Other stability changes

- If a user or moderator deletes the live dashboard, the monitor stops quietly instead of throwing an error every 10 seconds.
- Finalization no longer relies on a custom `disable_all_items()` method; every component is disabled through a safe shared helper.
- Clicking **Stop Live** no longer causes the monitor to finalize twice.
- The monitor still refreshes every 10 seconds and stops automatically after 27 minutes.
- The one-message full-lobby image, PR, alive/dead status, sorting, and elimination tracking are unchanged.

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
2. Replace all old files in the root of your GitHub repository with the extracted files.
3. Commit the changes.
4. In Railway, choose **Redeploy with cleared build cache**.
5. Start a brand-new `/players_live` or `/players_live_id` monitor. Existing messages from the older deployment cannot be converted.

The PyNaCl voice warning is harmless because this bot does not use Discord voice.
