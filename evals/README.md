# Extraction eval

Scores how well a model pulls reservations out of real booking emails and PDFs, field by field,
against hand-checked answers. It runs the same path an ingest does (`parsing.parse` →
`llm.extract` → `enrich`) against whatever Ollama `OLLAMA_URL` points at, so it can run on a
workstation GPU while choosing models for marvin.

The golden set is private: real mail lives in the `triptych-evals` R2 bucket (filled by accounts
that opt in on the account page's Eval set tab) and the answers live in the private
`hatstand/triptych-evals` repo. `evals/smoke/` is a five-case public set over `samples/`, for
trying the harness and for CI.

## Workstation setup (3090 Ti)

1. Install [Ollama](https://ollama.com/download) and pull the models to compare. Use the exact tags
   marvin would run, so quantisation matches:

   ```sh
   ollama pull gemma4:26b-a4b-it-qat   # production today (LLM_MODEL)
   ollama pull gemma4:31b
   ollama pull qwen3.6:35b
   ollama pull qwen3.8:27b
   ollama pull gemma4:e4b
   ```

   Tags come from release roundups, so check each with `ollama pull` before relying on it. All of
   these fit marvin. On a 24 GB card the larger ones may spill a few layers to the CPU at the
   production context length. That makes them slower but doesn't change their answers.

2. Clone the private set and point the harness at it:

   ```sh
   git clone git@github.com:hatstand/triptych-evals.git ~/src/triptych-evals
   export TRIPTYCH_EVAL_DIR=~/src/triptych-evals
   ```

3. Make a read-only R2 token for the `triptych-evals` bucket (Cloudflare dashboard → R2 → Manage API
   tokens → Object Read only, scoped to that bucket) and export it:

   ```sh
   export EVALS_BUCKET=triptych-evals
   export EVALS_ENDPOINT_URL=https://<account id>.r2.cloudflarestorage.com
   export EVALS_ACCESS_KEY_ID=…
   export EVALS_SECRET_ACCESS_KEY=…
   ```

4. Match production's generation settings: `LLM_NUM_CTX` (default 16384) and `LLM_TIMEOUT_S` are
   read from the environment as in the app. Leave `OLLAMA_URL` unset for the local Ollama.

Then `uv sync` in this repo. The harness is in the dev dependencies.

## Labelling

```sh
uv run python -m evals sync     # fetch new shared cases; each gets cases/<id>/expected.yaml as a draft
uv run python -m evals check    # validate everything and list the drafts left to verify
```

A draft is what production's model answered when the mail arrived. Open it next to
`inputs/<id>/original.*` (or `parsed.txt`, the text the model saw), fix it, and change
`status: draft` to `status: verified`. Only verified cases are scored. Editors with the YAML
language server (VS Code's YAML extension) validate and autocomplete against
`expected.schema.json`.

- Leave a field out when the document doesn't say it. The harness counts a value there as a
  hallucination.
- `accept:` lists other right answers per field, with `null` if leaving it out is fine as well:
  `accept: {city: [Lisboa], start_city: [null]}`.
- `tags:` slice the report. Sync starts each case with its source and file type; add your own, such
  as `rail`, `multi-leg` or `no-year`.
- Unscored fields (title, provider, address, seat…) are only shown for reference.

For mail that never went through the app, save it from Gmail (⋮ → Download message) and run
`uv run python -m evals add ~/Downloads/*.eml`. That drafts a `local-<hash>` case with the
local model. Local inputs aren't in the bucket, so keep them, or commit them beside the
expected.yaml and point `input:` at them.

## Running

```sh
uv run python -m evals run                                   # production's model and prompt
uv run python -m evals run --model gemma4:31b --model qwen3.6:35b --think off --think on
uv run python -m evals run --prompt prompts/terser.txt --tag rail --repeat 3
uv run python -m evals --dir evals/smoke run                 # the public smoke set
```

Each variant, a model × prompt × think combination, writes `runs/<time>-<model>-<prompt
version>[-think]/` with `results.json`, which holds everything, and `summary.md`, which shows rates
per tag and per field and lists every case that wasn't fully correct with the reason. The prompt
version is the same hash production records with each extraction run, so a run lines up with the
`extraction_run` rows it's compared against.

```sh
uv run python -m evals compare runs/<before> runs/<after>
```

This shows how each rate and each field moved, and which cases were fixed or broke.

### What's scored

- **fully correct**: the right travel/not-travel call, every reservation found, nothing extra, and
  every key field right: kind, start and end times, airports, city, confirmation code, flight
  number, and the timezones the app ends up storing.
- **classification**, and reservation **recall** and **precision**. Reservations are paired with
  the expected ones by an optimal assignment, so their order doesn't matter.
- Per field, each paired value is **correct**, **near**, **wrong**, **missed** (expected a value,
  got null) or **hallucinated** (expected null, got a value). Near covers the right day at the wrong
  time, a day out, the wrong year, or another airport in the same city. Cities ignore case and
  accents, codes ignore case and spaces, and timezones count as equal if they keep the same time all
  year.
- **start_tz / end_tz** are scored after `enrich.normalise` and `fill_timezones` run over both sides.
  A wrong model timezone that the places data overrides doesn't hurt the calendar, and this shows it.

### Speed

Durations are recorded with the host they ran on. A 3090 Ti and marvin's Strix Halo differ a lot in
speed, so compare durations only between runs on the same host. Answers can also differ slightly
between CUDA and ROCm. Before switching `LLM_MODEL`, re-run the winner on marvin (or try it there
first as `SHADOW_MODEL`).

## Tests

`uv run pytest tests/test_eval_score.py` checks the scoring, case files and sync without a model.
`uv run pytest -m llm` also runs the smoke set through `LLM_MODEL` on the local Ollama.
