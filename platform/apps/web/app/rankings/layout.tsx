import type { Metadata } from "next";

export const metadata: Metadata = { title: "赛季排行榜｜稷下辩论" };

export default function RankingsLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
