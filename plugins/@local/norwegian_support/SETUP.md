# Norwegian Air Shuttle support plugin — setup

## Load

```
?plugin load @local/norwegian_support
```

Reload after editing during development:

```
?plugin reload @local/norwegian_support
```

Verify wiring, storage and config with:

```
?nas status
```

The DM hook must read `installed`, `confirm_thread_creation` must read `off`,
and ticket log retention must not read `Never`.

## Staff commands

| Command | Permission | Purpose |
|---|---|---|
| `?nas status` | Administrator | Wiring, storage counts, config sanity |
| `?nas verbose` | Administrator | Log why the AI deferred (see below) |
| `?nas consent @user` | Supporter | Show a stored consent record |
| `?nas revoke @user` | Supporter | Withdraw consent; re-prompts next ticket |
| `?nas ticket VLG-XXXXXX` | Supporter | Resolve a reference to its Modmail log |

`?nas revoke` exists because the privacy notice tells users they can withdraw
acceptance by asking the support team. Keep it working.

## Required config

Modmail-native strings are set through `?config`, not written into plugin code,
so they stay editable without a reload. Run these once.

### Turn off the built-in confirm step

The plugin's gate replaces it. Leaving it on double-prompts the user ahead of
the consent notice.

```
?config set confirm_thread_creation no
```

### Thread creation messages

Sent by Modmail itself once a thread is created at handoff.

```
?config set thread_creation_title Ticket Opened
?config set thread_creation_response A member of the Norwegian Air Shuttle support team will be with you shortly. Please keep your booking or flight details to hand.
?config set thread_creation_footer Your message has been sent to the support team
```

### Cancellation

Shown when a user declines the privacy notice, when the notice times out, or
when thread creation is otherwise stopped.

```
?config set thread_cancelled Support Cancelled
```

### Log retention — required, and load-bearing

The privacy notice tells users their ticket messages are deleted 7 days after
the ticket closes. **That is only true if this is set.** Modmail's default is
`Never`, which keeps every thread log forever and would make the notice a false
statement.

```
?config set log_expiration P7D
```

**Then restart the bot.** Modmail's `log_expiry` task calls `.stop()` on itself
during its first run if `log_expiration` is unset, and never restarts within
that process — so setting this on a running bot has no effect until a restart.

Confirm it took with `?nas status`, which reports the effective retention.

### DM receipt emoji

Left at Modmail's defaults unless you want brand-specific marks. Both accept
custom guild emoji.

```
?config set sent_emoji ✅
?config set blocked_emoji ⛔
```

### Optional — minimum message length

Applies before the plugin gate and cuts down on empty "hello" tickets.

```
?config set thread_min_characters 15
?config set thread_min_characters_title Please add some detail
?config set thread_min_characters_response Tell us what you need help with and we can route you to the right team.
```

### Colors

The plugin reads `main_color`, `mod_color` and `error_color` from config at
runtime for every embed it builds. Nothing is hardcoded, so setting them here
restyles the plugin's embeds too.

```
?config set main_color #D81939
?config set mod_color #D81939
?config set error_color #C0392B
```

## Environment

The AI pre-screen needs a Groq API key in `.env` alongside the existing Modmail
values:

```
GROQ_API_KEY=gsk_...
```

`core/config.py` calls `load_dotenv()` at import, so `.env` is enough — no
export needed. `?nas status` reports whether the key and the `groq` package are
both present. Without either, every request simply escalates to a human; nothing
breaks.

## The FAQ — read this before going live

`FAQ_KNOWLEDGE` in `norwegian_support.py` is the **only** thing the assistant is
allowed to answer from. It is instructed to hand off anything not covered, so:

- **A wrong entry becomes a wrong answer, stated confidently, to a real user.**
- A missing entry costs nothing: that question escalates to a human.

The current content was supplied by the group. Keep it that way — when
something is unknown, delete the entry rather than guessing, and the question
will simply escalate to a human.

Two known gaps, both deliberate:

