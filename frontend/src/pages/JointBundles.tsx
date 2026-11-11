import { useEffect, useMemo, useState } from 'react';
import type { BundleScopePayload } from '../api/bundles';
import { api } from '../api/client';
import { useBundleStore } from '../stores/bundleStore';
import { useAuthStore } from '../stores/authStore';
import { SectionHead } from '../components/common/SectionHead';
import { StampBadge } from '../components/common/StampBadge';
import { EmptyState } from '../components/common/EmptyState';
import { StatStrip } from '../components/common/StatStrip';
import { BUILD_STATUS_LABELS, EXPORT_STATUS_LABELS } from '../constants/enums';
import { formatNumber, shortId, errorMessage } from '../utils/format';
import type { BundleStats, BundleTableStats, JointBundle } from '../types/models';

const TABLE_LABELS: Record<string, string> = {
  patients: '患者主索引',
  visits: '就诊记录',
  labs: '检验结果',
};

const REASON_LABELS: Record<string, string> = {
  patient_not_found: '患者主索引缺失',
  visit_not_in_scope: '关联就诊不在包内',
  patient_visit_mismatch: '就诊-患者归属不一致',
};

const SCOPE_MODE_LABELS: Record<string, string> = {
  ids: '编号清单',
  range: '编号区间',
  all: '全部患者',
  mixed: '清单 + 区间',
};

const GRANULARITY_LABELS: Record<string, string> = {
  year: '年',
  month: '月',
};

const statusTone = (status: JointBundle['status']) =>
  status === 'approved' ? 'ok' : status === 'rejected' ? 'stamp' : 'warn';

const reasonText = (stats: BundleTableStats): string => {
  const entries = Object.entries(stats.missing_reasons ?? {});
  if (!entries.length) return '—';
  return entries
    .map(([key, value]) => `${REASON_LABELS[key] ?? key} ${formatNumber(value)}`)
    .join(' · ');
};

/** 支持 P0001、P0001-P0050、逗号/空格/换行混排；区间按原编号宽度补零。 */
const parsePatientIds = (text: string): string[] => {
  const tokens = text.split(/[,\s;，；]+/).filter(Boolean);
  const ids = new Set<string>();
  for (const token of tokens) {
    const match = token.match(/^([A-Za-z]*)(\d+)-([A-Za-z]*)(\d+)$/);
    if (match) {
      const [, prefixA, startText, prefixB, endText] = match;
      const start = Number(startText);
      const end = Number(endText);
      if (start <= end && end - start <= 10000) {
        const prefix = prefixA || prefixB;
        const width = Math.max(startText.length, endText.length);
        for (let index = start; index <= end; index += 1) {
          ids.add(`${prefix}${String(index).padStart(width, '0')}`);
        }
        continue;
      }
    }
    ids.add(token);
  }
  return Array.from(ids).sort();
};

const tableKeys = ['patients', 'visits', 'labs'] as const;

const statOf = (stats: BundleStats | undefined, key: (typeof tableKeys)[number]): BundleTableStats | null =>
  stats?.[key] ?? null;

const StatsTable = ({ stats }: { stats: BundleStats }) => (
  <div className="table-wrap">
    <table className="table">
      <thead>
        <tr>
          <th>表</th>
          <th className="right">源表行数</th>
          <th className="right">输入</th>
          <th className="right">保留</th>
          <th className="right">缺失</th>
          <th>缺口原因</th>
          <th>文件摘要</th>
        </tr>
      </thead>
      <tbody>
        {tableKeys.map((key) => {
          const row = statOf(stats, key);
          if (!row) return null;
          return (
            <tr key={key}>
              <td>
                <div className="mono">{key}</div>
                <div className="faint" style={{ fontSize: 10.5 }}>
                  {TABLE_LABELS[key]}
                </div>
              </td>
              <td className="right mono faint">{formatNumber(row.source_rows)}</td>
              <td className="right mono">{formatNumber(row.input_rows)}</td>
              <td className="right mono" style={{ color: 'var(--olive)' }}>
                {formatNumber(row.retained_rows)}
              </td>
              <td className="right mono" style={{ color: row.missing_rows > 0 ? 'var(--stamp)' : undefined }}>
                {formatNumber(row.missing_rows)}
              </td>
              <td className="muted" style={{ fontSize: 11 }}>
                {reasonText(row)}
              </td>
              <td className="mono faint" style={{ fontSize: 10.5 }}>
                {row.sha256 ? `${row.sha256.slice(0, 14)}…` : '生成后写入'}
              </td>
            </tr>
          );
        })}
        {stats.totals ? (
          <tr>
            <td style={{ fontWeight: 600 }}>合计</td>
            <td className="right mono faint">{formatNumber(stats.totals.source_rows)}</td>
            <td className="right mono">{formatNumber(stats.totals.input_rows)}</td>
            <td className="right mono" style={{ color: 'var(--olive)' }}>
              {formatNumber(stats.totals.retained_rows)}
            </td>
            <td className="right mono" style={{ color: stats.totals.missing_rows > 0 ? 'var(--stamp)' : undefined }}>
              {formatNumber(stats.totals.missing_rows)}
            </td>
            <td colSpan={2} />
          </tr>
        ) : null}
      </tbody>
    </table>
  </div>
);

