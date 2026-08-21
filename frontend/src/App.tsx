import { ChangeEvent, MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownWideNarrow,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  FileText,
  FolderInput,
  Info,
  ImageIcon,
  Layers3,
  Network,
  Play,
  RefreshCw,
  Save,
  Search,
  SlidersHorizontal,
  Tags,
  Trash2,
  X
} from "lucide-react";
import brandLogo from "./assets/brand-logo-black.png";
import { DataSourcesPanel } from "./DataSourcesPanel";
import { TagManager } from "./TagManager";
import { DetailTagEditor } from "./DetailTagEditor";
import { AnnotationPage } from "./AnnotationPage";

type View = "config" | "home" | "ocr" | "about" | "audit" | "export" | "folderExport" | "duplicates" | "annotation";
type SortKey = "imported_desc" | "clicks_desc" | "downloads_desc" | "size_desc" | "title_asc" | "front_number_desc" | "front_number_asc";
type DisplayVersion = "raw" | "16:9" | "1:1" | "9:16";
type SourceType = "http" | "smb";

type NasConfig = {
  type: SourceType;
  host: string;
  share: string;
  base_path: string;
  username: string;
  password: string;
  recursive: boolean;
};

type LocalConfig = {
  nas: NasConfig;
  app: {
    title: string;
    default_batch_size: number;
    allowed_extensions: string[];
    derivative_output_path?: string;
  };
};

type Asset = {
  id: number;
  detectedName: string;
  libraryName: string;
  resolution: string;
  ratio: string;
  fileSizeMb: number;
  tags: string[];
  importedAt: string;
  downloads: number;
  clicks: number;
  path: string;
  imageUrl: string;
  ocrText: string;
  ocrStatus: string;
  ocrError: string;
  folder: string;
  sourcePath: string;
  displayVersion: DisplayVersion;
  displayAuthor: string;
  derivativeAvailable: boolean;
  derivativeStatus: string;
  derivativePath: string;
  derivativeAuthor: string;
  derivativeChannel: string;
  derivativeSequence: number | null;
  derivativeGeneratedAt: string;
  hidden: boolean;
};

type UserPrefs = {
  query: string;
  fuzzySearch: boolean;
  includeLibrarySearch: boolean;
  importedFrom: string;
  importedTo: string;
  folderFilter: string;
  authorFilter: string;
  channelFilter: string;
  excludePath: string;
  excludePathEnabled: boolean;
  showHidden: boolean;
  selectedTags: string[];
  tagMode: "and" | "or";
  sortKey: SortKey;
  batchSize: number;
  loadedCount: number;
  scrollY: number;
  mediaVersion: DisplayVersion;
};

type FilterOptions = { folders: string[]; authors: string[]; channels: string[] };

type AuditItem = {
  id: number;
  detectedName: string;
  libraryName: string;
  path: string;
  ocrStatus: string;
  importedAt: string;
  status: "available" | "missing" | "unauthorized" | "network_error";
  detail: string;
  checkedAt?: string;
};

type DeletedAsset = {
  deletedId: number;
  originalAssetId: number;
  detectedName: string;
  path: string;
  deletedAt: string;
  deletedBy: string;
  reason?: string;
};

type RecycledDerivative = { id: number; assetId: number; title: string; originalPath: string; deletedAt: string; deletedBy: string };

type DerivativeOrphan = { path: string; name: string; sizeBytes: number; modifiedAt: string };

type DerivativePlanItem = {
  id: number;
  title: string;
  author: string;
  channel: string;
  sourcePath: string;
  sourceResolution: string;
  sourceSizeMb: number;
  status: "ready" | "already_generated" | "unknown_resolution" | "ratio_too_far";
  warnings: string[];
  message: string;
  sequence?: number;
  existingSequence?: number | null;
  existingPath?: string;
  outputPath?: string;
  kind?: "16:9" | "1:1" | "9:16";
  targetResolution?: string;
  wasStretched?: boolean;
  nativeSize?: boolean;
  willFillGap?: boolean;
  ratioDeviationPercent?: number;
};

type DerivativePreflight = {
  outputRoot: string;
  outputRootExists: boolean;
  missingNumbers: number[];
  duplicateNumbers: number[];
  items: DerivativePlanItem[];
  summary: { total: number; ready: number; alreadyGenerated: number; skipped: number };
};

type LegacyMigrationItem = { derivativeId: number; assetId: number; sourcePath: string; targetPath: string; title: string; sequence: number; status: string; reason: string };
type LegacyMigrationPlan = { outputRoot: string; targetRoot: string; items: LegacyMigrationItem[]; summary: { total: number; ready: number; skipped: number } };

type OcrProgress = {
  active: boolean;
  current: number;
  total: number;
  succeeded: number;
  failed: number;
  autoHidden: number;
  label: string;
};
type ActionNotice = { tone: "success" | "error" | "info"; text: string };
type OcrAutoHiddenItem = { assetId: number; name: string; hiddenReason: string };

type OcrSummary = {
  total: number;
  unrecognized: number;
  done: number;
  failed: number;
  running: number;
  unrecognizedIds: number[];
  failedIds: number[];
  failedItems: { id: number; name: string; reason: string; status: string }[];
};

type ServiceStatus = {
  status: string;
  version: string;
  pid: number;
  uptimeSeconds: number;
  hostname: string;
  platform: string;
  pythonVersion: string;
  memoryTotalMb: number;
  memoryAvailableMb: number;
  diskTotalGb: number;
  diskFreeGb: number;
  cpuUsagePercent: number | null;
  ocrInstalled: boolean;
  restartSupported: boolean;
};
type BackgroundJob = {
  id: number;
  job_type: "scan" | "ocr" | string;
  status: "queued" | "running" | string;
  data_source_id: number | null;
  asset_id: number | null;
  source_name: string;
  progress_current: number;
  progress_total: number;
  current_path: string;
};
type JobStatus = {
  current: BackgroundJob | null;
  active: BackgroundJob[];
  pending: BackgroundJob[];
  queue_length: number;
  queued_by_type: Record<string, number>;
  failed: number;
  recent_error: string;
};
type JobDetail = BackgroundJob & {
  status: "queued" | "running" | "cancel_requested" | "done" | "failed" | "cancelled";
  error: string;
  message: string;
  result: {
    groups?: DuplicateGroup[];
    errors?: string[];
    message?: string;
    scanned?: number;
    autoHidden?: boolean;
    hiddenReason?: string;
    assetId?: number;
    name?: string;
    syncWarning?: string;
  };
};
type DuplicateGroup = { items: Asset[]; pairs: { leftId: number; rightId: number; visualDifferencePercent: number; textSimilarity: number }[] };
type DuplicateHistoryItem = { keepAssetId: number; hiddenCount: number; updatedAt: string };
type HiddenAsset = { id: number; name: string; path: string; hiddenAt: string; reason: string };

const APP_TITLE = "短剧广告大图素材库展示网站";
const COPYRIGHT_TEXT = "© 2026 短剧广告大图素材库展示网站。仅限授权的内部使用。";
const username = "本地用户";

const defaultNasConfig: NasConfig = {
  type: "smb",
  host: "",
  share: "",
  base_path: "\\\\FILE_SERVER\\shared\\image-library",
  username: "",
  password: "",
  recursive: false
};

const defaultPrefs: UserPrefs = {
  query: "",
  fuzzySearch: false,
  includeLibrarySearch: true,
  importedFrom: "",
  importedTo: "",
  folderFilter: "",
  authorFilter: "",
  channelFilter: "",
  excludePath: "\\\\FILE_SERVER\\shared\\image-library\\derived",
  excludePathEnabled: true,
  showHidden: false,
  selectedTags: [],
  tagMode: "and",
  sortKey: "imported_desc",
  batchSize: 50,
  loadedCount: 50,
  scrollY: 0,
  mediaVersion: "16:9"
};

function getPrefsKey(username: string) {
  return `short-drama-ad-library:prefs:${username || "guest"}`;
}

function loadPrefs(username: string): UserPrefs {
  try {
    const raw = localStorage.getItem(getPrefsKey(username));
    if (!raw) return defaultPrefs;
    const rawPrefs = JSON.parse(raw) as Partial<Omit<UserPrefs, "mediaVersion">> & { mediaVersion?: string };
    const saved = { ...defaultPrefs, ...rawPrefs } as UserPrefs;
    const storedVersion = rawPrefs.mediaVersion;
    return {
      ...saved,
      mediaVersion: storedVersion === "1280" ? "16:9" : storedVersion === "original" ? "raw" : (storedVersion as DisplayVersion | undefined) || defaultPrefs.mediaVersion,
      tagMode: saved.tagMode === "or" ? "or" : "and"
    };
  } catch {
    return defaultPrefs;
  }
}

function apiUrl(path: string) {
  return path.startsWith("http") ? path : path;
}

function CopyrightNotice() {
  return <p className="copyright-notice">{COPYRIGHT_TEXT}</p>;
}

async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    ...options
  });
  if (!response.ok) {
    const rawError = await response.text();
    try {
      const payload = JSON.parse(rawError) as { detail?: string };
      throw new Error(payload.detail || rawError || `请求失败（HTTP ${response.status}）`);
    } catch (error) {
      if (error instanceof SyntaxError) throw new Error(rawError || `请求失败（HTTP ${response.status}）`);
      throw error;
    }
  }
  return response.json() as Promise<T>;
}

