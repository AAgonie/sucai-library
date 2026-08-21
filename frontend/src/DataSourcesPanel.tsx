import { useEffect, useRef, useState } from "react";
import { Pencil, RefreshCw, Trash2, Wifi } from "lucide-react";

type ScanJob = {
  id: number; job_type: string; status: "queued" | "running" | "cancel_requested" | "done" | "failed" | "cancelled";
  progress_current: number; progress_total: number; current_path: string; error: string;
  queue_position: number; message: string; result: Record<string, number>;
};
type Source = {
  id: number; name: string; root_path: string; enabled: boolean; recursive: boolean;
  scan_interval_minutes: number; auto_ocr: boolean; default_author: string; updated_at: string;
  last_scanned_at: string | null; last_scan_error: string; active_job?: ScanJob | null;
};
type SourceDraft = Omit<Source, "id" | "updated_at" | "last_scanned_at" | "last_scan_error" | "active_job">;
type Validation = { status: "ok" | "unavailable" | "invalid" | "unauthorized" | "error"; message: string };

const emptyDraft: SourceDraft = {
  name: "", root_path: "", enabled: true, recursive: true, scan_interval_minutes: 10, auto_ocr: true, default_author: ""
};

async function api<T>(url: string, init?: RequestInit) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
}

export function DataSourcesPanel() {
  const [items, setItems] = useState<Source[]>([]);
  const [note, setNote] = useState("");
  const [validations, setValidations] = useState<Record<number, Validation>>({});
  const [scanJobs, setScanJobs] = useState<Record<number, ScanJob>>({});
  const [busyId, setBusyId] = useState<number | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingVersion, setEditingVersion] = useState("");
  const [draft, setDraft] = useState<SourceDraft>(emptyDraft);
  const scanTimers = useRef<Record<number, number>>({});

  const load = () => api<{ items: Source[] }>("/api/data-sources").then((data) => {
    setItems(data.items);
    setScanJobs((current) => {
      const next: Record<number, ScanJob> = {};
      data.items.forEach((source) => {
        if (source.active_job) next[source.id] = source.active_job;
        else if (current[source.id]?.status === "done" || current[source.id]?.status === "failed") next[source.id] = current[source.id];
      });
      return next;
    });
    data.items.forEach((source) => { if (source.active_job) queueNextPoll(source, source.active_job.id, 5000); });
  });

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => () => Object.values(scanTimers.current).forEach((timer) => window.clearTimeout(timer)), []);

  function resetForm() {
    setEditingId(null);
    setEditingVersion("");
    setDraft(emptyDraft);
  }

  function beginEdit(source: Source) {
    setEditingId(source.id);
    setEditingVersion(source.updated_at);
    setDraft({
      name: source.name, root_path: source.root_path, enabled: source.enabled, recursive: source.recursive,
      scan_interval_minutes: source.scan_interval_minutes, auto_ocr: source.auto_ocr, default_author: source.default_author
    });
    setNote(`正在修改数据源“${source.name}”。`);
  }

  async function save() {
    setNote("");
    try {
      const editing = editingId !== null;
      const source = await api<Source>(editing ? `/api/data-sources/${editingId}` : "/api/data-sources", {
        method: editing ? "PUT" : "POST", body: JSON.stringify(editing ? { ...draft, expected_updated_at: editingVersion } : draft)
      });
      setNote(editing ? `数据源“${source.name}”已更新。` : "数据源已保存，正在进行只读访问验证。");
      resetForm();
      await load();
      if (!editing) await validate(source.id);
    } catch (error) {
      setNote(`无法${editingId === null ? "新增" : "更新"}数据源：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }

  async function validate(sourceId: number) {
    setBusyId(sourceId);
    try {
      const result = await api<Validation>(`/api/data-sources/${sourceId}/validate`, { method: "POST" });
      setValidations((current) => ({ ...current, [sourceId]: result }));
    } catch (error) {
      setValidations((current) => ({ ...current, [sourceId]: { status: "error", message: error instanceof Error ? error.message : "验证失败" } }));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(source: Source) {
    if (!window.confirm(`删除数据源“${source.name}”吗？只会移除扫描配置；已入库图片和 NAS 文件都会保留。`)) return;
    setBusyId(source.id);
    try {
      const result = await api<{ message: string }>(`/api/data-sources/${source.id}`, { method: "DELETE" });
      setNote(result.message);
      setItems((current) => current.filter((item) => item.id !== source.id));
      setValidations((current) => { const next = { ...current }; delete next[source.id]; return next; });
      setScanJobs((current) => { const next = { ...current }; delete next[source.id]; return next; });
      if (editingId === source.id) resetForm();
      void load();
    } catch (error) {
      setNote(`无法删除数据源：${error instanceof Error ? error.message : "未知错误"}`);
    } finally {
      setBusyId(null);
    }
  }

  function queueNextPoll(source: Source, jobId: number, delayMs: number) {
    const previous = scanTimers.current[source.id];
    if (previous) window.clearTimeout(previous);
    scanTimers.current[source.id] = window.setTimeout(() => void pollScan(source, jobId), delayMs);
  }

  async function pollScan(source: Source, jobId: number) {
    try {
      const job = await api<ScanJob>(`/api/jobs/${jobId}`);
      setScanJobs((current) => ({ ...current, [source.id]: job }));
      if (job.status === "queued" || job.status === "running" || job.status === "cancel_requested") {
        queueNextPoll(source, jobId, job.status === "queued" ? 5000 : 2000);
        return;
      }
      delete scanTimers.current[source.id];
      if (job.status === "done") {
        const result = job.result || {};
        setNote(`“${source.name}”扫描完成：发现 ${result.discovered || 0} 个图片文件，新增 ${result.imported || 0} 条，更新 ${result.updated || 0} 条，未变更 ${result.unchanged || 0} 条。`);
        await load();
      } else if (job.status === "cancelled") {
        setNote(`“${source.name}”扫描已停止。`);
        await load();
      } else {
        setNote(`“${source.name}”扫描失败：${job.error || job.message || "未知错误"}`);
      }
    } catch (error) {
      setNote(`无法读取“${source.name}”扫描状态：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }

  async function startScan(source: Source) {
    try {
      const result = await api<{ job_id: number }>(`/api/data-sources/${source.id}/scan`, { method: "POST" });
      setScanJobs((current) => ({ ...current, [source.id]: { id: result.job_id, job_type: "scan", status: "queued", progress_current: 0, progress_total: 0, current_path: "", error: "", queue_position: 0, message: "", result: {} } }));
      setNote(`“${source.name}”已加入扫描队列，正在等待结果。`);
      await pollScan(source, result.job_id);
    } catch (error) {
      setNote(`无法加入扫描队列：${error instanceof Error ? error.message : "未知错误"}`);
    }
  }

  async function stopScan(source: Source, jobId: number) {
    setBusyId(source.id);
    try {
      const result = await api<{ status: ScanJob["status"]; message: string }>(`/api/jobs/${jobId}/cancel`, { method: "POST" });
      setNote(`“${source.name}”${result.message}`);
      await pollScan(source, jobId);
    } catch (error) {
      setNote(`无法停止“${source.name}”扫描：${error instanceof Error ? error.message : "未知错误"}`);
    } finally {
      setBusyId(null);
    }
  }

  return <section className="data-sources-panel">
    <div className="panel-heading"><div><h2>{editingId === null ? "数据源" : "修改数据源"}</h2><p>多个共享目录的后台扫描配置；不会保存 SMB（Windows 共享协议）账号或密码。关闭自动定时扫描后，仍可随时手动扫描。</p></div></div>
    <div className="config-grid">
      <label>名称<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
      <label className="wide">UNC 路径<input value={draft.root_path} onChange={(event) => setDraft({ ...draft, root_path: event.target.value })} placeholder="例如 \\服务器\共享目录\图片" /></label>
      <label>扫描间隔（分钟）<input type="number" min="1" disabled={!draft.enabled} value={draft.scan_interval_minutes} onChange={(event) => setDraft({ ...draft, scan_interval_minutes: Number(event.target.value) })} /></label>
      <label>默认作者<input value={draft.default_author} onChange={(event) => setDraft({ ...draft, default_author: event.target.value })} /></label>
      <label className="checkbox-line"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />启用自动定时扫描</label>
      <label className="checkbox-line"><input type="checkbox" checked={draft.recursive} onChange={(event) => setDraft({ ...draft, recursive: event.target.checked })} />递归扫描</label>
      <label className="checkbox-line"><input type="checkbox" checked={draft.auto_ocr} onChange={(event) => setDraft({ ...draft, auto_ocr: event.target.checked })} />自动 OCR</label>
    </div>
    <div className="action-row"><button type="button" className="primary-action" onClick={() => void save()}>{editingId === null ? "新增数据源" : "保存修改"}</button>{editingId !== null ? <button type="button" className="ghost-action" onClick={resetForm}>取消修改</button> : null}</div>
    {note ? <p className="data-source-note">{note}</p> : null}
    <div className="trash-list data-source-list">{items.map((source) => {
      const validation = validations[source.id];
      const scanJob = scanJobs[source.id];
      const running = scanJob?.status === "running" || scanJob?.status === "cancel_requested";
      const waiting = scanJob?.status === "queued";
      const scanning = running || waiting;
      return <article key={source.id} className="data-source-row">
        <div><strong>{source.name}</strong><small>{source.root_path} · {source.enabled ? `${source.scan_interval_minutes} 分钟自动扫描` : "仅手动扫描"} · {source.recursive ? "递归扫描" : "仅当前目录"}</small></div>
        <div className="data-source-actions">
          <button type="button" className="ghost-action" onClick={() => beginEdit(source)} disabled={busyId === source.id || running}><Pencil size={16} />修改</button>
          <button type="button" className="ghost-action" onClick={() => void validate(source.id)} disabled={busyId === source.id || scanning}><Wifi size={16} />验证访问</button>
          <button type="button" className="ghost-action" onClick={() => void startScan(source)} disabled={busyId === source.id || scanning}><RefreshCw size={16} />{scanning ? "正在扫描" : "立即扫描"}</button>
          {scanning ? <button type="button" className="ghost-action danger-action" onClick={() => void stopScan(source, scanJob.id)} disabled={busyId === source.id || scanJob.status === "cancel_requested"}>{scanJob.status === "cancel_requested" ? "正在停止" : "停止扫描"}</button> : null}
          <button type="button" className="ghost-action danger-action" onClick={() => void remove(source)} disabled={busyId === source.id || running}><Trash2 size={16} />删除</button>
        </div>
        {validation ? <p className={`data-source-validation ${validation.status}`}><strong>{validation.status === "ok" ? "访问正常" : "访问未通过"}</strong>{validation.message}</p> : <p className="data-source-validation pending">尚未验证。验证不会扫描、写入、移动或删除 NAS 文件。</p>}
        {scanJob ? <p className={`data-source-validation scan-${scanJob.status}`}><strong>{scanJob.status === "queued" ? "等待扫描" : scanJob.status === "running" ? "正在扫描" : scanJob.status === "cancel_requested" ? "正在停止" : scanJob.status === "done" ? "扫描完成" : scanJob.status === "cancelled" ? "已停止" : "扫描失败"}</strong>{scanJob.status === "queued" ? `任务已进入公共队列${scanJob.queue_position > 0 ? `，前方还有 ${scanJob.queue_position - 1} 项扫描任务。` : "。"}` : scanJob.status === "running" || scanJob.status === "cancel_requested" ? `${scanJob.status === "cancel_requested" ? "已请求停止；" : ""}${scanJob.progress_total ? `${scanJob.progress_current}/${scanJob.progress_total}` : "正在读取目录"}${scanJob.current_path ? ` · ${scanJob.current_path}` : ""}` : scanJob.message || scanJob.error}</p> : null}
        {!scanJob && source.last_scan_error ? <p className="data-source-validation error"><strong>上次扫描失败</strong>{source.last_scan_error}</p> : null}
      </article>;
    })}</div>
  </section>;
}
