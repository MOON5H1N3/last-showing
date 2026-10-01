"""Discord bot: DMs you the monthly plan and lets you mark tickets used.

Commands (work in your DM with the bot):
  /picks    this month's plan right now
  /used     mark a ticket used (autocompletes this month's films)
  /undo     give back the most recently used ticket
  /refresh  pull fresh listings and Letterboxd activity
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import discord
from discord import app_commands

from . import tickets
from .jobs import Engine

log = logging.getLogger(__name__)
PINK = 0xFFB020  # amber, to match the dashboard
AMBER = 0xE0A030


def _d(v: str | None, fmt: str = "%a %-d %b") -> str:
    if not v:
        return "?"
    try:
        return (datetime.fromisoformat(v) if "T" in v else datetime.fromisoformat(v + "T00:00")).strftime(fmt)
    except ValueError:
        return v


def _everywhere(cmd):
    """Let a command be used in DMs (and in a server, if you want)."""
    if hasattr(app_commands, "allowed_contexts"):
        cmd = app_commands.allowed_installs(guilds=True, users=True)(cmd)
        cmd = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(cmd)
    return cmd


def plan_embeds(plan: dict, dashboard_url: str) -> list[discord.Embed]:
    left = plan.get("tickets_left", 0)
    month = plan.get("month_label", "")
    head = discord.Embed(title=f"Last Showing: your {month} plan", color=PINK, url=dashboard_url or None)
    if left:
        head.description = (f"**{left} ticket{'s' if left != 1 else ''}** to use by **{_d(plan.get('month_end'))}**. "
                            "Picks are ordered by when you need to go.")
    else:
        head.description = "All your tickets are used this month. Anything below is worth paying for."
    m0 = (plan.get("months") or [{}])[0]
    worth = m0.get("worth_paying") or []
    if worth:
        head.add_field(name="Your paid trips" if m0.get("paid_planned") else "Worth paying for", inline=False,
                       value="\n".join(f"• **{i['title']}** {i['predicted']:.1f}★"
                                       + (f". {i['big_screen_note']}" if i.get("big_screen_note") else "")
                                       for i in worth[:4]))
    home = m0.get("at_home") or []
    if home:
        head.add_field(name="Catching later (fine at home)", inline=False, value="\n".join(
            f"• {i['title']} {i['predicted']:.1f}★" + (f", likely to rent from {_d(i['home_from'])}" if i.get("home_from") else "")
            for i in home[:3]))
    shown = {i["film_id"] for i in worth}
    leaving = [i for i in plan.get("leaving_soon") or [] if i["film_id"] not in shown]
    if leaving:
        head.add_field(name="Leaving soon", inline=False, value="\n".join(
            f"• **{i['title']}** {i['predicted']:.1f}★, likely gone by {_d(i['est_end'])}" for i in leaving[:3]))
    for m in (plan.get("months") or [])[1:3]:
        if m.get("picks"):
            head.add_field(name=f"Looking ahead: {m['month_name']}", inline=False, value="\n".join(
                f"• {'📌 ' if i.get('pinned') else ''}{i['title']} {i['predicted']:.1f}★"
                + (f", opens {_d(i['release_date'])}" if (i.get("release_date") or "") >= m["month_start"] else "")
                for i in m["picks"][:3]))
    if not plan.get("months") and plan.get("next_month"):
        head.add_field(name="Opening next month", inline=False, value="\n".join(
            f"• {i['title']} {i['predicted']:.1f}★, opens {_d(i['first_date'])}" for i in plan["next_month"][:3]))
    embeds = [head]
    for p in plan.get("picks", [])[:8]:
        e = discord.Embed(title=f"{p['title']} ({p['year']})" if p.get("year") else p["title"],
                          url=p.get("vue_url") or None, color=AMBER if p.get("p_next_month", 1) < 0.2 else PINK)
        e.description = (f"Predicted **{p['predicted']:.1f}★** · see by **{_d(p.get('watch_by'))}**\n"
                         f"*{p.get('end_note', '')}*")
        if p.get("reasons"):
            e.add_field(name="Why", value="\n".join(f"• {r}" for r in p["reasons"][:4])[:1024], inline=False)
        if p.get("sessions"):
            e.add_field(name="Next showings", inline=False, value="\n".join(
                f"[{_d(t['start'], '%a %-d %b, %H:%M')}]({t['url']}){' EPIC' if 'epic' in t['formats'] else ''}"
                for t in p["sessions"][:4])[:1024])
        elif p.get("first_date"):
            e.add_field(name="Showings", value=f"Opens {_d(p['first_date'])}; times usually appear the Tuesday before.",
                        inline=False)
        if p.get("poster"):
            e.set_thumbnail(url=p["poster"])
        embeds.append(e)
    return embeds[:10]


class PlanView(discord.ui.View):
    """'Used a ticket' buttons. custom_ids are handled in on_interaction so they keep working after restarts."""

    def __init__(self, plan: dict, dashboard_url: str):
        super().__init__(timeout=None)
        # one row per pick: used / seeing it / not for me (Discord allows 5 rows, the last is the dashboard link)
        for row, p in enumerate(plan.get("picks", [])[:4]):
            short = p["title"] if len(p["title"]) <= 40 else p["title"][:39] + "…"
            self.add_item(discord.ui.Button(label=f"Used a ticket: {short}", style=discord.ButtonStyle.primary,
                                            custom_id=f"ls:use:{p['film_id']}"[:100], row=row))
            if not p.get("pinned"):
                self.add_item(discord.ui.Button(label="I'm seeing this", style=discord.ButtonStyle.secondary,
                                                custom_id=f"ls:pin:{p['film_id']}"[:100], row=row))
            self.add_item(discord.ui.Button(label="Not for me", style=discord.ButtonStyle.secondary,
                                            custom_id=f"ls:nope:{p['film_id']}"[:100], row=row))
        if dashboard_url.startswith(("http://", "https://")):
            self.add_item(discord.ui.Button(label="Open dashboard", url=dashboard_url, row=4))


class VueBot(discord.Client):
    def __init__(self, engine: Engine):
        intents = discord.Intents.none()
        intents.guilds = True  # not privileged; avoids "Guilds intent seems to be disabled"
        super().__init__(intents=intents)
        self.engine = engine
        self.owner = engine.s.discord_user_id
        self.tree = app_commands.CommandTree(self)
        self._register()

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            log.info("Discord: synced %d slash commands (new commands can take a few minutes to appear)", len(synced))
        except Exception as e:
            log.warning("Discord: couldn't sync slash commands: %s", e)

    async def on_ready(self):
        log.info("Discord: logged in as %s", self.user)
        await self.check_setup()

    async def check_setup(self) -> dict:
        """Works out, up front, whether the bot will be able to DM you, and says why not in plain words."""
        problems, owner_name, shared = [], None, []
        guilds = list(self.guilds)
        try:
            owner = await self.fetch_user(self.owner)
            owner_name = str(owner)
            if owner.bot or owner.id == getattr(self.user, "id", None) or owner.id == self.application_id:
                problems.append(f"DISCORD_USER_ID ({self.owner}) belongs to a bot ({owner}), probably the Application ID "
                                "from the developer portal. It needs your own ID: right-click your name in Discord, "
                                "Copy User ID.")
        except discord.NotFound:
            problems.append(f"DISCORD_USER_ID ({self.owner}) doesn't match any Discord user. Right-click your name in "
                            "Discord and choose Copy User ID (turn on Developer Mode first).")
            owner = None
        if not guilds:
            invite = (f"https://discord.com/oauth2/authorize?client_id={self.application_id}"
                      "&scope=bot%20applications.commands&permissions=0")
            problems.append("The bot isn't in any server, and Discord only lets bots DM people they share a server with. "
                            f"Open this link to add it to your server: {invite}")
        elif owner and not problems:
            for g in guilds:
                try:
                    await g.fetch_member(owner.id)
                    shared.append(g.name)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            if not shared:
                problems.append(f"The bot is in {', '.join(g.name for g in guilds)}, but you aren't. Add the bot to a "
                                "server you're in.")
        status = {"bot": str(self.user), "servers": [g.name for g in guilds], "shared": shared, "owner": owner_name,
                  "problems": problems, "checked_at": datetime.now().isoformat(timespec="seconds")}
        self.engine.db.set("discord_status", status)
        for p in problems:
            log.error("Discord setup: %s", p)
        if not problems:
            log.info("Discord: will DM %s (you share %s with the bot)", owner_name, ", ".join(shared))
        return status

    # ---------- sending ----------
    async def send_monthly_plan(self, plan: dict) -> bool:
        if not plan:
            return False
        try:
            user = await self.fetch_user(self.owner)
            await user.send(embeds=plan_embeds(plan, self.engine.s.public_url),
                            view=PlanView(plan, self.engine.s.public_url))
            return True
        except discord.Forbidden as e:
            why = ("Discord says you don't accept DMs from this bot. Right-click the server you share with it, "
                   "open Privacy Settings and turn on Direct Messages." if getattr(e, "code", None) == 50007 else
                   f"Discord refused the DM ({getattr(e, 'code', '?')}: {getattr(e, 'text', e)}).")
            status = self.engine.db.get("discord_status") or {}
            status["problems"] = list(dict.fromkeys((status.get("problems") or []) + [why]))
            self.engine.db.set("discord_status", status)
            log.error("Discord: can't DM you. %s", why)
        except Exception as e:
            log.error("Discord: sending the plan failed: %s", e)
        return False

    # ---------- buttons ----------
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith(("ls:", "vuearr:")):  # "vuearr:" = buttons in DMs sent before the rename
            return
        if interaction.user.id != self.owner:
            await interaction.response.send_message("These tickets aren't yours.", ephemeral=True)
            return
        _, action, film_id = cid.split(":", 2)
        if action == "use":
            msg = self._use(film_id=film_id)
        elif action == "pin":
            msg = self._pin(film_id)
        elif action == "nope":
            msg = self._dismiss(film_id)
        else:
            return
        await interaction.response.send_message(msg)

    def _film(self, film_id: str) -> tuple[str, int | None]:
        row = self.engine.db.one("SELECT title, tmdb_id FROM vue_films WHERE film_id=?", (film_id,))
        return (row["title"], row["tmdb_id"]) if row else (film_id, None)

    def _pin(self, film_id: str) -> str:
        title, _ = self._film(film_id)
        month = datetime.now(self.engine.s.tz).strftime("%Y-%m")
        self.engine.db.x("INSERT OR REPLACE INTO pins(film_id,month,created_at) VALUES(?,?,?)",
                         (film_id, month, datetime.now().isoformat(timespec="seconds")))
        self.engine.replan()
        return f"📌 Pinned **{title}**: one of this month's tickets is kept for it."

    def _dismiss(self, film_id: str) -> str:
        title, tid = self._film(film_id)
        db = self.engine.db
        db.x("INSERT OR REPLACE INTO dismissed(film_id,tmdb_id,title,created_at) VALUES(?,?,?,?)",
             (film_id, tid, title, datetime.now().isoformat(timespec="seconds")))
        db.x("DELETE FROM pins WHERE film_id=?", (film_id,))
        plan = self.engine.replan()
        nxt = plan.get("picks") or []
        tail = f" Your picks are now: {', '.join(p['title'] for p in nxt)}." if nxt else ""
        return f"Got it, **{title}** won't be suggested again (you can bring it back on the dashboard).{tail}"

    def _use(self, film_id: str | None = None, title: str | None = None) -> str:
        db, s = self.engine.db, self.engine.s
        tmdb_id = None
        if film_id:
            row = db.one("SELECT title, tmdb_id FROM vue_films WHERE film_id=?", (film_id,))
            if row:
                title, tmdb_id = row["title"], row["tmdb_id"]
        if not title:
            return "I couldn't find that film."
        today = datetime.now(s.tz).date()
        month = today.strftime("%Y-%m")
        if film_id and db.one("SELECT 1 FROM ticket_uses WHERE month=? AND film_id=? AND active=1", (month, film_id)):
            return f"You've already used a ticket on **{title}** this month."
        tickets.add_use(db, film_id, tmdb_id, title, today, "discord")
        plan = self.engine.replan()
        left = plan.get("tickets_left", 0)
        nxt = plan.get("picks") or []
        tail = f" Next up: **{nxt[0]['title']}**, see by {_d(nxt[0].get('watch_by'))}." if left and nxt else ""
        return f"🎟️ Ticket used on **{title}**. {left} left this month.{tail}"

    # ---------- slash commands ----------
    def _register(self):
        bot = self

        async def owner_only(interaction: discord.Interaction) -> bool:
            if interaction.user.id != bot.owner:
                await interaction.response.send_message("This bot only answers its owner.", ephemeral=True)
                return False
            return True

        @_everywhere
        @self.tree.command(name="picks", description="This month's Vue plan")
        async def picks(interaction: discord.Interaction):
            if not await owner_only(interaction):
                return
            plan = bot.engine.plan() or bot.engine.replan()
            await interaction.response.send_message(embeds=plan_embeds(plan, bot.engine.s.public_url),
                                                    view=PlanView(plan, bot.engine.s.public_url))

        async def film_autocomplete(interaction: discord.Interaction, current: str):
            plan = bot.engine.plan() or {}
            cur = current.lower()
            opts = [c for c in plan.get("all_candidates", []) if cur in c["title"].lower()]
            return [app_commands.Choice(name=c["title"][:100], value=c["film_id"]) for c in opts[:25]]

        @_everywhere
        @self.tree.command(name="used", description="Mark a ticket as used")
        @app_commands.describe(film="Start typing a film showing this month")
        @app_commands.autocomplete(film=film_autocomplete)
        async def used(interaction: discord.Interaction, film: str):
            if not await owner_only(interaction):
                return
            known = bot.engine.db.one("SELECT 1 FROM vue_films WHERE film_id=?", (film,))
            msg = bot._use(film_id=film) if known else bot._use(title=film)
            await interaction.response.send_message(msg)

        @_everywhere
        @self.tree.command(name="undo", description="Give back the last ticket you marked as used")
        async def undo(interaction: discord.Interaction):
            if not await owner_only(interaction):
                return
            month = datetime.now(bot.engine.s.tz).strftime("%Y-%m")
            row = tickets.undo_latest(bot.engine.db, month)
            plan = bot.engine.replan()
            await interaction.response.send_message(
                f"Gave back the ticket for **{row['title']}**. {plan['tickets_left']} left." if row
                else "No tickets used this month.")

        @_everywhere
        @self.tree.command(name="refresh", description="Check Vue and Letterboxd for updates now")
        async def refresh(interaction: discord.Interaction):
            if not await owner_only(interaction):
                return
            if bot.engine.busy:
                await interaction.response.send_message("Already refreshing, give it a minute.")
                return
            await interaction.response.defer(thinking=True)
            report = await bot.engine.refresh()
            lines = [f"{'✅' if st['ok'] else '⚠️'} {st['step']}: {st['result']}" for st in report["steps"]]
            await interaction.followup.send("\n".join(lines)[:1900] or "Done.")
