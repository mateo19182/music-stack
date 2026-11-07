# LLM use in acquisition: redesign proposal (2026-11)

Status: proposal for review. The migration to cliproxy Haiku itself is already done
(branch `llm-cliproxy-haiku`, commit 9b7d034, deployed 2026-11-07).

## 1. Where models are used today

All calls go through `app/llm.py`: one OpenAI-compatible endpoint (`llm_base_url`,
default `https://ai.m19182.dev/v1`), cliproxy key `music-acquisition` (Haiku only,
30 rpm, $2/day, $10/week).

| Path | Code | Trigger | Volume (last 30 d, on OpenRouter) | Per call |
|---|---|---|---|---|
| Matching: which search results are the album/song | `llm_match.Judge`, called from `wishlist._rank` / `_rank_track` | every wishlist search, re-searches included | ~2,180 (`typesafe/jev`) | ~1.7k in / ~240 out, ~2 s, ~$0.0003 at meter prices |
| Review decider: add/replace/skip for open review questions | `review_decider.Decider`, `main.model_decide` | job reaches Review with automatic adding on and open questions | 53 (`openai/gpt-6-luna`, high effort) | ~2 s on Haiku |
| Metadata advice | `llm_review.Reviewer` | manual "AI advice" button; automatic advice is off (`review_advice: false`) | 3 (`muse-spark`) | batches of 20 files |
| Hermes `music-pending` (not in the app) | hourly cron, monitor-gated | something new is pending | already cliproxy Haiku | agent loop |

Where the matching volume comes from: ~1,400 wishlist items were searched in 30 days
(987 tracks, 221 albums, mostly playlist imports now `have`), plus re-searches after failed
or wrong downloads (2,512 `tried` entries). That is about 1.5 model calls per request.
Concerts (`concerts/`) make no model calls.

## 2. Problems

1. **Every search pays a model call, even when the rules are certain.** `matching.ranked_tracks`
   and `matching.identity` already reject most wrong candidates; for a typical track request
   (exact title and artist in the file name, length matches the reference) the model only
   confirms what the rules found.
2. **No memory of verdicts.** A re-search of the same request sends the same candidates again;
   a Soulseek folder seen in five searches is judged five times.
3. **One request per call.** The wishlist runs 3 searches per tick; each is its own call with
   the same ~1.2k-token system prompt.
4. **Silent success, in-memory accounting.** Successful calls log nothing; `match_judge.usage`
   resets on every restart. The cliproxy key meter is now the only durable record.
5. **Overlap with Hermes.** `music-pending` re-judges "choice" requests (record, version,
   completeness) that the matching model has already judged, with a larger prompt.
6. **Model-specific calibration left behind.** `YOUTUBE_TITLE_FLOOR = 0.2` was tuned on Jev's
   logit probabilities; Haiku's `p_wanted` is self-reported (checked: same-titled albums by
   other artists score 0.02-0.03, a plausible partial 0.35, so the floor still separates them,
   but on two examples only).

## 3. What Haiku is good and bad at here

Good: misspelled artists ("Djion"), version words (remix, live, sped up, instrumental),
"chapter vs album", strict JSON through a forced tool call or `json_schema`, ~2 s latency,
cheap enough that volume is not a budget problem (~$0.70/month at meter prices for current
matching volume).

Weak: discography knowledge for obscure releases (which album has which tracks), calibrated
probabilities, and nuanced replace-vs-keep judgements that `gpt-6-luna` at high effort did
with more reasoning. The decider now runs Haiku with `reasoning_effort: high`; its safety net
is unchanged (unsure stays in Review, replace must name a library copy), but its agreement
with your past decisions is unmeasured.

Proxy behaviour to remember: cliproxy turns on extended thinking for Haiku by default. A forced
tool call (matching) disables it; advice sends `reasoning_effort: none`; the decider sends
`high` on purpose.

## 4. Proposed design

**Principle:** code decides what is certain, Haiku decides identity questions code cannot
settle, Hermes only talks to you.

1. **Deterministic short-circuit (tracks first).** Skip the model when the rules give a
   single-version answer: title and artist words all present, no unrequested version words,
   length within 3 s of the reference tracklist, lossless or the quality floor met. Send to
   Haiku only the ambiguous remainder (no reference length, several versions, typo-level
   matches, YouTube-only results). For albums: skip when `identity()` passes with zero extra
   words and the track count matches the reference.
2. **Verdict cache.** SQLite table `verdicts(request_key, candidate_hash, model, prompt_version,
   match, p_wanted, problem, reason, at)`. A search sends only unseen candidates; the cache is
   invalidated by bumping `prompt_version` when the prompts change.
3. **Batch per tick.** Put the ambiguous candidates of all searches in one tick into one call
   (`requests: {r0: {wanted, reference, candidates}}`), cap ~25 candidates per call. Fewer
   calls, one shared system prompt (cached by cliproxy).
4. **Shadow mode before trusting step 1.** For two weeks, run rules and model side by side and
   log disagreements; only then let the rules skip the call. This also gives a labelled set.
5. **Durable accounting.** One log line per call (purpose, model, tokens, latency, outcome) and
   a persisted counter; show it in Settings next to the key's daily/weekly meter.
6. **Hermes boundary.** The app auto-resolves "choice" when exactly one edition matches with
   `p_wanted >= 0.8`; Hermes `music-pending` only handles real ambiguity, Soulseek checks and
   messages to you. No identity judging in the agent loop.
7. **Decider evaluation.** Replay the last ~200 owner-decided reviews through the Haiku decider
   (metadata only, no publication) and measure agreement; if it is below ~90 %, either keep
   more in "unsure" or allow Sonnet on this key for the decider only (~50 calls/month).

Expected effect: match calls from ~2,200 to an estimated 500-900/month (unverified until
shadow mode measures how many searches the rules settle), fewer Hermes runs that re-judge
choices, and a durable record of every call.

## 5. Migration steps

1. Add call logging and the persisted counter (small, no behaviour change).
2. Add the verdict cache (behaviour-neutral, removes duplicate calls on re-search).
3. Ship shadow mode for the short-circuit; review disagreements after two weeks.
4. Turn on the short-circuit for tracks, then albums.
5. Batch per tick.
6. Decider replay evaluation; decide on Haiku vs Sonnet.
7. Narrow the Hermes prompt once the app resolves single-edition choices.
8. Retire the OpenRouter "music" key after a week with no usage.

## 6. Open questions for Mateo

1. Is reaching cliproxy through `https://ai.m19182.dev/v1` (Cloudflare round trip, fails over to
   the rules if the tunnel is down) acceptable, or do you want a small dedicated Docker network
   shared only by cliproxy and its clients? Joining `henad_default` is not an option because it
   would also expose `henad_db`.
2. Should the review decider stay on Haiku, or may the `music-acquisition` key also allow Sonnet?
3. Turn automatic metadata advice (`review_advice`) back on now that it is cheap?
4. How strict should the short-circuit be: is a missed edition difference (say, a remaster
   picked for the original) acceptable when quality is equal?
5. Can the OpenRouter "music" key be revoked once usage stays flat for a week?
