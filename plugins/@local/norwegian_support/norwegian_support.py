"""
Norwegian Air Shuttle (Roblox) support plugin for Modmail.

Stages 2-3 — plugin structure and consent gate.

A new conversation is gated on a privacy notice before Modmail creates a
thread. Once consent is on record the DM is handed straight to Modmail, so
behaviour from that point is stock. The AI pre-screen (stage 4) and handoff
(stage 5) slot into `_gate` at the marked seam.
"""

import asyncio
import typing
from datetime import datetime, timezone

import discord
import isodate
from discord.ext import commands

from core import checks
from core.models import DMDisabled, PermissionLevel, getLogger
from core.utils import AcceptButton, DenyButton

logger = getLogger(__name__)

# Bumped when the privacy notice text changes materially. A stored consent with
# a lower version is re-prompted once (stage 3).
POLICY_VERSION = 1

# Human-facing ticket reference prefix, mapped to Modmail's own log key.
TICKET_PREFIX = "NAS"

# Retention for stage-4 AI transcripts. Enforced by a MongoDB TTL index on
# `expires_at`; documents without that field (consents, ticket mappings) are
# ignored by the TTL monitor and kept indefinitely.
TRANSCRIPT_RETENTION_DAYS = 7

# Discriminators. get_plugin_partition() hands back a single collection
# (plugins.NorwegianSupport), so the logical collections share it.
TYPE_CONSENT = "consent"
TYPE_TRANSCRIPT = "ai_transcript"
TYPE_TICKET = "ticket"

# How long the privacy notice waits for a button press. Deliberately much longer
# than Modmail's own 30s confirm view: this is something the user has to read.
CONSENT_TIMEOUT_SECONDS = 300


class ConsentView(discord.ui.View):
    """Accept/decline view for the privacy notice.

    Shaped like Modmail's own ConfirmThreadCreationView, but that class hardcodes
    timeout=30 in __init__ with no parameter to override, which is far too short
    to read a privacy notice. The buttons themselves are Modmail's.
    """

    def __init__(self, timeout: float):
        super().__init__(timeout=timeout)
        self.value = None


class _LabelledAcceptButton(AcceptButton):
    """Modmail's AcceptButton with a text label alongside the emoji."""

    def __init__(self, custom_id: str, emoji: str, label: str):
        super().__init__(custom_id, emoji)
        self.label = label


class _LabelledDenyButton(DenyButton):
    """Modmail's DenyButton with a text label alongside the emoji."""

    def __init__(self, custom_id: str, emoji: str, label: str):
        super().__init__(custom_id, emoji)
        self.label = label


