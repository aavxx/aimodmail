"""
Norwegian Air Shuttle (Roblox) support plugin for Modmail.

A new conversation opens with a fixed data-processing disclosure, then is
pre-screened by Groq.
Questions the assistant can answer from the FAQ below are answered without a
thread ever being created; everything else falls through to Modmail, which
creates the thread exactly as it always did. Every failure path ends in that
fallthrough, so a user is never left without a response.

The handoff summary and ticket reference (stage 5) slot into `_gate` at the
marked seam.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import typing
from datetime import datetime, timedelta, timezone

import discord
import isodate
from discord.ext import commands, tasks
from pymongo import ReturnDocument

from core import checks
from core.models import DMDisabled, PermissionLevel, getLogger
from core.time import human_timedelta
from core.utils import AcceptButton, DenyButton, safe_typing, truncate

try:
    from groq import AsyncGroq
except ImportError:  # pragma: no cover - dependency install failed
    AsyncGroq = None

logger = getLogger(__name__)

# Shown at the start of every conversation, before the greeting. Informational
# only: processing is on the basis of the contractual relationship, not consent,
# so there is nothing to accept and nothing stored per user. Data rights are
# exercised through the linked privacy policy, not in chat.
DISCLOSURE_PARTS = (
    "Vueling will process your data to provide you with the services you have "
    "requested and improve your experience with Vueling, based on the execution "
    "of a contractual relationship. For more information, see our "
    "[privacy policy](https://vuelingrbx.vercel.app/privacy).",
    "This chatbot uses an Artificial Intelligence tool to identify the most "
    "relevant answers to frequently asked questions.",
)

# Human-facing ticket reference prefix, mapped to Modmail's own log key.
TICKET_PREFIX = "VLG"

# Reference body alphabet. No O/0 or I/1, so a code read aloud or retyped from
# memory cannot land on the wrong ticket.
TICKET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TICKET_BODY_LENGTH = 6

# Asked of Groq when handing over, so staff open the thread already knowing what
# it is about. A failure here must not block the handoff, so it falls back.
SUMMARY_PROMPT = """\
Summarise this support conversation for the staff member about to take it over.
Two or three sentences, plain and factual: what the user is asking for, anything
they have already been told, and what still needs doing. Address the staff
member, not the user. Do not invent detail that is not in the conversation.
"""

SUMMARY_FALLBACK = (
    "This conversation could not be resolved automatically and was transferred. "
    "No automatic summary is available, so please read the messages above."
)

# Sent to the user once their thread exists. {reference} is substituted.
HANDOFF_REFERENCE_TEXT = (
    "You're now connected to our support team, and someone will be with you as "
    "soon as they can.\n\nYour ticket reference is **{reference}** — quote it if "
    "you need to follow this up later."
)

# One loop does both jobs. The retention sweep only needs to be occasional, but
# the inactivity checks below need finer resolution than a 1 hour warning, and an
# indexed delete_many running 5-minutely costs nothing, so this stays a single
# timer rather than two.
SWEEP_INTERVAL_MINUTES = 5

# Inactivity in the pre-screen conversation only. Modmail's own thread_auto_close
# governs open tickets and is untouched by any of this.
INACTIVITY_WARNING_AFTER = timedelta(hours=1)
INACTIVITY_CLOSE_AFTER = timedelta(hours=3)

INACTIVITY_WARNING_TEXT = (
    "Are you still there? I'll close this chat shortly if you don't need anything "
    "else — just send a message and I'll keep helping. \N{SMILING FACE WITH SMILING EYES}"
)

# Three separate messages when a conversation ends, however it ended.
CLOSING_PARTS = (
    "Thanks for chatting with me today, I hope I was able to help! " "\N{SMILING FACE WITH SMILING EYES}",
    "Bye for now! \N{SMILING FACE WITH SMILING EYES}",
    "You have been disconnected from Vueling AI. Whenever you need us again, just "
    "send a message here and a new conversation will start.",
)

# Phrases that end the conversation, checked only once it is already open so a
# conversation cannot be closed by its own first message. Word-boundary anchored.
CLOSING_PATTERNS = [
    r"^\s*(?:no|nope|nah|no\s+thanks?|no\s+thank\s+you)\s*[.!]*\s*$",
    r"\bthat'?s\s+(?:all|it|everything)\b",
    r"\bnothing\s+(?:else|more)\b",
    r"\b(?:close|end|finish|stop)\s+(?:the\s+|this\s+)?(?:chat|conversation|ticket)\b",
    r"\b(?:i'?m|im|we'?re)\s+(?:all\s+)?done\b",
    r"^\s*(?:bye|goodbye|byebye|cya|see\s+you)\b",
]

_CLOSING_RE = [re.compile(p, re.IGNORECASE) for p in CLOSING_PATTERNS]

# Retention for AI pre-screen transcripts. Enforced by a MongoDB TTL index on
# `expires_at`; documents without that field (sessions, ticket mappings) are
# ignored by the TTL monitor and kept until something else removes them.
TRANSCRIPT_RETENTION_DAYS = 7

# Discriminators. get_plugin_partition() hands back a single collection
# (plugins.NorwegianSupport), so the logical collections share it.
TYPE_TRANSCRIPT = "ai_transcript"
TYPE_TICKET = "ticket"

TYPE_META = "meta"

# Tracks an open pre-screen conversation so the inactivity sweep can reach the
# user. Holds the raw user id, because a transcript is keyed by a one-way hash
# and you cannot DM a hash. It carries no message content, and is deleted the
# moment the conversation closes, so the identifiable part is scoped to
# conversations that are actually open.
TYPE_SESSION = "session"

# No longer written. Documents from the removed consent gate may still exist;
# `.vlg forget` clears them alongside a user's transcripts.
TYPE_LEGACY_CONSENT = "consent"

GROQ_MODEL = "llama-3.3-70b-versatile"

# The gate runs inside Modmail's per-user DM queue, so a hung request would stall
# that user's messages. Bounded twice: once by the client, once by wait_for.
GROQ_TIMEOUT_SECONDS = 20

# Turns of prior context sent back to Groq. Caps token spend on long chats.
AI_HISTORY_LIMIT = 12

# Assistant identity. The author row goes on every embed the plugin builds; the
# footer caveat does not, because it is a statement about model output and would
# misattribute fixed plugin copy.
AI_TITLE = "Vueling AI"
AI_FOOTER = "Vueling AI can make mistakes. Please double check responses."

# Handoff confirmation buttons: guild emoji, no label.
BUTTON_YES_EMOJI = "<:yes:1533908794684473354>"
BUTTON_NO_EMOJI = "<:no:1533908791245017198>"

# The model picks exactly one of these per reply. A single enum rather than a
# pair of booleans, so it cannot express a contradiction like understood-but-not
# understood, and so "I did not follow you" stops being the same outcome as
# "I followed you and cannot help".
STATUS_ANSWERED = "answered"
STATUS_CHAT = "chat"
STATUS_UNCLEAR = "unclear"
STATUS_ESCALATE = "escalate"
STATUSES = {STATUS_ANSWERED, STATUS_CHAT, STATUS_UNCLEAR, STATUS_ESCALATE}

# Statuses where the assistant handled the turn itself.
STATUS_HANDLED = {STATUS_ANSWERED, STATUS_CHAT}

# Asking someone to rephrase forever is its own dead end, so after this many
# consecutive unclear turns the conversation offers a human instead.
MAX_CONSECUTIVE_UNCLEAR = 2

# Why a conversation reached a human. Only AI_DEFERRED means the assistant
# understood the question and could not answer it, which is the number worth
# watching; the others are the user's choice or an outage.
HANDOFF_AI_DEFERRED = "ai_deferred"
HANDOFF_ASKED_FOR_HUMAN = "asked_for_human"
HANDOFF_AI_UNAVAILABLE = "ai_unavailable"
HANDOFF_AI_ERROR = "ai_error"
HANDOFF_AI_UNCLEAR = "ai_unclear"

HANDOFF_REASON_LABELS = {
    HANDOFF_AI_DEFERRED: "assistant could not answer",
    HANDOFF_ASKED_FOR_HUMAN: "user asked for a human",
    HANDOFF_AI_UNAVAILABLE: "assistant not configured",
    HANDOFF_AI_ERROR: "Groq call failed",
    HANDOFF_AI_UNCLEAR: "could not understand after retrying",
}

# Stripped before ranking terms in deferred questions. Topic words are what
# tell you which FAQ entry to write; these never do.
STATS_STOPWORDS = {
    "a",
    "about",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "get",
    "got",
    "had",
    "has",
    "have",
    "help",
    "hi",
    "hello",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "just",
    "know",
    "like",
    "me",
    "my",
    "need",
    "not",
    "of",
    "on",
    "one",
    "or",
    "please",
    "so",
    "some",
    "thanks",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "to",
    "up",
    "want",
    "was",
    "we",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
}

# Used if the guild emoji above cannot be sent. Discord rejects a component
# carrying an emoji the bot has no access to, which fails the whole message, so
# without a fallback a bad emoji id means no confirmation prompt at all.
BUTTON_YES_FALLBACK = "\N{WHITE HEAVY CHECK MARK}"
BUTTON_NO_FALLBACK = "\N{CROSS MARK}"

# Author-row icon. None means "use the bot's own avatar", which follows the
# Developer Portal without a redeploy and is the normal case.
#
# Set this to a direct image URL only if the embed icon needs to differ from the
# bot's avatar. Note the Portal has two separate images: the App Icon on General
# Information, and the Bot avatar on the Bot tab. Only the Bot avatar reaches
# `bot.user.display_avatar`, so setting the App Icon alone leaves the embed icon
# on Discord's default grey.
AI_ICON_URL: typing.Optional[str] = None

# How long the typing indicator runs before each message. Every send in the
# pre-screen pauses for this, so a greeted question costs roughly three of these
# plus the Groq round trip before the user sees an answer.
TYPING_DELAY_SECONDS = 2.5

# Sent once when a user opens a new pre-screen conversation, ahead of their first
# message being processed. Two separate messages, each with its own typing pause.
GREETING_PARTS = (
    "Hola! I'm Vueling's virtual assistant. I'm new and still learning but "
    "there are a lot of things I can do for you.",
    "How can I help you? Please, try to be as brief as possible so I can "
    "understand you \N{SMILING FACE WITH SMILING EYES}",
)

# Asked when the user opens with nothing to act on, and the greeting has already
# been sent earlier in the conversation.
CONTENTLESS_PROMPT = "What can I help you with today? \N{SMILING FACE WITH SMILING EYES}"

# Words that carry no request on their own. A message made up entirely of these
# is a hello, not a question, and must not be escalated as unanswerable.
GREETING_WORDS = {
    "hi",
    "hii",
    "hiii",
    "hiya",
    "hello",
    "helo",
    "hey",
    "heyy",
    "heya",
    "hai",
    "hola",
    "yo",
    "sup",
    "howdy",
    "greetings",
    "good",
    "morning",
    "afternoon",
    "evening",
    "day",
    "there",
    "everyone",
    "all",
    "team",
    "please",
    "thanks",
    "hallo",
    "salut",
    "ola",
}

# Handoff confirmation. Fixed copy: this is the plugin speaking, not the model.
# Deliberately short. The model's own reply is sent first and carries the
# explanation, so repeating "I can't help" here would be the second time the
# user is told that in two messages.
HANDOFF_CONFIRM_TEXT = "Would you like me to connect you with a member of our team?"
HANDOFF_ACCEPTED_TEXT = "Okay, I will get you right over to someone to help you further."
HANDOFF_GOODBYE_TEXT = "Bye for now! \N{SMILING FACE WITH SMILING EYES}"
HANDOFF_DECLINED_TEXT = (
    "No problem at all! Is there anything else I can help you with? " "\N{SMILING FACE WITH SMILING EYES}"
)

# Unanswered confirmations escalate. The user either asked for a human outright
# or hit something the assistant could not resolve, so silence after "shall I
# connect you?" is worse than a ticket nobody follows up.
HANDOFF_CONFIRM_TIMEOUT_SECONDS = 120

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
- Fly Grande is priority boarding, purchased in-game for 15 Robux.
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
You are the first-line automated support assistant for Vueling, a virtual
airline group on Roblox. You answer straightforward questions from passengers
and staff.

Every fact you state must come from the reference information below. Never
invent flight times, prices, rank names, policies or links.

That is a rule about facts, not about phrasing. You are expected to reword the
reference, and to combine two or three points from different parts of it, to
answer naturally. The reference will not contain a ready-made answer to every
question, and it does not need to: if the facts you need are in there, you can
answer. Handing over to a human is for when a fact is genuinely missing, not for
when the wording does not line up.

Work out what they are referring to, not whether they said it the reference's
way. People use shorthand, abbreviations, partial names, plurals, synonyms and
typos, and all of those still point at the same entry:

- "grande", "fly grande", "priority", "priority boarding" → Fly Grande
- "alts", "alt account", "second account", "two accounts" → alt accounts
- "jobs", "hiring", "applications", "vacancies", "recruitment", "apply" → open positions
- "unban", "appeal", "I got banned" → the appeals page
- "outfit", "dress code", "what do I wear", "kit" → the uniform policy
- "timetable", "schedule", "next one", "departure times" → the flight pattern and departures page
- "money back", "refund", "can I get my robux back" → the refunds policy
- "how old do you have to be", "age limit", "am I old enough" → the minimum age
- "the group", "joining", "do I need to be a member" → the group membership requirement

Recognising the term and possessing the fact are separate things, and matching
someone's wording never licenses an answer the reference does not contain:

- "what are the levels called?" is recognisably about ranks. You should
  understand it, and the reference still does not list the rank names, so it is
  escalate anyway.
- If a subject appears nowhere in the reference under any wording, that is also
  escalate. Do not stretch a neighbouring entry to cover it, and do not
  assume two things are the same because they sound similar.

Give every reply exactly one status.

"answered" — you answered it from the reference. Rewording it, or combining two
  or three entries, still counts. Being brief is fine. Stating a policy the user
  will not like is a complete answer, not a failure.

"chat" — the message needs no airline fact at all: thanks, a greeting partway
  through, small talk, someone saying they are annoyed or that you helped. Reply
  like a person would and stay in the conversation. Never put airline facts in a
  "chat" reply; if one is needed, the status is not "chat".

"unclear" — you genuinely cannot tell what is being asked. A typo, a fragment, a
  garbled sentence, or something with two very different readings. Ask them to
  put it another way, and say which part you are unsure about if you can. Do NOT
  use this when you understood the question perfectly well and simply do not
  have the fact — that is "escalate".

"escalate" — you understood, and doing it properly needs a person: a subject the
  reference does not cover at all, a decision about one individual's account,
  punishment, appeal or application, a report of a specific purchase going
  wrong, or a user who is upset or has already asked twice without being helped.

Try before you hand over. If any part of the question is covered, answer that
part first and then say plainly what you cannot do yourself. If a small
clarification would let you answer, ask for it instead of escalating. A reply
that gives someone something and then offers a human is far better than one that
only offers a human.

Your reply is always shown to the user, including when you escalate — they read
your words before they are offered an agent. Never write a reply that is only
"I can't help with that"; say what you do know, or what you would need.

"I would have to make a fact up" is the test for escalating. Being unsure how to
word something is not.

Worked examples:
- "how do I join a flight?" — answered, combining the group membership
  requirement with the departures page and the timing pattern.
- "what time is the next flight?" — answered, giving the XX:00 / XX:20 / XX:25 /
  XX:35 pattern and the departures link, without naming an hour.
- "can I get a refund?" — answered, stating the no-refunds policy plainly.
- "thanks, that helped!" — chat.
- "hlo wut abt teh thing" — unclear, ask which thing they mean.
- "what are the ranks called?" — escalate, but say first that the age
  requirement is 13 and where applications are, since that much is covered.
- "why was my application rejected?" — escalate, a decision about one person.

Language: reply in whatever language the user wrote in.

Links: when the reference information provides a link, reproduce it exactly as
written, in the same [display.domain](https://full.url) markdown form. Never
show a bare URL, never alter the display text or the target, and never invent a
link that is not in the reference information.

Tone: warm and kind, like a helpful person who is glad to see them. Keep it
plain and easy to read, under 900 characters. A little emoji is welcome, general
friendly ones such as \N{SMILING FACE WITH SMILING EYES}. Never use aircraft,
travel, luggage or destination emoji. Do not claim to be human.

Watch for answers that land bluntly. A short reply, a flat "no", a restriction,
or anything that tells the user what they cannot have reads as cold on its own,
however accurate it is. In those cases use the two-message form: the answer
first, then a brief warm follow-up offering more help. For example, asked about
uniform:

  ["The uniform policy is staff-only information. \N{SMILING FACE WITH SMILING EYES}",
   "Is there anything else I can help you with? \N{SMILING FACE WITH SMILING EYES}"]

The same applies to the refund policy and to anything else the user will not
want to hear: state it plainly, without softening the policy itself, then offer
to keep helping. An answer that is already a few warm sentences does not need
the follow-up.

Reference information:
{FAQ_KNOWLEDGE}

Respond with a single json object with exactly these keys:
  "status": one of "answered", "chat", "unclear", "escalate"
  "reply": either a string, or an array of two strings

Use the array form when the answer reads better as two messages: a short or
blunt answer followed by the warm offer of further help described above, or a
direct answer followed by a related pointer. A single string is fine when the
reply is already conversational.
"""


