
import sys, os, json, requests

sys.path.insert(0, r"E:\OperationsAssistantORIG\Tech\Code\NanoGhost\src")

# Load env
env_path = r"E:\OperationsAssistantORIG\Tech\Code\NanoGhost\instances\cc\.env"
with open(env_path, "r") as f:
    for line in f:
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip("'\"")

from agent_core.channel.feishu.api import get_tenant_access_token, _feishu_request

# 1. Check token
token = get_tenant_access_token()
print(f"[1] Token: {'OK' if token and len(token) > 20 else 'FAIL'}")

# 2. Test reaction API with a real message_id? No, let's check the app info first
# Check what permissions the app has by looking at app info
headers = {"Authorization": f"Bearer {token}"}
resp = requests.get(
    "https://open.feishu.cn/open-apis/im/v1/messages",
    headers=headers,
    timeout=10
)
data = resp.json()
print(f"[2] List messages API: code={data.get('code')} msg={data.get('msg', '')}")

# 3. Try reaction with fake message_id to check permission
resp2 = requests.post(
    "https://open.feishu.cn/open-apis/im/v1/messages/test_fake_msg_id/reactions",
    headers={**headers, "Content-Type": "application/json"},
    json={"reaction_type": {"emoji_type": "\u23f3"}},
    timeout=10
)
data2 = resp2.json()
code2 = data2.get("code")
print(f"[3] Reaction API test: code={code2} msg={data2.get('msg', '')}")
if code2 == 10002:
    print("    -> 10002 = invalid message_id (expected for fake msg), API works!")
elif code2 == 99991663:
    print("    -> 99991663 = permission denied, need im:message:reaction scope!")
elif code2 == 0:
    print("    -> Code 0 = success!")
else:
    print(f"    -> Unknown code, check response")

# 4. Check app info for scopes
resp3 = requests.get(
    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
    json={"app_id": os.environ.get("FEISHU_APP_ID"), "app_secret": os.environ.get("FEISHU_APP_SECRET")},
    timeout=10
)
print(f"[4] App token check: {resp3.json()}")
