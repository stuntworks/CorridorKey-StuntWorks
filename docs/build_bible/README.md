# Build Bible System

A reusable, layered documentation system for finished projects. You run any completed build through it once, and you get a source of truth that lets the project be rebuilt correctly and that stops an AI from stalling the work with "it cannot be done."

This folder is the document. The skill comes next and points at these files. The skill is deliberately thin, because the knowledge lives here, not in the skill.

The system is 14 files (`00` through `13`): a router (`00_INDEX.md`) plus thirteen self-contained parts, each loaded only when its concern is touched. Every technical claim that matters carries an evidence tag (VERIFIED, PARTIALLY VERIFIED, HYPOTHESIS, FAILED APPROACH, STALE-REVERIFY, or USER DECISION) with a last-verified date and a recheck trigger, so a reader always knows how much to trust a given line without re-deriving it. The doctrine underneath the hard part (Part 04) and the open ledger (Part 13) is failure-first: every dead end tried before the working solution is recorded on purpose, because a hard part with no failed attempts behind it has probably not been documented honestly, and the failures are what map the territory that does not work.

## Why it is layered

A flat document forces an agent to swallow the whole thing to answer one question, which burns tokens on every call. This system splits into a cheap router (`00_INDEX.md`) that is always read, and one detailed part per concern that is loaded only when the task touches it. A question about the build chain never pulls in the hard-part chapter.

## The parts

- `00_INDEX.md` is the router. Always read first. It maps a task to the one part to load, carries the protected-list guardrail, and defines the evidence-tag system.
- `01` through `12` are the parts, each self-contained, each a template you fill per project.
- `13_open_issues_and_decisions.md` is the live ledger: what is currently broken, undecided, or half-verified. It exists even when empty, and issues graduate out of it into Part 08 once solved.
- `examples/` holds one filled example, to show the pattern in use. It is an example, not the system.

## How to use it

1. To document a finished project, work through parts 01 to 13, filling each with that project's real specifics, including the Part 04 graveyard. Mark any part that does not apply as not applicable, in one line, rather than deleting it, so the router stays valid across every project.
2. To answer a question about a documented project, read `00_INDEX.md`, match the task, load that one part. For any code or config change, also load Part 08.

## What the skill will do

The skill will reference this system, not restate it. On a task about a finished project, it reads the index, picks the part, loads only that part. To document a new finished project, it walks the parts as a template and fills them from the repo, mining failed attempts from handoffs and logs, not just the working history. Thin skill, heavy reference, low token cost per call.

## The rule that holds the whole thing together

Structural knowledge (identity, rules, architecture, the hard part, the pushback, the standard) does not drift and is trusted. Drift-prone values (versions, paths, thresholds, prices) are corralled in Part 12 and marked check-against-live. Failures, once written down with their root cause, are trusted assets, not embarrassments to hide. Trust the walls, verify the furniture, keep the graveyard honest.
