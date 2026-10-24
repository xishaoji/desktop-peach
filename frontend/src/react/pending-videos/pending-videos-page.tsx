/* 待处理视频页：扫描与修复留下的几处「这条要人看一眼」，在这里能真的处理。
 *
 * 按问题类型分成三档（缺时长、播放异常、采集未完成），每档一张表。行上能做的三件事——
 * 点开看它、按这一类忽略、重新探测；能重探的才给键，不能的写明该走哪条路。选中之后
 * 批量条上给「重新探测」「重试采集」「忽略这一类」与「移入回收站」。
 *
 * 删除只到回收站：永久删除仍只在回收站里经 `purge_assets()` 执行，这一页不替人做
 * 不可逆的事。移入回收站之后回执上带撤销，撤销走同一批 id 的 `restore`。 */
import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { confirmModal } from '@peach/legacy/ui';
import { LOC } from '@peach/legacy/core';
import { RiArrowRightLine, RiRefreshLine } from '@remixicon/react';

import { Button } from '@/components/base/buttons/button';
import { LinkButton } from '@/components/base/buttons/link-button';

import { errorMessage } from '../../api';
import type { PendingVideosProps } from '../bundle';
import { BoardTabs, TabCount } from '../components/board-tabs';
import { CollectionSummary } from '../components/collection-summary';
import { DataTable, type DataColumn } from '../components/data-table';
import { EmptyState } from '../components/empty-state';
import { Note } from '../components/note';
import { spriteGlyph } from '../components/sprite-glyph';
import { TRASH_KEY } from '../data-cleanup/data-cleanup';
import { busyProps } from '../settings/use-action';
import {
  dispose, fetchPending, ignoreKind, KIND_TEXT, PENDING_KEY, pendingLede, refreshPending, reprobe,
  restore, retryProcessing, unignoreKind, type PendingItem, type PendingKind,
} from './pending-videos';

const ALERT = spriteGlyph('circle-alert');
const FLAG = 'rounded-sm px-1.5 py-px text-caption-1-regular leading-5';

const kindLabel = (kind: PendingKind) => KIND_TEXT.find(([key]) => key === kind)?.[1] ?? kind;

function Identity({ item, openItem }: { item: PendingItem; openItem?(id: number): void }) {
  const label = <span title={item.name} className="min-w-0 truncate text-body-regular text-text-primary">{item.title}</span>;
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span className="flex min-w-0 items-center gap-2">
        {openItem
          ? <button type="button" onClick={() => openItem(item.id)} title={item.name}
              className="min-w-0 cursor-pointer truncate text-left text-body-regular text-text-primary hover:text-text-secondary">
              {item.title}
            </button>
          : label}
        {item.code ? <code className="flex-none text-caption-1-regular text-text-secondary">{item.code}</code> : null}
        {item.ignored ? <i className={`${FLAG} flex-none not-italic bg-background-tertiary-default text-text-secondary`}>已忽略</i> : null}
      </span>
      <small className="max-w-96 truncate text-caption-1-regular text-text-secondary">
        {[item.library, LOC[item.location] || item.location].filter(Boolean).join(' · ')}
      </small>
    </span>
  );
}

