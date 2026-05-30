"""Web search using urllib (bypasses proxy). Called as: python search_web.py <query>"""
import urllib.request, urllib.parse, re, sys

query = sys.argv[1] if len(sys.argv) > 1 else ""
if not query:
    print("请提供搜索关键词")
    sys.exit(0)

results = []

# 1. Try Sogou
url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    html = resp.read().decode("utf-8", errors="replace")
    if len(html) > 5000 and "antispider" not in html and "请输入验证码" not in html:
        pairs = re.findall(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
        for path, title in pairs[:10]:
            t = re.sub(r"<[^>]+>", "", title).strip()
            u = "https://www.sogou.com" + path if path.startswith("/") else path
            if t:
                results.append((t, u))
        if results:
            print(f"source: sogou")
            print(f"count: {len(results)}")
            for t, u in results:
                print(f"title: {t}")
                print(f"url: {u}")
                print()
            sys.exit(0)
except Exception:
    pass

# 2. Fallback to Bing
url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&mkt=zh-CN"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    html = resp.read().decode("utf-8", errors="replace")
    if len(html) > 5000:
        pairs = re.findall(r'<h2>.*?<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
        for u, title in pairs[:10]:
            t = re.sub(r"<[^>]+>", "", title).strip()
            if t and "bing.com" not in u and "microsoft.com" not in u:
                results.append((t, u))
        if results:
            print(f"source: bing")
            print(f"count: {len(results)}")
            for t, u in results:
                print(f"title: {t}")
                print(f"url: {u}")
                print()
            sys.exit(0)
except Exception:
    pass

print("source: none")
print("count: 0")
print("所有搜索源均被拦截")
