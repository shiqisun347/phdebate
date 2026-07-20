import type { FormEvent } from "react";

import { competitionDisplayName } from "@/lib/primary-competition";
import type { Competition, Season } from "@/lib/types";
import type { SeasonForm } from "@/components/admin/admin-module-types";

type Props = {
  competitions: Competition[];
  seasons: Season[];
  seasonForm: SeasonForm;
  saving: boolean;
  onSeasonFormChange: (value: SeasonForm) => void;
  onCreateSeason: (event: FormEvent) => void;
  onToggleSeason: (season: Season) => void;
  onPatchCompetition: (id: string, patch: Record<string, unknown>) => void;
  onPatchTopic: (competitionId: string, topicId: string, patch: Record<string, unknown>) => void;
  onAddTopic: (id: string) => void;
  onToggleCompetition: (competition: Competition) => void;
};

const isLegacyCompetition = (competition: Pick<Competition, "slug" | "format">) =>
  competition.format === "legacy" || competition.slug.startsWith("legacy");

export default function CompetitionsModule({
  competitions,
  seasons,
  seasonForm,
  saving,
  onSeasonFormChange,
  onCreateSeason,
  onToggleSeason,
  onPatchCompetition,
  onPatchTopic,
  onAddTopic,
  onToggleCompetition,
}: Props) {
  return (
    <>
      <div className="panel-title">
        <h2>赛事与题库</h2>
        <span className="muted">题目停用后不会出现在新建房间中，历史比赛不受影响</span>
      </div>
      <div className="panel-title" style={{ marginTop: 24 }}>
        <h3>赛季生命周期</h3>
        <span className="badge">房间创建时固定赛季</span>
      </div>
      <form className="panel-subsection form-stack" onSubmit={onCreateSeason}>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="season-name">赛季名称</label>
            <input id="season-name" className="input" required maxLength={100} value={seasonForm.name} onChange={(event) => onSeasonFormChange({ ...seasonForm, name: event.target.value })} placeholder="第二赛季" />
          </div>
          <div className="field">
            <label htmlFor="season-slug">赛季标识</label>
            <input id="season-slug" className="input" required pattern="[a-z0-9][a-z0-9-]*" maxLength={80} value={seasonForm.slug} onChange={(event) => onSeasonFormChange({ ...seasonForm, slug: event.target.value.toLowerCase() })} placeholder="season-2" />
          </div>
        </div>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="season-start">开始时间</label>
            <input id="season-start" className="input" type="datetime-local" required value={seasonForm.starts_at} onChange={(event) => onSeasonFormChange({ ...seasonForm, starts_at: event.target.value })} />
          </div>
          <div className="field">
            <label htmlFor="season-end">结束时间</label>
            <input id="season-end" className="input" type="datetime-local" value={seasonForm.ends_at} onChange={(event) => onSeasonFormChange({ ...seasonForm, ends_at: event.target.value })} />
          </div>
        </div>
        <div className="card-actions"><button className="button button-small" disabled={saving}>创建赛季</button></div>
      </form>
      <div className="table-wrap" role="region" aria-label="赛季管理表格" tabIndex={0} style={{ marginTop: 18 }}>
        <table>
          <thead><tr><th>赛季</th><th>时间范围</th><th>赛事 / 比赛</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {seasons.map((season) => (
              <tr key={season.id}>
                <td><strong>{season.name}</strong><small className="muted">{season.slug}</small></td>
                <td>{new Date(season.starts_at).toLocaleString("zh-CN")}<small className="muted">至 {season.ends_at ? new Date(season.ends_at).toLocaleString("zh-CN") : "长期"}</small></td>
                <td>{season.competition_count || 0} 个赛事 · {season.match_count || 0} 场比赛</td>
                <td>{season.is_open ? "进行中" : season.is_active ? "未开放或已结束" : "已停用"}</td>
                <td><button className="text-button" disabled={saving} onClick={() => onToggleSeason(season)}>{season.is_active ? "停用" : "启用"}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel-title" style={{ marginTop: 30 }}><h3>赛事配置与题库</h3></div>
      <div className="competition-grid">
        {competitions.map((item) => (
          <article className="competition-card" key={item.id}>
            <span className="badge">{item.format}</span>
            <h3>{competitionDisplayName(item)}</h3>
            <p>{item.tagline}</p>
            <div className="card-meta"><span>{item.ranked ? "积分赛" : "训练赛"}</span><span>{item.seat_count} 席位</span></div>
            {isLegacyCompetition(item) ? (
              <div className="field">
                <span className="label">归档属性</span>
                <strong>历史记录 · 永久只读</strong>
                <small className="muted">仅用于查询迁移比赛，不能重新启用、绑定赛季或维护题库。</small>
              </div>
            ) : (
              <div className="field">
                <label htmlFor={`competition-season-${item.id}`}>绑定赛季</label>
                <select id={`competition-season-${item.id}`} className="select" value={item.season?.id || ""} disabled={saving} onChange={(event) => onPatchCompetition(item.id, { season_id: event.target.value })}>
                  <option value="" disabled>未绑定赛季</option>
                  {seasons.filter((season) => season.is_active).map((season) => <option key={season.id} value={season.id}>{season.name}{season.is_open ? " · 进行中" : ""}</option>)}
                </select>
                <small className="muted">已创建房间继续使用原赛季</small>
              </div>
            )}
            <details className="competition-topics">
              <summary>题库管理 · {item.topics?.filter((topic) => topic.is_active !== false).length || 0} 个启用 / {item.topics?.length || 0} 个全部</summary>
              <div className="history-list">
                {item.topics?.map((topic) => (
                  <div className="history-row" style={{ gridTemplateColumns: "1fr auto" }} key={topic.id}>
                    <span className="row-main"><strong>{topic.title}</strong><small>{topic.is_active === false ? "已停用" : "创建房间时可选"}</small></span>
                    {!isLegacyCompetition(item) && <button className="text-button" disabled={saving} onClick={() => onPatchTopic(item.id, topic.id, { is_active: topic.is_active === false })}>{topic.is_active === false ? "启用" : "停用"}</button>}
                  </div>
                ))}
                {!item.topics?.length && <div className="empty">该赛事还没有题目</div>}
              </div>
            </details>
            {!isLegacyCompetition(item) && (
              <div className="card-actions">
                <button className="button button-small" onClick={() => onAddTopic(item.id)}>添加题目</button>
                <button className="button button-small button-secondary" onClick={() => onToggleCompetition(item)}>{item.is_active === false ? "启用赛事" : "停用赛事"}</button>
              </div>
            )}
          </article>
        ))}
      </div>
    </>
  );
}
