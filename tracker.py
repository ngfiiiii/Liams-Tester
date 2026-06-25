from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from typing import Any, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from models import PlayerResult, TeamResult
from ocr import normalize_session_id
from config import settings

SESSION_URL = "https://fortnitetracker.com/events/sessions/{session_id}"
PR_URL = "https://api.fortnitetracker.com/v1/powerrankings/{platform}/{region}/{epic}"
CACHE_DB = os.getenv("CACHE_DB", "lobby_scout_cache.sqlite3")
CACHE_TTL_SECONDS = 60 * 60 * 24

NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
PLACED_RE = re.compile(
    r"(?P<time>\d{1,2}\s*m\s*\d{1,2}\s*s|\d{1,2}:\d{2})\s*\|?\s*(?:team|player)\s+placed\s+#(?P<place>\d{1,3})",
    re.I,
)

class LobbyScoutError(RuntimeError):
    pass

class PRCache:
    def __init__(self, db_path: str = CACHE_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pr_cache (
                    player TEXT NOT NULL,
                    region TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    pr REAL NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    PRIMARY KEY (player, region, platform)
                )
                """
            )

    def get(self, player: str, region: str, platform: str) -> Optional[float]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT pr, fetched_at FROM pr_cache WHERE player=? AND region=? AND platform=?",
                (player.lower(), region.upper(), platform.lower()),
            ).fetchone()
        if not row:
            return None
        pr, fetched_at = row
        if int(time.time()) - fetched_at > CACHE_TTL_SECONDS:
            return None
        return float(pr)

    def set(self, player: str, region: str, platform: str, pr: float) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pr_cache(player, region, platform, pr, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (player.lower(), region.upper(), platform.lower(), float(pr), int(time.time())),
            )

pr_cache = PRCache()

def _to_int(text: str | None) -> Optional[int]:
    if text is None:
        return None
    m = NUM_RE.search(str(text).replace(",", ""))
    return int(float(m.group(0))) if m else None

def _to_float(text: str | None) -> float:
    if text is None:
        return 0.0
    m = NUM_RE.search(str(text).replace(",", ""))
    return float(m.group(0)) if m else 0.0

def _normalize_time(text: str | None) -> Optional[str]:
    if not text:
        return None
    return re.sub(r"\s+", "", text).replace(":", "m") + ("s" if ":" in text else "")

def clean_player_name(name: str) -> str:
    name = re.sub(r"\[FNCS\d{4}\]\s*", "", name or "", flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" ,•\n\t")
    return name.strip()

def _split_players(cell: str) -> list[str]:
    # Table cells often put duo names on separate lines or separated by commas.
    raw = cell.replace("\r", "\n")
    parts = re.split(r"\n|\s{2,}|\s*,\s*|\s+ / \s+", raw)
    cleaned = [clean_player_name(p) for p in parts]
    return [p for p in cleaned if p and p.lower() not in {"unknown", "player", "players", "team"}]

def _parse_tables_from_html(html: str) -> tuple[list[PlayerResult], list[TeamResult]]:
    soup = BeautifulSoup(html, "html.parser")
    players: dict[str, PlayerResult] = {}
    teams: list[TeamResult] = []

    for table in soup.select("table"):
        headers = [h.get_text(" ", strip=True).lower() for h in table.select("thead th")]
        if not headers:
            first_row = table.select_one("tr")
            if first_row:
                headers = [c.get_text(" ", strip=True).lower() for c in first_row.select("th,td")]
        header_blob = " ".join(headers)
        body_rows = table.select("tbody tr") or table.select("tr")[1:]

        if "power rating" in header_blob or ("kills" in header_blob and "damage" in header_blob):
            for tr in body_rows:
                cells = [c.get_text("\n", strip=True) for c in tr.select("td")]
                if len(cells) < 3:
                    continue
                # Expected: Place | Player | Kills | Damage | Power Rating
                placement = _to_int(cells[0])
                name = clean_player_name(cells[1])
                if not name:
                    continue
                pr = _to_float(cells[-1])
                kills = _to_int(cells[2]) if len(cells) > 2 else None
                damage = _to_int(cells[3]) if len(cells) > 3 else None
                players[name.lower()] = PlayerResult(
                    name=name,
                    placement=placement,
                    kills=kills,
                    damage=damage,
                    pr=pr,
                    source="session",
                )

        elif "points" in header_blob and ("earned" in header_blob or "eliminations" in header_blob):
            for tr in body_rows:
                cells = [c.get_text("\n", strip=True) for c in tr.select("td")]
                if len(cells) < 3:
                    continue
                placement = _to_int(cells[0])
                names = _split_players(cells[1])
                if not names:
                    continue
                team = TeamResult(
                    placement=placement,
                    players=names,
                    points=_to_int(cells[2]) if len(cells) > 2 else None,
                    eliminations=_to_int(cells[3]) if len(cells) > 3 else None,
                    time_played=cells[4] if len(cells) > 4 else None,
                    damage_text=cells[5] if len(cells) > 5 else None,
                )
                teams.append(team)

    return list(players.values()), teams

def _parse_rendered_text(text: str) -> tuple[list[PlayerResult], list[TeamResult]]:
    """Fallback parser for pages where data renders without real <table> tags."""
    players: dict[str, PlayerResult] = {}
    teams: list[TeamResult] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Parse individual stats block between 'Place Player Kills Damage Power Rating' and Kill Feed area.
    try:
        start = next(i for i, ln in enumerate(lines) if "Place Player Kills Damage Power Rating".lower() in ln.lower())
        end = next((i for i in range(start + 1, len(lines)) if "Time Instigator Action Victim".lower() in lines[i].lower()), len(lines))
        block = lines[start + 1:end]
        for ln in block:
            # Examples usually look like: 12 PlayerName 3 1,450 8,920
            m = re.match(r"^(?P<place>\d{1,3})\s+(?P<name>.+?)\s+(?P<kills>\d+)\s+(?P<damage>[\d,]+).*?\s+(?P<pr>[\d,]+)\s*$", ln)
            if not m:
                continue
            name = clean_player_name(m.group("name"))
            if not name:
                continue
            players[name.lower()] = PlayerResult(
                name=name,
                placement=int(m.group("place")),
                kills=int(m.group("kills")),
                damage=int(m.group("damage").replace(",", "")),
                pr=float(m.group("pr").replace(",", "")),
                source="session-text",
            )
    except StopIteration:
        pass

    # Parse roster/team block.
    try:
        start = next(i for i, ln in enumerate(lines) if "Place Player Points Earned Eliminations".lower() in ln.lower())
        end = next((i for i in range(start + 1, len(lines)) if "SWEAT FACTOR".lower() in lines[i].lower() or "Most Kills".lower() in lines[i].lower()), len(lines))
        block = lines[start + 1:end]
        for ln in block:
            m = re.match(r"^(?P<place>\d{1,3})\s+(?P<names>.+?)\s+(?P<points>\d+)\s+(?P<elims>\d+)\s+", ln)
            if not m:
                continue
            names = _split_players(m.group("names"))
            if names:
                teams.append(TeamResult(
                    placement=int(m.group("place")),
                    players=names,
                    points=int(m.group("points")),
                    eliminations=int(m.group("elims")),
                ))
    except StopIteration:
        pass

    return list(players.values()), teams

def _parse_elimination_events(text: str) -> dict[int, dict[str, Any]]:
    """Return placement -> event info from the Match Timeline's 'Team placed #N' text."""
    events: dict[int, dict[str, Any]] = {}
    for idx, match in enumerate(PLACED_RE.finditer(text or ""), start=1):
        place = int(match.group("place"))
        events[place] = {
            "placement": place,
            "time": _normalize_time(match.group("time")) or match.group("time"),
            "order": idx,
        }
    return events

def _infer_live_state(teams: list[TeamResult], text: str) -> list[TeamResult]:
    """Attach eliminated/alive flags using timeline events first and placement data second."""
    events = _parse_elimination_events(text)

    for team in teams:
        if team.placement in events:
            event = events[team.placement]
            team.is_eliminated = True
            team.eliminated_at = event.get("time")
            team.eliminated_order = event.get("order")
        elif team.placement is not None and team.placement > 1:
            # Fallback: during live sessions, placement usually appears once a team is dead.
            # At the end of the match, this also marks all non-winners as eliminated.
            team.is_eliminated = True
            team.eliminated_at = team.time_played
            # Bigger placement numbers die earlier in battle royale placement order.
            team.eliminated_order = max(1, 101 - team.placement)
        else:
            team.is_eliminated = False
            team.eliminated_at = None
            team.eliminated_order = None

    return teams

async def _render_session_page(session_id: str) -> tuple[str, str, str]:
    url = SESSION_URL.format(session_id=session_id)
    chromium_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=chromium_path,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 1400},
        )
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # The page receives match data dynamically. Wait for actual stats/roster text.
            for _ in range(24):
                body_text = await page.locator("body").inner_text(timeout=5000)
                if "{{" not in body_text and (
                    "Power Rating" in body_text or "Copy Match ID" in body_text or "Kill Feed" in body_text
                ):
                    break
                await page.wait_for_timeout(1000)
            # Give live session websocket/API a few more seconds.
            await page.wait_for_timeout(2500)
            html = await page.content()
            text = await page.locator("body").inner_text(timeout=10000)
            title = await page.title()
            return html, text, title
        except PlaywrightTimeoutError as exc:
            raise LobbyScoutError(f"Fortnite Tracker took too long to load: {exc}") from exc
        finally:
            await browser.close()

