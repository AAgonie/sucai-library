import { useEffect, useState } from "react";
import { FolderPlus, Pencil, Plus, Power, RotateCcw, Tags, Trash2 } from "lucide-react";

type Tag = { id: number; name: string; active: boolean };
type Group = { id: number; name: string; active: boolean; tags: Tag[] };

async function api<T>(url: string, init?: RequestInit) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
}

export function TagManager() {
  const [groups, setGroups] = useState<Group[]>([]);
  const [message, setMessage] = useState("");

  const load = async () => {
    setGroups((await api<{ items: Group[] }>("/api/tags/library?include_inactive=true")).items);
  };

  useEffect(() => { void load(); }, []);

  const manage = async (
    action: "category-add" | "category-rename" | "category-delete" | "tag-add" | "tag-rename" | "tag-delete",
    group?: Group,
    tag?: Tag,
  ) => {
    const name = action.includes("rename")
      ? window.prompt("请输入新名称", tag?.name || group?.name)
      : action.endsWith("add")
        ? window.prompt(action === "tag-add" ? "请输入标签名称" : "请输入分类名称")
        : "";
    if ((action.includes("add") || action.includes("rename")) && !name) return;

    const url = action === "category-add"
      ? "/api/tags/categories"
      : action === "category-rename" || action === "category-delete"
        ? `/api/tags/categories/${group!.id}`
        : action === "tag-add"
          ? "/api/tags"
          : `/api/tags/${tag!.id}`;
    const method = action.includes("delete") ? "DELETE" : action.includes("rename") ? "PATCH" : "POST";
    const body = action.startsWith("category")
      ? { name: name || group!.name }
      : action === "tag-add"
        ? { category_id: group!.id, name }
        : { category_id: group!.id, name: name || tag!.name };

    try {
      await api(url, { method, body: method === "DELETE" ? undefined : JSON.stringify(body) });
      if (action === "category-delete") {
        setGroups((current) => current.filter((item) => item.id !== group!.id));
      } else if (action === "tag-delete") {
        setGroups((current) => current.map((item) => item.id === group!.id ? { ...item, tags: item.tags.filter((itemTag) => itemTag.id !== tag!.id) } : item));
      }
      setMessage("标签库已更新。");
      void load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "操作失败");
    }
  };

  return <section className="panel tag-manager">
    <div className="tag-manager-heading">
      <div className="tag-manager-title">
        <span className="tag-manager-icon"><Tags size={22} aria-hidden="true" /></span>
        <div><h2>标签管理</h2><p>管理逐图标注与主界面筛选共用的标签库。</p></div>
      </div>
      <div className="tag-manager-summary"><span>{groups.length} 个分类</span><span>{groups.reduce((count, group) => count + group.tags.filter(tag => tag.active).length, 0)} 个启用标签</span></div>
      <button className="primary-action tag-manager-add" onClick={() => void manage("category-add")}><FolderPlus size={18} aria-hidden="true" />新增分类</button>
    </div>
    {message ? <p className="tag-manager-message" role="status">{message}</p> : null}
    <div className="tag-manager-grid">
      {groups.map(group => <article className="tag-category-card" key={group.id}>
        <header className="tag-category-header">
          <div><h3>{group.name}</h3><span>{group.tags.length} 个标签</span></div>
          <div className="tag-category-actions">
            <button title="重命名分类" aria-label={`重命名分类 ${group.name}`} onClick={() => void manage("category-rename", group)}><Pencil size={15} aria-hidden="true" /></button>
            <button className="danger" title="删除分类" aria-label={`删除分类 ${group.name}`} onClick={() => void manage("category-delete", group)}><Trash2 size={15} aria-hidden="true" /></button>
          </div>
        </header>
        <div className="tag-manager-list">{group.tags.map(tag => <div className={tag.active ? "tag-manager-item" : "tag-manager-item inactive"} key={tag.id}>
          <div className="tag-manager-name"><span>{tag.name}</span>{!tag.active ? <em>已停用</em> : null}</div>
          <div className="tag-item-actions">
            <button title={tag.active ? "停用标签" : "恢复标签"} aria-label={`${tag.active ? "停用" : "恢复"}标签 ${tag.name}`} onClick={() => void api(`/api/tags/${tag.id}?active=${!tag.active}`, { method: "PATCH", body: JSON.stringify({ category_id: group.id, name: tag.name }) }).then(load)}>{tag.active ? <Power size={14} aria-hidden="true" /> : <RotateCcw size={14} aria-hidden="true" />}</button>
            <button title="重命名标签" aria-label={`重命名标签 ${tag.name}`} onClick={() => void manage("tag-rename", group, tag)}><Pencil size={14} aria-hidden="true" /></button>
            <button className="danger" title="删除标签" aria-label={`删除标签 ${tag.name}`} onClick={() => void manage("tag-delete", group, tag)}><Trash2 size={14} aria-hidden="true" /></button>
          </div>
        </div>)}</div>
        <button className="tag-add-button" onClick={() => void manage("tag-add", group)}><Plus size={16} aria-hidden="true" />新增标签</button>
      </article>)}
    </div>
  </section>;
}
