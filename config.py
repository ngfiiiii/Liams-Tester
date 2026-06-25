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
    port: int = _int_env("PORT", "8080")


settings = Settings()