async def scrape_session(session_id_or_url: str) -> dict[str, Any]:
    session_id = normalize_session_id(session_id_or_url)
    if not session_id:
        raise LobbyScoutError("That does not look like a valid Fortnite Tracker session/match ID.")

    html, text, title = await _render_session_page(session_id)
    players, teams = _parse_tables_from_html(html)
    fallback_players, fallback_teams = _parse_rendered_text(text)

    if not players and fallback_players:
        players = fallback_players
    if not teams and fallback_teams:
        teams = fallback_teams

    teams = _infer_live_state(teams, text)

    # Attach known player PR totals to teams.
    pr_by_name = {p.name.lower(): p.pr for p in players}
    for team in teams:
        team.combined_pr = sum(pr_by_name.get(name.lower(), 0.0) for name in team.players)
        for name in team.players:
            key = name.lower()
            if key in pr_by_name:
                for p in players:
                    if p.name.lower() == key:
                        p.team = " / ".join(team.players)

    return {
        "session_id": session_id,
        "url": SESSION_URL.format(session_id=session_id),
        "title": title,
        "players": sorted(players, key=lambda p: p.pr, reverse=True),
        "teams": sorted(teams, key=lambda t: t.combined_pr, reverse=True),
        "raw_text": text,
        "elimination_events": _parse_elimination_events(text),
    }

