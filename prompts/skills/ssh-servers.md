# Skill: SSH Remote Servers

## Tools Available

Full SSH contour — use these instead of `run_shell("ssh ...")`:

| Tool | Purpose |
|------|---------|
| `ssh_target_register` | Register server alias (host, port, user, auth type) |
| `ssh_target_list` | List registered targets |
| `ssh_target_ping` | Test connectivity |
| `ssh_target_get` | Remove target |
| `ssh_session_bootstrap` | Bootstrap/validate SSH auth + multiplexed session |
| `ssh_key_generate` | Generate new SSH keypair |
| `ssh_key_list` | List managed keys |
| `ssh_key_deploy` | Deploy public key to remote server |
| `remote_command_exec` | Run command on remote host |
| `remote_read_file` | Read remote file |
| `remote_write_file` | Write file to remote host (with guardrails) |
| `remote_list_dir` | List remote directory |
| `remote_find` | Find files matching pattern |
| `remote_grep` | Grep in remote files |
| `remote_mkdir` | Create remote directory |
| `remote_service_status` | Check systemd service status |
| `remote_service_action` | start/stop/restart/enable/disable service |
| `remote_service_logs` | Get service journal logs |
| `remote_service_list` | List running services |
| `remote_server_health` | Full snapshot: uptime/load/disk/memory/ports/services/TLS |
| `remote_capabilities_overview` (alias `remote_operator_overview`) | Summary of contour capabilities |

## Typical Sequences

### Bootstrap new server (password auth → key auth)

```
1. ssh_target_register(alias="myserver", host="IP", user="root", auth_type="password")
2. ssh_session_bootstrap(target="myserver", password="PASS")
   # This validates connectivity + sets up multiplexed session
3. ssh_key_generate(name="myserver-key")
4. ssh_key_deploy(alias="myserver", key_name="myserver-key", password="PASS")
   # Deploys public key to ~/.ssh/authorized_keys
5. ssh_target_register(alias="myserver", host="IP", user="root", auth_type="key", key_name="myserver-key")
   # Re-register with key auth
6. ssh_session_bootstrap(target="myserver")
   # Validate key auth works
```

### Health check

```
remote_server_health(target="myserver")
# Returns: overall_status, uptime, load, disk, memory, open_ports, services, TLS certs
```

Good `overall_status=ok` means all checks passed. Red flags appear in the `issues` list.

### Run arbitrary command

```
remote_service_status(alias="myserver", service_name="nginx.service")
```

### Read file

```
remote_read_file(target="myserver", path="/etc/nginx/nginx.conf")
```

### Write file

```
remote_write_file(target="myserver", path="/etc/myapp/config.json", content="{...}")
# Guardrails: normalizes path, checks realpath before write
```

### Manage systemd service

```
remote_service_status(alias="myserver", service_name="nginx.service")
remote_service_action(target="myserver", service="nginx", action="restart")
remote_service_logs(alias="myserver", service_name="nginx.service", lines=50)
```

## Known Servers

| Alias | Host | Notes |
|-------|------|-------|
| `veles-de` (current) | `402213.vm.spacecore.network` | Main VPS, veles lives here |

To register the current server or any new server — use `ssh_target_register` first.

## Key Gotchas (from live smoke)

### Password bootstrap order matters
When bootstrapping with password for the first time, the bootstrap sequence must complete fully before attempting key deploy. `ssh_session_bootstrap` with `password=` handles this correctly — do not bypass it with raw `run_shell ssh`.

### Shell quoting in remote commands
`remote_command_exec` passes commands through SSH transport. Commands with special characters (`;`, `|`, `$`) are handled internally — do not double-escape.

### `remote_server_health` internals
Health command runs as a plain shell script via SSH wrapper (not nested `sh -lc`). If output is empty or sections missing — check SSH connectivity first (`ssh_target_ping`), then re-run bootstrap.

### Password deploy flow
`ssh_key_deploy` uses `_run_ssh_probe` internally (same as bootstrap), not a separate PTY helper. If key deploy hangs, check that `ssh_session_bootstrap` succeeded first.

### Avoid nested SSH
Do not `remote_command_exec(command="ssh other-host ...")` — this creates unreliable nested SSH. For chained server access, register each server separately and use direct tools.

## Firewall / nftables Checklist

After any firewall change on a server running X-Ray/VPN:
```bash
# Verify IPv6 ICMPv6 is allowed
nft list ruleset | grep -E 'icmp|ip6'

# Minimal required rules for IPv6 hosts
meta l4proto ipv6-icmp accept  # must be in input chain

# Test after change
ping -6 2606:4700:4700::1111
curl -6 -I https://cloudflare.com
```

## File Transfer Pattern

For moving files between servers (e.g. migration):
```
# Read from source
content = remote_read_file(target="old-server", path="/path/to/file")

# Write to destination  
remote_write_file(target="new-server", path="/path/to/file", content=content)
```

For large directories — prefer `remote_command_exec` with `tar | ssh` pipeline or rsync.

## Service Deploy Pattern

```
1. remote_write_file — write service config / env file
2. remote_write_file — write /etc/systemd/system/myapp.service
3. remote_service_action(alias="myserver", service_name="myapp.service", action="restart")
4. remote_service_action(service="myapp", action="enable")
5. remote_service_action(service="myapp", action="start")
6. remote_service_status(alias="myserver", service_name="myapp.service")
   # Verify: active (running)
```


## Xray / 3x-ui diagnostics

На 3x-ui хостах Xray часто не существует как отдельный `xray.service`: его поднимает `x-ui.service` как дочерний процесс. Поэтому сначала используй `remote_xray_status(alias="myserver")`, а уже потом проверяй `remote_service_status(alias="myserver", service_name="x-ui.service")`, `remote_service_logs(...)` и `remote_netstat(alias="myserver", state_filter="LISTEN")`. Отсутствие `xray.service` само по себе не означает падение Xray core.
