from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from typing import Any, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from models import PlayerResult, TeamResult
from ocr import normalize_session_id
from config import settings

log = logging.getLogger("lobby-scout.tracker")

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
                (player.casefold(), region.upper(), platform.lower()),
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
                (player.casefold(), region.upper(), platform.lower(), float(pr), int(time.time())),
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


def _number_from_value(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _to_float(value)
    if isinstance(value, dict):
        for key in ("value", "displayValue", "display", "points", "pr"):
            if key in value:
                n = _number_from_value(value[key])
                if n:
                    return n
    return 0.0


def _normalize_time(text: str | None) -> Optional[str]:
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    if ":" in compact:
        mins, secs = compact.split(":", 1)
        return f"{mins}m{secs}s"
    return compact


def clean_player_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", str(name or ""))
    name = re.sub(r"\[FNCS\d{4}\]\s*", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" ,•\n\t")
    return name.strip()


def player_key(name: str) -> str:
    return clean_player_name(name).casefold()


def _split_players(cell: str) -> list[str]:
    raw = str(cell or "").replace("\r", "\n")
    parts = re.split(r"\n|\s{2,}|\s*,\s*|\s+ / \s+", raw)
    cleaned = [clean_player_name(p) for p in parts]
    return [p for p in cleaned if p and p.casefold() not in {"unknown", "player", "players", "team"}]


def _looks_like_player_name(name: str) -> bool:
    name = clean_player_name(name)
    if not name or len(name) > 80:
        return False
    bad = {
        "unknown", "player", "players", "team", "teams", "stats", "roster",
        "most kills", "most damage", "highest pr", "power rating",
    }
    if name.casefold() in bad:
        return False
    return bool(re.search(r"[A-Za-z0-9]", name))


def _first_present(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {str(k).casefold(): v for k, v in obj.items()}
    for key in keys:
        if key.casefold() in lowered:
            return lowered[key.casefold()]
    return None


def _name_from_object(obj: dict[str, Any]) -> str:
    direct = _first_present(
        obj,
        (
            "displayName", "display_name", "epicName", "epic_name", "playerName",
            "player_name", "accountName", "account_name", "name",
        ),
    )
    if isinstance(direct, str) and _looks_like_player_name(direct):
        return clean_player_name(direct)

    player = _first_present(obj, ("player", "account", "profile", "user"))
    if isinstance(player, str) and _looks_like_player_name(player):
        return clean_player_name(player)
    if isinstance(player, (list, tuple)):
        # Fortnite Tracker commonly represents a player as [accountId, displayName].
        for candidate in reversed(player):
            if isinstance(candidate, str) and _looks_like_player_name(candidate):
                # Ignore UUID/account-id looking values if another display value exists.
                if re.fullmatch(r"[0-9a-f-]{24,40}", candidate, re.I):
                    continue
                return clean_player_name(candidate)
    if isinstance(player, dict):
        nested = _name_from_object(player)
        if nested:
            return nested
    return ""


def _pr_from_object(obj: dict[str, Any]) -> float:
    # Prefer explicit PR fields. Avoid generic "points" because tournament points are not PR.
    value = _first_present(
        obj,
        (
            "pr", "powerRating", "power_rating", "powerRank", "power_rank",
            "powerRanking", "power_ranking", "powerPoints", "power_points",
            "prPoints", "pr_points", "currentPr", "current_pr",
        ),
    )
    return max(0.0, _number_from_value(value))


def _merge_player(target: dict[str, PlayerResult], incoming: PlayerResult) -> None:
    key = player_key(incoming.name)
    if not key:
        return
    current = target.get(key)
    if current is None:
        target[key] = incoming
        return

    # Keep the best non-zero PR and fill any missing stats from either source.
    if incoming.pr > current.pr:
        current.pr = incoming.pr
        current.source = incoming.source
        current.name = incoming.name or current.name
    if current.placement is None and incoming.placement is not None:
        current.placement = incoming.placement
    if current.kills is None and incoming.kills is not None:
        current.kills = incoming.kills
    if current.damage is None and incoming.damage is not None:
        current.damage = incoming.damage
    if not current.team and incoming.team:
        current.team = incoming.team


def _merge_player_lists(*lists: list[PlayerResult]) -> list[PlayerResult]:
    merged: dict[str, PlayerResult] = {}
    for entries in lists:
        for player in entries:
            _merge_player(merged, player)
    return list(merged.values())


def _parse_tables_from_html(html: str) -> tuple[list[PlayerResult], list[TeamResult]]:
    soup = BeautifulSoup(html, "html.parser")
    players: dict[str, PlayerResult] = {}
    teams: list[TeamResult] = []

    for table in soup.select("table"):
        headers = [h.get_text(" ", strip=True).casefold() for h in table.select("thead th")]
        if not headers:
            first_row = table.select_one("tr")
            if first_row:
                headers = [c.get_text(" ", strip=True).casefold() for c in first_row.select("th,td")]
        header_blob = " ".join(headers)
        body_rows = table.select("tbody tr") or table.select("tr")[1:]

        if "power rating" in header_blob or ("kills" in header_blob and "damage" in header_blob):
            # Find column indexes from headers instead of assuming PR is always the last cell.
            def col_index(fragment: str, default: int) -> int:
                return next((i for i, h in enumerate(headers) if fragment in h), default)

            place_idx = col_index("place", 0)
            player_idx = col_index("player", 1)
            kills_idx = col_index("kill", 2)
            damage_idx = col_index("damage", 3)
            pr_idx = col_index("power rating", max(0, len(headers) - 1))

            for tr in body_rows:
                cells = [c.get_text("\n", strip=True) for c in tr.select("td")]
                if len(cells) < 2:
                    continue
                try:
                    name = clean_player_name(cells[player_idx])
                except IndexError:
                    continue
                if not _looks_like_player_name(name):
                    continue

                pr_text = cells[pr_idx] if pr_idx < len(cells) else cells[-1]
                # Some layouts put an em dash before/after the PR; take the largest numeric token.
                pr_values = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", pr_text)]
                pr = max(pr_values) if pr_values else 0.0
                result = PlayerResult(
                    name=name,
                    placement=_to_int(cells[place_idx]) if place_idx < len(cells) else None,
                    kills=_to_int(cells[kills_idx]) if kills_idx < len(cells) else None,
                    damage=_to_int(cells[damage_idx]) if damage_idx < len(cells) else None,
                    pr=pr,
                    source="session-stats-tab",
                )
                _merge_player(players, result)

        elif "points" in header_blob and ("earned" in header_blob or "eliminations" in header_blob):
            for tr in body_rows:
                cells = [c.get_text("\n", strip=True) for c in tr.select("td")]
                if len(cells) < 3:
                    continue
                placement = _to_int(cells[0])
                names = _split_players(cells[1])
                if not names:
                    continue
                teams.append(
                    TeamResult(
                        placement=placement,
                        players=names,
                        points=_to_int(cells[2]) if len(cells) > 2 else None,
                        eliminations=_to_int(cells[3]) if len(cells) > 3 else None,
                        time_played=cells[4] if len(cells) > 4 else None,
                        damage_text=cells[5] if len(cells) > 5 else None,
                    )
                )

    return list(players.values()), teams


def _parse_rendered_text(text: str) -> tuple[list[PlayerResult], list[TeamResult]]:
    """Fallback parser for pages where data renders without real <table> tags."""
    players: dict[str, PlayerResult] = {}
    teams: list[TeamResult] = []
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]

    # Horizontal-row layout.
    try:
        start = next(i for i, ln in enumerate(lines) if "place player kills damage power rating" in ln.casefold())
        end = next((i for i in range(start + 1, len(lines)) if "time instigator action victim" in lines[i].casefold()), len(lines))
        block = lines[start + 1 : end]
        for ln in block:
            m = re.match(
                r"^(?P<place>\d{1,3})\s+(?P<name>.+?)\s+(?P<kills>\d+|—)\s+(?P<damage>[\d,]+).*?\s+(?P<pr>[\d,]+)\s*(?:PR)?\s*(?:—)?$",
                ln,
            )
            if not m:
                continue
            name = clean_player_name(m.group("name"))
            if not _looks_like_player_name(name):
                continue
            _merge_player(
                players,
                PlayerResult(
                    name=name,
                    placement=int(m.group("place")),
                    kills=_to_int(m.group("kills")),
                    damage=_to_int(m.group("damage")),
                    pr=_to_float(m.group("pr")),
                    source="session-rendered-text",
                ),
            )
    except StopIteration:
        pass

    # Also recognize kill-feed rows because they contain explicit PR beside names.
    killfeed_re = re.compile(
        r"(?P<instigator>[^\n]+?)\s+(?P<ipr>[\d,]+)\s+PR\s+.*?\s+(?P<victim>[^\n]+?)\s+(?P<vpr>[\d,]+)\s+PR",
        re.I,
    )
    for match in killfeed_re.finditer(text or ""):
        for name_group, pr_group in (("instigator", "ipr"), ("victim", "vpr")):
            name = clean_player_name(match.group(name_group))
            if _looks_like_player_name(name):
                _merge_player(
                    players,
                    PlayerResult(name=name, pr=_to_float(match.group(pr_group)), source="session-kill-feed"),
                )

    # Roster/team block.
    try:
        start = next(i for i, ln in enumerate(lines) if "place player points earned eliminations" in ln.casefold())
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if "sweat factor" in lines[i].casefold() or "most kills" in lines[i].casefold()
            ),
            len(lines),
        )
        block = lines[start + 1 : end]
        for ln in block:
            m = re.match(r"^(?P<place>\d{1,3})\s+(?P<names>.+?)\s+(?P<points>\d+)\s+(?P<elims>\d+)\s+", ln)
            if not m:
                continue
            names = _split_players(m.group("names"))
            if names:
                teams.append(
                    TeamResult(
                        placement=int(m.group("place")),
                        players=names,
                        points=int(m.group("points")),
                        eliminations=int(m.group("elims")),
                    )
                )
    except StopIteration:
        pass

    return list(players.values()), teams


