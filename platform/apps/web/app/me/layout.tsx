import type { Metadata } from "next";

export const metadata: Metadata = { title: "个人中心｜稷下辩论" };

export default function MeLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
