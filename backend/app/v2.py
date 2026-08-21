"""V2 data-source, persistent-job, and tag-library endpoints."""
from __future__ import annotations
import json
import os
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .db import get_db, normalize_path_key, transaction, utc_now_text

router = APIRouter(prefix="/api")

class DataSourceInput(BaseModel):
    name: str
    root_path: str
    enabled: bool = True
    recursive: bool = True
    scan_interval_minutes: int = Field(10, ge=1, le=1440)
    auto_ocr: bool = True
    default_author: str = ""
    expected_updated_at: str = ""
class CategoryInput(BaseModel): name: str
class TagInput(BaseModel): category_id: int; name: str
class AssetTagsInput(BaseModel): tag_ids: list[int]

def _source(row: Any) -> dict[str, Any]:
    item=dict(row)
    for name in ("enabled","recursive","auto_ocr"): item[name]=bool(item[name])
    return item

def _job_snapshot(row: Any, queue_position: int = 0) -> dict[str, Any]:
    return {
        "id": row["id"], "job_type": row["job_type"], "status": row["status"],
        "progress_current": row["progress_current"], "progress_total": row["progress_total"],
        "current_path": row["current_path"], "error": row["error"],
        "queue_position": queue_position, "message": "", "result": {},
    }
def _overlap(a: str,b: str) -> bool:
    a,b=normalize_path_key(a),normalize_path_key(b)
    return a==b or a.startswith(b+"/") or b.startswith(a+"/")
def _validate_source(payload: DataSourceInput, current: Optional[int]=None, connection: Any=None) -> None:
    if not payload.name.strip() or not payload.root_path.strip(): raise HTTPException(400,"名称和 UNC 路径不能为空")
    if connection is not None:
        rows=connection.execute("SELECT id,root_path FROM data_sources WHERE source_type <> 'legacy'").fetchall()
    else:
        with get_db() as c: rows=c.execute("SELECT id,root_path FROM data_sources WHERE source_type <> 'legacy'").fetchall()
    if any(row["id"] != current and _overlap(payload.root_path,row["root_path"]) for row in rows): raise HTTPException(409,"数据源根目录与已有数据源重叠")

@router.get("/data-sources")
def data_sources() -> dict[str,Any]:
    with get_db() as c:
        rows=c.execute("SELECT * FROM data_sources WHERE source_type <> 'legacy' ORDER BY name COLLATE NOCASE").fetchall()
        active=c.execute(
            "SELECT * FROM background_jobs WHERE job_type='scan' AND status IN ('queued','running','cancel_requested') "
            "AND data_source_id IS NOT NULL ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id"
        ).fetchall()
        queue_positions={row["id"]: index for index,row in enumerate(
            c.execute("SELECT id FROM background_jobs WHERE job_type='scan' AND status='queued' ORDER BY id").fetchall(), start=1
        )}
    active_by_source={row["data_source_id"]: row for row in active}
    items=[]
    for row in rows:
        item=_source(row)
        job=active_by_source.get(row["id"])
        item["active_job"]=_job_snapshot(job, queue_positions.get(job["id"], 0)) if job else None
        items.append(item)
    return {"items":items}
@router.post("/data-sources")
def create_source(payload: DataSourceInput) -> dict[str,Any]:
    now=utc_now_text()
    with get_db() as c:
        try:
            with transaction(c):
                _validate_source(payload,connection=c)
                cur=c.execute("INSERT INTO data_sources(name,source_type,root_path,root_path_key,enabled,recursive,scan_interval_minutes,auto_ocr,default_author,created_at,updated_at) VALUES (?, 'smb',?,?,?,?,?,?,?,?,?)",(payload.name.strip(),payload.root_path.strip(),normalize_path_key(payload.root_path),int(payload.enabled),int(payload.recursive),payload.scan_interval_minutes,int(payload.auto_ocr),payload.default_author.strip(),now,now))
                source=c.execute("SELECT * FROM data_sources WHERE id=?",(cur.lastrowid,)).fetchone()
        except HTTPException: raise
        except Exception as e: raise HTTPException(409,f"无法创建数据源：{e}")
        return _source(source)
