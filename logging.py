"""Server logging: messages, members, roles, channels, voice, moderation. Each category can have its own channel."""
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import db
from util import admin

CATS = ("messages", "members", "roles", "channels", "voice", "moderation")
Cat = Literal["messages", "members", "roles", "channels", "voice", "moderation"]
DEFAULT = {"default": None, "channels": {}, "ignored_channels": []}
GREEN, RED, YELLOW, BLUE = 0x57F287, 0xED4245, 0xFEE75C, 0x5865F2


def E(title, desc="", color=BLUE) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())


class Logs(commands.Cog):
    logs = app_commands.Group(name="logs", description="Server logging", guild_only=True,
                              default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot

    async def emit(self, guild: discord.Guild, cat: str, embed: discord.Embed):
        c = db.cfg(guild.id, "logs", DEFAULT)
        cid = c["channels"].get(cat) or c["default"]
        ch = guild.get_channel(cid) if cid else None
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.HTTPException:
                pass

    def ignored(self, guild_id, channel_id) -> bool:
        return channel_id in db.cfg(guild_id, "logs", DEFAULT)["ignored_channels"]

    # ---------------------------------------------------------------- messages
    @commands.Cog.listener()
    async def on_message_delete(self, m: discord.Message):
        if not m.guild or m.author.bot or self.ignored(m.guild.id, m.channel.id):
            return
        e = E("🗑️ Message deleted", f"**Author:** {m.author.mention}\n**Channel:** {m.channel.mention}", RED)
        if m.content:
            e.add_field(name="Content", value=m.content[:1000], inline=False)
        if m.attachments:
            e.add_field(name="Attachments", value="\n".join(a.url for a in m.attachments)[:1000], inline=False)
        e.set_footer(text=f"User ID: {m.author.id}")
        await self.emit(m.guild, "messages", e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot or before.content == after.content or self.ignored(after.guild.id, after.channel.id):
            return
        e = E("✏️ Message edited", f"**Author:** {after.author.mention}\n**Channel:** {after.channel.mention}\n[Jump]({after.jump_url})", YELLOW)
        e.add_field(name="Before", value=(before.content or "-")[:1000], inline=False)
        e.add_field(name="After", value=(after.content or "-")[:1000], inline=False)
        await self.emit(after.guild, "messages", e)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages):
        if not messages or not messages[0].guild:
            return
        m = messages[0]
        await self.emit(m.guild, "messages", E("🧹 Bulk delete", f"**{len(messages)}** messages deleted in {m.channel.mention}", RED))

    # ---------------------------------------------------------------- members
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        age = discord.utils.format_dt(member.created_at, "R")
        e = E("📥 Member joined", f"{member.mention} ({member})\n**Account created:** {age}", GREEN)
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"ID: {member.id}")
        await self.emit(member.guild, "members", e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        roles = ", ".join(r.mention for r in member.roles[1:]) or "none"
        e = E("📤 Member left", f"{member.mention} ({member})\n**Roles:** {roles[:900]}", RED)
        e.set_footer(text=f"ID: {member.id}")
        await self.emit(member.guild, "members", e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if before.nick != after.nick:
            await self.emit(g, "members", E("📝 Nickname changed", f"{after.mention}\n`{before.nick}` → `{after.nick}`"))
        added, removed = set(after.roles) - set(before.roles), set(before.roles) - set(after.roles)
        if added or removed:
            e = E("🏷️ Roles updated", after.mention)
            if added:
                e.add_field(name="Added", value=" ".join(r.mention for r in added))
            if removed:
                e.add_field(name="Removed", value=" ".join(r.mention for r in removed))
            await self.emit(g, "roles", e)
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                await self.emit(g, "moderation", E("⏳ Timeout", f"{after.mention} until {discord.utils.format_dt(after.timed_out_until)}", YELLOW))
            else:
                await self.emit(g, "moderation", E("✅ Timeout removed", after.mention, GREEN))

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await self.emit(guild, "moderation", E("🔨 Member banned", f"{user} ({user.id})", RED))

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        await self.emit(guild, "moderation", E("✅ Member unbanned", f"{user} ({user.id})", GREEN))

    # ---------------------------------------------------------------- channels & roles
    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch):
        await self.emit(ch.guild, "channels", E("📁 Channel created", f"{ch.mention} (`{ch.name}`)", GREEN))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, ch):
        await self.emit(ch.guild, "channels", E("🗑️ Channel deleted", f"`#{ch.name}`", RED))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if before.name != after.name:
            await self.emit(after.guild, "channels", E("✏️ Channel renamed", f"{after.mention}\n`{before.name}` → `{after.name}`"))
        elif getattr(before, "topic", None) != getattr(after, "topic", None):
            await self.emit(after.guild, "channels", E("✏️ Topic changed", f"{after.mention}\n{getattr(after, 'topic', None) or '-'}"))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await self.emit(role.guild, "roles", E("🏷️ Role created", f"{role.mention} (`{role.name}`)", GREEN))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self.emit(role.guild, "roles", E("🗑️ Role deleted", f"`{role.name}`", RED))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"Color: `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            diff = [n for (n, v), (_, v2) in zip(before.permissions, after.permissions) if v != v2]
            changes.append("Permissions changed: " + ", ".join(diff))
        if changes:
            await self.emit(after.guild, "roles", E("🏷️ Role updated", f"{after.mention}\n" + "\n".join(changes)))

    # ---------------------------------------------------------------- voice
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel == after.channel:
            return
        if before.channel is None:
            text = f"{member.mention} joined **{after.channel.name}**"
        elif after.channel is None:
            text = f"{member.mention} left **{before.channel.name}**"
        else:
            text = f"{member.mention} moved **{before.channel.name}** → **{after.channel.name}**"
        await self.emit(member.guild, "voice", E("🔊 Voice", text))

    # ---------------------------------------------------------------- commands
    @logs.command(name="set", description="Send a log category to a channel")
    @admin
    async def set_cmd(self, inter: discord.Interaction, category: Cat, channel: discord.TextChannel):
        c = db.cfg(inter.guild_id, "logs", DEFAULT)
        c["channels"][category] = channel.id
        db.save_cfg(inter.guild_id, "logs", c)
        await inter.response.send_message(f"`{category}` logs → {channel.mention}", ephemeral=True)

    @logs.command(name="default", description="Channel for every category without its own channel")
    @admin
    async def default_cmd(self, inter: discord.Interaction, channel: discord.TextChannel):
        c = db.cfg(inter.guild_id, "logs", DEFAULT)
        c["default"] = channel.id
        db.save_cfg(inter.guild_id, "logs", c)
        await inter.response.send_message(f"Default log channel: {channel.mention}", ephemeral=True)

    @logs.command(name="disable", description="Stop logging a category")
    @admin
    async def disable(self, inter: discord.Interaction, category: Cat):
        c = db.cfg(inter.guild_id, "logs", DEFAULT)
        c["channels"].pop(category, None)
        db.save_cfg(inter.guild_id, "logs", c)
        await inter.response.send_message(
            f"`{category}` now uses the default channel (or nothing if no default is set).", ephemeral=True)

    @logs.command(name="ignore", description="Don't log message events from a channel")
    @admin
    async def ignore(self, inter: discord.Interaction, channel: discord.TextChannel, action: Literal["add", "remove"] = "add"):
        c = db.cfg(inter.guild_id, "logs", DEFAULT)
        lst = c["ignored_channels"]
        if action == "add" and channel.id not in lst:
            lst.append(channel.id)
        elif action == "remove" and channel.id in lst:
            lst.remove(channel.id)
        db.save_cfg(inter.guild_id, "logs", c)
        await inter.response.send_message("Updated.", ephemeral=True)

    @logs.command(name="show", description="Show logging setup")
    @admin
    async def show(self, inter: discord.Interaction):
        c = db.cfg(inter.guild_id, "logs", DEFAULT)
        lines = [f"**default:** {('<#%s>' % c['default']) if c['default'] else 'not set'}"]
        for cat in CATS:
            cid = c["channels"].get(cat)
            lines.append(f"**{cat}:** {('<#%s>' % cid) if cid else 'default'}")
        await inter.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Logs(bot))
