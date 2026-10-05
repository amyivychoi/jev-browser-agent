# Architecture

## Modules

| Module | Responsibility |
| --- | --- |
| `jev_ultrafast/page_reader.py` | Attach Browser Use to the Browser Harness-owned Chrome target and produce a structured DOM/accessibility snapshot. It does not perform actions. |
| `jev_ultrafast/model.py` | Send the structured state and bounded action candidates to Jev through OpenRouter's Decisions API; use the configured chat model only when Jev needs a fallback. |
| `jev_ultrafast/agent.py` | Run the observe → decide → validate → act loop and keep action history. |
| `jev_ultrafast/browser.py` | Orchestrate Browser Use observations and apply the final freshness, visibility, hit-test, and action checks before dispatching an operation through Browser Harness. |
| `jev_ultrafast/demo.py` | Serve the loopback-only web interface and manage the local service lifecycle. |

## Decision and action flow

```text
Browser Use / structured DOM and accessibility understanding
          ↓
Structured page state + observed action candidates
          ↓
Jev selects one operation and compatible candidate
          ↓
Code-owned validation: candidate membership, freshness, visibility, hit test
          ↓
Browser Harness executes one bounded operation
          ↓
Browser Use observes the resulting page
```

Browser Use reads the page title, accessible DOM tree, headings, visible controls, form state, and viewport/scroll information. It reads the DOM of whatever page is in focus, with no host-based content exemptions, matching upstream Browser Use. Password fields are excluded from action candidates, and Browser Use's serializer omits password values.

Any page the agent reaches — mailbox and account pages included — therefore has its text and action candidates sent to the configured decision providers. Chrome runs with the user's real profile and login state, so this is the practical privacy boundary: the providers in `.env` see the pages the run visits.

### Candidate rules

A candidate's label comes from its own accessible name first (`aria-label`, `title`, AX name, `placeholder`, `alt`), then its own short visible text, then a heading within two levels or an ancestor within four. A label is never borrowed from an arbitrary descendant, so a container that wraps a result list cannot inherit the first title inside it.

A candidate is marked a result title link only when it is a link or button whose own label is that heading text. `heading_label` therefore identifies a real primary result link, never its wrappers.

Candidates require a semantic interactive role or a non-generic click listener. Missing geometry never drops a candidate: Browser Harness re-resolves fresh bounds and hit-tests before acting. A candidate that only wraps another candidate is removed, as is a click duplicate with the same role, label, and href; de-duplication never empties the list. The remainder is ranked — result title links, then fields and submits, then links and buttons, then everything else — and capped at 60, so results survive a dense page. Every drop is counted in `candidate_diagnostics`, with a sample of dropped labels for debugging.

Page identity for freshness covers the target ID, URL, title, headings, offered actions, and guard values. Ambient page text is excluded so ads, clocks, and lazy content cannot invalidate a valid decision.

Labels are compared with their self-changing parts removed: bracketed counters (`收件箱 (3)`), relative times (`2 分钟前`, `5 minutes ago`), clock values (`09:41`), and any remaining digit runs. Freshness asks whether this is still the same control, and an unread count that ticks over while the model decides is not a different control. The normalisation applies to the guard label, the page title, headings, and per-action labels. It is for comparison only: the model always receives the real label. Identity-bearing words are untouched, so `收件箱` and `草稿箱` stay distinct. Labels that are purely numeric, such as pagination links, normalise to nothing and remain distinguishable through the guard's backend node ID and `href`.

### New tabs

A link with `target="_blank"` is offered normally and flagged `new_tab` for the model. After each action the agent compares the page target list captured before execution against the list after it; a target that appeared becomes the new focus. Both halves move together — the observation target Browser Use reads and the CDP session Browser Harness dispatches input through — so the next decision is made about the page the click actually produced. This mirrors Browser Use's own post-click focus switch.

Following a new tab counts as progress. Without it, a `_blank` link leaves the original target and URL unchanged, the run reads its own success as a stall, and it clicks again: the mechanism behind repeated identical tabs. Tabs the run opens and then leaves are tracked and closed when the run tears down.

### A closed page

The mirror case is the run's own page disappearing. Browser Use's session manager recovers from a detached focus target by switching to any remaining tab, and those tabs belong to the user. Assigning `agent_focus_target_id` does not make a target exist, so every observation first checks that the target is still among the live page targets and raises `TargetLost` when it is not.

