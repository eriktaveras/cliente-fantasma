# Devpost submission draft: Cliente Fantasma

> Draft for the Nebius x NVIDIA Global AI Hackathon form. Fill the bracketed links before submitting (deadline: Thu Oct 30, 2026, 1:00 PM EDT).

## Basics

- **Project name:** Cliente Fantasma
- **Tagline:** Stress-test your WhatsApp sales bot with synthetic Latin American customers before real customers do.
- **Track:** Best Apps and Agents
- **Team:** Erik Taveras (solo)
- **Demo:** https://fantasma.eriktaveras.com
- **Code:** [GitHub link] (MIT)
- **Video:** [YouTube link]
- **Built with:** NVIDIA Nemotron 3 Nano, NVIDIA Nemotron 3 Ultra, Nebius Token Factory, Tavily, Python 3.12, FastAPI, Pydantic, SQLite, n8n, Docker, Cloudflare Tunnel

## Inspiration

Small businesses in Latin America sell on WhatsApp, and many are now putting an AI bot in that chat. I build these automations for clients and teach them to business owners in Spanish. The same pattern keeps showing up: the bot gets built in an afternoon and goes live without a single test. Then it invents a price, promises free delivery, gives a discount to anyone who says "the owner told me", or doesn't understand a Dominican customer who writes "klk cuanto e".

Nobody tests these bots because testing a conversational agent takes a team, a dataset and a week. A corner-store owner has none of those.

## What it does

You give it your business (a URL, or just paste your price list and policies). In about three minutes you get a report card:

