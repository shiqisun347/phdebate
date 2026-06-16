import * as React from "react";
import { useToast } from "./toast";
import { useAdminData } from "./data";

/**
 * Wraps an async admin action with: pending flag, toast on success/error,
 * and an automatic snapshot refresh. Returns [run, pending].
 */
export function useAction() {
  const toast = useToast();
  const { refresh } = useAdminData();
  const [pending, setPending] = React.useState(false);

  const run = React.useCallback(
    async (fn: () => Promise<unknown>, opts?: { success?: string; refresh?: boolean }) => {
      setPending(true);
      try {
        await fn();
        if (opts?.refresh !== false) await refresh();
        if (opts?.success) toast(opts.success, "success");
        return true;
      } catch (err) {
        toast(err instanceof Error ? err.message : "操作失败", "error");
        return false;
      } finally {
        setPending(false);
      }
    },
    [refresh, toast]
  );

  return { run, pending };
}
