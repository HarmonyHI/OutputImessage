#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iMessage / 信息 全量导出工具
读取 ~/Library/Messages/chat.db，导出为 CSV + JSON + 可搜索 HTML。

用法:
    python3 export_imessage.py                 # 导出到桌面的 iMessage导出_日期 文件夹
    python3 export_imessage.py -o ~/somewhere  # 指定输出目录
    python3 export_imessage.py --no-copy-attachments  # 不复制附件（默认会复制）
    python3 export_imessage.py --db /path/to/chat.db  # 指定数据库（如 Time Machine 备份）

需要「完全磁盘访问权限」。数据库只会被只读复制到临时目录后读取，原库不会被修改。
"""

import argparse
import csv
import filecmp
import json
import lzma
import os
import struct
import re
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 00:00:00 UTC vs 1970-01-01
DEFAULT_DB = os.path.expanduser("~/Library/Messages/chat.db")


# --------------------------------------------------------------------------
# 文本解码：新版 macOS 只把正文存在 attributedBody（typedstream 归档）里
# --------------------------------------------------------------------------
_MARKERS = [
    b"NSString\x01\x94\x84\x01\x2b",
    b"NSString\x01\x95\x84\x01\x2b",
    b"NSString\x01\x96\x84\x01\x2b",
    b"\x01\x94\x84\x01\x2b",
    b"\x01\x95\x84\x01\x2b",
    b"\x01\x2b",
]


def _read_length(buf, idx):
    """typedstream 的长度前缀：<0x80 单字节；0x81/0x82/0x83 后接 2/4/8 字节小端"""
    if idx >= len(buf):
        return None, idx
    c = buf[idx]
    if c < 0x80:
        return c, idx + 1
    sizes = {0x81: 2, 0x82: 4, 0x83: 8}
    n = sizes.get(c)
    if n is None:
        return None, idx
    return int.from_bytes(buf[idx + 1: idx + 1 + n], "little"), idx + 1 + n


_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_JUNK_KEYS = ("__kIM", "AttributeName", "streamtyped", "NSAttributedString",
              "NSMutableString", "NSDictionary", "NSObject", "NSValue", "bplist")


def sanitize_text(s):
    """清掉占位符与控制字符（\ufffc 是附件/表情的占位符）"""
    if not s:
        return ""
    s = s.replace("\ufffc", "").replace("\ufffd", "")
    s = _CTRL_RE.sub("", s)
    return s.strip()


def _looks_like_metadata(s):
    return any(k in s for k in _JUNK_KEYS)


def _longest_printable(buf):
    """兜底：从二进制里捞出最长的一段可读文本；捞到的是属性元数据就放弃"""
    try:
        txt = buf.decode("utf-8", "ignore")
    except Exception:
        return None
    if _looks_like_metadata(txt):
        return None
    runs = re.findall(r"[^\x00-\x08\x0b-\x1f\x7f]{2,}", txt)
    if not runs:
        return None
    best = max(runs, key=len)
    # 可读性门槛：字母/数字/中文占比要够高，否则就是二进制残渣
    good = sum(1 for ch in best if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
               or ch in " \n\t，。！？、；：\"'（）《》【】·…—-—,.!?;:@#%&*+=/\\()[]<>~`|")
    if not best or good / len(best) < 0.75:
        return None
    return best.strip() or None


def decode_attributed_body(blob):
    if not blob:
        return None
    if isinstance(blob, memoryview):
        blob = bytes(blob)
    if isinstance(blob, str):
        return blob or None
    for marker in _MARKERS:
        pos = blob.find(marker)
        if pos == -1:
            continue
        start = pos + len(marker)
        length, start2 = _read_length(blob, start)
        if length is None:
            continue
        raw = blob[start2: start2 + length]
        if not raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
        text = sanitize_text(text)
        if text:
            return text
    return _longest_printable(blob)


def balloon_label(balloon):
    """把气泡插件包名翻译成人话"""
    b = as_str(balloon).lower()
    if not b or b == "null":
        return ""
    if "handwriting" in b:
        return "[手写消息]"
    if "digitaltouch" in b:
        return "[数码点触]"
    if "stickers" in b:
        return "[贴纸]"
    if "peerpayment" in b or "applepay" in b or "apple cash" in b:
        return "[Apple Pay]"
    if "music" in b or "shoebox" in b:
        return "[Apple Music]"
    if "msmessageextensionballoonplugin" in b:
        tail = b.rsplit(".", 1)[-1]
        return f"[App 消息: {tail}]"
    return f"[App 消息: {b.rsplit('.', 1)[-1]}]"


_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tiff", ".tif"}
_VID_EXT = {".mov", ".mp4", ".m4v", ".avi", ".3gp"}
_AUD_EXT = {".caf", ".m4a", ".mp3", ".amr", ".wav", ".aac", ".opus"}


def media_kind(name, mime):
    m = (mime or "").lower()
    ext = os.path.splitext(name or "")[1].lower()
    if m.startswith("image/") or ext in _IMG_EXT:
        return "image"
    if m.startswith("video/") or ext in _VID_EXT:
        return "video"
    if m.startswith("audio/") or ext in _AUD_EXT:
        return "audio"
    return "file"


def safe_name(s, limit=60):
    return re.sub(r"[^\w\-.@+]", "_", s or "")[:limit]


def classify_blob(blob):
    """按文件头判断插件载荷的真实类型"""
    if not blob:
        return ("bin", "bin")
    if blob[:8] == b"bplist00":
        return ("plist", "plist")
    if blob[:2] == b"\x1f\x8b":
        return ("gzip", "gz")
    if blob[:4] == b"%PDF":
        return ("pdf", "pdf")
    if blob[:4] == b"PK\x03\x04":
        return ("zip", "zip")
    return ("bin", "bin")


# --------------------------------------------------------------------------
# 时间转换
# --------------------------------------------------------------------------
def apple_ts(value, tz):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # 新版系统存纳秒(~1e17)，少数存毫秒(~1e11)，老版存秒(~1e8)
    if abs(v) > 1e15:
        secs = v / 1_000_000_000
    elif abs(v) > 1e10:
        secs = v / 1_000
    else:
        secs = v
    try:
        return datetime.fromtimestamp(APPLE_EPOCH_OFFSET + secs, tz)
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# 数据库读取
# --------------------------------------------------------------------------
def snapshot_db(src, workdir):
    """把数据库连同 WAL 复制到临时目录，避免锁库并保证一致性"""
    dst = os.path.join(workdir, "chat.db")
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        p = src + suffix
        if os.path.exists(p):
            try:
                shutil.copy2(p, dst + suffix)
            except OSError:
                pass
    return dst


def as_str(v):
    """sqlite 可能返回 bytes / None，统一成 str"""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


# --------------------------------------------------------------------------
# 联系人姓名（尽力而为，读不到就只显示号码）
# --------------------------------------------------------------------------
def _norm_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if not d:
        return None
    if d.startswith("0086"):
        d = d[4:]
    elif d.startswith("86") and len(d) > 11:
        d = d[2:]
    return d


def load_contacts():
    """返回 lookup(handle) -> 姓名 或 None"""
    base = os.path.expanduser("~/Library/Application Support/AddressBook")
    index = {}
    dbs = []
    try:
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fn.endswith(".abcddb"):
                    dbs.append(os.path.join(root, fn))
    except OSError:
        return lambda h: None
    for path in dbs:
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            recs = {}
            for row in c.execute(
                    "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION FROM ZABCDRECORD"):
                pk, first, last, nick, org = (as_str(x).strip() for x in row)
                name = " ".join(x for x in (first, last) if x) or nick or org
                if name:
                    recs[pk] = name
            for row in c.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                pk, num = row
                name = recs.get(pk)
                d = _norm_phone(as_str(num))
                if name and d:
                    index.setdefault("p:" + d, name)
                    if len(d) >= 7:
                        index.setdefault("s:" + d[-7:], name)
            for row in c.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                pk, addr = row
                name = recs.get(pk)
                a = as_str(addr).strip().lower()
                if name and a:
                    index.setdefault("e:" + a, name)
            c.close()
        except sqlite3.Error:
            continue

    def lookup(handle):
        h = as_str(handle).strip()
        if not h:
            return None
        if "@" in h:
            return index.get("e:" + h.lower())
        d = _norm_phone(h)
        if d:
            return index.get("p:" + d) or index.get("s:" + d[-7:])
        return None

    return lookup


def table_columns(conn, table):
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    except sqlite3.Error:
        return set()


def pick(cols, *names, default="NULL"):
    for n in names:
        if n in cols:
            return n
    return None


def load_chats(conn, lookup):
    mcols = table_columns(conn, "chat")
    if not mcols:
        raise RuntimeError("找不到 chat 表，可能不是有效的 iMessage 数据库")

    c_id = "ROWID"
    c_ident = pick(mcols, "chat_identifier")
    c_name = pick(mcols, "display_name")
    c_style = pick(mcols, "style")
    c_service = pick(mcols, "service_name")

    sql = f"""
        SELECT {c_id},
               COALESCE({c_ident or "''"}, '')  AS identifier,
               COALESCE({c_name or "''"}, '')   AS display_name,
               COALESCE({c_style or "0"}, 0)    AS style,
               COALESCE({c_service or "''"}, '') AS service
        FROM chat
    """
    chats = {}
    for cid, ident, name, style, service in conn.execute(sql):
        chats[cid] = {
            "chat_id": cid,
            "identifier": as_str(ident),
            "display_name": as_str(name),
            "is_group": bool(style) and int(style) == 43,
            "service": as_str(service),
            "participants": [],
        }

    # 群成员
    if table_columns(conn, "chat_handle_join") and table_columns(conn, "handle"):
        hcols = table_columns(conn, "handle")
        h_id = pick(hcols, "id")
        if h_id:
            try:
                q = f"""
                    SELECT chj.chat_id, h.{h_id}
                    FROM chat_handle_join chj
                    JOIN handle h ON h.ROWID = chj.handle_id
                """
                for cid, handle in conn.execute(q):
                    if cid in chats and handle:
                        chats[cid]["participants"].append(as_str(handle))
            except sqlite3.Error:
                pass

    # 计算可读名称：备注名 > 联系人姓名 > 群成员 > 号码
    for chat in chats.values():
        ident = chat["identifier"]
        if chat["display_name"]:
            chat["title"] = chat["display_name"]
            continue
        if chat["is_group"]:
            names = [lookup(p) or p for p in chat["participants"]]
            names = [n for n in names if n]
            if names:
                head = "、".join(names[:3])
                chat["title"] = f"群聊 · {head}" + ("…" if len(names) > 3 else "")
            else:
                chat["title"] = "群聊"
        else:
            chat["title"] = lookup(ident) or ident or f"对话 {chat['chat_id']}"
    return chats


def build_chat_index(conn, chats):
    """把 chat 的 guid / chat_identifier 映射成 ROWID，用于从 ck_chat_id 反查归属"""
    index = {}
    guids = {}
    if table_columns(conn, "chat") and "guid" in table_columns(conn, "chat"):
        try:
            for cid, g in conn.execute("SELECT ROWID, guid FROM chat"):
                guids[cid] = as_str(g)
        except sqlite3.Error:
            pass
    for cid, info in chats.items():
        for k in (info.get("identifier") or "", guids.get(cid, "")):
            if not k:
                continue
            index.setdefault(k, cid)
            if ";-;" in k:
                index.setdefault(k.split(";-;", 1)[1], cid)
    return index


def resolve_chat_id(join_chat_id, ck_chat, index):
    """先信 chat_message_join；它为空时用 message.ck_chat_id 反查；都不行归到 0"""
    if join_chat_id:
        return join_chat_id
    ck = as_str(ck_chat)
    if not ck:
        return 0
    if ck in index:
        return index[ck]
    tail = ck.split(";-;", 1)[1] if ";-;" in ck else ck
    if tail in index:
        return index[tail]
    digits = "".join(ch for ch in tail if ch.isdigit())
    if len(digits) >= 7:
        for key, cid in index.items():
            kd = "".join(ch for ch in key if ch.isdigit())
            if kd and (kd.endswith(digits[-9:]) or digits.endswith(kd[-9:])):
                return cid
    return 0


def load_messages(conn, chats, tz, lookup):
    mcols = table_columns(conn, "message")
    if not mcols:
        raise RuntimeError("找不到 message 表")

    def col(*names, expr="NULL"):
        n = pick(mcols, *names)
        return n if n else expr

    sel = f"""
        SELECT
            m.ROWID                                   AS rowid,
            {col('guid', expr="''")}                  AS guid,
            {col('text')}                             AS text,
            {col('attributedBody')}                   AS attributed_body,
            {col('handle_id', expr='0')}              AS handle_id,
            {col('date')}                             AS date_raw,
            {col('date_read')}                        AS date_read_raw,
            {col('is_from_me', expr='0')}             AS is_from_me,
            {col('service', expr="''")}               AS service,
            {col('associated_message_type', expr='0')} AS assoc_type,
            {col('associated_message_guid')}          AS assoc_guid,
            {col('item_type', expr='0')}              AS item_type,
            {col('balloon_bundle_id')}                AS balloon,
            {col('is_audio_message', expr='0')}       AS is_audio,
            {col('payload_data')}                     AS payload_blob,
            (SELECT MIN(chat_id) FROM chat_message_join j
              WHERE j.message_id = m.ROWID)           AS join_chat_id,
            {col('ck_chat_id', expr='NULL')}          AS ck_chat
        FROM message m
        ORDER BY m.ROWID ASC
    """

    # chat_message_join 在新版系统里会被「消息归并」清空，导致整库消息凭空消失。
    # 真正的归属记在 message.ck_chat_id（形如 iMessage;-;号码），这里建立反查表。
    chat_index = build_chat_index(conn, chats)

    handles = {}
    if table_columns(conn, "handle"):
        hcols = table_columns(conn, "handle")
        if "id" in hcols:
            try:
                for rid, hid in conn.execute("SELECT ROWID, id FROM handle"):
                    handles[rid] = as_str(hid)
            except sqlite3.Error:
                pass

    # 附件
    attachments = {}
    if table_columns(conn, "message_attachment_join") and table_columns(conn, "attachment"):
        acols = table_columns(conn, "attachment")
        a_name = pick(acols, "transfer_name", "filename")
        try:
            q = f"""
                SELECT maj.message_id,
                       a.ROWID,
                       COALESCE({a_name or "''"}, '') AS name,
                       COALESCE({pick(acols, 'mime_type') or "''"}, '') AS mime,
                       COALESCE({pick(acols, 'filename') or "''"}, '') AS path,
                       COALESCE({pick(acols, 'total_bytes') or '0'}, 0) AS size
                FROM message_attachment_join maj
                JOIN attachment a ON a.ROWID = maj.attachment_id
            """
            for mid, aid, name, mime, path, size in conn.execute(q):
                path = as_str(path)
                disp_name = as_str(name) or os.path.basename(path)
                attachments.setdefault(mid, []).append({
                    "attachment_id": aid,
                    "name": disp_name,
                    "mime": as_str(mime),
                    "path": path,
                    "bytes": size or 0,
                    "kind": media_kind(disp_name, as_str(mime)),
                    "url": "",
                })
        except sqlite3.Error:
            pass

    rows = []
    for r in conn.execute(sel):
        (rowid, guid, text, abody, handle_id, date_raw, date_read_raw,
         is_from_me, service, assoc_type, assoc_guid, item_type, balloon,
         is_audio, payload_blob, join_chat_id, ck_chat) = r

        chat_id = resolve_chat_id(join_chat_id, ck_chat, chat_index)

        att_list = attachments.get(rowid, [])
        body = sanitize_text(as_str(text))
        if not body:
            body = decode_attributed_body(abody) or ""

        dt = apple_ts(date_raw, tz)
        dt_read = apple_ts(date_read_raw, tz)

        assoc = int(assoc_type or 0)
        kind = "message"
        balloon_s = as_str(balloon).lower()
        if assoc in (2000, 2001, 2002, 2003, 2004, 2005):
            kind = "reaction"
            if not body:
                body = "[表情回应]"
        elif assoc in (1000, 1001, 1002, 1003, 1004, 1005):
            kind = "unsend_or_edit"
            if not body:
                body = "[消息已撤回]" if assoc == 1000 else "[消息已编辑]"
        elif "stickers" in balloon_s or (not body and any(
                "sticker" in (a["name"] or "").lower() or "sticker" in (a["mime"] or "").lower()
                for a in att_list)):
            kind = "sticker"
        elif int(is_audio or 0):
            kind = "audio"
        elif not body and att_list:
            kind = "attachment"
        elif not body and balloon_s:
            kind = "app_message"
        elif not body:
            kind = "empty"
        # 纯贴纸/附件/手写等没有正文的消息，补一个可读标记
        if not body:
            body = {
                "sticker": "[贴纸]", "attachment": "[附件]", "audio": "[语音]",
                "reaction": "[表情回应]", "app_message": balloon_label(balloon_s),
            }.get(kind, "")

        chat = chats.get(chat_id, {})
        if not chat and chat_id == 0:
            chat = {"title": "未归类消息", "identifier": "", "is_group": False,
                    "participants": []}
        raw_sender = handles.get(handle_id) or ("" if is_from_me else "unknown")
        sender_name = "我" if is_from_me else (lookup(raw_sender) or raw_sender)

        rows.append({
            "chat_id": chat_id,
            "chat_name": chat.get("title") or chat.get("identifier") or f"chat_{chat_id}",
            "chat_identifier": chat.get("identifier", ""),
            "is_group": chat.get("is_group", False),
            "participants": ";".join(chat.get("participants", [])),
            "rowid": rowid,
            "guid": as_str(guid),
            "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
            "timestamp_read": dt_read.strftime("%Y-%m-%d %H:%M:%S") if dt_read else "",
            "epoch": int(dt.timestamp()) if dt else "",
            "from_me": bool(is_from_me),
            "sender": sender_name,
            "sender_handle": raw_sender,
            "service": as_str(service),
            "kind": kind,
            "app": balloon_label(balloon_s),
            "text": body,
            "attachments": att_list,
            "payload": None,
            "note": "",
            "_payload": bytes(payload_blob) if payload_blob else None,
            "sort_key": dt.timestamp() if dt else 0,
        })

    rows.sort(key=lambda x: (x["sort_key"], x["rowid"]))
    return rows


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def write_csv(rows, path):
    fields = ["timestamp", "chat_name", "chat_identifier", "is_group", "participants",
              "from_me", "sender", "sender_handle", "service", "kind", "app", "text",
              "attachment_count", "attachments", "guid", "rowid"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({
                **r,
                "is_group": 1 if r["is_group"] else 0,
                "from_me": 1 if r["from_me"] else 0,
                "attachment_count": len(r["attachments"]),
                "attachments": " | ".join(a["name"] for a in r["attachments"]),
            })


def write_json(rows, path):
    clean = []
    for r in rows:
        d = dict(r)
        d.pop("sort_key", None)
        d.pop("_payload", None)
        # 内联的 base64 只服务 HTML，JSON 里换成瘦身版
        d["attachments"] = [
            {k: v for k, v in a.items() if k != "data"} for a in r["attachments"]
        ]
        clean.append(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=1)


HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iMessage 导出 · __COUNT__ 条</title>
<style>
  :root{--bg:#f6f7f9;--panel:#fff;--line:#e6e8ec;--tx:#1b1e23;--muted:#71767d;
        --me:#0b84ff;--them:#eef0f3;--accent:#e8543f;}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.55 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;
       background:var(--bg);color:var(--tx);height:100vh;display:flex;overflow:hidden}
  #side{width:290px;flex:0 0 290px;background:var(--panel);border-right:1px solid var(--line);
        display:flex;flex-direction:column}
  #side h1{font-size:14px;margin:0;padding:16px 16px 10px;letter-spacing:.02em}
  #side .sub{font-size:11.5px;color:var(--muted);padding:0 16px 12px}
  #q{margin:0 12px 10px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
     font-size:13px;outline:none;background:#fbfbfc}
  #q:focus{border-color:#c6d4e6;background:#fff}
  #list{overflow-y:auto;flex:1;padding-bottom:16px}
  .item{padding:9px 16px;cursor:pointer;border-left:3px solid transparent}
  .itop{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
  .iprev{font-size:11.5px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
         white-space:nowrap;margin-top:2px}
  .bub.hw svg{width:100%;height:auto;display:block}
  .hint{margin:14px auto 4px;max-width:620px;background:#fff8e6;border:1px solid #f0dfae;
        color:#7a5c14;font-size:12.5px;padding:10px 13px;border-radius:9px;line-height:1.65}
  .item:hover{background:#f2f4f7}
  .item.on{background:#eef4ff;border-left-color:var(--me)}
  .item .nm{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .item .ct{font-size:11px;color:var(--muted);flex:0 0 auto}
  #main{flex:1;display:flex;flex-direction:column;min-width:0}
  #head{padding:13px 22px;border-bottom:1px solid var(--line);background:var(--panel);}
  #head .t{font-weight:600;font-size:15px}
  #head .m{font-size:11.5px;color:var(--muted);margin-top:2px}
  #msgs{overflow-y:auto;padding:20px 22px 60px;display:flex;flex-direction:column;gap:6px}
  .row{display:flex;flex-direction:column;max-width:74%}
  .row.me{align-self:flex-end;align-items:flex-end}
  .row.them{align-self:flex-start;align-items:flex-start}
  .meta{font-size:10.5px;color:var(--muted);margin:0 6px 3px}
  .bub{padding:8px 12px;border-radius:15px;white-space:pre-wrap;word-break:break-word;
       background:var(--them);border:1px solid rgba(0,0,0,.04)}
  .row.me .bub{background:var(--me);color:#fff;border-color:transparent}
  /* 手写/笔迹气泡：必须保持透明背景，特异性需压过 .row.me .bub */
  .row .bub.hw{background:transparent;border:0;padding:0;width:300px;max-width:100%}
  .row.me .bub a{color:#dceaff}
  .bub a{color:var(--me)}
  .att{font-size:11.5px;color:var(--muted);margin:4px 6px 0}
  .att div{background:#f0f2f5;border-radius:6px;padding:3px 7px;display:inline-block;margin:2px 3px 0 0}
  .row.me .att div{background:#e6edf6}
  .media{margin-top:5px;display:flex;flex-direction:column;gap:5px;align-items:flex-start}
  .row.me .media{align-items:flex-end}
  .media a{display:block;line-height:0;border-radius:12px;overflow:hidden;border:1px solid rgba(0,0,0,.06)}
  .media img{display:block;max-width:250px;max-height:250px;width:auto;height:auto;background:#fff}
  .media img.sticker{max-width:140px;max-height:140px;background:transparent;border:0}
  .media video{display:block;max-width:280px;border-radius:12px;background:#000}
  .media audio{width:250px;height:34px}
  .filechips{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
  .filechips a{font-size:11.5px;background:#f0f2f5;border-radius:6px;padding:4px 8px;
     color:#3a3f47;text-decoration:none;border:1px solid rgba(0,0,0,.04)}
  .filechips a:hover{background:#e4e8ee}
  .row.me .filechips a{background:#e6edf6}
  .note{font-size:11px;color:var(--muted);margin:4px 6px 0;line-height:1.45}
  .day{align-self:center;font-size:11px;color:var(--muted);padding:14px 0 6px}
  .rx{font-size:11.5px;color:var(--accent);margin:2px 6px 0}
  #empty{margin:auto;color:var(--muted);font-size:13px}
  mark{background:#fff2a8;color:inherit;padding:0 1px;border-radius:2px}
</style>
</head>
<body>
<div id="side">
  <h1>信息导出</h1>
  <div class="sub">__COUNT__ 条消息 · __CHATS__ 个对话</div>
  <input id="q" placeholder="搜索对话或内容…">
  <div id="list"></div>
</div>
<div id="main">
  <div id="head"><div class="t">选择一个对话</div><div class="m">从左侧列表点击查看</div></div>
  <div id="msgs"><div id="empty">← 左侧共有 __CHATS__ 个对话</div></div>
</div>
<script>
const DATA = __DATA__;
const byChat = new Map();
for (const m of DATA) {
  if (!byChat.has(m.chat_id)) byChat.set(m.chat_id, {name: m.chat_name, ident: m.chat_identifier,
       group: m.is_group, parts: m.participants, msgs: []});
  byChat.get(m.chat_id).msgs.push(m);
}
const chats = [...byChat.entries()].map(([id, c]) => ({id, ...c}))
  .sort((a,b) => (b.msgs[b.msgs.length-1]?.sort_key||0) - (a.msgs[a.msgs.length-1]?.sort_key||0));

const listEl = document.getElementById('list');
const msgsEl = document.getElementById('msgs');
const headEl = document.getElementById('head');
let current = null;

function esc(s){return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function linkify(s){
  return esc(s).replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
}
function fmt(ts){ if(!ts) return ''; return ts.slice(5,16); }
function fmtSize(n){
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
function fmtFull(ts){ return ts || ''; }

function renderList(filter){
  const f = (filter||'').toLowerCase();
  listEl.innerHTML = '';
  chats.forEach(c => {
    if (f) {
      const hit = c.name.toLowerCase().includes(f) || c.ident.toLowerCase().includes(f)
        || c.msgs.some(m => (m.text||'').toLowerCase().includes(f));
      if (!hit) return;
    }
    const d = document.createElement('div');
    d.className = 'item' + (current === c.id ? ' on' : '');
    const last = c.msgs[c.msgs.length - 1] || {};
    const prev = (last.text || '').replace(/[ ]+/g, ' ').trim().slice(0, 34);
    d.innerHTML = '<div class="itop"><span class="nm">' + esc(c.name) + '</span>'
      + '<span class="ct">' + c.msgs.length + '</span></div>'
      + '<div class="iprev">' + esc(prev || '（无文字内容）') + '</div>';
    d.onclick = () => open(c.id);
    listEl.appendChild(d);
  });
}

function open(id){
  current = id;
  const c = byChat.get(id);
  renderList(document.getElementById('q').value);
  const firstTs = (c.msgs[0] || {}).timestamp || '';
  const lastTs = (c.msgs[c.msgs.length - 1] || {}).timestamp || '';
  const span = firstTs ? (' · ' + firstTs.slice(0, 10) + (firstTs.slice(0,10) !== lastTs.slice(0,10) ? ' ~ ' + lastTs.slice(0, 10) : '')) : '';
  headEl.innerHTML = '<div class="t">' + esc(c.name) + '</div><div class="m">'
     + c.msgs.length + ' 条' + span + ' · ' + esc(c.ident)
     + (c.parts && c.parts !== c.ident ? ' · ' + esc(c.parts) : '') + '</div>';
  msgsEl.innerHTML = '';

  // 记录很少时先把话说清楚，避免让人以为「导漏了」
  if (c.msgs.length <= 2) {
    const hd = document.createElement('div');
    hd.className = 'hint';
    hd.textContent = '本机 Messages 数据库里这个对话只有 ' + c.msgs.length
      + ' 条记录（不是导出遗漏）。其余聊天可能只存在于 iPhone / iPad 上，'
      + '或没有通过 iCloud 云端信息同步到这台 Mac。';
    msgsEl.appendChild(hd);
  }

  let lastDay = '';
  c.msgs.forEach(m => {
    const day = (m.timestamp||'').slice(0,10);
    if (day && day !== lastDay) {
      lastDay = day;
      const d = document.createElement('div'); d.className='day'; d.textContent = day;
      msgsEl.appendChild(d);
    }
    const row = document.createElement('div');
    row.className = 'row ' + (m.from_me ? 'me' : 'them');
    const who = m.from_me ? '我' : (m.sender || '对方');
    let h = '<div class="meta">' + esc(fmt(m.timestamp)) + ' · ' + esc(who) + '</div>';

    // 分拣：能直接显示的多媒体 vs 只能给链接的文件
    const media = [];
    (m.attachments || []).forEach(a => {
      const url = a.url || "";
      if (!url) return;
      const sticker = (a.name || "").indexOf("sticker") >= 0;
      if (a.kind === "image") {
        media.push('<a href="' + esc(url) + '" target="_blank"><img loading="lazy" alt=""'
          + (sticker ? ' class="sticker"' : "") + ' src="' + esc(a.data || url) + '"></a>');
      } else if (a.kind === "video") {
        media.push('<video controls preload="metadata" src="' + esc(url) + '"></video>');
      } else if (a.kind === "audio") {
        media.push('<audio controls preload="none" src="' + esc(url) + '"></audio>');
      }
    });

    const placeholder = ["[贴纸]", "[附件]", "[语音]", "[手写消息]", "[数码点触]"].indexOf(m.text) >= 0;
    if (m.svg) {
      h += '<div class="bub hw">' + m.svg + '</div>';
    } else if (m.kind === "reaction") {
      h += '<div class="rx">' + esc(m.text) + '</div>';
    } else if (m.text && !(placeholder && media.length)) {
      h += '<div class="bub">' + linkify(m.text) + '</div>';
    }
    if (media.length) h += '<div class="media">' + media.join("") + '</div>';

    const chipHtml = [];
    (m.attachments || []).forEach(a => {
      if (!a.url) return;
      if (["image", "video", "audio"].indexOf(a.kind) >= 0) return;
      chipHtml.push('<a href="' + esc(a.url) + '" target="_blank">附件 · ' + esc(a.name || a.mime || "未命名")
        + (a.size ? " · " + fmtSize(a.size) : "") + '</a>');
    });
    if (m.payload && m.payload.url) {
      chipHtml.push('<a href="' + esc(m.payload.url) + '" target="_blank">原始数据 · '
        + esc(m.payload.label || "插件载荷") + '</a>');
    }
    if (chipHtml.length) h += '<div class="filechips">' + chipHtml.join("") + '</div>';
    if (m.note) h += '<div class="note">' + esc(m.note) + '</div>';

    row.innerHTML = h;
    msgsEl.appendChild(row);
  });
  msgsEl.scrollTop = 0;
}

document.getElementById('q').addEventListener('input', e => renderList(e.target.value));
renderList('');
// 默认打开消息最多的对话，方便先看内容最丰富的那个
if (chats.length) {
  const biggest = chats.slice().sort((a, b) => b.msgs.length - a.msgs.length)[0];
  open(biggest.id);
}
</script>
</body>
</html>
"""


