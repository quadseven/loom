# Operator REST budget samples

The station samples the configured operator's GitHub core REST quota hourly,
including when lanes are idle. This is separate from the station GitHub App's
budget and does not change dispatch or merge gates.

The sampler uses the existing personal-token resolver. It creates no credentials,
changes no authentication configuration, and makes only GET requests. The first
non-bot login in `LOOM_BRIEF_AUTHORS` identifies the expected operator. A `/user`
response must identify that user before a budget is attributed to the operator.

The private station state file `operator-budget.json` contains up to 168 samples
and 168 safe failed-read records. Each sample has an observation epoch, remaining,
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
kept. Activity means a local queued/running brief or positive in-flight evidence
from a repo scan in the preceding hour. Idle low observations do not create a
signal; if work becomes active before that observation expires or its quota resets,
the station may flag that fresh observation without another API read. Failed reads
do not clear an outstanding signal. A successful observation at or above the
threshold clears it. The existing invariant filer surfaces the signal using the
station client and its existing publication gates. The sampling client never
publishes anything.

Tests are entirely offline:

```
python3 -B .github/unittest_parallel.py --allow-skips --only tests.test_operator_budget
```

They inject the credential resolver and API client, use temporary state, and never
read live credentials or bind sockets. Focused success is not a substitute for the
repository's mandatory pre-publication validation.
