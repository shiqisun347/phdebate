import type { LucideIcon } from "lucide-react";
import {
  LayoutDashboard,
  Trophy,
  ListChecks,
  Bot,
  AudioLines,
  Database,
  Sparkles,
  ShieldCheck,
  Users,
  Workflow,
  Stethoscope,
  MonitorPlay,
  ScrollText,
  MessagesSquare,
} from "lucide-react";

export type ModuleId =
  | "overview"
  | "matches"
  | "rulesets"
  | "agents"
  | "speech"
  | "xiaoqi"
  | "data"
  | "logs"
  | "security"
  | "debaters"
  | "flow"
  | "diagnostics"
  | "control"
  | "debate-process";

export interface NavItem {
  id: ModuleId;
  label: string;
  icon: LucideIcon;
  desc: string;
}

export interface NavZone {
  key: "global" | "match" | "live";
  title: string;
  hint: string;
  items: NavItem[];
}

export const NAV: NavZone[] = [
  {
    key: "global",
    title: "全局设置",
    hint: "面向整个系统 · 与具体比赛无关",
    items: [
      { id: "overview", label: "概览", icon: LayoutDashboard, desc: "系统与当前比赛概况" },
      { id: "matches", label: "比赛管理", icon: Trophy, desc: "比赛的增删改与切换" },
      { id: "rulesets", label: "赛制规则", icon: ListChecks, desc: "预设赛制规则库" },
      { id: "agents", label: "Agent 管理", icon: Bot, desc: "AI 辩手接入配置" },
      { id: "speech", label: "语音引擎", icon: AudioLines, desc: "TTS / ASR 设置与自检" },
      { id: "xiaoqi", label: "小七管理", icon: Sparkles, desc: "接口信息 / 请求体 / 请求测试" },
      { id: "data", label: "数据管理", icon: Database, desc: "历史比赛数据与导出" },
      { id: "logs", label: "日志查看", icon: ScrollText, desc: "完整输入输出 · 多级分类" },
      { id: "security", label: "安全管理", icon: ShieldCheck, desc: "登录密码与访问控制" },
    ],
  },
  {
    key: "match",
    title: "当前比赛管理",
    hint: "配置当前激活的比赛",
    items: [
      { id: "debaters", label: "辩手管理", icon: Users, desc: "按赛制设置正反方辩手" },
      { id: "flow", label: "比赛流程", icon: Workflow, desc: "可视化预设流程" },
    ],
  },
  {
    key: "live",
    title: "现场控制",
    hint: "赛前调试 + 现场控场",
    items: [
      { id: "diagnostics", label: "调试与总览", icon: Stethoscope, desc: "设备与功能赛前自检" },
      { id: "control", label: "控场台", icon: MonitorPlay, desc: "现场控制" },
      { id: "debate-process", label: "实时辩论过程", icon: MessagesSquare, desc: "查看与修正历史辩论" },
    ],
  },
];

export const ALL_ITEMS: NavItem[] = NAV.flatMap((z) => z.items);

export function findItem(id: ModuleId): NavItem | undefined {
  return ALL_ITEMS.find((i) => i.id === id);
}