def _parse_players_from_json(payloads: list[Any]) -> list[PlayerResult]:
    """Extract player PR from Tracker's live XHR/fetch JSON without relying on one endpoint shape."""
    players: dict[str, PlayerResult] = {}
    seen_objects: set[int] = set()

    def add(name: str, pr: float, obj: dict[str, Any] | None, source: str) -> None:
        name = clean_player_name(name)
        if not _looks_like_player_name(name) or pr <= 0:
            return
        obj = obj or {}
        placement = _to_int(str(_first_present(obj, ("placement", "place", "rank"))))
        kills = _to_int(str(_first_present(obj, ("kills", "eliminations", "elims"))))
        damage = _to_int(str(_first_present(obj, ("damage", "damageDealt", "damage_dealt"))))
        _merge_player(
            players,
            PlayerResult(
                name=name,
                pr=pr,
                placement=placement,
                kills=kills,
                damage=damage,
                source=source,
            ),
        )

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            oid = id(obj)
            if oid in seen_objects:
                return
            seen_objects.add(oid)

            name = _name_from_object(obj)
            pr = _pr_from_object(obj)
            if name and pr > 0:
                add(name, pr, obj, "session-json")

            # Kill-feed/event shapes often use separate instigator/victim fields.
            for prefix in ("instigator", "victim", "killer", "eliminator"):
                name_value = _first_present(
                    obj,
                    (
                        prefix,
                        f"{prefix}Name",
                        f"{prefix}_name",
                        f"{prefix}DisplayName",
                        f"{prefix}_display_name",
                    ),
                )
                pr_value = _first_present(
                    obj,
                    (
                        f"{prefix}PR",
                        f"{prefix}Pr",
                        f"{prefix}_pr",
                        f"{prefix}PowerRating",
                        f"{prefix}_power_rating",
                    ),
                )
                if isinstance(name_value, dict):
                    event_name = _name_from_object(name_value)
                elif isinstance(name_value, (list, tuple)):
                    event_name = _name_from_object({"player": name_value})
                else:
                    event_name = clean_player_name(str(name_value or ""))
                event_pr = _number_from_value(pr_value)
                if event_name and event_pr > 0:
                    add(event_name, event_pr, obj, "session-json-event")

            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for payload in payloads:
        walk(payload)
    return list(players.values())


