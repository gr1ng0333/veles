import json, time
d = json.load(open("/opt/veles-data/state/codex_accounts.json"))
now = time.time()
print("active_idx:", d.get("active_idx"))
for i, a in enumerate(d.get("accounts", [])):
    exp = a.get("expires", 0)
    has_a = "YES" if a.get("access") else "NO"
    has_r = "YES" if a.get("refresh") else "NO"
    rem = max(0, exp - now)
    dead = a.get("dead", False)
    err = a.get("last_error", {})
    cat = err.get("category", "-")
    print(f"  #{i}: access={has_a} refresh={has_r} expires_in={rem:.0f}s dead={dead} err_cat={cat}")
    if a.get("refresh"):
        print(f"       refresh_prefix={a['refresh'][:20]}...")