def fetch_pr_from_trn(player_name: str, region: str, platform: str) -> float:
    """Optional fallback when session page does not expose PR for a player."""
    if not settings.trn_api_key:
        return 0.0

    cached = pr_cache.get(player_name, region, platform)
    if cached is not None:
        return cached

    headers = {"TRN-Api-Key": settings.trn_api_key}
    url = PR_URL.format(
        platform=platform.lower(),
        region=region.upper(),
        epic=quote(player_name, safe=""),
    )
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return 0.0
        data = r.json()
    except Exception:
        return 0.0

    # Tracker has changed response shapes over time. Search recursively for likely PR fields.
    def walk(obj: Any) -> list[float]:
        vals: list[float] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in {"points", "pr", "power", "powerpoints", "power_rank", "powerranking"}:
                    if isinstance(v, (int, float, str)):
                        vals.append(_to_float(str(v)))
                vals.extend(walk(v))
        elif isinstance(obj, list):
            for item in obj:
                vals.extend(walk(item))
        return vals

    values = [v for v in walk(data) if v > 0]
    pr = max(values) if values else 0.0
    if pr:
        pr_cache.set(player_name, region, platform, pr)
    return pr

async def fill_missing_pr(players: list[PlayerResult], region: str, platform: str) -> list[PlayerResult]:
    if not settings.trn_api_key:
        return players

    async def one(p: PlayerResult) -> PlayerResult:
        if p.pr > 0:
            return p
        pr = await asyncio.to_thread(fetch_pr_from_trn, p.name, region, platform)
        if pr:
            p.pr = pr
            p.source = "trn-api"
        return p

    # Keep it gentle to avoid API rate limits.
    sem = asyncio.Semaphore(3)
    async def guarded(p: PlayerResult) -> PlayerResult:
        async with sem:
            return await one(p)

    return sorted(await asyncio.gather(*(guarded(p) for p in players)), key=lambda p: p.pr, reverse=True)
