import type { Metadata } from "next";

export const metadata: Metadata = { title: "登录｜稷下辩论" };

export default function LoginLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
