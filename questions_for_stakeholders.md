# swarm-city — Questions for Stakeholders

Unknowns that cannot be answered from the repository (audit of 2026-08-25).

1. **Hardware:** What GPU/RAM does the target host actually have? The default `balanced` profile needs `qwen3-coder:30b` + `gpt-oss:20b` + `qwen3:14b` loadable; `light` vs `balanced` vs `quality` is a hardware decision.
2. **DeepSeek:** Is a `DEEPSEEK_API_KEY` provisioned, what is the real monthly USD budget (the repo only has a 20M-token count), and do DeepSeek's commercial terms permit shipping its output into client/partner repositories?
3. **Default branch:** The GitHub default branch is `claude/repository-setup-3n542a`. Should it be renamed/repointed to `main` (the swarm-checks template and contributor expectations assume `main`)?
4. **Target repos:** Which repos are first-class apply targets (`my_publishing`, `alice_chains`, `epubnations`?), where do they live, and what is each one's real deterministic `TEST_COMMAND`?
5. **Sandbox:** What will `TEST_RUNNER_COMMAND` point to in production (Docker-in-Docker, firejail, a CI runner)? Apply mode is blocked on this by design.
6. **Supabase:** Keep and wire it (Epic 2), or drop the dead schema? If kept — who owns the service-role key, and is the anon key ever meant to grant console read access via RLS policies?
7. **Console scope:** Is the Vercel console read-only forever, or must it eventually carry the approval button (which requires auth — Supabase Auth? Vercel access controls? IP allowlist)?
8. **Load:** Expected concurrent tasks and daily task volume? `MAX_ACTIVE_TASKS=1` is the current ceiling — is single-flight acceptable for the first 90 days?
9. **GitHub credentials:** For `OPEN_PR=true`, which least-privilege credential (fine-grained PAT / GitHub App) will the orchestrator container get, and who owns rotating it?
10. **SLAs on third parties:** Any uptime/latency expectations on Ollama-host hardware, DeepSeek, Supabase, Vercel that we must design around or contractually promise?
11. **Team & on-call:** Who besides Renee operates this? Who reviews `approval_required` gates, and within what turnaround?
12. **Business validation:** Are the two hypothesized buyer-#2 design partners (studios needing local-first codegen) real named prospects we can pilot with in the 90-day window, and is $199/seat a tested price point or a placeholder?
