import * as React from "react";
import { Button } from "./primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "./Dialog";

interface ConfirmOptions {
  title: string;
  description?: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  tone?: "default" | "destructive";
}

interface ConfirmState extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

/**
 * In-UI confirmation dialog (replaces the browser's native `confirm`).
 * Returns `confirm(opts)` -> Promise<boolean> and a `dialog` node to render.
 */
export function useConfirm() {
  const [state, setState] = React.useState<ConfirmState | null>(null);

  const confirm = React.useCallback(
    (opts: ConfirmOptions) => new Promise<boolean>((resolve) => setState({ ...opts, resolve })),
    []
  );

  const settle = React.useCallback(
    (ok: boolean) => {
      setState((current) => {
        current?.resolve(ok);
        return null;
      });
    },
    []
  );

  const dialog = state ? (
    <Dialog open onClose={() => settle(false)} size="sm">
      <DialogHeader title={state.title} onClose={() => settle(false)} />
      {state.description && <DialogBody>{<div className="text-sm text-muted-foreground">{state.description}</div>}</DialogBody>}
      <DialogFooter>
        <Button variant="outline" onClick={() => settle(false)}>
          {state.cancelText ?? "取消"}
        </Button>
        <Button variant={state.tone === "destructive" ? "destructive" : "default"} onClick={() => settle(true)}>
          {state.confirmText ?? "确认"}
        </Button>
      </DialogFooter>
    </Dialog>
  ) : null;

  return { confirm, dialog };
}
