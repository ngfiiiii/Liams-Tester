from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands

from config import settings
from models import PlayerResult, TeamResult
from ocr import extract_session_id_from_image, normalize_session_id
from tracker import LobbyScoutError, fill_missing_pr, scrape_session

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s")
log = logging.getLogger("lobby-scout")

VALID_REGIONS = ["NAC", "NAE", "NAW", "EU", "BR", "OCE", "ASIA", "ME"]
VALID_PLATFORMS = ["pc", "console", "mobile"]

# Fixed automatically for every live command. Users no longer need to enter these.
LIVE_POLL_SECONDS = 10
LIVE_MAX_MINUTES = 27

# expected_teams is used for the progress display. The monitor always keeps every
# team returned by Fortnite Tracker, even if more than the expected count appears.
LOBBY_FORMATS = {
    "solos": {"label": "Solos", "team_size": 1, "expected_teams": 100},
    "duos": {"label": "Duos", "team_size": 2, "expected_teams": 50},
    "trios": {"label": "Trios", "team_size": 3, "expected_teams": 34},
    "squads": {"label": "Squads", "team_size": 4, "expected_teams": 25},
}

FORMAT_CHOICES = [
    app_commands.Choice(name="Solos — up to 100 players", value="solos"),
    app_commands.Choice(name="Duos — up to 50 teams", value="duos"),
    app_commands.Choice(name="Trios — up to 34 teams", value="trios"),
    app_commands.Choice(name="Squads — up to 25 teams", value="squads"),
]

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

# Message id -> active monitor. Railway restarts clear in-memory monitors.
active_monitors: dict[int, "LiveMonitor"] = {}


def fmt_num(n: float | int | None) -> str:
    if n is None:
        return "—"
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return f"{n:,.0f}" if isinstance(n, (int, float)) else str(n)


def format_info(lobby_format: str) -> dict[str, int | str]:
    return LOBBY_FORMATS.get(lobby_format, LOBBY_FORMATS["duos"])


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
    status = "alive"
    if team.is_eliminated:
        place = f"#{team.placement}" if team.placement is not None else "#?"
        when = f" @ {team.eliminated_at}" if team.eliminated_at else ""
        status = f"placed {place}{when}"
    extra = []
    if team.eliminations is not None:
        extra.append(f"{team.eliminations} elim")
    if team.points is not None:
        extra.append(f"{team.points} pts")
    extra_txt = f" • {' • '.join(extra)}" if extra else ""
    return (
        f"`{rank:02}` {icon} {names} — **Team:** `{fmt_num(team.combined_pr)} PR` "
        f"• {status}{extra_txt}"
    )


def player_line(rank: int, player: PlayerResult) -> str:
    kills = f" • {player.kills} elim" if player.kills is not None else ""
    dmg = f" • {fmt_num(player.damage)} dmg" if player.damage is not None else ""
    place = f" • place #{player.placement}" if player.placement is not None else ""
    return f"`{rank:02}` **{player.name}** — `{fmt_num(player.pr)} PR`{kills}{dmg}{place}"


def make_text_pages(lines: list[str], max_chars: int = 950) -> list[str]:
    """Split rows into Discord-safe embed field pages while preserving every row."""
    if not lines:
        return ["No teams found yet. The session may still be processing."]

    pages: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        # Discord field values are limited to 1024 characters.
        safe_line = line[:900]
        extra = len(safe_line) + (1 if current else 0)
        if current and current_len + extra > max_chars:
            pages.append("\n".join(current))
            current = [safe_line]
            current_len = len(safe_line)
        else:
            current.append(safe_line)
            current_len += extra
    if current:
        pages.append("\n".join(current))
    return pages or ["No rows found."]


def build_team_pages(
    teams: list[TeamResult],
    players: list[PlayerResult],
    sort_mode: str,
) -> list[str]:
    pr_by_name = player_pr_map(players)
    ordered = sort_teams(teams, sort_mode)
    lines = [team_line(i, team, pr_by_name) for i, team in enumerate(ordered, start=1)]
    return make_text_pages(lines)


def clamp_page(page: int, pages: list[str]) -> int:
    return max(0, min(page, max(0, len(pages) - 1)))


