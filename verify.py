"""Verification gate: button / math / code / custom-question modes, roles to give & remove,
account-age check, attempt limits, auto-kick of unverified members, fully custom panel look."""
import random
from collections import defaultdict
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from util import admin, clean, dm, mk_embed, parse_color, style_of, valid_hex

DEFAULT = {
    "enabled": False,
    "mode": "button",               # button | math | code | question
    "give_roles": [], "remove_roles": [], "unverified_role": None,
    "min_account_age_days": 0, "max_attempts": 3,
    "fail_action": "none",          # none | kick | timeout (when attempts run out)
    "kick_after_minutes": 0,        # kick members who stay unverified this long (0 = off)
    "log_channel": None, "dm_success": False,
    "question": "What is the secret word?", "answers": [],
    "panel": {"title": "Verification", "description": "Click the button below to verify and unlock the server.",
              "color": "#57F287", "image": None, "thumbnail": None, "footer": None,
              "button_label": "Verify", "button_emoji": "✅", "button_color": "green"},
    "messages": {"success": "✅ You're verified. Welcome!", "fail": "❌ Wrong answer. {left} attempt(s) left.",
                 "too_young": "Your account must be at least {days} days old to verify.",
                 "already": "You're already verified."},
}


class VerifyView(discord.ui.View):
    def __init__(self, label="Verify", style=discord.ButtonStyle.success, emoji=None):
        super().__init__(timeout=None)
        btn = discord.ui.Button(label=label, style=style, emoji=emoji, custom_id="verify:start")
        btn.callback = self.clicked
        self.add_item(btn)

    async def clicked(self, inter: discord.Interaction):
        await inter.client.get_cog("Verification").start(inter)


class AnswerModal(discord.ui.Modal):
    def __init__(self, cog, label, placeholder, check):
        super().__init__(title="Verification", timeout=300)
        self.cog, self.check = cog, check
        self.answer = discord.ui.TextInput(label=label[:45], placeholder=(placeholder or None), max_length=100)
        self.add_item(self.answer)

    async def on_submit(self, inter: discord.Interaction):
        await self.cog.finish(inter, self.check(self.answer.value.strip().lower()))


