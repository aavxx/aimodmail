"""
Norwegian Air Shuttle (Roblox) support plugin for Modmail.

Stages 2-4 — plugin structure, consent gate and AI pre-screen.

A new conversation is gated on a privacy notice, then pre-screened by Groq.
Questions the assistant can answer from the FAQ below are answered without a
thread ever being created; everything else falls through to Modmail, which
creates the thread exactly as it always did. Every failure path ends in that
fallthrough, so a user is never left without a response.

The handoff summary and ticket reference (stage 5) slot into `_gate` at the
marked seam.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import typing
from datetime import datetime, timedelta, timezone

import discord
import isodate
from discord.ext import commands
from pymongo import ReturnDocument

from core import checks
from core.models import DMDisabled, PermissionLevel, getLogger
from core.utils import AcceptButton, DenyButton, safe_typing

try:
    from groq import AsyncGroq
except ImportError:  # pragma: no cover - dependency install failed
    AsyncGroq = None

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

TYPE_META = "meta"

# How long the privacy notice waits for a button press. Deliberately much longer
# than Modmail's own 30s confirm view: this is something the user has to read.
CONSENT_TIMEOUT_SECONDS = 300

GROQ_MODEL = "llama-3.3-70b-versatile"

# The gate runs inside Modmail's per-user DM queue, so a hung request would stall
# that user's messages. Bounded twice: once by the client, once by wait_for.
GROQ_TIMEOUT_SECONDS = 20

# Turns of prior context sent back to Groq. Caps token spend on long chats.
AI_HISTORY_LIMIT = 12

# Escalation phrases, matched on word boundaries so "management" does not trip
# "agent" and "humanity" does not trip "human". Extend freely; each entry is a
# full regex checked case-insensitively against the user's message.
ESCALATION_PATTERNS = [
    r"\bagents?\b",
    r"\bhumans?\b",
    r"\brepresentatives?\b",
    r"\breal (?:person|people)\b",
    r"\b(?:talk|speak|chat)\s+(?:to|with)\s+(?:someone|somebody|a\s+person|staff|support|an?\s+\w+)\b",
    r"\b(?:live|actual|real)\s+(?:agent|support|person)\b",
    r"\bescalate\b",
]

_ESCALATION_RE = [re.compile(p, re.IGNORECASE) for p in ESCALATION_PATTERNS]

# Everything the assistant is allowed to answer from. Supplied by the group, not
# invented here. The model is instructed to hand off anything not covered, so a
# wrong entry becomes a confidently wrong answer while a missing one merely
# escalates. Keep it that way: delete rather than guess.
FAQ_KNOWLEDGE = """\
Membership and eligibility
- The minimum age to join the team is 13.
- Passengers must be a member of the Roblox group to attend flights.
- Alt accounts are not allowed.

Flights
- Every flight follows the same pattern, where XX is that flight's hour: the
  server opens at XX:00, locks at XX:20, boarding begins at XX:25, and the
  flight departs at XX:35.
- The hour itself varies by flight. NEVER state a specific hour or date. Give
  the pattern if asked how flights run, and point to the departures page or the
  Discord server's events for actual times.