class YesNoView(discord.ui.View):
    """Yes/no view for the handoff confirmation.

    Shaped like Modmail's own ConfirmThreadCreationView, but that class hardcodes
    timeout=30 in __init__ with no parameter to override. The buttons themselves
    are Modmail's.
    """

    def __init__(self, timeout: float):
        super().__init__(timeout=timeout)
        self.value = None


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
        self._verbose = False
        self._loaded_at: typing.Optional[datetime] = None
        # user_id -> summary, handed to on_thread_ready once the channel exists.
        self._pending_handoff: typing.Dict[int, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        self._loaded_at = datetime.now(timezone.utc)
        self._install_dm_hook()
        self.bot.loop.create_task(self._ensure_indexes())
        self.bot.loop.create_task(self._load_verbose())
        self.maintenance_sweep.start()

    async def _load_verbose(self) -> None:
        """Restore the diagnostics toggle.

        Held in the partition rather than memory: a plugin reload builds a new
        cog instance and a restart loses the old one, so an in-memory flag
        silently reverts to off exactly when someone is mid-diagnosis.
        """
        await self.bot.wait_for_connected()
        try:
            doc = await self.db.find_one({"_type": TYPE_META, "key": "verbose"})
        except Exception:
            logger.error("Could not read the verbose flag; defaulting to off.", exc_info=True)
            return
        self._verbose = bool(doc and doc.get("value"))
        if self._verbose:
            logger.info(
                "Verbose diagnostics are ON (restored). Turn off with %svlg verbose off.", self.bot.prefix
            )

    async def cog_unload(self) -> None:
        self._remove_dm_hook()
        self.maintenance_sweep.cancel()

    @tasks.loop(minutes=SWEEP_INTERVAL_MINUTES)
    async def maintenance_sweep(self) -> None:
        """Retention cleanup and pre-screen inactivity, on one timer.

        Retention only needs to be occasional, but the inactivity warning is on
        the hour, so this runs at the finer of the two intervals. Neither job may
        stop the other from running.
        """
        try:
            await self._expire_transcripts()
        except Exception:
            logger.error("Transcript expiry sweep failed.", exc_info=True)
        try:
            await self._sweep_inactive_conversations()
        except Exception:
            logger.error("Inactivity sweep failed.", exc_info=True)

    async def _expire_transcripts(self) -> None:
        """Delete transcripts past their retention window.

        The TTL index on `expires_at` is the primary mechanism. This is a
        backstop: TTL is a background monitor that some deployments disable or
        run behind, and the retention the linked privacy policy promises should
        not depend on a setting nobody here controls.
        """
        result = await self.db.delete_many(
            {"_type": TYPE_TRANSCRIPT, "expires_at": {"$lte": datetime.now(timezone.utc)}}
        )
        if result.deleted_count:
            logger.info("Deleted %s expired AI transcript(s).", result.deleted_count)

    async def _sweep_inactive_conversations(self) -> None:
        """Warn, then close, pre-screen conversations that have gone quiet.

        Only the plugin's own pre-screen stage. An open Modmail thread is past
        this point entirely and is governed by Modmail's `thread_auto_close`.
        """
        now = datetime.now(timezone.utc)

        # Closing is handled before warning, so a conversation idle past both
        # thresholds closes rather than being warned and closed a tick later.
        stale = await self.db.find(
            {"_type": TYPE_SESSION, "last_activity_at": {"$lte": now - INACTIVITY_CLOSE_AFTER}}
        ).to_list(length=200)
        for session in stale:
            user_id = session["user_id"]
            channel = await self._dm_channel(user_id)
            if channel is None:
                # Cannot reach them, but the conversation must still end or it
                # would be swept again on every tick forever.
                await self._close_transcript(user_id, handed_off=False)
                continue
            await self._close_conversation(user_id, channel, reason="inactivity")

        quiet = await self.db.find(
            {
                "_type": TYPE_SESSION,
                "warned_at": None,
                "last_activity_at": {"$lte": now - INACTIVITY_WARNING_AFTER},
            }
        ).to_list(length=200)
        for session in quiet:
            user_id = session["user_id"]
            # Stamped before sending, so a send that fails does not re-warn on
            # every tick for the next two hours.
            await self.db.update_one(
                {"_type": TYPE_SESSION, "user_id": user_id},
                {"$set": {"warned_at": now}},
            )
            channel = await self._dm_channel(user_id)
            if channel is None:
                continue
            try:
                await self._send_with_typing(channel, self._plain_embed(INACTIVITY_WARNING_TEXT))
            except discord.HTTPException:
                logger.error("Could not warn %s about inactivity.", user_id, exc_info=True)
            else:
                logger.info("Warned %s that their conversation will close.", user_id)

    async def _dm_channel(self, user_id: int):
        """The user's DM channel, or None if they cannot be reached."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            return await user.create_dm()
        except Exception:
            logger.error("Could not open a DM channel with %s.", user_id, exc_info=True)
            return None

    @maintenance_sweep.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_for_connected()

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
            await self.db.create_index([("_type", 1), ("last_activity_at", 1)])
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

            # AI pre-screen. True means the user has their answer and no thread
            # is needed. The data-processing disclosure goes out inside this, at
            # the start of a conversation, ahead of the greeting.
            if await self._ai_prescreen(message):
                return

            # Summarise while the transcript is still open, then close it: a
            # human is taking over and the pre-screen conversation is done.
            await self._prepare_handoff(message)
            await self._close_transcript(message.author.id, handed_off=True)

            # Modmail creates the thread from here, exactly as it always did.
            # on_thread_ready posts the summary and issues the reference once
            # the channel exists. Nothing after this point is intercepted.
            return await passthrough(message)

        except Exception:
            logger.error("Norwegian support gate failed; falling back to Modmail.", exc_info=True)
            try:
                return await passthrough(message)
            except Exception:
                logger.error("Fallback to Modmail also failed.", exc_info=True)

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

    async def _send_with_typing(
        self,
        channel,
        embed: discord.Embed,
        view: typing.Optional[discord.ui.View] = None,
    ):
        """Send one message behind a typing indicator.

        safe_typing is Modmail's own helper and already swallows failures from
        the typing endpoint, so a typing outage cannot stop the message going
        out. The pause is what makes consecutive messages read as separate
        turns rather than one wall of text.
        """
        async with safe_typing(channel):
            await asyncio.sleep(TYPING_DELAY_SECONDS)
        return await channel.send(embed=embed, view=view) if view else await channel.send(embed=embed)

    async def _send_buttons(self, channel, view: discord.ui.View):
        """Send a message carrying only the buttons. None if it could not go out.

        Discord has historically refused a message with components and no
        content or embed, and I could not verify which way this account's API
        behaves from here, so an empty send falls back to a zero-width space
        rather than losing the buttons. The space renders as nothing.
        """
        async with safe_typing(channel):
            await asyncio.sleep(TYPING_DELAY_SECONDS)
        try:
            return await channel.send(view=view)
        except discord.HTTPException:
            logger.debug("Components-only message refused; retrying with a spacer.", exc_info=True)
        try:
            return await channel.send(content="​", view=view)
        except discord.HTTPException:
            logger.debug("Buttons could not be sent at all.", exc_info=True)
            return None

    async def _send_conversation_opening(self, channel) -> None:
        """Disclosure then greeting, at the start of every conversation.

        The disclosure is informational and repeats every time rather than being
        shown once and remembered: processing rests on the contractual
        relationship, not consent, so there is no per-user decision to store and
        nothing to look up. Nothing here waits for input.
        """
        for part in DISCLOSURE_PARTS:
            await self._send_with_typing(channel, self._plain_embed(part))
        for part in GREETING_PARTS:
            await self._send_with_typing(channel, self._plain_embed(part))

    @staticmethod
    def _is_contentless(text: str) -> bool:
        """True when the message is a hello with no request attached.

        "hi" must not be escalated as an unanswerable question, but "hi when is
        the flight" carries a real one, so only messages made up entirely of
        greeting words count.
        """
        words = re.findall(r"[a-z']+", text.lower())
        if not words:
            # Emoji, punctuation or an attachment with no text.
            return True
        return all(word in GREETING_WORDS for word in words)

    @staticmethod
    def _is_closing_request(text: str) -> typing.Optional[str]:
        for pattern in _CLOSING_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    async def _close_conversation(self, user_id: int, channel, *, reason: str) -> None:
        """Send the three closing messages, then end the conversation."""
        try:
            for part in CLOSING_PARTS:
                await self._send_with_typing(channel, self._plain_embed(part))
        except discord.HTTPException:
            # Closing still has to happen, or the user is stuck in a conversation
            # the assistant believes is over.
            logger.error("Could not deliver the closing messages to %s.", user_id, exc_info=True)

        await self._close_transcript(user_id, handed_off=False)
        logger.info("Closed the pre-screen conversation with %s (%s).", user_id, reason)

    async def _confirm_handoff(self, message: discord.Message) -> bool:
        """Ask before escalating.

        Returns True to stay with the assistant, False to hand off. An
        unanswered prompt escalates: see HANDOFF_CONFIRM_TIMEOUT_SECONDS.
        """
        user = message.author
        # Modmail's own unlabelled buttons, carrying the guild emoji and nothing
        # else. The consent notice keeps its text labels: an emoji-only choice is
        # fine for "shall I fetch a human", not for accepting a privacy policy.
        view = YesNoView(timeout=HANDOFF_CONFIRM_TIMEOUT_SECONDS)
        view.add_item(AcceptButton("nas-handoff-yes", BUTTON_YES_EMOJI))
        view.add_item(DenyButton("nas-handoff-no", BUTTON_NO_EMOJI))

        # The statement and the buttons are two separate messages.
        try:
            await self._send_with_typing(message.channel, self._plain_embed(HANDOFF_CONFIRM_TEXT))
        except discord.HTTPException:
            logger.error("Could not tell %s the assistant is stuck; escalating.", user, exc_info=True)
            return False

        prompt = await self._send_buttons(message.channel, view)

        if prompt is None:
            # Overwhelmingly the guild emoji: Discord rejects a component
            # carrying one the bot cannot use, and that fails the whole message,
            # so the user would silently get no buttons at all. Retry with plain
            # unicode rather than escalating over a decoration.
            logger.error(
                "Handoff buttons rejected for %s, retrying with unicode instead of %s / %s. "
                "Check `%svlg status`.",
                user,
                BUTTON_YES_EMOJI,
                BUTTON_NO_EMOJI,
                self.bot.prefix,
            )
            view = YesNoView(timeout=HANDOFF_CONFIRM_TIMEOUT_SECONDS)
            view.add_item(AcceptButton("nas-handoff-yes", BUTTON_YES_FALLBACK))
            view.add_item(DenyButton("nas-handoff-no", BUTTON_NO_FALLBACK))
            prompt = await self._send_buttons(message.channel, view)

        if prompt is None:
            logger.error("Could not send handoff buttons to %s at all; escalating.", user)
            return False

        await view.wait()

        if view.value is False:
            await self._send_with_typing(message.channel, self._plain_embed(HANDOFF_DECLINED_TEXT))
            logger.info("%s (%s) declined the handoff; staying with the assistant.", user, user.id)
            return True

        if view.value is None:
            try:
                await prompt.edit(view=None)
            except discord.HTTPException:
                logger.debug("Could not clear handoff buttons after timeout.", exc_info=True)
            logger.info("Handoff confirmation timed out for %s (%s); escalating.", user, user.id)

        await self._send_with_typing(message.channel, self._plain_embed(HANDOFF_ACCEPTED_TEXT))
        await self._send_with_typing(message.channel, self._plain_embed(HANDOFF_GOODBYE_TEXT))
        return False

    @staticmethod
    def _escalation_match(text: str) -> typing.Optional[str]:
        for pattern in _ESCALATION_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    def _diag(self, msg: str, *args) -> None:
        """Diagnostic line: debug normally, info while `.vlg verbose` is on.

        Modmail applies log_level once at startup, so turning on global debug
        needs a restart and brings discord.py's own debug noise with it. The
        toggle promotes just these lines instead.
        """
        logger.info(msg, *args) if self._verbose else logger.debug(msg, *args)

    def _open_filter(self, user_hash: str) -> dict:
        """Matches a pre-screen conversation that is still running.

        Both fields must be unset. `handed_off_at` alone would leave documents
        written before automatic closing existed looking open forever, and
        `closed_at` alone would do the same to older handed-off ones.
        """
        return {
            "_type": TYPE_TRANSCRIPT,
            "user_id_hash": user_hash,
            "handed_off_at": None,
            "closed_at": None,
        }

    async def _open_transcript(self, user_id: int) -> typing.Optional[dict]:
        """The user's in-progress pre-screen, if it has not ended."""
        return await self.db.find_one(self._open_filter(await self._user_hash(user_id)))

    async def _append_transcript(
        self,
        user_id: int,
        user_text: str,
        assistant_text: typing.Optional[str] = None,
        status: typing.Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        resolved = status in STATUS_HANDLED if status else None
        entries = [{"role": "user", "content": user_text, "at": now}]
        if assistant_text is not None:
            # `status` is kept per turn as well as on the document, because the
            # replay has to rebuild the exact json this turn was produced as.
            entries.append({"role": "assistant", "content": assistant_text, "at": now, "status": status})

        await self.db.update_one(
            self._open_filter(await self._user_hash(user_id)),
            {
                "$push": {"messages": {"$each": entries}},
                "$set": {"resolved": resolved, "status": status},
                "$setOnInsert": {
                    "created_at": now,
                    # TTL index on this field expires the transcript 7 days
                    # after it started. Only transcripts carry it.
                    "expires_at": now + timedelta(days=TRANSCRIPT_RETENTION_DAYS),
                },
            },
            upsert=True,
        )

        # Activity clock for the inactivity sweep. Any reply clears a pending
        # warning, so a user who comes back gets the full hour again.
        await self.db.update_one(
            {"_type": TYPE_SESSION, "user_id": user_id},
            {
                "$set": {"last_activity_at": now, "warned_at": None},
                "$setOnInsert": {"started_at": now},
            },
            upsert=True,
        )

    async def _mark_handoff_reason(self, user_id: int, reason: str) -> None:
        """Record why a conversation is about to reach a human.

        Without this every handoff looks identical in storage, and a deferral
        rate would silently fold in people who asked for an agent outright and
        conversations that only escalated because Groq was unreachable.
        """
        await self.db.update_one(
            self._open_filter(await self._user_hash(user_id)),
            {"$set": {"handoff_reason": reason}},
        )

    async def _close_transcript(self, user_id: int, *, handed_off: bool) -> None:
        """End the pre-screen conversation.

        Without this the transcript stays open forever: the next conversation
        would skip its disclosure and replay stale history back to Groq.

        `handed_off_at` is still stamped separately when a human took over, so
        "was this escalated" stays answerable rather than being flattened into
        "this ended".
        """
        now = datetime.now(timezone.utc)
        changes = {"closed_at": now}
        if handed_off:
            changes["handed_off_at"] = now

        await self.db.update_one(self._open_filter(await self._user_hash(user_id)), {"$set": changes})
        await self.db.delete_one({"_type": TYPE_SESSION, "user_id": user_id})

    @staticmethod
    def _replay(history: list) -> list:
        """Turn stored transcript entries back into a well-formed exchange.

        Two things have to be repaired, and both only ever affected the live
        path, which is why a dry run with no history answered better than a real
        conversation did.

        The assistant is required to emit a json object, but its replies are
        stored as the plain text inside them. Replaying that bare text shows the
        model its own previous turns breaking the output contract it is being
        held to, which degrades both format and answer quality on every turn
        after the first. Assistant turns are therefore rebuilt into the shape
        they were originally produced in.

        A user turn is also appended on paths where no answer follows: a
        contentless hello, an escalation phrase, a Groq failure. Left in, those
        stack consecutive user messages with no assistant turn between them.
        Unanswered user turns are dropped from the replay for that reason; they
        remain in the transcript for summaries and stats.
        """
        turns = []
        for entry in history:
            role, content = entry.get("role"), entry.get("content")
            if role not in ("user", "assistant") or not content:
                continue
            if role == "assistant":
                content = json.dumps({"status": entry.get("status") or STATUS_ANSWERED, "reply": content})
            turns.append({"role": role, "content": content})

        paired = []
        for index, turn in enumerate(turns):
            if turn["role"] == "user":
                following = turns[index + 1] if index + 1 < len(turns) else None
                if following is None or following["role"] != "assistant":
                    continue
            paired.append(turn)

        return paired[-AI_HISTORY_LIMIT:]

    async def _groq_answer(self, history: list, user_text: str) -> typing.Tuple[str, list, str]:
        """Ask Groq to answer or defer. Raises on any failure."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self._replay(history))
        messages.append({"role": "user", "content": user_text})

        completion = await self._groq().chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=600,
        )

        raw = completion.choices[0].message.content
        payload = json.loads(raw)
        status = payload.get("status")
        reply = payload.get("reply")

        # Tolerate the old boolean if the model falls back to it, rather than
        # treating a usable answer as a malformed response and handing off.
        if status is None and isinstance(payload.get("resolved"), bool):
            status = STATUS_ANSWERED if payload["resolved"] else STATUS_ESCALATE

        # The model may split an answer across two messages, so `reply` is
        # accepted as either a string or a short list of them.
        if isinstance(reply, str):
            reply = [reply]
        if not isinstance(reply, list):
            raise ValueError(f"unusable Groq payload: {payload!r}")

        replies = [part.strip() for part in reply if isinstance(part, str) and part.strip()]

        # A malformed response must not be treated as a confident answer.
        if status not in STATUSES or not replies:
            raise ValueError(f"unusable Groq payload: {payload!r}")

        return status, replies[:2], raw

    async def _ai_prescreen(self, message: discord.Message) -> bool:
        """Try to resolve the message without a human.

        Returns True when the user has been answered and no thread is needed.
        Returns False to fall through to handoff. Never raises: every failure
        path ends in a handoff so the user always gets a response.
        """
        user = message.author
        content = (message.content or "").strip()

        # No open transcript means this is the start of a new conversation.
        transcript = await self._open_transcript(user.id)
        greeted = False

        # Disclosure and greeting go out before anything is decided about the
        # message, including escalation, so opening with "agent" still gets the
        # disclosure first.
        if transcript is None:
            try:
                await self._send_conversation_opening(message.channel)
                greeted = True
            except discord.HTTPException:
                # Not worth abandoning the request over; carry on and answer.
                logger.error("Failed opening conversation with %s (%s).", user, user.id, exc_info=True)

        # Only once a conversation is already running: "bye" as an opening line
        # is not a request to close something that has not started.
        if transcript is not None:
            closing = self._is_closing_request(content)
            if closing:
                await self._append_transcript(user.id, content, status=STATUS_CHAT)
                await self._close_conversation(user.id, message.channel, reason=f"user said {closing!r}")
                return True

        # A bare "hi" is an opening, not an unanswerable question. Asking what
        # they need is the reply; escalating a hello to a human is not.
        if self._is_contentless(content):
            await self._append_transcript(user.id, content, status=STATUS_CHAT)
            if not greeted:
                # Mid-conversation, so the greeting's own prompt is not in view.
                await self._send_with_typing(message.channel, self._plain_embed(CONTENTLESS_PROMPT))
            logger.info("Contentless opener from %s (%s); prompted instead of escalating.", user, user.id)
            return True

        matched = self._escalation_match(content)
        if matched:
            logger.info("Escalation phrase %r from %s (%s).", matched, user, user.id)
            await self._append_transcript(user.id, content, status=STATUS_ESCALATE)
            await self._mark_handoff_reason(user.id, HANDOFF_ASKED_FOR_HUMAN)
            return await self._confirm_handoff(message)

        # Technical failures below skip the confirmation and hand off directly.
        # Offering to keep talking to an assistant that cannot answer would loop
        # the user through the same failure on every message.
        if self._groq() is None:
            await self._append_transcript(user.id, content, status=STATUS_ESCALATE)
            await self._mark_handoff_reason(user.id, HANDOFF_AI_UNAVAILABLE)
            return False

        history = (transcript or {}).get("messages", [])

        try:
            async with safe_typing(message.channel):
                status, replies, raw = await asyncio.wait_for(
                    self._groq_answer(history, content),
                    timeout=GROQ_TIMEOUT_SECONDS,
                )
        except Exception:
            logger.error("Groq pre-screen failed for %s (%s); handing off.", user, user.id, exc_info=True)
            await self._append_transcript(user.id, content, status=STATUS_ESCALATE)
            await self._mark_handoff_reason(user.id, HANDOFF_AI_ERROR)
            return False

        await self._append_transcript(user.id, content, "\n\n".join(replies), status)

        # The model's own words go out first in every case, including when it is
        # about to hand over. Discarding them and sending only a canned line is
        # what made the assistant read as giving up rather than trying.
        try:
            for reply in replies:
                await self._send_with_typing(message.channel, self._ai_embed(reply))
        except discord.HTTPException:
            logger.error("Failed delivering AI reply to %s; handing off.", user, exc_info=True)
            return False

        if status in STATUS_HANDLED:
            await self._set_unclear_streak(user.id, 0)
            logger.info("AI handled the request from %s (%s) as %s.", user, user.id, status)
            return True

        if status == STATUS_UNCLEAR:
            streak = await self._bump_unclear_streak(user.id)
            if streak < MAX_CONSECUTIVE_UNCLEAR:
                logger.info("AI did not understand %s (%s); asked to rephrase.", user, user.id)
                return True
            # Asking a third time would be its own dead end.
            logger.info("AI still did not understand %s (%s) after %s tries.", user, user.id, streak)
            await self._mark_handoff_reason(user.id, HANDOFF_AI_UNCLEAR)
            return await self._confirm_handoff(message)

        logger.info("AI deferred for %s (%s); asking about handoff.", user, user.id)
        # Why it deferred is invisible from the line above, so the message that
        # prompted it and the model's verbatim answer go out together.
        self._diag("Deferred message from %s (%s) was: %r", user, user.id, content)
        self._diag("Groq raw response for %s (%s): %s", user, user.id, raw)
        await self._mark_handoff_reason(user.id, HANDOFF_AI_DEFERRED)
        return await self._confirm_handoff(message)

    async def _bump_unclear_streak(self, user_id: int) -> int:
        session = await self.db.find_one({"_type": TYPE_SESSION, "user_id": user_id})
        streak = (session or {}).get("unclear_streak", 0) + 1
        await self._set_unclear_streak(user_id, streak)
        return streak

    async def _set_unclear_streak(self, user_id: int, value: int) -> None:
        await self.db.update_one(
            {"_type": TYPE_SESSION, "user_id": user_id},
            {"$set": {"unclear_streak": value}},
        )

    def _plain_embed(self, text: str) -> discord.Embed:
        """Plugin-authored copy: greeting, prompts, handoff messages.

        No title and no footer. The "can make mistakes" caveat is a statement
        about model output, so putting it on fixed strings the plugin controls
        would misattribute it.
        """
        return self._embed(description=text, color=self.bot.main_color)

    def _ai_embed(self, reply: str) -> discord.Embed:
        """A model-generated reply. Custom; Modmail has no slot for this.

        No title: the author row already names the assistant, and carrying both
        says it twice. The footer stays plain text, since an icon beside a
        disclaimer reads as branding rather than a caveat.
        """
        return self._embed(
            description=reply,
            color=self.bot.mod_color,
            footer=AI_FOOTER,
            footer_icon=False,
        )

    # ------------------------------------------------------------------
    # Handoff (stage 5)
    # ------------------------------------------------------------------

    async def _summarise(self, history: list) -> str:
        """Two or three sentences for the staff member picking this up.

        Never raises: a failed summary must not stop the handoff, so the
        fallback text goes in instead and staff read the thread themselves.
        """
        turns = [
            f"{entry.get('role', 'user')}: {entry.get('content', '')}"
            for entry in history[-AI_HISTORY_LIMIT:]
            if entry.get("content")
        ]
        if not turns:
            return SUMMARY_FALLBACK

        client = self._groq()
        if client is None:
            return SUMMARY_FALLBACK

        try:
            completion = await asyncio.wait_for(
                client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": SUMMARY_PROMPT},
                        {"role": "user", "content": "\n".join(turns)},
                    ],
                    temperature=0.2,
                    max_tokens=300,
                ),
                timeout=GROQ_TIMEOUT_SECONDS,
            )
            summary = (completion.choices[0].message.content or "").strip()
        except Exception:
            logger.error("Handoff summary failed; using the fallback.", exc_info=True)
            return SUMMARY_FALLBACK

        return summary or SUMMARY_FALLBACK

    async def _new_ticket_reference(self) -> str:
        """A VLG-XXXXXX code, unique against the partition."""
        for _ in range(10):
            body = "".join(secrets.choice(TICKET_ALPHABET) for _ in range(TICKET_BODY_LENGTH))
            reference = f"{TICKET_PREFIX}-{body}"
            if await self.db.find_one({"_type": TYPE_TICKET, "nas_ref": reference}) is None:
                return reference
        # 32^6 codes makes this essentially unreachable, but never hand back a
        # reference that might already belong to another ticket.
        raise RuntimeError("could not allocate an unused ticket reference")

    async def _prepare_handoff(self, message: discord.Message) -> None:
        """Summarise before Modmail creates the thread.

        The summary is stashed for on_thread_ready rather than posted here,
        because the channel does not exist yet.
        """
        user = message.author
        transcript = await self._open_transcript(user.id)
        history = (transcript or {}).get("messages", [])
        try:
            self._pending_handoff[user.id] = await self._summarise(history)
        except Exception:
            logger.error("Preparing the handoff summary failed.", exc_info=True)
            self._pending_handoff[user.id] = SUMMARY_FALLBACK

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """Post the summary and hand the user their reference.

        Modmail dispatches this once the channel, genesis message and staff
        mirroring are all in place, so everything below is additive.
        """
        recipient = getattr(thread, "recipient", None)
        if recipient is None:
            return

        summary = self._pending_handoff.pop(recipient.id, None)
        if summary is None:
            # A thread Modmail opened without going through the pre-screen,
            # such as ?contact. Not ours to annotate.
            return

        try:
            reference = await self._new_ticket_reference()
        except Exception:
            logger.error("Could not allocate a ticket reference.", exc_info=True)
            reference = None

        # Modmail's own log key is the durable identifier; the reference is the
        # readable handle staff and users can actually say out loud.
        log_key = None
        try:
            log = await self.bot.api.get_log(thread.channel.id)
            log_key = (log or {}).get("key")
        except Exception:
            logger.error("Could not read the log key for %s.", thread.channel, exc_info=True)

        if reference is not None:
            try:
                await self.db.insert_one(
                    {
                        "_type": TYPE_TICKET,
                        "nas_ref": reference,
                        "log_key": log_key,
                        "user_id": recipient.id,
                        "channel_id": thread.channel.id,
                        "created_at": datetime.now(timezone.utc),
                    }
                )
            except Exception:
                logger.error("Could not store ticket mapping %s.", reference, exc_info=True)

        embed = self._embed(
            title="Assistant summary",
            description=summary,
            color=self.bot.main_color,
            footer=f"Reference {reference}" if reference else "Reference unavailable",
        )
        try:
            await thread.channel.send(embed=embed)
        except discord.HTTPException:
            logger.error("Could not post the handoff summary to %s.", thread.channel, exc_info=True)

        if reference is not None:
            try:
                await self._send_with_typing(
                    await recipient.create_dm(),
                    self._plain_embed(HANDOFF_REFERENCE_TEXT.format(reference=reference)),
                )
            except discord.HTTPException:
                logger.error("Could not send the reference to %s.", recipient, exc_info=True)

        logger.info("Handed %s (%s) to a human as %s.", recipient, recipient.id, reference)

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
        footer_icon: bool = True,
    ) -> discord.Embed:
        """Build an embed in Modmail's own style.

        Colors come from bot.config at call time via the bot's colour
        properties, so `?config set main_color ...` takes effect without a
        reload and nothing here hardcodes a hex value.

        Every embed the plugin builds carries the assistant's author row, so
        the identity is consistent whether the text came from the model or is
        fixed plugin copy.
        """
        embed = discord.Embed(
            color=self.bot.main_color if color is None else color,
            description=description,
        )
        if title is not None:
            embed.title = title
        if self.bot.config["show_timestamp"]:
            embed.timestamp = discord.utils.utcnow()

        # Icon follows whatever avatar is set in the Developer Portal, so it
        # tracks the bot's account without a redeploy.
        embed.set_author(name=AI_TITLE, icon_url=self._bot_avatar())

        if footer is not None:
            embed.set_footer(
                text=footer,
                icon_url=(self.bot.get_guild_icon(guild=self.bot.guild, size=128) if footer_icon else None),
            )
        return embed

    def _bot_avatar(self) -> typing.Optional[str]:
        """The bot's avatar URL, or None.

        Guarded from `self.bot` outwards on purpose: `bot.user` is None until
        login, and an embed helper must never be able to raise. This is called
        while building the opening disclosure, and an exception there would fall
        through the gate's fail-open path and skip the disclosure entirely.
        """
        if AI_ICON_URL:
            return AI_ICON_URL
        user = getattr(self.bot, "user", None)
        return getattr(getattr(user, "display_avatar", None), "url", None)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @commands.group(name="vlg", aliases=["nas"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def vlg(self, ctx):
        """Vueling support plugin."""
        await ctx.send_help(ctx.command)

    @vlg.command(name="status")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def vlg_status(self, ctx):
        """Show plugin wiring, storage and config state."""
        hooked = getattr(self.bot.process_dm_modmail, "__nas_wrapped__", False)

        try:
            counts = {
                "transcripts": await self.db.count_documents({"_type": TYPE_TRANSCRIPT}),
                "tickets": await self.db.count_documents({"_type": TYPE_TICKET}),
                "open conversations": await self.db.count_documents({"_type": TYPE_SESSION}),
            }
            # Written by the removed consent gate. Non-zero means personal data
            # is being kept that nothing reads any more.
            legacy = await self.db.count_documents({"_type": TYPE_LEGACY_CONSENT})
            if legacy:
                counts["obsolete consent records"] = legacy
            storage = "\n".join(f"`{k}`: {v}" for k, v in counts.items())
        except Exception as e:
            storage = f"unreachable: `{e}`"

        embed = self._embed(title="Vueling support — status")
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

        # The author-row icon is a common source of "that isn't my logo": the
        # Portal's App Icon and the Bot avatar are different images, and only
        # the latter reaches display_avatar.
        icon = self._bot_avatar()
        if AI_ICON_URL:
            icon_state = f"overridden by `AI_ICON_URL`\n{icon}"
        elif icon:
            icon_state = f"bot avatar\n{icon}"
        else:
            icon_state = "**none** — the bot has no avatar set on the Portal's *Bot* tab"
        embed.add_field(name="Embed icon", value=icon_state, inline=False)

        # A component carrying an emoji the bot cannot use is rejected outright,
        # taking the whole message with it, so this is the usual reason a
        # confirmation prompt never appears.
        emoji_lines = []
        for label, raw in (("yes", BUTTON_YES_EMOJI), ("no", BUTTON_NO_EMOJI)):
            match = re.search(r":(\d+)>$", raw)
            resolved = self.bot.get_emoji(int(match.group(1))) if match else None
            emoji_lines.append(f"`{label}` {raw} — {'usable' if resolved else '**not usable by this bot**'}")
        embed.add_field(
            name="Confirmation button emoji",
            value="\n".join(emoji_lines)
            + (
                "\n*Unusable means the bot is not in the server that owns the emoji. "
                "The prompt falls back to plain unicode.*"
            ),
            inline=False,
        )

        embed.add_field(
            name="Verbose diagnostics",
            value=(
                "`on` — deferrals log the message and Groq's raw response at INFO"
                if self._verbose
                else "`off` — defer reasons only appear at DEBUG"
            ),
            inline=False,
        )

        # The linked privacy policy is what now states retention, so this is no
        # longer self-checking: if that page promises deletion, this has to be set
        # for the promise to hold.
        expiry = self.bot.config.get("log_expiration")
        embed.add_field(
            name="Ticket log retention",
            value=(
                f"`{isodate.duration_isoformat(expiry)}` — ticket logs expire"
                if expiry and expiry != isodate.Duration()
                else (
                    "`Never` — ticket logs are kept forever. **Check this against "
                    "what the linked privacy policy says.** Set `log_expiration` to "
                    "`P7D` and restart the bot to match a 7 day promise."
                )
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @vlg.command(name="version", aliases=["updated"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def vlg_version(self, ctx):
        """What code is actually running, and whether it is the code on disk."""
        path = pathlib.Path(__file__).resolve()

        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:12]
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            lines = raw.count(b"\n") + 1
        except OSError as e:
            return await ctx.send(
                embed=self._embed(
                    description=f"Could not read the plugin file: `{e}`", color=self.bot.error_color
                )
            )

        # The comparison that actually matters. Editing or pulling the file does
        # nothing until the plugin is reloaded, and every other field here would
        # look healthy while stale code kept running.
        stale = self._loaded_at is not None and modified > self._loaded_at

        embed = self._embed(
            title="Vueling support — running code",
            description=(
                "**The file on disk is newer than the running code.** "
                f"Reload with `{self.bot.prefix}plugin reload @local/norwegian_support`."
                if stale
                else "Running code matches the file on disk."
            ),
            color=self.bot.error_color if stale else None,
        )
        # Both the computed age and Discord's own timestamp: the first is exact
        # and identical for everyone, the second renders in the reader's own
        # timezone but rounds hard ("an hour ago" for anything near one).
        embed.add_field(
            name="File last updated",
            value=(
                f"**{human_timedelta(modified)}**\n"
                f"{discord.utils.format_dt(modified, 'F')} ({discord.utils.format_dt(modified, 'R')})"
            ),
            inline=False,
        )
        if self._loaded_at is not None:
            embed.add_field(
                name="Plugin loaded",
                value=(
                    f"**{human_timedelta(self._loaded_at)}**"
                    f" — running for {human_timedelta(self._loaded_at, suffix=False)}\n"
                    f"{discord.utils.format_dt(self._loaded_at, 'F')}"
                ),
                inline=False,
            )
            gap = self._loaded_at - modified
            embed.add_field(
                name="Loaded after the file changed?",
                value=(
                    f"yes, by {human_timedelta(modified, source=self._loaded_at, suffix=False)}"
                    if gap.total_seconds() >= 0
                    else f"**no — the file changed "
                    f"{human_timedelta(self._loaded_at, source=modified, suffix=False)} later**"
                ),
                inline=False,
            )
        embed.add_field(name="Content hash", value=f"`{digest}`  ({lines} lines)", inline=False)

        commit = self._git_head()
        if commit:
            embed.add_field(name="Repo commit", value=f"`{commit}`", inline=False)

        embed.set_footer(text="Hash changes whenever the file does; compare it against what was shipped.")
        await ctx.send(embed=embed)

    @staticmethod
    def _git_head() -> typing.Optional[str]:
        """Short commit of the checkout, best-effort and without shelling out."""
        try:
            root = pathlib.Path(__file__).resolve().parents[3]
            head = (root / ".git" / "HEAD").read_text().strip()
            if not head.startswith("ref: "):
                return head[:8]
            ref = (root / ".git" / head[5:]).read_text().strip()
            return ref[:8]
        except Exception:
            # Packed refs, a non-git deploy, a different layout. Not worth
            # failing the command over.
            return None

    @vlg.command(name="ask")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def vlg_ask(self, ctx, *, question: str):
        """Dry-run a question through the pre-screen and show the raw result.

        Touches nothing: no conversation, no transcript, no session, no
        greeting. For iterating on the FAQ without DMing from a test account.
        """
        # Mirrors _ai_prescreen's order, so the route shown here is the route a
        # real message would take. Diverging from it would make this misleading.
        if self._is_contentless(question):
            return await ctx.send(
                embed=self._embed(
                    title="Dry run — no pre-screen",
                    description=(
                        f"> {truncate(question, 200)}\n\n"
                        "Treated as a greeting with no request in it. Answered with a "
                        "prompt for what they need; never escalated, never sent to Groq."
                    ),
                )
            )

        closing = self._is_closing_request(question)
        if closing:
            return await ctx.send(
                embed=self._embed(
                    title="Dry run — no pre-screen",
                    description=(
                        f"> {truncate(question, 200)}\n\n"
                        f"Matches a closing phrase (`{closing}`), so mid-conversation this "
                        "would end the chat. As a first message it would fall through to "
                        "the pre-screen instead."
                    ),
                )
            )

        matched = self._escalation_match(question)
        if matched:
            return await ctx.send(
                embed=self._embed(
                    title="Dry run — no pre-screen",
                    description=(
                        f"> {truncate(question, 200)}\n\n"
                        f"Matches escalation phrase `{matched}`, so it goes straight to the "
                        "handoff offer without Groq being called at all."
                    ),
                )
            )

        if self._groq() is None:
            return await ctx.send(
                embed=self._embed(
                    description="Groq is not configured, so every question escalates. "
                    f"See `{self.bot.prefix}vlg status`.",
                    color=self.bot.error_color,
                )
            )

        async with safe_typing(ctx):
            try:
                status, replies, raw = await asyncio.wait_for(
                    self._groq_answer([], question), timeout=GROQ_TIMEOUT_SECONDS
                )
            except Exception as e:
                return await ctx.send(
                    embed=self._embed(
                        title="Dry run — pre-screen failed",
                        description=(
                            f"> {truncate(question, 200)}\n\n"
                            f"```{type(e).__name__}: {truncate(str(e), 400)}```\n"
                            "In a real conversation this hands off immediately, with no "
                            "confirmation prompt."
                        ),
                        color=self.bot.error_color,
                    )
                )

        embed = self._embed(
            title=f"Dry run — {status}",
            description=f"> {truncate(question, 200)}",
            color=None if status in STATUS_HANDLED else self.bot.error_color,
        )
        embed.add_field(
            name="Outcome",
            value={
                STATUS_ANSWERED: "`answered` — the reply goes out and no thread is created.",
                STATUS_CHAT: "`chat` — conversational reply, no airline fact needed, no thread.",
                STATUS_UNCLEAR: (
                    "`unclear` — the reply asks them to rephrase. Only after "
                    f"{MAX_CONSECUTIVE_UNCLEAR} unclear turns in a row is an agent offered."
                ),
                STATUS_ESCALATE: (
                    "`escalate` — the reply is sent first, then the user is asked "
                    "whether to connect to an agent."
                ),
            }[status],
            inline=False,
        )
        for index, reply in enumerate(replies, start=1):
            embed.add_field(
                name=f"Reply {index} of {len(replies)}" if len(replies) > 1 else "Reply",
                value=truncate(reply, 1000),
                inline=False,
            )
        embed.add_field(name="Raw", value=f"```json\n{truncate(raw, 900)}\n```", inline=False)
        embed.set_footer(text="No history replayed, so this is a first message. Nothing was stored.")
        await ctx.send(embed=embed)

    @vlg.command(name="stats")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def vlg_stats(self, ctx):
        """How the assistant is doing, and what it keeps failing to answer."""
        try:
            transcripts = await self.db.find({"_type": TYPE_TRANSCRIPT}).to_list(length=2000)
        except Exception as e:
            return await ctx.send(
                embed=self._embed(
                    description=f"Could not read transcripts: `{e}`", color=self.bot.error_color
                )
            )

        if not transcripts:
            return await ctx.send(
                embed=self._embed(
                    title="Vueling AI — stats",
                    description=(
                        "No conversations on record yet.\n\nTranscripts are deleted after "
                        f"{TRANSCRIPT_RETENTION_DAYS} days, so this is always a rolling window."
                    ),
                )
            )

        total = len(transcripts)
        still_open = sum(1 for t in transcripts if t.get("closed_at") is None)
        handed_off = [t for t in transcripts if t.get("handed_off_at") is not None]
        finished = [t for t in transcripts if t.get("closed_at") is not None]

        # Only genuine deferrals belong in the headline rate. A user typing
        # "agent" is a routing preference, not a failure to answer.
        deferred = [t for t in handed_off if t.get("handoff_reason") == HANDOFF_AI_DEFERRED]
        deferral_rate = (len(deferred) / len(finished) * 100) if finished else 0.0
        handoff_rate = (len(handed_off) / len(finished) * 100) if finished else 0.0

        embed = self._embed(
            title="Vueling AI — stats",
            description=(
                f"**{total}** conversation(s) on record, **{still_open}** still open.\n"
                f"Transcripts are deleted after {TRANSCRIPT_RETENTION_DAYS} days, so this is "
                "a rolling window, not all time."
            ),
        )
        embed.add_field(
            name="Reached a human",
            value=(
                f"**{handoff_rate:.0f}%** of finished conversations ({len(handed_off)}/{len(finished)})\n"
                f"of which **{deferral_rate:.0f}%** were the assistant genuinely unable to answer"
                if finished
                else "no finished conversations yet"
            ),
            inline=False,
        )

        reasons = {}
        for t in handed_off:
            reasons[t.get("handoff_reason") or "unrecorded"] = (
                reasons.get(t.get("handoff_reason") or "unrecorded", 0) + 1
            )
        if reasons:
            embed.add_field(
                name="Why it handed off",
                value="\n".join(
                    f"`{count}` {HANDOFF_REASON_LABELS.get(reason, reason)}"
                    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])
                ),
                inline=False,
            )

        questions = [self._last_user_message(t) for t in deferred]
        questions = [q for q in questions if q]

        if questions:
            repeats = {}
            for q in questions:
                key = " ".join(re.findall(r"[\w']+", q.lower()))
                repeats[key] = repeats.get(key, 0) + 1
            repeated = [(k, c) for k, c in repeats.items() if c > 1]
            repeated.sort(key=lambda kv: -kv[1])
            if repeated:
                embed.add_field(
                    name="Asked more than once",
                    value="\n".join(f"`{c}x` {truncate(k, 80)}" for k, c in repeated[:5]),
                    inline=False,
                )

            # Most questions are phrased uniquely, so term frequency is what
            # actually points at the missing FAQ entry.
            terms = {}
            for q in questions:
                for word in set(re.findall(r"[a-z']{3,}", q.lower())):
                    if word not in STATS_STOPWORDS:
                        terms[word] = terms.get(word, 0) + 1
            ranked = sorted(terms.items(), key=lambda kv: -kv[1])[:12]
            if ranked:
                embed.add_field(
                    name="Common terms in unanswered questions",
                    value=" ".join(f"`{w}`×{c}" for w, c in ranked),
                    inline=False,
                )

            embed.add_field(
                name="Most recent unanswered",
                value="\n".join(f"• {truncate(q, 90)}" for q in questions[-5:]),
                inline=False,
            )
        else:
            embed.add_field(
                name="Unanswered questions",
                value="None recorded. Either nothing has been deferred, or those "
                "conversations predate handoff reasons being stored.",
                inline=False,
            )

        embed.set_footer(text="Add FAQ entries for what shows up here; each one removes a handoff.")
        await ctx.send(embed=embed)

    @staticmethod
    def _last_user_message(transcript: dict) -> typing.Optional[str]:
        """The question that ended up going to a human."""
        for entry in reversed(transcript.get("messages") or []):
            if entry.get("role") == "user" and entry.get("content"):
                return entry["content"]
        return None

    @vlg.command(name="verbose")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def vlg_verbose(self, ctx, enabled: bool = None):
        """Promote this plugin's diagnostics to INFO without a bot restart.

        Logs the message that caused a defer and Groq's verbatim response.
        """
        self._verbose = (not self._verbose) if enabled is None else enabled

        await self.db.update_one(
            {"_type": TYPE_META, "key": "verbose"},
            {"$set": {"value": self._verbose}},
            upsert=True,
        )

        if self._verbose:
            # Write a line immediately so the log itself confirms the toggle
            # took, rather than waiting on the next deferral to find out.
            logger.info(
                "Verbose diagnostics ENABLED by %s. This line confirms they reach the log.", ctx.author
            )
            note = (
                "Deferrals will now log the user's message and Groq's verbatim "
                "response at INFO.\n\nA confirmation line has just been written to "
                "the log — if you cannot see it, the log level itself is the "
                "problem, not this toggle.\n\nThis writes ticket message content "
                "to the bot log, which is not on the 7-day deletion path. Turn it "
                f"off with `{self.bot.prefix}vlg verbose off` once you are done."
            )
        else:
            logger.info("Verbose diagnostics disabled by %s.", ctx.author)
            note = "Diagnostics are back to DEBUG level."

        await ctx.send(
            embed=self._embed(
                title=f"Verbose diagnostics {'on' if self._verbose else 'off'}",
                description=note,
                color=None if self._verbose else self.bot.error_color,
                footer="Setting is stored, and survives reloads and restarts",
            )
        )

    @vlg.command(name="forget", aliases=["revoke"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def vlg_forget(self, ctx, user: discord.User):
        """Delete a user's stored assistant conversations.

        For acting on an erasure request from the data protection page. There is
        no consent to withdraw any more, but the transcripts still exist and this
        is the only way to remove them before their 7 day expiry.
        """
        transcripts = await self.db.delete_many(
            {"_type": TYPE_TRANSCRIPT, "user_id_hash": await self._user_hash(user.id)}
        )
        # Sweep any record left behind by the removed consent gate.
        legacy = await self.db.delete_many({"_type": TYPE_LEGACY_CONSENT, "user_id": user.id})

        if not transcripts.deleted_count and not legacy.deleted_count:
            return await ctx.send(
                embed=self._embed(
                    description=f"Nothing stored for {user.mention}.",
                    color=self.bot.error_color,
                )
            )

        note = f"Deleted {transcripts.deleted_count} assistant conversation(s) for {user.mention}."
        if legacy.deleted_count:
            note += f"\nAlso cleared {legacy.deleted_count} obsolete consent record(s)."
        note += (
            "\n\nThis does not touch ticket transcripts with a human agent: those are "
            "Modmail's own logs, expiring on `log_expiration`. Use `?logs` for those."
        )

        await ctx.send(embed=self._embed(description=note))
        logger.info("Erased stored data for %s (%s) at the request of %s.", user, user.id, ctx.author)

    @vlg.command(name="ticket")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def vlg_ticket(self, ctx, reference: str):
        """Look up a VLG-XXXXXX reference and its Modmail log."""
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
