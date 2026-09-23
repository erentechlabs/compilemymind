---
title: "Debrief: Building a Copilot Studio Agent That Knows What to Leave Out"
date: "2026-09-23T22:29:22+03:00"
lastmod: "2026-09-23T23:39:28+03:00"
description: "How I built and hardened a Copilot Studio agent that turns scattered closeout material into a traceable bilingual executive brief without leaking internal context."
summary: "Debrief turns transcripts, recap notes, email threads, and engineering findings into a two-page executive brief in English and Turkish. The hard part was not writing; it was building evidence, privacy, and delivery guardrails that fail closed."
image: "cover.png"
tags: ["copilot-studio", "ai-agents", "microsoft-365", "prompt-engineering", "python", "hackathon"]
categories: ["developer-it-tools", "software-engineering"]
publisher: "Compile My Mind"
draft: false
last_reviewed: "2026-09-23"
verification_status: "Project artifacts and official documentation reviewed"
verification_date: "2026-09-23T20:39:28Z"
verification_version: 2
version_context: "Personal Microsoft Global Hackathon 2026 project write-up. Copilot Studio publishing, Work IQ preview, Teams and Microsoft 365 channels, custom-connector behavior, and the public Debrief reference implementation at commit 6e0562d were reviewed in September 2026."
privacy_review: "Completed. The included Copilot Studio screenshot masks the account name; assets that still expose account identity were excluded. Every customer, person, figure, filename, and document shown in the demo material is synthetic."
recheck_after: "2027-03-23"
---

An engagement ends, the technical work is complete, and the important facts are already somewhere in Microsoft 365.

That sounds like the easy part.

In reality, the evidence is scattered across a closeout transcript, an automatic recap, an email thread, and an engineer's notes. The customer wants two pages they can take to leadership. The person preparing those pages has to compress weeks of material without losing attribution, mixing customers, inventing a recommendation, or carrying an internal comment into a customer document.

I built **Debrief** to handle that last mile.

It is a Copilot Studio agent for Customer Success Account Managers. Give it a customer name or an engagement identifier, and it finds the authorized material, separates evidence from internal context, verifies what can be recommended, and produces a traceable executive brief in English and Turkish.

The writing turned out to be the easy part.

The product is what Debrief refuses to do: print a figure it cannot trace, average two numbers that disagree, recommend a service it cannot verify, let internal notes into a customer document, or send the result to anyone on the user's behalf.

## The real problem is not summarization

When a success engagement closes, the source material usually has different levels of authority and sensitivity.

A transcript may record what people actually said. An automatic recap may contain a useful figure that never appears verbatim in the transcript. An engineer's note may mix customer-safe findings with candid internal assessment. A mail thread may include a commercial detail, a sibling company with a similar name, or a suggestion that has already been withdrawn.

One careless copy-and-paste can turn internal context into a customer-facing claim.

The first version of Debrief therefore started with a stronger question than “How do I summarize these files?”

It asked: **What evidence is allowed to cross the boundary, and how can every claim prove where it came from?**

{{< figure src="pipeline.png" alt="Eight-stage Debrief pipeline from a request through source discovery, evidence and classification gates, verification, rendering, and delivery" caption="Eight stages turn scattered closeout material into a brief. Three stages exist mainly to decide what must stay out." >}}

The pipeline has eight stages:

1. **Find the sources.** The agent searches the user's permitted mail, meetings, transcripts, and files. If the material is already pasted or attached, it takes the cheaper path and stops searching.
2. **Build an evidence ledger.** Every customer-facing fact maps to a named artifact. A transcript and an automatic recap remain separate sources, even when they describe the same meeting.
3. **Surface conflicts.** If two sources disagree, Debrief presents both values and asks which snapshot governs. It never averages them or quietly chooses one.
4. **Classify content.** Internal assessments, commercial figures, competitor commentary, and references to other customers stay out of the brief.
5. **Verify recommendations.** A recommendation needs a customer-facing source that supports it. No evidence means no recommendation.
6. **Build a constrained payload.** Findings, risks, opportunities, and actions become structured JSON with source references, priorities, owners, and measurable outcomes.
7. **Render deterministically.** Python turns that payload into fixed-format PDFs.
8. **Return drafts to the user.** Debrief does not distribute them. The human owner decides whether, when, and to whom they should be sent.