The run then stops with `target-lost`. Nothing is re-observed, nothing is sent to the reviewer, and no freshness retry is spent: a verdict on an unrelated tab would be both meaningless and a disclosure of that tab. Without this check the failure is silent and misattributed — the observation succeeds, returns another tab's DOM while still reporting the dead target's ID, and page identity never matches again, so the run reports `stale-page` until its retries run out.

### Page review on a stale-page stop

A page that rewrites itself can exhaust the freshness retries while already satisfying the goal. When the third retry is spent, the agent sends the goal, the freshly observed page, and the last eight actions to the chat model for a verdict before reporting a dead end. The reviewer reports `met` plus a short reason; it never selects an action and never rewrites the user's instruction.

`met` finishes the run with stop reason `review-goal-met`. Not met clears the stale counter and grants one more pass on the fresh observation. A reviewer that errors, returns a non-boolean `met`, or is unreachable leaves the original `stale-page` stop in place. Rescues are capped at two per run, so a page that always reports not met can still stop.

The verdict does not substitute for the user's own check: when completion text is configured, `review-goal-met` is still verified against the page text like any other completion. Page content is treated as untrusted data in the reviewer's prompt — text on the page cannot change the goal or the instructions. Each review is recorded in `review_calls` with its trigger, verdict, reason, model, and latency.

### Form focus rules

Two rules narrow candidates around a partly filled form, and both yield to visible results. When a form holds values and its own submit button is offered, candidates outside that form are dropped so the run submits rather than wandering off. When that submit button is covered, an offered dismiss or minimize control is preferred over unrelated navigation.

Either rule, applied to a results page, would hide every result and leave only a control that cannot progress. A page counts as showing results when any non-submit click candidate carries a result title or an `href`; heading markup alone is not the test, since many sites render result titles without `h1`-`h6`. While results are visible, neither rule drops anything.

Jev can only select operation and target identifiers already present in the observation. Python validates the response against that candidate set. Immediately before execution, Browser Use takes a fresh structured observation and the agent checks the selected node's identity and guard values. Browser Harness then resolves the Browser Use backend node ID and rechecks that the element is connected, enabled, visible, in the viewport, and not covered at the point it is about to click. The executor dispatches one click, text entry, dropdown selection, scroll, or wait. It never retries a browser mutation automatically.

### Choosing the point to click

A search result's title is an inline `<a>` laid out on two lines: the site's url above, the title heading below. An inline element's client rects are its per-line fragment boxes and carry no leading, so `getBoundingClientRect()` returns a union taller than the lines it covers and the union's centre falls in the gap between them. Hit-testing that one point lands on whatever is painted in the gap, and the action is refused as covered — measured on a live Google results page as `union y=323 h=45, centre [490,345] -> span`, against line boxes of 16px whose own centres hit the link.

So the check tries the union centre first, then each line box, and clicks the first point that resolves to the element or one of its descendants. This is what clicking the visible text does. Occlusion detection is unaffected: an overlay covers every candidate point, and a point outside the viewport is still refused, so the run scrolls rather than clicking blind.

Before this, the correct result link could be selected repeatedly and refused every time, surfacing as `stale-page` — a decision problem by appearance and a geometry problem in fact. `tests/test_hit_point.py` runs the shipped function against pages built from layout rules rather than one site's markup, and skips when no browser is reachable.

Jev decisions use OpenRouter's Decisions API with `~typesafe/jev-latest`. If Jev errors, returns invalid choices, selects a candidate below the confidence threshold, or blocks while usable actions remain, the agent asks an independent chat model to choose from the same observation. If `DEEPSEEK_API_KEY` is set, fallback decisions and generated field text go directly to DeepSeek; otherwise they use OpenRouter. Jev remains on OpenRouter.

Text entry uses a separate bounded text-generation request. It may infer a search query from the goal but does not invent credentials or personal details that the user has not supplied. A missing or invalid value stops the run before typing.

## Service boundaries

The web interface needs only a goal. It starts at Google when no URL is supplied; optional URL and visible-text completion verification remain available. The server binds to `127.0.0.1` and uses a per-process token for UI commands.

The agent opens an owned background Chrome target through Browser Harness and attaches Browser Use to that target for read-only page understanding. Browser Use telemetry and DOM highlights are disabled. Browser Harness remains the only action executor.

Service restart is intended to replace the Python process, close the active Browser Harness target, clear in-memory task state, reread `.env`, and load current code. Service stop shuts down the process; a new terminal command is required to start it again. The running process must be restarted after source changes.

This local MVP has no user accounts, persistent task database, queue, or site-specific scripts. Custom keyboard widgets remain outside the supported action set. The OpenRouter/DeepSeek providers receive the goal and structured page state required for their configured model calls, for every page the run reaches.
