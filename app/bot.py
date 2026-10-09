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

from . import tickets, trips
from .jobs import Engine

log = logging.getLogger(__name__)
VELVET = 0xA32A3C  # the brand's identity colour: the embed edge


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


def _line(i: dict, tail: str) -> str:
    return f"**{i['title']}** · {i['predicted']:.1f}★ · {tail}"


def plan_embeds(plan: dict, dashboard_url: str) -> list[discord.Embed]:
    """The monthly DM: the decision first, then one line per film (brand guide, Discord DM)."""
    left = plan.get("tickets_left", 0)
    month = (plan.get("month_label") or "").split(" ")[0]
    m0 = (plan.get("months") or [{}])[0]
    picks = plan.get("picks") or []
    paid = m0.get("paid_trips") or []
    worth = [i for i in (m0.get("worth_paying") or []) if i["film_id"] not in {p["film_id"] for p in picks}][:2]
    home = (m0.get("at_home") or [])[:1]
    e = discord.Embed(title=f"Your {month} plan", color=VELVET, url=dashboard_url or None)

    n_films = len(picks) + len(paid) + len(worth)
    if left:
        first = min((p["watch_by"] for p in picks if p.get("watch_by")), default=None)
        e.description = (f"{left} ticket{'s' if left != 1 else ''}, {n_films} film{'s' if n_films != 1 else ''} worth it."
                         + (f" Use one by {_d(first)}." if first else ""))
    else:
        e.description = (f"Your tickets are used this month. {len(worth)} more worth paying for."
                         if worth else "Your tickets are used this month.")

    lines = [_line(p, f"go by {_d(p.get('watch_by'))}") for p in picks]
    lines += [_line(p, f"paid trip, go by {_d(p.get('watch_by'))}") for p in paid]
    lines += [_line(i, "worth paying for") for i in worth]
    lines += [_line(i, "Catching later") for i in home]
    if lines:
        e.add_field(name="\u200b", value="\n".join(lines)[:1024], inline=False)
    wants = plan.get("wants") or {}
    if wants.get("paid"):
        e.add_field(name="Want to see", inline=False,
                    value=f"Your free tickets cover {wants['free']} of the {wants['total']} films you want to see; "
                          f"{len(wants['paid'])} need{'s' if len(wants['paid']) == 1 else ''} a paid trip.")
    ahead = [m for m in (plan.get("months") or [])[1:3] if m.get("picks")]
    if ahead:
        e.add_field(name="Looking ahead", inline=False, value="\n".join(
            f"{m['month_name']}: " + ", ".join(i["title"] for i in m["picks"][:3]) for m in ahead))
    return [e]


