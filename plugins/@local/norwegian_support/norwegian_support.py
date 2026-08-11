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

Almost everything here is configured from inside Discord rather than from this
file or from `.env`: `.ai set` for settings that carry a value, `.ai features`
for the optional behaviour that is simply on or off. Both are stored in the
plugin partition, so they survive reloads, restarts and a `git pull`. The
constants below are the defaults those settings fall back to. See the Settings
section for the registry.

Credentials — the bot token, GROQ_API_KEY, the Mongo URI — stay in `.env`. Those
are secrets rather than settings.
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
from bson import ObjectId
from discord.ext import commands, tasks
from pymongo import ReturnDocument

from core import checks
from core.models import DMDisabled, PermissionLevel, getLogger
from core.time import human_timedelta
from core.utils import safe_typing, truncate

try:
    from groq import AsyncGroq
except ImportError:  # pragma: no cover - dependency install failed
    AsyncGroq = None

logger = getLogger(__name__)

# The command group. `ai` is the primary name, so a fresh install on any bot
# reads as `.ai status` rather than as somebody else's branding.
#
# The legacy names stay as aliases: this plugin was `nas` and then `vlg` before
# it was `ai`, and typing either still works. Aliases do not appear in the help
# listing, so a new install sees only `.ai` while existing muscle memory and any
# saved command still resolve.
GROUP_NAME = "ai"
LEGACY_ALIASES = ("vlg", "nas")

# Default identity. Both are settings (`brandname` / `assistantname`), and every
# user-facing string that names either reads the setting rather than a constant,
# so installing this on another bot is a rename rather than an edit.
#
# BRAND_NAME is the organisation ("Vueling"); ASSISTANT_NAME is what the bot
# calls itself ("Vueling AI"). They are separate because the disclosure is about
# the company while the embed author row is about the bot.
BRAND_NAME = "Vueling"
ASSISTANT_NAME = "Vueling AI"

# Linked from the opening disclosure. Per-install and legally load-bearing: it
# is the only route by which a user exercises their data rights, so it is a
# setting rather than something to remember to edit.
PRIVACY_POLICY_URL = "https://vuelingrbx.vercel.app/privacy"


def disclosure_parts(brand: str, privacy_url: str) -> typing.Tuple[str, str]:
    """Shown at the start of every conversation, before the greeting.

    Informational only: processing is on the basis of the contractual
    relationship, not consent, so there is nothing to accept and nothing stored
    per user. Data rights are exercised through the linked privacy policy, not
    in chat.
    """
    return (
        f"{brand} will process your data to provide you with the services you "
        f"have requested and improve your experience with {brand}, based on the "
        "execution of a contractual relationship. For more information, see our "
        f"[privacy policy]({privacy_url}).",
        "This chatbot uses an Artificial Intelligence tool to identify the most "
        "relevant answers to frequently asked questions.",
    )


# Human-facing ticket reference prefix, mapped to Modmail's own log key. The
# default for the `ticketprefix` setting.
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
#
# Defaults for the `inactivitywarning` / `inactivityclose` settings, which are
# stored in minutes; these are the only place the durations are written as
# timedeltas.
INACTIVITY_WARNING_AFTER = timedelta(hours=1)
INACTIVITY_CLOSE_AFTER = timedelta(hours=3)

INACTIVITY_WARNING_TEXT = (
    "Are you still there? I'll close this chat shortly if you don't need anything "
    "else — just send a message and I'll keep helping. \N{SMILING FACE WITH SMILING EYES}"
)


