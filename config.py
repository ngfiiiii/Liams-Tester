import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

def _int_env(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)

@dataclass(frozen=True)
class Settings:
    discord_token: str = os.getenv("DISCORD_TOKEN", "").strip()
    discord_guild_id: str = os.getenv("DISCORD_GUILD_ID", "").strip()
    trn_api_key: str = os.getenv("TRN_API_KEY", "").strip()
    default_region: str = os.getenv("DEFAULT_REGION", "NAC").strip().upper()
    default_platform: str = os.getenv("DEFAULT_PLATFORM", "pc").strip().lower()
    top_n: int = _int_env("TOP_N", "25")
    max_rows: int = _int_env("MAX_ROWS", "60")
    port: int = _int_env("PORT", "8080")

    # Live monitor settings. Railway free/small plans should keep this at 8-15 seconds.
    live_poll_seconds: int = max(5, _int_env("LIVE_POLL_SECONDS", "10"))
    live_max_minutes: int = max(1, _int_env("LIVE_MAX_MINUTES", "30"))
    live_top_teams: int = max(5, _int_env("LIVE_TOP_TEAMS", "25"))

settings = Settings()
