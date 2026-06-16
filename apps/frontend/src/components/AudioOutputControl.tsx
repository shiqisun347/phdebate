import { Monitor, RadioTower, Settings2, Volume2, VolumeX } from "lucide-react";
import type { AudioOutputMode, AudioOutputState } from "../types/contracts";

interface AudioOutputControlProps {
  state?: AudioOutputState;
  activeRole: "host" | "admin" | "screen";
  onModeChange: (mode: AudioOutputMode) => void;
  onTest: () => void;
}

const audioOutputOptions: Array<{
  mode: AudioOutputMode;
  label: string;
  detail: string;
  icon: typeof Volume2;
}> = [
  { mode: "host", label: "主持导播台", detail: "主持电脑连接大屏/音响时选择", icon: RadioTower },
  { mode: "admin", label: "技术后台", detail: "技术电脑连接大屏/音响时选择", icon: Settings2 },
  { mode: "screen", label: "大屏幕电脑", detail: "大屏电脑连接音响时选择（推荐）", icon: Monitor },
  { mode: "off", label: "关闭声音", detail: "临时排障或静音彩排", icon: VolumeX }
];

export function AudioOutputControl({ state, activeRole, onModeChange, onTest }: AudioOutputControlProps) {
  const mode = state?.mode ?? "host";
  const isLocalOutput = mode === activeRole;
  const roleLabel = activeRole === "host" ? "本机是主持导播台" : "本机是技术后台";
  const currentLabel = mode === "off" ? "已关闭" : state?.label ?? audioOutputLabel(mode);

  return (
    <div className="audio-output-control">
      <div className="audio-output-head">
        <span><Volume2 size={16} />现场声音输出</span>
        <strong className={isLocalOutput ? "active" : mode === "off" ? "off" : ""}>{currentLabel}</strong>
      </div>
      <div className="audio-output-options" role="group" aria-label="选择连接大屏和音响的电脑">
        {audioOutputOptions.map((item) => {
          const Icon = item.icon;
          const selected = mode === item.mode;
          const local = selected && isLocalOutput;
          return (
            <button
              type="button"
              className={`${selected ? "selected" : ""} ${local ? "local" : ""}`}
              key={item.mode}
              onClick={() => onModeChange(item.mode)}
              aria-pressed={selected}
            >
              <Icon size={15} />
              <strong>{item.label}</strong>
              <span>{item.detail}</span>
            </button>
          );
        })}
      </div>
      <div className="audio-output-row">
        <span>音响连接电脑</span>
        <button type="button" onClick={onTest} disabled={!isLocalOutput} title={isLocalOutput ? "播放一声本机测试铃" : "只有当前指定输出端可以测试本机声音"}>
          <Volume2 size={15} />测试本机铃
        </button>
      </div>
      <p>
        {isLocalOutput
          ? `${roleLabel}，当前这台电脑是唯一外放端；请保持本页打开，并确认系统声音输出到大屏/音响。`
          : mode === "off"
            ? "所有浏览器提示音已关闭，适合临时排障。"
            : `当前由${currentLabel}播放，${roleLabel}不会外放。`}
        辩手端和投票端不播放铃声或 TTS。
      </p>
    </div>
  );
}

function audioOutputLabel(mode: AudioOutputMode): string {
  if (mode === "admin") return "技术后台电脑";
  if (mode === "screen") return "大屏幕电脑";
  if (mode === "off") return "关闭浏览器提示音";
  return "主持导播台电脑";
}