function formatDuration(totalSeconds: number) {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

function formatCardDate(value: string) {
  const match = value.match(/(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[1].slice(2)}/${match[2]}/${match[3]}` : "未知日期";
}

function formatDateTime(value: string) {
  const match = value.match(/(\d{4}-\d{2}-\d{2})[T\s](\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : value || "未知时间";
}

function localDateValue(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDays(date: Date, amount: number) {
  const next = new Date(date);
  next.setDate(next.getDate() + amount);
  return next;
}

function startOfWeek(date: Date) {
  const next = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  next.setDate(next.getDate() - ((next.getDay() + 6) % 7));
  return next;
}

function assetAspectClass(asset: Asset) {
  const ratio = asset.displayVersion === "raw" ? asset.ratio.replace(/\s+/g, "") : asset.displayVersion;
  if (ratio === "1:1") return "aspect-square";
  if (ratio === "9:16") return "aspect-portrait";
  if (asset.displayVersion === "raw") {
    const parts = ratio.split(":").map(Number);
    const value = parts.length === 2 && parts[1] ? parts[0] / parts[1] : 0;
    if (Math.abs(value - 1) <= 0.05) return "aspect-square";
    if (Math.abs(value - 9 / 16) <= 0.05) return "aspect-portrait";
  }
  return "aspect-wide";
}

function downloadFilename(contentDisposition: string | null, fallback: string) {
  const safeFallback = fallback.split(/[\\/]/).pop() || "download";
  if (!contentDisposition) return safeFallback;
  const encoded = contentDisposition.match(/filename\*\s*=\s*([^;]+)/i)?.[1]?.trim().replace(/^"|"$/g, "").replace(/^UTF-8''/i, "");
  if (encoded) {
    try {
      return (decodeURIComponent(encoded).split(/[\\/]/).pop() || safeFallback).trim();
    } catch {
      // Fall through to the plain filename parameter when RFC 5987 decoding fails.
    }
  }
  const plain = contentDisposition.match(/filename\s*=\s*(?:"([^"]+)"|([^;]+))/i);
  return ((plain?.[1] || plain?.[2] || safeFallback).trim().split(/[\\/]/).pop() || safeFallback);
}

function DateRangePicker({ from, to, onChange }: { from: string; to: string; onChange: (from: string, to: string) => void }) {
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(() => from ? new Date(`${from}T00:00:00`) : new Date());
  const today = new Date();
  const presets = [
    ["今天", () => [today, today]], ["昨天", () => { const day = addDays(today, -1); return [day, day]; }],
    ["3 天", () => [addDays(today, -2), today]], ["7 天", () => [addDays(today, -6), today]],
    ["30 天", () => [addDays(today, -29), today]], ["90 天", () => [addDays(today, -89), today]],
    ["上周", () => { const end = addDays(startOfWeek(today), -1); return [addDays(end, -6), end]; }],
    ["本周", () => [startOfWeek(today), today]]
  ] as const;
  const months = [0, 1];

  function chooseDay(value: string) {
    if (!from || to) {
      onChange(value, "");
      return;
    }
    if (value < from) onChange(value, from);
    else onChange(from, value);
  }

  function renderMonth(offset: number) {
    const monthDate = new Date(cursor.getFullYear(), cursor.getMonth() + offset, 1);
    const firstDay = (monthDate.getDay() + 6) % 7;
    const days = new Date(monthDate.getFullYear(), monthDate.getMonth() + 1, 0).getDate();
    const cells = Array.from({ length: firstDay + days }, (_, index) => index < firstDay ? "" : localDateValue(new Date(monthDate.getFullYear(), monthDate.getMonth(), index - firstDay + 1)));
    return <section className={`calendar-month month-${offset}`} key={`${monthDate.getFullYear()}-${monthDate.getMonth()}`}><h4>{monthDate.toLocaleDateString("zh-CN", { year: "numeric", month: "long" })}</h4><div className="calendar-week">{["一", "二", "三", "四", "五", "六", "日"].map((day) => <span key={day}>{day}</span>)}</div><div className="calendar-days">{cells.map((value, index) => value ? <button type="button" key={value} className={`${value === from || value === to ? "edge" : ""} ${from && to && value > from && value < to ? "in-range" : ""} ${value === localDateValue(today) ? "today" : ""}`} onClick={() => chooseDay(value)}>{Number(value.slice(-2))}</button> : <span key={`blank-${index}`} />)}</div></section>;
  }

  function applyPreset(range: () => Date[]) {
    const [start, end] = range();
    onChange(localDateValue(start), localDateValue(end));
    setOpen(false);
  }

  return <div className="date-range-picker"><div className="date-quick-actions" aria-label="入库日期快捷筛选">{presets.map(([label, range]) => { const [start, end] = range(); const active = from === localDateValue(start) && to === localDateValue(end); return <button type="button" key={label} className={active ? "active" : ""} onClick={() => applyPreset(range)}>{label}</button>; })}<button type="button" className={!from && !to ? "active" : ""} onClick={() => { onChange("", ""); setOpen(false); }}>全周期</button></div><div className="date-range-trigger"><button type="button" onClick={() => setOpen((current) => !current)}>{from && to ? `${from.slice(5).replace("-", "/")} - ${to.slice(5).replace("-", "/")}` : from ? `${from.slice(5).replace("-", "/")} 起` : "自定义入库日期"}</button>{(from || to) ? <button type="button" className="clear-date" aria-label="清除日期筛选" onClick={() => onChange("", "")}>×</button> : null}</div>{open ? <div className="date-range-popover"><div className="calendar-toolbar"><button type="button" aria-label="上个月" onClick={() => setCursor((value) => new Date(value.getFullYear(), value.getMonth() - 1, 1))}>‹</button><span>自定义范围</span><button type="button" aria-label="下个月" onClick={() => setCursor((value) => new Date(value.getFullYear(), value.getMonth() + 1, 1))}>›</button></div><div className="calendar-months">{months.map(renderMonth)}</div><p>{from && !to ? "请选择结束日期" : "范围两端均包含"}</p></div> : null}</div>;
}

export function App() {
  const [view, setView] = useState<View>("home");
  const [nasConfig, setNasConfig] = useState<NasConfig>(defaultNasConfig);
  const [appConfig, setAppConfig] = useState<LocalConfig["app"]>({ title: APP_TITLE, default_batch_size: 50, allowed_extensions: [".jpg", ".jpeg", ".png", ".webp"], derivative_output_path: "" });
  const [prefs, setPrefs] = useState<UserPrefs>(() => loadPrefs(username));
  const [status, setStatus] = useState("本地图片库已就绪，等待你手动测试连接或扫描。");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [isLoadingAssets, setIsLoadingAssets] = useState(false);
  const [total, setTotal] = useState(0);
  const [allTags, setAllTags] = useState<string[]>([]);
  const [allFolders, setAllFolders] = useState<string[]>([]);
  const [filterOptions, setFilterOptions] = useState<FilterOptions>({ folders: [], authors: [], channels: [] });
  const [selectedAsset, setSelectedAsset] = useState<Asset | null>(null);
  const [isEditingDetectedName, setIsEditingDetectedName] = useState(false);
  const [detectedNameDraft, setDetectedNameDraft] = useState("");
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [ocrProgress, setOcrProgress] = useState<OcrProgress | null>(null);
  const [ocrSummary, setOcrSummary] = useState<OcrSummary | null>(null);
  const [ocrErrors, setOcrErrors] = useState<string[]>([]);
  const [ocrAutoHiddenReport, setOcrAutoHiddenReport] = useState<OcrAutoHiddenItem[]>([]);
  const [ocrFolderFilter, setOcrFolderFilter] = useState("");
  const [ocrRatioFilter, setOcrRatioFilter] = useState("");
  const [serviceStatus, setServiceStatus] = useState<ServiceStatus | null>(null);
  const [serviceRestarting, setServiceRestarting] = useState(false);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [ignorePhrases, setIgnorePhrases] = useState<string[]>([]);
  const [newIgnorePhrase, setNewIgnorePhrase] = useState("");
  const [editingIgnoreOriginal, setEditingIgnoreOriginal] = useState("");
  const [editingIgnorePhrase, setEditingIgnorePhrase] = useState("");
  const [auditItems, setAuditItems] = useState<AuditItem[]>([]);
  const [auditSelectedIds, setAuditSelectedIds] = useState<number[]>([]);
  const [deletedAssets, setDeletedAssets] = useState<DeletedAsset[]>([]);
  const [deletedSelectedIds, setDeletedSelectedIds] = useState<number[]>([]);
  const [recycledDerivatives, setRecycledDerivatives] = useState<RecycledDerivative[]>([]);
  const [derivativeOrphans, setDerivativeOrphans] = useState<DerivativeOrphan[]>([]);
  const [selectedDerivativeOrphanPaths, setSelectedDerivativeOrphanPaths] = useState<string[]>([]);
  const [derivativeOrphanRoot, setDerivativeOrphanRoot] = useState("");
  const [derivativePlan, setDerivativePlan] = useState<DerivativePreflight | null>(null);
  const [derivativeErrors, setDerivativeErrors] = useState<string[]>([]);
  const [derivativeAuthor, setDerivativeAuthor] = useState("");
  const [derivativeChannel, setDerivativeChannel] = useState("");
  const [legacyMigrationPlan, setLegacyMigrationPlan] = useState<LegacyMigrationPlan | null>(null);
  const [folderDerivativePlan, setFolderDerivativePlan] = useState<DerivativePreflight | null>(null);
  const [folderExportPath, setFolderExportPath] = useState("");
  const [folderDerivativeAuthor, setFolderDerivativeAuthor] = useState("");
  const [folderDerivativeChannel, setFolderDerivativeChannel] = useState("");
  const [folderDerivativeErrors, setFolderDerivativeErrors] = useState<string[]>([]);
  const [duplicateGroups, setDuplicateGroups] = useState<DuplicateGroup[]>([]);
  const [duplicateErrors, setDuplicateErrors] = useState<string[]>([]);
  const [duplicateFolderFilter, setDuplicateFolderFilter] = useState("");
  const [duplicateRatioFilter, setDuplicateRatioFilter] = useState("");
  const [duplicateJobId, setDuplicateJobId] = useState<number | null>(null);
  const [duplicateJob, setDuplicateJob] = useState<JobDetail | null>(null);
  const [duplicateSubmitting, setDuplicateSubmitting] = useState(false);
  const [duplicateHistory, setDuplicateHistory] = useState<DuplicateHistoryItem[]>([]);
  const [hiddenAssets, setHiddenAssets] = useState<HiddenAsset[]>([]);
  const [savedDerivativeAuthors, setSavedDerivativeAuthors] = useState<string[]>([]);
  const [savedDerivativeChannels, setSavedDerivativeChannels] = useState<string[]>([]);
  const [actionNotice, setActionNotice] = useState<ActionNotice | null>(null);
  const scrollSaveTimer = useRef<number | null>(null);
  const actionNoticeTimer = useRef<number | null>(null);
  const selectedAssetRef = useRef<Asset | null>(null);
  const savedScrollYRef = useRef(0);

  useEffect(() => {
    selectedAssetRef.current = selectedAsset;
  }, [selectedAsset]);

  const displayName = username;
  const homeFolders = useMemo(() => Array.from(new Set([...filterOptions.folders, prefs.folderFilter].filter(Boolean))), [filterOptions.folders, prefs.folderFilter]);
  const homeAuthors = useMemo(() => Array.from(new Set([...filterOptions.authors, prefs.authorFilter].filter(Boolean))), [filterOptions.authors, prefs.authorFilter]);
  const homeChannels = useMemo(() => Array.from(new Set([...filterOptions.channels, prefs.channelFilter].filter(Boolean))), [filterOptions.channels, prefs.channelFilter]);

  useEffect(() => {
    requestJson<LocalConfig>("/api/config")
      .then((config) => {
        setNasConfig(config.nas || defaultNasConfig);
        setAppConfig(config.app || { title: APP_TITLE, default_batch_size: 50, allowed_extensions: [".jpg", ".jpeg", ".png", ".webp"], derivative_output_path: "" });
        setPrefs((current) => ({ ...current, batchSize: config.app?.default_batch_size || current.batchSize, loadedCount: config.app?.default_batch_size || current.loadedCount }));
      })
      .catch(() => setStatus("读取配置失败，请确认后端服务正在运行。"));
  }, []);

  useEffect(() => {
    localStorage.setItem(getPrefsKey(username), JSON.stringify(prefs));
  }, [prefs]);

  useEffect(() => {
    function saveScroll() {
      if (scrollSaveTimer.current) window.clearTimeout(scrollSaveTimer.current);
      scrollSaveTimer.current = window.setTimeout(() => {
        setPrefs((current) => ({ ...current, scrollY: window.scrollY }));
      }, 160);
    }
    window.addEventListener("scroll", saveScroll, { passive: true });
    return () => window.removeEventListener("scroll", saveScroll);
  }, []);

  useEffect(() => {
    if (view === "home") {
      void loadAssets();
      void loadTags();
      void loadFolders();
      void loadFilterOptions();
    }
    if (view === "ocr") {
      void loadOcrSummary();
      void loadIgnorePhrases();
      void loadFolders();
    }
    if (view === "audit") {
      void loadLatestAudit();
      void loadDeletedAssets();
      void loadRecycledDerivatives();
      void loadHiddenAssets();
    }
    if (view === "export" || view === "folderExport") {
      void Promise.all([loadDerivativeAuthors(), loadDerivativeChannels(), loadFolders()]);
    }
    if (view === "duplicates") { void loadDuplicateHistory(); void loadFolders(); }
  }, [view, prefs.query, prefs.fuzzySearch, prefs.includeLibrarySearch, prefs.importedFrom, prefs.importedTo, prefs.folderFilter, prefs.authorFilter, prefs.channelFilter, prefs.excludePath, prefs.excludePathEnabled, prefs.showHidden, prefs.selectedTags, prefs.tagMode, prefs.sortKey, prefs.loadedCount, prefs.mediaVersion, ocrFolderFilter, ocrRatioFilter]);

  useEffect(() => {
    if (view !== "about") return;
    void loadServiceStatus();
    void loadJobStatus();
    const serviceTimer = window.setInterval(() => void loadServiceStatus(), 15000);
    const jobTimer = window.setInterval(() => void loadJobStatus(), 5000);
    return () => { window.clearInterval(serviceTimer); window.clearInterval(jobTimer); };
  }, [view]);

  useEffect(() => {
    if (duplicateJobId === null) return;
    let disposed = false;
    let timer: number | null = null;

    async function pollDuplicateJob() {
      try {
        const job = await requestJson<JobDetail>(`/api/jobs/${duplicateJobId}`);
        if (disposed) return;
        setDuplicateJob(job);
        if (job.status === "done") {
          const result = job.result || {};
          setDuplicateGroups(result.groups || []);
          setDuplicateErrors(result.errors || []);
          const message = result.message || job.message || "重复审查完成。";
          setStatus(message);
          notifyAction("success", `${message}${result.errors?.length ? ` ${result.errors.length} 个文件无法检测。` : ""}`);
          setDuplicateJobId(null);
          void loadJobStatus();
          return;
        }
        if (job.status === "failed" || job.status === "cancelled") {
          const message = job.status === "cancelled" ? (job.message || "重复审查已停止。") : `重复审查失败：${job.error || job.message || "未知错误"}`;
          setStatus(message);
          notifyAction(job.status === "cancelled" ? "info" : "error", message);
          setDuplicateJobId(null);
          void loadJobStatus();
          return;
        }
        timer = window.setTimeout(() => void pollDuplicateJob(), job.status === "queued" ? 2500 : 1200);
      } catch (error) {
        if (disposed) return;
        setStatus(`读取重复审查进度失败，将自动重试：${String(error)}`);
        timer = window.setTimeout(() => void pollDuplicateJob(), 3000);
      }
    }

    void pollDuplicateJob();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [duplicateJobId]);

  function patchPrefs(patch: Partial<UserPrefs>) {
    setPrefs((current) => ({ ...current, ...patch }));
  }

  function notifyAction(tone: ActionNotice["tone"], text: string) {
    setActionNotice({ tone, text });
    if (actionNoticeTimer.current) window.clearTimeout(actionNoticeTimer.current);
    actionNoticeTimer.current = window.setTimeout(() => setActionNotice(null), 5000);
  }

  function captureOcrAutoHidden(job: JobDetail, fallbackAssetId?: number, fallbackName?: string) {
    const result = job.result || {};
    const assetId = result.assetId || fallbackAssetId;
    if (!result.autoHidden || !assetId) return null;
    const item: OcrAutoHiddenItem = {
      assetId,
      name: result.name || fallbackName || `图片 #${assetId}`,
      hiddenReason: `${result.hiddenReason || "OCR 命中自动隐藏规则"}${result.syncWarning ? `；${result.syncWarning}` : ""}`
    };
    setOcrAutoHiddenReport((current) => [item, ...current.filter((existing) => existing.assetId !== item.assetId)]);
    setAssets((current) => current.filter((asset) => asset.id !== item.assetId));
    setSelectedIds((current) => current.filter((id) => id !== item.assetId));
    if (selectedAssetRef.current?.id === item.assetId) closeDetail();
    void loadHiddenAssets();
    return item;
  }

  async function loadAssets() {
    setIsLoadingAssets(true);
    try {
      const requestedCount = Math.max(prefs.batchSize, prefs.loadedCount);
      const combined: Asset[] = [];
      let offset = 0;
      let totalCount = 0;
      let message = "已从数据库读取图片。";
      while (offset < requestedCount) {
        const params = new URLSearchParams({
          q: prefs.query,
          sort: prefs.sortKey,
          fuzzy: String(prefs.fuzzySearch),
          include_library: String(prefs.includeLibrarySearch),
          imported_from: prefs.importedFrom,
          imported_to: prefs.importedTo,
          folder: prefs.folderFilter,
          author: prefs.authorFilter,
          channel: prefs.channelFilter,
          exclude_path: prefs.excludePathEnabled ? prefs.excludePath.trim() : "",
          include_hidden: String(prefs.showHidden),
          version: prefs.mediaVersion,
          limit: String(Math.min(200, requestedCount - offset)),
          offset: String(offset)
        });
        prefs.selectedTags.forEach((tag) => params.append("tags", tag));
        params.set("tag_mode", prefs.tagMode);
        const data = await requestJson<{ items: Asset[]; total: number; message: string }>(`/api/assets?${params.toString()}`);
        combined.push(...data.items);
        totalCount = data.total;
        message = data.message;
        offset += data.items.length;
        if (!data.items.length || offset >= totalCount) break;
      }
      setAssets(combined);
      setTotal(totalCount);
      setStatus(message);
    } catch (error) {
      setStatus(`读取图片失败：${String(error)}`);
    } finally {
      setIsLoadingAssets(false);
    }
  }

  async function loadTags() {
    try {
      const data = await requestJson<{ items: string[] }>("/api/tags");
      setAllTags(data.items);
    } catch {
      setAllTags([]);
    }
  }

  async function loadFolders() {
    try {
      const data = await requestJson<{ items: string[] }>("/api/folders");
      setAllFolders(data.items);
    } catch {
      setAllFolders([]);
    }
  }

  async function loadFilterOptions() {
    try {
      const data = await requestJson<FilterOptions>(`/api/assets/filter-options?version=${encodeURIComponent(prefs.mediaVersion)}`);
      setFilterOptions(data);
    } catch {
      setFilterOptions({ folders: [], authors: [], channels: [] });
    }
  }

  async function loadDerivativeAuthors() {
    try {
      const data = await requestJson<{ items: string[] }>("/api/derivatives/authors");
      setSavedDerivativeAuthors(data.items);
    } catch {
      setSavedDerivativeAuthors([]);
    }
  }

  async function loadDerivativeChannels() {
    try {
      const data = await requestJson<{ items: string[] }>("/api/derivatives/channels");
      setSavedDerivativeChannels(data.items);
    } catch {
      setSavedDerivativeChannels([]);
    }
  }

  async function loadLatestAudit() {
    try {
      const data = await requestJson<{ items: AuditItem[] }>("/api/database-review/latest");
      setAuditItems(data.items);
      setAuditSelectedIds((current) => current.filter((id) => data.items.some((item) => item.id === id)));
    } catch (error) {
      setStatus(`读取数据库审查记录失败：${String(error)}`);
    }
  }

  async function loadDeletedAssets() {
    try {
      const data = await requestJson<{ items: DeletedAsset[] }>("/api/database-review/trash");
      setDeletedAssets(data.items);
      setDeletedSelectedIds((current) => current.filter((id) => data.items.some((item) => item.deletedId === id)));
    } catch (error) {
      setStatus(`读取数据库回收站失败：${String(error)}`);
    }
  }

  async function loadRecycledDerivatives() {
    try {
      const data = await requestJson<{ items: RecycledDerivative[] }>("/api/recycle/derivatives");
      setRecycledDerivatives(data.items);
    } catch {
      setRecycledDerivatives([]);
    }
  }

  async function loadDuplicateHistory() {
    try {
      const data = await requestJson<{ items: DuplicateHistoryItem[] }>("/api/duplicates/history");
      setDuplicateHistory(data.items);
    } catch {
      setDuplicateHistory([]);
    }
  }

  async function loadHiddenAssets() {
    try {
      const data = await requestJson<{ items: HiddenAsset[] }>("/api/assets/hidden");
      setHiddenAssets(data.items);
    } catch {
      setHiddenAssets([]);
    }
  }

  async function findDerivativeOrphans() {
    setBusy(true);
    try {
      const data = await requestJson<{ outputRoot: string; items: DerivativeOrphan[] }>("/api/database-review/derivative-orphans");
      setDerivativeOrphanRoot(data.outputRoot);
      setDerivativeOrphans(data.items);
      setSelectedDerivativeOrphanPaths(data.items.map((item) => item.path));
      setStatus(data.items.length ? `检测到 ${data.items.length} 个未登记的 1280 孤儿文件，已默认勾选。` : "未发现未登记的 1280 孤儿文件。");
    } catch (error) {
      setStatus(`检测 1280 孤儿文件失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function toggleDerivativeOrphanSelection(path: string) {
    setSelectedDerivativeOrphanPaths((current) => current.includes(path) ? current.filter((item) => item !== path) : [...current, path]);
  }

  async function deleteSelectedDerivativeOrphans() {
    if (!selectedDerivativeOrphanPaths.length) {
      setStatus("请先选择要清理的 1280 孤儿文件。");
      return;
    }
    if (!window.confirm(`确认将 ${selectedDerivativeOrphanPaths.length} 个未登记的 1280 文件移入 NAS 回收站吗？不会删除原图，也不会永久删除文件。`)) return;
    setBusy(true);
    try {
      const result = await requestJson<{ moved: number; skipped: { reason: string }[]; message: string }>("/api/database-review/derivative-orphans/delete", {
        method: "POST",
        body: JSON.stringify({ paths: selectedDerivativeOrphanPaths, username: displayName })
      });
      setSelectedDerivativeOrphanPaths([]);
      setDerivativeOrphans((current) => current.filter((item) => !selectedDerivativeOrphanPaths.includes(item.path)));
      setStatus(`${result.message}${result.skipped.length ? ` 跳过 ${result.skipped.length} 项。` : ""}`);
      notifyAction("success", `${result.message}${result.skipped.length ? ` 跳过 ${result.skipped.length} 项。` : ""}`);
      void Promise.all([findDerivativeOrphans(), loadRecycledDerivatives(), loadAssets()]);
    } catch (error) {
      setStatus(`清理 1280 孤儿文件失败：${String(error)}`);
      notifyAction("error", `清理 1280 孤儿文件失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function runSourceAudit() {
    setBusy(true);
    try {
      const data = await requestJson<{ items: AuditItem[]; summary: { total: number; missing: number; available: number; unauthorized: number; networkError: number } }>("/api/database-review/audit", { method: "POST", body: JSON.stringify({ asset_ids: [] }) });
      setAuditItems(data.items);
      setAuditSelectedIds(data.items.filter((item) => item.status === "missing").map((item) => item.id));
      setStatus(`审查完成：共 ${data.summary.total} 项，文件不存在 ${data.summary.missing} 项；仅自动勾选不存在项目。`);
    } catch (error) {
      setStatus(`检测失效来源失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function toggleAuditSelection(assetId: number) {
    setAuditSelectedIds((current) => current.includes(assetId) ? current.filter((id) => id !== assetId) : [...current, assetId]);
  }

  async function removeAuditedAssets() {
    const selected = auditItems.filter((item) => auditSelectedIds.includes(item.id) && item.status === "missing");
    if (!selected.length) {
      setStatus("请先选择“文件不存在”的项目。权限或网络异常项目不会被删除。");
      return;
    }
    if (!window.confirm(`确认仅从数据库移除 ${selected.length} 项吗？NAS 文件不会被删除。`)) return;
    setBusy(true);
    try {
      const result = await requestJson<{ removed: number; message: string }>("/api/database-review/remove", { method: "POST", body: JSON.stringify({ asset_ids: selected.map((item) => item.id), username: displayName }) });
      setAuditSelectedIds([]);
      setAuditItems((current) => current.filter((item) => !selected.some((selectedItem) => selectedItem.id === item.id)));
      setAssets((current) => current.filter((item) => !selected.some((selectedItem) => selectedItem.id === item.id)));
      setStatus(result.message);
      notifyAction("success", result.message);
      void Promise.all([loadLatestAudit(), loadDeletedAssets(), loadAssets(), loadTags(), loadFolders()]);
    } catch (error) {
      setStatus(`数据库移除失败：${String(error)}`);
      notifyAction("error", `数据库移除失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function toggleDeletedSelection(deletedId: number) {
    setDeletedSelectedIds((current) => current.includes(deletedId) ? current.filter((id) => id !== deletedId) : [...current, deletedId]);
  }

  async function restoreDeletedItems() {
    if (!deletedSelectedIds.length) {
      setStatus("请先选择要恢复的数据库记录。");
      return;
    }
    setBusy(true);
    try {
      const result = await requestJson<{ restored: number; skipped: { reason: string }[] }>("/api/database-review/restore", { method: "POST", body: JSON.stringify({ deleted_ids: deletedSelectedIds, username: displayName }) });
      setDeletedSelectedIds([]);
      setStatus(`已恢复 ${result.restored} 项${result.skipped.length ? `，跳过 ${result.skipped.length} 项` : ""}。`);
      await Promise.all([loadLatestAudit(), loadDeletedAssets(), loadAssets(), loadTags(), loadFolders()]);
    } catch (error) {
      setStatus(`恢复数据库记录失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function restoreRecycledDerivative(item: RecycledDerivative) {
    if (!window.confirm(`确认恢复“${item.title}”的 1280×720 文件到原输出路径吗？`)) return;
    setBusy(true);
    try {
      const result = await requestJson<{ message: string }>(`/api/recycle/derivatives/${item.id}/restore`, { method: "POST", body: "{}" });
      setStatus(result.message);
      await Promise.all([loadRecycledDerivatives(), loadAssets()]);
    } catch (error) {
      setStatus(`恢复 1280 文件失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function preflightDerivatives() {
    if (!selectedIds.length) {
      setStatus("请先在图片库选择要导出的图片。");
      return;
    }
    setBusy(true);
    setDerivativeErrors([]);
    try {
      const plan = await requestJson<DerivativePreflight>("/api/derivatives/preflight", { method: "POST", body: JSON.stringify({ asset_ids: selectedIds, channel: derivativeChannel.trim() }) });
      setDerivativePlan(plan);
      setView("export");
      setStatus(`导出预检完成：可生成 ${plan.summary.ready} 项，跳过 ${plan.summary.skipped} 项。`);
    } catch (error) {
      setStatus(`导出预检失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function generateDerivatives() {
    const channel = derivativeChannel.trim();
    if (!channel) {
      setStatus("请先填写获取渠道后再生成派生图。 ");
      return;
    }
    setBusy(true);
    setDerivativeErrors([]);
    try {
      const author = derivativeAuthor.trim();
      const currentPlan = await requestJson<DerivativePreflight>("/api/derivatives/preflight", { method: "POST", body: JSON.stringify({ asset_ids: selectedIds, author, channel }) });
      const readyIds = currentPlan.items.filter((item) => item.status === "ready").map((item) => item.id);
      if (!readyIds.length) {
        setDerivativePlan(currentPlan);
        setStatus("预检中没有可生成的项目。比例偏差超过 10% 的图片需要人工处理。 ");
        return;
      }
      if (!window.confirm(`确认生成 ${readyIds.length} 个自动匹配比例的 JPG 派生图到 ${currentPlan.outputRoot} 吗？原图不会改动。`)) return;
      const result = await requestJson<{ generated: number; failed: number; items: { id: number; status: string; message?: string; warnings?: string[] }[]; message: string }>("/api/derivatives/generate", { method: "POST", body: JSON.stringify({ asset_ids: readyIds, author, channel, username: displayName }) });
      const errors = result.items.filter((item) => item.status === "failed").map((item) => `图片 ${item.id}：${item.message || "生成失败"}`);
      setDerivativeErrors(errors);
      setStatus(`派生图生成完成：成功 ${result.generated} 项，失败 ${result.failed} 项。`);
      const plan = await requestJson<DerivativePreflight>("/api/derivatives/preflight", { method: "POST", body: JSON.stringify({ asset_ids: selectedIds, author, channel }) });
      setDerivativePlan(plan);
      await Promise.all([loadAssets(), loadDerivativeAuthors()]);
    } catch (error) {
      setStatus(`生成派生图失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function preflightFolderDerivatives() {
    if (!folderExportPath) {
      setStatus("请先选择要批量导出的文件夹。 ");
      return;
    }
    setBusy(true);
    setFolderDerivativeErrors([]);
    try {
      const plan = await requestJson<DerivativePreflight>("/api/derivatives/folder-preflight", { method: "POST", body: JSON.stringify({ folder: folderExportPath, author: folderDerivativeAuthor.trim(), channel: folderDerivativeChannel.trim() }) });
      setFolderDerivativePlan(plan);
      setStatus(`文件夹预检完成：待导出 ${plan.summary.ready} 项，已导出 ${plan.summary.alreadyGenerated} 项。`);
    } catch (error) {
      setStatus(`文件夹导出预检失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function generateFolderDerivatives() {
    if (!folderExportPath) {
      setStatus("请先选择要批量导出的文件夹。 ");
      return;
    }
    const channel = folderDerivativeChannel.trim();
    if (!channel) {
      setStatus("请先填写获取渠道后再导出。 ");
      return;
    }
    setBusy(true);
    setFolderDerivativeErrors([]);
    try {
      const author = folderDerivativeAuthor.trim();
      const currentPlan = await requestJson<DerivativePreflight>("/api/derivatives/folder-preflight", { method: "POST", body: JSON.stringify({ folder: folderExportPath, author, channel }) });
      const readyIds = currentPlan.items.filter((item) => item.status === "ready").map((item) => item.id);
      if (!readyIds.length) {
        setFolderDerivativePlan(currentPlan);
        setStatus("该文件夹没有待导出的图片。 ");
        return;
      }
      if (!window.confirm(`确认导出 ${readyIds.length} 项到 ${currentPlan.outputRoot} 吗？系统会自动选择唯一比例并统一输出 JPG；原图不会改动。`)) return;
      const result = await requestJson<{ generated: number; failed: number; items: { id: number; status: string; message?: string; warnings?: string[] }[] }>("/api/derivatives/generate", { method: "POST", body: JSON.stringify({ asset_ids: readyIds, author, channel, username: displayName }) });
      setFolderDerivativeErrors(result.items.filter((item) => item.status === "failed").map((item) => `图片 ${item.id}：${item.message || "生成失败"}`));
      setStatus(`文件夹导出完成：成功 ${result.generated} 项，失败 ${result.failed} 项。`);
      const plan = await requestJson<DerivativePreflight>("/api/derivatives/folder-preflight", { method: "POST", body: JSON.stringify({ folder: folderExportPath, author, channel }) });
      setFolderDerivativePlan(plan);
      await Promise.all([loadAssets(), loadDerivativeAuthors()]);
    } catch (error) {
      setStatus(`文件夹导出失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function updateGeneratedDerivativeMetadata() {
    const author = derivativeAuthor.trim();
    const channel = derivativeChannel.trim();
    const exportedIds = derivativePlan?.items.filter((item) => item.status === "already_generated").map((item) => item.id) || [];
    if (!author && !channel) {
      setStatus("请至少填写作者或获取渠道，再更新已导出文件。 ");
      return;
    }
    if (!exportedIds.length) {
      setStatus("当前预检中没有已导出的派生图。 ");
      return;
    }
    const fields = [author ? `作者“${author}”` : "", channel ? `获取渠道“${channel}”` : ""].filter(Boolean).join("、");
    if (!window.confirm(`确认更新 ${exportedIds.length} 个已导出文件的${fields}吗？未填写的字段会保持原值；会同步更新所有比例的派生图。`)) return;
    setBusy(true);
    setDerivativeErrors([]);
    try {
      const result = await requestJson<{ updated: number; failed: number; skipped: number; items: { id: number; status: string; message: string }[] }>("/api/derivatives/update-author", { method: "POST", body: JSON.stringify({ asset_ids: exportedIds, author, channel, username: displayName }) });
      const errors = result.items.filter((item) => item.status !== "updated").map((item) => `图片 ${item.id}：${item.message}`);
      setDerivativeErrors(errors);
      setStatus(`已导出文件信息更新完成：成功 ${result.updated} 项，失败 ${result.failed} 项。`);
      const plan = await requestJson<DerivativePreflight>("/api/derivatives/preflight", { method: "POST", body: JSON.stringify({ asset_ids: selectedIds, author, channel }) });
      setDerivativePlan(plan);
      await Promise.all([loadAssets(), loadDerivativeAuthors(), loadDerivativeChannels()]);
    } catch (error) {
      setStatus(`更新已导出文件信息失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function preflightLegacy1280Migration() {
    setBusy(true);
    try {
      const plan = await requestJson<LegacyMigrationPlan>("/api/derivatives/legacy-1280/preflight");
      setLegacyMigrationPlan(plan);
      setStatus(`旧 1280 文件预检完成：可迁移 ${plan.summary.ready} 项，跳过 ${plan.summary.skipped} 项。`);
    } catch (error) {
      setStatus(`旧 1280 文件预检失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function migrateLegacy1280Files() {
    const ready = legacyMigrationPlan?.items.filter((item) => item.status === "ready") || [];
    if (!ready.length) {
      setStatus("没有可迁移的旧 1280 文件。 ");
      return;
    }
    if (!window.confirm(`确认将 ${ready.length} 个旧 1280 文件移入 DataBase\\16x9 吗？每项均会写入日志，可按日志回滚。`)) return;
    setBusy(true);
    try {
      const result = await requestJson<{ migrated: number; skipped: number; failed: number }>("/api/derivatives/legacy-1280/migrate", { method: "POST", body: JSON.stringify({ confirm: true, migration_ids: ready.map((item) => item.derivativeId) }) });
      setStatus(`旧 1280 文件迁移完成：成功 ${result.migrated} 项，跳过 ${result.skipped} 项，失败 ${result.failed} 项。`);
      await Promise.all([preflightLegacy1280Migration(), loadAssets()]);
    } catch (error) {
      setStatus(`旧 1280 文件迁移失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function loadOcrSummary() {
    try {
      const params = new URLSearchParams({ exclude_path: prefs.excludePathEnabled ? prefs.excludePath.trim() : "", folder: ocrFolderFilter, ratio: ocrRatioFilter });
      const data = await requestJson<OcrSummary>(`/api/ocr/summary?${params.toString()}`);
      setOcrSummary(data);
      setOcrErrors(data.failedItems.map((item) => `${item.name}：${item.reason}`));
      setStatus(`识别概览：共 ${data.total} 张，未识别 ${data.unrecognized} 张，已识别 ${data.done} 张。`);
      return data;
    } catch (error) {
      setStatus(`读取识别概览失败：${String(error)}`);
      return null;
    }
  }

  async function loadServiceStatus() {
    try {
      const data = await requestJson<ServiceStatus>("/api/system/status");
      setServiceStatus(data);
      if (serviceRestarting) setServiceRestarting(false);
    } catch {
      setServiceStatus(null);
    }
  }

  async function loadJobStatus() {
    try {
      setJobStatus(await requestJson<JobStatus>("/api/jobs/status"));
    } catch {
      setJobStatus(null);
    }
  }

  async function cancelBackgroundJob(job: BackgroundJob) {
    try {
      const result = await requestJson<{ status: string; message: string }>(`/api/jobs/${job.id}/cancel`, { method: "POST", body: "{}" });
      setStatus(result.message);
      await loadJobStatus();
    } catch (error) {
      setStatus(`停止任务失败：${String(error)}`);
    }
  }

  async function restartService() {
    if (!window.confirm("确认重启素材库服务？重启期间网页会短暂无法访问，数据库与 NAS 文件不会被修改。")) return;
    setBusy(true);
    setServiceRestarting(true);
    setStatus("正在重启素材库服务，网页会短暂断开...");
    try {
      await requestJson<{ status: string; message: string }>("/api/system/restart", { method: "POST", body: JSON.stringify({ confirm: true }) });
      const startedAt = Date.now();
      const timer = window.setInterval(async () => {
        try {
          const data = await requestJson<ServiceStatus>("/api/system/status");
          setServiceStatus(data);
          setServiceRestarting(false);
          setStatus("素材库服务已恢复。");
          window.clearInterval(timer);
          window.setTimeout(() => window.location.reload(), 500);
        } catch {
          if (Date.now() - startedAt > 30000) {
            window.clearInterval(timer);
            setServiceRestarting(false);
            setStatus("服务重启超过 30 秒仍未恢复，请检查 start.cmd 窗口或端口 8000。 ");
          }
        }
      }, 1000);
    } catch (error) {
      setServiceRestarting(false);
      setStatus(`请求重启服务失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function loadIgnorePhrases() {
    try {
      const data = await requestJson<{ items: string[] }>("/api/ocr/ignore-phrases");
      setIgnorePhrases(data.items);
    } catch (error) {
      setStatus(`读取 OCR 无效词失败：${String(error)}`);
    }
  }

  async function addIgnorePhrase() {
    const phrase = newIgnorePhrase.trim();
    if (!phrase) {
      setStatus("无效词不能为空。");
      return;
    }
    setBusy(true);
    try {
      const data = await requestJson<{ items: string[] }>("/api/ocr/ignore-phrases", {
        method: "POST",
        body: JSON.stringify({ phrase })
      });
      setIgnorePhrases(data.items);
      setNewIgnorePhrase("");
      setStatus(`已添加 OCR 无效词：${phrase}`);
      notifyAction("success", `已添加 OCR 无效词：${phrase}`);
    } catch (error) {
      setStatus(`添加 OCR 无效词失败：${String(error)}`);
      notifyAction("error", `添加 OCR 无效词失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function deleteIgnorePhrase(phrase: string) {
    setBusy(true);
    try {
      const data = await requestJson<{ items: string[] }>(`/api/ocr/ignore-phrases?phrase=${encodeURIComponent(phrase)}`, { method: "DELETE" });
      setIgnorePhrases(data.items);
      if (editingIgnoreOriginal === phrase) {
        setEditingIgnoreOriginal("");
        setEditingIgnorePhrase("");
      }
      setStatus(`已删除 OCR 无效词：${phrase}`);
      notifyAction("success", `已删除 OCR 无效词：${phrase}`);
    } catch (error) {
      setStatus(`删除 OCR 无效词失败：${String(error)}`);
      notifyAction("error", `删除 OCR 无效词失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function startEditIgnorePhrase(phrase: string) {
    setEditingIgnoreOriginal(phrase);
    setEditingIgnorePhrase(phrase);
  }

  async function saveEditedIgnorePhrase() {
    const phrase = editingIgnorePhrase.trim();
    if (!editingIgnoreOriginal || !phrase) {
      setStatus("请先选择一个无效词，并填写修改后的内容。");
      return;
    }
    const nextPhrases = ignorePhrases.map((item) => (item === editingIgnoreOriginal ? phrase : item));
    setBusy(true);
    try {
      const data = await requestJson<{ items: string[] }>("/api/ocr/ignore-phrases", {
        method: "PUT",
        body: JSON.stringify({ phrases: nextPhrases })
      });
      setIgnorePhrases(data.items);
      setEditingIgnoreOriginal("");
      setEditingIgnorePhrase("");
      setStatus(`已修改 OCR 无效词：${phrase}`);
    } catch (error) {
      setStatus(`修改 OCR 无效词失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function updateNasConfig(field: keyof NasConfig, value: string | boolean) {
    setNasConfig((current) => ({ ...current, [field]: value }));
  }

  function handleConfigChange(field: keyof NasConfig) {
    return (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
      const target = event.target as HTMLInputElement;
      updateNasConfig(field, target.type === "checkbox" ? target.checked : target.value);
    };
  }

  async function saveConfig() {
    setBusy(true);
    try {
      await requestJson<LocalConfig>("/api/config", {
        method: "POST",
        body: JSON.stringify({ nas: nasConfig, app: { ...appConfig, title: APP_TITLE, default_batch_size: prefs.batchSize } })
      });
      setStatus("配置已保存到本机 config.local.json。");
    } catch (error) {
      setStatus(`保存配置失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function checkConnection() {
    setBusy(true);
    try {
      await saveConfig();
      const result = await requestJson<{ status: string; message: string; detail?: string }>("/api/nas/check", { method: "POST", body: JSON.stringify(nasConfig) });
      setStatus(`${result.message}${result.detail ? ` ${result.detail}` : ""}`);
    } catch (error) {
      setStatus(`连接测试失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function scanAssets() {
    setBusy(true);
    try {
      await saveConfig();
      const result = await requestJson<{ status: string; message: string; scanned: number; imported: number }>("/api/assets/scan", { method: "POST", body: JSON.stringify({ config: nasConfig }) });
      setStatus(`${result.message} 扫描 ${result.scanned} 张，新增 ${result.imported} 张。`);
      patchPrefs({ loadedCount: prefs.batchSize });
      await loadAssets();
      await loadTags();
      await loadFolders();
      setView("home");
    } catch (error) {
      setStatus(`扫描失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function backfillDataBaseAuthors() {
    setBusy(true);
    try {
      const result = await requestJson<{ checked: number; updated: number; errors: string[]; message: string }>("/api/derivatives/backfill-authors", { method: "POST" });
      setStatus(`${result.message} 检查 ${result.checked} 张，回填 ${result.updated} 位作者。${result.errors.length ? ` ${result.errors.length} 项无法读取，请查看服务端日志。` : ""}`);
      await loadAssets();
    } catch (error) {
      setStatus(`回填 DataBase 作者失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function toggleTag(tag: string) {
    const selectedTags = prefs.selectedTags.includes(tag) ? prefs.selectedTags.filter((item) => item !== tag) : [...prefs.selectedTags, tag];
    patchPrefs({ selectedTags, loadedCount: prefs.batchSize, scrollY: 0 });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function openDetail(asset: Asset) {
    const scrollY = window.scrollY;
    savedScrollYRef.current = scrollY;
    selectedAssetRef.current = asset;
    setSelectedAsset(asset);
    setDetectedNameDraft(asset.detectedName);
    setIsEditingDetectedName(false);
    setBusy(true);
    try {
      await requestJson<Asset>(`/api/assets/${asset.id}/click`, { method: "POST", body: "{}" });
      const updated = await requestJson<Asset>(`/api/assets/${asset.id}/variant?version=${asset.displayVersion}`);
      selectedAssetRef.current = updated;
      setSelectedAsset(updated);
      setDetectedNameDraft(updated.detectedName);
      setAssets((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    } catch {
      setStatus("点击统计写入失败，但详情仍可查看。");
    } finally {
      setBusy(false);
    }
  }

  async function downloadAssets(selected: Asset[]) {
    if (!selected.length) return;
    const failed: string[] = [];
    const requestedIds: number[] = [];
    setBusy(true);
    setStatus(`正在逐项下载 ${selected.length} 张图片，请允许浏览器进行多文件下载。`);
    try {
      for (const asset of selected) {
        try {
          const response = await fetch(`/api/assets/${asset.id}/download?version=${encodeURIComponent(asset.displayVersion)}`);
          if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
          const blob = await response.blob();
          const filename = downloadFilename(response.headers.get("Content-Disposition"), asset.libraryName || `asset-${asset.id}`);
          const url = URL.createObjectURL(blob);
          try {
            const link = document.createElement("a");
            link.href = url;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            link.remove();
            await new Promise((resolve) => window.setTimeout(resolve, 80));
          } finally {
            URL.revokeObjectURL(url);
          }
          requestedIds.push(asset.id);
        } catch (error) {
          failed.push(`${asset.detectedName || asset.libraryName || `图片 #${asset.id}`}：${String(error)}`);
        }
      }
      if (requestedIds.length) {
        const downloaded = new Set(requestedIds);
        setAssets((current) => current.map((asset) => downloaded.has(asset.id) ? { ...asset, downloads: asset.downloads + 1 } : asset));
        setSelectedAsset((current) => current && downloaded.has(current.id) ? { ...current, downloads: current.downloads + 1 } : current);
      }
      const report = `已向浏览器发起 ${requestedIds.length} 个独立文件下载请求，接口失败 ${failed.length} 张。`;
      setStatus(failed.length ? `${report} ${failed.join("；")}` : report);
      notifyAction(requestedIds.length ? "success" : "error", failed.length ? `${report} 请查看顶部状态中的失败原因。` : `${report} 若浏览器拦截，请在地址栏允许本站下载多个文件后重试。`);
    } finally {
      setBusy(false);
    }
  }

  async function switchDetailVersion(version: DisplayVersion) {
    if (!selectedAsset || selectedAsset.displayVersion === version) return;
    setBusy(true);
    try {
      const updated = await requestJson<Asset>(`/api/assets/${selectedAsset.id}/variant?version=${version}`);
      setSelectedAsset(updated);
      setDetectedNameDraft(updated.detectedName);
    } catch (error) {
      setStatus(version === "raw" ? `无法切换到生肉：${String(error)}` : `无法切换到 ${version} 派生版本：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }




  function closeDetail() {
    selectedAssetRef.current = null;
    setSelectedAsset(null);
    setIsEditingDetectedName(false);
    setDetectedNameDraft("");
    window.setTimeout(() => window.scrollTo({ top: savedScrollYRef.current, behavior: "auto" }), 0);
  }
  function closeFromBackdrop(event: MouseEvent<HTMLDivElement>) {
    if (event.target === event.currentTarget) closeDetail();
  }

  function openAdjacentAsset(direction: "prev" | "next") {
    if (!selectedAsset || !assets.length) return;
    const currentIndex = assets.findIndex((asset) => asset.id === selectedAsset.id);
    if (currentIndex < 0) return;
    const nextIndex = direction === "next" ? currentIndex + 1 : currentIndex - 1;
    const nextAsset = assets[nextIndex];
    if (nextAsset) {
      void openDetail(nextAsset);
    }
  }



  async function recognizeSelectedAssetText(asset: Asset) {
    setBusy(true);
    setOcrProgress({ active: true, current: 0, total: 1, succeeded: 0, failed: 0, autoHidden: 0, label: asset.libraryName || asset.detectedName });
    setStatus("正在本地识别图片文字，请稍候...");
    try {
      const result = await enqueueOcrAndWait(asset.id, (job) => setStatus(job.status === "queued" ? "OCR 已排队，正在等待..." : "正在本地识别图片文字，请稍候..."));
      const ok = result.status === "done";
      const hiddenItem = ok ? captureOcrAutoHidden(result, asset.id, asset.detectedName || asset.libraryName) : null;
      setOcrProgress({ active: true, current: 1, total: 1, succeeded: ok ? 1 : 0, failed: ok ? 0 : 1, autoHidden: hiddenItem ? 1 : 0, label: asset.libraryName || asset.detectedName });
      let finalMessage = result.error || result.message || "OCR 未完成";
      if (ok && hiddenItem) {
        finalMessage = `OCR 完成并已自动隐藏“${hiddenItem.name}”：${hiddenItem.hiddenReason}`;
      } else if (ok) {
        const updated = await requestJson<Asset>(`/api/assets/${asset.id}/variant?version=${asset.displayVersion}`);
        setSelectedAsset(updated);
        setAssets((current) => current.map((item) => (item.id === updated.id ? updated : item)));
        finalMessage = `OCR 完成：${updated.ocrText || "未识别到文字"}${result.result?.syncWarning ? `；${result.result.syncWarning}` : ""}`;
      }
      await Promise.all([loadTags(), loadOcrSummary()]);
      setStatus(finalMessage);
      notifyAction(hiddenItem ? "info" : ok ? "success" : "error", finalMessage);
      window.setTimeout(() => setOcrProgress(null), 1600);
    } catch (error) {
      setOcrProgress({ active: true, current: 1, total: 1, succeeded: 0, failed: 1, autoHidden: 0, label: asset.libraryName || asset.detectedName });
      setStatus(`OCR 失败：${String(error)}`);
      window.setTimeout(() => setOcrProgress(null), 2200);
    } finally {
      setBusy(false);
    }
  }

  async function enqueueOcrAndWait(assetId: number, onUpdate?: (job: JobDetail) => void): Promise<JobDetail> {
    const queued = await requestJson<{ job_id: number }>(`/api/jobs/retry-ocr/${assetId}`, { method: "POST", body: "{}" });
    while (true) {
      const job = await requestJson<JobDetail>(`/api/jobs/${queued.job_id}`);
      onUpdate?.(job);
      if (!["queued", "running", "cancel_requested"].includes(job.status)) return job;
      await new Promise((resolve) => window.setTimeout(resolve, job.status === "queued" ? 3000 : 1500));
    }
  }

  async function saveDetectedName() {
    if (!selectedAsset) return;
    const nextName = detectedNameDraft.trim();
    if (!nextName) {
      setStatus("识别命名不能为空。");
      return;
    }
    setBusy(true);
    try {
      await requestJson<Asset>(`/api/assets/${selectedAsset.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          libraryName: selectedAsset.libraryName,
          detectedName: nextName,
          tags: selectedAsset.tags
        })
      });
      const updated = await requestJson<Asset>(`/api/assets/${selectedAsset.id}/variant?version=${selectedAsset.displayVersion}`);
      setSelectedAsset(updated);
      setDetectedNameDraft(updated.detectedName);
      setIsEditingDetectedName(false);
      setAssets((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setStatus(`识别命名已更新：${updated.detectedName}`);
      await loadAssets();
    } catch (error) {
      setStatus(`保存识别命名失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function analyzeDuplicates() {
    if (duplicateSubmitting || duplicateJobId !== null) return;
    setDuplicateSubmitting(true);
    setDuplicateErrors([]);
    setDuplicateGroups([]);
    setDuplicateJob(null);
    setStatus("正在提交后台重复审查任务...");
    try {
      const queued = await requestJson<{ job_id: number }>("/api/duplicates/analyze", {
        method: "POST",
        body: JSON.stringify({ folder: duplicateFolderFilter, ratio: duplicateRatioFilter })
      });
      setDuplicateJobId(queued.job_id);
      setStatus(`重复审查任务 #${queued.job_id} 已进入后台队列，其他页面可继续使用。`);
      void loadJobStatus();
    } catch (error) {
      setStatus(`重复审查失败：${String(error)}`);
      notifyAction("error", `无法启动重复审查：${String(error)}`);
    } finally {
      setDuplicateSubmitting(false);
    }
  }

  async function cancelDuplicateAnalysis() {
    if (!duplicateJob || duplicateJobId === null) return;
    setDuplicateJob((current) => current ? { ...current, status: "cancel_requested" } : current);
    await cancelBackgroundJob(duplicateJob);
  }

  async function retainDuplicateAsset(keepAsset: Asset, group: DuplicateGroup) {
    const candidateIds = group.items.filter((item) => item.id !== keepAsset.id).map((item) => item.id);
    if (!candidateIds.length || !window.confirm(`确认保留“${keepAsset.detectedName || keepAsset.libraryName}”吗？仅隐藏其余 ${candidateIds.length} 条数据库记录，不移动或删除 NAS 文件。`)) return;
    setBusy(true);
    try {
      const result = await requestJson<{ message: string }>("/api/duplicates/retain", { method: "POST", body: JSON.stringify({ keep_asset_id: keepAsset.id, candidate_asset_ids: candidateIds }) });
      setStatus(result.message);
      notifyAction("success", result.message);
      setDuplicateGroups((current) => current.map((currentGroup) => ({ ...currentGroup, items: currentGroup.items.filter((item) => !candidateIds.includes(item.id)) })).filter((currentGroup) => currentGroup.items.length > 1));
      setAssets((current) => current.filter((item) => !candidateIds.includes(item.id)));
      setSelectedIds((current) => current.filter((id) => !candidateIds.includes(id)));
      void Promise.all([loadAssets(), loadDuplicateHistory(), loadHiddenAssets()]);
    } catch (error) {
      setStatus(`保留重复图片失败：${String(error)}`);
      notifyAction("error", `保留重复图片失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function undoDuplicateRetain(item: DuplicateHistoryItem) {
    if (!window.confirm(`确认恢复该次审查隐藏的 ${item.hiddenCount} 条记录吗？不会改动 NAS 文件。`)) return;
    setBusy(true);
    try {
      const result = await requestJson<{ message: string }>("/api/duplicates/undo", { method: "POST", body: JSON.stringify({ keep_asset_id: item.keepAssetId }) });
      setStatus(result.message);
      notifyAction("success", result.message);
      await Promise.all([loadDuplicateHistory(), loadAssets(), loadHiddenAssets()]);
      void analyzeDuplicates();
    } catch (error) {
      setStatus(`撤销重复审查失败：${String(error)}`);
      notifyAction("error", `撤销重复审查失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function restoreWebsiteHiddenAsset(item: HiddenAsset) {
    setBusy(true);
    try {
      const result = await requestJson<{ message: string }>(`/api/assets/${item.id}/restore-hidden`, { method: "POST", body: "{}" });
      setStatus(result.message);
      notifyAction("success", result.message);
      await Promise.all([loadHiddenAssets(), loadAssets(), loadDuplicateHistory()]);
    } catch (error) {
      setStatus(`恢复隐藏记录失败：${String(error)}`);
      notifyAction("error", `恢复隐藏记录失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function deleteDerivative(selected: Asset) {
    const kind = selected.displayVersion === "raw" ? "16:9" : selected.displayVersion;
    if (!window.confirm(`将把该图片当前的 ${kind} 派生图移入 NAS 回收站，并删除这条派生图记录。原图与数据库主记录不会删除。`)) return;
    setBusy(true);
    try {
      const result = await requestJson<{ message: string }>(`/api/assets/${selected.id}/delete-derivative`, { method: "POST", body: JSON.stringify({ username: displayName, kind }) });
      setStatus(result.message);
      notifyAction("success", result.message);
      const updated = await requestJson<Asset>(`/api/assets/${selected.id}/variant?version=original`);
      setSelectedAsset(updated);
      setAssets((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    } catch (error) {
      setStatus(`删除派生图失败：${String(error)}`);
      notifyAction("error", `删除派生图失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function hideWebsiteAsset(selected: Asset) {
    if (!window.confirm("将从网站总览隐藏该图片记录。NAS 原图、派生图和 SQLite 数据库记录均不会删除；可在审查页“网站隐藏记录”中恢复。")) return;
    setBusy(true);
    try {
      const result = await requestJson<{ message: string }>(`/api/assets/${selected.id}/hide`, { method: "POST", body: JSON.stringify({ username: displayName }) });
      setStatus(result.message);
      setAssets((current) => current.filter((item) => item.id !== selected.id));
      setSelectedIds((current) => current.filter((id) => id !== selected.id));
      closeDetail();
      notifyAction("success", result.message);
      void loadAssets();
    } catch (error) {
      setStatus(`隐藏网站记录失败：${String(error)}`);
      notifyAction("error", `隐藏网站记录失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function deleteSourceAsset(selected: Asset) {
    if (!window.confirm("红色操作：将把 NAS 原图、全部已登记派生图移入对应回收站，并将数据库记录移入审查回收站。不会永久删除；恢复前会检查路径避免覆盖。")) return;
    setBusy(true);
    try {
      const result = await requestJson<{ message: string }>(`/api/assets/${selected.id}/delete-source`, { method: "POST", body: JSON.stringify({ username: displayName }) });
      setStatus(result.message);
      setAssets((current) => current.filter((item) => item.id !== selected.id));
      setSelectedIds((current) => current.filter((id) => id !== selected.id));
      closeDetail();
      notifyAction("success", result.message);
      void Promise.all([loadAssets(), loadDeletedAssets()]);
    } catch (error) {
      setStatus(`删除 NAS 原图失败：${String(error)}`);
      notifyAction("error", `删除 NAS 原图失败：${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function recognizeSelectedAssetsTextBatch() {
    if (!selectedIds.length) {
      setStatus("请先选择要识别文字的图片。");
      return;
    }
    const idsToProcess = [...selectedIds];
    const assetNameById = new Map(assets.map((asset) => [asset.id, asset.libraryName || asset.detectedName]));
    let succeeded = 0;
    let failed = 0;
    let autoHidden = 0;
    const autoHiddenIds: number[] = [];
    const syncWarnings: string[] = [];
    setBusy(true);
    setOcrProgress({ active: true, current: 0, total: idsToProcess.length, succeeded: 0, failed: 0, autoHidden: 0, label: "准备开始" });
    setStatus(`正在本地识别 ${idsToProcess.length} 张图片文字...`);
    try {
      for (let index = 0; index < idsToProcess.length; index += 1) {
        const assetId = idsToProcess[index];
        const label = assetNameById.get(assetId) || `图片 ${assetId}`;
        setOcrProgress({ active: true, current: index, total: idsToProcess.length, succeeded, failed, autoHidden, label });
        setStatus(`OCR 识别中：${index + 1}/${idsToProcess.length} ${label}`);
        try {
          const result = await enqueueOcrAndWait(assetId, (job) => setStatus(job.status === "queued" ? `OCR 等待队列：${index + 1}/${idsToProcess.length} ${label}` : `OCR 识别中：${index + 1}/${idsToProcess.length} ${label}`));
          if (result.status === "done") {
            succeeded += 1;
            if (result.result?.syncWarning) syncWarnings.push(`${label}：${result.result.syncWarning}`);
            if (captureOcrAutoHidden(result, assetId, label)) {
              autoHidden += 1;
              autoHiddenIds.push(assetId);
            }
          } else {
            failed += 1;
          }
        } catch {
          failed += 1;
        }
        setOcrProgress({ active: true, current: index + 1, total: idsToProcess.length, succeeded, failed, autoHidden, label });
      }
      await loadAssets();
      if (autoHiddenIds.length) {
        const hiddenIds = new Set(autoHiddenIds);
        setAssets((current) => current.filter((asset) => !hiddenIds.has(asset.id)));
      }
      await loadTags();
      await loadOcrSummary();
      if (syncWarnings.length) setOcrErrors(syncWarnings);
      const finalMessage = `批量 OCR 完成。已处理 ${idsToProcess.length} 张，成功 ${succeeded} 张，失败 ${failed} 张，自动隐藏 ${autoHidden} 张，派生命名同步警告 ${syncWarnings.length} 项。`;
      setStatus(finalMessage);
      notifyAction(failed ? "info" : "success", finalMessage);
      window.setTimeout(() => setOcrProgress(null), 2200);
    } finally {
      setBusy(false);
    }
  }

  async function recognizeSummaryAssets(kind: "unrecognized" | "failed") {
    const summary = await loadOcrSummary();
    const idsToProcess = kind === "failed" ? summary?.failedIds ?? [] : summary?.unrecognizedIds ?? [];
    if (!summary || !idsToProcess.length) {
      setStatus(kind === "failed" ? "没有需要重新识别的失败项目。" : "没有需要识别的图片。");
      return;
    }
    const failedNameById = new Map(summary.failedItems.map((item) => [item.id, item.name]));
    const assetNameById = new Map([...assets.map((asset) => [asset.id, asset.detectedName || asset.libraryName] as const), ...failedNameById]);
    const taskName = kind === "failed" ? "重新识别失败项目" : "识别未识别项目";
    let succeeded = 0;
    let failed = 0;
    let autoHidden = 0;
    const autoHiddenIds: number[] = [];
    const errors: string[] = [];
    setOcrErrors([]);
    setBusy(true);
    setOcrProgress({ active: true, current: 0, total: idsToProcess.length, succeeded: 0, failed: 0, autoHidden: 0, label: `准备${taskName}` });
    try {
      for (let index = 0; index < idsToProcess.length; index += 1) {
        const assetId = idsToProcess[index];
        const label = assetNameById.get(assetId) || `图片 ${assetId}`;
        setOcrProgress({ active: true, current: index, total: idsToProcess.length, succeeded, failed, autoHidden, label });
        setStatus(`${taskName}：${index + 1}/${idsToProcess.length} ${label}`);
        try {
          const result = await enqueueOcrAndWait(assetId, (job) => setStatus(job.status === "queued" ? `${taskName}等待队列：${index + 1}/${idsToProcess.length} ${label}` : `${taskName}：${index + 1}/${idsToProcess.length} ${label}`));
          if (result.status === "done") {
            succeeded += 1;
            if (result.result?.syncWarning) errors.push(`${label}：${result.result.syncWarning}`);
            if (captureOcrAutoHidden(result, assetId, label)) {
              autoHidden += 1;
              autoHiddenIds.push(assetId);
            }
          } else {
            failed += 1;
            errors.push(`${label}：${result.error || result.message || "任务未完成"}`);
          }
        } catch (error) {
          failed += 1;
          errors.push(`${label}：${String(error)}`);
        }
        setOcrErrors([...errors]);
        setOcrProgress({ active: true, current: index + 1, total: idsToProcess.length, succeeded, failed, autoHidden, label });
      }
      await loadAssets();
      if (autoHiddenIds.length) {
        const hiddenIds = new Set(autoHiddenIds);
        setAssets((current) => current.filter((asset) => !hiddenIds.has(asset.id)));
      }
      await loadTags();
      await loadOcrSummary();
      const finalMessage = `${taskName}完成。成功 ${succeeded} 张，失败 ${failed} 张，自动隐藏 ${autoHidden} 张。`;
      setStatus(finalMessage);
      notifyAction(failed ? "info" : "success", finalMessage);
      window.setTimeout(() => setOcrProgress(null), 2200);
    } finally {
      setBusy(false);
    }
  }

  async function recognizeUnrecognizedAssets() {
    await recognizeSummaryAssets("unrecognized");
  }

  async function recognizeFailedAssets() {
    await recognizeSummaryAssets("failed");
  }

  function toggleAssetSelection(assetId: number, event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    setSelectedIds((current) => (current.includes(assetId) ? current.filter((id) => id !== assetId) : [...current, assetId]));
  }

  function selectVisibleAssets() {
    setSelectedIds((current) => Array.from(new Set([...current, ...assets.map((asset) => asset.id)])));
  }

  function clearSelectedAssets() {
    setSelectedIds([]);
  }

  async function downloadSelectedAssets() {
    if (!selectedIds.length) {
      setStatus("请先选择要下载的图片。");
      return;
    }
    const selected = assets.filter((asset) => selectedIds.includes(asset.id));
    if (!selected.length) {
      setStatus("当前页面没有可处理的已选图片，请重新选择。");
      return;
    }
    await downloadAssets(selected);
  }

  function loadNextBatch() {
    patchPrefs({ loadedCount: Math.min(prefs.loadedCount + prefs.batchSize, total || prefs.loadedCount + prefs.batchSize) });
  }

  function jumpBatch(direction: "prev" | "next") {
    const delta = direction === "next" ? prefs.batchSize : -prefs.batchSize;
    const nextCount = Math.min(Math.max(prefs.batchSize, prefs.loadedCount + delta), total || prefs.batchSize);
    patchPrefs({ loadedCount: nextCount });
    window.scrollTo({ top: Math.max(0, document.body.scrollHeight - window.innerHeight - 900), behavior: "smooth" });
  }

  return (
    <main className="app-shell">
      <aside className="side-rail">
        <div className="rail-logo"><img className="project-mark" src={brandLogo} alt="素材库标识" /></div>
        <button className={view === "home" ? "active" : ""} onClick={() => setView("home")} title="图片库" aria-label="图片库"><Database size={20} aria-hidden="true" /></button>
        <button className={view === "ocr" ? "active" : ""} onClick={() => setView("ocr")} title="识别中心" aria-label="识别中心"><FileText size={20} aria-hidden="true" /></button>
        <button className={view === "audit" ? "active" : ""} onClick={() => setView("audit")} title="数据库审查" aria-label="数据库审查"><Search size={20} aria-hidden="true" /></button>
        <button className={view === "export" ? "active" : ""} onClick={() => setView("export")} title="派生图导出中心" aria-label="派生图导出中心"><Download size={20} aria-hidden="true" /></button>
        <button className={view === "folderExport" ? "active" : ""} onClick={() => setView("folderExport")} title="按文件夹导出" aria-label="按文件夹导出"><FolderInput size={20} aria-hidden="true" /></button>
        <button className={view === "duplicates" ? "active" : ""} onClick={() => setView("duplicates")} title="重复审查" aria-label="重复审查"><Layers3 size={20} aria-hidden="true" /></button>
        <button className={view === "annotation" ? "active" : ""} onClick={() => setView("annotation")} title="标签管理与标注" aria-label="标签管理与标注"><Tags size={20} aria-hidden="true" /></button>
        <div className="rail-bottom-nav">
          <button className={view === "config" ? "active" : ""} onClick={() => setView("config")} title="NAS 配置与扫描" aria-label="NAS 配置与扫描"><Network size={20} aria-hidden="true" /></button>
          <button className={`about-nav ${view === "about" ? "active" : ""}`} onClick={() => setView("about")} title="关于素材库" aria-label="关于素材库"><Info size={20} aria-hidden="true" /></button>
        </div>
      </aside>
      <section className="workspace">
        <header className="topbar"><div><p className="eyebrow">当前用户：{displayName}</p><h1>{APP_TITLE}</h1></div><div className="status-pill"><CheckCircle2 size={16} aria-hidden="true" />{status}</div></header>
        {actionNotice ? <section className={`action-notice ${actionNotice.tone}`} role="status" aria-live="polite">{actionNotice.text}</section> : null}
        {busy || isLoadingAssets ? <section className="operation-progress" role="status" aria-live="polite"><div className="operation-progress-head"><strong>{ocrProgress?.active ? "OCR 处理中" : isLoadingAssets && !busy ? "正在读取图片库" : "正在处理，请稍候"}</strong><span>{ocrProgress?.active ? `${ocrProgress.current}/${ocrProgress.total}` : "请勿关闭页面"}</span></div><div className="operation-progress-track"><div className={ocrProgress?.active ? "determinate" : "indeterminate"} style={ocrProgress?.active ? { width: `${Math.round((ocrProgress.current / Math.max(1, ocrProgress.total)) * 100)}%` } : undefined} /></div></section> : null}

        {view === "annotation" ? (
          <><AnnotationPage /><TagManager /></>
        ) : view === "config" ? (
          <section className="panel">
            <div className="panel-heading"><FolderInput size={22} aria-hidden="true" /><div><h2>NAS 配置与扫描</h2><p>你提供的信息已预填。点击“连接测试”才会访问该地址；点击“扫描入库”才会读取图片列表。</p></div></div>
            <form className="config-grid" onSubmit={(event) => { event.preventDefault(); void saveConfig(); }}>
              <label>连接类型<select value={nasConfig.type} onChange={handleConfigChange("type")}><option value="http">HTTP/Web 页面</option><option value="smb">Windows/SMB 路径</option></select></label>
              <label>NAS 地址<input value={nasConfig.host} onChange={handleConfigChange("host")} placeholder="例如 FILE_SERVER" /></label>
              <label>共享目录<input value={nasConfig.share} onChange={handleConfigChange("share")} placeholder="例如 shared\\image-library" /></label>
              <label className="wide">基础路径<input value={nasConfig.base_path} onChange={handleConfigChange("base_path")} placeholder="例如 \\\\FILE_SERVER\\shared\\image-library" /></label>
              <label>用户名<input value={nasConfig.username} onChange={handleConfigChange("username")} placeholder="NAS 用户名" /></label>
              <label>密码<input type="password" value={nasConfig.password} onChange={handleConfigChange("password")} placeholder="NAS 密码" /></label>
              <label>每批加载数量<input type="number" min={5} max={100} value={prefs.batchSize} onChange={(event) => patchPrefs({ batchSize: Number(event.target.value), loadedCount: Number(event.target.value) })} /></label>
              <label className="checkbox-line"><input type="checkbox" checked={nasConfig.recursive} onChange={handleConfigChange("recursive")} />递归扫描子文件夹</label>
              <div className="action-row"><button type="submit" className="primary-action" disabled={busy}><Save size={18} />保存配置</button><button type="button" className="primary-action" onClick={checkConnection} disabled={busy}><Play size={18} />连接测试</button><button type="button" className="primary-action" onClick={scanAssets} disabled={busy}><RefreshCw size={18} />扫描入库</button></div>
            </form>
            <DataSourcesPanel />
          </section>
        ) : view === "ocr" ? (
          <section className="panel ocr-center-panel">
            <div className="panel-heading"><FileText size={22} aria-hidden="true" /><div><h2>识别中心</h2><p>检测数据库里尚未识别或识别失败的图片，并统一执行 OCR。识别结果写入同一个 SQLite 数据库，其他电脑访问这台服务时可以看到同一份结果。</p></div></div>
            <section className="scope-filter-controls" aria-label="OCR 批量范围">
              <label>文件夹<select value={ocrFolderFilter} onChange={(event) => setOcrFolderFilter(event.target.value)}><option value="">全部已入库文件夹</option>{allFolders.filter((folder) => !folder.toLowerCase().endsWith("\\database")).map((folder) => <option key={folder} value={folder}>{folder}</option>)}</select></label>
              <label>图片比例<select value={ocrRatioFilter} onChange={(event) => setOcrRatioFilter(event.target.value)}><option value="">全部比例</option><option value="16:9">16:9</option><option value="1:1">1:1</option><option value="9:16">9:16</option><option value="other">其他或未知</option></select></label>
              <p>批量 OCR 只会处理当前范围内的图片；不同原始分辨率均按原图读取，不会生成或替换图片。</p>
            </section>
            <section className="metric-row ocr-metric-row"><div><span>{ocrSummary?.total ?? 0}</span>全部图片</div><div><span>{ocrSummary?.unrecognized ?? 0}</span>未识别</div><div><span>{ocrSummary?.done ?? 0}</span>已识别</div><div><span>{ocrSummary?.failed ?? 0}</span>失败</div><div><span>{ocrAutoHiddenReport.length}</span>本次自动隐藏</div></section>
            <section className="ocr-exclusion-bar"><div><strong>识别范围</strong><span>{prefs.excludePathEnabled ? `已排除：${prefs.excludePath || "未填写路径"}` : "当前会包含导出目录"}</span></div><button type="button" aria-pressed={prefs.excludePathEnabled} className={prefs.excludePathEnabled ? "path-exclude-toggle active" : "path-exclude-toggle"} disabled={!prefs.excludePath.trim()} onClick={() => patchPrefs({ excludePathEnabled: !prefs.excludePathEnabled })}>{prefs.excludePathEnabled ? "已排除导出目录" : "纳入导出目录"}</button></section>
            <div className="action-row"><button type="button" className="primary-action" onClick={() => void loadOcrSummary()} disabled={busy}><RefreshCw size={18} />检测未识别项目</button><button type="button" className="primary-action" onClick={() => void recognizeUnrecognizedAssets()} disabled={busy || !(ocrSummary?.unrecognizedIds.length)}><FileText size={18} />统一识别未识别项目</button><button type="button" className="ghost-action retry-failed-action" onClick={() => void recognizeFailedAssets()} disabled={busy || !(ocrSummary?.failedIds.length)}><RefreshCw size={18} />重新识别失败项目（{ocrSummary?.failed ?? 0}）</button></div>
            {ocrProgress?.active ? <section className="ocr-progress-panel" aria-label="OCR 识别进度"><div className="ocr-progress-head"><strong>OCR 识别进度</strong><span>{ocrProgress.current}/{ocrProgress.total}，成功 {ocrProgress.succeeded}，失败 {ocrProgress.failed}，自动隐藏 {ocrProgress.autoHidden}</span></div><div className="ocr-progress-track"><div style={{ width: `${Math.round((ocrProgress.current / Math.max(1, ocrProgress.total)) * 100)}%` }} /></div><p>{ocrProgress.label}</p></section> : null}
            <section className="ignore-phrase-panel">
              <div className="section-heading"><div><h3>OCR 无效词</h3><p>这里维护会被标题清洗剔除的词。新增或修改后，后续 OCR 会立即使用；已经识别过的图片需要重新识别才会刷新标题。</p></div><button type="button" className="ghost-action" onClick={() => void loadIgnorePhrases()} disabled={busy}><RefreshCw size={16} />刷新</button></div>
              <div className="ignore-editor">
                <input value={newIgnorePhrase} onChange={(event) => setNewIgnorePhrase(event.target.value)} placeholder="新增无效词，例如：继续观看" />
                <button type="button" className="primary-action" onClick={() => void addIgnorePhrase()} disabled={busy}>添加</button>
              </div>
              <div className="ignore-editor">
                <input value={editingIgnorePhrase} onChange={(event) => setEditingIgnorePhrase(event.target.value)} placeholder="选择下方词条后在这里修改" />
                <button type="button" className="ghost-action" onClick={() => void saveEditedIgnorePhrase()} disabled={busy || !editingIgnoreOriginal}>保存修改</button>
                <button type="button" className="ghost-action" onClick={() => { setEditingIgnoreOriginal(""); setEditingIgnorePhrase(""); }} disabled={!editingIgnoreOriginal}>取消编辑</button>
              </div>
              <div className="ignore-list" aria-label="OCR 无效词列表">
                {ignorePhrases.length ? ignorePhrases.map((phrase) => (
                  <div className={editingIgnoreOriginal === phrase ? "ignore-chip editing" : "ignore-chip"} key={phrase}>
                    <span>{phrase}</span>
                    <button type="button" onClick={() => startEditIgnorePhrase(phrase)}>编辑</button>
                    <button type="button" onClick={() => void deleteIgnorePhrase(phrase)} disabled={busy}>删除</button>
                  </div>
                )) : <p>暂无无效词。</p>}
              </div>
            </section>
            <section className="ocr-error-panel ocr-auto-hidden-panel"><h3>自动隐藏报告</h3><p>OCR（光学字符识别）文本命中书名号《…》时，图片会立即从当前图片流移除并进入隐藏记录。</p>{ocrAutoHiddenReport.length ? <ul>{ocrAutoHiddenReport.map((item) => <li key={item.assetId}><strong>{item.name}</strong><span>图片 #{item.assetId} · {item.hiddenReason}</span></li>)}</ul> : <p>本次打开页面后尚无自动隐藏记录。</p>}</section>
            <section className="ocr-error-panel"><h3>失败原因</h3><p>这里保留当前失败项目的最近一次原因；重新识别成功后会自动移除对应记录。</p>{ocrErrors.length ? <ul>{ocrErrors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}</ul> : <p>暂无失败记录。</p>}</section>
          </section>
        ) : view === "audit" ? (
          <section className="panel review-panel">
            <div className="panel-heading"><Search size={22} aria-hidden="true" /><div><h2>数据库审查</h2><p>检测源文件是否仍可访问。仅“文件不存在”的项目可被批量从数据库移除；不会删除、移动或改名 NAS 文件。若仅删数据库记录留下了 1280 文件，可在下方检测并移入回收站。</p></div></div>
            <div className="action-row"><button type="button" className="primary-action" onClick={() => void runSourceAudit()} disabled={busy}><RefreshCw size={18} />检测失效来源</button><button type="button" className="ghost-action" onClick={() => void removeAuditedAssets()} disabled={busy || !auditSelectedIds.length}>仅从数据库移除 {auditSelectedIds.length} 项</button></div>
            <section className="audit-list" aria-label="来源审查结果">
              {auditItems.length ? auditItems.map((item) => <article className={`audit-row ${item.status}`} key={item.id}><label className="audit-check"><input type="checkbox" checked={auditSelectedIds.includes(item.id)} disabled={item.status !== "missing"} onChange={() => toggleAuditSelection(item.id)} /><span /></label><div><strong>{item.detectedName || item.libraryName}</strong><p>{item.path}</p><small>OCR：{item.ocrStatus} · 入库：{item.importedAt}</small></div><div className="audit-result"><b>{item.status === "missing" ? "文件不存在" : item.status === "available" ? "正常" : item.status === "unauthorized" ? "无权限" : "网络错误"}</b><span>{item.detail}</span></div></article>) : <p className="empty-copy">尚未执行审查。点击“检测失效来源”后才会访问记录中的源路径。</p>}
            </section>
            <section className="trash-panel orphan-derivative-panel"><div className="section-heading"><div><h3>1280 孤儿文件清理</h3><p>仅检查 DataBase 输出目录中没有对应 SQLite 导出记录的图片。清理会移入 NAS 回收站，不会删除原图。</p></div><div className="action-row"><button type="button" className="ghost-action" onClick={() => void findDerivativeOrphans()} disabled={busy}><RefreshCw size={18} />检测 1280 孤儿文件</button><button type="button" className="danger-action" onClick={() => void deleteSelectedDerivativeOrphans()} disabled={busy || !selectedDerivativeOrphanPaths.length}><Trash2 size={18} />清理所选 {selectedDerivativeOrphanPaths.length} 项</button></div></div>{derivativeOrphanRoot ? <small className="orphan-root">输出目录：{derivativeOrphanRoot}</small> : null}{derivativeOrphans.length ? <div className="trash-list">{derivativeOrphans.map((item) => <label key={item.path}><input type="checkbox" checked={selectedDerivativeOrphanPaths.includes(item.path)} onChange={() => toggleDerivativeOrphanSelection(item.path)} /><span><strong>{item.name}</strong><small>{item.modifiedAt} · {(item.sizeBytes / 1024 / 1024).toFixed(2)} MB · {item.path}</small></span></label>)}</div> : derivativeOrphanRoot ? <p className="empty-copy">未发现未登记的 1280 文件。</p> : <p className="empty-copy">点击“检测 1280 孤儿文件”后才会扫描 DataBase 输出目录。</p>}</section>
            <section className="trash-panel"><div className="section-heading"><div><h3>数据库与 NAS 原图回收站</h3><p>从网页移除的记录和移入 NAS 回收站的原图都可在这里恢复。</p></div><button type="button" className="ghost-action" onClick={() => void restoreDeletedItems()} disabled={busy || !deletedSelectedIds.length}>恢复所选 {deletedSelectedIds.length} 项</button></div>{deletedAssets.length ? <div className="trash-list">{deletedAssets.map((item) => <label key={item.deletedId}><input type="checkbox" checked={deletedSelectedIds.includes(item.deletedId)} onChange={() => toggleDeletedSelection(item.deletedId)} /><span><strong>{item.detectedName}</strong><small>{item.deletedAt} · {item.reason || "数据库移除"} · {item.path}</small></span></label>)}</div> : <p className="empty-copy">回收站为空。</p>}</section>
            <section className="trash-panel"><div className="section-heading"><div><h3>1280 文件回收站</h3><p>恢复会把生成的 1280×720 文件放回原 DataBase 路径，并重新登记导出记录。</p></div></div>{recycledDerivatives.length ? <div className="trash-list">{recycledDerivatives.map((item) => <div className="trash-derivative-row" key={item.id}><span><strong>{item.title}</strong><small>{item.deletedAt} · {item.originalPath}</small></span><button type="button" className="ghost-action" onClick={() => void restoreRecycledDerivative(item)} disabled={busy}>恢复 1280 文件</button></div>)}</div> : <p className="empty-copy">暂无已删除的 1280 文件。</p>}</section>
            <section className="trash-panel"><div className="section-heading"><div><h3>网站隐藏记录</h3><p>这里包含手动隐藏和重复审查隐藏的图片。恢复不会改动 NAS 原图或派生图。</p></div><button type="button" className="ghost-action" onClick={() => void loadHiddenAssets()} disabled={busy}><RefreshCw size={16} />刷新</button></div>{hiddenAssets.length ? <div className="trash-list">{hiddenAssets.map((item) => <div className="trash-derivative-row" key={item.id}><span><strong>{item.name}</strong><small>{item.hiddenAt} · {item.reason} · {item.path}</small></span><button type="button" className="ghost-action" onClick={() => void restoreWebsiteHiddenAsset(item)} disabled={busy}>恢复显示</button></div>)}</div> : <p className="empty-copy">暂无网站隐藏记录。</p>}</section>
          </section>
        ) : view === "export" ? (
          <section className="panel export-panel">
            <div className="panel-heading"><Download size={22} aria-hidden="true" /><div><h2>派生图导出中心</h2><p>每张原图只会自动选择一个最接近的比例：16:9、1:1 或 9:16；偏差超过 10% 跳过，不裁切。所有输出统一为 JPG。</p></div></div>
            {derivativePlan ? <>
              <section className="export-summary">
                <div><span>输出目录</span><strong>{derivativePlan.outputRoot}</strong><small>{derivativePlan.outputRootExists ? "目录已存在" : "生成时将尝试创建目录"}</small></div>
                <div><span>可生成</span><strong>{derivativePlan.summary.ready}</strong><small>共 {derivativePlan.summary.total} 项</small></div>
                <div><span>编号缺口</span><strong>{derivativePlan.missingNumbers.length}</strong><small>{derivativePlan.missingNumbers.length ? derivativePlan.missingNumbers.slice(0, 20).join("、") : "无"}</small></div>
                <div><span>重复编号</span><strong>{derivativePlan.duplicateNumbers.length}</strong><small>{derivativePlan.duplicateNumbers.length ? derivativePlan.duplicateNumbers.join("、") : "无"}</small></div>
              </section>
              <section className="derivative-author-panel">
                <label>
                  <span>作者信息（可选）</span>
                  <input list="derivative-author-history" value={derivativeAuthor} onChange={(event) => setDerivativeAuthor(event.target.value)} placeholder="留空时按来源文件夹自动识别" maxLength={120} />
                  <datalist id="derivative-author-history">{savedDerivativeAuthors.map((author) => <option value={author} key={author} />)}</datalist>
                </label>
                <label>
                  <span>获取渠道（必填）</span>
                  <input list="derivative-channel-history" value={derivativeChannel} onChange={(event) => setDerivativeChannel(event.target.value)} placeholder="例如：抖音、素材平台、合作方" maxLength={120} required />
                  <datalist id="derivative-channel-history">{savedDerivativeChannels.map((channel) => <option value={channel} key={channel} />)}</datalist>
                </label>
                <div className="derivative-export-options"><p>统一输出 JPG；作者写入 Windows“作者”，获取渠道写入“备注”，并以 SQLite 作为查询主来源。</p></div>
                <button type="button" className="ghost-action" onClick={() => void updateGeneratedDerivativeMetadata()} disabled={busy || (!derivativeAuthor.trim() && !derivativeChannel.trim()) || !derivativePlan.summary.alreadyGenerated}>更新已导出信息 {derivativePlan.summary.alreadyGenerated ? `(${derivativePlan.summary.alreadyGenerated})` : ""}</button>
              </section>
              <div className="action-row"><button type="button" className="primary-action" onClick={() => void generateDerivatives()} disabled={busy || !derivativePlan.summary.ready}><Download size={18} />生成 {derivativePlan.summary.ready} 项</button><button type="button" className="ghost-action" onClick={() => setView("home")}>返回图片库</button></div>
              <section className="export-list">{derivativePlan.items.map((item) => <article className={`export-row ${item.status}`} key={item.id}><div><strong>{item.title}</strong><p>{item.kind || "待判断比例"} · 作者：{item.author} · 源图：{item.sourceResolution} · {item.sourceSizeMb.toFixed(2)} MB</p><small>{item.outputPath || item.existingPath || item.message}</small></div><div><b>{item.status === "ready" ? `${item.kind} #${item.sequence}` : item.status === "already_generated" ? `已生成 ${item.kind} #${item.existingSequence}` : item.status === "ratio_too_far" ? "比例超限" : "分辨率未知"}</b><span>{item.willFillGap ? "补号" : item.wasStretched ? "将拉伸" : "无需重采样"}</span>{item.ratioDeviationPercent !== undefined ? <small>偏差 {item.ratioDeviationPercent}%</small> : null}{item.warnings.map((warning) => <small className="warning" key={warning}>{warning}</small>)}</div></article>)}</section>
              <section className="trash-panel legacy-migration-panel"><div className="section-heading"><div><h3>旧 1280 文件迁移</h3><p>只在你确认后，把已登记的旧文件移动到 `DataBase\\16x9`；执行日志可用于回滚，目标已存在时不会覆盖。</p></div><div className="action-row"><button type="button" className="ghost-action" onClick={() => void preflightLegacy1280Migration()} disabled={busy}><RefreshCw size={16} />预检旧文件</button><button type="button" className="ghost-action" onClick={() => void migrateLegacy1280Files()} disabled={busy || !legacyMigrationPlan?.summary.ready}>迁移 {legacyMigrationPlan?.summary.ready || 0} 项</button></div></div>{legacyMigrationPlan ? <div className="export-list">{legacyMigrationPlan.items.map((item) => <article className={`export-row ${item.status}`} key={item.derivativeId}><div><strong>#{item.sequence} {item.title}</strong><small>{item.sourcePath} → {item.targetPath}</small></div><div><b>{item.status}</b><span>{item.reason}</span></div></article>)}</div> : null}</section>
              {derivativeErrors.length ? <section className="ocr-error-panel"><h3>导出错误</h3><ul>{derivativeErrors.map((error) => <li key={error}>{error}</li>)}</ul></section> : null}
            </> : <section className="empty-state"><ImageIcon size={42} /><h2>尚未选择导出图片</h2><p>回到图片库，选择图片后点击“派生图导出预检”。</p><button className="primary-action" onClick={() => setView("home")}>去图片库选择</button></section>}
          </section>
        ) : view === "duplicates" ? (
          <section className="panel duplicate-panel">
            <div className="panel-heading"><Layers3 size={22} aria-hidden="true" /><div><h2>重复审查</h2><p>仅当画面感知指纹接近且 OCR 文案高度相似时才进入候选组。系统不自动删除，参考图按分辨率和文件大小优先排序。</p></div></div>
            <section className="scope-filter-controls" aria-label="重复审查范围">
              <label>文件夹<select value={duplicateFolderFilter} disabled={duplicateSubmitting || duplicateJobId !== null} onChange={(event) => { setDuplicateFolderFilter(event.target.value); setDuplicateGroups([]); setDuplicateErrors([]); }}><option value="">全部已入库文件夹</option>{allFolders.filter((folder) => !folder.toLowerCase().endsWith("\\database")).map((folder) => <option key={folder} value={folder}>{folder}</option>)}</select></label>
              <label>图片比例<select value={duplicateRatioFilter} disabled={duplicateSubmitting || duplicateJobId !== null} onChange={(event) => { setDuplicateRatioFilter(event.target.value); setDuplicateGroups([]); setDuplicateErrors([]); }}><option value="">全部比例</option><option value="16:9">16:9</option><option value="1:1">1:1</option><option value="9:16">9:16</option><option value="other">其他或未知</option></select></label>
              <p>重复比较只在当前文件夹和比例范围内进行，不会跨范围把不同素材归为候选。</p>
            </section>
            <div className="action-row"><button type="button" className="primary-action" onClick={() => void analyzeDuplicates()} disabled={duplicateSubmitting || duplicateJobId !== null}><RefreshCw size={18} />{duplicateSubmitting ? "正在提交" : duplicateJobId !== null ? "后台检测中" : "检测画面与文案重复"}</button><button type="button" className="ghost-action" onClick={() => void backfillDataBaseAuthors()} disabled={busy}><RefreshCw size={18} />回填 DataBase 作者</button></div>
            {duplicateSubmitting || duplicateJobId !== null ? <section className="ocr-progress-panel duplicate-progress-panel" aria-label="重复审查进度"><div className="ocr-progress-head"><strong>{duplicateSubmitting ? "正在提交重复审查" : duplicateJob?.status === "queued" ? "重复审查等待执行" : duplicateJob?.status === "cancel_requested" ? "正在停止重复审查" : "后台重复审查中"}</strong><span>{duplicateJob?.progress_total ? `${duplicateJob.progress_current}/${duplicateJob.progress_total}` : `任务 #${duplicateJobId || "-"}`}</span></div><div className="ocr-progress-track"><div style={{ width: duplicateJob?.progress_total ? `${Math.round((duplicateJob.progress_current / Math.max(1, duplicateJob.progress_total)) * 100)}%` : "8%" }} /></div><p>{duplicateJob?.current_path || "任务在服务端后台运行，当前终端可继续使用其他功能。"}</p><button type="button" className="ghost-action" onClick={() => void cancelDuplicateAnalysis()} disabled={!duplicateJob || duplicateJob.status === "cancel_requested"}>{duplicateJob?.status === "cancel_requested" ? "停止中" : "停止任务"}</button></section> : null}
            {duplicateGroups.length ? <section className="duplicate-groups">{duplicateGroups.map((group, index) => <article className="duplicate-group" key={`${group.items[0]?.id}-${index}`}><div className="section-heading"><div><h3>候选组 {index + 1}</h3><p>共 {group.items.length} 张。第一张为建议参考图，作者不会被自动合并或覆盖。</p></div><span>{group.pairs[0] ? `画面差异 ${group.pairs[0].visualDifferencePercent.toFixed(2)}% · 文案 ${group.pairs[0].textSimilarity}%` : "候选重复"}</span></div><div className="duplicate-items">{group.items.map((asset, itemIndex) => <article key={asset.id} className={itemIndex === 0 ? "duplicate-item reference" : "duplicate-item"}><img src={asset.imageUrl} alt={asset.detectedName || asset.libraryName} /><div><strong>{asset.detectedName || asset.libraryName}</strong><small>{asset.resolution} · {asset.fileSizeMb.toFixed(2)} MB</small><small>作者：{asset.displayAuthor || "未识别作者"}</small><small>{asset.sourcePath}</small><button type="button" className="ghost-action" onClick={() => void openDetail(asset)}>查看详情</button><button type="button" className="primary-action" onClick={() => void retainDuplicateAsset(asset, group)} disabled={busy}>保留这一张</button></div></article>)}</div></article>)}</section> : <section className="empty-state"><Layers3 size={42} /><h2>{duplicateJobId !== null ? "正在后台生成候选组" : "尚未生成候选组"}</h2><p>{duplicateJobId !== null ? "检测期间可切换到其他页面继续使用网站，完成后会自动显示结果。" : "点击“检测画面与文案重复”后，系统会复用未变化图片的画面指纹并在后台生成候选。"}</p></section>}
            {duplicateErrors.length ? <section className="ocr-error-panel"><h3>无法检测的文件</h3><ul>{duplicateErrors.map((error) => <li key={error}>{error}</li>)}</ul></section> : null}
            <section className="trash-panel"><div className="section-heading"><div><h3>重复审查历史</h3><p>撤销会恢复该次保留时隐藏的所有数据库记录，不移动或删除 NAS 文件。</p></div><button type="button" className="ghost-action" onClick={() => void loadDuplicateHistory()} disabled={busy}><RefreshCw size={16} />刷新</button></div>{duplicateHistory.length ? <div className="trash-list">{duplicateHistory.map((item) => <div className="trash-derivative-row" key={`${item.keepAssetId}-${item.updatedAt}`}><span><strong>保留图片 #{item.keepAssetId}</strong><small>{item.updatedAt} · 已隐藏 {item.hiddenCount} 条记录</small></span><button type="button" className="ghost-action" onClick={() => void undoDuplicateRetain(item)} disabled={busy}>撤销并恢复</button></div>)}</div> : <p className="empty-copy">暂无可撤销的重复审查操作。</p>}</section>
          </section>
        ) : view === "folderExport" ? (
          <section className="panel folder-export-panel">
            <div className="panel-heading"><FolderInput size={22} aria-hidden="true" /><div><h2>按文件夹导出</h2><p>选择已入库文件夹后，系统会按每张原图的最接近比例输出唯一 JPG 派生图，并写入作者和获取渠道。</p></div></div>
            <section className="folder-export-controls">
              <label>源文件夹<select value={folderExportPath} onChange={(event) => { setFolderExportPath(event.target.value); setFolderDerivativePlan(null); setFolderDerivativeErrors([]); }}><option value="">请选择已入库文件夹</option>{allFolders.filter((folder) => !folder.toLowerCase().endsWith("\\database")).map((folder) => <option key={folder} value={folder}>{folder}</option>)}</select></label>
              <label>作者信息（可选）<input list="folder-derivative-author-history" value={folderDerivativeAuthor} onChange={(event) => setFolderDerivativeAuthor(event.target.value)} placeholder="留空时按来源文件夹自动识别" maxLength={120} /><datalist id="folder-derivative-author-history">{savedDerivativeAuthors.map((author) => <option value={author} key={author} />)}</datalist></label>
              <label>获取渠道（必填）<input list="folder-derivative-channel-history" value={folderDerivativeChannel} onChange={(event) => setFolderDerivativeChannel(event.target.value)} placeholder="例如：抖音、素材平台、合作方" maxLength={120} required /><datalist id="folder-derivative-channel-history">{savedDerivativeChannels.map((channel) => <option value={channel} key={channel} />)}</datalist></label>
              <button type="button" className="primary-action" onClick={() => void preflightFolderDerivatives()} disabled={busy || !folderExportPath}><RefreshCw size={18} />检测待转移文件</button>
            </section>
            {folderDerivativePlan ? <>
              <section className="metric-row"><div><span>{folderDerivativePlan.summary.total}</span>文件夹图片</div><div><span>{folderDerivativePlan.summary.ready}</span>待导出</div><div><span>{folderDerivativePlan.summary.alreadyGenerated}</span>已导出</div><div><span>{folderDerivativePlan.summary.skipped}</span>已跳过</div></section>
              <div className="action-row"><button type="button" className="primary-action" onClick={() => void generateFolderDerivatives()} disabled={busy || !folderDerivativePlan.summary.ready}><Download size={18} />导出待转移 {folderDerivativePlan.summary.ready} 项</button><button type="button" className="ghost-action" onClick={() => void preflightFolderDerivatives()} disabled={busy}><RefreshCw size={18} />重新检测</button></div>
              <section className="export-list">{folderDerivativePlan.items.map((item) => <article className={`export-row ${item.status}`} key={item.id}><div><strong>{item.title}</strong><p>{item.nativeSize ? `${item.kind} 原尺寸，不重采样` : `源图：${item.sourceResolution}${item.wasStretched ? " · 将拉伸" : ""}`} · 作者：{item.author}</p><small>{item.outputPath || item.existingPath || item.message}</small></div><div><b>{item.status === "ready" ? `${item.kind} 待导出 #${item.sequence}` : item.status === "already_generated" ? `已导出 ${item.kind} #${item.existingSequence}` : item.status === "ratio_too_far" ? "比例超限" : "分辨率未知"}</b><span>{item.message}</span>{item.warnings.map((warning) => <small className="warning" key={warning}>{warning}</small>)}</div></article>)}</section>
              {folderDerivativeErrors.length ? <section className="ocr-error-panel"><h3>文件夹导出错误</h3><ul>{folderDerivativeErrors.map((error) => <li key={error}>{error}</li>)}</ul></section> : null}
            </> : <section className="empty-state"><FolderInput size={42} /><h2>选择一个已入库文件夹</h2><p>点击“检测待转移文件”后会显示已导出、待导出和不能自动导出的图片。</p></section>}
          </section>
        ) : view === "about" ? (
          <section className="panel about-panel">
            <div className="panel-heading"><Info size={22} aria-hidden="true" /><div><h2>关于素材库</h2><p>这是一个面向局域网和 NAS 图片素材的本地审查工具，重点是快速浏览、检索、识别文字、记录使用情况，并保持部署方式尽量轻。</p></div></div>
            <section className="about-hero">
              <div><p className="eyebrow">站长札记</p><h3>先让图片找得到，再让审查变轻松。</h3><p>项目当前以 Windows 电脑运行服务为主，浏览器访问同一台服务时共享同一个 SQLite 数据库。OCR 结果、点击次数、下载次数和扫描入库记录都会写入这份数据库。</p></div>
              <div className="about-badge"><span>本地运行</span><strong>FastAPI + React + SQLite</strong></div>
            </section>
            <section className="job-status-panel" aria-label="正在处理的事务">
              <div className="job-status-heading"><div><p className="eyebrow">正在处理的事务</p><h3>{jobStatus?.active.length ? `正在执行 ${jobStatus.active.length} 项任务` : jobStatus?.pending.length ? `等待执行 ${jobStatus.pending.length} 项任务` : "当前没有执行中的任务"}</h3><p>{jobStatus ? `待处理 ${jobStatus.queue_length} 项${jobStatus.failed ? ` · 待排查失败 ${jobStatus.failed} 项` : ""}` : "正在读取任务队列状态。"}</p></div><button type="button" className="ghost-action" onClick={() => void loadJobStatus()}><RefreshCw size={16} />刷新</button></div>
              {jobStatus && (jobStatus.active.length || jobStatus.pending.length) ? <div className="job-status-list">{[...jobStatus.active, ...jobStatus.pending].map((job) => <div key={job.id}><strong>{job.status === "queued" ? "等待执行" : job.status === "cancel_requested" ? "正在停止" : job.job_type === "scan" ? "扫描" : job.job_type === "ocr" ? "OCR 识别" : job.job_type === "duplicate" ? "重复审查" : job.job_type}</strong><span>{job.source_name || (job.asset_id ? `图片 #${job.asset_id}` : `任务 #${job.id}`)}</span><button type="button" className="job-cancel-button" disabled={job.status === "cancel_requested"} onClick={() => void cancelBackgroundJob(job)}>{job.status === "cancel_requested" ? "停止中" : "停止"}</button><small>{job.status === "queued" ? "等待工作者领取" : job.status === "cancel_requested" ? "已请求停止" : `${job.progress_total ? `${job.progress_current}/${job.progress_total}` : "正在准备"}${job.current_path ? ` · ${job.current_path}` : ""}`}</small></div>)}</div> : null}
            </section>
            <section className="service-status-panel" aria-label="服务电脑状态">
              <div className="service-status-heading"><div><p className="eyebrow">服务电脑状态</p><h3>{serviceRestarting ? "正在重启服务" : serviceStatus ? `素材库 V${serviceStatus.version} 运行中` : "暂时无法读取服务状态"}</h3><p>{serviceStatus ? `${serviceStatus.hostname} · Python ${serviceStatus.pythonVersion} · 已运行 ${formatDuration(serviceStatus.uptimeSeconds)}` : "状态每 15 秒自动刷新；服务不可用时请先检查运行素材库的电脑。"}</p></div><button type="button" className="ghost-action service-restart-button" onClick={() => void restartService()} disabled={busy || serviceRestarting || !serviceStatus?.restartSupported} title={serviceStatus && !serviceStatus.restartSupported ? "当前启动方式需要在服务终端或容器管理器中重启" : ""}><RefreshCw size={18} />{serviceRestarting ? "正在重启" : serviceStatus && !serviceStatus.restartSupported ? "请在终端重启" : "重启素材库服务"}</button></div>
              {serviceStatus ? <div className="service-metrics"><div><span>内存可用</span><strong>{serviceStatus.memoryAvailableMb ? `${serviceStatus.memoryAvailableMb} / ${serviceStatus.memoryTotalMb} MB` : "无法读取"}</strong></div><div><span>磁盘可用</span><strong>{serviceStatus.diskFreeGb} / {serviceStatus.diskTotalGb} GB</strong></div><div><span>CPU 使用率</span><strong>{serviceStatus.cpuUsagePercent === null ? "正在采样" : `${serviceStatus.cpuUsagePercent}%`}</strong></div><div><span>服务进程</span><strong>PID {serviceStatus.pid}</strong></div></div> : null}
            </section>
            <section className="about-grid" aria-label="功能介绍">
              <article><h3>图片流审查</h3><p>主页提供连续加载的图片流，支持按批次继续加载、快进快退、保留滚动位置，并可点击图片打开详情小窗。</p></article>
              <article><h3>检索与筛选</h3><p>支持标题、OCR 文本、标签、日期时间、文件夹筛选，也支持模糊搜索和是否纳入库中命名的开关。</p></article>
              <article><h3>本地 OCR</h3><p>OCR（光学字符识别）始终读取 NAS 原图；比例派生图与原图共用同一条数据库记录，因此先导出再识别也会同步更新。识别文本含书名号《…》时会自动隐藏图片并生成即时报告。</p></article>
              <article><h3>批量处理</h3><p>可以选择当前显示图片、批量识别文字，并让浏览器逐项下载多个原图或当前比例派生图；每项单独报告成功或失败，不再生成 ZIP 压缩包。</p></article>
              <article><h3>NAS 连接</h3><p>配置页可保存多个 NAS 或本地路径数据源。每个来源都要先通过只读验证，之后可选择仅手动扫描，或开启定时自动扫描与自动 OCR。</p></article>
              <article><h3>自动恢复</h3><p>服务长时间运行后若意外退出，启动监督会自动重启并多次尝试恢复；浏览器短暂断开后可直接重试，无需更换访问方式。</p></article>
            </section>
            <section className="release-card access-mode-card" aria-label="浏览器多文件下载"><span>下载</span><div><h3>浏览器多文件下载</h3><p>单图与批量操作均通过浏览器逐个保存文件，不打包、不选择本地目录，也不依赖证书认证。首次批量下载时请允许站点下载多个文件。</p></div></section>
            <section className="changelog">
              <div className="section-heading"><div><h3>更新日志</h3><p>以下记录按当前项目实际迭代整理，便于之后迁移和排查。</p></div></div>
              <article className="release-card"><span>V2.0.12</span><div><h3>数据库移除关联记录修复</h3><p>修复带标签、逐图确认、画面哈希、重复审查或任务历史的图片无法“仅从数据库移除”的外键错误。移除会保留可恢复快照；正在 OCR 或排队的图片会提示先结束任务，避免任务与删除冲突。</p></div></article>
              <article className="release-card"><span>V2.0.11</span><div><h3>隐藏筛选开关修正</h3><p>“显示隐藏图片”关闭时，任何筛选都不显示隐藏项；开启后才显示，作者和文件夹筛选不再绕过此开关。</p></div></article>
              <article className="release-card"><span>V2.0.10</span><div><h3>隐藏图片筛选与标记</h3><p>隐藏图片在主图卡左上角显示“已隐藏”及规则说明。筛选区新增“显示隐藏图片”开关；开启时显示隐藏项，关闭时不显示。</p></div></article>
              <article className="release-card"><span>V2.0.9</span><div><h3>主页布局与删除反馈</h3><p>主页日期筛选、全周期和重置筛选在宽屏下保持紧凑同排；批量操作栏补足列数，下载按钮不再单独换行。删除共享文件夹图片失败时会显示服务端返回的具体原因，便于确认路径或权限问题。</p></div></article>
              <article className="release-card"><span>V2.0.8</span><div><h3>范围批处理与操作反馈</h3><p>识别中心和重复审查均可按已入库文件夹及图片比例限定批量范围；OCR 始终读取原图，不会因原始分辨率不同生成或替换文件。删除、隐藏和重复保留在服务端成功后立即从当前列表移除，并显示成功或失败提示。</p></div></article>
              <article className="release-card"><span>V2.0.7</span><div><h3>入库日期筛选与时间显示修复</h3><p>修复新旧入库时间格式混用时，单日筛选会漏掉 ISO 8601（包含时区的标准时间格式）记录的问题。今天、昨天和手选同一天都按入库日期两端包含筛选；快捷日期按钮会准确显示当前选中项。详情页的入库时间和导出时间改为本地可读格式，不再显示原始时间戳。</p></div></article>
              <article className="release-card"><span>V2.0.6</span><div><h3>数据源分辨率补全与导出修复</h3><p>修复 SMB 数据源扫描把图片分辨率长期写成“未知”的致命问题。新扫描会读取 PNG、JPG、WebP、GIF 和 BMP 的实际尺寸；历史记录在重新检测时会补全。即使尚未重扫，按文件夹导出预检也会只读原图、补写数据库分辨率后继续生成，不会移动或修改 NAS 原图。此前的 OCR 串行化、重启租约释放、失败扫描退避与数据源并发保存保护继续保留。</p></div></article>
              <article className="release-card"><span>V2.0.4</span><div><h3>扫描停止与排队任务可见性</h3><p>数据源卡片增加“停止扫描”：等待中的任务立即取消，运行中的任务在当前文件或目录遍历检查点结束后停止。关闭自动定时扫描会同时取消该来源尚未执行的扫描。“关于素材库”的事务面板新增等待项和正在停止状态，不再只显示运行中的任务。</p></div></article>
              <article className="release-card"><span>V2.0.3</span><div><h3>数据源控制与事务可见性</h3><p>数据源可关闭自动定时扫描，保留手动扫描；已添加的数据源可直接修改名称、路径、扫描间隔、递归、自动 OCR 和默认作者。扫描状态轮询改为运行中每两秒、排队中每五秒，减少服务日志噪声。“关于素材库”顶部增加正在处理事务、队列数量和实时进度。</p></div></article>
              <article className="release-card"><span>V2.0.2</span><div><h3>多终端任务队列优化</h3><p>同一 SQLite 数据库中的扫描与 OCR 队列改为工作者租约加心跳：多个客户端同时触发同一数据源时只保留一项任务，所有终端都能看到公共队列位置和正在进行的扫描。服务进程异常中断后，超过五分钟没有心跳的工作者可由下一台服务进程安全接管并重新排队。仍建议同一台服务电脑一次只启动一种服务模式。</p></div></article>
              <article className="release-card"><span>V2.0.1</span><div><h3>数据源验证、扫描反馈、筛选与标注体验修复</h3><p>数据源新增后会立即进行服务端只读访问验证，并可随时重新验证或删除扫描配置；删除仅解除数据源关联，已入库记录和 NAS 文件均保留。点击扫描会显示等待、进度、成功数量或具体失败原因。日期快捷筛选固定显示并修复日历浮层层级。逐图标注首次打开文件夹会跳过已有标签或已确认的图片；HTTP 兼容入口会关闭新的本地目录复制，仅保留原有下载方式。</p></div></article>
              <article className="release-card"><span>V2.0.0</span><div><h3>素材库 V2 最终组装</h3><p>完成多数据源与持久化任务队列、标签库与逐图标注、主图库组合筛选、三种比例派生图、重复审查与可恢复删除。发布包不包含本地配置、运行数据、日志或证书私钥。</p></div></article>
              <article className="release-card"><span>V1.0.13</span><div><h3>原图与派生文件一致性</h3><p>将原图移入回收站时，已登记且存在的 1280×720 文件会同步移入回收站；恢复时会先检查全部路径，避免覆盖。数据库审查新增未登记 1280 文件检测与回收入口。发布包不再携带 NAS 本地密码配置。</p></div></article>
              <article className="release-card"><span>V1.0.12</span><div><h3>已导出文件信息更新</h3><p>1280×720 导出页可对已导出的文件单独更新作者、获取渠道或同时更新两项。未填写的字段保持原值，更新会同步写回 SQLite 与图片元数据，不重新导出图片。</p></div></article>
              <article className="release-card"><span>V1.0.11</span><div><h3>渠道信息与卡片精简</h3><p>1280×720 导出新增必填的获取渠道，记录在 SQLite 并写入图片元数据“备注”；主页卡片标题限制为一行，底部改为入库日期和获取渠道，保留未标注与未 OCR 状态。</p></div></article>
              <article className="release-card"><span>V1.0.10</span><div><h3>重复审查精度与作者兜底</h3><p>重复审查改用彩色结构差异并在每次检测时重算，避免低精度灰度指纹将不同画面显示为零差异。作者按导出记录、图片元数据、来源文件夹依次回退；回填入口移至重复审查页。服务状态将 OCR 安装状态替换为 CPU 使用率。</p></div></article>
              <article className="release-card"><span>V1.0.9</span><div><h3>作者回填与导航整理</h3><p>扫描 DataBase 文件时会读取已写入的图片作者元数据并保存到 SQLite；配置页可为历史文件手动回填作者。左侧导航改为上方功能区与左下角设置区，NAS 配置与关于素材库固定在底部。</p></div></article>
              <article className="release-card"><span>V1.0.8</span><div><h3>重复审查与可恢复删除</h3><p>新增“重复审查”页面，以画面感知哈希和 OCR 文案双重条件筛出候选组，始终由人工决定。详情页支持回收生成的 1280 文件、仅移除数据库记录或将原图移入 NAS 回收站；回收站可恢复。修改已导出素材的识别命名时会同步重命名 DataBase 文件。</p></div></article>
              <article className="release-card"><span>V1.0.7</span><div><h3>文件夹批量导出</h3><p>新增独立的文件夹 1280×720 导出页，可自动检测待转移项目、选择作者并批量输出。原图已为 1280×720 时不再缩放，仅写入作者元数据后输出。</p></div></article>
              <article className="release-card"><span>V1.0.6</span><div><h3>连续加载突破 200 条</h3><p>主页加载改为自动分段读取并合并显示，后端仍保留单次 200 条保护。继续加载、快进在素材数量超过 200 条时可正常工作。</p></div></article>
              <article className="release-card"><span>V1.0.5</span><div><h3>失败重试与服务状态</h3><p>OCR 失败原因会写入数据库并在识别中心长期显示，可一键重试失败项目；关于页面新增运行电脑状态监控与网页内重启服务功能。</p></div></article>
              <article className="release-card"><span>V1.0.4</span><div><h3>识别中心排除派生目录</h3><p>识别中心复用主界面的排除路径设置。开启后，识别概览、检测未识别项目与统一 OCR 队列均会排除导出目录，避免对 1280×720 版本重复识别。</p></div></article>
              <article className="release-card"><span>V1.0.3</span><div><h3>导出目录排除筛选</h3><p>主页面默认排除 DataBase 导出目录，避免原图与 1280×720 版本重复浏览；路径可编辑，也可随时停用，不会删除数据库或 NAS 文件。</p></div></article>
              <article className="release-card"><span>V1.0.2</span><div><h3>处理进度与导出操作区优化</h3><p>所有带等待状态的操作统一显示处理进度条；OCR 保留逐张真实进度。1280×720 导出页的生成与返回按钮在桌面端并列显示，窄屏自动适配。</p></div></article>
              <article className="release-card"><span>V1.0.1</span><div><h3>作者元数据兼容性修复</h3><p>修复中文作者在 Windows 图片属性中显示为“???”的问题：中文作者改写入 XPAuthor 字段，已导出版本可通过“更新已导出作者”重写修复。保留作者历史和统一转为 JPG 导出能力。</p></div></article>
              <article className="release-card"><span>v0.10</span><div><h3>派生版本输出目录迁移</h3><p>1280×720 版本默认改为写入图片素材下的 DataBase 目录；旧目录中已登记的派生文件可经确认后迁移，并在成功移动后更新数据库路径。</p></div></article>
              <article className="release-card"><span>v0.9</span><div><h3>数据库审查与 1280×720 派生版本</h3><p>新增失效源路径审查、仅删数据库记录的回收站；支持预检并导出 1280×720 版本，记录编号缺口、作者、拉伸状态和输出文件信息。</p></div></article>
              <article className="release-card"><span>v0.8</span><div><h3>卡片信息精简与项目标识</h3><p>主页图片卡片不再显示路径或额外 OCR 正文；未标注、未识字等待处理状态仅在需要时显示于底部右侧。左侧的 Z 标记更新为项目旋纹标识。</p></div></article>
              <article className="release-card"><span>v0.7</span><div><h3>项目说明与无效词维护</h3><p>新增左侧“关于项目”页面；识别中心增加 OCR 无效词增删改查，清洗词表从硬编码升级为本地配置文件。</p></div></article>
              <article className="release-card"><span>v0.6</span><div><h3>识别中心与筛选增强</h3><p>新增未识别项目检测、统一识别、进度条、错误记录；补充模糊搜索、库名搜索开关、日期和文件夹筛选、按前面数字排序。</p></div></article>
              <article className="release-card"><span>v0.5</span><div><h3>本地 OCR 标题生成</h3><p>接入本机 PaddleOCR，识别结果写入数据库；标题清洗会去掉换行、空格、临时路径噪声和常见无效短剧角标文案。</p></div></article>
              <article className="release-card"><span>v0.4</span><div><h3>批量选择</h3><p>加入多选、选择当前显示和清空选择等素材批量操作基础能力。</p></div></article>
              <article className="release-card"><span>v0.3</span><div><h3>SQLite 入库与统计</h3><p>扫描图片后保存路径、命名、分辨率、比例、大小、入库时间、点击次数和下载次数。</p></div></article>
              <article className="release-card"><span>v0.2</span><div><h3>PS5 风格图片流</h3><p>完成深色玻璃感界面、图片流、筛选工具区和固定位置详情弹窗。</p></div></article>
              <article className="release-card"><span>v0.1</span><div><h3>NAS 连接验证</h3><p>完成 NAS 配置保存、连接测试和图片列表读取的最小可行链路。</p></div></article>
            </section>
            <CopyrightNotice />
          </section>
        ) : (
          <>
            <section className="control-deck"><div className="search-box"><Search size={18} aria-hidden="true" /><input value={prefs.query} onChange={(event) => patchPrefs({ query: event.target.value, loadedCount: prefs.batchSize })} placeholder="搜索识别命名、OCR、标签" /><button type="button" className={prefs.fuzzySearch ? "fuzzy-toggle active" : "fuzzy-toggle"} onClick={() => patchPrefs({ fuzzySearch: !prefs.fuzzySearch, loadedCount: prefs.batchSize })}>模糊</button><button type="button" className={prefs.includeLibrarySearch ? "fuzzy-toggle active" : "fuzzy-toggle"} onClick={() => patchPrefs({ includeLibrarySearch: !prefs.includeLibrarySearch, loadedCount: prefs.batchSize })}>库名</button></div><div className="version-switch" aria-label="图片比例"><button type="button" className={prefs.mediaVersion === "16:9" ? "active" : ""} onClick={() => patchPrefs({ mediaVersion: "16:9", loadedCount: prefs.batchSize })}>16:9</button><button type="button" className={prefs.mediaVersion === "1:1" ? "active" : ""} onClick={() => patchPrefs({ mediaVersion: "1:1", loadedCount: prefs.batchSize })}>1:1</button><button type="button" className={prefs.mediaVersion === "9:16" ? "active" : ""} onClick={() => patchPrefs({ mediaVersion: "9:16", loadedCount: prefs.batchSize })}>9:16</button><button type="button" className={prefs.mediaVersion === "raw" ? "active" : ""} onClick={() => patchPrefs({ mediaVersion: "raw", loadedCount: prefs.batchSize })}>生肉</button></div><label className="compact-field"><ArrowDownWideNarrow size={18} aria-hidden="true" /><select value={prefs.sortKey} onChange={(event) => patchPrefs({ sortKey: event.target.value as SortKey })}><option value="imported_desc">入库时间优先</option><option value="clicks_desc">点击次数优先</option><option value="downloads_desc">下载次数优先</option><option value="size_desc">文件大小优先</option><option value="title_asc">库中命名 A-Z</option><option value="front_number_desc">前面数字从大到小</option><option value="front_number_asc">前面数字从小到大</option></select></label><label className="compact-field"><SlidersHorizontal size={18} aria-hidden="true" /><input type="number" min={5} max={100} value={prefs.batchSize} onChange={(event) => patchPrefs({ batchSize: Number(event.target.value), loadedCount: Number(event.target.value) })} /></label><button className="ghost-action" type="button" onClick={() => setView("config")}><Network size={18} />配置/扫描</button></section>
            <section className="filter-deck">
              <label>入库日期<DateRangePicker from={prefs.importedFrom} to={prefs.importedTo} onChange={(importedFrom, importedTo) => patchPrefs({ importedFrom, importedTo, loadedCount: prefs.batchSize })} /></label>
              <label>文件夹<select value={prefs.folderFilter} onChange={(event) => patchPrefs({ folderFilter: event.target.value, loadedCount: prefs.batchSize })}><option value="">全部文件夹</option>{homeFolders.map((folder) => <option key={folder} value={folder}>{folder}</option>)}</select></label>
              <label>作者<select value={prefs.authorFilter} onChange={(event) => patchPrefs({ authorFilter: event.target.value, loadedCount: prefs.batchSize })}><option value="">全部作者</option>{homeAuthors.map((author) => <option key={author} value={author}>{author}</option>)}</select></label>
              <label>获取渠道<select value={prefs.channelFilter} onChange={(event) => patchPrefs({ channelFilter: event.target.value, loadedCount: prefs.batchSize })}><option value="">全部渠道</option>{homeChannels.map((channel) => <option key={channel} value={channel}>{channel}</option>)}</select></label>
              <label className="exclude-path-field">排除路径<input value={prefs.excludePath} onChange={(event) => patchPrefs({ excludePath: event.target.value, loadedCount: prefs.batchSize })} placeholder="输入不纳入浏览结果的文件夹路径" /></label>
              <div className="filter-actions"><button type="button" aria-pressed={prefs.excludePathEnabled} className={prefs.excludePathEnabled ? "path-exclude-toggle active" : "path-exclude-toggle"} disabled={!prefs.excludePath.trim()} onClick={() => patchPrefs({ excludePathEnabled: !prefs.excludePathEnabled, loadedCount: prefs.batchSize })}>{prefs.excludePathEnabled ? "已排除该路径" : "启用排除路径"}</button><button type="button" aria-pressed={prefs.showHidden} className={prefs.showHidden ? "show-hidden-toggle active" : "show-hidden-toggle"} onClick={() => patchPrefs({ showHidden: !prefs.showHidden, loadedCount: prefs.batchSize })}>{prefs.showHidden ? "正在显示隐藏图片" : "显示隐藏图片"}</button><button type="button" className="ghost-action" onClick={() => patchPrefs({ importedFrom: "", importedTo: "", folderFilter: "", authorFilter: "", channelFilter: "", excludePath: defaultPrefs.excludePath, excludePathEnabled: true, showHidden: false, selectedTags: [], tagMode: "and", loadedCount: prefs.batchSize })}>重置筛选</button></div>
            </section>
            <section className="tag-strip" aria-label="标签筛选"><span><Tags size={16} aria-hidden="true" />标签</span><div className="tag-mode"><button type="button" className={prefs.tagMode === "and" ? "selected" : ""} onClick={() => patchPrefs({ tagMode: "and", loadedCount: prefs.batchSize })}>AND</button><button type="button" className={prefs.tagMode === "or" ? "selected" : ""} onClick={() => patchPrefs({ tagMode: "or", loadedCount: prefs.batchSize })}>OR</button></div>{allTags.length ? allTags.map((tag) => <button key={tag} className={prefs.selectedTags.includes(tag) ? "selected" : ""} onClick={() => toggleTag(tag)} type="button">{tag}</button>) : <em>扫描入库后显示标签</em>}</section>
            <section className="metric-row"><div><span>{total}</span>匹配图片</div><div><span>{assets.length}</span>当前显示</div><div><span>{prefs.batchSize}</span>每批数量</div><div><span>{prefs.selectedTags.length}</span>已选标签</div></section><section className="batch-toolbar"><div><strong>{selectedIds.length}</strong><span>已选择</span></div><button type="button" onClick={selectVisibleAssets}>选择当前显示</button><button type="button" onClick={clearSelectedAssets}>清空选择</button><button type="button" onClick={() => void preflightDerivatives()} disabled={busy || !selectedIds.length}><ImageIcon size={16} />派生图导出预检</button><button type="button" onClick={recognizeSelectedAssetsTextBatch} disabled={busy || !selectedIds.length}><FileText size={16} />识别所选文字</button><button type="button" className="primary-action" onClick={downloadSelectedAssets} disabled={busy || !selectedIds.length}><Download size={16} />浏览器多文件下载</button></section>
            {ocrProgress?.active ? <section className="ocr-progress-panel" aria-label="OCR 识别进度"><div className="ocr-progress-head"><strong>OCR 识别进度</strong><span>{ocrProgress.current}/{ocrProgress.total}，成功 {ocrProgress.succeeded}，失败 {ocrProgress.failed}，自动隐藏 {ocrProgress.autoHidden}</span></div><div className="ocr-progress-track"><div style={{ width: `${Math.round((ocrProgress.current / Math.max(1, ocrProgress.total)) * 100)}%` }} /></div><p>{ocrProgress.label}</p></section> : null}
            {assets.length ? <section className="asset-grid" aria-label="图片流">{assets.map((asset) => <article className={`asset-card ${selectedIds.includes(asset.id) ? "selected-card" : ""}`} key={asset.id} onClick={() => void openDetail(asset)}>{asset.hidden ? <div className="hidden-card-marker"><span>已隐藏</span><button type="button" aria-label="说明隐藏图片的显示规则" title="显示规则" onClick={(event) => { event.stopPropagation(); notifyAction("info", "启用“显示隐藏图片”后才会显示该项目。"); }}><Info size={14} aria-hidden="true" /></button></div> : null}<button className="select-toggle" type="button" onClick={(event) => toggleAssetSelection(asset.id, event)} aria-label="选择图片">{selectedIds.includes(asset.id) ? "已选" : "选择"}</button><div className={`asset-art real-art ${assetAspectClass(asset)}`}><img src={asset.imageUrl} alt={asset.libraryName} loading="lazy" onError={(event) => { event.currentTarget.style.display = "none"; }} /><div className="image-fallback"><ImageIcon size={36} /><span>图片读取失败</span></div></div><div className="asset-card-copy"><h3 title={asset.detectedName || asset.libraryName}>{asset.detectedName || asset.libraryName}</h3><div><span>{formatCardDate(asset.importedAt)}</span><span title={asset.displayAuthor}>{asset.displayAuthor || "未识别作者"}</span></div></div></article>)}</section> : <section className="empty-state"><ImageIcon size={42} /><h2>还没有入库图片</h2><p>进入配置页，先点“连接测试”，确认可访问后再点“扫描入库”。如果你填的是 NAS 管理后台页面，而不是图片目录，扫描结果可能为 0。</p><button className="primary-action" onClick={() => setView("config")}>去配置扫描</button></section>}
            <section className="load-panel"><button type="button" onClick={() => jumpBatch("prev")}><ChevronLeft size={18} />快退 {prefs.batchSize}</button><button type="button" onClick={loadNextBatch} disabled={assets.length >= total}><Layers3 size={18} />继续加载 {prefs.batchSize} 张</button><button type="button" onClick={() => jumpBatch("next")} disabled={assets.length >= total}>快进 {prefs.batchSize}<ChevronRight size={18} /></button></section>
            <CopyrightNotice />
          </>
        )}
      </section>
      {selectedAsset ? (
        <div className="modal-backdrop" onMouseDown={closeFromBackdrop}>
          <button className="modal-nav prev" type="button" onClick={() => openAdjacentAsset("prev")} aria-label="上一张"><ChevronLeft size={24} /></button>
          <button className="modal-nav next" type="button" onClick={() => openAdjacentAsset("next")} aria-label="下一张"><ChevronRight size={24} /></button>
          <section className="detail-modal" role="dialog" aria-modal="true" aria-label="图片详情">
            <button className="modal-close" onClick={closeDetail} aria-label="关闭详情"><X size={20} /></button>
            <div className="detail-preview real-detail"><img src={selectedAsset.imageUrl} alt={selectedAsset.libraryName} /></div>
            <div className="detail-content">
              <p className="eyebrow">图片详情</p>
              <h2>{selectedAsset.detectedName || selectedAsset.libraryName}</h2>
              <div className="version-switch detail-version-switch" aria-label="详情图片版本"><button type="button" className={selectedAsset.displayVersion === "16:9" ? "active" : ""} disabled={selectedAsset.displayVersion !== "16:9" && !(selectedAsset.displayVersion === "raw" && selectedAsset.derivativeAvailable)} onClick={() => void switchDetailVersion("16:9")}>16:9</button><button type="button" className={selectedAsset.displayVersion === "1:1" ? "active" : ""} disabled={selectedAsset.displayVersion !== "1:1"} onClick={() => void switchDetailVersion("1:1")}>1:1</button><button type="button" className={selectedAsset.displayVersion === "9:16" ? "active" : ""} disabled={selectedAsset.displayVersion !== "9:16"} onClick={() => void switchDetailVersion("9:16")}>9:16</button><button type="button" className={selectedAsset.displayVersion === "raw" ? "active" : ""} onClick={() => void switchDetailVersion("raw")}>生肉</button></div>
              <DetailTagEditor assetId={selectedAsset.id} onSaved={(tags) => { setSelectedAsset({ ...selectedAsset, tags }); setAssets((items) => items.map((item) => item.id === selectedAsset.id ? { ...item, tags } : item)); void loadTags(); }} />
              <dl className="detail-list">
                <div className="wide-detail"><dt>识别命名</dt><dd>{isEditingDetectedName ? <span className="metadata-edit-row"><input value={detectedNameDraft} onChange={(event) => setDetectedNameDraft(event.target.value)} placeholder="输入识别命名" /><button type="button" onClick={() => void saveDetectedName()} disabled={busy}><Save size={14} />保存</button><button type="button" onClick={() => { setDetectedNameDraft(selectedAsset.detectedName); setIsEditingDetectedName(false); }} disabled={busy}>取消</button></span> : <span className="metadata-read-row"><span>{selectedAsset.detectedName || "未命名"}</span><button type="button" onClick={() => { setDetectedNameDraft(selectedAsset.detectedName); setIsEditingDetectedName(true); }}>修改</button></span>}</dd></div>
                <div><dt>库中命名</dt><dd>{selectedAsset.libraryName}</dd></div>
                <div><dt>分辨率</dt><dd>{selectedAsset.resolution}</dd></div>
                <div><dt>比例</dt><dd>{selectedAsset.ratio}</dd></div>
                <div><dt>文件大小</dt><dd>{selectedAsset.fileSizeMb.toFixed(2)} MB</dd></div>
                <div><dt>入库时间</dt><dd>{formatDateTime(selectedAsset.importedAt)}</dd></div>
                <div><dt>下载次数</dt><dd>{selectedAsset.downloads}</dd></div>
                <div><dt>点击次数</dt><dd>{selectedAsset.clicks}</dd></div>
                {selectedAsset.derivativeAvailable ? <><div><dt>导出编号</dt><dd>{selectedAsset.derivativeSequence ? `#${selectedAsset.derivativeSequence}` : "未知"}</dd></div><div><dt>作者</dt><dd>{selectedAsset.displayAuthor || "未识别作者"}</dd></div><div><dt>获取渠道</dt><dd>{selectedAsset.derivativeChannel || "未填写"}</dd></div><div><dt>版本状态</dt><dd>{selectedAsset.displayVersion === "raw" ? "生肉" : `${selectedAsset.displayVersion} 派生版本`}</dd></div><div><dt>导出时间</dt><dd>{formatDateTime(selectedAsset.derivativeGeneratedAt)}</dd></div></> : null}
                <div className="wide-detail"><dt>标签</dt><dd>{selectedAsset.tags.join("、")}</dd></div><div className="wide-detail"><dt>OCR 文本</dt><dd className="ocr-text-block">{selectedAsset.ocrText || "尚未识别"}</dd></div>
                <div className="wide-detail"><dt>{selectedAsset.displayVersion === "raw" ? "路径" : "导出路径"}</dt><dd>{selectedAsset.path}</dd></div>{selectedAsset.derivativeAvailable ? <div className="wide-detail"><dt>原图路径</dt><dd>{selectedAsset.sourcePath}</dd></div> : null}
              </dl>
              {ocrProgress?.active ? <div className="ocr-progress-inline" aria-label="OCR 识别进度"><div className="ocr-progress-head"><strong>OCR 识别进度</strong><span>{ocrProgress.current}/{ocrProgress.total} · 自动隐藏 {ocrProgress.autoHidden}</span></div><div className="ocr-progress-track"><div style={{ width: `${Math.round((ocrProgress.current / Math.max(1, ocrProgress.total)) * 100)}%` }} /></div><p>{ocrProgress.label}</p></div> : null}<div className="detail-actions"><button className="primary-action" type="button" onClick={() => void recognizeSelectedAssetText(selectedAsset)} disabled={busy}><FileText size={18} />识别文字</button><button className="ghost-action" type="button" onClick={() => void downloadAssets([selectedAsset])} disabled={busy}><Download size={18} />下载当前版本</button></div>
            </div>
          </section>
          <aside className="detail-external-actions" aria-label="图片删除操作"><span>删除与隐藏</span>{selectedAsset.derivativeAvailable ? <button className="ghost-action" type="button" onClick={() => void deleteDerivative(selectedAsset)} disabled={busy}><Trash2 size={18} />删除当前派生图</button> : null}<button className="ghost-action" type="button" onClick={() => void hideWebsiteAsset(selectedAsset)} disabled={busy}><Database size={18} />从网站隐藏记录</button><button className="danger-action" type="button" onClick={() => void deleteSourceAsset(selectedAsset)} disabled={busy}><Trash2 size={18} />删除 NAS 原图</button></aside>
        </div>
      ) : null}
</main>
  );
}
