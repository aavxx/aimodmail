"""
Norwegian Air Shuttle (Roblox) support plugin for Modmail.

Stages 2-4 — plugin structure, consent gate and AI pre-screen — plus the
post-disconnect survey and consented training set.

A new conversation is gated on a privacy notice, then pre-screened by Groq.
Questions the assistant can answer from the FAQ below are answered without a
thread ever being created; everything else falls through to Modmail, which
creates the thread exactly as it always did. Every failure path ends in that
fallthrough, so a user is never left without a response.

When the assistant itself ends a conversation — the user says goodbye, or the
session goes idle — the user is told they have been disconnected and offered a
one-minute survey: a 1-5 rating, and a yes/no on reusing the chat to improve the
assistant. A yes copies the (already pseudonymous) transcript into a training
set that outlives the 7-day transcript expiry. Conversations a human agent took
over never reach any of this; closing a ticket is Modmail's own flow.

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
from bson import ObjectId
from discord.ext import commands, tasks
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
#
# 2 — added the training-set paragraph. Keeping consented transcripts past the
#     7-day expiry makes version 1's "deleted after 7 days" wording false for
#     anyone who opts in, so everyone is re-prompted once.
POLICY_VERSION = 2

# What the assistant calls itself to users. Every user-facing string that names
# it reads this, so rebranding is a one-line change.
ASSISTANT_NAME = "Norwegian Air Shuttle AI"

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

# Post-disconnect survey and the consented training set.
TYPE_SESSION = "session"
TYPE_SURVEY = "survey"
TYPE_TRAINING = "training_transcript"

TYPE_META = "meta"

# Silence after which the assistant ends the conversation itself. Long enough
# that someone reading a reply and typing a follow-up is not cut off.
SESSION_IDLE_MINUTES = 15

# How often the idle sweeper runs. Disconnects land within this of the deadline.
SESSION_SWEEP_SECONDS = 60

# Conversations disconnected per sweep. A backlog is drained over successive
# ticks rather than firing hundreds of DMs into the rate limiter at once.
SESSION_SWEEP_BATCH = 50

# The `session` document is the only place a live AI conversation is tied back
# to a Discord user ID; the transcript itself is keyed by hash alone. It exists
# so the sweeper knows who to DM, and is deleted the moment the disconnect
# message goes out. This TTL is a backstop for sessions that never got that far.
SESSION_MAX_AGE_HOURS = 24

# Whether a 1-5 rating is kept when the user declines the training question. The
# rating document holds no conversation content — only the hashed user, the
# number and a timestamp — so it is feedback about the service rather than a
# copy of the chat. Set to False to discard ratings from anyone who said no.
KEEP_RATINGS_WITHOUT_CONSENT = True

# Longest message still treated as a sign-off rather than a new question.
FAREWELL_MAX_CHARS = 120

# Sign-offs that end the conversation immediately instead of waiting out the
# idle timer. Conservative on purpose: a false positive disconnects someone
# mid-question, so these only fire on a short message the assistant resolved.
FAREWELL_PATTERNS = [
    r"\b(?:bye|byebye|goodbye|good\s?bye|cya|see\s+(?:ya|you))\b",
    r"\bthat'?s\s+(?:all|it|everything)\b",
    r"\bno(?:thing)?\s+(?:more|else)\b",
    r"\ball\s+(?:good|sorted|done|set)\b",
    r"\b(?:never\s?mind|nvm)\b",
    r"\bhave\s+a\s+(?:good|nice|great)\s+(?:day|one|night|evening)\b",
]

_FAREWELL_RE = [re.compile(p, re.IGNORECASE) for p in FAREWELL_PATTERNS]

# Survey copy. Discord caps a modal label at 45 characters and its description
# at 100, so both questions are checked against that at import time below.
SURVEY_RATING_QUESTION = "How satisfied were you with our support?"
SURVEY_TRAINING_QUESTION = "Can we use this chat to help improve our AI?"
SURVEY_TRAINING_NOTE = "This is 100% anonymous."

SURVEY_RATING_OPTIONS = [
    ("5", "5 — Very satisfied"),
    ("4", "4 — Satisfied"),
    ("3", "3 — Neutral"),
    ("2", "2 — Dissatisfied"),
    ("1", "1 — Very dissatisfied"),
]

assert len(SURVEY_RATING_QUESTION) <= 45, "modal label is capped at 45 characters"
assert len(SURVEY_TRAINING_QUESTION) <= 45, "modal label is capped at 45 characters"
assert len(SURVEY_TRAINING_NOTE) <= 100, "modal description is capped at 100 characters"

# Samples shown by `?nas training` when no count is given.
TRAINING_SAMPLE_DEFAULT = 3
TRAINING_SAMPLE_MAX = 10

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

# Everything the assistant is allowed to answer from. REVIEW AND EDIT THIS: the
# model is instructed to hand off anything not covered here, so wrong entries
# become wrong answers given confidently, and missing entries simply escalate.
FAQ_KNOWLEDGE = """\
Norwegian Air Shuttle is a virtual airline group operating on Roblox. It runs
scheduled passenger flights, staff training, and a ranked staff structure.