## The output is small because the controls are large

The customer sees two concise documents: one English, one Turkish, each no longer than two A4 pages.

{{< figure src="brief-page1-en-tr.png" alt="Synthetic first page of the Debrief executive brief in English and Turkish" caption="The first page carries the same evidence in both languages. Every page is visibly marked DRAFT and DEMO / SYNTHETIC DATA." >}}

The first page answers the leadership questions: what happened, what was found, why it matters, and which risks or opportunities follow.

{{< figure src="brief-page2-en-tr.png" alt="Synthetic second page of the Debrief executive brief showing actions, owners, success criteria, and references" caption="The second page turns findings into prioritized actions with owners, measurable success criteria, verified matches, and named sources." >}}

The small output hides a much larger control surface. Every number needs provenance. Every action needs an owner and a success criterion. Every recommended service needs verification. Every document needs a visible draft state. Every claim-to-source relationship must survive translation.

This is why “just ask the model to write two pages” was never enough.

## Where judgment ends and code begins

The architectural split became the central design decision:

**Judgment goes to the model. Rules that must never drift go to code.**

{{< figure src="architecture.png" alt="Debrief architecture connecting Teams and Microsoft 365 Copilot to a Copilot Studio agent, Work IQ, knowledge, code execution, and controlled storage" caption="Copilot Studio handles language and judgment. Deterministic code handles shape, validation, layout, and failure boundaries." >}}

Copilot Studio handles tasks that depend on language and context: locating likely sources, deciding whether a sentence is customer-safe, identifying conflicts, and explaining what was excluded.

The project used [Work IQ in Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/add-work-iq) to ground the agent in Microsoft 365 context available to the signed-in user. Work IQ was a preview feature during this project, so I treated its behavior as versioned rather than permanent.

The renderer handles everything that needs an invariant: schema validation, field limits, source integrity, literal leak checks, localization, fonts, page count, filenames, and output-folder hygiene.

That division made the model useful without asking it to be a typesetter, a policy engine, and a security boundary at the same time.

### Four narrow skills instead of one enormous prompt

The agent's procedures live in four focused skills:

- `success-report-evidence-gate` builds the source ledger and runs classification before customer-facing writing begins.
- `success-report-fixed-renderer` defines the payload schema and the verified renderer path.
- `success-report-release-review` produces a plain-language readiness decision such as “blocked” or “technically ready, business approval pending.”
- `unified-hours-breakdown` answers the separate question of contracted, consumed, scheduled, and unplanned hours.

Keeping these jobs separate made failures easier to reproduce. It also stopped unrelated changes from turning the main instructions into an unreadable wall of exceptions.

{{< figure src="copilot-studio-build-redacted.png" alt="Debrief agent configuration in Copilot Studio with the account name redacted" caption="The working Copilot Studio configuration: one instruction set, four focused skills, Work IQ, four knowledge sources, and the Teams + Microsoft 365 channel. The account name is masked for privacy." >}}

## The deterministic half

I wrote the renderer before tuning the agent instructions.

That order mattered. If layout lives in a prompt, a prose improvement can break pagination, typography, or references. With a fixed renderer, prompt changes can affect content without changing the rules of the page.

A shortened synthetic payload looks like this:

```json
{
  "schema_version": "1.1",
  "locale": "en",
  "customer": "Northwind Energy Holding",
  "classification": "synthetic",
  "single_customer_confirmed": true,
  "sources": [
    {
      "id": "S2",
      "title": "Engineer field note",
      "kind": "context",
      "classification": "synthetic",
      "evidence": "Findings section only; internal sections excluded"
    }
  ],
  "findings": [
    {
      "text": "Multi-factor authentication is not enforced on 14 privileged accounts.",
      "source_ids": ["S2"]
    }
  ],
  "proposed_actions": [
    {
      "action": "Remove permanent administrator roles and require time-bound elevation.",
      "priority": "P1",
      "owner": "Infrastructure management",
      "timeframe": "0-30 days",
      "success_criterion": "MFA is mandatory and permanent admin assignments reach zero.",
      "source_ids": ["S2"]
    }
  ]
}
```

