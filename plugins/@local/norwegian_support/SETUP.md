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
| `?nas consent @user` | Supporter | Show a stored consent record |
| `?nas revoke @user` | Supporter | Withdraw consent; re-prompts next ticket |
| `?nas ticket NAS-XXXXXX` | Supporter | Resolve a reference to its Modmail log |

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

Stage 4 needs a Groq API key in `.env` alongside the existing Modmail values:

```
GROQ_API_KEY=gsk_...
```

## Storage

`bot.api.get_plugin_partition(self)` returns a single collection, not a
database, so all documents live in **`plugins.NorwegianSupport`** in Modmail's
MongoDB and are separated by a `_type` field:

| `_type`        | Fields                                                     | Retention |
|----------------|------------------------------------------------------------|-----------|
| `consent`      | `user_id`, `accepted_at`, `policy_version`                   | until withdrawn |
| `ai_transcript`| `user_id_hash`, `messages[]`, `resolved`, `created_at`, `expires_at` | 7 days |
| `ticket`       | `nas_ref`, `log_key`, `user_id`, `created_at`                | kept      |

Retention is enforced by a MongoDB TTL index on `expires_at` with
`expireAfterSeconds: 0`. Only `ai_transcript` documents carry that field, so
consents and ticket mappings are never touched by the TTL monitor.

The staff website reads this collection directly. Query by `_type`; there is no
Supabase copy and nothing dual-writes.

Declining the notice stores **nothing at all** — not even the user ID — since
recording a refusal would mean retaining data from someone who just refused to
let us. A declining user is therefore prompted again on their next message.

Ticket message content itself is not stored here. It lives in Modmail's own
`logs` collection and expires via `log_expiration` above.

Renaming the `NorwegianSupport` cog class changes the partition name and
orphans all existing data.
