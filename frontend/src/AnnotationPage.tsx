import { useEffect, useState } from "react";

type Asset = { id: number; detectedName: string; libraryName: string; imageUrl: string };
type Tag = { id: number; name: string };
type Group = { id: number; name: string; tags: Tag[] };

async function api<T>(url: string, init?: RequestInit) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
}

export function AnnotationPage() {
  const [folders, setFolders] = useState<string[]>([]);
  const [folder, setFolder] = useState("");
  const [items, setItems] = useState<Asset[]>([]);
  const [index, setIndex] = useState(0);
  const [groups, setGroups] = useState<Group[]>([]);
  const [ids, setIds] = useState<number[]>([]);
  const [enter, setEnter] = useState(false);
  const [note, setNote] = useState("");
  const asset = items[index];

  useEffect(() => { void Promise.all([api<{ items: string[] }>("/api/folders"), api<{ items: Group[] }>("/api/tags/library")]).then(([folderData, tagData]) => { setFolders(folderData.items); setGroups(tagData.items); }); }, []);
  useEffect(() => {
    if (!folder) { setItems([]); setIndex(0); return; }
    setNote("正在跳过已有标签或已确认的图片…");
    void api<{ items: Asset[] }>(`/api/assets?folder=${encodeURIComponent(folder)}&exclude_hidden=true&annotation_status=unconfirmed&limit=200`).then((data) => {
      setItems(data.items);
      setIndex(0);
      setNote(data.items.length ? `已跳过已确认项目，剩余 ${data.items.length} 张待标注。` : "该文件夹的图片都已确认标注。 ");
    }).catch((error) => setNote(`无法读取待标注队列：${error instanceof Error ? error.message : "未知错误"}`));
  }, [folder]);
  useEffect(() => { if (asset) void api<{ items: Tag[] }>(`/api/assets/${asset.id}/tags`).then((data) => setIds(data.items.map((tag) => tag.id))); }, [asset?.id]);

  async function save() {
    if (!asset) return;
    await api(`/api/assets/${asset.id}/tags`, { method: "PUT", body: JSON.stringify({ tag_ids: ids }) });
    setItems((current) => current.filter((item) => item.id !== asset.id));
    setIndex((current) => Math.max(0, Math.min(current, items.length - 2)));
    setIds([]);
    setNote("已确认并自动跳过该图片。 ");
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (enter && event.key === "Enter") { event.preventDefault(); void save(); } };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enter, asset?.id, ids]);

  return <section className="annotation-page"><header className="panel-heading"><div><h2>逐图标注</h2><p>选择文件夹后逐张审阅；首次打开会自动跳过已有标签或曾确认的图片，已隐藏记录也不会进入队列。</p></div><label>文件夹<select value={folder} onChange={(event) => setFolder(event.target.value)}><option value="">请选择</option>{folders.map((item) => <option value={item} key={item}>{item}</option>)}</select></label></header>{note ? <p className="annotation-note">{note}</p> : null}{asset ? <div className="annotation-workspace"><div className="annotation-preview"><img src={asset.imageUrl} alt="待标注图片" /><strong>{asset.detectedName || asset.libraryName}</strong><small>{index + 1}/{items.length}</small></div><div className="annotation-controls">{groups.map((group) => <section key={group.id}><h3>{group.name}</h3><div className="annotation-tags">{group.tags.map((tag) => <button type="button" className={ids.includes(tag.id) ? "selected" : ""} onClick={() => setIds(ids.includes(tag.id) ? ids.filter((id) => id !== tag.id) : [...ids, tag.id])} key={tag.id}>{tag.name}</button>)}</div></section>)}<label className="checkbox-line"><input type="checkbox" checked={enter} onChange={(event) => setEnter(event.target.checked)} />启用回车快速确认</label><div className="annotation-actions"><button type="button" onClick={() => setIndex((current) => Math.max(0, current - 1))} disabled={!index}>上一个</button><button type="button" className="primary-action" onClick={() => void save()}>确认并下一个</button><button type="button" onClick={() => setIndex((current) => Math.min(items.length - 1, current + 1))}>跳过</button></div></div></div> : <section className="empty-state"><p>{folder ? "该文件夹暂无待标注图片。" : "请选择文件夹开始审阅。"}</p></section>}</section>;
}
