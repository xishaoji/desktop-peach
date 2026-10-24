/* 待处理视频的数据契约。
 *
 * 这一页把三处「这条要人看一眼」收成一张清单：缺时长、播放异常、采集未完成。判据全在
 * 服务端（`web_pending`），前端只负责按问题类型分组、把每一类能做什么摆出来。
 *
 * 每条带 `reprobe`（能不能自动重探）与 `blocked_reason`（不能的话为什么）。这两个字段
 * 不是装饰：能重探的排在一起、不能的写明该走哪条路，比让人点了再失败一次诚实。
 *
 * 动作一共五个，都是服务端一个端点上的 `action`：忽略与恢复只写 `review_decision`，
 * 移入回收站转交 `/api/batch` 的 `dispose`（回收与撤销只有那一条实现），重新探测与
 * 重试采集各起一趟后台任务。 */
import type { QueryKey } from '@tanstack/react-query';

import { apiGet, apiSend } from '../../api';
import { BATCH_URL } from '../duplicates/duplicates';
import { queryClient } from '../query';

export const PENDING_URL = '/api/pending-videos';
/** 这一页的清单。键里带 `active`／`all`：两种 `include_ignored` 是两份结果。 */
export const PENDING_KEY = ['pending-videos'] as const;
/** 数据管理页那张卡片只要读数，与清单同一段前缀，写完一起失效。 */
export const PENDING_COUNTS_KEY = ['pending-videos', 'counts'] as const;

/** 一条动作最多接多少个（服务端同一个上限）。 */
export const ACTION_LIMIT = 200;

export type PendingKind = 'duration' | 'media' | 'processing';

export interface PendingItem {
  id: number;
  kind: PendingKind;
  code: string;
  title: string;
  name: string;
  path: string;
  location: string;
  library: string;
  detail: string;
  /** 这一条能不能由这一页自动重探。 */
  reprobe: boolean;
  /** 不能重探时的原因，也就是该走哪条路。能重探时是空串。 */
  blocked_reason: string;
  ignored: boolean;
  /** 采集未完成才有：上一趟任务的编号与能不能重试。 */
  job_id?: string;
  retryable?: boolean;
}

export interface PendingCounts { total: number; reprobe: number; blocked: number; ignored: number }

export interface PendingKindReading extends PendingCounts { kind: PendingKind; label: string }

export interface PendingJob {
  status: string;
  job_id: string;
  checked: number;
  total: number;
  message: string;
  stage?: string;
  error?: string;
}

export interface PendingData {
  items: PendingItem[];
  counts: Record<PendingKind, PendingCounts>;
  kinds: PendingKindReading[];
  kind: string;
  include_ignored: boolean;
  total: number;
  reprobe: number;
  blocked: number;
  ignored_total: number;
  job?: PendingJob;
}

export type PendingAction = 'ignore' | 'unignore' | 'reprobe' | 'retry' | 'dispose';

export interface PendingActionResult { ok: boolean; action: PendingAction }

/** 清单。`includeIgnored` 时把按类型忽略过的也列出来，逐条恢复用。 */
export const fetchPending = (signal?: AbortSignal, includeIgnored = false) =>
  apiGet<PendingData>(includeIgnored ? `${PENDING_URL}?include_ignored=1` : PENDING_URL, signal);

/** 数据管理页那张卡片：只要读数，不要明细。 */
export const fetchPendingCounts = (signal?: AbortSignal) =>
  apiGet<PendingData>(`${PENDING_URL}?counts=1`, signal);

/** 首屏：清单这一份。数据管理页那张卡片读的是它自己的 `counts=1`，两处各自失败各自算。 */
export async function prefetchPending(signal: AbortSignal): Promise<void> {
  await queryClient.fetchQuery({ queryKey: [...PENDING_KEY, 'active'], queryFn: () => fetchPending(signal) });
}

