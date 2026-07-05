"use client";

import { useEffect } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { CheckCircle, AlertTriangle, Info, X } from "lucide-react";
import { useNotificationStore, type AppNotification, type NotificationType } from "@/lib/store";
import { C } from "@/lib/colors";

// Errors stay up longer — they're the ones the operator actually needs to read.
const AUTO_DISMISS_MS: Record<NotificationType, number> = {
  success: 5000,
  info: 5000,
  warning: 5000,
  error: 8000,
};

const ICON: Record<NotificationType, typeof CheckCircle> = {
  success: CheckCircle,
  error: AlertTriangle,
  warning: AlertTriangle,
  info: Info,
};

const TYPE_COLOR: Record<NotificationType, string> = {
  success: C.online,
  error: C.error,
  warning: C.warning,
  info: C.info,
};

const MAX_VISIBLE = 4;

function ToastItem({ notification }: { notification: AppNotification }) {
  const dismissNotification = useNotificationStore((s) => s.dismissNotification);
  const { id, type, message } = notification;

  useEffect(() => {
    const timer = setTimeout(() => dismissNotification(id), AUTO_DISMISS_MS[type]);
    return () => clearTimeout(timer);
  }, [id, type, dismissNotification]);

  const Icon = ICON[type];
  const color = TYPE_COLOR[type];

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 12, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 8, scale: 0.96, transition: { duration: 0.15, ease: "easeOut" } }}
      transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
      className="pointer-events-auto flex items-start gap-2.5 rounded-lg pl-3.5 pr-2.5 py-3 w-full"
      style={{
        backgroundColor: C.bgSurface,
        border: `1px solid ${color}4D`,
        boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
      }}
    >
      <Icon size={16} style={{ color }} className="mt-0.5 shrink-0" aria-hidden="true" />
      <p
        className="flex-1 min-w-0 text-[13px] leading-snug break-words"
        style={{ color: C.textPrimary }}
      >
        {message}
      </p>
      <button
        type="button"
        onClick={() => dismissNotification(id)}
        aria-label="Dismiss notification"
        className="shrink-0 p-1 rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2"
        style={{ color: C.textMuted }}
        onMouseEnter={(e) => (e.currentTarget.style.color = C.textPrimary)}
        onMouseLeave={(e) => (e.currentTarget.style.color = C.textMuted)}
      >
        <X size={14} />
      </button>
    </motion.div>
  );
}

export default function ToastRenderer() {
  const notifications = useNotificationStore((s) => s.notifications);
  const visible = notifications.filter((n) => !n.dismissed).slice(-MAX_VISIBLE);

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 w-[360px] max-w-[calc(100vw-2rem)]"
    >
      <AnimatePresence initial={false}>
        {visible.map((n) => (
          <ToastItem key={n.id} notification={n} />
        ))}
      </AnimatePresence>
    </div>
  );
}