class ShowingView(discord.ui.View):
    """'Which showing did you book?': one button per listed showing (custom_ids handled in on_interaction)."""

    def __init__(self, options: list[dict]):
        super().__init__(timeout=None)
        for i, o in enumerate(options[:20]):
            self.add_item(discord.ui.Button(label=o["label"][:80], style=discord.ButtonStyle.secondary,
                                            custom_id=f"ls:show:{o['session_id']}"[:100], row=i // 5))


class PlanView(discord.ui.View):
    """Booked / Want to see / Hide this film buttons. custom_ids are handled in on_interaction so they keep working after restarts."""

    def __init__(self, plan: dict, dashboard_url: str):
        super().__init__(timeout=None)
        # one row per pick: used / seeing it / not for me (Discord allows 5 rows, the last is the dashboard link)
        for row, p in enumerate(plan.get("picks", [])[:4]):
            short = p["title"] if len(p["title"]) <= 40 else p["title"][:39] + "…"
            self.add_item(discord.ui.Button(label=f"Booked: {short}", style=discord.ButtonStyle.primary,
                                            custom_id=f"ls:use:{p['film_id']}"[:100], row=row))
            if not p.get("wanted"):
                self.add_item(discord.ui.Button(label="Want to see", style=discord.ButtonStyle.secondary,
                                                custom_id=f"ls:want:{p['film_id']}"[:100], row=row))
            self.add_item(discord.ui.Button(label="Hide this film", style=discord.ButtonStyle.secondary,
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
    async def send_alerts(self, messages: list[str]) -> bool:
        """One DM per refresh, only when something changed: a pinned or wanted film lost or back, or a break alert."""
        try:
            user = await self.fetch_user(self.owner)
            await user.send("**Last Showing**\n" + "\n\n".join(messages))
            return True
        except Exception as e:
            log.warning("couldn't DM the alert: %s", e)
            return False

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
            msg, view = self._use(film_id=film_id), self._showing_view(film_id)
            await interaction.response.send_message(msg, **({"view": view} if view else {}))
            return
        elif action == "show":
            got = trips.set_showing(self.engine.db, film_id)  # here the id is the showing's
            await interaction.response.send_message(
                f"Noted: **{got['title']}**, {got['label']}" + (f", {got['screen']}." if got.get("screen") else ".")
                if got else "That showing isn't listed any more.")
            return
        elif action == "pin":
            msg = self._pin(film_id)
        elif action == "want":
            msg = self._want(film_id)
        elif action == "nope":
            msg = self._dismiss(film_id)
        else:
            return
        await interaction.response.send_message(msg)

    def _showing_view(self, film_id: str | None) -> "ShowingView | None":
        """After a ticket is used: the showings to pick from, if Vue has listed any yet (else the dashboard asks later)."""
        if not film_id:
            return None
        db = self.engine.db
        trips.ensure(db)
        if not (db.one("SELECT 1 FROM ticket_uses WHERE film_id=? AND active=1", (film_id,))
                or db.one("SELECT 1 FROM cinema_trips WHERE film_id=? AND source='paid'", (film_id,))):
            return None
        opts = trips.showings(db, film_id, datetime.now(self.engine.s.tz).date())
        return ShowingView(opts) if opts else None

    def _film(self, film_id: str) -> tuple[str, int | None]:
        row = self.engine.db.one("SELECT title, tmdb_id FROM vue_films WHERE film_id=?", (film_id,))
        return (row["title"], row["tmdb_id"]) if row else (film_id, None)

    def _pin(self, film_id: str) -> str:
        title, _ = self._film(film_id)
        month = datetime.now(self.engine.s.tz).strftime("%Y-%m")
        self.engine.db.x("INSERT OR REPLACE INTO pins(film_id,month,created_at) VALUES(?,?,?)",
                         (film_id, month, datetime.now().isoformat(timespec="seconds")))
        self.engine.replan()
        self.engine.db.x("INSERT OR IGNORE INTO wants(film_id,title,created_at) VALUES(?,?,?)",
                         (film_id, title, datetime.now().isoformat(timespec="seconds")))
        return f"**{title}**: going this month, with one of this month's tickets (or a paid trip if they're used)."

    def _want(self, film_id: str) -> str:
        title, tid = self._film(film_id)
        self.engine.db.x("INSERT OR REPLACE INTO wants(film_id,tmdb_id,title,created_at) VALUES(?,?,?,?)",
                         (film_id, tid, title, datetime.now().isoformat(timespec="seconds")))
        w = (self.engine.replan() or {}).get("wants") or {}
        paid = len(w.get("paid") or [])
        tail = (f" You'd now need {paid} paid trip{'s' if paid != 1 else ''} to see everything you want."
                if paid else " Your free tickets still cover everything you want to see.")
        return f"**{title}** added to Want to see: it gets the next free ticket before it's likely to leave.{tail}"

    def _dismiss(self, film_id: str) -> str:
        title, tid = self._film(film_id)
        db = self.engine.db
        db.x("INSERT OR REPLACE INTO dismissed(film_id,tmdb_id,title,created_at) VALUES(?,?,?,?)",
             (film_id, tid, title, datetime.now().isoformat(timespec="seconds")))
        db.x("DELETE FROM pins WHERE film_id=?", (film_id,))
        plan = self.engine.replan()
        nxt = plan.get("picks") or []
        tail = f" Your picks are now: {', '.join(p['title'] for p in nxt)}." if nxt else ""
        return f"**{title}** is hidden and won't be suggested again (bring it back under Settings).{tail}"

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
        if (self.engine.plan() or {}).get("tickets_left", 1) <= 0:  # no free tickets left: a paid trip
            trips.record(db, film_id, tmdb_id, title, today, "paid", s.vue_cinema_name)
            self.engine.replan()
            ask = (" Which showing did you book?" if trips.showings(db, film_id, today) else
                   " I'll ask which showing you booked once Vue lists it.") if film_id else ""
            return f"Paid trip noted: **{title}** (this month's free tickets are used).{ask}"
        tickets.add_use(db, film_id, tmdb_id, title, today, "discord")
        plan = self.engine.replan()
        left = plan.get("tickets_left", 0)
        nxt = plan.get("picks") or []
        tail = f" Next up: **{nxt[0]['title']}**, see by {_d(nxt[0].get('watch_by'))}." if left and nxt else ""
        ask = (" Which showing did you book?" if trips.showings(db, film_id, today) else
               " I'll ask which showing you booked once Vue lists it.") if film_id else ""
        return f"Ticket used on **{title}**. {left} left this month.{tail}{ask}"

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
            view = bot._showing_view(film) if known else None
            await interaction.response.send_message(msg, **({"view": view} if view else {}))

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
            lines = [f"{'OK' if st['ok'] else 'Failed'} · {st['step']}: {st['result']}" for st in report["steps"]]
            await interaction.followup.send("\n".join(lines)[:1900] or "Done.")
