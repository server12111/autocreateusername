from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    lang: Mapped[str] = mapped_column(String(8), default="ru")
    is_tg_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_captcha_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    free_searches_left: Mapped[int] = mapped_column(Integer, default=1)
    sponsor_bonus_claimed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    paid_searches_left: Mapped[int] = mapped_column(Integer, default=0)
    total_searches_done: Mapped[int] = mapped_column(Integer, default=0)
    last_search_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_bonus_claim: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    referrals_count: Mapped[int] = mapped_column(Integer, default=0)
    is_ref_counted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked_bot: Mapped[bool] = mapped_column(Boolean, default=False)


class SearchHistory(Base):
    __tablename__ = "searches_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    username_query: Mapped[str] = mapped_column(String(64), index=True)
    is_available_tg: Mapped[bool] = mapped_column(Boolean, default=False)
    is_available_fragment: Mapped[bool] = mapped_column(Boolean, default=False)
    status_detail: Mapped[str] = mapped_column(String(128), default="")
    is_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NicknameTrap(Base):
    __tablename__ = "nickname_traps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    target_username: Mapped[str] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SponsorChannel(Base):
    __tablename__ = "sponsor_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    invite_link: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    reward_type: Mapped[str] = mapped_column(String(16))  # premium_days | searches
    reward_value: Mapped[int] = mapped_column(Integer)
    max_activations: Mapped[int] = mapped_column(Integer, default=100)
    activations_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromocodeActivation(Base):
    __tablename__ = "promocode_activations"
    __table_args__ = (UniqueConstraint("promocode_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promocode_id: Mapped[int] = mapped_column(Integer, ForeignKey("promocodes.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"))
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    payload: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="XTR")
    charge_id: Mapped[str] = mapped_column(String(256), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CryptoInvoice(Base):
    """Счёт на оплату в долларах через CryptoBot (cb) или xRocket (xr)."""

    __tablename__ = "crypto_invoices"
    __table_args__ = (UniqueConstraint("provider", "invoice_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(8))
    invoice_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.tg_id"), index=True)
    payload: Mapped[str] = mapped_column(String(64))
    amount_usd: Mapped[str] = mapped_column(String(16))
    pay_url: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active | paid | expired
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class BattleVote(Base):
    __tablename__ = "battle_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    winner: Mapped[str] = mapped_column(String(64), index=True)
    loser: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
