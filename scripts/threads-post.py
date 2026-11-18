#!/usr/bin/env python3
"""把一条帖子（正文，可选文字附件）发到 Threads。

官方机制（Meta for Developers，2026 核对）：
  1. 创建媒体容器：POST https://graph.threads.com/v1.0/{user-id}/threads
     参数 media_type=TEXT、text=<正文>、text_attachment={"plaintext": "<附件全文>"}
  2. 发布：POST https://graph.threads.com/v1.0/{user-id}/threads_publish，creation_id=<容器 id>

限制（官方文档）：
  · 正文 500 字符；文字附件 10,000 字符
  · 文字附件只能配纯文本帖（不能带图、视频、投票）
  · 链接数 ≤5（2025-12-22 起超过直接报 THREADS_API__LINK_LIMIT_EXCEEDED）
  · 发布额度 250 条帖 / 24 小时

凭证（三选一）：
  · 环境变量 THREADS_ACCESS_TOKEN 与 THREADS_USER_ID
  · --env-file 指定的文件，内含 THREADS_ACCESS_TOKEN=... / THREADS_USER_ID=...
    推荐放在 ~/.dev/private/social-agent/threads.env（不进 git）

用法：
  python3 scripts/threads-post.py --body <正文文件> [--attachment <附件文件>]      # 默认 dry-run，只校验
  python3 scripts/threads-post.py --body ... --attachment ... --publish          # 真发

退出码：0 成功；1 校验失败；2 凭证缺失；3 接口报错
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://graph.threads.com/v1.0"
BODY_LIMIT = 500
ATTACH_LIMIT = 10_000
LINK_LIMIT = 5
URL_RE = re.compile(r"https?://\S+", re.I)


def load_env(path: str | None) -> tuple[str, str]:
    """读凭证：环境变量优先，其次 env 文件。"""
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    uid = os.environ.get("THREADS_USER_ID", "").strip()
    if path and (not token or not uid):
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            print(f"[ERR] 找不到凭证文件：{expanded}", file=sys.stderr)
            return token, uid
        with open(expanded, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key == "THREADS_ACCESS_TOKEN" and not token:
                    token = val
                elif key == "THREADS_USER_ID" and not uid:
                    uid = val
    return token, uid


def read_text(path: str) -> str:
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read().strip()


def check(body: str, attachment: str | None) -> list[str]:
    """做长度与链接校验，返回问题列表（空=通过）。"""
    problems: list[str] = []
    for name, text, limit in (("正文", body, BODY_LIMIT), ("附件", attachment or "", ATTACH_LIMIT)):
        if not text:
            continue
        chars, nbytes = len(text), len(text.encode("utf-8"))
        print(f"  {name}：{chars} 字符 / {nbytes} 字节（上限 {limit}）")
        if chars > limit:
            problems.append(f"{name}超过 {limit} 字符（现在 {chars}）")
        elif nbytes > limit and limit == ATTACH_LIMIT:
            # 官方只对 emoji 明确按 UTF-8 字节计；中文口径未公开，把字节数一并提示
            print(f"  [注意] {name}字节数 {nbytes} 也接近上限，若服务端按字节计可能被拒")
    links = set(URL_RE.findall(body) + URL_RE.findall(attachment or ""))
    if len(links) > LINK_LIMIT:
        problems.append(f"链接 {len(links)} 个，超过上限 {LINK_LIMIT}")
    return problems


def post_form(url: str, data: dict[str, str]) -> dict:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误：{exc.reason}") from None


def get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None


def publish(user_id: str, token: str, body: str, attachment: str | None) -> None:
    data = {
        "media_type": "TEXT",
        "text": body,
        "access_token": token,
    }
    if attachment:
        data["text_attachment"] = json.dumps({"plaintext": attachment}, ensure_ascii=False)

    print("[1/2] 创建媒体容器…")
    created = post_form(f"{API}/{user_id}/threads", data)
    container = created.get("id")
    if not container:
        raise RuntimeError(f"创建容器未返回 id：{created}")
    print(f"      容器 id：{container}")

    # 官方建议发布前等约 30 秒，让平台完成处理
    wait = 30
    print(f"[2/2] 等待 {wait} 秒后发布…")
    time.sleep(wait)

    published = post_form(
        f"{API}/{user_id}/threads_publish",
        {"creation_id": container, "access_token": token},
    )
    media_id = published.get("id")
    if not media_id:
        raise RuntimeError(f"发布未返回 id：{published}")
    print(f"      已发布，media id：{media_id}")

    try:
        info = get_json(f"{API}/{media_id}?fields=permalink,text&access_token={token}")
        if info.get("permalink"):
            print(f"      链接：{info['permalink']}")
    except RuntimeError as exc:
        print(f"      [警告] 取 permalink 失败：{exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="把正文（+ 文字附件）发到 Threads")
    ap.add_argument("--body", required=True, help="正文文件路径（≤500 字符）")
    ap.add_argument("--attachment", help="附件文件路径（≤10000 字符）")
    ap.add_argument("--env-file", help="凭证文件（THREADS_ACCESS_TOKEN / THREADS_USER_ID）")
    ap.add_argument("--publish", action="store_true", help="真发；不加则只做 dry-run 校验")
    args = ap.parse_args()

    body = read_text(args.body)
    attachment = read_text(args.attachment) if args.attachment else None

    print("== 校验 ==")
    problems = check(body, attachment)
    print(f"  正文首行：{body.splitlines()[0] if body else '(空)'}")
    if attachment:
        print(f"  附件首行：{attachment.splitlines()[0]}")
    if problems:
        print("\n[FAIL] 校验未通过：")
        for item in problems:
            print(f"  - {item}")
        return 1

    if not args.publish:
        print("\n[DRY-RUN] 校验通过，未发布。加 --publish 才真正发出去。")
        return 0

    token, user_id = load_env(args.env_file)
    if not token or not user_id:
        print(
            "[ERR] 缺少凭证：需要 THREADS_ACCESS_TOKEN 与 THREADS_USER_ID\n"
            "      未配置时先把 token 写进 ~/.dev/private/social-agent/threads.env，"
            "再用 --env-file 指过去。",
            file=sys.stderr,
        )
        return 2

    try:
        publish(user_id, token, body, attachment)
    except RuntimeError as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