@dataclass
class StaticLobbyState:
    session_id: str
    url: str
    players: list[PlayerResult]
    teams: list[TeamResult]
    region: str
    platform: str
    lobby_format: str
    page: int = 0

    def pages(self) -> list[str]:
        if self.teams:
            return build_team_pages(self.teams, self.players, "pr")
        lines = [player_line(i, p) for i, p in enumerate(self.players, start=1)]
        return make_text_pages(lines)


def make_static_embed(state: StaticLobbyState, detected_from_ocr: bool = False) -> discord.Embed:
    info = format_info(state.lobby_format)
    pages = state.pages()
    state.page = clamp_page(state.page, pages)
    expected = int(info["expected_teams"])
    known = len(state.teams) if state.teams else len(state.players)
    unit = "players" if state.lobby_format == "solos" else "teams"

    embed = discord.Embed(
        title="Lobby Scout Results",
        description=(
            f"[`{state.session_id}`]({state.url}) • `{info['label']}` • "
            f"Region `{state.region}` • Platform `{state.platform}`"
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="Lobby Coverage",
        value=(
            f"Found `{known}` of up to `{expected}` {unit}. "
            "Every returned entry is included across the pages below."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"PR Ranking • Page {state.page + 1}/{len(pages)}",
        value=pages[state.page][:1024],
        inline=False,
    )
    if detected_from_ocr:
        embed.add_field(name="OCR", value="Session ID extracted from the uploaded screenshot.", inline=False)
    embed.set_footer(text="Use Previous/Next to view the full lobby. Sorted by highest combined PR.")
    return embed


class StaticPreviousButton(discord.ui.Button):
    def __init__(self, view: "StaticLobbyView"):
        super().__init__(style=discord.ButtonStyle.secondary, label="Previous", emoji="⬅️", row=0)
        self.owner_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner_view.state.page -= 1
        self.owner_view.sync_buttons()
        await interaction.response.edit_message(
            embed=make_static_embed(self.owner_view.state, self.owner_view.detected_from_ocr),
            view=self.owner_view,
        )


class StaticNextButton(discord.ui.Button):
    def __init__(self, view: "StaticLobbyView"):
        super().__init__(style=discord.ButtonStyle.secondary, label="Next", emoji="➡️", row=0)
        self.owner_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner_view.state.page += 1
        self.owner_view.sync_buttons()
        await interaction.response.edit_message(
            embed=make_static_embed(self.owner_view.state, self.owner_view.detected_from_ocr),
            view=self.owner_view,
        )


class StaticLobbyView(discord.ui.View):
    def __init__(self, state: StaticLobbyState, detected_from_ocr: bool):
        super().__init__(timeout=900)
        self.state = state
        self.detected_from_ocr = detected_from_ocr
        self.previous = StaticPreviousButton(self)
        self.next = StaticNextButton(self)
        self.add_item(self.previous)
        self.add_item(self.next)
        self.sync_buttons()

    def sync_buttons(self) -> None:
        pages = self.state.pages()
        self.state.page = clamp_page(self.state.page, pages)
        self.previous.disabled = self.state.page <= 0
        self.next.disabled = self.state.page >= len(pages) - 1


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
        lobby_format: str,
    ):
        self.message = message
        self.session_id_or_url = session_id_or_url
        self.session_id = normalize_session_id(session_id_or_url) or session_id_or_url
        self.region = region
        self.platform = platform
        self.lobby_format = lobby_format
        self.poll_seconds = LIVE_POLL_SECONDS
        self.max_minutes = LIVE_MAX_MINUTES
        self.sort_mode = "alive_pr"
        self.page = 0
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

    def pages(self) -> list[str]:
        if not self.snapshot:
            return ["Starting live monitor…"]
        return build_team_pages(self.snapshot.teams, self.snapshot.players, self.sort_mode)

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
        self.page = clamp_page(self.page, self.pages())

    async def refresh_and_edit(self) -> None:
        try:
            await self.refresh()
        except Exception as exc:
            log.exception("Live refresh failed")
            self.last_error = f"{type(exc).__name__}: {exc}"
        await self.edit_message()

    async def edit_message(self, final: bool = False) -> None:
        if self.view:
            self.view.sync_controls()
        await self.message.edit(embed=make_live_embed(self, final=final), view=self.view)


class SortSelect(discord.ui.Select):
    def __init__(self, monitor: LiveMonitor):
        self.monitor = monitor
        options = [
            discord.SelectOption(label=label, value=value, default=(monitor.sort_mode == value))
            for value, label in SORT_CHOICES.items()
        ]
        super().__init__(placeholder="Sort lobby…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.monitor.sort_mode = self.values[0]
        self.monitor.page = 0
        if self.monitor.view:
            self.monitor.view.refresh_select_options()
            self.monitor.view.sync_controls()
        await interaction.response.edit_message(embed=make_live_embed(self.monitor), view=self.monitor.view)


class LivePreviousButton(discord.ui.Button):
    def __init__(self, monitor: LiveMonitor):
        super().__init__(style=discord.ButtonStyle.secondary, label="Previous", emoji="⬅️", row=1)
        self.monitor = monitor

    async def callback(self, interaction: discord.Interaction) -> None:
        self.monitor.page -= 1
        if self.monitor.view:
            self.monitor.view.sync_controls()
        await interaction.response.edit_message(embed=make_live_embed(self.monitor), view=self.monitor.view)


class LiveNextButton(discord.ui.Button):
    def __init__(self, monitor: LiveMonitor):
        super().__init__(style=discord.ButtonStyle.secondary, label="Next", emoji="➡️", row=1)
        self.monitor = monitor

    async def callback(self, interaction: discord.Interaction) -> None:
        self.monitor.page += 1
        if self.monitor.view:
            self.monitor.view.sync_controls()
        await interaction.response.edit_message(embed=make_live_embed(self.monitor), view=self.monitor.view)


class RefreshButton(discord.ui.Button):
    def __init__(self, monitor: LiveMonitor):
        super().__init__(style=discord.ButtonStyle.primary, label="Refresh", emoji="🔄", row=1)
        self.monitor = monitor

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=False)
        await self.monitor.refresh_and_edit()


class StopButton(discord.ui.Button):
    def __init__(self, monitor: LiveMonitor):
        super().__init__(style=discord.ButtonStyle.danger, label="Stop Live", emoji="🛑", row=1)
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
        self.previous = LivePreviousButton(monitor)
        self.next = LiveNextButton(monitor)
        self.refresh = RefreshButton(monitor)
        self.stop = StopButton(monitor)
        self.add_item(SortSelect(monitor))
        self.add_item(self.previous)
        self.add_item(self.next)
        self.add_item(self.refresh)
        self.add_item(self.stop)
        self.sync_controls()

    def refresh_select_options(self) -> None:
        for item in list(self.children):
            if isinstance(item, SortSelect):
                self.remove_item(item)
        self.add_item(SortSelect(self.monitor))

    def sync_controls(self) -> None:
        pages = self.monitor.pages()
        self.monitor.page = clamp_page(self.monitor.page, pages)
        self.previous.disabled = self.monitor.page <= 0 or self.monitor.stopped
        self.next.disabled = self.monitor.page >= len(pages) - 1 or self.monitor.stopped

    async def on_timeout(self) -> None:
        self.monitor.stop()
        self.disable_all_items()
        try:
            await self.monitor.edit_message(final=True)
        except Exception:
            pass


def make_live_embed(monitor: LiveMonitor, final: bool = False) -> discord.Embed:
    snap = monitor.snapshot
    info = format_info(monitor.lobby_format)
    url = f"https://fortnitetracker.com/events/sessions/{monitor.session_id}"
    title_suffix = "Ended" if final or monitor.stopped else "Live"
    embed = discord.Embed(
        title=f"Lobby Scout Live • {title_suffix}",
        description=(
            f"[`{monitor.session_id}`]({url}) • `{info['label']}` • "
            f"Region `{monitor.region}` • Platform `{monitor.platform}`"
        ),
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
        expected = int(info["expected_teams"])
        pending = max(0, expected - known)
        sort_label = SORT_CHOICES.get(monitor.sort_mode, monitor.sort_mode)
        unit = "players" if monitor.lobby_format == "solos" else "teams"
        pages = monitor.pages()
        monitor.page = clamp_page(monitor.page, pages)

        embed.add_field(
            name="Live Summary",
            value=(
                f"**Format:** `{info['label']}` • **Tracked:** `{known}/{expected}` {unit} • "
                f"**Pending:** `{pending}`\n"
                f"**Alive/incomplete:** `{alive}` • **Eliminated:** `{eliminated}` • "
                f"**Sort:** `{sort_label}`\n"
                f"**Auto refresh:** `{monitor.poll_seconds}s` • "
                f"**Timer:** `{elapsed//60}:{elapsed%60:02}` / `{max_seconds//60}:{max_seconds%60:02}`"
            ),
            inline=False,
        )

        if monitor.new_deaths:
            death_lines = [team_line(i, t, pr_by_name) for i, t in enumerate(monitor.new_deaths[:4], start=1)]
            embed.add_field(
                name="🚨 Newly Eliminated",
                value=make_text_pages(death_lines, max_chars=950)[0][:1024],
                inline=False,
            )

        embed.add_field(
            name=f"Full Lobby • Page {monitor.page + 1}/{len(pages)}",
            value=pages[monitor.page][:1024],
            inline=False,
        )

        top_players = sorted(players, key=lambda p: p.pr, reverse=True)[:3]
        if top_players:
            embed.add_field(
                name="Highest PR Players",
                value="\n".join(player_line(i, p) for i, p in enumerate(top_players, start=1))[:1024],
                inline=False,
            )

    if monitor.last_error:
        embed.add_field(name="Last refresh warning", value=f"`{monitor.last_error[:900]}`", inline=False)

    embed.set_footer(
        text=(
            "Every returned team is monitored from the first refresh. Pages only control what is visible. "
            "No memory reading or packet sniffing."
        )
    )
    return embed


async def handle_session_lookup(
    interaction: discord.Interaction,
    session_id_or_url: str,
    region: str,
    platform: str,
    lobby_format: str,
    detected_from_ocr: bool = False,
) -> None:
    region = (region or settings.default_region).upper()
    platform = (platform or settings.default_platform).lower()

    await interaction.followup.send(
        f"Found session ID `{normalize_session_id(session_id_or_url) or session_id_or_url}`. Fetching the full lobby…",
        ephemeral=True,
    )

    data = await scrape_session(session_id_or_url)
    players = await fill_missing_pr(data["players"], region, platform)
    teams = data["teams"]

    pr_by_name = {p.name.lower(): p.pr for p in players}
    for team in teams:
        team.combined_pr = sum(pr_by_name.get(name.lower(), 0.0) for name in team.players)

    state = StaticLobbyState(
        session_id=data["session_id"],
        url=data["url"],
        players=players,
        teams=teams,
        region=region,
        platform=platform,
        lobby_format=lobby_format,
    )
    view = StaticLobbyView(state, detected_from_ocr)
    await interaction.followup.send(embed=make_static_embed(state, detected_from_ocr), view=view)


async def extract_id_or_reply(
    interaction: discord.Interaction,
    screenshot: discord.Attachment,
) -> Optional[str]:
    if not screenshot.content_type or not screenshot.content_type.startswith("image/"):
        await interaction.followup.send("Attach a normal screenshot image such as PNG, JPG, or WebP.", ephemeral=True)
        return None
    image_bytes = await screenshot.read()
    session_id, debug_text = extract_session_id_from_image(image_bytes)
    if not session_id:
        short_debug = (debug_text or "").replace("`", "")[:700]
        await interaction.followup.send(
            "I couldn’t read a valid 32-character session ID. Crop closer to the top-left ID, "
            "or use the matching `_id` command with a pasted Tracker link/ID.\n\n"
            f"OCR saw: ```{short_debug or 'nothing readable'}```",
            ephemeral=True,
        )
        return None
    return session_id


@tree.command(name="players", description="One-time full lobby lookup from a screenshot of the Fortnite session ID.")
@app_commands.describe(
    screenshot="Screenshot containing the Fortnite match/session ID",
    mode="Choose Solos, Duos, Trios, or Squads",
    region="Fortnite event region",
    platform="pc, console, or mobile",
)
@app_commands.choices(
    mode=FORMAT_CHOICES,
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players(
    interaction: discord.Interaction,
    screenshot: discord.Attachment,
    mode: app_commands.Choice[str],
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
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
            mode.value,
            detected_from_ocr=True,
        )
    except LobbyScoutError as exc:
        await interaction.followup.send(f"Lookup failed: {exc}", ephemeral=True)
    except Exception as exc:
        log.exception("/players failed")
        await interaction.followup.send(f"Unexpected error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="players_id", description="One-time full lobby lookup from a Tracker session URL or ID.")
@app_commands.describe(
    match_id_or_url="Fortnite Tracker session URL or 32-character ID",
    mode="Choose Solos, Duos, Trios, or Squads",
    region="Fortnite event region",
    platform="pc, console, or mobile",
)
@app_commands.choices(
    mode=FORMAT_CHOICES,
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players_id(
    interaction: discord.Interaction,
    match_id_or_url: str,
    mode: app_commands.Choice[str],
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
):
    await interaction.response.defer(thinking=True)
    try:
        await handle_session_lookup(
            interaction,
            match_id_or_url,
            region.value if region else settings.default_region,
            platform.value if platform else settings.default_platform,
            mode.value,
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
    lobby_format: str,
    from_ocr: bool,
) -> None:
    region = (region or settings.default_region).upper()
    platform = (platform or settings.default_platform).lower()
    session_id = normalize_session_id(session_id_or_url)
    if not session_id:
        await interaction.followup.send("That does not look like a valid Fortnite Tracker session/match ID.", ephemeral=True)
        return

    info = format_info(lobby_format)
    message = await interaction.followup.send(
        embed=discord.Embed(
            title="Lobby Scout Live • Starting",
            description=(
                f"[`{session_id}`](https://fortnitetracker.com/events/sessions/{session_id}) • "
                f"`{info['label']}` • auto-refresh every `{LIVE_POLL_SECONDS}s` for `{LIVE_MAX_MINUTES}` minutes"
            ),
            color=0x2ECC71,
        ),
        wait=True,
    )
    monitor = LiveMonitor(
        message=message,
        session_id_or_url=session_id_or_url,
        region=region,
        platform=platform,
        lobby_format=lobby_format,
    )
    await monitor.start()
    if from_ocr:
        await interaction.followup.send("OCR found the session ID and the full-lobby monitor is running.", ephemeral=True)


@tree.command(name="players_live", description="Live full-lobby monitor from a screenshot; updates as teams are eliminated.")
@app_commands.describe(
    screenshot="Screenshot containing the Fortnite match/session ID",
    mode="Choose Solos, Duos, Trios, or Squads",
    region="Fortnite event region",
    platform="pc, console, or mobile",
)
@app_commands.choices(
    mode=FORMAT_CHOICES,
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players_live(
    interaction: discord.Interaction,
    screenshot: discord.Attachment,
    mode: app_commands.Choice[str],
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
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
            mode.value,
            from_ocr=True,
        )
    except Exception as exc:
        log.exception("/players_live failed")
        await interaction.followup.send(f"Unexpected error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="players_live_id", description="Live full-lobby monitor from a Tracker session URL or ID.")
@app_commands.describe(
    match_id_or_url="Fortnite Tracker session URL or 32-character ID",
    mode="Choose Solos, Duos, Trios, or Squads",
    region="Fortnite event region",
    platform="pc, console, or mobile",
)
@app_commands.choices(
    mode=FORMAT_CHOICES,
    region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS],
    platform=[app_commands.Choice(name=p, value=p) for p in VALID_PLATFORMS],
)
async def players_live_id(
    interaction: discord.Interaction,
    match_id_or_url: str,
    mode: app_commands.Choice[str],
    region: Optional[app_commands.Choice[str]] = None,
    platform: Optional[app_commands.Choice[str]] = None,
):
    await interaction.response.defer(thinking=True)
    try:
        await start_live_monitor(
            interaction,
            match_id_or_url,
            region.value if region else settings.default_region,
            platform.value if platform else settings.default_platform,
            mode.value,
            from_ocr=False,
        )
    except Exception as exc:
        log.exception("/players_live_id failed")
        await interaction.followup.send(f"Unexpected error: `{type(exc).__name__}: {exc}`", ephemeral=True)


@tree.command(name="bot_status", description="Check whether Lobby Scout Pro is online.")
async def bot_status(interaction: discord.Interaction):
    await interaction.response.send_message(
        (
            f"Lobby Scout Pro is online ✅ Active live monitors: `{len(active_monitors)}`\n"
            f"Live defaults: refresh every `{LIVE_POLL_SECONDS}s` for `{LIVE_MAX_MINUTES}` minutes."
        ),
        ephemeral=True,
    )


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
    return web.json_response(
        {
            "ok": True,
            "bot": str(client.user) if client.user else None,
            "active_live_monitors": len(active_monitors),
            "live_poll_seconds": LIVE_POLL_SECONDS,
            "live_max_minutes": LIVE_MAX_MINUTES,
        }
    )


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
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to Railway Variables.")
    await start_health_server()
    await client.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
