"""Ticket system.

Concepts
  * TYPE   (/tickettype ...)  - what kind of ticket: roles, category, name format, welcome message,
                                questions (modal), limits, channel-or-thread mode...
  * PANEL  (/ticketpanel ...) - an embed with buttons or a dropdown that opens tickets of one or more types.
  * SETTINGS (/ticket settings) - logs, transcripts, claim, close behaviour, rating, auto-close...

Placeholders for names/welcome text: {user} {username} {displayname} {server} {type} {number}
"""
import asyncio
import html
import io
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from util import admin, clean, dm, fmt, mk_embed, parse_color, slug, style_of, valid_hex

SETTINGS = {
    "counter": 0, "log_channel": None, "closed_category": None, "claim": True, "close_confirm": True,
    "close_action": "delete",       # delete | archive
    "delete_delay": 5, "transcripts": True, "dm_transcript": True, "rating": False,
    "autoclose_hours": 0, "close_reason_required": False,
}
TYPE = {
    "label": "Support", "emoji": None, "color": "blue", "description": "", "category_id": None,
    "support_roles": [], "ping_roles": [], "required_roles": [], "blocked_roles": [],
    "name_format": "ticket-{number}", "mode": "channel",     # channel | thread
    "welcome_title": "{type} ticket #{number}",
    "welcome": "Hello {user}! A staff member will be with you shortly.\nPlease describe your issue in detail.",
    "welcome_color": "#5865F2", "welcome_image": None, "questions": [], "max_open": 1, "topic": "Ticket for {username}",
}
PANEL = {
    "channel_id": None, "message_id": None, "title": "Support", "description": "Press a button below to open a ticket.",
    "color": "#5865F2", "image": None, "thumbnail": None, "footer": None, "style": "buttons",
    "placeholder": "Choose a ticket type…", "types": [],
}
COLOR = Literal["blue", "grey", "green", "red"]


# ---------------------------------------------------------------- UI pieces
class OpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"tkt:open:(?P<type>[a-z0-9-]+)"):
    def __init__(self, type_name: str, label="Open ticket", emoji=None, style=discord.ButtonStyle.primary):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"tkt:open:{type_name}"))
        self.type_name = type_name

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["type"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Tickets").begin(interaction, self.type_name)


class SelectView(discord.ui.View):
    def __init__(self, options=None, placeholder="Choose a ticket type…"):
        super().__init__(timeout=None)
        sel = discord.ui.Select(custom_id="tkt:select", placeholder=placeholder, options=options or [], min_values=1, max_values=1)
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    async def picked(self, inter: discord.Interaction):
        cog = inter.client.get_cog("Tickets")
        type_name = self.sel.values[0]
        await cog.begin(inter, type_name)
        # reset the dropdown so the same option can be picked again
        try:
            for name, p in db.obj_list(inter.guild_id, "ticket_panel").items():
                if p.get("message_id") == inter.message.id:
                    await inter.message.edit(view=cog.panel_view(inter.guild_id, {**PANEL, **p}))
        except (discord.HTTPException, AttributeError):
            pass


class TicketControls(discord.ui.View):
    def __init__(self, claim=True):
        super().__init__(timeout=None)
        close = discord.ui.Button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="tkt:close")
        close.callback = self.on_close
        self.add_item(close)
        if claim:
            c = discord.ui.Button(label="Claim", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="tkt:claim")
            c.callback = self.on_claim
            self.add_item(c)

    async def on_close(self, inter):
        await inter.client.get_cog("Tickets").request_close(inter)

    async def on_claim(self, inter):
        await inter.client.get_cog("Tickets").claim_ticket(inter)


class ArchivedControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        r = discord.ui.Button(label="Reopen", emoji="🔓", style=discord.ButtonStyle.success, custom_id="tkt:reopen")
        d = discord.ui.Button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="tkt:delete")
        r.callback, d.callback = self.on_reopen, self.on_delete
        self.add_item(r)
        self.add_item(d)

    async def on_reopen(self, inter):
        await inter.client.get_cog("Tickets").reopen(inter)

    async def on_delete(self, inter):
        await inter.client.get_cog("Tickets").delete_archived(inter)


