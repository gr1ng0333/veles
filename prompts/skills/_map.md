# Skills Map

Available skills — load with `skill_load("name")` in the first round when relevant.
This map is always in context. Skill files are loaded on demand only.

| Name | File | When to load |
|------|------|--------------|
| `3xui` | `prompts/skills/3xui.md` | Working with 3x-ui panel: inbounds, clients, traffic, certificates, webBasePath |
| `ssh-servers` | `prompts/skills/ssh-servers.md` | SSH key deploy, remote health check, systemd service management on remote hosts |

## Protocol

At the start of every task, check this map.
If the task touches a domain listed here — call `skill_load("name")` in the first round.
The skill content will be available from the second round onward.
Skills auto-reset after each task. Load them fresh each time.