const GapPanel = ({ stats }: { stats: BundleStats }) => {
  const gaps = stats.gaps;
  if (!gaps) return null;
  const missing = gaps.missing_patient_ids ?? [];
  return (
    <div className="panel panel-pad">
      <div className="subhead">范围缺口</div>
      <div className="grid-3" style={{ marginBottom: 12 }}>
        <div className="stat">
          <div className="stat-value num" style={{ color: gaps.missing_patient_count ? 'var(--stamp)' : 'var(--olive)' }}>
            {formatNumber(gaps.missing_patient_count)}
          </div>
          <div className="stat-label">范围内患者未找到</div>
        </div>
        <div className="stat">
          <div className="stat-value num">{formatNumber(gaps.patients_without_visits)}</div>
          <div className="stat-label">有患者、无就诊</div>
        </div>
        <div className="stat">
          <div className="stat-value num">{formatNumber(gaps.patients_without_labs)}</div>
          <div className="stat-label">有患者、无检验</div>
        </div>
      </div>
      {missing.length ? (
        <div className="notice notice-warn">
          未找到的患者编号：
          <span className="mono" style={{ fontSize: 11 }}>
            {' '}
            {missing.slice(0, 60).join('、')}
            {gaps.missing_patient_ids_truncated || missing.length > 60 ? ' …' : ''}
          </span>
        </div>
      ) : (
        <div className="notice notice-ok">范围内患者编号均在患者主索引中，且已检查就诊/检验关联缺口。</div>
      )}
    </div>
  );
};

