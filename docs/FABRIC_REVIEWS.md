# Expected Fabric review notes

The station can check that scheduled reviews produced their expected notes.
It cannot list Fabric jobs, tell whether one ran, or re-enable a disabled job.
The check is off until configured and makes no AI or chat request.

Set `LOOM_FABRIC_REVIEW_NOTES` to a JSON object in the station environment:

```sh
LOOM_FABRIC_REVIEW_NOTES='{"timezone":"America/New_York","notes":[{"title_prefix":"Loom morning review","start":"00:00","by":"14:00"},{"title_prefix":"Loom weekly trend","start":"00:00","by":"14:00","days":[0]}]}'
LOOM_FABRIC_KEY_SSM="/example/fabric-key"
```

Export these settings when running `bash lanes/mac/install.sh`. The installer
preserves the review configuration in the station's launchd environment only,
not in the brain or lane environments. Changing the installed configuration
requires running the normal installer again. This feature does not run an
installer or change a running deployment itself.

Use the existing workspace key through its SSM parameter and the existing
`LOOM_AWS_REGION` setting. No key is created, stored in state or printed.
Incidents go to `LOOM_SELF_REPO` (the normal station setting), with
`loom:factory` and `loom:triage` labels. Set this to the intended repository.

- `timezone` is an IANA zone name. Eastern time follows its summer/winter offset.
- `notes` holds one to ten expected outputs. Each has a case-sensitive
  `title_prefix`, `start` and `by` in 24-hour `HH:MM` format. The start must be
  earlier than the deadline on the same calendar day. In an autumn repeated
  hour, the first occurrence is used. A window containing a spring skipped
  clock time is rejected before any requests; choose times outside that hour.
- `days` optionally limits the expected weekdays: Monday is 0, Sunday is 6.
  Omit it for every day. A weekly note is checked on its configured day only.
- Prefixes must be public-safe plain text, at most 120 characters. These
  configured names appear in incidents, so never include personal or secret
  information. Remote titles, contents, URLs and raw errors are not published.

After the last deadline for that day, one station step searches each expected
note using Fabric's documented metadata filter. A matching `notepad` must have
a name beginning with the prefix and `createdAt` inside the inclusive window.
An older note that was merely modified does not count. Windows missed while
the station was down are not backfilled.

All missing outputs are grouped into one incident. While that incident stays
open, later days add findings as routine status events instead of opening
another issue. A later day can open a new incident after the earlier one is
closed. A day with no findings adds nothing. A 402 response
is reported as a Fabric credit-limit cause. Failed, malformed or truncated
searches are marked "could not verify", never asserted to be missing. Search
uses at most five pages per note, 1 MiB per page and a 45-second shared request
budget, with a 10-second timeout per request. No match after a limit is unknown.

The daily attempt is saved before requests. A pending incident is retried
without searching Fabric again; the day marker also prevents duplicate
creation or a repeated status event after an uncertain GitHub reply. Shadow
mode holds incident writes.
An interrupted check reports that verification was interrupted, rather than
silently pretending all notes exist. Invalid config or unreadable state stops
the check with a local diagnostic. Other station work continues.

## API evidence

The read-only `POST /v2/resources/filter` route, `name` filtering and cursor
pagination are documented in [retrieving resources](https://developers.fabric.so/developer-guide/resources/retrieving-resources).
The [API reference](https://developers.fabric.so/api-reference/tag/resources/post/v2/resources/filter)
defines `kind`, creation-date filters, `resources`, `hasMore` and `nextCursor`.
The [notepad schema](https://developers.fabric.so/api-reference/tag/notepads/post/v2/notepads)
identifies notes as `kind: notepad`. This check calls only the metadata filter,
not semantic search, chatbot, note writes or scheduled-job endpoints.
