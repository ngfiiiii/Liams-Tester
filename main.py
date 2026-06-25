from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from aiohttp import web

from config import settings
from ocr import extract_session_id_from_image, normalize_session_id
from tracker import scrape_session, fill_missing_pr, LobbyScoutError
from models import PlayerResult, TeamResult

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s")
log = logging.getLogger("lobby-scout")

VALID_REGIONS = ["NAC", "NAE", "NAW", "EU", "BR", "OCE", "ASIA", "ME"]
VALID_PLATFORMS = ["pc", "console", "mobile"]
SORT_CHOICES = {
    "alive_pr": "Alive First + PR",
    "pr": "Most PR",
    "recent_deaths": "Recent Eliminations",
    "death_order": "Death Order",
    "placement": "Placement",
    "elims": "Most Eliminations",
}

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Message id -> active monitor. This is in-memory, so a Railway redeploy/restart stops old monitors.
active_monitors: dict[int, "LiveMonitor"] = {}


def fmt_num(n: float | int | None) -> str:
    if n is None:
        return "—"
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return f"{n:,.0f}" if isinstance(n, (int, float)) else str(n)


def team_key(team: TeamResult) -> str:
    if team.players:
        return "|".join(sorted(p.lower() for p in team.players))
    if team.placement is not None:
        return f"place:{team.placement}"
    return f"unknown:{id(team)}"


def summarize_status(teams: list[TeamResult]) -> tuple[int, int, int]:
    known = len(teams)
    eliminated = sum(1 for t in teams if t.is_eliminated)
    alive = max(0, known - eliminated)
    return known, alive, eliminated


def sort_teams(teams: list[TeamResult], mode: str) -> list[TeamResult]:
    if mode == "pr":
        return sorted(teams, key=lambda t: t.combined_pr, reverse=True)
    if mode == "recent_deaths":
        return sorted(
            teams,
            key=lambda t: (
                not t.is_eliminated,
                -(t.eliminated_order or 0),
                -(t.placement or 0),
                -t.combined_pr,
            ),
        )
    if mode == "death_order":
        return sorted(
            teams,
            key=lambda t: (
                not t.is_eliminated,
                t.eliminated_order or 9999,
                -(t.placement or 0),
            ),
        )
    if mode == "placement":
        return sorted(teams, key=lambda t: (t.placement is None, t.placement or 9999))
    if mode == "elims":
        return sorted(teams, key=lambda t: (t.eliminations or 0, t.combined_pr), reverse=True)
    # Default: alive first, then strongest PR.
    return sorted(teams, key=lambda t: (t.is_eliminated, -t.combined_pr, t.placement or 9999))


def player_pr_map(players: list[PlayerResult]) -> dict[str, float]:
    return {p.name.lower(): float(p.pr or 0) for p in players}


def team_names_with_pr(team: TeamResult, pr_by_name: dict[str, float] | None = None) -> str:
    """Render each player name with their own PR next to it.

    Example: PlayerA `9,100 PR` / PlayerB `4,200 PR`
    Falls back to `0 PR` when Tracker has not exposed a player PR yet.
    """
    if not team.players:
        return "Unknown team"
    pr_by_name = pr_by_name or {}
    parts = []
    for name in team.players:
        individual_pr = pr_by_name.get(name.lower(), 0.0)
        parts.append(f"**{name}** `({fmt_num(individual_pr)} PR)`")
    return " / ".join(parts)


def team_line(rank: int, team: TeamResult, pr_by_name: dict[str, float] | None = None) -> str:
    icon = "💀" if team.is_eliminated else "🟢"
    names = team_names_with_pr(team, pr_by_name)
    pr = fmt_num(team.combined_pr)
    status = "alive"
    if team.is_eliminated:
        place = f"#{team.placement}" if team.placement is not None else "#?"
        when = f" at {team.eliminated_at}" if team.eliminated_at else ""
        status = f"placed {place}{when}"
    extra = []
    if team.eliminations is not None:
        extra.append(f"{team.eliminations} elim")
    if team.points is not None:
        extra.append(f"{team.points} pts")
    extra_txt = f" • {' • '.join(extra)}" if extra else ""
    return f"`{rank:02}` {icon} {names} — **Team:** `{pr} PR` • {status}{extra_txt}"


