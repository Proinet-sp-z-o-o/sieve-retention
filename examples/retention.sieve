require ["editheader"];

# Tag matching mail with how many days it should live before it's expunged.
# The message is NOT moved anywhere - it stays right where Sieve would have
# delivered it anyway (normally INBOX), fully visible, until
# inbox-retention-expunge.py decides it's past due.
#
# Replace the conditions below with your own; the header name and the
# enforcement script don't care what the rule was, only what it wrote.

if header :contains "from" "newsletter@example.com"
{
	addheader "X-Retention-After" "7";
}

if header :contains "subject" "backup report"
{
	addheader "X-Retention-After" "14";
}
