"""Auto-moderation: spam, duplicates, invites, links, caps, mentions, emojis, bad words + strike escalation.
Needs the Message Content intent."""
import datetime as dt
import re
import time
from collections import defaultdict, deque
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from util import admin, dm, mk_embed

Action = Literal["none", "warn", "timeout", "kick", "ban"]
RuleName = Literal["spam", "duplicates", "invites", "links", "caps", "mentions", "emojis", "badwords"]


def _rule(**kw):
    return {"enabled": False, "action": "none", "minutes": 10, **kw}


DEFAULT = {
    "enabled": False, "ignored_roles": [], "ignored_channels": [], "log_channel": None, "notify": True,
    "rules": {
        "spam": _rule(action="timeout", limit=5, seconds=5),
        "duplicates": _rule(action="timeout", limit=3, seconds=60),
        "invites": _rule(action="warn"),
        "links": _rule(action="warn", allowed_domains=[]),
        "caps": _rule(limit=70, min_length=10),
        "mentions": _rule(action="timeout", limit=5),
        "emojis": _rule(limit=10),
        "badwords": _rule(action="warn", words=[]),
    },
    "strikes": {"threshold": 3, "hours": 24, "action": "timeout", "minutes": 60},
}

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/[\w-]+", re.I)
URL_RE = re.compile(r"https?://([^\s/<>]+)", re.I)
EMOJI_RE = re.compile(r"<a?:\w+:\d+>|[\U0001F300-\U0001FAFF\u2600-\u27BF]")