class NorwegianSupport(commands.Cog):
    """Consent gate and AI pre-screen ahead of Modmail's thread creation."""

    def __init__(self, bot):
        self.bot = bot
        # Modmail's documented plugin storage pattern. Partition name is derived
        # from this class name, so renaming the class orphans existing data.
        self.db = self.bot.api.get_plugin_partition(self)

        # Populated when the DM seam is installed; None means not installed.
        self._original_process_dm: typing.Optional[typing.Callable] = None
        self._ready = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        self._install_dm_hook()
        self.bot.loop.create_task(self._ensure_indexes())

    async def cog_unload(self) -> None:
        self._remove_dm_hook()

    def _install_dm_hook(self) -> None:
        """Wrap bot.process_dm_modmail so the plugin sees DMs before Modmail.

        Modmail has no gating hook in the thread lifecycle: confirm_thread_creation
        is inline in ThreadManager.create, and thread_initiate/thread_ready are
        fire-and-forget dispatches that cannot delay channel creation. Wrapping
        the per-user DM entry point is the only interception point that sits
        above all thread logic without forking core/.
        """
        current = self.bot.process_dm_modmail

        if getattr(current, "__nas_wrapped__", False):
            # A previous instance of this cog is still wrapped (reload without a
            # clean unload). Take over its original rather than nesting wrappers,
            # which would run the gate twice per DM.
            original = getattr(current, "__nas_original__", None)
            logger.warning("DM hook already installed; taking over the existing wrapper chain.")
        else:
            original = current

        self._original_process_dm = original

        # A closure rather than `self._gate` directly: attributes cannot be set
        # on bound methods, and each `self._gate` access returns a new object,
        # so the marker below would be invisible to the check above.
        async def wrapper(message: discord.Message) -> None:
            return await self._gate(message, original)

        wrapper.__nas_wrapped__ = True
        wrapper.__nas_original__ = original

        self.bot.process_dm_modmail = wrapper
        logger.info("Norwegian support DM hook installed.")

    def _remove_dm_hook(self) -> None:
        if self._original_process_dm is None:
            return

        if not getattr(self.bot.process_dm_modmail, "__nas_wrapped__", False):
            logger.warning("DM hook was replaced by something else; leaving it alone.")
            self._original_process_dm = None
            return

        original = self._original_process_dm
        class_method = getattr(type(self.bot), "process_dm_modmail", None)

        if getattr(original, "__func__", None) is class_method:
            # The original was the bot class's own method. Drop our instance
            # attribute so normal attribute lookup resumes, rather than leaving
            # a stale bound method permanently shadowing the class.
            self.bot.__dict__.pop("process_dm_modmail", None)
        else:
            # Something else was already wrapping it; hand that back untouched.
            self.bot.process_dm_modmail = original

        self._original_process_dm = None
        logger.info("Norwegian support DM hook removed.")

    async def _ensure_indexes(self) -> None:
        """Create the partition's indexes. Safe to run repeatedly."""
        await self.bot.wait_for_connected()
        try:
            await self.db.create_index([("_type", 1), ("user_id", 1)])
            await self.db.create_index([("_type", 1), ("user_id_hash", 1)])
            await self.db.create_index(
                [("nas_ref", 1)],
                unique=True,
                partialFilterExpression={"_type": TYPE_TICKET},
            )
            # TTL for stage-4 transcripts only. Documents lacking `expires_at`
            # (consents, ticket mappings) are never touched by the TTL monitor.
            await self.db.create_index([("expires_at", 1)], expireAfterSeconds=0)
        except Exception:
            logger.error("Failed creating plugin partition indexes.", exc_info=True)
        else:
            logger.info("Norwegian support storage indexes ready.")
        finally:
            self._ready.set()

    # ------------------------------------------------------------------
    # DM interception
    # ------------------------------------------------------------------

    async def _gate(self, message: discord.Message, passthrough: typing.Callable) -> None:
        """Runs ahead of Modmail's process_dm_modmail for every user DM.

        Fails open in every error path: if anything here raises, the DM is
        handed to Modmail so the user is never left without a response.
        """
        if passthrough is None:
            logger.error("DM gate invoked with no original handler; cannot fall back to Modmail.")
            return

        try:
            # Blocked users must never reach the consent notice. Checked without
            # send_message so Modmail still owns the block response and emoji.
            if await self.bot.is_blocked(message.author, channel=message.channel, send_message=False):
                return await passthrough(message)

            # An open thread means this conversation already belongs to a human.
            # From that point the plugin is transparent, permanently.
            if await self.bot.threads.find(recipient=message.author) is not None:
                return await passthrough(message)

            # Modmail's own refusals are checked before the consent notice, so we
            # never ask someone to accept a privacy policy for support that is
            # then declined anyway. Modmail still owns each of these responses.
            if self.bot.config["dm_disabled"] in (DMDisabled.NEW_THREADS, DMDisabled.ALL_THREADS):
                return await passthrough(message)

            if await self.bot.get_thread_cooldown(message.author):
                return await passthrough(message)

            min_chars = self.bot.config.get("thread_min_characters") or 0
            try:
                min_chars = int(min_chars)
            except (TypeError, ValueError):
                min_chars = 0
            if min_chars > 0 and len((message.content or "").strip()) < min_chars:
                return await passthrough(message)

            # Stage 3: consent gate.
            if not await self._ensure_consent(message):
                return

            # ---------------------------------------------------------- seam
            # Stage 4 (AI pre-screen) and stage 5 (handoff) attach here. Until
            # then a consented conversation goes straight to stock Modmail.
            # ---------------------------------------------------------------

            return await passthrough(message)

        except Exception:
            logger.error("Norwegian support gate failed; falling back to Modmail.", exc_info=True)
            try:
                return await passthrough(message)
            except Exception:
                logger.error("Fallback to Modmail also failed.", exc_info=True)

    # ------------------------------------------------------------------
    # Consent
    # ------------------------------------------------------------------

    async def _get_consent(self, user_id: int) -> typing.Optional[dict]:
        return await self.db.find_one({"_type": TYPE_CONSENT, "user_id": user_id})

    async def _store_consent(self, user_id: int) -> None:
        await self.db.update_one(
            {"_type": TYPE_CONSENT, "user_id": user_id},
            {
                "$set": {
                    "accepted_at": datetime.now(timezone.utc),
                    "policy_version": POLICY_VERSION,
                }
            },
            upsert=True,
        )

    async def _delete_consent(self, user_id: int) -> int:
        result = await self.db.delete_one({"_type": TYPE_CONSENT, "user_id": user_id})
        return result.deleted_count

    def _privacy_embed(self, *, renewal: bool) -> discord.Embed:
        """The privacy notice. Custom because Modmail has no config slot for it."""
        if renewal:
            intro = (
                "Our privacy notice has been updated since you last accepted it. "
                "Please review it again before we continue."
            )
        else:
            intro = "Before we open a support ticket, please read how we handle your " "information."

        embed = self._embed(
            title="Norwegian Air Shuttle — Support Privacy Notice",
            description=intro,
            footer=f"Policy version {POLICY_VERSION}",
        )
        embed.add_field(
            name="What we store",
            value=(
                "• Your Discord user ID\n"
                "• The messages you send while your ticket is open\n"
                "• The time you accepted this notice"
            ),
            inline=False,
        )
        embed.add_field(
            name="How long we keep it",
            value=(
                "Your ticket messages are deleted automatically **7 days** after the "
                "ticket is closed. Conversations with our automated assistant are "
                "deleted **7 days** after they happen.\n"
                "Your acceptance of this notice (user ID and timestamp only) is kept "
                "until you withdraw it, so we do not have to ask you again."
            ),
            inline=False,
        )
        embed.add_field(
            name="Who can read it",
            value=(
                "The Norwegian Air Shuttle support team, and airline executives " "reviewing ticket quality."
            ),
            inline=False,
        )
        embed.add_field(
            name="Your choice",
            value=(
                "Accepting lets us open your ticket. Declining means we cannot "
                "continue, but you can start again at any time by messaging us. "
                "To withdraw your acceptance later, ask any member of the support team."
            ),
            inline=False,
        )
        return embed

    async def _ensure_consent(self, message: discord.Message) -> bool:
        """Gate a new conversation on the privacy notice.

        Returns True when the conversation may continue. Prompts at most once per
        conversation, since the gate only runs when the user has no open thread.
        """
        user = message.author
        record = await self._get_consent(user.id)

        if record is not None and record.get("policy_version", 0) >= POLICY_VERSION:
            return True

        renewal = record is not None
        view = ConsentView(timeout=CONSENT_TIMEOUT_SECONDS)
        view.add_item(
            _LabelledAcceptButton(
                "nas-consent-accept",
                self.bot.config["confirm_thread_creation_accept"],
                "Accept",
            )
        )
        view.add_item(
            _LabelledDenyButton(
                "nas-consent-deny",
                self.bot.config["confirm_thread_creation_deny"],
                "Decline",
            )
        )

        try:
            prompt = await message.channel.send(embed=self._privacy_embed(renewal=renewal), view=view)
        except discord.HTTPException:
            # Cannot show the notice at all. Fail open rather than strand the
            # user in silence; Modmail handles the conversation as it always did.
            logger.error("Failed sending privacy notice to %s; passing through.", user, exc_info=True)
            return True

        await view.wait()

        if view.value is None:
            # Timed out. Mirrors Modmail's own confirm-timeout wording.
            try:
                await prompt.edit(view=None)
            except discord.HTTPException:
                logger.debug("Could not clear consent buttons after timeout.", exc_info=True)
            await message.channel.send(
                embed=self._embed(
                    title=self.bot.config["thread_cancelled"],
                    description="Timed out",
                    color=self.bot.error_color,
                )
            )
            logger.info("Consent prompt timed out for %s (%s).", user, user.id)
            return False

        if view.value is False:
            if renewal:
                # Declining an updated notice withdraws the earlier acceptance.
                await self._delete_consent(user.id)
            await message.channel.send(
                embed=self._embed(
                    title=self.bot.config["thread_cancelled"],
                    description=(
                        "We cannot open a support ticket without your acceptance of "
                        "the privacy notice, so this request has been cancelled.\n\n"
                        "If you change your mind, just message us again."
                    ),
                    color=self.bot.error_color,
                )
            )
            logger.info("Consent declined by %s (%s).", user, user.id)
            return False

        await self._store_consent(user.id)
        logger.info("Consent accepted by %s (%s), policy version %s.", user, user.id, POLICY_VERSION)
        return True

    # ------------------------------------------------------------------
    # Embed helpers
    # ------------------------------------------------------------------

    def _embed(
        self,
        *,
        title: typing.Optional[str] = None,
        description: typing.Optional[str] = None,
        color: typing.Optional[int] = None,
        footer: typing.Optional[str] = None,
    ) -> discord.Embed:
        """Build an embed in Modmail's own style.

        Colors come from bot.config at call time via the bot's colour
        properties, so `?config set main_color ...` takes effect without a
        reload and nothing here hardcodes a hex value.
        """
        embed = discord.Embed(
            color=self.bot.main_color if color is None else color,
            description=description,
        )
        if title is not None:
            embed.title = title
        if self.bot.config["show_timestamp"]:
            embed.timestamp = discord.utils.utcnow()
        if footer is not None:
            embed.set_footer(
                text=footer,
                icon_url=self.bot.get_guild_icon(guild=self.bot.guild, size=128),
            )
        return embed

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @commands.group(name="nas", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def nas(self, ctx):
        """Norwegian Air Shuttle support plugin."""
        await ctx.send_help(ctx.command)

    @nas.command(name="status")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def nas_status(self, ctx):
        """Show plugin wiring, storage and config state."""
        hooked = getattr(self.bot.process_dm_modmail, "__nas_wrapped__", False)

        try:
            counts = {
                "consents": await self.db.count_documents({"_type": TYPE_CONSENT}),
                "transcripts": await self.db.count_documents({"_type": TYPE_TRANSCRIPT}),
                "tickets": await self.db.count_documents({"_type": TYPE_TICKET}),
            }
            storage = "\n".join(f"`{k}`: {v}" for k, v in counts.items())
        except Exception as e:
            storage = f"unreachable: `{e}`"

        embed = self._embed(
            title="Norwegian support — status",
            footer=f"policy version {POLICY_VERSION}",
        )
        embed.add_field(
            name="DM hook",
            value="installed" if hooked else "**not installed**",
            inline=True,
        )
        embed.add_field(
            name="Partition",
            value=f"`{self.db.name}`",
            inline=True,
        )
        embed.add_field(
            name="Indexes",
            value="ready" if self._ready.is_set() else "pending",
            inline=True,
        )
        embed.add_field(name="Storage", value=storage, inline=False)
        embed.add_field(
            name="confirm_thread_creation",
            value=(
                "`off` — correct, the plugin gate replaces it"
                if not self.bot.config["confirm_thread_creation"]
                else "`on` — **turn this off**, it double-prompts ahead of the plugin gate"
            ),
            inline=False,
        )

        # The privacy notice promises ticket messages are deleted after 7 days.
        # That claim is only true while Modmail's own log expiry is configured.
        expiry = self.bot.config.get("log_expiration")
        embed.add_field(
            name="Ticket log retention",
            value=(
                f"`{isodate.duration_isoformat(expiry)}` — the privacy notice's " "deletion promise holds"
                if expiry and expiry != isodate.Duration()
                else (
                    "`Never` — **the privacy notice promises deletion after 7 days "
                    "and this makes that false.** Set `log_expiration` to `P7D` and "
                    "restart the bot."
                )
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @nas.command(name="consent")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def nas_consent(self, ctx, user: discord.User):
        """Show a user's stored consent record."""
        record = await self._get_consent(user.id)
        if record is None:
            return await ctx.send(
                embed=self._embed(
                    description=f"No consent on record for {user.mention}.",
                    color=self.bot.error_color,
                )
            )

        stored_version = record.get("policy_version", 0)
        embed = self._embed(
            title="Consent on record",
            description=f"{user.mention} (`{user.id}`)",
            footer=f"current policy version {POLICY_VERSION}",
        )
        embed.add_field(
            name="Policy version",
            value=(
                f"{stored_version}"
                if stored_version >= POLICY_VERSION
                else f"{stored_version} — **outdated**, will be re-prompted"
            ),
            inline=True,
        )
        accepted = record.get("accepted_at")
        if isinstance(accepted, datetime):
            embed.add_field(
                name="Accepted",
                value=discord.utils.format_dt(accepted.replace(tzinfo=timezone.utc), "F"),
                inline=True,
            )
        await ctx.send(embed=embed)

    @nas.command(name="revoke")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def nas_revoke(self, ctx, user: discord.User):
        """Withdraw a user's consent. They are re-prompted on their next ticket."""
        deleted = await self._delete_consent(user.id)
        if not deleted:
            return await ctx.send(
                embed=self._embed(
                    description=f"No consent on record for {user.mention}.",
                    color=self.bot.error_color,
                )
            )
        await ctx.send(
            embed=self._embed(
                description=(
                    f"Consent withdrawn for {user.mention}. They will see the privacy "
                    "notice again the next time they open a ticket."
                ),
            )
        )
        logger.info("Consent withdrawn for %s (%s) by %s.", user, user.id, ctx.author)

    @nas.command(name="ticket")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def nas_ticket(self, ctx, reference: str):
        """Look up a NAS-XXXXXX reference and its Modmail log."""
        doc = await self.db.find_one({"_type": TYPE_TICKET, "nas_ref": reference.upper()})
        if doc is None:
            return await ctx.send(
                embed=self._embed(
                    description=f"No ticket found for `{reference.upper()}`.",
                    color=self.bot.error_color,
                )
            )

        log_key = doc.get("log_key")
        embed = self._embed(title=doc["nas_ref"])
        embed.add_field(name="User", value=f"<@{doc['user_id']}>", inline=True)
        embed.add_field(name="Log key", value=f"`{log_key}`", inline=True)
        if log_key:
            embed.add_field(
                name="Log",
                value=f"{self.bot.config['log_url'].strip('/')}/{log_key}",
                inline=False,
            )
        created = doc.get("created_at")
        if isinstance(created, datetime):
            embed.timestamp = created.replace(tzinfo=timezone.utc)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(NorwegianSupport(bot))
