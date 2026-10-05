# Acquisition tools

Use `agent_tools.py` to search for music, queue a selected source, and follow preparation through Review. The user's final publication decision happens in the acquisition web app. These tools do not approve, reject, publish, delete, or change Soulseek sharing.

## Starting the MCP server

Run the bridge with the acquisition virtual environment:

```sh
acquisition/.venv/bin/python acquisition/agent_tools.py
```

The default credentials file is `~/.config/music-stack/acquisition-agent-credentials.json`. It contains `base_url` and a dedicated agent bearer `token`, and must have mode `0600`. The service owner provisions this token. Do not substitute a user's password or cookie, print the token, or copy the credentials into repository files or tool arguments.

Set `ACQUIRE_AGENT_CREDENTIALS` to another credentials-file path, or pass `--credentials /absolute/path/to/file.json`. CLI `--credentials` takes precedence over the environment variable. Configure a stdio MCP client to launch the Python command above; credentials stay in the local file rather than the client configuration.

The bridge implements [MCP 2025-06-18 stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports), [initialization](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle), and [tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools). A client starts with `initialize`, sends `notifications/initialized`, then requests `tools/list` and `tools/call`. Each JSON-RPC message occupies one line. Standard output contains protocol messages; diagnostics use standard error. Closing standard input ends the bridge.

## Available tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `health` | None | Checks workers and API availability. |
| `search_music` | `query`, optional `source`, `kind`, `artist`, `title`, `album` | Starts a search. Source is `all`, `soulseek`, or `youtube`; kind is `track` or `album`. Artist/title/album can also supply a query. |
| `get_search` | `id` | Reads search progress, candidates and library-match hints. |
| `list_jobs` | None | Lists visible downloads and processing jobs. |
| `get_job` | `id` | Reads one job's stage, progress, files and errors. |
| `list_reviews` | None | Lists prepared files awaiting manual approval. |
| `request_review_advice` | `id` | Requests advisory metadata/version/duplicate guidance for a review job. It does not change its publication decision. |
| `library` | Optional `q`, `genre`, `key`, `bpm_min`, `bpm_max`, `page`, `sort` | Reads the published library. Sort by `title`, `artist`, `album`, `genre`, `bpm`, or `key`. |
| `enqueue` | `candidate_id`, optional `artist`, `title`, `album` | Queues an explicitly selected search result. |
| `enqueue_url` | `url`, optional `kind`, `artist`, `title`, `album` | Queues a supported media URL or playlist. The API validates the site and URL. |
| `retry_job` | `id`, `stage` | Retries `download`, `processing`, or a previously approved `publishing` step. Processing retries reuse completed audio; publication retries retain the existing human decision. |
| `cancel_job` | `id` | Requests cancellation; completed source audio remains available. |
| `import_files` | `paths` | Prepares 1–100 selected inbox-relative paths. Requires an authorized admin agent. |

Search, enqueue, advice, retry, cancellation and import calls change server state. Reading health, search status, jobs, reviews and library does not. The server enforces record ownership and agent permissions; input schemas reject unknown fields and invalid types.

## Completing an acquisition request

1. Check `health` and `library` when relevant. Existing-library hints compare metadata; they do not establish identical audio or the right studio/live/remix version.
2. Start `search_music`, keep its returned ID, and use `get_search` until its status is `done` or `failed`. Source failures may accompany otherwise usable results.
3. Choose the candidate the user requested. Keep the source artist/title/album separate from the requested identity. Do not enqueue all search results or treat every result as the requested recording. Use `enqueue_url` for a supplied supported URL.
4. Store the returned job ID and poll `get_job`. Typical stages are `queued`, `downloading`, `process_queued`, `processing`, and `review`. These are asynchronous; an enqueue response does not mean the file is ready or published. Use short, spaced status checks and report substantive progress.
5. At `review`, inspect the existing and proposed tags, source identity, catalog confidence, album/version, duplicate summaries and analysis provenance. `request_review_advice` can help explain the decision. Advice and catalog suggestions are advisory, and estimated BPM/key values remain estimates.
6. Present any uncertainty and link the user to the web app for manual approval. Existing authenticated preview/download routes use `/api/files/{id}/preview`, `/api/files/{id}/download`, and `/api/albums/{album_id}/download` when those IDs are present. Bearer credentials do not belong in URLs. Stop tool-driven ingestion at Review; do not bypass the dedicated agent's approval or sharing restrictions through another API or credential.

For a failed job, read its error and failed stage. Retry processing when audio is already complete; retry download for an incomplete transfer. Refresh a job after a state-conflict response. Agent authentication or authorization errors require the service owner to check permissions; do not fall back to a human login. No tool claims a failed or cancelled job was published.

## Verification

Run `uv run --project acquisition pytest acquisition/tests/test_agent_tools.py -q` from the repository root. Tests cover all endpoint mappings, private credentials, annotation accuracy, invalid inputs, safe API errors, JSON-RPC framing and an actual stdio subprocess handshake. This bridge imports no acquisition service configuration and adds no dependency beyond the project's existing `httpx`.
