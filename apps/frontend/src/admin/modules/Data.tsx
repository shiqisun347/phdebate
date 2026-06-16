import * as React from "react";
import { Database, Download, Package, RefreshCw, HardDrive, FileArchive } from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, Badge, Spinner, Separator, EmptyState } from "../ui/primitives";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { getDataSummary, createExportBundle, withCurrentAuthQuery } from "../../api/client";
import type { DataSummary } from "../../types/contracts";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const COUNT_LABELS: Record<string, string> = {
  phases: "环节",
  speakers: "辩手",
  human_speakers: "人类辩手",
  agent_speakers: "AI 辩手",
  agent_configs: "Agent 配置",
  transcript_segments: "转写片段",
  final_transcript_segments: "终稿片段",
  speech_revisions: "发言修订",
  audio_assets: "音频文件",
  audio_chunks: "音频分片",
  audience_votes: "学生投票",
  agent_requests: "Agent 请求",
  speech_service_requests: "语音请求",
  export_bundles: "导出包",
  events: "事件",
  audit_logs: "审计日志",
  archives: "归档",
};

export function Data() {
  const { matchId } = useAdminData();
  const toast = useToast();
  const [summary, setSummary] = React.useState<DataSummary | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [exporting, setExporting] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      setSummary(await getDataSummary(matchId));
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载失败", "error");
    } finally {
      setLoading(false);
    }
  }, [matchId, toast]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function doExport() {
    setExporting(true);
    try {
      const bundle = await createExportBundle(matchId);
      toast("导出包已生成", "success");
      window.open(withCurrentAuthQuery(bundle.download_url), "_blank");
      await load();
    } catch (err) {
      toast(err instanceof Error ? err.message : "导出失败", "error");
    } finally {
      setExporting(false);
    }
  }

  if (loading && !summary) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载数据概况…
      </div>
    );
  }
  if (!summary) return null;

  const counts = Object.entries(summary.counts).filter(([k]) => COUNT_LABELS[k]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <HardDrive className="size-4" />
          {summary.persistence.driver} · <span className="font-mono text-xs">{summary.persistence.database_path}</span>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={load} loading={loading}>
            <RefreshCw /> 刷新
          </Button>
          <Button onClick={doExport} loading={exporting}>
            <Package /> 导出当前比赛
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>当前比赛数据统计</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
            {counts.map(([k, v]) => (
              <div key={k} className="rounded-lg border border-border p-3 text-center">
                <p className="text-xl font-bold text-foreground">{v as number}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">{COUNT_LABELS[k]}</p>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>历史归档与导出</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {summary.latest_export && (
            <div className="flex items-center justify-between rounded-md border border-border p-3">
              <div className="flex items-center gap-2">
                <FileArchive className="size-4 text-primary" />
                <div>
                  <p className="text-sm font-medium text-foreground">最新导出包</p>
                  <p className="text-xs text-muted-foreground">
                    {fmtBytes(summary.latest_export.size_bytes)} · {summary.latest_export.entry_count} 个文件
                  </p>
                </div>
              </div>
              <a href={withCurrentAuthQuery(summary.latest_export.download_url)} target="_blank" rel="noreferrer">
                <Button size="sm" variant="outline">
                  <Download /> 下载
                </Button>
              </a>
            </div>
          )}
          <Separator />
          {summary.archives.length === 0 ? (
            <EmptyState icon={<Database />} title="暂无历史归档" description="比赛结束并归档后会出现在此处。" />
          ) : (
            summary.archives.map((a) => (
              <div key={a.id} className="flex items-center justify-between rounded-md border border-border p-3">
                <div>
                  <p className="text-sm font-medium text-foreground">{a.title || a.archived_match_id}</p>
                  <p className="text-xs text-muted-foreground">
                    {a.topic} · 转写 {a.counts.transcript_segments} · 音频 {a.counts.audio_assets} · 投票 {a.counts.audience_votes}
                  </p>
                </div>
                {a.export_bundle && (
                  <a href={withCurrentAuthQuery(a.export_bundle.download_url)} target="_blank" rel="noreferrer">
                    <Button size="sm" variant="outline">
                      <Download /> {fmtBytes(a.export_bundle.size_bytes)}
                    </Button>
                  </a>
                )}
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">最近事件</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-1.5">
            {summary.recent_events.slice(0, 10).map((e) => (
              <div key={e.id} className="flex items-center justify-between text-xs">
                <span className="font-mono text-muted-foreground">#{e.seq}</span>
                <Badge variant="secondary">{e.type}</Badge>
                <span className="text-muted-foreground">{new Date(e.created_at).toLocaleTimeString()}</span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