- **Rank names are not in the reference.** Public answers are allowed to include
  them, but nobody has supplied them, so the model is explicitly told to hand
  off rather than guess when asked to name the ranks. Add them to unlock that
  answer.
- **No flight hour is ever stated.** The reference gives only the pattern
  (`XX:00` open, `XX:20` lock, `XX:25` boarding, `XX:35` departure) and points
  at the departures page for real times.

The assistant is additionally instructed to defer on anything case-by-case
(individual bans, appeal outcomes, application outcomes, accounts), on reports
of a specific failed purchase, and where the user seems upset.

Note what is deliberately *not* deferred: the **no-refunds policy** is answered
plainly rather than escalated, and handing someone the **appeals link** counts
as a complete answer. Both were escalation cases in an earlier draft; stating a
policy the user will not like is still a complete answer.

### Links

The three links are written as `[display.domain](https://real.vercel.url)` and
the model is instructed to reproduce them verbatim, never as bare URLs and never
invented. Discord renders markdown links in embed descriptions, so the user sees
the clean domain.

The link domains and the assistant's identity are both Vueling now.

### Diagnosing a deferral

When the assistant hands off, the log line only says it deferred. The user's
message and Groq's verbatim `{resolved, reply}` response are logged alongside
it at DEBUG.

Modmail applies `log_level` once at startup, so `?config set log_level debug`
needs a full restart and turns on discord.py's own debug firehose too. To avoid
both:

```
?nas verbose on
```

That promotes only these two lines to INFO, takes effect immediately, and
survives until toggled off or the bot restarts.

**It writes ticket message content into the bot log.** That content is covered
by the privacy notice but the log is not on the 7-day deletion path, so turn it
off once you are done:

```
?nas verbose off
```

### Conversation flow

Every message the assistant sends runs behind a typing indicator for
`TYPING_DELAY_SECONDS` (2.5s), via Modmail's own `safe_typing`. That is what
makes consecutive messages read as separate turns — Discord groups messages from
the same author, so without the pause they run together visually no matter how
they are styled.

**Budget the latency.** A greeted question costs two greeting pauses plus one
for the answer, on top of the Groq round trip: roughly 9–11 seconds before the
user sees an answer. Lower `TYPING_DELAY_SECONDS` if that feels slow.

`GREETING_PARTS` is two messages, sent once at the start of a new conversation,
before the first message is processed — including before the escalation check,
so someone opening with "agent" is greeted before being handed over. It does not
repeat later in the same conversation.

**Contentless openers.** A message made entirely of greeting words (`hi`,
`hello`, `good morning`, emoji only…) is treated as an opening, not an
unanswerable question. It never reaches Groq and is never escalated: the
greeting's second part already asks what they need. Mid-conversation, they get
`CONTENTLESS_PROMPT` instead. `GREETING_WORDS` drives this — a message with any
word outside that set is a real request, so "hi when is the flight" gets the
greeting *and* an answer.

**Handoff confirmation.** When the assistant can't resolve something, or an
escalation phrase matches, it asks `HANDOFF_CONFIRM_TEXT` with Yes/No buttons
before creating any thread.

- **Yes** → `HANDOFF_ACCEPTED_TEXT`, then `HANDOFF_GOODBYE_TEXT` as a separate
  message, then the normal Modmail handoff.
- **No** → `HANDOFF_DECLINED_TEXT` and the conversation carries on with the
  assistant. The transcript stays open.
- **No answer** → escalates after `HANDOFF_CONFIRM_TIMEOUT_SECONDS` (120s). The
  user either asked for a human or hit something unanswerable, so silence is
  worse than a ticket.

Buttons rather than typed yes/no, matching Modmail's own confirm flows. A typed
answer would also be re-processed as a fresh DM by the queue, which would need
message-level interception to avoid double handling.

**Technical failures skip the confirmation.** A Groq exception, timeout, bad
payload, or missing key hands off directly — offering to keep talking to an
assistant that cannot answer would loop the user through the same failure.

### Embeds