@router.put("/data-sources/{source_id}")
def update_source(source_id:int,payload:DataSourceInput)->dict[str,Any]:
    with get_db() as c:
        with transaction(c):
            current=c.execute("SELECT * FROM data_sources WHERE id=? AND source_type<>'legacy'",(source_id,)).fetchone()
            if not current: raise HTTPException(404,"数据源不存在")
            if payload.expected_updated_at and payload.expected_updated_at != current["updated_at"]:
                raise HTTPException(409,"该数据源已被另一终端更新，请刷新后重新修改。")
            _validate_source(payload,source_id,connection=c)
            cur=c.execute("UPDATE data_sources SET name=?,root_path=?,root_path_key=?,enabled=?,recursive=?,scan_interval_minutes=?,auto_ocr=?,default_author=?,updated_at=? WHERE id=? AND source_type<>'legacy'",(payload.name.strip(),payload.root_path.strip(),normalize_path_key(payload.root_path),int(payload.enabled),int(payload.recursive),payload.scan_interval_minutes,int(payload.auto_ocr),payload.default_author.strip(),utc_now_text(),source_id))
            if not cur.rowcount: raise HTTPException(404,"数据源不存在")
            if not payload.enabled:
                queued=c.execute("SELECT id,asset_id FROM background_jobs WHERE data_source_id=? AND job_type='scan' AND status='queued'",(source_id,)).fetchall()
                c.execute("UPDATE background_jobs SET status='cancelled',finished_at=?,error='' WHERE data_source_id=? AND job_type='scan' AND status='queued'",(utc_now_text(),source_id))
                c.execute("UPDATE background_jobs SET status='cancel_requested',error=? WHERE data_source_id=? AND job_type='scan' AND status='running'",("自动扫描已关闭，正在停止当前扫描。",source_id))
                for job in queued:
                    c.execute("INSERT INTO job_events(job_id,event_type,message,asset_id,created_at) VALUES (?, 'cancelled', ?, ?, ?)",(job["id"],"关闭自动扫描时取消等待任务。",job["asset_id"],utc_now_text()))
            source=c.execute("SELECT * FROM data_sources WHERE id=?",(source_id,)).fetchone()
        return _source(source)
@router.delete("/data-sources/{source_id}")
def delete_source(source_id:int)->dict[str,Any]:
    with get_db() as c:
        with transaction(c):
            source=c.execute("SELECT name FROM data_sources WHERE id=? AND source_type<>'legacy'",(source_id,)).fetchone()
            if not source: raise HTTPException(404,"数据源不存在")
            if c.execute("SELECT 1 FROM background_jobs WHERE data_source_id=? AND status IN ('running','cancel_requested')",(source_id,)).fetchone():
                raise HTTPException(409,"该数据源仍在扫描或停止中，请等待任务结束后再删除")
            asset_count=c.execute("SELECT COUNT(*) FROM assets WHERE data_source_id=?",(source_id,)).fetchone()[0]
            cancelled=c.execute("UPDATE background_jobs SET status='cancelled',finished_at=? WHERE data_source_id=? AND status='queued'",(utc_now_text(),source_id)).rowcount
            c.execute("UPDATE background_jobs SET data_source_id=NULL WHERE data_source_id=?",(source_id,))
            c.execute("UPDATE assets SET data_source_id=NULL WHERE data_source_id=?",(source_id,))
            cur=c.execute("DELETE FROM data_sources WHERE id=? AND source_type<>'legacy'",(source_id,))
    return {"deleted":int(cur.rowcount),"preserved_assets":asset_count,"cancelled_jobs":cancelled,"message":f"已删除数据源“{source['name']}”；已入库的 {asset_count} 条资产记录保留。"}