def closing_parts(assistant: str) -> typing.Tuple[str, str, str]:
    """Three separate messages when a conversation ends, however it ended."""
    return (
        "Thanks for chatting with me today, I hope I was able to help! " "\N{SMILING FACE WITH SMILING EYES}",
        "Bye for now! \N{SMILING FACE WITH SMILING EYES}",
        f"You have been disconnected from {assistant}. Whenever you need us again, "
        "just send a message here and a new conversation will start.",
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

# Offered after the closing messages above, with a button that opens the survey.
# Only ever on an assistant-ended conversation: a thread a human took over ends
# through Modmail and never reaches _close_conversation.
SURVEY_INVITE_TEXT = (
    "We would like to hear your feedback! Click the button below to answer a " "quick 1 minute survey."
)

SURVEY_BUTTON_LABEL = "Answer the survey"
SURVEY_MODAL_TITLE = "Your feedback"

# The rating questions, in the order they appear. Each is (key, label,
# description, scale), where scale reads 1 to 5 and is worded for that specific
# question — a generic "satisfied/dissatisfied" scale against "how quickly did
# we reply" is the kind of thing that makes a form feel bolted together.
#
# `key` is the storage key and must not change once answers exist; the wording
# above it can be rewritten freely.
SURVEY_RATING_QUESTIONS = (
    (
        "speed",
        "How quickly did we get back to you?",
        "1 = far too slow, 5 = very quick",
        ("Far too slow", "A bit slow", "About right", "Quick", "Very quick"),
    ),
    (
        "helpfulness",
        "How helpful was the assistant?",
        "1 = not helpful at all, 5 = very helpful",
        ("Not helpful at all", "Not very helpful", "Somewhat helpful", "Helpful", "Very helpful"),
    ),
    (
        "overall",
        "How was your experience overall?",
        "1 = very dissatisfied, 5 = very satisfied",
        ("Very dissatisfied", "Dissatisfied", "Neutral", "Satisfied", "Very satisfied"),
    ),
)

# The one whose score is the headline: what `.ai stats` reports and what the
# low-rating alert fires on. Must be a key in SURVEY_RATING_QUESTIONS.
SURVEY_HEADLINE_KEY = "overall"

SURVEY_TRAINING_QUESTION = "Can we use this chat to help improve our AI?"
SURVEY_TRAINING_NOTE = "This is 100% anonymous."

# Discord's caps on a modal: 5 components, a 45 character label and a 100
# character description. Asserted at import so an over-long rewrite fails the
# plugin load rather than failing silently when a user opens the form.
assert len(SURVEY_RATING_QUESTIONS) + 1 <= 5, "a modal holds at most 5 components"
assert SURVEY_HEADLINE_KEY in {q[0] for q in SURVEY_RATING_QUESTIONS}, "headline key must be a question"
for _key, _label, _description, _scale in SURVEY_RATING_QUESTIONS:
    assert len(_label) <= 45, f"modal label over 45 characters: {_label!r}"
    assert len(_description) <= 100, f"modal description over 100 characters: {_description!r}"
    assert len(_scale) == 5, f"a 1-5 scale needs 5 words: {_key!r}"
assert len(SURVEY_TRAINING_QUESTION) <= 45, "modal label is capped at 45 characters"
assert len(SURVEY_TRAINING_NOTE) <= 100, "modal description is capped at 100 characters"


def survey_rating_options(scale: typing.Sequence[str]) -> typing.List[discord.SelectOption]:
    """A 1-5 dropdown, best first — the order a rating scale is read in."""
    return [
        discord.SelectOption(label=f"{score} — {scale[score - 1]}", value=str(score))
        for score in range(5, 0, -1)
    ]


SURVEY_THANKS_TRAINING = (
    "Thank you, your feedback has been recorded, and this chat will help us " "improve our assistant."
)
SURVEY_THANKS_RATING = "Thank you, your feedback has been recorded."
SURVEY_THANKS_EXPIRED = (
    "Thank you, your rating has been recorded. This conversation is no longer "
    "available, so nothing from it has been kept."
)
SURVEY_ALREADY_ANSWERED = "Thanks — you have already answered this one."
SURVEY_NOT_YOURS = "Sorry, that survey is not yours to answer."
SURVEY_FAILED = "Sorry, we could not save that. Please try again."

# Whether a 1-5 rating is kept when the user declines the training question. The
# rating document holds no conversation content — only the hashed user, the
# number and a timestamp — so it is feedback about the service rather than a
# copy of the chat. Set to False to discard ratings from anyone who said no.
KEEP_RATINGS_WITHOUT_CONSENT = True

# Samples shown by `.ai training` when no count is given.
TRAINING_SAMPLE_DEFAULT = 3
TRAINING_SAMPLE_MAX = 10

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

# Post-disconnect survey answers: the rating, and whether the chat could be
# reused. Content-free, and keyed by the same one-way hash as a transcript.
TYPE_SURVEY = "survey"

# Conversations the user agreed we could keep to improve the assistant. These
# carry no `expires_at` and so outlive the 7 day transcript retention — the
# whole point of asking. Written only on an explicit yes.
TYPE_TRAINING = "training_transcript"

# No longer written. Documents from the removed consent gate may still exist;
# `.ai forget` clears them alongside a user's transcripts.
TYPE_LEGACY_CONSENT = "consent"

GROQ_MODEL = "llama-3.3-70b-versatile"

# The gate runs inside Modmail's per-user DM queue, so a hung request would stall
# that user's messages. Bounded twice: once by the client, once by wait_for.
GROQ_TIMEOUT_SECONDS = 20

# Turns of prior context sent back to Groq. Caps token spend on long chats.
AI_HISTORY_LIMIT = 12

# An answer longer than this is split into two messages before it is sent. The
# prompt already asks the model to do this itself, but it obeys inconsistently
# and a long answer arriving as one wall of text is exactly the case that needed
# splitting, so the same rule is enforced here rather than left to the model.
#
# Sized against the FAQ answers: two or three sentences land under this and are
# left alone, four or more go over it and are broken up.
#
# Deliberately on the eager side. The complaint this exists to fix is long
# answers arriving as one block, and the cost of the two failure modes is not
# symmetric: an over-split reply is two short messages, which is how the rest of
# this conversation already reads, while an under-split one is the wall of text
# nobody wanted.
REPLY_SPLIT_THRESHOLD = 240

# A split is only worth making if both halves are substantial. Below this, the
# break lands near one end and produces a stray one-line message.
REPLY_SPLIT_MIN_PART = 80


# Assistant identity. The author row goes on every embed the plugin builds; the
# footer caveat does not, because it is a statement about model output and would
# misattribute fixed plugin copy. Both read the `assistantname` setting.
def ai_footer(assistant: str) -> str:
    return f"{assistant} can make mistakes. Please double check responses."


# Handoff confirmation buttons: guild emoji, no label. Defaults for the
# `yesemoji` / `noemoji` settings — these ids belong to one specific server, so
# on any other install they resolve to nothing and the prompt falls back to
# plain unicode. Setting them is how another install gets its own.
BUTTON_YES_EMOJI = "<:yes:1534231866888945764>"
BUTTON_NO_EMOJI = "<:no:1534231863319593172>"

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

HANDOFF_PARTNERSHIP = "partnership"

HANDOFF_REASON_LABELS = {
    HANDOFF_AI_DEFERRED: "assistant could not answer",
    HANDOFF_ASKED_FOR_HUMAN: "user asked for a human",
    HANDOFF_AI_UNAVAILABLE: "assistant not configured",
    HANDOFF_AI_ERROR: "Groq call failed",
    HANDOFF_AI_UNCLEAR: "could not understand after retrying",
    HANDOFF_PARTNERSHIP: "partnership lead",
}

# What staff see at the top of a handed-off ticket. Short enough to scan a
# channel list on, and ordered so the ones needing a specialist read first.
HANDOFF_REASON_TAGS = {
    HANDOFF_PARTNERSHIP: "\N{HANDSHAKE} PARTNERSHIP",
    HANDOFF_ASKED_FOR_HUMAN: "\N{BUSTS IN SILHOUETTE} ASKED FOR A HUMAN",
    HANDOFF_AI_UNCLEAR: "\N{WHITE QUESTION MARK ORNAMENT} REPEATEDLY UNCLEAR",
    HANDOFF_AI_DEFERRED: "\N{MEMO} NOT IN THE FAQ",
    HANDOFF_AI_UNAVAILABLE: "\N{WARNING SIGN} ASSISTANT OFFLINE",
    HANDOFF_AI_ERROR: "\N{WARNING SIGN} ASSISTANT ERROR",
}

HANDOFF_URGENCY_TAG = "\N{FIRE} ANGRY OR URGENT"

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

# Carried by every message whose only visible content is its buttons.
#
# Not decoration. A button answers by editing its own message to drop the
# components, and Discord rejects an edit that would leave a message with no
# content, no embed and no components. On a components-only message that edit
# therefore fails, the interaction is never acknowledged, and the click surfaces
# to the user as "Interaction failed" — which is exactly what the handoff
# confirmation was doing. A zero-width space renders as nothing and leaves
# something behind for the edit to keep.
BUTTON_SPACER = "\N{ZERO WIDTH SPACE}"

# Matches the id out of "<:name:1234>" / "<a:name:1234>".
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w+:(\d+)>$")

# Author-row icon. None means "use the bot's own avatar", which follows the
# Developer Portal without a redeploy and is the normal case.
#
# Set this to a direct image URL only if the embed icon needs to differ from the
# bot's avatar. Note the Portal has two separate images: the App Icon on General
# Information, and the Bot avatar on the Bot tab. Only the Bot avatar reaches
# `bot.user.display_avatar`, so setting the App Icon alone leaves the embed icon
# on Discord's default grey.
AI_ICON_URL: typing.Optional[str] = None

# How long the typing indicator runs before each message the assistant composes.
# Overridable with `.ai set typingdelay`; this is the default.
#
# The two opening disclosures deliberately do not use this — see
# DISCLOSURE_GAP_SECONDS.
TYPING_DELAY_SECONDS = 1.5

# The gap between the two opening disclosures, which are sent with no typing
# indicator at all. They are fixed legal text that was written long before the
# user said anything, so showing them being "typed" is both slow and a small
# lie about what is happening. They go out back to back, just far enough apart
# to read as two messages rather than one.
DISCLOSURE_GAP_SECONDS = 1.0

# The opening line of the greeting. A setting, because it is the first thing
# anyone reads and the one piece of copy most likely to want changing per
# install — "Hola!" in particular is Vueling's voice, not a neutral default.
# {brand} is substituted; a greeting with no placeholder in it is used as-is.
GREETING_OPENER = (
    "Hola! I'm {brand}'s virtual assistant. I'm new and still learning but "
    "there are a lot of things I can do for you."
)

# The question after it. Fixed: it explains how to talk to the assistant rather
# than saying anything about the brand, so there is nothing per-install in it.
GREETING_PROMPT = (
    "How can I help you? Please, try to be as brief as possible so I can "
    "understand you \N{SMILING FACE WITH SMILING EYES}"
)


def greeting_parts(brand: str, opener: str) -> typing.Tuple[str, str]:
    """Sent once when a user opens a new pre-screen conversation.

    Two separate messages, each with its own typing pause, ahead of their first
    message being processed.
    """
    try:
        opening = opener.format(brand=brand)
    except (KeyError, IndexError, ValueError):
        # Someone put a stray brace in a custom greeting. Their words verbatim
        # beat refusing to greet anyone.
        logger.warning("Greeting opener has an unusable placeholder; using it as written.")
        opening = opener
    return (opening, GREETING_PROMPT)


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

# An unanswered confirmation does nothing. Connecting someone to an agent
# because they walked away puts a person on a ticket nobody is sitting at, and
# reads to the user as the bot having pressed yes for them.
HANDOFF_CONFIRM_TIMEOUT_SECONDS = 60

HANDOFF_TIMEOUT_TEXT = (
    "It looks like you're not around at the moment, so I won't put you through to "
    "anyone just yet. Message me whenever you're ready and we'll pick this back up. "
    "\N{SMILING FACE WITH SMILING EYES}"
)

# Sent by the plugin after every answer, rather than left to the model, which
# offered it only sometimes.
FOLLOW_UP_TEXT = "Is there anything else I can help you with? \N{SMILING FACE WITH SMILING EYES}"

# Partnership requests get their own form rather than a conversation, so they
# are matched before anything else: the five questions below are what a human
# would ask anyway, and collecting them up front is better than a ticket that
# starts by asking them one at a time.
# Open-ended suffixes rather than a list of endings: "partnered" and
# "collaborating" were both missed by the enumerated form, and every word
# starting with these stems is the same request.
PARTNERSHIP_PATTERNS = [
    r"\bpartner\w*",
    r"\bcollab\w*",
    r"\baffiliat\w*",
]

_PARTNERSHIP_RE = [re.compile(p, re.IGNORECASE) for p in PARTNERSHIP_PATTERNS]

# "my partner is on the flight" is a passenger, not a business proposal. Only
# the bare noun is ambiguous this way: "our partnership" is still the request.
_PARTNERSHIP_POSSESSIVES = {"my", "your", "his", "her", "their", "our", "a"}
_PARTNERSHIP_AMBIGUOUS = {"partner", "partners"}

PARTNERSHIP_INTRO = (
    "Certainly, we'd be glad to hear about it! Tap the button below and fill in a "
    "few details about your group and I'll pass it straight to the team."
)
# Deliberately still says "vlg" after the rename to `.ai`, as do the survey
# custom_ids below. A custom_id is baked into every button already posted in
# Discord; renaming these would orphan every live partnership post and survey
# invitation, which is a real cost for a string no user ever sees.
PARTNERSHIP_BUTTON_ID = "vlg-partnership"
PARTNERSHIP_BUTTON_LABEL = "Request a partnership"
PARTNERSHIP_MODAL_TITLE = "Partnership request"

# (label, paragraph?, max length). Labels are capped at 45 characters by Discord.
PARTNERSHIP_QUESTIONS = [
    ("Your group's name", False, 100),
    ("Your group's invite", False, 200),
    ("About your group", True, 1000),
    ("What benefits do you see in this partnership?", True, 1000),
    ("What are your expectations from us?", True, 1000),
]

PARTNERSHIP_THANKS = (
    "Thank you! Your application has been sent to the team. Someone will get back "
    "to you here within **2 business days** with a final answer. There's nothing "
    "else you need to do in the meantime."
)
PARTNERSHIP_FAILED = (
    "Sorry, something went wrong sending that to the team and I don't want to tell "
    "you it arrived when it hasn't. Let me put you through to someone instead."
)

# Where submitted applications land. The guild is the first guess only — see
# `_home_guild`, which falls back to Modmail's own configured server, so a fresh
# install elsewhere resolves channels without touching this.
PARTNERSHIP_GUILD_ID = 1532428044822642808
PARTNERSHIP_CHANNEL_ID = 1534233033085948074


def _env_channel_id(name: str, default: int) -> int:
    """A channel id from the environment, falling back rather than raising.

    A typo in `.env` must not take the plugin down at import; `.ai status`
    reports what these actually resolved to.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error("%s is not a channel id (%r); falling back to %s.", name, raw, default)
        return default


# Where staff notifications land: the low-rating alert and the weekly digest.
# Defaults to the partnership channel, which is already known to be visible to
# the bot.
#
# This is now only the *default* for the `staffchannel` setting. It still reads
# the environment so an install that set VLG_STAFF_CHANNEL_ID before there was a
# `.ai set` keeps working untouched, but `.ai set staffchannel` overrides it
# and is the documented way to change it.
STAFF_CHANNEL_ID = _env_channel_id("VLG_STAFF_CHANNEL_ID", PARTNERSHIP_CHANNEL_ID)

# A headline score at or below this raises an alert as soon as it is submitted.
LOW_RATING_THRESHOLD = 2

# The weekly digest fires on the first sweep at or after this hour on this
# weekday, and never twice in the same week. Monday morning UTC by default, so
# it is waiting when the week starts rather than arriving mid-conversation.
DIGEST_WEEKDAY = 0
DIGEST_HOUR_UTC = 9

# Guards against a second digest if a sweep runs twice in the same hour, and
# against a backlog firing if the bot was down over the window.
DIGEST_MIN_GAP = timedelta(days=6)

# Emoji staff react with to claim a partnership lead, and the note added to the
# post once someone has. Reaction-based rather than a button so a claim survives
# any restart with no persistent view to register.
CLAIM_EMOJI = "\N{RAISED HAND}"

# The claim state lives in the post's own footer rather than in the database.
# It is what staff read, it survives restarts for free, and there is no second
# copy that can disagree with what the channel shows.
CLAIM_UNCLAIMED_FOOTER = f"Unclaimed — react with {CLAIM_EMOJI} to take this lead"
CLAIM_CLAIMED_PREFIX = "Claimed by "

# Obvious profanity only. Deliberately short and word-boundary anchored: a false
# positive warns, and then closes the conversation of somebody who did nothing,
# which is far worse than missing a word. Stems cover the common suffixes.
PROFANITY_PATTERNS = [
    r"\bf+u+c+k+\w*",
    r"\bs+h+i+t+\w*",
    r"\bb+i+t+c+h+\w*",
    r"\bc+u+n+t+\w*",
    r"\ba+s+s+h+o+l+e+\w*",
    r"\bd+i+c+k+h+e+a+d+\w*",
    r"\bb+a+s+t+a+r+d+\w*",
    r"\bw+a+n+k+\w*",
    r"\bp+r+i+c+k+s?\b",
    r"\bt+w+a+t+\w*",
    r"\bb+o+l+l+o+c+k+s+\b",
    r"\bn+i+g+g+[ae]+r+\w*",
    r"\bf+a+g+g+o+t+\w*",
    r"\br+e+t+a+r+d+\w*",
]

_PROFANITY_RE = [re.compile(p, re.IGNORECASE) for p in PROFANITY_PATTERNS]

# Sent on the first one. Warm, and explicit about what happens next, so the
# close that may follow is not a surprise.
PROFANITY_WARNING_TEXT = (
    "Let's keep the tone respectful, please. \N{SLIGHTLY SMILING FACE} I'm happy to keep "
    "helping — but if it happens again I'll have to close this chat."
)

# Sent instead of a second warning. The conversation then closes exactly as an
# inactivity close does, survey included.
PROFANITY_CLOSING_TEXT = "I did ask that we keep things respectful, so I'm going to close this chat here."

# Swears in one conversation before it is closed. The first is a warning.
PROFANITY_STRIKES = 2

# Signals that someone is angry or in a hurry, used only to tag the ticket for
# staff. Never changes what the user is told, so a false positive costs nothing
# beyond a misleading tag.
URGENCY_PATTERNS = [
    r"\b(?:urgent|urgently|asap|emergency|immediately|right\s+now)\b",
    r"\b(?:unacceptable|ridiculous|disgraceful|appalling|outrageous)\b",
    r"\b(?:furious|livid|angry|fed\s+up|sick\s+of|disgusted)\b",
    r"\b(?:complain|complaint|complaining)\b",
    r"\b(?:lawyer|legal\s+action|refund\s+now|charge\s?back)\b",
    r"\b(?:still\s+waiting|no\s+one\s+has\s+(?:replied|answered|helped))\b",
]

_URGENCY_RE = [re.compile(p, re.IGNORECASE) for p in URGENCY_PATTERNS]


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
#
# Everything below is set from inside Discord and stored in the plugin
# partition, so somebody installing this configures it by talking to the bot
# rather than by editing this file or an `.env` on the box. It follows the same
# pattern the verbose toggle already used: one document per setting under
# TYPE_META, read into a cache on load.
#
# The constants above are the defaults. An unset setting reads its constant, so
# an existing install behaves exactly as it did before anything was set, and
# `.ai set <key> default` puts it back.
#
# Real credentials — the bot token, the Groq key, the Mongo URI — stay in
# `.env`. Those are secrets rather than settings, and nothing here touches them.

# Accepted spellings for a boolean. Anything else is rejected rather than
# guessed at: silently reading an unrecognised word as "off" would turn a
# feature off while telling the user it was on.
_TRUE_WORDS = {"on", "yes", "true", "enable", "enabled", "y", "1"}
_FALSE_WORDS = {"off", "no", "false", "disable", "disabled", "n", "0"}

# Matches "<#123>" as well as a bare id.
_CHANNEL_MENTION_RE = re.compile(r"^<#(\d+)>$")


def _looks_like_unicode_emoji(raw: str) -> bool:
    """A rough check that something is a single unicode emoji.

    Deliberately loose. Discord accepts far more than any list here would cover,
    and the cost of being wrong is only a rejected setting, so this rules out
    obvious mistakes — a word, a sentence — rather than trying to be exhaustive.
    """
    if len(raw) > 8 or not raw:
        return False
    return not raw.isascii()


class Setting:
    """One configurable setting: how to parse it, how to show it, its default.

    `default` is read at call time from the module constant rather than copied
    here, so the two cannot drift apart.
    """

    def __init__(
        self,
        key: str,
        kind: str,
        default: typing.Callable[[], typing.Any],
        summary: str,
        *,
        minimum: typing.Optional[float] = None,
        maximum: typing.Optional[float] = None,
        unit: str = "",
        example: str = "",
    ):
        self.key = key
        self.kind = kind
        self._default = default
        self.summary = summary
        self.minimum = minimum
        self.maximum = maximum
        self.unit = unit
        self.example = example

    @property
    def default(self) -> typing.Any:
        return self._default()

    @property
    def is_feature(self) -> bool:
        return self.kind == "bool"

    def parse(self, raw: str, guild: typing.Optional[discord.Guild]) -> typing.Any:
        """Turn what someone typed into a stored value, or raise ValueError.

        The ValueError message is shown to whoever typed it, so it says what a
        good value looks like rather than naming the type that failed.
        """
        raw = raw.strip()

        if self.kind == "bool":
            lowered = raw.lower()
            if lowered in _TRUE_WORDS:
                return True
            if lowered in _FALSE_WORDS:
                return False
            raise ValueError("say `on` or `off`.")

        if self.kind in ("text", "url", "emoji"):
            if not raw:
                raise ValueError("that cannot be empty.")
            if self.maximum is not None and len(raw) > self.maximum:
                raise ValueError(f"keep that under {int(self.maximum)} characters.")
            if self.kind == "url" and not raw.lower().startswith(("http://", "https://")):
                raise ValueError("that needs to be a full link starting with `https://`.")
            if self.kind == "emoji" and not (_CUSTOM_EMOJI_RE.match(raw) or _looks_like_unicode_emoji(raw)):
                raise ValueError("send a single emoji, or a custom one like `<:yes:123>`.")
            return raw

        if self.kind == "channel":
            mention = _CHANNEL_MENTION_RE.match(raw)
            if mention:
                return int(mention.group(1))
            if raw.isdigit():
                return int(raw)
            # A plain name, with or without the leading hash. Only resolvable
            # against the server the command was run in.
            if guild is not None:
                wanted = raw.lstrip("#").casefold()
                for channel in guild.text_channels:
                    if channel.name.casefold() == wanted:
                        return channel.id
            raise ValueError("mention a channel like `#staff`, or paste its id.")

        try:
            value = int(raw) if self.kind == "int" else float(raw)
        except ValueError:
            raise ValueError("that needs to be a number.") from None

        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"that cannot be below `{self._number(self.minimum)}`.")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"that cannot be above `{self._number(self.maximum)}`.")
        return value

    @staticmethod
    def _number(value: float) -> str:
        """Render a number without a trailing `.0` on whole values."""
        return f"{value:g}"

    def render(self, value: typing.Any) -> str:
        """The stored value, written for someone reading the settings list."""
        if self.kind == "bool":
            return "on" if value else "off"
        if self.kind == "channel":
            return f"<#{value}>"
        if self.kind == "emoji":
            # Shown as the emoji itself; a custom one renders, a dead id does not,
            # which is the fastest way to see that it is wrong.
            return str(value)
        if self.kind in ("text", "url"):
            return f"`{truncate(str(value), 80)}`"
        return f"`{self._number(value)}`{self.unit}"


# Settings that carry a value. These are what `.ai set` lists and changes.
#
# The identity block comes first on purpose: it is what someone installing this
# on a different bot has to change, and listing it above the tuning knobs makes
# that the obvious first move.
VALUE_SETTINGS = (
    Setting(
        "brandname",
        "text",
        lambda: BRAND_NAME,
        "Your organisation's name, as users see it. Used in the opening privacy "
        "notice, the greeting, and what the AI is told it works for.",
        maximum=60,
        example="Vueling",
    ),
    Setting(
        "assistantname",
        "text",
        lambda: ASSISTANT_NAME,
        "What the assistant calls itself. Shown on every message it sends and in "
        "the goodbye when a chat ends.",
        maximum=60,
        example="Vueling AI",
    ),
    Setting(
        "privacyurl",
        "url",
        lambda: PRIVACY_POLICY_URL,
        "The privacy policy linked in the opening notice. This is the only route "
        "a user has to exercise their data rights, so point it at a real page.",
        maximum=300,
        example="https://example.com/privacy",
    ),
    Setting(
        "ticketprefix",
        "text",
        lambda: TICKET_PREFIX,
        "The prefix on ticket references, e.g. `VLG` gives `VLG-A3K9PQ`. Existing "
        "references keep the prefix they were issued with.",
        maximum=10,
        example="VLG",
    ),
    Setting(
        "greeting",
        "text",
        lambda: GREETING_OPENER,
        "The first thing the assistant says. Write `{brand}` where you want your "
        "organisation's name to appear.",
        maximum=400,
    ),
    Setting(
        "yesemoji",
        "emoji",
        lambda: BUTTON_YES_EMOJI,
        "The emoji on the 'yes, connect me to a human' button. A custom emoji only "
        "works if this bot is in the server that owns it.",
        maximum=60,
    ),
    Setting(
        "noemoji",
        "emoji",
        lambda: BUTTON_NO_EMOJI,
        "The emoji on the 'no thanks' button, same rules as above.",
        maximum=60,
    ),
    Setting(
        "staffchannel",
        "channel",
        lambda: STAFF_CHANNEL_ID,
        "Where the weekly digest and low-rating alerts are posted.",
        example="#staff-alerts",
    ),
    Setting(
        "partnershipchannel",
        "channel",
        lambda: PARTNERSHIP_CHANNEL_ID,
        "Where submitted partnership applications are posted for staff to claim.",
        example="#partnership-leads",
    ),
    Setting(
        "retentiondays",
        "int",
        lambda: TRANSCRIPT_RETENTION_DAYS,
        "How many days a conversation with the assistant is kept before it is "
        "deleted automatically. Your privacy policy should say the same number.",
        minimum=1,
        maximum=365,
        unit=" days",
    ),
    Setting(
        "typingdelay",
        "float",
        lambda: TYPING_DELAY_SECONDS,
        "How long the assistant shows a typing indicator before each message. "
        "Higher feels more human, lower feels quicker.",
        minimum=0,
        maximum=10,
        unit="s",
    ),
    Setting(
        "lowratingthreshold",
        "int",
        lambda: LOW_RATING_THRESHOLD,
        "A survey score of this or below raises an alert in the staff channel. "
        "Only used when low-rating alerts are on.",
        minimum=1,
        maximum=5,
    ),
    Setting(
        "profanitystrikes",
        "int",
        lambda: PROFANITY_STRIKES,
        "How many times someone can swear in one chat before it is closed. The "
        "first one is a warning. Only used when profanity moderation is on.",
        minimum=1,
        maximum=10,
    ),
    Setting(
        "inactivitywarning",
        "int",
        lambda: int(INACTIVITY_WARNING_AFTER.total_seconds() // 60),
        "How many quiet minutes before the assistant asks whether the user is " "still there.",
        minimum=1,
        maximum=10080,
        unit=" min",
    ),
    Setting(
        "inactivityclose",
        "int",
        lambda: int(INACTIVITY_CLOSE_AFTER.total_seconds() // 60),
        "How many quiet minutes before the assistant closes the chat. Should be "
        "larger than the warning above.",
        minimum=1,
        maximum=10080,
        unit=" min",
    ),
    Setting(
        "digestday",
        "int",
        lambda: DIGEST_WEEKDAY,
        "Which day the weekly digest is posted: 0 is Monday, 6 is Sunday.",
        minimum=0,
        maximum=6,
    ),
    Setting(
        "digesthour",
        "int",
        lambda: DIGEST_HOUR_UTC,
        "What hour (UTC, 24-hour clock) the weekly digest is posted.",
        minimum=0,
        maximum=23,
    ),
)

# Anything that is simply on or off. These are what `.ai features` lists.
# Wording is for whoever runs the bot, not for whoever wrote it: each line says
# what turning it off actually stops happening.
FEATURE_SETTINGS = (
    Setting(
        "survey",
        "bool",
        lambda: True,
        "Ask the user to rate the chat after it ends. Turning this off also "
        "stops the training question and low-rating alerts, since both come "
        "from the survey.",
    ),
    Setting(
        "training",
        "bool",
        lambda: True,
        "Ask the user, in that survey, whether their chat may be kept to improve "
        "the AI. Off means the question is never asked and no chat is ever "
        "stored for training.",
    ),
    Setting(
        "profanity",
        "bool",
        lambda: True,
        "Warn someone who swears at the assistant, and close the chat if they " "keep going.",
    ),
    Setting(
        "lowratingalerts",
        "bool",
        lambda: True,
        "Post an alert in the staff channel as soon as someone leaves a bad " "survey score.",
    ),
    Setting(
        "digest",
        "bool",
        lambda: True,
        "Post a weekly summary in the staff channel of the questions the "
        "assistant could not answer, so you know what to add to the FAQ.",
    ),
    Setting(
        "partnershipform",
        "bool",
        lambda: True,
        "Offer a partnership application form when someone asks about "
        "partnering or collaborating. Off means those go to a human like any "
        "other question.",
    ),
    Setting(
        "urgencytags",
        "bool",
        lambda: True,
        "Tag a ticket as urgent when the user sounds angry or in a hurry. Only "
        "staff see the tag; it never changes what the user is told.",
    ),
    Setting(
        "verbose",
        "bool",
        lambda: False,
        "Write extra diagnostics to the bot log, including message content. For "
        "debugging only — leave this off in normal use.",
    ),
)

# Stored like any other setting, but deliberately in neither list above, so it
# appears in neither `.ai set` nor `.ai features`. Turning developer tooling on
# should require knowing it exists; putting it in the menu that advertises every
# other toggle would defeat the point of gating anything behind it.
DEVMODE_SETTING = Setting(
    "devmode",
    "bool",
    lambda: False,
    "Show the developer commands in the command list.",
)

SETTINGS: typing.Dict[str, Setting] = {
    s.key: s for s in VALUE_SETTINGS + FEATURE_SETTINGS + (DEVMODE_SETTING,)
}


# ----------------------------------------------------------------------
# Command listing
# ----------------------------------------------------------------------
#
# What a bare `.ai` prints. Grouped by what someone is trying to do rather than
# alphabetically, and each line says what the command is *for* — the audience is
# a person who has just installed this and has had nothing explained to them.
#
# Kept next to the settings rather than beside the commands because it is the
# same kind of thing: the plugin's front door, written for a stranger.
COMMAND_CATEGORIES = (
    (
        "General",
        (
            ("status", "Is everything wired up and working? Start here when something looks wrong."),
            ("version", "Which version of the plugin is actually running right now."),
            ("ask", "Try a question against the assistant privately, without opening a real chat."),
        ),
    ),
    (
        "Settings",
        (
            ("set", "Change a setting that has a value — channels, timings, your organisation's name."),
            ("features", "Turn optional features on and off."),
        ),
    ),
    (
        "Reports",
        (
            ("stats", "How well the assistant is doing, and what to add to the FAQ next."),
            ("digest", "Send this week's summary to the staff channel now, instead of waiting."),
        ),
    ),
    (
        "User data",
        (
            ("forget", "Delete everything stored about one user. Use this for erasure requests."),
            ("training", "Read the chats users agreed could be kept to improve the assistant."),
            ("ticket", "Look up a ticket reference and find its transcript."),
        ),
    ),
    (
        "Developer",
        (("verbose", "Log why the assistant handed a chat over. Writes message content to the log."),),
    ),
)

# Listed only while devmode is on — internal debugging rather than day-to-day
# running. See `_command_visible` for the exception that keeps a *running* tool
# on screen regardless.
DEV_COMMANDS = frozenset({"verbose"})

# Never listed, at all. `devmode` is deliberately undiscoverable: it is the
# switch you have to have been told about.
HIDDEN_COMMANDS = frozenset({"devmode"})

assert DEV_COMMANDS <= {
    name for _, entries in COMMAND_CATEGORIES for name, _ in entries
}, "a dev command is missing from the listing, so devmode could never reveal it"

# TYPE_META also holds bookkeeping that is not a setting. Reserved so a future
# setting cannot be given a key that would overwrite one of them.
_RESERVED_META_KEYS = {"user_id_salt", "last_digest_at"}
assert not (SETTINGS.keys() & _RESERVED_META_KEYS), "a setting key collides with stored bookkeeping"

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


def build_system_prompt(brand: str) -> str:
    """The system prompt, with the brand name substituted in.

    Built per call rather than at import because `brandname` is a setting: a
    module-level constant would freeze whatever the brand was when the plugin
    loaded and quietly ignore a later rename.
    """
    return f"""\
You are the first-line automated support assistant for {brand}, a virtual
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
however accurate it is. Warm the delivery: a friendly opening clause, an
acknowledgement that it is not what they hoped for. Never soften the policy
itself — say it plainly, then be kind about it. Asked about uniform, something
like "That one's staff-only I'm afraid, so I can't share the details here."

Do NOT end your reply by asking whether there is anything else you can help
with. That question is added automatically after every answer you give, so
writing it yourself means the user is asked it twice in a row.

Reference information:
{FAQ_KNOWLEDGE}

Respond with a single json object with exactly these keys:
  "status": one of "answered", "chat", "unclear", "escalate"
  "reply": either a string, or an array of two strings

Use the array form whenever the answer runs to more than about three sentences.
A long answer arriving as one block is hard to read on a phone, which is where
most of these are read, so break it into two messages: the direct answer first,
then the detail, the caveat, or the related pointer. Split at a natural pause —
never mid-sentence, and never with a markdown link straddling the two parts.

A single string is for genuinely short replies: a one or two sentence answer, or
anything conversational. If you are unsure which you have, use two.
"""


class PartnershipModal(discord.ui.Modal):
    """The five questions, asked as one form instead of five turns."""

    def __init__(self, cog: "NorwegianSupport"):
        super().__init__(title=PARTNERSHIP_MODAL_TITLE, timeout=None)
        self.cog = cog
        self.answers: typing.List[discord.ui.TextInput] = []
        for label, paragraph, max_length in PARTNERSHIP_QUESTIONS:
            field = discord.ui.TextInput(
                label=label,
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                required=True,
                max_length=max_length,
            )
            self.add_item(field)
            self.answers.append(field)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.submit_partnership(interaction, self.answers)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error("Partnership modal failed for %s.", interaction.user, exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(PARTNERSHIP_FAILED, ephemeral=True)


class PartnershipButton(discord.ui.Button):
    def __init__(self, cog: "NorwegianSupport"):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=PARTNERSHIP_BUTTON_LABEL,
            custom_id=PARTNERSHIP_BUTTON_ID,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PartnershipModal(self.cog))


class PartnershipView(discord.ui.View):
    """Persistent: the form has to still open the next day, and after a restart."""

    def __init__(self, cog: "NorwegianSupport"):
        super().__init__(timeout=None)
        self.add_item(PartnershipButton(cog))


class SurveyModal(discord.ui.Modal):
    """The survey: three 1-5 scores, and whether the chat may be reused.

    Every answer is a dropdown rather than free text. discord.py 2.6 allows a
    Select inside a Modal only when wrapped in a Label, which is also what
    carries the question and its scale caption — a Select's own placeholder is
    neither, and is only ever visible before an answer is picked.
    """

    def __init__(self, cog: "NorwegianSupport", transcript_id: str, source: typing.Optional[discord.Message]):
        super().__init__(title=SURVEY_MODAL_TITLE, timeout=None)
        self.cog = cog
        self.transcript_id = transcript_id
        self.source = source

        # Rating questions first, in a fixed order, then the one question that
        # is not a rating. Keeping the yes/no last lets the scale questions read
        # as a single block instead of being interrupted by a different shape of
        # answer, and puts the question with a consequence at the end.
        self.ratings: typing.Dict[str, discord.ui.Select] = {}
        for key, label, description, scale in SURVEY_RATING_QUESTIONS:
            select = discord.ui.Select(
                custom_id=f"vlg-survey-{key}",
                placeholder="Choose a score",
                options=survey_rating_options(scale),
                required=True,
            )
            self.add_item(discord.ui.Label(text=label, description=description, component=select))
            self.ratings[key] = select

        # Asked only when training collection is on. Off means the question is
        # never put to the user at all, rather than asked and then ignored —
        # asking permission for something that cannot happen is worse than not
        # asking, and `on_submit` reads a missing component as a no.
        self.training: typing.Optional[discord.ui.Select] = None
        if cog.setting("training"):
            self.training = discord.ui.Select(
                custom_id="vlg-survey-training",
                placeholder="Yes or no",
                options=[
                    discord.SelectOption(label="Yes, use it to improve the AI", value="yes"),
                    discord.SelectOption(label="No, do not keep it", value="no"),
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

    async def on_submit(self, interaction: discord.Interaction):
        ratings = {}
        for key, select in self.ratings.items():
            try:
                ratings[key] = int(select.values[0])
            except (IndexError, ValueError):
                # Every rating is required, so an unusable value here means a
                # malformed payload rather than a question someone skipped.
                logger.error("Survey submitted without a usable %s score: %r", key, select.values)
                with contextlib.suppress(discord.HTTPException):
                    await interaction.response.send_message(SURVEY_FAILED, ephemeral=True)
                return

        consented = (
            self.training is not None and bool(self.training.values) and self.training.values[0] == "yes"
        )
        await self.cog.record_survey(
            interaction,
            transcript_id=self.transcript_id,
            ratings=ratings,
            consented=consented,
            source=self.source,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error("Survey modal failed for %s.", interaction.user, exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            if not interaction.response.is_done():
                await interaction.response.send_message(SURVEY_FAILED, ephemeral=True)


class SurveyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vlg:survey:(?P<transcript_id>[0-9a-f]{24})",
):
    """The button on the survey invitation.

    A dynamic item rather than a member of a persistent view, because each
    button belongs to one specific conversation. The transcript is encoded in
    the custom_id and parsed back out on click, so a survey offered overnight
    still opens after a restart with nothing held in memory.
    """

    def __init__(self, transcript_id: str):
        self.transcript_id = transcript_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=SURVEY_BUTTON_LABEL,
                custom_id=f"vlg:survey:{transcript_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["transcript_id"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("NorwegianSupport")
        if cog is None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(SURVEY_FAILED, ephemeral=True)
            return

        if await cog.survey_already_answered(interaction, self.transcript_id):
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(SURVEY_ALREADY_ANSWERED, ephemeral=True)
            return

        await interaction.response.send_modal(SurveyModal(cog, self.transcript_id, interaction.message))


def survey_view(transcript_id: str) -> discord.ui.View:
    """A view carrying just the survey button for one conversation."""
    view = discord.ui.View(timeout=None)
    view.add_item(SurveyButton(transcript_id))
    return view


class ChoiceButton(discord.ui.Button):
    """A yes/no button that always acknowledges the click.

    Replaces Modmail's AcceptButton/DenyButton here. Those answer the
    interaction *only* by editing the message to drop the view, so any failure
    of that edit leaves the interaction unacknowledged and Discord tells the
    user "Interaction failed" — which is what happened on every handoff
    confirmation, because the buttons sit on a message with nothing else in it
    and the edit would have emptied it (see BUTTON_SPACER).

    BUTTON_SPACER stops that edit from failing in the first place. This is the
    second layer: whatever happens to the edit, the click is acknowledged, and
    the choice is recorded before anything that can raise is attempted.
    """

    def __init__(self, custom_id: str, emoji: str, value: bool):
        super().__init__(style=discord.ButtonStyle.gray, emoji=emoji, custom_id=custom_id)
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        # First, so a failure below cannot lose a choice the user did make.
        if view is not None:
            view.value = self.value

        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            logger.error(
                "Could not clear the choice buttons; acknowledging the click instead.", exc_info=True
            )
            with contextlib.suppress(discord.HTTPException):
                if not interaction.response.is_done():
                    # A bare acknowledgement. The buttons stay on screen, but
                    # the click registered and the conversation moves on, which
                    # is far better than the user being told it failed.
                    await interaction.response.defer()
        finally:
            if view is not None:
                view.stop()


class YesNoView(discord.ui.View):
    """Yes/no view for the handoff confirmation.

    Shaped like Modmail's own ConfirmThreadCreationView, but that class hardcodes
    timeout=30 in __init__ with no parameter to override.
    """

    def __init__(self, timeout: float):
        super().__init__(timeout=timeout)
        self.value = None

    def with_choices(self, yes_emoji: str, no_emoji: str) -> "YesNoView":
        self.add_item(ChoiceButton("nas-handoff-yes", yes_emoji, True))
        self.add_item(ChoiceButton("nas-handoff-no", no_emoji, False))
        return self


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
        # Stored settings, by key. Only keys that have actually been set appear
        # here; anything missing falls back to its default, so this starting
        # empty is the same as everything being at its default.
        self._settings: typing.Dict[str, typing.Any] = {}
        self._loaded_at: typing.Optional[datetime] = None
        # user_id -> {summary, reason, urgent}, handed to on_thread_ready once
        # the channel exists.
        self._pending_handoff: typing.Dict[int, dict] = {}
        self._partnership_view: typing.Optional[PartnershipView] = None
        # Serialises claim checks; see on_raw_reaction_add.
        self._claim_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        self._loaded_at = datetime.now(timezone.utc)
        self._install_dm_hook()
        self.bot.loop.create_task(self._ensure_indexes())
        self.bot.loop.create_task(self._load_settings())
        # Kept so cog_unload can stop it. A persistent view outlives the cog
        # otherwise, and after a reload the old one would still be serving form
        # buttons through the previous, unhooked instance.
        self._partnership_view = PartnershipView(self)
        self.bot.add_view(self._partnership_view)
        # Registered on the client rather than as a view: each survey button
        # belongs to one conversation, so there is no single view to keep.
        self.bot.add_dynamic_items(SurveyButton)
        self.maintenance_sweep.start()

    async def _load_settings(self) -> None:
        """Restore every stored setting into the cache.

        Held in the partition rather than memory: a plugin reload builds a new
        cog instance and a restart loses the old one, so an in-memory flag
        silently reverts to its default exactly when someone is relying on it.

        Read once here rather than per use, because these are read on the hot
        path — every message the assistant sends checks the typing delay — and a
        database round trip per send is not worth it for values that only change
        when somebody runs a command.
        """
        await self.bot.wait_for_connected()
        try:
            docs = await self.db.find({"_type": TYPE_META, "key": {"$in": list(SETTINGS)}}).to_list(
                length=len(SETTINGS)
            )
        except Exception:
            logger.error("Could not read stored settings; using defaults.", exc_info=True)
            return

        for doc in docs:
            key = doc.get("key")
            if key in SETTINGS and doc.get("value") is not None:
                self._settings[key] = doc["value"]

        if self._settings:
            logger.info(
                "Restored %s stored setting(s): %s.",
                len(self._settings),
                ", ".join(sorted(self._settings)),
            )
        if self.setting("verbose"):
            logger.info(
                "Verbose diagnostics are ON (restored). Turn off with %s off.",
                self._cmd("verbose"),
            )

        # Settings arrive after the cog is added, so the decorators' own
        # `hidden` values are whatever the class declared. Reconcile them now.
        self._apply_devmode_visibility()

    def setting(self, key: str) -> typing.Any:
        """The current value of a setting, or its default when unset."""
        if key in self._settings:
            return self._settings[key]
        return SETTINGS[key].default

    def _cmd(self, sub: str = "") -> str:
        """How to type a command in this plugin, for use in user-facing text.

        Always the primary name, never the alias someone happened to type, so
        the help text teaches one name rather than echoing whichever legacy one
        is still in muscle memory.
        """
        return f"{self.bot.prefix}{GROUP_NAME}{' ' + sub if sub else ''}"

    async def set_setting(self, key: str, value: typing.Any) -> None:
        """Store a setting and update the cache.

        The cache is only updated once the write succeeds, so a failed write
        leaves the bot behaving the way `.ai set` will still report.
        """
        await self.db.update_one(
            {"_type": TYPE_META, "key": key},
            {"$set": {"value": value}},
            upsert=True,
        )
        self._settings[key] = value

    async def clear_setting(self, key: str) -> None:
        """Put a setting back to its default by forgetting it entirely.

        Deleted rather than rewritten with the default, so a later change to the
        default in code reaches installs that never overrode it.
        """
        await self.db.delete_one({"_type": TYPE_META, "key": key})
        self._settings.pop(key, None)

    @property
    def _verbose(self) -> bool:
        """Kept as an attribute because `_diag` reads it on every deferral."""
        return bool(self.setting("verbose"))

    async def cog_unload(self) -> None:
        self._remove_dm_hook()
        self.maintenance_sweep.cancel()
        if self._partnership_view is not None:
            self._partnership_view.stop()
            self._partnership_view = None
        try:
            self.bot.remove_dynamic_items(SurveyButton)
        except Exception:
            logger.debug("Could not unregister the survey button.", exc_info=True)

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
        try:
            await self._maybe_send_digest()
        except Exception:
            logger.error("Weekly digest check failed.", exc_info=True)

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
        warn_after = timedelta(minutes=self.setting("inactivitywarning"))
        close_after = timedelta(minutes=self.setting("inactivityclose"))

        # Closing is handled before warning, so a conversation idle past both
        # thresholds closes rather than being warned and closed a tick later.
        stale = await self.db.find(
            {"_type": TYPE_SESSION, "last_activity_at": {"$lte": now - close_after}}
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
                "last_activity_at": {"$lte": now - warn_after},
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

    async def _maybe_send_digest(self) -> None:
        """Post the weekly digest once the window comes round.

        Driven off the existing sweep rather than its own timer, and gated on a
        stored timestamp rather than an in-memory one, so a restart mid-week
        neither skips a digest nor sends a second.
        """
        if not self.setting("digest"):
            return

        now = datetime.now(timezone.utc)
        if now.weekday() != self.setting("digestday") or now.hour < self.setting("digesthour"):
            return

        doc = await self.db.find_one({"_type": TYPE_META, "key": "last_digest_at"})
        last = (doc or {}).get("value")
        if isinstance(last, datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < DIGEST_MIN_GAP:
                return
        elif doc is None:
            # First ever run. Seed the clock and send nothing, so loading the
            # plugin on a Monday morning does not fire a digest with a few
            # hours of data in it.
            await self.db.update_one(
                {"_type": TYPE_META, "key": "last_digest_at"},
                {"$set": {"value": now}},
                upsert=True,
            )
            logger.info("Weekly digest scheduled; the first one goes out next week.")
            return

        # Stamped before sending. A send that fails must not retry on every
        # sweep for the rest of the day.
        await self.db.update_one(
            {"_type": TYPE_META, "key": "last_digest_at"},
            {"$set": {"value": now}},
            upsert=True,
        )

        embed = await self._digest_embed()
        if embed is None:
            logger.info("Weekly digest skipped: nothing on record.")
            return

        if await self._post_to_staff(embed, what="weekly digest") is not None:
            logger.info("Weekly digest posted.")

    async def _digest_embed(self) -> typing.Optional[discord.Embed]:
        """The digest body, or None when there is nothing worth sending."""
        transcripts = await self.db.find({"_type": TYPE_TRANSCRIPT}).to_list(length=2000)
        if not transcripts:
            return None

        gaps = self._faq_gaps(transcripts)
        embed = self._embed(
            title="Weekly digest — what the assistant could not answer",
            description=(
                f"**{gaps['total']}** conversation(s) in the last "
                f"{self.setting('retentiondays')} days. "
                f"**{gaps['deferred']}** ended with the assistant unable to answer "
                f"(**{gaps['deferral_rate']:.0f}%** of finished conversations).\n\n"
                "Each FAQ entry written for something below removes a handoff."
            ),
        )
        await self._add_rating_field(embed)
        self._add_gap_fields(embed, gaps)
        embed.set_footer(text=f"Runs weekly • {self._cmd()} stats for this on demand")
        return embed

    def _home_guild(self) -> typing.Optional[discord.Guild]:
        """The server staff channels are looked up in.

        PARTNERSHIP_GUILD_ID names one specific server, which is right for the
        install this was written for and meaningless on any other, so it is only
        the first guess. Modmail's own configured guild is the fallback, which is
        the correct answer for a fresh install and needs nothing set.
        """
        return self.bot.get_guild(PARTNERSHIP_GUILD_ID) or getattr(self.bot, "guild", None)

    def _guild_channel(self, channel_id: int):
        """A staff channel by id, or None if this bot cannot see it.

        Tries the home guild first so an id that also exists elsewhere cannot be
        resolved to the wrong server, then falls back to a global lookup for a
        channel in a guild neither of those names.
        """
        try:
            guild = self._home_guild()
            channel = guild.get_channel(channel_id) if guild else None
            return channel or self.bot.get_channel(channel_id)
        except Exception:
            logger.error("Could not resolve channel %s.", channel_id, exc_info=True)
            return None

    async def _post_to_staff(self, embed: discord.Embed, *, what: str) -> typing.Optional[discord.Message]:
        """Send one notification to the staff channel. None if it did not land."""
        channel_id = self.setting("staffchannel")
        channel = self._guild_channel(channel_id)
        if channel is None:
            logger.error(
                "Staff channel %s is not visible to this bot, so the %s was not sent. " "Set one with %s.",
                channel_id,
                what,
                self._cmd("set staffchannel"),
            )
            return None
        try:
            return await channel.send(embed=embed)
        except discord.HTTPException:
            logger.error("Could not post the %s to the staff channel.", what, exc_info=True)
            return None

    async def _alert_low_rating(
        self, transcript_id: ObjectId, ratings: typing.Dict[str, int], *, consented: bool
    ) -> None:
        """Tell staff about a bad score while it is still worth acting on.

        Carries the scores and, when the user agreed to it, the conversation
        itself — without that consent there is a rating and nothing to read,
        which is the whole reason the survey asks.
        """
        headline = ratings.get(SURVEY_HEADLINE_KEY)
        embed = self._embed(
            title=f"Low rating — {headline}/5",
            description="A conversation was just rated poorly.",
            color=self.bot.error_color,
        )
        embed.add_field(
            name="Scores",
            value="\n".join(
                f"**{ratings[key]}**/5 — {label}"
                for key, label, _, _ in SURVEY_RATING_QUESTIONS
                if key in ratings
            )
            or "none recorded",
            inline=False,
        )

        if consented:
            training = await self.db.find_one({"_type": TYPE_TRAINING, "transcript_id": transcript_id})
            if training is not None:
                embed.add_field(
                    name="What was said",
                    value=self._training_sample_body(training),
                    inline=False,
                )
                embed.set_footer(text="Kept for review, so this conversation can be read in full")
        else:
            embed.set_footer(
                text="The user did not agree to us keeping this chat, so there is nothing to read"
            )

        await self._post_to_staff(embed, what="low-rating alert")

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
            # Survey answers and their training copies are found by conversation.
            await self.db.create_index([("_type", 1), ("transcript_id", 1)])
            await self.db.create_index([("_type", 1), ("consented_at", -1)])
            # TTL for pre-screen transcripts only. Documents lacking `expires_at`
            # — sessions, ticket mappings, survey answers and the conversations
            # kept for review — are never touched by the TTL monitor.
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
            await asyncio.sleep(self.setting("typingdelay"))
        return await channel.send(embed=embed, view=view) if view else await channel.send(embed=embed)

    @staticmethod
    async def _send_plain(channel, embed: discord.Embed):
        """Send with no typing indicator and no pause before it.

        For text the assistant is not composing: the opening disclosures are
        fixed and were written long before this conversation started, so showing
        them being typed is both slower than it needs to be and a small
        misrepresentation of what is happening.
        """
        return await channel.send(embed=embed)

    async def _send_buttons(self, channel, view: discord.ui.View):
        """Send a message carrying only the buttons. None if it could not go out.

        The spacer is always sent, never as a fallback: see BUTTON_SPACER. A
        components-only message goes out fine and is the reason clicking either
        handoff button used to fail, because the reply to the click is an edit
        that would have left the message empty.
        """
        async with safe_typing(channel):
            await asyncio.sleep(self.setting("typingdelay"))
        try:
            return await channel.send(content=BUTTON_SPACER, view=view)
        except discord.HTTPException:
            logger.debug("Buttons could not be sent at all.", exc_info=True)
            return None

    async def _send_conversation_opening(self, channel) -> None:
        """Disclosure then greeting, at the start of every conversation.

        The disclosure is informational and repeats every time rather than being
        shown once and remembered: processing rests on the contractual
        relationship, not consent, so there is no per-user decision to store and
        nothing to look up. Nothing here waits for input.

        The two halves are paced differently on purpose. The disclosures are
        fixed text and go out back to back with no typing indicator, because
        making somebody watch a bot pretend to compose a privacy notice is slow
        and faintly dishonest. The greeting that follows is the assistant
        actually addressing them, so it gets the typing indicator and the normal
        delay, and so does the question after it.
        """
        for index, part in enumerate(disclosure_parts(self.setting("brandname"), self.setting("privacyurl"))):
            if index:
                # Between the disclosures only, so the first one is immediate.
                await asyncio.sleep(DISCLOSURE_GAP_SECONDS)
            await self._send_plain(channel, self._plain_embed(part))
        for part in greeting_parts(self.setting("brandname"), self.setting("greeting")):
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
    def _profanity_match(text: str) -> typing.Optional[str]:
        for pattern in _PROFANITY_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    @staticmethod
    def _urgency_match(text: str) -> typing.Optional[str]:
        for pattern in _URGENCY_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    async def _handle_profanity(self, message: discord.Message, matched: str) -> bool:
        """Warn, then close. True when the message was dealt with here.

        The count lives on the open transcript, so it resets with the
        conversation exactly as asked — a user who swore last week starts again
        on a clean slate, and someone who swears twice in one chat does not.
        """
        user = message.author
        # Recorded like any other turn first. That keeps the conversation
        # readable afterwards, and means an opening message that swears has a
        # transcript to count against rather than being a special case.
        await self._append_transcript(user.id, message.content or "", status=STATUS_CHAT)

        transcript = await self.db.find_one_and_update(
            self._open_filter(await self._user_hash(user.id)),
            {"$inc": {"profanity_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        count = (transcript or {}).get("profanity_count", 1)
        logger.info("Profanity %r from %s (%s), strike %s.", matched, user, user.id, count)

        if count < self.setting("profanitystrikes"):
            with contextlib.suppress(discord.HTTPException):
                await self._send_with_typing(message.channel, self._ai_embed(PROFANITY_WARNING_TEXT))
            return True

        with contextlib.suppress(discord.HTTPException):
            await self._send_with_typing(message.channel, self._ai_embed(PROFANITY_CLOSING_TEXT))
        # Deliberately the same close as an inactivity timeout: same closing
        # messages, same survey, same fresh start on their next message.
        await self._close_conversation(user.id, message.channel, reason="profanity")
        return True

    @staticmethod
    def _is_closing_request(text: str) -> typing.Optional[str]:
        for pattern in _CLOSING_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    async def _close_conversation(self, user_id: int, channel, *, reason: str) -> None:
        """Send the closing messages and the survey, then end the conversation."""
        try:
            for part in closing_parts(self.setting("assistantname")):
                await self._send_with_typing(channel, self._plain_embed(part))
        except discord.HTTPException:
            # Closing still has to happen, or the user is stuck in a conversation
            # the assistant believes is over.
            logger.error("Could not deliver the closing messages to %s.", user_id, exc_info=True)

        closed = await self._close_transcript(user_id, handed_off=False)
        logger.info("Closed the pre-screen conversation with %s (%s).", user_id, reason)

        # Only the conversation that this call actually closed gets a survey. A
        # None here means something else closed it first, and offering a second
        # survey for the same chat would collect the same answer twice.
        if closed is None or not self.setting("survey"):
            return

        try:
            await self._send_with_typing(
                channel,
                self._plain_embed(SURVEY_INVITE_TEXT),
                view=survey_view(str(closed["_id"])),
            )
        except discord.HTTPException:
            # No survey, but the conversation is correctly closed either way.
            logger.error("Could not offer the survey to %s.", user_id, exc_info=True)

    async def _confirm_handoff(self, message: discord.Message) -> bool:
        """Ask before escalating.

        Returns True to stay with the assistant, False to hand off. An
        unanswered prompt escalates: see HANDOFF_CONFIRM_TIMEOUT_SECONDS.
        """
        user = message.author
        # Modmail's own unlabelled buttons, carrying the guild emoji and nothing
        # else. The consent notice keeps its text labels: an emoji-only choice is
        # fine for "shall I fetch a human", not for accepting a privacy policy.
        yes_emoji, no_emoji = self._button_emoji()
        view = YesNoView(timeout=HANDOFF_CONFIRM_TIMEOUT_SECONDS).with_choices(yes_emoji, no_emoji)

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
                "Handoff buttons rejected for %s, retrying with unicode instead of %s / %s. " "Check `%s`.",
                user,
                self.setting("yesemoji"),
                self.setting("noemoji"),
                self._cmd("status"),
            )
            view = YesNoView(timeout=HANDOFF_CONFIRM_TIMEOUT_SECONDS).with_choices(
                BUTTON_YES_FALLBACK, BUTTON_NO_FALLBACK
            )
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
            # Nobody pressed anything. Connecting them anyway would look like the
            # bot pressed yes on their behalf, and would put a person on a ticket
            # the user has already walked away from.
            try:
                await prompt.edit(view=None)
            except discord.HTTPException:
                logger.debug("Could not clear handoff buttons after timeout.", exc_info=True)
            await self._send_with_typing(message.channel, self._plain_embed(HANDOFF_TIMEOUT_TEXT))
            logger.info("Handoff confirmation timed out for %s (%s); stayed put.", user, user.id)
            return True

        await self._send_with_typing(message.channel, self._plain_embed(HANDOFF_ACCEPTED_TEXT))
        await self._send_with_typing(message.channel, self._plain_embed(HANDOFF_GOODBYE_TEXT))
        return False

    def _resolve_emoji(self, raw: str) -> typing.Optional[discord.Emoji]:
        """The guild emoji `raw` names, if this bot can actually use it.

        `get_emoji` looks the id up in the bot's own emoji cache, which only
        holds emoji from servers it is a member of — which is exactly the
        condition Discord enforces when a component carries one.
        """
        match = _CUSTOM_EMOJI_RE.match(raw.strip())
        if match is None:
            # Plain unicode. Always usable, nothing to resolve.
            return None
        return self.bot.get_emoji(int(match.group(1)))

    def _button_emoji(self) -> typing.Tuple[str, str]:
        """The yes/no pair to put on a confirmation, already checked.

        Discord rejects a component carrying an emoji the bot cannot use and
        that failure takes the whole message with it, so the pair is resolved
        before the send rather than after: the send-and-retry path below is a
        second net, not the mechanism. Falls back as a pair — one guild emoji
        beside one unicode mark looks like a rendering fault.
        """
        for raw in (self.setting("yesemoji"), self.setting("noemoji")):
            if _CUSTOM_EMOJI_RE.match(raw.strip()) and self._resolve_emoji(raw) is None:
                logger.warning(
                    "Confirmation emoji %s is not usable by this bot, so the buttons fall back to "
                    "%s / %s. The bot is not in the server that owns it. Check `%s`.",
                    raw,
                    BUTTON_YES_FALLBACK,
                    BUTTON_NO_FALLBACK,
                    self._cmd("status"),
                )
                return BUTTON_YES_FALLBACK, BUTTON_NO_FALLBACK
        return self.setting("yesemoji"), self.setting("noemoji")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """First staff member to react owns the lead.

        Raw rather than `on_reaction_add` so a post from before the last
        restart still claims — the cached-message variant silently ignores
        anything not in memory, which is most of what this channel holds.
        """
        if str(payload.emoji) != CLAIM_EMOJI:
            return
        if payload.channel_id != self.setting("partnershipchannel"):
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return

        channel = self._guild_channel(payload.channel_id)
        if channel is None:
            return

        # Serialised because the check and the edit are separated by awaits, and
        # two people clicking at the same moment would otherwise both read the
        # post as unclaimed and both be told they had it.
        async with self._claim_lock:
            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.HTTPException:
                return

            if message.author.id != getattr(self.bot.user, "id", None) or not message.embeds:
                return

            embed = message.embeds[0]
            footer = (embed.footer.text or "") if embed.footer else ""
            if footer != CLAIM_UNCLAIMED_FOOTER:
                # Already claimed, or not a claimable post. Take the late
                # reaction back off so the post keeps showing one owner.
                if footer.startswith(CLAIM_CLAIMED_PREFIX):
                    with contextlib.suppress(discord.HTTPException):
                        member = payload.member or await self.bot.fetch_user(payload.user_id)
                        await message.remove_reaction(payload.emoji, member)
                return

            claimer = payload.member or self.bot.get_user(payload.user_id)
            if claimer is None:
                with contextlib.suppress(discord.HTTPException):
                    claimer = await self.bot.fetch_user(payload.user_id)
            if claimer is None:
                return

            name = getattr(claimer, "display_name", None) or str(claimer)
            embed.set_footer(text=f"{CLAIM_CLAIMED_PREFIX}{name}")
            embed.colour = self.bot.main_color

            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                logger.error("Could not mark partnership lead %s claimed.", payload.message_id, exc_info=True)
                return

        logger.info("Partnership lead %s claimed by %s (%s).", payload.message_id, name, payload.user_id)

    @staticmethod
    def _partnership_match(text: str) -> typing.Optional[str]:
        for pattern in _PARTNERSHIP_RE:
            for found in pattern.finditer(text):
                word = found.group(0).lower()
                if word in _PARTNERSHIP_AMBIGUOUS:
                    before = re.findall(r"[a-z']+", text[: found.start()].lower())
                    if before and before[-1] in _PARTNERSHIP_POSSESSIVES:
                        # Someone's travelling companion. Keep looking: a later
                        # match in the same message may still be the request.
                        continue
                return found.group(0)
        return None

    async def _offer_partnership_form(self, message: discord.Message) -> bool:
        """Reply, then a message carrying the form button. True if it went out."""
        try:
            await self._send_with_typing(message.channel, self._ai_embed(PARTNERSHIP_INTRO))
        except discord.HTTPException:
            logger.error("Could not introduce the partnership form.", exc_info=True)
            return False
        sent = await self._send_buttons(message.channel, PartnershipView(self))
        return sent is not None

    async def submit_partnership(
        self, interaction: discord.Interaction, answers: typing.List[discord.ui.TextInput]
    ) -> None:
        """Post an application to the staff channel, then confirm to the user.

        The user is only told it arrived if it actually did. Saying "sent" for
        something that silently failed is worse than saying nothing.
        """
        user = interaction.user
        await interaction.response.defer()

        embed = self._embed(
            title="Partnership request",
            description=f"From {user.mention} (`{user.id}`)",
            color=self.bot.main_color,
        )
        for field, (label, _, _) in zip(answers, PARTNERSHIP_QUESTIONS):
            embed.add_field(name=label, value=truncate(str(field.value).strip() or "—", 1000), inline=False)

        partnership_channel_id = self.setting("partnershipchannel")
        channel = self._guild_channel(partnership_channel_id)

        delivered = False
        if channel is None:
            logger.error(
                "Partnership channel %s is not visible to this bot in %s. "
                "Set one with %s set partnershipchannel.",
                partnership_channel_id,
                self._home_guild(),
                self._cmd(),
            )
        else:
            try:
                embed.set_footer(text=CLAIM_UNCLAIMED_FOOTER)
                posted = await channel.send(embed=embed)
                delivered = True
            except discord.HTTPException:
                logger.error("Could not post the partnership application.", exc_info=True)
            else:
                # Seeded by the bot so staff can claim with one click rather
                # than finding the emoji. Failing here costs the convenience,
                # not the claim: reacting manually works just as well.
                with contextlib.suppress(discord.HTTPException):
                    await posted.add_reaction(CLAIM_EMOJI)

        dm = interaction.channel
        if dm is None:
            return

        if not delivered:
            with contextlib.suppress(discord.HTTPException):
                await self._send_with_typing(dm, self._ai_embed(PARTNERSHIP_FAILED))
            return

        logger.info("Partnership application submitted by %s (%s).", user, user.id)
        with contextlib.suppress(discord.HTTPException):
            await self._send_with_typing(dm, self._ai_embed(PARTNERSHIP_THANKS))
            await self._send_with_typing(dm, self._ai_embed(FOLLOW_UP_TEXT))

    @staticmethod
    def _escalation_match(text: str) -> typing.Optional[str]:
        for pattern in _ESCALATION_RE:
            found = pattern.search(text)
            if found:
                return found.group(0)
        return None

    def _diag(self, msg: str, *args) -> None:
        """Diagnostic line: debug normally, info while `.ai verbose` is on.

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
                    "expires_at": now + timedelta(days=self.setting("retentiondays")),
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

    async def _flag_urgency(self, user_id: int) -> None:
        """Mark the conversation as sounding angry or urgent.

        Separate from `handoff_reason` because it is orthogonal to it: someone
        can be furious *and* asking for a partnership, and staff want to see
        both on the ticket.
        """
        await self.db.update_one(
            self._open_filter(await self._user_hash(user_id)),
            {"$set": {"urgent": True}},
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

    async def _close_transcript(self, user_id: int, *, handed_off: bool) -> typing.Optional[dict]:
        """End the pre-screen conversation, returning the transcript it closed.

        Without this the transcript stays open forever: the next conversation
        would skip its disclosure and replay stale history back to Groq.

        `handed_off_at` is still stamped separately when a human took over, so
        "was this escalated" stays answerable rather than being flattened into
        "this ended".

        The returned document is what the survey button is keyed to. Closing is
        a single atomic update against the open filter, so a conversation the
        inactivity sweep and a goodbye both reach is only ever closed — and so
        only ever surveyed — once.
        """
        now = datetime.now(timezone.utc)
        changes = {"closed_at": now}
        if handed_off:
            changes["handed_off_at"] = now

        closed = await self.db.find_one_and_update(
            self._open_filter(await self._user_hash(user_id)),
            {"$set": changes},
            return_document=ReturnDocument.AFTER,
        )
        await self.db.delete_one({"_type": TYPE_SESSION, "user_id": user_id})
        return closed

    # ------------------------------------------------------------------
    # Survey answers and the training set
    # ------------------------------------------------------------------

    @staticmethod
    def _survey_filter(transcript_id: ObjectId, user_hash: str) -> dict:
        """One answer per user per conversation.

        Keyed on the submitter as well as the conversation. A custom_id comes
        back from the client, so keying on the conversation alone would let one
        person's submission overwrite the answer stored against another's.
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
            # a repeat answer is caught again on write.
            logger.error("Could not check for an existing survey answer.", exc_info=True)
            return False
        return answered is not None

    async def record_survey(
        self,
        interaction: discord.Interaction,
        *,
        transcript_id: str,
        ratings: typing.Dict[str, int],
        consented: bool,
        source: typing.Optional[discord.Message],
    ) -> None:
        """Store the answers and, on a yes, the copy kept for review."""
        now = datetime.now(timezone.utc)

        # Checked again here, not just when the modal was built. A form opened
        # before collection was turned off is still submittable minutes later,
        # and "never store anything for training" has to hold for that one too.
        if consented and not self.setting("training"):
            logger.info("Training consent ignored: collection is turned off.")
            consented = False

        # Kept alongside the per-question scores so one number still means
        # something without unpacking the dict: it is what `.ai stats` reports,
        # what the low-rating alert fires on, and what answers stored before
        # there was more than one question already hold.
        headline = ratings.get(SURVEY_HEADLINE_KEY)
        user_hash = await self._user_hash(interaction.user.id)

        try:
            oid = ObjectId(transcript_id)
        except Exception:
            logger.error("Survey submitted with an unusable transcript id %r.", transcript_id)
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(SURVEY_FAILED, ephemeral=True)
            return

        transcript = await self.db.find_one({"_type": TYPE_TRANSCRIPT, "_id": oid})

        # The custom_id is client-supplied, so a conversation is only usable
        # once it is shown to belong to whoever pressed the button. Someone
        # else's is refused outright rather than downgraded to a bare rating,
        # which would let them write over the real answer.
        if transcript is not None and transcript.get("user_id_hash") != user_hash:
            logger.warning(
                "%s (%s) submitted a survey for a conversation they do not own.",
                interaction.user,
                interaction.user.id,
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(SURVEY_NOT_YOURS, ephemeral=True)
            return

        stored_training = False
        if consented and transcript is not None:
            await self.db.update_one(
                {"_type": TYPE_TRAINING, "transcript_id": oid, "user_id_hash": user_hash},
                {
                    "$set": {
                        "messages": transcript.get("messages", []),
                        "message_count": len(transcript.get("messages", [])),
                        "ratings": ratings,
                        "rating": headline,
                        "handoff_reason": transcript.get("handoff_reason"),
                        "conversation_started_at": transcript.get("created_at"),
                        "conversation_ended_at": transcript.get("closed_at"),
                        "consented_at": now,
                    }
                },
                # No `expires_at`: this copy is review material for improving the
                # assistant, not a live conversation, so neither the TTL index
                # nor _expire_transcripts touches it.
                upsert=True,
            )
            stored_training = True

        stored_rating = consented or KEEP_RATINGS_WITHOUT_CONSENT
        if stored_rating:
            await self.db.update_one(
                self._survey_filter(oid, user_hash),
                {
                    "$set": {
                        "ratings": ratings,
                        "rating": headline,
                        "training_consent": consented,
                        "answered_at": now,
                    }
                },
                upsert=True,
            )

        # Take the button away so the same survey cannot be reopened from an old
        # message. Losing this is cosmetic; the answer is already stored.
        if source is not None:
            with contextlib.suppress(discord.HTTPException):
                await source.edit(view=None)

        # Each branch says only what happened. A blanket "recorded" is untrue
        # when the conversation had already expired out from under the button.
        if stored_training:
            thanks = SURVEY_THANKS_TRAINING
        elif consented and stored_rating:
            thanks = SURVEY_THANKS_EXPIRED
        elif consented:
            thanks = "Thank you. This conversation is no longer available, so nothing has been kept."
        elif stored_rating:
            thanks = SURVEY_THANKS_RATING
        else:
            thanks = "Thank you for your feedback."

        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(thanks, ephemeral=True)

        logger.info(
            "Survey answered for %s: %s, training consent %s.",
            transcript_id,
            ", ".join(f"{k}={v}" for k, v in ratings.items()) or "no scores",
            consented,
        )
        self._diag(
            "survey %s ratings=%r consent=%s stored=%s", transcript_id, ratings, consented, stored_training
        )

        # Fired last and never allowed to affect the user's reply: they have
        # already been thanked, and a staff notification failing is not their
        # problem.
        if (
            stored_rating
            and self.setting("lowratingalerts")
            and headline is not None
            and headline <= self.setting("lowratingthreshold")
        ):
            try:
                await self._alert_low_rating(oid, ratings, consented=consented)
            except Exception:
                logger.error("Could not raise the low-rating alert.", exc_info=True)

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
        messages = [{"role": "system", "content": build_system_prompt(self.setting("brandname"))}]
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

        replies = replies[:2]

        # The prompt asks for two messages on a long answer and the model obliges
        # only sometimes, so the rule is applied here as well. Only ever to a
        # single long reply: two parts are already what was wanted.
        if len(replies) == 1:
            replies = self._split_long_reply(replies[0])

        return status, replies, raw

    @staticmethod
    def _link_spans(text: str) -> typing.List[typing.Tuple[int, int]]:
        """Character ranges covered by a markdown link, so a split can avoid them."""
        return [match.span() for match in re.finditer(r"\[[^\]]*\]\([^)]*\)", text)]

    @classmethod
    def _split_long_reply(cls, text: str) -> typing.List[str]:
        """Break one long answer into two messages, or leave it alone.

        Returns one or two parts. Splits only at a paragraph break or the end of
        a sentence, preferring whichever candidate sits closest to the middle, so
        the result reads as two deliberate messages rather than a truncation.

        Never splits inside a markdown link: the FAQ answers are full of them and
        half a link in each message renders as neither.
        """
        if len(text) <= REPLY_SPLIT_THRESHOLD:
            return [text]

        spans = cls._link_spans(text)

        def usable(index: int) -> bool:
            # Both halves have to be worth sending on their own.
            if index < REPLY_SPLIT_MIN_PART or len(text) - index < REPLY_SPLIT_MIN_PART:
                return False
            return not any(start < index < end for start, end in spans)

        # A blank line is the author's own break and beats any sentence end.
        # Sentence ends are the fallback, and require the whitespace so a period
        # inside "vuelingrbx.vercel.app" is never mistaken for one.
        candidates = [match.end() for match in re.finditer(r"\n\s*\n", text)]
        if not any(usable(index) for index in candidates):
            candidates = [match.end() for match in re.finditer(r"(?<=[.!?])\s+", text)]

        usable_candidates = [index for index in candidates if usable(index)]
        if not usable_candidates:
            # Nowhere clean to break: one long message beats a mangled pair.
            return [text]

        midpoint = len(text) / 2
        best = min(usable_candidates, key=lambda index: abs(index - midpoint))
        return [text[:best].strip(), text[best:].strip()]

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

        # After the opening, so a first message that swears still gets the
        # disclosure, and before everything else, so no other route can answer
        # a message that is about to be moderated.
        if self.setting("profanity"):
            swore = self._profanity_match(content)
            if swore:
                return await self._handle_profanity(message, swore)

        # Noted for the ticket tag, not acted on. Checked on every message
        # because the temper usually arrives a few turns in, not at the top.
        if self.setting("urgencytags") and self._urgency_match(content):
            await self._flag_urgency(user.id)

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

        # Ahead of the escalation check on purpose: "can we partner?" is a
        # request the form answers better than a human retyping the same five
        # questions, even when it is phrased as wanting to speak to someone.
        partnership = self._partnership_match(content) if self.setting("partnershipform") else None
        if partnership:
            logger.info("Partnership intent %r from %s (%s).", partnership, user, user.id)
            await self._append_transcript(user.id, content, PARTNERSHIP_INTRO, status=STATUS_ANSWERED)
            if await self._offer_partnership_form(message):
                return True
            logger.error("Could not offer the partnership form to %s; handing off.", user)
            # Tagged as the lead it is, not as the error that routed it. A
            # submitted form never opens a thread at all, so this reason only
            # ever appears when the form itself could not be offered — which is
            # exactly the case where staff need to know it was a partnership.
            await self._mark_handoff_reason(user.id, HANDOFF_PARTNERSHIP)
            return False

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
            # Always, rather than leaving it to the model, which offered it only
            # sometimes. The prompt is told not to add its own so it never doubles.
            with contextlib.suppress(discord.HTTPException):
                await self._send_with_typing(message.channel, self._ai_embed(FOLLOW_UP_TEXT))
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
            footer=ai_footer(self.setting("assistantname")),
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
            reference = f'{self.setting("ticketprefix")}-{body}'
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
            summary = await self._summarise(history)
        except Exception:
            logger.error("Preparing the handoff summary failed.", exc_info=True)
            summary = SUMMARY_FALLBACK

        # Why it escalated is read here rather than in on_thread_ready, because
        # the transcript is closed in between and carrying it across is cheaper
        # and less fragile than looking it up again afterwards.
        self._pending_handoff[user.id] = {
            "summary": summary,
            "reason": (transcript or {}).get("handoff_reason"),
            "urgent": bool((transcript or {}).get("urgent")),
        }

    @staticmethod
    def _handoff_tags(pending: dict) -> typing.List[str]:
        """The triage labels for one handoff, most specific first."""
        tags = []
        if pending.get("urgent"):
            tags.append(HANDOFF_URGENCY_TAG)
        tag = HANDOFF_REASON_TAGS.get(pending.get("reason"))
        if tag:
            tags.append(tag)
        return tags

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """Post the summary and hand the user their reference.

        Modmail dispatches this once the channel, genesis message and staff
        mirroring are all in place, so everything below is additive.
        """
        recipient = getattr(thread, "recipient", None)
        if recipient is None:
            return

        pending = self._pending_handoff.pop(recipient.id, None)
        if pending is None:
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

        tags = self._handoff_tags(pending)
        embed = self._embed(
            title="Assistant summary",
            description=pending.get("summary") or SUMMARY_FALLBACK,
            # An angry or urgent ticket is coloured as one, so it stands out in
            # the channel before anyone reads a word of it.
            color=self.bot.error_color if pending.get("urgent") else self.bot.main_color,
            footer=f"Reference {reference}" if reference else "Reference unavailable",
        )
        if tags:
            embed.add_field(name="Triage", value="  ".join(f"**{t}**" for t in tags), inline=False)
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
        embed.set_author(name=self.setting("assistantname"), icon_url=self._bot_avatar())

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

    @commands.group(name=GROUP_NAME, aliases=list(LEGACY_ALIASES), invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai(self, ctx):
        """The AI support assistant: settings, reports and data tools."""
        await ctx.send(embed=self._command_overview())

    def _command_overview(self) -> discord.Embed:
        """The grouped command list shown by a bare `.ai`.

        Written for someone who has never seen this plugin: grouped by what you
        would be trying to do, and described in terms of what each command is
        for rather than what it technically does. Modmail's own help formatter
        gives one flat alphabetical list of docstrings, which is accurate and
        tells a newcomer nothing.
        """
        embed = self._embed(
            title=f"{self.setting('assistantname')} — commands",
            description=(
                f"Type `{self._cmd('<command>')}` to run one, or "
                f"`{self.bot.prefix}help {GROUP_NAME} <command>` for the full detail on it."
            ),
            footer="Settings are stored in the database and survive restarts",
        )

        for category, entries in COMMAND_CATEGORIES:
            visible = [(name, blurb) for name, blurb in entries if self._command_visible(name)]
            if not visible:
                continue
            embed.add_field(
                name=category,
                value="\n".join(f"`{name}` — {blurb}" for name, blurb in visible),
                inline=False,
            )

        return embed

    def _command_visible(self, name: str) -> bool:
        """Whether a command belongs in the listing right now.

        `devmode` is never listed: it is the one thing you have to already know
        about. The rest of the developer tools are listed only while devmode is
        on — with one exception. A tool that is currently *running* is always
        listed, however devmode is set, because the alternative is `verbose`
        quietly writing message content to the log with nothing on screen
        admitting it.
        """
        if name in HIDDEN_COMMANDS:
            return False
        if name not in DEV_COMMANDS:
            return True
        if self.setting("devmode"):
            return True
        return bool(self._settings.get(name))

    @ai.command(name="status")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_status(self, ctx):
        """Show plugin wiring, storage and config state."""
        hooked = getattr(self.bot.process_dm_modmail, "__nas_wrapped__", False)

        try:
            counts = {
                "transcripts": await self.db.count_documents({"_type": TYPE_TRANSCRIPT}),
                "tickets": await self.db.count_documents({"_type": TYPE_TICKET}),
                "open conversations": await self.db.count_documents({"_type": TYPE_SESSION}),
                "survey answers": await self.db.count_documents({"_type": TYPE_SURVEY}),
                "kept for review": await self.db.count_documents({"_type": TYPE_TRAINING}),
            }
            # Written by the removed consent gate. Non-zero means personal data
            # is being kept that nothing reads any more.
            legacy = await self.db.count_documents({"_type": TYPE_LEGACY_CONSENT})
            if legacy:
                counts["obsolete consent records"] = legacy
            storage = "\n".join(f"`{k}`: {v}" for k, v in counts.items())
        except Exception as e:
            storage = f"unreachable: `{e}`"

        # First, because every other line here describes the code that is
        # running, not the code that was pulled. Stale code is the one state
        # where all of this can read healthy and none of it is what is live.
        stale = self._is_stale()
        embed = self._embed(
            title=f"{self.setting('assistantname')} — status",
            description=(
                "**The file on disk is newer than the running code, so none of this "
                "reflects what is live.** Reload with "
                f"`{self.bot.prefix}plugin reload @local/norwegian_support`, then run this again. "
                f"See `{self._cmd()} version`."
                if stale
                else None
            ),
            color=self.bot.error_color if stale else None,
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
        # First-class here, not just in `.ai version`: after a deploy this is
        # the one line that answers "did the restart actually pick up the new
        # code" without anyone having to guess from behaviour.
        commit = self._git_head()
        embed.add_field(
            name="Commit",
            value=f"`{commit}`" if commit else "unknown — not a git checkout",
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
        unusable = False
        for label, raw in (("yes", self.setting("yesemoji")), ("no", self.setting("noemoji"))):
            if not _CUSTOM_EMOJI_RE.match(raw.strip()):
                emoji_lines.append(f"`{label}` {raw} — unicode, always usable")
                continue
            resolved = self._resolve_emoji(raw)
            if resolved is None:
                unusable = True
                emoji_lines.append(f"`{label}` `{raw}` — **not usable by this bot**")
            else:
                # Naming the owning server turns "not usable" into an action:
                # either invite the bot there or take the ids from a server it
                # is already in.
                emoji_lines.append(f"`{label}` {raw} — usable, from **{resolved.guild}**")
        embed.add_field(
            name="Confirmation button emoji",
            value="\n".join(emoji_lines)
            + (
                (
                    "\n*The bot is not in the server that owns these, so it cannot use them and "
                    f"the buttons show {BUTTON_YES_FALLBACK} / {BUTTON_NO_FALLBACK} instead. Invite "
                    "the bot to that server, or edit `BUTTON_YES_EMOJI` / `BUTTON_NO_EMOJI` in the "
                    "plugin to ids from a server it is already in — then reload the plugin.*"
                )
                if unusable
                else "\n*Unusable would mean the bot is not in the server that owns the emoji, "
                "and the prompt would fall back to plain unicode.*"
            ),
            inline=False,
        )

        # The form is offered from the DM but delivered to a staff channel, so
        # the half that can silently break is not visible from the DM side.
        partnership_channel_id = self.setting("partnershipchannel")
        guild = self._home_guild()
        channel = guild.get_channel(partnership_channel_id) if guild else None
        if channel is None:
            channel = self.bot.get_channel(partnership_channel_id)
        embed.add_field(
            name="Partnership form",
            value=(
                (
                    "**off** — partnership questions go to a human like any other"
                    if not self.setting("partnershipform")
                    else (
                        f"submissions go to {channel.mention} in **{channel.guild}**"
                        if channel is not None
                        else f"**channel `{partnership_channel_id}` is not visible to this bot** — "
                        "submissions cannot be delivered, and the user is told so and handed to a human"
                    )
                )
                + f"\nTriggered by: {', '.join(f'`{p}`' for p in PARTNERSHIP_PATTERNS)}"
                + f"\nCheck a specific message with `{self._cmd()} ask <message>`."
            ),
            inline=False,
        )

        # A glance at what is on, so a feature nobody remembers turning off is
        # visible from the one command people already run.
        on, off = "\N{WHITE HEAVY CHECK MARK}", "\N{CROSS MARK}"
        embed.add_field(
            name="Features",
            value=(
                " ".join(f"{on if self.setting(f.key) else off}`{f.key}`" for f in FEATURE_SETTINGS)
                + f"\n{self._cmd()} features to change these, "
                + f"{self._cmd()} set for values."
            ),
            inline=False,
        )

        problems, lines = self._config_checks()
        embed.add_field(
            name=("Configuration" if not problems else f"Configuration — **{problems} to fix**"),
            value="\n".join(lines),
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

    def _config_checks(self) -> typing.Tuple[int, typing.List[str]]:
        """Every setting that can be wrong without anything visibly breaking.

        A bad channel id or an unset key does not raise anywhere — it just means
        a notification silently never arrives. Returns how many are wrong, and
        one line per check, so a glance at `.ai status` answers "is the
        environment right" without reading `.env` on the box.
        """
        checks: typing.List[typing.Tuple[bool, str, str]] = []

        guild = self._home_guild()
        checks.append(
            (
                guild is not None,
                "Home server",
                (
                    f"**{guild}** — staff channels are looked up here"
                    if guild is not None
                    else "unresolved — the bot is in neither the built-in server nor "
                    "the one `GUILD_ID` names, so no staff channel can be found"
                ),
            )
        )

        modmail_guild = getattr(self.bot, "guild", None)
        checks.append(
            (
                modmail_guild is not None,
                "Modmail guild",
                f"**{modmail_guild}**" if modmail_guild else "unresolved — check `GUILD_ID`",
            )
        )

        staff_id = self.setting("staffchannel")
        staff = self._guild_channel(staff_id)
        configured = self._setting_source("staffchannel")
        # Only worth flagging when something actually sends there.
        staff_needed = bool(self.setting("digest") or self.setting("lowratingalerts"))
        checks.append(
            (
                staff is not None or not staff_needed,
                "Staff notifications",
                (
                    f"{staff.mention} ({configured}) — digest and low-rating alerts"
                    if staff is not None
                    else f"`{staff_id}` not visible ({configured}) — "
                    + (
                        "**alerts and the weekly digest will not arrive**. Set one with "
                        f"`{self._cmd()} set staffchannel #channel`"
                        if staff_needed
                        else "nothing posts there right now, both features are off"
                    )
                ),
            )
        )

        # The env var is still read, but only as the default for an unset
        # setting, so a stale one is confusing rather than wrong. Say which is
        # actually in force.
        raw_staff = (os.getenv("VLG_STAFF_CHANNEL_ID") or "").strip()
        if raw_staff:
            overridden = "staffchannel" in self._settings
            checks.append(
                (
                    True,
                    "VLG_STAFF_CHANNEL_ID",
                    (
                        f"`{raw_staff}` — **ignored**, `{self._cmd()} set staffchannel` " "takes precedence"
                        if overridden
                        else f"`{raw_staff}` — in use as the default"
                        + ("" if raw_staff.isdigit() else ", but **it is not a channel id**")
                    ),
                )
            )

        key_set = bool(os.getenv("GROQ_API_KEY"))
        checks.append(
            (
                key_set and AsyncGroq is not None,
                "GROQ_API_KEY",
                (
                    "set"
                    if key_set and AsyncGroq is not None
                    else ("groq package missing" if key_set else "**not set** — every request escalates")
                ),
            )
        )

        log_url = (self.bot.config.get("log_url") or "").strip()
        checks.append(
            (
                bool(log_url),
                "log_url",
                f"`{log_url}`" if log_url else f"unset — `{self._cmd()} ticket` cannot link a log",
            )
        )

        expiry = self.bot.config.get("log_expiration")
        expiry_set = bool(expiry and expiry != isodate.Duration())
        checks.append(
            (
                expiry_set,
                "Retention",
                (
                    f"transcripts {self.setting('retentiondays')}d, ticket logs "
                    f"`{isodate.duration_isoformat(expiry)}`"
                    if expiry_set
                    else f"transcripts {self.setting('retentiondays')}d, ticket logs **never expire**"
                ),
            )
        )

        problems = sum(1 for ok, _, _ in checks if not ok)
        # Named rather than inlined: an escape inside an f-string expression is
        # a syntax error before Python 3.12, and this has to run on 3.10.
        good = "\N{WHITE HEAVY CHECK MARK}"
        bad = "\N{WARNING SIGN}"
        lines = [f"{good if ok else bad} {name}: {detail}" for ok, name, detail in checks]
        return problems, lines

    def _source_mtime(self) -> typing.Optional[datetime]:
        """When the plugin file on disk last changed. None if unreadable."""
        try:
            return datetime.fromtimestamp(pathlib.Path(__file__).resolve().stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    def _is_stale(self) -> bool:
        """True when the file has been pulled or edited since this code loaded.

        The comparison that actually matters: editing or pulling the file does
        nothing at all until the plugin is reloaded, and a bot still running the
        previous version looks completely healthy while doing so.
        """
        modified = self._source_mtime()
        return modified is not None and self._loaded_at is not None and modified > self._loaded_at

    @ai.command(name="version", aliases=["updated"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_version(self, ctx):
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

        stale = self._is_stale()

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

    @ai.command(name="ask")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_ask(self, ctx, *, question: str):
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

        # Before escalation, exactly as the live route has it: a partnership
        # request phrased as wanting to speak to someone still gets the form.
        # Gated on the same setting, so a dry run cannot promise a form that the
        # live route would not offer.
        partnership = self._partnership_match(question) if self.setting("partnershipform") else None
        if partnership:
            return await ctx.send(
                embed=self._embed(
                    title="Dry run — no pre-screen",
                    description=(
                        f"> {truncate(question, 200)}\n\n"
                        f"Matches partnership term `{partnership}`, so the form is offered and "
                        "Groq is never called. Submissions go to the channel shown in "
                        f"`{self._cmd()} status`."
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
                    f"See `{self._cmd()} status`.",
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

    @ai.command(name="stats")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_stats(self, ctx):
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
                        f"{self.setting('retentiondays')} days, so this is always a rolling window."
                    ),
                )
            )

        gaps = self._faq_gaps(transcripts)

        embed = self._embed(
            title="Vueling AI — stats",
            description=(
                f"**{gaps['total']}** conversation(s) on record, **{gaps['still_open']}** still open.\n"
                f"Transcripts are deleted after {self.setting('retentiondays')} days, so this is "
                "a rolling window, not all time."
            ),
        )
        embed.add_field(
            name="Reached a human",
            value=(
                f"**{gaps['handoff_rate']:.0f}%** of finished conversations "
                f"({gaps['handed_off']}/{gaps['finished']})\n"
                f"of which **{gaps['deferral_rate']:.0f}%** were the assistant genuinely unable to answer"
                if gaps["finished"]
                else "no finished conversations yet"
            ),
            inline=False,
        )

        if gaps["reasons"]:
            embed.add_field(
                name="Why it handed off",
                value="\n".join(
                    f"`{count}` {HANDOFF_REASON_LABELS.get(reason, reason)}"
                    for reason, count in gaps["reasons"]
                ),
                inline=False,
            )

        await self._add_rating_field(embed)
        self._add_gap_fields(embed, gaps)

        embed.set_footer(text="Add FAQ entries for what shows up here; each one removes a handoff.")
        await ctx.send(embed=embed)

    @staticmethod
    def _faq_gaps(transcripts: typing.List[dict]) -> dict:
        """Reduce transcripts to the numbers `.ai stats` and the digest report.

        Pulled out of the command so the weekly digest reports exactly what
        someone running the command by hand would see, rather than a second
        implementation that can drift from it.
        """
        still_open = sum(1 for t in transcripts if t.get("closed_at") is None)
        handed_off = [t for t in transcripts if t.get("handed_off_at") is not None]
        finished = [t for t in transcripts if t.get("closed_at") is not None]

        # Only genuine deferrals belong in the headline rate. A user typing
        # "agent" is a routing preference, not a failure to answer.
        deferred = [t for t in handed_off if t.get("handoff_reason") == HANDOFF_AI_DEFERRED]

        reasons: typing.Dict[str, int] = {}
        for t in handed_off:
            key = t.get("handoff_reason") or "unrecorded"
            reasons[key] = reasons.get(key, 0) + 1

        questions = [NorwegianSupport._last_user_message(t) for t in deferred]
        questions = [q for q in questions if q]

        repeats: typing.Dict[str, int] = {}
        for q in questions:
            key = " ".join(re.findall(r"[\w']+", q.lower()))
            repeats[key] = repeats.get(key, 0) + 1
        repeated = sorted(((k, c) for k, c in repeats.items() if c > 1), key=lambda kv: -kv[1])

        # Most questions are phrased uniquely, so term frequency is what
        # actually points at the missing FAQ entry.
        terms: typing.Dict[str, int] = {}
        for q in questions:
            for word in set(re.findall(r"[a-z']{3,}", q.lower())):
                if word not in STATS_STOPWORDS:
                    terms[word] = terms.get(word, 0) + 1
        ranked = sorted(terms.items(), key=lambda kv: -kv[1])[:12]

        return {
            "total": len(transcripts),
            "still_open": still_open,
            "handed_off": len(handed_off),
            "finished": len(finished),
            "deferred": len(deferred),
            "handoff_rate": (len(handed_off) / len(finished) * 100) if finished else 0.0,
            "deferral_rate": (len(deferred) / len(finished) * 100) if finished else 0.0,
            "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
            "repeated": repeated,
            "terms": ranked,
            "questions": questions,
        }

    @staticmethod
    def _add_gap_fields(embed: discord.Embed, gaps: dict) -> None:
        """The what-to-write-next half, shared by the command and the digest."""
        if not gaps["questions"]:
            embed.add_field(
                name="Unanswered questions",
                value="None recorded. Either nothing has been deferred, or those "
                "conversations predate handoff reasons being stored.",
                inline=False,
            )
            return

        if gaps["repeated"]:
            embed.add_field(
                name="Asked more than once",
                value="\n".join(f"`{c}x` {truncate(k, 80)}" for k, c in gaps["repeated"][:5]),
                inline=False,
            )
        if gaps["terms"]:
            embed.add_field(
                name="Common terms in unanswered questions",
                value=" ".join(f"`{w}`×{c}" for w, c in gaps["terms"]),
                inline=False,
            )
        embed.add_field(
            name="Most recent unanswered",
            value="\n".join(f"• {truncate(q, 90)}" for q in gaps["questions"][-5:]),
            inline=False,
        )

    async def _add_rating_field(self, embed: discord.Embed) -> None:
        """Average survey scores, when any have been submitted."""
        try:
            answers = await self.db.find(
                {"_type": TYPE_SURVEY}, {"ratings": 1, "rating": 1, "training_consent": 1}
            ).to_list(length=2000)
        except Exception:
            logger.error("Could not read survey answers.", exc_info=True)
            return

        if not answers:
            return

        lines = []
        for key, label, _, _ in SURVEY_RATING_QUESTIONS:
            # Answers stored before there was more than one question carry only
            # the headline score, so fall back to it for that one question.
            scores = []
            for a in answers:
                score = (a.get("ratings") or {}).get(key)
                if score is None and key == SURVEY_HEADLINE_KEY:
                    score = a.get("rating")
                if isinstance(score, int):
                    scores.append(score)
            if scores:
                lines.append(f"**{sum(scores) / len(scores):.1f}**/5 — {label} ({len(scores)})")

        consented = sum(1 for a in answers if a.get("training_consent"))
        lines.append(f"{consented} of {len(answers)} agreed we could keep the chat")

        embed.add_field(name="Survey", value="\n".join(lines), inline=False)

    @staticmethod
    def _last_user_message(transcript: dict) -> typing.Optional[str]:
        """The question that ended up going to a human."""
        for entry in reversed(transcript.get("messages") or []):
            if entry.get("role") == "user" and entry.get("content"):
                return entry["content"]
        return None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _setting_source(self, key: str) -> str:
        """Whether a setting was set here or is still on its default."""
        return "set" if key in self._settings else "default"

    @ai.command(name="set")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_set(self, ctx, key: str = None, *, value: str = None):
        """Show or change a setting that carries a value.

        `.ai set` on its own lists everything with its current value.
        `.ai set staffchannel #staff` changes one.
        `.ai set staffchannel default` puts it back to the built-in default.
        """
        if key is None:
            return await ctx.send(embed=self._settings_overview())

        key = key.lower().lstrip("-")
        setting = SETTINGS.get(key)

        if setting is None or setting.is_feature:
            # A feature name typed here is a near miss, not a mistake worth a
            # bare error: say where it actually lives.
            if setting is not None:
                return await ctx.send(
                    embed=self._embed(
                        title="That one is a feature, not a value",
                        description=(
                            f"`{key}` is on or off rather than a value, so it lives in "
                            f"`{self._cmd()} features`.\n\n"
                            f"Try `{self._cmd()} features {key} on`."
                        ),
                        color=self.bot.error_color,
                    )
                )
            return await ctx.send(embed=self._unknown_setting_embed(key, VALUE_SETTINGS, "set"))

        if value is None:
            current = setting.render(self.setting(key))
            return await ctx.send(
                embed=self._embed(
                    title=key,
                    description=(
                        f"{setting.summary}\n\n**Currently:** {current} "
                        f"({self._setting_source(key)})\n\n"
                        f"Change it with `{self._cmd()} set {key} "
                        f"{setting.example or '<value>'}`."
                    ),
                )
            )

        if value.strip().lower() in ("default", "reset", "clear"):
            await self.clear_setting(key)
            logger.info("%s reset setting %s to its default.", ctx.author, key)
            return await ctx.send(
                embed=self._embed(
                    title=f"{key} reset",
                    description=f"Back to the default: {setting.render(setting.default)}.",
                )
            )

        try:
            parsed = setting.parse(value, ctx.guild)
        except ValueError as e:
            return await ctx.send(
                embed=self._embed(
                    title=f"Could not set {key}",
                    description=f"{e}\n\n{setting.summary}",
                    color=self.bot.error_color,
                )
            )

        await self.set_setting(key, parsed)
        logger.info("%s set %s to %r.", ctx.author, key, parsed)

        embed = self._embed(
            title=f"{key} updated",
            description=f"Now {setting.render(parsed)}.\n\n{setting.summary}",
        )

        # A channel the bot cannot see accepts silently and then never delivers
        # anything, which is exactly the failure this command exists to prevent.
        if setting.kind == "channel" and self._guild_channel(parsed) is None:
            embed.color = self.bot.error_color
            embed.add_field(
                name="\N{WARNING SIGN} Cannot see that channel",
                value=(
                    "The setting is saved, but this bot cannot currently see that "
                    "channel, so nothing will be posted there. Check it exists and "
                    "that the bot has permission to view and send messages in it."
                ),
                inline=False,
            )

        # Both halves of a pair only make sense relative to each other.
        if key == "inactivitywarning" and parsed >= self.setting("inactivityclose"):
            embed.add_field(
                name="\N{WARNING SIGN} Warning is not before the close",
                value=(
                    f"`inactivityclose` is {self.setting('inactivityclose')} min, so the "
                    "chat closes before anyone is warned. Set the warning lower."
                ),
                inline=False,
            )
        if key == "inactivityclose" and parsed <= self.setting("inactivitywarning"):
            embed.add_field(
                name="\N{WARNING SIGN} Close is not after the warning",
                value=(
                    f"`inactivitywarning` is {self.setting('inactivitywarning')} min, so the "
                    "chat closes before anyone is warned. Set the close higher."
                ),
                inline=False,
            )

        await ctx.send(embed=embed)

    def _settings_overview(self) -> discord.Embed:
        """Every value setting, its current value, and what it is for."""
        embed = self._embed(
            title="Settings",
            description=(
                f"Change one with `{self._cmd()} set <name> <value>`, or put it "
                f"back with `{self._cmd()} set <name> default`.\n"
                f"On/off features are in `{self._cmd()} features`."
            ),
            footer="Stored in the database — these survive reloads and restarts",
        )
        for setting in VALUE_SETTINGS:
            embed.add_field(
                name=f"{setting.key} — {setting.render(self.setting(setting.key))}",
                value=f"{setting.summary} *({self._setting_source(setting.key)})*",
                inline=False,
            )
        return embed

    def _unknown_setting_embed(self, key: str, pool, command: str) -> discord.Embed:
        return self._embed(
            title=f"No setting called {key}",
            description=(
                "The ones you can change here are:\n"
                + "\n".join(f"• `{s.key}`" for s in pool)
                + f"\n\nRun `{self._cmd()} {command}` to see them with their values."
            ),
            color=self.bot.error_color,
        )

    @ai.command(name="features", aliases=["feature"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_features(self, ctx, key: str = None, state: str = None):
        """Turn optional features on and off.

        `.ai features` lists everything with its current state.
        `.ai features digest off` turns one off.
        `.ai features digest` flips whatever it is now.
        """
        if key is None:
            return await ctx.send(embed=self._features_overview())

        key = key.lower().lstrip("-")
        setting = SETTINGS.get(key)

        if setting is None or not setting.is_feature:
            if setting is not None:
                return await ctx.send(
                    embed=self._embed(
                        title="That one carries a value",
                        description=(
                            f"`{key}` is not simply on or off, so it lives in "
                            f"`{self._cmd()} set`.\n\n"
                            f"Try `{self._cmd()} set {key}`."
                        ),
                        color=self.bot.error_color,
                    )
                )
            return await ctx.send(embed=self._unknown_setting_embed(key, FEATURE_SETTINGS, "features"))

        if state is None:
            new_value = not self.setting(key)
        else:
            try:
                new_value = setting.parse(state, ctx.guild)
            except ValueError as e:
                return await ctx.send(
                    embed=self._embed(
                        title=f"Could not change {key}",
                        description=f"{e}",
                        color=self.bot.error_color,
                    )
                )

        await self.set_setting(key, new_value)
        # A developer feature toggled here changes whether it is listed.
        self._apply_devmode_visibility()
        logger.info("%s turned %s %s.", ctx.author, key, "on" if new_value else "off")

        embed = self._embed(
            title=f"{key} is now {'on' if new_value else 'off'}",
            description=setting.summary,
            color=None if new_value else self.bot.error_color,
        )

        # Turning the parent off leaves the children stored as on but inert, so
        # say that rather than letting the list read as though they still work.
        if key == "survey" and not new_value:
            dependents = [k for k in ("training", "lowratingalerts") if self.setting(k)]
            if dependents:
                embed.add_field(
                    name="Also stopped",
                    value=(
                        "These come from the survey, so they stop too until it is back on: "
                        + ", ".join(f"`{k}`" for k in dependents)
                    ),
                    inline=False,
                )
        if key in ("training", "lowratingalerts") and new_value and not self.setting("survey"):
            embed.add_field(
                name="\N{WARNING SIGN} The survey is off",
                value=(
                    "This needs the survey, which is currently off, so nothing will "
                    f"happen yet. Turn it on with `{self._cmd()} features survey on`."
                ),
                inline=False,
            )
        if key in ("digest", "lowratingalerts") and new_value:
            if self._guild_channel(self.setting("staffchannel")) is None:
                embed.add_field(
                    name="\N{WARNING SIGN} No staff channel",
                    value=(
                        "This posts to the staff channel, which this bot cannot "
                        f"currently see. Set one with `{self._cmd()} set "
                        "staffchannel #channel`."
                    ),
                    inline=False,
                )

        await ctx.send(embed=embed)

    def _features_overview(self) -> discord.Embed:
        """Every optional feature, on or off, in plain words."""
        embed = self._embed(
            title="Features",
            description=(
                f"Turn one on or off with `{self._cmd()} features <name> on|off`.\n"
                f"Settings that carry a value are in `{self._cmd()} set`."
            ),
            footer="Stored in the database — these survive reloads and restarts",
        )
        on, off = "\N{WHITE HEAVY CHECK MARK}", "\N{CROSS MARK}"
        for setting in FEATURE_SETTINGS:
            # A developer tool is hidden here on the same rule as in the command
            # list, so the two never disagree about what exists.
            if not self._command_visible(setting.key):
                continue
            enabled = bool(self.setting(setting.key))
            embed.add_field(
                name=f"{on if enabled else off} {setting.key} — {'on' if enabled else 'off'}",
                value=f"{setting.summary} *({self._setting_source(setting.key)})*",
                inline=False,
            )
        return embed

    @ai.command(name="devmode", hidden=True)
    @checks.has_permissions(PermissionLevel.OWNER)
    async def ai_devmode(self, ctx, enabled: bool = None):
        """Show or hide the developer commands. Undocumented on purpose."""
        await self.set_setting("devmode", (not self.setting("devmode")) if enabled is None else enabled)
        state = self.setting("devmode")
        self._apply_devmode_visibility()

        logger.info("Developer mode turned %s by %s.", "on" if state else "off", ctx.author)
        await ctx.send(
            embed=self._embed(
                title=f"Developer mode {'on' if state else 'off'}",
                description=(
                    (
                        "Developer commands are now listed in "
                        f"`{self._cmd()}`: {', '.join(f'`{c}`' for c in sorted(DEV_COMMANDS))}."
                    )
                    if state
                    else (
                        "Developer commands are hidden from the command list again. "
                        "They still work if you type them."
                    )
                ),
                color=None if state else self.bot.error_color,
                footer="This command is never listed, on or off",
            )
        )

    def _apply_devmode_visibility(self) -> None:
        """Match Modmail's own help to the grouped listing.

        `.ai` builds its list from COMMAND_CATEGORIES, but `?help ai` is
        Modmail's formatter reading `command.hidden`, and a command absent from
        one list while present in the other is worse than not gating it at all.

        The group is fetched from the bot rather than as `self.ai`, which looks
        like the obvious way to reach it and is not: accessing a command through
        the cog hands back a fresh copy every time, so setting `hidden` on it
        changes an object nothing else will ever look at. `bot.get_command` is
        the instance the dispatcher and the help formatter actually hold.
        """
        group = self.bot.get_command(GROUP_NAME)
        if group is None or not hasattr(group, "commands"):
            logger.debug("Command group %r is not registered yet; visibility not applied.", GROUP_NAME)
            return

        for command in group.commands:
            if command.name in HIDDEN_COMMANDS:
                command.hidden = True
            elif command.name in DEV_COMMANDS:
                command.hidden = not self._command_visible(command.name)

    # Declared hidden so it stays hidden in the window between the cog being
    # added and the stored settings arriving. _apply_devmode_visibility settles it.
    @ai.command(name="verbose", hidden=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_verbose(self, ctx, enabled: bool = None):
        """Promote this plugin's diagnostics to INFO without a bot restart.

        Logs the message that caused a defer and Groq's verbatim response.
        Kept as its own command because the warning below is worth showing;
        it is the same setting as `.ai features verbose`.
        """
        await self.set_setting("verbose", (not self._verbose) if enabled is None else enabled)
        # Turning verbose on makes it visible even with devmode off, and
        # turning it off may take it away again.
        self._apply_devmode_visibility()

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
                f"to the bot log, which is not on the {self.setting('retentiondays')}-day "
                f"deletion path. Turn it off with `{self._cmd()} verbose off` "
                "once you are done."
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

    @ai.command(name="forget", aliases=["revoke"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_forget(self, ctx, user: discord.User):
        """Delete a user's stored assistant conversations.

        For acting on an erasure request from the data protection page. There is
        no consent to withdraw any more, but the transcripts still exist and this
        is the only way to remove them before their 7 day expiry.
        """
        user_hash = await self._user_hash(user.id)
        transcripts = await self.db.delete_many({"_type": TYPE_TRANSCRIPT, "user_id_hash": user_hash})
        # Conversations kept for review have to go too. They were kept on this
        # user's say-so, and an erasure request reaching everything except the
        # copy that outlives the others would be the wrong way round. This is
        # why the training copy keeps the hash rather than nothing at all.
        training = await self.db.delete_many({"_type": TYPE_TRAINING, "user_id_hash": user_hash})
        surveys = await self.db.delete_many({"_type": TYPE_SURVEY, "user_id_hash": user_hash})
        await self.db.delete_many({"_type": TYPE_SESSION, "user_id": user.id})
        # Sweep any record left behind by the removed consent gate.
        legacy = await self.db.delete_many({"_type": TYPE_LEGACY_CONSENT, "user_id": user.id})

        removed = (
            transcripts.deleted_count + training.deleted_count + surveys.deleted_count + legacy.deleted_count
        )
        if not removed:
            return await ctx.send(
                embed=self._embed(
                    description=f"Nothing stored for {user.mention}.",
                    color=self.bot.error_color,
                )
            )

        note = f"Deleted {transcripts.deleted_count} assistant conversation(s) for {user.mention}."
        if training.deleted_count or surveys.deleted_count:
            note += (
                f"\nAlso removed {training.deleted_count} conversation(s) kept for review "
                f"and {surveys.deleted_count} survey answer(s)."
            )
        if legacy.deleted_count:
            note += f"\nAlso cleared {legacy.deleted_count} obsolete consent record(s)."
        note += (
            "\n\nThis does not touch ticket transcripts with a human agent: those are "
            "Modmail's own logs, expiring on `log_expiration`. Use `?logs` for those."
        )

        await ctx.send(embed=self._embed(description=note))
        logger.info("Erased stored data for %s (%s) at the request of %s.", user, user.id, ctx.author)

    @ai.command(name="digest")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_digest(self, ctx):
        """Send the weekly digest now, without waiting for its slot.

        For checking the staff channel is reachable and the content reads
        right. Does not move the weekly schedule.
        """
        embed = await self._digest_embed()
        if embed is None:
            return await ctx.send(
                embed=self._embed(
                    description="Nothing on record to summarise yet.", color=self.bot.error_color
                )
            )

        sent = await self._post_to_staff(embed, what="weekly digest")
        result = self._embed(
            description=(
                f"Digest posted to <#{self.setting('staffchannel')}>."
                if sent is not None
                else f"**Could not post to <#{self.setting('staffchannel')}>** "
                f"(`{self.setting('staffchannel')}`). Check the bot can see that channel, "
                f"or set one with `{self._cmd()} set staffchannel #channel`."
            ),
            color=None if sent is not None else self.bot.error_color,
        )
        # Sending on demand deliberately still works with the feature off — this
        # is the command you reach for to check the channel — but it would read
        # as proof the weekly one is running, so say that it is not.
        if not self.setting("digest"):
            result.add_field(
                name="The weekly digest is off",
                value=(
                    "This one was sent because you asked for it. No digest will arrive "
                    f"on its own until `{self._cmd()} features digest on`."
                ),
                inline=False,
            )
        await ctx.send(embed=result)

    @ai.command(name="training")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_training(self, ctx, samples: int = TRAINING_SAMPLE_DEFAULT):
        """Conversations users agreed we could keep, with a few read inline.

        A reading list, not a pipeline. Nothing here changes the assistant on
        its own — `FAQ_KNOWLEDGE` and the prompt are edited by hand, the same
        way the gaps `.ai stats` surfaces are fixed today.
        """
        samples = max(0, min(samples, TRAINING_SAMPLE_MAX))

        total = await self.db.count_documents({"_type": TYPE_TRAINING})
        if not total:
            reason = (
                "Nothing kept yet. Conversations appear here when a user answers "
                "yes to the training question in the survey after a chat ends."
            )
            if not self.setting("training"):
                reason = (
                    "Training data collection is **off**, so the question is never "
                    "asked and nothing is being kept.\n\nTurn it on with "
                    f"`{self._cmd()} features training on`."
                )
            elif not self.setting("survey"):
                reason = (
                    "The post-chat survey is **off**, and the training question is part "
                    "of it, so nothing is being kept.\n\nTurn it on with "
                    f"`{self._cmd()} features survey on`."
                )
            return await ctx.send(embed=self._embed(description=reason, color=self.bot.error_color))

        answered = await self.db.count_documents({"_type": TYPE_SURVEY})
        recent = (
            await self.db.find({"_type": TYPE_TRAINING})
            .sort("consented_at", -1)
            .limit(samples)
            .to_list(length=samples)
        )

        embed = self._embed(
            title="Kept for review",
            description=f"**{total}** conversation(s) from **{answered}** survey answer(s).",
            footer="No user IDs or names are stored with these",
        )

        # A field caps at 1024 characters but the whole embed caps at 6000, so
        # the full ten do not necessarily fit. Stop early rather than have
        # Discord reject the message outright.
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
                f"**{total}** conversation(s) from **{answered}** survey answer(s), showing "
                f"{shown} — the rest did not fit. Ask for fewer at a time."
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
        """Render a conversation into one embed field, inside the 1024 cap."""
        lines = []
        for entry in doc.get("messages", []):
            speaker = "**User**" if entry.get("role") == "user" else "**AI**"
            content = " ".join((entry.get("content") or "").split())
            line = f"{speaker}: {truncate(content, 160)}"
            # Leave room for the marker rather than losing the whole embed.
            if sum(len(x) + 1 for x in lines) + len(line) > 970:
                lines.append("…")
                break
            lines.append(line)
        return "\n".join(lines) or "*empty*"

    @ai.command(name="ticket")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_ticket(self, ctx, reference: str):
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
