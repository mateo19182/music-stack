## Music acquisition through Telegram

For music requests, use the music-acquisition MCP tools. Check the library first
when relevant and point out existing tracks. Search Soulseek and YouTube; show
recording/version, format and available quality before choosing a source. If the
user supplies a supported URL or selects a result, enqueue that source. For an
open-ended request, suggest a small set of results and let the user choose if the
recording or version is ambiguous. Do not enqueue all results.

Keep the job ID and check its progress with get_job. Downloads and processing are
asynchronous. Report the stage truthfully; a queued job is not a completed import.
At Review, use list_reviews or get_job and request_review_advice if helpful.
Summarize review_summary.concerns, duplicates, album numbering/completeness,
recording versions, and AI advice. Unverified album editions remain unverified.
Advice is based on metadata and cannot establish what the recording sounds like.

Final approval is the user's action in the acquisition web app. Send its link
and tell the user which job to open in Queue. These tools can enqueue, inspect and
advise; they cannot approve, reject, publish or change sharing. Do not use terminal
commands, database edits, browser automation, user passwords or another API to
bypass this boundary. Agent credentials stay in their private mode-0600 file;
never expose them in Telegram, tool arguments, links or repository files.

If an agent token expires or is revoked, report the authentication error and ask
the service owner to provision a replacement in Acquire. Do not substitute human
credentials. New-source downloads and processing retries retain the manual review
step. A publishing retry may resume only a decision already approved by the user.
