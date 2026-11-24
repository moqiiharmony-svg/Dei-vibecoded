"""Shared helpers for the cogs."""
import datetime as dt
import re
from typing import Optional

import discord
from discord import app_commands

BLURPLE = 0x5865F2

# Reusable permission checks (put under the @group.command decorator)
admin = app_commands.checks.has_permissions(manage_guild=True)
manage_msgs = app_commands.checks.has_permissions(manage_messages=True)

STYLES = {
    "blue": discord.ButtonStyle.primary,
    "grey": discord.ButtonStyle.secondary,
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
}
ColorName = str  # one of STYLES keys


def style_of(name: str) -> discord.ButtonStyle:
    return STYLES.get(name, discord.ButtonStyle.primary)


def mk_embed(title=None, desc=None, color=BLURPLE) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


def parse_color(value, default=BLURPLE) -> discord.Color:
    try:
        return discord.Color(int(str(value).lstrip("#"), 16))
    except (ValueError, TypeError):
        return discord.Color(default)


def valid_hex(value: Optional[str]) -> bool:
    return bool(re.fullmatch(r"#?[0-9a-fA-F]{6}", value or ""))


def clean(value: Optional[str]) -> Optional[str]:
    """'none' / 'off' / 'clear' clears an optional text setting."""
    if value is None:
        return None
    return None if value.strip().lower() in ("none", "off", "clear", "") else value


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt(text: Optional[str], member: discord.abc.User, **extra) -> Optional[str]:
    """Replace {user} {username} {displayname} {id} {server} {count} {ordinal} + any extras."""
    if not text:
        return text
    g = getattr(member, "guild", None)
    rep = {
        "user": member.mention,
        "username": member.name,
        "displayname": member.display_name,
        "id": str(member.id),
        "server": g.name if g else "",
        "count": str(g.member_count) if g else "",
        "ordinal": ordinal(g.member_count or 0) if g else "",
    }
    rep.update({k: str(v) for k, v in extra.items()})
    for k, v in rep.items():
        text = text.replace("{" + k + "}", v)
    return text.replace("\\n", "\n")


def parse_duration(text: str) -> Optional[dt.timedelta]:
    parts = re.findall(r"(\d+)\s*([smhdw])", (text or "").lower())
    if not parts:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = sum(int(n) * mult[u] for n, u in parts)
    return dt.timedelta(seconds=total) if total > 0 else None


def slug(name: str, limit: int = 30) -> str:
    return re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-").replace("_", "-"))[:limit]


async def dm(user, text: Optional[str] = None, embed: Optional[discord.Embed] = None, **kw) -> bool:
    try:
        await user.send(text, embed=embed, **kw)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def bar(done: int, total: int, width: int = 20) -> str:
    filled = 0 if total <= 0 else min(width, int(width * done / total))
    return "█" * filled + "░" * (width - filled)
    
