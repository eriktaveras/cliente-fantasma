# Demo video outline (target 2:50)

Recording plan only: this file is not a completed video. Use English narration/captions and real footage of the current deployed application. A public YouTube URL is still required in Devpost.

| Time | Show | Explain |
|---|---|---|
| 0:00–0:15 | One actual quoted failure beside the approved business fact | Which customer problem the app detects. Show the run's real result; label any illustrative opening as an illustration. |
| 0:15–0:40 | English homepage (headline and the Nemotron / Token Factory / Tavily pills) → **I already have instructions** → Use an example → reviewed business profile | The intended users are small businesses with sales bots. The example business is fictitious; a generated bot is an example agent, not the owner's production bot. |
| 0:40–1:10 | Launch **Full**; real conversations and evaluations arriving | NVIDIA Nemotron Ultra generates customers with varied intent, attack, dialect and writing style; Nano runs the sales agent with simulated business tools. If time is compressed, label the footage as sped up. |
| 1:10–1:45 | The completed or interrupted report: coverage, actual score and exact failure quote | Ground truth comes from the reviewed business card. Missing coverage cannot approve a bot. Do not preselect a score or invent a successful result. |
| 1:45–2:05 | A real prompt suggestion and its acceptance/rejection status | Ultra proposes changes; training/holdout checks decide whether to accept them. A rejected or unvalidated suggestion is not a proven fix. |
| 2:05–2:25 | How it works: active Nano/Ultra role table; a sanitized config/usage excerpt from this run | Runtime calls go to Nebius Token Factory's API. Explain how its OpenAI-compatible endpoint supports separate models per role. The frontend is hosted on the homelab; do not claim AI Cloud/Serverless use. |
| 2:25–2:40 | **I have a live bot** form, webhook contract and n8n connector download | Existing bots can be evaluated through a webhook. Only claim a real n8n integration demonstration if it was actually recorded; opening the form alone is not integration evidence. |
| 2:40–2:50 | App URL, public GitHub URL, MIT license | The benefit is evidence for human review before deployment. Show actual observed cost/runtime if mentioned and the current automated test result; make no guarantees about latency or model accuracy. |

## Recording and release checks

- Use a real fresh Nebius run. Keep the unedited recording/artifacts so results can be traced to the app.
- Never show API keys, passwords, authorization headers, browser login prompts or private customer information. Use only the fictitious example data.
- Translate Spanish customer messages into English captions where needed.
- Use original/permitted assets. Avoid background music or logos unless usage rights are confirmed.
- Target 1080p and **2:50**, remaining below the three-minute requirement after intros/outros.
- Upload to YouTube or Vimeo as **Public** or **Unlisted** (no password), as the Devpost form allows. Test playback while signed out and paste the actual URL in `docs/devpost.md` and the Devpost form. Only the first three minutes are reviewed.
- A video does not replace working judge access: supply the judge account privately in Devpost and keep the app available through December 15, 2026.

## Plan A: a real, honest improvement

Use the sample run seeded with two common prompt mistakes (no human handoff; long bulleted answers with bold headers). Say on screen that the prompt was seeded with those mistakes. Both are fixable and are measured again on holdout cases the optimizer never saw.

## Plan B: if no version passes validation

Replace 1:35–2:10 with "detects and refuses to fake a fix": the three worst failures with their quote and the business fact they contradict, the score by customer type, and the rejected candidate with its plain-language reason. Never show a score that did not come from a real run.
