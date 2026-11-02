# Loom

Loom keeps an operator's goals moving by turning them into tickets and spreading the work across every subscription, key and local model the operator has, within each one's limits.

## Goals and work

Titles and kind labels follow docs/NAMING.md: the label carries the kind, and a title has one leading kind word at most (`Goal:`, `Epic:`, `Map:`).

**Goal**:
A durable outcome the operator wants, defined by a home issue in the project's own repo and labelled `loom:goal`.
_Avoid_: project, mission, objective

**Backlog goal home**:
The goal home that holds a repo's whole backlog, labelled `loom:goal` and `loom:backlog`; triage runs on it every hour.
_Avoid_: backlog issue, Backlog: title

**Goal home**:
The issue in a project repo that states a goal, its success condition and its rules; its children are the goal's epics.
_Avoid_: goal file, GOAL.md

**Epic**:
A finite chunk of work under a goal: an issue labelled `epic` with a done-when, holding at most 15 tickets, closed when its done-when holds. A goal's children are epics, and a goal has at most 6 open at once.
_Avoid_: milestone, project, initiative

**Ticket**:
One scoped unit of work in an epic, an issue in the project's repo with Why, What, Acceptance criteria, Size and Out of scope.
_Avoid_: task, job, story

**Map**:
A wayfinder map: one issue labelled `wayfinder:map` that plans an unclear piece of work as decision tickets, resolved one at a time until the way is clear and the work can be filed as tickets.
_Avoid_: roadmap, plan, spec

**Ticket state**:
Where a ticket stands, carried by the one state label on it: queued, briefed, running, pr, failed, shipped or verified.
_Avoid_: status, label

**Brief**:
A ticket rendered for one lane: everything a worker needs to do it, and nothing else.
_Avoid_: prompt, assignment

## Doing the work

**Lane**:
A worker that turns a brief into a pull request with one inference source, such as one subscription, API key or local model.
_Avoid_: worker, agent, queue

**Runner**:
The process on the lane machine that takes a lane's briefs one at a time and opens the pull request; the agent inside it never pushes or merges.
_Avoid_: poller, daemon

**Station**:
The process on the lane machine that turns briefed tickets into local briefs for the runners, reports each outcome back to the ticket, publishes lane status and forwards asks to the operator; the only part of the lane machine that talks to GitHub about tickets.
_Avoid_: runner, poller, supervisor

**Heartbeat**:
A lane's latest report of whether it can take work right now, and why not.
_Avoid_: status, ping

**Lane status**:
The one issue in this repo labelled `loom:lanes`, whose comment the station keeps current with a table of lane, available and heartbeat time.
_Avoid_: heartbeat, dashboard

**Quota**:
The usage a lane's inference source still allows before its limit, window or credit runs out.
_Avoid_: budget, allowance

**Validation contract**:
A repo's `.loom/validation.conf`: the interpreter, submodules, setup and check its CI uses, which the runner prepares before the agent and runs again on the agent's commit; only a passing check opens the pull request.
_Avoid_: test config, CI file

**Transcript**:
The full record of one agent session on one brief: the brief, the agent's messages and every tool call with its result.
_Avoid_: log, chat history

## Deciding

**Brain**:
The single orchestrator the operator talks to; it decides what to brief, reviews, ships and verifies, and does no work a lane can do.
_Avoid_: orchestrator, foreman, dispatcher

**Tick lock**:
The branch one tick holds while it works, so two ticks never run at once; the commit on it names the holder and when the hold expires, and the tick deletes it when it is done.
_Avoid_: mutex, lease

**Ship**:
Merge a reviewed pull request and roll it out under the project's own rules.
_Avoid_: deploy, release

**Verify**:
Confirm on the live system that a shipped change shows its proof and moved the metric it targets.
_Avoid_: test, check

**Audit**:
The brain re-reading the evidence of a proof a lane's verify run made; a failed audit moves the ticket back to shipped and holds that lane's verify runs for a day.

**Close reason**:
The plain-English comment `bus/closing.py` posts in the thread before any close or reopen. A close with no reason is refused.

**Close audit**:
A check of what git holds for a ticket's close: each commit the closer cited, whether the default branch carries it, and which merged pull request delivered it. `bus.brain audit-close` prints it; the station reopens an unproven close by anyone else, once per ticket.
_Avoid_: review, recheck

**Journal**:
The comment the brain leaves on a goal's home issue for each tick that changed something (shipped, briefed, waiting, asks and cost), with a five-line report every two hours.
_Avoid_: log, summary
