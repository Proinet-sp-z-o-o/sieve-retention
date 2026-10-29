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
import json
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


def mailbox_status(user, mailbox):
    """(uidvalidity, uidnext) for mailbox - uidnext is the UID the next
    delivered message will get, i.e. a safe upper bound for "nothing here
    yet, but anything from here on is new"."""
    out = doveadm("mailbox", "status", "-u", user, "uidvalidity uidnext", mailbox)
    fields = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", out)}
    return fields["uidvalidity"], fields["uidnext"]


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f)


def sweep(user, mailbox, header, execute, log, incremental=False, state_path=None):
    watermark = 1
    current_uidvalidity = uidnext = None
    if incremental:
        current_uidvalidity, uidnext = mailbox_status(user, mailbox)
        state = load_state(state_path)
        if state.get("uidvalidity") == current_uidvalidity:
            watermark = state.get("watermark_uid", 1)
        elif state:
            log(f"{user}/{mailbox}: uidvalidity changed ({state.get('uidvalidity')} -> "
                f"{current_uidvalidity}), forcing a full rescan this run")

    search_args = ["search", "-u", user, "mailbox", mailbox, "header", header, ""]
    if incremental and watermark > 1:
        # Everything below the watermark was already resolved (expunged, or
        # never tagged) as of the last run - only UIDs from here up can
        # still be pending or newly arrived.
        search_args += ["uid", f"{watermark}:*"]
    out = doveadm(*search_args)
    uids = [u for u in out.split() if u.isdigit()]

    expunged = set()
    if uids:
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
                expunged.update(aged_uids)
            else:
                log(f"{user}/{mailbox}: would expunge {len(aged_uids)} uids (retention={days}d): {aged_uids}")

    if incremental:
        # Dry runs never actually remove anything, so nothing may be
        # dropped from the pending set, or a later --execute run would
        # silently skip messages that are already overdue today.
        still_pending = (set(uids) - expunged) if execute else set(uids)
        new_watermark = min(int(u) for u in still_pending) if still_pending else uidnext
        save_state(state_path, {"uidvalidity": current_uidvalidity, "watermark_uid": new_watermark})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", required=True, help="IMAP/mail user, as doveadm -u expects it")
    p.add_argument("--mailbox", default="INBOX", help="mailbox to sweep (default: INBOX)")
    p.add_argument("--header", default="X-Retention-After",
                   help='header carrying the retention window in days (default: X-Retention-After)')
    p.add_argument("--execute", action="store_true",
                   help="actually expunge; without this, only prints what would be expunged")
    p.add_argument("--log", help="append log lines to this file instead of stdout")
    p.add_argument("--incremental", action="store_true",
                   help="only rescan UIDs at or above the last run's watermark instead of the "
                        "whole mailbox (requires --state); see README for the UID-monotonicity "
                        "assumption this relies on")
    p.add_argument("--state", help="JSON file to persist the incremental watermark in "
                                    "(required with --incremental, one file per user+mailbox+header)")
    args = p.parse_args()

    if args.incremental and not args.state:
        p.error("--incremental requires --state PATH")

    if args.log:
        def log(msg):
            with open(args.log, "a") as f:
                f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    else:
        def log(msg):
            print(msg)

    sweep(args.user, args.mailbox, args.header, args.execute, log,
          incremental=args.incremental, state_path=args.state)


if __name__ == "__main__":
    sys.exit(main())
