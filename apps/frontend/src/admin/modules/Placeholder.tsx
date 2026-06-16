import { Construction } from "lucide-react";
import { EmptyState } from "../ui/primitives";

export function Placeholder({ label }: { label: string }) {
  return (
    <EmptyState
      icon={<Construction />}
      title={`${label} · 建设中`}
      description="该模块将在后续阶段交付。"
    />
  );
}
