import type { Metadata } from "next";
import "./globals.css";
import { GlobalNav } from "@/components/global-nav";

export const metadata: Metadata = {
  title: "稷下辩论｜AI 时代的辩论竞技场",
  description: "真人与 AI 同场竞技的自动化辩论平台"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <a className="skip-link" href="#main-content">跳到主要内容</a>
        <div className="ambient ambient-a" />
        <div className="ambient ambient-b" />
        <GlobalNav />
        <main id="main-content">{children}</main>
        <footer className="site-footer">
          <span>稷下辩论 JIXIA DEBATE</span>
          <span>真人思辨 · AI 协作 · 自动赛程</span>
        </footer>
      </body>
    </html>
  );
}