@router.post("/data-sources/{source_id}/validate")
def validate_source(source_id:int)->dict[str,str]:
    """Check that the server process can read the configured SMB directory.

    No files are changed and no scan job is created.  SMB credentials remain
    outside the database and are supplied by the Windows service account.
    """
    with get_db() as c:
        source=c.execute("SELECT name,root_path FROM data_sources WHERE id=? AND source_type<>'legacy'",(source_id,)).fetchone()
    if not source: raise HTTPException(404,"数据源不存在")
    path=os.fspath(source["root_path"])
    try:
        if not os.path.exists(path):
            return {"status":"unavailable","message":"路径不存在、网络不可达，或当前服务账户没有该共享目录权限。"}
        if not os.path.isdir(path):
            return {"status":"invalid","message":"该路径可访问，但不是文件夹。"}
        with os.scandir(path) as entries:
            next(entries, None)
    except PermissionError:
        return {"status":"unauthorized","message":"路径存在，但运行服务的 Windows 账户没有读取权限。"}
    except OSError as exc:
        return {"status":"error","message":f"读取路径失败：{exc}"}
    return {"status":"ok","message":"验证通过：服务电脑可读取该文件夹，可继续立即扫描或等待定时扫描。"}

def enqueue(
    job_type: str,
    source_id: Optional[int] = None,
    asset_id: Optional[int] = None,
    payload: dict[str, Any] | None = None,
) -> int:
    serialized_payload = json.dumps(payload or {}, ensure_ascii=False)
    with get_db() as c:
        # BEGIN IMMEDIATE serializes this lookup-and-insert across server processes.
        with transaction(c):
            duplicate_payload_clause = " AND payload=?" if job_type == "duplicate" else ""
            lookup_params: tuple[Any, ...] = (job_type, source_id, asset_id, serialized_payload) if job_type == "duplicate" else (job_type, source_id, asset_id)
            old=c.execute(
                "SELECT id FROM background_jobs WHERE job_type=? AND status IN ('queued','running') "
                "AND COALESCE(data_source_id,0)=COALESCE(?,0) AND COALESCE(asset_id,0)=COALESCE(?,0)"
                + duplicate_payload_clause,
                lookup_params,
            ).fetchone()
            if old:return old["id"]
            return c.execute(
                "INSERT INTO background_jobs(job_type,status,data_source_id,asset_id,payload,created_at) "
                "VALUES (?, 'queued',?,?,?,?)",
                (job_type, source_id, asset_id, serialized_payload, utc_now_text()),
            ).lastrowid
@router.post("/data-sources/{source_id}/scan")
def queue_scan(source_id:int)->dict[str,int]:
    with get_db() as c:
        if not c.execute("SELECT 1 FROM data_sources WHERE id=?",(source_id,)).fetchone(): raise HTTPException(404,"数据源不存在")
    return {"job_id":enqueue("scan",source_id=source_id)}


@router.get("/jobs/{job_id}")
def job_detail(job_id:int)->dict[str,Any]:
    with get_db() as c:
        job=c.execute("SELECT * FROM background_jobs WHERE id=?",(job_id,)).fetchone()
        event=c.execute("SELECT message,detail FROM job_events WHERE job_id=? AND event_type IN ('completed','error','cancelled') ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
        queue_position=c.execute("SELECT COUNT(*) FROM background_jobs WHERE job_type=? AND status='queued' AND id<=?",(job["job_type"],job["id"])).fetchone()[0] if job and job["status"] == "queued" else 0
    if not job: raise HTTPException(404,"任务不存在")
    result: dict[str,Any]={}
    if event and event["detail"]:
        try: result=json.loads(event["detail"])
        except json.JSONDecodeError: result={}
    return {**_job_snapshot(job, queue_position),"message":event["message"] if event else "","result":result}

@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id:int)->dict[str,Any]:
    """Cancel a queued job immediately or request cooperative cancellation for a running job."""
    with get_db() as c:
        with transaction(c):
            job=c.execute("SELECT * FROM background_jobs WHERE id=?",(job_id,)).fetchone()
            if not job: raise HTTPException(404,"任务不存在")
            if job["status"] == "queued":
                c.execute("UPDATE background_jobs SET status='cancelled',finished_at=?,error='' WHERE id=? AND status='queued'",(utc_now_text(),job_id))
                c.execute("INSERT INTO job_events(job_id,event_type,message,asset_id,created_at) VALUES (?, 'cancelled', ?, ?, ?)",(job_id,"已取消等待中的任务。",job["asset_id"],utc_now_text()))
                status="cancelled"; message="已取消等待中的任务。"
            elif job["status"] == "running":
                c.execute("UPDATE background_jobs SET status='cancel_requested',error=? WHERE id=? AND status='running'",("已请求停止，正在结束当前文件。",job_id))
                status="cancel_requested"; message="已请求停止；扫描会在结束当前文件后停止。"
            elif job["status"] == "cancel_requested":
                status="cancel_requested"; message="已请求停止；正在等待工作者结束当前文件。"
            else:
                status=job["status"]; message="该任务已结束，不能再停止。"
    return {"id":job_id,"status":status,"message":message}
