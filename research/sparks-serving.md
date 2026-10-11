# Sparks today: serving engine, best coding model, and a lane endpoint

Research for #33 (part of #30). Checked 2026-10-11. Live checks were read-only:
service state, loaded models, listening ports, temperatures and the gateway's
public health routes. Nothing on the Sparks was started, stopped or changed,
and no benchmark was run.

## Summary

- Both Sparks serve Ollama only. No vLLM, SGLang or sparkrun engine is running.
  The "overhaul" is thermal hardening after both units powered off under
  sustained load. Inference is capped at one generation per node.
- NVIDIA and the handbook point to vLLM (or SGLang) and NVFP4 for serving. The
  NVIDIA CLI coding agent playbook still uses Ollama.
- Qwen3-Coder-Next fits one Spark at FP8 or 4-bit. It has no valid
  agentic-coding score in assay: its 6/6 ran before the runner contained reads
  and is void. The best valid score is `nemotron-3.5-lightning:30b` at 6/6,
  35 s per task.
- A lane can call the gateway's OpenAI-compatible `/v1` over HTTPS from inside
  the current API-lane sandbox, which allows remote network and denies
  loopback. The gateway has no auth. Seatbelt cannot limit a lane to that one
  host, so the sandbox needs more work first.
- Recommendation: do not move the whole fleet to vLLM. One node can serve the
  coder model on vLLM once two things are true: the thermal guard covers the
  vLLM engine, and a contained `agentic_coding` run shows vLLM at least ties
  Ollama.

## 1. Current state of the two Sparks

Observed on 2026-10-11 at about 22:20 UTC on both nodes:

| | node A | node B |
|---|---|---|
| Engine | Ollama 0.34.2 (systemd, port 11434) | Ollama 0.34.2 (systemd, port 11434) |
| Other engines | vLLM and two-node DeepSeek units installed, both disabled and inactive | none installed |
| Containers | none running | none running |
| Loaded model | `qwen3-coder-next:q4_K_M`, 59.7 GB resident, 262,144 context | none |
| Ollama settings | `NUM_PARALLEL=1`, `MAX_LOADED_MODELS=2`, `CONTEXT_LENGTH=262144`, keep-alive 24 h | same, keep-alive 5 min |
| GPU at check | 44 C, 8 W, idle | 41 C, 5 W, idle |
| Thermal controls | clock cap 1800 MHz; thermal guard active | same |

- Node A's Ollama tags: `qwen3-coder-next:q4_K_M`, `qwen3.5:122b`,
  `nemotron-3.5-lightning:30b`, `nemotron-3-nano:30b-a3b-q4_K_M`,
  `qwen3-vl:4b`, `qwen3-vl:4b-32k` and an embedding model.
- Node B has a larger catalog. It adds `qwen3-coder-next:q8_0`, `gpt-oss:120b`,
  `nemotron-3-super:120b`, `qwen3.8-flash-next:iq4xs`, `qwen3.8:27b` (q8 and
  MTP-q8), `qwen3.6:35b`, `qwen3.6:27b`, `gemma4:31b` and others.
- vLLM and SGLang images are present but idle on both nodes: `vllm/vllm-openai`
  cu130 nightly, `nvcr.io/nvidia/vllm:26.04` (node B only), a community DGX
  vLLM nightly and SGLang 0.5.9. sparkrun runs through `uvx` and is not
  installed as a tool.
- These weights are cached on both nodes: `Qwen/Qwen3-Coder-Next-FP8` (75 GB),
  DeepSeek-V4-Flash, `nvidia/MiniMax-M2.7-NVFP4` (131 GB), Nemotron-3-Super
  NVFP4, Laguna-S-2.1 NVFP4, Qwen3.5-122B FP8, and Qwen3.8-27B (FP8 and NVFP4).

The gateway's `/readyz` answered `{"ok":true,"targets":[<both nodes>]}` and
`/v1/models` listed the Ollama tags. The gateway config treats every vLLM
setting as history: its comment dated 2026-09-21 says no vLLM target is
configured and both nodes serve Ollama (`infra#2342`).

### What the overhaul is

Between 2026-09-19 and 2026-09-28 both Sparks cut power with no shutdown log.
The hottest thermal zone was 85-97 C at the time, below the 104.8 C kernel trip.
The failures came from sustained load, not from short bursts. One crash
happened with 42% of memory free, which rules out memory headroom. The fault is
still open (`infra#4729`). These controls are in place:

- GPU graphics clock capped at 1800 MHz from boot. At that cap an MoE soak
  peaked at 73.7 C. A dense q8 27B model still reached 80 C in 293 s, so dense
  models are not served.
- `host-thermal-guard` refuses new connections at 75 C and resumes at 65 C. It
  kills running generations when the zone holds 82 C for 30 s, and releases
  below 70 C. It watches only port 11434 and the Ollama cgroup, so a vLLM
  engine on another port would get no in-flight abort.
- At the gateway: one generation per model per node, a default output cap of
  8192 tokens for callers that send no limit, and a fallback to OpenRouter free
  models for `spark:warm-any` when no Spark can serve.
- Measured earlier on one node: four concurrent agent lanes took 79-224 s to
  the first byte, against 0.2-0.3 s for one lane. One GB10 serves one agentic
  lane well.

## 2. What the handbook and NVIDIA recommend

### DGX Spark Handbook

The handbook is a community article (EXO Labs blog on Hugging Face, published
2026-09-25). Its speed figures are the author's own runs or recipe READMEs.
Source: <https://huggingface.co/blog/exolabs/the-dgx-spark-handbook>.

- Engines: "Recipes use vLLM or SGLang, which are faster than LM Studio and
  serve many agents at once." LM Studio is the suggested first test. llama.cpp
  and TensorRT-LLM are not covered. Ollama appears only in a list of NVIDIA
  playbooks.
- Format: NVFP4, which GB10 reads natively. "4 bits is the sweet spot." GGUF
  and FP8 are not recommended.
- Memory: about 120 GB usable per Spark and about 240 GB across two.
- Two Sparks connect with one QSFP cable and NCCL over RDMA. NVIDIA's scaling
  test cut time per output token from 269 ms to 133 ms going from one Spark to
  two.
- Model picks: Qwen3.8-Flash-Next is "the smartest model on one Spark".
  GLM-5.3-Flash is the best choice on two Sparks. Qwen3.6-35B is the everyday
  helper (95 tok/s for one user, 317 tok/s across eight). DeepSeek-V4.1-Flash
  is for three or four Sparks. The handbook does not mention Qwen3-Coder-Next.
- Caveats: one Spark draws 90-200 W, multi-Spark software is "least polished",
  and heavy quantization costs accuracy.

### NVIDIA build guides

Source: <https://build.nvidia.com/spark>. The index lists playbooks for vLLM,
vLLM Cluster, SGLang, TRT-LLM, llama.cpp, NIM LLM, LM Studio, speculative
decoding, NVFP4 quantization, Connect Two Sparks and NCCL. Ollama is not a
playbook of its own.