Flights
- Flights are announced in the group's Discord announcement channels and in the
  Roblox group wall. There is no separate booking system; passengers join the
  flight game when the flight goes live.
- Passengers do not need to be group members to fly, but members get priority
  boarding at some events.
- Flight times are posted per event. There is no fixed daily timetable.

Staff and ranks
- Staff applications open periodically and are announced in Discord. Applying
  requires meeting the minimum age and account-age requirements stated in the
  application post.
- Promotions come from attending training sessions and passing assessments.
  Ranks are not sold and cannot be requested directly.
- Cabin crew, pilots, and ground staff each have their own training paths.

Conduct and moderation
- Rule breaking in flights or Discord is handled by the moderation team.
- Ban appeals are handled by the moderation team only, never by the assistant.

Uniform
- Uniform is issued through the group's uniform system. Members must wear the
  uniform matching their current rank while on duty.
"""

SYSTEM_PROMPT = f"""\
You are the first-line automated support assistant for Norwegian Air Shuttle, a
virtual airline group on Roblox. You answer straightforward questions from
passengers and staff.

Answer ONLY from the reference information below. If the answer is not clearly
contained in it, you MUST NOT guess: set resolved to false and let a human take
over. Never invent flight times, prices, ranks, policies, or names.

Set resolved to false, with a brief reply, for any of these:
- the question is not covered by the reference information
- it concerns a specific individual's account, ban, appeal, application outcome,
  or any other case-by-case decision
- it involves a complaint, a payment or refund, or anything with real money
- the user seems upset, or has asked the same thing twice without being helped
- you are not confident the answer is correct

Set resolved to true only when you have fully answered a general question from
the reference information and no human follow-up is needed.

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


def _survey_cog(interaction: discord.Interaction) -> typing.Optional["NorwegianSupport"]:
    """The live cog instance behind a survey interaction, or None if unloaded."""
    return interaction.client.get_cog("NorwegianSupport")