def player_line(rank: int, player: PlayerResult) -> str:
    kills = f" • {player.kills} elim" if player.kills is not None else ""
    dmg = f" • {fmt_num(player.damage)} dmg" if player.damage is not None else ""
    place = f" • place #{player.placement}" if player.placement is not None else ""
    return f"`{rank:02}` **{player.name}** — `{fmt_num(player.pr)} PR`{kills}{dmg}{place}"


def make_embed(
    session_id: str,
    url: str,
    players: list[PlayerResult],
    teams: list[TeamResult],
    top: int,
    region: str,
    platform: str,
) -> discord.Embed:
    top = max(1, min(top, settings.max_rows))
    embed = discord.Embed(
        title="Lobby Scout Results",
        description=f"[`{session_id}`]({url}) • Region `{region}` • Platform `{platform}`",
        color=0x5865F2,
    )

    pr_by_name = player_pr_map(players)

    if teams:
        lines = [team_line(i, t, pr_by_name) for i, t in enumerate(sort_teams(teams, "pr")[:top], start=1)]
        embed.add_field(
            name=f"Top Teams by Combined PR ({min(top, len(teams))}/{len(teams)})",
            value="\n".join(lines)[:3900] or "No teams found.",
            inline=False,
        )

    if players:
        lines = [player_line(i, p) for i, p in enumerate(players[:top], start=1)]
        embed.add_field(name=f"Top Players by PR ({min(top, len(players))}/{len(players)})", value="\n".join(lines)[:3900], inline=False)

    if not players and not teams:
        embed.add_field(
            name="No roster found",
            value=(
                "The session page loaded, but I could not parse roster/stats yet. "
                "This can happen if the match has not processed, the ID is wrong, or Fortnite Tracker changed the page."
            ),
            inline=False,
        )

    embed.set_footer(text="External OCR + Fortnite Tracker page parsing only. No memory reading or packet sniffing.")
    return embed


@dataclass
class LiveSnapshot:
    session_id: str
    url: str
    players: list[PlayerResult]
    teams: list[TeamResult]


class LiveMonitor:
    def __init__(
        self,
        message: discord.Message,
        session_id_or_url: str,
        region: str,
        platform: str,
        top: int,
        poll_seconds: int,
        max_minutes: int,
    ):
        self.message = message
        self.session_id_or_url = session_id_or_url
        self.session_id = normalize_session_id(session_id_or_url) or session_id_or_url
        self.region = region
        self.platform = platform
        self.top = max(5, min(top, settings.max_rows))
        self.poll_seconds = max(5, poll_seconds)
        self.max_minutes = max(1, max_minutes)
        self.sort_mode = "alive_pr"
        self.started_at = time.monotonic()
        self.stopped = False
        self.task: Optional[asyncio.Task] = None
        self.snapshot: Optional[LiveSnapshot] = None
        self.dead_keys: set[str] = set()
        self.new_deaths: list[TeamResult] = []
        self.last_error: Optional[str] = None
        self.refresh_count = 0
        self.view: Optional[LiveLobbyView] = None

    async def start(self) -> None:
        self.view = LiveLobbyView(self)
        self.task = asyncio.create_task(self.run(), name=f"live-monitor-{self.session_id}")
        active_monitors[self.message.id] = self

    def stop(self) -> None:
        self.stopped = True

    async def run(self) -> None:
        deadline = self.started_at + (self.max_minutes * 60)
        try:
            while not self.stopped and time.monotonic() < deadline:
                await self.refresh_and_edit()
                await asyncio.sleep(self.poll_seconds)
        finally:
            self.stopped = True
            active_monitors.pop(self.message.id, None)
            try:
                if self.view:
                    self.view.disable_all_items()
                await self.edit_message(final=True)
            except Exception:
                log.exception("Failed to finalize live monitor message")

    async def refresh(self) -> None:
        data = await scrape_session(self.session_id_or_url)
        players = await fill_missing_pr(data["players"], self.region, self.platform)
        pr_by_name = {p.name.lower(): p.pr for p in players}
        for team in data["teams"]:
            team.combined_pr = sum(pr_by_name.get(name.lower(), 0.0) for name in team.players)

        teams = data["teams"]
        current_dead = {team_key(t) for t in teams if t.is_eliminated}
        if self.refresh_count == 0:
            self.new_deaths = []
        else:
            new_keys = current_dead - self.dead_keys
            self.new_deaths = [t for t in teams if team_key(t) in new_keys]
            self.new_deaths = sort_teams(self.new_deaths, "recent_deaths")[:8]
        self.dead_keys = current_dead
        self.refresh_count += 1
        self.last_error = None
        self.snapshot = LiveSnapshot(
            session_id=data["session_id"],
            url=data["url"],
            players=players,
            teams=teams,
        )

    async def refresh_and_edit(self) -> None:
        try:
            await self.refresh()
        except Exception as exc:
            log.exception("Live refresh failed")
            self.last_error = f"{type(exc).__name__}: {exc}"
        await self.edit_message()

    async def edit_message(self, final: bool = False) -> None:
        embed = make_live_embed(self, final=final)
        await self.message.edit(embed=embed, view=self.view)


