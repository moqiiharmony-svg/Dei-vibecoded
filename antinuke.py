"""Anti-nuke: detects a rogue admin / compromised account doing mass destructive actions (via audit log),
punishes them and optionally restores deleted channels. Bot needs View Audit Log (+ Administrator is best)."""
import asyncio
from collections import defaultdict, deque
import time
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from util import admin, mk_embed

A = discord.AuditLogAction
TRACKED = {
    "channel_delete": A.channel_delete, "channel_create": A.channel_create,
    "role_delete": A.role_delete, "role_create": A.role_create,
    "ban": A.ban, "kick": A.kick, "webhook_create": A.webhook_create,
}
ActionName = Literal["channel_delete", "channel_create", "role_delete", "role_create", "ban", "kick", "webhook_create"]
DEFAULT = {
    "enabled": False, "punishment": "strip",  # strip | kick | ban
    "whitelist": [], "block_bots": False, "restore": True, "log_channel": None,
    "limits": {
        "channel_delete": {"count": 3, "seconds": 10}, "channel_create": {"count": 5, "seconds": 10},
        "role_delete": {"count": 3, "seconds": 10}, "role_create": {"count": 5, "seconds": 10},
        "ban": {"count": 3, "seconds": 10}, "kick": {"count": 3, "seconds": 10},
        "webhook_create": {"count": 3, "seconds": 10},
    },
}


def dangerous(role: discord.Role) -> bool:
    p = role.permissions
    return any((p.administrator, p.manage_guild, p.manage_roles, p.manage_channels,
                p.kick_members, p.ban_members, p.manage_webhooks))


