# AI support assistant — setup

Full reference. If you only want it working, `README.md` plus `?ai setup` is
enough; this is the document you come back to when you want to know exactly what
something does.

## Install

```
?plugin add aavxx/norwegian/aisupport@main
```

Then give it a [Groq](https://console.groq.com) API key, either in the bot's
`.env` (needs shell access, needs a restart):

```
GROQ_API_KEY=gsk_...
```

or from Discord, with no shell access at all:

```
.ai devmode
.ai set groqkey gsk_...
```

See *The API key* below for which wins and how the value is protected. Then:

```
?ai setup
```

Check it afterwards with `?ai status`. **Answering messages** must read yes,
**AI** must read connected, and nothing should be listed under *Needs
attention*.

Updating later:

```
?plugin update aavxx/norwegian/aisupport@main
```

Settings live in the database rather than in the files, so updating never resets
them.

## Staff commands

The group is **`.ai`**. `.vlg` and `.nas` still work as aliases — turn them off
with `.ai set legacyaliases off` if you want a single clean name.

`.ai` on its own prints the **categories**, not every command. Pick one to open
it:

```
.ai                 → the categories
.ai general         → the commands in that category
.ai settings
.ai reports
.ai data
```

| Category | Command | Permission | Purpose |
|---|---|---|---|
| General | `.ai status` | Administrator | Is the assistant running and set up correctly |
| General | `.ai ask <question>` | Supporter | Ask it something yourself, to see how it answers |
| Settings | `.ai set` | Administrator | Names, channels, timings, anything with a value |
| Settings | `.ai features` | Administrator | Turn optional parts on and off |
| Reports | `.ai stats` | Supporter | How often it answers, and what it gets stuck on |
| Reports | `.ai digest` | Supporter | Send the weekly summary to staff now |
| User data | `.ai forget @user` | Supporter | Delete everything stored about one person |
| User data | `.ai training [n]` | Supporter | Chats people agreed could be kept |
| User data | `.ai ticket VLG-XXXXXX` | Supporter | Resolve a reference to its conversation |
| Developer | `.ai version` | Supporter | Which build is running, down to the commit |
| Developer | `.ai verbose` | Administrator | Log why the assistant handed a chat over |
| Developer | `.ai devmode` | Administrator | Show or hide the developer commands |

### `.ai setup` — the first-run wizard

Not listed anywhere by default. It is a one-time thing, and listing it
permanently would put a "set this up" prompt in front of installs that are
already set up.

```
.ai setup
```

It asks **14 questions, one at a time, in the channel**. You answer by sending
an ordinary message. At any question you can type **skip** to leave a setting
alone, **clear** to put it back to its default, or **cancel** to stop.

Every answer is saved as it is given, so cancelling or timing out keeps what you
already answered — running it again picks up from the top with your answers
already in place as the "currently" value.

A rejected answer re-asks the same question rather than moving on, so you cannot
end up believing you configured something you did not.

What it covers, in order: command name, organisation name, assistant name,
ticket prefix, privacy policy link, retention days, **what the assistant knows**,
prices, what to always escalate, staff channel, whether to offer the partnership
form (and its channel, only if yes), whether to ask users about keeping chats
for training, and an icon.

The one thing it cannot do is the API key. `GROQ_API_KEY` belongs in `.env`
because it is a credential, not a setting.

Changing the **command name** takes effect immediately — the group is
re-registered under the new name, with the previous names kept as aliases.

## What the assistant knows

This is the most important thing to configure and the easiest to get wrong. The
assistant may only answer from what is here; anything else goes to a human,
which is the safe direction — a missing fact costs a handoff, a wrong one gets
stated to a customer as though it were true.

Three sections, all set in `.ai setup` and editable afterwards:

| Section | For |
|---|---|
| `knowledge` | General facts about your business |
| `pricing` | Prices, products, fare classes — separate because it changes most and is worst to get wrong |
| `neveranswer` | Topics to always escalate, whatever the other two say |

```
.ai knowledge                              show all three
.ai knowledge add <fact>                   append one line
.ai knowledge pricing add <fact>           append to prices
.ai knowledge neveranswer add <topic>      always escalate it
.ai knowledge pricing clear                empty a section
```

### There is no length limit

Sections are unbounded. Put in as much as your business actually needs.

`.ai setup` collects a section across **as many messages as you want** — send
them one after another and say **done** when you have finished — because a
single Discord message stops at 2000 characters and an FAQ does not. `.ai
knowledge add` appends a line at a time afterwards.

Reading it back: `.ai knowledge` shows the start of each section inline, and
attaches any long section as a `.txt` file so you can check the whole thing.
Knowledge you cannot read back is knowledge you cannot check, which matters more
here than anywhere else — every line of it gets stated to a user as fact.

**What is real, and reported rather than enforced:** all of it is sent to the
model on *every single request*. So more knowledge means every reply costs a
little more and takes a little longer, and past a point it will not fit in the
model's context at all — at which point requests fail and every message goes to
a human.

The plugin tells you where you stand rather than picking a limit for you:

| Roughly | What happens |
|---|---|
| under a quarter of the context window | nothing said |
| a quarter to 60% | `.ai knowledge` notes the size and suggests trimming |
| over 60% | listed under *Needs attention* in `.ai status` |

Nothing is ever refused for being too long. If you want 100,000 characters of
knowledge, you can have it — you will just be told what it costs.

### Why this is not raw prompt editing

The instructions — how to behave, when to escalate, the output format — stay in
code. What you supply is **facts**, and they are put into the prompt as fenced
data with an explicit note that nothing inside the fence is an instruction. Four
things guard it:

1. **A read-back.** Setup shows what you typed and asks you to confirm before
   saving. This catches the paste that went in twice and the half-finished
   sentence far more often than it catches anything malicious.
2. **The fence is stripped from input.** The one string that could let typed
   text escape the data block is removed from anything you write.
3. **Instruction-shaped text is flagged.** Phrases like "ignore all previous
   instructions" or "system prompt" are pointed out at the moment you enter
   them. It still saves — a real business might legitimately mention them — but
   you are told.
4. **The rules are restated after the reference**, so the last thing the model
   reads is the instruction to escalate anything not covered.

None of that makes a wrong fact safe. It makes a wrong fact *yours*, entered
deliberately, and visible in `.ai knowledge`.

### The built-in example

The plugin ships with the original install's airline facts as the default. On any
other install those are confidently wrong answers waiting to happen, so as soon
as `brandname` says this is a different organisation, `.ai status` reports it as
the first thing needing attention, and `.ai knowledge` says so at the top.

The first `.ai knowledge add` on an unconfigured install **replaces** the example
rather than appending to it — adding your own fact to somebody else's airline is
worse than either alone.

### Developer mode### Developer mode

`.ai devmode` toggles. It takes no arguments — run it again to turn it back off.

The split is *who needs this*: an admin running their own copy needs to know the
assistant is answering well and to action an erasure request. They do not need a
git commit hash or the raw JSON the model returned. Those are developer tools and
are off by default.

While devmode is **off**, developer commands are:

- **not listed** — absent from `.ai`, from `.ai features`, and from Modmail's own
  `?help ai`; the Developer category itself does not appear
- **not runnable** — typing one directly refuses with an explanation, rather than
  quietly working. Hiding a command from a list is not a gate on its own.

While devmode is **on**, everything appears in both `.ai` and `?help ai`, and
`.ai status` and `.ai ask` grow their technical sections.

The whole **Developer category disappears** when devmode is off — it is not
shown with fewer entries under it, it is absent.

Two deliberate exceptions, both about *running* rather than listing:

- `.ai devmode` and `.ai setup` always run, whatever devmode is set to. One is
  the way back in and cannot be gated behind itself; the other has to work on an
  install where nobody has heard of devmode.
- A developer tool that is currently **on** stays runnable — but not listed — so
  turning `verbose` off does not require turning devmode on first just to reach
  it. That it is on is reported by `.ai status` instead, under *Needs
  attention*, which is where a warning belongs.

`.ai forget` is the only way to action an erasure request.`.ai forget` is the only way to action an erasure request. There is no consent
to withdraw any more, and the disclosure now links only to the privacy policy, so
requests will arrive by whatever route that page describes — but the transcripts
still exist until their expiry (`retentiondays`, 7 by default) and nothing else
deletes them on request.
`.ai revoke` still works as an alias. It also removes anything the user agreed
to leave behind in the training set — see *The post-chat survey*, which is the
one thing here with no expiry of its own.

## Required config

Modmail-native strings are set through `?config`, not written into plugin code,
so they stay editable without a reload. Run these once.

### Turn off the built-in confirm step

The plugin's gate replaces it. Leaving it on adds a second prompt on top of the
plugin's own flow.

```
?config set confirm_thread_creation no
```

### Thread creation messages

Sent by Modmail itself once a thread is created at handoff.

```
?config set thread_creation_title Ticket Opened
?config set thread_creation_response A member of our support team will be with you shortly.
?config set thread_creation_footer Your message has been sent to the support team
```

### Cancellation

Shown when thread creation is stopped.

```
?config set thread_cancelled Support Cancelled
```

### Log retention

Modmail's default is `Never`, which keeps every thread log forever. The bot no
longer states a retention period itself — the opening disclosure links to your
privacy policy instead — so **whatever that page says has to match this
setting.**

```
?config set log_expiration P7D
```

**Then restart the bot.** Modmail's `log_expiry` task calls `.stop()` on itself
during its first run if `log_expiration` is unset, and never restarts within
that process — so setting this on a running bot has no effect until a restart.

Confirm it took with `.ai status`, which reports the effective retention.

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

## The API key

The assistant needs a [Groq](https://console.groq.com) API key. There are two
places to put it, and **the one set in Discord wins**.

### In `.env` (needs shell access)

```
GROQ_API_KEY=gsk_...
```

`core/config.py` calls `load_dotenv()` at import, so `.env` is enough — no
export needed. The bot has to be restarted to pick up a change.

### In Discord (no shell access needed)

```
.ai devmode
.ai set groqkey gsk_...
```

Stored in this bot's own database like every other setting, so it survives
restarts and is never shared with another install. It takes effect immediately —
no restart, no reload.

It is behind `devmode` because it is not part of everyday configuration and does
not belong in the menu everyone sees. `.ai devmode` toggles that; it is a switch
you have to know about rather than a real barrier.

### Why the database wins

The whole reason this setting exists is for people who cannot reach `.env`. If a
stale `.env` value silently beat the one they had just set, the person unable to
edit `.env` would also be unable to work around it — the exact situation the
setting is meant to solve. So the stored key takes precedence, and `.ai status`
names which source is in force so it is never a guess.

To fall back to `.env` again, clear the stored one:

```
.ai set groqkey default
```

### What is done with it

- **Never displayed.** `.ai set`, `.ai set groqkey` and `.ai status` show
  `set` or `not set` and, at most, which source it came from. There is no
  command anywhere that prints the value.
- **Never logged.** The line recording the change says *"set groqkey to a new
  value"*. Every other setting logs its value; this one is special-cased.
- **Never echoed in an error.** A rejected key is still a key, so the validation
  messages describe the problem without quoting what you typed.
- **Your message is deleted** after it is saved, so the key does not sit in
  channel history. If the bot lacks **Manage Messages**, the confirmation says so
  and tells you to delete it yourself — it does not fail quietly.

Two things worth knowing. The key is stored **as plain text in your database**,
the same as every other setting; treat database access as equivalent to key
access. And Discord may have the message in transit and in client caches before
the delete lands, so a key pasted into a busy public channel should be rotated
rather than trusted.

Without a key from either source, nothing breaks: every request simply escalates
to a human.

That is the only thing this plugin needs from `.env`. Everything else is set
from inside Discord — see *Settings* below.

## Settings

Everything configurable lives behind two commands, and is stored in the database
rather than in `.env` or in the plugin source, so it survives reloads, restarts
and a `git pull`. Nothing here needs a redeploy.

Only real credentials stay in `.env`: the bot token, `GROQ_API_KEY`, and the
Mongo URI. Those are secrets rather than settings.

### `.ai set` — settings that carry a value

`.ai set` on its own lists every setting with its current value and whether it
is still on the built-in default.

**Identity** — what you change first when installing this on another bot:

| Setting | Default | What it does |
|---|---|---|
| `brandname` | this server | Your organisation's name, as users see it |
| `assistantname` | Support Assistant | What the assistant calls itself |
| `privacyurl` | unset | The privacy policy linked in the opening notice |
| `ticketprefix` | VLG | Prefix on ticket references, e.g. `VLG-A3K9PQ` |
| `greeting` | "Hola! I'm {brand}'s…" | The assistant's opening line; `{brand}` is substituted |
| `yesemoji` / `noemoji` | guild emoji | Emoji on the "connect me to a human" buttons |
| `iconurl` | unset | Image shown as the icon on the assistant's messages |
| `legacyaliases` | on | Whether the old names still work alongside the current one |
| `commandname` | ai | What you type to reach the plugin — `.ai status`, `.ai set` |

`iconurl` unset means the bot's own Discord avatar is used, which is usually
right. Set it when the avatar and the logo you want on messages differ — note
the Developer Portal's *App Icon* and *Bot* avatar are two different images, and
only the Bot one reaches messages, which is the usual reason for wanting this.
`.ai set iconurl default` goes back to the avatar.

`commandname` re-registers the group live, no restart. The previous names stay
registered as aliases so nothing in flight breaks. Note that Modmail keys any
per-command permission overrides by name, so if you have set some with
`?permissions`, they will need re-adding under the new name.

`legacyaliases off` makes every name except the current one refuse with a note
pointing at the right command. It is stored per install like every other
setting, so turning it off here does not affect anyone else running this
plugin.

A custom emoji only works if the bot is in the server that owns it. If it is
not, the buttons fall back to plain unicode automatically rather than failing to
send — `.ai status` tells you when that has happened.

**Everything else:**

| Setting | Default | What it does |
|---|---|---|
| `staffchannel` | the partnership channel | Where the weekly digest and low-rating alerts are posted |
| `partnershipchannel` | the built-in id | Where partnership applications are posted for staff to claim |
| `retentiondays` | 7 | Days a conversation is kept before automatic deletion |
| `typingdelay` | 1.5s | How long the typing indicator runs before each composed message |
| `lowratingthreshold` | 2 | A survey score at or below this raises an alert |
| `profanitystrikes` | 2 | Swears allowed in one chat before it closes; the first is a warning |
| `inactivitywarning` | 60 min | Quiet minutes before the assistant asks if the user is still there |
| `inactivityclose` | 180 min | Quiet minutes before the chat closes |
| `digestday` | 0 (Monday) | Day of the week the digest posts |
| `digesthour` | 9 | Hour (UTC) the digest posts |

```
.ai set staffchannel #staff-alerts
.ai set retentiondays 14
.ai set staffchannel default
```

A channel can be a mention, a raw id, or a plain name in the server you run the
command in. Numbers are range-checked and a bad value is refused with a message
saying what a good one looks like, rather than being stored and silently
misbehaving. Setting a channel the bot cannot see is accepted but warned about
loudly, because that is the failure that otherwise shows up as "the digest never
arrived". `default` puts a setting back.

If `retentiondays` is changed, **the privacy policy the opening disclosure links
to should say the same number.** Nothing checks that for you.

`ticketprefix` only affects references issued from then on. Codes already handed
to users keep the prefix they were given, and `.ai ticket` still resolves them.

## Installing this on another bot

The plugin ships configured for one specific server, but nothing about that is
baked into the code any more. On a fresh install:

1. `GROQ_API_KEY` in `.env`, alongside Modmail's own values. That is the only
   thing this plugin needs from the environment.
2. **`.ai setup`** — 14 questions covering the command name, your names, the
   privacy policy link, retention, **what the assistant knows**, the staff
   channels, and the optional features. That is nearly all of it.
3. `.ai set ticketprefix` and `greeting` if you want them, and `.ai set yesemoji`
   / `noemoji` — the built-in emoji ids belong to another server, so they
   resolve to nothing and the buttons fall back to plain unicode automatically.
4. `.ai status` — names anything still wrong, in plain words.

Staff channels are looked up in the server `PARTNERSHIP_GUILD_ID` names, and
then in whatever Modmail's own `GUILD_ID` is set to. A fresh install matches the
second, so channel lookup works without touching the constant.

The assistant's **knowledge** is configuration now — see *What the assistant
knows*. What stays in code is the prompt's *instructions*: how to behave, when to
escalate, the output format. Those are tuned prose rather than settings, and
keeping them out of reach is what makes supplied knowledge safe to accept.

Embed colours are Modmail's, not this plugin's: `?config set main_color`,
`error_color` and `mod_color` already govern every embed here.

### `.ai features` — things that are just on or off

`.ai features` lists every optional feature, its state, and a plain description
of what turning it off actually stops.

| Feature | Default | What it does |
|---|---|---|
| `survey` | on | Ask the user to rate the chat after it ends |
| `training` | on | Ask, in that survey, whether the chat may be kept to improve the AI |
| `profanity` | on | Warn on swearing, and close the chat if it continues |
| `lowratingalerts` | on | Alert staff as soon as a bad score is left |
| `digest` | on | Post the weekly summary of unanswered questions |
| `partnershipform` | on | Offer the application form when someone asks about partnering |
| `urgencytags` | on | Tag a ticket as urgent when the user sounds angry or rushed |
| `verbose` | off | Extra diagnostics in the bot log, including message content |

```
.ai features
.ai features digest off
.ai features digest        # flips whatever it is now
```

`training` off means the question is never put to the user at all, and nothing
is ever written to the training set — not asked-and-ignored. A form opened just
before you turned it off is re-checked on submit, so it cannot slip through.

`survey` is the parent of `training` and `lowratingalerts`: turning it off stops
both, and the command says so rather than leaving them reading as on. Turning
`digest` or `lowratingalerts` on while the staff channel is unreachable warns
you at the point you turn it on.

`verbose` also has its own `.ai verbose` command, which carries a longer
warning about writing message content to the log. It is the same setting, and
like the command it is hidden from this list while devmode is off — unless it is
currently on, in which case it stays visible so it cannot be left running
unnoticed.

### `.ai status`

Two views, depending on devmode.

**Default** — written for whoever runs the bot, not whoever wrote it. Is it
seeing messages, is the AI connected, how long has it been up, which features
are on, and a plain-language list of anything that needs fixing with the command
to fix it. No commit hashes, no collection names.

**With devmode on** — the full technical view: DM hook, partition, commit,
index state, document counts, emoji resolution, and the configuration checks
below.

### Config sanity

`.ai status` now ends with a **Configuration** block: one line per setting that
can be wrong without anything visibly breaking — both guild IDs, the staff
channel, `GROQ_API_KEY`, `log_url`, and retention. It also carries a **Features**
line showing what is on and off at a glance. A bad value does not
raise anywhere; it just means a notification silently never arrives, so this is
the place that says so. The heading counts the problems, so a healthy bot is one
glance rather than a read.

The **Commit** field alongside it is the short hash of the running checkout. After
a deploy, compare it against what you shipped rather than guessing from
behaviour whether the restart took.

## Running twice

Two bot processes on one token both receive every gateway event, so every DM is
answered twice. Nothing in Discord's API prevents this and neither process logs
anything unusual.

`core/single_instance.py` takes an `flock` before `main()` does anything else. A
second process refuses to start and names the PID holding the lock:

```
another bot process is already running (pid 4171, lock /tmp/modmail-9c1f2a0b4e77.lock)
```

It **refuses rather than killing** the other process — killing by a PID read out
of a file is how you take down something that merely reused the number. Stop the
running one deliberately, then start again.

An `flock` is chosen over a PID file because the kernel releases it when the
process ends *however* it ends, so a bot that was killed or OOMed leaves nothing
stale behind. The PID in the file is only there to name the holder in the message
above.

The lock path is derived from the install directory, so two different bots on one
host do not block each other. `MODMAIL_LOCK_FILE` overrides it, and
`MODMAIL_ALLOW_MULTIPLE=1` skips the check entirely — with a warning, since the
duplicate messages are the whole thing it exists to prevent.

## The FAQ — read this before going live

What you set with `?ai setup` and `?ai knowledge` is the **only** thing the
assistant is allowed to answer from. It is instructed to hand off anything not
covered, so the two mistakes are not symmetrical:

- **A wrong entry becomes a wrong answer, stated confidently, to a real user.**
- A missing entry costs nothing: that question escalates to a human.

So when something is unknown, leave it out rather than guessing. The question
simply goes to a person, which is what would have happened anyway.

### The four states

Every model reply carries one `status`, replacing the old answer-or-give-up
boolean:

| status | what happens |
|---|---|
| `answered` | Reply sent, conversation continues, no thread. |
| `chat` | Thanks, greetings, small talk. Answered warmly, no airline fact involved, no agent offered. |
| `unclear` | It did not understand. The reply asks them to rephrase. Only after `MAX_CONSECUTIVE_UNCLEAR` (2) in a row is an agent offered. |
| `escalate` | It understood and needs a person. |

**The model's reply is always sent, including on `escalate`.** Previously the
plugin discarded it and sent a canned "Looks like I can't help you with that",
so even a genuinely useful attempt never reached the user — which is what made
the assistant read as giving up instantly. Now the attempt goes out first and the
agent offer follows it, shortened to "Would you like me to connect you with a
member of our team?" since the reply already explains itself.

The prompt also requires it to try: answer whatever part of a question is
covered before saying what it cannot do, ask for a clarification if that would
unlock an answer, and never write a reply that is only "I can't help".

`unclear` is deliberately separate from `escalate`. Not understanding is not the
same as understanding and lacking the fact, and only the second is worth a
human's time. A legacy `{"resolved": bool}` payload is still accepted and mapped,
so a model that falls back to the old shape is not treated as an error.

### Partnerships

A message containing a word starting `partner` / `collab` / `affiliat` is routed
to a form rather than a conversation, **before** the escalation check. The stems
are open-ended (`\bpartner\w*`), so "partnered" and "collaborating" match as well
as "partnership"; the one exception is the bare noun after a possessive — "my
partner is on the flight" is a passenger, not a proposal, and is left to the
assistant. `.ai ask "…"` reports the partnership route, so any wording can be
checked without DMing the bot. The five
questions are what a human would ask anyway, so collecting them up front beats a
ticket that opens by asking them one at a time — even when the request is phrased
as wanting to speak to someone.

The reply offers a button, the button opens a Discord modal, and the submission
is posted to the configured staff channel. `PARTNERSHIP_GUILD_ID` and
`PARTNERSHIP_CHANNEL_ID` are where it lands; if the bot cannot see that channel
the user is told the submission failed and offered a human, rather than being
thanked for something that never arrived. `.ai status` resolves that channel and
names it, so a form that would fail on submission is visible before anyone uses
it.

The button view is persistent, so a form offered overnight still opens.

### The follow-up question

"Is there anything else I can help you with?" is sent by the plugin after every
answered or chat reply, not by the model. It used to be left to the model's
discretion, which is why it appeared only sometimes. The prompt now explicitly
tells the model **not** to write it, so it never doubles up.

It is deliberately not sent after `unclear` (it would contradict the question
just asked) or `escalate` (the agent offer follows instead).

### Wording vs facts

The prompt tells the model to recognise shorthand, abbreviations, partial names,
synonyms and typos as pointing at the right entry — "grande" is Fly Grande,
"unban" is the appeals page, "dress code" is the uniform policy — so a question
is not deferred merely because it was not phrased the reference's way.

That is deliberately separate from having the fact. The prompt says so
explicitly, with a worked example: "what are the levels called?" is recognisably
about ranks, the model should understand it, and it still defers because the rank
names are not in the reference. **Recognition never licenses an answer the
reference does not contain**, and the model is told not to stretch a neighbouring
entry to cover a term that is absent.

The practical consequence: if users ask about something by a name the FAQ does
not use *at all*, that is an FAQ gap, not a prompt problem. Adding the entry is
the fix; adding a synonym mapping for a subject that is not in the reference
would just produce confident invention.

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

### Diagnosing a deferral

When the assistant hands off, the log line only says it deferred. The user's
message and Groq's verbatim `{resolved, reply}` response are logged alongside
it at DEBUG.

Modmail applies `log_level` once at startup, so `?config set log_level debug`
needs a full restart and turns on discord.py's own debug firehose too. To avoid
both:

```
.ai verbose on
```

That promotes only these two lines to INFO, takes effect immediately, and
survives until toggled off or the bot restarts.

**It writes ticket message content into the bot log.** That content is covered
by the privacy notice but the log is not on the 7-day deletion path, so turn it
off once you are done:

```
.ai verbose off
```

### The opening disclosure

Every conversation opens with two fixed, informational messages before the
greeting: the data-processing notice and the AI-tool notice (`DISCLOSURE_PARTS`).
Nothing waits for input and nothing is stored per user.

The opening is paced in two halves, on purpose:

| Message | Typing indicator | Delay before it |
|---|---|---|
| Data-processing notice | no | none |
| AI-tool notice | no | 1s (`DISCLOSURE_GAP_SECONDS`) |
| Greeting ("Hola! ...") | yes | 1.5s (`typingdelay`) |
| Follow-up ("How can I help you? ...") | yes | 1.5s (`typingdelay`) |

The two disclosures are fixed legal text that was written long before the user
said anything, so showing a typing indicator for them is both slow and a small
misrepresentation of what is happening — they go out back to back. The greeting
and the question after it are the assistant actually addressing the user, so
they get the indicator and the normal delay.

Every other message the assistant composes uses the same `typingdelay`, so the
whole conversation reads at one pace. Change it with `.ai set typingdelay`.

It repeats on **every** new conversation rather than being shown once, on the
same open/closed boundary as the greeting. See *Ending a conversation* below for
the three ways one ends.

There is deliberately no accept/decline step. The disclosure states that
processing rests on the contractual relationship, not on consent, so there is no
decision to capture and nothing to look up or withdraw. Data rights are exercised
through the linked privacy policy rather than in chat; staff action an erasure
with `.ai forget`.

Documents written by the removed consent gate are not deleted automatically.
`.ai status` counts them under *obsolete consent records* if any remain, and
`.ai forget` clears them per user. To drop them all at once:

```js
db.getCollection("plugins.AISupport").deleteMany({_type: "consent"})
```

### The post-chat survey

When the assistant ends a conversation — a closing phrase, or the inactivity
close — the closing messages are followed by one more offering a survey, with a
button. **Only assistant-ended conversations reach this.** A conversation a human
took over ends through `_close_transcript(handed_off=True)` and never passes
through `_close_conversation`, so closing a ticket is Modmail's own flow and
produces nothing here.

The survey is a Discord modal with two dropdowns: a 1-5 rating, and *"Can we use
this chat to help improve our AI? This is 100% anonymous."* Discord caps a modal
label at 45 characters; `SURVEY_RATING_QUESTION` and `SURVEY_TRAINING_QUESTION`
are asserted against that at import, so an over-long rewrite fails the plugin
load rather than breaking the form for a user.

The button is a persistent dynamic item, so a survey offered overnight still
opens after a restart. The conversation it belongs to is encoded in its
`custom_id` and checked against whoever clicks it — someone else's survey is
refused rather than answered.

#### The questions

`SURVEY_RATING_QUESTIONS` holds three 1-5 questions — speed, helpfulness, and
overall — each with its own scale wording, because a generic
satisfied/dissatisfied scale against "how quickly did we reply" is what makes a
form feel bolted together. The yes/no sits last so the scale questions read as
one block and the question with a consequence comes at the end.

A modal takes at most **5 components**, which is the hard ceiling on this: three
ratings plus the yes/no leaves room for one more question, no more. That limit
is asserted at import alongside the label and description caps.

`SURVEY_HEADLINE_KEY` (`overall`) is the score that stands for the conversation:
what `.ai stats` averages first and what the low-rating alert fires on. Each
answer stores the full `ratings` map *and* that one score as `rating`, so
answers submitted when there was only one question still read correctly.

Changing a question's `key` orphans the answers already stored under it. The
wording above it can be rewritten freely.

#### Low-rating alerts

A headline score at or below `LOW_RATING_THRESHOLD` (2) posts to the staff
channel as soon as it is submitted, with all three scores. If the user agreed to
us keeping the chat it carries the conversation too; if they did not, it says so
rather than leaving staff wondering where the transcript went. The alert is
raised after the user has already been thanked and can never affect their reply.

#### The weekly digest

Monday 09:00 UTC (`DIGEST_WEEKDAY`, `DIGEST_HOUR_UTC`), posted to the same
channel: the deferral rate, survey averages, and the same FAQ-gap analysis
`.ai stats` prints. Both call `_faq_gaps`, so the digest cannot drift from what
someone running the command by hand sees.

It rides the existing 5-minute maintenance sweep rather than a second timer, and
is gated on a timestamp in the database, so a restart mid-week neither skips a
digest nor sends two. The first ever run seeds that timestamp and sends nothing —
otherwise loading the plugin on a Monday morning would fire a digest covering a
few hours. `.ai digest` sends one on demand without moving the schedule.

Closing is a single atomic update, so a conversation the inactivity sweep and a
goodbye both reach is closed, and surveyed, exactly once.

#### What the answers do

**Yes** copies the conversation into `training_transcript`, which carries no
`expires_at` and so survives both the TTL index and `_expire_transcripts`.
**No** copies nothing. The rating is stored either way, with no conversation
content — set `KEEP_RATINGS_WITHOUT_CONSENT = False` to drop even that on a no.

Read them with `.ai training` (count, plus recent conversations rendered
inline). `.ai status` counts both under *survey answers* and *kept for review*.

**Nothing acts on this data automatically.** `.ai training` is a reading list.
The FAQ and prompt are edited by hand, the same way `.ai stats` gaps are fixed
today.

#### Two things to check before going live

1. **The privacy policy you set with `privacyurl` must mention this.** The
   opening disclosure says processing rests on the contractual relationship and
   points at that page. Keeping consented chats past the normal transcript
   retention is a separate purpose with a separate lawful basis —
   the user's explicit yes — and that page should say so. The in-chat question is
   where consent is captured; the policy page is where it is explained.
2. **`.ai forget` reaches the training set**, which is why the copy keeps
   `user_id_hash` rather than nothing at all. A copy nobody can find is a copy
   nobody can delete, and this is the one that outlives the others. The hash is
   the same keyed HMAC used everywhere else — not reversible without the salt,
   and no name or user ID is stored beside it. That is what "anonymous" means in
   the survey wording. To make the set genuinely unlinkable instead, drop
   `user_id_hash` from the copy in `record_survey` and accept that an erasure
   request can no longer reach it.

### Tone moderation

`PROFANITY_PATTERNS` is checked on every message, after the opening disclosure
so a first message that swears still gets it, and before every other route so
nothing else can answer a message about to be moderated.

The first swear warns, warmly, and says plainly that a second closes the chat.
The second closes it **exactly as an inactivity timeout does** — same closing
messages, same survey, same fresh start on the next message. Nothing is blocked
or banned; the user can simply message again.

The count lives on the open transcript as `profanity_count`, so it resets with
the conversation: someone who swore last week starts clean, someone who swears
twice in one chat does not.

The list is deliberately short and word-boundary anchored. A false positive here
warns, then closes the conversation of somebody who did nothing — much worse than
missing a word — so add stems cautiously and keep the `\b` anchors.

### Triage tags on handoffs

A handed-off ticket opens with the assistant's summary and a **Triage** line:

| tag | when |
|---|---|
| 🔥 ANGRY OR URGENT | `URGENCY_PATTERNS` matched anywhere in the conversation |
| 🤝 PARTNERSHIP | a partnership lead that could not be routed to the form |
| 👥 ASKED FOR A HUMAN | the user asked outright |
| ❔ REPEATEDLY UNCLEAR | `MAX_CONSECUTIVE_UNCLEAR` unclear turns in a row |
| 📝 NOT IN THE FAQ | the assistant understood and had no answer |
| ⚠️ ASSISTANT OFFLINE / ERROR | Groq unreachable or misconfigured |

Urgency is stored separately from `handoff_reason` because it is orthogonal to
it — someone can be furious *and* asking about a partnership, and both belong on
the ticket. An urgent ticket is also coloured with `error_color`, so it stands
out in the channel list before anyone reads a word.

The urgency check never changes what the user is told, so a false positive costs
nothing beyond a misleading tag. That is why it can afford to be broad where the
profanity list cannot.

### Claiming a partnership lead

Each submission is posted with a 🤚 reaction already on it and an *Unclaimed*
footer. The first staff member to react owns it: the footer becomes *Claimed by
…* and any later reaction is removed, so the post always shows one owner.

The claim state lives in the post's own footer rather than in the database. It is
what staff read, it survives restarts for free, and there is no second copy that
can disagree with the channel. The listener is `on_raw_reaction_add`, not
`on_reaction_add`, because the cached-message variant silently ignores anything
not in memory — which is most of that channel after a restart.

Two people reacting at the same moment are serialised behind a lock, since the
check and the edit are separated by awaits and both would otherwise be told they
had it. Removing the losing reaction needs *Manage Messages*; without it the
claim still works and the extra reaction just stays.

### `.ai ask`

Runs a question through the pre-screen and prints the raw `{resolved, reply}`
plus which route it took. Creates no conversation, no transcript, no session,
sends the user nothing — so FAQ wording can be iterated on in one channel
instead of a round trip through a test account's DMs.

It deliberately mirrors `_ai_prescreen`'s ordering, so the route it reports is
the route a real message takes. Greeting-only, closing phrases, partnership terms
and escalation phrases are named as such rather than being pre-screened, because
in a real conversation those never reach Groq either.

### Is my change actually running?

**Check this before diagnosing anything else.** A pull does not change what the
bot is running; only `?plugin update aavxx/norwegian/aisupport@main` (or a restart)
does. Until then the bot keeps serving the previous version, which looks exactly
like the new one being broken — the old button emoji, the old routing, no
partnership form. `.ai status` now leads with a red warning when the file on
disk is newer than the running code, for that reason.

`.ai version` answers it in detail. The field that matters is the comparison between
**file last modified** and **plugin loaded**: editing or pulling the file changes
nothing until the plugin is reloaded, and every other signal looks healthy while
stale code keeps running. If the file is newer, it says so in red and gives the
reload command.

It also prints a content hash of the running file, so a shipped change can be
confirmed by comparing hashes rather than by guessing from behaviour, plus the
repo commit when the checkout is a git one.

### Buttons not appearing

`.ai status` answers this directly. Two independent causes:

- **Confirmation Yes/No** — the guild emoji are resolved through `bot.get_emoji`
  and reported as usable or not, naming the server that owns them when they are.
  Discord rejects a component carrying an emoji the bot has no access to, and
  that fails the **entire message**, so an emoji from a server the bot is not in
  would mean the prompt never appears and the user is escalated silently. The
  pair is therefore resolved *before* the send and swapped for ✅ / ❌ when it
  cannot be used, with a warning in the log naming the emoji; the send-and-retry
  path remains as a second net.

  **So plain ✅ / ❌ on the buttons is the symptom, not the setting.** It means
  the bot is not in the server that owns `BUTTON_YES_EMOJI` /
  `BUTTON_NO_EMOJI` — `.ai status` says which of the two failed. Fix it by
  inviting the bot to that server, or by putting ids from a server it is already
  in into those two constants and reloading. Both are swapped as a pair: one
  guild emoji beside one unicode mark reads as a rendering fault.
- **Feedback 👍/👎** — status reports whether the persistent view is registered.
  These only ever appear on answers sent *after* the feature shipped; older
  transcripts have no `answer_message_ids`, so a click could not be attributed to
  them even if the buttons were there.

### `.ai stats`

The loop-closer: it tells you which FAQ entries to write next.

- **Handoff rate** — how many finished conversations reached a human.
- **Deferral rate** — the subset where the assistant genuinely could not answer.
  These are separated on purpose: someone typing "agent" is a routing preference,
  and a Groq outage is an incident. Neither is an FAQ gap, and folding them into
  one number would make it useless as a quality signal.
- **Why it handed off** — the breakdown by cause.
- **Answer feedback** — 👍/👎 totals, the only check on the model's own claim.
- **Common terms in unanswered questions** — most questions are worded uniquely,
  so term frequency is what actually points at the missing entry. Repeats are
  listed separately when they occur, and the most recent unanswered questions are
  shown verbatim.

Everything is a **rolling window**: transcripts are deleted after `retentiondays`
(7 by default), so this
is never all-time. The embed says so, because a stats screen that silently means
"last week" is worse than no stats.

Conversations that predate this feature show as `unrecorded` under *Why it handed
off* — they were never tagged, so they are excluded from the deferral rate rather
than guessed at.

This prints user questions verbatim to whatever channel it is run in, so it is
Supporter-gated, same exposure as reading a ticket.

### Conversation history

The live gate replays the transcript into every Groq call; `.ai ask` passes an
empty history. That was the *only* difference between them, and it made real
conversations answer worse than dry runs after the first turn:

- The model must emit `{resolved, reply}`, but replies were stored as the plain
  text inside that object and replayed as bare prose. The model saw its own
  previous turns breaking the contract it was being held to.
- A user turn is appended on paths where no answer follows — a contentless
  hello, an escalation phrase, a Groq failure — which stacked consecutive user
  messages with no assistant turn between them.

`_replay()` rebuilds assistant turns into their original json shape and drops
unanswered user turns, so the exchange the model sees strictly alternates and
every assistant turn is valid against the schema. The dropped turns stay in the
transcript for summaries and stats; only the replay is filtered.

A test asserts a first live message produces a byte-identical payload to
`.ai ask`, so the two cannot silently diverge again.

### Ending a conversation

A pre-screen conversation ends in one of three ways. All of them send the same
three messages (`CLOSING_PARTS`) — a warm sign-off, "Bye for now!", and a
disconnect notice explaining that messaging again starts a new one — then stamp
`closed_at`, delete the session, and let the next message begin fresh with the
disclosure.

1. **Handoff.** Also stamps `handed_off_at`, so "did this become a ticket" stays
   answerable rather than being flattened into "this ended".
2. **The user asks to stop.** `CLOSING_PATTERNS` covers a bare "no", "that's
   all", "nothing else", "close the chat", "I'm done", "bye". Checked only once a
   conversation is already open, so a first message of "no" cannot close
   something that has not started.
3. **Inactivity.** Warned at `INACTIVITY_WARNING_AFTER` (1 hour), closed at
   `INACTIVITY_CLOSE_AFTER` (3 hours). Any reply resets the clock and clears a
   pending warning, so someone who comes back gets the full hour again.

**This is the plugin's own pre-screen stage only.** Modmail's `thread_auto_close`
governs open tickets and is untouched — once a thread exists the plugin does not
intercept anything.

`maintenance_sweep` runs every `SWEEP_INTERVAL_MINUTES` (5) and does both the
inactivity checks and the transcript retention sweep. One timer rather than two:
the retention delete is indexed and costs nothing at this cadence, and the
inactivity warning needs finer resolution than the 6-hourly loop it replaced.

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

Every embed the plugin builds carries the **assistant's author row** — name plus
the bot's own avatar, read from `bot.user.display_avatar` so it follows whatever
is set in the Developer Portal without a redeploy.

| Message | Colour | Title | Footer |
|---|---|---|---|
| Greeting, prompts, handoff copy, consent notice | `main_color` | none | none |
| Model-generated replies | `mod_color` | none | `<assistant> can make mistakes…`, no icon |

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
element is sent as its own message. The prompt asks it to use the array form
whenever an answer runs past about three sentences.

Because it obeys that inconsistently, the same rule is **also enforced in code**:
a single reply longer than `REPLY_SPLIT_THRESHOLD` (240 characters) is split into
two messages by `_split_long_reply`. It breaks at a blank line if there is one,
otherwise at the sentence end nearest the middle, and never inside a markdown
link — the FAQ answers are full of links and half of one in each message renders
as neither. If there is no clean break, or either half would come out under
`REPLY_SPLIT_MIN_PART` (80 characters), the reply is left whole: one long message
beats a mangled pair.

The threshold is deliberately eager. The failure it exists to fix is a long
answer arriving as one block, and an over-split reply is just two short messages,
which is how the rest of the conversation already reads.

A conversation ends when a human takes over: the handoff stamps `handed_off_at`
on the transcript, so the next time that user writes in they are greeted afresh.
A conversation the assistant resolved stays open, so follow-up questions do not
re-greet; it lapses naturally when the transcript expires after `retentiondays`.

### Embed icon

The author-row icon comes from `bot.user.display_avatar`, so it follows the
Developer Portal without a redeploy. `.ai status` prints the URL it resolves to.

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

## Storage

`bot.api.get_plugin_partition(self)` returns a single collection, not a
database, so all documents live in **`plugins.AISupport`** in your bot's own
MongoDB — the one its `CONNECTION_URI` points at — separated by a `_type` field.

This is what keeps installs apart. The collection is reached through the bot's
own database connection, so two bots running this plugin share nothing unless
they were deliberately pointed at the same database.

| `_type`        | Fields                                                     | Retention |
|----------------|------------------------------------------------------------|-----------|
| `consent`      | `user_id`, `accepted_at`, `policy_version`                   | until withdrawn |
| `ai_transcript`| `user_id_hash`, `messages[]`, `resolved`, `created_at`, `expires_at`, `closed_at`, `handed_off_at`, `handoff_reason`, `urgent`, `profanity_count` | `retentiondays` (7) |
| `session`      | `user_id`, `started_at`, `last_activity_at`, `warned_at`      | until the conversation closes |
| `survey`       | `user_id_hash`, `transcript_id`, `ratings{}`, `rating`, `training_consent`, `answered_at` | kept |
| `training_transcript` | `user_id_hash`, `transcript_id`, `messages[]`, `message_count`, `ratings{}`, `rating`, `handoff_reason`, `consented_at`, `conversation_started_at`, `conversation_ended_at` | **kept indefinitely** |
| `ticket`       | `nas_ref`, `log_key`, `user_id`, `channel_id`, `created_at`   | kept      |
| `meta`         | `key`, `value` — currently the user-ID hashing salt          | permanent |

### Why `session` holds a raw user id

Transcripts are keyed by a one-way hash, and you cannot DM a hash. The inactivity
sweep has to reach the user, so the open conversation is tracked in a separate
`session` document holding the raw id — but **no message content**, and it is
deleted the moment the conversation closes. The identifiable part is therefore
scoped to conversations that are currently open, while what was actually said
stays under the hash.

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

Renaming the `AISupport` cog class changes the partition name and
orphans all existing data.