class SortSelect(discord.ui.Select):
    def __init__(self, monitor: LiveMonitor):
        self.monitor = monitor
        options = [
            discord.SelectOption(label=label, value=value, default=(monitor.sort_mode == value))
            for value, label in SORT_CHOICES.items()
        ]
        super().__init__(placeholder="Sort lobby…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.monitor.sort_mode = self.values[0]
        if self.monitor.view:
            self.monitor.view.refresh_select_options()
        await interaction.response.edit_message(embed=make_live_embed(self.monitor), view=self.monitor.view)


class RefreshButton(discord.ui.Button):
    def __init__(self, monitor: LiveMonitor):
        super().__init__(style=discord.ButtonStyle.primary, label="Refresh Now", emoji="🔄")
        self.monitor = monitor

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False)
        await self.monitor.refresh_and_edit()


class StopButton(discord.ui.Button):
    def __init__(self, monitor: LiveMonitor):
        super().__init__(style=discord.ButtonStyle.danger, label="Stop Live", emoji="🛑")
        self.monitor = monitor

    async def callback(self, interaction: discord.Interaction) -> None:
        self.monitor.stop()
        if self.monitor.view:
            self.monitor.view.disable_all_items()
        await interaction.response.edit_message(embed=make_live_embed(self.monitor, final=True), view=self.monitor.view)


class LiveLobbyView(discord.ui.View):
    def __init__(self, monitor: LiveMonitor):
        super().__init__(timeout=monitor.max_minutes * 60 + 120)
        self.monitor = monitor
        self.add_item(SortSelect(monitor))
        self.add_item(RefreshButton(monitor))
        self.add_item(StopButton(monitor))

    def refresh_select_options(self) -> None:
        # Rebuild the select so Discord shows the new default option.
        for item in list(self.children):
            if isinstance(item, SortSelect):
                self.remove_item(item)
        self.add_item(SortSelect(self.monitor))

    async def on_timeout(self) -> None:
        self.monitor.stop()
        self.disable_all_items()
        try:
            await self.monitor.edit_message(final=True)
        except Exception:
            pass


