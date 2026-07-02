"use client";

import { useNotificationStore } from "./store";

export const notify = {
  success: (message: string) =>
    useNotificationStore.getState().addNotification({ type: "success", message, persistent: false }),
  error: (message: string) =>
    useNotificationStore.getState().addNotification({ type: "error", message, persistent: true }),
  warning: (message: string) =>
    useNotificationStore.getState().addNotification({ type: "warning", message, persistent: false }),
  info: (message: string) =>
    useNotificationStore.getState().addNotification({ type: "info", message, persistent: false }),
};