Every embed the plugin builds carries the **Vueling AI author row** — name plus
the bot's own avatar, read from `bot.user.display_avatar` so it follows whatever
is set in the Developer Portal without a redeploy.

| Message | Colour | Title | Footer |
|---|---|---|---|
| Greeting, prompts, handoff copy, consent notice | `main_color` | none | none |
| Model-generated replies | `mod_color` | none | `Vueling AI can make mistakes…`, no icon |

Model replies carry no title: the author row already names the assistant, and
both together says it twice. The footer is plain text — an icon beside a
disclaimer reads as branding rather than a caveat.

The caveat footer is a statement about *model* output, so it goes only on text
the model produced. Putting it on fixed plugin copy would misattribute it.

Handoff Yes/No buttons are guild emoji with no labels
(`BUTTON_YES_EMOJI` / `BUTTON_NO_EMOJI`). The consent notice keeps its **text
labels**: an emoji-only choice is fine for "shall I fetch a human", not for
accepting a privacy policy.

The model may return `reply` as a string or as an array of two strings, and each
element is sent as its own message. It is told to split only when an answer
genuinely reads better in two parts.

A conversation ends when a human takes over: the handoff stamps `handed_off_at`
on the transcript, so the next time that user writes in they are greeted afresh.
A conversation the assistant resolved stays open, so follow-up questions do not
re-greet; it lapses naturally when the transcript expires after 7 days.

### Branding, mid-rebrand

Three surfaces say Vueling by explicit instruction: the greeting, the embed
title (`Vueling AI`), and the caveat footer. Everything else is deliberately
untouched pending the full rebrand pass — bot identity, the repo name, the
`?nas` command group, and the `NorwegianSupport` cog class (whose name *is* the
partition name, so renaming it orphans every stored document).

Ticket references are now `VLG-XXXXXX`. The stored field is still called
`nas_ref` for compatibility with existing documents; renaming it needs a data
migration.

The privacy notice now names Vueling, and `POLICY_VERSION` is **2**. Everyone
who accepted under version 1 is re-prompted on their next ticket, with the
renewal wording explaining the rename. Declining the new notice withdraws the
old consent, as it does for any renewal.

**Still naming the old airline**, and needing action outside this file: the
`thread_creation_response` **config value** suggested above. Editing this file
does nothing on its own — re-run the `?config set` on the live bot.

Also still Norwegian Air Shuttle, but internal only and left for the rebrand
pass: the module docstring, the `?nas` command group help text, the
`norwegian_support` plugin/folder name, and the `NorwegianSupport` cog class
(whose name is the storage partition, so it needs a data migration).

### Embed icon

The author-row icon comes from `bot.user.display_avatar`, so it follows the
Developer Portal without a redeploy. `?nas status` prints the URL it resolves to.

If it is not the logo you expect, check *which* Portal image you set: **App Icon**
on General Information and **Bot avatar** on the Bot tab are separate images, and
only the Bot avatar reaches `display_avatar`. Setting the App Icon alone leaves
the embed on Discord's default grey.

To point the icon at something other than the bot's avatar, set `AI_ICON_URL` to
a direct image URL. Discord must be able to fetch it, so it needs to be hosted
somewhere public — an attachment in a Discord channel works.

### Escalation phrases

`ESCALATION_PATTERNS` short-circuits to a human before Groq is called at all.
Each entry is a case-insensitive regex matched on word boundaries, so `agent`
does not fire on "management" and `human` does not fire on "humanity". Add
phrases freely; keep the `\b` anchors.

### Handoff

On handoff the plugin summarises the conversation with one more Groq call,
then lets Modmail create the thread exactly as before. `on_thread_ready` — the
event Modmail dispatches once the channel, genesis message and staff mirroring
are all in place — posts the summary as the thread's first plugin message,
allocates a `VLG-XXXXXX` reference, stores it against Modmail's own log key, and
DMs the reference to the user.

Nothing here blocks the handoff. A failed summary falls back to generic text, a
failed reference allocation still posts the summary, and a thread opened outside
the pre-screen (`?contact`, for instance) is left alone entirely.