def make_live_embed(monitor: LiveMonitor, final: bool = False) -> discord.Embed:
    snap = monitor.snapshot
    url = f"https://fortnitetracker.com/events/sessions/{monitor.session_id}"
    title_suffix = "Ended" if final or monitor.stopped else "Live"
    embed = discord.Embed(
        title=f"Lobby Scout Live • {title_suffix}",
        description=f"[`{monitor.session_id}`]({url}) • Region `{monitor.region}` • Platform `{monitor.platform}`",
        color=0x2ECC71 if not final and not monitor.stopped else 0x95A5A6,
    )

    elapsed = int(time.monotonic() - monitor.started_at)
    max_seconds = monitor.max_minutes * 60

    if not snap:
        embed.add_field(name="Status", value="Starting live monitor…", inline=False)
    else:
        teams = snap.teams
        players = snap.players
        pr_by_name = player_pr_map(players)
        known, alive, eliminated = summarize_status(teams)
        sort_label = SORT_CHOICES.get(monitor.sort_mode, monitor.sort_mode)
        embed.add_field(
            name="Live Summary",
            value=(
                f"**Known teams:** `{known}` • **Alive/incomplete:** `{alive}` • **Eliminated:** `{eliminated}`\n"
                f"**Sort:** `{sort_label}` • **Refresh:** every `{monitor.poll_seconds}s` • "
                f"**Timer:** `{elapsed//60}:{elapsed%60:02}` / `{max_seconds//60}:{max_seconds%60:02}`"
            ),
            inline=False,
        )

        if monitor.new_deaths:
            lines = [team_line(i, t, pr_by_name) for i, t in enumerate(monitor.new_deaths[:5], start=1)]
            embed.add_field(name="🚨 Newly Eliminated", value="\n".join(lines)[:1000], inline=False)

        sorted_list = sort_teams(teams, monitor.sort_mode)
        lines = [team_line(i, t, pr_by_name) for i, t in enumerate(sorted_list[:monitor.top], start=1)]
        embed.add_field(
            name=f"Teams ({min(monitor.top, len(teams))}/{len(teams)})",
            value="\n".join(lines)[:3900] or "No teams found yet. The session may still be processing.",
            inline=False,
        )

        top_players = sorted(players, key=lambda p: p.pr, reverse=True)[:5]
        if top_players:
            embed.add_field(
                name="Highest PR Players",
                value="\n".join(player_line(i, p) for i, p in enumerate(top_players, start=1))[:1000],
                inline=False,
            )

    if monitor.last_error:
        embed.add_field(name="Last refresh warning", value=f"`{monitor.last_error[:900]}`", inline=False)

    embed.set_footer(text="Live page polling only. No Fortnite memory reading, injection, or packet sniffing.")
    return embed


async def handle_session_lookup(
    interaction: discord.Interaction,
    session_id_or_url: str,
    region: str,
    platform: str,
    top: int,
    detected_from_ocr: bool = False,
) -> None:
    region = (region or settings.default_region).upper()
    platform = (platform or settings.default_platform).lower()
    top = max(1, min(int(top or settings.top_n), settings.max_rows))

    await interaction.followup.send(
        f"Found session ID `{normalize_session_id(session_id_or_url) or session_id_or_url}`. Fetching Fortnite Tracker roster…",
        ephemeral=True,
    )

    data = await scrape_session(session_id_or_url)
    players = data["players"]
    teams = data["teams"]

    # Optional TRN PR fallback only for missing player PR values.
    players = await fill_missing_pr(players, region, platform)

    # Recalculate teams after PR fallback.
    pr_by_name = {p.name.lower(): p.pr for p in players}
    for t in teams:
        t.combined_pr = sum(pr_by_name.get(name.lower(), 0.0) for name in t.players)
    teams = sorted(teams, key=lambda t: t.combined_pr, reverse=True)

    embed = make_embed(
        session_id=data["session_id"],
        url=data["url"],
        players=players,
        teams=teams,
        top=top,
        region=region,
        platform=platform,
    )
    if detected_from_ocr:
        embed.add_field(name="OCR", value="Session ID was extracted from your screenshot.", inline=False)
    await interaction.followup.send(embed=embed)