class ConfirmView(discord.ui.View):
    def __init__(self, cog, ticket_id, reason):
        super().__init__(timeout=60)
        self.cog, self.ticket_id, self.reason = cog, ticket_id, reason

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger)
    async def yes(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.edit_message(content="🔒 Closing…", view=None)
        t = db.row("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if t and t["status"] == "open":
            await self.cog.finish_close(inter.channel, t, inter.user, self.reason)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.edit_message(content="Cancelled.", view=None)


class ReasonModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Reason for closing", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, inter: discord.Interaction):
        await self.cog.request_close(inter, self.reason.value)


class QuestionModal(discord.ui.Modal):
    def __init__(self, cog, type_name, questions):
        super().__init__(title="Open a ticket", timeout=600)
        self.cog, self.type_name, self.inputs = cog, type_name, []
        for q in questions[:5]:
            optional = q.startswith("[opt]")
            text = q.removeprefix("[opt]").strip()
            ti = discord.ui.TextInput(label=text[:45], style=discord.TextStyle.paragraph, required=not optional, max_length=1000)
            self.add_item(ti)
            self.inputs.append((text, ti))

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        await self.cog.create(inter, self.type_name, [(q, ti.value) for q, ti in self.inputs])


class RatingView(discord.ui.View):
    def __init__(self, ticket_id):
        super().__init__(timeout=86400)
        self.ticket_id = ticket_id
        for i in range(1, 6):
            b = discord.ui.Button(label="⭐" * i, style=discord.ButtonStyle.secondary)
            b.callback = self._make(i)
            self.add_item(b)

    def _make(self, n):
        async def cb(inter: discord.Interaction):
            db.run("UPDATE tickets SET rating=? WHERE id=?", (n, self.ticket_id))
            await inter.response.edit_message(content=f"Thanks for your feedback: {'⭐' * n}", view=None)
        return cb


# ---------------------------------------------------------------- transcript
async def transcript_bytes(channel) -> bytes:
    rows = []
    async for m in channel.history(limit=2000, oldest_first=True):
        body = html.escape(m.content or "").replace("\n", "<br>")
        for emb in m.embeds:
            body += f"<div class='emb'><b>{html.escape(emb.title or '')}</b><br>{html.escape(emb.description or '')}"
            for f in emb.fields:
                body += f"<br><b>{html.escape(f.name)}</b>: {html.escape(f.value)}"
            body += "</div>"
        att = "".join(f"<div><a href='{a.url}'>{html.escape(a.filename)}</a></div>" for a in m.attachments)
        rows.append(f"<div class='m'><b>{html.escape(m.author.display_name)}</b> "
                    f"<small>{m.created_at:%Y-%m-%d %H:%M} UTC</small><div>{body}</div>{att}</div>")
    doc = ("<html><head><meta charset='utf-8'><title>Transcript</title><style>"
           "body{font-family:sans-serif;background:#313338;color:#dbdee1;padding:20px}"
           ".m{padding:6px 0;border-bottom:1px solid #3f4147}small{color:#949ba4}"
           ".emb{border-left:4px solid #5865f2;padding:4px 8px;margin:4px 0;background:#2b2d31}"
           "a{color:#00a8fc}</style></head><body><h2>#" + html.escape(channel.name) + "</h2>" + "".join(rows) + "</body></html>")
    return doc.encode("utf-8")


# ---------------------------------------------------------------- cog
class Tickets(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Ticket commands", guild_only=True)
    tickettype = app_commands.Group(name="tickettype", description="Manage ticket types", guild_only=True,
                                    default_permissions=discord.Permissions(manage_guild=True))
    ticketpanel = app_commands.Group(name="ticketpanel", description="Manage ticket panels", guild_only=True,
                                     default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(OpenButton)
        self.bot.add_view(SelectView())
        self.bot.add_view(TicketControls())
        self.bot.add_view(ArchivedControls())
        self.autoclose.start()

    async def cog_unload(self):
        self.autoclose.cancel()

    # ---- data helpers
    def settings(self, gid): return db.cfg(gid, "tickets", SETTINGS)
    def get_type(self, gid, name):
        t = db.obj_get(gid, "ticket_type", name)
        return {**TYPE, **t} if t is not None else None
    def get_panel(self, gid, name):
        p = db.obj_get(gid, "ticket_panel", name)
        return {**PANEL, **p} if p is not None else None
    def ticket_row(self, channel_id):
        return db.row("SELECT * FROM tickets WHERE channel_id=? AND status IN ('open','archived')", (channel_id,))

    def is_staff(self, member: discord.Member, ttype_name: str) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_channels:
            return True
        t = self.get_type(member.guild.id, ttype_name)
        return bool(t) and any(r.id in t["support_roles"] for r in member.roles)

    def can_manage(self, member, ticket) -> bool:
        return member.id == ticket["user_id"] or self.is_staff(member, ticket["ttype"])

    # ---- panel rendering
    def panel_embed(self, p) -> discord.Embed:
        e = discord.Embed(title=p["title"], description=p["description"], color=parse_color(p["color"]))
        if p["image"]: e.set_image(url=p["image"])
        if p["thumbnail"]: e.set_thumbnail(url=p["thumbnail"])
        if p["footer"]: e.set_footer(text=p["footer"])
        return e

    def panel_view(self, gid, p) -> discord.ui.View:
        types = [(n, self.get_type(gid, n)) for n in p["types"]]
        types = [(n, t) for n, t in types if t]
        if p["style"] == "dropdown":
            opts = [discord.SelectOption(label=t["label"][:100], value=n, description=(t["description"][:100] or None),
                                         emoji=t["emoji"] or None) for n, t in types[:25]]
            return SelectView(opts, p["placeholder"])
        v = discord.ui.View(timeout=None)
        for n, t in types[:25]:
            v.add_item(OpenButton(n, t["label"], t["emoji"], style_of(t["color"])))
        return v

    # ---- opening
    async def begin(self, inter: discord.Interaction, type_name: str):
        t = self.get_type(inter.guild_id, type_name)
        if not t:
            return await inter.response.send_message("That ticket type no longer exists.", ephemeral=True)
        m = inter.user
        if any(r.id in t["blocked_roles"] for r in m.roles):
            return await inter.response.send_message("You're not allowed to open this type of ticket.", ephemeral=True)
        if t["required_roles"] and not any(r.id in t["required_roles"] for r in m.roles):
            return await inter.response.send_message("You need a specific role to open this type of ticket.", ephemeral=True)
        if t["max_open"]:
            open_rows = db.rows("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND ttype=? AND status='open'",
                                (inter.guild_id, m.id, type_name))
            if len(open_rows) >= t["max_open"]:
                return await inter.response.send_message(f"You already have an open ticket: <#{open_rows[0]['channel_id']}>", ephemeral=True)
        if t["questions"]:
            return await inter.response.send_modal(QuestionModal(self, type_name, t["questions"]))
        await inter.response.defer(ephemeral=True)
        await self.create(inter, type_name, [])

    def make_name(self, t, number, user, type_name) -> str:
        name = (t["name_format"].replace("{number}", f"{number:04d}").replace("{username}", user.name)
                .replace("{type}", type_name))
        return slug(name, 90) or f"ticket-{number}"

    async def create(self, inter: discord.Interaction, type_name: str, answers: list):
        guild, user = inter.guild, inter.user
        t = self.get_type(guild.id, type_name)
        if not t:
            return await inter.followup.send("That ticket type no longer exists.", ephemeral=True)
        s = self.settings(guild.id)
        s["counter"] += 1
        db.save_cfg(guild.id, "tickets", s)
        number = s["counter"]
        name = self.make_name(t, number, user, type_name)
        support = [r for r in (guild.get_role(i) for i in t["support_roles"]) if r]
        base_perms = dict(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)
        try:
            if t["mode"] == "thread" and isinstance(inter.channel, discord.TextChannel):
                ch = await inter.channel.create_thread(name=name, type=discord.ChannelType.private_thread, invitable=False)
                await ch.add_user(user)
            else:
                cat = guild.get_channel(t["category_id"]) if t["category_id"] else getattr(inter.channel, "category", None)
                if not isinstance(cat, discord.CategoryChannel):
                    cat = None
                ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      user: discord.PermissionOverwrite(**base_perms),
                      guild.me: discord.PermissionOverwrite(**base_perms, manage_channels=True, manage_messages=True)}
                for r in support:
                    ow[r] = discord.PermissionOverwrite(**base_perms, manage_messages=True)
                ch = await guild.create_text_channel(name, category=cat, overwrites=ow,
                                                     topic=fmt(t["topic"], user, type=t["label"], number=number),
                                                     reason=f"Ticket by {user}")
        except discord.HTTPException as ex:
            return await inter.followup.send(f"I couldn't create the ticket channel ({ex.text}). Tell an admin.", ephemeral=True)

        db.run("INSERT INTO tickets(guild_id,channel_id,user_id,ttype,number,opened) VALUES(?,?,?,?,?,strftime('%s','now'))",
               (guild.id, ch.id, user.id, type_name, number))

        e = discord.Embed(title=fmt(t["welcome_title"], user, type=t["label"], number=number),
                          description=fmt(t["welcome"], user, type=t["label"], number=number),
                          color=parse_color(t["welcome_color"]), timestamp=discord.utils.utcnow())
        if t["welcome_image"]:
            e.set_image(url=t["welcome_image"])
        for q, a in answers:
            e.add_field(name=q[:256], value=(a or "-")[:1024], inline=False)
        ping = " ".join([user.mention] + [f"<@&{i}>" for i in t["ping_roles"]])
        await ch.send(content=ping, embed=e, view=TicketControls(claim=s["claim"]),
                      allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        if t["mode"] == "thread" and support:
            await ch.send(" ".join(r.mention for r in support), allowed_mentions=discord.AllowedMentions(roles=True), delete_after=3)
        await inter.followup.send(f"🎫 Your ticket is ready: {ch.mention}", ephemeral=True)

        lc = guild.get_channel(s["log_channel"]) if s["log_channel"] else None
        if lc:
            await lc.send(embed=mk_embed("🎫 Ticket opened", f"{ch.mention} · **{t['label']}** · #{number}\nBy {user.mention}", 0x57F287))

    # ---- claim / close / reopen
    async def claim_ticket(self, inter: discord.Interaction):
        t = self.ticket_row(inter.channel_id)
        if not t or t["status"] != "open":
            return await inter.response.send_message("This isn't an open ticket.", ephemeral=True)
        if not self.is_staff(inter.user, t["ttype"]):
            return await inter.response.send_message("Only staff can claim tickets.", ephemeral=True)
        if t["claimed_by"]:
            return await inter.response.send_message(f"Already claimed by <@{t['claimed_by']}>.", ephemeral=True)
        db.run("UPDATE tickets SET claimed_by=? WHERE id=?", (inter.user.id, t["id"]))
        await inter.response.send_message(embed=mk_embed("🙋 Ticket claimed", f"{inter.user.mention} will handle this ticket.", 0x57F287))
        if inter.message is not None:
            try:
                await inter.message.edit(view=TicketControls(claim=False))
            except discord.HTTPException:
                pass

    async def request_close(self, inter: discord.Interaction, reason: Optional[str] = None):
        t = self.ticket_row(inter.channel_id)
        if not t or t["status"] != "open":
            return await inter.response.send_message("This isn't an open ticket.", ephemeral=True)
        if not self.can_manage(inter.user, t):
            return await inter.response.send_message("Only staff or the ticket owner can close it.", ephemeral=True)
        s = self.settings(inter.guild_id)
        if s["close_reason_required"] and not reason:
            return await inter.response.send_modal(ReasonModal(self))
        if s["close_confirm"]:
            return await inter.response.send_message("Close this ticket?", view=ConfirmView(self, t["id"], reason), ephemeral=True)
        await inter.response.send_message("🔒 Closing ticket…")
        await self.finish_close(inter.channel, t, inter.user, reason)

    async def finish_close(self, channel, t: dict, closer, reason: Optional[str]):
        guild = channel.guild
        s = self.settings(guild.id)
        tt = self.get_type(guild.id, t["ttype"]) or TYPE
        opener = guild.get_member(t["user_id"])
        archive = s["close_action"] == "archive"
        db.run("UPDATE tickets SET status=?, closed=strftime('%s','now') WHERE id=?", ("archived" if archive else "closed", t["id"]))

        data = await transcript_bytes(channel) if s["transcripts"] else None
        fname = f"transcript-{t['number']:04d}.html"
        e = mk_embed("🔒 Ticket closed", f"**Ticket:** #{t['number']} ({tt['label']})\n**Opened by:** <@{t['user_id']}>\n"
                     f"**Closed by:** {closer.mention if hasattr(closer, 'mention') else closer}\n"
                     f"**Claimed by:** {('<@%s>' % t['claimed_by']) if t['claimed_by'] else 'nobody'}\n"
                     f"**Reason:** {reason or 'No reason provided'}", 0xED4245)
        lc = guild.get_channel(s["log_channel"]) if s["log_channel"] el
