"""Routine type slugs — the dispatcher keys, and nothing else.

A leaf on purpose: this module imports nothing, so anything may import it.

These lived in `models.routines` beside the ORM class they describe, which read
naturally until `core.plans` needed them to declare which routines each plan
includes. That made `core` depend on `models`, against the layering everywhere
else, and it closed a cycle — `models.billing` reads `CURRENCY`/`PLAN_PRO` back
out of `core.plans` for its column defaults. The cycle stayed latent only
because nothing imported the two modules in the order that would expose it;
registering every model in `models/__init__` did exactly that, and
`core.plans` blew up half-initialised.

`models.routines` re-exports these, so importers that reasonably expect to find
a routine's slug next to its table keep working unchanged.
"""

# Routine types (also the dispatcher keys).
ROUTINE_BRIEFING = "briefing"  # daily "what needs your attention" digest
ROUTINE_NEWSLETTER_DIGEST = "newsletter_digest"  # roundup of marketing/newsletters
ROUTINE_CHASE_THREADS = "chase_threads"  # threads you're awaiting a reply on
ROUTINE_RECONNECT = "reconnect"  # nudge to reach out before threads go cold
ROUTINE_DEADLINE_SCAN = "deadline_scan"  # extract deadlines into reminders
ROUTINE_CATCHUP = "catchup"  # digest of important unread
ROUTINE_INVOICES = "invoices"  # summarize recent invoices/receipts
ROUTINE_DOUBLE_BOOKINGS = "double_bookings"  # heads-up when meetings collide
ROUTINE_SCHEDULE_TRUSTED = "schedule_trusted"  # draft slot proposals for VIP meeting requests
