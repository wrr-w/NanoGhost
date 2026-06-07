import requests, json, os, sys

# Set env vars from the instance .env
env_path = r"E:\\OperationsAssistantORIG\\Tech\\Code\\NanoGhost\\instances\\cc\\.env"
with open(env_path, "r") as f:
    for line in f:
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip("'\"")

sys.path.insert(0, r"E:\\OperationsAssistantORIG\\Tech\\Code\\NanoGhost\\src")
from agent_core.channel.feishu.api import get_tenant_access_token

token = get_tenant_access_token()
print(f"Token obtained: {token is not None and len(token) > 20}")

if token:
    headers = {"Authorization": f"Bearer {token}"}
    
    # Check if we have reaction permission by hitting with a fake message_id
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages/FAKE_TEST_MSG/reactions",
        headers={**headers, "Content-Type": "application/json"},
        json={"reaction_type": {"emoji_type": "⏳"}},
        timeout=10
    )
    data = resp.json()
    print(f"Reaction API: code={data.get('code')} msg={data.get('msg')}")
    # 10002 = invalid message_id (expected for fake id) - means API is reachable
    # other codes might indicate permission issues