@router.post("/jobs/retry-ocr/{asset_id}")
def retry_ocr(asset_id:int)->dict[str,int]: return {"job_id":enqueue("ocr",asset_id=asset_id)}
@router.get("/jobs/status")
def jobs_status()->dict[str,Any]:
    with get_db() as c:
        running=c.execute(
            "SELECT j.*,COALESCE(s.name,'') AS source_name FROM background_jobs j "
            "LEFT JOIN data_sources s ON s.id=j.data_source_id WHERE j.status='running' "
            "ORDER BY CASE j.job_type WHEN 'scan' THEN 0 ELSE 1 END,j.id"
        ).fetchall()
        pending=c.execute(
            "SELECT j.*,COALESCE(s.name,'') AS source_name FROM background_jobs j "
            "LEFT JOIN data_sources s ON s.id=j.data_source_id WHERE j.status IN ('queued','cancel_requested') "
            "ORDER BY CASE j.status WHEN 'cancel_requested' THEN 0 ELSE 1 END,j.id LIMIT 12"
        ).fetchall()
        queued=c.execute("SELECT COUNT(*) FROM background_jobs WHERE status='queued'").fetchone()[0]
        queued_by_type={row["job_type"]:row["count"] for row in c.execute(
            "SELECT job_type,COUNT(*) AS count FROM background_jobs WHERE status='queued' GROUP BY job_type"
        ).fetchall()}
        failed=c.execute(
            "SELECT COUNT(*) FROM background_jobs failed_job WHERE failed_job.status='failed' AND NOT EXISTS ("
            "SELECT 1 FROM background_jobs later WHERE later.id>failed_job.id AND later.job_type=failed_job.job_type "
            "AND COALESCE(later.data_source_id,0)=COALESCE(failed_job.data_source_id,0) "
            "AND COALESCE(later.asset_id,0)=COALESCE(failed_job.asset_id,0) "
            "AND (failed_job.job_type<>'duplicate' OR later.payload=failed_job.payload) AND later.status='done')"
        ).fetchone()[0]
        event=c.execute("SELECT message FROM job_events WHERE event_type='error' ORDER BY id DESC LIMIT 1").fetchone()
    active=[dict(row) for row in running]
    return {"current":active[0] if active else None,"active":active,"pending":[dict(row) for row in pending],"queue_length":queued,"queued_by_type":queued_by_type,"failed":failed,"recent_error":event["message"] if event else ""}

@router.get("/tags/library")
def tag_library(include_inactive:bool=False)->dict[str,Any]:
    where="" if include_inactive else "WHERE c.active=1 AND t.active=1"
    with get_db() as c: rows=c.execute(f"SELECT c.id category_id,c.name category_name,c.active category_active,t.id,t.name,t.active FROM tag_categories c LEFT JOIN tag_definitions t ON t.category_id=c.id {where} ORDER BY c.name,t.name").fetchall()
    groups:dict[int,dict[str,Any]]={}
    for row in rows:
        g=groups.setdefault(row['category_id'],{'id':row['category_id'],'name':row['category_name'],'active':bool(row['category_active']),'tags':[]})
        if row['id'] is not None:g['tags'].append({'id':row['id'],'name':row['name'],'active':bool(row['active'])})
    return {'items':list(groups.values())}
@router.post("/tags/categories")
def create_category(payload:CategoryInput)->dict[str,int]:
    now=utc_now_text()
    with get_db() as c: return {'id':c.execute("INSERT INTO tag_categories(name,active,created_at,updated_at) VALUES (?,1,?,?)",(payload.name.strip(),now,now)).lastrowid}
