# Loom

Loom keeps an operator's goals moving by turning them into tickets and spreading the work across every subscription, key and local model the operator has, within each one's limits.

## Goals and work

**Goal**:
A durable outcome the operator wants, defined by a home issue in the project's own repo and labelled `loom:goal`.
_Avoid_: project, mission, objective

**Goal home**:
The issue in a project repo that states a goal, its success condition and its rules.
_Avoid_: goal file, GOAL.md

**Ticket**:
One scoped unit of work toward a goal, an issue in the project's repo with Why, What, Acceptance criteria, Size and Out of scope.
_Avoid_: task, job, story

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

**Heartbeat**:
A lane's latest report of whether it can take work right now, and why not.
_Avoid_: status, ping

**Quota**:
The usage a lane's inference source still allows before its limit, window or credit runs out.
_Avoid_: budget, allowance

**Transcript**:
The full record of one agent session on one brief: the brief, the agent's messages and every tool call with its result.
_Avoid_: log, chat history

## Deciding

**Brain**:
The single orchestrator the operator talks to; it decides what to brief, reviews, ships and verifies, and does no work a lane can do.
_Avoid_: orchestrator, foreman, dispatcher

**Ship**:
Merge a reviewed pull request and roll it out under the project's own rules.
_Avoid_: deploy, release

**Verify**:
Confirm on the live system that a shipped change shows its proof and moved the metric it targets.
_Avoid_: test, check