export const JointBundlesPage = () => {
  const can = useAuthStore((state) => state.can);
  const user = useAuthStore((state) => state.user);
  const { items, preview, previewing, loading, busyId, error, load, runPreview, create, approve, reject, retry } =
    useBundleStore();

  const [scopeMode, setScopeMode] = useState<'ids' | 'range' | 'all'>('ids');
  const [idsText, setIdsText] = useState('P0001-P0010');
  const [rangeStart, setRangeStart] = useState('P0001');
  const [rangeEnd, setRangeEnd] = useState('P0050');
  const [granularity, setGranularity] = useState<'year' | 'month'>('year');
  const [purpose, setPurpose] = useState(
    '心研所联合随访分析：患者-就诊-检验三表联合去标识包，仅带范围内关联数据，伦理批件 YZ-2026-131。',
  );
  const [note, setNote] = useState('范围与用途匹配伦理批件，三表共用稳定假名后放行。');
  const [formMessage, setFormMessage] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  useEffect(() => {
    void load();
  }, [load]);

  // worker 生成中：静默刷新，直到 done / failed
  const hasActiveBuild = useMemo(
    () => items.some((item) => item.build_status === 'queued' || item.build_status === 'running'),
    [items],
  );
  useEffect(() => {
    if (!hasActiveBuild) return undefined;
    const timer = window.setInterval(() => void load(), 1500);
    return () => window.clearInterval(timer);
  }, [hasActiveBuild, load]);

  const scopePayload = (): BundleScopePayload => {
    const base = { date_granularity: granularity };
    if (scopeMode === 'all') return { ...base, all_patients: true };
    if (scopeMode === 'range') {
      return { ...base, patient_id_start: rangeStart.trim(), patient_id_end: rangeEnd.trim() };
    }
    return { ...base, patient_ids: parsePatientIds(idsText) };
  };

  const scopeCount = useMemo(() => {
    if (scopeMode === 'all') return '全部患者';
    if (scopeMode === 'range') return `${rangeStart || '?'} – ${rangeEnd || '?'}`;
    return `${parsePatientIds(idsText).length} 个编号`;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeMode, idsText, rangeStart, rangeEnd]);

  const handlePreview = async () => {
    setFormMessage(null);
    const ok = await runPreview(scopePayload());
    if (ok) setFormMessage('预检完成：下方是各表输入 / 保留 / 缺失与范围缺口，确认后再发起联合包。');
  };

  const handleCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    setFormMessage(null);
    if (scopeMode === 'ids' && parsePatientIds(idsText).length === 0) {
      setFormMessage('请至少填写一个患者编号，或改用编号区间 / 全部患者。');
      return;
    }
    const created = await create({ ...scopePayload(), purpose });
    if (!created) return;
    if (can('exports:approve')) {
      setFormMessage(
        created.build_status === 'done'
          ? '联合包已生成：同范围再次发起会复用同一包编号与同一批假名。'
          : created.build_status === 'failed'
            ? `联合包生成失败：${created.error_detail ?? '未知原因'}。可在台账里点「重试」，失败不会留下半套包。`
            : '联合包已发起并自动批准，worker 正在生成三表文件。',
      );
    } else {
      setFormMessage('联合包申请已提交，等待 admin / dpo 审批后生成。审批前即可查看缺口统计。');
    }
  };

  const handleApprove = async (bundleId: string) => {
    const ok = await approve(bundleId, note || '同意按患者范围生成联合去标识包。');
    if (ok) setFormMessage('已批准，正在生成三表联合包；完成后可下载 zip 或单表 CSV。');
  };

  const handleReject = async (bundleId: string) => {
    const ok = await reject(bundleId, note || '范围或用途材料不足，请补充后重新发起。');
    if (ok) setFormMessage('已驳回，审批意见已写入台账。');
  };

  const handleRetry = async (bundleId: string) => {
    const ok = await retry(bundleId);
    if (ok) setFormMessage('已重新排队生成；staging 会先清理，失败不会留下半套包。');
  };

  const handleDownload = async (bundleId: string, tableName?: string) => {
    setDownloadError(null);
    try {
      const scopeKey = items.find((item) => item.id === bundleId)?.scope_key ?? bundleId;
      const suffix = scopeKey.slice(0, 12);
      const path = tableName
        ? `/bundles/${bundleId}/download/${tableName}`
        : `/bundles/${bundleId}/download`;
      const filename = tableName
        ? `maskguard-${tableName}-${suffix}.csv`
        : `maskguard-joint-bundle-${suffix}.zip`;
      await api.downloadFile(path, filename);
    } catch (error) {
      setDownloadError(errorMessage(error));
    }
  };

  const pending = items.filter((item) => item.status === 'pending').length;
  const done = items.filter((item) => item.build_status === 'done').length;
  const failed = items.filter((item) => item.build_status === 'failed').length;
  const busy = busyId !== null;

  return (
    <>
      <SectionHead
        kicker="06 / JOINT DE-ID BUNDLE"
        title="联合去标识包"
        description="按患者范围一次生成 patients / visits / labs 三表联合包：三表共用同一稳定假名，关联表日期降到年/月，只带范围内关联数据。同范围重复发起复用同一包编号与同一批文件。"
      />

      {error ? <div className="notice section">{error}</div> : null}
      {downloadError ? <div className="notice notice-warn section">{downloadError}</div> : null}
      {formMessage ? <div className="notice notice-ok section">{formMessage}</div> : null}

      <div className="section">
        <StatStrip
          items={[
            { label: '联合包总数', value: items.length },
            { label: '待审批', value: pending, tone: 'amber' },
            { label: '已生成', value: done, tone: 'olive' },
            { label: '生成失败', value: failed, tone: 'stamp' },
            { label: '当前范围', value: scopeCount },
          ]}
        />
      </div>

      <div className="grid-2 section" style={{ alignItems: 'start' }}>
        <form className="panel panel-pad" onSubmit={handleCreate}>
          <div className="subhead">发起联合包</div>

          <label className="field" style={{ marginBottom: 10 }}>
            <span className="field-label">患者范围方式</span>
            <select
              className="select"
              value={scopeMode}
              onChange={(event) => setScopeMode(event.target.value as 'ids' | 'range' | 'all')}
            >
              <option value="ids">患者编号清单（支持 P0001-P0050）</option>
              <option value="range">患者编号区间</option>
              <option value="all">全部患者</option>
            </select>
          </label>

          {scopeMode === 'ids' ? (
            <label className="field" style={{ marginBottom: 10 }}>
              <span className="field-label">患者编号（逗号 / 空格 / 换行分隔）</span>
              <textarea
                className="textarea"
                value={idsText}
                onChange={(event) => setIdsText(event.target.value)}
                placeholder="P0001, P0002, P0003-P0050"
              />
            </label>
          ) : null}

          {scopeMode === 'range' ? (
            <div className="row" style={{ marginBottom: 10, alignItems: 'flex-end' }}>
              <label className="field" style={{ flex: 1 }}>
                <span className="field-label">起始编号</span>
                <input className="input mono" value={rangeStart} onChange={(event) => setRangeStart(event.target.value)} />
              </label>
              <label className="field" style={{ flex: 1 }}>
                <span className="field-label">结束编号</span>
                <input className="input mono" value={rangeEnd} onChange={(event) => setRangeEnd(event.target.value)} />
              </label>
            </div>
          ) : null}

          <div className="row" style={{ marginBottom: 10, alignItems: 'flex-end' }}>
            <label className="field" style={{ flex: 1 }}>
              <span className="field-label">关联表日期精度</span>
              <select
                className="select"
                value={granularity}
                onChange={(event) => setGranularity(event.target.value as 'year' | 'month')}
              >
                <option value="year">年（2026）</option>
                <option value="month">年月（2026-11）</option>
              </select>
            </label>
            <label className="field" style={{ flex: 2 }}>
              <span className="field-label">数据集版本</span>
              <input className="input mono" value="2026-11" readOnly />
            </label>
          </div>

          <label className="field" style={{ marginBottom: 10 }}>
            <span className="field-label">用途说明（与伦理批件对应）</span>
            <textarea className="textarea" value={purpose} onChange={(event) => setPurpose(event.target.value)} />
          </label>

          <div className="row">
            <button className="btn" type="button" disabled={previewing} onClick={() => void handlePreview()}>
              {previewing ? '预检中…' : '预检缺口'}
            </button>
            <button className="btn btn-stamp" type="submit" disabled={busy || purpose.trim().length < 6}>
              发起联合包
            </button>
          </div>
          <p className="faint" style={{ fontSize: 11, marginTop: 10 }}>
            admin / dpo 发起时自动批准并生成；client 发起后进入待审批。同范围重复发起会直接复用已存在的包，
            不会重新分配假名。
          </p>
        </form>

        <div className="panel panel-pad">
          <div className="subhead">审批备注</div>
          {can('exports:approve') ? (
            <>
              <label className="field">
                <span className="field-label">备注</span>
                <textarea className="textarea" value={note} onChange={(event) => setNote(event.target.value)} />
              </label>
              <p className="faint" style={{ fontSize: 11, marginTop: 10 }}>
                在下方台账里点「批准 / 驳回 / 重试」。批准后 worker 生成 patients.csv、visits.csv、
                labs.csv、manifest.json 与 bundle.zip；失败重试复用同一包编号。
              </p>
            </>
          ) : (
            <div className="notice">
              client 可发起、预检缺口、查看统计并下载已生成的包；审批与重试由 admin / dpo 执行。
            </div>
          )}
        </div>
      </div>

      {preview ? (
        <>
          <div className="section">
            <div className="subhead" style={{ borderBottom: 'none' }}>
              范围预检 / {preview.scope.patient_count} 名患者 / 日期精度 {GRANULARITY_LABELS[preview.scope.date_granularity]} /
              范围键 <span className="mono">{preview.scope.scope_key.slice(0, 14)}…</span>
            </div>
            {preview.existing_bundle_id ? (
              <div className="notice notice-warn section">
                同范围已有联合包（{preview.existing_status}），发起时会复用包编号{' '}
                <span className="mono">{shortId(preview.existing_bundle_id)}</span>。
              </div>
            ) : null}
            <div className="panel">
              <StatsTable stats={preview.stats} />
            </div>
          </div>
          <div className="section">
            <GapPanel stats={preview.stats} />
          </div>
        </>
      ) : null}

      <div className="section">
        <div className="kicker" style={{ marginBottom: 8 }}>
          联合包台账 / {items.length} 条
        </div>
        <div className="panel">
          {loading && !items.length ? (
            <div className="panel-pad faint">加载联合包台账…</div>
          ) : items.length ? (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 64 }}>编号</th>
                    <th>范围</th>
                    <th>用途 / 申请人</th>
                    <th style={{ width: 170 }}>各表 输入 / 保留 / 缺失</th>
                    <th style={{ width: 96 }}>状态</th>
                    <th style={{ width: 130 }}>文件摘要</th>
                    <th style={{ width: 210 }}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => {
                    const stats = item.stats ?? {};
                    return (
                      <tr key={item.id}>
                        <td className="mono faint">{shortId(item.id)}</td>
                        <td>
                          <div>
                            {item.scope.patient_count} 名患者{" "}
                            <span className="faint">/ {SCOPE_MODE_LABELS[item.scope.mode] ?? item.scope.mode}</span>
                          </div>
                          <div className="faint" style={{ fontSize: 10.5 }}>
                            日期 {GRANULARITY_LABELS[item.scope.date_granularity] ?? item.scope.date_granularity} ·
                            范围键 {item.scope_key.slice(0, 10)}…
                          </div>
                        </td>
                        <td className="muted" style={{ fontSize: 11.5, maxWidth: 240 }}>
                          <div>{item.purpose}</div>
                          <div className="faint" style={{ fontSize: 10.5, marginTop: 2 }}>
                            {item.requester_name} · {item.requester_email}
                          </div>
                        </td>
                        <td>
                          {tableKeys.map((key) => {
                            const row = statOf(stats, key);
                            return (
                              <div key={key} className="mono" style={{ fontSize: 10.5 }}>
                                {key}:{' '}
                                <span>{row ? formatNumber(row.input_rows) : '—'}</span> /{' '}
                                <span style={{ color: 'var(--olive)' }}>{row ? formatNumber(row.retained_rows) : '—'}</span>{' '}
                                /{' '}
                                <span style={{ color: row && row.missing_rows ? 'var(--stamp)' : undefined }}>
                                  {row ? formatNumber(row.missing_rows) : '—'}
                                </span>
                              </div>
                            );
                          })}
                        </td>
                        <td>
                          <StampBadge tone={statusTone(item.status)} rotate={-2}>
                            {EXPORT_STATUS_LABELS[item.status]}
                          </StampBadge>
                          <div className="faint" style={{ fontSize: 10, marginTop: 3 }}>
                            {BUILD_STATUS_LABELS[item.build_status]}
                            {item.attempt_count ? ` · 第 ${item.attempt_count} 次` : ''}
                          </div>
                          {item.error_detail ? (
                            <div className="faint" style={{ fontSize: 10, marginTop: 3, maxWidth: 160, color: 'var(--stamp)' }}>
                              {item.error_detail}
                            </div>
                          ) : null}
                        </td>
                        <td className="mono faint" style={{ fontSize: 10.5 }}>
                          {item.file_digest ? (
                            <>
                              {item.file_digest.slice(0, 14)}…
                              <div className="faint">{formatNumber(item.retained_rows)} 行入包</div>
                            </>
                          ) : (
                            '—'
                          )}
                        </td>
                        <td>
                          <div className="row" style={{ gap: 4 }}>
                            {can('exports:approve') && item.status === 'pending' ? (
                              <>
                                <button
                                  className="btn"
                                  type="button"
                                  style={{ padding: '3px 8px', fontSize: 11 }}
                                  disabled={busy}
                                  onClick={() => void handleApprove(item.id)}
                                >
                                  批准
                                </button>
                                <button
                                  className="btn"
                                  type="button"
                                  style={{ padding: '3px 8px', fontSize: 11 }}
                                  disabled={busy}
                                  onClick={() => void handleReject(item.id)}
                                >
                                  驳回
                                </button>
                              </>
                            ) : null}
                            {item.can_retry && (can('exports:approve') || item.requester_id === user?.id) ? (
                              <button
                                className="btn"
                                type="button"
                                style={{ padding: '3px 8px', fontSize: 11 }}
                                disabled={busy}
                                onClick={() => void handleRetry(item.id)}
                              >
                                重试
                              </button>
                            ) : null}
                            {item.download_ready ? (
                              <>
                                <button
                                  className="btn"
                                  type="button"
                                  style={{ padding: '3px 8px', fontSize: 11 }}
                                  onClick={() => void handleDownload(item.id)}
                                >
                                  下载 zip
                                </button>
                                {tableKeys.map((key) => (
                                  <button
                                    key={key}
                                    className="btn-quiet"
                                    type="button"
                                    style={{ fontSize: 10.5 }}
                                    onClick={() => void handleDownload(item.id, key)}
                                  >
                                    {key}.csv
                                  </button>
                                ))}
                              </>
                            ) : null}
                          </div>
                          {item.review_note ? (
                            <div className="faint" style={{ fontSize: 10.5, marginTop: 4, maxWidth: 200 }}>
                              审批：{item.review_note}
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="还没有联合包" hint="在上方选定患者范围，先预检缺口，再发起联合包。" />
          )}
        </div>
      </div>
    </>
  );
};
