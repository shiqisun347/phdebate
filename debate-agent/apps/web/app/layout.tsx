import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "稷下 Debate Agent",
  description: "独立辩论 Agent 模型、Prompt、Memory 与服务管理平台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