class Verification(commands.Cog):
    verification = app_commands.Group(name="verification", description="Verification gate", guild_only=True,
                                      default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot
        self.attempts = defaultdict(int)

    async def cog_load(self):
        self.bot.add_view(VerifyView())
        self.sweeper.start()

    async def cog_unload(self):
        self.sweeper.cancel()

    def cfg(self, gid):
        return db.cfg(gid, "verification", DEFAULT)

    def is_verified(self, member: discord.Member, c: dict) -> bool:
        if c["give_roles"]:
            return all(any(r.id == rid for r in member.roles) for rid in c["give_roles"])
        return bool(c["unverified_role"]) and not any(r.id == c["unverified_role"] for r in member.roles)

    # ------------------------------------------------------------- flow
    async def start(self, inter: discord.Interaction):
        c = self.cfg(inter.guild_id)
        msgs, member = c["messages"], inter.user
        if not c["enabled"]:
            return await inter.response.send_message("Verification is currently turned off.", ephemeral=True)
        if self.is_verified(member, c):
            return await inter.response.send_message(msgs["already"], ephemeral=True)
        if c["min_account_age_days"] and (discord.utils.utcnow() - member.created_at).days < c["min_account_age_days"]:
            return await inter.response.send_message(msgs["too_young"].replace("{days}", str(c["min_account_age_days"])), ephemeral=True)
        if self.attempts[(inter.guild_id, member.id)] >= c["max_attempts"]:
            return await inter.response.send_message("Too many failed attempts. Ask a moderator for help.", ephemeral=True)

        mode = c["mode"]
        if mode == "button":
            return await self.finish(inter, True)
        if mode == "math":
            a, b, op = random.randint(2, 15), random.randint(2, 15), random.choice(["+", "-", "x"])
            result = a + b if op == "+" else a - b if op == "-" else a * b
            return await inter.response.send_modal(AnswerModal(self, f"What is {a} {op} {b}?", "", lambda v: v == str(result)))
        if mode == "code":
            code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
            return await inter.response.send_modal(AnswerModal(self, f"Type this code: {code}", "", lambda v: v == code.lower()))
        q, answers = c["question"], [a.lower() for a in c["answers"]]
        label, placeholder = q[:45], (q[45:145] if len(q) > 45 else "")
        await inter.response.send_modal(AnswerModal(self, label, placeholder, lambda v: v in answers))

    async def finish(self, inter: discord.Interaction, ok: bool):
        c = self.cfg(inter.guild_id)
        guild, member, msgs = inter.guild, inter.user, c["messages"]
        key = (guild.id, member.id)
        if ok:
            self.attempts.pop(key, None)
            add = [r for r in (guild.get_role(i) for i in c["give_roles"]) if r]
            rem = [r for r in (guild.get_role(i) for i in c["remove_roles"] + ([c["unverified_role"]] if c["unverified_role"] else [])) if r]
            try:
                if add:
                    await member.add_roles(*add, reason="Verified")
                rem = [r for r in rem if r in member.roles]
                if rem:
                    await member.remove_roles(*rem, reason="Verified")
            except discord.HTTPException:
                return await inter.response.send_message(
                    "I couldn't update your roles. Tell an admin my role must be above the verification roles.", ephemeral=True)
            await inter.response.send_message(msgs["success"], ephemeral=True)
            if c["dm_success"]:
                await dm(member, f"✅ You are now verified in **{guild.name}**.")
            ch = guild.get_channel(c["log_channel"]) if c["log_channel"] else None
            if ch:
                await ch.send(embed=mk_embed("✅ Verified", f"{member.mention} ({member.id})", 0x57F287))
        else:
            self.attempts[key] += 1
            left = c["max_attempts"] - self.attempts[key]
            await inter.response.send_message(msgs["fail"].replace("{left}", str(max(left, 0))), ephemeral=True)
            if left <= 0:
                try:
                    if c["fail_action"] == "kick":
                        await member.kick(reason="Failed verification")
                    elif c["fail_action"] == "timeout":
                        import datetime as dt
                        await member.timeout(dt.timedelta(minutes=10), reason="Failed verification")
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------- events / tasks
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        c = self.cfg(member.guild.id)
        if c["enabled"] and c["unverified_role"] and not member.bot:
            role = member.guild.get_role(c["unverified_role"])
            if role:
                try:
                    await member.add_roles(role, reason="Unverified on join")
                except discord.HTTPException:
                    pass

    @tasks.loop(minutes=2)
    async def sweeper(self):
        for guild in self.bot.guilds:
            c = self.cfg(guild.id)
            if not (c["enabled"] and c["kick_after_minutes"] and c["unverified_role"]):
                continue
            role = guild.get_role(c["unverified_role"])
            if not role:
                continue
            for m in list(role.members):
                if m.joined_at and (discord.utils.utcnow() - m.joined_at).total_seconds() > c["kick_after_minutes"] * 60 \
                        and not m.guild_permissions.administrator:
                    try:
                        await dm(m, f"You were removed from **{guild.name}** for not verifying in time. You can rejoin and try again.")
                        await m.kick(reason="Did not verify in time")
                    except discord.HTTPException:
                        pass

    @sweeper.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- commands
    @verification.command(name="settings", description="Change verification settings (fill only what you want to change)")
    @app_commands.describe(mode="button, math, code (type a random code) or question (your own Q&A)",
                           unverified_role="Role given on join & removed on verify",
                           min_account_age_days="Reject accounts younger than this (0 = off)",
                           max_attempts="Wrong answers allowed", fail_action="When attempts run out",
                           kick_after_minutes="Kick members still unverified after this long (0 = off)",
                           log_channel="Log successful verifications", dm_success="DM members when verified")
    @admin
    async def settings(self, inter: discord.Interaction, enabled: Optional[bool] = None,
                       mode: Optional[Literal["button", "math", "code", "question"]] = None,
                       unverified_role: Optional[discord.Role] = None,
                       min_account_age_days: Optional[app_commands.Range[int, 0, 365]] = None,
                       max_attempts: Optional[app_commands.Range[int, 1, 10]] = None,
                       fail_action: Optional[Literal["none", "kick", "timeout"]] = None,
                       kick_after_minutes: Optional[app_commands.Range[int, 0, 10080]] = None,
                       log_channel: Optional[discord.TextChannel] = None, dm_success: Optional[bool] = None):
        c = self.cfg(inter.guild_id)
        for key, val in (("enabled", enabled), ("mode", mode), ("min_account_age_days", min_account_age_days),
                         ("max_attempts", max_attempts), ("fail_action", fail_action),
                         ("kick_after_minutes", kick_after_minutes), ("dm_success", dm_success)):
            if val is not None:
                c[key] = val
        if unverified_role:
            c["unverified_role"] = unverified_role.id
        if log_channel:
            c["log_channel"] = log_channel.id
        db.save_cfg(inter.guild_id, "verification", c)
        await inter.response.send_message("Saved. Use `/verification show` to review.", ephemeral=True)

    @verification.command(name="roles", description="Roles to give or remove when someone verifies")
    @admin
    async def roles(self, inter: discord.Interaction, kind: Literal["give", "remove"],
                    action: Literal["add", "remove"], role: discord.Role):
        c = self.cfg(inter.guild_id)
        lst = c["give_roles" if kind == "give" else "remove_roles"]
        if action == "add" and role.id not in lst:
            lst.append(role.id)
        elif action == "remove" and role.id in lst:
            lst.remove(role.id)
        db.save_cfg(inter.guild_id, "verification", c)
        await inter.response.send_message(f"{kind} roles: {', '.join(f'<@&{r}>' for r in lst) or 'none'}", ephemeral=True,
                                          allowed_mentions=discord.AllowedMentions.none())

    @verification.command(name="question", description="Set the custom question (mode: question)")
    @app_commands.describe(question="Keep it under 45 characters so it fits the pop-up", answers="Accepted answers, comma separated")
    @admin
    async def question(self, inter: discord.Interaction, question: str, answers: str):
        c = self.cfg(inter.guild_id)
        c["question"] = question
        c["answers"] = [a.strip().lower() for a in answers.split(",") if a.strip()]
        db.save_cfg(inter.guild_id, "verification", c)
        await inter.response.send_message(f"Question set with {len(c['answers'])} accepted answer(s). Use `/verification settings mode:question`.", ephemeral=True)

    @verification.command(name="style", description="Customize the panel embed and button (type 'none' to clear)")
    @admin
    async def style(self, inter: discord.Interaction, title: Optional[str] = None, description: Optional[str] = None,
                    color: Optional[str] = None, image: Optional[str] = None, thumbnail: Optional[str] = None,
                    footer: Optional[str] = None, button_label: Optional[str] = None, button_emoji: Optional[str] = None,
                    button_color: Optional[Literal["blue", "grey", "green", "red"]] = None):
        c = self.cfg(inter.guild_id)
        p = c["panel"]
        if title is not None: p["title"] = clean(title)
        if description is not None: p["description"] = (clean(description) or "").replace("\\n", "\n") or None
        if color is not None and valid_hex(color): p["color"] = color
        if image is not None: p["image"] = clean(image)
        if thumbnail is not None: p["thumbnail"] = clean(thumbnail)
        if footer is not None: p["footer"] = clean(footer)
        if button_label is not None: p["button_label"] = button_label[:80]
        if button_emoji is not None: p["button_emoji"] = clean(button_emoji)
        if button_color is not None: p["button_color"] = button_color
        db.save_cfg(inter.guild_id, "verification", c)
        await inter.response.send_message("Panel style saved. Run `/verification panel` to post it.", ephemeral=True)

    @verification.command(name="messages", description="Customize reply messages ({left} and {days} are available)")
    @admin
    async def messages(self, inter: discord.Interaction, success: Optional[str] = None, fail: Optional[str] = None,
                       too_young: Optional[str] = None, already: Optional[str] = None):
        c = self.cfg(inter.guild_id)
        for key, val in (("success", success), ("fail", fail), ("too_young", too_young), ("already", already)):
            if val:
                c["messages"][key] = val
        db.save_cfg(inter.guild_id, "verification", c)
        await inter.response.send_message("Messages saved.", ephemeral=True)

    @verification.command(name="panel", description="Post the verification panel")
    @admin
    async def panel(self, inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        c = self.cfg(inter.guild_id)
        p = c["panel"]
        channel = channel or inter.channel
        e = discord.Embed(title=p["title"], description=p["description"], color=parse_color(p["color"]))
        if p["image"]: e.set_image(url=p["image"])
        if p["thumbnail"]: e.set_thumbnail(url=p["thumbnail"])
        if p["footer"]: e.set_footer(text=p["footer"])
        view = VerifyView(p["button_label"], style_of(p["button_color"]), p["button_emoji"])
        await channel.send(embed=e, view=view)
        await inter.response.send_message(f"Panel posted in {channel.mention}.", ephemeral=True)

    @verification.command(name="show", description="Show verification settings")
    @admin
    async def show(self, inter: discord.Interaction):
        c = self.cfg(inter.guild_id)
        e = mk_embed(f"Verification ({'ON' if c['enabled'] else 'OFF'})")
        e.add_field(name="Mode", value=c["mode"])
        e.add_field(name="Attempts", value=f"{c['max_attempts']} → {c['fail_action']}")
        e.add_field(name="Min account age", value=f"{c['min_account_age_days']} days")
        e.add_field(name="Give roles", value=" ".join(f"<@&{r}>" for r in c["give_roles"]) or "none")
        e.add_field(name="Remove roles", value=" ".join(f"<@&{r}>" for r in c["remove_roles"]) or "none")
        e.add_field(name="Unverified role", value=f"<@&{c['unverified_role']}>" if c["unverified_role"] else "none")
        e.add_field(name="Kick unverified after", value=f"{c['kick_after_minutes']} min")
        if c["mode"] == "question":
            e.add_field(name="Question", value=c["question"], inline=False)
        await inter.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Verification(bot))
