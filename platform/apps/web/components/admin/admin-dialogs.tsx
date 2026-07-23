import type { Dispatch, FormEvent, SetStateAction } from "react";

import type { Competition, User } from "@/lib/types";
import type { AutomationTemplate, Review } from "@/components/admin/admin-module-types";

type PasswordResetForm = { new_password: string; confirm_password: string };
type ReviewForm = { winner: string; affirmative_score: number; negative_score: number; reasoning: string };

export function TopicDialog({ competition, title, saving, onTitleChange, onClose, onSubmit }: {
  competition: Competition;
  title: string;
  saving: boolean;
  onTitleChange: (value: string) => void;
  onClose: () => void;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <div className="dialog-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <form className="dialog" role="dialog" aria-modal="true" aria-label="添加赛事题目" onSubmit={onSubmit}>
        <div className="dialog-head"><div><span className="eyebrow">Topic Library</span><h2>添加辩题</h2></div><button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>×</button></div>
        <p className="muted">将题目加入“{competition.name}”题库，之后创建房间时即可选择。</p>
        <div className="field"><label htmlFor="admin-topic-title">辩题内容</label><textarea id="admin-topic-title" className="textarea" minLength={4} maxLength={300} required autoFocus value={title} onChange={(event) => onTitleChange(event.target.value)} placeholder="例如：人工智能时代，还要不要学编程？" /></div>
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>取消</button><button className="button" disabled={saving || title.trim().length < 4}>{saving ? "保存中…" : "保存题目"}</button></div>
      </form>
    </div>
  );
}

export function PasswordResetDialog({ user, form, saving, onFormChange, onClose, onSubmit }: {
  user: User;
  form: PasswordResetForm;
  saving: boolean;
  onFormChange: Dispatch<SetStateAction<PasswordResetForm>>;
  onClose: () => void;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <div className="dialog-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <form className="dialog" role="dialog" aria-modal="true" aria-label="重置用户密码" onSubmit={onSubmit}>
        <div className="dialog-head"><div><span className="eyebrow">Account Recovery</span><h2>重置用户密码</h2></div><button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>×</button></div>
        <p className="muted">为 {user.real_name}（@{user.account}）设置临时密码。提交后，该用户的所有已登录设备会立即退出。</p>
        <div className="notice-box">审计日志只记录会话撤销数量，不会保存或展示新密码。请通过安全方式将密码告知用户，并提醒其登录后自行修改。</div>
        <div className="field"><label htmlFor="admin-reset-password">新密码</label><input id="admin-reset-password" className="input" type="password" autoComplete="new-password" minLength={8} maxLength={128} required autoFocus value={form.new_password} onChange={(event) => onFormChange((value) => ({ ...value, new_password: event.target.value }))} /></div>
        <div className="field"><label htmlFor="admin-reset-password-confirm">确认新密码</label><input id="admin-reset-password-confirm" className="input" type="password" autoComplete="new-password" minLength={8} maxLength={128} required value={form.confirm_password} onChange={(event) => onFormChange((value) => ({ ...value, confirm_password: event.target.value }))} /></div>
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>取消</button><button className="button" disabled={saving || form.new_password.length < 8 || form.new_password !== form.confirm_password}>{saving ? "重置中…" : "确认重置"}</button></div>
      </form>
    </div>
  );
}