async def extract_id_or_reply(interaction: discord.Interaction, screenshot: discord.Attachment) -> Optional[str]:
    if not screenshot.content_type or not screenshot.content_type.startswith("image/"):
        await interaction.followup.send("Attach a normal screenshot image like PNG/JPG/WebP.", ephemeral=True)
        return None
    image_bytes = await screenshot.read()
    session_id, debug_text = extract_session_id_from_image(image_bytes)
    if not session_id:
        short_debug = (debug_text or "").replace("`", "")[:700]
        await interaction.followup.send(
            "I couldn’t read a valid 32-character session ID from that screenshot. "
            "Crop closer to the top-right ID or use the ID command with the pasted Fortnite Tracker link/ID.\n\n"
            f"OCR saw: ```{short_debug or 'nothing readable'}```",
            ephemeral=True,
        )
        return None
    return session_id


@tree.command(name="players", description="Upload a screenshot of the top-right Fortnite match/session ID and rank the lobby by PR.")
@app_commands.describe(
    screenshot="Screenshot containing the top-right match/session ID",
    region="Fortnite event region, default from env. Example: NAC, NAE, EU",
    platform="pc, console, or mobile",
    top="How many players/teams to show",
)
@app_commands.choices(
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players(
    interaction: discord.Interaction,
    screenshot: discord.Attachment,
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
    top: Optional[int] = None,
):
    await interaction.response.defer(thinking=True)
    try:
        session_id = await extract_id_or_reply(interaction, screenshot)
        if not session_id:
            return
        await handle_session_lookup(
            interaction,
            session_id,
            region.value if region else settings.default_region,
            platform.value if platform else settings.default_platform,
            top or settings.top_n,
            detected_from_ocr=True,
        )
    except LobbyScoutError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}", ephemeral=True)
    except Exception as exc:
        log.exception("/players failed")
        await interaction.followup.send(f"Unexpected error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="players_id", description="Paste a Fortnite Tracker session URL or 32-character match/session ID.")
@app_commands.describe(
    match_id_or_url="Example: https://fortnitetracker.com/events/sessions/<id>",
    region="Fortnite event region, default from env. Example: NAC, NAE, EU",
    platform="pc, console, or mobile",
    top="How many players/teams to show",
)
@app_commands.choices(
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players_id(
    interaction: discord.Interaction,
    match_id_or_url: str,
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
    top: Optional[int] = None,
):
    await interaction.response.defer(thinking=True)
    try:
        await handle_session_lookup(
            interaction,
            match_id_or_url,
            region.value if region else settings.default_region,
            platform.value if platform else settings.default_platform,
            top or settings.top_n,
            detected_from_ocr=False,
        )
    except LobbyScoutError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}", ephemeral=True)
    except Exception as exc:
        log.exception("/players_id failed")
        await interaction.followup.send(f"Unexpected error: `{type(exc).__name__}: {exc}`", ephemeral=True)


async def start_live_monitor(
    interaction: discord.Interaction,
    session_id_or_url: str,
    region: str,
    platform: str,
    top: int,
    poll_seconds: int,
    max_minutes: int,
    from_ocr: bool,
) -> None:
    region = (region or settings.default_region).upper()
    platform = (platform or settings.default_platform).lower()
    top = max(5, min(int(top or settings.live_top_teams), settings.max_rows))
    poll_seconds = max(5, int(poll_seconds or settings.live_poll_seconds))
    max_minutes = max(1, min(int(max_minutes or settings.live_max_minutes), 120))
    session_id = normalize_session_id(session_id_or_url)
    if not session_id:
        await interaction.followup.send("That does not look like a valid Fortnite Tracker session/match ID.", ephemeral=True)
        return

    message = await interaction.followup.send(
        embed=discord.Embed(
            title="Lobby Scout Live • Starting",
            description=f"[`{session_id}`](https://fortnitetracker.com/events/sessions/{session_id}) • preparing live monitor…",
            color=0x2ECC71,
        ),
        wait=True,
    )
    monitor = LiveMonitor(
        message=message,
        session_id_or_url=session_id_or_url,
        region=region,
        platform=platform,
        top=top,
        poll_seconds=poll_seconds,
        max_minutes=max_minutes,
    )
    await monitor.start()
    if from_ocr:
        await interaction.followup.send("OCR found the session ID and the live monitor is running.", ephemeral=True)


@tree.command(name="players_live", description="Live monitor a Fortnite Tracker session from a screenshot ID; updates one message as teams die.")
@app_commands.describe(
    screenshot="Screenshot containing the top-right match/session ID",
    region="Fortnite event region, default from env. Example: NAC, NAE, EU",
    platform="pc, console, or mobile",
    top="How many teams to show",
    poll_seconds="How often to refresh. Minimum 5 seconds. Default from env is 10.",
    max_minutes="How long to keep updating. Default from env is 30.",
)
@app_commands.choices(
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players_live(
    interaction: discord.Interaction,
    screenshot: discord.Attachment,
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
    top: Optional[int] = None,
    poll_seconds: Optional[int] = None,
    max_minutes: Optional[int] = None,
):
    await interaction.response.defer(thinking=True)
    try:
        session_id = await extract_id_or_reply(interaction, screenshot)
        if not session_id:
            return
        await start_live_monitor(
            interaction,
            session_id,
            region.value if region else settings.default_region,
            platform.value if platform else settings.default_platform,
            top or settings.live_top_teams,
            poll_seconds or settings.live_poll_seconds,
            max_minutes or settings.live_max_minutes,
            from_ocr=True,
        )
    except Exception as exc:
        log.exception("/players_live failed")
        await interaction.followup.send(f"Unexpected error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="players_live_id", description="Live monitor a Fortnite Tracker session URL/ID; updates one message as teams die.")
@app_commands.describe(
    match_id_or_url="Example: https://fortnitetracker.com/events/sessions/<id>",
    region="Fortnite event region, default from env. Example: NAC, NAE, EU",
    platform="pc, console, or mobile",
    top="How many teams to show",
    poll_seconds="How often to refresh. Minimum 5 seconds. Default from env is 10.",
    max_minutes="How long to keep updating. Default from env is 30.",
)
@app_commands.choices(
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players_live_id(
    interaction: discord.Interaction,
    match_id_or_url: str,
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
    top: Optional[int] = None,
    poll_seconds: Optional[int] = None,
    max_minutes: Optional[int] = None,
):
    await interaction.response.defer(thinking=True)
    try:
        await start_live_monitor(
            interaction,
            match_id_or_url,
            region.value if region else settings.default_region,
            platform.value if platform else settings.default_platform,
            top or settings.live_top_teams,
            poll_seconds or settings.live_poll_seconds,
            max_minutes or settings.live_max_minutes,
            from_ocr=False,
        )
    except Exception as exc:
        log.exception("/players_live_id failed")
        await interaction.followup.send(f"Unexpected error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="bot_status", description="Check whether Lobby Scout Pro is online.")
async def bot_status(interaction: discord.Interaction):
    await interaction.response.send_message(f"Lobby Scout Pro is online ✅ Active live monitors: `{len(active_monitors)}`", ephemeral=True)


@client.event
async def on_ready():
    log.info("Logged in as %s", client.user)
    try:
        if settings.discord_guild_id:
            guild = discord.Object(id=int(settings.discord_guild_id))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %s", len(synced), settings.discord_guild_id)
        else:
            synced = await tree.sync()
            log.info("Synced %d global slash commands", len(synced))
    except Exception:
        log.exception("Slash command sync failed")


async def health(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "bot": str(client.user) if client.user else None,
        "active_live_monitors": len(active_monitors),
    })


async def start_health_server() -> None:
    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/health", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    log.info("Health server listening on port %s", settings.port)


async def main():
    if not settings.discord_token:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to your .env or Railway Variables.")
    await start_health_server()
    await client.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