def _parse_elimination_events(text: str) -> dict[int, dict[str, Any]]:
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
    events = _parse_elimination_events(text)

    for team in teams:
        if team.placement in events:
            event = events[team.placement]
            team.is_eliminated = True
            team.eliminated_at = event.get("time")
            team.eliminated_order = event.get("order")
        elif team.placement is not None and team.placement > 1:
            team.is_eliminated = True
            team.eliminated_at = team.time_played
            team.eliminated_order = max(1, 101 - team.placement)
        else:
            team.is_eliminated = False
            team.eliminated_at = None
            team.eliminated_order = None

    return teams


async def _click_tab(page: Any, tab_name: str) -> bool:
    """Click a Tracker tab using several selectors because the site markup changes often."""
    candidates = [
        page.get_by_role("tab", name=re.compile(rf"^{re.escape(tab_name)}$", re.I)),
        page.get_by_role("button", name=re.compile(rf"^{re.escape(tab_name)}$", re.I)),
        page.get_by_text(tab_name, exact=True),
        page.locator(f"a:has-text('{tab_name}')"),
        page.locator(f"button:has-text('{tab_name}')"),
    ]
    for locator in candidates:
        try:
            if await locator.count() > 0:
                await locator.first.click(timeout=3500)
                await page.wait_for_timeout(900)
                return True
        except Exception:
            continue
    return False


