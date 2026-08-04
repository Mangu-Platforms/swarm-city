# Target-repository CI template

This directory is **not** part of this project's own CI. The workflow in
`.github/workflows/` at the repository root is what runs here.

`ci/.github/workflows/swarm-checks.yml` is a template to copy into a repository
the swarm opens pull requests against. It gives that repository an independent
check on swarm-authored changes — the swarm does not approve or merge its own
work, so this workflow is the gate that actually holds.

## Using it

```bash
cp ci/.github/workflows/swarm-checks.yml \
   /path/to/target-repo/.github/workflows/swarm-checks.yml
```

Then, in the target repository:

1. Replace the test-command detection block with that repository's real,
   deterministic test command. The bundled detection is a starting point, not
   something to ship as a required check.
2. Make the workflow a required status check on the protected branch.
3. Keep human review required. A passing check means the change did not break
   the suite; it does not mean the change is correct.

The `semgrep` step uses Community Edition rules and is pinned. Adjust or remove
it to match what the target repository already runs.
