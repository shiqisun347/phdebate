import type { Metadata } from "next";

const competitionTitles: Record<string, string> = {
  "daily-4v4": "4v4 人机辩论正式赛",
  "training-1v1": "1v1 辩论训练赛",
};

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params;
  return { title: `${competitionTitles[slug] || "赛事详情"}｜稷下辩论` };
}

export default function CompetitionLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