/** 问题类型的名字与一句说明。顺序就是页面上的顺序。 */
export const KIND_TEXT: ReadonlyArray<readonly [PendingKind, string, string]> = [
  ['duration', '缺时长', '还没探出时长，播放器不知道这条多长。可以在这里重新探测。'],
  ['media', '播放异常', '媒体修复那一轮判过这条播不动。修复按媒体库整轮执行，在数据管理页的「媒体修复」里。'],
  ['processing', '采集未完成', '上一趟扫描与采集没有做完这条。能重试的可以交回去再跑一次。'],
];

/** 这批 id 要发几次。一次最多 200 条，多出来的分批发：并发发出去只是让服务端自己排队。 */
export const batches = (ids: readonly number[]): number[][] => {
  const chunks: number[][] = [];
  for (let offset = 0; offset < ids.length; offset += ACTION_LIMIT) chunks.push([...ids.slice(offset, offset + ACTION_LIMIT)]);
  return chunks;
};

/** 同一类里发同一个动作。忽略与恢复按问题类型记，所以必须带上 `kinds`。 */
async function sendAll(action: PendingAction, ids: readonly number[], extra: Record<string, unknown> = {}) {
  for (const chunk of batches(ids)) await apiSend<PendingActionResult>(PENDING_URL, { action, ids: chunk, ...extra });
}

export const ignoreKind = (ids: readonly number[], kind: PendingKind) => sendAll('ignore', ids, { kinds: [kind] });
export const unignoreKind = (ids: readonly number[], kind: PendingKind) => sendAll('unignore', ids, { kinds: [kind] });

/** 重新探测：服务端按「来源 + 根」归组串行跑，回的是这一趟的任务快照。
 *  只发一趟、不拆批：每发一次都是一趟新任务，后一趟会把前一趟顶掉。 */
export async function reprobe(ids: readonly number[]) {
  const [first = []] = batches(ids);
  return apiSend<PendingActionResult & { job?: PendingJob }>(PENDING_URL, { action: 'reprobe', ids: first });
}

/** 交给上一趟扫描与采集重试。`jobId` 是那一条记录上记着的任务编号。 */
export const retryProcessing = (ids: readonly number[], jobId: string) => sendAll('retry', ids, { job_id: jobId });

/** 移入回收站：与目录、重复文件同一条批量实现，撤销走 `restore`。 */
export async function dispose(ids: readonly number[]) {
  for (const chunk of batches(ids)) {
    await apiSend(BATCH_URL, { ids: chunk, operation: 'dispose', disposal_reason: 'manual' });
  }
}

export async function restore(ids: readonly number[]) {
  for (const chunk of batches(ids)) await apiSend(BATCH_URL, { ids: chunk, operation: 'restore' });
}

/** 改完之后要重读的几份：这一页（两种 `include_ignored` 都算）、数据管理页的读数、
 *  目录网格与回收站（移入回收站之后）。前缀失效，几份一起走。 */
export function refreshPending(keys: QueryKey[] = []) {
  void queryClient.invalidateQueries({ queryKey: PENDING_KEY });
  for (const queryKey of keys) void queryClient.invalidateQueries({ queryKey, exact: true });
}

/** 数据管理页那张卡片的分项：只列真有东西的那几类，已忽略的接在后面。 */
export function pendingBreakdown(data: Pick<PendingData, 'kinds' | 'ignored_total'>): string {
  const parts = data.kinds.filter((row) => row.total > 0).map((row) => `${row.label} ${row.total.toLocaleString()}`);
  if (data.ignored_total) parts.push(`已忽略 ${data.ignored_total.toLocaleString()}`);
  return parts.join(' · ');
}

/** 主读数那一句：一共几条，其中多少能直接处理、多少要先走别的路。 */
export const pendingLede = (data: Pick<PendingData, 'total' | 'reprobe' | 'blocked' | 'ignored_total'>) => {
  const parts = [`${data.total.toLocaleString()} 条待处理`];
  if (data.reprobe) parts.push(`可重新探测 ${data.reprobe.toLocaleString()}`);
  if (data.blocked) parts.push(`需另走一步 ${data.blocked.toLocaleString()}`);
  if (data.ignored_total) parts.push(`已忽略 ${data.ignored_total.toLocaleString()}`);
  return parts.join(' · ');
};
