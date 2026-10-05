from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str
    ADMIN_IDS: str = ""
    DATABASE_URL: str = "sqlite+aiosqlite:///data/bot.db"

    API_ID: int = 0
    API_HASH: str = ""
    SESSIONS_DIR: str = "sessions"

    TGRASS_API_KEY: str = ""
    SNIPER_INTERVAL: int = 120
    HTTP_PROXY: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.ADMIN_IDS.replace(" ", "").split(",") if x}


settings = Settings()

# Тарифы Premium: ключ -> (дни, цена в Stars)
PREMIUM_PLANS = {
    "p1": (1, 10),
    "p3": (3, 25),
    "p10": (10, 65),
    "p30": (30, 100),
}
# Пакеты поисков: ключ -> (кол-во поисков, цена в Stars)
SEARCH_PACKS = {
    "s10": (10, 5),
    "s50": (50, 20),
    "s150": (150, 50),
}
# Уровни реферальных наград: (кол-во друзей, дни Premium)
REF_TIERS = [(3, 1), (10, 3), (15, 10), (35, 25)]

# Значения по умолчанию для таблицы settings
DEFAULT_SETTINGS = {
    "tgrass_api_key": "",
    "tgrass_enabled": "0",
    "daily_free_limit": "3",
    "search_cooldown_sec": "20",
    "support_url": "https://t.me/",
    "battle_enabled": "1",
    "captcha_enabled": "0",
    "premium_emoji": "{}",
    "premium_emoji_enabled": "1",
    "button_colors_enabled": "1",
}
