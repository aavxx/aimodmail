# AI support assistant for Modmail

A [Modmail](https://github.com/modmail-dev/modmail) plugin that answers the
easy questions before a ticket is ever opened.

When someone DMs your bot, they get a short data-processing notice and a
greeting, and their message is checked against knowledge **you** supply. If the
answer is in there, they get it in seconds and no thread is created. If it is
not — or they ask for a person, or sound upset, or the question is about their
individual account — it falls through to Modmail and opens the ticket exactly as
it always did.

Every failure path ends in that fallthrough, so a user is never left without a
reply. If the AI is misconfigured, down, or slow, every message simply becomes a
normal ticket.

## What it does

- **Answers from your knowledge, and nothing else.** Anything not covered goes
  to a human. A missing fact costs you one handoff; an invented one gets stated
  to a customer as though it were true, so the assistant is built to prefer the
  former.
- **Asks before handing over.** The user is offered a human rather than being
  transferred silently, and the assistant's own reply goes out first.
- **Summarises the conversation** for whoever picks up the ticket, and gives the
  user a reference code.
- **Post-chat survey**, optional, with an explicit opt-in before any conversation
  is kept for improving the assistant.
- **Weekly digest** of the questions it could not answer — the list of things
  worth adding to its knowledge.
- **Alerts** when someone leaves a bad rating.
- **Tone moderation**, urgency tagging, and an optional partnership application
  form.

All of it is optional and switchable from Discord.

## Requirements

- A working Modmail bot (v4.x)
- A [Groq](https://console.groq.com) API key — the free tier is enough to try it
- Python 3.10+, which Modmail already requires

## Install

```
?plugin add aavxx/norwegian/aisupport@main
```

Then add your API key to the bot's `.env`, alongside Modmail's own values:

```
GROQ_API_KEY=gsk_...
```

Restart the bot, then run:

```
?ai setup
```

That asks 14 questions in the channel — your organisation's name, what the
assistant should be called, what it knows about your business, where staff
notifications go — and configures everything. Nothing needs editing in code.

`?` is Modmail's default prefix; use whatever yours is.

## After setup

| Command | What it is for |
|---|---|
| `?ai` | Everything, grouped by what you are trying to do |
| `?ai status` | Is it running and set up correctly |
| `?ai knowledge` | Read or extend what the assistant may answer from |
| `?ai set` | Settings that carry a value |
| `?ai features` | Turn optional behaviour on and off |
| `?ai stats` | How often it answers, and what it keeps getting stuck on |
| `?ai ask <question>` | Try a question yourself, without opening a real chat |

Full detail, including every setting and the privacy behaviour, is in
[SETUP.md](SETUP.md).

## Your data stays yours

Everything this plugin stores — settings, conversations, survey answers, the
consented training set — lives in your own bot's database, in a collection
Modmail hands out per plugin. Two installs of this plugin never share anything,
because they never share a database.

Conversations with the assistant are deleted automatically after a configurable
number of days. Nothing is kept for improving the assistant unless the user is
asked and says yes, and that question can be turned off entirely.

## Updating

```
?plugin update aavxx/norwegian/aisupport@main
```

Your settings and knowledge are in the database, not in the files, so an update
never resets them.

## Licence

AGPL-3.0. See [LICENSE](LICENSE).