async def _render_session_page(session_id: str) -> tuple[str, str, str, list[Any]]:
    url = SESSION_URL.format(session_id=session_id)
    chromium_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
    json_payloads: list[Any] = []
    response_tasks: set[asyncio.Task[Any]] = set()

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

        async def capture_json_response(response: Any) -> None:
            try:
                content_type = (await response.all_headers()).get("content-type", "").casefold()
                if "json" not in content_type and response.request.resource_type not in {"xhr", "fetch"}:
                    return
                # Ignore ads/analytics and keep payloads reasonably bounded.
                if not any(host in response.url for host in ("fortnitetracker.com", "tracker.gg", "tracker.network")):
                    return
                body = await response.body()
                if not body or len(body) > 8_000_000:
                    return
                try:
                    payload = json.loads(body.decode("utf-8", errors="ignore"))
                except Exception:
                    return
                if isinstance(payload, (dict, list)):
                    json_payloads.append(payload)
            except Exception:
                return

        def on_response(response: Any) -> None:
            task = asyncio.create_task(capture_json_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Wait for the live session shell/data to initialize.
            for _ in range(30):
                body_text = await page.locator("body").inner_text(timeout=5000)
                if "{{" not in body_text and (
                    "Copy Match ID" in body_text
                    or "Match Timeline" in body_text
                    or "Power Rating" in body_text
                ):
                    break
                await page.wait_for_timeout(1000)

            await page.wait_for_timeout(1800)
            title = await page.title()
            html_parts: list[str] = [await page.content()]
            text_parts: list[str] = [await page.locator("body").inner_text(timeout=10000)]

            # PR is on the Stats tab. Previous builds only captured the default Roster tab,
            # which is why every player displayed 0 PR.
            if await _click_tab(page, "Stats"):
                for _ in range(15):
                    stats_text = await page.locator("body").inner_text(timeout=5000)
                    if "Power Rating" in stats_text and "{{ player.pr" not in stats_text:
                        break
                    await page.wait_for_timeout(500)
                await page.wait_for_timeout(1000)
                html_parts.append(await page.content())
                text_parts.append(await page.locator("body").inner_text(timeout=10000))

            # Return to roster so team rows are also guaranteed to be captured.
            if await _click_tab(page, "Roster"):
                await page.wait_for_timeout(700)
                html_parts.append(await page.content())
                text_parts.append(await page.locator("body").inner_text(timeout=10000))

            # The kill feed is another PR source when the stats table is partially processed.
            if await _click_tab(page, "Kill Feed"):
                await page.wait_for_timeout(700)
                html_parts.append(await page.content())
                text_parts.append(await page.locator("body").inner_text(timeout=10000))

            if response_tasks:
                await asyncio.wait(response_tasks, timeout=5)

            return "\n".join(html_parts), "\n".join(text_parts), title, json_payloads
        except PlaywrightTimeoutError as exc:
            raise LobbyScoutError(f"Fortnite Tracker took too long to load: {exc}") from exc
        finally:
            await browser.close()


async def scrape_session(session_id_or_url: str) -> dict[str, Any]:
    session_id = normalize_session_id(session_id_or_url)
    if not session_id:
        raise LobbyScoutError("That does not look like a valid Fortnite Tracker session/match ID.")

    html, text, title, json_payloads = await _render_session_page(session_id)
    table_players, teams = _parse_tables_from_html(html)
    text_players, fallback_teams = _parse_rendered_text(text)
    json_players = _parse_players_from_json(json_payloads)

    players = _merge_player_lists(table_players, text_players, json_players)
    if not teams and fallback_teams:
        teams = fallback_teams

    teams = _infer_live_state(teams, text)

    pr_by_name = {player_key(p.name): p.pr for p in players if p.pr > 0}
    for team in teams:
        team.combined_pr = sum(pr_by_name.get(player_key(name), 0.0) for name in team.players)
        team_label = " / ".join(team.players)
        for player in players:
            if player_key(player.name) in {player_key(name) for name in team.players}:
                player.team = team_label

    positive_pr = sum(1 for p in players if p.pr > 0)
    log.info(
        "Session %s parsed: %d teams, %d players, %d with PR (table=%d text=%d json=%d payloads=%d)",
        session_id,
        len(teams),
        len(players),
        positive_pr,
        len(table_players),
        len(text_players),
        len(json_players),
        len(json_payloads),
    )

    return {
        "session_id": session_id,
        "url": SESSION_URL.format(session_id=session_id),
        "title": title,
        "players": sorted(players, key=lambda p: p.pr, reverse=True),
        "teams": sorted(teams, key=lambda t: t.combined_pr, reverse=True),
        "raw_text": text,
        "elimination_events": _parse_elimination_events(text),
        "pr_debug": {
            "players": len(players),
            "players_with_pr": positive_pr,
            "table_players": len(table_players),
            "text_players": len(text_players),
            "json_players": len(json_players),
            "json_payloads": len(json_payloads),
        },
    }


def fetch_pr_from_trn(player_name: str, region: str, platform: str) -> float:
    """Optional fallback when the session page does not expose PR for a player."""
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
        response = requests.get(url, headers=headers, timeout=12)
        if response.status_code != 200:
            log.warning("TRN PR lookup returned HTTP %s for %s", response.status_code, player_name)
            return 0.0
        data = response.json()
    except Exception as exc:
        log.warning("TRN PR lookup failed for %s: %s", player_name, exc)
        return 0.0

    def walk(obj: Any) -> list[float]:
        values: list[float] = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_lower = str(key).casefold()
                if key_lower in {
                    "pr", "power", "powerrating", "power_rating", "powerpoints",
                    "power_rank", "powerranking", "power_ranking",
                }:
                    number = _number_from_value(value)
                    if number:
                        values.append(number)
                values.extend(walk(value))
        elif isinstance(obj, list):
            for item in obj:
                values.extend(walk(item))
        return values

    values = [value for value in walk(data) if value > 0]
    pr = max(values) if values else 0.0
    if pr:
        pr_cache.set(player_name, region, platform, pr)
    return pr


async def fill_missing_pr(players: list[PlayerResult], region: str, platform: str) -> list[PlayerResult]:
    if not settings.trn_api_key:
        return players

    async def one(player: PlayerResult) -> PlayerResult:
        if player.pr > 0:
            return player
        pr = await asyncio.to_thread(fetch_pr_from_trn, player.name, region, platform)
        if pr:
            player.pr = pr
            player.source = "trn-api"
        return player

    sem = asyncio.Semaphore(3)

    async def guarded(player: PlayerResult) -> PlayerResult:
        async with sem:
            return await one(player)

    return sorted(await asyncio.gather(*(guarded(p) for p in players)), key=lambda p: p.pr, reverse=True)