export function PendingVideosPage({ openItem, toast, failure }: PendingVideosProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const [kind, setKind] = useState<PendingKind | ''>('');
  const [showIgnored, setShowIgnored] = useState(false);
  const [pendingKey, setPendingKey] = useState('');

  const pending = useQuery({
    queryKey: [...PENDING_KEY, showIgnored ? 'all' : 'active'],
    queryFn: ({ signal }) => fetchPending(signal, showIgnored),
  });
  const data = pending.data;

  /** 移入回收站之后目录网格与回收站读数也要重读，和重复文件页同一条口径。 */
  const afterWrite = () => { refreshPending([TRASH_KEY]); setSelected([]) };

  const ignore = useMutation({
    mutationFn: ({ ids, kind: one }: { ids: number[]; kind: PendingKind }) => ignoreKind(ids, one),
    onSuccess: (_result, { ids, kind: one }) => { toast(`已忽略 ${ids.length} 条「${kindLabel(one)}」提示`); afterWrite() },
    onError: (cause) => failure('忽略待处理项', cause),
    onSettled: () => setPendingKey(''),
  });
  const unignore = useMutation({
    mutationFn: ({ ids, kind: one }: { ids: number[]; kind: PendingKind }) => unignoreKind(ids, one),
    onSuccess: (_result, { ids }) => { toast(`已恢复 ${ids.length} 条提示`); afterWrite() },
    onError: (cause) => failure('恢复待处理项', cause),
    onSettled: () => setPendingKey(''),
  });
  const probe = useMutation({
    mutationFn: (rows: PendingItem[]) => reprobe(rows.filter((item) => item.reprobe).map((item) => item.id)),
    onSuccess: () => { toast('已开始重新探测，进度在任务中心'); afterWrite() },
    onError: (cause) => failure('开始重新探测', cause),
    onSettled: () => setPendingKey(''),
  });
  const retry = useMutation({
    mutationFn: (rows: PendingItem[]) => {
      const retryable = rows.filter((item) => item.retryable);
      return retryProcessing(retryable.map((item) => item.id), String(retryable[0]?.job_id || ''));
    },
    onSuccess: () => { toast('已交回扫描与采集重试'); afterWrite() },
    onError: (cause) => failure('重试采集', cause),
    onSettled: () => setPendingKey(''),
  });
  const remove = useMutation({
    mutationFn: (ids: number[]) => dispose(ids),
    onSuccess: (_result, ids) => {
      refreshPending([TRASH_KEY]);
      setSelected([]);
      toast(`已把 ${ids.length} 项移入回收站`, {
        undo: async () => {
          try {
            await restore(ids);
          } catch (cause) {
            failure('撤销移入回收站', cause);
          } finally {
            refreshPending([TRASH_KEY]);
          }
        },
      });
    },
    onError: (cause) => failure('移入回收站', cause),
    onSettled: () => setPendingKey(''),
  });

  const acting = ignore.isPending || unignore.isPending || probe.isPending || retry.isPending || remove.isPending;
  const busy = (key: string) => busyProps(pendingKey === key && acting);
  const act = (key: string, work: () => void) => () => { setPendingKey(key); work() };

  if (!data) {
    return (
      <div className="mx-auto w-full max-w-board">
        <Note tone="error" title="待处理视频读取失败">
          {pending.error ? errorMessage(pending.error) : '未取得待处理清单，请刷新页面重试。'}
        </Note>
      </div>
    );
  }
  const items = data.items.filter((item) => !kind || item.kind === kind);
  const ignoreGroups = (rows: PendingItem[]) => {
    const groups = new Map<PendingKind, number[]>();
    for (const item of rows) groups.set(item.kind, [...(groups.get(item.kind) ?? []), item.id]);
    for (const [one, ids] of groups) ignore.mutate({ ids, kind: one });
  };
  const runDispose = (rows: PendingItem[]) => {
    if (!rows.length || remove.isPending) return;
    void confirmModal({
      title: '移入回收站',
      body: `这 ${rows.length} 项会移入回收站，记录可从回收站还原；永久删除仍只在回收站里执行。`,
      items: rows.map((item) => item.title),
      confirmLabel: '移入回收站',
      onConfirm: () => remove.mutateAsync(rows.map((item) => item.id)),
    });
  };

  const columns: DataColumn<PendingItem>[] = [
    { id: 'item', label: '作品', value: (item) => item.title, render: (item) => <Identity item={item} openItem={openItem} /> },
    { id: 'kind', label: '问题', value: (item) => kindLabel(item.kind), render: (item) => (
      <span className="flex min-w-40 flex-col gap-0.5">
        <span className="text-body-2-regular text-text-primary">{kindLabel(item.kind)}</span>
        <small className="text-caption-1-regular text-text-secondary wrap-anywhere">{item.detail}</small>
      </span>
    ) },
    { id: 'path', label: '位置', value: (item) => item.path, render: (item) => (
      <span title={item.path}
        className="block max-w-80 truncate font-mono text-caption-1-regular text-text-secondary">{item.path}</span>
    ) },
    { id: 'actions', label: '处理', render: (item) => (
      <span className="flex flex-wrap items-center gap-2">
        {item.ignored
          ? <Button variant="secondary" size="small" {...busy(`unignore:${item.id}`)}
              onClick={act(`unignore:${item.id}`, () => unignore.mutate({ ids: [item.id], kind: item.kind }))}>
              恢复提示
            </Button>
          : <>
              {item.reprobe
                ? <Button variant="secondary" size="small" {...busy(`probe:${item.id}`)}
                    onClick={act(`probe:${item.id}`, () => probe.mutate([item]))}>
                    <RiRefreshLine aria-hidden className="size-4" />重新探测
                  </Button>
                : item.retryable
                  ? <Button variant="secondary" size="small" {...busy(`retry:${item.id}`)}
                      onClick={act(`retry:${item.id}`, () => retry.mutate([item]))}>重试采集</Button>
                  : item.blocked_reason
                    ? <small className="max-w-72 text-caption-1-regular text-text-secondary">{item.blocked_reason}</small>
                    : null}
              <Button variant="ghost" size="small" {...busy(`ignore:${item.id}`)}
                onClick={act(`ignore:${item.id}`, () => ignore.mutate({ ids: [item.id], kind: item.kind }))}>
                忽略
              </Button>
            </>}
      </span>
    ) },
  ];

  return (
    <div className="mx-auto flex w-full max-w-board flex-col gap-6 pb-10.5">
      <CollectionSummary label="待处理视频" figure={`${Number(data.total || 0).toLocaleString()} 条`}
        detail={pendingLede(data)} />
      <div className="flex flex-wrap items-center gap-3">
        <BoardTabs className="" label="问题类型" value={kind} onChange={(next) => { setKind(next); setSelected([]) }}
          tabs={[
            { value: '' as const, label: '全部' },
            ...data.kinds.map((row) => ({ value: row.kind, label: row.label })),
          ]} />
        <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-caption-1-regular text-text-secondary">
          {data.kinds.map((row) => <span key={row.kind} className="flex items-center gap-1">
            {row.label}<TabCount value={row.total} />
            {row.ignored ? <span className="text-text-tertiary">{`已忽略 ${row.ignored}`}</span> : null}
          </span>)}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" size="small" aria-pressed={showIgnored}
          onClick={() => { setShowIgnored(!showIgnored); setSelected([]) }}>
          {showIgnored ? '只看未忽略' : `显示已忽略（${Number(data.ignored_total || 0).toLocaleString()}）`}
        </Button>
        <LinkButton href="/data-cleanup#mediaRepair" size="small" trailingIcon={RiArrowRightLine}>媒体修复</LinkButton>
      </div>
      {items.length
        ? <DataTable label="待处理视频" items={items} columns={columns} rowId={(item) => String(item.id)}
            rowLabel={(item) => item.title} selected={selected} onSelected={setSelected} unit="条"
            description="重新探测会把选中项按来源逐个再探一遍，只处理没探过时长、来源不计费的那些；其余在行上写明该走哪条路。"
            actions={(rows) => (
              <>
                {rows.some((item) => item.reprobe)
                  ? <Button variant="secondary" size="small" {...busy('probe:batch')}
                      onClick={act('probe:batch', () => probe.mutate(rows))}>重新探测</Button>
                  : null}
                {rows.some((item) => item.retryable)
                  ? <Button variant="secondary" size="small" {...busy('retry:batch')}
                      onClick={act('retry:batch', () => retry.mutate(rows))}>重试采集</Button>
                  : null}
                <Button variant="secondary" size="small" {...busy('ignore:batch')}
                  onClick={act('ignore:batch', () => ignoreGroups(rows))}>
                  {showIgnored ? '恢复这一类' : '忽略这一类'}
                </Button>
                <Button variant="danger" size="small" {...busy('dispose')}
                  onClick={() => runDispose(rows)}>
                  {`移入回收站（${rows.length}）`}
                </Button>
              </>
            )} />
        : <EmptyState icon={ALERT} title={kind ? '这一类没有待处理项' : '没有待处理视频'}>
            {Number(data.ignored_total || 0)
              ? '剩下的都按类型忽略过了；点「显示已忽略」可以逐条恢复。'
              : '扫描之后没有发现缺时长、播不动或采集未完成的视频。'}
          </EmptyState>}
    </div>
  );
}
