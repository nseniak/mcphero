import { useState, useCallback, useRef } from "react";
import { X } from "lucide-react";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message?: string;
  confirmLabel: string;
  /** Omit (or pass empty) to render a single-button alert — useful for
   *  surfacing a failure message where there's nothing to cancel. */
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
  destructive?: boolean;
  /** When set, the user must type this exact text to enable the confirm button. */
  requireText?: string;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
  destructive,
  requireText,
}: ConfirmDialogProps) {
  const [typedText, setTypedText] = useState("");

  if (!open) return null;

  const textMatches = !requireText || typedText === requireText;

  function handleConfirm() {
    setTypedText("");
    onConfirm();
  }

  function handleCancel() {
    setTypedText("");
    onCancel();
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/40" onClick={handleCancel} />
      <div role="dialog" className="relative bg-white rounded-lg shadow-lg p-6 max-w-sm w-full mx-4 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold text-zinc-900">{title}</h3>
          <button
            onClick={handleCancel}
            className="text-zinc-400 hover:text-zinc-600 transition-colors"
          >
            <X size={16} />
          </button>
        </div>
        {message && <p className="text-sm text-zinc-600">{message}</p>}
        {requireText && (
          <div>
            <p className="text-sm text-zinc-500 mb-1">
              Type <strong className="text-zinc-900">{requireText}</strong> to confirm:
            </p>
            <input
              value={typedText}
              onChange={(e) => setTypedText(e.target.value)}
              className="w-full px-3 py-2 border border-zinc-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-red-500"
              autoFocus
            />
          </div>
        )}
        <div className="flex justify-end gap-2">
          {cancelLabel && (
            <button
              onClick={handleCancel}
              className="px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 rounded"
            >
              {cancelLabel}
            </button>
          )}
          <button
            onClick={handleConfirm}
            disabled={!textMatches}
            className={`px-3 py-1.5 text-sm text-white rounded disabled:opacity-50 disabled:cursor-not-allowed ${
              destructive
                ? "bg-red-600 hover:bg-red-700"
                : "bg-blue-600 hover:bg-blue-700"
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

interface ConfirmState {
  open: boolean;
  title: string;
  message?: string;
  confirmLabel: string;
  cancelLabel: string;
  destructive: boolean;
  requireText?: string;
}

export function useConfirm() {
  const [state, setState] = useState<ConfirmState>({
    open: false,
    title: "",
    confirmLabel: "",
    cancelLabel: "",
    destructive: false,
  });

  const resolveRef = useRef<((value: boolean) => void) | null>(null);

  const confirm = useCallback(
    (opts: {
      title: string;
      message?: string;
      confirmLabel: string;
      cancelLabel: string;
      destructive?: boolean;
      requireText?: string;
    }): Promise<boolean> => {
      return new Promise((resolve) => {
        resolveRef.current = resolve;
        setState({
          open: true,
          title: opts.title,
          message: opts.message,
          confirmLabel: opts.confirmLabel,
          cancelLabel: opts.cancelLabel,
          destructive: opts.destructive ?? false,
          requireText: opts.requireText,
        });
      });
    },
    [],
  );

  const onConfirm = useCallback(() => {
    setState((s) => ({ ...s, open: false }));
    resolveRef.current?.(true);
  }, []);

  const onCancel = useCallback(() => {
    setState((s) => ({ ...s, open: false }));
    resolveRef.current?.(false);
  }, []);

  const dialogProps = {
    ...state,
    onConfirm,
    onCancel,
  };

  return { confirm, dialogProps };
}
