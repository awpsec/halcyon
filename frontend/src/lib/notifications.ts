type ToastTone = "info" | "success" | "error";

export type ToastMessage = {
  id: number;
  tone: ToastTone;
  title: string;
  detail?: string;
  href?: string;
};

type Listener = (toast: ToastMessage) => void;

const listeners = new Set<Listener>();
let nextToastId = 1;

export function pushToast(
  tone: ToastTone,
  title: string,
  detail?: string,
  options?: { href?: string },
) {
  const toast: ToastMessage = {
    id: nextToastId++,
    tone,
    title,
    detail,
    href: options?.href,
  };
  for (const listener of listeners) {
    listener(toast);
  }
}

export function subscribeToToasts(listener: Listener) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