class AntiNuke(commands.Cog):
    antinuke = app_commands.Group(name="antinuke", description="Protection against rogue admins", guild_only=True,
                                  default_permissions=discord.Permissions(administrator=True))

    def __init__(self, bot):
        self.bot = bot
        self.events = defaultdict(deque)   # (guild, user, action) -> deque[(ts, snapshot)]

    def cfg(self, gid):
        return db.cfg(gid, "antinuke", DEFAULT)

    async def executor_of(self, guild, action, target_id=None):
        if not guild.me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(1)  # audit log entries lag slightly
        try:
            async for e in guild.audit_logs(limit=8, action=action):
                if (discord.utils.utcnow() - e.created_at).total_seconds() > 20:
                    break
                if target_id is None or (e.target is not None and getattr(e.target, "id", None) == target_id):
                    return e.user
        except discord.HTTPException:
            pass
        return None

    async def record(self, guild, name, target_id=None, snapshot=None):
        c = self.cfg(guild.id)
        if not c["enabled"]:
            return
        user = await self.executor_of(guild, TRACKED[name], target_id)
        if not user or user.id in (self.bot.user.id, guild.owner_id) or user.id in c["whitelist"]:
            return
        lim, now = c["limits"][name], time.time()
        q = self.events[(guild.id, user.id, name)]
        q.append((now, snapshot))
        while q and now - q[0][0] > lim["seconds"]:
            q.popleft()
        if len(q) >= lim["count"]:
            snaps = [s for _, s in q if s]
            q.clear()
            await self.punish(guild, user, name, c, snaps)

    async def punish(self, guild, user, name, c, snaps):
        member = guild.get_member(user.id)
        result = "failed (check my role position / permissions)"
        try:
            if c["punishment"] == "ban":
                await guild.ban(user, reason=f"Anti-nuke: mass {name}", delete_message_seconds=0)
                result = "banned"
            elif c["punishment"] == "kick" and member:
                await member.kick(reason=f"Anti-nuke: mass {name}")
                result = "kicked"
            elif member:
                bad = [r for r in member.roles[1:] if dangerous(r) and not r.managed and r < guild.me.top_role]
                if bad:
                    await member.remove_roles(*bad, reason=f"Anti-nuke: mass {name}")
                still = any(dangerous(r) for r in member.roles[1:])
                if still and member.bot:
                    await member.kick(reason="Anti-nuke")
                    result = "bot kicked"
                else:
                    result = f"stripped {len(bad)} dangerous role(s)"
        except discord.HTTPException:
            pass
        restored = 0
        if c["restore"] and snaps:
            for s in snaps:
                if await self.restore_channel(guild, s):
                    restored += 1
        db.add_case(guild.id, user.id, self.bot.user.id, "AntiNuke", f"mass {name}: {result}")
        e = mk_embed("🚨 Anti-nuke triggered", f"**User:** {user.mention} ({user.id})\n**Action:** mass `{name}`\n"
                     f"**Result:** {result}\n**Channels restored:** {restored}", 0xED4245)
        ch = guild.get_channel(c["log_channel"]) if c["log_channel"] else guild.system_channel
        if ch:
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                pass
        owner = guild.owner
        if owner:
            try:
                await owner.send(embed=e)
            except discord.HTTPException:
                pass

    async def restore_channel(self, guild, s) -> bool:
        try:
            cat = guild.get_channel(s["category"]) if s["category"] else None
            ow = {}
            for tid, (allow, deny, is_role) in s["overwrites"].items():
                target = guild.get_role(int(tid)) if is_role else guild.get_member(int(tid))
                if target:
                    ow[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(allow), discord.Permissions(deny))
            if s["type"] == "text":
                await guild.create_text_channel(s["name"], category=cat, overwrites=ow, topic=s["topic"], position=s["position"])
            elif s["type"] == "voice":
                await guild.create_voice_channel(s["name"], category=cat, overwrites=ow, position=s["position"])
            elif s["type"] == "category":
                await guild.create_category(s["name"], overwrites=ow, position=s["position"])
            else:
                return False
            return True
        except discord.HTTPException:
            return False

    # ------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, ch):
        kind = ("text" if isinstance(ch, discord.TextChannel) else "voice" if isinstance(ch, discord.VoiceChannel)
                else "category" if isinstance(ch, discord.CategoryChannel) else "other")
        snap = {"name": ch.name, "type": kind, "category": ch.category_id, "topic": getattr(ch, "topic", None),
                "position": ch.position,
                "overwrites": {str(t.id): (o.pair()[0].value, o.pair()[1].value, isinstance(t, discord.Role))
                               for t, o in ch.overwrites.items()}}
        await self.record(ch.guild, "channel_delete", ch.id, snap)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch):
        await self.record(ch.guild, "channel_create", ch.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self.record(role.guild, "role_delete", role.id)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await self.record(role.guild, "role_create", role.id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await self.record(guild, "ban", user.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if self.cfg(member.guild.id)["enabled"]:
            await self.record(member.guild, "kick", member.id)   # only counts if the audit log shows a kick

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        await self.record(channel.guild, "webhook_create")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        c = self.cfg(member.guild.id)
        if not (c["enabled"] and c["block_bots"] and member.bot):
            return
        adder = await self.executor_of(member.guild, A.bot_add, member.id)
        if adder and (adder.id == member.guild.owner_id or adder.id in c["whitelist"]):
            return
        try:
            await member.kick(reason="Anti-nuke: bots must be added by the owner/whitelist")
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ commands
    @antinuke.command(name="enable", description="Turn anti-nuke on/off")
    @admin
    async def enable(self, inter: discord.Interaction, enabled: bool):
        c = self.cfg(inter.guild_id)
        c["enabled"] = enabled
        db.save_cfg(inter.guild_id, "antinuke", c)
        await inter.response.send_message(f"Anti-nuke {'enabled' if enabled else 'disabled'}. The server owner is always trusted.", ephemeral=True)

    @antinuke.command(name="settings", description="Punishment, bot blocking, channel restore, alert channel")
    @admin
    async def settings(self, inter: discord.Interaction, punishment: Optional[Literal["strip", "kick", "ban"]] = None,
                       block_bots: Optional[bool] = None, restore: Optional[bool] = None,
                       log_channel: Optional[discord.TextChannel] = None):
        c = self.cfg(inter.guild_id)
        if punishment: c["punishment"] = punishment
        if block_bots is not None: c["block_bots"] = block_bots
        if restore is not None: c["restore"] = restore
        if log_channel: c["log_channel"] = log_channel.id
        db.save_cfg(inter.guild_id, "antinuke", c)
        await inter.response.send_message("Saved.", ephemeral=True)

    @antinuke.command(name="limit", description="Set how many actions in how many seconds trigger a punishment")
    @admin
    async def limit(self, inter: discord.Interaction, action: ActionName, count: app_commands.Range[int, 1, 50],
                    seconds: app_commands.Range[int, 1, 300]):
        c = self.cfg(inter.guild_id)
        c["limits"][action] = {"count": count, "seconds": seconds}
        db.save_cfg(inter.guild_id, "antinuke", c)
        await inter.response.send_message(f"`{action}`: {count} in {seconds}s.", ephemeral=True)

    @antinuke.command(name="whitelist", description="Trusted users who are never punished")
    @admin
    async def whitelist(self, inter: discord.Interaction, action: Literal["add", "remove", "list"],
                        user: Optional[discord.User] = None):
        c = self.cfg(inter.guild_id)
        w = c["whitelist"]
        if action == "add" and user and user.id not in w:
            w.append(user.id)
        elif action == "remove" and user and user.id in w:
            w.remove(user.id)
        db.save_cfg(inter.guild_id, "antinuke", c)
        await inter.response.send_message("Whitelist: " + (", ".join(f"<@{u}>" for u in w) or "empty"), ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    @antinuke.command(name="show", description="Show anti-nuke settings")
    @admin
    async def show(self, inter: discord.Interaction):
        c = self.cfg(inter.guild_id)
        e = mk_embed(f"Anti-nuke ({'ON' if c['enabled'] else 'OFF'})",
                     f"Punishment: `{c['punishment']}` · Block bots: {c['block_bots']} · Restore channels: {c['restore']}")
        for k, v in c["limits"].items():
            e.add_field(name=k, value=f"{v['count']} / {v['seconds']}s")
        await inter.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AntiNuke(bot))
