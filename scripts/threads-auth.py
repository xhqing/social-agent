#!/usr/bin/env python3
"""Threads OAuth 授权：拿长期 access token，写进本机 env 文件。

官方流程（Meta for Developers，2026 核对）：
  1. 授权窗口：https://threads.com/oauth/authorize?client_id=&redirect_uri=&scope=&response_type=code
  2. 换短期 token：POST https://graph.threads.com/oauth/access_token（返回 access_token + user_id）
  3. 换长期 token：GET https://graph.threads.com/access_token?grant_type=th_exchange_token
     → 有效期 60 天，可在到期前刷新（GET /refresh_access_token?grant_type=th_refresh_token）

前置（用户在开发者后台做，脚本无法代劳）：
  · 建 Meta app，选 Threads use case；记下 App settings → Basic 里的「Threads App ID / App secret」
  · Valid OAuth URIs 里加上本脚本用的 redirect_uri（必须一模一样，注意尾部斜杠）
  · App roles → Roles → Add People → Threads Tester，把你自己的 Threads 账号加进去
  · 到 threads.com/settings/account → 网站权限，接受邀请

用法：
  # 自动模式：本地起一个回调服务
  python3 scripts/threads-auth.py --app-id <ID> --app-secret <SECRET> \
      --redirect-uri http://localhost:8765/callback

  # 手动模式：授权后浏览器会跳到回调地址（打不开没关系），把地址栏整条 URL 粘回来
  python3 scripts/threads-auth.py --app-id <ID> --app-secret <SECRET> \
      --redirect-uri https://example.com/callback --manual

凭证写到 --out（默认 ~/.dev/private/social-agent/threads.env，权限 600）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

AUTH_URL = "https://threads.com/oauth/authorize"
TOKEN_URL = "https://graph.threads.com/oauth/access_token"
LONG_TOKEN_URL = "https://graph.threads.com/access_token"
ME_URL = "https://graph.threads.com/v1.0/me"
SCOPE = "threads_basic,threads_content_publish"


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}：{exc.read().decode('utf-8', errors='replace')}") from None


def _post_form(url: str, data: dict[str, str]) -> dict:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}：{exc.read().decode('utf-8', errors='replace')}") from None


def build_auth_url(app_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "response_type": "code",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(app_id: str, app_secret: str, redirect_uri: str, code: str) -> dict:
    """短期 token + user_id。"""
    return _post_form(
        TOKEN_URL,
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )


def exchange_long(app_secret: str, short_token: str) -> dict:
    """长期 token（60 天）。"""
    query = urllib.parse.urlencode(
        {"grant_type": "th_exchange_token", "client_secret": app_secret, "access_token": short_token}
    )
    return _get(f"{LONG_TOKEN_URL}?{query}")


def whoami(token: str) -> dict:
    query = urllib.parse.urlencode({"fields": "id,username", "access_token": token})
    return _get(f"{ME_URL}?{query}")


def write_env(path: str, token: str, user_id: str, username: str, expires_in: int) -> None:
    expanded = os.path.expanduser(path)
    os.makedirs(os.path.dirname(expanded), exist_ok=True)
    expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + expires_in))
    lines = [
        "# Threads API 凭证（本机私有，不进 git）",
        f"# 授权账号：@{username}（user id {user_id}）",
        f"# 获取时间：{time.strftime('%Y-%m-%d %H:%M')}",
        f"# 到期时间：{expires_at}（60 天，到期前可用 refresh_access_token 续期）",
        f"THREADS_ACCESS_TOKEN={token}",
        f"THREADS_USER_ID={user_id}",
    ]
    with open(expanded, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(expanded, 0o600)
    print(f"[OK] 凭证已写入 {expanded}（权限 600）")


class CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    expected_path: str = "/callback"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        if "code" in query:
            CallbackHandler.code = query["code"][0]
            body = "授权成功，回到终端继续。可以关掉这个页面。"
        else:
            CallbackHandler.error = query.get("error_description", ["未知错误"])[0]
            body = f"授权失败：{CallbackHandler.error}"
        payload = f"<html><meta charset='utf-8'><body style='font:16px system-ui;padding:2em'>{body}</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def log_message(self, *args) -> None:  # 静音
        pass


def parse_code_from_url(raw: str) -> str:
    """从回调 URL / 整条地址栏里取 code。"""
    raw = raw.strip()
    query = urllib.parse.urlparse(raw).query or raw
    params = urllib.parse.parse_qs(query)
    if "code" not in params:
        raise RuntimeError(f"这段里没有 code 参数：{raw}")
    return params["code"][0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Threads OAuth 授权，拿 60 天 access token")
    ap.add_argument("--app-id", required=True, help="App Dashboard → App settings → Basic → Threads App ID")
    ap.add_argument("--app-secret", required=True, help="同上，Threads App secret")
    ap.add_argument("--redirect-uri", required=True, help="必须与后台 Valid OAuth URIs 完全一致")
    ap.add_argument("--manual", action="store_true", help="手动模式：授权后粘贴回调 URL")
    ap.add_argument("--out", default="~/.dev/private/social-agent/threads.env", help="凭证输出路径")
    args = ap.parse_args()

    state = str(int(time.time()))
    auth_url = build_auth_url(args.app_id, args.redirect_uri, state)

    print("== 第 1 步：在浏览器里打开下面的授权地址，用你的 Threads 账号同意授权 ==\n")
    print(auth_url)
    print()

    if args.manual:
        print("== 第 2 步：授权后浏览器会跳到回调地址（可能打不开），把地址栏整条 URL 粘到这里 ==")
        try:
            raw = input("回调 URL（或只贴 code）：").strip()
            code = parse_code_from_url(raw)
        except (EOFError, RuntimeError) as exc:
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1
    else:
        parsed = urllib.parse.urlparse(args.redirect_uri)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
            print(
                "[ERR] 自动模式只支持 http://localhost:<端口>。\n"
                "      用 https 或其它域名的回调请加 --manual，授权后手动粘贴地址。",
                file=sys.stderr,
            )
            return 1
        CallbackHandler.expected_path = parsed.path or "/callback"
        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
        print(f"== 第 2 步：等待回调（监听 127.0.0.1:{port}{CallbackHandler.expected_path}，最长 300 秒）==")
        server.timeout = 300
        while CallbackHandler.code is None and CallbackHandler.error is None:
            server.handle_request()
        server.server_close()
        if CallbackHandler.error:
            print(f"[ERR] 授权失败：{CallbackHandler.error}", file=sys.stderr)
            return 1
        code = CallbackHandler.code or ""

    print("\n== 第 3 步：换取 token ==")
    try:
        short = exchange_code(args.app_id, args.app_secret, args.redirect_uri, code)
    except RuntimeError as exc:
        print(f"[ERR] 换短期 token 失败：{exc}", file=sys.stderr)
        print("      常见原因：redirect_uri 与后台配置不完全一致（含尾部斜杠）、授权码已用过或超过 1 小时。", file=sys.stderr)
        return 2

    user_id = str(short.get("user_id", ""))
    short_token = short.get("access_token", "")
    if not short_token:
        print(f"[ERR] 返回里没有 access_token：{short}", file=sys.stderr)
        return 2
    print(f"      短期 token 已拿到，user_id={user_id}")

    try:
        long = exchange_long(args.app_secret, short_token)
    except RuntimeError as exc:
        print(f"[ERR] 换长期 token 失败：{exc}", file=sys.stderr)
        return 3
    token = long.get("access_token", "")
    expires_in = int(long.get("expires_in", 5183944))
    if not token:
        print(f"[ERR] 返回里没有长期 token：{long}", file=sys.stderr)
        return 3
    print(f"      长期 token 已拿到，有效期约 {expires_in / 86400:.0f} 天")

    username = "(未知)"
    try:
        me = whoami(token)
        username = me.get("username", username)
        user_id = str(me.get("id", user_id))
        print(f"      授权账号：@{username}")
    except RuntimeError as exc:
        print(f"      [警告] 取账号信息失败：{exc}")

    write_env(args.out, token, user_id, username, expires_in)
    print("\n下一步：")
    print("  python3 scripts/threads-post.py --body <正文> --attachment <附件> --env-file "
          + args.out + " --publish")
    return 0


if __name__ == "__main__":
    sys.exit(main())
