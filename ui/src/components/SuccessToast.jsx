import { useEffect } from "react";
import { createPortal } from "react-dom";

export default function SuccessToast({ title, children, onClose, error = false }) {
  useEffect(() => {
    const timeout = setTimeout(onClose, 60_000);
    return () => clearTimeout(timeout);
  }, [onClose]);

  return createPortal(
    <div className={error ? "success-toast toast-error" : "success-toast"}>
      <div role={error ? "alert" : "status"}>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
      <button type="button" aria-label="Tutup notifikasi" onClick={onClose}>×</button>
    </div>,
    document.body,
  );
}
