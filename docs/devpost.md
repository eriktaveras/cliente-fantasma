# Devpost submission draft: Cliente Fantasma

Reviewed against the [official rules](https://nebiusglobalaihackathon.devpost.com/rules) on October 14, 2026. Submission deadline: **Friday, October 30, 2026, 10:00 AM PDT / 1:00 PM EDT**. Judging runs December 1–15, 2026.

This is a draft. Do not mark the submission complete until every remaining item below is verified.

## Basics

- **Project name:** Cliente Fantasma (Ghost Customer)
- **Tagline:** Find invented prices, unauthorized promises and manipulation failures in sales bots before real customers do.
- **Track:** Best Apps and Agents
- **Author:** Erik Taveras
- **Demo:** https://fantasma.eriktaveras.com/idioma/en?volver=/
- **Code:** https://github.com/eriktaveras/cliente-fantasma (MIT)
- **Video:** PENDING — public YouTube URL, duration below three minutes.
- **Built with:** NVIDIA Nemotron 3 Nano, NVIDIA Nemotron 3 Ultra, Nebius Token Factory, Tavily, Python, FastAPI, Jinja, Pydantic, SQLite, Docker, n8n and Cloudflare Tunnel.

## Inspiration and audience

Small businesses in Latin America increasingly put sales bots in their WhatsApp workflows. A bot that invents a price, accepts an invented discount or misunderstands informal regional Spanish can lose a sale or make an unauthorized commitment. Owners rarely have a dedicated evaluation team or a representative conversational test set. Cliente Fantasma gives them a repeatable way to inspect those failures with business-specific evidence.

## What it does

The owner supplies a price list and business policies, optionally supplemented by public web research. They review the extracted business card before starting a test. Synthetic customers then converse with either a generated example bot or an existing bot connected by webhook. The report shows evaluated coverage, scores, critical failures with exact quotes, dialect/attack breakdowns and suggested prompt changes with their validation status.

The generated mode does not claim to test an existing WhatsApp bot. The example business is fictitious. A rejected prompt proposal remains a suggestion, and an incomplete evaluation cannot approve a bot. Findings help prioritize human review; the automated judge still requires calibration.

## How it works and use of Nebius / NVIDIA

The hackathon configuration sends runtime requests to **https://api.tokenfactory.nebius.com/v1**:

| Role | NVIDIA model | Purpose |
|---|---|---|
| Sales agent under test | Nemotron 3 Nano 30B-A3B | Fast responses with simulated catalog, order, FAQ and human-escalation tools |
| Customer simulator | Nemotron 3 Ultra 550B-A55B | Multi-turn synthetic customers with regional Spanish and adversarial goals |
| Case generator | Nemotron 3 Ultra | Business-specific personas, expected behavior and evaluation criteria |
| Business-card extraction | Nemotron 3 Ultra | Structured products, prices and policies from supplied material |
| Judge | Nemotron 3 Ultra | Evidence-based grading grounded in the approved business card |
| Optimizer | Nemotron 3 Ultra | Prompt proposals tested against training and held-out cases |

Token Factory's OpenAI-compatible API lets the same client use separate models and reasoning settings per role without hosting model weights or provisioning GPUs. Nano handles the tested sales agent; Ultra handles the synthetic-data and evaluation workflow. Structured outputs, tool calls and token-usage accounting are part of the implementation. The web app runs in Docker on the author's homelab; inference runs on Nebius. Serverless Jobs/Endpoints are not used or claimed.

Optional Tavily search/extract supplements public business information. Owner-provided text takes precedence, and extraction checks product–price–currency associations. Missing information remains explicit. Live conversations use SSE; SQLite stores run artifacts. Conservative reservations bound model spending before requests; unknown model prices fail before inference.

## Product design and impact

The interface follows four stages: business information → review → live test → results. Advanced fields are folded, the mobile layout retains navigation, and English/Spanish controls are available. Results prioritize evidence and coverage rather than an isolated score. Existing bots can be connected with the documented HTTPS webhook/n8n contract.

The specific intended impact is earlier detection of pricing and policy failures for small-business sales automations. We do not claim measured revenue gains, universal dialect coverage or human-level judging accuracy. Offline scripted tests validate software behavior; they are not evidence of live model quality.

## Development during the submission period

Cliente Fantasma reuses the author's own *cuna* prompt-optimization engine. The project record describes that base engine as written in October 2026. The hackathon-specific work includes business-card extraction, synthetic-customer axes, the sales rubric, the existing-bot webhook, budgeted service, live web interface and shareable reports.

The October 14 changes add coverage integrity, stronger grounded evaluation, product–price association checks, protected critical rules, conservative request reservations, daily accounting, deployment hardening and backup restoration. The frontend was restructured into a coherent four-stage desktop/mobile workflow. These are substantial application changes rather than a reskin of the engine. If any base project actually predates **August 26, 2026**, confirm its original date and keep this explanation of the additions made during the submission period.

## Testing instructions for Devpost (private field)

1. Open the English demo URL above and click **View sample report** to read a completed analysis without signup or new inference. To run a new analysis, sign in with the judge account provided privately in the submission access instructions, or create an account. No manual approval is required. Do not show real credentials in screenshots or video.
2. Choose **Business facts only**, click **Use an example**, and review the fictitious Bluebird Store profile. **I already have instructions** tests a pasted prompt in the simulated environment; **I have a live bot** audits a webhook.
3. Select **Light** and launch. Watch conversations/evaluations; open the report when the run stops. Runtime varies and model requests are subject to a US$1 run cap.
4. Inspect coverage, exact failure messages, prompt-validation status, export and sharing controls. A budget stop produces a partial report, not a passing result.
5. Use **How it works** to inspect active models. For an existing bot, use **I have a live bot** with the README webhook contract.
6. If testing locally, follow the README setup, set a valid `NEBIUS_API_KEY`, copy `config.example.yaml` to `config.yaml`, and use pasted example data; Tavily is optional.

The operator must keep the hosted project free and available to judges through **December 15, 2026**, provide clear account-access instructions in the submission and ensure budget limits do not prevent their evaluation.

## Technical verification — October 14, 2026

- Public GitHub repository verified accessible with GitHub-detected MIT license.
- Deployed configuration verified: all six roles use NVIDIA Nemotron Nano/Ultra through Nebius Token Factory; the local Ornith provider is inactive.
- Both configured models returned successful live inference responses. A complete Light run through the hosted app finished with 15/15 cases and 15/15 required repetitions, at an observed US$0.463754. [Read-only report with fictitious business data](https://fantasma.eriktaveras.com/b/7E2rUIHGg4a_4bO4K02oQ5ax). The generated bot scored 26/100 with 11 critical failures; its prompt proposal was rejected. This demonstrates detection and transparent reporting, not successful model remediation or calibrated accuracy.
- Production Docker test target: **385 passed, 1 skipped**. The skipped opt-in live test was covered separately by the hosted end-to-end run; it remains skipped in the offline suite.
- English demo route and public report returned HTTP 200; the shared report was visually checked at a 390px mobile width.
- Code/UX/doc changes remain uncommitted on `design/ux-frontend-20261014`; the public default branch has not yet been updated to this release.

## Submission items still requiring release or author input

- [ ] Commit/publish the current audited code and UX to the public repository, then verify that the submitted branch contains this version and GitHub shows MIT.
- [ ] Paste a real public YouTube video URL below three minutes. Show the app working, the active Nemotron models and their runtime use of Token Factory. All narration/captions and submission materials must be English or include an English translation.
- [ ] Test the read-only public sample from a fresh browser, plus judge login and the registration-to-analysis flow, and update Devpost with the current access instructions.
- [ ] Confirm personal/team eligibility and any pre-existing project dates; select a Builders & Brews city only if actually attended.
- [ ] Confirm the final submission form contains description, category, demo, public code, video, feedback and any required prior-work explanation.
- [ ] Keep demo access, inference credits and availability working throughout judging. Current caps alone do not establish that future availability.

## Feedback on Nebius Token Factory / NVIDIA

Historical development observations recorded by the project author; latency and catalog availability may change. These are feedback, not claims about current service guarantees:

1. **Reasoning is on by default and it eats `max_tokens`.** On Nano, Lightning and Super, a request with a modest `max_tokens` often came back with the budget spent on thinking and an empty or truncated answer. For agent and chat use cases, off by default (or a clear per-model note in the catalog) would save people a lot of debugging.
2. **`/no_think` in the prompt does not work, but `chat_template_kwargs` does.** Sending `"chat_template_kwargs": {"enable_thinking": false}` turned reasoning off reliably. The `/no_think` system-prompt convention had no effect. This deserves a line in the Token Factory docs for each Nemotron model.
3. **Nemotron Super latency was erratic.** The same short request answered in 1.4 s, then in 77 s, then didn't answer within a 120 s timeout. I moved every role to Ultra, which was consistently fast. A status or latency indicator per model would help.
4. **No Nemotron omni or vision model in the catalog.** Small businesses on WhatsApp live on voice notes and product photos. A speech or vision Nemotron on Token Factory would unlock testing exactly that.
5. **Tool calling worked on Nano, Lightning and Ultra** with the OpenAI `tools` format, with no adapter needed. That's what made the Nano sales bot possible.
6. **Positive:** the OpenAI-compatible endpoint meant my existing client worked by changing the base URL, and per-request token usage made exact cost accounting easy.