export function TemplateVersionDialog({ template, competitions, name, stages, competitionIds, saving, onNameChange, onStagesChange, onCompetitionIdsChange, onClose, onSubmit }: {
  template: AutomationTemplate;
  competitions: Competition[];
  name: string;
  stages: string;
  competitionIds: string[];
  saving: boolean;
  onNameChange: (value: string) => void;
  onStagesChange: (value: string) => void;
  onCompetitionIdsChange: Dispatch<SetStateAction<string[]>>;
  onClose: () => void;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <div className="dialog-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <form className="dialog dialog-wide" role="dialog" aria-modal="true" aria-label="创建自动流程新版本" onSubmit={onSubmit}>
        <div className="dialog-head"><div><span className="eyebrow">Versioned Automation</span><h2>创建自动流程新版本</h2></div><button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>×</button></div>
        <div className="notice-box">源版本：{template.name} v{template.version}。保存后只影响新创建房间，进行中和历史房间继续使用原快照。</div>
        <div className="field"><label htmlFor="template-name">模板名称</label><input id="template-name" className="input" maxLength={120} required value={name} onChange={(event) => onNameChange(event.target.value)} /></div>
        <div className="field">
          <label>绑定到赛事</label>
          <div className="seat-picker">
            {competitions.map((competition) => (
              <label className={`seat-option ${competitionIds.includes(competition.id) ? "selected" : ""}`} key={competition.id}>
                <input type="checkbox" checked={competitionIds.includes(competition.id)} onChange={(event) => onCompetitionIdsChange((current) => event.target.checked ? [...current, competition.id] : current.filter((id) => id !== competition.id))} />
                <strong>{competition.name}</strong><small>{competition.format} · {competition.seat_count} 席</small>
              </label>
            ))}
          </div>
        </div>
        <div className="field"><label htmlFor="template-stages">阶段 JSON</label><textarea id="template-stages" className="textarea code-textarea" rows={18} required value={stages} onChange={(event) => onStagesChange(event.target.value)} spellCheck={false} /><small className="muted">最后阶段必须为 judging；speech 需要 seat；free 需要 side，且 turn_duration 为 5–30 秒。</small></div>
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>取消</button><button className="button" disabled={saving || !name.trim() || !stages.trim()}>{saving ? "创建中…" : "创建并绑定新版本"}</button></div>
      </form>
    </div>
  );
}

export function ReviewDialog({ review, mode, form, saving, onFormChange, onClose, onSubmit }: {
  review: Review;
  mode: "approve" | "correct";
  form: ReviewForm;
  saving: boolean;
  onFormChange: Dispatch<SetStateAction<ReviewForm>>;
  onClose: () => void;
  onSubmit: (event: FormEvent) => void;
}) {
  const correcting = mode === "correct";
  return (
    <div className="dialog-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <form className="dialog" role="dialog" aria-modal="true" aria-label={correcting ? "修正比赛结果" : "复核比赛结果"} onSubmit={onSubmit}>
        <div className="dialog-head"><div><span className="eyebrow">{correcting ? "Result Correction" : "Judge Review"}</span><h2>{correcting ? "修正比赛结果" : "复核比赛结果"}</h2></div><button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>×</button></div>
        <p className="muted">房间 #{review.room_code} · {review.topic}</p>
        {correcting && <div className="notice-box">原始积分记录不会被覆盖；系统将追加补偿记录并保留完整审计轨迹。</div>}
        <div className="review-form-grid">
          <div className="field"><label htmlFor="review-winner">胜方</label><select id="review-winner" className="select" value={form.winner} onChange={(event) => onFormChange((value) => ({ ...value, winner: event.target.value }))}><option value="aff">正方</option><option value="neg">反方</option><option value="draw">平局</option></select></div>
          <div className="field"><label htmlFor="review-aff-score">正方评分</label><input id="review-aff-score" className="input" type="number" min={0} max={100} step={0.1} value={form.affirmative_score} onChange={(event) => onFormChange((value) => ({ ...value, affirmative_score: Number(event.target.value) }))} /></div>
          <div className="field"><label htmlFor="review-neg-score">反方评分</label><input id="review-neg-score" className="input" type="number" min={0} max={100} step={0.1} value={form.negative_score} onChange={(event) => onFormChange((value) => ({ ...value, negative_score: Number(event.target.value) }))} /></div>
        </div>
        <div className="field"><label htmlFor="review-reasoning">{correcting ? "修正理由" : "复核理由"}</label><textarea id="review-reasoning" className="textarea" minLength={2} maxLength={5000} required value={form.reasoning} onChange={(event) => onFormChange((value) => ({ ...value, reasoning: event.target.value }))} /></div>
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>取消</button><button className="button" disabled={saving || form.reasoning.trim().length < 2}>{saving ? "提交中…" : correcting ? "确认修正" : "确认判定"}</button></div>
      </form>
    </div>
  );
}
