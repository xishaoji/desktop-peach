/* 待处理视频页：三类问题摆成一张清单，忽略、恢复、重新探测与移入回收站各自发什么。
 *
 * 首屏和真实路径一样先落进共用缓存（遗留层 `prefetch` 那一步），页面挂上去直接读。
 * 移入回收站与重复文件页同一条批量实现，所以确认弹层也按那一套替身点过去。 */
import { notifyManager, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as legacyUi from '@peach/legacy/ui';

import {
  batches, KIND_TEXT, pendingBreakdown, pendingLede, PENDING_KEY, prefetchPending,
  type PendingData,
} from '../../src/react/pending-videos/pending-videos';
import { PendingVideosPage } from '../../src/react/pending-videos/pending-videos-page';
import { queryClient } from '../../src/react/query';
import { buttonNamed, click, mount, sentBody, settle } from './render';

afterEach(() => { queryClient.clear() });
notifyManager.setScheduler((notify) => notify());

const item = (id: number, patch: Partial<PendingData['items'][number]> = {}): PendingData['items'][number] => ({
  id, kind: 'duration', code: `ABC-${id}`, title: `第 ${id} 部`, name: `${id}.mp4`,
  path: `R:\\Media\\${id}.mp4`, location: 'local', library: '本地',
  detail: '还没有探到时长', reprobe: true, blocked_reason: '', ignored: false, ...patch,
});

const emptyCounts = { total: 0, reprobe: 0, blocked: 0, ignored: 0 };
const data: PendingData = {
  items: [
    item(1),
    item(2, { name: 'pointer.strm', reprobe: false, blocked_reason: 'STRM 与 ISO 不执行视频探测' }),
    item(3, { kind: 'media', code: 'ABC-003', detail: '缺文件索引，打不开', reprobe: false,
      blocked_reason: '修复在数据管理页的「媒体修复」里按媒体库整轮执行' }),
    item(4, { kind: 'processing', code: 'ABC-004', detail: '封面下载超时', reprobe: false,
      blocked_reason: '', retryable: true, job_id: 'job-1' }),
  ],
  counts: {
    duration: { total: 2, reprobe: 1, blocked: 1, ignored: 0 },
    media: { total: 1, reprobe: 0, blocked: 1, ignored: 0 },
    processing: { total: 1, reprobe: 0, blocked: 1, ignored: 0 },
  },
  kinds: [
    { kind: 'duration', label: '缺时长', ...emptyCounts, total: 2, reprobe: 1, blocked: 1 },
    { kind: 'media', label: '播放异常', ...emptyCounts, total: 1, blocked: 1 },
    { kind: 'processing', label: '采集未完成', ...emptyCounts, total: 1, blocked: 1 },
  ],
  kind: '', include_ignored: false, total: 4, reprobe: 1, blocked: 3, ignored_total: 0,
};

type Confirmation = Parameters<typeof legacyUi.confirmModal>[0];

function confirmEverything() {
  const seen: Confirmation[] = [];
  vi.spyOn(legacyUi, 'confirmModal').mockImplementation((async (options: Confirmation) => {
    seen.push(options);
    await options.onConfirm?.();
    return { confirmed: true };
  }) as typeof legacyUi.confirmModal);
  return seen;
}

/** 读请求回这一份清单，写请求回一句 ok：页面挂载时会重取一次（共用缓存没有新鲜期），
 *  写完之后也会失效重取，两种都要答得上来。 */
function server(payload: PendingData = data) {
  return vi.fn(async (path: string, init?: RequestInit) =>
    ({ ok: true, status: 200, json: async () => (init?.method === 'POST' ? { ok: true } : payload) }));
}

/** 只看写请求：读请求的条数随重取次数变化，和动作断言无关。 */
const writes = (fetcher: ReturnType<typeof server>) =>
  fetcher.mock.calls.filter((call) => call[1]?.method === 'POST');

async function open(payload: PendingData = data, fetcher = server(payload)) {
  vi.stubGlobal('fetch', fetcher);
  queryClient.setQueryData([...PENDING_KEY, 'active'], payload);
  const props = { openItem: vi.fn(), toast: vi.fn(), failure: vi.fn() };
  const host = await mount(
    <QueryClientProvider client={queryClient}>
      <PendingVideosPage {...props} />
    </QueryClientProvider>,
  );
  return { host, fetcher, ...props };
}

describe('数据契约', () => {
  it('主读数与分项都只列真有东西的那几类', () => {
    expect(pendingLede(data)).toBe('4 条待处理 · 可重新探测 1 · 需另走一步 3');
    expect(pendingBreakdown({ kinds: data.kinds, ignored_total: 2 }))
      .toBe('缺时长 2 · 播放异常 1 · 采集未完成 1 · 已忽略 2');
  });

  it('一次动作最多 200 条，多的拆开发', () => {
    expect(batches([1, 2, 3]).map((chunk) => chunk.length)).toEqual([3]);
    const many = Array.from({ length: 401 }, (_, at) => at + 1);
    expect(batches(many).map((chunk) => chunk.length)).toEqual([200, 200, 1]);
  });

  it('每一类都有一句「不能处理时该走哪条路」', () => {
    expect(KIND_TEXT.map(([kind]) => kind)).toEqual(['duration', 'media', 'processing']);
    for (const [, , hint] of KIND_TEXT) expect(hint.length).toBeGreaterThan(8);
  });
});

it('首屏取回清单，三类问题按顺序排成一张表', async () => {
  const fetcher = vi.fn(async (_path: string) => ({ ok: true, status: 200, json: async () => data }));
  vi.stubGlobal('fetch', fetcher);
  await prefetchPending(new AbortController().signal);
  const host = await mount(
    <QueryClientProvider client={queryClient}>
      <PendingVideosPage openItem={vi.fn()} toast={vi.fn()} failure={vi.fn()} />
    </QueryClientProvider>);
  expect(fetcher.mock.calls.map((call) => call[0])).toEqual(['/api/pending-videos']);
  expect(host.textContent).toContain('4 条');
  expect(host.textContent).toContain('缺时长');
  expect(host.textContent).toContain('第 1 部');
  expect(host.textContent).toContain('STRM 与 ISO 不执行视频探测');
});

it('能重探的行给「重新探测」，不能的写明该走哪条路', async () => {
  const { host } = await open();
  expect(buttonNamed('重新探测', host)).not.toBeNull();
  const rows = [...host.querySelectorAll('tbody tr')];
  expect(rows[0]!.textContent).toContain('重新探测');
  expect(rows[1]!.textContent).toContain('STRM 与 ISO 不执行视频探测');
  expect(buttonNamed('重试采集', host)).not.toBeNull();
  expect(rows[2]!.textContent).toContain('媒体修复');
});

it('「重新探测」只发一条消息：多次发就等于多趟任务，后一趟会顶掉前一趟', async () => {
  const { host, fetcher, toast } = await open();
  await click(buttonNamed('重新探测', [...host.querySelectorAll('tbody tr')][0]));
  await settle();
  const sent = writes(fetcher);
  expect(sent).toHaveLength(1);
  expect(sent[0]![0]).toBe('/api/pending-videos');
  expect(sentBody(fetcher, fetcher.mock.calls.indexOf(sent[0]!)))
    .toEqual({ action: 'reprobe', ids: [1] });
  expect(toast).toHaveBeenCalledWith('已开始重新探测，进度在任务中心');
});

it('忽略与恢复都带上问题类型：同一条片子可能同时有两类问题', async () => {
  const { host, fetcher, toast } = await open();
  await click(buttonNamed('忽略', [...host.querySelectorAll('tbody tr')][2]));
  await settle();
  const sent = writes(fetcher);
  expect(sent).toHaveLength(1);
  expect(sentBody(fetcher, fetcher.mock.calls.indexOf(sent[0]!)))
    .toEqual({ action: 'ignore', ids: [3], kinds: ['media'] });
  expect(toast).toHaveBeenCalledWith('已忽略 1 条「播放异常」提示');
});

it('已忽略的清单在「显示已忽略」之后才有，逐条可以恢复', async () => {
  const ignored: PendingData = {
    ...data, include_ignored: true, ignored_total: 1,
    items: [...data.items, item(5, { ignored: true, reprobe: true })],
  };
  const { host, fetcher, toast } = await open(ignored);
  await click(buttonNamed('显示已忽略（1）', host));
  await settle();
  expect(fetcher.mock.calls.some((call) => String(call[0]).includes('include_ignored=1'))).toBe(true);
  expect(host.textContent).toContain('已忽略');
  await click(buttonNamed('恢复提示', host));
  await settle();
  const sent = writes(fetcher);
  expect(sentBody(fetcher, fetcher.mock.calls.indexOf(sent[0]!)))
    .toEqual({ action: 'unignore', ids: [5], kinds: ['duration'] });
  expect(toast).toHaveBeenCalledWith('已恢复 1 条提示');
});

it('批量移入回收站先确认，确认后走批量接口，回执带撤销', async () => {
  const seen = confirmEverything();
  const { host, fetcher, toast } = await open();
  await click([...host.querySelectorAll('tbody tr input[type="checkbox"]')][0]);
  await settle();
  await click(buttonNamed('移入回收站（1）', host));
  await settle();
  expect(seen).toHaveLength(1);
  expect(seen[0]!.body).toContain('永久删除仍只在回收站里执行');
  expect(seen[0]!.items).toEqual(['第 1 部']);
  const sent = writes(fetcher);
  expect(sent[0]![0]).toBe('/api/batch');
  expect(sentBody(fetcher, fetcher.mock.calls.indexOf(sent[0]!)))
    .toEqual({ ids: [1], operation: 'dispose', disposal_reason: 'manual' });
  expect(toast).toHaveBeenCalledWith('已把 1 项移入回收站', expect.anything());
  const options = toast.mock.calls[0]![1] as { undo: () => Promise<void> };
  await options.undo();
  const undo = writes(fetcher).at(-1)!;
  expect(undo[0]).toBe('/api/batch');
  expect(sentBody(fetcher, fetcher.mock.calls.indexOf(undo)))
    .toEqual({ ids: [1], operation: 'restore' });
});

it('没有待处理项时给空态，并说明忽略过的那几条去哪儿了', async () => {
  const empty: PendingData = { ...data, items: [], total: 0, reprobe: 0, blocked: 0, ignored_total: 3 };
  const { host } = await open(empty);
  expect(host.querySelector('[data-empty-state]')).not.toBeNull();
  expect(host.textContent).toContain('没有待处理视频');
  expect(host.textContent).toContain('显示已忽略');
});

it('取不到清单时说明是读取失败，而不是「没有待处理项」', async () => {
  const fetcher = vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ error: 'internal' }) }));
  vi.stubGlobal('fetch', fetcher);
  const props = { openItem: vi.fn(), toast: vi.fn(), failure: vi.fn() };
  const host = await mount(
    <QueryClientProvider client={queryClient}>
      <PendingVideosPage {...props} />
    </QueryClientProvider>);
  expect(host.textContent).toContain('待处理视频读取失败');
  expect(host.querySelector('[data-empty-state]')).toBeNull();
});
