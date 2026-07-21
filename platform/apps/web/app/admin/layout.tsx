import type { Metadata } from "next";

export const metadata: Metadata = { title: "系统管理｜稷下辩论" };

export default function AdminLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
