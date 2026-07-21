"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const STAGE_ROUTE = /^\/rooms\/[^/]+\/(?:debate|watch)(?:\/|$)/;

export function MainContent({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  if (STAGE_ROUTE.test(pathname)) {
    return <div id="main-content" tabIndex={-1}>{children}</div>;
  }

  return <main id="main-content" tabIndex={-1}>{children}</main>;
}
