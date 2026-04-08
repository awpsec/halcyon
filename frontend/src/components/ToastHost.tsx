import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { subscribeToToasts, type ToastMessage } from "../lib/notifications";

export function ToastHost() {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  useEffect(() => {
    return subscribeToToasts((toast) => {
      setToasts((current) => [...current, toast]);
      window.setTimeout(() => {
        setToasts((current) => current.filter((item) => item.id !== toast.id));
      }, 3200);
    });
  }, []);

  return (
    <div className="toast-stack" aria-live="polite">
      {toasts.map((toast) =>
        toast.href ? (
          <Link
            key={toast.id}
            className={`toast-card toast-link tone-${toast.tone}`}
            to={toast.href}
          >
            <strong>{toast.title}</strong>
            {toast.detail ? <small>{toast.detail}</small> : null}
          </Link>
        ) : (
          <div key={toast.id} className={`toast-card tone-${toast.tone}`}>
            <strong>{toast.title}</strong>
            {toast.detail ? <small>{toast.detail}</small> : null}
          </div>
        ),
      )}
    </div>
  );
}
