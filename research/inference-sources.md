# Inference sources for Loom

Research for #31, as of 2026-10-11. Every inference source Loom could use,
how a headless harness on the owner's Mac reaches it, its limits, cost, and
whether its terms allow an automated harness run by the account owner.

Method: provider facts come from official docs, pricing and terms pages,
cited inline. Local facts (installed CLIs, which keys exist) come from
checking the owner's machine and listing AWS SSM parameter names. No secret
values were read. Keys are named generically; the real SSM paths are set at
install time and never written in this repo.

## Summary table

| Source | Reach (CLI or API) | Key | Limits | Cost | Terms for an owner-run harness |
|---|---|---|---|---|---|
| Claude Max | `claude -p` (official Claude Code, subscription login) | Subscription OAuth in the CLI's own store | 5-hour session limit plus weekly limits, shared with the Claude apps; no published numbers | From $100/mo | Yes, only through the unmodified official CLI. Extracting the OAuth token into another client is not allowed. |
| Anthropic API credits | `claude --bare -p` with `ANTHROPIC_API_KEY`, or the Messages API | Anthropic API key in SSM | Tier rate limits; credits expire 2026-10-21 | Per token (Opus 5.5 $4/$20 per MTok) | Yes. Commercial Terms govern API keys and allow automated use. |
| Codex (ChatGPT plan) | `codex exec` with the saved ChatGPT login | ChatGPT login in the CLI's own store | Per-5-hour message windows per model, weekly limits may apply | Plan price (Plus $20, Pro $100 to $500) | Yes on the owner's own private machine. OpenAI recommends API keys for automation and forbids the auth-file path for public repos. |
| OpenCode Go | `opencode run` with provider `opencode-go`, or the Zen Go API | OpenCode Go key in SSM | Monthly dollar allowance per model; 5-hour and weekly slices at 20% and 50% | $10/mo (Go Plus $40) | Yes for coding-agent traffic. Send a stable `x-opencode-session` header. One ToS clause on unattended processes is ambiguous. |
| OpenCode Zen free models | `opencode run -m opencode/<model>-free` | Works without a stored key in opencode (0 credentials configured, models still listed) | No published numbers; "limited time" models | Free | Allowed, but several free models train on prompts. |
| OpenRouter free models | `opencode run` (OpenRouter provider) or `claude` pointed at OpenRouter's Anthropic endpoint | OpenRouter key in SSM | 20 RPM; 1,000 requests/day once 10+ credits were ever bought, else 50 | Free (`:free` models only) | Yes. Terms ban scraping and reselling, not automated API use. |
| Poolside | `opencode run` (native Poolside provider) or the OpenAI-compatible API | Poolside key in SSM | No published rate limits | Laguna S 2.1 and XS 2.1 at $0.10/$0.20 per MTok | No terms text found in the docs; standard API product. |
| TypeSafe (Jev) | TypeSafe API; also "Jev 1.13 Free" on OpenCode Zen | TypeSafe key in SSM | Balance exhausted | $42 per billion input tokens | Typed-judgment model, not a coding model. |
| Google plan via agy | `agy -p` | Google login in the CLI's own store | 5-hour refresh up to a weekly cap (Pro); `/usage` shows quota | Plan price (Pro $20/mo) | Yes for direct scripting with `agy -p`. Wrapping agy inside another agent is not allowed. |
| Google AI Studio (Gemini API) free tier | Gemini API with a key; not an opencode docs provider (Vertex is) | Gemini API key in SSM | Per-project RPM, TPM, RPD shown only in AI Studio | Free tier on Flash-class models | Allowed, but free-tier prompts may be read by humans and used to improve products. |
| AWS Bedrock | `claude` with `CLAUDE_CODE_USE_BEDROCK=1`, `opencode` (native Bedrock), Strands Agents SDK | AWS credentials on the machine; no new key needed | Per-model TPM and RPM quotas in Service Quotas | Per token; no free tier on this account | Yes. Pay-per-token API on the owner's account. |
| Cloudflare Workers AI | `opencode` (native provider) or the OpenAI-compatible REST API | A Cloudflare account and API token exist in SSM; Workers AI scope unverified | 10,000 Neurons/day free, 300 RPM | Free allocation, then $0.011 per 1,000 Neurons | Yes. Programmatic API product. |
| NVIDIA build.nvidia.com | `opencode` (native NVIDIA provider) or the OpenAI-compatible API | None stored | About 40 RPM per community reports, varies by model and load | Free for prototyping | Gray zone: trial terms limit use to internal testing and evaluation, not production. |
| Z.ai free Flash models | `opencode` (native Z.AI provider) or the API | None stored | No published rate limits | Free (GLM-4.7-Flash, GLM-4.5-Flash) | No restriction found on automated API use. |
| Local models (sparks lane, deferred) | `opencode` with a local OpenAI-compatible gateway provider | None | GPU capacity | Electricity | Owner's hardware; no third-party terms. |