The vLLM playbook (<https://build.nvidia.com/spark/vllm>) covers one Spark, or
two clustered with a QSFP cable. Its recommended recipes:

| Setup | Model | Format | Notes |
|---|---|---|---|
| One Spark | Qwen3.8-27B | NVFP4 | image `vllm/vllm-openai:qwen38`, model `nvidia/Qwen3.8-27B-NVFP4` |
| Two Sparks | Qwen3.8-27B | NVFP4 | tensor parallel plus DFlash2 speculative decoding |

- It serves on port 8000, with health at `/health` and the API at `/v1/models`
  and `/v1/chat/completions`. It sets no endpoint API key.
- The playbook names no coding model. Its single-Spark pick is a dense 27B,
  which overheats these units: the dense soak above, and a 2026-09-27
  benchmark that powered a node off.

The CLI coding agent playbook (<https://build.nvidia.com/spark/cli-coding-agent>)
sets up:

- Ollama with Qwen3.6, default tag `qwen3.6:35b-a3b-mtp-q4_K_M` (about 23 GB).
  `q8_0` (about 39 GB) and `bf16` (about 71 GB) are alternatives.
- One of Claude Code, OpenCode or Codex CLI, started with `ollama launch <agent>`.
  For OpenCode the command is
  `ollama launch opencode --model qwen3.6:35b-a3b-mtp-q4_K_M`. Ollama writes the
  provider config, so "no environment variables, provider config files, or
  external cloud APIs" are needed.
- One Spark only. It uses no vLLM and no endpoint key, and the agent runs on the
  Spark itself.

NVIDIA therefore uses vLLM for throughput serving and Ollama for the local
coding-agent setup. Neither guide covers sustained agentic load on hardware
with this thermal fault.

## 3. Qwen3-Coder-Next and other candidates

### Qwen3-Coder-Next

Model card: <https://huggingface.co/Qwen/Qwen3-Coder-Next>.

- 80B total parameters, 3B active. It is an MoE with 512 experts (10 routed
  plus 1 shared).
- Hybrid attention: 12 x (3 x Gated DeltaNet + 1 x Gated Attention), 48 layers
  in all. Only a quarter of the layers keep a growing KV cache, so long context
  is cheap.
- 262,144-token native context, Apache-2.0.
- Non-thinking: it "does not generate `<think></think>` blocks". This matters
  because reasoning models measured slower per agentic task here.
- Serving per the card: vLLM >= 0.15.0 with
  `--enable-auto-tool-choice --tool-call-parser qwen3_coder`, or SGLang
  >= 0.5.8 with `--tool-call-parser qwen3_coder`. Both example commands use
  TP=2 at BF16.
- Recommended sampling: temperature 1.0, top_p 0.95, top_k 40.

How each build fits on 121 GB usable per Spark:

| Build | Weights | Fits | Status on the Sparks |
|---|---|---|---|
| BF16 | about 160 GB | two Sparks only (TP=2), which takes the whole fleet | not cached |
| FP8 (official) | 75 GB on disk | one Spark, about 45 GB left for KV and runtime | cached on both; vLLM review trial at 65k context, 2 sequences, 80% GPU memory |
| Ollama `q8_0` | 84 GB (about 91 GB resident) | one Spark | on node B |
| Ollama `q4_K_M` | 51 GB (59.7 GB resident at 262k context) | one Spark, with room for a second model | on both; loaded on node A |

Two independent single-node copies give more throughput than one TP=2 copy and
fail independently, which is what assay's serving suite concluded
(<https://github.com/quadseven/assay/blob/main/suites/spark_serving/results/index.md>).

### What assay says

Source: <https://github.com/quadseven/assay/blob/main/suites/agentic_coding/results/scoreboard.json>.
Every row uses the six-task corpus `8d61e9c2dbdd8b3a` unless noted.

| Model | Size | Agentic coding | Status |
|---|---|---|---|
| `nemotron-3.5-lightning:30b` (Ollama) | 25 GB | 6/6, 35 s/task (2026-09-26); 5/6, 45 s (2026-09-20) | valid; current `warm-any` first choice; peak zone 80.9 C |
| Qwen3.8-Flash-Next, unsloth IQ4_XS (Ollama) | 93.7 GB, about 98 GB resident | 6/6, 79 s/task | valid; 3 attempts voided by the guard and re-run; peak zone 90.1 C |
| Qwen3.8-27B, `mtp-q8_0` (Ollama) | 30 GB, dense | 2/2 valid, 4 void | incomplete; powered a node off |
| `nemotron-3-nano:30b-a3b-q4_K_M` | 24 GB | 2/6, 33 s/task | valid; weakest |
| `qwen3-coder-next:q8_0` (Ollama) | 84 GB | void (ran 6/6, 40 s/task) | ran before reads were contained |
| `gpt-oss:120b` (Ollama) | 65 GB | void (ran 6/6, 97 s/task) | ran before reads were contained |
| DeepSeek-V4-Flash-0731, TP=2 | 167 GB | void (ran 5/6, 79 s/task) | ran before reads were contained; takes both nodes |
| Qwen3.6-35B-A3B FP8 (vLLM) | about 35 GB | void (ran 3/4 on the 5-task corpus) | edited a file outside its working directory |
| `qwen3.6:35b` (Ollama) | 23 GB | void | wrote into another repository's checkout |
| `nemotron-3-super:120b` | 86 GB | void | harness defect (orphaned attempts) |
| `gemma4:31b` | 19 GB | void | never loaded at the fleet context |

Other assay evidence on Qwen3-Coder-Next comes from code review, not agentic
coding
(<https://github.com/quadseven/assay/blob/main/suites/review_quality/results/index.md>):

| Build | Reviewer prompt size | Defects caught | Empty on a buggy diff | Median time |
|---|---|---|---|---|
| `q4_K_M` (Ollama) | 10k tokens | 86% | 14% | 13.8 s |
| FP8 (vLLM 0.25.1, one GPU) | 10k tokens | 93% | 7% | 5.0 s |
| FP8 (vLLM 0.25.1, one GPU) | 40k tokens | 71% | 21% | 20.7 s, with 10% timeouts |

assay calls the vLLM result "a faster engine, not a clearly better reviewer".
No suite has compared the two engines on agentic coding.

Read together:

- The best valid agentic score belongs to `nemotron-3.5-lightning:30b`. It is
  also the smallest and coolest strong model.
- Qwen3-Coder-Next is the likely co-leader but is unproven under the contained
  runner.
- With two models at 6/6, the corpus is saturated at the top. Ranking the
  leaders needs harder tasks, such as the constrained-refactor shape the suite
  README names as missing.
- Decode tok/s inverted against task time twice on this hardware, so it is not
  a selection criterion.

### Candidates that need a contained `agentic_coding` run

In priority order:

1. `qwen3-coder-next:q4_K_M` on Ollama. This build is already loaded on node A
   and has never been run on agentic coding.
2. `qwen3-coder-next:q8_0` on Ollama, to replace the void row.
3. `Qwen/Qwen3-Coder-Next-FP8` on vLLM, one node. This is the engine
   comparison, and the weights are already cached. Run it only after the
   thermal guard covers the vLLM port and cgroup.
4. `qwen3.6:35b-a3b-mtp-q4_K_M`, NVIDIA's playbook default. Both earlier
   Qwen3.6 rows are void for writing outside the task tree, and the runner now
   enforces filesystem isolation.
5. `gpt-oss:120b`, to replace the void row. It is a reasoning model, so expect
   it to be slow.

Deferred:

- These take both nodes with TP=2 and add sustained load while the thermal
  fault is open: DeepSeek-V4-Flash, GLM-5.3-Flash (the handbook's two-Spark
  pick, not cached) and MiniMax-M2.7-NVFP4 (cached, but a TP=2 launch failed
  on 2026-08-31).
- `nemotron-3-super:120b` needs a clean re-run at a 300 s cap.

### The exact assay procedure

From the assay README, AGENTS.md and the suite READMEs. Hosts are placeholders.

1. Make the box quiet. Run one sweep at a time on a node with no other
   consumer of the model under test. Check for orphans from earlier runs
   (`pgrep -f` on the agent wrapper) before trusting the timings.
2. Serving contracts first, a cheap way to disqualify:

   ```bash
   cd assay/suites/spark_serving
   uv run --with pillow python3 probe.py --url http://<node>:11434 --model qwen3-coder-next:q4_K_M
   ```

   The model must hold `tool_calling` and `json_object`. Qwen3-Coder-Next is
   text-only, so a vision failure is expected. That is fine for a coding lane,
   but it rules the model out as the only general model.
3. Agentic coding under the contained runner:

   ```bash
   cd assay/suites/agentic_coding
   python3 runner.py --agent 'claude-or <target> qwen3-coder-next:q4_K_M' \
     --label qwen3-coder-next:q4_K_M --endpoint-model qwen3-coder-next:q4_K_M \
     --endpoint <node>:11434 --timeout 300 \
     --env 'CLAUDE_OR_<TARGET>_HOST={endpoint_host}' \
     --env 'CLAUDE_OR_<TARGET>_PORT={endpoint_port}' \
     --out results/qwen3-coder-next-q4.json
   ```

   - The runner's proxy forwards only Anthropic `POST /v1/messages` (plus
     `count_tokens` and a `HEAD` preconnect) over plain TCP.
   - An Ollama node can be the endpoint directly, because Ollama serves
     `/v1/messages` natively.
   - For a vLLM engine, run the gateway's Anthropic bridge on a loopback port
     against the engine, pass `--endpoint 127.0.0.1:<port>`, and set
     `--endpoint-model` to the served id.
4. Treat heat as part of the result.
   - Record the peak thermal zone.
   - An attempt the guard refuses is void, not failed. Re-run it alone,
     starting below 60 C.
   - Put the real `claude` binary first on PATH. A wrapper shim gives a false
     0/6 with exit 126.
5. Record the result.
   - Add the row to `suites/agentic_coding/results/scoreboard.json`. The date
     and caveat are mandatory.
   - Run `python3 tools/scoreboard.py --write`.
   - Run `python3 tools/leakscan.py --selftest`, then `python3 tools/leakscan.py .`.
     Confirm the local term count is non-zero before pushing.
   - Open an assay PR.

One gap: assay measures Claude Code over the Anthropic wire, so its numbers
transfer directly to a Claude Code lane. An opencode lane on the OpenAI wire is
a different harness, and the runner's proxy would refuse its requests today.

## 4. Endpoint and sandboxing for a Loom lane

### Endpoint

- Base URL: `https://<gateway>/v1`, OpenAI-compatible.
  - `POST /v1/chat/completions` (with tools) and `GET /v1/models`.
  - The same host serves Anthropic `POST /v1/messages`: native on Ollama,
    translated for vLLM by the gateway's bridge.
  - Health: `/livez` and `/readyz`. `readyz` lists the reachable nodes.
- Auth: none. The gateway accepts unauthenticated requests and is reachable
  only over the operator's private network. Clients that require a key take
  any placeholder.
  - Send `X-Spark-Caller: loom-sparks` so gateway metrics and per-caller
    output caps can name the lane.
  - Leave `X-Spark-Priority` at its default, `batch`.
- Model: pin a literal Spark model, for example `qwen3-coder-next:q4_K_M`.
  Do not use `spark:warm-any`, for two reasons:
  - It resolves to the gateway's preference list, which is Nemotron today.
  - It falls through to OpenRouter free models when no Spark can serve. The
    lane's PR footer would then name the Sparks for work done elsewhere.
  - A literal local model never falls through. It returns a clean error
    instead.
- Limits a lane must expect:
  - One generation per model per node; extra lanes queue.
  - 8192 output tokens per call unless the client sends `max_tokens`.
  - Connections are refused while a node is above 75 C. A running generation
    is killed at 82 C sustained.
  - The runner should record a refusal or a reset as a void run, not a model
    failure.

### Sandboxing

The current API-lane profile (`lanes/run_lane.sh`, `sandbox_profile`):

- Starts from `allow default`.
- Denies reads and writes under the real HOME, except the worktree, the
  scratch home and the CLI's directory.
- Denies `.git` writes, keychain lookups, Apple Events, `osascript`, `open`
  and `launchctl`.
- Denies Unix sockets except the DNS resolver, and every loopback connection
  (`remote ip "localhost:*"`).

What follows for a Sparks lane:

- The gateway URL works unchanged, because remote network is allowed. A
  loopback bridge on the Mac does not, so the lane must call the gateway (or a
  node) over the network.
- The existing `api_agent` path fits:
  - CLI `opencode`.
  - `OPENCODE_CONFIG_CONTENT` carrying a provider block (`npm`
    `@ai-sdk/openai-compatible`, `options.baseURL` set to the gateway `/v1`,
    a placeholder `apiKey`, and the caller header), with `model` and
    `small_model` pinned to the Spark model.
  - `lane_ready` currently requires a key in SSM, so it needs a no-key case
    for this lane.
  - One check is still open: opencode may fetch its provider package or model
    catalog over the network at start. That would work under this profile but
    would break under a network allowlist.
- A Claude Code lane is the closer match to the measured harness:
  `ANTHROPIC_BASE_URL=https://<gateway>` with the literal model. That is the
  path assay scored.

Gaps to close before the lane runs unattended. #9 deferred the lane partly for
these reasons.

- Seatbelt cannot name a remote host. `remote ip` takes only `*` or
  `localhost` (measured in assay's runner). So the profile cannot pin the
  agent to the gateway.
  - The agent can reach every host on the network, including each node's raw
    Ollama port.
  - That port serves `/api/delete` and `/api/pull` without auth.
  - The assay pattern fixes this: the runner holds an inference-only proxy on
    a per-run loopback port. The profile allows exactly that port and denies
    all other outbound traffic. The proxy forwards only inference routes for
    the pinned model.
- Local models have written outside their working directory, once into
  another repository's checkout.
  - The current profile denies only HOME, so `/tmp`, `/private/var/folders`
    and other paths outside HOME stay writable.
  - A Sparks lane should use a write allowlist (worktree, scratch home and
    `/dev` only), as the assay box does.
- Spark-written patches have passed every test and still failed review on
  form. The brain must read every diff, and the lane should take only easy,
  test-gated tickets.

## Verdict

- Engine:
  - Keep Ollama on both nodes for now.
  - Move one node to vLLM for the coder model only after the thermal guard
    covers vLLM and a contained `agentic_coding` run puts
    Qwen3-Coder-Next FP8 on vLLM at or above the Ollama builds.
  - vLLM's main advantage, batching many agents, cannot be used while
    concurrency is held at one per node for heat.
- Model: `nemotron-3.5-lightning:30b` holds the only valid 6/6 at 35 s/task.
  Qwen3-Coder-Next needs the contained runs listed above before it can claim
  the lane.
- Endpoint: a lane calls `https://<gateway>/v1` with no auth and a pinned
  literal model. It runs under the API-lane sandbox plus an inference-only
  proxy and a write allowlist, which the profile still lacks.
