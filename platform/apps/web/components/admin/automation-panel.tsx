import { competitionDisplayNameFromStoredName } from "@/lib/primary-competition";
import type { AutomationTemplate } from "@/components/admin/admin-module-types";

type AutomationPanelProps = {
  templates: AutomationTemplate[];
  saving: boolean;
  onVersion: (template: AutomationTemplate) => void;
};

export default function AutomationPanel({ templates, saving, onVersion }: AutomationPanelProps) {
  return (
    <>
      <div className="panel-title">
        <h2>自动比赛流程</h2>
        <span className="muted">每次修改创建新版本；已创建房间继续使用原快照</span>
      </div>
      <div className="table-wrap" role="region" aria-label="自动流程模板表格" tabIndex={0}>
        <table>
          <thead><tr><th>模板</th><th>版本</th><th>阶段</th><th>绑定赛事</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            {templates.map((item) => (
              <tr key={item.id}>
                <td><strong>{item.name}</strong><small className="muted"> · {item.slug}</small></td>
                <td>v{item.version}</td>
                <td>{item.stages.length}</td>
                <td>{item.competitions.map((competition) => competitionDisplayNameFromStoredName(competition.name)).join("、") || "历史版本"}</td>
                <td>{new Date(item.created_at).toLocaleString("zh-CN")}</td>
                <td>
                  {item.slug.startsWith("legacy") ? (
                    <span className="muted">只读归档</span>
                  ) : (
                    <button className="text-button" disabled={saving} onClick={() => onVersion(item)}>创建新版本</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!templates.length && <div className="empty">暂无自动流程模板</div>}
      </div>
    </>
  );
}
