#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
无边记（Freeform）看板导出

能做什么：
  - 导出每个看板的标题、最后活动时间、是否共享/已删（boards.csv）
  - 导出看板缩略图 PNG（优先取共享记录里的高清缩略图，其次取 App 缓存预览图）

做不到什么：
  - 看板内容本身是私有 CRDT（boards.data / board_items.specific_data），没有公开格式，
    无法还原成 PDF 或可编辑文件。要完整保真只能靠 App 自己的「文件 > 导出为 PDF」。

用法：
    python3 export_freeform.py                    # 输出到桌面的 无边记导出_日期
    python3 export_freeform.py -o ~/somewhere
"""

import argparse
import csv
import os
import plistlib
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

BOARDS_DB = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.freeform/Boards/boards.db")
PREVIEW_DIR = os.path.expanduser(
    "~/Library/Containers/com.apple.freeform/Data/Library/Caches/BoardPreviewImages2")
APPLE_EPOCH = datetime(2001, 1, 1)


def safe(s, limit=40):
    return re.sub(r"[^\w\-.@+\u4e00-\u9fff]", "_", str(s or ""))[:limit] or "未命名"


def apple_time(v):
    try:
        return APPLE_EPOCH + timedelta(seconds=float(v))
    except (TypeError, ValueError):
        return None


def snapshot(src, workdir):
    dst = os.path.join(workdir, "boards.db")
    shutil.copy2(src, dst)
    for suf in ("-wal", "-shm"):
        if os.path.exists(src + suf):
            try:
                shutil.copy2(src + suf, dst + suf)
            except OSError:
                pass
    return dst


def _resolve(objs, v, depth=0):
    """NSKeyedArchiver 的 UID 是指向 $objects 的下标，这里解引用"""
    while isinstance(v, plistlib.UID) and depth < 8:
        try:
            v = objs[v.data]
        except (IndexError, AttributeError):
            return None
        depth += 1
    return v


def _walk_dicts(obj, objs, seen=None):
    """把 NSDictionary 还原成普通 dict 并逐个 yield"""
    if seen is None:
        seen = set()
    if isinstance(obj, (dict, list)):
        if id(obj) in seen:
            return
        seen.add(id(obj))
    if isinstance(obj, dict):
        keys = _resolve(objs, obj.get("NS.keys"))
        vals = _resolve(objs, obj.get("NS.objects"))
        if isinstance(keys, list) and isinstance(vals, list):
            plain = {}
            for k, v in zip(keys, vals):
                k = _resolve(objs, k)
                if isinstance(k, str):
                    plain[k] = _resolve(objs, v)
            if plain:
                yield plain
        for v in obj.values():
            yield from _walk_dicts(v, objs, seen)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v, objs, seen)


def extract_meta(blob):
    """从 CloudKit 归档里挖标题与缩略图"""
    title, thumb, shared = None, None, False
    if not blob:
        return title, thumb, shared
    i = blob.find(b"bplist00")
    if i < 0:
        return title, thumb, shared
    try:
        pl = plistlib.loads(blob[i:])
    except Exception:
        return title, thumb, shared
    objs = pl.get("$objects") or []
    for d in _walk_dicts(pl, objs):
        if "cloudkit.title" in d and isinstance(d["cloudkit.title"], str):
            title = d["cloudkit.title"]
        t = d.get("cloudkit.thumbnailImageData")
        if isinstance(t, (bytes, bytearray)) and bytes(t[:4]) == b"\x89PNG":
            thumb = bytes(t)
        if d.get("cloudkit.type") == "freeform":
            shared = True
    if any(isinstance(o, str) and o == "cloudkit.zoneshare" for o in objs):
        shared = True
    return title, thumb, shared


def cache_preview(board_uuid):
    """App 缓存里的预览图，取最大尺寸的那个"""
    best, best_area = None, 0
    try:
        names = os.listdir(PREVIEW_DIR)
    except OSError:
        return None
    for fn in names:
        if board_uuid.lower() not in fn.lower():
            continue
        path = os.path.join(PREVIEW_DIR, fn)
        size = os.path.getsize(path)
        if size > best_area:
            best, best_area = path, size
    return best


def main():
    ap = argparse.ArgumentParser(description="导出无边记看板清单与缩略图")
    ap.add_argument("-o", "--out", default=None, help="输出目录")
    ap.add_argument("--db", default=BOARDS_DB, help="boards.db 路径")
    args = ap.parse_args()

    db = os.path.expanduser(args.db)
    if not os.path.exists(db):
        print(f"✗ 找不到数据库: {db}")
        print("  可能从未使用过「无边记」，或数据只在 iCloud 未落地到本机。")
        return 2

    outdir = os.path.expanduser(args.out) if args.out else os.path.join(
        os.path.expanduser("~/Desktop"), f"无边记导出_{datetime.now():%Y%m%d}")

    tmp = tempfile.mkdtemp(prefix="freeform_export_")
    try:
        local_db = snapshot(db, tmp)
        conn = sqlite3.connect(local_db)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(boards)")}

        def c(name, expr="NULL"):
            return name if name in cols else expr

        sql = f"""SELECT board_identifier,
                         {c('last_activity_time')} AS last_activity,
                         {c('tombstoned', '0')} AS tombstoned,
                         {c('ckshare_data')} AS share_data,
                         {c('sync_data')} AS sync_data
                  FROM boards"""
        rows = list(conn.execute(sql))

        # 已删看板单独统计，默认也导出来（用户可能想要）
        records = []
        for r in rows:
            bid = r["board_identifier"]
            try:
                u = str(uuid.UUID(bytes=bid)) if isinstance(bid, bytes) else str(bid)
            except ValueError:
                u = str(bid)
            title, thumb, _ = extract_meta(r["share_data"])
            if not title:
                t2, thumb2, _ = extract_meta(r["sync_data"])
                title = t2 or title
                thumb = thumb or thumb2
            shared = bool(r["share_data"])
            records.append({
                "uuid": u,
                "title": title or "未命名",
                "shared": shared,
                "deleted": int(r["tombstoned"] or 0),
                "last_activity": apple_time(r["last_activity"]),
                "thumb": thumb,
                "preview_path": cache_preview(u),
            })

        records.sort(key=lambda x: (x["deleted"], -(x["last_activity"].timestamp()
                                                    if x["last_activity"] else 0)))
        os.makedirs(outdir, exist_ok=True)

        saved = 0
        for n, rec in enumerate(records, 1):
            tag = "已删除_" if rec["deleted"] else ""
            stem = f"{n:02d}_{tag}{safe(rec['title'])}_{rec['uuid'][:8]}"
            path = None
            if rec["thumb"]:
                path = os.path.join(outdir, stem + ".png")
                with open(path, "wb") as f:
                    f.write(rec["thumb"])
                rec["source"] = "CloudKit 缩略图"
            elif rec["preview_path"]:
                path = os.path.join(outdir, stem + ".png")
                shutil.copy2(rec["preview_path"], path)
                rec["source"] = "本地预览缓存"
            rec["file"] = os.path.basename(path) if path else ""
            saved += 1 if path else 0

        with open(os.path.join(outdir, "boards.csv"), "w", newline="",
                  encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["序号", "标题", "UUID", "最后活动时间", "是否共享", "已删除",
                        "图片文件", "图片来源"])
            for n, rec in enumerate(records, 1):
                w.writerow([n, rec["title"], rec["uuid"],
                            rec["last_activity"].strftime("%Y-%m-%d %H:%M")
                            if rec["last_activity"] else "",
                            "是" if rec["shared"] else "否",
                            "是" if rec["deleted"] else "否",
                            rec.get("file", ""), rec.get("source", "")])

        with open(os.path.join(outdir, "说明.md"), "w", encoding="utf-8") as f:
            f.write("""# 无边记导出说明