- Departures: [departures.vuelingrbx.com](https://vuelingrbx.vercel.app/departures),
  or check the Discord server's events.

Fly Grande (priority boarding)
- Fly Grande is priority boarding, purchased in-game for 25 Robux.
- It is a one-time purchase and applies to a single flight.

Payments
- There are no refunds under any circumstances. State this plainly. Do not
  soften it, do not suggest exceptions, and do not offer to check or escalate a
  refund request.

Jobs and staff
- Open positions: [work.vuelingrbx.com](https://vuelingrbx.vercel.app/work)
- Publicly, only basic staff information may be given: the age requirement and
  the rank names. Nothing beyond that.
- The rank names are not listed in this reference. If asked to name the ranks,
  do not guess: hand off instead.
- Promotion and rank details are basic-only in public. Fuller detail is
  available once someone is hired.
- The uniform policy is internal, is covered in the employee handbook, and is
  not for public disclosure. If asked about uniform, say that it is staff-only
  information. That is a complete answer.

Moderation
- Ban appeals: [appeal.vuelingrbx.com](https://vuelingrbx.vercel.app/appeal)
- Giving someone the appeals link is a complete answer. Never comment on
  whether a specific ban or appeal was justified.
"""

SYSTEM_PROMPT = f"""\
You are the first-line automated support assistant for Norwegian Air Shuttle, a
virtual airline group on Roblox. You answer straightforward questions from
passengers and staff.

Answer ONLY from the reference information below. If the answer is not clearly
contained in it, you MUST NOT guess: set resolved to false and let a human take
over. Never invent flight times, prices, rank names, policies, or links.

Set resolved to false, with a brief reply, for any of these:
- the question is not covered by the reference information
- it concerns a specific individual's account, punishment, appeal outcome, or
  application outcome, or any other case-by-case judgement. Pointing someone to
  the appeals page is a complete answer and does not count as judging a case.
- someone reports a problem with a purchase they made, such as paying and not
  receiving what they paid for. The refund policy as a general question is
  covered above and should be answered plainly instead of handed off.
- the user seems upset, or has asked the same thing twice without being helped
- you are not confident the answer is correct

Set resolved to true only when you have fully answered a general question from
the reference information and no human follow-up is needed. Stating a policy
that the reference information gives you, including one the user will not like,
is a complete answer.

Links: when the reference information provides a link, reproduce it exactly as
written, in the same [display.domain](https://full.url) markdown form. Never
show a bare URL, never alter the display text or the target, and never invent a
link that is not in the reference information.

Keep replies under 900 characters, friendly and plain. Do not claim to be human.

Reference information:
{FAQ_KNOWLEDGE}

Respond with a single json object with exactly these keys:
  "resolved": boolean
  "reply": string
"""


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
        self._groq_client = None
        self._user_salt: typing.Optional[str] = None

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

            # Stage 4: AI pre-screen. True means the user has their answer and
            # no thread is needed.
            if await self._ai_prescreen(message):
                return

            # ---------------------------------------------------------- seam
            # Stage 5 (handoff summary and ticket reference) attaches here.
            # Until then an unresolved conversation goes to stock Modmail,
            # which creates the thread exactly as it always did.
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
    # AI pre-screen
    # ------------------------------------------------------------------

    async def _salt(self) -> str:
        """Fetch (or create once) the salt used to pseudonymise user IDs.

        A bare SHA-256 of a Discord snowflake is trivially reversible by
        enumeration, so the hash is keyed. The salt lives in the partition so it
        survives restarts; regenerating it orphans every existing transcript.
        """
        if self._user_salt is not None:
            return self._user_salt

        doc = await self.db.find_one_and_update(
            {"_type": TYPE_META, "key": "user_id_salt"},
            {"$setOnInsert": {"value": secrets.token_hex(32)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        self._user_salt = doc["value"]
        return self._user_salt

    async def _user_hash(self, user_id: int) -> str:
        salt = await self._salt()
        return hmac.new(salt.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()

    def _groq(self):
        """Lazily build the Groq client. None when unusable."""
        if self._groq_client is not None:
            return self._groq_client

        if AsyncGroq is None:
            logger.error("groq package is not installed; AI pre-screen disabled.")
            return None

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            logger.error("GROQ_API_KEY is not set; AI pre-screen disabled.")
            return None

        self._groq_client = AsyncGroq(api_key=api_key, timeout=GROQ_TIMEOUT_SECONDS)
        return self._groq_client

    @staticmethod
    def _escalation_match(text: str) -> typing.Optional[str]:
        for pattern in _ESCALATION_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    async def _open_transcript(self, user_id: int) -> typing.Optional[dict]:
        """The user's in-progress pre-screen, if one has not been handed off."""
        return await self.db.find_one(
            {
                "_type": TYPE_TRANSCRIPT,
                "user_id_hash": await self._user_hash(user_id),
                "handed_off_at": None,
            }
        )

    async def _append_transcript(
        self,
        user_id: int,
        user_text: str,
        assistant_text: typing.Optional[str] = None,
        resolved: typing.Optional[bool] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        entries = [{"role": "user", "content": user_text, "at": now}]
        if assistant_text is not None:
            entries.append({"role": "assistant", "content": assistant_text, "at": now})

        await self.db.update_one(
            {
                "_type": TYPE_TRANSCRIPT,
                "user_id_hash": await self._user_hash(user_id),
                "handed_off_at": None,
            },
            {
                "$push": {"messages": {"$each": entries}},
                "$set": {"resolved": resolved},
                "$setOnInsert": {
                    "created_at": now,
                    # TTL index on this field expires the transcript 7 days
                    # after it started. Only transcripts carry it.
                    "expires_at": now + timedelta(days=TRANSCRIPT_RETENTION_DAYS),
                },
            },
            upsert=True,
        )

    async def _groq_answer(self, history: list, user_text: str) -> typing.Tuple[bool, str]:
        """Ask Groq to answer or defer. Raises on any failure."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for entry in history[-AI_HISTORY_LIMIT:]:
            role = entry.get("role")
            content = entry.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        completion = await self._groq().chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=600,
        )

        payload = json.loads(completion.choices[0].message.content)
        resolved = payload.get("resolved")
        reply = payload.get("reply")

        # A malformed response must not be treated as a confident answer.
        if not isinstance(resolved, bool) or not isinstance(reply, str) or not reply.strip():
            raise ValueError(f"unusable Groq payload: {payload!r}")

        return resolved, reply.strip()

    async def _ai_prescreen(self, message: discord.Message) -> bool:
        """Try to resolve the message without a human.

        Returns True when the user has been answered and no thread is needed.
        Returns False to fall through to handoff. Never raises: every failure
        path ends in a handoff so the user always gets a response.
        """
        user = message.author
        content = (message.content or "").strip()

        matched = self._escalation_match(content)
        if matched:
            logger.info("Escalation phrase %r from %s (%s); handing off.", matched, user, user.id)
            await self._append_transcript(user.id, content, resolved=False)
            return False

        if self._groq() is None:
            await self._append_transcript(user.id, content, resolved=False)
            return False

        transcript = await self._open_transcript(user.id)
        history = (transcript or {}).get("messages", [])

        try:
            async with safe_typing(message.channel):
                resolved, reply = await asyncio.wait_for(
                    self._groq_answer(history, content),
                    timeout=GROQ_TIMEOUT_SECONDS,
                )
        except Exception:
            logger.error("Groq pre-screen failed for %s (%s); handing off.", user, user.id, exc_info=True)
            await self._append_transcript(user.id, content, resolved=False)
            return False

        await self._append_transcript(user.id, content, reply, resolved)

        if not resolved:
            logger.info("AI deferred for %s (%s); handing off.", user, user.id)
            return False

        try:
            await message.channel.send(embed=self._ai_embed(reply))
        except discord.HTTPException:
            logger.error("Failed delivering AI reply to %s; handing off.", user, exc_info=True)
            return False

        logger.info("AI resolved the request from %s (%s).", user, user.id)
        return True

    def _ai_embed(self, reply: str) -> discord.Embed:
        """The assistant's own reply. Custom; Modmail has no slot for this."""
        return self._embed(
            title="Norwegian Air Shuttle Support",
            description=reply,
            color=self.bot.mod_color,
            footer='Automated assistant • reply with "agent" to reach a human',
        )

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

        if AsyncGroq is None:
            ai_state = "**groq package missing** — every request escalates"
        elif not os.getenv("GROQ_API_KEY"):
            ai_state = "**GROQ_API_KEY not set** — every request escalates"
        else:
            ai_state = f"ready — `{GROQ_MODEL}`"
        embed.add_field(name="AI pre-screen", value=ai_state, inline=False)

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

        # Withdrawal has to take the pre-screen conversations with it, otherwise
        # data collected under that consent outlives it.
        transcripts = await self.db.delete_many(
            {"_type": TYPE_TRANSCRIPT, "user_id_hash": await self._user_hash(user.id)}
        )

        if not deleted and not transcripts.deleted_count:
            return await ctx.send(
                embed=self._embed(
                    description=f"Nothing on record for {user.mention}.",
                    color=self.bot.error_color,
                )
            )

        await ctx.send(
            embed=self._embed(
                description=(
                    f"Consent withdrawn for {user.mention} and "
                    f"{transcripts.deleted_count} assistant conversation(s) deleted. "
                    "They will see the privacy notice again the next time they "
                    "open a ticket.\n\n"
                    "Ticket transcripts with a human agent are Modmail's own logs "
                    "and are not touched by this; use `?logs` to review them."
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