@router.post("/tags")
def create_tag(payload:TagInput)->dict[str,int]:
    now=utc_now_text()
    with get_db() as c:return {'id':c.execute("INSERT INTO tag_definitions(category_id,name,active,created_at,updated_at) VALUES (?,?,1,?,?)",(payload.category_id,payload.name.strip(),now,now)).lastrowid}
@router.patch("/tags/categories/{category_id}")
def patch_category(category_id:int,payload:CategoryInput,active:Optional[bool]=None)->dict[str,int]:
    with get_db() as c:
        cur=c.execute("UPDATE tag_categories SET name=?,active=COALESCE(?,active),updated_at=? WHERE id=?",(payload.name.strip(),active,utc_now_text(),category_id))
    if not cur.rowcount: raise HTTPException(404,"分类不存在")
    return {'id':category_id}
@router.patch("/tags/{tag_id}")
def patch_tag(tag_id:int,payload:TagInput,active:Optional[bool]=None)->dict[str,int]:
    with get_db() as c:
        cur=c.execute("UPDATE tag_definitions SET category_id=?,name=?,active=COALESCE(?,active),updated_at=? WHERE id=?",(payload.category_id,payload.name.strip(),active,utc_now_text(),tag_id))
    if not cur.rowcount: raise HTTPException(404,"标签不存在")
    return {'id':tag_id}
@router.delete("/tags/{tag_id}")
def delete_tag(tag_id:int)->dict[str,int]:
    with get_db() as c:
        category=c.execute("SELECT c.name FROM tag_definitions t JOIN tag_categories c ON c.id=t.category_id WHERE t.id=?",(tag_id,)).fetchone()
        if not category: raise HTTPException(404,"标签不存在")
        if c.execute("SELECT 1 FROM asset_tags WHERE tag_id=?",(tag_id,)).fetchone():
            if category["name"] != "历史标签（待整理）": raise HTTPException(409,"已有图片使用该标签，请停用而非删除")
            c.execute("DELETE FROM asset_tags WHERE tag_id=?",(tag_id,))
        cur=c.execute("DELETE FROM tag_definitions WHERE id=?",(tag_id,))
    return {'deleted':1}
@router.delete("/tags/categories/{category_id}")
def delete_category(category_id:int)->dict[str,int]:
    with get_db() as c:
        if c.execute("SELECT 1 FROM tag_definitions WHERE category_id=?",(category_id,)).fetchone(): raise HTTPException(409,"分类仍有标签，不能删除")
        cur=c.execute("DELETE FROM tag_categories WHERE id=?",(category_id,))
    if not cur.rowcount: raise HTTPException(404,"分类不存在")
    return {'deleted':1}
@router.put("/assets/{asset_id}/tags")
def save_asset_tags(asset_id:int,payload:AssetTagsInput)->dict[str,Any]:
    ids=sorted(set(payload.tag_ids)); now=utc_now_text()
    with get_db() as c:
        valid={x[0] for x in c.execute("SELECT id FROM tag_definitions WHERE active=1")}
        if not set(ids)<=valid:raise HTTPException(400,"包含不存在或已停用的标签")
        c.execute("DELETE FROM asset_tags WHERE asset_id=?",(asset_id,));c.executemany("INSERT INTO asset_tags(asset_id,tag_id,created_at) VALUES (?,?,?)",[(asset_id,x,now) for x in ids])
        c.execute("INSERT INTO asset_annotations(asset_id,confirmed_at) VALUES (?,?) ON CONFLICT(asset_id) DO UPDATE SET confirmed_at=excluded.confirmed_at",(asset_id,now))
    return {'asset_id':asset_id,'tag_ids':ids}
@router.get("/assets/{asset_id}/tags")
def asset_tags(asset_id:int)->dict[str,Any]:
    with get_db() as c:
        rows=c.execute("SELECT t.id,t.name FROM asset_tags at JOIN tag_definitions t ON t.id=at.tag_id WHERE at.asset_id=?",(asset_id,)).fetchall()
    return {'items':[dict(row) for row in rows]}