## 这里有什么

- 每个看板一张缩略图（PNG）。优先来自 iCloud 同步记录里的高清缩略图，
  没有的话取 App 本地缓存的预览图（尺寸偏小，约 400px）。
- `boards.csv`：看板清单，含标题、UUID、最后活动时间、是否共享、是否已删除。

## 为什么没有看板内容本身

无边记把看板存成私有 CRDT 数据（`boards.data` / `board_items.specific_data`），
不是 PDF、不是图片，也没有公开格式。第三方工具只能读到元数据和缩略图，
无法还原成可编辑文件或用高分辨率渲染。

## 要完整保真怎么办

在「无边记」App 里逐个看板导出：

1. 打开看板 → 菜单栏 `文件 > 导出为 PDF`（或工具栏 `...` → 导出）
2. 也支持 `文件 > 导出为图片`，以及复制全部内容粘贴到其他 App
3. 批量：只能逐个做，Apple 没提供批量导出接口

## 已删除的看板

标记为「已删除」的是最近删除里尚未彻底清除的看板，通常还能在 App 的
「最近删除」里恢复并导出。
""")

        live = sum(1 for r in records if not r["deleted"])
        print(f"→ 看板数: {len(records)}（有效 {live}，已删除 {len(records) - live}）")
        print(f"→ 缩略图: {saved} 张")
        print(f"\n✓ 导出完成: {outdir}")
        for rec in records:
            act = rec["last_activity"].strftime("%Y-%m-%d") if rec["last_activity"] else "—"
            print(f"   {'[已删]' if rec['deleted'] else '      '} {rec['title'][:18]:<20} {act}  "
                  f"{rec.get('source', '无图')}")
        return 0
    except sqlite3.Error as e:
        print(f"✗ 数据库读取失败: {e}")
        return 4
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
