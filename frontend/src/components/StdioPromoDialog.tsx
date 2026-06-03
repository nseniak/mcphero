import { useState } from "react";
import { Bell, Check, Terminal, X } from "lucide-react";
import { track } from "../lib/analytics";

interface StdioPromoDialogProps {
  open: boolean;
  onClose: () => void;
  notifyEmail: string | null;
}

export function StdioPromoDialog({ open, onClose, notifyEmail }: StdioPromoDialogProps) {
  const [notified, setNotified] = useState(false);

  if (!open) return null;

  function handleNotify() {
    if (notifyEmail) {
      track("stdio_notify_requested", { notify_email: notifyEmail });
    }
    setNotified(true);
  }

  function handleClose() {
    setNotified(false);
    onClose();
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="fixed inset-0 bg-black/40" onClick={handleClose} />
      <div
        role="dialog"
        className="relative bg-white rounded-lg shadow-lg p-6 max-w-md w-full mx-4 space-y-4"
      >
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center justify-center w-8 h-8 rounded-md bg-zinc-900 text-white">
              <Terminal size={16} />
            </span>
            <h3 className="text-lg font-semibold text-zinc-900">
              Stdio MCPs are coming soon
            </h3>
          </div>
          <button
            onClick={handleClose}
            className="text-zinc-400 hover:text-zinc-600 transition-colors"
          >
            <X size={16} />
          </button>
        </div>
        <div className="space-y-3 text-sm text-zinc-600 leading-relaxed">
          <p>
            Stdio MCPs let you run command-line servers — local Python or Node
            scripts, or anything you can spawn locally — behind MCP Hero.
          </p>
          <p>
            We're planning to ship this as part of our Team plan.
          </p>
          <p className="text-zinc-500">
            For now, only remote HTTP MCPs (those with a{" "}
            <code className="font-mono text-xs">url</code> field) are supported.
          </p>
        </div>
        {notified ? (
          <div className="flex items-center gap-2 px-3 py-2 bg-green-50 border border-green-200 rounded text-sm text-green-800">
            <Check size={14} className="shrink-0" />
            <span>
              Thanks! We'll email{" "}
              {notifyEmail ? (
                <span className="font-medium">{notifyEmail}</span>
              ) : (
                "you"
              )}{" "}
              when stdio ships.
            </span>
          </div>
        ) : null}
        {!notified && (
          <div className="flex justify-end">
            {notifyEmail ? (
              <button
                onClick={handleNotify}
                className="px-4 py-2 bg-zinc-900 text-white text-sm rounded hover:bg-zinc-800 flex items-center gap-1.5"
              >
                <Bell size={14} /> Email me when it's ready
              </button>
            ) : (
              <button
                onClick={handleClose}
                className="px-4 py-2 bg-zinc-900 text-white text-sm rounded hover:bg-zinc-800"
              >
                Got it
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