Not usable: GitHub Models (retired 2026-07-30), Cerebras (no renewing free
tier), Gemini CLI (retired for unpaid and Google One users), Qwen Code OAuth
(free tier ended 2026-04-15). Details below.

## Installed CLIs on the lane machine

Present: `claude` (Claude Code 2.1.x), `codex` (0.162), `opencode` (1.18),
`agy` (1.3), `gemini` (0.49, but retired for this account; it returns
`IneligibleTierError` and points to Antigravity), `qwen`, `aws` (2.36), `gh`.
Absent: `wrangler`, `strands`, `ollama`, `copilot`, `cursor-agent`.

opencode currently configures only local providers in its user config and
holds 0 stored credentials. It still lists ten OpenCode Zen free models.
Loom's API lanes inject keys per run from SSM, so this is expected.

opencode documents native providers for Amazon Bedrock, Cloudflare Workers
AI, Cloudflare AI Gateway, NVIDIA, OpenRouter, Poolside, OpenCode Zen,
OpenCode Go, Groq, Cerebras, Hugging Face, Together, Moonshot, Z.AI, Ollama
Cloud and GitHub Copilot ([opencode providers](https://opencode.ai/docs/providers/)).

## Sources the operator has

### Claude Max (official Claude Code only)

- Plans: Max starts at $100/mo in 5x and 20x tiers
  ([pricing](https://claude.com/pricing)).
- Limits: a five-hour session limit and a weekly limit, plus a separate
  weekly Fable limit on Max. Limits are shared across Claude and Claude Code,
  with no published numbers. Monthly API credits included with Max cannot be
  used in Claude Code
  ([support](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)).
- Headless: `claude -p` is the CLI form of the Agent SDK. `--bare` skips
  OAuth and the keychain and needs `ANTHROPIC_API_KEY`, so subscription runs
  must not use `--bare`
  ([headless docs](https://code.claude.com/docs/en/headless)).
- Terms: subscription OAuth is for "ordinary use of Claude Code and other
  native Anthropic applications". Third parties may not route requests
  through Pro or Max credentials or intermediate Claude.ai tokens. An end
  user signing in to the unmodified Claude Code binary with their own
  subscription stays allowed
  ([legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)).
  The Consumer Terms forbid automated access except via an API key "or where
  we otherwise explicitly permit it"
  ([consumer terms](https://www.anthropic.com/legal/consumer-terms)).
- Loom fit: the `claude` lane already matches this. The harness writes
  briefs and the official CLI does the work; nothing else holds the login.

### Anthropic API credits

- Reach: `claude --bare -p` with `ANTHROPIC_API_KEY`, or the Messages API.
  Key: an Anthropic API key is stored in SSM.
- Expiry: the operator's credits expire 2026-10-21. Usage credits expire one
  calendar year after issue
  ([credit terms](https://www.anthropic.com/legal/credit-terms)).
- Pricing per MTok input/output: Opus 5.5 $4/$20, Sonnet 5.5 $2/$10,
  Haiku 5.5 $0.10/$0.50 up to 100K-token prompts, Fable 5.1 $10/$50
  ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)).
- Rate limits: Start tier caps spend at $500/mo with 1,000 RPM for 5.x
  models. A spend-cap 429 carries `enforced_spend_limit_reached` and no
  `retry-after` ([rate limits](https://platform.claude.com/docs/en/api/rate-limits)).
- Terms: the Commercial Terms govern API keys and allow using the services
  to power products, with no ban on automated use
  ([commercial terms](https://www.anthropic.com/legal/commercial-terms)).
- Loom fit: spend the remaining balance on the `anthropic` lane before
  2026-10-21; after that the lane goes unavailable unless credits are bought.

### Codex subscription (codex exec only)

- Plans: Plus $20 includes Codex CLI with GPT-6.1 Sol and GPT-6 Luna; Pro is
  $100, $200 or $500/mo. Plus and Business local messages per 5 hours:
  Astra 5 to 45, Sol 15 to 160, Luna 350 to 3,000. Pro has no five-hour limit
  at present; weekly limits may apply
  ([Codex pricing](https://learn.chatgpt.com/docs/pricing)).
- Headless: `codex exec` reuses the saved CLI login and runs read-only by
  default. OpenAI calls API keys "the right default for automation"; the
  ChatGPT login is for users who want plan rate limits instead
  ([non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)).
- Terms: reusing the auth file in CI is for "trusted private automation" and
  must not be used for public or open-source repositories or shared across
  concurrent jobs or machines
  ([CI/CD auth](https://learn.chatgpt.com/docs/auth/ci-cd-auth)). Running
  `codex exec` on the owner's own logged-in machine is the documented case.
  The OpenAI Terms of Use page returned HTTP 403 and was not checked.
- Loom fit: keep one codex run at a time per login. Codex works on public
  repos only through the owner's local CLI session, never a copied auth file.

### OpenCode Go ($10/mo)

- Models: about 35, including GPT 6 Luna, Claude Haiku 5.5, Kimi K3,
  DeepSeek V4, MiniMax M3 and GLM-5.3
  ([Go docs](https://opencode.ai/docs/go/)).
- Limits: monthly dollar allowance per model, with 5-hour and weekly slices
  at 20% and 50% of the month. An optional "Use balance" falls back to Zen
  balance. Endpoints split across `/responses`, `/messages` and
  `/chat/completions`; a model is served from only one of them.
- Key: an OpenCode Go key is stored in SSM.
- Terms: "Traffic is monitored for abuse." Clients should send their own
  user agent and a stable `x-opencode-session` header per conversation. The
  OpenCode Terms forbid "processes that run or are activated while you are
  not logged into the Services" and multiple accounts to dodge limits
  ([terms](https://opencode.ai/legal/terms-of-service)). The first clause is
  ambiguous for unattended runs; the Go docs openly expect agent traffic.
- Data: most Go models keep 0 days; Grok, GPT Luna and Claude Haiku keep 30
  days; Muse Spark Contributor trains on prompts.

### OpenCode Zen free models

- Reach: `opencode run -m opencode/<id>`. Ten free models show in
  `opencode models` on the lane machine with no stored credential.
- Models: Big Pickle, Space Bunny, LongCat 2.5 Preview, Step 5 Preview,
  Exo, MiMo-V2.6-Flash, Ling 3.x Flash, Nemotron 3 Ultra and 3.5 Lightning,
  Muse Spark 1.3 Contributor, Jev 1.13. Most are "available for a limited
  time", with no published limits ([Zen docs](https://opencode.ai/docs/zen/)).
- Data: Big Pickle, Exo, MiMo and Ling may train on prompts; Nemotron free
  models are NVIDIA trial endpoints ("do not submit personal or
  confidential data"); Muse Spark Contributor trains Meta models; Space
  Bunny, LongCat and Step keep nothing.
- Loom fit: a zero-cost volume lane for public-repo tickets only, given the
  training caveats.

### OpenRouter free tier

- Limits: 20 requests/min on `:free` models; 50 requests/day with under 10
  credits ever bought, 1,000 with 10 or more. A negative balance can 402
  even free models. `GET /api/v1/key` returns `free_model_daily_requests`
  with used, limit and remaining for the UTC day
  ([limits](https://openrouter.ai/docs/api/reference/limits)).
- Key: an OpenRouter key is stored in SSM. Operator records show more than
  10 credits bought, so the 1,000/day cap should apply; the key endpoint
  confirms it live.
- Terms: bans scraping, reselling and competing services; the key holder is
  responsible for its activity; no ban on automated API use
  ([terms](https://openrouter.ai/terms)). Free providers may log and train;
  that follows the account's privacy toggles
  ([provider logging](https://openrouter.ai/docs/guides/privacy/provider-logging)).
- Watch: the Poolside Laguna `:free` listings end 2026-10-31
  ([listing](https://openrouter.ai/poolside/laguna-s-2.1:free)).

### Poolside API

- Reach: OpenAI-compatible at `https://inference.poolside.ai/v1` with a
  bearer key ([API overview](https://docs.poolside.ai/api/overview)). Loom's
  `lanes/opencode/config.example.json` uses `https://api.poolside.ai/v1`;
  confirm which host the key accepts. opencode also has a native Poolside
  provider.
- Models and price: Laguna S 2.1 $0.10/$0.20/$0.01 per MTok
  (input/output/cache read), Laguna XS 2.1 $0.10/$0.20/$0.05
  ([S 2.1](https://poolside.ai/blog/introducing-laguna-s-2-1),
  [XS 2.1](https://poolside.ai/blog/introducing-laguna-xs-2-1)).
- Key: a Poolside key is stored in SSM. Balance and rate limits are not
  published in the docs; check the platform console.

### TypeSafe (Jev)

- What it is: a "System One" model returning typed judgments with
  probabilities, not a chat or coding model. $42 per billion input tokens,
  early access ([site](https://typesafe.ai/)).
- State: a TypeSafe key is stored in SSM; the balance is exhausted.
  "Jev 1.13 Free" is listed among OpenCode Zen free models.
- Loom fit: a routing or triage judgment primitive for the dispatcher, not a
  worker lane.

### Google plan via agy

- Reach: `agy -p` (Antigravity CLI). The FAQ says headless mode "is intended
  for direct scripting and CI workflows". It also says wrapping agy inside
  third-party coding agents and automating account rotation are not
  supported on consumer plans, and that third-party tools accessing
  Antigravity violate the ToS ([FAQ](https://antigravity.google/docs/faq/)).
- Quotas: Pro refreshes every 5 hours up to a weekly cap; Ultra is higher.
  Gemini Flash and Pro share one limit; non-Gemini models have another
  ([plans](https://antigravity.google/docs/plans/),
  [plan changes](https://antigravity.google/blog/changes-to-antigravity-plans/)).
  `/usage` shows per-model quota
  ([usage command](https://antigravity.google/docs/cli/commands/usage/)).
- Loom fit: a lane that calls `agy -p` directly per brief, like the codex
  lane, fits "direct scripting". Running agy as a tool inside another agent
  does not.

## Candidates

### Google AI Studio (Gemini API) free tier

- Free models include Gemini 3.8 Flash, 3.6 Flash, 3.5 Flash-Lite,
  2.5 Pro and 2.5 Flash; no free tier for 3.1 Pro Preview
  ([pricing](https://ai.google.dev/gemini-api/docs/pricing)). Limits are per
  project and reset at midnight Pacific; numbers show only in AI Studio
  ([rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)).
- Key: a Gemini API key is stored in SSM.
- Terms: unpaid-service prompts may be read by human reviewers and used to
  improve products; agentic use must not bypass human-confirmation requests;
  EEA, Swiss and UK end users need paid services
  ([terms](https://ai.google.dev/gemini-api/terms)).
- CLI: opencode documents Vertex AI, not AI Studio; the models.dev catalog
  has a Google provider, so `opencode models google` may work once the key
  is exported. Untested.

### AWS Bedrock (and Strands Agents)

- Pricing: per token on demand, plus Flex, Priority, batch and provisioned
  tiers; no Bedrock free allowance
  ([pricing](https://aws.amazon.com/bedrock/pricing/)). Free Tier credits
  apply only to new accounts ([free tier](https://aws.amazon.com/free/)); the
  owner's account has no Free Tier plan, so all use is billed.
- Catalog on the account: Claude (Haiku through Opus 5.5 and Fable 5.1),
  OpenAI GPT-5.x and GPT-6.x, Kimi K3, GLM-5.3, Nova, Llama, DeepSeek, Qwen
  and others, via `us.` and `global.` inference profiles. Anthropic models
  show as authorized, but the marketplace agreement is not yet accepted;
  first use needs the one-time Anthropic use case form and subscription.
- Quotas: per-model TPM and RPM, possibly set lower for an account's age
  and history ([quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html)).
- Claude Code on Bedrock: `CLAUDE_CODE_USE_BEDROCK=1` and `AWS_REGION`.
  Without pinning, the main model defaults to Opus 5.5 at the Opus rate, so
  pin `ANTHROPIC_MODEL` ([docs](https://code.claude.com/docs/en/amazon-bedrock)).
- Strands: `Agent()` defaults to Bedrock with Claude Sonnet 4.6. It is a
  Python or TypeScript SDK, not a CLI, and is not installed
  ([Strands Bedrock](https://strandsagents.com/docs/user-guide/concepts/model-providers/amazon-bedrock/)).
- CLIs: Claude Code and opencode both drive Bedrock natively.
- Loom fit: the paid overflow lane. It costs money per token, so it needs a
  spend cap or budget alarm before use.

### Cloudflare Workers AI

- Free: 10,000 Neurons/day on Free and Paid plans, reset 00:00 UTC; then
  $0.011 per 1,000 Neurons ([pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)).
  That buys about 110K output tokens/day on gpt-oss-120b, or about 270K on
  glm-4.7-flash.
- Free-plan coding models: qwen2.5-coder-32b, gpt-oss-120b, gpt-oss-20b,
  llama-3.3-70b, glm-4.7-flash, kimi-k2.5. Newer Kimi, GLM-5.x and DeepSeek
  V4 need Workers Paid.
- Limits: 300 RPM for text generation
  ([limits](https://developers.cloudflare.com/workers-ai/platform/limits/)).
  OpenAI-compatible endpoint under the account's `/ai/v1` path
  ([compatibility](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)).
- Key: a Cloudflare account and API token exist in SSM, but the token's
  scope is unverified. Workers AI needs "Workers AI - Read" and "Edit"
  ([REST API](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)),
  likely via a new dedicated token.
- CLI: opencode native provider.
- Loom fit: too small for agent loops; usable for triage or summarization.

### NVIDIA build.nvidia.com (NIM API)

- Access: free Developer Program membership gives NIM endpoints "for
  prototyping"; production needs NVIDIA AI Enterprise
  ([NIM docs](https://docs.api.nvidia.com/nim/docs/product)). Trial terms
  limit use without a subscription to "internal testing and evaluation
  purposes, not in production"
  ([trial terms](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf)).
- Limits: about 40 RPM per community forum reports only; varies by model
  and load.
- Endpoint: OpenAI-compatible `https://integrate.api.nvidia.com/v1`, with
  qwen2.5-coder, DeepSeek, Kimi, GLM, gpt-oss and Nemotron
  ([LLM APIs](https://docs.api.nvidia.com/nim/reference/llm-apis)).
- Key: none stored; needs a Developer Program sign-up.
- CLI: opencode native provider.
- Loom fit: an owner's dev harness reads as internal development, a gray
  zone. Treat it as a fallback, not a primary lane.

### GitHub Models

Retired for all customers on 2026-07-30
([changelog](https://github.blog/changelog/2026-07-30-github-models-is-now-retired/)).
Not usable. opencode supports GitHub Copilot instead, which needs a Copilot
plan.

### Groq

- Limits on coding-relevant models (gpt-oss-120b, gpt-oss-20b,
  qwen3.8-27b): 30 RPM, 1K RPD, 8K TPM, 200K TPD. The page labels the table
  for both Free and Developer plans, so treat it as an upper bound
  ([rate limits](https://console.groq.com/docs/rate-limits)).
- Key: none stored; needs sign-up. CLI: opencode native provider.
- Loom fit: 8K TPM is too tight for agent loops with large context; fine for
  short triage calls.

### Cerebras

No renewing free tier. New accounts get a one-time $5 credit after adding a
payment method, expiring after 30 days, "not intended for production use"
([pricing](https://www.cerebras.ai/pricing),
[rate limits](https://inference-docs.cerebras.ai/support/rate-limits)).
Requires a card, so out of scope.

### Other free tiers checked

- Z.ai: GLM-4.7-Flash and GLM-4.5-Flash are free for all token types, no
  published rate limits ([pricing](https://docs.z.ai/guides/overview/pricing)).
  opencode native. The most credible genuinely free API found, though the
  models are small.
- Mistral: a free mode with monthly usage inside console-only limits, for
  evaluation and prototyping
  ([usage limits](https://docs.mistral.ai/admin/billing-usage/usage-limits));
  API data is not used for training
  ([inference](https://mistral.ai/cloud/inference/)). Not a documented
  opencode provider.
- Ollama Cloud: a free plan with unspecified starter credits, starter models
  only, one concurrent request ([pricing](https://ollama.com/pricing)).
  opencode native.
- Not free: Hugging Face Inference Providers (no free monthly credits),
  SambaNova (needs card and credits), Together ($15 minimum), Chutes (paid
  plans), Moonshot Kimi ($1 minimum)
  ([HF](https://huggingface.co/docs/inference-providers/pricing),
  [SambaNova](https://cloud.sambanova.ai/plans),
  [Together](https://docs.together.ai/docs/billing),
  [Chutes](https://chutes.ai/pricing),
  [Kimi](https://platform.kimi.ai/docs/pricing/limits)).
- Qwen Code: the OAuth free tier ended 2026-04-15
  ([auth docs](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/)).
- Gemini CLI: replaced by Antigravity CLI for unpaid and Google One users on
  2026-06-18 ([quota page](https://geminicli.com/docs/resources/quota-and-pricing)).

## Open questions

- Whether Cloudflare Workers AI is worth a dedicated token, given the small
  daily allocation.
- Whether NVIDIA's "internal testing and evaluation" wording covers a
  harness doing the owner's own development work.
- How the OpenCode Terms clause on processes running "while you are not
  logged into the Services" applies to unattended Go runs.
- The live OpenRouter free-request cap, via `GET /api/v1/key`.
- The Poolside balance and the correct API host.

## Conclusion

Of Loom's five active lanes, four run on durable sources (Claude Max, Codex,
OpenCode Go, OpenRouter free) and one expires (Anthropic API credits, gone
after 2026-10-21). Local models wait on the deferred sparks lane, and agy
called directly with `-p` is the one addable subscription lane with a clean
terms path. OpenCode Zen free models and Z.ai free Flash
models are the best no-cost additions through opencode. Bedrock is the
only large-capacity option left, and it is paid per token.