class SurveyModal(discord.ui.Modal):
    """The survey itself: a 1-5 rating and the training-data question.

    Both answers are select menus rather than free text. discord.py 2.6 allows a
    Select inside a Modal only when wrapped in a Label, which is also what
    carries the question wording — the Select's own placeholder is not a label.
    """

    def __init__(self, transcript_id: str, source: typing.Optional[discord.Message]):
        super().__init__(title="Your feedback", timeout=600)
        self.transcript_id = transcript_id
        self.source = source

        self.rating = discord.ui.Select(
            custom_id="nas-survey-rating",
            placeholder="Pick a rating",
            options=[
                discord.SelectOption(label=label, value=value) for value, label in SURVEY_RATING_OPTIONS
            ],
            required=True,
        )
        self.add_item(discord.ui.Label(text=SURVEY_RATING_QUESTION, component=self.rating))

        self.training = discord.ui.Select(
            custom_id="nas-survey-training",
            placeholder="Yes or no",
            options=[
                discord.SelectOption(label="Yes", value="yes"),
                discord.SelectOption(label="No", value="no"),
            ],
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text=SURVEY_TRAINING_QUESTION,
                description=SURVEY_TRAINING_NOTE,
                component=self.training,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _survey_cog(interaction)
        if cog is None:
            return await interaction.response.send_message(
                "Sorry, feedback is unavailable right now.", ephemeral=True
            )

        try:
            rating = int(self.rating.values[0])
        except (IndexError, ValueError):
            logger.error("Survey submitted without a usable rating: %r", self.rating.values)
            return await interaction.response.send_message(
                "Sorry, that did not come through. Please try again.", ephemeral=True
            )

        consented = self.training.values[0] == "yes" if self.training.values else False

        await cog.record_survey(
            interaction,
            transcript_id=self.transcript_id,
            rating=rating,
            consented=consented,
            source=self.source,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("Survey modal failed for %s.", interaction.user, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Sorry, we could not save that. Please try again.", ephemeral=True
            )


class SurveyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nas:survey:(?P<transcript_id>[0-9a-f]{24})",
):
    """The button on the survey invitation.

    A dynamic item rather than a plain button so it keeps working across bot
    restarts: the transcript it belongs to is encoded in the custom_id and
    parsed back out on click, with no view left registered in memory.
    """

    def __init__(self, transcript_id: str):
        self.transcript_id = transcript_id
        super().__init__(
            discord.ui.Button(
                label="Answer the survey",
                style=discord.ButtonStyle.primary,
                emoji="📝",
                custom_id=f"nas:survey:{transcript_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["transcript_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _survey_cog(interaction)
        if cog is None:
            return await interaction.response.send_message(
                "Sorry, feedback is unavailable right now.", ephemeral=True
            )

        if await cog.survey_already_answered(interaction, self.transcript_id):
            return await interaction.response.send_message(
                "Thanks — you have already answered this one.", ephemeral=True
            )

        await interaction.response.send_modal(SurveyModal(self.transcript_id, interaction.message))


def survey_view(transcript_id: str) -> discord.ui.View:
    """A view carrying just the survey button for one transcript."""
    view = discord.ui.View(timeout=None)
    view.add_item(SurveyButton(transcript_id))
    return view


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
        # Registered on the client, not on a view, so a survey button clicked
        # after a restart still resolves to a handler.
        self.bot.add_dynamic_items(SurveyButton)
        self.bot.loop.create_task(self._ensure_indexes())
        self._sweep_idle_sessions.start()

    async def cog_unload(self) -> None:
        self._remove_dm_hook()
        self._sweep_idle_sessions.cancel()
        try:
            self.bot.remove_dynamic_items(SurveyButton)
        except Exception:
            logger.debug("Could not unregister the survey button.", exc_info=True)

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
            # Drives the idle sweeper's scan for conversations to disconnect.
            await self.db.create_index([("_type", 1), ("last_activity_at", 1)])
            await self.db.create_index([("_type", 1), ("transcript_id", 1)])
            # TTL for stage-4 transcripts and live session pointers. Documents
            # lacking `expires_at` — consents, ticket mappings, surveys and the
            # consented training set — are never touched by the TTL monitor.
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
            name="Helping us improve",
            value=(
                "After a conversation with our automated assistant ends, we may ask "
                "whether we can keep it to improve the assistant. That is optional "
                "and we only keep it if you say yes — in which case it is kept "
                "**beyond the 7 days above**, without your name or Discord ID "
                "attached. Saying no keeps the normal deletion above."
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

    @staticmethod
    def _farewell_match(text: str) -> typing.Optional[str]:
        """A sign-off, or None. Only short messages are eligible."""
        if len(text) > FAREWELL_MAX_CHARS:
            return None
        for pattern in _FAREWELL_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    def _open_transcript_filter(self, user_hash: str) -> dict:
        """Matches the one transcript a user may currently be adding to.

        Both `handed_off_at` and `closed_at` being null is what makes a
        transcript live. A disconnect sets `closed_at`, so the user's next
        message starts a fresh document — which is the promise the disconnect
        message makes them.
        """
        return {
            "_type": TYPE_TRANSCRIPT,
            "user_id_hash": user_hash,
            "handed_off_at": None,
            "closed_at": None,
        }

    async def _open_transcript(self, user_id: int) -> typing.Optional[dict]:
        """The user's in-progress pre-screen, if one is still live."""
        return await self.db.find_one(self._open_transcript_filter(await self._user_hash(user_id)))

    async def _append_transcript(
        self,
        user_id: int,
        user_text: str,
        assistant_text: typing.Optional[str] = None,
        resolved: typing.Optional[bool] = None,
    ) -> typing.Optional[dict]:
        """Record one exchange, returning the transcript it landed in."""
        now = datetime.now(timezone.utc)
        entries = [{"role": "user", "content": user_text, "at": now}]
        if assistant_text is not None:
            entries.append({"role": "assistant", "content": assistant_text, "at": now})

        return await self.db.find_one_and_update(
            self._open_transcript_filter(await self._user_hash(user_id)),
            {
                "$push": {"messages": {"$each": entries}},
                # `last_activity_at` is what the idle sweeper measures against.
                "$set": {"resolved": resolved, "last_activity_at": now},
                "$setOnInsert": {
                    "created_at": now,
                    # TTL index on this field expires the transcript 7 days
                    # after it started. Only transcripts carry it.
                    "expires_at": now + timedelta(days=TRANSCRIPT_RETENTION_DAYS),
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def _touch_session(self, user_id: int, transcript_id: ObjectId) -> None:
        """Keep the user ID needed to deliver a disconnect message to this chat.

        Deliberately a separate document: the transcript stays hash-only, so the
        copy that may end up in the training set never carries an identifier,
        and this pointer is deleted as soon as the disconnect is delivered.
        """
        now = datetime.now(timezone.utc)
        await self.db.update_one(
            {"_type": TYPE_SESSION, "transcript_id": transcript_id},
            {
                "$set": {
                    "user_id": user_id,
                    "last_activity_at": now,
                    "expires_at": now + timedelta(hours=SESSION_MAX_AGE_HOURS),
                },
                "$setOnInsert": {"created_at": now},
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

        transcript = await self._append_transcript(user.id, content, reply, resolved)

        if not resolved:
            logger.info("AI deferred for %s (%s); handing off.", user, user.id)
            return False

        try:
            await message.channel.send(embed=self._ai_embed(reply))
        except discord.HTTPException:
            logger.error("Failed delivering AI reply to %s; handing off.", user, exc_info=True)
            return False

        logger.info("AI resolved the request from %s (%s).", user, user.id)

        if transcript is not None:
            # Only now, with the reply delivered, is the conversation worth
            # tracking for a disconnect.
            await self._touch_session(user.id, transcript["_id"])

            farewell = self._farewell_match(content)
            if farewell is not None:
                logger.info("Sign-off %r from %s (%s); ending the conversation.", farewell, user, user.id)
                await self._end_session(transcript["_id"], user=user, channel=message.channel)

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
    # Ending an AI conversation
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message) -> None:
        """A human has the conversation now; the assistant is done with it.

        Marking the transcript handed off takes it out of the sweeper's reach,
        so a ticket a human agent later closes never produces a disconnect
        message or a survey. That path is Modmail's own, and stays that way.
        """
        recipient = getattr(thread, "recipient", None)
        if recipient is None:
            return

        try:
            handed_off = await self.db.find_one_and_update(
                self._open_transcript_filter(await self._user_hash(recipient.id)),
                {"$set": {"handed_off_at": datetime.now(timezone.utc)}},
            )
            if handed_off is not None:
                await self.db.delete_many({"_type": TYPE_SESSION, "transcript_id": handed_off["_id"]})
        except Exception:
            logger.error("Could not mark the transcript for %s handed off.", recipient, exc_info=True)

    @tasks.loop(seconds=SESSION_SWEEP_SECONDS)
    async def _sweep_idle_sessions(self) -> None:
        """Disconnect AI conversations that have gone quiet.

        Only transcripts whose last exchange the assistant resolved itself are
        eligible: a transcript left unresolved was on its way to a human, and
        telling that user they have been disconnected would be wrong.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=SESSION_IDLE_MINUTES)

        try:
            stale = (
                await self.db.find(
                    {
                        "_type": TYPE_TRANSCRIPT,
                        "handed_off_at": None,
                        "closed_at": None,
                        "resolved": True,
                        # `$lt` also excludes transcripts written before this field
                        # existed, which is the safe direction: they simply expire.
                        "last_activity_at": {"$lt": cutoff},
                    },
                    # Only `_id` is needed; `_end_session` re-reads under its own
                    # atomic claim anyway, and transcripts can be large.
                    {"_id": 1},
                )
                .limit(SESSION_SWEEP_BATCH)
                .to_list(length=SESSION_SWEEP_BATCH)
            )
        except Exception:
            logger.error("Idle session sweep could not read transcripts.", exc_info=True)
            return

        for transcript in stale:
            try:
                await self._end_session(transcript["_id"])
            except Exception:
                logger.error("Failed ending idle session %s.", transcript.get("_id"), exc_info=True)

    @_sweep_idle_sessions.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_for_connected()
        await self._ready.wait()

    async def _end_session(
        self,
        transcript_id: ObjectId,
        *,
        user: typing.Optional[discord.abc.User] = None,
        channel: typing.Optional[discord.abc.Messageable] = None,
    ) -> None:
        """Close one AI conversation and offer the survey.

        Claiming the transcript first — a single atomic update that sets
        `closed_at` — is what stops the sweeper and a sign-off from both
        disconnecting the same conversation.
        """
        transcript = await self.db.find_one_and_update(
            {"_type": TYPE_TRANSCRIPT, "_id": transcript_id, "closed_at": None, "handed_off_at": None},
            {"$set": {"closed_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        if transcript is None:
            return

        session = await self.db.find_one({"_type": TYPE_SESSION, "transcript_id": transcript_id})

        if user is None and session is not None:
            user = self.bot.get_user(session["user_id"])
            if user is None:
                try:
                    user = await self.bot.fetch_user(session["user_id"])
                except discord.HTTPException:
                    logger.warning("Could not resolve user %s to disconnect.", session["user_id"])

        # The pointer has done its job. Dropping it here keeps the identifier's
        # lifetime as short as the delivery it existed for, whether or not the
        # send below succeeds.
        await self.db.delete_many({"_type": TYPE_SESSION, "transcript_id": transcript_id})

        if user is None:
            return

        # A thread opened in the gap between the sweep and now means a human has
        # them; say nothing rather than talk over the ticket.
        try:
            if await self.bot.threads.find(recipient=user) is not None:
                return
        except Exception:
            logger.debug("Could not check for an open thread before disconnecting.", exc_info=True)

        if channel is None:
            channel = user.dm_channel
            if channel is None:
                try:
                    channel = await user.create_dm()
                except discord.HTTPException:
                    logger.warning("Could not open a DM with %s to disconnect.", user)
                    return

        try:
            await channel.send(embed=self._disconnect_embed())
            await channel.send(
                embed=self._survey_invite_embed(),
                view=survey_view(str(transcript_id)),
            )
        except discord.HTTPException:
            # DMs closed, or the user blocked the bot. The conversation is still
            # correctly closed; there is simply nobody to tell.
            logger.info("Could not deliver the disconnect message to %s.", user, exc_info=True)
            return

        logger.info("Ended AI conversation %s for %s (%s).", transcript_id, user, user.id)

    def _disconnect_embed(self) -> discord.Embed:
        return self._embed(
            description=(
                f"You have been disconnected from {ASSISTANT_NAME}. Whenever you need "
                "us again, just send a message here and a new conversation will start."
            ),
            color=self.bot.mod_color,
        )

    def _survey_invite_embed(self) -> discord.Embed:
        return self._embed(
            description=(
                "We would like to hear your feedback! Click the button below to "
                "answer a quick 1 minute survey."
            ),
        )

    # ------------------------------------------------------------------
    # Survey results
    # ------------------------------------------------------------------

    @staticmethod
    def _survey_filter(transcript_id: ObjectId, user_hash: str) -> dict:
        """One answer per user per conversation.

        Keyed on the submitter as well as the conversation: a custom_id arrives
        from the client, so keying on the transcript alone would let one user's
        submission overwrite the answer stored against another user's.
        """
        return {"_type": TYPE_SURVEY, "transcript_id": transcript_id, "user_id_hash": user_hash}

    async def survey_already_answered(self, interaction: discord.Interaction, transcript_id: str) -> bool:
        try:
            answered = await self.db.find_one(
                self._survey_filter(ObjectId(transcript_id), await self._user_hash(interaction.user.id)),
                {"_id": 1},
            )
        except Exception:
            # Never block someone from answering because of a lookup failure;
            # a duplicate is caught again on write.
            logger.error("Could not check for an existing survey answer.", exc_info=True)
            return False
        return answered is not None

    async def record_survey(
        self,
        interaction: discord.Interaction,
        *,
        transcript_id: str,
        rating: int,
        consented: bool,
        source: typing.Optional[discord.Message],
    ) -> None:
        """Store the survey answers and, on a yes, the training copy."""
        user_hash = await self._user_hash(interaction.user.id)
        now = datetime.now(timezone.utc)

        try:
            oid = ObjectId(transcript_id)
        except Exception:
            logger.error("Survey submitted with an unusable transcript id %r.", transcript_id)
            return await interaction.response.send_message("Sorry, we could not save that.", ephemeral=True)

        transcript = await self.db.find_one({"_type": TYPE_TRANSCRIPT, "_id": oid})

        # The button's custom_id is client-supplied, so a transcript is only
        # usable once it is shown to belong to whoever pressed it. Someone
        # else's conversation is refused outright rather than downgraded to a
        # bare rating, which would let them write over the real answer.
        if transcript is not None and transcript.get("user_id_hash") != user_hash:
            logger.warning(
                "%s (%s) submitted a survey for a conversation they do not own.",
                interaction.user,
                interaction.user.id,
            )
            return await interaction.response.send_message(
                "Sorry, that survey is not yours to answer.", ephemeral=True
            )

        stored_training = False
        if consented and transcript is not None:
            await self.db.update_one(
                {"_type": TYPE_TRAINING, "transcript_id": oid, "user_id_hash": user_hash},
                {
                    "$set": {
                        "user_id_hash": user_hash,
                        "messages": transcript.get("messages", []),
                        "message_count": len(transcript.get("messages", [])),
                        "rating": rating,
                        "conversation_started_at": transcript.get("created_at"),
                        "conversation_ended_at": transcript.get("closed_at"),
                        "consented_at": now,
                    }
                },
                # No `expires_at`: this copy is review material for improving
                # the assistant, not a live conversation, so the 7-day TTL that
                # governs the transcript deliberately does not apply.
                upsert=True,
            )
            stored_training = True

        stored_rating = consented or KEEP_RATINGS_WITHOUT_CONSENT
        if stored_rating:
            await self.db.update_one(
                self._survey_filter(oid, user_hash),
                {
                    "$set": {
                        "user_id_hash": user_hash,
                        "rating": rating,
                        "training_consent": consented,
                        "answered_at": now,
                    }
                },
                upsert=True,
            )

        if source is not None:
            try:
                await source.edit(view=None)
            except discord.HTTPException:
                logger.debug("Could not clear the survey button.", exc_info=True)

        # Each branch says only what actually happened; a blanket "recorded" is
        # not true when the conversation had already expired, and not true at
        # all when KEEP_RATINGS_WITHOUT_CONSENT discarded the rating.
        if stored_training:
            thanks = (
                "Thank you, your feedback has been recorded, and this chat will help "
                "us improve our assistant."
            )
        elif consented and stored_rating:
            thanks = (
                "Thank you, your rating has been recorded. This conversation is no "
                "longer available, so nothing from it has been kept."
            )
        elif consented:
            thanks = "Thank you. This conversation is no longer available, so nothing has been kept."
        elif stored_rating:
            thanks = "Thank you, your feedback has been recorded."
        else:
            thanks = "Thank you for your feedback."

        await interaction.response.send_message(thanks, ephemeral=True)
        logger.info(
            "Survey answered for %s: rating %s, training consent %s.",
            transcript_id,
            rating,
            consented,
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
                "live sessions": await self.db.count_documents({"_type": TYPE_SESSION}),
                "surveys": await self.db.count_documents({"_type": TYPE_SURVEY}),
                "training set": await self.db.count_documents({"_type": TYPE_TRAINING}),
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

        embed.add_field(
            name="Disconnect sweeper",
            value=(
                f"running — idle after `{SESSION_IDLE_MINUTES}m`, checked every "
                f"`{SESSION_SWEEP_SECONDS}s`"
                if self._sweep_idle_sessions.is_running()
                else "**stopped** — no conversation will be disconnected or surveyed"
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

        # Withdrawal has to take the pre-screen conversations with it, otherwise
        # data collected under that consent outlives it. The training set is
        # included: it is kept under the same acceptance, so withdrawing that
        # acceptance has to reach it, which is why the training copy keeps the
        # hashed user ID rather than nothing at all.
        user_hash = await self._user_hash(user.id)
        transcripts = await self.db.delete_many({"_type": TYPE_TRANSCRIPT, "user_id_hash": user_hash})
        training = await self.db.delete_many({"_type": TYPE_TRAINING, "user_id_hash": user_hash})
        surveys = await self.db.delete_many({"_type": TYPE_SURVEY, "user_id_hash": user_hash})
        await self.db.delete_many({"_type": TYPE_SESSION, "user_id": user.id})

        removed = transcripts.deleted_count + training.deleted_count + surveys.deleted_count
        if not deleted and not removed:
            return await ctx.send(
                embed=self._embed(
                    description=f"Nothing on record for {user.mention}.",
                    color=self.bot.error_color,
                )
            )

        await ctx.send(
            embed=self._embed(
                description=(
                    f"Consent withdrawn for {user.mention}. Deleted "
                    f"{transcripts.deleted_count} assistant conversation(s), "
                    f"{training.deleted_count} training copy/copies and "
                    f"{surveys.deleted_count} survey answer(s). They will see the "
                    "privacy notice again the next time they open a ticket.\n\n"
                    "Ticket transcripts with a human agent are Modmail's own logs "
                    "and are not touched by this; use `?logs` to review them."
                ),
            )
        )
        logger.info("Consent withdrawn for %s (%s) by %s.", user, user.id, ctx.author)

    @nas.command(name="feedback")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def nas_feedback(self, ctx):
        """Summarise the post-disconnect survey answers."""
        answers = await self.db.find({"_type": TYPE_SURVEY}, {"rating": 1, "training_consent": 1}).to_list(
            length=None
        )

        if not answers:
            return await ctx.send(
                embed=self._embed(description="No survey answers yet.", color=self.bot.error_color)
            )

        ratings = [a["rating"] for a in answers if isinstance(a.get("rating"), int)]
        consented = sum(1 for a in answers if a.get("training_consent"))

        embed = self._embed(
            title="Survey answers",
            description=f"{len(answers)} answered.",
        )
        if ratings:
            distribution = "\n".join(
                f"`{score}` {'█' * ratings.count(score)} {ratings.count(score)}" for score in range(5, 0, -1)
            )
            embed.add_field(
                name=f"Average {sum(ratings) / len(ratings):.2f} / 5",
                value=distribution,
                inline=False,
            )
        embed.add_field(
            name="Training consent",
            value=f"{consented} of {len(answers)} said yes ({consented / len(answers):.0%}).",
            inline=False,
        )
        await ctx.send(embed=embed)

    @nas.command(name="training")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def nas_training(self, ctx, samples: int = TRAINING_SAMPLE_DEFAULT):
        """Show the consented training set: how much there is, and a few samples.

        This is a reading list, not a pipeline. Nothing here feeds back into the
        assistant on its own — the FAQ and system prompt are edited by hand, the
        same way gaps found in day-to-day tickets are.
        """
        samples = max(0, min(samples, TRAINING_SAMPLE_MAX))

        total = await self.db.count_documents({"_type": TYPE_TRAINING})
        if not total:
            return await ctx.send(
                embed=self._embed(
                    description=(
                        "No consented conversations yet. They appear here when a user "
                        "answers yes to the training question in the post-chat survey."
                    ),
                    color=self.bot.error_color,
                )
            )

        recent = (
            await self.db.find({"_type": TYPE_TRAINING})
            .sort("consented_at", -1)
            .limit(samples)
            .to_list(length=samples)
        )

        embed = self._embed(
            title="Consented training set",
            description=(
                f"**{total}** conversation(s) kept for review, showing the " f"{len(recent)} most recent."
            ),
            footer="Anonymous — no user IDs or names are stored with these",
        )

        # A field caps at 1024 characters but the whole embed caps at 6000, so
        # the full ten samples do not necessarily fit. Stop early rather than
        # have Discord reject the message outright.
        shown = 0
        for doc in recent:
            name = self._training_sample_name(doc)
            body = self._training_sample_body(doc)
            if len(embed) + len(name) + len(body) > 5900:
                break
            embed.add_field(name=name, value=body, inline=False)
            shown += 1

        if shown < len(recent):
            embed.description = (
                f"**{total}** conversation(s) kept for review, showing {shown} — the rest "
                f"did not fit. Ask for fewer at a time, or read them in the database."
            )

        await ctx.send(embed=embed)

    @staticmethod
    def _training_sample_name(doc: dict) -> str:
        rating = doc.get("rating")
        started = doc.get("conversation_started_at")
        when = started.strftime("%Y-%m-%d") if isinstance(started, datetime) else "unknown date"
        return f"{when} • rated {rating}/5 • {doc.get('message_count', 0)} messages"

    @staticmethod
    def _training_sample_body(doc: dict) -> str:
        """Render a transcript into one embed field, inside the 1024-char cap."""
        lines = []
        for entry in doc.get("messages", []):
            speaker = "**User**" if entry.get("role") == "user" else "**AI**"
            content = " ".join((entry.get("content") or "").split())
            if len(content) > 160:
                content = content[:157] + "…"
            line = f"{speaker}: {content}"
            # 1024 is Discord's field-value cap; leave room for the truncation
            # marker rather than having the whole embed rejected.
            if sum(len(x) + 1 for x in lines) + len(line) > 970:
                lines.append("…")
                break
            lines.append(line)
        return "\n".join(lines) or "*empty*"

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
