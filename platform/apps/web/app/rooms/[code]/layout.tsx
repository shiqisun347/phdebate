import type { Metadata } from "next";

export async function generateMetadata({ params }: { params: Promise<{ code: string }> }): Promise<Metadata> {
  const { code } = await params;
  return { title: `比赛房间 #${code}｜稷下辩论` };
}

export default function RoomLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
