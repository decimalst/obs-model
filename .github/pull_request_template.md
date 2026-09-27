## Change

Describe the problem, resulting behavior, and operational effect.

## Validation

List the commands run and results. State unavailable checks and live scenarios not tested. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the suites relevant to this change.

## Operations

Describe migration, recovery, ownership, capacity, and cost implications, or state why they do not apply.

## Review checklist

Mark items that apply; explain any exceptions above.

- [ ] Tests cover changed behavior and relevant failure cases; generated artifacts are in sync.
- [ ] Regional routing, quota scope, debug expiry, and trace affinity remain correct where affected.
- [ ] Stateful changes have migration instructions; existing resource and controller ownership is preserved.
- [ ] The diff and attachments contain no credentials, state/plan files, private configuration, or captured telemetry.
- [ ] Claims distinguish local checks from live qualification; unexecuted live gates remain pending.
