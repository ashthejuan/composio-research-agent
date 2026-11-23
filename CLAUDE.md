# Agent instructions

This is the centralized agent instructions file for this repository. All agents should follow these rules.

## Context, phases, and secrets

Refer to `context` directory for design, architecture, and/or security rules for the project. Also, update and create new files under `context/phases` whenever a major new feature or any changes are added to the project. Always keep this `/phases` directory in project root dir gitignore. These files will contain information about what has been implemented, how it has been done, and which files were changed. `/context` must be referenced as `context`. Never touch or view `.env` unless explicitly asked. Only make changes to `.env.example`. Go through `.agents/skills` for guidance on development best practices.


## Development rules

Use subagents (decide model and effort based on task. Use Sonnet for building basic features, writing tests, fixing isolated bugs, small code reviews, and generating standard APIs, Opus for complex, high-stakes, large scale code reviews, or open-ended problems where deep reasoning is required, such as designing multi-layered systems, planning large-scale refactors, or untangling deeply complex business rules, situations where you do not yet clearly know the exact requirements and need the model to figure out what needs to be solved, or where Sonnet gives shallow or repeated wrong answers on a tough problem and increasing its thinking effort doesn't fix it) to execute plans. Dispatch a fresh implementer subagent per task, a task review (spec compliance + code quality) after each, and a broad whole-branch review at the end. Why subagents: You delegate tasks to specialized agents with isolated context. By precisely crafting their instructions and context, you ensure they stay focused and succeed at their task. They should never inherit your session's context or history — you construct exactly what they need. This also preserves your own context for coordination work. Do not pause to check in with your human partner between tasks. Execute all tasks from the plan without stopping. The only reasons to stop are the four named below, or all tasks complete. "Should I continue?" prompts and progress summaries waste their time — they asked you to execute the plan, so execute it. Reasons to stop: an irreversible or destructive operation; a security-sensitive action; a side effect outside this worktree that norms say you ask about first (a merge, a push to a shared branch, a publish); and a plan so broken that every path forward is a guess. For those, stop and ask.

## Diagram Rules

When asked to explain something at any phase, use mermaid diagrams/flowcharts in the chat wherever you feel it helps. Architecture explanations, workflows, etc.