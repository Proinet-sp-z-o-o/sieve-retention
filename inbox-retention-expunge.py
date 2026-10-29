#!/usr/bin/env python3
"""Expunge messages tagged with a retention header once they're older than
the number of days the header says - directly from wherever they live
(typically INBOX), with no intermediate "move to a folder and clean that up
later" step.

Why this exists: moving a message out of INBOX to enforce a retention
policy means you never see it again before it's gone, which defeats the
point of "relevant for a while, then delete." Sieve itself has no way to
test "message is older than N days" (date/currentdate only compare against
literal values, and Sieve only runs once, at delivery, when age is always
zero) - so the only way to express both "self-expiring" and "still visible
in INBOX in the meantime" is to split the two concerns:

  1. Sieve tags matching mail declaratively, at delivery, with a header
     naming its own retention window - e.g. `addheader "X-Retention-After"
     "7";` for a 7-day rule. The message is NOT moved; it stays in INBOX.
  2. This script is the single, generic enforcement step. It knows nothing
     about *why* a message is tagged - it just reads the header + the
     message's own Date, and expunges anything past due. Add a new
     retention rule in Sieve (or in Roundcube's Filters UI, see README) and
     this script picks it up automatically; it never needs to change.

Age comparison is delegated entirely to `doveadm search ... sentbefore`
(correct date/timezone handling) rather than computed here. IMAP HEADER
search is substring, not exact-match (a query for "7" would also match a
header value of "17"), so values are read individually with `doveadm
fetch` and verified exactly in Python before being grouped; only the
resulting exact-matched UID set is ever passed to the aged `doveadm
expunge` call.

Requires: Dovecot with Pigeonhole's `editheader` extension enabled (see
README), and `doveadm` on PATH with permission to act on the target user's
mailbox (either as root, or as that user if your Dovecot setup allows
self-service doveadm - see README). Python 3, stdlib only.
"""
import argparse
import datetime as dt
import re
import subprocess
import sys


def doveadm(*args):
    return subprocess.run(["doveadm", *args], check=True, capture_output=True, text=True).stdout


def exact_values(user, mailbox, header, uids):
    """uid -> exact header value, read individually (the search that found
    these uids only guarantees a substring match)."""
    values = {}
    for uid in uids:
        out = doveadm("-f", "tab", "fetch", "-u", user, f"hdr.{header}", "mailbox", mailbox, "uid", uid)
        lines = out.strip("\n").split("\n")
        value = lines[1].strip() if len(lines) > 1 else ""
        if value:
            values[uid] = value
    return values


def sweep(user, mailbox, header, execute, log):
    out = doveadm("search", "-u", user, "mailbox", mailbox, "header", header, "")
    uids = [u for u in out.split() if u.isdigit()]
    if not uids:
        return

    values = exact_values(user, mailbox, header, uids)
    by_value = {}
    for uid, v in values.items():
        by_value.setdefault(v, []).append(uid)

    for value, group_uids in by_value.items():
        if not re.fullmatch(r"\d+", value):
            log(f"{user}/{mailbox}: ignoring non-numeric {header}={value!r} on uids {group_uids}")
            continue
        days = int(value)
        uidset = ",".join(group_uids)
        out = doveadm("search", "-u", user, "mailbox", mailbox, "uid", uidset, "sentbefore", f"{days}d")
        aged_uids = [u for u in out.split() if u.isdigit()]
        if not aged_uids:
            continue
        aged_set = ",".join(aged_uids)
        if execute:
            doveadm("expunge", "-u", user, "mailbox", mailbox, "uid", aged_set)
            log(f"{user}/{mailbox}: expunged {len(aged_uids)} uids (retention={days}d): {aged_uids}")
        else:
            log(f"{user}/{mailbox}: would expunge {len(aged_uids)} uids (retention={days}d): {aged_uids}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", required=True, help="IMAP/mail user, as doveadm -u expects it")
    p.add_argument("--mailbox", default="INBOX", help="mailbox to sweep (default: INBOX)")
    p.add_argument("--header", default="X-Retention-After",
                   help='header carrying the retention window in days (default: X-Retention-After)')
    p.add_argument("--execute", action="store_true",
                   help="actually expunge; without this, only prints what would be expunged")
    p.add_argument("--log", help="append log lines to this file instead of stdout")
    args = p.parse_args()

    if args.log:
        def log(msg):
            with open(args.log, "a") as f:
                f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    else:
        def log(msg):
            print(msg)

    sweep(args.user, args.mailbox, args.header, args.execute, log)


if __name__ == "__main__":
    sys.exit(main())
