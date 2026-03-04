"""One-shot VPS deployment script for Veles. Run locally, executes via SSH."""
import paramiko
import sys
import time
import os

HOST = "80.90.179.90"
USER = "root"
PASS = "jD@M_EVnT2dMZ@"
GITHUB_TOKEN = "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"

def ssh_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)
    return ssh

def run(ssh, cmd, label="", timeout=120):
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
    print(f"$ {cmd[:200]}{'...' if len(cmd)>200 else ''}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    if out.strip():
        print(out[-3000:] if len(out) > 3000 else out)
    if err.strip():
        # Filter out pip warnings and other noise
        for line in err.strip().split("\n"):
            if "WARNING" not in line and "DEPRECATION" not in line:
                print(f"  [stderr] {line}")
    if rc != 0:
        print(f"  [exit code: {rc}]")
    return rc, out, err

def main():
    step = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    ssh = ssh_connect()
    print(f"Connected to {HOST}")

    # ---- Step 0: Copy SSH key ----
    if step <= 0:
        pubkey_path = os.path.expanduser("~/.ssh/id_rsa.pub")
        if os.path.exists(pubkey_path):
            pubkey = open(pubkey_path).read().strip()
            run(ssh, f'mkdir -p ~/.ssh && echo "{pubkey}" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && chmod 700 ~/.ssh',
                "Step 0: Copy SSH public key")
            print("SSH key copied. Key-based auth should work now.")

    # ---- Step 1: Clone repo & setup venv ----
    if step <= 1:
        run(ssh, "rm -rf /opt/veles", "Step 1a: Clean old install")
        run(ssh, f"git clone https://{GITHUB_TOKEN}@github.com/gr1ng0333/veles.git /opt/veles",
            "Step 1b: Clone repo", timeout=60)
        run(ssh, "cd /opt/veles && git checkout veles && git log --oneline -3",
            "Step 1c: Checkout veles branch")
        run(ssh, "python3 -m venv /opt/veles/venv",
            "Step 1d: Create virtualenv", timeout=60)
        run(ssh, "/opt/veles/venv/bin/pip install --upgrade pip -q && /opt/veles/venv/bin/pip install openai requests playwright -q",
            "Step 1e: Install Python deps", timeout=120)
        run(ssh, "/opt/veles/venv/bin/playwright install chromium 2>&1 | tail -5",
            "Step 1f: Install Playwright Chromium", timeout=180)
        run(ssh, "/opt/veles/venv/bin/playwright install-deps 2>&1 | tail -10",
            "Step 1g: Install Playwright system deps", timeout=180)
        print("\n✅ Step 1 complete: repo cloned, venv ready")

    # ---- Step 2: SearXNG via Docker ----
    if step <= 2:
        searxng_compose = r"""version: '3'
services:
  searxng:
    image: searxng/searxng:latest
    container_name: searxng
    ports:
      - "8888:8080"
    volumes:
      - ./settings.yml:/etc/searxng/settings.yml
    restart: unless-stopped"""

        searxng_settings = r"""use_default_settings: true
server:
  secret_key: "veles-searxng-secret-2026"
  bind_address: "0.0.0.0"
  port: 8080
search:
  formats:
    - html
    - json"""

        run(ssh, "mkdir -p /opt/searxng", "Step 2a: Create SearXNG dir")
        run(ssh, f"cat > /opt/searxng/docker-compose.yml << 'DCEOF'\n{searxng_compose}\nDCEOF",
            "Step 2b: Write docker-compose.yml")
        run(ssh, f"cat > /opt/searxng/settings.yml << 'STEOF'\n{searxng_settings}\nSTEOF",
            "Step 2c: Write settings.yml")
        # Check if docker-compose or docker compose is available
        rc, _, _ = run(ssh, "command -v docker-compose && echo 'has-compose' || (docker compose version && echo 'has-plugin')")
        if "has-compose" in _:
            compose_cmd = "docker-compose"
        else:
            compose_cmd = "docker compose"
        run(ssh, f"cd /opt/searxng && {compose_cmd} up -d",
            "Step 2d: Start SearXNG", timeout=120)
        print("Waiting 8s for SearXNG to start...")
        time.sleep(8)
        run(ssh, 'curl -s "http://localhost:8888/search?q=test&format=json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\'SearXNG OK: {len(d.get(chr(114)+chr(101)+chr(115)+chr(117)+chr(108)+chr(116)+chr(115),[]))} results\')" 2>&1 || echo "SearXNG check failed"',
            "Step 2e: Verify SearXNG")
        print("\n✅ Step 2 complete: SearXNG running")

    # ---- Step 3: Data directories ----
    if step <= 3:
        run(ssh, "mkdir -p /opt/veles-data/{state,logs,memory/knowledge}",
            "Step 3a: Create data dirs")
        run(ssh, "cp /opt/veles/identity.md /opt/veles-data/memory/identity.md 2>/dev/null; cp /opt/veles/scratchpad.md /opt/veles-data/memory/scratchpad.md 2>/dev/null; ls -la /opt/veles-data/memory/",
            "Step 3b: Copy identity files")
        print("\n✅ Step 3 complete: data directories ready")

    # ---- Step 4: Create .env ----
    if step <= 4:
        # Codex tokens from openclaw auth-profiles
        codex_access = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjE5MzQ0ZTY1LWJiYzktNDRkMS1hOWQwLWY5NTdiMDc5YmQwZSIsInR5cCI6IkpXVCJ9.eyJhdWQiOlsiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS92MSJdLCJjbGllbnRfaWQiOiJhcHBfRU1vYW1FRVo3M2YwQ2tYYVhwN2hyYW5uIiwiZXhwIjoxNzczNDM4MjY5LCJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYzgzMzEzMDctYzVjZi00MmM1LTk2YmItZGJlYjU5NjQyMDNiIiwiY2hhdGdwdF9hY2NvdW50X3VzZXJfaWQiOiJ1c2VyLWhCcjNSRmx3bzEyRWdxUGJtRVh1R01CTl9fYzgzMzEzMDctYzVjZi00MmM1LTk2YmItZGJlYjU5NjQyMDNiIiwiY2hhdGdwdF9jb21wdXRlX3Jlc2lkZW5jeSI6Im5vX2NvbnN0cmFpbnQiLCJjaGF0Z3B0X3BsYW5fdHlwZSI6InBsdXMiLCJjaGF0Z3B0X3VzZXJfaWQiOiJ1c2VyLWhCcjNSRmx3bzEyRWdxUGJtRVh1R01CTiIsInVzZXJfaWQiOiJ1c2VyLWhCcjNSRmx3bzEyRWdxUGJtRVh1R01CTiJ9LCJodHRwczovL2FwaS5vcGVuYWkuY29tL3Byb2ZpbGUiOnsiZW1haWwiOiJzdGVyZWNiZXJkZXJ3QHByb3Rvbi5tZSIsImVtYWlsX3ZlcmlmaWVkIjp0cnVlfSwiaWF0IjoxNzcyNTc0MjY4LCJpc3MiOiJodHRwczovL2F1dGgub3BlbmFpLmNvbSIsImp0aSI6IjMwYWI2YmJhLTY1ODEtNDMwYi1hZDI3LTY5MjhiNTE5MGMzMSIsIm5iZiI6MTc3MjU3NDI2OCwicHdkX2F1dGhfdGltZSI6MTc3MjU3NDI2NzUxMCwic2NwIjpbIm9wZW5pZCIsInByb2ZpbGUiLCJlbWFpbCIsIm9mZmxpbmVfYWNjZXNzIl0sInNlc3Npb25faWQiOiJhdXRoc2Vzc183cENud0RsdkFaTGNFRGM0dmpRRzlWNE8iLCJzdWIiOiJhdXRoMHxZUEFLMWRMaEdiWmQ5OXNLaHlGQ1EwMUIifQ.1PXtKAfnMNkuzJjiHGV6qdPIO9QodkJzpkWcj_uAvcUkYLlVuXzHOpry1DXp6nEJJVHgGEdrSI0xyqKowMh5tyc0bPryfdncbrlP3sgG591BqMxNcRGb0aTmUArNmWAlz_Iy1N2-ZL4BHIt9WGIBEzmNsgzBFqaVKMcH7a8UmBoPakm08hMmnaevY3UTCdV7fh6o1-dG9eNx7jYTiU3YWjgBZNUyHkzrDJmb_IHNvdRiimhaV-rZc8RCG8eh4FSFDC0CX8GYzKMwvdYUY_rofH1GCcKJMmv1SUvHqXQZcv2ffcIEWXvg5_KePNO0jPj-epN-NBAgClqahiw8ueETKGbt2mmBL4R38FmyDE1URHOmuBpiqWjkfvmzPKYo7-gREhwgyJyC5qjBFiEgOaYL6LwKrgh0ACV_kzj5GWrVYx1uKEYyQy5lNQTEyFrn5eEfgjOcHZNmMJhZbQUmlgut03aJPHgjgsKMZVVLBYVFWNaPjh-O1ldpAujoKUQstf9p4VL1TUVLl-ogbSBGJNopTUxAhmO4Ar1O1fEwsLZl72sY0-xklQAHWq_8qNXfGwldlh2eYkqAWS3JbacToQpCmkcpF_fT949hEsSAWWCfwrnwkWMOPhzqZs38XELp4j8EHF9ct_CDAx7cpyu8bVe49JKMEnPrEngc-BG461RUh08"
        codex_refresh = "rt_m0FeieEjPM1xWk0Klu-Z4GccQ3FEJODDuP1hi0GV32Q.lu0JX3sAtivWHDOz1Wl7Xo9K5uafbmWAS9jMtOQeQjg"
        codex_expires = "1773438269"
        codex_account = "c8331307-c5cf-42c5-96bb-dbeb5964203b"

        env_content = f"""OPENROUTER_API_KEY=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
TELEGRAM_BOT_TOKEN=8639479997:AAHgman8_jAeZzzs1AWif5KNr_1exVfzIYY
GITHUB_TOKEN={GITHUB_TOKEN}
GITHUB_USER=gr1ng0333
GITHUB_REPO=veles
OUROBOROS_BRANCH_DEV=veles
SEARXNG_URL=http://localhost:8888

OUROBOROS_MODEL=codex/gpt-5.3-codex
OUROBOROS_MODEL_LIGHT=anthropic/claude-haiku-4.5
OUROBOROS_MODEL_CODE=codex/gpt-5.3-codex
OUROBOROS_EXTRA_MODELS=anthropic/claude-sonnet-4.6,anthropic/claude-haiku-4.5
OUROBOROS_MODEL_FALLBACK_LIST=anthropic/claude-haiku-4.5

CODEX_ACCESS_TOKEN={codex_access}
CODEX_REFRESH_TOKEN={codex_refresh}
CODEX_TOKEN_EXPIRES={codex_expires}
CODEX_ACCOUNT_ID={codex_account}

TOTAL_BUDGET=100
OUROBOROS_BG_BUDGET_PCT=5
OUROBOROS_MAX_ROUNDS=20
OUROBOROS_MAX_WORKERS=1
OUROBOROS_SOFT_TIMEOUT_SEC=180
OUROBOROS_HARD_TIMEOUT_SEC=600
"""
        # Write .env via sftp to avoid shell escaping issues with JWT
        sftp = ssh.open_sftp()
        with sftp.file("/opt/veles/.env", "w") as f:
            f.write(env_content)
        sftp.close()
        run(ssh, "wc -l /opt/veles/.env && echo '--- first 10 lines ---' && head -10 /opt/veles/.env",
            "Step 4: Verify .env written")
        print("\n✅ Step 4 complete: .env created with all tokens")

    # ---- Step 5: Launch in screen ----
    if step <= 5:
        run(ssh, "apt install -y screen 2>&1 | tail -3", "Step 5a: Install screen")
        # Kill existing screen if any
        run(ssh, "screen -X -S veles quit 2>/dev/null; sleep 1", "Step 5b: Kill old screen")
        run(ssh, "screen -dmS veles bash -c 'cd /opt/veles && source venv/bin/activate && set -a && source .env && set +a && python3 colab_launcher.py 2>&1 | tee /opt/veles-data/logs/launcher.log'",
            "Step 5c: Launch Veles in screen")
        time.sleep(5)
        run(ssh, "screen -ls", "Step 5d: Verify screen session")
        print("\n✅ Step 5 complete: Veles launched")

    # ---- Step 6: Verify ----
    if step <= 6:
        print("\nWaiting 10s for launcher to initialize...")
        time.sleep(10)
        run(ssh, 'curl -s "http://localhost:8888/search?q=test&format=json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\'SearXNG: {len(d.get(chr(114)+chr(101)+chr(115)+chr(117)+chr(108)+chr(116)+chr(115),[]))} results\')" 2>&1 || echo "SearXNG FAILED"',
            "Step 6a: Check SearXNG")
        run(ssh, "tail -30 /opt/veles-data/logs/launcher.log 2>/dev/null || echo 'No log yet'",
            "Step 6b: Check launcher log")
        run(ssh, "ps aux | grep -E 'colab_launcher|python3' | grep -v grep",
            "Step 6c: Check process")
        print("\n✅ Step 6 complete: verification done")

    ssh.close()
    print("\n🎉 Deployment complete!")

if __name__ == "__main__":
    main()