1. **Business card.** Tavily reads what's public about the business and Nemotron Ultra turns it into a fact sheet: products, prices, hours, delivery zones, payment methods, policies. Code rejects any price that isn't in the source material. The owner reviews and edits it.
2. **Ghosts.** Nemotron Ultra generates synthetic customers along four axes: intent (buy, ask price, out-of-catalog, haggle, complaint, wholesale, delivery), attack (none, deception, prompt injection, pressure), dialect (Dominican, Mexican, Colombian, Argentine, Spain) and writing style (no accents, split messages, emojis, hurry, bad spelling). Each ghost has a hidden goal and the ground truth.
3. **Live conversations.** The ghosts chat with the bot, and you watch it happen live in the browser. The bot is either a Nemotron Nano agent built from your card, with simulated catalog, order, FAQ and escalation tools, or **your own bot** through a webhook (there's an n8n template).
4. **Judge.** Code checks every price and product the bot mentions against the card. Then Nemotron Ultra grades each conversation with a fixed sales-bot rubric (invents, promises, gives in to deception, escalates, advances the sale, understands the dialect, WhatsApp tone) plus criteria specific to the business.
5. **Fix.** Nemotron Ultra rewrites the bot's prompt from the failures. The change is accepted only if it improves on the training cases and holds on a holdout set.
6. **Report card.** Score 0-100, a verdict (Ready / Needs fixes / Don't ship it), the 3 worst failures with the exact message quoted, scores by dialect and by attack, the before/after prompt with a diff, a public share link and a JSONL export of every conversation.

## How we built it

- **Engine:** I started from *cuna*, my own prompt-optimization engine for sales agents (October 2026): an OpenAI-compatible client with one model per role, a simulator, an agent with simulated tools, a judge, an optimizer with a holdout set, and a SQLite store per run. For this hackathon I added the business card, the ghost axes and fixed rubric, Bring your bot, the budgeted service layer, the report card and the web app.
- **Web:** FastAPI with Jinja templates and plain JS. Conversations are streamed over Server-Sent Events. Each run executes in the background with a live budget.
- **Synthetic data is the core.** Everything the judge needs is generated: the personas, their hidden goals, the expected behaviour, the ground truth. The distribution is deterministic, so even a 15-ghost run covers every intent and every dialect, half of the ghosts attack, and no combination repeats in the first 200.
- **Cost control:** every LLM call is metered per role. A run is cancelled cleanly when it hits its cap, and the report card is built from what was measured. A global daily cap and per-visitor limits keep the public demo alive until judging ends.
- **Deployment:** Docker on a home server, exposed through a Cloudflare Tunnel. All inference runs on Token Factory.

## How we used NVIDIA Nemotron and Nebius Token Factory

All five roles run on Nemotron through Token Factory's OpenAI-compatible API (`https://api.tokenfactory.nebius.com/v1`):

| Role | Model | Reasoning |
|---|---|---|
| Sales bot under test | Nemotron 3 Nano 30B-A3B | off |
| Ghost customers | Nemotron 3 Ultra 550B-A55B | off |
| Case and card generator | Nemotron 3 Ultra | on |
| Judge | Nemotron 3 Ultra | off |
| Prompt optimizer | Nemotron 3 Ultra | on |

- **Nano is the bot** because it's what a small business can afford: fast, cheap and capable of tool calling. In a measured run the bot cost US$0.050 out of US$0.52, about 10% of the total.
- **Ultra does the synthetic data generation:** the cases, and the customers themselves turn by turn. It was by far the most convincing at regional Spanish, and it kept a hidden goal across a whole conversation. It also does the structured extraction for the card, the grading with quotes and the prompt rewriting.
- **Reasoning is toggled per role** with `chat_template_kwargs.enable_thinking`. It's on for generation and optimization, where it improves the output, and off for the bot, the ghosts and the judge, where it would only burn tokens and latency.
- **Structured output** (JSON schema) is used for the card, the cases, the judge verdicts and the optimizer proposals.
- **Real numbers:** a business card costs US$0.004-0.010. A light run with 15 ghosts and 1 optimization round takes 3 to 3.6 minutes and US$0.45-0.52 with the generated bot, or 1.5 to 2.5 minutes and US$0.22-0.26 against your own webhook (capped at US$1.00).

## How we used Tavily

Tavily **extract** reads the URLs the owner gives (website, link-in-bio page, menu). Tavily **search** (`include_raw_content=markdown`, 5 results) finds what else is public about the business. The owner's pasted text always goes first. If Tavily can't read a page, the run continues with what the owner pasted. Nemotron Ultra structures the material under a no-invention rule, and code double-checks every price.

## Challenges we ran into

- **The model guessed what the business sells.** The case generator only saw the business description. On the first real run Ultra read "colmado" (a Dominican corner store) as a mattress shop and generated ghosts asking about mattresses. Fix: product names go into the description. The next run had zero mattresses.
- **Empty catalogs.** In 1 of 5 runs Ultra returned a card with no products although the material had prices. Code validation now catches it and retries with the problem listed. The retry fixed it every time.
- **Judging a bot you can't see inside.** In Bring your bot mode there are no tool calls to check. Some checks would always pass and inflate the score, others would produce false criticals. I went through every check and decided which to skip, which to keep (invented prices against the card) and which to detect from the text (handing off to a human).
- **A public app that calls URLs typed by strangers.** That's an SSRF invitation. Fix: HTTPS only, every resolved IP must be global, the connection is pinned to the validated IP (no DNS rebinding), no redirects, and a size cap on the response.
- **The optimizer inflates prompts.** One proposal went from 1,112 to 3,311 tokens. The validator rejects proposals over the limit, and the report card shows them as a suggestion instead.
- **Spending real money in a public demo.** I needed per-run, per-visitor and global daily caps that cut cleanly in the middle of a run. They were tested for real: a capped run overshot by only US$0.009 of in-flight calls.

## Accomplishments that we're proud of

- The first real test found real bugs. The corner store's bot invented a delivery fee ("$5", "free from RD$150" when the minimum is RD$500) and quoted eggs at RD$380 instead of RD$330. The report card pointed to the exact message.
- It works with the bots people already have: the ghosts can talk to a real n8n bot.
- A light report card for 25 to 50 cents.
- 310 offline tests.

## What we learned

- For synthetic customers, voice matters as much as intent. A ghost that writes like a real Dominican customer finds failures that a polite, clean-Spanish tester never triggers.
- The judge needs ground truth, not opinions. The deterministic checks against the card are what make a "this bot invents prices" verdict trustworthy.
- Big model where it creates the data, small model where it serves the customer. That split is what makes the economics work.

## What's next

- Calibrate the judge against a hand-labelled set of conversations (the tooling exists: `calibrate`).
- Aggregate findings across all public runs ("X% of bots invent prices") as a public dashboard.
- Recurring tests: re-run the ghosts every time the owner changes their bot or their prices.
- More dialects (Peru, Chile, Venezuela, Puerto Rico) and currencies.
- Voice notes as input, if Token Factory adds a speech or omni model.

## Feedback on Nebius Token Factory / NVIDIA

Real findings from building this:

1. **Reasoning is on by default and it eats `max_tokens`.** On Nano, Lightning and Super, a request with a modest `max_tokens` often came back with the budget spent on thinking and an empty or truncated answer. For agent and chat use cases, off by default (or a clear per-model note in the catalog) would save people a lot of debugging.
2. **`/no_think` in the prompt does not work, but `chat_template_kwargs` does.** Sending `"chat_template_kwargs": {"enable_thinking": false}` turned reasoning off reliably. The `/no_think` system-prompt convention had no effect. This deserves a line in the Token Factory docs for each Nemotron model.
3. **Nemotron Super latency was erratic.** The same short request answered in 1.4 s, then in 77 s, then didn't answer within a 120 s timeout. I moved every role to Ultra, which was consistently fast. A status or latency indicator per model would help.
4. **No Nemotron omni or vision model in the catalog.** Small businesses on WhatsApp live on voice notes and product photos. A speech or vision Nemotron on Token Factory would unlock testing exactly that.
5. **Tool calling worked on Nano, Lightning and Ultra** with the OpenAI `tools` format, with no adapter needed. That's what made the Nano sales bot possible.
6. **Positive:** the OpenAI-compatible endpoint meant my existing client worked by changing the base URL, and per-request token usage made exact cost accounting easy.