Modmail's log key stays the durable identifier; `VLG-XXXXXX` is the readable
handle, drawn from an alphabet with no `O/0` or `I/1` so a code read aloud
cannot land on the wrong ticket.

## Local changes to Modmail core

This plugin is self-contained with one exception. **Re-apply it after any
upstream Modmail merge** — a merge that takes upstream's version of `bot.py`
will drop it silently, and the only symptom is a traceback in the log.

Find it with:

```
grep -n "LOCAL PATCH" bot.py
```

### `bot.py` — `on_message`, pin-notice delete

`Thread.setup()` pins the genesis message (`core/thread.py`, in
`send_genesis_message`), which makes Discord post a *"<bot> pinned a message to
this channel"* notice. `on_message` deletes that notice to keep the channel
clean, but upstream does it unguarded:

```python
if message.type == discord.MessageType.pins_add and message.author == self.user:
    await message.delete()
```

If the notice is already gone the delete raises `NotFound` out of `on_message`,
which discord.py logs as an unhandled exception and which aborts the rest of the
handler. Already-deleted is the outcome we wanted anyway. The patch catches
`NotFound` (debug, nothing to do) and `Forbidden` (warning — missing Manage
Messages, worth knowing once but not worth a traceback per thread).

This is not caused by the plugin: it never pins, deletes, or touches thread
channels. But because handoff is now the main thing that creates threads, the
traceback reliably appears about a second after an "AI deferred" log line, which
makes it look related. It is not.

`bot.py` is not black-clean upstream, so do not run the formatter over it to fix
this — that produces a large unrelated diff and makes future merges worse.

## Storage

`bot.api.get_plugin_partition(self)` returns a single collection, not a
database, so all documents live in **`plugins.NorwegianSupport`** in Modmail's
MongoDB and are separated by a `_type` field:

| `_type`        | Fields                                                     | Retention |
|----------------|------------------------------------------------------------|-----------|
| `consent`      | `user_id`, `accepted_at`, `policy_version`                   | until withdrawn |
| `ai_transcript`| `user_id_hash`, `messages[]`, `resolved`, `created_at`, `expires_at`, `handed_off_at` | 7 days |
| `ticket`       | `nas_ref`, `log_key`, `user_id`, `channel_id`, `created_at`   | kept      |
| `meta`         | `key`, `value` — currently the user-ID hashing salt          | permanent |

### The hashing salt

`ai_transcript.user_id_hash` is `HMAC-SHA256(salt, user_id)`, not a bare hash: a
plain SHA-256 of a Discord snowflake is trivially reversible by enumeration. The
salt is generated once and stored as the `meta` document.

**Deleting or regenerating that document orphans every existing transcript** —
the hashes stop matching and the data can no longer be tied to a user, including
for deletion requests. Back it up with the rest of the database.

The website cannot go from a transcript back to a user ID. To find one user's
transcripts it must compute `HMAC-SHA256(salt, user_id)` itself using the stored
salt, and query on that.

Retention is enforced two ways. The MongoDB TTL index on `expires_at`
(`expireAfterSeconds: 0`) is primary; a `cleanup_transcripts` task sweeps the
same documents every 6 hours as a backstop, because TTL is a background monitor
some deployments disable or run behind, and a retention promise made in a
privacy notice should not depend on a setting nobody here controls.

Only `ai_transcript` documents carry `expires_at`, so consents and ticket
mappings are never touched by either mechanism.

The `nas_ref` field keeps its name for compatibility with existing documents
even though references are now `VLG-`. Renaming it needs a data migration.

The staff website reads this collection directly. Query by `_type`; there is no
Supabase copy and nothing dual-writes.

Declining the notice stores **nothing at all** — not even the user ID — since
recording a refusal would mean retaining data from someone who just refused to
let us. A declining user is therefore prompted again on their next message.

Ticket message content itself is not stored here. It lives in Modmail's own
`logs` collection and expires via `log_expiration` above.

Renaming the `NorwegianSupport` cog class changes the partition name and
orphans all existing data.
