# Demo backends

Two tiny, deterministic backends for the LiteLLM gateway demo. **The MCP server makes no model calls and no outbound network requests; the A2A agent makes none unless you explicitly enable its optional gateway model mode.**
They are **unauthenticated demo backends** meant for **internal-only ingress** behind the gateway; never expose them publicly.

| Folder | What | Port | Paths |
| --- | --- | --- | --- |
| `mcp-server/` | Curriculum catalogue MCP server for the AI tutor (`mcp` 2.3.0, stateless streamable HTTP): tools `list_topics`, `get_topic`, `get_section`, `list_learning_modes` (see [below](#curriculum-catalogue-mcp-server)) | 8080 | `POST /mcp`, `GET /healthz` |
| `a2a-agent/` | A2A **1.0** agent `quiz_master` (`a2a-sdk` 1.2.2, JSON-RPC) with skills `create-quiz` and `grade-quiz` (see [below](#quiz-master-a2a-agent)) | 8080 | `POST /` (`SendMessage`), `GET /.well-known/agent-card.json`, `GET /healthz` |

Invalid MCP tool input (bad id, unknown topic/section/depth, oversized string) returns an MCP tool error (`isError: true`) with a short message listing the valid values.
The A2A agent caps request bodies at 64 KiB (HTTP 413) and answers every JSON-RPC method other than `SendMessage` with error `-32601`.
Set `PUBLIC_URL` (default `http://localhost:8080/`) so the agent card advertises the URL clients should call. The A2A agent also requires `QUIZ_SIGNING_KEY` (at least 32 characters) at startup.

## Run locally

```bash
cd demo/mcp-server   # or demo/a2a-agent
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover
python main.py       # listens on 0.0.0.0:8080
```

Or with Docker: `docker build -t demo-mcp demo/mcp-server && docker run --rm -p 8080:8080 demo-mcp`.

`requirements.txt` in each folder is a fully pinned (`==`) lock, so builds are reproducible.

## Curriculum catalogue MCP server

`mcp-server/` is a read-only, stateless catalogue of lesson content for a middle-school AI tutor. It holds no learner state and calls no model. Content is static JSON, loaded and validated at startup (a malformed file stops the server with a clear error).

| Tool | Parameters | Returns |
| --- | --- | --- |
| `list_topics` | none | `{topics:[{id,title,summary,section_count}]}` |
| `get_topic` | `topic_id` | topic map: `{id,title,summary,prerequisites,sections:[{id,title,summary,prerequisites,estimated_minutes,objectives}]}` (no lesson content) |
| `get_section` | `topic_id`, `section_id`, `depth` = `intro` \| `working` (default) \| `deep` | `{topic_id,section_id,title,depth,objectives,prerequisites,estimated_minutes,content,next_section_id,available_depths}`; `content` holds only the requested depth: `explanation`, `key_points`, `worked_example{problem,steps,answer}`, `common_mistakes`, `check_questions` |
| `list_learning_modes` | none | `{modes:[{id,title,description,tutor_behaviour,quiz_policy{when,kind},default_depth}]}` for `guided_path`, `map_then_zoom`, `mastery_gate` |

Topics: `fractions`, `primes`, `linear-equations`, each with three sections (nine in total, 18 objectives such as `frac.simplify` or `eq.two-step`). Results are compact JSON text plus structured content to save model tokens.

Data layout (`mcp-server/curriculum/`, copied into the image):

- `<topic-id>.json`: one file per topic with `id`, `title`, `summary`, `prerequisites` (topic ids) and `sections` in teaching order. Each section has `id`, `title`, `summary`, `prerequisites` (earlier section ids of the same topic), `objectives[{id,statement}]`, `estimated_minutes` (5-30) and `content{intro,working,deep}`.
- `modes.json`: the learning modes (`quiz_policy.kind` is `diagnostic`, `section` or `mastery`).

To add a topic: create `curriculum/<topic-id>.json` (the file name must equal the topic id) following an existing file. Ids match `^[a-z][a-z0-9-]{0,40}$`, objective ids match `^[a-z]+\.[a-z0-9-]+$` and must be unique across all topics. Each depth block needs an explanation (at most 120 words), 3-5 key points, a worked example, 2-3 common mistakes and exactly 2 check questions (conversational, ending in `?`). Then run `python -m unittest discover` in `mcp-server/`; `test_curriculum.py` validates the schema and recomputes the worked examples, so add a checker there for any new kind of example.

## Quiz master A2A agent

`a2a-agent/` is a stateless **A2A 1.0** agent (`quiz_master`, `a2a-sdk` 1.2.2) that creates short quizzes and grades them for the three curriculum topics (`fractions`, `primes`, `linear-equations`; the ids in `curriculum_index.py` mirror the MCP server's data). It speaks protocol 1.0 only: JSON-RPC method `SendMessage` (PascalCase, requests need header `A2A-Version: 1.0`), roles `ROLE_USER`/`ROLE_AGENT`, parts `{"text": ...}`; there is no 0.3 compatibility (`message/send`, `/.well-known/agent.json` and every other method give `-32601`/404). It replies with one direct `Message` (no tasks, no streaming). The card is the SDK's native 1.0 card at `GET /.well-known/agent-card.json` (`supportedInterfaces` JSONRPC 1.0, url from `PUBLIC_URL`, text/plain modes).

The message must hold exactly one text part containing a JSON object; the reply is one text part containing a JSON string.

```text
create  {"action":"create","kind":"diagnostic|section|mastery","topic_id":"fractions","section_id":"fractions-basics","level":"intro|working|deep","count":3-10}
        section_id is required for section/mastery and forbidden for diagnostic; level defaults to working, count to 5 (mastery 8)
reply   {"quiz_id","kind","topic_id","section_id","level","questions":[{"id":"q1","objective","type":"fraction|integer|integer_list|choice","prompt","choices"(choice only),"answer_format"}],"model_assisted":false}
grade   {"action":"grade","quiz_id":"...","answers":{"q1":"3/4","q2":"x = 5"}}   (every question id, no extras; "" skips; answers up to 64 characters)
reply   {"quiz_id","kind","score","max","percent","passed","pass_threshold","results":[{"id","objective","correct","expected","explanation","feedback"}],"weak_objectives":[...],"summary","model_assisted"}
error   {"error":{"code":"invalid_request|unknown_topic|unknown_section|invalid_quiz_id|internal_error","message":"..."}}  (a normal message, never a stack trace)
```

`expected`, `explanation` and the optional `feedback` appear only on incorrect results. `percent` is 0-100; `pass_threshold` is a ratio (diagnostic `null`/`null`, section 0.6, mastery 0.8).

**Deterministic and signed.** Questions come from templates (at least two per objective) driven by a seed; answers, scores and pass/fail are always computed in code. The `quiz_id` is **short enough for a language model to copy exactly**: 24 Crockford base32 characters shown in groups (for example `k3x9-0mq2-7fd8-hr5w-c2n4-ybzp`, 29 characters). It packs the kind, level, question count, topic and section index and a 48-bit random seed (10 bytes), plus a 5-byte HMAC-SHA256 tag keyed by `QUIZ_SIGNING_KEY` over a fingerprint of the curriculum and the payload, so grading needs no database: the agent recomputes the quiz from the id, compares the tag in constant time and rejects any altered, truncated, foreign-key or old-curriculum id (`invalid_quiz_id`). Case, hyphens, spaces, quotes and trailing punctuation are ignored when an id is submitted, and `o`, `i` and `l` read as `0`, `1` and `1`. The curriculum order in `curriculum_index.py` must stay append-only: reordering it invalidates open quizzes. The first version of this id was a ~170-character base64 token, which the tutor model corrupted while copying it, hence the short form. A fresh random seed per `create` makes retakes differ. Create replies never contain answers, and the id holds only the seed and quiz parameters, not answers (a learner who can read the source could still derive answers from the seed; this is a demo, not an exam system). Answers are normalised (trim, case, unicode minus/times/divide); `frac.simplify` needs lowest terms while other fraction answers accept equivalents; `x = 5` and `5` are both fine for equations; prime factor lists ignore order and accept `2*2*3` or `2^2*3`; choices accept a letter or the text; thousands separators such as `1,000` are not understood and count as incorrect.

**Optional model layer.** With `QUIZ_MODEL_MODE=gateway` the agent may call the LiteLLM gateway (OpenAI-compatible `/v1/chat/completions`, alias `gpt-5.4-nano`, plain chat completions with JSON output) for two optional things: rewording questions on `create` (one batched call) and a short kind `feedback` for incorrect answers on `grade` (one call, only if something is wrong). The model never sees answers on create, never influences `expected`, `explanation`, score or pass/fail, and its output is validated strictly (rewrites must keep every given verbatim, add no numbers beyond one small integer, never leak the answer; feedback is at most 300 plain-text characters without markup or URLs); anything that fails validation is dropped. Learner answers go to the model only as untrusted JSON data. If the model is off, slow (8 s per call, 12 s budget), unreachable, returns a non-200 or bad output, the agent behaves exactly as without a model (`"model_assisted": false`) and logs only the error class (never prompts, answers, tokens or bodies); after a failure the model is skipped for 20 s. The gateway token comes from the user-assigned managed identity per request and is never stored on the HTTP client; redirects are not followed.

| Env var | Meaning |
| --- | --- |
| `QUIZ_SIGNING_KEY` | **Required.** At least 32 characters; signs quiz ids. Keep it secret and stable (changing it invalidates open quizzes). |
| `PUBLIC_URL` | URL advertised in the agent card (default `http://localhost:8080/`). |
| `QUIZ_MODEL_MODE` | `off` (default) or `gateway`. |
| `QUIZ_GATEWAY_BASE_URL` | gateway mode: `https://<gateway-host>/v1` (https, path `/v1`, no credentials or query). |
| `QUIZ_GATEWAY_AUDIENCE` | gateway mode: the gateway app registration's client id (GUID); the token scope is `api://<GUID>/.default`. |
| `AZURE_CLIENT_ID` | gateway mode: client id (GUID) of the user-assigned managed identity used to get the token. |
| `QUIZ_MODEL` | gateway mode: model alias, default `gpt-5.4-nano`. |

In `gateway` mode the agent fails at startup if any of these is missing or invalid.