class AutoMod(commands.Cog):
    automod = app_commands.Group(name="automod", description="Auto-moderation settings", guild_only=True,
                                 default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot
        self.hist = defaultdict(lambda: deque(maxlen=30))

    def cfg(self, gid):
        return db.cfg(gid, "automod", DEFAULT)

    # ------------------------------------------------------------ detection
    def detect(self, m: discord.Message, c: dict):
        R, txt, now = c["rules"], m.content, time.time()
        norm = txt.lower().strip()
        h = self.hist[(m.guild.id, m.author.id)]
        h.append((now, norm))

        r = R["spam"]
        if r["enabled"] and sum(1 for t, _ in h if now - t <= r["seconds"]) >= r["limit"]:
            h.clear()
            return "spam", f"{r['limit']}+ messages in {r['seconds']}s"
        r = R["duplicates"]
        if r["enabled"] and norm and sum(1 for t, x in h if now - t <= r["seconds"] and x == norm) >= r["limit"]:
            h.clear()
            return "duplicates", "repeated message"
        r = R["invites"]
        if r["enabled"] and INVITE_RE.search(txt):
            return "invites", "invite link"
        r = R["links"]
        if r["enabled"]:
            allowed = [d.lower() for d in r["allowed_domains"]]
            for host in URL_RE.findall(txt):
                host = host.lower().removeprefix("www.").split(":")[0]
                if not any(host == d or host.endswith("." + d) for d in allowed):
                    return "links", host
        r = R["caps"]
        if r["enabled"]:
            letters = [ch for ch in txt if ch.isalpha()]
            if len(letters) >= r["min_length"] and sum(ch.isupper() for ch in letters) * 100 / len(letters) >= r["limit"]:
                return "caps", f"{r['limit']}%+ capitals"
        r = R["mentions"]
        if r["enabled"] and len(m.mentions) + len(m.role_mentions) >= r["limit"]:
            return "mentions", "mass mention"
        r = R["emojis"]
        if r["enabled"] and len(EMOJI_RE.findall(txt)) >= r["limit"]:
            return "emojis", "emoji spam"
        r = R["badwords"]
        if r["enabled"]:
            for w in r["words"]:
                if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", txt, re.I):
                    return "badwords", "blocked word"
        return None

    # ------------------------------------------------------------ punishment
    async def apply(self, member: discord.Member, action: str, minutes: int, reason: str):
        try:
            if action == "timeout":
                await member.timeout(dt.timedelta(minutes=minutes), reason=reason)
            elif action == "kick":
                await member.kick(reason=reason)
            elif action == "ban":
                await member.ban(reason=reason, delete_message_seconds=0)
        except discord.HTTPException:
            pass

    async def handle(self, m: discord.Message, c: dict, rule: str, detail: str):
        r = c["rules"][rule]
        try:
            await m.delete()
        except discord.HTTPException:
            pass
        reason = f"AutoMod: {rule} ({detail})"
        case = db.add_case(m.guild.id, m.author.id, m.guild.me.id, f"AutoMod:{rule}", reason)
        if r["action"] != "none":
            await self.apply(m.author, r["action"], r["minutes"], reason)
        if r["action"] == "warn":
            await dm(m.author, f"⚠️ Warning from **{m.guild.name}**: your message broke the `{rule}` rule.")
        if c["notify"]:
            try:
                await m.channel.send(f"{m.author.mention} your message was removed (**{rule}**).", delete_after=6)
            except discord.HTTPException:
                pass
        s = c["strikes"]
        escalated = False
        if s["action"] != "none":
            n = db.count_cases(m.guild.id, m.author.id, "AutoMod:", int(time.time()) - s["hours"] * 3600)
            if n >= s["threshold"]:
                await self.apply(m.author, s["action"], s["minutes"], f"AutoMod: {n} strikes in {s['hours']}h")
                db.add_case(m.guild.id, m.author.id, m.guild.me.id, "Escalation", f"{n} AutoMod strikes")
                escalated = True
        ch = m.guild.get_channel(c["log_channel"]) if c["log_channel"] else None
        if ch:
            e = mk_embed("🤖 AutoMod", f"**User:** {m.author.mention} ({m.author.id})\n**Channel:** {m.channel.mention}\n"
                         f"**Rule:** {rule} - {detail}\n**Action:** {r['action']}{' + escalation' if escalated else ''}\n"
                         f"**Case:** #{case}", 0xFEE75C)
            if rule != "badwords" and m.content:
                e.add_field(name="Message", value=m.content[:1000], inline=False)
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if not m.guild or m.author.bot or not isinstance(m.author, discord.Member):
            return
        c = self.cfg(m.guild.id)
        if not c["enabled"]:
            return
        p = m.author.guild_permissions
        if p.administrator or p.manage_messages:
            return
        if m.channel.id in c["ignored_channels"] or any(r.id in c["ignored_roles"] for r in m.author.roles):
            return
        hit = self.detect(m, c)
        if hit:
            await self.handle(m, c, *hit)

    # ------------------------------------------------------------ commands
    @automod.command(name="toggle", description="Turn auto-moderation on/off")
    @admin
    async def toggle(self, inter: discord.Interaction, enabled: bool, notify: Optional[bool] = None):
        c = self.cfg(inter.guild_id)
        c["enabled"] = enabled
        if notify is not None:
            c["notify"] = notify
        db.save_cfg(inter.guild_id, "automod", c)
        await inter.response.send_message(f"AutoMod {'enabled' if enabled else 'disabled'}.", ephemeral=True)

    @automod.command(name="rule", description="Configure one rule (fill only what you want to change)")
    @app_commands.describe(rule="Which rule", enabled="Turn the rule on/off", action="Punishment (message is always deleted)",
                           minutes="Timeout length in minutes", limit="Threshold: messages / mentions / emojis / caps %",
                           seconds="Time window for spam & duplicates", min_length="Min letters before the caps check applies")
    @admin
    async def rule_cmd(self, inter: discord.Interaction, rule: RuleName, enabled: Optional[bool] = None,
                       action: Optional[Action] = None, minutes: Optional[app_commands.Range[int, 1, 40320]] = None,
                       limit: Optional[app_commands.Range[int, 1, 100]] = None,
                       seconds: Optional[app_commands.Range[int, 1, 600]] = None,
                       min_length: Optional[app_commands.Range[int, 1, 200]] = None):
        c = self.cfg(inter.guild_id)
        r = c["rules"][rule]
        for key, val in (("enabled", enabled), ("action", action), ("minutes", minutes),
                         ("limit", limit), ("seconds", seconds), ("min_length", min_length)):
            if val is not None and (key in r):
                r[key] = val
        db.save_cfg(inter.guild_id, "automod", c)
        await inter.response.send_message(f"Updated **{rule}**: `{ {k: v for k, v in r.items() if k not in ('words', 'allowed_domains')} }`",
                                          ephemeral=True)

    @automod.command(name="words", description="Manage the blocked-word list")
    @admin
    async def words(self, inter: discord.Interaction, action: Literal["add", "remove", "list"], word: Optional[str] = None):
        c = self.cfg(inter.guild_id)
        w = c["rules"]["badwords"]["words"]
        if action == "list":
            return await inter.response.send_message(", ".join(w) or "No words set.", ephemeral=True)
        if not word:
            return await inter.response.send_message("Give a word.", ephemeral=True)
        word = word.lower().strip()
        if action == "add" and word not in w:
            w.append(word)
        elif action == "remove" and word in w:
            w.remove(word)
        db.save_cfg(inter.guild_id, "automod", c)
        await inter.response.send_message(f"Blocked words: {len(w)}. (Enable with `/automod rule badwords enabled:True`)", ephemeral=True)

    @automod.command(name="domains", description="Allowed domains for the link rule")
    @admin
    async def domains(self, inter: discord.Interaction, action: Literal["add", "remove", "list"], domain: Optional[str] = None):
        c = self.cfg(inter.guild_id)
        d = c["rules"]["links"]["allowed_domains"]
        if action == "list":
            return await inter.response.send_message(", ".join(d) or "No domains allowed (all links blocked when the rule is on).", ephemeral=True)
        if not domain:
            return await inter.response.send_message("Give a domain like `youtube.com`.", ephemeral=True)
        domain = domain.lower().strip().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
        if action == "add" and domain not in d:
            d.append(domain)
        elif action == "remove" and domain in d:
            d.remove(domain)
        db.save_cfg(inter.guild_id, "automod", c)
        await inter.response.send_message(f"Allowed domains: {', '.join(d) or 'none'}", ephemeral=True)

    @automod.command(name="ignore", description="Exempt a role or channel from AutoMod")
    @admin
    async def ignore(self, inter: discord.Interaction, action: Literal["add", "remove"],
                     role: Optional[discord.Role] = None, channel: Optional[discord.TextChannel] = None):
        c = self.cfg(inter.guild_id)
        for obj, key in ((role, "ignored_roles"), (channel, "ignored_channels")):
            if obj is None:
                continue
            lst = c[key]
            if action == "add" and obj.id not in lst:
                lst.append(obj.id)
            elif action == "remove" and obj.id in lst:
                lst.remove(obj.id)
        db.save_cfg(inter.guild_id, "automod", c)
        await inter.response.send_message("Updated. Admins and members with Manage Messages are always exempt.", ephemeral=True)

    @automod.command(name="strikes", description="Escalation after repeated AutoMod hits")
    @admin
    async def strikes(self, inter: discord.Interaction, threshold: Optional[app_commands.Range[int, 2, 50]] = None,
                      hours: Optional[app_commands.Range[int, 1, 720]] = None, action: Optional[Action] = None,
                      minutes: Optional[app_commands.Range[int, 1, 40320]] = None):
        c = self.cfg(inter.guild_id)
        s = c["strikes"]
        for key, val in (("threshold", threshold), ("hours", hours), ("action", action), ("minutes", minutes)):
            if val is not None:
                s[key] = val
        db.save_cfg(inter.guild_id, "automod", c)
        await inter.response.send_message(f"{s['threshold']} hits in {s['hours']}h → `{s['action']}` ({s['minutes']}m).", ephemeral=True)

    @automod.command(name="logchannel", description="Where AutoMod reports are sent")
    @admin
    async def logchannel(self, inter: discord.Interaction, channel: discord.TextChannel):
        c = self.cfg(inter.guild_id)
        c["log_channel"] = channel.id
        db.save_cfg(inter.guild_id, "automod", c)
        await inter.response.send_message(f"AutoMod log channel: {channel.mention}", ephemeral=True)

    @automod.command(name="show", description="Show current AutoMod settings")
    @admin
    async def show(self, inter: discord.Interaction):
        c = self.cfg(inter.guild_id)
        e = mk_embed(f"AutoMod ({'ON' if c['enabled'] else 'OFF'})")
        for name, r in c["rules"].items():
            extra = ", ".join(f"{k}={v}" for k, v in r.items() if k in ("limit", "seconds", "min_length"))
            e.add_field(name=f"{'✅' if r['enabled'] else '❌'} {name}", value=f"{r['action']} {extra}".strip(), inline=True)
        s = c["strikes"]
        e.add_field(name="Strikes", value=f"{s['threshold']}/{s['hours']}h → {s['action']}", inline=False)
        await inter.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
