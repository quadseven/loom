# Operator REST budget samples

The station samples the configured operator's GitHub core REST quota hourly,
including when lanes are idle. This is separate from the station GitHub App's
budget and does not change dispatch or merge gates.

The sampler uses the existing personal-token resolver. It creates no credentials,
changes no authentication configuration, and makes only GET requests. The first
non-bot login in `LOOM_BRIEF_AUTHORS` identifies the expected operator. A `/user`
response must identify that user before a budget is attributed to the operator.

The private station state file `operator-budget.json` contains up to 169 samples
and 169 safe failed-read records (a full week of hourly samples with both endpoints). Each sample has an observation epoch, remaining,
limit, reset epoch, and whether work was active. No account names, credentials,
raw headers, or exception text are stored. An atomic write records each attempt
before network work; failed reads retry no sooner than five minutes later and do
not stop the station. Successful reads are due again after one hour.

A SHA-256 digest binds the existing credential to the expected operator. The
binding lasts 24 hours and lets subsequent station processes read `/rate_limit`
without spending quota on `/user`. Thus an exhausted budget can still be observed
after a previously verified sample. Credential rotation, operator changes, invalid
state, or expiry require verification again. An initial observation at zero cannot
be attributed safely if `/user` is refused: this is logged as unknown, never as an
App budget or a successful operator sample. The same limit applies after binding
expiry. The sampler does not replace, refresh, or broaden credentials to recover.

Below 10 percent remaining while work is active, a durable factory signal is
kept. Activity requires an assigned local ticket and a live agent or watchdog process
group in that lane. A queued brief, idle runner, missing/unreadable process ID,
or stale repo scan is insufficient. Process checks use the existing station helpers. Remote-only activity remains
unknown: the existing runner ledger does not distinguish dispatched-but-queued
work from a running agent, and this observer does not change runner dispatch
logic. Sampling still runs regardless of lane type or activity. Idle low observations do not create a
signal; if work becomes active before that observation expires or its quota resets,
the station may flag that fresh observation without another API read. Failed reads
do not clear an outstanding signal. A successful observation at or above the
threshold clears it. The station surfaces the durable factory flag in its normal output, including
when the station App budget is exhausted. This observer does not publish issues
or change existing merge, close, or reporting gates. The sampling client never
publishes anything.

Tests are entirely offline:

```
python3 -B .github/unittest_parallel.py --allow-skips --only tests.test_operator_budget
```

They inject the credential resolver and API client, use temporary state, and never
read live credentials or bind sockets. Focused success is not a substitute for the
repository's mandatory pre-publication validation.
