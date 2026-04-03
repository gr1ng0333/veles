# Skill: 3x-ui Panel

## Panel Access

**Critical gotcha:** 3x-ui never listens on the root path. Always use `webBasePath`.
- Pattern: `http(s)://HOST:PORT/WEBBASEPATH/`
- Example: `http://94.156.122.66:2053/4w7plggEiurTWqZyLj/`
- On root `/` you get 404 — this is normal, not an error.
- Default port: `2053` (HTTP), panel may also serve on `443`/`8443` depending on config.

If `webBasePath` is unknown:
1. Check `/etc/x-ui/x-ui.db` via SSH: `sqlite3 /etc/x-ui/x-ui.db "SELECT key, value FROM settings WHERE key LIKE 'web%'"`
2. Or read `x-ui` settings file directly.

## Login

Default credentials (if not changed): `admin` / `admin`
After first login → always change password immediately.

Browser flow:
```
browse_page(url="http://HOST:PORT/WEBBASEPATH/")
→ fill username/password fields
→ click login button
→ check for redirect to dashboard (URL changes, or panel header appears)
```

Session persists in browser across calls. Logout is not needed between tool calls.

## Panel Structure

After login, main sections:
- **Inbounds** — VPN tunnel configs (VLESS, VMess, Trojan, Shadowsocks)
- **Xray Logs** — real-time log viewer
- **Settings** — panel config (port, basepath, TLS, auth)
- **Panel Cert** — TLS cert for the panel itself (not for inbounds)

## Inbound Formats

### VLESS + Reality (recommended for modern setups)

```json
{
  "tag": "inbound-443",
  "port": 443,
  "protocol": "vless",
  "settings": {
    "clients": [
      {
        "id": "UUID-HERE",
        "flow": "xtls-rprx-vision",
        "email": "user@example"
      }
    ],
    "decryption": "none"
  },
  "streamSettings": {
    "network": "tcp",
    "security": "reality",
    "realitySettings": {
      "show": false,
      "dest": "google.com:443",
      "xver": 0,
      "serverNames": ["google.com", "www.google.com"],
      "privateKey": "PRIVATE_KEY_HERE",
      "shortIds": ["SHORT_ID_HERE"]
    }
  },
  "sniffing": {
    "enabled": true,
    "destOverride": ["http", "tls", "quic"]
  }
}
```

### VLESS + WebSocket + TLS

```json
{
  "tag": "inbound-ws",
  "port": 8443,
  "protocol": "vless",
  "settings": {
    "clients": [{"id": "UUID-HERE", "email": "user@example"}],
    "decryption": "none"
  },
  "streamSettings": {
    "network": "ws",
    "security": "tls",
    "tlsSettings": {
      "certificates": [
        {
          "certificateFile": "/path/to/cert.pem",
          "keyFile": "/path/to/key.pem"
        }
      ]
    },
    "wsSettings": {
      "path": "/ws"
    }
  }
}
```

### VMess + TCP

```json
{
  "tag": "inbound-vmess",
  "port": 2096,
  "protocol": "vmess",
  "settings": {
    "clients": [{"id": "UUID-HERE", "alterId": 0, "email": "user@example"}]
  },
  "streamSettings": {
    "network": "tcp"
  }
}
```

## Client Management

Add client to existing inbound via panel UI:
1. Inbounds → find row → click edit (pencil icon)
2. In modal: scroll to "Clients" section → click "+ Add Client"
3. Fill: UUID (auto-generate or paste), Email (unique label), optional Flow/Expire

Generate UUID: `cat /proc/sys/kernel/random/uuid` or use panel's auto-generate button.

**Client link formats:**
- VLESS: `vless://UUID@HOST:PORT?security=reality&...#LABEL`
- Panel generates share links automatically in client row → QR code / copy link

## Traffic and Monitoring

- **Inbound traffic** — shown in Inbounds table (↑↓ counters per inbound)
- **Client traffic** — shown in inbound edit modal, per-client row
- **Reset traffic**: inbound row → reset traffic button (circular arrow)
- **Xray logs**: Xray Logs section → shows real-time connection log

## Certificate Management

**Panel TLS cert** (for the panel web UI itself):
- Settings → Panel Cert section
- Update `webCertFile` / `webKeyFile` paths
- Or via SSH: `sqlite3 /etc/x-ui/x-ui.db "UPDATE settings SET value='/path/cert.pem' WHERE key='webCertFile'"`
- Then restart: `systemctl restart x-ui`

**Gotcha:** `x-ui setting -webCert ... -webCertKey ...` CLI may not update DB reliably in some installs. Verify DB directly after CLI update.

**Inbound TLS cert** (for VLESS/VMess inbounds):
- Edit inbound → streamSettings → tlsSettings → certificateFile / keyFile

**Certificate renewal gotcha:**
- If port `80` is owned by another service (nginx, ghost, caddy), standalone ACME renewal fails.
- Solution: reuse existing domain cert from the other service, or use DNS challenge.
- `acme.sh --renew -d DOMAIN --ecc` needs port 80 free for HTTP challenge.

## Typical Sequences

### Add new inbound
1. Inbounds → `+` button
2. Fill: remark (name), port, protocol, stream settings
3. Enable toggle → Submit
4. Verify: inbound appears in list with ↑↓ = 0

### Add client to inbound
1. Inbounds → edit (pencil) on target inbound
2. Add client → fill UUID + email
3. Submit
4. Copy share link for client

### Check if panel is alive
```python
browse_page(url="http://HOST:PORT/WEBBASEPATH/")
# Check: if URL ends with /login or shows login form → panel up, not logged in
# If redirects to dashboard → already logged in
```

### Update panel cert
1. SSH to server
2. Write new cert/key files to `/root/cert/xui/`
3. Update DB: `sqlite3 /etc/x-ui/x-ui.db "UPDATE settings SET value='...' WHERE key='webCertFile'"`
4. `systemctl restart x-ui`
5. Verify: `curl -vk https://HOST:2053/WEBBASEPATH/ 2>&1 | grep -A2 "subject\|issuer\|expire"`

## IPv6 Gotcha (nftables)

If X-Ray inbounds are up but traffic dies: check nftables for ICMPv6.
```bash
nft list ruleset | grep icmp
# If ICMPv6 missing → add: meta l4proto ipv6-icmp accept
# Then: nft -f /etc/nftables.conf
```
Symptom: `ip -6 neigh` shows router as FAILED, `ping -6` fails.

## Known Servers

| Alias | Host | Panel URL pattern |
|-------|------|-------------------|
| `spacecore-94` (old, deleted) | `94.156.122.66` | `:2053/4w7plggEiurTWqZyLj/` |
| New DE server | `402213.vm.spacecore.network` | Check webBasePath via SSH if needed |
