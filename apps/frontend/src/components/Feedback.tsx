import { AlertTriangle, CheckCircle2, Info, LoaderCircle, X, XCircle } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useMemo, useRef, useState } from "react";

type FeedbackTone = "info" | "success" | "error" | "loading";
type ConfirmTone = "normal" | "danger";

interface Toast {
  id: string;
  tone: FeedbackTone;
  title: string;
  message?: string;
  sticky?: boolean;
}

interface ConfirmRequest {
  id: string;
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  tone: ConfirmTone;
  resolve: (confirmed: boolean) => void;
}

interface RunOptions {
  confirmText?: string;
  confirmTitle?: string;
  confirmLabel?: string;
  successText?: string;
  errorText?: string;
  loadingText?: string;
  danger?: boolean;
}

interface FeedbackContextValue {
  notify: (toast: Omit<Toast, "id">) => string;
  dismiss: (id: string) => void;
  update: (id: string, toast: Partial<Omit<Toast, "id">>) => void;
  confirm: (request: {
    title?: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    tone?: ConfirmTone;
  }) => Promise<boolean>;
  run: <T>(title: string, task: () => Promise<T>, options?: RunOptions) => Promise<T | undefined>;
}

const FeedbackContext = createContext<FeedbackContextValue | null>(null);

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);
  const toastCounterRef = useRef(0);

  const dismiss = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const notify = useCallback((toast: Omit<Toast, "id">) => {
    const id = `toast_${Date.now()}_${toastCounterRef.current}`;
    toastCounterRef.current += 1;
    const nextToast = { ...toast, id };
    setToasts((current) => [nextToast, ...current].slice(0, 4));
    if (!toast.sticky && toast.tone !== "loading") {
      window.setTimeout(() => dismiss(id), toast.tone === "error" ? 5200 : 3200);
    }
    return id;
  }, [dismiss]);

  const update = useCallback((id: string, toast: Partial<Omit<Toast, "id">>) => {
    setToasts((current) => current.map((item) => item.id === id ? { ...item, ...toast } : item));
    if (toast.tone && toast.tone !== "loading") {
      window.setTimeout(() => dismiss(id), toast.tone === "error" ? 5200 : 2800);
    }
  }, [dismiss]);

  const confirm = useCallback<FeedbackContextValue["confirm"]>((request) => (
    new Promise((resolve) => {
      setConfirmRequest({
        id: `confirm_${Date.now()}`,
        title: request.title ?? "确认操作",
        message: request.message,
        confirmLabel: request.confirmLabel ?? "确认",
        cancelLabel: request.cancelLabel ?? "取消",
        tone: request.tone ?? "normal",
        resolve
      });
    })
  ), []);

  const settleConfirm = useCallback((confirmed: boolean) => {
    setConfirmRequest((current) => {
      current?.resolve(confirmed);
      return null;
    });
  }, []);

  const run = useCallback<FeedbackContextValue["run"]>(async (title, task, options = {}) => {
    if (options.confirmText) {
      const confirmed = await confirm({
        title: options.confirmTitle,
        message: options.confirmText,
        confirmLabel: options.confirmLabel,
        tone: options.danger ? "danger" : "normal"
      });
      if (!confirmed) return undefined;
    }

    let toastId: string | null = null;
    const loadingTimer = window.setTimeout(() => {
      toastId = notify({
        tone: "loading",
        title,
        message: options.loadingText ?? "正在处理，请稍候。",
        sticky: true
      });
    }, 520);

    try {
      const result = await task();
      window.clearTimeout(loadingTimer);
      if (toastId) {
        update(toastId, {
          tone: "success",
          title: options.successText ?? `${title}完成`,
          message: "已同步到现场状态。",
          sticky: false
        });
      } else {
        notify({
          tone: "success",
          title: options.successText ?? `${title}完成`,
          message: "已同步到现场状态。"
        });
      }
      return result;
    } catch (error) {
      window.clearTimeout(loadingTimer);
      const payload = {
        tone: "error" as const,
        title: options.errorText ?? `${title}失败`,
        message: error instanceof Error ? error.message : "请稍后重试或联系技术人员。",
        sticky: false
      };
      if (toastId) update(toastId, payload);
      else notify(payload);
      throw error;
    }
  }, [confirm, notify, update]);

  const value = useMemo(() => ({ notify, dismiss, update, confirm, run }), [confirm, dismiss, notify, run, update]);

  return (
    <FeedbackContext.Provider value={value}>
      {children}
      <div className="feedback-toasts" aria-live="polite" aria-atomic="true">
        {toasts.map((toast) => (
          <div className={`feedback-toast tone-${toast.tone}`} key={toast.id}>
            <ToastIcon tone={toast.tone} />
            <div>
              <strong>{toast.title}</strong>
              {toast.message && <span>{toast.message}</span>}
            </div>
            {toast.tone !== "loading" && (
              <button type="button" className="toast-close" aria-label="关闭提示" onClick={() => dismiss(toast.id)}>
                <X size={14} />
              </button>
            )}
          </div>
        ))}
      </div>
      {confirmRequest && (
        <div className="feedback-modal-backdrop" role="presentation" onMouseDown={() => settleConfirm(false)}>
          <section
            className={`feedback-modal ${confirmRequest.tone}`}
            role="dialog"
            aria-modal="true"
            aria-labelledby={`${confirmRequest.id}_title`}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="feedback-modal-icon">
              <AlertTriangle size={22} />
            </div>
            <h2 id={`${confirmRequest.id}_title`}>{confirmRequest.title}</h2>
            <p>{confirmRequest.message}</p>
            <div className="feedback-modal-actions">
              <button type="button" onClick={() => settleConfirm(false)}>{confirmRequest.cancelLabel}</button>
              <button type="button" className={confirmRequest.tone === "danger" ? "danger" : "primary"} onClick={() => settleConfirm(true)}>
                {confirmRequest.confirmLabel}
              </button>
            </div>
          </section>
        </div>
      )}
    </FeedbackContext.Provider>
  );
}

export function useFeedback(): FeedbackContextValue {
  const value = useContext(FeedbackContext);
  if (!value) throw new Error("useFeedback must be used inside FeedbackProvider");
  return value;
}

export function useActionFeedback() {
  const feedback = useFeedback();
  const [busyKey, setBusyKey] = useState<string | null>(null);

  async function runAction<T>(key: string, title: string, task: () => Promise<T>, options?: RunOptions): Promise<T | undefined> {
    setBusyKey(key);
    try {
      return await feedback.run(title, task, options);
    } finally {
      setBusyKey(null);
    }
  }

  function busyProps(key: string) {
    const busy = busyKey === key;
    return {
      "data-busy": busy ? "true" : undefined,
      "aria-busy": busy ? true : undefined
    };
  }

  return { ...feedback, busyKey, busyProps, runAction };
}

function ToastIcon({ tone }: { tone: FeedbackTone }) {
  if (tone === "loading") return <LoaderCircle className="toast-spin" size={18} />;
  if (tone === "success") return <CheckCircle2 size={18} />;
  if (tone === "error") return <XCircle size={18} />;
  return <Info size={18} />;
}
