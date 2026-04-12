# Skills Map

Available skills — load with `skill_load("name")` in the first round when relevant.
This map is always in context. Skill files are loaded on demand only.

| Name | File | When to load |
|------|------|--------------|
| `3xui` | `prompts/skills/3xui.md` | Working with 3x-ui panel: inbounds, clients, traffic, certificates, webBasePath |
| `ssh-servers` | `prompts/skills/ssh-servers.md` | SSH key deploy, remote health check, systemd service management on remote hosts |
| `copilot-tgbot` | `prompts/skills/copilot-tgbot.md` | Working on copilot-telegram-bot project: bot.py, storage.py, web.py, billing trick, SQLite schema |
| `ouro-fitness-bot` | `prompts/skills/ouro-fitness-bot.md` | Working on ouro-fitness-bot: FatSecret, daemon, calisthenics program, deploy on server |
| `proxy-deploy-bot` | `prompts/skills/proxy-deploy-bot.md` | Working on proxy-deploy-bot: deployer pipeline, 3x-ui provider, diagnostics, SNI, Gemini integration, templates |

## Protocol

At the start of every task, check this map.
If the task touches a domain listed here — call `skill_load("name")` in the first round.
The skill content will be available from the second round onward.
Skills auto-reset after each task. Load them fresh each time.