def write_html(rows, path):
    slim = []
    for r in rows:
        slim.append({
            "chat_id": r["chat_id"], "chat_name": r["chat_name"],
            "chat_identifier": r["chat_identifier"], "is_group": r["is_group"],
            "participants": r["participants"], "timestamp": r["timestamp"],
            "from_me": r["from_me"], "sender": r["sender"], "kind": r["kind"],
            "text": r["text"], "attachments": r["attachments"],
            "payload": r.get("payload"), "note": r.get("note", ""),
            "svg": r.get("svg"),
            "sort_key": r["sort_key"],
        })
    payload = json.dumps(slim, ensure_ascii=False).replace("</", "<\\/")
    html = (HTML_TPL
            .replace("__DATA__", payload)
            .replace("__COUNT__", f"{len(rows):,}")
            .replace("__CHATS__", str(len({r['chat_id'] for r in rows}))))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


_MAGIC = [
    (b"\xff\xd8\xff", "image", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image", ".png"),
    (b"GIF87a", "image", ".gif"),
    (b"GIF89a", "image", ".gif"),
    (b"BM", "image", ".bmp"),
]


def sniff_kind(path):
    """很多插件附件没有 mime 和正确扩展名，按文件头猜类型"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None, None
    for magic, kind, ext in _MAGIC:
        if head.startswith(magic):
            return kind, ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
            return "image", ".heic"
        if brand == b"qt  ":
            return "video", ".mov"
        return "video", ".mp4"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "video", ".webm"
    if head[:4] == b"OggS":
        return "audio", ".ogg"
    if head[:4] == b"caff":
        return "audio", ".caf"
    if head[:3] == b"ID3" or head[:2] == b"\xff\xfb":
        return "audio", ".mp3"
    return None, None


def _same_file(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def copy_attachments(rows, outdir):
    """把附件复制到 attachments/<对话名>/，并把可访问的相对路径写回 a['url']"""
    dest_root = os.path.join(outdir, "attachments")
    os.makedirs(dest_root, exist_ok=True)
    copied = failed = 0
    for r in rows:
        for a in r["attachments"]:
            src = a.get("path") or ""
            if not src:
                continue
            src = os.path.expanduser(src)
            if not os.path.isabs(src) or not os.path.exists(src):
                failed += 1
                continue
            if a.get("kind") == "file" and not (a.get("mime") or "").strip():
                k, ext = sniff_kind(src)
                if k:
                    a["kind"] = k
                    stem, old_ext = os.path.splitext(a["name"])
                    if old_ext.lower() != ext:
                        a["name"] = stem + ext
            d = os.path.join(dest_root, safe_name(r["chat_name"]))
            os.makedirs(d, exist_ok=True)
            name = a["name"] or os.path.basename(src)
            target = os.path.join(d, name)
            # 重跑时若已存在完全相同的文件，直接复用，避免产生 _1 _2 副本
            if os.path.exists(target) and _same_file(src, target):
                a["rel"] = f"attachments/{safe_name(r['chat_name'])}/{name}"
                a["url"] = quote(a["rel"])
                copied += 1
                continue
            base, ext = os.path.splitext(target)
            i = 1
            while os.path.exists(target):
                target = f"{base}_{i}{ext}"
                i += 1
            try:
                shutil.copy2(src, target)
                a["rel"] = f"attachments/{safe_name(r['chat_name'])}/{os.path.basename(target)}"
                a["url"] = quote(a["rel"])
                copied += 1
            except OSError:
                failed += 1
    return copied, failed


def inline_images(rows, outdir, cap=900_000):
    """把体积不大的图片内联成 data URI，index.html 单独发出去也能看到图"""
    import base64
    total = 0
    for r in rows:
        for a in r["attachments"]:
            if a.get("kind") != "image" or not a.get("rel"):
                continue
            path = os.path.join(outdir, a["rel"])
            if not os.path.exists(path):
                continue
            size = os.path.getsize(path)
            if size > cap:
                continue
            mime = a.get("mime") or ("image/png" if path.lower().endswith(".png") else "image/jpeg")
            try:
                with open(path, "rb") as f:
                    a["data"] = "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode())
                total += size
            except OSError:
                continue
    return total


def link_originals(rows):
    """没复制附件时，退化为指向原始文件的 file:// 链接"""
    for r in rows:
        for a in r["attachments"]:
            if a.get("url"):
                continue
            src = a.get("path") or ""
            if src and os.path.isabs(os.path.expanduser(src)):
                a["url"] = "file://" + quote(os.path.expanduser(src))
            else:
                a["url"] = ""


# --------------------------------------------------------------------------
# 手写消息 / 数码点触 还原（格式依据社区逆向：ReagentX/imessage-exporter、trymoose/handwriting2svg）
# --------------------------------------------------------------------------
def _s16(v):
    """这些格式把 int16 中心化存储：真实值 = 原值 XOR 0x8000"""
    v ^= 0x8000
    return v - 65536 if v >= 32768 else v


def _pb_fields(buf):
    """极简 protobuf 解析，返回 [(field_no, wire, value)]"""
    out, i = [], 0
    try:
        while i < len(buf):
            key, i = _varint(buf, i)
            f, w = key >> 3, key & 7
            if w == 0:
                v, i = _varint(buf, i)
                out.append((f, 0, v))
            elif w == 2:
                n, i = _varint(buf, i)
                out.append((f, 2, buf[i:i + n]))
                i += n
            elif w == 5:
                out.append((f, 5, buf[i:i + 4])); i += 4
            elif w == 1:
                out.append((f, 1, buf[i:i + 8])); i += 8
            else:
                break
    except (IndexError, struct.error):
        return out
    return out


def _varint(buf, i):
    r = s = 0
    while True:
        x = buf[i]; i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7


def decode_handwriting(blob):
    """com.apple.Handwriting.HandwritingProvider

    返回 (strokes, (frame_w, frame_h) 或 None)。
    strokes = [[(x, y, width), ...], ...] 每项一笔
    """
    if not blob:
        return None, None
    i = blob.find(b"\xfd7zXZ\x00")
    try:
        data = lzma.decompress(blob[i:]) if i >= 0 else bytes(blob)
    except Exception:
        return None, None
    strokes, pos = [], 0
    while pos + 2 <= len(data):
        cnt = struct.unpack_from("<H", data, pos)[0]; pos += 2
        if cnt == 0 or pos + cnt * 8 > len(data):
            return None, None
        pts = []
        for _ in range(cnt):
            x, y, w, _z = struct.unpack_from("<4H", data, pos); pos += 8
            # 坐标按 Apple 私有格式 XOR 0x8000 解码（见 imessage-exporter handwriting.proto），
            # 结果是无符号画布坐标，y 轴已是自上而下，不要翻转
            pts.append((x ^ 0x8000, y ^ 0x8000, w ^ 0x8000))
        strokes.append(pts)
    if not strokes or pos != len(data):
        return None, None
    return strokes, _handwriting_frame(blob)


def _handwriting_frame(blob):
    """取 Handwriting.Frame：Origin(x,y) + Size(w,h)，4×int16 LE，各自 XOR 0x8000

    点坐标的 x/y 单位并不相等，必须靠 frame 的宽高把两轴分别归一，否则纵向会被拉长。
    """
    try:
        top = _pb_fields(blob)
        body = next((v for f, w, v in top if f == 4 and w == 2), None)
        if not body:
            return None
        sub = _pb_fields(body)
        rect = next((v for f, w, v in sub if f == 3 and w == 2), None)
        if not rect or len(rect) < 8:
            return None
        ox, oy, rw, rh = struct.unpack_from("<4H", rect)
        rw ^= 0x8000
        rh ^= 0x8000
        if rw <= 0 or rh <= 0:
            return None
        return rw, rh
    except Exception:
        return None


def handwriting_svg(strokes, frame=None, color="#1b1e23"):
    """笔迹 → 内联 SVG。坐标原点左上、y 向下（Apple 原始即如此，勿翻转）

    两个关键点：
    1. x/y 两轴单位不等，须按 Frame 宽高分别归一，否则纵向会被拉长；
    2. 每点第三个字段是该点的笔宽，必须逐点体现，否则一笔一个粗细、笔压全丢。
    """
    xs = [p[0] for s in strokes for p in s]
    ys = [p[1] for s in strokes for p in s]
    max_x = max(xs) or 1
    max_y = max(ys) or 1
    fw, fh = frame if frame else (max_x, max_y)
    kx, ky = fw / max_x, fh / max_y

    widths = [p[2] for s in strokes for p in s]
    min_w, max_w = min(widths), max(widths)
    span_w = max(max_w - min_w, 1)
    # 1 个“显示像素”对应的 SVG 用户单位（按 340px 显示宽度折算）
    unit = max(fw, fh) / 340.0

    def stroke_w(pw):
        """笔宽 → SVG 线宽：映射到约 1.8~8 个显示像素"""
        t = (pw - min_w) / span_w
        return (2.0 + 6.0 * t) * unit

    parts = []
    for s in strokes:
        if not s:
            continue
        if len(s) == 1:
            p = s[0]
            parts.append(f'<circle cx="{p[0] * kx:.1f}" cy="{p[1] * ky:.1f}" '
                         f'r="{stroke_w(p[2]) / 2:.1f}"/>')
            continue
        # 把同宽（量化到 8 档）的连续点合并成一段，段间重叠一个点避免断口
        seg, lv = [s[0]], round(8 * (s[0][2] - min_w) / span_w)
        for p in s[1:]:
            nl = round(8 * (p[2] - min_w) / span_w)
            if nl != lv:
                seg.append(p)
                pts = " ".join(f"{q[0] * kx:.1f},{q[1] * ky:.1f}" for q in seg)
                parts.append(f'<polyline points="{pts}" '
                             f'stroke-width="{(2.0 + 6.0 * lv / 8) * unit:.1f}"/>')
                seg, lv = [p], nl
            else:
                seg.append(p)
        if len(seg) > 1:
            pts = " ".join(f"{q[0] * kx:.1f},{q[1] * ky:.1f}" for q in seg)
            parts.append(f'<polyline points="{pts}" '
                         f'stroke-width="{(2.0 + 6.0 * lv / 8) * unit:.1f}"/>')
        else:
            q = seg[0]
            parts.append(f'<circle cx="{q[0] * kx:.1f}" cy="{q[1] * ky:.1f}" '
                         f'r="{(2.0 + 6.0 * lv / 8) * unit / 2:.1f}"/>')
    return (f'<svg viewBox="0 0 {fw:.0f} {fh:.0f}" preserveAspectRatio="xMidYMid meet" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<g fill="none" stroke="{color}" stroke-linecap="round" '
            f'stroke-linejoin="round">{"".join(parts)}</g></svg>')


def decode_digital_touch_sketch(blob):
    """com.apple.DigitalTouchBalloonProvider（Sketch=4）→ [(points, "#rrggbb"), ...]"""
    if not blob:
        return None
    fields = _pb_fields(blob)
    kind = next((v for f, w, v in fields if f == 1 and w == 0), None)
    payload = next((v for f, w, v in fields if f == 3 and w == 2), None)
    if kind != 4 or not payload:
        return None
    sub = _pb_fields(payload)
    count = next((v for f, w, v in sub if f == 1 and w == 0), 0)
    raw = next((v for f, w, v in sub if f == 2 and w == 2), b"")
    colors_raw = next((v for f, w, v in sub if f == 3 and w == 2), b"")
    colors = [f"#{colors_raw[i]:02x}{colors_raw[i+1]:02x}{colors_raw[i+2]:02x}"
              for i in range(0, len(colors_raw) - 3, 4)]
    strokes, idx = [], 0
    try:
        for k in range(count):
            if idx + 4 > len(raw):
                return None
            pts_n = struct.unpack_from("<H", raw, idx + 2)[0]; idx += 4
            end = idx + pts_n * 4
            if end > len(raw):
                return None
            pts = [(struct.unpack_from("<H", raw, idx + j * 4)[0],
                    struct.unpack_from("<H", raw, idx + j * 4 + 2)[0])
                   for j in range(pts_n)]
            idx = end
            strokes.append((pts, colors[k] if k < len(colors) else "#ffffff"))
    except struct.error:
        return None
    return strokes or None


def sketch_svg(strokes, bg="#111318"):
    """数码点触涂鸦 → SVG。坐标 0..65535，原点在左下角，y 向上"""
    xs = [p[0] for pts, _ in strokes for p in pts]
    ys = [65535 - p[1] for pts, _ in strokes for p in pts]
    if not xs:
        return None
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    span = max(max_x - min_x, max_y - min_y, 1)
    pad = span * 0.06 + 4
    vx, vy = min_x - pad, min_y - pad
    vw, vh = (max_x - min_x) + pad * 2, (max_y - min_y) + pad * 2
    sw = max(vw, vh) * 0.012
    parts = []
    for pts, color in strokes:
        if not pts:
            continue
        s = " ".join(f"{p[0]:.0f},{65535 - p[1]:.0f}" for p in pts)
        if len(pts) == 1:
            x, y = pts[0]
            parts.append(f'<circle cx="{x}" cy="{65535 - y}" r="{sw / 2:.1f}" '
                         f'fill="{color}" stroke="none"/>')
        else:
            parts.append(f'<polyline points="{s}" stroke="{color}" '
                         f'stroke-width="{sw:.1f}"/>')
    return (f'<svg viewBox="{vx:.0f} {vy:.0f} {vw:.0f} {vh:.0f}" '
            f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'
            f'<rect x="{vx:.0f}" y="{vy:.0f}" width="{vw:.0f}" height="{vh:.0f}" '
            f'fill="{bg}" rx="{max(vw, vh) * 0.02:.0f}"/>'
            f'<g fill="none" stroke-linecap="round" stroke-linejoin="round">'
            f'{"".join(parts)}</g></svg>')


PAYLOAD_HINT = {
    "手写": "Apple 把「手写消息」存成 PencilKit 笔迹数据而非图片，无法直接显示；原始载荷已导出，附在下方。",
    "数码点触": "「数码点触」是触控轨迹 + 颜色 + 时长的私有数据，不是图片；原始载荷已导出，需专用播放器还原动画。",
}


def extract_payloads(rows, outdir):
    """把手写 / 数码点触 / 小游戏等插件消息的 payload_data 落盘"""
    dest_root = os.path.join(outdir, "payloads")
    written = 0
    for r in rows:
        blob = r.pop("_payload", None)
        label = r.get("app") or ""
        if not blob or not label:
            continue
        os.makedirs(dest_root, exist_ok=True)
        magic, ext = classify_blob(blob)
        name = f"{r['rowid']:06d}_{safe_name(label.strip('[]').replace(' ', '_').replace(':', ''), 24)}.{ext}"
        path = os.path.join(dest_root, name)
        try:
            with open(path, "wb") as f:
                f.write(blob)
        except OSError:
            continue
        r["payload"] = {"label": f"{label} · {magic}", "url": quote(f"payloads/{name}"),
                        "bytes": len(blob)}
        written += 1

        # 能还原的笔迹直接生成预览 SVG
        svg = None
        if "手写" in label:
            strokes, frame = decode_handwriting(blob)
            if strokes:
                svg = handwriting_svg(strokes, frame)
        elif "数码点触" in label:
            sk = decode_digital_touch_sketch(blob)
            if sk:
                svg = sketch_svg(sk)
        if svg:
            r["svg"] = svg
            r["note"] = ""
            svg_path = os.path.join(dest_root, name.rsplit(".", 1)[0] + ".svg")
            try:
                with open(svg_path, "w", encoding="utf-8") as f:
                    f.write(svg)
            except OSError:
                pass
            continue
        # 还原不了的插件消息补一句说明
        for key, hint in PAYLOAD_HINT.items():
            if key in label:
                r["note"] = hint
                break
    return written


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="导出 macOS 信息（iMessage/SMS）到 CSV/JSON/HTML")
    ap.add_argument("-o", "--out", default=None, help="输出目录")
    ap.add_argument("--db", default=DEFAULT_DB, help="chat.db 路径")
    ap.add_argument("--no-copy-attachments", action="store_true",
                    help="不复制附件文件（默认会复制，避免重跑后图片/视频失效）")
    ap.add_argument("--no-inline", action="store_true", help="不要把图片内联进 HTML")
    ap.add_argument("--tz", type=float, default=8.0, help="时区偏移小时，默认 8（北京时间）")
    args = ap.parse_args()

    db = os.path.expanduser(args.db)
    if not os.path.exists(db):
        print(f"✗ 找不到数据库: {db}")
        return 2

    # 先做一次可读性探测，尽早给出准确提示
    try:
        with open(db, "rb") as f:
            f.read(16)
    except PermissionError:
        print("✗ 没有读取权限。请到 系统设置 → 隐私与安全性 → 完全磁盘访问权限，"
              "把运行本脚本的程序（终端 / WorkBuddy）打开，然后重启该程序。")
        return 3
    except OSError as e:
        print(f"✗ 读取失败: {e}")
        return 3

    tz = timezone(timedelta(hours=args.tz))
    outdir = os.path.expanduser(args.out) if args.out else os.path.join(
        os.path.expanduser("~/Desktop"), f"iMessage导出_{datetime.now():%Y%m%d}")

    tmp = tempfile.mkdtemp(prefix="imessage_export_")
    try:
        print(f"→ 读取数据库: {db}")
        local_db = snapshot_db(db, tmp)
        conn = sqlite3.connect(local_db)

        lookup = load_contacts()
        chats = load_chats(conn, lookup)
        print(f"→ 对话数: {len(chats)}")
        rows = load_messages(conn, chats, tz, lookup)
        conn.close()
        print(f"→ 消息数: {len(rows)}")

        if not rows:
            print("！数据库里没有消息记录")
            return 1

        os.makedirs(outdir, exist_ok=True)

        if not args.no_copy_attachments:
            print("→ 复制附件中…")
            ok, bad = copy_attachments(rows, outdir)
            print(f"  附件复制成功 {ok} 个，缺失/失败 {bad} 个")
        link_originals(rows)
        if not args.no_inline:
            inlined = inline_images(rows, outdir)
            if inlined:
                print(f"→ 内联图片: {inlined / 1024:.0f} KB（index.html 可单独分享）")

        n_payload = extract_payloads(rows, outdir)
        if n_payload:
            print(f"→ 导出插件原始数据: {n_payload} 条")

        write_csv(rows, os.path.join(outdir, "messages.csv"))
        write_json(rows, os.path.join(outdir, "messages.json"))
        write_html(rows, os.path.join(outdir, "index.html"))

        span_start = rows[0]["timestamp"]
        span_end = rows[-1]["timestamp"]
        print(f"\n✓ 导出完成: {outdir}")
        print(f"  时间范围: {span_start} ~ {span_end}")
        print("  文件: index.html（可搜索浏览） · messages.csv · messages.json")
        return 0
    except sqlite3.Error as e:
        print(f"✗ 数据库读取失败: {e}")
        return 4
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
