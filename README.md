# sieve-retention

Self-expiring mail for Dovecot/Sieve, without ever moving the message out of
your INBOX first.

## The problem

"Some mail is only relevant for a while, then it can be deleted" sounds like
it should be a Sieve filter plus a cron job. It almost is, except:

- **Sieve can't test "older than N days."** Its `date`/`currentdate` tests
  (RFC 5260) only compare against a literal value you write into the script,
  not a relative offset from "now" - and Sieve only ever runs once, at
  delivery, at which point every message is 0 days old anyway. Age-based
  logic fundamentally has to live outside Sieve, in something that re-checks
  mail after time has passed.
- **The obvious workaround - file matching mail into a folder, then expire
  that folder by age - defeats its own purpose.** The moment a message
  leaves INBOX, you stop seeing it. "Relevant for 7 days, then gone" becomes
  "invisible from day 0, then gone" - the cleanup still works, but you never
  actually got the benefit of the mail being relevant.
- **Nothing off-the-shelf covers this combination.** Dovecot's built-in
  `autoexpunge` mailbox setting is age-only, with no per-message criteria.
  The `expire` plugin is the same: whole-mailbox, no header/sender matching.
  Roundcube has no plugin for rule-based expiry of existing mail - it comes
  up periodically in the Roundcube mailing list and forums, with no answer
  beyond "you'd need something server-side."

## The idea

Split the two concerns that kept getting tangled together:

1. **Sieve declares *what* and *how long*, at delivery, without moving
   anything.** A matching rule just tags the message:

   ```sieve
   require ["editheader"];
   if header :contains "from" "newsletter@example.com"
   {
       addheader "X-Retention-After" "7";
   }
   ```

   The message stays exactly where Sieve would have put it anyway (normally
   INBOX) - fully visible, searchable, readable - for the next 7 days.

2. **One generic script enforces it.** It doesn't know or care *why* a
   message was tagged - it just reads `X-Retention-After` and the message's
   own `Date`, and expunges anything past due. Add a new retention rule in
   Sieve (or straight from Roundcube's Filters UI - see below) and the
   script picks it up automatically; it never needs to change.

This is the same pattern a `TTL` or `expires_at` column would be in any
other system, just expressed as a mail header instead of a database field.

## Requirements

- **Dovecot with Pigeonhole** (`dovecot-sieve` on Debian/Ubuntu) new enough
  to support the `editheader` extension (RFC 5293 `addheader`/
  `deleteheader`). Tested against Dovecot 2.4.1 / Pigeonhole bundled with
  it; the extension has existed since Pigeonhole 0.5.x, matching
  roughly Dovecot 2.3+.
- **`doveadm`** on the machine the script runs on (part of `dovecot-core`),
  with permission to `search`/`fetch`/`expunge` on the target mailbox -
  either as root, or as the mailbox's own user if your setup allows
  self-service `doveadm` (many do; that's how this is deployed in
  production use).
- **Python 3**, standard library only - no `pip install` needed.
- **cron** (or any scheduler) to run the enforcement script periodically.

That's the complete list. No database, no daemon, no Roundcube plugin to
install for the enforcement side.

## Setup

### 1. Enable `editheader`

It's compiled into Pigeonhole but off by default, since it's an extension
that modifies message content. Add to your Dovecot config (e.g.
`conf.d/92-sieve-editheader.conf`):

```
sieve_extensions {
  editheader = yes
}
```

Validate and reload:

```
dovecot -n          # syntax check - should print no errors
systemctl reload dovecot
```

Scope note: this only lets a user's *own* Sieve script edit headers on mail
delivered to *themselves* - the same trust boundary `fileinto`/`vacation`
already have. If you want to additionally forbid specific header names from
being touched (e.g. as defense in depth on a shared server), see
`sieve_editheader_header { ... forbid_add = yes }` in the
[Dovecot Sieve plugin docs](https://doc.dovecot.org/latest/core/plugins/sieve.html).

### 2. Write the retention rules

See `examples/retention.sieve` for the pattern. Add rules to your active
Sieve script, each ending in `addheader "X-Retention-After" "<days>"`
instead of `fileinto`.

**Or skip Sieve scripting entirely:** once `editheader` is enabled, Roundcube's
bundled `managesieve` plugin shows an **"Add header to message"** action in
its Filters UI automatically (it's gated purely on the server reporting the
`editheader` capability - no Roundcube-side plugin or config needed). Anyone
with Roundcube filter access can build retention rules by hand: condition →
action "Add header to message" → header name `X-Retention-After` → value in
days.

### 3. Install the enforcement script

```
install -m 755 inbox-retention-expunge.py /usr/local/sbin/inbox-retention-expunge
```

Dry-run first - it only prints what it *would* expunge until you pass
`--execute`:

```
/usr/local/sbin/inbox-retention-expunge --user alice --mailbox INBOX
```

### 4. Schedule it

```cron
0 9 * * * /usr/local/sbin/inbox-retention-expunge --user alice --mailbox INBOX --log /var/log/inbox-retention-expunge.log --execute
```

One line per (user, mailbox) you want swept. If several users share a
server, run it per user in their own crontab (if your `doveadm` permissions
allow that) or loop over a user list in a root cron job.

## CLI reference

```
inbox-retention-expunge.py --user USER [--mailbox MAILBOX] [--header HEADER]
                            [--execute] [--log PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--user` | *(required)* | mail user, as `doveadm -u` expects it |
| `--mailbox` | `INBOX` | mailbox to sweep |
| `--header` | `X-Retention-After` | header carrying the retention window, in days |
| `--execute` | off | actually expunge; omit for a dry run |
| `--log PATH` | stdout | append log lines here instead |

## How age is computed

Entirely by `doveadm search ... sentbefore` - this script never parses a
`Date` header or does date arithmetic itself, so it inherits Dovecot's own
(correct, timezone-aware) notion of a message's sent date.

IMAP's `HEADER` search is a substring match, not exact - a search for `7`
would also match a header value of `17` or `27`. To stay correct, the script
first finds candidate UIDs with *any* value of the header present, reads
each one's exact value individually via `doveadm fetch`, groups UIDs by
their real value in Python, and only then runs the aged `sentbefore` query
against that exact-matched UID set - never against the header value as a
search term.

## Safety notes

- Defaults to a dry run. Nothing is ever deleted without `--execute`.
- Every execution the script actually performs is logged with the UIDs it
  touched, so a deletion is always traceable after the fact.
- `doveadm expunge` is permanent (not a move to Trash). If you want a safety
  net, point your `X-Retention-After` rules at Trash-adjacent handling by
  having Sieve `fileinto "Trash"` for genuinely unwanted mail, and reserve
  this tool for mail you're comfortable permanently losing once it expires.

## License

MIT - see `LICENSE`.