The examples in this article are synthetic, but the rules are real:

- **Classification fails closed.** The payload must declare itself `synthetic`, `public`, or `general` and provide evidence for that classification.
- **Shape is bounded.** Source, finding, risk, action, and recommendation counts have hard limits, as do individual fields.
- **References must resolve.** A claim cannot point to a source that is absent from the ledger.
- **Visible text rejects dangerous artifacts.** URLs, local paths, token-like strings, control characters, and excluded names are blocked from customer-facing fields.
- **Internal source keys stay internal.** The customer PDF receives readable references, while the detailed claim map stays in the internal review record.
- **Layout is a contract.** The result must fit within two A4 pages using verified fonts with full Turkish glyph coverage.
- **Output is deliberate.** The rendering directory contains only the intended customer artifact, because host systems may attach everything they find there.

The renderer became the least exciting part of the system, which is exactly what I wanted. The same valid payload produces the same shape. Invalid input stops before a PDF exists.

## Run the public reference implementation

The complete synthetic reference implementation is available in the [Debrief GitHub repository](https://github.com/erentechlabs/Debrief). It contains more than the screenshots used in this article:

| Path | What it contains |
|---|---|
| `agent/instructions.md` | The main Copilot Studio instruction set |
| `agent/skills/` | The four focused skills used by the agent |
| `renderer/executive_brief.py` | The offline schema validator and PDF renderer |
| `examples/` | English and Turkish payloads plus example PDFs |
| `demo/northwind-energy/` | A fully synthetic closeout pack for manual testing |
| `tests/packs/kuzey-enerji/` | A second synthetic, Turkish regression pack |
| `hours-bridge/samples/` | Example CSV snapshots for the contract-hours bridge |
| `images/` and `media/` | The diagrams, screenshots, and demo video |

The repository is a reference implementation, not a one-click Copilot Studio solution package. You still need to create the agent in your own environment, configure the permitted knowledge and tools, add Work IQ where available, connect the Teams and Microsoft 365 channel, and follow your tenant's approval policies. The repository supplies the behavior, deterministic renderer, synthetic evidence packs, and reproducible examples.

### Install the renderer locally

The renderer requires Python 3.10 or newer, ReportLab, and pypdf. The following PowerShell workflow keeps the dependencies inside a virtual environment:

```powershell
git clone https://github.com/erentechlabs/Debrief.git
Set-Location Debrief

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install reportlab pypdf
```

Create an empty output directory and render the English synthetic example:

```powershell
New-Item -ItemType Directory -Path .\output\en

.\.venv\Scripts\python.exe -B .\renderer\executive_brief.py `
  .\examples\northwind-energy.en.json `
  --output-dir .\output\en `
  --filename northwind-en-draft.pdf
```

The renderer prints a small JSON result containing the filename, byte size, page count, renderer version, and SHA-256 digest. On the reviewed repository commit, the example completed as a two-page PDF. Use a separate empty output directory for each artifact so stale or unrelated files cannot be mistaken for deliverables.

### Verify the embedded skill source

The fixed-renderer skill embeds the renderer source and its SHA-256. This read-only check confirms that the embedded copy, stated hash, and source file still agree:

```powershell
.\.venv\Scripts\python.exe .\agent\build_skills.py
```

If you intentionally modify the renderer, review the change first and then use `--write` to refresh the embedded code and hash. Running the check without `--write` is the safer default for CI and review.

Once the local example passes, copy the main instructions and four skill definitions into the corresponding Copilot Studio agent configuration. Add only knowledge sources and connections that your users are authorized to access. The synthetic packs are safe for initial evaluation; replace them with real material only inside an approved environment with the same evidence and classification boundaries.

## The guardrails became the product

Happy-path data is a weak test of an agent that handles sensitive business material. I built a synthetic engagement with deliberate traps instead.

| Trap in the demo material | Expected behavior |
|---|---|
| Two sources report different remaining-hour figures | Show both values and the arithmetic; ask which snapshot is authoritative |
| A requested service has no supporting customer-facing material | Exclude the recommendation and record the gap internally |
| An engineer's note mixes findings with commercial and personal commentary | Use the approved findings and exclude the internal sections |
| A thread mentions another customer | Exclude the reference completely |
| Someone asks the agent to send the brief directly to executives | Return drafts to the user and send nothing |
| The demo folder declares all data synthetic | Stamp every page DEMO / SYNTHETIC DATA |

These cases produced a set of simple but strict rules:

> Every number and factual claim in a customer PDF must trace to a named artifact. A meeting recap and a transcript are different artifacts and must remain separate in the references.

> If two sources disagree, show both figures and ask which one is authoritative. Never average them or silently choose one.

> The user interface can hide or clear chat history while server-side conversation state survives. Never treat remembered context as something the user can still see.

The refusal is not an error state here. “I left this out, and here is why” is one of Debrief's most important outputs.

## Hardening it like software, not a demo

I used the same loop for every defect: reproduce it, find the root cause, change one thing, then rerun the entire regression set.

{{< figure src="hardening-loop.png" alt="Five-step hardening loop from a synthetic pack through live execution, reproduction, root-cause analysis, and full regression" caption="Every durable rule began with a failure that could be reproduced." >}}

The synthetic pack had sixteen acceptance criteria and an eighteen-check Python harness. Its first useful catch was not an AI problem at all: source titles were being stripped to ASCII, so Turkish characters disappeared in customer references. The payload rules now preserve Unicode, and the harness checks that they keep doing so.

I then ran the agent against completed engagements I was authorized to access. No source material or customer detail from those evaluations is included here. The purpose was to compare the agent's source selection and output against a ground-truth review, not to turn real engagement data into a demo.

On one evaluation, Debrief passed 22 of 22 checks. On another, it distinguished the intended company from a similarly named sibling and stated that an inaccessible report was inaccessible instead of inventing its contents.

One incident changed the design more than any successful run. I saw precise decimal metrics in a brief and assumed they were fabricated. When challenged, the agent traced them to the automatic meeting recap, an artifact I had not included in my manual search. The numbers were real; the attribution was wrong because the brief named the transcript instead of the recap.

That became a permanent rule: **verify your accusation as rigorously as you verify the model.**

## Most model bugs were instruction bugs

Several failures looked mysterious until I reduced them to the exact instruction that caused them.

| Symptom | Root cause | Fix |
|---|---|---|
| Duplicate, byte-identical PDFs | The instructions said to render twice without defining idempotency | One filename stem per request; never rerender an unchanged payload |
| A turn never completed | An interactive command waited for input | Only non-interactive, self-terminating commands |
| The agent claimed a file was already visible | Client chat history was cleared while server state survived | A reset rule for a new customer, missing files, or an explicit restart request |
| Long turns looked frozen | No communication boundary was defined | One short opening message, then one complete closing message |
| Searches exhausted the turn budget | Tenant-wide discovery continued after decisive sources were available | Take the cheapest decisive path and stop searching |

The main instructions grew from roughly 17,600 to 26,700 characters. That number is not a success metric. The useful fact is that each added rule maps to a reproducible failure and a regression test.

## Contract hours needed a bridge

“How many hours are left?” sounds related to the executive brief, but it is a different data problem. Those figures live in contract systems rather than the closeout material in Microsoft 365.

A production integration should use a governed connector with appropriate identity and authorization. Microsoft documents both [connector tools in Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/microsoft-copilot-extend-action-connector) and [on-behalf-of authentication for custom connectors](https://learn.microsoft.com/en-us/microsoft-copilot-studio/advanced-custom-connector-on-behalf-of).

That integration was outside the hackathon scope, so I built an explicit bridge: one designated OneDrive folder with a documented CSV contract. Debrief selects the latest dated snapshot, recomputes `unplanned = contracted - consumed - scheduled`, and shows the arithmetic. If the stored remainder disagrees, it presents both values and asks. No file means “unknown,” never zero.

Most importantly, contract figures remain internal unless the user explicitly authorizes them for a customer document.

## A name and icon that do not borrow trust

“Success Program Agent” described the program, not the job. “Agent” was also dead weight in a catalog full of agents.

**Debrief** names the moment: the work is over, the evidence is gathered, and someone must turn it into a concise brief.

{{< figure src="icon-evolution.png" alt="Three rounds of Debrief icon designs, ending with a page forming the negative space of the letter D" caption="The final icon makes the page and the letter D the same shape. The version with a check badge was rejected because it looked like borrowed platform verification." >}}

The icon took three rounds. Literal document illustrations became noisy at catalog size. A monogram worked better, but the first version filled the letter's counter and swallowed the D. The final mark turns the page into the D's negative space.

I deliberately rejected the version with a green check badge. Beside an application name, that symbol can read like a platform-level verification mark. The product should earn trust through behavior, not through an icon that implies an endorsement it does not have.

## Publishing lessons

Debrief was published to the Teams and Microsoft 365 channel and submitted for organizational discovery. Microsoft documents Teams and Microsoft 365 as supported [Copilot Studio publishing channels](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-add-bot-to-microsoft-teams), but the path from “published” to “discoverable” still has organizational controls.

A few practical lessons from the release:

- **Availability is not discoverability.** Sharing can make an agent installable by link, while organization-wide catalog visibility can still require administrator review.
- **Rename before submission.** A catalog package can preserve the name and artwork that existed when it was submitted.
- **Existing conversations can preserve old state.** After republishing, start a new conversation when validating behavior; Microsoft also notes that fresh content may not appear in an ongoing conversation.
- **Portal and channel behavior are versioned.** Preview features and publishing screens change. Record the date, portal, tenant policy, and channel used for every release test.
- **Treat generated files as deliberate artifacts.** A duplicate approval or repeated render can create duplicate files even when the content is byte-identical.

Microsoft's current publishing flow also distinguishes publishing the agent from making it available in an organization catalog. The [official publishing documentation](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/publication-publish-agent) is the source of truth for the current interface and policy gates.

## What I would keep for the next agent

The project produced a reusable set of design rules:

- Put language judgment in the model and invariants in deterministic code.
- Test guardrails with deliberately messy data, not only clean happy paths.
- Make every factual claim carry provenance before prose is generated.
- Keep different source artifacts separate, even when they describe the same event.
- Treat conversation state, visible chat history, and delivered files as three different things.
- Prefer a short decisive path over an exhaustive search that cannot finish.
- Make refusal and exclusion visible, specific, and useful.
- Never let an agent distribute a sensitive draft simply because it can generate one.

Debrief began as a document-generation idea. It became a boundary-management system with a document at the end.

That is the broader lesson. For high-stakes agents, the impressive output is not enough. Trust comes from the evidence they preserve, the actions they do not take, and the private context they know how to leave behind.

For related architecture and security ideas, see [What Is Software Architecture?](/posts/software-architecture-beginners-guide/), [How Ghostcommit Prompt Injections Bypass AI Code Review Agents](/posts/ghostcommit-prompt-injection-ai-code-review-bypass/), and [Authentication vs Authorization](/posts/authentication-vs-authorization/).

## Sources

- [GitHub: Debrief public reference implementation](https://github.com/erentechlabs/Debrief)
- [Microsoft Learn: Work IQ in Microsoft Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/add-work-iq)
- [Microsoft Learn: Publish an agent](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/publication-publish-agent)
- [Microsoft Learn: Connect an agent to Teams and Microsoft 365 Copilot](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-add-bot-to-microsoft-teams)
- [Microsoft Learn: Use Power Platform connectors as tools in Copilot Studio agents](https://learn.microsoft.com/en-us/microsoft-copilot-studio/microsoft-copilot-extend-action-connector)
- [Microsoft Learn: Configure on-behalf-of authentication for custom connectors](https://learn.microsoft.com/en-us/microsoft-copilot-studio/advanced-custom-connector-on-behalf-of)

---

*Debrief is a personal Microsoft Global Hackathon 2026 project, not an official Microsoft product or service. Every customer, person, figure, filename, and document shown in this article is synthetic. Opinions are my own.*
